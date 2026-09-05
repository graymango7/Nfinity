"""
Gig-Score API — 9/2 완성판 (8/26 스텁을 설계서 기준 실제 지표로 교체)

- GET /api/v1/gig-score/{user_id}

원래 8/26 뼈대는 "거래가 얼마나 많고(activity)" + "지출이 얼마나 안정적인지(stability)"만
보는 자리표시자였고, 설계서에 있던 진짜 지표(세금 파킹 성실도, 업무 고정비 유지율,
플랫폼별 정산 완료율)는 "8/30-31에 교체 예정"이라고만 적혀 있었는데 끝까지 스텁으로
남아있었습니다. 9/2에 수입 플랫폼 연결 기능(income_sources/income_events, 9/2 추가)이
생기면서 아래처럼 실제 지표로 교체합니다 — activity(거래 건수)는 "돈을 얼마나 많이
버는가"가 아니라 그냥 "데이터가 얼마나 쌓였는가"라 신용 지표로 쓰기엔 부적절해서 뺐습니다.

1. 플랫폼 연결 성실도 (platform_connect_rate): 등록 가능한 수입 플랫폼 중 실제로
   "연결"해서 추적 중인 비율. N잡러가 자기 수입원을 얼마나 빠짐없이 챙기고 관리하는지의
   대리 지표입니다 — income.py의 연결 흐름과 바로 이어집니다.
2. 지출 안정성 (spending_stability): 8/26 산식 그대로 유지 — 거래 금액의 변동계수
   (표준편차/평균)가 낮을수록(지출이 예측 가능할수록) 높은 점수.
3. 업무경비 인정률 (expense_diligence): "세금 파킹 성실도"를 새 ML 없이 가장 정직하게
   근사한 값입니다. data_pipeline/expense_classifier.py(조소현님이 만든 AI 절세장부
   분류 로직 — app/routers/expense.py가 이미 프로덕션에서 쓰고 있는 바로 그 코드)로
   최근 거래 15건을 분류해서, 업무 경비로 인정될 확률(prob)이 APPROVAL_THRESHOLD(50)
   이상인 비율을 씁니다. 경비 증빙을 꾸준히 만들어두는 사람일수록 실제 세금 신고 때
   유리하다는 논리를 그대로 점수화한 것입니다.

가중치는 세 지표를 동등에 가깝게(0.35/0.35/0.30) 반영합니다. 1000점 만점.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import GigScoreResponse
from app.security import verify_api_key

try:
    from data_pipeline.expense_classifier import APPROVAL_THRESHOLD, classify_one
except ImportError:  # uvicorn을 data_pipeline 폴더 안에서 띄우는 등 경로가 다를 때 대비
    from expense_classifier import APPROVAL_THRESHOLD, classify_one  # type: ignore

router = APIRouter(prefix="/api/v1/gig-score", tags=["gig-score"], dependencies=[Depends(verify_api_key)])


def _platform_connect_rate(db: Session, user_id: str) -> float:
    """연결 가능한 수입 플랫폼 중 실제로 연결된 비율. income_sources가 아예 없는
    유저(데모 5명 외 일반 시드 유저)는 이 지표를 계산할 수 없으니 None 취급으로
    0.0을 반환합니다(가중치는 그대로 두되, 나머지 두 지표로 점수를 매깁니다)."""
    row = db.execute(
        text(
            "SELECT count(*) AS total, count(*) FILTER (WHERE connected) AS connected "
            "FROM income_sources WHERE user_id = :uid"
        ),
        {"uid": user_id},
    ).mappings().first()
    if not row or not row["total"]:
        return 0.0
    return row["connected"] / row["total"]


def _expense_diligence(db: Session, user_id: str) -> float:
    """최근 거래 15건을 AI 절세장부 로직으로 분류해서, 업무경비로 인정될 확률이
    임계값 이상인 비율을 계산합니다. app/routers/expense.py의 classify_expense()와
    똑같은 호출 방식입니다(거래 없으면 0.0)."""
    user_row = db.execute(
        text("SELECT persona FROM users WHERE user_id = :uid"), {"uid": user_id}
    ).mappings().first()
    job = (user_row["persona"] or "") if user_row else ""

    rows = db.execute(
        text(
            "SELECT merchant_name, amount, timestamp FROM transactions "
            "WHERE user_id = :uid ORDER BY timestamp DESC LIMIT 15"
        ),
        {"uid": user_id},
    ).mappings().all()
    if not rows:
        return 0.0

    approved = 0
    for r in rows:
        result = classify_one(job, r["merchant_name"] or "", r["timestamp"].hour, int(r["amount"]))
        if result["prob"] >= APPROVAL_THRESHOLD:
            approved += 1
    return approved / len(rows)


@router.get("/{user_id}", response_model=GigScoreResponse)
def get_gig_score(user_id: str, db: Session = Depends(get_db)):
    profile = db.execute(
        text("SELECT * FROM user_risk_profiles WHERE user_id = :uid"), {"uid": user_id}
    ).mappings().first()
    if not profile:
        raise HTTPException(status_code=404, detail="유저 프로파일이 없습니다.")

    # 변동계수(표준편차/평균)가 낮을수록 "지출이 규칙적"이라고 보고 안정성 점수를 높게 줍니다.
    avg_amt = float(profile["avg_transaction_amount"] or 0)
    std_amt = float(profile["std_transaction_amount"] or 0)
    stability = 1.0
    if avg_amt > 0:
        cv = std_amt / avg_amt
        stability = max(0.0, 1.0 - min(cv, 1.0))

    connect_rate = _platform_connect_rate(db, user_id)
    expense_diligence = _expense_diligence(db, user_id)

    score = int((connect_rate * 0.35 + stability * 0.35 + expense_diligence * 0.30) * 1000)

    return GigScoreResponse(
        user_id=user_id,
        score=score,
        components={
            "platform_connect_rate": round(connect_rate, 2),
            "spending_stability": round(stability, 2),
            "expense_diligence": round(expense_diligence, 2),
        },
        message="수입 플랫폼 연결 성실도 + 지출 안정성 + 업무경비 인정률 기반 종합 점수입니다.",
    )

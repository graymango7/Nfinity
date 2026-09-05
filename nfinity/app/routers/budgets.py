"""
예산통제 API

- POST /api/v1/budgets              : 카테고리별 예산 한도 설정
- GET  /api/v1/budgets/usage        : 이번 달 카테고리별 사용량 (Redis에서 실시간으로 읽음) +
  이미 넘은 알림 임계값(crossed_thresholds)
- POST /api/v1/budgets/record-spending : 거래 하나만큼 누적 지출에 더하기 (지금은 수동 호출용.
  나중엔 카테고리분류 파이프라인에서 거래가 들어올 때마다 자동으로 이 함수를 호출하게 연결합니다)
- GET  /api/v1/budgets/forecast     : 월말 예상 지출 = 기존 단순 선형 예측 + 이번 달 안에
  아직 안 나온 정기결제 예상액 (8/30, 소현님의 data_pipeline/recurring_detector.py 연동)

8/30 업데이트: 소현님이 만든 정기결제 감지(detect_recurring_payments)와 예산 임계값 체크
(check_crossed_thresholds)를 실제로 연결했습니다. 기존 Redis 기반 "지금까지 쓴 돈" 계산은
그대로 두고, 거기에 "아직 안 나온 정기결제 예상액"만 더하는 방식입니다 — 이미 검증된
기존 계산을 갈아엎지 않고 얹는 구조라, 정기결제가 없는 카테고리는 이전과 동일하게 동작합니다.

9/2 버그 수정: "이번 달"을 실서버 시각(date.today())으로 계산하고 있었는데, mock 데이터가
2026-08-22에 끝나서 실제 달력이 9월로 넘어가면(딱 심사 기간, 9/7~9/11) 이번 달 실거래가
0건이 되어 예산 사용량이 전부 0으로 보이는 문제가 있었습니다. app/demo_clock.py의
get_demo_now()로 "데이터 기준 지금"을 쓰도록 고쳤습니다 — 아래 모든 today는 그 값입니다.
"""
from calendar import monthrange
from datetime import datetime

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import get_db
from app.demo_clock import get_demo_now
from app.models import BudgetCreate, BudgetForecast, BudgetUsage, RecurringPayment
from app.redis_client import add_spending, get_spending
from app.security import verify_api_key

try:
    from data_pipeline.categorizer import categorize_merchant
    from data_pipeline.recurring_detector import check_crossed_thresholds, detect_recurring_payments
except ImportError:  # uvicorn을 data_pipeline 폴더 안에서 띄우는 등 경로가 다를 때 대비
    from categorizer import categorize_merchant  # type: ignore
    from recurring_detector import check_crossed_thresholds, detect_recurring_payments  # type: ignore

router = APIRouter(prefix="/api/v1/budgets", tags=["budgets"], dependencies=[Depends(verify_api_key)])

_DEFAULT_THRESHOLDS_PCT = [50, 80, 90, 100, 120]  # alert_thresholds가 비어있을 때의 기본값


def _crossed_thresholds_pct(usage_rate: float, alert_thresholds_fraction) -> list[int]:
    """budgets.alert_thresholds는 0.5/0.8 같은 '비율'로 저장되어 있어서, 소현님 함수가
    기대하는 '%' 단위(50/80)로 변환해서 넘겨줍니다."""
    thresholds_pct = (
        [round(float(t) * 100) for t in alert_thresholds_fraction]
        if alert_thresholds_fraction
        else _DEFAULT_THRESHOLDS_PCT
    )
    return check_crossed_thresholds(usage_rate, alert_thresholds=thresholds_pct)


def _load_transactions_df(db: Session, user_id: str) -> pd.DataFrame:
    """detect_recurring_payments()가 기대하는 컬럼(user_id, merchant_name, amount,
    timestamp) 그대로 유저의 전체 거래 이력을 DataFrame으로 불러옵니다."""
    rows = db.execute(
        text(
            "SELECT user_id, merchant_name, amount, timestamp FROM transactions "
            "WHERE user_id = :uid ORDER BY timestamp"
        ),
        {"uid": user_id},
    ).mappings().all()
    return pd.DataFrame(
        [{"user_id": r["user_id"], "merchant_name": r["merchant_name"],
          "amount": float(r["amount"]), "timestamp": r["timestamp"]} for r in rows]
    )


@router.post("", response_model=BudgetUsage)
def create_or_update_budget(body: BudgetCreate, db: Session = Depends(get_db)):
    """카테고리별 월 예산 한도를 설정합니다. 이미 있으면 한도만 갱신합니다."""
    row = db.execute(
        text(
            """
            INSERT INTO budgets (user_id, category, period, limit_amount)
            VALUES (:uid, :cat, :period, :limit_amount)
            ON CONFLICT (user_id, category, period) DO UPDATE
                SET limit_amount = EXCLUDED.limit_amount, updated_at = now()
            RETURNING alert_thresholds
            """
        ),
        {
            "uid": body.user_id,
            "cat": body.category,
            "period": body.period,
            "limit_amount": body.limit_amount,
        },
    ).mappings().first()
    db.commit()

    spent = get_spending(body.user_id, body.category, body.period, now=get_demo_now(db))
    usage_rate = (spent / body.limit_amount) if body.limit_amount else 0.0
    return BudgetUsage(
        user_id=body.user_id,
        category=body.category,
        period=body.period,
        spent_amount=spent,
        limit_amount=body.limit_amount,
        usage_rate=usage_rate,
        crossed_thresholds=_crossed_thresholds_pct(usage_rate, row["alert_thresholds"] if row else None),
    )


@router.get("/usage", response_model=list[BudgetUsage])
def get_usage(user_id: str, period: str = "monthly", db: Session = Depends(get_db)):
    """유저가 설정해둔 모든 카테고리의 이번 기간 사용량을 반환합니다."""
    rows = db.execute(
        text(
            "SELECT category, limit_amount, alert_thresholds FROM budgets "
            "WHERE user_id = :uid AND period = :period"
        ),
        {"uid": user_id, "period": period},
    ).mappings().all()

    if not rows:
        raise HTTPException(
            status_code=404,
            detail="설정된 예산이 없습니다. 먼저 POST /api/v1/budgets 로 예산을 만들어주세요.",
        )

    demo_now = get_demo_now(db)
    result = []
    for r in rows:
        spent = get_spending(user_id, r["category"], period, now=demo_now)
        limit_amount = float(r["limit_amount"])
        usage_rate = (spent / limit_amount) if limit_amount else 0.0
        result.append(
            BudgetUsage(
                user_id=user_id,
                category=r["category"],
                period=period,
                spent_amount=spent,
                limit_amount=limit_amount,
                usage_rate=usage_rate,
                crossed_thresholds=_crossed_thresholds_pct(usage_rate, r["alert_thresholds"]),
            )
        )
    return result


@router.post("/record-spending")
def record_spending(user_id: str, category: str, amount: float, period: str = "monthly", db: Session = Depends(get_db)):
    """
    거래 하나만큼 누적 지출에 더합니다.
    지금은 테스트를 위해 직접 호출하는 용도이고, 나중엔 하은님의 카테고리분류
    파이프라인이 거래를 저장할 때마다 자동으로 이 로직을 같이 실행하도록 연결할 예정입니다.
    """
    new_total = add_spending(user_id, category, amount, period, now=get_demo_now(db))
    return {"user_id": user_id, "category": category, "period": period, "total_spent": new_total}


@router.get("/forecast", response_model=BudgetForecast)
def get_forecast(user_id: str, category: str, period: str = "monthly", db: Session = Depends(get_db)):
    """
    월말 예상 지출을 계산합니다.

    기본 예측(base): (지금까지 쓴 돈 / 지난 날짜 수) * 이번 달 전체 날짜 수
    → "지금까지의 지출 속도가 이번 달 내내 유지된다면"을 가정한 가장 단순한 선형 예측.
    여기에 8/30부터는, 이 카테고리에 속하는 가맹점 중 이번 달 안에 아직 결제 안 된
    정기결제(넷플릭스 등)의 예상 금액을 더합니다 — data_pipeline/recurring_detector.py.
    """
    row = db.execute(
        text(
            "SELECT limit_amount FROM budgets "
            "WHERE user_id = :uid AND category = :cat AND period = :period"
        ),
        {"uid": user_id, "cat": category, "period": period},
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="설정된 예산이 없습니다.")

    limit_amount = float(row["limit_amount"])
    demo_now = get_demo_now(db)
    spent = get_spending(user_id, category, period, now=demo_now)

    today = demo_now.date()
    days_in_month = monthrange(today.year, today.month)[1]
    days_elapsed = today.day
    daily_rate = spent / days_elapsed if days_elapsed else 0.0
    base_predicted_total = daily_rate * days_in_month

    # 이 유저의 전체 거래 이력에서 정기결제 후보를 찾고, 이 카테고리에 속하는
    # 가맹점만, 그리고 "이번 달 안 + 아직 안 지나간" 것만 골라서 더합니다.
    recurring_payments: list[RecurringPayment] = []
    recurring_addition = 0.0
    tx_df = _load_transactions_df(db, user_id)
    if not tx_df.empty:
        recurring_df = detect_recurring_payments(tx_df)
        if not recurring_df.empty:
            recurring_df = recurring_df[
                recurring_df["merchant_name"].apply(categorize_merchant) == category
            ]
            now = datetime.combine(today, datetime.min.time())
            month_start = now.replace(day=1)
            next_month = (month_start.replace(day=28) + pd.Timedelta(days=4)).replace(day=1)
            recurring_df = recurring_df.copy()
            recurring_df["next_expected_date"] = pd.to_datetime(recurring_df["next_expected_date"])
            upcoming = recurring_df[
                (recurring_df["next_expected_date"] > now) & (recurring_df["next_expected_date"] < next_month)
            ]
            for _, r in upcoming.iterrows():
                recurring_payments.append(
                    RecurringPayment(
                        merchant_name=r["merchant_name"],
                        expected_amount=int(r["avg_amount"]),
                        expected_date=r["next_expected_date"].date().isoformat(),
                    )
                )
            recurring_addition = float(upcoming["avg_amount"].sum())

    predicted_total = base_predicted_total + recurring_addition

    return BudgetForecast(
        user_id=user_id,
        category=category,
        current_spent=spent,
        predicted_total=predicted_total,
        base_predicted_total=base_predicted_total,
        predicted_overage=max(0.0, predicted_total - limit_amount),
        confidence=min(days_elapsed / days_in_month, 1.0),
        recurring_payments=recurring_payments,
    )

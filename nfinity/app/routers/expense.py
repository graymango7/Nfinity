"""
AI 절세장부(경비분류) API — 8/28 실제 로직 연동판

- GET /api/v1/expense/classify?user_id=...&limit=15

조소현님이 완성한 프롬프트/분류 로직(data_pipeline/expense_classifier.py)을 그대로 가져다 씁니다.
이 라우터가 하는 일은 딱 하나 — "DB에서 그 유저의 실제 거래를 읽어와서 classify_one()에
하나씩 넘기고, 결과를 프론트와 합의된 모양(transactions_queue + current_estimated_refund)으로
포장해서 돌려주는 것"입니다. 분류 로직 자체(프롬프트, few-shot, 규칙 기반 폴백)는 전혀
손대지 않았습니다.

유저의 "직업(job)" 정보는 users 테이블의 persona 컬럼을 그대로 씁니다 — 이미
'프리랜서 마케터/데이터분석가', '배달_투잡러' 같은 문자열이 들어있어서 조소현님 로직의
JOB_CONTEXT_MERCHANTS 판단("개발자"/"마케터" 문자열 포함 여부)과 그대로 맞습니다.

Mock/실제 API 전환은 이 파일이 아니라 data_pipeline/expense_classifier.py의 MOCK_MODE가
결정합니다 (.env에 GEMINI_API_KEY가 있으면 자동으로 실제 Gemini 호출로 전환).
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import ExpenseClassifyResponse
from app.security import verify_api_key

try:
    from data_pipeline.expense_classifier import (
        APPROVAL_THRESHOLD,
        ASSUMED_MARGINAL_TAX_RATE,
        classify_one,
    )
except ImportError:  # uvicorn을 data_pipeline 폴더 안에서 띄우는 등 경로가 다를 때 대비
    from expense_classifier import (  # type: ignore
        APPROVAL_THRESHOLD,
        ASSUMED_MARGINAL_TAX_RATE,
        classify_one,
    )

router = APIRouter(prefix="/api/v1/expense", tags=["expense"], dependencies=[Depends(verify_api_key)])


@router.get("/classify", response_model=ExpenseClassifyResponse)
def classify_expense(user_id: str, limit: int = 15, db: Session = Depends(get_db)):
    """해당 유저의 최근 거래 <limit>건을 실제로 분류해서 스와이프 큐로 반환합니다."""
    user_row = db.execute(
        text("SELECT persona FROM users WHERE user_id = :uid"),
        {"uid": user_id},
    ).mappings().first()
    if not user_row:
        raise HTTPException(status_code=404, detail="존재하지 않는 유저입니다.")
    job = user_row["persona"] or ""

    rows = db.execute(
        text(
            "SELECT merchant_name, amount, timestamp FROM transactions "
            "WHERE user_id = :uid ORDER BY timestamp DESC LIMIT :limit"
        ),
        {"uid": user_id, "limit": limit},
    ).mappings().all()
    if not rows:
        raise HTTPException(status_code=404, detail="해당 유저의 거래 내역이 없습니다.")

    queue = []
    total_refund = 0
    for i, r in enumerate(rows):
        amount = int(r["amount"])
        result = classify_one(job, r["merchant_name"], r["timestamp"].hour, amount)
        queue.append(
            {
                "id": i + 1,
                "merchant": r["merchant_name"],
                "amount": amount,
                "ai_tag": result["ai_tag"],
                "prob": result["prob"],
            }
        )
        if result["prob"] >= APPROVAL_THRESHOLD:
            total_refund += int(amount * ASSUMED_MARGINAL_TAX_RATE)

    return ExpenseClassifyResponse(transactions_queue=queue, current_estimated_refund=total_refund)

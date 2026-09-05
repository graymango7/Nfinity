"""
세금 · 건강보험료 추정 API (9/5 신규)

- GET /api/v1/tax/estimate/{user_id}

app/tax.py의 계산 엔진에 이 유저의 실제 정산 수입을 넣어서, 내년 5월 종합소득세와
건강보험료를 추정합니다. 연 환산은 "최근 90일 정산완료 수입 × 4"로 합니다 — 이 데모의
수입 데이터가 90일치라 그 이상은 만들어낼 수 없고, 없는 기간을 0으로 두면 세금이 실제보다
훨씬 낮게 나와서 오히려 오해를 줍니다.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import get_db
from app.demo_clock import get_demo_now
from app.security import verify_api_key
from app.tax import DISCLAIMER, estimate_health_insurance, estimate_income_tax, expense_rate_for

router = APIRouter(prefix="/api/v1/tax", tags=["tax"], dependencies=[Depends(verify_api_key)])

INCOME_LOOKBACK_DAYS = 90


def load_annual_income(db: Session, user_id: str):
    """연결된 플랫폼의 최근 90일 정산완료 수입을 연 환산하고, 유형별로 나눠 돌려줍니다.

    반환: (사업소득 연환산, 근로소득 연환산, 플랫폼 유형 목록)
    '본업' 유형은 근로소득이라 사업소득 경비율·종소세 계산에서 분리합니다.
    """
    from datetime import timedelta

    demo_now = get_demo_now(db)
    since = demo_now - timedelta(days=INCOME_LOOKBACK_DAYS)
    rows = db.execute(
        text(
            """
            SELECT s.platform_type, COALESCE(SUM(e.amount), 0) AS total
            FROM income_events e
            JOIN income_sources s ON s.source_id = e.source_id
            WHERE s.user_id = :uid AND s.connected = TRUE
              AND e.status = '정산완료' AND e.settled_at >= :since
            GROUP BY s.platform_type
            """
        ),
        {"uid": user_id, "since": since},
    ).mappings().all()

    factor = 365 / INCOME_LOOKBACK_DAYS
    business, employment, types = 0.0, 0.0, []
    for r in rows:
        annual = float(r["total"]) * factor
        types.append(r["platform_type"])
        if r["platform_type"] == "본업":
            employment += annual
        else:
            business += annual
    return business, employment, types


@router.get("/estimate/{user_id}")
def estimate(user_id: str, db: Session = Depends(get_db)):
    business, employment, types = load_annual_income(db, user_id)
    if business <= 0 and employment <= 0:
        raise HTTPException(status_code=404, detail="연결된 수입 플랫폼이 없어 추정할 수 없습니다.")

    rate = expense_rate_for(types)
    income_tax = estimate_income_tax(business, rate)
    # 건강보험료는 사업소득 기준으로 봅니다. 본업(근로소득)이 있으면 직장가입자로 보고
    # 급여 외 소득 2,000만원 초과분에만 소득월액보험료가 붙습니다.
    health = estimate_health_insurance(business, has_employment_income=employment > 0)

    return {
        "user_id": user_id,
        "annual_business_income": round(business),
        "annual_employment_income": round(employment),
        "platform_types": sorted(set(types)),
        "income_tax": income_tax,
        "health_insurance": health,
        "monthly_reserve_total": income_tax["monthly_reserve"] + health["total_monthly"],
        "disclaimer": DISCLAIMER,
    }

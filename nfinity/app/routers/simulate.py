"""
직접 입력 시뮬레이션 API (9/6 신규) — POST /api/v1/simulate/cashflow

무엇을 하나
-----------
시연용 인물의 데이터가 아니라, **보는 사람이 직접 입력한 수입·지출 조건**으로 45일 현금흐름을
계산합니다. 정산 주기와 금액만 넣으면 잔고가 언제 최소 안전선을 밑도는지, 정산이 밀리거나
수입이 줄면 그 시점이 며칠 앞당겨지는지를 그대로 돌려줍니다.

왜 필요한가
-----------
지금까지 이 서비스는 준비된 인물 3명을 **구경하는** 형태였습니다. 그러면 보는 사람은
"이 사람들 데이터니까 이렇게 나오겠지"에서 멈추고, 자기 상황에 대입해볼 방법이 없습니다.
숫자를 직접 넣어 결과가 바뀌는 걸 보는 순간 예측이 남의 것에서 자기 것이 됩니다.

계산 방식은 기존 엔진(app/cashflow.py)과 동일한 논리를 씁니다 — 정산 주기마다 수입을 배치하고,
고정비는 매달 같은 날, 변동비는 매일 균등하게 빼면서 일별 잔액을 누적합니다. DB를 쓰지 않으므로
입력값은 저장되지 않고, 계산 결과만 응답으로 돌려줍니다(개인정보를 남기지 않기 위함).
"""
from datetime import date, timedelta

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.security import verify_api_key

router = APIRouter(prefix="/api/v1/simulate", tags=["simulate"], dependencies=[Depends(verify_api_key)])

HORIZON_DAYS = 45
MAX_SOURCES = 5


class IncomeSourceInput(BaseModel):
    """수입원 하나. '며칠마다 얼마씩 들어오는가'만 받습니다."""

    name: str = Field(default="수입원", max_length=30)
    cycle_days: int = Field(ge=1, le=90)
    amount: float = Field(ge=0, le=100_000_000)
    days_until_next: int = Field(default=0, ge=0, le=90)


class SimulationInput(BaseModel):
    current_balance: float = Field(ge=0, le=1_000_000_000)
    min_safety_balance: float = Field(default=300_000, ge=0, le=100_000_000)
    fixed_monthly_cost: float = Field(default=0, ge=0, le=100_000_000)
    fixed_cost_day: int = Field(default=25, ge=1, le=28)
    daily_variable_cost: float = Field(default=0, ge=0, le=10_000_000)
    sources: list[IncomeSourceInput] = Field(default_factory=list, max_length=MAX_SOURCES)


def _run(inp: SimulationInput, delay_days: int = 0, income_ratio: float = 1.0) -> list[dict]:
    """하루씩 잔액을 굴립니다. delay_days는 정산 지연, income_ratio는 수입 감소 가정입니다."""
    today = date.today()
    balance = float(inp.current_balance)
    points = []

    # 각 수입원의 다음 정산일을 미리 배치해둡니다.
    upcoming = {}
    for idx, src in enumerate(inp.sources):
        upcoming[idx] = src.days_until_next + delay_days

    for day in range(HORIZON_DAYS + 1):
        current = today + timedelta(days=day)
        events = []

        if day > 0:
            balance -= inp.daily_variable_cost
            if current.day == inp.fixed_cost_day and inp.fixed_monthly_cost:
                balance -= inp.fixed_monthly_cost
                events.append("고정비 -" + format(int(inp.fixed_monthly_cost), ",") + "원")

            for idx, src in enumerate(inp.sources):
                if src.cycle_days <= 0 or src.amount <= 0:
                    continue
                if day == upcoming[idx]:
                    amount = src.amount * income_ratio
                    balance += amount
                    events.append(src.name + " +" + format(int(amount), ",") + "원")
                    upcoming[idx] += src.cycle_days

        points.append({"date": current.isoformat(), "balance": round(balance), "events": events})
    return points


def _first_shortage(points: list[dict], floor: float):
    for p in points:
        if p["balance"] < floor:
            return p["date"]
    return None


@router.post("/cashflow")
def simulate_cashflow(body: SimulationInput):
    floor = float(body.min_safety_balance)
    base = _run(body)
    scenarios = {
        "기본": base,
        "입금지연": _run(body, delay_days=10),
        "수입감소": _run(body, income_ratio=0.7),
    }
    shortages = {k: _first_shortage(v, floor) for k, v in scenarios.items()}

    monthly_income = sum(
        (s.amount * (30.0 / s.cycle_days)) for s in body.sources if s.cycle_days > 0
    )
    monthly_cost = body.fixed_monthly_cost + body.daily_variable_cost * 30

    return {
        "horizon_days": HORIZON_DAYS,
        "min_safety_balance": floor,
        "daily_balances": base,
        "scenarios": scenarios,
        "shortage_dates": shortages,
        "monthly_income_estimate": round(monthly_income),
        "monthly_cost_estimate": round(monthly_cost),
        "net_monthly": round(monthly_income - monthly_cost),
        "note": (
            "입력값은 저장하지 않고 계산에만 사용합니다. 시연 인물과 동일한 방식으로 계산하며, "
            "실제 정산일·금액의 변동은 반영되지 않은 단순화된 추정입니다."
        ),
    }

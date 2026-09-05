"""
Risk Shield / Dynamic Parking 계산 엔진 (9/2 세 번째 업데이트)

기획서(2026 금융 AI Challenge 기획서 1차) 4번·5-3번이 설명하는 두 핵심 기능을
실제 계산 로직으로 옮긴 파일입니다.

- Risk Shield: "월별 합계가 아닌 일별 예상 잔액, 최저잔액, 다음 현금 부족 예상일"
- Dynamic Parking: "수입 변동성과 납부 예정액을 반영한 세금·건보료 준비 권장액"

★ 중요: 이 앱은 실제 계좌 잔액을 갖고 있지 않습니다(오픈뱅킹 연동은 MVP 범위 밖).
대신 이미 있는 두 데이터(수입: income_events, 지출: transactions)의 누적 차이로
"가상 잔액"을 근사합니다 — 실제 계좌잔액이 아니라 이 데모 안에서의 추정치라는 점을
API 응답과 화면 양쪽에 항상 명시합니다(app/security_headers.py처럼 이 파일도 안전장치
목적이 큽니다).

세금·건보료 준비율(TAX_RESERVE_RATE, INSURANCE_RESERVE_RATE)은 실제 세법·건강보험료
산정식이 아니라, 프리랜서에게 흔히 권장되는 "수입의 일정 비율을 미리 떼어놓기" 관행을
단순화한 예시 비율입니다. 기획서 5-2/8번이 요구하는 대로 "확정 세액을 가장하지
않는다"는 원칙을 지키기 위해, 화면에는 반드시 "간이 추정치, 실제 세액/보험료 아님"
문구를 같이 보여줘야 합니다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

import logging

import pandas as pd
from sqlalchemy import text
from sqlalchemy.orm import Session

from data_pipeline.recurring_detector import detect_recurring_payments

# ------------------------------------------------------------------
# 0. 설정값 (전부 "간이 추정치"라는 전제 — 코드 상단 docstring 참고)
# ------------------------------------------------------------------
logger = logging.getLogger("nfinity.cashflow")

TAX_RESERVE_RATE = 0.08        # 종합소득세 예비율(간이 추정)
INSURANCE_RESERVE_RATE = 0.07  # 지역가입자 건강보험료 예비율(간이 추정)
DEFAULT_MIN_SAFETY_BALANCE = 300_000  # 사용자가 직접 설정 안 했을 때 기본 최소 안전잔액
SIMULATION_HORIZON_DAYS = 45
INCOME_LOOKBACK_DAYS = 90


@dataclass
class DailyBalancePoint:
    day: date
    balance: float
    events: list[str] = field(default_factory=list)  # 그 날 반영된 사건(설명가능성용)


def _load_income_events(db: Session, user_id: str) -> pd.DataFrame:
    """연결된(connected=true) 플랫폼의 수입 이벤트만 불러옵니다. 연결 안 한 플랫폼은
    사용자가 아직 앱에 알려주지 않은 수입이라, 실제 오픈뱅킹 연동과 마찬가지로
    Risk Shield 계산에서도 제외합니다 — '연결할수록 예측이 정확해진다'는 동기부여와
    맞닿아 있습니다."""
    rows = db.execute(
        text(
            "SELECT ie.source_id, s.platform_name, ie.amount, ie.status, ie.settled_at "
            "FROM income_events ie JOIN income_sources s ON s.source_id = ie.source_id "
            "WHERE ie.user_id = :uid AND s.connected = TRUE "
            "ORDER BY ie.settled_at"
        ),
        {"uid": user_id},
    ).mappings().all()
    return pd.DataFrame(
        [
            {
                "source_id": str(r["source_id"]),
                "platform_name": r["platform_name"],
                "amount": float(r["amount"]),
                "status": r["status"],
                "settled_at": r["settled_at"],
            }
            for r in rows
        ]
    )


def _load_transactions(db: Session, user_id: str) -> pd.DataFrame:
    rows = db.execute(
        text(
            "SELECT merchant_name, amount, timestamp FROM transactions "
            "WHERE user_id = :uid ORDER BY timestamp"
        ),
        {"uid": user_id},
    ).mappings().all()
    return pd.DataFrame(
        [{"merchant_name": r["merchant_name"], "amount": float(r["amount"]), "timestamp": r["timestamp"]} for r in rows]
    )


def _get_min_safety_balance(db: Session, user_id: str) -> float:
    row = db.execute(
        text("SELECT min_safety_balance FROM user_settings WHERE user_id = :uid"),
        {"uid": user_id},
    ).mappings().first()
    if row and row["min_safety_balance"] is not None:
        return float(row["min_safety_balance"])
    return float(DEFAULT_MIN_SAFETY_BALANCE)


def _get_current_balance(db: Session, user_id: str, income_df: pd.DataFrame, tx_df: pd.DataFrame) -> tuple[float, str]:
    """시뮬레이션의 시작 잔액과 그 출처를 함께 돌려줍니다.

    9/5 수정 — 이전에는 무조건 `연결된 플랫폼의 정산완료 수입 합계 - 전체 지출 합계`로
    잔액을 유도했습니다. 그런데 이 앱은 (설계상) 사용자가 '연결'한 플랫폼의 수입만
    집계하는 반면 지출은 카드 거래 전부를 집계하기 때문에, 미연결 플랫폼이 하나라도
    있으면 그만큼 수입이 통째로 빠진 채 지출만 온전히 반영되어 잔액이 구조적으로
    음수 쪽으로 치우칩니다. 실제로 데모 페르소나 5명 중 3명이 마이너스 수백만원으로
    나왔고, 그 결과 45일 시뮬레이션이 전부 "1일 후 잔고 부족"만 반환해서 예측 기능이
    사실상 죽어 있었습니다.

    오픈뱅킹 연동이 없는 이 MVP에서 정직한 모델은 "사용자가 알려준 현재 잔액"을
    기준점으로 삼는 것입니다(실서비스라면 마이데이터로 채울 자리). 설정값이 없으면
    수입·지출을 같은 기간(최근 30일)으로 맞춰 순현금흐름을 추정하되, 위와 같은 이유로
    음수가 될 수 있어 0 밑으로는 내려가지 않게 막고 출처를 'estimated'로 표시합니다.
    """
    row = db.execute(
        text("SELECT current_balance FROM user_settings WHERE user_id = :uid"),
        {"uid": user_id},
    ).mappings().first()
    if row and row["current_balance"] is not None:
        return float(row["current_balance"]), "user_set"

    from app.demo_clock import get_demo_now

    demo_now = get_demo_now(db)
    window_start = demo_now - timedelta(days=30)
    income_30d = 0.0
    if not income_df.empty:
        settled = income_df[income_df["status"] == "정산완료"]
        income_30d = float(settled[settled["settled_at"] >= window_start]["amount"].sum())
    spend_30d = float(tx_df[tx_df["timestamp"] >= window_start]["amount"].sum()) if not tx_df.empty else 0.0
    return max(0.0, income_30d - spend_30d), "estimated"


def _project_future_income(income_df: pd.DataFrame, demo_now: datetime, horizon_end: datetime) -> list[dict]:
    """소스(플랫폼)별로 과거 '정산완료' 이벤트의 간격·금액 패턴을 보고, 이미 알고 있는
    '정산예정' 건 이후 구간까지 미래 수입을 이어서 추정합니다.

    예: 배달의민족이 지난 넉 달간 7일 간격으로 평균 27만원씩 들어왔다면, 앞으로도
    7일마다 27만원이 들어올 거라고 가정합니다. 실제 오픈뱅킹이라면 예정된 정산
    스케줄을 그대로 가져오겠지만, 이 데모는 과거 패턴으로 근사합니다."""
    projected: list[dict] = []
    if income_df.empty:
        return projected

    settled = income_df[income_df["status"] == "정산완료"].copy()
    for source_id, grp in settled.groupby("source_id"):
        grp = grp.sort_values("settled_at")
        platform_name = grp["platform_name"].iloc[0]
        if len(grp) >= 2:
            intervals = grp["settled_at"].diff().dt.total_seconds().dropna() / 86400
            avg_interval_days = max(1, round(float(intervals.median())))
        else:
            avg_interval_days = 30  # 이력이 1건뿐이면 월 1회로 가정(보수적 기본값)
        avg_amount = float(grp["amount"].tail(4).mean())

        # 이 소스의 '가장 마지막으로 알고 있는' 이벤트(정산완료든 정산예정이든) 이후부터 투사
        all_known = income_df[income_df["source_id"] == source_id]
        last_known_date = all_known["settled_at"].max()

        next_date = last_known_date + timedelta(days=avg_interval_days)
        while next_date <= horizon_end:
            if next_date > demo_now:
                projected.append(
                    {"date": next_date, "amount": avg_amount, "platform_name": platform_name, "kind": "예상 수입(패턴 추정)"}
                )
            next_date += timedelta(days=avg_interval_days)
    return projected


def _project_recurring_expenses(tx_df: pd.DataFrame, demo_now: datetime, horizon_end: datetime) -> list[dict]:
    """data_pipeline/recurring_detector.py(이미 budgets/forecast가 쓰는 모듈)로 찾은
    정기결제를, 감지된 간격으로 시뮬레이션 기간 끝까지 이어서 투사합니다."""
    projected: list[dict] = []
    if tx_df.empty:
        return projected
    tx_for_detect = tx_df.rename(columns={})
    tx_for_detect["user_id"] = "u"  # detect_recurring_payments는 user_id 컬럼을 기대함(단일 유저라 더미값)
    recurring_df = detect_recurring_payments(tx_for_detect)
    if recurring_df.empty:
        return projected

    for _, r in recurring_df.iterrows():
        interval = max(7, int(round(r["avg_interval_days"])))
        next_date = pd.to_datetime(r["next_expected_date"])
        while next_date <= horizon_end:
            if next_date > demo_now:
                projected.append(
                    {"date": next_date, "amount": float(r["avg_amount"]), "merchant_name": r["merchant_name"], "kind": "정기결제(고정비)"}
                )
            next_date += timedelta(days=interval)
    return projected


def _income_variability(income_df: pd.DataFrame, demo_now: datetime) -> float:
    """최근 3개월 '월별 수입 합계'의 변동계수(표준편차/평균). 값이 클수록 수입이
    들쭉날쭉하다는 뜻이라, 세금/건보료 준비금을 더 넉넉히 잡도록 반영합니다."""
    settled = income_df[income_df["status"] == "정산완료"].copy()
    if settled.empty:
        return 0.0
    window_start = demo_now - timedelta(days=INCOME_LOOKBACK_DAYS)
    settled = settled[settled["settled_at"] >= window_start]
    if settled.empty:
        return 0.0
    settled["month"] = settled["settled_at"].dt.to_period("M")
    monthly = settled.groupby("month")["amount"].sum()
    if len(monthly) < 2 or monthly.mean() == 0:
        return 0.0
    return float(monthly.std(ddof=0) / monthly.mean())


def compute_shield(db: Session, user_id: str) -> dict:
    """Risk Shield + Dynamic Parking 통합 계산. 기획서 7-2 데모 API 계약의 필드명을
    그대로 따릅니다(current_balance/tax_reserve/insurance_reserve/available_cash/
    minimum_balance_date/risk_level/risk_reasons/recommended_reserve)."""
    from app.demo_clock import get_demo_now

    demo_now = get_demo_now(db)
    income_df = _load_income_events(db, user_id)
    tx_df = _load_transactions(db, user_id)

    if income_df.empty and tx_df.empty:
        return None  # 라우터에서 404 처리

    current_balance, balance_source = _get_current_balance(db, user_id, income_df, tx_df)

    # --- Dynamic Parking: 월 평균 수입 기준 간이 준비금 ---
    window_start = demo_now - timedelta(days=INCOME_LOOKBACK_DAYS)
    trailing_income = 0.0
    if not income_df.empty:
        settled = income_df[income_df["status"] == "정산완료"]
        trailing_income = float(settled[settled["settled_at"] >= window_start]["amount"].sum())
    monthly_avg_income = trailing_income / 3 if trailing_income else 0.0

    cv = _income_variability(income_df, demo_now)
    variability_multiplier = 1 + min(cv, 1.0) * 0.5  # cv 0→1.0배, cv>=1→최대 1.5배

    # 9/5 변경 — 예전에는 월평균 수입에 고정 비율(8%/7%)을 곱했습니다. 그 숫자의 근거가
    # "프리랜서에게 흔히 권장되는 관행"뿐이라, 실제 제도 계산식(app/tax.py)으로 바꿉니다:
    # 종합소득세는 단순경비율·누진세율·기납부 3.3%까지 반영한 뒤 12로 나눈 월 적립액,
    # 건강보험료는 요율 7.19% + 장기요양보험료를 적용한 월 보험료입니다.
    # 수입 데이터가 없는 유저(연결된 플랫폼이 없음)는 기존 방식으로 조용히 되돌립니다.
    try:
        from app.routers.tax import load_annual_income
        from app.tax import estimate_health_insurance, estimate_income_tax, expense_rate_for

        annual_business, annual_employment, platform_types = load_annual_income(db, user_id)
        if annual_business > 0 or annual_employment > 0:
            _tax = estimate_income_tax(annual_business, expense_rate_for(platform_types))
            _health = estimate_health_insurance(annual_business, has_employment_income=annual_employment > 0)
            tax_reserve = _tax["monthly_reserve"]
            insurance_reserve = _health["total_monthly"]
        else:
            tax_reserve = round(monthly_avg_income * TAX_RESERVE_RATE)
            insurance_reserve = round(monthly_avg_income * INSURANCE_RESERVE_RATE)
    except Exception:
        logger.exception("[cashflow] 세금·건보료 추정에 실패해 간이 비율로 되돌립니다.")
        tax_reserve = round(monthly_avg_income * TAX_RESERVE_RATE)
        insurance_reserve = round(monthly_avg_income * INSURANCE_RESERVE_RATE)
    available_cash = current_balance - tax_reserve - insurance_reserve

    # 다음 정산 주기(약 30일) 동안 추가로 더 모아둬야 할 권장액 — 변동성 클수록 더 넉넉히
    recommended_reserve = round((tax_reserve + insurance_reserve) * variability_multiplier)

    # --- Risk Shield: 일별 시뮬레이션 ---
    min_safety_balance = _get_min_safety_balance(db, user_id)
    horizon_end = demo_now + timedelta(days=SIMULATION_HORIZON_DAYS)

    future_income = _project_future_income(income_df, demo_now, horizon_end)
    future_expenses = _project_recurring_expenses(tx_df, demo_now, horizon_end)

    # 정기결제로 안 잡히는 나머지(변동비)는 최근 지출 속도로 매일 조금씩 빠져나간다고 가정
    recent_window_days = 60
    recent_tx = tx_df[tx_df["timestamp"] >= demo_now - timedelta(days=recent_window_days)] if not tx_df.empty else tx_df
    recent_total = float(recent_tx["amount"].sum()) if not recent_tx.empty else 0.0
    recurring_total_recent = 0.0
    if future_expenses:
        recurring_merchants = {e["merchant_name"] for e in future_expenses}
        recurring_total_recent = float(
            recent_tx[recent_tx["merchant_name"].isin(recurring_merchants)]["amount"].sum()
        ) if not recent_tx.empty else 0.0
    variable_daily_rate = max(0.0, (recent_total - recurring_total_recent)) / recent_window_days

    daily_points: list[DailyBalancePoint] = []
    balance = current_balance
    minimum_balance_date: Optional[date] = None
    running_min = balance

    for offset in range(1, SIMULATION_HORIZON_DAYS + 1):
        day = (demo_now + timedelta(days=offset)).date()
        day_start = datetime.combine(day, datetime.min.time())
        day_end = day_start + timedelta(days=1)
        events_today = []

        for inc in future_income:
            if day_start <= inc["date"] < day_end:
                balance += inc["amount"]
                events_today.append(f"{inc['platform_name']} 수입 +{inc['amount']:,.0f}원")

        for exp in future_expenses:
            if day_start <= exp["date"] < day_end:
                balance -= exp["amount"]
                events_today.append(f"{exp['merchant_name']} 정기결제 -{exp['amount']:,.0f}원")

        balance -= variable_daily_rate

        daily_points.append(DailyBalancePoint(day=day, balance=round(balance), events=events_today))
        if balance < running_min:
            running_min = balance
        if balance < min_safety_balance and minimum_balance_date is None:
            minimum_balance_date = day

    # --- 위험도 판정 ---
    risk_reasons: list[str] = []
    if cv >= 0.4:
        risk_reasons.append(f"최근 수입 변동성 증가 (변동계수 {cv:.2f})")
    if not tx_df.empty:
        fixed_ratio = (recurring_total_recent / recent_total) if recent_total else 0.0
        if fixed_ratio >= 0.45:
            risk_reasons.append(f"고정비 비율 {fixed_ratio*100:.0f}%")
    if available_cash < recommended_reserve:
        risk_reasons.append("세금·건보료 준비금 부족")
    if minimum_balance_date is not None:
        days_until = (minimum_balance_date - demo_now.date()).days
        risk_reasons.append(f"{days_until}일 후 최소 안전잔액 이하로 예상")

    if minimum_balance_date is not None and running_min < 0:
        risk_level = "위험"
    elif minimum_balance_date is not None or available_cash < recommended_reserve:
        risk_level = "주의"
    else:
        risk_level = "안전"
        if not risk_reasons:
            risk_reasons.append("최근 현금흐름이 안정적이에요")

    return {
        "user_id": user_id,
        "current_balance": round(current_balance),
        "balance_source": balance_source,
        "tax_reserve": tax_reserve,
        "insurance_reserve": insurance_reserve,
        "available_cash": round(available_cash),
        "minimum_balance_date": minimum_balance_date.isoformat() if minimum_balance_date else None,
        "risk_level": risk_level,
        "risk_reasons": risk_reasons,
        "recommended_reserve": recommended_reserve,
        "min_safety_balance": min_safety_balance,
        "income_variability": round(cv, 3),
        "daily_balances": [
            {"date": p.day.isoformat(), "balance": p.balance, "events": p.events} for p in daily_points
        ],
        "calculated_at": demo_now.isoformat(),
    }


def compute_scenarios(db: Session, user_id: str) -> Optional[dict]:
    """기본/입금지연/수입감소 3개 시나리오의 일별 잔액 시계열. compute_shield()와
    같은 시뮬레이션 로직을 재사용하되, 입력을 살짝 비틀어서 3번 돌립니다."""
    from app.demo_clock import get_demo_now

    demo_now = get_demo_now(db)
    income_df = _load_income_events(db, user_id)
    tx_df = _load_transactions(db, user_id)
    if income_df.empty and tx_df.empty:
        return None

    current_balance, _ = _get_current_balance(db, user_id, income_df, tx_df)
    horizon_end = demo_now + timedelta(days=SIMULATION_HORIZON_DAYS)

    def _simulate(income_delay_days: int, income_scale: float) -> list[dict]:
        future_income = _project_future_income(income_df, demo_now, horizon_end)
        for inc in future_income:
            inc["date"] = inc["date"] + timedelta(days=income_delay_days)
            inc["amount"] = inc["amount"] * income_scale
        future_expenses = _project_recurring_expenses(tx_df, demo_now, horizon_end)

        recent_window_days = 60
        recent_tx = tx_df[tx_df["timestamp"] >= demo_now - timedelta(days=recent_window_days)] if not tx_df.empty else tx_df
        recent_total = float(recent_tx["amount"].sum()) if not recent_tx.empty else 0.0
        recurring_merchants = {e["merchant_name"] for e in future_expenses}
        recurring_total_recent = float(
            recent_tx[recent_tx["merchant_name"].isin(recurring_merchants)]["amount"].sum()
        ) if not recent_tx.empty and recurring_merchants else 0.0
        variable_daily_rate = max(0.0, (recent_total - recurring_total_recent)) / recent_window_days

        balance = current_balance
        points = []
        for offset in range(1, SIMULATION_HORIZON_DAYS + 1):
            day = (demo_now + timedelta(days=offset)).date()
            day_start = datetime.combine(day, datetime.min.time())
            day_end = day_start + timedelta(days=1)
            for inc in future_income:
                if day_start <= inc["date"] < day_end:
                    balance += inc["amount"]
            for exp in future_expenses:
                if day_start <= exp["date"] < day_end:
                    balance -= exp["amount"]
            balance -= variable_daily_rate
            points.append({"date": day.isoformat(), "balance": round(balance)})
        return points

    return {
        "user_id": user_id,
        "calculated_at": demo_now.isoformat(),
        "scenarios": {
            "기본": _simulate(income_delay_days=0, income_scale=1.0),
            "입금지연": _simulate(income_delay_days=10, income_scale=1.0),
            "수입감소": _simulate(income_delay_days=0, income_scale=0.7),
        },
    }

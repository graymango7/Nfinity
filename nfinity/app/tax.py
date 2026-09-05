"""
종합소득세 · 건강보험료 추정 엔진 (9/5 신규)

왜 만들었나
-----------
이전까지 app/cashflow.py는 세금·건보료 준비금을 "월평균 수입 × 8%", "× 7%"라는
고정 비율로 잡았습니다. 코드 주석에도 "실제 세법·건강보험료 산정식이 아니라 프리랜서에게
흔히 권장되는 관행을 단순화한 예시 비율"이라고 적혀 있었습니다. 그런데 이 서비스가 내세우는
가치가 "N잡러가 1~2년 뒤에 맞는 세금·건보료를 지금 미리 알려준다"인 이상, 그 숫자의 근거가
관행이면 곤란합니다. 이 파일은 실제 제도의 계산 구조를 그대로 옮깁니다.

무엇을 계산하나
---------------
1) 종합소득세: 플랫폼 정산 수입(사업소득)을 연 환산 → 단순경비율로 필요경비 차감 →
   기본공제 → 과세표준 → 누진세율표 적용 → 지방소득세 10% 가산 → 이미 원천징수된
   3.3%를 기납부세액으로 차감. 결과가 양수면 "5월에 더 낼 돈", 음수면 "돌려받을 돈".
2) 건강보험료: 연소득 기준 보험료율을 적용하고 장기요양보험료를 얹습니다.
   직장가입자는 급여 외 소득이 연 2,000만원을 넘는 부분에 대해서만 소득월액보험료를
   본인이 100% 부담합니다. 피부양자는 연소득 2,000만원을 넘으면 자격을 잃고 지역가입자가
   되므로, 그 경계에 가까우면 경고를 함께 돌려줍니다.

한계 (화면에도 반드시 같이 표기)
--------------------------------
- 확정 세액·보험료가 아니라 "지금 흐름이 이어질 경우"의 추정치입니다. 인적공제(부양가족),
  연금·보험료 공제, 세액공제, 다른 소득(근로/이자/배당), 기납부 지방소득세 등은 반영하지
  않습니다.
- 지역가입자 건강보험료는 실제로는 소득 외에 재산·자동차 점수까지 합산해 산정합니다.
  이 앱은 재산 정보를 갖고 있지 않으므로 소득 부분만 계산합니다.
- 단순경비율은 업종·수입 규모에 따라 다르고 매년 고시됩니다. 여기서는 인적용역 계열의
  대표값을 쓰되 업종별로 다르게 잡을 수 있게 열어뒀습니다.
"""
from __future__ import annotations

# ------------------------------------------------------------------
# 제도 상수 (2026년 기준)
# ------------------------------------------------------------------

# 종합소득세 누진세율표 — (과세표준 상한, 세율, 누진공제액)
# 상한 None은 그 위 전부. 산출세액 = 과세표준 × 세율 − 누진공제액.
INCOME_TAX_BRACKETS = [
    (14_000_000, 0.06, 0),
    (50_000_000, 0.15, 1_260_000),
    (88_000_000, 0.24, 5_760_000),
    (150_000_000, 0.35, 15_440_000),
    (300_000_000, 0.38, 19_940_000),
    (500_000_000, 0.40, 25_940_000),
    (1_000_000_000, 0.42, 35_940_000),
    (None, 0.45, 65_940_000),
]

LOCAL_INCOME_TAX_RATE = 0.10   # 지방소득세: 소득세액의 10%
WITHHOLDING_RATE = 0.033       # 프리랜서 사업소득 원천징수 3.3%(지방소득세 0.3% 포함)
BASIC_DEDUCTION = 1_500_000    # 기본공제(본인) 150만원

# 단순경비율(업종별 고시값 중 인적용역 계열 대표값). 장부를 쓰지 않는 소규모 사업자가
# 증빙 없이 인정받는 필요경비 비율입니다.
SIMPLE_EXPENSE_RATES = {
    "배달": 0.794,       # 배달·퀵서비스 등 인적용역
    "프리랜서": 0.642,   # 기타 인적용역(강사·디자이너·마케터 등)
    "콘텐츠": 0.642,     # 1인 미디어 콘텐츠 창작자
    "커머스": 0.560,
    "본업": 0.0,         # 근로소득은 경비율 개념이 없음(별도 처리)
}
DEFAULT_EXPENSE_RATE = 0.642

# 건강보험
HEALTH_INSURANCE_RATE = 0.0719        # 2026년 건강보험료율 7.19%
LONG_TERM_CARE_RATE = 0.1295          # 장기요양보험료 = 건강보험료 × 12.95%
EMPLOYEE_SHARE = 0.5                  # 직장가입자는 회사와 절반씩 부담
DEPENDENT_INCOME_LIMIT = 20_000_000   # 피부양자 자격 / 소득월액보험료 기준: 연 2,000만원


def _progressive_tax(tax_base: float) -> float:
    """과세표준에 누진세율표를 적용해 산출세액을 계산합니다."""
    if tax_base <= 0:
        return 0.0
    for upper, rate, deduction in INCOME_TAX_BRACKETS:
        if upper is None or tax_base <= upper:
            return max(0.0, tax_base * rate - deduction)
    return 0.0


def expense_rate_for(platform_types: list[str] | None) -> float:
    """연결된 플랫폼 유형들을 보고 적용할 단순경비율을 정합니다.

    여러 유형이 섞인 N잡러가 대부분이라, 해당하는 유형들의 평균을 씁니다
    (본업=근로소득은 사업소득 경비율 계산에서 제외).
    """
    rates = [
        SIMPLE_EXPENSE_RATES[t]
        for t in (platform_types or [])
        if t in SIMPLE_EXPENSE_RATES and t != "본업"
    ]
    return sum(rates) / len(rates) if rates else DEFAULT_EXPENSE_RATE


def estimate_income_tax(annual_business_income: float, expense_rate: float = DEFAULT_EXPENSE_RATE) -> dict:
    """사업소득(플랫폼 정산 수입) 연 환산액으로 종합소득세를 추정합니다.

    반환값의 `balance_due`가 양수면 5월에 추가로 낼 돈, 음수면 돌려받을 돈입니다.
    """
    income = max(0.0, float(annual_business_income))
    necessary_expense = income * expense_rate
    income_amount = income - necessary_expense              # 소득금액
    tax_base = max(0.0, income_amount - BASIC_DEDUCTION)    # 과세표준

    calculated = _progressive_tax(tax_base)                  # 산출세액(소득세)
    local_tax = calculated * LOCAL_INCOME_TAX_RATE           # 지방소득세
    total_tax = calculated + local_tax
    prepaid = income * WITHHOLDING_RATE                      # 3.3% 원천징수분
    balance = total_tax - prepaid

    # 적용된 한계세율(어느 구간에 걸쳐 있는지) — 화면에서 설명용으로 씁니다.
    marginal = 0.0
    for upper, rate, _ in INCOME_TAX_BRACKETS:
        if upper is None or tax_base <= upper:
            marginal = rate
            break

    return {
        "annual_income": round(income),
        "expense_rate": round(expense_rate, 3),
        "necessary_expense": round(necessary_expense),
        "income_amount": round(income_amount),
        "tax_base": round(tax_base),
        "marginal_rate": marginal,
        "calculated_tax": round(calculated),
        "local_income_tax": round(local_tax),
        "total_tax": round(total_tax),
        "prepaid_withholding": round(prepaid),
        "balance_due": round(balance),
        "monthly_reserve": round(max(0.0, balance) / 12),
    }


def estimate_health_insurance(annual_income: float, has_employment_income: bool = False) -> dict:
    """건강보험료(+장기요양보험료)를 추정합니다.

    - 직장가입자(has_employment_income=True): 급여 외 소득이 연 2,000만원을 넘는 부분에만
      '소득월액보험료'가 붙고, 이건 회사 지원 없이 본인이 100% 부담합니다.
    - 지역가입자: 소득 전체에 보험료율을 적용합니다(재산·자동차 점수는 이 앱에 정보가 없어
      계산에 넣지 않습니다 — 실제 고지액은 이보다 높을 수 있습니다).
    """
    income = max(0.0, float(annual_income))

    if has_employment_income:
        excess = max(0.0, income - DEPENDENT_INCOME_LIMIT)
        base_annual = excess
        note = (
            "직장가입자는 급여 외 소득이 연 2,000만원을 넘는 부분에만 소득월액보험료가 "
            "붙고, 회사 지원 없이 본인이 전액 부담합니다."
        )
        subscriber_type = "직장가입자(소득월액보험료)"
    else:
        base_annual = income
        note = (
            "지역가입자는 소득에 보험료율을 그대로 적용합니다. 실제 고지액은 재산·자동차 "
            "점수가 더해져 이보다 높을 수 있습니다."
        )
        subscriber_type = "지역가입자"

    health_monthly = base_annual * HEALTH_INSURANCE_RATE / 12
    care_monthly = health_monthly * LONG_TERM_CARE_RATE
    total_monthly = health_monthly + care_monthly

    # 피부양자 경계 경고: 연 2,000만원을 넘으면 자격을 잃고 보험료를 직접 내야 합니다.
    dependent_lost = income > DEPENDENT_INCOME_LIMIT
    ratio = income / DEPENDENT_INCOME_LIMIT if DEPENDENT_INCOME_LIMIT else 0
    if dependent_lost:
        warning = (
            "연 소득이 2,000만원을 넘어 피부양자 자격 기준을 초과합니다. "
            "피부양자였다면 자격을 잃고 보험료를 직접 부담하게 됩니다."
        )
    elif ratio >= 0.85:
        warning = (
            "연 소득이 피부양자 기준(2,000만원)의 "
            + str(int(ratio * 100))
            + "%입니다. 지금 속도로 벌면 올해 안에 기준을 넘길 수 있습니다."
        )
    else:
        warning = None

    return {
        "subscriber_type": subscriber_type,
        "annual_income": round(income),
        "premium_base": round(base_annual),
        "health_monthly": round(health_monthly),
        "long_term_care_monthly": round(care_monthly),
        "total_monthly": round(total_monthly),
        "annual_total": round(total_monthly * 12),
        "dependent_limit": DEPENDENT_INCOME_LIMIT,
        "dependent_limit_exceeded": dependent_lost,
        "warning": warning,
        "note": note,
    }


DISCLAIMER = (
    "확정 세액·보험료가 아니라 지금 수입 흐름이 이어질 경우의 추정치입니다. "
    "인적공제·세액공제·다른 소득과 재산 점수는 반영하지 않았습니다. "
    "실제 신고·고지 금액과 다를 수 있습니다."
)

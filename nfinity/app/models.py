"""
Pydantic 모델 정의
- DB 테이블(sql/init.sql)과 1:1로 맞춰서 만들었습니다.
- 이 파일의 필드명을 B(소현)/C(하은)와 공유해서 mock_transactions.csv, 카테고리 매핑 결과가
  여기 이름과 정확히 일치하도록 맞춰주세요. (이게 "데이터 계약"입니다)
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class Severity(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class RuleAction(str, Enum):
    ALLOW = "ALLOW"
    FLAG_FOR_REVIEW = "FLAG_FOR_REVIEW"
    REQUIRE_CONFIRMATION = "REQUIRE_CONFIRMATION"
    BLOCK = "BLOCK"


class Transaction(BaseModel):
    """하나의 결제/거래 건. transactions 테이블과 대응됩니다."""

    transaction_id: Optional[str] = None
    user_id: str
    amount: float
    merchant_id: Optional[str] = None
    merchant_name: Optional[str] = None
    mcc_code: Optional[str] = None
    category: Optional[str] = None
    timestamp: datetime
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    country: str = "KR"
    device_id: Optional[str] = None


class UserRiskProfile(BaseModel):
    """유저별 리스크 판단 기준선. user_risk_profiles 테이블과 대응됩니다."""

    user_id: str
    avg_transaction_amount: float = 0
    std_transaction_amount: float = 0
    single_transaction_limit: float = 500_000
    daily_limit: float = 1_000_000
    avg_daily_transactions: float = 0
    allowed_countries: list[str] = Field(default_factory=lambda: ["KR"])


class RuleResult(BaseModel):
    """룰 엔진이 규칙 하나에 위반되었을 때 반환하는 결과."""

    rule_id: str
    severity: Severity
    description: str
    action: RuleAction


class RiskAssessment(BaseModel):
    """리스크 평가 최종 결과. risk_events 테이블과 대응됩니다."""

    user_id: str
    transaction_id: Optional[str] = None
    score: int
    severity: Severity
    action: RuleAction
    triggered_rules: list[RuleResult] = Field(default_factory=list)


# ============================================================
# 예산통제 (budgets) — 8/26 백엔드 구축 데이에 새로 추가
# ============================================================


class BudgetCreate(BaseModel):
    """예산 설정/변경 요청. POST /api/v1/budgets 의 입력."""

    user_id: str
    category: str
    period: str = "monthly"  # weekly / monthly / quarterly
    limit_amount: float


class BudgetUsage(BaseModel):
    """카테고리 하나의 현재 사용 현황. GET /api/v1/budgets/usage 의 출력."""

    user_id: str
    category: str
    period: str
    spent_amount: float
    limit_amount: float
    usage_rate: float  # 0.0 ~ 1.0+ (1.0 넘으면 예산 초과)
    # 8/30 추가 (소현님 recurring_detector.py의 check_crossed_thresholds() 사용).
    # 이미 넘은 임계값들(%). 예: [50, 80] = 50%/80%는 넘었고 90% 이상은 아직 안 넘음.
    crossed_thresholds: list[int] = Field(default_factory=list)


class RecurringPayment(BaseModel):
    """감지된 정기결제 한 건. BudgetForecast.recurring_payments 안에 들어갑니다."""

    merchant_name: str
    expected_amount: int
    expected_date: str  # YYYY-MM-DD


class BudgetForecast(BaseModel):
    """월말 예상 지출. GET /api/v1/budgets/forecast 의 출력.

    8/30: 소현님이 만든 정기결제 감지 로직(data_pipeline/recurring_detector.py)을
    반영해서, "지금까지 쓴 돈/지난 날짜 수 * 이번달 전체 날짜 수"(base_predicted_total)에
    이번 달 안에 아직 안 나온 정기결제 예상액(recurring_addition)을 더합니다.
    """

    user_id: str
    category: str
    current_spent: float
    predicted_total: float  # 정기결제까지 반영한 최종 예측치
    base_predicted_total: float  # 참고용: 정기결제 반영 전(기존 단순 선형 예측) 값
    predicted_overage: float  # 0이면 초과 예상 없음 (predicted_total 기준)
    confidence: float  # 0.0 ~ 1.0, 월초일수록 낮음
    recurring_payments: list[RecurringPayment] = Field(default_factory=list)


# ============================================================
# Gig-Score — 8/26 백엔드 구축 데이에 새로 추가 (스텁)
# ============================================================


class GigScoreResponse(BaseModel):
    """GET /api/v1/gig-score/{user_id} 의 출력.

    지금은 임시 산식입니다. 세금 파킹 성실도·업무경비 비율 등
    실제 지표로 8/30-31에 교체될 예정입니다.
    """

    user_id: str
    score: int  # 0 ~ 1000
    components: dict[str, float]
    message: str = ""


# ============================================================
# AI 절세장부(경비분류) — 8/28 계약 확정판
# (조소현님이 실제 프롬프트/분류 로직을 완성해서 전달, data_pipeline/expense_classifier.py로 연동)
# ★ 필드명은 프론트(하은)와 이미 합의된 것이라 임의로 바꾸면 안 됩니다.
# ============================================================


class ExpenseQueueItem(BaseModel):
    """Tinder형 스와이프 카드 한 장에 해당하는 거래 한 건."""

    id: int
    merchant: str
    amount: int
    ai_tag: str  # 예: "업무미팅", "SW구독", "개인지출", "미분류"
    prob: int  # 0 ~ 100 (업무 경비로 인정될 확률, %)


class ExpenseClassifyResponse(BaseModel):
    """GET /api/v1/expense/classify 의 출력. 화면(스와이프 큐 + 환급액 카운터)에 그대로 매핑됩니다."""

    transactions_queue: list[ExpenseQueueItem]
    current_estimated_refund: int  # prob >= APPROVAL_THRESHOLD(50)인 건들의 예상 환급액 합계(원)


# ============================================================
# 수입 플랫폼 연결 (income) — 9/2 추가
# 실제 계좌/마이데이터 연동 대신, "연결하기"를 누르면 이미 있던 데모 데이터가
# 드러나는 방식으로 그 흐름을 재연합니다. app/demo_income.py 참고.
# ============================================================


class IncomeSource(BaseModel):
    """플랫폼/계좌 하나. income_sources 테이블과 대응됩니다."""

    source_id: str
    platform_name: str
    platform_type: str  # 배달 / 프리랜서 / 콘텐츠 / 커머스 / 본업
    icon_emoji: str
    connected: bool
    connected_at: Optional[datetime] = None
    # connected=false인 동안은 요약에서 제외되지만, 몇 건이 기다리고 있는지는 미리 보여줍니다
    # ("연결하면 3건이 더 보여요" 같은 동기부여용 — 금액 자체는 연결 전엔 숨깁니다).
    pending_event_count: int = 0


class IncomeConnectResponse(BaseModel):
    """POST /api/v1/income/sources/{source_id}/connect 의 출력."""

    source_id: str
    platform_name: str
    connected: bool
    newly_connected: bool  # 이번 호출로 처음 연결됐으면 true, 이미 연결돼있던 거면 false
    revealed_event_count: int  # 이번에 새로 드러난(=이미 있던) 수입 이벤트 건수


class IncomeEvent(BaseModel):
    """수입 이벤트 하나. income_events 테이블과 대응됩니다."""

    platform_name: str
    icon_emoji: str
    amount: float
    memo: Optional[str] = None
    status: str  # 정산완료 / 정산예정
    settled_at: datetime


class IncomeByPlatform(BaseModel):
    """플랫폼별 이번 기간 수입 합계 (연결된 플랫폼만)."""

    platform_name: str
    platform_type: str
    icon_emoji: str
    total_amount: float
    event_count: int


class IncomeSummary(BaseModel):
    """GET /api/v1/income/summary 의 출력 — '한 장 요약' 홈 카드가 그대로 쓰는 모양."""

    user_id: str
    period_label: str  # 예: "이번 주" / "이번 달"
    total_settled: float  # 연결된 플랫폼의 정산완료 합계
    total_upcoming: float  # 연결된 플랫폼의 정산예정 합계
    by_platform: list[IncomeByPlatform] = Field(default_factory=list)
    unconnected_source_count: int  # 아직 연결 안 한 플랫폼 수 (있으면 연결 유도 배너용)


class IncomeDisconnectResponse(BaseModel):
    """POST /api/v1/income/sources/{source_id}/disconnect 의 출력.
    기획서 8번 "사용자가 연결 해제할 수 있는 화면" 요구사항 반영 (9/2 세 번째 업데이트)."""

    source_id: str
    platform_name: str
    connected: bool  # 항상 false


# ============================================================
# Risk Shield / Dynamic Parking (9/2 세 번째 업데이트)
# app/cashflow.py의 계산 결과를 그대로 담는 응답 모델입니다.
# 필드명은 기획서 7-2 데모 API 계약을 그대로 따릅니다.
# ============================================================


class DailyBalancePoint(BaseModel):
    date: str
    balance: float
    events: list[str] = Field(default_factory=list)


class ShieldResponse(BaseModel):
    user_id: str
    current_balance: float
    # "user_set" = 사용자가 알려준 잔액에서 출발한 시뮬레이션,
    # "estimated" = 최근 30일 수입·지출로 앱이 추정한 값 (app/cashflow.py 참고)
    balance_source: str = "estimated"
    tax_reserve: float
    insurance_reserve: float
    available_cash: float
    minimum_balance_date: Optional[str] = None
    risk_level: str  # 안전 / 주의 / 위험
    risk_reasons: list[str] = Field(default_factory=list)
    recommended_reserve: float
    min_safety_balance: float
    income_variability: float
    daily_balances: list[DailyBalancePoint] = Field(default_factory=list)
    calculated_at: str
    disclaimer: str = (
        "이 잔액과 예측은 연결된 수입·지출 데이터를 바탕으로 한 참고용 추정치이며, "
        "실제 계좌 잔액이나 확정 세액·보험료가 아닙니다. 세무 신고, 보험료 고지, "
        "신용·대출 심사에 사용되지 않습니다."
    )


class ScenarioSeries(BaseModel):
    date: str
    balance: float


class ScenarioResponse(BaseModel):
    user_id: str
    calculated_at: str
    scenarios: dict[str, list[ScenarioSeries]]


class SafetyBalanceUpdate(BaseModel):
    min_safety_balance: float = Field(ge=0)


class SafetyBalanceResponse(BaseModel):
    user_id: str
    min_safety_balance: float
    is_default: bool  # 사용자가 직접 설정했으면 false, 기본값을 쓰고 있으면 true

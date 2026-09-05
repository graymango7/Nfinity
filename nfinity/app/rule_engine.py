"""
리스크방어 룰 엔진 (R001 ~ R007)

설계서(리스크방어 및 예산통제 시스템 아키텍처 설계서.pdf, 3.4.1절)의 의사코드를
실제로 동작하는 파이썬 코드로 옮긴 버전입니다.

포인트: 이 모듈은 DB나 B/C의 데이터가 없어도 지금 바로 테스트할 수 있습니다.
        tests/test_rule_engine.py 에서 손으로 만든 샘플 거래로 검증합니다.
"""
from __future__ import annotations

from datetime import timedelta
from math import radians, sin, cos, sqrt, atan2
from typing import Optional

from app.models import RuleAction, RuleResult, Severity, Transaction, UserRiskProfile


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """두 좌표 사이의 거리(km). 이동 속도 계산(R004)에 씁니다."""
    R = 6371.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))


class RuleEngine:
    """
    사용법:
        engine = RuleEngine(history=과거_거래_리스트)
        results = engine.evaluate(new_transaction, user_profile)

    history는 해당 유저의 "이 거래 이전" 거래 목록입니다.
    실제 서비스에서는 DB에서 조회해오지만, 테스트에서는 그냥 리스트를 넘기면 됩니다.
    """

    def __init__(self, history: Optional[list[Transaction]] = None):
        self.history = sorted(history or [], key=lambda t: t.timestamp)

    # ---- 내부 헬퍼: 과거 거래 조회 ----

    def _daily_total(self, user_id: str, date) -> float:
        return sum(
            t.amount
            for t in self.history
            if t.user_id == user_id and t.timestamp.date() == date
        )

    def _is_known_merchant(self, user_id: str, merchant_id: Optional[str]) -> bool:
        if not merchant_id:
            return False
        return any(
            t.user_id == user_id and t.merchant_id == merchant_id for t in self.history
        )

    def _last_transaction(self, user_id: str) -> Optional[Transaction]:
        user_txns = [t for t in self.history if t.user_id == user_id]
        return user_txns[-1] if user_txns else None

    def _recent_merchant_count(self, user_id: str, merchant_id: Optional[str], since) -> int:
        if not merchant_id:
            return 0
        return sum(
            1
            for t in self.history
            if t.user_id == user_id
            and t.merchant_id == merchant_id
            and t.timestamp >= since
        )

    # ---- 메인 진입점 ----

    def evaluate(self, txn: Transaction, profile: UserRiskProfile) -> list[RuleResult]:
        results: list[RuleResult] = []

        # R001: 단일 거래 한도 초과
        if txn.amount > profile.single_transaction_limit:
            results.append(
                RuleResult(
                    rule_id="R001",
                    severity=Severity.HIGH,
                    description=(
                        f"단일 거래 한도 초과: {txn.amount:,.0f}원 > "
                        f"{profile.single_transaction_limit:,.0f}원"
                    ),
                    action=RuleAction.REQUIRE_CONFIRMATION,
                )
            )

        # R002: 일일 누적 거래 한도 초과
        daily_total = self._daily_total(txn.user_id, txn.timestamp.date())
        if daily_total + txn.amount > profile.daily_limit:
            results.append(
                RuleResult(
                    rule_id="R002",
                    severity=Severity.CRITICAL,
                    description=(
                        f"일일 한도 초과: 누적 {daily_total:,.0f}원 + "
                        f"{txn.amount:,.0f}원 > {profile.daily_limit:,.0f}원"
                    ),
                    action=RuleAction.BLOCK,
                )
            )

        # R003: 신규 가맹점 + 평균 대비 대액
        if not self._is_known_merchant(txn.user_id, txn.merchant_id):
            if profile.avg_transaction_amount > 0 and txn.amount > profile.avg_transaction_amount * 3:
                results.append(
                    RuleResult(
                        rule_id="R003",
                        severity=Severity.MEDIUM,
                        description="신규 가맹점 + 평균 거래액 3배 초과",
                        action=RuleAction.FLAG_FOR_REVIEW,
                    )
                )

        # R004: 불가능한 이동 속도 (직전 거래와 비교)
        last_txn = self._last_transaction(txn.user_id)
        if last_txn and last_txn.latitude is not None and txn.latitude is not None:
            distance = haversine_km(last_txn.latitude, last_txn.longitude, txn.latitude, txn.longitude)
            time_diff_hours = (txn.timestamp - last_txn.timestamp).total_seconds() / 3600
            if time_diff_hours > 0:
                speed = distance / time_diff_hours
                if speed > 900:  # 항공편보다 빠른 이동 = 물리적으로 불가능
                    results.append(
                        RuleResult(
                            rule_id="R004",
                            severity=Severity.CRITICAL,
                            description=f"불가능한 이동 속도: {speed:.0f} km/h",
                            action=RuleAction.BLOCK,
                        )
                    )

        # R005: 심야 대액 결제 (02:00 ~ 05:00)
        if 2 <= txn.timestamp.hour <= 5:
            if profile.avg_transaction_amount > 0 and txn.amount > profile.avg_transaction_amount * 5:
                results.append(
                    RuleResult(
                        rule_id="R005",
                        severity=Severity.MEDIUM,
                        description="심야 시간대 평균 거래액 5배 초과",
                        action=RuleAction.FLAG_FOR_REVIEW,
                    )
                )

        # R006: 동일 가맹점 단기 반복 결제 (5분 내 3회 이상)
        recent_count = self._recent_merchant_count(
            txn.user_id, txn.merchant_id, since=txn.timestamp - timedelta(minutes=5)
        )
        if recent_count >= 2:  # 과거 2회 + 이번 거래 = 3회
            results.append(
                RuleResult(
                    rule_id="R006",
                    severity=Severity.HIGH,
                    description=f"5분 내 동일 가맹점 {recent_count + 1}회 결제",
                    action=RuleAction.REQUIRE_CONFIRMATION,
                )
            )

        # R007: 허용 국가 외 결제
        if txn.country not in profile.allowed_countries:
            results.append(
                RuleResult(
                    rule_id="R007",
                    severity=Severity.HIGH,
                    description=f"허용되지 않은 국가 결제: {txn.country}",
                    action=RuleAction.REQUIRE_CONFIRMATION,
                )
            )

        return results


# 규칙별 심각도 -> 점수 환산 (0~1). 나중에 Isolation Forest 결과와 앙상블할 때 씁니다.
_SEVERITY_SCORE = {
    Severity.LOW: 0.2,
    Severity.MEDIUM: 0.4,
    Severity.HIGH: 0.7,
    Severity.CRITICAL: 1.0,
}


def rule_results_to_score(results: list[RuleResult]) -> float:
    """여러 규칙이 동시에 걸렸을 때, 가장 심각한 것을 기준으로 0~1 점수를 냅니다."""
    if not results:
        return 0.0
    return max(_SEVERITY_SCORE[r.severity] for r in results)

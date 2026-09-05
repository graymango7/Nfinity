"""
리스크방어 API
- POST /api/v1/risk/assess   : 새 거래 하나를 평가 (룰 엔진 실행 + risk_events 기록)
- GET  /api/v1/risk/events   : 리스크 이벤트 목록 조회
- GET  /api/v1/risk/score/{user_id} : 유저의 최근 리스크 점수 조회
"""
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.anomaly_model import ANOMALY_HIGH_THRESHOLD, ANOMALY_ALERT_THRESHOLD, score_anomaly
from app.database import get_db
from app.models import RiskAssessment, RuleAction, RuleResult, Severity, Transaction, UserRiskProfile
from app.rule_engine import RuleEngine, rule_results_to_score
from app.security import verify_api_key

router = APIRouter(prefix="/api/v1/risk", tags=["risk"], dependencies=[Depends(verify_api_key)])

# 심각도/액션 랭킹표. 여러 규칙이 동시에 걸렸을 때 "가장 심각한 것" 하나를 정확히
# 골라내기 위한 기준입니다.
# (버그 수정: 기존 코드는 `max(results, key=lambda r: r.severity)`로 Severity를
#  알파벳 문자열 순으로 비교했습니다. "MEDIUM" > "HIGH" > "CRITICAL" 순서라서,
#  HIGH와 CRITICAL이 동시에 걸려도 최종 severity가 "MEDIUM"으로 나올 수 있었습니다.
#  action도 `results[0].action`이라 규칙 정의 순서에 우연히 의존했습니다.)
_SEVERITY_RANK = {Severity.LOW: 0, Severity.MEDIUM: 1, Severity.HIGH: 2, Severity.CRITICAL: 3}
_ACTION_RANK = {
    RuleAction.ALLOW: 0,
    RuleAction.FLAG_FOR_REVIEW: 1,
    RuleAction.REQUIRE_CONFIRMATION: 2,
    RuleAction.BLOCK: 3,
}


def _load_profile(db: Session, user_id: str) -> UserRiskProfile:
    row = db.execute(
        text("SELECT * FROM user_risk_profiles WHERE user_id = :uid"), {"uid": user_id}
    ).mappings().first()
    if not row:
        # 프로파일이 아직 없으면 기본값으로 평가 (신규 유저)
        return UserRiskProfile(user_id=user_id)
    return UserRiskProfile(
        user_id=row["user_id"],
        avg_transaction_amount=float(row["avg_transaction_amount"]),
        std_transaction_amount=float(row["std_transaction_amount"]),
        single_transaction_limit=float(row["single_transaction_limit"]),
        daily_limit=float(row["daily_limit"]),
        avg_daily_transactions=float(row["avg_daily_transactions"]),
        allowed_countries=list(row["allowed_countries"] or ["KR"]),
    )


def _load_recent_history(db: Session, user_id: str, before: datetime, days: int = 180) -> list[Transaction]:
    """
    <before> 시점(=평가 대상 거래의 timestamp) 기준으로 과거 <days>일 이력을 가져옵니다.

    🐛 8/28 버그 수정: 원래는 `datetime.utcnow() - timedelta(days=7)`로, "지금 서버 시각"
    기준이었습니다. mock_transactions.csv의 실제 데이터는 2026-05-25 ~ 2026-08-22인데,
    이 서버가 실행되는 "지금"(예: 8/28)과 비교하면 7일 이내로 걸리는 데이터가 사실상 없어서
    R003(신규가맹점)/R004(불가능한 이동속도)/R006(단기반복)이 실제 DB 데이터로는 거의 항상
    빈 이력을 받아 오작동했습니다(항상 "이력 없음"으로 취급). 평가 대상 거래 자신의 시각을
    기준으로 바꾸고, 윈도우도 180일로 넓혔습니다 — Isolation Forest 피처(직전 거래와의 거리,
    가맹점 방문 빈도, 기기 이력)도 이 함수가 반환하는 history를 그대로 씁니다.
    """
    since = before - timedelta(days=days)
    rows = db.execute(
        text(
            "SELECT * FROM transactions WHERE user_id = :uid "
            "AND timestamp >= :since AND timestamp < :before "
            "ORDER BY timestamp"
        ),
        {"uid": user_id, "since": since, "before": before},
    ).mappings().all()
    return [
        Transaction(
            transaction_id=str(r["transaction_id"]),
            user_id=r["user_id"],
            amount=float(r["amount"]),
            merchant_id=r["merchant_id"],
            merchant_name=r["merchant_name"],
            mcc_code=r["mcc_code"],
            category=r["category"],
            timestamp=r["timestamp"],
            latitude=r["latitude"],
            longitude=r["longitude"],
            country=r["country"] or "KR",
            device_id=r["device_id"],
        )
        for r in rows
    ]


@router.post("/assess", response_model=RiskAssessment)
def assess_transaction(txn: Transaction, db: Session = Depends(get_db)):
    """
    새 거래를 평가하고, 규칙(또는 AI 이상탐지)에 걸리면 risk_events에 기록합니다.

    8/28: 룰 엔진(rule_prob) + Isolation Forest(anomaly_prob, app/anomaly_model.py)를
    "확률적 OR"로 앙상블합니다: combined = 1 - (1-rule_prob)*(1-anomaly_prob).
    두 탐지기 중 하나라도 강하게 의심하면 최종 점수가 올라갑니다. 룰이 이미 CRITICAL이면
    (rule_prob=1.0) AI 결과와 무관하게 그대로 100점 — 기존 룰 기반 동작은 전혀 안 바뀝니다.
    반대로 룰이 하나도 안 걸렸는데 AI가 확신하면(ANOMALY_ALERT_THRESHOLD 이상), 룰 엔진이
    원천적으로 설계되지 않은 유형(R00x_위치급변/R00y_신규기기 같은)도 "AI001"이라는 가상의
    rule_id로 risk_events에 남습니다 — 룰 목록에 없다고 조용히 묻히지 않게 하려는 것입니다.
    """
    # 9/5: 본문으로 들어오는 user_id는 미들웨어(app/demo_guard.py)가 검사하지 못하므로
    # 여기서 직접 막습니다 — 임의의 user_id로 남의 이력에 평가 기록을 남길 수 없게.
    from app.demo_guard import allowed_user_ids

    if txn.user_id not in allowed_user_ids():
        raise HTTPException(status_code=403, detail="이 시연 배포는 공개된 데모 계정만 평가할 수 있습니다.")

    profile = _load_profile(db, txn.user_id)
    history = _load_recent_history(db, txn.user_id, before=txn.timestamp)

    engine = RuleEngine(history=history)
    results = engine.evaluate(txn, profile)
    rule_prob = rule_results_to_score(results)

    anomaly_prob = score_anomaly(txn, profile, history)
    if not results and anomaly_prob >= ANOMALY_ALERT_THRESHOLD:
        ai_severity = Severity.HIGH if anomaly_prob >= ANOMALY_HIGH_THRESHOLD else Severity.MEDIUM
        ai_action = (
            RuleAction.REQUIRE_CONFIRMATION if anomaly_prob >= ANOMALY_HIGH_THRESHOLD
            else RuleAction.FLAG_FOR_REVIEW
        )
        results = [
            RuleResult(
                rule_id="AI001",
                severity=ai_severity,
                description=(
                    f"AI 이상탐지 모델이 이 거래를 이상 패턴으로 판단했습니다 "
                    f"(anomaly_prob={anomaly_prob:.2f}). 걸린 규칙은 없지만 과거 패턴과 크게 달라요."
                ),
                action=ai_action,
            )
        ]

    combined_prob = 1 - (1 - rule_prob) * (1 - anomaly_prob)
    score = int(round(combined_prob * 100))

    if results:
        severity = max(results, key=lambda r: _SEVERITY_RANK[r.severity]).severity
        action = max(results, key=lambda r: _ACTION_RANK[r.action]).action
        for r in results:
            db.execute(
                text(
                    "INSERT INTO risk_events "
                    "(transaction_id, user_id, rule_id, severity, description, action, score) "
                    "VALUES (:tid, :uid, :rid, :sev, :desc, :act, :score)"
                ),
                {
                    "tid": txn.transaction_id,
                    "uid": txn.user_id,
                    "rid": r.rule_id,
                    "sev": r.severity.value,
                    "desc": r.description,
                    "act": r.action.value,
                    "score": score,
                },
            )
        db.commit()
    else:
        severity = Severity.LOW
        action = RuleAction.ALLOW

    return RiskAssessment(
        user_id=txn.user_id,
        transaction_id=txn.transaction_id,
        score=score,
        severity=severity,
        action=action,
        triggered_rules=results,
    )


@router.get("/events")
def list_risk_events(
    user_id: str,
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """
    8/30: 프론트(리스크 타임라인 화면)가 가맹점명/금액도 같이 보여줘야 해서, transactions와
    LEFT JOIN해서 merchant_name/amount/timestamp도 같이 내려줍니다 (risk_events 자체엔
    없는 컬럼이라, 이전엔 rule_id/severity만으로는 "무슨 거래였는지" 알 수 없었습니다).

    9/5 보안 수정: 예전에는 user_id가 선택값이라 아예 안 넘기면 미들웨어(app/demo_guard.py)의
    user_id 검사를 우회해서 "전체 유저의 risk_events"가 무인증으로 통째로 조회됐습니다
    (데모 3명이 아닌 시드 유저까지 포함 — 전형적인 IDOR). user_id를 필수로 바꾸고, 여기서도
    직접 데모 페르소나 화이트리스트로 한 번 더 막습니다. limit도 음수/과대값이 들어오면
    Postgres LIMIT에서 500이 나던 걸 Query 제약(1~200)으로 막습니다.
    """
    from app.demo_guard import allowed_user_ids

    if user_id not in allowed_user_ids():
        raise HTTPException(
            status_code=403,
            detail="이 시연 배포는 공개된 데모 계정의 데이터만 조회할 수 있습니다.",
        )

    query = (
        "SELECT re.*, t.merchant_name, t.amount AS transaction_amount, t.timestamp AS transaction_timestamp "
        "FROM risk_events re LEFT JOIN transactions t ON t.transaction_id = re.transaction_id "
        "WHERE re.user_id = :uid ORDER BY re.created_at DESC LIMIT :limit"
    )
    rows = db.execute(text(query), {"uid": user_id, "limit": limit}).mappings().all()
    return [dict(r) for r in rows]


@router.get("/score/{user_id}")
def get_latest_score(user_id: str, db: Session = Depends(get_db)):
    # 참고: risk_events는 규칙 하나당 한 행이라, 같은 거래에서 여러 규칙이
    # 동시에 걸리면 created_at이 동일한 행이 여러 개 생깁니다. created_at만으로
    # 정렬하면 그중 어느 게 뽑힐지 보장이 안 돼서, 심각도 랭킹을 2차 정렬 기준으로
    # 추가했습니다 (동시간대면 가장 심각한 것 우선).
    row = db.execute(
        text(
            # 8/30: action도 같이 내려줍니다 — 프론트가 "본인 확인이 필요해요" 같은
            # 안내 문구를 severity가 아니라 실제 action(ALLOW/FLAG_FOR_REVIEW/...)
            # 기준으로 보여줘야 해서 필요합니다.
            "SELECT score, severity, action, created_at FROM risk_events "
            "WHERE user_id = :uid ORDER BY created_at DESC, "
            "CASE severity "
            "  WHEN 'CRITICAL' THEN 4 WHEN 'HIGH' THEN 3 "
            "  WHEN 'MEDIUM' THEN 2 WHEN 'LOW' THEN 1 ELSE 0 END DESC "
            "LIMIT 1"
        ),
        {"uid": user_id},
    ).mappings().first()
    if not row:
        return {"user_id": user_id, "score": 0, "severity": "LOW", "action": "ALLOW", "message": "이력 없음"}
    return {"user_id": user_id, **dict(row)}

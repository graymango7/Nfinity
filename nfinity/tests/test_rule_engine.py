"""
룰 엔진 단위 테스트. DB/도커 없이 바로 실행 가능합니다.

실행법:
    /tmp/venv/bin/python -m pytest tests/test_rule_engine.py -v
    (또는 프로젝트 루트에서 pip install -r requirements.txt pytest 후 pytest 실행)
"""
from datetime import datetime

from app.models import RuleAction, Transaction, UserRiskProfile
from app.rule_engine import RuleEngine


def make_profile(**kwargs) -> UserRiskProfile:
    defaults = dict(
        user_id="u_test",
        avg_transaction_amount=20000,
        single_transaction_limit=500000,
        daily_limit=1000000,
        allowed_countries=["KR"],
    )
    defaults.update(kwargs)
    return UserRiskProfile(**defaults)


def make_txn(**kwargs) -> Transaction:
    defaults = dict(
        user_id="u_test",
        amount=15000,
        merchant_id="m_1",
        merchant_name="테스트 가맹점",
        timestamp=datetime(2026, 8, 23, 14, 0, 0),
        country="KR",
    )
    defaults.update(kwargs)
    return Transaction(**defaults)


def test_normal_transaction_triggers_nothing():
    profile = make_profile()
    txn = make_txn(amount=15000)
    engine = RuleEngine(history=[])
    results = engine.evaluate(txn, profile)
    assert results == []


def test_single_transaction_limit_exceeded():
    profile = make_profile(single_transaction_limit=500000)
    txn = make_txn(amount=900000)
    engine = RuleEngine(history=[])
    results = engine.evaluate(txn, profile)
    rule_ids = [r.rule_id for r in results]
    assert "R001" in rule_ids


def test_daily_limit_exceeded_blocks():
    profile = make_profile(daily_limit=1000000)
    history = [make_txn(amount=800000, timestamp=datetime(2026, 8, 23, 9, 0, 0))]
    txn = make_txn(amount=300000, timestamp=datetime(2026, 8, 23, 20, 0, 0))
    engine = RuleEngine(history=history)
    results = engine.evaluate(txn, profile)
    r002 = next(r for r in results if r.rule_id == "R002")
    assert r002.action == RuleAction.BLOCK


def test_disallowed_country():
    profile = make_profile(allowed_countries=["KR"])
    txn = make_txn(country="US")
    engine = RuleEngine(history=[])
    results = engine.evaluate(txn, profile)
    assert any(r.rule_id == "R007" for r in results)


def test_impossible_travel_speed_blocks():
    profile = make_profile()
    history = [
        make_txn(
            timestamp=datetime(2026, 8, 23, 14, 0, 0),
            latitude=37.5665,  # 서울
            longitude=126.9780,
        )
    ]
    # 5분 뒤 뉴욕에서 결제 -> 물리적으로 불가능한 이동
    txn = make_txn(
        timestamp=datetime(2026, 8, 23, 14, 5, 0),
        latitude=40.7128,  # 뉴욕
        longitude=-74.0060,
    )
    engine = RuleEngine(history=history)
    results = engine.evaluate(txn, profile)
    assert any(r.rule_id == "R004" for r in results)


def test_rapid_repeat_payment_same_merchant():
    profile = make_profile()
    base = datetime(2026, 8, 23, 14, 0, 0)
    history = [
        make_txn(timestamp=base, merchant_id="m_1"),
        make_txn(timestamp=base.replace(minute=1), merchant_id="m_1"),
    ]
    txn = make_txn(timestamp=base.replace(minute=2), merchant_id="m_1")
    engine = RuleEngine(history=history)
    results = engine.evaluate(txn, profile)
    assert any(r.rule_id == "R006" for r in results)

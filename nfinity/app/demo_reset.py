"""
데모 상태 자동 복원 (9/5 추가)

왜 필요한가
-----------
이 배포는 심사 기간 동안 여러 사람이 차례로 열어보는 공개 데모인데, 화면의 주요 동작이
실제로 서버 상태를 바꿉니다. 그래서 앞사람이 만져놓은 상태 그대로 다음 사람이 보게 되고,
볼수록 데모가 약해집니다. 실제로 테스트 중에 두 가지가 확인됐습니다.

1. "연결하기"를 누르면 그 플랫폼이 영구히 연결됩니다. 박지수의 미연결 플랫폼을 연결하면
   수입이 늘어 현금흐름이 '주의'에서 '안전'으로 바뀌고, 45일 내 잔고 부족 예상도 사라집니다
   — 이 데모에서 가장 보여주고 싶은 장면이 사라지는 셈입니다. 동시에 "연결하면 예측이
   정확해진다"는 동선 자체도 없어집니다.
2. "거래 위험도 검사"는 결과를 risk_events에 기록합니다(그게 이 기능의 장점이기도 합니다).
   그런데 몇 번만 눌러도 최근 10건이 전부 테스트 평가로 채워져서, 원래 시드된 이상거래
   (해외 결제·심야 고액 등)가 목록 밖으로 밀려납니다.

무엇을 하나
-----------
주기적으로(기본 30분) 아래 둘을 시드 상태로 되돌립니다.

- income_sources.connected 를 scripts/seed_demo_income.py의 설정값으로 복원
- risk_events 중 transaction_id가 NULL인 행 삭제 — 실제 거래에 붙은 시드 이벤트는 모두
  transaction_id를 갖고 있고, 화면에서 즉석 검사로 만들어진 평가만 NULL입니다.
- user_settings.min_safety_balance 를 NULL(=기본값)로 복원 — PUT /shield/{uid}/settings로
  누군가 바꿔둔 최소 안전잔액이 다음 관람자에게 그대로 남지 않도록.
- Redis 예산 지출 카운터를 시드값으로 복원 — POST /budgets/record-spending은 Redis에
  INCRBYFLOAT로 누적만 하고 되돌리지 않아서(TTL 40일), 몇 번만 눌러도 예산 사용률이
  실제 거래와 무관하게 부풀려진 채 다음 관람자에게 남습니다. 시드와 같은 방식으로
  (해당 키 삭제 후 실제 거래 합계로 다시 채움) 정확한 값으로 되돌립니다.

주기를 30분으로 잡은 이유: 한 사람이 둘러보는 동안(보통 몇 분)에는 자기가 만든 변화가
그대로 남아 있어야 조작한 보람이 있고, 그 사람이 떠난 뒤 다음 사람은 온전한 데모를
봐야 하기 때문입니다.
"""
import logging

from sqlalchemy import text

logger = logging.getLogger("nfinity.demo_reset")


def reset_demo_state() -> dict:
    """연결 상태와 즉석 평가 기록을 시드 상태로 되돌립니다. 실패해도 예외를 던지지 않습니다."""
    from app.database import SessionLocal
    from app.demo_personas import DEMO_PERSONAS

    try:
        from scripts.seed_demo_income import INCOME_PLATFORMS
    except Exception:
        logger.exception("[demo_reset] 시드 설정을 불러오지 못해 복원을 건너뜁니다.")
        return {"restored": 0, "deleted_events": 0}

    demo_ids = {p["user_id"] for p in DEMO_PERSONAS}
    restored = 0
    deleted = 0
    db = SessionLocal()
    try:
        for uid, platforms in INCOME_PLATFORMS.items():
            if uid not in demo_ids:
                continue
            for platform in platforms:
                name, connected_seed = platform[0], platform[6]
                result = db.execute(
                    text(
                        "UPDATE income_sources SET connected = :want, "
                        "connected_at = CASE WHEN :want THEN COALESCE(connected_at, now()) ELSE NULL END "
                        "WHERE user_id = :uid AND platform_name = :name AND connected <> :want"
                    ),
                    {"want": bool(connected_seed), "uid": uid, "name": name},
                )
                restored += result.rowcount or 0

        # 즉석 검사로 생긴 평가만 지웁니다(시드 이벤트는 transaction_id가 있습니다).
        res = db.execute(text("DELETE FROM risk_events WHERE transaction_id IS NULL"))
        deleted = res.rowcount or 0

        # 화면에서 바꿔둔 최소 안전잔액을 기본값으로 되돌립니다(NULL이면 get_shield가
        # DEFAULT_MIN_SAFETY_BALANCE로 폴백). current_balance는 시드값이라 건드리지 않습니다.
        for uid in demo_ids:
            db.execute(
                text("UPDATE user_settings SET min_safety_balance = NULL WHERE user_id = :uid"),
                {"uid": uid},
            )

        db.commit()
        if restored or deleted:
            logger.info("[demo_reset] 연결 상태 %d건 복원, 즉석 평가 %d건 정리", restored, deleted)
    except Exception:
        db.rollback()
        logger.exception("[demo_reset] 복원 중 오류가 발생했습니다.")
    finally:
        db.close()

    # Redis 예산 지출 복원은 별도 트랜잭션/연결이라 위 DB 작업과 분리해서, 하나가 실패해도
    # 다른 하나는 되돌아가도록 합니다(시드 스크립트의 로직을 그대로 재사용).
    _reset_demo_spending()

    return {"restored": restored, "deleted_events": deleted}


def _reset_demo_spending() -> None:
    """데모 페르소나의 Redis 예산 지출 카운터를 시드 상태(실제 거래 합계)로 되돌립니다.
    seed_demo_personas의 로직을 그대로 재사용합니다 — 먼저 해당 budget:* 키를 삭제하고,
    실제 mock 거래를 카테고리별로 합산해 다시 채우므로 재호출해도 값이 정확합니다."""
    from app.database import SessionLocal

    try:
        from scripts.seed_demo_personas import _reset_demo_redis_keys, seed_budgets_and_spending
    except Exception:
        logger.exception("[demo_reset] 예산 시드 로직을 불러오지 못해 지출 복원을 건너뜁니다.")
        return

    try:
        _reset_demo_redis_keys()
        db = SessionLocal()
        try:
            seed_budgets_and_spending(db)
        finally:
            db.close()
    except Exception:
        logger.exception("[demo_reset] Redis 예산 지출 복원 중 오류가 발생했습니다.")

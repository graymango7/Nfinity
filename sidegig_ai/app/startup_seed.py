"""
9/2 추가 — 배포 직후 DB가 비어있으면 앱이 스스로 스키마/데모 데이터를 채우는 시작 훅.

배경(9/2에 발견한 문제): 지금까지 스키마 생성은 docker-compose.yml이 로컬 postgres
컨테이너를 "완전히 새로" 띄울 때만 `sql/init.sql`을 자동 실행해주는 방식(공식 postgres
이미지의 `/docker-entrypoint-initdb.d/` 관례)에 의존했고, 데모 데이터(5개 페르소나,
mock 거래, 예산/리스크/수입) 적재는 `scripts/`·`data/` 폴더의 스크립트를 사람이 직접
로컬에서 실행하는 방식이었습니다. 둘 다 "실제 배포 환경(Railway/Render 등, 관리형
Postgres를 쓰거나 Dockerfile만으로 빌드하는 경우)"에서는 자동으로 일어나지 않는
가정이었습니다 — 그 결과 그대로 배포하면 DB가 완전히 빈 채로 뜨고, 5개 데모 페르소나가
전부 404/빈 화면으로 보이는 문제가 있었습니다.

이 파일은 앱이 시작할 때(app/main.py의 startup 이벤트) 아래 두 단계를 자동으로
실행해서, "Dockerfile로 빌드해서 아무 Postgres/Redis에 연결만 시켜주면" 나머지는
전부 앱이 알아서 채우도록 만듭니다.

1. ensure_schema(): sql/init.sql 전체를 그대로 실행합니다. 이 파일은 전부
   `CREATE TABLE IF NOT EXISTS` / `ON CONFLICT DO NOTHING`으로 작성돼 있어서(원래도
   docker-compose 로컬 재시작에 안전하도록 그렇게 만들어져 있었음) 스키마가 이미 있어도
   매번 안전하게 다시 실행할 수 있습니다 — 그래서 별도 "이미 있는지" 확인 없이 매번
   실행합니다.
2. ensure_demo_data_seeded(): transactions/user_risk_profiles/budgets/income_sources
   각각이 비어있는지 따로 확인해서, 비어있는 단계만 순서대로 채웁니다(아래 파이프라인
   재실행/재시작 시 이미 끝난 단계를 다시 하지 않도록 — 특히 이미 데이터가 있는데
   ingestion을 다시 돌리면 transactions를 TRUNCATE하고 다시 채우기 때문에 여기서만
   엄격하게 "완전히 비어있을 때"로 제한합니다).

실패해도(예: CSV가 없거나 DB 연결 문제) 예외를 밖으로 던지지 않고 로그만 남깁니다 —
시딩이 실패했다고 서버 자체가 못 뜨면 원인 파악(헬스체크, 로그)조차 더 어려워지기
때문입니다.
"""
import logging
from pathlib import Path

from sqlalchemy import text

logger = logging.getLogger("sidegig.startup_seed")

_SQL_INIT_PATH = Path(__file__).resolve().parent.parent / "sql" / "init.sql"


def ensure_schema():
    """sql/init.sql을 그대로 실행해서 테이블/인덱스가 있는지 보장합니다.
    (docker-compose의 로컬 postgres 자동초기화에 의존하지 않고, 어떤 Postgres에
    연결하든 앱이 직접 스키마를 만듭니다 — 관리형 Postgres 배포 대응.)"""
    from app.database import engine

    if not _SQL_INIT_PATH.exists():
        logger.warning("[startup_seed] sql/init.sql을 찾을 수 없어 스키마 생성을 건너뜁니다: %s", _SQL_INIT_PATH)
        return

    sql_text = _SQL_INIT_PATH.read_text(encoding="utf-8")
    raw_conn = engine.raw_connection()
    try:
        cursor = raw_conn.cursor()
        # psycopg2는 파라미터 없는 execute()에 여러 문장(세미콜론으로 구분, DO $$ ... $$
        # 블록 포함)을 한 번에 넘기면 simple query protocol로 그대로 실행합니다 —
        # `psql -f init.sql`과 동일한 방식입니다.
        cursor.execute(sql_text)
        raw_conn.commit()
        cursor.close()
        logger.info("[startup_seed] 스키마 확인/생성 완료 (sql/init.sql 실행).")
    except Exception:
        raw_conn.rollback()
        logger.exception("[startup_seed] 스키마 생성 중 오류가 발생했습니다.")
        raise
    finally:
        raw_conn.close()


def _count(db, table: str, where: str = "") -> int:
    row = db.execute(text(f"SELECT count(*) AS n FROM {table} {where}")).mappings().first()
    return row["n"] if row else 0


def ensure_demo_data_seeded():
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        txn_count = _count(db, "transactions")
        profile_count = _count(db, "user_risk_profiles", "WHERE avg_transaction_amount > 0")
        budget_count = _count(db, "budgets")
        income_count = _count(db, "income_sources")
    finally:
        db.close()

    try:
        if txn_count == 0:
            logger.info("[startup_seed] transactions가 비어있어 mock 데이터 적재를 시작합니다...")
            from data_pipeline.ingestion import run_pipeline

            run_pipeline()
        else:
            logger.info("[startup_seed] transactions에 이미 %d건이 있어 적재를 건너뜁니다.", txn_count)

        if profile_count == 0:
            from scripts.build_user_profiles import build_profiles

            build_profiles()

        if budget_count == 0:
            from scripts.seed_demo_personas import replay_risk_assessments, seed_budgets_and_spending

            db2 = SessionLocal()
            try:
                seed_budgets_and_spending(db2)
                replay_risk_assessments(db2)
            finally:
                db2.close()

        if income_count == 0:
            from scripts.seed_demo_income import seed_income

            db3 = SessionLocal()
            try:
                seed_income(db3)
            finally:
                db3.close()

        logger.info("[startup_seed] 데모 데이터 시딩 완료.")
    except Exception:
        logger.exception("[startup_seed] 데모 데이터 시딩 중 오류가 발생했습니다 (서버는 계속 켭니다).")


def run_startup_seed():
    """app/main.py의 startup 이벤트에서 호출하는 진입점."""
    try:
        ensure_schema()
    except Exception:
        # 스키마 생성 자체가 실패하면(예: DB 연결 불가) 이후 시딩은 의미가 없으니 중단하되,
        # 서버 프로세스는 그대로 띄워서 /health로 원인을 확인할 수 있게 합니다.
        return
    ensure_demo_data_seeded()

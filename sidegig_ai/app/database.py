"""
DB / Redis 연결 설정.
- DATABASE_URL, REDIS_URL은 .env 파일에서 읽어옵니다.
- 로컬에서 도커 없이 테스트할 때는 환경변수를 sqlite 등으로 바꿔서 써도 됩니다.
"""
import os

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

load_dotenv()

DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://sidegig:sidegig_pw@localhost:5432/sidegig"
)
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    """FastAPI Depends()로 주입해서 쓰는 DB 세션 제너레이터."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_redis():
    """지연 import: redis 서버가 없는 환경(로컬 테스트)에서도 앱이 죽지 않도록."""
    import redis

    return redis.from_url(REDIS_URL, decode_responses=True)

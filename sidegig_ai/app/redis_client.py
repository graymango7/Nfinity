"""
Redis 연동 헬퍼.

지금까지는 database.py에 get_redis() 함수만 있고 실제로 쓰는 곳이 없었습니다
("README에 '지금은 연결만 해두고 아직 안 씀'이라고 적혀 있던 그 부분").
이 파일부터 실제로 Redis를 씁니다 — 예산통제 기능에서 "이번 달 카테고리별로
얼마 썼는지"를 실시간으로 집계할 때 씁니다.

왜 굳이 Redis를 쓰나요?
- PostgreSQL에서 매번 `SELECT SUM(amount) FROM transactions WHERE ...`로 합계를 구해도
  되긴 하지만, 거래가 쌓일수록 매번 다시 계산하는 게 느려집니다.
- Redis는 "숫자를 계속 더해나가는" 작업(INCRBYFLOAT)에 최적화된 아주 빠른 저장소라서,
  거래가 들어올 때마다 누적값에 더하기만 하면 되고 조회도 즉시 됩니다.
- 대신 Redis 값은 "언제든 날아가도 되는 캐시"로 취급합니다. 진짜 원본 데이터는
  PostgreSQL의 transactions 테이블에 그대로 남아있습니다.
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Optional

import redis

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

_redis_client: Optional[redis.Redis] = None


def get_redis_client() -> redis.Redis:
    """Redis 연결은 앱 시작할 때 한 번만 만들고 재사용합니다 (매번 새로 연결하면 느려짐)."""
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.from_url(REDIS_URL, decode_responses=True)
    return _redis_client


def _period_key(period: str, dt: Optional[datetime] = None) -> str:
    """'2026-08' 같은, 기간을 나타내는 문자열을 만듭니다. (월간/주간/일간)"""
    dt = dt or datetime.utcnow()
    if period == "monthly":
        return dt.strftime("%Y-%m")
    if period == "weekly":
        iso = dt.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"
    return dt.strftime("%Y-%m-%d")  # daily 등 그 외는 일 단위로


def budget_key(user_id: str, category: str, period: str = "monthly", dt: Optional[datetime] = None) -> str:
    """
    Redis에 값을 저장할 때 쓰는 '이름표'입니다.
    예: budget:u_001:식비:monthly:2026-08
    이렇게 유저+카테고리+기간을 다 합쳐서 키로 만들면, 서로 다른 유저/카테고리/달의
    값이 섞이지 않습니다.
    """
    return f"budget:{user_id}:{category}:{period}:{_period_key(period, dt)}"


def add_spending(
    user_id: str, category: str, amount: float, period: str = "monthly", now: Optional[datetime] = None
) -> float:
    """
    거래 하나가 발생할 때마다 호출합니다. 누적 지출에 amount를 더하고,
    더한 뒤의 새 누적값을 반환합니다.

    INCRBYFLOAT는 "현재 값에 얼마를 더해라"를 원자적으로(다른 요청과 겹쳐도 안전하게)
    처리해주는 Redis 명령어입니다.

    now: 9/2에 추가된 파라미터입니다. "이번 달"을 실서버 시각이 아니라 데모 데이터
    기준 시각(app/demo_clock.py의 get_demo_now)으로 맞추기 위한 것입니다 — 실서버
    시각을 그대로 쓰면 mock 데이터가 끝난 달이 지나가는 순간 이번 달 누적치가 항상
    0이 되어버리는 버그가 있었습니다. 안 넘기면 기존처럼 실서버 시각을 씁니다.
    """
    client = get_redis_client()
    key = budget_key(user_id, category, period, now)
    new_total = client.incrbyfloat(key, amount)
    # 기간이 끝나면(예: 다음 달이 되면) 이 값은 더 이상 의미가 없으니, 40일 뒤 자동 삭제
    # 되도록 만료시간(TTL)을 걸어둡니다. 안 걸어두면 Redis에 계속 쌓입니다.
    client.expire(key, 60 * 60 * 24 * 40)
    return float(new_total)


def get_spending(user_id: str, category: str, period: str = "monthly", now: Optional[datetime] = None) -> float:
    """지금까지 누적된 지출을 조회합니다. 값이 없으면(=아직 지출 없음) 0을 반환합니다.
    now: add_spending과 동일 — 데모 기준 시각을 넘기면 그 기준으로 키를 찾습니다."""
    client = get_redis_client()
    key = budget_key(user_id, category, period, now)
    val = client.get(key)
    return float(val) if val else 0.0


def ping() -> bool:
    """Redis가 살아있는지 확인 (헬스체크용)."""
    try:
        return get_redis_client().ping()
    except redis.exceptions.RedisError:
        return False

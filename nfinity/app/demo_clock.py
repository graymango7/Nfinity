"""
데모 "현재 시각" 기준점 (9/2 발견한 버그를 고치면서 추가)

문제: budgets.py/redis_client.py/seed_demo_personas.py가 전부 실제 서버 시각
(datetime.utcnow() / date.today())을 "이번 달"의 기준으로 썼습니다. 그런데 이 앱의
mock_transactions.csv는 2026-05-25 ~ 2026-08-22 데이터만 있습니다 — 즉 실제 달력이
8월을 넘어가는 순간(정확히는 심사 기간인 9/7~9/11 내내), "이번 달 실제로 쓴 돈"을 실서버
시각 기준으로 계산하면 이번 달(9월) 거래가 하나도 없으니 무조건 0원이 나옵니다.
직접 재현해서 확인함: 9/2에 seed_demo_personas.py를 돌려보니 5명 전원 "이번 달 실지출
합계 0원"이 나왔고, 그 상태로는 예산 화면이 전부 0%/미사용으로 보여서 리뷰 때 그렇게
공들인 예산통제 기능이 심사 당일엔 텅 빈 것처럼 보이는 심각한 문제였습니다.

고친 방식: "지금"을 실서버 시각이 아니라 "이 mock 데이터셋에 있는 가장 최근 거래
시각"으로 정의합니다(get_demo_now). 데이터가 8/22에 끝나니 데모 기준 "오늘"은 항상
8/22이고, 심사위원이 9/7에 열든 9/11에 열든 결과가 똑같이 재현됩니다 — 실서버 시각에
좌우되지 않습니다. DB 쿼리 결과는 프로세스 안에서 캐싱합니다(mock 데이터는 안 바뀌므로
매 요청마다 다시 조회할 필요가 없습니다).
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

_cached_demo_now: datetime | None = None


def get_demo_now(db: Session) -> datetime:
    """이 mock 데이터셋 기준 '지금'. transactions 테이블의 가장 최근 timestamp를 씁니다."""
    global _cached_demo_now
    if _cached_demo_now is not None:
        return _cached_demo_now

    row = db.execute(text("SELECT MAX(timestamp) AS latest FROM transactions")).mappings().first()
    latest = row["latest"] if row else None
    # 거래가 하나도 없는 극단적인 경우(빈 DB)에는 실서버 시각으로 대체합니다.
    _cached_demo_now = latest if latest is not None else datetime.utcnow()
    return _cached_demo_now

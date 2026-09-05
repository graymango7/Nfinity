"""
8/30 데모 모드 준비 스크립트 (1회성, 재실행해도 안전 — idempotent)

공모전 제출은 "내 정보를 직접 입력"하는 게 아니라 "버튼 하나로 미리 만들어둔 인물의
폰을 잠깐 구경하는" 데모 방식으로 가기로 했습니다(가상 데이터 사용은 데이콘 운영진이
게시판에서 공식 허용 — 화면에 안내 문구만 넣으면 됨). 그래서 이야기가 뚜렷한 5명의
페르소나(app/demo_personas.py에 정의, seed_data.py의 story_flag 기준)를 골라서, 그
사람들의 *실제* 거래 내역(mock_transactions.csv 기반, DB에 이미 들어있음)을 가지고:

  1. 카테고리별 이번 달 실제 지출 합계를 계산해서 Redis 예산 카운터에 반영합니다
     (지금까지는 이 카운터가 실제 거래와 연결되지 않은 채 비어 있었습니다 — budgets.py
     주석에 있던 "나중엔 자동으로 연결" 부분을 데모용으로 채우는 작업입니다. 숫자를
     지어내는 게 아니라, DB에 이미 있는 실제 mock 거래를 합산해서 반영하는 것입니다).
  2. 각 페르소나의 전체 거래를 시간순으로 리스크 평가 로직에 실제로 흘려보내서
     risk_events를 채웁니다 (지금까지는 이 테이블이 비어있어서 리스크 점수가 항상 0이었습니다).
  3. 카테고리별 예산 한도(budgets 테이블)를 만듭니다 — 한도 자체는 "이 페르소나라면 이
     정도가 적당하다"는 판단이 들어간 값이지만, 지출액은 위 1번처럼 실제 데이터입니다.

페르소나 목록 자체는 app/demo_personas.py 한 곳에서만 관리합니다(프론트가 부르는
GET /api/v1/demo/personas도 같은 파일을 씁니다) — 목록이 두 군데서 따로 관리되며
어긋나는 걸 막기 위함입니다.

실행: python3 scripts/seed_demo_personas.py  (로컬 PostgreSQL/Redis가 떠 있어야 함)

9/2 버그 수정: "이번 달"을 실서버 시각(date.today())으로 잡고 있었는데, mock 데이터가
2026-08-22에 끝나서 실제 달력이 9월로 넘어가면(심사 기간 9/7~9/11 내내 그렇습니다) 이
스크립트가 매번 "이번 달 실지출 합계 0원"을 채우게 되는 문제가 있었습니다 — 실제로
9/2에 재현해서 확인했습니다. app/demo_clock.py의 get_demo_now()(=mock 데이터의 가장
최근 거래 시각, 즉 8/22)를 "오늘"로 쓰도록 고쳤습니다. Redis도 이제 이 기준 시각으로
키를 저장해야 budgets.py가 조회할 때(같은 함수로 같은 기준 시각을 씀) 값이 맞습니다.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("DATABASE_URL", "postgresql://sidegig:sidegig_pw@localhost:5432/sidegig")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from sqlalchemy import text  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.demo_clock import get_demo_now  # noqa: E402
from app.demo_personas import CATEGORIES, DEMO_PERSONAS, PERIOD  # noqa: E402
from app.redis_client import add_spending, get_redis_client  # noqa: E402
from data_pipeline.categorizer import categorize_merchant  # noqa: E402


def _reset_demo_redis_keys():
    """재실행해도 중복 누적되지 않도록, 이 스크립트가 채우는 5명의 budget:* 키를 먼저 지웁니다.
    (Redis DEL은 와일드카드를 지원하지 않아서 KEYS로 실제 키를 찾아서 지웁니다.)"""
    client = get_redis_client()
    for p in DEMO_PERSONAS:
        keys = client.keys(f"budget:{p['user_id']}:*:{PERIOD}:*")
        if keys:
            client.delete(*keys)


def seed_budgets_and_spending(db):
    today = get_demo_now(db).date()
    month_start = today.replace(day=1)
    print(f"[예산/지출] 이번 달 기준(데모 데이터 기준 시각): {month_start} ~ {today}")

    for p in DEMO_PERSONAS:
        uid = p["user_id"]
        rows = db.execute(
            text(
                "SELECT merchant_name, amount FROM transactions "
                "WHERE user_id = :uid AND timestamp >= :start AND timestamp <= :end"
            ),
            {"uid": uid, "start": month_start, "end": today},
        ).mappings().all()

        spend_by_cat = {c: 0.0 for c in CATEGORIES}
        for r in rows:
            cat = categorize_merchant(r["merchant_name"])
            spend_by_cat[cat] = spend_by_cat.get(cat, 0.0) + float(r["amount"])

        for cat in CATEGORIES:
            limit_amount = p["limits"][cat]
            db.execute(
                text(
                    """
                    INSERT INTO budgets (user_id, category, period, limit_amount)
                    VALUES (:uid, :cat, :period, :limit_amount)
                    ON CONFLICT (user_id, category, period) DO UPDATE
                        SET limit_amount = EXCLUDED.limit_amount, updated_at = now()
                    """
                ),
                {"uid": uid, "cat": cat, "period": PERIOD, "limit_amount": limit_amount},
            )
            spent = spend_by_cat[cat]
            if spent > 0:
                add_spending(uid, cat, spent, PERIOD, now=get_demo_now(db))
        db.commit()
        total = sum(spend_by_cat.values())
        print(f"  {p['name']}({p['job']}, {p['story']}): 이번 달 실지출 합계 {total:,.0f}원 "
              f"({', '.join(f'{c} {v:,.0f}' for c, v in spend_by_cat.items() if v > 0)})")


def replay_risk_assessments(db):
    from app.models import Transaction
    from app.routers.risk import assess_transaction

    print("\n[리스크] 전체 거래 시간순 재생 (risk_events 채우기)")
    for p in DEMO_PERSONAS:
        uid = p["user_id"]
        # 재실행해도 중복 기록되지 않도록, 이 유저의 기존 risk_events를 먼저 지웁니다.
        db.execute(text("DELETE FROM risk_events WHERE user_id = :uid"), {"uid": uid})
        db.commit()
        rows = db.execute(
            text("SELECT * FROM transactions WHERE user_id = :uid ORDER BY timestamp"),
            {"uid": uid},
        ).mappings().all()

        flagged = 0
        for r in rows:
            txn = Transaction(
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
            result = assess_transaction(txn, db=db)
            if result.triggered_rules:
                flagged += 1
        print(f"  {p['name']}: 거래 {len(rows)}건 중 {flagged}건 리스크 이벤트 기록")


if __name__ == "__main__":
    _reset_demo_redis_keys()
    db = SessionLocal()
    try:
        seed_budgets_and_spending(db)
        replay_risk_assessments(db)
    finally:
        db.close()
    print("\n완료.")

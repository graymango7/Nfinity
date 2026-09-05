"""
9/2 추가 — 수입 플랫폼 연결 데모용 시드 스크립트 (1회성, 재실행해도 안전 — idempotent)

"여러 플랫폼에서 오는 수입이 정신없다"는 N잡러의 핵심 페인포인트를 보여주기 위해,
계좌/플랫폼을 실제로 연동하는 대신(이번 MVP 범위 밖) 각 페르소나별로 그럴듯한 수입
플랫폼 2~3개와 그 정산 내역을 미리 만들어둡니다. income_sources.connected가 false인
동안 프론트는 이 데이터를 보여주지 않다가, "연결하기" 버튼(POST
/api/v1/income/sources/{id}/connect)을 누르는 순간 true로 바뀌면서 이미 DB에 있던
이 데이터가 드러납니다 — 실제 마이데이터 연동과 사용자 체감이 비슷하도록 일부러 이렇게
설계했습니다.

각 페르소나마다 플랫폼 하나는 일부러 미연결(connected=False) 상태로 남겨둡니다 —
데모에서 "연결하기"를 실제로 눌러볼 거리가 있어야 하기 때문입니다.

금액/직업은 app/demo_personas.py의 job/story와 mock_transactions.csv 기반 실제 지출
규모(scripts/seed_demo_personas.py 실행 결과로 확인한 이번 달 지출 합계)를 참고해서,
"이 정도는 벌어야 저 정도를 쓰는 게 말이 된다"는 수준으로 정했습니다(완전 무작위 아님).

날짜는 app/demo_clock.py의 get_demo_now() 기준(=mock 거래 데이터의 마지막 시각,
2026-08-22)으로 계산합니다 — budgets.py와 동일한 "데모 기준 시각"을 씁니다. 그래야
"이번 주/이번 달 수입" 집계가 실서버 시각이 아니라 데이터 기준으로 안정적으로 나옵니다.

실행: python3 scripts/seed_demo_income.py  (로컬 PostgreSQL이 떠 있어야 함)
"""
import os
import random
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("DATABASE_URL", "postgresql://sidegig:sidegig_pw@localhost:5432/sidegig")

from sqlalchemy import text  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.demo_clock import get_demo_now  # noqa: E402
from app.demo_personas import DEMO_PERSONAS  # noqa: E402

random.seed(42)

# 페르소나별 수입 플랫폼 구성. 각 항목: (플랫폼명, 유형, 이모지, 정산주기(일), 1회 평균액,
# 변동폭(0~1), 연결 상태). 정산주기가 짧을수록(배달) 이벤트가 자주, 길수록(본업) 드물게
# 생성됩니다. 리스트의 마지막 항목을 일부러 미연결로 둡니다.
INCOME_PLATFORMS = {
    "c5dd4004-498c-4409-9472-7a0e457ff8f1": [  # 김민철 - 배달 투잡러
        ("본업 급여", "본업", "🏢", 30, 2400000, 0.02, True),
        ("배달의민족", "배달", "🛵", 7, 280000, 0.3, True),
        ("쿠팡이츠", "배달", "🚴", 7, 150000, 0.35, False),
    ],
    "6d422b5f-1b23-4312-bbfc-4ef97e5fc845": [  # 박지수 - 프리랜서 마케터
        ("클라이언트 직접입금", "프리랜서", "🤝", 14, 1200000, 0.5, True),
        ("크몽", "프리랜서", "💼", 10, 450000, 0.4, True),
        ("숨고", "프리랜서", "🧩", 12, 300000, 0.45, False),
    ],
    "131b9d96-aa5a-4938-844d-2d08b7a2df9a": [  # 이하늘 - IT 개발자
        ("회사 급여", "본업", "🏢", 30, 4800000, 0.02, True),
        ("외주 프로젝트", "프리랜서", "💻", 21, 2000000, 0.6, True),
        ("개인 유튜브", "콘텐츠", "🎬", 30, 90000, 0.5, False),
    ],
    "a0598579-8124-47ac-ab26-dc5e2c7c8c1a": [  # 최유진 - 크리에이터 인플루언서
        ("유튜브 애드센스", "콘텐츠", "📺", 30, 3200000, 0.3, True),
        ("브랜드 협찬", "콘텐츠", "🤳", 18, 2500000, 0.6, True),
        ("굿즈 스토어", "커머스", "🛍️", 14, 800000, 0.5, False),
    ],
    "c781347d-a5e7-47fc-87ab-4f298190cdfb": [  # 정다운 - 강사 겸 배민커넥트
        ("강의료 정산", "프리랜서", "📚", 30, 1800000, 0.25, True),
        ("배민커넥트", "배달", "🛵", 7, 220000, 0.3, True),
        ("온라인 강의 플랫폼", "프리랜서", "🎧", 30, 350000, 0.4, False),
    ],
}

PERIOD_START_OFFSET_DAYS = 90  # get_demo_now() 기준 90일 전부터 수입 이벤트 생성


def _reset(db, user_ids):
    db.execute(text("DELETE FROM income_events WHERE user_id = ANY(:uids)"), {"uids": user_ids})
    db.execute(text("DELETE FROM income_sources WHERE user_id = ANY(:uids)"), {"uids": user_ids})
    db.commit()


def seed_income(db):
    demo_now = get_demo_now(db)
    period_start = demo_now - timedelta(days=PERIOD_START_OFFSET_DAYS)
    user_ids = list(INCOME_PLATFORMS.keys())
    _reset(db, user_ids)

    name_by_id = {p["user_id"]: p["name"] for p in DEMO_PERSONAS}

    for uid, platforms in INCOME_PLATFORMS.items():
        print(f"\n[수입] {name_by_id.get(uid, uid)}")
        for platform_name, platform_type, icon, cycle_days, avg_amount, variance, connected in platforms:
            source_id = db.execute(
                text(
                    """
                    INSERT INTO income_sources
                        (user_id, platform_name, platform_type, icon_emoji, connected, connected_at)
                    VALUES (:uid, :name, :type, :icon, :connected, CASE WHEN :connected THEN now() ELSE NULL END)
                    RETURNING source_id
                    """
                ),
                {"uid": uid, "name": platform_name, "type": platform_type, "icon": icon, "connected": connected},
            ).scalar()

            # 정산주기(cycle_days)마다 한 건씩, period_start ~ demo_now+7일(정산예정 포함)까지 생성.
            # demo_now를 넘어가는 회차는 '정산예정', 그 이전은 '정산완료'.
            t = period_start
            count, total = 0, 0
            while t <= demo_now + timedelta(days=7):
                amount = max(10000, int(avg_amount * (1 + random.uniform(-variance, variance))))
                status = "정산완료" if t <= demo_now else "정산예정"
                db.execute(
                    text(
                        """
                        INSERT INTO income_events (source_id, user_id, amount, memo, status, settled_at)
                        VALUES (:sid, :uid, :amount, :memo, :status, :settled_at)
                        """
                    ),
                    {
                        "sid": source_id, "uid": uid, "amount": amount,
                        "memo": f"{platform_name} 정산", "status": status, "settled_at": t,
                    },
                )
                count += 1
                total += amount
                t += timedelta(days=cycle_days)
            db.commit()
            tag = "연결됨" if connected else "미연결(데모용)"
            print(f"  {icon} {platform_name} ({platform_type}, {tag}): {count}건, 누적 {total:,}원")


if __name__ == "__main__":
    db = SessionLocal()
    try:
        seed_income(db)
        print("\n완료.")
    finally:
        db.close()

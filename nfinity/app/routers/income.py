"""
수입 플랫폼 연결 API — 9/2 추가

배경: "N잡러는 여러 플랫폼에서 수입이 들쭉날쭉 들어와서 정신없다"는 게 이 서비스가
풀어야 할 핵심 문제인데, 지금까지는 지출(리스크/예산/절세) 기능만 있고 정작 "수입이
얼마나, 어디서 들어오는지"를 한눈에 보여주는 화면이 없었습니다. 실제 오픈뱅킹/마이데이터
연동은 이번 MVP 범위 밖이라, 계좌를 "연결"하면 그 플랫폼의 수입이 드러나는 흐름을
더미 데이터(scripts/seed_demo_income.py)로 재연합니다.

- GET  /api/v1/income/sources                 : 이 유저의 연결 가능한 플랫폼 목록 (연결 여부 포함)
- POST /api/v1/income/sources/{id}/connect    : "연결하기" — 이미 있던 데이터를 드러냄 (idempotent)
- POST /api/v1/income/sources/{id}/disconnect : "연결 해제" — 다시 숨김 (9/2 세 번째 업데이트,
  기획서 8번 "사용자가 연결 해제·삭제·정정할 수 있는 화면" 요구사항 반영)
- GET  /api/v1/income/summary                  : 한 장 요약 홈 카드가 그대로 쓰는 집계
- GET  /api/v1/income/events                    : 최근 수입 이벤트 목록 (연결된 플랫폼만)
"""
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import get_db
from app.demo_clock import get_demo_now
from app.models import (
    IncomeByPlatform,
    IncomeConnectResponse,
    IncomeDisconnectResponse,
    IncomeEvent,
    IncomeSource,
    IncomeSummary,
)
from app.security import verify_api_key

router = APIRouter(prefix="/api/v1/income", tags=["income"], dependencies=[Depends(verify_api_key)])


@router.get("/sources", response_model=list[IncomeSource])
def get_sources(user_id: str, db: Session = Depends(get_db)):
    """이 유저에게 연결 가능한 플랫폼 전체 목록. 연결 안 된 것도 포함해서 보여줍니다
    (연결 유도 화면이 "연결하면 이만큼 더 보여요"를 말해줄 수 있도록 건수만 미리 노출)."""
    rows = db.execute(
        text(
            """
            SELECT s.source_id, s.platform_name, s.platform_type, s.icon_emoji,
                   s.connected, s.connected_at,
                   (SELECT count(*) FROM income_events e WHERE e.source_id = s.source_id) AS pending_count
            FROM income_sources s
            WHERE s.user_id = :uid
            ORDER BY s.connected DESC, s.created_at
            """
        ),
        {"uid": user_id},
    ).mappings().all()

    if not rows:
        raise HTTPException(status_code=404, detail="이 유저에게 연결 가능한 수입 플랫폼이 없습니다.")

    return [
        IncomeSource(
            source_id=str(r["source_id"]),
            platform_name=r["platform_name"],
            platform_type=r["platform_type"],
            icon_emoji=r["icon_emoji"],
            connected=r["connected"],
            connected_at=r["connected_at"],
            pending_event_count=0 if r["connected"] else r["pending_count"],
        )
        for r in rows
    ]


@router.post("/sources/{source_id}/connect", response_model=IncomeConnectResponse)
def connect_source(source_id: str, user_id: str, db: Session = Depends(get_db)):
    """플랫폼 연결하기. 이미 연결돼있으면 그냥 현재 상태를 그대로 돌려줍니다(재시도해도 안전)."""
    row = db.execute(
        text(
            "SELECT source_id, platform_name, connected FROM income_sources "
            "WHERE source_id = :sid AND user_id = :uid"
        ),
        {"sid": source_id, "uid": user_id},
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="존재하지 않는 수입 플랫폼입니다.")

    already_connected = bool(row["connected"])
    if not already_connected:
        db.execute(
            text("UPDATE income_sources SET connected = TRUE, connected_at = now() WHERE source_id = :sid"),
            {"sid": source_id},
        )
        db.commit()

    count_row = db.execute(
        text("SELECT count(*) AS n FROM income_events WHERE source_id = :sid"), {"sid": source_id}
    ).mappings().first()

    return IncomeConnectResponse(
        source_id=source_id,
        platform_name=row["platform_name"],
        connected=True,
        newly_connected=not already_connected,
        revealed_event_count=count_row["n"] if count_row else 0,
    )


@router.post("/sources/{source_id}/disconnect", response_model=IncomeDisconnectResponse)
def disconnect_source(source_id: str, user_id: str, db: Session = Depends(get_db)):
    """연결 해제. 실제 계좌 연동이라면 이 시점에 마이데이터 접근 권한을 회수하는
    동작에 해당합니다 — 이 데모에서는 connected를 다시 false로 돌려서, 그 플랫폼의
    수입이 요약/시뮬레이션(app/cashflow.py)에서 다시 제외되도록 합니다. 이미 연결 해제된
    상태에 다시 호출해도 안전합니다(idempotent)."""
    row = db.execute(
        text(
            "SELECT source_id, platform_name FROM income_sources "
            "WHERE source_id = :sid AND user_id = :uid"
        ),
        {"sid": source_id, "uid": user_id},
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="존재하지 않는 수입 플랫폼입니다.")

    db.execute(
        text("UPDATE income_sources SET connected = FALSE, connected_at = NULL WHERE source_id = :sid"),
        {"sid": source_id},
    )
    db.commit()

    return IncomeDisconnectResponse(source_id=source_id, platform_name=row["platform_name"], connected=False)


@router.get("/summary", response_model=IncomeSummary)
def get_summary(user_id: str, period: str = "month", db: Session = Depends(get_db)):
    """연결된 플랫폼만 집계해서 '한 장 요약' 홈 카드용 데이터를 만듭니다.
    period: "week"(최근 7일) / "month"(이번 달, 1일부터)."""
    demo_now = get_demo_now(db)
    if period == "week":
        period_start = demo_now - timedelta(days=7)
        period_label = "이번 주"
    else:
        period_start = demo_now.replace(day=1)
        period_label = "이번 달"

    rows = db.execute(
        text(
            """
            SELECT s.platform_name, s.platform_type, s.icon_emoji,
                   e.amount, e.status
            FROM income_events e
            JOIN income_sources s ON s.source_id = e.source_id
            WHERE s.user_id = :uid AND s.connected = TRUE AND e.settled_at >= :start
            """
        ),
        {"uid": user_id, "start": period_start},
    ).mappings().all()

    by_platform: dict[str, IncomeByPlatform] = {}
    total_settled = 0.0
    total_upcoming = 0.0
    for r in rows:
        amt = float(r["amount"])
        if r["status"] == "정산완료":
            total_settled += amt
        else:
            total_upcoming += amt
        key = r["platform_name"]
        if key not in by_platform:
            by_platform[key] = IncomeByPlatform(
                platform_name=r["platform_name"],
                platform_type=r["platform_type"],
                icon_emoji=r["icon_emoji"],
                total_amount=0.0,
                event_count=0,
            )
        by_platform[key].total_amount += amt
        by_platform[key].event_count += 1

    unconnected_row = db.execute(
        text("SELECT count(*) AS n FROM income_sources WHERE user_id = :uid AND connected = FALSE"),
        {"uid": user_id},
    ).mappings().first()

    return IncomeSummary(
        user_id=user_id,
        period_label=period_label,
        total_settled=total_settled,
        total_upcoming=total_upcoming,
        by_platform=sorted(by_platform.values(), key=lambda p: -p.total_amount),
        unconnected_source_count=unconnected_row["n"] if unconnected_row else 0,
    )


@router.get("/events", response_model=list[IncomeEvent])
def get_events(user_id: str, limit: int = Query(20, ge=1, le=200), db: Session = Depends(get_db)):
    """최근 수입 이벤트 목록 (연결된 플랫폼만, 최신순).

    9/5 수정: limit에 음수가 들어오면 Postgres LIMIT에서 500이 나던 걸 Query(1~200)로 막습니다."""
    rows = db.execute(
        text(
            """
            SELECT s.platform_name, s.icon_emoji, e.amount, e.memo, e.status, e.settled_at
            FROM income_events e
            JOIN income_sources s ON s.source_id = e.source_id
            WHERE s.user_id = :uid AND s.connected = TRUE
            ORDER BY e.settled_at DESC
            LIMIT :limit
            """
        ),
        {"uid": user_id, "limit": limit},
    ).mappings().all()
    return [
        IncomeEvent(
            platform_name=r["platform_name"],
            icon_emoji=r["icon_emoji"],
            amount=float(r["amount"]),
            memo=r["memo"],
            status=r["status"],
            settled_at=r["settled_at"],
        )
        for r in rows
    ]

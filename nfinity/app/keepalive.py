"""
배포된 데모를 깨워두는 자가 핑 (9/5 추가)

왜 필요한가
-----------
Render 무료 플랜의 웹 서비스는 15분 동안 들어오는 요청이 없으면 컨테이너를 내리고,
그 다음 첫 요청은 다시 뜰 때까지 50초 넘게 걸립니다. 공모전 MVP 심사는
2026-09-07 11:00 ~ 09-11 23:59 사이에 이뤄지는데, 심사위원이 링크를 눌렀을 때 흰 화면을
한참 보게 되면 인상이 나빠지고, 접속 불가로 오해하면 결격 사유가 될 수도 있습니다.

어떻게
------
앱이 뜬 뒤 백그라운드 태스크로 KEEPALIVE_INTERVAL_SECONDS마다 자기 자신의 공개 URL
(/health)을 한 번 호출합니다. 스핀다운은 "외부에서 들어오는 요청이 없을 때" 일어나므로,
프로세스가 살아있는 동안 스스로 트래픽을 만들어 주면 잠들지 않습니다.

- 공개 URL은 Render가 넣어주는 RENDER_EXTERNAL_URL 환경변수에서 읽습니다. 이 값이 없는
  환경(로컬 개발, docker-compose)에서는 아무 것도 하지 않습니다.
- 요청 실패는 무시하고 다음 주기에 다시 시도합니다. 이 기능이 실패해도 서비스 자체에는
  영향이 없어야 하기 때문입니다.
- urllib만 씁니다(의존성 추가 없음). 동기 호출이라 이벤트 루프를 막지 않도록 스레드로
  넘깁니다.

한계: 배포·재시작 직후처럼 프로세스가 아예 내려간 상태에서는 스스로 깨어날 수 없습니다.
그때는 누군가 한 번 접속하면 다시 살아나고, 이후로는 이 루프가 계속 유지합니다.
"""
import asyncio
import logging
import os
import urllib.request

logger = logging.getLogger("nfinity.keepalive")

# 15분 스핀다운 기준으로 여유를 둡니다.
KEEPALIVE_INTERVAL_SECONDS = int(os.environ.get("KEEPALIVE_INTERVAL_SECONDS", "300"))


def _public_health_url() -> str | None:
    base = (os.environ.get("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
    if not base:
        return None
    return base + "/health"


# 데모 상태 복원 주기(초).
#
# 처음엔 30분으로 잡았는데, 그 사이에 누군가 "연결하기"를 누르면 다음 사람이 다른 수치를
# 보게 됩니다. 실제로 플랫폼 하나가 연결된 채로 남아 잔고 부족 예상일이 사라지고(9/26 → 없음)
# 세금이 환급에서 납부로 뒤집혀, 기능명세서에 적어둔 확인 절차의 예상 결과와 화면이
# 어긋나는 상황이 발생했습니다. 관람자가 자기 조작 결과를 확인할 시간은 남기되
# 어긋난 상태가 오래가지 않도록 5분으로 줄였습니다. 관람자가 자기 조작 결과를 확인하기에는
# 충분하고(연결 즉시 화면이 다시 계산되어 바로 보임), 다음 사람이 어긋난 값을 볼 여지는 최소화됩니다.
DEMO_RESET_INTERVAL_SECONDS = int(os.environ.get("DEMO_RESET_INTERVAL_SECONDS", "300"))


async def _loop(url: str):
    elapsed = 0
    while True:
        await asyncio.sleep(KEEPALIVE_INTERVAL_SECONDS)
        elapsed += KEEPALIVE_INTERVAL_SECONDS
        try:
            await asyncio.to_thread(_fetch, url)
        except Exception as exc:  # 실패해도 서비스에 영향이 없어야 하므로 로그만 남깁니다.
            logger.warning("[keepalive] 자가 핑 실패: %s", exc)

        if elapsed >= DEMO_RESET_INTERVAL_SECONDS:
            elapsed = 0
            try:
                from app.demo_reset import reset_demo_state

                await asyncio.to_thread(reset_demo_state)
            except Exception as exc:
                logger.warning("[keepalive] 데모 상태 복원 실패: %s", exc)

            # 생성형 AI 결과 캐시가 비어 있으면 조용히 채워둡니다.
            #
            # 무료 Redis는 재시작 시 내용이 사라질 수 있고, Gemini 무료 할당량은 하루 단위로
            # 소진·회복됩니다. 둘이 겹치면 심사위원이 열었을 때 AI 브리핑이 템플릿 문장으로
            # 보일 수 있어서, 주기적으로 비어 있는 것만 한 건씩 미리 만들어 둡니다.
            # 한 번에 하나만 호출해 할당량을 아끼고, 실패하면 다음 주기에 다시 시도합니다.
            try:
                await asyncio.to_thread(_warm_one_brief)
            except Exception as exc:
                logger.warning("[keepalive] 브리핑 예열 실패: %s", exc)


def _warm_one_brief() -> None:
    """캐시가 없는 데모 페르소나 브리핑을 하나만 생성해 캐시에 넣습니다."""
    from app.database import SessionLocal
    from app.demo_personas import DEMO_PERSONAS
    from app.routers.brief import get_brief

    db = SessionLocal()
    try:
        for persona in DEMO_PERSONAS:
            try:
                result = get_brief(persona["user_id"], db=db)
            except Exception:
                continue
            if result.get("source") == "gemini":
                continue  # 이미 캐시에 있음
            # 아직 템플릿이면 방금 호출에서 캐시가 채워졌을 수도 있으니 여기서 멈춥니다.
            logger.info("[keepalive] %s 브리핑 예열 시도 (source=%s)", persona["name"], result.get("source"))
            return
    finally:
        db.close()


def _fetch(url: str):
    with urllib.request.urlopen(url, timeout=30) as resp:
        resp.read(64)


def start(loop_factory=asyncio.ensure_future):
    """앱 시작 시 호출. 공개 URL을 모르는 환경에서는 조용히 넘어갑니다."""
    url = _public_health_url()
    if not url:
        logger.info("[keepalive] RENDER_EXTERNAL_URL이 없어 자가 핑을 켜지 않습니다.")
        return None
    logger.info("[keepalive] %d초마다 %s 를 호출해 인스턴스를 깨워둡니다.", KEEPALIVE_INTERVAL_SECONDS, url)
    return loop_factory(_loop(url))

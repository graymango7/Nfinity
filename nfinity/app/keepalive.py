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
KEEPALIVE_INTERVAL_SECONDS = int(os.environ.get("KEEPALIVE_INTERVAL_SECONDS", "600"))


def _public_health_url() -> str | None:
    base = (os.environ.get("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
    if not base:
        return None
    return base + "/health"


# 데모 상태 복원 주기(초). 한 사람이 둘러보는 동안에는 자기가 만든 변화가 남아 있고,
# 그 사람이 떠난 뒤 다음 사람은 온전한 데모를 보도록 30분으로 잡았습니다.
DEMO_RESET_INTERVAL_SECONDS = int(os.environ.get("DEMO_RESET_INTERVAL_SECONDS", "1800"))


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

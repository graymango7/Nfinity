"""
기본 Rate Limiting (9/2 세 번째 업데이트 — 금융 데이터를 다루는 API라 스크립트로
전체 유저 데이터를 긁어가거나(스크래이핑), API_ACCESS_KEY를 무차별 대입으로 맞춰보는
공격을 최소한이라도 막기 위해 추가했습니다.

구현 방식과 한계를 솔직하게 적어둡니다:
- 이 앱은 지금 단일 프로세스(uvicorn 워커 1개)로만 배포됩니다(Dockerfile CMD에
  --workers 옵션이 없음). 그래서 메모리(dict) 안에 "IP별 최근 요청 시각"을 그냥
  들고 있는 가장 단순한 방식으로도 충분히 동작합니다.
- 만약 나중에 --workers 2 이상으로 멀티프로세스 배포를 한다면, 이 방식은 프로세스마다
  카운터가 따로 놀아서 실제 허용량이 워커 수만큼 늘어나 버립니다 — 그때는 Redis
  (이미 이 프로젝트에 떠 있음) 같은 공유 저장소 기반으로 바꿔야 합니다. 지금 범위에선
  단일 프로세스 배포가 맞아서 이 정도로 충분하다고 판단했습니다.
- 클라이언트 IP는 리버스 프록시(대부분의 PaaS가 이 앞에 하나 있음) 뒤에 있으면
  X-Forwarded-For 헤더의 첫 값을 신뢰합니다. 이건 "믿을 수 있는 프록시가 이 헤더를
  올바르게 채워준다"는 전제가 있어야 의미가 있고, 직접 인터넷에 노출된 배포(프록시 없음)
  에서는 이 헤더를 요청자가 마음대로 조작할 수 있으므로 그 경우엔 request.client.host를
  씁니다.
- 정적 프론트(HTML/JS/CSS)는 제한하지 않습니다 — 페이지 하나 로드에도 여러 파일을
  받아오는데, 그것까지 세면 정상 사용자도 금방 걸립니다. /api/v1/* 요청만 셉니다.

기본값: IP당 분당 120회. 데모 페이지 하나가 보통 3~5개 API를 호출하니 넉넉하지만,
스크립트로 반복 호출하는 건 확실히 막습니다. 필요하면 아래 상수만 바꾸면 됩니다.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

WINDOW_SECONDS = 60
MAX_REQUESTS_PER_WINDOW = 120
_RATE_LIMITED_PREFIX = "/api/v1/"

# IP -> 최근 WINDOW_SECONDS 안에 들어온 요청 타임스탬프들
_hits: dict[str, deque[float]] = defaultdict(deque)


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        if not request.url.path.startswith(_RATE_LIMITED_PREFIX):
            return await call_next(request)

        ip = _client_ip(request)
        now = time.monotonic()
        window = _hits[ip]

        while window and now - window[0] > WINDOW_SECONDS:
            window.popleft()

        if len(window) >= MAX_REQUESTS_PER_WINDOW:
            return JSONResponse(
                status_code=429,
                content={
                    "error": {
                        "code": 429,
                        "message": "요청이 너무 많습니다. 잠시 후 다시 시도해주세요.",
                    }
                },
                headers={"Retry-After": str(WINDOW_SECONDS)},
            )

        window.append(now)
        return await call_next(request)

"""
보안 응답 헤더 미들웨어 (9/2 세 번째 업데이트 — 금융보안원 주최 공모전이라 보안을
특별히 더 신경써달라는 요청으로 추가)

브라우저가 이 헤더들을 보고 스스로 방어하게 만드는, 서버 쪽에서는 몇 줄이면 되는데
효과는 큰 대책들입니다. 하나씩:

- Content-Security-Policy: "이 페이지는 어디서 온 스크립트/스타일/폰트만 믿는다"를
  선언합니다. 예를 들어 만약 XSS 취약점이 어딘가에 있어서 공격자가 스크립트를
  주입해도, CSP가 막아주면 실행되지 않습니다. frontend/*.html이 각 페이지마다
  <script>인라인 코드</script> 블록을 하나씩 쓰고 있어서(외부 api.js 파일 + 페이지별
  인라인 로직 조합) script-src에 'unsafe-inline'을 넣을 수밖에 없었습니다 — 완벽한
  CSP는 아니지만, 그래도 외부 도메인에서 스크립트를 불러오는 공격(가장 흔한 XSS
  공격 형태)은 확실히 막습니다. (다음 단계로 더 강화하려면 인라인 스크립트를 전부
  별도 .js 파일로 옮기고 nonce/hash 기반 CSP로 바꾸면 되는데, 이번 일정에서는
  범위 밖이라 다음 할 일로 남깁니다.)
- X-Content-Type-Options: nosniff — 브라우저가 파일 확장자/내용을 보고 "이거 사실
  실행 가능한 스크립트 아냐?"하고 지레짐작(MIME sniffing)하지 못하게 막습니다.
- X-Frame-Options / frame-ancestors: 이 사이트를 다른 사이트의 <iframe> 안에 몰래
  띄워서 사용자를 속이는 클릭재킹(clickjacking) 공격을 막습니다.
- Referrer-Policy: 다른 사이트로 이동할 때 이 사이트의 URL을 통째로 넘기지 않도록
  제한합니다(우리 URL엔 user_id 같은 값이 쿼리 파라미터로 들어갈 수 있어서 중요).
- Permissions-Policy: 이 앱은 카메라/마이크/위치 정보를 전혀 안 쓰므로 아예 꺼둡니다.
- Strict-Transport-Security(HSTS): 이후 요청을 전부 HTTPS로만 하도록 브라우저에
  지시합니다. 요청이 HTTPS로 왔을 때만(또는 리버스 프록시가 X-Forwarded-Proto:
  https로 알려줬을 때만) 붙입니다 — 로컬 http 개발 환경에서는 안 붙습니다.
"""
from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    # 9/5 업데이트 — 하은님의 새 frontend/index.html이 Pretendard 가변 폰트를
    # cdn.jsdelivr.net(GitHub raw 미러)에서 불러옵니다. 기존 CSP는 fonts.googleapis.com만
    # 허용해서, 이 CDN의 CSS/폰트 파일 둘 다 브라우저가 조용히 차단하고 있었습니다
    # (콘솔에서만 보이고 화면은 시스템 폰트로 자동 대체되어 눈에 잘 안 띄는 버그).
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdn.jsdelivr.net; "
    "font-src 'self' https://fonts.gstatic.com https://cdn.jsdelivr.net; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "object-src 'none'"
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = (
            "geolocation=(), microphone=(), camera=(), payment=()"
        )
        response.headers["Content-Security-Policy"] = _CSP

        is_https = request.url.scheme == "https" or request.headers.get(
            "x-forwarded-proto", ""
        ).lower() == "https"
        if is_https:
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
        return response

"""
데모 계정 접근 제한 (9/5 추가)

배경
----
이 서비스의 API는 조회할 대상을 `user_id`로 받습니다(경로 또는 쿼리 파라미터). 로그인이
없는 공개 데모라 지금까지는 누구든 `user_id`만 바꿔서 다른 사람의 예산·리스크·수입
데이터를 꺼내볼 수 있었습니다 — 전형적인 IDOR입니다. README에도 "의도적으로 남겨둔
한계"로 적어뒀지만, 금융보안원이 주최하는 공모전에서 그대로 두기에는 눈에 띄는 구멍입니다.

무엇을 하나
-----------
이 배포는 시연용이라 공개해야 할 데이터가 시드된 페르소나 3명뿐입니다. 그래서 요청에
담긴 `user_id`가 그 3명이 아니면 서버가 403으로 막습니다. mock 데이터에는 이 3명 말고도
35명의 시드 유저가 들어있는데, 그 데이터도 이제 외부에서 꺼낼 수 없습니다.

이게 진짜 인가(authorization)는 아니라는 점은 분명히 해둡니다. 실서비스로 가면 세션
로그인을 붙이고 "요청자 본인의 user_id만 조회 가능"을 서버가 강제해야 합니다. 다만 그
구조로 가기 전이라도, 공개된 데모가 시드 데이터 전체를 열어두지는 않게 하는 최소한의
방어선입니다.
"""
import re

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

# 경로에 박힌 UUID를 찾기 위한 패턴 (예: /api/v1/shield/<uuid>/scenarios)
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

_FORBIDDEN = JSONResponse(
    status_code=403,
    content={
        "error": {
            "code": 403,
            "message": "이 시연 배포는 공개된 데모 계정의 데이터만 조회할 수 있습니다.",
        }
    },
)


def allowed_user_ids() -> set[str]:
    from app.demo_personas import DEMO_PERSONAS

    return {p["user_id"] for p in DEMO_PERSONAS}


class DemoUserGuardMiddleware(BaseHTTPMiddleware):
    """/api/v1/* 요청에 담긴 user_id가 데모 페르소나인지 확인합니다.

    경로의 UUID와 쿼리 파라미터 `user_id`를 검사합니다. POST 본문에 user_id를 담는
    엔드포인트(/risk/assess)는 본문을 여기서 읽으면 라우터가 다시 읽지 못하므로,
    그쪽은 라우터에서 직접 확인합니다(app/routers/risk.py).
    """

    async def dispatch(self, request, call_next):
        path = request.url.path
        if path.startswith("/api/v1/"):
            allowed = allowed_user_ids()

            qs_user = request.query_params.get("user_id")
            if qs_user and qs_user not in allowed:
                return _FORBIDDEN

            for found in _UUID_RE.findall(path):
                if found not in allowed:
                    return _FORBIDDEN

        return await call_next(request)

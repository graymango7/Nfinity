"""
FastAPI 앱 진입점.
실행: docker-compose up --build -d  (또는 uvicorn app.main:app --reload)
확인: http://localhost:8000/docs 에서 Swagger UI로 API를 직접 눌러볼 수 있습니다.

8/30 보안 점검 업데이트:
- CORS: 프론트(정적 HTML/JS)가 이 API를 다른 origin에서 부를 수 있도록 허용 목록을
  추가했습니다. 기본값은 로컬 개발용 origin들만 허용하고, `*`(전체 허용)는 절대
  쓰지 않습니다 — 금융 데이터를 다루는 API라 필요합니다. 배포 시엔 .env의
  CORS_ALLOWED_ORIGINS에 실제 프론트 배포 주소를 콤마로 추가하세요.
- 각 라우터(risk/budgets/gig-score/expense/demo)에는 app/security.py의 verify_api_key가
  붙어있습니다. 자세한 이유는 그 파일 docstring 참고.

8/30 데모 프론트 연동:
- 루트(`/`)는 이제 JSON 상태 응답 대신 frontend/ 폴더의 데모 웹앱(index.html =
  페르소나 선택 화면)을 그대로 서빙합니다. 헬스체크는 `/health`가 대신합니다.

9/2 세 번째 업데이트(보안 강화 — 금융보안원 주최 공모전이라 특별히 신경써달라는
요청으로 추가):
- SecurityHeadersMiddleware(app/security_headers.py): CSP, X-Frame-Options,
  X-Content-Type-Options 등 브라우저 방어용 응답 헤더를 모든 응답에 붙입니다.
- RateLimitMiddleware(app/rate_limit.py): /api/v1/* 요청을 IP당 분당 제한해서
  무차별 스크래이핑/무차별 대입을 최소한이라도 막습니다.
- 미들웨어는 나중에 등록한 게 바깥쪽(먼저 실행)이라, SecurityHeadersMiddleware를
  가장 나중에 등록해서 CORS 에러/429 응답을 포함한 "모든" 응답에 보안 헤더가
  붙도록 했습니다.
"""
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.demo_guard import DemoUserGuardMiddleware
from app.keepalive import start as start_keepalive
from app.rate_limit import RateLimitMiddleware
from app.redis_client import ping as redis_ping
from app.routers import budgets, demo, expense, gig_score, income, risk, shield, tax
from app.security_headers import (
    SecurityHeadersMiddleware,
    build_security_headers,
    is_https_request,
)
from app.startup_seed import run_startup_seed

app = FastAPI(
    title="Nfinity API",
    description="N잡러를 위한 리스크방어 & 예산통제 플랫폼 백엔드",
    version="0.2.0",
)

_DEFAULT_DEV_ORIGINS = "http://localhost:3000,http://127.0.0.1:3000,http://localhost:5500,http://127.0.0.1:5500,http://localhost:8000,http://127.0.0.1:8000"
_cors_origins = [
    o.strip()
    for o in os.environ.get("CORS_ALLOWED_ORIGINS", _DEFAULT_DEV_ORIGINS).split(",")
    if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=False,
    # 9/2 세 번째 업데이트: Risk Shield의 최소 안전잔액 설정(PUT /api/v1/shield/{uid}/settings)이
    # 추가되면서 PUT도 허용해야 합니다.
    allow_methods=["GET", "POST", "PUT"],
    allow_headers=["Content-Type", "X-API-Key"],
)
# 9/5: 공개 데모라 시드된 페르소나 외의 user_id 조회를 서버에서 막습니다(app/demo_guard.py).
app.add_middleware(DemoUserGuardMiddleware)
app.add_middleware(RateLimitMiddleware)
app.add_middleware(SecurityHeadersMiddleware)

app.include_router(risk.router)
app.include_router(shield.router)
app.include_router(budgets.router)
app.include_router(gig_score.router)
app.include_router(expense.router)
app.include_router(income.router)
app.include_router(demo.router)
app.include_router(tax.router)


@app.on_event("startup")
def _seed_on_startup():
    """9/2 추가: 배포 직후 DB가 비어있으면(관리형 Postgres에 처음 연결하는 경우 등)
    스키마 생성 + 데모 데이터 시딩을 앱이 스스로 처리합니다. app/startup_seed.py 참고 —
    이미 데이터가 있으면 아무 것도 하지 않고 즉시 반환하므로 재시작마다 반복 실행돼도
    안전합니다."""
    run_startup_seed()
    # 9/5: Render 무료 플랜의 15분 스핀다운으로 심사 중 첫 접속이 50초 넘게 걸리는 걸
    # 막기 위해, 앱이 스스로 자기 /health를 주기적으로 호출합니다(app/keepalive.py).
    start_keepalive()
    # 배포/재시작 시점에도 한 번 정리해서, 앞선 관람자가 만져놓은 상태로 시작하지 않게 합니다.
    try:
        from app.demo_reset import reset_demo_state

        reset_demo_state()
    except Exception:  # 복원 실패가 서버 기동을 막으면 안 됩니다.
        pass


# ============================================================
# 공통 에러 응답 포맷 통일 (8/26 백엔드 구축 데이)
#
# 이전에는 라우터마다 FastAPI 기본 에러 형식({"detail": "..."})을 그대로 썼는데,
# 여기서 한 군데로 모아서 모든 API가 항상 같은 모양의 에러를 반환하도록 통일했습니다.
# 프론트(소현)는 이제 어떤 API를 호출하든 에러 처리 코드를 하나만 만들면 됩니다.
#
#   {"error": {"code": 404, "message": "..."}}
# ============================================================


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.status_code, "message": exc.detail}},
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """요청 body/쿼리 형식이 잘못됐을 때(예: amount에 문자열을 보낸 경우) 나오는 에러."""
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": 422,
                "message": "입력값이 올바르지 않습니다.",
                "detail": exc.errors(),
            }
        },
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """9/5 추가: 위 두 핸들러가 잡지 못한 "예상 못 한" 예외(예: 잘못된 쿼리로 DB가 던지는
    오류)까지 항상 같은 {"error": {...}} 포맷으로 내보냅니다. 이전에는 이런 500이 Starlette
    기본 처리로 빠져서 (1) 응답 포맷이 맨 'Internal Server Error' 문자열이었고 (2)
    SecurityHeadersMiddleware가 실행되지 않아 CSP 등 보안 헤더가 통째로 빠졌습니다. 실제
    원인/스택트레이스는 서버 로그로만 남기고, 응답 본문에는 노출하지 않습니다."""
    resp = JSONResponse(
        status_code=500,
        content={"error": {"code": 500, "message": "서버 내부 오류가 발생했습니다."}},
    )
    for key, value in build_security_headers(is_https_request(request)).items():
        resp.headers[key] = value
    return resp


@app.get("/health", tags=["health"])
def health():
    """DB는 요청이 오면 바로 알 수 있지만, Redis는 안 쓰는 API도 있어서 여기서 미리 확인합니다.

    9/5 추가 — 업무경비 분류가 실제 Gemini를 부르는지, 규칙 기반 폴백으로 도는지도 같이
    돌려줍니다. 둘의 응답 모양이 같아서 밖에서는 구분할 방법이 없었는데, 키를 넣었는데도
    폴백으로 도는 상황(패키지 미설치, 키 오타 등)을 눈치채지 못하면 "AI 서비스"라고 해놓고
    규칙 엔진만 돌리는 셈이 됩니다.
    """
    expense_ai = "unknown"
    try:
        from data_pipeline import expense_classifier as ec

        if ec.genai is None:
            expense_ai = "fallback (google-genai 패키지 없음)"
        elif not os.environ.get("GEMINI_API_KEY"):
            expense_ai = "fallback (GEMINI_API_KEY 없음)"
        elif ec.MOCK_MODE:
            expense_ai = "fallback (모듈 로드 시점에 키가 없었음 — 재시작 필요)"
        else:
            expense_ai = "gemini"
    except Exception as exc:  # 진단이 헬스체크를 깨뜨리면 안 됩니다.
        expense_ai = "unknown (" + type(exc).__name__ + ")"

    return {
        "status": "healthy",
        "redis": "connected" if redis_ping() else "unreachable",
        "expense_ai": expense_ai,
    }


# ============================================================
# 데모 프론트엔드 서빙 (8/30)
#
# 별도의 프론트 서버 없이, 이 FastAPI 앱 하나가 API(/api/v1/*)와 데모 웹앱을 함께
# 서빙합니다 — 공모전 MVP가 "실행 가능한 배포 URL 하나"를 요구하기 때문에, 한 프로세스로
# 배포가 끝나는 게 가장 실수할 여지가 적습니다. 반드시 다른 라우터를 전부 등록한
# "다음"에 마운트해야 합니다 — StaticFiles(html=True)는 "/" 아래 모든 경로를 잡아버려서,
# 먼저 마운트하면 /api/v1/* 같은 API 라우트가 전부 가려집니다.
# ============================================================
_FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
if _FRONTEND_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(_FRONTEND_DIR), html=True), name="frontend")

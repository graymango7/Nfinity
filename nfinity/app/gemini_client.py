"""
Gemini 호출 공통 계층 (9/6 신규)

왜 만들었나
-----------
두 곳(업무경비 분류·AI 브리핑)이 각자 Gemini를 부르다 보니, 모델 하나가 죽으면 서비스 전체의
AI가 조용히 멈췄습니다. 실제로 두 번 겪었습니다.

1. `gemini-2.0-flash`가 서비스 종료되어 모든 호출이 404. 응답 모양이 규칙 폴백과 같아서
   화면상으로는 정상으로 보였고, /health도 "키가 있으니 gemini"라고만 보고했습니다.
2. 최신 모델로 바꾼 직후에는 503(high demand) — 일시적 용량 문제로 호출이 실패했습니다.

그래서 (a) 모델을 여러 개 순서대로 시도하고, (b) 마지막 호출이 성공했는지를 기록해서
/health가 사실대로 보고하게 합니다. 심사 기간에 특정 모델이 붐벼도 다음 모델로 넘어가고,
전부 실패하면 각 호출부가 가진 규칙/템플릿 폴백으로 내려갑니다.
"""
import json
import logging
import os
import time

logger = logging.getLogger("nfinity.gemini")

# 앞에서부터 시도합니다. 환경변수 GEMINI_MODEL을 주면 그 모델을 최우선으로 씁니다.
# 9/6 진단(/api/v1/validation/gemini)으로 확인한 것:
#   gemini-2.5-flash → 404 (이미 서비스 종료)  … 체인에서 제거
#   gemini-3.6-flash → 429 (무료 할당량 소진)
#   gemini-flash-latest → 503 (일시 과부하)
# 종료된 모델을 계속 시도하는 건 실패 시간만 늘리므로 빼고, 경량 모델을 뒤에 둡니다.
# 경량 모델은 할당량이 따로 잡히는 경우가 있어 주 모델이 429일 때 살아있을 수 있습니다.
# 9/6 2차 진단 결과: gemini-3.6-flash-lite도 404(존재하지 않는 이름)였고,
# gemini-flash-lite-latest만 정상 응답했습니다. 주 모델들이 할당량(429)에 걸린 동안에도
# 경량 모델은 살아있어서, 마지막 폴백으로 두면 AI 결과를 계속 보여줄 수 있습니다.
_DEFAULT_CHAIN = [
    "gemini-3.6-flash",
    "gemini-flash-latest",
    "gemini-flash-lite-latest",
]

# 마지막 호출 결과 — /health가 읽습니다.
LAST_CALL_OK = None      # None=아직 호출 전 / True / False
LAST_CALL_ERROR = ""
LAST_MODEL_USED = ""

# 차단기(circuit breaker). 한 번 실패하면 이 시각까지는 아예 시도하지 않습니다.
#
# 왜 필요한가: 업무경비 분류는 거래 한 건마다 이 함수를 부르고(화면 한 장에 최대 35건),
# 실패하면 모델 체인을 3개까지 순서대로 시도합니다. Gemini가 503을 내는 동안에는
# 화면 한 번 그리는 데 실패한 네트워크 왕복이 백 번 가까이 쌓여 응답이 수십 초로 늘어나고,
# 실패는 캐시되지 않아 새로고침해도 그대로 반복됩니다. 실제로 이 상태에서 Gig Score와
# 업무경비 카드가 "불러오는 중"에서 멈추는 것을 확인했습니다.
# 한 번 실패하면 잠시 규칙 기반으로만 답하고, 쿨다운이 지난 뒤 다시 시도합니다.
COOLDOWN_SECONDS = int(os.environ.get("GEMINI_COOLDOWN_SECONDS", "120"))
_cooldown_until = 0.0


class GeminiCoolingDown(RuntimeError):
    """직전 실패로 쿨다운 중이라 호출을 건너뛴 경우."""


def model_chain() -> list[str]:
    preferred = (os.environ.get("GEMINI_MODEL") or "").strip()
    chain = [m for m in _DEFAULT_CHAIN]
    if preferred:
        chain = [preferred] + [m for m in chain if m != preferred]
    return chain


def available() -> bool:
    """패키지와 키가 모두 있는지 (실제 호출이 되는지는 별개)."""
    if not os.environ.get("GEMINI_API_KEY"):
        return False
    try:
        from google import genai  # noqa: F401
    except ImportError:
        return False
    return True


def generate_json(system_instruction: str, contents: str, temperature: float = 0.0) -> dict:
    """JSON 응답을 요구하는 호출. 모델 체인을 순서대로 시도하고, 전부 실패하면 예외를 냅니다."""
    global LAST_CALL_OK, LAST_CALL_ERROR, LAST_MODEL_USED, _cooldown_until

    now = time.monotonic()
    if now < _cooldown_until:
        raise GeminiCoolingDown(
            "직전 호출 실패로 " + str(int(_cooldown_until - now)) + "초간 규칙 기반으로 동작합니다."
        )

    from google import genai
    from google.genai import types

    last_exc = None
    try:
        client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    except Exception as exc:  # 클라이언트 생성 실패도 "호출 실패"로 기록해야 health가 사실대로 보고합니다
        LAST_CALL_OK, LAST_CALL_ERROR, LAST_MODEL_USED = False, (type(exc).__name__ + ": " + str(exc))[:200], ""
        raise
    for model in model_chain():
        try:
            resp = client.models.generate_content(
                model=model,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    response_mime_type="application/json",
                    temperature=temperature,
                ),
            )
            parsed = json.loads(resp.text)
            LAST_CALL_OK, LAST_CALL_ERROR, LAST_MODEL_USED = True, "", model
            _cooldown_until = 0.0
            return parsed
        except Exception as exc:
            last_exc = exc
            logger.warning("[gemini] %s 호출 실패: %s", model, str(exc)[:160])

    LAST_CALL_OK = False
    LAST_CALL_ERROR = (type(last_exc).__name__ + ": " + str(last_exc))[:200] if last_exc else "unknown"
    LAST_MODEL_USED = ""
    _cooldown_until = time.monotonic() + COOLDOWN_SECONDS
    raise last_exc if last_exc else RuntimeError("Gemini 호출 실패")


def status() -> str:
    """/health용 한 줄 상태."""
    if not os.environ.get("GEMINI_API_KEY"):
        return "fallback (GEMINI_API_KEY 없음)"
    try:
        from google import genai  # noqa: F401
    except ImportError:
        return "fallback (google-genai 패키지 없음)"
    if LAST_CALL_OK is True:
        return "gemini (" + LAST_MODEL_USED + ")"
    if LAST_CALL_OK is False:
        remain = int(max(0.0, _cooldown_until - time.monotonic()))
        suffix = " · " + str(remain) + "초 후 재시도" if remain else ""
        return "fallback (호출 실패: " + LAST_CALL_ERROR[:80] + suffix + ")"
    return "gemini (대기 중 — 아직 호출 없음)"

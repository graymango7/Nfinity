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

logger = logging.getLogger("nfinity.gemini")

# 앞에서부터 시도합니다. 환경변수 GEMINI_MODEL을 주면 그 모델을 최우선으로 씁니다.
_DEFAULT_CHAIN = ["gemini-3.6-flash", "gemini-2.5-flash", "gemini-flash-latest"]

# 마지막 호출 결과 — /health가 읽습니다.
LAST_CALL_OK = None      # None=아직 호출 전 / True / False
LAST_CALL_ERROR = ""
LAST_MODEL_USED = ""


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
    global LAST_CALL_OK, LAST_CALL_ERROR, LAST_MODEL_USED

    from google import genai
    from google.genai import types

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    last_exc = None
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
            return parsed
        except Exception as exc:
            last_exc = exc
            logger.warning("[gemini] %s 호출 실패: %s", model, str(exc)[:160])

    LAST_CALL_OK = False
    LAST_CALL_ERROR = (type(last_exc).__name__ + ": " + str(last_exc))[:200] if last_exc else "unknown"
    LAST_MODEL_USED = ""
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
        return "fallback (호출 실패: " + LAST_CALL_ERROR[:90] + ")"
    return "gemini (대기 중 — 아직 호출 없음)"

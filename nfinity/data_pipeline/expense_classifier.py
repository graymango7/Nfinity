"""
expense_classifier.py
2026 금융 AI Challenge - SideGig AI (AI 경비분류 프롬프트 로직 — 조소현님 설계, 은겸 정리/연동)

역할: data_pipeline/categorizer.py(가맹점명 기반 카테고리 매핑)의 결과를 받아서,
'이 결제가 업무 경비로 인정될 확률(prob)'과 'AI 추천 태그(ai_tag)'를 붙이는 레이어.

★ API 계약 (필드명 절대 변경 금지 — 프론트 C(하은)님과 이미 이 모양으로 합의됨):
    {
      "transactions_queue": [
        {"id": 1, "merchant": "스타벅스", "amount": 15000, "ai_tag": "업무미팅", "prob": 92},
        ...
      ],
      "current_estimated_refund": 125000
    }

────────────────────────────────────────────────────────────────
8/28 은겸이 조소현님 원본 파일에서 고친 부분 (버그 수정 — 로직/프롬프트는 그대로 유지):

1. 🚨 (중요) call_openai() 안에 실제 API 키로 보이는 문자열이 따옴표 없이 코드에 그대로
   박혀 있었습니다: `os.environ.get(AQ.Ab8RN6...)` — 이건 "AQ.Ab8RN6..." 라는 존재하지
   않는 변수를 참조하는 문법 오류라 어차피 실행하면 NameError가 나지만, 진짜 API 키를
   실수로 코드에 붙여넣은 것처럼 보여서 위험합니다. 소현님께 꼭 확인해서, 만약 진짜 키라면
   지금 바로 재발급(rotate) 받으시길 권해드립니다 — 이 파일이 팀 zip으로 여러 번
   돌아다녔으니까요. 이 버전에서는 정상적인 `os.environ.get("GEMINI_API_KEY")` 형태로
   고쳤습니다.
2. 파일 위쪽에 MOCK_MODE가 두 번 정의되어 있었습니다 (7번째 줄 False, 46번째 줄 True).
   두 번째 값이 항상 이기기 때문에 실제로는 계속 Mock으로만 동작했을 거예요. 환경변수
   기준으로 자동 결정하도록 하나로 정리했습니다 (키가 있으면 실제 호출, 없으면 Mock).
3. run_batch()의 CSV 경로가 본인 컴퓨터 절대경로(`C:/Users/amro0/...`)로 하드코딩되어
   있어서 다른 사람 컴퓨터/이 서버에서는 실행이 안 됐습니다 → 프로젝트 기준 상대경로로 변경.
4. import가 중복되어 있던 것 정리(json/os/random/pandas 위에서 한 번 더 있었음).
5. 로직(프롬프트, few-shot 예시, 규칙 기반 폴백, 하이브리드 판단)은 전부 소현님이 짠 그대로
   유지했습니다 — 손댄 건 실행 환경 관련 버그뿐입니다.
────────────────────────────────────────────────────────────────

사용법:
  - MOCK_MODE: 환경변수 GEMINI_API_KEY가 없으면 자동으로 Mock(규칙 기반 흉내) 모드로 동작.
    인터넷이 안 되거나 키가 없는 환경에서도 파이프라인 전체가 도는지 바로 확인 가능.
  - 실제 Gemini API를 쓰려면: `pip install google-genai` 후 .env에 GEMINI_API_KEY=... 추가.
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

import pandas as pd

# google-genai 패키지가 없어도 이 파일을 import는 할 수 있게 (Mock 모드는 그대로 동작)
try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None  # type: ignore
    types = None  # type: ignore

# 키가 설정되어 있으면 실제 API, 없으면 Mock — 매번 코드를 고쳐가며 껐다 켤 필요 없게 자동화
# 9/6: gemini-2.0-flash가 서비스 종료되어 호출이 404로 실패하고 있었습니다. 응답 형태가
# 폴백과 같아서 밖에서는 알 수 없었고, /health도 "키가 있으니 gemini"라고만 보고했습니다.
# 모델명을 환경변수로 빼서 다음에 또 바뀌어도 코드 수정 없이 넘길 수 있게 합니다.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")

MOCK_MODE = genai is None or not os.environ.get("GEMINI_API_KEY")

# 마지막 실제 호출이 성공했는지 (None=아직 호출 안 함). /health가 이 값을 읽어서
# "키는 있는데 호출은 실패 중"인 상태를 드러냅니다.
LAST_CALL_OK = None

# 마진 세율 가정 (환급액 계산용 — 실제로는 페르소나별 과세표준에 맞게 조정 필요)
ASSUMED_MARGINAL_TAX_RATE = 0.15
# 이 확률 미만이면 '경비 승인'으로 자동 집계하지 않음 (환급액 계산 시 임계치)
APPROVAL_THRESHOLD = 50

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


# ==========================================================
# 1. 프롬프트 설계 (조소현님 원안 그대로)
# ==========================================================
SYSTEM_PROMPT = """당신은 한국 프리랜서/N잡러의 결제 내역을 보고, 해당 결제가
'업무상 필요 경비(종합소득세 신고 시 경비 인정 가능)'인지 판단하는 세무 보조 AI입니다.

- 유저의 직업, 결제 가맹점, 결제 시간대를 함께 고려해서 판단하세요.
  같은 가맹점이라도 직업/시간대에 따라 업무 경비일 확률이 달라집니다.
  예) 평일 낮 시간대 카페 결제 + 프리랜서(마케터/디자이너) → 미팅/작업 공간 이용 가능성 높음(90%대)
      IT 개발자가 소프트웨어/전자기기 판매처에서 결제 → 업무용 장비 가능성 높음(90%대 이상)
      심야 시간대의 유흥/사행성 업종, 직업과 무관한 고액 결제 → 개인 지출 가능성 높음(10% 이하)

반드시 아래 JSON 형식으로만 답하세요. 다른 설명은 절대 추가하지 마세요.
{"ai_tag": "<10자 이내 짧은 태그, 예: 업무미팅, SW구독, 개인지출>", "prob": <0~100 사이 정수>}
"""

FEW_SHOT_EXAMPLES = [
    {
        "input": {"직업": "프리랜서_마케터", "가맹점": "스타벅스", "시간대": "평일 오후 2시", "금액": 15000},
        "output": {"ai_tag": "업무미팅", "prob": 92},
    },
    {
        "input": {"직업": "IT_개발자", "가맹점": "프리스비", "시간대": "평일 오전", "금액": 3000000},
        "output": {"ai_tag": "업무용장비", "prob": 95},
    },
    {
        "input": {"직업": "IT_개발자", "가맹점": "Adobe Systems", "시간대": "평일 오전", "금액": 62000},
        "output": {"ai_tag": "SW구독", "prob": 98},
    },
    {
        "input": {"직업": "배달_투잡러", "가맹점": "유흥업소", "시간대": "심야 3시", "금액": 4500000},
        "output": {"ai_tag": "개인지출", "prob": 3},
    },
]


def build_user_prompt(job: str, merchant: str, time_period: str, amount: int) -> str:
    """기획서에 나온 형태 그대로: {"직업":..., "가맹점":..., "시간대":...} 프롬프트 구성"""
    payload = {"직업": job, "가맹점": merchant, "시간대": time_period, "금액": amount}
    return json.dumps(payload, ensure_ascii=False)


def time_period_label(hour: int) -> str:
    if 0 <= hour < 6:
        return "심야"
    if 6 <= hour < 12:
        return "평일 오전"
    if 12 <= hour < 18:
        return "평일 오후"
    return "저녁"


# ==========================================================
# 2. Gemini API 호출 (실제 모드)
# ==========================================================
def call_gemini(job: str, merchant: str, time_period: str, amount: int) -> dict:
    """실제 Gemini API 호출. GEMINI_API_KEY 환경변수가 있을 때만 사용됩니다."""
    try:
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise ValueError("환경변수에 GEMINI_API_KEY가 설정되어 있지 않습니다.")

        full_system_instruction = SYSTEM_PROMPT + "\n\n[참고 예시]\n"
        for ex in FEW_SHOT_EXAMPLES:
            full_system_instruction += f"입력: {json.dumps(ex['input'], ensure_ascii=False)} / 출력: {json.dumps(ex['output'], ensure_ascii=False)}\n"

        user_content = build_user_prompt(job, merchant, time_period, amount)

        from app.gemini_client import generate_json

        result = generate_json(full_system_instruction, user_content, temperature=0)
        globals()["LAST_CALL_OK"] = True
        return {"ai_tag": result["ai_tag"], "prob": int(result["prob"])}

    except Exception as e:
        globals()["LAST_CALL_OK"] = False
        globals()["LAST_CALL_ERROR"] = f"{type(e).__name__}: {e}"[:200]
        print(f"⚠️ Gemini API 호출 실패({e}) → 규칙 기반 폴백 사용")
        return rule_based_fallback(job, merchant)


# ==========================================================
# 3. 규칙 기반 폴백 / Mock 모드 (API 없이 로직 검증용)
# ==========================================================
BUSINESS_MERCHANTS = {"어도비(Adobe)": 98, "AWS": 97, "노션": 95, "패스트파이브": 90,
                       "Adobe Systems": 98}
JOB_CONTEXT_MERCHANTS = {"스타벅스": ("업무미팅", 85), "맥도날드": ("업무미팅", 55),
                          "프리스비": ("업무용장비", 92), "쿠팡": ("업무용구매", 45)}
PERSONAL_MERCHANTS = {"금은방_귀금속": 2, "중고거래_송금": 5, "유흥업소": 3,
                       "Amazon_US": 20, "AliExpress_CN": 15, "Steam_Global": 8,
                       "게임아이템_스토어": 5, "올리브영": 10, "무신사": 12}


def rule_based_fallback(job: str, merchant: str) -> dict:
    """LLM 호출 없이/실패 시 대략적인 확률을 매기는 안전망.
    실제 LLM 판단보다 거칠지만, 데모 중 API 장애로 전체가 멎는 것보다 낫습니다."""
    if merchant in BUSINESS_MERCHANTS:
        return {"ai_tag": "업무용구독/장비", "prob": BUSINESS_MERCHANTS[merchant]}
    if merchant in PERSONAL_MERCHANTS:
        return {"ai_tag": "개인지출", "prob": PERSONAL_MERCHANTS[merchant]}
    if merchant in JOB_CONTEXT_MERCHANTS:
        tag, prob = JOB_CONTEXT_MERCHANTS[merchant]
        if "개발자" in job or "마케터" in job:
            return {"ai_tag": tag, "prob": prob}
        return {"ai_tag": tag, "prob": max(prob - 30, 10)}
    return {"ai_tag": "미분류", "prob": 30}


def mock_llm_classify(job: str, merchant: str, time_period: str, amount: int) -> dict:
    """MOCK_MODE용: 실제 LLM 호출 대신 few-shot 예시의 패턴을 흉내낸 결정론적 함수.
    GEMINI_API_KEY를 설정하면 call_gemini()가 대신 실행됩니다."""
    base = rule_based_fallback(job, merchant)
    # 시간대 컨텍스트 살짝 반영 (기획서 예시처럼 '평일 낮'이면 가산)
    if time_period in ("평일 오전", "평일 오후") and base["prob"] not in (2, 3, 5, 8):
        base = {**base, "prob": min(base["prob"] + 5, 99)}
    if time_period == "심야" and merchant not in BUSINESS_MERCHANTS:
        base = {**base, "prob": max(base["prob"] - 20, 1)}
    return base


# ==========================================================
# 4. 하이브리드 판단: 이미 확실한 건 LLM 호출 스킵 (비용/속도 절감)
# ==========================================================
HIGH_CONFIDENCE_BUSINESS = {"어도비(Adobe)", "AWS", "노션", "패스트파이브", "Adobe Systems"}
HIGH_CONFIDENCE_PERSONAL = {"금은방_귀금속", "중고거래_송금", "유흥업소", "게임아이템_스토어"}


def classify_one(job: str, merchant: str, hour: int, amount: int) -> dict:
    time_period = time_period_label(hour)

    # 이미 카테고리가 명백한 건 LLM 호출 없이 즉시 반환 (비용 절감)
    if merchant in HIGH_CONFIDENCE_BUSINESS:
        return rule_based_fallback(job, merchant)
    if merchant in HIGH_CONFIDENCE_PERSONAL:
        return rule_based_fallback(job, merchant)

    # 애매한 케이스만 LLM(or mock)로 판단
    if MOCK_MODE:
        return mock_llm_classify(job, merchant, time_period, amount)
    return dict(_call_gemini_cached(job, merchant, time_period, int(amount)))


def _redis_key(job: str, merchant: str, time_period: str, amount: int) -> str:
    # 금액은 만원 단위로 뭉쳐서 캐시 적중률을 높입니다(같은 가맹점·같은 시간대면 금액이
    # 조금 달라도 업무경비 여부 판단은 거의 같습니다).
    return f"exp:{job}:{merchant}:{time_period}:{amount // 10000}"


def _cache_get(key: str):
    try:
        from app.redis_client import get_redis_client

        raw = get_redis_client().get(key)
        return json.loads(raw) if raw else None
    except Exception:
        return None


def _cache_set(key: str, value: dict) -> None:
    try:
        from app.redis_client import get_redis_client

        # 만료를 두지 않습니다 — 한 번 분류한 결과는 계속 재사용해서, 할당량이 소진되거나
        # 서버가 재시작돼도 화면에는 계속 실제 LLM 결과가 보이게 합니다.
        get_redis_client().set(key, json.dumps(value, ensure_ascii=False))
    except Exception:
        pass


@lru_cache(maxsize=512)
def _call_gemini_cached(job: str, merchant: str, time_period: str, amount: int) -> tuple:
    """같은 조건이면 같은 답이 나오므로 결과를 캐싱합니다. (9/5 추가)

    화면 한 번을 그리는 데 이 함수가 여러 번 불립니다 — 업무경비 분류가 최근 20건,
    Gig Score가 별도로 15건을 분류하기 때문입니다. 거래 목록에는 같은 가맹점이 반복해서
    나오는데(구독·단골 카페 등) 캐시가 없으면 그때마다 Gemini를 새로 호출해서, 실제 키를
    넣는 순간 대시보드 한 장에 수십 번의 LLM 왕복이 생깁니다.

    dict는 캐시가 불가능해서(해시 불가) 항목 튜플로 저장했다가 호출부에서 다시 dict로
    만듭니다. 프로세스 메모리에만 남고 재시작하면 비워집니다.
    """
    # 프로세스 캐시(lru_cache)는 재시작하면 사라지므로, 그 앞에 Redis 영구 캐시를 둡니다.
    key = _redis_key(job, merchant, time_period, amount)
    cached = _cache_get(key)
    if cached:
        return tuple(cached.items())

    result = call_gemini(job, merchant, time_period, amount)
    # 규칙 폴백 결과는 저장하지 않습니다 — 나중에 할당량이 회복됐을 때 실제 LLM 결과로
    # 채워질 수 있어야 하기 때문입니다.
    from app.gemini_client import LAST_CALL_OK

    if LAST_CALL_OK:
        _cache_set(key, result)
    return tuple(result.items())


# ==========================================================
# 5. 배치 실행: mock_transactions.csv → API Contract 형태로 출력 (CSV 기준 — 로컬 테스트용)
# ==========================================================
def run_batch(transactions_csv: str | None = None, persona_csv: str | None = None,
              sample_size=15, marginal_tax_rate=ASSUMED_MARGINAL_TAX_RATE) -> dict:
    transactions_csv = transactions_csv or str(DATA_DIR / "mock_transactions.csv")
    persona_csv = persona_csv or str(DATA_DIR / "persona_map.csv")

    df = pd.read_csv(transactions_csv)
    persona = pd.read_csv(persona_csv)[["user_id", "job"]]
    df = df.merge(persona, on="user_id", how="left")
    df["timestamp"] = pd.to_datetime(df["timestamp"])

    # 데모/검증용으로 일부만 샘플링 (전체 4,978건을 다 부르면 비용·시간 부담)
    sample = df.sample(n=min(sample_size, len(df)), random_state=42).reset_index(drop=True)

    return _build_queue_response(
        (row["job"], row["merchant_name"], row["timestamp"].hour, int(row["amount"]))
        for _, row in sample.iterrows()
    )


def _build_queue_response(rows) -> dict:
    """(job, merchant, hour, amount) 튜플들을 받아 API 계약 모양으로 조립하는 공통 로직.
    run_batch()(CSV 기준)와 app/routers/expense.py(실제 DB 기준)가 같이 씁니다."""
    queue = []
    total_refund = 0
    for i, (job, merchant, hour, amount) in enumerate(rows):
        result = classify_one(job, merchant, hour, amount)
        queue.append({
            "id": i + 1,
            "merchant": merchant,
            "amount": amount,
            "ai_tag": result["ai_tag"],
            "prob": result["prob"],
        })
        if result["prob"] >= APPROVAL_THRESHOLD:
            total_refund += int(amount * ASSUMED_MARGINAL_TAX_RATE)
    return {"transactions_queue": queue, "current_estimated_refund": total_refund}


if __name__ == "__main__":
    output = run_batch()
    print(f"MOCK_MODE = {MOCK_MODE}  (실제 API 붙이려면 .env에 GEMINI_API_KEY 추가)")
    print(json.dumps(output, ensure_ascii=False, indent=2))

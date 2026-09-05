"""
AI 브리핑 API (9/6 신규) — GET /api/v1/brief/{user_id}

무엇을 하나
-----------
이 사용자의 현금흐름·세금·예산 계산 결과를 한 문단으로 풀어 설명하고, 지금 할 일을 한 줄로
제안합니다. 화면 맨 위에서 "그래서 내 상황이 어떻다는 건데?"에 답하는 자리입니다.

왜 생성형 AI인가
----------------
숫자는 이미 규칙과 통계로 정확히 계산됩니다(app/cashflow.py, app/tax.py). 문제는 그 숫자들이
서로 얽혀 있을 때 사람이 인과를 읽어내기 어렵다는 것입니다 — "잔고는 145만원인데 세금·건보료
빼면 107만원이고, 다음 정산은 8일 뒤인데 그 사이 고정비가 나가서 9월 26일에 바닥난다"를
스스로 이어붙여야 합니다. 이 연결을 문장으로 만들어주는 게 생성형 모델이 실제로 잘하는 일이라
여기에 씁니다.

환각 방지
---------
모델에는 **이미 계산이 끝난 수치만** 넘기고, 새로운 숫자를 만들어내거나 조언을 지어내지 말라고
지시합니다. 호출이 실패하거나 키가 없으면 같은 수치로 만든 템플릿 문장으로 조용히 대체해서,
이 기능 때문에 화면이 비는 일이 없게 합니다.
"""
import json
import logging
import os
from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.cashflow import compute_shield
from app.database import get_db
from app.security import verify_api_key

logger = logging.getLogger("nfinity.brief")
router = APIRouter(prefix="/api/v1/brief", tags=["brief"], dependencies=[Depends(verify_api_key)])

SYSTEM_PROMPT = """너는 N잡러(여러 플랫폼에서 수입을 얻는 사람)의 자금 상황을 설명해주는 금융 비서다.
주어진 수치만 사용해 상황을 설명하고, 지금 할 일을 하나만 제안한다.

규칙:
- 주어지지 않은 숫자나 사실을 절대 만들어내지 마라.
- 2~3문장, 존댓말, 담백하게. 과장·불안 조성 금지.
- 금액은 만원 단위로 반올림해 읽기 쉽게 (예: 145만원).
- 확정된 세액·보험료가 아니라 추정치라는 점을 필요할 때만 짧게 덧붙인다.
- 출력은 JSON: {"summary": "...", "action": "..."} (action은 25자 이내 한 줄 권고)"""


def _facts(db: Session, user_id: str) -> tuple[dict, dict]:
    shield = compute_shield(db, user_id)
    if shield is None:
        raise HTTPException(status_code=404, detail="아직 계산할 수입·지출 데이터가 없습니다.")

    facts = {
        "현재잔액": shield["current_balance"],
        "세금건보료_제외_가용액": shield["available_cash"],
        "최소안전잔액": shield["min_safety_balance"],
        "잔고부족_예상일": shield["minimum_balance_date"],
        "현금흐름_위험도": shield["risk_level"],
        "위험사유": shield["risk_reasons"],
        "수입_변동계수": shield["income_variability"],
    }
    try:
        from app.routers.tax import estimate as tax_estimate

        tax = tax_estimate(user_id, db)
        it, hi = tax["income_tax"], tax["health_insurance"]
        facts["연_사업소득_환산"] = tax["annual_business_income"]
        facts["5월_종합소득세_정산액"] = it["balance_due"]  # 음수면 환급
        facts["건강보험료_월"] = hi["total_monthly"]
        facts["건강보험_가입자유형"] = hi["subscriber_type"]
        if hi.get("warning"):
            facts["건강보험_경고"] = hi["warning"]
    except HTTPException:
        pass
    except Exception:
        logger.exception("[brief] 세금 추정 조회 실패")
    return facts, shield


def _fallback(facts: dict) -> dict:
    """키가 없거나 호출이 실패했을 때 같은 수치로 만드는 템플릿 문장."""
    man = lambda v: format(int(round((v or 0) / 10000)), ",") + "만원"
    short = facts.get("잔고부족_예상일")
    if short:
        summary = (
            "현재 잔액 " + man(facts.get("현재잔액")) + " 중 세금·건보료를 빼면 "
            + man(facts.get("세금건보료_제외_가용액")) + "을 쓸 수 있고, 이대로면 "
            + str(short) + "에 최소 안전잔액 아래로 내려갑니다."
        )
        action = "정산 예정 수입 확인하기"
    else:
        summary = (
            "현재 잔액 " + man(facts.get("현재잔액")) + " 기준으로 앞으로 45일간 잔고가 "
            "최소 안전잔액 아래로 내려갈 것으로 보이지는 않습니다."
        )
        action = "지금 상태 유지하기"
    tax = facts.get("5월_종합소득세_정산액")
    if isinstance(tax, (int, float)) and tax < 0:
        summary += " 5월 종합소득세는 " + man(-tax) + " 환급이 예상됩니다."
    return {"summary": summary, "action": action, "source": "template"}


@lru_cache(maxsize=64)
def _call_gemini_cached(facts_json: str) -> str:
    """같은 수치면 같은 문장이 나오므로 캐싱합니다(화면 재방문 때마다 호출하지 않도록)."""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    resp = client.models.generate_content(
        model=os.environ.get("GEMINI_MODEL", "gemini-3.6-flash"),
        contents="다음은 이미 계산이 끝난 이 사용자의 수치다:\n" + facts_json,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            temperature=0.2,
        ),
    )
    return resp.text


@router.get("/{user_id}")
def get_brief(user_id: str, debug: int = 0, db: Session = Depends(get_db)):
    facts, shield = _facts(db, user_id)
    facts_json = json.dumps(facts, ensure_ascii=False, sort_keys=True)

    if not os.environ.get("GEMINI_API_KEY"):
        result = _fallback(facts)
    else:
        try:
            parsed = json.loads(_call_gemini_cached(facts_json))
            result = {
                "summary": str(parsed.get("summary", ""))[:400],
                "action": str(parsed.get("action", ""))[:60],
                "source": "gemini",
            }
            if not result["summary"]:
                result = _fallback(facts)
        except Exception as exc:
            logger.exception("[brief] Gemini 호출 실패 → 템플릿 문장으로 대체")
            result = _fallback(facts)
            if debug:  # 배포 환경에서 실패 원인을 확인하기 위한 임시 진단
                result["error"] = type(exc).__name__ + ": " + str(exc)[:200]

    result["disclaimer"] = "계산된 추정치를 바탕으로 작성된 안내이며, 확정 세액·보험료가 아닙니다."
    return result

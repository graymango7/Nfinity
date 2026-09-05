"""
대응 가이드 API (9/6 신규) — GET /api/v1/guidance/{user_id}

무엇을 하나
-----------
계산된 현금흐름·세금 상태를 보고 "지금 이 사람이 고려할 만한 선택지"를 근거와 함께 제시합니다.
잔고가 부족할 것으로 보이면 부족을 메우는 순서를, 여유가 있으면 먼저 떼어둘 돈을 안내합니다.

왜 '상품 추천'이 아니라 '선택지 안내'인가
------------------------------------------
처음 검토한 형태는 "여유자금이면 투자상품, 부족하면 대출 상품을 추천"이었는데, 그대로 만들지
않았습니다.

- 자본시장법상 투자권유는 등록된 금융투자업자만 할 수 있고, 대출 모집도 대출모집인 등록이
  필요합니다. 미등록 서비스가 특정 상품을 권유하는 형태는 위법 소지가 있습니다.
- 무엇보다 잔고가 부족할 것으로 예측된 사용자에게 곧바로 고금리 신용대출을 권하는 흐름은
  이용자에게 해가 될 수 있습니다. 금융소비자보호 관점에서 이 서비스가 취해야 할 태도는
  "돈을 빌리라"가 아니라 "무엇부터 확인하고, 어떤 순서로 대응할 수 있는지"입니다.

그래서 이 API는 **특정 금융상품·금융회사를 지목하지 않습니다.** 대신
(1) 지금 상태가 왜 그런지 계산 근거를 들고,
(2) 비용이 낮은 선택지부터 순서대로 놓고,
(3) 각 선택지의 주의점을 함께 적습니다.
대출 같은 선택지도 숨기지 않되 마지막 순서에 두고, 조건을 반드시 확인하라는 주의를 붙입니다.

이 구성은 "맞춤형 금융 서비스"라는 주제에 부합하면서도, 미등록 권유·부채 유도라는 위험을
만들지 않습니다.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.cashflow import compute_shield
from app.database import get_db
from app.security import verify_api_key

router = APIRouter(prefix="/api/v1/guidance", tags=["guidance"], dependencies=[Depends(verify_api_key)])

DISCLAIMER = (
    "특정 금융상품이나 금융회사를 권유하지 않으며, 투자권유·대출모집에 해당하지 않습니다. "
    "계산된 추정치를 바탕으로 한 일반적인 정보 안내이며, 실제 결정 전 해당 기관에 조건을 확인하세요."
)


def _man(v) -> str:
    return format(int(round((v or 0) / 10000)), ",") + "만원"


@router.get("/{user_id}")
def get_guidance(user_id: str, db: Session = Depends(get_db)):
    shield = compute_shield(db, user_id)
    if shield is None:
        raise HTTPException(status_code=404, detail="아직 계산할 수입·지출 데이터가 없습니다.")

    tax = None
    try:
        from app.routers.tax import estimate as tax_estimate

        tax = tax_estimate(user_id, db)
    except Exception:
        tax = None

    shortage_date = shield.get("minimum_balance_date")
    available = shield.get("available_cash") or 0
    reserve = shield.get("recommended_reserve") or 0
    items: list[dict] = []

    if shortage_date:
        # 부족이 예상되는 경우 — 비용이 낮은 순서로 배치합니다.
        items.append(
            {
                "tone": "warn",
                "title": "정산 예정일을 먼저 확인하세요",
                "why": shortage_date + "에 잔고가 최소 안전잔액 아래로 내려갈 것으로 계산됩니다.",
                "options": [
                    "연결한 플랫폼의 다음 정산 예정일과 금액을 확인하고, 부족 시점보다 앞선 정산이 있는지 봅니다.",
                    "플랫폼에 따라 정산 주기를 앞당기거나 조기 정산을 신청할 수 있는 경우가 있습니다.",
                    "아직 연결하지 않은 수입원이 있다면 연결해 예측에 반영합니다.",
                ],
                "caution": "조기 정산은 수수료가 붙는 경우가 있으니 조건을 먼저 확인하세요.",
            }
        )
        items.append(
            {
                "tone": "warn",
                "title": "고정 지출 시점을 옮길 수 있는지 봅니다",
                "why": "부족은 대개 '수입이 오기 전에 고정비가 먼저 빠져나가서' 생깁니다.",
                "options": [
                    "구독·보험료 등 자동이체 날짜를 정산일 이후로 옮기면 부족 구간을 피할 수 있습니다.",
                    "이번 달 예산 초과 카테고리의 지출을 정산일까지 미룹니다.",
                ],
                "caution": "이체일 변경은 연체로 처리되지 않도록 기관에 미리 신청해야 합니다.",
            }
        )
        items.append(
            {
                "tone": "neutral",
                "title": "그래도 모자란다면",
                "why": "위 방법으로 메워지지 않는 금액이 남는 경우에 한해 고려할 수 있는 선택지입니다.",
                "options": [
                    "마이너스 통장·비상금 등 이미 보유한 여유 한도를 먼저 씁니다.",
                    "정책 지원 제도(고용보험 가입 특수형태근로종사자 대상 지원, 소상공인 정책자금 등)에 "
                    "해당되는지 확인합니다.",
                    "신용대출·카드론은 금리와 상환 부담이 크므로 마지막 순서로 두고, 반드시 총 상환액을 "
                    "계산한 뒤 결정합니다.",
                ],
                "caution": "이 서비스는 특정 상품을 권유하지 않습니다. 조건·금리는 해당 금융회사에서 직접 확인하세요.",
            }
        )
    else:
        items.append(
            {
                "tone": "safe",
                "title": "먼저 세금·건강보험료로 떼어둘 돈을 확보하세요",
                "why": "앞으로 45일간 잔고 부족은 예상되지 않습니다. 지금 여유는 "
                + _man(available)
                + "이고, 권장 준비금은 "
                + _man(reserve)
                + "입니다.",
                "options": [
                    "권장 준비금만큼을 생활비 계좌와 분리해 따로 둡니다.",
                    "5월 종합소득세와 매달 나가는 건강보험료는 시차를 두고 청구되므로, 미리 떼어두면 "
                    "그 시점의 현금흐름 충격을 줄일 수 있습니다.",
                ],
                "caution": "표시된 세액·보험료는 확정 금액이 아니라 추정치입니다.",
            }
        )
        items.append(
            {
                "tone": "neutral",
                "title": "그 다음 여유분의 운용",
                "why": "준비금을 떼고도 남는 금액이 있다면 고려할 수 있습니다.",
                "options": [
                    "수입이 불규칙한 만큼, 먼저 몇 달치 고정비에 해당하는 비상금을 확보하는 것이 일반적인 순서입니다.",
                    "그 이후의 자금 운용은 본인의 위험 성향과 자금 사용 시점에 따라 달라지므로, "
                    "금융회사나 자격을 갖춘 전문가와 상담해 결정하세요.",
                ],
                "caution": "이 서비스는 투자상품을 추천하거나 권유하지 않습니다.",
            }
        )

    if tax:
        hi = tax.get("health_insurance") or {}
        if hi.get("warning"):
            items.append(
                {
                    "tone": "warn",
                    "title": "건강보험료 기준선을 확인하세요",
                    "why": hi["warning"],
                    "options": [
                        "연말까지의 예상 소득을 확인해 기준을 넘는 시점을 미리 파악합니다.",
                        "넘게 되는 경우 보험료가 새로 생기거나 늘어나므로, 그만큼을 미리 준비금에 반영합니다.",
                    ],
                    "caution": "실제 부과액은 소득 외 재산 등도 반영되어 달라질 수 있습니다.",
                }
            )
        it = tax.get("income_tax") or {}
        if isinstance(it.get("balance_due"), (int, float)) and it["balance_due"] < 0:
            items.append(
                {
                    "tone": "safe",
                    "title": "5월에 돌려받을 세금이 있습니다",
                    "why": "원천징수된 3.3%가 추정 세액보다 많아 " + _man(-it["balance_due"]) + " 환급이 예상됩니다.",
                    "options": [
                        "종합소득세 신고를 해야 환급이 이뤄집니다. 신고하지 않으면 돌려받지 못합니다.",
                        "업무 관련 지출 증빙을 모아두면 필요경비로 인정받아 환급액이 달라질 수 있습니다.",
                    ],
                    "caution": "추정치이며, 실제 환급액은 공제 항목에 따라 달라집니다.",
                }
            )

    return {"user_id": user_id, "items": items, "disclaimer": DISCLAIMER}

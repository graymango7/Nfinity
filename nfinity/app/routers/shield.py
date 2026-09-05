"""
Risk Shield / Dynamic Parking API (9/2 세 번째 업데이트)

기획서 4번의 대표 차별화 기능인 "Risk Shield"(일별 예상 잔액·최저잔액·현금 부족
예상일)와 "Dynamic Parking"(세금·건보료 준비 권장액)을 실제로 구현합니다.

- GET  /api/v1/shield/{user_id}            : 현재 상태 + 45일 시뮬레이션
- GET  /api/v1/shield/{user_id}/scenarios  : 기본/입금지연/수입감소 3개 시나리오
- GET  /api/v1/shield/{user_id}/settings   : 최소 안전잔액 조회
- PUT  /api/v1/shield/{user_id}/settings   : 최소 안전잔액 설정(온보딩)

이름이 비슷해서 헷갈릴 수 있는데, `/api/v1/risk`(app/routers/risk.py)는 카드
이상거래/사기 탐지 룰엔진이고, 이 파일의 `/api/v1/shield`는 완전히 다른 개념인
"현금흐름이 바닥나기 전에 미리 경고하는" 기능입니다. 계산 로직은 app/cashflow.py에
전부 있고, 이 라우터는 얇게 그 위에 얹은 API 계층입니다.
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.cashflow import DEFAULT_MIN_SAFETY_BALANCE, compute_scenarios, compute_shield
from app.database import get_db
from app.models import SafetyBalanceResponse, SafetyBalanceUpdate, ScenarioResponse, ShieldResponse
from app.security import verify_api_key

router = APIRouter(prefix="/api/v1/shield", tags=["shield"], dependencies=[Depends(verify_api_key)])


@router.get("/{user_id}", response_model=ShieldResponse)
def get_shield(user_id: str, db: Session = Depends(get_db)):
    result = compute_shield(db, user_id)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail="아직 수입/지출 데이터가 없어서 Risk Shield를 계산할 수 없습니다.",
        )
    return result


@router.get("/{user_id}/scenarios", response_model=ScenarioResponse)
def get_scenarios(user_id: str, db: Session = Depends(get_db)):
    result = compute_scenarios(db, user_id)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail="아직 수입/지출 데이터가 없어서 시나리오를 계산할 수 없습니다.",
        )
    return result


@router.get("/{user_id}/settings", response_model=SafetyBalanceResponse)
def get_settings(user_id: str, db: Session = Depends(get_db)):
    row = db.execute(
        text("SELECT min_safety_balance FROM user_settings WHERE user_id = :uid"),
        {"uid": user_id},
    ).mappings().first()
    if row and row["min_safety_balance"] is not None:
        return {"user_id": user_id, "min_safety_balance": float(row["min_safety_balance"]), "is_default": False}
    return {"user_id": user_id, "min_safety_balance": float(DEFAULT_MIN_SAFETY_BALANCE), "is_default": True}


@router.put("/{user_id}/settings", response_model=SafetyBalanceResponse)
def update_settings(user_id: str, body: SafetyBalanceUpdate, db: Session = Depends(get_db)):
    exists = db.execute(text("SELECT 1 FROM users WHERE user_id = :uid"), {"uid": user_id}).first()
    if not exists:
        raise HTTPException(status_code=404, detail="존재하지 않는 user_id 입니다.")
    db.execute(
        text(
            "INSERT INTO user_settings (user_id, min_safety_balance, updated_at) "
            "VALUES (:uid, :bal, :now) "
            "ON CONFLICT (user_id) DO UPDATE SET min_safety_balance = :bal, updated_at = :now"
        ),
        {"uid": user_id, "bal": body.min_safety_balance, "now": datetime.utcnow()},
    )
    db.commit()
    return {"user_id": user_id, "min_safety_balance": body.min_safety_balance, "is_default": False}

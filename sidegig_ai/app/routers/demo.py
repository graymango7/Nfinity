"""
데모 페르소나 목록 API (8/30 추가)

- GET /api/v1/demo/personas

프론트(데모 페르소나 선택 화면)가 이 API 하나로 5명의 카드 정보를 그립니다.
목록의 실제 값은 app/demo_personas.py 한 곳에서만 관리합니다.
"""
from fastapi import APIRouter, Depends

from app.demo_personas import DEMO_PERSONAS
from app.security import verify_api_key

router = APIRouter(prefix="/api/v1/demo", tags=["demo"], dependencies=[Depends(verify_api_key)])


@router.get("/personas")
def list_demo_personas():
    return {
        "note": "실제 인물이 아닌, 시연을 위해 준비한 가상 데이터입니다.",
        "personas": [
            {
                "user_id": p["user_id"],
                "name": p["name"],
                "job": p["job"],
                "tagline": p["tagline"],
                "avatar_emoji": p["avatar_emoji"],
            }
            for p in DEMO_PERSONAS
        ],
    }

"""
API 접근 제어 (8/30 보안 점검 추가)

기존엔 어떤 인증도 없어서, 배포된 API 주소만 알면 누구나 user_id를 바꿔가며
다른 사람의 예산/리스크/절세장부 데이터를 조회할 수 있었습니다 (금융 데이터를
다루는 앱에서는 치명적인 문제).

풀 로그인(회원가입/세션) 시스템은 지금 남은 일정에 비해 과합니다. 대신
"이 API를 호출해도 되는 클라이언트인지"만 최소한으로 확인하는 공유 키(shared
secret) 방식을 넣었습니다:

- .env에 API_ACCESS_KEY가 설정되어 있으면: 모든 /api/v1/* 요청에 그 값과 정확히
  일치하는 `X-API-Key` 헤더가 있어야 합니다. 프론트(하은/소현 쪽 코드)는 API를
  부를 때마다 이 헤더를 같이 보내야 합니다.
- .env에 API_ACCESS_KEY가 없으면(로컬 개발 중 기본값): 인증을 요구하지 않습니다.
  로컬에서 Swagger UI(/docs)로 바로 테스트할 수 있게 하기 위함입니다.

★ 깃허브에 배포할 때는 반드시 API_ACCESS_KEY를 설정한 값으로 .env를 채워서
배포하세요 (README 6단계 참고). 이 값 자체도 비밀번호와 같으니 절대 코드에
하드코딩하거나 커밋하지 마세요.

이것으로 "다른 사람인 척 아무 user_id나 넣어 조회"까지 막지는 못합니다(그러려면
로그인/세션이 필요합니다) — 최소한 "API 주소를 아는 아무나"가 못 들어오게는
막아줍니다. 진짜 로그인 붙이기 전까지의 임시 방어선이라는 점을 README에도
분명히 적어뒀습니다.
"""
import os
import secrets

from fastapi import Header, HTTPException

API_ACCESS_KEY = os.environ.get("API_ACCESS_KEY", "").strip()


def verify_api_key(x_api_key: str = Header(default=None)):
    """API_ACCESS_KEY가 설정된 배포 환경에서만 강제되는 최소한의 접근 제어.
    라우터에 `dependencies=[Depends(verify_api_key)]` 로 붙여서 씁니다."""
    if not API_ACCESS_KEY:
        # 로컬 개발 기본값: 키를 안 정해뒀으면 막지 않음 (Swagger UI 테스트 편의)
        return
    if not x_api_key or not secrets.compare_digest(x_api_key, API_ACCESS_KEY):
        raise HTTPException(
            status_code=401,
            detail="유효한 X-API-Key 헤더가 필요합니다.",
        )

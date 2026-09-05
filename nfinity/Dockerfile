FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 8/30 수정: data_pipeline(카테고리 분류/절세장부 분류/정기결제 감지 로직),
# models(학습된 Isolation Forest 모델 .pkl), frontend(데모 화면)가 실제로는
# app/ 코드에서 import/서빙되는데 이전엔 COPY가 안 돼 있어서, 이 이미지로
# 배포하면 바로 ImportError로 죽는 상태였습니다.
#
# 9/2 수정: data/·scripts/·sql/은 "로컬 배치테스트/개발 전용"이라며 이미지에서 뺐었는데,
# 그 판단이 실제로는 배포 버그였습니다 — 관리형 Postgres(Railway/Render 등)에 새로
# 배포하면 스키마도 데모 데이터도 전혀 없는 빈 DB로 뜨는데, 그걸 채워주는 스크립트들이
# 전부 이 세 폴더 안에 있어서 이미지 안에서 실행할 방법이 없었습니다. app/startup_seed.py가
# 앱 시작 시 이 파일들을 이용해 스키마 생성 + 데모 데이터 적재를 자동으로 하므로, 용량이
# 작은(전부 합쳐 1MB 미만) 이 세 폴더는 이제 반드시 이미지에 포함해야 합니다.
COPY app ./app
COPY data_pipeline ./data_pipeline
COPY models ./models
COPY frontend ./frontend
COPY data ./data
COPY scripts ./scripts
COPY sql ./sql

# 9/2 세 번째 업데이트(보안 강화): 지금까지는 컨테이너 안에서 root로 앱이 돌고
# 있었습니다 — 만약 앱 코드에 원격 코드 실행(RCE)으로 이어지는 취약점이 있다면,
# root 권한 그대로 컨테이너 안의 모든 파일을 건드릴 수 있다는 뜻입니다. 이 앱은
# 런타임에 /app 아래 아무 파일도 쓰지 않으므로(모델 학습(app/anomaly_model.py의
# train())은 개발 중 수동으로만 돌리는 오프라인 스크립트라 컨테이너 안에서 실행되지
# 않음, DB/Redis 쓰기는 전부 외부 서비스로 나감) 전용 비루트 유저로 내려도 기능에
# 영향이 없습니다.
RUN useradd --create-home --uid 1000 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# 운영 배포용 이미지라 --reload는 뺐습니다(파일 감시 오버헤드 + 배포 환경에서 의미 없음).
# 로컬 개발 중 코드 변경을 바로 반영하고 싶으면 docker-compose.yml의 api 서비스에서
# volumes 바인드 마운트 + command 오버라이드로 --reload를 켜서 쓰세요.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

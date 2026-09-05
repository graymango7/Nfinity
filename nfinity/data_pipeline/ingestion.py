# ingestion.py
"""
팀원 C - 데이터 수집 및 PostgreSQL DB 적재 파이프라인

병합 후 변경점 (은겸):
- categorizer/cleanser를 data_pipeline 패키지 상대 import로 바꿔서, 저장소 루트
  어디서 실행하든(=`python -m data_pipeline.ingestion`) 동작하도록 했습니다.
  (기존 `from categorizer import ...`는 이 스크립트가 있는 폴더를 CWD로 하고
  직접 실행할 때만 동작해서, 다른 위치에서 import하면 실패했습니다.)
- CSV 기본 경로를 저장소의 data/ 폴더를 가리키도록 바꿨습니다.
"""
import os
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine, text

try:
    from data_pipeline.categorizer import categorize_merchant
    from data_pipeline.cleanser import clean_transactions
except ImportError:
    # 이 폴더 안에서 직접 `python ingestion.py`로 실행하는 경우를 위한 폴백
    from categorizer import categorize_merchant
    from cleanser import clean_transactions

# 팀원 A의 .env 및 docker-compose 설정 기준 DB 접속 URL
DB_URL = os.getenv("DATABASE_URL", "postgresql://sidegig:sidegig_pw@localhost:5432/sidegig")

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def sync_users(engine, df_transactions: pd.DataFrame, persona_map_path: str = None):
    """
    FOREIGN KEY 외래키 제약조건 방지를 위해, 거래 내역 삽입 전 users 테이블을 먼저 동기화합니다.
    """
    print("👤 users 테이블 동기화 시작...")

    if persona_map_path is None:
        persona_map_path = str(DATA_DIR / "persona_map.csv")

    if os.path.exists(persona_map_path):
        df_persona = pd.read_csv(persona_map_path)
        df_persona = df_persona.rename(columns={'persona_name': 'name', 'job': 'persona'})
        df_users = df_persona[['user_id', 'name', 'persona']].drop_duplicates(subset=['user_id'])
    else:
        # persona_map이 없을 경우 transaction의 unique user_id 추출
        unique_users = df_transactions['user_id'].unique()
        df_users = pd.DataFrame({
            'user_id': unique_users,
            'name': [f'사용자_{i+1}' for i in range(len(unique_users))],
            'persona': '미지정'
        })
    
    with engine.begin() as conn:
        for idx, row in df_users.iterrows():
            stmt = text("""
                INSERT INTO users (user_id, name, persona)
                VALUES (:user_id, :name, :persona)
                ON CONFLICT (user_id) DO UPDATE 
                SET name = EXCLUDED.name, persona = EXCLUDED.persona;
            """)
            conn.execute(stmt, {"user_id": row['user_id'], "name": str(row['name']), "persona": str(row['persona'])})
            
            # user_risk_profiles 도 함께 생성
            stmt_profile = text("""
                INSERT INTO user_risk_profiles (user_id)
                VALUES (:user_id)
                ON CONFLICT (user_id) DO NOTHING;
            """)
            conn.execute(stmt_profile, {"user_id": row['user_id']})
            
    print("✅ users 및 user_risk_profiles 동기화 완료!")

def run_pipeline(csv_path: str = None, truncate_first: bool = True):
    """
    truncate_first=True(기본값): 다시 실행해도 안전하도록 매번 transactions를 비우고
    새로 적재합니다. (transaction_id가 DB에서 자동 생성되는 UUID라, 이 옵션 없이
    여러 번 실행하면 매번 중복 적재됩니다 — 개발 중 재실행 시 주의)
    """
    print("🚀 [팀원 C 파이프라인] 데이터 적재 시작...")

    if csv_path is None:
        csv_path = str(DATA_DIR / "mock_transactions.csv")

    if not os.path.exists(csv_path):
        print(f"❌ Error: {csv_path} 파일을 찾을 수 없습니다.")
        return

    # 1. CSV 데이터 읽기
    print(f"1. {csv_path} 데이터 로드 중...")
    df_raw = pd.read_csv(csv_path)

    # 2. 데이터 정제
    print("2. 데이터 정제(Cleansing) 진행 중...")
    df_clean = clean_transactions(df_raw)

    # 3. 카테고리 자동 분류
    print("3. 가맹점 카테고리 매핑(Categorization) 진행 중...")
    df_clean['category'] = df_clean['merchant_name'].apply(categorize_merchant)

    # 4. DB 연결
    print("4. PostgreSQL DB 연결 연결 시도 중...")
    engine = create_engine(DB_URL)

    # 5. 사용자(Users) 사전 등록 (외래키 에러 방지)
    sync_users(engine, df_clean)

    if truncate_first:
        print("4-1. 재실행 대비 기존 transactions 비우는 중...")
        with engine.begin() as conn:
            conn.execute(text("TRUNCATE TABLE risk_events, transactions RESTART IDENTITY CASCADE;"))

    # 6. transactions 테이블 적재
    print("5. transactions 테이블에 데이터 적재 중...")
    df_clean.to_sql('transactions', engine, if_exists='append', index=False)

    print(f"🎉 [성공] {len(df_clean)}건 적재 완료!")

if __name__ == "__main__":
    run_pipeline()

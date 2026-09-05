# cleanser.py
"""
팀원 C - 데이터 정제 및 이상 데이터 검수 모듈
"""
import pandas as pd

def clean_transactions(df: pd.DataFrame) -> pd.DataFrame:
    """
    mock_transactions.csv 데이터를 읽어와 결측치 처리 및 데이터 타입 정제 수행
    """
    df = df.copy()
    
    # 1. 필수 입력 필드 누락 행 제거
    df = df.dropna(subset=['user_id', 'amount', 'timestamp']).copy()
    
    # 2. amount 숫자 타입 변환 및 0 이하 값 처리
    df['amount'] = pd.to_numeric(df['amount'], errors='coerce')
    df = df[df['amount'] > 0].copy()
    
    # 3. timestamp 날짜시간 형변환
    df['timestamp'] = pd.to_datetime(df['timestamp'], errors='coerce')
    df = df.dropna(subset=['timestamp']).copy()
    
    # 4. mcc_code 문자열로 포맷팅
    if 'mcc_code' in df.columns:
        df['mcc_code'] = df['mcc_code'].fillna('').astype(str).str.replace('.0', '', regex=False)
        
    # 5. row_no 컬럼이 있다면 제거 (DB 스키마와 맞춤)
    if 'row_no' in df.columns:
        df = df.drop(columns=['row_no'])
        
    return df

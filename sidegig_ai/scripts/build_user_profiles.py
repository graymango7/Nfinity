"""
Day 3 작업: user_risk_profiles를 "진짜" 값으로 채우는 배치 스크립트.

지금까지는 sql/init.sql / ingestion.py의 sync_users()가 user_risk_profiles를
전부 기본값(단일한도 50만원, 일일한도 100만원 등)으로만 만들어뒀습니다.
이 스크립트는 각 유저의 실제 거래 이력(transactions 테이블)을 바탕으로
평균/표준편차/한도를 계산해서 덮어씁니다.

설계서(리스크방어 및 예산통제 시스템 아키텍처 설계서.pdf, 3.4.5절)의 로직을
단순화해서 옮겼습니다:
  - avg / std / p95 거래금액
  - single_transaction_limit = p95 거래금액 * 3
  - daily_limit = 평균 거래금액 * 하루 평균 거래건수 * 3  (단, single_transaction_limit보다는 크게)
  - allowed_countries = 'KR' + 이력에서 실제로 5% 이상 비중을 차지한 해외 국가

실행:
    python -m scripts.build_user_profiles
"""
import os

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text

DB_URL = os.getenv("DATABASE_URL", "postgresql://sidegig:sidegig_pw@localhost:5432/sidegig")

MIN_TXN_FOR_REAL_PROFILE = 5  # 이보다 거래가 적으면 기본값 유지 (신뢰 불가)


def build_profiles():
    engine = create_engine(DB_URL)

    print("1. transactions 테이블에서 유저별 거래 이력 로드 중...")
    df = pd.read_sql(text("SELECT user_id, amount, timestamp, country FROM transactions"), engine)

    if df.empty:
        print("⚠️  transactions가 비어 있습니다. 먼저 data_pipeline/ingestion.py를 실행하세요.")
        return

    updated, skipped = 0, 0

    with engine.begin() as conn:
        for user_id, g in df.groupby("user_id"):
            n = len(g)
            if n < MIN_TXN_FOR_REAL_PROFILE:
                skipped += 1
                continue

            avg_amt = float(g["amount"].mean())
            std_amt = float(g["amount"].std(ddof=0) or 0.0)
            p95_amt = float(np.percentile(g["amount"], 95))

            span_days = max((g["timestamp"].max() - g["timestamp"].min()).days, 1)
            avg_daily_txns = n / span_days

            single_limit = max(p95_amt * 3, avg_amt * 2)  # 최소한 평균의 2배는 되도록 보정
            daily_limit = max(avg_amt * avg_daily_txns * 3, single_limit * 1.2)

            country_share = g["country"].value_counts(normalize=True)
            allowed = {"KR"} | set(country_share[country_share >= 0.05].index)

            conn.execute(
                text(
                    """
                    UPDATE user_risk_profiles
                    SET avg_transaction_amount = :avg_amt,
                        std_transaction_amount = :std_amt,
                        single_transaction_limit = :single_limit,
                        daily_limit = :daily_limit,
                        avg_daily_transactions = :avg_daily_txns,
                        allowed_countries = :allowed,
                        updated_at = now()
                    WHERE user_id = :uid
                    """
                ),
                {
                    "avg_amt": avg_amt,
                    "std_amt": std_amt,
                    "single_limit": single_limit,
                    "daily_limit": daily_limit,
                    "avg_daily_txns": avg_daily_txns,
                    "allowed": list(allowed),
                    "uid": user_id,
                },
            )
            updated += 1

    print(f"✅ 완료: {updated}명 프로파일 갱신, {skipped}명은 거래 이력이 {MIN_TXN_FOR_REAL_PROFILE}건 미만이라 기본값 유지")


if __name__ == "__main__":
    build_profiles()

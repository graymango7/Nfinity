"""
recurring_detector.py
2026 금융 AI Challenge - SideGig AI (팀원 B: 소현 — Day 6·8 남은 조각)

[이 파일이 하는 일 한 줄 요약]
mock_transactions.csv를 보고 "매달 반복해서 나가는 결제"(넷플릭스 구독료 같은 것)를
자동으로 찾아내고, 그걸 이번 달 예산 예측(/budgets/forecast)에 반영하는 로직입니다.

[To-do 3가지]
  1) 정기결제 감지: 같은 유저 + 같은 가맹점 + 비슷한 금액(±5%) + 약 30일 간격 패턴 찾기
  2) /budgets/forecast 예측식에 "아직 안 나온 정기결제 예상액" 더하기
  3) budgets.alert_thresholds(50/80/90/100/120%)와 usage_rate 비교해서
     지금 넘은 임계값이 뭔지 리스트로 반환하기

[주의] /budgets/forecast 응답에 새로 추가되는 필드명은 은겸님이 먼저 제안한 이름을
그대로 썼습니다: recurring_payments, crossed_thresholds
→ 이 이름을 실제로 API에 반영하기 전에 최종 컨펌 받기.
"""

import os
import pandas as pd
from datetime import datetime, timedelta

# ------------------------------------------------------------------
# 0. 설정값
# ------------------------------------------------------------------
AMOUNT_TOLERANCE = 0.05      # 금액이 이 비율(±5%) 안에서 다르면 "같은 결제"로 취급
MIN_OCCURRENCES = 3          # 최소 3번은 반복돼야 "정기결제"로 인정 (2번은 우연히 겹칠 확률이 높음)

# 9/5 수정: 고정 30일(±5일) 대신 "주기가 일정한가"로 판정합니다 — 아래 detect_recurring_payments
# 주석 참고. 주간(7일)·격주(14일)·월간(30일) 결제를 모두 잡되 불규칙 결제는 계속 걸러냅니다.
MIN_INTERVAL_DAYS = 5             # 이보다 촘촘하면 정기결제가 아니라 일상 반복 소비로 봄
MAX_INTERVAL_DAYS = 40            # 이보다 뜸하면 이 데이터 기간(약 3개월)에서 주기 판단이 어려움
INTERVAL_RELATIVE_TOLERANCE = 0.25  # 모든 간격이 중앙값의 ±25% 안에 들어와야 "규칙적"

# ------------------------------------------------------------------
# 0-1. ★ 파일 경로 자동탐색 (expense_classifier.py와 똑같은 방식)
#
#   __file__ = 지금 이 코드가 들어있는 파일 자체의 위치.
#   이걸 기준으로 삼으면 터미널을 어디서 열든 항상 같은 데이터 폴더를 찾습니다.
#
#   폴더 구조 가정:
#     sidegig_ai/
#       ├── data/
#       │     └── mock_transactions.csv
#       └── data_pipeline/
#             └── recurring_detector.py   
# ------------------------------------------------------------------
THIS_FILE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_TX_CSV = os.path.join(THIS_FILE_DIR, "..", "data", "mock_transactions.csv")


# ==========================================================
# 1. 정기결제 감지
# ==========================================================
def detect_recurring_payments(df: pd.DataFrame) -> pd.DataFrame:
    """
    입력: user_id, merchant_name, amount, timestamp 컬럼이 있는 거래 내역 DataFrame
    출력: 정기결제로 판단된 (유저, 가맹점) 조합마다 한 줄씩 정리된 DataFrame
          [user_id, merchant_name, occurrences(반복횟수), avg_amount(평균금액),
           avg_interval_days(평균간격), last_paid_date(마지막결제일),
           next_expected_date(다음예상일)]

    동작 순서 (같은 유저+가맹점 그룹마다 반복):
      1) 시간순으로 정렬한다
      2) 금액이 서로 ±5% 이내로 비슷한 거래들끼리 하나의 묶음(클러스터)으로 그룹핑한다
         (같은 가맹점이라도 어쩌다 한 번 다른 금액을 결제했을 수 있으니까)
      3) 그 묶음 안에서 결제일 간격들을 계산해서, 전부 25~35일 사이인지 확인한다
      4) 조건을 만족하면 "정기결제"로 인정하고 결과에 추가한다
    """
    df = df.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"])

    results = []

    # (유저, 가맹점) 쌍마다 따로 검사
    for (user_id, merchant), group in df.sort_values("timestamp").groupby(["user_id", "merchant_name"]):
        group = group.reset_index(drop=True)
        if len(group) < MIN_OCCURRENCES:
            continue  # 애초에 반복 횟수가 부족하면 볼 것도 없음

        used = [False] * len(group)  # 이미 다른 클러스터에 포함된 거래는 다시 안 씀

        for i in range(len(group)):
            if used[i]:
                continue

            # i번째 거래를 기준으로, 금액이 ±5% 이내인 다른 거래들을 전부 모은다
            cluster_idx = [i]
            base_amount = group.loc[i, "amount"]
            for j in range(i + 1, len(group)):
                if used[j]:
                    continue
                if abs(group.loc[j, "amount"] - base_amount) <= base_amount * AMOUNT_TOLERANCE:
                    cluster_idx.append(j)

            if len(cluster_idx) < MIN_OCCURRENCES:
                continue  # 이 금액대로는 반복 횟수가 부족함

            cluster = group.loc[cluster_idx].sort_values("timestamp").reset_index(drop=True)

            # 연속된 결제일 사이의 간격(일수)을 계산 (예: [30, 31, 29] 이런 식으로)
            intervals = cluster["timestamp"].diff().dt.days.dropna()
            if intervals.empty:
                continue

            # 9/5 수정 — 원래는 "모든 간격이 25~35일"일 때만 정기결제로 인정했습니다.
            # 그러다 보니 월 구독만 잡히고 주간(배달 앱 구독)·격주 결제는 전부 놓쳤고,
            # 실제로 데모 페르소나의 45일 현금흐름 시뮬레이션에 투사된 정기 지출이
            # 단 한 건도 없어서(수입 계단만 있고 지출은 매일 균등 감소) 예측 그래프가
            # 사실상 직선이 되는 문제가 있었습니다.
            #
            # 대신 "간격이 얼마나 일정한가"로 판정합니다: 간격의 중앙값이 주간~월간
            # 범위(5~40일) 안에 있고, 모든 간격이 그 중앙값의 ±25% 안에 들어오면
            # 정기결제로 봅니다. 주기가 7일이든 14일이든 30일이든 규칙적이기만 하면
            # 잡히고, 불규칙한 결제(스타벅스처럼)는 여전히 걸러집니다.
            median_interval = float(intervals.median())
            if not (MIN_INTERVAL_DAYS <= median_interval <= MAX_INTERVAL_DAYS):
                continue
            tolerance = median_interval * INTERVAL_RELATIVE_TOLERANCE
            is_regular = intervals.between(
                median_interval - tolerance,
                median_interval + tolerance,
            ).all()
            if not is_regular:
                continue

            # 이 클러스터에 쓰인 거래들은 "이미 처리됨" 표시 (중복 집계 방지)
            for idx in cluster_idx:
                used[idx] = True

            avg_amount = int(cluster["amount"].mean())
            avg_interval = round(intervals.mean(), 1)
            last_paid = cluster["timestamp"].max()
            next_expected = last_paid + timedelta(days=round(avg_interval))

            results.append({
                "user_id": user_id,
                "merchant_name": merchant,
                "occurrences": len(cluster),
                "avg_amount": avg_amount,
                "avg_interval_days": avg_interval,
                "last_paid_date": last_paid.date().isoformat(),
                "next_expected_date": next_expected.date().isoformat(),
            })

    return pd.DataFrame(results)


# ==========================================================
# 2. /budgets/forecast에 "아직 안 나온 정기결제 예상액" 더하기
# ==========================================================
def forecast_month_end(user_id: str, transactions_df: pd.DataFrame, recurring_df: pd.DataFrame,
                        current_date: datetime) -> dict:
    """
    기존 예측 방식: "이번 달 지금까지 쓴 돈 ÷ 지난 날 수" = 하루 평균 지출
                    → 하루 평균 지출 × 남은 날 수를 더해서 "이번 달 말 예상 총 지출" 계산

    여기에 추가하는 것: 이 유저의 정기결제 중, "다음 예상일이 이번 달 안에 있고
    아직 지나지 않은 것들"의 금액을 더함 (예: 넷플릭스 결제일이 아직 안 왔으면 그만큼 더 나갈 걸 미리 반영)
    """
    month_start = current_date.replace(day=1)
    days_elapsed = (current_date - month_start).days + 1

    # 이번 달이 총 며칠까지 있는지 계산 (28일이 있는 달에 4일 더한 뒤 그 달 1일로 되돌리는 트릭)
    next_month = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
    days_in_month = (next_month - month_start).days
    remaining_days = days_in_month - days_elapsed

    tx = transactions_df.copy()
    tx["timestamp"] = pd.to_datetime(tx["timestamp"])
    this_month_tx = tx[(tx["user_id"] == user_id) &
                        (tx["timestamp"] >= month_start) &
                        (tx["timestamp"] <= current_date)]

    spent_so_far = int(this_month_tx["amount"].sum())
    avg_daily_spend = spent_so_far / days_elapsed if days_elapsed > 0 else 0
    base_forecast = spent_so_far + avg_daily_spend * remaining_days  # 기존 방식 그대로

    # 이 유저의 정기결제 중 "이번 달 안에, 아직 안 지나간 것"만 골라냄
    user_recurring = recurring_df[recurring_df["user_id"] == user_id].copy()
    recurring_payments = []
    recurring_addition = 0
    if not user_recurring.empty:
        user_recurring["next_expected_date"] = pd.to_datetime(user_recurring["next_expected_date"])
        upcoming = user_recurring[
            (user_recurring["next_expected_date"] > current_date) &
            (user_recurring["next_expected_date"] < next_month)
        ]
        for _, row in upcoming.iterrows():
            recurring_payments.append({
                "merchant_name": row["merchant_name"],
                "expected_amount": int(row["avg_amount"]),
                "expected_date": row["next_expected_date"].date().isoformat(),
            })
        recurring_addition = int(upcoming["avg_amount"].sum())

    return {
        "forecast_amount": int(base_forecast + recurring_addition),   # 최종 예측치 (정기결제 반영)
        "base_forecast_amount": int(base_forecast),                    # 참고용: 정기결제 반영 전 값
        "recurring_payments": recurring_payments,                      # ★ 은겸님 제안 필드명
    }


# ==========================================================
# 3. 예산 임계값(alert_thresholds) 체크
# ==========================================================
def check_crossed_thresholds(usage_rate: float, alert_thresholds=(50, 80, 90, 100, 120)) -> list:
    """
    usage_rate: budgets 테이블의 usage_rate 그대로 (0.83 = 83% 사용했다는 뜻)
    반환: 지금 usage_rate가 이미 넘은 임계값들의 리스트, 오름차순 정렬
          예) usage_rate=0.83 -> [50, 80]  (90/100/120은 아직 안 넘었으니 제외)

    신호등 색으로 바꾸는 건 프론트(하은님) 몫이라, 여기선 숫자 리스트만 돌려줍니다.
    """
    usage_pct = usage_rate * 100
    return [t for t in sorted(alert_thresholds) if usage_pct >= t]


# ==========================================================
# 4. 이 파일을 직접 실행했을 때 (python recurring_detector.py)
#    — 실제 데이터로 감지 + 합성 데이터로 로직 자체 검증 + 데모 출력까지 한번에
# ==========================================================
if __name__ == "__main__":
    print(f"[안내] 거래 데이터 경로: {DEFAULT_TX_CSV}\n")

    # --- 4-1. 실제 mock_transactions.csv로 정기결제 탐지 ---
    real_df = pd.read_csv(DEFAULT_TX_CSV)
    real_recurring = detect_recurring_payments(real_df)
    print("=" * 55)
    print(f"실제 데이터에서 발견된 정기결제 후보: {len(real_recurring)}건")
    if not real_recurring.empty:
        print(real_recurring.head(10).to_string(index=False))
    else:
        print("(정기결제 패턴이 감지되지 않았습니다. 아래 4-2는 로직 자체 검증용 테스트입니다.)")

    # --- 4-2. 로직이 제대로 동작하는지 확인하는 합성(가짜) 테스트 데이터 ---
    #     "넷플릭스 매달 17,000원" 패턴은 잡히고, 간격이 불규칙한 스타벅스는 걸러지는지 확인
    print("\n" + "=" * 55)
    print("[자체 검증] 넷플릭스 매달 17,000원 패턴 합성 테스트")
    synthetic = pd.DataFrame([
        {"user_id": "test-user-1", "merchant_name": "넷플릭스", "amount": 17000, "timestamp": "2026-05-15 10:00:00"},
        {"user_id": "test-user-1", "merchant_name": "넷플릭스", "amount": 17000, "timestamp": "2026-06-14 10:00:00"},
        {"user_id": "test-user-1", "merchant_name": "넷플릭스", "amount": 17400, "timestamp": "2026-07-16 10:00:00"},
        {"user_id": "test-user-1", "merchant_name": "스타벅스", "amount": 5000, "timestamp": "2026-06-01 09:00:00"},
        {"user_id": "test-user-1", "merchant_name": "스타벅스", "amount": 5200, "timestamp": "2026-06-20 09:00:00"},
    ])
    synthetic_recurring = detect_recurring_payments(synthetic)
    print(synthetic_recurring.to_string(index=False))
    assert len(synthetic_recurring) == 1, "넷플릭스 패턴 1건만 잡혀야 합니다"
    assert synthetic_recurring.iloc[0]["merchant_name"] == "넷플릭스"
    print("✅ 합성 테스트 통과 (넷플릭스만 정기결제로 잡히고, 간격 불규칙한 스타벅스는 제외됨)")

    # --- 4-3. forecast 계산 데모 ---
    print("\n" + "=" * 55)
    print("[데모] /budgets/forecast 응답 예시 (정기결제 반영)")
    demo_tx = pd.concat([synthetic, pd.DataFrame([
        {"user_id": "test-user-1", "merchant_name": "쿠팡", "amount": 32000, "timestamp": "2026-08-03 14:00:00"},
        {"user_id": "test-user-1", "merchant_name": "김밥천국", "amount": 8000, "timestamp": "2026-08-07 12:30:00"},
    ])], ignore_index=True)
    fake_now = datetime(2026, 8, 10)  # 넷플릭스 다음 예상일(8/16)보다 이전 시점
    forecast = forecast_month_end("test-user-1", demo_tx, synthetic_recurring, fake_now)
    print(f"기준일: {fake_now.date()} (이번 달 지출 40,000원 발생 + 넷플릭스 8/16 예정)")
    print(forecast)

    # --- 4-4. 임계값 체크 데모 ---
    print("\n" + "=" * 55)
    print("[데모] check_crossed_thresholds 예시")
    for rate in [0.35, 0.55, 0.83, 1.05]:
        print(f"usage_rate={rate} -> crossed_thresholds={check_crossed_thresholds(rate)}")

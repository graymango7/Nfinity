"""
seed_data.py (v2)
2026 금융 AI Challenge - SideGig AI (팀원 B: 데이터 담당)

팀원 A의 README 6번 "데이터 계약"에 맞춰 컬럼을 다시 맞춘 버전입니다.

필수 컬럼 (mock_transactions.csv, 팀원A/C와 합의된 것 그대로):
    user_id, amount, merchant_id, merchant_name, mcc_code, timestamp,
    latitude, longitude, country, device_id

★ category, is_anomaly, anomaly_reason은 이제 이 파일에 넣지 않습니다.
  - category는 팀원 C가 category_mapping 테이블 기준으로 채우는 몫입니다.
  - is_anomaly는 팀원 A의 rule_engine.py(R001~R007)가 계산하는 몫입니다.
  다만 우리가 "제대로 걸러지는지" 자체 검증할 때 쓰라고, 정답지를
  mock_transactions_answer_key.csv 로 따로 뽑아둡니다. (row_no로 join)

★ R001~R007 정확한 판별 로직은 app/rule_engine.py를 못 받아서 README에
  나온 이름(단일 한도초과/심야대액/단기반복/해외결제) + 일반적인 이상거래
  패턴(위치 급변, 신규 기기)을 기준으로 만들었습니다. 실제 rule_engine.py
  받으면 기준값(threshold)만 맞춰 조정하면 됩니다.
"""

import pandas as pd
import random
import uuid
from datetime import datetime, timedelta

# ==========================================================
# 0. 설정값
# ==========================================================
random.seed(42)

N_GENERIC_USERS = 30
MOCK_MONTHS = 3
ANOMALY_EVENT_RATE = 0.039

END_DATE = datetime(2026, 8, 23)
START_DATE = END_DATE - timedelta(days=90)
TOTAL_DAYS = (END_DATE - START_DATE).days

# 대한민국 대략적 위경도 범위 (유저별 '집' 좌표를 이 안에서 랜덤 배정)
KR_LAT_RANGE = (35.0, 37.7)
KR_LON_RANGE = (126.5, 129.0)


# ==========================================================
# 1. 페르소나 정의 (이전 버전과 동일한 스토리 유지)
# ==========================================================
PERSONAS = [
    {
        "name": "김민철", "job": "배달_투잡러", "story_flag": "normal",
        "expense_bias": {"식비": 0.35, "교통": 0.30, "쇼핑": 0.20, "업무용": 0.15},
        "expense_amount_scale": 0.7,
    },
    {
        "name": "박지수", "job": "프리랜서_마케터", "story_flag": "cashflow_crisis",
        "expense_bias": {"식비": 0.25, "교통": 0.15, "쇼핑": 0.20, "업무용": 0.40},
        "expense_amount_scale": 1.3,
    },
    {
        "name": "이하늘", "job": "IT_개발자", "story_flag": "normal",
        "expense_bias": {"식비": 0.20, "교통": 0.10, "쇼핑": 0.20, "업무용": 0.50},
        "expense_amount_scale": 1.5,
    },
    {
        "name": "최유진", "job": "크리에이터_인플루언서", "story_flag": "budget_over",
        "expense_bias": {"식비": 0.20, "교통": 0.10, "쇼핑": 0.55, "업무용": 0.15},
        "expense_amount_scale": 2.2,
    },
    {
        "name": "정다운", "job": "강사_배민커넥트", "story_flag": "nhis_risk",
        "expense_bias": {"식비": 0.30, "교통": 0.25, "쇼핑": 0.25, "업무용": 0.20},
        "expense_amount_scale": 0.9,
    },
]
GENERIC_JOBS = ["배달_투잡러", "프리랜서_마케터", "IT_개발자", "크리에이터_인플루언서", "강사_배민커넥트"]

# 가맹점명 -> (MCC 코드, 카테고리*내부용) — *카테고리는 merchant 선택 가중치용으로만 쓰고 출력 안 함
MERCHANTS = {
    "식비": [("스타벅스", "5814"), ("맥도날드", "5814"), ("배달의민족", "5812"), ("김밥천국", "5812")],
    "교통": [("카카오택시", "4121"), ("지하철", "4111"), ("버스", "4111"), ("KTX", "4112")],
    "쇼핑": [("쿠팡", "5399"), ("프리스비", "5732"), ("올리브영", "5977"), ("무신사", "5651")],
    "업무용": [("어도비(Adobe)", "5734"), ("AWS", "7372"), ("노션", "7372"), ("패스트파이브", "7011")],
}

# 이상거래용 가맹점 (mcc, country)
ANOMALY_MERCHANTS = {
    "late_night": [("금은방_귀금속", "5944"), ("중고거래_송금", "6051"), ("유흥업소", "5813")],
    "overseas": [("Amazon_US", "5942"), ("AliExpress_CN", "5399"), ("Steam_Global", "5816")],
    "repeated": [("게임아이템_스토어", "7994")],
}

MERCHANT_ID_CACHE = {}


def merchant_id_for(name: str) -> str:
    """가맹점명마다 고정된 merchant_id를 부여 (동일 가맹점이면 항상 같은 ID)."""
    if name not in MERCHANT_ID_CACHE:
        MERCHANT_ID_CACHE[name] = "MCH-" + uuid.uuid5(uuid.NAMESPACE_DNS, name).hex[:10]
    return MERCHANT_ID_CACHE[name]


# ==========================================================
# 2. 유저 풀 구성 (페르소나 5명 + 배경 유저 N명, 각자 '집' 좌표/기기 배정)
# ==========================================================
def build_user_pool():
    users = []

    def make_user(name, profile):
        home_lat = round(random.uniform(*KR_LAT_RANGE), 6)
        home_lon = round(random.uniform(*KR_LON_RANGE), 6)
        return {
            "user_id": str(uuid.uuid4()),
            "persona_name": name,
            "profile": profile,
            "home_lat": home_lat,
            "home_lon": home_lon,
            "device_id": "DEV-" + uuid.uuid4().hex[:10],  # 평소 쓰는 기기
        }

    for p in PERSONAS:
        users.append(make_user(p["name"], p))

    for i in range(N_GENERIC_USERS):
        job = random.choice(GENERIC_JOBS)
        base = next(p for p in PERSONAS if p["job"] == job)
        profile = dict(base)
        users.append(make_user(f"일반유저_{job}_{i+1}", profile))

    return users


# ==========================================================
# 3. 거래 row 생성 헬퍼
# ==========================================================
def pick_category(bias: dict) -> str:
    return random.choices(list(bias.keys()), weights=list(bias.values()), k=1)[0]


def jitter_coord(lat, lon, km=3):
    # 대략 위도 1도 ≈ 111km 이므로, km 반경 내 근처 좌표를 만듦
    d = km / 111
    return round(lat + random.uniform(-d, d), 6), round(lon + random.uniform(-d, d), 6)


def far_coord():
    # '집'과 무관하게 멀리 떨어진 임의 좌표 (위치 급변 이상거래용)
    return round(random.uniform(*KR_LAT_RANGE), 6), round(random.uniform(*KR_LON_RANGE), 6)


def make_row(user, merchant_name, mcc, amount, txn_time, lat, lon,
             country="KR", device_id=None, is_anomaly=0, anomaly_reason="정상"):
    return {
        "user_id": user["user_id"],
        "amount": amount,
        "merchant_id": merchant_id_for(merchant_name),
        "merchant_name": merchant_name,
        "mcc_code": mcc,
        "timestamp": txn_time.strftime("%Y-%m-%d %H:%M:%S"),
        "latitude": lat,
        "longitude": lon,
        "country": country,
        "device_id": device_id or user["device_id"],
        # --- 아래 두 컬럼은 answer_key 전용, 최종 산출 시 분리됨 ---
        "_is_anomaly": is_anomaly,
        "_anomaly_reason": anomaly_reason,
    }


# ==========================================================
# 4. 지출(결제) 데이터 생성
# ==========================================================
def generate_expenses(users):
    rows = []
    for u in users:
        profile = u["profile"]
        scale = profile["expense_amount_scale"]
        budget_over = profile["story_flag"] == "budget_over"

        for day_offset in range(TOTAL_DAYS):
            current_date = START_DATE + timedelta(days=day_offset)
            daily_txn_count = random.randint(0, 3)
            if budget_over and day_offset > TOTAL_DAYS - 14:
                daily_txn_count += random.randint(1, 3)

            for _ in range(daily_txn_count):
                roll = random.random()
                lat, lon = jitter_coord(u["home_lat"], u["home_lon"])

                if roll < ANOMALY_EVENT_RATE * 0.3:
                    # R005 심야 대액 결제
                    name, mcc = random.choice(ANOMALY_MERCHANTS["late_night"])
                    amount = random.randint(100, 500) * 10000
                    txn_time = current_date.replace(hour=random.randint(2, 5), minute=random.randint(0, 59))
                    rows.append(make_row(u, name, mcc, amount, txn_time, lat, lon,
                                          is_anomaly=1, anomaly_reason="R005_심야대액"))

                elif roll < ANOMALY_EVENT_RATE * 0.55:
                    # R007 해외 결제
                    name, mcc = random.choice(ANOMALY_MERCHANTS["overseas"])
                    amount = random.randint(10, 100) * 10000
                    txn_time = current_date.replace(hour=random.randint(0, 23), minute=random.randint(0, 59))
                    country = random.choice(["US", "CN", "UK"])
                    rows.append(make_row(u, name, mcc, amount, txn_time, lat, lon, country=country,
                                          is_anomaly=1, anomaly_reason="R007_해외결제"))

                elif roll < ANOMALY_EVENT_RATE * 0.75:
                    # R001 단일 한도 초과
                    category = pick_category(profile["expense_bias"])
                    name, mcc = random.choice(MERCHANTS[category])
                    amount = int(random.randint(80, 200) * 10000 * scale)
                    txn_time = current_date.replace(hour=random.randint(8, 22), minute=random.randint(0, 59))
                    rows.append(make_row(u, name, mcc, amount, txn_time, lat, lon,
                                          is_anomaly=1, anomaly_reason="R001_단일한도초과"))

                elif roll < ANOMALY_EVENT_RATE * 0.9:
                    # R00x 위치 급변 (짧은 시간 내 먼 거리로 이동 - GPS 위조/명의도용 의심)
                    category = pick_category(profile["expense_bias"])
                    name, mcc = random.choice(MERCHANTS[category])
                    amount = int(random.randint(15, 100) * 1000 * scale)
                    txn_time = current_date.replace(hour=random.randint(8, 22), minute=random.randint(0, 59))
                    far_lat, far_lon = far_coord()
                    rows.append(make_row(u, name, mcc, amount, txn_time, far_lat, far_lon,
                                          is_anomaly=1, anomaly_reason="R00x_위치급변"))

                elif roll < ANOMALY_EVENT_RATE:
                    # R00y 신규(미인식) 기기 사용
                    category = pick_category(profile["expense_bias"])
                    name, mcc = random.choice(MERCHANTS[category])
                    amount = int(random.randint(15, 100) * 1000 * scale)
                    txn_time = current_date.replace(hour=random.randint(8, 22), minute=random.randint(0, 59))
                    new_device = "DEV-" + uuid.uuid4().hex[:10]
                    rows.append(make_row(u, name, mcc, amount, txn_time, lat, lon, device_id=new_device,
                                          is_anomaly=1, anomaly_reason="R00y_신규기기"))

                else:
                    # 정상 거래
                    category = pick_category(profile["expense_bias"])
                    name, mcc = random.choice(MERCHANTS[category])
                    if category == "식비":
                        amount = int(random.randint(5, 30) * 1000 * scale)
                    elif category == "쇼핑":
                        amount = int(random.randint(15, 100) * 1000 * scale)
                    elif category == "교통":
                        amount = int(random.randint(15, 200) * 100 * scale)
                    else:
                        amount = int(random.randint(5, 30) * 10000 * scale)
                    txn_time = current_date.replace(hour=random.randint(8, 22), minute=random.randint(0, 59))
                    rows.append(make_row(u, name, mcc, amount, txn_time, lat, lon))

            # R006 단기 반복 결제 (3연타, row 비율에 영향 적게 별도 확률로)
            if random.random() < ANOMALY_EVENT_RATE * 0.15:
                name, mcc = ANOMALY_MERCHANTS["repeated"][0]
                amount = random.randint(5, 15) * 10000
                base_hour = random.randint(8, 22)
                base_minute = random.randint(0, 50)
                lat, lon = jitter_coord(u["home_lat"], u["home_lon"])
                for i in range(3):
                    txn_time = current_date.replace(hour=base_hour, minute=base_minute + i)
                    rows.append(make_row(u, name, mcc, amount, txn_time, lat, lon,
                                          is_anomaly=1, anomaly_reason="R006_단기반복"))

    return pd.DataFrame(rows)


# ==========================================================
# 5. 실행
# ==========================================================
if __name__ == "__main__":
    users = build_user_pool()
    df = generate_expenses(users)
    df.insert(0, "row_no", range(1, len(df) + 1))  # 두 파일 join용 (계약 컬럼 아님)
    df = df.sort_values(by=["user_id", "timestamp"]).reset_index(drop=True)
    df["row_no"] = range(1, len(df) + 1)

    CONTRACT_COLUMNS = ["user_id", "amount", "merchant_id", "merchant_name",
                         "mcc_code", "timestamp", "latitude", "longitude", "country", "device_id"]
    df["mcc_code"] = df["mcc_code"].astype(str)  # 앞자리 0 등 코드 포맷 보존 (숫자로 캐스팅되지 않게)
    df[CONTRACT_COLUMNS].to_csv("mock_transactions.csv", index=False, encoding="utf-8-sig")

    df[["user_id", "_is_anomaly", "_anomaly_reason"]].rename(
        columns={"_is_anomaly": "is_anomaly", "_anomaly_reason": "anomaly_reason"}
    ).to_csv("mock_transactions_answer_key.csv", index=False, encoding="utf-8-sig")

    df_persona_map = pd.DataFrame([
        {"user_id": u["user_id"], "persona_name": u["persona_name"],
         "job": u["profile"]["job"], "story_flag": u["profile"]["story_flag"],
         "home_lat": u["home_lat"], "home_lon": u["home_lon"], "device_id": u["device_id"]}
        for u in users
    ])
    df_persona_map.to_csv("persona_map.csv", index=False, encoding="utf-8-sig")

    total = len(df)
    anomaly = int(df["_is_anomaly"].sum())
    print("=" * 55)
    print(f"총 유저 수: {len(users)}명 (페르소나 {len(PERSONAS)}명 + 배경 유저 {N_GENERIC_USERS}명)")
    print(f"지출 거래: {total}건 / 이상거래: {anomaly}건 ({anomaly/total*100:.2f}%)")
    print(df["_anomaly_reason"].value_counts().to_string())
    print("저장 완료: mock_transactions.csv (계약 컬럼만) / mock_transactions_answer_key.csv (검증용) / persona_map.csv")
    print("=" * 55)

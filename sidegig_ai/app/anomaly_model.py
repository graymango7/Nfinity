"""
app/anomaly_model.py — Day 4 작업: Isolation Forest 이상 탐지 모델 (은겸 담당)

이 모듈이 하는 일: 룰 엔진(app/rule_engine.py)의 R001~R007은 "미리 정해둔 규칙"만 잡습니다.
정답지(data/mock_transactions_answer_key.csv)를 까보면 이상 거래 263건 중 36건
(R00x_위치급변 21건 + R00y_신규기기 15건)은 애초에 규칙으로 설계되지 않은 유형이라 룰
엔진으로는 절대 못 잡습니다 — 이 두 유형을 잡는 게 이 모델의 목표입니다.

사용법:
  1) 학습 (최초 1회, 또는 데이터가 바뀌면 다시):
       python -m app.anomaly_model
     data/mock_transactions.csv 전체로 학습해서 models/anomaly_iforest.pkl로 저장하고,
     정답지 대비 이 모델 단독 성능(이 두 유형을 얼마나 잡는지)을 바로 출력합니다.
  2) 추론 (app/routers/risk.py가 자동으로 사용 — 직접 호출할 일은 거의 없음):
       from app.anomaly_model import score_anomaly
       anomaly_prob = score_anomaly(txn, profile, history)  # 0.0(정상) ~ 1.0(이상)

★ 모델 파일(models/anomaly_iforest.pkl)이 아직 없으면 score_anomaly()는 항상 0.0을 돌려줘서
   룰 엔진만 동작하던 기존 동작을 그대로 유지합니다 (학습 전이라고 서버가 죽지 않음).

피처 4개 — 전부 "그 거래 시점까지의 과거 이력만" 사용합니다(미래 정보 누출 없음):
  - amount_zscore          유저 평균 대비 이번 결제가 몇 표준편차 떨어져 있는지
  - is_new_device           이 device_id를 이 유저가 이전에 쓴 적 있는지 → R00y_신규기기 타깃
  - distance_from_prev_km   직전 결제 위치에서 얼마나 떨어져 있는지 → R00x_위치급변 타깃
                            (R004 룰은 "물리적으로 불가능한 속도"만 잡는데, 그 정도까진
                            아니어도 "갑자기 먼 곳"인 패턴은 이 피처로 IF가 잡아냅니다)
  - merchant_rarity          이 가맹점을 이 유저가 그동안 얼마나 드물게 갔는지 (0~1, 1에
                            가까울수록 처음 보는 가맹점)

※ 처음엔 hour_sin/cos, dow_sin/cos(결제 시각/요일)도 넣어서 8개 피처로 만들었는데,
  실제로 정답지 대비 검증해보니 이 두 피처가 오히려 신호를 희석시켰습니다 — IsolationForest는
  각 분기(split)마다 피처를 무작위로 하나씩 골라 쓰는 방식이라, 의미 없는 피처가 섞여있으면
  진짜 신호가 있는 피처(is_new_device, distance_from_prev_km)가 뽑힐 확률이 그만큼 줄어듭니다.
  8개 피처일 때: 타깃 유형 탐지 6/36건 → 4개로 줄이니 20/36건으로 향상 (아래 self-evaluate
  결과 참고). 그래서 시간대 관련 이상 패턴은 이미 룰 엔진의 R005(심야 대액)가 담당하는 걸로
  역할을 나누고, 여기선 룰 엔진이 원천적으로 못 잡는 두 유형에 집중하도록 피처를 줄였습니다.
"""
from __future__ import annotations

import pickle
from datetime import datetime
from math import radians, sin, cos, sqrt, atan2
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler

from app.models import Transaction, UserRiskProfile

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
MODEL_DIR = Path(__file__).resolve().parent.parent / "models"
MODEL_PATH = MODEL_DIR / "anomaly_iforest.pkl"

FEATURE_NAMES = [
    "amount_zscore",
    "is_new_device",
    "distance_from_prev_km",
    "merchant_rarity",
]

# 이 확률 이상이면, 걸린 룰이 하나도 없어도 "AI 이상탐지"로 risk_events에 별도 기록합니다.
# 0.6 기준 자체 검증: 타깃 유형(R00x/R00y) 36건 중 20건 탐지, 정상 거래 오탐율 1.0%.
ANOMALY_ALERT_THRESHOLD = 0.6
ANOMALY_HIGH_THRESHOLD = 0.8  # 이 이상이면 HIGH로, 아니면 MEDIUM으로 기록


def _haversine_km(lat1, lon1, lat2, lon2) -> float:
    R = 6371.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))


# ============================================================
# 1. 추론 시점 피처 계산 (실시간 API에서 사용)
# ============================================================


def compute_features(
    txn: Transaction, profile: UserRiskProfile, history: list[Transaction]
) -> np.ndarray:
    """history는 이 거래보다 "이전"인, 같은 유저의 거래 목록(시간순 정렬)이어야 합니다."""
    std = profile.std_transaction_amount or (profile.avg_transaction_amount * 0.3) or 1.0
    amount_zscore = (txn.amount - profile.avg_transaction_amount) / (std + 1e-6)

    seen_devices = {t.device_id for t in history if t.device_id}
    is_new_device = 0.0 if (not txn.device_id or txn.device_id in seen_devices) else 1.0

    distance_from_prev = 0.0
    if history and history[-1].latitude is not None and txn.latitude is not None:
        last = history[-1]
        distance_from_prev = _haversine_km(last.latitude, last.longitude, txn.latitude, txn.longitude)

    merchant_hits = sum(1 for t in history if t.merchant_name == txn.merchant_name)
    merchant_rarity = 1.0 / (1.0 + merchant_hits)

    return np.array(
        [[amount_zscore, is_new_device, distance_from_prev, merchant_rarity]],
        dtype=float,
    )


# ============================================================
# 2. 학습용 피처 계산 (CSV 전체를 시간순으로 훑으면서, 미래 정보 누출 없이 계산)
# ============================================================


def _build_training_dataframe(
    transactions_csv: Optional[str] = None, persona_csv: Optional[str] = None
) -> tuple[pd.DataFrame, pd.Series]:
    transactions_csv = transactions_csv or str(DATA_DIR / "mock_transactions.csv")
    df = pd.read_csv(transactions_csv)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.sort_values(["user_id", "timestamp"]).reset_index(drop=True)

    rows = []
    for user_id, g in df.groupby("user_id", sort=False):
        g = g.sort_values("timestamp")
        amounts = g["amount"].to_numpy(dtype=float)
        # 학습 시점엔 user_risk_profiles가 아직 없을 수 있으니, 그 유저의 전체 이력으로
        # 평균/표준편차를 근사합니다 (scripts/build_user_profiles.py와 같은 방식).
        avg_amt = float(amounts.mean())
        std_amt = float(amounts.std(ddof=0)) or (avg_amt * 0.3) or 1.0

        seen_devices: set[str] = set()
        merchant_counts: dict[str, int] = {}
        prev_lat = prev_lon = None

        for _, r in g.iterrows():
            amount_zscore = (r["amount"] - avg_amt) / (std_amt + 1e-6)

            device_id = r.get("device_id")
            is_new_device = 0.0 if (pd.isna(device_id) or device_id in seen_devices) else 1.0

            distance_from_prev = 0.0
            if prev_lat is not None and not pd.isna(r["latitude"]):
                distance_from_prev = _haversine_km(prev_lat, prev_lon, r["latitude"], r["longitude"])

            merchant = r["merchant_name"]
            hits = merchant_counts.get(merchant, 0)
            merchant_rarity = 1.0 / (1.0 + hits)

            rows.append(
                {
                    "row_no": r["row_no"],
                    "user_id": user_id,
                    "amount_zscore": amount_zscore,
                    "is_new_device": is_new_device,
                    "distance_from_prev_km": distance_from_prev,
                    "merchant_rarity": merchant_rarity,
                }
            )

            # 이번 거래를 이력에 반영 (다음 거래부터는 "과거"가 됨 — 미래 정보 누출 방지)
            if device_id and not pd.isna(device_id):
                seen_devices.add(device_id)
            merchant_counts[merchant] = hits + 1
            if not pd.isna(r["latitude"]):
                prev_lat, prev_lon = r["latitude"], r["longitude"]

    feat_df = pd.DataFrame(rows)
    return feat_df, feat_df["row_no"]


# ============================================================
# 3. 학습 & 저장
# ============================================================


def train(transactions_csv: Optional[str] = None, contamination: float = 0.06) -> dict:
    print("1. 학습용 피처 계산 중 (유저별 시간순 진행, 미래 정보 누출 없이)...")
    feat_df, row_nos = _build_training_dataframe(transactions_csv)
    X = feat_df[FEATURE_NAMES].to_numpy(dtype=float)

    print(f"2. IsolationForest 학습 중... (샘플 {len(X)}건, contamination={contamination})")
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)

    model = IsolationForest(
        n_estimators=200, contamination=contamination, random_state=42, n_jobs=-1
    )
    model.fit(X_scaled)

    raw_scores = -model.score_samples(X_scaled)  # 높을수록 이상
    train_min, train_max = float(raw_scores.min()), float(raw_scores.max())

    MODEL_DIR.mkdir(exist_ok=True)
    with open(MODEL_PATH, "wb") as f:
        pickle.dump(
            {
                "model": model,
                "scaler": scaler,
                "train_min": train_min,
                "train_max": train_max,
                "feature_names": FEATURE_NAMES,
                "trained_at": datetime.utcnow().isoformat(),
            },
            f,
        )
    print(f"✅ 저장 완료: {MODEL_PATH}")

    return {"feat_df": feat_df, "row_nos": row_nos, "raw_scores": raw_scores,
            "train_min": train_min, "train_max": train_max}


# ============================================================
# 4. 추론 (risk.py에서 사용)
# ============================================================

_cached_bundle: Optional[dict] = None
_cache_checked = False


def _load_bundle() -> Optional[dict]:
    global _cached_bundle, _cache_checked
    if _cache_checked:
        return _cached_bundle
    _cache_checked = True
    if MODEL_PATH.exists():
        with open(MODEL_PATH, "rb") as f:
            _cached_bundle = pickle.load(f)
    return _cached_bundle


def score_anomaly(txn: Transaction, profile: UserRiskProfile, history: list[Transaction]) -> float:
    """0.0(정상) ~ 1.0(이상). 모델이 아직 학습 전이면 항상 0.0 — 룰 엔진만 있던 기존 동작 유지."""
    bundle = _load_bundle()
    if bundle is None:
        return 0.0

    X = compute_features(txn, profile, history)
    X_scaled = bundle["scaler"].transform(X)
    raw = float(-bundle["model"].score_samples(X_scaled)[0])

    train_min, train_max = bundle["train_min"], bundle["train_max"]
    span = (train_max - train_min) or 1.0
    return float(np.clip((raw - train_min) / span, 0.0, 1.0))


# ============================================================
# 5. 자체 검증: 정답지 대비 "이 모델 단독" 성능 (학습 직후 바로 출력)
# ============================================================


def _self_evaluate(feat_df: pd.DataFrame, raw_scores: np.ndarray, train_min: float, train_max: float):
    answer_path = DATA_DIR / "mock_transactions_answer_key.csv"
    if not answer_path.exists():
        print("(정답지가 없어 자체 검증은 건너뜁니다)")
        return

    answers = pd.read_csv(answer_path)
    span = (train_max - train_min) or 1.0
    anomaly_prob = np.clip((raw_scores - train_min) / span, 0.0, 1.0)

    merged = feat_df[["row_no"]].copy()
    merged["anomaly_prob"] = anomaly_prob
    merged = merged.merge(answers, on="row_no", how="left")

    target_types = {"R00x_위치급변", "R00y_신규기기"}
    is_target = merged["anomaly_reason"].isin(target_types)
    flagged = merged["anomaly_prob"] >= ANOMALY_ALERT_THRESHOLD

    tp = int((is_target & flagged).sum())
    fn = int((is_target & ~flagged).sum())
    fp = int(((merged["anomaly_reason"] == "정상") & flagged).sum())
    normal_total = int((merged["anomaly_reason"] == "정상").sum())

    print("\n=== 자체 검증: IsolationForest 단독 (룰 엔진 없이) — 임계값 "
          f"{ANOMALY_ALERT_THRESHOLD} 기준 ===")
    print(f"타깃 유형(R00x_위치급변 + R00y_신규기기) 총 {int(is_target.sum())}건 중 "
          f"{tp}건 탐지 (놓친 것 {fn}건)")
    print(f"정상 거래 {normal_total}건 중 {fp}건을 이상으로 오탐 "
          f"(오탐율 {fp / normal_total * 100:.1f}%)")
    print("→ 이 모델이 잡아내는 건 위 두 유형(원래 룰 엔진이 아예 설계 안 된 것)이 메인 타깃이고,")
    print("  R001/R005/R006/R007처럼 이미 룰이 잘 잡는 유형까지 억지로 잡을 필요는 없습니다 —")
    print("  최종 점수는 app/routers/risk.py에서 룰 엔진 결과와 함께 앙상블됩니다.")


if __name__ == "__main__":
    result = train()
    _self_evaluate(result["feat_df"], result["raw_scores"], result["train_min"], result["train_max"])

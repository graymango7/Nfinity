"""
예측 정확도 백테스트 API (9/6 신규) — GET /api/v1/validation/forecast

왜 만들었나
-----------
이 서비스의 차별점은 "플랫폼별 정산 주기와 반복 지출을 학습해 앞으로를 예측한다"는 것인데,
그 예측이 실제로 맞는지를 보여주는 수치가 없었습니다. 이상거래 탐지는 정답지 대비 성능
(탐지율 73.4%)을 제시할 수 있었지만, 정작 핵심 기능은 "그럴듯해 보인다" 이상의 근거가 없었던
셈입니다.

어떻게 검증하나 (홀드아웃 방식)
-------------------------------
45일 곡선을 만드는 예측은 두 가지 추론 위에 서 있습니다.

  (1) 수입: 플랫폼마다 "다음 정산은 언제, 얼마" — 과거 정산 간격의 중앙값으로 추정
  (2) 지출: 규칙적으로 반복되는 결제의 "다음 결제일" — 간격 중앙값으로 추정

두 경우 모두 **마지막 실제 발생 건을 감춘 뒤**, 그 이전 기록만으로 그 시점을 예측하고
실제와 비교합니다. 미래를 몰랐던 상태를 그대로 재현하는 것이라, 학습에 쓴 데이터로 자신을
평가하는 문제를 피할 수 있습니다.

무엇을 보고하나
---------------
- 정산일 예측 오차(일): 평균 절대 오차와 ±3일 이내 적중률
- 정산 금액 예측 오차(%): 직전 회차들의 평균으로 다음 금액을 추정했을 때의 절대 백분율 오차
- 반복 지출 탐지: 같은 방식으로 다음 결제일을 맞춘 비율

한계도 같이 밝힙니다: 표본은 시연용 가상 데이터이고, 플랫폼당 홀드아웃 1건씩이라
표본 수가 많지 않습니다. 실제 정산 데이터로 다시 측정해야 확정적인 수치가 됩니다.
"""
import statistics
from datetime import date, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import get_db
from app.demo_personas import DEMO_PERSONAS
from app.security import verify_api_key

router = APIRouter(prefix="/api/v1/validation", tags=["validation"], dependencies=[Depends(verify_api_key)])

HIT_TOLERANCE_DAYS = 3
MIN_HISTORY = 3  # 간격을 추정하려면 최소 3건(간격 2개)은 있어야 합니다


def _predict_next(dates: list[date], amounts: list[float]) -> tuple[date, float]:
    """마지막 건을 제외한 기록으로 '다음 발생일과 금액'을 추정합니다.
    현재 서비스가 쓰는 방식과 동일하게, 간격은 중앙값·금액은 최근 평균을 씁니다."""
    gaps = [(dates[i] - dates[i - 1]).days for i in range(1, len(dates))]
    interval = statistics.median(gaps)
    predicted_date = dates[-1] + timedelta(days=round(interval))
    predicted_amount = sum(amounts[-4:]) / len(amounts[-4:])
    return predicted_date, predicted_amount


def _backtest_income(db: Session, user_ids: list[str]) -> dict:
    rows = db.execute(
        text(
            """
            SELECT s.source_id, s.platform_name, e.settled_at, e.amount
            FROM income_events e
            JOIN income_sources s ON s.source_id = e.source_id
            WHERE s.user_id = ANY(:uids)
            ORDER BY s.source_id, e.settled_at
            """
        ),
        {"uids": user_ids},
    ).mappings().all()

    by_source: dict = {}
    for r in rows:
        by_source.setdefault(str(r["source_id"]), {"name": r["platform_name"], "events": []})
        by_source[str(r["source_id"])]["events"].append((r["settled_at"].date(), float(r["amount"])))

    date_errors, amount_errors, details = [], [], []
    for src in by_source.values():
        events = src["events"]
        if len(events) < MIN_HISTORY + 1:
            continue
        history, actual = events[:-1], events[-1]
        pred_date, pred_amount = _predict_next([d for d, _ in history], [a for _, a in history])

        d_err = abs((pred_date - actual[0]).days)
        a_err = abs(pred_amount - actual[1]) / actual[1] * 100 if actual[1] else 0.0
        date_errors.append(d_err)
        amount_errors.append(a_err)
        details.append(
            {
                "platform": src["name"],
                "predicted_date": pred_date.isoformat(),
                "actual_date": actual[0].isoformat(),
                "date_error_days": d_err,
                "amount_error_pct": round(a_err, 1),
            }
        )

    n = len(date_errors)
    return {
        "samples": n,
        "mean_absolute_error_days": round(sum(date_errors) / n, 2) if n else None,
        "within_3days_rate": round(sum(1 for e in date_errors if e <= HIT_TOLERANCE_DAYS) / n * 100, 1) if n else None,
        "mean_amount_error_pct": round(sum(amount_errors) / n, 1) if n else None,
        "details": details,
    }


def _backtest_recurring(db: Session, user_ids: list[str]) -> dict:
    """반복 결제로 판정된 (유저, 가맹점) 조합에 대해 같은 홀드아웃 검증을 수행합니다."""
    import pandas as pd

    from data_pipeline.recurring_detector import detect_recurring_payments

    rows = db.execute(
        text(
            "SELECT user_id, merchant_name, amount, timestamp FROM transactions "
            "WHERE user_id = ANY(:uids) ORDER BY timestamp"
        ),
        {"uids": user_ids},
    ).mappings().all()
    if not rows:
        return {"samples": 0, "mean_absolute_error_days": None, "within_3days_rate": None}

    df = pd.DataFrame([dict(r) for r in rows])
    detected = detect_recurring_payments(df)

    errors = []
    for _, rec in detected.iterrows():
        grp = df[(df["user_id"] == rec["user_id"]) & (df["merchant_name"] == rec["merchant_name"])]
        dates = sorted(pd.to_datetime(grp["timestamp"]).dt.date.tolist())
        if len(dates) < MIN_HISTORY + 1:
            continue
        pred_date, _ = _predict_next(dates[:-1], [1.0] * (len(dates) - 1))
        errors.append(abs((pred_date - dates[-1]).days))

    n = len(errors)
    return {
        "samples": n,
        "mean_absolute_error_days": round(sum(errors) / n, 2) if n else None,
        "within_3days_rate": round(sum(1 for e in errors if e <= HIT_TOLERANCE_DAYS) / n * 100, 1) if n else None,
    }


@router.get("/forecast")
def forecast_accuracy(db: Session = Depends(get_db)):
    """예측 엔진의 홀드아웃 검증 결과. 시연 인물 전체를 대상으로 계산합니다."""
    user_ids = [p["user_id"] for p in DEMO_PERSONAS]
    income = _backtest_income(db, user_ids)
    recurring = _backtest_recurring(db, user_ids)
    return {
        "method": (
            "각 플랫폼·가맹점의 마지막 발생 건을 감춘 뒤, 그 이전 기록만으로 발생일과 금액을 "
            "예측하고 실제와 비교했습니다(홀드아웃). 예측 방식은 서비스가 실제로 쓰는 것과 동일합니다."
        ),
        "income_settlement": income,
        "recurring_payment": recurring,
        "limitations": (
            "표본은 시연용 가상 데이터이며 대상별 홀드아웃 1건씩이라 표본 수가 제한적입니다. "
            "실제 정산 데이터 연동 후 재측정이 필요합니다."
        ),
    }

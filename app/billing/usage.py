import datetime

from firebase_admin import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from app.billing.plans import ModelEntry
from app.firebase import db


def _today() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%d")


def _this_month() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m")


def _counter_id(provider: str, model: str, period: str) -> str:
    # Firestore doc ids can't contain "/" - OpenRouter model slugs can (e.g. "org/model:free").
    # The id is write-target only; querying uses the "provider"/"model"/"period" fields below.
    return f"{provider}:{model}:{period}".replace("/", "_")


def _counters(uid: str):
    return db.collection("usage").document(uid).collection("counters")


def _sum_period(uid: str, period: str) -> dict:
    requests = tokens = 0
    for doc in _counters(uid).where(filter=FieldFilter("period", "==", period)).stream():
        data = doc.to_dict()
        requests += data.get("requests", 0)
        tokens += data.get("tokens", 0)
    return {"requests": requests, "tokens": tokens}


def within_tier_limits(uid: str, limits: dict) -> bool:
    daily = _sum_period(uid, _today())
    monthly = _sum_period(uid, _this_month())
    return daily["requests"] < limits["requests_per_day"] and monthly["tokens"] < limits["tokens_per_month"]


def has_model_quota(uid: str, entry: ModelEntry) -> bool:
    if entry.quota is None:
        return True
    daily = _counters(uid).document(_counter_id(entry.provider, entry.model, _today())).get()
    monthly = _counters(uid).document(_counter_id(entry.provider, entry.model, _this_month())).get()
    daily_requests = (daily.to_dict() or {}).get("requests", 0)
    monthly_tokens = (monthly.to_dict() or {}).get("tokens", 0)
    return daily_requests < entry.quota["requests_per_day"] and monthly_tokens < entry.quota["tokens_per_month"]


def record_usage(uid: str, entry: ModelEntry, usage: dict) -> None:
    tokens = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
    for period in (_today(), _this_month()):
        ref = _counters(uid).document(_counter_id(entry.provider, entry.model, period))
        ref.set(
            {
                "provider": entry.provider,
                "model": entry.model,
                "period": period,
                "requests": firestore.Increment(1),
                "tokens": firestore.Increment(tokens),
            },
            merge=True,
        )

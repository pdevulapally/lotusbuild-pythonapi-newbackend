"""Advisory selection reads only. Actual admission MUST use budgets.reserve.

V2 usage is separate from legacy counters: migrate before admitting existing
customers. Do not run legacy workers concurrently against these allowances.
"""
import datetime
from app.billing.budgets import budgets, _key, positive_int
from app.billing.plans import ModelEntry


def _today():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


def _this_month():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m")


def within_tier_limits(uid: str, limits: dict) -> bool:
    daily, monthly = budgets.snapshot(uid, _today()), budgets.snapshot(uid, _this_month())
    return (not daily.get("blocked") and not monthly.get("blocked")
            and daily.get("requests", 0) < positive_int(limits.get("requests_per_day"))
            and monthly.get("tokens", 0) < positive_int(limits.get("tokens_per_month")))


def has_model_quota(uid: str, entry: ModelEntry) -> bool:
    if entry.quota is None:
        return True
    key = _key(entry.provider, entry.model)
    daily = budgets.snapshot(uid, _today()).get("models", {}).get(key, {})
    monthly = budgets.snapshot(uid, _this_month()).get("models", {}).get(key, {})
    return (daily.get("requests", 0) < positive_int(entry.quota.get("requests_per_day"))
            and monthly.get("tokens", 0) < positive_int(entry.quota.get("tokens_per_month")))

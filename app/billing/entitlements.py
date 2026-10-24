from dataclasses import dataclass
import math
import time

from app.billing.plans import ModelEntry, get_plan
from app.firebase import db

DEFAULT_TIER = "free"
# Deliberately no implicit past-due/grace-period access. A future policy change
# must be explicit and tested. Cancellation at period end remains active until
# the paid-through boundary; canceled/unpaid/incomplete subscriptions do not.
ELIGIBLE_SUBSCRIPTION_STATUSES = frozenset({"active", "trialing"})


def _eligible_tier(subscription: dict, now: float) -> str:
    tier = subscription.get("tier_id")
    status = subscription.get("status")
    period_end = subscription.get("current_period_end")
    if (
        not isinstance(tier, str)
        or not tier
        or not isinstance(status, str)
        or status not in ELIGIBLE_SUBSCRIPTION_STATUSES
        or isinstance(period_end, bool)
        or not isinstance(period_end, (int, float))
        or not math.isfinite(period_end)
        or period_end <= now
    ):
        return DEFAULT_TIER
    return tier


@dataclass
class Entitlements:
    tier_id: str
    limits: dict
    models: list[ModelEntry]


def resolve_entitlements(uid: str) -> Entitlements:
    sub = db.collection("subscriptions").document(uid).get()
    tier_id = _eligible_tier(sub.to_dict() or {}, time.time()) if sub.exists else DEFAULT_TIER
    plan = get_plan(tier_id)
    return Entitlements(
        tier_id=tier_id,
        limits=plan["limits"],
        models=[ModelEntry(**m) for m in plan["models"]],
    )

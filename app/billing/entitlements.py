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
    capabilities: list | None = None
    meter_limits: list | None = None


def resolve_entitlements(uid: str, *, client=None, transaction=None, now=None) -> Entitlements:
    ref = (db if client is None else client).collection("subscriptions").document(uid)
    sub = ref.get() if transaction is None else ref.get(transaction=transaction)
    tier_id = _eligible_tier(sub.to_dict() or {}, time.time() if now is None else now) if sub.exists else DEFAULT_TIER
    try:
        plan = get_plan(tier_id) if client is None and transaction is None else get_plan(
            tier_id, client=client, transaction=transaction)
        return Entitlements(tier_id=tier_id, limits=plan["limits"],
                            models=[ModelEntry(**m) for m in plan["models"]], capabilities=plan.get("entitlements"), meter_limits=plan.get("meterLimits"))
    except (ValueError, TypeError, KeyError):
        from app.billing.model_select import SelectionError
        raise SelectionError(503, "session/model-unavailable", "Model entitlement configuration is unavailable.") from None

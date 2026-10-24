from dataclasses import dataclass

from app.billing.plans import ModelEntry, get_plan
from app.firebase import db

DEFAULT_TIER = "free"


@dataclass
class Entitlements:
    tier_id: str
    limits: dict
    models: list[ModelEntry]


def resolve_entitlements(uid: str) -> Entitlements:
    sub = db.collection("subscriptions").document(uid).get()
    tier_id = sub.to_dict().get("tier_id", DEFAULT_TIER) if sub.exists else DEFAULT_TIER
    plan = get_plan(tier_id)
    return Entitlements(
        tier_id=tier_id,
        limits=plan["limits"],
        models=[ModelEntry(**m) for m in plan["models"]],
    )

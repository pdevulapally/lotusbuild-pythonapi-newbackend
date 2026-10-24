from app.agent.providers import get_adapter
from app.billing.entitlements import Entitlements
from app.billing.plans import ModelEntry
from app.billing.usage import has_model_quota, within_tier_limits


class NoAvailableModelError(RuntimeError):
    pass


def pick_model(entitlements: Entitlements, uid: str, sandbox) -> ModelEntry:
    if not within_tier_limits(uid, entitlements.limits):
        raise NoAvailableModelError(f"uid {uid} exhausted tier {entitlements.tier_id!r} limits")

    needs_images = hasattr(sandbox, "screenshot")
    for entry in entitlements.models:
        if needs_images and not get_adapter(entry.provider).supports_images:
            continue
        if has_model_quota(uid, entry):
            return entry

    raise NoAvailableModelError(f"uid {uid} has no available model left in tier {entitlements.tier_id!r}")

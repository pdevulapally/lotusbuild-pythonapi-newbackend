from app.agent.providers import get_adapter
from app.billing.entitlements import Entitlements
from app.billing.plans import ModelEntry
from app.billing.usage import has_model_quota, within_tier_limits


from app.billing.budgets import BudgetExceeded


class SelectionError(BudgetExceeded):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code = status, code


def validate_selection(session, pending=None):
    from app.agent.policy import session_mode
    session_mode(session)
    model = session.get("model")
    if "model" in session and (type(model) is not str or not model or len(model) > 128):
        raise SelectionError(409, "session/selection-invalid", "Stored model selection is invalid.")
    if ("provider" in session) != ("model" in session) or (model is not None and
            (type(session.get("provider")) is not str or not session["provider"])):
        raise SelectionError(409, "session/selection-invalid", "Stored model selection is invalid.")
    if "effort" in session:
        reject_effort(session["effort"])
    if pending is not None:
        if any(type(pending.get(key)) is not str or not pending[key] for key in ("provider", "model")):
            raise SelectionError(409, "session/selection-invalid", "Approval model selection is invalid.")
        if model is not None and (pending.get("provider"), pending.get("model")) != (session["provider"], model):
            raise SelectionError(409, "session/selection-invalid", "Approval model selection has changed.")
        if "effort" in pending:
            reject_effort(pending["effort"])


def reject_effort(effort):
    # Native plans have no verified effort capabilities or paid effort ceiling.
    # Never infer either from a model ID, provider, seed plan or tier name.
    if effort not in ("low", "standard", "high", "xhigh") or type(effort) is not str:
        raise SelectionError(422, "request/invalid", "Invalid reasoning effort.")
    raise SelectionError(503, "session/model-unavailable",
                         "Reasoning effort capabilities and plan limits are not configured.")


def resolve_model(entitlements, model, *, provider=None):
    """Exact current entitlement selection, shared by admission and spend.

    Never uses quota fallback. Quota admission remains BudgetLedger.reserve.
    """
    matches = [m for m in entitlements.models if m.model == model and
               (provider is None or m.provider == provider)]
    if not matches:
        raise SelectionError(403, "session/model-not-permitted", "Selected model is no longer allowed by current entitlement.")
    if len(matches) != 1:
        raise SelectionError(503, "session/model-unavailable", "Selected model configuration is ambiguous.")
    return matches[0]


def require_model_kind(choice, kind):
    # Inspect declared adapter capabilities without constructing a client or
    # making provider catalog/discovery calls.
    from app.agent.providers import _ADAPTER_CLASSES
    adapter = _ADAPTER_CLASSES.get(choice.provider)
    if adapter is None or (kind == "desktop" and not adapter.supports_images):
        raise SelectionError(400, "session/model-unsupported", "Selected model provider does not support this session kind.")


def selected_model(session, entitlements, pending=None):
    validate_selection(session, pending)
    if session.get("model") is not None:
        return resolve_model(entitlements, session["model"], provider=session["provider"])
    if pending is not None:
        return resolve_model(entitlements, pending.get("model"), provider=pending.get("provider"))
    return None


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

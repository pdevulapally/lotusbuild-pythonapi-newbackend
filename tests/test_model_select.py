import os
from contextlib import contextmanager

os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("OPENROUTER_API_KEY", "test")

import app.billing.model_select as model_select
from app.billing.entitlements import Entitlements
from app.billing.model_select import NoAvailableModelError, pick_model
from app.billing.plans import ModelEntry


class FakeCodeSandbox:
    pass


class FakeDesktopSandbox:
    def screenshot(self):
        return b""


def _entitlements(models):
    return Entitlements(tier_id="test", limits={"requests_per_day": 1, "tokens_per_month": 1}, models=models)


@contextmanager
def _patched(within_tier_limits, has_model_quota):
    original = (model_select.within_tier_limits, model_select.has_model_quota)
    model_select.within_tier_limits = within_tier_limits
    model_select.has_model_quota = has_model_quota
    try:
        yield
    finally:
        model_select.within_tier_limits, model_select.has_model_quota = original


def test_pick_model_skips_exhausted_entry_then_returns_next():
    models = [ModelEntry("anthropic", "a", {}), ModelEntry("openrouter", "b", None)]
    with _patched(lambda uid, limits: True, lambda uid, entry: entry.model == "b"):
        choice = pick_model(_entitlements(models), "uid1", FakeCodeSandbox())
    assert choice.model == "b"


def test_pick_model_skips_non_image_provider_for_desktop_sandbox():
    models = [ModelEntry("openrouter", "b", None), ModelEntry("anthropic", "a", None)]
    with _patched(lambda uid, limits: True, lambda uid, entry: True):
        choice = pick_model(_entitlements(models), "uid1", FakeDesktopSandbox())
    assert choice.provider == "anthropic"


def test_pick_model_raises_when_tier_limit_exceeded():
    models = [ModelEntry("anthropic", "a", None)]
    with _patched(lambda uid, limits: False, lambda uid, entry: True):
        try:
            pick_model(_entitlements(models), "uid1", FakeCodeSandbox())
            assert False, "expected NoAvailableModelError"
        except NoAvailableModelError:
            pass


def test_pick_model_raises_when_every_model_exhausted():
    models = [ModelEntry("anthropic", "a", {}), ModelEntry("openrouter", "b", {})]
    with _patched(lambda uid, limits: True, lambda uid, entry: False):
        try:
            pick_model(_entitlements(models), "uid1", FakeCodeSandbox())
            assert False, "expected NoAvailableModelError"
        except NoAvailableModelError:
            pass


if __name__ == "__main__":
    test_pick_model_skips_exhausted_entry_then_returns_next()
    test_pick_model_skips_non_image_provider_for_desktop_sandbox()
    test_pick_model_raises_when_tier_limit_exceeded()
    test_pick_model_raises_when_every_model_exhausted()
    print("ok")

import pytest

from app.billing import entitlements


@pytest.mark.parametrize("status", ["active", "trialing"])
def test_current_eligible_subscription_gets_paid_tier(status):
    assert entitlements._eligible_tier(
        {"tier_id": "pro", "status": status, "current_period_end": 1001}, 1000
    ) == "pro"


@pytest.mark.parametrize("status", [None, [], {}, "past_due", "unpaid", "incomplete", "incomplete_expired", "canceled", "paused"])
def test_ineligible_status_downgrades_even_with_future_period(status):
    assert entitlements._eligible_tier(
        {"tier_id": "pro", "status": status, "current_period_end": 1001}, 1000
    ) == "free"


@pytest.mark.parametrize("period_end", [None, 999, 1000, "2000", True, float("nan"), float("inf")])
def test_missing_invalid_or_expired_period_downgrades(period_end):
    assert entitlements._eligible_tier(
        {"tier_id": "pro", "status": "active", "current_period_end": period_end}, 1000
    ) == "free"


def test_missing_subscription_and_invalid_tier_default_free():
    assert entitlements._eligible_tier({}, 1000) == "free"
    assert entitlements._eligible_tier(
        {"tier_id": [], "status": "active", "current_period_end": 1001}, 1000
    ) == "free"


def test_cancel_at_period_end_remains_paid_until_boundary():
    sub = {"tier_id": "pro", "status": "active", "current_period_end": 1001, "cancel_at_period_end": True}
    assert entitlements._eligible_tier(sub, 1000) == "pro"
    assert entitlements._eligible_tier(sub, 1001) == "free"


def test_resolver_loads_free_plan_for_invalid_subscription(monkeypatch):
    class DB:
        exists = True
        def collection(self, _): return self
        def document(self, _): return self
        def get(self): return self
        def to_dict(self): return {"tier_id": "pro", "status": "unpaid", "current_period_end": 9999999999}
    tiers = []
    def plan(tier):
        tiers.append(tier)
        return {"limits": {}, "models": []}
    monkeypatch.setattr(entitlements, "db", DB())
    monkeypatch.setattr(entitlements, "get_plan", plan)
    assert entitlements.resolve_entitlements("alice").tier_id == "free"
    assert tiers == ["free"]

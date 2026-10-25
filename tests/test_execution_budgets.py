"""Offline serialized-transaction invariants; not live billing/provider evidence."""
from concurrent.futures import ThreadPoolExecutor
import copy

import pytest

from app.billing.budgets import (BudgetLedger, BudgetExceeded, BUDGETS_COLLECTION,
    RESERVATIONS_COLLECTION, request_allowance, MAX_OUTPUT_TOKENS, _key)
from app.billing.entitlements import Entitlements
from app.billing.plans import ModelEntry
from app.execution.repository import CommandError, RunRepository, LEASE_SECONDS
from app.execution.slots import SLOTS_COLLECTION, MAX_ACTIVE_RUNS, MAX_SANDBOXES
from test_durable_runs import MemoryDB, create, idle, waiting, doc

ENTRY = ModelEntry("anthropic", "test", None)
LIMITS = {"requests_per_day": 100, "tokens_per_month": 1000}


@pytest.fixture
def ledger():
    db = MemoryDB()
    clock = [1727740799.0]  # 2024-09-30 just before midnight UTC
    return BudgetLedger(db, lambda: clock[0], db.transaction), db, clock


def test_concurrent_calls_cannot_spend_same_tier_allowance(ledger):
    ledger, db, _ = ledger
    def attempt(_):
        try:
            return ledger.reserve("u", ENTRY, LIMITS, 300)
        except BudgetExceeded:
            return None
    with ThreadPoolExecutor(16) as pool:
        accepted = [r for r in pool.map(attempt, range(32)) if r]
    assert len(accepted) == 3
    assert len([c for c, _ in db.data if c == RESERVATIONS_COLLECTION]) == 3
    assert all(d["tokens"] == 900 for (c, _), d in db.data.items() if c == BUDGETS_COLLECTION)


def test_model_limit_and_tier_limit_both_atomic(ledger):
    ledger, _, _ = ledger
    entry = ModelEntry("p", "m", {"requests_per_day": 1, "tokens_per_month": 500})
    ledger.reserve("u", entry, LIMITS, 300)
    with pytest.raises(BudgetExceeded, match="model"):
        ledger.reserve("u", entry, LIMITS, 300)
    # A different model can use remainder, but not overspend shared tier.
    ledger.reserve("u", ENTRY, LIMITS, 700)
    with pytest.raises(BudgetExceeded, match="tier"):
        ledger.reserve("u", ENTRY, LIMITS, 1)


def test_failed_transaction_has_no_partial_counters(ledger):
    ledger, db, _ = ledger
    with pytest.raises(BudgetExceeded):
        ledger.reserve("u", ENTRY, LIMITS, 1001)
    assert db.data == {}


def test_settle_once_across_month_boundary(ledger):
    ledger, db, clock = ledger
    rid = ledger.reserve("u", ENTRY, LIMITS, 900)
    clock[0] += 2
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(lambda _: ledger.settle(rid, {"input_tokens": 100, "output_tokens": 20}), range(8)))
    counters = [v for (c, _), v in db.data.items() if c == BUDGETS_COLLECTION]
    assert len(counters) == 2  # Not newly recreated in October.
    assert all(v["tokens"] == 120 and v["requests"] == 1 for v in counters)
    assert ledger.snapshot("u", "2024-09")["tokens"] == 120
    assert ledger.snapshot("u", "2024-10") == {}


@pytest.mark.parametrize("usage", [{}, {"input_tokens": -1, "output_tokens": 0},
    {"input_tokens": True, "output_tokens": 2}, {"input_tokens": 3.2, "output_tokens": 2}])
def test_untrusted_usage_never_refunds(ledger, usage):
    ledger, db, _ = ledger
    rid = ledger.reserve("u", ENTRY, LIMITS, 900)
    before = copy.deepcopy(db.data)
    with pytest.raises(BudgetExceeded):
        ledger.settle(rid, usage)
    assert db.data == before


def test_observed_overrun_blocks_more_work_and_is_idempotent(ledger):
    ledger, db, _ = ledger
    rid = ledger.reserve("u", ENTRY, LIMITS, 200)
    for _ in range(2):
        with pytest.raises(BudgetExceeded, match="exceeded"):
            ledger.settle(rid, {"input_tokens": 300, "output_tokens": 2})
    assert ledger.snapshot("u", "2024-09")["tokens"] == 302
    with pytest.raises(BudgetExceeded, match="reconciliation"):
        ledger.reserve("u", ENTRY, LIMITS, 1)


class Adapter:
    calls = 0
    def render(self, messages):
        return messages
    def create(self, **kw):
        self.calls += 1
        return object()
    def usage(self, response):
        return {"input_tokens": 10, "output_tokens": 2}


@pytest.mark.parametrize("messages,tools,max_tokens", [
    ([{"type": "image", "source": {"data": "x"}}], [], 1),
    ([], [{"type": "web_search_20260209"}], 1),
    ([{"text": "x" * 96001}], [], 1),
    ([], [], MAX_OUTPUT_TOKENS + 1),
])
def test_unsupported_or_unbounded_request_fails_before_provider(messages, tools, max_tokens):
    with pytest.raises(BudgetExceeded):
        request_allowance(Adapter(), "s", messages, tools, max_tokens)


def setup_metered(monkeypatch, ledger, models=None):
    import app.billing.budgets as module
    import app.billing.entitlements as ent
    import app.execution.context as context
    monkeypatch.setattr(module, "budgets", ledger)
    monkeypatch.setattr(context, "checkpoint", lambda: None)
    monkeypatch.setattr(ent, "resolve_entitlements", lambda uid: Entitlements(
        "free", {"requests_per_day": 2, "tokens_per_month": 100_000}, models or [ENTRY]))
    return module


def test_every_call_reserved_and_exception_retains_full_allowance(monkeypatch, ledger):
    ledger, db, _ = ledger
    module = setup_metered(monkeypatch, ledger)
    class Broken(Adapter):
        def create(self, **kw):
            # Reservation must have committed before even entering SDK call.
            assert any(c == RESERVATIONS_COLLECTION for c, _ in db.data)
            self.calls += 1
            raise TimeoutError("outcome unknown")
    adapter = Broken()
    for _ in range(2):
        with pytest.raises(TimeoutError):
            module.metered_create("u", adapter, ENTRY, system="s", messages=[], tools=[], max_tokens=10)
    with pytest.raises(BudgetExceeded):
        module.metered_create("u", adapter, ENTRY, system="s", messages=[], tools=[], max_tokens=10)
    assert adapter.calls == 2
    assert all(r["state"] == "reserved" for (c, _), r in db.data.items() if c == RESERVATIONS_COLLECTION)


def test_stale_pending_model_cannot_bypass_current_entitlements(monkeypatch, ledger):
    ledger, db, _ = ledger
    module = setup_metered(monkeypatch, ledger, [ModelEntry("other", "different", None)])
    adapter = Adapter()
    with pytest.raises(BudgetExceeded, match="no longer allowed"):
        module.metered_create("u", adapter, ENTRY, system="s", messages=[], tools=[], max_tokens=10)
    assert adapter.calls == 0
    assert db.data == {}


def test_current_model_quota_overrides_stale_saved_quota(monkeypatch, ledger):
    ledger, db, _ = ledger
    module = setup_metered(monkeypatch, ledger, [ModelEntry(ENTRY.provider, ENTRY.model,
        {"requests_per_day": 1, "tokens_per_month": 100_000})])
    adapter = Adapter()
    module.metered_create("u", adapter, ENTRY, system="s", messages=[], tools=[], max_tokens=10)
    with pytest.raises(BudgetExceeded, match="model"):
        module.metered_create("u", adapter, ENTRY, system="s", messages=[], tools=[], max_tokens=10)
    assert adapter.calls == 1


def repository():
    db, clock = MemoryDB(), [1000.0]
    return RunRepository(db, lambda: clock[0], db.transaction), db, clock


def test_concurrent_submission_reserves_slots_atomically():
    repo, db, _ = repository()
    def attempt(i):
        try:
            return create(repo, str(i))
        except CommandError as e:
            assert e.status == 429
    with ThreadPoolExecutor(16) as pool:
        accepted = [s for s in pool.map(attempt, range(32)) if s]
    assert len(accepted) == MAX_ACTIVE_RUNS
    slots = doc(db, SLOTS_COLLECTION, "alice")
    assert len(slots["runs"]) == len(slots["sandboxes"]) == MAX_ACTIVE_RUNS
    # Re-submit a known accepted command using its deterministic command id.
    receipt_id = accepted[0]["id"]
    from app.execution.repository import _digest
    matching_key = next(str(i) for i in range(32) if _digest(["alice", "create", None, str(i)]) == receipt_id)
    assert create(repo, matching_key) == accepted[0]
    assert doc(db, SLOTS_COLLECTION, "alice") == slots


def test_idle_and_approval_release_run_but_not_sandbox():
    repo, db, _ = repository()
    a, b = create(repo, "a"), create(repo, "b")
    idle(repo, a)
    waiting(repo, b)
    slots = doc(db, SLOTS_COLLECTION, "alice")
    assert slots["runs"] == []
    assert len(slots["sandboxes"]) == 2
    c = create(repo, "c")
    idle(repo, c)
    assert len(doc(db, SLOTS_COLLECTION, "alice")["sandboxes"]) == MAX_SANDBOXES
    with pytest.raises(CommandError, match="sandbox"):
        create(repo, "d")


def test_cancel_cleanup_releases_sandbox_only_after_confirmation():
    repo, db, _ = repository()
    a = create(repo)
    repo.cancel(a["id"], "alice")
    assert doc(db, SLOTS_COLLECTION, "alice")["runs"] == []
    assert doc(db, SLOTS_COLLECTION, "alice")["sandboxes"] == [a["id"]]
    repo.cleanup_once(lambda sid: None, "closer")
    assert doc(db, SLOTS_COLLECTION, "alice")["sandboxes"] == []


def test_interrupted_run_retains_both_slots_even_if_closer_returns():
    repo, db, clock = repository()
    a = create(repo)
    claim = repo.claim(a["active_run_id"], "w")
    repo.start(claim)
    before = doc(db, SLOTS_COLLECTION, "alice")
    clock[0] += LEASE_SECONDS + 1
    repo.claim(a["active_run_id"], "other")
    repo.cleanup_once(lambda sid: None, "closer")
    assert doc(db, SLOTS_COLLECTION, "alice") == before


def test_cancel_does_not_release_an_interrupted_run_slot():
    repo, db, clock = repository()
    a = create(repo)
    claim = repo.claim(a["active_run_id"], "w")
    repo.start(claim)
    clock[0] += LEASE_SECONDS + 1
    repo.claim(a["active_run_id"], "other")
    before = doc(db, SLOTS_COLLECTION, "alice")
    repo.cancel(a["id"], "alice")
    assert doc(db, SLOTS_COLLECTION, "alice") == before


def test_failed_cleanup_never_releases_capacity():
    repo, db, _ = repository()
    a = create(repo)
    repo.cancel(a["id"], "alice")
    before = doc(db, SLOTS_COLLECTION, "alice")
    def broken(sid):
        raise TimeoutError("cannot prove remote termination")
    repo.cleanup_once(broken, "closer")
    assert doc(db, SLOTS_COLLECTION, "alice") == before


def test_reservation_failure_prevents_provider_invocation(monkeypatch, ledger):
    ledger, db, _ = ledger
    module = setup_metered(monkeypatch, ledger)
    def broken(*args, **kwargs):
        raise RuntimeError("database unavailable")
    monkeypatch.setattr(ledger, "reserve", broken)
    adapter = Adapter()
    with pytest.raises(RuntimeError, match="database"):
        module.metered_create("u", adapter, ENTRY, system="s", messages=[], tools=[], max_tokens=10)
    assert adapter.calls == 0


def test_cancel_after_response_settles_usage_but_does_not_continue(monkeypatch, ledger):
    from app.execution.repository import CancelRequested
    import app.execution.context as context
    ledger, db, _ = ledger
    module = setup_metered(monkeypatch, ledger)
    checks = []
    def checkpoint():
        checks.append(1)
        if len(checks) == 3:
            raise CancelRequested()
    monkeypatch.setattr(context, "checkpoint", checkpoint)
    with pytest.raises(CancelRequested):
        module.metered_create("u", Adapter(), ENTRY, system="s", messages=[], tools=[], max_tokens=10)
    assert all(r["state"] == "settled" for (c, _), r in db.data.items() if c == RESERVATIONS_COLLECTION)


def test_sdk_timeouts_and_no_implicit_retries(monkeypatch):
    import app.agent.providers.anthropic_adapter as anthropic
    import app.agent.providers.openai_adapter as openai
    captured = []
    monkeypatch.setattr(anthropic.anthropic, "Anthropic", lambda **kw: captured.append(kw))
    monkeypatch.setattr(openai, "OpenAI", lambda **kw: captured.append(kw))
    anthropic.AnthropicAdapter()
    openai.OpenAIAdapter()
    assert captured == [{"timeout": 60.0, "max_retries": 0}] * 2


def test_anthropic_cached_tokens_are_counted():
    from types import SimpleNamespace
    from app.agent.providers.anthropic_adapter import AnthropicAdapter
    adapter = object.__new__(AnthropicAdapter)
    response = SimpleNamespace(usage=SimpleNamespace(input_tokens=4, output_tokens=2,
        cache_creation_input_tokens=20, cache_read_input_tokens=100))
    assert adapter.usage(response) == {"input_tokens": 124, "output_tokens": 2}


def test_sandbox_lifetime_is_bounded(monkeypatch):
    from app.sandbox.manager import _kind_config
    for value in ("0", "-1", "901"):
        monkeypatch.setenv("E2B_CODE_TIMEOUT", value)
        with pytest.raises(ValueError, match="lifetime"):
            _kind_config("code")
    monkeypatch.setenv("E2B_CODE_TIMEOUT", "300")
    assert _kind_config("code")["timeout"] == 300


def test_compaction_uses_authorized_turn_model_through_same_guard(monkeypatch):
    import app.agent.compaction as compaction
    seen = []
    adapter = Adapter()
    adapter.text = lambda _: "summary"
    monkeypatch.setattr(compaction, "get_adapter", lambda provider: adapter)
    def guarded(uid, passed_adapter, entry, **kwargs):
        seen.append((uid, entry, kwargs["max_tokens"]))
        return object()
    monkeypatch.setattr(compaction, "metered_create", guarded)
    paid = ModelEntry("openai", "paid-model", {"requests_per_day": 10, "tokens_per_month": 100000})
    compaction.compact({"uid": "u", "messages": [{"role": "user", "content": []}] * 20}, paid)
    assert seen == [("u", paid, 512)]


def test_default_compaction_starts_before_text_envelope_cap():
    from app.agent.compaction import needs_compaction
    messages = [{"role": "user", "content": [{"type": "text", "text": "x" * 2000}]}] * 20
    assert needs_compaction(messages)
    assert request_allowance(Adapter(), "small prompt", messages, [], 512) > 0


def test_provider_failure_during_cancellation_retains_unknown_capacity():
    from test_durable_runs import process_claim
    from app.store import SESSIONS_COLLECTION
    repo, db, _ = repository()
    a = create(repo)
    claim = repo.claim(a["active_run_id"], "w")
    before = doc(db, SLOTS_COLLECTION, "alice")
    def handler(job, session):
        repo.cancel(session["id"], "alice")
        raise TimeoutError("provision may still complete after cancellation")
    process_claim(repo, claim, handler)
    assert doc(db, SESSIONS_COLLECTION, a["id"])["recovery_required"]
    repo.cleanup_once(lambda sid: None, "closer")
    assert doc(db, SLOTS_COLLECTION, "alice") == before

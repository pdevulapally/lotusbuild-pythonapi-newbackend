"""Exact selection and authoritative Plan permissions; synthetic services only."""
import copy
from dataclasses import replace
from types import SimpleNamespace

import pytest

import app.agent.tools as tools
from app.agent.policy import ToolPolicyDenied
from app.billing.entitlements import Entitlements
from app.billing.model_select import SelectionError, resolve_model, selected_model
from app.billing.plans import ModelEntry
from app.execution.actions import ACTIONS_COLLECTION
from app.execution.context import Guard, execution_context
from app.execution.repository import CommandError, JOBS_COLLECTION, LEASE_SECONDS, LeaseLost, RunRepository
from app.store import SESSIONS_COLLECTION
from test_durable_runs import doc, env, idle, seal_pending
from test_security_diagnostics import client


MODELS = [ModelEntry("anthropic", "fixture-a", None), ModelEntry("openai", "fixture-b", None)]
LIMITS = {"requests_per_day": 100, "tokens_per_month": 100_000}
READ = {"id": "read", "name": "read_file", "input": {"path": "fixture.txt"}}
WRITE = {"id": "write", "name": "write_file", "input": {"path": "fixture.txt", "content": "fixture"}}


def seed(db, models=None, tier="free"):
    db.data[("plans", tier)] = {"limits": LIMITS, "models": [vars(m) for m in (MODELS if models is None else models)]}


def start(env, mode="build"):
    repo, db, _ = env
    seed(db)
    session = repo.submit("alice", "create", "create", {"task": "test", "kind": "code", "model": "fixture-a", "mode": mode})
    claim = repo.claim(session["active_run_id"], "worker")
    _, session = repo.start(claim)
    return claim, session


@pytest.mark.parametrize("metadata", [
    {"model": None}, {"model": False}, {"model": {}}, {"model": ""}, {"model": "x" * 129},
    {"mode": None}, {"mode": "PLAN"}, {"mode": False}, {"effort": None}, {"effort": True},
    {"effort": "medium"}, {"provider": "anthropic"}, {"selection": {"model": "fixture-a"}},
])
def test_http_invalid_metadata_is_shared_rejection_before_effects(client, monkeypatch, env, metadata):
    import app.api.routes as routes
    repo, db, _ = env
    monkeypatch.setattr(routes, "runs", repo)
    response = client.post("/sessions", json={"task": "test", **metadata},
                           headers={"Authorization": "Bearer synthetic", "Idempotency-Key": "create"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request/invalid"
    assert not db.data


@pytest.mark.parametrize("effort", ["low", "standard", "high", "xhigh"])
def test_missing_effort_capability_and_entitlement_metadata_fails_closed(client, monkeypatch, env, effort):
    import app.api.routes as routes
    repo, db, _ = env
    monkeypatch.setattr(routes, "runs", repo)
    response = client.post("/sessions", json={"task": "test", "model": "fixture-a", "effort": effort},
                           headers={"Authorization": "Bearer synthetic", "Idempotency-Key": "create"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "session/model-unavailable"
    assert "not configured" in response.json()["error"]["message"]
    assert not db.data  # no command, job, slot or reservation, even without plans


def test_http_exact_selection_and_entitlement_boundaries(client, monkeypatch, env):
    import app.api.routes as routes
    repo, db, _ = env
    seed(db)
    monkeypatch.setattr(routes, "runs", repo)
    headers = {"Authorization": "Bearer synthetic", "Idempotency-Key": "create"}
    before = copy.deepcopy(db.data)
    denied = client.post("/sessions", json={"task": "test", "model": "not-entitled"}, headers=headers)
    assert denied.status_code == 403 and denied.json()["error"]["code"] == "session/model-not-permitted"
    assert db.data == before
    accepted = client.post("/sessions", json={"task": "test", "model": "fixture-b", "mode": "plan"}, headers=headers)
    assert accepted.status_code == 200
    result = accepted.json()
    assert (result["model"], result["mode"], result["effort"]) == ("fixture-b", "plan", None)
    stored = doc(db, SESSIONS_COLLECTION, result["id"])
    assert stored["provider"] == "openai"
    assert repo.submit("alice", "create", "create", {"task": "test", "kind": "code", "model": "fixture-b", "mode": "plan"}) == stored
    # A different requested mode cannot borrow the original idempotent receipt.
    with pytest.raises(CommandError, match="different input"):
        repo.submit("alice", "create", "create", {"task": "test", "model": "fixture-b", "mode": "build"})


def test_exact_resolver_rejects_ambiguity_and_uses_current_quota():
    ent = Entitlements("fixture", LIMITS, list(MODELS))
    assert resolve_model(ent, "fixture-b").provider == "openai"
    with pytest.raises(SelectionError):
        resolve_model(ent, "unknown")
    ent.models.append(ModelEntry("openrouter", "fixture-a", None))
    with pytest.raises(SelectionError, match="ambiguous"):
        resolve_model(ent, "fixture-a")
    assert resolve_model(ent, "fixture-a", provider="anthropic") == MODELS[0]
    ent.models[0] = ModelEntry("anthropic", "fixture-a", {"requests_per_day": 1, "tokens_per_month": 1})
    assert resolve_model(ent, "fixture-a", provider="anthropic").quota["tokens_per_month"] == 1


def test_desktop_incompatible_model_rejects_before_job_or_client(env, monkeypatch):
    import app.agent.providers as providers
    repo, db, _ = env
    seed(db)
    before = copy.deepcopy(db.data)
    monkeypatch.setattr(providers, "get_adapter", lambda *a: pytest.fail("no client construction"))
    with pytest.raises(SelectionError, match="session kind"):
        repo.submit("alice", "create", "create", {"task": "test", "kind": "desktop", "model": "fixture-b"})
    assert db.data == before


def test_selection_survives_turn_mode_change_restart_and_recovery(env):
    repo, db, clock = env
    claim, session = start(env)
    session["status"] = "idle"
    repo.save(claim, session)
    repo.finish(claim)
    queued = repo.submit("alice", "message", "message", {"content": "inspect", "mode": "plan"}, session["id"])
    assert (queued["model"], queued["provider"], queued["mode"]) == ("fixture-a", "anthropic", "plan")
    restarted = RunRepository(db, clock=lambda: clock[0], transaction_runner=db.transaction)
    second = restarted.claim(queued["active_run_id"], "replacement")
    _, running = restarted.start(second)
    before = copy.deepcopy(db.data)
    with pytest.raises(CommandError, match="still active"):
        restarted.submit("alice", "busy-mode", "message", {"content": "change", "mode": "build"}, session["id"])
    assert db.data == before
    clock[0] += LEASE_SECONDS + 1
    assert restarted.claim(second.job_id, "third") is None
    recovery = doc(db, SESSIONS_COLLECTION, session["id"])
    assert recovery["recovery_required"] and recovery["model"] == "fixture-a" and recovery["mode"] == "plan"
    with pytest.raises(CommandError, match="reconciliation"):
        restarted.submit("alice", "after-recovery", "message", {"content": "retry"}, session["id"])
    with pytest.raises(CommandError) as error:
        restarted.submit("bob", "other-tenant", "message", {"content": "test"}, session["id"])
    assert error.value.status == 404


@pytest.mark.parametrize("field,value", [("model", "fixture-b"), ("provider", "openai"), ("mode", "build"),
                                         ("mode", None), ("effort", "high"), ("kind", "desktop")])
def test_child_cannot_change_execution_selection(env, field, value):
    repo, db, _ = env
    claim, session = start(env, "plan")
    before = copy.deepcopy(db.data)
    session[field] = value
    with pytest.raises((LeaseLost, ToolPolicyDenied, SelectionError)):
        repo.save(claim, session)
    assert db.data == before


def test_plan_mixed_batch_rejects_before_any_receipt_or_effect(env):
    repo, db, _ = env
    claim, session = start(env, "plan")
    before = copy.deepcopy(db.data)
    with pytest.raises(ToolPolicyDenied):
        repo.prepare_actions(claim, step=0, calls=[READ, WRITE])
    assert db.data == before
    assert not any(c == ACTIONS_COLLECTION for c, _ in db.data)


@pytest.mark.parametrize("name,input", [("bash", {"command": "cat fixture.txt"}), ("git_diff", {}),
    ("remember", {"note": "fixture"}), ("update_plan", {"steps": []}), ("screenshot", {}),
    ("write_file", WRITE["input"]), ("batch", {"calls": [READ, WRITE]}),
    ("read_file_alias", READ["input"])])
def test_plan_direct_dispatch_and_aliases_deny(env, monkeypatch, name, input):
    repo, db, _ = env
    claim, session = start(env, "plan")
    effects = []
    read = tools.tool_spec("read_file")
    monkeypatch.setattr(tools, "TOOL_SPECS", tools.TOOL_SPECS + [replace(read, name="read_file_alias",
                                             handler=lambda *a: effects.append(True))])
    with execution_context(Guard(repo, claim)), pytest.raises((ToolPolicyDenied, ValueError)):
        tools.dispatch_tool_call(session, object(), name, input)
    assert not effects


def test_read_capability_allowed_with_receipt_and_forged_build_denied_by_parent(env):
    repo, db, _ = env
    claim, session = start(env, "plan")
    batch = repo.prepare_actions(claim, step=0, calls=[READ])
    session.update(batch["session"])
    sandbox = SimpleNamespace(files=SimpleNamespace(read=lambda path: "fixture content"))
    with execution_context(Guard(repo, claim)):
        assert tools.run_tool(session, sandbox, "read_file", READ["input"], batch["action_ids"][0]) == "fixture content"
    assert repo.inspect_action(session["id"], "alice", batch["action_ids"][0])["action"]["state"] == "handler_returned"
    forged = {**session, "mode": "build"}
    # No valid mutation receipt can be sealed under Plan. Even a forged local
    # Build snapshot cannot obtain a parent permit for a direct handler call.
    with execution_context(Guard(repo, claim)), pytest.raises(ToolPolicyDenied):
        tools.run_tool(forged, object(), WRITE["name"], WRITE["input"], batch["action_ids"][0])


def test_parent_rechecks_policy_for_prepared_and_approved_actions(env):
    repo, db, _ = env
    claim, session = start(env)
    call = {"id": "risk", "name": "bash", "input": {"command": "sudo fixture"}, "risky": True}
    seal_pending(repo, claim, session, {"id": "approval", "provider": "anthropic", "model": "fixture-a",
                                       "quota": None, "steps_used": 0, "calls": [call]})
    repo.save(claim, session)
    approved = repo.submit("alice", "approve", "approve", {"approval_id": "approval",
                          "decisions": [{"call_id": "risk", "approved": True}]}, session["id"])
    second = repo.claim(approved["active_run_id"], "next")
    repo.start(second)
    # Simulate restored Plan state containing a historical prepared Build batch.
    # Approval is not authority to bypass the current persisted tool policy.
    db.data[(SESSIONS_COLLECTION, session["id"])]["mode"] = "plan"
    before = copy.deepcopy(db.data)
    with pytest.raises(ToolPolicyDenied):
        repo.permit_action(second, action_id=call["action_id"], name=call["name"], input=call["input"])
    assert db.data == before


def test_pending_selection_tamper_and_revoked_entitlement_prevent_approval_effects(env, monkeypatch):
    import app.agent.loop as loop
    repo, db, _ = env
    claim, session = start(env)
    call = {"id": "risk", "name": "bash", "input": {"command": "sudo fixture"}, "risky": True}
    pending = {"id": "approval", "provider": "anthropic", "model": "fixture-a", "quota": None,
               "steps_used": 0, "calls": [call]}
    seal_pending(repo, claim, session, pending)
    session["pending_approval"]["model"] = "fixture-b"
    with pytest.raises(SelectionError, match="changed"):
        repo.save(claim, session)
    session["pending_approval"]["model"] = "fixture-a"
    repo.save(claim, session)
    queued = repo.submit("alice", "approve", "approve", {"approval_id": "approval",
                          "decisions": [{"call_id": "risk", "approved": True}]}, session["id"])
    resumed = repo.claim(queued["active_run_id"], "second")
    _, session = repo.start(resumed)
    seed(db, [MODELS[1]])
    before = copy.deepcopy(db.data)
    monkeypatch.setattr(loop.entitlement_service, "resolve_entitlements", lambda uid, **kwargs: Entitlements("free", LIMITS, [MODELS[1]]))
    monkeypatch.setattr(loop, "dispatch_tool_call", lambda *a: pytest.fail("revoked approval cannot execute"))
    monkeypatch.setattr(loop, "get_adapter", lambda *a: pytest.fail("revoked approval cannot construct provider"))
    with execution_context(Guard(repo, resumed)), pytest.raises(SelectionError):
        loop.resume_turn(session, [{"call_id": "risk", "approved": True}])
    with pytest.raises(SelectionError):
        repo.permit_action(resumed, action_id=call["action_id"], name=call["name"], input=call["input"])
    assert db.data == before


def test_legacy_missing_fields_preserve_native_path_and_malformed_state_never_defaults():
    ent = Entitlements("free", LIMITS, MODELS)
    assert selected_model({"uid": "alice"}, ent) is None
    for state in ({"model": None}, {"provider": "anthropic"}, {"mode": "PLAN"}, {"mode": None}, {"effort": "high"}):
        with pytest.raises((SelectionError, ToolPolicyDenied)):
            selected_model(state, ent)


def test_historical_http_create_receipt_replays_without_fingerprint_drift(client, monkeypatch, env):
    import app.api.routes as routes
    repo, db, _ = env
    old = repo.submit("alice", "historical", "create", {"task": "test", "kind": "code"})
    before = copy.deepcopy(db.data)
    monkeypatch.setattr(routes, "runs", repo)
    response = client.post("/sessions", json={"task": "test"}, headers={
        "Authorization": "Bearer synthetic", "Idempotency-Key": "historical"})
    assert response.status_code == 200 and response.json()["id"] == old["id"]
    assert db.data == before


def test_legacy_approval_current_entitlement_is_checked_by_parent(env):
    from test_durable_runs import create, waiting
    repo, db, _ = env
    state = waiting(repo, create(repo))
    assert "model" not in state
    queued = repo.submit("alice", "approve", "approve", {"approval_id": "batch-1",
                        "decisions": [{"call_id": "call-1", "approved": True}]}, state["id"])
    claim = repo.claim(queued["active_run_id"], "next")
    repo.start(claim)
    tier = db.data[("subscriptions", "alice")]["tier_id"]
    db.data[("plans", tier)]["models"] = []
    before = copy.deepcopy(db.data)
    call = state["pending_approval"]["calls"][0]
    with pytest.raises(SelectionError):
        repo.permit_action(claim, action_id=call["action_id"], name=call["name"], input=call["input"])
    assert db.data == before


def test_missing_config_is_clear_shared_error_before_any_job(client, monkeypatch, env):
    import app.api.routes as routes
    repo, db, _ = env
    monkeypatch.setattr(routes, "runs", repo)
    response = client.post("/sessions", json={"task": "test", "model": "fixture-a"}, headers={
        "Authorization": "Bearer synthetic", "Idempotency-Key": "create"})
    assert response.status_code == 503 and response.json()["error"]["code"] == "session/model-unavailable"
    assert response.json()["error"]["message"] == "Model entitlement configuration is unavailable."
    assert not db.data


def test_plan_schemas_are_same_enforced_capability_subset():
    from app.agent.loop import _tools_for
    adapter = SimpleNamespace(tool_schemas=lambda specs, **kwargs: [s.name for s in specs])
    assert _tools_for(adapter, object(), {"mode": "plan"}) == ["read_file"]
    assert "bash" in _tools_for(adapter, object(), {"mode": "build"})


def test_explicit_loop_selection_does_not_call_fallback_or_other_provider(env, monkeypatch):
    import app.agent.loop as loop
    repo, db, _ = env
    claim, session = start(env)
    seen = []
    monkeypatch.setattr(loop.manager, "get", lambda _: object())
    monkeypatch.setattr(loop.entitlement_service, "resolve_entitlements", lambda uid, **kwargs: Entitlements("free", LIMITS, MODELS))
    monkeypatch.setattr(loop, "pick_model", lambda *a: pytest.fail("explicit request cannot fallback"))
    adapter = SimpleNamespace(tool_schemas=lambda *a, **kw: [])
    monkeypatch.setattr(loop, "get_adapter", lambda provider: seen.append(provider) or adapter)
    monkeypatch.setattr(loop, "_agent_loop", lambda session, adapter, choice, tools: choice.model)
    with execution_context(Guard(repo, claim)):
        assert loop.run_turn(session, system_prompt="fixture") == "fixture-a"
    assert seen == ["anthropic"]


def test_selected_quota_exhaustion_and_compaction_never_fall_back(env, monkeypatch):
    import app.agent.compaction as compaction
    import app.billing.budgets as budget_module
    import app.billing.entitlements as entitlement_module
    from app.billing.budgets import BudgetLedger, BudgetExceeded, RESERVATIONS_COLLECTION
    repo, db, clock = env
    claim, session = start(env)
    choice = ModelEntry("anthropic", "fixture-a", {"requests_per_day": 1, "tokens_per_month": 100_000})
    ledger = BudgetLedger(db, lambda: clock[0], db.transaction)
    monkeypatch.setattr(budget_module, "budgets", ledger)
    monkeypatch.setattr(entitlement_module, "resolve_entitlements", lambda _, **kwargs: Entitlements("free", LIMITS, [choice, MODELS[1]]))
    calls = []
    adapter = SimpleNamespace(render=lambda messages: messages, create=lambda **kwargs: calls.append(kwargs) or "response",
                              usage=lambda _: {"input_tokens": 1, "output_tokens": 1}, text=lambda _: "summary")
    monkeypatch.setattr(compaction, "get_adapter", lambda provider: adapter)
    with execution_context(Guard(repo, claim)):
        assert compaction._summarize([], "alice", choice) == "summary"
        with pytest.raises(BudgetExceeded):
            budget_module.metered_create("alice", adapter, choice, system="test", messages=[], tools=[], max_tokens=10)
    assert [c["model"] for c in calls] == ["fixture-a"]
    assert len([1 for c, _ in db.data if c == RESERVATIONS_COLLECTION]) == 1

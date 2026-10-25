from tool_unit_adapter import handler_receipt_adapter
"""Authority, uncertainty and payload bounds at the real shared boundaries."""
import copy
from dataclasses import replace
from types import SimpleNamespace

import pytest

import app.agent.tools as tools
from app.execution.errors import OutputLimitExceeded, ToolOutcomeUncertain
from app.execution.limits import bounded_json, MAX_TOOL_OUTPUT_BYTES, MAX_SESSION_BYTES
from app.execution.repository import (
    CANCEL_GRACE_SECONDS, CLEANUP_COLLECTION, EVENTS_COLLECTION, JOBS_COLLECTION,
    CancelRequested, LeaseLost,
)
from app.execution.slots import SLOTS_COLLECTION
from app.store import SESSIONS_COLLECTION
from test_durable_runs import create, doc, env, seal_pending


def running(env, monkeypatch):
    import app.execution.repository as module
    monkeypatch.setattr(module, "MAX_RUN_SECONDS", 20)
    repo, _, _ = env
    session = create(repo)
    claim = repo.claim(session["active_run_id"], "worker")
    _, state = repo.start(claim)
    return session, claim, state


@pytest.mark.parametrize("operation", ["checkpoint", "heartbeat", "save", "finish"])
@pytest.mark.parametrize("offset", [0, 1])
def test_deadline_revocation_commits_before_rejection_without_heartbeat(env, monkeypatch, operation, offset):
    repo, db, clock = env
    session, claim, state = running(env, monkeypatch)
    clock[0] += 20 + offset
    before = len(repo.event_history(session["id"], "alice"))
    with pytest.raises(LeaseLost):
        if operation == "save":
            state["status"] = "idle"
            repo.save(claim, state)
        elif operation == "finish":
            repo.finish(claim, failed=True)
        else:
            getattr(repo, operation)(claim)
    current = doc(db, SESSIONS_COLLECTION, session["id"])
    assert current["status"] == "error" and current["recovery_required"]
    assert doc(db, JOBS_COLLECTION, claim.job_id)["state"] == "interrupted"
    assert doc(db, CLEANUP_COLLECTION, session["id"])["state"] == "queued"
    assert doc(db, SLOTS_COLLECTION, "alice")["runs"] == [claim.job_id]
    assert len(repo.event_history(session["id"], "alice")) == before + 1
    with pytest.raises(LeaseLost):
        repo.checkpoint(claim)
    assert len(repo.event_history(session["id"], "alice")) == before + 1


def test_checkpoint_before_deadline_remains_authorized(env, monkeypatch):
    repo, _, clock = env
    _, claim, _ = running(env, monkeypatch)
    clock[0] += 19
    repo.checkpoint(claim)
    repo.heartbeat(claim)


def test_cancel_grace_is_enforced_at_checkpoint_and_retains_uncertain_capacity(env, monkeypatch):
    repo, db, clock = env
    session, claim, _ = running(env, monkeypatch)
    repo.cancel(session["id"], "alice")
    with pytest.raises(CancelRequested):
        repo.checkpoint(claim)
    clock[0] += CANCEL_GRACE_SECONDS
    with pytest.raises(LeaseLost):
        repo.checkpoint(claim)
    current = doc(db, SESSIONS_COLLECTION, session["id"])
    assert current["status"] == "cancelled" and current["recovery_required"]
    assert doc(db, SLOTS_COLLECTION, "alice")["runs"] == [claim.job_id]


@pytest.mark.parametrize("expired_grace", [False, True])
def test_deadline_cancel_overlap_preserves_existing_precedence(env, monkeypatch, expired_grace):
    repo, db, clock = env
    session, claim, _ = running(env, monkeypatch)
    if not expired_grace:
        clock[0] += 19
    repo.cancel(session["id"], "alice")
    clock[0] = 1020.0
    with pytest.raises(LeaseLost):
        repo.checkpoint(claim)
    current = doc(db, SESSIONS_COLLECTION, session["id"])
    assert current["status"] == ("cancelled" if expired_grace else "error")
    assert current["recovery_required"] and doc(db, SLOTS_COLLECTION, "alice")["runs"] == [claim.job_id]


def test_failed_revocation_transaction_does_not_partially_publish_stop(env, monkeypatch):
    repo, db, clock = env
    session, claim, _ = running(env, monkeypatch)
    clock[0] += 20
    before = copy.deepcopy(db.data)

    def failed_transaction(callback):
        def abort(tx):
            callback(tx)
            raise RuntimeError("synthetic commit failure")
        return db.transaction(abort)

    repo.transaction_runner = failed_transaction
    with pytest.raises(RuntimeError, match="commit failure"):
        repo.checkpoint(claim)
    assert db.data == before
    repo.transaction_runner = db.transaction
    with pytest.raises(LeaseLost):
        repo.checkpoint(claim)
    assert doc(db, SESSIONS_COLLECTION, session["id"])["recovery_required"]


_EFFECTFUL = [spec.name for spec in tools.TOOL_SPECS if spec.enabled and spec.effectful]
_EFFECTFUL += [spec.name for spec in tools.VIRTUAL_TOOL_SPECS if spec.effectful]
_INPUTS = {
    "bash": {"command": "synthetic"}, "write_file": {"path": "a", "content": "x"},
    "expose_port": {"port": 3000}, "click": {"x": 1, "y": 2}, "move_mouse": {"x": 1, "y": 2},
    "type_text": {"text": "synthetic"}, "press_key": {"key": "Enter"},
    "scroll": {"direction": "down", "ticks": 1}, "git_commit": {"message": "synthetic"},
    "git_diff": {}, "remember": {"note": "synthetic"},
}


@pytest.mark.parametrize("name", _EFFECTFUL)
def test_effectful_handler_exception_is_an_uncertain_control_stop(monkeypatch, name):
    calls = []

    def effect_then_timeout(*args):
        calls.append(name)
        raise TimeoutError("synthetic-private-provider-diagnostic")

    for field in ("TOOL_SPECS", "VIRTUAL_TOOL_SPECS"):
        monkeypatch.setattr(tools, field, [replace(spec, handler=effect_then_timeout,
            **({"supported": lambda _: True} if isinstance(spec, tools.ToolSpec) else {})) if spec.name == name else spec
            for spec in getattr(tools, field)])
    with pytest.raises(ToolOutcomeUncertain) as stopped:
        tools.dispatch_tool_call({"uid": "alice"}, object(), name, _INPUTS[name])
    assert calls == [name]
    assert stopped.value.recovery == {"reason": "tool_outcome_uncertain", "tool": name}
    assert "private" not in str(stopped.value)


@pytest.mark.parametrize("value", ["x" * (MAX_TOOL_OUTPUT_BYTES + 1), float("nan"), float("inf"), object(), {1: "bad-key"}, 2 ** 63, -(2 ** 63) - 1])
def test_non_json_or_oversized_output_fails_closed(value):
    with pytest.raises(OutputLimitExceeded):
        bounded_json(value, MAX_TOOL_OUTPUT_BYTES)


def test_deep_and_cyclic_payloads_are_rejected_without_serialization():
    cycle = []
    cycle.append(cycle)
    deep = []
    for _ in range(40):
        deep = [deep]
    for value in (cycle, deep):
        with pytest.raises(OutputLimitExceeded):
            bounded_json(value, MAX_TOOL_OUTPUT_BYTES)


def test_screenshot_limit_precedes_base64_allocation(monkeypatch):
    monkeypatch.setattr(tools.base64, "b64encode", lambda _: pytest.fail("must reject raw bytes first"))
    sandbox = SimpleNamespace(screenshot=lambda: b"x" * (MAX_TOOL_OUTPUT_BYTES + 1))
    with pytest.raises(OutputLimitExceeded):
        tools.run_tool({}, sandbox, "screenshot", {})


def test_known_pre_dispatch_rejections_do_not_become_uncertain():
    for name, error in (("create_pull_request", PermissionError), ("unknown-tool", ValueError), ("screenshot", ValueError)):
        with pytest.raises(error):
            tools.run_tool({}, object(), name, {})


def test_observation_failure_is_sanitized_without_uncertain_effect(monkeypatch):
    def fail(*args):
        raise ValueError("synthetic-private-observation-diagnostic")
    monkeypatch.setattr(tools, "TOOL_SPECS", [replace(spec, handler=fail) if spec.name == "read_file" else spec for spec in tools.TOOL_SPECS])
    with pytest.raises(LeaseLost, match="observation handler did not return") as error:
        tools.run_tool({}, object(), "read_file", {"path": "a"})
    assert "private" not in str(error.value)


def test_oversized_worker_session_never_reaches_durable_storage(env, monkeypatch):
    repo, db, _ = env
    session, claim, current = running(env, monkeypatch)
    before = copy.deepcopy(db.data)
    current["messages"].append({"role": "assistant", "content": "x" * MAX_SESSION_BYTES})
    with pytest.raises(OutputLimitExceeded):
        repo.save(claim, current)
    assert db.data == before
    assert doc(db, SESSIONS_COLLECTION, session["id"])["status"] == "running"


def test_known_command_exit_is_a_completed_result_not_an_unknown_effect():
    def run(*args, **kwargs):
        raise tools.CommandExitException(stderr="synthetic error", stdout="", exit_code=2, error=None)
    sandbox = SimpleNamespace(commands=SimpleNamespace(run=run))
    assert tools.run_tool({}, sandbox, "bash", {"command": "synthetic"}) == "synthetic error"


@pytest.mark.parametrize("phase", ["ordinary", "approval"])
@pytest.mark.parametrize("outcome", ["timeout", "cancel_timeout", "oversized", "deadline_late"])
def test_tool_stop_prevents_later_tools_or_models_and_preserves_settled_usage(env, monkeypatch, caplog, phase, outcome):
    import app.agent.loop as loop
    import app.billing.budgets as billing
    import app.billing.entitlements as entitlements
    from app.agent.providers.base import ToolCall
    from app.billing.budgets import BudgetLedger, RESERVATIONS_COLLECTION
    from app.billing.entitlements import Entitlements
    from app.billing.plans import ModelEntry
    from test_durable_runs import process_claim

    repo, db, clock = env
    import app.execution.repository as repository
    monkeypatch.setattr(repository, "MAX_RUN_SECONDS", 20)
    session = create(repo)
    entry = ModelEntry("test-provider", "test-model", None)
    ledger = BudgetLedger(db, lambda: clock[0], db.transaction)
    monkeypatch.setattr(billing, "budgets", ledger)
    monkeypatch.setattr(entitlements, "resolve_entitlements", lambda _, **kwargs: Entitlements(
        "free", {"requests_per_day": 100, "tokens_per_month": 1_000_000}, [entry]))
    monkeypatch.setattr(loop, "needs_compaction", lambda _: False)
    monkeypatch.setattr(loop, "record_step", lambda *args, **kwargs: None)
    monkeypatch.setattr(loop.manager, "get", lambda _: object())
    name = "write_file" if phase == "ordinary" else "bash"
    call_input = {"path": "synthetic", "content": "x"} if name == "write_file" else {"command": "sudo synthetic"}
    calls = [ToolCall("effect", name, call_input), ToolCall("later", "read_file", {"path": "a"})]
    dispatched = []

    def effect(*args):
        dispatched.append(name)
        if outcome == "cancel_timeout":
            repo.cancel(session["id"], "alice")
        if outcome == "oversized":
            return "synthetic-private-output" * MAX_TOOL_OUTPUT_BYTES
        if outcome == "deadline_late":
            clock[0] += 20
            return "late completed output"
        raise TimeoutError("synthetic-private-provider-error")

    def later(*args):
        pytest.fail("remaining batch must stop")

    monkeypatch.setattr(tools, "TOOL_SPECS", [
        replace(spec, handler=effect) if spec.name == name else
        replace(spec, handler=later) if spec.name == "read_file" else spec for spec in tools.TOOL_SPECS])

    class Adapter:
        model_calls = 0

        def render(self, messages):
            return messages

        def create(self, **kwargs):
            self.model_calls += 1
            return object()

        def usage(self, response):
            return {"input_tokens": 1, "output_tokens": 1}

        def assistant_message(self, response):
            return {"role": "assistant", "content": [
                {"type": "tool_use", "id": call.id, "name": call.name, "input": call.input} for call in calls]}

        def is_tool_use(self, response):
            return True

        def tool_calls(self, response):
            return calls

        def tool_result_messages(self, results):
            pytest.fail("uncertain/oversized result must never reach transcript construction")

    adapter = Adapter()
    monkeypatch.setattr(loop, "get_adapter", lambda _: adapter)
    if phase == "approval":
        old = repo.claim(session["active_run_id"], "setup")
        _, waiting = repo.start(old)
        waiting["system_prompt"] = "test"
        seal_pending(repo, old, waiting, {
            "id": "batch", "provider": entry.provider, "model": entry.model, "quota": None, "steps_used": 0,
            "calls": [{"id": c.id, "name": c.name, "input": c.input, "risky": c.id == "effect"} for c in calls]})
        repo.save(old, waiting)
        repo.finish(old)
        rid = ledger.reserve("alice", entry, {"requests_per_day": 100, "tokens_per_month": 1_000_000}, 100)
        ledger.settle(rid, {"input_tokens": 1, "output_tokens": 1})
        session = repo.submit("alice", "approval", "approve", {
            "approval_id": "batch", "decisions": [{"call_id": "effect", "approved": True}]}, session["id"])
    claim = repo.claim(session["active_run_id"], "worker")

    def handler(job, state):
        if phase == "approval":
            loop.resume_turn(state, job["payload"]["decisions"])
        else:
            state["system_prompt"] = "test"
            loop._agent_loop(state, adapter, entry, [])

    process_claim(repo, claim, handler)
    current = doc(db, SESSIONS_COLLECTION, session["id"])
    assert current["status"] == ("cancelled" if outcome == "cancel_timeout" else "error")
    assert current["recovery_required"] and current["recovery"]["reason"] == (
        "output_limit_exceeded" if outcome == "oversized" else
        "run_deadline_reached" if outcome == "deadline_late" else "tool_outcome_uncertain")
    assert dispatched == [name] and adapter.model_calls == (0 if phase == "approval" else 1)
    assert doc(db, SLOTS_COLLECTION, "alice")["runs"] == [claim.job_id]
    assert doc(db, CLEANUP_COLLECTION, session["id"])["state"] == "queued"
    receipts = [value for (collection, _), value in db.data.items() if collection == RESERVATIONS_COLLECTION]
    assert len(receipts) == 1 and receipts[0]["state"] == "settled" and receipts[0]["actual_tokens"] == 2
    assert "synthetic-private" not in caplog.text
    assert "synthetic-private" not in str(repo.event_history(session["id"], "alice"))


def test_batch_output_limit_stops_before_appending_full_combined_transcript():
    import app.agent.loop as loop
    results = []
    loop._append_result(results, ("one", "x" * 20_000, False))
    with pytest.raises(OutputLimitExceeded):
        loop._append_result(results, ("two", "x" * 20_000, False))


def test_oversized_command_output_retains_the_resolved_tool_name():
    sandbox = SimpleNamespace(commands=SimpleNamespace(run=lambda *args, **kwargs:
        SimpleNamespace(stdout="x" * 40_000, stderr="", exit_code=0)))
    with pytest.raises(OutputLimitExceeded) as stopped:
        tools.run_tool({}, sandbox, "bash", {"command": "synthetic"})
    assert stopped.value.recovery["tool"] == "bash"


@pytest.mark.parametrize("input", [None, [], {}, {"steps": "not-a-list"},
    {"steps": [{"step": 42, "status": "pending"}]}, {"steps": [{"step": "x", "status": "unknown"}]}])
def test_registry_schema_rejects_malformed_args_before_local_state_changes(input):
    session = {"plan": None}
    with pytest.raises(ValueError, match="invalid tool arguments"):
        tools.dispatch_tool_call(session, None, "update_plan", input)
    assert session == {"plan": None}


def test_schema_additional_properties_policy_is_derived_from_spec(monkeypatch):
    calls = []
    spec = tools.ToolSpec("synthetic", "test", {
        "type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"],
        "additionalProperties": False}, lambda *args: calls.append(True))
    monkeypatch.setattr(tools, "TOOL_SPECS", [spec])
    with pytest.raises(ValueError, match="invalid tool arguments"):
        tools.run_tool({}, None, "synthetic", {"value": "ok", "unexpected": "synthetic-private-input"})
    assert calls == []

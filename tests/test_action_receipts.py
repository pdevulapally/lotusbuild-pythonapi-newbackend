"""Crash windows and receipt authority, with no real external effects."""
import copy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest

import app.agent.tools as tools
from app.execution.actions import ACTIONS_COLLECTION, BATCHES_COLLECTION, EVIDENCE_COLLECTION
from app.execution.context import Guard, execution_context
from app.execution.repository import CommandError, JOBS_COLLECTION, LeaseLost, RunRepository, _digest, LEASE_SECONDS
from app.execution.slots import SLOTS_COLLECTION
from app.store import SESSIONS_COLLECTION
from test_durable_runs import create, doc, env, seal_pending

CALL = {"id": "one", "name": "write_file", "input": {"path": "synthetic", "content": "synthetic-private-input"}}


def prepared(env, calls=None):
    repo, db, clock = env
    session = create(repo)
    claim = repo.claim(session["active_run_id"], "worker")
    repo.start(claim)
    batch = repo.prepare_actions(claim, step=0, calls=calls or [CALL])
    return claim, batch["session"], batch["action_ids"][0], batch


def permit(repo, claim, aid, call=CALL):
    return repo.permit_action(claim, action_id=aid, name=call["name"], input=call["input"])


def payload(version=1, assertion="inconclusive"):
    return {"expected_version": version, "assertion": assertion, "evidence_sha256": "a" * 64}


def test_seal_replay_is_stable_and_identical_intentional_calls_are_distinct(env):
    repo, db, _ = env
    calls = [CALL, {**CALL, "id": "two"}]
    claim, state, aid, batch = prepared(env, calls)
    before = copy.deepcopy(db.data)
    assert repo.prepare_actions(claim, step=0, calls=calls) == batch
    assert db.data == before
    assert batch["action_ids"][0] != batch["action_ids"][1]
    assert len(repo.action_history(state["id"], "alice")["actions"]) == 2
    with pytest.raises(LeaseLost, match="input changed"):
        repo.prepare_actions(claim, step=0, calls=list(reversed(calls)))
    with pytest.raises(LeaseLost, match="duplicate"):
        repo.prepare_actions(claim, step=1, calls=[CALL, CALL])
    assert db.data == before


def test_concurrent_permits_issue_one_permission_and_never_replay(env):
    repo, db, _ = env
    claim, state, aid, _ = prepared(env)
    def attempt(_):
        try:
            permit(repo, claim, aid)
            return True
        except LeaseLost:
            return False
    with ThreadPoolExecutor(6) as pool:
        assert sum(pool.map(attempt, range(6))) == 1
    receipt = doc(db, ACTIONS_COLLECTION, aid)
    assert receipt["state"] == "outcome_unknown"
    job = doc(db, JOBS_COLLECTION, claim.job_id)
    assert receipt["permit"]["lease_expires_at"] == job["lease_expires_at"]
    assert receipt["permit"]["run_deadline"] == job["run_deadline"]
    assert job["open_actions"] == [aid]
    with pytest.raises(LeaseLost):
        repo.prepare_actions(claim, step=1, calls=[CALL])
    state["status"] = "idle"
    with pytest.raises(LeaseLost, match="reconciliation"):
        repo.save(claim, state)
    with pytest.raises(LeaseLost):
        repo.finish(claim)
    assert doc(db, SLOTS_COLLECTION, "alice")["runs"] == [claim.job_id]


@pytest.mark.parametrize("window", ["prepared", "permitted", "returned"])
def test_restart_preserves_crash_window_without_replay_or_refund(env, window):
    repo, db, clock = env
    claim, state, aid, _ = prepared(env)
    if window != "prepared":
        permit(repo, claim, aid)
    if window == "returned":
        repo.record_action_result(claim, action_id=aid, result_digest=_digest("returned"))
    clock[0] += LEASE_SECONDS + 1
    restarted = RunRepository(db, clock=lambda: clock[0], transaction_runner=db.transaction)
    assert restarted.claim(claim.job_id, "replacement") is None
    expected = {"prepared": "prepared", "permitted": "outcome_unknown", "returned": "handler_returned"}[window]
    assert restarted.inspect_action(state["id"], "alice", aid)["action"]["state"] == expected
    with pytest.raises(LeaseLost):
        permit(restarted, claim, aid)
    with pytest.raises(LeaseLost):
        restarted.record_action_result(claim, action_id=aid, result_digest=_digest("returned"))
    assert doc(db, SLOTS_COLLECTION, "alice")["runs"] == [claim.job_id]
    assert doc(db, SESSIONS_COLLECTION, state["id"])["recovery_required"]


def test_changed_input_forged_fence_and_conflicting_results_reject(env):
    repo, db, _ = env
    claim, state, aid, _ = prepared(env)
    before = copy.deepcopy(db.data)
    with pytest.raises(LeaseLost):
        repo.permit_action(claim, action_id=aid, name="bash", input={"command": "x"})
    with pytest.raises(LeaseLost):
        permit(repo, replace(claim, token=claim.token + 1), aid)
    assert db.data == before
    permit(repo, claim, aid)
    repo.record_action_result(claim, action_id=aid, result_digest=_digest("result"))
    before = copy.deepcopy(db.data)
    repo.record_action_result(claim, action_id=aid, result_digest=_digest("result"))
    assert db.data == before
    with pytest.raises(LeaseLost, match="conflicts"):
        repo.record_action_result(claim, action_id=aid, result_digest=_digest("changed"))
    with pytest.raises(LeaseLost):
        permit(repo, claim, aid)
    assert db.data == before


@pytest.mark.parametrize("operation", ["prepare", "permit", "result", "evidence"])
def test_receipt_transactions_roll_back_atomically(env, operation):
    repo, db, _ = env
    claim, state, aid, _ = prepared(env)
    if operation == "result":
        permit(repo, claim, aid)
    before = copy.deepcopy(db.data)
    def aborted(callback):
        def abort(tx):
            callback(tx)
            raise RuntimeError("synthetic commit abort")
        return db.transaction(abort)
    repo.transaction_runner = aborted
    with pytest.raises(RuntimeError, match="commit abort"):
        if operation == "prepare":
            repo.prepare_actions(claim, step=1, calls=[CALL])
        elif operation == "permit":
            permit(repo, claim, aid)
        elif operation == "result":
            repo.record_action_result(claim, action_id=aid, result_digest=_digest("result"))
        else:
            repo.record_action_evidence(state["id"], "alice", aid, "key", payload())
    assert db.data == before


@pytest.mark.parametrize("operation", ["permit_action", "record_action_result"])
def test_lost_transaction_reply_stops_guard_and_cannot_reissue_permission(env, operation):
    repo, db, _ = env
    claim, state, aid, _ = prepared(env)
    if operation == "record_action_result":
        permit(repo, claim, aid)
    def lost_reply(callback):
        db.transaction(callback)
        raise TimeoutError("synthetic-private-commit-response")
    repo.transaction_runner = lost_reply
    with execution_context(Guard(repo, claim)):
        from app.execution.context import action
        # checkpoint uses the same adapter, so allow its first read-only call.
        count = [0]
        def runner(callback):
            count[0] += 1
            return db.transaction(callback) if count[0] == 1 else lost_reply(callback)
        repo.transaction_runner = runner
        kwargs = {"action_id": aid, "name": CALL["name"], "input": CALL["input"]} if operation == "permit_action" else {
            "action_id": aid, "result_digest": _digest("result")}
        with pytest.raises(LeaseLost, match="unavailable") as error:
            action(operation, **kwargs)
        assert "private" not in str(error.value)
    repo.transaction_runner = db.transaction
    assert doc(db, ACTIONS_COLLECTION, aid)["state"] == ("outcome_unknown" if operation == "permit_action" else "handler_returned")
    with pytest.raises(LeaseLost):
        permit(repo, claim, aid)


@pytest.mark.parametrize("failure", ["timeout", "oversized", "observation"])
def test_invocation_failure_keeps_receipt_open_and_stops_execution(env, monkeypatch, failure):
    from app.execution.errors import ExecutionStopped
    repo, db, _ = env
    call = {"id": "read", "name": "read_file", "input": {"path": "synthetic"}} if failure == "observation" else CALL
    claim, state, aid, _ = prepared(env, [call])
    effects = []
    def handler(*args):
        effects.append(True)
        if failure == "oversized":
            return "synthetic-private-output" * 32000
        raise TimeoutError("synthetic-private-handler-error")
    monkeypatch.setattr(tools, "TOOL_SPECS", [replace(s, handler=handler) if s.name == call["name"] else s for s in tools.TOOL_SPECS])
    with execution_context(Guard(repo, claim)), pytest.raises(ExecutionStopped):
        tools.dispatch_tool_call(state, object(), call["name"], call["input"], aid)
        pytest.fail("must stop before later tool/model dispatch")
    assert effects == [True]
    assert doc(db, ACTIONS_COLLECTION, aid)["state"] == "outcome_unknown"
    assert doc(db, JOBS_COLLECTION, claim.job_id)["open_actions"] == [aid]


def test_shared_dispatch_requires_claim_even_for_direct_call(monkeypatch):
    monkeypatch.setattr(tools, "TOOL_SPECS", [replace(s, handler=lambda *a: pytest.fail("unclaimed handler"))
                                              if s.name == CALL["name"] else s for s in tools.TOOL_SPECS])
    with pytest.raises(LeaseLost, match="durable worker claim"):
        tools.dispatch_tool_call({}, object(), CALL["name"], CALL["input"])


@pytest.mark.parametrize("approved", [False, True])
def test_approval_new_job_reuses_original_action_and_durable_decision(env, approved):
    repo, db, _ = env
    session = create(repo)
    old = repo.claim(session["active_run_id"], "first")
    _, state = repo.start(old)
    call = {"id": "risk", "name": "bash", "input": {"command": "sudo synthetic"}, "risky": True}
    seal_pending(repo, old, state, {"id": "approval", "calls": [call]})
    aid = call["action_id"]
    batch_id = state["pending_approval"]["batch_id"]
    with pytest.raises(LeaseLost, match="approval"):
        permit(repo, old, aid, call)
    forged = copy.deepcopy(state)
    forged["pending_approval"]["calls"][0]["input"] = {"command": "changed"}
    with pytest.raises(LeaseLost):
        repo.save(old, forged)
    repo.save(old, state)
    resumed = repo.submit("alice", "approve", "approve", {"approval_id": "approval",
                          "decisions": [{"call_id": "risk", "approved": approved}]}, state["id"])
    new = repo.claim(resumed["active_run_id"], "second")
    _, fresh = repo.start(new)
    assert new.job_id != old.job_id and fresh["pending_approval"]["batch_id"] == batch_id
    if approved:
        permit(repo, new, aid, call)
        repo.record_action_result(new, action_id=aid, result_digest=_digest("returned"))
        assert doc(db, ACTIONS_COLLECTION, aid)["origin_run_id"] == old.job_id
        assert doc(db, ACTIONS_COLLECTION, aid)["permit"]["run_id"] == new.job_id
    else:
        with pytest.raises(LeaseLost, match="approval"):
            permit(repo, new, aid, call)
        assert doc(db, ACTIONS_COLLECTION, aid)["state"] == "prepared"
    fresh.update(status="idle", pending_approval=None)
    repo.save(new, fresh)
    assert len([1 for c, _ in db.data if c == ACTIONS_COLLECTION]) == 1


def test_owner_evidence_is_unverified_idempotent_versioned_and_cannot_release_unknown(env):
    repo, db, _ = env
    claim, state, aid, _ = prepared(env)
    permit(repo, claim, aid)
    repo.finish(claim, failed=True)
    before = {k: copy.deepcopy(v) for k, v in db.data.items() if k[0] not in {ACTIONS_COLLECTION, EVIDENCE_COLLECTION, "agent_v2_commands"}}
    response = repo.record_action_evidence(state["id"], "alice", aid, "evidence", payload(2, "effect_observed"))
    assert response["verification"] == "unverified_owner_attestation"
    assert repo.record_action_evidence(state["id"], "alice", aid, "evidence", payload(2, "effect_observed")) == response
    with pytest.raises(CommandError) as conflict:
        repo.record_action_evidence(state["id"], "alice", aid, "evidence", payload(3, "no_effect_observed"))
    assert conflict.value.status == 409
    with pytest.raises(CommandError):
        repo.record_action_evidence(state["id"], "alice", aid, "other", payload(2))
    assert {k: v for k, v in db.data.items() if k in before} == before
    assert doc(db, ACTIONS_COLLECTION, aid)["state"] == "outcome_unknown"
    assert len(repo.inspect_action(state["id"], "alice", aid)["evidence"]) == 1


def test_conflicting_owner_evidence_race_accepts_one_version(env):
    repo, db, _ = env
    claim, state, aid, _ = prepared(env)
    def attempt(index):
        try:
            return repo.record_action_evidence(state["id"], "alice", aid, str(index), payload())
        except CommandError as error:
            assert error.status == 409
            return None
    with ThreadPoolExecutor(6) as pool:
        assert sum(r is not None for r in pool.map(attempt, range(6))) == 1
    assert doc(db, ACTIONS_COLLECTION, aid)["version"] == 2


def test_owner_scope_precedes_bad_cursor_and_receipt_replay_and_historical_gaps_fail_closed(env):
    repo, db, _ = env
    claim, state, aid, _ = prepared(env)
    repo.record_action_evidence(state["id"], "alice", aid, "same", payload())
    other = create(repo, "another")
    for operation in (lambda: repo.action_history(state["id"], "bob", "bad", 1000),
                      lambda: repo.inspect_action(state["id"], "bob", aid, "bad"),
                      lambda: repo.record_action_evidence(state["id"], "bob", aid, "same", payload()),
                      lambda: repo.inspect_action(other["id"], "alice", aid)):
        with pytest.raises(CommandError) as error:
            operation()
        assert error.value.status == 404
    db.data[(SESSIONS_COLLECTION, state["id"])]["uid"] = "bob"
    with pytest.raises(CommandError) as error:
        repo.record_action_evidence(state["id"], "alice", aid, "same", payload())
    assert error.value.status == 404
    db.data[(SESSIONS_COLLECTION, state["id"])]["uid"] = "alice"
    del db.data[(ACTIONS_COLLECTION, aid)]
    with pytest.raises(CommandError) as error:
        repo.action_history(state["id"], "alice")
    assert error.value.status == 503


def test_public_pages_and_evidence_are_bounded_and_secret_safe(env):
    repo, db, _ = env
    claim, state, aid, _ = prepared(env, [CALL, {**CALL, "id": "two"}])
    first = repo.action_history(state["id"], "alice", limit=1)
    assert first["next_cursor"] == "1"
    assert len(repo.action_history(state["id"], "alice", first["next_cursor"])["actions"]) == 1
    response = repo.record_action_evidence(state["id"], "alice", aid, "e", payload())
    db.data[(EVIDENCE_COLLECTION, response["id"])]["private_diagnostic"] = "synthetic-private-secret"
    public = repo.inspect_action(state["id"], "alice", aid)
    assert "synthetic-private" not in str(public)
    assert "private_diagnostic" not in str(public)
    assert "input" not in public["action"] and "call_id" not in public["action"]
    with pytest.raises(CommandError):
        repo.action_history(state["id"], "alice", "9")
    with pytest.raises(CommandError):
        repo.record_action_evidence(state["id"], "alice", aid, "bad", {**payload(), "clear_recovery": True})


def test_child_cannot_erase_action_head_or_publish_empty_approval(env):
    repo, db, _ = env
    claim, state, aid, _ = prepared(env)
    state["action_sequence"] = 0
    repo.save(claim, state)
    assert state["action_sequence"] == 1
    state["status"] = "awaiting_approval"
    with pytest.raises(LeaseLost):
        repo.save(claim, state)
    assert doc(db, SLOTS_COLLECTION, "alice")["runs"] == [claim.job_id]


def test_http_evidence_auth_strict_body_and_no_new_execution(env, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.auth import get_uid
    import app.api.actions as api
    import app.ratelimit as ratelimit
    repo, db, _ = env
    claim, state, aid, _ = prepared(env)
    monkeypatch.setattr(api, "runs", repo)
    monkeypatch.setattr(ratelimit, "check_rate_limit", lambda *a: None)
    app.dependency_overrides[get_uid] = lambda: "alice"
    try:
        with TestClient(app) as client:
            path = f"/sessions/{state['id']}/actions/{aid}"
            assert client.get(path).status_code == 200
            for bad in ({**payload(), "success": True}, {**payload(), "expected_version": True},
                        {**payload(), "evidence_sha256": "https://example.test/private"}):
                assert client.post(path + "/reconciliation-evidence", headers={"Idempotency-Key": "bad"}, json=bad).status_code == 422
            before_jobs = len([1 for c, _ in db.data if c == JOBS_COLLECTION])
            response = client.post(path + "/reconciliation-evidence", headers={"Idempotency-Key": "e"}, json=payload())
            assert response.status_code == 200 and response.json()["verification"] == "unverified_owner_attestation"
            assert len([1 for c, _ in db.data if c == JOBS_COLLECTION]) == before_jobs
            app.dependency_overrides[get_uid] = lambda: "bob"
            assert client.get(path).status_code == 404
            assert client.post(path + "/reconciliation-evidence", headers={"Idempotency-Key": "e"}, json=payload()).status_code == 404
    finally:
        app.dependency_overrides.pop(get_uid, None)


def test_observation_failure_stops_remaining_batch_and_next_model(env, monkeypatch):
    import app.agent.loop as loop
    from app.agent.providers.base import ToolCall
    from app.billing.plans import ModelEntry
    repo, db, _ = env
    session = create(repo)
    claim = repo.claim(session["active_run_id"], "worker")
    _, state = repo.start(claim)
    state["system_prompt"] = "synthetic"
    repo.save(claim, state)
    calls = [ToolCall("read", "read_file", {"path": "synthetic"}), ToolCall("later", "write_file", CALL["input"])]
    model_calls, later_calls = [], []
    adapter = SimpleNamespace(usage=lambda _: {"input_tokens": 0, "output_tokens": 0},
                              assistant_message=lambda _: {"role": "assistant", "content": []},
                              is_tool_use=lambda _: True, tool_calls=lambda _: calls)
    monkeypatch.setattr(loop.manager, "get", lambda _: object())
    monkeypatch.setattr(loop, "needs_compaction", lambda _: False)
    monkeypatch.setattr(loop, "metered_create", lambda *a, **kw: model_calls.append(True))
    def failed_read(*args):
        raise TimeoutError("synthetic-private-read-error")
    monkeypatch.setattr(tools, "TOOL_SPECS", [replace(s, handler=failed_read) if s.name == "read_file" else
                        replace(s, handler=lambda *a: later_calls.append(True)) if s.name == "write_file" else s for s in tools.TOOL_SPECS])
    with execution_context(Guard(repo, claim)), pytest.raises(LeaseLost):
        loop._agent_loop(state, adapter, ModelEntry("test", "test", None), [])
    assert model_calls == [True] and later_calls == []
    assert [a["state"] for a in repo.action_history(state["id"], "alice")["actions"]] == ["outcome_unknown", "prepared"]


def test_result_checkpoint_storage_error_stops_next_tool_and_model(env, monkeypatch):
    import app.agent.loop as loop
    from app.agent.providers.base import ToolCall
    from app.billing.plans import ModelEntry
    repo, db, _ = env
    session = create(repo)
    claim = repo.claim(session["active_run_id"], "worker")
    _, state = repo.start(claim)
    state["system_prompt"] = "synthetic"
    repo.save(claim, state)
    calls = [ToolCall("first", "write_file", CALL["input"]), ToolCall("second", "write_file", CALL["input"])]
    effects, models = [], []
    adapter = SimpleNamespace(usage=lambda _: {"input_tokens": 0, "output_tokens": 0},
                              assistant_message=lambda _: {"role": "assistant", "content": []},
                              is_tool_use=lambda _: True, tool_calls=lambda _: calls)
    monkeypatch.setattr(loop.manager, "get", lambda _: object())
    monkeypatch.setattr(loop, "needs_compaction", lambda _: False)
    monkeypatch.setattr(loop, "metered_create", lambda *a, **kw: models.append(True))
    monkeypatch.setattr(tools, "TOOL_SPECS", [replace(s, handler=lambda *a: effects.append(True))
                                             if s.name == "write_file" else s for s in tools.TOOL_SPECS])
    original = repo.checkpoint
    failed = []
    def checkpoint(claim):
        if effects and not failed:
            failed.append(True)
            raise TimeoutError("synthetic-private-result-checkpoint")
        return original(claim)
    monkeypatch.setattr(repo, "checkpoint", checkpoint)
    with execution_context(Guard(repo, claim)), pytest.raises(LeaseLost, match="unavailable"):
        loop._agent_loop(state, adapter, ModelEntry("test", "test", None), [])
    assert effects == [True] and models == [True] and failed == [True]
    assert [a["state"] for a in repo.action_history(state["id"], "alice")["actions"]] == ["outcome_unknown", "prepared"]


def test_open_action_blocks_even_a_different_same_batch_permit(env):
    repo, db, _ = env
    claim, state, aid, batch = prepared(env, [CALL, {**CALL, "id": "two"}])
    second = batch["action_ids"][1]
    permit(repo, claim, aid)
    with pytest.raises(LeaseLost, match="reconciliation"):
        permit(repo, claim, second)
    repo.record_action_result(claim, action_id=aid, result_digest=_digest("returned"))
    permit(repo, claim, second)
    assert doc(db, JOBS_COLLECTION, claim.job_id)["open_actions"] == [second]


def test_old_approval_cannot_authorize_later_changed_batch_with_same_call_id(env):
    repo, db, _ = env
    session = create(repo)
    old = repo.claim(session["active_run_id"], "first")
    _, state = repo.start(old)
    call = {"id": "risk", "name": "bash", "input": {"command": "sudo original"}, "risky": True}
    seal_pending(repo, old, state, {"id": "approval", "calls": [call]})
    repo.save(old, state)
    resumed = repo.submit("alice", "approve", "approve", {"approval_id": "approval",
                          "decisions": [{"call_id": "risk", "approved": True}]}, state["id"])
    claim = repo.claim(resumed["active_run_id"], "second")
    _, fresh = repo.start(claim)
    permit(repo, claim, call["action_id"], call)
    repo.record_action_result(claim, action_id=call["action_id"], result_digest=_digest("returned"))
    fresh.update(status="running", pending_approval=None)
    repo.save(claim, fresh)
    changed = {"id": "risk", "name": "bash", "input": {"command": "sudo changed"}}
    batch = repo.prepare_actions(claim, step=1, calls=[changed])
    with pytest.raises(LeaseLost, match="sealed approval"):
        permit(repo, claim, batch["action_ids"][0], changed)
    assert doc(db, ACTIONS_COLLECTION, batch["action_ids"][0])["state"] == "prepared"


@pytest.mark.parametrize("phase", ["ordinary", "approval"])
def test_successful_loops_dispatch_original_sealed_identity(env, monkeypatch, phase):
    import app.agent.loop as loop
    from app.agent.providers.base import ToolCall
    from app.billing.plans import ModelEntry
    repo, db, _ = env
    session = create(repo)
    old = repo.claim(session["active_run_id"], "first")
    _, state = repo.start(old)
    state["system_prompt"] = "synthetic"
    results, effects, ids = [], [], []
    if phase == "approval":
        call = {"id": "risk", "name": "bash", "input": {"command": "sudo synthetic"}, "risky": True}
        seal_pending(repo, old, state, {"id": "approval", "provider": "test", "model": "test", "quota": None,
                                       "steps_used": 0, "calls": [call]})
        repo.save(old, state)
        queued = repo.submit("alice", "approve", "approve", {"approval_id": "approval",
                             "decisions": [{"call_id": "risk", "approved": True}]}, state["id"])
        claim = repo.claim(queued["active_run_id"], "second")
        _, state = repo.start(claim)
        expected = call["action_id"]
        monkeypatch.setattr(loop, "_agent_loop", lambda *a, **kw: "continued")
    else:
        claim = old
        repo.save(claim, state)
        expected = None
    adapter = SimpleNamespace(tool_result_messages=lambda values: results.extend(values) or [])
    from app.billing import entitlements as entitlement_service
    from app.billing.entitlements import Entitlements
    monkeypatch.setattr(entitlement_service, "resolve_entitlements", lambda uid, **kwargs: Entitlements(
        "fixture", {"requests_per_day": 100, "tokens_per_month": 1_000_000}, [ModelEntry("test", "test", None)]))
    monkeypatch.setattr(loop, "get_adapter", lambda _: adapter)
    monkeypatch.setattr(loop.manager, "get", lambda _: object())
    monkeypatch.setattr(loop, "_tools_for", lambda *a: [])
    monkeypatch.setattr(loop, "record_step", lambda *a, **kw: None)
    monkeypatch.setattr(loop, "needs_compaction", lambda _: False)
    name = "bash" if phase == "approval" else "write_file"
    monkeypatch.setattr(tools, "TOOL_SPECS", [replace(s, handler=lambda *a: effects.append(True) or "returned")
                                             if s.name == name else s for s in tools.TOOL_SPECS])
    original_permit = repo.permit_action
    def capture(claim, **data):
        ids.append(data["action_id"])
        return original_permit(claim, **data)
    monkeypatch.setattr(repo, "permit_action", capture)
    with execution_context(Guard(repo, claim)):
        if phase == "approval":
            assert loop.resume_turn(state, [{"call_id": "risk", "approved": True}]) == "continued"
        else:
            models = []
            def model(*a, **kw):
                models.append(True)
                return len(models)
            monkeypatch.setattr(loop, "metered_create", model)
            adapter.usage = lambda _: {"input_tokens": 0, "output_tokens": 0}
            adapter.assistant_message = lambda _: {"role": "assistant", "content": []}
            adapter.is_tool_use = lambda response: response == 1
            adapter.tool_calls = lambda _: [ToolCall(CALL["id"], CALL["name"], CALL["input"])]
            adapter.text = lambda _: "done"
            assert loop._agent_loop(state, adapter, ModelEntry("test", "test", None), []) == "done"
    actions = repo.action_history(state["id"], "alice")["actions"]
    assert effects == [True] and len(results) == 1
    assert ids == [actions[0]["id"]]
    if expected:
        assert ids == [expected]
    assert actions[0]["state"] == "handler_returned"

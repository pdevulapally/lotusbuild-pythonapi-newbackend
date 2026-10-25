"""Finite native FIFO turns: real repository authority, synthetic settings only."""
import copy
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

import app.api.queue as routes
import app.auth as auth
from app.auth import QueueIdentity
from app.execution.queue import QUEUE_COLLECTION
from app.execution.repository import CommandError, JOBS_COLLECTION, RunRepository
from app.execution.contracts import MESSAGES_COLLECTION
from app.billing.budgets import BUDGETS_COLLECTION, RESERVATIONS_COLLECTION
from app.store import SESSIONS_COLLECTION
from test_durable_runs import env, doc
from test_product_contracts import configure, project_session, running, assert_error


def setup(env, *, maximum=4):
    repo, db, clock = env
    plan = configure(db)
    plan["meterLimits"] = [{"meterKey": "fixture-builds", "includedAllowance": maximum, "hardLimit": maximum, "overageEnabled": False}]
    db.data[("config", "registry")] = {"buildMeterKey": "fixture-builds", "meters": [{"key": "fixture-builds", "kind": "COUNTER", "resetPeriod": "DAILY"}]}
    project = repo.create_project("alice", "project", {"name": "fixture"})
    session = project_session(repo, project)
    return session


def identity(env, uid="alice"):
    now = env[2][0]
    return QueueIdentity(uid, 900, 2000, now + 1)


def enqueue(env, session, key="one", expiry=1800, prompt="queued user text"):
    return env[0].enqueue_message(session["id"], "alice", key, prompt, expiry, identity(env))


def quiesce(env, session):
    repo, _, _ = env
    claim, current = running(repo, session)
    current["status"] = "idle"
    repo.save(claim, current)
    repo.finish(claim)
    repo.confirm_quiescent(claim)  # Trusted parent after validated containment.
    return claim, current


def dispatch(env, session, key="dispatch"):
    return env[0].dispatch_next(session["id"], "alice", key, identity(env), owner_request=True)


def test_queue_repeat_cancel_and_replay_never_resurrect_or_renew(env):
    repo, db, _ = env
    session = setup(env)
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda _: enqueue(env, session), range(4)))
    assert all(value == results[0] for value in results)
    item = results[0]["queuedMessage"]
    assert len(repo.list_queued(session["id"], "alice")["queuedMessages"]) == 1
    assert doc(db, SESSIONS_COLLECTION, session["id"])["revision"] == session["revision"]
    before = copy.deepcopy(db.data)
    assert_error(409, lambda: enqueue(env, session, prompt="different"))
    assert db.data == before
    repo.cancel_queued(session["id"], "alice", item["id"])
    before = copy.deepcopy(db.data)
    assert enqueue(env, session) == results[0]
    assert repo.cancel_queued(session["id"], "alice", item["id"])["state"] == "cancelled"
    assert repo.list_queued(session["id"], "alice")["queuedMessages"] == []
    assert db.data == before
    assert doc(db, QUEUE_COLLECTION, item["id"])["expires_at"] == 1800


@pytest.mark.parametrize("problem", ["registry", "limits", "wrong_meter", "overage"])
def test_queue_policy_requires_authoritative_build_meter(env, problem):
    repo, db, _ = env
    session = setup(env)
    if problem == "registry":
        del db.data[("config", "registry")]
    elif problem == "limits":
        db.data[("plans", "free")].pop("meterLimits")
    elif problem == "wrong_meter":
        db.data[("config", "registry")]["meters"][0]["kind"] = "GAUGE"
    else:
        db.data[("plans", "free")]["meterLimits"][0]["overageEnabled"] = True
    before = copy.deepcopy(db.data)
    assert_error(503, lambda: enqueue(env, session))
    assert db.data == before


def test_queue_limit_race_fails_without_receipt_or_duplicate(env):
    repo, db, _ = env
    session = setup(env, maximum=1)
    def attempt(key):
        try:
            return enqueue(env, session, key)
        except CommandError as error:
            return error.status
    with ThreadPoolExecutor(3) as pool:
        results = list(pool.map(attempt, ["one", "two", "three"]))
    assert sum(isinstance(value, dict) for value in results) == 1 and results.count(429) == 2
    assert len([1 for c, _ in db.data if c == QUEUE_COLLECTION]) == 1


def test_atomic_dispatch_repeat_has_one_job_text_and_counter_charge(env):
    repo, db, _ = env
    session = setup(env)
    item = enqueue(env, session)["queuedMessage"]
    quiesce(env, session)
    with ThreadPoolExecutor(3) as pool:
        results = list(pool.map(lambda _: dispatch(env, session), range(3)))
    assert all(value == results[0] for value in results)
    result = results[0]
    assert result["dispatched"] is True and result["session"]["status"] == "queued"
    assert result["session"]["mode"] == "build"
    assert repo.inspect_queued(session["id"], "alice", item["id"])["state"] == "dispatched"
    assert len([1 for c, _ in db.data if c == JOBS_COLLECTION]) == 2
    assert len([1 for c, _ in db.data if c == MESSAGES_COLLECTION]) == 2
    assert len([1 for c, _ in db.data if c == RESERVATIONS_COLLECTION]) == 2
    assert [v["consumed"] for (c, _), v in db.data.items() if c == BUDGETS_COLLECTION] == [2]
    assert_error(409, lambda: repo.cancel_queued(session["id"], "alice", item["id"]))


def test_fifo_cannot_be_bypassed_by_direct_native_message(env):
    repo, _, _ = env
    session = setup(env)
    first = enqueue(env, session, "one", prompt="first")["queuedMessage"]
    second = enqueue(env, session, "two", prompt="second")["queuedMessage"]
    quiesce(env, session)
    assert_error(409, lambda: repo.submit("alice", "jump", "message", {"content": "jump ahead"}, session["id"]))
    page = repo.list_queued(session["id"], "alice", limit=1)
    assert page["queuedMessages"][0]["id"] == first["id"] and page["nextCursor"] == first["id"]
    repo.cancel_queued(session["id"], "alice", first["id"])
    assert repo.list_queued(session["id"], "alice", after=first["id"])["queuedMessages"][0]["id"] == second["id"]
    assert dispatch(env, session)["queuedMessage"]["id"] == second["id"]


@pytest.mark.parametrize("state", ["unverified", "approval", "recovery", "cancelled", "restore", "open_action"])
def test_dispatch_never_flattens_unsafe_lifecycle(env, state):
    repo, db, _ = env
    session = setup(env)
    enqueue(env, session)
    claim, current = running(repo, session)
    current["status"] = "idle"
    repo.save(claim, current)
    repo.finish(claim)
    if state != "unverified":
        repo.confirm_quiescent(claim)
    stored = db.data[(SESSIONS_COLLECTION, session["id"])]
    if state == "approval":
        stored.update(status="awaiting_approval", pending_approval={"id": "sealed"})
    elif state == "recovery":
        stored["recovery_required"] = True
    elif state == "cancelled":
        repo.cancel(session["id"], "alice")
    elif state == "restore":
        stored["workspace_restore_id"] = "quarantine"
    elif state == "open_action":
        db.data[(JOBS_COLLECTION, claim.job_id)]["open_actions"] = ["unknown"]
    before = copy.deepcopy(db.data)
    assert_error(409, lambda: dispatch(env, session))
    assert db.data == before


def test_enqueue_during_running_does_not_stale_or_erase_child_state(env):
    repo, db, _ = env
    session = setup(env)
    claim, current = running(repo, session)
    enqueue(env, session)
    current.update(queue_pending=[], queue_sequence=0, queue_ready=True)
    current["plan"] = [{"step": "still running", "status": "in_progress"}]
    repo.save(claim, current)
    stored = doc(db, SESSIONS_COLLECTION, session["id"])
    assert len(stored["queue_pending"]) == stored["queue_sequence"] == 1
    assert stored.get("queue_ready") is not True
    current["status"] = "idle"
    repo.save(claim, current)
    assert_error(409, lambda: dispatch(env, session))
    repo.confirm_quiescent(claim)
    assert dispatch(env, session)["dispatched"] is True


def test_expiry_and_cancel_are_retained_no_execution_or_quota_charge(env):
    repo, db, clock = env
    session = setup(env)
    item = enqueue(env, session, expiry=1002)["queuedMessage"]
    quiesce(env, session)
    clock[0] = 1003
    assert repo.inspect_queued(session["id"], "alice", item["id"])["state"] == "expired"
    result = dispatch(env, session)
    assert result["dispatched"] is False and result["queuedMessage"]["state"] == "expired"
    assert len([1 for c, _ in db.data if c == RESERVATIONS_COLLECTION]) == 1  # Initial turn only.
    assert len([1 for c, _ in db.data if c == MESSAGES_COLLECTION]) == 1
    assert doc(db, QUEUE_COLLECTION, item["id"])["state"] == "expired"
    assert_error(409, lambda: enqueue(env, session, "new"))


@pytest.mark.parametrize("gate", ["identity", "model", "policy", "build_quota", "slots"])
def test_failed_dispatch_has_no_partial_admission_or_charge(env, gate):
    repo, db, clock = env
    session = setup(env)
    enqueue(env, session)
    quiesce(env, session)
    proof = identity(env)
    if gate == "identity":
        proof = QueueIdentity("alice", 900, 2000, clock[0])
    elif gate == "model":
        db.data[("plans", "free")]["models"] = []
    elif gate == "policy":
        db.data[("config", "registry")].pop("buildMeterKey")
    elif gate == "build_quota":
        db.data[("plans", "free")]["meterLimits"][0]["hardLimit"] = 0
        db.data[("plans", "free")]["meterLimits"][0]["includedAllowance"] = 0
    else:
        db.data[("agent_v2_slots", "alice")]["runs"] = ["other1", "other2"]
    before = copy.deepcopy(db.data)
    with pytest.raises(Exception):
        repo.dispatch_next(session["id"], "alice", "dispatch", proof, owner_request=True)
    assert db.data == before


def test_cross_tenant_queue_paths_and_malformed_cursors_are_opaque(env):
    repo, db, _ = env
    session = setup(env)
    item = enqueue(env, session)["queuedMessage"]
    before = copy.deepcopy(db.data)
    operations = [lambda: repo.list_queued(session["id"], "mallory", "invalid", 0),
        lambda: repo.cancel_queued(session["id"], "mallory", item["id"]),
        lambda: repo.inspect_queued(session["id"], "mallory", item["id"]),
        lambda: repo.dispatch_next(session["id"], "mallory", "one", identity(env, "mallory"), owner_request=True)]
    for operation in operations:
        assert_error(404, operation)
    assert db.data == before


def test_restart_discovers_quiescent_work_but_default_worker_has_no_auth_grant(env, monkeypatch):
    import app.worker as worker
    repo, db, clock = env
    session = setup(env)
    enqueue(env, session)
    quiesce(env, session)
    restarted = RunRepository(db, clock=lambda: clock[0], transaction_runner=db.transaction)
    monkeypatch.setattr(restarted, "cleanup_once", lambda *args: None)
    assert worker.run_once(restarted) is False
    assert worker.run_once(restarted, queue_authorizer=lambda candidate: identity(env)) is True
    assert doc(db, SESSIONS_COLLECTION, session["id"])["status"] == "queued"


@pytest.fixture
def client(env, monkeypatch):
    from app.main import app
    monkeypatch.setattr(routes, "runs", env[0])
    monkeypatch.setattr("app.ratelimit.check_rate_limit", lambda *args: None)
    app.dependency_overrides[auth.get_uid] = lambda: "alice"
    app.dependency_overrides[auth.get_queue_identity] = lambda: identity(env)
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


def test_http_real_native_queue_shapes_and_explicit_steering_defer(env, client):
    session = setup(env)
    path = f"/sessions/{session['id']}/queued-messages"
    body = {"prompt": "next user turn", "expiresAt": "1970-01-01T00:30:00Z"}
    response = client.post(path, json=body, headers={"Idempotency-Key": "one"})
    assert response.status_code == 202, response.text
    item = response.json()["queuedMessage"]
    assert {"id", "sessionId", "prompt", "createdAt", "attachments"} <= set(item)
    assert not {"uid", "authorized_iat", "identity_exp"} & set(item)
    assert client.get(path+"?limit=100").json()["queuedMessages"][0] == item
    assert client.post(path, json=body | {"steer": True}, headers={"Idempotency-Key": "steer"}).status_code == 501
    assert client.post(path, json=body | {"mode": "plan"}, headers={"Idempotency-Key": "mode"}).status_code == 409
    assert client.delete(path+"/"+item["id"]).status_code == 204
    assert client.get(path+"/"+item["id"]).json()["state"] == "cancelled"


def test_http_dispatch_is_native_and_requires_fresh_identity(env, client):
    session = setup(env)
    enqueue(env, session)
    quiesce(env, session)
    response = client.post(f"/sessions/{session['id']}/queued-messages/dispatch", headers={"Idempotency-Key": "one"})
    assert response.status_code == 200, response.text
    assert response.json()["session"]["status"] == "queued" and response.json()["dispatched"] is True


def test_public_message_key_cannot_replay_or_reset_private_queue_job(env):
    from app.execution.repository import _digest
    repo, db, _ = env
    session = setup(env, maximum=8)
    quiesce(env, session)
    item_id = _digest(["alice", "queue_enqueue", session["id"], "future"])
    public = repo.submit("alice", "queue:" + item_id, "message", {"content": "same prompt"}, session["id"])
    enqueue(env, session, "future", prompt="same prompt")
    quiesce(env, public)
    previous = doc(db, JOBS_COLLECTION, public["active_run_id"])
    accepted = dispatch(env, session)
    assert accepted["session"]["active_run_id"] != public["active_run_id"]
    assert doc(db, JOBS_COLLECTION, public["active_run_id"]) == previous
    assert previous["fence"] == 1 and previous["state"] == "succeeded"


@pytest.mark.parametrize("orphan", ["job", "command", "counter"])
def test_orphan_queue_admission_artifacts_fail_closed(env, orphan):
    from app.execution.repository import _digest, RECEIPTS_COLLECTION
    from app.billing.budgets import _key
    repo, db, _ = env
    session = setup(env)
    item = enqueue(env, session)["queuedMessage"]
    quiesce(env, session)
    run_id = _digest(["alice", "queue_turn", session["id"], item["id"]])
    if orphan == "job":
        db.data[(JOBS_COLLECTION, run_id)] = {"id": run_id, "state": "succeeded", "fence": 99}
    elif orphan == "command":
        db.data[(RECEIPTS_COLLECTION, run_id)] = {"fingerprint": "orphan"}
    else:
        db.data[(RESERVATIONS_COLLECTION, _key("alice", "counter", "fixture-builds", run_id))] = {
            "uid": "alice", "run_id": run_id, "meter_key": "fixture-builds", "state": "consumed", "quantity": 1}
    before = copy.deepcopy(db.data)
    assert_error(503, lambda: dispatch(env, session))
    assert db.data == before


@pytest.mark.parametrize("boundary", ["enqueue", "dispatch"])
def test_auth_freshness_is_rechecked_after_slow_transaction_read(env, monkeypatch, boundary):
    repo, db, clock = env
    session = setup(env)
    if boundary == "dispatch":
        enqueue(env, session)
        quiesce(env, session)
    proof = identity(env)
    original = repo._read
    advanced = []
    def slow(ref, tx):
        result = original(ref, tx)
        if getattr(ref, "collection", None) == "config" and not advanced:
            clock[0] += 2
            advanced.append(True)
        return result
    monkeypatch.setattr(repo, "_read", slow)
    before = copy.deepcopy(db.data)
    if boundary == "enqueue":
        operation = lambda: repo.enqueue_message(session["id"], "alice", "late", "next", 1800, proof)
    else:
        operation = lambda: repo.dispatch_next(session["id"], "alice", "late", proof, owner_request=True)
    assert_error(401, operation)
    assert advanced and db.data == before


def test_configured_build_counter_covers_initial_immediate_and_queued_turns(env):
    repo, db, _ = env
    session = setup(env, maximum=2)
    assert [v["consumed"] for (c, _), v in db.data.items() if c == BUDGETS_COLLECTION] == [1]
    quiesce(env, session)
    immediate = repo.submit("alice", "immediate", "message", {"content": "second turn"}, session["id"])
    assert [v["consumed"] for (c, _), v in db.data.items() if c == BUDGETS_COLLECTION] == [2]
    enqueue(env, immediate)
    quiesce(env, immediate)
    before = copy.deepcopy(db.data)
    assert_error(429, lambda: dispatch(env, session))
    assert db.data == before


@pytest.mark.parametrize("which", ["cancel_item", "cancel_session"])
def test_cancel_dispatch_race_has_one_atomic_winner_and_no_duplicate_job(env, which):
    repo, db, _ = env
    session = setup(env)
    item = enqueue(env, session)["queuedMessage"]
    quiesce(env, session)
    def cancel():
        try:
            return repo.cancel_queued(session["id"], "alice", item["id"]) if which == "cancel_item" else repo.cancel(session["id"], "alice")
        except CommandError as error:
            return error.status
    def promote():
        try:
            return dispatch(env, session)
        except CommandError as error:
            return error.status
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(cancel), pool.submit(promote)]
        values = [future.result() for future in futures]
    state = repo.inspect_queued(session["id"], "alice", item["id"])["state"]
    assert state in ({"cancelled", "dispatched"} if which == "cancel_item" else {"queued", "dispatched"})
    assert len([1 for c, _ in db.data if c == JOBS_COLLECTION]) <= 2
    assert len([1 for c, _ in db.data if c == MESSAGES_COLLECTION]) <= 2
    assert any(isinstance(value, dict) for value in values)
    if which == "cancel_session":
        assert doc(db, SESSIONS_COLLECTION, session["id"])["status"] == "cancelled"


def test_lost_dispatch_reply_inspects_receipt_without_reexecuting(env):
    repo, db, _ = env
    session = setup(env)
    enqueue(env, session)
    quiesce(env, session)
    original = repo.transaction_runner
    def lost_reply(fn):
        result = original(fn)
        # The receipt lookup is now a read-only transaction before admission.
        # Lose the reply only AFTER an actual dispatch commit, not its preflight.
        if isinstance(result, dict) and result.get("dispatched") is True:
            raise TimeoutError("synthetic lost committed response")
        return result
    repo.transaction_runner = lost_reply
    with pytest.raises(TimeoutError):
        dispatch(env, session)
    repo.transaction_runner = original
    before = copy.deepcopy(db.data)
    assert dispatch(env, session)["dispatched"] is True
    assert db.data == before


def test_expired_promoted_job_retains_workspace_without_cleanup_or_provider(env):
    from app.worker import process_claim
    from app.execution.repository import CLEANUP_COLLECTION
    repo, db, clock = env
    session = setup(env)
    item = enqueue(env, session, expiry=1002)["queuedMessage"]
    quiesce(env, session)
    accepted = dispatch(env, session)
    clock[0] = 1003
    claim = repo.claim(accepted["session"]["active_run_id"], "worker")
    effects = []
    process_claim(repo, claim, lambda *args: effects.append(True))
    current = doc(db, SESSIONS_COLLECTION, session["id"])
    assert not effects and current["recovery"]["reason"] == "queued_run_not_started"
    assert current["status"] == "error" and current["recovery_required"] is True
    assert not any(c == CLEANUP_COLLECTION for c, _ in db.data)
    assert db.data[("agent_v2_slots", "alice")]["sandboxes"] == [session["id"]]
    assert db.data[("agent_v2_slots", "alice")]["runs"] == []
    assert repo.inspect_queued(session["id"], "alice", item["id"])["state"] == "expired"


@pytest.mark.parametrize("after_expiry", [True, False])
def test_approval_continuation_cannot_extend_queued_execution_window(env, after_expiry):
    from test_durable_runs import seal_pending
    repo, db, clock = env
    session = setup(env)
    enqueue(env, session, expiry=1020)
    quiesce(env, session)
    accepted = dispatch(env, session)
    claim, current = running(repo, accepted["session"])
    seal_pending(repo, claim, current, {"id": "approval", "provider": "anthropic", "model": "fixture-model", "quota": None,
        "calls": [{"id": "risk", "name": "bash", "input": {"command": "sudo synthetic"}, "risky": True}]})
    repo.save(claim, current)
    repo.finish(claim)
    clock[0] = 1021 if after_expiry else 1005
    payload = {"approval_id": current["pending_approval"]["id"], "decisions": [{"call_id": "risk", "approved": True}]}
    if after_expiry:
        before = copy.deepcopy(db.data)
        assert_error(409, lambda: repo.submit("alice", "approve", "approve", payload, session["id"]))
        assert db.data == before
    else:
        approved = repo.submit("alice", "approve", "approve", payload, session["id"])
        next_claim, _ = running(repo, approved)
        assert doc(db, JOBS_COLLECTION, next_claim.job_id)["run_deadline"] == 1020


@pytest.mark.parametrize("field,value", [("prompt", ""), ("prompt", "x"*8001), ("createdAt", "bad"), ("expiresAt", "bad")])
def test_corrupt_queue_rows_cannot_be_displayed_or_dispatched(env, field, value):
    repo, db, _ = env
    session = setup(env)
    item = enqueue(env, session)["queuedMessage"]
    db.data[(QUEUE_COLLECTION, item["id"])][field] = value
    assert_error(503, lambda: repo.list_queued(session["id"], "alice"))


def test_expired_item_is_not_reported_as_active_client_queue(env):
    repo, _, clock = env
    session = setup(env)
    item = enqueue(env, session, expiry=1002)["queuedMessage"]
    clock[0] = 1003
    page = repo.list_queued(session["id"], "alice")
    assert page["queuedMessages"] == [] and page["expiredCount"] == 1
    assert repo.inspect_queued(session["id"], "alice", item["id"])["state"] == "expired"


def test_post_exit_confirmation_gap_is_explicitly_blocked_not_guessed(env):
    repo, db, clock = env
    session = setup(env)
    enqueue(env, session)
    claim, current = running(repo, session)
    current["status"] = "idle"
    repo.save(claim, current)
    repo.finish(claim)
    restarted = RunRepository(db, clock=lambda: clock[0], transaction_runner=db.transaction)
    assert restarted.queue_candidates() == []
    assert_error(409, lambda: restarted.dispatch_next(session["id"], "alice", "lost-parent", identity(env), owner_request=True))


def held_queue_child(job, session):
    import json
    import time
    from pathlib import Path
    from app.execution.context import save
    options = json.loads(session["messages"][0]["content"][0]["text"])
    session["status"] = "idle"
    save(session)  # Supervisor stages this until containment exits.
    Path(options["ready"]).write_text("terminal staged")
    deadline = time.monotonic() + 8
    while not Path(options["allow"]).exists():
        if time.monotonic() >= deadline:
            raise RuntimeError("synthetic parent did not release the child")
        time.sleep(0.02)


def test_actual_spawn_does_not_dispatch_before_verified_child_exit(env, tmp_path):
    import json
    import threading
    import time
    from app.worker import process_claim
    repo, db, _ = env
    configure(db)
    plan = db.data[("plans", "free")]
    plan["meterLimits"] = [{"meterKey": "fixture-builds", "includedAllowance": 4, "hardLimit": 4, "overageEnabled": False}]
    db.data[("config", "registry")] = {"buildMeterKey": "fixture-builds", "meters": [{"key": "fixture-builds", "kind": "COUNTER", "resetPeriod": "DAILY"}]}
    project = repo.create_project("alice", "project", {"name": "fixture"})
    ready, allow = tmp_path / "ready", tmp_path / "allow"
    session = repo.submit("alice", "run", "create", {"task": json.dumps({"ready": str(ready), "allow": str(allow)}),
        "kind": "code", "model": "fixture-model", "project_id": project["id"]})
    enqueue(env, session)
    claim = repo.claim(session["active_run_id"], "worker")
    thread = threading.Thread(target=process_claim, args=(repo, claim, held_queue_child))
    thread.start()
    try:
        deadline = time.monotonic() + 8
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists()
        assert doc(db, SESSIONS_COLLECTION, session["id"])["status"] == "running"
        assert_error(409, lambda: dispatch(env, session))
        assert repo.queue_candidates() == []
    finally:
        allow.write_text("allow exit")
        thread.join(10)
    assert not thread.is_alive()
    stored = doc(db, SESSIONS_COLLECTION, session["id"])
    assert stored["status"] == "idle" and stored["queue_ready"] is True
    assert doc(db, JOBS_COLLECTION, claim.job_id)["completion_verified"] is True
    assert dispatch(env, session)["dispatched"] is True


def test_denied_background_head_does_not_starve_another_owner(env, monkeypatch):
    import app.worker as worker
    repo, db, clock = env
    first = setup(env)
    enqueue(env, first)
    quiesce(env, first)
    project = repo.create_project("bob", "project", {"name": "bob fixture"})
    second = repo.submit("bob", "run", "create", {"task": "bob original", "kind": "code", "model": "fixture-model", "project_id": project["id"]})
    proof = QueueIdentity("bob", 900, 2000, clock[0] + 1)
    repo.enqueue_message(second["id"], "bob", "one", "bob next turn", 1800, proof)
    claim, current = running(repo, second)
    current["status"] = "idle"
    repo.save(claim, current)
    repo.finish(claim)
    repo.confirm_quiescent(claim)
    monkeypatch.setattr(repo, "cleanup_once", lambda *args: None)
    observed = []
    def authorize(candidate):
        observed.append(candidate["uid"])
        if candidate["uid"] == "alice":
            raise auth.AuthenticationError("invalid token")
        return proof
    assert worker.run_once(repo, queue_authorizer=authorize) is True
    assert observed == ["alice", "bob"]
    assert doc(db, SESSIONS_COLLECTION, first["id"])["status"] == "idle"
    assert doc(db, SESSIONS_COLLECTION, second["id"])["status"] == "queued"


@pytest.mark.parametrize("invalid", [{"iat": None}, {"exp": float("nan")}, {"email_verified": False}])
def test_queue_http_identity_requires_verified_finite_claims(monkeypatch, invalid):
    import time
    monkeypatch.setattr(time, "time", lambda: 1000)
    monkeypatch.setattr(auth, "_verify_token", lambda _: {"uid": "alice", "iat": 900, "exp": 2000, "email_verified": True} | invalid)
    with pytest.raises(auth.AuthenticationError):
        auth.get_queue_identity("Bearer synthetic")


def test_queue_http_identity_freshness_starts_before_verifier_io(monkeypatch):
    import time
    clock = [1000]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    def slow(_):
        clock[0] += 2
        return {"uid": "alice", "iat": 900, "exp": 2000, "email_verified": True}
    monkeypatch.setattr(auth, "_verify_token", slow)
    with pytest.raises(auth.AuthenticationError):
        auth.get_queue_identity("Bearer synthetic")


def test_queue_http_identity_retains_only_numeric_verified_metadata(monkeypatch):
    import time
    monkeypatch.setattr(time, "time", lambda: 1000)
    monkeypatch.setattr(auth, "_verify_token", lambda _: {"uid": "alice", "iat": 900, "exp": 2000, "email_verified": True})
    assert auth.get_queue_identity("Bearer synthetic") == QueueIdentity("alice", 900, 2000, 1001)

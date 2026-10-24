"""Offline serializable state-machine tests, NOT a Firestore emulator substitute."""
import copy
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.execution.context import checkpoint, save
from app.execution.repository import (
    CancelRequested, CommandError, JOBS_COLLECTION, LEASE_SECONDS, LeaseLost,
    CLEANUP_COLLECTION, CANCEL_GRACE_SECONDS, MAX_RUN_SECONDS,
    RECEIPTS_COLLECTION, RunRepository,
)
from app.store import SESSIONS_COLLECTION
from app.worker import process_claim


class Snapshot:
    def __init__(self, key, data):
        self.id, self.data = key, copy.deepcopy(data)
        self.exists = data is not None

    def to_dict(self):
        return copy.deepcopy(self.data)


class Reference:
    def __init__(self, db, collection, key):
        self.db, self.collection, self.id = db, collection, key

    def get(self, transaction=None):
        return Snapshot(self.id, self.db.data.get((self.collection, self.id)))


class Collection:
    def __init__(self, db, name):
        self.db, self.name = db, name

    def document(self, key):
        return Reference(self.db, self.name, key)

    def where(self, *, filter):
        return Query(self.db, self.name, [filter])


class Query:
    def __init__(self, db, name, filters):
        self.db, self.name, self.filters = db, name, filters
        self.maximum = 100

    def where(self, *, filter):
        self.filters.append(filter)
        return self

    def order_by(self, field):
        return self

    def limit(self, n):
        self.maximum = n
        return self

    def stream(self):
        def matches(data):
            for f in self.filters:
                value = data.get(f.field_path)
                if f.op_string == "==" and value != f.value:
                    return False
                if f.op_string == "<=" and (value is None or value > f.value):
                    return False
                if f.op_string == "in" and value not in f.value:
                    return False
            return True
        return [Snapshot(k, v) for (c, k), v in list(self.db.data.items())
                if c == self.name and matches(v)][:self.maximum]


class Transaction:
    def __init__(self, db):
        self.db, self.writes = db, {}

    def set(self, ref, data):
        self.writes[(ref.collection, ref.id)] = copy.deepcopy(data)


class MemoryDB:
    def __init__(self):
        self.data, self.lock = {}, threading.Lock()

    def collection(self, name):
        return Collection(self, name)

    def transaction(self, callback):
        # Serialize transactions, commit all writes only on success, and never
        # leak object references. Unlike a no-op decorator this exercises races.
        with self.lock:
            tx = Transaction(self)
            result = callback(tx)
            self.data.update(tx.writes)
            return copy.deepcopy(result)


@pytest.fixture
def env():
    db, clock = MemoryDB(), [1000.0]
    repo = RunRepository(db, clock=lambda: clock[0], transaction_runner=db.transaction)
    return repo, db, clock


def create(repo, key="one"):
    return repo.submit("alice", key, "create", {"task": "hello", "kind": "code"})


def doc(db, collection, key):
    return copy.deepcopy(db.data[(collection, key)])


def idle(repo, session):
    claim = repo.claim(session["active_run_id"], "worker")
    _, current = repo.start(claim)
    current["status"] = "idle"
    repo.save(claim, current)
    repo.finish(claim)


def waiting(repo, session):
    claim = repo.claim(session["active_run_id"], "worker")
    _, current = repo.start(claim)
    current["status"] = "awaiting_approval"
    current["pending_approval"] = {"id": "batch-1", "calls": [
        {"id": "call-1", "name": "bash", "input": {"command": "echo hi"}, "risky": True}]}
    repo.save(claim, current)
    repo.finish(claim)
    return current


def test_submission_commits_session_job_and_receipt_together(env):
    repo, db, _ = env
    session = create(repo)
    assert session["status"] == "queued"
    assert session["schema_version"] == 2
    assert {c for c, _ in db.data} == {SESSIONS_COLLECTION, JOBS_COLLECTION, RECEIPTS_COLLECTION}
    # Worker discovers queued record even if API dies immediately after commit.
    assert repo.claim(session["active_run_id"], "restarted-worker")
    assert create(repo) == session  # replay does not lose/schedule another job
    assert len(db.data) == 3


def test_changed_idempotency_payload_rejected(env):
    repo, _, _ = env
    create(repo)
    with pytest.raises(CommandError, match="different input"):
        repo.submit("alice", "one", "create", {"task": "other", "kind": "code"})


def test_competing_workers_only_one_claims(env):
    repo, _, _ = env
    session = create(repo)
    with ThreadPoolExecutor(8) as pool:
        claims = list(pool.map(lambda n: repo.claim(session["active_run_id"], str(n)), range(8)))
    assert sum(c is not None for c in claims) == 1


def test_crash_before_effects_can_reclaim_and_fences_old_worker(env):
    repo, _, clock = env
    session = create(repo)
    old = repo.claim(session["active_run_id"], "old")
    clock[0] += LEASE_SECONDS + 1
    new = repo.claim(session["active_run_id"], "new")
    assert new.token > old.token
    with pytest.raises(LeaseLost):
        repo.start(old)
    with pytest.raises(LeaseLost):
        repo.heartbeat(old)
    repo.start(new)


def test_crash_after_effects_started_is_not_replayed(env):
    repo, db, clock = env
    session = create(repo)
    claim = repo.claim(session["active_run_id"], "old")
    repo.start(claim)
    clock[0] += LEASE_SECONDS + 1
    assert repo.claim(claim.job_id, "new") is None
    current = doc(db, SESSIONS_COLLECTION, session["id"])
    assert current["recovery_required"] is True
    assert current["status"] == "error"
    assert doc(db, JOBS_COLLECTION, claim.job_id)["state"] == "interrupted"
    with pytest.raises(LeaseLost):
        repo.save(claim, current)
    with pytest.raises(CommandError, match="reconciliation"):
        repo.submit("alice", "next", "message", {"content": "retry"}, session["id"])


def test_cancel_queued_run_never_executes(env):
    repo, db, _ = env
    session = create(repo)
    assert repo.cancel(session["id"], "alice")["status"] == "cancelled"
    assert repo.claim(session["active_run_id"], "worker") is None
    assert repo.cancel(session["id"], "alice")["status"] == "cancelled"


def test_cancel_cannot_be_overwritten_by_stale_session(env):
    repo, db, _ = env
    session = create(repo)
    claim = repo.claim(session["active_run_id"], "worker")
    _, stale = repo.start(claim)
    repo.cancel(session["id"], "alice")
    stale["status"] = "idle"
    with pytest.raises(CancelRequested):
        repo.save(claim, stale)
    repo.finish(claim)
    assert doc(db, SESSIONS_COLLECTION, session["id"])["status"] == "cancelled"


def test_foreign_user_cannot_submit_or_cancel(env):
    repo, _, _ = env
    session = create(repo)
    for fn in (lambda: repo.cancel(session["id"], "bob"),
               lambda: repo.submit("bob", "x", "message", {"content": "x"}, session["id"])):
        with pytest.raises(CommandError) as exc:
            fn()
        assert exc.value.status == 404


def test_distinct_key_approvals_only_queue_one_job(env):
    repo, db, _ = env
    session = waiting(repo, create(repo))
    payload = {"approval_id": "batch-1", "decisions": [{"call_id": "call-1", "approved": True}]}
    def attempt(n):
        try:
            return repo.submit("alice", str(n), "approve", payload, session["id"])
        except CommandError:
            return None
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(attempt, range(8)))
    assert sum(r is not None for r in results) == 1
    assert len([1 for c, _ in db.data if c == JOBS_COLLECTION]) == 2


@pytest.mark.parametrize("payload", [
    {"approval_id": "old", "decisions": [{"call_id": "call-1", "approved": True}]},
    {"approval_id": "batch-1", "decisions": []},
    {"approval_id": "batch-1", "decisions": [{"call_id": "other", "approved": True}]},
    {"approval_id": "batch-1", "decisions": [{"call_id": "call-1", "approved": True}] * 2},
])
def test_approval_batch_and_decision_set_must_match(env, payload):
    repo, db, _ = env
    session = waiting(repo, create(repo))
    before = copy.deepcopy(db.data)
    with pytest.raises(CommandError):
        repo.submit("alice", "approve", "approve", payload, session["id"])
    assert db.data == before  # failed transaction has no writes


def test_cancel_during_fake_model_prevents_next_tool(env):
    repo, db, _ = env
    session, effects = create(repo), []
    claim = repo.claim(session["active_run_id"], "worker")
    def fake_handler(job, state):
        checkpoint()
        repo.cancel(state["id"], "alice")  # model finished after user cancelled
        checkpoint()
        effects.append("must not execute")
    process_claim(repo, claim, fake_handler)
    assert effects == []
    assert doc(db, SESSIONS_COLLECTION, session["id"])["status"] == "cancelled"


def test_cancel_between_batch_tools_prevents_second_effect(env):
    repo, db, _ = env
    session, effects = create(repo), []
    claim = repo.claim(session["active_run_id"], "worker")
    def fake_handler(job, state):
        for action in ["first", "second"]:
            checkpoint()
            effects.append(action)
            repo.cancel(state["id"], "alice")
    process_claim(repo, claim, fake_handler)
    assert effects == ["first"]


def test_fake_worker_complete_and_new_turn(env):
    repo, db, _ = env
    session = create(repo)
    claim = repo.claim(session["active_run_id"], "worker")
    def fake_handler(job, state):
        checkpoint()
        state["messages"].append({"role": "assistant", "content": [{"type": "text", "text": "done"}]})
        state["status"] = "idle"
        save(state)
    process_claim(repo, claim, fake_handler)
    assert doc(db, JOBS_COLLECTION, claim.job_id)["state"] == "succeeded"
    next_session = repo.submit("alice", "next", "message", {"content": "again"}, session["id"])
    assert next_session["status"] == "queued"
    assert next_session["active_run_id"] != claim.job_id


def test_worker_heartbeat_renews_during_blocked_provider(env):
    repo, db, clock = env
    session = create(repo)
    claim = repo.claim(session["active_run_id"], "worker")
    beat = threading.Event()
    original = repo.heartbeat
    def observed(c):
        clock[0] += 1
        original(c)
        beat.set()
    repo.heartbeat = observed
    def fake_handler(job, state):
        assert beat.wait(2)
        state["status"] = "idle"
        save(state)
    process_claim(repo, claim, fake_handler, heartbeat_interval=0.01)
    assert beat.is_set()
    assert doc(db, JOBS_COLLECTION, claim.job_id)["state"] == "succeeded"


def test_session_write_without_worker_is_denied():
    with pytest.raises(LeaseLost):
        save({"id": "x"})


def test_terminal_save_atomically_allows_next_command(env):
    repo, _, _ = env
    session = create(repo)
    claim = repo.claim(session["active_run_id"], "worker")
    _, current = repo.start(claim)
    current["status"] = "idle"
    repo.save(claim, current)
    next_session = repo.submit("alice", "next", "message", {"content": "next"}, session["id"])
    assert next_session["status"] == "queued"
    repo.finish(claim)  # old worker completion cannot change the new run
    with pytest.raises(LeaseLost):
        repo.save(claim, current)


def test_cancel_deadline_revokes_even_while_provider_is_blocked(env):
    repo, db, clock = env
    session = create(repo)
    claim = repo.claim(session["active_run_id"], "worker")
    repo.start(claim)
    repo.cancel(session["id"], "alice")
    clock[0] += CANCEL_GRACE_SECONDS + 1
    with pytest.raises(LeaseLost):
        repo.heartbeat(claim)
    assert doc(db, SESSIONS_COLLECTION, session["id"])["status"] == "cancelled"
    assert doc(db, SESSIONS_COLLECTION, session["id"])["recovery_required"]


def test_run_deadline_cannot_be_renewed_forever(env):
    repo, db, clock = env
    session = create(repo)
    claim = repo.claim(session["active_run_id"], "worker")
    repo.start(claim)
    for _ in range(MAX_RUN_SECONDS // 10):
        clock[0] += 10
        try:
            repo.heartbeat(claim)
        except LeaseLost:
            break
    assert doc(db, JOBS_COLLECTION, claim.job_id)["state"] == "interrupted"


def test_cleanup_failure_retains_intent_for_retry(env):
    repo, db, clock = env
    session = create(repo)
    repo.cancel(session["id"], "alice")
    calls = []
    def failed_close(sid):
        calls.append(sid)
        raise TimeoutError("provider unreachable")
    assert repo.cleanup_once(failed_close, "cleaner")
    assert doc(db, CLEANUP_COLLECTION, session["id"])["state"] == "queued"
    clock[0] += 61
    assert repo.cleanup_once(lambda sid: calls.append(sid), "cleaner")
    assert doc(db, CLEANUP_COLLECTION, session["id"])["state"] == "completed"
    assert len(calls) == 2


def test_interrupted_provision_cleanup_stays_discoverable(env):
    repo, db, clock = env
    session = create(repo)
    claim = repo.claim(session["active_run_id"], "worker")
    repo.start(claim)
    repo.cancel(session["id"], "alice")
    clock[0] += LEASE_SECONDS + 1
    repo.claim(claim.job_id, "recovery")
    assert repo.cleanup_once(lambda sid: None, "cleaner")
    # A remote create may return after worker death, so no registry yet is not
    # sufficient evidence to forget this cleanup intent.
    assert doc(db, CLEANUP_COLLECTION, session["id"])["state"] == "queued"


def test_real_agent_loop_observes_cancel_after_model(env, monkeypatch):
    import app.agent.loop as loop
    from app.billing.plans import ModelEntry
    repo, db, _ = env
    session = create(repo)
    claim = repo.claim(session["active_run_id"], "worker")
    monkeypatch.setattr(loop.manager, "get", lambda sid: object())
    monkeypatch.setattr(loop, "needs_compaction", lambda _: False)
    class Adapter:
        def create(self, **kwargs):
            repo.cancel(session["id"], "alice")
            return object()
        def usage(self, response):
            pytest.fail("model response must not dispatch work after cancellation")
    def handler(job, state):
        state["system_prompt"] = "test"
        loop._agent_loop(state, Adapter(), ModelEntry("test", "model", None), [])
    process_claim(repo, claim, handler)
    assert doc(db, SESSIONS_COLLECTION, session["id"])["status"] == "cancelled"


def test_sandbox_close_failure_preserves_registry(monkeypatch):
    from app.sandbox.manager import SandboxManager
    manager = SandboxManager()
    monkeypatch.setattr(manager, "get", lambda sid: (_ for _ in ()).throw(ValueError("unreachable")))
    with pytest.raises(ValueError):
        manager.close("session")  # no DB delete or credential initialization


def test_http_routes_use_durable_commands_without_background_execution(env, monkeypatch):
    from fastapi.testclient import TestClient
    import app.api.routes as routes
    import app.ratelimit as ratelimit
    monkeypatch.setenv("STRIPE_SECRET_KEY", "test-not-a-real-key")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "test-not-a-real-secret")
    from app.main import app
    from app.auth import get_uid
    repo, db, _ = env
    monkeypatch.setattr(routes, "runs", repo)
    monkeypatch.setattr(routes, "get_session", lambda sid: copy.deepcopy(db.data.get((SESSIONS_COLLECTION, sid))))
    monkeypatch.setattr(ratelimit, "check_rate_limit", lambda *args: None)
    monkeypatch.setattr(routes.manager, "create", lambda *args: pytest.fail("HTTP handler must not provision"))
    app.dependency_overrides[get_uid] = lambda: "alice"
    try:
        with TestClient(app) as client:
            response = client.post("/sessions", headers={"Idempotency-Key": "create"}, json={"task": "hello", "kind": "code"})
            assert response.status_code == 200
            created = response.json()
            sid = created["id"]
            assert created["status"] == "queued"
            assert created["active_run_id"]
            assert created["created_at"].endswith("+00:00")
            assert client.get(f"/sessions/{sid}").json()["status"] == "queued"
            assert client.post("/sessions", headers={"Idempotency-Key": "create"}, json={"task": "different"}).status_code == 409
            waiting(repo, created)
            bad = client.post(f"/sessions/{sid}/approve", headers={"Idempotency-Key": "approve"}, json={"decisions": []})
            assert bad.status_code == 422
            accepted = client.post(f"/sessions/{sid}/approve", headers={"Idempotency-Key": "approve"}, json={
                "approval_id": "batch-1", "decisions": [{"call_id": "call-1", "approved": False}]})
            assert accepted.status_code == 200
            assert accepted.json()["status"] == "queued"
            cancelled = client.delete(f"/sessions/{sid}")
            assert cancelled.status_code == 200
            assert cancelled.json()["status"] == "cancelled"
            assert client.get(f"/sessions/{sid}/preview", params={"port": 3000}).status_code == 409
            assert client.get(f"/sessions/{sid}/desktop").status_code == 409
            assert client.get(f"/sessions/{sid}/screenshot").status_code == 409
    finally:
        app.dependency_overrides.pop(get_uid, None)


def test_deadline_and_crash_enqueue_cleanup_without_cancel(env):
    repo, db, clock = env
    session = create(repo)
    claim = repo.claim(session["active_run_id"], "worker")
    repo.start(claim)
    clock[0] += LEASE_SECONDS + 1
    repo.claim(claim.job_id, "recover")
    assert doc(db, CLEANUP_COLLECTION, session["id"])["state"] == "queued"


def test_failure_enqueues_cleanup_without_cancel(env):
    repo, db, _ = env
    session = create(repo)
    claim = repo.claim(session["active_run_id"], "worker")
    repo.start(claim)
    repo.finish(claim, failed=True)
    assert doc(db, CLEANUP_COLLECTION, session["id"])["state"] == "queued"

"""Synthetic consent/status only; never create actual user access or call Firebase."""
import copy
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

import app.auth as auth
import app.api.task_authorization as routes
from app.auth import QueueIdentity
from app.auth_account import AccountEvidence, BoundedAccountReader
from app.execution.task_authorization import GRANTS_COLLECTION, TaskQueueAuthorizer, OPERATIONS
from app.execution.repository import CommandError, JOBS_COLLECTION, LeaseLost
from app.billing.budgets import BudgetLedger, BudgetExceeded, BUDGETS_COLLECTION, RESERVATIONS_COLLECTION
from app.billing.plans import ModelEntry
from app.store import SESSIONS_COLLECTION
from test_durable_runs import env, doc
from test_queued_messages import setup, identity, quiesce
from test_product_contracts import assert_error, running


class SyntheticAccounts:
    def __init__(self):
        self.disabled, self.email, self.valid_after, self.calls, self.fail, self.stale = False, True, 0, 0, False, False
    def read(self, uid, *, timeout, ttl, clock):
        self.calls += 1
        if self.fail:
            raise RuntimeError("private synthetic SDK diagnostic")
        now = clock()
        return AccountEvidence(uid, self.disabled, self.email, self.valid_after, now,
            now - 1 if self.stale else now + ttl, time.monotonic() + ttl)


def policy():
    return {"schema_version": 1, "version": 1, "max_duration_seconds": 6000, "max_turns": 5,
            "max_run_seconds": 60, "max_model_requests": 10, "max_reserved_tokens": 10000,
            "reader_timeout_seconds": 3, "evidence_ttl_seconds": 10, "allowed_modes": ["build", "plan"],
            "allowed_models": ["fixture-model"], "allowed_tools": ["read_file", "write_file", "update_plan"]}


def request(**changes):
    return {"mode": "build", "model": "fixture-model", "provider": "anthropic", "effort": None,
            "operations": sorted(OPERATIONS), "tools": ["read_file", "write_file"], "expires_at": 5000,
            "max_turns": 2, "max_run_seconds": 30, "max_model_requests": 2, "max_reserved_tokens": 1000} | changes


def configured(env):
    repo, db, _ = env
    session = setup(env, maximum=8)
    repo.account_reader = SyntheticAccounts()
    db.data[("config", "task_authorization")] = policy()
    return session


def consent(env, session, key="consent", payload=None):
    return env[0].consent_task(session["id"], identity(env), key, request() if payload is None else payload)


def attach(env, session, grant, key="queued", expiry=4000):
    return env[0].enqueue_message(session["id"], "alice", key, "explicit scoped user task", expiry,
        identity(env), task_authorization_id=grant["id"])


def promote(env, session):
    repo, _, _ = env
    candidate = repo.queue_candidates()[0]
    return repo.dispatch_next(session["id"], "alice", "worker:" + candidate["id"], TaskQueueAuthorizer(repo)(candidate))


def authorized_run(env, payload=None):
    session = configured(env)
    grant = consent(env, session, payload=payload)
    attach(env, session, grant)
    quiesce(env, session)
    result = promote(env, session)
    claim, current = running(env[0], result["session"])
    return session, grant, claim, current


def test_consent_replay_revocation_version_races_and_no_resurrection(env):
    repo, db, _ = env
    session = configured(env)
    with ThreadPoolExecutor(3) as pool:
        values = list(pool.map(lambda _: consent(env, session), range(3)))
    grant = values[0]
    assert all(value == grant for value in values)
    assert grant["version"] == 1 and grant["uses"] == 0
    assert "consent_iat" not in grant and "uid" not in grant
    def revoke(key):
        try:
            return repo.revoke_task(session["id"], "alice", grant["id"], key, 1)
        except CommandError as error:
            return error.status
    with ThreadPoolExecutor(2) as pool:
        outcomes = list(pool.map(revoke, ["one", "two"]))
    assert sum(isinstance(value, dict) for value in outcomes) == 1 and outcomes.count(409) == 1
    before = copy.deepcopy(db.data)
    assert consent(env, session) == grant
    assert repo.inspect_task(session["id"], "alice", grant["id"])["status"] == "revoked"
    assert db.data == before


@pytest.mark.parametrize("bad", [{"max_turns": 0}, {"max_reserved_tokens": 10001}, {"mode": "plan"},
    {"model": "different"}, {"provider": "different"}, {"tools": ["bash"]},
    {"operations": ["queue.dispatch", "send-email"]}, {"expires_at": 100000}])
def test_explicit_scope_cannot_widen_mode_model_tools_operations_or_bounds(env, bad):
    repo, db, _ = env
    session = configured(env)
    before = copy.deepcopy(db.data)
    with pytest.raises(CommandError):
        consent(env, session, payload=request(**bad))
    assert db.data == before


@pytest.mark.parametrize("missing", ["policy", "reader"])
def test_missing_configuration_cannot_create_authorization(env, missing):
    repo, db, _ = env
    session = configured(env)
    if missing == "policy":
        del db.data[("config", "task_authorization")]
    else:
        repo.account_reader = None
    before = copy.deepcopy(db.data)
    assert_error(503, lambda: consent(env, session))
    assert db.data == before


def test_cross_owner_scope_and_unsupported_firebase_namespace_fail_before_reader(env):
    repo, db, clock = env
    session = configured(env)
    grant = consent(env, session)
    for operation in [lambda: repo.inspect_task(session["id"], "mallory", grant["id"]),
        lambda: repo.revoke_task(session["id"], "mallory", grant["id"], "x", 1),
        lambda: repo.consent_task(session["id"], QueueIdentity("mallory", 900, 2000, 1001), "x", request())]:
        assert_error(404, operation)
    calls = repo.account_reader.calls
    tenant = QueueIdentity("alice", 900, 2000, clock[0] + 1, "another-tenant")
    assert_error(403, lambda: repo.consent_task(session["id"], tenant, "tenant", request()))
    assert repo.account_reader.calls == calls


@pytest.mark.parametrize("denial", ["disabled", "email", "revoked", "unavailable", "stale"])
def test_current_account_facts_fail_closed_without_grants(env, denial):
    repo, db, _ = env
    session = configured(env)
    reader = repo.account_reader
    if denial == "disabled": reader.disabled = True
    elif denial == "email": reader.email = False
    elif denial == "revoked": reader.valid_after = 900001
    elif denial == "unavailable": reader.fail = True
    else: reader.stale = True
    before = copy.deepcopy(db.data)
    with pytest.raises(Exception) as failure:
        consent(env, session)
    assert "private synthetic" not in str(failure.value)
    assert db.data == before


def test_fresh_policy_cannot_race_old_account_reader_settings(env):
    repo, db, _ = env
    session = configured(env)
    original = repo.account_reader.read
    def changed(*args, **kwargs):
        evidence = original(*args, **kwargs)
        db.data[("config", "task_authorization")]["version"] = 2
        return evidence
    repo.account_reader.read = changed
    assert_error(503, lambda: consent(env, session))
    assert not any(c == GRANTS_COLLECTION for c, _ in db.data)


def test_durable_scope_exceeds_login_expiry_only_via_explicit_new_consent(env):
    repo, db, clock = env
    session = configured(env)
    grant = consent(env, session)
    item = attach(env, session, grant)["queuedMessage"]
    assert item["expiresAt"] > "1970-01-01T00:33:20+00:00"
    quiesce(env, session)
    clock[0] = 2100  # Original login exp2000 is not standing task authority.
    accepted = promote(env, session)
    assert accepted["dispatched"] is True and accepted["session"]["status"] == "queued"
    assert repo.inspect_task(session["id"], "alice", grant["id"])["uses"] == 1


def test_generic_owner_dispatch_cannot_bypass_revoked_pinned_grant(env):
    repo, db, _ = env
    session = configured(env)
    grant = consent(env, session)
    attach(env, session, grant)
    quiesce(env, session)
    repo.revoke_task(session["id"], "alice", grant["id"], "revoke", 1)
    before = copy.deepcopy(db.data)
    assert_error(403, lambda: repo.dispatch_next(session["id"], "alice", "owner", identity(env), owner_request=True))
    assert db.data == before


def test_turn_ceiling_and_model_reservations_are_cumulative(env):
    repo, db, clock = env
    session, grant, claim, current = authorized_run(env, request(max_turns=1, max_model_requests=2, max_reserved_tokens=10))
    ledger = BudgetLedger(db, clock=lambda: clock[0], transaction_runner=db.transaction)
    model = ModelEntry("anthropic", "fixture-model", None)
    first = ledger.reserve("alice", model, {"requests_per_day": 100, "tokens_per_month": 100000}, 6, run_id=claim.job_id)
    ledger.settle(first, {"input_tokens": 1, "output_tokens": 1})
    before = copy.deepcopy(db.data)
    with pytest.raises(BudgetExceeded):
        ledger.reserve("alice", model, {"requests_per_day": 100, "tokens_per_month": 100000}, 5, run_id=claim.job_id)
    assert db.data == before  # Settlement never renews reserved-token authorization.
    second = ledger.reserve("alice", model, {"requests_per_day": 100, "tokens_per_month": 100000}, 4, run_id=claim.job_id)
    with pytest.raises(BudgetExceeded):
        ledger.reserve("alice", model, {"requests_per_day": 100, "tokens_per_month": 100000}, 1, run_id=claim.job_id)
    assert second != first
    attach(env, session, grant, key="second")
    current["status"] = "idle"
    repo.save(claim, current)
    repo.finish(claim)
    repo.confirm_quiescent(claim)
    assert_error(403, lambda: promote(env, session))


def test_revoke_stops_new_actions_reservations_and_reader_but_settlement_and_failure_remain(env):
    repo, db, clock = env
    session, grant, claim, current = authorized_run(env)
    ledger = BudgetLedger(db, clock=lambda: clock[0], transaction_runner=db.transaction)
    model = ModelEntry("anthropic", "fixture-model", None)
    rid = ledger.reserve("alice", model, {"requests_per_day": 100, "tokens_per_month": 100000}, 10, run_id=claim.job_id)
    repo.revoke_task(session["id"], "alice", grant["id"], "revoke", 1)
    assert_error(403, lambda: repo.checkpoint(claim))
    with pytest.raises(BudgetExceeded):
        ledger.reserve("alice", model, {"requests_per_day": 100, "tokens_per_month": 100000}, 10, run_id=claim.job_id)
    ledger.settle(rid, {"input_tokens": 2, "output_tokens": 3})
    repo.account_reader = None
    repo.finish(claim, failed=True)
    assert doc(db, RESERVATIONS_COLLECTION, rid)["state"] == "settled"
    assert doc(db, SESSIONS_COLLECTION, session["id"])["recovery_required"] is True


def test_scope_and_deadline_are_parent_owned_and_no_shell_or_connector_is_granted(env):
    repo, db, _ = env
    session, grant, claim, current = authorized_run(env)
    assert doc(db, JOBS_COLLECTION, claim.job_id)["run_deadline"] == 1030
    current.update(task_authorization_id="forged", task_authorization_version=999, task_root_deadline=999999,
                   task_allowed_tools=["bash", "create_pull_request"])
    repo.save(claim, current)
    assert current["task_authorization_id"] == grant["id"] and current["task_root_deadline"] == 1030
    assert "bash" not in current["task_allowed_tools"]
    with pytest.raises(Exception):
        repo.prepare_actions(claim, step=0, calls=[{"id": "shell", "name": "bash", "input": {"command": "echo synthetic"}}])


@pytest.mark.parametrize("revoked", [False, True])
def test_approval_resume_keeps_original_grant_and_deadline(env, monkeypatch, revoked):
    from test_durable_runs import seal_pending
    import app.agent.risk as risk
    repo, db, clock = env
    session, grant, claim, current = authorized_run(env)
    monkeypatch.setitem(risk.RISK_POLICY, "write_file", "always")
    seal_pending(repo, claim, current, {"id": "approval", "provider": "anthropic", "model": "fixture-model", "quota": None,
        "calls": [{"id": "risk", "name": "write_file", "input": {"path": "fixture", "content": "fixture"}, "risky": True}]})
    repo.save(claim, current)
    repo.finish(claim)
    if revoked:
        repo.revoke_task(session["id"], "alice", grant["id"], "revoke", 1)
        before = copy.deepcopy(db.data)
        assert_error(403, lambda: repo.submit("alice", "approve", "approve", {"approval_id": current["pending_approval"]["id"],
            "decisions": [{"call_id": "risk", "approved": True}]}, session["id"]))
        assert db.data == before
    else:
        clock[0] = 1005
        approved = repo.submit("alice", "approve", "approve", {"approval_id": current["pending_approval"]["id"],
            "decisions": [{"call_id": "risk", "approved": True}]}, session["id"])
        next_claim, _ = running(repo, approved)
        job = doc(db, JOBS_COLLECTION, next_claim.job_id)
        assert job["task_authorization_id"] == grant["id"] and job["run_deadline"] == 1030


def account_ok(job, session):
    return {"uid": job["uid"], "disabled": False, "email_verified": True, "valid_after_ms": 0}


def account_bad_uid(job, session):
    return account_ok({"uid": "another-owner"}, session)


def account_crash(job, session):
    import os
    os._exit(17)


def account_hang(job, session):
    time.sleep(60)


def account_private_error(job, session):
    raise SystemExit("private synthetic token diagnostic")


def test_real_bounded_account_reader_reuses_original_freshness_without_tokens():
    clock = [1000.0]
    reader = BoundedAccountReader(account_ok, max_concurrent=1)
    evidence = reader.read("synthetic", timeout=5, ttl=10, clock=lambda: clock[0])
    assert evidence.uid == "synthetic" and evidence.checked_at == 1000 and evidence.valid_until == 1010
    clock[0] = 1001
    assert reader.read("synthetic", timeout=5, ttl=10, clock=lambda: clock[0]) is evidence


@pytest.mark.parametrize("handler", [account_bad_uid, account_crash, account_hang, account_private_error])
def test_real_reader_faults_are_sanitized_and_owned_processes_reaped(handler):
    started = time.monotonic()
    reader = BoundedAccountReader(handler, max_concurrent=1)
    with pytest.raises(auth.AuthenticationError) as error:
        reader.read("synthetic", timeout=2, ttl=5, clock=lambda: 1000)
    assert "private synthetic" not in str(error.value)
    assert time.monotonic() - started < 12


@pytest.fixture
def client(env, monkeypatch):
    from app.main import app
    monkeypatch.setattr(routes, "runs", env[0])
    monkeypatch.setattr("app.ratelimit.check_rate_limit", lambda *args: None)
    app.dependency_overrides[auth.get_uid] = lambda: "alice"
    app.dependency_overrides[auth.get_task_identity] = lambda: identity(env)
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


def test_native_http_consent_inspect_revoke_with_synthetic_fixture_only(env, client):
    session = configured(env)
    body = request()
    body["expiresAt"] = "1970-01-01T01:23:20Z"
    body.pop("expires_at")
    path = f"/sessions/{session['id']}/task-authorizations"
    created = client.post(path, json=body, headers={"Idempotency-Key": "one"})
    assert created.status_code == 201, created.text
    gid = created.json()["id"]
    assert client.get(path+"/"+gid).json()["status"] == "active"
    revoked = client.post(path+"/"+gid+"/revoke", json={"expected_version": 1}, headers={"Idempotency-Key": "revoke"})
    assert revoked.status_code == 200 and revoked.json()["version"] == 2


@pytest.mark.parametrize("stop", ["revoked", "disabled", "unavailable", "expired"])
def test_task_denial_before_start_keeps_prior_workspace_and_slots(env, stop):
    from app.worker import process_claim
    from app.execution.repository import CLEANUP_COLLECTION
    repo, db, clock = env
    session = configured(env)
    grant = consent(env, session, payload=request(expires_at=1002) if stop == "expired" else None)
    attach(env, session, grant, expiry=1002 if stop == "expired" else 4000)
    quiesce(env, session)
    accepted = promote(env, session)
    claim = repo.claim(accepted["session"]["active_run_id"], "worker")
    if stop == "revoked": repo.revoke_task(session["id"], "alice", grant["id"], "stop", 1)
    elif stop == "disabled": repo.account_reader.disabled = True
    elif stop == "unavailable": repo.account_reader.fail = True
    else: clock[0] = 1003
    effects = []
    process_claim(repo, claim, lambda *args: effects.append(True))
    assert not effects
    assert not any(c == CLEANUP_COLLECTION for c, _ in db.data)
    assert db.data[("agent_v2_slots", "alice")]["sandboxes"] == [session["id"]]
    assert db.data[("agent_v2_slots", "alice")]["runs"] == []
    assert doc(db, SESSIONS_COLLECTION, session["id"])["recovery"]["reason"] == "queued_run_not_started"


def test_missing_initialized_task_budget_never_resets_after_spend(env):
    from app.billing.budgets import _key
    repo, db, clock = env
    session, grant, claim, _ = authorized_run(env)
    ledger = BudgetLedger(db, clock=lambda: clock[0], transaction_runner=db.transaction)
    model = ModelEntry("anthropic", "fixture-model", None)
    ledger.reserve("alice", model, {"requests_per_day": 100, "tokens_per_month": 100000}, 5, run_id=claim.job_id)
    del db.data[(BUDGETS_COLLECTION, _key("alice", "task", grant["id"]))]
    before = copy.deepcopy(db.data)
    with pytest.raises(BudgetExceeded):
        ledger.reserve("alice", model, {"requests_per_day": 100, "tokens_per_month": 100000}, 5, run_id=claim.job_id)
    assert db.data == before
    assert repo.revoke_task(session["id"], "alice", grant["id"], "withdraw", 1)["status"] == "revoked"


def test_slow_task_budget_read_cannot_reserve_with_expired_status(env, monkeypatch):
    from app.billing.budgets import _key
    from test_durable_runs import Reference
    repo, db, clock = env
    _, grant, claim, _ = authorized_run(env)
    ledger = BudgetLedger(db, clock=lambda: clock[0], transaction_runner=db.transaction)
    target = _key("alice", "task", grant["id"])
    original = Reference.get
    def slow(ref, transaction=None):
        result = original(ref, transaction=transaction)
        if ref.collection == BUDGETS_COLLECTION and ref.id == target:
            clock[0] += 11
        return result
    monkeypatch.setattr(Reference, "get", slow)
    before = copy.deepcopy(db.data)
    with pytest.raises(BudgetExceeded):
        ledger.reserve("alice", ModelEntry("anthropic", "fixture-model", None), {"requests_per_day": 100, "tokens_per_month": 100000}, 5, run_id=claim.job_id)
    assert db.data == before


def test_revocation_after_model_reservation_stops_provider_and_keeps_receipt(env, monkeypatch):
    import app.billing.budgets as budgets
    import app.billing.entitlements as entitlements
    from app.execution.context import Guard, execution_context
    from types import SimpleNamespace
    repo, db, clock = env
    session, grant, claim, _ = authorized_run(env)
    ledger = BudgetLedger(db, clock=lambda: clock[0], transaction_runner=db.transaction)
    original = ledger.reserve
    receipts, effects = [], []
    def reserve(*args, **kwargs):
        rid = original(*args, **kwargs)
        receipts.append(rid)
        repo.revoke_task(session["id"], "alice", grant["id"], "withdraw", 1)
        return rid
    ledger.reserve = reserve
    resolve = entitlements.resolve_entitlements
    monkeypatch.setattr(entitlements, "resolve_entitlements", lambda uid, **kwargs: resolve(uid, **(kwargs or {"client": db, "now": clock[0]})))
    monkeypatch.setattr(budgets, "budgets", ledger)
    monkeypatch.setattr(budgets, "request_allowance", lambda *args: 5)
    with execution_context(Guard(repo, claim)):
        with pytest.raises(CommandError):
            budgets.metered_create("alice", SimpleNamespace(create=lambda **kw: effects.append(True)),
                ModelEntry("anthropic", "fixture-model", None), system="fixture", messages=[], tools=[], max_tokens=1)
    assert not effects and len(receipts) == 1
    assert doc(db, RESERVATIONS_COLLECTION, receipts[0])["state"] == "reserved"


@pytest.mark.parametrize("bad", [{"allowed_modes": [{}]}, {"allowed_tools": [[]]}, {"allowed_tools": ["list_files"]}, {"schema_version": True}])
def test_malformed_or_unimplemented_policy_is_explicitly_unavailable(env, bad):
    repo, db, _ = env
    session = configured(env)
    db.data[("config", "task_authorization")].update(bad)
    assert_error(503, lambda: consent(env, session))


def test_nondefault_tenant_claim_is_rejected_by_shared_verifier(monkeypatch):
    monkeypatch.setattr(auth, "ensure_initialized", lambda: None)
    monkeypatch.setattr(auth.firebase_auth, "verify_id_token", lambda *args, **kwargs: {"uid": "alice", "firebase": {"tenant": "same-uid-other-namespace"}})
    with pytest.raises(auth.AuthenticationError):
        auth.get_uid("Bearer synthetic")


def test_reader_concurrency_admission_is_explicit_and_fails_closed(monkeypatch):
    import threading
    import app.execution.supervision as supervision
    entered, release = threading.Event(), threading.Event()
    def held(request, **kwargs):
        entered.set()
        assert release.wait(3)
        return account_ok(request, {})
    monkeypatch.setattr(supervision, "supervise_read", held)
    reader = BoundedAccountReader(account_ok, max_concurrent=1)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(reader.read, "first", timeout=2, ttl=5, clock=lambda: 1000)
        assert entered.wait(2)
        with pytest.raises(auth.AuthenticationError):
            reader.read("second", timeout=2, ttl=5, clock=lambda: 1000)
        release.set()
        assert first.result().uid == "first"


def test_actual_contained_reader_supports_cold_consent_attach_and_dispatch(env, monkeypatch):
    import datetime
    import app.execution.repository as repository
    from app.main import app
    repo, db, _ = env
    session = configured(env)
    repo.clock = time.time
    repo.account_reader = BoundedAccountReader(account_ok, max_concurrent=1)
    now = time.time()
    monkeypatch.setattr(auth, "_verify_token", lambda _: {"uid": "alice", "iat": now - 60, "exp": now + 600, "email_verified": True})
    monkeypatch.setattr(repository, "runs", repo)
    monkeypatch.setattr(routes, "runs", repo)
    monkeypatch.setattr("app.ratelimit.check_rate_limit", lambda *args: None)
    body = request()
    body.pop("expires_at")
    body["expiresAt"] = datetime.datetime.fromtimestamp(now + 1200, datetime.timezone.utc).isoformat()
    base = f"/sessions/{session['id']}/task-authorizations"
    headers = {"Authorization": "Bearer synthetic", "Idempotency-Key": "positive-cold"}
    with TestClient(app) as client:
        response = client.post(base, json=body, headers=headers)
        assert response.status_code == 201, response.text
        gid = response.json()["id"]
        queued = client.post(base+f"/{gid}/queued-messages", json={"prompt": "scoped next turn", "expiresAt": datetime.datetime.fromtimestamp(now+900, datetime.timezone.utc).isoformat()}, headers=headers)
        assert queued.status_code == 202, queued.text
        claim, current = running(repo, session)
        current["status"] = "idle"
        repo.save(claim, current)
        repo.finish(claim)
        repo.confirm_quiescent(claim)
        dispatched = client.post(base+f"/{gid}/dispatch", headers=headers)
        assert dispatched.status_code == 200 and dispatched.json()["dispatched"] is True, dispatched.text
        task_claim, _ = running(repo, dispatched.json()["session"])
        assert doc(db, JOBS_COLLECTION, task_claim.job_id)["task_authorization_id"] == gid


def test_stale_task_claim_cannot_release_or_mutate_admission(env):
    from app.worker import process_claim
    repo, db, clock = env
    session = configured(env)
    grant = consent(env, session)
    attach(env, session, grant)
    quiesce(env, session)
    accepted = promote(env, session)
    claim = repo.claim(accepted["session"]["active_run_id"], "old-worker")
    clock[0] += 46
    before = copy.deepcopy(db.data)
    process_claim(repo, claim, lambda *args: pytest.fail("stale worker must not launch"))
    assert db.data == before


def test_fractional_freshness_policy_has_no_float_subtraction_rejection(env):
    repo, db, _ = env
    session = configured(env)
    db.data[("config", "task_authorization")].update(reader_timeout_seconds=0.1, evidence_ttl_seconds=0.2)
    grant = consent(env, session)
    assert grant["status"] == "active"


def test_new_grant_cannot_rebind_an_existing_queue_item(env):
    repo, db, _ = env
    session = configured(env)
    old = consent(env, session)
    attach(env, session, old)
    newer = consent(env, session, "new-consent", request(max_reserved_tokens=2000))
    quiesce(env, session)
    from app.execution.task_authorization import TaskDispatchEvidence
    candidate = repo.queue_candidates()[0]
    old_evidence = TaskQueueAuthorizer(repo)(candidate)
    forged = TaskDispatchEvidence("alice", newer["id"], 1, candidate["id"], old_evidence.account)
    before = copy.deepcopy(db.data)
    assert_error(403, lambda: repo.dispatch_next(session["id"], "alice", "rebind", forged))
    assert db.data == before


def test_scoped_dispatch_receipt_replays_before_new_head_or_missing_reader(env):
    repo, db, _ = env
    session = configured(env)
    first, second = consent(env, session), consent(env, session, "second")
    attach(env, session, first, key="first")
    attach(env, session, second, key="second")
    quiesce(env, session)
    result = repo.dispatch_next(session["id"], "alice", "scoped", identity(env), owner_request=True, expected_task_authorization_id=first["id"])
    repo.account_reader = None
    before = copy.deepcopy(db.data)
    assert repo.dispatch_next(session["id"], "alice", "scoped", identity(env), owner_request=True, expected_task_authorization_id=first["id"]) == result
    assert db.data == before


def test_scoped_dispatch_checks_target_inside_current_fifo_transaction(env, monkeypatch):
    repo, db, _ = env
    session = configured(env)
    first, second = consent(env, session), consent(env, session, "second")
    a = attach(env, session, first, key="first")["queuedMessage"]
    attach(env, session, second, key="second")
    quiesce(env, session)
    repo.cancel_queued(session["id"], "alice", a["id"])
    before = copy.deepcopy(db.data)
    assert_error(409, lambda: repo.dispatch_next(session["id"], "alice", "wrong-head", identity(env), owner_request=True, expected_task_authorization_id=first["id"]))
    assert db.data == before

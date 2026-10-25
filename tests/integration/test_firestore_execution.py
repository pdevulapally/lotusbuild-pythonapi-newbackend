"""Real Firestore SDK transactions against an explicitly selected local emulator.

Never falls back to Firebase initialization, ADC, or a remote project. Normal
offline runs skip these tests; see docs/FIRESTORE_EMULATOR_TESTS.md.
"""
from concurrent.futures import ThreadPoolExecutor
import os
import re
import threading
import time
from types import SimpleNamespace
import uuid

import pytest
from google.api_core.exceptions import Aborted
from google.auth.credentials import AnonymousCredentials
from google.cloud.firestore_v1 import Client
from google.cloud.firestore_v1.base_query import FieldFilter
from google.cloud.firestore_v1.transaction import Transaction

from app.billing.budgets import (
    BUDGETS_COLLECTION, RESERVATIONS_COLLECTION, BudgetExceeded, BudgetLedger,
)
from app.billing.plans import ModelEntry
from app.execution.repository import (
    CLEANUP_COLLECTION, JOBS_COLLECTION, LEASE_SECONDS, RECEIPTS_COLLECTION, EVENTS_COLLECTION,
    CancelRequested, CommandError, LeaseLost, RunRepository,
)
from app.execution.actions import ACTIONS_COLLECTION, BATCHES_COLLECTION, EVIDENCE_COLLECTION
from app.execution.workspaces import CHECKPOINTS_COLLECTION, RESTORES_COLLECTION
from app.execution.errors import ExecutionStopped
from test_durable_runs import seal_pending
from app.execution.slots import MAX_ACTIVE_RUNS, SLOTS_COLLECTION
from app.store import SESSIONS_COLLECTION
from app.execution.queue import QUEUE_COLLECTION
from app.execution.task_authorization import GRANTS_COLLECTION, TaskQueueAuthorizer, OPERATIONS
from app.auth_account import AccountEvidence
from app.execution.contracts import MESSAGES_COLLECTION, PROJECTS_COLLECTION, ACCOUNTS_COLLECTION


PROJECT = "demo-lotusbuild-local"
ENTRY = ModelEntry("test-provider", "test-model", None)
LIMITS = {"requests_per_day": 100, "tokens_per_month": 1000}


def owned(db, collection, uid):
    return list(db.collection(collection).where(filter=FieldFilter("uid", "==", uid)).stream(timeout=10))


def read(env, collection, key):
    return env.db.collection(collection).document(key).get(timeout=10).to_dict()


def submit(env, key="create"):
    return env.repo.submit(env.uid, key, "create", {"task": "integration test", "kind": "code"})


def race(*operations):
    """Race database commands, then reconcile known-aborted attempts once.

    These callables only mutate emulator records, never execute remote effects.
    Finite SDK contention rejection is not an accepted command; this explicit
    retry happens after every contender finishes, not inside a production loop.
    """
    barrier = threading.Barrier(len(operations))

    def invoke(operation):
        barrier.wait(timeout=10)
        return operation()

    with ThreadPoolExecutor(len(operations)) as pool:
        futures = [pool.submit(invoke, operation) for operation in operations]
        results, retry_indices = [], []
        for index, future in enumerate(futures):
            try:
                results.append(future.result(timeout=60))
            except (Aborted, ValueError) as error:
                assert known_contention(error)
                retry_indices.append(index)
                results.append(None)
    for index in retry_indices:
        results[index] = operations[index]()
    return results


def known_contention(error):
    return isinstance(error, Aborted) or (isinstance(error, ValueError) and isinstance(error.__cause__, Aborted))


@pytest.fixture
def env():
    endpoint = os.environ.get("FIRESTORE_EMULATOR_HOST")
    if endpoint is None:
        pytest.skip("set FIRESTORE_EMULATOR_HOST to an explicitly started loopback emulator")
    from scripts.emulator_endpoint import require_endpoint
    try:
        require_endpoint(endpoint)
    except ValueError:
        pytest.fail("integration tests require a numeric IPv4 loopback emulator endpoint")
    db = Client(project=PROJECT, credentials=AnonymousCredentials())
    assert db._emulator_host == endpoint
    uid = "integration-" + uuid.uuid4().hex
    clock = [1727740799.0]  # 2024-09-30 23:59:59 UTC, fixed for period rollover.
    # No injected transaction_runner: exercise production Firestore decorators.
    context = SimpleNamespace(db=db, uid=uid, clock=clock,
                              repo=RunRepository(db, clock=lambda: clock[0]),
                              ledger=BudgetLedger(db, clock=lambda: clock[0]))
    try:
        yield context
    finally:
        # Delete only this test's synthetic records; never reset a whole project.
        try:
            for session in owned(db, SESSIONS_COLLECTION, uid):
                db.collection(CLEANUP_COLLECTION).document(session.id).delete(timeout=10)
            for collection in (SESSIONS_COLLECTION, JOBS_COLLECTION, RECEIPTS_COLLECTION,
                               BUDGETS_COLLECTION, RESERVATIONS_COLLECTION, EVENTS_COLLECTION,
                               ACTIONS_COLLECTION, BATCHES_COLLECTION, EVIDENCE_COLLECTION,
                               CHECKPOINTS_COLLECTION, RESTORES_COLLECTION, MESSAGES_COLLECTION, PROJECTS_COLLECTION, ACCOUNTS_COLLECTION, QUEUE_COLLECTION, GRANTS_COLLECTION):
                for snapshot in owned(db, collection, uid):
                    snapshot.reference.delete(timeout=10)
            db.collection(SLOTS_COLLECTION).document(uid).delete(timeout=10)
            db.collection("subscriptions").document(uid).delete(timeout=10)
            for prefix in ("fixture-approval-", "fixture-selection-"):
                db.collection("plans").document(prefix + uid).delete(timeout=10)
        finally:
            db.close()


def selection_plan(env, models=None):
    tier = "fixture-selection-" + env.uid
    env.db.collection("plans").document(tier).set({"limits": LIMITS, "models": [
        {"provider": "anthropic", "model": "fixture-selected", "quota": None}] if models is None else models})
    env.db.collection("subscriptions").document(env.uid).set({
        "tier_id": tier, "status": "active", "current_period_end": env.clock[0] + 3600})


def test_selection_plan_preflight_persistence_and_transaction_rollback(env):
    from app.agent.policy import ToolPolicyDenied
    from app.billing.model_select import SelectionError
    selection_plan(env)
    payload = {"task": "fixture", "kind": "code", "model": "fixture-selected", "mode": "plan"}
    first, replay = race(lambda: env.repo.submit(env.uid, "selected", "create", payload),
                         lambda: env.repo.submit(env.uid, "selected", "create", payload))
    assert first == replay and first["model"] == "fixture-selected"
    claim = env.repo.claim(first["active_run_id"], "worker")
    _, current = env.repo.start(claim)
    calls = [{"id": "read", "name": "read_file", "input": {"path": "fixture"}},
             {"id": "write", "name": "write_file", "input": {"path": "fixture", "content": "x"}}]
    before = read(env, SESSIONS_COLLECTION, first["id"])
    with pytest.raises(ToolPolicyDenied):
        env.repo.prepare_actions(claim, step=0, calls=calls)
    assert read(env, SESSIONS_COLLECTION, first["id"]) == before
    assert owned(env.db, ACTIONS_COLLECTION, env.uid) == []
    forged = {**current, "mode": "build"}
    with pytest.raises(LeaseLost):
        env.repo.save(claim, forged)
    batch = env.repo.prepare_actions(claim, step=0, calls=calls[:1])
    env.repo.permit_action(claim, action_id=batch["action_ids"][0], name="read_file", input={"path": "fixture"})
    env.repo.record_action_result(claim, action_id=batch["action_ids"][0], result_digest="a" * 64)
    current = batch["session"]
    current["status"] = "idle"
    env.repo.save(claim, current)
    env.repo.finish(claim)
    next_turn = env.repo.submit(env.uid, "next", "message", {"content": "build", "mode": "build"}, first["id"])
    assert (next_turn["model"], next_turn["provider"], next_turn["mode"]) == ("fixture-selected", "anthropic", "build")
    with pytest.raises(SelectionError):
        env.repo.submit(env.uid, "effort", "create", {**payload, "effort": "high"})
    assert len(owned(env.db, JOBS_COLLECTION, env.uid)) == 2


def test_selection_entitlement_revocation_blocks_parent_permit_atomically(env):
    from app.billing.model_select import SelectionError
    selection_plan(env)
    session = env.repo.submit(env.uid, "selected", "create", {
        "task": "fixture", "kind": "code", "model": "fixture-selected"})
    claim = env.repo.claim(session["active_run_id"], "worker")
    env.repo.start(claim)
    call = {"id": "write", "name": "write_file", "input": {"path": "fixture", "content": "x"}}
    batch = env.repo.prepare_actions(claim, step=0, calls=[call])
    selection_plan(env, models=[])
    before = read(env, JOBS_COLLECTION, claim.job_id)
    with pytest.raises(SelectionError):
        env.repo.permit_action(claim, action_id=batch["action_ids"][0], name=call["name"], input=call["input"])
    assert read(env, JOBS_COLLECTION, claim.job_id) == before
    assert read(env, ACTIONS_COLLECTION, batch["action_ids"][0])["state"] == "prepared"


def test_legacy_approval_plan_and_current_entitlement_survive_new_repository(env):
    from app.agent.policy import ToolPolicyDenied
    from app.billing.model_select import SelectionError
    session = submit(env)
    claim = env.repo.claim(session["active_run_id"], "first")
    _, state = env.repo.start(claim)
    call = {"id": "risk", "name": "bash", "input": {"command": "sudo fixture"}, "risky": True}
    seal_pending(env.repo, claim, state, {"id": "approval", "calls": [call]})
    env.repo.save(claim, state)
    queued = env.repo.submit(env.uid, "approve", "approve", {"approval_id": "approval",
                 "decisions": [{"call_id": "risk", "approved": True}]}, session["id"])
    restarted = RunRepository(env.db, clock=lambda: env.clock[0])
    second = restarted.claim(queued["active_run_id"], "second")
    restarted.start(second)
    env.db.collection(SESSIONS_COLLECTION).document(session["id"]).set({"mode": "plan"}, merge=True)
    with pytest.raises(ToolPolicyDenied):
        restarted.permit_action(second, action_id=call["action_id"], name="bash", input=call["input"])
    env.db.collection(SESSIONS_COLLECTION).document(session["id"]).set({"mode": "build"}, merge=True)
    env.db.collection("plans").document("fixture-approval-" + env.uid).set({"models": []}, merge=True)
    with pytest.raises(SelectionError):
        restarted.permit_action(second, action_id=call["action_id"], name="bash", input=call["input"])
    assert read(env, ACTIONS_COLLECTION, call["action_id"])["state"] == "prepared"


def test_same_command_race_commits_one_receipt_job_and_slot(env):
    first, second = race(lambda: submit(env), lambda: submit(env))
    assert first == second
    assert len(owned(env.db, SESSIONS_COLLECTION, env.uid)) == 1
    assert len(owned(env.db, JOBS_COLLECTION, env.uid)) == 1
    assert len(owned(env.db, RECEIPTS_COLLECTION, env.uid)) == 1
    assert len(owned(env.db, EVENTS_COLLECTION, env.uid)) == 1
    assert env.repo.event_history(first["id"], env.uid)[0]["type"] == "session.queued"
    assert read(env, SLOTS_COLLECTION, env.uid) == {"runs": [first["active_run_id"]], "sandboxes": [first["id"]]}
    assert first["active_run_id"] in env.repo.candidates()
    with pytest.raises(CommandError, match="different input"):
        env.repo.submit(env.uid, "create", "create", {"task": "changed", "kind": "code"})


def test_competing_submissions_never_exceed_capacity_or_leave_partial_documents(env, record_property):
    def attempt(index):
        try:
            return "accepted", submit(env, str(index))
        except CommandError as error:
            assert error.status == 429
            return "capacity-rejected", None
        except (Aborted, ValueError) as error:
            assert known_contention(error)
            return "contention-rejected", None

    outcomes = race(*(lambda i=i: attempt(i) for i in range(MAX_ACTIVE_RUNS + 2)))
    accepted = [value for outcome, value in outcomes if outcome == "accepted"]
    record_property("accepted", len(accepted))
    record_property("contention_rejected", sum(outcome == "contention-rejected" for outcome, _ in outcomes))
    record_property("capacity_rejected", sum(outcome == "capacity-rejected" for outcome, _ in outcomes))
    assert len(accepted) <= MAX_ACTIVE_RUNS
    for collection in (SESSIONS_COLLECTION, JOBS_COLLECTION, RECEIPTS_COLLECTION):
        assert len(owned(env.db, collection, env.uid)) == len(accepted)
    assert len(owned(env.db, EVENTS_COLLECTION, env.uid)) == len(accepted)
    slots = read(env, SLOTS_COLLECTION, env.uid) or {"runs": [], "sandboxes": []}
    assert set(slots["runs"]) == {value["active_run_id"] for value in accepted}
    assert set(slots["sandboxes"]) == {value["id"] for value in accepted}


def test_competing_claims_and_lease_reclaim_fence_the_previous_owner(env):
    session = submit(env)
    claims = race(lambda: env.repo.claim(session["active_run_id"], "first"),
                  lambda: env.repo.claim(session["active_run_id"], "second"))
    assert sum(claim is not None for claim in claims) == 1
    old = next(claim for claim in claims if claim is not None)
    env.clock[0] += LEASE_SECONDS + 1
    new = env.repo.claim(old.job_id, "replacement")
    assert new.token > old.token
    with pytest.raises(LeaseLost):
        env.repo.start(old)
    with pytest.raises(LeaseLost):
        env.repo.heartbeat(old)
    env.repo.start(new)
    env.repo.heartbeat(new)
    assert read(env, JOBS_COLLECTION, new.job_id)["state"] == "running"


def test_competing_approval_keys_claim_one_immutable_batch(env):
    session = submit(env)
    claim = env.repo.claim(session["active_run_id"], "worker")
    _, current = env.repo.start(claim)
    seal_pending(env.repo, claim, current, {"id": "batch", "calls": [
        {"id": "call", "name": "bash", "input": {"command": "sudo synthetic"}, "risky": True}]})
    env.repo.save(claim, current)
    env.repo.finish(claim)
    assert read(env, SLOTS_COLLECTION, env.uid)["runs"] == []
    assert read(env, JOBS_COLLECTION, claim.job_id)["state"] == "waiting_for_approval"

    def approve(key):
        try:
            return env.repo.submit(env.uid, key, "approve", {
                "approval_id": "batch", "decisions": [{"call_id": "call", "approved": False}]}, session["id"])
        except CommandError as error:
            assert error.status == 409
            return None

    results = race(lambda: approve("decision-one"), lambda: approve("decision-two"))
    accepted = [value for value in results if value]
    assert len(accepted) == 1
    assert len(owned(env.db, JOBS_COLLECTION, env.uid)) == 2
    assert len(owned(env.db, RECEIPTS_COLLECTION, env.uid)) == 2
    assert read(env, SLOTS_COLLECTION, env.uid)["runs"] == [accepted[0]["active_run_id"]]
    with pytest.raises(CommandError) as rejected:
        env.repo.cancel(session["id"], "different-tenant")
    assert rejected.value.status == 404


def test_cancellation_races_save_and_cleanup_releases_only_confirmed_capacity(env, record_property):
    session = submit(env)
    claim = env.repo.claim(session["active_run_id"], "worker")
    _, stale = env.repo.start(claim)
    stale["status"] = "idle"

    def finish_turn():
        try:
            env.repo.save(claim, stale)
        except CancelRequested:
            pass
        except (Aborted, ValueError) as error:
            assert known_contention(error)
            return "contention-rejected"

    def cancel():
        try:
            env.repo.cancel(session["id"], env.uid)
        except (Aborted, ValueError) as error:
            assert known_contention(error)
            return "contention-rejected"

    outcomes = race(cancel, finish_turn)
    record_property("contention_rejected", outcomes.count("contention-rejected"))
    # A rejected cancellation is not an accepted intent. Explicitly retry this
    # idempotent database command after contention; never retry a remote effect.
    env.repo.cancel(session["id"], env.uid)
    env.repo.finish(claim)
    assert read(env, SESSIONS_COLLECTION, session["id"])["status"] == "cancelled"
    assert read(env, SLOTS_COLLECTION, env.uid) == {"runs": [], "sandboxes": [session["id"]]}
    with pytest.raises(LeaseLost):
        env.repo.save(claim, stale)

    def failed_close(session_id):
        assert session_id == session["id"]
        raise TimeoutError("synthetic cleanup failure")

    assert env.repo.cleanup_once(failed_close, "cleaner")
    assert read(env, SLOTS_COLLECTION, env.uid)["sandboxes"] == [session["id"]]
    env.clock[0] += 61
    closed = []
    assert env.repo.cleanup_once(closed.append, "cleaner")
    assert closed == [session["id"]]
    assert read(env, SLOTS_COLLECTION, env.uid) == {"runs": [], "sandboxes": []}
    assert read(env, CLEANUP_COLLECTION, session["id"])["state"] == "completed"
    events = env.repo.event_history(session["id"], env.uid)
    assert events[-1]["type"] == "session.cancelled"
    assert [int(event["id"].split("-")[1]) for event in events] == list(range(1, len(events) + 1))


def test_expired_running_job_is_not_replayed_or_refunded(env):
    session = submit(env)
    claim = env.repo.claim(session["active_run_id"], "worker")
    env.repo.start(claim)
    env.clock[0] += LEASE_SECONDS + 1
    assert env.repo.claim(claim.job_id, "recovery") is None
    current = read(env, SESSIONS_COLLECTION, session["id"])
    assert current["recovery_required"] and current["status"] == "error"
    assert read(env, JOBS_COLLECTION, claim.job_id)["state"] == "interrupted"
    assert env.repo.cleanup_once(lambda sid: None, "cleaner")
    assert read(env, CLEANUP_COLLECTION, session["id"])["state"] == "queued"
    assert read(env, SLOTS_COLLECTION, env.uid) == {"runs": [claim.job_id], "sandboxes": [session["id"]]}
    with pytest.raises(LeaseLost):
        env.repo.checkpoint(claim)


def test_heartbeat_deadline_revokes_authority_without_releasing_uncertain_slots(env, monkeypatch):
    import app.execution.repository as repository

    monkeypatch.setattr(repository, "MAX_RUN_SECONDS", 10)
    session = submit(env)
    claim = env.repo.claim(session["active_run_id"], "worker")
    env.repo.start(claim)
    env.clock[0] += 11
    with pytest.raises(LeaseLost):
        env.repo.heartbeat(claim)
    assert read(env, SESSIONS_COLLECTION, session["id"])["recovery_required"]
    assert read(env, SLOTS_COLLECTION, env.uid)["runs"] == [claim.job_id]
    assert read(env, CLEANUP_COLLECTION, session["id"])["state"] == "queued"


def test_budget_reservation_race_never_overspends_or_leaves_partial_receipts(env, record_property):
    def reserve():
        try:
            return env.ledger.reserve(env.uid, ENTRY, LIMITS, 300)
        except BudgetExceeded:
            return "quota-rejected"
        except (Aborted, ValueError) as error:
            # Bounded SDK retries may exhaust under contention. That is an
            # availability failure, never permission to overspend or call a model.
            assert known_contention(error)
            return "contention-rejected"

    outcomes = race(*(reserve for _ in range(6)))
    reservations = [value for value in outcomes if not value.endswith("rejected")]
    record_property("accepted", len(reservations))
    record_property("contention_rejected", outcomes.count("contention-rejected"))
    record_property("quota_rejected", outcomes.count("quota-rejected"))
    assert len(reservations) <= 3
    assert len(owned(env.db, RESERVATIONS_COLLECTION, env.uid)) == len(reservations)
    assert len(owned(env.db, BUDGETS_COLLECTION, env.uid)) == (2 if reservations else 0)
    assert env.ledger.snapshot(env.uid, "2024-09").get("tokens", 0) == 300 * len(reservations)
    assert env.ledger.snapshot(env.uid, "2024-09-30").get("requests", 0) == len(reservations)


def test_idempotent_settlement_across_month_boundary(env):
    rid = env.ledger.reserve(env.uid, ENTRY, LIMITS, 900)
    env.clock[0] += 2
    usage = {"input_tokens": 100, "output_tokens": 20}

    def settle():
        try:
            env.ledger.settle(rid, usage)
        except ValueError as error:
            assert known_contention(error)

    race(settle, settle)
    # Reconcile the same known usage after contenders finish. Settlement may be
    # retried idempotently; this never repeats the associated provider call.
    env.ledger.settle(rid, usage)
    assert env.ledger.snapshot(env.uid, "2024-09")["tokens"] == 120
    assert env.ledger.snapshot(env.uid, "2024-10") == {}
    assert read(env, RESERVATIONS_COLLECTION, rid)["state"] == "settled"


def test_aborted_commit_retries_real_transaction_without_duplicate_reservation(env, monkeypatch):
    original = Transaction._commit
    attempts = []

    def abort_once(transaction):
        attempts.append(transaction.id)
        if len(attempts) == 1:
            # A server-side ABORTED result releases its read locks. Release the
            # actual emulator transaction as well before injecting that result.
            transaction._rollback()
            raise Aborted("synthetic failure before commit dispatch")
        return original(transaction)

    monkeypatch.setattr(Transaction, "_commit", abort_once)
    rid = env.ledger.reserve(env.uid, ENTRY, LIMITS, 300)
    assert len(attempts) == 2 and attempts[0] != attempts[1]
    assert len(owned(env.db, RESERVATIONS_COLLECTION, env.uid)) == 1
    assert read(env, RESERVATIONS_COLLECTION, rid)["tokens"] == 300
    assert env.ledger.snapshot(env.uid, "2024-09")["tokens"] == 300
    assert env.ledger.snapshot(env.uid, "2024-09-30")["requests"] == 1


def test_provider_overrun_is_recorded_once_and_blocks_later_admission(env):
    rid = env.ledger.reserve(env.uid, ENTRY, LIMITS, 200)
    for _ in range(2):
        with pytest.raises(BudgetExceeded, match="exceeded"):
            env.ledger.settle(rid, {"input_tokens": 300, "output_tokens": 2})
    assert env.ledger.snapshot(env.uid, "2024-09")["tokens"] == 302
    with pytest.raises(BudgetExceeded, match="reconciliation"):
        env.ledger.reserve(env.uid, ENTRY, LIMITS, 1)
    assert len(owned(env.db, RESERVATIONS_COLLECTION, env.uid)) == 1


def test_event_journal_uses_authoritative_metadata_and_resumes_on_real_query(env):
    session = submit(env)
    claim = env.repo.claim(session["active_run_id"], "worker")
    env.repo.start(claim)
    first = env.repo.event_history(session["id"], env.uid)
    stale = dict(session, status="idle", event_sequence=0, event_time_ms=0)
    env.repo.save(claim, stale)
    env.repo.finish(claim)
    events = env.repo.event_history(session["id"], env.uid)
    assert events[:2] == first
    assert events[-1]["type"] == "session.idle" and events[-1]["id"].endswith("-3")
    assert env.repo.event_history(session["id"], env.uid, first[-1]["id"]) == events[2:]
    with pytest.raises(CommandError) as unauthorized:
        env.repo.event_history(session["id"], "different-tenant")
    assert unauthorized.value.status == 404


def test_event_history_gap_is_not_silently_skipped(env):
    session = submit(env)
    claim = env.repo.claim(session["active_run_id"], "worker")
    env.repo.start(claim)
    env.db.collection(EVENTS_COLLECTION).document(f"{session['id']}:1").delete(timeout=10)
    with pytest.raises(CommandError, match="reconciliation"):
        env.repo.event_history(session["id"], env.uid)


@pytest.mark.parametrize("operation", ["checkpoint", "save", "finish"])
def test_deadline_boundary_commits_recovery_event_and_cleanup_without_heartbeat(env, monkeypatch, operation):
    import app.execution.repository as repository
    monkeypatch.setattr(repository, "MAX_RUN_SECONDS", 10)
    session = submit(env)
    claim = env.repo.claim(session["active_run_id"], "worker")
    _, state = env.repo.start(claim)
    env.clock[0] += 10
    with pytest.raises(LeaseLost):
        if operation == "save":
            state["status"] = "idle"
            env.repo.save(claim, state)
        elif operation == "finish":
            env.repo.finish(claim, failed=True)
        else:
            env.repo.checkpoint(claim)
    current = read(env, SESSIONS_COLLECTION, session["id"])
    assert current["status"] == "error" and current["recovery_required"]
    assert read(env, CLEANUP_COLLECTION, session["id"])["state"] == "queued"
    assert read(env, SLOTS_COLLECTION, env.uid)["runs"] == [claim.job_id]
    assert env.repo.event_history(session["id"], env.uid)[-1]["type"] == "session.failed"


def test_cancel_grace_is_committed_at_real_checkpoint(env):
    from app.execution.repository import CANCEL_GRACE_SECONDS
    session = submit(env)
    claim = env.repo.claim(session["active_run_id"], "worker")
    env.repo.start(claim)
    env.repo.cancel(session["id"], env.uid)
    env.clock[0] += CANCEL_GRACE_SECONDS
    with pytest.raises(LeaseLost):
        env.repo.checkpoint(claim)
    current = read(env, SESSIONS_COLLECTION, session["id"])
    assert current["status"] == "cancelled" and current["recovery_required"]
    assert read(env, SLOTS_COLLECTION, env.uid)["runs"] == [claim.job_id]


def test_oversized_output_save_rolls_back_before_recovery_finalization(env):
    from app.execution.errors import OutputLimitExceeded
    from app.execution.limits import MAX_SESSION_BYTES
    session = submit(env)
    claim = env.repo.claim(session["active_run_id"], "worker")
    _, current = env.repo.start(claim)
    before = read(env, SESSIONS_COLLECTION, session["id"])
    current["messages"].append({"role": "assistant", "content": "x" * MAX_SESSION_BYTES})
    with pytest.raises(OutputLimitExceeded):
        env.repo.save(claim, current)
    assert read(env, SESSIONS_COLLECTION, session["id"]) == before
    env.repo.finish(claim, failed=True, recovery={"reason": "output_limit_exceeded"})
    assert read(env, SESSIONS_COLLECTION, session["id"])["recovery_required"]


@pytest.mark.parametrize("cancelled", [False, True])
def test_uncertain_tool_failure_retains_real_capacity_and_cleanup(env, monkeypatch, cancelled):
    from dataclasses import replace
    import app.agent.tools as tools
    from test_durable_runs import process_claim
    session = submit(env)
    claim = env.repo.claim(session["active_run_id"], "worker")
    effects = []

    def effect(*args):
        effects.append(True)
        if cancelled:
            env.repo.cancel(session["id"], env.uid)
        raise TimeoutError("synthetic-private-provider-error")

    monkeypatch.setattr(tools, "TOOL_SPECS", [replace(spec, handler=effect) if spec.name == "write_file" else spec
                                            for spec in tools.TOOL_SPECS])

    def handler(job, current):
        from app.execution.context import prepare_actions
        call = {"id": "effect", "name": "write_file", "input": {"path": "synthetic", "content": "x"}}
        batch = prepare_actions(current, 0, [call])
        tools.dispatch_tool_call(current, object(), call["name"], call["input"], batch["action_ids"][0])

    process_claim(env.repo, claim, handler)
    current = read(env, SESSIONS_COLLECTION, session["id"])
    assert effects == [True]
    assert current["status"] == ("cancelled" if cancelled else "error") and current["recovery_required"]
    assert current["recovery"] == {"reason": "tool_outcome_uncertain", "tool": "write_file"}
    assert read(env, SLOTS_COLLECTION, env.uid)["runs"] == [claim.job_id]
    assert read(env, CLEANUP_COLLECTION, session["id"])["state"] == "queued"


def test_synthetic_worker_crash_after_effect_is_not_automatically_replayed(env):
    from test_durable_runs import process_claim
    session = submit(env)
    claim = env.repo.claim(session["active_run_id"], "worker")
    effects = []

    def crash(job, current):
        effects.append(True)
        raise SystemExit("synthetic worker exit after dispatch")

    with pytest.raises(SystemExit):
        process_claim(env.repo, claim, crash)
    env.clock[0] += LEASE_SECONDS + 1
    assert env.repo.claim(claim.job_id, "recovery") is None
    assert effects == [True]
    assert read(env, SESSIONS_COLLECTION, session["id"])["recovery_required"]
    assert read(env, SLOTS_COLLECTION, env.uid)["runs"] == [claim.job_id]


def test_spawned_worker_roundtrip_commits_real_repository_state(env, tmp_path):
    import json
    from app.worker import process_claim
    from spawn_stubs import stub
    marker = tmp_path / "spawn.pid"
    session = env.repo.submit(env.uid, "spawn-success", "create", {
        "task": json.dumps({"mode": "updates", "marker": str(marker)}), "kind": "code"})
    claim = env.repo.claim(session["active_run_id"], "spawn-worker")
    process_claim(env.repo, claim, stub)
    current = read(env, SESSIONS_COLLECTION, session["id"])
    assert marker.exists() and current["status"] == "idle"
    assert current["plan"][0]["status"] == "done" and current["revision"] == 4
    assert not read(env, SLOTS_COLLECTION, env.uid)["runs"]


def test_spawned_cancelled_hang_keeps_real_uncertain_capacity(env, tmp_path):
    import json
    from app.worker import process_claim
    from spawn_stubs import stub
    from test_process_supervision import wait_for
    marker = tmp_path / "cancel.pid"
    session = env.repo.submit(env.uid, "spawn-cancel", "create", {
        "task": json.dumps({"mode": "hang", "marker": str(marker)}), "kind": "code"})
    claim = env.repo.claim(session["active_run_id"], "spawn-worker")
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(process_claim, env.repo, claim, stub)
        wait_for(marker.exists)
        env.repo.cancel(session["id"], env.uid)
        future.result(timeout=15)
    current = read(env, SESSIONS_COLLECTION, session["id"])
    assert current["status"] == "cancelled" and current["recovery_required"]
    assert read(env, SLOTS_COLLECTION, env.uid)["runs"] == [claim.job_id]


@pytest.mark.skipif(os.name != "nt", reason="Windows owned Job Object kill/restart integration")
def test_actual_worker_parent_kill_recovers_real_state_without_replay_or_refund(env, tmp_path):
    import ctypes
    import json
    import multiprocessing
    from ctypes import wintypes as w
    from spawn_stubs import emulator_owner_parent
    from test_process_supervision import wait_for

    marker = tmp_path / "killed-worker.pid"
    session = env.repo.submit(env.uid, "killed-parent", "create", {
        "task": json.dumps({"mode": "hang", "marker": str(marker)}), "kind": "code"})
    reservation = env.ledger.reserve(env.uid, ENTRY, LIMITS, 100)
    context = multiprocessing.get_context("spawn")
    receive, send = context.Pipe(duplex=False)
    owner = context.Process(target=emulator_owner_parent,
        args=(send, session["active_run_id"], env.clock[0]))
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.OpenProcess.argtypes, api.OpenProcess.restype = [w.DWORD, w.BOOL, w.DWORD], w.HANDLE
    api.WaitForSingleObject.argtypes, api.WaitForSingleObject.restype = [w.HANDLE, w.DWORD], w.DWORD
    api.TerminateProcess.argtypes, api.TerminateProcess.restype = [w.HANDLE, w.UINT], w.BOOL
    api.CloseHandle.argtypes = [w.HANDLE]
    child_handle = None
    try:
        owner.start()
        send.close()
        assert receive.poll(15)
        child_pid = receive.recv()  # Trusted fixture channel, never runtime IPC.
        wait_for(marker.exists)
        assert int(marker.read_text()) == child_pid
        child_handle = api.OpenProcess(0x100000 | 1, False, child_pid)
        assert child_handle
        owner.kill()
        owner.join(5)
        assert not owner.is_alive() and api.WaitForSingleObject(child_handle, 5000) == 0
    finally:
        if owner.pid and owner.is_alive():
            owner.kill()
            owner.join(5)
        if child_handle:
            if api.WaitForSingleObject(child_handle, 0) != 0:
                api.TerminateProcess(child_handle, 73)
                api.WaitForSingleObject(child_handle, 5000)
            api.CloseHandle(child_handle)
        owner.close()
        receive.close()
    env.clock[0] += LEASE_SECONDS + 1
    assert env.repo.claim(session["active_run_id"], "restart-worker") is None
    current = read(env, SESSIONS_COLLECTION, session["id"])
    assert current["status"] == "error" and current["recovery_required"]
    assert read(env, SLOTS_COLLECTION, env.uid)["runs"] == [session["active_run_id"]]
    assert read(env, RESERVATIONS_COLLECTION, reservation)["state"] == "reserved"


def prepare_action(env):
    session = submit(env)
    claim = env.repo.claim(session["active_run_id"], "receipt-worker")
    env.repo.start(claim)
    call = {"id": "one", "name": "write_file", "input": {"path": "synthetic", "content": "synthetic-private-input"}}
    batch = env.repo.prepare_actions(claim, step=0, calls=[call])
    return session, claim, call, batch


def test_action_seal_and_permit_races_commit_only_one_permission(env):
    from app.execution.repository import _digest
    session, claim, call, batch = prepare_action(env)
    aid = batch["action_ids"][0]
    seals = race(lambda: env.repo.prepare_actions(claim, step=0, calls=[call]),
                 lambda: env.repo.prepare_actions(claim, step=0, calls=[call]))
    assert seals == [batch, batch]
    def permit():
        try:
            env.repo.permit_action(claim, action_id=aid, name=call["name"], input=call["input"])
            return True
        except LeaseLost:
            return False
    assert sum(race(permit, permit)) == 1
    assert read(env, ACTIONS_COLLECTION, aid)["state"] == "outcome_unknown"
    def result():
        env.repo.record_action_result(claim, action_id=aid, result_digest=_digest("returned"))
    race(result, result)
    assert read(env, ACTIONS_COLLECTION, aid)["state"] == "handler_returned"
    with pytest.raises(LeaseLost):
        env.repo.record_action_result(claim, action_id=aid, result_digest=_digest("conflict"))
    assert not permit()
    assert len(owned(env.db, ACTIONS_COLLECTION, env.uid)) == 1


@pytest.mark.parametrize("operation", ["prepare", "permit", "result", "evidence"])
def test_action_transaction_commit_abort_leaves_no_partial_receipt(env, monkeypatch, operation):
    from google.cloud.firestore_v1 import transactional
    from app.execution.repository import _digest
    session, claim, call, batch = prepare_action(env)
    aid = batch["action_ids"][0]
    if operation == "result":
        env.repo.permit_action(claim, action_id=aid, name=call["name"], input=call["input"])
    before_action = read(env, ACTIONS_COLLECTION, aid)
    before_job = read(env, JOBS_COLLECTION, claim.job_id)
    before_session = read(env, SESSIONS_COLLECTION, session["id"])
    def aborted(callback):
        def run(tx):
            callback(tx)
            raise RuntimeError("synthetic precommit abort")
        return transactional(run)(env.db.transaction())
    monkeypatch.setattr(env.repo, "transaction_runner", aborted)
    with pytest.raises(RuntimeError, match="precommit abort"):
        if operation == "prepare":
            env.repo.prepare_actions(claim, step=1, calls=[call])
        elif operation == "permit":
            env.repo.permit_action(claim, action_id=aid, name=call["name"], input=call["input"])
        elif operation == "result":
            env.repo.record_action_result(claim, action_id=aid, result_digest=_digest("returned"))
        else:
            env.repo.record_action_evidence(session["id"], env.uid, aid, "e", {
                "expected_version": 1, "assertion": "inconclusive", "evidence_sha256": "a" * 64})
    assert read(env, ACTIONS_COLLECTION, aid) == before_action
    assert read(env, JOBS_COLLECTION, claim.job_id) == before_job
    assert read(env, SESSIONS_COLLECTION, session["id"]) == before_session
    assert len(owned(env.db, ACTIONS_COLLECTION, env.uid)) == 1
    assert not owned(env.db, EVIDENCE_COLLECTION, env.uid)


def test_action_evidence_conflict_owner_scope_and_unknown_capacity(env):
    session, claim, call, batch = prepare_action(env)
    aid = batch["action_ids"][0]
    env.repo.permit_action(claim, action_id=aid, name=call["name"], input=call["input"])
    env.repo.finish(claim, failed=True)
    payload = {"expected_version": 2, "assertion": "effect_observed", "evidence_sha256": "b" * 64}
    def record(key, assertion):
        try:
            return env.repo.record_action_evidence(session["id"], env.uid, aid, key, {**payload, "assertion": assertion})
        except CommandError as error:
            assert error.status == 409
            return None
    records = race(lambda: record("a", "effect_observed"), lambda: record("b", "no_effect_observed"))
    assert sum(item is not None for item in records) == 1
    accepted = next(item for item in records if item)
    key = "a" if accepted["assertion"] == "effect_observed" else "b"
    replay_payload = {**payload, "assertion": accepted["assertion"]}
    assert env.repo.record_action_evidence(session["id"], env.uid, aid, key, replay_payload) == accepted
    with pytest.raises(CommandError) as denied:
        env.repo.record_action_evidence(session["id"], "other-tenant", aid, key, replay_payload)
    assert denied.value.status == 404
    inspected = env.repo.inspect_action(session["id"], env.uid, aid)
    assert inspected["action"]["state"] == "outcome_unknown"
    assert len(inspected["evidence"]) == 1 and "synthetic-private" not in str(inspected)
    assert read(env, SLOTS_COLLECTION, env.uid)["runs"] == [claim.job_id]
    assert read(env, SESSIONS_COLLECTION, session["id"])["recovery_required"]


@pytest.mark.parametrize("mode,expected", [("prepared_crash", "prepared"), ("permit_crash", "outcome_unknown"),
                                           ("effect_crash", "outcome_unknown"), ("returned_crash", "handler_returned")])
def test_real_spawned_action_crash_uses_emulator_receipts_without_replay(env, tmp_path, mode, expected):
    import json
    from app.worker import process_claim
    from spawn_stubs import action_stub
    marker = tmp_path / "effect"
    session = env.repo.submit(env.uid, "action-crash", "create", {
        "task": json.dumps({"mode": mode, "marker": str(marker)}), "kind": "code"})
    reservation = env.ledger.reserve(env.uid, ENTRY, LIMITS, 100)
    claim = env.repo.claim(session["active_run_id"], "spawn-worker")
    process_claim(env.repo, claim, action_stub)
    actions = env.repo.action_history(session["id"], env.uid)["actions"]
    assert len(actions) == 1 and actions[0]["state"] == expected
    if mode in {"effect_crash", "returned_crash"}:
        assert marker.read_text() == "effect\n"
    else:
        assert not marker.exists()
    assert read(env, SESSIONS_COLLECTION, session["id"])["recovery_required"]
    assert read(env, SLOTS_COLLECTION, env.uid)["runs"] == [claim.job_id]
    assert read(env, RESERVATIONS_COLLECTION, reservation)["state"] == "reserved"
    restarted = RunRepository(env.db, clock=lambda: env.clock[0])
    assert restarted.claim(claim.job_id, "restart") is None
    with pytest.raises(LeaseLost):
        restarted.permit_action(claim, action_id=actions[0]["id"], name="write_file", input={"path": "synthetic", "content": "x"})


def test_approval_batch_binding_and_open_permit_gate_are_transactional(env):
    from app.execution.repository import _digest
    session = submit(env)
    old = env.repo.claim(session["active_run_id"], "first")
    _, state = env.repo.start(old)
    call = {"id": "risk", "name": "bash", "input": {"command": "sudo original"}, "risky": True}
    seal_pending(env.repo, old, state, {"id": "approval", "calls": [call]})
    env.repo.save(old, state)
    queued = env.repo.submit(env.uid, "approve", "approve", {"approval_id": "approval",
                             "decisions": [{"call_id": "risk", "approved": True}]}, state["id"])
    claim = env.repo.claim(queued["active_run_id"], "second")
    _, current = env.repo.start(claim)
    aid = call["action_id"]
    env.repo.permit_action(claim, action_id=aid, name=call["name"], input=call["input"])
    env.repo.record_action_result(claim, action_id=aid, result_digest=_digest("returned"))
    current.update(status="running", pending_approval=None)
    env.repo.save(claim, current)
    changed = {"id": "risk", "name": "bash", "input": {"command": "sudo changed"}}
    batch = env.repo.prepare_actions(claim, step=1, calls=[changed])
    with pytest.raises(LeaseLost, match="sealed approval"):
        env.repo.permit_action(claim, action_id=batch["action_ids"][0], name=changed["name"], input=changed["input"])
    assert read(env, ACTIONS_COLLECTION, batch["action_ids"][0])["state"] == "prepared"
    ordinary = {"id": "one", "name": "write_file", "input": {"path": "synthetic", "content": "x"}}
    batch = env.repo.prepare_actions(claim, step=2, calls=[ordinary, {**ordinary, "id": "two"}])
    first, second = batch["action_ids"]
    env.repo.permit_action(claim, action_id=first, name=ordinary["name"], input=ordinary["input"])
    with pytest.raises(LeaseLost, match="reconciliation"):
        env.repo.permit_action(claim, action_id=second, name=ordinary["name"], input=ordinary["input"])
    env.repo.record_action_result(claim, action_id=first, result_digest=_digest("returned"))
    env.repo.permit_action(claim, action_id=second, name=ordinary["name"], input=ordinary["input"])
    assert read(env, JOBS_COLLECTION, claim.job_id)["open_actions"] == [second]


def workspace_ready(env, tmp_path, *, finish=True):
    from app.sandbox.checkpoints import LocalHarness, WorkspacePersistence
    local = LocalHarness(tmp_path / "workspace-local")
    service = WorkspacePersistence(env.repo, local, local)
    source = local.fixture({"src/a.txt": b"uncommitted fixture work", ".env": b"excluded"})
    session = submit(env)
    claim = env.repo.claim(session["active_run_id"], "workspace-source")
    env.repo.start(claim)
    receipt = service.snapshot(claim, key="snapshot", source=source, expires_at=env.clock[0] + 100)
    if finish:
        current = read(env, SESSIONS_COLLECTION, session["id"])
        current["status"] = "idle"
        env.repo.save(claim, current)
    return service, local, source, session, claim, receipt


def workspace_target(env):
    session = submit(env, "workspace-target")
    claim = env.repo.claim(session["active_run_id"], "workspace-target")
    env.repo.start(claim)
    return claim


def test_workspace_publication_roundtrip_is_transactional_and_keeps_slots(env, tmp_path):
    service, local, source, session, claim, receipt = workspace_ready(env, tmp_path)
    target = workspace_target(env)
    slots = read(env, SLOTS_COLLECTION, env.uid)
    materialized = service.restore(target, checkpoint_id=receipt["id"], key="restore")
    assert materialized["state"] == "materialized"
    assert read(env, RESTORES_COLLECTION, materialized["id"])["state"] == "materialized"
    assert (local.candidates / materialized["id"] / "src/a.txt").read_bytes() == b"uncommitted fixture work"
    assert service.restore(target, checkpoint_id=receipt["id"], key="restore") == materialized
    assert read(env, SLOTS_COLLECTION, env.uid) == slots
    with pytest.raises(LeaseLost):
        env.repo.finish(target)


@pytest.mark.parametrize("fault", ["active", "expired", "newer", "foreign"])
def test_workspace_owner_generation_activity_retention_gates(env, tmp_path, fault):
    service, local, source, session, claim, receipt = workspace_ready(env, tmp_path, finish=fault != "active")
    target = workspace_target(env)
    checkpoint_ref = env.db.collection(CHECKPOINTS_COLLECTION).document(receipt["id"])
    if fault == "expired":
        started = env.clock[0]
        env.clock[0] = started + 44
        env.repo.heartbeat(target)
        env.clock[0] = started + 88
        env.repo.heartbeat(target)
        env.clock[0] = receipt["expires_at"]
    if fault == "newer": env.db.collection(SESSIONS_COLLECTION).document(session["id"]).update({"workspace_generation": 1})
    if fault == "foreign": checkpoint_ref.update({"uid": "foreign-fixture-owner"})
    try:
        with pytest.raises(LeaseLost):
            service.restore(target, checkpoint_id=receipt["id"], key="restore")
        assert not list(local.candidates.iterdir())
    finally:
        if fault == "foreign": checkpoint_ref.update({"uid": env.uid})


def test_workspace_partial_upload_and_stale_fence_never_publish(env, tmp_path, monkeypatch):
    from app.sandbox.checkpoints import LocalHarness, WorkspacePersistence
    local = LocalHarness(tmp_path / "workspace-local")
    service = WorkspacePersistence(env.repo, local, local)
    source = local.fixture({"a": b"work"})
    session = submit(env)
    claim = env.repo.claim(session["active_run_id"], "source")
    env.repo.start(claim)
    put = local.put
    def lose(key, data):
        put(key, data)
        env.clock[0] += LEASE_SECONDS
    monkeypatch.setattr(local, "put", lose)
    with pytest.raises(LeaseLost):
        service.snapshot(claim, key="snapshot", source=source, expires_at=env.clock[0] + 100)
    items = [snap.to_dict() for snap in owned(env.db, CHECKPOINTS_COLLECTION, env.uid)]
    assert len(items) == 1 and items[0]["state"] == "staging"
    assert read(env, SLOTS_COLLECTION, env.uid)["runs"] == [claim.job_id]


def test_workspace_cancel_after_materialization_keeps_quarantine(env, tmp_path, monkeypatch):
    service, local, source, session, claim, receipt = workspace_ready(env, tmp_path)
    target = workspace_target(env)
    materialize = local.materialize
    def cancel(restore_id, files, checkpoint):
        materialize(restore_id, files, checkpoint)
        env.repo.cancel(target.session_id, env.uid)
    monkeypatch.setattr(local, "materialize", cancel)
    with pytest.raises(ExecutionStopped):
        service.restore(target, checkpoint_id=receipt["id"], key="restore")
    records = [snap.to_dict() for snap in owned(env.db, RESTORES_COLLECTION, env.uid)]
    assert len(records) == 1 and records[0]["state"] == "staging"
    assert target.job_id in read(env, SLOTS_COLLECTION, env.uid)["runs"]


def test_workspace_competing_checkpoint_transactions_admit_one(env):
    session = submit(env)
    claim = env.repo.claim(session["active_run_id"], "source")
    env.repo.start(claim)
    def attempt(key):
        try:
            return env.repo.begin_workspace_checkpoint(claim, key=key, source_digest="a" * 64,
                                                        expires_at=env.clock[0] + 100)
        except LeaseLost:
            return None
    results = race(lambda: attempt("one"), lambda: attempt("two"))
    assert sum(result is not None for result in results) == 1
    assert len(owned(env.db, CHECKPOINTS_COLLECTION, env.uid)) == 1


@pytest.fixture
def broker_enabled(monkeypatch):
    import app.agent.tools as tools
    spec = next(t for t in tools.TOOL_SPECS if t.name == "create_pull_request")
    monkeypatch.setattr(spec, "enabled", True)


def github_harness(env):
    from test_github_broker import approved, Authority, Clients
    from app.github.broker import GitHubWriteBroker
    claim, session, aid = approved(env.repo, env.uid)
    authority, clients = Authority(session, env.clock[0]), Clients()
    broker = GitHubWriteBroker(env.repo, authority=authority, clients=clients,
                               clock=lambda: env.clock[0])
    return SimpleNamespace(claim=claim, session=session, aid=aid, authority=authority, clients=clients, broker=broker)


def github_execute(h):
    from test_github_broker import INPUT
    return h.broker.execute(h.claim, action_id=h.aid, operation="create_pull_request", input=INPUT)


def test_github_broker_concurrent_dispatch_one_native_boundary(env, broker_enabled):
    h = github_harness(env)
    def execute():
        try:
            return github_execute(h)
        except ExecutionStopped:
            return None
    results = race(execute, execute)
    assert sum(v is not None for v in results) == 1
    assert h.clients.posts == 1
    assert read(env, ACTIONS_COLLECTION, h.aid)["state"] == "handler_returned"
    assert not execute() and h.clients.posts == 1


@pytest.mark.parametrize("case", ["plan", "model_revoked", "approval_changed"])
def test_github_broker_current_firestore_authority_before_credentials(env, broker_enabled, case):
    h = github_harness(env)
    if case == "plan":
        env.db.collection(SESSIONS_COLLECTION).document(h.session["id"]).update({"mode": "plan"})
    elif case == "model_revoked":
        env.db.collection("plans").document("fixture-approval-" + env.uid).update({"models": []})
    else:
        env.db.collection(JOBS_COLLECTION).document(h.claim.job_id).update({"approval_batch_id": "changed"})
    with pytest.raises(ExecutionStopped):
        github_execute(h)
    assert h.clients.opens == h.clients.posts == 0
    assert read(env, ACTIONS_COLLECTION, h.aid)["state"] == "prepared"


@pytest.mark.parametrize("lost", ["provider_response", "receipt_response"])
def test_github_broker_lost_replies_never_retry_provider(env, broker_enabled, monkeypatch, lost):
    from app.execution.errors import ToolOutcomeUncertain
    h = github_harness(env)
    if lost == "provider_response":
        h.clients.before_post = lambda: (_ for _ in ()).throw(TimeoutError("synthetic-private-error"))
    else:
        original = env.repo.record_action_result
        def lose(*args, **kwargs):
            original(*args, **kwargs)
            raise TimeoutError("synthetic-private-error")
        monkeypatch.setattr(env.repo, "record_action_result", lose)
    with pytest.raises(ToolOutcomeUncertain) as error:
        github_execute(h)
    assert "private" not in str(error.value)
    assert h.clients.posts == 1
    assert read(env, ACTIONS_COLLECTION, h.aid)["state"] == (
        "outcome_unknown" if lost == "provider_response" else "handler_returned")
    with pytest.raises(ExecutionStopped):
        github_execute(h)
    assert h.clients.posts == 1


# Account/project/transcript slice: every fixture remains synthetic and local.
def product_plan(env, maximum=3, web=True):
    selection_plan(env)
    ref = env.db.collection("plans").document("fixture-selection-" + env.uid)
    data = ref.get().to_dict()
    data["entitlements"] = [
        {"capabilityKey": "platform.web", "value": {"kind": "boolean", "value": web}},
        {"capabilityKey": "projects.max", "value": {"kind": "number", "value": maximum}},
        {"capabilityKey": "ai.run", "value": {"kind": "boolean", "value": True}},
        {"capabilityKey": "ai.model.allowlist", "value": {"kind": "stringList", "value": []}}]
    ref.set(data)


def product_project(env, key="project"):
    return env.repo.create_project(env.uid, key, {"name": "synthetic project"})


def product_session(env, project, key="run"):
    return env.repo.submit(env.uid, key, "create", {"task": "original text", "kind": "code",
        "project_id": project["id"], "model": "fixture-selected"})


def product_append(env, claim, current, text):
    message = {"role": "assistant", "content": [{"type": "text", "text": text}]}
    current["messages"].append(message)
    current["_transcript_append"] = [message]
    env.repo.save(claim, current)


def test_product_project_repeat_and_limit_races_are_atomic(env):
    product_plan(env, maximum=1)
    results = race(*[lambda: product_project(env) for _ in range(3)])
    assert all(result == results[0] for result in results)
    assert read(env, ACCOUNTS_COLLECTION, env.uid)["projectCount"] == 1
    assert len(owned(env.db, PROJECTS_COLLECTION, env.uid)) == 1
    assert len(owned(env.db, RECEIPTS_COLLECTION, env.uid)) == 1
    def other(key):
        try:
            return product_project(env, key)
        except CommandError as error:
            return error.status
    assert race(lambda: other("two"), lambda: other("three")) == [403, 403]
    assert len(owned(env.db, RECEIPTS_COLLECTION, env.uid)) == 1


def test_product_session_competing_keys_and_owner_guard(env):
    product_plan(env)
    project = product_project(env)
    def create(key):
        try:
            return product_session(env, project, key)
        except CommandError as error:
            return error.status
    results = race(lambda: create("one"), lambda: create("two"))
    accepted = [value for value in results if isinstance(value, dict)]
    assert len(accepted) == 1 and results.count(409) == 1
    session = accepted[0]
    assert read(env, PROJECTS_COLLECTION, project["id"])["active_session_id"] == session["id"]
    assert len(owned(env.db, JOBS_COLLECTION, env.uid)) == 1
    assert len(owned(env.db, MESSAGES_COLLECTION, env.uid)) == 1
    with pytest.raises(CommandError) as error:
        env.repo.submit(env.uid + "-foreign", "foreign", "create", {"task": "x", "project_id": project["id"]})
    assert error.value.status == 404
    assert len(owned(env.db, JOBS_COLLECTION, env.uid)) == 1


def test_product_project_patch_race_preserves_binding_and_unrelated_fields(env):
    product_plan(env)
    project = product_project(env)
    session = product_session(env, project)
    race(lambda: env.repo.update_project(project["id"], env.uid, {"name": "new name"}),
         lambda: env.repo.update_project(project["id"], env.uid, {"description": "new description"}))
    current = read(env, PROJECTS_COLLECTION, project["id"])
    assert current["name"] == "new name" and current["description"] == "new description"
    assert current["active_session_id"] == session["id"]
    with pytest.raises(CommandError) as error:
        env.repo.update_project(project["id"], env.uid + "-foreign", {"name": "forged"})
    assert error.value.status == 404


def test_product_transcript_collision_rolls_back_session_and_events(env):
    from google.api_core.exceptions import AlreadyExists
    session = submit(env)
    claim = env.repo.claim(session["active_run_id"], "worker")
    _, current = env.repo.start(claim)
    collision = env.db.collection(MESSAGES_COLLECTION).document(session["id"] + ":1")
    collision.set({"uid": env.uid, "immutable": True})
    before = read(env, SESSIONS_COLLECTION, session["id"])
    events_before = len(owned(env.db, EVENTS_COLLECTION, env.uid))
    with pytest.raises(AlreadyExists):
        product_append(env, claim, current, "new")
    assert read(env, SESSIONS_COLLECTION, session["id"]) == before
    assert len(owned(env.db, EVENTS_COLLECTION, env.uid)) == events_before
    assert collision.get().to_dict() == {"uid": env.uid, "immutable": True}
    collision.delete()
    env.repo.save(claim, current)
    assert env.repo.message_history(session["id"], env.uid)["messages"][-1]["content"] == "new"


def test_product_transcript_compaction_does_not_change_original_history(env, monkeypatch):
    import app.agent.compaction as compaction
    session = submit(env)
    claim = env.repo.claim(session["active_run_id"], "worker")
    _, current = env.repo.start(claim)
    for n in range(12):
        product_append(env, claim, current, f"original-{n}")
    before = env.repo.message_history(session["id"], env.uid)["messages"]
    monkeypatch.setattr(compaction, "_summarize", lambda *args: "synthetic summary")
    compaction.compact(current, ENTRY)
    env.repo.save(claim, current)
    product_append(env, claim, current, "after compaction")
    rows = env.repo.message_history(session["id"], env.uid)["messages"]
    assert rows[:-1] == before and rows[-1]["content"] == "after compaction"
    assert "summary" not in str(rows)


def test_product_real_sdk_history_pagination_and_tenant_scope(env):
    product_plan(env, maximum=5)
    projects = [product_project(env, str(n)) for n in range(4)]
    first = env.repo.list_projects(env.uid, limit=2)
    assert [p["id"] for p in first["projects"]] == [p["id"] for p in projects[-2:][::-1]]
    assert [p["id"] for p in env.repo.list_projects(env.uid, first["nextCursor"], 2)["projects"]] == [p["id"] for p in projects[:2][::-1]]
    session = product_session(env, projects[0])
    claim = env.repo.claim(session["active_run_id"], "worker")
    _, current = env.repo.start(claim)
    for n in range(4):
        product_append(env, claim, current, str(n))
    page = env.repo.message_history(session["id"], env.uid, limit=2)
    assert [m["seq"] for m in page["messages"]] == [3, 4]
    page2 = env.repo.message_history(session["id"], env.uid, page["nextCursor"], 2)
    assert [m["seq"] for m in page2["messages"]] == [1, 2]
    with pytest.raises(CommandError) as error:
        env.repo.message_history(session["id"], env.uid + "-foreign", "bad", 0)
    assert error.value.status == 404


def test_product_history_metadata_and_project_cannot_be_rewritten_by_worker(env):
    import copy
    product_plan(env)
    project = product_project(env)
    session = product_session(env, project)
    claim = env.repo.claim(session["active_run_id"], "worker")
    _, current = env.repo.start(claim)
    before = read(env, SESSIONS_COLLECTION, session["id"])
    for field, value in (("project_id", "foreign"), ("message_count", 0), ("transcript_version", 0)):
        candidate = copy.deepcopy(current)
        candidate[field] = value
        with pytest.raises(LeaseLost):
            env.repo.save(claim, candidate)
        assert read(env, SESSIONS_COLLECTION, session["id"]) == before


def test_product_cancel_repeat_race_has_one_receipt_and_native_state(env):
    session = submit(env)
    claim = env.repo.claim(session["active_run_id"], "worker")
    env.repo.start(claim)
    results = race(*[lambda: env.repo.cancel(session["id"], env.uid, "same") for _ in range(3)])
    assert all(value == results[0] for value in results)
    assert results[0]["status"] == "cancel_requested"
    env.repo.finish(claim)
    assert env.repo.cancel(session["id"], env.uid, "same") == results[0]
    assert read(env, SESSIONS_COLLECTION, session["id"])["status"] == "cancelled"
    with pytest.raises(CommandError) as error:
        env.repo.cancel(session["id"], env.uid + "-foreign", "same")
    assert error.value.status == 404
    assert len(owned(env.db, RECEIPTS_COLLECTION, env.uid)) == 2


def test_product_plan_revocation_stops_start_but_allows_failure_cleanup(env):
    product_plan(env)
    project = product_project(env)
    session = product_session(env, project)
    claim = env.repo.claim(session["active_run_id"], "worker")
    product_plan(env, web=False)
    with pytest.raises(CommandError) as error:
        env.repo.start(claim)
    assert error.value.status == 403
    env.repo.finish(claim, failed=True)
    current = read(env, SESSIONS_COLLECTION, session["id"])
    assert current["status"] == "error" and current["recovery_required"] is True
    assert read(env, PROJECTS_COLLECTION, project["id"])["active_session_id"] == session["id"]


def test_product_ai_deny_and_checkpoint_revocation_remain_fenced(env):
    product_plan(env)
    project = product_project(env)
    ref = env.db.collection("plans").document("fixture-selection-" + env.uid)
    plan = ref.get().to_dict()
    plan["entitlements"][2]["value"]["value"] = False
    ref.set(plan)
    with pytest.raises(CommandError) as error:
        product_session(env, project)
    assert error.value.status == 403 and error.value.code == "session/ai-not-permitted"
    assert not owned(env.db, JOBS_COLLECTION, env.uid)
    product_plan(env)
    session = product_session(env, project)
    claim = env.repo.claim(session["active_run_id"], "worker")
    env.repo.start(claim)
    ref.set(plan)
    with pytest.raises(CommandError) as error:
        env.repo.checkpoint(claim)
    assert error.value.status == 403
    env.repo.finish(claim, failed=True)
    assert read(env, SESSIONS_COLLECTION, session["id"])["recovery_required"] is True


@pytest.fixture
def queue_env(env):
    product_plan(env)
    ref = env.db.collection("plans").document("fixture-selection-" + env.uid)
    plan = ref.get().to_dict()
    key = "fixture-builds-" + env.uid
    plan["meterLimits"] = [{"meterKey": key, "includedAllowance": 8, "hardLimit": 8, "overageEnabled": False}]
    ref.set(plan)
    registry = env.db.collection("config").document("registry")
    previous = registry.get().to_dict()
    assert previous is None, "refuse to replace a registry not created by this fixture"
    registry.set({"fixture_uid": env.uid, "buildMeterKey": key, "meters": [{"key": key, "kind": "COUNTER", "resetPeriod": "DAILY"}]})
    try:
        yield env
    finally:
        current = registry.get().to_dict()
        assert current is not None and current.get("fixture_uid") == env.uid
        registry.delete()  # Exact synthetic fixture created above, emulator only.


def queue_identity(env):
    from app.auth import QueueIdentity
    return QueueIdentity(env.uid, env.clock[0] - 100, env.clock[0] + 600, env.clock[0] + 1)


def queue_submit(env, session, key="one", prompt="synthetic queued next turn", expiry=None):
    return env.repo.enqueue_message(session["id"], env.uid, key, prompt,
        env.clock[0] + 300 if expiry is None else expiry, queue_identity(env))


def queue_quiesce(env, session):
    claim = env.repo.claim(session["active_run_id"], "worker")
    _, current = env.repo.start(claim)
    current["status"] = "idle"
    env.repo.save(claim, current)
    env.repo.finish(claim)
    env.repo.confirm_quiescent(claim)
    return claim


def queue_dispatch(env, session, key="dispatch"):
    return env.repo.dispatch_next(session["id"], env.uid, key, queue_identity(env), owner_request=True)


def test_native_queue_repeat_race_is_one_intent_with_one_order(queue_env):
    env = queue_env
    project = product_project(env)
    session = product_session(env, project)
    results = race(*[lambda: queue_submit(env, session) for _ in range(3)])
    assert all(value == results[0] for value in results)
    assert len(owned(env.db, QUEUE_COLLECTION, env.uid)) == 1
    assert read(env, SESSIONS_COLLECTION, session["id"])["queue_sequence"] == 1
    assert read(env, SESSIONS_COLLECTION, session["id"])["revision"] == session["revision"]


def test_native_queue_competing_dispatch_is_one_job_history_and_charge(queue_env):
    env = queue_env
    session = product_session(env, product_project(env))
    queue_submit(env, session)
    queue_quiesce(env, session)
    results = race(*[lambda: queue_dispatch(env, session) for _ in range(3)])
    assert all(value == results[0] for value in results)
    assert len(owned(env.db, JOBS_COLLECTION, env.uid)) == 2
    assert len(owned(env.db, MESSAGES_COLLECTION, env.uid)) == 2
    assert [s.to_dict()["consumed"] for s in owned(env.db, BUDGETS_COLLECTION, env.uid)] == [2]
    assert len(owned(env.db, RESERVATIONS_COLLECTION, env.uid)) == 2


@pytest.mark.parametrize("kind", ["item", "session"])
def test_native_queue_cancel_dispatch_race_retains_single_outcome(queue_env, kind):
    env = queue_env
    session = product_session(env, product_project(env))
    item = queue_submit(env, session)["queuedMessage"]
    queue_quiesce(env, session)
    def cancel():
        try:
            return env.repo.cancel_queued(session["id"], env.uid, item["id"]) if kind == "item" else env.repo.cancel(session["id"], env.uid)
        except CommandError as error:
            return error.status
    def dispatch():
        try:
            return queue_dispatch(env, session)
        except CommandError as error:
            return error.status
    race(cancel, dispatch)
    assert len(owned(env.db, JOBS_COLLECTION, env.uid)) <= 2
    assert len(owned(env.db, MESSAGES_COLLECTION, env.uid)) <= 2
    state = env.repo.inspect_queued(session["id"], env.uid, item["id"])["state"]
    assert state in ({"cancelled", "dispatched"} if kind == "item" else {"queued", "dispatched"})
    if kind == "session":
        assert read(env, SESSIONS_COLLECTION, session["id"])["status"] == "cancelled"


def test_native_queue_restart_fifo_cursor_and_cross_tenant_ownership(queue_env):
    env = queue_env
    session = product_session(env, product_project(env))
    first = queue_submit(env, session)["queuedMessage"]
    second = queue_submit(env, session, "two", "second")["queuedMessage"]
    queue_quiesce(env, session)
    env.repo.cancel_queued(session["id"], env.uid, first["id"])
    page = env.repo.list_queued(session["id"], env.uid, first["id"], 1)
    assert page["queuedMessages"][0]["id"] == second["id"]
    with pytest.raises(CommandError) as failure:
        env.repo.list_queued(session["id"], env.uid + "-foreign", "invalid", 0)
    assert failure.value.status == 404
    restarted = RunRepository(env.db, clock=lambda: env.clock[0])
    assert restarted.queue_candidates()[0]["id"] == second["id"]
    result = restarted.dispatch_next(session["id"], env.uid, "restart", queue_identity(env), owner_request=True)
    assert result["queuedMessage"]["id"] == second["id"]


def test_native_queue_approval_expiry_never_renews_original_window(queue_env):
    env = queue_env
    session = product_session(env, product_project(env))
    deadline = env.clock[0] + 20
    queue_submit(env, session, expiry=deadline)
    queue_quiesce(env, session)
    accepted = queue_dispatch(env, session)
    claim = env.repo.claim(accepted["session"]["active_run_id"], "worker")
    _, current = env.repo.start(claim)
    seal_pending(env.repo, claim, current, {"id": "approval", "provider": "anthropic", "model": "fixture-selected", "quota": None,
        "calls": [{"id": "risk", "name": "bash", "input": {"command": "sudo synthetic"}, "risky": True}]})
    env.repo.save(claim, current)
    env.repo.finish(claim)
    env.clock[0] = deadline + 1
    before = read(env, SESSIONS_COLLECTION, session["id"])
    with pytest.raises(CommandError) as failure:
        env.repo.submit(env.uid, "approve", "approve", {"approval_id": current["pending_approval"]["id"],
            "decisions": [{"call_id": "risk", "approved": True}]}, session["id"])
    assert failure.value.status == 409
    assert read(env, SESSIONS_COLLECTION, session["id"]) == before


def test_native_queue_stale_child_preserves_new_intent_and_history_collision_rolls_back(queue_env):
    from google.api_core.exceptions import AlreadyExists
    env = queue_env
    session = product_session(env, product_project(env))
    claim = env.repo.claim(session["active_run_id"], "worker")
    _, current = env.repo.start(claim)
    item = queue_submit(env, session)["queuedMessage"]
    current.update(queue_pending=[], queue_sequence=0, queue_ready=True)
    current["status"] = "idle"
    env.repo.save(claim, current)
    env.repo.finish(claim)
    env.repo.confirm_quiescent(claim)
    assert read(env, SESSIONS_COLLECTION, session["id"])["queue_pending"] == [item["id"]]
    collision = env.db.collection(MESSAGES_COLLECTION).document(session["id"] + ":1")
    collision.set({"uid": env.uid, "immutable": True})
    before = read(env, SESSIONS_COLLECTION, session["id"])
    with pytest.raises(AlreadyExists):
        queue_dispatch(env, session)
    assert read(env, SESSIONS_COLLECTION, session["id"]) == before
    assert [s.to_dict()["consumed"] for s in owned(env.db, BUDGETS_COLLECTION, env.uid)] == [1]
    assert len(owned(env.db, JOBS_COLLECTION, env.uid)) == 1


def test_native_queue_expired_before_start_preserves_workspace_without_cleanup(queue_env):
    from app.worker import process_claim
    env = queue_env
    session = product_session(env, product_project(env))
    deadline = env.clock[0] + 2
    queue_submit(env, session, expiry=deadline)
    queue_quiesce(env, session)
    accepted = queue_dispatch(env, session)
    env.clock[0] = deadline + 1
    claim = env.repo.claim(accepted["session"]["active_run_id"], "worker")
    effects = []
    process_claim(env.repo, claim, lambda *args: effects.append(True))
    assert not effects
    assert read(env, SESSIONS_COLLECTION, session["id"])["recovery"]["reason"] == "queued_run_not_started"
    assert not env.db.collection(CLEANUP_COLLECTION).document(session["id"]).get().exists
    assert read(env, SLOTS_COLLECTION, env.uid)["sandboxes"] == [session["id"]]


def test_native_queue_unverified_completion_and_current_deny_block_dispatch(queue_env):
    env = queue_env
    session = product_session(env, product_project(env))
    queue_submit(env, session)
    claim = env.repo.claim(session["active_run_id"], "worker")
    _, current = env.repo.start(claim)
    current["status"] = "idle"
    env.repo.save(claim, current)
    env.repo.finish(claim)
    with pytest.raises(CommandError) as failure:
        queue_dispatch(env, session)
    assert failure.value.status == 409
    env.repo.confirm_quiescent(claim)
    ref = env.db.collection("plans").document("fixture-selection-" + env.uid)
    plan = ref.get().to_dict()
    plan["entitlements"][2]["value"]["value"] = False
    ref.set(plan)
    with pytest.raises(CommandError) as failure:
        queue_dispatch(env, session)
    assert failure.value.status == 403
    assert len(owned(env.db, JOBS_COLLECTION, env.uid)) == 1
    assert [s.to_dict()["consumed"] for s in owned(env.db, BUDGETS_COLLECTION, env.uid)] == [1]



class EmulatorAccountFacts:
    """Explicit synthetic facts; never invokes Firebase Admin or reads a token."""
    disabled = False
    unavailable = False

    def read(self, uid, *, timeout, ttl, clock):
        if self.unavailable:
            raise TimeoutError("synthetic unavailable account reader")
        now = clock()
        return AccountEvidence(uid, self.disabled, True, 0, now, now + ttl, time.monotonic() + ttl)


@pytest.fixture
def task_env(queue_env):
    env = queue_env
    env.repo.account_reader = EmulatorAccountFacts()
    policy = env.db.collection("config").document("task_authorization")
    assert not policy.get().exists, "refuse to replace a policy not created by this fixture"
    policy.set({"fixture_uid": env.uid, "schema_version": 1, "version": 1,
        "max_duration_seconds": 1800, "max_turns": 3, "max_run_seconds": 60,
        "max_model_requests": 4, "max_reserved_tokens": 100,
        "reader_timeout_seconds": 3, "evidence_ttl_seconds": 30,
        "allowed_modes": ["build", "plan"], "allowed_models": ["fixture-selected"],
        "allowed_tools": ["read_file", "write_file", "update_plan"]})
    try:
        yield env
    finally:
        assert policy.get().to_dict().get("fixture_uid") == env.uid
        policy.delete()  # Exact owned emulator-only synthetic policy.


def task_request(env, **changes):
    return {"mode": "build", "model": "fixture-selected", "provider": "anthropic", "effort": None,
        "operations": sorted(OPERATIONS), "tools": ["read_file", "write_file"],
        "expires_at": env.clock[0] + 1200, "max_turns": 2, "max_run_seconds": 30,
        "max_model_requests": 2, "max_reserved_tokens": 20} | changes


def task_consent(env, session, key="consent", **changes):
    return env.repo.consent_task(session["id"], queue_identity(env), key, task_request(env, **changes))


def task_attach(env, session, grant, key="queued"):
    return env.repo.enqueue_message(session["id"], env.uid, key, "synthetic authorized next turn",
        env.clock[0] + 900, queue_identity(env), task_authorization_id=grant["id"])


def task_dispatch(env, session, key="task-dispatch"):
    candidate = next(row for row in env.repo.queue_candidates() if row["session_id"] == session["id"])
    evidence = TaskQueueAuthorizer(env.repo)(candidate)
    return env.repo.dispatch_next(session["id"], env.uid, key, evidence)


def task_ready(env, **changes):
    session = product_session(env, product_project(env))
    grant = task_consent(env, session, **changes)
    task_attach(env, session, grant)
    queue_quiesce(env, session)
    return session, grant


def task_running(env, **changes):
    session, grant = task_ready(env, **changes)
    result = task_dispatch(env, session)
    claim = env.repo.claim(result["session"]["active_run_id"], "task-worker")
    _, current = env.repo.start(claim)
    return session, grant, claim, current


def test_task_consent_repeat_and_revoke_version_contention(task_env):
    env = task_env
    session = product_session(env, product_project(env))
    results = race(*[lambda: task_consent(env, session) for _ in range(3)])
    grant = results[0]
    assert all(result == grant for result in results)
    assert len(owned(env.db, GRANTS_COLLECTION, env.uid)) == 1
    assert env.repo.inspect_task(session["id"], env.uid, grant["id"])["resource_usage"] == {
        "requests": 0, "reserved_tokens": 0, "actual_tokens": 0}
    def revoke(key):
        try:
            return env.repo.revoke_task(session["id"], env.uid, grant["id"], key, 1)
        except CommandError as error:
            return error.status
    results = race(lambda: revoke("one"), lambda: revoke("two"))
    assert sum(isinstance(value, dict) for value in results) == 1 and results.count(409) == 1
    assert task_consent(env, session) == grant
    assert env.repo.inspect_task(session["id"], env.uid, grant["id"])["status"] == "revoked"
    with pytest.raises(CommandError) as failure:
        env.repo.inspect_task(session["id"], env.uid + "-foreign", grant["id"])
    assert failure.value.status == 404


def test_task_dispatch_revoke_race_never_reauthorizes_or_double_consumes(task_env):
    env = task_env
    session, grant = task_ready(env)
    def dispatch():
        try:
            return task_dispatch(env, session)
        except CommandError as error:
            return error.status
    race(dispatch, lambda: env.repo.revoke_task(session["id"], env.uid, grant["id"], "revoke", 1))
    inspected = env.repo.inspect_task(session["id"], env.uid, grant["id"])
    assert inspected["status"] == "revoked" and inspected["uses"] <= 1
    jobs = owned(env.db, JOBS_COLLECTION, env.uid)
    assert len(jobs) == 1 + inspected["uses"]
    if inspected["uses"]:
        current = read(env, SESSIONS_COLLECTION, session["id"])
        claim = env.repo.claim(current["active_run_id"], "late-worker")
        with pytest.raises(CommandError) as failure:
            env.repo.start(claim)
        assert failure.value.status == 403
        env.repo.finish(claim, failed=True)
        assert not env.db.collection(CLEANUP_COLLECTION).document(session["id"]).get().exists


def test_task_restart_uses_original_consent_without_login_replay(task_env):
    env = task_env
    session, grant = task_ready(env)
    env.clock[0] += 601  # Original request token expiry passed; grant still in scope.
    env.repo = RunRepository(env.db, clock=lambda: env.clock[0], account_reader=EmulatorAccountFacts())
    # Both contenders represent the SAME discovered intent. A known-aborted
    # command retry must not rediscover a head already removed by the winner.
    candidate = next(row for row in env.repo.queue_candidates() if row["session_id"] == session["id"])
    evidence = TaskQueueAuthorizer(env.repo)(candidate)
    results = race(*[lambda: env.repo.dispatch_next(session["id"], env.uid, "task-dispatch", evidence) for _ in range(2)])
    assert results[0] == results[1]
    assert env.repo.inspect_task(session["id"], env.uid, grant["id"])["uses"] == 1
    assert len(owned(env.db, JOBS_COLLECTION, env.uid)) == 2
    assert len(owned(env.db, MESSAGES_COLLECTION, env.uid)) == 2


@pytest.mark.parametrize("stop", ["disabled", "unavailable"])
def test_task_current_account_denial_before_start_preserves_workspace(task_env, stop):
    from app.worker import process_claim
    env = task_env
    session, _ = task_ready(env)
    result = task_dispatch(env, session)
    setattr(env.repo.account_reader, stop, True)
    claim = env.repo.claim(result["session"]["active_run_id"], "blocked-worker")
    effects = []
    process_claim(env.repo, claim, lambda *args: effects.append(True))
    assert not effects
    assert read(env, SESSIONS_COLLECTION, session["id"])["recovery"]["reason"] == "queued_run_not_started"
    assert read(env, SLOTS_COLLECTION, env.uid) == {"runs": [], "sandboxes": [session["id"]]}
    assert not env.db.collection(CLEANUP_COLLECTION).document(session["id"]).get().exists


def test_task_reservation_race_shares_cumulative_ledger_and_settles_after_revoke(task_env):
    env = task_env
    session, grant, claim, _ = task_running(env, max_model_requests=1, max_reserved_tokens=10)
    entry = ModelEntry("anthropic", "fixture-selected", None)
    def reserve():
        try:
            return env.ledger.reserve(env.uid, entry, LIMITS, 6, run_id=claim.job_id)
        except BudgetExceeded:
            return None
    results = race(reserve, reserve)
    accepted = [value for value in results if value]
    assert len(accepted) == 1
    env.repo.revoke_task(session["id"], env.uid, grant["id"], "revoke", 1)
    with pytest.raises(BudgetExceeded):
        env.ledger.reserve(env.uid, entry, LIMITS, 1, run_id=claim.job_id)
    env.ledger.settle(accepted[0], {"input_tokens": 1, "output_tokens": 1})
    usage = env.repo.inspect_task(session["id"], env.uid, grant["id"])["resource_usage"]
    assert usage == {"requests": 1, "reserved_tokens": 6, "actual_tokens": 2}


def test_task_missing_budget_does_not_reset_and_scope_drift_blocks(task_env):
    from app.billing.budgets import _key
    env = task_env
    session, grant = task_ready(env)
    state = read(env, SESSIONS_COLLECTION, session["id"])
    env.db.collection(SESSIONS_COLLECTION).document(session["id"]).set({"mode": "plan"}, merge=True)
    with pytest.raises(CommandError) as failure:
        task_dispatch(env, session)
    assert failure.value.status == 403
    env.db.collection(SESSIONS_COLLECTION).document(session["id"]).set({"mode": state["mode"]}, merge=True)
    env.db.collection(BUDGETS_COLLECTION).document(_key(env.uid, "task", grant["id"])).delete()
    with pytest.raises(CommandError) as failure:
        task_dispatch(env, session)
    assert failure.value.status == 503
    assert len(owned(env.db, JOBS_COLLECTION, env.uid)) == 1
    assert read(env, GRANTS_COLLECTION, grant["id"])["uses"] == 0
    assert env.repo.revoke_task(session["id"], env.uid, grant["id"], "revoke", 1)["status"] == "revoked"


@pytest.mark.parametrize("revoked", [False, True])
def test_task_approval_resume_keeps_pin_and_original_deadline(task_env, monkeypatch, revoked):
    import app.agent.risk as risk
    env = task_env
    session, grant, claim, current = task_running(env)
    deadline = read(env, JOBS_COLLECTION, claim.job_id)["run_deadline"]
    monkeypatch.setitem(risk.RISK_POLICY, "write_file", "always")
    seal_pending(env.repo, claim, current, {"id": "approval", "provider": "anthropic", "model": "fixture-selected", "quota": None,
        "calls": [{"id": "risk", "name": "write_file", "input": {"path": "fixture", "content": "synthetic"}, "risky": True}]})
    env.repo.save(claim, current)
    env.repo.finish(claim)
    if revoked:
        env.repo.revoke_task(session["id"], env.uid, grant["id"], "revoke", 1)
    env.clock[0] += 5
    payload = {"approval_id": current["pending_approval"]["id"], "decisions": [{"call_id": "risk", "approved": True}]}
    if revoked:
        with pytest.raises(CommandError) as failure:
            env.repo.submit(env.uid, "approve", "approve", payload, session["id"])
        assert failure.value.status == 403
        assert read(env, SESSIONS_COLLECTION, session["id"])["status"] == "awaiting_approval"
    else:
        approved = env.repo.submit(env.uid, "approve", "approve", payload, session["id"])
        second = env.repo.claim(approved["active_run_id"], "approval-worker")
        env.repo.start(second)
        job = read(env, JOBS_COLLECTION, second.job_id)
        assert job["task_authorization_id"] == grant["id"] and job["run_deadline"] == deadline

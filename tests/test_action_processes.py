"""Receipt crash windows through actual supervised Windows spawn/RPC."""
import json

import pytest

from app.execution.repository import JOBS_COLLECTION, RunRepository
from app.execution.slots import SLOTS_COLLECTION
from app.store import SESSIONS_COLLECTION
from app.worker import process_claim
from test_durable_runs import doc, env
from spawn_stubs import action_stub
from spawn_stubs import forged_plan_stub


@pytest.mark.parametrize("mode,receipt,effected", [
    ("prepared_crash", "prepared", False),
    ("permit_crash", "outcome_unknown", False),
    ("effect_crash", "outcome_unknown", True),
    ("returned_crash", "handler_returned", True),
    ("duplicate", "handler_returned", True),
    ("success", "handler_returned", True),
])
def test_real_crash_windows_and_duplicate_delivery_never_repeat_effect(env, tmp_path, mode, receipt, effected):
    repo, db, clock = env
    marker = tmp_path / "local-effect"
    session = repo.submit("alice", "receipt-spawn", "create", {
        "task": json.dumps({"mode": mode, "marker": str(marker)}), "kind": "code"})
    claim = repo.claim(session["active_run_id"], "spawn-worker")
    process_claim(repo, claim, action_stub)
    actions = repo.action_history(session["id"], "alice")["actions"]
    assert len(actions) == 1 and actions[0]["state"] == receipt
    assert marker.exists() is effected
    if effected:
        assert marker.read_text() == "effect\n"
    current = doc(db, SESSIONS_COLLECTION, session["id"])
    assert current["status"] == ("idle" if mode == "success" else "error")
    assert current["recovery_required"] is (mode != "success")
    slots = doc(db, SLOTS_COLLECTION, "alice")
    assert slots["runs"] == ([] if mode == "success" else [claim.job_id])
    restarted = RunRepository(db, clock=lambda: clock[0], transaction_runner=db.transaction)
    assert restarted.claim(claim.job_id, "restart") is None
    assert restarted.inspect_action(session["id"], "alice", actions[0]["id"])["action"]["state"] == receipt


@pytest.mark.parametrize("mode", ["save", "prepare"])
def test_real_child_forged_plan_cannot_save_or_prepare_mutation(env, tmp_path, mode):
    repo, db, _ = env
    marker = tmp_path / "must-not-exist"
    session = repo.submit("alice", "plan-spawn", "create", {"kind": "code", "mode": "plan",
        "task": json.dumps({"mode": mode, "marker": str(marker)})})
    claim = repo.claim(session["active_run_id"], "worker")
    process_claim(repo, claim, forged_plan_stub)
    assert not marker.exists()
    assert repo.action_history(session["id"], "alice")["actions"] == []
    current = doc(db, SESSIONS_COLLECTION, session["id"])
    assert current["mode"] == "plan" and current["status"] == "error" and current["recovery_required"]

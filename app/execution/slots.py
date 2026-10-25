"""Fail-closed per-user admission slots shared by all API/worker instances.

No time-based expiry: an unknown external outcome must not free capacity for
more work. Manual reconciliation is necessary after interrupted execution.
"""
import os


def _capacity(name, default):
    value = int(os.environ.get(name, default))
    if not 1 <= value <= 100:
        raise ValueError(f"{name} must be between 1 and 100")
    return value


SLOTS_COLLECTION = "agent_v2_slots"
MAX_ACTIVE_RUNS = _capacity("MAX_ACTIVE_RUNS_PER_USER", "2")
MAX_SANDBOXES = _capacity("MAX_SANDBOXES_PER_USER", "3")


def read_slots(db, tx, uid):
    ref = db.collection(SLOTS_COLLECTION).document(uid)
    state = ref.get(transaction=tx).to_dict() or {"runs": [], "sandboxes": []}
    return ref, state


def admit(state, run_id, session_id, *, creating):
    if len(state["runs"]) >= MAX_ACTIVE_RUNS:
        return "active run capacity exhausted"
    if creating and len(state["sandboxes"]) >= MAX_SANDBOXES:
        return "sandbox capacity exhausted; close an existing session"
    if not creating and session_id not in state["sandboxes"]:
        return "session has no reserved sandbox; reconciliation required"
    state["runs"].append(run_id)
    if creating:
        state["sandboxes"].append(session_id)
    return None


def release_run(state, run_id):
    state["runs"] = [value for value in state["runs"] if value != run_id]


def release_sandbox(state, session_id):
    state["sandboxes"] = [value for value in state["sandboxes"] if value != session_id]

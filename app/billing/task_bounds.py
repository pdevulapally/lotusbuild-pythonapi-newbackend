"""Additional finite task ceilings in the existing reservation transaction."""
import math

from app.billing.budgets import BUDGETS_COLLECTION, BudgetExceeded, _key


def prepare_task_reservation(client, tx, uid, run_id, entry, tokens, clock):
    now = clock()
    if run_id is None:
        return None
    from app.execution.repository import JOBS_COLLECTION
    from app.execution.task_authorization import GRANTS_COLLECTION, POLICY_COLLECTION, POLICY_ID, policy_from, check_grant
    from app.store import SESSIONS_COLLECTION
    def read(collection, key):
        snap = client.collection(collection).document(key).get(transaction=tx)
        return snap.to_dict() if snap.exists else None
    job = read(JOBS_COLLECTION, run_id)
    if not job or not job.get("task_authorization_id"):
        return None
    try:
        session = read(SESSIONS_COLLECTION, job["session_id"])
        grant = read(GRANTS_COLLECTION, job["task_authorization_id"])
        policy = policy_from(read(POLICY_COLLECTION, POLICY_ID))
        scope = check_grant(grant, session, policy, now, version=job["task_authorization_version"])
        evidence = job.get("task_account_evidence", {})
        if (job.get("uid") != uid or not session or session["uid"] != uid or session.get("active_run_id") != run_id or
                job.get("state") != "running" or job.get("lease_expires_at", 0) <= now or job.get("run_deadline", 0) <= now or
                (session.get("task_authorization_id"), session.get("task_authorization_version")) != (grant["id"], job["task_authorization_version"]) or
                (entry.provider, entry.model) != (scope["provider"], scope["model"]) or evidence.get("uid") != uid or
                any(type(evidence.get(k)) not in (int, float) or not math.isfinite(evidence[k]) for k in ("checked_at", "valid_until", "valid_after_ms")) or
                not evidence["checked_at"] <= now < evidence["valid_until"] or evidence["valid_until"] > evidence["checked_at"] + policy["evidence_ttl_seconds"] or
                grant["consent_iat"] * 1000 < evidence["valid_after_ms"]):
            raise ValueError("task authority unavailable")
        ref = client.collection(BUDGETS_COLLECTION).document(_key(uid, "task", grant["id"]))
        snap = ref.get(transaction=tx)
        if not snap.exists:
            raise ValueError("missing initialized task budget state")
        state = snap.to_dict()
        if (state.get("uid") != uid or state.get("task_authorization_id") != grant["id"] or state.get("blocked") or
                any(type(state.get(k)) is not int or state[k] < 0 for k in ("requests", "reserved_tokens", "actual_tokens")) or
                state["requests"] + 1 > scope["max_model_requests"] or state["reserved_tokens"] + tokens > scope["max_reserved_tokens"]):
            raise ValueError("task resource ceiling reached")
        state.update(requests=state["requests"] + 1, reserved_tokens=state["reserved_tokens"] + tokens)
        def final_check():
            fresh_now = clock()
            check_grant(grant, session, policy, fresh_now, version=job["task_authorization_version"])
            if (not evidence["checked_at"] <= fresh_now < evidence["valid_until"] or
                    fresh_now >= job.get("lease_expires_at", 0) or fresh_now >= job.get("run_deadline", 0)):
                raise BudgetExceeded("task authority expired during reservation")
        final_check()
        return ref, state, final_check
    except Exception:
        raise BudgetExceeded("task authorization or resource ceiling unavailable") from None

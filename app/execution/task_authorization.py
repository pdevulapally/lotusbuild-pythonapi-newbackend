"""Explicit exact-session native consent, additional to current auth/Plan/budget.

No credential/grant is provisioned on import. No mutable scope or autoapproval.
"""
import copy
import math
from dataclasses import dataclass

from app.auth import QueueIdentity
from app.auth_account import AccountEvidence, require_account
from app.execution.contracts import reject, timestamp, valid_timestamp
from app.execution.queue import identity_valid

GRANTS_COLLECTION = "agent_v2_task_authorizations"
POLICY_COLLECTION, POLICY_ID = "config", "task_authorization"
OPERATIONS = frozenset({"queue.dispatch", "run.execute"})
# This v1 authority never permits shell/browser/computer/connector/deploy tools.
SAFE_TOOLS = frozenset({"read_file", "write_file", "update_plan"})
BOUND_KEYS = ("max_turns", "max_run_seconds", "max_model_requests", "max_reserved_tokens")


@dataclass(frozen=True)
class TaskDispatchEvidence:
    uid: str
    grant_id: str
    version: int
    item_id: str
    account: AccountEvidence


def policy_from(value):
    if not isinstance(value, dict):
        reject(503, "Task authorization policy is not configured.", "task/policy-unavailable")
    integers = ("version", "max_duration_seconds", *BOUND_KEYS)
    if (type(value.get("schema_version")) is not int or value["schema_version"] != 1 or any(type(value.get(k)) is not int or not 1 <= value[k] <= 2**53 - 1 for k in integers) or
            value["max_run_seconds"] > 900 or any(type(value.get(k)) not in (int, float) or not math.isfinite(value[k]) for k in ("reader_timeout_seconds", "evidence_ttl_seconds")) or
            not 0 < value["reader_timeout_seconds"] < value["evidence_ttl_seconds"] <= 30 or
            not isinstance(value.get("allowed_modes"), list) or not value["allowed_modes"] or any(not isinstance(v, str) for v in value["allowed_modes"]) or
            len(value["allowed_modes"]) != len(set(value["allowed_modes"])) or set(value["allowed_modes"]) - {"build", "plan"} or
            not isinstance(value.get("allowed_models"), list) or not value["allowed_models"] or len(value["allowed_models"]) > 100 or
            any(not isinstance(v, str) or not 1 <= len(v) <= 128 for v in value["allowed_models"]) or
            not isinstance(value.get("allowed_tools"), list) or not value["allowed_tools"] or any(not isinstance(v, str) for v in value["allowed_tools"]) or
            len(value["allowed_tools"]) != len(set(value["allowed_tools"])) or set(value["allowed_tools"]) - SAFE_TOOLS):
        reject(503, "Task authorization policy is invalid or unsupported.", "task/policy-unavailable")
    return value


def public_grant(grant, now):
    return {k: grant[k] for k in ("id", "session_id", "project_id", "version", "status", "scope", "uses", "created_at")} | {
        "effective_status": "expired" if grant["status"] == "active" and now >= grant["scope"]["expires_at"] else grant["status"]}


def task_owner_fresh(identity, policy, now):
    identity_valid(identity, identity.uid, now)
    verified_at = identity.verified_at if identity.verified_at is not None else identity.valid_until - 1
    if (type(verified_at) not in (int, float) or not math.isfinite(verified_at) or verified_at > now or
            now >= verified_at + policy["evidence_ttl_seconds"]):
        reject(401, "Fresh task owner consent is required.", "task/authorization-unavailable")


def check_grant(grant, session, policy, now, *, version=None):
    if (not isinstance(grant, dict) or grant.get("uid") != session["uid"] or grant.get("session_id") != session["id"] or
            grant.get("project_id") != session.get("project_id") or grant.get("schema_version") != 1 or
            type(grant.get("version")) is not int or type(grant.get("uses")) is not int or grant["uses"] < 0 or
            not isinstance(grant.get("scope"), dict)):
        reject(503, "Task authorization requires reconciliation.", "task/invalid")
    scope = grant["scope"]
    if (grant.get("status") != "active" or (version is not None and grant["version"] != version) or
            grant.get("policy_version") != policy["version"] or type(scope.get("expires_at")) not in (int, float) or
            not math.isfinite(scope["expires_at"]) or now >= scope["expires_at"]):
        reject(403, "Task authorization is expired, revoked or changed.", "task/not-authorized")
    if (session.get("kind") != "code" or scope.get("mode") != session.get("mode", "build") or
            scope.get("model") != session.get("model") or scope.get("provider") != session.get("provider") or
            scope.get("effort") != session.get("effort") or scope["mode"] not in policy["allowed_modes"] or
            scope["model"] not in policy["allowed_models"] or scope.get("operations") != sorted(OPERATIONS) or
            not isinstance(scope.get("tools"), list) or not scope["tools"] or set(scope["tools"]) - set(policy["allowed_tools"]) or
            any(type(scope.get(k)) is not int or not 1 <= scope[k] <= policy[k] for k in BOUND_KEYS)):
        reject(403, "Task authorization does not permit this execution scope.", "task/scope-denied")
    return scope


class TaskAuthorizations:
    def _task_policy(self, tx=None):
        ref = self._ref(POLICY_COLLECTION, POLICY_ID)
        snapshot = None if tx is not None else ref.get()
        return policy_from(self._read(ref, tx) if tx is not None else (snapshot.to_dict() if snapshot.exists else None))

    def _task_grant(self, session, grant_id, tx=None):
        from app.execution.contracts import require_id
        require_id(grant_id, "task-authorization")
        ref = self._ref(GRANTS_COLLECTION, grant_id)
        snapshot = None if tx is not None else ref.get()
        grant = self._read(ref, tx) if tx is not None else (snapshot.to_dict() if snapshot.exists else None)
        if not grant or grant.get("uid") != session["uid"] or grant.get("session_id") != session["id"]:
            reject(404, "task authorization not found", "task/not-found")
        scope = grant.get("scope")
        if (grant.get("id") != grant_id or grant.get("project_id") != session.get("project_id") or type(grant.get("schema_version")) is not int or grant["schema_version"] != 1 or
                not isinstance(grant.get("status"), str) or grant["status"] not in {"active", "revoked"} or
                type(grant.get("version")) is not int or grant["version"] < 1 or type(grant.get("policy_version")) is not int or grant["policy_version"] < 1 or
                type(grant.get("uses")) is not int or grant["uses"] < 0 or not valid_timestamp(grant.get("created_at")) or
                type(grant.get("consent_iat")) not in (int, float) or not math.isfinite(grant["consent_iat"]) or grant["consent_iat"] < 0 or
                not isinstance(scope, dict) or any(type(scope.get(k)) is not int or scope[k] < 1 for k in BOUND_KEYS) or
                type(scope.get("expires_at")) not in (int, float) or not math.isfinite(scope["expires_at"]) or
                not isinstance(scope.get("mode"), str) or scope["mode"] not in {"build", "plan"} or any(not isinstance(scope.get(k), str) or not scope[k] for k in ("model", "provider")) or
                scope.get("operations") != sorted(OPERATIONS) or not isinstance(scope.get("tools"), list) or not scope["tools"] or
                any(not isinstance(v, str) for v in scope["tools"]) or set(scope["tools"]) - SAFE_TOOLS or grant["uses"] > scope["max_turns"]):
            reject(503, "Task authorization requires reconciliation.", "task/invalid")
        return ref, grant

    def _task_account(self, uid, policy):
        if self.account_reader is None:
            reject(503, "Bounded fresh task account reader is not configured.", "task/account-unavailable")
        try:
            return self.account_reader.read(uid, timeout=policy["reader_timeout_seconds"], ttl=policy["evidence_ttl_seconds"], clock=self.clock)
        except BaseException:
            reject(503, "Fresh task account status is unavailable.", "task/account-unavailable")

    def consent_task(self, session_id, identity, key, payload):
        from app.execution.repository import RECEIPTS_COLLECTION, _digest
        session = self._owner_session(session_id, identity.uid)
        identity_valid(identity, session["uid"], self.clock())
        if getattr(identity, "tenant_id", None) is not None:
            reject(403, "Task authorization supports only the configured default Firebase namespace.", "task/namespace-unsupported")
        if not isinstance(key, str) or not 1 <= len(key) <= 200:
            reject(422, "Idempotency-Key must contain 1 to 200 characters", "request/invalid")
        gid = _digest([identity.uid, "task_consent", session_id, key])
        fingerprint = _digest(payload)
        receipt_ref = self._ref(RECEIPTS_COLLECTION, gid)
        def replay(tx):
            current = self._owner_session(session_id, identity.uid, tx)
            receipt = self._read(receipt_ref, tx)
            if receipt:
                self._task_grant(current, gid, tx)
                if receipt["fingerprint"] != fingerprint:
                    reject(409, "idempotency key was used with different input", "command/conflict")
                return receipt["response"]
        existing = self._tx(replay)
        if existing is not None:
            return existing
        policy = self._task_policy()
        task_owner_fresh(identity, policy, self.clock())
        account = self._task_account(identity.uid, policy)
        def consent(tx):
            current = self._owner_session(session_id, identity.uid, tx)
            receipt = self._read(receipt_ref, tx)
            if receipt:
                if receipt["fingerprint"] != fingerprint:
                    reject(409, "idempotency key was used with different input", "command/conflict")
                return receipt["response"]
            configured = self._task_policy(tx)
            if configured != policy:
                reject(503, "Task policy changed during account verification; retry with fresh evidence.", "task/policy-unavailable")
            ref = self._ref(GRANTS_COLLECTION, gid)
            if self._read(ref, tx) is not None:
                reject(503, "Task consent requires reconciliation.", "task/invalid")
            from app.billing.budgets import BUDGETS_COLLECTION, _key
            task_budget_ref = self._ref(BUDGETS_COLLECTION, _key(identity.uid, "task", gid))
            if self._read(task_budget_ref, tx) is not None:
                reject(503, "Task budget state exists without its consent.", "task/invalid")
            if (not current.get("project_id") or current.get("recovery_required") or current.get("cancel_requested") or
                    current.get("workspace_operation") or current.get("workspace_restore_id")):
                reject(409, "Task session cannot accept standing consent in its current state.", "task/session-stopped")
            self._selection(tx, current, current.get("pending_approval"))
            scope = copy.deepcopy(payload)
            if (set(scope) != {"mode", "model", "provider", "effort", "operations", "tools", "expires_at", *BOUND_KEYS} or
                    type(scope["expires_at"]) not in (int, float) or not math.isfinite(scope["expires_at"]) or
                    not self.clock() < scope["expires_at"] <= self.clock() + configured["max_duration_seconds"]):
                reject(422, "Explicit finite task scope and bounds are required.", "task/invalid-input")
            grant = {"id": gid, "uid": identity.uid, "session_id": session_id, "project_id": current["project_id"],
                     "schema_version": 1, "version": 1, "status": "active", "policy_version": configured["version"],
                     "scope": scope, "uses": 0, "consent_iat": identity.issued_at, "created_at": timestamp(self.clock())}
            check_grant(grant, current, configured, self.clock())
            from app.agent.tools import tool_spec
            from app.agent.policy import tool_allowed
            for name in scope["tools"]:
                spec = tool_spec(name)
                if not getattr(spec, "enabled", True) or not tool_allowed(spec, scope["mode"]):
                    reject(403, "Task tools exceed current mode capability.", "task/scope-denied")
            identity_valid(identity, identity.uid, self.clock())
            task_owner_fresh(identity, configured, self.clock())
            require_account(account, identity.uid, identity.issued_at, self.clock())
            response = public_grant(grant, self.clock())
            tx.create(ref, grant)
            tx.create(task_budget_ref, {"uid": identity.uid, "task_authorization_id": gid,
                "requests": 0, "reserved_tokens": 0, "actual_tokens": 0})
            tx.create(receipt_ref, {"uid": identity.uid, "fingerprint": fingerprint, "response": response, "created_at": self.clock(), "schema_version": 2})
            identity_valid(identity, identity.uid, self.clock())
            task_owner_fresh(identity, configured, self.clock())
            require_account(account, identity.uid, identity.issued_at, self.clock())
            return response
        return self._tx(consent)

    def inspect_task(self, session_id, uid, grant_id):
        def inspect(tx):
            session = self._owner_session(session_id, uid, tx)
            _, grant = self._task_grant(session, grant_id, tx)
            budget = self._task_budget(tx, session, grant)
            return public_grant(grant, self.clock()) | {"resource_usage": {k: budget[k] for k in ("requests", "reserved_tokens", "actual_tokens")}}
        return self._tx(inspect)

    def _task_budget(self, tx, session, grant):
        from app.billing.budgets import BUDGETS_COLLECTION, _key
        state = self._read(self._ref(BUDGETS_COLLECTION, _key(session["uid"], "task", grant["id"])), tx)
        if (not state or state.get("uid") != session["uid"] or state.get("task_authorization_id") != grant["id"] or
                any(type(state.get(k)) is not int or state[k] < 0 for k in ("requests", "reserved_tokens", "actual_tokens"))):
            reject(503, "Initialized task budget state requires reconciliation.", "task/budget-unavailable")
        return state

    def revoke_task(self, session_id, uid, grant_id, key, expected_version):
        from app.execution.repository import RECEIPTS_COLLECTION, _digest
        def revoke(tx):
            session = self._owner_session(session_id, uid, tx)
            ref, grant = self._task_grant(session, grant_id, tx)
            if not isinstance(key, str) or not 1 <= len(key) <= 200 or type(expected_version) is not int or expected_version < 1:
                reject(422, "An idempotent expected-version revocation is required.", "task/invalid-input")
            receipt_ref = self._ref(RECEIPTS_COLLECTION, _digest([uid, "task_revoke", session_id, grant_id, key]))
            receipt = self._read(receipt_ref, tx)
            if receipt:
                if receipt["fingerprint"] != _digest(expected_version):
                    reject(409, "idempotency key was used with different input", "command/conflict")
                return receipt["response"]
            if grant["version"] != expected_version:
                reject(409, "Task authorization version changed.", "task/version-conflict")
            if grant["status"] != "revoked":
                grant.update(status="revoked", version=grant["version"] + 1, revoked_at=timestamp(self.clock()))
            response = public_grant(grant, self.clock())
            tx.set(ref, grant)
            tx.create(receipt_ref, {"uid": uid, "fingerprint": _digest(expected_version), "response": response, "created_at": self.clock(), "schema_version": 2})
            return response
        return self._tx(revoke)

    def task_queue_evidence(self, candidate):
        session = self._owner_session(candidate["session_id"], candidate["uid"])
        _, item = self._queue_item(session, candidate["id"])
        if not item.get("task_authorization_id"):
            reject(403, "This finite queue item has no durable task consent.", "task/not-authorized")
        _, grant = self._task_grant(session, item["task_authorization_id"])
        policy = self._task_policy()
        check_grant(grant, session, policy, self.clock(), version=item["task_authorization_version"])
        account = self._task_account(session["uid"], policy)
        require_account(account, session["uid"], grant["consent_iat"], self.clock())
        return TaskDispatchEvidence(session["uid"], grant["id"], grant["version"], item["id"], account)

    def _task_queue_check(self, tx, session, item, evidence, *, consume=False):
        if (not isinstance(evidence, TaskDispatchEvidence) or evidence.uid != session["uid"] or
                evidence.item_id != item["id"] or evidence.grant_id != item.get("task_authorization_id") or
                evidence.version != item.get("task_authorization_version")):
            reject(403, "Task evidence does not bind this queue item.", "task/scope-denied")
        ref, grant = self._task_grant(session, evidence.grant_id, tx)
        policy = self._task_policy(tx)
        scope = check_grant(grant, session, policy, self.clock(), version=evidence.version)
        require_account(evidence.account, session["uid"], grant["consent_iat"], self.clock())
        if evidence.account.valid_until > evidence.account.checked_at + policy["evidence_ttl_seconds"]:
            reject(503, "Account evidence exceeds current freshness policy.", "task/account-unavailable")
        if item["expires_at"] > scope["expires_at"]:
            reject(403, "Task consent cannot extend queued execution expiry.", "task/scope-denied")
        if consume:
            budget = self._task_budget(tx, session, grant)
            if budget.get("blocked") or budget["requests"] >= scope["max_model_requests"] or budget["reserved_tokens"] >= scope["max_reserved_tokens"]:
                reject(403, "Task resource authorization exhausted.", "task/exhausted")
            if grant["uses"] >= scope["max_turns"]:
                reject(403, "Task authorization turn bound exhausted.", "task/exhausted")
            grant["uses"] += 1
        return ref, grant

    def _task_run_evidence(self, claim):
        from app.execution.repository import JOBS_COLLECTION
        snap = self._ref(JOBS_COLLECTION, claim.job_id).get()
        job = snap.to_dict() if snap.exists else None
        if not job or not job.get("task_authorization_id"):
            return None
        session = self._owner_session(claim.session_id, job["uid"])
        _, grant = self._task_grant(session, job["task_authorization_id"])
        policy = self._task_policy()
        check_grant(grant, session, policy, self.clock(), version=job["task_authorization_version"])
        return self._task_account(job["uid"], policy)

    def _task_run_check(self, tx, job, session, evidence):
        if not job.get("task_authorization_id"):
            if session.get("task_authorization_id"):
                reject(503, "Task run binding is missing.", "task/invalid")
            return None
        if (session.get("task_authorization_id"), session.get("task_authorization_version")) != (job["task_authorization_id"], job.get("task_authorization_version")):
            reject(403, "Task run binding changed.", "task/scope-denied")
        _, grant = self._task_grant(session, job["task_authorization_id"], tx)
        policy = self._task_policy(tx)
        scope = check_grant(grant, session, policy, self.clock(), version=job["task_authorization_version"])
        require_account(evidence, session["uid"], grant["consent_iat"], self.clock())
        if evidence.valid_until > evidence.checked_at + policy["evidence_ttl_seconds"]:
            reject(503, "Account evidence exceeds current freshness policy.", "task/account-unavailable")
        job["task_account_evidence"] = {"uid": evidence.uid, "valid_after_ms": evidence.valid_after_ms,
                                        "checked_at": evidence.checked_at, "valid_until": evidence.valid_until}
        job["task_run_seconds_limit"] = scope["max_run_seconds"]
        job["task_allowed_tools"] = scope["tools"]
        session["task_allowed_tools"] = scope["tools"]
        return grant


class TaskQueueAuthorizer:
    """Explicit composition hook only; never installed/enabled by main."""
    def __init__(self, repository):
        self.repository = repository

    def __call__(self, candidate):
        return self.repository.task_queue_evidence(candidate)

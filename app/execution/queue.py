"""Finite, owner-authorized FIFO next turns on the existing command authority.

No steering, token persistence, scheduler, background grant or alternate runner.
"""
import math

from google.cloud.firestore_v1.base_query import FieldFilter

from app.auth import QueueIdentity
from app.execution.contracts import reject, require_id, timestamp, valid_timestamp
from app.execution.errors import LeaseLost
from app.execution.limits import bounded_json, bounded_session, MAX_SESSION_BYTES

QUEUE_COLLECTION = "agent_v2_queued_messages"
MAX_PENDING = 100  # Technical document/read bound, not a commercial allowance.


def prompt_text(value):
    if not isinstance(value, str):
        reject(422, "invalid queued prompt", "request/invalid")
    value = value.strip()
    if not 1 <= len(value) <= 8000 or any((ord(c) < 32 and c not in "\t\n\r") or ord(c) == 127 for c in value):
        reject(422, "invalid queued prompt", "request/invalid")
    return value


def identity_valid(identity, uid, now):
    if (not isinstance(identity, QueueIdentity) or identity.uid != uid or
            any(type(v) not in (int, float) or not math.isfinite(v) for v in
                (identity.issued_at, identity.expires_at, identity.valid_until)) or
            identity.issued_at < 0 or now >= min(identity.expires_at, identity.valid_until)):
        reject(401, "Fresh queue authorization is required.", "queue/authorization-unavailable")


def projection(item, now):
    result = {k: item[k] for k in ("id", "sessionId", "prompt", "attachments", "createdAt", "expiresAt", "state")}
    if result["state"] == "queued" and item["expires_at"] <= now:
        result["state"] = "expired"
    if item.get("run_id"):
        result["runId"] = item["run_id"]
    return result


class QueueTransitions:
    def _queue_state(self, session):
        pending, sequence = session.get("queue_pending", []), session.get("queue_sequence", 0)
        if (not isinstance(pending, list) or len(pending) > MAX_PENDING or
                any(not isinstance(item, str) or len(item) != 64 for item in pending) or len(set(pending)) != len(pending) or
                type(sequence) is not int or not 0 <= sequence <= 2**53 - 1):
            reject(503, "Queue metadata requires reconciliation.", "queue/history-unavailable")
        return pending, sequence

    def _queue_item(self, session, item_id, tx=None):
        require_id(item_id, "queued-message")
        ref = self._ref(QUEUE_COLLECTION, item_id)
        snap = None if tx is not None else ref.get()
        item = self._read(ref, tx) if tx is not None else (snap.to_dict() if snap.exists else None)
        if not item or item.get("uid") != session["uid"] or item.get("sessionId") != session["id"]:
            reject(404, "queued message not found", "queue/not-found")
        if (item.get("id") != item_id or item.get("state") not in {"queued", "cancelled", "expired", "dispatched"} or
                type(item.get("sequence")) is not int or item["sequence"] < 1 or item.get("attachments") != [] or
                not isinstance(item.get("prompt"), str) or not isinstance(item.get("createdAt"), str) or not isinstance(item.get("expiresAt"), str) or
                any(type(item.get(key)) not in (int, float) or not math.isfinite(item[key]) for key in ("expires_at", "authorized_iat", "identity_exp"))):
            reject(503, "Queue record requires reconciliation.", "queue/history-unavailable")
        try:
            from app.execution.repository import CommandError
            text = prompt_text(item["prompt"])
            expiry = timestamp(item["expires_at"])
        except (CommandError, ValueError, OverflowError, OSError):
            reject(503, "Queue record requires reconciliation.", "queue/history-unavailable")
        if (text != item["prompt"] or not valid_timestamp(item["createdAt"]) or item["expiresAt"] != expiry or
                not 0 <= item["authorized_iat"] < item["identity_exp"] or (item["expires_at"] > item["identity_exp"] and not item.get("task_authorization_id")) or
                item["sequence"] > session.get("queue_sequence", 0)):
            reject(503, "Queue record requires reconciliation.", "queue/history-unavailable")
        return ref, item

    def _build_meter(self, tx, uid):
        from app.billing.entitlements import resolve_entitlements
        entitlements = resolve_entitlements(uid, client=self.db, transaction=tx, now=self.clock())
        registry = self._read(self._ref("config", "registry"), tx) or {}
        key = registry.get("buildMeterKey")
        meters, limits = registry.get("meters"), entitlements.meter_limits
        if not isinstance(key, str) or not key or not isinstance(meters, list) or not isinstance(limits, list):
            reject(503, "Queue requires config/registry.buildMeterKey, its counter meter and current plan meterLimits.", "queue/policy-unavailable")
        declared = [entry for entry in meters if isinstance(entry, dict) and entry.get("key") == key]
        permitted = [entry for entry in limits if isinstance(entry, dict) and entry.get("meterKey") == key]
        if len(declared) != 1 or len(permitted) != 1:
            reject(503, "Configured queue build meter is missing or ambiguous.", "queue/policy-unavailable")
        meter, limit = declared[0], permitted[0]
        if (meter.get("kind") != "COUNTER" or meter.get("resetPeriod") not in {"DAILY", "MONTHLY", "NEVER"} or
                any(type(limit.get(k)) is not int or not 0 <= limit[k] <= 2**53 - 1 for k in ("includedAllowance", "hardLimit")) or
                limit["includedAllowance"] > limit["hardLimit"] or limit.get("overageEnabled") is not False):
            reject(503, "Queue build-meter configuration is invalid or requires unsupported paid overage.", "queue/policy-unavailable")
        return key, meter["resetPeriod"], limit["hardLimit"]

    def enqueue_message(self, session_id, uid, key, prompt, expires_at, identity, *, task_authorization_id=None):
        from app.execution.repository import RECEIPTS_COLLECTION, _digest
        from app.store import SESSIONS_COLLECTION
        task_evidence = None
        if task_authorization_id is not None:
            from app.execution.task_authorization import TaskDispatchEvidence
            owned = self._owner_session(session_id, uid)
            identity_valid(identity, uid, self.clock())
            if identity.tenant_id is not None:
                reject(403, "Unsupported task namespace.", "task/namespace-unsupported")
            _, consent = self._task_grant(owned, task_authorization_id)
            account = self._task_account(uid, self._task_policy())
            task_evidence = TaskDispatchEvidence(uid, task_authorization_id, consent["version"],
                _digest([uid, "queue_enqueue", session_id, key]), account)
        def enqueue(tx):
            session = self._owner_session(session_id, uid, tx)
            identity_valid(identity, uid, self.clock())
            text = prompt_text(prompt)
            if not isinstance(key, str) or not 1 <= len(key) <= 200:
                reject(422, "Idempotency-Key must contain 1 to 200 characters", "request/invalid")
            if type(expires_at) not in (int, float) or not math.isfinite(expires_at):
                reject(422, "An explicit execution expiry is required.", "queue/expiry-required")
            item_id = _digest([uid, "queue_enqueue", session_id, key])
            receipt_ref = self._ref(RECEIPTS_COLLECTION, item_id)
            receipt = self._read(receipt_ref, tx)
            fingerprint = _digest({"prompt": text, "expires_at": expires_at} | ({"task_authorization_id": task_authorization_id} if task_authorization_id is not None else {}))
            if receipt:
                self._queue_item(session, item_id, tx)
                if receipt["fingerprint"] != fingerprint:
                    reject(409, "idempotency key was used with different input", "command/conflict")
                return receipt["response"]  # Never renew original expiry/identity.
            grant_check_item = {"id": item_id, "expires_at": expires_at, "task_authorization_id": task_authorization_id,
                                "task_authorization_version": task_evidence.version if task_evidence else None}
            if task_evidence is not None:
                _, consent = self._task_queue_check(tx, session, grant_check_item, task_evidence)
                from app.execution.task_authorization import task_owner_fresh
                checked_task_policy = self._task_policy(tx)
                task_owner_fresh(identity, checked_task_policy, self.clock())
                maximum_expiry = consent["scope"]["expires_at"]
            else:
                maximum_expiry = identity.expires_at
            if not self.clock() < expires_at <= maximum_expiry:
                reject(422, "Execution expiry exceeds the explicit authorization window.", "queue/expiry-invalid")
            if (session["status"] not in {"queued", "running"} or session.get("cancel_requested") or
                    session.get("recovery_required") or session.get("pending_approval") or
                    session.get("workspace_operation") or session.get("workspace_restore_id") or
                    session.get("transcript_version") != 1 or not session.get("project_id")):
                reject(409, "Session is not accepting queued next turns.", "queue/session-not-active")
            self._selection(tx, session)
            _, _, maximum = self._build_meter(tx, uid)
            pending, sequence = self._queue_state(session)
            # Explicitly expired items remain tombstones; no content is deleted.
            records = [self._queue_item(session, item_id, tx) for item_id in pending]
            if any(item["state"] != "queued" for _, item in records):
                reject(503, "Pending queue requires reconciliation.", "queue/history-unavailable")
            active = [item["id"] for _, item in records if item["expires_at"] > self.clock()]
            if len(active) >= min(maximum, MAX_PENDING):
                reject(429, "The configured queue or technical pending bound is full.", "queue/full")
            item = {"id": item_id, "sessionId": session_id, "uid": uid, "sequence": sequence + 1,
                    "prompt": text, "attachments": [], "createdAt": timestamp(self.clock()), "state": "queued",
                    "expiresAt": timestamp(expires_at), "expires_at": expires_at,
                    "authorized_iat": identity.issued_at, "identity_exp": identity.expires_at}
            identity_valid(identity, uid, self.clock())
            if self.clock() >= expires_at:
                reject(409, "Queue execution expiry elapsed during admission.", "queue/expired")
            if task_evidence is not None:
                item.update(task_authorization_id=task_evidence.grant_id, task_authorization_version=task_evidence.version)
            for ref, old in records:
                if old["expires_at"] <= self.clock():
                    tx.set(ref, old | {"state": "expired"})
            session.update(queue_pending=[*active, item_id], queue_sequence=sequence + 1)
            response = {"queued": True, "queuedMessage": projection(item, self.clock())}
            tx.create(self._ref(QUEUE_COLLECTION, item_id), item)
            self._write_session(tx, self._ref(SESSIONS_COLLECTION, session_id), session, "session.message_queued")
            bounded_session(session)
            tx.create(receipt_ref, {"uid": uid, "fingerprint": fingerprint, "response": response, "created_at": self.clock(), "schema_version": 2})
            identity_valid(identity, uid, self.clock())
            if task_evidence is not None:
                from app.auth_account import require_account
                require_account(task_evidence.account, uid, consent["consent_iat"], self.clock())
                task_owner_fresh(identity, checked_task_policy, self.clock())
            return response
        return self._tx(enqueue)

    def list_queued(self, session_id, uid, after=None, limit=100):
        def read(tx):
            session = self._owner_session(session_id, uid, tx)
            pending, _ = self._queue_state(session)
            start = 0
            if after is not None:
                _, cursor = self._queue_item(session, after, tx)
                start = cursor["sequence"]
            if type(limit) is not int or not 1 <= limit <= 200:
                reject(422, "queue page limit must be between 1 and 200", "request/invalid")
            items = [self._queue_item(session, item_id, tx)[1] for item_id in pending]
            sequences = [item["sequence"] for item in items]
            if any(item["state"] != "queued" for item in items) or sequences != sorted(set(sequences)):
                reject(503, "Pending queue requires reconciliation.", "queue/history-unavailable")
            now = self.clock()
            older = [item for item in items if item["sequence"] > start and item["expires_at"] > now]
            page = older[:limit]
            result = {"queuedMessages": [projection(item, now) for item in page],
                      "nextCursor": page[-1]["id"] if len(older) > len(page) else None,
                      "expiredCount": sum(item["expires_at"] <= now for item in items)}
            bounded_json(result, MAX_SESSION_BYTES)
            return result
        return self._tx(read)

    def inspect_queued(self, session_id, uid, item_id):
        session = self._owner_session(session_id, uid)
        return projection(self._queue_item(session, item_id)[1], self.clock())

    def cancel_queued(self, session_id, uid, item_id):
        from app.execution.repository import RECEIPTS_COLLECTION, _digest
        from app.store import SESSIONS_COLLECTION
        def cancel(tx):
            session = self._owner_session(session_id, uid, tx)
            ref, item = self._queue_item(session, item_id, tx)
            receipt_ref = self._ref(RECEIPTS_COLLECTION, _digest([uid, "queue_cancel", session_id, item_id]))
            receipt = self._read(receipt_ref, tx)
            if receipt:
                return receipt["response"]
            if item["state"] == "dispatched":
                reject(409, "Queued turn was dispatched; use the run cancellation contract.", "queue/already-dispatched")
            pending, _ = self._queue_state(session)
            if item["state"] == "queued" and item_id not in pending:
                reject(503, "Pending queue requires reconciliation.", "queue/history-unavailable")
            item["state"] = "expired" if item["expires_at"] <= self.clock() else "cancelled"
            session["queue_pending"] = [key for key in pending if key != item_id]
            session["queue_ready"] = bool(session.get("queue_ready") and session["queue_pending"])
            response = projection(item, self.clock())
            tx.set(ref, item)
            self._write_session(tx, self._ref(SESSIONS_COLLECTION, session_id), session, "session.message_dequeued")
            bounded_session(session)
            tx.create(receipt_ref, {"uid": uid, "response": response, "created_at": self.clock(), "schema_version": 2})
            return response
        return self._tx(cancel)

    def confirm_quiescent(self, claim):
        from app.execution.repository import JOBS_COLLECTION
        from app.store import SESSIONS_COLLECTION
        def confirm(tx):
            jr, sr = self._ref(JOBS_COLLECTION, claim.job_id), self._ref(SESSIONS_COLLECTION, claim.session_id)
            job, session = self._read(jr, tx), self._read(sr, tx)
            if (not job or not session or job.get("owner") != claim.owner or job.get("fence") != claim.token or
                    session.get("active_run_id") != claim.job_id):
                raise LeaseLost("quiescence authority changed")
            if job.get("state") != "succeeded" or session.get("status") != "idle":
                return  # Approval/cancellation/recovery never become success.
            if (job.get("open_actions") or session.get("cancel_requested") or session.get("recovery_required") or
                    session.get("pending_approval") or session.get("workspace_operation") or session.get("workspace_restore_id")):
                raise LeaseLost("run is not quiescent")
            job["completion_verified"] = True
            session["queue_ready"] = bool(self._queue_state(session)[0])
            bounded_session(session)
            tx.set(jr, job)
            tx.set(sr, session)
        return self._tx(confirm)

    def _expire_unstarted_queue(self, tx, jr, sr, job, session, slots, *, expired=True):
        from app.execution.repository import _Stopped
        from app.execution.slots import release_run, SLOTS_COLLECTION
        ref, item = self._queue_item(session, job["queue_item_id"], tx)
        if item.get("run_id") != job.get("queue_root_run_id", job["id"]) or item["state"] != "dispatched":
            raise LeaseLost("Expired queued turn requires reconciliation.")
        # The leased job has never started. Retain the existing workspace and
        # original transcript; no sandbox cleanup or quota refund is justified.
        if expired:
            item["state"] = "expired"
        job.update(state="cancelled" if expired else "failed", updated_at=self.clock(), stop_reason="queue_stopped_before_start")
        session.update(status="error", queue_ready=False, recovery_required=True,
                       recovery={"reason": "queued_run_not_started", "run_id": job["id"]},
                       error="current queued run stopped before execution; prior work retained for reconciliation")
        release_run(slots, job["id"])
        tx.set(self._ref(SLOTS_COLLECTION, session["uid"]), slots)
        tx.set(ref, item)
        tx.set(jr, job)
        self._write_session(tx, sr, session)
        bounded_session(session)
        return _Stopped("current queued run stopped before execution; prior work retained")

    def queue_candidates(self, limit=20):
        from app.store import SESSIONS_COLLECTION
        items = []
        for snap in self.db.collection(SESSIONS_COLLECTION).where(filter=FieldFilter("queue_ready", "==", True)).limit(limit).stream():
            session = snap.to_dict()
            pending, _ = self._queue_state(session)
            if not pending:
                reject(503, "Ready queue has no pending head.", "queue/history-unavailable")
            item = self._queue_item(session, pending[0])[1]
            items.append({k: item[k] for k in ("id", "uid", "authorized_iat", "identity_exp", "expires_at")} | {"session_id": session["id"]})
        return items

    def dispatch_next(self, session_id, uid, key, identity, *, owner_request=False, expected_task_authorization_id=None):
        from app.execution.repository import RECEIPTS_COLLECTION, _digest
        from app.store import SESSIONS_COLLECTION
        from app.execution.task_authorization import TaskDispatchEvidence
        owner_identity = identity if isinstance(identity, QueueIdentity) else None
        # Resolve a known reply BEFORE consulting a newer head/reader. Replays
        # do not confer fresh authority or consume another user's queued intent.
        def replay(tx):
            session = self._owner_session(session_id, uid, tx)
            if owner_identity is not None:
                identity_valid(owner_identity, uid, self.clock())
                if expected_task_authorization_id is not None and owner_identity.tenant_id is not None:
                    reject(403, "Unsupported task namespace.", "task/namespace-unsupported")
            elif not isinstance(identity, TaskDispatchEvidence) or identity.uid != uid:
                reject(401, "Fresh dispatch identity unavailable.", "queue/authorization-unavailable")
            if not isinstance(key, str) or not 1 <= len(key) <= 200:
                reject(422, "Idempotency-Key must contain 1 to 200 characters", "request/invalid")
            scope = [uid, "queue_dispatch", session_id, key]
            if expected_task_authorization_id is not None:
                scope.append(expected_task_authorization_id)
            receipt = self._read(self._ref(RECEIPTS_COLLECTION, _digest(scope)), tx)
            return receipt["response"] if receipt else None
        known = self._tx(replay)
        if known is not None:
            return known
        if owner_identity is not None:
            self._owner_session(session_id, uid)
            identity_valid(owner_identity, uid, self.clock())
            snapshot = self._owner_session(session_id, uid)
            pending = self._queue_state(snapshot)[0]
            if pending:
                head = self._queue_item(snapshot, pending[0])[1]
                if head.get("task_authorization_id"):
                    if owner_identity.tenant_id is not None:
                        reject(403, "Unsupported task namespace.", "task/namespace-unsupported")
                    identity = self.task_queue_evidence({"session_id": session_id, "uid": uid, "id": head["id"]})
        def check_identity():
            if owner_identity is not None:
                identity_valid(owner_identity, uid, self.clock())
            elif not isinstance(identity, TaskDispatchEvidence):
                reject(401, "Fresh dispatch identity unavailable.", "queue/authorization-unavailable")
        def dispatch(tx):
            session = self._owner_session(session_id, uid, tx)
            check_identity()
            if not isinstance(key, str) or not 1 <= len(key) <= 200:
                reject(422, "Idempotency-Key must contain 1 to 200 characters", "request/invalid")
            request_scope = [uid, "queue_dispatch", session_id, key]
            if expected_task_authorization_id is not None:
                request_scope.append(expected_task_authorization_id)
            request_ref = self._ref(RECEIPTS_COLLECTION, _digest(request_scope))
            receipt = self._read(request_ref, tx)
            if receipt:
                return receipt["response"]
            pending, _ = self._queue_state(session)
            if not pending:
                reject(409, "No queued next turn is pending.", "queue/empty")
            ref, item = self._queue_item(session, pending[0], tx)
            if expected_task_authorization_id is not None and item.get("task_authorization_id") != expected_task_authorization_id:
                reject(409, "Task consent does not bind the current FIFO head.", "task/scope-denied")
            if item["state"] != "queued":
                reject(503, "Queue head requires reconciliation.", "queue/history-unavailable")
            task_grant = None
            if item.get("task_authorization_id"):
                task_grant = self._task_queue_check(tx, session, item, identity, consume=True)
                checked_task_policy = self._task_policy(tx)
                if owner_identity is not None:
                    from app.execution.task_authorization import task_owner_fresh
                    task_owner_fresh(owner_identity, checked_task_policy, self.clock())
            elif isinstance(identity, TaskDispatchEvidence):
                reject(403, "Task consent cannot rebind an older finite queue item.", "task/scope-denied")
            if task_grant is None and not owner_request and (identity.issued_at != item["authorized_iat"] or identity.expires_at != item["identity_exp"]):
                reject(401, "Fresh authorization does not bind the queued identity window.", "queue/authorization-unavailable")
            if item["expires_at"] <= self.clock():
                item["state"] = "expired"
                session["queue_pending"] = pending[1:]
                session["queue_ready"] = bool(session.get("queue_ready") and pending[1:])
                response = {"queuedMessage": projection(item, self.clock()), "dispatched": False}
                tx.set(ref, item)
                self._write_session(tx, self._ref(SESSIONS_COLLECTION, session_id), session, "session.message_dequeued")
                bounded_session(session)
            else:
                if (session.get("status") != "idle" or session.get("cancel_requested") or session.get("recovery_required") or
                        session.get("pending_approval") or session.get("workspace_operation") or session.get("workspace_restore_id")):
                    reject(409, "Queued work is waiting for a safe run boundary.", "queue/session-not-idle")
                message_key = item["id"]
                run_id = _digest([uid, "queue_turn", session_id, message_key])
                def final_check():
                    check_identity()
                    if task_grant is not None:
                        from app.auth_account import require_account
                        require_account(identity.account, uid, task_grant[1]["consent_iat"], self.clock())
                        if owner_identity is not None:
                            task_owner_fresh(owner_identity, checked_task_policy, self.clock())
                    if self.clock() >= item["expires_at"]:
                        reject(409, "Queued execution expiry elapsed during admission.", "queue/expired")
                accepted = self._submit_tx(tx, uid, message_key, "message", {"content": item["prompt"]}, session_id,
                                           queue_item=item, command_scope="queue_turn", final_check=final_check)
                # _submit_tx completed ALL reads. These additional writes join
                # that same commit; no dispatch/dequeue/admission handoff gap.
                item.update(state="dispatched", run_id=run_id)
                tx.set(ref, item)
                if task_grant is not None:
                    tx.set(*task_grant)
                from app.models import SessionOut
                response = {"queuedMessage": projection(item, self.clock()), "dispatched": True,
                            "session": SessionOut(**accepted).model_dump()}
                bounded_session(accepted)
            tx.create(request_ref, {"uid": uid, "response": response, "created_at": self.clock(), "schema_version": 2})
            check_identity()
            if task_grant is not None:
                from app.auth_account import require_account
                require_account(identity.account, uid, task_grant[1]["consent_iat"], self.clock())
                if owner_identity is not None:
                    task_owner_fresh(owner_identity, checked_task_policy, self.clock())
            if response["dispatched"] and self.clock() >= item["expires_at"]:
                reject(409, "Queued execution expiry elapsed during admission.", "queue/expired")
            return response
        return self._tx(dispatch)

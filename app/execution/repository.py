"""Transactional job/session state, independent of model and sandbox providers.

A leased job has not executed effects and can be reclaimed. Once running, an
expired job is failed closed, never replayed: a remote effect may have happened.
This intentionally does not promise exactly-once external execution.
"""
import copy
import hashlib
import json
import re
import time
from dataclasses import dataclass

from google.cloud.firestore_v1 import transactional
from google.cloud.firestore_v1.base_query import FieldFilter

from app.firebase import db
from app.store import SESSIONS_COLLECTION, create_session
from app.execution.slots import read_slots, admit, release_run, release_sandbox
from app.execution.errors import ExecutionStopped, LeaseLost, CancelRequested
from app.execution.limits import bounded_session
from app.execution.workspaces import WorkspaceTransitions, require_unlocked
from app.execution.contracts import ProductContracts
from app.execution.queue import QueueTransitions
from app.execution.task_authorization import TaskAuthorizations
from app.execution.actions import (
    ACTIONS_COLLECTION, BATCHES_COLLECTION, EVIDENCE_COLLECTION, HEX_ID, ASSERTIONS,
    validate_batch, projection,
)

JOBS_COLLECTION = "agent_v2_jobs"
RECEIPTS_COLLECTION = "agent_v2_commands"
LEASE_SECONDS = 45
CANCEL_GRACE_SECONDS = 15
MAX_RUN_SECONDS = 900
CLEANUP_COLLECTION = "agent_v2_cleanup"
EVENTS_COLLECTION = "agent_v2_events"
_CURSOR = re.compile(r"([0-9]{1,20})-([0-9]{1,20})\Z")
_EVENT_TYPES = {
    "queued": "session.queued", "running": "session.updated", "idle": "session.idle",
    "awaiting_approval": "session.awaiting_approval", "cancel_requested": "session.cancel_requested",
    "cancelled": "session.cancelled", "error": "session.failed",
}


class CommandError(Exception):
    def __init__(self, status: int, message: str, *, code="command/rejected"):
        super().__init__(message)
        self.status, self.code = status, code


@dataclass(frozen=True)
class _Stopped:
    reason: str


@dataclass(frozen=True)
class Claim:
    job_id: str
    session_id: str
    owner: str
    token: int


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class RunRepository(TaskAuthorizations, QueueTransitions, ProductContracts, WorkspaceTransitions):
    def __init__(self, client=db, clock=time.time, transaction_runner=None, *, account_reader=None):
        self.db, self.clock = client, clock
        self.account_reader = account_reader
        # Injectable serializable adapter for offline tests. Production always
        # uses the real Firestore transaction/retry decorator.
        self.transaction_runner = transaction_runner

    def _tx(self, fn):
        if self.transaction_runner:
            return self.transaction_runner(fn)
        return transactional(fn)(self.db.transaction())

    def _ref(self, collection, key):
        return self.db.collection(collection).document(key)

    @staticmethod
    def _read(ref, tx):
        snap = ref.get(transaction=tx)
        return snap.to_dict() if snap.exists else None

    def _revoke(self, tx, jr, sr, job, session, *, cancelled, reason):
        self._queue_cleanup(tx, session["id"])
        job.update(state="cancelled" if cancelled else "interrupted", updated_at=self.clock())
        session.update(status="cancelled" if cancelled else "error", recovery_required=True,
                       pending_approval=None, updated_at=_iso(self.clock()), error=reason)
        recovery = {"reason": "cancellation_grace_expired" if cancelled else "run_deadline_reached"}
        session["recovery"] = recovery
        job["recovery"] = recovery
        tx.set(jr, job)
        self._write_session(tx, sr, session)

    def _authority_tx(self, claim, operation, *, allow_cancel=False, completed_ok=False, require_task=True):
        """Check and revoke in the same transaction as the requested transition.

        Return a stop outcome so cleanup/state/event writes COMMIT before raising.
        All callers share deadline/cancel-grace semantics; no action is renewed
        just because the previous heartbeat ran shortly before the deadline.
        """
        task_evidence = self._task_run_evidence(claim) if require_task else None
        def execute(tx):
            if completed_ok:
                existing = self._read(self._ref(JOBS_COLLECTION, claim.job_id), tx)
                if (existing and existing.get("owner") == claim.owner and existing.get("fence") == claim.token
                        and existing["state"] in {"succeeded", "waiting_for_approval", "cancelled", "failed"}):
                    return
            jr, sr, job, session = self._owned(tx, claim, allow_cancel=True)
            now = self.clock()  # Recomputed inside every retried transaction.
            cancel_expired = (session.get("cancel_requested") and
                              now >= session.get("cancel_requested_at", now) + CANCEL_GRACE_SECONDS)
            deadline_expired = now >= job.get("run_deadline", float("inf"))
            if cancel_expired or deadline_expired:
                reason = "execution deadline reached; in-flight action outcome requires reconciliation"
                self._revoke(tx, jr, sr, job, session, cancelled=bool(cancel_expired), reason=reason)
                return _Stopped(reason)
            if session.get("cancel_requested") and not allow_cancel:
                raise CancelRequested("cancellation requested")
            grant = self._task_run_check(tx, job, session, task_evidence) if require_task else None
            result = operation(tx, jr, sr, job, session)
            if grant is not None:
                from app.auth_account import require_account
                require_account(task_evidence, session["uid"], grant["consent_iat"], self.clock())
                if self.clock() >= grant["scope"]["expires_at"]:
                    raise LeaseLost("task authorization expired during operation")
                tx.set(jr, job)
            return result

        result = self._tx(execute)
        if isinstance(result, _Stopped):
            raise LeaseLost(result.reason)
        return result

    def _write_session(self, tx, ref, session, event_type=None):
        """One commit boundary for session state and its bounded public event.

        This has no reads: callers finish authorization/fencing reads first.
        Prompts, model content, tool payloads and connector data stay out of events.
        """
        sequence = session.get("event_sequence", 0) + 1
        milliseconds = max(int(self.clock() * 1000), session.get("event_time_ms", 0))
        session.update(event_sequence=sequence, event_time_ms=milliseconds)
        event = {"id": f"{milliseconds}-{sequence}", "sequence": sequence,
                 "session_id": session["id"], "uid": session["uid"],
                 "type": event_type or _EVENT_TYPES[session["status"]],
                 "data": {"sessionId": session["id"], "status": session["status"],
                          "revision": session["revision"],
                          "recoveryRequired": session.get("recovery_required") is True}}
        tx.create(self._ref(EVENTS_COLLECTION, f"{session['id']}:{sequence}"), event)
        tx.set(ref, session)

    def event_history(self, session_id, uid, after=None, limit=100):
        """Bounded ordered replay, authorized before inspecting any cursor."""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise CommandError(422, "event page limit must be between 1 and 100")
        snapshot = self._ref(SESSIONS_COLLECTION, session_id).get()
        session = snapshot.to_dict() if snapshot.exists else None
        if not session or session["uid"] != uid:
            raise CommandError(404, "session not found")
        sequence = 0
        if after is not None:
            cursor = _CURSOR.fullmatch(after)
            if not cursor:
                raise CommandError(422, "invalid Last-Event-ID")
            sequence = int(cursor[2])
            if sequence > session.get("event_sequence", 0):
                raise CommandError(409, "event cursor is ahead of this session")
            event = self._ref(EVENTS_COLLECTION, f"{session_id}:{sequence}").get().to_dict()
            if not event or event["id"] != after:
                raise CommandError(409, "event cursor is not available for this session")
        query = self.db.collection(EVENTS_COLLECTION).where(
            filter=FieldFilter("session_id", "==", session_id)).where(
            filter=FieldFilter("sequence", ">", sequence)).where(
            filter=FieldFilter("sequence", "<=", session.get("event_sequence", 0))).order_by("sequence").limit(limit)
        events = [doc.to_dict() for doc in query.stream()]
        expected = range(sequence + 1, min(session.get("event_sequence", 0), sequence + limit) + 1)
        if ([event.get("sequence") for event in events] != list(expected) or
                any(event.get("uid") != uid for event in events)):
            raise CommandError(503, "event history requires reconciliation")
        return [{key: event[key] for key in ("id", "type", "data")} for event in events]

    def _selection(self, tx, session, pending=None):
        from app.billing.model_select import validate_selection, selected_model
        from app.billing.entitlements import resolve_entitlements
        self._project_execution(tx, session)
        validate_selection(session, pending)
        if session.get("model") is not None or pending is not None:
            return selected_model(session, resolve_entitlements(session["uid"], client=self.db,
                                  transaction=tx, now=self.clock()), pending)

    def prepare_actions(self, claim, *, step, calls):
        validate_batch(calls)
        if type(step) is not int or not 0 <= step <= 1_000_000:
            raise LeaseLost("invalid action step")
        # Reuse the live registry and risk policy, including disabled tools.
        from app.agent.tools import tool_spec, validate_tool_input
        from app.agent.risk import is_risky
        sealed = []
        batch_id = _digest([claim.session_id, claim.job_id, step])
        for ordinal, call in enumerate(calls):
            spec = tool_spec(call["name"])
            validate_tool_input(spec, call["input"])
            sealed.append({"id": _digest([batch_id, ordinal]), "call_id": call["id"],
                           "name": spec.name, "input_digest": _digest(call["input"]),
                           "risky": is_risky(spec.name, call["input"]), "effectful": spec.effectful})

        def prepare(tx, jr, sr, job, session):
            from app.agent.policy import require_tool
            require_unlocked(session)
            self._selection(tx, session)
            for call in calls:
                require_tool(session, tool_spec(call["name"]))
                if job.get("task_authorization_id") and call["name"] not in job.get("task_allowed_tools", []):
                    raise LeaseLost("tool exceeds task authorization")
            if job["state"] != "running" or session.get("pending_approval"):
                raise LeaseLost("cannot prepare actions in this run state")
            ref = self._ref(BATCHES_COLLECTION, batch_id)
            existing = self._read(ref, tx)
            if existing:
                if existing["calls"] != sealed:
                    raise LeaseLost("action batch input changed")
                return {"batch_id": batch_id, "action_ids": [c["id"] for c in sealed], "session": session}
            if job.get("open_actions"):
                raise LeaseLost("an action outcome requires reconciliation")
            # Read every create target before writing, so a missing/corrupt
            # batch cannot overwrite historical action receipts.
            refs = [self._ref(ACTIONS_COLLECTION, c["id"]) for c in sealed]
            if any([self._read(ref, tx) is not None for ref in refs]):
                raise LeaseLost("action history requires reconciliation")
            now = self.clock()
            head = session.get("action_sequence", 0)
            tx.create(ref, {"id": batch_id, "uid": job["uid"], "session_id": claim.session_id,
                            "origin_run_id": claim.job_id, "step": step, "calls": sealed})
            for ordinal, (call, action_ref) in enumerate(zip(sealed, refs)):
                tx.create(action_ref, {**call, "batch_id": batch_id, "uid": job["uid"],
                          "session_id": claim.session_id, "origin_run_id": claim.job_id,
                          "ordinal": ordinal, "sequence": head + ordinal + 1, "state": "prepared",
                          "version": 1, "created_at": now, "evidence_sequence": 0})
            job["action_batch_id"] = batch_id
            session.update(action_sequence=head + len(sealed), revision=session["revision"] + 1)
            bounded_session(session)
            tx.set(jr, job)
            self._write_session(tx, sr, session)
            return {"batch_id": batch_id, "action_ids": [c["id"] for c in sealed], "session": session}
        return self._authority_tx(claim, prepare)

    def _action(self, tx, job, session, action_id):
        if not isinstance(action_id, str) or not HEX_ID.fullmatch(action_id):
            raise LeaseLost("sealed action identity required")
        ref = self._ref(ACTIONS_COLLECTION, action_id)
        item = self._read(ref, tx)
        if (not item or item["session_id"] != session["id"] or item["uid"] != session["uid"] or
                item["batch_id"] != job.get("action_batch_id")):
            raise LeaseLost("action does not belong to this run")
        if item["origin_run_id"] != job["id"]:
            pending = session.get("pending_approval")
            if (job["operation"] != "approve" or not pending or
                    pending.get("batch_id") != item["batch_id"]):
                raise LeaseLost("action approval is not current")
        return ref, item

    def permit_action(self, claim, *, action_id, name, input):
        def permit(tx, jr, sr, job, session):
            from app.agent.tools import tool_spec
            from app.agent.policy import require_tool
            require_unlocked(session)
            self._selection(tx, session, session.get("pending_approval"))
            require_tool(session, tool_spec(name))
            if job.get("task_authorization_id") and name not in job.get("task_allowed_tools", []):
                raise LeaseLost("tool exceeds task authorization")
            ref, item = self._action(tx, job, session, action_id)
            if job["state"] != "running" or item["name"] != name or item["input_digest"] != _digest(input):
                raise LeaseLost("action input changed")
            if item["state"] != "prepared":
                raise LeaseLost("action already permitted; inspect its receipt")
            if job.get("open_actions"):
                raise LeaseLost("an action outcome requires reconciliation")
            if item["risky"]:
                decisions = job.get("payload", {}).get("decisions", [])
                if (job["operation"] != "approve" or job.get("approval_batch_id") != item["batch_id"] or
                        not any(d["call_id"] == item["call_id"] and d["approved"] is True for d in decisions)):
                    raise LeaseLost("action requires its sealed approval")
            item.update(state="outcome_unknown", version=item["version"] + 1,
                        permit={"run_id": claim.job_id, "owner": claim.owner, "fence": claim.token,
                                "issued_at": self.clock(), "lease_expires_at": job["lease_expires_at"],
                                "run_deadline": job["run_deadline"]})
            job["open_actions"] = [*job.get("open_actions", []), action_id]
            if item["effectful"]:
                session["workspace_generation"] = session.get("workspace_generation", 0) + 1
                bounded_session(session)
                tx.set(sr, session)
            tx.set(ref, item)
            tx.set(jr, job)
        return self._authority_tx(claim, permit)

    def record_action_result(self, claim, *, action_id, result_digest):
        if not isinstance(result_digest, str) or not HEX_ID.fullmatch(result_digest):
            raise LeaseLost("invalid action result digest")
        def record(tx, jr, sr, job, session):
            ref, item = self._action(tx, job, session, action_id)
            permit = item.get("permit", {})
            if (permit.get("run_id"), permit.get("owner"), permit.get("fence")) != (claim.job_id, claim.owner, claim.token):
                raise LeaseLost("action result fence changed")
            if item["state"] == "handler_returned":
                if item["result_digest"] != result_digest:
                    raise LeaseLost("action result conflicts with its receipt")
                return
            if item["state"] != "outcome_unknown" or action_id not in job.get("open_actions", []):
                raise LeaseLost("action was not permitted")
            item.update(state="handler_returned", result_digest=result_digest, returned_at=self.clock(),
                        version=item["version"] + 1)
            job["open_actions"].remove(action_id)
            tx.set(ref, item)
            tx.set(jr, job)
        return self._authority_tx(claim, record)

    def _owner_session(self, session_id, uid, tx=None):
        from app.execution.contracts import require_id
        require_id(session_id, "session")
        ref = self._ref(SESSIONS_COLLECTION, session_id)
        if tx is None:
            snap = ref.get()
            session = snap.to_dict() if snap.exists else None
        else:
            session = self._read(ref, tx)
        if not session or session["uid"] != uid:
            raise CommandError(404, "session not found")
        return session

    @staticmethod
    def _page(after, limit, head):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise CommandError(422, "page limit must be between 1 and 100")
        if after is None:
            return 0
        if not isinstance(after, str) or not re.fullmatch(r"[0-9]{1,16}", after):
            raise CommandError(422, "invalid receipt cursor")
        sequence = int(after)
        if sequence > head:
            raise CommandError(409, "receipt cursor is ahead of history")
        return sequence

    def action_history(self, session_id, uid, after=None, limit=50):
        session = self._owner_session(session_id, uid)
        head = session.get("action_sequence", 0)
        sequence = self._page(after, limit, head)
        query = self.db.collection(ACTIONS_COLLECTION).where(filter=FieldFilter("session_id", "==", session_id)).where(
            filter=FieldFilter("sequence", ">", sequence)).where(filter=FieldFilter("sequence", "<=", head)).order_by("sequence").limit(limit)
        items = [snap.to_dict() for snap in query.stream()]
        if ([item.get("sequence") for item in items] != list(range(sequence + 1, min(head, sequence + limit) + 1)) or
                any(item.get("uid") != uid for item in items)):
            raise CommandError(503, "action history requires reconciliation")
        return {"actions": [projection(item) for item in items],
                "next_cursor": str(items[-1]["sequence"]) if items and items[-1]["sequence"] < head else None}

    def _owner_action(self, session_id, uid, action_id, tx=None):
        self._owner_session(session_id, uid, tx)
        if not isinstance(action_id, str) or not HEX_ID.fullmatch(action_id):
            raise CommandError(404, "action not found")
        ref = self._ref(ACTIONS_COLLECTION, action_id)
        if tx is None:
            snap = ref.get()
            item = snap.to_dict() if snap.exists else None
        else:
            item = self._read(ref, tx)
        if not item or item["session_id"] != session_id or item["uid"] != uid:
            raise CommandError(404, "action not found")
        return ref, item

    def inspect_action(self, session_id, uid, action_id, after=None, limit=50):
        _, item = self._owner_action(session_id, uid, action_id)
        head = item.get("evidence_sequence", 0)
        sequence = self._page(after, limit, head)
        query = self.db.collection(EVIDENCE_COLLECTION).where(filter=FieldFilter("action_id", "==", action_id)).where(
            filter=FieldFilter("sequence", ">", sequence)).where(filter=FieldFilter("sequence", "<=", head)).order_by("sequence").limit(limit)
        evidence = [snap.to_dict() for snap in query.stream()]
        if ([e.get("sequence") for e in evidence] != list(range(sequence + 1, min(head, sequence + limit) + 1)) or
                any(e.get("uid") != uid or e.get("session_id") != session_id for e in evidence)):
            raise CommandError(503, "action evidence requires reconciliation")
        return {"action": projection(item), "evidence": [{k: e[k] for k in ("id", "session_id", "action_id", "sequence", "assertion", "evidence_sha256",
                                                       "verification", "created_at", "previous_version", "version")} for e in evidence],
                "next_cursor": str(evidence[-1]["sequence"]) if evidence and evidence[-1]["sequence"] < head else None}

    def record_action_evidence(self, session_id, uid, action_id, key, payload):
        # Ownership/action scope are checked inside the transaction BEFORE key,
        # payload validation or command replay. Bearer auth does not prove human.
        def attest(tx):
            action_ref, item = self._owner_action(session_id, uid, action_id, tx)
            if not isinstance(key, str) or not 1 <= len(key) <= 200:
                raise CommandError(422, "Idempotency-Key must contain 1 to 200 characters")
            if (not isinstance(payload, dict) or set(payload) != {"expected_version", "assertion", "evidence_sha256"} or
                    type(payload["expected_version"]) is not int or not 1 <= payload["expected_version"] <= 2**63 - 1 or
                    not isinstance(payload["assertion"], str) or payload["assertion"] not in ASSERTIONS or
                    not isinstance(payload["evidence_sha256"], str) or not HEX_ID.fullmatch(payload["evidence_sha256"])):
                raise CommandError(422, "invalid reconciliation evidence")
            command_id = _digest([uid, "action_evidence", session_id, action_id, key])
            receipt_ref = self._ref(RECEIPTS_COLLECTION, command_id)
            receipt = self._read(receipt_ref, tx)
            fingerprint = _digest(payload)
            if receipt:
                if receipt["fingerprint"] != fingerprint:
                    raise CommandError(409, "idempotency key was used with different input")
                return receipt["response"]
            if payload["expected_version"] != item["version"]:
                raise CommandError(409, "action version changed; inspect before recording evidence")
            sequence = item.get("evidence_sequence", 0) + 1
            record = {"id": command_id, "uid": uid, "session_id": session_id, "action_id": action_id,
                      "sequence": sequence, "assertion": payload["assertion"], "evidence_sha256": payload["evidence_sha256"],
                      "verification": "unverified_owner_attestation", "created_at": self.clock(),
                      "previous_version": item["version"], "version": item["version"] + 1}
            response = {k: v for k, v in record.items() if k != "uid"}
            tx.create(self._ref(EVIDENCE_COLLECTION, command_id), record)
            item.update(version=record["version"], evidence_sequence=sequence)
            tx.set(action_ref, item)
            tx.create(receipt_ref, {"uid": uid, "fingerprint": fingerprint, "response": response,
                                    "created_at": self.clock(), "schema_version": 2})
            return response
        return self._tx(attest)

    def _validate_pending(self, tx, job, pending):
        if not pending or pending.get("batch_id") != job.get("action_batch_id"):
            raise LeaseLost("approval requires a sealed action batch")
        batch = self._read(self._ref(BATCHES_COLLECTION, pending["batch_id"]), tx)
        calls = pending.get("calls", [])
        validate_batch([{k: c[k] for k in ("id", "name", "input")} for c in calls])
        if not batch or len(batch["calls"]) != len(calls):
            raise LeaseLost("approval batch changed")
        for call, sealed in zip(calls, batch["calls"]):
            if (call.get("action_id"), call["id"], call["name"], _digest(call["input"]), call.get("risky")) != (
                    sealed["id"], sealed["call_id"], sealed["name"], sealed["input_digest"], sealed["risky"]):
                raise LeaseLost("approval action input changed")

    def submit(self, uid: str, key: str, operation: str, payload: dict, session_id=None) -> dict:
        approval_evidence = None
        if operation == "approve":
            current = self._owner_session(session_id, uid)
            if current.get("task_authorization_id"):
                command = self._ref(RECEIPTS_COLLECTION, _digest([uid, operation, session_id, key])).get()
                if not command.exists:
                    prior = self._ref(JOBS_COLLECTION, current["active_run_id"]).get().to_dict()
                    if not prior:
                        raise CommandError(409, "Approval authority requires reconciliation.")
                    approval_evidence = self._task_run_evidence(Claim(prior["id"], session_id, prior["owner"], prior["fence"]))
        return self._tx(lambda tx: self._submit_tx(tx, uid, key, operation, payload, session_id, approval_evidence=approval_evidence))

    def _submit_tx(self, tx, uid, key, operation, payload, session_id=None, *, queue_item=None, command_scope=None, final_check=None, approval_evidence=None):
        """Commit receipt + session transition + job together; no handoff gap."""
        if not key or len(key) > 200:
            raise CommandError(422, "Idempotency-Key must contain 1 to 200 characters")
        command_id = _digest([uid, command_scope or operation, session_id, key])
        fingerprint = _digest(payload)
        receipt_ref = self._ref(RECEIPTS_COLLECTION, command_id)
        job_ref = self._ref(JOBS_COLLECTION, command_id)
        sid = session_id or command_id
        session_ref = self._ref(SESSIONS_COLLECTION, sid)
        now = self.clock()

        current = self._read(session_ref, tx)
        if operation != "create" and (not current or current["uid"] != uid):
            raise CommandError(404, "session not found")
        project_binding = None
        if operation == "create" and payload.get("project_id") is not None:
            # Owner check precedes receipt/policy details even on replay.
            self._owner_project(payload["project_id"], uid, tx)
        receipt = self._read(receipt_ref, tx)
        if receipt:
            if queue_item is not None:
                raise CommandError(503, "Queue command receipt already exists; reconcile the pending head.")
            if receipt["fingerprint"] != fingerprint:
                raise CommandError(409, "idempotency key was used with different input")
            return receipt["response"]
        if queue_item is not None and self._read(job_ref, tx) is not None:
            raise CommandError(503, "Queue run already exists without its paired receipt; reconcile the pending head.")
        if operation == "create":
            if current:
                raise CommandError(409, "session already exists")
            from app.models import SessionCreate
            from app.billing.model_select import resolve_model, reject_effort, require_model_kind
            from app.billing.entitlements import resolve_entitlements
            parsed = SessionCreate.model_validate(payload)
            if parsed.project_id is not None and parsed.kind != "code":
                raise CommandError(422, "Project sessions require the verified web/code contract.")
            if parsed.project_id is not None and parsed.model is None:
                raise CommandError(422, "Project sessions require an explicit model.")
            session = create_session(parsed.task, parsed.kind, uid)
            session.update(id=sid, mode=parsed.mode, transcript_version=1, message_count=0)
            if parsed.project_id is not None:
                project_binding = self._bind_project(tx, parsed.project_id, uid, sid, parsed.model)
                session["project_id"] = parsed.project_id
            if "effort" in payload:
                reject_effort(parsed.effort)
            if "model" in payload:
                choice = resolve_model(resolve_entitlements(uid, client=self.db, transaction=tx, now=now), parsed.model)
                require_model_kind(choice, parsed.kind)
                session.update(model=choice.model, provider=choice.provider)
        else:
            if current is None or current["uid"] != uid:
                raise CommandError(404, "session not found")
            session = current
            if session.get("project_id") is not None:
                _, project = self._owner_project(session["project_id"], uid, tx)
                if project.get("active_session_id") != sid:
                    raise CommandError(409, "project session authority changed")
            if session.get("workspace_operation") or session.get("workspace_restore_id"):
                raise CommandError(409, "workspace requires completion or reconciliation")
            self._selection(tx, session, session.get("pending_approval"))
            active_job = self._read(self._ref(JOBS_COLLECTION, session["active_run_id"]), tx)
            if active_job and active_job["state"] in {"queued", "leased", "running"}:
                raise CommandError(409, "current run is still active")
            if session.get("recovery_required"):
                raise CommandError(409, "interrupted execution requires reconciliation; start a new session")
            if operation == "message":
                if queue_item is None and session.get("queue_pending"):
                    raise CommandError(409, "Pending FIFO messages must be dispatched or cancelled first.")
                if queue_item is not None:
                    if (not session.get("queue_pending") or session["queue_pending"][0] != queue_item["id"] or
                            not active_job or active_job.get("completion_verified") is not True or
                            active_job.get("state") != "succeeded" or active_job.get("open_actions") or
                            session.get("pending_approval")):
                        raise CommandError(409, "Prior run is not verified quiescent for FIFO dispatch.")
                    session["queue_pending"] = session["queue_pending"][1:]
                    session["queue_ready"] = False
                    session["queue_turn_expires_at"] = queue_item["expires_at"]
                    for field in ("task_authorization_id", "task_authorization_version"):
                        if field in queue_item:
                            session[field] = queue_item[field]
                        else:
                            session.pop(field, None)
                    session.pop("task_root_deadline", None)
                else:
                    session.pop("queue_turn_expires_at", None)
                    for field in ("task_authorization_id", "task_authorization_version", "task_root_deadline", "task_allowed_tools"):
                        session.pop(field, None)
                if session["status"] != "idle":
                    raise CommandError(409, "session must be idle before another turn")
                from app.models import MessageIn
                parsed = MessageIn.model_validate(payload)
                if "mode" in payload:
                    session["mode"] = parsed.mode
                session["messages"].append({"role": "user", "content": [{"type": "text", "text": parsed.content}]})
            elif operation == "approve":
                if session.get("task_authorization_id"):
                    if not active_job or self.clock() >= session.get("task_root_deadline", 0):
                        raise CommandError(409, "Original task execution deadline expired.")
                    approval_grant = self._task_run_check(tx, active_job, session, approval_evidence)
                if self.clock() >= session.get("queue_turn_expires_at", float("inf")):
                    raise CommandError(409, "Queued turn authorization expired; approval cannot renew it.")
                pending = session.get("pending_approval")
                if session["status"] != "awaiting_approval" or not pending:
                    raise CommandError(409, "no pending approval on this session")
                if payload["approval_id"] != pending["id"]:
                    raise CommandError(409, "approval batch is no longer current")
                if not active_job:
                    raise CommandError(409, "approval history requires reconciliation")
                try:
                    self._validate_pending(tx, active_job, pending)
                except LeaseLost:
                    raise CommandError(409, "approval history requires reconciliation") from None
                risky = {c["id"] for c in pending["calls"] if c["risky"]}
                ids = [d["call_id"] for d in payload["decisions"]]
                if len(ids) != len(set(ids)) or set(ids) != risky:
                    raise CommandError(400, "decide exactly once on every risky pending call")
            else:
                raise ValueError("unknown command")
        charges = []
        if session.get("project_id") and operation in {"create", "message"}:
            from app.billing.entitlements import resolve_entitlements
            entitlement = resolve_entitlements(uid, client=self.db, transaction=tx, now=self.clock())
            registry = self._read(self._ref("config", "registry"), tx)
            if entitlement.meter_limits is not None or registry is not None or queue_item is not None:
                from app.billing.budgets import BudgetLedger, BudgetExceeded, CounterUnavailable
                meter_key, reset_period, hard_limit = self._build_meter(tx, uid)
                try:
                    charges = BudgetLedger(self.db, clock=self.clock).prepare_counter(tx, uid, meter_key=meter_key,
                        reset_period=reset_period, hard_limit=hard_limit, run_id=command_id)
                except CounterUnavailable:
                    raise CommandError(503, "Build counter requires reconciliation.", code="session/build-unavailable") from None
                except BudgetExceeded:
                    raise CommandError(429, "Configured build allowance exhausted.", code="session/build-unavailable") from None
        slot_ref, slots = read_slots(self.db, tx, uid)
        rejection = admit(slots, command_id, sid, creating=operation == "create")
        if rejection:
            raise CommandError(429, rejection)
        if final_check is not None:
            final_check()
        if operation == "approve" and self.clock() >= session.get("queue_turn_expires_at", float("inf")):
            raise CommandError(409, "Queued turn authorization expired; approval cannot renew it.")
        if operation == "approve" and session.get("task_authorization_id"):
            from app.auth_account import require_account
            require_account(approval_evidence, uid, approval_grant["consent_iat"], self.clock())
            if self.clock() >= session["task_root_deadline"]:
                raise CommandError(409, "Original task execution deadline expired.")
        tx.set(slot_ref, slots)
        session.update(status="queued", active_run_id=command_id, cancel_requested=False,
                       error=None, schema_version=2, updated_at=_iso(now),
                       revision=session.get("revision", 0) + 1)
        bounded_session(session)
        job = {"id": command_id, "session_id": sid, "uid": uid, "schema_version": 2,
               "operation": operation, "payload": payload, "state": "queued",
               "fence": 0, "created_at": now, "updated_at": now}
        if operation in {"message", "approve"} and session.get("task_authorization_id"):
            job.update(task_authorization_id=session["task_authorization_id"], task_authorization_version=session["task_authorization_version"])
        if queue_item is not None:
            job.update(queue_item_id=queue_item["id"], queue_expires_at=queue_item["expires_at"], queue_root_run_id=command_id)
        if operation == "approve" and "queue_turn_expires_at" in session:
            job.update(queue_item_id=active_job.get("queue_item_id"), queue_expires_at=session["queue_turn_expires_at"],
                       queue_root_run_id=active_job.get("queue_root_run_id", active_job["id"]))
        if operation == "approve":
            job["action_batch_id"] = session["pending_approval"]["batch_id"]
            job["approval_batch_id"] = session["pending_approval"]["batch_id"]
        if project_binding is not None:
            tx.set(*project_binding)
        if operation in {"create", "message"}:
            self._append_transcript(tx, session, session["messages"] if operation == "create" else session["messages"][-1:])
        # A response snapshot makes lost-response retries deterministic.
        # Receipts have no automatic expiry: deleting them permits key reuse.
        self._write_session(tx, session_ref, session)
        for charge_ref, data in charges:
            tx.set(charge_ref, data)
        tx.set(job_ref, job)
        tx.set(receipt_ref, {"uid": uid, "fingerprint": fingerprint,
                             "response": session, "created_at": now, "schema_version": 2})
        if operation == "approve" and session.get("task_authorization_id"):
            from app.auth_account import require_account
            require_account(approval_evidence, uid, approval_grant["consent_iat"], self.clock())
        return session

    def _queue_cleanup(self, tx, session_id):
        ref = self._ref(CLEANUP_COLLECTION, session_id)
        if self._read(ref, tx) is None:
            tx.set(ref, {"session_id": session_id, "state": "queued", "next_attempt": self.clock(),
                         "fence": 0, "attempts": 0})

    def claim(self, job_id: str, owner: str) -> Claim | None:
        now = self.clock()
        job_ref = self._ref(JOBS_COLLECTION, job_id)

        def claim_job(tx):
            job = self._read(job_ref, tx)
            if not job:
                return None
            session_ref = self._ref(SESSIONS_COLLECTION, job["session_id"])
            session = self._read(session_ref, tx)
            if not session or session.get("active_run_id") != job_id:
                return None
            if job["state"] == "running" and job["lease_expires_at"] <= now:
                self._queue_cleanup(tx, session["id"])
                # The prior worker might have performed effects. Never retry.
                cancelled = session.get("cancel_requested", False)
                session.update(status="cancelled" if cancelled else "error", pending_approval=None,
                               recovery_required=True, updated_at=_iso(now),
                               error="worker interrupted; action outcome requires reconciliation")
                job.update(state="cancelled" if cancelled else "interrupted", updated_at=now)
                self._write_session(tx, session_ref, session)
                tx.set(job_ref, job)
                return None
            eligible = job["state"] == "queued" or (
                job["state"] == "leased" and job["lease_expires_at"] <= now)
            if not eligible:
                return None
            if session.get("cancel_requested"):
                slot_ref, slots = read_slots(self.db, tx, job["uid"])
                release_run(slots, job_id)
                tx.set(slot_ref, slots)
                session.update(status="cancelled", pending_approval=None, updated_at=_iso(now))
                job.update(state="cancelled", updated_at=now)
                self._write_session(tx, session_ref, session)
                tx.set(job_ref, job)
                return None
            token = job["fence"] + 1
            job.update(state="leased", owner=owner, fence=token,
                       lease_expires_at=now + LEASE_SECONDS, updated_at=now)
            tx.set(job_ref, job)
            return Claim(job_id, job["session_id"], owner, token)
        return self._tx(claim_job)

    def _owned(self, tx, claim: Claim, allow_cancel=False):
        job_ref = self._ref(JOBS_COLLECTION, claim.job_id)
        session_ref = self._ref(SESSIONS_COLLECTION, claim.session_id)
        job, session = self._read(job_ref, tx), self._read(session_ref, tx)
        if (not job or not session or job.get("owner") != claim.owner or
                job.get("fence") != claim.token or job["state"] not in {"leased", "running"} or
                job.get("lease_expires_at", 0) <= self.clock() or
                session.get("active_run_id") != claim.job_id):
            raise LeaseLost("worker no longer owns this run")
        if session.get("cancel_requested") and not allow_cancel:
            raise CancelRequested("cancellation requested")
        return job_ref, session_ref, job, session

    def start(self, claim: Claim) -> tuple[dict, dict]:
        def start_job(tx, jr, sr, job, session):
            require_unlocked(session)
            if job["state"] != "leased":
                raise LeaseLost("run already started")
            _, slots = read_slots(self.db, tx, job["uid"])
            if claim.job_id not in slots["runs"] or claim.session_id not in slots["sandboxes"]:
                raise LeaseLost("run requires admission-slot reconciliation")
            if self.clock() >= min(job.get("queue_expires_at", float("inf")), session.get("task_root_deadline", float("inf"))):
                return self._expire_unstarted_queue(tx, jr, sr, job, session, slots)
            self._selection(tx, session, session.get("pending_approval"))
            if job.get("task_authorization_id") and "task_root_deadline" not in session:
                session["task_root_deadline"] = min(self.clock() + job["task_run_seconds_limit"], job["queue_expires_at"])
            job.update(state="running", updated_at=self.clock(), run_deadline=min(
                self.clock() + MAX_RUN_SECONDS, job.get("queue_expires_at", float("inf")), session.get("task_root_deadline", float("inf"))))
            session.update(status="running", updated_at=_iso(self.clock()))
            tx.set(jr, job)
            self._write_session(tx, sr, session, "session.running")
            return job, session
        return self._authority_tx(claim, start_job)

    def heartbeat(self, claim: Claim):
        def beat(tx, jr, sr, job, session):
            job.update(lease_expires_at=self.clock() + LEASE_SECONDS, updated_at=self.clock())
            tx.set(jr, job)
            return job["lease_expires_at"]
        return self._authority_tx(claim, beat, allow_cancel=True)

    def checkpoint(self, claim: Claim):
        # Existing provider-spend checkpoints now also honor current project
        # execution denies. Failed finish/cancel/cleanup do not use this gate.
        self._authority_tx(claim, lambda tx, jr, sr, job, session: self._project_execution(tx, session))

    def save(self, claim: Claim, session: dict):
        def save(tx, jr, sr, job, current):
            require_unlocked(current)
            if session.get("revision") != current.get("revision"):
                raise LeaseLost("session revision changed")
            if session.get("active_run_id") != claim.job_id:
                raise LeaseLost("session belongs to a different run")
            if session.get("id") != current["id"] or session.get("uid") != current["uid"]:
                raise LeaseLost("session identity changed")
            from app.billing.model_select import validate_selection
            validate_selection(session, session.get("pending_approval"))
            if any(session.get(key) != current.get(key) for key in ("model", "provider", "effort", "mode", "kind")):
                raise LeaseLost("session execution selection changed")
            if session["status"] not in {"running", "idle", "awaiting_approval"}:
                raise ValueError("invalid worker session state")
            if session["status"] in {"idle", "awaiting_approval"} and job.get("open_actions"):
                raise LeaseLost("action outcome requires reconciliation")
            pending = session.get("pending_approval")
            if session["status"] == "awaiting_approval" and pending is None:
                raise LeaseLost("approval requires a sealed action batch")
            if pending is not None:
                self._validate_pending(tx, job, pending)
                if current.get("pending_approval") is not None and pending != current["pending_approval"]:
                    raise LeaseLost("pending approval changed")
            elif current.get("pending_approval") is not None and job["operation"] != "approve":
                raise LeaseLost("pending approval cannot be cleared by this run")
            if session["status"] == "idle" and pending is not None:
                raise LeaseLost("pending approval cannot publish completion")
            slot_ref, slots = read_slots(self.db, tx, job["uid"])
            bounded_session(session)
            updated = copy.deepcopy(session)
            self._worker_transcript(tx, current, updated)
            updated.update(updated_at=_iso(self.clock()), revision=current["revision"] + 1,
                           event_sequence=current.get("event_sequence", 0),
                           event_time_ms=current.get("event_time_ms", 0),
                           action_sequence=current.get("action_sequence", 0))
            # Workspace authority stays parent-owned, like action_sequence.
            for field in ("workspace_generation", "workspace_checkpoint_id", "workspace_operation", "workspace_restore_id",
                          "queue_pending", "queue_sequence", "queue_ready", "queue_turn_expires_at",
                          "task_authorization_id", "task_authorization_version", "task_root_deadline", "task_allowed_tools"):
                if field in current:
                    updated[field] = current[field]
                else:
                    updated.pop(field, None)
            bounded_session(updated)
            self._write_session(tx, sr, updated)
            if updated["status"] in {"idle", "awaiting_approval"}:
                release_run(slots, claim.job_id)
                tx.set(slot_ref, slots)
                job.update(state="succeeded" if updated["status"] == "idle" else "waiting_for_approval",
                           updated_at=self.clock())
                tx.set(jr, job)
            return updated
        updated = self._authority_tx(claim, save)
        session.pop("_transcript_append", None)
        session.pop("_context_compacted", None)
        session.update(updated)

    def finish(self, claim: Claim, *, failed=False, recovery=None):
        def finish(tx, jr, sr, job, session):
            if not failed:
                require_unlocked(session)
            if not failed and job.get("open_actions"):
                raise LeaseLost("action outcome requires reconciliation")
            slot_ref, slots = read_slots(self.db, tx, job["uid"])
            if failed and job.get("state") == "leased" and job.get("queue_item_id"):
                return self._expire_unstarted_queue(tx, jr, sr, job, session, slots, expired=False)
            if failed or session.get("cancel_requested"):
                self._queue_cleanup(tx, session["id"])
            if failed:
                # Cancellation does not make a simultaneous provider failure
                # certain: a timed-out provision may still materialize later.
                session["recovery_required"] = True
                if recovery:
                    session["recovery"] = recovery
                    job["recovery"] = recovery
            if session.get("cancel_requested"):
                session.update(status="cancelled", pending_approval=None)
                job["state"] = "cancelled"
            elif failed:
                session.update(status="error", recovery_required=True, pending_approval=None,
                               error="run failed; inspect and reconcile before starting another session")
                job["state"] = "failed"
            elif session["status"] == "awaiting_approval":
                job["state"] = "waiting_for_approval"
            elif session["status"] == "idle":
                job["state"] = "succeeded"
            else:
                raise ValueError("run did not reach a terminal or approval state")
            if not failed:
                release_run(slots, claim.job_id)
                tx.set(slot_ref, slots)
            job["updated_at"] = self.clock()
            session["updated_at"] = _iso(self.clock())
            tx.set(jr, job)
            self._write_session(tx, sr, session)
        self._authority_tx(claim, finish, allow_cancel=True, completed_ok=True, require_task=False)

    def cancel(self, session_id: str, uid: str, key=None) -> dict:
        sr = self._ref(SESSIONS_COLLECTION, session_id)
        def cancel(tx):
            session = self._read(sr, tx)
            if session is None or session["uid"] != uid:
                raise CommandError(404, "session not found")
            receipt_ref = None
            if key is not None:
                if not isinstance(key, str) or not 1 <= len(key) <= 200:
                    raise CommandError(422, "Idempotency-Key must contain 1 to 200 characters")
                receipt_ref = self._ref(RECEIPTS_COLLECTION, _digest([uid, "cancel", session_id, key]))
                receipt = self._read(receipt_ref, tx)
                if receipt:
                    return receipt["response"]
            jr = self._ref(JOBS_COLLECTION, session["active_run_id"])
            job = self._read(jr, tx)
            cleanup_ref = self._ref(CLEANUP_COLLECTION, session_id)
            cleanup = self._read(cleanup_ref, tx)
            slot_ref, slots = read_slots(self.db, tx, uid)
            if session["status"] == "cancelled":
                if receipt_ref is not None:
                    tx.create(receipt_ref, {"uid": uid, "response": session, "created_at": self.clock(), "schema_version": 2})
                return session
            executing = job and job["state"] == "running"
            session["queue_ready"] = False
            session.update(cancel_requested=True, cancel_requested_at=session.get("cancel_requested_at", self.clock()), pending_approval=None,
                           status="cancel_requested" if executing else "cancelled",
                           updated_at=_iso(self.clock()))
            if job and not executing:
                if not session.get("recovery_required"):
                    release_run(slots, job["id"])
                    tx.set(slot_ref, slots)
                job.update(state="cancelled", updated_at=self.clock())
                tx.set(jr, job)
            self._write_session(tx, sr, session)
            if cleanup is None:
                tx.set(cleanup_ref, {"session_id": session_id, "state": "queued", "next_attempt": self.clock(),
                                     "fence": 0, "attempts": 0})
            if receipt_ref is not None:
                tx.create(receipt_ref, {"uid": uid, "response": session, "created_at": self.clock(), "schema_version": 2})
            return session
        return self._tx(cancel)

    def cleanup_once(self, closer, owner: str):
        """Retryable resource cleanup. Never forget registry on provider errors."""
        candidates = self.db.collection(CLEANUP_COLLECTION).where(
            filter=FieldFilter("next_attempt", "<=", self.clock())).limit(20).stream()
        for candidate in candidates:
            ref = self._ref(CLEANUP_COLLECTION, candidate.id)
            def claim_cleanup(tx):
                item = self._read(ref, tx)
                if not item or item["next_attempt"] > self.clock():
                    return None
                session = self._read(self._ref(SESSIONS_COLLECTION, item["session_id"]), tx)
                # Once running code has observed cancellation or its lease is
                # revoked, close its remote sandbox. Pending work is not raced.
                if session and session["status"] == "cancel_requested":
                    return None
                item.update(owner=owner, fence=item["fence"] + 1,
                            next_attempt=self.clock() + LEASE_SECONDS)
                tx.set(ref, item)
                return item, bool(session and session.get("recovery_required"))
            claimed = self._tx(claim_cleanup)
            if not claimed:
                continue
            item, uncertain = claimed
            try:
                closer(item["session_id"])
                # An interrupted provision may register a resource later. Keep
                # the intent discoverable until manual reconciliation in that
                # case, even when no registry currently exists.
                success = not uncertain
            except Exception:
                success = False
            def settle(tx):
                current = self._read(ref, tx)
                if not current or current.get("owner") != owner or current["fence"] != item["fence"]:
                    return
                session = self._read(self._ref(SESSIONS_COLLECTION, item["session_id"]), tx)
                if success and session:
                    slot_ref, slots = read_slots(self.db, tx, session["uid"])
                    release_sandbox(slots, item["session_id"])
                    tx.set(slot_ref, slots)
                current.update(attempts=current["attempts"] + 1)
                if success:
                    # Completed cleanup is excluded by the due-time query.
                    current.update(state="completed", next_attempt=253402300799.0)
                else:
                    current.update(state="queued", next_attempt=self.clock() + 60,
                                   last_error="cleanup requires retry or reconciliation")
                tx.set(ref, current)
            self._tx(settle)
            return True
        return False

    def candidates(self, limit=50):
        jobs = self.db.collection(JOBS_COLLECTION)
        # Two bounded queries avoid active leases starving the queued scan.
        stale = jobs.where(filter=FieldFilter("state", "in", ["leased", "running"])).where(
            filter=FieldFilter("lease_expires_at", "<=", self.clock())).limit(limit).stream()
        queued = jobs.where(filter=FieldFilter("state", "==", "queued")).order_by(
            "created_at").limit(limit).stream()
        return [doc.id for doc in stale] + [doc.id for doc in queued]


def _iso(timestamp):
    import datetime
    return datetime.datetime.fromtimestamp(timestamp, datetime.timezone.utc).isoformat()


runs = RunRepository()

"""Transactional job/session state, independent of model and sandbox providers.

A leased job has not executed effects and can be reclaimed. Once running, an
expired job is failed closed, never replayed: a remote effect may have happened.
This intentionally does not promise exactly-once external execution.
"""
import copy
import hashlib
import json
import time
from dataclasses import dataclass

from google.cloud.firestore_v1 import transactional
from google.cloud.firestore_v1.base_query import FieldFilter

from app.firebase import db
from app.store import SESSIONS_COLLECTION, create_session

JOBS_COLLECTION = "agent_v2_jobs"
RECEIPTS_COLLECTION = "agent_v2_commands"
LEASE_SECONDS = 45
CANCEL_GRACE_SECONDS = 15
MAX_RUN_SECONDS = 900
CLEANUP_COLLECTION = "agent_v2_cleanup"


class CommandError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class ExecutionStopped(Exception):
    """Control flow: never turn cancellation/fence loss into a tool result."""


class LeaseLost(ExecutionStopped):
    pass


class CancelRequested(ExecutionStopped):
    pass


@dataclass(frozen=True)
class Claim:
    job_id: str
    session_id: str
    owner: str
    token: int


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class RunRepository:
    def __init__(self, client=db, clock=time.time, transaction_runner=None):
        self.db, self.clock = client, clock
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

    def submit(self, uid: str, key: str, operation: str, payload: dict, session_id=None) -> dict:
        """Commit receipt + session transition + job together; no handoff gap."""
        if not key or len(key) > 200:
            raise CommandError(422, "Idempotency-Key must contain 1 to 200 characters")
        command_id = _digest([uid, operation, session_id, key])
        fingerprint = _digest(payload)
        receipt_ref = self._ref(RECEIPTS_COLLECTION, command_id)
        job_ref = self._ref(JOBS_COLLECTION, command_id)
        sid = session_id or command_id
        session_ref = self._ref(SESSIONS_COLLECTION, sid)
        now = self.clock()

        def command(tx):
            receipt = self._read(receipt_ref, tx)
            if receipt:
                if receipt["fingerprint"] != fingerprint:
                    raise CommandError(409, "idempotency key was used with different input")
                return receipt["response"]
            current = self._read(session_ref, tx)
            if operation == "create":
                if current:
                    raise CommandError(409, "session already exists")
                session = create_session(payload["task"], payload["kind"], uid)
                session["id"] = sid
            else:
                if current is None or current["uid"] != uid:
                    raise CommandError(404, "session not found")
                session = current
                active_job = self._read(self._ref(JOBS_COLLECTION, session["active_run_id"]), tx)
                if active_job and active_job["state"] in {"queued", "leased", "running"}:
                    raise CommandError(409, "current run is still active")
                if session.get("recovery_required"):
                    raise CommandError(409, "interrupted execution requires reconciliation; start a new session")
                if operation == "message":
                    if session["status"] != "idle":
                        raise CommandError(409, "session must be idle before another turn")
                    session["messages"].append({"role": "user", "content": [{"type": "text", "text": payload["content"]}]})
                elif operation == "approve":
                    pending = session.get("pending_approval")
                    if session["status"] != "awaiting_approval" or not pending:
                        raise CommandError(409, "no pending approval on this session")
                    if payload["approval_id"] != pending["id"]:
                        raise CommandError(409, "approval batch is no longer current")
                    risky = {c["id"] for c in pending["calls"] if c["risky"]}
                    ids = [d["call_id"] for d in payload["decisions"]]
                    if len(ids) != len(set(ids)) or set(ids) != risky:
                        raise CommandError(400, "decide exactly once on every risky pending call")
                else:
                    raise ValueError("unknown command")
            session.update(status="queued", active_run_id=command_id, cancel_requested=False,
                           error=None, schema_version=2, updated_at=_iso(now),
                           revision=session.get("revision", 0) + 1)
            job = {"id": command_id, "session_id": sid, "uid": uid, "schema_version": 2,
                   "operation": operation, "payload": payload, "state": "queued",
                   "fence": 0, "created_at": now, "updated_at": now}
            # A response snapshot makes lost-response retries deterministic.
            # Receipts have no automatic expiry: deleting them permits key reuse.
            tx.set(session_ref, session)
            tx.set(job_ref, job)
            tx.set(receipt_ref, {"uid": uid, "fingerprint": fingerprint,
                                 "response": session, "created_at": now, "schema_version": 2})
            return session
        return self._tx(command)

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
                tx.set(session_ref, session)
                tx.set(job_ref, job)
                return None
            eligible = job["state"] == "queued" or (
                job["state"] == "leased" and job["lease_expires_at"] <= now)
            if not eligible:
                return None
            if session.get("cancel_requested"):
                session.update(status="cancelled", pending_approval=None, updated_at=_iso(now))
                job.update(state="cancelled", updated_at=now)
                tx.set(session_ref, session)
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
        def start_job(tx):
            jr, sr, job, session = self._owned(tx, claim)
            if job["state"] != "leased":
                raise LeaseLost("run already started")
            job.update(state="running", updated_at=self.clock(), run_deadline=self.clock() + MAX_RUN_SECONDS)
            session.update(status="running", updated_at=_iso(self.clock()))
            tx.set(jr, job)
            tx.set(sr, session)
            return job, session
        return self._tx(start_job)

    def heartbeat(self, claim: Claim):
        def beat(tx):
            jr, sr, job, session = self._owned(tx, claim, allow_cancel=True)
            cancel_expired = (session.get("cancel_requested") and
                              self.clock() >= session.get("cancel_requested_at", self.clock()) + CANCEL_GRACE_SECONDS)
            deadline_expired = self.clock() >= job.get("run_deadline", float("inf"))
            if cancel_expired or deadline_expired:
                self._queue_cleanup(tx, session["id"])
                job.update(state="cancelled" if cancel_expired else "interrupted", updated_at=self.clock())
                session.update(status="cancelled" if cancel_expired else "error", recovery_required=True,
                               pending_approval=None, updated_at=_iso(self.clock()),
                               error="execution deadline reached; in-flight action outcome requires reconciliation")
                tx.set(jr, job)
                tx.set(sr, session)
                return False
            job.update(lease_expires_at=self.clock() + LEASE_SECONDS, updated_at=self.clock())
            tx.set(jr, job)
            return True
        if not self._tx(beat):
            raise LeaseLost("run deadline expired")

    def checkpoint(self, claim: Claim):
        def check(tx):
            self._owned(tx, claim)
        self._tx(check)

    def save(self, claim: Claim, session: dict):
        def save(tx):
            jr, sr, job, current = self._owned(tx, claim)
            if session.get("revision") != current.get("revision"):
                raise LeaseLost("session revision changed")
            if session.get("active_run_id") != claim.job_id:
                raise LeaseLost("session belongs to a different run")
            if session["status"] not in {"running", "idle", "awaiting_approval"}:
                raise ValueError("invalid worker session state")
            updated = copy.deepcopy(session)
            updated.update(updated_at=_iso(self.clock()), revision=current["revision"] + 1)
            tx.set(sr, updated)
            if updated["status"] in {"idle", "awaiting_approval"}:
                job.update(state="succeeded" if updated["status"] == "idle" else "waiting_for_approval",
                           updated_at=self.clock())
                tx.set(jr, job)
            return updated
        updated = self._tx(save)
        session.update(updated)

    def finish(self, claim: Claim, *, failed=False):
        def finish(tx):
            existing = self._read(self._ref(JOBS_COLLECTION, claim.job_id), tx)
            if (existing and existing.get("owner") == claim.owner and
                    existing.get("fence") == claim.token and
                    existing["state"] in {"succeeded", "waiting_for_approval", "cancelled", "failed"}):
                return  # Result and terminal job already committed atomically.
            jr, sr, job, session = self._owned(tx, claim, allow_cancel=True)
            if failed or session.get("cancel_requested"):
                self._queue_cleanup(tx, session["id"])
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
            job["updated_at"] = self.clock()
            session["updated_at"] = _iso(self.clock())
            tx.set(jr, job)
            tx.set(sr, session)
        self._tx(finish)

    def cancel(self, session_id: str, uid: str) -> dict:
        sr = self._ref(SESSIONS_COLLECTION, session_id)
        def cancel(tx):
            session = self._read(sr, tx)
            if session is None or session["uid"] != uid:
                raise CommandError(404, "session not found")
            jr = self._ref(JOBS_COLLECTION, session["active_run_id"])
            job = self._read(jr, tx)
            cleanup_ref = self._ref(CLEANUP_COLLECTION, session_id)
            cleanup = self._read(cleanup_ref, tx)
            if session["status"] == "cancelled":
                return session
            executing = job and job["state"] == "running"
            session.update(cancel_requested=True, cancel_requested_at=session.get("cancel_requested_at", self.clock()), pending_approval=None,
                           status="cancel_requested" if executing else "cancelled",
                           updated_at=_iso(self.clock()))
            if job and not executing:
                job.update(state="cancelled", updated_at=self.clock())
                tx.set(jr, job)
            tx.set(sr, session)
            if cleanup is None:
                tx.set(cleanup_ref, {"session_id": session_id, "state": "queued", "next_attempt": self.clock(),
                                     "fence": 0, "attempts": 0})
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

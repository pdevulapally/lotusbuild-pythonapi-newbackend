"""Workspace metadata transitions using RunRepository's existing transaction authority.

No route, provider dispatch, recovery clearance, or budget release lives here.
Only a trusted adapter may publish verified bytes; child RPC cannot call these.
"""
import hashlib
import json
import math
import re

MAX_FILES = 256
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024
MAX_MANIFEST_BYTES = 128 * 1024

CHECKPOINTS_COLLECTION = "agent_v2_workspace_checkpoints"
RESTORES_COLLECTION = "agent_v2_workspace_restores"
REGISTRY_COLLECTION = "agent_v2_sandbox_registry"
_HEX = re.compile(r"[0-9a-f]{64}\Z")


def identity(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def require_unlocked(session):
    from app.execution.errors import LeaseLost
    if session.get("workspace_operation") or session.get("workspace_restore_id"):
        raise LeaseLost("workspace operation requires completion or reconciliation")


def _valid_id(value):
    from app.execution.errors import LeaseLost
    if type(value) is not str or not _HEX.fullmatch(value):
        raise LeaseLost("invalid workspace receipt identity")


class WorkspaceTransitions:
    """Mixin: intentionally shares _tx, _authority_tx, slots, sessions and events."""

    def _write_workspace(self, tx, sr, session):
        from app.execution.limits import bounded_session
        bounded_session(session)
        self._write_session(tx, sr, session)

    def workspace_restore_source(self, claim, *, checkpoint_id):
        _valid_id(checkpoint_id)
        def read(tx, jr, sr, job, session):
            self._fresh_restore_target(tx, claim, job, session)
            return self._restore_source(tx, session, checkpoint_id)
        return self._authority_tx(claim, read)

    def begin_workspace_checkpoint(self, claim, *, key, expires_at, source_digest):
        from app.execution.errors import LeaseLost
        from app.execution.repository import CLEANUP_COLLECTION
        from app.execution.slots import read_slots
        if type(key) is not str or not 1 <= len(key) <= 200:
            raise LeaseLost("workspace key must contain 1 to 200 characters")
        if type(expires_at) not in (int, float) or not math.isfinite(expires_at):
            raise LeaseLost("explicit finite workspace retention required")
        _valid_id(source_digest)
        receipt_id = identity([claim.session_id, claim.job_id, "checkpoint", key])

        def begin(tx, jr, sr, job, session):
            ref = self._ref(CHECKPOINTS_COLLECTION, receipt_id)
            existing = self._read(ref, tx)
            cleanup = self._read(self._ref(CLEANUP_COLLECTION, session["id"]), tx)
            _, slots = read_slots(self.db, tx, session["uid"])
            if (job["state"] != "running" or session.get("recovery_required") or job.get("open_actions")
                    or cleanup or claim.job_id not in slots["runs"] or session["id"] not in slots["sandboxes"]):
                raise LeaseLost("workspace is not quiescent under admitted run authority")
            generation = session.get("workspace_generation", 0)
            if existing:
                if (existing["uid"] != session["uid"] or existing["generation"] != generation
                        or existing["expires_at"] != expires_at or existing["source_digest"] != source_digest or self.clock() >= expires_at):
                    raise LeaseLost("checkpoint retry changed or expired")
                if existing["state"] == "ready":
                    return existing
                if session.get("workspace_operation") == {"id": receipt_id, "kind": "checkpoint"}:
                    return existing
                raise LeaseLost("checkpoint requires reconciliation")
            require_unlocked(session)
            if expires_at <= self.clock():
                raise LeaseLost("checkpoint retention expired")
            item = {"id": receipt_id, "uid": session["uid"], "session_id": session["id"],
                    "run_id": claim.job_id, "owner": claim.owner, "fence": claim.token,
                    "generation": generation, "source_digest": source_digest, "schema_version": 1, "state": "staging",
                    "created_at": self.clock(), "expires_at": expires_at}
            session["workspace_operation"] = {"id": receipt_id, "kind": "checkpoint"}
            tx.create(ref, item)
            self._write_workspace(tx, sr, session)
            return item
        return self._authority_tx(claim, begin)

    def publish_workspace_checkpoint(self, claim, *, checkpoint_id, manifest_digest, file_count, total_bytes):
        from app.execution.errors import LeaseLost
        _valid_id(checkpoint_id)
        _valid_id(manifest_digest)
        if (type(file_count) is not int or not 0 <= file_count <= MAX_FILES or
                type(total_bytes) is not int or not 0 <= total_bytes <= MAX_TOTAL_BYTES):
            raise LeaseLost("invalid checkpoint bounds")

        def publish(tx, jr, sr, job, session):
            ref = self._ref(CHECKPOINTS_COLLECTION, checkpoint_id)
            item = self._read(ref, tx)
            if (not item or (item["session_id"], item["uid"], item["run_id"], item["owner"], item["fence"]) !=
                    (session["id"], session["uid"], claim.job_id, claim.owner, claim.token)
                    or item["generation"] != session.get("workspace_generation", 0)
                    or self.clock() >= item["expires_at"] or job.get("open_actions") or session.get("recovery_required")):
                raise LeaseLost("checkpoint authority changed")
            sealed = {"manifest_digest": manifest_digest, "file_count": file_count, "total_bytes": total_bytes}
            if item["state"] == "ready":
                if any(item[k] != v for k, v in sealed.items()):
                    raise LeaseLost("checkpoint retry changed bytes")
                return item
            if (item["state"] != "staging" or session.get("workspace_operation") !=
                    {"id": checkpoint_id, "kind": "checkpoint"}):
                raise LeaseLost("checkpoint publication is not current")
            item.update(state="ready", **sealed)
            session.update(workspace_checkpoint_id=checkpoint_id, workspace_operation=None)
            tx.set(ref, item)
            self._write_workspace(tx, sr, session)
            return item
        return self._authority_tx(claim, publish)

    def _restore_source(self, tx, session, checkpoint_id):
        from app.execution.errors import LeaseLost
        from app.execution.repository import JOBS_COLLECTION
        item = self._read(self._ref(CHECKPOINTS_COLLECTION, checkpoint_id), tx)
        if not item or item.get("uid") != session["uid"]:
            raise LeaseLost("checkpoint not available to this owner")
        source = self._owner_session(item["session_id"], session["uid"], tx)
        source_job = self._read(self._ref(JOBS_COLLECTION, source["active_run_id"]), tx)
        if (item["state"] != "ready" or self.clock() >= item["expires_at"] or
                source.get("workspace_generation", 0) != item["generation"] or
                source.get("workspace_checkpoint_id") != checkpoint_id or source.get("workspace_operation") or
                source_job and source_job["state"] in {"queued", "leased", "running"}):
            raise LeaseLost("checkpoint is expired, stale or its source is active")
        return item

    def _fresh_restore_target(self, tx, claim, job, session):
        from app.execution.errors import LeaseLost
        from app.execution.repository import CLEANUP_COLLECTION
        from app.execution.slots import read_slots
        registry = self._read(self._ref(REGISTRY_COLLECTION, session["id"]), tx)
        cleanup = self._read(self._ref(CLEANUP_COLLECTION, session["id"]), tx)
        _, slots = read_slots(self.db, tx, session["uid"])
        if (job["state"] != "running" or job["operation"] != "create" or registry or cleanup
                or session.get("recovery_required") or session.get("action_sequence", 0)
                or session.get("workspace_generation", 0) or job.get("open_actions")
                or claim.job_id not in slots["runs"] or session["id"] not in slots["sandboxes"]):
            raise LeaseLost("restore requires a fresh admitted target before sandbox provisioning")

    def begin_workspace_restore(self, claim, *, checkpoint_id, key):
        from app.execution.errors import LeaseLost
        _valid_id(checkpoint_id)
        if type(key) is not str or not 1 <= len(key) <= 200:
            raise LeaseLost("workspace key must contain 1 to 200 characters")
        receipt_id = identity([claim.session_id, claim.job_id, "restore", key])

        def begin(tx, jr, sr, job, session):
            self._fresh_restore_target(tx, claim, job, session)
            item = self._restore_source(tx, session, checkpoint_id)
            ref = self._ref(RESTORES_COLLECTION, receipt_id)
            existing = self._read(ref, tx)
            if existing:
                if existing["checkpoint_id"] != checkpoint_id:
                    raise LeaseLost("restore retry changed input")
                if existing["state"] == "materialized":
                    return existing
                # A partial local directory is never retried or overwritten.
                raise LeaseLost("partial restore is quarantined; reconciliation required")
            require_unlocked(session)
            receipt = {"id": receipt_id, "uid": session["uid"], "session_id": session["id"],
                       "checkpoint_id": checkpoint_id, "source_session_id": item["session_id"],
                       "generation": item["generation"], "manifest_digest": item["manifest_digest"],
                       "owner": claim.owner, "fence": claim.token, "run_id": claim.job_id,
                       "schema_version": 1, "state": "staging", "created_at": self.clock()}
            session["workspace_operation"] = {"id": receipt_id, "kind": "restore"}
            tx.create(ref, receipt)
            self._write_workspace(tx, sr, session)
            return receipt
        return self._authority_tx(claim, begin)

    def complete_workspace_restore(self, claim, *, restore_id):
        from app.execution.errors import LeaseLost
        _valid_id(restore_id)

        def complete(tx, jr, sr, job, session):
            self._fresh_restore_target(tx, claim, job, session)
            ref = self._ref(RESTORES_COLLECTION, restore_id)
            receipt = self._read(ref, tx)
            if (not receipt or (receipt["session_id"], receipt["uid"], receipt["run_id"], receipt["owner"], receipt["fence"]) !=
                    (session["id"], session["uid"], claim.job_id, claim.owner, claim.token)):
                raise LeaseLost("restore authority changed")
            self._restore_source(tx, session, receipt["checkpoint_id"])
            if receipt["state"] == "materialized":
                return receipt
            if session.get("workspace_operation") != {"id": restore_id, "kind": "restore"}:
                raise LeaseLost("restore publication is not current")
            receipt["state"] = "materialized"
            # This is a quarantined candidate, never an active sandbox or a
            # successful recovered run. Adoption needs a verified provider.
            session.update(workspace_operation=None, workspace_restore_id=restore_id)
            tx.set(ref, receipt)
            self._write_workspace(tx, sr, session)
            return receipt
        return self._authority_tx(claim, complete)

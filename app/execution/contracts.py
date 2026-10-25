"""Owner-scoped metadata and immutable text history on the run transaction authority.

No provider, auth, job execution, budget or lifecycle implementation lives here.
Collection names deliberately do not adopt the TypeScript runtime's documents.
"""
import datetime
import re

from google.cloud.firestore_v1.base_query import FieldFilter

from app.execution.errors import LeaseLost
from app.execution.limits import bounded_json, MAX_SESSION_BYTES

PROJECTS_COLLECTION = "agent_v2_projects"
ACCOUNTS_COLLECTION = "agent_v2_accounts"
MESSAGES_COLLECTION = "agent_v2_messages"
DOCUMENT_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


def reject(status, message, code):
    from app.execution.repository import CommandError
    raise CommandError(status, message, code=code)


def timestamp(now):
    return datetime.datetime.fromtimestamp(now, datetime.timezone.utc).isoformat()


def require_id(value, kind):
    if not isinstance(value, str) or not DOCUMENT_ID.fullmatch(value):
        reject(404, f"{kind} not found", f"{kind}/not-found")


def project_response(project):
    return {key: project[key] for key in ("id", "name", "platform", "status", "publicPreviewEnabled", "createdAt")
            if key in project} | ({"description": project["description"]} if "description" in project else {})


def valid_timestamp(value):
    try:
        return isinstance(value, str) and len(value) <= 64 and datetime.datetime.fromisoformat(value).utcoffset() is not None
    except ValueError:
        return False


def validate_project_record(project):
    if (not isinstance(project.get("id"), str) or not DOCUMENT_ID.fullmatch(project["id"]) or
            not isinstance(project.get("name"), str) or not 1 <= len(project["name"]) <= 100 or
            not project["name"].strip() or any(ord(c) < 32 or ord(c) == 127 for c in project["name"]) or
            ("description" in project and (not isinstance(project["description"], str) or len(project["description"]) > 500)) or
            not valid_timestamp(project.get("createdAt")) or type(project.get("sequence")) is not int or
            project["sequence"] < 1 or project.get("platform") != "web" or project.get("publicPreviewEnabled") is not False):
        reject(503, "Project metadata requires reconciliation.", "project/metadata-unavailable")
    if ("active_session_id" not in project or (project["active_session_id"] is not None and
            (not isinstance(project["active_session_id"], str) or not DOCUMENT_ID.fullmatch(project["active_session_id"])))):
        reject(503, "Project session guard requires reconciliation.", "project/session-guard-unavailable")


def validate_project_input(payload, *, creating):
    if not isinstance(payload, dict) or set(payload) - {"name", "description"} or (creating and "name" not in payload):
        reject(422, "invalid project metadata", "request/invalid")
    result = {}
    for key, value in payload.items():
        if type(value) is not str:
            reject(422, "invalid project metadata", "request/invalid")
        value = value.strip()
        if (len(value) > (100 if key == "name" else 500) or (key == "name" and not value) or
                any(ord(char) < 32 or ord(char) == 127 for char in value)):
            reject(422, "invalid project metadata", "request/invalid")
        result[key] = value
    return result


def capability_values(entitlements):
    """Read the verified TS typed capability shape from the CURRENT native plan.

    Only the plan contributes in the verified server. No seed allowances, tier
    mapping or second subscription resolver is introduced here.
    """
    raw = entitlements.capabilities
    values = {}
    if not isinstance(raw, list) or len(raw) > 100:
        reject(503, "Project capabilities platform.web and projects.max are not configured.", "project/policy-unavailable")
    for entry in raw:
        if (not isinstance(entry, dict) or set(entry) != {"capabilityKey", "value"} or
                not isinstance(entry["capabilityKey"], str) or entry["capabilityKey"] in values or
                not isinstance(entry["value"], dict) or set(entry["value"]) != {"kind", "value"}):
            reject(503, "Project capability configuration is invalid.", "project/policy-unavailable")
        values[entry["capabilityKey"]] = entry["value"]
    return values


def project_policy(entitlements):
    values = capability_values(entitlements)
    web, maximum = values.get("platform.web", {}), values.get("projects.max", {})
    if (web.get("kind") != "boolean" or type(web.get("value")) is not bool or
            maximum.get("kind") != "number" or type(maximum.get("value")) is not int or
            not 0 <= maximum["value"] <= 2**53 - 1):
        reject(503, "Project capabilities platform.web and projects.max are missing or invalid.", "project/policy-unavailable")
    if not web["value"]:
        reject(403, "This plan cannot create web projects.", "project/platform-not-permitted")
    return maximum["value"]


def project_execution_policy(entitlements, model):
    values = capability_values(entitlements)
    ai, web, models = (values.get(key, {}) for key in ("ai.run", "platform.web", "ai.model.allowlist"))
    if (ai.get("kind") != "boolean" or type(ai.get("value")) is not bool or
            web.get("kind") != "boolean" or type(web.get("value")) is not bool or
            models.get("kind") != "stringList" or not isinstance(models.get("value"), list) or
            len(models["value"]) > 100 or any(not isinstance(item, str) or not 1 <= len(item) <= 128 for item in models["value"])):
        reject(503, "Project execution requires configured ai.run, platform.web and ai.model.allowlist capabilities.", "session/policy-unavailable")
    if not ai["value"]:
        reject(403, "This plan cannot run builds.", "session/ai-not-permitted")
    if not web["value"]:
        reject(403, "This plan cannot execute web projects.", "session/platform-not-permitted")
    # Verified upstream semantics: empty means unrestricted by this capability.
    # Native models remain mandatory current catalog/quota authority; this
    # capability only restricts them and never grants a missing native model.
    if models["value"] and model not in models["value"]:
        reject(403, "Selected model is not permitted for project execution.", "session/model-not-permitted")


class ProductContracts:
    def _account(self, uid, tx=None):
        ref = self._ref(ACCOUNTS_COLLECTION, uid)
        snap = None if tx is not None else ref.get()
        account = self._read(ref, tx) if tx is not None else (snap.to_dict() if snap.exists else None)
        if account is None:
            return ref, {"uid": uid, "projectCount": 0, "project_sequence": 0}
        if (account.get("uid") != uid or type(account.get("projectCount")) is not int or
                account["projectCount"] < 0 or type(account.get("project_sequence")) is not int or
                account.get("project_sequence") != account["projectCount"]):
            reject(503, "Project counter requires reconciliation.", "project/counter-unavailable")
        return ref, account

    def _owner_project(self, project_id, uid, tx=None):
        require_id(project_id, "project")
        ref = self._ref(PROJECTS_COLLECTION, project_id)
        snap = None if tx is not None else ref.get()
        project = self._read(ref, tx) if tx is not None else (snap.to_dict() if snap.exists else None)
        if not project or project.get("uid") != uid or project.get("status") != "draft":
            reject(404, "project not found", "project/not-found")
        if project.get("id") != project_id:
            reject(503, "Project metadata requires reconciliation.", "project/metadata-unavailable")
        validate_project_record(project)
        return ref, project

    def read_project(self, project_id, uid):
        return project_response(self._owner_project(project_id, uid)[1])

    def create_project(self, uid, key, payload):
        from app.billing.entitlements import resolve_entitlements
        from app.execution.repository import RECEIPTS_COLLECTION, _digest
        payload = validate_project_input(payload, creating=True)
        if not isinstance(key, str) or not 1 <= len(key) <= 200:
            reject(422, "Idempotency-Key must contain 1 to 200 characters", "request/invalid")
        command_id = _digest([uid, "project_create", key])
        receipt_ref = self._ref(RECEIPTS_COLLECTION, command_id)
        fingerprint = _digest(payload)
        def create(tx):
            receipt = self._read(receipt_ref, tx)
            if receipt:
                # Scope ownership is checked again even on a lost-response replay.
                self._owner_project(command_id, uid, tx)
                if receipt["fingerprint"] != fingerprint:
                    reject(409, "idempotency key was used with different input", "command/conflict")
                return receipt["response"]
            ref = self._ref(PROJECTS_COLLECTION, command_id)
            if self._read(ref, tx) is not None:
                reject(409, "Project command requires reconciliation.", "command/conflict")
            account_ref, account = self._account(uid, tx)
            maximum = project_policy(resolve_entitlements(uid, client=self.db, transaction=tx, now=self.clock()))
            if account["projectCount"] >= maximum:
                reject(403, "Project limit reached for the current plan.", "project/limit-reached")
            project = {"id": command_id, "uid": uid, **payload, "platform": "web", "status": "draft",
                       "publicPreviewEnabled": False, "active_session_id": None,
                       "sequence": account["project_sequence"] + 1, "createdAt": timestamp(self.clock())}
            account.update(projectCount=account["projectCount"] + 1, project_sequence=project["sequence"])
            response = project_response(project)
            tx.create(ref, project)
            tx.set(account_ref, account)
            tx.create(receipt_ref, {"uid": uid, "fingerprint": fingerprint, "response": response,
                                    "created_at": self.clock(), "schema_version": 2})
            return response
        return self._tx(create)

    def update_project(self, project_id, uid, payload):
        def update(tx):
            ref, project = self._owner_project(project_id, uid, tx)
            changes = validate_project_input(payload, creating=False)
            # A narrow patch reads current state inside the commit transaction.
            # It cannot replace execution guards or change ownership/capabilities.
            project.update(changes)
            if project.get("description") == "":
                project.pop("description")
            tx.set(ref, project)
            return project_response(project)
        return self._tx(update)

    def list_projects(self, uid, before=None, limit=25):
        _, account = self._account(uid)
        upper = account["project_sequence"] + 1
        if before is not None:
            _, anchor = self._owner_project(before, uid)
            upper = anchor["sequence"]
        if type(limit) is not int or not 1 <= limit <= 100:
            reject(422, "project page limit must be between 1 and 100", "request/invalid")
        lower = max(0, upper - limit - 1)
        query = self.db.collection(PROJECTS_COLLECTION).where(filter=FieldFilter("uid", "==", uid)).where(
            filter=FieldFilter("sequence", ">", lower)).where(filter=FieldFilter("sequence", "<=", upper - 1)).order_by("sequence").limit(limit)
        items = [snap.to_dict() for snap in query.stream()]
        if ([p.get("sequence") for p in items] != list(range(lower + 1, upper)) or
                any(p.get("uid") != uid or p.get("status") != "draft" for p in items)):
            reject(503, "Project history requires reconciliation.", "project/history-unavailable")
        for project in items:
            validate_project_record(project)
        items.reverse()
        return {"projects": [project_response(p) for p in items],
                "nextCursor": items[-1]["id"] if items and lower > 0 else None}

    def _bind_project(self, tx, project_id, uid, session_id, model):
        from app.store import SESSIONS_COLLECTION
        from app.execution.repository import JOBS_COLLECTION
        ref, project = self._owner_project(project_id, uid, tx)
        from app.billing.entitlements import resolve_entitlements
        project_execution_policy(resolve_entitlements(uid, client=self.db, transaction=tx, now=self.clock()), model)
        if "active_session_id" not in project:
            reject(503, "Project session guard requires reconciliation.", "project/session-guard-unavailable")
        active_id = project["active_session_id"]
        if active_id is not None:
            active = self._read(self._ref(SESSIONS_COLLECTION, active_id), tx)
            job = self._read(self._ref(JOBS_COLLECTION, active["active_run_id"]), tx) if active else None
            # Idle sessions still hold their workspace. Release/adoption is not
            # inferred from completion; a new session must not overwrite it.
            if not active or active.get("uid") != uid or active.get("project_id") != project_id or not job:
                reject(503, "Project session guard requires reconciliation.", "project/session-guard-unavailable")
            reject(409, "Another session owns this project's workspace.", "project/session-active")
        return ref, project | {"active_session_id": session_id}

    def _project_execution(self, tx, session):
        if session.get("project_id") is None:
            return
        from app.billing.entitlements import resolve_entitlements
        _, project = self._owner_project(session["project_id"], session["uid"], tx)
        if project.get("active_session_id") != session["id"]:
            reject(409, "Project session authority changed.", "project/session-active")
        project_execution_policy(resolve_entitlements(session["uid"], client=self.db, transaction=tx, now=self.clock()), session.get("model"))

    def _append_transcript(self, tx, session, messages):
        if session.get("transcript_version") != 1:
            return  # Pre-upgrade contexts are never backfilled as original history.
        count = session["message_count"]
        for message in messages:
            role, blocks = message.get("role"), message.get("content")
            if role not in {"user", "assistant"} or not isinstance(blocks, list):
                continue
            text = "\n".join(block["text"] for block in blocks if isinstance(block, dict) and
                             block.get("type") == "text" and isinstance(block.get("text"), str))
            if not text:
                continue  # Tool inputs/results and model context are not UI text.
            record = {"session_id": session["id"], "uid": session["uid"], "seq": count,
                      "role": role, "content": text, "attachments": [], "createdAt": timestamp(self.clock())}
            bounded_json(record, MAX_SESSION_BYTES)
            tx.create(self._ref(MESSAGES_COLLECTION, f"{session['id']}:{count}"), record)
            count += 1
        session["message_count"] = count

    def _worker_transcript(self, tx, current, candidate):
        """Explicit append signal validated against the unchanged context prefix.

        Compaction has a separate explicit signal and can only replace the
        middle with one summary. Its synthetic summary is never archived.
        """
        if any(candidate.get(k) != current.get(k) for k in ("project_id", "transcript_version", "message_count")):
            raise LeaseLost("session history or project authority changed")
        append = candidate.pop("_transcript_append", None)
        compacted = candidate.pop("_context_compacted", None)
        if current.get("transcript_version") != 1:
            return
        previous, messages = current["messages"], candidate["messages"]
        if append is not None:
            if (compacted is not None or not isinstance(append, list) or not 1 <= len(append) <= 100 or
                    messages != previous + append or any(m.get("role") != "assistant" for m in append)):
                raise LeaseLost("invalid transcript append")
            self._append_transcript(tx, candidate, append)
        elif compacted is not None:
            from app.agent.compaction import COMPACTION_KEEP_LAST_N
            if (compacted is not True or len(previous) <= COMPACTION_KEEP_LAST_N + 1 or
                    len(messages) != COMPACTION_KEEP_LAST_N + 2 or messages[0] != previous[0] or
                    messages[2:] != previous[-COMPACTION_KEEP_LAST_N:] or messages[1].get("role") != "user" or
                    not isinstance(messages[1].get("content"), list) or len(messages[1]["content"]) != 1 or
                    messages[1]["content"][0].get("type") != "text" or
                    not messages[1]["content"][0].get("text", "").startswith("[earlier conversation summarized]\n")):
                raise LeaseLost("invalid context compaction")
        elif messages != previous:
            # Ordinary tool-result appends remain model context only.
            suffix = messages[len(previous):]
            if (messages[:len(previous)] != previous or any(not isinstance(m, dict) or
                    m.get("role") not in {"user", "tool_result"} or not isinstance(m.get("content"), list) or
                    any(not isinstance(block, dict) or block.get("type") != "tool_result" for block in m["content"])
                    for m in suffix)):
                raise LeaseLost("text history requires an explicit append")

    def message_history(self, session_id, uid, before=None, limit=100):
        require_id(session_id, "session")
        session = self._owner_session(session_id, uid)
        if session.get("transcript_version") != 1:
            reject(503, "Original transcript is unavailable for this pre-upgrade session.", "session/history-unavailable")
        head = session.get("message_count")
        if type(head) is not int or not 1 <= head <= 2**53 - 1:
            reject(503, "Transcript metadata requires reconciliation.", "session/history-unavailable")
        upper = head
        if type(limit) is not int or not 1 <= limit <= 200:
            reject(422, "message page limit must be between 1 and 200", "request/invalid")
        if before is not None:
            if not isinstance(before, str) or not re.fullmatch(r"[0-9]{1,16}", before) or not 0 <= int(before) < head:
                reject(422, "invalid message cursor", "request/invalid")
            upper = int(before)
            anchor = self._ref(MESSAGES_COLLECTION, f"{session_id}:{upper}").get().to_dict()
            if not anchor or anchor.get("uid") != uid or anchor.get("session_id") != session_id or anchor.get("seq") != upper:
                reject(503, "Transcript requires reconciliation.", "session/history-unavailable")
        lower = max(0, upper - limit)
        query = self.db.collection(MESSAGES_COLLECTION).where(filter=FieldFilter("session_id", "==", session_id)).where(
            filter=FieldFilter("seq", ">", lower - 1)).where(filter=FieldFilter("seq", "<=", upper - 1)).order_by("seq").limit(limit)
        items = []
        for snap in query.stream():
            items.append(snap.to_dict())
            # A bounded count is insufficient when original text is large.
            # Refuse an oversized page; never advance a cursor past omitted rows.
            bounded_json(items, MAX_SESSION_BYTES)
        if ([m.get("seq") for m in items] != list(range(lower, upper)) or
                any(m.get("uid") != uid or m.get("session_id") != session_id or type(m.get("seq")) is not int or
                    m.get("role") not in {"user", "assistant"} or not isinstance(m.get("content"), str) or
                    m.get("attachments") != [] or not valid_timestamp(m.get("createdAt")) for m in items)):
            reject(503, "Transcript requires reconciliation.", "session/history-unavailable")
        return {"messages": [{k: m[k] for k in ("seq", "role", "content", "attachments", "createdAt")} for m in items],
                "nextCursor": str(lower) if lower > 0 else None}

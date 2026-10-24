import datetime
import uuid

from app.firebase import db

SESSIONS_COLLECTION = "agent_v2_sessions"


def create_session(task: str, kind: str, uid: str) -> dict:
    """Pure builder - no I/O. Caller is responsible for save_session()."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    return {
        "id": uuid.uuid4().hex,
        "uid": uid,
        "kind": kind,
        "status": "queued",
        "schema_version": 2,
        "revision": 0,
        "active_run_id": None,
        "cancel_requested": False,
        "recovery_required": False,
        "error": None,
        "messages": [{"role": "user", "content": [{"type": "text", "text": task}]}],
        "plan": None,
        "pending_approval": None,
        "system_prompt": None,
        "created_at": now,
        "updated_at": now,
    }


def save_session(session: dict) -> None:
    from app.execution.context import save
    save(session)


def get_session(session_id: str) -> dict | None:
    doc = db.collection(SESSIONS_COLLECTION).document(session_id).get()
    return doc.to_dict() if doc.exists else None


def delete_session(session_id: str) -> None:
    db.collection(SESSIONS_COLLECTION).document(session_id).delete()

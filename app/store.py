import datetime
import uuid

from app.firebase import db

SESSIONS_COLLECTION = "sessions"


def create_session(task: str, kind: str, uid: str) -> dict:
    """Pure builder - no I/O. Caller is responsible for save_session()."""
    now = datetime.datetime.utcnow().isoformat()
    return {
        "id": uuid.uuid4().hex,
        "uid": uid,
        "kind": kind,
        "status": "running",
        "error": None,
        "messages": [{"role": "user", "content": [{"type": "text", "text": task}]}],
        "plan": None,
        "pending_approval": None,
        "system_prompt": None,
        "created_at": now,
        "updated_at": now,
    }


def save_session(session: dict) -> None:
    session["updated_at"] = datetime.datetime.utcnow().isoformat()
    db.collection(SESSIONS_COLLECTION).document(session["id"]).set(session)


def get_session(session_id: str) -> dict | None:
    doc = db.collection(SESSIONS_COLLECTION).document(session_id).get()
    return doc.to_dict() if doc.exists else None


def delete_session(session_id: str) -> None:
    db.collection(SESSIONS_COLLECTION).document(session_id).delete()

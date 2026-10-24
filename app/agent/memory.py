import datetime

from firebase_admin import firestore

from app.firebase import db

MEMORY_COLLECTION = "memory"


def append_note(uid: str, note: str) -> None:
    db.collection(MEMORY_COLLECTION).document(uid).set(
        {"notes": firestore.ArrayUnion([note]), "updated_at": datetime.datetime.utcnow().isoformat()},
        merge=True,
    )


def get_memory(uid: str) -> list[str]:
    doc = db.collection(MEMORY_COLLECTION).document(uid).get()
    return doc.to_dict().get("notes", []) if doc.exists else []


def clear_memory(uid: str) -> None:
    db.collection(MEMORY_COLLECTION).document(uid).delete()

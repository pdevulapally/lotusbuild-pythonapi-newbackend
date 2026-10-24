import datetime
import os

from cryptography.fernet import Fernet

from app.firebase import db

CREDENTIALS_COLLECTION = "github_credentials"

_fernet_instance: Fernet | None = None


def _fernet() -> Fernet:
    global _fernet_instance
    if _fernet_instance is None:
        _fernet_instance = Fernet(os.environ["CREDENTIAL_ENCRYPTION_KEY"])
    return _fernet_instance


def save_token(uid: str, token: str) -> None:
    encrypted = _fernet().encrypt(token.encode()).decode()
    db.collection(CREDENTIALS_COLLECTION).document(uid).set(
        {"token": encrypted, "created_at": datetime.datetime.utcnow().isoformat()}
    )


def get_token(uid: str) -> str:
    doc = db.collection(CREDENTIALS_COLLECTION).document(uid).get()
    if not doc.exists:
        raise ValueError("no GitHub account linked for this user")
    return _fernet().decrypt(doc.to_dict()["token"].encode()).decode()


def has_token(uid: str) -> bool:
    return db.collection(CREDENTIALS_COLLECTION).document(uid).get().exists

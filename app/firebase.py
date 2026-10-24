import json
import os

import firebase_admin
from firebase_admin import credentials, firestore

_db = None


def ensure_initialized() -> None:
    if not firebase_admin._apps:
        cred = credentials.Certificate(json.loads(os.environ["FIREBASE_SERVICE_ACCOUNT_JSON"]))
        firebase_admin.initialize_app(cred)


class _LazyFirestore:
    """Defers firebase_admin init - and its required env var - until Firestore is
    actually used, so importing a module that merely references `db` (e.g. for an
    offline unit test) doesn't require Firebase credentials to be configured."""

    def __getattr__(self, name):
        global _db
        if _db is None:
            ensure_initialized()
            _db = firestore.client()
        return getattr(_db, name)


db = _LazyFirestore()

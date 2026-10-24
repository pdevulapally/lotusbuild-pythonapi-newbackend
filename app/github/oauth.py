"""Browser-bound, single-use GitHub account linking.

Only opaque random state leaves this service. A separate HttpOnly browser secret
binds the callback to the browser that authenticated /start. Transactions are
consumed before exchanging a code: a failed exchange requires starting again,
not replaying an uncertain external operation. Expiry is enforced in application
code; optional Firestore TTL cleanup is not a security boundary.
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import urllib.parse
import urllib.request

from firebase_admin import firestore

from app.firebase import db

AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
TOKEN_URL = "https://github.com/login/oauth/access_token"
STATE_TTL_SECONDS = 600
TRANSACTIONS_COLLECTION = "github_oauth_transactions_v1"
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def begin_authorization(uid: str) -> tuple[str, str]:
    """Return (authorize URL, browser secret); never expose the secret in URL/JSON."""
    state = secrets.token_urlsafe(32)
    browser_secret = secrets.token_urlsafe(32)
    now = time.time()
    db.collection(TRANSACTIONS_COLLECTION).document(_digest(state)).create(
        {
            "uid": uid,
            "browser_secret_hash": _digest(browser_secret),
            "created_at": now,
            "expires_at": now + STATE_TTL_SECONDS,
            "consumed": False,
        }
    )
    params = {
        "client_id": os.environ["GITHUB_CLIENT_ID"],
        "redirect_uri": os.environ["GITHUB_REDIRECT_URI"],
        "scope": "repo",
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}", browser_secret


def _consume(transaction, ref, browser_secret: str) -> str:
    snapshot = ref.get(transaction=transaction)
    record = snapshot.to_dict() if snapshot.exists else None
    if (
        not record
        or record.get("consumed") is not False
        or not isinstance(record.get("expires_at"), (int, float))
        or record["expires_at"] <= time.time()
        or not hmac.compare_digest(record.get("browser_secret_hash", ""), _digest(browser_secret))
        or not isinstance(record.get("uid"), str)
        or not record["uid"]
    ):
        raise ValueError("invalid or expired OAuth state")
    transaction.update(ref, {"consumed": True, "consumed_at": time.time()})
    return record["uid"]


def _consume_transaction(ref, browser_secret: str) -> str:
    # Firestore's wrapper holds retry IDs; do not share that mutable wrapper
    # between concurrent callbacks handled by different request threads.
    return firestore.transactional(_consume)(db.transaction(), ref, browser_secret)


def resolve_state(state: str, browser_secret: str | None) -> str:
    """Atomically consume valid state only when the initiating browser is present."""
    if not _TOKEN_RE.fullmatch(state) or not browser_secret or not _TOKEN_RE.fullmatch(browser_secret):
        raise ValueError("invalid or expired OAuth state")
    ref = db.collection(TRANSACTIONS_COLLECTION).document(_digest(state))
    return _consume_transaction(ref, browser_secret)


def exchange_code_for_token(code: str) -> str:
    payload = urllib.parse.urlencode(
        {
            "client_id": os.environ["GITHUB_CLIENT_ID"],
            "client_secret": os.environ["GITHUB_CLIENT_SECRET"],
            "code": code,
            "redirect_uri": os.environ["GITHUB_REDIRECT_URI"],
        }
    ).encode()
    request = urllib.request.Request(
        TOKEN_URL, data=payload, headers={"Accept": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        raw = response.read(65537)
    if len(raw) > 65536:
        raise RuntimeError("GitHub token exchange failed")
    data = json.loads(raw)
    token = data.get("access_token")
    if not isinstance(token, str) or not token:
        raise RuntimeError("GitHub token exchange failed")
    return token

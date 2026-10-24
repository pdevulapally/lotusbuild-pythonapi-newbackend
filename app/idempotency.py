import datetime
import os
from dataclasses import dataclass
from typing import Callable

from fastapi import Header, HTTPException
from firebase_admin import firestore

from app.firebase import db
from app.security_log import log_security_event

IDEMPOTENCY_STALE_SECONDS = int(os.environ.get("IDEMPOTENCY_STALE_SECONDS", "120"))
IDEMPOTENCY_TTL_HOURS = int(os.environ.get("IDEMPOTENCY_TTL_HOURS", "24"))
COLLECTION = "idempotency_keys"


class IdempotencyConflict(Exception):
    pass


@dataclass
class IdempotentResult:
    replayed: bool
    status_code: int
    body: dict


def require_idempotency_key(idempotency_key: str = Header(..., alias="Idempotency-Key")) -> str:
    if not idempotency_key.strip():
        raise HTTPException(status_code=400, detail="Idempotency-Key header must not be blank")
    return idempotency_key


def _doc_id(uid: str | None, scope: str, key: str) -> str:
    return f"{uid if uid is not None else 'webhook'}:{scope}:{key}"


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def run_idempotent(uid: str | None, scope: str, key: str, fn: Callable[[], tuple[int, dict]]) -> IdempotentResult:
    """fn() runs at most once per (uid, scope, key) within IDEMPOTENCY_TTL_HOURS.

    A completed call's result is replayed verbatim to any later caller with the
    same key, without calling fn() again. A record still "in_progress" and younger
    than IDEMPOTENCY_STALE_SECONDS means a genuinely concurrent duplicate -> raises
    IdempotencyConflict. An "in_progress" record older than that is treated as an
    abandoned attempt (the process that started it likely crashed) and retried.
    Any exception from fn() deletes the record so the key is retryable from scratch.
    """
    ref = db.collection(COLLECTION).document(_doc_id(uid, scope, key))

    @firestore.transactional
    def _check_and_start(transaction) -> str:
        snapshot = ref.get(transaction=transaction)
        if snapshot.exists:
            data = snapshot.to_dict()
            if data["status"] == "completed":
                return "replay"
            if (_now() - data["created_at"]).total_seconds() < IDEMPOTENCY_STALE_SECONDS:
                log_security_event("idempotency_conflict", scope=scope, key=key, uid=uid)
                raise IdempotencyConflict(f"request with key {key!r} is already being processed")
        transaction.set(
            ref,
            {
                "status": "in_progress",
                "created_at": _now(),
                "ttl_expires_at": _now() + datetime.timedelta(hours=IDEMPOTENCY_TTL_HOURS),
            },
        )
        return "run"

    outcome = _check_and_start(db.transaction())

    if outcome == "replay":
        data = ref.get().to_dict()
        return IdempotentResult(True, data["response_status_code"], data["response_body"])

    try:
        status_code, body = fn()
    except Exception:
        ref.delete()
        raise

    ref.set(
        {
            "status": "completed",
            "response_status_code": status_code,
            "response_body": body,
            "completed_at": _now(),
            "ttl_expires_at": _now() + datetime.timedelta(hours=IDEMPOTENCY_TTL_HOURS),
        }
    )
    return IdempotentResult(False, status_code, body)

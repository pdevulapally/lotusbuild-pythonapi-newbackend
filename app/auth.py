from dataclasses import dataclass
import math

from fastapi import Header, HTTPException
from firebase_admin import auth as firebase_auth

from app.firebase import ensure_initialized
from app.security_log import log_security_event


class AuthenticationError(HTTPException):
    def __init__(self, detail, *, expired=False):
        super().__init__(status_code=401, detail=detail, headers={"WWW-Authenticate": "Bearer"})
        self.expired = expired
        self.code = "auth/token-expired" if expired else (
            "auth/missing-token" if detail == "missing bearer token" else "auth/invalid-token")


def _verify_token(authorization):
    if (not authorization or not authorization.startswith("Bearer ") or
            not authorization.removeprefix("Bearer ").strip()):
        log_security_event("auth_failure", reason="missing bearer token")
        raise AuthenticationError("missing bearer token")
    ensure_initialized()
    try:
        decoded = firebase_auth.verify_id_token(authorization.removeprefix("Bearer "), check_revoked=True)
        uid = decoded.get("uid") if isinstance(decoded, dict) else None
        if not isinstance(uid, str) or not uid:
            raise ValueError("token has no usable identity")
        # This backend configures only the default Firebase project namespace.
        # The SDK default client does not reject tenant JWTs automatically.
        firebase_claims = decoded.get("firebase")
        if isinstance(firebase_claims, dict) and firebase_claims.get("tenant") is not None:
            raise ValueError("unsupported Firebase tenant namespace")
    except Exception as exc:
        # SDK errors may contain token claims or other private diagnostics.
        log_security_event("auth_failure", reason="invalid token")
        raise AuthenticationError("invalid token", expired=isinstance(exc, firebase_auth.ExpiredIdTokenError)) from None
    return decoded


def get_uid(authorization: str | None = Header(default=None)) -> str:
    return _verify_token(authorization)["uid"]


@dataclass(frozen=True)
class VerifiedIdentity:
    uid: str
    expires_at: float


def verify_identity(authorization):
    claims = _verify_token(authorization)
    expiry = claims.get("exp")
    if type(expiry) not in (int, float) or not math.isfinite(expiry):
        raise AuthenticationError("invalid token")
    return VerifiedIdentity(claims["uid"], expiry)


def get_verified_uid(authorization: str | None = Header(default=None)) -> str:
    claims = _verify_token(authorization)
    if claims.get("email_verified") is not True:
        error = HTTPException(status_code=403, detail="Verify your email before creating projects or project sessions.")
        error.code = "auth/email-not-verified"
        raise error
    return claims["uid"]


@dataclass(frozen=True)
class QueueIdentity:
    uid: str
    issued_at: float
    expires_at: float
    valid_until: float
    tenant_id: str | None = None
    verified_at: float | None = None


def _request_identity(authorization, window):
    import time
    started = time.time()
    claims = _verify_token(authorization)
    issued, expiry = claims.get("iat"), claims.get("exp")
    tenant = claims.get("firebase", {}).get("tenant") if isinstance(claims.get("firebase"), dict) else None
    if (tenant is not None or claims.get("email_verified") is not True or
            any(type(value) not in (int, float) or not math.isfinite(value) for value in (issued, expiry)) or
            issued < 0 or expiry <= started or expiry <= issued):
        raise AuthenticationError("invalid token")
    seconds = window() if callable(window) else window
    if time.time() >= min(expiry, started + seconds):
        raise AuthenticationError("invalid token")
    return QueueIdentity(claims["uid"], issued, expiry, min(expiry, started + seconds), tenant,
                         started if callable(window) else None)


def get_queue_identity(authorization: str | None = Header(default=None)) -> QueueIdentity:
    return _request_identity(authorization, 1)


def get_task_identity(authorization: str | None = Header(default=None)) -> QueueIdentity:
    # New consent uses the explicitly configured freshness window, still
    # anchored BEFORE the shared verifier I/O. No deadline is renewed later.
    def configured_window():
        from app.execution.repository import runs
        return runs._task_policy()["evidence_ttl_seconds"]
    return _request_identity(authorization, configured_window)

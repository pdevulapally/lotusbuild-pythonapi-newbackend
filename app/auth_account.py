"""Minimal fresh account facts; no token cache or alternate identity authority."""
import math
import threading
import time
from dataclasses import dataclass

from app.auth import AuthenticationError


@dataclass(frozen=True)
class AccountEvidence:
    uid: str
    disabled: bool
    email_verified: bool
    valid_after_ms: float
    checked_at: float
    valid_until: float
    monotonic_until: float


def require_account(evidence, uid, consent_iat, now):
    if (not isinstance(evidence, AccountEvidence) or evidence.uid != uid or
            type(evidence.disabled) is not bool or type(evidence.email_verified) is not bool or
            any(type(v) not in (int, float) or not math.isfinite(v) for v in
                (evidence.valid_after_ms, evidence.checked_at, evidence.valid_until, evidence.monotonic_until, consent_iat)) or
            evidence.valid_after_ms < 0 or consent_iat < 0 or evidence.checked_at > now or
            now >= evidence.valid_until or time.monotonic() >= evidence.monotonic_until):
        raise AuthenticationError("fresh account evidence unavailable")
    # Same installed Firebase verifier comparison: equality remains valid.
    if evidence.disabled or not evidence.email_verified or consent_iat * 1000 < evidence.valid_after_ms:
        raise AuthenticationError("task authorization no longer valid")


def firebase_account_status(job, session):
    """Called only in explicitly configured trusted contained reader process."""
    from app.firebase import ensure_initialized
    from firebase_admin import auth
    ensure_initialized()
    user = auth.get_user(job["uid"])
    return {"uid": user.uid, "disabled": user.disabled, "email_verified": user.email_verified,
            "valid_after_ms": user.tokens_valid_after_timestamp}


class BoundedAccountReader:
    def __init__(self, handler=firebase_account_status, *, max_concurrent):
        if type(max_concurrent) is not int or not 1 <= max_concurrent <= 16:
            raise ValueError("explicit reader concurrency bound required")
        self.handler = handler
        self._cache = {}
        self._lock = threading.Lock()
        self._admission = threading.BoundedSemaphore(max_concurrent)

    def read(self, uid, *, timeout, ttl, clock=time.time):
        from app.execution.supervision import supervise_read
        started, monotonic_started = clock(), time.monotonic()
        if (not isinstance(uid, str) or not uid or len(uid) > 128 or
                type(timeout) not in (int, float) or type(ttl) not in (int, float) or
                not 0 < timeout < ttl <= 30):
            raise AuthenticationError("account reader policy unavailable")
        key = (uid, timeout, ttl)
        with self._lock:
            cached = self._cache.get(key)
            if cached and started < cached.valid_until and monotonic_started < cached.monotonic_until:
                return cached  # Preserve original pre-I/O deadline; never slide it.
        if not self._admission.acquire(blocking=False):
            raise AuthenticationError("fresh account reader capacity unavailable")
        try:
            result = supervise_read({"uid": uid}, handler=self.handler, timeout=timeout)
            if (not isinstance(result, dict) or set(result) != {"uid", "disabled", "email_verified", "valid_after_ms"} or
                    result["uid"] != uid or type(result["disabled"]) is not bool or type(result["email_verified"]) is not bool or
                    type(result["valid_after_ms"]) not in (int, float) or not math.isfinite(result["valid_after_ms"]) or result["valid_after_ms"] < 0):
                raise ValueError("invalid account facts")
            evidence = AccountEvidence(uid, result["disabled"], result["email_verified"], result["valid_after_ms"],
                                       started, started + ttl, monotonic_started + ttl)
            if clock() >= evidence.valid_until or time.monotonic() >= evidence.monotonic_until:
                raise ValueError("stale account facts")
        except BaseException:
            # Reader faults/SDK diagnostics never become HTTP bodies or grants.
            raise AuthenticationError("fresh account evidence unavailable") from None
        finally:
            self._admission.release()
        with self._lock:
            # Technical memory bound; no persisted or endlessly renewable proof.
            if len(self._cache) >= 100:
                self._cache.clear()
            self._cache[key] = evidence
        return evidence

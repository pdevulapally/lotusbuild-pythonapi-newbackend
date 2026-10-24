from fastapi import Header, HTTPException
from firebase_admin import auth as firebase_auth

from app.firebase import ensure_initialized
from app.security_log import log_security_event


def get_uid(authorization: str = Header(...)) -> str:
    if not authorization.startswith("Bearer "):
        log_security_event("auth_failure", reason="missing bearer token")
        raise HTTPException(status_code=401, detail="missing bearer token")
    ensure_initialized()
    try:
        decoded = firebase_auth.verify_id_token(authorization.removeprefix("Bearer "), check_revoked=True)
    except Exception as exc:
        log_security_event("auth_failure", reason=str(exc))
        raise HTTPException(status_code=401, detail=f"invalid token: {exc}")
    return decoded["uid"]

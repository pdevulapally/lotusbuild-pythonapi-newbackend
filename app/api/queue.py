"""Explicit finite native next-turn queue; no mid-run steering or saved tokens."""
from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict, AwareDatetime, Field

from app.auth import get_uid, get_queue_identity, QueueIdentity
from app.execution.repository import runs
from app.execution.contracts import reject
from app.api.contracts import page_limit
from app.idempotency import require_idempotency_key
from app.ratelimit import rate_limit_dependency

router = APIRouter()


class QueueInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    prompt: str = Field(min_length=1, max_length=8000)
    expiresAt: AwareDatetime = Field(strict=False)
    taskAuthorizationId: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    steer: bool = False
    mode: str | None = None
    attachments: list = Field(default_factory=list, max_length=10)


@router.post("/sessions/{session_id}/queued-messages", status_code=202)
def enqueue(session_id: str, body: QueueInput, identity: QueueIdentity = Depends(get_queue_identity),
            key: str = Depends(require_idempotency_key), _rl=Depends(rate_limit_dependency("mutate"))):
    runs._owner_session(session_id, identity.uid)
    if body.steer:
        reject(501, "Mid-run steering is separate from queued next-turn work and is unsupported.", "queue/steering-unavailable")
    if body.mode is not None:
        reject(409, "Wait for queued work to finish before changing mode.", "session/mode-change-busy")
    if body.attachments:
        reject(501, "Owned attachment storage is not implemented.", "attachment/unavailable")
    return runs.enqueue_message(session_id, identity.uid, key, body.prompt, body.expiresAt.timestamp(), identity, task_authorization_id=body.taskAuthorizationId)


@router.get("/sessions/{session_id}/queued-messages")
def list_queued(session_id: str, limit: str = "100", after: str | None = None, uid: str = Depends(get_uid)):
    runs._owner_session(session_id, uid)
    return runs.list_queued(session_id, uid, after, page_limit(limit, 200))


@router.get("/sessions/{session_id}/queued-messages/{item_id}")
def inspect(session_id: str, item_id: str, uid: str = Depends(get_uid)):
    return runs.inspect_queued(session_id, uid, item_id)


@router.delete("/sessions/{session_id}/queued-messages/{item_id}", status_code=204)
def cancel(session_id: str, item_id: str, uid: str = Depends(get_uid), _rl=Depends(rate_limit_dependency("mutate"))):
    runs.cancel_queued(session_id, uid, item_id)
    return Response(status_code=204)


@router.post("/sessions/{session_id}/queued-messages/dispatch")
def dispatch(session_id: str, identity: QueueIdentity = Depends(get_queue_identity),
             key: str = Depends(require_idempotency_key), _rl=Depends(rate_limit_dependency("mutate"))):
    return runs.dispatch_next(session_id, identity.uid, key, identity, owner_request=True)

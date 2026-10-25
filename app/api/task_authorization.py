"""Native explicit consent only; not frontend parity or a production enable switch."""
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, AwareDatetime

from app.auth import QueueIdentity, get_task_identity, get_uid
from app.execution.repository import runs
from app.idempotency import require_idempotency_key
from app.ratelimit import rate_limit_dependency

router = APIRouter()


class ConsentInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    mode: Literal["build", "plan"]
    model: str = Field(min_length=1, max_length=128)
    provider: str = Field(min_length=1, max_length=64)
    effort: str | None
    operations: list[Literal["queue.dispatch", "run.execute"]] = Field(min_length=2, max_length=2)
    tools: list[str] = Field(min_length=1, max_length=5)
    expiresAt: AwareDatetime = Field(strict=False)
    max_turns: int = Field(gt=0)
    max_run_seconds: int = Field(gt=0)
    max_model_requests: int = Field(gt=0)
    max_reserved_tokens: int = Field(gt=0)


class RevokeInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_version: int = Field(gt=0)


class TaskQueueInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    prompt: str = Field(min_length=1, max_length=8000)
    expiresAt: AwareDatetime = Field(strict=False)


@router.post("/sessions/{session_id}/task-authorizations", status_code=201)
def consent(session_id: str, body: ConsentInput, identity: QueueIdentity = Depends(get_task_identity),
            key: str = Depends(require_idempotency_key), _rl=Depends(rate_limit_dependency("mutate"))):
    payload = body.model_dump(exclude={"expiresAt"}) | {"expires_at": body.expiresAt.timestamp()}
    payload["operations"] = sorted(payload["operations"])
    payload["tools"] = sorted(payload["tools"])
    return runs.consent_task(session_id, identity, key, payload)


@router.get("/sessions/{session_id}/task-authorizations/{grant_id}")
def inspect(session_id: str, grant_id: str, uid: str = Depends(get_uid)):
    return runs.inspect_task(session_id, uid, grant_id)


@router.post("/sessions/{session_id}/task-authorizations/{grant_id}/revoke")
def revoke(session_id: str, grant_id: str, body: RevokeInput, uid: str = Depends(get_uid),
           key: str = Depends(require_idempotency_key), _rl=Depends(rate_limit_dependency("mutate"))):
    return runs.revoke_task(session_id, uid, grant_id, key, body.expected_version)


@router.post("/sessions/{session_id}/task-authorizations/{grant_id}/queued-messages", status_code=202)
def queue_task(session_id: str, grant_id: str, body: TaskQueueInput, identity: QueueIdentity = Depends(get_task_identity),
               key: str = Depends(require_idempotency_key), _rl=Depends(rate_limit_dependency("mutate"))):
    from app.execution.task_authorization import task_owner_fresh
    runs._owner_session(session_id, identity.uid)
    task_owner_fresh(identity, runs._task_policy(), runs.clock())
    return runs.enqueue_message(session_id, identity.uid, key, body.prompt, body.expiresAt.timestamp(), identity, task_authorization_id=grant_id)


@router.post("/sessions/{session_id}/task-authorizations/{grant_id}/dispatch")
def dispatch_task(session_id: str, grant_id: str, identity: QueueIdentity = Depends(get_task_identity),
                  key: str = Depends(require_idempotency_key), _rl=Depends(rate_limit_dependency("mutate"))):
    from app.execution.task_authorization import task_owner_fresh
    runs._owner_session(session_id, identity.uid)
    task_owner_fresh(identity, runs._task_policy(), runs.clock())
    return runs.dispatch_next(session_id, identity.uid, key, identity, owner_request=True, expected_task_authorization_id=grant_id)

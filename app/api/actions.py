"""Owner inspection and unverified evidence; no replay or recovery authority."""
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from app.auth import get_uid
from app.execution.repository import runs
from app.idempotency import require_idempotency_key
from app.ratelimit import rate_limit_dependency

router = APIRouter()


class ActionEvidenceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: Annotated[StrictInt, Field(ge=1, le=2**63 - 1)]
    assertion: Literal["effect_observed", "no_effect_observed", "inconclusive"]
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64)


@router.get("/sessions/{session_id}/actions")
def actions(session_id: str, after: str | None = None, limit: int = 50, uid: str = Depends(get_uid)):
    return runs.action_history(session_id, uid, after, limit)


@router.get("/sessions/{session_id}/actions/{action_id}")
def inspect(session_id: str, action_id: str, after: str | None = None, limit: int = 50, uid: str = Depends(get_uid)):
    return runs.inspect_action(session_id, uid, action_id, after, limit)


@router.post("/sessions/{session_id}/actions/{action_id}/reconciliation-evidence")
def evidence(session_id: str, action_id: str, body: ActionEvidenceIn, uid: str = Depends(get_uid),
             key: str = Depends(require_idempotency_key),
             _rl: None = Depends(rate_limit_dependency("mutate"))):
    return runs.record_action_evidence(session_id, uid, action_id, key, body.model_dump())

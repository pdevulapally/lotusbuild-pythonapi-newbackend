"""Verified project/text-history contracts; explicit errors for missing products.

The native session contract is opt-in on new project routes. The legacy client
cannot consume native approval/cancellation/recovery or unconfigured effort.
"""
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, field_validator
from google.cloud.firestore_v1.base_query import FieldFilter

from app.auth import get_uid, get_verified_uid
from app.execution.repository import runs
from app.execution.contracts import reject
from app.idempotency import require_idempotency_key
from app.models import SelectionInput
from app.ratelimit import rate_limit_dependency
from app.store import SESSIONS_COLLECTION

router = APIRouter()


class ProjectMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(max_length=100)
    description: str | None = Field(default=None, max_length=500)


class ProjectPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str | None = Field(default=None, max_length=100)
    description: str | None = Field(default=None, max_length=500)


class ProjectSessionCreate(SelectionInput):
    model_config = ConfigDict(extra="forbid", strict=True)
    prompt: str = Field(min_length=1, max_length=8000)
    model: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    effort: Literal["low", "standard", "high", "xhigh"] | None = None
    mode: Literal["build", "plan"] = "build"
    attachments: list = Field(default_factory=list, max_length=10)

    @field_validator("prompt")
    @classmethod
    def prompt_text(cls, value):
        value = value.strip()
        if not value or any((ord(c) < 32 and c not in "\t\n\r") or ord(c) == 127 for c in value):
            raise ValueError("invalid prompt")
        return value


def page_limit(value, maximum):
    if not value.isascii() or not value.isdecimal() or len(value) > 3 or not 1 <= int(value) <= maximum:
        reject(422, f"page limit must be between 1 and {maximum}", "request/invalid")
    return int(value)


def unsupported_session():
    reject(503, "The current client session schema lacks native approval, cancellation and recovery states; model effort/default capabilities are unconfigured. Use the documented native contract.",
           "session/client-contract-unavailable")


@router.get("/me")
def account(uid: str = Depends(get_uid)):
    # No empty models/limits or synthetic paid account while authoritative
    # catalog names, efforts, ceilings and meter mapping are not implemented.
    reject(503, "Account client contract requires verified model catalog names, effort capabilities, paid effort ceilings and meter-limit mapping from the current entitlement authority.",
           "account/contract-unavailable")


@router.get("/me/usage")
def usage(uid: str = Depends(get_uid)):
    reject(503, "Account usage meter mapping from the existing budget authority is not implemented.", "account/usage-unavailable")


@router.post("/projects", status_code=201)
def create_project(body: ProjectMetadata, uid: str = Depends(get_verified_uid),
                   key: str = Depends(require_idempotency_key), _rl=Depends(rate_limit_dependency("mutate"))):
    return runs.create_project(uid, key, body.model_dump(exclude_unset=True))


@router.get("/projects")
def projects(limit: str = "25", before: str | None = None, uid: str = Depends(get_uid)):
    return runs.list_projects(uid, before, page_limit(limit, 100))


@router.get("/projects/{project_id}")
def project(project_id: str, uid: str = Depends(get_uid)):
    return runs.read_project(project_id, uid)


@router.patch("/projects/{project_id}")
def update_project(project_id: str, body: ProjectPatch, uid: str = Depends(get_uid),
                   _rl=Depends(rate_limit_dependency("mutate"))):
    return runs.update_project(project_id, uid, body.model_dump(exclude_unset=True))


@router.delete("/projects/{project_id}")
def delete_project(project_id: str, uid: str = Depends(get_uid)):
    runs.read_project(project_id, uid)
    reject(501, "Recoverable project deletion, retention and authorization policy are not implemented.", "project/deletion-unavailable")


@router.post("/projects/{project_id}/sessions", status_code=201)
def create_session(project_id: str, body: ProjectSessionCreate, native: bool = False,
                   uid: str = Depends(get_verified_uid), key: str = Depends(require_idempotency_key),
                   _rl=Depends(rate_limit_dependency("mutate"))):
    runs.read_project(project_id, uid)
    if body.attachments:
        reject(501, "Owned attachment storage and validation are not implemented.", "attachment/unavailable")
    if not native:
        unsupported_session()  # Reject BEFORE committing a job or receipt.
    payload = {"task": body.prompt, "kind": "code", "model": body.model, "project_id": project_id, "mode": body.mode}
    if "effort" in body.model_fields_set:
        payload["effort"] = body.effort
    from app.models import SessionOut
    return SessionOut(**runs.submit(uid, key, "create", payload)).model_dump()


@router.get("/projects/{project_id}/sessions")
def sessions(project_id: str, native: bool = False, limit: str = "25", uid: str = Depends(get_uid)):
    _, project = runs._owner_project(project_id, uid)
    maximum = page_limit(limit, 100)
    if not native:
        unsupported_session()
    # Each project stays exclusively bound to its first workspace in this
    # slice. There is no historical migration or silent workspace adoption.
    query = runs.db.collection(SESSIONS_COLLECTION).where(filter=FieldFilter("uid", "==", uid)).where(
        filter=FieldFilter("project_id", "==", project_id)).order_by("created_at").limit(maximum)
    from app.models import SessionOut
    items = [snap.to_dict() for snap in query.stream()]
    expected = [] if project.get("active_session_id") is None else [project["active_session_id"]]
    if ([s.get("id") for s in items] != expected or
            any(s.get("uid") != uid or s.get("project_id") != project_id for s in items)):
        reject(503, "Project session history requires reconciliation.", "project/session-history-unavailable")
    return {"sessions": [SessionOut(**s).model_dump() for s in items]}


@router.get("/sessions/{session_id}/messages")
def messages(session_id: str, limit: str = "100", before: str | None = None, uid: str = Depends(get_uid)):
    runs._owner_session(session_id, uid)
    return runs.message_history(session_id, uid, before, page_limit(limit, 200))


@router.patch("/sessions/{session_id}")
def update_session(session_id: str, uid: str = Depends(get_uid)):
    runs._owner_session(session_id, uid)
    reject(501, "Generic session updates are unsupported; use fenced message, approval and cancellation commands.", "session/update-unavailable")


@router.post("/sessions/{session_id}/cancel")
def cancel_session(session_id: str, native: bool = False, uid: str = Depends(get_uid),
                   key: str = Depends(require_idempotency_key), _rl=Depends(rate_limit_dependency("mutate"))):
    runs._owner_session(session_id, uid)
    if not native:
        unsupported_session()
    from app.models import SessionOut
    return SessionOut(**runs.cancel(session_id, uid, key)).model_dump()


@router.get("/projects/{project_id}/{resource:path}")
def unsupported_project_resource(project_id: str, resource: str, uid: str = Depends(get_uid)):
    runs.read_project(project_id, uid)
    reject(501, "Project files, saved changes and archives require verified persistence/adoption adapters.", "project/content-unavailable")

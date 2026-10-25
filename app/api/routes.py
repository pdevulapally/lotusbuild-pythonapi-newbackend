import asyncio

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response

from app.agent.memory import clear_memory, get_memory
from app.agent.tracing import get_trace
from app.auth import get_uid
from app.idempotency import require_idempotency_key
from app.execution.repository import runs
from app.models import (
    ApproveIn,
    DesktopOut,
    MessageIn,
    PreviewOut,
    SessionCreate,
    SessionOut,
    TraceStepOut,
)
from app.ratelimit import rate_limit_dependency
from app.sandbox.manager import manager
from app.store import get_session
from app.api.events import router as events_router
from app.api.actions import router as actions_router
from app.api.streaming import AuthorizationGate, ManagedEventResponse, admission, POLL_SECONDS, read
from app.auth import AuthenticationError
from app.execution.limits import bounded_session
from app.execution.errors import OutputLimitExceeded

router = APIRouter()
router.include_router(events_router)
router.include_router(actions_router)

_TERMINAL_STATUSES = {"idle", "error", "cancelled", "awaiting_approval"}


def _require_session(session_id: str, uid: str) -> dict:
    session = get_session(session_id)
    if session is None or session["uid"] != uid:
        raise HTTPException(status_code=404, detail="session not found")
    return session


def _require_live_session(session_id: str, uid: str) -> dict:
    session = _require_session(session_id, uid)
    if session.get("cancel_requested") or session.get("recovery_required"):
        raise HTTPException(status_code=409, detail="sandbox access disabled for a stopped run")
    return session


@router.post("/sessions", response_model=SessionOut)
def start_session(
    body: SessionCreate,
    authorization: str | None = Header(default=None),
    uid: str = Depends(get_uid),
    idempotency_key: str = Depends(require_idempotency_key),
    _rl: None = Depends(rate_limit_dependency("mutate")),
):
    # Retain the historical kind default in command fingerprints so an exact
    # HTTP retry can replay a pre-selection receipt after an upgrade.
    if body.project_id is not None:
        from app.auth import get_verified_uid
        if get_verified_uid(authorization) != uid:
            raise HTTPException(status_code=401, detail="invalid token")
    payload = {**body.model_dump(exclude_unset=True), "kind": body.kind}
    return runs.submit(uid, idempotency_key, "create", payload)


@router.get("/sessions/{session_id}", response_model=SessionOut)
def read_session(session_id: str, uid: str = Depends(get_uid)):
    return _require_session(session_id, uid)


async def snapshot_stream(request, session_id, uid, authorization):
    gate = AuthorizationGate(authorization, uid, lambda: _require_session(session_id, uid))
    last_seen = None
    try:
        while not await request.is_disconnected():
            session = await read(_require_session, session_id, uid)
            bounded_session(session)
            payload = SessionOut(**session).model_dump_json()
            # Compare the actual public snapshot; timestamps/counts alone can
            # miss same-tick cancellation or approval transitions.
            await gate.ensure()
            if payload != last_seen:
                yield f"data: {payload}\n\n"
                last_seen = payload
            if session["status"] in _TERMINAL_STATUSES:
                return
            await asyncio.sleep(POLL_SECONDS)
    except AuthenticationError:
        yield 'event: error\ndata: {"error":"session access no longer authorized"}\n\n'
    except HTTPException:
        yield 'event: error\ndata: {"error":"session not found"}\n\n'
    except Exception:
        yield 'event: error\ndata: {"error":"session stream unavailable"}\n\n'


@router.get("/sessions/{session_id}/stream")
async def stream_session(request: Request, session_id: str,
                         authorization: str | None = Header(default=None), uid: str = Depends(get_uid)):
    """Step-level live updates via SSE - pushes the current SessionOut shape
    whenever it changes, closing once the session leaves a non-terminal status.

    NOTE: browsers' native `EventSource` cannot send an `Authorization` header,
    so this endpoint is for non-browser or fetch-based SSE clients, not
    `new EventSource(url)`. Don't pass the token as a query param instead -
    that leaks bearer tokens into access logs.
    """
    initial = await read(_require_session, session_id, uid)
    bounded_session(initial)
    return ManagedEventResponse(snapshot_stream(request, session_id, uid, authorization), admission.acquire(uid))


@router.post("/sessions/{session_id}/messages", response_model=SessionOut)
def send_message(
    session_id: str,
    body: MessageIn,
    uid: str = Depends(get_uid),
    idempotency_key: str = Depends(require_idempotency_key),
    _rl: None = Depends(rate_limit_dependency("mutate")),
):
    return runs.submit(uid, idempotency_key, "message", body.model_dump(exclude_unset=True), session_id)


@router.post("/sessions/{session_id}/approve", response_model=SessionOut)
def approve(
    session_id: str,
    body: ApproveIn,
    uid: str = Depends(get_uid),
    idempotency_key: str = Depends(require_idempotency_key),
    _rl: None = Depends(rate_limit_dependency("mutate")),
):
    return runs.submit(uid, idempotency_key, "approve", body.model_dump(), session_id)


@router.delete("/sessions/{session_id}", response_model=SessionOut)
def end_session(session_id: str, uid: str = Depends(get_uid)):
    return runs.cancel(session_id, uid)


@router.get("/sessions/{session_id}/preview", response_model=PreviewOut)
def preview(session_id: str, port: int, uid: str = Depends(get_uid)):
    _require_live_session(session_id, uid)
    try:
        return {"url": manager.preview_url(session_id, port)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/sessions/{session_id}/desktop", response_model=DesktopOut)
def desktop(session_id: str, uid: str = Depends(get_uid)):
    _require_live_session(session_id, uid)
    try:
        return {"stream_url": manager.desktop_stream_url(session_id)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/sessions/{session_id}/screenshot")
def screenshot(session_id: str, uid: str = Depends(get_uid)):
    _require_live_session(session_id, uid)
    try:
        png = manager.screenshot(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return Response(content=png, media_type="image/png")


@router.get("/sessions/{session_id}/trace", response_model=list[TraceStepOut])
def trace(session_id: str, uid: str = Depends(get_uid)):
    _require_session(session_id, uid)
    return get_trace(session_id)


@router.get("/memory")
def read_memory(uid: str = Depends(get_uid)):
    return {"notes": get_memory(uid)}


@router.delete("/memory")
def delete_memory(uid: str = Depends(get_uid)):
    clear_memory(uid)
    return {"status": "cleared"}

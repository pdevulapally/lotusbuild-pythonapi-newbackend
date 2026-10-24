import asyncio
import os

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response
from fastapi.responses import StreamingResponse

from app.agent.loop import provision_and_run, resume_turn_background, run_turn_background
from app.agent.memory import clear_memory, get_memory
from app.agent.tracing import get_trace
from app.auth import get_uid
from app.idempotency import require_idempotency_key, run_idempotent
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
from app.store import create_session, get_session, save_session

router = APIRouter()

SSE_POLL_INTERVAL_SECONDS = float(os.environ.get("SSE_POLL_INTERVAL_SECONDS", "1"))
_TERMINAL_STATUSES = {"idle", "error"}


def _require_session(session_id: str, uid: str) -> dict:
    session = get_session(session_id)
    if session is None or session["uid"] != uid:
        raise HTTPException(status_code=404, detail="session not found")
    return session


@router.post("/sessions", response_model=SessionOut)
def start_session(
    body: SessionCreate,
    background_tasks: BackgroundTasks,
    uid: str = Depends(get_uid),
    idempotency_key: str = Depends(require_idempotency_key),
    _rl: None = Depends(rate_limit_dependency("mutate")),
):
    def do():
        session = create_session(body.task, body.kind, uid)
        save_session(session)
        background_tasks.add_task(provision_and_run, session["id"], body.kind)
        return 200, session

    return run_idempotent(uid, "sessions:create", idempotency_key, do).body


@router.get("/sessions/{session_id}", response_model=SessionOut)
def read_session(session_id: str, uid: str = Depends(get_uid)):
    return _require_session(session_id, uid)


@router.get("/sessions/{session_id}/stream")
async def stream_session(session_id: str, uid: str = Depends(get_uid)):
    """Step-level live updates via SSE - pushes the current SessionOut shape
    whenever it changes, closing once the session leaves a non-terminal status.

    NOTE: browsers' native `EventSource` cannot send an `Authorization` header,
    so this endpoint is for non-browser or fetch-based SSE clients, not
    `new EventSource(url)`. Don't pass the token as a query param instead -
    that leaks bearer tokens into access logs.
    """
    _require_session(session_id, uid)

    async def events():
        last_seen = None
        while True:
            session = await asyncio.to_thread(get_session, session_id)
            if session is None or session["uid"] != uid:
                yield "event: error\ndata: session not found\n\n"
                return
            marker = (session["updated_at"], len(session["messages"]))
            if marker != last_seen:
                last_seen = marker
                yield f"data: {SessionOut(**session).model_dump_json()}\n\n"
            if session["status"] in _TERMINAL_STATUSES:
                return
            await asyncio.sleep(SSE_POLL_INTERVAL_SECONDS)

    return StreamingResponse(events(), media_type="text/event-stream")


@router.post("/sessions/{session_id}/messages", response_model=SessionOut)
def send_message(
    session_id: str,
    body: MessageIn,
    background_tasks: BackgroundTasks,
    uid: str = Depends(get_uid),
    idempotency_key: str = Depends(require_idempotency_key),
    _rl: None = Depends(rate_limit_dependency("mutate")),
):
    def do():
        session = _require_session(session_id, uid)
        if session["status"] in ("running", "awaiting_approval"):
            raise HTTPException(status_code=409, detail=f"session is {session['status']}")
        session["messages"].append({"role": "user", "content": [{"type": "text", "text": body.content}]})
        session["status"] = "running"
        save_session(session)
        background_tasks.add_task(run_turn_background, session_id)
        return 200, session

    return run_idempotent(uid, f"sessions:message:{session_id}", idempotency_key, do).body


@router.post("/sessions/{session_id}/approve", response_model=SessionOut)
def approve(
    session_id: str,
    body: ApproveIn,
    background_tasks: BackgroundTasks,
    uid: str = Depends(get_uid),
    idempotency_key: str = Depends(require_idempotency_key),
    _rl: None = Depends(rate_limit_dependency("mutate")),
):
    def do():
        session = _require_session(session_id, uid)
        if session["status"] != "awaiting_approval":
            raise HTTPException(status_code=409, detail="no pending approval on this session")
        risky_ids = {c["id"] for c in session["pending_approval"]["calls"] if c["risky"]}
        decided_ids = {d.call_id for d in body.decisions}
        if not risky_ids <= decided_ids:
            raise HTTPException(status_code=400, detail="must decide on every risky pending call")
        background_tasks.add_task(resume_turn_background, session_id, [d.model_dump() for d in body.decisions])
        return 200, session

    return run_idempotent(uid, f"sessions:approve:{session_id}", idempotency_key, do).body


@router.delete("/sessions/{session_id}")
def end_session(session_id: str, uid: str = Depends(get_uid)):
    _require_session(session_id, uid)
    manager.close(session_id)
    return {"status": "closed"}


@router.get("/sessions/{session_id}/preview", response_model=PreviewOut)
def preview(session_id: str, port: int, uid: str = Depends(get_uid)):
    _require_session(session_id, uid)
    try:
        return {"url": manager.preview_url(session_id, port)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/sessions/{session_id}/desktop", response_model=DesktopOut)
def desktop(session_id: str, uid: str = Depends(get_uid)):
    _require_session(session_id, uid)
    try:
        return {"stream_url": manager.desktop_stream_url(session_id)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/sessions/{session_id}/screenshot")
def screenshot(session_id: str, uid: str = Depends(get_uid)):
    _require_session(session_id, uid)
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

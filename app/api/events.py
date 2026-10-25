"""Bounded, resumable lifecycle events from the authoritative run repository."""
import asyncio
import json

from fastapi import APIRouter, Depends, Header, Path, Request

from app.auth import AuthenticationError, get_uid
from app.execution.repository import CommandError, runs
from app.api.streaming import (
    AuthorizationGate, ManagedEventResponse, StreamAdmission, admission,
    POLL_SECONDS, read as _read,
)


router = APIRouter()


def frame(event):
    prefix = f"id: {event['id']}\n" if "id" in event else ""
    return f"{prefix}event: {event['type']}\ndata: {json.dumps(event['data'], separators=(',', ':'))}\n\n"


def closing(reason):
    return frame({"type": "stream.closing", "data": {"reason": reason}})


async def event_stream(request, repository, session_id, uid, authorization, after):
    gate = AuthorizationGate(authorization, uid, lambda: repository.event_history(session_id, uid, after, 1))
    try:
        while not await request.is_disconnected():
            # Recheck ownership on every page, including idle/terminal sessions.
            page = await _read(repository.event_history, session_id, uid, after)
            for event in page:
                if await request.is_disconnected():
                    return
                await gate.ensure()
                yield frame(event)
                after = event["id"]
            if len(page) == 100:
                continue
            await gate.ensure()
            yield ": keep-alive\n\n"
            await asyncio.sleep(POLL_SECONDS)
    except AuthenticationError as exc:
        yield closing("token-expired" if exc.expired else "authorization-failed")
    except CommandError as exc:
        yield closing("authorization-failed" if exc.status == 404 else "history-unavailable")
    except Exception:
        # No token, prompt, URL, provider payload or exception text in the frame.
        yield closing("history-unavailable")


@router.get("/sessions/{session_id}/events")
async def events(
    request: Request,
    session_id: str = Path(pattern=r"^[A-Za-z0-9_-]{1,128}$"),
    authorization: str | None = Header(default=None),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID", max_length=41),
    uid: str = Depends(get_uid),
):
    # Validate ownership/cursor before sending successful streaming headers.
    await _read(runs.event_history, session_id, uid, last_event_id)
    release = admission.acquire(uid)
    return ManagedEventResponse(
        event_stream(request, runs, session_id, uid, authorization, last_event_id), release)

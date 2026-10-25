"""Shared stream authorization, I/O bounds and socket admission."""
import asyncio
from collections import Counter
import os
import threading
import time

from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from starlette.requests import ClientDisconnect

from app.auth import AuthenticationError, verify_identity


def _positive_setting(name, default):
    value = float(os.environ.get(name, default))
    if not 0 < value <= 3600:
        raise ValueError(f"invalid {name}")
    return value


POLL_SECONDS = _positive_setting("SSE_POLL_INTERVAL_SECONDS", "1")
IO_TIMEOUT_SECONDS = _positive_setting("SSE_OPERATION_TIMEOUT_SECONDS", "10")
AUTH_RECHECK_SECONDS = 1.0


async def read(function, *args):
    # Ending this wait cannot forcibly kill a blocking SDK thread.
    return await asyncio.wait_for(asyncio.to_thread(function, *args), IO_TIMEOUT_SECONDS)


class AuthorizationGate:
    """Check expiry before every emission; refresh revocation/ownership each second."""
    def __init__(self, authorization, uid, owned, *, clock=time.time, monotonic=time.monotonic):
        self.authorization, self.uid, self.owned = authorization, uid, owned
        self.clock, self.monotonic = clock, monotonic
        self.expires_at, self.recheck_at = float("inf"), 0.0

    async def ensure(self):
        if self.clock() >= self.expires_at:
            raise AuthenticationError("invalid token", expired=True)
        if self.monotonic() >= self.recheck_at:
            checked_at = self.monotonic()
            identity = await read(verify_identity, self.authorization)
            if identity.uid != self.uid:
                raise AuthenticationError("invalid token")
            await read(self.owned)
            self.expires_at = identity.expires_at
            if self.clock() >= self.expires_at:
                raise AuthenticationError("invalid token", expired=True)
            if self.monotonic() >= checked_at + AUTH_RECHECK_SECONDS:
                raise AuthenticationError("invalid token")
            self.recheck_at = min(checked_at + AUTH_RECHECK_SECONDS,
                                  self.monotonic() + self.expires_at - self.clock())


class StreamAdmission:
    def __init__(self, total=100, per_user=2):
        self.total, self.per_user = total, per_user
        self.counts, self.lock = Counter(), threading.Lock()

    def acquire(self, uid):
        with self.lock:
            if self.counts[uid] >= self.per_user:
                raise HTTPException(status_code=429, detail="close an existing event stream before opening another")
            if sum(self.counts.values()) >= self.total:
                raise HTTPException(status_code=503, detail="event stream capacity reached")
            self.counts[uid] += 1
        released = False

        def release():
            nonlocal released
            with self.lock:
                if not released:
                    self.counts[uid] -= 1
                    if not self.counts[uid]:
                        del self.counts[uid]
                    released = True
        return release


admission = StreamAdmission()


class ManagedEventResponse(StreamingResponse):
    def __init__(self, content, release):
        super().__init__(content, media_type="text/event-stream", headers={
            "Cache-Control": "no-store, no-transform", "X-Accel-Buffering": "no"})
        self.release = release

    async def __call__(self, scope, receive, send):
        async def bounded_send(message):
            await asyncio.wait_for(send(message), IO_TIMEOUT_SECONDS)

        try:
            await super().__call__(scope, receive, bounded_send)
        except (TimeoutError, ClientDisconnect):
            return  # A blocked/disconnected peer cannot receive a closing frame.
        finally:
            try:
                close = getattr(self.body_iterator, "aclose", None)
                if close:
                    await close()
            finally:
                self.release()

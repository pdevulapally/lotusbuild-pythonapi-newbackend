import os

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request

from app.api.routes import router
from app.billing.routes import router as billing_router
from app.github.routes import router as github_router
from app.idempotency import IdempotencyConflict
from app.execution.repository import CommandError
from app.ratelimit import RateLimitMiddleware
from app.security_log import log_security_event

# "*" (default) means unrestricted - set explicitly in production. Empty ALLOWED_ORIGINS
# means no cross-origin browser access at all until configured - never defaults to "*".
ALLOWED_HOSTS = [h.strip() for h in os.environ.get("ALLOWED_HOSTS", "*").split(",") if h.strip()]
ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()]


class SecurityHeadersMiddleware:
    """ASGI middleware - adds standard hardening headers to every response."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).extend(
                    [
                        (b"strict-transport-security", b"max-age=63072000; includeSubDomains"),
                        (b"x-content-type-options", b"nosniff"),
                        (b"x-frame-options", b"DENY"),
                        (b"referrer-policy", b"no-referrer"),
                        (b"permissions-policy", b"geolocation=(), microphone=(), camera=()"),
                    ]
                )
            await send(message)

        await self.app(scope, receive, send_with_headers)


app = FastAPI(title="Lotus Build Agent API")

# Added outermost-first: Host check -> CORS -> response headers -> rate limit.
app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Authorization", "Idempotency-Key", "Content-Type"],
    allow_credentials=False,
)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RateLimitMiddleware)

app.include_router(router)
app.include_router(billing_router)
app.include_router(github_router)


@app.exception_handler(IdempotencyConflict)
async def idempotency_conflict_handler(request: Request, exc: IdempotencyConflict):
    return JSONResponse({"detail": str(exc)}, status_code=409)


@app.exception_handler(CommandError)
async def command_error_handler(request: Request, exc: CommandError):
    return JSONResponse({"detail": str(exc)}, status_code=exc.status)


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    log_security_event("unhandled_exception", path=request.url.path, error=str(exc))
    return JSONResponse({"detail": "internal server error"}, status_code=500)


@app.get("/health")
def health():
    return {"status": "ok"}

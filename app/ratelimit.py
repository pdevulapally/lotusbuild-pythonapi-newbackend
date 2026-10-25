import os

import redis
from fastapi import Depends, HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from app.api.errors import error_response

from app.auth import get_uid
from app.security_log import log_security_event

_redis = redis.Redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"), decode_responses=True)

# (max_requests, window_seconds) per bucket. Thresholds are env-overridable data;
# the bucket set itself is code, per this project's config convention.
RATE_LIMITS = {
    "anon": (int(os.environ.get("RATE_LIMIT_ANON", "60")), 60),
    "mutate": (int(os.environ.get("RATE_LIMIT_MUTATE", "20")), 60),
    "webhook": (int(os.environ.get("RATE_LIMIT_WEBHOOK", "100")), 60),
}


def check_rate_limit(bucket: str, identity: str) -> None:
    """Raises HTTPException(429) if `identity` is over `bucket`'s limit.

    Fails OPEN on a Redis connection error (logs it, allows the request) -
    availability over strict abuse defense; flip to fail-closed if abuse
    patterns ever show that's the wrong tradeoff.
    """
    limit, window_seconds = RATE_LIMITS[bucket]
    redis_key = f"ratelimit:{bucket}:{identity}"
    try:
        pipe = _redis.pipeline()
        pipe.incr(redis_key)
        pipe.expire(redis_key, window_seconds, nx=True)
        count, _ = pipe.execute()
    except redis.RedisError as exc:
        log_security_event("rate_limit_backend_unavailable", bucket=bucket, error_type=type(exc).__name__)
        return
    if count > limit:
        log_security_event("rate_limit_exceeded", bucket=bucket, identity=identity, count=count)
        raise HTTPException(status_code=429, detail=f"rate limit exceeded for {bucket!r}")


def rate_limit_dependency(bucket: str):
    """FastAPI dependency factory: a per-uid rate limit layer on top of the
    global per-IP middleware below, reusing the same check_rate_limit."""

    def _dep(uid: str = Depends(get_uid)) -> None:
        check_rate_limit(bucket, uid)

    return _dep


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Per-IP limiter applied to every request, including unauthenticated ones -
    the abuse/brute-force layer independent of per-plan usage quotas."""

    async def dispatch(self, request: Request, call_next):
        bucket = "webhook" if request.url.path == "/billing/webhook" else "anon"
        identity = request.client.host if request.client else "unknown"
        try:
            check_rate_limit(bucket, identity)
        except HTTPException as exc:
            return error_response(exc.status_code, exc.detail, code="request/rate-limited")
        return await call_next(request)

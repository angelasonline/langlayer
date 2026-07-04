"""In-process rate limiting (single-instance deployment, per PRODUCTION.md).

Token buckets per (client IP, endpoint class). Multi-instance deployments
move these buckets to Redis; the interface stays the same.
"""
from __future__ import annotations

import time

from fastapi import HTTPException, Request

# endpoint class -> (max tokens, refill per second)
LIMITS = {
    "space_create": (5, 5 / 3600),      # 5 per hour
    "join": (30, 30 / 60),              # 30 per minute
    "ingest": (60, 60 / 60),            # 60 announcements per minute
    "speak": (20, 20 / 60),
    "read": (240, 240 / 60),
}

_buckets: dict[tuple[str, str], list[float]] = {}   # key -> [tokens, last_ts]


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return (fwd.split(",")[0].strip() if fwd else
            (request.client.host if request.client else "unknown"))


def check(request: Request, kind: str) -> None:
    cap, rate = LIMITS[kind]
    key = (_client_ip(request), kind)
    now = time.monotonic()
    tokens, last = _buckets.get(key, [float(cap), now])
    tokens = min(cap, tokens + (now - last) * rate)
    if tokens < 1:
        raise HTTPException(429, "Too many requests. Please slow down.")
    _buckets[key] = [tokens - 1, now]


def limiter(kind: str):
    """FastAPI dependency: Depends(limiter("join"))."""
    def dep(request: Request) -> None:
        check(request, kind)
    return dep

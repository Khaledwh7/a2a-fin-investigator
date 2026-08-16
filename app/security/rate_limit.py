"""Rate limiting + request-size guards, as HTTP middleware.

A token-bucket limiter keyed by client host. Portfolio-scale: in-process state,
one bucket per client. In a multi-instance deployment you'd move the buckets to
Redis, but the algorithm is identical.

``install_http_guards`` also rejects over-sized request bodies (413) before they
are read into memory.
"""

from __future__ import annotations

import math
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class RateLimiter:
    def __init__(self, requests_per_minute: int, burst: int) -> None:
        self._rate = requests_per_minute / 60.0   # tokens per second
        self._burst = float(burst)
        self._buckets: dict[str, tuple[float, float]] = {}  # key -> (tokens, last_ts)

    def allow(self, key: str) -> tuple[bool, float]:
        """Return (allowed, retry_after_seconds)."""
        now = time.monotonic()
        tokens, last = self._buckets.get(key, (self._burst, now))
        tokens = min(self._burst, tokens + (now - last) * self._rate)
        if tokens >= 1.0:
            self._buckets[key] = (tokens - 1.0, now)
            return True, 0.0
        self._buckets[key] = (tokens, now)
        return False, (1.0 - tokens) / self._rate if self._rate > 0 else 60.0


def install_http_guards(app: FastAPI, *, max_bytes: int,
                        limiter: RateLimiter | None) -> None:
    @app.middleware("http")
    async def guards(request: Request, call_next):  # noqa: ANN202
        # 1) request-size guard (before reading the body)
        content_length = request.headers.get("content-length")
        if content_length and content_length.isdigit() and int(content_length) > max_bytes:
            return JSONResponse({"error": "request too large"}, status_code=413)

        # 2) rate limit (per client host)
        if limiter is not None:
            key = request.client.host if request.client else "anonymous"
            allowed, retry_after = limiter.allow(key)
            if not allowed:
                return JSONResponse(
                    {"error": "rate limit exceeded"}, status_code=429,
                    headers={"Retry-After": str(math.ceil(retry_after))})

        return await call_next(request)

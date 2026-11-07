"""Per-client-IP rate limiting for the expensive endpoints.

Every /query costs several LLM calls (planner, grader, responder, verifier) billed to
our provider keys, and every /documents upload costs embedding calls. A public URL
without a limit lets one client burn the whole quota or budget in minutes.

Algorithm: sliding-window log. Each client keeps the timestamps of its recent hits;
a request is allowed when fewer than `count` hits fall inside the last `period`
seconds. Unlike a fixed window, a client can't double its budget by bursting at a
window boundary.

Limits are in process memory, so they apply PER INSTANCE: with Cloud Run max 2
instances a client can get at most 2x the limit, which is acceptable here. A
multi-instance deployment that needs exact global limits would use Redis instead.

Configuration (read on each request, validated at startup):
  RATE_LIMIT_QUERY     default "20/minute"   /query and /query/stream (one shared budget);
                                             also /feedback and /analytics (one budget each)
  RATE_LIMIT_UPLOAD    default "5/minute"    /documents
  TRUST_PROXY_HEADERS  default false         use X-Forwarded-For (see client_ip())
"""

import math
import os
import threading
import time
from collections import deque
from collections.abc import Callable

from fastapi import HTTPException, Request

QUERY_LIMIT_ENV, QUERY_LIMIT_DEFAULT = "RATE_LIMIT_QUERY", "20/minute"
UPLOAD_LIMIT_ENV, UPLOAD_LIMIT_DEFAULT = "RATE_LIMIT_UPLOAD", "5/minute"

_PERIODS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}

# Sweep idle clients once this many are tracked, so memory can't grow without bound.
_SWEEP_THRESHOLD = 10_000


def parse_rate(value: str) -> tuple[int, int]:
    """'20/minute' → (20, 60). Accepts second/minute/hour/day, singular or plural."""
    try:
        count_text, period_text = value.strip().split("/", 1)
        count = int(count_text)
        period = _PERIODS[period_text.strip().lower().removesuffix("s")]
    except (AttributeError, ValueError, KeyError):
        raise ValueError(
            f"Invalid rate limit {value!r}. Use '<count>/<second|minute|hour|day>', e.g. '20/minute'."
        ) from None
    if count < 1:
        raise ValueError(f"Invalid rate limit {value!r}: count must be at least 1.")
    return count, period


def configured_rate(env_var: str, default: str) -> tuple[int, int]:
    return parse_rate(os.getenv(env_var) or default)


def validate_rate_limits() -> None:
    """Fail fast at startup on a mistyped limit instead of on the first request."""
    configured_rate(QUERY_LIMIT_ENV, QUERY_LIMIT_DEFAULT)
    configured_rate(UPLOAD_LIMIT_ENV, UPLOAD_LIMIT_DEFAULT)


def trust_proxy_headers() -> bool:
    return os.getenv("TRUST_PROXY_HEADERS", "false").strip().lower() in {"true", "1", "yes", "on"}


def client_ip(request: Request) -> str:
    """
    The address the limit is keyed on.

    X-Forwarded-For is an ordinary request header: any client can send
    "X-Forwarded-For: 1.2.3.4" and, if we believed it, get a fresh budget on every
    request by changing the value, i.e. no rate limit at all. So it is used only when
    TRUST_PROXY_HEADERS=true, which means a proxy we control (Cloud Run's front end)
    always sits in front of the app. Even then, the LEFTMOST entries are still
    client-supplied: the proxy appends the address it actually saw to whatever the
    client sent. Only the RIGHTMOST entry was written by our proxy, so that is the one
    used. Without the flag, the direct TCP peer address is used.
    """
    if trust_proxy_headers():
        forwarded = request.headers.get("x-forwarded-for", "")
        hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
        if hops:
            return hops[-1]
    return request.client.host if request.client else "unknown"


class SlidingWindowLimiter:
    """Thread-safe in-memory sliding-window log (sync endpoints run in a thread pool)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()
        # Longest window in use: the sweep must not drop a client that a longer-period
        # limit (e.g. 5/hour) still has to remember.
        self._max_period = 0

    def hit(self, key: str, count: int, period: int) -> float | None:
        """Records one request. Returns None if allowed, else seconds until a slot frees up."""
        now = self._clock()
        with self._lock:
            self._max_period = max(self._max_period, period)
            if len(self._hits) > _SWEEP_THRESHOLD:
                self._sweep(now)
            window = self._hits.setdefault(key, deque())
            while window and window[0] <= now - period:
                window.popleft()
            if len(window) >= count:
                return window[0] + period - now
            window.append(now)
            return None

    def _sweep(self, now: float) -> None:
        for key in [k for k, hits in self._hits.items() if not hits or hits[-1] <= now - self._max_period]:
            del self._hits[key]

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


limiter = SlidingWindowLimiter()


def rate_limit(scope: str, env_var: str, default: str) -> Callable[[Request], None]:
    """
    FastAPI dependency enforcing `env_var`'s limit per client IP. Endpoints sharing a
    `scope` share one budget. Raises 429 with Retry-After when the limit is exceeded.
    """

    def dependency(request: Request) -> None:
        count, period = configured_rate(env_var, default)
        retry_after = limiter.hit(f"{scope}:{client_ip(request)}", count, period)
        if retry_after is not None:
            seconds = max(1, math.ceil(retry_after))
            raise HTTPException(
                status_code=429,
                detail=(
                    f"Rate limit exceeded: at most {count} {scope} requests per {period} s from one client. "
                    f"Try again in {seconds} s."
                ),
                headers={"Retry-After": str(seconds)},
            )

    return dependency


limit_queries = rate_limit("query", QUERY_LIMIT_ENV, QUERY_LIMIT_DEFAULT)
limit_uploads = rate_limit("upload", UPLOAD_LIMIT_ENV, UPLOAD_LIMIT_DEFAULT)
# Same limit as /query (RATE_LIMIT_QUERY), separate budgets: voting must not use up a
# user's question budget, and /analytics failures (wrong key) can't be retried at will.
limit_feedback = rate_limit("feedback", QUERY_LIMIT_ENV, QUERY_LIMIT_DEFAULT)
limit_analytics = rate_limit("analytics", QUERY_LIMIT_ENV, QUERY_LIMIT_DEFAULT)

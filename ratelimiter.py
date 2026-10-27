"""Context-aware, in-memory rate limiter (fixed window) for Flask."""
import math
import threading
import time

from flask import jsonify, request

# (tier, endpoint_type) -> (max_requests, window_seconds)
RULES = {
    ("free", "ai"): (5, 60),
    ("free", "read"): (30, 60),
    ("paid", "ai"): (30, 60),
    ("paid", "read"): (120, 60),
}
VALID_TIERS = {"free", "paid"}
DEFAULT_TIER = "free"
DEFAULT_ENDPOINT_TYPE = "read"  # for routes someone forgot to tag
TAG_ATTR = "_rate_limit_type"


def rate_limit(endpoint_type):
    """Route-level tag. Does not wrap the handler; it only labels it."""
    if endpoint_type not in {e for _, e in RULES}:
        raise ValueError(f"unknown endpoint type: {endpoint_type!r}")

    def tag(fn):
        setattr(fn, TAG_ATTR, endpoint_type)
        return fn

    return tag


class RateLimitStore:
    """key -> [count, window_start]. One lock makes check+increment atomic."""

    def __init__(self, clock=time.monotonic, sweep_every=1000):
        self._data = {}
        self._lock = threading.Lock()
        self._clock = clock
        self._sweep_every = sweep_every
        self._calls = 0
        self._max_window = max(w for _, w in RULES.values())

    def hit(self, key, limit, window):
        """Returns (allowed, retry_after_seconds)."""
        with self._lock:
            now = self._clock()
            self._calls += 1
            if self._calls % self._sweep_every == 0:
                self._sweep(now)

            entry = self._data.get(key)
            if entry is None or now - entry[1] >= window:
                entry = [0, now]  # new / elapsed window: reset
                self._data[key] = entry

            if entry[0] >= limit:
                remaining = window - (now - entry[1])
                return False, max(0, math.ceil(remaining))

            entry[0] += 1
            return True, 0

    def _sweep(self, now):
        stale = [k for k, (_, start) in self._data.items()
                 if now - start >= self._max_window]
        for k in stale:
            del self._data[k]

    def size(self):
        with self._lock:
            return len(self._data)


def resolve_tier(headers):
    tier = (headers.get("X-User-Tier") or "").strip().lower()
    return tier if tier in VALID_TIERS else DEFAULT_TIER


def resolve_user_id(headers, remote_addr):
    uid = (headers.get("X-User-Id") or "").strip()
    return uid or remote_addr or "anonymous"


def init_rate_limiter(app, store=None):
    store = store or RateLimitStore()
    app.extensions["rate_limit_store"] = store

    @app.before_request
    def enforce():
        view = app.view_functions.get(request.endpoint)
        if view is None:  # 404 etc: nothing to protect
            return None
        endpoint_type = getattr(view, TAG_ATTR, DEFAULT_ENDPOINT_TYPE)
        tier = resolve_tier(request.headers)
        user = resolve_user_id(request.headers, request.remote_addr)
        limit, window = RULES[(tier, endpoint_type)]

        allowed, retry_after = store.hit((user, tier, endpoint_type), limit, window)
        if allowed:
            return None
        resp = jsonify({
            "error": "rate_limit_exceeded",
            "limit": limit,
            "window_seconds": window,
            "retry_after_seconds": retry_after,
        })
        resp.status_code = 429
        resp.headers["Retry-After"] = str(retry_after)
        return resp

    return store

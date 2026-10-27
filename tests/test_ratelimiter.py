import sys, os, threading
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import pytest
from app import create_app
from ratelimiter import RateLimitStore


class Clock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t


@pytest.fixture
def env():
    clock = Clock()
    app = create_app(RateLimitStore(clock=clock))
    return app.test_client(), clock


def hit(c, method, path, tier=None, user="u1"):
    h = {"X-User-Id": user}
    if tier is not None:
        h["X-User-Tier"] = tier
    return getattr(c, method)(path, headers=h)


@pytest.mark.parametrize("tier,method,path,limit", [
    ("free", "post", "/ai/generate", 5),
    ("free", "get", "/data/list", 30),
    ("paid", "post", "/ai/summarise", 30),
    ("paid", "get", "/data/export", 120),
])
def test_each_rule(env, tier, method, path, limit):
    c, _ = env
    for _ in range(limit):
        assert hit(c, method, path, tier).status_code == 200
    r = hit(c, method, path, tier)
    assert r.status_code == 429
    body = r.get_json()
    assert body["limit"] == limit and body["window_seconds"] == 60
    assert body["error"] == "rate_limit_exceeded"


def test_missing_and_invalid_header_default_to_free(env):
    c, _ = env
    for tier in (None, "gold", ""):
        user = f"u-{tier}"
        for _ in range(5):
            assert hit(c, "post", "/ai/generate", tier, user).status_code == 200
        assert hit(c, "post", "/ai/generate", tier, user).status_code == 429


def test_retry_after_and_reset(env):
    c, clock = env
    for _ in range(5):
        hit(c, "post", "/ai/generate", "free")
    clock.t += 26
    r = hit(c, "post", "/ai/generate", "free")
    assert r.get_json()["retry_after_seconds"] == 34
    clock.t += 33.5
    r = hit(c, "post", "/ai/generate", "free")
    assert r.status_code == 429 and r.get_json()["retry_after_seconds"] >= 0
    clock.t += 1
    assert hit(c, "post", "/ai/generate", "free").status_code == 200


def test_buckets_are_independent(env):
    c, _ = env
    for _ in range(5):
        hit(c, "post", "/ai/generate", "free")
    assert hit(c, "post", "/ai/generate", "free").status_code == 429
    assert hit(c, "post", "/ai/summarise", "free").status_code == 429  # same type, shared
    assert hit(c, "get", "/data/list", "free").status_code == 200       # other type
    assert hit(c, "post", "/ai/generate", "free", "other").status_code == 200


def test_concurrency_does_not_overshoot(env):
    c, _ = env
    results = []
    def worker():
        results.append(hit(c, "post", "/ai/generate", "free").status_code)
    ts = [threading.Thread(target=worker) for _ in range(50)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert results.count(200) == 5 and results.count(429) == 45

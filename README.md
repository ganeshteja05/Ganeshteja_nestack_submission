# Context-Aware Rate Limiter (Nestack Take-Home)

**Live deployment:** `<PASTE YOUR DEPLOYMENT URL HERE>`

A Flask service whose middleware applies a different rate limit depending on who is calling (free or paid tier) and what they are calling (AI/heavy or read/light endpoint). All state lives in process memory. There is no Redis, no database, and no rate-limiting library.

## How to run

You need Python 3.9 or newer.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python app.py                      # dev server on http://localhost:8000
# or, closer to production:
gunicorn app:app --workers 1 --threads 8 --bind 0.0.0.0:8000
```

Try it:

```bash
for i in $(seq 1 6); do
  curl -s -o /dev/null -w "%{http_code}\n" -X POST localhost:8000/ai/generate \
       -H "X-User-Id: alice" -H "X-User-Tier: free"
done
# five 200s, then a 429 with the JSON error body
```

Run the tests with `python -m pytest -q`. They cover all four limit cells, header defaulting, window reset, `retry_after_seconds`, bucket independence and 50 concurrent threads. The tests use an injected fake clock, so they do not sleep.

The gunicorn command deliberately uses a single worker with several threads. See Limitations for why. `Procfile` and `render.yaml` are included for deployment.

Optional request headers: `X-User-Tier` (`free` or `paid`) and `X-User-Id` (the caller identity; the client IP is used if it is absent).

## Design decisions

**Middleware plus a route tag.** `rate_limit("ai")` is a decorator that only attaches a label to the handler function and returns it unchanged. A single `before_request` hook looks up the handler for the matched route, reads its label, and enforces the rule. The URL is never inspected, so renaming `/ai/generate` to `/v2/gen` changes nothing, and a new route is protected simply by tagging it. Routes that nobody tagged fall back to the `read` type rather than going unprotected, because failing closed is safer than silently exempting something.

**Fixed window per key.** Each key is `(user id, tier, endpoint type)` and holds a request count and a window start time. When a request arrives and the window has elapsed, the entry is reset. A fixed window is exactly what the spec describes (N requests per 60 seconds, with a reset), costs O(1) memory per key, and makes `retry_after_seconds` trivial: window length minus time elapsed. I chose it over sliding-window log or token bucket because those give smoother behaviour but are not what the rule table asks for, and they store more per key.

**Endpoint type is the bucket, not the route.** `/ai/generate` and `/ai/summarise` share one "ai" bucket per user. A user's budget is for expensive work in general, so splitting it by route would let them multiply the limit by the number of AI routes.

**Atomic check-and-increment.** The store's `hit()` does the window check, the limit comparison and the increment under one `threading.Lock`. Without it, two threads could both read `count == 4` and both be admitted, overshooting the limit. A global lock is simple and correct. Critical sections are a few dictionary operations, so contention is not a concern at this scale.

**Monotonic clock.** Window timing uses `time.monotonic()`, so changes to the system clock (NTP corrections, manual changes) cannot extend or shorten a window or make `retry_after_seconds` negative. The clock is injectable, which is what lets the tests verify reset behaviour without sleeping.

**Retry-after.** On rejection the remaining time is rounded up with `ceil` and clamped at 0. Rounding up means a client that waits that many seconds will not be rejected again. The same value is sent in the standard `Retry-After` header.

**Tier parsing.** The header is trimmed and lower-cased. Missing, empty or unrecognised values mean `free`, the most restrictive tier.

**Memory hygiene.** Expired entries are swept every 1000 requests under the same lock, so memory is bounded by the number of users active within one window, not all users ever seen.

## Limitations

- **Single process only.** State lives in one process's memory. If you run several gunicorn workers or several instances behind a load balancer, each has its own counters, and a free user could get 5 AI requests per worker per minute. That is why the deployment uses one worker. Scaling horizontally would need shared state (Redis or similar), which this assignment excludes.
- **State is lost on restart or deploy.** A crash or redeploy resets every counter, so a client can get a fresh budget by timing requests around a restart. On platforms with sleeping free tiers the process may also spin down.
- **The tier header is trusted.** Anyone can send `X-User-Tier: paid`. In a real system the tier would come from a verified token or session, not a client-supplied header. Likewise `X-User-Id` is self-declared: a caller can rotate it to get a new bucket each time. When it is absent we fall back to the IP address, which is wrong behind a proxy or NAT (many users share one IP, or all traffic appears to come from the proxy) unless forwarded-IP handling is configured.
- **Fixed-window burst.** A client can send the full limit at the end of one window and the full limit again at the start of the next, so up to 2x the limit can pass in a short span around the boundary. A sliding window would prevent this at the cost of more memory per key.
- **One global lock.** It serialises all rate-limit checks within the process. This is fine here, but it would become a bottleneck under very heavy load.
- **Worst-case memory.** Between sweeps, an attacker rotating user IDs can create many keys. Entries are small and expire, but there is no hard cap.
- **Blocked requests still count as traffic on the wire.** Rejected requests are not added to the counter, so a client that keeps hammering after a 429 is not penalised further.

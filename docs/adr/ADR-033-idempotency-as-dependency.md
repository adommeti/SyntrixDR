# ADR-033 — Idempotency-Key is a FastAPI dependency + explicit `complete()`, not ASGI middleware

**Status:** Accepted
**Date:** 2026-09-13
**Increment:** BUILD-01
**Relates to:** D-215 (`Idempotency-Key` required on every command POST; 400 `IDEMPOTENCY_KEY_REQUIRED`;
replay of stored outcome; 24 h TTL)

## Context
D-215 decides *what* the Idempotency-Key contract is; it doesn't decide *how* it's wired into every
command route. ASGI middleware can inspect the request/response generically, but it can't cleanly
short-circuit a route *before* the route's business logic runs and skip re-execution on replay without
either buffering/re-injecting the whole ASGI message flow or reaching into route internals — both fragile
for a monolith with dozens of command routes to come.

## Decision
`apps/api/app/core/idempotency.py` exposes `require_idempotency_key(request, session, user_id, clock)` as
a FastAPI dependency and a separate `complete(session, ctx, status_code, body)` call. Every command route
follows the same contract:

```python
ctx = await require_idempotency_key(request, session, user_id, clock)
if ctx.is_replay:
    return JSONResponse(status_code=ctx.stored_status, content=ctx.stored_body)
... business logic ...
await complete(session, ctx, status_code, body)
```

`require_idempotency_key` raises `IdempotencyKeyRequiredError` (400) on a missing header, resets an
already-*expired* `(key, user, route)` row in place (the unique constraint has no time dimension, so a
second insert after expiry would violate it), and raises `IdempotencyInProgressError` (409) on a genuine
concurrent race for a fresh key (insert wrapped in `session.begin_nested()`, `IntegrityError` → 409).

## Alternatives rejected
- **ASGI middleware wrapping the whole app** — rejected: can gate on the header (a dependency does that
  fine too) but can't avoid running the route handler's business logic before knowing whether this is a
  replay, without much more machinery than a plain dependency + explicit early-return.
- **A decorator around each route function** — rejected: FastAPI's own dependency injection already does
  this job and composes with per-route `Depends()` overrides used in tests; a decorator would be a second,
  parallel mechanism for the same thing.

## Consequences
- Every future command route (BUILD-02 onward) must include the `if ctx.is_replay: return ...` guard
  explicitly — there is no framework-level enforcement that a route remembered to check it. `/drcc-transition-service`
  should reference this contract in its template/checklist so it isn't reintroduced ad hoc per module.
- **Resolved** (issue #4, fixed ahead of BUILD-02): the expired-key-reset path's SELECT now uses
  `.with_for_update()`. A concurrent request racing on the same expired key blocks on the row lock until
  the winner's transaction commits, then re-reads the winner's now-current (non-expired, completed) row
  and takes the active-row replay branch instead of also resetting and re-executing —
  `apps/api/tests/test_idempotency.py::test_concurrent_expired_key_reset_is_not_a_double_execution`.
- BUILD-01 proved this contract only against a test-only dummy route (`apps/api/tests/test_idempotency.py`)
  since no domain command endpoint exists yet — not a gap, just the honest state of an increment with zero
  command endpoints.

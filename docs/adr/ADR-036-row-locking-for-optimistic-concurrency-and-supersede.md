# ADR-036 — Row-level locking for `expected_version` checks and append-only supersede writes

**Status:** Accepted
**Date:** 2026-09-14
**Increment:** BUILD-03
**Relates to:** invariant #22 (D-214 stale-write handling), invariant #11 (audit/append-only), `apps/api/app/applications_catalog/commands.py`, `apps/api/app/policies_admin/commands.py`

## Context
BUILD-03's first review pass implemented `expected_version` checks as plain sequential reads: `SELECT` the
row, compare `.version` to the caller's `expected_version`, then mutate and increment. An independent
second-opinion review correctly identified that this is not atomic — two concurrent requests can both read
the same version, both pass the check, and both commit as `version + 1`, silently discarding one write. The
sequential-request tests already in the suite (create, then PATCH twice with a stale key) could not have
caught this, because they only exercise the check's *logic*, not its behavior under real concurrent
transactions. The same class of race existed in `policies_admin`'s supersede-then-insert write path: two
concurrent writes to the same `(policy_definition_id, scope_type, scope_id)` could both see no active row
and both insert, leaving two simultaneously-active rows for the same key/scope — there is no partial unique
index in the frozen schema to catch this at the database level (`policy_values`'s only constraint is the
`policy_scope_ck` CHECK, not a `WHERE superseded_at IS NULL` unique index), and adding one would require a
migration this module has no product mandate to make.

## Decision
Two related, narrower locking patterns, chosen per situation rather than one generic mechanism:
1. **Optimistic-concurrency checks on a versioned row** (`Application.version`, `Tier.version`, and any
   future versioned aggregate) must read the row with `SELECT ... FOR UPDATE` — in SQLAlchemy 2 async,
   `session.get(Model, pk, with_for_update=True)` or `select(...).with_for_update()` — *before* comparing
   `expected_version`. This forces a second concurrent transaction to block until the first commits, so it
   re-reads the now-incremented version and correctly raises `ConcurrencyConflictError` instead of racing.
2. **Append-only supersede writes with no partial unique index to lean on** (`policy_values`, and any future
   table with the same "one active row per logical key, older rows kept for history" shape) must take a
   `pg_advisory_xact_lock(hashtext(<definition_id>:<scope_type>:<scope_id>))` before the read-supersede-insert
   sequence. The lock is transaction-scoped and released automatically at commit/rollback; it serializes
   writers to the same logical key/scope without requiring a schema change.

## Alternatives rejected
- **A single generic `with_for_update` for both cases** — doesn't fit the supersede case, where there is no
  single row to lock before the first write ever happens (two concurrent *first* writes both see "no active
  row" and neither has anything to lock).
- **A partial unique index (`WHERE superseded_at IS NULL`) on `policy_values`** — would close the race at
  the database level and is arguably the more idiomatic Postgres answer, but requires a new migration
  (`0003+`) that this increment has no product-driven reason to ship; revisit if a future increment already
  needs a `policy_values` migration for another reason.
- **Catch the resulting `IntegrityError`/duplicate and retry** — viable for the supersede case if the index
  existed, but doesn't help the plain optimistic-concurrency case (no unique constraint violation occurs
  there; the second write just silently succeeds with a higher version number), so it wouldn't be a single
  consistent pattern across both situations.

## Consequences
- Every future increment adding a versioned aggregate (dr_events, tasks, dr_applications all have
  `version INTEGER` columns per DATA_MODEL.md) should read-with-lock before an `expected_version` check,
  not just compare after a plain `SELECT`/`session.get()`.
- Every future increment adding an append-only "one active row, others superseded" table without a partial
  unique index should take the same advisory-lock-before-read-supersede-insert shape, keyed on whatever
  tuple defines "the same logical row."
- Known test-coverage gap, called out rather than silently claimed as tested: the existing test harness
  (one shared, rolled-back transaction per test, per `.claude/rules/tests.md`) cannot exercise two genuinely
  concurrent transactions in-process. A future increment that needs to prove this under load should use a
  dedicated two-connection/two-session test or an external concurrency test (e.g. `tests/load/`), not the
  standard `session` fixture.

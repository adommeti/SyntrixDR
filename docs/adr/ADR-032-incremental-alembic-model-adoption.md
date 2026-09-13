# ADR-032 — Incremental SQLAlchemy model adoption against a fully-defined frozen schema

**Status:** Accepted
**Date:** 2026-09-13
**Increment:** BUILD-01
**Relates to:** D-247 (schema_v1.sql = Alembic 0001, reconciliation = 0002, hand-written models, `alembic
check` drift gate)

## Context
`schema_v1.sql` and `schema_v2_reconciliation.sql` define the entire V1 database up front (every table
for every future module), applied verbatim as revisions 0001/0002. D-247 also requires hand-written
SQLAlchemy models kept in sync via `alembic check`. But models are added module-by-module as each BUILD
lands its domain code (BUILD-01 only owns `idempotency_keys`/`outbox_events`; `users`, `dr_events`, etc.
belong to modules that haven't landed yet). Naively pointing `alembic check` at `Base.metadata` with only
2 of ~50 tables modeled makes every un-modeled table look like a pending `DROP` — the drift gate would be
permanently red until the last module ships.

Separately, `idempotency_keys.user_id` and `outbox_events.dr_event_id` are real foreign keys into
`users`/`dr_events`, tables no module has modeled yet. SQLAlchemy needs *some* mapped object for a
`ForeignKey("users.id")` string reference to resolve at all.

## Decision
Two small, paired mechanisms in `apps/api/migrations/env.py` / `apps/api/app/core/external_refs.py`:

1. **`include_object` filters `alembic check`/autogenerate to fully-modeled tables only.** A table is
   "comparable" iff it exists in `Base.metadata.tables` *and* isn't flagged `fk_resolution_stub` (below).
   Every un-modeled schema_v1/v2 table is silently excluded from comparison — not "DROP", not "ADD",
   simply not considered. Each module registers its own tables on the shared `Base.metadata` as it adds a
   real model; the drift gate widens automatically, with no central list to update.
2. **FK-resolution stubs carry the target table's shape only far enough to satisfy SQLAlchemy's FK
   resolver** — `app/core/external_refs.py` declares `users`/`dr_events` as bare one-column (`id`) tables
   on `Base.metadata` via `Table(..., keep_existing=True, info={"fk_resolution_stub": True})`. The
   `fk_resolution_stub` info flag is what `include_object` uses to keep these two out of comparison even
   though they *are* present in metadata. `keep_existing=True` means the owning module's later, full model
   for the same table name wins without a "table already defined" error — no code needs to delete the stub
   when the real model lands.

`include_object` (not `include_name`) is required specifically: `include_name` was tried first and,
empirically, does not suppress whole-table add/remove ops on the Alembic version pinned here — only
`include_object`'s table-level filtering does.

## Alternatives rejected
- **Model every table on day one, even for modules with no code yet** — rejected: hand-written models
  with no consuming module are dead code with no tests, and D-247 says models are hand-written to match
  what a module actually needs, not speculative ahead of the module.
- **Skip `alembic check` in CI until all modules land** — rejected: defeats D-247's drift gate exactly
  when it matters most (early, before drift habits form).
- **A raw string/`Column` sentinel instead of a stub `Table`** — rejected: SQLAlchemy's `ForeignKey`
  resolver needs an actual mapped `Table` with the target column to resolve at all; there's no lighter
  option that still lets `alembic check`/autogenerate run.

## Consequences
- When a module adds the real `users` (BUILD-02) or `dr_events` (BUILD-04) model: import it *after*
  `app.core.external_refs` in `migrations/env.py`'s import order isn't required for correctness
  (`keep_existing=True` means whichever import runs first "wins" only if the second one doesn't also pass
  `keep_existing=True` — the real model should NOT set `keep_existing=True`, so it always overrides the
  stub regardless of import order). Confirm this when BUILD-02 lands the real `User` model.
- `include_object`'s owning-table logic means a FK *into* a stub table stays comparable as long as the FK's
  *own* table (e.g. `idempotency_keys`) is fully modeled — verified in `apps/api/tests/test_migrations.py`.
- Follow-up test (flagged in code review, not yet written): run `alembic check` after inserting real rows
  that populate the `users`/`dr_events` stub tables, to prove the exclusion still holds once BUILD-02 adds
  real data alongside the stub definition during a transition period.

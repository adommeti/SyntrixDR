# ADR-043 — Event-wide graph reads use an Event-scoped subquery and column tuples; load tests are opt-in

**Status:** Accepted
**Date:** 2026-09-30
**Increment:** BUILD-06
**Relates to:** BUILD-06 Verify line (5,000-Task DAG readiness < 500 ms locally) · TEST_STRATEGY load layer

## Context
The first measurement of `tasks_dependencies/queries.py::dependency_graph` over a 5,000-Task,
~10,000-edge Event took 493 ms against a 500 ms budget. Profiling showed two costs of similar size:
- psycopg converting 5,000-element `IN (...)` bind lists;
- hydrating ~15,000 ORM objects that the projection only reads.

## Decision
- Event-wide reads filter edges and gates by an Event-scoped subquery (`live_tasks`: `SELECT id FROM tasks
  WHERE dr_event_id = :e AND deleted_at IS NULL`). They never pass a bound list of Task ids.
- They select column tuples (`select(Task.id, Task.status, ...)`), not ORM entities. The pure functions
  `derive_readiness`, `cycle_path` and `has_cycle` work on plain values, so Hypothesis can test them
  without a database.
- The perf smoke (`tests/test_dependency_performance.py`) carries the `load` marker. It is excluded from
  the default run by `pyproject.toml` addopts (`-m "not load"`). It runs through `make test-load` and a
  dedicated `scripts/verify.sh` step ("pytest (load smoke)"), because shared CI runners are too variable
  to time reliably.

## Alternatives rejected
- A recursive CTE computing readiness in SQL — this is harder to reason about, and it duplicates the
  pure `derive_readiness` rule that property tests pin.
- Caching the projection in Redis — this adds an invalidation surface on every Task and edge write,
  for a query that already runs in about 170–230 ms.

## Consequences
- Measured after the change: `dependency_graph` 172–233 ms, and the worst-case `find_cycle_path` 17–60 ms (median of 3).
- New Event-wide queries must follow the same shape. A bound id list is the regression to watch for.
- BUILD-10 health and BUILD-12/13 screens read this projection; if they add columns, re-run `make test-load`.

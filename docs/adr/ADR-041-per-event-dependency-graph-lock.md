# ADR-041 — Per-Event advisory lock serialises every dependency-graph write

**Status:** Accepted
**Date:** 2026-09-30
**Increment:** BUILD-06
**Relates to:** FROZEN_DECISIONS §7.9 (no self edges, no directed cycles) · D-224 (`readiness.dependency_graph_acyclic`, HARD_STOP, not configurable) · ADR-036 (version checks need a lock before the read)

## Context
A cycle check is a read of the whole Event graph followed by an insert. Two concurrent edge inserts
(A→B and B→A, or the last two legs of a longer loop) can each read a graph that is still acyclic and
both commit, producing a cycle neither transaction saw. Row locks don't help: the conflicting rows are
different edges, and the thing that must be consistent is the graph as a whole. The spec requires the
graph to be acyclic at all times; it leaves the concurrency mechanism to engineering.

## Decision
Every write that can add or remove a dependency edge or Milestone gate takes
`pg_advisory_xact_lock(hashtextextended('dependency_graph:<dr_event_id>', 0))` before it reads the graph,
via `tasks_dependencies/commands.py::lock_event_dependency_graph`. The lock is transaction-scoped, so it
is released at commit or rollback. Callers:
`DependencyService.add_task_dependency` / `remove_task_dependency` / `add_milestone_gate` /
`remove_milestone_gate`, and `plans_import/commands.py::process_accepted_import` (Excel accept). Under the
lock the writer runs `queries.find_cycle_path` and rejects with 409 `DEPENDENCY_CYCLE`
(`details.cycle_path`) before inserting. `create_draft_dependency` is an insert-only helper and never
checks for cycles itself; any new caller must hold the lock and run the check first.

## Alternatives rejected
- `SERIALIZABLE` isolation for dependency writes — retries surface as 40001 errors callers must loop on,
  and the rest of the request (audit, outbox, idempotency row) would have to be retry-safe too.
- Locking the `dr_events` row `FOR UPDATE` — also serialises unrelated Event writes (lifecycle,
  readiness overrides) behind graph edits.
- Detecting cycles only at `activate` — D-224 makes a cycle invalid outright, and a committed cycle would
  leave derived Ready undefined in the meantime.

## Consequences
- Graph writes within one Event are serialised; different Events never contend.
- Proven by `test_task_dependencies.py::test_every_dependency_write_holds_the_event_graph_lock_until_commit`
  (a second connection cannot take the lock until the writer commits) and
  `test_dependency_properties.py::test_the_service_never_commits_a_cycle`.
- BUILD-07 adds Task→Milestone ("contributes") edges; they must go through the same lock and the cycle
  check must traverse both edge kinds.

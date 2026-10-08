# ADR-047 — Tasks and Milestones reference each other through module imports and select-only views

**Status:** Accepted  
**Date:** 2026-10-08  
**Increment:** BUILD-07  
**Relates to:** ARCHITECTURE.md layering / ADR-038 / ADR-041

## Context
`milestone_tasks` links Tasks to Milestones in both directions: a Task contributes to a Milestone,
and a Milestone gates a Task. The models reference each other, Task commands must recompute
Milestones, and the readiness and cycle queries in `tasks_dependencies` must see Milestone edges.
A `from x.models import Class` on both sides fails under one of the two import orders, and
`tasks_dependencies.queries` importing `app.milestones.queries` would be a queries↔queries cycle.

## Decision
1. `tasks_dependencies.models` and `milestones.models` import each other as *modules*
   (`from app.milestones import models as milestone_models`), the one in `tasks_dependencies.models`
   placed at the bottom of the file. Neither imports a class name from the other at module top level.
   `test_model_import_order.py` imports each module first in a fresh interpreter.
2. Task code reads Milestone state through select-only views defined in
   `tasks_dependencies.queries` (`milestones_view`, `milestone_tasks_view`) built on the table
   objects, never through `app.milestones.queries`. Writes go the allowed direction only: Task
   transition code calls `milestones.commands`, which owns the recompute.
3. `add_milestone_gate` and contribution writes run the same `find_cycle_path` under the ADR-041
   lock, because a Task → Milestone (required contribution) → gated Task path is a cycle.

## Alternatives rejected
- Merging the two modules — Milestones have their own lifecycle, routes and policies.
- `TYPE_CHECKING`-guarded class imports — hides the FK registration order problem ADR-038 solves
  and still leaves the runtime cycle for the queries layer.
- A third "graph" module owning both tables — more moving parts for a two-table relationship.

## Consequences
- Future bidirectional pairs (e.g. Tasks ↔ Blockers) follow the same pattern.
- The late module import looks odd; the comment next to it cites this ADR.

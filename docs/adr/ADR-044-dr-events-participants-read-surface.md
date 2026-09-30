# ADR-044 — `dr_events/participants.py` is a sanctioned cross-module surface

**Status:** Accepted
**Date:** 2026-09-30
**Increment:** BUILD-06
**Relates to:** D-222 (participants, auto-enrolment) · ARCHITECTURE.md module layering · ADR-038

## Context
The layering rule says cross-module calls go through the other module's `commands.py` or `queries.py`,
never its repository. `dr_events/participants.py` holds two things:
- `user_can_see_event`, the D-222 visibility check;
- `enrol_participant`, the auto-enrolment write.

Since BUILD-04, other modules (Tasks, dependencies, Work Streams, import) have imported both straight
from `participants.py`. The BUILD-06 code review flagged this as a second, undocumented crossing point.

## Decision
`app.dr_events.participants` is a public application-layer surface of `dr_events`, on the same footing
as its `commands.py` and `queries.py`.
- **Reads** (`user_can_see_event`, `is_participant`, `visible_event_ids_for_user`) may be imported by any module.
- **The writes** (`enrol_participant`, `remove_participant`) may be called only as a side effect of a command that has
  already authorized its own action. Examples: Task creation enrolling the Owning Team, and a Work
  Stream's Lead being enrolled.
- It never touches the repository or ORM models of another module.

## Alternatives rejected
- Re-exporting both through `dr_events/queries.py` and `commands.py` — two import paths for one
  function, with no change in behaviour.
- Moving the functions into `queries.py` / `commands.py` — a churn-only refactor across five modules.

## Consequences
- Reviewers should not flag `from app.dr_events.participants import ...` as a layering violation.
- A new function added to `participants.py` becomes part of this surface. Keep it to D-222 visibility and enrolment.
- D-222 enrolment is a snapshot taken when the command runs: reassigning a Task or changing a Team's
  membership later does not re-enrol anyone. Assignment (BUILD-07) must enrol the new assignee
  (`TASK_ASSIGNEE`) in the same transaction.

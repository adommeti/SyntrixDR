# ADR-046 — D-214 Manager precedence is decided from the Task's versioned audit trail

**Status:** Accepted  
**Date:** 2026-10-08  
**Increment:** BUILD-07  
**Relates to:** D-214 / DATA_MODEL.md invariant 22 / ADR-036

## Context
D-214: a Team Manager's assignment with a stale `expected_version` on their own Team's Task is
accepted when the intervening change was an assignment by a DR Coordinator or Global Admin; every
other stale write is 409 `CONCURRENCY_CONFLICT`. Deciding that needs to know *what* happened
between `expected_version` and the current version, not just that the version moved. The spec does
not say where that history comes from.

## Decision
The history is the Task's own audit rows. Every versioned Task change writes exactly one `TASK`
audit row whose `after_data.version` is the new version (`transition_service._finish`,
`commands.apply_assignment`, `commands.update_task_metadata`), and assignment rows carry
`metadata.authority` (ADMIN, COORDINATOR, WORK_STREAM_LEAD, MANAGER, APP_OWNER, EXECUTOR,
VOLUNTEER), set server-side. `tasks_dependencies.queries.task_changes_after(task_id, version)`
returns those rows; `resources_skills.assignment_service._yields_to_manager` accepts the stale
write only when the rows are contiguous from `expected_version + 1` to the current version and
every one is a `TASK_ASSIGNED` with authority ADMIN or COORDINATOR. A version gap, any other
action, any other authority, a Manager of another Team, a cross-Team target or a future
`expected_version` is a plain 409. The Task row is locked `FOR UPDATE` before the read (ADR-036).

The accepted write bumps the version, writes the Manager's `TASK_ASSIGNED` audit with
`conflict_resolution=MANAGER_PRECEDENCE` and `superseded_version`, writes one `overrides` row
(`MANAGER_PRECEDENCE`), and emits `AssignmentChanged` with `affected_user_ids` (the superseded
actors, the previous assignee and the new one). This happens even when the Manager names the
assignee the Coordinator already chose; the "assignee unchanged" no-op applies only to non-stale
writes (PR #19).

## Alternatives rejected
- A dedicated `task_assignment_history` table — duplicates the audit log, which is append-only and
  already carries actor, action and the after-image.
- Comparing only the latest change — a Coordinator assignment followed by anything else must not be
  overridden; the whole gap has to be inspected.
- Treating a version gap as precedence-eligible — a change without a versioned audit row is a bug,
  and the safe direction is 409.

## Consequences
- New Task writers must keep the one-row-per-version contract, or Manager precedence silently
  degrades to 409.
- Notification rows are not written: BUILD-08b turns `AssignmentChanged` into notifications once
  the D-234 type catalog exists.
- Tests: `test_task_assignment.py` precedence and 409 families, including
  `test_precedence_onto_the_current_assignee_is_still_recorded`.

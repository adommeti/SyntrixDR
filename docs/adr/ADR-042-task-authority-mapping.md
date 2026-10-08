# ADR-042 — Mapping RBAC_MATRIX Task rows onto capabilities (execute, change, designated Lead)

**Status:** Accepted (interim reading of RBAC_MATRIX.md; owner confirmation requested in the BUILD-06 PR)
**Date:** 2026-09-30
**Increment:** BUILD-06
**Relates to:** RBAC_MATRIX.md rows "Own-Team reassignment", "Change Task metadata", "Validate Task (standard)" · D-209 · D-226 · D-227 · ADR-038

## Context
RBAC_MATRIX.md has no "Execute Task" row, and some Task cells are prose ("own/Team work", "stream",
"app"). The `GRANTS` table in `users_teams_org/authorization.py` needs concrete capabilities and scopes.
This ADR records how BUILD-06 encoded those cells. It adds no new authority: every rule traces to a
matrix cell, and where a cell is ambiguous the narrower reading was chosen.

## Decision
- **`EXECUTE_TASK`** (start / block / resume / submit-validation), in `tasks_dependencies/policies.py::actor_may_execute_task`:
  - Admin, Coordinator, Work Stream Lead / App Owner in the Task's scope, and the Owning Team's Manager.
  - An Executor also qualifies when they hold the EXECUTOR role *and* are the current assignee or an
    active member of the Owning Team. Being assigned alone is not authority.
- **`CHANGE_TASK_METADATA`** (`cancel`, and every field of `PATCH /tasks/{id}`): Admin, Coordinator,
  Lead / App Owner in scope, Manager of the Owning Team. An Executor gets only the *descriptive* PATCH
  fields (title, description, expected duration, sort order), via `actor_may_edit_task_metadata`.
  - The requirement fields (`evidence_required`, `evidence_min_count`, `verification_note_required`,
    `needs_specific_validation`) need `CHANGE_TASK_METADATA`.
  - Otherwise an assignee could lower their own completion bar past D-226's guard.
- **Designated Lead:** `work_streams.lead_user_id` holds Work Stream Lead authority in *that* stream,
  for exactly the capabilities whose `GRANTS` row gives `WORK_STREAM_LEAD` `True` or `"SCOPE"`.
  - It is checked in `_can_in_any_scope` via `work_streams.queries.is_work_stream_lead`.
  - Leading one stream confers nothing in another.
- **Creating a Work Stream** needs `MANAGE_WORK_STREAMS` (Admin, Coordinator).
- **Validator (D-209):** `VALIDATE_TASK_STANDARD`.
  - Application-scoped work: any SYSTEM_APPLICATION owner slot, or the APP_OWNER role.
  - Shared stream work: that stream's Lead.
  - Admin and Coordinator may validate either kind.
  - Never the assignee or the submitter: 403 `SELF_VALIDATION_FORBIDDEN`.

## Alternatives rejected
- Treating assignment as authority (any assignee may execute) — the matrix is role-based throughout.
- Giving Executors all PATCH fields — lets the executor relax D-226 for their own Task.
- A separate `LEAD_OF_STREAM` role row per stream — duplicates `work_streams.lead_user_id` and can drift from it.

## Consequences
- Proven by `test_task_transitions.py::test_visible_but_unauthorized_actors_get_403`, `test_work_streams.py::test_leading_one_stream_confers_nothing_in_another`, `test_task_metadata.py::test_the_executor_cannot_change_what_their_own_completion_requires`, and `test_api_negative_matrix.py` (403 row per command).
- If the owner's spec PR widens or narrows a cell, change the `GRANTS` entry or the one condition in `policies.py`, and the named tests. No schema change is needed.

# ADR-048 — `GET /teams/{id}/workload` is owned by `resources_skills` and computed live from Tasks

**Status:** Accepted  
**Date:** 2026-10-08  
**Increment:** BUILD-07  
**Relates to:** API_CONTRACT.md:91, :143 / RBAC_MATRIX.md / ARCHITECTURE.md module layout

## Context
BUILD-02 shipped `GET /teams/{id}/workload` inside `users_teams_org` as a placeholder returning a
member count with no authority check. BUILD-07 needs the real roll-up (open Tasks per member and
per Team, derived from Task assignments). `users_teams_org` cannot import `tasks_dependencies`
(it is below it in the layering, and the import would be a cycle), and no RBAC row names who may
read a Team's workload.

## Decision
- The route moves to `resources_skills/routes.py`; `users_teams_org` no longer serves it. The
  response keeps `team_id`, `team_name`, `member_count` and adds the live roll-up from
  `resources_skills/queries.py::team_workload`, which reads Tasks through
  `tasks_dependencies.queries` only.
- Workload is never persisted; it is derived on every read and counts only Events the viewer can
  see (participant visibility, invariant 10).
- Read authority (conservative, no RBAC row): Global Admin, DR Coordinator, the Team's Manager and
  the Team's members (`policies.may_view_team_workload`). `GET /dr-events/{id}/resources` requires
  Event visibility.

## Alternatives rejected
- Keeping the route in `users_teams_org` and calling `tasks_dependencies` — layering cycle.
- A materialised workload table updated by assignment commands — a second source of truth for a
  value that is cheap to derive.
- Leaving the read unrestricted — it reveals assignee and Task counts across Events.

## Consequences
- BUILD-02's 404 test still passes against the moved route.
- Follow-up (independent review of #18): authorise before running the count queries, so an
  unauthorised caller cannot tell an existing Team id from a missing one; batch `get_team()` in
  `event_resources`.

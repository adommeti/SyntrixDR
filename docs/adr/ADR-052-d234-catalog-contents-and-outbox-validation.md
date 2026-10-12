# ADR-052 — Initial D-234 catalog contents and outbox event-type validation

**Status:** Accepted  
**Date:** 2026-10-11  
**Increment:** BUILD-08  
**Relates to:** D-234 / D-216 / API_CONTRACT.md §WebSocket / UI_UX.md §Event timeline / ADR-021

## Context
D-234 requires one canonical typed catalog in `packages/contracts` mirrored by backend enums for
notification types, alert types, severities, WebSocket event types and timeline event types, and says new
operational types need an ADR/spec update. The spec enumerates the WebSocket union (16 members) and
severities, names three alert types (`SLA_WARNING`, `RTO_BREACH`, `RPO_BREACH`), and gives the timeline
only as filter facets. This ADR records the initial contents so later additions have a baseline.

## Decision
- `OutboxEventType` (`core/catalogs.py`) = exactly the API_CONTRACT V1 union; `WS_EVENT_TYPES` in
  `catalog.ts` is the same list. `write_outbox` raises on any other string, and a test greps every
  `event_type=` literal under `app/` against the enum.
- `Severity` = CRITICAL, HIGH, MEDIUM, LOW.
- `AlertType` = BLOCKER_ESCALATED, SLA_WARNING, RTO_BREACH, RPO_BREACH; RTO/RPO breach acknowledgements
  need a reason (FROZEN §9.8).
- `NotificationType` = BLOCKER_ESCALATED, MENTIONED, ASSIGNMENT_CHANGED (the three the code emits).
- `TimelineEventType` = EVENT_STATE_CHANGED, TASK_STATE_CHANGED, BLOCKER_STATE_CHANGED, ISSUE_STATE_CHANGED,
  MILESTONE_STATE_CHANGED, OVERRIDE_RECORDED, VALIDATION_DECIDED, RTO_RPO_RECORDED, SLA_WARNING, SLA_BREACH,
  AI_ACTION, REPORT_PUBLISHED — one per UI_UX timeline filter facet; no producer exists yet.
- `tests/test_catalogs.py` pins each backend enum to its contracts array and the union to the contract text.

## Alternatives rejected
- Free-text `event_type` / `alert_type` columns — the schema allows it, D-234 forbids it in practice.
- Generating the TypeScript from the Python enums — the contracts package is hand-written by design
  (`catalog.ts` header); the pinning test is the single source of drift detection.

## Consequences
- Adding a member: ADR or spec PR, then `catalog.py` / `core/catalogs.py` and `catalog.ts` in one commit, or
  `test_catalogs.py` fails.
- Alert acknowledge/snooze and comments have no realtime event until the union grows (ADR-051).

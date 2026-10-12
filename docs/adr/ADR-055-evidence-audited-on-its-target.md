# ADR-055 — Evidence actions are audited on the target entity; outbox only where the union has a type

**Status:** Accepted  
**Date:** 2026-10-12  
**Increment:** BUILD-09  
**Relates to:** D-234 (audit entity superset) / DATA_MODEL invariant 11 / ADR-051 / ADR-052

## Context
`audit_events.entity_type` is constrained to the 16 `target_type` labels plus the admin/identity names
(schema_v2_reconciliation.sql, D-234). `EVIDENCE` is not among them, and the D-234 outbox union has no evidence
event type either. Evidence creation, attachment, scanning and quarantine still have to be auditable, and a
Task's acceptable-evidence count changing is something the UI must learn about.

## Decision
- Evidence actions (`EVIDENCE_CREATED`, `EVIDENCE_FILE_ATTACHED`, `EVIDENCE_SCANNED`, `EVIDENCE_QUARANTINED`) are
  audited on the **target** entity (`entity_type = target_type`, `entity_id = target_id`) with
  `after.evidence_id`, `evidence_type` and `malware_scan_status` — the same shape as `COMMENT_CREATED`.
- When the target is a TASK, each action also emits `TaskChanged` (the Task's derived evidence count moved).
  Other targets get audit only until the union gains an evidence or generic target event (D-234 ADR/spec change).
- Scan verdicts arriving from the webhook are audited with `actor_type = SYSTEM` and no user.

## Alternatives rejected
- Extending the audit entity CHECK with `EVIDENCE` — a migration and a D-234 catalog change for one entity.
- Using `DOCUMENT` as the entity — documents are a different table and concept (ADR-014).
- Emitting `TaskChanged` for non-Task targets — wrong aggregate; consumers would invalidate the wrong caches.

## Consequences
- Tests filter audits with `action LIKE 'EVIDENCE_%'` on the target id.
- Flagged for the owner: a realtime signal for evidence on Applications, Blockers, Issues and Milestones needs a
  union extension.

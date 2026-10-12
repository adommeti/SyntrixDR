# ADR-056 — DR Application validation: one Validation record per phase and a RecoveryMetrics port

**Status:** Accepted  
**Date:** 2026-10-12  
**Increment:** BUILD-09  
**Relates to:** D-208 / D-210 / D-254 / D-257 / STATE_MACHINES.md §Application recovery (I-3) / BUILD-10 prompt

## Context
`POST /dr-applications/{id}/submit-validation` and `/validate` drive `RECOVERING → TECHNICAL_VALIDATION →
FAILED_OVER | COMPLETED` and, after failback, `FAILBACK_IN_PROGRESS → COMPLETED`. The `validations` table has no
phase column and no REST resource (D-210). The RTO/RPO numbers computed at technical validation belong to BUILD-10,
but the state machine, the clock stop and the record had to land with evidence so protected completion is real.

## Decision
- `DrApplicationTransitionService` lives in `dr_events/transition_service.py` (the DR Application status is
  written there and nowhere else). Order: lock the Application row, Event visibility (404), Event not
  CLOSED/CANCELLED (409 — Event cancellation does not cascade), `actor_may_validate_application` (Admin /
  Coordinator unscoped, `VALIDATE_APPLICATION_WORK` at APPLICATION scope, or any System Owner slot), version,
  legality, pending-record guard, phase-Task guard with audited override, mutate, record, audit, outbox.
- One Validation row per submission with `target_type = DR_APPLICATION` and the phase recorded as
  `verification_note = "phase:FAILOVER" | "phase:FAILBACK"`. A failback submit opens the record and leaves the
  status at `FAILBACK_IN_PROGRESS` (the machine has no failback-validation state; the PENDING record is the
  marker). The submitter may not validate their own submission (conservative; D-209 names Tasks).
- `validate` approve stamps `technical_validated_at` only in the failover phase (D-257's clock stop) and calls
  `dr_events/ports.py::RecoveryMetrics.on_technical_validation`; `NoRecoveryMetricsYet` records nothing until
  BUILD-10 provides the calculator. Rejection needs a note and returns the Application to RECOVERING; a failback
  rejection keeps the phase. Both commands emit `ApplicationHealthChanged` (the validation score moved; the
  union has no plain Application event).
- `application.auto_submit_validation` is not wired: the key is not in the seeded `policy_definitions`, so it
  cannot be set; BUILD-10 adds it by spec PR and the hook in `TaskTransitionService.validate`.

## Alternatives rejected
- A separate `application_validations` table or a phase column — schema change for a value the note carries.
- A new status for "failback validation pending" — not in D-208's enum.
- Computing RTO/RPO here from `network_cut_at` — BUILD-10 owns the formulas and their golden tests.

## Consequences
- `GET /dr-events/{id}` exposes each Application's `version` and `technical_validated_at` so clients can send
  `expected_version` (second-opinion review of PR #25).
- Tests: `test_application_validation.py` (37), `test_protected_completion.py` (28).

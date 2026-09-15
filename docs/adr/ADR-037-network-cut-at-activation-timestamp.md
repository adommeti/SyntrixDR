# ADR-037 — Deriving D-237's `activated_at` bound from `dr_events.updated_at`

**Status:** Accepted
**Date:** 2026-09-14
**Increment:** BUILD-04
**Relates to:** D-237, `apps/api/app/dr_events/transition_service.py::start_failover`

## Context
D-237 (FREEZE_ADDENDUM.md:73) requires `start-failover`'s optional `network_cut_at` to satisfy
`activated_at ≤ network_cut_at ≤ now()`, but no schema column named `activated_at` (or
equivalent) exists anywhere in `dr_events`. schema_v2_reconciliation.sql:389-393 confirms this
explicitly: "D-237 — start-failover network_cut_at (no schema change) — `dr_events.network_cut_at`
already stores the value. The service enforces `activated_at ≤ network_cut_at ≤ now()`... " — the
spec assumes the service can determine `activated_at` without naming where it comes from.

## Decision
`activated_at` is derived as `dr_events.updated_at`, read at the moment `start-failover` begins,
rather than a dedicated timestamp column. This is valid because:
- `updated_at` is bumped by every transition (`activate`, `start-failover`, etc.), so immediately
  after `activate` runs, `updated_at` equals the exact activation instant.
- STATE_MACHINES.md's ACTIVE description ("Event activated, ready for execution/start command")
  and this session's scope (readiness/scope edits happen in PLANNED, not ACTIVE) mean no other
  legal mutation touches the Event row between `activate` and `start-failover` — so `updated_at`
  stays pinned to the activation timestamp for exactly as long as it needs to.
- This keeps D-237 a pure service-layer computation, matching schema_v2_reconciliation.sql's own
  "no schema change" framing — introducing a dedicated `activated_at` column would be additive
  schema no product requirement asks for.

## Alternatives rejected
- **Add a dedicated `dr_events.activated_at` column** — the frozen reconciliation note explicitly
  frames D-237 as requiring no schema change; adding one anyway would contradict that without a
  product-driven reason, and duplicates information `updated_at` already carries for this window.
- **Derive `activated_at` from the most recent `DR_EVENT_ACTIVATED` `audit_events` row** — correct
  in principle, but adds a query against `audit_events` (a different table, no index tailored to
  this lookup) for information already available on the row being transitioned; only worth it if a
  future increment introduces a legal PLANNED→ACTIVE→(edit)→start-failover path that mutates the
  Event row after activation, which none does today.

## Consequences
- If a future increment adds a legal Event-row mutation while ACTIVE (e.g. editing
  `coordinator_user_id` or `event_timezone` before failover), `updated_at` would then reflect that
  later edit instead of the activation instant, silently breaking this derivation. Any such
  increment must re-derive `activated_at` from `audit_events` (the rejected alternative above)
  instead, or reintroduce a dedicated column.
- No test can fully prove this at the unit level without simulating that failure mode; the
  existing `test_start_failover_rejects_network_cut_at_before_activation` test proves the *current*
  correct behavior (bound enforced against the real activation timestamp), not the invariant that
  no intervening mutation exists — that invariant is enforced by code review, not a test, until/if
  it needs mechanical enforcement.

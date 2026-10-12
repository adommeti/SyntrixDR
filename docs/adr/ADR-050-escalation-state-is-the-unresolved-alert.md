# ADR-050 — Blocker escalation state is the unresolved escalation alert, not a Blocker column

**Status:** Accepted  
**Date:** 2026-10-11  
**Increment:** BUILD-08  
**Relates to:** D-225 / D-241 / STATE_MACHINES.md §Blocker / FROZEN §9.8 / ADR-045

## Context
An OPEN Blocker that sits unrouted longer than `blocker.escalation_minutes.<tier>` must escalate exactly
once, and the sweep runs every 30 s on the one beat. The `blockers` table carries no "escalated" marker and
DATA_MODEL says the Blocker keeps "escalation history", which the schema stores nowhere but the audit
trail. Adding a column is a schema change; `alerts` already exists with `resolved_at`.

## Decision
The escalation of a Blocker **is** its `alerts` row: `alert_type = BLOCKER_ESCALATED`, `target_type =
BLOCKER`, `target_id = <blocker>`. "Already escalated" means an unresolved such row exists. The sweep checks
that marker before and again after taking the Blocker row lock, so two overlapping sweeps raise one alert.
Leaving OPEN (`assign`) resolves the alert. Acknowledging the alert never sets `resolved_at`: acknowledging
means "seen", not "condition gone", and resolving it would make the sweep re-fire for a Blocker that is still
unrouted. The sweep skips Blockers on finished Events and terminal Tasks. The timer runs from `blocked_at`
while OPEN only; a Task without an Application uses the slowest (T4) key.

## Alternatives rejected
- `blockers.escalated_at` column — schema change for a fact the alert already records.
- A partial unique index on `(alert_type, target_type, target_id) WHERE resolved_at IS NULL` — stronger
  than the check-after-lock, but also a schema change; may be added by a later migration.
- Escalating ASSIGNED Blockers that nobody starts — an owner question (plan Risk #1), not decided here.

## Consequences
- `comments_mentions_notifications.commands.has_unresolved_alert` / `resolve_alerts` are the only readers
  and writers of the marker.
- Tests: `test_blocker_escalation.py` (idempotent sweeps, check-after-lock, acknowledge no re-fire,
  finished Event/Task skipped, routing resolves).

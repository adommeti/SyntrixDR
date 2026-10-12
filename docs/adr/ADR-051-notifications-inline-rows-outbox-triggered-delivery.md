# ADR-051 — Notification rows are written inline; channel delivery is an outbox-triggered job behind a port

**Status:** Accepted  
**Date:** 2026-10-11  
**Increment:** BUILD-08  
**Relates to:** D-214 ("affected users are notified") / D-234 / D-241 / D-225 (`notifications.*.enabled`) / ADR-021

## Context
Mentions, assignment changes and Blocker escalations must notify users. D-241 lists "notification fan-out"
as a worker job and ADR-021 reserves `outbox_events.published_at` for the realtime drain (BUILD-11). The
email adapter is BUILD-18; Teams/Slack are disabled. The in-app inbox is the only V1 channel. A job that
*creates* notification rows would need its own cursor over the outbox, colliding with the drain's marker.

## Decision
1. The command that causes a notification writes the `notifications` row in its own transaction through
   `comments_mentions_notifications.commands.notify_user`, which also writes one `NotificationCreated`
   outbox event (D-234 union). Invariant 11 holds: the row and its event commit together or not at all.
2. Delivery to external channels is `jobs.deliver_notifications(session, sender, ids)` / Celery
   `drcc.fan_out_notifications(ids)`, behind `ports.NotificationSender`. It is **not** on the beat and never
   reads or marks the outbox; the BUILD-11 drain dispatches it per `NotificationCreated` event. The only
   adapter is `NullNotificationSender` until BUILD-18 returns an SMTP sender when
   `notifications.email.enabled` is true.
3. Mentioned users who cannot see the target's Event get a `mentions` row but no notification; the ids are
   recorded on the audit row. There is no D-222 enrolment source for mentions.

## Alternatives rejected
- A fan-out job that scans the outbox and creates rows — needs a second published marker or a cursor table.
- Sending email from the request path — a provider outage would fail a critical workflow (invariant 14).
- Enrolling mentioned users as participants — D-222 lists the enrolment sources and mentions are not one.

## Consequences
- `AssignmentChanged` fan-out deferred from BUILD-07 is closed: `apply_assignment` notifies every affected
  user except the actor.
- An escalation now emits two outbox rows (`BlockerChanged`, `NotificationCreated`).
- Alert acknowledge/snooze and comment create are audit-only: the union has no alert or comment event
  type, and adding one is a D-234 ADR/spec change.

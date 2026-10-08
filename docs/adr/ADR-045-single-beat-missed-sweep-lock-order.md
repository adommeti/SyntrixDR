# ADR-045 — One Celery beat entry for the MISSED sweep; jobs act as SYSTEM and lock Milestones in id order

**Status:** Accepted  
**Date:** 2026-10-08  
**Increment:** BUILD-07  
**Relates to:** D-241 (Celery + Redis, one scheduler) / STATE_MACHINES.md §Milestone / ADR-036

## Context
A Milestone whose `target_at` passes without ACHIEVED becomes MISSED. Nothing in a user's request
triggers that, so a scheduled job must do it. D-241 fixes the worker stack (Celery + Redis) and
forbids a second scheduler. Three engineering choices were open: where the schedule lives, who the
audit row names as actor when no user acted, and in which order a job that touches several
Milestones takes its row locks when Task commands take the same locks.

## Decision
1. `celery_app.conf.beat_schedule` is the only schedule. It has one entry,
   `milestones-missed-sweep` → task `drcc.sweep_missed_milestones`, every 60 s. Docker Compose runs
   beat inside the worker process; Azure runs it as exactly one replica. No cron, no APScheduler,
   no per-module schedule file.
2. Jobs follow the `plans_import.jobs` split: `sweep_missed_milestones(session, clock)` takes its
   session and clock (tests pass theirs); `_sweep_async` owns an engine for the Celery task. The job
   calls the same `MilestoneTransitionService.miss` a user path would and writes its audit row with
   `actor_type="SYSTEM"` and no `actor_user_id`. `miss` re-checks the state under the row lock, so a
   Milestone confirmed between the select and the lock stays ACHIEVED.
3. Every code path that locks more than one Milestone row in one transaction locks them in
   ascending `id` order: `milestone_ids_for_task` orders by id for Task-command recomputation, and
   the sweep sorts the overdue ids before calling `miss`. The selection query may order by
   `target_at` for fairness under its `limit`; the lock order never follows it.

## Alternatives rejected
- A second scheduler (cron in the API container, APScheduler) — D-241 forbids it and it would run
  outside the audited command path.
- Locking in selection order — the sweep (target order) and a Task command (id order) could hold
  each other's rows and Postgres would abort one, possibly the user's. Caught by the independent
  review of PR #18, fixed in #19.
- Attributing job audits to a service user — invents a user row the RBAC model has no role for;
  `actor_type` already distinguishes SYSTEM from USER.

## Consequences
- `test_milestone_missed_sweep.py::test_the_sweep_is_registered_and_scheduled_on_the_one_beat` pins
  the single entry; `::test_the_sweep_locks_in_id_order_not_target_order` pins the lock order.
- Any future job (RTO breach checks, notification digests) reuses the same three rules; adding a
  second beat entry is fine, adding a second scheduler is not.
- Follow-up: a two-connection test that a Task command and the sweep contend on the same rows
  without deadlock (plan BUILD-07 reviewer follow-up 5).

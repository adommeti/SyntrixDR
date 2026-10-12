# ADR-049 — Blocker commands lock the Task row before the Blocker row

**Status:** Accepted  
**Date:** 2026-10-11  
**Increment:** BUILD-08  
**Relates to:** D-211 / D-252 / STATE_MACHINES.md §Blocker / ADR-036 / ADR-045

## Context
`verify` on the last active Blocker returns the Task `BLOCKED → IN_PROGRESS` in the same transaction
(D-252, I-1). Two users may verify two sibling Blockers of one Task at the same moment; each must see the
other's close before deciding whether it is the last. Task commands (`block`, `resume`, assignment) take the
Task row `FOR UPDATE` first. A Blocker command that locked its own row first and the Task second would take
the two rows in the opposite order to a Task command and could deadlock against it.

## Decision
Every Blocker command loads through one helper: read the Blocker **without** a lock to learn its Task, lock
the Task (`lock_visible_task`, which is also the visibility check and the 404), then lock the Blocker. The
escalation sweep locks only Blocker rows and never a Task. Sibling `verify`s therefore serialise on the Task:
the second one re-counts active Blockers after the first commits and `resume_after_last_blocker` is a no-op
when the Task is already IN_PROGRESS.

## Alternatives rejected
- Lock the Blocker first (it is the addressed row) — opposite order to every Task command; a real deadlock
  window, caught by the specification audit of PR #23 and fixed before merge.
- An Event-wide advisory lock for Blocker commands (ADR-041 style) — serialises unrelated Blockers across
  the whole Event for a two-row problem.
- No Task lock, decide "last" from a count — two concurrent verifies both see one remaining and neither
  resumes the Task.

## Consequences
- Any new command that touches a Blocker and its Task keeps this order; the rules file records it.
- Follow-up: a two-connection test of concurrent sibling verifies (the current tests share one
  SAVEPOINT session).

"""The canonical Task lifecycle (STATE_MACHINES.md §Task, D-252) -- the only place a Task's
`status` is written.

Every method runs the same order (`/drcc-transition-service`, BUILD-06.plan.md Risk #6):
  1. load the Task `FOR UPDATE` (refreshing any cached copy), 404 if missing *or invisible*
  2. authorize against the Task's own scopes -> 403
  3. version check -> 409 CONCURRENCY_CONFLICT
  4. legality (per command) -> 409 INVALID_TRANSITION, then business guards; overrides are
     recorded here, before the mutation
  5. mutate: the single status write, version bump, timestamps, side records
  6. audit, 7. outbox -- same transaction
  8. the route commits (after storing the idempotent response)

Visibility (1) runs before capability (2) on purpose: Task authority is object-scoped, so the Task
must be loaded to evaluate it at all, and checking visibility first means an outsider gets the
same 404 whether or not the Task exists. `DrEventTransitionService` authorizes *before* loading for
the mirror-image reason -- its capability is unconditional."""

from __future__ import annotations

import uuid
from collections.abc import Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.blockers.commands import create_blocker_for_task
from app.blockers.queries import count_active_blockers
from app.core.audit import write_audit
from app.core.clock import Clock, SystemClock
from app.core.errors import AppError, ConcurrencyConflictError, OverrideReasonRequiredError
from app.core.outbox import write_outbox
from app.dr_events.commands import record_override
from app.dr_events.participants import user_can_see_event
from app.milestones.commands import on_task_changed
from app.tasks_dependencies.commands import TaskNotFoundError
from app.tasks_dependencies.models import Task
from app.tasks_dependencies.policies import (
    actor_may_change_task,
    actor_may_execute_task,
    actor_may_override,
    actor_may_validate_task,
    assert_not_self_validation,
)
from app.tasks_dependencies.queries import Readiness, readiness
from app.users_teams_org.authorization import AuthorizationRequiredError
from app.validation_evidence.commands import (
    close_task_validation,
    close_validation_for_cancelled_task,
    open_task_validation,
)
from app.validation_evidence.ports import EvidenceCounter
from app.validation_evidence.queries import get_pending_task_validation

#: Legal from-states per *command*, not per target state: READY_FOR_VALIDATION -> IN_PROGRESS is
#: legal only as a validation rejection, so a state->state table would wrongly let `resume` fire
#: from READY_FOR_VALIDATION.
_LEGAL_FROM: dict[str, frozenset[str]] = {
    "start": frozenset({"NOT_STARTED"}),
    "block": frozenset({"IN_PROGRESS"}),
    "resume": frozenset({"BLOCKED"}),
    "submit_validation": frozenset({"IN_PROGRESS"}),
    "validate": frozenset({"READY_FOR_VALIDATION"}),
    "cancel": frozenset({"NOT_STARTED", "IN_PROGRESS", "BLOCKED", "READY_FOR_VALIDATION"}),
}


class InvalidTaskTransitionError(AppError):
    code = "INVALID_TRANSITION"
    status_code = 409

    def __init__(self, command: str, current: str, detail: str | None = None) -> None:
        message = f"Cannot {command.replace('_', '-')} a Task that is {current}."
        super().__init__(f"{message} {detail}" if detail else message)


class DependencyNotSatisfiedError(AppError):
    code = "DEPENDENCY_NOT_SATISFIED"
    status_code = 409

    def __init__(self, r: Readiness) -> None:
        super().__init__(
            "A HARD predecessor isn't COMPLETED or a gating Milestone isn't ACHIEVED. "
            "Supply an override_reason to start anyway.",
            details={"blocking_task_ids": r.ids("TASK"), "blocking_milestone_ids": r.ids("MILESTONE")},
        )


class EvidenceRequiredError(AppError):
    code = "EVIDENCE_REQUIRED"
    status_code = 409

    def __init__(self, message: str, details: dict[str, object]) -> None:
        super().__init__(message, details=details)


def _clean(text: str | None) -> str | None:
    """Blank or whitespace-only counts as absent."""
    if text is None:
        return None
    stripped = text.strip()
    return stripped or None


async def _load_visible_for_update(session: AsyncSession, task_id: uuid.UUID, actor_id: uuid.UUID) -> Task:
    # populate_existing: a copy already in the identity map would otherwise keep its cached
    # attributes even though FOR UPDATE re-reads the row -- the version check below must see the
    # locked row, not a stale one.
    task = (
        await session.execute(
            select(Task)
            .where(Task.id == task_id, Task.deleted_at.is_(None))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if task is None or not await user_can_see_event(session, actor_id, task.dr_event_id):
        raise TaskNotFoundError()
    return task


def _check_version(task: Task, expected_version: int) -> None:
    if task.version != expected_version:
        raise ConcurrencyConflictError()


def _assert_legal(command: str, task: Task) -> None:
    if task.status not in _LEGAL_FROM[command]:
        raise InvalidTaskTransitionError(command, task.status)


def _snapshot(task: Task) -> dict[str, object]:
    return {"status": task.status, "version": task.version}


async def _record_task_override(
    session: AsyncSession,
    *,
    task: Task,
    actor_id: uuid.UUID,
    override_type: str,
    reason: str,
    metadata: dict[str, object],
) -> None:
    await record_override(
        session,
        dr_event_id=task.dr_event_id,
        target_type="TASK",
        target_id=task.id,
        override_type=override_type,
        reason=reason,
        performed_by_user_id=actor_id,
        metadata=metadata,
    )
    await write_audit(
        session,
        actor_user_id=actor_id,
        entity_type="TASK",
        entity_id=task.id,
        action=f"{override_type}_RECORDED",
        dr_event_id=task.dr_event_id,
        after={"reason": reason, **metadata},
    )


async def _finish(
    session: AsyncSession,
    *,
    task: Task,
    actor_id: uuid.UUID,
    before: Mapping[str, object],
    action: str,
    clock: Clock,
    extra: Mapping[str, object] | None = None,
) -> Task:
    """Shared tail -- version, audit, outbox. The status write itself stays in each caller so every
    transition keeps exactly one plainly visible `task.status = ...` line."""
    task.version += 1
    task.updated_at = clock.now()
    await session.flush()

    after: dict[str, object] = {"status": task.status, "version": task.version, **(extra or {})}
    await write_audit(
        session,
        actor_user_id=actor_id,
        entity_type="TASK",
        entity_id=task.id,
        action=action,
        dr_event_id=task.dr_event_id,
        before=dict(before),
        after=after,
    )
    await write_outbox(
        session,
        aggregate_type="TASK",
        aggregate_id=task.id,
        event_type="TaskChanged",  # D-234 V1 union (API_CONTRACT.md:274)
        payload=after,
        clock=clock,
        dr_event_id=task.dr_event_id,
    )
    # Milestones this Task contributes to move with it, in this same transaction (BUILD-07).
    await on_task_changed(session, task_id=task.id, actor_id=actor_id, clock=clock)
    return task


class TaskTransitionService:
    @staticmethod
    async def start(
        session: AsyncSession,
        *,
        actor_id: uuid.UUID,
        task_id: uuid.UUID,
        expected_version: int,
        override_reason: str | None = None,
        clock: Clock | None = None,
    ) -> Task:
        """NOT_STARTED -> IN_PROGRESS when derived Ready, or over an audited dependency override."""
        clock = clock or SystemClock()
        task = await _load_visible_for_update(session, task_id, actor_id)
        if not await actor_may_execute_task(session, actor_id, task, at=clock.now()):
            raise AuthorizationRequiredError()
        _check_version(task, expected_version)
        _assert_legal("start", task)

        r = await readiness(session, task)
        if r.blocking:
            reason = _clean(override_reason)
            if reason is None:
                raise DependencyNotSatisfiedError(r)
            cross_stream = any(g.work_stream_id != task.work_stream_id for g in r.blocking)
            if not await actor_may_override(session, actor_id, task, cross_stream=cross_stream):
                raise AuthorizationRequiredError()
            await _record_task_override(
                session,
                task=task,
                actor_id=actor_id,
                override_type="DEPENDENCY_OVERRIDE",
                reason=reason,
                metadata={
                    "blocking_task_ids": r.ids("TASK"),
                    "blocking_milestone_ids": r.ids("MILESTONE"),
                    "cross_stream": cross_stream,
                },
            )

        before = _snapshot(task)
        task.status = "IN_PROGRESS"
        task.started_at = clock.now()
        # Invariant #2 "ADVISORY warn": start goes ahead, and the pending ADVISORY upstream is recorded
        # on the audit row (BUILD-06.plan.md Risk #16).
        return await _finish(
            session,
            task=task,
            actor_id=actor_id,
            before=before,
            action="TASK_STARTED",
            clock=clock,
            extra={
                "advisory_pending_task_ids": r.ids("TASK", advisory=True),
                "advisory_pending_milestone_ids": r.ids("MILESTONE", advisory=True),
            },
        )

    @staticmethod
    async def block(
        session: AsyncSession,
        *,
        actor_id: uuid.UUID,
        task_id: uuid.UUID,
        expected_version: int,
        reason: str | None,
        clock: Clock | None = None,
    ) -> Task:
        """IN_PROGRESS -> BLOCKED, creating the Blocker that backs it in the same transaction
        (invariant #10, D-252)."""
        clock = clock or SystemClock()
        task = await _load_visible_for_update(session, task_id, actor_id)
        if not await actor_may_execute_task(session, actor_id, task, at=clock.now()):
            raise AuthorizationRequiredError()
        _check_version(task, expected_version)
        _assert_legal("block", task)
        clean_reason = _clean(reason)
        if clean_reason is None:
            raise OverrideReasonRequiredError("Blocking a Task")

        before = _snapshot(task)
        task.status = "BLOCKED"
        blocker = await create_blocker_for_task(
            session,
            task_id=task.id,
            dr_event_id=task.dr_event_id,
            reason=clean_reason,
            created_by_user_id=actor_id,
            clock=clock,
        )
        return await _finish(
            session,
            task=task,
            actor_id=actor_id,
            before=before,
            action="TASK_BLOCKED",
            clock=clock,
            extra={"blocker_id": str(blocker.id)},
        )

    @staticmethod
    async def resume(
        session: AsyncSession,
        *,
        actor_id: uuid.UUID,
        task_id: uuid.UUID,
        expected_version: int,
        override_reason: str | None = None,
        clock: Clock | None = None,
    ) -> Task:
        """BLOCKED -> IN_PROGRESS, the *manual* path only (I-1). The normal path is automatic when
        BUILD-08's `blockers/{id}/verify` closes the last active Blocker. With no active Blocker
        left this is a plain resume; with one still active it's an override -- reason required,
        audited -- and the Blocker stays open. Never advances past IN_PROGRESS."""
        clock = clock or SystemClock()
        task = await _load_visible_for_update(session, task_id, actor_id)
        if not await actor_may_execute_task(session, actor_id, task, at=clock.now()):
            raise AuthorizationRequiredError()
        _check_version(task, expected_version)
        _assert_legal("resume", task)

        active_blockers = await count_active_blockers(session, task.id)
        if active_blockers:
            reason = _clean(override_reason)
            if reason is None:
                raise OverrideReasonRequiredError("Resuming a Task that still has an active Blocker")
            if not await actor_may_override(session, actor_id, task, cross_stream=False):
                raise AuthorizationRequiredError()
            await _record_task_override(
                session,
                task=task,
                actor_id=actor_id,
                override_type="BLOCKER_ACTIVE_RESUME_OVERRIDE",
                reason=reason,
                metadata={"active_blocker_count": active_blockers},
            )

        before = _snapshot(task)
        task.status = "IN_PROGRESS"
        return await _finish(
            session, task=task, actor_id=actor_id, before=before, action="TASK_RESUMED", clock=clock
        )

    @staticmethod
    async def resume_after_last_blocker(
        session: AsyncSession,
        *,
        task_id: uuid.UUID,
        actor_id: uuid.UUID,
        blocker_id: uuid.UUID,
        clock: Clock,
    ) -> Task:
        """BLOCKED -> IN_PROGRESS as the side effect of `blockers/{id}/verify` closing the Task's last
        active Blocker (D-252, I-1): same transaction, no version check (nothing the caller sent names
        the Task's version), authorization already done by the Blocker command. A Task that is not
        BLOCKED, or still has an active Blocker, is left alone. Never advances past IN_PROGRESS."""
        task = (
            await session.execute(
                select(Task)
                .where(Task.id == task_id, Task.deleted_at.is_(None))
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if task is None:
            raise TaskNotFoundError()
        if task.status != "BLOCKED" or await count_active_blockers(session, task.id):
            return task

        before = _snapshot(task)
        task.status = "IN_PROGRESS"
        return await _finish(
            session,
            task=task,
            actor_id=actor_id,
            before=before,
            action="TASK_RESUMED",
            clock=clock,
            extra={"blocker_id": str(blocker_id), "automatic": True},
        )

    @staticmethod
    async def submit_validation(
        session: AsyncSession,
        *,
        actor_id: uuid.UUID,
        task_id: uuid.UUID,
        expected_version: int,
        verification_note: str | None,
        evidence_counter: EvidenceCounter,
        clock: Clock | None = None,
    ) -> Task:
        """IN_PROGRESS -> READY_FOR_VALIDATION, opening the persisted PENDING Validation (D-210).
        D-226: at least `evidence_min_count` acceptable Evidence Items when `evidence_required`,
        and a non-blank verification note when `verification_note_required`."""
        clock = clock or SystemClock()
        task = await _load_visible_for_update(session, task_id, actor_id)
        if not await actor_may_execute_task(session, actor_id, task, at=clock.now()):
            raise AuthorizationRequiredError()
        _check_version(task, expected_version)
        _assert_legal("submit_validation", task)

        if task.evidence_required:
            found = await evidence_counter.count_acceptable_for_task(session, task.id)
            if found < task.evidence_min_count:
                raise EvidenceRequiredError(
                    f"This Task needs at least {task.evidence_min_count} acceptable Evidence Item(s); "
                    f"it has {found}.",
                    details={"evidence_min_count": task.evidence_min_count, "evidence_found": found},
                )
        note = _clean(verification_note)
        if task.verification_note_required and note is None:
            raise EvidenceRequiredError(
                "This Task requires a verification note.", details={"verification_note_required": True}
            )

        before = _snapshot(task)
        task.status = "READY_FOR_VALIDATION"
        validation = await open_task_validation(
            session,
            task_id=task.id,
            dr_event_id=task.dr_event_id,
            verification_note=note,
            submitted_by_user_id=actor_id,
            clock=clock,
        )
        return await _finish(
            session,
            task=task,
            actor_id=actor_id,
            before=before,
            action="TASK_SUBMITTED",
            clock=clock,
            extra={"validation_id": str(validation.id)},
        )

    @staticmethod
    async def validate(
        session: AsyncSession,
        *,
        actor_id: uuid.UUID,
        task_id: uuid.UUID,
        expected_version: int,
        approve: bool,
        note: str | None = None,
        clock: Clock | None = None,
    ) -> Task:
        """READY_FOR_VALIDATION -> COMPLETED (approve) or back to IN_PROGRESS (reject, D-210),
        closing the PENDING Validation. The only way a Task ever reaches COMPLETED (D-207/D-209).
        Never via AI (D-228): no AI path reaches this command."""
        clock = clock or SystemClock()
        task = await _load_visible_for_update(session, task_id, actor_id)
        if not await actor_may_validate_task(session, actor_id, task):
            raise AuthorizationRequiredError()
        _check_version(task, expected_version)
        _assert_legal("validate", task)

        validation = await get_pending_task_validation(session, task.id, for_update=True)
        if validation is None:
            raise InvalidTaskTransitionError("validate", task.status, "It has no open Validation record.")
        assert_not_self_validation(actor_id, task, validation.created_by_user_id)

        before = _snapshot(task)
        task.status = "COMPLETED" if approve else "IN_PROGRESS"
        if approve:
            task.completed_at = clock.now()
        await close_task_validation(
            session,
            validation=validation,
            approve=approve,
            validator_user_id=actor_id,
            note=_clean(note),
            dr_event_id=task.dr_event_id,
            clock=clock,
        )
        return await _finish(
            session,
            task=task,
            actor_id=actor_id,
            before=before,
            action="TASK_VALIDATED" if approve else "TASK_VALIDATION_REJECTED",
            clock=clock,
            extra={"validation_id": str(validation.id)},
        )

    @staticmethod
    async def cancel(
        session: AsyncSession,
        *,
        actor_id: uuid.UUID,
        task_id: uuid.UUID,
        expected_version: int,
        reason: str | None,
        clock: Clock | None = None,
    ) -> Task:
        """Any non-terminal state -> CANCELLED. Reason required (API_CONTRACT.md:53 names cancel)."""
        clock = clock or SystemClock()
        task = await _load_visible_for_update(session, task_id, actor_id)
        if not await actor_may_change_task(session, actor_id, task):
            raise AuthorizationRequiredError()
        _check_version(task, expected_version)
        _assert_legal("cancel", task)
        clean_reason = _clean(reason)
        if clean_reason is None:
            raise OverrideReasonRequiredError("Cancelling a Task")

        before = _snapshot(task)
        if task.status == "READY_FOR_VALIDATION":
            await close_validation_for_cancelled_task(
                session,
                task_id=task.id,
                actor_id=actor_id,
                reason=clean_reason,
                dr_event_id=task.dr_event_id,
                clock=clock,
            )
        task.status = "CANCELLED"
        return await _finish(
            session,
            task=task,
            actor_id=actor_id,
            before=before,
            action="TASK_CANCELLED",
            clock=clock,
            extra={"reason": clean_reason},
        )

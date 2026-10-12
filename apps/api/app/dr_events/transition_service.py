from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import write_audit
from app.core.clock import Clock, SystemClock
from app.core.errors import AppError, ConcurrencyConflictError, OverrideReasonRequiredError
from app.core.outbox import write_outbox
from app.dr_events.commands import DrEventNotFoundError, record_override
from app.dr_events.models import DrApplication, DrEvent, Override
from app.dr_events.participants import user_can_see_event
from app.dr_events.policies import actor_may_validate_application
from app.dr_events.ports import NoRecoveryMetricsYet, RecoveryMetrics
from app.dr_events.queries import has_non_terminal_children
from app.dr_events.readiness_service import NON_OVERRIDABLE_KEYS, evaluate_readiness
from app.plans_import.commands import capture_baseline_snapshot
from app.tasks_dependencies.policies import SelfValidationForbiddenError
from app.tasks_dependencies.queries import unfinished_monitoring_task_ids, unfinished_phase_task_ids
from app.users_teams_org.authorization import AuthorizationRequiredError, AuthorizationService, Capability
from app.validation_evidence.commands import close_application_validation, open_application_validation
from app.validation_evidence.queries import get_pending_validation


class MonitoringClosureWarningError(AppError):
    """D-227 / API_CONTRACT.md `close` row: overridable only by an audited closure exception."""

    code = "MONITORING_CLOSURE_WARNING"
    status_code = 409

    def __init__(self, task_ids: list[uuid.UUID]) -> None:
        super().__init__(
            "Monitoring Work Stream Tasks are still open. Complete or cancel them, or close with a "
            "closure_exception and a reason.",
            details={"monitoring_task_ids": [str(t) for t in task_ids]},
        )


class ReadinessHardStopError(AppError):
    code = "READINESS_HARD_STOP"
    status_code = 409

    def __init__(self, failed_keys: list[str]) -> None:
        super().__init__(
            "Readiness check(s) failed and no override_reason was supplied.",
            details={"failed_keys": failed_keys},
        )


#: STATE_MACHINES.md §DR Event. CANCELLED is reachable from any non-terminal state (handled
#: separately in `cancel`, not folded into this table, since it's a blanket rule not a per-row one).
_LEGAL_TRANSITIONS: dict[str, set[str]] = {
    "PLANNED": {"ACTIVE"},
    "ACTIVE": {"FAILOVER_IN_PROGRESS"},
    "FAILOVER_IN_PROGRESS": {"FAILED_OVER"},
    "FAILED_OVER": {"FAILBACK_IN_PROGRESS", "CLOSED"},
    "FAILBACK_IN_PROGRESS": {"CLOSED"},
    "CLOSED": set(),
    "CANCELLED": set(),
}
_TERMINAL_STATES = frozenset({"CLOSED", "CANCELLED"})


class InvalidEventTransitionError(AppError):
    code = "INVALID_TRANSITION"
    status_code = 409  # API_CONTRACT.md:49

    def __init__(self, current: str, target: str) -> None:
        super().__init__(f"Cannot transition DR Event from {current} to {target}.")


class NetworkCutAtOutOfBoundsError(AppError):
    code = "NETWORK_CUT_AT_OUT_OF_BOUNDS"
    status_code = 422

    def __init__(self) -> None:
        super().__init__("network_cut_at must be between the Event's activation time and now (D-237).")


class CancelReasonRequiredError(AppError):
    code = "CANCEL_REASON_REQUIRED"
    status_code = 422

    def __init__(self) -> None:
        super().__init__("A reason is required to cancel a DR Event.")


class ClosureBlockedByNonTerminalChildError(AppError):
    code = "CLOSURE_HARD_STOP"
    status_code = 409

    def __init__(self) -> None:
        super().__init__("Cannot close: a child DR Event is not yet CLOSED or CANCELLED (D-219).")


class FailbackRequiredBeforeCloseError(AppError):
    """CLAUDE.md:43: "`FAILED_OVER → CLOSED` only when failback not required." Reuses the
    `CLOSURE_HARD_STOP` code (API_CONTRACT.md:52, "Closure guard failed on close") -- that row's
    listed cases (child Event, monitoring Tasks) aren't exhaustive by its own wording, and no
    dedicated code exists for this specific guard."""

    code = "CLOSURE_HARD_STOP"
    status_code = 409

    def __init__(self) -> None:
        super().__init__(
            "Cannot close directly from FAILED_OVER: at least one in-scope DR Application still "
            "requires failback (failback_required=true). Start failback first."
        )


async def _load_for_update(session: AsyncSession, event_id: uuid.UUID) -> DrEvent:
    event = await session.get(DrEvent, event_id, with_for_update=True)
    if event is None:
        raise DrEventNotFoundError()
    return event


def _assert_legal(current: str, target: str) -> None:
    if target not in _LEGAL_TRANSITIONS.get(current, set()):
        raise InvalidEventTransitionError(current, target)


async def _mutate_and_finish(
    session: AsyncSession,
    *,
    event: DrEvent,
    actor_id: uuid.UUID,
    before: Mapping[str, object],
    new_status: str,
    action: str,
    clock: Clock,
    extra_after: Mapping[str, object] | None = None,
) -> DrEvent:
    """Shared tail: bump version/timestamp, audit, outbox. The single `event.status = ...`
    assignment lives in each caller (not here) so every transition still has exactly one
    plainly-visible status write, per the `/drcc-transition-service` checklist."""
    event.version += 1
    event.updated_at = clock.now()
    await session.flush()

    after: dict[str, object] = {"status": event.status, "version": event.version}
    if extra_after:
        after.update(extra_after)

    await write_audit(
        session,
        actor_user_id=actor_id,
        entity_type="DR_EVENT",
        entity_id=event.id,
        action=action,
        dr_event_id=event.id,
        before=dict(before),
        after=after,
    )
    await write_outbox(
        session,
        aggregate_type="DR_EVENT",
        aggregate_id=event.id,
        event_type="EventStateChanged",
        payload={"status": new_status},
        clock=clock,
        dr_event_id=event.id,
    )
    return event


class DrEventTransitionService:
    """`activate`/`start_failover`/`mark_failed_over`/`start_failback`/`close`/`cancel` on the
    canonical DR Event machine (STATE_MACHINES.md §DR Event). See BUILD-04.plan.md Risk #3 for
    which guards are deferred this session (readiness on `activate`, D-227 monitoring on
    `close`, a computable validation guard on `mark_failed_over`) and why."""

    @staticmethod
    async def activate(
        session: AsyncSession,
        *,
        actor_id: uuid.UUID,
        event_id: uuid.UUID,
        expected_version: int,
        override_reason: str | None = None,
        clock: Clock | None = None,
    ) -> DrEvent:
        """D-224: evaluates `readiness_service.EVALUATED_KEYS` (the 6 of 13 catalog items
        computable with entities that exist today — see BUILD-04.plan.md session-b scope for the
        other 7). An unsatisfied HARD_STOP-severity key blocks activation unless `override_reason`
        is supplied, in which case one audited `overrides` row is written per failed key
        ("Coordinator override... always with reason") — no extra capability check, since
        `EVENT_LIFECYCLE_COMMAND` (required below) is already exactly Admin/Coordinator.
        WARNING-severity failures never block; both are recorded in this transition's audit
        metadata. An OFF-severity key is skipped for this call."""
        clock = clock or SystemClock()
        # Authorization before existence (invariant #1): EVENT_LIFECYCLE_COMMAND is an unconditional
        # Admin/Coordinator-only capability, not participant-scoped, so checking it first means an
        # unauthorized actor gets the same 403 whether the Event exists or not -- loading (and row-
        # locking) first would otherwise 404 a nonexistent Event but 403 an existing one, leaking
        # existence to any authenticated caller (found in review).
        await AuthorizationService.require(session, actor_id, Capability.EVENT_LIFECYCLE_COMMAND)
        event = await _load_for_update(session, event_id)
        if event.version != expected_version:
            raise ConcurrencyConflictError()
        _assert_legal(event.status, "ACTIVE")

        readiness_results = await evaluate_readiness(session, event)
        hard_stop_failures = [r for r in readiness_results if r.severity == "HARD_STOP" and not r.satisfied]
        warning_failures = [r for r in readiness_results if r.severity == "WARNING" and not r.satisfied]
        if hard_stop_failures:
            if not override_reason or not override_reason.strip():
                raise ReadinessHardStopError([r.key for r in hard_stop_failures])
            # D-224: `dependency_graph_acyclic` is HARD_STOP and not configurable; a cycle is invalid
            # outright (FROZEN_DECISIONS.md §7.9), so no override_reason gets past it (readiness_service).
            unoverridable = [r.key for r in hard_stop_failures if r.key in NON_OVERRIDABLE_KEYS]
            if unoverridable:
                raise ReadinessHardStopError(unoverridable)
            for result in hard_stop_failures:
                override = Override(
                    dr_event_id=event.id,
                    target_type="DR_EVENT",
                    target_id=event.id,
                    override_type="READINESS_OVERRIDE",
                    policy_key=result.key,
                    reason=override_reason,
                    performed_by_user_id=actor_id,
                )
                session.add(override)
                await session.flush()
                await write_audit(
                    session,
                    actor_user_id=actor_id,
                    entity_type="DR_EVENT",
                    entity_id=event.id,
                    action="READINESS_OVERRIDE_RECORDED",
                    dr_event_id=event.id,
                    after={"policy_key": result.key, "reason": override_reason},
                )

        before = {"status": event.status, "version": event.version}
        event.status = "ACTIVE"
        return await _mutate_and_finish(
            session,
            event=event,
            actor_id=actor_id,
            before=before,
            new_status="ACTIVE",
            action="DR_EVENT_ACTIVATED",
            clock=clock,
            extra_after={
                "readiness_hard_stop_overridden": [r.key for r in hard_stop_failures],
                "readiness_warnings": [r.key for r in warning_failures],
            },
        )

    @staticmethod
    async def start_failover(
        session: AsyncSession,
        *,
        actor_id: uuid.UUID,
        event_id: uuid.UUID,
        expected_version: int,
        network_cut_at: datetime | None = None,
        clock: Clock | None = None,
    ) -> DrEvent:
        clock = clock or SystemClock()
        # Authorization before existence (invariant #1): EVENT_LIFECYCLE_COMMAND is an unconditional
        # Admin/Coordinator-only capability, not participant-scoped, so checking it first means an
        # unauthorized actor gets the same 403 whether the Event exists or not -- loading (and row-
        # locking) first would otherwise 404 a nonexistent Event but 403 an existing one, leaking
        # existence to any authenticated caller (found in review).
        await AuthorizationService.require(session, actor_id, Capability.EVENT_LIFECYCLE_COMMAND)
        event = await _load_for_update(session, event_id)
        if event.version != expected_version:
            raise ConcurrencyConflictError()
        _assert_legal(event.status, "FAILOVER_IN_PROGRESS")

        now = clock.now()
        # D-237: activated_at has no dedicated column (schema_v2_reconciliation.sql:389-393,
        # "no schema change") -- derived as `updated_at` at the ACTIVE transition, still pinned
        # to that moment since no legal Event-row mutation happens between ACTIVE and this call
        # (ADR-037).
        activated_at = event.updated_at
        cut_at = network_cut_at if network_cut_at is not None else now
        if cut_at < activated_at or cut_at > now:
            raise NetworkCutAtOutOfBoundsError()

        before = {"status": event.status, "version": event.version}
        event.status = "FAILOVER_IN_PROGRESS"
        event.network_cut_at = cut_at

        baseline_version = await capture_baseline_snapshot(
            session, actor_id=actor_id, dr_event_id=event.id, clock=clock
        )
        if baseline_version is not None:
            event.baseline_plan_version_id = baseline_version.id

        # I-3: every in-scope DrApplication NOT_STARTED -> RECOVERING, audited per Application.
        result = await session.execute(
            select(DrApplication)
            .where(
                DrApplication.dr_event_id == event.id,
                DrApplication.status == "NOT_STARTED",
                DrApplication.deleted_at.is_(None),
            )
            .with_for_update()
        )
        for dr_application in result.scalars().all():
            app_before = {"status": dr_application.status}
            dr_application.status = "RECOVERING"
            dr_application.sla_start_at = cut_at
            dr_application.version += 1
            dr_application.updated_at = now
            await write_audit(
                session,
                actor_user_id=actor_id,
                entity_type="DR_APPLICATION",
                entity_id=dr_application.id,
                action="DR_APPLICATION_RECOVERING",
                dr_event_id=event.id,
                before=app_before,
                after={"status": dr_application.status},
            )

        return await _mutate_and_finish(
            session,
            event=event,
            actor_id=actor_id,
            before=before,
            new_status="FAILOVER_IN_PROGRESS",
            action="DR_EVENT_FAILOVER_STARTED",
            clock=clock,
            extra_after={"network_cut_at": cut_at.isoformat()},
        )

    @staticmethod
    async def mark_failed_over(
        session: AsyncSession,
        *,
        actor_id: uuid.UUID,
        event_id: uuid.UUID,
        expected_version: int,
        clock: Clock | None = None,
    ) -> DrEvent:
        """No guard beyond state-machine legality this session (BUILD-04.plan.md Risk #3,
        restated here per review): the real guard belongs to DR Application/Task validation
        state, which doesn't exist until a later increment. A Coordinator's own judgment is the
        only gate for now — do not silently wire a stronger check in without updating this
        comment and the plan's Risk #3."""
        clock = clock or SystemClock()
        # Authorization before existence (invariant #1): EVENT_LIFECYCLE_COMMAND is an unconditional
        # Admin/Coordinator-only capability, not participant-scoped, so checking it first means an
        # unauthorized actor gets the same 403 whether the Event exists or not -- loading (and row-
        # locking) first would otherwise 404 a nonexistent Event but 403 an existing one, leaking
        # existence to any authenticated caller (found in review).
        await AuthorizationService.require(session, actor_id, Capability.EVENT_LIFECYCLE_COMMAND)
        event = await _load_for_update(session, event_id)
        if event.version != expected_version:
            raise ConcurrencyConflictError()
        _assert_legal(event.status, "FAILED_OVER")

        before = {"status": event.status, "version": event.version}
        event.status = "FAILED_OVER"
        return await _mutate_and_finish(
            session,
            event=event,
            actor_id=actor_id,
            before=before,
            new_status="FAILED_OVER",
            action="DR_EVENT_FAILED_OVER",
            clock=clock,
        )

    @staticmethod
    async def start_failback(
        session: AsyncSession,
        *,
        actor_id: uuid.UUID,
        event_id: uuid.UUID,
        expected_version: int,
        clock: Clock | None = None,
    ) -> DrEvent:
        clock = clock or SystemClock()
        # Authorization before existence (invariant #1): EVENT_LIFECYCLE_COMMAND is an unconditional
        # Admin/Coordinator-only capability, not participant-scoped, so checking it first means an
        # unauthorized actor gets the same 403 whether the Event exists or not -- loading (and row-
        # locking) first would otherwise 404 a nonexistent Event but 403 an existing one, leaking
        # existence to any authenticated caller (found in review).
        await AuthorizationService.require(session, actor_id, Capability.EVENT_LIFECYCLE_COMMAND)
        event = await _load_for_update(session, event_id)
        if event.version != expected_version:
            raise ConcurrencyConflictError()
        _assert_legal(event.status, "FAILBACK_IN_PROGRESS")

        now = clock.now()
        before = {"status": event.status, "version": event.version}
        event.status = "FAILBACK_IN_PROGRESS"
        event.failback_started_at = now

        result = await session.execute(
            select(DrApplication)
            .where(
                DrApplication.dr_event_id == event.id,
                DrApplication.status == "FAILED_OVER",
                DrApplication.failback_required.is_(True),
                DrApplication.deleted_at.is_(None),
            )
            .with_for_update()
        )
        for dr_application in result.scalars().all():
            app_before = {"status": dr_application.status}
            dr_application.status = "FAILBACK_IN_PROGRESS"
            dr_application.version += 1
            dr_application.updated_at = now
            await write_audit(
                session,
                actor_user_id=actor_id,
                entity_type="DR_APPLICATION",
                entity_id=dr_application.id,
                action="DR_APPLICATION_FAILBACK_STARTED",
                dr_event_id=event.id,
                before=app_before,
                after={"status": dr_application.status},
            )

        return await _mutate_and_finish(
            session,
            event=event,
            actor_id=actor_id,
            before=before,
            new_status="FAILBACK_IN_PROGRESS",
            action="DR_EVENT_FAILBACK_STARTED",
            clock=clock,
        )

    @staticmethod
    async def close(
        session: AsyncSession,
        *,
        actor_id: uuid.UUID,
        event_id: uuid.UUID,
        expected_version: int,
        closure_exception_reason: str | None = None,
        clock: Clock | None = None,
    ) -> DrEvent:
        """Guards in order: D-219 child Events and CLAUDE.md:43's "FAILED_OVER -> CLOSED only when
        failback not required" are hard stops no exception bypasses; then D-227: unfinished Tasks in
        a MONITORING Work Stream block with MONITORING_CLOSURE_WARNING unless the caller gives a
        `closure_exception_reason` and holds CLOSURE_EXCEPTION_OVERRIDE -- one audited override row.
        An exception given when nothing needs one records nothing."""
        clock = clock or SystemClock()
        # Authorization before existence (invariant #1): EVENT_LIFECYCLE_COMMAND is an unconditional
        # Admin/Coordinator-only capability, not participant-scoped, so checking it first means an
        # unauthorized actor gets the same 403 whether the Event exists or not -- loading (and row-
        # locking) first would otherwise 404 a nonexistent Event but 403 an existing one, leaking
        # existence to any authenticated caller (found in review).
        await AuthorizationService.require(session, actor_id, Capability.EVENT_LIFECYCLE_COMMAND)
        event = await _load_for_update(session, event_id)
        if event.version != expected_version:
            raise ConcurrencyConflictError()
        _assert_legal(event.status, "CLOSED")

        if await has_non_terminal_children(session, event.id):
            raise ClosureBlockedByNonTerminalChildError()

        if event.status == "FAILED_OVER":
            failback_pending = await session.execute(
                select(DrApplication.id).where(
                    DrApplication.dr_event_id == event.id,
                    DrApplication.failback_required.is_(True),
                    DrApplication.deleted_at.is_(None),
                )
            )
            if failback_pending.first() is not None:
                raise FailbackRequiredBeforeCloseError()

        monitoring = await unfinished_monitoring_task_ids(session, event.id)
        if monitoring:
            if closure_exception_reason is None:
                raise MonitoringClosureWarningError(monitoring)
            reason = closure_exception_reason.strip()
            if not reason:
                raise OverrideReasonRequiredError("A closure exception")
            await AuthorizationService.require(session, actor_id, Capability.CLOSURE_EXCEPTION_OVERRIDE)
            ids = [str(t) for t in monitoring]
            await record_override(
                session,
                dr_event_id=event.id,
                target_type="DR_EVENT",
                target_id=event.id,
                override_type="CLOSURE_EXCEPTION",
                reason=reason,
                performed_by_user_id=actor_id,
                metadata={"monitoring_task_ids": ids},
            )
            await write_audit(
                session,
                actor_user_id=actor_id,
                entity_type="DR_EVENT",
                entity_id=event.id,
                action="CLOSURE_EXCEPTION_RECORDED",
                dr_event_id=event.id,
                after={"reason": reason, "monitoring_task_ids": ids},
            )

        before = {"status": event.status, "version": event.version}
        event.status = "CLOSED"
        event.completed_at = clock.now()
        return await _mutate_and_finish(
            session,
            event=event,
            actor_id=actor_id,
            before=before,
            new_status="CLOSED",
            action="DR_EVENT_CLOSED",
            clock=clock,
        )

    @staticmethod
    async def cancel(
        session: AsyncSession,
        *,
        actor_id: uuid.UUID,
        event_id: uuid.UUID,
        expected_version: int,
        reason: str,
        clock: Clock | None = None,
    ) -> DrEvent:
        clock = clock or SystemClock()
        if not reason or not reason.strip():
            raise CancelReasonRequiredError()

        # Authorization before existence (invariant #1): EVENT_LIFECYCLE_COMMAND is an unconditional
        # Admin/Coordinator-only capability, not participant-scoped, so checking it first means an
        # unauthorized actor gets the same 403 whether the Event exists or not -- loading (and row-
        # locking) first would otherwise 404 a nonexistent Event but 403 an existing one, leaking
        # existence to any authenticated caller (found in review).
        await AuthorizationService.require(session, actor_id, Capability.EVENT_LIFECYCLE_COMMAND)
        event = await _load_for_update(session, event_id)
        if event.version != expected_version:
            raise ConcurrencyConflictError()
        if event.status in _TERMINAL_STATES:
            raise InvalidEventTransitionError(event.status, "CANCELLED")

        before = {"status": event.status, "version": event.version}
        event.status = "CANCELLED"
        event.cancelled_at = clock.now()
        event.cancel_reason = reason
        return await _mutate_and_finish(
            session,
            event=event,
            actor_id=actor_id,
            before=before,
            new_status="CANCELLED",
            action="DR_EVENT_CANCELLED",
            clock=clock,
            extra_after={"cancel_reason": reason},
        )


# --------------------------------------------------------------------------------------------
# DR Application: technical validation (STATE_MACHINES.md §Application recovery, I-3, D-208, D-254)
# --------------------------------------------------------------------------------------------


class DrApplicationNotFoundError(AppError):
    """404 for a missing DR Application and for one in an Event the caller can't see (invariant #1)."""

    code = "DR_APPLICATION_NOT_FOUND"
    status_code = 404

    def __init__(self) -> None:
        super().__init__("DR Application not found.")


class InvalidApplicationTransitionError(AppError):
    code = "INVALID_TRANSITION"
    status_code = 409

    def __init__(self, command: str, current: str, detail: str | None = None) -> None:
        message = f"Cannot {command.replace('_', '-')} a DR Application that is {current}."
        super().__init__(f"{message} {detail}" if detail else message)


class ApplicationTasksUnfinishedError(AppError):
    code = "APPLICATION_TASKS_UNFINISHED"
    status_code = 409

    def __init__(self, phase: str, task_ids: list[uuid.UUID]) -> None:
        super().__init__(
            f"{phase} Tasks of this Application are still BLOCKED or IN_PROGRESS. Finish them, or submit "
            "with an override_reason.",
            details={"phase": phase, "unfinished_task_ids": [str(t) for t in task_ids]},
        )


class RejectionNoteRequiredError(AppError):
    code = "REJECTION_NOTE_REQUIRED"
    status_code = 422

    def __init__(self) -> None:
        super().__init__(
            "Rejecting an Application validation needs a note (it returns the Application to RECOVERING)."
        )


#: The phase whose Tasks must be finished before technical validation, per current status (I-3).
_PHASE_FOR_SUBMIT: dict[str, str] = {"RECOVERING": "FAILOVER", "FAILBACK_IN_PROGRESS": "FAILBACK"}
_VALIDATABLE = frozenset({"TECHNICAL_VALIDATION", "FAILBACK_IN_PROGRESS"})


async def _load_visible_application(
    session: AsyncSession, actor_id: uuid.UUID, dr_application_id: uuid.UUID
) -> DrApplication:
    dr_application = (
        await session.execute(
            select(DrApplication)
            .where(DrApplication.id == dr_application_id, DrApplication.deleted_at.is_(None))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if dr_application is None or not await user_can_see_event(session, actor_id, dr_application.dr_event_id):
        raise DrApplicationNotFoundError()
    event = await session.get(DrEvent, dr_application.dr_event_id)
    if event is None or event.status in _TERMINAL_STATES:
        # Cancelling or closing an Event does not cascade to its Applications; their commands stop here.
        raise InvalidApplicationTransitionError(
            "act on", dr_application.status, f"Its DR Event is {event.status if event else 'missing'}."
        )
    return dr_application


def _app_snapshot(dr_application: DrApplication) -> dict[str, object]:
    return {"status": dr_application.status, "version": dr_application.version}


async def _finish_application(
    session: AsyncSession,
    *,
    dr_application: DrApplication,
    actor_id: uuid.UUID,
    before: Mapping[str, object],
    action: str,
    clock: Clock,
    extra: Mapping[str, object] | None = None,
    emit_health: bool = False,
) -> DrApplication:
    """Shared tail for Application commands: version, audit, and an `ApplicationHealthChanged` outbox
    event (invariant 11; the D-234 union has no plain Application event and every command here moves
    the validation score -- BUILD-09.plan.md Risk B3). The status write stays in each caller."""
    dr_application.version += 1
    dr_application.updated_at = clock.now()
    await session.flush()
    after: dict[str, object] = {**_app_snapshot(dr_application), **(extra or {})}
    await write_audit(
        session,
        actor_user_id=actor_id,
        entity_type="DR_APPLICATION",
        entity_id=dr_application.id,
        action=action,
        dr_event_id=dr_application.dr_event_id,
        before=dict(before),
        after=after,
    )
    if emit_health:
        await write_outbox(
            session,
            aggregate_type="DR_APPLICATION",
            aggregate_id=dr_application.id,
            event_type="ApplicationHealthChanged",
            payload={"change": action, **after},
            clock=clock,
            dr_event_id=dr_application.dr_event_id,
        )
    return dr_application


class DrApplicationTransitionService:
    """`submit_validation` and `validate` on the canonical DR Application machine (D-208). The two
    side-effect transitions (NOT_STARTED → RECOVERING, FAILED_OVER → FAILBACK_IN_PROGRESS) belong to the
    Event commands above. Never via AI (D-228)."""

    @staticmethod
    async def submit_validation(
        session: AsyncSession,
        *,
        actor_id: uuid.UUID,
        dr_application_id: uuid.UUID,
        expected_version: int,
        override_reason: str | None = None,
        clock: Clock | None = None,
    ) -> DrApplication:
        """RECOVERING → TECHNICAL_VALIDATION, or -- while FAILBACK_IN_PROGRESS -- a failback validation
        request that leaves the status alone. Guard: no Task of the Application in the phase's work
        (FAILOVER / FAILBACK) may be BLOCKED or IN_PROGRESS unless an audited override with reason."""
        clock = clock or SystemClock()
        dr_application = await _load_visible_application(session, actor_id, dr_application_id)
        if not await actor_may_validate_application(session, actor_id, dr_application):
            raise AuthorizationRequiredError()
        if dr_application.version != expected_version:
            raise ConcurrencyConflictError()
        phase = _PHASE_FOR_SUBMIT.get(dr_application.status)
        if phase is None:
            raise InvalidApplicationTransitionError("submit_validation", dr_application.status)
        if await get_pending_validation(session, "DR_APPLICATION", dr_application.id) is not None:
            raise InvalidApplicationTransitionError(
                "submit_validation", dr_application.status, "A validation is already pending."
            )

        unfinished = await unfinished_phase_task_ids(session, dr_application.id, phase)
        reason = override_reason.strip() if override_reason and override_reason.strip() else None
        if unfinished:
            if reason is None:
                raise ApplicationTasksUnfinishedError(phase, unfinished)
            await record_override(
                session,
                dr_event_id=dr_application.dr_event_id,
                target_type="DR_APPLICATION",
                target_id=dr_application.id,
                override_type="APPLICATION_VALIDATION_OVERRIDE",
                reason=reason,
                performed_by_user_id=actor_id,
                metadata={"phase": phase, "unfinished_task_ids": [str(t) for t in unfinished]},
            )
            await write_audit(
                session,
                actor_user_id=actor_id,
                entity_type="DR_APPLICATION",
                entity_id=dr_application.id,
                action="APPLICATION_VALIDATION_OVERRIDE_RECORDED",
                dr_event_id=dr_application.dr_event_id,
                after={"reason": reason, "phase": phase, "unfinished_task_ids": [str(t) for t in unfinished]},
            )

        before = _app_snapshot(dr_application)
        if dr_application.status == "RECOVERING":
            dr_application.status = "TECHNICAL_VALIDATION"
        validation = await open_application_validation(
            session,
            dr_application_id=dr_application.id,
            dr_event_id=dr_application.dr_event_id,
            phase=phase,
            submitted_by_user_id=actor_id,
            clock=clock,
        )
        return await _finish_application(
            session,
            dr_application=dr_application,
            actor_id=actor_id,
            before=before,
            action="DR_APPLICATION_SUBMITTED",
            clock=clock,
            extra={"phase": phase, "validation_id": str(validation.id), "override": reason is not None},
            emit_health=True,  # the pending technical validation moves the Application's validation score
        )

    @staticmethod
    async def validate(
        session: AsyncSession,
        *,
        actor_id: uuid.UUID,
        dr_application_id: uuid.UUID,
        expected_version: int,
        approve: bool,
        note: str | None = None,
        business_confirmed: bool = False,
        business_confirmation_note: str | None = None,
        metrics: RecoveryMetrics | None = None,
        clock: Clock | None = None,
    ) -> DrApplication:
        """Approve: TECHNICAL_VALIDATION → FAILED_OVER, or → COMPLETED when `failback_required=false`;
        FAILBACK_IN_PROGRESS → COMPLETED. `technical_validated_at` is the RTO clock stop (D-257); the
        numbers are `RecoveryMetrics`' (BUILD-10). Reject: TECHNICAL_VALIDATION → RECOVERING with a note;
        a failback rejection keeps FAILBACK_IN_PROGRESS. Never the submitter (Risk B1)."""
        clock = clock or SystemClock()
        now = clock.now()
        dr_application = await _load_visible_application(session, actor_id, dr_application_id)
        if not await actor_may_validate_application(session, actor_id, dr_application):
            raise AuthorizationRequiredError()
        if dr_application.version != expected_version:
            raise ConcurrencyConflictError()
        if dr_application.status not in _VALIDATABLE:
            raise InvalidApplicationTransitionError("validate", dr_application.status)
        validation = await get_pending_validation(
            session, "DR_APPLICATION", dr_application.id, for_update=True
        )
        if validation is None:
            raise InvalidApplicationTransitionError(
                "validate", dr_application.status, "It has no open Validation record."
            )
        if validation.created_by_user_id == actor_id:
            raise SelfValidationForbiddenError()
        clean_note = note.strip() if note and note.strip() else None
        if not approve and clean_note is None:
            raise RejectionNoteRequiredError()

        failback_phase = dr_application.status == "FAILBACK_IN_PROGRESS"
        before = _app_snapshot(dr_application)
        if approve:
            if failback_phase or not dr_application.failback_required:
                dr_application.status = "COMPLETED"
            else:
                dr_application.status = "FAILED_OVER"
            if not failback_phase:
                dr_application.technical_validated_at = now
                await (metrics or NoRecoveryMetricsYet()).on_technical_validation(
                    session, dr_application, validated_at=now
                )
        elif not failback_phase:
            dr_application.status = "RECOVERING"
        await close_application_validation(
            session,
            validation=validation,
            approve=approve,
            validator_user_id=actor_id,
            note=clean_note,
            business_confirmed=business_confirmed,
            business_confirmation_note=(
                business_confirmation_note.strip() if business_confirmation_note else None
            ),
            dr_event_id=dr_application.dr_event_id,
            clock=clock,
        )
        return await _finish_application(
            session,
            dr_application=dr_application,
            actor_id=actor_id,
            before=before,
            action="DR_APPLICATION_VALIDATED" if approve else "DR_APPLICATION_VALIDATION_REJECTED",
            clock=clock,
            extra={"validation_id": str(validation.id), "failback_phase": failback_phase},
            emit_health=True,
        )

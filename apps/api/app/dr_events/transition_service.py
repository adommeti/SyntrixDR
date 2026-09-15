from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import write_audit
from app.core.clock import Clock, SystemClock
from app.core.errors import AppError, ConcurrencyConflictError
from app.core.outbox import write_outbox
from app.dr_events.commands import DrEventNotFoundError
from app.dr_events.models import DrApplication, DrEvent, Override
from app.dr_events.queries import has_non_terminal_children
from app.dr_events.readiness_service import evaluate_readiness
from app.plans_import.commands import capture_baseline_snapshot
from app.users_teams_org.authorization import AuthorizationService, Capability


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
    status_code = 422

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
        event = await _load_for_update(session, event_id)
        await AuthorizationService.require(session, actor_id, Capability.EVENT_LIFECYCLE_COMMAND)
        if event.version != expected_version:
            raise ConcurrencyConflictError()
        _assert_legal(event.status, "ACTIVE")

        readiness_results = await evaluate_readiness(session, event)
        hard_stop_failures = [r for r in readiness_results if r.severity == "HARD_STOP" and not r.satisfied]
        warning_failures = [r for r in readiness_results if r.severity == "WARNING" and not r.satisfied]
        if hard_stop_failures:
            if not override_reason or not override_reason.strip():
                raise ReadinessHardStopError([r.key for r in hard_stop_failures])
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
        event = await _load_for_update(session, event_id)
        await AuthorizationService.require(session, actor_id, Capability.EVENT_LIFECYCLE_COMMAND)
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
        event = await _load_for_update(session, event_id)
        await AuthorizationService.require(session, actor_id, Capability.EVENT_LIFECYCLE_COMMAND)
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
        event = await _load_for_update(session, event_id)
        await AuthorizationService.require(session, actor_id, Capability.EVENT_LIFECYCLE_COMMAND)
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
        clock: Clock | None = None,
    ) -> DrEvent:
        """D-219 child guard is enforced (only needs `parent_dr_event_id`, already in scope).
        D-227's monitoring-Task guard is deferred (BUILD-04.plan.md Risk #3): work_streams/
        tasks_dependencies don't exist yet, so there can never be a non-terminal monitoring Task
        to find -- an honestly-vacuous guard, not a silently-skipped one."""
        clock = clock or SystemClock()
        event = await _load_for_update(session, event_id)
        await AuthorizationService.require(session, actor_id, Capability.EVENT_LIFECYCLE_COMMAND)
        if event.version != expected_version:
            raise ConcurrencyConflictError()
        _assert_legal(event.status, "CLOSED")

        if await has_non_terminal_children(session, event.id):
            raise ClosureBlockedByNonTerminalChildError()

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

        event = await _load_for_update(session, event_id)
        await AuthorizationService.require(session, actor_id, Capability.EVENT_LIFECYCLE_COMMAND)
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

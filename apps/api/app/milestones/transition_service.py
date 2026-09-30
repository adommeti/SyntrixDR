"""The only place a Milestone's `status` changes (STATE_MACHINES.md §Milestone, ADR-009, D-225).

`NOT_STARTED -> IN_PROGRESS/AT_RISK -> READY_FOR_CONFIRMATION -> ACHIEVED | MISSED`

- `recompute` derives progression up to READY_FOR_CONFIRMATION from the contributing Tasks, in the
  transaction of the Task command that changed them (`milestones.commands.on_task_changed`), and
  auto-confirms only an AUTOMATIC Milestone while `milestone.auto_confirm_allowed` is true.
- `confirm` is the human gate (`POST /milestones/{id}/confirm`).
- `miss` is the target-time sweep (`milestones.jobs`), a system action.

ACHIEVED and MISSED are terminal: no reopen command exists (API_CONTRACT.md:150-151). A MISSED gate
keeps its successors not-Ready; the DR proceeds through the Task `start` override, audited.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import write_audit
from app.core.clock import Clock, SystemClock
from app.core.errors import AppError, ConcurrencyConflictError
from app.core.outbox import write_outbox
from app.dr_events.participants import user_can_see_event
from app.dr_events.queries import get_dr_application
from app.milestones.models import Milestone
from app.milestones.policies import actor_may_confirm_milestone
from app.milestones.queries import contributor_states, lock_milestone
from app.policies_admin.queries import PolicyService
from app.users_teams_org.authorization import AuthorizationRequiredError

NOT_STARTED = "NOT_STARTED"
IN_PROGRESS = "IN_PROGRESS"
AT_RISK = "AT_RISK"
READY = "READY_FOR_CONFIRMATION"
ACHIEVED = "ACHIEVED"
MISSED = "MISSED"
TERMINAL = frozenset({ACHIEVED, MISSED})

#: Derived moves only. READY_FOR_CONFIRMATION is left only by `confirm`, auto-confirm or `miss`.
_DERIVED_LEGAL: dict[str, frozenset[str]] = {
    NOT_STARTED: frozenset({IN_PROGRESS, AT_RISK, READY}),
    IN_PROGRESS: frozenset({AT_RISK, READY}),
    AT_RISK: frozenset({IN_PROGRESS, READY}),
}
_DERIVED_ACTION = {
    (NOT_STARTED, IN_PROGRESS): "MILESTONE_STARTED",
    (AT_RISK, IN_PROGRESS): "MILESTONE_BACK_ON_TRACK",
    (NOT_STARTED, AT_RISK): "MILESTONE_AT_RISK",
    (IN_PROGRESS, AT_RISK): "MILESTONE_AT_RISK",
}


class MilestoneNotFoundError(AppError):
    code = "MILESTONE_NOT_FOUND"
    status_code = 404

    def __init__(self) -> None:
        super().__init__("Milestone not found.")


class InvalidMilestoneTransitionError(AppError):
    code = "INVALID_TRANSITION"
    status_code = 409

    def __init__(self, command: str, current: str) -> None:
        super().__init__(
            f"Cannot {command} a Milestone that is {current}.",
            details={"command": command, "status": current},
        )


def derive_target(current: str, contributors: list[tuple[str, bool]]) -> str:
    """Pure: where the contributing Tasks put a Milestone that is `current`.

    - every required contributor COMPLETED (vacuously true with none) -> READY_FOR_CONFIRMATION;
    - else any required contributor BLOCKED -> AT_RISK (BUILD-07.plan.md Risk #1: the spec names the
      state, not its trigger; a blocked required Task is the only signal that needs no new policy);
    - else any contributor out of NOT_STARTED -> IN_PROGRESS.
    A CANCELLED required Task is not done (Risk #2). A target the current state can't move to by
    derivation -- anything backwards, anything from READY/ACHIEVED/MISSED -- leaves it where it is."""
    required = [status for status, is_required in contributors if is_required]
    if all(status == "COMPLETED" for status in required):
        target = READY
    elif any(status == "BLOCKED" for status in required):
        target = AT_RISK
    elif any(status != "NOT_STARTED" for status, _ in contributors):
        target = IN_PROGRESS
    else:
        target = NOT_STARTED
    return target if target in _DERIVED_LEGAL.get(current, frozenset()) else current


def _snapshot(milestone: Milestone) -> dict[str, Any]:
    return {"status": milestone.status, "version": milestone.version}


async def _finish(
    session: AsyncSession,
    *,
    milestone: Milestone,
    actor_id: uuid.UUID | None,
    before: dict[str, Any],
    action: str,
    clock: Clock,
    extra: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    actor_type: str = "USER",
) -> Milestone:
    milestone.version += 1
    milestone.updated_at = clock.now()
    await session.flush()
    after: dict[str, Any] = {"status": milestone.status, "version": milestone.version, **(extra or {})}
    await write_audit(
        session,
        actor_user_id=actor_id,
        actor_type=actor_type,
        entity_type="MILESTONE",
        entity_id=milestone.id,
        action=action,
        dr_event_id=milestone.dr_event_id,
        before=before,
        after=after,
        metadata=metadata,
    )
    await write_outbox(
        session,
        aggregate_type="MILESTONE",
        aggregate_id=milestone.id,
        event_type="MilestoneChanged",  # D-234 V1 union (API_CONTRACT.md:274)
        payload={"change": action, **after},
        clock=clock,
        dr_event_id=milestone.dr_event_id,
    )
    return milestone


async def _auto_confirm_allowed(session: AsyncSession, milestone: Milestone) -> bool:
    application_id = None
    if milestone.dr_application_id is not None:
        dr_app = await get_dr_application(session, milestone.dr_application_id)
        application_id = dr_app.application_id if dr_app is not None else None
    value = await PolicyService.resolve(
        session,
        "milestone.auto_confirm_allowed",
        event_id=milestone.dr_event_id,
        work_stream_id=milestone.work_stream_id,
        application_id=application_id,
    )
    return value is True


class MilestoneTransitionService:
    @staticmethod
    async def recompute(
        session: AsyncSession,
        *,
        milestone_id: uuid.UUID,
        actor_id: uuid.UUID,
        clock: Clock | None = None,
        trigger_task_id: uuid.UUID | None = None,
    ) -> Milestone | None:
        """Derived progression, as a side effect of an already-authorized command: `actor_id` is the
        user whose command changed a contributing Task (or created the Milestone). No version check --
        nothing the caller sent names this Milestone's version."""
        clock = clock or SystemClock()
        milestone = await lock_milestone(session, milestone_id)
        if milestone is None or milestone.status in TERMINAL:
            return milestone
        contributors = await contributor_states(session, milestone.id)
        metadata = {"trigger_task_id": str(trigger_task_id)} if trigger_task_id else None

        target = derive_target(milestone.status, contributors)
        if target != milestone.status:
            before = _snapshot(milestone)
            action = _DERIVED_ACTION.get((milestone.status, target), "MILESTONE_READY_FOR_CONFIRMATION")
            milestone.status = target
            if target == READY:
                milestone.ready_for_confirmation_at = clock.now()
            await _finish(
                session, milestone=milestone, actor_id=actor_id, before=before, action=action,
                clock=clock, metadata=metadata,
            )  # fmt: skip

        # D-225: automatic confirmation only for an AUTOMATIC Milestone, only while the policy allows
        # it, and never for a vacuously-ready one (nothing required -- a human must confirm that).
        if (
            milestone.status == READY
            and target == READY
            and milestone.confirmation_mode == "AUTOMATIC"
            and any(is_required for _, is_required in contributors)
            and await _auto_confirm_allowed(session, milestone)
        ):
            before = _snapshot(milestone)
            milestone.status = ACHIEVED
            milestone.achieved_at = clock.now()
            await _finish(
                session, milestone=milestone, actor_id=actor_id, before=before, action="MILESTONE_ACHIEVED",
                clock=clock, extra={"auto_confirmed": True}, metadata=metadata,
            )  # fmt: skip
        return milestone

    @staticmethod
    async def confirm(
        session: AsyncSession,
        *,
        actor_id: uuid.UUID,
        milestone_id: uuid.UUID,
        expected_version: int,
        clock: Clock | None = None,
    ) -> Milestone:
        """READY_FOR_CONFIRMATION -> ACHIEVED by Admin, Coordinator or this stream's Lead."""
        clock = clock or SystemClock()
        milestone = await lock_milestone(session, milestone_id)
        if milestone is None or not await user_can_see_event(session, actor_id, milestone.dr_event_id):
            raise MilestoneNotFoundError()
        if not await actor_may_confirm_milestone(session, actor_id, milestone):
            raise AuthorizationRequiredError()
        if milestone.version != expected_version:
            raise ConcurrencyConflictError()
        if milestone.status != READY:
            raise InvalidMilestoneTransitionError("confirm", milestone.status)

        before = _snapshot(milestone)
        milestone.status = ACHIEVED
        milestone.achieved_at = clock.now()
        return await _finish(
            session, milestone=milestone, actor_id=actor_id, before=before, action="MILESTONE_ACHIEVED",
            clock=clock, extra={"auto_confirmed": False},
        )  # fmt: skip

    @staticmethod
    async def miss(
        session: AsyncSession, *, milestone_id: uuid.UUID, clock: Clock | None = None
    ) -> Milestone:
        """Any non-terminal state -> MISSED once `target_at` has passed. A system action (no user)."""
        clock = clock or SystemClock()
        milestone = await lock_milestone(session, milestone_id)
        if milestone is None:
            raise MilestoneNotFoundError()
        if milestone.status in TERMINAL:
            raise InvalidMilestoneTransitionError("miss", milestone.status)

        before = _snapshot(milestone)
        target_at = milestone.target_at.isoformat() if milestone.target_at else None
        milestone.status = MISSED
        return await _finish(
            session, milestone=milestone, actor_id=None, before=before, action="MILESTONE_MISSED",
            clock=clock, extra={"target_at": target_at}, actor_type="SYSTEM",
        )  # fmt: skip

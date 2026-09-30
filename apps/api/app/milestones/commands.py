from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import write_audit
from app.core.clock import Clock, SystemClock
from app.core.errors import AppError
from app.core.outbox import write_outbox
from app.dr_events.errors import DrEventNotFoundError
from app.dr_events.participants import user_can_see_event
from app.dr_events.queries import get_dr_application, get_event
from app.milestones.models import Milestone, MilestoneTask
from app.milestones.policies import actor_may_create_milestone
from app.milestones.queries import milestone_ids_for_task
from app.milestones.transition_service import MilestoneTransitionService, auto_confirm_allowed
from app.tasks_dependencies.commands import (
    CrossEventReferenceError,
    add_milestone_gate,
    live_task_ids_in_event,
    lock_event_dependency_graph,
)
from app.users_teams_org.authorization import AuthorizationRequiredError
from app.users_teams_org.commands import UserNotFoundError
from app.users_teams_org.queries import get_user
from app.work_streams.queries import get_work_stream


async def on_task_changed(
    session: AsyncSession, *, task_id: uuid.UUID, actor_id: uuid.UUID, clock: Clock
) -> None:
    """Re-derive every Milestone this Task contributes to, in the Task command's own transaction.

    Called by `TaskTransitionService` after each Task transition. Milestones are locked in id order
    (`milestone_ids_for_task`), so two Task commands touching overlapping Milestones can't deadlock."""
    for milestone_id in await milestone_ids_for_task(session, task_id):
        await MilestoneTransitionService.recompute(
            session, milestone_id=milestone_id, actor_id=actor_id, clock=clock, trigger_task_id=task_id
        )


class MilestoneContextRequiredError(AppError):
    code = "MILESTONE_CONTEXT_REQUIRED"
    status_code = 422

    def __init__(self) -> None:
        super().__init__("A Milestone belongs to a Work Stream, a DR Application, or both.")


class AutoConfirmNotAllowedError(AppError):
    code = "MILESTONE_AUTO_CONFIRM_NOT_ALLOWED"
    status_code = 422

    def __init__(self) -> None:
        super().__init__(
            "AUTOMATIC confirmation is off by policy (milestone.auto_confirm_allowed); create it MANUAL."
        )


async def create_milestone(
    session: AsyncSession,
    *,
    actor_id: uuid.UUID,
    dr_event_id: uuid.UUID,
    name: str,
    description: str | None = None,
    work_stream_id: uuid.UUID | None = None,
    dr_application_id: uuid.UUID | None = None,
    owner_user_id: uuid.UUID | None = None,
    confirmation_mode: str = "MANUAL",
    target_at: datetime | None = None,
    contributing_tasks: list[tuple[uuid.UUID, bool]] | None = None,
    gates: list[tuple[uuid.UUID, str]] | None = None,
    clock: Clock | None = None,
) -> Milestone:
    """`POST /dr-events/{id}/milestones`. Order: Event visible (404) -> context (422) -> references in
    this Event (422 `CROSS_EVENT_REFERENCE`) / owner exists (404) -> authority (403) -> D-225 mode
    (422) -> create + contributions under the graph lock -> gates through `add_milestone_gate` (its own
    authority, cycle check) -> derive the initial state. Any failure rolls the whole request back."""
    clock = clock or SystemClock()
    contributing_tasks = contributing_tasks or []
    gates = gates or []
    if await get_event(session, dr_event_id) is None or not await user_can_see_event(
        session, actor_id, dr_event_id
    ):
        raise DrEventNotFoundError()
    if work_stream_id is None and dr_application_id is None:
        raise MilestoneContextRequiredError()
    if work_stream_id is not None:
        ws = await get_work_stream(session, work_stream_id)
        if ws is None or ws.dr_event_id != dr_event_id:
            raise CrossEventReferenceError("work_stream_id")
    if dr_application_id is not None:
        dr_app = await get_dr_application(session, dr_application_id)
        if dr_app is None or dr_app.dr_event_id != dr_event_id or dr_app.deleted_at is not None:
            raise CrossEventReferenceError("dr_application_id")
    for field, task_ids in (
        ("contributing_tasks", [t for t, _ in contributing_tasks]),
        ("gates", [t for t, _ in gates]),
    ):
        if await live_task_ids_in_event(session, dr_event_id, task_ids) != set(task_ids):
            raise CrossEventReferenceError(field)
    if owner_user_id is not None and await get_user(session, owner_user_id) is None:
        raise UserNotFoundError()
    if not await actor_may_create_milestone(session, actor_id, work_stream_id):
        raise AuthorizationRequiredError()

    milestone = Milestone(
        dr_event_id=dr_event_id,
        work_stream_id=work_stream_id,
        dr_application_id=dr_application_id,
        name=name.strip(),
        description=description,
        owner_user_id=owner_user_id,
        confirmation_mode=confirmation_mode,
        target_at=target_at,
        created_by_user_id=actor_id,
        created_at=clock.now(),
        updated_at=clock.now(),
    )
    if confirmation_mode == "AUTOMATIC" and not await auto_confirm_allowed(session, milestone):
        raise AutoConfirmNotAllowedError()

    # Contributions are graph edges (Task -> Milestone): written under ADR-041's lock. The new
    # Milestone has no gates yet, so they can't close a cycle; the gates below are checked.
    await lock_event_dependency_graph(session, dr_event_id)
    session.add(milestone)
    await session.flush()
    for task_id, is_required in contributing_tasks:
        session.add(
            MilestoneTask(
                milestone_id=milestone.id, task_id=task_id, is_required=is_required, created_at=clock.now()
            )
        )
    await session.flush()
    after: dict[str, Any] = {
        "name": milestone.name,
        "status": milestone.status,
        "confirmation_mode": confirmation_mode,
        "work_stream_id": str(work_stream_id) if work_stream_id else None,
        "dr_application_id": str(dr_application_id) if dr_application_id else None,
        "owner_user_id": str(owner_user_id) if owner_user_id else None,
        "target_at": target_at.isoformat() if target_at else None,
        "contributing_tasks": [{"task_id": str(t), "is_required": r} for t, r in contributing_tasks],
        "version": milestone.version,
    }
    await write_audit(
        session,
        actor_user_id=actor_id,
        entity_type="MILESTONE",
        entity_id=milestone.id,
        action="MILESTONE_CREATED",
        dr_event_id=dr_event_id,
        after=after,
    )
    await write_outbox(
        session,
        aggregate_type="MILESTONE",
        aggregate_id=milestone.id,
        event_type="MilestoneChanged",
        payload={"change": "MILESTONE_CREATED", "status": milestone.status, "version": milestone.version},
        clock=clock,
        dr_event_id=dr_event_id,
    )

    for task_id, strength in gates:
        await add_milestone_gate(
            session, actor_id=actor_id, milestone_id=milestone.id, successor_task_id=task_id,
            strength=strength, clock=clock,
        )  # fmt: skip
    await MilestoneTransitionService.recompute(
        session, milestone_id=milestone.id, actor_id=actor_id, clock=clock
    )
    return milestone

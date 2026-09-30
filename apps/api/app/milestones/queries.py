from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dr_events.participants import user_can_see_event
from app.milestones.models import Milestone, MilestoneTask
from app.tasks_dependencies.queries import task_statuses


async def lock_milestone(session: AsyncSession, milestone_id: uuid.UUID) -> Milestone | None:
    """The live Milestone row under `FOR UPDATE`. `populate_existing` so an identity-map copy can't
    hide the locked row's version (BUILD-06 lesson)."""
    return (
        await session.execute(
            select(Milestone)
            .where(Milestone.id == milestone_id, Milestone.deleted_at.is_(None))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def get_visible_milestone(
    session: AsyncSession, actor_id: uuid.UUID, milestone_id: uuid.UUID
) -> Milestone | None:
    """None when missing *or* in an Event the actor can't see -- both render as the same 404."""
    milestone = (
        await session.execute(
            select(Milestone).where(Milestone.id == milestone_id, Milestone.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if milestone is None or not await user_can_see_event(session, actor_id, milestone.dr_event_id):
        return None
    return milestone


async def contributor_states(session: AsyncSession, milestone_id: uuid.UUID) -> list[tuple[str, bool]]:
    """(Task status, is_required) for each *live* contributing Task."""
    rows = (
        await session.execute(
            select(MilestoneTask.task_id, MilestoneTask.is_required).where(
                MilestoneTask.milestone_id == milestone_id
            )
        )
    ).all()
    statuses = await task_statuses(session, [task_id for task_id, _ in rows])
    return [(statuses[task_id], required) for task_id, required in rows if task_id in statuses]


async def milestone_ids_for_task(session: AsyncSession, task_id: uuid.UUID) -> list[uuid.UUID]:
    """Live Milestones this Task contributes to, ordered by id (the lock order)."""
    rows = await session.execute(
        select(Milestone.id)
        .join(MilestoneTask, MilestoneTask.milestone_id == Milestone.id)
        .where(MilestoneTask.task_id == task_id, Milestone.deleted_at.is_(None))
        .order_by(Milestone.id)
    )
    return [milestone_id for (milestone_id,) in rows]


async def list_milestones(session: AsyncSession, dr_event_id: uuid.UUID) -> list[Milestone]:
    rows = await session.execute(
        select(Milestone)
        .where(Milestone.dr_event_id == dr_event_id, Milestone.deleted_at.is_(None))
        .order_by(Milestone.created_at, Milestone.id)
    )
    return list(rows.scalars().all())


async def contributions_by_milestone(
    session: AsyncSession, milestone_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[MilestoneTask]]:
    by_milestone: dict[uuid.UUID, list[MilestoneTask]] = {m: [] for m in milestone_ids}
    if not milestone_ids:
        return by_milestone
    rows = await session.execute(
        select(MilestoneTask)
        .where(MilestoneTask.milestone_id.in_(milestone_ids))
        .order_by(MilestoneTask.created_at, MilestoneTask.task_id)
    )
    for contribution in rows.scalars():
        by_milestone[contribution.milestone_id].append(contribution)
    return by_milestone


async def manual_milestone_without_owner_exists(session: AsyncSession, dr_event_id: uuid.UUID) -> bool:
    """D-224 `readiness.critical_milestone_owner`: a critical Milestone is a MANUAL one (ADR-009)."""
    row = await session.execute(
        select(Milestone.id).where(
            Milestone.dr_event_id == dr_event_id,
            Milestone.deleted_at.is_(None),
            Milestone.confirmation_mode == "MANUAL",
            Milestone.owner_user_id.is_(None),
        )
    )
    return row.first() is not None


async def overdue_milestone_ids(session: AsyncSession, *, now: datetime, limit: int = 500) -> list[uuid.UUID]:
    """Non-terminal Milestones whose target has passed, for the MISSED sweep."""
    rows = await session.execute(
        select(Milestone.id)
        .where(
            Milestone.deleted_at.is_(None),
            Milestone.target_at.is_not(None),
            Milestone.target_at <= now,
            Milestone.status.not_in(("ACHIEVED", "MISSED")),
        )
        .order_by(Milestone.target_at, Milestone.id)
        .limit(limit)
    )
    return [milestone_id for (milestone_id,) in rows]

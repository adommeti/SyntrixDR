from __future__ import annotations

import uuid

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.blockers.models import INACTIVE_BLOCKER_STATUS, Blocker


async def count_active_blockers(session: AsyncSession, task_id: uuid.UUID) -> int:
    """Blockers on this Task that are not yet CLOSED (and not soft-deleted) -- the count
    invariant #10 and the `resume` guard both depend on."""
    result = await session.execute(
        select(func.count())
        .select_from(Blocker)
        .where(
            Blocker.task_id == task_id,
            Blocker.deleted_at.is_(None),
            Blocker.status != INACTIVE_BLOCKER_STATUS,
        )
    )
    return result.scalar_one()


async def count_active_blockers_by_task(
    session: AsyncSession, task_ids: list[uuid.UUID] | Select[tuple[uuid.UUID]]
) -> dict[uuid.UUID, int]:
    """`count_active_blockers` for many Tasks in one query -- a list of ids, or a subquery selecting
    them. Tasks with none are absent."""
    if isinstance(task_ids, list) and not task_ids:
        return {}
    rows = await session.execute(
        select(Blocker.task_id, func.count())
        .where(
            Blocker.task_id.in_(task_ids),
            Blocker.deleted_at.is_(None),
            Blocker.status != INACTIVE_BLOCKER_STATUS,
        )
        .group_by(Blocker.task_id)
    )
    return {task_id: n for task_id, n in rows}


async def lock_blocker(session: AsyncSession, blocker_id: uuid.UUID) -> Blocker | None:
    """The live Blocker row under `FOR UPDATE`; `populate_existing` so an identity-map copy can't hide
    the locked row's version (ADR-036)."""
    return (
        await session.execute(
            select(Blocker)
            .where(Blocker.id == blocker_id, Blocker.deleted_at.is_(None))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def list_blockers(
    session: AsyncSession,
    dr_event_id: uuid.UUID,
    *,
    status: str | None = None,
    team_id: uuid.UUID | None = None,
    task_id: uuid.UUID | None = None,
    include_closed: bool = False,
) -> list[Blocker]:
    """`GET /dr-events/{id}/blockers` (API_CONTRACT.md:180): the Event's active Blockers, oldest
    first, optionally one Team's queue (FROZEN §108), one status, or one Task; `include_closed` adds
    the history."""
    from app.tasks_dependencies.models import Task  # table object only; the FK target is registered

    query = (
        select(Blocker)
        .join(Task, Task.id == Blocker.task_id)
        .where(Task.dr_event_id == dr_event_id, Blocker.deleted_at.is_(None))
        .order_by(Blocker.blocked_at, Blocker.id)
    )
    if not include_closed:
        query = query.where(Blocker.status != INACTIVE_BLOCKER_STATUS)
    if status is not None:
        query = query.where(Blocker.status == status)
    if team_id is not None:
        query = query.where(Blocker.blocker_team_id == team_id)
    if task_id is not None:
        query = query.where(Blocker.task_id == task_id)
    return list((await session.execute(query)).scalars().all())

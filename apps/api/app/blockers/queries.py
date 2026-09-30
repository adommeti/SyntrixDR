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

from __future__ import annotations

import uuid

from sqlalchemy import func, select
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

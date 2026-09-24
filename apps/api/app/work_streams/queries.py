from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.work_streams.models import WorkStream


async def list_work_streams(session: AsyncSession, dr_event_id: uuid.UUID) -> list[WorkStream]:
    """Live streams in display order: `sequence_order` first (unset last), then name."""
    result = await session.execute(
        select(WorkStream)
        .where(WorkStream.dr_event_id == dr_event_id, WorkStream.deleted_at.is_(None))
        .order_by(WorkStream.sequence_order.asc().nulls_last(), func.lower(WorkStream.name), WorkStream.id)
    )
    return list(result.scalars().all())


async def is_work_stream_lead(session: AsyncSession, work_stream_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    """Whether `user_id` is the stream's designated Lead (`lead_user_id`). This is the Lead D-224's
    "every Work Stream has a Lead" refers to, so it carries Work Stream Lead authority in that stream
    (BUILD-06.plan.md Risk #19) -- no role grant required."""
    result = await session.execute(
        select(WorkStream.id).where(
            WorkStream.id == work_stream_id,
            WorkStream.lead_user_id == user_id,
            WorkStream.deleted_at.is_(None),
        )
    )
    return result.first() is not None

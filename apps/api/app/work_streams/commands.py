from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import write_audit
from app.work_streams.models import WorkStream


async def get_or_create_work_stream(
    session: AsyncSession, *, dr_event_id: uuid.UUID, name: str, actor_id: uuid.UUID
) -> WorkStream:
    """Case-insensitive exact match on `name`, scoped to `dr_event_id`, among non-deleted rows.
    No fuzzy matching here -- BUILD-05's Excel-import `accept` step is the only caller today, and
    a wrong Work Stream match would misfile Tasks (BUILD-05.plan.md Risk #5's same reasoning for
    entity-name resolution)."""
    existing = await session.execute(
        select(WorkStream).where(
            WorkStream.dr_event_id == dr_event_id,
            func.lower(WorkStream.name) == name.lower(),
            WorkStream.deleted_at.is_(None),
        )
    )
    work_stream = existing.scalar_one_or_none()
    if work_stream is not None:
        return work_stream

    work_stream = WorkStream(dr_event_id=dr_event_id, name=name)
    session.add(work_stream)
    await session.flush()
    await write_audit(
        session,
        actor_user_id=actor_id,
        entity_type="WORK_STREAM",
        entity_id=work_stream.id,
        action="WORK_STREAM_CREATED",
        dr_event_id=dr_event_id,
        after={"name": name, "stream_type": work_stream.stream_type},
    )
    return work_stream

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.validation_evidence.models import Validation


async def get_pending_task_validation(
    session: AsyncSession, task_id: uuid.UUID, *, for_update: bool = False
) -> Validation | None:
    """The single open (PENDING) Validation for a Task. A Task in READY_FOR_VALIDATION always has
    exactly one; a rejected submission's row stays REJECTED and a re-submit opens a new one, so
    older rows never match here."""
    stmt = select(Validation).where(
        Validation.target_type == "TASK",
        Validation.target_id == task_id,
        Validation.status == "PENDING",
        Validation.deleted_at.is_(None),
    )
    if for_update:
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()

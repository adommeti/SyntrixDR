from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.issues_findings.models import IssueFinding


async def get_issue(session: AsyncSession, issue_id: uuid.UUID) -> IssueFinding | None:
    return (
        await session.execute(
            select(IssueFinding).where(IssueFinding.id == issue_id, IssueFinding.deleted_at.is_(None))
        )
    ).scalar_one_or_none()


async def lock_issue(session: AsyncSession, issue_id: uuid.UUID) -> IssueFinding | None:
    """The live row under `FOR UPDATE`, refreshed (ADR-036)."""
    return (
        await session.execute(
            select(IssueFinding)
            .where(IssueFinding.id == issue_id, IssueFinding.deleted_at.is_(None))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def list_issues(
    session: AsyncSession,
    dr_event_id: uuid.UUID,
    *,
    status: str | None = None,
    task_id: uuid.UUID | None = None,
) -> list[IssueFinding]:
    query = (
        select(IssueFinding)
        .where(IssueFinding.dr_event_id == dr_event_id, IssueFinding.deleted_at.is_(None))
        .order_by(IssueFinding.created_at, IssueFinding.id)
    )
    if status is not None:
        query = query.where(IssueFinding.status == status)
    if task_id is not None:
        query = query.where(IssueFinding.task_id == task_id)
    return list((await session.execute(query)).scalars().all())

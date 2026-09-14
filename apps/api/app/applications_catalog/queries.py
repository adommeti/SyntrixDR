from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.applications_catalog.models import Application, ApplicationOwner, Tier


async def get_application(session: AsyncSession, application_id: uuid.UUID) -> Application | None:
    return await session.get(Application, application_id)


async def list_applications(session: AsyncSession) -> list[Application]:
    result = await session.execute(
        select(Application).where(Application.deleted_at.is_(None)).order_by(Application.name)
    )
    return list(result.scalars().all())


async def list_application_owners(session: AsyncSession, application_id: uuid.UUID) -> list[ApplicationOwner]:
    result = await session.execute(
        select(ApplicationOwner)
        .where(
            ApplicationOwner.application_id == application_id,
            ApplicationOwner.deleted_at.is_(None),
        )
        .order_by(ApplicationOwner.owner_type, ApplicationOwner.owner_order)
    )
    return list(result.scalars().all())


async def list_tiers(session: AsyncSession) -> list[Tier]:
    result = await session.execute(select(Tier).order_by(Tier.rank))
    return list(result.scalars().all())


async def get_tier_by_code(session: AsyncSession, code: str) -> Tier | None:
    result = await session.execute(select(Tier).where(Tier.code == code))
    return result.scalar_one_or_none()


async def get_application_history(
    session: AsyncSession, application_id: uuid.UUID
) -> list[dict[str, object]]:
    """D-212 historical DR comparison. No DR Event/DR Application participation data exists
    until BUILD-04+ (dr_events, rto_rpo_health) — returns a real, correctly-shaped empty list
    rather than fabricating rows or 501ing (BUILD-03.plan.md Risk #2)."""
    _ = session, application_id
    return []

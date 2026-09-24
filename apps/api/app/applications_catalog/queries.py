from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.applications_catalog.models import Application, ApplicationOwner, Tier


async def get_application(session: AsyncSession, application_id: uuid.UUID) -> Application | None:
    return await session.get(Application, application_id)


async def list_applications(session: AsyncSession) -> list[Application]:
    result = await session.execute(
        select(Application).where(Application.deleted_at.is_(None)).order_by(Application.name)
    )
    return list(result.scalars().all())


async def get_application_by_name(session: AsyncSession, name: str) -> Application | None:
    """Case-insensitive exact match among non-deleted Applications. Used by BUILD-05's Excel
    import row resolution for the `Application` column."""
    result = await session.execute(
        select(Application).where(
            func.lower(Application.name) == name.lower(), Application.deleted_at.is_(None)
        )
    )
    return result.scalar_one_or_none()


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


async def list_application_ids_with_primary_owner(
    session: AsyncSession, application_ids: list[uuid.UUID], owner_type: str
) -> set[uuid.UUID]:
    """Application ids (subset of `application_ids`) that have an owner_order=1 owner of `owner_type`."""
    if not application_ids:
        return set()
    result = await session.execute(
        select(ApplicationOwner.application_id).where(
            ApplicationOwner.application_id.in_(application_ids),
            ApplicationOwner.owner_type == owner_type,
            ApplicationOwner.owner_order == 1,
            ApplicationOwner.deleted_at.is_(None),
        )
    )
    return set(result.scalars().all())


async def list_tiers(session: AsyncSession) -> list[Tier]:
    result = await session.execute(select(Tier).order_by(Tier.rank))
    return list(result.scalars().all())


async def get_tier_by_code(session: AsyncSession, code: str) -> Tier | None:
    result = await session.execute(select(Tier).where(Tier.code == code))
    return result.scalar_one_or_none()


async def get_tier(session: AsyncSession, tier_id: uuid.UUID) -> Tier | None:
    return await session.get(Tier, tier_id)


async def get_application_history(
    session: AsyncSession, application_id: uuid.UUID
) -> list[dict[str, object]]:
    """D-212 historical DR comparison. No DR Event/DR Application participation data exists
    until BUILD-04+ (dr_events, rto_rpo_health) — returns a real, correctly-shaped empty list
    rather than fabricating rows or 501ing (BUILD-03.plan.md Risk #2)."""
    _ = session, application_id
    return []


async def is_system_application_owner(
    session: AsyncSession, application_id: uuid.UUID, user_id: uuid.UUID
) -> bool:
    """Any of the up-to-3 `SYSTEM_APPLICATION` slots (D-209, D-254). Never `BUSINESS`: technical
    validation belongs to the Application/System Owner, not the Business Owner (RBAC_MATRIX.md
    security notes). Slot ownership is its own table, not a role grant, so `AuthorizationService`
    can't see it -- callers needing "is this user an owner" must ask here."""
    result = await session.execute(
        select(ApplicationOwner.id).where(
            ApplicationOwner.application_id == application_id,
            ApplicationOwner.user_id == user_id,
            ApplicationOwner.owner_type == "SYSTEM_APPLICATION",
            ApplicationOwner.deleted_at.is_(None),
        )
    )
    return result.first() is not None

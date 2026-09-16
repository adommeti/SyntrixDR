from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dr_events.models import DrApplication, DrEvent
from app.dr_events.participants import user_can_see_event, visible_event_ids_for_user
from app.users_teams_org.authorization import AuthorizationService


async def get_event(session: AsyncSession, event_id: uuid.UUID) -> DrEvent | None:
    return await session.get(DrEvent, event_id)


async def list_events(session: AsyncSession) -> list[DrEvent]:
    result = await session.execute(select(DrEvent).where(DrEvent.deleted_at.is_(None)).order_by(DrEvent.name))
    return list(result.scalars().all())


async def list_visible_events(session: AsyncSession, user_id: uuid.UUID) -> list[DrEvent]:
    """Invariant #1/#10: UUID knowledge is not authorization; default visibility is
    `dr_event_participants` (found in review — `GET /dr-events` had no visibility filter at
    all). Global Admin/GLOBAL_READONLY see every Event; everyone else sees only Events they're
    an explicit participant of."""
    if await AuthorizationService.is_global_admin(
        session, user_id
    ) or await AuthorizationService.is_global_readonly(session, user_id):
        return await list_events(session)
    result = await session.execute(
        select(DrEvent)
        .where(DrEvent.deleted_at.is_(None), DrEvent.id.in_(visible_event_ids_for_user(user_id)))
        .order_by(DrEvent.name)
    )
    return list(result.scalars().all())


async def get_visible_event(session: AsyncSession, user_id: uuid.UUID, event_id: uuid.UUID) -> DrEvent | None:
    """Returns `None` (not a 403) for a foreign Event the caller can't see, so the route 404s
    without confirming the Event exists (found in review, same IDOR shape as `list_visible_events`)."""
    event = await get_event(session, event_id)
    if event is None:
        return None
    if not await user_can_see_event(session, user_id, event_id):
        return None
    return event


async def list_dr_applications(session: AsyncSession, dr_event_id: uuid.UUID) -> list[DrApplication]:
    result = await session.execute(
        select(DrApplication).where(
            DrApplication.dr_event_id == dr_event_id, DrApplication.deleted_at.is_(None)
        )
    )
    return list(result.scalars().all())


async def has_non_terminal_children(session: AsyncSession, parent_dr_event_id: uuid.UUID) -> bool:
    """D-219: a parent Event cannot CLOSE while any non-cancelled child Event is non-terminal
    (CLOSED/CANCELLED are the only terminal states)."""
    result = await session.execute(
        select(DrEvent.id).where(
            DrEvent.parent_dr_event_id == parent_dr_event_id,
            DrEvent.deleted_at.is_(None),
            DrEvent.status.notin_(["CLOSED", "CANCELLED"]),
        )
    )
    return result.first() is not None


async def list_descendant_event_ids(session: AsyncSession, event_id: uuid.UUID) -> list[uuid.UUID]:
    """`event_id` plus every Event reachable by following `parent_dr_event_id` down the tree
    (a comprehensive DR's sub-DRs, D-219). BFS in Python, not a recursive CTE: the child depth
    here is small (comprehensive DR -> sub-DRs), and no other module has an existing recursive-
    query precedent to follow."""
    seen = {event_id}
    frontier = [event_id]
    while frontier:
        result = await session.execute(
            select(DrEvent.id).where(DrEvent.parent_dr_event_id.in_(frontier), DrEvent.deleted_at.is_(None))
        )
        frontier = [row_id for row_id in result.scalars().all() if row_id not in seen]
        seen.update(frontier)
    return list(seen)


async def list_descendant_dr_applications(session: AsyncSession, event_id: uuid.UUID) -> list[DrApplication]:
    """D-219: "parent aggregates all descendant DR Applications for health/treemap/reporting" --
    this is the aggregation query stub those future increments (rto_rpo_health, reporting_exports)
    will consume; BUILD-04 has no health/treemap/reporting of its own to wire it into yet."""
    descendant_ids = await list_descendant_event_ids(session, event_id)
    result = await session.execute(
        select(DrApplication).where(
            DrApplication.dr_event_id.in_(descendant_ids), DrApplication.deleted_at.is_(None)
        )
    )
    return list(result.scalars().all())

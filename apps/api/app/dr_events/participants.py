from __future__ import annotations

import uuid

from sqlalchemy import Select, and_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock, SystemClock
from app.dr_events.models import DrEventParticipant
from app.users_teams_org.authorization import AuthorizationService


async def enrol_participant(
    session: AsyncSession,
    dr_event_id: uuid.UUID,
    user_id: uuid.UUID,
    source: str,
    *,
    added_by_user_id: uuid.UUID | None = None,
) -> None:
    """Idempotent: re-enrolling an already-active `(dr_event_id, user_id, source)` is a no-op,
    matching the partial unique index `ux_dr_event_participants_active` (D-222). The insert is
    wrapped in a savepoint so two concurrent enrolments for the same key race safely: the loser's
    `IntegrityError` (from the unique index) is swallowed as a no-op instead of bubbling as a 500,
    the same pattern as `core/idempotency.py`'s fresh-key race."""
    existing = await session.execute(
        select(DrEventParticipant.id).where(
            and_(
                DrEventParticipant.dr_event_id == dr_event_id,
                DrEventParticipant.user_id == user_id,
                DrEventParticipant.source == source,
                DrEventParticipant.removed_at.is_(None),
            )
        )
    )
    if existing.scalar_one_or_none() is not None:
        return
    try:
        async with session.begin_nested():
            session.add(
                DrEventParticipant(
                    dr_event_id=dr_event_id,
                    user_id=user_id,
                    source=source,
                    added_by_user_id=added_by_user_id,
                )
            )
            await session.flush()
    except IntegrityError:
        pass


async def remove_participant(
    session: AsyncSession,
    dr_event_id: uuid.UUID,
    user_id: uuid.UUID,
    source: str,
    *,
    clock: Clock | None = None,
) -> None:
    """Soft-remove: sets `removed_at` on the active row for this source, if any."""
    clock = clock or SystemClock()
    result = await session.execute(
        select(DrEventParticipant).where(
            and_(
                DrEventParticipant.dr_event_id == dr_event_id,
                DrEventParticipant.user_id == user_id,
                DrEventParticipant.source == source,
                DrEventParticipant.removed_at.is_(None),
            )
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        return

    row.removed_at = clock.now()
    await session.flush()


async def is_participant(session: AsyncSession, dr_event_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    """Global Admins are implicit participants of every Event (D-222) — checked by the caller via
    `AuthorizationService`, not here, since this function only knows about explicit rows. Callers
    needing the full visibility rule should check `AuthorizationService.can(..., GLOBAL_ADMIN)` OR
    this function, not this function alone."""
    result = await session.execute(
        select(DrEventParticipant.id)
        .where(
            and_(
                DrEventParticipant.dr_event_id == dr_event_id,
                DrEventParticipant.user_id == user_id,
                DrEventParticipant.removed_at.is_(None),
            )
        )
        # A user can hold multiple simultaneous active rows for the same (event, user) via
        # different sources (e.g. EXPLICIT + ROLE) — the unique index is per-source, not
        # per-(event, user). scalar_one_or_none() would raise MultipleResultsFound in that case
        # (found in review); this only checks existence, so cap at one row.
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


def visible_event_ids_for_user(user_id: uuid.UUID) -> Select[tuple[uuid.UUID]]:
    """The set of `dr_event_id`s explicitly visible to `user_id` (not counting the Global Admin
    implicit-everywhere rule — callers check that via `AuthorizationService` first and skip this
    filter entirely for admins). Event-scoped modules filter their own query by:
    `select(DrEvent).where(DrEvent.id.in_(visible_event_ids_for_user(user_id)))`. A foreign/invisible
    Event matches no row; mapping that empty result to 404 is the route's job, not this query's."""
    return select(DrEventParticipant.dr_event_id).where(
        and_(DrEventParticipant.user_id == user_id, DrEventParticipant.removed_at.is_(None))
    )


async def user_can_see_event(session: AsyncSession, user_id: uuid.UUID, dr_event_id: uuid.UUID) -> bool:
    """Global Admin implicit OR an active explicit participant row (D-222). Goes through
    `AuthorizationService.is_global_admin` (not a raw role-table check) so a LOCAL GLOBAL_ADMIN
    without TOTP enrolled (D-235) doesn't get implicit visibility either — found in review that a
    second, independent GLOBAL_ADMIN check here would have silently bypassed that same gate."""
    if await AuthorizationService.is_global_admin(session, user_id):
        return True
    return await is_participant(session, dr_event_id, user_id)

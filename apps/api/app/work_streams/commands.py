from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import write_audit
from app.core.clock import Clock, SystemClock
from app.core.errors import AppError
from app.dr_events.errors import DrEventNotFoundError
from app.dr_events.participants import enrol_participant, user_can_see_event
from app.dr_events.queries import get_event
from app.users_teams_org.authorization import AuthorizationService, Capability
from app.users_teams_org.commands import TeamNotFoundError, UserNotFoundError
from app.users_teams_org.queries import get_team, get_user
from app.work_streams.models import WorkStream


class WorkStreamExistsError(AppError):
    code = "WORK_STREAM_EXISTS"
    status_code = 409

    def __init__(self) -> None:
        super().__init__("A Work Stream with that name already exists in this DR Event.")


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


async def create_work_stream(
    session: AsyncSession,
    *,
    actor_id: uuid.UUID,
    dr_event_id: uuid.UUID,
    name: str,
    stream_type: str = "CUSTOM",
    description: str | None = None,
    lead_user_id: uuid.UUID | None = None,
    owning_team_id: uuid.UUID | None = None,
    sequence_order: int | None = None,
    clock: Clock | None = None,
) -> WorkStream:
    """`POST /dr-events/{id}/work-streams` (API_CONTRACT.md:149, D-227). Visibility before capability,
    so an outsider gets the same 404 whether or not the Event exists. The Lead is auto-enrolled as a
    participant (D-222). No outbox event: D-234's V1 union has no Work Stream type
    (BUILD-06.plan.md Risk #20)."""
    clock = clock or SystemClock()
    if await get_event(session, dr_event_id) is None or not await user_can_see_event(
        session, actor_id, dr_event_id
    ):
        raise DrEventNotFoundError()
    await AuthorizationService.require(session, actor_id, Capability.MANAGE_WORK_STREAMS)
    if lead_user_id is not None and await get_user(session, lead_user_id) is None:
        raise UserNotFoundError()
    if owning_team_id is not None and await get_team(session, owning_team_id) is None:
        raise TeamNotFoundError()

    clean_name = name.strip()
    now = clock.now()
    work_stream = WorkStream(
        dr_event_id=dr_event_id,
        name=clean_name,
        description=description,
        stream_type=stream_type,
        lead_user_id=lead_user_id,
        owning_team_id=owning_team_id,
        sequence_order=sequence_order,
        created_at=now,
        updated_at=now,
    )
    try:
        async with session.begin_nested():
            session.add(work_stream)
            await session.flush()
    except IntegrityError:
        # ux_work_stream_event_name (lower(name), live rows) is the single source of truth for
        # uniqueness -- it catches a plain duplicate and a concurrent one alike. Lead and Team were
        # checked above, so no other constraint on this insert can raise IntegrityError.
        raise WorkStreamExistsError() from None

    await write_audit(
        session,
        actor_user_id=actor_id,
        entity_type="WORK_STREAM",
        entity_id=work_stream.id,
        action="WORK_STREAM_CREATED",
        dr_event_id=dr_event_id,
        after={
            "name": clean_name,
            "stream_type": stream_type,
            "lead_user_id": str(lead_user_id) if lead_user_id else None,
            "owning_team_id": str(owning_team_id) if owning_team_id else None,
        },
    )
    if lead_user_id is not None:
        await enrol_participant(
            session, dr_event_id, lead_user_id, "WORK_STREAM_LEAD", added_by_user_id=actor_id
        )
    return work_stream

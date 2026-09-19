"""Command-layer tests for `work_streams/commands.py::get_or_create_work_stream` (BUILD-05).

This module is deliberately minimal (BUILD-05.plan.md Risk #2) -- only the get-or-create helper
Excel import's `accept` step needs. No routes exist yet, so these tests call the command directly
against the `session` fixture, no HTTP app involved.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.work_streams.commands import get_or_create_work_stream

pytestmark = pytest.mark.domain


async def _create_entra_admin(session: AsyncSession) -> uuid.UUID:
    user_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO users (id, identity_type, display_name, email, entra_object_id) "
            "VALUES (:id, 'ENTRA', 'Work Stream Admin', :email, :oid)"
        ),
        {"id": user_id, "email": f"{user_id}@example.test", "oid": str(uuid.uuid4())},
    )
    await session.flush()
    return user_id


async def _create_event_row(session: AsyncSession, *, actor_id: uuid.UUID, name: str) -> uuid.UUID:
    event_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO dr_events "
            "(id, name, event_type, status, version, created_by_user_id, created_at, updated_at) "
            "VALUES (:id, :name, 'PLANNED_DR', 'PLANNED', 1, :creator, now(), now())"
        ),
        {"id": event_id, "name": name, "creator": actor_id},
    )
    await session.flush()
    return event_id


@pytest.mark.asyncio
async def test_creates_new_work_stream_with_custom_default_and_audit_row(session: AsyncSession) -> None:
    admin_id = await _create_entra_admin(session)
    event_id = await _create_event_row(session, actor_id=admin_id, name="Create Event")

    work_stream = await get_or_create_work_stream(
        session, dr_event_id=event_id, name="Network", actor_id=admin_id
    )

    assert work_stream.name == "Network"
    assert work_stream.stream_type == "CUSTOM"
    assert work_stream.dr_event_id == event_id

    audit_row = await session.execute(
        text(
            "SELECT action, entity_id FROM audit_events "
            "WHERE entity_type = 'WORK_STREAM' AND entity_id = :wid"
        ),
        {"wid": work_stream.id},
    )
    rows = audit_row.all()
    assert len(rows) == 1
    assert rows[0].action == "WORK_STREAM_CREATED"


@pytest.mark.asyncio
async def test_second_call_with_same_name_case_insensitive_returns_existing_row(
    session: AsyncSession,
) -> None:
    admin_id = await _create_entra_admin(session)
    event_id = await _create_event_row(session, actor_id=admin_id, name="Idempotent Event")

    first = await get_or_create_work_stream(session, dr_event_id=event_id, name="Network", actor_id=admin_id)
    second = await get_or_create_work_stream(session, dr_event_id=event_id, name="network", actor_id=admin_id)

    assert second.id == first.id

    count = await session.execute(
        text("SELECT COUNT(*) AS cnt FROM work_streams WHERE dr_event_id = :eid"), {"eid": event_id}
    )
    assert count.one().cnt == 1

    audit_count = await session.execute(
        text(
            "SELECT COUNT(*) AS cnt FROM audit_events WHERE entity_type = 'WORK_STREAM' AND entity_id = :wid"
        ),
        {"wid": first.id},
    )
    assert audit_count.one().cnt == 1


@pytest.mark.asyncio
async def test_same_name_under_different_events_creates_independent_rows(session: AsyncSession) -> None:
    admin_id = await _create_entra_admin(session)
    event_a = await _create_event_row(session, actor_id=admin_id, name="Event A")
    event_b = await _create_event_row(session, actor_id=admin_id, name="Event B")

    work_stream_a = await get_or_create_work_stream(
        session, dr_event_id=event_a, name="Network", actor_id=admin_id
    )
    work_stream_b = await get_or_create_work_stream(
        session, dr_event_id=event_b, name="Network", actor_id=admin_id
    )

    assert work_stream_a.id != work_stream_b.id
    assert work_stream_a.dr_event_id == event_a
    assert work_stream_b.dr_event_id == event_b

"""Command-layer tests for `tasks_dependencies/commands.py` (BUILD-05).

This module is deliberately minimal (BUILD-05.plan.md Risk #2) -- only the two internal helpers
Excel import's `accept` step needs. No routes exist yet, so these tests call the commands
directly against the `session` fixture, no HTTP app involved.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.tasks_dependencies.commands import (
    TaskContextRequiredError,
    create_draft_dependency,
    create_draft_task,
)

pytestmark = pytest.mark.domain


async def _create_entra_admin(session: AsyncSession) -> uuid.UUID:
    user_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO users (id, identity_type, display_name, email, entra_object_id) "
            "VALUES (:id, 'ENTRA', 'Task Admin', :email, :oid)"
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


async def _create_team(session: AsyncSession, *, name: str) -> uuid.UUID:
    team_id = uuid.uuid4()
    await session.execute(
        text("INSERT INTO teams (id, name) VALUES (:id, :name)"), {"id": team_id, "name": name}
    )
    await session.flush()
    return team_id


async def _create_work_stream(session: AsyncSession, *, dr_event_id: uuid.UUID, name: str) -> uuid.UUID:
    work_stream_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO work_streams (id, dr_event_id, name, stream_type) "
            "VALUES (:id, :eid, :name, 'CUSTOM')"
        ),
        {"id": work_stream_id, "eid": dr_event_id, "name": name},
    )
    await session.flush()
    return work_stream_id


@pytest.mark.asyncio
async def test_create_draft_task_relies_on_status_column_default(session: AsyncSession) -> None:
    admin_id = await _create_entra_admin(session)
    event_id = await _create_event_row(session, actor_id=admin_id, name="Task Event")
    team_id = await _create_team(session, name="Network Team")
    work_stream_id = await _create_work_stream(session, dr_event_id=event_id, name="Network")

    task = await create_draft_task(
        session,
        dr_event_id=event_id,
        title="Repoint DNS",
        phase="FAILOVER",
        owning_team_id=team_id,
        created_by_user_id=admin_id,
        work_stream_id=work_stream_id,
        source_import_id=uuid.uuid4(),
        source_import_row="2",
    )

    assert task.status == "NOT_STARTED"
    assert task.title == "Repoint DNS"
    assert task.source_import_row == "2"

    audit_row = await session.execute(
        text("SELECT action FROM audit_events WHERE entity_type = 'TASK' AND entity_id = :tid"),
        {"tid": task.id},
    )
    assert audit_row.scalar_one() == "TASK_CREATED"


@pytest.mark.asyncio
async def test_create_draft_task_requires_application_or_work_stream(session: AsyncSession) -> None:
    admin_id = await _create_entra_admin(session)
    event_id = await _create_event_row(session, actor_id=admin_id, name="No Context Event")
    team_id = await _create_team(session, name="Storage Team")

    with pytest.raises(TaskContextRequiredError):
        await create_draft_task(
            session,
            dr_event_id=event_id,
            title="Orphan Task",
            phase="FAILOVER",
            owning_team_id=team_id,
            created_by_user_id=admin_id,
        )


@pytest.mark.asyncio
async def test_create_draft_dependency_between_two_tasks(session: AsyncSession) -> None:
    admin_id = await _create_entra_admin(session)
    event_id = await _create_event_row(session, actor_id=admin_id, name="Dependency Event")
    team_id = await _create_team(session, name="DB Team")
    work_stream_id = await _create_work_stream(session, dr_event_id=event_id, name="Database")

    predecessor = await create_draft_task(
        session,
        dr_event_id=event_id,
        title="Restore Database",
        phase="FAILOVER",
        owning_team_id=team_id,
        created_by_user_id=admin_id,
        work_stream_id=work_stream_id,
    )
    successor = await create_draft_task(
        session,
        dr_event_id=event_id,
        title="Verify Database",
        phase="FAILOVER",
        owning_team_id=team_id,
        created_by_user_id=admin_id,
        work_stream_id=work_stream_id,
    )

    dependency = await create_draft_dependency(
        session,
        dr_event_id=event_id,
        predecessor_task_id=predecessor.id,
        successor_task_id=successor.id,
        created_by_user_id=admin_id,
    )

    assert dependency is not None
    assert dependency.strength == "HARD"

    audit_row = await session.execute(
        text("SELECT action FROM audit_events WHERE entity_type = 'TASK_DEPENDENCY' AND entity_id = :did"),
        {"did": dependency.id},
    )
    assert audit_row.scalar_one() == "TASK_DEPENDENCY_CREATED"


@pytest.mark.asyncio
async def test_create_draft_dependency_self_edge_is_a_noop_not_an_error(session: AsyncSession) -> None:
    admin_id = await _create_entra_admin(session)
    event_id = await _create_event_row(session, actor_id=admin_id, name="Self Edge Event")
    team_id = await _create_team(session, name="App Team")
    work_stream_id = await _create_work_stream(session, dr_event_id=event_id, name="Applications")

    task = await create_draft_task(
        session,
        dr_event_id=event_id,
        title="Solo Task",
        phase="FAILOVER",
        owning_team_id=team_id,
        created_by_user_id=admin_id,
        work_stream_id=work_stream_id,
    )

    result = await create_draft_dependency(
        session,
        dr_event_id=event_id,
        predecessor_task_id=task.id,
        successor_task_id=task.id,
        created_by_user_id=admin_id,
    )

    assert result is None

    count = await session.execute(
        text("SELECT COUNT(*) AS cnt FROM task_dependencies WHERE predecessor_task_id = :tid"),
        {"tid": task.id},
    )
    assert count.one().cnt == 0


@pytest.mark.asyncio
async def test_create_draft_dependency_exact_duplicate_is_a_noop(session: AsyncSession) -> None:
    admin_id = await _create_entra_admin(session)
    event_id = await _create_event_row(session, actor_id=admin_id, name="Duplicate Edge Event")
    team_id = await _create_team(session, name="Net Team")
    work_stream_id = await _create_work_stream(session, dr_event_id=event_id, name="Network2")

    predecessor = await create_draft_task(
        session,
        dr_event_id=event_id,
        title="Step 1",
        phase="FAILOVER",
        owning_team_id=team_id,
        created_by_user_id=admin_id,
        work_stream_id=work_stream_id,
    )
    successor = await create_draft_task(
        session,
        dr_event_id=event_id,
        title="Step 2",
        phase="FAILOVER",
        owning_team_id=team_id,
        created_by_user_id=admin_id,
        work_stream_id=work_stream_id,
    )

    first = await create_draft_dependency(
        session,
        dr_event_id=event_id,
        predecessor_task_id=predecessor.id,
        successor_task_id=successor.id,
        created_by_user_id=admin_id,
    )
    second = await create_draft_dependency(
        session,
        dr_event_id=event_id,
        predecessor_task_id=predecessor.id,
        successor_task_id=successor.id,
        created_by_user_id=admin_id,
    )

    assert first is not None
    assert second is None

    count = await session.execute(
        text(
            "SELECT COUNT(*) AS cnt FROM task_dependencies "
            "WHERE predecessor_task_id = :pid AND successor_task_id = :sid"
        ),
        {"pid": predecessor.id, "sid": successor.id},
    )
    assert count.one().cnt == 1

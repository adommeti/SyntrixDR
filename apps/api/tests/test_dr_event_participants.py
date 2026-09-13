from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import FakeClock
from app.dr_events.participants import (
    enrol_participant,
    is_participant,
    remove_participant,
    user_can_see_event,
    visible_event_ids_for_user,
)
from app.users_teams_org.commands import grant_role
from app.users_teams_org.models import RoleAssignment

pytestmark = [pytest.mark.api, pytest.mark.integration]


async def _create_user(session: AsyncSession) -> uuid.UUID:
    user_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO users (id, identity_type, display_name, email) "
            "VALUES (:id, 'LOCAL', 'Test User', :email)"
        ),
        {"id": user_id, "email": f"{user_id}@example.test"},
    )
    await session.flush()
    return user_id


async def _create_dr_event(session: AsyncSession) -> uuid.UUID:
    """dr_events core entity is BUILD-04 — insert a minimal row via the frozen table directly,
    since only the FK-resolution stub (id column) exists on the ORM side this session."""
    event_id = uuid.uuid4()
    creator_id = await _create_user(session)
    await session.execute(
        text(
            "INSERT INTO dr_events (id, name, event_type, status, created_by_user_id) "
            "VALUES (:id, 'Test Event', 'PLANNED_DR', 'PLANNED', :creator_id)"
        ),
        {"id": event_id, "creator_id": creator_id},
    )
    await session.flush()
    return event_id


@pytest.mark.asyncio
async def test_enrol_participant_is_idempotent(session: AsyncSession) -> None:
    user_id = await _create_user(session)
    event_id = await _create_dr_event(session)

    await enrol_participant(session, event_id, user_id, "ROLE")
    await enrol_participant(session, event_id, user_id, "ROLE")  # second call: no-op, no IntegrityError

    result = await session.execute(
        text(
            "SELECT COUNT(*) AS cnt FROM dr_event_participants "
            "WHERE dr_event_id = :eid AND user_id = :uid AND source = 'ROLE' AND removed_at IS NULL"
        ),
        {"eid": event_id, "uid": user_id},
    )
    assert result.one().cnt == 1


@pytest.mark.asyncio
async def test_remove_then_reenrol_creates_a_new_active_row(session: AsyncSession, clock: FakeClock) -> None:
    user_id = await _create_user(session)
    event_id = await _create_dr_event(session)

    await enrol_participant(session, event_id, user_id, "ROLE")
    await remove_participant(session, event_id, user_id, "ROLE", clock=clock)
    assert await is_participant(session, event_id, user_id) is False

    await enrol_participant(session, event_id, user_id, "ROLE")
    assert await is_participant(session, event_id, user_id) is True


@pytest.mark.asyncio
async def test_is_participant_false_for_global_admin_with_zero_rows(session: AsyncSession) -> None:
    """`is_participant` only checks explicit rows — Global Admin's implicit-everywhere rule is the
    caller's job (`user_can_see_event`), documented in the function's own docstring."""
    admin_id = await _create_user(session)
    event_id = await _create_dr_event(session)
    assert await is_participant(session, event_id, admin_id) is False


@pytest.mark.asyncio
async def test_user_can_see_event_true_for_global_admin_with_zero_rows(session: AsyncSession) -> None:
    admin_id = await _create_user(session)
    event_id = await _create_dr_event(session)
    session.add(RoleAssignment(user_id=admin_id, role_key="GLOBAL_ADMIN", scope_type="GLOBAL"))
    await session.flush()

    assert await user_can_see_event(session, admin_id, event_id) is True


@pytest.mark.asyncio
async def test_user_can_see_event_false_for_non_participant_non_admin(session: AsyncSession) -> None:
    user_id = await _create_user(session)
    event_id = await _create_dr_event(session)
    assert await user_can_see_event(session, user_id, event_id) is False


@pytest.mark.asyncio
async def test_grant_role_at_dr_event_scope_auto_enrols_role_source(session: AsyncSession) -> None:
    """The one D-222 source wired end-to-end this session: granting a DR_EVENT-scoped role creates
    both the `role_assignments` row and a `dr_event_participants` row with `source='ROLE'`."""
    granter_id = await _create_user(session)
    grantee_id = await _create_user(session)
    event_id = await _create_dr_event(session)

    await grant_role(
        session,
        actor_id=granter_id,
        user_id=grantee_id,
        role_key="WORK_STREAM_LEAD",
        scope_type="DR_EVENT",
        scope_id=event_id,
    )

    assert await is_participant(session, event_id, grantee_id) is True


@pytest.mark.asyncio
async def test_grant_role_at_global_scope_does_not_enrol_participant(session: AsyncSession) -> None:
    """Only DR_EVENT-scoped role grants trigger the ROLE auto-enrol source."""
    granter_id = await _create_user(session)
    grantee_id = await _create_user(session)
    event_id = await _create_dr_event(session)

    await grant_role(session, actor_id=granter_id, user_id=grantee_id, role_key="GLOBAL_ADMIN")

    assert await is_participant(session, event_id, grantee_id) is False


@pytest.mark.asyncio
async def test_visible_event_ids_for_user_returns_only_active_rows(session: AsyncSession) -> None:
    user_id = await _create_user(session)
    visible_event = await _create_dr_event(session)
    removed_event = await _create_dr_event(session)

    await enrol_participant(session, visible_event, user_id, "ROLE")
    await enrol_participant(session, removed_event, user_id, "ROLE")
    await remove_participant(session, removed_event, user_id, "ROLE")

    result = await session.execute(visible_event_ids_for_user(user_id))
    visible_ids = {row[0] for row in result.all()}
    assert visible_ids == {visible_event}

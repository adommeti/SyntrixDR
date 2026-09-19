"""Pure query-layer tests for `users_teams_org/queries.py` lookups added for BUILD-05's Excel
import row resolution (Owning Team, Assignee)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.users_teams_org.queries import find_active_user_by_display_name, get_team_by_name

pytestmark = [pytest.mark.domain]


async def _create_team(session: AsyncSession, *, name: str) -> uuid.UUID:
    team_id = uuid.uuid4()
    await session.execute(
        text("INSERT INTO teams (id, name) VALUES (:id, :name)"), {"id": team_id, "name": name}
    )
    await session.flush()
    return team_id


async def _create_user(session: AsyncSession, *, display_name: str, is_active: bool = True) -> uuid.UUID:
    user_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO users (id, identity_type, display_name, email, is_active) "
            "VALUES (:id, 'LOCAL', :name, :email, :active)"
        ),
        {"id": user_id, "name": display_name, "email": f"{user_id}@example.test", "active": is_active},
    )
    await session.flush()
    return user_id


@pytest.mark.asyncio
async def test_get_team_by_name_matches_case_insensitively(session: AsyncSession) -> None:
    team_id = await _create_team(session, name="Network Operations")

    assert (await get_team_by_name(session, "Network Operations")).id == team_id  # type: ignore[union-attr]
    assert (await get_team_by_name(session, "network operations")).id == team_id  # type: ignore[union-attr]
    assert (await get_team_by_name(session, "NETWORK OPERATIONS")).id == team_id  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_get_team_by_name_returns_none_when_not_found(session: AsyncSession) -> None:
    await _create_team(session, name="Network Operations")

    assert await get_team_by_name(session, "Storage Team") is None


@pytest.mark.asyncio
async def test_find_active_user_by_display_name_matches_case_insensitively(
    session: AsyncSession,
) -> None:
    user_id = await _create_user(session, display_name="Priya Sharma")

    assert await find_active_user_by_display_name(session, "Priya Sharma") == user_id
    assert await find_active_user_by_display_name(session, "priya sharma") == user_id


@pytest.mark.asyncio
async def test_find_active_user_by_display_name_returns_none_when_not_found(
    session: AsyncSession,
) -> None:
    await _create_user(session, display_name="Priya Sharma")

    assert await find_active_user_by_display_name(session, "Nobody Here") is None


@pytest.mark.asyncio
async def test_find_active_user_by_display_name_returns_none_when_ambiguous(
    session: AsyncSession,
) -> None:
    """Two active users sharing a display name must never silently resolve to one of them --
    an import that misassigns a Task to the wrong person is worse than leaving it unassigned."""
    await _create_user(session, display_name="Alex Kim")
    await _create_user(session, display_name="Alex Kim")

    assert await find_active_user_by_display_name(session, "Alex Kim") is None


@pytest.mark.asyncio
async def test_find_active_user_by_display_name_excludes_inactive_users(
    session: AsyncSession,
) -> None:
    await _create_user(session, display_name="Deactivated Dana", is_active=False)

    assert await find_active_user_by_display_name(session, "Deactivated Dana") is None

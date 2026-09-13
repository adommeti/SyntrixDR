"""Tests for Entra callback (find-or-create user and session issuance).

This module tests `app.identity_auth.commands.entra_callback` directly—the
database/session-issuing logic—NOT the real OIDC redirect/token-exchange flow
(which lives in `app.identity_auth.oidc.py` and would require mocking authlib).
HTTP-level OIDC callback tests with mocked authlib are out of scope; that belongs
in the web layer integration tests (BUILD-04+).
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock, FakeClock
from app.identity_auth.commands import LoginResult, entra_callback
from app.identity_auth.session_store import SessionData

pytestmark = [pytest.mark.api, pytest.mark.auth, pytest.mark.integration]


class MockRedisSessionStore:
    """In-memory mock of RedisSessionStore for testing."""

    def __init__(self) -> None:
        self._store: dict[str, dict[str, str]] = {}

    async def create(
        self, session_id: uuid.UUID, user_id: uuid.UUID, identity_type: str, clock: Clock
    ) -> SessionData:
        """Create a session in mock store and return SessionData."""
        absolute_expires_at = clock.now() + timedelta(hours=8)
        idle_expires_at = clock.now() + timedelta(minutes=30)
        csrf_token = f"csrf_{uuid.uuid4()}"

        self._store[str(session_id)] = {
            "user_id": str(user_id),
            "identity_type": identity_type,
            "csrf_token": csrf_token,
            "absolute_expires_at": absolute_expires_at.isoformat(),
            "idle_expires_at": idle_expires_at.isoformat(),
        }

        return SessionData(
            session_id=str(session_id),
            user_id=user_id,
            identity_type=identity_type,
            csrf_token=csrf_token,
            absolute_expires_at=absolute_expires_at,
            idle_expires_at=idle_expires_at,
        )

    async def read(self, session_id: str) -> SessionData | None:
        """Read a session from mock store."""
        data = self._store.get(session_id)
        if not data:
            return None
        from datetime import datetime

        return SessionData(
            session_id=session_id,
            user_id=uuid.UUID(data["user_id"]),
            identity_type=data["identity_type"],
            csrf_token=data["csrf_token"],
            absolute_expires_at=datetime.fromisoformat(data["absolute_expires_at"]),
            idle_expires_at=datetime.fromisoformat(data["idle_expires_at"]),
        )

    async def touch(self, session_id: str, clock: Clock) -> SessionData | None:
        """Touch a session (update idle_expires_at)."""
        data = self._store.get(session_id)
        if not data:
            return None
        from datetime import datetime

        absolute_expires_at = datetime.fromisoformat(data["absolute_expires_at"])
        idle_expires_at = clock.now() + timedelta(minutes=30)
        if idle_expires_at > absolute_expires_at:
            idle_expires_at = absolute_expires_at

        data["idle_expires_at"] = idle_expires_at.isoformat()

        return SessionData(
            session_id=session_id,
            user_id=uuid.UUID(data["user_id"]),
            identity_type=data["identity_type"],
            csrf_token=data["csrf_token"],
            absolute_expires_at=absolute_expires_at,
            idle_expires_at=idle_expires_at,
        )

    async def revoke(self, session_id: str) -> None:
        """Revoke a session."""
        self._store.pop(session_id, None)


@pytest.fixture
def mock_store() -> MockRedisSessionStore:
    """Provide an in-memory mock session store."""
    return MockRedisSessionStore()


@pytest.mark.asyncio
async def test_entra_callback_creates_new_user_and_session(
    session: AsyncSession, clock: FakeClock, mock_store: MockRedisSessionStore
) -> None:
    """Calling entra_callback with a new entra_object_id creates a users row
    with identity_type='ENTRA' and issues a session (LoginResult)."""
    entra_object_id = str(uuid.uuid4())
    email = "newuser@example.com"
    display_name = "New User"

    result = await entra_callback(
        session,
        mock_store,
        clock,
        entra_object_id=entra_object_id,
        email=email,
        display_name=display_name,
    )

    await session.flush()

    # Verify LoginResult structure
    assert isinstance(result, LoginResult)
    assert isinstance(result.session_id, str)
    assert isinstance(result.csrf_token, str)
    assert isinstance(result.user_id, uuid.UUID)

    # Verify user was created
    user_row = (
        await session.execute(
            text(
                "SELECT id, identity_type, display_name, email, is_active " "FROM users WHERE id = :user_id"
            ),
            {"user_id": result.user_id},
        )
    ).one_or_none()

    assert user_row is not None
    assert user_row.identity_type == "ENTRA"
    assert user_row.display_name == display_name
    assert user_row.email == email
    assert user_row.is_active is True

    # Verify session was created in database
    session_row = (
        await session.execute(
            text("SELECT id, user_id, identity_type, revoked_at " "FROM sessions WHERE id = :session_id"),
            {"session_id": uuid.UUID(result.session_id)},
        )
    ).one_or_none()

    assert session_row is not None
    assert str(session_row.user_id) == str(result.user_id)
    assert session_row.identity_type == "ENTRA"
    assert session_row.revoked_at is None

    # Verify session was stored in mock store
    store_data = await mock_store.read(result.session_id)
    assert store_data is not None
    assert store_data.user_id == result.user_id
    assert store_data.identity_type == "ENTRA"
    assert store_data.csrf_token == result.csrf_token


@pytest.mark.asyncio
async def test_entra_callback_reuses_existing_user_and_creates_new_session(
    session: AsyncSession, clock: FakeClock, mock_store: MockRedisSessionStore
) -> None:
    """Calling entra_callback with the SAME entra_object_id reuses the existing
    user (no duplicate row) and issues a new session."""
    entra_object_id = str(uuid.uuid4())
    email = "existing@example.com"
    display_name = "Existing User"

    # First call: create user
    first_result = await entra_callback(
        session,
        mock_store,
        clock,
        entra_object_id=entra_object_id,
        email=email,
        display_name=display_name,
    )
    await session.flush()

    first_user_id = first_result.user_id
    first_session_id = first_result.session_id

    # Second call: reuse user, create new session
    second_result = await entra_callback(
        session,
        mock_store,
        clock,
        entra_object_id=entra_object_id,
        email=email,
        display_name=display_name,
    )
    await session.flush()

    second_user_id = second_result.user_id
    second_session_id = second_result.session_id

    # Verify user_id is the same (reused)
    assert first_user_id == second_user_id

    # Verify session_id is different (new session)
    assert first_session_id != second_session_id

    # Verify only one users row exists for this entra_object_id
    user_count = (
        await session.execute(
            text("SELECT COUNT(*) as cnt FROM users WHERE id = :user_id"),
            {"user_id": first_user_id},
        )
    ).scalar()
    assert user_count == 1

    # Verify two separate session rows exist
    session_count = (
        await session.execute(
            text("SELECT COUNT(*) as cnt FROM sessions WHERE user_id = :user_id " "AND revoked_at IS NULL"),
            {"user_id": first_user_id},
        )
    ).scalar()
    assert session_count == 2

    # Verify both sessions are valid in the store
    first_store = await mock_store.read(first_session_id)
    second_store = await mock_store.read(second_session_id)
    assert first_store is not None
    assert second_store is not None
    assert first_store.user_id == second_store.user_id


@pytest.mark.asyncio
async def test_entra_callback_writes_audit_event(
    session: AsyncSession, clock: FakeClock, mock_store: MockRedisSessionStore
) -> None:
    """Calling entra_callback writes an AUTH_LOGIN_ENTRA audit event."""
    entra_object_id = str(uuid.uuid4())
    email = "audit@example.com"
    display_name = "Audit User"

    result = await entra_callback(
        session,
        mock_store,
        clock,
        entra_object_id=entra_object_id,
        email=email,
        display_name=display_name,
    )
    await session.flush()

    # Verify audit event was created
    audit_row = (
        await session.execute(
            text(
                "SELECT action, entity_type, entity_id, actor_user_id, dr_event_id "
                "FROM audit_events WHERE action = :action AND entity_id = :entity_id"
            ),
            {"action": "AUTH_LOGIN_ENTRA", "entity_id": result.user_id},
        )
    ).one_or_none()

    assert audit_row is not None
    assert audit_row.entity_type == "USER"
    assert audit_row.actor_user_id == result.user_id
    assert audit_row.dr_event_id is None  # Auth events don't have a DR event

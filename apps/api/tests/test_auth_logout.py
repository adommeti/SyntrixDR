from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import AuditEvent
from app.core.clock import FakeClock
from app.identity_auth.commands import logout
from app.identity_auth.models import LocalCredential, SessionRecord
from app.identity_auth.security import PasswordHasher
from app.identity_auth.session_store import RedisSessionStore, SessionData

pytestmark = [pytest.mark.api, pytest.mark.auth, pytest.mark.integration]


@pytest_asyncio.fixture
async def local_user(session: AsyncSession, clock: FakeClock) -> uuid.UUID:
    user_id = uuid.uuid4()
    hasher = PasswordHasher()
    await session.execute(
        text(
            "INSERT INTO users (id, identity_type, display_name, email) "
            "VALUES (:id, 'LOCAL', 'Test User', :email)"
        ),
        {"id": user_id, "email": f"{user_id}@example.test"},
    )
    session.add(
        LocalCredential(
            user_id=user_id,
            password_hash=hasher.hash_password("MyTestPassword123!"),
            algorithm="argon2id",
            failed_attempts=0,
            totp_enabled=False,
            must_reset=False,
            created_at=clock.now(),
            updated_at=clock.now(),
        )
    )
    await session.flush()
    return user_id


@pytest_asyncio.fixture
async def active_session_record(
    session: AsyncSession, local_user: uuid.UUID, clock: FakeClock
) -> SessionRecord:
    now = clock.now()
    record = SessionRecord(
        id=uuid.uuid4(),
        user_id=local_user,
        identity_type="LOCAL",
        created_at=now,
        last_seen_at=now,
        absolute_expires_at=now + timedelta(hours=8),
        idle_expires_at=now + timedelta(minutes=30),
        revoked_at=None,
        revoked_reason=None,
    )
    session.add(record)
    await session.flush()
    return record


@pytest.fixture
def session_data(active_session_record: SessionRecord) -> SessionData:
    return SessionData(
        session_id=str(active_session_record.id),
        user_id=active_session_record.user_id,
        identity_type="LOCAL",
        csrf_token="dummy-csrf-token-for-testing",
        absolute_expires_at=active_session_record.absolute_expires_at,
        idle_expires_at=active_session_record.idle_expires_at,
    )


@pytest.mark.asyncio
async def test_logout_revokes_redis_session(
    session: AsyncSession, redis_client: Redis, clock: FakeClock, session_data: SessionData
) -> None:
    """logout() removes the session from Redis so it can no longer be read back."""
    store = RedisSessionStore(redis_client, idle_minutes=30, absolute_hours=8)
    await store.create(uuid.UUID(session_data.session_id), session_data.user_id, "LOCAL", clock)

    await logout(session, store, clock, session_data=session_data)

    assert await store.read(session_data.session_id) is None


@pytest.mark.asyncio
async def test_logout_marks_session_record_revoked_with_injected_clock(
    session: AsyncSession,
    redis_client: Redis,
    clock: FakeClock,
    session_data: SessionData,
    active_session_record: SessionRecord,
) -> None:
    """logout() stamps revoked_at from the injected Clock, not wall-clock time."""
    store = RedisSessionStore(redis_client, idle_minutes=30, absolute_hours=8)
    clock.advance(minutes=17)
    expected_now = clock.now()

    await logout(session, store, clock, session_data=session_data)

    refreshed = await session.get(SessionRecord, active_session_record.id)
    assert refreshed is not None
    assert refreshed.revoked_at == expected_now
    assert refreshed.revoked_reason == "LOGOUT"


@pytest.mark.asyncio
async def test_logout_writes_audit_event(
    session: AsyncSession,
    redis_client: Redis,
    clock: FakeClock,
    session_data: SessionData,
    active_session_record: SessionRecord,
) -> None:
    """logout() writes a single AUTH_LOGOUT audit row for the session."""
    store = RedisSessionStore(redis_client, idle_minutes=30, absolute_hours=8)

    await logout(session, store, clock, session_data=session_data)

    audit = (
        (
            await session.execute(
                select(AuditEvent).where(
                    (AuditEvent.entity_type == "SESSION")
                    & (AuditEvent.entity_id == active_session_record.id)
                    & (AuditEvent.action == "AUTH_LOGOUT")
                )
            )
        )
        .scalars()
        .all()
    )

    assert len(audit) == 1
    assert audit[0].actor_user_id == session_data.user_id

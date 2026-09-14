from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import FakeClock
from app.core.errors import AppError, app_error_handler
from app.identity_auth.dependencies import get_current_session
from app.identity_auth.session_store import RedisSessionStore, SessionData

pytestmark = [pytest.mark.api, pytest.mark.auth, pytest.mark.integration]


class TestRedisSessionStoreCreateAndRead:
    """RedisSessionStore.create() → read() round-trips all SessionData fields."""

    @pytest.mark.asyncio
    async def test_create_and_read_round_trip(
        self, redis_client: Redis, clock: FakeClock, seed_user: uuid.UUID
    ) -> None:
        """All SessionData fields persist through Redis round-trip."""
        store = RedisSessionStore(redis_client, idle_minutes=30, absolute_hours=8)
        session_id = uuid.uuid4()
        identity_type = "LOCAL"

        # Create session
        created_data = await store.create(session_id, seed_user, identity_type, clock)

        assert created_data.session_id == str(session_id)
        assert created_data.user_id == seed_user
        assert created_data.identity_type == identity_type
        assert isinstance(created_data.csrf_token, str)
        assert len(created_data.csrf_token) > 0
        assert created_data.absolute_expires_at == clock.now() + timedelta(hours=8)
        assert created_data.idle_expires_at == clock.now() + timedelta(minutes=30)

        # Read back the session
        read_data = await store.read(str(session_id))

        assert read_data is not None
        assert read_data.session_id == created_data.session_id
        assert read_data.user_id == created_data.user_id
        assert read_data.identity_type == created_data.identity_type
        assert read_data.csrf_token == created_data.csrf_token
        assert read_data.absolute_expires_at == created_data.absolute_expires_at
        assert read_data.idle_expires_at == created_data.idle_expires_at

        # Cleanup
        await redis_client.delete(f"drcc:session:{session_id}")

    @pytest.mark.asyncio
    async def test_read_nonexistent_session_returns_none(self, redis_client: Redis) -> None:
        """Reading a non-existent session returns None."""
        store = RedisSessionStore(redis_client, idle_minutes=30, absolute_hours=8)
        result = await store.read(str(uuid.uuid4()))
        assert result is None


class TestRedisSessionStoreTouch:
    """touch() slides idle_expires_at forward but never exceeds absolute_expires_at."""

    @pytest.mark.asyncio
    async def test_touch_slides_idle_expiry_forward(
        self, redis_client: Redis, clock: FakeClock, seed_user: uuid.UUID
    ) -> None:
        """touch() extends idle_expires_at by idle_minutes."""
        store = RedisSessionStore(redis_client, idle_minutes=30, absolute_hours=8)
        session_id = uuid.uuid4()

        # Create session
        created = await store.create(session_id, seed_user, "LOCAL", clock)
        initial_idle = created.idle_expires_at

        # Advance clock by 15 minutes and touch
        clock.advance(minutes=15)
        touched = await store.touch(str(session_id), clock)

        assert touched is not None
        assert touched.idle_expires_at > initial_idle
        # New idle expiry should be 30 minutes from now (15 min later than original)
        assert touched.idle_expires_at == clock.now() + timedelta(minutes=30)

        # Cleanup
        await redis_client.delete(f"drcc:session:{session_id}")

    @pytest.mark.asyncio
    async def test_touch_never_exceeds_absolute_expiry(
        self, redis_client: Redis, clock: FakeClock, seed_user: uuid.UUID
    ) -> None:
        """touch() caps idle_expires_at at absolute_expires_at."""
        store = RedisSessionStore(redis_client, idle_minutes=30, absolute_hours=8)
        session_id = uuid.uuid4()

        # Create session
        created = await store.create(session_id, seed_user, "LOCAL", clock)
        absolute_expiry = created.absolute_expires_at

        # Touch every 20 minutes (well inside the 30-min idle window) up to 7h55m, so the
        # idle window never itself lapses on the way there — only the LAST touch (with 5
        # minutes left before the 8h absolute cutoff) should be capped at absolute_expires_at.
        total_advanced = timedelta()
        step = timedelta(minutes=20)
        target = timedelta(hours=7, minutes=55)
        touched = created
        while total_advanced + step < target:
            clock.advance(minutes=20)
            total_advanced += step
            touched = await store.touch(str(session_id), clock)
            assert touched is not None
        clock.advance(seconds=(target - total_advanced).total_seconds())
        touched = await store.touch(str(session_id), clock)

        assert touched is not None
        # Even though we'd normally set idle to now + 30min, it should cap at absolute
        assert touched.idle_expires_at <= absolute_expiry
        assert touched.idle_expires_at == absolute_expiry

        # Cleanup
        await redis_client.delete(f"drcc:session:{session_id}")

    @pytest.mark.asyncio
    async def test_touch_returns_none_when_idle_expired(
        self, redis_client: Redis, clock: FakeClock, seed_user: uuid.UUID
    ) -> None:
        """touch() returns None and deletes key if idle timeout has passed."""
        store = RedisSessionStore(redis_client, idle_minutes=30, absolute_hours=8)
        session_id = uuid.uuid4()

        # Create session
        await store.create(session_id, seed_user, "LOCAL", clock)

        # Advance clock past idle timeout (31 minutes)
        clock.advance(minutes=31)
        result = await store.touch(str(session_id), clock)

        assert result is None
        # Key should be deleted
        assert await redis_client.exists(f"drcc:session:{session_id}") == 0

    @pytest.mark.asyncio
    async def test_touch_returns_none_when_absolute_expired(
        self, redis_client: Redis, clock: FakeClock, seed_user: uuid.UUID
    ) -> None:
        """touch() returns None and deletes key if absolute timeout has passed."""
        store = RedisSessionStore(redis_client, idle_minutes=30, absolute_hours=8)
        session_id = uuid.uuid4()

        # Create session
        await store.create(session_id, seed_user, "LOCAL", clock)

        # Advance clock past absolute timeout (8 hours 1 minute)
        clock.advance(hours=8, minutes=1)
        result = await store.touch(str(session_id), clock)

        assert result is None
        # Key should be deleted
        assert await redis_client.exists(f"drcc:session:{session_id}") == 0


class TestRedisSessionStoreRevoke:
    """revoke() makes subsequent read() return None."""

    @pytest.mark.asyncio
    async def test_revoke_deletes_session(
        self, redis_client: Redis, clock: FakeClock, seed_user: uuid.UUID
    ) -> None:
        """revoke() removes the session from Redis."""
        store = RedisSessionStore(redis_client, idle_minutes=30, absolute_hours=8)
        session_id = uuid.uuid4()

        # Create and verify session exists
        await store.create(session_id, seed_user, "LOCAL", clock)
        assert await store.read(str(session_id)) is not None

        # Revoke
        await store.revoke(str(session_id))

        # Verify it's gone
        assert await store.read(str(session_id)) is None

    @pytest.mark.asyncio
    async def test_revoke_idempotent(
        self, redis_client: Redis, clock: FakeClock, seed_user: uuid.UUID
    ) -> None:
        """revoke() on a non-existent session does not error."""
        store = RedisSessionStore(redis_client, idle_minutes=30, absolute_hours=8)
        session_id = uuid.uuid4()

        # Revoke a session that doesn't exist (should not raise)
        await store.revoke(str(session_id))


class TestGetCurrentSessionDependency:
    """get_current_session dependency wiring and error cases."""

    def _build_app(
        self, redis_client: Redis, session: AsyncSession, clock: FakeClock
    ) -> tuple[FastAPI, RedisSessionStore]:
        """Build a test FastAPI app with session dependency wired."""
        app = FastAPI()
        app.add_exception_handler(AppError, app_error_handler)
        store = RedisSessionStore(redis_client, idle_minutes=30, absolute_hours=8)

        async def _get_store() -> RedisSessionStore:
            return store

        async def _get_clock() -> FakeClock:
            return clock

        async def _get_db_session() -> AsyncSession:
            return session

        async def _get_session_dependency(
            request: Request,
            store: RedisSessionStore = Depends(_get_store),  # noqa: B008
            clock: FakeClock = Depends(_get_clock),  # noqa: B008
            db_session: AsyncSession = Depends(_get_db_session),  # noqa: B008
        ) -> SessionData:
            return await get_current_session(request, store, clock, db_session)

        @app.get("/api/v1/_test-session")
        async def session_route(session_data: SessionData = Depends(_get_session_dependency)) -> JSONResponse:  # noqa: ANN001, B008
            return JSONResponse(
                status_code=200,
                content={
                    "session_id": session_data.session_id,
                    "user_id": str(session_data.user_id),
                    "identity_type": session_data.identity_type,
                },
            )

        return app, store

    async def _client(self, app: FastAPI) -> AsyncClient:
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    @pytest.mark.asyncio
    async def test_missing_session_cookie_raises_401(
        self, redis_client: Redis, session: AsyncSession, clock: FakeClock
    ) -> None:
        """get_current_session raises 401 when drcc_session cookie is missing."""
        app, _ = self._build_app(redis_client, session, clock)
        async with await self._client(app) as client:
            response = await client.get("/api/v1/_test-session")

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "SESSION_EXPIRED"

    @pytest.mark.asyncio
    async def test_invalid_session_cookie_raises_401(
        self, redis_client: Redis, session: AsyncSession, clock: FakeClock
    ) -> None:
        """get_current_session raises 401 when session doesn't exist in Redis."""
        app, _ = self._build_app(redis_client, session, clock)
        cookies = {"drcc_session": str(uuid.uuid4())}

        async with await self._client(app) as client:
            response = await client.get("/api/v1/_test-session", cookies=cookies)

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "SESSION_EXPIRED"

    @pytest.mark.asyncio
    async def test_valid_session_returns_200(
        self, redis_client: Redis, session: AsyncSession, clock: FakeClock, seed_user: uuid.UUID
    ) -> None:
        """get_current_session returns session data for a valid, non-expired session."""
        app, store = self._build_app(redis_client, session, clock)
        session_id = uuid.uuid4()

        # Create a session
        await store.create(session_id, seed_user, "LOCAL", clock)
        cookies = {"drcc_session": str(session_id)}

        async with await self._client(app) as client:
            response = await client.get("/api/v1/_test-session", cookies=cookies)

        assert response.status_code == 200
        body = response.json()
        assert body["session_id"] == str(session_id)
        assert body["user_id"] == str(seed_user)
        assert body["identity_type"] == "LOCAL"

        # Cleanup
        await redis_client.delete(f"drcc:session:{session_id}")

    @pytest.mark.asyncio
    async def test_idle_timeout_raises_401(
        self, redis_client: Redis, session: AsyncSession, clock: FakeClock, seed_user: uuid.UUID
    ) -> None:
        """get_current_session raises 401 when idle timeout (31 min) is exceeded.

        Even though absolute timeout (8h) hasn't been reached, the session should
        expire. Advancing by 31 minutes should trigger idle expiry.
        """
        app, store = self._build_app(redis_client, session, clock)
        session_id = uuid.uuid4()

        # Create a session
        await store.create(session_id, seed_user, "LOCAL", clock)
        cookies = {"drcc_session": str(session_id)}

        # First request: should succeed
        async with await self._client(app) as client:
            response = await client.get("/api/v1/_test-session", cookies=cookies)
        assert response.status_code == 200

        # Advance clock past idle timeout (31 minutes)
        clock.advance(minutes=31)

        # Second request: should fail with 401
        async with await self._client(app) as client:
            response = await client.get("/api/v1/_test-session", cookies=cookies)

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "SESSION_EXPIRED"

        # Cleanup
        await redis_client.delete(f"drcc:session:{session_id}")

    @pytest.mark.asyncio
    async def test_absolute_timeout_raises_401(
        self, redis_client: Redis, session: AsyncSession, clock: FakeClock, seed_user: uuid.UUID
    ) -> None:
        """get_current_session raises 401 when absolute timeout (8h1m) is exceeded.

        Even with periodic touch() calls to refresh idle timeout, the session
        should expire when absolute timeout is reached. This test simulates
        continuous activity (touch every few minutes) but still hits the 8h limit.
        """
        app, store = self._build_app(redis_client, session, clock)
        session_id = uuid.uuid4()

        # Create a session
        await store.create(session_id, seed_user, "LOCAL", clock)
        cookies = {"drcc_session": str(session_id)}

        # Initial request: succeed
        async with await self._client(app) as client:
            response = await client.get("/api/v1/_test-session", cookies=cookies)
        assert response.status_code == 200

        # Simulate continuous activity: advance 3 minutes at a time, touching the session.
        # Do this 160 times to get past 8 hours (480 minutes).
        for _ in range(160):
            clock.advance(minutes=3)
            touched = await store.touch(str(session_id), clock)
            # At some point touch will return None when absolute expires
            if touched is None:
                break

        # Now the session should be expired (absolute timeout reached)
        async with await self._client(app) as client:
            response = await client.get("/api/v1/_test-session", cookies=cookies)

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "SESSION_EXPIRED"

        # Cleanup
        await redis_client.delete(f"drcc:session:{session_id}")

    @pytest.mark.asyncio
    async def test_touch_on_valid_request_extends_session(
        self, redis_client: Redis, session: AsyncSession, clock: FakeClock, seed_user: uuid.UUID
    ) -> None:
        """Each request to get_current_session touches (refreshes) the session.

        Advance 25 minutes (less than idle timeout), make a request, then advance
        another 20 minutes (total 45 > 30 idle timeout), but because the first
        request touched the session, it should still be valid.
        """
        app, store = self._build_app(redis_client, session, clock)
        session_id = uuid.uuid4()

        # Create session
        await store.create(session_id, seed_user, "LOCAL", clock)
        cookies = {"drcc_session": str(session_id)}

        # First request at t=0, advances idle_expires_at to t=30min
        async with await self._client(app) as client:
            response = await client.get("/api/v1/_test-session", cookies=cookies)
        assert response.status_code == 200

        # Advance 25 minutes (t=25min, still within new idle window of t=30min)
        clock.advance(minutes=25)

        # Second request at t=25min, touches again, advancing idle_expires_at to t=55min
        async with await self._client(app) as client:
            response = await client.get("/api/v1/_test-session", cookies=cookies)
        assert response.status_code == 200

        # Advance another 20 minutes (t=45min, still within t=55min idle window)
        clock.advance(minutes=20)

        # Third request at t=45min should still succeed
        async with await self._client(app) as client:
            response = await client.get("/api/v1/_test-session", cookies=cookies)
        assert response.status_code == 200

        # Cleanup
        await redis_client.delete(f"drcc:session:{session_id}")

    @pytest.mark.asyncio
    async def test_deactivated_user_session_is_rejected(
        self, redis_client: Redis, session: AsyncSession, clock: FakeClock, seed_user: uuid.UUID
    ) -> None:
        """A session created before the user was deactivated must stop working immediately, not
        keep being accepted and refreshed until its normal idle/absolute expiry — found in review:
        `get_current_session` only checked Redis TTLs, never the Postgres user record, so a
        deactivated administrator could keep calling privileged endpoints with an existing
        session. Revokes the Redis session too, as defense in depth."""
        app, store = self._build_app(redis_client, session, clock)
        session_id = uuid.uuid4()

        await store.create(session_id, seed_user, "LOCAL", clock)
        cookies = {"drcc_session": str(session_id)}

        # Valid before deactivation.
        async with await self._client(app) as client:
            response = await client.get("/api/v1/_test-session", cookies=cookies)
        assert response.status_code == 200

        await session.execute(text("UPDATE users SET is_active = false WHERE id = :id"), {"id": seed_user})
        await session.commit()

        async with await self._client(app) as client:
            response = await client.get("/api/v1/_test-session", cookies=cookies)

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "SESSION_EXPIRED"

        # The Redis session itself must be revoked, not just rejected at this layer.
        assert await store.read(str(session_id)) is None

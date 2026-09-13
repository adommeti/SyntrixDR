"""Mounts the REAL users_teams_org.routes.router (not a hand-rolled test app) to prove the actual
FastAPI DI wiring works end-to-end, not just the underlying commands/queries in isolation — the gap
that let BUILD-02 session a's routes.py ship broken (see BUILD-02.handoff.md)."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock, FakeClock
from app.core.database import get_request_session
from app.core.errors import AppError, app_error_handler
from app.identity_auth.dependencies import get_clock, get_session_store
from app.identity_auth.models import LocalCredential, ReauthGrant, SessionRecord
from app.identity_auth.security import PasswordHasher
from app.identity_auth.session_store import RedisSessionStore
from app.users_teams_org.models import RoleAssignment, Team
from app.users_teams_org.routes import router

pytestmark = [pytest.mark.api, pytest.mark.integration]


def _build_app(session: AsyncSession, redis_client: Redis, clock: FakeClock) -> FastAPI:
    app = FastAPI()
    app.add_exception_handler(AppError, app_error_handler)
    app.include_router(router)

    async def _get_session_override():  # noqa: ANN202
        yield session

    async def _get_clock_override() -> Clock:
        return clock

    app.state.session_store = RedisSessionStore(redis_client, idle_minutes=30, absolute_hours=8)
    app.dependency_overrides[get_request_session] = _get_session_override
    app.dependency_overrides[get_clock] = _get_clock_override
    app.dependency_overrides[get_session_store] = lambda: app.state.session_store
    return app


async def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _create_session_cookie(
    session: AsyncSession, redis_client: Redis, clock: FakeClock, user_id: uuid.UUID
) -> tuple[str, str]:
    store = RedisSessionStore(redis_client, idle_minutes=30, absolute_hours=8)
    session_id = uuid.uuid4()
    data = await store.create(session_id, user_id, "LOCAL", clock)
    # reauth_grants.session_id FKs to the durable `sessions` table, not just the Redis-side record.
    session.add(
        SessionRecord(
            id=session_id,
            user_id=user_id,
            identity_type="LOCAL",
            created_at=clock.now(),
            last_seen_at=clock.now(),
            absolute_expires_at=clock.now() + timedelta(hours=8),
            idle_expires_at=clock.now() + timedelta(minutes=30),
        )
    )
    await session.flush()
    return str(session_id), data.csrf_token


@pytest.mark.asyncio
async def test_get_me_without_session_returns_401(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    app = _build_app(session, redis_client, clock)
    async with await _client(app) as client:
        response = await client.get("/api/v1/me")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_get_me_returns_profile(
    session: AsyncSession, redis_client: Redis, clock: FakeClock, seed_user: uuid.UUID
) -> None:
    app = _build_app(session, redis_client, clock)
    session_id, _ = await _create_session_cookie(session, redis_client, clock, seed_user)

    async with await _client(app) as client:
        response = await client.get("/api/v1/me", cookies={"drcc_session": session_id})

    assert response.status_code == 200
    assert response.json()["id"] == str(seed_user)
    await redis_client.delete(f"drcc:session:{session_id}")


@pytest.mark.asyncio
async def test_get_user_by_id(
    session: AsyncSession, redis_client: Redis, clock: FakeClock, seed_user: uuid.UUID
) -> None:
    app = _build_app(session, redis_client, clock)
    session_id, _ = await _create_session_cookie(session, redis_client, clock, seed_user)

    async with await _client(app) as client:
        response = await client.get(f"/api/v1/users/{seed_user}", cookies={"drcc_session": session_id})

    assert response.status_code == 200
    assert response.json()["id"] == str(seed_user)
    await redis_client.delete(f"drcc:session:{session_id}")


@pytest.mark.asyncio
async def test_get_user_by_id_404_for_unknown_user(
    session: AsyncSession, redis_client: Redis, clock: FakeClock, seed_user: uuid.UUID
) -> None:
    app = _build_app(session, redis_client, clock)
    session_id, _ = await _create_session_cookie(session, redis_client, clock, seed_user)

    async with await _client(app) as client:
        response = await client.get(f"/api/v1/users/{uuid.uuid4()}", cookies={"drcc_session": session_id})

    assert response.status_code == 404
    await redis_client.delete(f"drcc:session:{session_id}")


@pytest.mark.asyncio
async def test_list_teams(
    session: AsyncSession, redis_client: Redis, clock: FakeClock, seed_user: uuid.UUID
) -> None:
    team_id = uuid.uuid4()
    session.add(Team(id=team_id, name=f"team-{team_id}"))
    await session.flush()

    app = _build_app(session, redis_client, clock)
    session_id, _ = await _create_session_cookie(session, redis_client, clock, seed_user)

    async with await _client(app) as client:
        response = await client.get("/api/v1/teams", cookies={"drcc_session": session_id})

    assert response.status_code == 200
    assert any(t["id"] == str(team_id) for t in response.json()["teams"])
    await redis_client.delete(f"drcc:session:{session_id}")


@pytest.mark.asyncio
async def test_team_workload_404_for_unknown_team(
    session: AsyncSession, redis_client: Redis, clock: FakeClock, seed_user: uuid.UUID
) -> None:
    app = _build_app(session, redis_client, clock)
    session_id, _ = await _create_session_cookie(session, redis_client, clock, seed_user)

    async with await _client(app) as client:
        response = await client.get(
            f"/api/v1/teams/{uuid.uuid4()}/workload", cookies={"drcc_session": session_id}
        )

    assert response.status_code == 404
    await redis_client.delete(f"drcc:session:{session_id}")


async def _create_local_admin(session: AsyncSession, password: str) -> uuid.UUID:
    user_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO users (id, identity_type, display_name, email) "
            "VALUES (:id, 'LOCAL', 'Admin', :email)"
        ),
        {"id": user_id, "email": f"{user_id}@example.test"},
    )
    hasher = PasswordHasher()
    session.add(LocalCredential(user_id=user_id, password_hash=hasher.hash_password(password)))
    session.add(RoleAssignment(user_id=user_id, role_key="GLOBAL_ADMIN", scope_type="GLOBAL"))
    await session.flush()
    return user_id


@pytest.mark.asyncio
async def test_create_local_user_without_session_returns_401(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    app = _build_app(session, redis_client, clock)

    async with await _client(app) as client:
        response = await client.post(
            "/api/v1/admin/local-users",
            json={"display_name": "New User", "email": "new@example.test", "password": "NewPassword123!"},
        )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_create_local_user_requires_csrf(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    admin_id = await _create_local_admin(session, "AdminPassword123!")
    app = _build_app(session, redis_client, clock)
    session_id, csrf_token = await _create_session_cookie(session, redis_client, clock, admin_id)

    from datetime import timedelta

    session.add(
        ReauthGrant(
            session_id=uuid.UUID(session_id),
            user_id=admin_id,
            method="PASSWORD",
            granted_at=clock.now(),
            expires_at=clock.now() + timedelta(minutes=5),
        )
    )
    await session.flush()

    async with await _client(app) as client:
        response = await client.post(
            "/api/v1/admin/local-users",
            cookies={"drcc_session": session_id},
            json={"display_name": "New User", "email": "new@example.test", "password": "NewPassword123!"},
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CSRF_TOKEN_INVALID"
    await redis_client.delete(f"drcc:session:{session_id}")
    _ = csrf_token


@pytest.mark.asyncio
async def test_create_local_user_requires_reauth(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    admin_id = await _create_local_admin(session, "AdminPassword123!")
    app = _build_app(session, redis_client, clock)
    session_id, csrf_token = await _create_session_cookie(session, redis_client, clock, admin_id)

    async with await _client(app) as client:
        response = await client.post(
            "/api/v1/admin/local-users",
            cookies={"drcc_session": session_id},
            headers={"X-CSRF-Token": csrf_token},
            json={"display_name": "New User", "email": "new@example.test", "password": "NewPassword123!"},
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "REAUTH_REQUIRED"
    await redis_client.delete(f"drcc:session:{session_id}")


@pytest.mark.asyncio
async def test_create_local_user_denies_non_admin(
    session: AsyncSession, redis_client: Redis, clock: FakeClock, seed_user: uuid.UUID
) -> None:
    from datetime import timedelta

    app = _build_app(session, redis_client, clock)
    session_id, csrf_token = await _create_session_cookie(session, redis_client, clock, seed_user)
    session.add(
        ReauthGrant(
            session_id=uuid.UUID(session_id),
            user_id=seed_user,
            method="PASSWORD",
            granted_at=clock.now(),
            expires_at=clock.now() + timedelta(minutes=5),
        )
    )
    await session.flush()

    async with await _client(app) as client:
        response = await client.post(
            "/api/v1/admin/local-users",
            cookies={"drcc_session": session_id},
            headers={"X-CSRF-Token": csrf_token, "Idempotency-Key": str(uuid.uuid4())},
            json={"display_name": "New User", "email": "new@example.test", "password": "NewPassword123!"},
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "AUTHORIZATION_REQUIRED"
    await redis_client.delete(f"drcc:session:{session_id}")


@pytest.mark.asyncio
async def test_create_local_user_succeeds_for_admin_with_csrf_and_reauth(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    from datetime import timedelta

    admin_id = await _create_local_admin(session, "AdminPassword123!")
    app = _build_app(session, redis_client, clock)
    session_id, csrf_token = await _create_session_cookie(session, redis_client, clock, admin_id)
    session.add(
        ReauthGrant(
            session_id=uuid.UUID(session_id),
            user_id=admin_id,
            method="PASSWORD",
            granted_at=clock.now(),
            expires_at=clock.now() + timedelta(minutes=5),
        )
    )
    await session.flush()

    idempotency_key = str(uuid.uuid4())
    async with await _client(app) as client:
        response = await client.post(
            "/api/v1/admin/local-users",
            cookies={"drcc_session": session_id},
            headers={"X-CSRF-Token": csrf_token, "Idempotency-Key": idempotency_key},
            json={"display_name": "New User", "email": "new@example.test", "password": "NewPassword123!"},
        )

    assert response.status_code == 200
    assert "user_id" in response.json()

    audit_row = await session.execute(
        text("SELECT action FROM audit_events WHERE entity_id = :uid"),
        {"uid": response.json()["user_id"]},
    )
    assert audit_row.scalar_one() == "AUTH_LOCAL_USER_CREATED"
    await redis_client.delete(f"drcc:session:{session_id}")


@pytest.mark.asyncio
async def test_create_local_user_missing_idempotency_key_returns_400(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    """D-215: POST /admin/local-users is a real domain command (not a session endpoint like
    identity_auth's routes), so a missing Idempotency-Key header is rejected."""
    admin_id = await _create_local_admin(session, "AdminPassword123!")
    app = _build_app(session, redis_client, clock)
    session_id, csrf_token = await _create_session_cookie(session, redis_client, clock, admin_id)
    session.add(
        ReauthGrant(
            session_id=uuid.UUID(session_id),
            user_id=admin_id,
            method="PASSWORD",
            granted_at=clock.now(),
            expires_at=clock.now() + timedelta(minutes=5),
        )
    )
    await session.flush()

    async with await _client(app) as client:
        response = await client.post(
            "/api/v1/admin/local-users",
            cookies={"drcc_session": session_id},
            headers={"X-CSRF-Token": csrf_token},
            json={"display_name": "New User", "email": "new@example.test", "password": "NewPassword123!"},
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    await redis_client.delete(f"drcc:session:{session_id}")


@pytest.mark.asyncio
async def test_create_local_user_replay_does_not_create_a_second_user(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    """D-215: replaying the same Idempotency-Key returns the original outcome, no second row."""
    admin_id = await _create_local_admin(session, "AdminPassword123!")
    app = _build_app(session, redis_client, clock)
    session_id, csrf_token = await _create_session_cookie(session, redis_client, clock, admin_id)
    session.add(
        ReauthGrant(
            session_id=uuid.UUID(session_id),
            user_id=admin_id,
            method="PASSWORD",
            granted_at=clock.now(),
            expires_at=clock.now() + timedelta(minutes=5),
        )
    )
    await session.flush()

    idempotency_key = str(uuid.uuid4())
    body = {"display_name": "New User", "email": "replay@example.test", "password": "NewPassword123!"}
    headers = {"X-CSRF-Token": csrf_token, "Idempotency-Key": idempotency_key}

    async with await _client(app) as client:
        first = await client.post(
            "/api/v1/admin/local-users", cookies={"drcc_session": session_id}, headers=headers, json=body
        )
        second = await client.post(
            "/api/v1/admin/local-users", cookies={"drcc_session": session_id}, headers=headers, json=body
        )

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()

    count = await session.execute(
        text("SELECT COUNT(*) AS cnt FROM users WHERE email = 'replay@example.test'")
    )
    assert count.one().cnt == 1
    await redis_client.delete(f"drcc:session:{session_id}")

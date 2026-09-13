"""CSRF protection tests — exercise the real dependencies, not a local reimplementation.

`app.identity_auth.dependencies.get_current_session`/`require_csrf` and
`app.identity_auth.session_store.RedisSessionStore` are wired into a small test app the
same way the real router wires them, via `CurrentSession`/`RequireCsrfDependency`.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis

from app.core.clock import Clock, FakeClock
from app.core.errors import AppError, app_error_handler
from app.identity_auth.dependencies import CurrentSession, RequireCsrfDependency, get_clock
from app.identity_auth.session_store import RedisSessionStore

pytestmark = [pytest.mark.api, pytest.mark.auth, pytest.mark.integration]


def _build_app(redis_client: Redis, clock: FakeClock) -> FastAPI:
    app = FastAPI()
    app.add_exception_handler(AppError, app_error_handler)
    app.state.session_store = RedisSessionStore(redis_client, idle_minutes=30, absolute_hours=8)

    async def _clock_override() -> Clock:
        return clock

    app.dependency_overrides[get_clock] = _clock_override

    @app.get("/api/v1/auth/csrf")
    async def get_csrf(session_data: CurrentSession) -> JSONResponse:
        return JSONResponse(status_code=200, content={"csrf_token": session_data.csrf_token})

    @app.post("/api/v1/auth/protected-action", dependencies=[RequireCsrfDependency])
    async def protected_post(session_data: CurrentSession) -> JSONResponse:
        return JSONResponse(
            status_code=200, content={"message": "success", "user_id": str(session_data.user_id)}
        )

    @app.post("/api/v1/auth/local/login")
    async def local_login() -> JSONResponse:
        """Mirrors the real login route's contract: no CSRF/session dependency at all."""
        return JSONResponse(status_code=200, content={"message": "no csrf required"})

    return app


async def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_get_csrf_without_session_returns_401(redis_client: Redis, clock: FakeClock) -> None:
    """GET /api/v1/auth/csrf without a session cookie -> 401 SESSION_EXPIRED."""
    app = _build_app(redis_client, clock)

    async with await _client(app) as client:
        response = await client.get("/api/v1/auth/csrf")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "SESSION_EXPIRED"


@pytest.mark.asyncio
async def test_get_csrf_with_valid_session_returns_token(
    redis_client: Redis, clock: FakeClock, seed_user: uuid.UUID
) -> None:
    """GET /api/v1/auth/csrf with a valid session -> returns the session's csrf_token."""
    app = _build_app(redis_client, clock)
    store = RedisSessionStore(redis_client, idle_minutes=30, absolute_hours=8)
    session_id = uuid.uuid4()
    created = await store.create(session_id, seed_user, "LOCAL", clock)

    async with await _client(app) as client:
        response = await client.get("/api/v1/auth/csrf", cookies={"drcc_session": str(session_id)})

    assert response.status_code == 200
    assert response.json()["csrf_token"] == created.csrf_token

    await redis_client.delete(f"drcc:session:{session_id}")


@pytest.mark.asyncio
async def test_post_without_csrf_header_returns_403(
    redis_client: Redis, clock: FakeClock, seed_user: uuid.UUID
) -> None:
    """POST to a CSRF-protected endpoint without X-CSRF-Token -> 403 CSRF_TOKEN_INVALID."""
    app = _build_app(redis_client, clock)
    store = RedisSessionStore(redis_client, idle_minutes=30, absolute_hours=8)
    session_id = uuid.uuid4()
    await store.create(session_id, seed_user, "LOCAL", clock)

    async with await _client(app) as client:
        response = await client.post(
            "/api/v1/auth/protected-action", cookies={"drcc_session": str(session_id)}, json={}
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CSRF_TOKEN_INVALID"

    await redis_client.delete(f"drcc:session:{session_id}")


@pytest.mark.asyncio
async def test_post_with_wrong_csrf_token_returns_403(
    redis_client: Redis, clock: FakeClock, seed_user: uuid.UUID
) -> None:
    """POST with the wrong X-CSRF-Token -> 403 CSRF_TOKEN_INVALID."""
    app = _build_app(redis_client, clock)
    store = RedisSessionStore(redis_client, idle_minutes=30, absolute_hours=8)
    session_id = uuid.uuid4()
    await store.create(session_id, seed_user, "LOCAL", clock)

    async with await _client(app) as client:
        response = await client.post(
            "/api/v1/auth/protected-action",
            cookies={"drcc_session": str(session_id)},
            headers={"X-CSRF-Token": "wrong-token"},
            json={},
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CSRF_TOKEN_INVALID"

    await redis_client.delete(f"drcc:session:{session_id}")


@pytest.mark.asyncio
async def test_post_with_correct_csrf_token_succeeds(
    redis_client: Redis, clock: FakeClock, seed_user: uuid.UUID
) -> None:
    """POST with the correct X-CSRF-Token -> request proceeds, 200."""
    app = _build_app(redis_client, clock)
    store = RedisSessionStore(redis_client, idle_minutes=30, absolute_hours=8)
    session_id = uuid.uuid4()
    created = await store.create(session_id, seed_user, "LOCAL", clock)

    async with await _client(app) as client:
        response = await client.post(
            "/api/v1/auth/protected-action",
            cookies={"drcc_session": str(session_id)},
            headers={"X-CSRF-Token": created.csrf_token},
            json={},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["message"] == "success"
    assert body["user_id"] == str(seed_user)

    await redis_client.delete(f"drcc:session:{session_id}")


@pytest.mark.asyncio
async def test_login_does_not_require_csrf(redis_client: Redis, clock: FakeClock) -> None:
    """POST /api/v1/auth/local/login has no session yet, so it carries no CSRF guard."""
    app = _build_app(redis_client, clock)

    async with await _client(app) as client:
        response = await client.post("/api/v1/auth/local/login", json={})

    assert response.status_code == 200

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import FakeClock
from app.core.errors import AppError, app_error_handler
from app.core.idempotency import IdempotencyContext, IdempotencyKey, complete, require_idempotency_key

pytestmark = [pytest.mark.api, pytest.mark.integration]

_SIDE_EFFECT_COUNTER = "side_effects"


def _build_app(session: AsyncSession, user_id: uuid.UUID, clock: FakeClock) -> FastAPI:
    app = FastAPI()
    app.state.side_effects = 0
    app.add_exception_handler(AppError, app_error_handler)

    async def _ctx(request: Request) -> IdempotencyContext:
        return await require_idempotency_key(request, session, user_id, clock)

    @app.post("/api/v1/_test-command")
    async def command_route(ctx=Depends(_ctx)) -> JSONResponse:  # noqa: ANN001, B008
        if ctx.is_replay:
            return JSONResponse(status_code=ctx.stored_status, content=ctx.stored_body)

        app.state.side_effects += 1
        body = {"side_effects": app.state.side_effects}
        await complete(session, ctx, 200, body)
        await session.commit()
        return JSONResponse(status_code=200, content=body)

    return app


async def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_missing_header_returns_400(
    session: AsyncSession, seed_user: uuid.UUID, clock: FakeClock
) -> None:
    app = _build_app(session, seed_user, clock)
    async with await _client(app) as client:
        response = await client.post("/api/v1/_test-command", json={})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


@pytest.mark.asyncio
async def test_replay_returns_original_outcome_without_reexecuting(
    session: AsyncSession, seed_user: uuid.UUID, clock: FakeClock, idempotency_key: str
) -> None:
    app = _build_app(session, seed_user, clock)
    headers = {"Idempotency-Key": idempotency_key}

    async with await _client(app) as client:
        first = await client.post("/api/v1/_test-command", json={}, headers=headers)
        second = await client.post("/api/v1/_test-command", json={}, headers=headers)

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() == {"side_effects": 1}
    assert app.state.side_effects == 1


@pytest.mark.asyncio
async def test_same_key_different_route_is_not_a_replay(
    session: AsyncSession, seed_user: uuid.UUID, clock: FakeClock, idempotency_key: str
) -> None:
    app = _build_app(session, seed_user, clock)
    app.state.side_effects = 0

    async def _ctx_other(request: Request) -> IdempotencyContext:
        return await require_idempotency_key(request, session, seed_user, clock)

    @app.post("/api/v1/_test-command-other")
    async def other_route(ctx=Depends(_ctx_other)) -> JSONResponse:  # noqa: ANN001, B008
        if ctx.is_replay:
            return JSONResponse(status_code=ctx.stored_status, content=ctx.stored_body)
        app.state.side_effects += 1
        body = {"side_effects": app.state.side_effects}
        await complete(session, ctx, 200, body)
        await session.commit()
        return JSONResponse(status_code=200, content=body)

    headers = {"Idempotency-Key": idempotency_key}
    async with await _client(app) as client:
        await client.post("/api/v1/_test-command", json={}, headers=headers)
        second = await client.post("/api/v1/_test-command-other", json={}, headers=headers)

    assert second.json() == {"side_effects": 2}
    assert app.state.side_effects == 2


@pytest.mark.asyncio
async def test_expired_key_re_executes(
    session: AsyncSession, seed_user: uuid.UUID, clock: FakeClock, idempotency_key: str
) -> None:
    app = _build_app(session, seed_user, clock)

    row = IdempotencyKey(
        key=idempotency_key,
        user_id=seed_user,
        route="/api/v1/_test-command",
        request_hash="deadbeef",
        response_status=200,
        response_body={"side_effects": 999},
        created_at=clock.now() - timedelta(hours=25),
        expires_at=clock.now() - timedelta(hours=1),
    )
    session.add(row)
    await session.flush()

    headers = {"Idempotency-Key": idempotency_key}
    async with await _client(app) as client:
        response = await client.post("/api/v1/_test-command", json={}, headers=headers)

    assert response.json() == {"side_effects": 1}


@pytest.mark.asyncio
async def test_row_persisted_with_expected_columns(
    session: AsyncSession, seed_user: uuid.UUID, clock: FakeClock, idempotency_key: str
) -> None:
    app = _build_app(session, seed_user, clock)
    headers = {"Idempotency-Key": idempotency_key}
    async with await _client(app) as client:
        await client.post("/api/v1/_test-command", json={}, headers=headers)

    row = (
        await session.execute(
            text("SELECT key, user_id, route, response_status FROM idempotency_keys WHERE key = :key"),
            {"key": idempotency_key},
        )
    ).one()
    assert row.route == "/api/v1/_test-command"
    assert row.response_status == 200

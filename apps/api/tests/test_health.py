from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.main import create_app

pytestmark = pytest.mark.api


@pytest.mark.asyncio
async def test_health_ok(pg_url: str) -> None:
    app = create_app(Settings(database_url=pg_url))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert "X-Correlation-Id" in response.headers

    await app.state.engine.dispose()


@pytest.mark.asyncio
async def test_unhandled_error_uses_envelope(pg_url: str) -> None:
    app = create_app(Settings(database_url=pg_url))

    @app.get("/api/v1/_boom")
    async def boom() -> None:
        raise RuntimeError("kaboom")

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/_boom")

    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "INTERNAL_ERROR"
    assert "correlation_id" in body["error"]

    await app.state.engine.dispose()

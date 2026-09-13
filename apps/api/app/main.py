from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import Settings, get_settings
from app.core.database import make_engine
from app.core.errors import AppError, app_error_handler, unhandled_error_handler
from app.core.telemetry import configure_telemetry

CORRELATION_ID_HEADER = "X-Correlation-Id"


async def correlation_id_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    correlation_id = request.headers.get(CORRELATION_ID_HEADER) or str(uuid.uuid4())
    request.state.correlation_id = correlation_id
    response = await call_next(request)
    response.headers[CORRELATION_ID_HEADER] = correlation_id
    return response


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_telemetry(settings)

    app = FastAPI(title="Syntrix DR Command Center API", version="0.1.0")
    app.state.settings = settings
    app.state.engine = make_engine(settings.database_url)

    app.middleware("http")(correlation_id_middleware)
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)

    @app.get("/api/v1/health")
    async def health() -> dict[str, str]:
        engine: AsyncEngine = app.state.engine
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return {"status": "ok"}

    return app


app = create_app()

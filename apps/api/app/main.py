from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.middleware.sessions import SessionMiddleware

from app.applications_catalog.routes import router as applications_catalog_router
from app.core.config import Settings, get_settings
from app.core.database import make_engine, make_session_factory
from app.core.errors import AppError, app_error_handler, unhandled_error_handler
from app.core.storage import ObjectStore
from app.core.telemetry import configure_telemetry
from app.dr_events.routes import router as dr_events_router
from app.identity_auth.oidc import register_entra_oauth
from app.identity_auth.routes import router as identity_auth_router
from app.identity_auth.session_store import RedisSessionStore
from app.plans_import.routes import router as plans_import_router
from app.policies_admin.routes import router as policies_admin_router
from app.users_teams_org.routes import router as users_teams_org_router

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
    if not settings.session_secret or not settings.csrf_secret:
        raise RuntimeError("SESSION_SECRET and CSRF_SECRET must be set (see .env.example).")
    configure_telemetry(settings)

    app = FastAPI(title="Syntrix DR Command Center API", version="0.1.0")
    # Authlib's authorize_redirect()/authorize_access_token() need Starlette's session for OIDC
    # state/nonce — a short-lived signed cookie for the handshake only, unrelated to and separate
    # from the drcc_session cookie (D-239's Redis-backed session, still the only auth boundary).
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        session_cookie="drcc_oidc_handshake",
        same_site="lax",
        https_only=settings.drcc_env != "local",
        max_age=600,
    )
    app.state.settings = settings
    app.state.engine = make_engine(settings.database_url)
    app.state.session_factory = make_session_factory(app.state.engine)
    app.state.redis = Redis.from_url(settings.redis_url)  # type: ignore[reportUnknownMemberType]
    app.state.session_store = RedisSessionStore(
        app.state.redis,
        idle_minutes=settings.session_idle_minutes,
        absolute_hours=settings.session_absolute_hours,
    )
    app.state.object_store = ObjectStore(settings.azure_storage_connection_string)
    register_entra_oauth(app.state.redis)

    app.middleware("http")(correlation_id_middleware)
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)
    app.include_router(identity_auth_router)
    app.include_router(users_teams_org_router)
    app.include_router(applications_catalog_router)
    app.include_router(policies_admin_router)
    app.include_router(plans_import_router)
    app.include_router(dr_events_router)

    @app.get("/api/v1/health")
    async def health() -> dict[str, str]:
        engine: AsyncEngine = app.state.engine
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return {"status": "ok"}

    return app


app = create_app()

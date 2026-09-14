from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock, SystemClock
from app.core.database import get_request_session
from app.core.errors import AppError
from app.identity_auth.models import ReauthGrant
from app.identity_auth.session_store import RedisSessionStore, SessionData


def get_clock() -> Clock:
    return SystemClock()


async def get_session_store(request: Request) -> RedisSessionStore:
    """FastAPI dependency: the process-wide Redis-backed session store on `app.state`."""
    return request.app.state.session_store  # type: ignore[no-any-return]


DbSession = Annotated[AsyncSession, Depends(get_request_session)]
SessionStore = Annotated[RedisSessionStore, Depends(get_session_store)]
ClockDep = Annotated[Clock, Depends(get_clock)]


class SessionExpiredError(AppError):
    code = "SESSION_EXPIRED"
    status_code = 401

    def __init__(self) -> None:
        super().__init__("Session expired or invalid.")


class CsrfTokenInvalidError(AppError):
    code = "CSRF_TOKEN_INVALID"
    status_code = 403

    def __init__(self) -> None:
        super().__init__("CSRF token is invalid or missing.")


class ReauthRequiredError(AppError):
    code = "REAUTH_REQUIRED"
    status_code = 403

    def __init__(self) -> None:
        super().__init__("Re-authentication is required for this operation.")


async def get_current_session(
    request: Request,
    store: RedisSessionStore,
    clock: Clock,
) -> SessionData:
    """Read and validate a session from the 'drcc_session' cookie.

    Raises SessionExpiredError if the session is missing, invalid, or expired.
    """
    session_id = request.cookies.get("drcc_session")
    if not session_id:
        raise SessionExpiredError()

    session_data = await store.touch(session_id, clock)
    if session_data is None:
        raise SessionExpiredError()

    return session_data


async def require_csrf(request: Request, session_data: SessionData) -> None:
    """Validate CSRF token from the X-CSRF-Token header against the session.

    Raises CsrfTokenInvalidError on mismatch or if header is missing.
    """
    token = request.headers.get("X-CSRF-Token")
    if not token or token != session_data.csrf_token:
        raise CsrfTokenInvalidError()


async def require_reauth(
    session: AsyncSession,
    session_data: SessionData,
    clock: Clock,
) -> None:
    """Verify that a re-authentication grant exists and is still valid.

    Raises ReauthRequiredError if no valid grant exists.
    """
    from sqlalchemy import and_, select

    now = clock.now()
    result = await session.execute(
        select(ReauthGrant)
        .where(
            and_(
                ReauthGrant.session_id == session_data.session_id,
                ReauthGrant.user_id == session_data.user_id,
                ReauthGrant.expires_at > now,
            )
        )
        # Reauthenticating twice within the window creates two valid rows —
        # scalar_one_or_none() would raise MultipleResultsFound (found in review). Any one
        # unexpired grant is sufficient; take the most recent.
        .order_by(ReauthGrant.expires_at.desc())
        .limit(1)
    )

    grant = result.scalar_one_or_none()
    if grant is None:
        raise ReauthRequiredError()


# FastAPI-native composition of the three functions above (which stay plain, explicit-arg
# async functions so tests can call them directly without going through FastAPI's DI).


async def _current_session_dep(request: Request, store: SessionStore, clock: ClockDep) -> SessionData:
    return await get_current_session(request, store, clock)


CurrentSession = Annotated[SessionData, Depends(_current_session_dep)]


async def _require_csrf_dep(request: Request, session_data: CurrentSession) -> None:
    await require_csrf(request, session_data)


async def _require_reauth_dep(session: DbSession, session_data: CurrentSession, clock: ClockDep) -> None:
    await require_reauth(session, session_data, clock)


RequireCsrf = Annotated[None, Depends(_require_csrf_dep)]
RequireReauth = Annotated[None, Depends(_require_reauth_dep)]

# For use in @router.post(..., dependencies=[...]) lists, which need a Depends() instance,
# not an Annotated[...] type alias.
RequireCsrfDependency = Depends(_require_csrf_dep)
RequireReauthDependency = Depends(_require_reauth_dep)

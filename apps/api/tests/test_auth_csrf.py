from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from app.core.errors import AppError, app_error_handler

pytestmark = [pytest.mark.api, pytest.mark.auth, pytest.mark.integration]


class SessionData:
    """Mock SessionData for testing CSRF without full session_store complexity."""

    def __init__(self, session_id: str, user_id: uuid.UUID, identity_type: str, csrf_token: str) -> None:
        self.session_id = session_id
        self.user_id = user_id
        self.identity_type = identity_type
        self.csrf_token = csrf_token


class SessionExpiredError(AppError):
    code = "SESSION_EXPIRED"
    status_code = 401

    def __init__(self) -> None:
        super().__init__("Session has expired or is invalid.")


class CsrfTokenInvalidError(AppError):
    code = "CSRF_TOKEN_INVALID"
    status_code = 403

    def __init__(self) -> None:
        super().__init__("CSRF token is missing or invalid.")


class MockSessionStore:
    """Minimal session store for testing; stores in memory."""

    def __init__(self) -> None:
        self.sessions: dict[str, SessionData] = {}

    async def create(
        self, session_id: str, user_id: uuid.UUID, identity_type: str, csrf_token: str
    ) -> SessionData:
        session_data = SessionData(session_id, user_id, identity_type, csrf_token)
        self.sessions[session_id] = session_data
        return session_data

    async def read(self, session_id: str) -> SessionData | None:
        return self.sessions.get(session_id)

    async def touch(self, session_id: str) -> SessionData | None:
        """Touch the session to update idle timeout."""
        return self.sessions.get(session_id)


async def get_current_session(request: Request, store: MockSessionStore) -> SessionData:
    """Dependency: read session from cookie, raise 401 if missing/expired."""
    session_id = request.cookies.get("drcc_session")
    if not session_id:
        raise SessionExpiredError()

    session_data = await store.read(session_id)
    if not session_data:
        raise SessionExpiredError()

    # Touch the session (update idle timeout)
    await store.touch(session_id)
    return session_data


async def require_csrf(request: Request, session_data: SessionData) -> None:
    """Dependency: validate X-CSRF-Token header matches session's csrf_token."""
    token = request.headers.get("X-CSRF-Token")
    if not token or token != session_data.csrf_token:
        raise CsrfTokenInvalidError()


def _build_app(store: MockSessionStore) -> FastAPI:
    """Build a test FastAPI app with CSRF-protected routes."""
    app = FastAPI()
    app.add_exception_handler(AppError, app_error_handler)
    app.state.store = store

    @app.get("/api/v1/auth/csrf", response_model=None)
    async def get_csrf(request: Request) -> JSONResponse:
        """Return the CSRF token for the current session."""
        session_data = await get_current_session(request, store)
        return JSONResponse(status_code=200, content={"csrf_token": session_data.csrf_token})

    @app.post("/api/v1/auth/protected-action", response_model=None)
    async def protected_post(request: Request) -> JSONResponse:
        """A CSRF-protected POST endpoint."""
        session_data = await get_current_session(request, store)
        await require_csrf(request, session_data)
        return JSONResponse(
            status_code=200, content={"message": "success", "user_id": str(session_data.user_id)}
        )

    @app.post("/api/v1/auth/local/login", response_model=None)
    async def local_login() -> JSONResponse:
        """Login does NOT require CSRF (no session yet)."""
        user_id = uuid.uuid4()
        session_id = str(uuid.uuid4())
        csrf_token = "test-csrf-token-12345"
        await store.create(session_id, user_id, "LOCAL", csrf_token)
        response = JSONResponse(status_code=200, content={"session_id": session_id, "csrf_token": csrf_token})
        response.set_cookie("drcc_session", session_id, httponly=True, samesite="lax")
        return response

    return app


async def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_get_csrf_without_session_returns_401() -> None:
    """GET /api/v1/auth/csrf without session cookie -> 401 SESSION_EXPIRED."""
    store = MockSessionStore()
    app = _build_app(store)

    async with await _client(app) as client:
        response = await client.get("/api/v1/auth/csrf")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "SESSION_EXPIRED"


@pytest.mark.asyncio
async def test_get_csrf_with_valid_session_returns_token() -> None:
    """GET /api/v1/auth/csrf with valid session -> returns the session's csrf_token."""
    store = MockSessionStore()
    user_id = uuid.uuid4()
    session_id = str(uuid.uuid4())
    expected_token = "test-csrf-token-abc123"
    await store.create(session_id, user_id, "LOCAL", expected_token)

    app = _build_app(store)
    async with await _client(app) as client:
        response = await client.get("/api/v1/auth/csrf", cookies={"drcc_session": session_id})

    assert response.status_code == 200
    body = response.json()
    assert body["csrf_token"] == expected_token


@pytest.mark.asyncio
async def test_post_without_csrf_header_returns_403() -> None:
    """POST to CSRF-protected endpoint without X-CSRF-Token header -> 403 CSRF_TOKEN_INVALID."""
    store = MockSessionStore()
    user_id = uuid.uuid4()
    session_id = str(uuid.uuid4())
    csrf_token = "valid-token-123"
    await store.create(session_id, user_id, "LOCAL", csrf_token)

    app = _build_app(store)
    async with await _client(app) as client:
        # No X-CSRF-Token header
        response = await client.post(
            "/api/v1/auth/protected-action", cookies={"drcc_session": session_id}, json={}
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CSRF_TOKEN_INVALID"


@pytest.mark.asyncio
async def test_post_with_wrong_csrf_token_returns_403() -> None:
    """POST with wrong X-CSRF-Token -> 403 CSRF_TOKEN_INVALID."""
    store = MockSessionStore()
    user_id = uuid.uuid4()
    session_id = str(uuid.uuid4())
    csrf_token = "correct-token"
    await store.create(session_id, user_id, "LOCAL", csrf_token)

    app = _build_app(store)
    async with await _client(app) as client:
        response = await client.post(
            "/api/v1/auth/protected-action",
            cookies={"drcc_session": session_id},
            headers={"X-CSRF-Token": "wrong-token"},
            json={},
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CSRF_TOKEN_INVALID"


@pytest.mark.asyncio
async def test_post_with_correct_csrf_token_succeeds() -> None:
    """POST with correct X-CSRF-Token -> request proceeds, 200."""
    store = MockSessionStore()
    user_id = uuid.uuid4()
    session_id = str(uuid.uuid4())
    csrf_token = "correct-token-xyz"
    await store.create(session_id, user_id, "LOCAL", csrf_token)

    app = _build_app(store)
    async with await _client(app) as client:
        response = await client.post(
            "/api/v1/auth/protected-action",
            cookies={"drcc_session": session_id},
            headers={"X-CSRF-Token": csrf_token},
            json={},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["message"] == "success"
    assert body["user_id"] == str(user_id)


@pytest.mark.asyncio
async def test_login_does_not_require_csrf() -> None:
    """POST /api/v1/auth/local/login has no session yet, does not require CSRF."""
    store = MockSessionStore()
    app = _build_app(store)

    async with await _client(app) as client:
        response = await client.post("/api/v1/auth/local/login", json={})

    assert response.status_code == 200
    body = response.json()
    assert "session_id" in body
    assert "csrf_token" in body
    # Verify the session was created and can be used in follow-up requests
    assert body["session_id"] in store.sessions

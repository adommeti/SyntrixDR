from __future__ import annotations

from fastapi import APIRouter, Request, Response
from fastapi.responses import RedirectResponse

from app.core.config import get_settings
from app.identity_auth.commands import (
    confirm_password_reset,
    enrol_totp,
    entra_callback,
    local_login,
    logout,
    reauth,
    request_password_reset,
    verify_totp,
)
from app.identity_auth.dependencies import (
    ClockDep,
    CurrentSession,
    DbSession,
    RequireCsrfDependency,
    RequireReauthDependency,
    SessionStore,
)
from app.identity_auth.oidc import get_entra_login_redirect, handle_entra_callback
from app.identity_auth.schemas import (
    CsrfTokenResponse,
    LocalLoginRequest,
    LocalLoginResponse,
    LogoutResponse,
    PasswordResetConfirmRequest,
    PasswordResetConfirmResponse,
    PasswordResetRequestBody,
    PasswordResetRequestResponse,
    ReauthRequest,
    ReauthResponse,
    TotpEnrolResponse,
    TotpVerifyRequest,
    TotpVerifyResponse,
)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


def _set_session_cookie(response: Response, session_id: str) -> None:
    settings = get_settings()
    response.set_cookie(
        "drcc_session",
        session_id,
        httponly=True,
        samesite="lax",
        secure=settings.drcc_env != "local",
        max_age=settings.session_absolute_hours * 3600,
    )


@router.post("/local/login")
async def post_local_login(
    body: LocalLoginRequest,
    response: Response,
    session: DbSession,
    store: SessionStore,
    clock: ClockDep,
) -> LocalLoginResponse:
    """POST /api/v1/auth/local/login

    Local username/password login. No CSRF required (no session yet).
    No Idempotency-Key required (session endpoints, not domain commands).
    """
    result = await local_login(
        session, store, clock, email=body.email, password=body.password, totp_code=body.totp_code
    )
    _set_session_cookie(response, result.session_id)
    return LocalLoginResponse(
        session_id=result.session_id, csrf_token=result.csrf_token, user_id=str(result.user_id)
    )


@router.post("/logout", dependencies=[RequireCsrfDependency])
async def post_logout(
    session: DbSession,
    store: SessionStore,
    session_data: CurrentSession,
) -> LogoutResponse:
    """POST /api/v1/auth/logout — terminate the current session. CSRF required."""
    await logout(session, store, session_data=session_data)
    return LogoutResponse(message="Logged out.")


@router.post("/reauth", dependencies=[RequireCsrfDependency])
async def post_reauth(
    body: ReauthRequest,
    session: DbSession,
    store: SessionStore,
    clock: ClockDep,
    session_data: CurrentSession,
) -> ReauthResponse:
    """POST /api/v1/auth/reauth — step-up via password/TOTP/Entra, issues a 5-min reauth grant."""
    result = await reauth(
        session,
        store,
        clock,
        session_data=session_data,
        method=body.method,
        password=body.password,
        totp_code=body.totp_code,
    )
    return ReauthResponse(granted_at=result.granted_at, expires_at=result.expires_at)


@router.post("/local/password-reset/request")
async def post_password_reset_request(
    body: PasswordResetRequestBody,
    session: DbSession,
    clock: ClockDep,
) -> PasswordResetRequestResponse:
    """POST /api/v1/auth/local/password-reset/request

    Always returns success — no account enumeration. No auth, no CSRF, no Idempotency-Key.
    """
    await request_password_reset(session, clock, email=body.email)
    return PasswordResetRequestResponse(message="If an account exists, a password reset link has been sent.")


@router.post("/local/password-reset/confirm")
async def post_password_reset_confirm(
    body: PasswordResetConfirmRequest,
    session: DbSession,
    clock: ClockDep,
) -> PasswordResetConfirmResponse:
    """POST /api/v1/auth/local/password-reset/confirm — consume the token, set a new password."""
    await confirm_password_reset(session, clock, token=body.token, new_password=body.new_password)
    return PasswordResetConfirmResponse(message="Password reset successful.")


@router.post("/local/totp/enrol", dependencies=[RequireCsrfDependency, RequireReauthDependency])
async def post_totp_enrol(
    session: DbSession,
    session_data: CurrentSession,
) -> TotpEnrolResponse:
    """POST /api/v1/auth/local/totp/enrol — start TOTP enrolment for the current Local user."""
    result = await enrol_totp(session, user_id=session_data.user_id)
    return TotpEnrolResponse(secret=result.secret, provisioning_uri=result.provisioning_uri)


@router.post("/local/totp/verify", dependencies=[RequireCsrfDependency])
async def post_totp_verify(
    body: TotpVerifyRequest,
    session: DbSession,
    session_data: CurrentSession,
) -> TotpVerifyResponse:
    """POST /api/v1/auth/local/totp/verify — confirm TOTP enrolment / verify a code."""
    await verify_totp(session, user_id=session_data.user_id, code=body.code)
    return TotpVerifyResponse(message="TOTP verified and enabled.")


@router.get("/csrf")
async def get_csrf(session_data: CurrentSession) -> CsrfTokenResponse:
    """GET /api/v1/auth/csrf — return the CSRF token bound to the current session."""
    return CsrfTokenResponse(csrf_token=session_data.csrf_token)


@router.get("/entra/login")
async def get_entra_login(request: Request) -> RedirectResponse:
    """GET /api/v1/auth/entra/login — redirect to the Entra OIDC authorization endpoint."""
    redis = request.app.state.redis
    return await get_entra_login_redirect(request, redis)  # type: ignore[return-value]


@router.get("/entra/callback")
async def get_entra_callback(
    request: Request,
    response: Response,
    session: DbSession,
    store: SessionStore,
    clock: ClockDep,
) -> LocalLoginResponse:
    """GET /api/v1/auth/entra/callback — OIDC callback; establishes the session cookie."""
    redis = request.app.state.redis
    entra_object_id, email, display_name = await handle_entra_callback(request, redis)
    result = await entra_callback(
        session, store, clock, entra_object_id=entra_object_id, email=email, display_name=display_name
    )
    _set_session_cookie(response, result.session_id)
    return LocalLoginResponse(
        session_id=result.session_id, csrf_token=result.csrf_token, user_id=str(result.user_id)
    )

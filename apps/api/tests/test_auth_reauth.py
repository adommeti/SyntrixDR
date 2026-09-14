from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import AuditEvent
from app.core.clock import FakeClock
from app.core.errors import AppError, app_error_handler
from app.identity_auth.commands import (
    AccountLockedError,
    InvalidCredentialsError,
    TotpInvalidError,
    local_login,
    reauth,
)
from app.identity_auth.dependencies import ReauthRequiredError, require_reauth
from app.identity_auth.models import LocalCredential, ReauthGrant, SessionRecord
from app.identity_auth.security import PasswordHasher, generate_totp_secret
from app.identity_auth.session_store import SessionData

pytestmark = [pytest.mark.api, pytest.mark.auth, pytest.mark.integration]


@pytest_asyncio.fixture
async def local_user_with_credentials(session: AsyncSession, clock: FakeClock) -> tuple[uuid.UUID, str]:
    """Create a LOCAL user with local credentials (password hash).

    Returns:
        Tuple of (user_id, plaintext_password)
    """
    user_id = uuid.uuid4()
    plaintext_password = "MyTestPassword123!"
    hasher = PasswordHasher()
    password_hash = hasher.hash_password(plaintext_password)

    # Create user
    await session.execute(
        text(
            "INSERT INTO users (id, identity_type, display_name, email) "
            "VALUES (:id, 'LOCAL', 'Test User', :email)"
        ),
        {"id": user_id, "email": f"{user_id}@example.test"},
    )

    # Create local credentials
    credential = LocalCredential(
        user_id=user_id,
        password_hash=password_hash,
        algorithm="argon2id",
        failed_attempts=0,
        totp_enabled=False,
        must_reset=False,
        created_at=clock.now(),
        updated_at=clock.now(),
    )
    session.add(credential)

    await session.flush()
    return user_id, plaintext_password


@pytest_asyncio.fixture
async def session_record(
    session: AsyncSession, local_user_with_credentials: tuple[uuid.UUID, str], clock: FakeClock
) -> SessionRecord:
    """Create a durable session record for testing require_reauth.

    Returns:
        SessionRecord with session_id, user_id, and expiry times set.
    """
    user_id, _ = local_user_with_credentials
    session_id = uuid.uuid4()
    now = clock.now()

    record = SessionRecord(
        id=session_id,
        user_id=user_id,
        identity_type="LOCAL",
        created_at=now,
        last_seen_at=now,
        absolute_expires_at=now + timedelta(hours=8),
        idle_expires_at=now + timedelta(minutes=30),
        revoked_at=None,
        revoked_reason=None,
    )
    session.add(record)
    await session.flush()
    return record


@pytest.fixture
def session_data(session_record: SessionRecord) -> SessionData:
    """Create session data object for passing to require_reauth and reauth.

    Returns:
        SessionData with session_id, user_id, identity_type, and expiry times.
    """
    return SessionData(
        session_id=str(session_record.id),
        user_id=session_record.user_id,
        identity_type="LOCAL",
        csrf_token="dummy-csrf-token-for-testing",
        absolute_expires_at=session_record.absolute_expires_at,
        idle_expires_at=session_record.idle_expires_at,
    )


@pytest.mark.asyncio
async def test_reauth_with_correct_password_inserts_grant(
    session: AsyncSession,
    local_user_with_credentials: tuple[uuid.UUID, str],
    session_data: SessionData,
    clock: FakeClock,
) -> None:
    """Test that reauth with correct password inserts a reauth_grants row."""
    user_id, plaintext_password = local_user_with_credentials

    result = await reauth(
        session,
        store=None,  # type: ignore # store is not used in reauth
        clock=clock,
        session_data=session_data,
        method="PASSWORD",
        password=plaintext_password,
    )

    # Verify the grant was created
    grant = (
        await session.execute(
            select(ReauthGrant).where(
                (ReauthGrant.session_id == uuid.UUID(session_data.session_id))
                & (ReauthGrant.user_id == user_id)
            )
        )
    ).scalar_one_or_none()

    assert grant is not None
    assert grant.method == "PASSWORD"
    assert grant.granted_at == clock.now()
    assert grant.expires_at == clock.now() + timedelta(minutes=5)

    # Verify the response
    assert result.granted_at == clock.now().isoformat()
    assert result.expires_at == (clock.now() + timedelta(minutes=5)).isoformat()


@pytest.mark.asyncio
async def test_require_reauth_passes_within_window(
    session: AsyncSession,
    local_user_with_credentials: tuple[uuid.UUID, str],
    session_data: SessionData,
    clock: FakeClock,
) -> None:
    """Test that require_reauth passes when a valid grant exists within the 5-minute window."""
    _, plaintext_password = local_user_with_credentials

    # First, grant reauth
    await reauth(
        session,
        store=None,  # type: ignore
        clock=clock,
        session_data=session_data,
        method="PASSWORD",
        password=plaintext_password,
    )

    # Verify require_reauth passes (does not raise)
    await require_reauth(session, session_data, clock)  # Should not raise


@pytest.mark.asyncio
async def test_require_reauth_passes_with_two_overlapping_valid_grants(
    session: AsyncSession,
    local_user_with_credentials: tuple[uuid.UUID, str],
    session_data: SessionData,
    clock: FakeClock,
) -> None:
    """Reauthenticating twice within the 5-minute window creates two rows that are BOTH still
    valid at the same instant. `require_reauth`'s query must not raise `MultipleResultsFound` in
    that case (found in review: `scalar_one_or_none()` on a query that can match >1 row)."""
    _, plaintext_password = local_user_with_credentials

    for _ in range(2):
        await reauth(
            session,
            store=None,  # type: ignore
            clock=clock,
            session_data=session_data,
            method="PASSWORD",
            password=plaintext_password,
        )

    grants = (
        (
            await session.execute(
                select(ReauthGrant).where(
                    (ReauthGrant.session_id == uuid.UUID(session_data.session_id))
                    & (ReauthGrant.user_id == session_data.user_id)
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(grants) == 2, "test setup should produce two overlapping valid grants"

    await require_reauth(session, session_data, clock)  # must not raise MultipleResultsFound


@pytest.mark.asyncio
async def test_require_reauth_fails_after_expiry(
    session: AsyncSession,
    local_user_with_credentials: tuple[uuid.UUID, str],
    session_data: SessionData,
    clock: FakeClock,
) -> None:
    """Test that require_reauth raises ReauthRequiredError after the 5-minute window expires.

    Even though the grant row still exists, it should be considered expired by the clock.
    """
    user_id, plaintext_password = local_user_with_credentials

    # Grant reauth
    await reauth(
        session,
        store=None,  # type: ignore
        clock=clock,
        session_data=session_data,
        method="PASSWORD",
        password=plaintext_password,
    )

    # Advance clock past the 5-minute window (5m 1s)
    clock.advance(minutes=5, seconds=1)

    # Verify that require_reauth now raises ReauthRequiredError
    with pytest.raises(ReauthRequiredError):
        await require_reauth(session, session_data, clock)

    # Verify the grant row still exists in the database (it's just expired)
    grant = (
        await session.execute(
            select(ReauthGrant).where(
                (ReauthGrant.session_id == uuid.UUID(session_data.session_id))
                & (ReauthGrant.user_id == user_id)
            )
        )
    ).scalar_one_or_none()

    assert grant is not None, "Grant should still exist in DB even though it's expired"


@pytest.mark.asyncio
async def test_reauth_after_expiry_issues_fresh_grant(
    session: AsyncSession,
    local_user_with_credentials: tuple[uuid.UUID, str],
    session_data: SessionData,
    clock: FakeClock,
) -> None:
    """Test that calling reauth again after expiry creates a new grant."""
    user_id, plaintext_password = local_user_with_credentials

    # First grant
    await reauth(
        session,
        store=None,  # type: ignore
        clock=clock,
        session_data=session_data,
        method="PASSWORD",
        password=plaintext_password,
    )

    # Advance clock past expiry
    clock.advance(minutes=5, seconds=1)

    # Second grant (after expiry)
    await reauth(
        session,
        store=None,  # type: ignore
        clock=clock,
        session_data=session_data,
        method="PASSWORD",
        password=plaintext_password,
    )

    # Verify both grants exist in the database
    grants = (
        (
            await session.execute(
                select(ReauthGrant).where(
                    (ReauthGrant.session_id == uuid.UUID(session_data.session_id))
                    & (ReauthGrant.user_id == user_id)
                )
            )
        )
        .scalars()
        .all()
    )

    assert len(grants) == 2, "Should have two grant rows (one expired, one fresh)"

    # The second grant should have a new expiry time
    fresh_grant = max(grants, key=lambda g: g.granted_at)
    assert fresh_grant.granted_at == clock.now()
    assert fresh_grant.expires_at == clock.now() + timedelta(minutes=5)

    # Verify require_reauth passes with the new grant
    await require_reauth(session, session_data, clock)  # Should not raise


@pytest.mark.asyncio
async def test_reauth_with_wrong_password_raises_invalid_credentials(
    session: AsyncSession,
    local_user_with_credentials: tuple[uuid.UUID, str],
    session_data: SessionData,
    clock: FakeClock,
) -> None:
    """Test that reauth with the wrong password raises InvalidCredentialsError."""
    user_id, _ = local_user_with_credentials

    with pytest.raises(InvalidCredentialsError):
        await reauth(
            session,
            store=None,  # type: ignore
            clock=clock,
            session_data=session_data,
            method="PASSWORD",
            password="WrongPassword123!",
        )

    # Verify no grant was created
    grants = (
        (
            await session.execute(
                select(ReauthGrant).where(
                    (ReauthGrant.session_id == uuid.UUID(session_data.session_id))
                    & (ReauthGrant.user_id == user_id)
                )
            )
        )
        .scalars()
        .all()
    )

    assert len(grants) == 0, "No grant should be created on failed reauth"


@pytest.mark.asyncio
async def test_repeated_wrong_password_reauth_locks_the_account(
    session: AsyncSession,
    local_user_with_credentials: tuple[uuid.UUID, str],
    session_data: SessionData,
    clock: FakeClock,
) -> None:
    """A caller holding a stolen session must not get unlimited password guesses against
    `/auth/reauth` — found in review that failures here only produced audit records, with no
    attempt counter, lockout check, or rate limiter at all, unlike `local_login`. Shares the
    same D-235 10-failures/15-min lockout counter on the user's `local_credentials` row."""
    lockout_threshold = 10

    for _ in range(lockout_threshold - 1):
        with pytest.raises(InvalidCredentialsError):
            await reauth(
                session,
                store=None,  # type: ignore
                clock=clock,
                session_data=session_data,
                method="PASSWORD",
                password="WrongPassword123!",
            )

    with pytest.raises(AccountLockedError):
        await reauth(
            session,
            store=None,  # type: ignore
            clock=clock,
            session_data=session_data,
            method="PASSWORD",
            password="WrongPassword123!",
        )

    # Even the correct password is now rejected — the account is locked, not just the guess.
    _, plaintext_password = local_user_with_credentials
    with pytest.raises(AccountLockedError):
        await reauth(
            session,
            store=None,  # type: ignore
            clock=clock,
            session_data=session_data,
            method="PASSWORD",
            password=plaintext_password,
        )


@pytest.mark.asyncio
async def test_repeated_wrong_totp_reauth_locks_the_account(
    session: AsyncSession,
    local_user_with_credentials: tuple[uuid.UUID, str],
    session_data: SessionData,
    clock: FakeClock,
) -> None:
    """Same bound applies to the TOTP method — a stolen session must not allow unlimited
    six-digit guesses against `/auth/reauth` either (found in review)."""
    user_id, _ = local_user_with_credentials
    secret = generate_totp_secret()
    await session.execute(
        text(
            "UPDATE local_credentials SET totp_enabled = true, totp_secret_encrypted = :secret "
            "WHERE user_id = :uid"
        ),
        {"secret": secret, "uid": user_id},
    )
    await session.flush()

    lockout_threshold = 10

    for _ in range(lockout_threshold - 1):
        with pytest.raises(TotpInvalidError):
            await reauth(
                session,
                store=None,  # type: ignore
                clock=clock,
                session_data=session_data,
                method="TOTP",
                totp_code="000000",
            )

    with pytest.raises(AccountLockedError):
        await reauth(
            session,
            store=None,  # type: ignore
            clock=clock,
            session_data=session_data,
            method="TOTP",
            totp_code="000000",
        )


@pytest.mark.asyncio
async def test_reauth_failures_lock_out_local_login_too(
    session: AsyncSession,
    local_user_with_credentials: tuple[uuid.UUID, str],
    session_data: SessionData,
    clock: FakeClock,
) -> None:
    """`local_login` and `/auth/reauth` share the same D-235 lockout counter on
    `local_credentials` by design — one 10-failures/15-min policy per account, not per
    surface. Pin that this is intentional and verified: failed `reauth` attempts alone can lock
    out a subsequent `local_login`, and the reverse also holds."""
    user_id, plaintext_password = local_user_with_credentials
    email = (
        await session.execute(text("SELECT email FROM users WHERE id = :uid"), {"uid": user_id})
    ).scalar_one()
    lockout_threshold = 10

    for _ in range(lockout_threshold):
        with pytest.raises((InvalidCredentialsError, AccountLockedError)):
            await reauth(
                session,
                store=None,  # type: ignore
                clock=clock,
                session_data=session_data,
                method="PASSWORD",
                password="WrongPassword123!",
            )

    # The account is now locked purely from reauth guesses — local_login is blocked too,
    # even with the correct password.
    with pytest.raises(AccountLockedError):
        await local_login(
            session,
            store=None,  # type: ignore
            clock=clock,
            email=email,
            password=plaintext_password,
        )


@pytest.mark.asyncio
async def test_local_login_failures_lock_out_reauth_too(
    session: AsyncSession,
    local_user_with_credentials: tuple[uuid.UUID, str],
    session_data: SessionData,
    clock: FakeClock,
) -> None:
    """Reverse of the above: failed `local_login` attempts alone can lock out a subsequent
    `/auth/reauth`, proving the shared counter is symmetric."""
    user_id, plaintext_password = local_user_with_credentials
    email = (
        await session.execute(text("SELECT email FROM users WHERE id = :uid"), {"uid": user_id})
    ).scalar_one()
    lockout_threshold = 10

    for _ in range(lockout_threshold):
        with pytest.raises((InvalidCredentialsError, AccountLockedError)):
            await local_login(
                session,
                store=None,  # type: ignore
                clock=clock,
                email=email,
                password="WrongPassword123!",
            )

    with pytest.raises(AccountLockedError):
        await reauth(
            session,
            store=None,  # type: ignore
            clock=clock,
            session_data=session_data,
            method="PASSWORD",
            password=plaintext_password,
        )


@pytest.mark.asyncio
async def test_reauth_with_entra_method_is_rejected(
    session: AsyncSession,
    local_user_with_credentials: tuple[uuid.UUID, str],
    session_data: SessionData,
    clock: FakeClock,
) -> None:
    """No real Entra step-up flow exists this session; accepting method='ENTRA' at face value
    would let ANY caller — including a LOCAL user — claim a privileged reauth grant with zero
    verification. Must be rejected, not silently trusted."""
    user_id, _ = local_user_with_credentials

    with pytest.raises(InvalidCredentialsError):
        await reauth(
            session,
            store=None,  # type: ignore
            clock=clock,
            session_data=session_data,
            method="ENTRA",
        )

    grants = (
        (
            await session.execute(
                select(ReauthGrant).where(
                    (ReauthGrant.session_id == uuid.UUID(session_data.session_id))
                    & (ReauthGrant.user_id == user_id)
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(grants) == 0, "No grant should be created for an unverified ENTRA claim"


@pytest.mark.asyncio
async def test_require_reauth_without_grant_raises_error(
    session: AsyncSession, session_data: SessionData, clock: FakeClock
) -> None:
    """Test that require_reauth raises ReauthRequiredError when no grant exists."""
    # Don't create any grant; directly call require_reauth
    with pytest.raises(ReauthRequiredError):
        await require_reauth(session, session_data, clock)


@pytest.mark.asyncio
async def test_reauth_audit_on_success(
    session: AsyncSession,
    local_user_with_credentials: tuple[uuid.UUID, str],
    session_data: SessionData,
    clock: FakeClock,
) -> None:
    """Test that reauth creates an AUTH_REAUTH_GRANTED audit event on success."""
    user_id, plaintext_password = local_user_with_credentials

    await reauth(
        session,
        store=None,  # type: ignore
        clock=clock,
        session_data=session_data,
        method="PASSWORD",
        password=plaintext_password,
    )

    # Verify audit event was created
    audit = (
        await session.execute(
            select(AuditEvent)
            .where(
                (AuditEvent.entity_type == "SESSION")
                & (AuditEvent.entity_id == uuid.UUID(session_data.session_id))
                & (AuditEvent.action == "AUTH_REAUTH_GRANTED")
            )
            .order_by(AuditEvent.occurred_at.desc())
        )
    ).scalar_one_or_none()

    assert audit is not None
    assert audit.actor_user_id == user_id
    assert audit.metadata_["method"] == "PASSWORD"


@pytest.mark.asyncio
async def test_reauth_audit_on_failure(
    session: AsyncSession,
    local_user_with_credentials: tuple[uuid.UUID, str],
    session_data: SessionData,
    clock: FakeClock,
) -> None:
    """Test that reauth creates an AUTH_REAUTH_FAILED audit event on failure."""
    user_id, _ = local_user_with_credentials

    with pytest.raises(InvalidCredentialsError):
        await reauth(
            session,
            store=None,  # type: ignore
            clock=clock,
            session_data=session_data,
            method="PASSWORD",
            password="WrongPassword123!",
        )

    # Verify audit event was created
    audit = (
        await session.execute(
            select(AuditEvent)
            .where(
                (AuditEvent.entity_type == "SESSION")
                & (AuditEvent.entity_id == uuid.UUID(session_data.session_id))
                & (AuditEvent.action == "AUTH_REAUTH_FAILED")
            )
            .order_by(AuditEvent.occurred_at.desc())
        )
    ).scalar_one_or_none()

    assert audit is not None
    assert audit.actor_user_id == user_id
    assert audit.metadata_["method"] == "PASSWORD"


@pytest.mark.asyncio
async def test_reauth_via_http_route_with_csrf(
    session: AsyncSession,
    local_user_with_credentials: tuple[uuid.UUID, str],
    session_data: SessionData,
    clock: FakeClock,
) -> None:
    """Test reauth through a FastAPI route wired with require_reauth dependency (integration test).

    This verifies that the dependency works correctly in a real route context.
    """
    _, plaintext_password = local_user_with_credentials

    # Build a test FastAPI app with a protected route
    app = FastAPI()
    app.add_exception_handler(AppError, app_error_handler)

    async def _get_session_data() -> SessionData:
        """Return the test session data."""
        return session_data

    async def _require_reauth_dep(
        session_arg: AsyncSession = Depends(lambda: session),  # noqa: B008
        session_data_arg: SessionData = Depends(_get_session_data),  # noqa: B008
        clock_arg: FakeClock = Depends(lambda: clock),  # noqa: B008
    ) -> None:  # noqa: ANN001
        """Dependency wrapper for require_reauth."""
        return await require_reauth(session_arg, session_data_arg, clock_arg)

    @app.post("/api/v1/_test-sensitive-operation")
    async def sensitive_route(dep=Depends(_require_reauth_dep)) -> JSONResponse:  # noqa: ANN001, B008
        return JSONResponse(status_code=200, content={"status": "success"})

    async def _client(app: FastAPI) -> AsyncClient:
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    # First, call the route without a grant—should fail with 403
    async with await _client(app) as client:
        response = await client.post("/api/v1/_test-sensitive-operation")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "REAUTH_REQUIRED"

    # Grant reauth
    await reauth(
        session,
        store=None,  # type: ignore
        clock=clock,
        session_data=session_data,
        method="PASSWORD",
        password=plaintext_password,
    )

    # Now call the route—should succeed
    async with await _client(app) as client:
        response = await client.post("/api/v1/_test-sensitive-operation")

    assert response.status_code == 200
    assert response.json()["status"] == "success"

    # Advance clock past expiry
    clock.advance(minutes=5, seconds=1)

    # Call the route again—should fail again
    async with await _client(app) as client:
        response = await client.post("/api/v1/_test-sensitive-operation")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "REAUTH_REQUIRED"

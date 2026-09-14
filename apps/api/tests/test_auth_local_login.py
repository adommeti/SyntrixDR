from __future__ import annotations

import uuid

import pyotp
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import AuditEvent
from app.core.clock import FakeClock
from app.identity_auth.commands import (
    InvalidCredentialsError,
    TotpInvalidError,
    TotpRequiredError,
    local_login,
)
from app.identity_auth.models import LocalCredential, SessionRecord
from app.identity_auth.security import generate_totp_secret
from app.identity_auth.session_store import RedisSessionStore

pytestmark = [pytest.mark.api, pytest.mark.auth, pytest.mark.integration]


@pytest.mark.asyncio
async def test_successful_local_login_creates_session(
    session: AsyncSession, local_user_with_password: tuple[uuid.UUID, str], clock: FakeClock, redis_client
) -> None:
    """Successful login with correct email and password creates a durable session and Redis entry."""
    user_id, password = local_user_with_password
    email = f"{user_id}@example.test"

    store = RedisSessionStore(redis_client, idle_minutes=30, absolute_hours=8)

    result = await local_login(session, store, clock, email=email, password=password)

    # Verify LoginResult
    assert result.session_id is not None
    assert result.csrf_token is not None
    assert result.user_id == user_id

    # Verify SessionRecord row exists
    session_id_uuid = uuid.UUID(result.session_id)
    session_record = await session.get(SessionRecord, session_id_uuid)
    assert session_record is not None
    assert session_record.user_id == user_id
    assert session_record.identity_type == "LOCAL"
    assert session_record.revoked_at is None

    # Verify Redis session exists
    redis_session = await store.read(result.session_id)
    assert redis_session is not None
    assert redis_session.user_id == user_id
    assert redis_session.csrf_token == result.csrf_token

    # Verify audit event
    audit_events = await session.execute(
        select(AuditEvent).where(
            (AuditEvent.action == "AUTH_LOGIN_LOCAL_SUCCESS") & (AuditEvent.entity_id == session_id_uuid)
        )
    )
    assert audit_events.scalar_one_or_none() is not None


@pytest.mark.asyncio
async def test_wrong_password_raises_invalid_credentials(
    session: AsyncSession, local_user_with_password: tuple[uuid.UUID, str], clock: FakeClock, redis_client
) -> None:
    """Login with correct email but wrong password raises InvalidCredentialsError."""
    user_id, _ = local_user_with_password
    email = f"{user_id}@example.test"

    store = RedisSessionStore(redis_client, idle_minutes=30, absolute_hours=8)

    with pytest.raises(InvalidCredentialsError) as exc_info:
        await local_login(session, store, clock, email=email, password="WrongPassword123!")

    assert exc_info.value.code == "INVALID_CREDENTIALS"
    assert exc_info.value.status_code == 401

    # Verify audit event for failure
    audit_events = await session.execute(
        select(AuditEvent).where(
            (AuditEvent.action == "AUTH_LOGIN_LOCAL_FAILURE") & (AuditEvent.entity_id == user_id)
        )
    )
    assert audit_events.scalar_one_or_none() is not None


@pytest.mark.asyncio
async def test_unknown_email_raises_invalid_credentials_same_error(
    session: AsyncSession, clock: FakeClock, redis_client
) -> None:
    """Login with unknown email raises InvalidCredentialsError (same as wrong password, no enumeration)."""
    store = RedisSessionStore(redis_client, idle_minutes=30, absolute_hours=8)

    with pytest.raises(InvalidCredentialsError) as exc_info:
        await local_login(session, store, clock, email="nonexistent@example.test", password="AnyPassword123!")

    assert exc_info.value.code == "INVALID_CREDENTIALS"
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_unknown_email_and_wrong_password_same_error_message(
    session: AsyncSession, local_user_with_password: tuple[uuid.UUID, str], clock: FakeClock, redis_client
) -> None:
    """Error message is identical for unknown email and wrong password (no account enumeration)."""
    user_id, _ = local_user_with_password
    email = f"{user_id}@example.test"

    store = RedisSessionStore(redis_client, idle_minutes=30, absolute_hours=8)

    # Get error for wrong password
    wrong_password_error = None
    try:
        await local_login(session, store, clock, email=email, password="WrongPassword123!")
    except InvalidCredentialsError as e:
        wrong_password_error = str(e)

    # Get error for unknown email
    unknown_email_error = None
    try:
        await local_login(session, store, clock, email="unknown@example.test", password="TestPassword123!")
    except InvalidCredentialsError as e:
        unknown_email_error = str(e)

    assert wrong_password_error == unknown_email_error


@pytest.mark.asyncio
async def test_totp_enabled_without_code_raises_totp_required(
    session: AsyncSession, local_user_with_password: tuple[uuid.UUID, str], clock: FakeClock, redis_client
) -> None:
    """User with TOTP enabled but no code provided raises TotpRequiredError."""
    user_id, password = local_user_with_password
    email = f"{user_id}@example.test"

    # Enable TOTP for user
    secret = generate_totp_secret()
    cred_result = await session.execute(select(LocalCredential).where(LocalCredential.user_id == user_id))
    cred = cred_result.scalar_one_or_none()
    assert cred is not None
    cred.totp_secret_encrypted = secret
    cred.totp_enabled = True
    await session.flush()

    store = RedisSessionStore(redis_client, idle_minutes=30, absolute_hours=8)

    with pytest.raises(TotpRequiredError) as exc_info:
        await local_login(session, store, clock, email=email, password=password, totp_code=None)

    assert exc_info.value.code == "TOTP_REQUIRED"
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_totp_enabled_with_correct_code_succeeds(
    session: AsyncSession, local_user_with_password: tuple[uuid.UUID, str], clock: FakeClock, redis_client
) -> None:
    """User with TOTP enabled and correct code succeeds."""
    user_id, password = local_user_with_password
    email = f"{user_id}@example.test"

    # Enable TOTP for user
    secret = generate_totp_secret()
    cred_result = await session.execute(select(LocalCredential).where(LocalCredential.user_id == user_id))
    cred = cred_result.scalar_one_or_none()
    assert cred is not None
    cred.totp_secret_encrypted = secret
    cred.totp_enabled = True
    await session.flush()

    # Generate valid TOTP code
    totp = pyotp.TOTP(secret)
    valid_code = totp.now()

    store = RedisSessionStore(redis_client, idle_minutes=30, absolute_hours=8)

    result = await local_login(session, store, clock, email=email, password=password, totp_code=valid_code)

    assert result.session_id is not None
    assert result.csrf_token is not None
    assert result.user_id == user_id

    # Verify SessionRecord exists
    session_id_uuid = uuid.UUID(result.session_id)
    session_record = await session.get(SessionRecord, session_id_uuid)
    assert session_record is not None

    # Verify audit event
    audit_events = await session.execute(
        select(AuditEvent).where(
            (AuditEvent.action == "AUTH_LOGIN_LOCAL_SUCCESS") & (AuditEvent.entity_id == session_id_uuid)
        )
    )
    assert audit_events.scalar_one_or_none() is not None


@pytest.mark.asyncio
async def test_totp_enabled_with_incorrect_code_raises_totp_invalid(
    session: AsyncSession, local_user_with_password: tuple[uuid.UUID, str], clock: FakeClock, redis_client
) -> None:
    """User with TOTP enabled and incorrect code raises TotpInvalidError."""
    user_id, password = local_user_with_password
    email = f"{user_id}@example.test"

    # Enable TOTP for user
    secret = generate_totp_secret()
    cred_result = await session.execute(select(LocalCredential).where(LocalCredential.user_id == user_id))
    cred = cred_result.scalar_one_or_none()
    assert cred is not None
    cred.totp_secret_encrypted = secret
    cred.totp_enabled = True
    await session.flush()

    store = RedisSessionStore(redis_client, idle_minutes=30, absolute_hours=8)

    with pytest.raises(TotpInvalidError) as exc_info:
        await local_login(session, store, clock, email=email, password=password, totp_code="000000")

    assert exc_info.value.code == "TOTP_INVALID"
    assert exc_info.value.status_code == 401

    # Verify audit event for TOTP failure
    audit_events = await session.execute(
        select(AuditEvent).where(
            (AuditEvent.action == "AUTH_TOTP_FAILED") & (AuditEvent.entity_id == user_id)
        )
    )
    assert audit_events.scalar_one_or_none() is not None


@pytest.mark.asyncio
async def test_failed_attempts_incremented_on_wrong_password(
    session: AsyncSession, local_user_with_password: tuple[uuid.UUID, str], clock: FakeClock, redis_client
) -> None:
    """Failed login attempts are tracked and incremented."""
    user_id, _ = local_user_with_password
    email = f"{user_id}@example.test"

    store = RedisSessionStore(redis_client, idle_minutes=30, absolute_hours=8)

    # Get initial failed attempts
    cred_before_result = await session.execute(
        select(LocalCredential).where(LocalCredential.user_id == user_id)
    )
    cred_before = cred_before_result.scalar_one()
    initial_attempts = cred_before.failed_attempts

    # Attempt login with wrong password
    with pytest.raises(InvalidCredentialsError):
        await local_login(session, store, clock, email=email, password="WrongPassword123!")

    # Verify failed_attempts incremented
    cred_after_result = await session.execute(
        select(LocalCredential).where(LocalCredential.user_id == user_id)
    )
    cred_after = cred_after_result.scalar_one()
    assert cred_after.failed_attempts == initial_attempts + 1


@pytest.mark.asyncio
async def test_failed_attempts_reset_on_successful_login(
    session: AsyncSession, local_user_with_password: tuple[uuid.UUID, str], clock: FakeClock, redis_client
) -> None:
    """Failed login attempts are reset to 0 on successful login."""
    user_id, password = local_user_with_password
    email = f"{user_id}@example.test"

    store = RedisSessionStore(redis_client, idle_minutes=30, absolute_hours=8)

    # Increment failed attempts
    cred_result = await session.execute(select(LocalCredential).where(LocalCredential.user_id == user_id))
    cred = cred_result.scalar_one()
    cred.failed_attempts = 5
    await session.flush()

    # Login successfully
    result = await local_login(session, store, clock, email=email, password=password)

    # Verify failed_attempts reset
    cred_after_result = await session.execute(
        select(LocalCredential).where(LocalCredential.user_id == user_id)
    )
    cred_after = cred_after_result.scalar_one()
    assert cred_after.failed_attempts == 0
    assert result.user_id == user_id


@pytest.mark.asyncio
async def test_case_insensitive_email_lookup(
    session: AsyncSession, local_user_with_password: tuple[uuid.UUID, str], clock: FakeClock, redis_client
) -> None:
    """Email lookup is case-insensitive."""
    user_id, password = local_user_with_password
    # Original email is lowercase
    original_email = f"{user_id}@example.test"

    store = RedisSessionStore(redis_client, idle_minutes=30, absolute_hours=8)

    # Try login with uppercase email
    result = await local_login(session, store, clock, email=original_email.upper(), password=password)

    assert result.user_id == user_id
    assert result.session_id is not None


@pytest.mark.asyncio
async def test_deactivated_user_cannot_log_in(
    session: AsyncSession, local_user_with_password: tuple[uuid.UUID, str], clock: FakeClock, redis_client
) -> None:
    """A deactivated (`is_active=False`) Local user is denied login, same error as wrong password
    (no enumeration of account state)."""
    user_id, password = local_user_with_password
    email = f"{user_id}@example.test"
    await session.execute(text("UPDATE users SET is_active = false WHERE id = :id"), {"id": user_id})
    await session.flush()

    store = RedisSessionStore(redis_client, idle_minutes=30, absolute_hours=8)

    with pytest.raises(InvalidCredentialsError):
        await local_login(session, store, clock, email=email, password=password)

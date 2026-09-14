from __future__ import annotations

import asyncio
import hashlib
import uuid
from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.clock import FakeClock
from app.identity_auth.commands import (
    PasswordResetTokenInvalidError,
    confirm_password_reset,
    request_password_reset,
)
from app.identity_auth.models import LocalCredential, PasswordResetToken
from app.identity_auth.security import PasswordHasher, PasswordPolicyError

pytestmark = [pytest.mark.api, pytest.mark.auth, pytest.mark.integration]


@pytest_asyncio.fixture
async def local_user(session: AsyncSession) -> uuid.UUID:
    """Create a LOCAL user with local credentials for password reset tests."""
    user_id = uuid.uuid4()
    email = f"user-{user_id}@example.test"

    # Create user row
    await session.execute(
        text(
            "INSERT INTO users (id, identity_type, display_name, email) "
            "VALUES (:id, 'LOCAL', 'Test User', :email)"
        ),
        {"id": user_id, "email": email},
    )
    await session.flush()

    # Create local credentials with a known password
    hasher = PasswordHasher()
    password_hash = hasher.hash_password("OldPassword123!")
    cred = LocalCredential(user_id=user_id, password_hash=password_hash)
    session.add(cred)
    await session.flush()

    return user_id


@pytest.mark.asyncio
async def test_request_password_reset_for_existing_local_user(
    session: AsyncSession, local_user: uuid.UUID, clock: FakeClock
) -> None:
    """Request password reset for an existing LOCAL user creates token and sends email."""
    # Get the user's email
    user_email = (
        await session.execute(
            text("SELECT email FROM users WHERE id = :id"),
            {"id": local_user},
        )
    ).scalar_one()

    # Mock the mailer to capture the sent token
    sent_emails: list[tuple[str, str]] = []

    async def mock_send_email(to: str, token: str) -> None:
        sent_emails.append((to, token))

    with patch("app.identity_auth.commands.send_password_reset_email", new=mock_send_email):
        await request_password_reset(session, clock, email=user_email)

    # Verify token was sent
    assert len(sent_emails) == 1
    assert sent_emails[0][0] == user_email

    plaintext_token = sent_emails[0][1]
    assert len(plaintext_token) > 0

    # Verify token row was created in DB with correct hash
    token_hash = hashlib.sha256(plaintext_token.encode()).hexdigest()
    token_row = (
        await session.execute(select(PasswordResetToken).where(PasswordResetToken.token_hash == token_hash))
    ).scalar_one_or_none()

    assert token_row is not None
    assert token_row.user_id == local_user
    assert token_row.used_at is None
    assert token_row.expires_at > clock.now()

    # Verify audit entry
    audit_rows = (
        await session.execute(
            text(
                "SELECT action, entity_id, entity_type FROM audit_events "
                "WHERE action = 'AUTH_PASSWORD_RESET_REQUESTED' AND entity_id = :entity_id"
            ),
            {"entity_id": local_user},
        )
    ).fetchall()

    assert len(audit_rows) == 1
    assert audit_rows[0][1] == local_user
    assert audit_rows[0][2] == "USER"


@pytest.mark.asyncio
async def test_request_password_reset_for_nonexistent_email(session: AsyncSession, clock: FakeClock) -> None:
    """Request password reset for non-existent email silently no-ops."""
    nonexistent_email = "nobody@example.test"
    sent_emails: list[tuple[str, str]] = []

    async def mock_send_email(to: str, token: str) -> None:
        sent_emails.append((to, token))

    with patch("app.identity_auth.commands.send_password_reset_email", new=mock_send_email):
        # Should not raise
        await request_password_reset(session, clock, email=nonexistent_email)

    # No email should be sent
    assert len(sent_emails) == 0

    # No token should be created
    token_rows = (await session.execute(select(PasswordResetToken))).fetchall()
    assert len(token_rows) == 0


@pytest.mark.asyncio
async def test_confirm_password_reset_with_valid_token_and_password(
    session: AsyncSession, local_user: uuid.UUID, clock: FakeClock
) -> None:
    """Confirm password reset with correct token and valid password succeeds."""
    # Get user email
    user_email = (
        await session.execute(
            text("SELECT email FROM users WHERE id = :id"),
            {"id": local_user},
        )
    ).scalar_one()

    # Generate and store a password reset token
    sent_tokens: list[str] = []

    async def mock_send_email(to: str, token: str) -> None:
        sent_tokens.append(token)

    with patch("app.identity_auth.commands.send_password_reset_email", new=mock_send_email):
        await request_password_reset(session, clock, email=user_email)

    plaintext_token = sent_tokens[0]

    # Now confirm the reset with a new password
    new_password = "NewPassword123!@#"
    await confirm_password_reset(session, clock, token=plaintext_token, new_password=new_password)

    # Verify old password no longer works
    # Fetch the credential fresh and verify password was updated
    result = await session.execute(
        select(LocalCredential.password_hash).where(LocalCredential.user_id == local_user)
    )
    password_hash_from_db = result.scalar_one()

    hasher = PasswordHasher()

    assert password_hash_from_db is not None
    assert not hasher.verify_password("OldPassword123!", password_hash_from_db)
    assert hasher.verify_password(new_password, password_hash_from_db)

    # Verify token is marked as used
    token_hash = hashlib.sha256(plaintext_token.encode()).hexdigest()
    token_row = (
        await session.execute(select(PasswordResetToken).where(PasswordResetToken.token_hash == token_hash))
    ).scalar_one()

    assert token_row.used_at is not None

    # Verify lockout state is cleared
    cred = (
        await session.execute(select(LocalCredential).where(LocalCredential.user_id == local_user))
    ).scalar_one()
    assert cred.failed_attempts == 0
    assert cred.locked_until is None

    # Verify audit entry
    audit_rows = (
        await session.execute(
            text(
                "SELECT action, entity_id FROM audit_events "
                "WHERE action = 'AUTH_PASSWORD_RESET_CONFIRMED' AND entity_id = :entity_id"
            ),
            {"entity_id": local_user},
        )
    ).fetchall()

    assert len(audit_rows) == 1


@pytest.mark.asyncio
async def test_confirm_password_reset_same_token_twice_fails(
    session: AsyncSession, local_user: uuid.UUID, clock: FakeClock
) -> None:
    """Confirming the same token twice raises PasswordResetTokenInvalidError."""
    user_email = (
        await session.execute(
            text("SELECT email FROM users WHERE id = :id"),
            {"id": local_user},
        )
    ).scalar_one()

    # Generate token
    sent_tokens: list[str] = []

    async def mock_send_email(to: str, token: str) -> None:
        sent_tokens.append(token)

    with patch("app.identity_auth.commands.send_password_reset_email", new=mock_send_email):
        await request_password_reset(session, clock, email=user_email)

    plaintext_token = sent_tokens[0]

    # First confirmation succeeds
    new_password = "NewPassword123!@#"
    await confirm_password_reset(session, clock, token=plaintext_token, new_password=new_password)

    # Second confirmation with the same token should fail
    with pytest.raises(PasswordResetTokenInvalidError):
        await confirm_password_reset(session, clock, token=plaintext_token, new_password="AnotherNew123!@#")


@pytest.mark.asyncio
async def test_confirm_password_reset_after_expiry_fails(
    session: AsyncSession, local_user: uuid.UUID, clock: FakeClock
) -> None:
    """Confirming a token after 31 minutes (TTL is 30) raises PasswordResetTokenInvalidError."""
    user_email = (
        await session.execute(
            text("SELECT email FROM users WHERE id = :id"),
            {"id": local_user},
        )
    ).scalar_one()

    # Generate token
    sent_tokens: list[str] = []

    async def mock_send_email(to: str, token: str) -> None:
        sent_tokens.append(token)

    with patch("app.identity_auth.commands.send_password_reset_email", new=mock_send_email):
        await request_password_reset(session, clock, email=user_email)

    plaintext_token = sent_tokens[0]

    # Advance clock by 31 minutes (token TTL is 30)
    clock.advance(minutes=31)

    # Attempt to confirm should fail
    with pytest.raises(PasswordResetTokenInvalidError):
        await confirm_password_reset(session, clock, token=plaintext_token, new_password="NewPassword123!@#")


@pytest.mark.asyncio
async def test_confirm_password_reset_with_password_too_short_fails(
    session: AsyncSession, local_user: uuid.UUID, clock: FakeClock
) -> None:
    """Confirming with a password < 12 characters raises PasswordPolicyError (422)."""
    user_email = (
        await session.execute(
            text("SELECT email FROM users WHERE id = :id"),
            {"id": local_user},
        )
    ).scalar_one()

    # Generate token
    sent_tokens: list[str] = []

    async def mock_send_email(to: str, token: str) -> None:
        sent_tokens.append(token)

    with patch("app.identity_auth.commands.send_password_reset_email", new=mock_send_email):
        await request_password_reset(session, clock, email=user_email)

    plaintext_token = sent_tokens[0]

    # Attempt to confirm with short password
    short_password = "Short123"  # Only 9 chars
    with pytest.raises(PasswordPolicyError) as exc_info:
        await confirm_password_reset(session, clock, token=plaintext_token, new_password=short_password)

    assert exc_info.value.status_code == 422


@pytest.mark.asyncio
async def test_confirm_password_reset_with_password_too_long_fails(
    session: AsyncSession, local_user: uuid.UUID, clock: FakeClock
) -> None:
    """Confirming with a password > 128 characters raises PasswordPolicyError (422)."""
    user_email = (
        await session.execute(
            text("SELECT email FROM users WHERE id = :id"),
            {"id": local_user},
        )
    ).scalar_one()

    # Generate token
    sent_tokens: list[str] = []

    async def mock_send_email(to: str, token: str) -> None:
        sent_tokens.append(token)

    with patch("app.identity_auth.commands.send_password_reset_email", new=mock_send_email):
        await request_password_reset(session, clock, email=user_email)

    plaintext_token = sent_tokens[0]

    # Attempt to confirm with long password
    long_password = "A" * 129  # 129 chars
    with pytest.raises(PasswordPolicyError) as exc_info:
        await confirm_password_reset(session, clock, token=plaintext_token, new_password=long_password)

    assert exc_info.value.status_code == 422


@pytest.mark.asyncio
async def test_confirm_password_reset_with_breached_password_fails(
    session: AsyncSession, local_user: uuid.UUID, clock: FakeClock
) -> None:
    """Confirming with a breached password from the denylist raises PasswordPolicyError (422)."""
    user_email = (
        await session.execute(
            text("SELECT email FROM users WHERE id = :id"),
            {"id": local_user},
        )
    ).scalar_one()

    # Generate token
    sent_tokens: list[str] = []

    async def mock_send_email(to: str, token: str) -> None:
        sent_tokens.append(token)

    with patch("app.identity_auth.commands.send_password_reset_email", new=mock_send_email):
        await request_password_reset(session, clock, email=user_email)

    plaintext_token = sent_tokens[0]

    # Attempt to confirm with a known breached password from denylist
    breached_password = "password1"  # In COMMON_BREACHED_PASSWORDS
    with pytest.raises(PasswordPolicyError) as exc_info:
        await confirm_password_reset(session, clock, token=plaintext_token, new_password=breached_password)

    assert exc_info.value.status_code == 422


@pytest.mark.asyncio
async def test_concurrent_confirm_password_reset_only_one_succeeds(engine: AsyncEngine) -> None:
    """Two concurrent confirmations of the same token must not both succeed — found in review: a
    plain SELECT-then-check-then-write let both readers see `used_at=None` before either
    committed. Uses two real, independently-connected sessions (not the shared per-test
    transaction), matching core/idempotency.py's proven concurrency-test pattern."""
    user_id = uuid.uuid4()
    email = f"race-{user_id}@example.test"
    plaintext_token = "concurrent-reset-token"
    token_hash = hashlib.sha256(plaintext_token.encode()).hexdigest()
    clock = FakeClock()

    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, identity_type, display_name, email) "
                "VALUES (:id, 'LOCAL', 'Test User', :email)"
            ),
            {"id": user_id, "email": email},
        )
        hasher = PasswordHasher()
        await conn.execute(
            text(
                "INSERT INTO local_credentials (id, user_id, password_hash) " "VALUES (:id, :user_id, :hash)"
            ),
            {"id": uuid.uuid4(), "user_id": user_id, "hash": hasher.hash_password("OldPassword123!")},
        )
        await conn.execute(
            text(
                "INSERT INTO password_reset_tokens (id, user_id, token_hash, expires_at) "
                "VALUES (:id, :user_id, :token_hash, :expires_at)"
            ),
            {
                "id": uuid.uuid4(),
                "user_id": user_id,
                "token_hash": token_hash,
                "expires_at": clock.now().replace(year=clock.now().year + 1),
            },
        )

    results: list[str] = []

    async def _racer(new_password: str, *, ready: asyncio.Event | None = None, hold: float = 0.0) -> None:
        conn = await engine.connect()
        try:
            sess = AsyncSession(bind=conn, expire_on_commit=False)
            original_execute = sess.execute

            async def _traced_execute(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
                result = await original_execute(*args, **kwargs)
                if ready is not None:
                    ready.set()
                    if hold:
                        await asyncio.sleep(hold)
                return result

            sess.execute = _traced_execute  # type: ignore[method-assign]
            try:
                await confirm_password_reset(sess, clock, token=plaintext_token, new_password=new_password)
                results.append("success")
            except PasswordResetTokenInvalidError:
                results.append("rejected")
        finally:
            await conn.close()

    try:
        ready = asyncio.Event()
        first = asyncio.create_task(_racer("FirstNewPassword123!", ready=ready, hold=0.1))
        await ready.wait()
        second = asyncio.create_task(_racer("SecondNewPassword123!"))
        await asyncio.gather(first, second)

        assert sorted(results) == [
            "rejected",
            "success",
        ], f"exactly one racer must succeed, the other must be rejected — got {results}"

        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text("SELECT used_at FROM password_reset_tokens WHERE token_hash = :h"),
                    {"h": token_hash},
                )
            ).one()
            assert row.used_at is not None
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM password_reset_tokens WHERE user_id = :uid"), {"uid": user_id}
            )
            await conn.execute(text("DELETE FROM local_credentials WHERE user_id = :uid"), {"uid": user_id})
            await conn.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})

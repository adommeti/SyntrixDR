"""Tests for authentication lockout logic (BUILD-02 session a)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.clock import Clock, FakeClock
from app.identity_auth.commands import (
    AccountLockedError,
    InvalidCredentialsError,
    TotpInvalidError,
    local_login,
)
from app.identity_auth.security import PasswordHasher, generate_totp_secret
from app.identity_auth.session_store import SessionData

pytestmark = [pytest.mark.api, pytest.mark.auth, pytest.mark.integration]


class _MockSessionStore:
    """In-memory session store for testing (no Redis dependency)."""

    def __init__(self) -> None:
        self.sessions: dict[str, SessionData] = {}

    async def create(
        self, session_id: uuid.UUID, user_id: uuid.UUID, identity_type: str, clock: Clock
    ) -> SessionData:
        data = SessionData(
            session_id=str(session_id),
            user_id=user_id,
            identity_type=identity_type,
            csrf_token="mock-csrf-token",
            absolute_expires_at=clock.now() + timedelta(hours=8),
            idle_expires_at=clock.now() + timedelta(minutes=30),
        )
        self.sessions[str(session_id)] = data
        return data

    async def read(self, session_id: str) -> SessionData | None:
        return self.sessions.get(session_id)

    async def touch(self, session_id: str, clock: Clock) -> SessionData | None:
        return self.sessions.get(session_id)

    async def revoke(self, session_id: str) -> None:
        self.sessions.pop(session_id, None)


async def _create_local_credentials(
    session: AsyncSession, user_id: uuid.UUID, email: str, password: str
) -> None:
    """Helper: create a local_credentials row for a test user."""
    hasher = PasswordHasher()
    password_hash = hasher.hash_password(password)

    await session.execute(
        text(
            "INSERT INTO local_credentials (id, user_id, password_hash, algorithm, failed_attempts, "
            "password_changed_at) "
            "VALUES (:id, :user_id, :hash, 'argon2id', 0, :now)"
        ),
        {
            "id": uuid.uuid4(),
            "user_id": user_id,
            "hash": password_hash,
            "now": datetime.now(UTC),
        },
    )
    await session.flush()


async def _create_local_credentials_with_totp(
    session: AsyncSession, user_id: uuid.UUID, email: str, password: str
) -> str:
    """Helper: create a local_credentials row with TOTP enabled; returns the secret."""
    hasher = PasswordHasher()
    password_hash = hasher.hash_password(password)
    secret = generate_totp_secret()

    await session.execute(
        text(
            "INSERT INTO local_credentials (id, user_id, password_hash, algorithm, failed_attempts, "
            "password_changed_at, totp_enabled, totp_secret_encrypted) "
            "VALUES (:id, :user_id, :hash, 'argon2id', 0, :now, true, :secret)"
        ),
        {
            "id": uuid.uuid4(),
            "user_id": user_id,
            "hash": password_hash,
            "now": datetime.now(UTC),
            "secret": secret,
        },
    )
    await session.flush()
    return secret


@pytest.mark.asyncio
async def test_wrong_totp_codes_count_toward_lockout(session: AsyncSession, clock: FakeClock) -> None:
    """An attacker who already knows the password must not get unlimited guesses against the
    six-digit TOTP code — invalid TOTP attempts must count toward the same failed_attempts
    lockout counter as wrong passwords (found in review: this branch only wrote an audit event
    and never touched failed_attempts/locked_until)."""
    user_id = uuid.uuid4()
    email = "totp-lockout@example.com"
    password = "CorrectPassword123!"

    await session.execute(
        text(
            "INSERT INTO users (id, identity_type, display_name, email) "
            "VALUES (:id, 'LOCAL', 'Test User', :email)"
        ),
        {"id": user_id, "email": email},
    )
    await _create_local_credentials_with_totp(session, user_id, email, password)
    await session.commit()

    store = _MockSessionStore()
    settings_lockout_threshold = 10

    for attempt in range(1, settings_lockout_threshold):
        with pytest.raises(TotpInvalidError):
            await local_login(session, store, clock, email=email, password=password, totp_code="000000")
        await session.commit()

        result = await session.execute(
            text("SELECT failed_attempts, locked_until FROM local_credentials WHERE user_id = :uid"),
            {"uid": user_id},
        )
        row = result.one()
        assert row.failed_attempts == attempt
        assert row.locked_until is None

    with pytest.raises(AccountLockedError):
        await local_login(session, store, clock, email=email, password=password, totp_code="000000")
    await session.commit()

    result = await session.execute(
        text("SELECT failed_attempts, locked_until FROM local_credentials WHERE user_id = :uid"),
        {"uid": user_id},
    )
    row = result.one()
    assert row.failed_attempts == settings_lockout_threshold
    assert row.locked_until is not None

    # Even the correct TOTP code is now rejected — the account is locked, not just the code.
    with pytest.raises(AccountLockedError):
        await local_login(session, store, clock, email=email, password=password, totp_code="000000")


@pytest.mark.asyncio
async def test_ten_failed_attempts_within_window_triggers_lockout_on_eleventh(
    session: AsyncSession, clock: FakeClock
) -> None:
    """After 10 failed login attempts within lockout_window_minutes, the 10th failure itself
    locks the account (expiry measured from that triggering failure, not a later request that
    merely re-observes an already-stale count — found in review), and the 11th attempt (even
    with correct password) raises AccountLockedError."""
    user_id = uuid.uuid4()
    email = "test@example.com"
    password = "CorrectPassword123!"

    # Create user and local_credentials row
    await session.execute(
        text(
            "INSERT INTO users (id, identity_type, display_name, email) "
            "VALUES (:id, 'LOCAL', 'Test User', :email)"
        ),
        {"id": user_id, "email": email},
    )
    await _create_local_credentials(session, user_id, email, password)
    await session.commit()

    store = _MockSessionStore()
    settings_lockout_threshold = 10

    # Attempts 1-9 are plain failures; the 10th crosses the threshold and locks immediately.
    for attempt in range(1, settings_lockout_threshold):
        with pytest.raises(InvalidCredentialsError):
            await local_login(
                session,
                store,
                clock,
                email=email,
                password="WrongPassword123!",
                totp_code=None,
            )
        await session.commit()

        # Verify failed_attempts was incremented
        result = await session.execute(
            text("SELECT failed_attempts, locked_until FROM local_credentials WHERE user_id = :uid"),
            {"uid": user_id},
        )
        row = result.one()
        assert row.failed_attempts == attempt
        assert row.locked_until is None

    with pytest.raises(AccountLockedError):
        await local_login(session, store, clock, email=email, password="WrongPassword123!", totp_code=None)
    await session.commit()

    # Verify the 10th failure both incremented the count and locked the account then and there.
    result = await session.execute(
        text("SELECT failed_attempts, locked_until FROM local_credentials WHERE user_id = :uid"),
        {"uid": user_id},
    )
    row = result.one()
    assert row.failed_attempts == settings_lockout_threshold
    assert row.locked_until is not None

    # 11th attempt with CORRECT password should still be locked
    with pytest.raises(AccountLockedError) as exc_info:
        await local_login(session, store, clock, email=email, password=password, totp_code=None)

    assert exc_info.value.details.get("locked_until") is not None


@pytest.mark.asyncio
async def test_lockout_clears_when_clock_advances_past_locked_until(
    session: AsyncSession, clock: FakeClock
) -> None:
    """After AccountLockedError is triggered, advancing FakeClock past locked_until
    allows a successful login with correct password."""
    user_id = uuid.uuid4()
    email = "test@example.com"
    password = "CorrectPassword123!"

    # Create user and local_credentials row
    await session.execute(
        text(
            "INSERT INTO users (id, identity_type, display_name, email) "
            "VALUES (:id, 'LOCAL', 'Test User', :email)"
        ),
        {"id": user_id, "email": email},
    )
    await _create_local_credentials(session, user_id, email, password)
    await session.commit()

    store = _MockSessionStore()
    settings_lockout_threshold = 10
    settings_lockout_duration_minutes = 30

    # Trigger lockout with 11 wrong attempts
    for _ in range(settings_lockout_threshold + 1):
        try:
            await local_login(
                session,
                store,
                clock,
                email=email,
                password="WrongPassword123!",
                totp_code=None,
            )
        except (InvalidCredentialsError, AccountLockedError):
            pass
        await session.commit()

    # Verify locked_until is set
    result = await session.execute(
        text("SELECT locked_until FROM local_credentials WHERE user_id = :uid"),
        {"uid": user_id},
    )
    locked_until = result.one().locked_until
    assert locked_until is not None

    # Attempt login while still locked should fail
    with pytest.raises(AccountLockedError):
        await local_login(session, store, clock, email=email, password=password, totp_code=None)

    # Advance clock past locked_until
    clock.advance(minutes=settings_lockout_duration_minutes + 1)

    # Attempt login should now succeed
    result = await local_login(session, store, clock, email=email, password=password, totp_code=None)
    assert result.session_id is not None
    assert result.user_id == user_id


@pytest.mark.asyncio
async def test_successful_login_resets_failed_attempts(session: AsyncSession, clock: FakeClock) -> None:
    """A successful login with correct password resets failed_attempts to 0."""
    user_id = uuid.uuid4()
    email = "test@example.com"
    password = "CorrectPassword123!"

    # Create user and local_credentials row
    await session.execute(
        text(
            "INSERT INTO users (id, identity_type, display_name, email) "
            "VALUES (:id, 'LOCAL', 'Test User', :email)"
        ),
        {"id": user_id, "email": email},
    )
    await _create_local_credentials(session, user_id, email, password)
    await session.commit()

    store = _MockSessionStore()

    # Trigger some failed attempts (but not enough to lock)
    for _ in range(3):
        try:
            await local_login(
                session,
                store,
                clock,
                email=email,
                password="WrongPassword123!",
                totp_code=None,
            )
        except InvalidCredentialsError:
            pass
        await session.commit()

    # Verify failed_attempts == 3
    result = await session.execute(
        text("SELECT failed_attempts FROM local_credentials WHERE user_id = :uid"),
        {"uid": user_id},
    )
    assert result.one().failed_attempts == 3

    # Login with correct password
    login_result = await local_login(session, store, clock, email=email, password=password, totp_code=None)
    assert login_result.user_id == user_id
    await session.commit()

    # Verify failed_attempts reset to 0
    result = await session.execute(
        text("SELECT failed_attempts FROM local_credentials WHERE user_id = :uid"),
        {"uid": user_id},
    )
    assert result.one().failed_attempts == 0


@pytest.mark.asyncio
async def test_concurrent_failed_logins_do_not_lose_attempts(engine: AsyncEngine) -> None:
    """Two simultaneous wrong-password login attempts against the same local_credentials row
    must both be reflected in failed_attempts (no lost update).

    Uses two independent engine connections + asyncio.gather to ensure real concurrency,
    similar to test_concurrent_expired_key_reset_is_not_a_double_execution."""
    user_id = uuid.uuid4()
    email = "concurrent@example.com"
    password = "CorrectPassword123!"

    # Set up: create user and local_credentials row
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, identity_type, display_name, email) "
                "VALUES (:id, 'LOCAL', 'Test User', :email)"
            ),
            {"id": user_id, "email": email},
        )
        hasher = PasswordHasher()
        password_hash = hasher.hash_password(password)
        await conn.execute(
            text(
                "INSERT INTO local_credentials (id, user_id, password_hash, algorithm, failed_attempts, "
                "password_changed_at) "
                "VALUES (:id, :user_id, :hash, 'argon2id', 0, :now)"
            ),
            {
                "id": uuid.uuid4(),
                "user_id": user_id,
                "hash": password_hash,
                "now": datetime.now(UTC),
            },
        )

    store = _MockSessionStore()
    clock = FakeClock()

    async def _racer(attempt_num: int, *, ready: asyncio.Event | None = None, hold: float = 0.0) -> None:
        """Racer task: attempt login, signal ready, optionally hold the transaction open."""
        conn = await engine.connect()
        try:
            sess = AsyncSession(bind=conn, expire_on_commit=False)
            try:
                await local_login(
                    sess,
                    store,
                    clock,
                    email=email,
                    password="WrongPassword123!",
                    totp_code=None,
                )
            except InvalidCredentialsError:
                # Expected: wrong password
                pass

            if ready is not None:
                ready.set()
            if hold:
                await asyncio.sleep(hold)
            await sess.commit()
        finally:
            await conn.close()

    # Run two concurrent failed login attempts with synchronization via asyncio.Event
    try:
        racer1_ready = asyncio.Event()
        racer1_task = asyncio.create_task(_racer(1, ready=racer1_ready, hold=0.1))
        # Wait for racer1 to read/reset the row, then let racer2 race in
        await racer1_ready.wait()
        racer2_task = asyncio.create_task(_racer(2))
        await asyncio.gather(racer1_task, racer2_task)

        # Verify both failed attempts were recorded (no lost update). Must run
        # BEFORE cleanup below, not after — the finally block deletes these rows.
        async with engine.connect() as conn:
            result = await conn.execute(
                text("SELECT failed_attempts FROM local_credentials WHERE user_id = :uid"),
                {"uid": user_id},
            )
            row = result.one_or_none()
            assert row is not None, "local_credentials row should exist"
            assert row.failed_attempts == 2, f"Expected failed_attempts == 2, got {row.failed_attempts}"
    finally:
        # Cleanup
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM local_credentials WHERE user_id = :uid"),
                {"uid": user_id},
            )
            await conn.execute(
                text("DELETE FROM users WHERE id = :id"),
                {"id": user_id},
            )


@pytest.mark.asyncio
async def test_lockout_is_committed_not_just_flushed(engine: AsyncEngine) -> None:
    """The lockout branch must commit `locked_until` before raising — a request-scoped session
    with no auto-commit (the real FastAPI dependency) would otherwise roll it back on the
    exception, and every later request would re-enter the lockout branch forever (found in
    review: `flush()` was used where `commit()` was needed). Uses a fresh connection per attempt
    to simulate real request boundaries, not the single shared test session."""
    user_id = uuid.uuid4()
    email = "commit-check@example.com"
    password = "CorrectPassword123!"
    clock = FakeClock()
    store = _MockSessionStore()

    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, identity_type, display_name, email) "
                "VALUES (:id, 'LOCAL', 'Test User', :email)"
            ),
            {"id": user_id, "email": email},
        )
    async with engine.begin() as conn:
        sess = AsyncSession(bind=conn, expire_on_commit=False)
        await _create_local_credentials(sess, user_id, email, password)
        await sess.commit()

    try:
        # 11 failed attempts, each on its own connection/session (a real request boundary).
        for _ in range(11):
            conn = await engine.connect()
            try:
                sess = AsyncSession(bind=conn, expire_on_commit=False)
                with pytest.raises((InvalidCredentialsError, AccountLockedError)):
                    await local_login(
                        sess, store, clock, email=email, password="WrongPassword123!", totp_code=None
                    )
            finally:
                await conn.close()

        # A brand-new connection must see locked_until actually persisted.
        async with engine.connect() as conn:
            result = await conn.execute(
                text("SELECT locked_until FROM local_credentials WHERE user_id = :uid"), {"uid": user_id}
            )
            row = result.one()
            assert row.locked_until is not None, "locked_until must be committed, not just flushed"
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM local_credentials WHERE user_id = :uid"), {"uid": user_id})
            await conn.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})

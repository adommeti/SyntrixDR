from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import write_audit
from app.core.clock import Clock
from app.core.config import get_settings
from app.core.errors import AppError
from app.identity_auth.mailer import send_password_reset_email
from app.identity_auth.models import LocalCredential, PasswordResetToken, ReauthGrant, SessionRecord
from app.identity_auth.security import PasswordHasher, validate_password_policy, verify_totp_code
from app.identity_auth.session_store import SessionData, SessionStore


class InvalidCredentialsError(AppError):
    code = "INVALID_CREDENTIALS"
    status_code = 401

    def __init__(self) -> None:
        super().__init__("Invalid email or password.")


class AccountLockedError(AppError):
    code = "ACCOUNT_LOCKED"
    status_code = 401

    def __init__(self, locked_until: str) -> None:
        super().__init__(
            "Account is locked due to too many failed login attempts.", details={"locked_until": locked_until}
        )


class TotpRequiredError(AppError):
    code = "TOTP_REQUIRED"
    status_code = 401

    def __init__(self) -> None:
        super().__init__("TOTP code is required for this account.")


class TotpInvalidError(AppError):
    code = "TOTP_INVALID"
    status_code = 401

    def __init__(self) -> None:
        super().__init__("TOTP code is invalid.")


class TotpAlreadyEnabledError(AppError):
    code = "TOTP_ALREADY_ENABLED"
    status_code = 409

    def __init__(self) -> None:
        super().__init__(
            "TOTP is already enabled on this account. Disabling or replacing it is not yet"
            " supported by this endpoint."
        )


class PasswordResetTokenInvalidError(AppError):
    code = "PASSWORD_RESET_TOKEN_INVALID"
    status_code = 400

    def __init__(self) -> None:
        super().__init__("Password reset token is invalid, expired, or already used.")


@dataclass
class LoginResult:
    """Result of a successful login."""

    session_id: str
    csrf_token: str
    user_id: uuid.UUID


@dataclass
class ReauthResult:
    """Result of a successful re-authentication."""

    granted_at: str
    expires_at: str


@dataclass
class TotpEnrolResult:
    """Result of initiating TOTP enrollment."""

    secret: str
    provisioning_uri: str


async def local_login(
    session: AsyncSession,
    store: SessionStore,
    clock: Clock,
    *,
    email: str,
    password: str,
    totp_code: str | None = None,
) -> LoginResult:
    """Authenticate a user with email and password.

    Handles account lockout, TOTP verification, and session creation.

    Args:
        session: AsyncSession for DB queries
        store: SessionStore for session creation
        clock: Clock instance for timestamps
        email: User email (case-insensitive lookup)
        password: Plaintext password
        totp_code: Optional TOTP code if user has TOTP enabled

    Returns:
        LoginResult with session_id and csrf_token on success

    Raises:
        InvalidCredentialsError: Email not found, no local credential, or password mismatch
        AccountLockedError: Account is locked
        TotpRequiredError: TOTP enabled but code not provided
        TotpInvalidError: TOTP code is invalid
    """
    from app.users_teams_org.queries import find_local_user_id_by_email

    user_id = await find_local_user_id_by_email(session, email)

    if user_id is None:
        # No enumeration: same error as wrong password
        raise InvalidCredentialsError()

    # Load local credentials. FOR UPDATE: two concurrent failed logins racing on the same
    # row must not lose an update (issue class fixed once already in core/idempotency.py —
    # a plain SELECT then ORM read-modify-write UPDATE with no lock lets the loser overwrite
    # the winner's committed increment with its own stale computed value).
    result = await session.execute(
        select(LocalCredential).where(LocalCredential.user_id == user_id).with_for_update()
    )
    cred = result.scalar_one_or_none()
    if cred is None:
        raise InvalidCredentialsError()

    settings = get_settings()
    now = clock.now()

    # Check if locked (by previous lockout or by checking threshold)
    if cred.locked_until and cred.locked_until > now:
        raise AccountLockedError(cred.locked_until.isoformat())

    # If lockout has expired, reset failed_attempts
    if cred.locked_until and cred.locked_until <= now:
        cred.failed_attempts = 0
        cred.locked_until = None
        await session.flush()

    # If failed_attempts has reached threshold, lock the account now
    if cred.failed_attempts >= settings.lockout_threshold:
        locked_until = now + timedelta(minutes=settings.lockout_duration_minutes)
        cred.locked_until = locked_until
        await write_audit(
            session,
            actor_user_id=None,
            entity_type="USER",
            entity_id=user_id,
            action="AUTH_ACCOUNT_LOCKED",
            metadata={"reason": "failed_attempts_exceeded"},
        )
        # Must commit before raising: the request-scoped session has no auto-commit, so an
        # exception propagating out of the route would roll back locked_until, and every
        # subsequent request would re-enter this same branch forever (lockout that never
        # expires, since the reset-on-expiry branch above never has a real locked_until to
        # compare against). Found in review.
        await session.commit()
        raise AccountLockedError(locked_until.isoformat())

    # Verify password
    hasher = PasswordHasher()
    if not hasher.verify_password(password, cred.password_hash):
        # D-235: 10 failures/15 min. `updated_at` doubles as "last failure time" (bumped on
        # every increment below) since the frozen schema has no dedicated window-start column.
        # A failure outside the window starts a fresh count instead of accumulating forever.
        window = timedelta(minutes=settings.lockout_window_minutes)
        if cred.failed_attempts > 0 and (now - cred.updated_at) > window:
            cred.failed_attempts = 1
        else:
            cred.failed_attempts += 1
        cred.updated_at = now

        await session.flush()
        await write_audit(
            session,
            actor_user_id=None,
            entity_type="USER",
            entity_id=user_id,
            action="AUTH_LOGIN_LOCAL_FAILURE",
            metadata={"failed_attempts": cred.failed_attempts},
        )
        await session.commit()
        raise InvalidCredentialsError()

    # Password is correct; check TOTP if enabled
    if cred.totp_enabled:
        if totp_code is None:
            await session.commit()
            raise TotpRequiredError()

        if not verify_totp_code(cred.totp_secret_encrypted or "", totp_code):
            await write_audit(
                session,
                actor_user_id=None,
                entity_type="USER",
                entity_id=user_id,
                action="AUTH_TOTP_FAILED",
            )
            await session.commit()
            raise TotpInvalidError()

    # Success: reset failed attempts, create session
    cred.failed_attempts = 0
    cred.locked_until = None

    # Create durable session record
    session_id = uuid.uuid4()
    absolute_expires_at = now + timedelta(hours=settings.session_absolute_hours)
    idle_expires_at = now + timedelta(minutes=settings.session_idle_minutes)

    session_record = SessionRecord(
        id=session_id,
        user_id=user_id,
        identity_type="LOCAL",
        created_at=now,
        last_seen_at=now,
        absolute_expires_at=absolute_expires_at,
        idle_expires_at=idle_expires_at,
    )
    session.add(session_record)
    await session.flush()

    # Create Redis session
    session_data = await store.create(session_id, user_id, "LOCAL", clock)

    await write_audit(
        session,
        actor_user_id=user_id,
        entity_type="SESSION",
        entity_id=session_id,
        action="AUTH_LOGIN_LOCAL_SUCCESS",
    )
    await session.commit()

    return LoginResult(
        session_id=session_data.session_id, csrf_token=session_data.csrf_token, user_id=user_id
    )


async def entra_callback(
    session: AsyncSession,
    store: SessionStore,
    clock: Clock,
    *,
    entra_object_id: str,
    email: str,
    display_name: str,
) -> LoginResult:
    """Handle ENTRA OIDC callback; find or create user and create session.

    Args:
        session: AsyncSession for DB operations
        store: SessionStore for session creation
        clock: Clock instance
        entra_object_id: Entra unique identifier (sub claim)
        email: User email
        display_name: User display name

    Returns:
        LoginResult with session_id and csrf_token
    """
    from app.users_teams_org.queries import find_entra_user_id_by_object_id, is_user_active

    # Find or create user with identity_type='ENTRA' keyed by entra_object_id
    user_id = await find_entra_user_id_by_object_id(session, entra_object_id)

    if user_id is not None and not await is_user_active(session, user_id):
        # find_entra_user_id_by_object_id deliberately doesn't filter is_active (find-or-create
        # semantics) — login itself must still reject a deactivated account here. Found in
        # review: a deactivated Entra admin could otherwise log in and keep their roles.
        raise InvalidCredentialsError()

    if user_id is None:
        # Minimal user provisioning; create new user (full profile sync is users_teams_org's job)
        user_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO users (id, identity_type, display_name, email, entra_object_id, is_active) "
                "VALUES (:id, 'ENTRA', :display_name, :email, :entra_object_id, true)"
            ),
            {
                "id": user_id,
                "display_name": display_name,
                "email": email,
                "entra_object_id": entra_object_id,
            },
        )
        await session.flush()

    settings = get_settings()
    now = clock.now()

    # Create durable session record
    session_id = uuid.uuid4()
    absolute_expires_at = now + timedelta(hours=settings.session_absolute_hours)
    idle_expires_at = now + timedelta(minutes=settings.session_idle_minutes)

    session_record = SessionRecord(
        id=session_id,
        user_id=user_id,
        identity_type="ENTRA",
        created_at=now,
        last_seen_at=now,
        absolute_expires_at=absolute_expires_at,
        idle_expires_at=idle_expires_at,
    )
    session.add(session_record)
    await session.flush()

    # Create Redis session
    session_data = await store.create(session_id, user_id, "ENTRA", clock)

    await write_audit(
        session,
        actor_user_id=user_id,
        entity_type="USER",
        entity_id=user_id,
        action="AUTH_LOGIN_ENTRA",
    )
    await session.commit()

    return LoginResult(
        session_id=session_data.session_id, csrf_token=session_data.csrf_token, user_id=user_id
    )


async def logout(
    session: AsyncSession,
    store: SessionStore,
    *,
    session_data: SessionData,
) -> None:
    """Log out a user by revoking their session.

    Args:
        session: AsyncSession for DB updates
        store: SessionStore for revocation
        session_data: The current session data
    """
    now = datetime.now()

    # Revoke from Redis
    await store.revoke(session_data.session_id)

    # Mark as revoked in DB
    session_id_uuid = uuid.UUID(session_data.session_id)
    session_record = await session.get(SessionRecord, session_id_uuid)
    if session_record:
        session_record.revoked_at = now
        session_record.revoked_reason = "LOGOUT"
        await session.flush()

        await write_audit(
            session,
            actor_user_id=session_data.user_id,
            entity_type="SESSION",
            entity_id=session_id_uuid,
            action="AUTH_LOGOUT",
        )

    await session.commit()


async def reauth(
    session: AsyncSession,
    store: SessionStore,
    clock: Clock,
    *,
    session_data: SessionData,
    method: str,
    password: str | None = None,
    totp_code: str | None = None,
) -> ReauthResult:
    """Grant a short-lived re-authentication grant for sensitive operations.

    Args:
        session: AsyncSession for DB operations
        store: SessionStore (unused in this operation)
        clock: Clock instance
        session_data: Current session data
        method: 'PASSWORD', 'TOTP', or 'ENTRA' (trusted assertion for Entra users)
        password: Required for PASSWORD method
        totp_code: Required for TOTP method

    Returns:
        ReauthResult with grant timestamps

    Raises:
        InvalidCredentialsError: Password verification failed
        TotpInvalidError: TOTP code verification failed
        NotImplementedError: Method not yet implemented (Entra step-up deferred)
    """

    settings = get_settings()
    now = clock.now()
    session_id_uuid = uuid.UUID(session_data.session_id)

    # Verify method and credentials
    if method == "PASSWORD":
        if password is None:
            raise InvalidCredentialsError()

        result = await session.execute(
            select(LocalCredential).where(LocalCredential.user_id == session_data.user_id)
        )
        cred = result.scalar_one_or_none()
        if cred is None or not PasswordHasher().verify_password(password, cred.password_hash):
            await write_audit(
                session,
                actor_user_id=session_data.user_id,
                entity_type="SESSION",
                entity_id=session_id_uuid,
                action="AUTH_REAUTH_FAILED",
                metadata={"method": method},
            )
            await session.commit()
            raise InvalidCredentialsError()

    elif method == "TOTP":
        if totp_code is None:
            raise TotpInvalidError()

        result = await session.execute(
            select(LocalCredential).where(LocalCredential.user_id == session_data.user_id)
        )
        cred = result.scalar_one_or_none()
        if cred is None or not cred.totp_enabled:
            raise TotpInvalidError()

        if not verify_totp_code(cred.totp_secret_encrypted or "", totp_code):
            await write_audit(
                session,
                actor_user_id=session_data.user_id,
                entity_type="SESSION",
                entity_id=session_id_uuid,
                action="AUTH_REAUTH_FAILED",
                metadata={"method": method},
            )
            await session.commit()
            raise TotpInvalidError()

    else:
        # 'ENTRA' step-up (D-235: "Entra prompt") requires a real, fresh redirect-based
        # re-authentication against Entra — this session builds no such flow. Accepting the
        # claim at face value here would let any caller (including a LOCAL user) submit
        # method='ENTRA' and receive a privileged reauth grant with zero verification, a real
        # authorization bypass caught in review. Reject rather than fake-accept; a real
        # Entra step-up prompt is a follow-up, not this endpoint silently trusting the client.
        raise InvalidCredentialsError()

    # Create re-auth grant
    expires_at = now + timedelta(minutes=settings.reauth_window_minutes)
    grant = ReauthGrant(
        session_id=uuid.UUID(session_data.session_id),
        user_id=session_data.user_id,
        method=method,
        granted_at=now,
        expires_at=expires_at,
    )
    session.add(grant)
    await session.flush()

    await write_audit(
        session,
        actor_user_id=session_data.user_id,
        entity_type="SESSION",
        entity_id=session_id_uuid,
        action="AUTH_REAUTH_GRANTED",
        metadata={"method": method},
    )
    await session.commit()

    return ReauthResult(granted_at=now.isoformat(), expires_at=expires_at.isoformat())


async def request_password_reset(
    session: AsyncSession,
    clock: Clock,
    *,
    email: str,
) -> None:
    """Request a password reset. Always returns success (no enumeration).

    Args:
        session: AsyncSession for DB operations
        clock: Clock instance
        email: Email address (may not exist)
    """
    from app.users_teams_org.queries import find_local_user_id_by_email

    settings = get_settings()
    now = clock.now()

    # Find LOCAL user by email
    user_id = await find_local_user_id_by_email(session, email)

    if user_id:
        # Generate token
        plaintext_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(plaintext_token.encode()).hexdigest()
        expires_at = now + timedelta(minutes=settings.password_reset_token_ttl_minutes)

        token_record = PasswordResetToken(
            user_id=user_id,
            token_hash=token_hash,
            expires_at=expires_at,
        )
        session.add(token_record)
        await session.flush()

        # Send email with plaintext token
        await send_password_reset_email(email, plaintext_token)

        await write_audit(
            session,
            actor_user_id=None,
            entity_type="USER",
            entity_id=user_id,
            action="AUTH_PASSWORD_RESET_REQUESTED",
        )

    # Always return 202, even if email doesn't exist (no enumeration)
    await session.commit()


async def confirm_password_reset(
    session: AsyncSession,
    clock: Clock,
    *,
    token: str,
    new_password: str,
) -> None:
    """Confirm password reset with a valid token and new password.

    Args:
        session: AsyncSession for DB operations
        clock: Clock instance
        token: Plaintext password reset token
        new_password: New password (will be validated against policy)

    Raises:
        PasswordResetTokenInvalidError: Token invalid, expired, or already used
        PasswordPolicyError: New password violates policy
    """
    now = clock.now()

    # Hash token and look up. FOR UPDATE: two concurrent confirmations of the same token could
    # both read used_at=None before either commits and both succeed, defeating single-use (found
    # in review) — same race class as core/idempotency.py's fresh-key path, fixed the same way.
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    result = await session.execute(
        select(PasswordResetToken).where(PasswordResetToken.token_hash == token_hash).with_for_update()
    )
    token_record = result.scalar_one_or_none()

    if token_record is None or token_record.used_at is not None or token_record.expires_at <= now:
        raise PasswordResetTokenInvalidError()

    # Validate new password
    validate_password_policy(new_password)

    # Update password and reset lockout
    user_id = token_record.user_id
    result = await session.execute(select(LocalCredential).where(LocalCredential.user_id == user_id))
    cred = result.scalar_one_or_none()
    if cred:
        hasher = PasswordHasher()
        cred.password_hash = hasher.hash_password(new_password)
        cred.password_changed_at = now
        cred.failed_attempts = 0
        cred.locked_until = None
        await session.flush()

    # Mark token as used
    token_record.used_at = now
    await session.flush()

    await write_audit(
        session,
        actor_user_id=user_id,
        entity_type="USER",
        entity_id=user_id,
        action="AUTH_PASSWORD_RESET_CONFIRMED",
    )
    await session.commit()


async def enrol_totp(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
) -> TotpEnrolResult:
    """Generate and store a TOTP secret for a user (not yet enabled).

    Args:
        session: AsyncSession for DB operations
        user_id: UUID of the user

    Returns:
        TotpEnrolResult with secret and provisioning URI for QR code

    Raises:
        InvalidCredentialsError: User has no local credentials
    """
    from app.identity_auth.security import generate_totp_secret, totp_provisioning_uri
    from app.users_teams_org.queries import get_user_email

    result = await session.execute(select(LocalCredential).where(LocalCredential.user_id == user_id))
    cred = result.scalar_one_or_none()
    if cred is None:
        raise InvalidCredentialsError()
    if cred.totp_enabled:
        # Re-enrolling while already enabled would silently overwrite and disable the active
        # factor with no verification of the caller's continued possession of it — a hijacked
        # session could strip 2FA this way. Found in review; the route also now requires a fresh
        # reauth grant, but the command rejects this independently of route wiring.
        raise TotpAlreadyEnabledError()

    # Get user email for provisioning URI
    email = await get_user_email(session, user_id)

    secret = generate_totp_secret()
    provisioning_uri = totp_provisioning_uri(secret, email or "unknown@example.com")

    # Store encrypted secret (in this session, plaintext only; real encryption via KMS is out of scope)
    cred.totp_secret_encrypted = secret
    cred.totp_enabled = False
    await session.flush()

    await write_audit(
        session,
        actor_user_id=user_id,
        entity_type="USER",
        entity_id=user_id,
        action="AUTH_TOTP_ENROLLED",
    )
    await session.commit()

    return TotpEnrolResult(secret=secret, provisioning_uri=provisioning_uri)


async def verify_totp(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    code: str,
) -> None:
    """Verify a TOTP code and enable TOTP for the user.

    Args:
        session: AsyncSession for DB operations
        user_id: UUID of the user
        code: 6-digit TOTP code

    Raises:
        TotpInvalidError: Code is invalid or TOTP not enrolled
    """
    result = await session.execute(select(LocalCredential).where(LocalCredential.user_id == user_id))
    cred = result.scalar_one_or_none()
    if cred is None or cred.totp_secret_encrypted is None or cred.totp_enabled:
        raise TotpInvalidError()

    if not verify_totp_code(cred.totp_secret_encrypted, code):
        await write_audit(
            session,
            actor_user_id=user_id,
            entity_type="USER",
            entity_id=user_id,
            action="AUTH_TOTP_FAILED",
        )
        await session.commit()
        raise TotpInvalidError()

    cred.totp_enabled = True
    await session.flush()

    await write_audit(
        session,
        actor_user_id=user_id,
        entity_type="USER",
        entity_id=user_id,
        action="AUTH_TOTP_VERIFIED",
    )
    await session.commit()

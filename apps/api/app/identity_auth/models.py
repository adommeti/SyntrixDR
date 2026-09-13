from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Text, text
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def _now_utc() -> datetime:
    """Return the current UTC datetime."""
    return datetime.now(UTC)


class LocalCredential(Base):
    """Stores password hashes and TOTP secrets for LOCAL identity users."""

    __tablename__ = "local_credentials"

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    algorithm: Mapped[str] = mapped_column(Text, nullable=False, default="argon2id")
    failed_attempts: Mapped[int] = mapped_column(nullable=False, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    password_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now_utc
    )
    totp_secret_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    totp_enabled: Mapped[bool] = mapped_column(nullable=False, default=False)
    must_reset: Mapped[bool] = mapped_column(nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now_utc)


class PasswordResetToken(Base):
    """One-time-use tokens for password reset flow."""

    __tablename__ = "password_reset_tokens"

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now_utc)

    __table_args__ = (
        Index(
            "ix_password_reset_tokens_user_open",
            "user_id",
            "expires_at",
            postgresql_where=text("used_at IS NULL"),
        ),
    )


class SessionRecord(Base):
    """Durable session records (the other half lives in Redis)."""

    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    identity_type: Mapped[str] = mapped_column(
        Enum("ENTRA", "LOCAL", name="identity_type", native_enum=True, create_type=False), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now_utc)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now_utc)
    absolute_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    idle_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index(
            "ix_sessions_user_active",
            "user_id",
            text("last_seen_at DESC"),
            postgresql_where=text("revoked_at IS NULL"),
        ),
        Index("ix_sessions_absolute_expires", "absolute_expires_at"),
    )


class ReauthGrant(Base):
    """Short-lived grants for re-authentication (step-up) within a session."""

    __tablename__ = "reauth_grants"

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("sessions.id", ondelete="RESTRICT"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now_utc)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    method: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (Index("ix_reauth_grants_session", "session_id", text("expires_at DESC")),)


# The `role_assignments` partial-read-only stub from session a is gone — superseded by the real
# `RoleAssignment` model in `app.users_teams_org.models` and `app.users_teams_org.authorization`
# (session b). GLOBAL_ADMIN/TOTP checks go through `AuthorizationService`, not a local stub here.

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def _now_utc() -> datetime:
    """Return the current UTC datetime."""
    return datetime.now(UTC)


class User(Base):
    """Entra or Local identity (DATA_MODEL.md: "Entra or Local identity; manager/contact/avatar
    data; audit-safe deactivation")."""

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "(identity_type = 'ENTRA' AND entra_object_id IS NOT NULL) OR identity_type = 'LOCAL'",
            name="users_identity_ck",
        ),
        Index(
            "ux_users_email_active",
            text("lower(email)"),
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("ix_users_manager", "manager_user_id", postgresql_where=text("deleted_at IS NULL")),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    entra_object_id: Mapped[str | None] = mapped_column(Text, unique=True, nullable=True)
    identity_type: Mapped[str] = mapped_column(
        Enum("ENTRA", "LOCAL", name="identity_type", native_enum=True, create_type=False), nullable=False
    )
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str] = mapped_column(Text, nullable=False)
    job_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    manager_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    avatar_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    mascot_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(nullable=False, default=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now_utc)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Team(Base):
    """Accountability/resource grouping (DATA_MODEL.md)."""

    __tablename__ = "teams"
    __table_args__ = (
        Index(
            "ux_teams_name_active",
            text("lower(name)"),
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ux_teams_external_ref",
            "external_system",
            "external_id",
            unique=True,
            postgresql_where=text(
                "external_system IS NOT NULL AND external_id IS NOT NULL AND deleted_at IS NULL"
            ),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    manager_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    mascot_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    external_system: Mapped[str | None] = mapped_column(Text, nullable=True)
    external_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now_utc)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TeamMembership(Base):
    """User<->Team relationship (DATA_MODEL.md)."""

    __tablename__ = "team_memberships"
    __table_args__ = (
        CheckConstraint(
            "effective_to IS NULL OR effective_from IS NULL OR effective_to >= effective_from",
            name="team_membership_dates_ck",
        ),
        Index(
            "ux_team_memberships_active",
            "team_id",
            "user_id",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    team_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("teams.id", ondelete="RESTRICT"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    membership_role: Mapped[str | None] = mapped_column(Text, nullable=True)
    effective_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now_utc)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Skill(Base):
    """V1 capability tag; no proficiency score until V2 (DATA_MODEL.md, D-218)."""

    __tablename__ = "skills"
    __table_args__ = (Index("ux_skills_name", text("lower(name)"), unique=True),)

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now_utc)


class UserSkill(Base):
    """User<->Skill association. `proficiency` stays nullable/unused in V1 (D-218)."""

    __tablename__ = "user_skills"
    __table_args__ = (UniqueConstraint("user_id", "skill_id"),)

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    skill_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("skills.id", ondelete="RESTRICT"), nullable=False
    )
    proficiency: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now_utc)


class RoleAssignment(Base):
    """Scoped role grant (GLOBAL_ADMIN, DR_COORDINATOR, WORK_STREAM_LEAD, APP_OWNER, BUSINESS_OWNER,
    MANAGER, EXECUTOR, GLOBAL_READONLY) at GLOBAL/DR_EVENT/WORK_STREAM/APPLICATION scope (GLOSSARY.md,
    RBAC_MATRIX.md). Supersedes `identity_auth.models`'s partial-read-only stub from session a."""

    __tablename__ = "role_assignments"
    __table_args__ = (
        CheckConstraint(
            "(scope_type = 'GLOBAL' AND scope_id IS NULL) "
            "OR (scope_type <> 'GLOBAL' AND scope_id IS NOT NULL)",
            name="role_scope_ck",
        ),
        Index(
            "ix_role_assignments_active",
            "user_id",
            "role_key",
            "scope_type",
            "scope_id",
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    role_key: Mapped[str] = mapped_column(Text, nullable=False)
    scope_type: Mapped[str] = mapped_column(
        Enum(
            "GLOBAL",
            "DR_EVENT",
            "WORK_STREAM",
            "APPLICATION",
            name="role_scope_type",
            native_enum=True,
            create_type=False,
        ),
        nullable=False,
    )
    scope_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    granted_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now_utc)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

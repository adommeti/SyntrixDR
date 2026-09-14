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
    SmallInteger,
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


class Tier(Base):
    """SLA/RTO/health-weight defaults (DATA_MODEL.md "Tier defaults"). V1 has exactly 5 rows
    (TIER_0..TIER_4), seeded by `0001_schema_v1.py`; no create/delete in V1 (BUILD-03.plan.md
    Risk #3)."""

    __tablename__ = "tiers"

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    rank: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    default_sla_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    default_health_weight: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now_utc)


class Application(Base):
    """Application master data (DATA_MODEL.md). `tier_id` never NULL — every Application has a
    Tier from creation."""

    __tablename__ = "applications"
    __table_args__ = (
        Index(
            "ux_applications_name_active",
            text("lower(name)"),
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    tier_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("tiers.id", ondelete="RESTRICT"), nullable=False
    )
    external_system: Mapped[str | None] = mapped_column(Text, nullable=True)
    external_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now_utc)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ApplicationOwner(Base):
    """System/Application or Business owner slot (DATA_MODEL.md "Owner cardinality", D-254).
    `owner_order` 1-3 per `owner_type`; slot 1 is Primary."""

    __tablename__ = "application_owners"
    __table_args__ = (
        CheckConstraint("owner_order BETWEEN 1 AND 3", name="application_owner_order_ck"),
        UniqueConstraint("application_id", "owner_type", "owner_order", name="application_owner_slot_unique"),
        Index("ix_application_owners_user", "user_id", postgresql_where=text("deleted_at IS NULL")),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    application_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("applications.id", ondelete="RESTRICT"), nullable=False
    )
    owner_type: Mapped[str] = mapped_column(
        Enum(
            "SYSTEM_APPLICATION",
            "BUSINESS",
            name="application_owner_type",
            native_enum=True,
            create_type=False,
        ),
        nullable=False,
    )
    owner_order: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now_utc)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

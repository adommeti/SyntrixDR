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

from app.applications_catalog.models import Application, Tier
from app.core.database import Base
from app.users_teams_org.models import User

_ = (User, Application, Tier)  # registers cross-module FK targets on Base.metadata

#: D-222 auto-enrolment sources. The full DR Event entity (and TASK_ASSIGNEE/BLOCKER_OWNER/APP_OWNER/
#: BUSINESS_OWNER/OWNING_TEAM auto-enrol triggers) land with their owning modules in later BUILDs;
#: this session wires ROLE end-to-end (see app/dr_events/participants.py and Risk #2 in the plan).
PARTICIPANT_SOURCES = (
    "EXPLICIT",
    "ROLE",
    "TASK_ASSIGNEE",
    "BLOCKER_OWNER",
    "APP_OWNER",
    "BUSINESS_OWNER",
    "WORK_STREAM_LEAD",
    "OWNING_TEAM",
)


def _now_utc() -> datetime:
    """Return the current UTC datetime."""
    return datetime.now(UTC)


class DrEvent(Base):
    """Canonical DR Event (STATE_MACHINES.md §DR Event, DATA_MODEL.md). Lifecycle status is
    written only by `dr_events/transition_service.py::DrEventTransitionService`.

    `baseline_plan_version_id` FKs to `plan_versions.id` (schema_v1.sql adds it via a deferred
    `ALTER TABLE`, schema_v1.sql:241-242, since `plan_versions.dr_event_id` FKs back to
    `dr_events.id` — the two tables reference each other). The ORM can still declare this FK as
    a plain string target: SQLAlchemy resolves a string `ForeignKey` lazily at
    mapper-configuration time, not at class-body eval time, so there is no real import-order
    circularity here (found in `db-check`: omitting it entirely causes alembic's autogenerate
    diff to see it as a spurious DB-only constraint to drop)."""

    __tablename__ = "dr_events"
    __table_args__ = (
        CheckConstraint("status <> 'CANCELLED' OR cancel_reason IS NOT NULL", name="dr_events_cancel_ck"),
        Index("ix_dr_events_status", "status", postgresql_where=text("deleted_at IS NULL")),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    parent_dr_event_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("dr_events.id", ondelete="RESTRICT"), nullable=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    event_type: Mapped[str] = mapped_column(
        Enum("PLANNED_DR", "REAL_INCIDENT", name="dr_event_type", native_enum=True, create_type=False),
        nullable=False,
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        Enum(
            "PLANNED",
            "ACTIVE",
            "FAILOVER_IN_PROGRESS",
            "FAILED_OVER",
            "FAILBACK_IN_PROGRESS",
            "CLOSED",
            "CANCELLED",
            name="dr_event_status",
            native_enum=True,
            create_type=False,
        ),
        nullable=False,
        default="PLANNED",
    )
    coordinator_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    source_location: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_location: Mapped[str | None] = mapped_column(Text, nullable=True)
    planned_start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    event_timezone: Mapped[str] = mapped_column(Text, nullable=False, default="UTC")
    network_cut_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failback_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_control_profile: Mapped[str] = mapped_column(
        Enum(
            "CONSERVATIVE",
            "BALANCED",
            "FAST_EXECUTION",
            name="ai_control_profile",
            native_enum=True,
            create_type=False,
        ),
        nullable=False,
        default="CONSERVATIVE",
    )
    baseline_plan_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("plan_versions.id", ondelete="RESTRICT"), nullable=True
    )
    health_score: Mapped[Decimal | None] = mapped_column(Numeric(7, 4), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now_utc)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DrApplication(Base):
    """Event-scoped instance of an Application (DATA_MODEL.md). Lives in `dr_events` (no
    dedicated module owns it yet, BUILD-04.plan.md Risk #5); this session only writes its two
    bulk status side effects (`start_failover`/`start_failback`) plus creation at Event scoping.
    RTO/RPO computation and the App's own command surface (submit-validation/validate) are a
    later increment's scope."""

    __tablename__ = "dr_applications"
    __table_args__ = (
        UniqueConstraint("dr_event_id", "application_id"),
        Index(
            "ix_dr_applications_event_status",
            "dr_event_id",
            "status",
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    dr_event_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("dr_events.id", ondelete="RESTRICT"), nullable=False
    )
    application_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("applications.id", ondelete="RESTRICT"), nullable=False
    )
    effective_tier_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("tiers.id", ondelete="RESTRICT"), nullable=False
    )
    effective_sla_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    rto_target_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    rto_actual_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rto_passed: Mapped[bool | None] = mapped_column(nullable=True)
    rpo_target_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rpo_not_applicable: Mapped[bool] = mapped_column(nullable=False, default=False)
    rpo_reference_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rpo_recovered_data_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rpo_actual_loss_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rpo_passed: Mapped[bool | None] = mapped_column(nullable=True)
    failback_required: Mapped[bool] = mapped_column(nullable=False, default=True)
    status: Mapped[str] = mapped_column(
        Enum(
            "NOT_STARTED",
            "RECOVERING",
            "TECHNICAL_VALIDATION",
            "FAILED_OVER",
            "FAILBACK_IN_PROGRESS",
            "COMPLETED",
            name="dr_application_status",
            native_enum=True,
            create_type=False,
        ),
        nullable=False,
        default="NOT_STARTED",
    )
    sla_start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    technical_validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sla_breached_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    target_recovery_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    forecast_recovery_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failback_target_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failback_forecast_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    business_confirmed: Mapped[bool] = mapped_column(nullable=False, default=False)
    business_confirmation_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    health_band: Mapped[str | None] = mapped_column(
        Enum(
            "GREEN",
            "LIGHT_ORANGE",
            "DARK_ORANGE",
            "RED",
            name="health_band",
            native_enum=True,
            create_type=False,
        ),
        nullable=True,
    )
    health_score: Mapped[Decimal | None] = mapped_column(Numeric(7, 4), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now_utc)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DrEventParticipant(Base):
    """Explicit participant row per (dr_event, user, source) (D-222). Global Admins are implicit
    participants and are never materialized here — see `dr_events.participants.is_participant`."""

    __tablename__ = "dr_event_participants"
    __table_args__ = (
        CheckConstraint(f"source IN {PARTICIPANT_SOURCES!r}", name="dr_event_participants_source_ck"),
        Index(
            "ux_dr_event_participants_active",
            "dr_event_id",
            "user_id",
            "source",
            unique=True,
            postgresql_where=text("removed_at IS NULL"),
        ),
        Index("ix_dr_event_participants_user", "user_id", postgresql_where=text("removed_at IS NULL")),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    dr_event_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("dr_events.id", ondelete="RESTRICT"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    source: Mapped[str] = mapped_column(Text, nullable=False)
    added_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now_utc)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.applications_catalog.models import Application
from app.core.database import Base
from app.core.external_refs import milestones_table, task_dependencies_table, tasks_table

_ = (Application, tasks_table, task_dependencies_table, milestones_table)  # FK-resolution registration


def _now_utc() -> datetime:
    """Return the current UTC datetime."""
    return datetime.now(UTC)


class Plan(Base):
    """Reusable versioned template (DATA_MODEL.md:31, FROZEN_DECISIONS.md:86) — an Event
    instantiates/copies a Plan; live Event edits never mutate the source Plan."""

    __tablename__ = "plans"

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    plan_type: Mapped[str] = mapped_column(
        Enum("FAILOVER", "FAILBACK", "GENERAL", name="plan_type", native_enum=True, create_type=False),
        nullable=False,
    )
    application_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("applications.id", ondelete="RESTRICT"), nullable=True
    )
    source_type: Mapped[str] = mapped_column(
        Enum(
            "MANUAL",
            "EXCEL_IMPORT",
            "CLONE",
            "AI_ASSISTED",
            name="plan_source_type",
            native_enum=True,
            create_type=False,
        ),
        nullable=False,
        default="MANUAL",
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now_utc)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PlanVersion(Base):
    """A version of a Plan (`plan_id` set) or an Event-scoped instantiation/Baseline
    (`dr_event_id` set, `plan_id` NULL) — `plan_version_parent_ck` requires at least one.
    `version_type=BASELINE` is written only by `dr_events/transition_service.py::start_failover`,
    never accepted from a caller (see `plans_import/commands.py::create_plan_version`)."""

    __tablename__ = "plan_versions"
    __table_args__ = (
        CheckConstraint("plan_id IS NOT NULL OR dr_event_id IS NOT NULL", name="plan_version_parent_ck"),
        Index(
            "ux_plan_versions_plan_version",
            "plan_id",
            "version_number",
            unique=True,
            postgresql_where=text("plan_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    plan_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("plans.id", ondelete="RESTRICT"), nullable=True
    )
    # String FK target: `dr_events` is defined in the sibling `dr_events` module. SQLAlchemy
    # resolves a string ForeignKey lazily at mapper-configuration time, not at class-body eval
    # time, so this has no import-order dependency on `app.dr_events.models` (unlike a
    # `relationship()` by class reference, which would).
    dr_event_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("dr_events.id", ondelete="RESTRICT"), nullable=True
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    version_type: Mapped[str] = mapped_column(
        Enum(
            "DRAFT",
            "BASELINE",
            "EXECUTION",
            "FINAL",
            name="plan_version_type",
            native_enum=True,
            create_type=False,
        ),
        nullable=False,
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now_utc)


class PlanVersionTask(Base):
    """JSONB snapshot of one Task at `plan_version_id`'s point in time. `source_task_id` FKs to
    the live `tasks` table (owned by `tasks_dependencies`, not built yet) via the
    `core.external_refs.tasks_table` FK-resolution stub, `ON DELETE SET NULL` matching
    schema_v1.sql exactly — a deleted source Task doesn't invalidate the historical snapshot."""

    __tablename__ = "plan_version_tasks"
    __table_args__ = (UniqueConstraint("plan_version_id", "source_task_id"),)

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    plan_version_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("plan_versions.id", ondelete="RESTRICT"), nullable=False
    )
    source_task_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )
    snapshot_data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now_utc)


class PlanVersionTaskDependency(Base):
    """JSONB snapshot of one Task Dependency (see `PlanVersionTask` docstring for the
    FK-resolution-stub rationale)."""

    __tablename__ = "plan_version_task_dependencies"

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    plan_version_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("plan_versions.id", ondelete="RESTRICT"), nullable=False
    )
    source_dependency_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("task_dependencies.id", ondelete="SET NULL"), nullable=True
    )
    snapshot_data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now_utc)


class PlanVersionMilestone(Base):
    """JSONB snapshot of one Milestone (see `PlanVersionTask` docstring for the
    FK-resolution-stub rationale)."""

    __tablename__ = "plan_version_milestones"

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    plan_version_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("plan_versions.id", ondelete="RESTRICT"), nullable=False
    )
    source_milestone_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("milestones.id", ondelete="SET NULL"), nullable=True
    )
    snapshot_data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now_utc)

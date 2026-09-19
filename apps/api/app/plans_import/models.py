from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

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
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.applications_catalog.models import Application
from app.core.database import Base
from app.core.external_refs import milestones_table
from app.tasks_dependencies.models import Task, TaskDependency

# `Application`/`Task`/`TaskDependency` already have real ORM-mapped classes (unlike
# `milestones`, whose owning module doesn't exist yet and uses the `core/external_refs.py` stub
# pattern below) -- importing the class directly, not a stub, is correct here; see the longer
# rationale in `dr_events/models.py` for why this is a deliberate exception to
# `.claude/rules/python-api.md`'s cross-module rule, not an oversight.
_ = (Application, Task, TaskDependency, milestones_table)  # FK-resolution registration


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
    the live `tasks` table (`tasks_dependencies.models.Task`, BUILD-05), `ON DELETE SET NULL`
    matching schema_v1.sql exactly — a deleted source Task doesn't invalidate the historical
    snapshot."""

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


class ImportJob(Base):
    """Excel import job (schema_v1.sql:734-744, D-221). `status` is plain TEXT in the frozen
    schema (no DB enum) -- the value set (`PENDING`, `PARSED`, `PARSE_FAILED`, `ACCEPTED`) is
    defined in code by `transition_service.py::ImportJobTransitionService`, the only file that
    writes it. No `version` column on this table -- no optimistic-concurrency check needed, but
    every command POST still requires `Idempotency-Key` (D-215's blanket rule)."""

    __tablename__ = "import_jobs"

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    dr_event_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("dr_events.id", ondelete="RESTRICT"), nullable=False
    )
    source_file_uri: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="PENDING")
    mapping_data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    error_data: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now_utc)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class NeedsReviewItem(Base):
    """Low-confidence import row flagged for human follow-up (schema_v1.sql:632-647, D-221). No
    dedicated module owns this table yet, so it lives here alongside its first caller (same
    precedent as `dr_events/models.py::Override` and BUILD-04.plan.md Risk #5). BUILD-05 only
    creates rows (`status` defaults `OPEN`, never set explicitly here) -- resolution/routing is a
    later increment's job (`Capability.RESOLVE_NEEDS_REVIEW` already exists, unused until then)."""

    __tablename__ = "needs_review_items"

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    dr_event_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("dr_events.id", ondelete="RESTRICT"), nullable=True
    )
    target_type: Mapped[str] = mapped_column(
        Enum(
            "DR_EVENT",
            "DR_APPLICATION",
            "APPLICATION",
            "WORK_STREAM",
            "TASK",
            "TASK_DEPENDENCY",
            "MILESTONE",
            "BLOCKER",
            "ISSUE_FINDING",
            "VALIDATION",
            "IMPORT_JOB",
            "PLAN",
            "PLAN_VERSION",
            "REPORT",
            "DOCUMENT",
            "ALERT",
            name="target_type",
            native_enum=True,
            create_type=False,
        ),
        nullable=False,
    )
    target_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(
        Enum(
            "AI",
            "EXCEL_IMPORT",
            "SEMANTIC_INFERENCE",
            name="review_source",
            native_enum=True,
            create_type=False,
        ),
        nullable=False,
    )
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(7, 6), nullable=True)
    status: Mapped[str] = mapped_column(
        Enum(
            "OPEN",
            "IN_REVIEW",
            "RESOLVED",
            "DISMISSED",
            name="review_status",
            native_enum=True,
            create_type=False,
        ),
        nullable=False,
        default="OPEN",
    )
    assigned_scope_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    assigned_scope_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    resolved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now_utc)

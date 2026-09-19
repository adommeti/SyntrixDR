from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def _now_utc() -> datetime:
    """Return the current UTC datetime."""
    return datetime.now(UTC)


class Task(Base):
    """Executable unit (schema_v1.sql:296-329, D-226 evidence columns via
    schema_v2_reconciliation.sql). BUILD-05 writes only this ORM model plus
    `commands.py::create_draft_task` for Excel import; the full lifecycle
    (`transition_service.py`, Ready-derivation, `routes.py`) is BUILD-06's scope -- see
    BUILD-05.plan.md Risk #2. Lifecycle status is never set explicitly by BUILD-05: creation
    relies on the column default (`NOT_STARTED`), so `guard-forbidden-stack.sh` never triggers."""

    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint(
            "dr_application_id IS NOT NULL OR work_stream_id IS NOT NULL", name="task_context_ck"
        ),
        Index("ix_tasks_event_status", "dr_event_id", "status", postgresql_where=text("deleted_at IS NULL")),
        Index(
            "ix_tasks_application",
            "dr_application_id",
            "status",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ix_tasks_work_stream", "work_stream_id", "status", postgresql_where=text("deleted_at IS NULL")
        ),
        Index(
            "ix_tasks_assignee",
            "current_assignee_user_id",
            "status",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("ix_tasks_team", "owning_team_id", "status", postgresql_where=text("deleted_at IS NULL")),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    dr_event_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("dr_events.id", ondelete="RESTRICT"), nullable=False
    )
    dr_application_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("dr_applications.id", ondelete="RESTRICT"), nullable=True
    )
    work_stream_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("work_streams.id", ondelete="RESTRICT"), nullable=True
    )
    parent_task_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=True
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    phase: Mapped[str] = mapped_column(
        Enum(
            "PRE_DR",
            "FAILOVER",
            "VALIDATION",
            "FAILBACK",
            "POST_DR",
            name="task_phase",
            native_enum=True,
            create_type=False,
        ),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        Enum(
            "NOT_STARTED",
            "IN_PROGRESS",
            "BLOCKED",
            "READY_FOR_VALIDATION",
            "COMPLETED",
            "CANCELLED",
            name="task_status",
            native_enum=True,
            create_type=False,
        ),
        nullable=False,
        default="NOT_STARTED",
    )
    owning_team_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("teams.id", ondelete="RESTRICT"), nullable=False
    )
    current_assignee_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    default_owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    backup_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    escalation_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    expected_duration_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    needs_specific_validation: Mapped[bool] = mapped_column(nullable=False, default=False)
    added_during_execution: Mapped[bool] = mapped_column(nullable=False, default=False)
    #: D-221 provenance -- which import produced this Task, and which row of that import.
    source_import_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    source_import_row: Mapped[str | None] = mapped_column(Text, nullable=True)
    sort_order: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: D-226 (schema_v2_reconciliation.sql) -- also carried on the plan/import task representation.
    evidence_required: Mapped[bool] = mapped_column(nullable=False, default=True)
    evidence_min_count: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    verification_note_required: Mapped[bool] = mapped_column(nullable=False, default=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now_utc)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TaskDependency(Base):
    """Finish-to-start dependency edge (schema_v1.sql:332-345). BUILD-05 only creates rows
    (`commands.py::create_draft_dependency`) with a minimal no-self-edge/no-exact-duplicate guard
    -- full cycle detection across the whole graph is BUILD-06's scope (plan Risk #2)."""

    __tablename__ = "task_dependencies"
    __table_args__ = (
        CheckConstraint("predecessor_task_id <> successor_task_id", name="no_self_dependency"),
        Index(
            "ux_task_dependency",
            "predecessor_task_id",
            "successor_task_id",
            "dependency_type",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    dr_event_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("dr_events.id", ondelete="RESTRICT"), nullable=False
    )
    predecessor_task_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=False
    )
    successor_task_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=False
    )
    dependency_type: Mapped[str] = mapped_column(
        Enum("FINISH_TO_START", name="dependency_type", native_enum=True, create_type=False),
        nullable=False,
        default="FINISH_TO_START",
    )
    strength: Mapped[str] = mapped_column(
        Enum("HARD", "ADVISORY", name="dependency_strength", native_enum=True, create_type=False),
        nullable=False,
        default="HARD",
    )
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now_utc)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

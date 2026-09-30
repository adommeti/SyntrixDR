from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, Enum, ForeignKey, Integer, Text
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

import app.tasks_dependencies.models as _task_models
from app.core.database import Base
from app.dr_events.models import DrApplication, DrEvent
from app.users_teams_org.models import User
from app.work_streams.models import WorkStream

# Registers every cross-module FK target on Base.metadata (ADR-038). `tasks` is registered through
# the module, not a class name: `tasks_dependencies.models` imports this module back (its
# `milestone_dependencies.milestone_id` FK), and a module reference survives that circular import.
_ = (_task_models, DrApplication, DrEvent, User, WorkStream)

MILESTONE_STATUSES = (
    "NOT_STARTED",
    "IN_PROGRESS",
    "AT_RISK",
    "READY_FOR_CONFIRMATION",
    "ACHIEVED",
    "MISSED",
)


def _now_utc() -> datetime:
    return datetime.now(UTC)


class Milestone(Base):
    """First-class gate (schema_v1.sql:347-366, ADR-009). Status only ever changes in
    `transition_service.py`; creation relies on the column default (NOT_STARTED)."""

    __tablename__ = "milestones"
    __table_args__ = (
        CheckConstraint(
            "work_stream_id IS NOT NULL OR dr_application_id IS NOT NULL", name="milestone_context_ck"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    dr_event_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("dr_events.id", ondelete="RESTRICT"), nullable=False
    )
    work_stream_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("work_streams.id", ondelete="RESTRICT"), nullable=True
    )
    dr_application_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("dr_applications.id", ondelete="RESTRICT"), nullable=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        Enum(*MILESTONE_STATUSES, name="milestone_status", native_enum=True, create_type=False),
        nullable=False,
        default="NOT_STARTED",
    )
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    confirmation_mode: Mapped[str] = mapped_column(
        Enum("MANUAL", "AUTOMATIC", name="milestone_confirmation_mode", native_enum=True, create_type=False),
        nullable=False,
        default="MANUAL",
    )
    ready_for_confirmation_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    achieved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    target_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now_utc)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MilestoneTask(Base):
    """A Task contributing to a Milestone (schema_v1.sql:367-373; DATA_MODEL.md "aggregates").
    Only `is_required` contributors hold the Milestone back from READY_FOR_CONFIRMATION."""

    __tablename__ = "milestone_tasks"

    milestone_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("milestones.id", ondelete="RESTRICT"), primary_key=True
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("tasks.id", ondelete="RESTRICT"), primary_key=True
    )
    is_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now_utc)

"""`issue_findings` (schema_v1.sql; STATE_MACHINES.md §Issue/Finding; D-253). A non-blocking
observation, risk, deviation or lesson linked to an Event and optionally to a DR Application, Work Stream
or Task. Nothing in execution reads this table: an Issue never stops a Task (D-253)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.dr_events.models import DrApplication, DrEvent
from app.tasks_dependencies.models import Task
from app.users_teams_org.models import User
from app.work_streams.models import WorkStream

_ = (DrApplication, DrEvent, Task, User, WorkStream)  # registers FK targets on Base.metadata (ADR-038)

ISSUE_TYPES = ("ISSUE", "FINDING", "RISK", "DEVIATION", "LESSON")
ISSUE_STATUSES = ("OPEN", "IN_REVIEW", "RESOLVED", "DISMISSED")


def _now_utc() -> datetime:
    return datetime.now(UTC)


class IssueFinding(Base):
    __tablename__ = "issue_findings"
    __table_args__ = (
        Index(
            "ix_issue_findings_event_status",
            "dr_event_id",
            "status",
            postgresql_where=text("deleted_at IS NULL"),
        ),
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
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=True
    )
    item_type: Mapped[str] = mapped_column(
        Enum(*ISSUE_TYPES, name="issue_finding_type", native_enum=True, create_type=False), nullable=False
    )
    status: Mapped[str] = mapped_column(
        Enum(*ISSUE_STATUSES, name="issue_finding_status", native_enum=True, create_type=False),
        nullable=False,
        default="OPEN",
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now_utc)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

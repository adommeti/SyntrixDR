from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.tasks_dependencies.models import Task
from app.users_teams_org.models import Team, User

_ = (Task, Team, User)  # registers cross-module FK targets on Base.metadata (ADR-038)


def _now_utc() -> datetime:
    return datetime.now(UTC)


#: A Blocker stays "active" until CLOSED -- the schema's own definition, since both partial
#: indexes are `WHERE status <> 'CLOSED'`. VERIFIED is transient (`verify` moves it to CLOSED in
#: the same transaction, D-211), so it still counts as active.
INACTIVE_BLOCKER_STATUS = "CLOSED"

_ACTIVE_WHERE = text("deleted_at IS NULL AND status <> 'CLOSED'")


class Blocker(Base):
    """Structured obstruction backing a BLOCKED Task (`blockers`, schema_v1.sql:388-411).

    BUILD-06 needs only enough of this to satisfy invariant #10 (a BLOCKED Task has >=1 active
    Blocker) when `TaskTransitionService.block` runs: this model plus
    `commands.py::create_blocker_for_task`. The Blocker's own lifecycle, its routes, and the
    `verify` -> Task-resume side effect (D-252) are BUILD-08's (BUILD-06.plan.md Risk #3)."""

    __tablename__ = "blockers"
    __table_args__ = (
        Index("ix_blockers_task_active", "task_id", "status", postgresql_where=_ACTIVE_WHERE),
        Index("ix_blockers_team_active", "blocker_team_id", "status", postgresql_where=_ACTIVE_WHERE),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=False
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        Enum(
            "OPEN",
            "ASSIGNED",
            "IN_PROGRESS",
            "RESOLVED",
            "VERIFIED",
            "CLOSED",
            name="blocker_status",
            native_enum=True,
            create_type=False,
        ),
        nullable=False,
        default="OPEN",
    )
    blocker_team_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("teams.id", ondelete="SET NULL"), nullable=True
    )
    blocker_owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    blocked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now_utc)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now_utc)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

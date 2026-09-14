from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Text, text
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.external_refs import dr_events_table
from app.users_teams_org.models import User

_ = (dr_events_table, User)  # registers dr_events (stub) / users (real) on Base.metadata

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

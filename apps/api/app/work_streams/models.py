from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def _now_utc() -> datetime:
    """Return the current UTC datetime."""
    return datetime.now(UTC)


class WorkStream(Base):
    """Shared operational lane within a DR Event (DATA_MODEL.md "Work Stream"; D-227's
    `stream_type`). BUILD-05 (this module's first caller, via Excel import's `accept` step)
    writes only `models.py` + `commands.py::get_or_create_work_stream` -- routes/schemas/
    transition_service/queries and the full Work Stream lifecycle are BUILD-06's scope
    (BUILD-05.plan.md Risk #2)."""

    __tablename__ = "work_streams"
    __table_args__ = (
        Index(
            "ux_work_stream_event_name",
            "dr_event_id",
            text("lower(name)"),
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ix_work_streams_event_type",
            "dr_event_id",
            "stream_type",
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    dr_event_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("dr_events.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    lead_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    owning_team_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("teams.id", ondelete="SET NULL"), nullable=True
    )
    sequence_order: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stream_type: Mapped[str] = mapped_column(
        Enum(
            "NETWORK",
            "STORAGE",
            "DATABASE",
            "APPLICATIONS",
            "MONITORING",
            "VALIDATION",
            "CUSTOM",
            name="work_stream_type",
            native_enum=True,
            create_type=False,
        ),
        nullable=False,
        default="CUSTOM",
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now_utc)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

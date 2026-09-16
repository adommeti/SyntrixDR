from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import ENUM as PgEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.core.clock import Clock
from app.core.database import Base
from app.dr_events.models import DrEvent

# Mirrors the `target_type` enum created by 0002_reconciliation; `create_type=False`
# because the type already exists in the database (migrations rule).
_TARGET_TYPE = PgEnum(
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
    create_type=False,
)


_ = DrEvent  # registers `dr_events` (real model now) on Base.metadata


class OutboxEvent(Base):
    """Mirrors `outbox_events` (schema_v2_reconciliation.sql, D-242).

    A publisher worker (BUILD-11) drains unpublished rows to Redis pub/sub;
    this session only provides the write side used inside a command's
    transaction.
    """

    __tablename__ = "outbox_events"
    __table_args__ = (
        CheckConstraint("attempts >= 0", name="outbox_events_attempts_check"),
        Index("ix_outbox_events_unpublished", "occurred_at", postgresql_where=text("published_at IS NULL")),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    aggregate_type: Mapped[str] = mapped_column(_TARGET_TYPE, nullable=False)
    aggregate_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    dr_event_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("dr_events.id", ondelete="RESTRICT"), nullable=True
    )
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


async def write_outbox(
    session: AsyncSession,
    *,
    aggregate_type: str,
    aggregate_id: uuid.UUID,
    event_type: str,
    payload: dict[str, Any],
    clock: Clock,
    dr_event_id: uuid.UUID | None = None,
) -> OutboxEvent:
    """Writes an outbox row in the caller's active transaction (same commit as the domain change)."""
    event = OutboxEvent(
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        dr_event_id=dr_event_id,
        event_type=event_type,
        payload=payload,
        occurred_at=clock.now(),
    )
    session.add(event)
    await session.flush()
    return event

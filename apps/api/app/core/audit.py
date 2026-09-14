from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.external_refs import dr_events_table
from app.users_teams_org.models import User

_ = (User, dr_events_table)  # registers users (real)/dr_events (stub) on Base.metadata


def _now_utc() -> datetime:
    """Return the current UTC datetime."""
    return datetime.now(UTC)


class AuditEvent(Base):
    """Immutable audit log of all state changes in the system."""

    __tablename__ = "audit_events"

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    dr_event_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("dr_events.id", ondelete="RESTRICT"), nullable=True
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    actor_type: Mapped[str] = mapped_column(Text, nullable=False, default="USER")
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    before_data: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    after_data: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, nullable=False, default={})
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now_utc)

    __table_args__ = (
        Index("ix_audit_event_time", "dr_event_id", text("occurred_at DESC")),
        Index("ix_audit_entity", "entity_type", "entity_id", text("occurred_at DESC")),
    )


async def write_audit(
    session: AsyncSession,
    *,
    actor_user_id: uuid.UUID | None,
    entity_type: str,
    entity_id: uuid.UUID,
    action: str,
    metadata: dict[str, Any] | None = None,
    dr_event_id: uuid.UUID | None = None,
) -> None:
    """Write an audit event. Does not commit the session; caller controls the transaction boundary.

    Args:
        session: AsyncSession for the write
        actor_user_id: UUID of the user performing the action (None for system/auth actions)
        entity_type: Type of entity being changed (e.g. "USER", "TASK", "SESSION")
        entity_id: UUID of the entity
        action: The action taken (e.g. "CREATE", "UPDATE", "DELETE", "LOGIN")
        metadata: Additional context (e.g. change details, reason code)
        dr_event_id: Optional DR Event context (None for auth events)
    """
    event = AuditEvent(
        dr_event_id=dr_event_id,
        actor_user_id=actor_user_id,
        actor_type="USER",
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        metadata_=metadata or {},
    )
    session.add(event)
    await session.flush()

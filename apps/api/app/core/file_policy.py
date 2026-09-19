from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import ARRAY, BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Text, select
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def _now_utc() -> datetime:
    """Return the current UTC datetime."""
    return datetime.now(UTC)


class FilePolicy(Base):
    """Admin-configurable file-upload policy (schema_v2_reconciliation.sql:354-374, D-225/D-245)
    -- a single seeded row (`singleton=TRUE`). First real consumer is BUILD-05's Excel import
    upload guard; BUILD-09 (evidence) reuses the same row, doesn't redefine it."""

    __tablename__ = "file_policy"
    __table_args__ = (CheckConstraint("singleton", name="file_policy_singleton_check"),)

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    singleton: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, unique=True)
    max_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=104_857_600)
    allowed_extensions: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    allowed_mime_types: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    scanning_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now_utc)


async def get_file_policy(session: AsyncSession) -> FilePolicy:
    """The migration seeds exactly one row (`INSERT INTO file_policy (singleton) VALUES (TRUE)`,
    schema_v2_reconciliation.sql:496) -- always present after `alembic upgrade head`."""
    result = await session.execute(select(FilePolicy).where(FilePolicy.singleton.is_(True)))
    policy = result.scalar_one_or_none()
    assert policy is not None, "file_policy singleton row missing -- migration seed didn't run"
    return policy

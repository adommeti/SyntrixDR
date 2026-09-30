from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.users_teams_org.models import User

_ = User  # registers the cross-module FK target on Base.metadata (ADR-038)


def _now_utc() -> datetime:
    return datetime.now(UTC)


class Validation(Base):
    """Persisted validation record (`validations`, schema_v1.sql:439-457, plus
    `verification_note` from schema_v2_reconciliation.sql:215-216). No REST resource of its own in
    V1 (D-210): `POST /tasks/{id}/submit-validation` opens it PENDING, `POST /tasks/{id}/validate`
    closes it APPROVED/REJECTED.

    Two notes, never conflated: `verification_note` is the *submitter's* note written at
    submit-validation; `note` is the *validator's* approve/reject reason."""

    __tablename__ = "validations"
    __table_args__ = (
        Index(
            "ix_validations_target",
            "target_type",
            "target_id",
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    target_type: Mapped[str] = mapped_column(
        Enum(
            "TASK",
            "DR_APPLICATION",
            "MILESTONE",
            name="validation_target_type",
            native_enum=True,
            create_type=False,
        ),
        nullable=False,
    )
    target_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(
        Enum(
            "PENDING",
            "IN_REVIEW",
            "APPROVED",
            "REJECTED",
            "REMEDIATION",
            name="validation_status",
            native_enum=True,
            create_type=False,
        ),
        nullable=False,
        default="PENDING",
    )
    validator_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    verification_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    business_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    business_confirmation_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now_utc)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

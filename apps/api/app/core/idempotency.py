from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from fastapi import Request
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, Text, UniqueConstraint, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.core.clock import Clock, SystemClock
from app.core.database import Base
from app.core.errors import AppError, IdempotencyKeyRequiredError
from app.core.external_refs import users_table

_ = users_table  # registers `users` (FK-resolution stub) on Base.metadata

IDEMPOTENCY_HEADER = "Idempotency-Key"


class IdempotencyKey(Base):
    """Mirrors `idempotency_keys` (schema_v2_reconciliation.sql, D-215).

    A row is inserted when a command starts (response_status NULL = in flight)
    and completed with the stored outcome; expired rows are purged by the
    retention job (BUILD-21).
    """

    __tablename__ = "idempotency_keys"
    __table_args__ = (
        UniqueConstraint("key", "user_id", "route", name="idempotency_keys_unique"),
        CheckConstraint(
            "response_status IS NULL OR (response_status BETWEEN 100 AND 599)",
            name="idempotency_keys_response_status_check",
        ),
        Index("ix_idempotency_keys_expires", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    key: Mapped[str] = mapped_column(Text, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    route: Mapped[str] = mapped_column(Text, nullable=False)
    request_hash: Mapped[str] = mapped_column(Text, nullable=False)
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_body: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class IdempotencyRequestMismatchError(AppError):
    code = "IDEMPOTENCY_REQUEST_MISMATCH"
    status_code = 422

    def __init__(self) -> None:
        super().__init__("This Idempotency-Key was already used with a different request body.")


class IdempotencyInProgressError(AppError):
    code = "IDEMPOTENCY_IN_PROGRESS"
    status_code = 409

    def __init__(self) -> None:
        super().__init__("A request with this Idempotency-Key is still being processed.")


@dataclass
class IdempotencyContext:
    """Returned to the route by `require_idempotency_key`.

    Every command route follows the same contract:
        ctx = await require_idempotency_key(...)
        if ctx.is_replay:
            return JSONResponse(status_code=ctx.stored_status, content=ctx.stored_body)
        ... business logic ...
        await complete(session, ctx, status_code, body)
    """

    row_id: uuid.UUID
    is_replay: bool
    stored_status: int | None = None
    stored_body: dict[str, Any] | None = None


def _hash_body(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


async def require_idempotency_key(
    request: Request,
    session: AsyncSession,
    user_id: uuid.UUID,
    clock: Clock | None = None,
) -> IdempotencyContext:
    key = request.headers.get(IDEMPOTENCY_HEADER)
    if not key:
        raise IdempotencyKeyRequiredError

    clock = clock or SystemClock()
    now = clock.now()
    route = request.url.path
    request_hash = _hash_body(await request.body())

    # (key, user, route) is UNIQUE regardless of expiry, so an expired row is
    # reset in place rather than replaced by a second insert.
    existing = (
        await session.execute(
            select(IdempotencyKey).where(
                IdempotencyKey.key == key,
                IdempotencyKey.user_id == user_id,
                IdempotencyKey.route == route,
            )
        )
    ).scalar_one_or_none()

    if existing is not None and existing.expires_at > now:
        if existing.request_hash != request_hash:
            raise IdempotencyRequestMismatchError
        if existing.response_status is None:
            raise IdempotencyInProgressError
        return IdempotencyContext(
            row_id=existing.id,
            is_replay=True,
            stored_status=existing.response_status,
            stored_body=existing.response_body,
        )

    if existing is not None:
        existing.request_hash = request_hash
        existing.response_status = None
        existing.response_body = None
        existing.created_at = now
        existing.expires_at = now + timedelta(hours=24)
        await session.flush()
        return IdempotencyContext(row_id=existing.id, is_replay=False)

    row = IdempotencyKey(
        key=key,
        user_id=user_id,
        route=route,
        request_hash=request_hash,
        created_at=now,
        expires_at=now + timedelta(hours=24),
    )
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        # Lost the race to a concurrent request with the same (key, user, route).
        raise IdempotencyInProgressError from None

    return IdempotencyContext(row_id=row.id, is_replay=False)


async def complete(
    session: AsyncSession,
    ctx: IdempotencyContext,
    status_code: int,
    body: dict[str, Any],
) -> None:
    """Stores the command's outcome so a later replay returns it verbatim."""
    if ctx.is_replay:
        return
    row = await session.get(IdempotencyKey, ctx.row_id)
    if row is None:
        return
    row.response_status = status_code
    row.response_body = body
    await session.flush()

"""Notification fan-out (D-241): deliver already-written `notifications` rows through the
`NotificationSender` port. The trigger is the outbox: BUILD-11's drain dispatches this task for each
`NotificationCreated` event (BUILD-08.plan.md Risk B4); nothing here reads or marks `outbox_events`.
Same DI split as the other jobs (ADR-045): `deliver_notifications(session, sender, ids)` takes its
session; `_deliver_async` owns an engine for the Celery task."""

from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.comments_mentions_notifications.models import Notification
from app.comments_mentions_notifications.ports import NotificationSender, NullNotificationSender
from app.core.config import get_settings
from app.core.database import make_engine, make_session_factory
from app.jobs.celery_app import celery_app


async def deliver_notifications(
    session: AsyncSession, sender: NotificationSender, notification_ids: list[uuid.UUID]
) -> list[uuid.UUID]:
    """Sends each existing notification once through `sender`, in id order; unknown ids are skipped.
    Idempotent per call: the sender is the party that must tolerate a redelivery (at-least-once)."""
    if not notification_ids:
        return []
    rows = await session.execute(
        select(Notification).where(Notification.id.in_(notification_ids)).order_by(Notification.id)
    )
    delivered: list[uuid.UUID] = []
    for notification in rows.scalars().all():
        await sender.send(notification)
        delivered.append(notification.id)
    return delivered


def default_sender() -> NotificationSender:
    """The adapter for this deployment. BUILD-18 returns an SMTP sender when
    `notifications.email.enabled` is true; until then every channel is the in-app inbox only."""
    return NullNotificationSender()


async def _deliver_async(notification_ids: list[str]) -> int:
    engine = make_engine(get_settings().database_url)
    session_factory = make_session_factory(engine)
    async with session_factory() as session:
        delivered = await deliver_notifications(
            session, default_sender(), [uuid.UUID(i) for i in notification_ids]
        )
    await engine.dispose()
    return len(delivered)


@celery_app.task(name="drcc.fan_out_notifications")  # pyright: ignore[reportUntypedFunctionDecorator, reportUnknownMemberType]
def fan_out_notifications_task(notification_ids: list[str]) -> int:
    return asyncio.run(_deliver_async(notification_ids))

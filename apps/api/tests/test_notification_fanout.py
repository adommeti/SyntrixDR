"""Notification fan-out (D-241): rows are written by the causing command; the job only delivers them
through the `NotificationSender` port. Email arrives in BUILD-18; the Null sender is the V1 adapter."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.comments_mentions_notifications import jobs
from app.comments_mentions_notifications.catalog import NotificationType, Severity
from app.comments_mentions_notifications.commands import notify_user
from app.comments_mentions_notifications.jobs import default_sender, deliver_notifications
from app.comments_mentions_notifications.models import Notification
from app.comments_mentions_notifications.ports import NullNotificationSender
from app.core.clock import FakeClock
from app.jobs.celery_app import celery_app
from tests.factories import build_world, seed_task

pytestmark = [pytest.mark.domain]


class RecordingSender:
    def __init__(self) -> None:
        self.sent: list[uuid.UUID] = []

    async def send(self, notification: Notification) -> None:
        self.sent.append(notification.id)


async def _two(session: AsyncSession, clock: FakeClock) -> list[uuid.UUID]:
    w = await build_world(session)
    task_id = await seed_task(session, w)
    ids: list[uuid.UUID] = []
    for user in (w.executor_id, w.teammate_id):
        note = await notify_user(
            session,
            user_id=user,
            dr_event_id=w.event_id,
            alert=None,
            notification_type=NotificationType.MENTIONED,
            severity=Severity.LOW,
            title="t",
            body=None,
            target_type="TASK",
            target_id=task_id,
            clock=clock,
        )
        ids.append(note.id)
    return ids


async def test_the_job_delivers_each_given_row_once_and_skips_unknown_ids(
    session: AsyncSession, clock: FakeClock
) -> None:
    ids = await _two(session, clock)
    sender = RecordingSender()

    delivered = await deliver_notifications(session, sender, [*ids, uuid.uuid4()])

    assert sorted(delivered) == sorted(ids) and sorted(sender.sent) == sorted(ids)
    assert await deliver_notifications(session, sender, []) == []


async def test_the_null_sender_is_the_default_and_delivers_nowhere(
    session: AsyncSession, clock: FakeClock
) -> None:
    ids = await _two(session, clock)
    assert isinstance(default_sender(), NullNotificationSender)
    assert await deliver_notifications(session, default_sender(), ids) == sorted(ids)


def test_the_task_is_registered_but_not_on_the_beat() -> None:
    """Triggered per `NotificationCreated` event by the outbox drain (BUILD-11), never by a schedule."""
    assert jobs.fan_out_notifications_task.name == "drcc.fan_out_notifications"
    assert "app.comments_mentions_notifications.jobs" in celery_app.conf.include
    assert all(e["task"] != "drcc.fan_out_notifications" for e in celery_app.conf.beat_schedule.values())

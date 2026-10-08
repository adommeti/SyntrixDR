"""The channel-delivery port for notifications (D-241 "notification fan-out" job). In-app rows are
written by the causing command; this port is only how a row leaves the system. `NullNotificationSender`
is the only adapter until BUILD-18 (SMTP, `notifications.email.enabled`); Teams/Slack stay disabled
(D-225). Nothing in a critical workflow depends on a sender succeeding."""

from __future__ import annotations

from typing import Protocol

from app.comments_mentions_notifications.models import Notification


class NotificationSender(Protocol):
    async def send(self, notification: Notification) -> None: ...


class NullNotificationSender:
    """Delivers nowhere. The in-app inbox is the V1 channel until the email adapter lands."""

    async def send(self, notification: Notification) -> None:
        return None

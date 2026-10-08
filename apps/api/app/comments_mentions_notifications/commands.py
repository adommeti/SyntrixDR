"""Alert and notification writes for other modules' already-authorized side effects (jobs, transition
services). No route here yet: acknowledge/snooze and the per-user inbox are BUILD-08 session b."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.comments_mentions_notifications.catalog import AlertType, NotificationType, Severity
from app.comments_mentions_notifications.models import Alert, Notification
from app.core.clock import Clock


async def raise_alert(
    session: AsyncSession,
    *,
    dr_event_id: uuid.UUID | None,
    alert_type: AlertType,
    severity: Severity,
    title: str,
    body: str | None,
    target_type: str,
    target_id: uuid.UUID,
    clock: Clock,
) -> Alert:
    alert = Alert(
        dr_event_id=dr_event_id,
        alert_type=alert_type.value,
        severity=severity.value,
        title=title,
        body=body,
        target_type=target_type,
        target_id=target_id,
        created_at=clock.now(),
    )
    session.add(alert)
    await session.flush()
    return alert


async def notify_user(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    dr_event_id: uuid.UUID | None,
    alert: Alert | None,
    notification_type: NotificationType,
    severity: Severity,
    title: str,
    body: str | None,
    target_type: str,
    target_id: uuid.UUID,
    clock: Clock,
) -> Notification:
    notification = Notification(
        user_id=user_id,
        dr_event_id=dr_event_id,
        alert_id=alert.id if alert is not None else None,
        notification_type=notification_type.value,
        severity=severity.value,
        title=title,
        body=body,
        target_type=target_type,
        target_id=target_id,
        created_at=clock.now(),
    )
    session.add(notification)
    await session.flush()
    return notification


async def has_unresolved_alert(
    session: AsyncSession, *, alert_type: AlertType, target_type: str, target_id: uuid.UUID
) -> bool:
    row = await session.execute(
        select(Alert.id).where(
            Alert.alert_type == alert_type.value,
            Alert.target_type == target_type,
            Alert.target_id == target_id,
            Alert.resolved_at.is_(None),
        )
    )
    return row.first() is not None


async def resolve_alerts(
    session: AsyncSession, *, alert_type: AlertType, target_type: str, target_id: uuid.UUID, at: datetime
) -> int:
    """Marks every unresolved alert of this type on this target resolved (the condition is gone)."""
    result = await session.execute(
        update(Alert)
        .where(
            Alert.alert_type == alert_type.value,
            Alert.target_type == target_type,
            Alert.target_id == target_id,
            Alert.resolved_at.is_(None),
        )
        .values(resolved_at=at)
        .returning(Alert.id)
    )
    return len(result.all())

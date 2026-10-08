"""Alert, notification and comment writes. Notification rows are written in the same transaction as
the change that causes them (invariant 11) and each emits a `NotificationCreated` outbox event;
delivery to channels is `jobs.deliver_notifications` behind the `NotificationSender` port
(BUILD-08.plan.md Risk B4)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.comments_mentions_notifications.catalog import (
    BREACH_ALERT_TYPES,
    AlertType,
    NotificationType,
    Severity,
)
from app.comments_mentions_notifications.mentions import MAX_COMMENT_CHARS, MAX_MENTIONS, parse_mentions
from app.comments_mentions_notifications.models import Alert, AlertSnooze, Comment, Mention, Notification
from app.comments_mentions_notifications.policies import actor_may_acknowledge_alert, actor_may_comment
from app.comments_mentions_notifications.queries import (
    COMMENT_TARGET_TYPES,
    get_comment,
    lock_alert,
    resolve_comment_target,
)
from app.core.audit import write_audit
from app.core.clock import Clock, SystemClock
from app.core.errors import AppError
from app.core.outbox import write_outbox
from app.dr_events.participants import user_can_see_event
from app.users_teams_org.authorization import AuthorizationRequiredError
from app.users_teams_org.queries import (
    find_active_user_by_display_name,
    find_active_user_by_email,
    is_user_active,
)


class TargetTypeNotAllowedError(AppError):
    code = "TARGET_TYPE_NOT_ALLOWED"
    status_code = 422

    def __init__(self, target_type: str) -> None:
        super().__init__(
            f"Comments are not allowed on {target_type}.",
            details={"allowed": sorted(COMMENT_TARGET_TYPES)},
        )


class CommentTargetNotFoundError(AppError):
    code = "COMMENT_TARGET_NOT_FOUND"
    status_code = 404

    def __init__(self) -> None:
        super().__init__("Comment target not found.")


class CommentBodyRequiredError(AppError):
    code = "COMMENT_BODY_REQUIRED"
    status_code = 422

    def __init__(self) -> None:
        super().__init__("A comment needs a non-empty body.")


class CommentTooLargeError(AppError):
    code = "COMMENT_TOO_LARGE"
    status_code = 422

    def __init__(self) -> None:
        super().__init__(
            f"A comment is at most {MAX_COMMENT_CHARS} characters and {MAX_MENTIONS} mentions.",
            details={"max_chars": MAX_COMMENT_CHARS, "max_mentions": MAX_MENTIONS},
        )


class ParentCommentMismatchError(AppError):
    code = "PARENT_COMMENT_MISMATCH"
    status_code = 422

    def __init__(self) -> None:
        super().__init__("A reply must answer a comment on the same target.")


class AlertNotFoundError(AppError):
    code = "ALERT_NOT_FOUND"
    status_code = 404

    def __init__(self) -> None:
        super().__init__("Alert not found.")


class AlertAlreadyAcknowledgedError(AppError):
    code = "ALERT_ALREADY_ACKNOWLEDGED"
    status_code = 409

    def __init__(self) -> None:
        super().__init__("This alert has already been acknowledged.")


class AcknowledgementReasonRequiredError(AppError):
    code = "ACKNOWLEDGEMENT_REASON_REQUIRED"
    status_code = 422

    def __init__(self, alert_type: str) -> None:
        super().__init__(f"Acknowledging a {alert_type} alert needs a reason (FROZEN §9.8).")


class SnoozeUntilInPastError(AppError):
    code = "SNOOZE_UNTIL_IN_PAST"
    status_code = 422

    def __init__(self) -> None:
        super().__init__("snoozed_until must be in the future.")


# --------------------------------------------------------------------------------------------
# Alerts and notifications (side effects of jobs / other commands)
# --------------------------------------------------------------------------------------------


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
    """One in-app notification row plus its `NotificationCreated` outbox event (same transaction)."""
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
    await write_outbox(
        session,
        aggregate_type=target_type,
        aggregate_id=target_id,
        event_type="NotificationCreated",  # D-234 V1 union
        payload={
            "notification_id": str(notification.id),
            "user_id": str(user_id),
            "notification_type": notification_type.value,
            "severity": severity.value,
            "title": title,
        },
        clock=clock,
        dr_event_id=dr_event_id,
    )
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


# --------------------------------------------------------------------------------------------
# Comments and @mentions (API_CONTRACT.md:217)
# --------------------------------------------------------------------------------------------


async def _resolve_handle(session: AsyncSession, handle: str) -> uuid.UUID | None:
    if "@" in handle:
        return await find_active_user_by_email(session, handle)
    return await find_active_user_by_display_name(session, handle)


async def create_comment(
    session: AsyncSession,
    *,
    actor_id: uuid.UUID,
    target_type: str,
    target_id: uuid.UUID,
    body: str,
    parent_comment_id: uuid.UUID | None = None,
    mentioned_user_ids: list[uuid.UUID] | None = None,
    clock: Clock | None = None,
) -> tuple[Comment, list[uuid.UUID]]:
    """Cross-Team comment on a visible target. Mentions come from `@` handles in the body and from
    explicit `mentioned_user_ids`; each resolved user who can see the target's Event gets a `MENTIONED`
    notification (D-222: no enrolment source for mentions, so invisible users are recorded, not
    notified -- BUILD-08.plan.md Risk B3). Returns the comment and the notified user ids."""
    clock = clock or SystemClock()
    if target_type not in COMMENT_TARGET_TYPES:
        raise TargetTypeNotAllowedError(target_type)
    target = await resolve_comment_target(session, target_type, target_id)
    if target is None or not await user_can_see_event(session, actor_id, target.dr_event_id):
        raise CommentTargetNotFoundError()
    if not await actor_may_comment(session, actor_id):
        raise AuthorizationRequiredError()
    clean = body.strip()
    if not clean:
        raise CommentBodyRequiredError()
    if len(clean) > MAX_COMMENT_CHARS or len(mentioned_user_ids or []) > MAX_MENTIONS:
        raise CommentTooLargeError()
    if parent_comment_id is not None:
        parent = await get_comment(session, parent_comment_id)
        if parent is None or (parent.target_type, parent.target_id) != (target_type, target_id):
            raise ParentCommentMismatchError()

    handles = parse_mentions(clean)
    explicit = list(dict.fromkeys(mentioned_user_ids or []))
    if len(handles) + len(explicit) > MAX_MENTIONS:
        raise CommentTooLargeError()
    resolved: dict[uuid.UUID, str] = {}
    unresolved: list[str] = []
    for handle in handles:
        user_id = await _resolve_handle(session, handle)
        if user_id is None:
            unresolved.append(handle)
        else:
            resolved.setdefault(user_id, handle)
    for user_id in explicit:
        if await is_user_active(session, user_id):
            resolved.setdefault(user_id, str(user_id))
        else:
            unresolved.append(str(user_id))
    resolved.pop(actor_id, None)  # mentioning yourself is not a notification

    comment = Comment(
        target_type=target_type,
        target_id=target_id,
        author_user_id=actor_id,
        body=clean,
        parent_comment_id=parent_comment_id,
        created_at=clock.now(),
    )
    session.add(comment)
    await session.flush()

    notified: list[uuid.UUID] = []
    skipped: list[uuid.UUID] = []
    for user_id in resolved:
        session.add(Mention(comment_id=comment.id, mentioned_user_id=user_id, created_at=clock.now()))
        if await user_can_see_event(session, user_id, target.dr_event_id):
            await notify_user(
                session,
                user_id=user_id,
                dr_event_id=target.dr_event_id,
                alert=None,
                notification_type=NotificationType.MENTIONED,
                severity=Severity.LOW,
                title=f"You were mentioned on a {target_type.lower().replace('_', ' ')}",
                body=clean[:500],
                target_type=target_type,
                target_id=target_id,
                clock=clock,
            )
            notified.append(user_id)
        else:
            skipped.append(user_id)
    await session.flush()
    await write_audit(
        session,
        actor_user_id=actor_id,
        entity_type=target_type,
        entity_id=target_id,
        action="COMMENT_CREATED",
        dr_event_id=target.dr_event_id,
        after={
            "comment_id": str(comment.id),
            "parent_comment_id": str(parent_comment_id) if parent_comment_id else None,
        },
        metadata={
            "mentioned_user_ids": [str(u) for u in resolved],
            "notified_user_ids": [str(u) for u in notified],
            "not_visible_user_ids": [str(u) for u in skipped],
            "unresolved_mentions": unresolved,
        },
    )
    return comment, notified


# --------------------------------------------------------------------------------------------
# Alert acknowledge (shared) and snooze (personal) -- API_CONTRACT.md:218-219
# --------------------------------------------------------------------------------------------


async def _visible_alert(session: AsyncSession, actor_id: uuid.UUID, alert_id: uuid.UUID) -> Alert:
    alert = await lock_alert(session, alert_id)
    if alert is None:
        raise AlertNotFoundError()
    if alert.dr_event_id is not None and not await user_can_see_event(session, actor_id, alert.dr_event_id):
        raise AlertNotFoundError()
    if alert.dr_event_id is None and not await actor_may_acknowledge_alert(
        session, actor_id, dr_event_id=None
    ):
        raise AlertNotFoundError()  # global alerts are Admin-only; existence is not disclosed
    return alert


async def acknowledge_alert(
    session: AsyncSession,
    *,
    actor_id: uuid.UUID,
    alert_id: uuid.UUID,
    reason: str | None,
    clock: Clock | None = None,
) -> Alert:
    """Shared acknowledgement, audited; a breach alert needs a reason (FROZEN §9.8). Once."""
    clock = clock or SystemClock()
    alert = await _visible_alert(session, actor_id, alert_id)
    if not await actor_may_acknowledge_alert(session, actor_id, dr_event_id=alert.dr_event_id):
        raise AuthorizationRequiredError()
    if alert.acknowledged_at is not None:
        raise AlertAlreadyAcknowledgedError()
    clean_reason = reason.strip() if reason and reason.strip() else None
    if alert.alert_type in {t.value for t in BREACH_ALERT_TYPES} and clean_reason is None:
        raise AcknowledgementReasonRequiredError(alert.alert_type)

    # `resolved_at` is deliberately untouched: acknowledging says "seen", not "condition gone". For a
    # BLOCKER_ESCALATED alert the unresolved row is also the sweep's "already escalated" marker, so
    # resolving it here would make the sweep re-fire every 30 s for a Blocker that is still unrouted.
    alert.acknowledged_at = clock.now()
    alert.acknowledged_by_user_id = actor_id
    await session.flush()
    await write_audit(
        session,
        actor_user_id=actor_id,
        entity_type="ALERT",
        entity_id=alert.id,
        action="ALERT_ACKNOWLEDGED",
        dr_event_id=alert.dr_event_id,
        after={"alert_type": alert.alert_type, "severity": alert.severity, "reason": clean_reason},
    )
    return alert


async def snooze_alert(
    session: AsyncSession,
    *,
    actor_id: uuid.UUID,
    alert_id: uuid.UUID,
    snoozed_until: datetime,
    clock: Clock | None = None,
) -> AlertSnooze:
    """Personal snooze (FROZEN §12.9): one row per (alert, user), moved forward on repeat. Never changes
    the shared acknowledgement."""
    clock = clock or SystemClock()
    alert = await _visible_alert(session, actor_id, alert_id)
    if snoozed_until <= clock.now():
        raise SnoozeUntilInPastError()
    existing = (
        await session.execute(
            select(AlertSnooze).where(AlertSnooze.alert_id == alert.id, AlertSnooze.user_id == actor_id)
        )
    ).scalar_one_or_none()
    if existing is None:
        existing = AlertSnooze(
            alert_id=alert.id, user_id=actor_id, snoozed_until=snoozed_until, created_at=clock.now()
        )
        session.add(existing)
    else:
        existing.snoozed_until = snoozed_until
    await session.flush()
    await write_audit(
        session,
        actor_user_id=actor_id,
        entity_type="ALERT",
        entity_id=alert.id,
        action="ALERT_SNOOZED",
        dr_event_id=alert.dr_event_id,
        after={"snoozed_until": snoozed_until.isoformat()},
    )
    return existing

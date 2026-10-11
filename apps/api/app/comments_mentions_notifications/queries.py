from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.blockers.queries import get_blocker
from app.comments_mentions_notifications.models import Alert, Comment, Mention, Notification
from app.core.target_types import TARGET_TYPE_SUBSETS, TargetType
from app.dr_events.queries import get_dr_application, get_event
from app.issues_findings.queries import get_issue
from app.tasks_dependencies.queries import get_milestone, get_task
from app.work_streams.queries import get_work_stream

COMMENT_TARGET_TYPES: frozenset[str] = frozenset(t.value for t in TARGET_TYPE_SUBSETS["comments"])


@dataclass(frozen=True)
class TargetRef:
    target_type: str
    target_id: uuid.UUID
    dr_event_id: uuid.UUID


async def resolve_comment_target(
    session: AsyncSession, target_type: str, target_id: uuid.UUID
) -> TargetRef | None:
    """The Event a comment target belongs to, or None when the target doesn't exist. Visibility is the
    caller's check (D-222): the target's Event decides who may read and write its comments."""
    event_id: uuid.UUID | None = None
    if target_type == TargetType.DR_EVENT:
        event = await get_event(session, target_id)
        event_id = event.id if event is not None and event.deleted_at is None else None
    elif target_type == TargetType.DR_APPLICATION:
        dr_application = await get_dr_application(session, target_id)
        event_id = dr_application.dr_event_id if dr_application is not None else None
    elif target_type == TargetType.WORK_STREAM:
        stream = await get_work_stream(session, target_id)
        event_id = stream.dr_event_id if stream is not None else None
    elif target_type == TargetType.TASK:
        task = await get_task(session, target_id)
        event_id = task.dr_event_id if task is not None else None
    elif target_type == TargetType.BLOCKER:
        blocker = await get_blocker(session, target_id)
        if blocker is not None:
            task = await get_task(session, blocker.task_id)
            event_id = task.dr_event_id if task is not None else None
    elif target_type == TargetType.ISSUE_FINDING:
        issue = await get_issue(session, target_id)
        event_id = issue.dr_event_id if issue is not None else None
    elif target_type == TargetType.MILESTONE:
        milestone = await get_milestone(session, target_id)
        event_id = milestone.dr_event_id if milestone is not None else None
    if event_id is None:
        return None
    return TargetRef(target_type=target_type, target_id=target_id, dr_event_id=event_id)


async def get_comment(session: AsyncSession, comment_id: uuid.UUID) -> Comment | None:
    return (
        await session.execute(select(Comment).where(Comment.id == comment_id, Comment.deleted_at.is_(None)))
    ).scalar_one_or_none()


async def list_comments(session: AsyncSession, target_type: str, target_id: uuid.UUID) -> list[Comment]:
    return list(
        (
            await session.execute(
                select(Comment)
                .where(
                    Comment.target_type == target_type,
                    Comment.target_id == target_id,
                    Comment.deleted_at.is_(None),
                )
                .order_by(Comment.created_at, Comment.id)
            )
        )
        .scalars()
        .all()
    )


async def mentions_by_comment(
    session: AsyncSession, comment_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[uuid.UUID]]:
    out: dict[uuid.UUID, list[uuid.UUID]] = {c: [] for c in comment_ids}
    if not comment_ids:
        return out
    rows = await session.execute(
        select(Mention.comment_id, Mention.mentioned_user_id)
        .where(Mention.comment_id.in_(comment_ids))
        .order_by(Mention.created_at)
    )
    for comment_id, user_id in rows:
        out[comment_id].append(user_id)
    return out


async def get_alert(session: AsyncSession, alert_id: uuid.UUID) -> Alert | None:
    return await session.get(Alert, alert_id)


async def lock_alert(session: AsyncSession, alert_id: uuid.UUID) -> Alert | None:
    return (
        await session.execute(
            select(Alert)
            .where(Alert.id == alert_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def list_notifications(
    session: AsyncSession, user_id: uuid.UUID, *, unread_only: bool = False
) -> list[Notification]:
    query = (
        select(Notification).where(Notification.user_id == user_id).order_by(Notification.created_at.desc())
    )
    if unread_only:
        query = query.where(Notification.read_at.is_(None))
    return list((await session.execute(query)).scalars().all())

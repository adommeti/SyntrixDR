from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import write_audit
from app.core.clock import Clock, SystemClock
from app.core.outbox import write_outbox
from app.dr_events.errors import DrEventNotFoundError
from app.dr_events.queries import get_dr_application, get_visible_event
from app.issues_findings.models import IssueFinding
from app.issues_findings.policies import actor_may_create_issue
from app.tasks_dependencies.commands import CrossEventReferenceError
from app.tasks_dependencies.queries import get_task
from app.users_teams_org.authorization import AuthorizationRequiredError
from app.users_teams_org.commands import UserNotFoundError
from app.users_teams_org.queries import is_user_active
from app.work_streams.queries import get_work_stream


async def create_issue(
    session: AsyncSession,
    *,
    actor_id: uuid.UUID,
    dr_event_id: uuid.UUID,
    item_type: str,
    title: str,
    description: str,
    priority: str | None = None,
    dr_application_id: uuid.UUID | None = None,
    work_stream_id: uuid.UUID | None = None,
    task_id: uuid.UUID | None = None,
    owner_user_id: uuid.UUID | None = None,
    clock: Clock | None = None,
) -> IssueFinding:
    """`POST /dr-events/{id}/issues` (API_CONTRACT.md:185). Links must belong to this Event
    (422 `CROSS_EVENT_REFERENCE`, the Task-create precedent). Creating an Issue changes nothing else:
    it never blocks a Task (D-253)."""
    clock = clock or SystemClock()
    if await get_visible_event(session, actor_id, dr_event_id) is None:
        raise DrEventNotFoundError()
    if not await actor_may_create_issue(
        session, actor_id, work_stream_id=work_stream_id, dr_application_id=dr_application_id
    ):
        raise AuthorizationRequiredError()
    if dr_application_id is not None:
        dr_application = await get_dr_application(session, dr_application_id)
        if dr_application is None or dr_application.dr_event_id != dr_event_id:
            raise CrossEventReferenceError("dr_application_id")
    if work_stream_id is not None:
        stream = await get_work_stream(session, work_stream_id)
        if stream is None or stream.dr_event_id != dr_event_id:
            raise CrossEventReferenceError("work_stream_id")
    if task_id is not None:
        task = await get_task(session, task_id)
        if task is None or task.dr_event_id != dr_event_id:
            raise CrossEventReferenceError("task_id")
    if owner_user_id is not None and not await is_user_active(session, owner_user_id):
        raise UserNotFoundError()

    issue = IssueFinding(
        dr_event_id=dr_event_id,
        dr_application_id=dr_application_id,
        work_stream_id=work_stream_id,
        task_id=task_id,
        item_type=item_type,
        title=title.strip(),
        description=description.strip(),
        priority=priority,
        owner_user_id=owner_user_id,
        created_by_user_id=actor_id,
        created_at=clock.now(),
        updated_at=clock.now(),
    )
    session.add(issue)
    await session.flush()
    after = {
        "status": issue.status,
        "version": issue.version,
        "item_type": item_type,
        "task_id": str(task_id) if task_id else None,
    }
    await write_audit(
        session,
        actor_user_id=actor_id,
        entity_type="ISSUE_FINDING",
        entity_id=issue.id,
        action="ISSUE_CREATED",
        dr_event_id=dr_event_id,
        after=after,
    )
    await write_outbox(
        session,
        aggregate_type="ISSUE_FINDING",
        aggregate_id=issue.id,
        event_type="IssueChanged",
        payload={"change": "ISSUE_CREATED", **after},
        clock=clock,
        dr_event_id=dr_event_id,
    )
    return issue

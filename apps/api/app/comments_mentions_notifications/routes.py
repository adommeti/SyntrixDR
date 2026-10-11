from __future__ import annotations

import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.comments_mentions_notifications.commands import (
    CommentTargetNotFoundError,
    acknowledge_alert,
    create_comment,
    snooze_alert,
)
from app.comments_mentions_notifications.models import Comment
from app.comments_mentions_notifications.queries import (
    list_comments,
    mentions_by_comment,
    resolve_comment_target,
)
from app.comments_mentions_notifications.schemas import (
    AcknowledgeAlertRequest,
    AlertResponse,
    CommentListResponse,
    CommentResponse,
    CommentTargetType,
    CreateCommentRequest,
    SnoozeAlertRequest,
    SnoozeResponse,
)
from app.core.idempotency import complete, require_idempotency_key
from app.dr_events.participants import user_can_see_event
from app.identity_auth.dependencies import ClockDep, CurrentSession, DbSession, RequireCsrfDependency

router = APIRouter(prefix="/api/v1", tags=["collaboration"])


async def _responses(session: DbSession, comments: list[Comment]) -> list[CommentResponse]:
    mentions = await mentions_by_comment(session, [c.id for c in comments])
    return [
        CommentResponse(
            id=c.id,
            target_type=c.target_type,
            target_id=c.target_id,
            author_user_id=c.author_user_id,
            body=c.body,
            parent_comment_id=c.parent_comment_id,
            mentioned_user_ids=mentions[c.id],
            created_at=c.created_at,
        )
        for c in comments
    ]


@router.get("/{target_type}/{target_id}/comments")
async def list_comments_route(
    target_type: CommentTargetType, target_id: uuid.UUID, session: DbSession, session_data: CurrentSession
) -> CommentListResponse:
    target = await resolve_comment_target(session, target_type, target_id)
    if target is None or not await user_can_see_event(session, session_data.user_id, target.dr_event_id):
        raise CommentTargetNotFoundError()
    return CommentListResponse(
        comments=await _responses(session, await list_comments(session, target_type, target_id))
    )


@router.post(
    "/{target_type}/{target_id}/comments",
    status_code=201,
    dependencies=[RequireCsrfDependency],
    response_model=None,
    responses={201: {"model": CommentResponse}},
)
async def post_create_comment(
    target_type: CommentTargetType,
    target_id: uuid.UUID,
    request: Request,
    body: CreateCommentRequest,
    session: DbSession,
    session_data: CurrentSession,
    clock: ClockDep,
) -> CommentResponse | JSONResponse:
    ctx = await require_idempotency_key(request, session, session_data.user_id, clock)
    if ctx.is_replay:
        assert ctx.stored_status is not None
        return JSONResponse(status_code=ctx.stored_status, content=ctx.stored_body)
    comment, _ = await create_comment(
        session,
        actor_id=session_data.user_id,
        target_type=target_type,
        target_id=target_id,
        body=body.body,
        parent_comment_id=body.parent_comment_id,
        mentioned_user_ids=body.mentioned_user_ids,
        clock=clock,
    )
    [response] = await _responses(session, [comment])
    await complete(session, ctx, 201, response.model_dump(mode="json"))
    await session.commit()
    return JSONResponse(status_code=201, content=response.model_dump(mode="json"))


@router.post("/alerts/{alert_id}/acknowledge", dependencies=[RequireCsrfDependency], response_model=None)
async def post_acknowledge_alert(
    alert_id: uuid.UUID,
    request: Request,
    body: AcknowledgeAlertRequest,
    session: DbSession,
    session_data: CurrentSession,
    clock: ClockDep,
) -> AlertResponse | JSONResponse:
    """API_CONTRACT.md:218 -- shared ack, reason where a breach requires it."""
    ctx = await require_idempotency_key(request, session, session_data.user_id, clock)
    if ctx.is_replay:
        assert ctx.stored_status is not None
        return JSONResponse(status_code=ctx.stored_status, content=ctx.stored_body)
    alert = await acknowledge_alert(
        session, actor_id=session_data.user_id, alert_id=alert_id, reason=body.reason, clock=clock
    )
    response = AlertResponse.model_validate(alert)
    await complete(session, ctx, 200, response.model_dump(mode="json"))
    await session.commit()
    return response


@router.post("/alerts/{alert_id}/snooze", dependencies=[RequireCsrfDependency], response_model=None)
async def post_snooze_alert(
    alert_id: uuid.UUID,
    request: Request,
    body: SnoozeAlertRequest,
    session: DbSession,
    session_data: CurrentSession,
    clock: ClockDep,
) -> SnoozeResponse | JSONResponse:
    """API_CONTRACT.md:219 -- personal snooze."""
    ctx = await require_idempotency_key(request, session, session_data.user_id, clock)
    if ctx.is_replay:
        assert ctx.stored_status is not None
        return JSONResponse(status_code=ctx.stored_status, content=ctx.stored_body)
    snooze = await snooze_alert(
        session,
        actor_id=session_data.user_id,
        alert_id=alert_id,
        snoozed_until=body.snoozed_until,
        clock=clock,
    )
    response = SnoozeResponse.model_validate(snooze)
    await complete(session, ctx, 200, response.model_dump(mode="json"))
    await session.commit()
    return response

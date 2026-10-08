from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.core.idempotency import complete, require_idempotency_key
from app.dr_events.errors import DrEventNotFoundError
from app.dr_events.queries import get_visible_event
from app.identity_auth.dependencies import ClockDep, CurrentSession, DbSession, RequireCsrfDependency
from app.issues_findings.commands import create_issue
from app.issues_findings.models import IssueFinding
from app.issues_findings.queries import list_issues
from app.issues_findings.schemas import (
    CloseIssueRequest,
    CreateIssueRequest,
    IssueListResponse,
    IssueResponse,
    IssueStatus,
    ReviewIssueRequest,
)
from app.issues_findings.transition_service import IssueTransitionService

router = APIRouter(prefix="/api/v1", tags=["issues_findings"])


async def _run(
    request: Request,
    session: DbSession,
    session_data: CurrentSession,
    clock: ClockDep,
    call: Callable[[], Awaitable[IssueFinding]],
    status_code: int = 200,
) -> IssueResponse | JSONResponse:
    ctx = await require_idempotency_key(request, session, session_data.user_id, clock)
    if ctx.is_replay:
        assert ctx.stored_status is not None
        return JSONResponse(status_code=ctx.stored_status, content=ctx.stored_body)
    issue = await call()
    response = IssueResponse.model_validate(issue)
    await complete(session, ctx, status_code, response.model_dump(mode="json"))
    await session.commit()
    return JSONResponse(status_code=status_code, content=response.model_dump(mode="json"))


@router.get("/dr-events/{event_id}/issues")
async def list_issues_route(
    event_id: uuid.UUID,
    session: DbSession,
    session_data: CurrentSession,
    status: IssueStatus | None = None,
    task_id: uuid.UUID | None = None,
) -> IssueListResponse:
    if await get_visible_event(session, session_data.user_id, event_id) is None:
        raise DrEventNotFoundError()
    rows = await list_issues(session, event_id, status=status, task_id=task_id)
    return IssueListResponse(issues=[IssueResponse.model_validate(i) for i in rows])


@router.post(
    "/dr-events/{event_id}/issues",
    status_code=201,
    dependencies=[RequireCsrfDependency],
    response_model=None,
    responses={201: {"model": IssueResponse}},
)
async def post_create_issue(
    event_id: uuid.UUID,
    request: Request,
    body: CreateIssueRequest,
    session: DbSession,
    session_data: CurrentSession,
    clock: ClockDep,
) -> IssueResponse | JSONResponse:
    return await _run(
        request,
        session,
        session_data,
        clock,
        lambda: create_issue(
            session,
            actor_id=session_data.user_id,
            dr_event_id=event_id,
            item_type=body.item_type,
            title=body.title,
            description=body.description,
            priority=body.priority,
            dr_application_id=body.dr_application_id,
            work_stream_id=body.work_stream_id,
            task_id=body.task_id,
            owner_user_id=body.owner_user_id,
            clock=clock,
        ),
        status_code=201,
    )


@router.post("/issues/{issue_id}/review", dependencies=[RequireCsrfDependency], response_model=None)
async def post_review_issue(
    issue_id: uuid.UUID,
    request: Request,
    body: ReviewIssueRequest,
    session: DbSession,
    session_data: CurrentSession,
    clock: ClockDep,
) -> IssueResponse | JSONResponse:
    return await _run(
        request,
        session,
        session_data,
        clock,
        lambda: IssueTransitionService.review(
            session,
            actor_id=session_data.user_id,
            issue_id=issue_id,
            expected_version=body.expected_version,
            clock=clock,
        ),
    )


@router.post("/issues/{issue_id}/resolve", dependencies=[RequireCsrfDependency], response_model=None)
async def post_resolve_issue(
    issue_id: uuid.UUID,
    request: Request,
    body: CloseIssueRequest,
    session: DbSession,
    session_data: CurrentSession,
    clock: ClockDep,
) -> IssueResponse | JSONResponse:
    return await _run(
        request,
        session,
        session_data,
        clock,
        lambda: IssueTransitionService.resolve(
            session,
            actor_id=session_data.user_id,
            issue_id=issue_id,
            expected_version=body.expected_version,
            note=body.note,
            clock=clock,
        ),
    )


@router.post("/issues/{issue_id}/dismiss", dependencies=[RequireCsrfDependency], response_model=None)
async def post_dismiss_issue(
    issue_id: uuid.UUID,
    request: Request,
    body: CloseIssueRequest,
    session: DbSession,
    session_data: CurrentSession,
    clock: ClockDep,
) -> IssueResponse | JSONResponse:
    return await _run(
        request,
        session,
        session_data,
        clock,
        lambda: IssueTransitionService.dismiss(
            session,
            actor_id=session_data.user_id,
            issue_id=issue_id,
            expected_version=body.expected_version,
            note=body.note,
            clock=clock,
        ),
    )

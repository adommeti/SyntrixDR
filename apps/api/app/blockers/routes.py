from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.blockers.models import Blocker
from app.blockers.queries import list_blockers
from app.blockers.schemas import (
    AssignBlockerRequest,
    BlockerListResponse,
    BlockerResponse,
    BlockerStatus,
    ResolveBlockerRequest,
    StartBlockerRequest,
    VerifyBlockerRequest,
)
from app.blockers.transition_service import BlockerTransitionService
from app.core.idempotency import complete, require_idempotency_key
from app.dr_events.errors import DrEventNotFoundError
from app.dr_events.queries import get_visible_event
from app.identity_auth.dependencies import ClockDep, CurrentSession, DbSession, RequireCsrfDependency

router = APIRouter(prefix="/api/v1", tags=["blockers"])


async def _run_transition(
    request: Request,
    session: DbSession,
    session_data: CurrentSession,
    clock: ClockDep,
    call: Callable[[], Awaitable[Blocker]],
) -> BlockerResponse | JSONResponse:
    """D-215: replay returns the stored response before any business logic runs."""
    ctx = await require_idempotency_key(request, session, session_data.user_id, clock)
    if ctx.is_replay:
        assert ctx.stored_status is not None
        return JSONResponse(status_code=ctx.stored_status, content=ctx.stored_body)

    blocker = await call()
    response = BlockerResponse.model_validate(blocker)
    await complete(session, ctx, 200, response.model_dump(mode="json"))
    await session.commit()
    return response


@router.get("/dr-events/{event_id}/blockers")
async def list_blockers_route(
    event_id: uuid.UUID,
    session: DbSession,
    session_data: CurrentSession,
    status: BlockerStatus | None = None,
    team_id: uuid.UUID | None = None,
    task_id: uuid.UUID | None = None,
    include_closed: bool = False,
) -> BlockerListResponse:
    """API_CONTRACT.md:180 -- active list by default; `team_id` is a Team's queue (FROZEN §108)."""
    if await get_visible_event(session, session_data.user_id, event_id) is None:
        raise DrEventNotFoundError()
    rows = await list_blockers(
        session, event_id, status=status, team_id=team_id, task_id=task_id, include_closed=include_closed
    )
    return BlockerListResponse(blockers=[BlockerResponse.model_validate(b) for b in rows])


@router.post("/blockers/{blocker_id}/assign", dependencies=[RequireCsrfDependency], response_model=None)
async def post_assign_blocker(
    blocker_id: uuid.UUID,
    request: Request,
    body: AssignBlockerRequest,
    session: DbSession,
    session_data: CurrentSession,
    clock: ClockDep,
) -> BlockerResponse | JSONResponse:
    return await _run_transition(
        request,
        session,
        session_data,
        clock,
        lambda: BlockerTransitionService.assign(
            session,
            actor_id=session_data.user_id,
            blocker_id=blocker_id,
            expected_version=body.expected_version,
            team_id=body.team_id,
            assignee_user_id=body.assignee_user_id,
            clock=clock,
        ),
    )


@router.post("/blockers/{blocker_id}/start", dependencies=[RequireCsrfDependency], response_model=None)
async def post_start_blocker(
    blocker_id: uuid.UUID,
    request: Request,
    body: StartBlockerRequest,
    session: DbSession,
    session_data: CurrentSession,
    clock: ClockDep,
) -> BlockerResponse | JSONResponse:
    """API_CONTRACT.md:182 -- the route D-211 added."""
    return await _run_transition(
        request,
        session,
        session_data,
        clock,
        lambda: BlockerTransitionService.start(
            session,
            actor_id=session_data.user_id,
            blocker_id=blocker_id,
            expected_version=body.expected_version,
            clock=clock,
        ),
    )


@router.post("/blockers/{blocker_id}/resolve", dependencies=[RequireCsrfDependency], response_model=None)
async def post_resolve_blocker(
    blocker_id: uuid.UUID,
    request: Request,
    body: ResolveBlockerRequest,
    session: DbSession,
    session_data: CurrentSession,
    clock: ClockDep,
) -> BlockerResponse | JSONResponse:
    return await _run_transition(
        request,
        session,
        session_data,
        clock,
        lambda: BlockerTransitionService.resolve(
            session,
            actor_id=session_data.user_id,
            blocker_id=blocker_id,
            expected_version=body.expected_version,
            resolution_note=body.resolution_note,
            clock=clock,
        ),
    )


@router.post("/blockers/{blocker_id}/verify", dependencies=[RequireCsrfDependency], response_model=None)
async def post_verify_blocker(
    blocker_id: uuid.UUID,
    request: Request,
    body: VerifyBlockerRequest,
    session: DbSession,
    session_data: CurrentSession,
    clock: ClockDep,
) -> BlockerResponse | JSONResponse:
    """API_CONTRACT.md:184 -- VERIFIED then CLOSED atomically; may resume the Task (D-252)."""
    return await _run_transition(
        request,
        session,
        session_data,
        clock,
        lambda: BlockerTransitionService.verify(
            session,
            actor_id=session_data.user_id,
            blocker_id=blocker_id,
            expected_version=body.expected_version,
            clock=clock,
        ),
    )

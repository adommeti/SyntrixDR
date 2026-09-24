from __future__ import annotations

import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.core.idempotency import complete, require_idempotency_key
from app.dr_events.commands import DrEventNotFoundError
from app.dr_events.queries import get_visible_event
from app.identity_auth.dependencies import ClockDep, CurrentSession, DbSession, RequireCsrfDependency
from app.work_streams.commands import create_work_stream
from app.work_streams.queries import list_work_streams
from app.work_streams.schemas import CreateWorkStreamRequest, WorkStreamListResponse, WorkStreamResponse

router = APIRouter(prefix="/api/v1", tags=["work_streams"])


@router.get("/dr-events/{event_id}/work-streams")
async def list_work_streams_route(
    event_id: uuid.UUID, session: DbSession, session_data: CurrentSession
) -> WorkStreamListResponse:
    if await get_visible_event(session, session_data.user_id, event_id) is None:
        raise DrEventNotFoundError()
    streams = await list_work_streams(session, event_id)
    return WorkStreamListResponse(work_streams=[WorkStreamResponse.model_validate(s) for s in streams])


@router.post(
    "/dr-events/{event_id}/work-streams",
    status_code=201,
    dependencies=[RequireCsrfDependency],
    response_model=None,
    responses={201: {"model": WorkStreamResponse}},
)
async def post_create_work_stream(
    event_id: uuid.UUID,
    request: Request,
    body: CreateWorkStreamRequest,
    session: DbSession,
    session_data: CurrentSession,
    clock: ClockDep,
) -> WorkStreamResponse | JSONResponse:
    ctx = await require_idempotency_key(request, session, session_data.user_id, clock)
    if ctx.is_replay:
        assert ctx.stored_status is not None
        return JSONResponse(status_code=ctx.stored_status, content=ctx.stored_body)

    work_stream = await create_work_stream(
        session,
        actor_id=session_data.user_id,
        dr_event_id=event_id,
        name=body.name,
        stream_type=body.stream_type,
        description=body.description,
        lead_user_id=body.lead_user_id,
        owning_team_id=body.owning_team_id,
        sequence_order=body.sequence_order,
        clock=clock,
    )
    response = WorkStreamResponse.model_validate(work_stream)
    await complete(session, ctx, 201, response.model_dump(mode="json"))
    await session.commit()
    return response

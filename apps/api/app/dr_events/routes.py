from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.core.idempotency import complete, require_idempotency_key
from app.dr_events.commands import DrEventNotFoundError, create_event
from app.dr_events.models import DrEvent
from app.dr_events.queries import get_visible_event, list_visible_events
from app.dr_events.schemas import (
    CancelEventRequest,
    CreateDrEventRequest,
    DrEventListResponse,
    DrEventResponse,
    ExpectedVersionRequest,
    StartFailoverRequest,
)
from app.dr_events.transition_service import DrEventTransitionService
from app.identity_auth.dependencies import ClockDep, CurrentSession, DbSession, RequireCsrfDependency

router = APIRouter(prefix="/api/v1", tags=["dr_events"])


def _event_response(event: DrEvent) -> DrEventResponse:
    return DrEventResponse(
        id=event.id,
        parent_dr_event_id=event.parent_dr_event_id,
        name=event.name,
        event_type=event.event_type,
        description=event.description,
        status=event.status,
        coordinator_user_id=event.coordinator_user_id,
        source_location=event.source_location,
        target_location=event.target_location,
        planned_start_at=event.planned_start_at,
        event_timezone=event.event_timezone,
        network_cut_at=event.network_cut_at,
        failback_started_at=event.failback_started_at,
        completed_at=event.completed_at,
        cancelled_at=event.cancelled_at,
        cancel_reason=event.cancel_reason,
        ai_control_profile=event.ai_control_profile,
        baseline_plan_version_id=event.baseline_plan_version_id,
        health_score=event.health_score,
        version=event.version,
        created_at=event.created_at,
        updated_at=event.updated_at,
        deleted_at=event.deleted_at,
    )


@router.post("/dr-events", status_code=201, dependencies=[RequireCsrfDependency], response_model=None)
async def post_create_event(
    request: Request,
    body: CreateDrEventRequest,
    session: DbSession,
    session_data: CurrentSession,
    clock: ClockDep,
) -> DrEventResponse | JSONResponse:
    ctx = await require_idempotency_key(request, session, session_data.user_id, clock)
    if ctx.is_replay:
        assert ctx.stored_status is not None
        return JSONResponse(status_code=ctx.stored_status, content=ctx.stored_body)

    event = await create_event(
        session,
        actor_id=session_data.user_id,
        name=body.name,
        event_type=body.event_type,
        description=body.description,
        application_ids=body.application_ids,
        plan_id=body.plan_id,
        plan_version_id=body.plan_version_id,
        parent_dr_event_id=body.parent_dr_event_id,
        clock=clock,
    )
    response = _event_response(event)
    await complete(session, ctx, 201, response.model_dump(mode="json"))
    await session.commit()
    return response


@router.get("/dr-events")
async def list_events_route(session: DbSession, session_data: CurrentSession) -> DrEventListResponse:
    events = await list_visible_events(session, session_data.user_id)
    return DrEventListResponse(events=[_event_response(e) for e in events])


@router.get("/dr-events/{event_id}")
async def get_event_route(
    event_id: uuid.UUID, session: DbSession, session_data: CurrentSession
) -> DrEventResponse:
    event = await get_visible_event(session, session_data.user_id, event_id)
    if event is None:
        raise DrEventNotFoundError()
    return _event_response(event)


async def _run_transition(
    request: Request,
    session: DbSession,
    session_data: CurrentSession,
    clock: ClockDep,
    call: Callable[[], Awaitable[DrEvent]],
) -> DrEventResponse | JSONResponse:
    ctx = await require_idempotency_key(request, session, session_data.user_id, clock)
    if ctx.is_replay:
        assert ctx.stored_status is not None
        return JSONResponse(status_code=ctx.stored_status, content=ctx.stored_body)

    event = await call()
    response = _event_response(event)
    await complete(session, ctx, 200, response.model_dump(mode="json"))
    await session.commit()
    return response


@router.post("/dr-events/{event_id}/activate", dependencies=[RequireCsrfDependency], response_model=None)
async def post_activate_event(
    event_id: uuid.UUID,
    request: Request,
    body: ExpectedVersionRequest,
    session: DbSession,
    session_data: CurrentSession,
    clock: ClockDep,
) -> DrEventResponse | JSONResponse:
    return await _run_transition(
        request,
        session,
        session_data,
        clock,
        lambda: DrEventTransitionService.activate(
            session,
            actor_id=session_data.user_id,
            event_id=event_id,
            expected_version=body.expected_version,
            clock=clock,
        ),
    )


@router.post(
    "/dr-events/{event_id}/start-failover", dependencies=[RequireCsrfDependency], response_model=None
)
async def post_start_failover(
    event_id: uuid.UUID,
    request: Request,
    body: StartFailoverRequest,
    session: DbSession,
    session_data: CurrentSession,
    clock: ClockDep,
) -> DrEventResponse | JSONResponse:
    return await _run_transition(
        request,
        session,
        session_data,
        clock,
        lambda: DrEventTransitionService.start_failover(
            session,
            actor_id=session_data.user_id,
            event_id=event_id,
            expected_version=body.expected_version,
            network_cut_at=body.network_cut_at,
            clock=clock,
        ),
    )


@router.post(
    "/dr-events/{event_id}/mark-failed-over", dependencies=[RequireCsrfDependency], response_model=None
)
async def post_mark_failed_over(
    event_id: uuid.UUID,
    request: Request,
    body: ExpectedVersionRequest,
    session: DbSession,
    session_data: CurrentSession,
    clock: ClockDep,
) -> DrEventResponse | JSONResponse:
    return await _run_transition(
        request,
        session,
        session_data,
        clock,
        lambda: DrEventTransitionService.mark_failed_over(
            session,
            actor_id=session_data.user_id,
            event_id=event_id,
            expected_version=body.expected_version,
            clock=clock,
        ),
    )


@router.post(
    "/dr-events/{event_id}/start-failback", dependencies=[RequireCsrfDependency], response_model=None
)
async def post_start_failback(
    event_id: uuid.UUID,
    request: Request,
    body: ExpectedVersionRequest,
    session: DbSession,
    session_data: CurrentSession,
    clock: ClockDep,
) -> DrEventResponse | JSONResponse:
    return await _run_transition(
        request,
        session,
        session_data,
        clock,
        lambda: DrEventTransitionService.start_failback(
            session,
            actor_id=session_data.user_id,
            event_id=event_id,
            expected_version=body.expected_version,
            clock=clock,
        ),
    )


@router.post("/dr-events/{event_id}/close", dependencies=[RequireCsrfDependency], response_model=None)
async def post_close_event(
    event_id: uuid.UUID,
    request: Request,
    body: ExpectedVersionRequest,
    session: DbSession,
    session_data: CurrentSession,
    clock: ClockDep,
) -> DrEventResponse | JSONResponse:
    return await _run_transition(
        request,
        session,
        session_data,
        clock,
        lambda: DrEventTransitionService.close(
            session,
            actor_id=session_data.user_id,
            event_id=event_id,
            expected_version=body.expected_version,
            clock=clock,
        ),
    )


@router.post("/dr-events/{event_id}/cancel", dependencies=[RequireCsrfDependency], response_model=None)
async def post_cancel_event(
    event_id: uuid.UUID,
    request: Request,
    body: CancelEventRequest,
    session: DbSession,
    session_data: CurrentSession,
    clock: ClockDep,
) -> DrEventResponse | JSONResponse:
    return await _run_transition(
        request,
        session,
        session_data,
        clock,
        lambda: DrEventTransitionService.cancel(
            session,
            actor_id=session_data.user_id,
            event_id=event_id,
            expected_version=body.expected_version,
            reason=body.reason,
            clock=clock,
        ),
    )

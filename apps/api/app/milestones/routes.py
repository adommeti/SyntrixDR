from __future__ import annotations

import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.core.idempotency import complete, require_idempotency_key
from app.dr_events.errors import DrEventNotFoundError
from app.dr_events.queries import get_visible_event
from app.identity_auth.dependencies import ClockDep, CurrentSession, DbSession, RequireCsrfDependency
from app.milestones.commands import create_milestone
from app.milestones.models import Milestone
from app.milestones.queries import contributions_by_milestone, list_milestones
from app.milestones.schemas import (
    ConfirmMilestoneRequest,
    CreateMilestoneRequest,
    MilestoneContributionResponse,
    MilestoneGateResponse,
    MilestoneListResponse,
    MilestoneResponse,
)
from app.milestones.transition_service import MilestoneTransitionService
from app.tasks_dependencies.queries import gates_by_milestone

router = APIRouter(prefix="/api/v1", tags=["milestones"])


async def _responses(session: DbSession, milestones: list[Milestone]) -> list[MilestoneResponse]:
    ids = [m.id for m in milestones]
    contributions = await contributions_by_milestone(session, ids)
    gates = await gates_by_milestone(session, ids)
    return [
        MilestoneResponse(
            id=m.id,
            dr_event_id=m.dr_event_id,
            work_stream_id=m.work_stream_id,
            dr_application_id=m.dr_application_id,
            name=m.name,
            description=m.description,
            status=m.status,  # pyright: ignore[reportArgumentType]  # DB enum == MilestoneStatus
            owner_user_id=m.owner_user_id,
            confirmation_mode=m.confirmation_mode,  # pyright: ignore[reportArgumentType]
            ready_for_confirmation_at=m.ready_for_confirmation_at,
            achieved_at=m.achieved_at,
            target_at=m.target_at,
            version=m.version,
            created_at=m.created_at,
            updated_at=m.updated_at,
            contributing_tasks=[
                MilestoneContributionResponse(task_id=c.task_id, is_required=c.is_required)
                for c in contributions[m.id]
            ],
            gates=[
                MilestoneGateResponse(
                    milestone_dependency_id=g.id,
                    task_id=g.successor_task_id,
                    strength=g.strength,  # pyright: ignore[reportArgumentType]
                )
                for g in gates[m.id]
            ],
        )
        for m in milestones
    ]


@router.get("/dr-events/{event_id}/milestones")
async def list_milestones_route(
    event_id: uuid.UUID, session: DbSession, session_data: CurrentSession
) -> MilestoneListResponse:
    if await get_visible_event(session, session_data.user_id, event_id) is None:
        raise DrEventNotFoundError()
    return MilestoneListResponse(
        milestones=await _responses(session, await list_milestones(session, event_id))
    )


@router.post(
    "/dr-events/{event_id}/milestones",
    status_code=201,
    dependencies=[RequireCsrfDependency],
    response_model=None,
    responses={201: {"model": MilestoneResponse}},
)
async def post_create_milestone(
    event_id: uuid.UUID,
    request: Request,
    body: CreateMilestoneRequest,
    session: DbSession,
    session_data: CurrentSession,
    clock: ClockDep,
) -> MilestoneResponse | JSONResponse:
    ctx = await require_idempotency_key(request, session, session_data.user_id, clock)
    if ctx.is_replay:
        assert ctx.stored_status is not None
        return JSONResponse(status_code=ctx.stored_status, content=ctx.stored_body)

    milestone = await create_milestone(
        session,
        actor_id=session_data.user_id,
        dr_event_id=event_id,
        name=body.name,
        description=body.description,
        work_stream_id=body.work_stream_id,
        dr_application_id=body.dr_application_id,
        owner_user_id=body.owner_user_id,
        confirmation_mode=body.confirmation_mode,
        target_at=body.target_at,
        contributing_tasks=[(c.task_id, c.is_required) for c in body.contributing_tasks],
        gates=[(g.task_id, g.strength) for g in body.gates],
        clock=clock,
    )
    [response] = await _responses(session, [milestone])
    await complete(session, ctx, 201, response.model_dump(mode="json"))
    await session.commit()
    return JSONResponse(status_code=201, content=response.model_dump(mode="json"))


@router.post("/milestones/{milestone_id}/confirm", dependencies=[RequireCsrfDependency], response_model=None)
async def post_confirm_milestone(
    milestone_id: uuid.UUID,
    request: Request,
    body: ConfirmMilestoneRequest,
    session: DbSession,
    session_data: CurrentSession,
    clock: ClockDep,
) -> MilestoneResponse | JSONResponse:
    """API_CONTRACT.md:151 -- the human gate (D-225; also in the D-228 AI Act allowlist, which reaches
    this same command service)."""
    ctx = await require_idempotency_key(request, session, session_data.user_id, clock)
    if ctx.is_replay:
        assert ctx.stored_status is not None
        return JSONResponse(status_code=ctx.stored_status, content=ctx.stored_body)

    milestone = await MilestoneTransitionService.confirm(
        session,
        actor_id=session_data.user_id,
        milestone_id=milestone_id,
        expected_version=body.expected_version,
        clock=clock,
    )
    [response] = await _responses(session, [milestone])
    await complete(session, ctx, 200, response.model_dump(mode="json"))
    await session.commit()
    return response

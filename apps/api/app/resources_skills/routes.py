from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.core.errors import AppError
from app.core.idempotency import complete, require_idempotency_key
from app.dr_events.errors import DrEventNotFoundError
from app.dr_events.queries import get_visible_event
from app.identity_auth.dependencies import ClockDep, CurrentSession, DbSession, RequireCsrfDependency
from app.resources_skills.assignment_service import AssignmentService
from app.resources_skills.queries import event_resources, may_view_team_workload, team_workload
from app.resources_skills.schemas import (
    AssignTaskRequest,
    EventPersonResponse,
    EventResourcesResponse,
    OpenTaskCounts,
    TeamLoadResponse,
    TeamMemberLoadResponse,
    TeamRef,
    TeamWorkloadResponse,
    VolunteerTaskRequest,
)
from app.tasks_dependencies.models import Task
from app.tasks_dependencies.schemas import TaskResponse
from app.users_teams_org.authorization import AuthorizationRequiredError

router = APIRouter(prefix="/api/v1", tags=["resources_skills"])


class TeamNotFoundError(AppError):
    code = "TEAM_NOT_FOUND"
    status_code = 404

    def __init__(self) -> None:
        super().__init__("Team not found.")


async def _command(
    request: Request,
    session: DbSession,
    session_data: CurrentSession,
    clock: ClockDep,
    call: Callable[[], Awaitable[Task]],
) -> TaskResponse | JSONResponse:
    """D-215: replay first, then the command, then store the response and commit."""
    ctx = await require_idempotency_key(request, session, session_data.user_id, clock)
    if ctx.is_replay:
        assert ctx.stored_status is not None
        return JSONResponse(status_code=ctx.stored_status, content=ctx.stored_body)
    response = TaskResponse.model_validate(await call())
    await complete(session, ctx, 200, response.model_dump(mode="json"))
    await session.commit()
    return response


@router.post("/tasks/{task_id}/assign", dependencies=[RequireCsrfDependency], response_model=None)
async def post_assign_task(
    task_id: uuid.UUID,
    request: Request,
    body: AssignTaskRequest,
    session: DbSession,
    session_data: CurrentSession,
    clock: ClockDep,
) -> TaskResponse | JSONResponse:
    """API_CONTRACT.md:165. A D-214 Manager-precedence write is 200, never 409."""
    return await _command(
        request,
        session,
        session_data,
        clock,
        lambda: AssignmentService.assign(
            session,
            actor_id=session_data.user_id,
            task_id=task_id,
            assignee_user_id=body.assignee_user_id,
            expected_version=body.expected_version,
            clock=clock,
        ),
    )


@router.post("/tasks/{task_id}/volunteer", dependencies=[RequireCsrfDependency], response_model=None)
async def post_volunteer_task(
    task_id: uuid.UUID,
    request: Request,
    body: VolunteerTaskRequest,
    session: DbSession,
    session_data: CurrentSession,
    clock: ClockDep,
) -> TaskResponse | JSONResponse:
    return await _command(
        request,
        session,
        session_data,
        clock,
        lambda: AssignmentService.volunteer(
            session,
            actor_id=session_data.user_id,
            task_id=task_id,
            expected_version=body.expected_version,
            clock=clock,
        ),
    )


def _counts(counts: dict[str, int]) -> OpenTaskCounts:
    return OpenTaskCounts.model_validate(counts)


@router.get("/teams/{team_id}/workload")
async def get_team_workload_route(
    team_id: uuid.UUID, session: DbSession, session_data: CurrentSession, clock: ClockDep
) -> TeamWorkloadResponse:
    workload = await team_workload(session, viewer_id=session_data.user_id, team_id=team_id, at=clock.now())
    if workload is None:
        raise TeamNotFoundError()
    if not await may_view_team_workload(session, session_data.user_id, team_id, at=clock.now()):
        raise AuthorizationRequiredError()
    return TeamWorkloadResponse(
        team_id=workload.team.team_id,
        team_name=workload.team.name,
        member_count=workload.member_count,
        owned_open_tasks=_counts(workload.team.counts),
        unassigned_open=workload.team.unassigned_open,
        members=[
            TeamMemberLoadResponse(
                user_id=m.user_id,
                display_name=m.display_name,
                open_tasks=_counts(m.counts),
                open_total=m.open_total,
                is_manager=m.is_manager,
            )
            for m in workload.members
        ],
    )


@router.get("/dr-events/{event_id}/resources")
async def get_event_resources_route(
    event_id: uuid.UUID, session: DbSession, session_data: CurrentSession, clock: ClockDep
) -> EventResourcesResponse:
    if await get_visible_event(session, session_data.user_id, event_id) is None:
        raise DrEventNotFoundError()
    resources = await event_resources(session, event_id, at=clock.now())
    return EventResourcesResponse(
        dr_event_id=event_id,
        people=[
            EventPersonResponse(
                user_id=p.user_id,
                display_name=p.display_name,
                open_tasks=_counts(p.counts),
                open_total=p.open_total,
                teams=[TeamRef(team_id=t, name=n) for t, n in p.teams],
                skills=p.skills,
            )
            for p in resources.people
        ],
        teams=[
            TeamLoadResponse(
                team_id=t.team_id,
                name=t.name,
                open_tasks=_counts(t.counts),
                unassigned_open=t.unassigned_open,
            )
            for t in resources.teams
        ],
    )

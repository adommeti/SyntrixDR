from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.core.idempotency import complete, require_idempotency_key
from app.dr_events.commands import DrEventNotFoundError
from app.dr_events.queries import get_visible_event
from app.identity_auth.dependencies import ClockDep, CurrentSession, DbSession, RequireCsrfDependency
from app.tasks_dependencies.commands import TaskNotFoundError, create_task, update_task_metadata
from app.tasks_dependencies.dependency_service import DependencyService
from app.tasks_dependencies.models import Task, TaskDependency
from app.tasks_dependencies.queries import get_visible_dependency_graph, get_visible_task, list_tasks
from app.tasks_dependencies.schemas import (
    BlockTaskRequest,
    CancelTaskRequest,
    CreateTaskDependencyRequest,
    CreateTaskRequest,
    DependencyGraphResponse,
    ResumeTaskRequest,
    StartTaskRequest,
    SubmitValidationRequest,
    TaskDependencyResponse,
    TaskListResponse,
    TaskResponse,
    UpdateTaskRequest,
    ValidateTaskRequest,
)
from app.tasks_dependencies.transition_service import TaskTransitionService
from app.validation_evidence.ports import EvidenceCounterDep

router = APIRouter(prefix="/api/v1", tags=["tasks_dependencies"])


async def _run_transition(
    request: Request,
    session: DbSession,
    session_data: CurrentSession,
    clock: ClockDep,
    call: Callable[[], Awaitable[Task]],
) -> TaskResponse | JSONResponse:
    """D-215: replay returns the stored response before any business logic runs; otherwise the
    command runs, its response is stored, and the route commits (transition step 8)."""
    ctx = await require_idempotency_key(request, session, session_data.user_id, clock)
    if ctx.is_replay:
        assert ctx.stored_status is not None
        return JSONResponse(status_code=ctx.stored_status, content=ctx.stored_body)

    task = await call()
    response = TaskResponse.model_validate(task)
    await complete(session, ctx, 200, response.model_dump(mode="json"))
    await session.commit()
    return response


@router.get("/tasks/{task_id}")
async def get_task_route(
    task_id: uuid.UUID, session: DbSession, session_data: CurrentSession
) -> TaskResponse:
    task = await get_visible_task(session, session_data.user_id, task_id)
    if task is None:
        raise TaskNotFoundError()
    return TaskResponse.model_validate(task)


@router.patch("/tasks/{task_id}", dependencies=[RequireCsrfDependency], response_model=None)
async def patch_task(
    task_id: uuid.UUID,
    request: Request,
    body: UpdateTaskRequest,
    session: DbSession,
    session_data: CurrentSession,
    clock: ClockDep,
) -> TaskResponse | JSONResponse:
    """API_CONTRACT.md:158 -- non-state Task metadata; only the fields present in the body change."""
    changes = {name: getattr(body, name) for name in body.model_fields_set - {"expected_version"}}
    return await _run_transition(
        request,
        session,
        session_data,
        clock,
        lambda: update_task_metadata(
            session,
            actor_id=session_data.user_id,
            task_id=task_id,
            expected_version=body.expected_version,
            changes=changes,
            clock=clock,
        ),
    )


@router.post("/tasks/{task_id}/start", dependencies=[RequireCsrfDependency], response_model=None)
async def post_start_task(
    task_id: uuid.UUID,
    request: Request,
    body: StartTaskRequest,
    session: DbSession,
    session_data: CurrentSession,
    clock: ClockDep,
) -> TaskResponse | JSONResponse:
    return await _run_transition(
        request,
        session,
        session_data,
        clock,
        lambda: TaskTransitionService.start(
            session,
            actor_id=session_data.user_id,
            task_id=task_id,
            expected_version=body.expected_version,
            override_reason=body.override_reason,
            clock=clock,
        ),
    )


@router.post("/tasks/{task_id}/block", dependencies=[RequireCsrfDependency], response_model=None)
async def post_block_task(
    task_id: uuid.UUID,
    request: Request,
    body: BlockTaskRequest,
    session: DbSession,
    session_data: CurrentSession,
    clock: ClockDep,
) -> TaskResponse | JSONResponse:
    return await _run_transition(
        request,
        session,
        session_data,
        clock,
        lambda: TaskTransitionService.block(
            session,
            actor_id=session_data.user_id,
            task_id=task_id,
            expected_version=body.expected_version,
            reason=body.reason,
            clock=clock,
        ),
    )


@router.post("/tasks/{task_id}/resume", dependencies=[RequireCsrfDependency], response_model=None)
async def post_resume_task(
    task_id: uuid.UUID,
    request: Request,
    body: ResumeTaskRequest,
    session: DbSession,
    session_data: CurrentSession,
    clock: ClockDep,
) -> TaskResponse | JSONResponse:
    return await _run_transition(
        request,
        session,
        session_data,
        clock,
        lambda: TaskTransitionService.resume(
            session,
            actor_id=session_data.user_id,
            task_id=task_id,
            expected_version=body.expected_version,
            override_reason=body.override_reason,
            clock=clock,
        ),
    )


@router.post("/tasks/{task_id}/submit-validation", dependencies=[RequireCsrfDependency], response_model=None)
async def post_submit_task_validation(
    task_id: uuid.UUID,
    request: Request,
    body: SubmitValidationRequest,
    session: DbSession,
    session_data: CurrentSession,
    clock: ClockDep,
    evidence_counter: EvidenceCounterDep,
) -> TaskResponse | JSONResponse:
    return await _run_transition(
        request,
        session,
        session_data,
        clock,
        lambda: TaskTransitionService.submit_validation(
            session,
            actor_id=session_data.user_id,
            task_id=task_id,
            expected_version=body.expected_version,
            verification_note=body.verification_note,
            evidence_counter=evidence_counter,
            clock=clock,
        ),
    )


@router.post("/tasks/{task_id}/validate", dependencies=[RequireCsrfDependency], response_model=None)
async def post_validate_task(
    task_id: uuid.UUID,
    request: Request,
    body: ValidateTaskRequest,
    session: DbSession,
    session_data: CurrentSession,
    clock: ClockDep,
) -> TaskResponse | JSONResponse:
    return await _run_transition(
        request,
        session,
        session_data,
        clock,
        lambda: TaskTransitionService.validate(
            session,
            actor_id=session_data.user_id,
            task_id=task_id,
            expected_version=body.expected_version,
            approve=body.approve,
            note=body.note,
            clock=clock,
        ),
    )


@router.post("/tasks/{task_id}/cancel", dependencies=[RequireCsrfDependency], response_model=None)
async def post_cancel_task(
    task_id: uuid.UUID,
    request: Request,
    body: CancelTaskRequest,
    session: DbSession,
    session_data: CurrentSession,
    clock: ClockDep,
) -> TaskResponse | JSONResponse:
    return await _run_transition(
        request,
        session,
        session_data,
        clock,
        lambda: TaskTransitionService.cancel(
            session,
            actor_id=session_data.user_id,
            task_id=task_id,
            expected_version=body.expected_version,
            reason=body.reason,
            clock=clock,
        ),
    )


# --------------------------------------------------------------------------------------------
# Dependencies (API_CONTRACT.md:168-174)
# --------------------------------------------------------------------------------------------


async def _run_dependency_command(
    request: Request,
    session: DbSession,
    session_data: CurrentSession,
    clock: ClockDep,
    status_code: int,
    call: Callable[[], Awaitable[TaskDependency]],
) -> TaskDependencyResponse | JSONResponse:
    ctx = await require_idempotency_key(request, session, session_data.user_id, clock)
    if ctx.is_replay:
        assert ctx.stored_status is not None
        return JSONResponse(status_code=ctx.stored_status, content=ctx.stored_body)

    edge = await call()
    response = TaskDependencyResponse.model_validate(edge)
    await complete(session, ctx, status_code, response.model_dump(mode="json"))
    await session.commit()
    return response


# `response_model=None` lets an idempotent replay return the stored JSONResponse; `responses=` still
# documents the body in the contract, which nothing else references (no GET for a single edge).
@router.post(
    "/task-dependencies",
    status_code=201,
    dependencies=[RequireCsrfDependency],
    response_model=None,
    responses={201: {"model": TaskDependencyResponse}},
)
async def post_create_task_dependency(
    request: Request,
    body: CreateTaskDependencyRequest,
    session: DbSession,
    session_data: CurrentSession,
    clock: ClockDep,
) -> TaskDependencyResponse | JSONResponse:
    return await _run_dependency_command(
        request,
        session,
        session_data,
        clock,
        201,
        lambda: DependencyService.add_task_dependency(
            session,
            actor_id=session_data.user_id,
            predecessor_task_id=body.predecessor_task_id,
            successor_task_id=body.successor_task_id,
            strength=body.strength,
            clock=clock,
        ),
    )


@router.delete(
    "/task-dependencies/{dependency_id}",
    dependencies=[RequireCsrfDependency],
    response_model=None,
    responses={200: {"model": TaskDependencyResponse}},
)
async def delete_task_dependency(
    dependency_id: uuid.UUID,
    request: Request,
    session: DbSession,
    session_data: CurrentSession,
    clock: ClockDep,
) -> TaskDependencyResponse | JSONResponse:
    """200 with the removed edge, not 204: the idempotent replay (D-215, required here as the stricter
    reading of "command POST") has to return a stored body (BUILD-06.plan.md Risk #15)."""
    return await _run_dependency_command(
        request,
        session,
        session_data,
        clock,
        200,
        lambda: DependencyService.remove_task_dependency(
            session, actor_id=session_data.user_id, dependency_id=dependency_id, clock=clock
        ),
    )


@router.get("/dr-events/{event_id}/dependency-graph")
async def get_dependency_graph_route(
    event_id: uuid.UUID, session: DbSession, session_data: CurrentSession
) -> DependencyGraphResponse:
    graph = await get_visible_dependency_graph(session, session_data.user_id, event_id)
    if graph is None:
        raise DrEventNotFoundError()
    return DependencyGraphResponse.model_validate(graph)


# --------------------------------------------------------------------------------------------
# Query / create Tasks (API_CONTRACT.md:157)
# --------------------------------------------------------------------------------------------


@router.get("/dr-events/{event_id}/tasks")
async def list_tasks_route(
    event_id: uuid.UUID, session: DbSession, session_data: CurrentSession
) -> TaskListResponse:
    if await get_visible_event(session, session_data.user_id, event_id) is None:
        raise DrEventNotFoundError()
    return TaskListResponse(
        tasks=[TaskResponse.model_validate(t) for t in await list_tasks(session, event_id)]
    )


@router.post(
    "/dr-events/{event_id}/tasks",
    status_code=201,
    dependencies=[RequireCsrfDependency],
    response_model=None,
    responses={201: {"model": TaskResponse}},
)
async def post_create_task(
    event_id: uuid.UUID,
    request: Request,
    body: CreateTaskRequest,
    session: DbSession,
    session_data: CurrentSession,
    clock: ClockDep,
) -> TaskResponse | JSONResponse:
    ctx = await require_idempotency_key(request, session, session_data.user_id, clock)
    if ctx.is_replay:
        assert ctx.stored_status is not None
        return JSONResponse(status_code=ctx.stored_status, content=ctx.stored_body)

    task = await create_task(
        session,
        actor_id=session_data.user_id,
        dr_event_id=event_id,
        title=body.title,
        phase=body.phase,
        owning_team_id=body.owning_team_id,
        dr_application_id=body.dr_application_id,
        work_stream_id=body.work_stream_id,
        parent_task_id=body.parent_task_id,
        description=body.description,
        expected_duration_minutes=body.expected_duration_minutes,
        sort_order=body.sort_order,
        evidence_required=body.evidence_required,
        evidence_min_count=body.evidence_min_count,
        verification_note_required=body.verification_note_required,
        needs_specific_validation=body.needs_specific_validation,
        clock=clock,
    )
    response = TaskResponse.model_validate(task)
    await complete(session, ctx, 201, response.model_dump(mode="json"))
    await session.commit()
    return response

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.core.idempotency import complete, require_idempotency_key
from app.identity_auth.dependencies import ClockDep, CurrentSession, DbSession, RequireCsrfDependency
from app.tasks_dependencies.commands import TaskNotFoundError
from app.tasks_dependencies.models import Task
from app.tasks_dependencies.queries import get_visible_task
from app.tasks_dependencies.schemas import (
    BlockTaskRequest,
    CancelTaskRequest,
    ResumeTaskRequest,
    StartTaskRequest,
    SubmitValidationRequest,
    TaskResponse,
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

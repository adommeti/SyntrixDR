from __future__ import annotations

import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.core.idempotency import complete, require_idempotency_key
from app.identity_auth.dependencies import ClockDep, CurrentSession, DbSession, RequireCsrfDependency
from app.plans_import.commands import PlanNotFoundError, SnapshotItem, create_plan, create_plan_version
from app.plans_import.models import Plan, PlanVersion
from app.plans_import.queries import count_snapshot_rows, get_plan, list_plan_versions, list_plans
from app.plans_import.schemas import (
    CreatePlanRequest,
    CreatePlanVersionRequest,
    PlanDetailResponse,
    PlanListResponse,
    PlanResponse,
    PlanVersionResponse,
)

router = APIRouter(prefix="/api/v1", tags=["plans_import"])


def _plan_response(plan: Plan) -> PlanResponse:
    return PlanResponse(
        id=plan.id,
        name=plan.name,
        description=plan.description,
        plan_type=plan.plan_type,
        application_id=plan.application_id,
        source_type=plan.source_type,
        version=plan.version,
        created_at=plan.created_at,
        updated_at=plan.updated_at,
        deleted_at=plan.deleted_at,
    )


async def _plan_version_response(session: DbSession, version: PlanVersion) -> PlanVersionResponse:
    task_count, dependency_count, milestone_count = await count_snapshot_rows(session, version.id)
    return PlanVersionResponse(
        id=version.id,
        plan_id=version.plan_id,
        dr_event_id=version.dr_event_id,
        version_number=version.version_number,
        version_type=version.version_type,
        notes=version.notes,
        created_at=version.created_at,
        task_count=task_count,
        task_dependency_count=dependency_count,
        milestone_count=milestone_count,
    )


@router.post("/plans", status_code=201, dependencies=[RequireCsrfDependency], response_model=None)
async def post_create_plan(
    request: Request,
    body: CreatePlanRequest,
    session: DbSession,
    session_data: CurrentSession,
    clock: ClockDep,
) -> PlanResponse | JSONResponse:
    ctx = await require_idempotency_key(request, session, session_data.user_id, clock)
    if ctx.is_replay:
        assert ctx.stored_status is not None
        return JSONResponse(status_code=ctx.stored_status, content=ctx.stored_body)

    plan = await create_plan(
        session,
        actor_id=session_data.user_id,
        name=body.name,
        plan_type=body.plan_type,
        description=body.description,
        application_id=body.application_id,
    )
    response = _plan_response(plan)
    await complete(session, ctx, 201, response.model_dump(mode="json"))
    await session.commit()
    return response


@router.get("/plans")
async def list_plans_route(session: DbSession, session_data: CurrentSession) -> PlanListResponse:
    # Deliberately open to any authenticated user, unlike DR Events (which are participant-
    # scoped, D-222): a Plan is a reusable template with no Event/participant relationship of
    # its own until instantiated, so there is no visibility boundary to enforce here yet
    # (mirrors BUILD-03's Application-catalog-reads-are-open precedent). Found in review — flag
    # if a future increment gives Plans their own access-scoping rule.
    _ = session_data
    plans = await list_plans(session)
    return PlanListResponse(plans=[_plan_response(p) for p in plans])


@router.get("/plans/{plan_id}")
async def get_plan_route(
    plan_id: uuid.UUID, session: DbSession, session_data: CurrentSession
) -> PlanDetailResponse:
    _ = session_data
    plan = await get_plan(session, plan_id)
    if plan is None:
        raise PlanNotFoundError()
    versions = await list_plan_versions(session, plan_id)
    version_responses = [await _plan_version_response(session, v) for v in versions]
    return PlanDetailResponse(**_plan_response(plan).model_dump(), versions=version_responses)


@router.post(
    "/plans/{plan_id}/versions", status_code=201, dependencies=[RequireCsrfDependency], response_model=None
)
async def post_create_plan_version(
    plan_id: uuid.UUID,
    request: Request,
    body: CreatePlanVersionRequest,
    session: DbSession,
    session_data: CurrentSession,
    clock: ClockDep,
) -> PlanVersionResponse | JSONResponse:
    ctx = await require_idempotency_key(request, session, session_data.user_id, clock)
    if ctx.is_replay:
        assert ctx.stored_status is not None
        return JSONResponse(status_code=ctx.stored_status, content=ctx.stored_body)

    version = await create_plan_version(
        session,
        actor_id=session_data.user_id,
        plan_id=plan_id,
        version_type=body.version_type,
        notes=body.notes,
        tasks=[
            SnapshotItem(source_id=item.source_id, snapshot_data=item.snapshot_data) for item in body.tasks
        ],
        task_dependencies=[
            SnapshotItem(source_id=item.source_id, snapshot_data=item.snapshot_data)
            for item in body.task_dependencies
        ],
        milestones=[
            SnapshotItem(source_id=item.source_id, snapshot_data=item.snapshot_data)
            for item in body.milestones
        ],
    )
    response = await _plan_version_response(session, version)
    await complete(session, ctx, 201, response.model_dump(mode="json"))
    await session.commit()
    return response

from __future__ import annotations

import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.datastructures import UploadFile

from app.core.errors import IdempotencyKeyRequiredError
from app.core.idempotency import IDEMPOTENCY_HEADER, complete, hash_file_payload, require_idempotency_key
from app.identity_auth.dependencies import (
    ClockDep,
    CurrentSession,
    DbSession,
    ObjectStoreDep,
    RequireCsrfDependency,
)
from app.plans_import.commands import (
    PlanNotFoundError,
    SnapshotItem,
    create_import_job,
    create_plan,
    create_plan_version,
)
from app.plans_import.jobs import parse_import_excel
from app.plans_import.models import ImportJob, Plan, PlanVersion
from app.plans_import.queries import (
    count_snapshot_rows,
    get_plan,
    get_visible_import_job,
    list_plan_versions,
    list_plans,
)
from app.plans_import.schemas import (
    AcceptImportRequest,
    AcceptImportResponse,
    ColumnMappingResponse,
    CreatePlanRequest,
    CreatePlanVersionRequest,
    ImportJobDetailResponse,
    ImportJobResponse,
    ImportRowResponse,
    PlanDetailResponse,
    PlanListResponse,
    PlanResponse,
    PlanVersionResponse,
)
from app.plans_import.transition_service import ImportJobNotFoundError, ImportJobTransitionService

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


def _import_job_response(import_job: ImportJob) -> ImportJobResponse:
    return ImportJobResponse(
        id=import_job.id,
        dr_event_id=import_job.dr_event_id,
        status=import_job.status,
        source_file_uri=import_job.source_file_uri,
        error_data=import_job.error_data,
        created_at=import_job.created_at,
        started_at=import_job.started_at,
        completed_at=import_job.completed_at,
    )


def _import_job_detail_response(import_job: ImportJob) -> ImportJobDetailResponse:
    mapping_data = import_job.mapping_data or {}
    return ImportJobDetailResponse(
        **_import_job_response(import_job).model_dump(),
        headers=mapping_data.get("headers", []),
        proposed_mapping=[ColumnMappingResponse(**m) for m in mapping_data.get("proposed_mapping", [])],
        rows=[ImportRowResponse(**r) for r in mapping_data.get("rows", [])],
    )


@router.post(
    "/dr-events/{event_id}/imports/excel",
    status_code=201,
    dependencies=[RequireCsrfDependency],
    response_model=None,
)
async def post_upload_import_excel(
    event_id: uuid.UUID,
    request: Request,
    session: DbSession,
    session_data: CurrentSession,
    clock: ClockDep,
    object_store: ObjectStoreDep,
) -> ImportJobResponse | JSONResponse:
    # A `file: UploadFile = File(...)` parameter would parse the multipart body during FastAPI's
    # own dependency resolution, ahead of this function running at all, consuming the ASGI stream
    # before anything else could read it -- so the file is read manually here instead (found in
    # review -- the naive `File(...)` parameter raised "Stream consumed"). The idempotency hash is
    # computed from the parsed filename + content (`hash_file_payload`), not the raw multipart
    # wire body, since a real client's retry uses a different random multipart boundary each time.
    if not request.headers.get(IDEMPOTENCY_HEADER):
        raise IdempotencyKeyRequiredError

    form = await request.form()
    upload = form["file"]
    assert isinstance(upload, UploadFile)
    content = await upload.read()
    filename = upload.filename or "upload.xlsx"

    ctx = await require_idempotency_key(
        request,
        session,
        session_data.user_id,
        clock,
        request_hash=hash_file_payload(filename, content),
    )
    if ctx.is_replay:
        assert ctx.stored_status is not None
        return JSONResponse(status_code=ctx.stored_status, content=ctx.stored_body)

    import_job = await create_import_job(
        session,
        actor_id=session_data.user_id,
        dr_event_id=event_id,
        filename=filename,
        content=content,
        object_store=object_store,
        clock=clock,
    )
    response = _import_job_response(import_job)
    await complete(session, ctx, 201, response.model_dump(mode="json"))
    await session.commit()
    parse_import_excel.delay(  # pyright: ignore[reportUnknownMemberType, reportFunctionMemberAccess]
        str(import_job.id)
    )
    return response


@router.get("/imports/{import_job_id}")
async def get_import_job_route(
    import_job_id: uuid.UUID, session: DbSession, session_data: CurrentSession
) -> ImportJobDetailResponse:
    import_job = await get_visible_import_job(session, session_data.user_id, import_job_id)
    if import_job is None:
        raise ImportJobNotFoundError()
    return _import_job_detail_response(import_job)


@router.post("/imports/{import_job_id}/accept", dependencies=[RequireCsrfDependency], response_model=None)
async def post_accept_import(
    import_job_id: uuid.UUID,
    request: Request,
    body: AcceptImportRequest,
    session: DbSession,
    session_data: CurrentSession,
    clock: ClockDep,
) -> AcceptImportResponse | JSONResponse:
    ctx = await require_idempotency_key(request, session, session_data.user_id, clock)
    if ctx.is_replay:
        assert ctx.stored_status is not None
        return JSONResponse(status_code=ctx.stored_status, content=ctx.stored_body)

    confirmed_mapping = {m.source_header: m.target_field for m in body.mapping}
    import_job, summary = await ImportJobTransitionService.accept(
        session,
        actor_id=session_data.user_id,
        import_job_id=import_job_id,
        confirmed_mapping=confirmed_mapping,
        clock=clock,
    )
    response = AcceptImportResponse(
        import_job=_import_job_response(import_job),
        created_task_count=summary.created_task_count,
        created_dependency_count=summary.created_dependency_count,
        needs_review_count=summary.needs_review_count,
    )
    await complete(session, ctx, 200, response.model_dump(mode="json"))
    await session.commit()
    return response

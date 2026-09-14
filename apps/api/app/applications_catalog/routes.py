from __future__ import annotations

import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.applications_catalog.commands import (
    ApplicationNotFoundError,
    OwnerSlot,
    TierUpdate,
    create_application,
    set_application_owners,
    update_application,
    update_tiers,
)
from app.applications_catalog.models import Application, ApplicationOwner, Tier
from app.applications_catalog.queries import (
    get_application,
    get_application_history,
    list_application_owners,
    list_applications,
    list_tiers,
)
from app.applications_catalog.schemas import (
    ApplicationHistoryResponse,
    ApplicationListResponse,
    ApplicationOwnerResponse,
    ApplicationResponse,
    CreateApplicationRequest,
    SetApplicationOwnersRequest,
    TierListResponse,
    TierResponse,
    UpdateApplicationRequest,
    UpdateTiersRequest,
)
from app.core.idempotency import complete, require_idempotency_key
from app.identity_auth.dependencies import ClockDep, CurrentSession, DbSession, RequireCsrfDependency

router = APIRouter(prefix="/api/v1", tags=["applications_catalog"])


def _application_response(application: Application, owners: list[ApplicationOwner]) -> ApplicationResponse:
    return ApplicationResponse(
        id=application.id,
        name=application.name,
        description=application.description,
        tier_id=application.tier_id,
        external_system=application.external_system,
        external_id=application.external_id,
        version=application.version,
        created_at=application.created_at,
        updated_at=application.updated_at,
        deleted_at=application.deleted_at,
        owners=[
            ApplicationOwnerResponse(
                owner_type=owner.owner_type, owner_order=owner.owner_order, user_id=owner.user_id
            )
            for owner in owners
        ],
    )


def _tier_response(tier: Tier) -> TierResponse:
    return TierResponse(
        id=tier.id,
        code=tier.code,
        rank=tier.rank,
        default_sla_minutes=tier.default_sla_minutes,
        default_health_weight=float(tier.default_health_weight),
        description=tier.description,
        version=tier.version,
    )


@router.get("/applications")
async def list_applications_route(
    session: DbSession, session_data: CurrentSession
) -> ApplicationListResponse:
    _ = session_data
    applications = await list_applications(session)
    responses: list[ApplicationResponse] = []
    for application in applications:
        owners = await list_application_owners(session, application.id)
        responses.append(_application_response(application, owners))
    return ApplicationListResponse(applications=responses)


@router.get("/applications/{application_id}")
async def get_application_route(
    application_id: uuid.UUID, session: DbSession, session_data: CurrentSession
) -> ApplicationResponse:
    _ = session_data
    application = await get_application(session, application_id)
    if application is None:
        raise ApplicationNotFoundError()
    owners = await list_application_owners(session, application_id)
    return _application_response(application, owners)


@router.post("/applications", dependencies=[RequireCsrfDependency], response_model=None)
async def post_create_application(
    request: Request,
    body: CreateApplicationRequest,
    session: DbSession,
    session_data: CurrentSession,
    clock: ClockDep,
) -> ApplicationResponse | JSONResponse:
    ctx = await require_idempotency_key(request, session, session_data.user_id, clock)
    if ctx.is_replay:
        assert ctx.stored_status is not None
        return JSONResponse(status_code=ctx.stored_status, content=ctx.stored_body)

    application = await create_application(
        session,
        actor_id=session_data.user_id,
        name=body.name,
        tier_id=body.tier_id,
        description=body.description,
        external_system=body.external_system,
        external_id=body.external_id,
    )
    response = _application_response(application, [])
    await complete(session, ctx, 201, response.model_dump(mode="json"))
    await session.commit()
    return response


@router.patch("/applications/{application_id}", dependencies=[RequireCsrfDependency], response_model=None)
async def patch_application(
    application_id: uuid.UUID,
    request: Request,
    body: UpdateApplicationRequest,
    session: DbSession,
    session_data: CurrentSession,
    clock: ClockDep,
) -> ApplicationResponse | JSONResponse:
    ctx = await require_idempotency_key(request, session, session_data.user_id, clock)
    if ctx.is_replay:
        assert ctx.stored_status is not None
        return JSONResponse(status_code=ctx.stored_status, content=ctx.stored_body)

    application = await update_application(
        session,
        actor_id=session_data.user_id,
        application_id=application_id,
        expected_version=body.expected_version,
        name=body.name,
        description=body.description,
        tier_id=body.tier_id,
        external_system=body.external_system,
        external_id=body.external_id,
        clock=clock,
    )
    owners = await list_application_owners(session, application_id)
    response = _application_response(application, owners)
    await complete(session, ctx, 200, response.model_dump(mode="json"))
    await session.commit()
    return response


@router.put(
    "/applications/{application_id}/owners", dependencies=[RequireCsrfDependency], response_model=None
)
async def put_application_owners(
    application_id: uuid.UUID,
    request: Request,
    body: SetApplicationOwnersRequest,
    session: DbSession,
    session_data: CurrentSession,
    clock: ClockDep,
) -> ApplicationResponse | JSONResponse:
    ctx = await require_idempotency_key(request, session, session_data.user_id, clock)
    if ctx.is_replay:
        assert ctx.stored_status is not None
        return JSONResponse(status_code=ctx.stored_status, content=ctx.stored_body)

    await set_application_owners(
        session,
        actor_id=session_data.user_id,
        application_id=application_id,
        owners=[
            OwnerSlot(owner_type=item.owner_type, owner_order=item.owner_order, user_id=item.user_id)
            for item in body.owners
        ],
        clock=clock,
    )
    application = await get_application(session, application_id)
    assert application is not None
    owners = await list_application_owners(session, application_id)
    response = _application_response(application, owners)
    await complete(session, ctx, 200, response.model_dump(mode="json"))
    await session.commit()
    return response


@router.get("/applications/{application_id}/history")
async def get_application_history_route(
    application_id: uuid.UUID, session: DbSession, session_data: CurrentSession
) -> ApplicationHistoryResponse:
    _ = session_data
    items = await get_application_history(session, application_id)
    return ApplicationHistoryResponse(items=items)


@router.get("/admin/tiers")
async def list_tiers_route(session: DbSession, session_data: CurrentSession) -> TierListResponse:
    _ = session_data
    tiers = await list_tiers(session)
    return TierListResponse(tiers=[_tier_response(t) for t in tiers])


@router.put("/admin/tiers", dependencies=[RequireCsrfDependency], response_model=None)
async def put_tiers(
    request: Request,
    body: UpdateTiersRequest,
    session: DbSession,
    session_data: CurrentSession,
    clock: ClockDep,
) -> TierListResponse | JSONResponse:
    ctx = await require_idempotency_key(request, session, session_data.user_id, clock)
    if ctx.is_replay:
        assert ctx.stored_status is not None
        return JSONResponse(status_code=ctx.stored_status, content=ctx.stored_body)

    await update_tiers(
        session,
        actor_id=session_data.user_id,
        updates=[
            TierUpdate(
                code=item.code,
                expected_version=item.expected_version,
                default_sla_minutes=item.default_sla_minutes,
                default_health_weight=item.default_health_weight,
                description=item.description,
            )
            for item in body.tiers
        ],
        clock=clock,
    )
    tiers = await list_tiers(session)
    response = TierListResponse(tiers=[_tier_response(t) for t in tiers])
    await complete(session, ctx, 200, response.model_dump(mode="json"))
    await session.commit()
    return response

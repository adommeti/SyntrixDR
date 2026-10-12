from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from app.core.file_policy import FilePolicy, get_file_policy
from app.core.idempotency import complete, require_idempotency_key
from app.identity_auth.dependencies import ClockDep, CurrentSession, DbSession, RequireCsrfDependency
from app.policies_admin.commands import resolve_all_authorized, set_policy_value, update_file_policy
from app.policies_admin.schemas import (
    FilePolicyResponse,
    PolicyItemResponse,
    PolicyListResponse,
    PolicyValueResponse,
    SetPolicyValueRequest,
    UpdateFilePolicyRequest,
)
from app.users_teams_org.authorization import AuthorizationService, Capability, Scope

router = APIRouter(prefix="/api/v1", tags=["policies_admin"])


@router.get("/admin/policies")
async def get_policies_route(
    session: DbSession,
    session_data: CurrentSession,
    event_id: Annotated[uuid.UUID | None, Query()] = None,
    work_stream_id: Annotated[uuid.UUID | None, Query()] = None,
    application_id: Annotated[uuid.UUID | None, Query()] = None,
) -> PolicyListResponse:
    resolved = await resolve_all_authorized(
        session,
        actor_id=session_data.user_id,
        event_id=event_id,
        work_stream_id=work_stream_id,
        application_id=application_id,
    )
    return PolicyListResponse(
        items=[
            PolicyItemResponse(
                key=definition.key,
                name=definition.name,
                description=definition.description,
                value_type=definition.value_type,
                value=value,
            )
            for definition, value in resolved
        ]
    )


@router.put("/admin/policies", dependencies=[RequireCsrfDependency], response_model=None)
async def put_policy_route(
    request: Request,
    body: SetPolicyValueRequest,
    session: DbSession,
    session_data: CurrentSession,
    clock: ClockDep,
) -> PolicyValueResponse | JSONResponse:
    ctx = await require_idempotency_key(request, session, session_data.user_id, clock)
    if ctx.is_replay:
        assert ctx.stored_status is not None
        return JSONResponse(status_code=ctx.stored_status, content=ctx.stored_body)

    policy_value = await set_policy_value(
        session,
        actor_id=session_data.user_id,
        key=body.key,
        value=body.value,
        scope_type=body.scope_type,
        scope_id=body.scope_id,
        clock=clock,
    )
    response = PolicyValueResponse(
        id=policy_value.id,
        key=body.key,
        value=policy_value.value,
        scope_type=policy_value.scope_type,
        scope_id=policy_value.scope_id,
    )
    await complete(session, ctx, 200, response.model_dump(mode="json"))
    await session.commit()
    return response


def _file_policy_response(policy: FilePolicy) -> FilePolicyResponse:
    return FilePolicyResponse(
        max_mb=policy.max_bytes // (1024 * 1024),
        max_bytes=policy.max_bytes,
        allowed_extensions=list(policy.allowed_extensions),
        allowed_mime_types=list(policy.allowed_mime_types),
        scanning_required=policy.scanning_required,
        updated_by_user_id=policy.updated_by_user_id,
    )


@router.get("/admin/file-policy")
async def get_file_policy_route(session: DbSession, session_data: CurrentSession) -> FilePolicyResponse:
    await AuthorizationService.require(
        session, session_data.user_id, Capability.GLOBAL_POLICY_CONFIG, Scope()
    )
    return _file_policy_response(await get_file_policy(session))


@router.put("/admin/file-policy", dependencies=[RequireCsrfDependency], response_model=None)
async def put_file_policy_route(
    request: Request,
    body: UpdateFilePolicyRequest,
    session: DbSession,
    session_data: CurrentSession,
    clock: ClockDep,
) -> FilePolicyResponse | JSONResponse:
    ctx = await require_idempotency_key(request, session, session_data.user_id, clock)
    if ctx.is_replay:
        assert ctx.stored_status is not None
        return JSONResponse(status_code=ctx.stored_status, content=ctx.stored_body)
    policy = await update_file_policy(
        session,
        actor_id=session_data.user_id,
        max_mb=body.max_mb,
        allowed_extensions=body.allowed_extensions,
        scanning_required=body.scanning_required,
        clock=clock,
    )
    response = _file_policy_response(policy)
    await complete(session, ctx, 200, response.model_dump(mode="json"))
    await session.commit()
    return response

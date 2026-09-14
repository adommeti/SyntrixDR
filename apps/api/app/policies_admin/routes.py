from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from app.core.idempotency import complete, require_idempotency_key
from app.identity_auth.dependencies import ClockDep, CurrentSession, DbSession, RequireCsrfDependency
from app.policies_admin.commands import set_policy_value
from app.policies_admin.queries import PolicyService
from app.policies_admin.schemas import (
    PolicyItemResponse,
    PolicyListResponse,
    PolicyValueResponse,
    SetPolicyValueRequest,
)

router = APIRouter(prefix="/api/v1", tags=["policies_admin"])


@router.get("/admin/policies")
async def get_policies_route(
    session: DbSession,
    session_data: CurrentSession,
    event_id: Annotated[uuid.UUID | None, Query()] = None,
    work_stream_id: Annotated[uuid.UUID | None, Query()] = None,
    application_id: Annotated[uuid.UUID | None, Query()] = None,
) -> PolicyListResponse:
    _ = session_data
    resolved = await PolicyService.resolve_all(
        session, event_id=event_id, work_stream_id=work_stream_id, application_id=application_id
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

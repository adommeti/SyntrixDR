from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from app.core.clock import SystemClock
from app.core.idempotency import complete, require_idempotency_key
from app.identity_auth.dependencies import (
    CurrentSession,
    DbSession,
    RequireCsrfDependency,
    RequireReauthDependency,
)
from app.users_teams_org.commands import create_local_user
from app.users_teams_org.queries import get_me, get_team_workload, get_user, list_teams
from app.users_teams_org.schemas import (
    CreateLocalUserRequest,
    CreateLocalUserResponse,
    MeResponse,
    TeamListResponse,
    TeamResponse,
    TeamWorkloadResponse,
    UserResponse,
)

router = APIRouter(prefix="/api/v1", tags=["users_teams_org"])


@router.get("/me")
async def get_me_route(session: DbSession, session_data: CurrentSession) -> MeResponse:
    user = await get_me(session, session_data.user_id)
    if user is None:
        raise HTTPException(status_code=404)
    return MeResponse(
        id=user.id,
        display_name=user.display_name,
        email=user.email,
        identity_type=user.identity_type,
        job_title=user.job_title,
        is_active=user.is_active,
    )


@router.get("/users/{user_id}")
async def get_user_route(
    user_id: uuid.UUID, session: DbSession, session_data: CurrentSession
) -> UserResponse:
    user = await get_user(session, user_id)
    if user is None:
        raise HTTPException(status_code=404)
    return UserResponse(
        id=user.id,
        display_name=user.display_name,
        email=user.email,
        identity_type=user.identity_type,
        job_title=user.job_title,
        avatar_uri=user.avatar_uri,
        is_active=user.is_active,
    )


@router.get("/teams")
async def list_teams_route(session: DbSession, session_data: CurrentSession) -> TeamListResponse:
    teams = await list_teams(session)
    return TeamListResponse(
        teams=[
            TeamResponse(id=t.id, name=t.name, description=t.description, manager_user_id=t.manager_user_id)
            for t in teams
        ]
    )


@router.get("/teams/{team_id}/workload")
async def get_team_workload_route(
    team_id: uuid.UUID, session: DbSession, session_data: CurrentSession
) -> TeamWorkloadResponse:
    workload = await get_team_workload(session, team_id)
    if workload is None:
        raise HTTPException(status_code=404)
    return TeamWorkloadResponse(**workload)  # type: ignore[arg-type]


@router.post(
    "/admin/local-users",
    dependencies=[RequireCsrfDependency, RequireReauthDependency],
    response_model=None,
)
async def post_create_local_user(
    request: Request, body: CreateLocalUserRequest, session: DbSession, session_data: CurrentSession
) -> CreateLocalUserResponse | JSONResponse:
    """High-risk, Global-Admin-only, reauth-guarded (D-235/D-228). A real domain command (not a
    session endpoint like identity_auth's routes), so D-215 applies: Idempotency-Key required."""
    ctx = await require_idempotency_key(request, session, session_data.user_id, SystemClock())
    if ctx.is_replay:
        assert ctx.stored_status is not None  # invariant: a replay always has a stored outcome
        return JSONResponse(status_code=ctx.stored_status, content=ctx.stored_body)

    result = await create_local_user(
        session,
        actor_id=session_data.user_id,
        display_name=body.display_name,
        email=body.email,
        password=body.password,
    )
    response_body = {"user_id": str(result.user_id)}
    await complete(session, ctx, 200, response_body)
    await session.commit()
    return CreateLocalUserResponse(user_id=result.user_id)

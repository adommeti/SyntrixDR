from __future__ import annotations

import uuid

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.users_teams_org.models import RoleAssignment, Team, TeamMembership, User


async def find_local_user_id_by_email(session: AsyncSession, email: str) -> uuid.UUID | None:
    """Case-insensitive lookup of an active LOCAL user's id by email. A deactivated
    (`is_active=False`) user must not be able to log in, so it's filtered here rather than in
    every caller."""
    result = await session.execute(
        select(User.id).where(
            and_(
                User.email == email.lower(),
                User.identity_type == "LOCAL",
                User.is_active.is_(True),
                User.deleted_at.is_(None),
            )
        )
    )
    return result.scalar_one_or_none()


async def find_entra_user_id_by_object_id(session: AsyncSession, entra_object_id: str) -> uuid.UUID | None:
    """Lookup a non-deleted ENTRA user's id by their Entra object id. Deliberately does not
    exclude `is_active=False` accounts: `entra_callback` is find-or-create keyed on the unique
    `entra_object_id`, so filtering here would make it try to create a duplicate row and hit the
    unique constraint instead of the correct behavior (deny the reactivated/deactivated session
    somewhere authorization-aware). Not compared case-insensitively — Entra object ids are GUIDs,
    not user-typed text like email."""
    result = await session.execute(
        select(User.id).where(and_(User.entra_object_id == entra_object_id, User.deleted_at.is_(None)))
    )
    return result.scalar_one_or_none()


async def get_user_email(session: AsyncSession, user_id: uuid.UUID) -> str | None:
    result = await session.execute(select(User.email).where(User.id == user_id))
    return result.scalar_one_or_none()


async def get_me(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    """Full profile for the current session's user."""
    result = await session.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()


async def get_user(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    """Person deep-card data (API_CONTRACT `GET /api/v1/users/{id}`)."""
    result = await session.execute(select(User).where(User.id == user_id, User.deleted_at.is_(None)))
    return result.scalar_one_or_none()


async def list_teams(session: AsyncSession) -> list[Team]:
    result = await session.execute(select(Team).where(Team.deleted_at.is_(None)).order_by(Team.name))
    return list(result.scalars().all())


async def get_team_workload(session: AsyncSession, team_id: uuid.UUID) -> dict[str, object] | None:
    """Team resource roll-up (API_CONTRACT `GET /api/v1/teams/{id}/workload`).

    V1: member count only — Task/assignment roll-up needs tasks_dependencies, a later BUILD.
    """
    team_result = await session.execute(select(Team).where(Team.id == team_id, Team.deleted_at.is_(None)))
    team = team_result.scalar_one_or_none()
    if team is None:
        return None

    member_count_result = await session.execute(
        select(TeamMembership).where(TeamMembership.team_id == team_id, TeamMembership.deleted_at.is_(None))
    )
    member_count = len(member_count_result.scalars().all())
    return {"team_id": team.id, "team_name": team.name, "member_count": member_count}


async def has_active_role(
    session: AsyncSession,
    user_id: uuid.UUID,
    role_key: str,
    *,
    scope_type: str | None = None,
    scope_id: uuid.UUID | None = None,
) -> bool:
    """True if `user_id` holds `role_key`, active (not revoked), optionally at an exact scope."""
    conditions = [
        RoleAssignment.user_id == user_id,
        RoleAssignment.role_key == role_key,
        RoleAssignment.revoked_at.is_(None),
    ]
    if scope_type is not None:
        conditions.append(RoleAssignment.scope_type == scope_type)
    if scope_id is not None:
        conditions.append(RoleAssignment.scope_id == scope_id)
    result = await session.execute(select(RoleAssignment.id).where(and_(*conditions)))
    return result.scalar_one_or_none() is not None


async def list_active_roles(session: AsyncSession, user_id: uuid.UUID) -> list[RoleAssignment]:
    result = await session.execute(
        select(RoleAssignment).where(RoleAssignment.user_id == user_id, RoleAssignment.revoked_at.is_(None))
    )
    return list(result.scalars().all())

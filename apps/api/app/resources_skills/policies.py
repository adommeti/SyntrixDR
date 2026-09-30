"""Assignment authority (RBAC_MATRIX.md:13-14; FROZEN_DECISIONS.md §4.10-12; DATA_MODEL.md invariant 9).

Returns *which* authority an actor assigns under, not just yes/no: the label goes on the audit row, and
D-214 reads it back to tell an intervening Coordinator/Admin assignment from any other."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.tasks_dependencies.models import Task
from app.tasks_dependencies.queries import holds_in_task_scope
from app.users_teams_org.authorization import AuthorizationService, Capability, Scope
from app.users_teams_org.queries import get_team, has_active_role, is_active_team_member

ADMIN = "ADMIN"
COORDINATOR = "COORDINATOR"
WORK_STREAM_LEAD = "WORK_STREAM_LEAD"
MANAGER = "MANAGER"
IN_SCOPE = "IN_SCOPE"  # App Owner (or Lead) reassigning within the Owning Team, in their scope
TEAM_MEMBER = "TEAM_MEMBER"
VOLUNTEER = "VOLUNTEER"
#: The authorities whose intervening assignment a Team Manager's stale assignment may supersede (D-214).
PRECEDENCE_YIELDS_TO = frozenset({ADMIN, COORDINATOR})


async def is_owning_team_manager(session: AsyncSession, actor_id: uuid.UUID, task: Task) -> bool:
    """MANAGER role *and* the Owning Team's `manager_user_id` (the OWN_TEAM grant's own test)."""
    team = await get_team(session, task.owning_team_id)
    return (
        team is not None
        and team.manager_user_id == actor_id
        and await has_active_role(session, actor_id, "MANAGER")
    )


async def assignment_authority(
    session: AsyncSession, actor_id: uuid.UUID, task: Task, *, same_team: bool, at: datetime
) -> str | None:
    """The authority `actor_id` assigns `task` under, or None (403).

    - Cross-Team target: Admin / Coordinator anywhere; a Work Stream Lead for Tasks in their stream
      ("stream-scoped where policy permits", read as the scoped grant -- BUILD-07.plan.md Risk B2).
    - Same-Team target, additionally: the Owning Team's Manager (Managers never cross Teams, §4.10); an
      App Owner / Lead in the Task's scope; any EXECUTOR on the Owning Team (§4.11)."""
    if await AuthorizationService.can(session, actor_id, Capability.CROSS_TEAM_ASSIGNMENT, Scope()):
        return ADMIN if await has_active_role(session, actor_id, "GLOBAL_ADMIN") else COORDINATOR
    if await holds_in_task_scope(session, actor_id, Capability.CROSS_TEAM_ASSIGNMENT, task):
        return WORK_STREAM_LEAD
    if not same_team:
        return None
    if await is_owning_team_manager(session, actor_id, task):
        return MANAGER
    if await holds_in_task_scope(session, actor_id, Capability.OWN_TEAM_REASSIGNMENT, task):
        return IN_SCOPE
    if await has_active_role(session, actor_id, "EXECUTOR") and await is_active_team_member(
        session, task.owning_team_id, actor_id, at=at
    ):
        return TEAM_MEMBER
    return None


async def may_volunteer(session: AsyncSession, actor_id: uuid.UUID) -> bool:
    """§4.12: a user volunteers for eligible cross-Team work -- RBAC_MATRIX.md's Executor "volunteer only".
    No skill test: nothing records what a Task requires (BUILD-07.plan.md Risk B3)."""
    return await has_active_role(session, actor_id, "EXECUTOR")

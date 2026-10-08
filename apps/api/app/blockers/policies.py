"""Who may act on a Blocker (RBAC_MATRIX.md:21-22, D-211).

Role half: `BLOCKER_CREATE_RESOLVE_VERIFY` (Admin, Coordinator, Work Stream Lead, App/System Owner --
unscoped ✓ cells) and `BLOCKER_START_CLAIM` (Admin, Coordinator unscoped; Lead / App Owner in the
Task's scope). The Manager and Executor cells are qualified ("scope", "eligible", "assigned resolver"),
which the grant table can't express, so they are resolved here from the Task and the Blocker:

- Manager: their Team is the Task's Owning Team, or the Team queue the Blocker is routed to.
- Executor (needs the EXECUTOR role): eligible = the Blocker's owner, the Task's current assignee, or
  an active member of the Owning Team or the routed Team. `start` only as the assigned resolver (or
  claiming an unowned Blocker on a queue they belong to); `verify` only from the Owning Team --
  "Owning Team verifies the blocking condition is actually gone" (STATE_MACHINES.md §Blocker)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.applications_catalog.queries import is_system_application_owner
from app.blockers.models import Blocker
from app.dr_events.queries import get_dr_application
from app.tasks_dependencies.models import Task
from app.tasks_dependencies.queries import holds_in_task_scope
from app.users_teams_org.authorization import AuthorizationService, Capability, Scope
from app.users_teams_org.queries import get_team, has_active_role, is_active_team_member


async def _manages(session: AsyncSession, actor_id: uuid.UUID, team_id: uuid.UUID | None) -> bool:
    if team_id is None:
        return False
    team = await get_team(session, team_id)
    return team is not None and team.manager_user_id == actor_id


async def _is_manager_of(session: AsyncSession, actor_id: uuid.UUID, *team_ids: uuid.UUID | None) -> bool:
    if not await has_active_role(session, actor_id, "MANAGER"):
        return False
    for team_id in team_ids:
        if await _manages(session, actor_id, team_id):
            return True
    return False


async def _member_of(
    session: AsyncSession, actor_id: uuid.UUID, *team_ids: uuid.UUID | None, at: datetime
) -> bool:
    for team_id in team_ids:
        if team_id is not None and await is_active_team_member(session, team_id, actor_id, at=at):
            return True
    return False


async def _executor_eligible(
    session: AsyncSession, actor_id: uuid.UUID, task: Task, blocker: Blocker, *, at: datetime
) -> bool:
    if not await has_active_role(session, actor_id, "EXECUTOR"):
        return False
    if actor_id in (blocker.blocker_owner_user_id, task.current_assignee_user_id):
        return True
    return await _member_of(session, actor_id, task.owning_team_id, blocker.blocker_team_id, at=at)


async def _is_application_owner(session: AsyncSession, actor_id: uuid.UUID, task: Task) -> bool:
    """RBAC_MATRIX.md's "App/System Owner" column: any `SYSTEM_APPLICATION` slot of the Task's
    Application. Slot ownership is a table, not a role grant, so `AuthorizationService` can't see it
    (same reading as `actor_may_validate_task`, ADR-042)."""
    if task.dr_application_id is None:
        return False
    dr_application = await get_dr_application(session, task.dr_application_id)
    if dr_application is None:
        return False
    return await is_system_application_owner(session, dr_application.application_id, actor_id)


async def _role_half(session: AsyncSession, actor_id: uuid.UUID, task: Task, capability: Capability) -> bool:
    """Unscoped ✓ cells, then the Lead / App Owner "scope" cells against the Task's own scopes, then
    the Application's owner slots."""
    if await AuthorizationService.can(session, actor_id, capability, Scope()):
        return True
    if await holds_in_task_scope(session, actor_id, capability, task):
        return True
    return await _is_application_owner(session, actor_id, task)


async def actor_may_route_blocker(
    session: AsyncSession,
    actor_id: uuid.UUID,
    task: Task,
    blocker: Blocker,
    *,
    team_id: uuid.UUID | None,
    assignee_user_id: uuid.UUID | None,
    at: datetime,
) -> bool:
    """`assign`: route to a Team queue and/or a resolver. An Executor may only claim for themselves,
    onto a queue they belong to (or no queue change)."""
    if await _role_half(session, actor_id, task, Capability.BLOCKER_CREATE_RESOLVE_VERIFY):
        return True
    if await _is_manager_of(session, actor_id, task.owning_team_id, blocker.blocker_team_id, team_id):
        return True
    if not await _executor_eligible(session, actor_id, task, blocker, at=at):
        return False
    if assignee_user_id != actor_id:
        return False
    return team_id is None or await _member_of(session, actor_id, team_id, at=at)


async def actor_may_start_blocker(
    session: AsyncSession, actor_id: uuid.UUID, task: Task, blocker: Blocker, *, at: datetime
) -> bool:
    """`start`: the assigned resolver begins work (RBAC_MATRIX.md:22)."""
    if await _role_half(session, actor_id, task, Capability.BLOCKER_START_CLAIM):
        return True
    if await _is_manager_of(session, actor_id, task.owning_team_id, blocker.blocker_team_id):
        return True
    if not await has_active_role(session, actor_id, "EXECUTOR"):
        return False
    if blocker.blocker_owner_user_id is not None:
        return blocker.blocker_owner_user_id == actor_id
    return await _member_of(session, actor_id, blocker.blocker_team_id, task.owning_team_id, at=at)


async def actor_may_resolve_blocker(
    session: AsyncSession, actor_id: uuid.UUID, task: Task, blocker: Blocker, *, at: datetime
) -> bool:
    """`resolve` belongs to the resolver side: the owner, or the routed Team's members. With no Team
    routed, the Owning Team's members and the Task's assignee are the resolvers."""
    if await _role_half(session, actor_id, task, Capability.BLOCKER_CREATE_RESOLVE_VERIFY):
        return True
    if await _is_manager_of(session, actor_id, task.owning_team_id, blocker.blocker_team_id):
        return True
    if not await has_active_role(session, actor_id, "EXECUTOR"):
        return False
    if blocker.blocker_owner_user_id == actor_id:
        return True
    if blocker.blocker_team_id is not None:
        return await _member_of(session, actor_id, blocker.blocker_team_id, at=at)
    if task.current_assignee_user_id == actor_id:
        return True
    return await _member_of(session, actor_id, task.owning_team_id, at=at)


async def actor_may_verify_blocker(
    session: AsyncSession, actor_id: uuid.UUID, task: Task, blocker: Blocker, *, at: datetime
) -> bool:
    """`verify` is the Owning Team's: its Manager, or an Executor who is its member / the Task's
    assignee. The resolver's own Team does not verify its own fix."""
    if await _role_half(session, actor_id, task, Capability.BLOCKER_CREATE_RESOLVE_VERIFY):
        return True
    if await _is_manager_of(session, actor_id, task.owning_team_id):
        return True
    if not await has_active_role(session, actor_id, "EXECUTOR"):
        return False
    if task.current_assignee_user_id == actor_id:
        return True
    return await _member_of(session, actor_id, task.owning_team_id, at=at)

"""Who may act on a Task. RBAC_MATRIX.md cells whose qualifiers `AuthorizationService`'s grant
markers can't express ("own/Team work", "any slot", "never the Task's own executor") are enforced
here, in the owning module -- exactly what the `GRANTS` docstring leaves to "the owning transition
service when it lands"."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.applications_catalog.queries import is_system_application_owner
from app.core.errors import AppError
from app.dr_events.queries import get_dr_application
from app.policies_admin.queries import PolicyService
from app.tasks_dependencies.models import Task
from app.users_teams_org.authorization import GRANTS, AuthorizationService, Capability, Scope
from app.users_teams_org.queries import has_active_role, is_active_team_member
from app.work_streams.queries import is_work_stream_lead


class SelfValidationForbiddenError(AppError):
    code = "SELF_VALIDATION_FORBIDDEN"
    status_code = 403

    def __init__(self) -> None:
        super().__init__("You can't validate a Task you're assigned to or submitted for validation (D-209).")


async def _application_id_of(session: AsyncSession, dr_application_id: uuid.UUID | None) -> uuid.UUID | None:
    if dr_application_id is None:
        return None
    dr_application = await get_dr_application(session, dr_application_id)
    return dr_application.application_id if dr_application is not None else None


async def _application_id(session: AsyncSession, task: Task) -> uuid.UUID | None:
    return await _application_id_of(session, task.dr_application_id)


async def _scopes(
    session: AsyncSession,
    *,
    owning_team_id: uuid.UUID,
    work_stream_id: uuid.UUID | None,
    dr_application_id: uuid.UUID | None,
) -> list[Scope]:
    """Every scope a role could hold that covers a Task with these ids: the Owning Team (Manager's
    OWN_TEAM), its Work Stream, and its Application."""
    scopes = [Scope(owning_team_id=owning_team_id)]
    if work_stream_id is not None:
        scopes.append(Scope(scope_type="WORK_STREAM", scope_id=work_stream_id))
    application_id = await _application_id_of(session, dr_application_id)
    if application_id is not None:
        scopes.append(Scope(scope_type="APPLICATION", scope_id=application_id))
    return scopes


async def _task_scopes(session: AsyncSession, task: Task) -> list[Scope]:
    return await _scopes(
        session,
        owning_team_id=task.owning_team_id,
        work_stream_id=task.work_stream_id,
        dr_application_id=task.dr_application_id,
    )


def _grants_work_stream_lead(capability: Capability) -> bool:
    return GRANTS.get(capability, {}).get("WORK_STREAM_LEAD") in (True, "SCOPE")


async def _can_in_any_scope(
    session: AsyncSession, actor_id: uuid.UUID, capability: Capability, scopes: list[Scope]
) -> bool:
    """`AuthorizationService.can` in any of the scopes -- plus a stream's designated Lead
    (`work_streams.lead_user_id`), who holds Work Stream Lead authority in that stream for exactly the
    capabilities whose GRANTS row gives WORK_STREAM_LEAD `True`/`"SCOPE"` (BUILD-06.plan.md Risk #19)."""
    for scope in scopes:
        if await AuthorizationService.can(session, actor_id, capability, scope):
            return True
        if (
            scope.scope_type == "WORK_STREAM"
            and scope.scope_id is not None
            and _grants_work_stream_lead(capability)
            and await is_work_stream_lead(session, scope.scope_id, actor_id)
        ):
            return True
    return False


async def actor_may_execute_task(
    session: AsyncSession, actor_id: uuid.UUID, task: Task, *, at: datetime
) -> bool:
    """`start`/`block`/`resume`/`submit-validation` (BUILD-06.plan.md Risk #2).

    Role-based half: `EXECUTE_TASK` (Admin, Coordinator, Work Stream Lead / App Owner in scope,
    the Owning Team's Manager). Executor half, RBAC_MATRIX.md's "own/Team work": the actor holds
    the EXECUTOR role *and* is the Task's current assignee or an active member of its Owning Team.
    Assignment alone isn't authority -- the matrix is role-based throughout."""
    if await _can_in_any_scope(session, actor_id, Capability.EXECUTE_TASK, await _task_scopes(session, task)):
        return True
    if not await has_active_role(session, actor_id, "EXECUTOR"):
        return False
    if task.current_assignee_user_id == actor_id:
        return True
    return await is_active_team_member(session, task.owning_team_id, actor_id, at=at)


async def actor_may_change_task(session: AsyncSession, actor_id: uuid.UUID, task: Task) -> bool:
    """`cancel` -- a management decision, so the Change Task metadata row, not execution."""
    return await _can_in_any_scope(
        session, actor_id, Capability.CHANGE_TASK_METADATA, await _task_scopes(session, task)
    )


#: `PATCH /tasks/{id}` fields that decide what completing the Task requires (D-226, D-209). Changing them
#: is a management decision: an Executor editing their own work may not relax their own completion bar.
REQUIREMENT_FIELDS = frozenset(
    {"evidence_required", "evidence_min_count", "verification_note_required", "needs_specific_validation"}
)


async def actor_may_edit_task_metadata(
    session: AsyncSession, actor_id: uuid.UUID, task: Task, fields: frozenset[str], *, at: datetime
) -> bool:
    """RBAC_MATRIX.md "Change Task metadata": Admin, Coordinator, Lead/App Owner in scope, Manager of the
    Owning Team (`CHANGE_TASK_METADATA`) edit every field; an Executor edits the descriptive fields of
    "own/Team work" -- EXECUTOR role and the current assignee or an active Owning Team member."""
    if await actor_may_change_task(session, actor_id, task):
        return True
    if fields & REQUIREMENT_FIELDS or not await has_active_role(session, actor_id, "EXECUTOR"):
        return False
    if task.current_assignee_user_id == actor_id:
        return True
    return await is_active_team_member(session, task.owning_team_id, actor_id, at=at)


async def actor_may_validate_task(session: AsyncSession, actor_id: uuid.UUID, task: Task) -> bool:
    """D-209, via RBAC_MATRIX.md's "Validate Task (standard)" row -- the row that names this exact
    transition. Application-scoped work: an Application/System Owner in *any* slot (checked against
    `application_owners`, which grants no role) or the APP_OWNER role on that Application; a Work
    Stream Lead never qualifies, even if the Task also sits in their stream. Shared Work Stream
    work (no Application): that stream's Lead. Admin/Coordinator validate either. The "never the
    executor" half is `assert_not_self_validation`, checked once the submitter is known."""
    application_id = await _application_id(session, task)
    if application_id is not None:
        app_scope = Scope(scope_type="APPLICATION", scope_id=application_id)
        if await AuthorizationService.can(session, actor_id, Capability.VALIDATE_TASK_STANDARD, app_scope):
            return True
        return await is_system_application_owner(session, application_id, actor_id)
    stream_scope = Scope(scope_type="WORK_STREAM", scope_id=task.work_stream_id)
    return await _can_in_any_scope(session, actor_id, Capability.VALIDATE_TASK_STANDARD, [stream_scope])


def assert_not_self_validation(actor_id: uuid.UUID, task: Task, submitted_by_user_id: uuid.UUID) -> None:
    """D-209: "Executors can never self-complete" -- applies to everyone, Admin included. The
    executor is identified as the current assignee or whoever submitted this Validation."""
    if actor_id in (task.current_assignee_user_id, submitted_by_user_id):
        raise SelfValidationForbiddenError()


async def actor_may_override(
    session: AsyncSession, actor_id: uuid.UUID, task: Task, *, cross_stream: bool
) -> bool:
    """BUILD-06.plan.md Risk #5. Overriding a guard that crosses Work Streams needs the stricter
    cross-stream capability (Admin/Coordinator); otherwise the within-stream one (plus the Lead of
    this Task's stream)."""
    if cross_stream:
        return await AuthorizationService.can(
            session, actor_id, Capability.OVERRIDE_CROSS_WORK_STREAM_GUARD, Scope()
        )
    scope = (
        Scope(scope_type="WORK_STREAM", scope_id=task.work_stream_id)
        if task.work_stream_id is not None
        else Scope()
    )
    return await _can_in_any_scope(session, actor_id, Capability.OVERRIDE_WITHIN_WORK_STREAM, [scope])


async def actor_may_change_dependency(
    session: AsyncSession, actor_id: uuid.UUID, successor: Task, *, at: datetime
) -> bool:
    """Adding or removing an edge into `successor` (BUILD-06.plan.md Risk #12). Judged on the successor
    alone: an edge only constrains when its successor may start.

    Policy `dependency.edit_requires` (schema_v2_reconciliation.sql:454), resolved at the successor's
    scope: `SCOPED_ROLE` (default) = CHANGE_DEPENDENCIES in the successor's scope, or RBAC_MATRIX.md's
    Executor cell "Team per policy" (EXECUTOR role and an active member of its Owning Team).
    `COORDINATOR_ONLY` -- or any value this code doesn't recognise -- = Admin/Coordinator only."""
    mode = await PolicyService.resolve(
        session,
        "dependency.edit_requires",
        event_id=successor.dr_event_id,
        work_stream_id=successor.work_stream_id,
        application_id=await _application_id(session, successor),
    )
    if mode != "SCOPED_ROLE":
        return await AuthorizationService.can(session, actor_id, Capability.CHANGE_DEPENDENCIES, Scope())
    scopes = await _task_scopes(session, successor)
    if await _can_in_any_scope(session, actor_id, Capability.CHANGE_DEPENDENCIES, scopes):
        return True
    return await has_active_role(session, actor_id, "EXECUTOR") and await is_active_team_member(
        session, successor.owning_team_id, actor_id, at=at
    )


async def actor_may_create_task(
    session: AsyncSession,
    actor_id: uuid.UUID,
    *,
    owning_team_id: uuid.UUID,
    work_stream_id: uuid.UUID | None,
    dr_application_id: uuid.UUID | None,
) -> bool:
    """Creating work is a planning act: CHANGE_TASK_METADATA's role grants in the new Task's scopes,
    with no Executor path (BUILD-06.plan.md Risk #21)."""
    scopes = await _scopes(
        session,
        owning_team_id=owning_team_id,
        work_stream_id=work_stream_id,
        dr_application_id=dr_application_id,
    )
    return await _can_in_any_scope(session, actor_id, Capability.CHANGE_TASK_METADATA, scopes)

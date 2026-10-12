"""Who may attach evidence to a target. No RBAC_MATRIX row names it, so the conservative reading
(BUILD-09.plan.md Risk #1): whoever may act on the target under that target's own policy."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.applications_catalog.queries import is_system_application_owner
from app.blockers.policies import actor_may_resolve_blocker
from app.blockers.queries import get_blocker
from app.dr_events.queries import get_dr_application
from app.issues_findings.policies import actor_may_handle_issue
from app.issues_findings.queries import get_issue
from app.tasks_dependencies.policies import actor_may_execute_task, actor_may_validate_task
from app.tasks_dependencies.queries import get_task
from app.users_teams_org.authorization import AuthorizationService, Capability, Scope
from app.validation_evidence.queries import EvidenceTarget


async def _admin_or_coordinator(session: AsyncSession, actor_id: uuid.UUID) -> bool:
    return await AuthorizationService.can(session, actor_id, Capability.EVENT_LIFECYCLE_COMMAND, Scope())


async def _may_act_on_task(
    session: AsyncSession, actor_id: uuid.UUID, task_id: uuid.UUID, *, at: datetime
) -> bool:
    task = await get_task(session, task_id)
    if task is None:
        return False
    if await actor_may_execute_task(session, actor_id, task, at=at):
        return True
    return await actor_may_validate_task(session, actor_id, task)


async def actor_may_attach_evidence(
    session: AsyncSession, actor_id: uuid.UUID, target: EvidenceTarget, *, at: datetime
) -> bool:
    if await _admin_or_coordinator(session, actor_id):
        return True
    if target.target_type in ("TASK", "VALIDATION") and target.task_id is not None:
        return await _may_act_on_task(session, actor_id, target.task_id, at=at)
    if target.target_type == "BLOCKER":
        blocker = await get_blocker(session, target.target_id)
        task = await get_task(session, blocker.task_id) if blocker is not None else None
        if blocker is None or task is None:
            return False
        if await actor_may_resolve_blocker(session, actor_id, task, blocker, at=at):
            return True
        return await _may_act_on_task(session, actor_id, task.id, at=at)
    if target.target_type in ("DR_APPLICATION", "VALIDATION"):
        dr_application = await get_dr_application(session, target.target_id)
        if dr_application is None:
            return False
        return await is_system_application_owner(session, dr_application.application_id, actor_id)
    if target.target_type == "ISSUE_FINDING":
        issue = await get_issue(session, target.target_id)
        return issue is not None and await actor_may_handle_issue(session, actor_id, issue)
    if target.target_type == "MILESTONE":
        from app.milestones.policies import actor_may_confirm_milestone
        from app.milestones.queries import get_milestone

        milestone = await get_milestone(session, target.target_id)
        return milestone is not None and await actor_may_confirm_milestone(session, actor_id, milestone)
    return False

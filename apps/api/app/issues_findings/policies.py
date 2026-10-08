"""Who may create and handle an Issue/Finding (RBAC_MATRIX.md:23 "Create Issue/Finding": every role ✓
except the Business Owner's qualified "comment/create if permitted", which the grant table reads as no
grant -- the conservative reading). Review/resolve/dismiss have no RBAC row (BUILD-08.plan.md Risk B6):
the same capability in the Issue's scopes, plus the Issue's creator and owner."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.applications_catalog.queries import is_system_application_owner
from app.dr_events.queries import get_dr_application
from app.issues_findings.models import IssueFinding
from app.users_teams_org.authorization import AuthorizationService, Capability, Scope
from app.work_streams.queries import is_work_stream_lead


async def _can_in_scopes(
    session: AsyncSession,
    actor_id: uuid.UUID,
    *,
    work_stream_id: uuid.UUID | None,
    dr_application_id: uuid.UUID | None,
) -> bool:
    capability = Capability.CREATE_ISSUE_FINDING
    if await AuthorizationService.can(session, actor_id, capability, Scope()):
        return True
    if work_stream_id is not None:
        if await AuthorizationService.can(
            session, actor_id, capability, Scope(scope_type="WORK_STREAM", scope_id=work_stream_id)
        ):
            return True
        if await is_work_stream_lead(session, work_stream_id, actor_id):
            return True
    if dr_application_id is not None:
        dr_application = await get_dr_application(session, dr_application_id)
        if dr_application is not None:
            application_id = dr_application.application_id
            if await AuthorizationService.can(
                session, actor_id, capability, Scope(scope_type="APPLICATION", scope_id=application_id)
            ):
                return True
            if await is_system_application_owner(session, application_id, actor_id):
                return True
    return False


async def actor_may_create_issue(
    session: AsyncSession,
    actor_id: uuid.UUID,
    *,
    work_stream_id: uuid.UUID | None,
    dr_application_id: uuid.UUID | None,
) -> bool:
    return await _can_in_scopes(
        session, actor_id, work_stream_id=work_stream_id, dr_application_id=dr_application_id
    )


async def actor_may_handle_issue(session: AsyncSession, actor_id: uuid.UUID, issue: IssueFinding) -> bool:
    if actor_id in (issue.created_by_user_id, issue.owner_user_id):
        return True
    return await _can_in_scopes(
        session, actor_id, work_stream_id=issue.work_stream_id, dr_application_id=issue.dr_application_id
    )

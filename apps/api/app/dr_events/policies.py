"""Who may submit and validate a DR Application's technical validation (RBAC_MATRIX.md:18 "Validate
application work": Admin ✓, Coordinator ✓, App/System Owner any slot (D-254); Business Owner gets an
optional business note only, never the validation itself)."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.applications_catalog.queries import is_system_application_owner
from app.dr_events.models import DrApplication
from app.users_teams_org.authorization import AuthorizationService, Capability, Scope


async def actor_may_validate_application(
    session: AsyncSession, actor_id: uuid.UUID, dr_application: DrApplication
) -> bool:
    capability = Capability.VALIDATE_APPLICATION_WORK
    if await AuthorizationService.can(session, actor_id, capability, Scope()):
        return True
    scope = Scope(scope_type="APPLICATION", scope_id=dr_application.application_id)
    if await AuthorizationService.can(session, actor_id, capability, scope):
        return True
    return await is_system_application_owner(session, dr_application.application_id, actor_id)

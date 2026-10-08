"""RBAC_MATRIX.md:24 "Comment/@mention cross-Team": every role ✓ (the grant table has them all True),
so authority is "holds any role" plus Event visibility, which the command checks first. Alert
acknowledgement has no RBAC row (BUILD-08.plan.md Risk B1)."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.applications_catalog.queries import holds_any_owner_slot
from app.users_teams_org.authorization import AuthorizationService, Capability, Scope
from app.users_teams_org.queries import has_active_role

#: Roles that may acknowledge an Event-scoped alert on everyone's behalf (shared ack): the ones that
#: manage work in the Event. Admin / Coordinator always; global (Event-less) alerts: Admin only.
_ACK_ROLES = ("WORK_STREAM_LEAD", "APP_OWNER", "MANAGER")


async def actor_may_comment(session: AsyncSession, actor_id: uuid.UUID) -> bool:
    """Every role column is ✓; Business and App/System Owners are owner slots rather than roles, so a
    slot counts too. Event visibility is checked by the command before this."""
    if await AuthorizationService.can(session, actor_id, Capability.COMMENT_MENTION_CROSS_TEAM, Scope()):
        return True
    return await holds_any_owner_slot(session, actor_id)


async def actor_may_acknowledge_alert(
    session: AsyncSession, actor_id: uuid.UUID, *, dr_event_id: uuid.UUID | None
) -> bool:
    if await AuthorizationService.is_global_admin(session, actor_id):
        return True
    if dr_event_id is None:
        return False
    if await has_active_role(session, actor_id, "DR_COORDINATOR"):
        return True
    for role in _ACK_ROLES:
        if await has_active_role(session, actor_id, role):
            return True
    return False

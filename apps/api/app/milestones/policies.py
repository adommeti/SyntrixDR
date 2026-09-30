from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.milestones.models import Milestone
from app.users_teams_org.authorization import AuthorizationService, Capability, Scope
from app.work_streams.queries import is_work_stream_lead


async def _in_milestone_stream(
    session: AsyncSession,
    actor_id: uuid.UUID,
    capability: Capability,
    milestone_work_stream_id: uuid.UUID | None,
) -> bool:
    """`capability` globally (Admin, Coordinator) or in this Milestone's Work Stream -- a Lead role scoped
    to it, or its designated `lead_user_id` (ADR-042). An Application-only Milestone has no stream, so
    only the unscoped grants apply."""
    if await AuthorizationService.can(session, actor_id, capability, Scope()):
        return True
    if milestone_work_stream_id is None:
        return False
    if await AuthorizationService.can(
        session, actor_id, capability, Scope(scope_type="WORK_STREAM", scope_id=milestone_work_stream_id)
    ):
        return True
    return await is_work_stream_lead(session, milestone_work_stream_id, actor_id)


async def actor_may_confirm_milestone(
    session: AsyncSession, actor_id: uuid.UUID, milestone: Milestone
) -> bool:
    """RBAC_MATRIX.md "Confirm shared Milestone": Admin, Coordinator, Work Stream Lead."""
    return await _in_milestone_stream(
        session, actor_id, Capability.CONFIRM_SHARED_MILESTONE, milestone.work_stream_id
    )


async def actor_may_create_milestone(
    session: AsyncSession, actor_id: uuid.UUID, work_stream_id: uuid.UUID | None
) -> bool:
    """No RBAC row names Milestone creation; `MANAGE_WORK_STREAMS` (Admin, Coordinator) plus the Lead of
    the Milestone's own stream -- the same people who confirm it (BUILD-07.plan.md Risk #4)."""
    if await AuthorizationService.can(session, actor_id, Capability.MANAGE_WORK_STREAMS, Scope()):
        return True
    if work_stream_id is None:
        return False
    return await _in_milestone_stream(session, actor_id, Capability.CONFIRM_SHARED_MILESTONE, work_stream_id)

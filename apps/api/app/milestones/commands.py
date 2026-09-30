from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.milestones.queries import milestone_ids_for_task
from app.milestones.transition_service import MilestoneTransitionService


async def on_task_changed(
    session: AsyncSession, *, task_id: uuid.UUID, actor_id: uuid.UUID, clock: Clock
) -> None:
    """Re-derive every Milestone this Task contributes to, in the Task command's own transaction.

    Called by `TaskTransitionService` after each Task transition. Milestones are locked in id order
    (`milestone_ids_for_task`), so two Task commands touching overlapping Milestones can't deadlock."""
    for milestone_id in await milestone_ids_for_task(session, task_id):
        await MilestoneTransitionService.recompute(
            session, milestone_id=milestone_id, actor_id=actor_id, clock=clock, trigger_task_id=task_id
        )

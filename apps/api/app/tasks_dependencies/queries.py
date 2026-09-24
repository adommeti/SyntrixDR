from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.tasks_dependencies.models import Task, TaskDependency


async def unsatisfied_hard_predecessors(session: AsyncSession, task_id: uuid.UUID) -> list[Task]:
    """Predecessors on live HARD Finish-to-Start edges into this Task that aren't COMPLETED -- the
    part of derived Ready (STATE_MACHINES.md:50) `start` needs. Strictly COMPLETED: a CANCELLED
    predecessor still blocks, so resolving that takes an audited override, never a silent pass.
    ADVISORY edges never block. Session b extends derived Ready (manual Milestone gates) on top of
    this rather than replacing it."""
    stmt = (
        select(Task)
        .join(TaskDependency, TaskDependency.predecessor_task_id == Task.id)
        .where(
            TaskDependency.successor_task_id == task_id,
            TaskDependency.dependency_type == "FINISH_TO_START",
            TaskDependency.strength == "HARD",
            TaskDependency.deleted_at.is_(None),
            Task.status != "COMPLETED",
        )
        .order_by(Task.id)
    )
    return list((await session.execute(stmt)).scalars().all())

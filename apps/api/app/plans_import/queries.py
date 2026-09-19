from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dr_events.participants import user_can_see_event
from app.plans_import.models import (
    ImportJob,
    Plan,
    PlanVersion,
    PlanVersionMilestone,
    PlanVersionTask,
    PlanVersionTaskDependency,
)


async def get_plan(session: AsyncSession, plan_id: uuid.UUID) -> Plan | None:
    return await session.get(Plan, plan_id)


async def list_plans(session: AsyncSession) -> list[Plan]:
    result = await session.execute(select(Plan).where(Plan.deleted_at.is_(None)).order_by(Plan.name))
    return list(result.scalars().all())


async def list_plan_versions(session: AsyncSession, plan_id: uuid.UUID) -> list[PlanVersion]:
    result = await session.execute(
        select(PlanVersion).where(PlanVersion.plan_id == plan_id).order_by(PlanVersion.version_number)
    )
    return list(result.scalars().all())


async def get_plan_version(session: AsyncSession, plan_version_id: uuid.UUID) -> PlanVersion | None:
    return await session.get(PlanVersion, plan_version_id)


async def count_snapshot_rows(session: AsyncSession, plan_version_id: uuid.UUID) -> tuple[int, int, int]:
    task_count = (
        await session.execute(
            select(func.count())
            .select_from(PlanVersionTask)
            .where(PlanVersionTask.plan_version_id == plan_version_id)
        )
    ).scalar_one()
    dependency_count = (
        await session.execute(
            select(func.count())
            .select_from(PlanVersionTaskDependency)
            .where(PlanVersionTaskDependency.plan_version_id == plan_version_id)
        )
    ).scalar_one()
    milestone_count = (
        await session.execute(
            select(func.count())
            .select_from(PlanVersionMilestone)
            .where(PlanVersionMilestone.plan_version_id == plan_version_id)
        )
    ).scalar_one()
    return task_count, dependency_count, milestone_count


async def next_plan_version_number(session: AsyncSession, plan_id: uuid.UUID) -> int:
    result = await session.execute(
        select(func.max(PlanVersion.version_number)).where(PlanVersion.plan_id == plan_id)
    )
    current_max = result.scalar_one_or_none()
    return (current_max or 0) + 1


async def get_visible_import_job(
    session: AsyncSession, user_id: uuid.UUID, import_job_id: uuid.UUID
) -> ImportJob | None:
    """Returns `None` (not a 403) for a foreign job the caller can't see, so the route 404s
    without confirming the job exists -- same IDOR-safe shape as
    `dr_events/queries.py::get_visible_event`."""
    import_job = await session.get(ImportJob, import_job_id)
    if import_job is None:
        return None
    if not await user_can_see_event(session, user_id, import_job.dr_event_id):
        return None
    return import_job

"""MISSED sweep (STATE_MACHINES.md §Milestone; D-241 Celery + Redis, one beat). Split like
`plans_import.jobs`: `sweep_missed_milestones(session, clock)` takes its session (tests pass theirs);
`_sweep_async` owns an engine for the Celery task."""

from __future__ import annotations

import asyncio
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock, SystemClock
from app.core.config import get_settings
from app.core.database import make_engine, make_session_factory
from app.jobs.celery_app import celery_app
from app.milestones.queries import overdue_milestone_ids
from app.milestones.transition_service import InvalidMilestoneTransitionError, MilestoneTransitionService


async def sweep_missed_milestones(session: AsyncSession, clock: Clock) -> list[uuid.UUID]:
    """Marks every live, non-terminal Milestone whose `target_at` has passed as MISSED. Idempotent: a
    MISSED one is terminal and no longer selected. `miss` re-checks the state under the row lock, so
    one confirmed between the select and the lock is left ACHIEVED."""
    missed: list[uuid.UUID] = []
    for milestone_id in await overdue_milestone_ids(session, now=clock.now()):
        try:
            await MilestoneTransitionService.miss(session, milestone_id=milestone_id, clock=clock)
        except InvalidMilestoneTransitionError:
            continue
        missed.append(milestone_id)
    return missed


async def _sweep_async() -> int:
    engine = make_engine(get_settings().database_url)
    session_factory = make_session_factory(engine)
    async with session_factory() as session:
        missed = await sweep_missed_milestones(session, SystemClock())
        await session.commit()
    await engine.dispose()
    return len(missed)


@celery_app.task(name="drcc.sweep_missed_milestones")  # pyright: ignore[reportUntypedFunctionDecorator, reportUnknownMemberType]
def sweep_missed_milestones_task() -> int:
    return asyncio.run(_sweep_async())

"""Escalation sweep on the one Celery beat (D-241; ADRS.md ADR-020: every 30 s). Same split as
`milestones.jobs` (ADR-045): `escalate_due_blockers(session, clock)` takes its session;
`_sweep_async` owns an engine for the Celery task."""

from __future__ import annotations

import asyncio

from app.blockers.escalation import escalate_due_blockers
from app.core.clock import SystemClock
from app.core.config import get_settings
from app.core.database import make_engine, make_session_factory
from app.jobs.celery_app import celery_app


async def _sweep_async() -> int:
    engine = make_engine(get_settings().database_url)
    session_factory = make_session_factory(engine)
    async with session_factory() as session:
        escalated = await escalate_due_blockers(session, SystemClock())
        await session.commit()
    await engine.dispose()
    return len(escalated)


@celery_app.task(name="drcc.escalate_blockers")  # pyright: ignore[reportUntypedFunctionDecorator, reportUnknownMemberType]
def escalate_blockers_task() -> int:
    return asyncio.run(_sweep_async())

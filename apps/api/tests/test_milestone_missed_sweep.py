"""MISSED when `target_at` passes without ACHIEVED -- the Celery sweep (D-241: Celery + Redis, no
second scheduler). The sweep body takes the caller's session (DI), like `plans_import.jobs`."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import FakeClock
from app.jobs.celery_app import celery_app
from app.milestones.jobs import sweep_missed_milestones
from tests.factories import audit_actions, build_world, seed_milestone

pytestmark = [pytest.mark.domain]


async def _status(session: AsyncSession, milestone_id: object) -> str:
    return (
        await session.execute(text("SELECT status FROM milestones WHERE id = :m"), {"m": milestone_id})
    ).scalar_one()


async def test_overdue_open_milestones_are_missed_and_nothing_else_is(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)
    past, future = clock.now() - timedelta(minutes=1), clock.now() + timedelta(minutes=1)
    overdue = {s: await seed_milestone(session, w, status=s, target_at=past) for s in (
        "NOT_STARTED", "IN_PROGRESS", "AT_RISK", "READY_FOR_CONFIRMATION"
    )}  # fmt: skip
    achieved = await seed_milestone(session, w, status="ACHIEVED", target_at=past)
    not_yet = await seed_milestone(session, w, status="IN_PROGRESS", target_at=future)
    no_target = await seed_milestone(session, w, status="IN_PROGRESS")

    missed = await sweep_missed_milestones(session, clock)

    assert set(missed) == set(overdue.values())
    for milestone_id in overdue.values():
        assert await _status(session, milestone_id) == "MISSED"
        assert await audit_actions(session, milestone_id) == ["MILESTONE_MISSED"]
    assert await _status(session, achieved) == "ACHIEVED"
    assert await _status(session, not_yet) == "IN_PROGRESS"
    assert await _status(session, no_target) == "IN_PROGRESS"


async def test_the_target_instant_itself_counts_as_passed(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)
    milestone_id = await seed_milestone(session, w, status="IN_PROGRESS", target_at=clock.now())

    assert await sweep_missed_milestones(session, clock) == [milestone_id]


async def test_a_second_sweep_writes_nothing(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)
    milestone_id = await seed_milestone(
        session, w, status="IN_PROGRESS", target_at=clock.now() - timedelta(hours=1)
    )
    await sweep_missed_milestones(session, clock)

    assert await sweep_missed_milestones(session, clock) == []
    assert await audit_actions(session, milestone_id) == ["MILESTONE_MISSED"]


async def test_moving_the_clock_past_the_target_is_what_misses_it(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)
    milestone_id = await seed_milestone(
        session, w, status="IN_PROGRESS", target_at=clock.now() + timedelta(hours=1)
    )

    assert await sweep_missed_milestones(session, clock) == []
    clock.advance(hours=2)
    assert await sweep_missed_milestones(session, clock) == [milestone_id]


def test_the_sweep_is_registered_and_scheduled_on_the_one_beat() -> None:
    assert "drcc.sweep_missed_milestones" in celery_app.tasks
    [entry] = [
        e for e in celery_app.conf.beat_schedule.values() if e["task"] == "drcc.sweep_missed_milestones"
    ]
    assert entry["schedule"] == 60.0

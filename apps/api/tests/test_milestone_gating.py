"""Milestones inside the dependency graph: Task commands drive them, their gates release successors
only once ACHIEVED (BUILD-07 Verify: "milestone release tests"), and a Milestone never lets the graph
wait on itself (FROZEN_DECISIONS.md §7.9, D-224, ADR-041).

Graph edges: Task -> Task, gate Milestone -> Task, and *required* contribution Task -> Milestone (a
Milestone can't be READY until its required Tasks are COMPLETED)."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import FakeClock
from app.core.errors import AppError
from app.milestones.transition_service import MilestoneTransitionService
from app.tasks_dependencies.dependency_service import DependencyService
from app.tasks_dependencies.queries import event_graph_has_cycle, readiness
from app.tasks_dependencies.transition_service import TaskTransitionService
from tests.factories import (
    World,
    audit_actions,
    build_world,
    depend,
    seed_contribution,
    seed_gate,
    seed_milestone,
    seed_task,
)

pytestmark = [pytest.mark.domain]


async def _status(session: AsyncSession, table: str, row_id: uuid.UUID) -> str:
    return (
        await session.execute(text(f"SELECT status FROM {table} WHERE id = :i"), {"i": row_id})
    ).scalar_one()


async def _version(session: AsyncSession, table: str, row_id: uuid.UUID) -> int:
    return (
        await session.execute(text(f"SELECT version FROM {table} WHERE id = :i"), {"i": row_id})
    ).scalar_one()


async def _ready(session: AsyncSession, task_id: uuid.UUID) -> bool:
    from app.tasks_dependencies.models import Task

    task = await session.get(Task, task_id, populate_existing=True)
    assert task is not None
    return (await readiness(session, task)).ready


# --------------------------------------------------------------------------------------------
# Cycles through Milestones
# --------------------------------------------------------------------------------------------


async def test_a_gate_on_its_own_required_contributor_is_a_cycle(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)
    a = await seed_task(session, w)
    m = await seed_milestone(session, w)
    await seed_contribution(session, milestone_id=m, task_id=a)

    with pytest.raises(AppError) as exc:
        await DependencyService.add_milestone_gate(
            session, actor_id=w.admin_id, milestone_id=m, successor_task_id=a, clock=clock
        )

    assert (exc.value.status_code, exc.value.code) == (409, "DEPENDENCY_CYCLE")
    assert exc.value.details["cycle_path"] == [str(m), str(a), str(m)]


async def test_a_gate_closing_a_longer_mixed_cycle_is_rejected(
    session: AsyncSession, clock: FakeClock
) -> None:
    """A -> B (Task edge), B contributes to M, then gate M -> A."""
    w = await build_world(session)
    a, b = await seed_task(session, w), await seed_task(session, w)
    await depend(session, w, predecessor=a, successor=b)
    m = await seed_milestone(session, w)
    await seed_contribution(session, milestone_id=m, task_id=b)

    with pytest.raises(AppError) as exc:
        await DependencyService.add_milestone_gate(
            session, actor_id=w.admin_id, milestone_id=m, successor_task_id=a, clock=clock
        )

    assert exc.value.details["cycle_path"] == [str(m), str(a), str(b), str(m)]


async def test_a_task_edge_closing_a_cycle_through_a_milestone_is_rejected(
    session: AsyncSession, clock: FakeClock
) -> None:
    """A contributes to M, M gates B, then B -> A."""
    w = await build_world(session)
    a, b = await seed_task(session, w), await seed_task(session, w)
    m = await seed_milestone(session, w)
    await seed_contribution(session, milestone_id=m, task_id=a)
    await seed_gate(session, w, milestone_id=m, task_id=b)

    with pytest.raises(AppError) as exc:
        await DependencyService.add_task_dependency(
            session, actor_id=w.admin_id, predecessor_task_id=b, successor_task_id=a, clock=clock
        )

    assert exc.value.code == "DEPENDENCY_CYCLE"
    assert exc.value.details["cycle_path"] == [str(b), str(a), str(m), str(b)]


async def test_an_optional_contributor_never_closes_a_cycle(session: AsyncSession, clock: FakeClock) -> None:
    """A non-required contributor doesn't hold the Milestone, so gating it on that Milestone is fine."""
    w = await build_world(session)
    a = await seed_task(session, w)
    m = await seed_milestone(session, w)
    await seed_contribution(session, milestone_id=m, task_id=a, required=False)

    await DependencyService.add_milestone_gate(
        session, actor_id=w.admin_id, milestone_id=m, successor_task_id=a, clock=clock
    )


async def test_readiness_sees_a_mixed_cycle_written_around_the_api(session: AsyncSession) -> None:
    """D-224 `readiness.dependency_graph_acyclic` must catch Milestone edges too."""
    w = await build_world(session)
    a = await seed_task(session, w)
    m = await seed_milestone(session, w)
    assert not await event_graph_has_cycle(session, w.event_id)
    await seed_contribution(session, milestone_id=m, task_id=a)
    await seed_gate(session, w, milestone_id=m, task_id=a)

    assert await event_graph_has_cycle(session, w.event_id)


# --------------------------------------------------------------------------------------------
# Task commands drive their Milestones (same transaction)
# --------------------------------------------------------------------------------------------


async def test_task_commands_drive_the_milestone_through_its_states(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)
    first = await seed_task(session, w)
    second = await seed_task(session, w, status="READY_FOR_VALIDATION")
    m = await seed_milestone(session, w)
    for task_id in (first, second):
        await seed_contribution(session, milestone_id=m, task_id=task_id)
    svc = TaskTransitionService

    await svc.start(session, actor_id=w.executor_id, task_id=first, expected_version=1, clock=clock)
    assert await _status(session, "milestones", m) == "IN_PROGRESS"

    await svc.block(
        session, actor_id=w.executor_id, task_id=first, expected_version=2, reason="DNS", clock=clock
    )
    assert await _status(session, "milestones", m) == "AT_RISK"

    await session.execute(text("UPDATE blockers SET status = 'CLOSED' WHERE task_id = :t"), {"t": first})
    await svc.resume(session, actor_id=w.executor_id, task_id=first, expected_version=3, clock=clock)
    assert await _status(session, "milestones", m) == "IN_PROGRESS"

    await svc.validate(
        session, actor_id=w.system_owner_id, task_id=second, expected_version=1, approve=True, clock=clock
    )
    assert await _status(session, "milestones", m) == "IN_PROGRESS"  # `first` is still open

    await session.execute(
        text("UPDATE tasks SET status = 'READY_FOR_VALIDATION' WHERE id = :t"), {"t": first}
    )
    await session.execute(
        text(
            "INSERT INTO validations (id, target_type, target_id, status, submitted_at, verification_note, "
            "created_by_user_id, created_at, updated_at) "
            "VALUES (:id, 'TASK', :t, 'PENDING', now(), 'n', :u, now(), now())"
        ),
        {"id": uuid.uuid4(), "t": first, "u": w.teammate_id},
    )
    await svc.validate(
        session, actor_id=w.system_owner_id, task_id=first, expected_version=4, approve=True, clock=clock
    )
    assert await _status(session, "milestones", m) == "READY_FOR_CONFIRMATION"
    assert await audit_actions(session, m) == sorted(
        [
            "MILESTONE_STARTED",
            "MILESTONE_AT_RISK",
            "MILESTONE_BACK_ON_TRACK",
            "MILESTONE_READY_FOR_CONFIRMATION",
        ]
    )


async def test_the_triggering_task_and_actor_are_on_the_milestone_audit(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w)
    m = await seed_milestone(session, w)
    await seed_contribution(session, milestone_id=m, task_id=task_id)

    await TaskTransitionService.start(
        session, actor_id=w.executor_id, task_id=task_id, expected_version=1, clock=clock
    )

    row = (
        await session.execute(
            text("SELECT actor_user_id, metadata FROM audit_events WHERE entity_id = :m"), {"m": m}
        )
    ).one()
    assert row.actor_user_id == w.executor_id
    assert row.metadata["trigger_task_id"] == str(task_id)


async def test_cancelling_a_required_task_does_not_ready_the_milestone(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)
    done = await seed_task(session, w, status="COMPLETED")
    doomed = await seed_task(session, w, status="IN_PROGRESS")
    m = await seed_milestone(session, w, status="IN_PROGRESS")
    for task_id in (done, doomed):
        await seed_contribution(session, milestone_id=m, task_id=task_id)

    await TaskTransitionService.cancel(
        session, actor_id=w.admin_id, task_id=doomed, expected_version=1, reason="Descoped", clock=clock
    )

    assert await _status(session, "milestones", m) == "IN_PROGRESS"


async def test_a_task_in_no_milestone_touches_none(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w)
    m = await seed_milestone(session, w)

    await TaskTransitionService.start(
        session, actor_id=w.executor_id, task_id=task_id, expected_version=1, clock=clock
    )

    assert (await _status(session, "milestones", m), await _version(session, "milestones", m)) == (
        "NOT_STARTED",
        1,
    )


# --------------------------------------------------------------------------------------------
# Release: a HARD gate holds its successor until ACHIEVED -- and only until then
# --------------------------------------------------------------------------------------------


async def _gated(session: AsyncSession, w: World, **milestone: Any) -> tuple[uuid.UUID, uuid.UUID]:
    m = await seed_milestone(session, w, **milestone)
    successor = await seed_task(session, w)
    await seed_gate(session, w, milestone_id=m, task_id=successor)
    return m, successor


@pytest.mark.parametrize(
    "status", ["NOT_STARTED", "IN_PROGRESS", "AT_RISK", "READY_FOR_CONFIRMATION", "MISSED"]
)
async def test_the_successor_is_held_before_achieved(
    session: AsyncSession, clock: FakeClock, status: str
) -> None:
    w = await build_world(session)
    m, successor = await _gated(session, w, status=status)

    assert not await _ready(session, successor)
    with pytest.raises(AppError) as exc:
        await TaskTransitionService.start(
            session, actor_id=w.executor_id, task_id=successor, expected_version=1, clock=clock
        )
    assert exc.value.code == "DEPENDENCY_NOT_SATISFIED"
    assert exc.value.details["blocking_milestone_ids"] == [str(m)]


async def test_confirming_the_milestone_releases_its_successor(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)
    m, successor = await _gated(session, w, status="READY_FOR_CONFIRMATION")

    await MilestoneTransitionService.confirm(
        session, actor_id=w.coordinator_id, milestone_id=m, expected_version=1, clock=clock
    )

    assert await _ready(session, successor)
    await TaskTransitionService.start(
        session, actor_id=w.executor_id, task_id=successor, expected_version=1, clock=clock
    )
    assert await _status(session, "tasks", successor) == "IN_PROGRESS"


async def test_finishing_the_work_alone_does_not_release_a_manual_gate(
    session: AsyncSession, clock: FakeClock
) -> None:
    """MANUAL is the default for critical Milestones (FROZEN_DECISIONS.md §7.13): the work being done
    only makes it READY_FOR_CONFIRMATION; a human still has to confirm."""
    w = await build_world(session)
    m, successor = await _gated(session, w)
    contributor = await seed_task(session, w, status="READY_FOR_VALIDATION")
    await seed_contribution(session, milestone_id=m, task_id=contributor)

    await TaskTransitionService.validate(
        session,
        actor_id=w.system_owner_id,
        task_id=contributor,
        expected_version=1,
        approve=True,
        clock=clock,
    )

    assert await _status(session, "milestones", m) == "READY_FOR_CONFIRMATION"
    assert not await _ready(session, successor)


async def test_a_missed_gate_is_passed_only_by_an_audited_override(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)
    m, successor = await _gated(session, w, target_at=clock.now() - timedelta(hours=1))
    await MilestoneTransitionService.miss(session, milestone_id=m, clock=clock)

    await TaskTransitionService.start(
        session,
        actor_id=w.coordinator_id,
        task_id=successor,
        expected_version=1,
        override_reason="Network team confirmed by phone",
        clock=clock,
    )

    assert await _status(session, "tasks", successor) == "IN_PROGRESS"
    overrides = (
        (
            await session.execute(
                text("SELECT override_type FROM overrides WHERE target_id = :t"), {"t": successor}
            )
        )
        .scalars()
        .all()
    )
    assert list(overrides) == ["DEPENDENCY_OVERRIDE"]


async def test_an_advisory_gate_never_holds_its_successor(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)
    m = await seed_milestone(session, w)
    successor = await seed_task(session, w)
    await seed_gate(session, w, milestone_id=m, task_id=successor, strength="ADVISORY")

    assert await _ready(session, successor)

"""Performance smoke (BUILD-06 session d): a 5,000-Task DAG, locally, under 500 ms for
- the Event-wide readiness projection (`dependency_graph`: derived Ready for every Task, edges, and the
  blocked-path walk), and
- the add-edge cycle check, worst case (a walk of the whole graph).

Marker `load`: excluded from the default pytest run (CI runners are slower); `make test-load` and the
local `verify.sh` gate run it (BUILD-06.plan.md Risk #27)."""

from __future__ import annotations

import statistics
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.tasks_dependencies.queries import dependency_graph, find_cycle_path
from tests.factories import World, build_world

pytestmark = [pytest.mark.load]

N_TASKS = 5_000
BUDGET_S = 0.5


async def seed_big_dag(session: AsyncSession, w: World) -> tuple[uuid.UUID, uuid.UUID]:
    """Chain edges i-1 -> i (HARD) plus skip edges i-7 -> i (ADVISORY): ~10k edges. Every 3rd Task
    COMPLETED, every 500th BLOCKED (so blocked paths exist). Returns (first, last) Task ids."""
    await session.execute(
        text(
            "INSERT INTO tasks (id, dr_event_id, work_stream_id, title, phase, owning_team_id, "
            "created_by_user_id, sort_order, status) "
            "SELECT gen_random_uuid(), :e, :ws, 'Task ' || g, 'FAILOVER', :team, :u, g, "
            "(CASE WHEN g % 500 = 0 THEN 'BLOCKED' WHEN g % 3 = 0 THEN 'COMPLETED' ELSE 'NOT_STARTED' END)"
            "::task_status FROM generate_series(1, :n) AS g"
        ),
        {"e": w.event_id, "ws": w.work_stream_id, "team": w.team_id, "u": w.admin_id, "n": N_TASKS},
    )
    for offset, strength in ((1, "HARD"), (7, "ADVISORY")):
        await session.execute(
            text(
                "INSERT INTO task_dependencies "
                "(id, dr_event_id, predecessor_task_id, successor_task_id, strength, created_by_user_id) "
                "SELECT gen_random_uuid(), :e, p.id, s.id, CAST(:st AS dependency_strength), :u "
                "FROM tasks s JOIN tasks p "
                "ON p.dr_event_id = s.dr_event_id AND p.sort_order = s.sort_order - :o "
                "WHERE s.dr_event_id = :e"
            ),
            {"e": w.event_id, "u": w.admin_id, "st": strength, "o": offset},
        )
    await session.execute(text("ANALYZE tasks; ANALYZE task_dependencies"))
    ends = (
        await session.execute(
            text("SELECT id FROM tasks WHERE dr_event_id = :e AND sort_order IN (1, :n) ORDER BY sort_order"),
            {"e": w.event_id, "n": N_TASKS},
        )
    ).all()
    return ends[0].id, ends[1].id


async def median_seconds(fn: Callable[[], Awaitable[Any]], runs: int = 3) -> float:
    await fn()  # warm-up: statement compilation and plan caching aren't what's being measured
    samples = []
    for _ in range(runs):
        started = time.perf_counter()
        await fn()
        samples.append(time.perf_counter() - started)
    return statistics.median(samples)


async def test_event_wide_readiness_projection_for_5000_tasks(session: AsyncSession) -> None:
    w = await build_world(session)
    await seed_big_dag(session, w)

    graph = await dependency_graph(session, w.event_id)
    assert len([n for n in graph.nodes if n.kind == "TASK"]) == N_TASKS
    assert len(graph.edges) > 9_900
    assert len(graph.blocked_paths) == N_TASKS // 500

    elapsed = await median_seconds(lambda: dependency_graph(session, w.event_id))

    print(f"\ndependency_graph over {N_TASKS} tasks: {elapsed * 1000:.0f} ms (median of 3)")
    assert elapsed < BUDGET_S, f"{elapsed * 1000:.0f} ms >= {BUDGET_S * 1000:.0f} ms"


async def test_worst_case_cycle_check_for_5000_tasks(session: AsyncSession) -> None:
    """Adding last -> first would close a 5,000-long cycle: the walk has to traverse the whole graph."""
    w = await build_world(session)
    first, last = await seed_big_dag(session, w)

    path = await find_cycle_path(session, w.event_id, last, first)
    assert path is not None and path[0] == path[-1] == last

    elapsed = await median_seconds(lambda: find_cycle_path(session, w.event_id, last, first))

    print(f"\nfind_cycle_path across {N_TASKS} tasks: {elapsed * 1000:.0f} ms (median of 3)")
    assert elapsed < BUDGET_S, f"{elapsed * 1000:.0f} ms >= {BUDGET_S * 1000:.0f} ms"

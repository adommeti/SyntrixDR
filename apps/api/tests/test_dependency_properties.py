"""Hypothesis properties for the dependency DAG and derived Ready (tests.md; BUILD-06 Verify line:
"property/table-driven graph and transition tests").

Oracles here are deliberately independent of the code under test (a three-colour DFS for cycles,
plain reachability, the Ready rule written out) -- checking a function against itself proves nothing.
Pure properties run 200 examples; the DB-backed ones fewer, since each example seeds a fresh Event.
"""

from __future__ import annotations

import uuid
from collections import deque

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import FakeClock
from app.core.errors import AppError
from app.tasks_dependencies.dependency_service import DependencyService
from app.tasks_dependencies.models import Task
from app.tasks_dependencies.queries import (
    GateRef,
    Upstream,
    cycle_path,
    dependency_graph,
    derive_readiness,
    event_graph_has_cycle,
    has_cycle,
    readiness,
)
from tests.factories import build_world, depend, seed_gate, seed_milestone, seed_task

pytestmark = [pytest.mark.domain]

N = 8
NODES = [uuid.UUID(int=i + 1) for i in range(N)]
STATUSES = ["NOT_STARTED", "IN_PROGRESS", "BLOCKED", "READY_FOR_VALIDATION", "COMPLETED", "CANCELLED"]
MILESTONE_STATUSES = ["NOT_STARTED", "IN_PROGRESS", "AT_RISK", "READY_FOR_CONFIRMATION", "ACHIEVED", "MISSED"]
#: Weighted toward the states the projection branches on (roots are BLOCKED; the walk stops at
#: COMPLETED and CANCELLED) -- uniform draws over 6 Tasks rarely put a COMPLETED Task downstream of a
#: BLOCKED one, and mutation testing showed that let a traversal bug slip through.
INTERESTING_STATUSES = st.sampled_from(
    [*STATUSES, "BLOCKED", "BLOCKED", "COMPLETED", "COMPLETED", "CANCELLED"]
)

pairs = st.lists(st.tuples(st.integers(0, N - 1), st.integers(0, N - 1)), max_size=40)


# --------------------------------------------------------------------------------------------
# Independent oracles
# --------------------------------------------------------------------------------------------


def oracle_has_cycle(edges: list[tuple[uuid.UUID, uuid.UUID]]) -> bool:
    """Three-colour DFS."""
    succ: dict[uuid.UUID, list[uuid.UUID]] = {}
    for a, b in edges:
        succ.setdefault(a, []).append(b)
    colour: dict[uuid.UUID, int] = {}  # 0/absent white, 1 grey, 2 black

    def visit(n: uuid.UUID) -> bool:
        colour[n] = 1
        for m in succ.get(n, []):
            if colour.get(m) == 1 or (colour.get(m) is None and visit(m)):
                return True
        colour[n] = 2
        return False

    return any(colour.get(n) is None and visit(n) for n in list(succ))


def oracle_reaches(edges: list[tuple[uuid.UUID, uuid.UUID]], start: uuid.UUID, goal: uuid.UUID) -> bool:
    seen, stack = {start}, [start]
    while stack:
        n = stack.pop()
        if n == goal:
            return True
        for a, b in edges:
            if a == n and b not in seen:
                seen.add(b)
                stack.append(b)
    return False


# --------------------------------------------------------------------------------------------
# Pure properties (200 examples each)
# --------------------------------------------------------------------------------------------


@settings(max_examples=200, deadline=None)
@given(pairs)
def test_accepting_only_non_closing_edges_never_builds_a_cycle(proposals: list[tuple[int, int]]) -> None:
    """Replays DependencyService's decision rule: self edges and duplicates are rejected first, then an
    edge is accepted iff `cycle_path` finds nothing. The graph must stay acyclic throughout, and every
    cycle rejection must be justified by a genuine cycle through the proposed edge."""
    accepted: list[tuple[uuid.UUID, uuid.UUID]] = []
    for a, b in proposals:
        u, v = NODES[a], NODES[b]
        if u == v or (u, v) in accepted:
            continue
        path = cycle_path(accepted, u, v)
        if path is None:
            assert not oracle_reaches(accepted, v, u)
            accepted.append((u, v))
            assert not oracle_has_cycle(accepted)
        else:
            assert oracle_reaches(accepted, v, u)
            assert (path[0], path[1], path[-1]) == (u, v, u)
            closing = {*accepted, (u, v)}
            assert all((x, y) in closing for x, y in zip(path, path[1:], strict=False))


@settings(max_examples=200, deadline=None)
@given(pairs)
def test_has_cycle_agrees_with_an_independent_dfs(raw: list[tuple[int, int]]) -> None:
    edges = [(NODES[a], NODES[b]) for a, b in raw]
    assert has_cycle(edges) is oracle_has_cycle(edges)


@settings(max_examples=200, deadline=None)
@given(
    st.sampled_from(STATUSES),
    st.lists(
        st.tuples(
            st.sampled_from(["TASK", "MILESTONE"]), st.sampled_from(["HARD", "ADVISORY"]), st.booleans()
        ),
        max_size=10,
    ),
)
def test_derive_readiness_is_the_ready_rule(status: str, upstream: list[tuple[str, str, bool]]) -> None:
    """Ready = NOT_STARTED and every HARD upstream satisfied (STATE_MACHINES.md:50); ADVISORY never blocks
    but is reported while pending."""
    items = [
        Upstream(GateRef(uuid.UUID(int=i + 1), kind, None), strength, ok)
        for i, (kind, strength, ok) in enumerate(upstream)
    ]

    r = derive_readiness(status, items)

    hard_pending = {u.ref.id for u in items if u.strength == "HARD" and not u.satisfied}
    advisory_pending = {u.ref.id for u in items if u.strength == "ADVISORY" and not u.satisfied}
    assert r.ready is (status == "NOT_STARTED" and not hard_pending)
    assert {g.id for g in r.blocking} == hard_pending
    assert {g.id for g in r.advisory_pending} == advisory_pending


# --------------------------------------------------------------------------------------------
# DB-backed properties
# --------------------------------------------------------------------------------------------

_DB = settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])


@_DB
@given(
    st.lists(
        st.tuples(st.integers(0, 5), st.integers(0, 5), st.sampled_from(["HARD", "ADVISORY"])), max_size=15
    )
)
async def test_the_service_never_commits_a_cycle(
    session: AsyncSession, clock: FakeClock, proposals: list[tuple[int, int, str]]
) -> None:
    """Through the real DependencyService (lock, checks, insert): whatever is proposed, the Event graph
    stays acyclic and the only rejections are the three structural ones."""
    w = await build_world(session)  # fresh Event per example; the fixture transaction isolates nothing here
    tasks = [await seed_task(session, w) for _ in range(6)]
    for a, b, strength in proposals:
        try:
            await DependencyService.add_task_dependency(
                session,
                actor_id=w.admin_id,
                predecessor_task_id=tasks[a],
                successor_task_id=tasks[b],
                strength=strength,
                clock=clock,
            )
        except AppError as exc:
            assert exc.code in {"SELF_DEPENDENCY", "DEPENDENCY_EXISTS", "DEPENDENCY_CYCLE"}
        assert await event_graph_has_cycle(session, w.event_id) is False


@_DB
@given(
    statuses=st.lists(INTERESTING_STATUSES, min_size=6, max_size=6),
    edges=st.lists(
        st.tuples(st.integers(0, 5), st.integers(0, 5), st.sampled_from(["HARD", "ADVISORY"])),
        min_size=4,
        max_size=15,
    ),
    gates=st.lists(
        st.tuples(
            st.sampled_from(MILESTONE_STATUSES), st.integers(0, 5), st.sampled_from(["HARD", "ADVISORY"])
        ),
        max_size=3,
    ),
)
@example(
    statuses=["BLOCKED", "COMPLETED", "NOT_STARTED", "CANCELLED", "NOT_STARTED", "IN_PROGRESS"],
    edges=[(0, 1, "HARD"), (1, 2, "HARD"), (0, 3, "HARD"), (3, 4, "HARD"), (0, 5, "ADVISORY")],
    gates=[],
)
async def test_the_projection_matches_an_oracle_and_single_task_readiness(
    session: AsyncSession,
    statuses: list[str],
    edges: list[tuple[int, int, str]],
    gates: list[tuple[str, int, str]],
) -> None:
    """Random DAG (edges only point from lower to higher index, so it's acyclic by construction) with
    random Task/Milestone states. Every node's Ready and blocking set in `dependency_graph` equals the
    rule computed from the raw data, *and* equals the single-Task `readiness()` path -- the two
    must never drift. Blocked paths cover exactly the HARD-reachable, still-open Tasks."""
    w = await build_world(session)
    tasks = [await seed_task(session, w, status=s, active_blocker=True) for s in statuses]
    edge_set: dict[tuple[int, int], str] = {}
    for a, b, strength in edges:
        lo, hi = min(a, b), max(a, b)
        if lo != hi and (lo, hi) not in edge_set:
            edge_set[(lo, hi)] = strength
            await depend(session, w, predecessor=tasks[lo], successor=tasks[hi], strength=strength)
    gate_rows: list[tuple[uuid.UUID, int, str, str]] = []
    for ms_status, t, strength in gates:
        m = await seed_milestone(session, w, status=ms_status)
        await seed_gate(session, w, milestone_id=m, task_id=tasks[t], strength=strength)
        gate_rows.append((m, t, ms_status, strength))

    graph = await dependency_graph(session, w.event_id)
    nodes = {n.id: n for n in graph.nodes}

    for i, tid in enumerate(tasks):
        hard_blocking = {
            tasks[a]
            for (a, b), s in edge_set.items()
            if b == i and s == "HARD" and statuses[a] != "COMPLETED"
        }
        hard_blocking |= {m for m, t, ms, s in gate_rows if t == i and s == "HARD" and ms != "ACHIEVED"}
        expected_ready = statuses[i] == "NOT_STARTED" and not hard_blocking
        assert (nodes[tid].ready, set(nodes[tid].blocked_by)) == (expected_ready, hard_blocking)

        task = await session.get(Task, tid)
        assert task is not None
        single = await readiness(session, task)
        assert (single.ready, {g.id for g in single.blocking}) == (
            nodes[tid].ready,
            set(nodes[tid].blocked_by),
        )

    hard_succ = {i: [b for (a, b), s in edge_set.items() if a == i and s == "HARD"] for i in range(6)}
    for path in graph.blocked_paths:
        root = tasks.index(path.root_task_id)
        expected, queue, seen = set(), deque([root]), {root}
        while queue:
            for nxt in hard_succ[queue.popleft()]:
                if nxt not in seen and statuses[nxt] not in ("COMPLETED", "CANCELLED"):
                    seen.add(nxt)
                    expected.add(tasks[nxt])
                    queue.append(nxt)
        assert set(path.downstream_task_ids) == expected
    assert {p.root_task_id for p in graph.blocked_paths} == {
        t for t, s in zip(tasks, statuses, strict=True) if s == "BLOCKED"
    }

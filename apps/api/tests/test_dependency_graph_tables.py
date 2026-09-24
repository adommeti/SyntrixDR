"""Table-driven graph tests (BUILD-06 Verify: "property/table-driven graph ... tests"). Each case is a
small named graph declared as data -- Tasks and their states, edges, Milestones and gates -- with the
expected derived Ready, blocking and advisory sets, and blocked-path impact. A second table proposes one
edge against a base graph and names the outcome."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import FakeClock
from app.core.errors import AppError
from app.tasks_dependencies.dependency_service import DependencyService
from app.tasks_dependencies.models import Task
from app.tasks_dependencies.queries import dependency_graph, readiness
from tests.factories import World, build_world, depend, seed_gate, seed_milestone, seed_task

pytestmark = [pytest.mark.domain]

H, A = "HARD", "ADVISORY"


@dataclass(frozen=True)
class GraphCase:
    tasks: dict[str, str]
    edges: list[tuple[str, str, str]]
    ready: dict[str, bool]
    blocked_by: dict[str, set[str]] = field(default_factory=dict)
    advisory: dict[str, set[str]] = field(default_factory=dict)
    impact: dict[str, set[str]] = field(default_factory=dict)
    milestones: dict[str, str] = field(default_factory=dict)
    gates: list[tuple[str, str, str]] = field(default_factory=list)


GRAPHS: dict[str, GraphCase] = {
    "chain": GraphCase(
        tasks={"a": "COMPLETED", "b": "NOT_STARTED", "c": "NOT_STARTED"},
        edges=[("a", "b", H), ("b", "c", H)],
        ready={"b": True, "c": False},
        blocked_by={"b": set(), "c": {"b"}},
    ),
    "diamond waits on its slowest branch": GraphCase(
        tasks={"a": "COMPLETED", "b": "COMPLETED", "c": "IN_PROGRESS", "d": "NOT_STARTED"},
        edges=[("a", "b", H), ("a", "c", H), ("b", "d", H), ("c", "d", H)],
        ready={"d": False},
        blocked_by={"d": {"c"}},
    ),
    "fan-in, all done": GraphCase(
        tasks={"a": "COMPLETED", "b": "COMPLETED", "c": "COMPLETED", "d": "NOT_STARTED"},
        edges=[("a", "d", H), ("b", "d", H), ("c", "d", H)],
        ready={"d": True},
        blocked_by={"d": set()},
    ),
    "advisory only warns": GraphCase(
        tasks={"a": "NOT_STARTED", "b": "NOT_STARTED"},
        edges=[("a", "b", A)],
        ready={"a": True, "b": True},
        advisory={"b": {"a"}},
    ),
    "mixed strengths": GraphCase(
        tasks={"a": "IN_PROGRESS", "b": "NOT_STARTED", "c": "NOT_STARTED"},
        edges=[("a", "c", A), ("b", "c", H)],
        ready={"c": False},
        blocked_by={"c": {"b"}},
        advisory={"c": {"a"}},
    ),
    "a cancelled predecessor still blocks": GraphCase(
        tasks={"a": "CANCELLED", "b": "NOT_STARTED"},
        edges=[("a", "b", H)],
        ready={"b": False},
        blocked_by={"b": {"a"}},
    ),
    "gates": GraphCase(
        tasks={"a": "NOT_STARTED", "b": "NOT_STARTED", "c": "NOT_STARTED", "d": "NOT_STARTED"},
        edges=[],
        milestones={"m_open": "NOT_STARTED", "m_done": "ACHIEVED", "m_missed": "MISSED", "m_adv": "AT_RISK"},
        gates=[("m_open", "a", H), ("m_done", "b", H), ("m_missed", "c", H), ("m_adv", "d", A)],
        ready={"a": False, "b": True, "c": False, "d": True},
        blocked_by={"a": {"m_open"}, "b": set(), "c": {"m_missed"}},
        advisory={"d": {"m_adv"}},
    ),
    "a task and a gate together": GraphCase(
        tasks={"a": "IN_PROGRESS", "b": "NOT_STARTED"},
        edges=[("a", "b", H)],
        milestones={"m": "READY_FOR_CONFIRMATION"},
        gates=[("m", "b", H)],
        ready={"b": False},
        blocked_by={"b": {"a", "m"}},
    ),
    "started work is never ready": GraphCase(
        tasks={"a": "IN_PROGRESS", "b": "BLOCKED", "c": "READY_FOR_VALIDATION", "d": "COMPLETED"},
        edges=[],
        ready={"a": False, "b": False, "c": False, "d": False},
        impact={"b": set()},  # a BLOCKED Task is a blocked-path root even with nothing downstream
    ),
    "blocked path stops at completed and cancelled work": GraphCase(
        tasks={
            "r": "BLOCKED",
            "done": "COMPLETED",
            "after_done": "NOT_STARTED",
            "gone": "CANCELLED",
            "after_gone": "NOT_STARTED",
            "held": "NOT_STARTED",
            "held_further": "NOT_STARTED",
            "advisory_only": "NOT_STARTED",
        },
        edges=[
            ("r", "done", H),
            ("done", "after_done", H),
            ("r", "gone", H),
            ("gone", "after_gone", H),
            ("r", "held", H),
            ("held", "held_further", H),
            ("r", "advisory_only", A),
        ],
        ready={"after_done": True, "after_gone": False, "held": False, "advisory_only": True},
        impact={"r": {"held", "held_further"}},
    ),
    "overlapping blocked paths": GraphCase(
        tasks={"r1": "BLOCKED", "r2": "BLOCKED", "s": "NOT_STARTED", "t": "NOT_STARTED"},
        edges=[("r1", "s", H), ("r2", "s", H), ("s", "t", H)],
        ready={"s": False, "t": False},
        impact={"r1": {"s", "t"}, "r2": {"s", "t"}},
    ),
    "a blocked task further downstream is itself impacted": GraphCase(
        tasks={"r": "BLOCKED", "mid": "BLOCKED", "end": "NOT_STARTED"},
        edges=[("r", "mid", H), ("mid", "end", H)],
        ready={"end": False},
        impact={"r": {"mid", "end"}, "mid": {"end"}},
    ),
}


async def build(session: AsyncSession, w: World, case: GraphCase) -> dict[str, uuid.UUID]:
    ids = {name: await seed_task(session, w, status=status) for name, status in case.tasks.items()}
    for pred, succ, strength in case.edges:
        await depend(session, w, predecessor=ids[pred], successor=ids[succ], strength=strength)
    for name, status in case.milestones.items():
        ids[name] = await seed_milestone(session, w, status=status)
    for milestone, task, strength in case.gates:
        await seed_gate(session, w, milestone_id=ids[milestone], task_id=ids[task], strength=strength)
    return ids


@pytest.mark.parametrize("name", list(GRAPHS))
async def test_graph_table(session: AsyncSession, name: str) -> None:
    case = GRAPHS[name]
    w = await build_world(session)
    ids = await build(session, w, case)
    names = {v: k for k, v in ids.items()}

    graph = await dependency_graph(session, w.event_id)
    nodes = {n.id: n for n in graph.nodes}

    for task, expected in case.ready.items():
        assert nodes[ids[task]].ready is expected, f"{name}: ready({task})"
    for task, expected in case.blocked_by.items():
        assert {names[i] for i in nodes[ids[task]].blocked_by} == expected, f"{name}: blocked_by({task})"
    for task, expected in case.advisory.items():
        assert {names[i] for i in nodes[ids[task]].advisory_pending} == expected, f"{name}: advisory({task})"
    impact = {names[p.root_task_id]: {names[i] for i in p.downstream_task_ids} for p in graph.blocked_paths}
    assert impact == case.impact, f"{name}: blocked paths"

    # The single-Task path must agree with the Event-wide projection on every Task.
    for task_name in case.tasks:
        task = await session.get(Task, ids[task_name])
        assert task is not None
        single = await readiness(session, task)
        assert single.ready is nodes[ids[task_name]].ready
        assert {g.id for g in single.blocking} == set(nodes[ids[task_name]].blocked_by)


# --------------------------------------------------------------------------------------------
# Proposed edge -> outcome
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class EdgeCase:
    base: list[tuple[str, str, str]]
    propose: tuple[str, str, str]
    outcome: str  # "ACCEPT" | error code
    cycle: list[str] | None = None  # exact path, when it's unique
    cycle_length: int | None = None  # when several shortest cycles exist
    remove_first: bool = False  # soft-delete every base edge before proposing


EDGES: dict[str, EdgeCase] = {
    "closing a chain": EdgeCase(
        [("a", "b", H), ("b", "c", H)], ("c", "a", H), "DEPENDENCY_CYCLE", ["c", "a", "b", "c"]
    ),
    "a back edge": EdgeCase([("a", "b", H)], ("b", "a", H), "DEPENDENCY_CYCLE", ["b", "a", "b"]),
    "a transitive shortcut is fine": EdgeCase([("a", "b", H), ("b", "c", H)], ("a", "c", H), "ACCEPT"),
    "joining parallel branches is fine": EdgeCase([("a", "b", H), ("c", "d", H)], ("b", "c", H), "ACCEPT"),
    "a self edge": EdgeCase([], ("a", "a", H), "SELF_DEPENDENCY"),
    "an exact duplicate": EdgeCase([("a", "b", H)], ("a", "b", H), "DEPENDENCY_EXISTS"),
    "a duplicate of another strength": EdgeCase([("a", "b", H)], ("a", "b", A), "DEPENDENCY_EXISTS"),
    "an advisory base edge still counts": EdgeCase(
        [("a", "b", A)], ("b", "a", H), "DEPENDENCY_CYCLE", ["b", "a", "b"]
    ),
    "an advisory proposal still counts": EdgeCase(
        [("a", "b", H)], ("b", "a", A), "DEPENDENCY_CYCLE", ["b", "a", "b"]
    ),
    "a removed edge no longer counts": EdgeCase([("a", "b", H)], ("b", "a", H), "ACCEPT", remove_first=True),
    "a diamond's back edge": EdgeCase(
        [("a", "b", H), ("a", "c", H), ("b", "d", H), ("c", "d", H)],
        ("d", "a", H),
        "DEPENDENCY_CYCLE",
        cycle_length=4,
    ),
    "closing a long chain": EdgeCase(
        [("a", "b", H), ("b", "c", H), ("c", "d", H), ("d", "e", H)],
        ("e", "a", H),
        "DEPENDENCY_CYCLE",
        ["e", "a", "b", "c", "d", "e"],
    ),
}


@pytest.mark.parametrize("name", list(EDGES))
async def test_edge_table(session: AsyncSession, clock: FakeClock, name: str) -> None:
    case = EDGES[name]
    w = await build_world(session)
    letters = sorted({n for e in [*case.base, case.propose] for n in e[:2]})
    ids = {n: await seed_task(session, w) for n in letters}
    names = {v: k for k, v in ids.items()}
    base_ids = []
    for pred, succ, strength in case.base:
        edge = await DependencyService.add_task_dependency(
            session,
            actor_id=w.admin_id,
            predecessor_task_id=ids[pred],
            successor_task_id=ids[succ],
            strength=strength,
            clock=clock,
        )
        base_ids.append(edge.id)
    if case.remove_first:
        for edge_id in base_ids:
            await DependencyService.remove_task_dependency(
                session, actor_id=w.admin_id, dependency_id=edge_id, clock=clock
            )

    pred, succ, strength = case.propose
    try:
        await DependencyService.add_task_dependency(
            session,
            actor_id=w.admin_id,
            predecessor_task_id=ids[pred],
            successor_task_id=ids[succ],
            strength=strength,
            clock=clock,
        )
        outcome, path = "ACCEPT", None
    except AppError as exc:
        outcome, path = exc.code, exc.details.get("cycle_path")

    assert outcome == case.outcome
    if case.cycle is not None:
        assert path is not None
        assert [names[uuid.UUID(p)] for p in path] == case.cycle
    if case.cycle_length is not None:
        assert path is not None
        steps = [names[uuid.UUID(p)] for p in path]
        live = {(p, s) for p, s, _ in case.base} | {(pred, succ)}
        assert len(steps) == case.cycle_length and steps[0] == steps[-1] == pred
        assert all((x, y) in live for x, y in zip(steps, steps[1:], strict=False))

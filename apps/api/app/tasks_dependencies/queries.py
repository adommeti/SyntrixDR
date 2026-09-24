"""Read side of the dependency graph: derived Ready, cycle detection, and the Event graph projection.

The graph is Event-scoped. Nodes: Tasks and Milestones. Edges: `task_dependencies` (Task -> Task) and
`milestone_dependencies` (Milestone -> Task gate). Milestones never have incoming edges here --
`milestone_tasks` is composition ("contributes/aggregates", DATA_MODEL.md:163-164) and confirmation is
MANUAL by default, so it isn't a gate -- which is why cycles can only form among Tasks.
"""

from __future__ import annotations

import uuid
from collections import Counter, defaultdict, deque
from dataclasses import dataclass

from sqlalchemy import DateTime, Select, Text, column, select, table
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.ext.asyncio import AsyncSession

from app.blockers.queries import count_active_blockers_by_task
from app.dr_events.participants import user_can_see_event
from app.dr_events.queries import get_event
from app.tasks_dependencies.models import MilestoneDependency, Task, TaskDependency
from app.work_streams.queries import list_monitoring_stream_ids

#: select()-only view of `milestones`. BUILD-07 owns the real model (BUILD-06.plan.md Risk #13); this
#: isn't on `Base.metadata`, so it can't collide with `core/external_refs.py`'s FK stub or that model.
milestones_view = table(
    "milestones",
    column("id", PgUUID(as_uuid=True)),
    column("dr_event_id", PgUUID(as_uuid=True)),
    column("work_stream_id", PgUUID(as_uuid=True)),
    column("dr_application_id", PgUUID(as_uuid=True)),
    column("name", Text),
    column("status", Text),
    column("deleted_at", DateTime(timezone=True)),
)

_TASK_SATISFIED = "COMPLETED"
_MILESTONE_SATISFIED = "ACHIEVED"
_NOT_TRAVERSED = frozenset({"COMPLETED", "CANCELLED"})


# --------------------------------------------------------------------------------------------
# Derived Ready
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class GateRef:
    """One upstream thing a Task waits on: a predecessor Task or a gating Milestone."""

    id: uuid.UUID
    kind: str  # "TASK" | "MILESTONE"
    work_stream_id: uuid.UUID | None


@dataclass(frozen=True)
class Readiness:
    status_ok: bool
    blocking: tuple[GateRef, ...]  # HARD and unsatisfied
    advisory_pending: tuple[GateRef, ...]  # ADVISORY and unsatisfied -- warns, never blocks

    @property
    def ready(self) -> bool:
        return self.status_ok and not self.blocking

    def ids(self, kind: str, *, advisory: bool = False) -> list[str]:
        refs = self.advisory_pending if advisory else self.blocking
        return [str(g.id) for g in refs if g.kind == kind]


@dataclass(frozen=True)
class _Upstream:
    ref: GateRef
    strength: str
    satisfied: bool


def _derive(task_status: str, upstream: list[_Upstream]) -> Readiness:
    """The Ready rule (STATE_MACHINES.md:50), in one place. Strict: a CANCELLED predecessor or a MISSED
    Milestone is not satisfied -- only an audited override gets past it."""
    order = sorted(upstream, key=lambda u: (u.ref.kind, str(u.ref.id)))
    return Readiness(
        status_ok=task_status == "NOT_STARTED",
        blocking=tuple(u.ref for u in order if u.strength == "HARD" and not u.satisfied),
        advisory_pending=tuple(u.ref for u in order if u.strength == "ADVISORY" and not u.satisfied),
    )


async def _upstream_by_task(
    session: AsyncSession, task_ids: list[uuid.UUID] | Select[tuple[uuid.UUID]]
) -> dict[uuid.UUID, list[_Upstream]]:
    """Every live incoming edge and gate for these Tasks -- a list of ids, or a subquery selecting them
    (what the Event-wide projection passes, to avoid thousands of bind parameters). Edges from a
    soft-deleted Task and gates of a soft-deleted Milestone are dead and ignored."""
    by_task: dict[uuid.UUID, list[_Upstream]] = defaultdict(list)
    if isinstance(task_ids, list) and not task_ids:
        return by_task
    preds = await session.execute(
        select(
            TaskDependency.successor_task_id,
            TaskDependency.strength,
            Task.id,
            Task.status,
            Task.work_stream_id,
        )
        .join(Task, Task.id == TaskDependency.predecessor_task_id)
        .where(
            TaskDependency.successor_task_id.in_(task_ids),
            TaskDependency.dependency_type == "FINISH_TO_START",
            TaskDependency.deleted_at.is_(None),
            Task.deleted_at.is_(None),
        )
    )
    for successor_id, strength, pred_id, pred_status, pred_ws in preds:
        by_task[successor_id].append(
            _Upstream(GateRef(pred_id, "TASK", pred_ws), strength, pred_status == _TASK_SATISFIED)
        )
    gates = await session.execute(
        select(
            MilestoneDependency.successor_task_id,
            MilestoneDependency.strength,
            milestones_view.c.id,
            milestones_view.c.status,
            milestones_view.c.work_stream_id,
        )
        .join(milestones_view, milestones_view.c.id == MilestoneDependency.milestone_id)
        .where(
            MilestoneDependency.successor_task_id.in_(task_ids),
            MilestoneDependency.deleted_at.is_(None),
            milestones_view.c.deleted_at.is_(None),
        )
    )
    for successor_id, strength, milestone_id, milestone_status, milestone_ws in gates:
        by_task[successor_id].append(
            _Upstream(
                GateRef(milestone_id, "MILESTONE", milestone_ws),
                strength,
                milestone_status == _MILESTONE_SATISFIED,
            )
        )
    return by_task


async def readiness(session: AsyncSession, task: Task) -> Readiness:
    return _derive(task.status, (await _upstream_by_task(session, [task.id]))[task.id])


async def is_ready(session: AsyncSession, task: Task) -> bool:
    """Derived Ready -- never persisted (it isn't a status)."""
    return (await readiness(session, task)).ready


# --------------------------------------------------------------------------------------------
# Lookups
# --------------------------------------------------------------------------------------------


async def get_visible_task(session: AsyncSession, actor_id: uuid.UUID, task_id: uuid.UUID) -> Task | None:
    """None both when the Task doesn't exist and when its Event isn't visible to the actor -- the
    caller renders both as the same 404 (invariant #1)."""
    task = (
        await session.execute(select(Task).where(Task.id == task_id, Task.deleted_at.is_(None)))
    ).scalar_one_or_none()
    if task is None or not await user_can_see_event(session, actor_id, task.dr_event_id):
        return None
    return task


@dataclass(frozen=True)
class MilestoneRef:
    id: uuid.UUID
    dr_event_id: uuid.UUID
    work_stream_id: uuid.UUID | None


async def get_milestone(session: AsyncSession, milestone_id: uuid.UUID) -> MilestoneRef | None:
    row = (
        await session.execute(
            select(
                milestones_view.c.id, milestones_view.c.dr_event_id, milestones_view.c.work_stream_id
            ).where(milestones_view.c.id == milestone_id, milestones_view.c.deleted_at.is_(None))
        )
    ).one_or_none()
    return MilestoneRef(row[0], row[1], row[2]) if row is not None else None


async def live_milestone_gate_exists(
    session: AsyncSession, *, milestone_id: uuid.UUID, successor_task_id: uuid.UUID
) -> bool:
    result = await session.execute(
        select(MilestoneDependency.id).where(
            MilestoneDependency.milestone_id == milestone_id,
            MilestoneDependency.successor_task_id == successor_task_id,
            MilestoneDependency.deleted_at.is_(None),
        )
    )
    return result.first() is not None


# --------------------------------------------------------------------------------------------
# Cycles
# --------------------------------------------------------------------------------------------


async def _live_task_edges(
    session: AsyncSession, dr_event_id: uuid.UUID
) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """Every live Task -> Task edge in the Event, any strength: an ADVISORY cycle is still a cycle."""
    rows = await session.execute(
        select(TaskDependency.predecessor_task_id, TaskDependency.successor_task_id).where(
            TaskDependency.dr_event_id == dr_event_id, TaskDependency.deleted_at.is_(None)
        )
    )
    return [(p, s) for p, s in rows]


async def find_cycle_path(
    session: AsyncSession, dr_event_id: uuid.UUID, predecessor_id: uuid.UUID, successor_id: uuid.UUID
) -> list[uuid.UUID] | None:
    """If adding `predecessor -> successor` would close a directed cycle, that cycle as
    `[predecessor, successor, ..., predecessor]`; otherwise None. The caller must hold the Event's
    dependency-graph lock, or a concurrent insert could close a cycle this check never saw."""
    successors: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
    for p, s in await _live_task_edges(session, dr_event_id):
        successors[p].append(s)

    parent: dict[uuid.UUID, uuid.UUID | None] = {successor_id: None}
    queue = deque([successor_id])
    while queue:
        node = queue.popleft()
        if node == predecessor_id:
            path: list[uuid.UUID] = []
            cursor: uuid.UUID | None = node
            while cursor is not None:
                path.append(cursor)
                cursor = parent[cursor]
            return [predecessor_id, *reversed(path)]
        for nxt in sorted(successors[node], key=str):
            if nxt not in parent:
                parent[nxt] = node
                queue.append(nxt)
    return None


async def event_graph_has_cycle(session: AsyncSession, dr_event_id: uuid.UUID) -> bool:
    """Kahn's algorithm over the Event's live Task edges. The API rejects cycles at write time, so this
    only ever fires on rows written around it -- which is what D-224's readiness key is for."""
    edges = await _live_task_edges(session, dr_event_id)
    successors: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
    indegree: Counter[uuid.UUID] = Counter()
    nodes: set[uuid.UUID] = set()
    for p, s in edges:
        successors[p].append(s)
        indegree[s] += 1
        nodes.update((p, s))
    queue = deque(n for n in nodes if indegree[n] == 0)
    removed = 0
    while queue:
        node = queue.popleft()
        removed += 1
        for nxt in successors[node]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)
    return removed != len(nodes)


# --------------------------------------------------------------------------------------------
# Graph projection with blocked-path impact (UI_UX.md:191-196)
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class GraphNode:
    id: uuid.UUID
    kind: str  # "TASK" | "MILESTONE"
    label: str
    status: str
    dr_application_id: uuid.UUID | None
    work_stream_id: uuid.UUID | None
    ready: bool | None  # Tasks only
    blocked_by: list[uuid.UUID]  # why it can't proceed: HARD upstream not yet satisfied
    advisory_pending: list[uuid.UUID]
    active_blocker_count: int


@dataclass(frozen=True)
class GraphEdge:
    id: uuid.UUID
    kind: str  # "TASK_DEPENDENCY" | "MILESTONE_GATE"
    from_id: uuid.UUID
    to_id: uuid.UUID
    strength: str


@dataclass(frozen=True)
class BlockedPath:
    """What a BLOCKED Task is holding up: Tasks reachable over HARD edges, not passing through a
    COMPLETED Task (its successors are no longer held by the root) and skipping CANCELLED ones."""

    root_task_id: uuid.UUID
    downstream_task_ids: list[uuid.UUID]
    impacted_dr_application_ids: list[uuid.UUID]  # the root's and the downstream Tasks'


@dataclass(frozen=True)
class DependencyGraph:
    dr_event_id: uuid.UUID
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    blocked_paths: list[BlockedPath]


async def dependency_graph(session: AsyncSession, dr_event_id: uuid.UUID) -> DependencyGraph:
    """Event-wide projection. Two things keep it inside the 5,000-Task / 500 ms smoke budget
    (tests/test_dependency_performance.py): every "these Tasks" filter is the Event-scoped `live_tasks`
    subquery, never a bound list of ids (5,000 bind parameters cost more than the query), and rows are
    read as column tuples, not ORM instances."""
    live_tasks = select(Task.id).where(Task.dr_event_id == dr_event_id, Task.deleted_at.is_(None))
    tasks = (
        await session.execute(
            select(Task.id, Task.title, Task.status, Task.dr_application_id, Task.work_stream_id)
            .where(Task.dr_event_id == dr_event_id, Task.deleted_at.is_(None))
            .order_by(Task.id)
        )
    ).all()
    task_by_id = {t.id: t for t in tasks}
    milestones = (
        await session.execute(
            select(
                milestones_view.c.id,
                milestones_view.c.name,
                milestones_view.c.status,
                milestones_view.c.dr_application_id,
                milestones_view.c.work_stream_id,
            )
            .where(milestones_view.c.dr_event_id == dr_event_id, milestones_view.c.deleted_at.is_(None))
            .order_by(milestones_view.c.id)
        )
    ).all()
    milestone_ids = {m[0] for m in milestones}

    task_edges = [
        e
        for e in (
            await session.execute(
                select(
                    TaskDependency.id,
                    TaskDependency.predecessor_task_id,
                    TaskDependency.successor_task_id,
                    TaskDependency.strength,
                )
                .where(TaskDependency.dr_event_id == dr_event_id, TaskDependency.deleted_at.is_(None))
                .order_by(TaskDependency.id)
            )
        ).all()
        if e.predecessor_task_id in task_by_id and e.successor_task_id in task_by_id
    ]
    gates = [
        g
        for g in (
            await session.execute(
                select(
                    MilestoneDependency.id,
                    MilestoneDependency.milestone_id,
                    MilestoneDependency.successor_task_id,
                    MilestoneDependency.strength,
                )
                .where(
                    MilestoneDependency.successor_task_id.in_(live_tasks),
                    MilestoneDependency.deleted_at.is_(None),
                )
                .order_by(MilestoneDependency.id)
            )
        ).all()
        if g.milestone_id in milestone_ids
    ]

    upstream = await _upstream_by_task(session, live_tasks)
    blocker_counts = await count_active_blockers_by_task(session, live_tasks)

    nodes: list[GraphNode] = []
    for t in tasks:
        r = _derive(t.status, upstream[t.id])
        nodes.append(
            GraphNode(
                id=t.id,
                kind="TASK",
                label=t.title,
                status=t.status,
                dr_application_id=t.dr_application_id,
                work_stream_id=t.work_stream_id,
                ready=r.ready,
                blocked_by=[g.id for g in r.blocking],
                advisory_pending=[g.id for g in r.advisory_pending],
                active_blocker_count=blocker_counts.get(t.id, 0),
            )
        )
    for m_id, m_name, m_status, m_app, m_ws in milestones:
        nodes.append(
            GraphNode(
                id=m_id,
                kind="MILESTONE",
                label=m_name,
                status=m_status,
                dr_application_id=m_app,
                work_stream_id=m_ws,
                ready=None,
                blocked_by=[],
                advisory_pending=[],
                active_blocker_count=0,
            )
        )

    edges = [
        GraphEdge(e.id, "TASK_DEPENDENCY", e.predecessor_task_id, e.successor_task_id, e.strength)
        for e in task_edges
    ] + [GraphEdge(g.id, "MILESTONE_GATE", g.milestone_id, g.successor_task_id, g.strength) for g in gates]

    hard_successors: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
    for e in task_edges:
        if e.strength == "HARD":
            hard_successors[e.predecessor_task_id].append(e.successor_task_id)

    blocked_paths: list[BlockedPath] = []
    for root in (t for t in tasks if t.status == "BLOCKED"):
        seen: set[uuid.UUID] = {root.id}
        downstream: list[uuid.UUID] = []
        queue = deque([root.id])
        while queue:
            for nxt in sorted(hard_successors[queue.popleft()], key=str):
                if nxt in seen or task_by_id[nxt].status in _NOT_TRAVERSED:
                    continue
                seen.add(nxt)
                downstream.append(nxt)
                queue.append(nxt)
        apps = {task_by_id[i].dr_application_id for i in (root.id, *downstream)}
        blocked_paths.append(
            BlockedPath(
                root_task_id=root.id,
                downstream_task_ids=downstream,
                impacted_dr_application_ids=sorted((a for a in apps if a is not None), key=str),
            )
        )

    return DependencyGraph(dr_event_id=dr_event_id, nodes=nodes, edges=edges, blocked_paths=blocked_paths)


async def get_visible_dependency_graph(
    session: AsyncSession, actor_id: uuid.UUID, dr_event_id: uuid.UUID
) -> DependencyGraph | None:
    """None for a missing Event and for one the actor can't see alike (invariant #1)."""
    if await get_event(session, dr_event_id) is None:
        return None
    if not await user_can_see_event(session, actor_id, dr_event_id):
        return None
    return await dependency_graph(session, dr_event_id)


async def list_tasks(session: AsyncSession, dr_event_id: uuid.UUID) -> list[Task]:
    """Live Tasks in plan order: `sort_order` (unset last), then creation."""
    result = await session.execute(
        select(Task)
        .where(Task.dr_event_id == dr_event_id, Task.deleted_at.is_(None))
        .order_by(Task.sort_order.asc().nulls_last(), Task.created_at, Task.id)
    )
    return list(result.scalars().all())


async def unfinished_monitoring_task_ids(session: AsyncSession, dr_event_id: uuid.UUID) -> list[uuid.UUID]:
    """D-227: non-cancelled, non-COMPLETED Tasks in the Event's MONITORING Work Streams."""
    streams = await list_monitoring_stream_ids(session, dr_event_id)
    if not streams:
        return []
    result = await session.execute(
        select(Task.id)
        .where(
            Task.work_stream_id.in_(streams),
            Task.deleted_at.is_(None),
            Task.status.not_in(("COMPLETED", "CANCELLED")),
        )
        .order_by(Task.id)
    )
    return list(result.scalars().all())


async def live_monitoring_task_exists(session: AsyncSession, dr_event_id: uuid.UUID) -> bool:
    """D-224 `readiness.monitoring_task_present`: at least one non-cancelled MONITORING-stream Task."""
    streams = await list_monitoring_stream_ids(session, dr_event_id)
    if not streams:
        return False
    result = await session.execute(
        select(Task.id).where(
            Task.work_stream_id.in_(streams), Task.deleted_at.is_(None), Task.status != "CANCELLED"
        )
    )
    return result.first() is not None


async def owning_team_ids(session: AsyncSession, dr_event_id: uuid.UUID) -> list[uuid.UUID]:
    result = await session.execute(
        select(Task.owning_team_id)
        .where(Task.dr_event_id == dr_event_id, Task.deleted_at.is_(None))
        .distinct()
    )
    return list(result.scalars().all())

"""The only way a dependency edge or Milestone gate is created or removed through the API
(invariant #2: no self edges, no directed cycles, non-configurable).

Every write runs: resolve both ends (404 when invisible) -> same-Event check -> authority over the
successor -> take the Event's dependency-graph lock -> duplicate check -> cycle check -> write ->
audit + outbox, all in the caller's transaction.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import write_audit
from app.core.clock import Clock, SystemClock
from app.core.errors import AppError
from app.core.outbox import write_outbox
from app.dr_events.participants import user_can_see_event
from app.tasks_dependencies.commands import TaskNotFoundError, dependency_exists, lock_event_dependency_graph
from app.tasks_dependencies.models import MilestoneDependency, Task, TaskDependency
from app.tasks_dependencies.policies import actor_may_change_dependency
from app.tasks_dependencies.queries import (
    find_cycle_path,
    get_milestone,
    get_visible_task,
    live_milestone_gate_exists,
)
from app.users_teams_org.authorization import AuthorizationRequiredError


class SelfDependencyError(AppError):
    code = "SELF_DEPENDENCY"
    status_code = 422

    def __init__(self) -> None:
        super().__init__("A Task can't depend on itself.")


class CrossEventDependencyError(AppError):
    code = "CROSS_EVENT_DEPENDENCY"
    status_code = 422

    def __init__(self) -> None:
        super().__init__("Dependencies must stay inside one DR Event.")


class DependencyExistsError(AppError):
    code = "DEPENDENCY_EXISTS"
    status_code = 409

    def __init__(self) -> None:
        super().__init__("That dependency already exists. Remove it first to change its strength.")


class DependencyCycleError(AppError):
    code = "DEPENDENCY_CYCLE"
    status_code = 409

    def __init__(self, cycle_path: list[uuid.UUID]) -> None:
        super().__init__(
            f"This dependency would create a cycle through {len(cycle_path) - 1} Task(s); "
            "a Task would end up waiting on itself.",
            details={"cycle_path": [str(n) for n in cycle_path]},
        )


class TaskDependencyNotFoundError(AppError):
    code = "TASK_DEPENDENCY_NOT_FOUND"
    status_code = 404

    def __init__(self) -> None:
        super().__init__("Dependency not found.")


class MilestoneNotFoundError(AppError):
    code = "MILESTONE_NOT_FOUND"
    status_code = 404

    def __init__(self) -> None:
        super().__init__("Milestone not found.")


class MilestoneGateNotFoundError(AppError):
    code = "MILESTONE_DEPENDENCY_NOT_FOUND"
    status_code = 404

    def __init__(self) -> None:
        super().__init__("Milestone gate not found.")


async def _visible_task(session: AsyncSession, actor_id: uuid.UUID, task_id: uuid.UUID) -> Task:
    task = await get_visible_task(session, actor_id, task_id)
    if task is None:
        raise TaskNotFoundError()
    return task


async def _authorize(session: AsyncSession, actor_id: uuid.UUID, successor: Task, clock: Clock) -> None:
    if not await actor_may_change_dependency(session, actor_id, successor, at=clock.now()):
        raise AuthorizationRequiredError()


async def _task_changed(
    session: AsyncSession, successor: Task, clock: Clock, *, change: str, ref_key: str, ref_id: uuid.UUID
) -> None:
    """An edge change moves the successor's derived Ready, so its `TaskChanged` (D-234) fires --
    socket payloads are hints to refetch, never source-of-truth data (D-242)."""
    await write_outbox(
        session,
        aggregate_type="TASK",
        aggregate_id=successor.id,
        event_type="TaskChanged",
        payload={"change": change, ref_key: str(ref_id)},
        clock=clock,
        dr_event_id=successor.dr_event_id,
    )


class DependencyService:
    @staticmethod
    async def add_task_dependency(
        session: AsyncSession,
        *,
        actor_id: uuid.UUID,
        predecessor_task_id: uuid.UUID,
        successor_task_id: uuid.UUID,
        strength: str = "HARD",
        clock: Clock | None = None,
    ) -> TaskDependency:
        clock = clock or SystemClock()
        if predecessor_task_id == successor_task_id:
            raise SelfDependencyError()
        predecessor = await _visible_task(session, actor_id, predecessor_task_id)
        successor = await _visible_task(session, actor_id, successor_task_id)
        if predecessor.dr_event_id != successor.dr_event_id:
            raise CrossEventDependencyError()
        await _authorize(session, actor_id, successor, clock)

        await lock_event_dependency_graph(session, successor.dr_event_id)
        if await dependency_exists(
            session, predecessor_task_id=predecessor.id, successor_task_id=successor.id
        ):
            raise DependencyExistsError()
        cycle = await find_cycle_path(session, successor.dr_event_id, predecessor.id, successor.id)
        if cycle is not None:
            raise DependencyCycleError(cycle)

        edge = TaskDependency(
            dr_event_id=successor.dr_event_id,
            predecessor_task_id=predecessor.id,
            successor_task_id=successor.id,
            strength=strength,
            created_by_user_id=actor_id,
            created_at=clock.now(),
        )
        session.add(edge)
        await session.flush()
        await write_audit(
            session,
            actor_user_id=actor_id,
            entity_type="TASK_DEPENDENCY",
            entity_id=edge.id,
            action="TASK_DEPENDENCY_CREATED",
            dr_event_id=edge.dr_event_id,
            after={
                "predecessor_task_id": str(predecessor.id),
                "successor_task_id": str(successor.id),
                "strength": strength,
            },
        )
        await _task_changed(
            session, successor, clock, change="DEPENDENCY_ADDED", ref_key="task_dependency_id", ref_id=edge.id
        )
        return edge

    @staticmethod
    async def remove_task_dependency(
        session: AsyncSession,
        *,
        actor_id: uuid.UUID,
        dependency_id: uuid.UUID,
        clock: Clock | None = None,
    ) -> TaskDependency:
        """Soft delete. Removing an edge can never create a cycle, so there's no cycle check -- but it
        still takes the lock so a concurrent add's check never races a half-applied change."""
        clock = clock or SystemClock()
        edge = (
            await session.execute(
                select(TaskDependency)
                .where(TaskDependency.id == dependency_id, TaskDependency.deleted_at.is_(None))
                .with_for_update()
            )
        ).scalar_one_or_none()
        if edge is None or not await user_can_see_event(session, actor_id, edge.dr_event_id):
            raise TaskDependencyNotFoundError()
        successor = await session.get(Task, edge.successor_task_id)
        assert successor is not None  # FK
        await _authorize(session, actor_id, successor, clock)

        await lock_event_dependency_graph(session, edge.dr_event_id)
        now = clock.now()
        edge.deleted_at = now
        await session.flush()
        await write_audit(
            session,
            actor_user_id=actor_id,
            entity_type="TASK_DEPENDENCY",
            entity_id=edge.id,
            action="TASK_DEPENDENCY_DELETED",
            dr_event_id=edge.dr_event_id,
            before={"deleted_at": None},
            after={"deleted_at": now.isoformat()},
        )
        await _task_changed(
            session,
            successor,
            clock,
            change="DEPENDENCY_REMOVED",
            ref_key="task_dependency_id",
            ref_id=edge.id,
        )
        return edge

    @staticmethod
    async def add_milestone_gate(
        session: AsyncSession,
        *,
        actor_id: uuid.UUID,
        milestone_id: uuid.UUID,
        successor_task_id: uuid.UUID,
        strength: str = "HARD",
        clock: Clock | None = None,
    ) -> MilestoneDependency:
        """No cycle check needed: nothing points *into* a Milestone in this graph (see queries.py), so a
        gate can never close a cycle. No HTTP route yet -- gate management belongs with BUILD-07's
        Milestone endpoints (BUILD-06.plan.md Risk #13)."""
        clock = clock or SystemClock()
        successor = await _visible_task(session, actor_id, successor_task_id)
        milestone = await get_milestone(session, milestone_id)
        if milestone is None or not await user_can_see_event(session, actor_id, milestone.dr_event_id):
            raise MilestoneNotFoundError()
        if milestone.dr_event_id != successor.dr_event_id:
            raise CrossEventDependencyError()
        await _authorize(session, actor_id, successor, clock)

        await lock_event_dependency_graph(session, successor.dr_event_id)
        if await live_milestone_gate_exists(
            session, milestone_id=milestone.id, successor_task_id=successor.id
        ):
            raise DependencyExistsError()

        gate = MilestoneDependency(
            milestone_id=milestone.id,
            successor_task_id=successor.id,
            strength=strength,
            created_by_user_id=actor_id,
            created_at=clock.now(),
        )
        session.add(gate)
        await session.flush()
        # audit_events.entity_type's CHECK has no MILESTONE_DEPENDENCY label, so the gate is audited
        # against the Task it holds (schema_v2_reconciliation.sql:159-169).
        await write_audit(
            session,
            actor_user_id=actor_id,
            entity_type="TASK",
            entity_id=successor.id,
            action="MILESTONE_GATE_ADDED",
            dr_event_id=successor.dr_event_id,
            after={
                "milestone_dependency_id": str(gate.id),
                "milestone_id": str(milestone.id),
                "strength": strength,
            },
        )
        await _task_changed(
            session,
            successor,
            clock,
            change="MILESTONE_GATE_ADDED",
            ref_key="milestone_dependency_id",
            ref_id=gate.id,
        )
        return gate

    @staticmethod
    async def remove_milestone_gate(
        session: AsyncSession,
        *,
        actor_id: uuid.UUID,
        milestone_dependency_id: uuid.UUID,
        clock: Clock | None = None,
    ) -> MilestoneDependency:
        clock = clock or SystemClock()
        gate = (
            await session.execute(
                select(MilestoneDependency)
                .where(
                    MilestoneDependency.id == milestone_dependency_id,
                    MilestoneDependency.deleted_at.is_(None),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        successor = await get_visible_task(session, actor_id, gate.successor_task_id) if gate else None
        if gate is None or successor is None:
            raise MilestoneGateNotFoundError()
        await _authorize(session, actor_id, successor, clock)

        await lock_event_dependency_graph(session, successor.dr_event_id)
        gate.deleted_at = clock.now()
        await session.flush()
        await write_audit(
            session,
            actor_user_id=actor_id,
            entity_type="TASK",
            entity_id=successor.id,
            action="MILESTONE_GATE_REMOVED",
            dr_event_id=successor.dr_event_id,
            after={"milestone_dependency_id": str(gate.id), "milestone_id": str(gate.milestone_id)},
        )
        await _task_changed(
            session,
            successor,
            clock,
            change="MILESTONE_GATE_REMOVED",
            ref_key="milestone_dependency_id",
            ref_id=gate.id,
        )
        return gate

from __future__ import annotations

import uuid
from collections.abc import Mapping

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import write_audit
from app.core.clock import Clock, SystemClock
from app.core.errors import AppError, ConcurrencyConflictError
from app.core.outbox import write_outbox
from app.dr_events.errors import DrEventNotFoundError
from app.dr_events.participants import enrol_participant, user_can_see_event
from app.dr_events.queries import get_dr_application, get_event
from app.tasks_dependencies.models import MilestoneDependency, Task, TaskDependency
from app.tasks_dependencies.policies import (
    REQUIREMENT_FIELDS,
    actor_may_create_task,
    actor_may_edit_task_metadata,
)
from app.users_teams_org.authorization import AuthorizationRequiredError
from app.users_teams_org.commands import TeamNotFoundError
from app.users_teams_org.queries import get_team, list_active_team_member_ids
from app.work_streams.queries import get_work_stream


class TaskNotFoundError(AppError):
    """404 both for a Task that doesn't exist and for one in an Event the caller can't see --
    never 403 for the latter, or the error would confirm the Task exists (invariant #1)."""

    code = "TASK_NOT_FOUND"
    status_code = 404

    def __init__(self) -> None:
        super().__init__("Task not found.")


class CrossEventReferenceError(AppError):
    code = "CROSS_EVENT_REFERENCE"
    status_code = 422

    def __init__(self, field: str) -> None:
        super().__init__(f"{field} must refer to something in this DR Event.", details={"field": field})


class TaskContextRequiredError(AppError):
    code = "TASK_CONTEXT_REQUIRED"
    status_code = 422

    def __init__(self) -> None:
        super().__init__("A Task needs a DR Application and/or a Work Stream (task_context_ck).")


class TaskMetadataLockedError(AppError):
    code = "TASK_METADATA_LOCKED"
    status_code = 409

    def __init__(self, status: str, fields: list[str]) -> None:
        super().__init__(
            f"These fields can't be edited while the Task is {status}.",
            details={"status": status, "fields": fields},
        )


#: The only fields `PATCH /tasks/{id}` may change (API_CONTRACT.md:158 "non-state editable Task
#: metadata"); the request schema forbids everything else.
EDITABLE_FIELDS = (
    frozenset({"title", "description", "expected_duration_minutes", "sort_order"}) | REQUIREMENT_FIELDS
)
_TERMINAL = frozenset({"COMPLETED", "CANCELLED"})


async def create_draft_task(
    session: AsyncSession,
    *,
    dr_event_id: uuid.UUID,
    title: str,
    phase: str,
    owning_team_id: uuid.UUID,
    created_by_user_id: uuid.UUID,
    dr_application_id: uuid.UUID | None = None,
    work_stream_id: uuid.UUID | None = None,
    parent_task_id: uuid.UUID | None = None,
    description: str | None = None,
    current_assignee_user_id: uuid.UUID | None = None,
    expected_duration_minutes: int | None = None,
    evidence_required: bool = True,
    evidence_min_count: int = 1,
    verification_note_required: bool = True,
    needs_specific_validation: bool = False,
    sort_order: int | None = None,
    added_during_execution: bool = False,
    source_import_id: uuid.UUID | None = None,
    source_import_row: str | None = None,
    clock: Clock | None = None,
) -> Task:
    """The one place a Task row is born -- `create_task` (the API) and BUILD-05's Excel import both
    call it, after their own authorization. Status comes from the column default (NOT_STARTED); this
    never writes a lifecycle status.

    D-222 auto-enrolment happens here so no creation path can skip it: the Owning Team's active
    members and its manager as `OWNING_TEAM`, and the assignee (import sets one) as `TASK_ASSIGNEE`.
    A snapshot at creation -- later joiners wait for a membership command (BUILD-06.plan.md Risk #22)."""
    clock = clock or SystemClock()
    if dr_application_id is None and work_stream_id is None:
        raise TaskContextRequiredError()

    task = Task(
        dr_event_id=dr_event_id,
        dr_application_id=dr_application_id,
        work_stream_id=work_stream_id,
        parent_task_id=parent_task_id,
        title=title,
        description=description,
        phase=phase,
        owning_team_id=owning_team_id,
        current_assignee_user_id=current_assignee_user_id,
        expected_duration_minutes=expected_duration_minutes,
        evidence_required=evidence_required,
        evidence_min_count=evidence_min_count,
        verification_note_required=verification_note_required,
        needs_specific_validation=needs_specific_validation,
        sort_order=sort_order,
        added_during_execution=added_during_execution,
        source_import_id=source_import_id,
        source_import_row=source_import_row,
        created_by_user_id=created_by_user_id,
    )
    session.add(task)
    await session.flush()
    await write_audit(
        session,
        actor_user_id=created_by_user_id,
        entity_type="TASK",
        entity_id=task.id,
        action="TASK_CREATED",
        dr_event_id=dr_event_id,
        after={
            "title": title,
            "phase": phase,
            "source_import_id": str(source_import_id) if source_import_id else None,
            "source_import_row": source_import_row,
        },
    )

    team = await get_team(session, owning_team_id)
    team_people = set(await list_active_team_member_ids(session, owning_team_id, at=clock.now()))
    if team is not None and team.manager_user_id is not None:
        team_people.add(team.manager_user_id)
    for user_id in sorted(team_people, key=str):
        await enrol_participant(
            session, dr_event_id, user_id, "OWNING_TEAM", added_by_user_id=created_by_user_id
        )
    if current_assignee_user_id is not None:
        await enrol_participant(
            session,
            dr_event_id,
            current_assignee_user_id,
            "TASK_ASSIGNEE",
            added_by_user_id=created_by_user_id,
        )
    return task


async def dependency_exists(
    session: AsyncSession, *, predecessor_task_id: uuid.UUID, successor_task_id: uuid.UUID
) -> bool:
    """Used by `plans_import/commands.py::process_accepted_import` to reject an import row that
    would create a direct 2-node cycle (row A's predecessor is B, row B's predecessor is A) by
    checking whether the reverse edge already exists -- full N-node DAG cycle detection across the
    whole graph is BUILD-06's scope (plan Risk #2)."""
    existing = await session.execute(
        select(TaskDependency.id).where(
            TaskDependency.predecessor_task_id == predecessor_task_id,
            TaskDependency.successor_task_id == successor_task_id,
            TaskDependency.deleted_at.is_(None),
        )
    )
    return existing.first() is not None


async def create_draft_dependency(
    session: AsyncSession,
    *,
    dr_event_id: uuid.UUID,
    predecessor_task_id: uuid.UUID,
    successor_task_id: uuid.UUID,
    created_by_user_id: uuid.UUID,
    strength: str = "HARD",
) -> TaskDependency | None:
    """Insert-only helper for BUILD-05's Excel import: rejects self edges and exact duplicates as a
    no-op (`None`, not an error), so one malformed row doesn't abort the whole accept (D-221: "manual
    edit/merge/split/reclassify always possible"). It does **not** check for cycles -- its caller must
    hold `lock_event_dependency_graph` and run `queries.find_cycle_path` first, as
    `plans_import.commands.process_accepted_import` does. API writes go through
    `dependency_service.DependencyService` instead."""
    if predecessor_task_id == successor_task_id:
        return None

    existing = await session.execute(
        select(TaskDependency.id).where(
            TaskDependency.predecessor_task_id == predecessor_task_id,
            TaskDependency.successor_task_id == successor_task_id,
            TaskDependency.dependency_type == "FINISH_TO_START",
            TaskDependency.deleted_at.is_(None),
        )
    )
    if existing.first() is not None:
        return None

    dependency = TaskDependency(
        dr_event_id=dr_event_id,
        predecessor_task_id=predecessor_task_id,
        successor_task_id=successor_task_id,
        strength=strength,
        created_by_user_id=created_by_user_id,
    )
    session.add(dependency)
    await session.flush()
    await write_audit(
        session,
        actor_user_id=created_by_user_id,
        entity_type="TASK_DEPENDENCY",
        entity_id=dependency.id,
        action="TASK_DEPENDENCY_CREATED",
        dr_event_id=dr_event_id,
        after={
            "predecessor_task_id": str(predecessor_task_id),
            "successor_task_id": str(successor_task_id),
            "strength": strength,
        },
    )
    return dependency


async def lock_event_dependency_graph(session: AsyncSession, dr_event_id: uuid.UUID) -> None:
    """Serializes every dependency write in one Event until the transaction ends (ADR-036's advisory-lock
    pattern). Without it, two concurrent inserts A->B and B->A would each pass the cycle check against a
    graph that doesn't yet contain the other, and both would commit a cycle."""
    await session.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended(f"dependency_graph:{dr_event_id}", 0)))
    )


async def create_task(
    session: AsyncSession,
    *,
    actor_id: uuid.UUID,
    dr_event_id: uuid.UUID,
    title: str,
    phase: str,
    owning_team_id: uuid.UUID,
    dr_application_id: uuid.UUID | None = None,
    work_stream_id: uuid.UUID | None = None,
    parent_task_id: uuid.UUID | None = None,
    description: str | None = None,
    expected_duration_minutes: int | None = None,
    sort_order: int | None = None,
    evidence_required: bool = True,
    evidence_min_count: int = 1,
    verification_note_required: bool = True,
    needs_specific_validation: bool = False,
    clock: Clock | None = None,
) -> Task:
    """`POST /dr-events/{event_id}/tasks` (API_CONTRACT.md:157). No initial assignee: assignment has its
    own rules and endpoint (BUILD-07, Risk #21). Order: Event visible (404) -> context (422) -> every
    reference in this Event (422) -> authority over the new Task's scopes (403) -> create."""
    clock = clock or SystemClock()
    event = await get_event(session, dr_event_id)
    if event is None or not await user_can_see_event(session, actor_id, dr_event_id):
        raise DrEventNotFoundError()
    if dr_application_id is None and work_stream_id is None:
        raise TaskContextRequiredError()
    if await get_team(session, owning_team_id) is None:
        raise TeamNotFoundError()
    if work_stream_id is not None:
        ws = await get_work_stream(session, work_stream_id)
        if ws is None or ws.dr_event_id != dr_event_id:
            raise CrossEventReferenceError("work_stream_id")
    if dr_application_id is not None:
        dr_app = await get_dr_application(session, dr_application_id)
        if dr_app is None or dr_app.dr_event_id != dr_event_id or dr_app.deleted_at is not None:
            raise CrossEventReferenceError("dr_application_id")
    if parent_task_id is not None:
        parent = await session.get(Task, parent_task_id)
        if parent is None or parent.dr_event_id != dr_event_id or parent.deleted_at is not None:
            raise CrossEventReferenceError("parent_task_id")
    if not await actor_may_create_task(
        session,
        actor_id,
        owning_team_id=owning_team_id,
        work_stream_id=work_stream_id,
        dr_application_id=dr_application_id,
    ):
        raise AuthorizationRequiredError()

    task = await create_draft_task(
        session,
        dr_event_id=dr_event_id,
        title=title.strip(),
        phase=phase,
        owning_team_id=owning_team_id,
        created_by_user_id=actor_id,
        dr_application_id=dr_application_id,
        work_stream_id=work_stream_id,
        parent_task_id=parent_task_id,
        description=description,
        expected_duration_minutes=expected_duration_minutes,
        sort_order=sort_order,
        evidence_required=evidence_required,
        evidence_min_count=evidence_min_count,
        verification_note_required=verification_note_required,
        needs_specific_validation=needs_specific_validation,
        added_during_execution=event.status != "PLANNED",
        clock=clock,
    )
    await write_outbox(
        session,
        aggregate_type="TASK",
        aggregate_id=task.id,
        event_type="TaskChanged",
        payload={"change": "TASK_CREATED", "status": task.status},
        clock=clock,
        dr_event_id=dr_event_id,
    )
    return task


async def update_task_metadata(
    session: AsyncSession,
    *,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
    expected_version: int,
    changes: Mapping[str, object],
    clock: Clock | None = None,
) -> Task:
    """`PATCH /tasks/{id}`. Never a lifecycle change -- status, Owning Team, assignee, context, phase and
    parent have no path here. Order as in the transition service: visible (404) -> authorized for these
    fields (403) -> version (409) -> editable in this state (409) -> write + audit + outbox. A COMPLETED or
    CANCELLED Task is closed; a READY_FOR_VALIDATION one keeps the requirements it was submitted under.
    Values equal to the current ones are dropped; nothing left means no write and no version bump."""
    clock = clock or SystemClock()
    unknown = set(changes) - EDITABLE_FIELDS
    if unknown:
        raise ValueError(f"not editable Task metadata: {sorted(unknown)}")
    task = (
        await session.execute(
            select(Task)
            .where(Task.id == task_id, Task.deleted_at.is_(None))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if task is None or not await user_can_see_event(session, actor_id, task.dr_event_id):
        raise TaskNotFoundError()
    fields = frozenset(changes)
    if not await actor_may_edit_task_metadata(session, actor_id, task, fields, at=clock.now()):
        raise AuthorizationRequiredError()
    if task.version != expected_version:
        raise ConcurrencyConflictError()
    if task.status in _TERMINAL:
        raise TaskMetadataLockedError(task.status, sorted(fields))
    if task.status == "READY_FOR_VALIDATION" and fields & REQUIREMENT_FIELDS:
        raise TaskMetadataLockedError(task.status, sorted(fields & REQUIREMENT_FIELDS))

    changed = {name: value for name, value in changes.items() if getattr(task, name) != value}
    if not changed:
        return task
    before: dict[str, object] = {name: getattr(task, name) for name in changed}
    before["version"] = task.version
    for name, value in changed.items():
        setattr(task, name, value)
    task.version += 1
    task.updated_at = clock.now()
    await session.flush()

    after: dict[str, object] = {**changed, "version": task.version}
    await write_audit(
        session,
        actor_user_id=actor_id,
        entity_type="TASK",
        entity_id=task.id,
        action="TASK_UPDATED",
        dr_event_id=task.dr_event_id,
        before=before,
        after=after,
    )
    await write_outbox(
        session,
        aggregate_type="TASK",
        aggregate_id=task.id,
        event_type="TaskChanged",  # D-234 V1 union (API_CONTRACT.md:274)
        payload={"change": "TASK_UPDATED", **after},
        clock=clock,
        dr_event_id=task.dr_event_id,
    )
    return task


async def add_milestone_gate(
    session: AsyncSession,
    *,
    actor_id: uuid.UUID,
    milestone_id: uuid.UUID,
    successor_task_id: uuid.UUID,
    strength: str,
    clock: Clock,
) -> MilestoneDependency:
    """Application-layer entry to `DependencyService.add_milestone_gate` for other modules
    (`milestones.commands`): same guards, graph lock and cycle check as every other edge. Imported
    inside the function because `dependency_service` imports this module."""
    from app.tasks_dependencies.dependency_service import DependencyService

    return await DependencyService.add_milestone_gate(
        session,
        actor_id=actor_id,
        milestone_id=milestone_id,
        successor_task_id=successor_task_id,
        strength=strength,
        clock=clock,
    )


async def live_task_ids_in_event(
    session: AsyncSession, dr_event_id: uuid.UUID, task_ids: list[uuid.UUID]
) -> set[uuid.UUID]:
    """The subset of `task_ids` that are live Tasks of this Event."""
    if not task_ids:
        return set()
    rows = await session.execute(
        select(Task.id).where(
            Task.id.in_(task_ids), Task.dr_event_id == dr_event_id, Task.deleted_at.is_(None)
        )
    )
    return {task_id for (task_id,) in rows}


async def apply_assignment(
    session: AsyncSession,
    *,
    task: Task,
    assignee_user_id: uuid.UUID,
    actor_id: uuid.UUID,
    action: str,
    metadata: Mapping[str, object],
    affected_user_ids: list[uuid.UUID],
    clock: Clock,
) -> Task:
    """The one writer of `current_assignee_user_id` (API_CONTRACT.md:165-166). The caller
    (`resources_skills.AssignmentService`) has locked `task`, authorized, checked version and state.
    Owning Team never changes (DATA_MODEL.md invariant 8). The new assignee joins the Event
    (D-222 `TASK_ASSIGNEE`); `AssignmentChanged` names who is affected, for BUILD-08's fan-out."""
    previous = task.current_assignee_user_id
    before: dict[str, object] = {
        "current_assignee_user_id": str(previous) if previous else None,
        "version": task.version,
    }
    task.current_assignee_user_id = assignee_user_id
    task.version += 1
    task.updated_at = clock.now()
    await session.flush()
    after: dict[str, object] = {"current_assignee_user_id": str(assignee_user_id), "version": task.version}
    await write_audit(
        session,
        actor_user_id=actor_id,
        entity_type="TASK",
        entity_id=task.id,
        action=action,
        dr_event_id=task.dr_event_id,
        before=before,
        after=after,
        metadata=dict(metadata),
    )
    await enrol_participant(
        session, task.dr_event_id, assignee_user_id, "TASK_ASSIGNEE", added_by_user_id=actor_id
    )
    await write_outbox(
        session,
        aggregate_type="TASK",
        aggregate_id=task.id,
        event_type="TaskChanged",  # D-234 V1 union
        payload={"change": action, **after},
        clock=clock,
        dr_event_id=task.dr_event_id,
    )
    await write_outbox(
        session,
        aggregate_type="TASK",
        aggregate_id=task.id,
        event_type="AssignmentChanged",  # D-234 V1 union
        payload={
            "change": action,
            "task_id": str(task.id),
            "previous_assignee_user_id": before["current_assignee_user_id"],
            **after,
            **{k: v for k, v in metadata.items() if k == "conflict_resolution"},
            "affected_user_ids": [str(u) for u in dict.fromkeys(affected_user_ids)],
        },
        clock=clock,
        dr_event_id=task.dr_event_id,
    )
    return task

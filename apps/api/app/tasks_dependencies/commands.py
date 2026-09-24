from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import write_audit
from app.core.clock import Clock, SystemClock
from app.core.errors import AppError
from app.core.outbox import write_outbox
from app.dr_events.errors import DrEventNotFoundError
from app.dr_events.participants import enrol_participant, user_can_see_event
from app.dr_events.queries import get_dr_application, get_event
from app.tasks_dependencies.models import Task, TaskDependency
from app.tasks_dependencies.policies import actor_may_create_task
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
    """Minimal no-self-edge/no-exact-duplicate guard only -- full DAG cycle detection across the
    whole graph is BUILD-06's scope (plan Risk #2). Returns `None` (no-op, not an error) for a
    self-edge or an exact duplicate of an existing edge, since a malformed import row shouldn't
    abort the whole accept; the row is left for a human to reconcile post-import (D-221: "manual
    edit/merge/split/reclassify always possible")."""
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

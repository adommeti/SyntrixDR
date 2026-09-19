from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import write_audit
from app.core.errors import AppError
from app.tasks_dependencies.models import Task, TaskDependency


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
    source_import_id: uuid.UUID | None = None,
    source_import_row: str | None = None,
) -> Task:
    """Internal-only helper for BUILD-05's Excel import accept flow -- not a route/command in its
    own right. Never sets `status=` explicitly (relies on the `NOT_STARTED` column default), so
    `guard-forbidden-stack.sh` never triggers. Full Task authoring (its own command/route/guards)
    is BUILD-06's scope (BUILD-05.plan.md Risk #2)."""
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
    return task


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

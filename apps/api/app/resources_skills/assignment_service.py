"""`assign` and `volunteer` (API_CONTRACT.md:165-166), including D-214 Manager precedence.

Order (the transition-service shape): lock the Task and check visibility (404) -> assignee exists (404)
-> authority for this target (403) -> version (409, or D-214 precedence) -> state (409) -> write via
`tasks_dependencies.commands.apply_assignment` (version, audit, outbox, D-222 enrolment)."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock, SystemClock
from app.core.errors import AppError, ConcurrencyConflictError
from app.dr_events.commands import record_override
from app.resources_skills.policies import (
    MANAGER,
    PRECEDENCE_YIELDS_TO,
    VOLUNTEER,
    assignment_authority,
    is_owning_team_manager,
    may_volunteer,
)
from app.tasks_dependencies.commands import TaskNotFoundError, apply_assignment
from app.tasks_dependencies.models import Task
from app.tasks_dependencies.queries import VersionedChange, lock_visible_task, task_changes_after
from app.users_teams_org.authorization import AuthorizationRequiredError
from app.users_teams_org.commands import UserNotFoundError
from app.users_teams_org.queries import is_active_team_member, is_user_active

MANAGER_PRECEDENCE = "MANAGER_PRECEDENCE"
_ASSIGNABLE = frozenset({"NOT_STARTED", "IN_PROGRESS", "BLOCKED", "READY_FOR_VALIDATION"})


class TaskNotAssignableError(AppError):
    code = "INVALID_TRANSITION"
    status_code = 409

    def __init__(self, command: str, status: str) -> None:
        super().__init__(
            f"Cannot {command} a Task that is {status}.", details={"command": command, "status": status}
        )


class TaskAlreadyAssignedError(AppError):
    code = "TASK_ALREADY_ASSIGNED"
    status_code = 409

    def __init__(self) -> None:
        super().__init__("Only unassigned work can be volunteered for; ask its Team to reassign it.")


def _yields_to_manager(changes: list[VersionedChange], expected_version: int, current_version: int) -> bool:
    """D-214's condition on what came between: every version bump since `expected_version` is an
    assignment made under Admin/Coordinator authority. A gap (a change with no versioned audit) or
    any other change -- a start, another Manager's or a Lead's assignment -- is a plain conflict."""
    return (
        len(changes) == current_version - expected_version
        and [c.version for c in changes] == list(range(expected_version + 1, current_version + 1))
        and all(
            c.action == "TASK_ASSIGNED" and c.metadata.get("authority") in PRECEDENCE_YIELDS_TO
            for c in changes
        )
    )


async def _load(session: AsyncSession, actor_id: uuid.UUID, task_id: uuid.UUID) -> Task:
    task = await lock_visible_task(session, actor_id, task_id)
    if task is None:
        raise TaskNotFoundError()
    return task


class AssignmentService:
    @staticmethod
    async def assign(
        session: AsyncSession,
        *,
        actor_id: uuid.UUID,
        task_id: uuid.UUID,
        assignee_user_id: uuid.UUID,
        expected_version: int,
        clock: Clock | None = None,
    ) -> Task:
        clock = clock or SystemClock()
        task = await _load(session, actor_id, task_id)
        if not await is_user_active(session, assignee_user_id):
            raise UserNotFoundError()
        same_team = await is_active_team_member(
            session, task.owning_team_id, assignee_user_id, at=clock.now()
        )
        authority = await assignment_authority(session, actor_id, task, same_team=same_team, at=clock.now())
        if authority is None:
            raise AuthorizationRequiredError()

        precedence: list[VersionedChange] | None = None
        if task.version != expected_version:
            # D-214: only the Owning Team's Manager, only onto their own Team, only over Coordinator/
            # Admin assignments. Everything else stale is 409 (DATA_MODEL.md invariant 22).
            changes = (
                await task_changes_after(session, task.id, expected_version)
                if expected_version < task.version
                else []
            )
            if not (
                same_team
                and expected_version < task.version
                and await is_owning_team_manager(session, actor_id, task)
                and _yields_to_manager(changes, expected_version, task.version)
            ):
                raise ConcurrencyConflictError()
            precedence = changes
        if task.status not in _ASSIGNABLE:
            raise TaskNotAssignableError("assign", task.status)
        if task.current_assignee_user_id == assignee_user_id:
            return task

        previous = task.current_assignee_user_id
        affected = [u for u in (previous, assignee_user_id) if u is not None]
        metadata: dict[str, object] = {"authority": MANAGER if precedence is not None else authority}
        if precedence is not None:
            superseded_by = list(dict.fromkeys(c.actor_user_id for c in precedence if c.actor_user_id))
            metadata["conflict_resolution"] = MANAGER_PRECEDENCE
            metadata["superseded_version"] = task.version
            affected = [*superseded_by, *affected]
            await record_override(
                session,
                dr_event_id=task.dr_event_id,
                target_type="TASK",
                target_id=task.id,
                override_type=MANAGER_PRECEDENCE,
                reason="Team Manager assignment over an intervening Coordinator/Admin assignment (D-214)",
                performed_by_user_id=actor_id,
                metadata={
                    "expected_version": expected_version,
                    "superseded_version": task.version,
                    "superseded_assignee_user_id": str(previous) if previous else None,
                    "superseded_by_user_ids": [str(u) for u in superseded_by],
                    "assignee_user_id": str(assignee_user_id),
                },
            )
        return await apply_assignment(
            session,
            task=task,
            assignee_user_id=assignee_user_id,
            actor_id=actor_id,
            action="TASK_ASSIGNED",
            metadata=metadata,
            affected_user_ids=affected,
            clock=clock,
        )

    @staticmethod
    async def volunteer(
        session: AsyncSession,
        *,
        actor_id: uuid.UUID,
        task_id: uuid.UUID,
        expected_version: int,
        clock: Clock | None = None,
    ) -> Task:
        """The actor claims unassigned work for themselves; the Owning Team is unchanged (§4.12)."""
        clock = clock or SystemClock()
        task = await _load(session, actor_id, task_id)
        if not await may_volunteer(session, actor_id):
            raise AuthorizationRequiredError()
        if task.version != expected_version:
            raise ConcurrencyConflictError()
        if task.status not in _ASSIGNABLE:
            raise TaskNotAssignableError("volunteer for", task.status)
        if task.current_assignee_user_id is not None:
            raise TaskAlreadyAssignedError()
        return await apply_assignment(
            session,
            task=task,
            assignee_user_id=actor_id,
            actor_id=actor_id,
            action="TASK_VOLUNTEERED",
            metadata={"authority": VOLUNTEER},
            affected_user_ids=[actor_id],
            clock=clock,
        )

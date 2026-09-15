from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import write_audit
from app.core.clock import Clock, SystemClock
from app.core.errors import AppError
from app.plans_import.models import (
    Plan,
    PlanVersion,
    PlanVersionMilestone,
    PlanVersionTask,
    PlanVersionTaskDependency,
)
from app.plans_import.queries import next_plan_version_number
from app.users_teams_org.authorization import AuthorizationService, Capability

#: `version_type` values a caller may request directly; BASELINE is written only by
#: `dr_events/transition_service.py::start_failover` via `capture_baseline_snapshot`.
_CALLER_ALLOWED_VERSION_TYPES = frozenset({"DRAFT", "EXECUTION", "FINAL"})


class PlanNotFoundError(AppError):
    code = "PLAN_NOT_FOUND"
    status_code = 404

    def __init__(self) -> None:
        super().__init__("Plan not found.")


class PlanVersionNotFoundError(AppError):
    code = "PLAN_VERSION_NOT_FOUND"
    status_code = 404

    def __init__(self) -> None:
        super().__init__("Plan version not found.")


class PlanVersionBaselineNotAllowedError(AppError):
    code = "PLAN_VERSION_BASELINE_NOT_ALLOWED"
    status_code = 422

    def __init__(self) -> None:
        super().__init__("version_type=BASELINE is system-managed and cannot be created directly.")


@dataclass(frozen=True)
class SnapshotItem:
    source_id: uuid.UUID | None
    snapshot_data: dict[str, Any]


async def create_plan(
    session: AsyncSession,
    *,
    actor_id: uuid.UUID,
    name: str,
    plan_type: str,
    description: str | None = None,
    application_id: uuid.UUID | None = None,
) -> Plan:
    await AuthorizationService.require(session, actor_id, Capability.MANAGE_PLANS)

    plan = Plan(
        name=name,
        plan_type=plan_type,
        description=description,
        application_id=application_id,
        created_by_user_id=actor_id,
    )
    session.add(plan)
    await session.flush()

    await write_audit(
        session,
        actor_user_id=actor_id,
        entity_type="PLAN",
        entity_id=plan.id,
        action="PLAN_CREATED",
        after={"name": name, "plan_type": plan_type},
    )
    return plan


async def create_plan_version(
    session: AsyncSession,
    *,
    actor_id: uuid.UUID,
    plan_id: uuid.UUID,
    version_type: str,
    notes: str | None = None,
    tasks: list[SnapshotItem] | None = None,
    task_dependencies: list[SnapshotItem] | None = None,
    milestones: list[SnapshotItem] | None = None,
) -> PlanVersion:
    await AuthorizationService.require(session, actor_id, Capability.MANAGE_PLANS)

    if version_type not in _CALLER_ALLOWED_VERSION_TYPES:
        raise PlanVersionBaselineNotAllowedError()

    plan = await session.get(Plan, plan_id)
    if plan is None:
        raise PlanNotFoundError()

    next_number = await next_plan_version_number(session, plan_id)
    version = PlanVersion(
        plan_id=plan_id,
        version_number=next_number,
        version_type=version_type,
        notes=notes,
        created_by_user_id=actor_id,
    )
    session.add(version)
    await session.flush()

    for item in tasks or []:
        session.add(
            PlanVersionTask(
                plan_version_id=version.id, source_task_id=item.source_id, snapshot_data=item.snapshot_data
            )
        )
    for item in task_dependencies or []:
        session.add(
            PlanVersionTaskDependency(
                plan_version_id=version.id,
                source_dependency_id=item.source_id,
                snapshot_data=item.snapshot_data,
            )
        )
    for item in milestones or []:
        session.add(
            PlanVersionMilestone(
                plan_version_id=version.id,
                source_milestone_id=item.source_id,
                snapshot_data=item.snapshot_data,
            )
        )
    await session.flush()

    await write_audit(
        session,
        actor_user_id=actor_id,
        entity_type="PLAN_VERSION",
        entity_id=version.id,
        action="PLAN_VERSION_CREATED",
        after={"plan_id": str(plan_id), "version_type": version_type, "version_number": next_number},
    )
    return version


async def _copy_snapshot_rows(
    session: AsyncSession, *, source_plan_version_id: uuid.UUID, target_plan_version_id: uuid.UUID
) -> None:
    """Copies every snapshot row from one `plan_version_id` to another. Never mutates the
    source rows (FROZEN_DECISIONS.md:86 — live/derived edits never mutate the source Plan)."""
    tasks = (
        await session.execute(
            select(PlanVersionTask).where(PlanVersionTask.plan_version_id == source_plan_version_id)
        )
    ).scalars()
    for task in tasks:
        session.add(
            PlanVersionTask(
                plan_version_id=target_plan_version_id,
                source_task_id=task.source_task_id,
                snapshot_data=task.snapshot_data,
            )
        )

    dependencies = (
        await session.execute(
            select(PlanVersionTaskDependency).where(
                PlanVersionTaskDependency.plan_version_id == source_plan_version_id
            )
        )
    ).scalars()
    for dependency in dependencies:
        session.add(
            PlanVersionTaskDependency(
                plan_version_id=target_plan_version_id,
                source_dependency_id=dependency.source_dependency_id,
                snapshot_data=dependency.snapshot_data,
            )
        )

    milestones = (
        await session.execute(
            select(PlanVersionMilestone).where(PlanVersionMilestone.plan_version_id == source_plan_version_id)
        )
    ).scalars()
    for milestone in milestones:
        session.add(
            PlanVersionMilestone(
                plan_version_id=target_plan_version_id,
                source_milestone_id=milestone.source_milestone_id,
                snapshot_data=milestone.snapshot_data,
            )
        )

    await session.flush()


async def instantiate_plan_into_event(
    session: AsyncSession,
    *,
    actor_id: uuid.UUID,
    plan_id: uuid.UUID,
    plan_version_id: uuid.UUID,
    dr_event_id: uuid.UUID,
    clock: Clock | None = None,
) -> PlanVersion:
    """Creates the Event's EXECUTION `plan_versions` row by copying `plan_version_id`'s snapshot
    rows (BUILD-04.plan.md Risk #1: no named REST endpoint for this — invoked from
    `dr_events/commands.py::create_event`, not exposed as its own route)."""
    _ = clock or SystemClock()
    await AuthorizationService.require(session, actor_id, Capability.MANAGE_PLANS)

    source_version = await session.get(PlanVersion, plan_version_id)
    if source_version is None or source_version.plan_id != plan_id:
        raise PlanVersionNotFoundError()

    new_version = PlanVersion(
        plan_id=None,
        dr_event_id=dr_event_id,
        version_number=1,
        version_type="EXECUTION",
        created_by_user_id=actor_id,
    )
    session.add(new_version)
    await session.flush()
    await _copy_snapshot_rows(
        session, source_plan_version_id=source_version.id, target_plan_version_id=new_version.id
    )

    await write_audit(
        session,
        actor_user_id=actor_id,
        entity_type="PLAN_VERSION",
        entity_id=new_version.id,
        action="PLAN_VERSION_INSTANTIATED",
        after={"dr_event_id": str(dr_event_id), "source_plan_version_id": str(plan_version_id)},
    )
    return new_version


async def capture_baseline_snapshot(
    session: AsyncSession, *, actor_id: uuid.UUID, dr_event_id: uuid.UUID, clock: Clock | None = None
) -> PlanVersion | None:
    """Copies the Event's current EXECUTION `plan_versions` snapshot rows into a new immutable
    BASELINE version at `start-failover` (DATA_MODEL.md:105). Returns `None` if the Event has no
    EXECUTION version yet — a Plan is optional at Event creation, so Baseline capture is a no-op,
    not an error, in that case. Called internally by the transition service after it has already
    verified `EVENT_LIFECYCLE_COMMAND`; no separate authorization check here (system-triggered
    side effect of an already-authorized transition, mirrors `grant_role` calling
    `enrol_participant` without a redundant re-check)."""
    _ = clock or SystemClock()
    execution_result = await session.execute(
        select(PlanVersion)
        .where(PlanVersion.dr_event_id == dr_event_id, PlanVersion.version_type == "EXECUTION")
        .order_by(PlanVersion.created_at.desc())
        .limit(1)
    )
    execution_version = execution_result.scalar_one_or_none()
    if execution_version is None:
        return None

    baseline_version = PlanVersion(
        plan_id=None,
        dr_event_id=dr_event_id,
        version_number=execution_version.version_number,
        version_type="BASELINE",
        created_by_user_id=actor_id,
    )
    session.add(baseline_version)
    await session.flush()
    await _copy_snapshot_rows(
        session, source_plan_version_id=execution_version.id, target_plan_version_id=baseline_version.id
    )

    await write_audit(
        session,
        actor_user_id=actor_id,
        entity_type="PLAN_VERSION",
        entity_id=baseline_version.id,
        action="PLAN_VERSION_BASELINE_CAPTURED",
        after={
            "dr_event_id": str(dr_event_id),
            "source_execution_plan_version_id": str(execution_version.id),
        },
    )
    return baseline_version

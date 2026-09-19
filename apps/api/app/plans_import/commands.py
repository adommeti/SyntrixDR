from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.applications_catalog.queries import get_application_by_name
from app.core.audit import write_audit
from app.core.clock import Clock, SystemClock
from app.core.errors import AppError
from app.core.file_policy import get_file_policy
from app.core.storage import ObjectStore
from app.dr_events.models import DrApplication, DrEvent
from app.dr_events.participants import user_can_see_event
from app.dr_events.queries import list_dr_applications
from app.plans_import.models import (
    ImportJob,
    NeedsReviewItem,
    Plan,
    PlanVersion,
    PlanVersionMilestone,
    PlanVersionTask,
    PlanVersionTaskDependency,
)
from app.plans_import.queries import next_plan_version_number
from app.tasks_dependencies.commands import (
    TaskContextRequiredError,
    create_draft_dependency,
    create_draft_task,
    dependency_exists,
)
from app.users_teams_org.authorization import AuthorizationService, Capability
from app.users_teams_org.queries import find_active_user_by_display_name, get_team_by_name
from app.work_streams.commands import get_or_create_work_stream

#: `version_type` values a caller may request directly; BASELINE is written only by
#: `dr_events/transition_service.py::start_failover` via `capture_baseline_snapshot`.
_CALLER_ALLOWED_VERSION_TYPES = frozenset({"DRAFT", "EXECUTION", "FINAL"})


class PlanNotFoundError(AppError):
    code = "PLAN_NOT_FOUND"
    status_code = 404

    def __init__(self) -> None:
        super().__init__("Plan not found.")


class DrEventNotFoundError(AppError):
    """Local copy of `dr_events/commands.py::DrEventNotFoundError` (same code/message) --
    `dr_events/commands.py` already imports this module for `instantiate_plan_into_event`, so
    importing the other way would be circular. Same shape, deliberately duplicated, not shared."""

    code = "DR_EVENT_NOT_FOUND"
    status_code = 404

    def __init__(self) -> None:
        super().__init__("DR Event not found.")


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

    # Locks the Plan row for the rest of this transaction so two concurrent requests (different
    # Idempotency-Keys) can't both read the same MAX(version_number) and race to insert the same
    # next number -- the second blocks here until the first commits, then sees the new max
    # (found in review; `ux_plan_versions_plan_version` would otherwise surface as an unhandled
    # integrity error instead of a clean outcome).
    plan = await session.get(Plan, plan_id, with_for_update=True)
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


class UnsupportedFileTypeError(AppError):
    code = "UNSUPPORTED_FILE_TYPE"
    status_code = 422

    def __init__(self, extension: str) -> None:
        super().__init__(f"File type {extension!r} is not on the allowed-extensions list (D-225).")


class FileTooLargeError(AppError):
    code = "FILE_TOO_LARGE"
    status_code = 422

    def __init__(self, max_bytes: int) -> None:
        super().__init__(f"File exceeds the {max_bytes} byte upload limit (D-225).")


async def create_import_job(
    session: AsyncSession,
    *,
    actor_id: uuid.UUID,
    dr_event_id: uuid.UUID,
    filename: str,
    content: bytes,
    object_store: ObjectStore,
    clock: Clock | None = None,
) -> ImportJob:
    """`POST /dr-events/{event_id}/imports/excel` (D-221). Validates against the admin-configurable
    `file_policy` singleton (D-225) before ever touching storage -- extension allowlist and size
    cap; malware scanning (D-245) is BUILD-09's own stated scope, not faked here (BUILD-05.plan.md
    Risk #3). Enqueuing the actual Celery parse job is the caller's job (`routes.py`), so this
    function stays synchronous/testable without a broker."""
    clock = clock or SystemClock()
    await AuthorizationService.require(session, actor_id, Capability.MANAGE_IMPORTS)

    event = await session.get(DrEvent, dr_event_id)
    if event is None or not await user_can_see_event(session, actor_id, dr_event_id):
        raise DrEventNotFoundError()

    policy = await get_file_policy(session)
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if extension not in policy.allowed_extensions:
        raise UnsupportedFileTypeError(extension)
    if len(content) > policy.max_bytes:
        raise FileTooLargeError(policy.max_bytes)

    blob_name = f"{dr_event_id}/{uuid.uuid4()}-{filename}"
    source_file_uri = await object_store.upload(
        "imports",
        blob_name,
        content,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    import_job = ImportJob(
        dr_event_id=dr_event_id, source_file_uri=source_file_uri, created_by_user_id=actor_id
    )
    session.add(import_job)
    await session.flush()
    await write_audit(
        session,
        actor_user_id=actor_id,
        entity_type="IMPORT_JOB",
        entity_id=import_job.id,
        action="IMPORT_JOB_CREATED",
        dr_event_id=dr_event_id,
        after={"source_file_uri": source_file_uri, "filename": filename},
    )
    return import_job


#: D-221 target fields this session actually resolves to a real column/lookup. `tier`/
#: `failover_failback` (beyond phase-inference) and `evidence_required`'s exact text values are
#: read but not deeply validated -- reviewed at the mapping step, not re-litigated here.
_PHASE_SYNONYMS: dict[str, str] = {
    "pre-dr": "PRE_DR",
    "pre dr": "PRE_DR",
    "predr": "PRE_DR",
    "failover": "FAILOVER",
    "validation": "VALIDATION",
    "failback": "FAILBACK",
    "post-dr": "POST_DR",
    "post dr": "POST_DR",
    "postdr": "POST_DR",
}


def _resolve_phase(row_values: dict[str, Any]) -> str:
    """Falls back to `FAILOVER` (the common case for every D-221 fixture example) when neither
    the `phase` nor `failover_failback` column resolves -- documented in BUILD-05.plan.md, not a
    silent guess: callers can always see/edit the created Task's phase after import (D-221)."""
    phase_value = row_values.get("phase")
    if phase_value:
        normalized = str(phase_value).strip().lower()
        if normalized in _PHASE_SYNONYMS:
            return _PHASE_SYNONYMS[normalized]

    failover_failback_value = row_values.get("failover_failback")
    if failover_failback_value:
        normalized = str(failover_failback_value).strip().lower()
        if "failback" in normalized:
            return "FAILBACK"
        if "failover" in normalized:
            return "FAILOVER"

    return "FAILOVER"


def _parse_duration_minutes(value: Any) -> int | None:
    """Accepts a plain number (minutes) or an "Xh"/"Xm"/"Xh Ym" style string. Returns `None` for
    anything else -- an unparseable duration is left blank, not a reason to flag the row."""
    if value is None:
        return None
    if isinstance(value, int | float):
        return int(value)
    text_value = str(value).strip().lower()
    if not text_value:
        return None
    if text_value.isdigit():
        return int(text_value)
    hours_match = re.search(r"(\d+(?:\.\d+)?)\s*h", text_value)
    minutes_match = re.search(r"(\d+(?:\.\d+)?)\s*m", text_value)
    if not hours_match and not minutes_match:
        return None
    total = 0.0
    if hours_match:
        total += float(hours_match.group(1)) * 60
    if minutes_match:
        total += float(minutes_match.group(1))
    return int(total)


def _parse_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    text_value = str(value).strip().lower()
    if text_value in {"yes", "true", "1", "y"}:
        return True
    if text_value in {"no", "false", "0", "n"}:
        return False
    return default


@dataclass(frozen=True)
class AcceptSummary:
    created_task_count: int = 0
    created_dependency_count: int = 0
    needs_review_count: int = 0


async def process_accepted_import(
    session: AsyncSession,
    *,
    import_job: ImportJob,
    confirmed_mapping: dict[str, str | None],
    actor_id: uuid.UUID,
    clock: Clock | None = None,
) -> AcceptSummary:
    """Applies `confirmed_mapping` (source_header -> target_field, possibly edited by the reviewer
    from the heuristic proposal) against `import_job.mapping_data["rows"]` to create draft
    Tasks/Dependencies with `source_import_id`/`source_import_row` provenance (D-221). Row
    processing decisions (missing title / unresolved required context / ambiguous optional
    lookups) are documented in BUILD-05.plan.md Risk #4 -- this function is the implementation of
    that exact policy, not a place to invent new ones."""
    clock = clock or SystemClock()
    dr_event_id = import_job.dr_event_id
    dr_apps = await list_dr_applications(session, dr_event_id)
    application_id_to_dr_application = {app.application_id: app for app in dr_apps}

    rows: list[dict[str, Any]] = import_job.mapping_data.get("rows", [])
    title_to_task_id: dict[str, uuid.UUID] = {}
    pending_predecessors: list[tuple[uuid.UUID, int, str]] = []  # (successor_id, row_number, raw_value)

    summary_created_tasks = 0
    summary_needs_review: list[tuple[str, uuid.UUID, str]] = []  # (target_type, target_id, reason)

    for row in rows:
        row_number: int = row["row_number"]
        raw_values: dict[str, Any] = row["values"]
        mapped: dict[str, Any] = {}
        for source_header, raw_value in raw_values.items():
            target_field = confirmed_mapping.get(source_header)
            if target_field:
                mapped[target_field] = raw_value

        title = str(mapped.get("title") or mapped.get("subtask") or "").strip()
        if not title:
            summary_needs_review.append(
                ("IMPORT_JOB", import_job.id, f"Row {row_number}: missing required Task title")
            )
            continue

        owning_team_name = str(mapped.get("owning_team") or "").strip()
        team = await get_team_by_name(session, owning_team_name) if owning_team_name else None
        if team is None:
            summary_needs_review.append(
                (
                    "IMPORT_JOB",
                    import_job.id,
                    f"Row {row_number}: no resolvable Owning Team ({owning_team_name!r})",
                )
            )
            continue

        application_name = str(mapped.get("application") or "").strip()
        dr_application: DrApplication | None = None
        if application_name:
            application = await get_application_by_name(session, application_name)
            if application is not None:
                dr_application = application_id_to_dr_application.get(application.id)

        work_stream_id = None
        if dr_application is None:
            work_stream_name = str(mapped.get("work_stream") or "").strip()
            if work_stream_name:
                work_stream = await get_or_create_work_stream(
                    session, dr_event_id=dr_event_id, name=work_stream_name, actor_id=actor_id
                )
                work_stream_id = work_stream.id

        if dr_application is None and work_stream_id is None:
            summary_needs_review.append(
                (
                    "IMPORT_JOB",
                    import_job.id,
                    f"Row {row_number}: no resolvable Application or Work Stream",
                )
            )
            continue

        description = str(mapped["notes"]).strip() if mapped.get("notes") else None
        if mapped.get("subtask") and mapped.get("title"):
            procedure_note = f"Procedure: {mapped['subtask']}"
            description = f"{description}\n{procedure_note}" if description else procedure_note

        assignee_name = str(mapped.get("assignee") or "").strip()
        assignee_user_id = None
        row_needs_review_reasons: list[str] = []
        if assignee_name:
            assignee_user_id = await find_active_user_by_display_name(session, assignee_name)
            if assignee_user_id is None:
                row_needs_review_reasons.append(f"assignee not resolvable ({assignee_name!r})")

        try:
            task = await create_draft_task(
                session,
                dr_event_id=dr_event_id,
                title=title,
                phase=_resolve_phase(mapped),
                owning_team_id=team.id,
                created_by_user_id=actor_id,
                dr_application_id=dr_application.id if dr_application else None,
                work_stream_id=work_stream_id,
                description=description,
                current_assignee_user_id=assignee_user_id,
                expected_duration_minutes=_parse_duration_minutes(mapped.get("expected_duration")),
                evidence_required=_parse_bool(mapped.get("evidence_required"), default=True),
                source_import_id=import_job.id,
                source_import_row=str(row_number),
            )
        except TaskContextRequiredError:
            summary_needs_review.append(
                ("IMPORT_JOB", import_job.id, f"Row {row_number}: no resolvable Application or Work Stream")
            )
            continue

        summary_created_tasks += 1
        title_to_task_id[title.lower()] = task.id
        if row_needs_review_reasons:
            summary_needs_review.append(("TASK", task.id, "; ".join(row_needs_review_reasons)))

        predecessor_value = mapped.get("predecessor")
        if predecessor_value:
            pending_predecessors.append((task.id, row_number, str(predecessor_value)))

    summary_created_dependencies = 0
    created_edges: set[tuple[uuid.UUID, uuid.UUID]] = set()
    for successor_id, row_number, raw_predecessor_value in pending_predecessors:
        for predecessor_name in raw_predecessor_value.split(","):
            predecessor_name = predecessor_name.strip()
            if not predecessor_name:
                continue
            predecessor_task_id = title_to_task_id.get(predecessor_name.lower())
            if predecessor_task_id is None:
                summary_needs_review.append(
                    (
                        "TASK",
                        successor_id,
                        f"Row {row_number}: unresolved predecessor reference {predecessor_name!r}",
                    )
                )
                continue
            # Reject a direct 2-node cycle (this row's predecessor is X, and X's own predecessor,
            # created earlier in this same accept, is this row) instead of silently committing a
            # directed cycle into the live Task graph (invariant: "no directed cycles").
            reverse_edge = (successor_id, predecessor_task_id)
            if reverse_edge in created_edges or await dependency_exists(
                session, predecessor_task_id=successor_id, successor_task_id=predecessor_task_id
            ):
                summary_needs_review.append(
                    (
                        "TASK",
                        successor_id,
                        f"Row {row_number}: predecessor {predecessor_name!r} would create a "
                        "dependency cycle and was not linked",
                    )
                )
                continue
            dependency = await create_draft_dependency(
                session,
                dr_event_id=dr_event_id,
                predecessor_task_id=predecessor_task_id,
                successor_task_id=successor_id,
                created_by_user_id=actor_id,
            )
            if dependency is not None:
                created_edges.add((predecessor_task_id, successor_id))
                summary_created_dependencies += 1

    for target_type, target_id, reason in summary_needs_review:
        review_item = NeedsReviewItem(
            dr_event_id=dr_event_id,
            target_type=target_type,
            target_id=target_id,
            reason=reason,
            source="EXCEL_IMPORT",
        )
        session.add(review_item)
    if summary_needs_review:
        await session.flush()

    return AcceptSummary(
        created_task_count=summary_created_tasks,
        created_dependency_count=summary_created_dependencies,
        needs_review_count=len(summary_needs_review),
    )

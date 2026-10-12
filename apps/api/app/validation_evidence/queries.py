from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.blockers.queries import get_blocker
from app.core.target_types import TARGET_TYPE_SUBSETS, TargetType
from app.dr_events.queries import get_dr_application
from app.issues_findings.queries import get_issue
from app.tasks_dependencies.queries import get_task
from app.validation_evidence.models import EvidenceItem, Validation

EVIDENCE_TARGET_TYPES: frozenset[str] = frozenset(t.value for t in TARGET_TYPE_SUBSETS["evidence_items"])
#: Evidence that needs no scan to count (D-226 "attachment/log/screenshot/link/text per policy").
NON_FILE_EVIDENCE = frozenset({"TEXT", "LOG", "EXTERNAL_LINK"})
FILE_EVIDENCE = frozenset({"FILE", "SCREENSHOT"})


async def get_pending_task_validation(
    session: AsyncSession, task_id: uuid.UUID, *, for_update: bool = False
) -> Validation | None:
    """The single open (PENDING) Validation for a Task. A Task in READY_FOR_VALIDATION always has
    exactly one; a rejected submission's row stays REJECTED and a re-submit opens a new one, so
    older rows never match here."""
    stmt = select(Validation).where(
        Validation.target_type == "TASK",
        Validation.target_id == task_id,
        Validation.status == "PENDING",
        Validation.deleted_at.is_(None),
    )
    if for_update:
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_pending_validation(
    session: AsyncSession, target_type: str, target_id: uuid.UUID, *, for_update: bool = False
) -> Validation | None:
    """The single open (PENDING) Validation for any target (Task or DR Application)."""
    stmt = select(Validation).where(
        Validation.target_type == target_type,
        Validation.target_id == target_id,
        Validation.status == "PENDING",
        Validation.deleted_at.is_(None),
    )
    if for_update:
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_validation(session: AsyncSession, validation_id: uuid.UUID) -> Validation | None:
    return (
        await session.execute(
            select(Validation).where(Validation.id == validation_id, Validation.deleted_at.is_(None))
        )
    ).scalar_one_or_none()


@dataclass(frozen=True)
class EvidenceTarget:
    target_type: str
    target_id: uuid.UUID
    dr_event_id: uuid.UUID
    task_id: uuid.UUID | None  # the Task behind a TASK / BLOCKER / VALIDATION(TASK) target


async def resolve_evidence_target(
    session: AsyncSession, target_type: str, target_id: uuid.UUID
) -> EvidenceTarget | None:
    """The Event (and Task, where there is one) behind an evidence target, or None when it doesn't
    exist. Visibility is the caller's check (D-222)."""
    event_id: uuid.UUID | None = None
    task_id: uuid.UUID | None = None
    if target_type == TargetType.TASK:
        task = await get_task(session, target_id)
        if task is not None:
            event_id, task_id = task.dr_event_id, task.id
    elif target_type == TargetType.DR_APPLICATION:
        dr_application = await get_dr_application(session, target_id)
        event_id = dr_application.dr_event_id if dr_application is not None else None
    elif target_type == TargetType.MILESTONE:
        from app.milestones.queries import get_milestone  # late: milestones.queries imports tasks' views

        milestone = await get_milestone(session, target_id)
        event_id = milestone.dr_event_id if milestone is not None else None
    elif target_type == TargetType.BLOCKER:
        blocker = await get_blocker(session, target_id)
        if blocker is not None:
            task = await get_task(session, blocker.task_id)
            if task is not None:
                event_id, task_id = task.dr_event_id, task.id
    elif target_type == TargetType.VALIDATION:
        validation = await get_validation(session, target_id)
        if validation is not None and validation.target_type == "TASK":
            task = await get_task(session, validation.target_id)
            if task is not None:
                event_id, task_id = task.dr_event_id, task.id
        elif validation is not None:
            dr_application = await get_dr_application(session, validation.target_id)
            event_id = dr_application.dr_event_id if dr_application is not None else None
    elif target_type == TargetType.ISSUE_FINDING:
        issue = await get_issue(session, target_id)
        event_id = issue.dr_event_id if issue is not None else None
    if event_id is None:
        return None
    return EvidenceTarget(target_type=target_type, target_id=target_id, dr_event_id=event_id, task_id=task_id)


async def get_evidence(session: AsyncSession, evidence_id: uuid.UUID) -> EvidenceItem | None:
    return (
        await session.execute(
            select(EvidenceItem).where(EvidenceItem.id == evidence_id, EvidenceItem.deleted_at.is_(None))
        )
    ).scalar_one_or_none()


async def lock_evidence(session: AsyncSession, evidence_id: uuid.UUID) -> EvidenceItem | None:
    return (
        await session.execute(
            select(EvidenceItem)
            .where(EvidenceItem.id == evidence_id, EvidenceItem.deleted_at.is_(None))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def find_evidence_by_blob_uri(session: AsyncSession, blob_uri: str) -> EvidenceItem | None:
    return (
        await session.execute(
            select(EvidenceItem).where(EvidenceItem.blob_uri == blob_uri, EvidenceItem.deleted_at.is_(None))
        )
    ).scalar_one_or_none()


async def list_evidence_for_target(
    session: AsyncSession, target_type: str, target_id: uuid.UUID
) -> list[EvidenceItem]:
    return list(
        (
            await session.execute(
                select(EvidenceItem)
                .where(
                    EvidenceItem.target_type == target_type,
                    EvidenceItem.target_id == target_id,
                    EvidenceItem.deleted_at.is_(None),
                )
                .order_by(EvidenceItem.created_at, EvidenceItem.id)
            )
        )
        .scalars()
        .all()
    )


class AcceptableEvidenceCounter:
    """The real `EvidenceCounter` (D-226, D-245): TEXT / LOG / EXTERNAL_LINK always count; a FILE or
    SCREENSHOT counts only when its scan verdict is CLEAN. INFECTED or unscanned (PENDING, including a
    handshake whose bytes never arrived) never counts -- D-245, SECURITY_REVIEW.md:108 and DATA_MODEL
    "Evidence" allow no exception, so `file_policy.scanning_required` does not relax this."""

    async def count_acceptable_for_task(self, session: AsyncSession, task_id: uuid.UUID) -> int:
        base = (
            select(func.count())
            .select_from(EvidenceItem)
            .where(
                EvidenceItem.target_type == "TASK",
                EvidenceItem.target_id == task_id,
                EvidenceItem.deleted_at.is_(None),
            )
        )
        non_file = base.where(EvidenceItem.evidence_type.in_(NON_FILE_EVIDENCE))
        files = base.where(
            EvidenceItem.evidence_type.in_(FILE_EVIDENCE), EvidenceItem.malware_scan_status == "CLEAN"
        )
        return int((await session.execute(non_file)).scalar_one()) + int(
            (await session.execute(files)).scalar_one()
        )

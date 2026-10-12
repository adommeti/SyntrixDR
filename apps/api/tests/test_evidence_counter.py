"""The real `EvidenceCounter` (D-226, D-245): text/log/link always count; files only when CLEAN (or, with
`file_policy.scanning_required=false`, when scanned-or-not but never INFECTED). End to end:
EICAR evidence alone can never satisfy `submit-validation`."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import FakeClock
from app.core.errors import AppError
from app.tasks_dependencies.transition_service import TaskTransitionService
from app.validation_evidence.ports import get_evidence_counter
from app.validation_evidence.queries import AcceptableEvidenceCounter
from tests.factories import build_world, seed_evidence, seed_task, task_row

pytestmark = [pytest.mark.domain, pytest.mark.files]


@pytest.mark.parametrize(
    ("evidence_type", "scan", "attached", "scanning_required", "counts"),
    [
        ("TEXT", None, True, True, 1),
        ("LOG", None, True, True, 1),
        ("EXTERNAL_LINK", None, True, True, 1),
        ("FILE", "CLEAN", True, True, 1),
        ("SCREENSHOT", "CLEAN", True, True, 1),
        ("FILE", "PENDING", True, True, 0),  # unscanned never counts while scanning is required
        ("FILE", "INFECTED", True, True, 0),
        ("FILE", "PENDING", False, True, 0),  # handshake without bytes
        ("FILE", "PENDING", True, False, 1),  # scanning not required: unscanned counts
        ("FILE", "INFECTED", True, False, 0),  # ... but INFECTED never does
        ("FILE", "PENDING", False, False, 0),  # ... and bytes must have arrived
    ],
)
async def test_acceptable_rules(
    session: AsyncSession,
    evidence_type: str,
    scan: str | None,
    attached: bool,
    scanning_required: bool,
    counts: int,
) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w, status="IN_PROGRESS")
    await session.execute(text("UPDATE file_policy SET scanning_required = :s"), {"s": scanning_required})
    await seed_evidence(
        session, w, target_id=task_id, evidence_type=evidence_type, scan_status=scan, attached=attached
    )

    assert await AcceptableEvidenceCounter().count_acceptable_for_task(session, task_id) == counts


async def test_other_targets_and_deleted_items_never_count(session: AsyncSession) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w, status="IN_PROGRESS")
    other = await seed_task(session, w, status="IN_PROGRESS")
    await seed_evidence(session, w, target_id=other)
    await seed_evidence(session, w, target_type="DR_APPLICATION", target_id=w.dr_application_id)
    deleted = await seed_evidence(session, w, target_id=task_id)
    await session.execute(text("UPDATE evidence_items SET deleted_at = now() WHERE id = :e"), {"e": deleted})

    assert await AcceptableEvidenceCounter().count_acceptable_for_task(session, task_id) == 0


async def test_the_dependency_now_returns_the_real_counter() -> None:
    assert isinstance(await get_evidence_counter(), AcceptableEvidenceCounter)


async def test_eicar_evidence_alone_cannot_satisfy_completion(
    session: AsyncSession, clock: FakeClock
) -> None:
    """D-245 end to end: an INFECTED file is the only evidence -> `EVIDENCE_REQUIRED`; a CLEAN one passes."""
    w = await build_world(session)
    task_id = await seed_task(session, w, status="IN_PROGRESS", evidence_required=True, evidence_min_count=1)
    await seed_evidence(session, w, target_id=task_id, evidence_type="FILE", scan_status="INFECTED")

    with pytest.raises(AppError) as e:
        await TaskTransitionService.submit_validation(
            session, actor_id=w.executor_id, task_id=task_id, expected_version=1, verification_note="done",
            evidence_counter=AcceptableEvidenceCounter(), clock=clock,
        )  # fmt: skip
    assert e.value.code == "EVIDENCE_REQUIRED" and e.value.details["evidence_found"] == 0
    assert (await task_row(session, task_id)).status == "IN_PROGRESS"

    await seed_evidence(session, w, target_id=task_id, evidence_type="FILE", scan_status="CLEAN")
    await TaskTransitionService.submit_validation(
        session, actor_id=w.executor_id, task_id=task_id, expected_version=1, verification_note="done",
        evidence_counter=AcceptableEvidenceCounter(), clock=clock,
    )  # fmt: skip
    assert (await task_row(session, task_id)).status == "READY_FOR_VALIDATION"

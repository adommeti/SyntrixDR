"""Protected completion end to end with real evidence rows (D-209, D-210, D-226, D-245; DATA_MODEL
invariant 11). Every path to COMPLETED goes through `submit-validation` (PENDING record, evidence + note
guards) and `validate` by an Owner / Lead / Admin / Coordinator -- never the executor, never the submitter."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import FakeClock
from app.core.errors import AppError
from app.tasks_dependencies.transition_service import TaskTransitionService
from app.validation_evidence.queries import AcceptableEvidenceCounter
from tests.factories import World, build_world, headers, http, login, seed_evidence, seed_task, task_row

pytestmark = [pytest.mark.transitions, pytest.mark.files]

COUNTER = AcceptableEvidenceCounter()


async def _submit(
    session: AsyncSession,
    clock: FakeClock,
    w: World,
    task_id: uuid.UUID,
    *,
    note: str | None = "done",
    version: int = 1,
) -> Any:
    return await TaskTransitionService.submit_validation(
        session,
        actor_id=w.executor_id,
        task_id=task_id,
        expected_version=version,
        verification_note=note,
        evidence_counter=COUNTER,
        clock=clock,
    )


async def _validate(
    session: AsyncSession,
    clock: FakeClock,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
    *,
    approve: bool,
    version: int = 2,
) -> Any:
    return await TaskTransitionService.validate(
        session,
        actor_id=actor_id,
        task_id=task_id,
        expected_version=version,
        approve=approve,
        note="reviewed",
        clock=clock,
    )


async def _validations(session: AsyncSession, task_id: uuid.UUID) -> list[Any]:
    rows = await session.execute(
        text(
            "SELECT status, verification_note, note, validator_user_id, created_by_user_id FROM validations "
            "WHERE target_type = 'TASK' AND target_id = :t ORDER BY created_at"
        ),
        {"t": task_id},
    )
    return list(rows)


#: (evidence_required, min_count, seeded evidence [(type, scan)], note_required, note) -> submit outcome
GUARD: list[tuple[bool, int, list[tuple[str, str | None]], bool, str | None, str | None]] = [
    (True, 1, [("TEXT", None)], True, "done", None),
    (True, 1, [], True, "done", "EVIDENCE_REQUIRED"),
    (True, 2, [("TEXT", None)], True, "done", "EVIDENCE_REQUIRED"),
    (True, 2, [("TEXT", None), ("EXTERNAL_LINK", None)], True, "done", None),
    (True, 1, [("FILE", "CLEAN")], True, "done", None),
    (True, 1, [("FILE", "INFECTED")], True, "done", "EVIDENCE_REQUIRED"),  # D-245
    (True, 1, [("FILE", "PENDING")], True, "done", "EVIDENCE_REQUIRED"),  # unscanned
    (True, 1, [("FILE", "INFECTED"), ("TEXT", None)], True, "done", None),  # the text item carries it
    (True, 1, [("TEXT", None)], True, None, "EVIDENCE_REQUIRED"),  # note required
    (True, 1, [("TEXT", None)], True, "   ", "EVIDENCE_REQUIRED"),
    (True, 1, [("TEXT", None)], False, None, None),  # policy relaxed the note (D-226)
    (False, 1, [], True, "done", None),  # policy relaxed evidence for this Task (D-226)
    (False, 1, [], False, None, None),
]


@pytest.mark.parametrize(("evidence_required", "min_count", "items", "note_required", "note", "error"), GUARD)
async def test_submit_validation_guard_matrix(
    session: AsyncSession,
    clock: FakeClock,
    evidence_required: bool,
    min_count: int,
    items: list[tuple[str, str | None]],
    note_required: bool,
    note: str | None,
    error: str | None,
) -> None:
    w = await build_world(session)
    task_id = await seed_task(
        session,
        w,
        status="IN_PROGRESS",
        evidence_required=evidence_required,
        evidence_min_count=min_count,
        verification_note_required=note_required,
    )
    for evidence_type, scan in items:
        await seed_evidence(session, w, target_id=task_id, evidence_type=evidence_type, scan_status=scan)

    if error:
        with pytest.raises(AppError) as e:
            await _submit(session, clock, w, task_id, note=note)
        assert (e.value.code, e.value.status_code) == (error, 409)
        assert (await task_row(session, task_id)).status == "IN_PROGRESS"
        assert await _validations(session, task_id) == []
    else:
        await _submit(session, clock, w, task_id, note=note)
        assert (await task_row(session, task_id)).status == "READY_FOR_VALIDATION"
        [v] = await _validations(session, task_id)
        assert (v.status, v.verification_note, v.created_by_user_id) == (
            "PENDING",
            note.strip() if note else None,
            w.executor_id,
        )


#: (actor, app_scoped Task, allowed) -- D-209: Owner any slot for Application work, Lead for shared stream
#: work, Admin / Coordinator either; never the executor (the submitter here), never the Business Owner.
VALIDATORS = [
    ("admin_id", True, True),
    ("coordinator_id", True, True),
    ("system_owner_id", True, True),  # secondary slot (D-254)
    ("other_app_owner_id", True, False),  # owns a different Application
    ("ws_lead_id", True, False),  # Lead never validates Application-scoped work
    ("ws_lead_id", False, True),  # ... but does validate shared stream work
    ("system_owner_id", False, False),
    ("business_owner_id", True, False),
    ("manager_id", True, False),
    ("teammate_id", True, False),
    ("executor_id", True, False),  # the executor / submitter never self-completes
]


@pytest.mark.parametrize(("actor", "app_scoped", "allowed"), VALIDATORS)
async def test_validator_matrix(
    session: AsyncSession, clock: FakeClock, actor: str, app_scoped: bool, allowed: bool
) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w, status="IN_PROGRESS", app_scoped=app_scoped)
    await seed_evidence(session, w, target_id=task_id)
    await _submit(session, clock, w, task_id)

    if allowed:
        await _validate(session, clock, getattr(w, actor), task_id, approve=True)
        assert (await task_row(session, task_id)).status == "COMPLETED"
        [v] = await _validations(session, task_id)
        assert (v.status, v.validator_user_id, v.note) == ("APPROVED", getattr(w, actor), "reviewed")
    else:
        with pytest.raises(AppError) as e:
            await _validate(session, clock, getattr(w, actor), task_id, approve=True)
        assert e.value.status_code == 403
        assert (await task_row(session, task_id)).status == "READY_FOR_VALIDATION"


async def test_reject_returns_the_task_to_in_progress_and_keeps_the_record(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w, status="IN_PROGRESS")
    await seed_evidence(session, w, target_id=task_id)
    await _submit(session, clock, w, task_id)

    await _validate(session, clock, w.system_owner_id, task_id, approve=False)
    assert (await task_row(session, task_id)).status == "IN_PROGRESS"

    await _submit(session, clock, w, task_id, note="fixed and resubmitted", version=3)
    await _validate(session, clock, w.coordinator_id, task_id, approve=True, version=4)

    assert (await task_row(session, task_id)).status == "COMPLETED"
    statuses = {(v.status, v.verification_note) for v in await _validations(session, task_id)}
    assert statuses == {("REJECTED", "done"), ("APPROVED", "fixed and resubmitted")}  # D-210: records persist


async def test_no_other_command_reaches_completed(session: AsyncSession, clock: FakeClock) -> None:
    """D-209: only `validate(approve)` writes COMPLETED; every other command on a READY_FOR_VALIDATION
    Task is illegal or leaves it short of COMPLETED."""
    w = await build_world(session)
    task_id = await seed_task(session, w, status="READY_FOR_VALIDATION")
    for command, kwargs in (
        ("start", {}),
        ("block", {"reason": "x"}),
        ("resume", {}),
        ("submit_validation", {"verification_note": "x", "evidence_counter": COUNTER}),
    ):
        with pytest.raises(AppError) as e:
            await getattr(TaskTransitionService, command)(
                session, actor_id=w.admin_id, task_id=task_id, expected_version=1, clock=clock, **kwargs
            )
        assert e.value.code == "INVALID_TRANSITION", command
    assert (await task_row(session, task_id)).status == "READY_FOR_VALIDATION"


async def test_needs_specific_validation_still_requires_standard_validation(
    session: AsyncSession, clock: FakeClock
) -> None:
    """D-209: `true` adds Task-specific criteria on top of the standard Owner/Lead validation; it never
    replaces it, and the flag travels on the Validation audit for the validator to see."""
    w = await build_world(session)
    task_id = await seed_task(session, w, status="IN_PROGRESS")
    await session.execute(
        text("UPDATE tasks SET needs_specific_validation = TRUE WHERE id = :t"), {"t": task_id}
    )
    await seed_evidence(session, w, target_id=task_id)
    await _submit(session, clock, w, task_id)

    with pytest.raises(AppError):
        await _validate(session, clock, w.executor_id, task_id, approve=True)
    await _validate(session, clock, w.system_owner_id, task_id, approve=True)

    assert (await task_row(session, task_id)).status == "COMPLETED"


@pytest.mark.api
async def test_completion_over_http_with_a_real_clean_upload(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    """The route's real evidence counter (not a test double): handshake + CLEAN upload, submit, validate."""
    w = await build_world(session)
    task_id = await seed_task(session, w, status="IN_PROGRESS", evidence_required=True)
    sid, csrf = await login(session, redis_client, clock, w.executor_id)
    async with http(session, redis_client, clock) as c:
        short = await c.post(
            f"/api/v1/tasks/{task_id}/submit-validation",
            cookies={"drcc_session": sid},
            headers=headers(csrf),
            json={"expected_version": 1, "verification_note": "done"},
        )
        assert (short.status_code, short.json()["error"]["code"]) == (409, "EVIDENCE_REQUIRED")
        handshake = await c.post(
            "/api/v1/evidence",
            cookies={"drcc_session": sid},
            headers=headers(csrf),
            json={
                "target_type": "TASK",
                "target_id": str(task_id),
                "evidence_type": "FILE",
                "file_name": "proof.txt",
            },
        )
        upload = await c.post(
            f"/api/v1/evidence/{handshake.json()['id']}/file",
            cookies={"drcc_session": sid},
            headers=headers(csrf),
            files={"file": ("proof.txt", b"failover verified at 02:14\n", "text/plain")},
        )
        assert upload.json()["malware_scan_status"] == "CLEAN"
        submitted = await c.post(
            f"/api/v1/tasks/{task_id}/submit-validation",
            cookies={"drcc_session": sid},
            headers=headers(csrf),
            json={"expected_version": 1, "verification_note": "done"},
        )
        assert submitted.json()["status"] == "READY_FOR_VALIDATION", submitted.text
    await redis_client.delete(f"drcc:session:{sid}")

    sid, csrf = await login(session, redis_client, clock, w.system_owner_id)
    async with http(session, redis_client, clock) as c:
        done = await c.post(
            f"/api/v1/tasks/{task_id}/validate",
            cookies={"drcc_session": sid},
            headers=headers(csrf),
            json={"expected_version": 2, "approve": True, "note": "ok"},
        )
    await redis_client.delete(f"drcc:session:{sid}")
    assert done.json()["status"] == "COMPLETED", done.text

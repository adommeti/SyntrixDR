"""Service-level tests for `TaskTransitionService` (BUILD-06 session a): the canonical Task
lifecycle table, its guards, D-209's validator rule, D-226's evidence/note guard, object-level
authorization (404 vs 403), optimistic concurrency, and audit + outbox side effects.

The HTTP section at the end covers the route-level concerns (Idempotency-Key, replay, 401, CSRF,
the error envelope) -- in this file because tests.md requires every transition test file to.

Test worlds are seeded directly with SQL where a command that would normally produce the state
doesn't exist yet: Task creation/assignment (a later session / BUILD-07) and D-222 participant
auto-enrolment (BUILD-06.plan.md Risk #8), Blocker closing (BUILD-08).
"""

from __future__ import annotations

import uuid
from collections import Counter
from typing import Any

import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import FakeClock
from app.core.errors import AppError
from app.tasks_dependencies.commands import create_draft_task
from app.tasks_dependencies.transition_service import TaskTransitionService
from app.validation_evidence.ports import NoEvidenceItemsYet
from tests.factories import (
    FixedEvidence,
    audit_actions,
    build_world,
    count,
    depend,
    headers,
    http,
    login,
    outbox_types,
    seed_task,
    task_row,
)

pytestmark = [pytest.mark.transitions]

ALL_STATES = ("NOT_STARTED", "IN_PROGRESS", "BLOCKED", "READY_FOR_VALIDATION", "COMPLETED", "CANCELLED")


async def run(
    session: AsyncSession,
    command: str,
    *,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
    clock: FakeClock,
    expected_version: int = 1,
    **kwargs: Any,
) -> Any:
    """Dispatch one command with the arguments a legal call needs by default."""
    svc = TaskTransitionService
    common: dict[str, Any] = {
        "actor_id": actor_id,
        "task_id": task_id,
        "expected_version": expected_version,
        "clock": clock,
    }
    if command == "start":
        return await svc.start(session, **common, **kwargs)
    if command == "block":
        return await svc.block(session, **common, reason=kwargs.pop("reason", "Waiting on DNS"), **kwargs)
    if command == "resume":
        return await svc.resume(session, **common, **kwargs)
    if command == "submit_validation":
        return await svc.submit_validation(
            session,
            **common,
            verification_note=kwargs.pop("verification_note", "Checked the failover target"),
            evidence_counter=kwargs.pop("evidence_counter", NoEvidenceItemsYet()),
            **kwargs,
        )
    if command == "validate":
        return await svc.validate(session, **common, approve=kwargs.pop("approve", True), **kwargs)
    if command == "cancel":
        return await svc.cancel(session, **common, reason=kwargs.pop("reason", "Descoped"), **kwargs)
    raise AssertionError(command)


def error_code(exc: pytest.ExceptionInfo[AppError]) -> str:
    return exc.value.code


# --------------------------------------------------------------------------------------------
# Transition table (STATE_MACHINES.md §Task)
# --------------------------------------------------------------------------------------------

#: (from_state, command, extra kwargs, to_state)
LEGAL_ROWS: list[tuple[str, str, dict[str, Any], str]] = [
    ("NOT_STARTED", "start", {}, "IN_PROGRESS"),
    ("IN_PROGRESS", "block", {}, "BLOCKED"),
    ("BLOCKED", "resume", {}, "IN_PROGRESS"),  # seeded with no active Blocker: plain manual resume
    ("IN_PROGRESS", "submit_validation", {}, "READY_FOR_VALIDATION"),
    ("READY_FOR_VALIDATION", "validate", {"approve": True}, "COMPLETED"),
    ("READY_FOR_VALIDATION", "validate", {"approve": False}, "IN_PROGRESS"),
    ("NOT_STARTED", "cancel", {}, "CANCELLED"),
    ("IN_PROGRESS", "cancel", {}, "CANCELLED"),
    ("BLOCKED", "cancel", {}, "CANCELLED"),
    ("READY_FOR_VALIDATION", "cancel", {}, "CANCELLED"),
]

_LEGAL_FROM: dict[str, set[str]] = {}
for _from, _cmd, _kw, _to in LEGAL_ROWS:
    _LEGAL_FROM.setdefault(_cmd, set()).add(_from)

#: Every (state, command) cell not in LEGAL_ROWS -- including D-252's named traps
#: BLOCKED -> READY_FOR_VALIDATION and NOT_STARTED -> READY_FOR_VALIDATION, and anything out of a
#: terminal state.
ILLEGAL_ROWS: list[tuple[str, str]] = [
    (state, cmd) for cmd in _LEGAL_FROM for state in ALL_STATES if state not in _LEGAL_FROM[cmd]
]


@pytest.mark.parametrize(("from_state", "command", "kwargs", "to_state"), LEGAL_ROWS)
async def test_legal_transition(
    session: AsyncSession,
    clock: FakeClock,
    from_state: str,
    command: str,
    kwargs: dict[str, Any],
    to_state: str,
) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w, status=from_state, active_blocker=False)

    task = await run(session, command, actor_id=w.admin_id, task_id=task_id, clock=clock, **kwargs)

    assert task.status == to_state
    row = await task_row(session, task_id)
    assert row.status == to_state
    assert row.version == 2
    # Fixed Owning Team, floating Current Assignee: no lifecycle command touches either (the
    # assignee moves only through BUILD-07's assign/volunteer).
    assert (task.owning_team_id, task.current_assignee_user_id) == (w.team_id, w.executor_id)


@pytest.mark.parametrize(("from_state", "command"), ILLEGAL_ROWS)
async def test_illegal_transition_is_rejected_and_nothing_changes(
    session: AsyncSession, clock: FakeClock, from_state: str, command: str
) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w, status=from_state)
    audit_before = await audit_actions(session, task_id)

    with pytest.raises(AppError) as exc:
        await run(session, command, actor_id=w.admin_id, task_id=task_id, clock=clock)

    assert error_code(exc) == "INVALID_TRANSITION"
    assert exc.value.status_code == 409
    row = await task_row(session, task_id)
    assert (row.status, row.version) == (from_state, 1)
    assert await audit_actions(session, task_id) == audit_before
    assert await outbox_types(session, task_id) == []


def test_illegal_rows_cover_the_d252_traps() -> None:
    assert ("BLOCKED", "submit_validation") in ILLEGAL_ROWS
    assert ("NOT_STARTED", "submit_validation") in ILLEGAL_ROWS
    assert ("COMPLETED", "cancel") in ILLEGAL_ROWS
    assert ("CANCELLED", "start") in ILLEGAL_ROWS


# --------------------------------------------------------------------------------------------
# Side effects: audit + outbox in the same transaction, version, timestamps
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("from_state", "command", "kwargs", "to_state"), LEGAL_ROWS)
async def test_every_transition_writes_one_task_audit_row_and_one_outbox_row(
    session: AsyncSession,
    clock: FakeClock,
    from_state: str,
    command: str,
    kwargs: dict[str, Any],
    to_state: str,
) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w, status=from_state, active_blocker=False)
    audit_before = await audit_actions(session, task_id)

    await run(session, command, actor_id=w.admin_id, task_id=task_id, clock=clock, **kwargs)

    # Same-transaction rows share one now(), so diff as multisets, never by position.
    new_actions = list((Counter(await audit_actions(session, task_id)) - Counter(audit_before)).elements())
    assert len(new_actions) == 1
    assert new_actions[0].startswith("TASK_")
    assert await outbox_types(session, task_id) == ["TaskChanged"]


async def test_start_stamps_started_at_and_validate_approve_stamps_completed_at(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)
    started = await seed_task(session, w)
    await run(session, "start", actor_id=w.admin_id, task_id=started, clock=clock)
    assert (await task_row(session, started)).started_at == clock.now()

    ready = await seed_task(session, w, status="READY_FOR_VALIDATION")
    await run(session, "validate", actor_id=w.admin_id, task_id=ready, clock=clock, approve=True)
    assert (await task_row(session, ready)).completed_at == clock.now()


# --------------------------------------------------------------------------------------------
# start: HARD dependencies (derived Ready) and dependency override
# --------------------------------------------------------------------------------------------


async def test_start_blocked_by_an_incomplete_hard_predecessor(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)
    predecessor = await seed_task(session, w, status="IN_PROGRESS")
    task_id = await seed_task(session, w)
    await depend(session, w, predecessor=predecessor, successor=task_id)

    with pytest.raises(AppError) as exc:
        await run(session, "start", actor_id=w.admin_id, task_id=task_id, clock=clock)

    assert error_code(exc) == "DEPENDENCY_NOT_SATISFIED"
    assert exc.value.status_code == 409
    assert exc.value.details["blocking_task_ids"] == [str(predecessor)]
    assert (await task_row(session, task_id)).status == "NOT_STARTED"
    assert await count(session, "SELECT count(*) FROM overrides WHERE target_id = :t", t=task_id) == 0


async def test_start_allowed_once_the_hard_predecessor_is_completed(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)
    predecessor = await seed_task(session, w, status="COMPLETED")
    task_id = await seed_task(session, w)
    await depend(session, w, predecessor=predecessor, successor=task_id)

    task = await run(session, "start", actor_id=w.admin_id, task_id=task_id, clock=clock)

    assert task.status == "IN_PROGRESS"


async def test_advisory_predecessor_never_blocks_start(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)
    predecessor = await seed_task(session, w, status="NOT_STARTED")
    task_id = await seed_task(session, w)
    await depend(session, w, predecessor=predecessor, successor=task_id, strength="ADVISORY")

    task = await run(session, "start", actor_id=w.admin_id, task_id=task_id, clock=clock)

    assert task.status == "IN_PROGRESS"


async def test_dependency_override_records_one_override_row_and_starts(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)
    predecessor = await seed_task(session, w, status="IN_PROGRESS")
    task_id = await seed_task(session, w)
    await depend(session, w, predecessor=predecessor, successor=task_id)

    task = await run(
        session,
        "start",
        actor_id=w.admin_id,
        task_id=task_id,
        clock=clock,
        override_reason="Vendor confirmed",
    )

    assert task.status == "IN_PROGRESS"
    row = (
        await session.execute(
            text(
                "SELECT override_type, target_type, reason, performed_by_user_id "
                "FROM overrides WHERE target_id = :t"
            ),
            {"t": task_id},
        )
    ).one()
    assert (row.override_type, row.target_type, row.reason) == (
        "DEPENDENCY_OVERRIDE",
        "TASK",
        "Vendor confirmed",
    )
    assert row.performed_by_user_id == w.admin_id
    assert "DEPENDENCY_OVERRIDE_RECORDED" in await audit_actions(session, task_id)


async def test_blank_override_reason_is_no_override(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)
    predecessor = await seed_task(session, w, status="IN_PROGRESS")
    task_id = await seed_task(session, w)
    await depend(session, w, predecessor=predecessor, successor=task_id)

    with pytest.raises(AppError) as exc:
        await run(session, "start", actor_id=w.admin_id, task_id=task_id, clock=clock, override_reason="   ")

    assert error_code(exc) == "DEPENDENCY_NOT_SATISFIED"


async def test_ws_lead_may_override_a_same_stream_dependency(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)
    predecessor = await seed_task(session, w, status="IN_PROGRESS", app_scoped=False)
    task_id = await seed_task(session, w, app_scoped=False)
    await depend(session, w, predecessor=predecessor, successor=task_id)

    task = await run(
        session, "start", actor_id=w.ws_lead_id, task_id=task_id, clock=clock, override_reason="Same lane"
    )

    assert task.status == "IN_PROGRESS"


async def test_ws_lead_may_not_override_a_cross_stream_dependency(
    session: AsyncSession, clock: FakeClock
) -> None:
    """BUILD-06.plan.md Risk #5: a predecessor in another Work Stream means the *cross*-stream
    guard is what's being overridden -- Admin/Coordinator only (RBAC_MATRIX.md)."""
    w = await build_world(session)
    predecessor = await seed_task(session, w, status="IN_PROGRESS", work_stream_id=w.other_work_stream_id)
    task_id = await seed_task(session, w, app_scoped=False)
    await depend(session, w, predecessor=predecessor, successor=task_id)

    with pytest.raises(AppError) as exc:
        await run(
            session, "start", actor_id=w.ws_lead_id, task_id=task_id, clock=clock, override_reason="Trust me"
        )

    assert exc.value.status_code == 403
    assert (await task_row(session, task_id)).status == "NOT_STARTED"
    assert await count(session, "SELECT count(*) FROM overrides WHERE target_id = :t", t=task_id) == 0


async def test_executor_cannot_override_a_dependency(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)
    predecessor = await seed_task(session, w, status="IN_PROGRESS")
    task_id = await seed_task(session, w)
    await depend(session, w, predecessor=predecessor, successor=task_id)

    with pytest.raises(AppError) as exc:
        await run(
            session, "start", actor_id=w.executor_id, task_id=task_id, clock=clock, override_reason="Hurry"
        )

    assert exc.value.status_code == 403


# --------------------------------------------------------------------------------------------
# block / resume (invariant #10, D-252, I-1)
# --------------------------------------------------------------------------------------------


async def test_block_creates_exactly_one_open_blocker(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w, status="IN_PROGRESS")

    await run(
        session,
        "block",
        actor_id=w.executor_id,
        task_id=task_id,
        clock=clock,
        reason="Firewall change pending",
    )

    rows = (
        await session.execute(text("SELECT status, reason FROM blockers WHERE task_id = :t"), {"t": task_id})
    ).all()
    assert [(r.status, r.reason) for r in rows] == [("OPEN", "Firewall change pending")]


@pytest.mark.parametrize("reason", ["", "   "])
async def test_block_requires_a_reason(session: AsyncSession, clock: FakeClock, reason: str) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w, status="IN_PROGRESS")

    with pytest.raises(AppError) as exc:
        await run(session, "block", actor_id=w.executor_id, task_id=task_id, clock=clock, reason=reason)

    assert error_code(exc) == "OVERRIDE_REASON_REQUIRED"
    assert exc.value.status_code == 400
    assert (await task_row(session, task_id)).status == "IN_PROGRESS"
    assert await count(session, "SELECT count(*) FROM blockers WHERE task_id = :t", t=task_id) == 0


async def test_resume_while_a_blocker_is_active_needs_an_override_reason(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w, status="BLOCKED", active_blocker=True)

    with pytest.raises(AppError) as exc:
        await run(session, "resume", actor_id=w.admin_id, task_id=task_id, clock=clock)

    assert error_code(exc) == "OVERRIDE_REASON_REQUIRED"
    assert (await task_row(session, task_id)).status == "BLOCKED"


async def test_resume_override_despite_active_blocker_is_audited_and_leaves_the_blocker_open(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w, status="BLOCKED", active_blocker=True)

    task = await run(
        session, "resume", actor_id=w.admin_id, task_id=task_id, clock=clock, override_reason="Workaround"
    )

    assert task.status == "IN_PROGRESS"
    override_type = await session.scalar(
        text("SELECT override_type FROM overrides WHERE target_id = :t"), {"t": task_id}
    )
    assert override_type == "BLOCKER_ACTIVE_RESUME_OVERRIDE"
    assert (
        await count(
            session, "SELECT count(*) FROM blockers WHERE task_id = :t AND status = 'OPEN'", t=task_id
        )
        == 1
    )


async def test_executor_cannot_resume_over_an_active_blocker(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w, status="BLOCKED", active_blocker=True)

    with pytest.raises(AppError) as exc:
        await run(
            session, "resume", actor_id=w.executor_id, task_id=task_id, clock=clock, override_reason="Please"
        )

    assert exc.value.status_code == 403


async def test_executor_may_resume_once_no_active_blocker_remains(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w, status="BLOCKED", active_blocker=False)

    task = await run(session, "resume", actor_id=w.executor_id, task_id=task_id, clock=clock)

    assert task.status == "IN_PROGRESS"
    assert "TASK_RESUMED" in await audit_actions(session, task_id)
    assert await count(session, "SELECT count(*) FROM overrides WHERE target_id = :t", t=task_id) == 0


# --------------------------------------------------------------------------------------------
# submit-validation (D-226, D-210)
# --------------------------------------------------------------------------------------------


async def test_default_evidence_requirement_cannot_be_met_before_build_09(
    session: AsyncSession, clock: FakeClock
) -> None:
    """BUILD-06.plan.md Risk #4: `NoEvidenceItemsYet` honestly reports 0, so D-226's default
    (evidence_required, min 1) blocks submission until BUILD-09 wires real evidence."""
    w = await build_world(session)
    task_id = await seed_task(session, w, status="IN_PROGRESS", evidence_required=True, evidence_min_count=1)

    with pytest.raises(AppError) as exc:
        await run(session, "submit_validation", actor_id=w.executor_id, task_id=task_id, clock=clock)

    assert error_code(exc) == "EVIDENCE_REQUIRED"
    assert exc.value.status_code == 409
    assert exc.value.details == {"evidence_min_count": 1, "evidence_found": 0}
    assert (await task_row(session, task_id)).status == "IN_PROGRESS"
    assert await count(session, "SELECT count(*) FROM validations WHERE target_id = :t", t=task_id) == 0


async def test_enough_evidence_satisfies_the_guard(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w, status="IN_PROGRESS", evidence_required=True, evidence_min_count=2)

    task = await run(
        session,
        "submit_validation",
        actor_id=w.executor_id,
        task_id=task_id,
        clock=clock,
        evidence_counter=FixedEvidence(2),
    )

    assert task.status == "READY_FOR_VALIDATION"


async def test_one_short_of_evidence_min_count_is_rejected(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w, status="IN_PROGRESS", evidence_required=True, evidence_min_count=2)

    with pytest.raises(AppError) as exc:
        await run(
            session,
            "submit_validation",
            actor_id=w.executor_id,
            task_id=task_id,
            clock=clock,
            evidence_counter=FixedEvidence(1),
        )

    assert error_code(exc) == "EVIDENCE_REQUIRED"


@pytest.mark.parametrize("note", [None, "", "   "])
async def test_verification_note_required_when_the_task_says_so(
    session: AsyncSession, clock: FakeClock, note: str | None
) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w, status="IN_PROGRESS", verification_note_required=True)

    with pytest.raises(AppError) as exc:
        await run(
            session,
            "submit_validation",
            actor_id=w.executor_id,
            task_id=task_id,
            clock=clock,
            verification_note=note,
        )

    assert error_code(exc) == "EVIDENCE_REQUIRED"
    assert exc.value.details == {"verification_note_required": True}


async def test_note_optional_when_not_required(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w, status="IN_PROGRESS", verification_note_required=False)

    task = await run(
        session,
        "submit_validation",
        actor_id=w.executor_id,
        task_id=task_id,
        clock=clock,
        verification_note=None,
    )

    assert task.status == "READY_FOR_VALIDATION"


async def test_submit_opens_exactly_one_pending_validation_carrying_the_submitters_note(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w, status="IN_PROGRESS")

    await run(
        session,
        "submit_validation",
        actor_id=w.executor_id,
        task_id=task_id,
        clock=clock,
        verification_note="DNS resolves to DR site",
    )

    row = (
        await session.execute(
            text(
                "SELECT status, verification_note, note, created_by_user_id, submitted_at "
                "FROM validations WHERE target_type = 'TASK' AND target_id = :t"
            ),
            {"t": task_id},
        )
    ).one()
    assert (row.status, row.verification_note, row.note) == ("PENDING", "DNS resolves to DR site", None)
    assert row.created_by_user_id == w.executor_id
    assert row.submitted_at == clock.now()


async def test_rejection_keeps_the_rejected_row_and_a_resubmit_opens_a_new_one(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w, status="IN_PROGRESS")

    await run(session, "submit_validation", actor_id=w.executor_id, task_id=task_id, clock=clock)
    await run(
        session,
        "validate",
        actor_id=w.system_owner_id,
        task_id=task_id,
        clock=clock,
        expected_version=2,
        approve=False,
        note="Evidence doesn't show the cutover",
    )
    assert (await task_row(session, task_id)).status == "IN_PROGRESS"
    await run(
        session, "submit_validation", actor_id=w.executor_id, task_id=task_id, clock=clock, expected_version=3
    )

    rows = (
        await session.execute(
            text("SELECT status, note FROM validations WHERE target_id = :t ORDER BY created_at, status"),
            {"t": task_id},
        )
    ).all()
    assert sorted((r.status, r.note) for r in rows) == [
        ("PENDING", None),
        ("REJECTED", "Evidence doesn't show the cutover"),
    ]


# --------------------------------------------------------------------------------------------
# validate: D-209 validator rule
# --------------------------------------------------------------------------------------------


async def test_any_system_owner_slot_may_validate_application_scoped_work(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w, status="READY_FOR_VALIDATION")

    task = await run(session, "validate", actor_id=w.system_owner_id, task_id=task_id, clock=clock)  # slot 2

    assert task.status == "COMPLETED"
    row = (
        await session.execute(
            text("SELECT status, validator_user_id, approved_at FROM validations WHERE target_id = :t"),
            {"t": task_id},
        )
    ).one()
    assert (row.status, row.validator_user_id, row.approved_at) == (
        "APPROVED",
        w.system_owner_id,
        clock.now(),
    )


@pytest.mark.parametrize(
    "who", ["business_owner_id", "ws_lead_id", "other_app_owner_id", "executor_id", "manager_id"]
)
async def test_only_owners_of_this_application_validate_application_scoped_work(
    session: AsyncSession, clock: FakeClock, who: str
) -> None:
    """D-209: Application-scoped work -> Application/System Owner only. Never a Business Owner
    (RBAC_MATRIX.md), never a Work Stream Lead (that's shared-stream work), never another
    Application's owner."""
    w = await build_world(session)
    task_id = await seed_task(
        session, w, status="READY_FOR_VALIDATION", assignee_id=w.teammate_id, submitter_id=w.teammate_id
    )

    with pytest.raises(AppError) as exc:
        await run(session, "validate", actor_id=getattr(w, who), task_id=task_id, clock=clock)

    assert exc.value.status_code == 403
    assert (await task_row(session, task_id)).status == "READY_FOR_VALIDATION"


async def test_ws_lead_validates_shared_work_stream_work(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w, status="READY_FOR_VALIDATION", app_scoped=False)

    task = await run(session, "validate", actor_id=w.ws_lead_id, task_id=task_id, clock=clock)

    assert task.status == "COMPLETED"


async def test_app_owner_cannot_validate_shared_work_stream_work(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w, status="READY_FOR_VALIDATION", app_scoped=False)

    with pytest.raises(AppError) as exc:
        await run(session, "validate", actor_id=w.system_owner_id, task_id=task_id, clock=clock)

    assert exc.value.status_code == 403


async def test_the_assignee_can_never_validate_their_own_task(
    session: AsyncSession, clock: FakeClock
) -> None:
    """D-209: "Executors can never self-complete" -- even when they otherwise hold validator
    authority (here: a System Owner who is also the Task's assignee)."""
    w = await build_world(session)
    task_id = await seed_task(
        session, w, status="READY_FOR_VALIDATION", assignee_id=w.system_owner_id, submitter_id=w.teammate_id
    )

    with pytest.raises(AppError) as exc:
        await run(session, "validate", actor_id=w.system_owner_id, task_id=task_id, clock=clock)

    assert error_code(exc) == "SELF_VALIDATION_FORBIDDEN"
    assert exc.value.status_code == 403
    assert (await task_row(session, task_id)).status == "READY_FOR_VALIDATION"


async def test_the_submitter_can_never_validate_their_own_submission(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w, status="READY_FOR_VALIDATION", submitter_id=w.admin_id)

    with pytest.raises(AppError) as exc:
        await run(session, "validate", actor_id=w.admin_id, task_id=task_id, clock=clock)

    assert error_code(exc) == "SELF_VALIDATION_FORBIDDEN"


# --------------------------------------------------------------------------------------------
# Who may execute a Task (BUILD-06.plan.md Risk #2)
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("who", ["executor_id", "teammate_id", "manager_id", "ws_lead_id", "admin_id"])
async def test_who_may_start_a_task(session: AsyncSession, clock: FakeClock, who: str) -> None:
    """Assignee and Owning-Team members holding the EXECUTOR role ("own/Team work"), the Team's
    Manager (OWN_TEAM), the Work Stream Lead in scope, and Admin."""
    w = await build_world(session)
    task_id = await seed_task(session, w)

    task = await run(session, "start", actor_id=getattr(w, who), task_id=task_id, clock=clock)

    assert task.status == "IN_PROGRESS"


@pytest.mark.parametrize("who", ["outsider_id", "roleless_member_id", "business_owner_id"])
async def test_visible_but_unauthorized_actors_get_403(
    session: AsyncSession, clock: FakeClock, who: str
) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w)

    with pytest.raises(AppError) as exc:
        await run(session, "start", actor_id=getattr(w, who), task_id=task_id, clock=clock)

    assert exc.value.status_code == 403
    assert (await task_row(session, task_id)).status == "NOT_STARTED"
    assert await outbox_types(session, task_id) == []


@pytest.mark.parametrize("command", ["start", "block", "resume", "submit_validation", "validate", "cancel"])
async def test_a_task_in_an_event_the_actor_cannot_see_is_404_never_403(
    session: AsyncSession, clock: FakeClock, command: str
) -> None:
    """Invariant #1: UUID knowledge is not authorization, and a 403 here would confirm the Task
    exists. The stranger holds the EXECUTOR role but is not a participant of the Event."""
    w = await build_world(session)
    task_id = await seed_task(session, w)

    with pytest.raises(AppError) as exc:
        await run(session, command, actor_id=w.stranger_id, task_id=task_id, clock=clock)

    assert error_code(exc) == "TASK_NOT_FOUND"
    assert exc.value.status_code == 404


async def test_nonexistent_task_is_404(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)

    with pytest.raises(AppError) as exc:
        await run(session, "start", actor_id=w.admin_id, task_id=uuid.uuid4(), clock=clock)

    assert error_code(exc) == "TASK_NOT_FOUND"


async def test_executor_may_not_cancel(session: AsyncSession, clock: FakeClock) -> None:
    """Cancelling is a management decision (CHANGE_TASK_METADATA), not execution."""
    w = await build_world(session)
    task_id = await seed_task(session, w)

    with pytest.raises(AppError) as exc:
        await run(session, "cancel", actor_id=w.executor_id, task_id=task_id, clock=clock)

    assert exc.value.status_code == 403


@pytest.mark.parametrize("reason", ["", "   "])
async def test_cancel_requires_a_reason(session: AsyncSession, clock: FakeClock, reason: str) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w)

    with pytest.raises(AppError) as exc:
        await run(session, "cancel", actor_id=w.admin_id, task_id=task_id, clock=clock, reason=reason)

    assert error_code(exc) == "OVERRIDE_REASON_REQUIRED"
    assert (await task_row(session, task_id)).status == "NOT_STARTED"


# --------------------------------------------------------------------------------------------
# Optimistic concurrency
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("command", ["start", "cancel"])
async def test_stale_expected_version_is_409_and_nothing_changes(
    session: AsyncSession, clock: FakeClock, command: str
) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w)
    audit_before = await audit_actions(session, task_id)

    with pytest.raises(AppError) as exc:
        await run(session, command, actor_id=w.admin_id, task_id=task_id, clock=clock, expected_version=7)

    assert error_code(exc) == "CONCURRENCY_CONFLICT"
    assert exc.value.status_code == 409
    assert (await task_row(session, task_id)).version == 1
    assert await audit_actions(session, task_id) == audit_before


async def test_version_check_reads_the_locked_row_not_a_cached_copy(
    session: AsyncSession, clock: FakeClock
) -> None:
    """The Task stays in this session's identity map after creation; a concurrent writer then
    bumps its version. The service must compare against the row it locks, not the cached copy --
    otherwise a correct `expected_version` is wrongly rejected (and a stale one wrongly accepted)."""
    w = await build_world(session)
    task = await create_draft_task(
        session,
        dr_event_id=w.event_id,
        title="Cached",
        phase="FAILOVER",
        owning_team_id=w.team_id,
        created_by_user_id=w.admin_id,
        work_stream_id=w.work_stream_id,
        current_assignee_user_id=w.executor_id,
    )
    assert task.version == 1
    await session.execute(text("UPDATE tasks SET version = 2 WHERE id = :id"), {"id": task.id})

    started = await run(
        session, "start", actor_id=w.admin_id, task_id=task.id, clock=clock, expected_version=2
    )

    assert (started.status, started.version) == ("IN_PROGRESS", 3)


# --------------------------------------------------------------------------------------------
# HTTP: Idempotency-Key (D-215), replay, 401, CSRF, the API_CONTRACT error envelope
# --------------------------------------------------------------------------------------------


#: path segment -> a body that's valid for that command
BODIES: dict[str, dict[str, Any]] = {
    "start": {"expected_version": 1},
    "block": {"expected_version": 1, "reason": "Waiting on DNS"},
    "resume": {"expected_version": 1},
    "submit-validation": {"expected_version": 1, "verification_note": "Checked"},
    "validate": {"expected_version": 1, "approve": True},
    "cancel": {"expected_version": 1, "reason": "Descoped"},
}


@pytest.mark.api
@pytest.mark.parametrize("path", list(BODIES))
async def test_every_task_command_requires_an_idempotency_key(
    session: AsyncSession, redis_client: Redis, clock: FakeClock, path: str
) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w)
    sid, csrf = await login(session, redis_client, clock, w.admin_id)

    async with http(session, redis_client, clock) as c:
        response = await c.post(
            f"/api/v1/tasks/{task_id}/{path}",
            cookies={"drcc_session": sid},
            headers={"X-CSRF-Token": csrf},
            json=BODIES[path],
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert (await task_row(session, task_id)).version == 1
    await redis_client.delete(f"drcc:session:{sid}")


@pytest.mark.api
async def test_block_replay_returns_the_original_response_and_repeats_nothing(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w, status="IN_PROGRESS")
    sid, csrf = await login(session, redis_client, clock, w.executor_id)
    replay_headers = headers(csrf)

    async with http(session, redis_client, clock) as c:
        url = f"/api/v1/tasks/{task_id}/block"
        first = await c.post(url, cookies={"drcc_session": sid}, headers=replay_headers, json=BODIES["block"])
        replay = await c.post(
            url, cookies={"drcc_session": sid}, headers=replay_headers, json=BODIES["block"]
        )

    assert first.status_code == 200
    assert (first.json()["status"], first.json()["version"]) == ("BLOCKED", 2)
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert await count(session, "SELECT count(*) FROM blockers WHERE task_id = :t", t=task_id) == 1
    assert Counter(await audit_actions(session, task_id))["TASK_BLOCKED"] == 1
    assert await outbox_types(session, task_id) == ["TaskChanged"]
    await redis_client.delete(f"drcc:session:{sid}")


@pytest.mark.api
async def test_submit_validation_replay_opens_only_one_validation(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w, status="IN_PROGRESS")
    sid, csrf = await login(session, redis_client, clock, w.executor_id)
    replay_headers = headers(csrf)

    async with http(session, redis_client, clock) as c:
        url = f"/api/v1/tasks/{task_id}/submit-validation"
        body = BODIES["submit-validation"]
        first = await c.post(url, cookies={"drcc_session": sid}, headers=replay_headers, json=body)
        replay = await c.post(url, cookies={"drcc_session": sid}, headers=replay_headers, json=body)

    assert first.status_code == replay.status_code == 200
    assert replay.json() == first.json()
    assert await count(session, "SELECT count(*) FROM validations WHERE target_id = :t", t=task_id) == 1
    await redis_client.delete(f"drcc:session:{sid}")


@pytest.mark.api
async def test_reusing_a_key_with_a_different_body_is_rejected(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w, status="IN_PROGRESS")
    sid, csrf = await login(session, redis_client, clock, w.executor_id)
    reused = headers(csrf)

    async with http(session, redis_client, clock) as c:
        url = f"/api/v1/tasks/{task_id}/block"
        await c.post(url, cookies={"drcc_session": sid}, headers=reused, json=BODIES["block"])
        second = await c.post(
            url,
            cookies={"drcc_session": sid},
            headers=reused,
            json={"expected_version": 1, "reason": "Other"},
        )

    assert second.status_code == 422
    assert second.json()["error"]["code"] == "IDEMPOTENCY_REQUEST_MISMATCH"
    await redis_client.delete(f"drcc:session:{sid}")


@pytest.mark.api
@pytest.mark.auth
async def test_unauthenticated_command_is_401(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w)

    async with http(session, redis_client, clock) as c:
        response = await c.post(
            f"/api/v1/tasks/{task_id}/start",
            headers={"Idempotency-Key": str(uuid.uuid4())},
            json=BODIES["start"],
        )

    assert response.status_code == 401
    assert (await task_row(session, task_id)).version == 1


@pytest.mark.api
@pytest.mark.auth
async def test_command_without_csrf_token_is_rejected(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w)
    sid, _csrf = await login(session, redis_client, clock, w.admin_id)

    async with http(session, redis_client, clock) as c:
        response = await c.post(
            f"/api/v1/tasks/{task_id}/start",
            cookies={"drcc_session": sid},
            headers={"Idempotency-Key": str(uuid.uuid4())},
            json=BODIES["start"],
        )

    assert response.status_code == 403
    assert (await task_row(session, task_id)).version == 1
    await redis_client.delete(f"drcc:session:{sid}")


@pytest.mark.api
async def test_domain_errors_render_the_api_contract_envelope(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w, status="COMPLETED")
    sid, csrf = await login(session, redis_client, clock, w.admin_id)

    async with http(session, redis_client, clock) as c:
        response = await c.post(
            f"/api/v1/tasks/{task_id}/start",
            cookies={"drcc_session": sid},
            headers=headers(csrf),
            json=BODIES["start"],
        )

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "INVALID_TRANSITION"
    assert error["correlation_id"]
    assert "Traceback" not in response.text
    await redis_client.delete(f"drcc:session:{sid}")


@pytest.mark.api
async def test_default_evidence_counter_honestly_reports_none_over_http(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    """The route's real dependency (not a test double) is `NoEvidenceItemsYet` until BUILD-09."""
    w = await build_world(session)
    task_id = await seed_task(session, w, status="IN_PROGRESS", evidence_required=True)
    sid, csrf = await login(session, redis_client, clock, w.executor_id)

    async with http(session, redis_client, clock) as c:
        response = await c.post(
            f"/api/v1/tasks/{task_id}/submit-validation",
            cookies={"drcc_session": sid},
            headers=headers(csrf),
            json=BODIES["submit-validation"],
        )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "EVIDENCE_REQUIRED"
    assert response.json()["error"]["details"]["evidence_found"] == 0
    await redis_client.delete(f"drcc:session:{sid}")


@pytest.mark.api
async def test_get_task_is_visible_to_participants_and_404_to_everyone_else(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w)
    member_sid, _ = await login(session, redis_client, clock, w.executor_id)
    stranger_sid, _ = await login(session, redis_client, clock, w.stranger_id)

    async with http(session, redis_client, clock) as c:
        seen = await c.get(f"/api/v1/tasks/{task_id}", cookies={"drcc_session": member_sid})
        hidden = await c.get(f"/api/v1/tasks/{task_id}", cookies={"drcc_session": stranger_sid})
        missing = await c.get(f"/api/v1/tasks/{uuid.uuid4()}", cookies={"drcc_session": member_sid})

    assert seen.status_code == 200
    body = seen.json()
    assert (body["id"], body["status"], body["owning_team_id"]) == (
        str(task_id),
        "NOT_STARTED",
        str(w.team_id),
    )
    assert (body["evidence_required"], body["evidence_min_count"], body["verification_note_required"]) == (
        False,
        1,
        True,
    )
    assert hidden.status_code == missing.status_code == 404
    assert hidden.json()["error"]["code"] == missing.json()["error"]["code"] == "TASK_NOT_FOUND"
    for sid in (member_sid, stranger_sid):
        await redis_client.delete(f"drcc:session:{sid}")


@pytest.mark.api
async def test_full_task_lifecycle_over_http(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    """Executor starts, blocks, resumes, submits; a System Owner validates. The Blocker is closed by
    SQL in the middle, standing in for BUILD-08's `blockers/{id}/verify`."""
    w = await build_world(session)
    task_id = await seed_task(session, w)
    exec_sid, exec_csrf = await login(session, redis_client, clock, w.executor_id)
    owner_sid, owner_csrf = await login(session, redis_client, clock, w.system_owner_id)

    async with http(session, redis_client, clock) as c:

        async def post(sid: str, csrf: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
            r = await c.post(
                f"/api/v1/tasks/{task_id}/{path}",
                cookies={"drcc_session": sid},
                headers=headers(csrf),
                json=body,
            )
            assert r.status_code == 200, r.text
            return r.json()

        assert (await post(exec_sid, exec_csrf, "start", {"expected_version": 1}))["status"] == "IN_PROGRESS"
        assert (await post(exec_sid, exec_csrf, "block", {"expected_version": 2, "reason": "DNS"}))[
            "status"
        ] == "BLOCKED"
        await session.execute(
            text("UPDATE blockers SET status = 'CLOSED' WHERE task_id = :t"), {"t": task_id}
        )
        assert (await post(exec_sid, exec_csrf, "resume", {"expected_version": 3}))["status"] == "IN_PROGRESS"
        submitted = await post(
            exec_sid,
            exec_csrf,
            "submit-validation",
            {"expected_version": 4, "verification_note": "Resolves at DR"},
        )
        assert submitted["status"] == "READY_FOR_VALIDATION"
        done = await post(owner_sid, owner_csrf, "validate", {"expected_version": 5, "approve": True})

    assert (done["status"], done["version"]) == ("COMPLETED", 6)
    assert done["completed_at"] is not None
    assert Counter(await outbox_types(session, task_id))["TaskChanged"] == 5
    for sid in (exec_sid, owner_sid):
        await redis_client.delete(f"drcc:session:{sid}")

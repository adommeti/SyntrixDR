"""DR Application technical validation (STATE_MACHINES.md §Application recovery, I-3, D-208, D-210, D-254):
`submit-validation` opens the PENDING record (guard: no phase Task BLOCKED/IN_PROGRESS unless override);
`validate` closes it and moves TECHNICAL_VALIDATION → FAILED_OVER | COMPLETED, or back to RECOVERING."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import FakeClock
from app.core.errors import AppError
from app.dr_events.models import DrApplication
from app.dr_events.transition_service import DrApplicationTransitionService
from tests.factories import World, audit_actions, build_world, outbox_types, seed_task

pytestmark = [pytest.mark.transitions]

RECOVERING, TV, FO, FB, DONE, NS = (
    "RECOVERING", "TECHNICAL_VALIDATION", "FAILED_OVER", "FAILBACK_IN_PROGRESS", "COMPLETED", "NOT_STARTED",
)  # fmt: skip
ALL = (NS, RECOVERING, TV, FO, FB, DONE)


async def _app_in(
    session: AsyncSession, w: World, status: str, *, failback_required: bool = True
) -> uuid.UUID:
    await session.execute(
        text(
            "UPDATE dr_applications SET status = CAST(:s AS dr_application_status), "
            "failback_required = :f WHERE id = :a"
        ),
        {"s": status, "f": failback_required, "a": w.dr_application_id},
    )
    return w.dr_application_id


async def _row(session: AsyncSession, app_id: uuid.UUID) -> Any:
    return (
        await session.execute(
            text("SELECT status, version, technical_validated_at FROM dr_applications WHERE id = :a"),
            {"a": app_id},
        )
    ).one()


async def _validations(session: AsyncSession, app_id: uuid.UUID) -> list[Any]:
    rows = await session.execute(
        text(
            "SELECT status, verification_note, note, business_confirmed, created_by_user_id, "
            "validator_user_id FROM validations WHERE target_type = 'DR_APPLICATION' AND target_id = :a "
            "ORDER BY created_at"
        ),
        {"a": app_id},
    )
    return list(rows)


async def _submit(
    session: AsyncSession, clock: FakeClock, w: World, actor: str, *, version: int = 1, **kw: Any
) -> Any:
    return await DrApplicationTransitionService.submit_validation(
        session,
        actor_id=getattr(w, actor),
        dr_application_id=w.dr_application_id,
        expected_version=version,
        clock=clock,
        **kw,
    )


async def _validate(
    session: AsyncSession, clock: FakeClock, w: World, actor: str, *, approve: bool, version: int, **kw: Any
) -> Any:
    kw.setdefault("note", None if approve else "not ready")
    return await DrApplicationTransitionService.validate(
        session, actor_id=getattr(w, actor), dr_application_id=w.dr_application_id, expected_version=version,
        approve=approve, clock=clock, **kw,
    )  # fmt: skip


# --------------------------------------------------------------------------------------------
# submit-validation
# --------------------------------------------------------------------------------------------


async def test_submit_moves_recovering_to_technical_validation_and_opens_the_record(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)
    app_id = await _app_in(session, w, RECOVERING)

    await _submit(session, clock, w, "system_owner_id")

    row = await _row(session, app_id)
    assert (row.status, row.version) == (TV, 2)
    [validation] = await _validations(session, app_id)
    assert (validation.status, validation.verification_note, validation.created_by_user_id) == (
        "PENDING", "phase:FAILOVER", w.system_owner_id,
    )  # fmt: skip
    assert await audit_actions(session, app_id) == ["DR_APPLICATION_SUBMITTED"]


async def test_failback_submit_opens_the_record_without_moving_status(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)
    app_id = await _app_in(session, w, FB)

    await _submit(session, clock, w, "coordinator_id")

    row = await _row(session, app_id)
    assert (row.status, row.version) == (FB, 2)
    assert (await _validations(session, app_id))[0].verification_note == "phase:FAILBACK"


@pytest.mark.parametrize("status", [NS, TV, FO, DONE])
async def test_submit_is_illegal_elsewhere(session: AsyncSession, clock: FakeClock, status: str) -> None:
    w = await build_world(session)
    await _app_in(session, w, status)
    with pytest.raises(AppError) as e:
        await _submit(session, clock, w, "admin_id")
    assert (e.value.code, e.value.status_code) == ("INVALID_TRANSITION", 409)
    assert await _validations(session, w.dr_application_id) == []


async def test_a_second_submit_while_one_is_pending_is_409(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)
    await _app_in(session, w, FB)
    await _submit(session, clock, w, "admin_id")
    with pytest.raises(AppError) as e:
        await _submit(session, clock, w, "admin_id", version=2)
    assert e.value.code == "INVALID_TRANSITION"


@pytest.mark.parametrize(
    ("task_phase", "task_status", "app_status", "blocks"),
    [
        ("FAILOVER", "IN_PROGRESS", RECOVERING, True),
        ("FAILOVER", "BLOCKED", RECOVERING, True),
        ("FAILOVER", "COMPLETED", RECOVERING, False),
        ("FAILOVER", "NOT_STARTED", RECOVERING, False),
        ("FAILBACK", "IN_PROGRESS", RECOVERING, False),  # other phase's work doesn't gate failover validation
        ("FAILBACK", "IN_PROGRESS", FB, True),
        ("FAILOVER", "IN_PROGRESS", FB, False),
    ],
)
async def test_unfinished_phase_tasks_block_submit(
    session: AsyncSession, clock: FakeClock, task_phase: str, task_status: str, app_status: str, blocks: bool
) -> None:
    w = await build_world(session)
    await _app_in(session, w, app_status)
    task_id = await seed_task(session, w, status=task_status)
    await session.execute(
        text("UPDATE tasks SET phase = CAST(:p AS task_phase) WHERE id = :t"), {"p": task_phase, "t": task_id}
    )

    if blocks:
        with pytest.raises(AppError) as e:
            await _submit(session, clock, w, "admin_id")
        assert (e.value.code, e.value.status_code) == ("APPLICATION_TASKS_UNFINISHED", 409)
        assert e.value.details["unfinished_task_ids"] == [str(task_id)]
        assert await _validations(session, w.dr_application_id) == []
    else:
        await _submit(session, clock, w, "admin_id")
        assert len(await _validations(session, w.dr_application_id)) == 1


async def test_an_override_with_reason_submits_past_unfinished_tasks_and_is_audited(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)
    await _app_in(session, w, RECOVERING)
    task_id = await seed_task(session, w, status="IN_PROGRESS")

    await _submit(session, clock, w, "coordinator_id", override_reason="  accepted risk: cosmetic step  ")

    assert (await _row(session, w.dr_application_id)).status == TV
    assert await audit_actions(session, w.dr_application_id) == [
        "APPLICATION_VALIDATION_OVERRIDE_RECORDED", "DR_APPLICATION_SUBMITTED",
    ]  # fmt: skip
    override = (
        await session.execute(
            text("SELECT override_type, reason, metadata FROM overrides WHERE target_id = :a"),
            {"a": w.dr_application_id},
        )
    ).one()
    assert (override.override_type, override.reason) == (
        "APPLICATION_VALIDATION_OVERRIDE",
        "accepted risk: cosmetic step",
    )
    assert override.metadata["unfinished_task_ids"] == [str(task_id)]
    with pytest.raises(AppError):  # a blank reason is no reason
        await _app_in(session, w, RECOVERING)
        await session.execute(
            text("DELETE FROM validations WHERE target_id = :a"), {"a": w.dr_application_id}
        )
        await _submit(session, clock, w, "coordinator_id", version=2, override_reason="   ")


# --------------------------------------------------------------------------------------------
# validate
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("from_status", "failback_required", "approve", "to_status"),
    [
        (TV, True, True, FO),
        (TV, False, True, DONE),  # failback not required: straight to COMPLETED (D-208)
        (TV, True, False, RECOVERING),
        (FB, True, True, DONE),
        (FB, True, False, FB),  # a failback rejection keeps the phase; the record is REJECTED
    ],
)
async def test_validate_moves_the_application(
    session: AsyncSession,
    clock: FakeClock,
    from_status: str,
    failback_required: bool,
    approve: bool,
    to_status: str,
) -> None:
    w = await build_world(session)
    await _app_in(session, w, from_status, failback_required=failback_required)
    submit_from = RECOVERING if from_status == TV else FB
    await _app_in(session, w, submit_from, failback_required=failback_required)
    await _submit(session, clock, w, "system_owner_id")  # v2, PENDING record by the system owner

    await _validate(session, clock, w, "coordinator_id", approve=approve, version=2)

    row = await _row(session, w.dr_application_id)
    assert (row.status, row.version) == (to_status, 3)
    [validation] = await _validations(session, w.dr_application_id)
    assert (validation.status, validation.validator_user_id) == (
        "APPROVED" if approve else "REJECTED",
        w.coordinator_id,
    )
    stamped = row.technical_validated_at is not None
    assert stamped is (approve and from_status == TV)  # the RTO clock stops on the failover validation only
    assert await outbox_types(session, w.dr_application_id) == ["ApplicationHealthChanged"]
    actions = await audit_actions(session, w.dr_application_id)
    assert ("DR_APPLICATION_VALIDATED" if approve else "DR_APPLICATION_VALIDATION_REJECTED") in actions


@pytest.mark.parametrize("status", [NS, RECOVERING, FO, DONE])
async def test_validate_is_illegal_elsewhere(session: AsyncSession, clock: FakeClock, status: str) -> None:
    w = await build_world(session)
    await _app_in(session, w, status)
    with pytest.raises(AppError) as e:
        await _validate(session, clock, w, "admin_id", approve=True, version=1)
    assert e.value.code == "INVALID_TRANSITION"


async def test_validate_without_a_pending_record_is_409(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)
    await _app_in(session, w, TV)
    with pytest.raises(AppError) as e:
        await _validate(session, clock, w, "admin_id", approve=True, version=1)
    assert e.value.code == "INVALID_TRANSITION" and "no open Validation" in e.value.message


async def test_rejection_needs_a_note(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)
    await _app_in(session, w, RECOVERING)
    await _submit(session, clock, w, "system_owner_id")
    with pytest.raises(AppError) as e:
        await _validate(session, clock, w, "coordinator_id", approve=False, version=2, note="  ")
    assert (e.value.code, e.value.status_code) == ("REJECTION_NOTE_REQUIRED", 422)
    assert (await _row(session, w.dr_application_id)).status == TV


async def test_the_submitter_may_not_validate_their_own_submission(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)
    await _app_in(session, w, RECOVERING)
    await _submit(session, clock, w, "system_owner_id")
    with pytest.raises(AppError) as e:
        await _validate(session, clock, w, "system_owner_id", approve=True, version=2)
    assert (e.value.code, e.value.status_code) == ("SELF_VALIDATION_FORBIDDEN", 403)


@pytest.mark.parametrize(
    ("actor", "allowed"),
    [
        ("admin_id", True),
        ("coordinator_id", True),
        ("system_owner_id", True),  # secondary slot (D-254: any slot)
        ("business_owner_id", False),  # optional business note only
        ("ws_lead_id", False),
        ("manager_id", False),
        ("executor_id", False),
        ("other_app_owner_id", False),  # owns a different Application
    ],
)
async def test_validation_authority(
    session: AsyncSession, clock: FakeClock, actor: str, allowed: bool
) -> None:
    w = await build_world(session)
    await _app_in(session, w, RECOVERING)
    if allowed:
        await _submit(session, clock, w, actor)
    else:
        with pytest.raises(AppError) as e:
            await _submit(session, clock, w, actor)
        assert (e.value.code, e.value.status_code) == ("AUTHORIZATION_REQUIRED", 403)


async def test_business_note_is_carried_on_the_record_and_metrics_port_is_called(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)
    await _app_in(session, w, RECOVERING)
    await _submit(session, clock, w, "system_owner_id")
    seen: list[tuple[uuid.UUID, datetime]] = []

    class Recording:
        async def on_technical_validation(
            self, s: AsyncSession, app: DrApplication, *, validated_at: datetime
        ) -> None:
            seen.append((app.id, validated_at))

    await _validate(
        session, clock, w, "admin_id", approve=True, version=2, business_confirmed=True,
        business_confirmation_note="  business confirms  ", metrics=Recording(),
    )  # fmt: skip

    [validation] = await _validations(session, w.dr_application_id)
    assert validation.business_confirmed is True
    assert seen == [(w.dr_application_id, clock.now())]


async def test_stale_version_and_invisible_event(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)
    await _app_in(session, w, RECOVERING)
    with pytest.raises(AppError) as e:
        await _submit(session, clock, w, "admin_id", version=9)
    assert e.value.code == "CONCURRENCY_CONFLICT"
    with pytest.raises(AppError) as e:
        await _submit(session, clock, w, "stranger_id")
    assert (e.value.code, e.value.status_code) == ("DR_APPLICATION_NOT_FOUND", 404)
    with pytest.raises(AppError) as e:
        await DrApplicationTransitionService.validate(
            session,
            actor_id=w.admin_id,
            dr_application_id=uuid.uuid4(),
            expected_version=1,
            approve=True,
            clock=clock,
        )
    assert e.value.code == "DR_APPLICATION_NOT_FOUND"

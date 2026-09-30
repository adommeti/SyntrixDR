"""Milestone lifecycle (STATE_MACHINES.md §Milestone, ADR-009, D-225):
`NOT_STARTED -> IN_PROGRESS/AT_RISK -> READY_FOR_CONFIRMATION -> ACHIEVED | MISSED`.

Progression up to READY_FOR_CONFIRMATION is derived from the contributing Tasks (`milestone_tasks`);
ACHIEVED is a human `confirm` (MANUAL, the default) or, only when the Milestone is AUTOMATIC *and*
`milestone.auto_confirm_allowed` is true, automatic on reaching READY_FOR_CONFIRMATION; MISSED is the
target-time sweep. ACHIEVED and MISSED are terminal."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import FakeClock
from app.core.errors import AppError
from app.milestones.transition_service import MilestoneTransitionService, derive_target
from app.policies_admin.commands import set_policy_value
from tests.factories import (
    World,
    audit_actions,
    build_world,
    grant_role,
    make_user,
    outbox_types,
    seed_contribution,
    seed_milestone,
    seed_task,
)

pytestmark = [pytest.mark.transitions]

NS, IP, AR, RFC, ACH, MIS = (
    "NOT_STARTED",
    "IN_PROGRESS",
    "AT_RISK",
    "READY_FOR_CONFIRMATION",
    "ACHIEVED",
    "MISSED",
)
ALL = (NS, IP, AR, RFC, ACH, MIS)

# --------------------------------------------------------------------------------------------
# The derived rule, as a table: (current, contributors [(task status, required)]) -> target
# --------------------------------------------------------------------------------------------

DERIVED: list[tuple[str, list[tuple[str, bool]], str]] = [
    (NS, [("NOT_STARTED", True)], NS),
    (NS, [("IN_PROGRESS", True)], IP),
    (NS, [("NOT_STARTED", True), ("IN_PROGRESS", False)], IP),  # any contributor starting starts it
    (NS, [("BLOCKED", True)], AR),
    (IP, [("BLOCKED", True), ("IN_PROGRESS", True)], AR),
    (IP, [("BLOCKED", False), ("IN_PROGRESS", True)], IP),  # an optional Task never puts it at risk
    (AR, [("IN_PROGRESS", True)], IP),  # back on track once nothing required is BLOCKED
    (IP, [("COMPLETED", True), ("COMPLETED", True)], RFC),
    (IP, [("COMPLETED", True), ("IN_PROGRESS", False)], RFC),  # optional work never holds it
    (AR, [("COMPLETED", True), ("BLOCKED", False)], RFC),
    (NS, [("COMPLETED", True)], RFC),
    (IP, [("COMPLETED", True), ("CANCELLED", True)], IP),  # a CANCELLED required Task isn't done
    (IP, [("COMPLETED", True), ("READY_FOR_VALIDATION", True)], IP),  # submitted isn't completed
    (NS, [], RFC),  # nothing required: only the human confirmation remains
    (NS, [("IN_PROGRESS", False)], RFC),
    (RFC, [("COMPLETED", True)], RFC),
    (ACH, [("BLOCKED", True)], ACH),  # terminal states never move
    (MIS, [("COMPLETED", True)], MIS),
]


@pytest.mark.parametrize(("current", "contributors", "target"), DERIVED)
def test_derived_target(current: str, contributors: list[tuple[str, bool]], target: str) -> None:
    assert derive_target(current, contributors) == target


def test_derived_rule_never_moves_backwards() -> None:
    """RFC is only left by confirm/auto-confirm/miss; nothing re-derives it down."""
    for contributors in ([("NOT_STARTED", True)], [("BLOCKED", True)], [("IN_PROGRESS", True)]):
        assert derive_target(RFC, contributors) == RFC


# --------------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------------


async def _row(session: AsyncSession, milestone_id: uuid.UUID) -> Any:
    return (
        await session.execute(
            text(
                "SELECT status, version, ready_for_confirmation_at, achieved_at FROM milestones "
                "WHERE id = :m"
            ),
            {"m": milestone_id},
        )
    ).one()


async def _audit(session: AsyncSession, milestone_id: uuid.UUID) -> list[Any]:
    rows = await session.execute(
        text(
            "SELECT action, actor_user_id, actor_type, dr_event_id, before_data, after_data "
            "FROM audit_events WHERE entity_id = :m AND entity_type = 'MILESTONE' "
            "ORDER BY occurred_at, action"
        ),
        {"m": milestone_id},
    )
    return list(rows.all())


async def _with_contributors(
    session: AsyncSession, w: World, statuses: list[tuple[str, bool]], **milestone: Any
) -> uuid.UUID:
    milestone_id = await seed_milestone(session, w, **milestone)
    for status, required in statuses:
        task_id = await seed_task(session, w, status=status)
        await seed_contribution(session, milestone_id=milestone_id, task_id=task_id, required=required)
    return milestone_id


async def _allow_auto_confirm(session: AsyncSession, w: World, clock: FakeClock) -> None:
    await set_policy_value(
        session,
        actor_id=w.admin_id,
        key="milestone.auto_confirm_allowed",
        value=True,
        scope_type="DR_EVENT",
        scope_id=w.event_id,
        clock=clock,
    )


# --------------------------------------------------------------------------------------------
# recompute: derived progression, audit + outbox
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("statuses", "to_state", "action"),
    [
        ([("IN_PROGRESS", True)], IP, "MILESTONE_STARTED"),
        ([("BLOCKED", True)], AR, "MILESTONE_AT_RISK"),
        ([("COMPLETED", True)], RFC, "MILESTONE_READY_FOR_CONFIRMATION"),
    ],
)
async def test_recompute_moves_and_writes_one_audit_and_one_outbox_row(
    session: AsyncSession, clock: FakeClock, statuses: list[tuple[str, bool]], to_state: str, action: str
) -> None:
    w = await build_world(session)
    milestone_id = await _with_contributors(session, w, statuses)

    await MilestoneTransitionService.recompute(
        session, milestone_id=milestone_id, actor_id=w.executor_id, clock=clock
    )

    row = await _row(session, milestone_id)
    assert (row.status, row.version) == (to_state, 2)
    assert (row.ready_for_confirmation_at is not None) == (to_state == RFC)
    [audit] = await _audit(session, milestone_id)
    assert (audit.action, audit.actor_user_id, audit.dr_event_id) == (action, w.executor_id, w.event_id)
    assert (audit.before_data["status"], audit.after_data["status"]) == (NS, to_state)
    assert await outbox_types(session, milestone_id) == ["MilestoneChanged"]


async def test_back_on_track_after_the_blocked_task_resumes(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)
    milestone_id = await _with_contributors(session, w, [("IN_PROGRESS", True)], status=AR)

    await MilestoneTransitionService.recompute(
        session, milestone_id=milestone_id, actor_id=w.admin_id, clock=clock
    )

    assert (await _row(session, milestone_id)).status == IP
    assert await audit_actions(session, milestone_id) == ["MILESTONE_BACK_ON_TRACK"]


async def test_recompute_with_nothing_to_change_writes_nothing(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)
    milestone_id = await _with_contributors(session, w, [("IN_PROGRESS", True)], status=IP)

    await MilestoneTransitionService.recompute(
        session, milestone_id=milestone_id, actor_id=w.admin_id, clock=clock
    )

    assert (await _row(session, milestone_id)).version == 1
    assert await audit_actions(session, milestone_id) == []
    assert await outbox_types(session, milestone_id) == []


async def test_soft_deleted_contributors_are_ignored(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)
    milestone_id = await _with_contributors(session, w, [("COMPLETED", True)])
    gone = await seed_task(session, w, status="BLOCKED")
    await seed_contribution(session, milestone_id=milestone_id, task_id=gone)
    await session.execute(text("UPDATE tasks SET deleted_at = now() WHERE id = :t"), {"t": gone})

    await MilestoneTransitionService.recompute(
        session, milestone_id=milestone_id, actor_id=w.admin_id, clock=clock
    )

    assert (await _row(session, milestone_id)).status == RFC


# --------------------------------------------------------------------------------------------
# D-225 auto-confirm
# --------------------------------------------------------------------------------------------


async def test_manual_milestones_never_auto_achieve(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)
    await _allow_auto_confirm(session, w, clock)
    milestone_id = await _with_contributors(session, w, [("COMPLETED", True)])

    await MilestoneTransitionService.recompute(
        session, milestone_id=milestone_id, actor_id=w.admin_id, clock=clock
    )

    assert (await _row(session, milestone_id)).status == RFC


async def test_automatic_milestones_wait_for_a_human_while_the_policy_is_off(
    session: AsyncSession, clock: FakeClock
) -> None:
    """`milestone.auto_confirm_allowed=false` is the default (D-225)."""
    w = await build_world(session)
    milestone_id = await _with_contributors(session, w, [("COMPLETED", True)], confirmation_mode="AUTOMATIC")

    await MilestoneTransitionService.recompute(
        session, milestone_id=milestone_id, actor_id=w.admin_id, clock=clock
    )

    assert (await _row(session, milestone_id)).status == RFC


async def test_automatic_milestone_achieves_itself_when_the_policy_allows(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)
    await _allow_auto_confirm(session, w, clock)
    milestone_id = await _with_contributors(session, w, [("COMPLETED", True)], confirmation_mode="AUTOMATIC")

    await MilestoneTransitionService.recompute(
        session, milestone_id=milestone_id, actor_id=w.executor_id, clock=clock
    )

    row = await _row(session, milestone_id)
    assert (row.status, row.version) == (ACH, 3)
    assert row.achieved_at is not None
    actions = [a.action for a in await _audit(session, milestone_id)]
    assert actions == ["MILESTONE_ACHIEVED", "MILESTONE_READY_FOR_CONFIRMATION"] or actions == [
        "MILESTONE_READY_FOR_CONFIRMATION",
        "MILESTONE_ACHIEVED",
    ]
    achieved = next(a for a in await _audit(session, milestone_id) if a.action == "MILESTONE_ACHIEVED")
    assert achieved.after_data["auto_confirmed"] is True


async def test_automatic_milestone_with_nothing_required_still_needs_a_human(
    session: AsyncSession, clock: FakeClock
) -> None:
    """Vacuous readiness never auto-achieves: an empty Milestone must be confirmed by someone."""
    w = await build_world(session)
    await _allow_auto_confirm(session, w, clock)
    milestone_id = await _with_contributors(session, w, [], confirmation_mode="AUTOMATIC")

    await MilestoneTransitionService.recompute(
        session, milestone_id=milestone_id, actor_id=w.admin_id, clock=clock
    )

    assert (await _row(session, milestone_id)).status == RFC


# --------------------------------------------------------------------------------------------
# confirm
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("actor", ["admin_id", "coordinator_id", "ws_lead_id"])
async def test_confirm_achieves_a_ready_milestone(
    session: AsyncSession, clock: FakeClock, actor: str
) -> None:
    w = await build_world(session)
    milestone_id = await seed_milestone(session, w, status=RFC)

    await MilestoneTransitionService.confirm(
        session, actor_id=getattr(w, actor), milestone_id=milestone_id, expected_version=1, clock=clock
    )

    row = await _row(session, milestone_id)
    assert (row.status, row.version) == (ACH, 2)
    assert row.achieved_at == clock.now()
    [audit] = await _audit(session, milestone_id)
    assert audit.action == "MILESTONE_ACHIEVED"
    assert audit.after_data["auto_confirmed"] is False
    assert audit.actor_user_id == getattr(w, actor)
    assert await outbox_types(session, milestone_id) == ["MilestoneChanged"]


async def test_the_designated_lead_confirms_their_streams_milestone(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)
    lead = await make_user(session, "Designated lead")
    await session.execute(
        text("UPDATE work_streams SET lead_user_id = :u WHERE id = :ws"),
        {"u": lead, "ws": w.other_work_stream_id},
    )
    await session.execute(
        text(
            "INSERT INTO dr_event_participants (id, dr_event_id, user_id, source) "
            "VALUES (:id, :e, :u, 'WORK_STREAM_LEAD')"
        ),
        {"id": uuid.uuid4(), "e": w.event_id, "u": lead},
    )
    milestone_id = await seed_milestone(session, w, status=RFC, work_stream_id=w.other_work_stream_id)

    await MilestoneTransitionService.confirm(
        session, actor_id=lead, milestone_id=milestone_id, expected_version=1, clock=clock
    )

    assert (await _row(session, milestone_id)).status == ACH


@pytest.mark.parametrize("actor", ["executor_id", "manager_id", "system_owner_id", "business_owner_id"])
async def test_roles_outside_the_confirm_row_get_403(
    session: AsyncSession, clock: FakeClock, actor: str
) -> None:
    """RBAC_MATRIX.md "Confirm shared Milestone": Admin, Coordinator, Work Stream Lead only."""
    w = await build_world(session)
    milestone_id = await seed_milestone(session, w, status=RFC)

    with pytest.raises(AppError) as exc:
        await MilestoneTransitionService.confirm(
            session, actor_id=getattr(w, actor), milestone_id=milestone_id, expected_version=1, clock=clock
        )

    assert exc.value.status_code == 403
    assert (await _row(session, milestone_id)).status == RFC


async def test_a_lead_of_another_stream_cannot_confirm(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)
    milestone_id = await seed_milestone(session, w, status=RFC, work_stream_id=w.other_work_stream_id)

    with pytest.raises(AppError) as exc:
        await MilestoneTransitionService.confirm(
            session, actor_id=w.ws_lead_id, milestone_id=milestone_id, expected_version=1, clock=clock
        )

    assert exc.value.status_code == 403


async def test_a_lead_cannot_confirm_an_application_milestone(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)
    milestone_id = await seed_milestone(session, w, status=RFC, app_scoped=True)

    with pytest.raises(AppError) as exc:
        await MilestoneTransitionService.confirm(
            session, actor_id=w.ws_lead_id, milestone_id=milestone_id, expected_version=1, clock=clock
        )

    assert exc.value.status_code == 403


async def test_a_lead_role_scoped_elsewhere_confers_nothing(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)
    elsewhere = await make_user(session, "Lead elsewhere")
    await grant_role(session, elsewhere, "WORK_STREAM_LEAD", scope_type="WORK_STREAM", scope_id=uuid.uuid4())
    await session.execute(
        text(
            "INSERT INTO dr_event_participants (id, dr_event_id, user_id, source) "
            "VALUES (:id, :e, :u, 'EXPLICIT')"
        ),
        {"id": uuid.uuid4(), "e": w.event_id, "u": elsewhere},
    )
    milestone_id = await seed_milestone(session, w, status=RFC)

    with pytest.raises(AppError) as exc:
        await MilestoneTransitionService.confirm(
            session, actor_id=elsewhere, milestone_id=milestone_id, expected_version=1, clock=clock
        )

    assert exc.value.status_code == 403


async def test_confirm_in_an_event_the_actor_cannot_see_is_404(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)
    milestone_id = await seed_milestone(session, w, status=RFC)

    for missing in (milestone_id, uuid.uuid4()):
        with pytest.raises(AppError) as exc:
            await MilestoneTransitionService.confirm(
                session, actor_id=w.stranger_id, milestone_id=missing, expected_version=1, clock=clock
            )
        assert (exc.value.status_code, exc.value.code) == (404, "MILESTONE_NOT_FOUND")


async def test_confirm_with_a_stale_version_is_409(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)
    milestone_id = await seed_milestone(session, w, status=RFC)

    with pytest.raises(AppError) as exc:
        await MilestoneTransitionService.confirm(
            session, actor_id=w.admin_id, milestone_id=milestone_id, expected_version=4, clock=clock
        )

    assert exc.value.code == "CONCURRENCY_CONFLICT"
    assert (await _row(session, milestone_id)).status == RFC


@pytest.mark.parametrize("from_state", [s for s in ALL if s != RFC])
async def test_confirm_is_legal_only_from_ready_for_confirmation(
    session: AsyncSession, clock: FakeClock, from_state: str
) -> None:
    w = await build_world(session)
    milestone_id = await seed_milestone(session, w, status=from_state)

    with pytest.raises(AppError) as exc:
        await MilestoneTransitionService.confirm(
            session, actor_id=w.admin_id, milestone_id=milestone_id, expected_version=1, clock=clock
        )

    assert (exc.value.status_code, exc.value.code) == (409, "INVALID_TRANSITION")
    assert (await _row(session, milestone_id)).status == from_state
    assert await audit_actions(session, milestone_id) == []


# --------------------------------------------------------------------------------------------
# miss (system)
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("from_state", [NS, IP, AR, RFC])
async def test_miss_from_every_non_terminal_state(
    session: AsyncSession, clock: FakeClock, from_state: str
) -> None:
    w = await build_world(session)
    milestone_id = await seed_milestone(
        session, w, status=from_state, target_at=clock.now() - timedelta(minutes=1)
    )

    await MilestoneTransitionService.miss(session, milestone_id=milestone_id, clock=clock)

    assert (await _row(session, milestone_id)).status == MIS
    [audit] = await _audit(session, milestone_id)
    assert (audit.action, audit.actor_user_id, audit.actor_type) == ("MILESTONE_MISSED", None, "SYSTEM")
    assert await outbox_types(session, milestone_id) == ["MilestoneChanged"]


@pytest.mark.parametrize("from_state", [ACH, MIS])
async def test_terminal_milestones_are_never_missed(
    session: AsyncSession, clock: FakeClock, from_state: str
) -> None:
    w = await build_world(session)
    milestone_id = await seed_milestone(
        session, w, status=from_state, target_at=clock.now() - timedelta(minutes=1)
    )

    with pytest.raises(AppError) as exc:
        await MilestoneTransitionService.miss(session, milestone_id=milestone_id, clock=clock)

    assert exc.value.code == "INVALID_TRANSITION"
    assert (await _row(session, milestone_id)).status == from_state

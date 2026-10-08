"""Blocker lifecycle (STATE_MACHINES.md §Blocker, D-211, D-252):
`OPEN -> ASSIGNED -> IN_PROGRESS -> RESOLVED -> VERIFIED -> CLOSED`.

Four commands drive six states. `verify` writes VERIFIED then CLOSED in one transaction with two audit
rows; when it closes the Task's last active Blocker the Task returns BLOCKED -> IN_PROGRESS in that same
transaction (I-1) and never further. No command touches the Task's Owning Team."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.blockers.transition_service import BlockerTransitionService
from app.core.clock import FakeClock
from app.core.errors import AppError
from tests.factories import (
    World,
    add_member,
    audit_actions,
    blocker_ids_of,
    blocker_row,
    build_world,
    grant_role,
    make_team,
    make_user,
    outbox_types,
    seed_blocker,
    seed_task,
    task_row,
)

pytestmark = [pytest.mark.transitions]

OPEN, ASSIGNED, IN_PROGRESS, RESOLVED, VERIFIED, CLOSED = (
    "OPEN",
    "ASSIGNED",
    "IN_PROGRESS",
    "RESOLVED",
    "VERIFIED",
    "CLOSED",
)
ALL = (OPEN, ASSIGNED, IN_PROGRESS, RESOLVED, VERIFIED, CLOSED)
LEGAL_FROM = {
    "assign": {OPEN, ASSIGNED},  # route, or re-route before work starts
    "start": {ASSIGNED},
    "resolve": {IN_PROGRESS},
    "verify": {RESOLVED},
}
COMMANDS = tuple(LEGAL_FROM)


async def _run(
    session: AsyncSession,
    clock: FakeClock,
    command: str,
    *,
    actor_id: uuid.UUID,
    blocker_id: uuid.UUID,
    version: int = 1,
    **kwargs: Any,
) -> Any:
    method = getattr(BlockerTransitionService, command)
    return await method(
        session, actor_id=actor_id, blocker_id=blocker_id, expected_version=version, clock=clock, **kwargs
    )


async def _blocked_task(
    session: AsyncSession, w: World, *, status: str = OPEN, **blocker: Any
) -> tuple[uuid.UUID, uuid.UUID]:
    """A BLOCKED Task carrying one Blocker in `status` (the seeded OPEN one is replaced)."""
    task_id = await seed_task(session, w, status="BLOCKED")
    await session.execute(text("DELETE FROM blockers WHERE task_id = :t"), {"t": task_id})
    return task_id, await seed_blocker(session, w, task_id, status=status, **blocker)


def _kwargs(command: str, w: World) -> dict[str, Any]:
    return {"assignee_user_id": w.teammate_id} if command == "assign" else {}


async def _audits(session: AsyncSession, entity_id: uuid.UUID) -> list[Any]:
    rows = await session.execute(
        text(
            "SELECT action, actor_user_id, before_data, after_data FROM audit_events "
            "WHERE entity_id = :id ORDER BY (after_data->>'version')::int"
        ),
        {"id": entity_id},
    )
    return list(rows)


# --------------------------------------------------------------------------------------------
# Transition table
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "from_state", "to_state"),
    [
        ("assign", OPEN, ASSIGNED),
        ("assign", ASSIGNED, ASSIGNED),
        ("start", ASSIGNED, IN_PROGRESS),
        ("resolve", IN_PROGRESS, RESOLVED),
        ("verify", RESOLVED, CLOSED),
    ],
)
async def test_legal_transition(
    session: AsyncSession, clock: FakeClock, command: str, from_state: str, to_state: str
) -> None:
    w = await build_world(session)
    task_id, blocker_id = await _blocked_task(session, w, status=from_state, owner_id=w.teammate_id)
    # Another active Blocker keeps the Task BLOCKED after verify, so this table tests the Blocker alone.
    await seed_blocker(session, w, task_id)

    await _run(
        session, clock, command, actor_id=w.coordinator_id, blocker_id=blocker_id, **_kwargs(command, w)
    )

    row = await blocker_row(session, blocker_id)
    assert row.status == to_state
    assert row.version == (3 if command == "verify" else 2)
    assert (await task_row(session, task_id)).status == "BLOCKED"


@pytest.mark.parametrize(
    ("command", "from_state"),
    [(c, s) for c in COMMANDS for s in ALL if s not in LEGAL_FROM[c]],
)
async def test_illegal_transition_is_rejected_and_nothing_changes(
    session: AsyncSession, clock: FakeClock, command: str, from_state: str
) -> None:
    w = await build_world(session)
    task_id, blocker_id = await _blocked_task(session, w, status=from_state, owner_id=w.teammate_id)

    with pytest.raises(AppError) as e:
        await _run(
            session, clock, command, actor_id=w.coordinator_id, blocker_id=blocker_id, **_kwargs(command, w)
        )

    assert (e.value.code, e.value.status_code) == ("INVALID_TRANSITION", 409)
    row = await blocker_row(session, blocker_id)
    assert (row.status, row.version) == (from_state, 1)
    assert await audit_actions(session, blocker_id) == []
    assert await outbox_types(session, blocker_id) == []
    assert (await task_row(session, task_id)).status == "BLOCKED"


@pytest.mark.parametrize("command", COMMANDS)
async def test_stale_version_is_409_and_writes_nothing(
    session: AsyncSession, clock: FakeClock, command: str
) -> None:
    w = await build_world(session)
    legal = next(iter(LEGAL_FROM[command]))
    _, blocker_id = await _blocked_task(session, w, status=legal, owner_id=w.teammate_id)

    with pytest.raises(AppError) as e:
        await _run(
            session,
            clock,
            command,
            actor_id=w.coordinator_id,
            blocker_id=blocker_id,
            version=9,
            **_kwargs(command, w),
        )

    assert e.value.code == "CONCURRENCY_CONFLICT"
    assert (await blocker_row(session, blocker_id)).version == 1
    assert await audit_actions(session, blocker_id) == []


@pytest.mark.parametrize("command", COMMANDS)
async def test_invisible_event_is_404(session: AsyncSession, clock: FakeClock, command: str) -> None:
    w = await build_world(session)
    legal = next(iter(LEGAL_FROM[command]))
    _, blocker_id = await _blocked_task(session, w, status=legal, owner_id=w.teammate_id)

    with pytest.raises(AppError) as e:
        await _run(
            session, clock, command, actor_id=w.stranger_id, blocker_id=blocker_id, **_kwargs(command, w)
        )
    assert (e.value.code, e.value.status_code) == ("BLOCKER_NOT_FOUND", 404)

    with pytest.raises(AppError) as e:
        await _run(
            session, clock, command, actor_id=w.coordinator_id, blocker_id=uuid.uuid4(), **_kwargs(command, w)
        )
    assert e.value.code == "BLOCKER_NOT_FOUND"


# --------------------------------------------------------------------------------------------
# D-211: verify is VERIFIED then CLOSED, atomically, both audited; Owning Team never changes
# --------------------------------------------------------------------------------------------


async def test_verify_writes_verified_then_closed_atomically_with_two_audits(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)
    task_id, blocker_id = await _blocked_task(session, w, status=RESOLVED, owner_id=w.teammate_id)
    await seed_blocker(session, w, task_id)  # keeps the Task BLOCKED

    await _run(session, clock, "verify", actor_id=w.coordinator_id, blocker_id=blocker_id)

    row = await blocker_row(session, blocker_id)
    assert (row.status, row.version) == (CLOSED, 3)
    assert row.verified_at == clock.now() and row.closed_at == clock.now()
    audits = await _audits(session, blocker_id)
    assert [(a.action, a.before_data["status"], a.after_data["status"]) for a in audits] == [
        ("BLOCKER_VERIFIED", RESOLVED, VERIFIED),
        ("BLOCKER_CLOSED", VERIFIED, CLOSED),
    ]
    assert [a.after_data["version"] for a in audits] == [2, 3]
    assert all(a.actor_user_id == w.coordinator_id for a in audits)
    assert await outbox_types(session, blocker_id) == ["BlockerChanged", "BlockerChanged"]


async def test_verify_on_the_last_active_blocker_resumes_the_task_in_the_same_transaction(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)
    task_id, blocker_id = await _blocked_task(session, w, status=RESOLVED, owner_id=w.teammate_id)
    await seed_blocker(session, w, task_id, status=CLOSED)  # an already-closed one doesn't count
    before = await task_row(session, task_id)

    await _run(session, clock, "verify", actor_id=w.coordinator_id, blocker_id=blocker_id)

    task = await task_row(session, task_id)
    assert (task.status, task.version) == ("IN_PROGRESS", before.version + 1)  # never READY_FOR_VALIDATION
    resumed = [a for a in await _audits(session, task_id) if a.action == "TASK_RESUMED"]
    assert len(resumed) == 1
    assert resumed[0].after_data["blocker_id"] == str(blocker_id)
    assert resumed[0].actor_user_id == w.coordinator_id
    assert "TaskChanged" in await outbox_types(session, task_id)


async def test_verify_leaving_another_active_blocker_does_not_touch_the_task(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)
    task_id, blocker_id = await _blocked_task(session, w, status=RESOLVED, owner_id=w.teammate_id)
    other = await seed_blocker(session, w, task_id, status=IN_PROGRESS)
    before = await task_row(session, task_id)

    await _run(session, clock, "verify", actor_id=w.coordinator_id, blocker_id=blocker_id)

    task = await task_row(session, task_id)
    assert (task.status, task.version) == ("BLOCKED", before.version)
    assert "TASK_RESUMED" not in await audit_actions(session, task_id)
    assert (await blocker_row(session, other)).status == IN_PROGRESS


async def test_verify_never_moves_a_task_that_is_not_blocked(session: AsyncSession, clock: FakeClock) -> None:
    """A Task already resumed by the manual override path (I-1) stays where it is."""
    w = await build_world(session)
    task_id = await seed_task(session, w, status="IN_PROGRESS")
    blocker_id = await seed_blocker(session, w, task_id, status=RESOLVED, owner_id=w.teammate_id)

    await _run(session, clock, "verify", actor_id=w.coordinator_id, blocker_id=blocker_id)

    assert (await task_row(session, task_id)).status == "IN_PROGRESS"
    assert (await blocker_row(session, blocker_id)).status == CLOSED
    assert "TASK_RESUMED" not in await audit_actions(session, task_id)


@pytest.mark.parametrize("command", COMMANDS)
async def test_no_command_changes_the_owning_team(
    session: AsyncSession, clock: FakeClock, command: str
) -> None:
    """D-211: routing a Blocker to another Team is not a change of accountability."""
    w = await build_world(session)
    legal = next(iter(LEGAL_FROM[command]))
    task_id, blocker_id = await _blocked_task(
        session, w, status=legal, owner_id=w.outsider_id, team_id=w.other_team_id
    )
    kwargs: dict[str, Any] = (
        {"team_id": w.other_team_id, "assignee_user_id": w.outsider_id} if command == "assign" else {}
    )

    await _run(session, clock, command, actor_id=w.coordinator_id, blocker_id=blocker_id, **kwargs)

    owning = (
        await session.execute(text("SELECT owning_team_id FROM tasks WHERE id = :t"), {"t": task_id})
    ).scalar_one()
    assert owning == w.team_id


# --------------------------------------------------------------------------------------------
# assign: Team-queue routing and claiming (FROZEN §108: one queue per Team); D-222 enrolment
# --------------------------------------------------------------------------------------------


async def test_assign_routes_to_a_team_queue_without_an_owner(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)
    _, blocker_id = await _blocked_task(session, w)

    await _run(
        session, clock, "assign", actor_id=w.coordinator_id, blocker_id=blocker_id, team_id=w.other_team_id
    )

    row = await blocker_row(session, blocker_id)
    assert (row.status, row.blocker_team_id, row.blocker_owner_user_id) == (ASSIGNED, w.other_team_id, None)
    assert await audit_actions(session, blocker_id) == ["BLOCKER_ASSIGNED"]
    assert await outbox_types(session, blocker_id) == ["BlockerChanged"]


async def test_assign_to_a_resolver_enrols_them_as_blocker_owner(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)
    _, blocker_id = await _blocked_task(session, w)
    assert not await _is_participant(session, w, w.stranger_id)
    await add_member(session, w.other_team_id, w.stranger_id)

    await _run(
        session, clock, "assign", actor_id=w.coordinator_id, blocker_id=blocker_id,
        team_id=w.other_team_id, assignee_user_id=w.stranger_id,
    )  # fmt: skip

    row = await blocker_row(session, blocker_id)
    assert (row.blocker_team_id, row.blocker_owner_user_id) == (w.other_team_id, w.stranger_id)
    assert await _is_participant(session, w, w.stranger_id, source="BLOCKER_OWNER")


async def test_assign_requires_a_team_or_a_resolver(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)
    _, blocker_id = await _blocked_task(session, w)

    with pytest.raises(AppError) as e:
        await _run(session, clock, "assign", actor_id=w.coordinator_id, blocker_id=blocker_id)

    assert (e.value.code, e.value.status_code) == ("BLOCKER_ROUTING_REQUIRED", 422)
    assert (await blocker_row(session, blocker_id)).status == OPEN


async def test_assign_rejects_a_resolver_outside_the_target_team(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)
    _, blocker_id = await _blocked_task(session, w)

    with pytest.raises(AppError) as e:
        await _run(
            session, clock, "assign", actor_id=w.coordinator_id, blocker_id=blocker_id,
            team_id=w.other_team_id, assignee_user_id=w.teammate_id,
        )  # fmt: skip

    assert (e.value.code, e.value.status_code) == ("BLOCKER_ROUTING_MISMATCH", 422)


async def test_assign_rejects_an_inactive_or_unknown_resolver(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)
    _, blocker_id = await _blocked_task(session, w)

    with pytest.raises(AppError) as e:
        await _run(
            session,
            clock,
            "assign",
            actor_id=w.coordinator_id,
            blocker_id=blocker_id,
            assignee_user_id=uuid.uuid4(),
        )
    assert (e.value.code, e.value.status_code) == ("USER_NOT_FOUND", 404)


async def test_reassign_moves_the_queue_before_work_starts(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)
    _, blocker_id = await _blocked_task(
        session, w, status=ASSIGNED, team_id=w.team_id, owner_id=w.teammate_id
    )

    await _run(
        session, clock, "assign", actor_id=w.coordinator_id, blocker_id=blocker_id, team_id=w.other_team_id
    )

    row = await blocker_row(session, blocker_id)
    assert (row.status, row.blocker_team_id, row.blocker_owner_user_id) == (ASSIGNED, w.other_team_id, None)


async def test_start_claims_an_unowned_blocker_for_the_actor(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)
    _, blocker_id = await _blocked_task(session, w, status=ASSIGNED, team_id=w.team_id)

    await _run(session, clock, "start", actor_id=w.teammate_id, blocker_id=blocker_id)

    row = await blocker_row(session, blocker_id)
    assert (row.status, row.blocker_owner_user_id, row.claimed_at) == (
        IN_PROGRESS,
        w.teammate_id,
        clock.now(),
    )
    assert await _is_participant(session, w, w.teammate_id, source="BLOCKER_OWNER")


async def test_resolve_records_the_note_and_time(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)
    _, blocker_id = await _blocked_task(session, w, status=IN_PROGRESS, owner_id=w.teammate_id)

    await _run(
        session, clock, "resolve", actor_id=w.teammate_id, blocker_id=blocker_id, resolution_note="  fixed  "
    )

    row = await blocker_row(session, blocker_id)
    assert (row.status, row.resolution_note, row.resolved_at) == (RESOLVED, "fixed", clock.now())


async def _is_participant(
    session: AsyncSession, w: World, user_id: uuid.UUID, *, source: str | None = None
) -> bool:
    sql = (
        "SELECT count(*) FROM dr_event_participants "
        "WHERE dr_event_id = :e AND user_id = :u AND removed_at IS NULL"
    )
    params: dict[str, Any] = {"e": w.event_id, "u": user_id}
    if source is not None:
        sql += " AND source = :s"
        params["s"] = source
    return (await session.execute(text(sql), params)).scalar_one() > 0


# --------------------------------------------------------------------------------------------
# Authority (RBAC_MATRIX.md:21-22): role half + Manager/Executor domain halves
# --------------------------------------------------------------------------------------------

#: (actor, command, blocker placement, expected): placement = who owns / which Team queue.
OWN = "own"  # Blocker routed to the Task's Owning Team, owned by the Executor under test
OTHER = "other"  # Blocker routed to the other Team, owned by the outsider
AUTHORITY: list[tuple[str, str, str, bool]] = [
    # Admin / Coordinator: everything
    ("admin_id", "assign", OTHER, True),
    ("coordinator_id", "verify", OTHER, True),
    # Work Stream Lead: create/resolve/verify ✓ (unscoped); start ✓ scope (Task in their stream)
    ("ws_lead_id", "resolve", OTHER, True),
    ("ws_lead_id", "verify", OTHER, True),
    ("ws_lead_id", "start", OTHER, True),
    # App/System Owner: same shape as the Lead, scoped to the Task's Application
    ("system_owner_id", "assign", OTHER, True),
    ("system_owner_id", "start", OTHER, True),
    ("system_owner_id", "verify", OTHER, True),
    # Business Owner: comment only
    ("business_owner_id", "assign", OWN, False),
    ("business_owner_id", "verify", OWN, False),
    # Manager: scope = their Team is the Owning Team or the Blocker's queue
    ("manager_id", "assign", OTHER, True),  # Owning Team's Manager routes it
    ("manager_id", "resolve", OWN, True),
    ("manager_id", "verify", OWN, True),
    ("manager_id", "verify", OTHER, True),  # verify is the Owning Team's; the Manager leads it
    ("manager_id", "start", OWN, True),
    # Executor: eligible = assignee / Owning Team member / Blocker owner; start = the assigned resolver
    ("executor_id", "resolve", OWN, True),
    ("executor_id", "verify", OWN, True),
    ("executor_id", "start", OWN, True),
    ("executor_id", "resolve", OTHER, False),  # not the resolver, not on that queue
    ("executor_id", "start", OTHER, False),
    ("outsider_id", "verify", OTHER, False),  # resolver's Team doesn't verify; the Owning Team does
    ("outsider_id", "resolve", OTHER, True),  # the assigned resolver resolves
    ("outsider_id", "start", OTHER, True),
    ("teammate_id", "verify", OTHER, True),  # Owning Team member verifies
    ("roleless_member_id", "verify", OWN, False),  # membership without the EXECUTOR role is nothing
]


@pytest.mark.parametrize(("actor", "command", "placement", "allowed"), AUTHORITY)
async def test_blocker_authority(
    session: AsyncSession, clock: FakeClock, actor: str, command: str, placement: str, allowed: bool
) -> None:
    w = await build_world(session)
    legal = next(iter(LEGAL_FROM[command]))
    team_id, owner_id = (w.team_id, w.executor_id) if placement == OWN else (w.other_team_id, w.outsider_id)
    _, blocker_id = await _blocked_task(session, w, status=legal, team_id=team_id, owner_id=owner_id)
    kwargs: dict[str, Any] = {"team_id": team_id, "assignee_user_id": owner_id} if command == "assign" else {}

    if allowed:
        await _run(session, clock, command, actor_id=getattr(w, actor), blocker_id=blocker_id, **kwargs)
        assert (await blocker_row(session, blocker_id)).version >= 2
    else:
        with pytest.raises(AppError) as e:
            await _run(session, clock, command, actor_id=getattr(w, actor), blocker_id=blocker_id, **kwargs)
        assert (e.value.code, e.value.status_code) == ("AUTHORIZATION_REQUIRED", 403)
        assert await audit_actions(session, blocker_id) == []


async def test_an_executor_may_only_claim_for_themselves(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)
    _, blocker_id = await _blocked_task(session, w)

    with pytest.raises(AppError) as e:
        await _run(
            session,
            clock,
            "assign",
            actor_id=w.executor_id,
            blocker_id=blocker_id,
            assignee_user_id=w.teammate_id,
        )
    assert e.value.code == "AUTHORIZATION_REQUIRED"

    await _run(
        session,
        clock,
        "assign",
        actor_id=w.executor_id,
        blocker_id=blocker_id,
        assignee_user_id=w.executor_id,
    )
    assert (await blocker_row(session, blocker_id)).blocker_owner_user_id == w.executor_id


async def test_a_manager_of_another_team_cannot_touch_a_blocker_outside_their_queue(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)
    other_manager = await make_user(session, "Other manager")
    await grant_role(session, other_manager, "MANAGER")
    third_team = await make_team(session, "Third", manager_id=other_manager)
    _, blocker_id = await _blocked_task(
        session, w, status=IN_PROGRESS, team_id=w.other_team_id, owner_id=w.outsider_id
    )

    with pytest.raises(AppError) as e:
        await _run(session, clock, "resolve", actor_id=other_manager, blocker_id=blocker_id)
    assert e.value.code in ("AUTHORIZATION_REQUIRED", "BLOCKER_NOT_FOUND")  # not a participant either

    # Routed to their Team: their queue, their call.
    await session.execute(
        text("UPDATE blockers SET blocker_team_id = :team WHERE id = :b"),
        {"team": third_team, "b": blocker_id},
    )
    from app.dr_events.participants import enrol_participant

    await enrol_participant(session, w.event_id, other_manager, "EXPLICIT", added_by_user_id=w.admin_id)
    await _run(session, clock, "resolve", actor_id=other_manager, blocker_id=blocker_id)
    assert (await blocker_row(session, blocker_id)).status == RESOLVED


async def test_seeded_blocked_task_has_one_open_blocker(session: AsyncSession) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w, status="BLOCKED")
    assert len(await blocker_ids_of(session, task_id)) == 1

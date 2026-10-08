"""`POST /tasks/{id}/assign` and `/volunteer` (API_CONTRACT.md:165-166; RBAC_MATRIX.md:13-14;
FROZEN_DECISIONS.md §4.10-14; DATA_MODEL.md invariants 8, 9, 22; D-214).

World (tests/factories.py): Owning Team "Network" is managed by `manager_id`, with members `executor_id`
(the default assignee), `teammate_id` and `roleless_member_id`. `outsider_id` is on "Storage". `ws_lead_id`
leads `work_stream_id`. `stranger_id` is not a participant."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import FakeClock
from app.core.errors import AppError
from app.dr_events.participants import enrol_participant
from app.resources_skills.assignment_service import AssignmentService
from app.tasks_dependencies.transition_service import TaskTransitionService
from tests.factories import World, build_world, grant_role, headers, http, login, make_user, seed_task

pytestmark = [pytest.mark.auth]


async def _task(session: AsyncSession, task_id: uuid.UUID) -> Any:
    return (
        await session.execute(
            text("SELECT current_assignee_user_id, owning_team_id, version, status FROM tasks WHERE id = :t"),
            {"t": task_id},
        )
    ).one()


async def _assign(
    session: AsyncSession,
    w: World,
    clock: FakeClock,
    actor: str,
    task_id: uuid.UUID,
    target: str,
    version: int = 1,
) -> Any:
    return await AssignmentService.assign(
        session,
        actor_id=getattr(w, actor),
        task_id=task_id,
        assignee_user_id=getattr(w, target),
        expected_version=version,
        clock=clock,
    )


async def _audits(session: AsyncSession, task_id: uuid.UUID) -> list[Any]:
    rows = await session.execute(
        text(
            "SELECT action, actor_user_id, metadata, after_data FROM audit_events "
            "WHERE entity_id = :t AND action IN ('TASK_ASSIGNED', 'TASK_VOLUNTEERED') "
            "ORDER BY (after_data->>'version')::int"
        ),
        {"t": task_id},
    )
    return list(rows.all())


async def _assignment_events(session: AsyncSession, task_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = await session.execute(
        text(
            "SELECT payload FROM outbox_events WHERE aggregate_id = :t AND event_type = 'AssignmentChanged'"
        ),
        {"t": task_id},
    )
    return [r.payload for r in rows]


# --------------------------------------------------------------------------------------------
# Authority (RBAC_MATRIX.md:13-14)
# --------------------------------------------------------------------------------------------

SAME_TEAM, CROSS_TEAM = "teammate_id", "outsider_id"

AUTHORITY: list[tuple[str, str, int]] = [
    ("admin_id", CROSS_TEAM, 200),
    ("coordinator_id", CROSS_TEAM, 200),
    ("ws_lead_id", CROSS_TEAM, 200),  # stream-scoped cross-Team assignment
    ("ws_lead_id", SAME_TEAM, 200),
    ("manager_id", SAME_TEAM, 200),
    ("manager_id", CROSS_TEAM, 403),  # Managers may reassign only members of their own Team (§4.10)
    ("executor_id", SAME_TEAM, 200),  # same-Team members reassign Team work to one another (§4.11)
    ("teammate_id", "roleless_member_id", 200),
    ("executor_id", CROSS_TEAM, 403),  # cross-Team work is volunteer-only for an Executor
    ("outsider_id", SAME_TEAM, 403),
    ("roleless_member_id", SAME_TEAM, 403),  # Team membership without the EXECUTOR role
    ("system_owner_id", SAME_TEAM, 403),  # an owner slot grants no assignment role
    ("business_owner_id", SAME_TEAM, 403),
]


@pytest.mark.parametrize(("actor", "target", "outcome"), AUTHORITY)
async def test_assignment_authority(
    session: AsyncSession, clock: FakeClock, actor: str, target: str, outcome: int
) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w)

    if outcome == 200:
        await _assign(session, w, clock, actor, task_id, target)
        row = await _task(session, task_id)
        assert (row.current_assignee_user_id, row.version) == (getattr(w, target), 2)
        assert row.owning_team_id == w.team_id  # invariant 8: accountability never moves
    else:
        with pytest.raises(AppError) as exc:
            await _assign(session, w, clock, actor, task_id, target)
        assert exc.value.status_code == outcome
        assert (await _task(session, task_id)).version == 1


async def test_a_lead_of_another_stream_cannot_assign_cross_team(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w, app_scoped=False, work_stream_id=w.other_work_stream_id)

    with pytest.raises(AppError) as exc:
        await _assign(session, w, clock, "ws_lead_id", task_id, CROSS_TEAM)

    assert exc.value.status_code == 403


async def test_a_manager_of_another_team_cannot_assign(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)
    other_manager = await make_user(session, "Storage manager")
    await session.execute(
        text("UPDATE teams SET manager_user_id = :u WHERE id = :t"),
        {"u": other_manager, "t": w.other_team_id},
    )
    await grant_role(session, other_manager, "MANAGER")
    await enrol_participant(session, w.event_id, other_manager, "EXPLICIT")
    task_id = await seed_task(session, w)

    with pytest.raises(AppError) as exc:
        await AssignmentService.assign(
            session, actor_id=other_manager, task_id=task_id, assignee_user_id=w.teammate_id,
            expected_version=1, clock=clock,
        )  # fmt: skip

    assert exc.value.status_code == 403


async def test_a_non_participant_gets_404(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w)

    for missing in (task_id, uuid.uuid4()):
        with pytest.raises(AppError) as exc:
            await AssignmentService.assign(
                session, actor_id=w.stranger_id, task_id=missing, assignee_user_id=w.teammate_id,
                expected_version=1, clock=clock,
            )  # fmt: skip
        assert (exc.value.status_code, exc.value.code) == (404, "TASK_NOT_FOUND")


async def test_an_unknown_assignee_is_404(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w)

    with pytest.raises(AppError) as exc:
        await AssignmentService.assign(
            session, actor_id=w.admin_id, task_id=task_id, assignee_user_id=uuid.uuid4(),
            expected_version=1, clock=clock,
        )  # fmt: skip

    assert exc.value.code == "USER_NOT_FOUND"


@pytest.mark.parametrize("status", ["COMPLETED", "CANCELLED"])
async def test_terminal_tasks_cannot_be_assigned(
    session: AsyncSession, clock: FakeClock, status: str
) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w, status=status)

    with pytest.raises(AppError) as exc:
        await _assign(session, w, clock, "admin_id", task_id, SAME_TEAM)

    assert (exc.value.status_code, exc.value.code) == (409, "INVALID_TRANSITION")


@pytest.mark.parametrize("status", ["NOT_STARTED", "IN_PROGRESS", "BLOCKED", "READY_FOR_VALIDATION"])
async def test_open_tasks_can_be_reassigned(session: AsyncSession, clock: FakeClock, status: str) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w, status=status)

    await _assign(session, w, clock, "coordinator_id", task_id, SAME_TEAM)

    assert (await _task(session, task_id)).status == status  # assignment never moves the lifecycle


async def test_assigning_the_current_assignee_again_changes_nothing(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w, assignee_id=w.teammate_id)

    await _assign(session, w, clock, "coordinator_id", task_id, "teammate_id")

    assert (await _task(session, task_id)).version == 1
    assert await _audits(session, task_id) == []


async def test_assignment_audits_emits_and_enrols(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)
    newcomer = await make_user(session, "Newcomer")  # not yet a participant of the Event
    await grant_role(session, newcomer, "EXECUTOR")
    task_id = await seed_task(session, w)

    await AssignmentService.assign(
        session,
        actor_id=w.coordinator_id,
        task_id=task_id,
        assignee_user_id=newcomer,
        expected_version=1,
        clock=clock,
    )

    [audit] = await _audits(session, task_id)
    assert (audit.action, audit.actor_user_id) == ("TASK_ASSIGNED", w.coordinator_id)
    assert audit.metadata["authority"] == "COORDINATOR"
    assert audit.after_data["current_assignee_user_id"] == str(newcomer)
    [event] = await _assignment_events(session, task_id)
    assert set(event["affected_user_ids"]) == {str(w.executor_id), str(newcomer)}
    enrolled = (
        (
            await session.execute(
                text(
                    "SELECT source FROM dr_event_participants WHERE dr_event_id = :e AND user_id = :u "
                    "AND removed_at IS NULL"
                ),
                {"e": w.event_id, "u": newcomer},
            )
        )
        .scalars()
        .all()
    )
    assert list(enrolled) == ["TASK_ASSIGNEE"]  # D-222


# --------------------------------------------------------------------------------------------
# D-214 Manager precedence
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("intervening", ["coordinator_id", "admin_id"])
async def test_manager_precedence_accepts_a_stale_own_team_assignment_after_a_coordinator_assignment(
    session: AsyncSession, clock: FakeClock, intervening: str
) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w)
    await _assign(session, w, clock, intervening, task_id, "teammate_id")  # v1 -> v2

    await _assign(session, w, clock, "manager_id", task_id, "roleless_member_id", version=1)  # stale

    row = await _task(session, task_id)
    assert (row.current_assignee_user_id, row.version) == (w.roleless_member_id, 3)
    first, second = await _audits(session, task_id)  # both actions audited
    assert (first.actor_user_id, second.actor_user_id) == (getattr(w, intervening), w.manager_id)
    assert second.metadata["conflict_resolution"] == "MANAGER_PRECEDENCE"
    override = (
        await session.execute(
            text("SELECT override_type, performed_by_user_id, metadata FROM overrides WHERE target_id = :t"),
            {"t": task_id},
        )
    ).one()
    assert (override.override_type, override.performed_by_user_id) == ("MANAGER_PRECEDENCE", w.manager_id)
    assert override.metadata["superseded_version"] == 2
    assert override.metadata["superseded_assignee_user_id"] == str(w.teammate_id)
    assert override.metadata["superseded_by_user_ids"] == [str(getattr(w, intervening))]
    precedence_event = (await _assignment_events(session, task_id))[-1]
    assert precedence_event["conflict_resolution"] == "MANAGER_PRECEDENCE"
    assert set(precedence_event["affected_user_ids"]) == {
        str(getattr(w, intervening)),
        str(w.teammate_id),
        str(w.roleless_member_id),
    }


@pytest.mark.parametrize("intervening", ["coordinator_id", "admin_id"])
async def test_precedence_onto_the_current_assignee_is_still_recorded(
    session: AsyncSession, clock: FakeClock, intervening: str
) -> None:
    """D-214 says the accepted stale Manager write bumps the version, is audited, records
    MANAGER_PRECEDENCE and notifies -- also when the Manager names the user the Coordinator chose."""
    w = await build_world(session)
    task_id = await seed_task(session, w)
    await _assign(session, w, clock, intervening, task_id, "teammate_id")  # v1 -> v2

    await _assign(session, w, clock, "manager_id", task_id, "teammate_id", version=1)  # stale, same user

    row = await _task(session, task_id)
    assert (row.current_assignee_user_id, row.version) == (w.teammate_id, 3)
    first, second = await _audits(session, task_id)
    assert (first.actor_user_id, second.actor_user_id) == (getattr(w, intervening), w.manager_id)
    assert second.metadata["conflict_resolution"] == "MANAGER_PRECEDENCE"
    override = (
        await session.execute(
            text("SELECT override_type, metadata FROM overrides WHERE target_id = :t"), {"t": task_id}
        )
    ).one()
    assert override.override_type == "MANAGER_PRECEDENCE"
    assert override.metadata["superseded_assignee_user_id"] == str(w.teammate_id)
    event = (await _assignment_events(session, task_id))[-1]
    assert event["conflict_resolution"] == "MANAGER_PRECEDENCE"
    assert sorted(event["affected_user_ids"]) == sorted({str(getattr(w, intervening)), str(w.teammate_id)})


async def test_a_fresh_assignment_onto_the_current_assignee_is_a_silent_no_op(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w)
    await _assign(session, w, clock, "coordinator_id", task_id, "teammate_id")  # v1 -> v2

    await _assign(session, w, clock, "coordinator_id", task_id, "teammate_id", version=2)  # current version

    assert (await _task(session, task_id)).version == 2
    assert len(await _audits(session, task_id)) == 1


async def test_precedence_spans_several_coordinator_assignments(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w)
    await _assign(session, w, clock, "coordinator_id", task_id, "teammate_id")
    await _assign(session, w, clock, "admin_id", task_id, "roleless_member_id", version=2)

    await _assign(session, w, clock, "manager_id", task_id, "executor_id", version=1)

    assert (await _task(session, task_id)).version == 4


@pytest.mark.parametrize("intervening", ["manager_id", "executor_id", "ws_lead_id"])
async def test_an_intervening_assignment_by_anyone_else_is_a_plain_409(
    session: AsyncSession, clock: FakeClock, intervening: str
) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w)
    await _assign(session, w, clock, intervening, task_id, "teammate_id")

    with pytest.raises(AppError) as exc:
        await _assign(session, w, clock, "manager_id", task_id, "roleless_member_id", version=1)

    assert exc.value.code == "CONCURRENCY_CONFLICT"
    assert (await _task(session, task_id)).version == 2


async def test_an_intervening_non_assignment_change_is_a_plain_409(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w)
    await _assign(session, w, clock, "coordinator_id", task_id, "teammate_id")  # v2
    await TaskTransitionService.start(
        session, actor_id=w.teammate_id, task_id=task_id, expected_version=2, clock=clock
    )

    with pytest.raises(AppError) as exc:
        await _assign(session, w, clock, "manager_id", task_id, "roleless_member_id", version=1)

    assert exc.value.code == "CONCURRENCY_CONFLICT"


@pytest.mark.parametrize("actor", ["coordinator_id", "admin_id", "ws_lead_id", "executor_id"])
async def test_every_other_stale_assignment_is_409(
    session: AsyncSession, clock: FakeClock, actor: str
) -> None:
    """Precedence belongs to the Team's Manager alone (D-214)."""
    w = await build_world(session)
    task_id = await seed_task(session, w)
    await _assign(session, w, clock, "coordinator_id", task_id, "teammate_id")

    with pytest.raises(AppError) as exc:
        await _assign(session, w, clock, actor, task_id, "roleless_member_id", version=1)

    assert exc.value.code == "CONCURRENCY_CONFLICT"


async def test_a_stale_manager_still_cannot_assign_cross_team(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w)
    await _assign(session, w, clock, "coordinator_id", task_id, "teammate_id")

    with pytest.raises(AppError) as exc:
        await _assign(session, w, clock, "manager_id", task_id, CROSS_TEAM, version=1)

    assert exc.value.status_code == 403


async def test_a_future_version_is_a_plain_409(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w)

    with pytest.raises(AppError) as exc:
        await _assign(session, w, clock, "manager_id", task_id, SAME_TEAM, version=5)

    assert exc.value.code == "CONCURRENCY_CONFLICT"


# --------------------------------------------------------------------------------------------
# volunteer (FROZEN_DECISIONS.md §4.12)
# --------------------------------------------------------------------------------------------


async def _volunteer(
    session: AsyncSession, w: World, clock: FakeClock, actor: str, task_id: uuid.UUID, v: int = 1
) -> Any:
    return await AssignmentService.volunteer(
        session, actor_id=getattr(w, actor), task_id=task_id, expected_version=v, clock=clock
    )


async def _unassigned(session: AsyncSession, w: World, **seed: Any) -> uuid.UUID:
    task_id = await seed_task(session, w, **seed)
    await session.execute(
        text("UPDATE tasks SET current_assignee_user_id = NULL WHERE id = :t"), {"t": task_id}
    )
    return task_id


async def test_an_executor_volunteers_for_unassigned_cross_team_work(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)
    task_id = await _unassigned(session, w)

    await _volunteer(session, w, clock, "outsider_id", task_id)

    row = await _task(session, task_id)
    assert (row.current_assignee_user_id, row.owning_team_id, row.version) == (w.outsider_id, w.team_id, 2)
    [audit] = await _audits(session, task_id)
    assert (audit.action, audit.actor_user_id) == ("TASK_VOLUNTEERED", w.outsider_id)
    [event] = await _assignment_events(session, task_id)
    assert event["affected_user_ids"] == [str(w.outsider_id)]


async def test_assigned_work_cannot_be_volunteered_for(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w)

    with pytest.raises(AppError) as exc:
        await _volunteer(session, w, clock, "outsider_id", task_id)

    assert (exc.value.status_code, exc.value.code) == (409, "TASK_ALREADY_ASSIGNED")


@pytest.mark.parametrize("actor", ["roleless_member_id", "system_owner_id", "business_owner_id"])
async def test_volunteering_needs_the_executor_role(
    session: AsyncSession, clock: FakeClock, actor: str
) -> None:
    w = await build_world(session)
    task_id = await _unassigned(session, w)

    with pytest.raises(AppError) as exc:
        await _volunteer(session, w, clock, actor, task_id)

    assert exc.value.status_code == 403


@pytest.mark.parametrize("status", ["COMPLETED", "CANCELLED"])
async def test_terminal_work_cannot_be_volunteered_for(
    session: AsyncSession, clock: FakeClock, status: str
) -> None:
    w = await build_world(session)
    task_id = await _unassigned(session, w, status=status)

    with pytest.raises(AppError) as exc:
        await _volunteer(session, w, clock, "outsider_id", task_id)

    assert exc.value.code == "INVALID_TRANSITION"


async def test_a_stale_volunteer_is_409(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)
    task_id = await _unassigned(session, w)

    with pytest.raises(AppError) as exc:
        await _volunteer(session, w, clock, "outsider_id", task_id, v=3)

    assert exc.value.code == "CONCURRENCY_CONFLICT"


async def test_a_stranger_cannot_volunteer(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)
    task_id = await _unassigned(session, w)

    with pytest.raises(AppError) as exc:
        await _volunteer(session, w, clock, "stranger_id", task_id)

    assert exc.value.status_code == 404


# --------------------------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------------------------


async def test_assign_over_http_replays(session: AsyncSession, redis_client: Redis, clock: FakeClock) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w)
    sid, csrf = await login(session, redis_client, clock, w.coordinator_id)
    same = headers(csrf)
    body = {"assignee_user_id": str(w.teammate_id), "expected_version": 1}

    async with http(session, redis_client, clock) as c:
        first = await c.post(
            f"/api/v1/tasks/{task_id}/assign", cookies={"drcc_session": sid}, headers=same, json=body
        )
        replay = await c.post(
            f"/api/v1/tasks/{task_id}/assign", cookies={"drcc_session": sid}, headers=same, json=body
        )
    await redis_client.delete(f"drcc:session:{sid}")

    assert first.status_code == 200, first.text
    assert first.json()["current_assignee_user_id"] == str(w.teammate_id)
    assert replay.json() == first.json()
    assert len(await _audits(session, task_id)) == 1


async def test_manager_precedence_over_http_is_200_not_409(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w)
    await _assign(session, w, clock, "coordinator_id", task_id, "teammate_id")
    sid, csrf = await login(session, redis_client, clock, w.manager_id)

    async with http(session, redis_client, clock) as c:
        r = await c.post(
            f"/api/v1/tasks/{task_id}/assign",
            cookies={"drcc_session": sid},
            headers=headers(csrf),
            json={"assignee_user_id": str(w.roleless_member_id), "expected_version": 1},
        )
    await redis_client.delete(f"drcc:session:{sid}")

    assert r.status_code == 200, r.text
    assert r.json()["version"] == 3

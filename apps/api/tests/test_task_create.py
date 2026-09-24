"""Task creation (API_CONTRACT.md:157): the D-226 evidence fields and `needs_specific_validation`
(D-209) in the body, same-Event references, and D-222 auto-enrolment of the Owning Team -- without
which an assigned Executor gets a 404 on their own Task (BUILD-06.plan.md Risk #8)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import FakeClock
from app.core.errors import AppError
from app.tasks_dependencies.commands import create_draft_task, create_task
from app.tasks_dependencies.transition_service import TaskTransitionService
from tests.factories import (
    World,
    add_member,
    audit_actions,
    build_world,
    grant_role,
    headers,
    http,
    login,
    make_team,
    make_user,
    outbox_types,
    seed_foreign_task,
    seed_task,
)

pytestmark = [pytest.mark.domain]


async def new(session: AsyncSession, w: World, clock: FakeClock, **kwargs: Any) -> uuid.UUID:
    fields: dict[str, Any] = {
        "actor_id": w.admin_id,
        "dr_event_id": w.event_id,
        "title": "Repoint DNS",
        "phase": "FAILOVER",
        "owning_team_id": w.team_id,
        "work_stream_id": w.work_stream_id,
        "clock": clock,
    }
    fields.update(kwargs)
    task = await create_task(session, **fields)
    return task.id


async def participants(session: AsyncSession, w: World, user_id: uuid.UUID) -> set[str]:
    rows = await session.execute(
        text(
            "SELECT source FROM dr_event_participants "
            "WHERE dr_event_id = :e AND user_id = :u AND removed_at IS NULL"
        ),
        {"e": w.event_id, "u": user_id},
    )
    return {r.source for r in rows}


async def row(session: AsyncSession, task_id: uuid.UUID) -> Any:
    return (
        await session.execute(
            text(
                "SELECT status, evidence_required, evidence_min_count, verification_note_required, "
                "needs_specific_validation, added_during_execution, current_assignee_user_id "
                "FROM tasks WHERE id = :id"
            ),
            {"id": task_id},
        )
    ).one()


# --------------------------------------------------------------------------------------------
# Fields
# --------------------------------------------------------------------------------------------


async def test_d226_defaults_and_a_fresh_not_started_task(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)

    task_id = await new(session, w, clock)

    r = await row(session, task_id)
    assert (r.status, r.evidence_required, r.evidence_min_count, r.verification_note_required) == (
        "NOT_STARTED",
        True,
        1,
        True,
    )
    assert r.needs_specific_validation is False
    assert r.current_assignee_user_id is None  # assignment is BUILD-07's (Risk #21)
    assert "TASK_CREATED" in await audit_actions(session, task_id)
    assert await outbox_types(session, task_id) == ["TaskChanged"]


async def test_explicit_evidence_settings_round_trip(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)

    task_id = await new(
        session,
        w,
        clock,
        evidence_required=False,
        evidence_min_count=0,
        verification_note_required=False,
        needs_specific_validation=True,
    )

    r = await row(session, task_id)
    assert (r.evidence_required, r.evidence_min_count, r.verification_note_required) == (False, 0, False)
    assert r.needs_specific_validation is True


async def test_added_during_execution_reflects_the_event_being_past_planned(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)  # the world's Event is ACTIVE
    live = await new(session, w, clock)
    await session.execute(text("UPDATE dr_events SET status = 'PLANNED' WHERE id = :e"), {"e": w.event_id})
    planned = await new(session, w, clock, title="Planned work")

    assert (await row(session, live)).added_during_execution is True
    assert (await row(session, planned)).added_during_execution is False


async def test_a_task_needs_an_application_or_a_work_stream(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)

    with pytest.raises(AppError) as exc:
        await new(session, w, clock, work_stream_id=None, dr_application_id=None)

    assert (exc.value.code, exc.value.status_code) == ("TASK_CONTEXT_REQUIRED", 422)


@pytest.mark.parametrize("field", ["work_stream_id", "dr_application_id", "parent_task_id"])
async def test_references_must_belong_to_the_same_event(
    session: AsyncSession, clock: FakeClock, field: str
) -> None:
    w = await build_world(session)
    other_event, foreign_task = await seed_foreign_task(session, w)
    foreign = {
        "work_stream_id": await session.scalar(
            text("SELECT id FROM work_streams WHERE dr_event_id = :e"), {"e": other_event}
        ),
        "dr_application_id": uuid.uuid4(),  # not a DR Application of this Event at all
        "parent_task_id": foreign_task,
    }[field]

    with pytest.raises(AppError) as exc:
        await new(session, w, clock, **{field: foreign})

    assert (exc.value.code, exc.value.status_code) == ("CROSS_EVENT_REFERENCE", 422)
    assert exc.value.details == {"field": field}


async def test_unknown_owning_team_is_404(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)

    with pytest.raises(AppError) as exc:
        await new(session, w, clock, owning_team_id=uuid.uuid4())

    assert (exc.value.code, exc.value.status_code) == ("TEAM_NOT_FOUND", 404)


# --------------------------------------------------------------------------------------------
# D-222 auto-enrolment
# --------------------------------------------------------------------------------------------


async def test_creating_a_task_enrols_its_owning_teams_members_and_manager(
    session: AsyncSession, clock: FakeClock
) -> None:
    """A Team nobody has enrolled yet: after creation its members and manager can see the Event."""
    w = await build_world(session)
    manager = await make_user(session, "Fresh manager")
    member = await make_user(session, "Fresh member")
    team = await make_team(session, "Fresh", manager_id=manager)
    await add_member(session, team, member)

    await new(session, w, clock, owning_team_id=team)

    assert await participants(session, w, member) == {"OWNING_TEAM"}
    assert await participants(session, w, manager) == {"OWNING_TEAM"}


async def test_an_enrolled_team_member_can_then_work_the_task(
    session: AsyncSession, clock: FakeClock
) -> None:
    """The Risk #8 regression: before enrolment existed, this member got TASK_NOT_FOUND."""
    w = await build_world(session)
    member = await make_user(session, "Fresh member")
    await grant_role(session, member, "EXECUTOR")
    team = await make_team(session, "Fresh")
    await add_member(session, team, member)
    task_id = await new(session, w, clock, owning_team_id=team)

    task = await TaskTransitionService.start(
        session, actor_id=member, task_id=task_id, expected_version=1, clock=clock
    )

    assert task.status == "IN_PROGRESS"


async def test_members_who_left_the_team_are_not_enrolled(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)
    former = await make_user(session, "Former member")
    team = await make_team(session, "Fresh")
    await session.execute(
        text("INSERT INTO team_memberships (id, team_id, user_id, effective_to) VALUES (:id, :t, :u, :to)"),
        {"id": uuid.uuid4(), "t": team, "u": former, "to": clock.now().replace(year=2025)},
    )

    await new(session, w, clock, owning_team_id=team)

    assert await participants(session, w, former) == set()


async def test_the_import_path_enrols_the_assignee_too(session: AsyncSession, clock: FakeClock) -> None:
    """`create_draft_task` is also BUILD-05's Excel-import path, which sets an assignee."""
    w = await build_world(session)
    assignee = await make_user(session, "Imported assignee")

    await create_draft_task(
        session,
        dr_event_id=w.event_id,
        title="Imported",
        phase="FAILOVER",
        owning_team_id=w.other_team_id,
        created_by_user_id=w.admin_id,
        work_stream_id=w.work_stream_id,
        current_assignee_user_id=assignee,
        clock=clock,
    )

    assert await participants(session, w, assignee) == {"TASK_ASSIGNEE"}


# --------------------------------------------------------------------------------------------
# Who may create a Task (CHANGE_TASK_METADATA on the new Task's scopes, Risk #21)
# --------------------------------------------------------------------------------------------


@pytest.mark.auth
@pytest.mark.parametrize("who", ["admin_id", "coordinator_id", "ws_lead_id", "manager_id"])
async def test_planners_in_scope_may_create(session: AsyncSession, clock: FakeClock, who: str) -> None:
    w = await build_world(session)

    task_id = await new(session, w, clock, actor_id=getattr(w, who))

    assert (await row(session, task_id)).status == "NOT_STARTED"


@pytest.mark.auth
@pytest.mark.parametrize("who", ["executor_id", "teammate_id", "business_owner_id", "outsider_id"])
async def test_executors_and_owners_out_of_scope_may_not_create(
    session: AsyncSession, clock: FakeClock, who: str
) -> None:
    w = await build_world(session)

    with pytest.raises(AppError) as exc:
        await new(session, w, clock, actor_id=getattr(w, who))

    assert exc.value.status_code == 403


@pytest.mark.auth
async def test_an_invisible_event_is_404(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)

    with pytest.raises(AppError) as exc:
        await new(session, w, clock, actor_id=w.stranger_id)

    assert (exc.value.code, exc.value.status_code) == ("DR_EVENT_NOT_FOUND", 404)


# --------------------------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------------------------


@pytest.mark.api
async def test_create_and_list_over_http(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    w = await build_world(session)
    existing = await seed_task(session, w)
    sid, csrf = await login(session, redis_client, clock, w.coordinator_id)
    same = headers(csrf)
    url = f"/api/v1/dr-events/{w.event_id}/tasks"
    body = {
        "title": "Verify replication lag",
        "phase": "VALIDATION",
        "owning_team_id": str(w.team_id),
        "work_stream_id": str(w.work_stream_id),
        "evidence_min_count": 2,
        "needs_specific_validation": True,
    }

    async with http(session, redis_client, clock) as c:
        keyless = await c.post(url, cookies={"drcc_session": sid}, headers={"X-CSRF-Token": csrf}, json=body)
        first = await c.post(url, cookies={"drcc_session": sid}, headers=same, json=body)
        replay = await c.post(url, cookies={"drcc_session": sid}, headers=same, json=body)
        negative = await c.post(
            url, cookies={"drcc_session": sid}, headers=headers(csrf), json={**body, "evidence_min_count": -1}
        )
        listed = await c.get(url, cookies={"drcc_session": sid})

    assert (keyless.status_code, keyless.json()["error"]["code"]) == (400, "IDEMPOTENCY_KEY_REQUIRED")
    assert first.status_code == replay.status_code == 201
    assert replay.json() == first.json()
    created = first.json()
    assert (
        created["evidence_required"],
        created["evidence_min_count"],
        created["verification_note_required"],
    ) == (
        True,
        2,
        True,
    )
    assert (created["needs_specific_validation"], created["status"]) == (True, "NOT_STARTED")
    assert negative.status_code == 422
    assert {t["id"] for t in listed.json()["tasks"]} == {str(existing), created["id"]}
    await redis_client.delete(f"drcc:session:{sid}")


@pytest.mark.api
async def test_list_is_404_for_an_invisible_event(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    w = await build_world(session)
    sid, _ = await login(session, redis_client, clock, w.stranger_id)

    async with http(session, redis_client, clock) as c:
        r = await c.get(f"/api/v1/dr-events/{w.event_id}/tasks", cookies={"drcc_session": sid})

    assert r.status_code == 404
    await redis_client.delete(f"drcc:session:{sid}")

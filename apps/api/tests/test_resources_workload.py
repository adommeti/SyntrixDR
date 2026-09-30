"""`GET /teams/{id}/workload` (API_CONTRACT.md:91) and `GET /dr-events/{id}/resources` (:143, D-212):
live roll-ups computed from Tasks at request time (BUILD-07 "dynamic workload from Tasks")."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import FakeClock
from app.resources_skills.assignment_service import AssignmentService
from tests.factories import World, build_world, http, login, seed_foreign_task, seed_task

pytestmark = [pytest.mark.api]


async def _get(
    session: AsyncSession, redis_client: Redis, clock: FakeClock, user_id: uuid.UUID, url: str
) -> Any:
    sid, _ = await login(session, redis_client, clock, user_id)
    async with http(session, redis_client, clock) as c:
        r = await c.get(url, cookies={"drcc_session": sid})
    await redis_client.delete(f"drcc:session:{sid}")
    return r


def _member(body: dict[str, Any], user_id: uuid.UUID) -> dict[str, Any]:
    return next(m for m in body["members"] if m["user_id"] == str(user_id))


async def _seed_load(session: AsyncSession, w: World) -> None:
    """Network Team: executor has 1 IN_PROGRESS + 1 BLOCKED; teammate 1 NOT_STARTED; one unassigned
    NOT_STARTED; one COMPLETED (never counted). Outsider (Storage) works one Network Task."""
    await seed_task(session, w, status="IN_PROGRESS")
    await seed_task(session, w, status="BLOCKED")
    await seed_task(session, w, assignee_id=w.teammate_id)
    unassigned = await seed_task(session, w)
    await session.execute(
        text("UPDATE tasks SET current_assignee_user_id = NULL WHERE id = :t"), {"t": unassigned}
    )
    await seed_task(session, w, status="COMPLETED")
    await seed_task(session, w, assignee_id=w.outsider_id, status="IN_PROGRESS")


async def test_team_workload_counts_live_open_work(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    w = await build_world(session)
    await _seed_load(session, w)

    r = await _get(session, redis_client, clock, w.coordinator_id, f"/api/v1/teams/{w.team_id}/workload")

    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["team_id"], body["member_count"]) == (str(w.team_id), 3)
    assert body["team_name"].startswith("Network")
    assert body["owned_open_tasks"] == {
        "NOT_STARTED": 2,
        "IN_PROGRESS": 2,
        "BLOCKED": 1,
        "READY_FOR_VALIDATION": 0,
    }
    assert body["unassigned_open"] == 1
    executor = _member(body, w.executor_id)
    assert executor["open_tasks"] == {
        "NOT_STARTED": 0,
        "IN_PROGRESS": 1,
        "BLOCKED": 1,
        "READY_FOR_VALIDATION": 0,
    }
    assert executor["open_total"] == 2
    assert _member(body, w.teammate_id)["open_total"] == 1


async def test_workload_moves_with_an_assignment(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w)
    url = f"/api/v1/teams/{w.team_id}/workload"
    before = await _get(session, redis_client, clock, w.manager_id, url)

    await AssignmentService.assign(
        session,
        actor_id=w.manager_id,
        task_id=task_id,
        assignee_user_id=w.teammate_id,
        expected_version=1,
        clock=clock,
    )
    after = await _get(session, redis_client, clock, w.manager_id, url)

    assert _member(before.json(), w.executor_id)["open_total"] == 1
    assert _member(after.json(), w.executor_id)["open_total"] == 0
    assert _member(after.json(), w.teammate_id)["open_total"] == 1


async def test_closed_events_drop_out_of_the_workload(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    w = await build_world(session)
    await seed_task(session, w, status="IN_PROGRESS")
    await session.execute(text("UPDATE dr_events SET status = 'CLOSED' WHERE id = :e"), {"e": w.event_id})

    r = await _get(session, redis_client, clock, w.admin_id, f"/api/v1/teams/{w.team_id}/workload")

    assert _member(r.json(), w.executor_id)["open_total"] == 0


async def test_workload_counts_only_events_the_viewer_can_see(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    """A Task in an Event the Manager doesn't participate in isn't counted for them; the Admin (implicit
    participant of every Event) sees it."""
    w = await build_world(session)
    foreign_event, foreign_task = await seed_foreign_task(session, w)
    await session.execute(
        text("UPDATE tasks SET current_assignee_user_id = :u WHERE id = :id"),
        {"u": w.executor_id, "id": foreign_task},
    )
    # Creating the Task enrolled the Owning Team (D-222); take the Manager back out of that Event.
    await session.execute(
        text("UPDATE dr_event_participants SET removed_at = now() WHERE dr_event_id = :e AND user_id = :u"),
        {"e": foreign_event, "u": w.manager_id},
    )
    url = f"/api/v1/teams/{w.team_id}/workload"

    as_manager = await _get(session, redis_client, clock, w.manager_id, url)
    as_admin = await _get(session, redis_client, clock, w.admin_id, url)

    assert _member(as_manager.json(), w.executor_id)["open_total"] == 0
    assert _member(as_admin.json(), w.executor_id)["open_total"] == 1


@pytest.mark.parametrize(("actor", "status"), [
    ("admin_id", 200), ("coordinator_id", 200), ("manager_id", 200), ("executor_id", 200),
    ("outsider_id", 403), ("ws_lead_id", 403), ("system_owner_id", 403),
])  # fmt: skip
async def test_who_may_see_a_teams_workload(
    session: AsyncSession, redis_client: Redis, clock: FakeClock, actor: str, status: int
) -> None:
    w = await build_world(session)

    r = await _get(session, redis_client, clock, getattr(w, actor), f"/api/v1/teams/{w.team_id}/workload")

    assert r.status_code == status, r.text


async def test_an_unknown_team_is_404(session: AsyncSession, redis_client: Redis, clock: FakeClock) -> None:
    w = await build_world(session)

    r = await _get(session, redis_client, clock, w.admin_id, f"/api/v1/teams/{uuid.uuid4()}/workload")

    assert (r.status_code, r.json()["error"]["code"]) == (404, "TEAM_NOT_FOUND")


async def test_event_resources_list_people_teams_and_load(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    w = await build_world(session)
    await _seed_load(session, w)
    skill_id = uuid.uuid4()
    await session.execute(text("INSERT INTO skills (id, name) VALUES (:id, 'DNS')"), {"id": skill_id})
    await session.execute(
        text("INSERT INTO user_skills (id, user_id, skill_id) VALUES (:id, :u, :s)"),
        {"id": uuid.uuid4(), "u": w.executor_id, "s": skill_id},
    )

    r = await _get(session, redis_client, clock, w.executor_id, f"/api/v1/dr-events/{w.event_id}/resources")

    assert r.status_code == 200, r.text
    body = r.json()
    executor = next(p for p in body["people"] if p["user_id"] == str(w.executor_id))
    assert executor["skills"] == ["DNS"]
    assert [t["team_id"] for t in executor["teams"]] == [str(w.team_id)]
    assert executor["open_total"] == 2
    outsider = next(p for p in body["people"] if p["user_id"] == str(w.outsider_id))
    assert outsider["open_tasks"]["IN_PROGRESS"] == 1
    assert [t["team_id"] for t in outsider["teams"]] == [str(w.other_team_id)]
    network = next(t for t in body["teams"] if t["team_id"] == str(w.team_id))
    assert (network["open_tasks"]["IN_PROGRESS"], network["unassigned_open"]) == (2, 1)
    assert body["people"][0]["open_total"] >= body["people"][-1]["open_total"]  # busiest first


async def test_event_resources_404_for_an_invisible_event(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    w = await build_world(session)

    r = await _get(session, redis_client, clock, w.stranger_id, f"/api/v1/dr-events/{w.event_id}/resources")

    assert r.status_code == 404

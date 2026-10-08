"""`GET /dr-events/{id}/blockers` and the four Blocker commands over HTTP (API_CONTRACT.md:180-184)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import FakeClock
from tests.factories import (
    World,
    audit_actions,
    blocker_row,
    build_world,
    headers,
    http,
    login,
    seed_blocker,
    seed_task,
    task_row,
)

pytestmark = [pytest.mark.api]


async def _post(
    session: AsyncSession,
    redis_client: Redis,
    clock: FakeClock,
    w: World,
    actor: str,
    blocker_id: uuid.UUID,
    command: str,
    body: dict[str, Any],
    key: str | None = None,
) -> Any:
    sid, csrf = await login(session, redis_client, clock, getattr(w, actor))
    async with http(session, redis_client, clock) as c:
        r = await c.post(
            f"/api/v1/blockers/{blocker_id}/{command}",
            cookies={"drcc_session": sid},
            headers=headers(csrf, key),
            json=body,
        )
    await redis_client.delete(f"drcc:session:{sid}")
    return r


async def _get(
    session: AsyncSession, redis_client: Redis, clock: FakeClock, w: World, actor: str, params: dict[str, Any]
) -> Any:
    sid, _ = await login(session, redis_client, clock, getattr(w, actor))
    async with http(session, redis_client, clock) as c:
        r = await c.get(
            f"/api/v1/dr-events/{w.event_id}/blockers", cookies={"drcc_session": sid}, params=params
        )
    await redis_client.delete(f"drcc:session:{sid}")
    return r


async def _blocked(session: AsyncSession, w: World, **blocker: Any) -> tuple[uuid.UUID, uuid.UUID]:
    task_id = await seed_task(session, w, status="BLOCKED")
    await session.execute(text("DELETE FROM blockers WHERE task_id = :t"), {"t": task_id})
    return task_id, await seed_blocker(session, w, task_id, **blocker)


async def test_the_full_lifecycle_over_http_resumes_the_task(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    w = await build_world(session)
    task_id, blocker_id = await _blocked(session, w)

    r = await _post(session, redis_client, clock, w, "manager_id", blocker_id, "assign", {
        "expected_version": 1, "team_id": str(w.other_team_id), "assignee_user_id": str(w.outsider_id),
    })  # fmt: skip
    assert r.status_code == 200, r.text
    assert (r.json()["status"], r.json()["version"]) == ("ASSIGNED", 2)
    assert r.json()["blocker_owner_user_id"] == str(w.outsider_id)

    r = await _post(
        session, redis_client, clock, w, "outsider_id", blocker_id, "start", {"expected_version": 2}
    )
    assert (r.status_code, r.json()["status"]) == (200, "IN_PROGRESS"), r.text

    r = await _post(session, redis_client, clock, w, "outsider_id", blocker_id, "resolve", {
        "expected_version": 3, "resolution_note": "rerouted the VLAN",
    })  # fmt: skip
    assert (r.status_code, r.json()["status"], r.json()["resolution_note"]) == (
        200,
        "RESOLVED",
        "rerouted the VLAN",
    )

    r = await _post(
        session, redis_client, clock, w, "executor_id", blocker_id, "verify", {"expected_version": 4}
    )
    assert (r.status_code, r.json()["status"], r.json()["version"]) == (200, "CLOSED", 6), r.text
    assert r.json()["closed_at"] is not None

    assert (await task_row(session, task_id)).status == "IN_PROGRESS"
    assert sorted(await audit_actions(session, blocker_id)) == [
        "BLOCKER_ASSIGNED",
        "BLOCKER_CLOSED",
        "BLOCKER_RESOLVED",
        "BLOCKER_STARTED",
        "BLOCKER_VERIFIED",
    ]


async def test_verify_replays_the_original_and_never_closes_twice(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    w = await build_world(session)
    task_id, blocker_id = await _blocked(session, w, status="RESOLVED", owner_id=w.outsider_id)
    key = str(uuid.uuid4())

    first = await _post(
        session, redis_client, clock, w, "coordinator_id", blocker_id, "verify", {"expected_version": 1}, key
    )
    replay = await _post(
        session, redis_client, clock, w, "coordinator_id", blocker_id, "verify", {"expected_version": 1}, key
    )

    assert first.status_code == 200, first.text
    assert (replay.status_code, replay.json()) == (200, first.json())
    assert (await blocker_row(session, blocker_id)).version == 3
    assert [a for a in await audit_actions(session, task_id) if a == "TASK_RESUMED"] == ["TASK_RESUMED"]


async def test_the_event_list_is_the_active_set_with_team_queue_and_history_filters(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    w = await build_world(session)
    _, open_mine = await _blocked(session, w, team_id=w.team_id)
    _, assigned_other = await _blocked(
        session, w, status="ASSIGNED", team_id=w.other_team_id, owner_id=w.outsider_id
    )
    task_id, closed = await _blocked(session, w, status="CLOSED", team_id=w.team_id)
    await session.execute(text("UPDATE tasks SET status = 'IN_PROGRESS' WHERE id = :t"), {"t": task_id})

    def ids(r: Any) -> set[str]:
        return {b["id"] for b in r.json()["blockers"]}

    assert ids(await _get(session, redis_client, clock, w, "executor_id", {})) == {
        str(open_mine),
        str(assigned_other),
    }
    assert ids(await _get(session, redis_client, clock, w, "executor_id", {"team_id": str(w.team_id)})) == {
        str(open_mine)
    }
    assert ids(await _get(session, redis_client, clock, w, "executor_id", {"status": "ASSIGNED"})) == {
        str(assigned_other)
    }
    assert ids(await _get(session, redis_client, clock, w, "executor_id", {"task_id": str(task_id)})) == set()
    assert ids(await _get(session, redis_client, clock, w, "executor_id", {"include_closed": "true"})) == {
        str(open_mine), str(assigned_other), str(closed),
    }  # fmt: skip
    r = await _get(session, redis_client, clock, w, "executor_id", {"status": "BOGUS"})
    assert r.status_code == 422


async def test_the_list_is_404_for_an_invisible_event(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    w = await build_world(session)
    await _blocked(session, w)

    r = await _get(session, redis_client, clock, w, "stranger_id", {})

    assert (r.status_code, r.json()["error"]["code"]) == (404, "DR_EVENT_NOT_FOUND")


async def test_assign_with_an_unknown_team_is_404(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    w = await build_world(session)
    _, blocker_id = await _blocked(session, w)

    r = await _post(session, redis_client, clock, w, "coordinator_id", blocker_id, "assign", {
        "expected_version": 1, "team_id": str(uuid.uuid4()),
    })  # fmt: skip

    assert (r.status_code, r.json()["error"]["code"]) == (404, "TEAM_NOT_FOUND")
    assert (await blocker_row(session, blocker_id)).status == "OPEN"

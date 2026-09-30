"""`GET/POST /dr-events/{id}/milestones` and `POST /milestones/{id}/confirm` (API_CONTRACT.md:150-151)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import FakeClock
from app.policies_admin.commands import set_policy_value
from tests.factories import (
    World,
    audit_actions,
    build_world,
    headers,
    http,
    login,
    outbox_types,
    seed_contribution,
    seed_foreign_task,
    seed_milestone,
    seed_task,
)

pytestmark = [pytest.mark.api]


async def _post(
    session: AsyncSession, redis_client: Redis, clock: FakeClock, w: World, actor: str, body: dict[str, Any]
) -> Any:
    sid, csrf = await login(session, redis_client, clock, getattr(w, actor))
    async with http(session, redis_client, clock) as c:
        r = await c.post(
            f"/api/v1/dr-events/{w.event_id}/milestones",
            cookies={"drcc_session": sid},
            headers=headers(csrf),
            json=body,
        )
    await redis_client.delete(f"drcc:session:{sid}")
    return r


async def test_create_with_contributions_and_gates(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    w = await build_world(session)
    required, optional, gated = (
        await seed_task(session, w),
        await seed_task(session, w),
        await seed_task(session, w),
    )

    r = await _post(
        session,
        redis_client,
        clock,
        w,
        "coordinator_id",
        {
            "name": "Network Ready",
            "work_stream_id": str(w.work_stream_id),
            "owner_user_id": str(w.ws_lead_id),
            "target_at": "2026-10-01T12:00:00+00:00",
            "contributing_tasks": [
                {"task_id": str(required)},
                {"task_id": str(optional), "is_required": False},
            ],
            "gates": [{"task_id": str(gated)}],
        },
    )

    assert r.status_code == 201, r.text
    body = r.json()
    assert (body["status"], body["confirmation_mode"], body["version"]) == ("NOT_STARTED", "MANUAL", 1)
    assert body["target_at"].startswith("2026-10-01T12:00:00")
    assert {(c["task_id"], c["is_required"]) for c in body["contributing_tasks"]} == {
        (str(required), True),
        (str(optional), False),
    }
    assert [(g["task_id"], g["strength"]) for g in body["gates"]] == [(str(gated), "HARD")]
    milestone_id = uuid.UUID(body["id"])
    assert await audit_actions(session, milestone_id) == ["MILESTONE_CREATED"]
    assert await outbox_types(session, milestone_id) == ["MilestoneChanged"]
    assert "MILESTONE_GATE_ADDED" in await audit_actions(session, gated)


async def test_a_milestone_whose_work_is_already_done_starts_ready(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    w = await build_world(session)
    done = await seed_task(session, w, status="COMPLETED")

    r = await _post(
        session, redis_client, clock, w, "coordinator_id",
        {
            "name": "Storage Ready", "work_stream_id": str(w.work_stream_id),
            "contributing_tasks": [{"task_id": str(done)}],
        },
    )  # fmt: skip

    assert r.status_code == 201, r.text
    assert (r.json()["status"], r.json()["version"]) == ("READY_FOR_CONFIRMATION", 2)
    assert r.json()["ready_for_confirmation_at"] is not None


async def test_a_milestone_needs_a_stream_or_an_application(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    w = await build_world(session)

    r = await _post(session, redis_client, clock, w, "coordinator_id", {"name": "Floating"})

    assert (r.status_code, r.json()["error"]["code"]) == (422, "MILESTONE_CONTEXT_REQUIRED")


@pytest.mark.parametrize("field", ["work_stream_id", "dr_application_id", "contributing_tasks", "gates"])
async def test_references_must_belong_to_the_event(
    session: AsyncSession, redis_client: Redis, clock: FakeClock, field: str
) -> None:
    w = await build_world(session)
    _, foreign_task = await seed_foreign_task(session, w)
    body: dict[str, Any] = {"name": "M", "work_stream_id": str(w.work_stream_id)}
    if field in ("work_stream_id", "dr_application_id"):
        body[field] = str(uuid.uuid4())
    else:
        body[field] = [{"task_id": str(foreign_task)}]

    r = await _post(session, redis_client, clock, w, "admin_id", body)

    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "CROSS_EVENT_REFERENCE"
    assert r.json()["error"]["details"]["field"] == field
    count = (
        await session.execute(
            text("SELECT count(*) FROM milestones WHERE dr_event_id = :e"), {"e": w.event_id}
        )
    ).scalar_one()
    assert count == 0


async def test_an_unknown_owner_is_404(session: AsyncSession, redis_client: Redis, clock: FakeClock) -> None:
    w = await build_world(session)

    r = await _post(
        session, redis_client, clock, w, "coordinator_id",
        {"name": "M", "work_stream_id": str(w.work_stream_id), "owner_user_id": str(uuid.uuid4())},
    )  # fmt: skip

    assert (r.status_code, r.json()["error"]["code"]) == (404, "USER_NOT_FOUND")


async def test_automatic_confirmation_needs_the_policy(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    """D-225 `milestone.auto_confirm_allowed=false` by default: an AUTOMATIC Milestone is refused
    rather than silently created as one that can never auto-confirm."""
    w = await build_world(session)
    body = {"name": "Low risk", "work_stream_id": str(w.work_stream_id), "confirmation_mode": "AUTOMATIC"}

    refused = await _post(session, redis_client, clock, w, "coordinator_id", body)
    await set_policy_value(
        session, actor_id=w.admin_id, key="milestone.auto_confirm_allowed", value=True,
        scope_type="DR_EVENT", scope_id=w.event_id, clock=clock,
    )  # fmt: skip
    allowed = await _post(session, redis_client, clock, w, "coordinator_id", body)

    assert (refused.status_code, refused.json()["error"]["code"]) == (
        422,
        "MILESTONE_AUTO_CONFIRM_NOT_ALLOWED",
    )
    assert allowed.status_code == 201, allowed.text
    assert allowed.json()["confirmation_mode"] == "AUTOMATIC"


async def test_a_gate_on_a_required_contributor_is_a_cycle(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w)

    r = await _post(
        session, redis_client, clock, w, "coordinator_id",
        {
            "name": "Self-gated", "work_stream_id": str(w.work_stream_id),
            "contributing_tasks": [{"task_id": str(task_id)}], "gates": [{"task_id": str(task_id)}],
        },
    )  # fmt: skip

    assert (r.status_code, r.json()["error"]["code"]) == (409, "DEPENDENCY_CYCLE")


@pytest.mark.parametrize("actor", ["executor_id", "manager_id", "system_owner_id"])
async def test_only_admin_coordinator_or_the_streams_lead_create(
    session: AsyncSession, redis_client: Redis, clock: FakeClock, actor: str
) -> None:
    w = await build_world(session)

    r = await _post(
        session, redis_client, clock, w, actor, {"name": "M", "work_stream_id": str(w.work_stream_id)}
    )

    assert r.status_code == 403, r.text


async def test_the_streams_lead_creates_in_their_stream_only(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    w = await build_world(session)

    mine = await _post(
        session, redis_client, clock, w, "ws_lead_id", {"name": "A", "work_stream_id": str(w.work_stream_id)}
    )
    other = await _post(
        session,
        redis_client,
        clock,
        w,
        "ws_lead_id",
        {"name": "B", "work_stream_id": str(w.other_work_stream_id)},
    )

    assert mine.status_code == 201, mine.text
    assert other.status_code == 403, other.text


async def test_a_non_participant_gets_404(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    w = await build_world(session)

    r = await _post(
        session, redis_client, clock, w, "stranger_id", {"name": "M", "work_stream_id": str(w.work_stream_id)}
    )

    assert (r.status_code, r.json()["error"]["code"]) == (404, "DR_EVENT_NOT_FOUND")


async def test_list_returns_the_events_milestones_with_their_edges(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    w = await build_world(session)
    m = await seed_milestone(session, w)
    task_id = await seed_task(session, w)
    await seed_contribution(session, milestone_id=m, task_id=task_id)
    sid, _ = await login(session, redis_client, clock, w.executor_id)

    async with http(session, redis_client, clock) as c:
        listed = await c.get(f"/api/v1/dr-events/{w.event_id}/milestones", cookies={"drcc_session": sid})
    await redis_client.delete(f"drcc:session:{sid}")

    assert listed.status_code == 200
    [only] = listed.json()["milestones"]
    assert only["id"] == str(m)
    assert only["contributing_tasks"] == [{"task_id": str(task_id), "is_required": True}]


async def test_list_is_404_for_an_invisible_event(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    w = await build_world(session)
    sid, _ = await login(session, redis_client, clock, w.stranger_id)

    async with http(session, redis_client, clock) as c:
        r = await c.get(f"/api/v1/dr-events/{w.event_id}/milestones", cookies={"drcc_session": sid})
    await redis_client.delete(f"drcc:session:{sid}")

    assert r.status_code == 404


async def test_confirm_over_http_and_replay(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    w = await build_world(session)
    m = await seed_milestone(session, w, status="READY_FOR_CONFIRMATION")
    sid, csrf = await login(session, redis_client, clock, w.coordinator_id)
    same = headers(csrf)

    async with http(session, redis_client, clock) as c:
        keyless = await c.post(
            f"/api/v1/milestones/{m}/confirm",
            cookies={"drcc_session": sid},
            headers={"X-CSRF-Token": csrf},
            json={"expected_version": 1},
        )
        first = await c.post(
            f"/api/v1/milestones/{m}/confirm",
            cookies={"drcc_session": sid},
            headers=same,
            json={"expected_version": 1},
        )
        replay = await c.post(
            f"/api/v1/milestones/{m}/confirm",
            cookies={"drcc_session": sid},
            headers=same,
            json={"expected_version": 1},
        )
    await redis_client.delete(f"drcc:session:{sid}")

    assert (keyless.status_code, keyless.json()["error"]["code"]) == (400, "IDEMPOTENCY_KEY_REQUIRED")
    assert first.status_code == 200, first.text
    assert (first.json()["status"], first.json()["version"]) == ("ACHIEVED", 2)
    assert replay.json() == first.json()
    assert await audit_actions(session, m) == ["MILESTONE_ACHIEVED"]


async def test_a_naive_target_time_is_rejected(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    w = await build_world(session)
    naive = datetime(2026, 10, 1, 12, 0, tzinfo=UTC).replace(tzinfo=None).isoformat()

    r = await _post(
        session, redis_client, clock, w, "coordinator_id",
        {"name": "M", "work_stream_id": str(w.work_stream_id), "target_at": naive},
    )  # fmt: skip

    assert r.status_code == 422

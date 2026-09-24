"""Work Streams (API_CONTRACT.md:149, D-227): create/list with `stream_type`, D-222 Lead enrolment,
and the designated Lead's authority (BUILD-06.plan.md Risk #19)."""

from __future__ import annotations

import uuid

import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import FakeClock
from app.core.errors import AppError
from app.tasks_dependencies.dependency_service import DependencyService
from app.tasks_dependencies.transition_service import TaskTransitionService
from app.work_streams.commands import create_work_stream
from tests.factories import audit_actions, build_world, headers, http, login, make_user, seed_task

pytestmark = [pytest.mark.domain]

STREAM_TYPES = ["NETWORK", "STORAGE", "DATABASE", "APPLICATIONS", "MONITORING", "VALIDATION", "CUSTOM"]


@pytest.mark.parametrize("stream_type", STREAM_TYPES)
async def test_every_stream_type_can_be_created(
    session: AsyncSession, clock: FakeClock, stream_type: str
) -> None:
    w = await build_world(session)

    ws = await create_work_stream(
        session,
        actor_id=w.coordinator_id,
        dr_event_id=w.event_id,
        name=f"{stream_type} lane",
        stream_type=stream_type,
        clock=clock,
    )

    assert (ws.stream_type, ws.dr_event_id, ws.version) == (stream_type, w.event_id, 1)
    assert await audit_actions(session, ws.id) == ["WORK_STREAM_CREATED"]


async def test_stream_type_defaults_to_custom(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)

    ws = await create_work_stream(
        session, actor_id=w.admin_id, dr_event_id=w.event_id, name="Misc", clock=clock
    )

    assert ws.stream_type == "CUSTOM"


async def test_the_lead_is_enrolled_as_a_participant(session: AsyncSession, clock: FakeClock) -> None:
    """D-222: users who lead a Work Stream are auto-enrolled."""
    w = await build_world(session)
    lead = await make_user(session, "New lead")

    await create_work_stream(
        session,
        actor_id=w.coordinator_id,
        dr_event_id=w.event_id,
        name="Monitoring",
        stream_type="MONITORING",
        lead_user_id=lead,
        clock=clock,
    )

    source = await session.scalar(
        text(
            "SELECT source FROM dr_event_participants "
            "WHERE dr_event_id = :e AND user_id = :u AND removed_at IS NULL"
        ),
        {"e": w.event_id, "u": lead},
    )
    assert source == "WORK_STREAM_LEAD"


async def test_names_are_unique_per_event_case_insensitively(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)
    await create_work_stream(
        session, actor_id=w.admin_id, dr_event_id=w.event_id, name="Database", clock=clock
    )

    with pytest.raises(AppError) as exc:
        await create_work_stream(
            session, actor_id=w.admin_id, dr_event_id=w.event_id, name="dataBASE", clock=clock
        )

    assert (exc.value.code, exc.value.status_code) == ("WORK_STREAM_EXISTS", 409)


@pytest.mark.auth
@pytest.mark.parametrize("who", ["ws_lead_id", "executor_id", "manager_id", "system_owner_id"])
async def test_only_admin_and_coordinator_create_work_streams(
    session: AsyncSession, clock: FakeClock, who: str
) -> None:
    w = await build_world(session)

    with pytest.raises(AppError) as exc:
        await create_work_stream(
            session, actor_id=getattr(w, who), dr_event_id=w.event_id, name="X", clock=clock
        )

    assert exc.value.status_code == 403


@pytest.mark.auth
async def test_an_event_the_actor_cannot_see_is_404(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)

    with pytest.raises(AppError) as exc:
        await create_work_stream(
            session, actor_id=w.stranger_id, dr_event_id=w.event_id, name="X", clock=clock
        )

    assert (exc.value.code, exc.value.status_code) == ("DR_EVENT_NOT_FOUND", 404)


@pytest.mark.parametrize(
    ("field", "code"), [("lead_user_id", "USER_NOT_FOUND"), ("owning_team_id", "TEAM_NOT_FOUND")]
)
async def test_unknown_lead_or_team_is_404(
    session: AsyncSession, clock: FakeClock, field: str, code: str
) -> None:
    w = await build_world(session)
    unknown = uuid.uuid4()

    with pytest.raises(AppError) as exc:
        await create_work_stream(
            session,
            actor_id=w.admin_id,
            dr_event_id=w.event_id,
            name="X",
            lead_user_id=unknown if field == "lead_user_id" else None,
            owning_team_id=unknown if field == "owning_team_id" else None,
            clock=clock,
        )

    assert (exc.value.code, exc.value.status_code) == (code, 404)


# --------------------------------------------------------------------------------------------
# The designated Lead holds Work Stream Lead authority in their own stream (Risk #19)
# --------------------------------------------------------------------------------------------


async def _led_stream(
    session: AsyncSession, clock: FakeClock, coordinator: uuid.UUID, event: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID]:
    """A new stream whose only link to its Lead is `lead_user_id` -- no WORK_STREAM_LEAD role grant."""
    lead = await make_user(session, "Designated lead")
    ws = await create_work_stream(
        session, actor_id=coordinator, dr_event_id=event, name="Storage lane", lead_user_id=lead, clock=clock
    )
    return lead, ws.id  # read now: seed_task() expires the session


async def test_the_designated_lead_validates_their_streams_shared_work(
    session: AsyncSession, clock: FakeClock
) -> None:
    """D-224's "every Work Stream has a Lead" is `lead_user_id`; D-209: shared Work Stream work is
    validated by its Lead."""
    w = await build_world(session)
    lead, ws_id = await _led_stream(session, clock, w.coordinator_id, w.event_id)
    task_id = await seed_task(
        session, w, status="READY_FOR_VALIDATION", app_scoped=False, work_stream_id=ws_id
    )

    task = await TaskTransitionService.validate(
        session, actor_id=lead, task_id=task_id, expected_version=1, approve=True, clock=clock
    )

    assert task.status == "COMPLETED"


async def test_the_designated_lead_executes_and_edits_dependencies_in_their_stream(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)
    lead, ws_id = await _led_stream(session, clock, w.coordinator_id, w.event_id)
    a = await seed_task(session, w, app_scoped=False, work_stream_id=ws_id)
    b = await seed_task(session, w, app_scoped=False, work_stream_id=ws_id)

    await DependencyService.add_task_dependency(
        session, actor_id=lead, predecessor_task_id=a, successor_task_id=b, clock=clock
    )
    started = await TaskTransitionService.start(
        session, actor_id=lead, task_id=a, expected_version=1, clock=clock
    )

    assert started.status == "IN_PROGRESS"


async def test_leading_one_stream_confers_nothing_in_another(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)
    lead, _ = await _led_stream(session, clock, w.coordinator_id, w.event_id)
    elsewhere = await seed_task(
        session, w, status="READY_FOR_VALIDATION", app_scoped=False
    )  # the world's stream

    with pytest.raises(AppError) as exc:
        await TaskTransitionService.validate(
            session, actor_id=lead, task_id=elsewhere, expected_version=1, approve=True, clock=clock
        )

    assert exc.value.status_code == 403


# --------------------------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------------------------


@pytest.mark.api
async def test_create_and_list_over_http(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    w = await build_world(session)
    sid, csrf = await login(session, redis_client, clock, w.coordinator_id)
    same = headers(csrf)
    body = {
        "name": "Monitoring",
        "stream_type": "MONITORING",
        "lead_user_id": str(w.ws_lead_id),
        "sequence_order": 5,
    }
    url = f"/api/v1/dr-events/{w.event_id}/work-streams"

    async with http(session, redis_client, clock) as c:
        keyless = await c.post(url, cookies={"drcc_session": sid}, headers={"X-CSRF-Token": csrf}, json=body)
        first = await c.post(url, cookies={"drcc_session": sid}, headers=same, json=body)
        replay = await c.post(url, cookies={"drcc_session": sid}, headers=same, json=body)
        bad = await c.post(
            url,
            cookies={"drcc_session": sid},
            headers=headers(csrf),
            json={"name": "Y", "stream_type": "LOGISTICS"},
        )
        listed = await c.get(url, cookies={"drcc_session": sid})

    assert (keyless.status_code, keyless.json()["error"]["code"]) == (400, "IDEMPOTENCY_KEY_REQUIRED")
    assert first.status_code == replay.status_code == 201
    assert replay.json() == first.json()
    assert (first.json()["stream_type"], first.json()["lead_user_id"]) == ("MONITORING", str(w.ws_lead_id))
    assert bad.status_code == 422  # stream_type is a closed enum (D-227)
    names = [s["name"] for s in listed.json()["work_streams"]]
    assert names.count("Monitoring") == 1
    assert {"Network", "Storage"} <= set(names)
    await redis_client.delete(f"drcc:session:{sid}")


@pytest.mark.api
async def test_list_is_404_for_an_invisible_event(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    w = await build_world(session)
    sid, _ = await login(session, redis_client, clock, w.stranger_id)

    async with http(session, redis_client, clock) as c:
        r = await c.get(f"/api/v1/dr-events/{w.event_id}/work-streams", cookies={"drcc_session": sid})

    assert r.status_code == 404
    await redis_client.delete(f"drcc:session:{sid}")

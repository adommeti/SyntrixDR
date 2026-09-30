"""What Work Streams and Tasks unlock on the DR Event side: D-227's Monitoring closure guard on
`close`, three D-224 readiness keys BUILD-04 couldn't compute yet, and `rpo_not_applicable` in the
Event detail (BUILD-06.plan.md session c)."""

from __future__ import annotations

import uuid

import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import FakeClock
from app.core.errors import AppError
from app.dr_events.models import DrEvent
from app.dr_events.readiness_service import evaluate_readiness
from app.dr_events.transition_service import DrEventTransitionService
from app.work_streams.commands import create_work_stream
from tests.factories import (
    World,
    audit_actions,
    build_world,
    count,
    headers,
    http,
    login,
    seed_milestone,
    seed_task,
)

pytestmark = [pytest.mark.transitions]


async def closable(session: AsyncSession, w: World) -> None:
    """FAILBACK_IN_PROGRESS -> CLOSED is legal and skips the FAILED_OVER failback guard."""
    await session.execute(
        text("UPDATE dr_events SET status = 'FAILBACK_IN_PROGRESS' WHERE id = :e"), {"e": w.event_id}
    )


async def monitoring_stream(session: AsyncSession, w: World, clock: FakeClock) -> uuid.UUID:
    ws = await create_work_stream(
        session,
        actor_id=w.admin_id,
        dr_event_id=w.event_id,
        name="Monitoring",
        stream_type="MONITORING",
        clock=clock,
    )
    return ws.id


async def close(
    session: AsyncSession,
    w: World,
    clock: FakeClock,
    *,
    actor: uuid.UUID | None = None,
    exception: str | None = None,
) -> DrEvent:
    return await DrEventTransitionService.close(
        session,
        actor_id=actor or w.coordinator_id,
        event_id=w.event_id,
        expected_version=1,
        closure_exception_reason=exception,
        clock=clock,
    )


async def event_status(session: AsyncSession, w: World) -> str:
    return await session.scalar(text("SELECT status FROM dr_events WHERE id = :e"), {"e": w.event_id})


# --------------------------------------------------------------------------------------------
# D-227: Monitoring closure guard
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["NOT_STARTED", "IN_PROGRESS", "BLOCKED", "READY_FOR_VALIDATION"])
async def test_unfinished_monitoring_work_blocks_close(
    session: AsyncSession, clock: FakeClock, status: str
) -> None:
    w = await build_world(session)
    ws = await monitoring_stream(session, w, clock)
    task_id = await seed_task(session, w, status=status, app_scoped=False, work_stream_id=ws)
    await closable(session, w)

    with pytest.raises(AppError) as exc:
        await close(session, w, clock)

    assert (exc.value.code, exc.value.status_code) == ("MONITORING_CLOSURE_WARNING", 409)
    assert exc.value.details == {"monitoring_task_ids": [str(task_id)]}
    assert await event_status(session, w) == "FAILBACK_IN_PROGRESS"


@pytest.mark.parametrize("status", ["COMPLETED", "CANCELLED"])
async def test_finished_or_cancelled_monitoring_work_does_not_block(
    session: AsyncSession, clock: FakeClock, status: str
) -> None:
    w = await build_world(session)
    ws = await monitoring_stream(session, w, clock)
    await seed_task(session, w, status=status, app_scoped=False, work_stream_id=ws)
    await closable(session, w)

    event = await close(session, w, clock)

    assert event.status == "CLOSED"


async def test_unfinished_work_outside_monitoring_streams_does_not_block(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)
    await seed_task(session, w, status="IN_PROGRESS")  # the world's stream is named Network but typed CUSTOM
    await closable(session, w)

    assert (await close(session, w, clock)).status == "CLOSED"


async def test_an_audited_closure_exception_closes_anyway(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)
    ws = await monitoring_stream(session, w, clock)
    task_id = await seed_task(session, w, status="IN_PROGRESS", app_scoped=False, work_stream_id=ws)
    await closable(session, w)

    event = await close(session, w, clock, exception="Monitoring moves to BAU tooling")

    assert event.status == "CLOSED"
    override = (
        await session.execute(
            text("SELECT override_type, target_type, reason, metadata FROM overrides WHERE dr_event_id = :e"),
            {"e": w.event_id},
        )
    ).one()
    assert (override.override_type, override.target_type) == ("CLOSURE_EXCEPTION", "DR_EVENT")
    assert override.reason == "Monitoring moves to BAU tooling"
    assert override.metadata["monitoring_task_ids"] == [str(task_id)]
    assert "CLOSURE_EXCEPTION_RECORDED" in await audit_actions(session, w.event_id)


@pytest.mark.parametrize("reason", ["", "   "])
async def test_a_blank_closure_exception_is_no_exception(
    session: AsyncSession, clock: FakeClock, reason: str
) -> None:
    w = await build_world(session)
    ws = await monitoring_stream(session, w, clock)
    await seed_task(session, w, status="IN_PROGRESS", app_scoped=False, work_stream_id=ws)
    await closable(session, w)

    with pytest.raises(AppError) as exc:
        await close(session, w, clock, exception=reason)

    assert (exc.value.code, exc.value.status_code) == ("OVERRIDE_REASON_REQUIRED", 400)


async def test_no_exception_is_recorded_when_nothing_needed_one(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)
    await closable(session, w)

    await close(session, w, clock, exception="Just in case")

    assert await count(session, "SELECT count(*) FROM overrides WHERE dr_event_id = :e", e=w.event_id) == 0


async def test_a_closure_exception_never_bypasses_the_child_event_hard_stop(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)
    await session.execute(
        text(
            "INSERT INTO dr_events "
            "(id, parent_dr_event_id, name, event_type, status, version, created_by_user_id) "
            "VALUES (:id, :p, 'Child', 'PLANNED_DR', 'ACTIVE', 1, :u)"
        ),
        {"id": uuid.uuid4(), "p": w.event_id, "u": w.admin_id},
    )
    await closable(session, w)

    with pytest.raises(AppError) as exc:
        await close(session, w, clock, exception="Close it all")

    assert exc.value.code == "CLOSURE_HARD_STOP"


@pytest.mark.api
async def test_closure_exception_over_http(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    w = await build_world(session)
    ws = await monitoring_stream(session, w, clock)
    await seed_task(session, w, status="IN_PROGRESS", app_scoped=False, work_stream_id=ws)
    await closable(session, w)
    sid, csrf = await login(session, redis_client, clock, w.coordinator_id)
    url = f"/api/v1/dr-events/{w.event_id}/close"

    async with http(session, redis_client, clock) as c:
        warned = await c.post(
            url, cookies={"drcc_session": sid}, headers=headers(csrf), json={"expected_version": 1}
        )
        closed = await c.post(
            url,
            cookies={"drcc_session": sid},
            headers=headers(csrf),
            json={"expected_version": 1, "closure_exception": {"reason": "Handed to BAU"}},
        )

    assert (warned.status_code, warned.json()["error"]["code"]) == (409, "MONITORING_CLOSURE_WARNING")
    assert (closed.status_code, closed.json()["status"]) == (200, "CLOSED")
    await redis_client.delete(f"drcc:session:{sid}")


# --------------------------------------------------------------------------------------------
# D-224 readiness keys now computable
# --------------------------------------------------------------------------------------------


async def readiness_of(session: AsyncSession, event_id: uuid.UUID) -> dict[str, tuple[bool, str]]:
    event = await session.get(DrEvent, event_id)
    assert event is not None
    return {r.key: (r.satisfied, r.severity) for r in await evaluate_readiness(session, event)}


async def test_work_stream_lead_warns_until_every_stream_has_a_lead(session: AsyncSession) -> None:
    w = await build_world(session)  # two streams, neither with a lead
    assert (await readiness_of(session, w.event_id))["readiness.work_stream_lead"] == (False, "WARNING")

    await session.execute(
        text("UPDATE work_streams SET lead_user_id = :u WHERE dr_event_id = :e"),
        {"u": w.ws_lead_id, "e": w.event_id},
    )

    assert (await readiness_of(session, w.event_id))["readiness.work_stream_lead"] == (True, "WARNING")


async def test_work_stream_lead_passes_with_no_streams_at_all(session: AsyncSession) -> None:
    """Risk #24: nothing to lead; unlike the in-scope-App keys, an empty set protects nothing here."""
    w = await build_world(session)
    await session.execute(
        text("UPDATE work_streams SET deleted_at = now() WHERE dr_event_id = :e"), {"e": w.event_id}
    )

    assert (await readiness_of(session, w.event_id))["readiness.work_stream_lead"][0] is True


async def test_monitoring_task_present_needs_a_live_monitoring_task(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)
    assert (await readiness_of(session, w.event_id))["readiness.monitoring_task_present"] == (
        False,
        "WARNING",
    )

    ws = await monitoring_stream(session, w, clock)
    cancelled = await seed_task(session, w, status="CANCELLED", app_scoped=False, work_stream_id=ws)
    assert (await readiness_of(session, w.event_id))["readiness.monitoring_task_present"][0] is False

    await session.execute(text("UPDATE tasks SET status = 'NOT_STARTED' WHERE id = :t"), {"t": cancelled})
    assert (await readiness_of(session, w.event_id))["readiness.monitoring_task_present"][0] is True


async def test_every_task_has_an_owning_team_fails_when_that_team_is_deleted(session: AsyncSession) -> None:
    """The column is NOT NULL, so the one way this can fail is the Team itself being soft-deleted."""
    w = await build_world(session)
    await seed_task(session, w)
    assert (await readiness_of(session, w.event_id))["readiness.task_owning_team"] == (True, "HARD_STOP")

    await session.execute(text("UPDATE teams SET deleted_at = now() WHERE id = :t"), {"t": w.team_id})

    assert (await readiness_of(session, w.event_id))["readiness.task_owning_team"] == (False, "HARD_STOP")


# --------------------------------------------------------------------------------------------
# rpo_not_applicable in the Event detail (D-224)
# --------------------------------------------------------------------------------------------


@pytest.mark.api
async def test_event_detail_lists_its_dr_applications_with_rpo_settings(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    w = await build_world(session)
    await session.execute(
        text("UPDATE dr_applications SET rpo_not_applicable = TRUE, failback_required = FALSE WHERE id = :d"),
        {"d": w.dr_application_id},
    )
    sid, _ = await login(session, redis_client, clock, w.executor_id)

    async with http(session, redis_client, clock) as c:
        r = await c.get(f"/api/v1/dr-events/{w.event_id}", cookies={"drcc_session": sid})

    assert r.status_code == 200
    [app] = r.json()["dr_applications"]
    assert (app["id"], app["application_id"]) == (str(w.dr_application_id), str(w.application_id))
    assert (app["rpo_not_applicable"], app["rpo_target_minutes"], app["failback_required"]) == (
        True,
        None,
        False,
    )
    assert (app["status"], app["rto_target_minutes"]) == ("NOT_STARTED", 120)
    await redis_client.delete(f"drcc:session:{sid}")


# --------------------------------------------------------------------------------------------
# Every audit row about a DR Event carries its id (found by the session-d negative matrix)
# --------------------------------------------------------------------------------------------


async def test_every_lifecycle_audit_row_is_linked_to_its_event(
    session: AsyncSession, clock: FakeClock
) -> None:
    """BUILD-04 wrote the Event's own lifecycle, creation, readiness-override and per-DR-Application
    audit rows with `dr_event_id` NULL, so none of them appeared in the Event's trail
    (`ix_audit_event_time`). Walk one Event through its whole lifecycle and check every row."""
    from app.dr_events.commands import create_event

    w = await build_world(session)
    event = await create_event(
        session,
        actor_id=w.admin_id,
        name="Audit trail",
        event_type="PLANNED_DR",
        application_ids=[w.application_id],
        clock=clock,
    )
    event_id = event.id
    svc = DrEventTransitionService
    await svc.activate(
        session,
        actor_id=w.admin_id,
        event_id=event_id,
        expected_version=1,
        override_reason="Drill",
        clock=clock,
    )
    await svc.start_failover(session, actor_id=w.admin_id, event_id=event_id, expected_version=2, clock=clock)
    await svc.mark_failed_over(
        session, actor_id=w.admin_id, event_id=event_id, expected_version=3, clock=clock
    )
    await svc.start_failback(session, actor_id=w.admin_id, event_id=event_id, expected_version=4, clock=clock)
    await svc.close(session, actor_id=w.admin_id, event_id=event_id, expected_version=5, clock=clock)

    rows = (
        await session.execute(
            text(
                "SELECT action, dr_event_id FROM audit_events WHERE entity_id = :e "
                "OR entity_id IN (SELECT id FROM dr_applications WHERE dr_event_id = :e)"
            ),
            {"e": event_id},
        )
    ).all()
    actions = {r.action for r in rows}
    assert {
        "DR_EVENT_CREATED",
        "DR_APPLICATION_CREATED",
        "READINESS_OVERRIDE_RECORDED",
        "DR_EVENT_ACTIVATED",
        "DR_EVENT_FAILOVER_STARTED",
        "DR_APPLICATION_RECOVERING",
        "DR_EVENT_FAILED_OVER",
        "DR_EVENT_FAILBACK_STARTED",
        "DR_EVENT_CLOSED",
    } <= actions
    unlinked = sorted(r.action for r in rows if r.dr_event_id != event_id)
    assert unlinked == [], f"audit rows missing dr_event_id: {unlinked}"


# --------------------------------------------------------------------------------------------
# D-224 readiness.critical_milestone_owner (BUILD-07): a critical Milestone is a MANUAL one (ADR-009)
# --------------------------------------------------------------------------------------------


OWNER_KEY = "readiness.critical_milestone_owner"


async def test_a_manual_milestone_without_an_owner_fails_the_key(session: AsyncSession) -> None:
    w = await build_world(session)
    assert (await readiness_of(session, w.event_id))["readiness.critical_milestone_owner"] == (
        True,
        "HARD_STOP",
    )

    await seed_milestone(session, w)

    assert (await readiness_of(session, w.event_id))["readiness.critical_milestone_owner"] == (
        False,
        "HARD_STOP",
    )


async def test_an_owned_or_automatic_milestone_satisfies_the_key(session: AsyncSession) -> None:
    w = await build_world(session)
    await seed_milestone(session, w, owner_id=w.ws_lead_id)
    await seed_milestone(session, w, confirmation_mode="AUTOMATIC")

    assert (await readiness_of(session, w.event_id))["readiness.critical_milestone_owner"] == (
        True,
        "HARD_STOP",
    )


async def test_an_ownerless_manual_milestone_blocks_activation(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)
    event_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO dr_events "
            "(id, name, event_type, status, version, created_by_user_id, event_timezone) "
            "VALUES (:id, 'Planned', 'PLANNED_DR', 'PLANNED', 1, :u, 'UTC')"
        ),
        {"id": event_id, "u": w.admin_id},
    )
    ws = await create_work_stream(session, actor_id=w.admin_id, dr_event_id=event_id, name="Net", clock=clock)
    await seed_milestone(session, w, dr_event_id=event_id, work_stream_id=ws.id)

    with pytest.raises(AppError) as exc:
        await DrEventTransitionService.activate(
            session, actor_id=w.admin_id, event_id=event_id, expected_version=1, clock=clock
        )

    assert exc.value.code == "READINESS_HARD_STOP"
    assert OWNER_KEY in exc.value.details["failed_keys"]

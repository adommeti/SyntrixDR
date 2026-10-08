"""Blocker escalation on the Celery beat (STATE_MACHINES.md §Blocker, D-225 `blocker.escalation_minutes`,
D-241). Fake clock throughout: the timer is `blocked_at + minutes(tier) <= now` while the Blocker is OPEN."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.blockers import jobs as blocker_jobs
from app.blockers.escalation import escalate_due_blockers
from app.blockers.transition_service import BlockerTransitionService
from app.core.clock import FakeClock
from app.jobs.celery_app import celery_app
from app.policies_admin.commands import set_policy_value
from tests.factories import World, audit_actions, build_world, outbox_types, seed_blocker, seed_task

pytestmark = [pytest.mark.domain]

DEFAULT_MINUTES = {"T0": 0, "T1": 15, "T2": 30, "T3": 60, "T4": 120}  # D-225


async def _open_blocker(
    session: AsyncSession, w: World, clock: FakeClock, *, tier: str | None = "T1", status: str = "OPEN"
) -> uuid.UUID:
    task_id = await seed_task(session, w, status="BLOCKED", app_scoped=tier is not None)
    await session.execute(text("DELETE FROM blockers WHERE task_id = :t"), {"t": task_id})
    if tier is not None:
        await session.execute(
            text(
                "UPDATE dr_applications SET effective_tier_id = (SELECT id FROM tiers WHERE code = :code) "
                "WHERE id = :d"
            ),
            {"code": f"TIER_{tier[1]}", "d": w.dr_application_id},
        )
    return await seed_blocker(session, w, task_id, status=status, blocked_at=clock.now())


async def _alerts(session: AsyncSession, blocker_id: uuid.UUID) -> list[Any]:
    rows = await session.execute(
        text(
            "SELECT alert_type, severity, dr_event_id, resolved_at, id FROM alerts "
            "WHERE target_type = 'BLOCKER' AND target_id = :b ORDER BY created_at"
        ),
        {"b": blocker_id},
    )
    return list(rows)


async def _notifications(session: AsyncSession, user_id: uuid.UUID) -> list[Any]:
    rows = await session.execute(
        text("SELECT notification_type, severity, alert_id, target_id FROM notifications WHERE user_id = :u"),
        {"u": user_id},
    )
    return list(rows)


async def test_t0_escalates_on_the_first_sweep(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)
    blocker_id = await _open_blocker(session, w, clock, tier="T0")

    assert await escalate_due_blockers(session, clock) == [blocker_id]
    [alert] = await _alerts(session, blocker_id)
    assert (alert.alert_type, alert.severity, alert.dr_event_id) == (
        "BLOCKER_ESCALATED",
        "CRITICAL",
        w.event_id,
    )


@pytest.mark.parametrize("tier", ["T1", "T2", "T3", "T4"])
async def test_tier_timers_come_from_policy_defaults(
    session: AsyncSession, clock: FakeClock, tier: str
) -> None:
    w = await build_world(session)
    blocker_id = await _open_blocker(session, w, clock, tier=tier)
    minutes = DEFAULT_MINUTES[tier]

    clock.advance(minutes=minutes - 1)
    assert await escalate_due_blockers(session, clock) == []
    assert await audit_actions(session, blocker_id) == []

    clock.advance(minutes=1)
    assert await escalate_due_blockers(session, clock) == [blocker_id]
    assert await audit_actions(session, blocker_id) == ["BLOCKER_ESCALATED"]


async def test_an_event_scoped_policy_override_wins(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)
    await set_policy_value(
        session, actor_id=w.admin_id, key="blocker.escalation_minutes.T1", value=5,
        scope_type="DR_EVENT", scope_id=w.event_id, clock=clock,
    )  # fmt: skip
    blocker_id = await _open_blocker(session, w, clock, tier="T1")

    clock.advance(minutes=4)
    assert await escalate_due_blockers(session, clock) == []
    clock.advance(minutes=1)
    assert await escalate_due_blockers(session, clock) == [blocker_id]


async def test_a_second_sweep_does_not_escalate_twice(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)
    blocker_id = await _open_blocker(session, w, clock, tier="T0")

    await escalate_due_blockers(session, clock)
    clock.advance(hours=1)
    assert await escalate_due_blockers(session, clock) == []

    assert len(await _alerts(session, blocker_id)) == 1
    assert await audit_actions(session, blocker_id) == ["BLOCKER_ESCALATED"]
    assert await outbox_types(session, blocker_id) == ["BlockerChanged"]


@pytest.mark.parametrize("status", ["ASSIGNED", "IN_PROGRESS", "RESOLVED", "CLOSED"])
async def test_only_open_blockers_escalate(session: AsyncSession, clock: FakeClock, status: str) -> None:
    w = await build_world(session)
    await _open_blocker(session, w, clock, tier="T0", status=status)
    clock.advance(hours=3)

    assert await escalate_due_blockers(session, clock) == []


async def test_escalation_writes_alert_coordinator_notification_audit_and_outbox(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)
    await session.execute(
        text("UPDATE dr_events SET coordinator_user_id = :c WHERE id = :e"),
        {"c": w.coordinator_id, "e": w.event_id},
    )
    blocker_id = await _open_blocker(session, w, clock, tier="T1")
    clock.advance(minutes=15)

    await escalate_due_blockers(session, clock)

    [alert] = await _alerts(session, blocker_id)
    assert (alert.severity, alert.resolved_at) == ("HIGH", None)
    [note] = await _notifications(session, w.coordinator_id)
    assert (note.notification_type, note.severity, note.alert_id, note.target_id) == (
        "BLOCKER_ESCALATED", "HIGH", alert.id, blocker_id,
    )  # fmt: skip
    audit = (
        await session.execute(
            text("SELECT actor_type, actor_user_id, after_data FROM audit_events WHERE entity_id = :b"),
            {"b": blocker_id},
        )
    ).one()
    assert (audit.actor_type, audit.actor_user_id) == ("SYSTEM", None)
    assert audit.after_data["tier"] == "T1" and audit.after_data["alert_id"] == str(alert.id)
    assert audit.after_data["notified_user_ids"] == [str(w.coordinator_id)]
    assert await outbox_types(session, blocker_id) == ["BlockerChanged"]


async def test_no_coordinator_means_an_alert_but_no_notification(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)
    blocker_id = await _open_blocker(session, w, clock, tier="T0")

    await escalate_due_blockers(session, clock)

    assert len(await _alerts(session, blocker_id)) == 1
    assert (await session.execute(text("SELECT count(*) FROM notifications"))).scalar_one() == 0


async def test_a_task_without_an_application_uses_the_t4_timer(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)
    blocker_id = await _open_blocker(session, w, clock, tier=None)

    clock.advance(minutes=119)
    assert await escalate_due_blockers(session, clock) == []
    clock.advance(minutes=1)
    assert await escalate_due_blockers(session, clock) == [blocker_id]
    [alert] = await _alerts(session, blocker_id)
    assert alert.severity == "MEDIUM"


async def test_routing_the_blocker_resolves_its_escalation_alert(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)
    blocker_id = await _open_blocker(session, w, clock, tier="T0")
    await escalate_due_blockers(session, clock)

    await BlockerTransitionService.assign(
        session,
        actor_id=w.coordinator_id,
        blocker_id=blocker_id,
        expected_version=1,
        team_id=w.team_id,
        clock=clock,
    )

    [alert] = await _alerts(session, blocker_id)
    assert alert.resolved_at == clock.now()
    clock.advance(hours=1)
    assert await escalate_due_blockers(session, clock) == []


async def test_the_sweep_escalates_in_id_order(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)
    seeded = [await _open_blocker(session, w, clock, tier="T0") for _ in range(3)]
    forced = sorted(uuid.uuid4() for _ in seeded)[::-1]
    for old, new in zip(seeded, forced, strict=True):
        await session.execute(text("UPDATE blockers SET id = :new WHERE id = :old"), {"new": new, "old": old})

    escalated = await escalate_due_blockers(session, clock)

    assert escalated == sorted(forced) and escalated != forced


def test_the_sweep_is_registered_on_the_one_beat() -> None:
    entry = celery_app.conf.beat_schedule["blockers-escalation-sweep"]
    assert (entry["task"], entry["schedule"]) == ("drcc.escalate_blockers", 30.0)
    assert "app.blockers.jobs" in celery_app.conf.include
    assert blocker_jobs.escalate_blockers_task.name == "drcc.escalate_blockers"


async def test_blocked_at_is_the_clock_not_now(session: AsyncSession, clock: FakeClock) -> None:
    """An OPEN Blocker created long ago (blocked_at in the past) is due regardless of wall time."""
    w = await build_world(session)
    task_id = await seed_task(session, w, status="BLOCKED")
    await session.execute(text("DELETE FROM blockers WHERE task_id = :t"), {"t": task_id})
    blocker_id = await seed_blocker(session, w, task_id, blocked_at=clock.now() - timedelta(minutes=15))

    assert await escalate_due_blockers(session, clock) == [blocker_id]

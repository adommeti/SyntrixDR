"""Alerts: shared acknowledgement (audited, reason required for a breach -- FROZEN §9.8) and personal
snooze (FROZEN §12.9), API_CONTRACT.md:218-219."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.comments_mentions_notifications.commands import acknowledge_alert, snooze_alert
from app.core.clock import FakeClock
from app.core.errors import AppError
from tests.factories import audit_actions, build_world, seed_alert

pytestmark = [pytest.mark.domain]


async def _alert(session: AsyncSession, alert_id: uuid.UUID) -> object:
    return (
        await session.execute(
            text("SELECT acknowledged_at, acknowledged_by_user_id FROM alerts WHERE id = :a"), {"a": alert_id}
        )
    ).one()


async def test_acknowledge_is_shared_audited_and_once(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)
    alert_id = await seed_alert(session, w)

    await acknowledge_alert(session, actor_id=w.coordinator_id, alert_id=alert_id, reason=None, clock=clock)

    row = await _alert(session, alert_id)
    assert (row.acknowledged_at, row.acknowledged_by_user_id) == (clock.now(), w.coordinator_id)  # pyright: ignore[reportAttributeAccessIssue]
    assert await audit_actions(session, alert_id) == ["ALERT_ACKNOWLEDGED"]

    with pytest.raises(AppError) as e:
        await acknowledge_alert(session, actor_id=w.admin_id, alert_id=alert_id, reason=None, clock=clock)
    assert (e.value.code, e.value.status_code) == ("ALERT_ALREADY_ACKNOWLEDGED", 409)


@pytest.mark.parametrize("alert_type", ["RTO_BREACH", "RPO_BREACH"])
async def test_a_breach_needs_a_reason(session: AsyncSession, clock: FakeClock, alert_type: str) -> None:
    w = await build_world(session)
    alert_id = await seed_alert(session, w, alert_type=alert_type, severity="CRITICAL")

    with pytest.raises(AppError) as e:
        await acknowledge_alert(
            session, actor_id=w.coordinator_id, alert_id=alert_id, reason="  ", clock=clock
        )
    assert (e.value.code, e.value.status_code) == ("ACKNOWLEDGEMENT_REASON_REQUIRED", 422)
    assert (await _alert(session, alert_id)).acknowledged_at is None  # pyright: ignore[reportAttributeAccessIssue]

    await acknowledge_alert(
        session, actor_id=w.coordinator_id, alert_id=alert_id, reason="late recovery accepted", clock=clock
    )
    reason = (
        await session.execute(
            text("SELECT after_data->>'reason' FROM audit_events WHERE entity_id = :a"), {"a": alert_id}
        )
    ).scalar_one()
    assert reason == "late recovery accepted"


@pytest.mark.parametrize("alert_type", ["SLA_WARNING", "BLOCKER_ESCALATED"])
async def test_non_breach_alerts_do_not_need_a_reason(
    session: AsyncSession, clock: FakeClock, alert_type: str
) -> None:
    w = await build_world(session)
    alert_id = await seed_alert(session, w, alert_type=alert_type)
    await acknowledge_alert(session, actor_id=w.manager_id, alert_id=alert_id, reason=None, clock=clock)
    assert (await _alert(session, alert_id)).acknowledged_at is not None  # pyright: ignore[reportAttributeAccessIssue]


@pytest.mark.parametrize(
    ("actor", "event_scoped", "allowed"),
    [
        ("admin_id", True, True),
        ("admin_id", False, True),
        ("coordinator_id", True, True),
        ("coordinator_id", False, False),  # global alerts: Admin only
        ("ws_lead_id", True, True),
        ("system_owner_id", True, False),  # owner slot, no APP_OWNER role
        ("manager_id", True, True),
        ("executor_id", True, False),
        ("business_owner_id", True, False),
    ],
)
async def test_acknowledge_authority(
    session: AsyncSession, clock: FakeClock, actor: str, event_scoped: bool, allowed: bool
) -> None:
    w = await build_world(session)
    alert_id = await seed_alert(session, w, event_scoped=event_scoped)
    if allowed:
        await acknowledge_alert(
            session, actor_id=getattr(w, actor), alert_id=alert_id, reason=None, clock=clock
        )
    else:
        with pytest.raises(AppError) as e:
            await acknowledge_alert(
                session, actor_id=getattr(w, actor), alert_id=alert_id, reason=None, clock=clock
            )
        assert e.value.status_code in (403, 404)
        assert await audit_actions(session, alert_id) == []


async def test_invisible_event_alert_is_404(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)
    alert_id = await seed_alert(session, w)
    for call in (acknowledge_alert, None):
        with pytest.raises(AppError) as e:
            if call is not None:
                await call(session, actor_id=w.stranger_id, alert_id=alert_id, reason=None, clock=clock)
            else:
                await snooze_alert(
                    session,
                    actor_id=w.stranger_id,
                    alert_id=alert_id,
                    snoozed_until=clock.now() + timedelta(hours=1),
                    clock=clock,
                )
        assert (e.value.code, e.value.status_code) == ("ALERT_NOT_FOUND", 404)


async def test_snooze_is_personal_upserts_and_leaves_the_shared_ack_alone(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)
    alert_id = await seed_alert(session, w)

    first = await snooze_alert(
        session,
        actor_id=w.executor_id,
        alert_id=alert_id,
        snoozed_until=clock.now() + timedelta(minutes=30),
        clock=clock,
    )
    second = await snooze_alert(
        session,
        actor_id=w.executor_id,
        alert_id=alert_id,
        snoozed_until=clock.now() + timedelta(hours=2),
        clock=clock,
    )
    await snooze_alert(
        session,
        actor_id=w.teammate_id,
        alert_id=alert_id,
        snoozed_until=clock.now() + timedelta(hours=1),
        clock=clock,
    )

    assert first.id == second.id and second.snoozed_until == clock.now() + timedelta(hours=2)
    rows = (
        await session.execute(
            text(
                "SELECT user_id, snoozed_until FROM alert_snoozes WHERE alert_id = :a ORDER BY snoozed_until"
            ),
            {"a": alert_id},
        )
    ).all()
    assert [r.user_id for r in rows] == [w.teammate_id, w.executor_id]
    assert (await _alert(session, alert_id)).acknowledged_at is None  # pyright: ignore[reportAttributeAccessIssue]
    assert await audit_actions(session, alert_id) == ["ALERT_SNOOZED", "ALERT_SNOOZED", "ALERT_SNOOZED"]


async def test_snooze_must_be_in_the_future(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)
    alert_id = await seed_alert(session, w)
    with pytest.raises(AppError) as e:
        await snooze_alert(
            session, actor_id=w.executor_id, alert_id=alert_id, snoozed_until=clock.now(), clock=clock
        )
    assert (e.value.code, e.value.status_code) == ("SNOOZE_UNTIL_IN_PAST", 422)

"""Blocker escalation (STATE_MACHINES.md §Blocker: "Tier 0 escalates immediately; lower tiers use
configurable timers"; D-225 `blocker.escalation_minutes.T0..T4`).

An OPEN Blocker that has sat unrouted for the tier's minutes (measured from `blocked_at`) is
escalated once: a `BLOCKER_ESCALATED` audit row (SYSTEM), an Event-scoped `alerts` row, a
`notifications` row for the Event's Coordinator and a `BlockerChanged` outbox event. The unresolved
alert is the "already escalated" marker (no schema change, BUILD-08.plan.md Risk #4); `assign`/`start`
resolve it. ASSIGNED and later Blockers are someone's work and are not escalated (Risk #1)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.applications_catalog.models import Tier
from app.blockers.models import Blocker
from app.blockers.queries import lock_blocker
from app.comments_mentions_notifications.catalog import AlertType, NotificationType, Severity
from app.comments_mentions_notifications.commands import has_unresolved_alert, notify_user, raise_alert
from app.core.audit import write_audit
from app.core.clock import Clock
from app.core.outbox import write_outbox
from app.dr_events.models import DrApplication, DrEvent
from app.policies_admin.queries import PolicyService
from app.tasks_dependencies.models import Task

#: A Task without an Application has no tier: the slowest timer and a middling severity
#: (BUILD-08.plan.md Risk #2). The severity ladder is Risk #3.
NO_TIER_KEY = "T4"
SEVERITY_BY_TIER: dict[str | None, Severity] = {
    "T0": Severity.CRITICAL,
    "T1": Severity.HIGH,
    "T2": Severity.MEDIUM,
    "T3": Severity.LOW,
    "T4": Severity.LOW,
    None: Severity.MEDIUM,
}


@dataclass(frozen=True)
class OpenBlocker:
    blocker_id: uuid.UUID
    task_id: uuid.UUID
    dr_event_id: uuid.UUID
    application_id: uuid.UUID | None
    tier: str | None  # "T0".."T4"
    blocked_at: datetime
    reason: str


def tier_key(tier_code: str | None) -> str | None:
    """`tiers.code` "TIER_n" -> the policy suffix "Tn"."""
    if tier_code is None:
        return None
    return "T" + tier_code.removeprefix("TIER_")


async def open_blockers(session: AsyncSession) -> list[OpenBlocker]:
    """Every live OPEN Blocker with what the timer needs, in id order (the lock order, ADR-045)."""
    rows = await session.execute(
        select(
            Blocker.id,
            Blocker.task_id,
            Task.dr_event_id,
            DrApplication.application_id,
            Tier.code,
            Blocker.blocked_at,
            Blocker.reason,
        )
        .join(Task, Task.id == Blocker.task_id)
        .outerjoin(DrApplication, DrApplication.id == Task.dr_application_id)
        .outerjoin(Tier, Tier.id == DrApplication.effective_tier_id)
        .where(Blocker.deleted_at.is_(None), Blocker.status == "OPEN", Task.deleted_at.is_(None))
        .order_by(Blocker.id)
    )
    return [
        OpenBlocker(
            blocker_id=b,
            task_id=t,
            dr_event_id=e,
            application_id=a,
            tier=tier_key(code),
            blocked_at=at,
            reason=r,
        )
        for b, t, e, a, code, at, r in rows
    ]


async def escalation_minutes(session: AsyncSession, candidate: OpenBlocker) -> int:
    value = await PolicyService.resolve(
        session,
        f"blocker.escalation_minutes.{candidate.tier or NO_TIER_KEY}",
        event_id=candidate.dr_event_id,
        application_id=candidate.application_id,
    )
    return int(value) if value is not None else 0


async def escalate_due_blockers(session: AsyncSession, clock: Clock) -> list[uuid.UUID]:
    """Escalates every OPEN Blocker whose timer has run out and that has no unresolved escalation
    alert yet. Idempotent across sweeps. Takes the caller's session (tests pass theirs); the Celery
    wrapper in `blockers.jobs` owns its engine."""
    now = clock.now()
    escalated: list[uuid.UUID] = []
    for candidate in await open_blockers(session):
        if candidate.blocked_at + timedelta(minutes=await escalation_minutes(session, candidate)) > now:
            continue
        if await has_unresolved_alert(
            session,
            alert_type=AlertType.BLOCKER_ESCALATED,
            target_type="BLOCKER",
            target_id=candidate.blocker_id,
        ):
            continue
        blocker = await lock_blocker(session, candidate.blocker_id)
        if blocker is None or blocker.status != "OPEN":
            continue  # routed between the select and the lock
        await _escalate(session, blocker, candidate, now=now, clock=clock)
        escalated.append(blocker.id)
    return escalated


async def _escalate(
    session: AsyncSession, blocker: Blocker, c: OpenBlocker, *, now: datetime, clock: Clock
) -> None:
    severity = SEVERITY_BY_TIER[c.tier]
    waited = int((now - c.blocked_at).total_seconds() // 60)
    title = f"Blocker escalated ({c.tier or 'no tier'}): unrouted for {waited} min"
    alert = await raise_alert(
        session,
        dr_event_id=c.dr_event_id,
        alert_type=AlertType.BLOCKER_ESCALATED,
        severity=severity,
        title=title,
        body=c.reason,
        target_type="BLOCKER",
        target_id=blocker.id,
        clock=clock,
    )
    event = await session.get(DrEvent, c.dr_event_id)
    coordinator_id = event.coordinator_user_id if event is not None else None
    if coordinator_id is not None:
        await notify_user(
            session,
            user_id=coordinator_id,
            dr_event_id=c.dr_event_id,
            alert=alert,
            notification_type=NotificationType.BLOCKER_ESCALATED,
            severity=severity,
            title=title,
            body=c.reason,
            target_type="BLOCKER",
            target_id=blocker.id,
            clock=clock,
        )
    after = {
        "status": blocker.status,
        "version": blocker.version,
        "escalated_at": now.isoformat(),
        "tier": c.tier,
        "severity": severity.value,
        "alert_id": str(alert.id),
        "notified_user_ids": [str(coordinator_id)] if coordinator_id else [],
    }
    await write_audit(
        session,
        actor_user_id=None,
        actor_type="SYSTEM",
        entity_type="BLOCKER",
        entity_id=blocker.id,
        action="BLOCKER_ESCALATED",
        dr_event_id=c.dr_event_id,
        after=after,
    )
    await write_outbox(
        session,
        aggregate_type="BLOCKER",
        aggregate_id=blocker.id,
        event_type="BlockerChanged",
        payload={"change": "BLOCKER_ESCALATED", "task_id": str(c.task_id), **after},
        clock=clock,
        dr_event_id=c.dr_event_id,
    )

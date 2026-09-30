"""Shared test factories for Task / dependency tests (tests.md: factories live here).

`build_world` seeds one ACTIVE DR Event with a cast of actors covering every authorization case
the Task and dependency rules distinguish; `seed_task` puts a Task into any lifecycle state with
that state's invariants already true. Both write raw SQL where the command that would normally
produce the state doesn't exist yet (Task creation/assignment, D-222 auto-enrolment, Blocker
closing) -- see BUILD-06.plan.md Risk #8.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock, FakeClock
from app.core.database import get_request_session
from app.core.errors import AppError, app_error_handler
from app.dr_events.participants import enrol_participant
from app.dr_events.routes import router as dr_events_router
from app.identity_auth.dependencies import get_clock, get_session_store
from app.identity_auth.models import SessionRecord
from app.identity_auth.session_store import RedisSessionStore
from app.tasks_dependencies.commands import create_draft_task
from app.tasks_dependencies.routes import router as tasks_router
from app.users_teams_org.models import RoleAssignment
from app.work_streams.commands import get_or_create_work_stream
from app.work_streams.routes import router as work_streams_router

# --------------------------------------------------------------------------------------------
# World
# --------------------------------------------------------------------------------------------


@dataclass
class World:
    event_id: uuid.UUID
    team_id: uuid.UUID
    other_team_id: uuid.UUID
    work_stream_id: uuid.UUID
    other_work_stream_id: uuid.UUID
    application_id: uuid.UUID
    other_application_id: uuid.UUID
    dr_application_id: uuid.UUID
    admin_id: uuid.UUID
    executor_id: uuid.UUID  # EXECUTOR role, Owning-Team member, the Task's assignee
    teammate_id: uuid.UUID  # EXECUTOR role, Owning-Team member, not the assignee
    outsider_id: uuid.UUID  # EXECUTOR role, visible participant, not in the Owning Team
    stranger_id: uuid.UUID  # EXECUTOR role, NOT a participant of the Event
    roleless_member_id: uuid.UUID  # Owning-Team member with no EXECUTOR role
    system_owner_id: uuid.UUID  # SYSTEM_APPLICATION slot 2 of the Task's Application
    other_app_owner_id: uuid.UUID  # SYSTEM_APPLICATION slot 1 of a different Application
    business_owner_id: uuid.UUID  # BUSINESS slot 1 of the Task's Application
    ws_lead_id: uuid.UUID  # WORK_STREAM_LEAD at the Task's Work Stream
    manager_id: uuid.UUID  # MANAGER role, Team.manager_user_id of the Owning Team
    coordinator_id: uuid.UUID  # DR_COORDINATOR (GLOBAL), explicit participant


async def make_user(session: AsyncSession, name: str, *, entra: bool = False) -> uuid.UUID:
    user_id = uuid.uuid4()
    if entra:
        await session.execute(
            text(
                "INSERT INTO users (id, identity_type, display_name, email, entra_object_id) "
                "VALUES (:id, 'ENTRA', :name, :email, :oid)"
            ),
            {"id": user_id, "name": name, "email": f"{user_id}@example.test", "oid": str(uuid.uuid4())},
        )
    else:
        await session.execute(
            text(
                "INSERT INTO users (id, identity_type, display_name, email) "
                "VALUES (:id, 'LOCAL', :name, :email)"
            ),
            {"id": user_id, "name": name, "email": f"{user_id}@example.test"},
        )
    return user_id


async def grant_role(
    session: AsyncSession,
    user_id: uuid.UUID,
    role_key: str,
    *,
    scope_type: str = "GLOBAL",
    scope_id: uuid.UUID | None = None,
) -> None:
    session.add(RoleAssignment(user_id=user_id, role_key=role_key, scope_type=scope_type, scope_id=scope_id))
    await session.flush()


async def make_team(session: AsyncSession, name: str, *, manager_id: uuid.UUID | None = None) -> uuid.UUID:
    team_id = uuid.uuid4()
    await session.execute(
        text("INSERT INTO teams (id, name, manager_user_id) VALUES (:id, :name, :mgr)"),
        {"id": team_id, "name": f"{name} {team_id}", "mgr": manager_id},
    )
    return team_id


async def add_member(session: AsyncSession, team_id: uuid.UUID, user_id: uuid.UUID) -> None:
    await session.execute(
        text("INSERT INTO team_memberships (id, team_id, user_id) VALUES (:id, :t, :u)"),
        {"id": uuid.uuid4(), "t": team_id, "u": user_id},
    )


async def make_application(session: AsyncSession, name: str) -> uuid.UUID:
    app_id = uuid.uuid4()
    tier_id = (await session.execute(text("SELECT id FROM tiers WHERE code = 'TIER_1'"))).scalar_one()
    await session.execute(
        text("INSERT INTO applications (id, name, tier_id) VALUES (:id, :name, :tier)"),
        {"id": app_id, "name": f"{name} {app_id}", "tier": tier_id},
    )
    return app_id


async def add_owner_slot(
    session: AsyncSession, app_id: uuid.UUID, user_id: uuid.UUID, *, owner_type: str, slot: int
) -> None:
    await session.execute(
        text(
            "INSERT INTO application_owners "
            "(id, application_id, owner_type, owner_order, user_id, created_at, updated_at) "
            "VALUES (:id, :aid, :otype, :slot, :uid, now(), now())"
        ),
        {"id": uuid.uuid4(), "aid": app_id, "otype": owner_type, "slot": slot, "uid": user_id},
    )


async def build_world(session: AsyncSession) -> World:
    admin_id = await make_user(session, "Admin", entra=True)
    await grant_role(session, admin_id, "GLOBAL_ADMIN")

    event_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO dr_events "
            "(id, name, event_type, status, version, created_by_user_id, created_at, updated_at) "
            "VALUES (:id, 'Task World', 'PLANNED_DR', 'ACTIVE', 1, :creator, now(), now())"
        ),
        {"id": event_id, "creator": admin_id},
    )

    manager_id = await make_user(session, "Manager")
    await grant_role(session, manager_id, "MANAGER")
    team_id = await make_team(session, "Network", manager_id=manager_id)
    other_team_id = await make_team(session, "Storage")

    executor_id = await make_user(session, "Executor")
    teammate_id = await make_user(session, "Teammate")
    outsider_id = await make_user(session, "Outsider")
    stranger_id = await make_user(session, "Stranger")
    for uid in (executor_id, teammate_id, outsider_id, stranger_id):
        await grant_role(session, uid, "EXECUTOR")
    roleless_member_id = await make_user(session, "Roleless member")
    for uid in (executor_id, teammate_id, roleless_member_id):
        await add_member(session, team_id, uid)
    await add_member(session, other_team_id, outsider_id)

    ws = await get_or_create_work_stream(session, dr_event_id=event_id, name="Network", actor_id=admin_id)
    other_ws = await get_or_create_work_stream(
        session, dr_event_id=event_id, name="Storage", actor_id=admin_id
    )

    application_id = await make_application(session, "Billing")
    other_application_id = await make_application(session, "Payroll")
    dr_application_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO dr_applications "
            "(id, dr_event_id, application_id, effective_tier_id, effective_sla_minutes, "
            "rto_target_minutes, status, version, created_at, updated_at) "
            "SELECT :id, :eid, :aid, id, 120, 120, 'NOT_STARTED', 1, now(), now() "
            "FROM tiers WHERE code = 'TIER_1'"
        ),
        {"id": dr_application_id, "eid": event_id, "aid": application_id},
    )

    system_owner_id = await make_user(session, "System owner")
    other_app_owner_id = await make_user(session, "Other app owner")
    business_owner_id = await make_user(session, "Business owner")
    primary_owner_id = await make_user(session, "Primary owner")
    await add_owner_slot(session, application_id, primary_owner_id, owner_type="SYSTEM_APPLICATION", slot=1)
    await add_owner_slot(session, application_id, system_owner_id, owner_type="SYSTEM_APPLICATION", slot=2)
    await add_owner_slot(session, application_id, business_owner_id, owner_type="BUSINESS", slot=1)
    await add_owner_slot(
        session, other_application_id, other_app_owner_id, owner_type="SYSTEM_APPLICATION", slot=1
    )

    coordinator_id = await make_user(session, "Coordinator")
    await grant_role(session, coordinator_id, "DR_COORDINATOR")
    ws_lead_id = await make_user(session, "WS lead")
    await grant_role(session, ws_lead_id, "WORK_STREAM_LEAD", scope_type="WORK_STREAM", scope_id=ws.id)

    # D-222 auto-enrolment isn't implemented yet (BUILD-06.plan.md Risk #8): enrol everyone
    # except the stranger explicitly, with the source auto-enrolment would eventually use.
    for uid, source in (
        (executor_id, "TASK_ASSIGNEE"),
        (teammate_id, "OWNING_TEAM"),
        (roleless_member_id, "OWNING_TEAM"),
        (manager_id, "OWNING_TEAM"),
        (outsider_id, "EXPLICIT"),
        (system_owner_id, "APP_OWNER"),
        (other_app_owner_id, "EXPLICIT"),
        (business_owner_id, "BUSINESS_OWNER"),
        (ws_lead_id, "WORK_STREAM_LEAD"),
        (coordinator_id, "EXPLICIT"),
    ):
        await enrol_participant(session, event_id, uid, source, added_by_user_id=admin_id)
    await session.flush()

    return World(
        event_id=event_id,
        team_id=team_id,
        other_team_id=other_team_id,
        work_stream_id=ws.id,
        other_work_stream_id=other_ws.id,
        application_id=application_id,
        other_application_id=other_application_id,
        dr_application_id=dr_application_id,
        admin_id=admin_id,
        executor_id=executor_id,
        teammate_id=teammate_id,
        outsider_id=outsider_id,
        stranger_id=stranger_id,
        roleless_member_id=roleless_member_id,
        system_owner_id=system_owner_id,
        other_app_owner_id=other_app_owner_id,
        business_owner_id=business_owner_id,
        ws_lead_id=ws_lead_id,
        manager_id=manager_id,
        coordinator_id=coordinator_id,
    )


async def seed_task(
    session: AsyncSession,
    w: World,
    *,
    status: str = "NOT_STARTED",
    app_scoped: bool = True,
    dr_application_id: uuid.UUID | None = None,
    work_stream_id: uuid.UUID | None = None,
    assignee_id: uuid.UUID | None = None,
    evidence_required: bool = False,
    evidence_min_count: int = 1,
    verification_note_required: bool = True,
    active_blocker: bool = True,
    submitter_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """A Task in `status`, consistent with that state's invariants: BLOCKED carries a Blocker
    (active unless `active_blocker=False`), READY_FOR_VALIDATION carries a PENDING Validation."""
    task = await create_draft_task(
        session,
        dr_event_id=w.event_id,
        title=f"Task {uuid.uuid4()}",
        phase="FAILOVER",
        owning_team_id=w.team_id,
        created_by_user_id=w.admin_id,
        dr_application_id=(dr_application_id or w.dr_application_id) if app_scoped else None,
        work_stream_id=work_stream_id or w.work_stream_id,
        current_assignee_user_id=assignee_id or w.executor_id,
        evidence_required=evidence_required,
    )
    task_id = task.id
    await session.execute(
        text(
            "UPDATE tasks SET status = CAST(:s AS task_status), evidence_min_count = :m, "
            "verification_note_required = :v WHERE id = :id"
        ),
        {"s": status, "m": evidence_min_count, "v": verification_note_required, "id": task_id},
    )
    if status == "BLOCKED":
        await session.execute(
            text(
                "INSERT INTO blockers (id, task_id, reason, status, created_by_user_id, "
                "blocked_at, created_at, updated_at) "
                "VALUES (:id, :tid, 'seeded', CAST(:st AS blocker_status), :uid, now(), now(), now())"
            ),
            {
                "id": uuid.uuid4(),
                "tid": task_id,
                "st": "OPEN" if active_blocker else "CLOSED",
                "uid": w.executor_id,
            },
        )
    if status == "READY_FOR_VALIDATION":
        await session.execute(
            text(
                "INSERT INTO validations (id, target_type, target_id, status, submitted_at, "
                "verification_note, created_by_user_id, created_at, updated_at) "
                "VALUES (:id, 'TASK', :tid, 'PENDING', now(), 'seeded', :uid, now(), now())"
            ),
            {"id": uuid.uuid4(), "tid": task_id, "uid": submitter_id or w.executor_id},
        )
    await session.flush()
    session.expire_all()  # raw SQL above bypassed the ORM; never read a stale cached Task
    return task_id


# --------------------------------------------------------------------------------------------
# Probes
# --------------------------------------------------------------------------------------------


async def task_row(session: AsyncSession, task_id: uuid.UUID) -> Any:
    return (
        await session.execute(
            text("SELECT status, version, started_at, completed_at FROM tasks WHERE id = :id"),
            {"id": task_id},
        )
    ).one()


async def audit_actions(session: AsyncSession, entity_id: uuid.UUID) -> list[str]:
    rows = await session.execute(
        text("SELECT action FROM audit_events WHERE entity_id = :id ORDER BY action"),
        {"id": entity_id},
    )
    return [r.action for r in rows]


async def outbox_types(session: AsyncSession, aggregate_id: uuid.UUID) -> list[str]:
    rows = await session.execute(
        text("SELECT event_type FROM outbox_events WHERE aggregate_id = :id"), {"id": aggregate_id}
    )
    return [r.event_type for r in rows]


async def count(session: AsyncSession, sql: str, **params: object) -> int:
    return (await session.execute(text(sql), params)).scalar_one()


class FixedEvidence:
    def __init__(self, n: int) -> None:
        self.n = n

    async def count_acceptable_for_task(self, session: AsyncSession, task_id: uuid.UUID) -> int:
        _ = (session, task_id)
        return self.n


async def depend(
    session: AsyncSession, w: World, *, predecessor: uuid.UUID, successor: uuid.UUID, strength: str = "HARD"
) -> uuid.UUID:
    row_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO task_dependencies "
            "(id, dr_event_id, predecessor_task_id, successor_task_id, strength, created_by_user_id) "
            "VALUES (:id, :eid, :p, :s, CAST(:st AS dependency_strength), :uid)"
        ),
        {
            "id": row_id,
            "eid": w.event_id,
            "p": predecessor,
            "s": successor,
            "st": strength,
            "uid": w.admin_id,
        },
    )
    await session.flush()
    return row_id


def build_app(session: AsyncSession, redis_client: Redis, clock: FakeClock) -> FastAPI:
    app = FastAPI()
    app.add_exception_handler(AppError, app_error_handler)
    app.include_router(tasks_router)
    app.include_router(dr_events_router)
    app.include_router(work_streams_router)

    async def _session_override():  # noqa: ANN202
        yield session

    async def _clock_override() -> Clock:
        return clock

    app.state.session_store = RedisSessionStore(redis_client, idle_minutes=30, absolute_hours=8)
    app.dependency_overrides[get_request_session] = _session_override
    app.dependency_overrides[get_clock] = _clock_override
    app.dependency_overrides[get_session_store] = lambda: app.state.session_store
    return app


async def login(
    session: AsyncSession, redis_client: Redis, clock: FakeClock, user_id: uuid.UUID
) -> tuple[str, str]:
    store = RedisSessionStore(redis_client, idle_minutes=30, absolute_hours=8)
    session_id = uuid.uuid4()
    data = await store.create(session_id, user_id, "LOCAL", clock)
    session.add(
        SessionRecord(
            id=session_id,
            user_id=user_id,
            identity_type="LOCAL",
            created_at=clock.now(),
            last_seen_at=clock.now(),
            absolute_expires_at=clock.now() + timedelta(hours=8),
            idle_expires_at=clock.now() + timedelta(minutes=30),
        )
    )
    await session.flush()
    return str(session_id), data.csrf_token


def http(session: AsyncSession, redis_client: Redis, clock: FakeClock) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=build_app(session, redis_client, clock)), base_url="http://t"
    )


def headers(csrf_token: str, key: str | None = None) -> dict[str, str]:
    return {"X-CSRF-Token": csrf_token, "Idempotency-Key": key or str(uuid.uuid4())}


async def seed_milestone(
    session: AsyncSession,
    w: World,
    *,
    status: str = "NOT_STARTED",
    work_stream_id: uuid.UUID | None = None,
    dr_event_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Raw SQL: Milestone creation is BUILD-07's command."""
    milestone_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO milestones (id, dr_event_id, work_stream_id, name, status, created_by_user_id) "
            "VALUES (:id, :eid, :ws, :name, CAST(:st AS milestone_status), :uid)"
        ),
        {
            "id": milestone_id,
            "eid": dr_event_id or w.event_id,
            "ws": work_stream_id or w.work_stream_id,
            "name": f"Milestone {milestone_id}",
            "st": status,
            "uid": w.admin_id,
        },
    )
    await session.flush()
    return milestone_id


async def seed_gate(
    session: AsyncSession, w: World, *, milestone_id: uuid.UUID, task_id: uuid.UUID, strength: str = "HARD"
) -> uuid.UUID:
    row_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO milestone_dependencies "
            "(id, milestone_id, successor_task_id, strength, created_by_user_id) "
            "VALUES (:id, :m, :t, CAST(:st AS dependency_strength), :uid)"
        ),
        {"id": row_id, "m": milestone_id, "t": task_id, "st": strength, "uid": w.admin_id},
    )
    await session.flush()
    return row_id


async def seed_foreign_task(session: AsyncSession, w: World) -> tuple[uuid.UUID, uuid.UUID]:
    """A second ACTIVE Event with one Task in it. Only the Global Admin can see it."""
    event_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO dr_events "
            "(id, name, event_type, status, version, created_by_user_id, created_at, updated_at) "
            "VALUES (:id, 'Other Event', 'PLANNED_DR', 'ACTIVE', 1, :creator, now(), now())"
        ),
        {"id": event_id, "creator": w.admin_id},
    )
    ws = await get_or_create_work_stream(session, dr_event_id=event_id, name="Elsewhere", actor_id=w.admin_id)
    task = await create_draft_task(
        session,
        dr_event_id=event_id,
        title="Foreign task",
        phase="FAILOVER",
        owning_team_id=w.team_id,
        created_by_user_id=w.admin_id,
        work_stream_id=ws.id,
    )
    return event_id, task.id


async def seed_dr_application(session: AsyncSession, w: World, *, application_id: uuid.UUID) -> uuid.UUID:
    dr_application_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO dr_applications "
            "(id, dr_event_id, application_id, effective_tier_id, effective_sla_minutes, "
            "rto_target_minutes, status, version, created_at, updated_at) "
            "SELECT :id, :eid, :aid, id, 120, 120, 'NOT_STARTED', 1, now(), now() "
            "FROM tiers WHERE code = 'TIER_1'"
        ),
        {"id": dr_application_id, "eid": w.event_id, "aid": application_id},
    )
    await session.flush()
    return dr_application_id

"""Transition-table tests for `dr_events/transition_service.py::DrEventTransitionService`
(BUILD-04, session a). Mounts the REAL `app.dr_events.routes.router` (mirrors
`test_applications_catalog.py`'s harness) so capability guards, `Idempotency-Key` handling and
audit/outbox side effects are exercised end-to-end. See `docs/plan/increments/BUILD-04.plan.md`
for the endpoint/capability/audit-action table and the Risk notes on which guards are deferred
this session (readiness on `activate`, D-227 monitoring on `close`, `mark_failed_over`'s
validation guard).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock, FakeClock
from app.core.database import get_request_session
from app.core.errors import AppError, app_error_handler
from app.dr_events.queries import list_descendant_dr_applications, list_descendant_event_ids
from app.dr_events.routes import router as dr_events_router
from app.identity_auth.dependencies import get_clock, get_session_store
from app.identity_auth.session_store import RedisSessionStore
from app.policies_admin.commands import set_policy_value
from app.users_teams_org.models import RoleAssignment

pytestmark = [pytest.mark.api, pytest.mark.transitions]


def _build_app(session: AsyncSession, redis_client: Redis, clock: FakeClock) -> FastAPI:
    app = FastAPI()
    app.add_exception_handler(AppError, app_error_handler)
    app.include_router(dr_events_router)

    async def _get_session_override():  # noqa: ANN202
        yield session

    async def _get_clock_override() -> Clock:
        return clock

    app.state.session_store = RedisSessionStore(redis_client, idle_minutes=30, absolute_hours=8)
    app.dependency_overrides[get_request_session] = _get_session_override
    app.dependency_overrides[get_clock] = _get_clock_override
    app.dependency_overrides[get_session_store] = lambda: app.state.session_store
    return app


async def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _create_user(session: AsyncSession, *, display_name: str = "Test User") -> uuid.UUID:
    user_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO users (id, identity_type, display_name, email) "
            "VALUES (:id, 'LOCAL', :name, :email)"
        ),
        {"id": user_id, "name": display_name, "email": f"{user_id}@example.test"},
    )
    await session.flush()
    return user_id


async def _create_entra_admin(session: AsyncSession) -> uuid.UUID:
    user_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO users (id, identity_type, display_name, email, entra_object_id) "
            "VALUES (:id, 'ENTRA', 'Event Admin', :email, :oid)"
        ),
        {"id": user_id, "email": f"{user_id}@example.test", "oid": str(uuid.uuid4())},
    )
    session.add(RoleAssignment(user_id=user_id, role_key="GLOBAL_ADMIN", scope_type="GLOBAL"))
    await session.flush()
    return user_id


async def _grant_role(
    session: AsyncSession,
    user_id: uuid.UUID,
    role_key: str,
    *,
    scope_type: str = "GLOBAL",
    scope_id: uuid.UUID | None = None,
) -> None:
    session.add(RoleAssignment(user_id=user_id, role_key=role_key, scope_type=scope_type, scope_id=scope_id))
    await session.flush()


async def _create_session_cookie(
    session: AsyncSession, redis_client: Redis, clock: FakeClock, user_id: uuid.UUID
) -> tuple[str, str]:
    from app.identity_auth.models import SessionRecord

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


def _idem_headers(csrf_token: str) -> dict[str, str]:
    return {"X-CSRF-Token": csrf_token, "Idempotency-Key": str(uuid.uuid4())}


async def _create_application(session: AsyncSession, *, name: str) -> uuid.UUID:
    tier_row = await session.execute(text("SELECT id FROM tiers WHERE code = 'TIER_2'"))
    tier_id = tier_row.scalar_one()
    app_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO applications (id, name, tier_id, version, created_at, updated_at) "
            "VALUES (:id, :name, :tier_id, 1, now(), now())"
        ),
        {"id": app_id, "name": name, "tier_id": tier_id},
    )
    await session.flush()
    return app_id


async def _create_event(
    client: AsyncClient,
    session_id: str,
    csrf_token: str,
    *,
    name: str,
    application_ids: list[uuid.UUID] | None = None,
) -> Response:
    return await client.post(
        "/api/v1/dr-events",
        cookies={"drcc_session": session_id},
        headers=_idem_headers(csrf_token),
        json={
            "name": name,
            "event_type": "PLANNED_DR",
            "application_ids": [str(a) for a in application_ids or []],
        },
    )


async def _create_event_row(
    session: AsyncSession, *, actor_id: uuid.UUID, status: str, version: int = 1, name: str = "Direct Event"
) -> uuid.UUID:
    """Inserts a `dr_events` row directly at `status`, bypassing the transition service, so
    transition-table tests can start from any state without walking every prior transition."""
    event_id = uuid.uuid4()
    cancel_reason = "n/a" if status == "CANCELLED" else None
    await session.execute(
        text(
            "INSERT INTO dr_events "
            "(id, name, event_type, status, version, cancel_reason, created_by_user_id, "
            "created_at, updated_at) "
            "VALUES (:id, :name, 'PLANNED_DR', :status, :version, :cancel_reason, :creator, now(), now())"
        ),
        {
            "id": event_id,
            "name": name,
            "status": status,
            "version": version,
            "cancel_reason": cancel_reason,
            "creator": actor_id,
        },
    )
    await session.flush()
    return event_id


@pytest.mark.asyncio
async def test_create_planned_event_requires_capability(
    session: AsyncSession, redis_client: Redis, clock: FakeClock, seed_user: uuid.UUID
) -> None:
    app = _build_app(session, redis_client, clock)
    session_id, csrf_token = await _create_session_cookie(session, redis_client, clock, seed_user)

    async with await _client(app) as client:
        response = await _create_event(client, session_id, csrf_token, name="Denied Event")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "AUTHORIZATION_REQUIRED"
    await redis_client.delete(f"drcc:session:{session_id}")


@pytest.mark.asyncio
async def test_app_owner_can_create_planned_event_scoped_to_own_application_only(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    owner_id = await _create_user(session, display_name="App Owner")
    own_app_id = await _create_application(session, name="Owned App")
    other_app_id = await _create_application(session, name="Other App")
    await _grant_role(session, owner_id, "APP_OWNER", scope_type="APPLICATION", scope_id=own_app_id)
    app = _build_app(session, redis_client, clock)
    session_id, csrf_token = await _create_session_cookie(session, redis_client, clock, owner_id)

    async with await _client(app) as client:
        allowed = await _create_event(
            client, session_id, csrf_token, name="Owned Event", application_ids=[own_app_id]
        )
        denied = await _create_event(
            client, session_id, csrf_token, name="Foreign Event", application_ids=[other_app_id]
        )

    assert allowed.status_code == 201
    assert denied.status_code == 403
    await redis_client.delete(f"drcc:session:{session_id}")


@pytest.mark.asyncio
async def test_app_owner_created_event_cannot_be_activated_by_that_owner(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    """D-213 acceptance line: an Owner-created PLANNED Event cannot be activated by that Owner --
    `EVENT_LIFECYCLE_COMMAND` grants only GLOBAL_ADMIN/DR_COORDINATOR, never APP_OWNER."""
    owner_id = await _create_user(session, display_name="App Owner 2")
    own_app_id = await _create_application(session, name="Owned App 2")
    await _grant_role(session, owner_id, "APP_OWNER", scope_type="APPLICATION", scope_id=own_app_id)
    app = _build_app(session, redis_client, clock)
    session_id, csrf_token = await _create_session_cookie(session, redis_client, clock, owner_id)

    async with await _client(app) as client:
        created = await _create_event(
            client, session_id, csrf_token, name="Owner Event", application_ids=[own_app_id]
        )
        event_id = created.json()["id"]

        activate_response = await client.post(
            f"/api/v1/dr-events/{event_id}/activate",
            cookies={"drcc_session": session_id},
            headers=_idem_headers(csrf_token),
            json={"expected_version": 1},
        )

    assert activate_response.status_code == 403
    await redis_client.delete(f"drcc:session:{session_id}")


@pytest.mark.asyncio
async def test_create_event_with_application_scopes_dr_applications_not_started(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    admin_id = await _create_entra_admin(session)
    app_id = await _create_application(session, name="Scoped App")
    app = _build_app(session, redis_client, clock)
    session_id, csrf_token = await _create_session_cookie(session, redis_client, clock, admin_id)

    async with await _client(app) as client:
        created = await _create_event(
            client, session_id, csrf_token, name="Scoped Event", application_ids=[app_id]
        )
        event_id = created.json()["id"]

    row = await session.execute(
        text(
            "SELECT status, effective_tier_id, rto_target_minutes "
            "FROM dr_applications WHERE dr_event_id = :eid"
        ),
        {"eid": event_id},
    )
    dr_app = row.one()
    assert dr_app.status == "NOT_STARTED"
    assert dr_app.rto_target_minutes == 120  # TIER_2 default_sla_minutes
    await redis_client.delete(f"drcc:session:{session_id}")


@pytest.mark.asyncio
async def test_full_legal_path_planned_to_closed(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    admin_id = await _create_entra_admin(session)
    app_id = await _create_application(session, name="Path App")
    app = _build_app(session, redis_client, clock)
    session_id, csrf_token = await _create_session_cookie(session, redis_client, clock, admin_id)

    async with await _client(app) as client:
        created = await _create_event(
            client, session_id, csrf_token, name="Path Event", application_ids=[app_id]
        )
        event_id = created.json()["id"]

        activate = await client.post(
            f"/api/v1/dr-events/{event_id}/activate",
            cookies={"drcc_session": session_id},
            headers=_idem_headers(csrf_token),
            json={"expected_version": 1, "override_reason": "no owner/RPO data seeded in this test"},
        )
        assert activate.status_code == 200
        assert activate.json()["status"] == "ACTIVE"

        clock.advance(minutes=5)
        failover = await client.post(
            f"/api/v1/dr-events/{event_id}/start-failover",
            cookies={"drcc_session": session_id},
            headers=_idem_headers(csrf_token),
            json={"expected_version": 2},
        )
        assert failover.status_code == 200
        assert failover.json()["status"] == "FAILOVER_IN_PROGRESS"

        failed_over = await client.post(
            f"/api/v1/dr-events/{event_id}/mark-failed-over",
            cookies={"drcc_session": session_id},
            headers=_idem_headers(csrf_token),
            json={"expected_version": 3},
        )
        assert failed_over.status_code == 200
        assert failed_over.json()["status"] == "FAILED_OVER"

        closed = await client.post(
            f"/api/v1/dr-events/{event_id}/close",
            cookies={"drcc_session": session_id},
            headers=_idem_headers(csrf_token),
            json={"expected_version": 4},
        )

    assert closed.status_code == 200
    assert closed.json()["status"] == "CLOSED"

    dr_app_row = await session.execute(
        text("SELECT status FROM dr_applications WHERE dr_event_id = :eid"), {"eid": event_id}
    )
    assert dr_app_row.scalar_one() == "RECOVERING"
    await redis_client.delete(f"drcc:session:{session_id}")


@pytest.mark.asyncio
async def test_start_failover_snapshots_baseline_and_moves_applications_to_recovering(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    from app.plans_import.commands import (
        SnapshotItem,
        create_plan,
        create_plan_version,
    )

    admin_id = await _create_entra_admin(session)
    app_id = await _create_application(session, name="Baseline App")
    plan = await create_plan(session, actor_id=admin_id, name="Baseline Plan", plan_type="FAILOVER")
    source_version = await create_plan_version(
        session,
        actor_id=admin_id,
        plan_id=plan.id,
        version_type="FINAL",
        tasks=[SnapshotItem(source_id=None, snapshot_data={"name": "Step 1"})],
    )

    fastapi_app = _build_app(session, redis_client, clock)
    session_id, csrf_token = await _create_session_cookie(session, redis_client, clock, admin_id)

    async with await _client(fastapi_app) as client:
        created = await client.post(
            "/api/v1/dr-events",
            cookies={"drcc_session": session_id},
            headers=_idem_headers(csrf_token),
            json={
                "name": "Baseline Event",
                "event_type": "PLANNED_DR",
                "application_ids": [str(app_id)],
                "plan_id": str(plan.id),
                "plan_version_id": str(source_version.id),
            },
        )
        event_id = created.json()["id"]

        await client.post(
            f"/api/v1/dr-events/{event_id}/activate",
            cookies={"drcc_session": session_id},
            headers=_idem_headers(csrf_token),
            json={"expected_version": 1, "override_reason": "no owner/RPO data seeded in this test"},
        )
        failover = await client.post(
            f"/api/v1/dr-events/{event_id}/start-failover",
            cookies={"drcc_session": session_id},
            headers=_idem_headers(csrf_token),
            json={"expected_version": 2},
        )

    assert failover.status_code == 200
    body = failover.json()
    assert body["baseline_plan_version_id"] is not None

    baseline_tasks = await session.execute(
        text("SELECT snapshot_data FROM plan_version_tasks WHERE plan_version_id = :vid"),
        {"vid": body["baseline_plan_version_id"]},
    )
    rows = baseline_tasks.all()
    assert len(rows) == 1
    assert rows[0].snapshot_data == {"name": "Step 1"}

    dr_app_status = await session.execute(
        text("SELECT status FROM dr_applications WHERE dr_event_id = :eid"), {"eid": event_id}
    )
    assert dr_app_status.scalar_one() == "RECOVERING"
    await redis_client.delete(f"drcc:session:{session_id}")


@pytest.mark.asyncio
async def test_start_failover_rejects_network_cut_at_before_activation(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    admin_id = await _create_entra_admin(session)
    app = _build_app(session, redis_client, clock)
    session_id, csrf_token = await _create_session_cookie(session, redis_client, clock, admin_id)

    async with await _client(app) as client:
        created = await _create_event(client, session_id, csrf_token, name="Bound Event")
        event_id = created.json()["id"]

        await client.post(
            f"/api/v1/dr-events/{event_id}/activate",
            cookies={"drcc_session": session_id},
            headers=_idem_headers(csrf_token),
            json={"expected_version": 1, "override_reason": "no Applications scoped in this test"},
        )
        activated_at = clock.now()
        clock.advance(minutes=10)
        too_early = (activated_at - timedelta(minutes=1)).isoformat()

        response = await client.post(
            f"/api/v1/dr-events/{event_id}/start-failover",
            cookies={"drcc_session": session_id},
            headers=_idem_headers(csrf_token),
            json={"expected_version": 2, "network_cut_at": too_early},
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "NETWORK_CUT_AT_OUT_OF_BOUNDS"
    await redis_client.delete(f"drcc:session:{session_id}")


@pytest.mark.asyncio
async def test_start_failover_rejects_network_cut_at_in_the_future(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    admin_id = await _create_entra_admin(session)
    app = _build_app(session, redis_client, clock)
    session_id, csrf_token = await _create_session_cookie(session, redis_client, clock, admin_id)

    async with await _client(app) as client:
        created = await _create_event(client, session_id, csrf_token, name="Future Cut Event")
        event_id = created.json()["id"]

        await client.post(
            f"/api/v1/dr-events/{event_id}/activate",
            cookies={"drcc_session": session_id},
            headers=_idem_headers(csrf_token),
            json={"expected_version": 1, "override_reason": "no Applications scoped in this test"},
        )
        too_late = (clock.now() + timedelta(minutes=5)).isoformat()

        response = await client.post(
            f"/api/v1/dr-events/{event_id}/start-failover",
            cookies={"drcc_session": session_id},
            headers=_idem_headers(csrf_token),
            json={"expected_version": 2, "network_cut_at": too_late},
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "NETWORK_CUT_AT_OUT_OF_BOUNDS"
    await redis_client.delete(f"drcc:session:{session_id}")


@pytest.mark.asyncio
async def test_start_failover_defaults_network_cut_at_to_now(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    admin_id = await _create_entra_admin(session)
    app = _build_app(session, redis_client, clock)
    session_id, csrf_token = await _create_session_cookie(session, redis_client, clock, admin_id)

    async with await _client(app) as client:
        created = await _create_event(client, session_id, csrf_token, name="Default Cut Event")
        event_id = created.json()["id"]

        await client.post(
            f"/api/v1/dr-events/{event_id}/activate",
            cookies={"drcc_session": session_id},
            headers=_idem_headers(csrf_token),
            json={"expected_version": 1, "override_reason": "no Applications scoped in this test"},
        )
        clock.advance(minutes=3)
        expected_now = clock.now()

        response = await client.post(
            f"/api/v1/dr-events/{event_id}/start-failover",
            cookies={"drcc_session": session_id},
            headers=_idem_headers(csrf_token),
            json={"expected_version": 2},
        )

    assert response.status_code == 200
    assert datetime.fromisoformat(response.json()["network_cut_at"]) == expected_now
    await redis_client.delete(f"drcc:session:{session_id}")


@pytest.mark.asyncio
async def test_start_failback_moves_only_failback_required_applications(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    admin_id = await _create_entra_admin(session)
    event_id = await _create_event_row(session, actor_id=admin_id, status="FAILED_OVER", version=1)

    required_app_id = uuid.uuid4()
    not_required_app_id = uuid.uuid4()
    application_a = await _create_application(session, name="Failback Required App")
    application_b = await _create_application(session, name="Failback Not Required App")
    tier_row = await session.execute(text("SELECT id FROM tiers WHERE code = 'TIER_2'"))
    tier_id = tier_row.scalar_one()
    await session.execute(
        text(
            "INSERT INTO dr_applications "
            "(id, dr_event_id, application_id, effective_tier_id, effective_sla_minutes, "
            "rto_target_minutes, failback_required, status, version, created_at, updated_at) "
            "VALUES (:id, :eid, :aid, :tid, 120, 120, TRUE, 'FAILED_OVER', 1, now(), now())"
        ),
        {"id": required_app_id, "eid": event_id, "aid": application_a, "tid": tier_id},
    )
    await session.execute(
        text(
            "INSERT INTO dr_applications "
            "(id, dr_event_id, application_id, effective_tier_id, effective_sla_minutes, "
            "rto_target_minutes, failback_required, status, version, created_at, updated_at) "
            "VALUES (:id, :eid, :aid, :tid, 120, 120, FALSE, 'FAILED_OVER', 1, now(), now())"
        ),
        {"id": not_required_app_id, "eid": event_id, "aid": application_b, "tid": tier_id},
    )
    await session.flush()

    app = _build_app(session, redis_client, clock)
    session_id, csrf_token = await _create_session_cookie(session, redis_client, clock, admin_id)

    async with await _client(app) as client:
        response = await client.post(
            f"/api/v1/dr-events/{event_id}/start-failback",
            cookies={"drcc_session": session_id},
            headers=_idem_headers(csrf_token),
            json={"expected_version": 1},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "FAILBACK_IN_PROGRESS"

    statuses = await session.execute(
        text("SELECT id, status FROM dr_applications WHERE dr_event_id = :eid"), {"eid": event_id}
    )
    by_id = {row.id: row.status for row in statuses}
    assert by_id[required_app_id] == "FAILBACK_IN_PROGRESS"
    assert by_id[not_required_app_id] == "FAILED_OVER"
    await redis_client.delete(f"drcc:session:{session_id}")


@pytest.mark.asyncio
async def test_close_blocked_by_non_terminal_child_event(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    admin_id = await _create_entra_admin(session)
    parent_id = await _create_event_row(
        session, actor_id=admin_id, status="FAILED_OVER", version=1, name="Parent"
    )
    child_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO dr_events "
            "(id, parent_dr_event_id, name, event_type, status, version, created_by_user_id, "
            "created_at, updated_at) "
            "VALUES (:id, :parent_id, 'Child', 'PLANNED_DR', 'ACTIVE', 1, :creator, now(), now())"
        ),
        {"id": child_id, "parent_id": parent_id, "creator": admin_id},
    )
    await session.flush()

    app = _build_app(session, redis_client, clock)
    session_id, csrf_token = await _create_session_cookie(session, redis_client, clock, admin_id)

    async with await _client(app) as client:
        response = await client.post(
            f"/api/v1/dr-events/{parent_id}/close",
            cookies={"drcc_session": session_id},
            headers=_idem_headers(csrf_token),
            json={"expected_version": 1},
        )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CLOSURE_HARD_STOP"
    await redis_client.delete(f"drcc:session:{session_id}")


@pytest.mark.asyncio
async def test_close_succeeds_when_children_terminal_or_absent(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    admin_id = await _create_entra_admin(session)
    parent_id = await _create_event_row(
        session, actor_id=admin_id, status="FAILED_OVER", version=1, name="Lone Parent"
    )
    app = _build_app(session, redis_client, clock)
    session_id, csrf_token = await _create_session_cookie(session, redis_client, clock, admin_id)

    async with await _client(app) as client:
        response = await client.post(
            f"/api/v1/dr-events/{parent_id}/close",
            cookies={"drcc_session": session_id},
            headers=_idem_headers(csrf_token),
            json={"expected_version": 1},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "CLOSED"
    await redis_client.delete(f"drcc:session:{session_id}")


@pytest.mark.asyncio
async def test_list_descendant_dr_applications_aggregates_multi_level_tree(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    """D-219: "parent aggregates all descendant DR Applications" -- proves the aggregation query
    stub walks the whole tree (parent -> child -> grandchild), not just direct children, and
    excludes an unrelated sibling tree's Applications."""
    admin_id = await _create_entra_admin(session)
    app_a = await _create_application(session, name="Parent App")
    app_b = await _create_application(session, name="Child App")
    app_c = await _create_application(session, name="Grandchild App")
    app_d = await _create_application(session, name="Unrelated App")
    app = _build_app(session, redis_client, clock)
    session_id, csrf_token = await _create_session_cookie(session, redis_client, clock, admin_id)

    async with await _client(app) as client:
        parent = await _create_event(client, session_id, csrf_token, name="Parent", application_ids=[app_a])
        parent_id = parent.json()["id"]
        child = await _create_event(client, session_id, csrf_token, name="Child", application_ids=[app_b])
        child_id = child.json()["id"]
        grandchild = await _create_event(
            client, session_id, csrf_token, name="Grandchild", application_ids=[app_c]
        )
        grandchild_id = grandchild.json()["id"]
        unrelated = await _create_event(
            client, session_id, csrf_token, name="Unrelated", application_ids=[app_d]
        )
        unrelated_id = unrelated.json()["id"]

    await session.execute(
        text("UPDATE dr_events SET parent_dr_event_id = :pid WHERE id = :cid"),
        {"pid": parent_id, "cid": child_id},
    )
    await session.execute(
        text("UPDATE dr_events SET parent_dr_event_id = :pid WHERE id = :cid"),
        {"pid": child_id, "cid": grandchild_id},
    )
    await session.flush()

    descendant_ids = await list_descendant_event_ids(session, uuid.UUID(parent_id))
    assert set(descendant_ids) == {uuid.UUID(parent_id), uuid.UUID(child_id), uuid.UUID(grandchild_id)}
    assert uuid.UUID(unrelated_id) not in descendant_ids

    descendant_apps = await list_descendant_dr_applications(session, uuid.UUID(parent_id))
    assert {a.application_id for a in descendant_apps} == {app_a, app_b, app_c}
    await redis_client.delete(f"drcc:session:{session_id}")


@pytest.mark.asyncio
async def test_list_descendant_event_ids_terminates_on_a_forced_cycle(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    """The command layer never lets `parent_dr_event_id` form a cycle (only set once, at create,
    against an already-existing parent), so this can't happen through the app -- but
    `list_descendant_event_ids`'s BFS should still terminate and return the finite reachable set
    if one were ever forced directly in the database, rather than looping forever."""
    admin_id = await _create_entra_admin(session)
    event_a = await _create_event_row(session, actor_id=admin_id, status="PLANNED", name="Cycle A")
    event_b = await _create_event_row(session, actor_id=admin_id, status="PLANNED", name="Cycle B")

    await session.execute(
        text("UPDATE dr_events SET parent_dr_event_id = :pid WHERE id = :cid"),
        {"pid": event_a, "cid": event_b},
    )
    await session.execute(
        text("UPDATE dr_events SET parent_dr_event_id = :pid WHERE id = :cid"),
        {"pid": event_b, "cid": event_a},
    )
    await session.flush()

    descendant_ids = await list_descendant_event_ids(session, event_a)
    assert set(descendant_ids) == {event_a, event_b}


@pytest.mark.asyncio
async def test_cancel_requires_reason(session: AsyncSession, redis_client: Redis, clock: FakeClock) -> None:
    admin_id = await _create_entra_admin(session)
    app = _build_app(session, redis_client, clock)
    session_id, csrf_token = await _create_session_cookie(session, redis_client, clock, admin_id)

    async with await _client(app) as client:
        created = await _create_event(client, session_id, csrf_token, name="Needs Reason Event")
        event_id = created.json()["id"]

        response = await client.post(
            f"/api/v1/dr-events/{event_id}/cancel",
            cookies={"drcc_session": session_id},
            headers=_idem_headers(csrf_token),
            json={"expected_version": 1, "reason": "   "},
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "CANCEL_REASON_REQUIRED"
    await redis_client.delete(f"drcc:session:{session_id}")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "from_status", ["PLANNED", "ACTIVE", "FAILOVER_IN_PROGRESS", "FAILED_OVER", "FAILBACK_IN_PROGRESS"]
)
async def test_cancel_from_any_nonterminal_state(
    from_status: str, session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    admin_id = await _create_entra_admin(session)
    event_id = await _create_event_row(session, actor_id=admin_id, status=from_status, version=1)
    app = _build_app(session, redis_client, clock)
    session_id, csrf_token = await _create_session_cookie(session, redis_client, clock, admin_id)

    async with await _client(app) as client:
        response = await client.post(
            f"/api/v1/dr-events/{event_id}/cancel",
            cookies={"drcc_session": session_id},
            headers=_idem_headers(csrf_token),
            json={"expected_version": 1, "reason": "no longer needed"},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "CANCELLED"
    assert response.json()["cancel_reason"] == "no longer needed"
    await redis_client.delete(f"drcc:session:{session_id}")


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_status", ["CLOSED", "CANCELLED"])
async def test_cancel_from_terminal_state_rejected(
    terminal_status: str, session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    admin_id = await _create_entra_admin(session)
    event_id = await _create_event_row(session, actor_id=admin_id, status=terminal_status, version=1)
    app = _build_app(session, redis_client, clock)
    session_id, csrf_token = await _create_session_cookie(session, redis_client, clock, admin_id)

    async with await _client(app) as client:
        response = await client.post(
            f"/api/v1/dr-events/{event_id}/cancel",
            cookies={"drcc_session": session_id},
            headers=_idem_headers(csrf_token),
            json={"expected_version": 1, "reason": "too late"},
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_TRANSITION"
    await redis_client.delete(f"drcc:session:{session_id}")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("from_status", "route"),
    [
        ("PLANNED", "start-failover"),
        ("PLANNED", "mark-failed-over"),
        ("PLANNED", "close"),
        ("ACTIVE", "close"),
        ("ACTIVE", "mark-failed-over"),
        ("FAILED_OVER", "activate"),
        ("CLOSED", "activate"),
    ],
)
async def test_illegal_transition_rejected(
    from_status: str, route: str, session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    admin_id = await _create_entra_admin(session)
    event_id = await _create_event_row(session, actor_id=admin_id, status=from_status, version=1)
    app = _build_app(session, redis_client, clock)
    session_id, csrf_token = await _create_session_cookie(session, redis_client, clock, admin_id)

    async with await _client(app) as client:
        response = await client.post(
            f"/api/v1/dr-events/{event_id}/{route}",
            cookies={"drcc_session": session_id},
            headers=_idem_headers(csrf_token),
            json={"expected_version": 1},
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_TRANSITION"
    await redis_client.delete(f"drcc:session:{session_id}")


@pytest.mark.asyncio
async def test_stale_version_returns_409(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    admin_id = await _create_entra_admin(session)
    app = _build_app(session, redis_client, clock)
    session_id, csrf_token = await _create_session_cookie(session, redis_client, clock, admin_id)

    async with await _client(app) as client:
        created = await _create_event(client, session_id, csrf_token, name="Stale Event")
        event_id = created.json()["id"]

        first = await client.post(
            f"/api/v1/dr-events/{event_id}/activate",
            cookies={"drcc_session": session_id},
            headers=_idem_headers(csrf_token),
            json={"expected_version": 1, "override_reason": "no Applications scoped in this test"},
        )
        assert first.status_code == 200

        stale = await client.post(
            f"/api/v1/dr-events/{event_id}/activate",
            cookies={"drcc_session": session_id},
            headers=_idem_headers(csrf_token),
            json={"expected_version": 1},
        )

    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "CONCURRENCY_CONFLICT"
    await redis_client.delete(f"drcc:session:{session_id}")


@pytest.mark.asyncio
async def test_unauthorized_actor_403_on_activate(
    session: AsyncSession, redis_client: Redis, clock: FakeClock, seed_user: uuid.UUID
) -> None:
    admin_id = await _create_entra_admin(session)
    event_id = await _create_event_row(session, actor_id=admin_id, status="PLANNED", version=1)
    app = _build_app(session, redis_client, clock)
    session_id, csrf_token = await _create_session_cookie(session, redis_client, clock, seed_user)

    async with await _client(app) as client:
        response = await client.post(
            f"/api/v1/dr-events/{event_id}/activate",
            cookies={"drcc_session": session_id},
            headers=_idem_headers(csrf_token),
            json={"expected_version": 1},
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "AUTHORIZATION_REQUIRED"
    await redis_client.delete(f"drcc:session:{session_id}")


@pytest.mark.asyncio
async def test_every_transition_writes_audit_and_outbox_in_same_transaction(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    admin_id = await _create_entra_admin(session)
    app = _build_app(session, redis_client, clock)
    session_id, csrf_token = await _create_session_cookie(session, redis_client, clock, admin_id)

    async with await _client(app) as client:
        created = await _create_event(client, session_id, csrf_token, name="Audited Event")
        event_id = created.json()["id"]

        response = await client.post(
            f"/api/v1/dr-events/{event_id}/activate",
            cookies={"drcc_session": session_id},
            headers=_idem_headers(csrf_token),
            json={"expected_version": 1, "override_reason": "no Applications scoped in this test"},
        )
        assert response.status_code == 200

    audit_count = await session.execute(
        text(
            "SELECT COUNT(*) AS cnt FROM audit_events "
            "WHERE entity_id = :eid AND action = 'DR_EVENT_ACTIVATED'"
        ),
        {"eid": event_id},
    )
    assert audit_count.one().cnt == 1

    outbox_count = await session.execute(
        text(
            "SELECT COUNT(*) AS cnt FROM outbox_events "
            "WHERE aggregate_id = :eid AND event_type = 'EventStateChanged'"
        ),
        {"eid": event_id},
    )
    assert outbox_count.one().cnt >= 1
    await redis_client.delete(f"drcc:session:{session_id}")


@pytest.mark.asyncio
async def test_activate_replay_returns_the_same_response_without_a_second_audit_row(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    """D-215: replaying the same Idempotency-Key must return the original outcome verbatim and
    must not re-execute the transition (no second audit row)."""
    admin_id = await _create_entra_admin(session)
    app = _build_app(session, redis_client, clock)
    session_id, csrf_token = await _create_session_cookie(session, redis_client, clock, admin_id)
    idem_headers = _idem_headers(csrf_token)

    async with await _client(app) as client:
        created = await _create_event(client, session_id, csrf_token, name="Replay Event")
        event_id = created.json()["id"]

        activate_body = {
            "expected_version": 1,
            "override_reason": "no Applications scoped in this test",
        }
        first = await client.post(
            f"/api/v1/dr-events/{event_id}/activate",
            cookies={"drcc_session": session_id},
            headers=idem_headers,
            json=activate_body,
        )
        replay = await client.post(
            f"/api/v1/dr-events/{event_id}/activate",
            cookies={"drcc_session": session_id},
            headers=idem_headers,
            json=activate_body,
        )

    assert first.status_code == 200
    assert replay.status_code == first.status_code
    assert replay.json() == first.json()

    audit_count = await session.execute(
        text(
            "SELECT COUNT(*) AS cnt FROM audit_events "
            "WHERE entity_id = :eid AND action = 'DR_EVENT_ACTIVATED'"
        ),
        {"eid": event_id},
    )
    assert audit_count.one().cnt == 1
    await redis_client.delete(f"drcc:session:{session_id}")


@pytest.mark.asyncio
async def test_activate_nonexistent_event_returns_404(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    admin_id = await _create_entra_admin(session)
    app = _build_app(session, redis_client, clock)
    session_id, csrf_token = await _create_session_cookie(session, redis_client, clock, admin_id)

    async with await _client(app) as client:
        response = await client.post(
            f"/api/v1/dr-events/{uuid.uuid4()}/activate",
            cookies={"drcc_session": session_id},
            headers=_idem_headers(csrf_token),
            json={"expected_version": 1},
        )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "DR_EVENT_NOT_FOUND"
    await redis_client.delete(f"drcc:session:{session_id}")


@pytest.mark.asyncio
async def test_create_event_with_invalid_application_id_commits_no_partial_rows(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    """A request naming one valid and one nonexistent Application must abort atomically --
    no `DrApplication` row for the valid Application should survive once the request-scoped
    session rolls back (found in review: the per-Application loop flushes each row before the
    error on a later id is raised). Production's real `get_request_session` dependency
    (`async with session_factory() as session:`) rolls back automatically when an unhandled
    `AppError` exits the request; this test's harness shares one session across the whole test
    (see `_build_app`) so it does that rollback explicitly here instead, rather than making the
    shared override roll back on every exception -- other tests in this file rely on that shared
    session keeping earlier, still-uncommitted fixture rows (e.g. the session cookie's
    `SessionRecord`) alive across multiple requests within one test."""
    admin_id = await _create_entra_admin(session)
    valid_app_id = await _create_application(session, name="Valid App")
    invalid_app_id = uuid.uuid4()
    app = _build_app(session, redis_client, clock)
    session_id, csrf_token = await _create_session_cookie(session, redis_client, clock, admin_id)

    async with await _client(app) as client:
        response = await _create_event(
            client,
            session_id,
            csrf_token,
            name="Partial Event",
            application_ids=[valid_app_id, invalid_app_id],
        )

    assert response.status_code == 404
    await session.rollback()

    event_count = await session.execute(
        text("SELECT COUNT(*) AS cnt FROM dr_events WHERE name = 'Partial Event'")
    )
    assert event_count.one().cnt == 0
    dr_app_count = await session.execute(
        text("SELECT COUNT(*) AS cnt FROM dr_applications WHERE application_id = :aid"), {"aid": valid_app_id}
    )
    assert dr_app_count.one().cnt == 0
    await redis_client.delete(f"drcc:session:{session_id}")


@pytest.mark.asyncio
async def test_list_events_only_shows_events_visible_to_the_caller(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    """Invariant #1/#10: `GET /dr-events` must not leak Events the caller has no participant
    visibility into (found in review — the route previously returned every Event unfiltered)."""
    admin_id = await _create_entra_admin(session)
    plain_user = await _create_user(session, display_name="No Participant")
    visible_event_id = await _create_event_row(session, actor_id=admin_id, status="PLANNED", name="Visible")
    hidden_event_id = await _create_event_row(session, actor_id=admin_id, status="PLANNED", name="Hidden")
    _ = hidden_event_id

    from app.dr_events.participants import enrol_participant

    await enrol_participant(session, visible_event_id, plain_user, "EXPLICIT")

    app = _build_app(session, redis_client, clock)
    session_id, _ = await _create_session_cookie(session, redis_client, clock, plain_user)

    async with await _client(app) as client:
        list_response = await client.get("/api/v1/dr-events", cookies={"drcc_session": session_id})
        hidden_detail = await client.get(
            f"/api/v1/dr-events/{hidden_event_id}", cookies={"drcc_session": session_id}
        )
        visible_detail = await client.get(
            f"/api/v1/dr-events/{visible_event_id}", cookies={"drcc_session": session_id}
        )

    assert list_response.status_code == 200
    names = {e["name"] for e in list_response.json()["events"]}
    assert names == {"Visible"}
    assert hidden_detail.status_code == 404
    assert visible_detail.status_code == 200
    await redis_client.delete(f"drcc:session:{session_id}")


@pytest.mark.asyncio
async def test_list_events_shows_all_events_to_global_admin(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    admin_id = await _create_entra_admin(session)
    await _create_event_row(session, actor_id=admin_id, status="PLANNED", name="Admin Sees This")
    app = _build_app(session, redis_client, clock)
    session_id, _ = await _create_session_cookie(session, redis_client, clock, admin_id)

    async with await _client(app) as client:
        response = await client.get("/api/v1/dr-events", cookies={"drcc_session": session_id})

    assert response.status_code == 200
    names = {e["name"] for e in response.json()["events"]}
    assert "Admin Sees This" in names
    await redis_client.delete(f"drcc:session:{session_id}")


@pytest.mark.asyncio
async def test_coordinator_can_see_the_event_they_just_created(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    """Found in review: closing the GET IDOR (participant-visibility filter) without also
    enrolling the creator as a participant left a Coordinator unable to see the Event they were
    just authorized to create — `EVENT_LIFECYCLE_COMMAND`'s unconditional grant only covers
    lifecycle *commands*, not GET visibility, which is participant-scoped (D-222)."""
    coordinator_id = await _create_user(session, display_name="Coordinator")
    await _grant_role(session, coordinator_id, "DR_COORDINATOR")
    app = _build_app(session, redis_client, clock)
    session_id, csrf_token = await _create_session_cookie(session, redis_client, clock, coordinator_id)

    async with await _client(app) as client:
        created = await _create_event(client, session_id, csrf_token, name="Coordinator's Event")
        assert created.status_code == 201
        event_id = created.json()["id"]

        detail = await client.get(f"/api/v1/dr-events/{event_id}", cookies={"drcc_session": session_id})
        listing = await client.get("/api/v1/dr-events", cookies={"drcc_session": session_id})

    assert detail.status_code == 200
    assert any(e["id"] == event_id for e in listing.json()["events"])
    await redis_client.delete(f"drcc:session:{session_id}")


@pytest.mark.asyncio
async def test_app_owner_can_see_own_created_event_and_business_owner_is_also_enrolled(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    """D-222's auto-enrolment list includes "any-slot System/Application or Business Owner of
    an in-scope Application" -- verifies both the creating App Owner and a separate Business
    Owner of the same Application become participants when the Event is created scoped to it."""
    owner_id = await _create_user(session, display_name="Creating App Owner")
    business_owner_id = await _create_user(session, display_name="Business Owner")
    app_id = await _create_application(session, name="Owned+Enrolled App")
    await _grant_role(session, owner_id, "APP_OWNER", scope_type="APPLICATION", scope_id=app_id)

    tier_row = await session.execute(text("SELECT id FROM tiers WHERE code = 'TIER_2'"))
    tier_id = tier_row.scalar_one()
    await session.execute(
        text(
            "INSERT INTO application_owners (id, application_id, owner_type, owner_order, user_id, "
            "created_at, updated_at) "
            "VALUES (:id, :aid, 'BUSINESS', 1, :uid, now(), now())"
        ),
        {"id": uuid.uuid4(), "aid": app_id, "uid": business_owner_id},
    )
    await session.flush()
    _ = tier_id

    fastapi_app = _build_app(session, redis_client, clock)
    owner_session_id, owner_csrf = await _create_session_cookie(session, redis_client, clock, owner_id)

    async with await _client(fastapi_app) as client:
        created = await _create_event(
            client, owner_session_id, owner_csrf, name="Owner Created Event", application_ids=[app_id]
        )
        assert created.status_code == 201
        event_id = created.json()["id"]

        owner_detail = await client.get(
            f"/api/v1/dr-events/{event_id}", cookies={"drcc_session": owner_session_id}
        )

        business_session_id, _ = await _create_session_cookie(session, redis_client, clock, business_owner_id)
        business_detail = await client.get(
            f"/api/v1/dr-events/{event_id}", cookies={"drcc_session": business_session_id}
        )

    assert owner_detail.status_code == 200
    assert business_detail.status_code == 200
    await redis_client.delete(f"drcc:session:{owner_session_id}")
    await redis_client.delete(f"drcc:session:{business_session_id}")


async def _grant_primary_system_owner(
    session: AsyncSession, *, app_id: uuid.UUID, user_id: uuid.UUID
) -> None:
    await session.execute(
        text(
            "INSERT INTO application_owners "
            "(id, application_id, owner_type, owner_order, user_id, created_at, updated_at) "
            "VALUES (:id, :aid, 'SYSTEM_APPLICATION', 1, :uid, now(), now())"
        ),
        {"id": uuid.uuid4(), "aid": app_id, "uid": user_id},
    )
    await session.flush()


@pytest.mark.asyncio
async def test_activate_blocked_by_readiness_hard_stop_without_override(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    """D-224: an Event scoped to an Application with no Primary System Owner and no RPO
    target/N-A must be blocked from activating -- `readiness.application_in_scope` is satisfied
    (Application scoped), but `coordinator_user_id` is never auto-assigned on create (no D-record
    authorizes that; spec-auditor finding on the first draft), so `readiness.coordinator_assigned`
    fails alongside the owner/RPO gaps."""
    admin_id = await _create_entra_admin(session)
    app_id = await _create_application(session, name="Readiness Gap App")
    app = _build_app(session, redis_client, clock)
    session_id, csrf_token = await _create_session_cookie(session, redis_client, clock, admin_id)

    async with await _client(app) as client:
        created = await _create_event(
            client, session_id, csrf_token, name="Not Ready Event", application_ids=[app_id]
        )
        event_id = created.json()["id"]

        response = await client.post(
            f"/api/v1/dr-events/{event_id}/activate",
            cookies={"drcc_session": session_id},
            headers=_idem_headers(csrf_token),
            json={"expected_version": 1},
        )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "READINESS_HARD_STOP"
    failed_keys = set(response.json()["error"]["details"]["failed_keys"])
    assert failed_keys == {
        "readiness.coordinator_assigned",
        "readiness.primary_system_owner",
        "readiness.rpo_target_or_na",
    }

    event_row = await session.execute(text("SELECT status FROM dr_events WHERE id = :eid"), {"eid": event_id})
    assert event_row.scalar_one() == "PLANNED"
    await redis_client.delete(f"drcc:session:{session_id}")


@pytest.mark.asyncio
async def test_activate_with_override_reason_records_overrides_and_succeeds(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    admin_id = await _create_entra_admin(session)
    app_id = await _create_application(session, name="Overridden App")
    app = _build_app(session, redis_client, clock)
    session_id, csrf_token = await _create_session_cookie(session, redis_client, clock, admin_id)

    async with await _client(app) as client:
        created = await _create_event(
            client, session_id, csrf_token, name="Override Event", application_ids=[app_id]
        )
        event_id = created.json()["id"]

        response = await client.post(
            f"/api/v1/dr-events/{event_id}/activate",
            cookies={"drcc_session": session_id},
            headers=_idem_headers(csrf_token),
            json={"expected_version": 1, "override_reason": "accepted risk, will fix post-activation"},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "ACTIVE"

    override_rows = await session.execute(
        text(
            "SELECT policy_key, reason, override_type FROM overrides "
            "WHERE dr_event_id = :eid ORDER BY policy_key"
        ),
        {"eid": event_id},
    )
    rows = override_rows.all()
    assert {r.policy_key for r in rows} == {
        "readiness.coordinator_assigned",
        "readiness.primary_system_owner",
        "readiness.rpo_target_or_na",
    }
    assert all(r.reason == "accepted risk, will fix post-activation" for r in rows)
    assert all(r.override_type == "READINESS_OVERRIDE" for r in rows)
    await redis_client.delete(f"drcc:session:{session_id}")


@pytest.mark.asyncio
async def test_activate_succeeds_without_override_when_readiness_satisfied(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    admin_id = await _create_entra_admin(session)
    owner_id = await _create_user(session, display_name="Ready App Owner")
    app_id = await _create_application(session, name="Fully Ready App")
    await _grant_primary_system_owner(session, app_id=app_id, user_id=owner_id)

    fastapi_app = _build_app(session, redis_client, clock)
    session_id, csrf_token = await _create_session_cookie(session, redis_client, clock, admin_id)

    async with await _client(fastapi_app) as client:
        created = await _create_event(
            client, session_id, csrf_token, name="Ready Event", application_ids=[app_id]
        )
        event_id = created.json()["id"]

    # readiness.rpo_target_or_na needs an explicit target or N/A flag -- set N/A directly (no
    # RPO-setting command exists yet, that's rto_rpo_health's scope, a later increment).
    # readiness.coordinator_assigned needs coordinator_user_id set -- no assign-coordinator command
    # exists yet either, so set it directly to prove the satisfied-readiness path end to end.
    await session.execute(
        text("UPDATE dr_applications SET rpo_not_applicable = TRUE WHERE dr_event_id = :eid"),
        {"eid": event_id},
    )
    await session.execute(
        text("UPDATE dr_events SET coordinator_user_id = :uid WHERE id = :eid"),
        {"uid": admin_id, "eid": event_id},
    )
    await session.flush()

    async with await _client(fastapi_app) as client:
        response = await client.post(
            f"/api/v1/dr-events/{event_id}/activate",
            cookies={"drcc_session": session_id},
            headers=_idem_headers(csrf_token),
            json={"expected_version": 1},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "ACTIVE"

    override_count = await session.execute(
        text("SELECT COUNT(*) AS cnt FROM overrides WHERE dr_event_id = :eid"), {"eid": event_id}
    )
    assert override_count.one().cnt == 0
    await redis_client.delete(f"drcc:session:{session_id}")


@pytest.mark.asyncio
async def test_activate_zero_apps_with_application_in_scope_downgraded_still_blocks(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    """D-224 regression: `readiness.application_in_scope` is not a locked policy key, so a
    Coordinator can downgrade it to WARNING at Event scope. An Event with zero in-scope
    Applications must still be blocked on `readiness.primary_system_owner`/`.rpo_target_or_na`
    (not vacuously satisfied) even when `application_in_scope` itself no longer blocks -- proves
    the fix for the vacuous-truth gap the reviewer found in readiness_service.py."""
    admin_id = await _create_entra_admin(session)
    app = _build_app(session, redis_client, clock)
    session_id, csrf_token = await _create_session_cookie(session, redis_client, clock, admin_id)

    async with await _client(app) as client:
        created = await _create_event(client, session_id, csrf_token, name="No Apps Event")
        event_id = created.json()["id"]

        await set_policy_value(
            session,
            actor_id=admin_id,
            key="readiness.application_in_scope",
            value="WARNING",
            scope_type="DR_EVENT",
            scope_id=uuid.UUID(event_id),
            clock=clock,
        )

        response = await client.post(
            f"/api/v1/dr-events/{event_id}/activate",
            cookies={"drcc_session": session_id},
            headers=_idem_headers(csrf_token),
            json={"expected_version": 1},
        )

    assert response.status_code == 409
    failed_keys = set(response.json()["error"]["details"]["failed_keys"])
    assert failed_keys == {
        "readiness.coordinator_assigned",
        "readiness.primary_system_owner",
        "readiness.rpo_target_or_na",
    }
    assert "readiness.application_in_scope" not in failed_keys
    await redis_client.delete(f"drcc:session:{session_id}")


@pytest.mark.asyncio
async def test_activate_succeeds_with_warning_only_failure_and_no_override(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    """D-224: a WARNING-severity failure never blocks `activate` and never needs an
    `override_reason` -- only unmet HARD_STOP keys do. Missing Primary Business Owner is
    downgraded to WARNING at Event scope; the Event otherwise satisfies every HARD_STOP key."""
    admin_id = await _create_entra_admin(session)
    owner_id = await _create_user(session, display_name="System Owner Only")
    app_id = await _create_application(session, name="Business-Owner-Missing App")
    await _grant_primary_system_owner(session, app_id=app_id, user_id=owner_id)

    fastapi_app = _build_app(session, redis_client, clock)
    session_id, csrf_token = await _create_session_cookie(session, redis_client, clock, admin_id)

    async with await _client(fastapi_app) as client:
        created = await _create_event(
            client, session_id, csrf_token, name="Warning Only Event", application_ids=[app_id]
        )
        event_id = created.json()["id"]

        await set_policy_value(
            session,
            actor_id=admin_id,
            key="readiness.primary_business_owner",
            value="WARNING",
            scope_type="DR_EVENT",
            scope_id=uuid.UUID(event_id),
            clock=clock,
        )

    await session.execute(
        text("UPDATE dr_applications SET rpo_not_applicable = TRUE WHERE dr_event_id = :eid"),
        {"eid": event_id},
    )
    await session.execute(
        text("UPDATE dr_events SET coordinator_user_id = :uid WHERE id = :eid"),
        {"uid": admin_id, "eid": event_id},
    )
    await session.flush()

    async with await _client(fastapi_app) as client:
        response = await client.post(
            f"/api/v1/dr-events/{event_id}/activate",
            cookies={"drcc_session": session_id},
            headers=_idem_headers(csrf_token),
            json={"expected_version": 1},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "ACTIVE"

    override_count = await session.execute(
        text("SELECT COUNT(*) AS cnt FROM overrides WHERE dr_event_id = :eid"), {"eid": event_id}
    )
    assert override_count.one().cnt == 0

    audit_row = await session.execute(
        text(
            "SELECT after_data FROM audit_events WHERE entity_id = :eid AND action = 'DR_EVENT_ACTIVATED' "
            "ORDER BY occurred_at DESC LIMIT 1"
        ),
        {"eid": event_id},
    )
    after = audit_row.one().after_data
    assert after["readiness_warnings"] == ["readiness.primary_business_owner"]
    await redis_client.delete(f"drcc:session:{session_id}")

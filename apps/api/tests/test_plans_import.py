"""Route-level tests for the `plans_import` module (BUILD-04, session a).

Mounts the REAL `app.plans_import.routes.router` (mirrors `test_applications_catalog.py`'s
harness) via `httpx.AsyncClient` + `ASGITransport`, so capability guards, `Idempotency-Key`
handling and snapshot-copy behaviour are exercised end-to-end. See
`docs/plan/increments/BUILD-04.plan.md` for the endpoint/capability/audit-action table.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.clock import Clock, FakeClock
from app.core.database import get_request_session
from app.core.errors import AppError, app_error_handler
from app.identity_auth.dependencies import get_clock, get_session_store
from app.identity_auth.session_store import RedisSessionStore
from app.plans_import.routes import router as plans_import_router
from app.users_teams_org.models import RoleAssignment

pytestmark = [pytest.mark.api, pytest.mark.integration]


def _build_app(session: AsyncSession, redis_client: Redis, clock: FakeClock) -> FastAPI:
    app = FastAPI()
    app.add_exception_handler(AppError, app_error_handler)
    app.include_router(plans_import_router)

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


async def _create_entra_admin(session: AsyncSession) -> uuid.UUID:
    """ENTRA identity keeps GLOBAL_ADMIN's bypass without needing D-235's LOCAL-TOTP dance."""
    user_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO users (id, identity_type, display_name, email, entra_object_id) "
            "VALUES (:id, 'ENTRA', 'Plan Admin', :email, :oid)"
        ),
        {"id": user_id, "email": f"{user_id}@example.test", "oid": str(uuid.uuid4())},
    )
    session.add(RoleAssignment(user_id=user_id, role_key="GLOBAL_ADMIN", scope_type="GLOBAL"))
    await session.flush()
    return user_id


async def _create_session_cookie(
    session: AsyncSession, redis_client: Redis, clock: FakeClock, user_id: uuid.UUID
) -> tuple[str, str]:
    from datetime import timedelta

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


async def _create_plan(client: AsyncClient, session_id: str, csrf_token: str, *, name: str) -> Response:
    return await client.post(
        "/api/v1/plans",
        cookies={"drcc_session": session_id},
        headers=_idem_headers(csrf_token),
        json={"name": name, "plan_type": "FAILOVER", "description": "d"},
    )


@pytest.mark.asyncio
async def test_create_plan_requires_manage_plans_capability(
    session: AsyncSession, redis_client: Redis, clock: FakeClock, seed_user: uuid.UUID
) -> None:
    app = _build_app(session, redis_client, clock)
    session_id, csrf_token = await _create_session_cookie(session, redis_client, clock, seed_user)

    async with await _client(app) as client:
        response = await _create_plan(client, session_id, csrf_token, name="Denied Plan")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "AUTHORIZATION_REQUIRED"
    count = await session.execute(text("SELECT COUNT(*) AS cnt FROM plans WHERE name = 'Denied Plan'"))
    assert count.one().cnt == 0
    await redis_client.delete(f"drcc:session:{session_id}")


@pytest.mark.asyncio
async def test_create_plan_and_version_round_trip(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    admin_id = await _create_entra_admin(session)
    app = _build_app(session, redis_client, clock)
    session_id, csrf_token = await _create_session_cookie(session, redis_client, clock, admin_id)

    async with await _client(app) as client:
        created = await _create_plan(client, session_id, csrf_token, name="Billing Failover")
        assert created.status_code == 201
        plan_id = created.json()["id"]

        version_response = await client.post(
            f"/api/v1/plans/{plan_id}/versions",
            cookies={"drcc_session": session_id},
            headers=_idem_headers(csrf_token),
            json={
                "version_type": "DRAFT",
                "notes": "first cut",
                "tasks": [{"snapshot_data": {"name": "Cut over DNS"}}],
                "task_dependencies": [],
                "milestones": [{"snapshot_data": {"name": "Go-live"}}],
            },
        )
        assert version_response.status_code == 201
        body = version_response.json()
        assert body["version_number"] == 1
        assert body["version_type"] == "DRAFT"
        assert body["task_count"] == 1
        assert body["milestone_count"] == 1

        detail = await client.get(f"/api/v1/plans/{plan_id}", cookies={"drcc_session": session_id})

    assert detail.status_code == 200
    assert len(detail.json()["versions"]) == 1
    await redis_client.delete(f"drcc:session:{session_id}")


@pytest.mark.asyncio
async def test_create_plan_version_rejects_caller_supplied_baseline(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    """BASELINE is system-managed (written only by `start_failover`'s `capture_baseline_snapshot`)
    -- a caller cannot create one directly through `POST /plans/{id}/versions`."""
    admin_id = await _create_entra_admin(session)
    app = _build_app(session, redis_client, clock)
    session_id, csrf_token = await _create_session_cookie(session, redis_client, clock, admin_id)

    async with await _client(app) as client:
        created = await _create_plan(client, session_id, csrf_token, name="Payments Failover")
        plan_id = created.json()["id"]

        response = await client.post(
            f"/api/v1/plans/{plan_id}/versions",
            cookies={"drcc_session": session_id},
            headers=_idem_headers(csrf_token),
            json={"version_type": "BASELINE"},
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "PLAN_VERSION_BASELINE_NOT_ALLOWED"
    await redis_client.delete(f"drcc:session:{session_id}")


@pytest.mark.asyncio
async def test_instantiate_plan_into_event_copies_snapshot_rows_to_new_execution_version(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    """`instantiate_plan_into_event` (BUILD-04.plan.md Risk #1) copies the source plan_version's
    snapshot rows into a new EXECUTION plan_version scoped to the target dr_event_id."""
    from app.plans_import.commands import (
        SnapshotItem,
        create_plan,
        create_plan_version,
        instantiate_plan_into_event,
    )

    admin_id = await _create_entra_admin(session)
    plan = await create_plan(session, actor_id=admin_id, name="Network Failover", plan_type="FAILOVER")
    source_version = await create_plan_version(
        session,
        actor_id=admin_id,
        plan_id=plan.id,
        version_type="FINAL",
        tasks=[SnapshotItem(source_id=None, snapshot_data={"name": "Repoint DNS"})],
    )
    fake_event_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO dr_events "
            "(id, name, event_type, status, created_by_user_id, created_at, updated_at) "
            "VALUES (:id, 'Fake Event', 'PLANNED_DR', 'PLANNED', :creator, now(), now())"
        ),
        {"id": fake_event_id, "creator": admin_id},
    )

    new_version = await instantiate_plan_into_event(
        session,
        actor_id=admin_id,
        plan_id=plan.id,
        plan_version_id=source_version.id,
        dr_event_id=fake_event_id,
        clock=clock,
    )

    assert new_version.version_type == "EXECUTION"
    assert new_version.dr_event_id == fake_event_id
    assert new_version.plan_id is None

    copied_tasks = (
        await session.execute(
            text("SELECT snapshot_data FROM plan_version_tasks WHERE plan_version_id = :vid"),
            {"vid": new_version.id},
        )
    ).all()
    assert len(copied_tasks) == 1
    assert copied_tasks[0].snapshot_data == {"name": "Repoint DNS"}


@pytest.mark.asyncio
async def test_instantiate_does_not_mutate_the_source_plan_version_rows(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    from app.plans_import.commands import (
        SnapshotItem,
        create_plan,
        create_plan_version,
        instantiate_plan_into_event,
    )

    admin_id = await _create_entra_admin(session)
    plan = await create_plan(session, actor_id=admin_id, name="Storage Failover", plan_type="FAILOVER")
    source_version = await create_plan_version(
        session,
        actor_id=admin_id,
        plan_id=plan.id,
        version_type="FINAL",
        tasks=[SnapshotItem(source_id=None, snapshot_data={"name": "Original"})],
    )
    fake_event_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO dr_events "
            "(id, name, event_type, status, created_by_user_id, created_at, updated_at) "
            "VALUES (:id, 'Fake Event 2', 'PLANNED_DR', 'PLANNED', :creator, now(), now())"
        ),
        {"id": fake_event_id, "creator": admin_id},
    )

    await instantiate_plan_into_event(
        session,
        actor_id=admin_id,
        plan_id=plan.id,
        plan_version_id=source_version.id,
        dr_event_id=fake_event_id,
        clock=clock,
    )

    source_rows = (
        await session.execute(
            text("SELECT snapshot_data FROM plan_version_tasks WHERE plan_version_id = :vid"),
            {"vid": source_version.id},
        )
    ).all()
    assert len(source_rows) == 1
    assert source_rows[0].snapshot_data == {"name": "Original"}


@pytest.mark.asyncio
async def test_create_plan_version_serializes_concurrent_requests_for_the_same_plan(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two concurrent `create_plan_version` calls for the same Plan (different Idempotency-Keys,
    modelled here as two independent DB sessions on separate connections) must not both read the
    same `MAX(version_number)` and race to insert the same next number -- the unique
    `(plan_id, version_number)` index would otherwise surface as an unhandled IntegrityError for
    one of them (found in review). Uses real, separate Postgres connections rather than the
    shared `session` fixture, whose single connection/transaction can't model genuine concurrency.
    Patches `next_plan_version_number` to pause briefly so both calls are deterministically inside
    the race window at the same time, rather than relying on incidental asyncio scheduling luck
    (confirmed unreliable: the unpatched version of this test passed 13/13 runs against the
    pre-fix code, since one coroutine's several fast local-DB round trips usually finished before
    the other was scheduled at all). With the row lock in place, the second call can't even reach
    the patched pause until the first commits, so it correctly sees the new max. Setup/cleanup
    commit for real (unlike every other test in this file) since two independent connections need
    to see the same committed Plan row."""
    import asyncio

    from app.plans_import import commands as plans_import_commands

    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    admin_id = uuid.uuid4()
    plan_id = uuid.uuid4()

    async with session_factory() as setup_session:
        await setup_session.execute(
            text(
                "INSERT INTO users (id, identity_type, display_name, email, entra_object_id) "
                "VALUES (:id, 'ENTRA', 'Race Admin', :email, :oid)"
            ),
            {"id": admin_id, "email": f"{admin_id}@example.test", "oid": str(uuid.uuid4())},
        )
        setup_session.add(RoleAssignment(user_id=admin_id, role_key="GLOBAL_ADMIN", scope_type="GLOBAL"))
        await setup_session.execute(
            text(
                "INSERT INTO plans (id, name, plan_type, created_by_user_id) "
                "VALUES (:id, 'Race Plan', 'FAILOVER', :creator)"
            ),
            {"id": plan_id, "creator": admin_id},
        )
        await setup_session.commit()

    original_next_number = plans_import_commands.next_plan_version_number

    async def _paused_next_plan_version_number(session: AsyncSession, pid: uuid.UUID) -> int:
        result = await original_next_number(session, pid)
        await asyncio.sleep(0.2)
        return result

    monkeypatch.setattr(plans_import_commands, "next_plan_version_number", _paused_next_plan_version_number)

    try:

        async def _create_version() -> int:
            async with session_factory() as sess:
                version = await plans_import_commands.create_plan_version(
                    sess, actor_id=admin_id, plan_id=plan_id, version_type="DRAFT"
                )
                await sess.commit()
                return version.version_number

        results = await asyncio.gather(_create_version(), _create_version())
        assert sorted(results) == [1, 2]
    finally:
        async with session_factory() as cleanup_session:
            await cleanup_session.execute(
                text("DELETE FROM plan_versions WHERE plan_id = :pid"), {"pid": plan_id}
            )
            await cleanup_session.execute(text("DELETE FROM plans WHERE id = :pid"), {"pid": plan_id})
            await cleanup_session.execute(
                text("DELETE FROM role_assignments WHERE user_id = :uid"), {"uid": admin_id}
            )
            await cleanup_session.execute(text("DELETE FROM users WHERE id = :uid"), {"uid": admin_id})
            await cleanup_session.commit()

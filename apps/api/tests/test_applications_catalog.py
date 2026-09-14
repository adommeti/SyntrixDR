"""Route-level tests for the `applications_catalog` module (BUILD-03).

Mounts the REAL `app.applications_catalog.routes.router` (mirrors
`test_users_teams_org_routes.py`'s pattern) rather than calling commands/queries
directly, so capability guards, `Idempotency-Key` handling and HTTP status codes
are exercised end-to-end. See `docs/plan/increments/BUILD-03.plan.md` — "Test plan"
for the exact test names and endpoint/capability/audit-action table this file
implements.

Golden tier defaults (DATA_MODEL.md:116-124, invariant #9's SLA/weight source):
    TIER_0 30m/100, TIER_1 60m/75, TIER_2 120m/50, TIER_3 240m/30, TIER_4 1440m/20.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# This import is expected to fail right now — `app.applications_catalog` does not
# exist yet (BUILD-03 is test-first). The whole module fails collection with a
# ModuleNotFoundError until routes.py lands, which is the "fails for the right
# reason" signal this file is written to produce ahead of implementation.
from app.applications_catalog.routes import router as applications_catalog_router
from app.core.clock import Clock, FakeClock
from app.core.database import get_request_session
from app.core.errors import AppError, app_error_handler
from app.identity_auth.dependencies import get_clock, get_session_store
from app.identity_auth.session_store import RedisSessionStore
from app.users_teams_org.models import RoleAssignment

pytestmark = [pytest.mark.api, pytest.mark.integration]

_TIER_DEFAULTS = {
    "TIER_0": (30, 100),
    "TIER_1": (60, 75),
    "TIER_2": (120, 50),
    "TIER_3": (240, 30),
    "TIER_4": (1440, 20),
}


def _build_app(session: AsyncSession, redis_client: Redis, clock: FakeClock) -> FastAPI:
    app = FastAPI()
    app.add_exception_handler(AppError, app_error_handler)
    app.include_router(applications_catalog_router)

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
    """ENTRA identity keeps GLOBAL_ADMIN's bypass without needing D-235's LOCAL-TOTP dance —
    irrelevant plumbing for these capability-guard tests, mirrors test_authz_matrix.py."""
    user_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO users (id, identity_type, display_name, email, entra_object_id) "
            "VALUES (:id, 'ENTRA', 'Catalog Admin', :email, :oid)"
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


async def _tier_id(session: AsyncSession, code: str) -> uuid.UUID:
    row = await session.execute(text("SELECT id FROM tiers WHERE code = :code"), {"code": code})
    return row.scalar_one()


def _idem_headers(csrf_token: str) -> dict[str, str]:
    return {"X-CSRF-Token": csrf_token, "Idempotency-Key": str(uuid.uuid4())}


async def _create_application(
    client: AsyncClient,
    session_id: str,
    csrf_token: str,
    tier_id: uuid.UUID,
    *,
    name: str,
) -> Response:
    response = await client.post(
        "/api/v1/applications",
        cookies={"drcc_session": session_id},
        headers=_idem_headers(csrf_token),
        json={"name": name, "description": "d", "tier_id": str(tier_id), "external_system": "SN"},
    )
    return response


@pytest.mark.asyncio
async def test_create_application_requires_manage_capability(
    session: AsyncSession, redis_client: Redis, clock: FakeClock, seed_user: uuid.UUID
) -> None:
    """`MANAGE_APPLICATION_CATALOG` is Admin-only (BUILD-03.plan.md risk #1) — a plain
    authenticated user with no role assignment gets 403, never a silently-created row."""
    tier_id = await _tier_id(session, "TIER_2")
    app = _build_app(session, redis_client, clock)
    session_id, csrf_token = await _create_session_cookie(session, redis_client, clock, seed_user)

    async with await _client(app) as client:
        response = await _create_application(client, session_id, csrf_token, tier_id, name="App A")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "AUTHORIZATION_REQUIRED"
    count = await session.execute(text("SELECT COUNT(*) AS cnt FROM applications WHERE name = 'App A'"))
    assert count.one().cnt == 0
    await redis_client.delete(f"drcc:session:{session_id}")


@pytest.mark.asyncio
async def test_create_application_defaults_and_unique_name(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    """New Application starts at `version=1`, not soft-deleted; a second Application whose
    name matches case-insensitively conflicts with `ux_applications_name_active`."""
    admin_id = await _create_entra_admin(session)
    tier_id = await _tier_id(session, "TIER_1")
    app = _build_app(session, redis_client, clock)
    session_id, csrf_token = await _create_session_cookie(session, redis_client, clock, admin_id)

    async with await _client(app) as client:
        first = await _create_application(client, session_id, csrf_token, tier_id, name="Billing Service")
        assert first.status_code in (200, 201)
        body = first.json()
        assert body["version"] == 1
        assert body.get("deleted_at") is None

        second = await _create_application(client, session_id, csrf_token, tier_id, name="billing service")

    assert second.status_code in (409, 422)
    assert second.json()["error"]["code"]

    count = await session.execute(
        text("SELECT COUNT(*) AS cnt FROM applications WHERE lower(name) = 'billing service'")
    )
    assert count.one().cnt == 1
    await redis_client.delete(f"drcc:session:{session_id}")


@pytest.mark.asyncio
async def test_update_application_stale_version_returns_409(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    """D-214: every stale write outside the Manager-precedence case gets 409 CONCURRENCY_CONFLICT."""
    admin_id = await _create_entra_admin(session)
    tier_id = await _tier_id(session, "TIER_1")
    app = _build_app(session, redis_client, clock)
    session_id, csrf_token = await _create_session_cookie(session, redis_client, clock, admin_id)

    async with await _client(app) as client:
        created = await _create_application(client, session_id, csrf_token, tier_id, name="Payments API")
        app_id = created.json()["id"]

        first_patch = await client.patch(
            f"/api/v1/applications/{app_id}",
            cookies={"drcc_session": session_id},
            headers=_idem_headers(csrf_token),
            json={"description": "updated once", "expected_version": 1},
        )
        assert first_patch.status_code == 200
        assert first_patch.json()["version"] == 2

        stale_patch = await client.patch(
            f"/api/v1/applications/{app_id}",
            cookies={"drcc_session": session_id},
            headers=_idem_headers(csrf_token),
            json={"description": "updated twice, stale", "expected_version": 1},
        )

    assert stale_patch.status_code == 409
    assert stale_patch.json()["error"]["code"] == "CONCURRENCY_CONFLICT"
    await redis_client.delete(f"drcc:session:{session_id}")


@pytest.mark.asyncio
async def test_set_owners_enforces_max_three_per_type(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    """D-254: at most 3 slots per `owner_type` (`application_owners` unique on
    (application_id, owner_type, owner_order), owner_order constrained 1-3)."""
    admin_id = await _create_entra_admin(session)
    tier_id = await _tier_id(session, "TIER_1")
    owner_ids = [await _create_user(session, display_name=f"Owner {i}") for i in range(4)]
    app = _build_app(session, redis_client, clock)
    session_id, csrf_token = await _create_session_cookie(session, redis_client, clock, admin_id)

    async with await _client(app) as client:
        created = await _create_application(client, session_id, csrf_token, tier_id, name="Ledger")
        app_id = created.json()["id"]

        response = await client.put(
            f"/api/v1/applications/{app_id}/owners",
            cookies={"drcc_session": session_id},
            headers=_idem_headers(csrf_token),
            json={
                "owners": [
                    {"owner_type": "SYSTEM_APPLICATION", "owner_order": i + 1, "user_id": str(owner_ids[i])}
                    for i in range(4)
                ]
            },
        )

    assert response.status_code == 422
    count = await session.execute(
        text("SELECT COUNT(*) AS cnt FROM application_owners WHERE application_id = :aid"),
        {"aid": app_id},
    )
    assert count.one().cnt == 0
    await redis_client.delete(f"drcc:session:{session_id}")


@pytest.mark.asyncio
async def test_set_owners_rejects_owner_order_out_of_range(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    """`owner_order` must be validated at the request boundary (1-3) — a payload with an
    out-of-range order but a count <=3 and slot 1 present must 422 cleanly, not reach the DB
    and trip `application_owner_order_ck` as an unhandled 500 (found in review)."""
    admin_id = await _create_entra_admin(session)
    tier_id = await _tier_id(session, "TIER_1")
    owner_1 = await _create_user(session, display_name="Owner Primary")
    owner_5 = await _create_user(session, display_name="Owner Five")
    app = _build_app(session, redis_client, clock)
    session_id, csrf_token = await _create_session_cookie(session, redis_client, clock, admin_id)

    async with await _client(app) as client:
        created = await _create_application(client, session_id, csrf_token, tier_id, name="Fraud Engine")
        app_id = created.json()["id"]

        response = await client.put(
            f"/api/v1/applications/{app_id}/owners",
            cookies={"drcc_session": session_id},
            headers=_idem_headers(csrf_token),
            json={
                "owners": [
                    {"owner_type": "SYSTEM_APPLICATION", "owner_order": 1, "user_id": str(owner_1)},
                    {"owner_type": "SYSTEM_APPLICATION", "owner_order": 5, "user_id": str(owner_5)},
                ]
            },
        )

    assert response.status_code == 422
    count = await session.execute(
        text("SELECT COUNT(*) AS cnt FROM application_owners WHERE application_id = :aid"),
        {"aid": app_id},
    )
    assert count.one().cnt == 0
    await redis_client.delete(f"drcc:session:{session_id}")


@pytest.mark.asyncio
async def test_set_owners_requires_primary_when_any_slot_set(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    """D-254 data-shape rule: slot 1 (Primary) must be present whenever any slot for that
    `owner_type` is present — a payload with only slot 2 is rejected."""
    admin_id = await _create_entra_admin(session)
    tier_id = await _tier_id(session, "TIER_1")
    owner_id = await _create_user(session, display_name="Secondary Owner")
    app = _build_app(session, redis_client, clock)
    session_id, csrf_token = await _create_session_cookie(session, redis_client, clock, admin_id)

    async with await _client(app) as client:
        created = await _create_application(client, session_id, csrf_token, tier_id, name="Inventory")
        app_id = created.json()["id"]

        response = await client.put(
            f"/api/v1/applications/{app_id}/owners",
            cookies={"drcc_session": session_id},
            headers=_idem_headers(csrf_token),
            json={
                "owners": [
                    {"owner_type": "SYSTEM_APPLICATION", "owner_order": 2, "user_id": str(owner_id)},
                ]
            },
        )

    assert response.status_code == 422
    await redis_client.delete(f"drcc:session:{session_id}")


@pytest.mark.asyncio
async def test_set_owners_rejects_unknown_user_id(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    admin_id = await _create_entra_admin(session)
    tier_id = await _tier_id(session, "TIER_1")
    app = _build_app(session, redis_client, clock)
    session_id, csrf_token = await _create_session_cookie(session, redis_client, clock, admin_id)

    async with await _client(app) as client:
        created = await _create_application(client, session_id, csrf_token, tier_id, name="Reporting")
        app_id = created.json()["id"]

        response = await client.put(
            f"/api/v1/applications/{app_id}/owners",
            cookies={"drcc_session": session_id},
            headers=_idem_headers(csrf_token),
            json={
                "owners": [
                    {
                        "owner_type": "SYSTEM_APPLICATION",
                        "owner_order": 1,
                        "user_id": str(uuid.uuid4()),
                    },
                ]
            },
        )

    assert response.status_code == 404
    await redis_client.delete(f"drcc:session:{session_id}")


@pytest.mark.asyncio
async def test_set_owners_replaces_prior_slots_and_audits_once(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    """The second PUT fully replaces slot 1 (owner A -> owner B), leaves no stale A row,
    and writes exactly one `APPLICATION_OWNERS_SET` audit row per PUT call (not per slot)."""
    admin_id = await _create_entra_admin(session)
    tier_id = await _tier_id(session, "TIER_1")
    owner_a = await _create_user(session, display_name="Owner A")
    owner_b = await _create_user(session, display_name="Owner B")
    app = _build_app(session, redis_client, clock)
    session_id, csrf_token = await _create_session_cookie(session, redis_client, clock, admin_id)

    async with await _client(app) as client:
        created = await _create_application(client, session_id, csrf_token, tier_id, name="Payroll")
        app_id = created.json()["id"]

        first = await client.put(
            f"/api/v1/applications/{app_id}/owners",
            cookies={"drcc_session": session_id},
            headers=_idem_headers(csrf_token),
            json={
                "owners": [
                    {"owner_type": "SYSTEM_APPLICATION", "owner_order": 1, "user_id": str(owner_a)},
                ]
            },
        )
        assert first.status_code == 200

        second = await client.put(
            f"/api/v1/applications/{app_id}/owners",
            cookies={"drcc_session": session_id},
            headers=_idem_headers(csrf_token),
            json={
                "owners": [
                    {"owner_type": "SYSTEM_APPLICATION", "owner_order": 1, "user_id": str(owner_b)},
                ]
            },
        )
        assert second.status_code == 200

    active_owners = await session.execute(
        text(
            "SELECT user_id FROM application_owners "
            "WHERE application_id = :aid AND owner_type = 'SYSTEM_APPLICATION' AND deleted_at IS NULL"
        ),
        {"aid": app_id},
    )
    rows = active_owners.all()
    assert len(rows) == 1
    assert rows[0].user_id == owner_b

    audit_count = await session.execute(
        text(
            "SELECT COUNT(*) AS cnt FROM audit_events "
            "WHERE entity_id = :aid AND action = 'APPLICATION_OWNERS_SET'"
        ),
        {"aid": app_id},
    )
    assert audit_count.one().cnt == 2
    await redis_client.delete(f"drcc:session:{session_id}")


@pytest.mark.asyncio
async def test_application_history_returns_empty_list_pre_dr_events(
    session: AsyncSession, redis_client: Redis, clock: FakeClock, seed_user: uuid.UUID
) -> None:
    """D-212 comparison data doesn't exist until BUILD-04+ (dr_events) — the endpoint
    returns a real, correctly-shaped empty list rather than 501 or fabricated rows."""
    admin_id = await _create_entra_admin(session)
    tier_id = await _tier_id(session, "TIER_1")
    app = _build_app(session, redis_client, clock)
    admin_session_id, admin_csrf = await _create_session_cookie(session, redis_client, clock, admin_id)

    async with await _client(app) as client:
        created = await _create_application(client, admin_session_id, admin_csrf, tier_id, name="Search")
        app_id = created.json()["id"]

        reader_session_id, _ = await _create_session_cookie(session, redis_client, clock, seed_user)
        response = await client.get(
            f"/api/v1/applications/{app_id}/history", cookies={"drcc_session": reader_session_id}
        )

    assert response.status_code == 200
    body = response.json()
    items = body if isinstance(body, list) else body["items"]
    assert items == []
    await redis_client.delete(f"drcc:session:{admin_session_id}")


@pytest.mark.asyncio
async def test_list_tiers_returns_all_five_seeded_rows_with_frozen_defaults(
    session: AsyncSession, redis_client: Redis, clock: FakeClock, seed_user: uuid.UUID
) -> None:
    """Golden defaults, DATA_MODEL.md:116-124: any authenticated user may read tiers."""
    app = _build_app(session, redis_client, clock)
    session_id, _ = await _create_session_cookie(session, redis_client, clock, seed_user)

    async with await _client(app) as client:
        response = await client.get("/api/v1/admin/tiers", cookies={"drcc_session": session_id})

    assert response.status_code == 200
    body = response.json()
    tiers = body if isinstance(body, list) else body["tiers"]
    assert len(tiers) == 5
    by_code = {t["code"]: t for t in tiers}
    assert set(by_code) == set(_TIER_DEFAULTS)
    for code, (sla_minutes, weight) in _TIER_DEFAULTS.items():
        assert by_code[code]["default_sla_minutes"] == sla_minutes
        assert by_code[code]["default_health_weight"] == weight
    await redis_client.delete(f"drcc:session:{session_id}")


@pytest.mark.asyncio
async def test_update_tiers_requires_manage_tiers_capability(
    session: AsyncSession, redis_client: Redis, clock: FakeClock, seed_user: uuid.UUID
) -> None:
    tier_id = await _tier_id(session, "TIER_0")
    app = _build_app(session, redis_client, clock)
    session_id, csrf_token = await _create_session_cookie(session, redis_client, clock, seed_user)

    async with await _client(app) as client:
        response = await client.put(
            "/api/v1/admin/tiers",
            cookies={"drcc_session": session_id},
            headers=_idem_headers(csrf_token),
            json={
                "tiers": [
                    {
                        "code": "TIER_0",
                        "expected_version": 1,
                        "default_sla_minutes": 45,
                        "default_health_weight": 90,
                    }
                ]
            },
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "AUTHORIZATION_REQUIRED"
    row = await session.execute(text("SELECT default_sla_minutes FROM tiers WHERE id = :id"), {"id": tier_id})
    assert row.scalar_one() == 30
    await redis_client.delete(f"drcc:session:{session_id}")


@pytest.mark.asyncio
async def test_update_tiers_rejects_unknown_code(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    """No V1 mechanism adds a 6th tier (BUILD-03.plan.md risk #3) — `PUT /admin/tiers` edits
    the 5 existing rows only, an unrecognised `code` is rejected, not upserted as a new tier."""
    admin_id = await _create_entra_admin(session)
    app = _build_app(session, redis_client, clock)
    session_id, csrf_token = await _create_session_cookie(session, redis_client, clock, admin_id)

    async with await _client(app) as client:
        response = await client.put(
            "/api/v1/admin/tiers",
            cookies={"drcc_session": session_id},
            headers=_idem_headers(csrf_token),
            json={
                "tiers": [
                    {
                        "code": "TIER_9",
                        "expected_version": 1,
                        "default_sla_minutes": 15,
                        "default_health_weight": 5,
                    }
                ]
            },
        )

    assert response.status_code in (404, 422)
    count = await session.execute(text("SELECT COUNT(*) AS cnt FROM tiers"))
    assert count.one().cnt == 5
    await redis_client.delete(f"drcc:session:{session_id}")


@pytest.mark.asyncio
async def test_update_tiers_audits_one_row_per_changed_tier(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    admin_id = await _create_entra_admin(session)
    tier_0_id = await _tier_id(session, "TIER_0")
    tier_1_id = await _tier_id(session, "TIER_1")
    app = _build_app(session, redis_client, clock)
    session_id, csrf_token = await _create_session_cookie(session, redis_client, clock, admin_id)

    async with await _client(app) as client:
        response = await client.put(
            "/api/v1/admin/tiers",
            cookies={"drcc_session": session_id},
            headers=_idem_headers(csrf_token),
            json={
                "tiers": [
                    {
                        "code": "TIER_0",
                        "expected_version": 1,
                        "default_sla_minutes": 25,
                        "default_health_weight": 100,
                    },
                    {
                        "code": "TIER_1",
                        "expected_version": 1,
                        "default_sla_minutes": 55,
                        "default_health_weight": 75,
                    },
                ]
            },
        )

    assert response.status_code == 200
    for tier_id in (tier_0_id, tier_1_id):
        audit_count = await session.execute(
            text(
                "SELECT COUNT(*) AS cnt FROM audit_events "
                "WHERE entity_id = :tid AND action = 'TIER_UPDATED'"
            ),
            {"tid": tier_id},
        )
        assert audit_count.one().cnt == 1


@pytest.mark.asyncio
async def test_update_tiers_stale_version_returns_409(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    """`Tier` is a versioned mutable aggregate like `Application` — a stale `expected_version`
    on `PUT /admin/tiers` must 409, not silently last-writer-wins (found in review)."""
    admin_id = await _create_entra_admin(session)
    tier_id = await _tier_id(session, "TIER_2")
    app = _build_app(session, redis_client, clock)
    session_id, csrf_token = await _create_session_cookie(session, redis_client, clock, admin_id)

    async with await _client(app) as client:
        first = await client.put(
            "/api/v1/admin/tiers",
            cookies={"drcc_session": session_id},
            headers=_idem_headers(csrf_token),
            json={
                "tiers": [
                    {
                        "code": "TIER_2",
                        "expected_version": 1,
                        "default_sla_minutes": 100,
                        "default_health_weight": 50,
                    }
                ]
            },
        )
        assert first.status_code == 200

        stale = await client.put(
            "/api/v1/admin/tiers",
            cookies={"drcc_session": session_id},
            headers=_idem_headers(csrf_token),
            json={
                "tiers": [
                    {
                        "code": "TIER_2",
                        "expected_version": 1,
                        "default_sla_minutes": 110,
                        "default_health_weight": 45,
                    }
                ]
            },
        )

    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "CONCURRENCY_CONFLICT"
    row = await session.execute(text("SELECT default_sla_minutes FROM tiers WHERE id = :id"), {"id": tier_id})
    assert row.scalar_one() == 100
    await redis_client.delete(f"drcc:session:{session_id}")
    await redis_client.delete(f"drcc:session:{session_id}")

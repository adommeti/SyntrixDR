"""Route-level tests for the `policies_admin` module (BUILD-03).

Mounts the REAL `app.policies_admin.routes.router` (mirrors
`test_applications_catalog.py`'s harness exactly — same DI-override pattern
via `httpx.AsyncClient` + `ASGITransport`) so capability guards, scope
precedence, `Idempotency-Key` handling and audit side effects are exercised
end-to-end. See `docs/plan/increments/BUILD-03.plan.md` — "Test plan" for the
exact test names and endpoint/capability/audit-action table this file
implements.

Precedence under test (BUILD-03.plan.md "Domain first" + invariant #9's
sibling policy invariant): APPLICATION -> WORK_STREAM -> DR_EVENT -> GLOBAL,
most-specific non-superseded `policy_values` row wins, else falls back to
`policy_definitions.default_value`.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock, FakeClock
from app.core.database import get_request_session
from app.core.errors import AppError, app_error_handler
from app.identity_auth.dependencies import get_clock, get_session_store
from app.identity_auth.session_store import RedisSessionStore

# This import is expected to fail right now -- `app.policies_admin` does not
# exist yet (BUILD-03 is test-first). The whole module fails collection with a
# ModuleNotFoundError until routes.py lands, which is the "fails for the right
# reason" signal this file is written to produce ahead of implementation.
from app.policies_admin.routes import router as policies_admin_router
from app.users_teams_org.models import RoleAssignment

pytestmark = [pytest.mark.api, pytest.mark.integration]


def _build_app(session: AsyncSession, redis_client: Redis, clock: FakeClock) -> FastAPI:
    app = FastAPI()
    app.add_exception_handler(AppError, app_error_handler)
    app.include_router(policies_admin_router)

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
    """ENTRA identity keeps GLOBAL_ADMIN's bypass without needing D-235's LOCAL-TOTP dance --
    irrelevant plumbing for these capability-guard tests, mirrors test_authz_matrix.py."""
    user_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO users (id, identity_type, display_name, email, entra_object_id) "
            "VALUES (:id, 'ENTRA', 'Policy Admin', :email, :oid)"
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
) -> RoleAssignment:
    ra = RoleAssignment(user_id=user_id, role_key=role_key, scope_type=scope_type, scope_id=scope_id)
    session.add(ra)
    await session.flush()
    return ra


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


async def _policy_definition_id(session: AsyncSession, key: str) -> uuid.UUID:
    row = await session.execute(text("SELECT id FROM policy_definitions WHERE key = :key"), {"key": key})
    return row.scalar_one()


async def _insert_dr_event(session: AsyncSession, *, name: str = "Fake Event") -> uuid.UUID:
    """Minimal `dr_events` row -- only the columns `policy_values.scope_id`'s FK-adjacent
    tests need exist; no DR Event lifecycle behaviour is under test here (that's BUILD-04+)."""
    event_id = uuid.uuid4()
    creator_id = await _create_user(session, display_name="Event Creator")
    await session.execute(
        text(
            "INSERT INTO dr_events "
            "(id, name, event_type, status, created_by_user_id, created_at, updated_at) "
            "VALUES (:id, :name, 'PLANNED_DR', 'PLANNED', :creator_id, now(), now())"
        ),
        {"id": event_id, "name": name, "creator_id": creator_id},
    )
    await session.flush()
    return event_id


async def _get_policies(
    client: AsyncClient,
    session_id: str,
    *,
    event_id: uuid.UUID | None = None,
    work_stream_id: uuid.UUID | None = None,
    application_id: uuid.UUID | None = None,
) -> Response:
    params: dict[str, str] = {}
    if event_id is not None:
        params["event_id"] = str(event_id)
    if work_stream_id is not None:
        params["work_stream_id"] = str(work_stream_id)
    if application_id is not None:
        params["application_id"] = str(application_id)
    return await client.get("/api/v1/admin/policies", cookies={"drcc_session": session_id}, params=params)


async def _put_policy(
    client: AsyncClient,
    session_id: str,
    csrf_token: str,
    *,
    key: str,
    value: object,
    scope_type: str,
    scope_id: uuid.UUID | None = None,
) -> Response:
    payload: dict[str, object] = {"key": key, "value": value, "scope_type": scope_type}
    if scope_id is not None:
        payload["scope_id"] = str(scope_id)
    return await client.put(
        "/api/v1/admin/policies",
        cookies={"drcc_session": session_id},
        headers=_idem_headers(csrf_token),
        json=payload,
    )


def _effective_value(body: Any, key: str) -> object:
    """`GET /admin/policies` returns a list of {key, value, ...} rows (or {"items": [...]})
    -- unwrap either shape and find the row for `key`."""
    items = body if isinstance(body, list) else body["items"]
    for row in items:
        if row["key"] == key:
            return row["value"]
    raise AssertionError(f"key {key!r} not present in GET /admin/policies response")


@pytest.mark.asyncio
async def test_resolve_falls_back_to_global_default_when_no_override(
    session: AsyncSession, redis_client: Redis, clock: FakeClock, seed_user: uuid.UUID
) -> None:
    """No `policy_values` row exists for `sla.warning_percent` -- resolve falls back to
    `policy_definitions.default_value` (85), per BUILD-03.plan.md's precedence rule."""
    app = _build_app(session, redis_client, clock)
    session_id, _ = await _create_session_cookie(session, redis_client, clock, seed_user)

    async with await _client(app) as client:
        response = await _get_policies(client, session_id)

    assert response.status_code == 200
    assert _effective_value(response.json(), "sla.warning_percent") == 85


@pytest.mark.asyncio
async def test_resolve_prefers_application_over_work_stream_over_event_over_global(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    """Seeds four `policy_values` rows for the same key at GLOBAL, DR_EVENT, WORK_STREAM
    and APPLICATION scope and asserts the APPLICATION-scoped row wins when all four scope
    ids are passed to the resolve query -- the most-specific-non-superseded-row-wins rule."""
    admin_id = await _create_entra_admin(session)
    app = _build_app(session, redis_client, clock)
    session_id, csrf_token = await _create_session_cookie(session, redis_client, clock, admin_id)

    event_id = await _insert_dr_event(session)
    work_stream_id = uuid.uuid4()
    application_id = uuid.uuid4()

    async with await _client(app) as client:
        global_write = await _put_policy(
            client, session_id, csrf_token, key="file.max_mb", value=50, scope_type="GLOBAL"
        )
        assert global_write.status_code == 200

        event_write = await _put_policy(
            client,
            session_id,
            csrf_token,
            key="file.max_mb",
            value=60,
            scope_type="DR_EVENT",
            scope_id=event_id,
        )
        assert event_write.status_code == 200

        stream_write = await _put_policy(
            client,
            session_id,
            csrf_token,
            key="file.max_mb",
            value=70,
            scope_type="WORK_STREAM",
            scope_id=work_stream_id,
        )
        assert stream_write.status_code == 200

        app_write = await _put_policy(
            client,
            session_id,
            csrf_token,
            key="file.max_mb",
            value=80,
            scope_type="APPLICATION",
            scope_id=application_id,
        )
        assert app_write.status_code == 200

        resolved = await _get_policies(
            client,
            session_id,
            event_id=event_id,
            work_stream_id=work_stream_id,
            application_id=application_id,
        )

    assert resolved.status_code == 200
    assert _effective_value(resolved.json(), "file.max_mb") == 80


@pytest.mark.asyncio
async def test_admin_can_write_global_scope(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    admin_id = await _create_entra_admin(session)
    app = _build_app(session, redis_client, clock)
    session_id, csrf_token = await _create_session_cookie(session, redis_client, clock, admin_id)

    async with await _client(app) as client:
        response = await _put_policy(
            client, session_id, csrf_token, key="sla.warning_percent", value=90, scope_type="GLOBAL"
        )

    assert response.status_code == 200
    definition_id = await _policy_definition_id(session, "sla.warning_percent")
    row = await session.execute(
        text(
            "SELECT value FROM policy_values "
            "WHERE policy_definition_id = :did AND scope_type = 'GLOBAL' AND superseded_at IS NULL"
        ),
        {"did": definition_id},
    )
    assert row.scalar_one() == 90


@pytest.mark.asyncio
async def test_coordinator_cannot_write_global_scope(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    """The `"SCOPE"` grant marker denies a Coordinator when `scope_type` is GLOBAL --
    Coordinator write authority is scoped to their own DR_EVENT, never global config."""
    coordinator_id = await _create_user(session, display_name="Coordinator")
    event_id = uuid.uuid4()
    await _grant_role(session, coordinator_id, "DR_COORDINATOR", scope_type="DR_EVENT", scope_id=event_id)
    app = _build_app(session, redis_client, clock)
    session_id, csrf_token = await _create_session_cookie(session, redis_client, clock, coordinator_id)

    async with await _client(app) as client:
        response = await _put_policy(
            client, session_id, csrf_token, key="sla.warning_percent", value=90, scope_type="GLOBAL"
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "AUTHORIZATION_REQUIRED"


@pytest.mark.asyncio
async def test_coordinator_can_write_event_scoped_override(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    """A Coordinator holding `DR_COORDINATOR` at a specific DR_EVENT scope may write a
    `DR_EVENT`-scoped override for that same event -- the `"SCOPE"` marker grants when the
    payload's scope matches the role assignment's scope."""
    coordinator_id = await _create_user(session, display_name="Coordinator")
    event_id = await _insert_dr_event(session)
    await _grant_role(session, coordinator_id, "DR_COORDINATOR", scope_type="DR_EVENT", scope_id=event_id)
    app = _build_app(session, redis_client, clock)
    session_id, csrf_token = await _create_session_cookie(session, redis_client, clock, coordinator_id)

    async with await _client(app) as client:
        response = await _put_policy(
            client,
            session_id,
            csrf_token,
            key="sla.warning_percent",
            value=70,
            scope_type="DR_EVENT",
            scope_id=event_id,
        )

    assert response.status_code == 200
    definition_id = await _policy_definition_id(session, "sla.warning_percent")
    row = await session.execute(
        text(
            "SELECT value FROM policy_values "
            "WHERE policy_definition_id = :did AND scope_type = 'DR_EVENT' "
            "AND scope_id = :sid AND superseded_at IS NULL"
        ),
        {"did": definition_id, "sid": event_id},
    )
    assert row.scalar_one() == 70


@pytest.mark.asyncio
async def test_policy_write_rejects_readiness_dependency_graph_acyclic_key(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    """schema_v2_reconciliation.sql:434 -- `readiness.dependency_graph_acyclic` is fixed at
    HARD_STOP and NOT configurable; `set_policy_value` rejects any write to this key with a
    422, regardless of actor or scope (BUILD-03.plan.md risk #4)."""
    admin_id = await _create_entra_admin(session)
    app = _build_app(session, redis_client, clock)
    session_id, csrf_token = await _create_session_cookie(session, redis_client, clock, admin_id)

    async with await _client(app) as client:
        response = await _put_policy(
            client,
            session_id,
            csrf_token,
            key="readiness.dependency_graph_acyclic",
            value="WARNING",
            scope_type="GLOBAL",
        )

    assert response.status_code == 422
    definition_id = await _policy_definition_id(session, "readiness.dependency_graph_acyclic")
    count = await session.execute(
        text("SELECT COUNT(*) AS cnt FROM policy_values WHERE policy_definition_id = :did"),
        {"did": definition_id},
    )
    assert count.one().cnt == 0


@pytest.mark.asyncio
async def test_policy_write_denies_unauthorized_actor_before_checking_the_key(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    """Authorization must be checked before the locked-key/key-existence guards, so an actor
    with no `GLOBAL_POLICY_CONFIG` grant at all gets 403 without learning whether the key
    exists or is locked (found in review — the guards previously ran before the authz check)."""
    plain_user = await _create_user(session, display_name="No Roles")
    app = _build_app(session, redis_client, clock)
    session_id, csrf_token = await _create_session_cookie(session, redis_client, clock, plain_user)

    async with await _client(app) as client:
        response = await _put_policy(
            client,
            session_id,
            csrf_token,
            key="readiness.dependency_graph_acyclic",
            value="WARNING",
            scope_type="GLOBAL",
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "AUTHORIZATION_REQUIRED"


@pytest.mark.asyncio
async def test_policy_write_supersedes_prior_value_not_deletes(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    """Invariant #11 (audit is append-only) applies to `policy_values` too -- a second write
    to the same key/scope sets `superseded_at` on the prior row rather than deleting it; the
    new row is the sole non-superseded row for that key/scope."""
    admin_id = await _create_entra_admin(session)
    app = _build_app(session, redis_client, clock)
    session_id, csrf_token = await _create_session_cookie(session, redis_client, clock, admin_id)

    async with await _client(app) as client:
        first = await _put_policy(
            client, session_id, csrf_token, key="file.max_mb", value=50, scope_type="GLOBAL"
        )
        assert first.status_code == 200

        second = await _put_policy(
            client, session_id, csrf_token, key="file.max_mb", value=60, scope_type="GLOBAL"
        )
        assert second.status_code == 200

    definition_id = await _policy_definition_id(session, "file.max_mb")
    rows = await session.execute(
        text(
            "SELECT value, superseded_at FROM policy_values "
            "WHERE policy_definition_id = :did AND scope_type = 'GLOBAL' ORDER BY created_at"
        ),
        {"did": definition_id},
    )
    all_rows = rows.all()
    assert len(all_rows) == 2
    assert all_rows[0].value == 50
    assert all_rows[0].superseded_at is not None
    assert all_rows[1].value == 60
    assert all_rows[1].superseded_at is None


@pytest.mark.asyncio
async def test_policy_write_audits_with_before_after_value(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    """Every `policy_values` write is audited as `POLICY_VALUE_SET` with `entity_type =
    'POLICY_VALUE'` (schema_v2_reconciliation.sql's audit superset) and before/after values
    recorded so the change is reconstructable without diffing `policy_values` rows."""
    admin_id = await _create_entra_admin(session)
    app = _build_app(session, redis_client, clock)
    session_id, csrf_token = await _create_session_cookie(session, redis_client, clock, admin_id)

    async with await _client(app) as client:
        first = await _put_policy(
            client, session_id, csrf_token, key="file.max_mb", value=50, scope_type="GLOBAL"
        )
        assert first.status_code == 200
        second = await _put_policy(
            client, session_id, csrf_token, key="file.max_mb", value=60, scope_type="GLOBAL"
        )
        assert second.status_code == 200

    row = await session.execute(
        text(
            "SELECT before_data, after_data FROM audit_events "
            "WHERE entity_type = 'POLICY_VALUE' AND action = 'POLICY_VALUE_SET' "
            "ORDER BY occurred_at DESC LIMIT 1"
        )
    )
    audited = row.one()
    assert audited.before_data is not None
    assert audited.after_data is not None
    before_value = audited.before_data.get("value", audited.before_data)
    after_value = audited.after_data.get("value", audited.after_data)
    assert before_value == 50
    assert after_value == 60


@pytest.mark.asyncio
async def test_get_policies_lists_all_seeded_keys(
    session: AsyncSession, redis_client: Redis, clock: FakeClock, seed_user: uuid.UUID
) -> None:
    """`GET /admin/policies` lists every seeded `policy_definitions` row (D-225/D-224 keys) --
    asserted against the live DB count rather than a hardcoded literal, since the exact seed
    count is a schema-owned fact (schema_v2_reconciliation.sql:439-490), not a test-owned one."""
    app = _build_app(session, redis_client, clock)
    session_id, _ = await _create_session_cookie(session, redis_client, clock, seed_user)

    db_keys_result = await session.execute(text("SELECT key FROM policy_definitions"))
    db_keys = {row.key for row in db_keys_result}

    async with await _client(app) as client:
        response = await _get_policies(client, session_id)

    assert response.status_code == 200
    body = response.json()
    items = body if isinstance(body, list) else body["items"]
    response_keys = {row["key"] for row in items}
    assert response_keys == db_keys
    assert "readiness.dependency_graph_acyclic" in response_keys
    assert "sla.warning_percent" in response_keys

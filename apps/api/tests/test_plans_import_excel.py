"""End-to-end tests for BUILD-05's Excel import (`POST /dr-events/{id}/imports/excel`,
`GET /imports/{id}`, `POST /imports/{id}/accept`, D-221). Mounts the real
`plans_import.routes.router` via `httpx.AsyncClient` + `ASGITransport`, same harness shape as
`test_plans_import.py`. The Celery parse job is exercised via `jobs.py::parse_import_job` directly
(the DI-friendly core, not `_parse_import_job_async`'s own-engine wrapper) so it shares the test's
`session` fixture -- no worker consumes the real Celery queue in this test harness
(BUILD-05.plan.md Test plan).
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock, FakeClock
from app.core.config import get_settings
from app.core.database import get_request_session
from app.core.errors import AppError, app_error_handler
from app.core.storage import ObjectStore
from app.identity_auth.dependencies import get_clock, get_object_store, get_session_store
from app.identity_auth.session_store import RedisSessionStore
from app.plans_import.commands import create_import_job
from app.plans_import.jobs import parse_import_job
from app.plans_import.routes import router as plans_import_router
from app.users_teams_org.models import RoleAssignment
from tests.fixtures.imports.workbooks import (
    canonical_workbook,
    messy_workbook,
    reordered_renamed_workbook,
)

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
    app.dependency_overrides[get_object_store] = lambda: ObjectStore(
        get_settings().azure_storage_connection_string
    )
    return app


async def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _create_entra_admin(session: AsyncSession) -> uuid.UUID:
    user_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO users (id, identity_type, display_name, email, entra_object_id) "
            "VALUES (:id, 'ENTRA', 'Import Admin', :email, :oid)"
        ),
        {"id": user_id, "email": f"{user_id}@example.test", "oid": str(uuid.uuid4())},
    )
    session.add(RoleAssignment(user_id=user_id, role_key="GLOBAL_ADMIN", scope_type="GLOBAL"))
    await session.flush()
    return user_id


async def _create_user(session: AsyncSession, *, display_name: str) -> uuid.UUID:
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


async def _create_event_row(session: AsyncSession, *, actor_id: uuid.UUID, name: str) -> uuid.UUID:
    event_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO dr_events "
            "(id, name, event_type, status, version, created_by_user_id, created_at, updated_at) "
            "VALUES (:id, :name, 'PLANNED_DR', 'PLANNED', 1, :creator, now(), now())"
        ),
        {"id": event_id, "name": name, "creator": actor_id},
    )
    await session.flush()
    return event_id


async def _create_team(session: AsyncSession, *, name: str) -> uuid.UUID:
    team_id = uuid.uuid4()
    await session.execute(
        text("INSERT INTO teams (id, name) VALUES (:id, :name)"), {"id": team_id, "name": name}
    )
    await session.flush()
    return team_id


async def _create_application(session: AsyncSession, *, name: str) -> uuid.UUID:
    application_id = uuid.uuid4()
    tier_row = await session.execute(text("SELECT id FROM tiers WHERE code = 'TIER_1'"))
    tier_id = tier_row.scalar_one()
    await session.execute(
        text("INSERT INTO applications (id, name, tier_id) VALUES (:id, :name, :tier_id)"),
        {"id": application_id, "name": name, "tier_id": tier_id},
    )
    await session.flush()
    return application_id


async def _scope_application_to_event(
    session: AsyncSession, *, dr_event_id: uuid.UUID, application_id: uuid.UUID
) -> None:
    tier_row = await session.execute(text("SELECT id FROM tiers WHERE code = 'TIER_1'"))
    tier_id = tier_row.scalar_one()
    await session.execute(
        text(
            "INSERT INTO dr_applications "
            "(id, dr_event_id, application_id, effective_tier_id, effective_sla_minutes, "
            "rto_target_minutes, status, version, created_at, updated_at) "
            "VALUES (:id, :eid, :aid, :tid, 120, 120, 'NOT_STARTED', 1, now(), now())"
        ),
        {"id": uuid.uuid4(), "eid": dr_event_id, "aid": application_id, "tid": tier_id},
    )
    await session.flush()


async def _upload_and_parse(
    session: AsyncSession,
    *,
    actor_id: uuid.UUID,
    dr_event_id: uuid.UUID,
    filename: str,
    content: bytes,
) -> uuid.UUID:
    """Command-layer upload + direct (non-Celery) parse, for tests that only need a PARSED job
    and don't care about exercising the HTTP upload route itself. Uses `parse_import_job` (not
    `_parse_import_job_async`) so it shares the test's own `session`/connection -- a genuinely
    separate engine connection can't see rows the test's transaction-scoped session hasn't truly
    committed (the `session` fixture wraps the whole test in one outer transaction)."""
    object_store = ObjectStore(get_settings().azure_storage_connection_string)
    import_job = await create_import_job(
        session,
        actor_id=actor_id,
        dr_event_id=dr_event_id,
        filename=filename,
        content=content,
        object_store=object_store,
    )
    await parse_import_job(session, object_store=object_store, import_job_id=import_job.id)
    return import_job.id


@pytest.mark.asyncio
async def test_upload_requires_manage_imports_capability(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    bare_user_id = await _create_user(session, display_name="No Capability User")
    event_id = await _create_event_row(session, actor_id=bare_user_id, name="Upload Event")
    app = _build_app(session, redis_client, clock)
    session_id, csrf_token = await _create_session_cookie(session, redis_client, clock, bare_user_id)

    async with await _client(app) as client:
        response = await client.post(
            f"/api/v1/dr-events/{event_id}/imports/excel",
            cookies={"drcc_session": session_id},
            headers=_idem_headers(csrf_token),
            files={"file": ("plan.xlsx", canonical_workbook(), "application/octet-stream")},
        )

    assert response.status_code == 403
    await redis_client.delete(f"drcc:session:{session_id}")


@pytest.mark.asyncio
async def test_upload_rejects_non_xlsx_extension(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    admin_id = await _create_entra_admin(session)
    event_id = await _create_event_row(session, actor_id=admin_id, name="Bad Extension Event")
    app = _build_app(session, redis_client, clock)
    session_id, csrf_token = await _create_session_cookie(session, redis_client, clock, admin_id)

    async with await _client(app) as client:
        response = await client.post(
            f"/api/v1/dr-events/{event_id}/imports/excel",
            cookies={"drcc_session": session_id},
            headers=_idem_headers(csrf_token),
            files={"file": ("plan.exe", b"not a workbook", "application/octet-stream")},
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "UNSUPPORTED_FILE_TYPE"
    await redis_client.delete(f"drcc:session:{session_id}")


@pytest.mark.asyncio
async def test_upload_rejects_oversized_file(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    admin_id = await _create_entra_admin(session)
    event_id = await _create_event_row(session, actor_id=admin_id, name="Oversized Event")
    await session.execute(text("UPDATE file_policy SET max_bytes = 10 WHERE singleton = TRUE"))
    await session.flush()
    app = _build_app(session, redis_client, clock)
    session_id, csrf_token = await _create_session_cookie(session, redis_client, clock, admin_id)

    async with await _client(app) as client:
        response = await client.post(
            f"/api/v1/dr-events/{event_id}/imports/excel",
            cookies={"drcc_session": session_id},
            headers=_idem_headers(csrf_token),
            files={"file": ("plan.xlsx", canonical_workbook(), "application/octet-stream")},
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "FILE_TOO_LARGE"
    await redis_client.delete(f"drcc:session:{session_id}")


@pytest.mark.asyncio
async def test_upload_without_idempotency_key_returns_400(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    admin_id = await _create_entra_admin(session)
    event_id = await _create_event_row(session, actor_id=admin_id, name="No Idem Event")
    app = _build_app(session, redis_client, clock)
    session_id, csrf_token = await _create_session_cookie(session, redis_client, clock, admin_id)

    async with await _client(app) as client:
        response = await client.post(
            f"/api/v1/dr-events/{event_id}/imports/excel",
            cookies={"drcc_session": session_id},
            headers={"X-CSRF-Token": csrf_token},
            files={"file": ("plan.xlsx", canonical_workbook(), "application/octet-stream")},
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    await redis_client.delete(f"drcc:session:{session_id}")


@pytest.mark.asyncio
async def test_upload_replay_returns_the_same_import_job(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    admin_id = await _create_entra_admin(session)
    event_id = await _create_event_row(session, actor_id=admin_id, name="Replay Event")
    app = _build_app(session, redis_client, clock)
    session_id, csrf_token = await _create_session_cookie(session, redis_client, clock, admin_id)
    headers = _idem_headers(csrf_token)

    async with await _client(app) as client:
        # `require_idempotency_key` hashes the raw request body -- httpx's `files=` convenience
        # param regenerates a random multipart boundary on every call, so two logically-identical
        # uploads via `files=` would produce different raw bytes and correctly fail as a mismatch,
        # not a replay. A real client retry resends the exact same encoded bytes; built once here
        # and reused for both calls to model that.
        built = client.build_request(
            "POST",
            f"/api/v1/dr-events/{event_id}/imports/excel",
            files={"file": ("plan.xlsx", canonical_workbook(), "application/octet-stream")},
        )
        raw_body = await built.aread()
        content_type = built.headers["content-type"]

        first = await client.post(
            f"/api/v1/dr-events/{event_id}/imports/excel",
            cookies={"drcc_session": session_id},
            headers={**headers, "content-type": content_type},
            content=raw_body,
        )
        replay = await client.post(
            f"/api/v1/dr-events/{event_id}/imports/excel",
            cookies={"drcc_session": session_id},
            headers={**headers, "content-type": content_type},
            content=raw_body,
        )

    assert first.status_code == 201
    assert replay.status_code == 201
    assert replay.json()["id"] == first.json()["id"]

    count = await session.execute(
        text("SELECT COUNT(*) AS cnt FROM import_jobs WHERE dr_event_id = :eid"), {"eid": event_id}
    )
    assert count.one().cnt == 1
    await redis_client.delete(f"drcc:session:{session_id}")


@pytest.mark.asyncio
async def test_get_import_job_404s_for_a_caller_who_cannot_see_the_event(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    admin_id = await _create_entra_admin(session)
    event_id = await _create_event_row(session, actor_id=admin_id, name="Private Event")
    import_job_id = await _upload_and_parse(
        session, actor_id=admin_id, dr_event_id=event_id, filename="plan.xlsx", content=canonical_workbook()
    )

    outsider_id = await _create_user(session, display_name="Outsider")
    app = _build_app(session, redis_client, clock)
    session_id, _csrf_token = await _create_session_cookie(session, redis_client, clock, outsider_id)

    async with await _client(app) as client:
        response = await client.get(f"/api/v1/imports/{import_job_id}", cookies={"drcc_session": session_id})

    assert response.status_code == 404
    await redis_client.delete(f"drcc:session:{session_id}")


@pytest.mark.asyncio
async def test_canonical_workbook_parses_with_full_confidence_mapping(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    admin_id = await _create_entra_admin(session)
    event_id = await _create_event_row(session, actor_id=admin_id, name="Canonical Parse Event")
    import_job_id = await _upload_and_parse(
        session, actor_id=admin_id, dr_event_id=event_id, filename="plan.xlsx", content=canonical_workbook()
    )
    app = _build_app(session, redis_client, clock)
    session_id, _csrf_token = await _create_session_cookie(session, redis_client, clock, admin_id)

    async with await _client(app) as client:
        response = await client.get(f"/api/v1/imports/{import_job_id}", cookies={"drcc_session": session_id})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "PARSED"
    assert len(body["rows"]) == 2
    by_header = {m["source_header"]: m for m in body["proposed_mapping"]}
    assert by_header["Task"]["target_field"] == "title"
    assert by_header["Task"]["confidence"] == 1.0
    await redis_client.delete(f"drcc:session:{session_id}")


@pytest.mark.asyncio
async def test_reordered_renamed_workbook_still_maps_correctly(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    admin_id = await _create_entra_admin(session)
    event_id = await _create_event_row(session, actor_id=admin_id, name="Reordered Parse Event")
    import_job_id = await _upload_and_parse(
        session,
        actor_id=admin_id,
        dr_event_id=event_id,
        filename="plan.xlsx",
        content=reordered_renamed_workbook(),
    )
    app = _build_app(session, redis_client, clock)
    session_id, _csrf_token = await _create_session_cookie(session, redis_client, clock, admin_id)

    async with await _client(app) as client:
        response = await client.get(f"/api/v1/imports/{import_job_id}", cookies={"drcc_session": session_id})

    assert response.status_code == 200
    by_header = {m["source_header"]: m for m in response.json()["proposed_mapping"]}
    assert by_header["Task Title"]["target_field"] == "title"
    assert by_header["Team"]["target_field"] == "owning_team"
    await redis_client.delete(f"drcc:session:{session_id}")


@pytest.mark.asyncio
async def test_corrupt_file_marks_job_parse_failed(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    admin_id = await _create_entra_admin(session)
    event_id = await _create_event_row(session, actor_id=admin_id, name="Corrupt Parse Event")
    import_job_id = await _upload_and_parse(
        session, actor_id=admin_id, dr_event_id=event_id, filename="plan.xlsx", content=b"not a workbook"
    )
    app = _build_app(session, redis_client, clock)
    session_id, _csrf_token = await _create_session_cookie(session, redis_client, clock, admin_id)

    async with await _client(app) as client:
        response = await client.get(f"/api/v1/imports/{import_job_id}", cookies={"drcc_session": session_id})

    assert response.status_code == 200
    assert response.json()["status"] == "PARSE_FAILED"
    assert response.json()["error_data"]["code"] == "IMPORT_FILE_UNREADABLE"
    await redis_client.delete(f"drcc:session:{session_id}")


@pytest.mark.asyncio
async def test_accept_before_parsed_returns_409(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    admin_id = await _create_entra_admin(session)
    event_id = await _create_event_row(session, actor_id=admin_id, name="Too Early Accept Event")
    object_store = ObjectStore(get_settings().azure_storage_connection_string)
    import_job = await create_import_job(
        session,
        actor_id=admin_id,
        dr_event_id=event_id,
        filename="plan.xlsx",
        content=canonical_workbook(),
        object_store=object_store,
    )
    await session.flush()
    app = _build_app(session, redis_client, clock)
    session_id, csrf_token = await _create_session_cookie(session, redis_client, clock, admin_id)

    async with await _client(app) as client:
        response = await client.post(
            f"/api/v1/imports/{import_job.id}/accept",
            cookies={"drcc_session": session_id},
            headers=_idem_headers(csrf_token),
            json={"mapping": []},
        )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INVALID_TRANSITION"
    await redis_client.delete(f"drcc:session:{session_id}")


@pytest.mark.asyncio
async def test_accept_canonical_workbook_creates_draft_tasks_and_dependency(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    admin_id = await _create_entra_admin(session)
    event_id = await _create_event_row(session, actor_id=admin_id, name="Accept Event")
    await _create_team(session, name="Network Team")
    await _create_team(session, name="DBA Team")
    await _create_user(session, display_name="Alex Rivera")
    application_id = await _create_application(session, name="Billing Service")
    await _scope_application_to_event(session, dr_event_id=event_id, application_id=application_id)
    import_job_id = await _upload_and_parse(
        session, actor_id=admin_id, dr_event_id=event_id, filename="plan.xlsx", content=canonical_workbook()
    )
    app = _build_app(session, redis_client, clock)
    session_id, csrf_token = await _create_session_cookie(session, redis_client, clock, admin_id)

    async with await _client(app) as client:
        detail = await client.get(f"/api/v1/imports/{import_job_id}", cookies={"drcc_session": session_id})
        mapping = [
            {"source_header": m["source_header"], "target_field": m["target_field"]}
            for m in detail.json()["proposed_mapping"]
        ]
        response = await client.post(
            f"/api/v1/imports/{import_job_id}/accept",
            cookies={"drcc_session": session_id},
            headers=_idem_headers(csrf_token),
            json={"mapping": mapping},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["created_task_count"] == 2
    assert body["created_dependency_count"] == 1
    assert body["needs_review_count"] == 0
    assert body["import_job"]["status"] == "ACCEPTED"

    tasks = await session.execute(
        text(
            "SELECT title, source_import_id, source_import_row, status, evidence_required "
            "FROM tasks WHERE dr_event_id = :eid ORDER BY source_import_row"
        ),
        {"eid": event_id},
    )
    rows = tasks.all()
    assert len(rows) == 2
    assert rows[0].title == "Repoint DNS"
    assert rows[0].source_import_id == import_job_id
    assert rows[0].source_import_row == "2"
    assert rows[0].status == "NOT_STARTED"
    assert rows[0].evidence_required is True

    dependency_count = await session.execute(
        text("SELECT COUNT(*) AS cnt FROM task_dependencies WHERE dr_event_id = :eid"), {"eid": event_id}
    )
    assert dependency_count.one().cnt == 1
    await redis_client.delete(f"drcc:session:{session_id}")


@pytest.mark.asyncio
async def test_accept_is_idempotent_on_replay_and_rejects_a_second_real_call(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    admin_id = await _create_entra_admin(session)
    event_id = await _create_event_row(session, actor_id=admin_id, name="Accept Replay Event")
    await _create_team(session, name="Network Team")
    await _create_team(session, name="DBA Team")
    application_id = await _create_application(session, name="Billing Service")
    await _scope_application_to_event(session, dr_event_id=event_id, application_id=application_id)
    import_job_id = await _upload_and_parse(
        session, actor_id=admin_id, dr_event_id=event_id, filename="plan.xlsx", content=canonical_workbook()
    )
    app = _build_app(session, redis_client, clock)
    session_id, csrf_token = await _create_session_cookie(session, redis_client, clock, admin_id)
    headers = _idem_headers(csrf_token)

    async with await _client(app) as client:
        detail = await client.get(f"/api/v1/imports/{import_job_id}", cookies={"drcc_session": session_id})
        mapping = [
            {"source_header": m["source_header"], "target_field": m["target_field"]}
            for m in detail.json()["proposed_mapping"]
        ]
        body = {"mapping": mapping}
        first = await client.post(
            f"/api/v1/imports/{import_job_id}/accept",
            cookies={"drcc_session": session_id},
            headers=headers,
            json=body,
        )
        replay = await client.post(
            f"/api/v1/imports/{import_job_id}/accept",
            cookies={"drcc_session": session_id},
            headers=headers,
            json=body,
        )

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json() == first.json()

    task_count = await session.execute(
        text("SELECT COUNT(*) AS cnt FROM tasks WHERE dr_event_id = :eid"), {"eid": event_id}
    )
    assert task_count.one().cnt == 2
    await redis_client.delete(f"drcc:session:{session_id}")


@pytest.mark.asyncio
async def test_messy_workbook_flags_needs_review_and_skips_bad_rows(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    admin_id = await _create_entra_admin(session)
    event_id = await _create_event_row(session, actor_id=admin_id, name="Messy Accept Event")
    await _create_team(session, name="Network Team")
    import_job_id = await _upload_and_parse(
        session, actor_id=admin_id, dr_event_id=event_id, filename="plan.xlsx", content=messy_workbook()
    )
    app = _build_app(session, redis_client, clock)
    session_id, csrf_token = await _create_session_cookie(session, redis_client, clock, admin_id)

    async with await _client(app) as client:
        detail = await client.get(f"/api/v1/imports/{import_job_id}", cookies={"drcc_session": session_id})
        mapping = [
            {"source_header": m["source_header"], "target_field": m["target_field"]}
            for m in detail.json()["proposed_mapping"]
        ]
        response = await client.post(
            f"/api/v1/imports/{import_job_id}/accept",
            cookies={"drcc_session": session_id},
            headers=_idem_headers(csrf_token),
            json={"mapping": mapping},
        )

    assert response.status_code == 200
    body = response.json()
    # Row 2: no title -> skipped+flagged. Row 3: unresolvable Owning Team -> skipped+flagged.
    # Row 4: "Verify Failover" creates fine but references an unresolved predecessor -> flagged.
    assert body["created_task_count"] == 1
    assert body["needs_review_count"] == 3

    reasons = await session.execute(
        text("SELECT target_type, reason FROM needs_review_items WHERE dr_event_id = :eid ORDER BY reason"),
        {"eid": event_id},
    )
    rows = reasons.all()
    assert any(r.target_type == "IMPORT_JOB" and "missing required Task title" in r.reason for r in rows)
    assert any(r.target_type == "IMPORT_JOB" and "Owning Team" in r.reason for r in rows)
    assert any(r.target_type == "TASK" and "unresolved predecessor" in r.reason for r in rows)
    await redis_client.delete(f"drcc:session:{session_id}")

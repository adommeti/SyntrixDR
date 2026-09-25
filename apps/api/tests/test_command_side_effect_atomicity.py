"""VERIFY.md A4.4 and A5.2 for a real Task command, through the production request-session path
(`get_request_session` -> `app.state.session_factory`, one session per request, commit only on
success). Both need *committed* rows seen by two independent connections, so this module builds its
world outside the rollback-per-test `session` fixture and deletes it afterwards.

- A4.4: two concurrent `start` requests with one Idempotency-Key -> exactly one execution and one
  TASK_STARTED audit row; the loser gets 409 IDEMPOTENCY_IN_PROGRESS, a later retry replays.
- A5.2: a failure after the command wrote its audit + outbox rows leaves neither (nor the status).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.clock import Clock, FakeClock
from app.core.errors import AppError, app_error_handler
from app.identity_auth.dependencies import get_clock, get_session_store
from app.identity_auth.session_store import RedisSessionStore
from app.tasks_dependencies import routes as task_routes
from app.tasks_dependencies.routes import router as tasks_router
from tests.factories import World, build_world, headers, login, seed_task

pytestmark = [pytest.mark.api]


class _InjectedFailureError(AppError):
    code = "INJECTED_FAILURE"
    status_code = 500

    def __init__(self) -> None:
        super().__init__("Injected failure after the command's side effects were written.")


class _Committed:
    def __init__(self, world: World, task_id: uuid.UUID, csrf: str, session_id: str) -> None:
        self.world = world
        self.task_id = task_id
        self.csrf = csrf
        self.session_id = session_id


def _app(engine: AsyncEngine, redis_client: Redis, clock: FakeClock) -> FastAPI:
    """Like `factories.build_app`, but without the shared-session override: every request opens and
    closes its own session exactly as production does."""
    app = FastAPI()
    app.add_exception_handler(AppError, app_error_handler)
    app.include_router(tasks_router)
    app.state.session_factory = async_sessionmaker(engine, expire_on_commit=False)
    app.state.session_store = RedisSessionStore(redis_client, idle_minutes=30, absolute_hours=8)

    async def _clock_override() -> Clock:
        return clock

    app.dependency_overrides[get_clock] = _clock_override
    app.dependency_overrides[get_session_store] = lambda: app.state.session_store
    return app


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


async def _delete_world(engine: AsyncEngine, w: World, user_ids: list[uuid.UUID]) -> None:
    params: dict[str, Any] = {
        "eid": w.event_id,
        "uids": user_ids,
        "tids": [w.team_id, w.other_team_id],
        "aids": [w.application_id, w.other_application_id],
    }
    statements = [
        "DELETE FROM idempotency_keys WHERE user_id = ANY(:uids)",
        "DELETE FROM sessions WHERE user_id = ANY(:uids)",
        "DELETE FROM outbox_events WHERE dr_event_id = :eid",
        "DELETE FROM audit_events WHERE dr_event_id = :eid OR actor_user_id = ANY(:uids)",
        "DELETE FROM tasks WHERE dr_event_id = :eid",
        "DELETE FROM dr_event_participants WHERE dr_event_id = :eid",
        "DELETE FROM role_assignments WHERE user_id = ANY(:uids)",
        "DELETE FROM work_streams WHERE dr_event_id = :eid",
        "DELETE FROM dr_applications WHERE dr_event_id = :eid",
        "DELETE FROM dr_events WHERE id = :eid",
        "DELETE FROM application_owners WHERE application_id = ANY(:aids)",
        "DELETE FROM applications WHERE id = ANY(:aids)",
        "DELETE FROM team_memberships WHERE team_id = ANY(:tids)",
        "DELETE FROM teams WHERE id = ANY(:tids)",
        "DELETE FROM users WHERE id = ANY(:uids)",
    ]
    async with engine.begin() as conn:
        for statement in statements:
            await conn.execute(text(statement), params)


@pytest_asyncio.fixture
async def committed(engine: AsyncEngine, redis_client: Redis, clock: FakeClock) -> AsyncIterator[_Committed]:
    async with AsyncSession(engine, expire_on_commit=False) as sess:
        w = await build_world(sess)
        task_id = await seed_task(sess, w)
        session_id, csrf = await login(sess, redis_client, clock, w.executor_id)
        await sess.commit()
    user_ids = [
        w.admin_id,
        w.executor_id,
        w.teammate_id,
        w.outsider_id,
        w.stranger_id,
        w.roleless_member_id,
        w.system_owner_id,
        w.other_app_owner_id,
        w.business_owner_id,
        w.ws_lead_id,
        w.manager_id,
        w.coordinator_id,
    ]
    try:
        yield _Committed(w, task_id, csrf, session_id)
    finally:
        await redis_client.delete(f"drcc:session:{session_id}")
        await _delete_world(engine, w, user_ids)


async def _counts(engine: AsyncEngine, task_id: uuid.UUID) -> tuple[str, int, int, int]:
    async with engine.connect() as conn:
        status = (
            await conn.execute(text("SELECT status FROM tasks WHERE id = :t"), {"t": task_id})
        ).scalar_one()
        started = (
            await conn.execute(
                text("SELECT count(*) FROM audit_events WHERE entity_id = :t AND action = 'TASK_STARTED'"),
                {"t": task_id},
            )
        ).scalar_one()
        outbox = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM outbox_events "
                    "WHERE aggregate_id = :t AND event_type = 'TaskChanged'"
                ),
                {"t": task_id},
            )
        ).scalar_one()
        version = (
            await conn.execute(text("SELECT version FROM tasks WHERE id = :t"), {"t": task_id})
        ).scalar_one()
    return str(status), int(started), int(outbox), int(version)


async def test_concurrent_same_key_start_executes_once_and_audits_once(
    engine: AsyncEngine,
    redis_client: Redis,
    clock: FakeClock,
    committed: _Committed,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The winner holds its transaction open *after* inserting the key and writing its audit and
    outbox rows; only then does the loser start, so its key INSERT blocks on the unique index until
    the winner commits and then fails -> 409, never a second execution."""
    winner_wrote = asyncio.Event()
    real_complete = task_routes.complete

    async def _slow_complete(*args: Any, **kwargs: Any) -> None:
        winner_wrote.set()
        await asyncio.sleep(0.3)
        await real_complete(*args, **kwargs)

    monkeypatch.setattr(task_routes, "complete", _slow_complete)
    app = _app(engine, redis_client, clock)
    key = str(uuid.uuid4())
    url = f"/api/v1/tasks/{committed.task_id}/start"
    body = {"expected_version": 1}

    async with _client(app) as client:
        cookies = {"drcc_session": committed.session_id}
        client.cookies.update(cookies)

        async def _loser():  # noqa: ANN202
            await winner_wrote.wait()
            return await client.post(url, json=body, headers=headers(committed.csrf, key))

        winner, loser = await asyncio.gather(
            client.post(url, json=body, headers=headers(committed.csrf, key)), _loser()
        )
        retry = await client.post(url, json=body, headers=headers(committed.csrf, key))

    assert winner.status_code == 200, winner.text
    assert loser.status_code == 409, loser.text
    assert loser.json()["error"]["code"] == "IDEMPOTENCY_IN_PROGRESS"
    assert retry.status_code == 200
    assert retry.json() == winner.json()
    assert await _counts(engine, committed.task_id) == ("IN_PROGRESS", 1, 1, 2)


async def test_failure_after_side_effects_leaves_no_audit_outbox_or_status_change(
    engine: AsyncEngine,
    redis_client: Redis,
    clock: FakeClock,
    committed: _Committed,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`complete` runs after the transition service has flushed status, audit and outbox; failing
    there must discard all three (the request session closes without committing)."""

    async def _fail(*_args: Any, **_kwargs: Any) -> None:
        raise _InjectedFailureError()

    monkeypatch.setattr(task_routes, "complete", _fail)
    app = _app(engine, redis_client, clock)

    async with _client(app) as client:
        client.cookies.update({"drcc_session": committed.session_id})
        response = await client.post(
            f"/api/v1/tasks/{committed.task_id}/start",
            json={"expected_version": 1},
            headers=headers(committed.csrf),
        )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INJECTED_FAILURE"
    assert await _counts(engine, committed.task_id) == ("NOT_STARTED", 0, 0, 1)

"""D-215: `Idempotency-Key` is required on every command. Walks the BUILD-06 routers' route tables so a
new command route can't ship without it: the body map below must name every non-GET route (a route
missing from it fails `test_every_command_route_is_covered`)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi.routing import APIRoute
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import FakeClock
from app.milestones.routes import router as milestones_router
from app.resources_skills.routes import router as resources_skills_router
from app.tasks_dependencies.routes import router as tasks_router
from app.work_streams.routes import router as work_streams_router
from tests.factories import build_world, http, login

pytestmark = [pytest.mark.api]

_ROUTERS = (tasks_router, work_streams_router, milestones_router, resources_skills_router)
_T = str(uuid.uuid4())

#: (method, path template) -> a body that passes schema validation, so the only thing missing is the key.
BODIES: dict[tuple[str, str], dict[str, Any] | None] = {
    ("PATCH", "/api/v1/tasks/{task_id}"): {"expected_version": 1, "title": "x"},
    ("POST", "/api/v1/tasks/{task_id}/start"): {"expected_version": 1},
    ("POST", "/api/v1/tasks/{task_id}/block"): {"expected_version": 1, "reason": "x"},
    ("POST", "/api/v1/tasks/{task_id}/resume"): {"expected_version": 1},
    ("POST", "/api/v1/tasks/{task_id}/submit-validation"): {"expected_version": 1},
    ("POST", "/api/v1/tasks/{task_id}/validate"): {"expected_version": 1, "approve": True},
    ("POST", "/api/v1/tasks/{task_id}/cancel"): {"expected_version": 1, "reason": "x"},
    ("POST", "/api/v1/task-dependencies"): {
        "predecessor_task_id": _T,
        "successor_task_id": str(uuid.uuid4()),
    },
    ("DELETE", "/api/v1/task-dependencies/{dependency_id}"): None,
    ("POST", "/api/v1/dr-events/{event_id}/tasks"): {"title": "x", "phase": "FAILOVER", "owning_team_id": _T},
    ("POST", "/api/v1/dr-events/{event_id}/work-streams"): {"name": "x"},
    ("POST", "/api/v1/dr-events/{event_id}/milestones"): {"name": "x"},
    ("POST", "/api/v1/milestones/{milestone_id}/confirm"): {"expected_version": 1},
    ("POST", "/api/v1/tasks/{task_id}/assign"): {"assignee_user_id": _T, "expected_version": 1},
    ("POST", "/api/v1/tasks/{task_id}/volunteer"): {"expected_version": 1},
}


def _command_routes() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for router in _ROUTERS:
        for route in router.routes:
            assert isinstance(route, APIRoute)
            for method in route.methods - {"GET", "HEAD"}:
                found.add((method, route.path))
    return found


def test_every_command_route_is_covered() -> None:
    assert _command_routes() == set(BODIES)


@pytest.mark.parametrize(("method", "path"), sorted(BODIES))
async def test_command_without_an_idempotency_key_is_400(
    session: AsyncSession, redis_client: Redis, clock: FakeClock, method: str, path: str
) -> None:
    w = await build_world(session)
    sid, csrf = await login(session, redis_client, clock, w.admin_id)
    url = path.format(
        task_id=uuid.uuid4(), dependency_id=uuid.uuid4(), milestone_id=uuid.uuid4(), event_id=w.event_id
    )

    async with http(session, redis_client, clock) as c:
        r = await c.request(
            method,
            url,
            cookies={"drcc_session": sid},
            headers={"X-CSRF-Token": csrf},
            json=BODIES[(method, path)],
        )

    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    await redis_client.delete(f"drcc:session:{sid}")

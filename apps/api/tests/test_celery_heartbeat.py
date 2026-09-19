from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.core.clock import FakeClock
from app.jobs.heartbeat import heartbeat

pytestmark = pytest.mark.integration


def test_heartbeat_returns_tz_aware_utc_timestamp() -> None:
    fixed = FakeClock(datetime(2026, 3, 1, 12, 0, tzinfo=UTC))

    result = heartbeat.run(clock=fixed)

    assert result == "2026-03-01T12:00:00+00:00"


def test_heartbeat_runs_eagerly_through_celery_app() -> None:
    from app.jobs.celery_app import celery_app

    # `celery_app` is a module-level singleton shared by the whole test session -- leaving
    # `task_always_eager` set to True here would make every later test's `.delay()` call run the
    # task body synchronously (in-process) instead of enqueuing, silently changing behavior for
    # any other test file that calls `.delay()` on a real Celery task (found while adding
    # BUILD-05's own `.delay()` call: eager mode inside an already-running async test event loop
    # makes `asyncio.run()` raise, which Celery's eager path swallows into a failed, unchecked
    # `AsyncResult` instead of surfacing it).
    original_always_eager = celery_app.conf.task_always_eager
    celery_app.conf.task_always_eager = True
    try:
        async_result = heartbeat.delay()
        assert async_result.successful()
        datetime.fromisoformat(async_result.result)
    finally:
        celery_app.conf.task_always_eager = original_always_eager

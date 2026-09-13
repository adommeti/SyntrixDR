from __future__ import annotations

from app.core.clock import Clock, SystemClock
from app.jobs.celery_app import celery_app


@celery_app.task(name="drcc.heartbeat")
def heartbeat(clock: Clock | None = None) -> str:
    """Proves the Celery + Redis wiring (D-241, no second scheduler). Returns an ISO-8601 UTC timestamp."""
    clock = clock or SystemClock()
    return clock.now().isoformat()

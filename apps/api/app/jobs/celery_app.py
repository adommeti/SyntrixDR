from __future__ import annotations

from celery import Celery

from app.core.config import get_settings

settings = get_settings()

celery_app = Celery(
    "drcc",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["app.jobs.heartbeat", "app.plans_import.jobs", "app.milestones.jobs", "app.blockers.jobs"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    # The single beat (D-241): docker-compose runs it inside the worker, Azure as one replica.
    beat_schedule={
        "milestones-missed-sweep": {"task": "drcc.sweep_missed_milestones", "schedule": 60.0},
        "blockers-escalation-sweep": {"task": "drcc.escalate_blockers", "schedule": 30.0},
    },
)

from __future__ import annotations

from celery import Celery

from app.core.config import get_settings

settings = get_settings()

celery_app = Celery(
    "drcc",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["app.jobs.heartbeat"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
)

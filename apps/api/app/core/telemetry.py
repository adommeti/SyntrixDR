from __future__ import annotations

import logging

import structlog

from app.core.config import Settings


def configure_telemetry(settings: Settings) -> None:
    """Structlog JSON logging with correlation_id/actor_id/dr_event_id bound per-request.

    Never binds prompt bodies, tokens, passwords, or cookies (python-api rule).
    """
    logging.basicConfig(level=settings.log_level, format="%(message)s")

    processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]
    if settings.log_format == "json":
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping().get(settings.log_level, logging.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

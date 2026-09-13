from __future__ import annotations

import uuid
from typing import Any

import structlog
from fastapi import Request
from fastapi.responses import JSONResponse

logger = structlog.get_logger(__name__)


class AppError(Exception):
    """Base for all domain/infra errors rendered through the API_CONTRACT envelope."""

    code: str = "INTERNAL_ERROR"
    status_code: int = 500

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class IdempotencyKeyRequiredError(AppError):
    code = "IDEMPOTENCY_KEY_REQUIRED"
    status_code = 400

    def __init__(self) -> None:
        super().__init__("An Idempotency-Key header is required on this command.")


def error_envelope(error: AppError, correlation_id: str) -> dict[str, Any]:
    return {
        "error": {
            "code": error.code,
            "message": error.message,
            "correlation_id": correlation_id,
            "details": error.details,
        }
    }


async def app_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AppError)
    correlation_id = getattr(request.state, "correlation_id", None) or str(uuid.uuid4())
    logger.warning("app_error", code=exc.code, correlation_id=correlation_id)
    return JSONResponse(status_code=exc.status_code, content=error_envelope(exc, correlation_id))


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    correlation_id = getattr(request.state, "correlation_id", None) or str(uuid.uuid4())
    logger.error("unhandled_error", correlation_id=correlation_id, exc_info=exc)
    fallback = AppError("An unexpected error occurred.")
    return JSONResponse(status_code=500, content=error_envelope(fallback, correlation_id))

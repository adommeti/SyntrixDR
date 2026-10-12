"""`POST /api/v1/internal/malware-result` (ADR-024): Microsoft Defender for Storage's verdict, relayed
by Event Grid. Machine-to-machine: no session, CSRF or Idempotency-Key -- the HMAC-SHA256 signature over
the raw body with `MALWARE_WEBHOOK_SECRET` is the authentication, and redelivery is harmless because a
final verdict is never overwritten. An empty secret disables the receiver (503)."""

from __future__ import annotations

import hashlib
import hmac
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict

from app.core.config import get_settings
from app.core.errors import AppError
from app.identity_auth.dependencies import ClockDep, DbSession, ObjectStoreDep
from app.validation_evidence.commands import record_scan_result
from app.validation_evidence.scanning import MalwareScanStatus

router = APIRouter(prefix="/api/v1/internal", tags=["internal"])

SIGNATURE_HEADER = "X-Signature"
MAX_BODY_BYTES = 64 * 1024  # an Event Grid envelope is a few KB


class WebhookDisabledError(AppError):
    code = "WEBHOOK_DISABLED"
    status_code = 503

    def __init__(self) -> None:
        super().__init__("The malware-result receiver is not configured (MALWARE_WEBHOOK_SECRET is empty).")


class WebhookBodyTooLargeError(AppError):
    code = "WEBHOOK_BODY_TOO_LARGE"
    status_code = 413

    def __init__(self) -> None:
        super().__init__("Webhook body exceeds the accepted size.")


class WebhookSignatureInvalidError(AppError):
    code = "WEBHOOK_SIGNATURE_INVALID"
    status_code = 401

    def __init__(self) -> None:
        super().__init__("Missing or invalid webhook signature.")


class MalwareResult(BaseModel):
    model_config = ConfigDict(extra="ignore")  # the Event Grid envelope carries more; BUILD-22 maps it

    blob_uri: str
    verdict: Literal["CLEAN", "INFECTED"]


class MalwareResultResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_id: str
    malware_scan_status: str


def sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


@router.post("/malware-result")
async def post_malware_result(
    request: Request, session: DbSession, clock: ClockDep, object_store: ObjectStoreDep
) -> MalwareResultResponse:
    secret = get_settings().malware_webhook_secret
    if not secret:
        raise WebhookDisabledError()
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise WebhookBodyTooLargeError()
    chunks: list[bytes] = []
    received = 0
    async for chunk in request.stream():  # counted as it arrives: a chunked body can't buffer past the cap
        received += len(chunk)
        if received > MAX_BODY_BYTES:
            raise WebhookBodyTooLargeError()
        chunks.append(chunk)
    body = b"".join(chunks)
    provided = request.headers.get(SIGNATURE_HEADER, "")
    if not provided or not hmac.compare_digest(provided.encode("utf-8"), sign(secret, body).encode("utf-8")):
        raise WebhookSignatureInvalidError()
    result = MalwareResult.model_validate_json(body)
    item = await record_scan_result(
        session,
        blob_uri=result.blob_uri,
        verdict=MalwareScanStatus(result.verdict),
        object_store=object_store,
        clock=clock,
    )
    await session.commit()
    return MalwareResultResponse(
        evidence_id=str(item.id), malware_scan_status=item.malware_scan_status or "PENDING"
    )

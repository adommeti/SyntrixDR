from __future__ import annotations

import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.datastructures import UploadFile

from app.core.config import get_settings
from app.core.errors import IdempotencyKeyRequiredError
from app.core.file_policy import get_file_policy
from app.core.idempotency import IDEMPOTENCY_HEADER, complete, hash_file_payload, require_idempotency_key
from app.core.storage import ObjectStore
from app.dr_events.participants import user_can_see_event
from app.identity_auth.dependencies import (
    ClockDep,
    CurrentSession,
    DbSession,
    ObjectStoreDep,
    RequireCsrfDependency,
)
from app.plans_import.commands import FileTooLargeError
from app.validation_evidence.commands import EvidenceNotFoundError, attach_file, create_evidence
from app.validation_evidence.models import EvidenceItem
from app.validation_evidence.queries import get_evidence, resolve_evidence_target
from app.validation_evidence.scanning import MalwareScannerDep
from app.validation_evidence.schemas import CreateEvidenceRequest, EvidenceResponse

router = APIRouter(prefix="/api/v1", tags=["evidence"])

#: Multipart framing and headers around the file part; a body larger than cap + this is oversize for sure.
_MULTIPART_OVERHEAD = 64 * 1024


def _response(item: EvidenceItem, object_store: ObjectStore) -> EvidenceResponse:
    attached = item.checksum is not None
    download_url = None
    if attached and item.malware_scan_status == "CLEAN" and item.blob_uri:
        download_url = object_store.signed_download_url(
            item.blob_uri, ttl_seconds=get_settings().blob_sas_ttl_seconds
        )
    return EvidenceResponse(
        id=item.id,
        target_type=item.target_type,
        target_id=item.target_id,
        evidence_type=item.evidence_type,  # pyright: ignore[reportArgumentType]
        text_content=item.text_content,
        external_url=item.external_url,
        file_name=item.file_name,
        mime_type=item.mime_type,
        size_bytes=item.size_bytes,
        checksum=item.checksum,
        malware_scan_status=item.malware_scan_status,  # pyright: ignore[reportArgumentType]
        malware_scanned_at=item.malware_scanned_at,
        file_attached=attached,
        download_url=download_url,
        uploaded_by_user_id=item.uploaded_by_user_id,
        created_at=item.created_at,
    )


@router.post(
    "/evidence",
    status_code=201,
    dependencies=[RequireCsrfDependency],
    response_model=None,
    responses={201: {"model": EvidenceResponse}},
)
async def post_create_evidence(
    request: Request,
    body: CreateEvidenceRequest,
    session: DbSession,
    session_data: CurrentSession,
    clock: ClockDep,
    object_store: ObjectStoreDep,
) -> EvidenceResponse | JSONResponse:
    ctx = await require_idempotency_key(request, session, session_data.user_id, clock)
    if ctx.is_replay:
        assert ctx.stored_status is not None
        return JSONResponse(status_code=ctx.stored_status, content=ctx.stored_body)
    item = await create_evidence(
        session,
        actor_id=session_data.user_id,
        target_type=body.target_type,
        target_id=body.target_id,
        evidence_type=body.evidence_type,
        text_content=body.text_content,
        external_url=body.external_url,
        file_name=body.file_name,
        clock=clock,
    )
    response = _response(item, object_store)
    await complete(session, ctx, 201, response.model_dump(mode="json"))
    await session.commit()
    return JSONResponse(status_code=201, content=response.model_dump(mode="json"))


@router.post(
    "/evidence/{evidence_id}/file",
    status_code=200,
    dependencies=[RequireCsrfDependency],
    response_model=None,
    responses={200: {"model": EvidenceResponse}},
)
async def post_attach_file(
    evidence_id: uuid.UUID,
    request: Request,
    session: DbSession,
    session_data: CurrentSession,
    clock: ClockDep,
    object_store: ObjectStoreDep,
    scanner: MalwareScannerDep,
) -> EvidenceResponse | JSONResponse:
    """API_CONTRACT.md:204 -- the attaching command, so the key is required (D-215). The multipart body is
    read by hand and hashed from filename + bytes (the BUILD-05 multipart rule)."""
    if not request.headers.get(IDEMPOTENCY_HEADER):
        raise IdempotencyKeyRequiredError
    # Reject an honestly-declared oversize body before parsing it (D-225 cap). Starlette spools file
    # parts to disk without a limit (`max_part_size` bounds only non-file fields), so a chunked or lying
    # upload is still bounded only by the command's exact check after the spool; the hard body-size cap
    # for that case belongs to the reverse proxy / ASGI layer (BUILD-22 follow-up).
    max_bytes = (await get_file_policy(session)).max_bytes
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > max_bytes + _MULTIPART_OVERHEAD:
        raise FileTooLargeError(max_bytes)
    form = await request.form()
    upload = form.get("file")
    if not isinstance(upload, UploadFile):
        return JSONResponse(status_code=422, content={"detail": "multipart field 'file' is required"})
    content = await upload.read(max_bytes + 1)  # one byte past the cap is enough to prove the overflow
    filename = upload.filename or "upload"

    ctx = await require_idempotency_key(
        request, session, session_data.user_id, clock, request_hash=hash_file_payload(filename, content)
    )
    if ctx.is_replay:
        assert ctx.stored_status is not None
        return JSONResponse(status_code=ctx.stored_status, content=ctx.stored_body)
    item = await attach_file(
        session,
        actor_id=session_data.user_id,
        evidence_id=evidence_id,
        file_name=filename,
        content=content,
        object_store=object_store,
        scanner=scanner,
        clock=clock,
    )
    response = _response(item, object_store)
    await complete(session, ctx, 200, response.model_dump(mode="json"))
    await session.commit()
    return response


@router.get("/evidence/{evidence_id}")
async def get_evidence_route(
    evidence_id: uuid.UUID, session: DbSession, session_data: CurrentSession, object_store: ObjectStoreDep
) -> EvidenceResponse:
    """API_CONTRACT.md:205 -- metadata plus content authorization (a signed URL for CLEAN files)."""
    item = await get_evidence(session, evidence_id)
    if item is None:
        raise EvidenceNotFoundError()
    target = await resolve_evidence_target(session, item.target_type, item.target_id)
    if target is None or not await user_can_see_event(session, session_data.user_id, target.dr_event_id):
        raise EvidenceNotFoundError()
    return _response(item, object_store)

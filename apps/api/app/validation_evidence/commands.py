from __future__ import annotations

import hashlib
import re
import uuid
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import write_audit
from app.core.clock import Clock, SystemClock
from app.core.config import get_settings
from app.core.errors import AppError
from app.core.file_policy import get_file_policy
from app.core.outbox import write_outbox
from app.core.storage import ObjectStore, split_blob_url
from app.dr_events.participants import user_can_see_event
from app.plans_import.commands import FileTooLargeError, UnsupportedFileTypeError
from app.users_teams_org.authorization import AuthorizationRequiredError
from app.validation_evidence.models import EvidenceItem, Validation
from app.validation_evidence.policies import actor_may_attach_evidence
from app.validation_evidence.queries import (
    EVIDENCE_TARGET_TYPES,
    FILE_EVIDENCE,
    EvidenceTarget,
    find_evidence_by_blob_uri,
    get_pending_task_validation,
    lock_evidence,
    resolve_evidence_target,
)
from app.validation_evidence.scanning import MalwareScanner, MalwareScanStatus
from app.validation_evidence.sniff import extension_of, sniffed_mime
from app.validation_evidence.transition_service import ValidationTransitionService

logger = structlog.get_logger(__name__)


async def open_task_validation(
    session: AsyncSession,
    *,
    task_id: uuid.UUID,
    dr_event_id: uuid.UUID,
    verification_note: str | None,
    submitted_by_user_id: uuid.UUID,
    clock: Clock,
) -> Validation:
    """Creates the persisted PENDING Validation record (D-210). Called only from
    `TaskTransitionService.submit_validation` after its own authorization and D-226 guards, so no
    separate check here. Status comes from the column default (PENDING)."""
    now = clock.now()
    validation = Validation(
        target_type="TASK",
        target_id=task_id,
        verification_note=verification_note,
        submitted_at=now,
        created_by_user_id=submitted_by_user_id,
        created_at=now,
        updated_at=now,
    )
    session.add(validation)
    await session.flush()
    await write_audit(
        session,
        actor_user_id=submitted_by_user_id,
        entity_type="VALIDATION",
        entity_id=validation.id,
        action="VALIDATION_OPENED",
        dr_event_id=dr_event_id,
        after={"target_type": "TASK", "target_id": str(task_id), "status": validation.status},
    )
    return validation


async def open_application_validation(
    session: AsyncSession,
    *,
    dr_application_id: uuid.UUID,
    dr_event_id: uuid.UUID,
    phase: str,
    submitted_by_user_id: uuid.UUID,
    clock: Clock,
) -> Validation:
    """The DR Application's PENDING Validation (D-210), opened by
    `DrApplicationTransitionService.submit_validation` after its own authorization and guards.
    `verification_note` records the phase (FAILOVER / FAILBACK) the validation is for."""
    now = clock.now()
    validation = Validation(
        target_type="DR_APPLICATION",
        target_id=dr_application_id,
        verification_note=f"phase:{phase}",
        submitted_at=now,
        created_by_user_id=submitted_by_user_id,
        created_at=now,
        updated_at=now,
    )
    session.add(validation)
    await session.flush()
    await write_audit(
        session,
        actor_user_id=submitted_by_user_id,
        entity_type="VALIDATION",
        entity_id=validation.id,
        action="VALIDATION_OPENED",
        dr_event_id=dr_event_id,
        after={
            "target_type": "DR_APPLICATION",
            "target_id": str(dr_application_id),
            "status": validation.status,
            "phase": phase,
        },
    )
    return validation


async def close_application_validation(
    session: AsyncSession,
    *,
    validation: Validation,
    approve: bool,
    validator_user_id: uuid.UUID,
    note: str | None,
    business_confirmed: bool,
    business_confirmation_note: str | None,
    dr_event_id: uuid.UUID,
    clock: Clock,
) -> Validation:
    """Application-layer entry for `DrApplicationTransitionService.validate` (cross-module rule)."""
    validation.business_confirmed = business_confirmed
    validation.business_confirmation_note = business_confirmation_note
    return await ValidationTransitionService.close_task_validation(
        session,
        validation=validation,
        approve=approve,
        validator_user_id=validator_user_id,
        note=note,
        dr_event_id=dr_event_id,
        clock=clock,
    )


async def close_task_validation(
    session: AsyncSession,
    *,
    validation: Validation,
    approve: bool,
    validator_user_id: uuid.UUID,
    note: str | None,
    dr_event_id: uuid.UUID,
    clock: Clock,
) -> Validation:
    """Application-layer entry point other modules call, so they never import this module's
    transition service directly (cross-module rule: commands.py/queries.py only)."""
    return await ValidationTransitionService.close_task_validation(
        session,
        validation=validation,
        approve=approve,
        validator_user_id=validator_user_id,
        note=note,
        dr_event_id=dr_event_id,
        clock=clock,
    )


async def close_validation_for_cancelled_task(
    session: AsyncSession,
    *,
    task_id: uuid.UUID,
    actor_id: uuid.UUID,
    reason: str,
    dr_event_id: uuid.UUID,
    clock: Clock,
) -> Validation | None:
    """Closes the Task's open submission, if any, as part of `cancel` (same transaction)."""
    validation = await get_pending_task_validation(session, task_id, for_update=True)
    if validation is None:
        return None
    return await ValidationTransitionService.close_for_cancelled_task(
        session,
        validation=validation,
        actor_id=actor_id,
        reason=reason,
        dr_event_id=dr_event_id,
        clock=clock,
    )


# --------------------------------------------------------------------------------------------
# Evidence Items (API_CONTRACT.md:203-205; D-225, D-226, D-245; ADR-014, ADR-024)
# --------------------------------------------------------------------------------------------


class TargetTypeNotAllowedError(AppError):
    code = "TARGET_TYPE_NOT_ALLOWED"
    status_code = 422

    def __init__(self, target_type: str) -> None:
        super().__init__(
            f"Evidence cannot be attached to {target_type}.",
            details={"allowed": sorted(EVIDENCE_TARGET_TYPES)},
        )


class EvidenceTargetNotFoundError(AppError):
    code = "EVIDENCE_TARGET_NOT_FOUND"
    status_code = 404

    def __init__(self) -> None:
        super().__init__("Evidence target not found.")


class EvidenceNotFoundError(AppError):
    code = "EVIDENCE_NOT_FOUND"
    status_code = 404

    def __init__(self) -> None:
        super().__init__("Evidence not found.")


class EvidencePayloadInvalidError(AppError):
    code = "EVIDENCE_PAYLOAD_INVALID"
    status_code = 422

    def __init__(self, message: str) -> None:
        super().__init__(message)


class EvidenceNotAFileError(AppError):
    code = "EVIDENCE_NOT_A_FILE"
    status_code = 409

    def __init__(self, evidence_type: str) -> None:
        super().__init__(f"A {evidence_type} evidence item takes no file.")


class EvidenceFileAlreadyAttachedError(AppError):
    code = "EVIDENCE_FILE_ALREADY_ATTACHED"
    status_code = 409

    def __init__(self) -> None:
        super().__init__("This evidence item already has its file; create a new item for another file.")


class FileContentMismatchError(AppError):
    code = "FILE_CONTENT_MISMATCH"
    status_code = 422

    def __init__(self, extension: str) -> None:
        super().__init__(
            f"The file's content is not a valid .{extension} "
            "(server-side check; the declared Content-Type is ignored)."
        )


_HTTP_SCHEMES = ("http://", "https://")
QUARANTINE_PREFIX = "quarantine/"


_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _safe_file_name(name: str) -> str:
    """Display metadata only (the blob name never contains it): control characters stripped, path
    separators replaced, trimmed, capped at 255 characters."""
    cleaned = _CONTROL.sub("", name).replace("/", "_").replace("\\", "_").strip().strip(".")
    return cleaned[:255]


def _blob_name(dr_event_id: uuid.UUID, evidence_id: uuid.UUID, extension: str) -> str:
    """Opaque, ASCII-only blob names (`<event>/<evidence>/<uuid>.<ext>`): no user text reaches the
    object store path, so URL encoding, length limits and reserved characters cannot bite."""
    return f"{dr_event_id}/{evidence_id}/{uuid.uuid4()}.{extension}"


async def _audit_on_target(
    session: AsyncSession,
    *,
    item: EvidenceItem,
    target: EvidenceTarget,
    actor_id: uuid.UUID | None,
    action: str,
    clock: Clock,
    extra: dict[str, Any] | None = None,
    actor_type: str = "USER",
) -> None:
    """Evidence is not an audit entity (the D-234 superset has no EVIDENCE): actions are audited on
    the target with `after.evidence_id` (the COMMENT_CREATED precedent, BUILD-09.plan.md Risk #4)."""
    after: dict[str, Any] = {
        "evidence_id": str(item.id),
        "evidence_type": item.evidence_type,
        "malware_scan_status": item.malware_scan_status,
        **(extra or {}),
    }
    await write_audit(
        session,
        actor_user_id=actor_id,
        actor_type=actor_type,
        entity_type=target.target_type,
        entity_id=target.target_id,
        action=action,
        dr_event_id=target.dr_event_id,
        after=after,
    )
    if target.target_type == "TASK":
        await write_outbox(
            session,
            aggregate_type="TASK",
            aggregate_id=target.target_id,
            event_type="TaskChanged",  # the Task's acceptable-evidence count changed
            payload={"change": action, **after},
            clock=clock,
            dr_event_id=target.dr_event_id,
        )


async def _resolve_visible(session: AsyncSession, actor_id: uuid.UUID, item: EvidenceItem) -> EvidenceTarget:
    target = await resolve_evidence_target(session, item.target_type, item.target_id)
    if target is None or not await user_can_see_event(session, actor_id, target.dr_event_id):
        raise EvidenceNotFoundError()
    return target


async def create_evidence(
    session: AsyncSession,
    *,
    actor_id: uuid.UUID,
    target_type: str,
    target_id: uuid.UUID,
    evidence_type: str,
    text_content: str | None = None,
    external_url: str | None = None,
    file_name: str | None = None,
    clock: Clock | None = None,
) -> EvidenceItem:
    """`POST /evidence`: a TEXT / LOG / EXTERNAL_LINK item, or the FILE / SCREENSHOT *handshake* -- the
    row exists (PENDING, planned `blob_uri`, no bytes) so that `POST /evidence/{id}/file` can attach."""
    clock = clock or SystemClock()
    now = clock.now()
    if target_type not in EVIDENCE_TARGET_TYPES:
        raise TargetTypeNotAllowedError(target_type)
    target = await resolve_evidence_target(session, target_type, target_id)
    if target is None or not await user_can_see_event(session, actor_id, target.dr_event_id):
        raise EvidenceTargetNotFoundError()
    if not await actor_may_attach_evidence(session, actor_id, target, at=now):
        raise AuthorizationRequiredError()

    item = EvidenceItem(
        id=uuid.uuid4(),  # known up front: the file handshake's planned blob_uri embeds it
        target_type=target_type,
        target_id=target_id,
        evidence_type=evidence_type,
        uploaded_by_user_id=actor_id,
        created_at=now,
    )
    if evidence_type in ("TEXT", "LOG"):
        clean = (text_content or "").strip()
        if not clean:
            raise EvidencePayloadInvalidError("TEXT and LOG evidence need text_content.")
        item.text_content = clean
    elif evidence_type == "EXTERNAL_LINK":
        url = (external_url or "").strip()
        if not url.startswith(_HTTP_SCHEMES) or " " in url:
            raise EvidencePayloadInvalidError("EXTERNAL_LINK evidence needs an http(s) external_url.")
        item.external_url = url
    else:  # FILE / SCREENSHOT handshake
        name = _safe_file_name(file_name or "")
        if not name:
            raise EvidencePayloadInvalidError("FILE and SCREENSHOT evidence need a file_name.")
        policy = await get_file_policy(session)
        extension = extension_of(name)
        if extension not in policy.allowed_extensions:
            raise UnsupportedFileTypeError(extension)
        item.file_name = name
        item.malware_scan_status = MalwareScanStatus.PENDING.value
        container = get_settings().blob_container_evidence
        item.blob_uri = f"planned://{container}/{_blob_name(target.dr_event_id, item.id, extension)}"
    session.add(item)
    await session.flush()
    await _audit_on_target(
        session, item=item, target=target, actor_id=actor_id, action="EVIDENCE_CREATED", clock=clock
    )
    return item


async def attach_file(
    session: AsyncSession,
    *,
    actor_id: uuid.UUID,
    evidence_id: uuid.UUID,
    file_name: str,
    content: bytes,
    object_store: ObjectStore,
    scanner: MalwareScanner,
    clock: Clock | None = None,
) -> EvidenceItem:
    """`POST /evidence/{id}/file`: extension allowlist → size cap → server-side content sniff and MIME
    allowlist (the client's Content-Type is ignored) → upload → sha256 → scan. INFECTED goes to
    quarantine at once. Each guard is from `file_policy` (D-225) and D-245."""
    clock = clock or SystemClock()
    now = clock.now()
    item = await lock_evidence(session, evidence_id)
    if item is None:
        raise EvidenceNotFoundError()
    target = await _resolve_visible(session, actor_id, item)
    if not await actor_may_attach_evidence(session, actor_id, target, at=now):
        raise AuthorizationRequiredError()
    if item.evidence_type not in FILE_EVIDENCE:
        raise EvidenceNotAFileError(item.evidence_type)
    if item.checksum is not None:
        raise EvidenceFileAlreadyAttachedError()

    policy = await get_file_policy(session)
    name = _safe_file_name(item.file_name or file_name)
    extension = extension_of(name)
    if extension not in policy.allowed_extensions:
        raise UnsupportedFileTypeError(extension)
    if len(content) > policy.max_bytes:
        raise FileTooLargeError(policy.max_bytes)
    mime = sniffed_mime(content, extension)
    if mime is None or mime not in policy.allowed_mime_types:
        raise FileContentMismatchError(extension)

    blob_name = _blob_name(target.dr_event_id, item.id, extension)
    uri = await object_store.upload(
        get_settings().blob_container_evidence, blob_name, content, content_type=mime
    )
    try:
        item.blob_uri = uri
        item.file_name = name
        item.mime_type = mime
        item.size_bytes = len(content)
        item.checksum = hashlib.sha256(content).hexdigest()
        await session.flush()
        attached = {
            "size_bytes": item.size_bytes,
            "mime_type": mime,
            "checksum": item.checksum,
            "scanner": scanner.name,
        }
        await _audit_on_target(
            session,
            item=item,
            target=target,
            actor_id=actor_id,
            action="EVIDENCE_FILE_ATTACHED",
            clock=clock,
            extra=attached,
        )
        verdict = await scanner.scan(content, file_name=name)
        if verdict is MalwareScanStatus.PENDING:
            item.malware_scan_status = MalwareScanStatus.PENDING.value  # Defender reports via the webhook
            return item
        await _apply_verdict(
            session, item=item, target=target, verdict=verdict, object_store=object_store, actor_id=actor_id,
            actor_type="USER", clock=clock,
        )  # fmt: skip
        return item
    except Exception:
        # The DB transaction rolls back; the blob must not outlive it (best-effort compensation).
        try:
            await object_store.delete(uri)
        except Exception as cleanup_error:  # noqa: BLE001 -- report, never mask the original failure
            logger.warning("evidence_blob_orphaned", blob_uri=uri, error=str(cleanup_error))
        raise


async def _apply_verdict(
    session: AsyncSession,
    *,
    item: EvidenceItem,
    target: EvidenceTarget,
    verdict: MalwareScanStatus,
    object_store: ObjectStore,
    actor_id: uuid.UUID | None,
    actor_type: str,
    clock: Clock,
) -> None:
    # The verdict is recorded before any blob I/O: even if the quarantine move fails, an INFECTED item
    # never counts (CLEAN-only counter) and never gets a download URL (CLEAN-only SAS).
    item.malware_scan_status = verdict.value
    item.malware_scanned_at = clock.now()
    await session.flush()
    await _audit_on_target(
        session, item=item, target=target, actor_id=actor_id, action="EVIDENCE_SCANNED", clock=clock,
        actor_type=actor_type,
    )  # fmt: skip
    if verdict is MalwareScanStatus.INFECTED and item.blob_uri and not _is_quarantined(item.blob_uri):
        _, blob_name = split_blob_url(item.blob_uri)
        item.blob_uri = await object_store.move(item.blob_uri, QUARANTINE_PREFIX + blob_name)
        await session.flush()
        await _audit_on_target(
            session, item=item, target=target, actor_id=actor_id, action="EVIDENCE_QUARANTINED", clock=clock,
            actor_type=actor_type, extra={"blob_uri": item.blob_uri},
        )  # fmt: skip


def _is_quarantined(uri: str) -> bool:
    _, blob_name = split_blob_url(uri)
    return blob_name.startswith(QUARANTINE_PREFIX)


async def record_scan_result(
    session: AsyncSession,
    *,
    blob_uri: str,
    verdict: MalwareScanStatus,
    object_store: ObjectStore,
    clock: Clock | None = None,
) -> EvidenceItem:
    """The Defender webhook's half (ADR-024): PENDING → CLEAN | INFECTED by `blob_uri`, once. A verdict
    on an item that already has a final status is a no-op (Event Grid delivers at least once)."""
    clock = clock or SystemClock()
    found = await find_evidence_by_blob_uri(session, blob_uri)
    if found is None:
        raise EvidenceNotFoundError()
    item = await lock_evidence(session, found.id)
    assert item is not None
    if item.malware_scan_status != MalwareScanStatus.PENDING.value:
        return item
    target = await resolve_evidence_target(session, item.target_type, item.target_id)
    if target is None:
        raise EvidenceNotFoundError()
    await _apply_verdict(
        session, item=item, target=target, verdict=verdict, object_store=object_store, actor_id=None,
        actor_type="SYSTEM", clock=clock,
    )  # fmt: skip
    return item

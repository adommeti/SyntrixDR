from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import write_audit
from app.core.clock import Clock, SystemClock
from app.core.errors import AppError
from app.dr_events.participants import user_can_see_event
from app.plans_import.commands import AcceptSummary, process_accepted_import
from app.plans_import.models import ImportJob
from app.users_teams_org.authorization import AuthorizationService, Capability


class ImportJobNotFoundError(AppError):
    code = "IMPORT_JOB_NOT_FOUND"
    status_code = 404

    def __init__(self) -> None:
        super().__init__("Import job not found.")


class InvalidImportJobTransitionError(AppError):
    code = "INVALID_TRANSITION"
    status_code = 409

    def __init__(self, current: str, target: str) -> None:
        super().__init__(f"Cannot transition import job from {current} to {target}.")


#: `import_jobs.status` is plain TEXT (no DB enum, schema_v1.sql:734-744) -- this value set and
#: legality table are defined here, the only file allowed to write `import_job.status =`
#: (`guard-forbidden-stack.sh`).
_LEGAL_TRANSITIONS: dict[str, set[str]] = {
    "PENDING": {"PARSED", "PARSE_FAILED"},
    "PARSED": {"ACCEPTED"},
    "PARSE_FAILED": set(),
    "ACCEPTED": set(),
}


async def _load_for_update(session: AsyncSession, import_job_id: uuid.UUID) -> ImportJob:
    import_job = await session.get(ImportJob, import_job_id, with_for_update=True)
    if import_job is None:
        raise ImportJobNotFoundError()
    return import_job


def _assert_legal(current: str, target: str) -> None:
    if target not in _LEGAL_TRANSITIONS.get(current, set()):
        raise InvalidImportJobTransitionError(current, target)


class ImportJobTransitionService:
    """`mark_parsed`/`mark_parse_failed`/`accept` on the import job machine
    (`PENDING -> PARSED | PARSE_FAILED -> ACCEPTED`, BUILD-05.plan.md). No outbox event: D-234's
    catalog has no import-job event type, so (matching BUILD-04's precedent for DR_APPLICATION
    changes) this is audit-only, not silently faked into an unrelated catalog entry."""

    @staticmethod
    async def mark_parsed(
        session: AsyncSession,
        *,
        import_job_id: uuid.UUID,
        mapping_data: dict[str, Any],
        clock: Clock | None = None,
    ) -> ImportJob:
        """Called only from `jobs.py`'s Celery task -- no separate authorization check (a
        system-triggered side effect of an already-authorized `create_import_job` call, mirrors
        `capture_baseline_snapshot`'s precedent)."""
        clock = clock or SystemClock()
        import_job = await _load_for_update(session, import_job_id)
        _assert_legal(import_job.status, "PARSED")

        before = {"status": import_job.status}
        import_job.status = "PARSED"
        import_job.mapping_data = mapping_data
        await session.flush()
        await write_audit(
            session,
            actor_user_id=None,
            entity_type="IMPORT_JOB",
            entity_id=import_job.id,
            action="IMPORT_JOB_PARSED",
            dr_event_id=import_job.dr_event_id,
            before=before,
            after={"status": import_job.status, "row_count": len(mapping_data.get("rows", []))},
        )
        return import_job

    @staticmethod
    async def mark_parse_failed(
        session: AsyncSession,
        *,
        import_job_id: uuid.UUID,
        error_data: dict[str, Any],
        clock: Clock | None = None,
    ) -> ImportJob:
        clock = clock or SystemClock()
        import_job = await _load_for_update(session, import_job_id)
        _assert_legal(import_job.status, "PARSE_FAILED")

        before = {"status": import_job.status}
        import_job.status = "PARSE_FAILED"
        import_job.error_data = error_data
        import_job.completed_at = clock.now()
        await session.flush()
        await write_audit(
            session,
            actor_user_id=None,
            entity_type="IMPORT_JOB",
            entity_id=import_job.id,
            action="IMPORT_JOB_PARSE_FAILED",
            dr_event_id=import_job.dr_event_id,
            before=before,
            after={"status": import_job.status, "error": error_data},
        )
        return import_job

    @staticmethod
    async def accept(
        session: AsyncSession,
        *,
        actor_id: uuid.UUID,
        import_job_id: uuid.UUID,
        confirmed_mapping: dict[str, str | None],
        clock: Clock | None = None,
    ) -> tuple[ImportJob, AcceptSummary]:
        clock = clock or SystemClock()
        # Authorization before existence (invariant #1, same reasoning as
        # `dr_events/transition_service.py`'s reordering): check the capability first so an
        # unauthorized actor gets the same 403 whether the job exists or not.
        await AuthorizationService.require(session, actor_id, Capability.MANAGE_IMPORTS)
        import_job = await _load_for_update(session, import_job_id)
        if not await user_can_see_event(session, actor_id, import_job.dr_event_id):
            raise ImportJobNotFoundError()
        _assert_legal(import_job.status, "ACCEPTED")

        summary = await process_accepted_import(
            session,
            import_job=import_job,
            confirmed_mapping=confirmed_mapping,
            actor_id=actor_id,
            clock=clock,
        )

        before = {"status": import_job.status}
        import_job.status = "ACCEPTED"
        import_job.completed_at = clock.now()
        await session.flush()
        await write_audit(
            session,
            actor_user_id=actor_id,
            entity_type="IMPORT_JOB",
            entity_id=import_job.id,
            action="IMPORT_JOB_ACCEPTED",
            dr_event_id=import_job.dr_event_id,
            before=before,
            after={
                "status": import_job.status,
                "created_task_count": summary.created_task_count,
                "created_dependency_count": summary.created_dependency_count,
                "needs_review_count": summary.needs_review_count,
            },
        )
        return import_job, summary

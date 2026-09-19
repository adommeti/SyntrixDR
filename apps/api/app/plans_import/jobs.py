from __future__ import annotations

import asyncio
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import make_engine, make_session_factory
from app.core.storage import ObjectStore
from app.jobs.celery_app import celery_app
from app.plans_import.mapping import propose_mapping
from app.plans_import.models import ImportJob
from app.plans_import.transition_service import ImportJobTransitionService
from app.plans_import.xlsx_parser import InvalidWorkbookError, parse_workbook


async def parse_import_job(
    session: AsyncSession, *, object_store: ObjectStore, import_job_id: uuid.UUID
) -> None:
    """The actual parse logic, taking `session`/`object_store` as parameters rather than
    constructing them -- testable directly against a test's own `session` fixture, no separate
    engine/connection involved (a genuinely separate connection can't see rows the test's
    transaction-scoped `session` hasn't truly committed). `_parse_import_job_async` is the only
    caller that needs its own engine (the real Celery worker, outside any request/test
    transaction). No retry/idempotency dance needed on failure: `mark_parse_failed` is itself
    idempotent-safe to call once (`_LEGAL_TRANSITIONS` blocks a second write once terminal), and
    re-uploading is the recovery path for a bad file, not a job retry."""
    import_job = await session.get(ImportJob, import_job_id)
    if import_job is None:
        return

    try:
        data = await object_store.download(import_job.source_file_uri)
        headers, rows = parse_workbook(data)
        proposed_mapping = propose_mapping(headers)
        mapping_data = {
            "headers": headers,
            "proposed_mapping": [
                {"source_header": m.source_header, "target_field": m.target_field, "confidence": m.confidence}
                for m in proposed_mapping
            ],
            "rows": [{"row_number": r.row_number, "values": r.values} for r in rows],
        }
        await ImportJobTransitionService.mark_parsed(
            session, import_job_id=import_job_id, mapping_data=mapping_data
        )
    except InvalidWorkbookError as exc:
        await ImportJobTransitionService.mark_parse_failed(
            session, import_job_id=import_job_id, error_data={"code": exc.code, "message": exc.message}
        )


async def _parse_import_job_async(import_job_id: uuid.UUID) -> None:
    """Runs outside the request lifecycle -- owns its own engine/session (not the FastAPI
    request-scoped one, and not a test's `session` fixture)."""
    settings = get_settings()
    engine = make_engine(settings.database_url)
    session_factory = make_session_factory(engine)
    object_store = ObjectStore(settings.azure_storage_connection_string)

    async with session_factory() as session:
        await parse_import_job(session, object_store=object_store, import_job_id=import_job_id)
        await session.commit()

    await engine.dispose()


@celery_app.task(name="drcc.parse_import_excel")  # pyright: ignore[reportUntypedFunctionDecorator, reportUnknownMemberType]
def parse_import_excel(import_job_id: str) -> None:
    asyncio.run(_parse_import_job_async(uuid.UUID(import_job_id)))

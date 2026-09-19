from __future__ import annotations

import asyncio
import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai_orchestration.provider import AIProvider, NullAIProvider
from app.core.config import get_settings
from app.core.database import make_engine, make_session_factory
from app.core.storage import ObjectStore
from app.jobs.celery_app import celery_app
from app.plans_import.ai_mapper import AIImportMapper, HeuristicImportMapper
from app.plans_import.mapping import ColumnMapping
from app.plans_import.models import ImportJob, NeedsReviewItem
from app.plans_import.transition_service import ImportJobTransitionService
from app.plans_import.xlsx_parser import InvalidWorkbookError, parse_workbook

logger = logging.getLogger(__name__)


async def _record_ai_mapping_suggestions(
    session: AsyncSession,
    *,
    import_job: ImportJob,
    headers: list[str],
    heuristic_mapping: list[ColumnMapping],
    ai_provider: AIProvider,
) -> None:
    """Runs the AI mapper *after* the heuristic proposal is already computed, and only ever
    compares against it -- never touches `mapping_data`. Every header where the AI's
    `target_field` is non-null and disagrees with the heuristic's becomes one
    `NeedsReviewItem(source="AI")` for a human to weigh (D-221: AI suggestions are advisory,
    "never auto-accepted"). AI failures are already swallowed inside `AIImportMapper` itself
    (returns `[]`), so this function never raises either."""
    heuristic_by_header = {m.source_header: m.target_field for m in heuristic_mapping}
    ai_suggestions = await AIImportMapper(ai_provider).propose_mapping(headers)

    for suggestion in ai_suggestions:
        if suggestion.target_field is None:
            continue
        heuristic_target = heuristic_by_header.get(suggestion.source_header)
        if suggestion.target_field == heuristic_target:
            continue
        session.add(
            NeedsReviewItem(
                dr_event_id=import_job.dr_event_id,
                target_type="IMPORT_JOB",
                target_id=import_job.id,
                reason=(
                    f"AI suggests {suggestion.source_header!r} -> {suggestion.target_field!r} "
                    f"(heuristic: {heuristic_target!r})"
                ),
                source="AI",
                confidence=suggestion.confidence,
            )
        )
    if ai_suggestions:
        await session.flush()


async def parse_import_job(
    session: AsyncSession,
    *,
    object_store: ObjectStore,
    import_job_id: uuid.UUID,
    ai_provider: AIProvider | None = None,
) -> None:
    """The actual parse logic, taking `session`/`object_store` as parameters rather than
    constructing them -- testable directly against a test's own `session` fixture, no separate
    engine/connection involved (a genuinely separate connection can't see rows the test's
    transaction-scoped `session` hasn't truly committed). `_parse_import_job_async` is the only
    caller that needs its own engine (the real Celery worker, outside any request/test
    transaction). No retry/idempotency dance needed on failure: `mark_parse_failed` is itself
    idempotent-safe to call once (`_LEGAL_TRANSITIONS` blocks a second write once terminal), and
    re-uploading is the recovery path for a bad file, not a job retry.

    `ai_provider=None` (the default) skips the AI path entirely -- byte-identical behavior to
    before BUILD-05 session b. When provided, AI enrichment runs strictly additively after the
    heuristic mapping is already final; any AI-path exception is caught here too (belt-and-braces
    on top of `AIImportMapper`'s own handling) so a parse job always succeeds on the heuristic
    path alone even if AI enrichment fails outright."""
    import_job = await session.get(ImportJob, import_job_id)
    if import_job is None:
        return

    try:
        data = await object_store.download(import_job.source_file_uri)
        headers, rows = parse_workbook(data)
        proposed_mapping = await HeuristicImportMapper().propose_mapping(headers)
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

        if ai_provider is not None:
            try:
                await _record_ai_mapping_suggestions(
                    session,
                    import_job=import_job,
                    headers=headers,
                    heuristic_mapping=proposed_mapping,
                    ai_provider=ai_provider,
                )
            except Exception:
                logger.warning("AI mapping enrichment failed; heuristic mapping stands", exc_info=True)
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
    # `NullAIProvider` is the only implementation shipped this increment (no real Synthetic/
    # Anthropic wiring yet, BUILD-16's scope) -- `AI_ENABLED=true` runs the AI path for real using
    # it, not a mock; it just always returns "no suggestions" today.
    ai_provider = NullAIProvider() if settings.ai_enabled else None

    async with session_factory() as session:
        await parse_import_job(
            session, object_store=object_store, import_job_id=import_job_id, ai_provider=ai_provider
        )
        await session.commit()

    await engine.dispose()


@celery_app.task(name="drcc.parse_import_excel")  # pyright: ignore[reportUntypedFunctionDecorator, reportUnknownMemberType]
def parse_import_excel(import_job_id: str) -> None:
    asyncio.run(_parse_import_job_async(uuid.UUID(import_job_id)))

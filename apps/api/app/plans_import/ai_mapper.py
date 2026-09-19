from __future__ import annotations

import json
import logging
from typing import Any, Protocol, cast

from app.ai_orchestration.provider import AIProvider
from app.plans_import.mapping import CANONICAL_FIELDS, ColumnMapping, propose_mapping

logger = logging.getLogger(__name__)


class ImportMapper(Protocol):
    """D-221: "allows AI or manual mapping." Two adapters: `HeuristicImportMapper` (default,
    always runs) and `AIImportMapper` (behind `AI_ENABLED`, advisory-only -- see
    `jobs.py::parse_import_job` for how its output is used, never as the operative mapping)."""

    async def propose_mapping(self, headers: list[str]) -> list[ColumnMapping]: ...


class HeuristicImportMapper:
    """Thin async wrapper around `mapping.propose_mapping` (unchanged sync heuristic) -- this is
    what populates `mapping_data.proposed_mapping`, regardless of `AI_ENABLED`."""

    async def propose_mapping(self, headers: list[str]) -> list[ColumnMapping]:
        return propose_mapping(headers)


def _build_prompt(headers: list[str]) -> str:
    fields = ", ".join(CANONICAL_FIELDS.keys())
    return (
        "Map each of the following spreadsheet column headers to one of these canonical fields "
        f"({fields}), or null if none fit. Respond with a JSON array of objects, each with "
        '"source_header", "target_field" and "confidence" (0.0-1.0) keys, and nothing else.\n\n'
        f"Headers: {json.dumps(headers)}"
    )


class AIImportMapper:
    """Never called to produce the operative mapping -- `jobs.py::parse_import_job` only ever
    compares its output against `HeuristicImportMapper`'s own result to decide what to flag via
    `needs_review_items` (D-221: AI suggestions are advisory, "never auto-accepted"). A malformed
    or failed provider response yields no suggestions, never raises -- AI enrichment must never
    break the core (heuristic-only) parse path."""

    def __init__(self, provider: AIProvider) -> None:
        self._provider = provider

    async def propose_mapping(self, headers: list[str]) -> list[ColumnMapping]:
        try:
            raw_response = await self._provider.complete(_build_prompt(headers))
            parsed = json.loads(raw_response)
        except Exception:
            logger.warning("AI import mapper: provider call or JSON parse failed", exc_info=True)
            return []

        if not isinstance(parsed, list):
            return []
        raw_entries = cast("list[Any]", parsed)

        suggestions: list[ColumnMapping] = []
        for entry in raw_entries:
            if not isinstance(entry, dict):
                continue
            raw_entry = cast("dict[str, Any]", entry)
            source_header = raw_entry.get("source_header")
            target_field = raw_entry.get("target_field")
            confidence = raw_entry.get("confidence")
            if not isinstance(source_header, str) or not isinstance(confidence, int | float):
                continue
            if target_field is not None and not isinstance(target_field, str):
                continue
            suggestions.append(
                ColumnMapping(
                    source_header=source_header, target_field=target_field, confidence=float(confidence)
                )
            )
        return suggestions

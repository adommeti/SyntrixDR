from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

#: D-221 fixture columns + a couple of natural synonyms. Deterministic, non-AI matching only --
#: session b (BUILD-05's suggested split) adds an optional AI enrichment layer on top of this,
#: never in place of it (manual mapping must always work with AI disabled).
CANONICAL_FIELDS: dict[str, tuple[str, ...]] = {
    "phase": ("phase",),
    "work_stream": ("work stream", "workstream", "stream"),
    "application": ("application", "app", "system"),
    "tier": ("tier",),
    "title": ("task", "task name", "task title"),
    "subtask": ("subtask / procedure", "subtask", "procedure", "step"),
    "owning_team": ("owning team", "team", "owner team"),
    "assignee": ("assignee", "assigned to"),
    "expected_duration": ("expected duration", "duration", "duration (min)"),
    "predecessor": ("predecessor / dependency", "predecessor", "dependency", "dependencies"),
    "failover_failback": ("failover / failback", "failover/failback"),
    "evidence_required": ("evidence required",),
    "notes": ("notes", "note", "description"),
}

#: Below this, a header is left unmapped rather than guessed -- the review step (GET /imports/{id})
#: is the safety net for anything the heuristic can't confidently place.
_CONFIDENCE_THRESHOLD = 0.6


@dataclass(frozen=True)
class ColumnMapping:
    source_header: str
    target_field: str | None
    confidence: float


def _normalize(header: str) -> str:
    return re.sub(r"\s+", " ", header.strip().lower())


def _score(normalized_header: str, synonym: str) -> float:
    if normalized_header == synonym:
        return 1.0
    return SequenceMatcher(None, normalized_header, synonym).ratio()


def propose_mapping(headers: list[str]) -> list[ColumnMapping]:
    """Proposes a target canonical field per header with a confidence score. Never guesses below
    `_CONFIDENCE_THRESHOLD` -- an unmapped column stays unmapped, not silently wrong."""
    mappings: list[ColumnMapping] = []
    for header in headers:
        normalized = _normalize(header)
        best_field: str | None = None
        best_score = 0.0
        for field, synonyms in CANONICAL_FIELDS.items():
            for synonym in synonyms:
                score = _score(normalized, synonym)
                if score > best_score:
                    best_score = score
                    best_field = field
        if best_score >= _CONFIDENCE_THRESHOLD:
            mappings.append(ColumnMapping(header, best_field, round(best_score, 4)))
        else:
            mappings.append(ColumnMapping(header, None, 0.0))
    return mappings

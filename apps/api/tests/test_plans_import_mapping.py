"""Unit tests for BUILD-05's heuristic (non-AI) column->field mapper. Pure functions, no DB."""

from __future__ import annotations

from app.plans_import.mapping import CANONICAL_FIELDS, propose_mapping


def test_exact_canonical_headers_map_with_full_confidence() -> None:
    headers = [
        "Phase",
        "Work Stream",
        "Application",
        "Tier",
        "Task",
        "Subtask / Procedure",
        "Owning Team",
        "Assignee",
        "Expected Duration",
        "Predecessor / Dependency",
        "Failover / Failback",
        "Evidence Required",
        "Notes",
    ]

    mappings = propose_mapping(headers)

    by_header = {m.source_header: m for m in mappings}
    assert by_header["Phase"].target_field == "phase"
    assert by_header["Phase"].confidence == 1.0
    assert by_header["Task"].target_field == "title"
    assert by_header["Subtask / Procedure"].target_field == "subtask"
    assert by_header["Owning Team"].target_field == "owning_team"
    assert by_header["Predecessor / Dependency"].target_field == "predecessor"
    assert by_header["Notes"].target_field == "notes"
    assert all(m.confidence == 1.0 for m in mappings)


def test_renamed_and_reordered_headers_still_map() -> None:
    headers = ["Notes", "Task Title", "Team", "Duration (min)"]

    mappings = propose_mapping(headers)

    by_header = {m.source_header: m for m in mappings}
    assert by_header["Notes"].target_field == "notes"
    assert by_header["Task Title"].target_field == "title"
    assert by_header["Team"].target_field == "owning_team"
    assert by_header["Duration (min)"].target_field == "expected_duration"


def test_header_with_zero_synonym_overlap_is_left_unmapped() -> None:
    mappings = propose_mapping(["Completely Unrelated Gibberish Column"])

    assert len(mappings) == 1
    assert mappings[0].target_field is None
    assert mappings[0].confidence == 0.0


def test_every_canonical_field_has_at_least_one_synonym() -> None:
    assert len(CANONICAL_FIELDS) >= 10
    for field, synonyms in CANONICAL_FIELDS.items():
        assert synonyms, f"{field} has no synonyms"

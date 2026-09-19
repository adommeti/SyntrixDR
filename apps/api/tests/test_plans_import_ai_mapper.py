"""Unit tests for `plans_import/ai_mapper.py` (BUILD-05 session b). Pure, no DB/network -- uses a
tiny in-test fake `AIProvider`, not the real `NullAIProvider`."""

from __future__ import annotations

import pytest

from app.plans_import.ai_mapper import AIImportMapper, HeuristicImportMapper


class _FakeProvider:
    def __init__(self, response: str) -> None:
        self._response = response

    async def complete(self, prompt: str) -> str:
        _ = prompt
        return self._response


class _RaisingProvider:
    async def complete(self, prompt: str) -> str:
        _ = prompt
        raise RuntimeError("provider unavailable")


@pytest.mark.asyncio
async def test_heuristic_import_mapper_delegates_to_the_existing_heuristic() -> None:
    mapper = HeuristicImportMapper()

    result = await mapper.propose_mapping(["Task", "Owning Team"])

    by_header = {m.source_header: m for m in result}
    assert by_header["Task"].target_field == "title"
    assert by_header["Owning Team"].target_field == "owning_team"


@pytest.mark.asyncio
async def test_ai_import_mapper_parses_a_well_formed_json_response() -> None:
    provider = _FakeProvider(
        '[{"source_header": "Task", "target_field": "title", "confidence": 0.95}, '
        '{"source_header": "Notes Column", "target_field": "notes", "confidence": 0.7}]'
    )
    mapper = AIImportMapper(provider)

    result = await mapper.propose_mapping(["Task", "Notes Column"])

    by_header = {m.source_header: m for m in result}
    assert by_header["Task"].target_field == "title"
    assert by_header["Task"].confidence == 0.95
    assert by_header["Notes Column"].target_field == "notes"


@pytest.mark.asyncio
async def test_ai_import_mapper_returns_empty_list_for_malformed_json() -> None:
    mapper = AIImportMapper(_FakeProvider("not json at all"))

    result = await mapper.propose_mapping(["Task"])

    assert result == []


@pytest.mark.asyncio
async def test_ai_import_mapper_returns_empty_list_for_non_list_json() -> None:
    mapper = AIImportMapper(_FakeProvider('{"not": "a list"}'))

    result = await mapper.propose_mapping(["Task"])

    assert result == []


@pytest.mark.asyncio
async def test_ai_import_mapper_returns_empty_list_when_provider_raises() -> None:
    mapper = AIImportMapper(_RaisingProvider())

    result = await mapper.propose_mapping(["Task"])

    assert result == []


@pytest.mark.asyncio
async def test_ai_import_mapper_ignores_malformed_entries_within_an_otherwise_valid_list() -> None:
    provider = _FakeProvider(
        '[{"source_header": "Task", "target_field": "title", "confidence": 0.9}, '
        '{"missing": "required keys"}, '
        '"not even an object"]'
    )
    mapper = AIImportMapper(provider)

    result = await mapper.propose_mapping(["Task"])

    assert len(result) == 1
    assert result[0].source_header == "Task"

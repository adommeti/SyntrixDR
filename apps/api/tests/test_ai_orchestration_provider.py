"""Unit tests for `ai_orchestration/provider.py` (BUILD-05 session b). Pure, no DB/network."""

from __future__ import annotations

import pytest

from app.ai_orchestration.provider import NullAIProvider


@pytest.mark.asyncio
async def test_null_provider_returns_its_fixed_stub_response() -> None:
    """No real Synthetic/Anthropic wiring yet (BUILD-16's scope) -- `NullAIProvider` is what
    `AI_ENABLED=true` actually uses today, so this must be a real, deterministic implementation
    of the port, not a test-only fixture."""
    provider = NullAIProvider()

    result = await provider.complete("propose a mapping for these headers: Task, Owning Team")

    assert isinstance(result, str)
    assert result == NullAIProvider.STUB_RESPONSE

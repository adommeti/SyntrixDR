from __future__ import annotations

from typing import Protocol


class AIProvider(Protocol):
    """Generic prompt-in/text-out port (python-api.md: "only `ai_orchestration` imports provider
    SDKs; retrieved text is data"). Deliberately not a bespoke per-feature RPC shape, so BUILD-16's
    real Synthetic/Anthropic adapter can implement this same port without a redesign."""

    async def complete(self, prompt: str) -> str: ...


class NullAIProvider:
    """The only `AIProvider` implementation this increment ships -- no real Synthetic/Anthropic
    call exists yet (BUILD-16's scope). `AI_ENABLED=true` uses this today, not a mock: it's the
    honest, literal "no real provider yet" state, always returning the same fixed "no
    suggestions" response so callers can be tested end-to-end without real credentials."""

    #: An empty JSON array -- the shape `plans_import/ai_mapper.py::AIImportMapper` expects,
    #: meaning "no suggestions" (never invents a plausible-looking mapping with no real basis).
    STUB_RESPONSE = "[]"

    async def complete(self, prompt: str) -> str:
        _ = prompt
        return self.STUB_RESPONSE

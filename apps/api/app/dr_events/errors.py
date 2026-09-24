from __future__ import annotations

from app.core.errors import AppError


class DrEventNotFoundError(AppError):
    """Lives in this leaf module, not `commands.py`, so modules that `dr_events.commands` itself
    depends on (via `plans_import.commands` -> `work_streams.commands`) can raise it without an import
    cycle. `dr_events.commands` imports it for its own use, so existing imports from there still work."""

    code = "DR_EVENT_NOT_FOUND"
    status_code = 404

    def __init__(self) -> None:
        super().__init__("DR Event not found.")

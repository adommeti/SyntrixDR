from __future__ import annotations

import uuid
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession


class EvidenceCounter(Protocol):
    """How many *acceptable* Evidence Items a Task has (D-226). "Acceptable" is BUILD-09's to
    define -- per D-245, INFECTED or not-yet-scanned files must never count."""

    async def count_acceptable_for_task(self, session: AsyncSession, task_id: uuid.UUID) -> int: ...


class NoEvidenceItemsYet:
    """The only `EvidenceCounter` shipped before BUILD-09. Returns 0 because it is literally true:
    nothing can create an `evidence_items` row until BUILD-09 builds that module.

    Consequence, and it is correct rather than a bug: a Task with D-226's default
    `evidence_required=true, evidence_min_count=1` cannot reach READY_FOR_VALIDATION until BUILD-09
    swaps this out. Deliberately not a stub returning a large count, which would make the D-226
    guard pass vacuously (BUILD-06.plan.md Risk #4)."""

    async def count_acceptable_for_task(self, session: AsyncSession, task_id: uuid.UUID) -> int:
        _ = (session, task_id)
        return 0

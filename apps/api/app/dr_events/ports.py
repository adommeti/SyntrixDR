"""`RecoveryMetrics` port: what a successful technical validation computes for a DR Application
(RTO actual / passed, RPO loss / passed, breach facts -- D-208, D-257). BUILD-10 provides the real
adapter; until then `NoRecoveryMetricsYet` records nothing beyond `technical_validated_at`, which the
transition service stamps itself (the RTO clock stop is a status fact, not a computation)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Protocol

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.dr_events.models import DrApplication


class RecoveryMetrics(Protocol):
    async def on_technical_validation(
        self, session: AsyncSession, dr_application: DrApplication, *, validated_at: datetime
    ) -> None: ...


class NoRecoveryMetricsYet:
    """Honest no-op: no RTO/RPO numbers exist before BUILD-10, so none are written."""

    async def on_technical_validation(
        self, session: AsyncSession, dr_application: DrApplication, *, validated_at: datetime
    ) -> None:
        _ = (session, dr_application, validated_at)
        return None


async def get_recovery_metrics() -> RecoveryMetrics:
    """FastAPI dependency -- the one place BUILD-10 swaps in the real calculator."""
    return NoRecoveryMetricsYet()


RecoveryMetricsDep = Annotated[RecoveryMetrics, Depends(get_recovery_metrics)]

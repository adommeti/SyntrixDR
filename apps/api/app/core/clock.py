from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FakeClock:
    def __init__(self, fixed: datetime | None = None) -> None:
        self._fixed = fixed or datetime(2026, 1, 1, tzinfo=UTC)

    def now(self) -> datetime:
        return self._fixed

    def advance(self, **kwargs: float) -> None:
        from datetime import timedelta

        self._fixed = self._fixed + timedelta(**kwargs)

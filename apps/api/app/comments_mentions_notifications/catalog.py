"""Backend half of the D-234 catalog: typed names for alert types, notification types and
severities. `packages/contracts` mirrors it (BUILD-08 session b). A new operational type needs an
ADR/spec update first (D-234); nothing here is free-text at a call site."""

from __future__ import annotations

from enum import StrEnum


class Severity(StrEnum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class AlertType(StrEnum):
    BLOCKER_ESCALATED = "BLOCKER_ESCALATED"


class NotificationType(StrEnum):
    BLOCKER_ESCALATED = "BLOCKER_ESCALATED"

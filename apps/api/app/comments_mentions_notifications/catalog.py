"""Backend half of the D-234 catalog for alerts and notifications: typed names for alert types,
notification types and severities. `packages/contracts/catalog.ts` mirrors them and
`tests/test_catalogs.py` pins the pairs. A new operational type needs an ADR/spec update first (D-234);
nothing here is free-text at a call site."""

from __future__ import annotations

from enum import StrEnum


class Severity(StrEnum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class AlertType(StrEnum):
    """Shared operational conditions (GLOSSARY.md "Alert": breach, warning, escalation)."""

    BLOCKER_ESCALATED = "BLOCKER_ESCALATED"
    SLA_WARNING = "SLA_WARNING"
    RTO_BREACH = "RTO_BREACH"
    RPO_BREACH = "RPO_BREACH"


#: Acknowledging these needs a reason (FROZEN §9.8: a breach alert requires acknowledgement + reason).
BREACH_ALERT_TYPES = frozenset({AlertType.RTO_BREACH, AlertType.RPO_BREACH})


class NotificationType(StrEnum):
    """Per-user in-app deliveries (GLOSSARY.md "Notification")."""

    BLOCKER_ESCALATED = "BLOCKER_ESCALATED"
    MENTIONED = "MENTIONED"
    ASSIGNMENT_CHANGED = "ASSIGNMENT_CHANGED"

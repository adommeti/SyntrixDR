"""Backend half of the D-234 catalogs that are not owned by one module: the outbox / WebSocket event
union (API_CONTRACT.md §WebSocket, V1 union) and the timeline event types. `packages/contracts/catalog.ts`
mirrors both; `tests/test_catalogs.py` pins the pairs. A new member needs an ADR/spec update first."""

from __future__ import annotations

from enum import StrEnum


class OutboxEventType(StrEnum):
    """The V1 union (API_CONTRACT.md:274). Every `write_outbox` call names one of these."""

    TASK_CHANGED = "TaskChanged"
    BLOCKER_CHANGED = "BlockerChanged"
    ISSUE_CHANGED = "IssueChanged"
    MILESTONE_CHANGED = "MilestoneChanged"
    APPLICATION_HEALTH_CHANGED = "ApplicationHealthChanged"
    EVENT_HEALTH_CHANGED = "EventHealthChanged"
    RTO_RPO_CHANGED = "RtoRpoChanged"
    NOTIFICATION_CREATED = "NotificationCreated"
    TIMELINE_EVENT_CREATED = "TimelineEventCreated"
    NEEDS_REVIEW_CHANGED = "NeedsReviewChanged"
    ASSIGNMENT_CHANGED = "AssignmentChanged"
    SLA_WARNING = "SlaWarning"
    SLA_BREACHED = "SlaBreached"
    REPORT_PUBLISHED = "ReportPublished"
    EVENT_STATE_CHANGED = "EventStateChanged"
    PARTICIPANT_CHANGED = "ParticipantChanged"


class TimelineEventType(StrEnum):
    """Meaningful operational moments on the Event timeline (UI_UX.md §Event timeline: filters by phase,
    Application, Work Stream, user, state, override, Blocker/Issue, RTO/RPO and AI action)."""

    EVENT_STATE_CHANGED = "EVENT_STATE_CHANGED"
    TASK_STATE_CHANGED = "TASK_STATE_CHANGED"
    BLOCKER_STATE_CHANGED = "BLOCKER_STATE_CHANGED"
    ISSUE_STATE_CHANGED = "ISSUE_STATE_CHANGED"
    MILESTONE_STATE_CHANGED = "MILESTONE_STATE_CHANGED"
    OVERRIDE_RECORDED = "OVERRIDE_RECORDED"
    VALIDATION_DECIDED = "VALIDATION_DECIDED"
    RTO_RPO_RECORDED = "RTO_RPO_RECORDED"
    SLA_WARNING = "SLA_WARNING"
    SLA_BREACH = "SLA_BREACH"
    AI_ACTION = "AI_ACTION"
    REPORT_PUBLISHED = "REPORT_PUBLISHED"

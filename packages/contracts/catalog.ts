/**
 * Hand-written typed catalogs (D-216, D-234). These are the single canonical
 * source for these enums on the frontend; never redeclare them elsewhere.
 * Extending any of these lists requires an ADR/spec update, not just a code change.
 */

// D-216: canonical polymorphic target enum. Each consuming table accepts only
// a subset (service/CHECK-enforced) — this is the full 16-label superset.
export const TARGET_TYPES = [
  "DR_EVENT",
  "DR_APPLICATION",
  "APPLICATION",
  "WORK_STREAM",
  "TASK",
  "TASK_DEPENDENCY",
  "MILESTONE",
  "BLOCKER",
  "ISSUE_FINDING",
  "VALIDATION",
  "IMPORT_JOB",
  "PLAN",
  "PLAN_VERSION",
  "REPORT",
  "DOCUMENT",
  "ALERT",
] as const;
export type TargetType = (typeof TARGET_TYPES)[number];

// D-234: severity catalog for notifications/alerts/findings.
export const SEVERITIES = ["CRITICAL", "HIGH", "MEDIUM", "LOW"] as const;
export type Severity = (typeof SEVERITIES)[number];

// D-234: alert types (GLOSSARY "Alert": breach, warning, escalation).
export const ALERT_TYPES = ["BLOCKER_ESCALATED", "SLA_WARNING", "RTO_BREACH", "RPO_BREACH"] as const;
export type AlertType = (typeof ALERT_TYPES)[number];

// D-234: per-user notification types.
export const NOTIFICATION_TYPES = ["BLOCKER_ESCALATED", "MENTIONED", "ASSIGNMENT_CHANGED"] as const;
export type NotificationType = (typeof NOTIFICATION_TYPES)[number];

// D-234: WebSocket / outbox event type catalog — the V1 union (API_CONTRACT §WebSocket).
// The backend writes only these to `outbox_events`; BUILD-11 forwards them to clients.
export const WS_EVENT_TYPES = [
  "TaskChanged",
  "BlockerChanged",
  "IssueChanged",
  "MilestoneChanged",
  "ApplicationHealthChanged",
  "EventHealthChanged",
  "RtoRpoChanged",
  "NotificationCreated",
  "TimelineEventCreated",
  "NeedsReviewChanged",
  "AssignmentChanged",
  "SlaWarning",
  "SlaBreached",
  "ReportPublished",
  "EventStateChanged",
  "ParticipantChanged",
] as const;
export type WsEventType = (typeof WS_EVENT_TYPES)[number];

// D-234: timeline event types (UI_UX "Event timeline" filter facets).
export const TIMELINE_EVENT_TYPES = [
  "EVENT_STATE_CHANGED",
  "TASK_STATE_CHANGED",
  "BLOCKER_STATE_CHANGED",
  "ISSUE_STATE_CHANGED",
  "MILESTONE_STATE_CHANGED",
  "OVERRIDE_RECORDED",
  "VALIDATION_DECIDED",
  "RTO_RPO_RECORDED",
  "SLA_WARNING",
  "SLA_BREACH",
  "AI_ACTION",
  "REPORT_PUBLISHED",
] as const;
export type TimelineEventType = (typeof TIMELINE_EVENT_TYPES)[number];

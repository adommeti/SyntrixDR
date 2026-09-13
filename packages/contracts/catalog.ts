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

// D-234: WebSocket event type catalog. Empty until the first realtime-emitting
// module (BUILD-11, transactional outbox -> Redis pub/sub -> clients). Not a
// placeholder — genuinely no event types are defined yet.
export const WS_EVENT_TYPES = [] as const;
export type WsEventType = (typeof WS_EVENT_TYPES)[number];

# DR Command Center — Frozen FastAPI Contract

**Reconciled 2026-09-12 against FREEZE_ADDENDUM.md (D-201…D-261).**

This file is the authoritative endpoint inventory; where other documents list endpoints that do not appear here, this file wins. (D-207)

## API principles

1. Explicit command endpoints for lifecycle mutations; never generic raw-status updates.
2. Pydantic v2 request/response schemas.
3. UUID resource IDs.
4. Optimistic concurrency through `expected_version` / `If-Match`-style semantics. A stale write returns `409 CONCURRENCY_CONFLICT`, except the Manager-precedence case documented under `tasks/{id}/assign`. (D-214)
5. `Idempotency-Key` header is **required on every command POST**; a missing header returns `400 IDEMPOTENCY_KEY_REQUIRED`. Keys are retained 24 hours. GET/queries are exempt. A pure pre-signed-upload handshake may be exempt, but the domain command attaching the artifact requires it. (D-215)
6. Server-side RBAC/transition guards are authoritative.
7. All successful state-changing commands emit Audit Event + real-time domain event.
8. Error payload is stable and non-leaking.
9. `packages/contracts` is generated from the FastAPI OpenAPI document with `openapi-typescript`; CI fails on drift. Zod is used only for UI/form/runtime validation, never as a hand-copied backend contract. (D-251)

## Explicitly not V1

The following endpoints/behaviours from pre-freeze material are **not** part of V1: (D-207)

- Event Pause/Resume (`/dr-events/{id}/pause`, `/dr-events/{id}/resume`); cancellation exists, pause does not.
- Event Abort (superseded by `cancel`).
- Task Skip.
- `POST /tasks/{id}/complete` — completion happens only through the canonical `submit-validation → validate` transition.
- Generic `/validations/{id}/approve|reject` — Validation has no separate REST resource in V1. (D-210)
- Generic `/ai/chat` — AI is exposed only through `read`, `recommend` and `act/prepare|confirm`.

## Error envelope

```json
{
  "error": {
    "code": "TASK_TRANSITION_NOT_ALLOWED",
    "message": "The task cannot start because a hard dependency is incomplete.",
    "correlation_id": "uuid",
    "details": {}
  }
}
```

## Error codes

| Code | HTTP | Meaning |
|---|---:|---|
| `IDEMPOTENCY_KEY_REQUIRED` | 400 | Command POST sent without `Idempotency-Key` header (D-215) |
| `CONCURRENCY_CONFLICT` | 409 | Stale `expected_version`; not raised for the MANAGER_PRECEDENCE case (D-214) |
| `INVALID_TRANSITION` | 409 | Requested lifecycle command is not valid from the current state (STATE_MACHINES.md) |
| `DEPENDENCY_NOT_SATISFIED` | 409 | HARD dependency/manual gate incomplete and no permitted override present |
| `READINESS_HARD_STOP` | 409 | A `HARD_STOP` readiness check failed on `activate` (D-224) |
| `CLOSURE_HARD_STOP` | 409 | Closure guard failed on `close` (non-terminal child Event, outstanding monitoring Tasks without exception override) (D-219, D-227) |
| `OVERRIDE_REASON_REQUIRED` | 400 | Override/force/cancel/acknowledge submitted without the mandatory reason |
| `EVIDENCE_REQUIRED` | 409 | `submit-validation` without the Task's required evidence count / verification note, or with only INFECTED/unscanned evidence (D-226, D-245) |
| `TENANT_PACKAGE_MISMATCH` | 409 | Package `tenant_id` does not match the importing tenant (D-230) |
| `PACKAGE_INTEGRITY_FAILED` | 422 | Package `content_hash` or decryption check failed (D-230) |
| `AI_CONFIRMATION_REQUIRED` | 409 | `ai/act/confirm` called without a valid prepared confirmation for the active `ai.control_profile` (D-228) |
| `MONITORING_CLOSURE_WARNING` | 409 | `close` blocked by non-completed MONITORING Work Stream Tasks; retry with an audited closure exception override (D-227) |
| `TASK_TRANSITION_NOT_ALLOWED` | 409 | Existing Task-specific transition error; retained for compatibility |

The addendum fixes the HTTP status only for `IDEMPOTENCY_KEY_REQUIRED` (400) and `CONCURRENCY_CONFLICT` (409); the remaining status values are derived conventions and may be adjusted at BUILD time without a spec change.

## Canonical `target_type` enum

[DECIDED] Polymorphic targets use one canonical enum: `DR_EVENT, DR_APPLICATION, APPLICATION, WORK_STREAM, TASK, TASK_DEPENDENCY, MILESTONE, BLOCKER, ISSUE_FINDING, VALIDATION, IMPORT_JOB, PLAN, PLAN_VERSION, REPORT, DOCUMENT, ALERT`. It is used by comments, evidence, overrides, needs-review items, alerts, notifications, audit links and `packages/contracts`. Each endpoint accepts only its permitted subset (enforced by CHECK constraint and/or service). (D-216)

## Authentication / session

[DECIDED] Session endpoints derived from D-235 and D-239. These are **session endpoints, not domain commands**: `Idempotency-Key` is not required; CSRF protection is required on every POST except login (which establishes the session/CSRF token). (I-2, plan §10)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/auth/entra/login` | Redirect to Entra OIDC authorization endpoint (state/nonce stored server-side) (D-239) |
| GET | `/api/v1/auth/entra/callback` | OIDC callback; establishes the Redis-backed HttpOnly/Secure/SameSite=Lax session cookie; no tokens reach browser JS (D-239) |
| POST | `/api/v1/auth/local/login` | Local account login (Argon2id); rate-limited; 10 failures/15 min → 30-min lockout; TOTP step where enrolled/required; enters the same session/RBAC system (D-235, D-239) |
| POST | `/api/v1/auth/logout` | Terminate the current session (CSRF required) |
| POST | `/api/v1/auth/reauth` | Step-up via password/TOTP (Local) or Entra prompt; issues a reauth grant valid 5 minutes; required by high-risk commands (D-235) |
| POST | `/api/v1/auth/local/password-reset/request` | Emails a single-use reset token (30-min expiry); always 202, no account enumeration (D-235) |
| POST | `/api/v1/auth/local/password-reset/confirm` | Consumes the token and sets a new password (12–128 chars, breached-password denylist) (D-235) |
| POST | `/api/v1/auth/local/totp/enrol` | Start TOTP enrolment for the current Local user (required for Local GLOBAL_ADMIN, configurable for others) (D-235) |
| POST | `/api/v1/auth/local/totp/verify` | Confirm TOTP enrolment / verify a code during login or reauth (D-235) |
| GET | `/api/v1/auth/csrf` | Return the CSRF token bound to the current session for state-changing requests (D-239) |

## Identity / profile

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/me` | Current profile/roles/scopes/preferences |
| GET | `/api/v1/users/{id}` | Person deep-card data |
| GET | `/api/v1/teams` | Teams/filters |
| GET | `/api/v1/teams/{id}/workload` | Team resource roll-up |
| POST | `/api/v1/admin/local-users` | Explicitly create Local fallback user; high-risk; never via AI (D-228) |

## Application catalog

| Method | Path | Purpose |
|---|---|---|
| GET/POST | `/api/v1/applications` | List/create Application |
| GET/PATCH | `/api/v1/applications/{id}` | Master data |
| PUT | `/api/v1/applications/{id}/owners` | Up to 3 system + 3 business owner slots |
| GET | `/api/v1/applications/{id}/history` | Historical DR comparison (rendered as a tab on `/applications/[applicationId]`) (D-212) |

## Plans/import

| Method | Path | Purpose |
|---|---|---|
| GET/POST | `/api/v1/plans` | Reusable versioned Plans (UI: `/plans`, `/plans/[planId]`) (D-212) |
| GET | `/api/v1/plans/{id}` | Plan detail with versions (D-212, derived) |
| POST | `/api/v1/plans/{id}/versions` | New Plan version |
| POST | `/api/v1/dr-events/{event_id}/imports/excel` | Upload Excel and start import job; arbitrary headers accepted (D-221) |
| GET | `/api/v1/imports/{id}` | Import status/mapping; mapping/review step supports AI or manual column mapping (D-221) |
| POST | `/api/v1/imports/{id}/accept` | Accept reviewed mapping; rows keep `source_import_id` / `source_import_row` provenance (D-221) |

## DR Event lifecycle

| Method | Path | Purpose | High risk |
|---|---|---|---|
| POST | `/api/v1/dr-events` | Create PLANNED Event. Admin/Coordinator: any scope. App/System Owner: only an Event scoped to their own Application(s) (D-213) | No |
| GET | `/api/v1/dr-events/{id}` | Event deep detail | No |
| POST | `/api/v1/dr-events/{id}/activate` | PLANNED→ACTIVE; readiness catalog evaluated (D-224); Admin/Coordinator only (D-213) | Yes |
| POST | `/api/v1/dr-events/{id}/start-failover` | ACTIVE→FAILOVER_IN_PROGRESS; capture baseline/network cut. Optional body `network_cut_at` (ISO-8601) with `activated_at ≤ network_cut_at ≤ now()`; defaults to server `now()`; applies to PLANNED_DR and REAL_INCIDENT; audited (D-237). Side effect: every in-scope DR Application `NOT_STARTED → RECOVERING` in the same transaction, audited per Application (I-3, plan §10). Admin/Coordinator only (D-213) | Yes |
| POST | `/api/v1/dr-events/{id}/mark-failed-over` | Failover validation transition; Admin/Coordinator only (D-213) | Yes |
| POST | `/api/v1/dr-events/{id}/start-failback` | FAILED_OVER→FAILBACK_IN_PROGRESS; Admin/Coordinator only (D-213). Side effect: every DR Application with `failback_required=true` moves `FAILED_OVER → FAILBACK_IN_PROGRESS` in the same transaction, audited per Application (I-3, plan §10) | Yes |
| POST | `/api/v1/dr-events/{id}/close` | →CLOSED after guards. Monitoring closure guard: any non-cancelled, non-COMPLETED Task in a MONITORING Work Stream returns `MONITORING_CLOSURE_WARNING` unless the body carries an audited `closure_exception` override with reason (Admin/Coordinator) (D-227). Blocked with `CLOSURE_HARD_STOP` while any non-cancelled child Event is non-terminal (D-219). Admin/Coordinator only (D-213) | Yes |
| POST | `/api/v1/dr-events/{id}/cancel` | →CANCELLED; reason required; Admin/Coordinator only (D-213) | Yes |
| POST | `/api/v1/dr-events/{id}/clone` | Clone historical Event as new PLANNED Event | No |

Event lifecycle commands are never available through AI Act. (D-228)

## Event participants

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/dr-events/{event_id}/participants` | List `dr_event_participants` with enrolment source (auto rule or explicit) (D-222) |
| POST | `/api/v1/dr-events/{event_id}/participants` | Explicit enrolment of a User (e.g. Auditor/Executive per-Event read access); Admin/Coordinator (D-222) |

Auto-enrolment (Event-scoped role, current Task assignee, Blocker owner, any-slot System/Application or Business Owner of an in-scope Application, Work Stream Lead, member/manager of an Owning Team with Tasks in the Event) is performed by the domain services and needs no API call. Global Admins are implicit participants of every Event. (D-222)

## Event resources

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/dr-events/{event_id}/resources` | Operational resource view (people/Teams, assignment load, availability) backing `/dr-events/[eventId]/resources` (D-212) |

## Work Streams / Milestones

| Method | Path | Purpose |
|---|---|---|
| GET/POST | `/api/v1/dr-events/{event_id}/work-streams` | Manage streams; `stream_type` ∈ `NETWORK, STORAGE, DATABASE, APPLICATIONS, MONITORING, VALIDATION, CUSTOM` (D-227) |
| GET/POST | `/api/v1/dr-events/{event_id}/milestones` | Manage milestones |
| POST | `/api/v1/milestones/{id}/confirm` | Critical gate confirmation |

## Tasks

| Method | Path | Purpose |
|---|---|---|
| GET/POST | `/api/v1/dr-events/{event_id}/tasks` | Query/create Task; body carries `evidence_required` (default true), `evidence_min_count` (default 1), `verification_note_required` (default true), `needs_specific_validation` (D-209, D-226) |
| GET/PATCH | `/api/v1/tasks/{id}` | Non-state editable Task metadata |
| POST | `/api/v1/tasks/{id}/start` | NOT_STARTED→IN_PROGRESS; hard deps checked |
| POST | `/api/v1/tasks/{id}/block` | →BLOCKED; reason required; Blocker created |
| POST | `/api/v1/tasks/{id}/resume` | BLOCKED→IN_PROGRESS, **manual path**: used when the Task is BLOCKED but no active Blocker remains (e.g. Blocker cancelled/closed by override), or when an authorized override (reason required, audited) resumes despite open Blockers. The normal path is automatic: `POST /blockers/{id}/verify` on the last active Blocker returns the Task in the same transaction (D-252). Never advances beyond IN_PROGRESS. Both paths emit `TASK_RESUMED` audit (I-1, plan §10) |
| POST | `/api/v1/tasks/{id}/submit-validation` | →READY_FOR_VALIDATION; creates the persisted Validation record as PENDING; requires ≥ `evidence_min_count` acceptable Evidence Items and a verification note where required, else `EVIDENCE_REQUIRED` (D-210, D-226) |
| POST | `/api/v1/tasks/{id}/validate` | →COMPLETED (APPROVED) or rejection back to IN_PROGRESS (REJECTED); closes the Validation record. Caller must be an Application/System Owner (any slot) for Application-scoped work, the Work Stream Lead for shared work, or Admin/Coordinator; never the executor of the Task; never via AI (D-209, D-210, D-228) |
| POST | `/api/v1/tasks/{id}/cancel` | →CANCELLED |
| POST | `/api/v1/tasks/{id}/assign` | Current Assignee mutation with scope/concurrency rules. **MANAGER_PRECEDENCE:** when a Team Manager assigns a Task owned by their Team with a stale `expected_version` and the intervening change was an assignment by a DR Coordinator/Global Admin, the request is accepted (200, version increments), both actions are audited, the conflict resolution is recorded as `MANAGER_PRECEDENCE` and affected users are notified. Every other stale write returns `409 CONCURRENCY_CONFLICT` (D-214) |
| POST | `/api/v1/tasks/{id}/volunteer` | Claim eligible cross-Team work without changing Owning Team |

## Dependencies

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/v1/task-dependencies` | Create HARD/ADVISORY Finish-to-Start edge; cycle check |
| DELETE | `/api/v1/task-dependencies/{id}` | Delete edge subject to policy/audit |
| GET | `/api/v1/dr-events/{id}/dependency-graph` | Graph projection/impact |

## Blockers and Issues

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/dr-events/{event_id}/blockers` | Active/history blocker list |
| POST | `/api/v1/blockers/{id}/assign` | Route/claim blocker; OPEN→ASSIGNED (D-211) |
| POST | `/api/v1/blockers/{id}/start` | ASSIGNED→IN_PROGRESS; resolver begins work (D-211) |
| POST | `/api/v1/blockers/{id}/resolve` | Resolver declares fixed; IN_PROGRESS→RESOLVED (D-211) |
| POST | `/api/v1/blockers/{id}/verify` | Owning Team verifies unblock; RESOLVED→VERIFIED→CLOSED atomically, both transitions audited (D-211). When this closes the **last** active Blocker of the Task, the Task is returned `BLOCKED → IN_PROGRESS` atomically in the same transaction and `TASK_RESUMED` is emitted (D-252); a verify that leaves other Blockers active does not change the Task — `POST /tasks/{id}/resume` is the manual path (I-1, plan §10) |
| GET/POST | `/api/v1/dr-events/{event_id}/issues` | Issue/Finding list/create |
| POST | `/api/v1/issues/{id}/review` | OPEN→IN_REVIEW |
| POST | `/api/v1/issues/{id}/resolve` | resolve with note |
| POST | `/api/v1/issues/{id}/dismiss` | dismiss with note |

## Needs Review

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/dr-events/{event_id}/reviews` | Needs Review queue for the Event (OPEN/IN_REVIEW/RESOLVED/DISMISSED) backing `/dr-events/[eventId]/reviews` (D-212, derived) |
| POST | `/api/v1/reviews/{id}/review` | OPEN→IN_REVIEW (D-212, derived) |
| POST | `/api/v1/reviews/{id}/resolve` | →RESOLVED with note; scope per RBAC (D-212, derived) |
| POST | `/api/v1/reviews/{id}/dismiss` | →DISMISSED with note; scope per RBAC (D-212, derived) |

## Validation / Evidence

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/v1/evidence` | Create text/link evidence or upload metadata handshake |
| POST | `/api/v1/evidence/{id}/file` | File upload path subject to allowlist/100MB/scan; the pre-signed-upload handshake may be idempotency-exempt, the attaching command is not (D-215) |
| GET | `/api/v1/evidence/{id}` | Retrieve metadata/content authorization |
| POST | `/api/v1/dr-applications/{id}/submit-validation` | RECOVERING→TECHNICAL_VALIDATION (failover phase) or requests failback validation while FAILBACK_IN_PROGRESS; creates the DR Application Validation record as PENDING. Caller: any App Owner slot, Coordinator or Admin. Guard: no Task of the Application in the current phase (FAILOVER / FAILBACK) may be `BLOCKED` or `IN_PROGRESS` unless an audited override with reason is supplied. Policy `application.auto_submit_validation` (default `false`) may auto-fire this command when all phase Tasks of the Application are `COMPLETED` (I-3, plan §10) |
| POST | `/api/v1/dr-applications/{id}/validate` | Technical app validation; computes RTO/RPO; closes the DR Application Validation record (APPROVED/REJECTED); on approval moves TECHNICAL_VALIDATION→FAILED_OVER, or →COMPLETED when `failback_required=false`; RTO clock stops. In the failback phase, approval moves FAILBACK_IN_PROGRESS→COMPLETED. Rejection returns the Application to RECOVERING with a note (I-3, plan §10). Any System/Application Owner slot, Admin or Coordinator; never via AI (D-208, D-210, D-228, D-254) |

Validation is a persisted record with no separate REST resource in V1; it is created and closed only by the Task and DR Application commands above. (D-210)

[DECIDED] DR Application transitions that are **not** commands on the Application: `NOT_STARTED → RECOVERING` for every in-scope DR Application is a side effect of Event `start-failover`, and `FAILED_OVER → FAILBACK_IN_PROGRESS` for every Application with `failback_required=true` is a side effect of Event `start-failback` — both applied in the same transaction as the Event command and audited per Application. `FAILBACK_IN_PROGRESS → COMPLETED` is reached by `submit-validation` again followed by `validate` in the failback phase. (I-3, plan §10)

## Collaboration

| Method | Path | Purpose |
|---|---|---|
| GET/POST | `/api/v1/{target_type}/{target_id}/comments` | Cross-Team comments; `target_type` from the canonical enum (D-216) |
| POST | `/api/v1/alerts/{id}/acknowledge` | Shared ack + reason where breach requires it |
| POST | `/api/v1/alerts/{id}/snooze` | Personal snooze |

## Dashboards

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/dr-events/{id}/dashboard` | Coordinator KPI/treemap/dial/timeline; parent Events aggregate descendant DR Applications (D-219) |
| GET | `/api/v1/dr-events/{id}/health/explain` | Exact health inputs/contributions per D-223 formulas, bands and overlays |
| GET | `/api/v1/my-dr` | Personalized Ready/In Progress/Blocked/Risk/Confirmation/Completed |
| GET | `/api/v1/managers/{id}/rollup` | Org-tree metrics |

## Search / AI

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/v1/search` | Hybrid read-only search |
| POST | `/api/v1/ai/read` | Grounded Q&A |
| POST | `/api/v1/ai/recommend` | Non-mutating recommendation |
| POST | `/api/v1/ai/act/prepare` | Prepare action/confirmation payload; allowlisted commands only (D-228) |
| POST | `/api/v1/ai/act/confirm` | Execute confirmed action through the normal command service under the caller's own RBAC; requires a valid prepared confirmation for the active `ai.control_profile`, else `AI_CONFIRMATION_REQUIRED` (D-228) |
| POST | `/api/v1/speech/transcribe` | Provider-agnostic speech-to-text; returns editable text only (faster-whisper first) (D-232) |

AI Act allowlist: task start/block/resume/submit-validation/assign/volunteer; blocker assign/start/resolve; milestone confirm; issue create/review/resolve; comment create; alert acknowledge/snooze. Never via AI: any Event lifecycle command, Task/App validation, report publish, package export/import, role/policy/admin/security configuration, local-user creation. Excluded commands stay excluded in every control profile. (D-228)

## Reports/exports

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/v1/dr-events/{id}/reports/generate` | Generate Draft facts + optional AI narrative |
| PATCH | `/api/v1/reports/{id}` | Edit Draft narrative |
| POST | `/api/v1/reports/{id}/publish` | Publish; high-risk; never via AI (D-228) |
| POST | `/api/v1/reports/{id}/export/pdf` | PDF (Playwright/Chromium print in the worker) (D-244) |
| POST | `/api/v1/reports/{id}/export/csv` | CSV |
| POST | `/api/v1/reports/{id}/export/excel` | Excel |
| POST | `/api/v1/dr-events/{id}/raci/export` | PDF/CSV RACI; Work Stream and Application sections (D-229) |
| POST | `/api/v1/dr-events/{id}/packages/export` | Encrypted package; high-risk; never via AI (D-228, D-230) |
| POST | `/api/v1/packages/import` | exact restore / clone-as-new; high-risk; `TENANT_PACKAGE_MISMATCH` / `PACKAGE_INTEGRITY_FAILED` on rejection (D-230) |

## Admin/config

| Method | Path | Purpose |
|---|---|---|
| GET/PUT | `/api/v1/admin/policies` | Global policy values (keys/defaults per D-225) |
| GET/PUT | `/api/v1/admin/tiers` | SLA/RTO/health-weight defaults |
| GET/PUT | `/api/v1/admin/notifications/email` | SMTP config/test; secret references only (D-256) |
| POST | `/api/v1/admin/notifications/email/test` | Test configured email |
| GET/PUT | `/api/v1/admin/ai` | Provider/model/feature flags; secrets stored via Key Vault path |
| GET/PUT | `/api/v1/admin/file-policy` | allowlist/max size/scanning settings |

## WebSocket

`/api/v1/ws/dr-events/{event_id}`

[DECIDED] WebSocket event types are defined once in the canonical typed catalog in `packages/contracts` (mirrored by backend enums); new event types require an ADR/spec update. (D-234)

The V1 union is: `TaskChanged`, `BlockerChanged`, `IssueChanged`, `MilestoneChanged`, `ApplicationHealthChanged`, `EventHealthChanged`, `RtoRpoChanged`, `NotificationCreated`, `TimelineEventCreated`, `NeedsReviewChanged`, `AssignmentChanged`, `SlaWarning`, `SlaBreached`, `ReportPublished`, `EventStateChanged`, `ParticipantChanged`. (D-234)

WebSocket authorization is Event-participant scoped (`dr_event_participants`) and uses the same session cookie as REST (D-222, D-239). Reconnect triggers normal REST refetch; socket messages are not source-of-truth data. Delivery path is PostgreSQL transaction → transactional outbox → worker → Redis → API replicas → clients; correctness never depends on session affinity. (D-242)

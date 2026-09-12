# DR Command Center — Frozen Product Requirements Document

## 1. Product definition

[DECIDED] DR Command Center is a real-time, AI-assisted internal command platform for preparing, executing, validating, failing back, auditing and reporting disaster-recovery exercises and real incidents.

[DECIDED] It replaces fragmented spreadsheets, distributed runbooks, chat-driven handoffs and after-the-fact evidence assembly with one live operational model spanning people, Applications, Work Streams, Tasks, Milestones, Dependencies, Blockers, Issues/Findings, Evidence, RTO/RPO, Failover, Failback and reporting.

## 2. Primary users

- [DECIDED] Global Admin — global configuration plus full DR lifecycle authority.
- [DECIDED] DR Coordinator — Event-level command, cross-Team allocation, overrides, failover/failback, closure and reporting.
- [DECIDED] Work Stream Lead — scoped control/validation of shared Network/Storage/Database/Applications/Monitoring/Validation work.
- [DECIDED] Application/System Owner — technical owner and validator; up to Primary/Secondary/Tertiary.
- [DECIDED] Business Owner — business stakeholder; up to Primary/Secondary/Tertiary; V1 confirmation is a note/checkbox, not a mandatory gate.
- [DECIDED] Manager — Team resource oversight/reassignment and org-tree roll-up.
- [DECIDED] Engineer/Executor — executes work, records evidence/comments/blockers and collaborates cross-Team.
- [DECIDED] Auditor/Compliance reader — consumes evidence, timeline, reports and exports.
- [DECIDED] Executive/Senior Manager — consumes high-level health, RTO/RPO, blocker and resource roll-ups.

## 3. Product outcomes

1. [DECIDED] Run a comprehensive regional DR end to end from one system.
2. [DECIDED] Know within 10 seconds what is failing and what requires action.
3. [DECIDED] Track Task execution without confusing Task completion with Application recovery success.
4. [DECIDED] Measure Application RTO and RPO, including late-recovery and data-recovery outcomes.
5. [DECIDED] Maintain shared infrastructure Work Streams while preserving application-centric views.
6. [DECIDED] Allow parallel work when dependencies permit it and isolate blocking to impacted downstream work.
7. [DECIDED] Preserve fixed Owning Team accountability while allowing cross-Team execution help.
8. [DECIDED] Explain health/forecast/blockers/resource load in real time.
9. [DECIDED] Produce auditor-ready history/evidence/reporting without manual reconstruction.
10. [DECIDED] Remain fully operable when AI is disabled/unavailable.

## 4. V1 scope

### 4.1 DR planning and readiness

- [DECIDED] Create planned or real DR Events.
- [DECIDED] Select all Applications or a subset.
- [DECIDED] Parent comprehensive Event may roll up independent child/sub-DR Events.
- [DECIDED] Reusable versioned Plans instantiate into Events and then diverge independently.
- [DECIDED] Excel runbook import with AI-assisted mapping/decomposition and human review.
- [DECIDED] Full manual creation/editing of Tasks, Subtasks, Dependencies and Milestones.
- [DECIDED] Baseline snapshot at DR start/network cut.
- [DECIDED] Readiness Hard Stops/Warnings/Off; no separate formal approval chain.
- [DECIDED] Failback Plan required by default; feature toggle permits explicit failback-not-required.

### 4.2 Shared execution model

- [DECIDED] Explicit Work Streams: Network, Storage, Database, Applications, Monitoring, Validation plus custom streams.
- [DECIDED] Task may belong to both DR Application and Work Stream.
- [DECIDED] HARD and ADVISORY Dependencies.
- [DECIDED] First-class Milestones with critical manual confirmation by default.
- [DECIDED] Manual handoffs such as Network Ready → Storage and Storage Ready → Database.
- [DECIDED] Deterministic critical-path forecast from remaining HARD dependency work and expected durations.

### 4.3 Task, Blocker and Issue behavior

- [DECIDED] Task states: NOT_STARTED, IN_PROGRESS, BLOCKED, READY_FOR_VALIDATION, COMPLETED, CANCELLED.
- [DECIDED] Derived Ready = NOT_STARTED + all HARD prerequisites/gates satisfied.
- [DECIDED] BLOCKED Task has structured Blocker reason, responsibility, escalation, resolution and verification history.
- [DECIDED] Separate Issue/Finding captures non-blocking observation/risk/deviation.
- [DECIDED] Required evidence + explicit verification/validation note before protected completion.
- [DECIDED] Any Event participant may comment on visible work and @mention other users.

### 4.4 Resource model

- [DECIDED] Owning Team is fixed accountability; Current Assignee may come from another Team.
- [DECIDED] Managers reassign only within their Team; Coordinators cross-Team.
- [DECIDED] Same-Team peers may reassign eligible work.
- [DECIDED] Users may volunteer for eligible cross-Team unassigned work without changing Owning Team.
- [DECIDED] Skills/capabilities V1; proficiency scoring V2.
- [DECIDED] Workload is dynamic from live assignments/states, not manually entered capacity.

### 4.5 RTO/RPO and application validation

- [DECIDED] Tier targets: T0 30m, T1 60m, T2 120m, T3 240m, T4 1440m.
- [DECIDED] Regional recovery clock starts at common network-cut timestamp.
- [DECIDED] RTO completes only on technical Application validation.
- [DECIDED] Late success does not erase a recorded RTO/SLA breach.
- [DECIDED] RPO stores target, pre-DR reference/snapshot time, recovered-data time/data-loss result and pass/fail.
- [DECIDED] Application Owner validates app work; Work Stream Lead validates shared work.
- [DECIDED] Business confirmation remains optional note/checkbox V1.

### 4.6 Failback

- [DECIDED] Independent Failback Plan/Tasks/Dependencies; never inferred as reverse failover.
- [DECIDED] Explicit Start Failback by Global Admin/DR Coordinator.
- [DECIDED] Manual gates/handoffs supported.
- [DECIDED] Independent target/forecast/reporting.

### 4.7 Dashboards and UX

- [DECIDED] Coordinator Command Center: weighted dial, Application treemap, resource sidebar, critical Blockers/Issues, phase/timeline, RTO/RPO risk, AI chat.
- [DECIDED] Treemap size represents criticality/tier weight; color progresses Green → Light Orange → Dark Orange → Red.
- [DECIDED] Health: Application = 70% Task + 30% Validation; Event = tier-weighted aggregate.
- [DECIDED] Weights: 100/75/50/30/20 for T0–T4.
- [DECIDED] No Tier 0 hard cap on dial.
- [DECIDED] Overall bands: GREEN ≥ 85; YELLOW 60–84.99; RED < 60 (D-223).
- [DECIDED] My DR tiles: Ready, In Progress, Blocked, SLA/RTO Risk, Needs My Confirmation, Completed.
- [DECIDED] Kanban/swimlane, labels, filters and live-linked Saved Views.
- [DECIDED] Hover quick summaries + Show More deep cards.
- [DECIDED] Light/Dark, WCAG 2.1 AA, desktop-first responsive design.
- [DECIDED] Figma clickable prototype and UI/UX sign-off are gates.

### 4.8 Search and AI

- [DECIDED] Structured filters + PostgreSQL full text + pgvector semantic search.
- [DECIDED] Structured dependencies are authoritative; semantic evidence is supplemental and cited.
- [DECIDED] V1 AI provider abstraction supports Synthetic and direct Anthropic.
- [DECIDED] Synthetic is preferred first POC provider; Kimi K3/GLM 5.3 were examples discussed.
- [DECIDED] Default embeddings: nomic-embed-text-v1.5, 768 dimensions, configurable.
- [DECIDED] AI Read / Recommend / Act; Act uses the same command services/RBAC/guards/reauth/audit.
- [DECIDED] Low-confidence output enters Needs Review: OPEN → IN_REVIEW → RESOLVED/DISMISSED.
- [DECIDED] AI summaries behind feature flag.
- [DECIDED] Provider-agnostic speech-to-text V1; user reviews transcription before submit.

### 4.9 Notifications

- [DECIDED] In-app notifications V1.
- [DECIDED] Email functional V1 through Admin-configurable SMTP abstraction.
- [DECIDED] Email failure does not rollback DR state.
- [DECIDED] Teams/Slack shown as Coming Soon only.
- [DECIDED] @mentions, personal snooze and shared acknowledgement.

### 4.10 Evidence, audit and reports

- [DECIDED] Evidence types: text/verification note, files, screenshots, logs, links.
- [DECIDED] File allowlist, 100MB default max, malware scanning in production.
- [DECIDED] Append-only audit.
- [DECIDED] Final report begins Draft; Admin/Coordinator can edit/publish; narrative versioned.
- [DECIDED] Report contains recovery timeline, RTO/RPO, validation, baseline drift, Blockers/Issues, overrides/exceptions, failback, monitoring and audit summary.
- [DECIDED] In-app, PDF, CSV and Excel report/export paths.
- [DECIDED] RACI export PDF/CSV.
- [DECIDED] Full encrypted package exact-restore and clone-as-new; AES-256-GCM; integrity validation; company/tenant binding.

## 5. V1 non-functional acceptance

- [DECIDED] 99.9% service objective.
- [DECIDED] Command Center RTO/RPO = 60m/15m.
- [DECIDED] Normal API average <500ms under POC load.
- [DECIDED] Dashboards usable within a couple seconds.
- [DECIDED] Connected live view state propagation <=10 seconds.
- [DECIDED] Load test: 500 Applications, 5,000 Tasks, 200 concurrent users.
- [DECIDED] Latest Chrome/Edge certification.
- [DECIDED] WCAG 2.1 AA.
- [DECIDED] Every critical workflow passes with AI disabled.
- [DECIDED] Backup/restore and rollback drill pass.
- [DECIDED] Security scanning gates pass.

## 6. Retention

- [DECIDED] Audit: 7 years.
- [DECIDED] DR Events/Tasks: 3 years.
- [DECIDED] Evidence: classification-configurable; default 3 years.
- [DECIDED] Legal hold suspends deletion.
- [DECIDED] Reports/imports/semantic indexes follow applicable source/Event/Evidence policy unless longer policy is configured.

## 7. Security baseline

- [DECIDED] Entra ID primary; explicitly created Local fallback accounts.
- [DECIDED] Event-participant default visibility; broad cross-Team read inside Event; scoped writes.
- [DECIDED] Reauth/MFA policy for high-risk operations.
- [DECIDED] Key Vault for production secrets/keys.
- [DECIDED] Secure sessions, CSRF where applicable, strict CORS, rate limiting and safe errors.
- [DECIDED] AI-retrieved content is untrusted; permission filtering occurs before retrieval.
- [DECIDED] SAST, dependency scan, SBOM and container scan gates.

## 8. Explicit V1 exclusions / later work

- [DECIDED] Functional Teams/Slack.
- [DECIDED] ServiceNow/Remedy/CMDB synchronization and alert suppression.
- [DECIDED] Microsoft Graph org sync.
- [DECIDED] Azure AI Foundry provider.
- [DECIDED] Calendar invitations/recurrence.
- [DECIDED] Public status page.
- [DECIDED] Skill proficiency scoring.
- [DECIDED] Formal compliance-framework certification/mapping.
- [DECIDED] ML forecasting.
- [DECIDED] Mandatory Business Owner validation gates.
- [DECIDED] Multi-tenant SaaS/on-prem deployment productization.

## 9. Release success definition

[DECIDED] V1 POC succeeds when a realistic comprehensive regional DR can be planned/imported, readiness-checked, baseline-captured, executed with real-time shared Tasks/Dependencies/Blockers/Issues, measured for RTO/RPO, validated, failed back, closed, audited and reported; the Coordinator can understand critical state within 10 seconds; connected updates appear within 10 seconds; and the entire exercise also works with AI disabled.

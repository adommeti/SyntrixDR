**Reconciled 2026-09-12 against FREEZE_ADDENDUM.md (D-201…D-261).**

# DR Command Center — Frozen V1 Decision Register

**Freeze date:** 2026-09-11  
**Status:** AUTHORITATIVE FOR V1 BUILD  
**Precedence:** This file and the frozen Master Build Specification supersede earlier discovery wording where conflicts exist. Historical discovery artifacts are retained under `archive/` only for provenance. `FREEZE_ADDENDUM.md` (D-201…D-261) sits above this file and is applied throughout. (D-201)

## 1. Product and release boundary

1. [DECIDED] V1 is a single-tenant Azure POC capable of running one real coordinated DR exercise end to end: preparation, failover, validation, failback, closure, reporting, evidence, audit, RTO/RPO and AI assistance.
2. [DECIDED] Planned DR tests and real incidents use the same core execution engine.
3. [DECIDED] The live **DR Event** is the operational center of gravity; runbooks/plans are inputs/templates, not the live source of truth.
4. [DECIDED] V1 remains usable with AI completely unavailable.
5. [DECIDED] V1 is targeted as a 10-week POC; the clock starts when BUILD-01 actually begins, followed by a separate production-hardening phase after week 10. (D-206)
6. [DECIDED] SaaS multi-tenancy and on-premises deployment are future options, not V1 requirements.

## 2. Mandatory technology stack

1. [DECIDED] Frontend: Next.js App Router, React, TypeScript strict, Tailwind CSS, shadcn/ui-style primitives.
2. [DECIDED] Client data/forms: TanStack Query, React Hook Form, Zod.
3. [DECIDED] Visualization: standard chart library for conventional charts; D3 only where custom treemap/health-dial behavior requires it.
4. [DECIDED] Backend/API: Python 3.12+, FastAPI, Pydantic v2, SQLAlchemy 2.x, Alembic.
5. [DECIDED] Architecture: modular monolith; no premature microservices.
6. [DECIDED] Database: PostgreSQL 16+.
7. [DECIDED] Search: structured SQL filters + PostgreSQL full text + pgvector semantic retrieval.
8. [DECIDED] Vector store: pgvector. Pinecone and Chroma are not V1 dependencies.
9. [DECIDED] Background work: Celery + Redis from BUILD-01 behind job interfaces; no second scheduler. Jobs: escalations, SLA timers, report generation, document parsing/indexing, embedding, Excel import enrichment, package export, retention, notification fan-out. (D-241)
10. [DECIDED] Real-time: WebSockets in V1.
11. [DECIDED] File/evidence storage: Azure Blob Storage in Azure; **Azurite** locally; one `ObjectStore` adapter on `azure-storage-blob`. MinIO is not used. (D-217)
12. [DECIDED] Azure runtime: Azure Container Apps; Azure Database for PostgreSQL; Key Vault; Application Insights/Azure Monitor/Log Analytics.
13. [DECIDED] Infrastructure as Code: Bicep in V1.
14. [DECIDED] Entire stack must run end to end on a MacBook using containers.
15. [DECIDED] No Kubernetes in V1.
16. [DECIDED] No Node-based backend; Node is used only for the Next.js frontend/toolchain.
17. [DECIDED] Toolchain: Python 3.12, uv, Node 22 LTS, pnpm workspaces (no Turborepo), Next.js App Router + TypeScript strict (exact version pinned at BUILD-01, lockfiles committed, no floating versions in CI), Ruff, Pyright, ESLint, Prettier. (D-240)
18. [DECIDED] PDF generation via Playwright/Chromium print of a dedicated report route/template, in the background worker container. WeasyPrint is not used. (D-244)
19. [DECIDED] Speech-to-text: first provider is local faster-whisper in the worker behind a provider interface; Azure AI Speech is a future adapter. (D-232)
20. [DECIDED] Embeddings: `nomic-embed-text-v1.5` via sentence-transformers in the worker, 768 dimensions, identical locally and in Azure POC; adapter-based so hosted providers can be added. (D-233)
21. [DECIDED] Azure cache/broker: Azure Managed Redis (preferred). Azure credentials are a prerequisite only for BUILD-22/23; BUILD-01…21 run end to end on the MacBook. (D-248)

## 3. Deployment and Command Center resilience

1. [DECIDED] The Command Center must be hosted outside the workload DR failure domain it coordinates.
2. [DECIDED] Dev and Test use separate Azure resource groups; Production is isolated separately.
3. [DECIDED] POC may use public ingress; Production should use private ingress/network controls.
4. [DECIDED] V1 availability objective: 99.9%.
5. [DECIDED] Command Center RTO: 60 minutes.
6. [DECIDED] Command Center RPO: 15 minutes.
7. [DECIDED] Production PostgreSQL point-in-time-recovery retention: 35 days.
8. [DECIDED] Backup/restore drill and application rollback drill are release gates.
9. [DECIDED] No fake offline-write mode; if API/source of truth is unavailable, UI shows degraded state rather than pretending writes succeeded.

## 4. Identity, users and authorization

1. [DECIDED] Entra ID OIDC is the primary identity mechanism.
2. [DECIDED] Local accounts are supported in V1 for testing/fallback.
3. [DECIDED] No local production accounts are pre-created. A Global Admin explicitly creates one when needed.
4. [DECIDED] Local-password policy is NIST-style length-based with rate limiting, lockout, reset flow and audit logging; privileged local accounts use MFA where feasible. Exact parameters: Argon2id; password 12–128 chars, no composition rules, breached-password denylist; 10 failures/15 min → 30-min lockout; 8 h absolute session, 30 min idle; 5-min privileged reauth window; TOTP required for Local GLOBAL_ADMIN, configurable for others; reset via emailed single-use token, 30-min expiry; no pre-created production Local account. (D-235)
5. [DECIDED] Microsoft Graph manager/org synchronization is deferred to V2. V1 organization/team/manager data can be maintained by authorized admins.
6. [DECIDED] Global Admin and DR Coordinator both have full live DR lifecycle authority. Application/System Owners may **create PLANNED** DR Events scoped to their own Application(s) (subset/sub-DR) but cannot `activate`, `start-failover` or any later lifecycle command unless they also hold Admin/Coordinator authority. (D-213)
7. [DECIDED] There is no standalone Validator role in V1. Application Owner validates application work; Work Stream Lead validates shared work.
8. [DECIDED] Default DR visibility is limited to DR Event participants. Within an Event, participants have broad cross-team read visibility. Participants are an explicit `dr_event_participants` table. A User is auto-enrolled when they: hold an Event-scoped role; are current Task assignee; are Blocker owner; are any-slot System/Application or Business Owner of an in-scope Application; lead a Work Stream; are member or manager of an Owning Team with Tasks in the Event. Global Admins are implicit participants of every Event. Auditor/Executive get a separately granted global read-only role or per-Event enrolment — never by job title. (D-222)
9. [DECIDED] Write access is server-side scoped by role, Team, Work Stream, Application and Event.
10. [DECIDED] Managers may reassign only members of their own Team. DR Coordinators may reassign across Teams.
11. [DECIDED] Same-Team members may reassign eligible Team work to one another.
12. [DECIDED] A user may volunteer for eligible unassigned cross-Team work; the Task Owning Team does not change.
13. [DECIDED] Owning Team represents accountability; Current Assignee/Executing Resource may differ.
14. [DECIDED] **Manager precedence is an explicit domain override, not a 409.** When a Team Manager submits an assignment for a Task owned by their Team with a stale `expected_version` and the intervening change was an assignment by a DR Coordinator/Global Admin, the Manager's assignment is **accepted** (version increments), both actions are audited, the conflict resolution is recorded as `MANAGER_PRECEDENCE`, and affected users are notified. All other stale writes return `409 CONCURRENCY_CONFLICT`. (D-214; supersedes the earlier "latest valid committed update is current" wording)
15. [DECIDED] High-risk actions require reauthentication/MFA policy. Seed high-risk actions include Start DR, Start Failback, Close/Cancel DR, force/override, report publish, package export/import, privileged role/policy changes and local-admin creation.
16. [DECIDED] No dual-control/second-person approval is required in V1.

## 5. Application ownership and contacts

1. [DECIDED] Application/System Owners support up to three slots: Primary required; Secondary and Tertiary optional.
2. [DECIDED] Business Owners support up to three slots: Primary required; Secondary and Tertiary optional.
3. [DECIDED] Contact methods in V1 are email and phone.
4. [DECIDED] Phone is contact information only; V1 has no SMS or voice-calling integration.
5. [DECIDED] User skills/capabilities are in V1; formal skill proficiency scoring is deferred to V2.

## 6. Canonical DR Event lifecycle

1. [DECIDED] Canonical V1 DR Event states are: `PLANNED`, `ACTIVE`, `FAILOVER_IN_PROGRESS`, `FAILED_OVER`, `FAILBACK_IN_PROGRESS`, `CLOSED`, `CANCELLED`.
2. [DECIDED] `CANCELLED` is a terminal unsuccessful/aborted outcome distinct from successfully `CLOSED`.
3. [DECIDED] `CLOSED` and `CANCELLED` are operationally terminal in V1; corrections happen through audit/report addenda, not reopening execution.
4. [DECIDED] Reusable Plan is a versioned template. A DR Event instantiates/copies it; live Event edits do not mutate the source Plan.
5. [DECIDED] A baseline snapshot is captured at DR start/network cut; live execution remains editable and auditable afterward.
6. [DECIDED] Comprehensive DRs may contain independent child/sub-DR Events that roll up to the parent. Parent DR Event = reporting/coordination container. Children own their lifecycle, `network_cut_at`, RTO/RPO, failover/failback and report. Parent aggregates all descendant DR Applications for health/treemap/reporting, never mutates child state, and cannot `CLOSE` while any non-cancelled child is non-terminal. Standalone subset-Application Events need no parent. (D-219)
7. [DECIDED] A DR Event can also be created for only a selected subset of Applications.
8. [DECIDED] V1 stores planned dates; calendar invitations/recurrence are P2/P3.
9. [DECIDED] V1 uses readiness gates, not a separate formal pre-DR approval chain.
10. [DECIDED] `POST /dr-events/{id}/start-failover` accepts optional `network_cut_at` with `activated_at ≤ network_cut_at ≤ now()`; defaults to server `now()`; applies to PLANNED_DR and REAL_INCIDENT; audited. (D-237)

## 7. Canonical Task, Blocker and Issue model

1. [DECIDED] Canonical Task states are: `NOT_STARTED`, `IN_PROGRESS`, `BLOCKED`, `READY_FOR_VALIDATION`, `COMPLETED`, `CANCELLED`. These are final. `BLOCKED` is backed by ≥1 structured Blocker; on blocker verify/close the Task returns to `IN_PROGRESS` (its prior actionable state) and proceeds to `READY_FOR_VALIDATION` only through an explicit `submit-validation`. (D-252)
2. [DECIDED] Acknowledged is not a required Task state.
3. [DECIDED] My DR still contains a **Ready** tile; Ready is derived from `NOT_STARTED` Tasks whose hard dependencies/manual gates are satisfied, not a separate Task lifecycle state.
4. [DECIDED] `BLOCKED` is a Task state and a structured Blocker record stores reason, routing, responsible Team/User, timestamps, escalation and resolution history. Blocker keeps six states. Commands: `assign → ASSIGNED`, `start → IN_PROGRESS`, `resolve → RESOLVED`, `verify → VERIFIED` then `CLOSED` atomically (both transitions audited). `POST /blockers/{id}/start` is added to API_CONTRACT. Owning Team accountability never changes. (D-211)
5. [DECIDED] Separate `Issue/Finding` entity exists in V1 for non-blocking observations, risks and deviations.
6. [DECIDED] Task completion requires required evidence plus an explicit verification/validation note where applicable; users cannot simply mark critical work complete without proof/attestation. Task carries `evidence_required BOOLEAN NOT NULL DEFAULT TRUE`, `evidence_min_count SMALLINT NOT NULL DEFAULT 1`, `verification_note_required BOOLEAN NOT NULL DEFAULT TRUE` (also in plan/import task representation). Normal completion = work done + ≥1 acceptable Evidence Item (attachment/log/screenshot/link/text per policy) + verification note + Owner/Lead validation. Policy may relax evidence for a class of Tasks; default is required. (D-226)
6a. [DECIDED] Every Task reaches `COMPLETED` only via `READY_FOR_VALIDATION → validate`. Application-scoped work is validated by an Application/System Owner (any of the up-to-3 slots); shared Work Stream work by the Work Stream Lead. `needs_specific_validation=false` means standard owner/lead validation applies; `true` means the Task additionally carries task-specific validation criteria/evidence requirements. Executors can never self-complete. Global Admin/DR Coordinator may validate per RBAC. (D-209)
6b. [DECIDED] Validation is a first-class persisted record (`validations` table) but has no separate REST resource in V1. `POST /tasks/{id}/submit-validation` creates it (PENDING); `POST /tasks/{id}/validate` and `POST /dr-applications/{id}/validate` close it (APPROVED | REJECTED). `IN_REVIEW` and `REMEDIATION` remain in the enum, reserved, unused in V1. A rejected Task returns to `IN_PROGRESS`. (D-210)
7. [DECIDED] A Task may directly reference both a DR Application and a Work Stream.
8. [DECIDED] Dependencies support `HARD` and `ADVISORY` semantics in V1. HARD blocks progress; ADVISORY warns but allows continuation.
9. [DECIDED] Self dependencies and directed dependency cycles are invalid.
10. [DECIDED] V1 forecast is deterministic/rules-only based on remaining HARD dependency work and expected durations; no ML forecast.
11. [DECIDED] One queue per Team plus tags is sufficient in V1.
12. [DECIDED] An owning Team must accept/review routed work. If it is misrouted, it is explicitly rerouted/reassigned with audit history; there is no terminal Declined state.
13. [DECIDED] Milestones remain first-class gates; critical milestones require manual confirmation by default.

## 8. Failback

1. [DECIDED] Failback has its own Plan/Tasks/Dependencies and is never assumed to be failover in reverse.
2. [DECIDED] Failback is required by default for coordinated DR Applications.
3. [DECIDED] A feature toggle permits `failback_required=false` for explicit cases.
4. [DECIDED] Global Admin or DR Coordinator explicitly starts failback.
5. [DECIDED] Failback uses the same style of manual gates/handoffs as failover.
6. [DECIDED] If failback is not required, the Application still reaches the common terminal completion outcome and reporting records `failback_required=false`.

## 9. RTO, RPO, SLA and timing

1. [DECIDED] V1 tracks both RTO and RPO per DR Application.
2. [DECIDED] Tier default recovery targets: Tier 0 = 30m; Tier 1 = 60m; Tier 2 = 120m; Tier 3 = 240m; Tier 4 = 1440m.
3. [DECIDED] Regional DR recovery timing begins at the common network-cut timestamp.
4. [DECIDED] RTO/SLA stops only after successful technical Application validation.
5. [DECIDED] A recorded breach remains a breach after late recovery.
6. [DECIDED] RPO stores target plus pre-DR data snapshot/reference timestamp, recovered-data timestamp/data-loss result and pass/fail.
7. [DECIDED] Pre-DR baseline/snapshot participates in RPO reporting.
8. [DECIDED] SLA/RTO breach creates an alert requiring acknowledgement and a reason; it does not automatically freeze unrelated work.
9. [DECIDED] SLA warning thresholds are Admin/Coordinator configurable by percentage and/or absolute remaining time.
10. [DECIDED] Timestamps are stored in UTC. Each DR Event has an explicit Event timezone. UI supports toggle between Event timezone and user's local timezone.
11. [DECIDED] Policy keys/defaults: `sla.warning_percent=85`; `sla.warning_minutes_remaining=null`; `blocker.escalation_minutes.{T0=0,T1=15,T2=30,T3=60,T4=120}`; `dependency.edit_requires=SCOPED_ROLE` (alt `COORDINATOR_ONLY`); `milestone.auto_confirm_allowed=false`; `ai.control_profile=CONSERVATIVE`; `retention.audit_years=7`; `retention.event_years=3`; `retention.evidence_years.default=3`; `file.max_mb=100`; `file.allowlist=[pdf,png,jpg,jpeg,txt,log,csv,xlsx,docx,zip,json]`; `notifications.email.enabled=true`; `notifications.teams.enabled=false`; `notifications.slack.enabled=false`; `readiness.*` per item 12 below; `application.auto_submit_validation=false` (when `true`, `POST /dr-applications/{id}/submit-validation` auto-fires once all phase Tasks of the Application are COMPLETED) (I-3, plan §10). All uploads still undergo MIME/content validation and malware scanning. SMTP credentials live in secret storage, never policy JSON. (D-225)
12. [DECIDED] Readiness catalog (each `HARD_STOP | WARNING | OFF`, globally configurable, Coordinator override only where policy permits and always with reason): failback plan exists for every `failback_required=true` DR App — HARD_STOP; every critical manual Milestone has an owner — HARD_STOP; every in-scope App has a Primary System Owner — HARD_STOP; Primary Business Owner present — WARNING; every Work Stream has a Lead — WARNING; every Task has an Owning Team — HARD_STOP; dependency graph acyclic — HARD_STOP and **not configurable**; ≥1 MONITORING Work Stream Task — WARNING; Event timezone set — HARD_STOP; unresolved import/AI Needs Review — WARNING; DR Coordinator assigned — HARD_STOP; ≥1 DR Application in scope — HARD_STOP; every DR App has RPO target or is explicitly RPO-N/A — HARD_STOP. (D-224)

## 10. Health model and treemap

1. [DECIDED] Tier health weights: Tier 0 = 100; Tier 1 = 75; Tier 2 = 50; Tier 3 = 30; Tier 4 = 20.
2. [DECIDED] Application health formula: 70% Task score + 30% Validation score. Exact: Task Score = 100 × COMPLETED eligible Tasks / eligible Tasks (eligible = non-CANCELLED; no extra Blocked penalty). Validation Score = 100 if the Application's required technical validation for the current recovery phase passed (failback validation when failback is active), else 0. Application Health = 0.70 × Task Score + 0.30 × Validation Score. (D-223)
3. [DECIDED] Overall DR health is the tier-weighted aggregation of Application health. Exact: Event Health = Σ(App Health × effective tier weight) / Σ(effective tier weight); weights T0–T4 = 100/75/50/30/20. Zero-Application Event: dial displays "—", not 100. (D-223)
4. [DECIDED] There is **no Tier 0 hard cap** on the overall dial. The earlier cap idea was rejected.
5. [DECIDED] Overall dial bands: GREEN ≥ 85; YELLOW 60–84.99; RED < 60. (D-223)
6. [DECIDED] Application treemap remains Green → Light Orange → Dark Orange → Red and uses tile size/criticality plus color intensity to make Tier 0/Tier 1 failures visually dominant. Treemap bands: GREEN ≥ 85; LIGHT_ORANGE 60–84.99; DARK_ORANGE 40–59.99; RED < 40. (D-223)
6a. [DECIDED] Overlays only worsen the visual band: RTO breach → RED; RPO breach → RED; SLA/RTO warning reached → ≥ DARK_ORANGE; active Blocker on T0/T1 App → ≥ DARK_ORANGE; active Blocker on T2–T4 App → ≥ LIGHT_ORANGE. Overlays never change the numeric dial. (D-223)
7. [DECIDED] AI must be able to explain the exact dial value and major contributors.

## 11. AI and semantic search

1. [DECIDED] V1 AI integration is provider-agnostic.
2. [DECIDED] V1 supports Synthetic API credentials and direct Anthropic API credentials.
3. [DECIDED] Synthetic is the first AI provider; initial chat model **Kimi K3** if available on the account at implementation time; other Synthetic models (GLM family) and direct Anthropic supported. Model IDs are Admin-configurable, verified at BUILD-16 (AI increment), never hard-coded in domain code. Credentials never committed. (D-249)
4. [DECIDED] Azure AI Foundry integration is deferred to V2.
5. [DECIDED] Provider, model, endpoint and secret are Admin-configurable; production secrets are stored in Key Vault and never sent to the browser.
5a. [DECIDED] AI Act allowlist: task start/block/resume/submit-validation/assign/volunteer; blocker assign/start/resolve; milestone confirm; issue create/review/resolve; comment create; alert acknowledge/snooze. **Never via AI**: any Event lifecycle command, Task/App validation, report publish, package export/import, role/policy/admin/security configuration, local-user creation. (D-228)
5b. [DECIDED] AI control profiles: CONSERVATIVE = confirm every Act with diff preview; BALANCED = explicit confirmation for cross-Team changes and Tier 0/1 work, one batch confirmation for own-scope low-risk; FAST_EXECUTION = own-scope low-risk allowlisted actions execute without per-action reconfirmation. RBAC/idempotency/guards/audit always apply; excluded commands stay excluded in every profile. (D-228)
6. [DECIDED] Embeddings are decoupled from the chat model.
7. [DECIDED] Default embedding model is `nomic-embed-text-v1.5`, 768 dimensions, configurable.
8. [DECIDED] Changing embedding model/dimension requires re-embedding indexed content.
9. [DECIDED] AI summaries ship in V1 behind a feature flag.
10. [DECIDED] AI supports Read, Recommend and Act. Act invokes the exact same command/transition services, RBAC, override, reauth and audit controls as manual operation.
11. [DECIDED] Retrieved AI content is untrusted data, never instruction authority.
12. [DECIDED] Permission filtering occurs before retrieval/context construction.
13. [DECIDED] Do not place secrets in model prompts. Log provider/model/request metadata without sensitive prompt bodies by default.
14. [DECIDED] AI provider use is environment opt-in and Admin-selected.
15. [DECIDED] Speech-to-text remains V1, provider-agnostic/configurable. Dictated text is reviewable/editable before submission.
16. [DECIDED] Search is structured filters + PostgreSQL full text + pgvector semantic retrieval.
17. [DECIDED] Structured dependency mappings are authoritative; semantic document evidence supplements them and is cited.
18. [DECIDED] Every critical workflow must pass end-to-end with AI disabled.

## 12. Notifications and collaboration

1. [DECIDED] In-app operational notifications are V1 core.
2. [DECIDED] Email is functional in V1 and configurable through the Admin portal.
3. [DECIDED] V1 email uses a provider abstraction with SMTP first; Gmail-compatible/test SMTP is acceptable for testing.
4. [DECIDED] Email delivery failure never rolls back a committed DR action; failures are logged/retried.
5. [DECIDED] Teams and Slack are not functional V1 integrations. The UI may show disabled/Coming Soon options.
6. [DECIDED] Runbooks may contain external communication instructions/links.
7. [DECIDED] No public/external status page in V1; reconsider in V2.
8. [DECIDED] @mentions are V1.
9. [DECIDED] Personal alert snooze remains separate from global acknowledgement.

## 13. Evidence, file security, packages and retention

1. [DECIDED] Evidence supports text/verification note, file, screenshot, log and external link.
2. [DECIDED] Upload security includes Admin-configurable file allowlist, 100 MB default maximum, production malware scanning in Azure, and a local/mock scanning path for development. Default allowlist: `[pdf,png,jpg,jpeg,txt,log,csv,xlsx,docx,zip,json]` (`file.allowlist`), `file.max_mb=100`; all uploads still undergo MIME/content validation and malware scanning. (D-225)
3. [DECIDED] Full DR packages use AES-256-GCM authenticated encryption plus integrity metadata. A random 256-bit DEK is wrapped by an Azure Key Vault key (prod) or env-provided dev key (local); no plaintext key in package. (D-230)
4. [DECIDED] Production keys live in Key Vault.
5. [DECIDED] No dual-control package export/import approval in V1.
6. [DECIDED] Package manifest includes company/tenant binding; cross-tenant restore is rejected by default. Manifest = `{package_version, tenant_id, dr_event_id, exported_at, exported_by, schema_version, content_hash (SHA-256), entity_counts}`. `tenant_id` = Entra tenant GUID in Azure, `DRCC_TENANT_ID` config locally. Dev→Test import allowed when tenant IDs match. (D-230)
7. [DECIDED] Report outputs include in-app report, PDF, CSV and Excel detailed exports.
8. [DECIDED] RACI export is included in V1 in PDF/CSV form.
9. [DECIDED] Final retention defaults supersede the earlier 90-day discussion: Audit = 7 years; DR Events/Tasks = 3 years; Evidence = configurable by classification, default 3 years; legal hold suspends deletion.
10. [DECIDED] Reports/imports/semantic indexes align to their source Event/Evidence retention unless policy specifies longer retention.
11. [DECIDED] Model/embedding changes trigger re-embedding where indexed source is still retained.
12. [DECIDED] Retention tests use a fake clock/short configuration; tests never wait real years.

## 14. Needs Review, Saved Views and Issue lifecycle

1. [DECIDED] Needs Review states: `OPEN`, `IN_REVIEW`, `RESOLVED`, `DISMISSED`.
2. [DECIDED] Dismissal requires note/audit just like resolution requires accountable action.
3. [DECIDED] Shared Saved Views are live-linked definitions and always reapply current access controls.
4. [DECIDED] Issue/Finding is separate from Blocker so non-blocking observations can be tracked without falsely stopping work.

## 15. World-class UI/UX requirements

1. [DECIDED] V1 must be designed in Figma first and receive formal UI/UX sign-off before core interface implementation is treated as approved.
1a. [DECIDED] Figma is not complete. A **Design System / Figma Gate** milestone precedes visually final Command Center work. BUILD-01…11 proceed; the web shell (layout, auth shell, nav, theme, responsive grid, design-token plumbing, generic Deep Card framework) proceeds; BUILD-12/13 and visual-heavy parts of later UI increments require Figma sign-off. Use the Figma integration if available, else manual token export. Figma deliverables: Command Center, My DR, Application deep view, Task Board, Task deep view, People/resource views, Needs Review, Admin, Reports, Light+Dark, tablet/mobile states, hover/deep-card behaviour, loading/empty/error states. (D-231)
1b. [DECIDED] Route map — UI_UX.md is authoritative, amended: top-level `/plans` and `/plans/[planId]`; both `/people` (operational/resource) and `/admin/users-teams` (identity/team admin); `/dr-events/[eventId]/reviews` (Needs Review) and `/dr-events/[eventId]/resources`; historical comparison is a tab on `/applications/[applicationId]` (no `/history`); `/admin/notifications` with Email functional and Teams/Slack disabled "Coming Soon". Master §15.3 `/dr/...` routes are superseded. (D-212)
2. [DECIDED] A clickable prototype of core flows is a design gate.
3. [DECIDED] **10-second comprehension rule:** a Coordinator should be able to identify what is failing and what requires action within 10 seconds of opening the live command surface.
4. [DECIDED] WCAG 2.1 AA is mandatory.
5. [DECIDED] Light and Dark modes ship in V1; preference persists.
6. [DECIDED] POC certification targets latest Chrome and Edge.
7. [DECIDED] Desktop is the primary Command Center surface; core views remain responsive on tablet/phone.
8. [DECIDED] Coordinator view includes weighted health dial, Application treemap, resource sidebar, critical Blockers/Issues, live phase/timeline, AI chat and RTO/RPO risk.
9. [DECIDED] My DR includes Ready (derived), In Progress, Blocked, SLA/RTO Risk, Needs My Confirmation and Completed tiles.
10. [DECIDED] Kanban/swimlane, labels, filters and live Saved Views ship in V1.
11. [DECIDED] Hover + Show More deep-card interaction is consistent across people, Applications, Tasks, Blockers, Issues and Event summaries.
12. [DECIDED] Application detail includes failover, failback, RTO, RPO, evidence, dependencies, validation and audit history.

## 16. Performance, testing and POC acceptance

1. [DECIDED] POC load certification dataset: 500 Applications and 5,000 Tasks.
2. [DECIDED] POC concurrency target: 200 concurrent users.
3. [DECIDED] Normal API operations target average latency below 500 ms under expected POC load.
4. [DECIDED] Primary dashboards should become usable within approximately a couple seconds.
5. [DECIDED] Live-state propagation acceptance target: <= 10 seconds from committed state change to connected dashboard visibility.
6. [DECIDED] Idempotency-key retention: 24 hours.
7. [DECIDED] POC must demonstrate one complete DR from planning through failover, RTO/RPO validation, failback, closure, audit and report.
8. [DECIDED] Audit coverage is required for all privileged/state-changing actions.
9. [DECIDED] AI-disabled E2E path must pass.
10. [DECIDED] Backup/restore and rollback drills must pass.
11. [DECIDED] WCAG and UI/UX sign-off are release gates.
12. [DECIDED] Test stack: pytest + testcontainers-python (ephemeral pgvector Postgres per session), Vitest + Testing Library, Playwright, k6; ephemeral Redis or isolated test compose profile for Celery integration; no dependence on a developer's shared DB. (D-250)
13. [DECIDED] CI security toolchain: Semgrep, Bandit, pip-audit, pnpm audit, gitleaks, Syft (SBOM), Grype, Checkov (Bicep). No GHAS assumed; if GHAS appears, add CodeQL, Dependabot, secret scanning. Gate: no unresolved Critical. (D-246)
14. [DECIDED] `packages/contracts` is generated from FastAPI OpenAPI with `openapi-typescript`; CI fails on drift; Zod only for UI/form/runtime validation, never a hand-copied backend contract. (D-251)

## 17. Operations and support

1. [DECIDED] V1 support model is business-hours support plus on-call coverage during scheduled/live DR exercises/incidents; no permanent 24/7 V1 requirement.
2. [DECIDED] Vulnerability remediation targets: Critical 7 days; High 30; Medium 60; Low 90.
3. [DECIDED] Monthly maintenance cycle with emergency exceptions.
4. [DECIDED] CI/CD includes tests, migration validation, SAST, dependency scanning, SBOM generation and container-image scanning.
4a. [DECIDED] Repository is **private**; no GHAS assumed. CI security stack per §16.13 (Semgrep, Bandit, pip-audit, pnpm audit, gitleaks, Syft, Grype, Checkov). (D-246)
5. [DECIDED] Release is blocked by known Critical vulnerabilities unless an explicit emergency exception is recorded.
6. [DECIDED] FinOps budgets and cost alerts are required.
7. [DECIDED] Structured logs flow to Log Analytics/Application Insights now; Sentinel integration is later.

## 18. V2/V3 reserved capabilities

1. [DECIDED] ServiceNow / Remedy / other ITSM/CMDB synchronization.
2. [DECIDED] Monitoring/ITSM alert suppression and restoration during approved DR windows.
3. [DECIDED] Microsoft Graph org/manager synchronization.
4. [DECIDED] Functional Teams and Slack integrations.
5. [DECIDED] Azure AI Foundry provider integration.
6. [DECIDED] Formal compliance-framework mapping/certification.
7. [DECIDED] Calendar invites/recurrence engine.
8. [DECIDED] Public/external status page only if a later need is established.
9. [DECIDED] Skill proficiency scoring.
10. [DECIDED] Sophisticated ML-based forecasting/prediction if deterministic V1 data later justifies it.
11. [DECIDED] Optional mandatory Business Owner validation gates for selected Applications.
12. [DECIDED] Potential multi-tenant SaaS and on-prem deployment models.

## 19. Explicit superseded/rejected alternatives

1. [DECIDED] The earlier Azure-managed relational-store suggestion was replaced by PostgreSQL for local/cloud parity.
2. [DECIDED] Pinecone and Chroma were rejected as unnecessary V1 infrastructure.
3. [DECIDED] Keyword-only search was rejected.
4. [DECIDED] A mandatory Acknowledged Task state was rejected.
5. [DECIDED] Automatically reversing failover to create failback was rejected.
6. [DECIDED] A separate mandatory Business Owner approval gate was rejected for V1.
7. [DECIDED] Universal manager approval for dependency edits was rejected because of the company's flatter working structure; policy controls it instead.
8. [DECIDED] Static manager-entered capacity was rejected as the primary resource-load model.
9. [DECIDED] The earlier proposal to change Task Owning Team when a cross-Team engineer helps was replaced by fixed Owning Team + floating Current Assignee.
10. [DECIDED] The earlier idea of a Tier 0 hard cap on the global health dial was rejected; criticality is expressed through weighting and treemap size/color.
11. [DECIDED] Earlier 90-day evidence retention default was superseded by the final 3-year classified-evidence default.
12. [DECIDED] Earlier functional Teams notification consideration was superseded: only email is functional V1; Teams/Slack are Coming Soon.
13. [DECIDED] Earlier Azure AI Foundry V1 model suggestion was superseded by provider-agnostic Synthetic/Anthropic V1; Foundry is V2.
14. [DECIDED] Earlier suggestion to defer speech-to-text was rejected; speech-to-text remains V1.
15. [DECIDED] Earlier Task state variants are superseded by the canonical V1 Task lifecycle in §7 of this file.
16. [DECIDED] Earlier DR Event state variants are superseded by the canonical V1 Event lifecycle in §6 of this file.
17. [DECIDED] Earlier "MinIO-compatible local object storage" (§2.11, bundle rules file) is superseded by Azurite locally with one `ObjectStore` adapter on `azure-storage-blob`. (D-217)
18. [DECIDED] Earlier discovery lean toward WeasyPrint for PDF is superseded by Playwright/Chromium print in the background worker. (D-244)
19. [DECIDED] Earlier schema_v1.sql `dr_application_status` enum (`NOT_STARTED, IN_PROGRESS, RECOVERED, VALIDATING, FAILBACK_IN_PROGRESS, RESTORED`) is superseded by `NOT_STARTED → RECOVERING → TECHNICAL_VALIDATION → FAILED_OVER → FAILBACK_IN_PROGRESS → COMPLETED`; `COMPLETED` is the common terminal state; with `failback_required=false` a validated Application moves `TECHNICAL_VALIDATION → COMPLETED` directly; RTO clock stops on `TECHNICAL_VALIDATION → FAILED_OVER` (or `→ COMPLETED`). (D-208)

## 20. Freeze rule

[DECIDED] There are **zero blocking product questions for V1 implementation**. Remaining implementation tunables must be represented as configuration or ADR-level engineering choices and may not reopen settled business behavior without an explicit product-change decision. This file and FREEZE_ADDENDUM.md D-201…D-261 are equally frozen. (D-201)

## 21. Delivery process

1. [DECIDED] Branches: `feat/<milestone>-<slug>` (e.g. `feat/build-06-tasks-dependencies`) and `fix/<slug>`. `main` is protected; squash-merge only; linear history; tag `v0.NN.0` after each merged BUILD-NN. Conventional Commits. (D-202)
2. [DECIDED] The 24 BUILD-NN increments are the PR/milestone units. Large increments run as internal sessions (`BUILD-06a/06b/06c`) with handoff notes and merge as one PR after the full BUILD acceptance passes. (D-203)
3. [DECIDED] Solo implementation with the development toolchain. An independent second-opinion review is mandatory for BUILD-06, 08, 10, 16, 20 and opportunistic for security/auth/schema/dependency-transition changes. (D-204)
4. [DECIDED] Docs layout: `/docs/spec/` (frozen + reconciled canon, protected read-only on code branches by CI and local guards), `/docs/plan/` (build plan, playbook — outside version control, D-262), `/docs/adr/`, `/docs/build-prompts/` (BUILD-01…24), `/docs/reviews/` (security/UX/architecture/review-board outputs). Local guards and the CI `spec-frozen` check prevent `/docs/spec/**` from being edited on code branches (D-205, D-262).
5. [DECIDED] The 10-week POC clock starts when BUILD-01 actually begins. No external DR exercise date constrains V1. Production hardening is a separate phase after week 10. (D-206)

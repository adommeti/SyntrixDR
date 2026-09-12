# DR Command Center — Freeze Addendum (Phase 1 Clarifications)

**Date:** 2026-09-12  
**Status:** AUTHORITATIVE — Tier 1 alongside FROZEN_DECISIONS.md  
**Source:** Product owner's written answers to `docs/plan/PHASE1_DISCOVERY.md` (51 questions)

## Precedence after this addendum

1. This addendum (`D-2nn` records below)
2. `FROZEN_DECISIONS.md` (reconciled)
3. Canonical companions (reconciled): `STATE_MACHINES.md`, `DATA_MODEL.md`, `schema_v1.sql` + `schema_v2_reconciliation.sql`, `API_CONTRACT.md`, `RBAC_MATRIX.md`, `UI_UX.md`, `ARCHITECTURE.md`, `SECURITY_REVIEW.md`, `BUILD_PROMPTS.md`, `TEST_STRATEGY.md`, `OPERATIONS.md`
4. `reference/MASTER_BUILD_SPEC_FROZEN.md` — provenance/detail only
5. Pre-freeze/archived material — never implementation authority

Every record below is `[DECIDED]`. Each cites the discovery question it resolves (Q) and the gap it closes (C/M/E/P). Where a record supersedes frozen wording, the superseded text is named so the reconciled doc can be checked against it.

---

## A. Repo, harness, process

- **D-201** (Q01, P-01) New GitHub repository, started clean. The pre-freeze tooling scaffold is reference-only; its mechanics (guard scripts, verify gate, review roles, helper scripts) are ported and re-based on the frozen bundle. Its `# OPEN(§n)` seam protocol, 16-build structure, stale enums and unresolved-question assumptions are **not** inherited. The new repo must not depend on where the old scaffold lives.
- **D-202** (Q02, C-12) Branches: `feat/<milestone>-<slug>` (e.g. `feat/build-06-tasks-dependencies`) and `fix/<slug>`. `main` is protected; squash-merge only; linear history; tag `v0.NN.0` after each merged BUILD-NN. Conventional Commits.
- **D-203** (Q03, P-03) The 24 BUILD-NN increments are the PR/milestone units. Large increments run as internal sessions (`BUILD-06a/06b/06c`) with handoff notes and merge as one PR after the full BUILD acceptance passes.
- **D-204** (Q48, P-05) Solo implementation with the development toolchain. An independent second-opinion review is mandatory for BUILD-06, 08, 10, 16, 20 and opportunistic for security/auth/schema/dependency-transition changes.
- **D-205** (Q49, P-07) Docs layout: `/docs/spec/` (frozen + reconciled canon, protected read-only on code branches by CI and local guards), `/docs/plan/` (build plan, playbook — kept outside version control, see D-262), `/docs/adr/`, `/docs/build-prompts/` (BUILD-01…24 — outside version control, see D-262), `/docs/reviews/` (security/UX/architecture/review-board outputs). Local guards and the CI `spec-frozen` check prevent `/docs/spec/**` from being edited on code branches.
- **D-206** (Q50, P-08) The 10-week POC clock starts when BUILD-01 actually begins. No external DR exercise date constrains V1. Production hardening is a separate phase after week 10.

## B. Canonical contradictions resolved

- **D-207** (Q04, C-01) `API_CONTRACT.md` is authoritative. **Not V1**: Event Pause/Resume, Task Skip, `/tasks/{id}/complete`, generic `/validations/{id}/approve|reject`, generic `/ai/chat`. Completion happens only through the canonical Task validation transition. Event cancellation exists; pause does not.
- **D-208** (Q05, C-02) DR Application status enum — **supersedes** schema_v1.sql `dr_application_status` (`NOT_STARTED, IN_PROGRESS, RECOVERED, VALIDATING, FAILBACK_IN_PROGRESS, RESTORED`):
  `NOT_STARTED → RECOVERING → TECHNICAL_VALIDATION → FAILED_OVER → FAILBACK_IN_PROGRESS → COMPLETED`.
  `COMPLETED` is the common terminal state. `failback_required` defaults `true`. With the toggle set `false`, a successfully validated Application moves `TECHNICAL_VALIDATION → COMPLETED` directly (skipping FAILED_OVER/FAILBACK_IN_PROGRESS). RTO clock stops on the `TECHNICAL_VALIDATION → FAILED_OVER` (or `→ COMPLETED`) transition.
- **D-209** (Q06, C-03) Every Task reaches `COMPLETED` only via `READY_FOR_VALIDATION → validate`. Application-scoped work is validated by an Application/System Owner (any of the up-to-3 slots); shared Work Stream work by the Work Stream Lead. `needs_specific_validation=false` means *standard* owner/lead validation applies; `true` means the Task additionally carries task-specific validation criteria/evidence requirements. Executors can never self-complete. Global Admin/DR Coordinator may validate per RBAC.
- **D-210** (Q07, C-04) Validation is a first-class **persisted** record (`validations` table) but has **no** separate REST resource in V1. `POST /tasks/{id}/submit-validation` creates it (PENDING); `POST /tasks/{id}/validate` and `POST /dr-applications/{id}/validate` close it (APPROVED | REJECTED). `IN_REVIEW` and `REMEDIATION` remain in the enum, reserved, unused in V1. A rejected Task returns to `IN_PROGRESS`.
- **D-211** (Q08, C-05) Blocker keeps six states. Commands: `assign → ASSIGNED`, `start → IN_PROGRESS`, `resolve → RESOLVED`, `verify → VERIFIED` then `CLOSED` atomically (both transitions audited). `POST /blockers/{id}/start` is **added** to API_CONTRACT. Owning Team accountability never changes.
- **D-212** (Q09, C-06) Route map — UI_UX.md is authoritative, amended: add top-level `/plans` and `/plans/[planId]`; keep both `/people` (operational/resource) and `/admin/users-teams` (identity/team admin); add `/dr-events/[eventId]/reviews` (Needs Review) and `/dr-events/[eventId]/resources`; historical comparison is a tab on `/applications/[applicationId]` (no `/history`); keep `/admin/notifications` with Email functional and Teams/Slack disabled "Coming Soon". Master §15.3 `/dr/...` routes are superseded.
- **D-213** (Q10, C-07) Global Admin and DR Coordinator hold full Event lifecycle authority. Application/System Owners may **create PLANNED** DR Events scoped to their own Application(s) (subset/sub-DR) but cannot `activate`, `start-failover` or any later lifecycle command unless they also hold Admin/Coordinator authority.
- **D-214** (Q11, C-08) **Manager precedence is an explicit domain override, not a 409.** When a Team Manager submits an assignment for a Task owned by their Team with a stale `expected_version` and the intervening change was an assignment by a DR Coordinator/Global Admin, the Manager's assignment is **accepted** (version increments), both actions are audited, the conflict resolution is recorded as `MANAGER_PRECEDENCE`, and affected users are notified. All other stale writes return `409 CONCURRENCY_CONFLICT`.
- **D-215** (Q12, C-09) `Idempotency-Key` header is **required on every command POST** (400 `IDEMPOTENCY_KEY_REQUIRED` if missing). Retained 24 h. GET/queries exempt. A pure pre-signed-upload handshake may be exempt, but the domain command attaching the artifact requires it.
- **D-216** (Q13, C-10) Canonical polymorphic target enum `target_type`: `DR_EVENT, DR_APPLICATION, APPLICATION, WORK_STREAM, TASK, TASK_DEPENDENCY, MILESTONE, BLOCKER, ISSUE_FINDING, VALIDATION, IMPORT_JOB, PLAN, PLAN_VERSION, REPORT, DOCUMENT, ALERT`. Used by comments, evidence_items, overrides, needs_review_items, alerts, notifications, audit links and `packages/contracts`. Each table may accept only a subset (enforced by CHECK/service).
- **D-217** (Q14, C-11) **Azurite** locally, Azure Blob Storage in Azure, one `ObjectStore` adapter on `azure-storage-blob`. **Supersedes** FROZEN §2.11 "MinIO-compatible local object storage" and the bundle rules file "Azure Blob / MinIO-compatible".
- **D-218** (Q15, C-13) (a) Correct the stale Tier-3 comment in schema_v1.sql; Tier 3 weight = 30. (b) `user_skills.proficiency` stays nullable and unused in V1. (c) Migration 0002 adds `local_credentials, sessions, reauth_grants, idempotency_keys, outbox_events, dr_event_participants, file_policy` plus indexes/constraints; schema_v1.sql is not otherwise mutated.
- **D-219** (Q16, C-14) Parent DR Event = reporting/coordination container. Children own their lifecycle, `network_cut_at`, RTO/RPO, failover/failback and report. Parent aggregates all descendant DR Applications for health/treemap/reporting, never mutates child state, and cannot `CLOSE` while any non-cancelled child is non-terminal. Standalone subset-Application Events need no parent.

## C. Missing specifications resolved

- **D-220** (Q17, M-01) Regenerate from the reconciled canon (never re-upload pre-freeze copies): `.env.example`, `docker-compose.yml`, `Makefile`, `VERIFY.md`, `ADRS.md`, `GLOSSARY.md`, `TRACEABILITY_MATRIX.md`, `OPEN_QUESTIONS.md` (must state zero blocking product questions after this addendum), `infra/BICEP_PLAN.md`.
- **D-221** (Q18, M-02) No rigid Excel format. Importer requires a **mapping/review step** supporting arbitrary headers, preserves row provenance (`source_import_id`, `source_import_row`), and allows AI or manual mapping. Fixture columns (test-only, not mandatory customer headers): `Phase, Work Stream, Application, Tier, Task, Subtask / Procedure, Owning Team, Assignee, Expected Duration, Predecessor / Dependency, Failover / Failback, Evidence Required, Notes`.
- **D-222** (Q19, M-03) Explicit `dr_event_participants` table. Auto-enrol a User when they: hold an Event-scoped role; are current Task assignee; are Blocker owner; are any-slot System/Application or Business Owner of an in-scope Application; lead a Work Stream; are member or manager of an Owning Team with Tasks in the Event. Global Admins are implicit participants of every Event. Auditor/Executive get a separately granted global read-only role or per-Event enrolment — never by job title. Default Event visibility is participant-scoped.
- **D-223** (Q20, M-04) Health formulas:
  - Task Score = 100 × COMPLETED eligible Tasks / eligible Tasks (eligible = non-CANCELLED; no extra Blocked penalty).
  - Validation Score = 100 if the Application's required technical validation for the current recovery phase passed (failback validation when failback is active), else 0.
  - Application Health = 0.70 × Task Score + 0.30 × Validation Score.
  - Event Health = Σ(App Health × effective tier weight) / Σ(effective tier weight); weights T0–T4 = 100/75/50/30/20; no Tier-0 cap.
  - Dial bands: GREEN ≥ 85; YELLOW 60–84.99; RED < 60.
  - Treemap bands: GREEN ≥ 85; LIGHT_ORANGE 60–84.99; DARK_ORANGE 40–59.99; RED < 40.
  - Overlays only worsen the visual band: RTO breach → RED; RPO breach → RED; SLA/RTO warning reached → ≥ DARK_ORANGE; active Blocker on T0/T1 App → ≥ DARK_ORANGE; active Blocker on T2–T4 App → ≥ LIGHT_ORANGE. Overlays never change the numeric dial.
  - Zero-Application Event: dial displays "—", not 100.
- **D-224** (Q21, M-05) Readiness catalog (each `HARD_STOP | WARNING | OFF`, globally configurable, Coordinator override only where policy permits and always with reason): failback plan exists for every `failback_required=true` DR App — HARD_STOP; every critical manual Milestone has an owner — HARD_STOP; every in-scope App has a Primary System Owner — HARD_STOP; Primary Business Owner present — WARNING; every Work Stream has a Lead — WARNING; every Task has an Owning Team — HARD_STOP; dependency graph acyclic — HARD_STOP and **not configurable**; ≥1 MONITORING Work Stream Task — WARNING; Event timezone set — HARD_STOP; unresolved import/AI Needs Review — WARNING; DR Coordinator assigned — HARD_STOP; ≥1 DR Application in scope — HARD_STOP; every DR App has RPO target or is explicitly RPO-N/A — HARD_STOP.
- **D-225** (Q22, M-06) Policy keys/defaults: `sla.warning_percent=85`; `sla.warning_minutes_remaining=null`; `blocker.escalation_minutes.{T0=0,T1=15,T2=30,T3=60,T4=120}`; `dependency.edit_requires=SCOPED_ROLE` (alt `COORDINATOR_ONLY`); `milestone.auto_confirm_allowed=false`; `ai.control_profile=CONSERVATIVE`; `retention.audit_years=7`; `retention.event_years=3`; `retention.evidence_years.default=3`; `file.max_mb=100`; `file.allowlist=[pdf,png,jpg,jpeg,txt,log,csv,xlsx,docx,zip,json]`; `notifications.email.enabled=true`; `notifications.teams.enabled=false`; `notifications.slack.enabled=false`; readiness.* per D-224. All uploads still undergo MIME/content validation and malware scanning. SMTP credentials live in secret storage, never policy JSON.
- **D-226** (Q23, M-07) Task carries `evidence_required BOOLEAN NOT NULL DEFAULT TRUE`, `evidence_min_count SMALLINT NOT NULL DEFAULT 1`, `verification_note_required BOOLEAN NOT NULL DEFAULT TRUE` (also in plan/import task representation). Normal completion = work done + ≥1 acceptable Evidence Item (attachment/log/screenshot/link/text per policy) + verification note + Owner/Lead validation. Policy may relax evidence for a class of Tasks; default is required.
- **D-227** (Q24, M-08) `work_streams.stream_type` enum: `NETWORK, STORAGE, DATABASE, APPLICATIONS, MONITORING, VALIDATION, CUSTOM`. At close, any non-cancelled Task in a MONITORING stream not COMPLETED raises an outstanding monitoring closure warning that blocks `close` unless Admin/Coordinator records an audited closure exception override.
- **D-228** (Q25, M-09) AI Act allowlist: task start/block/resume/submit-validation/assign/volunteer; blocker assign/start/resolve; milestone confirm; issue create/review/resolve; comment create; alert acknowledge/snooze. **Never via AI**: any Event lifecycle command, Task/App validation, report publish, package export/import, role/policy/admin/security configuration, local-user creation. Profiles: CONSERVATIVE = confirm every Act with diff preview; BALANCED = explicit confirmation for cross-Team changes and Tier 0/1 work, one batch confirmation for own-scope low-risk; FAST_EXECUTION = own-scope low-risk allowlisted actions execute without per-action reconfirmation; RBAC/idempotency/guards/audit always apply; excluded commands stay excluded in every profile.
- **D-229** (Q26, M-10) RACI export has two sections/sheets. Work Stream RACI: R = Owning Team/active resources; A = Work Stream Lead; C = DR Coordinator + dependent System Owners; I = affected Business Owners + participants. Application RACI: R = Owning Teams executing App Tasks; A = Primary System Owner; C = Secondary/Tertiary System Owners + dependent Work Stream Leads; I = Business Owners (all slots) + DR Coordinator. CSV and PDF render the same model.
- **D-230** (Q27, M-11) Package manifest `{package_version, tenant_id, dr_event_id, exported_at, exported_by, schema_version, content_hash (SHA-256), entity_counts}`. AES-256-GCM with a random 256-bit DEK wrapped by an Azure Key Vault key (prod) or env-provided dev key (local); no plaintext key in package. `tenant_id` = Entra tenant GUID in Azure, `DRCC_TENANT_ID` config locally. Dev→Test import allowed when tenant IDs match; cross-tenant rejected by default.
- **D-231** (Q28, M-12/P-04) Figma is not complete. A **Design System / Figma Gate** milestone precedes visually final Command Center work. BUILD-01…11 proceed; the web shell (layout, auth shell, nav, theme, responsive grid, design-token plumbing, generic Deep Card framework) proceeds; BUILD-12/13 and visual-heavy parts of later UI increments require Figma sign-off. Use the Figma integration if available, else manual token export. Figma deliverables: Command Center, My DR, Application deep view, Task Board, Task deep view, People/resource views, Needs Review, Admin, Reports, Light+Dark, tablet/mobile states, hover/deep-card behaviour, loading/empty/error states.
- **D-232** (Q29, M-13) First speech-to-text provider: local **faster-whisper** in the worker behind a provider interface; Azure AI Speech is a future adapter. Transcribed text is always shown for review before submission.
- **D-233** (Q30, M-14) Embeddings: `nomic-embed-text-v1.5` via **sentence-transformers in the worker**, 768 dimensions, identical locally and in Azure POC; adapter-based so hosted providers can be added; model/dimension change triggers re-embedding, never mixed vectors.
- **D-234** (Q31, M-15) One canonical typed catalog in `packages/contracts` + backend enums for notification types, alert types, severities (`CRITICAL, HIGH, MEDIUM, LOW`), WebSocket event types, timeline event types. New operational event types require an ADR/spec update.
- **D-235** (Q32, M-16) Local accounts: Argon2id; password 12–128 chars, no composition rules, breached-password denylist; 10 failures/15 min → 30-min lockout; 8 h absolute session, 30 min idle; 5-min privileged reauth window; TOTP required for Local GLOBAL_ADMIN, configurable for others; reset via emailed single-use token, 30-min expiry; no pre-created production Local account.
- **D-236** (Q33, M-17) Fictitious data only for the East US 2 → Central US golden scenario and the 500-App/5,000-Task load seed.
- **D-237** (Q51, M-19) `POST /dr-events/{id}/start-failover` accepts optional `network_cut_at` with `activated_at ≤ network_cut_at ≤ now()`; defaults to server `now()`; applies to PLANNED_DR and REAL_INCIDENT; audited.
- **D-238** (Q34, M-18) Versioned report facts schema. Excel sheets: `Event, Applications, Tasks, Dependencies, Milestones, Blockers, Issues, Evidence, Validations, Overrides, RTO_RPO, RACI, Audit_Summary`. CSV default = Task-level; entity CSVs from the same facts model. PDF = executive/auditor report. AI may draft narrative; facts come only from the immutable snapshot.

## D. Engineering decisions ratified

- **D-239** (Q35, E-01) FastAPI owns auth: Entra OIDC via FastAPI; HttpOnly, Secure (prod), SameSite=Lax session cookie; Redis-backed session; CSRF protection on state-changing requests; same-origin `/api` routing from Next.js to FastAPI; WebSocket auth via the same session; Local auth enters the same session/RBAC system; no access tokens in browser JavaScript.
- **D-240** (Q36, E-02) Toolchain: Python 3.12, uv, Node 22 LTS, pnpm workspaces (no Turborepo), Next.js App Router + TypeScript strict (exact version pinned at BUILD-01, lockfiles committed, no floating versions in CI), Ruff, Pyright, ESLint, Prettier.
- **D-241** (Q37, E-03) Celery + Redis from BUILD-01 behind job interfaces; no second scheduler. Jobs: escalations, SLA timers, report generation, document parsing/indexing, embedding, Excel import enrichment, package export, retention, notification fan-out.
- **D-242** (Q38, E-04) Real-time: PostgreSQL transaction → transactional outbox row → worker publishes to Redis → API replicas subscribe → WebSocket clients receive. Container Apps session affinity may be enabled for stability but correctness never depends on it; any replica rebuilds state from PostgreSQL on reconnect.
- **D-243** (Q39, E-05) Recharts/shadcn chart primitives for standard graphs; D3 for the Application treemap and custom health dial. Treemap is a true squarified treemap, not a grid.
- **D-244** (Q40, E-06) PDF via **Playwright/Chromium print** of a dedicated report route/template, in the background worker container. **Supersedes** the WeasyPrint lean.
- **D-245** (Q41, E-07) Azure: Blob + Microsoft Defender for Storage malware scanning; result updates `malware_scan_status`; INFECTED/unscanned evidence can't satisfy completion. Local: scanner adapter — EICAR payload → INFECTED, else CLEAN.
- **D-246** (Q42, E-08/P-06) Repository is **private**; no GHAS assumed. CI security: Semgrep, Bandit, pip-audit, pnpm audit, gitleaks, Syft (SBOM), Grype, Checkov (Bicep). If GHAS appears: add CodeQL, Dependabot, secret scanning. Gate: no unresolved Critical.
- **D-247** (Q43, E-09) `schema_v1.sql` is Alembic revision 0001 (historical basis). Revision 0002 = this reconciliation (enum corrections, comment fix, new tables, target-type enum, evidence fields, stream_type, other approved changes). SQLAlchemy models hand-written to match; CI runs model↔migration drift check (`alembic check`); autogenerate never redefines the domain.
- **D-248** (Q44, E-10) Azure credentials are a prerequisite only for BUILD-22/23. BUILD-01…21 run end-to-end on the MacBook. Azure targets: Container Apps, Azure Database for PostgreSQL, Blob, Key Vault, Log Analytics/App Insights, **Azure Managed Redis** (preferred). Command Center deploys outside the protected workload failure domain; Dev/Test separate resource groups.
- **D-249** (Q45, E-11) First AI provider: **Synthetic**, initial chat model **Kimi K3** if available on the account at implementation time; other Synthetic models (GLM family) and direct Anthropic supported. Model IDs are Admin-configurable, verified at BUILD-16 (AI increment), never hard-coded in domain code. Credentials never committed. Foundry is V2.
- **D-250** (Q46, E-12) Tests: pytest + testcontainers-python (ephemeral pgvector Postgres per session), Vitest + Testing Library, Playwright, k6; ephemeral Redis or isolated test compose profile for Celery integration; no dependence on a developer's shared DB.
- **D-251** (Q47, E-13) `packages/contracts` generated from FastAPI OpenAPI with `openapi-typescript`; CI fails on drift; Zod only for UI/form/runtime validation, never a hand-copied backend contract.

## E. Consequential clarifications (restated for implementers)

- **D-252** Task states final: `NOT_STARTED, IN_PROGRESS, BLOCKED, READY_FOR_VALIDATION, COMPLETED, CANCELLED`. `BLOCKED` is a Task state backed by ≥1 structured Blocker. On blocker verify/close the Task returns to `IN_PROGRESS` (its prior actionable state); it proceeds to `READY_FOR_VALIDATION` only through an explicit `submit-validation`.
- **D-253** Issue/Finding is separate from Blocker; an Issue never stops execution unless explicitly linked/converted to a Blocker or a policy/gate says so.
- **D-254** Ownership: up to 3 System/Application Owners and 3 Business Owners (Primary required). Primary System Owner is Accountable for application validation; any System Owner slot may perform it.
- **D-255** Contact: email + phone (profile data only; no telephony/SMS).
- **D-256** Email functional V1 via Admin-configured SMTP (secret references); Mailpit locally; Teams/Slack disabled "Coming Soon".
- **D-257** RTO measured `network_cut_at → technical_validated_at`; RPO stores target + recovered-data point/result; reports compare target vs actual for both; recorded breaches are immutable.
- **D-258** Retention: audit 7 y; Events/Tasks 3 y; Evidence 3 y default, classification-configurable; reports/imports/indexes follow related Event/Evidence policy; legal hold blocks purge; tests use injectable clocks.
- **D-259** Command Center RTO 60 min / RPO 15 min (configurable). POC load 200 users / 500 Apps / 5,000 Tasks. Latest Chrome + Edge. WCAG 2.1 AA. Light + Dark persisted. UTC storage; Event timezone; user-local toggle. Live propagation ≤ 10 s under POC load.
- **D-260** Support: business hours + on-call during DR windows. Patch SLA 7/30/60/90 days; monthly maintenance + emergency. Bicep IaC. PITR 35 d prod. Structured logs to Log Analytics/App Insights, Sentinel later. AI security: backend-only provider calls, retrieved text untrusted, permission filter before retrieval, no secrets in prompts, metadata-only logging.
- **D-261** UI gate: Figma/UI-UX review is a formal V1 release gate; the 10-second comprehension rule is the core UX acceptance test.
- **D-262** (owner instruction 2026-09-12) Repository hygiene: commits and pull requests carry no tool-attribution trailers or "generated with" lines — the sole author of record is the product owner. Development-tooling configuration, prompts, plans and playbooks are kept outside version control (`.gitignore`) and distributed privately; CI enforces a tracked-path allowlist and rejects attribution trailers.

## F. Superseded frozen wording (checklist for reconciliation)

| Frozen text | Superseded by |
|---|---|
| FROZEN §2.11 / bundle rules file "MinIO-compatible local object storage" | D-217 (Azurite) |
| schema_v1.sql `dr_application_status` enum values | D-208 |
| schema_v1.sql Tier-3 comment | D-218 |
| STATE_MACHINES "suggested operational projection" wording for Application recovery | D-208 (now canonical, not suggested) |
| API_CONTRACT missing `POST /blockers/{id}/start` | D-211 |
| API_CONTRACT "idempotency key required for high-risk/retry-sensitive commands" | D-215 (all command POSTs) |
| UI_UX route map (no /plans, /reviews, /resources) | D-212 |
| RBAC "Create/activate DR Event — app-context planning" | D-213 (create PLANNED only) |
| FROZEN §4.14 "latest valid committed update is current" ambiguity | D-214 |
| Master §14.4 endpoints (pause/resume/abort/skip/complete/ai chat) | D-207 |
| Master §15.3 `/dr/...` route map | D-212 |
| Master §10.5 "T3 weight cannot be invented" / §15.6 "Tier 0 cap may apply" | FROZEN §10 + D-218 |
| Free-text `target_type` columns | D-216 |
| Discovery lean "WeasyPrint" | D-244 |

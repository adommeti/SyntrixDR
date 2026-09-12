**Reconciled 2026-09-12 against FREEZE_ADDENDUM.md (D-201…D-261).**

# DR Command Center — Ordered Build Prompts

Use one BUILD prompt per focused coding session/increment. Each assumes the project rules and `docs/spec/FREEZE_ADDENDUM.md` have been read. Each BUILD-NN is one PR/milestone unit; large increments may run as internal sessions (`BUILD-06a/06b/06c`) and merge as one PR after full acceptance; tag `v0.NN.0` after merge (D-202, D-203). independent second-opinion review is mandatory for BUILD-06, 08, 10, 16, 20 (D-204). The 10-week clock starts when BUILD-01 begins (D-206).

## BUILD-01 — Monorepo + local platform foundation

**Goal:** Create production-shaped local development foundation.

**Create/modify:** `apps/web`, `apps/api`, `packages`, `infra/bicep`, root Compose/Makefile/config.

**Rules:** frozen stack only; PostgreSQL16+pgvector, Redis, **Azurite** (not MinIO; one `ObjectStore` adapter on `azure-storage-blob`) (D-217), Mailpit; Celery + Redis wired from this increment behind job interfaces, no second scheduler (D-241); Python 3.12 + uv, Node 22 LTS + pnpm workspaces (no Turborepo), exact Next.js version pinned, lockfiles committed, Ruff/Pyright/ESLint/Prettier (D-240); pytest + testcontainers-python for ephemeral pgvector Postgres, Vitest + Testing Library, Playwright, k6 scaffolds (D-250); `packages/contracts` generated from FastAPI OpenAPI with `openapi-typescript`, CI drift check (D-251); `Idempotency-Key` middleware complete — required on every command POST, 400 `IDEMPOTENCY_KEY_REQUIRED` if missing, replay of the original outcome within the window, 24 h TTL on `idempotency_keys` (D-215); no AI requirement.

**Acceptance:** web/API health endpoints; DB migration runs (schema_v1.sql = Alembic 0001, revision 0002 = reconciliation, `alembic check` drift gate) (D-247); Idempotency-Key middleware complete (400 `IDEMPOTENCY_KEY_REQUIRED`, replay of original outcome, 24 h TTL) (D-215); local services healthy on Mac; OpenTelemetry/correlation skeleton; CI-ready lint/type/test; contracts generation runs and CI fails on drift (D-251).

**Verify:** `make verify-env && make lint && make typecheck && make test`.

## BUILD-02 — Identity, users, teams and authorization

**Goal:** Entra-ready auth abstraction + Local dev/fallback auth + users/teams/roles/scopes.

**Rules:** Event-participant visibility model backed by explicit `dr_event_participants` with auto-enrolment (Event-scoped role; current Task assignee; Blocker owner; any-slot System/Application or Business Owner of in-scope App; Work Stream Lead; member/manager of an Owning Team with Tasks in the Event; Global Admins implicit; Auditor/Executive by granted global read-only role or per-Event enrolment, never job title) (D-222); server authorization; Global Admin/Coordinator lifecycle authority; Manager own-Team boundary; FastAPI owns auth — Entra OIDC via FastAPI, HttpOnly/Secure/SameSite=Lax Redis-backed session cookie, CSRF on state-changing requests, same-origin `/api` routing, WebSocket auth via same session, Local auth in the same session/RBAC system, no access tokens in browser JS (D-239); Local accounts: Argon2id, password 12–128 chars, no composition rules, breached-password denylist, 10 failures/15 min → 30-min lockout, 8 h absolute / 30 min idle session, 5-min privileged reauth window, TOTP required for Local GLOBAL_ADMIN and configurable for others, emailed single-use reset token with 30-min expiry (D-235); auth/session endpoints per API_CONTRACT "Authentication / session" — `GET /api/v1/auth/entra/login` (redirect), `GET /api/v1/auth/entra/callback`, `POST /api/v1/auth/local/login` (rate-limited, lockout), `POST /api/v1/auth/logout`, `POST /api/v1/auth/reauth` (password/TOTP/Entra step-up → reauth grant, 5-min window; required by high-risk commands), `POST /api/v1/auth/local/password-reset/request`, `POST /api/v1/auth/local/password-reset/confirm`, `POST /api/v1/auth/local/totp/enrol`, `POST /api/v1/auth/local/totp/verify`, `GET /api/v1/auth/csrf`; these are session endpoints, not domain commands — no `Idempotency-Key`; CSRF required on every POST except login (I-2, plan §10).

**Acceptance:** authorization matrix tests; explicit Local-user create path; no production default Local admin (D-235); audit security changes; participant auto-enrolment tests (D-222); auth endpoint tests — lockout after 10 failures/15 min, reauth grant expires at 5 min, reset token single-use/30-min, CSRF rejected on POSTs other than login (I-2, plan §10).

**Verify:** `make test-auth`.

## BUILD-03 — Application catalog, owners, tiers and policy

**Goal:** Application/Tier/policy domain.

**Rules:** up to 3 System/Application owners and 3 Business owners; Primary required; Tier defaults 30/60/120/240/1440 and weights 100/75/50/30/20; Admin global + Coordinator Event overrides audited.

**Verify:** `make test-domain`.

## BUILD-04 — Reusable Plans, DR Events and readiness

**Goal:** Plan template/version + canonical Event lifecycle + readiness.

**Rules:** canonical Event states only; baseline captured at Start Failover; no separate approval chain; parent/child roll-up — parent is a reporting/coordination container, children own lifecycle/`network_cut_at`/RTO/RPO/failover/failback/report, parent aggregates descendant DR Applications, never mutates child state, cannot `CLOSE` while any non-cancelled child is non-terminal, standalone subset Events need no parent (D-219); subset Apps; failback required default; Application/System Owners may **create PLANNED** Events scoped to their own Application(s) but cannot `activate`, `start-failover` or later commands without Admin/Coordinator authority (D-213); `start-failover` accepts optional `network_cut_at` with `activated_at ≤ network_cut_at ≤ now()`, default server `now()`, for PLANNED_DR and REAL_INCIDENT, audited (D-237); readiness catalog and HARD_STOP/WARNING/OFF configurability per D-224.

**Acceptance:** illegal transitions rejected; CLOSED/CANCELLED terminal; audit/outbox event; out-of-bounds `network_cut_at` rejected (D-237); Owner-created PLANNED Event cannot be activated by that Owner (D-213); parent close blocked by non-terminal child (D-219).

**Verify:** `make test-transitions`.

## BUILD-05 — Excel import and plan editor

**Goal:** Upload `.xlsx`, parse rows, create reviewable draft Tasks/Subtasks with provenance.

**Rules:** no rigid Excel format; importer requires a **mapping/review step** supporting arbitrary headers, preserves row provenance (`source_import_id`, `source_import_row`), allows AI or manual mapping (D-221); AI optional; manual edit/merge/split/reclassify always possible; low confidence Needs Review; expected duration retained; imported task representation carries `evidence_required`, `evidence_min_count`, `verification_note_required` (D-226).

**Acceptance:** fixture columns (test-only, not mandatory customer headers): `Phase, Work Stream, Application, Tier, Task, Subtask / Procedure, Owning Team, Assignee, Expected Duration, Predecessor / Dependency, Failover / Failback, Evidence Required, Notes` (D-221).

**Verify:** fixture workbooks + AI-disabled import tests.

## BUILD-06 — Work Streams, Tasks and dependencies

**Goal:** canonical Task model, Work Streams, HARD/ADVISORY Finish-to-Start dependency DAG.

**Rules:** Task may reference app + stream; Ready derived; no raw status patch; no cycles/self edge; Task states final `NOT_STARTED, IN_PROGRESS, BLOCKED, READY_FOR_VALIDATION, COMPLETED, CANCELLED`, BLOCKED backed by ≥1 Blocker, return to `IN_PROGRESS` on blocker verify/close, `READY_FOR_VALIDATION` only via explicit `submit-validation` (D-252); `BLOCKED → IN_PROGRESS` returns automatically when `blockers/{id}/verify` closes the last active Blocker, `tasks/{id}/resume` is the manual path only (no active Blocker left, or audited override with reason), both emit `TASK_RESUMED` (I-1, plan §10); surface the 0002 enums/columns (created in BUILD-01 migration 0002) in models, schemas and contracts — `Idempotency-Key` enforcement on every Task/dependency command POST (middleware from BUILD-01, D-215); canonical `target_type` enum `DR_EVENT, DR_APPLICATION, APPLICATION, WORK_STREAM, TASK, TASK_DEPENDENCY, MILESTONE, BLOCKER, ISSUE_FINDING, VALIDATION, IMPORT_JOB, PLAN, PLAN_VERSION, REPORT, DOCUMENT, ALERT`, per-table subsets enforced by CHECK/service (D-216); Task columns `evidence_required BOOLEAN NOT NULL DEFAULT TRUE`, `evidence_min_count SMALLINT NOT NULL DEFAULT 1`, `verification_note_required BOOLEAN NOT NULL DEFAULT TRUE` (D-226); `work_streams.stream_type` enum `NETWORK, STORAGE, DATABASE, APPLICATIONS, MONITORING, VALIDATION, CUSTOM` (D-227). independent second-opinion review mandatory (D-204).

**Verify:** property/table-driven graph and transition tests; missing Idempotency-Key on a Task command → 400 `IDEMPOTENCY_KEY_REQUIRED` (D-215); resume-vs-verify table (last-Blocker verify resumes; non-last verify leaves BLOCKED; resume without override rejected while a Blocker is active) (I-1, plan §10).

## BUILD-07 — Milestones, assignments and resource model

**Goal:** critical manual gates + fixed Owning Team/floating assignee + same-Team/Coordinator assignment logic.

**Rules:** Manager own Team only; dynamic workload from Tasks; **Manager precedence exact semantics** — when a Team Manager submits an assignment for a Task owned by their Team with a stale `expected_version` and the intervening change was an assignment by a DR Coordinator/Global Admin, the Manager's assignment is accepted (version increments), both actions audited, conflict resolution recorded as `MANAGER_PRECEDENCE`, affected users notified; all other stale writes return `409 CONCURRENCY_CONFLICT` (D-214); `milestone.auto_confirm_allowed=false` default (D-225).

**Verify:** concurrency tests (MANAGER_PRECEDENCE accepted path + 409 for every other stale write) (D-214) + milestone release tests.

## BUILD-08 — Blockers, Issues, comments and notifications

**Goal:** BLOCKED state + structured Blocker + separate Issue/Finding + comments/@mentions/in-app alerts.

**Rules:** blocker reason required; four commands `assign → ASSIGNED`, `start → IN_PROGRESS`, `resolve → RESOLVED`, `verify → VERIFIED` then `CLOSED` atomically (both audited); `POST /blockers/{id}/start` added; Owning Team accountability never changes (D-211); escalation timers `blocker.escalation_minutes.{T0=0,T1=15,T2=30,T3=60,T4=120}` (D-225); Issue nonblocking unless explicitly linked/converted (D-253); personal snooze != shared ack; one canonical typed catalog in `packages/contracts` + backend enums for notification types, alert types, severities (`CRITICAL, HIGH, MEDIUM, LOW`), WebSocket event types, timeline event types; new operational event types require ADR/spec update (D-234). independent second-opinion review mandatory (D-204).

**Verify:** escalation-clock tests and cross-Team comment tests; blocker verify → VERIFIED + CLOSED atomic audit test (D-211).

## BUILD-09 — Evidence, validation and file security

**Goal:** evidence model and protected completion.

**Rules:** file/text/screenshot/log/link; evidence + verification note; app owner/work-stream lead validator; every Task reaches `COMPLETED` only via `READY_FOR_VALIDATION → validate` by an Application/System Owner (any slot) or Work Stream Lead, Executors never self-complete, Global Admin/DR Coordinator may validate per RBAC, `needs_specific_validation` semantics (D-209); `validations` table persisted with no separate REST resource — `POST /tasks/{id}/submit-validation` creates PENDING, `POST /tasks/{id}/validate` and `POST /dr-applications/{id}/validate` close APPROVED | REJECTED, `IN_REVIEW`/`REMEDIATION` reserved unused, rejected Task returns to `IN_PROGRESS` (D-210); normal completion = work done + ≥1 acceptable Evidence Item + verification note + Owner/Lead validation, policy may relax per Task class, default required (D-226); 100MB default; allowlist `[pdf,png,jpg,jpeg,txt,log,csv,xlsx,docx,zip,json]` (D-225); scanner adapter — Azure: Blob + Microsoft Defender for Storage updates `malware_scan_status`, INFECTED/unscanned evidence can't satisfy completion; local: EICAR payload → INFECTED, else CLEAN (D-245).

**Verify:** malicious/oversize/disallowed-file tests + completion guard tests; EICAR → INFECTED cannot satisfy completion (D-245).

## BUILD-10 — RTO, RPO, forecast and health

**Goal:** metrics engine.

**Rules:** common network-cut RTO start; stop only technical validation; breach immutable; RPO target/reference/recovered data; deterministic HARD critical-path forecast; DR Application status enum `NOT_STARTED → RECOVERING → TECHNICAL_VALIDATION → FAILED_OVER → FAILBACK_IN_PROGRESS → COMPLETED`, `COMPLETED` common terminal, `failback_required=false` moves `TECHNICAL_VALIDATION → COMPLETED` directly, RTO clock stops on `TECHNICAL_VALIDATION → FAILED_OVER` (or `→ COMPLETED`) (D-208); DR Application transition → command map — `NOT_STARTED → RECOVERING` for every in-scope DR App is a side effect of Event `start-failover` (same transaction, audited per App); `RECOVERING → TECHNICAL_VALIDATION` via new `POST /api/v1/dr-applications/{id}/submit-validation` (App Owner slot / Coordinator / Admin; guard: no FAILOVER-phase Task of the App in BLOCKED/IN_PROGRESS unless audited override; creates the Validation record PENDING); policy `application.auto_submit_validation` (default `false`) may auto-fire it when all FAILOVER-phase Tasks are COMPLETED; `TECHNICAL_VALIDATION → FAILED_OVER | COMPLETED` via existing `validate`, rejection returns to `RECOVERING` with note (I-3, plan §10); health formulas — Task Score = 100 × COMPLETED eligible / eligible (non-CANCELLED, no Blocked penalty); Validation Score = 100 if required technical validation for current phase passed (failback validation when failback active) else 0; App Health = 0.70 × Task + 0.30 × Validation; Event Health = Σ(App Health × effective tier weight) / Σ(weight), T0–T4 = 100/75/50/30/20, no T0 cap; dial bands GREEN ≥ 85 / YELLOW 60–84.99 / RED < 60; treemap bands GREEN ≥ 85 / LIGHT_ORANGE 60–84.99 / DARK_ORANGE 40–59.99 / RED < 40; overlays only worsen the visual band (RTO breach → RED; RPO breach → RED; SLA/RTO warning → ≥ DARK_ORANGE; active Blocker T0/T1 → ≥ DARK_ORANGE; active Blocker T2–T4 → ≥ LIGHT_ORANGE) and never change the numeric dial; zero-App Event dial "—" (D-223); every DR App has RPO target or is explicitly RPO-N/A as a HARD_STOP readiness rule (D-224); `sla.warning_percent=85`, `sla.warning_minutes_remaining=null` (D-225). independent second-opinion review mandatory (D-204).

**Verify:** deterministic calculation golden tests including breach and late recovery; golden numbers for every band boundary and overlay (D-223); RPO-N/A readiness test (D-224); App status transition table incl. no-failback shortcut (D-208); start-failover moves every in-scope App to RECOVERING in one transaction; submit-validation guard rejects with an open FAILOVER Task and passes with override; auto-submit fires only when `application.auto_submit_validation=true`; validate rejection → RECOVERING (I-3, plan §10).

## BUILD-11 — WebSockets and live projections

**Goal:** reliable live updates after committed mutations.

**Rules:** DB truth first; PostgreSQL transaction → transactional outbox row → worker publishes to Redis → API replicas subscribe → WebSocket clients receive; Container Apps session affinity optional for stability, correctness never depends on it; any replica rebuilds state from PostgreSQL on reconnect (D-242); reconnect refetch; Event participant authorization (D-222); WebSocket auth via the same session cookie (D-239).

**Verify:** multi-client Playwright test and <=10s local acceptance; two-replica test where the client is connected to a replica other than the one that committed (D-242).

## BUILD-00 — Design System / Figma Gate (parallel lane)

**This is a design gate, not a code increment.** It runs in parallel with BUILD-01…11 and must be signed off before BUILD-12/13 and the visual-heavy parts of later UI increments begin. (D-231)

**Goal:** complete the Figma design system and screens so visually final Command Center work can be implemented against approved designs. (D-231)

**Not gated (proceeds in BUILD-01…11):** the web shell — layout, auth shell, nav, theme, responsive grid, design-token plumbing, generic Deep Card framework. (D-231)

**Gated:** BUILD-12, BUILD-13, and visual-heavy parts of later UI increments. (D-231)

**Deliverables:** Command Center, My DR, Application deep view, Task Board, Task deep view, People/resource views, Needs Review, Admin, Reports, Light+Dark, tablet/mobile states, hover/deep-card behaviour, loading/empty/error states. (D-231)

**Tooling:** Figma integration if available, else manual token export. (D-231)

**Acceptance:** formal UI/UX sign-off recorded under `/docs/reviews/`; 10-second comprehension rule is the core UX acceptance test (D-261, D-205).

## BUILD-12 — Figma-approved Coordinator Command Center

**Goal:** implement approved treemap, dial, resource sidebar, critical panel, timeline and RTO/RPO status.

**Rules:** gated on BUILD-00 Figma sign-off (D-231); 10-second comprehension; WCAG; color-independent state cues; Light/Dark; Recharts/shadcn chart primitives for standard graphs, D3 for the Application treemap (true squarified treemap, not a grid) and custom health dial (D-243); render D-223 bands/overlays and "—" for zero-App Events.

**Verify:** visual/accessibility/Playwright + 500-app seed render.

## BUILD-13 — My DR, Kanban and deep cards

**Goal:** personalized work experience.

**Rules:** gated on BUILD-00 Figma sign-off (D-231); Ready derived tile; canonical state board; hover + Show More; labels/filters/live saved views; routes per D-212 — `/plans`, `/plans/[planId]`, `/dr-events/[eventId]/reviews`, `/dr-events/[eventId]/resources`, both `/people` and `/admin/users-teams`, Application history as a tab on `/applications/[applicationId]` (no `/history`), `/admin/notifications` with Email functional and Teams/Slack "Coming Soon" (D-212).

**Verify:** role-specific UI tests; route map test against D-212 (D-212).

## BUILD-14 — Failback, monitoring and closure

**Goal:** independent failback and close/cancel behavior.

**Rules:** explicit Start Failback; feature-toggle no-failback exception; monitoring guard — at close, any non-cancelled Task in a `MONITORING` `stream_type` Work Stream not COMPLETED raises an outstanding monitoring closure warning that blocks `close` unless Admin/Coordinator records an audited closure exception override (D-227); parent close rule — parent cannot `CLOSE` while any non-cancelled child is non-terminal (D-219); terminal history; DR Application failback side effects — Event `start-failback` moves every App with `failback_required=true` `FAILED_OVER → FAILBACK_IN_PROGRESS` in the same transaction, audited per App; `FAILBACK_IN_PROGRESS → COMPLETED` via `POST /dr-applications/{id}/submit-validation` again (guard: no FAILBACK-phase Task BLOCKED/IN_PROGRESS unless audited override; `application.auto_submit_validation` may auto-fire) followed by `validate` in the failback phase; Apps with `failback_required=false` are already COMPLETED and untouched (I-3, plan §10).

**Verify:** complete manual DR scenario through close; monitoring guard blocks then exception override passes (D-227); parent close with open child rejected (D-219); start-failback moves only `failback_required=true` Apps; failback submit-validation guard + validate → COMPLETED (I-3, plan §10).

## BUILD-15 — Hybrid search and documents

**Goal:** PostgreSQL FTS + pgvector document retrieval.

**Rules:** structured relations authoritative; pre-retrieval permission filtering; `nomic-embed-text-v1.5` via sentence-transformers in the worker, 768 dimensions, identical locally and in Azure POC, adapter-based; model/dimension change triggers re-embedding, never mixed vectors (D-233); provenance/citation metadata.

**Verify:** semantic/lexical tests and inaccessible-chunk negative test.

## BUILD-16 — AI provider abstraction and chat

**Goal:** Synthetic + Anthropic Read/Recommend/Act, summaries feature flag, Needs Review.

**Rules:** backend-only provider keys; AI content untrusted; Act calls typed command service; no direct DB write; AI-off product still works; Act allowlist — task start/block/resume/submit-validation/assign/volunteer, blocker assign/start/resolve, milestone confirm, issue create/review/resolve, comment create, alert acknowledge/snooze; never via AI — Event lifecycle commands, Task/App validation, report publish, package export/import, role/policy/admin/security configuration, local-user creation; profiles CONSERVATIVE (default, confirm every Act with diff preview) / BALANCED / FAST_EXECUTION with RBAC/idempotency/guards/audit always applying (D-228, D-225); first provider Synthetic, initial chat model Kimi K3 if available on the account, GLM family and direct Anthropic supported, model IDs Admin-configurable and verified in this increment, never hard-coded, credentials never committed (D-249). independent second-opinion review mandatory (D-204).

**Verify:** prompt-injection/RBAC/action-confirmation tests; excluded command cannot be invoked via AI in any profile (D-228).

## BUILD-17 — Speech-to-text

**Goal:** provider-agnostic transcription endpoint/UI.

**Rules:** first provider local faster-whisper in the worker behind a provider interface; Azure AI Speech future adapter (D-232); transcription returns editable text always shown for review before submission (D-232); no automatic high-risk command execution from audio.

**Verify:** faster-whisper adapter plus mock provider plus review/edit flow (D-232).

## BUILD-18 — Email administration

**Goal:** functional V1 SMTP config/test/send/retry plus Teams/Slack Coming Soon UI.

**Rules:** Admin-configured SMTP via secret references; Mailpit locally; Teams/Slack disabled "Coming Soon" (D-256); `notifications.email.enabled=true`, `notifications.teams.enabled=false`, `notifications.slack.enabled=false` defaults; SMTP credentials never in policy JSON (D-225); failure never rolls back committed DR action; secrets secured.

**Verify:** Mailpit and Gmail-compatible config integration test.

## BUILD-19 — Reporting and RACI exports

**Goal:** Draft→edit/version→publish report; PDF/CSV/Excel; RACI PDF/CSV.

**Rules:** factual snapshot separate from editable narrative; include RTO/RPO/timeline/drift/blockers/issues/evidence/audit/failback; versioned report facts schema; Excel sheets `Event, Applications, Tasks, Dependencies, Milestones, Blockers, Issues, Evidence, Validations, Overrides, RTO_RPO, RACI, Audit_Summary`; CSV default Task-level with entity CSVs from the same facts model; PDF = executive/auditor report; AI may draft narrative, facts only from the immutable snapshot (D-238); RACI two sections/sheets — Work Stream RACI (R = Owning Team/active resources; A = Work Stream Lead; C = DR Coordinator + dependent System Owners; I = affected Business Owners + participants) and Application RACI (R = Owning Teams executing App Tasks; A = Primary System Owner; C = Secondary/Tertiary System Owners + dependent Work Stream Leads; I = Business Owners all slots + DR Coordinator), CSV and PDF render the same model (D-229); PDF via Playwright/Chromium print of a dedicated report route/template in the worker container (D-244).

**Verify:** deterministic report fixture tests; PDF worker render test (D-244).

## BUILD-20 — Encrypted DR package

**Goal:** exact restore + clone-as-new package.

**Rules:** AES-256-GCM with random 256-bit DEK wrapped by Azure Key Vault key (prod) or env-provided dev key (local), no plaintext key in package; manifest `{package_version, tenant_id, dr_event_id, exported_at, exported_by, schema_version, content_hash (SHA-256), entity_counts}`; `tenant_id` = Entra tenant GUID in Azure, `DRCC_TENANT_ID` locally; Dev→Test import allowed when tenant IDs match, cross-tenant rejected by default (D-230); integrity; high-risk reauth; stage/validate before mutation. independent second-opinion review mandatory (D-204).

**Verify:** round trip + wrong tenant + tamper + wrong key negative tests.

## BUILD-21 — Retention/legal hold/history

**Goal:** retention and comparison jobs.

**Rules:** Audit7y/Event3y/Evidence default3y; legal hold; fake clock tests; historical app comparisons.

**Verify:** purge/legal-hold tests without real waiting.

## BUILD-22 — Azure/Bicep and production-shaped telemetry

**Goal:** Dev/Test environment Bicep, Container Apps, managed PostgreSQL, Key Vault, Blob, telemetry.

**Rules:** POC public ingress allowed; production private posture documented; outside protected workload failure domain; Azure credentials are first required here (BUILD-01…21 ran locally); targets Container Apps, Azure Database for PostgreSQL, Blob, Key Vault, Log Analytics/App Insights, **Azure Managed Redis** (preferred); Dev/Test separate resource groups (D-248); Blob + Microsoft Defender for Storage malware scanning (D-245).

**Verify:** Bicep validation/lint/security checks (Checkov) (D-246).

## BUILD-23 — Security and resilience hardening

**Goal:** sessions/CORS/CSRF/rate limits/high-risk reauth, SAST/dependency/SBOM/container/IaC gates, backup/rollback runbooks.

**Rules:** private repository, no GHAS assumed; CI security stack Semgrep, Bandit, pip-audit, pnpm audit, gitleaks, Syft (SBOM), Grype, Checkov (Bicep); if GHAS appears add CodeQL, Dependabot, secret scanning; gate: no unresolved Critical (D-246); Azure credentials required (D-248).

**Verify:** `make security` + restore/rollback drill evidence.

## BUILD-24 — POC scale + final dry run

**Goal:** final acceptance.

**Seed:** 500 Applications, 5,000 Tasks; 200 concurrent users.

**Acceptance:** API/dashboard/realtime goals; WCAG; 10-second UX test; full regional DR; RTO/RPO; failback; report/package; AI-on then AI-off execution; reviewer-board sign-off.

**Verify:** `make release-verify`.

# DR Command Center — Architecture Decision Records

**Status:** Canonical companion (regenerated 2026-09-12 from the reconciled canon, D-220).
**Precedence:** `FREEZE_ADDENDUM.md` > `FROZEN_DECISIONS.md` > this file. Where an ADR cites a D-record, the D-record is the authority; this file explains it.
**Scope:** ADR-001…016 restate master §16.14 with the frozen outcome (every `[PROPOSED]` closed). ADR-017…030 record the engineering decisions ratified in the addendum.
**Process:** New ADRs go in `docs/adr/ADR-0NN-<slug>.md` (D-205) and are summarised here in the next spec PR. An ADR never changes product behaviour; that needs a D-record (see `OPEN_QUESTIONS.md`).

Statuses: **Accepted** (frozen, implement as written) · **Superseded by ADR-0NN**.

---

## ADR-001 — Modular monolith

**Context.** V1 has rich domain behaviour (Events, Tasks, dependencies, blockers, health, AI) but one product, one team and no demonstrated need for distributed-service complexity. Transactional consistency across Task → Blocker → audit → outbox matters more than independent scaling.
**Decision.** One FastAPI application with explicit module boundaries (`apps/api/app/<module>/` with route / application / domain / infrastructure layers; ARCHITECTURE.md lists the 19 modules). Lifecycle transitions live only in `<module>/transition_service.py`.
**Alternatives rejected.** Microservices (operational overhead, cross-service transactions for audit/outbox); serverless functions (no long-lived WebSocket, cold starts during a live DR).
**Consequences.** Simple local run and deploy; one migration stream; module extraction remains possible because modules communicate through application services, not shared ORM sessions across boundaries.
**Status.** Accepted (FROZEN §2.5).

## ADR-002 — PostgreSQL (local and Azure Database for PostgreSQL)

**Context.** An earlier Azure-managed relational store idea conflicted with the mandate that the whole stack runs on a MacBook in containers.
**Decision.** PostgreSQL 16 everywhere: `pgvector/pgvector:pg16` locally, Azure Database for PostgreSQL Flexible Server in Azure. UTC storage; Event timezone is metadata.
**Alternatives rejected.** Azure SQL (no local parity, no pgvector); SQLite for local (divergent behaviour, no enums/tsvector).
**Consequences.** One schema, one migration tool (Alembic), FTS and vectors in the same transaction as the domain rows.
**Status.** Accepted (FROZEN §2.6, §19.1).

## ADR-003 — pgvector over Pinecone

**Context.** V1 semantic retrieval covers runbooks, evidence and comments for a single tenant; volumes are small (POC: thousands of chunks).
**Decision.** `document_chunks.embedding vector(768)` in PostgreSQL with pgvector; ANN index added once the model/dimension is fixed (D-233).
**Alternatives rejected.** Pinecone: a second managed system, network egress from the failure domain, no local parity, extra credentials to protect.
**Consequences.** Retrieval is filtered by RBAC in the same SQL statement as the vector search (permission filter before retrieval, SECURITY_REVIEW §8).
**Status.** Accepted (FROZEN §2.8, §19.2).

## ADR-004 — pgvector over Chroma

**Context.** Chroma is convenient for a quick local RAG but would make local and production diverge.
**Decision.** Same as ADR-003; no Chroma anywhere, including tests.
**Alternatives rejected.** Chroma for local + pgvector for prod (two retrieval code paths, two test matrices).
**Consequences.** Tests use a real pgvector container (ADR-027).
**Status.** Accepted (FROZEN §2.8).

## ADR-005 — Hybrid structured + semantic search; structured relations authoritative

**Context.** DR impact decisions must be explainable and auditable. Vector similarity is a hint, not a fact.
**Decision.** Search = structured SQL filters + PostgreSQL FTS (`search_vector` tsvector) + pgvector similarity. `task_dependencies` rows are authoritative; AI-inferred relations are labelled, cited and enter Needs Review (`review_source = SEMANTIC_INFERENCE`), never silently persisted as dependencies.
**Alternatives rejected.** Keyword-only search (FROZEN §19.3); treating similarity as dependency truth.
**Consequences.** Every AI answer carries provenance; forecast and readiness only read structured edges.
**Status.** Accepted (FROZEN §2.7, §11.16–17).

## ADR-006 — Manual-first AI assist

**Context.** DR cannot depend on a probabilistic component or an external provider being up.
**Decision.** Every critical operation has a non-AI path. AI Read/Recommend never mutate; AI Act calls the same command service as the UI (`/ai/act/prepare` → `/ai/act/confirm`). `AI_ENABLED=false` is a first-class, tested configuration (release gate B1.3).
**Alternatives rejected.** AI-first flows where the model is required to execute commands.
**Consequences.** AI panel degrades to "unavailable"; no domain code imports a provider SDK.
**Status.** Accepted (FROZEN §1.4, §11.10, §11.18).

## ADR-007 — Fixed Task Owning Team, floating Current Assignee

**Context.** Cross-Team help is common during a live DR; accountability and reporting must not drift with it.
**Decision.** `tasks.owning_team_id` never changes because of assignment or volunteering; `tasks.current_assignee_user_id` may point to any eligible user. Volunteer claims (`POST /tasks/{id}/volunteer`) leave Owning Team intact.
**Alternatives rejected.** Moving the Task into the volunteer's Team with a "ghost card" in the original Team (FROZEN §19.9).
**Consequences.** Team workload is computed from current assignments; RACI "R" is the Owning Team; Manager precedence (ADR-030) is defined in terms of Owning Team.
**Status.** Accepted (FROZEN §4.13, §7).

## ADR-008 — Lightweight structured Blocker object

**Context.** A blocked Task needs routing, escalation and verification history without turning every block into a heavyweight ticket.
**Decision.** `blockers` table tied to a Task; six states `OPEN → ASSIGNED → IN_PROGRESS → RESOLVED → VERIFIED → CLOSED`, driven by four commands (`assign`, `start`, `resolve`, `verify` which closes atomically) (D-211). `BLOCKED` is a Task state backed by ≥ 1 active Blocker (D-252). Issue/Finding stays a separate, non-blocking entity (D-253).
**Alternatives rejected.** Auto-creating an Issue per block; a `blocked` boolean without history.
**Consequences.** Tier-0 immediate escalation and configurable timers (`blocker.escalation_minutes.*`, D-225) run as Celery jobs; verify returns the Task to `IN_PROGRESS`, never to `READY_FOR_VALIDATION`.
**Status.** Accepted (FROZEN §7.4, D-211, D-252).

## ADR-009 — First-class Milestone

**Context.** Shared handoff gates (Network Ready → Storage, Storage Ready → Database) unblock many downstream Tasks and need distinct confirmation semantics.
**Decision.** `milestones` entity with `NOT_STARTED → IN_PROGRESS/AT_RISK → READY_FOR_CONFIRMATION → ACHIEVED | MISSED`; critical milestones require manual confirmation (`confirmation_mode = MANUAL`) unless `milestone.auto_confirm_allowed` policy permits otherwise (D-225). Confirmation is `POST /milestones/{id}/confirm`.
**Alternatives rejected.** Styling an ordinary Task as a milestone.
**Consequences.** Readiness rule "every critical manual Milestone has an owner" is a HARD_STOP (D-224); derived Ready considers milestone gates.
**Status.** Accepted (FROZEN §7.13).

## ADR-010 — Immutable Baseline plus editable, audited live execution

**Context.** Incident execution must stay flexible; retrospectives need planned-vs-actual drift.
**Decision.** `start-failover` snapshots the plan into a `plan_versions` row of type `BASELINE` referenced by `dr_events.baseline_plan_version_id`. Live Tasks/dependencies/milestones remain editable; every edit is an `audit_events` row. Reports compute drift = baseline vs live.
**Alternatives rejected.** Fully immutable live plan after start.
**Consequences.** Baseline immutability is an application-service tested invariant (TEST_STRATEGY "baseline immutability vs live edits").
**Status.** Accepted (FROZEN §6.5, DATA_MODEL invariant 17).

## ADR-011 — WebSockets for real-time (translation from SignalR)

**Context.** The earlier design assumed Azure SignalR; the mandated stack is Python/FastAPI. Master §16.14 left "WebSocket or SSE" open.
**Decision.** WebSockets, one channel per Event: `/api/v1/ws/dr-events/{event_id}`, authenticated by the same session cookie (ADR-018), participant-scoped. Fan-out mechanics are ADR-021.
**Alternatives rejected.** SSE (no bidirectional presence/ack needs today, but WebSocket keeps the door open and matches the frozen wording); Azure SignalR/Web PubSub (extra managed service, no local parity).
**Consequences.** Socket messages are never source-of-truth; reconnect triggers REST refetch.
**Status.** Accepted (FROZEN §2.10; closes master §16.14 ADR-011 item 3).

## ADR-012 — Azure Container Apps

**Context.** Container-first Mac development; need horizontally scalable web/api plus a long-running worker without Kubernetes.
**Decision.** Three Container Apps (`web`, `api`, `worker`) in one Container Apps environment per Dev/Test resource group, outside the protected workload failure domain (D-248). Details in `infra/BICEP_PLAN.md`.
**Alternatives rejected.** App Service (weaker worker/scale-to-zero story, no sidecar model); AKS (FROZEN §2.15 forbids Kubernetes in V1).
**Consequences.** Session affinity is optional for stability only (ADR-021); secrets are Key Vault references via managed identity.
**Status.** Accepted (FROZEN §2.12; master §16.14 ADR-012 alternative closed).

## ADR-013 — Explicit command endpoints, no status PATCH

**Context.** Guards, authorization, audit, outbox and idempotency must be centralised.
**Decision.** Every lifecycle mutation is a semantic command (`/dr-events/{id}/start-failover`, `/tasks/{id}/submit-validation`, …) per `API_CONTRACT.md`. `PATCH /tasks/{id}` edits non-state metadata only. Not V1: pause/resume, task skip, `/tasks/{id}/complete`, generic validation approve/reject, `/ai/chat` (D-207).
**Alternatives rejected.** Generic `PATCH {status}` on lifecycle entities.
**Consequences.** `Idempotency-Key` required on all command POSTs (ADR-030); every command has the seven-case API test set (TEST_STRATEGY "API tests").
**Status.** Accepted (ARCHITECTURE "Command/query split", D-207).

## ADR-014 — Blob storage for binary evidence, PostgreSQL for metadata

**Context.** Evidence files up to 100 MB must not bloat the relational database yet must stay auditable.
**Decision.** `evidence_items`/`documents` store `blob_uri`, `checksum`, `mime_type`, `size_bytes`, `malware_scan_status`; bytes live in Blob containers `evidence`, `documents`, `packages`. Objects are private; access is via short-lived authorised URLs (`BLOB_SAS_TTL_SECONDS`).
**Alternatives rejected.** `bytea` columns; public containers.
**Consequences.** Local store is Azurite (ADR-017); scanning is ADR-024.
**Status.** Accepted (FROZEN §2.11).

## ADR-015 — Background jobs: Celery + Redis

**Context.** Master §16.14 recorded Celery + Redis as `[PROPOSED]` with no runner selected. Timers (escalation, SLA), import enrichment, embeddings, PDF and package work need a durable worker.
**Decision.** Celery with Redis broker/result backend from BUILD-01 behind job interfaces; Celery beat is the only scheduler. Job list: escalations, SLA timers, report generation, document parsing/indexing, embedding, Excel import enrichment, package export, retention, notification fan-out, outbox publishing (D-241). Detail in ADR-020.
**Alternatives rejected.** arq/taskiq (lighter, but a second migration later); APScheduler in-process (lost on replica restart).
**Consequences.** Worker container carries Chromium, sentence-transformers and faster-whisper (ADR-023, ADR-029).
**Status.** Accepted (closes master §16.14 ADR-015 `[PROPOSED]`/`[NOT DISCUSSED]`; FROZEN §2.9, D-241).

## ADR-016 — Figma-first interface with a formal gate

**Context.** The product's value is information hierarchy under pressure (10-second comprehension rule).
**Decision.** Figma clickable prototype and UI/UX sign-off are release gates. Sequencing per D-231: BUILD-01…11 and the web shell proceed; BUILD-12/13 and visual-heavy parts of later increments wait for sign-off. the Figma integration if available, otherwise manual token export.
**Alternatives rejected.** Code-first UI with retroactive design review.
**Consequences.** VERIFY.md A7.10 and B2.3/B2.4.
**Status.** Accepted (FROZEN §15.1–2, D-231, D-261).

---

## ADR-017 — Azurite locally, Azure Blob in Azure, one `ObjectStore` adapter (D-217)

**Context.** FROZEN §2.11 said "MinIO-compatible local object storage". MinIO speaks S3; production speaks the Azure Blob API. Two protocols mean two adapters, two test matrices and a divergence exactly where malware-scan status and SAS semantics matter.
**Decision.** `mcr.microsoft.com/azure-storage/azurite` (blob service only) in Compose; Azure Blob Storage in the cloud; a single `ObjectStore` adapter implemented on `azure-storage-blob`. Connection string is the well-known `devstoreaccount1` locally and a Key Vault reference in Azure. Containers: `evidence`, `documents`, `packages`.
**Alternatives rejected.** MinIO + S3 adapter locally and Blob adapter in prod; MinIO with an S3-compatibility gateway in front of Blob.
**Consequences.** Supersedes FROZEN §2.11 / bundle rules file "MinIO" wording. Integration tests run against Azurite (testcontainers or the compose service). Defender for Storage has no local equivalent, hence ADR-024's mock scanner.
**Status.** Accepted.

## ADR-018 — FastAPI owns authentication and the session (D-239)

**Context.** Entra OIDC is primary, Local accounts are a fallback, WebSockets need the same identity, and no access token may live in browser JavaScript.
**Decision.** FastAPI terminates OIDC (authorization-code flow via `authlib`), stores the session in Redis, and issues an HttpOnly, SameSite=Lax cookie (Secure outside `local`). Next.js is a same-origin client: `/api/*` rewrites to FastAPI. CSRF is a double-submit token on every state-changing request. Local login is another route into the same session/RBAC. WebSocket upgrade validates the same cookie. Reauth grants (`reauth_grants`, 5-minute window) gate high-risk commands. Session policy: 8 h absolute / 30 min idle (D-235).
**Alternatives rejected.** Auth.js in Next.js forwarding a JWT as bearer (two identity code paths, token in JS runtime); MSAL in the browser (tokens in JS, Local fallback awkward).
**Consequences.** Tables `sessions`, `reauth_grants`, `local_credentials` in migration 0002 (ADR-026). CORS allowlist is explicit and small because the browser only ever talks to its own origin.
**Status.** Accepted.

## ADR-019 — Toolchain: Python 3.12 + uv, Node 22 LTS + pnpm workspaces (D-240)

**Context.** Reproducible builds with pinned lockfiles, fast installs, and a monorepo with `apps/web`, `apps/api`, `packages/*`.
**Decision.** `uv` (project + lockfile, `uv run` for every Python command), `pnpm` workspaces without Turborepo, Node 22 LTS, Next.js App Router + TypeScript strict pinned exactly at BUILD-01, Ruff (lint + format), Pyright, ESLint, Prettier. Lockfiles committed; CI uses `--frozen-lockfile` / `uv sync --frozen`.
**Alternatives rejected.** Poetry/pip-tools (slower, weaker workspace story); npm (no strict hoisting, slower); Turborepo (unneeded for three packages).
**Consequences.** Makefile invokes `uv run` from `apps/api` and `pnpm --filter web`; the security script exports `requirements.txt` from `uv.lock` for pip-audit.
**Status.** Accepted.

## ADR-020 — Celery + Redis from BUILD-01 behind job interfaces (D-241)

**Context.** Restates ADR-015 with implementation shape.
**Decision.** `app/jobs/celery_app.py` defines the app; domain modules depend on a `JobDispatcher` port, never on Celery directly. Beat schedules: escalation/SLA sweeps (every 30 s), outbox drain (every 1 s or notify-driven), retention (daily). Redis DB 1 broker / DB 2 results / DB 0 sessions+pub-sub. Locally one container runs `worker --beat`; in Azure the worker app runs N replicas plus one beat replica (BICEP_PLAN).
**Alternatives rejected.** Second scheduler (APScheduler/cron) alongside Celery — explicitly forbidden.
**Consequences.** Celery integration tests use the `test` compose profile Redis (6380) or an ephemeral container (ADR-027).
**Status.** Accepted.

## ADR-021 — Real-time via transactional outbox → Redis pub/sub → WebSocket (D-242)

**Context.** Multiple API replicas each hold WebSocket connections; a commit on replica A must reach clients on replica B, and no event may be published for a rolled-back transaction.
**Decision.** Commands write the domain change, the `audit_events` row and an `outbox_events` row in one PostgreSQL transaction. A worker job drains `outbox_events` (ordered, at-least-once, marks `published_at`) and publishes to Redis channel `drcc.rt.<event_id>`. Every API replica subscribes on first client connect per Event and forwards typed messages from the catalog (D-234). Clients invalidate TanStack Query caches; on reconnect they refetch REST. Container Apps session affinity MAY be enabled for connection stability; correctness never depends on it.
**Alternatives rejected.** Publish-after-commit from the request handler (lost on crash between commit and publish); PostgreSQL `LISTEN/NOTIFY` as the backplane (payload limits, connection-pool interaction); sticky-session-only fan-out.
**Consequences.** Acceptance ≤ 10 s locally under POC load (BUILD-11, D-259); duplicate delivery is tolerated by idempotent client cache invalidation.
**Status.** Accepted.

## ADR-022 — Recharts/shadcn chart primitives + D3 for treemap and dial (D-243)

**Context.** FROZEN §2.3 allows "standard chart library + D3 where custom behaviour requires it".
**Decision.** Recharts via shadcn/ui chart wrappers for timelines, bars, sparklines. D3 (`d3-hierarchy` squarified treemap, `d3-shape`/`d3-scale` for the health dial) renders the Application treemap and the dial as accessible SVG with text/icon reinforcement. The treemap is a true squarified treemap sized by effective tier weight, coloured by the four-band `health_band` with overlay rules from D-223.
**Alternatives rejected.** Nivo/visx as the general library (smaller ecosystem in shadcn context); CSS-grid "treemap" (not area-proportional, fails the 10-second rule at 500 Apps).
**Consequences.** `dataviz` conventions: colour never sole cue; dial shows "—" for zero Applications.
**Status.** Accepted.

## ADR-023 — PDF via Playwright/Chromium print in the worker (D-244)

**Context.** Reports must match the in-app report visually (same React components, Light theme) and render charts. The discovery lean was WeasyPrint.
**Decision.** The worker renders `apps/web` route `/print/reports/{versionId}` (service-authenticated, read-only) with headless Chromium through Playwright and stores the PDF in the `packages`/`documents` container. Same path for RACI PDF.
**Alternatives rejected.** WeasyPrint (no JS, charts would need a second SVG pipeline); ReportLab (hand-built layout diverging from the app).
**Consequences.** Worker image includes Chromium (~300 MB); print route reads only the immutable `facts_snapshot` of the report version (D-238); the test suite has a deterministic fixture PDF comparison (BUILD-19).
**Status.** Accepted. Supersedes the WeasyPrint lean.

## ADR-024 — Malware scanning adapters: Defender for Storage in Azure, mock locally (D-245)

**Context.** Uploaded evidence must be scanned before it counts toward completion; there is no local Defender.
**Decision.** `MalwareScanner` port with two adapters. `defender`: Microsoft Defender for Storage on-upload scanning; scan result arrives via Event Grid → `POST /api/v1/internal/malware-result` (HMAC-signed with `MALWARE_WEBHOOK_SECRET`) and updates `malware_scan_status` (`PENDING → CLEAN | INFECTED`). `mock`: EICAR payload → `INFECTED`, anything else → `CLEAN`, synchronous. Evidence with status `PENDING` or `INFECTED` cannot satisfy `evidence_min_count` (D-226); `INFECTED` objects are quarantined (moved to a `quarantine/` prefix, access revoked).
**Alternatives rejected.** ClamAV container locally (heavier, still not Defender-equivalent; may be added as a third adapter later).
**Consequences.** File tests (VERIFY B1.16) run against the mock; a Defender integration check is part of BUILD-22.
**Status.** Accepted.

## ADR-025 — CI security toolchain for a private repository without GHAS (D-246)

**Context.** The repository is private; GitHub Advanced Security is not assumed.
**Decision.** `scripts/security.sh` (via `make security`) runs Semgrep, Bandit, pip-audit, pnpm audit, gitleaks, Syft (SBOM), Grype (image/SBOM scan) and Checkov (Bicep). Locally a missing tool prints `SKIP` with an install hint; CI sets `SECURITY_REQUIRE_ALL=1`. Gate: no unresolved Critical (FROZEN §17.5); patch SLA 7/30/60/90 days. If GHAS becomes available: add CodeQL, Dependabot and secret scanning without removing the OSS set.
**Alternatives rejected.** Trivy as the single scanner (fine, but Syft+Grype gives an SBOM artifact the release needs anyway); relying on GitHub-native scanning alone.
**Consequences.** Reports land in `.security/` (git-ignored); SBOM attached to each tagged release.
**Status.** Accepted.

## ADR-026 — `schema_v1.sql` is Alembic revision 0001; reconciliation is 0002; hand-written models (D-247)

**Context.** The frozen DDL is authoritative but the addendum changed enums and added tables. Autogenerate must never "discover" the domain.
**Decision.** Revision `0001_schema_v1` executes `schema_v1.sql` verbatim (via `op.execute` of the file contents). Revision `0002_reconciliation` applies: `dr_application_status` enum → `NOT_STARTED, RECOVERING, TECHNICAL_VALIDATION, FAILED_OVER, FAILBACK_IN_PROGRESS, COMPLETED` (D-208); Tier-3 comment correction (D-218); new tables `local_credentials`, `password_reset_tokens`, `sessions`, `reauth_grants`, `idempotency_keys`, `outbox_events`, `dr_event_participants`, `file_policy` (D-218, D-235); `target_type` enum (D-216) replacing free-text columns on `comments`, `evidence_items`, `overrides`, `needs_review_items`, `alerts`, `notifications`, while `audit_events.entity_type` stays TEXT under a CHECK on the 31-name audit superset (16 `target_type` labels + 15 admin/identity/configuration names; extending it needs an ADR/spec update and a migration, D-234); `tasks.evidence_required/evidence_min_count/verification_note_required` and `validations.verification_note` (D-226); `work_streams.stream_type` (D-227); `dr_applications.rpo_not_applicable` with a mutual-exclusion CHECK against `rpo_target_minutes` (D-224); `documents.embedding_model/embedding_dim` and the pgvector HNSW index on `document_chunks.embedding` (D-233). SQLAlchemy 2.x models are written by hand to match; CI runs `alembic check` (`make db-check`). Later revisions are authored, reviewed diffs — autogenerate is a drafting aid only.
**Alternatives rejected.** Models as source of truth with autogenerate from BUILD-01 (would silently re-derive frozen DDL); editing `schema_v1.sql` in place (loses provenance).
**Consequences.** `docs/spec/schema_v1.sql` stays byte-stable except the Tier-3 comment; every schema change after 0002 is a numbered revision with downgrade.
**Status.** Accepted.

## ADR-027 — Tests on ephemeral containers: testcontainers-python, Vitest, Playwright, k6 (D-250)

**Context.** Tests must not depend on a developer's shared database or on manual compose state.
**Decision.** pytest + `testcontainers-python` starts one pgvector PostgreSQL (and Redis/Azurite when needed) per test session; migrations run from empty; each test runs in a rolled-back transaction or a fresh schema. The `test` compose profile (5433/6380) is the fallback for CI runners without Docker-in-Docker. Web: Vitest + Testing Library. E2E: Playwright (`tests/e2e`). Load: k6 (`tests/load`). Retention tests use an injected clock.
**Alternatives rejected.** Sharing the dev compose DB (state leakage); SQLite for unit tests (enum/tsvector/vector divergence); mocking the DB layer.
**Consequences.** `make test-api` needs Docker; pure-domain tests (`-m domain`) do not touch containers and stay fast.
**Status.** Accepted.

## ADR-028 — `packages/contracts` generated from OpenAPI with drift check (D-251)

**Context.** The web client must match the API exactly; hand-copied types drift.
**Decision.** `make contracts` exports FastAPI's OpenAPI document to `packages/contracts/openapi.json`, runs `openapi-typescript` to produce `packages/contracts/src/api.d.ts`, plus the canonical enum catalogs (notification/alert/severity/WebSocket/timeline types, D-234). CI fails when the committed output differs. Zod schemas exist only for UI/form/runtime validation and are derived from, never a copy of, the generated types.
**Alternatives rejected.** Hand-written Zod schemas as the contract; tRPC-style runtime coupling.
**Consequences.** Every API change is visible as a `packages/contracts` diff in the PR.
**Status.** Accepted.

## ADR-029 — sentence-transformers embeddings and faster-whisper in the worker (D-232, D-233)

**Context.** `nomic-embed-text-v1.5` (768 d) is frozen but its hosting was not; Anthropic has no embeddings API; speech-to-text had no first provider.
**Decision.** Embeddings: `sentence-transformers` runs `nomic-embed-text-v1.5` in-process in the worker (CPU, ~275 MB, cached in the `model-cache` volume / Azure Files); identical locally and in the Azure POC; `EmbeddingProvider` port allows hosted providers later; a model or dimension change bumps `embedding_model_version` and schedules re-embedding — mixed vectors are never queried together. Speech: `faster-whisper` (`base` model, CPU int8) behind `SpeechProvider`; Azure AI Speech is a future adapter; transcripts are returned as editable text and never execute commands directly.
**Alternatives rejected.** Nomic Atlas API / Ollama sidecar (extra service, egress); Azure AI Speech first (needs Azure before BUILD-17; D-248 keeps BUILD-01…21 local).
**Consequences.** Worker image is large; embedding/transcription latency is acceptable for background use; audio is not retained by default (SECURITY_REVIEW §9).
**Status.** Accepted.

## ADR-030 — `Idempotency-Key` on every command POST; Manager precedence as a domain override (D-214, D-215)

**Context.** Two rules that shape every command test. API_CONTRACT's "required for high-risk" wording and FROZEN §4.14's "Manager wins" needed exact semantics.
**Decision.**
1. `Idempotency-Key` (UUID) is required on every command POST; missing → `400 IDEMPOTENCY_KEY_REQUIRED`. Keys are stored in `idempotency_keys` (user, route, key, request hash, response, 24 h TTL). Same key + same request → stored response replayed; same key + different request → `409 IDEMPOTENCY_KEY_REUSED`. GETs are exempt; the pre-signed upload handshake may be exempt, but the domain command that attaches the artifact is not.
2. Optimistic concurrency: stale `expected_version` → `409 CONCURRENCY_CONFLICT`, with one domain override: a Team Manager assigning a Task owned by their Team with a stale version, where the intervening change was an assignment by a DR Coordinator/Global Admin, is accepted; version increments; both actions are audited; the override is recorded as `MANAGER_PRECEDENCE` in `overrides`; affected users are notified. Every other stale write is a 409.
**Alternatives rejected.** Optional idempotency on low-risk commands (retry storms during a live DR are exactly the low-risk commands); treating Manager precedence as "first writer wins" (reads as a lost update for the Manager, contradicting FROZEN §4.14).
**Consequences.** Client SDK generates keys automatically; API test set includes "idempotent retry" for every command; assignment tests include the precedence matrix (Manager/Coordinator/Executor × own/other Team).
**Status.** Accepted.

---

## Index

| ADR | Title | Source |
|---|---|---|
| 001 | Modular monolith | FROZEN §2.5 |
| 002 | PostgreSQL | FROZEN §2.6 |
| 003 | pgvector over Pinecone | FROZEN §2.8 |
| 004 | pgvector over Chroma | FROZEN §2.8 |
| 005 | Hybrid search, structured authoritative | FROZEN §2.7, §11 |
| 006 | Manual-first AI | FROZEN §1.4, §11 |
| 007 | Fixed Owning Team | FROZEN §4.13 |
| 008 | Structured Blocker | D-211, D-252 |
| 009 | First-class Milestone | FROZEN §7.13 |
| 010 | Baseline + audit | FROZEN §6.5 |
| 011 | WebSockets | FROZEN §2.10 |
| 012 | Azure Container Apps | FROZEN §2.12 |
| 013 | Explicit command APIs | D-207 |
| 014 | Blob for evidence | FROZEN §2.11 |
| 015 | Celery + Redis | D-241 |
| 016 | Figma-first | D-231, D-261 |
| 017 | Azurite | D-217 |
| 018 | FastAPI-owned auth/session | D-239 |
| 019 | pnpm + uv toolchain | D-240 |
| 020 | Celery + Redis implementation | D-241 |
| 021 | Outbox + Redis pub/sub real-time | D-242 |
| 022 | Recharts + D3 | D-243 |
| 023 | Playwright PDF | D-244 |
| 024 | Malware scan adapters | D-245 |
| 025 | CI security toolchain | D-246 |
| 026 | schema_v1 as 0001, reconciliation 0002 | D-247 |
| 027 | testcontainers | D-250 |
| 028 | openapi-typescript contracts | D-251 |
| 029 | sentence-transformers + faster-whisper | D-232, D-233 |
| 030 | Idempotency-Key everywhere + Manager precedence | D-214, D-215 |

**Reconciled 2026-09-12 against FREEZE_ADDENDUM.md (D-201…D-261).**

# DR Command Center — V1 Engineering & Test Strategy

## Quality bar

Production-shaped POC code. No throwaway architecture. Every increment includes tests, migrations where needed, audit/authorization checks and documentation.

## Recommended toolchain

- Python: pytest + testcontainers-python (ephemeral pgvector Postgres per session), pytest-asyncio, Hypothesis where property tests help, Ruff, Pyright. (D-250, D-240)
- Web: Vitest + Testing Library, ESLint, TypeScript strict. (D-250)
- E2E: Playwright. (D-250)
- Load: k6. (D-250)
- Celery integration: ephemeral Redis or isolated test compose profile; no dependence on a developer's shared DB. (D-250)
- API contracts: `packages/contracts` generated from FastAPI OpenAPI via `openapi-typescript`; CI fails on drift; Zod only for UI/form/runtime validation. (D-251)
- Accessibility: axe integration + manual keyboard/screen-reader review.
- Security: Semgrep, Bandit, pip-audit, pnpm audit, gitleaks, Syft (SBOM), Grype (container), Checkov (Bicep/IaC). (D-246)

## Test layers

### Domain unit tests

Table-driven tests for every Event/Task/Blocker/Issue/NeedsReview/Milestone transition and each invalid transition.

DR Application status transitions (D-208): table covering `NOT_STARTED → RECOVERING → TECHNICAL_VALIDATION → FAILED_OVER → FAILBACK_IN_PROGRESS → COMPLETED`; with `failback_required=false`, `TECHNICAL_VALIDATION → COMPLETED` directly and FAILED_OVER/FAILBACK_IN_PROGRESS are skipped; RTO clock stops on `TECHNICAL_VALIDATION → FAILED_OVER` (or `→ COMPLETED`); every other edge rejected. (D-208)

Blocker commands (D-211): `assign → ASSIGNED`, `start → IN_PROGRESS`, `resolve → RESOLVED`, `verify → VERIFIED` then `CLOSED` atomically with both transitions audited; Task returns to `IN_PROGRESS` on verify/close and reaches `READY_FOR_VALIDATION` only via explicit `submit-validation`. (D-211, D-252)

Property tests for dependency DAG invariants: no self edge, no cycle, HARD prereq behavior, ADVISORY non-blocking behavior.

### Application-service tests

- Manager own-Team assignment boundary.
- Coordinator/Admin cross-Team authority.
- fixed Owning Team under cross-Team assignment.
- Manager precedence (D-214): Manager submits assignment for own-Team Task with stale `expected_version` where the intervening change was a Coordinator/Global Admin assignment → accepted, version increments, both actions audited, conflict resolution recorded as `MANAGER_PRECEDENCE`, affected users notified. Negative cases: intervening change not an assignment, Task not owned by Manager's Team, actor not a Manager, or intervening actor not Coordinator/Admin → `409 CONCURRENCY_CONFLICT`. (D-214)
- evidence/validation guards: completion requires ≥1 acceptable Evidence Item + verification note + Owner/Lead validation; Executor cannot self-complete; INFECTED/unscanned evidence cannot satisfy completion. (D-209, D-226, D-245)
- baseline immutability vs live edits.
- RTO/RPO calculation and immutable breach.
- `network_cut_at` bounds (D-237): `start-failover` accepts `activated_at ≤ network_cut_at ≤ now()`; value below `activated_at` or in the future rejected; omitted value defaults to server `now()`; applies to PLANNED_DR and REAL_INCIDENT; audited. (D-237)
- health golden numbers (D-223): Task Score = 100 × COMPLETED eligible / eligible (CANCELLED excluded, BLOCKED not penalised); Validation Score 100/0 per current-phase technical validation (failback validation when failback active); App Health = 0.70 × Task + 0.30 × Validation; Event Health = Σ(App Health × tier weight) / Σ(weight) with T0–T4 = 100/75/50/30/20; dial band boundaries 85 / 60 (GREEN / YELLOW / RED) and treemap boundaries 85 / 60 / 40 (GREEN / LIGHT_ORANGE / DARK_ORANGE / RED) tested at exact values and at ±0.01; overlays (RTO breach → RED, RPO breach → RED, SLA/RTO warning → ≥ DARK_ORANGE, active Blocker T0/T1 → ≥ DARK_ORANGE, active Blocker T2–T4 → ≥ LIGHT_ORANGE) only worsen the visual band and never change the numeric value; zero-App Event dial renders "—". (D-223)
- readiness rules (D-224): each catalog rule evaluates HARD_STOP / WARNING / OFF per global configuration; dependency-graph-acyclic is HARD_STOP and not configurable; Coordinator override only where policy permits and always records a reason; RPO-N/A satisfies the RPO-target HARD_STOP. (D-224)
- monitoring guard (D-227): at close, any non-cancelled Task in a `MONITORING` stream not COMPLETED raises the outstanding monitoring closure warning and blocks `close`; Admin/Coordinator audited closure exception override allows close; non-MONITORING streams do not trigger it. (D-227)
- parent Event cannot `CLOSE` while any non-cancelled child is non-terminal. (D-219)
- App Owner may create PLANNED subset Event but cannot `activate` / `start-failover` without Admin/Coordinator authority. (D-213)
- failback-required feature toggle.
- package tenant binding and integrity (DEK wrapped by KV/dev key; manifest `content_hash` SHA-256; cross-tenant rejected; Dev→Test allowed when tenant IDs match). (D-230)

### Repository/integration tests

Use real PostgreSQL+pgvector container via testcontainers-python, ephemeral per session. (D-250) Test Alembic upgrade from blank DB and representative previous revision; `alembic check` model↔migration drift gate. (D-247) Test optimistic concurrency and append-only audit permissions. Participant auto-enrolment rules populate `dr_event_participants` for each trigger. (D-222)

### API tests

Every command has success, unauthorized, forbidden, guard failure, version conflict, idempotent retry and malformed-input coverage.

- Missing `Idempotency-Key` on any command POST → 400 `IDEMPOTENCY_KEY_REQUIRED`; GET/queries unaffected; same key replayed within 24 h returns the original result. (D-215)
- `POST /blockers/{id}/start` exists and transitions `ASSIGNED → IN_PROGRESS`. (D-211)
- Not-V1 endpoints (Event pause/resume, Task skip, `/tasks/{id}/complete`, generic `/validations/{id}/approve|reject`, generic `/ai/chat`) are absent. (D-207)

### AI/security tests

- AI disabled: full DR workflow passes.
- Prompt injection embedded in documents cannot call actions or elevate permission.
- User A cannot retrieve semantic chunks inaccessible to A.
- Act cannot bypass RBAC/transition guard/reauth.
- Provider failure degrades gracefully.
- sensitive prompt content not written into standard telemetry.

### File tests

- allowlisted file accepted.
- blocked extension/MIME rejected.
- >100MB default rejected.
- malicious scan result quarantined/rejected.
- external link evidence handled as reference.

### Frontend tests

- My DR derived Ready logic rendering.
- Task state board and invalid drag/drop rollback.
- treemap/dial explanations.
- real-time reconnection/refetch.
- concurrent update conflict UX.
- Light/Dark persistence.
- local/Event timezone toggle.
- AI unavailable state.
- Coming Soon Teams/Slack versus active Email configuration.

## E2E golden scenario

1. Global Admin/Coordinator creates East US 2 regional DR.
2. Imports runbook Excel and manually corrects AI suggestions.
3. Scope contains Tier 0–4 Applications and explicit Work Streams.
4. Readiness checks verify failover/failback/owners/milestones.
5. Start DR captures baseline and common network-cut time.
6. Network gate confirms, Storage begins, DB work fans out.
7. One DB Task blocks on Storage; unrelated Applications continue.
8. Blocker routes/escalates and is resolved/verified.
9. Cross-Team engineer helps without Owning Team changing.
10. App validates before RTO; another breaches and acknowledges reason.
11. RPO passes for one Application and fails for another.
12. Monitoring warning prevents closure until cleared/excepted.
13. Start Failback explicitly; execute independent failback plan.
14. Close DR.
15. Generate/edit/publish report and exports.
16. Ask AI why health changed and inspect cited evidence.
17. Repeat core execution with AI disabled.

## POC load profile

- 500 Applications.
- 5,000 Tasks.
- 200 concurrent users.
- realistic comments/blockers/evidence metadata/document chunks.
- dashboard usable within ~2 seconds under normal POC conditions.
- normal API average <500 ms.
- committed live changes visible to connected clients <=10 seconds.

## Retention testing

Use injected clock and short policy durations. Tests prove 7y/3y policy behavior logically without waiting real time. Legal hold must suppress purge in test.

## Release gates

- all unit/integration/API/E2E tests green
- POC load targets pass
- AI-disabled E2E passes
- WCAG 2.1 AA automated/manual gate passes
- 10-second UX comprehension test signed off
- migration dry run passes
- backup/restore drill passes
- rollback drill passes
- SAST/dependency/secret/SBOM/container/IaC scans pass (Semgrep, Bandit, pip-audit, pnpm audit, gitleaks, Syft, Grype, Checkov) (D-246)
- contracts drift check passes (D-251)
- no unaccepted Critical vulnerability
- audit completeness assertion passes for all privileged/state-changing golden-scenario operations

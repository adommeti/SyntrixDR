# DR Command Center — VERIFY.md

**Status:** Canonical companion (regenerated 2026-09-12 from the reconciled canon, D-220).
**Use:** Complete Part A for every BUILD-NN increment before claiming it done (project rule: verify before claiming done). Complete Part B once, at BUILD-24, and again for any production release. Paste the Part C evidence table into the PR description.

An item is either checked with evidence (a command that was run and its result, a test id, a file path) or marked `N/A — <reason>`. "Skipped" is not a state; a skipped failing test never counts as green (project testing rules).

---

## Part A — Per-increment checklist

Run in this order; stop at the first failure.

### A.1 Automated gate

```bash
make verify          # lint + typecheck + test + db-check + contracts
```

| # | Check | Command / evidence | Done |
|---|---|---|---|
| A1.1 | Lint clean (ruff, eslint, prettier) | `make lint` | ☐ |
| A1.2 | Types clean (pyright, tsc) | `make typecheck` | ☐ |
| A1.3 | All API tests green, no `skip`/`xfail` added to hide a failure | `make test-api` — paste summary line | ☐ |
| A1.4 | All web tests green | `make test-web` | ☐ |
| A1.5 | Migration up-from-empty, `alembic check` clean, downgrade one + re-upgrade | `make db-check` (D-247) | ☐ |
| A1.6 | Contracts regenerated, zero drift | `make contracts` (D-251) | ☐ |

### A.2 Domain rules

| # | Check | Evidence | Done |
|---|---|---|---|
| A2.1 | Every new domain rule has a table-driven unit test (marker `domain`) | test ids | ☐ |
| A2.2 | Every new/changed state transition has a success test **and** an invalid-transition test that asserts the error code (`*_TRANSITION_NOT_ALLOWED`) (marker `transitions`) | test ids | ☐ |
| A2.3 | Transitions live only in `apps/api/app/<module>/transition_service.py`; no lifecycle `status` write anywhere else | `grep -rn "status =" apps/api/app --include=*.py \| grep -v transition_service` is empty for lifecycle entities | ☐ |
| A2.4 | Guards: HARD dependency blocks Start; ADVISORY warns; self-edge and cycle rejected (when touching tasks/dependencies) | test ids | ☐ |
| A2.5 | Frozen numbers unchanged: tier defaults 30/60/120/240/1440, weights 100/75/50/30/20, health 70/30, bands ≥85/60–84.99/<60, treemap ≥85/60/40, no T0 cap (when touching health/tiers) | golden test ids (D-223) | ☐ |

### A.3 Authorization

| # | Check | Evidence | Done |
|---|---|---|---|
| A3.1 | Every new command endpoint has tests for: success, 401 unauthenticated, 403 wrong role, 403 right role wrong scope (IDOR — foreign Event/Team/App UUID), 404-vs-403 policy consistent | test ids (marker `auth`) | ☐ |
| A3.2 | Event-participant visibility enforced server-side for new reads; non-participant gets 403/404, never an empty 200 that leaks existence | test ids | ☐ |
| A3.3 | Manager own-Team boundary / Coordinator cross-Team authority respected (when touching assignment) | test ids | ☐ |
| A3.4 | High-risk commands require reauth grant within 5 min and are rejected without it (when touching the high-risk list, SECURITY_REVIEW §3) | test ids | ☐ |
| A3.5 | AI Act cannot reach a command the invoking user cannot call manually; excluded commands rejected in every profile (when touching AI) | test ids | ☐ |

### A.4 Concurrency and idempotency

| # | Check | Evidence | Done |
|---|---|---|---|
| A4.1 | Stale `expected_version` returns `409 CONCURRENCY_CONFLICT`; version increments on success | test ids | ☐ |
| A4.2 | Manager precedence (D-214) accepted, both actions audited, resolution `MANAGER_PRECEDENCE`, affected users notified — only for the exact case (Manager, own Team, intervening Coordinator/Admin assignment) | test ids (when touching assignment) | ☐ |
| A4.3 | Missing `Idempotency-Key` on a command POST → `400 IDEMPOTENCY_KEY_REQUIRED`; replay with same key returns the original result without a second side effect; key expires after 24 h (injected clock) | test ids (D-215) | ☐ |
| A4.4 | Parallel submissions of the same command with the same key produce exactly one audit event | test id | ☐ |

### A.5 Side effects

| # | Check | Evidence | Done |
|---|---|---|---|
| A5.1 | Every state-changing command writes exactly one `audit_events` row with actor, entity, action, before/after; assertion present in the command's test | test ids | ☐ |
| A5.2 | Every state-changing command writes an `outbox_events` row in the same transaction; rollback leaves neither audit nor outbox row | test ids (D-242) | ☐ |
| A5.3 | Real-time: outbox drained → Redis publish → WebSocket event type from the canonical catalog in `packages/contracts` (D-234); client receives within 10 s locally | Playwright test id or `make e2e` log (from BUILD-11) | ☐ |
| A5.4 | Notifications/alerts created use catalog types/severities only | test ids | ☐ |
| A5.5 | Email failure does not roll back the committed command | test id (when touching email) | ☐ |

### A.6 Data and migrations

| # | Check | Evidence | Done |
|---|---|---|---|
| A6.1 | New revision is hand-reviewed; autogenerate did not rename/redefine frozen enums or tables | reviewer initials | ☐ |
| A6.2 | Revision is reversible (`downgrade` implemented, exercised by `make db-check`) | `make db-check` | ☐ |
| A6.3 | Destructive change follows expand/migrate/contract (OPERATIONS.md Rollback) or is explicitly justified | note | ☐ |
| A6.4 | `drcc_readonly` can still `SELECT` new tables (default privileges) | `psql "$DATABASE_URL_RO" -c "select 1 from <new_table> limit 1"` | ☐ |

### A.7 UI states (any increment that adds or changes a screen)

| # | Check | Evidence | Done |
|---|---|---|---|
| A7.1 | Loading state designed and tested (skeleton, not spinner-only) | Vitest/Playwright id | ☐ |
| A7.2 | Error state shows the safe error envelope message + correlation id; no stack trace | test id | ☐ |
| A7.3 | Degraded state when API/WebSocket unavailable: read-only banner, no fake writes (FROZEN §3.9) | test id | ☐ |
| A7.4 | AI-off state: AI panel/actions hidden or disabled with explanation; page fully usable | test id with `AI_ENABLED=false` | ☐ |
| A7.5 | Empty state designed (zero Applications → dial shows "—", D-223) | test id | ☐ |
| A7.6 | Conflict UX: 409 surfaces "changed by <actor>, reload" with no silent overwrite | test id | ☐ |
| A7.7 | Color never the only cue; keyboard reachable; axe scan clean for the route | axe report | ☐ |
| A7.8 | Light + Dark render; preference persists | test id | ☐ |
| A7.9 | Timezone toggle (Event vs local) applied to every timestamp on the screen | test id | ☐ |
| A7.10 | Figma gate: visually final Command Center/My DR work only after sign-off (D-231) | sign-off reference or "shell only" | ☐ |

### A.8 Security hygiene

| # | Check | Evidence | Done |
|---|---|---|---|
| A8.1 | No secret in code, tests, fixtures, logs or `.env.example` | `make security` (gitleaks) | ☐ |
| A8.2 | New file inputs validated server-side (allowlist, size, MIME sniff, scan status) | test ids (when touching uploads) | ☐ |
| A8.3 | New logs contain no prompt bodies, document text, passwords or tokens | grep of log calls | ☐ |
| A8.4 | Retrieved/AI content treated as untrusted; permission filter before retrieval | test ids (when touching search/AI) | ☐ |

### A.9 Documentation

| # | Check | Evidence | Done |
|---|---|---|---|
| A9.1 | `API_CONTRACT.md` / `DATA_MODEL.md` / `STATE_MACHINES.md` unchanged **or** changed via a `spec/<slug>` PR owned by the product owner (D-205; never silently) | PR link or "unchanged" | ☐ |
| A9.2 | `docs/adr/` updated if an engineering decision was made or changed | ADR id | ☐ |
| A9.3 | `TRACEABILITY_MATRIX.md` row(s) for this BUILD point at real test markers/ids | diff | ☐ |
| A9.4 | Handoff note written for lettered sessions (`BUILD-06a/b/c`, D-203) | path | ☐ |
| A9.5 | Independent second-opinion review verdict pasted into PR for BUILD-06, 08, 10, 16, 20 (D-204) | verdict | ☐ |

---

## Part B — Release gate (BUILD-24 and every production release)

Sources: TEST_STRATEGY.md "Release gates", SECURITY_REVIEW.md §15, FROZEN_DECISIONS §16–17, D-260/D-261.

```bash
make release-verify   # verify + security + e2e + load-smoke + AI-off e2e
```

### B.1 Automated

| # | Gate | Source | Evidence | Pass |
|---|---|---|---|---|
| B1.1 | All unit/integration/API/E2E tests green | TEST_STRATEGY | `make release-verify` log | ☐ |
| B1.2 | Golden E2E scenario (17 steps) passes with AI on | TEST_STRATEGY | Playwright report | ☐ |
| B1.3 | Golden E2E passes with `AI_ENABLED=false` | FROZEN §16.9 | Playwright report (`@ai-off`) | ☐ |
| B1.4 | POC load: 500 Apps / 5,000 Tasks / 200 users; API avg < 500 ms; dashboard usable ~2 s; live propagation ≤ 10 s | FROZEN §16, D-259 | k6 summary + Playwright timing | ☐ |
| B1.5 | Migration dry run on a copy of the previous release DB | TEST_STRATEGY | log | ☐ |
| B1.6 | SAST (Semgrep, Bandit) — no unaccepted Critical | D-246 | `make security` | ☐ |
| B1.7 | Dependency scan (pip-audit, pnpm audit) — no unaccepted Critical | D-246 | `make security` | ☐ |
| B1.8 | Secret scan (gitleaks) clean, including history | D-246 | `make security` | ☐ |
| B1.9 | SBOM generated (Syft) and attached to the release | D-246 | artifact path | ☐ |
| B1.10 | Container image scan (Grype) — no unaccepted Critical | D-246 | `SECURITY_IMAGES=... make security` | ☐ |
| B1.11 | IaC scan (Checkov on Bicep) clean | D-246 | `make security` | ☐ |
| B1.12 | Audit completeness assertion: every privileged/state-changing golden-scenario operation has an audit row | TEST_STRATEGY, SECURITY §15 | test id | ☐ |
| B1.13 | RBAC/IDOR suite green | SECURITY §15 | `make test-auth` | ☐ |
| B1.14 | High-risk reauth suite green | SECURITY §15 | test ids | ☐ |
| B1.15 | AI tool-permission / prompt-injection suite green (inaccessible chunk, injected instruction, Act bypass attempts) | SECURITY §15 | test ids | ☐ |
| B1.16 | File scan suite green (allowlisted, blocked ext/MIME, >100 MB, EICAR → INFECTED cannot satisfy completion, link as reference) | SECURITY §15 | test ids | ☐ |
| B1.17 | Package negative suite green (wrong tenant, tampered, wrong key, round trip) | SECURITY §15, BUILD-20 | test ids | ☐ |
| B1.18 | Retention suite green with injected clock; legal hold suppresses purge | FROZEN §13.12 | test ids | ☐ |
| B1.19 | WCAG 2.1 AA automated (axe) clean on every route | FROZEN §15.4 | report | ☐ |

### B.2 Manual / sign-off

| # | Gate | Source | Owner | Evidence | Pass |
|---|---|---|---|---|---|
| B2.1 | Threat model reviewed against SECURITY_REVIEW §14 for this release | SECURITY §15 | security reviewer | doc link | ☐ |
| B2.2 | WCAG 2.1 AA manual keyboard + screen-reader pass | FROZEN §16.11 | UX reviewer | notes | ☐ |
| B2.3 | 10-second comprehension test signed off on the live Command Center | D-261 | product owner | sign-off | ☐ |
| B2.4 | Figma / UI-UX sign-off | D-231, D-261 | product owner | sign-off | ☐ |
| B2.5 | Backup/restore drill passed; Command Center RTO ≤ 60 min, RPO ≤ 15 min recorded | FROZEN §3.8, OPERATIONS | ops | drill record | ☐ |
| B2.6 | Application rollback drill passed (previous immutable image) | FROZEN §3.8 | ops | drill record | ☐ |
| B2.7 | Production secret scan clean; no Local production account pre-created | SECURITY §15, FROZEN §4.3 | security | record | ☐ |
| B2.8 | Privacy/provider review completed before production AI enablement | SECURITY §15 | security | record | ☐ |
| B2.9 | No unaccepted Critical vulnerability; any exception recorded with approver and expiry | FROZEN §17.5 | privileged approver | exception record or "none" | ☐ |
| B2.10 | Review-board sign-off (BUILD-24 acceptance) | BUILD_PROMPTS BUILD-24 | review board | sign-off | ☐ |

---

## Part C — PR evidence table (paste into the PR description)

```markdown
### Verification evidence — BUILD-NN <slug>

| Check | Result | Evidence |
|---|---|---|
| make verify | PASS / FAIL | `<paste last 3 lines>` |
| Domain rule tests (`-m domain`) | n passed | `tests/domain/test_<x>.py::<id>` |
| Transition tests incl. invalid (`-m transitions`) | n passed | ids |
| Authorization tests (`-m auth`) 401/403/IDOR | n passed | ids |
| Concurrency (409 / manager precedence) | n passed / N/A | ids |
| Idempotency-Key required + replay | n passed | ids |
| Audit assertion on every command | n passed | ids |
| Outbox + real-time event | n passed / N/A | ids or Playwright report |
| make db-check (up-from-empty, alembic check, down/up) | PASS | log line |
| make contracts (drift) | PASS | log line |
| UI states loading/error/degraded/AI-off/empty | covered / N/A | test ids |
| axe scan for changed routes | clean / N/A | report |
| make security | PASS (skipped: <tools>) | summary line |
| Spec docs | unchanged / spec PR #<n> | link |
| ADR | none / ADR-0NN | link |
| Independent second-opinion review (06/08/10/16/20) | verdict | link |
| Skipped or xfail tests added | none | — |
| Handoff notes (lettered sessions) | N/A / path | — |
```

---

## Marker conventions (pytest)

| Marker | Meaning | Make target |
|---|---|---|
| `auth` | authorization matrix, IDOR, reauth, session | `make test-auth` |
| `domain` | pure domain rules, formulas, DAG invariants | `make test-domain` |
| `transitions` | state machines incl. invalid transitions | `make test-transitions` |
| `integration` | real PostgreSQL (testcontainers), Redis, Azurite | `make test-api` |
| `api` | HTTP contract: success/401/403/guard/409/idempotent/malformed | `make test-api` |
| `ai` | provider adapters, injection, Act guards | `make test-api` |
| `files` | upload allowlist/size/MIME/scan | `make test-api` |
| `slow` | > 5 s; excluded from the default `make test-api` in pre-commit hooks | `make test-api PYTEST_ARGS="-m slow"` |

Playwright tags: `@golden` (17-step scenario), `@ai-off`, `@a11y`, `@realtime`.

**Reconciled 2026-09-12 against FREEZE_ADDENDUM.md (D-201…D-261).**

# DR Command Center — Frozen 10-Week POC Delivery Plan

## Definition of POC complete

One realistic regional DR can run end to end with readiness, baseline, Work Streams, application recovery, dependencies/gates, Blockers/Issues, evidence, RTO/RPO, monitoring, failback, real-time dashboards, audit, report/export and AI-assist; the same critical execution passes with AI off.

## Week-to-BUILD map (D-203, D-206)

| Week | BUILD increments |
|---|---|
| 0 | Design System / Figma Gate (BUILD-00, parallel lane; does not start the clock) (D-231, D-206) |
| 1 | BUILD-01 (clock starts here) (D-206) |
| 2 | BUILD-02, BUILD-03 |
| 3 | BUILD-04, BUILD-05 |
| 4 | BUILD-06, BUILD-07, BUILD-08 |
| 5 | BUILD-09, BUILD-10 |
| 6 | BUILD-11, BUILD-12, BUILD-13 (12/13 gated on Figma sign-off) (D-231) |
| 7 | BUILD-14 |
| 8 | BUILD-15, BUILD-16, BUILD-17, BUILD-18 |
| 9 | BUILD-19, BUILD-20, BUILD-21 |
| 10 | BUILD-22, BUILD-23, BUILD-24 |

Each BUILD-NN is one PR/milestone; tag `v0.NN.0` after merge (D-202, D-203). An independent second-opinion review is mandatory for BUILD-06, 08, 10, 16, 20 (D-204).

## Week 0 / pre-build design gate

- freeze docs accepted (FROZEN_DECISIONS.md + FREEZE_ADDENDUM.md D-201…D-261) (D-201)
- Design System / Figma Gate: Figma is not complete; the gate precedes visually final Command Center work. BUILD-01…11 and the web shell (layout, auth shell, nav, theme, responsive grid, design-token plumbing, generic Deep Card framework) proceed without it; BUILD-12/13 and visual-heavy parts of later UI increments require Figma sign-off. the Figma integration if available, else manual token export. (D-231)
- Figma deliverables: Command Center, My DR, Application deep view, Task Board, Task deep view, People/resource views, Needs Review, Admin, Reports, Light+Dark, tablet/mobile states, hover/deep-card behaviour, loading/empty/error states. (D-231)
- 10-second comprehension prototype test (core UX acceptance test) (D-261)
- local Mac toolchain validated (Python 3.12, uv, Node 22, pnpm) (D-240)
- Bicep environment skeleton reviewed
- new private GitHub repository started clean; harness mechanics ported, old seam protocol/16-build structure not inherited (D-201, D-246)

Exit: UI/UX and architecture/security reviewers sign off enough to build foundations. The 10-week POC clock does **not** start in Week 0; it starts when BUILD-01 actually begins. No external DR exercise date constrains V1. (D-206)

## Week 1 — foundation (BUILD-01)

- monorepo scaffolding (pnpm workspaces, uv; no Turborepo) (D-240)
- Next.js shell/theme/navigation (web shell not gated by Figma) (D-231)
- FastAPI app/module skeleton
- PostgreSQL/pgvector + Alembic (schema_v1 = 0001; 0002 = reconciliation) (D-247)
- Redis/Azurite/Mailpit local Compose (D-217, D-256)
- Celery + Redis wired behind job interfaces (D-241)
- `packages/contracts` generation via openapi-typescript with CI drift check (D-251)
- Idempotency-Key middleware skeleton (D-215)
- config/secrets pattern
- logging/correlation IDs
- CI lint/type/unit/security baseline (Semgrep, Bandit, pip-audit, pnpm audit, gitleaks, Syft, Grype, Checkov) (D-246)
- testcontainers-python test harness (D-250)

Exit: `make dev` + `make verify` on Mac.

## Week 2 — identity/catalog/RBAC (BUILD-02, BUILD-03)

- Entra auth adapter + dev/local auth
- Users/Teams/memberships/manager hierarchy admin
- roles/scopes/participant visibility
- Application/Tier catalog
- up-to-three System/Application + Business Owners
- skills and contact data
- high-risk reauth abstraction

Exit: authorization matrix tests pass.

## Week 3 — Plans/Event/readiness/import (BUILD-04, BUILD-05)

- reusable Plan/version model
- DR Event canonical lifecycle
- subset Apps + parent/child Event roll-up
- Work Streams
- readiness rules
- Excel import/manual review
- baseline capture

Exit: can prepare and Start a DR with AI disabled.

## Week 4 — Task/dependency execution core (BUILD-06, BUILD-07, BUILD-08)

- canonical Task transitions
- HARD/ADVISORY dependencies + cycle detection
- first-class Milestones/manual gates
- assignment/volunteering/fixed Owning Team
- Blocker lifecycle + Team queue/tags
- Issue/Finding
- comments/@mentions
- audit/outbox/realtime event pipeline

Exit: Network→Storage→DB→Apps selective parallel scenario works.

## Week 5 — RTO/RPO/validation/monitoring/health (BUILD-09, BUILD-10)

- RTO/RPO model/calculation
- validation owner rules
- evidence/verification-note gates
- file storage/allowlist/100MB/scanner adapter (BUILD-09) (D-217, D-225, D-245)
- deterministic forecast
- health 70/30 + tier weighting
- treemap/dial API projections
- SLA/RTO warnings/breach acknowledgement

Exit: recovery outcome metrics and health explain correctly.

## Week 6 — world-class Command Center UI (BUILD-11, BUILD-12, BUILD-13; 12/13 gated on Figma sign-off — D-231)

- Coordinator dashboard
- 500-App treemap stress behavior
- health dial/explanation
- resource sidebar/deep cards
- My DR
- Kanban/filters/labels/saved views
- dependency map
- Blocker/Issue/Needs Review surfaces
- timeline and timezone toggle
- Light/Dark and responsive core screens

Exit: 10-second comprehension test passes against integrated data.

## Week 7 — failback, monitoring and closure (BUILD-14)

- explicit Start Failback
- failback-required feature toggle
- failback targets/forecast
- monitoring warnings/closure guard (BUILD-14) (D-227)
- closure guards/overrides
- CLOSED/CANCELLED terminal behavior

Exit: full manual DR/failback flow closes with evidence/audit.

## Week 8 — search, AI, speech, notifications (BUILD-15, BUILD-16, BUILD-17, BUILD-18)

- FTS + pgvector retrieval
- document chunk/index/re-embed jobs
- Synthetic/Anthropic provider abstraction
- AI Read/Recommend/Act
- Needs Review
- AI summaries feature flag
- speech-to-text adapter/review flow
- functional SMTP Admin config + test email
- Teams/Slack Coming Soon UI

Exit: AI grounded/permission-filtered; AI-off workflow still green.

## Week 9 — reports/export/history (BUILD-19, BUILD-20, BUILD-21)

- report facts snapshot + AI draft narrative
- edit/version/publish
- PDF/CSV/Excel
- RACI export
- package foundations (BUILD-20) (D-230)
- AES-256-GCM full package export/import + integrity/tenant binding
- historical Application comparisons
- retention/legal hold jobs

Exit: auditor-ready package produced and exact/clone import tests pass.

## Week 10 — hardening and POC dry run (BUILD-22, BUILD-23, BUILD-24; Azure credentials first required here — D-248)

- 500 Apps / 5,000 Tasks seed
- 200-user k6 profile
- <=10s realtime propagation validation
- WCAG 2.1 AA audit
- security/prompt-injection/RBAC/IDOR tests
- migration/backup/restore/rollback drill
- SAST/dependency/SBOM/container/IaC scans
- end-to-end regional DR dry run including RTO/RPO and AI-off repetition
- reviewer-board sign-off

Exit: POC acceptance checklist complete.

## Post-POC production hardening

Not part of the 10-week POC commitment. Includes production private networking, production sizing/load margin, provider privacy/legal approval, enterprise release/on-call integration, penetration testing as required, operational runbook rehearsal and production change approval.

## Parallel work lanes

- Figma/design system (BUILD-00 Design System / Figma Gate) progresses alongside BUILD-01…11 and must be signed off before BUILD-12/13 begin. (D-231)
- Azure Bicep and CI can progress alongside domain model.
- Report templates can begin after RTO/RPO/data contracts stabilize.
- AI adapter can begin before UI but cannot define domain behavior.
- Security tests are incremental, not a final-week-only activity.

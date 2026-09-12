# DR Command Center — Open Questions

**Status:** Canonical companion (regenerated 2026-09-12, D-220).

## Zero blocking product questions

**There are zero blocking product questions for V1 implementation after `FREEZE_ADDENDUM.md` (2026-09-12).**

- FROZEN_DECISIONS.md §20 declared the freeze on 2026-09-11.
- Phase 1 discovery (`docs/plan/PHASE1_DISCOVERY.md`) surfaced 14 contradictions (C-01…C-14), 19 missing specifications (M-01…M-19), 13 engineering decisions (E-01…E-13) and 8 process items (P-01…P-08) as 51 questions (Q01…Q51).
- The product owner answered every question in writing; each answer is a `[DECIDED]` record D-201…D-261 in `FREEZE_ADDENDUM.md`, which sits above FROZEN_DECISIONS.md in precedence.
- Remaining tunables (master §22.2: exact MIME allowlist, SMTP host per environment, Redis/Celery sizing, treemap interpolation colours, seed names, cloud SKUs) are configuration or ADR-level engineering choices and may not reopen settled behaviour.

Harness tooling may assert on the sentence in bold above.

## Resolved index — Q → D-record

| Q | Topic | Gap | Resolved by | One-line outcome |
|---|---|---|---|---|
| Q01 | Existing harness scaffold | P-01 | D-201 | New clean repo; port mechanics, drop `# OPEN` protocol and 16-build structure |
| Q02 | Branching and merging | C-12 / P-02 | D-202 | `feat/<milestone>-<slug>`, `fix/<slug>`, squash-merge, linear, tag `v0.NN.0`, Conventional Commits |
| Q03 | Milestone granularity | P-03 | D-203 | 24 BUILD-NN as PR units; lettered sessions merge as one PR |
| Q04 | API surface | C-01 | D-207 | API_CONTRACT.md authoritative; no pause/resume, skip, `/complete`, generic validation, `/ai/chat` |
| Q05 | DR Application status enum | C-02 | D-208 | `NOT_STARTED → RECOVERING → TECHNICAL_VALIDATION → FAILED_OVER → FAILBACK_IN_PROGRESS → COMPLETED` |
| Q06 | Task completion path | C-03 | D-209 | Only via `READY_FOR_VALIDATION → validate` by Owner/Lead; executors never self-complete |
| Q07 | Validation entity | C-04 | D-210 | Persisted `validations` record, no REST resource; `PENDING → APPROVED \| REJECTED` |
| Q08 | Blocker transitions | C-05 | D-211 | Six states, four commands; `POST /blockers/{id}/start` added; verify closes atomically |
| Q09 | Route map | C-06 | D-212 | UI_UX.md + `/plans`, `/dr-events/[id]/reviews`, `/dr-events/[id]/resources`; history is a tab |
| Q10 | Who can create a DR Event | C-07 | D-213 | Admin/Coordinator full; App/System Owner may create PLANNED subset Event only |
| Q11 | Manager-wins semantics | C-08 | D-214 | Explicit domain override, accepted, audited as `MANAGER_PRECEDENCE`; all else 409 |
| Q12 | Idempotency-Key | C-09 | D-215 | Required on every command POST; 24 h retention |
| Q13 | Polymorphic target types | C-10 | D-216 | Closed 16-value `target_type` enum |
| Q14 | Local object storage | C-11 | D-217 | Azurite + `azure-storage-blob`; MinIO superseded |
| Q15 | Schema clean-ups | C-13 | D-218 | Tier-3 comment fixed; proficiency nullable/unused; new tables in migration 0002 |
| Q16 | Parent/child Events | C-14 | D-219 | Parent = reporting container; children own lifecycle; parent cannot close with open child |
| Q17 | Missing bundle files | M-01 | D-220 | Regenerated from reconciled canon (this file and its siblings) |
| Q18 | Excel runbook format | M-02 | D-221 | No rigid format; mapping/review step; fixture columns are test-only |
| Q19 | Event participant definition | M-03 | D-222 | Explicit `dr_event_participants` + auto-enrolment rules; Admin implicit; Auditor/Executive granted |
| Q20 | Health sub-formulas | M-04 | D-223 | Task Score, Validation Score, 70/30, tier weights, dial and treemap bands, overlays, "—" at zero |
| Q21 | Readiness rule catalog | M-05 | D-224 | 13 rules with HARD_STOP/WARNING defaults; acyclic rule not configurable |
| Q22 | Policy keys and defaults | M-06 | D-225 | Full key catalog; `sla.warning_percent=85`; escalation 0/15/30/60/120 |
| Q23 | Where required evidence is declared | M-07 | D-226 | Per-Task `evidence_required`, `evidence_min_count`, `verification_note_required` |
| Q24 | Monitoring warning | M-08 | D-227 | `work_streams.stream_type`; incomplete MONITORING Task blocks close unless closure exception |
| Q25 | AI Act allowlist and profiles | M-09 | D-228 | Allowlist and never-list fixed; profile semantics defined |
| Q26 | RACI export grain | M-10 | D-229 | Two sections: Work Stream RACI and Application RACI |
| Q27 | Package format | M-11 | D-230 | Manifest fields; DEK wrapped by Key Vault/dev key; tenant = Entra GUID / `DRCC_TENANT_ID` |
| Q28 | Figma gate | M-12 / P-04 | D-231 | Design System / Figma Gate before BUILD-12/13; shell proceeds |
| Q29 | Speech-to-text provider | M-13 | D-232 | Local faster-whisper in worker; Azure AI Speech later |
| Q30 | Embedding hosting | M-14 | D-233 | sentence-transformers in worker, 768 d, adapter-based |
| Q31 | Canonical catalogs | M-15 | D-234 | One typed catalog in `packages/contracts` + backend enums |
| Q32 | Local auth parameters | M-16 | D-235 | Argon2id, 12–128 chars, lockout 10/15/30, 8 h/30 min, reauth 5 min, TOTP for Local admin |
| Q33 | Golden seed | M-17 | D-236 | Fictitious data only |
| Q34 | Report/exports | M-18 | D-238 | Versioned facts schema; 13 Excel sheets; CSV Task-level default |
| Q35 | Auth topology | E-01 | D-239 | FastAPI owns OIDC/session; cookie; CSRF; same-origin `/api` |
| Q36 | Tooling | E-02 | D-240 | Python 3.12 + uv, Node 22 + pnpm, no Turborepo, Ruff/Pyright/ESLint/Prettier |
| Q37 | Job runner | E-03 | D-241 | Celery + Redis from BUILD-01 |
| Q38 | Real-time fan-out | E-04 | D-242 | Outbox → worker → Redis pub/sub → API replicas → WebSocket |
| Q39 | Charts | E-05 | D-243 | Recharts/shadcn + D3 treemap/dial |
| Q40 | PDF engine | E-06 | D-244 | Playwright/Chromium print in worker |
| Q41 | Malware scanning | E-07 | D-245 | Defender for Storage in Azure; mock (EICAR) locally |
| Q42 | Repo visibility + security tools | E-08 / P-06 | D-246 | Private; Semgrep, Bandit, pip-audit, pnpm audit, gitleaks, Syft, Grype, Checkov |
| Q43 | Schema source of truth | E-09 | D-247 | schema_v1.sql = 0001; reconciliation = 0002; hand-written models; `alembic check` |
| Q44 | Azure availability | E-10 | D-248 | Needed only for BUILD-22/23; Managed Redis preferred |
| Q45 | AI credentials | E-11 | D-249 | Synthetic first (Kimi K3 if available); Anthropic supported; Admin-configurable |
| Q46 | Test infrastructure | E-12 | D-250 | pytest + testcontainers, Vitest, Playwright, k6 |
| Q47 | Contracts package | E-13 | D-251 | openapi-typescript with CI drift check |
| Q48 | Team and review lane | P-05 | D-204 | Solo implementation; independent second-opinion review on BUILD-06/08/10/16/20 |
| Q49 | Docs layout | P-07 | D-205 | `/docs/spec` (protected), `/docs/plan`, `/docs/adr`, `/docs/build-prompts`, `/docs/reviews` |
| Q50 | Calendar | P-08 | D-206 | Clock starts at BUILD-01; no external DR date |
| Q51 | Network-cut timestamp | M-19 | D-237 | Optional `network_cut_at` bounded `activated_at ≤ t ≤ now()`, default `now()` |

Consequential restatements without a Q: D-252…D-261 (Task states, Issue vs Blocker, ownership slots, contact, email, RTO/RPO measurement, retention, NFRs, support/security posture, UI gate).

## Non-blocking tunables (configuration or ADR only)

| Tunable | Where it is set | Constraint |
|---|---|---|
| Exact MIME/extension allowlist | `file.allowlist` policy, `FILE_ALLOWLIST` env | Must stay inside SECURITY_REVIEW §6 (validation + scanning always) |
| SMTP host per environment | Admin `/admin/notifications/email`; Key Vault secret | Credentials never in policy JSON (D-225) |
| Redis/Celery sizing, Container Apps replicas/SKUs | `infra/BICEP_PLAN.md` parameters | Must meet D-259 load targets |
| Treemap interpolation colours within a band | web design tokens (Figma) | Bands and overlays fixed by D-223; WCAG contrast |
| Seed names | `apps/api/app/tooling/seed.py` | Fictitious only (D-236) |
| Chat/embedding model IDs | Admin `/admin/ai`, `SYNTHETIC_MODEL`, `ANTHROPIC_MODEL` | Verified at BUILD-16; never hard-coded (D-249) |
| pytest/Playwright timeouts, k6 stages | test config | Must still prove D-259 targets |

## How to raise a new product question

Code never resolves a product question. If implementation reveals behaviour the canon does not define, or two canonical docs conflict:

1. **Stop at the seam.** Do not pick an interpretation in code. Leave the seam failing (a test marked with the question id, not `skip`) or unimplemented.
2. **Open a spec PR** on branch `spec/<slug>` (D-202 naming; D-205 layout). The product owner is the PR author of record; tooling may draft it but must not merge it and the `/docs/spec/**` protection hook must not be bypassed.
3. **The PR contains exactly:**
   - a new record `D-2nn` appended to `FREEZE_ADDENDUM.md` §A–E in the next free number, formatted `**D-2nn** (Q-new-<slug>, <gap id or "new">) [DECIDED] …`, citing the docs it reconciles and any superseded wording added to §F;
   - the reconciled edits to the affected canonical companions (STATE_MACHINES, DATA_MODEL, API_CONTRACT, RBAC_MATRIX, UI_UX, …), each carrying a `(D-2nn)` tag on the changed line;
   - a row added to the "Resolved index" table above (`Q-new-<slug>`);
   - updated rows in `TRACEABILITY_MATRIX.md` and, if a term changed, `GLOSSARY.md`;
   - a migration note if the answer changes `schema_v1.sql` semantics (new Alembic revision, never an edit to 0001/0002).
4. **Review:** product owner approves the D-record; for security/auth/schema/dependency-transition impact, an independent second-opinion review is required (D-204).
5. **Merge, tag nothing.** Spec PRs do not bump `v0.NN.0`; the next BUILD PR that implements the record references it in its evidence table (VERIFY.md Part C, "Spec docs" row).
6. **Engineering-only choices** (library, sizing, internal structure) that do not change observable product behaviour are ADRs in `docs/adr/` (summarised in `ADRS.md`), not D-records. If in doubt whether a change is product-visible, it is a D-record.

The bold sentence at the top of this file is updated only by a spec PR that adds a record; it is never edited to say a question is "pending".

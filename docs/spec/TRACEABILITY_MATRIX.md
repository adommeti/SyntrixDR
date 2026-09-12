# DR Command Center — Traceability Matrix

**Status:** Canonical companion (regenerated 2026-09-12 from the reconciled canon, D-220).
**Reads:** PRD requirement → canonical doc section that specifies it → BUILD-NN increment(s) that implement it → test layer / pytest marker or Playwright tag that proves it → release gate (VERIFY.md Part B id) that blocks release without it.
**Maintenance:** VERIFY.md A9.3 — each BUILD PR replaces the placeholder test ids in the "Test" column with real ids. A row without a test id is not done.

Test layer key: `domain` `transitions` `auth` `api` `integration` `ai` `files` (pytest markers, VERIFY.md "Marker conventions"); `web` (Vitest); `e2e:@tag` (Playwright); `load` (k6); `manual` (sign-off recorded in VERIFY.md).

---

## PRD §3 — Product outcomes

| # | Outcome | Canonical section | BUILD | Test | Gate |
|---|---|---|---|---|---|
| O-1 | Run a comprehensive regional DR end to end from one system | FROZEN §1.1, §6; STATE_MACHINES "DR Event"; TEST_STRATEGY "E2E golden scenario" | 04, 06, 07, 08, 09, 10, 14, 19, 24 | `e2e:@golden` (17 steps) | B1.2 |
| O-2 | Know within 10 seconds what is failing and what requires action | UI_UX "10-second comprehension rule"; D-261; D-223 | 12, 13 | `manual` 10-second test; `e2e:@a11y`; `web` treemap/dial explanation | B2.3, B2.4 |
| O-3 | Track Task execution without confusing Task completion with Application recovery success | STATE_MACHINES "Task", "Application recovery"; D-208, D-209 | 06, 09, 10 | `transitions` task/app machines; `domain` health separation | B1.1 |
| O-4 | Measure Application RTO and RPO incl. late recovery and data-recovery outcomes | DATA_MODEL "RTO/RPO model"; D-257; FROZEN §9 | 10 | `domain` golden RTO/RPO incl. breach + late recovery | B1.1, B1.12 |
| O-5 | Maintain shared infrastructure Work Streams while preserving application-centric views | D-227; DATA_MODEL "Work Stream"; UI_UX routes `/work-streams`, `/applications` | 06, 12, 13 | `domain` stream_type; `web` route tests | B1.1 |
| O-6 | Parallel work when dependencies permit; isolate blocking to impacted downstream work | FROZEN §7.8–9; DATA_MODEL invariants 5–7 | 06, 08 | `domain` DAG property tests; `transitions` HARD/ADVISORY start guard | B1.1 |
| O-7 | Fixed Owning Team accountability with cross-Team execution help | FROZEN §4.10–13; ADR-007; D-214 | 07 | `auth` assignment matrix; `api` volunteer; `integration` manager precedence | B1.13 |
| O-8 | Explain health/forecast/blockers/resource load in real time | D-223; API `/health/explain`, `/teams/{id}/workload`; D-242 | 10, 11, 12 | `domain` explain payload; `e2e:@realtime` ≤ 10 s | B1.4 |
| O-9 | Auditor-ready history/evidence/reporting without manual reconstruction | SECURITY_REVIEW §11; D-238; D-229 | 09, 19, 21 | `integration` audit completeness; deterministic report fixture | B1.12 |
| O-10 | Fully operable with AI disabled/unavailable | FROZEN §1.4, §11.18; ADR-006 | 16 (and every UI increment) | `e2e:@ai-off`; `ai` provider-failure degradation | B1.3 |

---

## PRD §4.1 — DR planning and readiness

| # | Scope item | Canonical section | BUILD | Test | Gate |
|---|---|---|---|---|---|
| 4.1.1 | Create planned or real DR Events | API `POST /dr-events`; `dr_event_type`; D-213 | 04 | `auth` create authority (Admin/Coordinator/App Owner PLANNED-only); `api` | B1.13 |
| 4.1.2 | Select all Applications or a subset | D-213, FROZEN §6.7; `dr_applications` | 04 | `api` scope; `domain` readiness "≥1 DR Application" | B1.1 |
| 4.1.3 | Parent comprehensive Event rolls up child Events | D-219; STATE_MACHINES parent/child rule | 04, 10, 14 | `domain` aggregation; `transitions` parent close guard | B1.1 |
| 4.1.4 | Reusable versioned Plans instantiate and diverge | FROZEN §6.4; `plans`, `plan_versions`; API `/plans` | 04 | `integration` instantiate + edit does not mutate Plan | B1.1 |
| 4.1.5 | Excel runbook import with AI-assisted mapping and human review | D-221; `import_jobs`; API `/imports/*` | 05 | fixture workbooks; AI-disabled import; Needs Review creation | B1.3 |
| 4.1.6 | Full manual creation/editing of Tasks, Subtasks, Dependencies, Milestones | API Tasks/Dependencies/Milestones; UI_UX plan editor | 05, 06, 07 | `api` CRUD + guard; `web` editor | B1.1 |
| 4.1.7 | Baseline snapshot at DR start/network cut | FROZEN §6.5; ADR-010; D-237 | 04, 10 | `transitions` start-failover captures baseline; `integration` baseline immutability | B1.1 |
| 4.1.8 | Readiness Hard Stops/Warnings/Off; no separate approval chain | D-224; FROZEN §6.9 | 04 | `domain` rule catalog table test; `transitions` activate blocked/overridden | B1.1 |
| 4.1.9 | Failback Plan required by default; toggle permits exception | FROZEN §8.2–3; D-208; D-224 | 04, 14 | `domain` readiness rule; `transitions` `TECHNICAL_VALIDATION → COMPLETED` when `failback_required=false` | B1.1 |

## PRD §4.2 — Shared execution model

| # | Scope item | Canonical section | BUILD | Test | Gate |
|---|---|---|---|---|---|
| 4.2.1 | Explicit Work Streams incl. custom | D-227 `stream_type` | 06 | `api` create; `domain` enum | B1.1 |
| 4.2.2 | Task may belong to both DR Application and Work Stream | `tasks.task_context_ck`; FROZEN §7.7 | 06 | `integration` constraint; `api` | B1.1 |
| 4.2.3 | HARD and ADVISORY Dependencies | FROZEN §7.8; `dependency_strength` | 06 | `domain` property tests; `transitions` start guard | B1.1 |
| 4.2.4 | First-class Milestones with manual confirmation | ADR-009; `milestone_confirmation_mode`; D-225 `milestone.auto_confirm_allowed` | 07 | `transitions` milestone machine; `auth` confirm authority | B1.1 |
| 4.2.5 | Manual handoffs (Network Ready → Storage → Database) | STATE_MACHINES "Milestone"; TEST_STRATEGY golden steps 6 | 07 | `e2e:@golden` step 6 | B1.2 |
| 4.2.6 | Deterministic critical-path forecast | FROZEN §7.10; DATA_MODEL "Target/Forecast" | 10 | `domain` forecast golden tests | B1.1 |

## PRD §4.3 — Task, Blocker and Issue behaviour

| # | Scope item | Canonical section | BUILD | Test | Gate |
|---|---|---|---|---|---|
| 4.3.1 | Task states NOT_STARTED…CANCELLED | STATE_MACHINES "Task"; D-252 | 06 | `transitions` full table incl. every invalid edge | B1.1 |
| 4.3.2 | Derived Ready | FROZEN §7.3 | 06, 13 | `domain` is_ready; `web` My DR tile | B1.1 |
| 4.3.3 | BLOCKED Task has structured Blocker with routing/escalation/resolution/verification | D-211, D-252; `blockers` | 08 | `transitions` blocker machine; escalation-clock tests (injected clock) | B1.1 |
| 4.3.4 | Separate Issue/Finding | D-253; `issue_findings` | 08 | `transitions` issue machine; `domain` non-blocking unless linked | B1.1 |
| 4.3.5 | Required evidence + verification note before protected completion | D-226; D-209; `tasks.evidence_*` | 09 | `transitions` submit-validation guard; `files` scan status blocks | B1.16 |
| 4.3.6 | Any participant may comment and @mention | FROZEN §12.8; API comments | 08 | `auth` participant-scoped comment; cross-Team comment test | B1.13 |

## PRD §4.4 — Resource model

| # | Scope item | Canonical section | BUILD | Test | Gate |
|---|---|---|---|---|---|
| 4.4.1 | Owning Team fixed; Current Assignee may be cross-Team | ADR-007; DATA_MODEL invariant 8 | 07 | `integration` assignment does not change `owning_team_id` | B1.1 |
| 4.4.2 | Managers reassign within Team; Coordinators cross-Team | FROZEN §4.10; RBAC_MATRIX | 07 | `auth` assignment matrix | B1.13 |
| 4.4.3 | Same-Team peers reassign eligible work | FROZEN §4.11 | 07 | `auth` | B1.13 |
| 4.4.4 | Volunteer for cross-Team unassigned work | FROZEN §4.12; API `/volunteer` | 07 | `api` volunteer success/forbidden | B1.13 |
| 4.4.5 | Skills V1; proficiency V2 | D-218; `user_skills` | 02 | `api` skills CRUD | B1.1 |
| 4.4.6 | Dynamic workload from live assignments | FROZEN §19.8; API `/teams/{id}/workload` | 07 | `domain` workload projection | B1.1 |
| 4.4.7 | Manager precedence on stale-version conflict | D-214; ADR-030 | 07 | `integration` concurrency matrix (accept / 409 cases) | B1.1 |

## PRD §4.5 — RTO/RPO and application validation

| # | Scope item | Canonical section | BUILD | Test | Gate |
|---|---|---|---|---|---|
| 4.5.1 | Tier targets 30/60/120/240/1440 | FROZEN §9.2; `tiers` seed | 03 | `domain` tier defaults; `integration` seed | B1.1 |
| 4.5.2 | Regional clock starts at common network cut | D-237; D-257 | 04, 10 | `transitions` network_cut_at bounds; `domain` RTO start | B1.1 |
| 4.5.3 | RTO completes only on technical validation | D-208; STATE_MACHINES app machine | 09, 10 | `transitions` `TECHNICAL_VALIDATION → FAILED_OVER` stops clock | B1.1 |
| 4.5.4 | Late success does not erase breach | FROZEN §9.5; DATA_MODEL invariant 14 | 10 | `domain` late-recovery golden test | B1.1 |
| 4.5.5 | RPO target/reference/recovered/loss/pass-fail | D-257; DATA_MODEL "RTO/RPO model" | 10 | `domain` RPO pass and fail cases | B1.1 |
| 4.5.6 | Application Owner validates app work; Work Stream Lead validates shared work | D-209; RBAC_MATRIX | 09 | `auth` validator authority incl. any owner slot, executor self-validate forbidden | B1.13 |
| 4.5.7 | Business confirmation optional note/checkbox | FROZEN §18.11; `business_confirmed` | 09 | `api` | B1.1 |

## PRD §4.6 — Failback

| # | Scope item | Canonical section | BUILD | Test | Gate |
|---|---|---|---|---|---|
| 4.6.1 | Independent Failback Plan/Tasks/Dependencies | FROZEN §8.1; `plan_type = FAILBACK` | 14 | `integration` failback plan not derived from failover | B1.1 |
| 4.6.2 | Explicit Start Failback by Admin/Coordinator | API `/start-failback`; RBAC | 14 | `auth` + `transitions` + reauth | B1.14 |
| 4.6.3 | Manual gates/handoffs in failback | FROZEN §8.5 | 14 | `e2e:@golden` step 13 | B1.2 |
| 4.6.4 | Independent failback target/forecast/reporting | `failback_target_at`, `failback_forecast_at`; D-238 | 14, 19 | `domain` forecast; report fixture | B1.1 |

## PRD §4.7 — Dashboards and UX

| # | Scope item | Canonical section | BUILD | Test | Gate |
|---|---|---|---|---|---|
| 4.7.1 | Coordinator Command Center (dial, treemap, sidebar, critical panel, timeline, RTO/RPO, AI chat) | UI_UX "Command Center"; FROZEN §15.8; D-243 | 12 | `web` components; `e2e:@a11y`; 500-app seed render | B1.4, B1.19, B2.4 |
| 4.7.2 | Treemap size = criticality; colour Green → Light Orange → Dark Orange → Red | D-223 treemap bands and overlays; ADR-022 | 10, 12 | `domain` band + overlay table; `web` treemap | B1.1 |
| 4.7.3 | Health 70/30; Event tier-weighted | D-223 | 10 | `domain` golden health tests | B1.1 |
| 4.7.4 | Weights 100/75/50/30/20 | FROZEN §10.1; D-218 | 03, 10 | `domain` | B1.1 |
| 4.7.5 | No Tier-0 cap | FROZEN §10.4 | 10 | `domain` negative test (cap absent) | B1.1 |
| 4.7.6 | Dial bands ≥85 / 60–84.99 / <60; "—" at zero Applications | D-223 | 10, 12 | `domain`; `web` empty state | B1.1 |
| 4.7.7 | My DR tiles | FROZEN §15.9; API `/my-dr` | 13 | `web` role-specific tiles; `api` projection | B1.1 |
| 4.7.8 | Kanban/swimlane, labels, filters, live Saved Views | FROZEN §15.10, §14.3 | 13 | `web` invalid drag/drop rollback; `auth` saved view reapplies ACL | B1.13 |
| 4.7.9 | Hover summaries + Show More deep cards | FROZEN §15.11 | 13 | `web` | B2.4 |
| 4.7.10 | Light/Dark, WCAG 2.1 AA, desktop-first responsive | FROZEN §15.4–7; D-259 | 01 (shell), 12, 13 | `web` theme persistence; axe on every route; manual review | B1.19, B2.2 |
| 4.7.11 | Figma prototype and UI/UX sign-off gates | D-231, D-261 | pre-12 | `manual` | B2.4 |
| 4.7.12 | Event-timezone / local toggle | FROZEN §9.10 | 01 (shell), 12 | `web` toggle test | B1.1 |

## PRD §4.8 — Search and AI

| # | Scope item | Canonical section | BUILD | Test | Gate |
|---|---|---|---|---|---|
| 4.8.1 | Structured filters + FTS + pgvector | ADR-005; API `POST /search` | 15 | `integration` lexical + semantic; inaccessible-chunk negative | B1.15 |
| 4.8.2 | Structured dependencies authoritative; semantic supplemental and cited | FROZEN §11.17; SECURITY §8 | 15, 16 | `ai` citation present; inference → Needs Review not persisted | B1.15 |
| 4.8.3 | Provider abstraction: Synthetic + Anthropic | D-249; API `/admin/ai` | 16 | `ai` adapter contract tests with mocked HTTP | B1.1 |
| 4.8.4 | Synthetic first; model IDs Admin-configurable | D-249 | 16 | `api` admin config; no hard-coded model grep | B1.1 |
| 4.8.5 | Embeddings nomic-embed-text-v1.5 / 768, configurable, re-embed on change | D-233; ADR-029 | 15 | `integration` dimension guard; re-embed job test | B1.1 |
| 4.8.6 | AI Read / Recommend / Act through command services | D-228; ADR-006 | 16 | `ai` Act allowlist + exclusion in every profile; RBAC bypass attempts | B1.15 |
| 4.8.7 | Low-confidence output → Needs Review | FROZEN §14.1; `needs_review_items` | 05, 16 | `transitions` review machine; dismissal needs note | B1.1 |
| 4.8.8 | AI summaries feature flag | FROZEN §11.9; `AI_SUMMARIES_ENABLED` | 16 | `api` flag off → 404/disabled | B1.1 |
| 4.8.9 | Provider-agnostic speech-to-text; review before submit | D-232; API `/speech/transcribe` | 17 | mock provider + review/edit flow; no command from audio | B1.15 |

## PRD §4.9 — Notifications

| # | Scope item | Canonical section | BUILD | Test | Gate |
|---|---|---|---|---|---|
| 4.9.1 | In-app notifications | FROZEN §12.1; `notifications`; D-234 catalog | 08 | `api` + `web` unread | B1.1 |
| 4.9.2 | Email via Admin-configurable SMTP | D-256; API `/admin/notifications/email` | 18 | Mailpit integration test; Gmail-compatible config test | B1.1 |
| 4.9.3 | Email failure never rolls back DR state | FROZEN §12.4 | 18 | `integration` SMTP down → command committed, retry queued | B1.1 |
| 4.9.4 | Teams/Slack Coming Soon only | D-256; policy `notifications.teams.enabled=false` | 18 | `web` disabled state test | B1.1 |
| 4.9.5 | @mentions, personal snooze, shared acknowledgement | FROZEN §12.8–9; `alert_snoozes` | 08 | `api` snooze ≠ ack; breach ack requires reason | B1.1 |

## PRD §4.10 — Evidence, audit and reports

| # | Scope item | Canonical section | BUILD | Test | Gate |
|---|---|---|---|---|---|
| 4.10.1 | Evidence types text/file/screenshot/log/link | `evidence_type`; FROZEN §13.1 | 09 | `api` each type; `evidence_payload_ck` | B1.16 |
| 4.10.2 | File allowlist, 100 MB, malware scanning | D-225, D-245; SECURITY §6 | 09, 22 | `files` allowlisted / blocked / oversize / EICAR; Defender check at 22 | B1.16 |
| 4.10.3 | Append-only audit | SECURITY §11; `audit_events` | 02 (base), every | `integration` UPDATE/DELETE denied for app role; per-command assertion | B1.12 |
| 4.10.4 | Report Draft → edit/publish, versioned narrative | FROZEN §13.7; `report_versions`; API `/reports/*` | 19 | `transitions` DRAFT→PUBLISHED; `auth` publish + reauth | B1.14 |
| 4.10.5 | Report contents (timeline, RTO/RPO, validation, drift, blockers/issues, overrides, failback, monitoring, audit summary) | D-238 facts schema | 19 | deterministic report fixture test | B1.1 |
| 4.10.6 | In-app, PDF, CSV, Excel | D-238; D-244 (Playwright PDF) | 19 | fixture comparison per format | B1.1 |
| 4.10.7 | RACI export PDF/CSV | D-229 | 19 | fixture: Work Stream + Application sections | B1.1 |
| 4.10.8 | Encrypted package exact-restore / clone-as-new; AES-256-GCM; integrity; tenant binding | D-230; SECURITY §7 | 20 | round trip; wrong tenant; tamper; wrong key; reauth | B1.17 |

---

## PRD §5–7 — Non-functional, retention, security (summary rows)

| # | Requirement | Canonical section | BUILD | Test | Gate |
|---|---|---|---|---|---|
| N-1 | 99.9 % objective; Command Center RTO 60 / RPO 15 | FROZEN §3.4–6; OPERATIONS | 22, 23 | `manual` restore drill record | B2.5 |
| N-2 | API avg < 500 ms; dashboard ~2 s; propagation ≤ 10 s; 500/5,000/200 load | FROZEN §16; D-259 | 11, 24 | `load` k6; `e2e:@realtime` | B1.4 |
| N-3 | Latest Chrome/Edge; WCAG 2.1 AA | FROZEN §15.4, §15.6 | 12, 13, 24 | axe + manual | B1.19, B2.2 |
| N-4 | Backup/restore and rollback drills | FROZEN §3.8 | 23 | `manual` | B2.5, B2.6 |
| N-5 | Retention 7 y / 3 y / 3 y, legal hold, injected clock | D-258; FROZEN §13.9–12 | 21 | purge/legal-hold tests | B1.18 |
| N-6 | Entra primary; explicit Local fallback; no pre-created prod Local | D-235, D-239; SECURITY §2 | 02 | `auth` login flows; lockout; TOTP; reset | B1.13, B2.7 |
| N-7 | Participant visibility; scoped writes | D-222; FROZEN §4.8–9 | 02, every | `auth` IDOR suite | B1.13 |
| N-8 | Reauth/MFA for high-risk | D-235; SECURITY §3 | 02, 23 | `auth` reauth window | B1.14 |
| N-9 | Key Vault secrets; no secrets in browser/repo | SECURITY §10; BICEP_PLAN | 22, 23 | gitleaks; Bicep review | B1.8 |
| N-10 | Sessions/CSRF/CORS/rate limits/safe errors | D-239; SECURITY §4 | 02, 23 | `auth` CSRF negative; rate-limit tests; error envelope | B1.13 |
| N-11 | SAST/dependency/SBOM/container/IaC gates | D-246; SECURITY §12 | 23 | `make security` in CI | B1.6–B1.11 |
| N-12 | Idempotency-Key required on all commands, 24 h | D-215; ADR-030 | 01 (middleware), every | `api` missing key / replay per command | B1.1 |
| N-13 | Optimistic concurrency everywhere | API principle 4 | every | `api` 409 per command | B1.1 |
| N-14 | Azure Dev/Test Bicep; outside failure domain; POC public ingress | D-248; BICEP_PLAN | 22 | Bicep lint/what-if; Checkov | B1.11 |

---

## BUILD → requirement index

| BUILD | Requirements covered (ids above) |
|---|---|
| 01 | 4.7.10, 4.7.12 (shell), N-12 middleware; platform for everything |
| 02 | 4.4.5, 4.10.3, N-6, N-7, N-8, N-10 |
| 03 | 4.5.1, 4.7.4 |
| 04 | 4.1.1–4.1.4, 4.1.7–4.1.9, 4.5.2 |
| 05 | 4.1.5, 4.1.6, 4.8.7 |
| 06 | O-3, O-5, O-6, 4.2.1–4.2.3, 4.3.1, 4.3.2 |
| 07 | O-7, 4.2.4, 4.2.5, 4.4.1–4.4.4, 4.4.6, 4.4.7 |
| 08 | 4.3.3, 4.3.4, 4.3.6, 4.9.1, 4.9.5 |
| 09 | 4.3.5, 4.5.3, 4.5.6, 4.5.7, 4.10.1, 4.10.2 |
| 10 | O-4, O-8, 4.1.3, 4.2.6, 4.5.2–4.5.5, 4.7.2–4.7.6 |
| 11 | O-8, N-2 |
| 12 | O-2, 4.7.1, 4.7.2, 4.7.6, 4.7.10, 4.7.12 |
| 13 | O-2, 4.3.2, 4.7.7–4.7.9 |
| 14 | 4.1.3, 4.1.9, 4.6.1–4.6.4 |
| 15 | 4.8.1, 4.8.2, 4.8.5 |
| 16 | O-10, 4.8.2–4.8.4, 4.8.6–4.8.8 |
| 17 | 4.8.9 |
| 18 | 4.9.2–4.9.4 |
| 19 | O-9, 4.6.4, 4.10.4–4.10.7 |
| 20 | 4.10.8 |
| 21 | O-9, N-5 |
| 22 | 4.10.2 (Defender), N-1, N-9, N-14 |
| 23 | N-1, N-4, N-8–N-11 |
| 24 | O-1, N-2, N-3; all gates in VERIFY.md Part B |

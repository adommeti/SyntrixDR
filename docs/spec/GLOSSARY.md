# DR Command Center — Glossary (ubiquitous language)

**Status:** Canonical companion (regenerated 2026-09-12 from master §21 and `FREEZE_ADDENDUM.md`, D-220).
**Rule:** Code uses the code name in column 3 exactly. A new term needs a row here in the same PR. Definitions defer to the canonical doc in column 4; if this table and that doc disagree, the doc wins and this table is fixed.

Column 3 notation: `table` · `table.column` · `enum_type = VALUE` · `TsType` (from `packages/contracts`) · `POST /path` · `policy.key`.

## Events, plans and scope

| Term | Definition | Code name | Canonical reference |
|---|---|---|---|
| DR | Disaster Recovery. | — | master §21 |
| DR Event | One planned DR test (`PLANNED_DR`) or real incident (`REAL_INCIDENT`) being managed; the live operational container and centre of gravity. States `PLANNED → ACTIVE → FAILOVER_IN_PROGRESS → FAILED_OVER → FAILBACK_IN_PROGRESS → CLOSED`, `CANCELLED` terminal from any non-terminal state. | `dr_events`; `dr_event_type`; `dr_event_status`; `DrEvent` | STATE_MACHINES "DR Event"; FROZEN §6 |
| Parent / child Event | A parent Event is a reporting/coordination container for child (sub-DR) Events; children own their lifecycle, `network_cut_at`, RTO/RPO and report; parent cannot CLOSE while a non-cancelled child is non-terminal. | `dr_events.parent_dr_event_id` | D-219 |
| Subset-Application Event | An Event scoped to selected Applications; needs no parent. An App/System Owner may create one as PLANNED for their own Applications but cannot activate it. | `dr_events` + `dr_applications` rows | D-213, FROZEN §6.7 |
| Application | Long-lived master record: name, default Tier, owners, external references, documents. | `applications`; `Application` | DATA_MODEL "Application" |
| DR Application | The Application's Event-specific recovery instance: effective Tier, RTO/RPO targets and actuals, failback requirement, validation state, health. States `NOT_STARTED → RECOVERING → TECHNICAL_VALIDATION → FAILED_OVER → FAILBACK_IN_PROGRESS → COMPLETED`. | `dr_applications`; `dr_application_status` (migration 0002); `DrApplication` | D-208; STATE_MACHINES "Application recovery" |
| Plan | Reusable, versioned template of Tasks/dependencies/milestones (`FAILOVER`, `FAILBACK`, `GENERAL`). Instantiating into an Event copies it; live edits never mutate the source. | `plans`; `plan_type`; `plan_source_type` | FROZEN §6.4; API `/plans` |
| Plan Version | Immutable numbered snapshot of a Plan or of an Event's plan (`DRAFT`, `BASELINE`, `EXECUTION`, `FINAL`). | `plan_versions`; `plan_version_type`; `plan_version_tasks` | DATA_MODEL "Plan" |
| Baseline | The `BASELINE` Plan Version captured at `start-failover`; immutable comparison data for drift reporting. | `dr_events.baseline_plan_version_id` | FROZEN §6.5; ADR-010 |
| Drift | Difference between Baseline and live execution (added/removed/changed Tasks, dependencies, milestones, durations). | report facts `drift` section | D-238 |
| Failover | Recovery movement from the (simulated) failed source into the DR target. Task phase `FAILOVER`. | `task_phase = FAILOVER` | master §21 |
| Failback | Separate planned process for returning after failover; own Plan/Tasks/dependencies; required by default. Never inferred by reversing failover. | `dr_applications.failback_required`; `task_phase = FAILBACK`; `POST /dr-events/{id}/start-failback` | FROZEN §8 |
| Runbook | Written DR procedure (usually Excel) imported into a Plan/Event via the mapping/review step. | `import_jobs`; `POST /dr-events/{id}/imports/excel` | D-221 |
| Import Job | An Excel import with arbitrary headers mapped (AI or manual) to Task fields; preserves row provenance. | `import_jobs`; `tasks.source_import_id`, `tasks.source_import_row` | D-221 |
| Readiness rule | A configurable check (`HARD_STOP`, `WARNING`, `OFF`) evaluated before `activate`; HARD_STOP blocks unless a permitted, reasoned Coordinator override exists. Acyclic-dependency rule is not configurable. | `readiness.<rule>` policy keys; `POST /dr-events/{id}/activate` | D-224 |
| Event timezone | Explicit IANA timezone of an Event, used for display toggle; storage is always UTC. Required (readiness HARD_STOP). | `dr_events.event_timezone` | FROZEN §9.10; D-224 |
| network_cut_at | Common timestamp at which the source network was cut; the RTO clock start for every DR Application in the Event. Supplied optionally on `start-failover` with `activated_at ≤ network_cut_at ≤ now()`, else server `now()`. | `dr_events.network_cut_at`; `dr_applications.sla_start_at` | D-237; D-257 |

## Execution model

| Term | Definition | Code name | Canonical reference |
|---|---|---|---|
| Work Stream | Explicit Event-level operational track: `NETWORK, STORAGE, DATABASE, APPLICATIONS, MONITORING, VALIDATION, CUSTOM`. Has a Lead. | `work_streams`; `work_streams.stream_type` (0002); `work_streams.lead_user_id` | D-227; DATA_MODEL |
| Work Stream Lead | User accountable for a Work Stream; validates shared Work Stream work; may override within the stream where policy allows. | `work_streams.lead_user_id`; role `WORK_STREAM_LEAD` scope `WORK_STREAM` | RBAC_MATRIX |
| Task | Executable unit of DR work. May reference a DR Application and/or a Work Stream (at least one). Fixed Owning Team, optional Current Assignee, phase, expected duration, evidence requirements, optimistic version. States `NOT_STARTED, IN_PROGRESS, BLOCKED, READY_FOR_VALIDATION, COMPLETED, CANCELLED`. | `tasks`; `task_status`; `task_phase`; `Task` | STATE_MACHINES "Task"; D-252 |
| Subtask | Child Task beneath a broader Task. | `tasks.parent_task_id` | master §21 |
| Ready (derived) | Not a state: a `NOT_STARTED` Task whose HARD dependencies and manual gates are satisfied. Drives the My DR "Ready" tile. | projection field `is_ready` | FROZEN §7.3 |
| Dependency (HARD / ADVISORY) | Finish-to-Start edge between Tasks. `HARD` blocks Start unless an audited override exists; `ADVISORY` warns only. No self-edges, no directed cycles. | `task_dependencies`; `dependency_strength = HARD \| ADVISORY`; `dependency_type = FINISH_TO_START` | FROZEN §7.8–9 |
| Milestone | First-class shared gate (e.g. "Network Ready") aggregating Tasks; critical milestones require manual confirmation. States `NOT_STARTED → IN_PROGRESS/AT_RISK → READY_FOR_CONFIRMATION → ACHIEVED \| MISSED`. | `milestones`; `milestone_status`; `milestone_confirmation_mode`; `POST /milestones/{id}/confirm` | STATE_MACHINES "Milestone"; ADR-009 |
| Ready for Confirmation | Milestone condition where contributing work is complete but human confirmation is pending. | `milestone_status = READY_FOR_CONFIRMATION` | master §21 |
| Phase | Task grouping within an Event: `PRE_DR, FAILOVER, VALIDATION, FAILBACK, POST_DR`. | `task_phase` | schema_v1 |
| Blocker | Structured record of the condition preventing a Task from progressing: reason, responsible Team/User, timestamps, escalation, resolution and verification. States `OPEN → ASSIGNED → IN_PROGRESS → RESOLVED → VERIFIED → CLOSED` via `assign`, `start`, `resolve`, `verify` (verify closes atomically). | `blockers`; `blocker_status`; `POST /blockers/{id}/{assign\|start\|resolve\|verify}` | D-211; STATE_MACHINES "Blocker" |
| Blocker Owner | Team/person responsible for clearing the blocking condition (distinct from the Task's Owning Team). | `blockers.blocker_team_id`, `blockers.blocker_owner_user_id` | master §21 |
| Escalation | Timer-driven notification when a Blocker is unaddressed: T0 immediate, T1–T4 configurable minutes. | `blocker.escalation_minutes.{T0..T4}` policy | D-225 |
| Issue / Finding | Non-blocking observation, risk, deviation or lesson (`ISSUE, FINDING, RISK, DEVIATION, LESSON`). Never stops execution unless explicitly linked/converted to a Blocker. States `OPEN → IN_REVIEW → RESOLVED \| DISMISSED`. | `issue_findings`; `issue_finding_type`; `issue_finding_status`; `POST /issues/{id}/{review\|resolve\|dismiss}` | D-253; STATE_MACHINES "Issue/Finding" |
| Validation | Persisted record created by `submit-validation` (`PENDING`) and closed by `validate` (`APPROVED`/`REJECTED`). No separate REST resource in V1; `IN_REVIEW`/`REMEDIATION` reserved. | `validations`; `validation_status`; `validation_target_type`; `POST /tasks/{id}/submit-validation`, `POST /tasks/{id}/validate`, `POST /dr-applications/{id}/validate` | D-210 |
| Technical Validation | Application-level technical confirmation by any System/Application Owner slot; stops the RTO clock (`TECHNICAL_VALIDATION → FAILED_OVER` or `→ COMPLETED`). | `dr_applications.technical_validated_at`; `POST /dr-applications/{id}/validate` | D-208, D-254 |
| Business Confirmation | Optional Business Owner checkbox/note; not a gate in V1. | `dr_applications.business_confirmed`, `business_confirmation_note` | FROZEN §18.11 |
| Evidence (Evidence Item) | Proof attached to a target: `TEXT, FILE, SCREENSHOT, LOG, EXTERNAL_LINK`; carries checksum and malware scan status. A Task normally needs ≥ `evidence_min_count` acceptable items plus a verification note before validation. | `evidence_items`; `evidence_type`; `tasks.evidence_required`, `evidence_min_count`, `verification_note_required` (0002) | D-226; SECURITY_REVIEW §6 |
| Verification note | Executor's text attestation submitted with `submit-validation`. | `validations.note` / `evidence_type = TEXT` | D-226 |
| needs_specific_validation | `false` = standard Owner/Lead validation; `true` = Task additionally carries task-specific criteria/evidence requirements. Never a self-completion shortcut. | `tasks.needs_specific_validation` | D-209 |
| Monitoring closure warning | At `close`, any non-cancelled, non-COMPLETED Task in a `MONITORING` Work Stream blocks closure unless an audited closure exception is recorded. | `overrides.override_type = CLOSURE_EXCEPTION` | D-227 |

## People, teams and authority

| Term | Definition | Code name | Canonical reference |
|---|---|---|---|
| User | Entra or Local identity with manager/contact data; deactivated, never deleted. | `users`; `identity_type = ENTRA \| LOCAL` | DATA_MODEL |
| Team | Accountability/resource grouping; one queue plus tags/labels. | `teams`; `team_memberships` | FROZEN §7.11 |
| Manager | User managing a Team; reassigns only within own Team; org-tree roll-up. | `team_memberships.membership_role = MANAGER`; `users.manager_user_id` | RBAC_MATRIX |
| Owning Team | Team accountable for a Task; fixed for the Task's life regardless of who executes. | `tasks.owning_team_id` | FROZEN §4.13; ADR-007 |
| Current Assignee | User currently executing a Task; may belong to another Team. | `tasks.current_assignee_user_id`; `POST /tasks/{id}/assign` | FROZEN §4.13 |
| Volunteer | A user claiming eligible unassigned cross-Team work; Owning Team unchanged. | `POST /tasks/{id}/volunteer` | FROZEN §4.12 |
| Participant | User enrolled in an Event (explicit table + auto-enrolment by role, assignment, ownership, Work Stream lead, Owning Team membership). Default read boundary and WebSocket scope. Global Admins are implicit participants. | `dr_event_participants` (0002) | D-222 |
| Role (scoped) | `GLOBAL_ADMIN, DR_COORDINATOR, WORK_STREAM_LEAD, APP_OWNER, BUSINESS_OWNER, MANAGER, EXECUTOR, GLOBAL_READONLY` assigned at `GLOBAL`, `DR_EVENT`, `WORK_STREAM` or `APPLICATION` scope. "Auditor" and "Executive" are display labels for `GLOBAL_READONLY` or per-Event enrolment, never role keys (D-222). | `role_assignments.role_key`; `role_scope_type` | RBAC_MATRIX; D-222 |
| Global Admin | Global configuration plus full Event lifecycle authority. | `role_key = GLOBAL_ADMIN` | FROZEN §4.6 |
| DR Coordinator | Event-level command, cross-Team allocation, overrides, failover/failback, closure, reporting. | `role_key = DR_COORDINATOR`; `dr_events.coordinator_user_id` | FROZEN §4.6 |
| Application / System Owner | Technical owner and validator; up to three slots (Primary required, Secondary, Tertiary). Primary is Accountable; any slot may validate. | `application_owners.owner_type = SYSTEM_APPLICATION`, `slot 1..3` | D-254; FROZEN §5 |
| Business Owner | Business stakeholder; up to three slots; read/comment/business confirmation only. | `application_owners.owner_type = BUSINESS` | FROZEN §5 |
| Executor / Engineer | Executes work, records evidence/comments/blockers; can never self-complete. | `role_key = EXECUTOR` | D-209 |
| Auditor / Executive | Display labels, not role keys: a separately granted `GLOBAL_READONLY` role or per-Event participant enrolment; never inferred from job title. | `role_key = GLOBAL_READONLY` or `dr_event_participants` row | D-222 |
| Skill | V1 capability tag on a user; proficiency deferred to V2. | `skills`; `user_skills` (`proficiency` nullable, unused) | D-218 |
| Manager precedence | Domain override: a Manager's stale-version assignment on an own-Team Task is accepted when the intervening change was a Coordinator/Admin assignment; both audited; recorded as `MANAGER_PRECEDENCE`; users notified. Everything else stale → `409 CONCURRENCY_CONFLICT`. | `overrides.override_type = MANAGER_PRECEDENCE`; error `CONCURRENCY_CONFLICT` | D-214; ADR-030 |
| Local account | Explicitly created fallback identity with Argon2id password, lockout, TOTP (mandatory for Local GLOBAL_ADMIN), emailed reset token. None pre-created in production. | `local_credentials` (0002); `POST /admin/local-users` | D-235 |
| Reauth | Fresh authentication (password/TOTP or Entra prompt) within a 5-minute window required for high-risk commands. | `reauth_grants` (0002); header `X-Reauth-Grant` | D-235; SECURITY_REVIEW §3 |
| High-risk action | Start DR, Start Failback, Close/Cancel, force/override, report publish, package export/import, privileged role/policy change, Local admin creation — all require reauth and audit. | API_CONTRACT "High risk" column | SECURITY_REVIEW §3 |

## Timing, health and risk

| Term | Definition | Code name | Canonical reference |
|---|---|---|---|
| Tier | Permanent/default application criticality T0–T4 with default RTO/SLA 30/60/120/240/1440 min and health weight 100/75/50/30/20. | `tiers.code = TIER_0..TIER_4`, `default_sla_minutes`, `default_health_weight`; `dr_applications.effective_tier_id` | FROZEN §9.2, §10.1 |
| RTO | Recovery Time Objective: target minutes from `network_cut_at` to `technical_validated_at`; actual computed on validation; pass/fail recorded. | `dr_applications.rto_target_minutes`, `rto_actual_minutes`, `rto_passed` | D-257; DATA_MODEL "RTO/RPO" |
| RPO | Recovery Point Objective: target data-loss window; stores pre-DR reference time, recovered-data time, actual loss and pass/fail. RPO-N/A = `dr_applications.rpo_not_applicable=true` (target must be NULL); CHECK enforces the mutual exclusion. | `dr_applications.rpo_target_minutes`, `rpo_not_applicable` (0002), `rpo_reference_at`, `rpo_recovered_data_at`, `rpo_actual_loss_minutes`, `rpo_passed` | D-257; D-224 |
| SLA | Recovery-time expectation for an Application in the regional model (effective from Tier, overridable per Event). | `dr_applications.effective_sla_minutes` | FROZEN §9 |
| SLA warning | Alert when elapsed time reaches `sla.warning_percent` (default 85) of target and/or `sla.warning_minutes_remaining`. | policy `sla.warning_percent`, `sla.warning_minutes_remaining`; alert type `SLA_WARNING` | D-225 |
| Breach | Target passed before successful technical validation; immutable historical fact even after late recovery; creates an alert requiring acknowledgement with reason. | `dr_applications.sla_breached_at`; alert type `RTO_BREACH` / `RPO_BREACH` | FROZEN §9.5, §9.8 |
| Target / Forecast | Target = committed recovery time; Forecast = deterministic projection from remaining HARD-dependency work and expected durations (no ML). | `dr_applications.target_recovery_at`, `forecast_recovery_at`, `failback_target_at`, `failback_forecast_at` | FROZEN §7.10 |
| Task Score | 100 × COMPLETED eligible Tasks / eligible Tasks of the Application (eligible = non-CANCELLED; no Blocked penalty). | health engine `task_score` | D-223 |
| Validation Score | 100 if the Application's required technical validation for the current recovery phase passed (failback validation while failback active), else 0. | health engine `validation_score` | D-223 |
| Application Health | 0.70 × Task Score + 0.30 × Validation Score. | `dr_applications.health_score` | D-223 |
| Event Health (dial) | Σ(App Health × effective tier weight) / Σ(weight); no Tier-0 cap; bands GREEN ≥ 85, YELLOW 60–84.99, RED < 60; "—" when zero Applications. | `dr_events.health_score`; `dr_health_snapshots`; `GET /dr-events/{id}/health/explain` | D-223 |
| Treemap band | Four-band Application colour: GREEN ≥ 85, LIGHT_ORANGE 60–84.99, DARK_ORANGE 40–59.99, RED < 40; overlays only worsen: RTO/RPO breach → RED; SLA warning → ≥ DARK_ORANGE; active Blocker on T0/T1 → ≥ DARK_ORANGE, on T2–T4 → ≥ LIGHT_ORANGE. Never changes the numeric dial. | `health_band`; `dr_applications.health_band` | D-223 |
| Treemap | Squarified D3 treemap of DR Applications sized by effective tier weight and coloured by band. | web `ApplicationTreemap` | D-243; ADR-022 |
| Health Dial | Weighted 0–100 Event health gauge with band colour and explanation. | web `HealthDial` | FROZEN §10 |

## Collaboration, review and views

| Term | Definition | Code name | Canonical reference |
|---|---|---|---|
| Comment / @mention | Cross-Team collaboration on any visible target; mentions notify. | `comments`; `mentions`; `POST /{target_type}/{target_id}/comments` | FROZEN §12.8 |
| Alert | Shared operational condition (breach, warning, escalation) requiring acknowledgement (with reason where a breach). Severities `CRITICAL, HIGH, MEDIUM, LOW`. | `alerts`; `alert_type`, `severity` catalogs in `packages/contracts`; `POST /alerts/{id}/acknowledge` | D-234; FROZEN §9.8 |
| Notification | Per-user in-app (and email) delivery of an alert or event; read state per user. | `notifications`; `notification_type` catalog | D-234; FROZEN §12 |
| Snooze vs Acknowledge | Snooze is personal and temporary (`alert_snoozes`); Acknowledge is shared, audited and may require a reason. | `alert_snoozes`; `alerts.acknowledged_at`; `POST /alerts/{id}/snooze` | FROZEN §12.9 |
| Needs Review | Queue item for low-confidence AI/import/semantic output: `OPEN → IN_REVIEW → RESOLVED \| DISMISSED`; dismissal needs a note. | `needs_review_items`; `review_source`; `review_status`; route `/dr-events/[eventId]/reviews` | FROZEN §14; D-212 |
| Override | Auditable, reasoned departure from a guard or policy (force start, dependency override, closure exception, manager precedence). | `overrides`; `overrides.override_type`, `reason` | DATA_MODEL "Override" |
| Closure exception | Admin/Coordinator override recorded to `close` an Event despite an outstanding monitoring warning. | `overrides.override_type = CLOSURE_EXCEPTION` | D-227 |
| Policy | Global default with scoped overrides (Event/Work Stream/Application); catalog in D-225. | `policy_definitions.key`; `policy_values` | D-225 |
| Saved View | Private or shared live-linked filter/layout definition; always reapplies current access controls. | `saved_views`; `saved_view_visibility` | FROZEN §14.3 |
| Label | Free tag on Tasks within an Event for filtering/swimlanes. | `labels`; `task_labels` | FROZEN §15.10 |
| Deep card | Hover summary → "Show More" full card, consistent across people, Applications, Tasks, Blockers, Issues, Events. | web `DeepCard` | FROZEN §15.11 |
| My DR | Personal dashboard with tiles Ready (derived), In Progress, Blocked, SLA/RTO Risk, Needs My Confirmation, Completed. | `GET /my-dr`; route `/my-dr` | FROZEN §15.9 |
| Timeline event | Meaningful operational moment shown on the Event timeline; typed catalog. | `timeline_event_type` catalog in `packages/contracts` | D-234 |
| Audit Event | Append-only actor/entity/action/before/after record; 7-year retention. | `audit_events` | SECURITY_REVIEW §11 |

## Reporting, packages and retention

| Term | Definition | Code name | Canonical reference |
|---|---|---|---|
| Report | Per-Event report starting as `DRAFT`; Admin/Coordinator edit narrative and `publish` (high-risk). | `reports`; `report_status`; `POST /reports/{id}/publish` | FROZEN §13.7 |
| Report Version | Numbered narrative + immutable `facts_snapshot` (versioned schema); AI may draft narrative, facts only from snapshot. | `report_versions.narrative`, `facts_snapshot`, `generated_by_ai` | D-238 |
| Facts schema | Versioned JSON of Event, Applications, Tasks, Dependencies, Milestones, Blockers, Issues, Evidence, Validations, Overrides, RTO_RPO, RACI, Audit_Summary — the same model feeds PDF/CSV/Excel. | `facts_snapshot.schema_version` | D-238 |
| RACI export | Two sections: Work Stream RACI and Application RACI, as CSV and PDF from the same model. | `POST /dr-events/{id}/raci/export` | D-229 |
| Package | AES-256-GCM encrypted export of an Event for exact restore or clone-as-new, with manifest and tenant binding; random DEK wrapped by Key Vault key (prod) or `DRCC_PACKAGE_DEV_KEY` (local). | `export_packages`; `POST /dr-events/{id}/packages/export`, `POST /packages/import` | D-230 |
| Manifest | `{package_version, tenant_id, dr_event_id, exported_at, exported_by, schema_version, content_hash, entity_counts}`. | `export_packages.encryption_metadata`, `integrity_hash` | D-230 |
| Tenant ID | Entra tenant GUID in Azure; `DRCC_TENANT_ID` locally; cross-tenant import rejected by default. | `export_packages.tenant_id`; env `DRCC_TENANT_ID` | D-230 |
| Legal hold | Flag that suspends retention purge for an Event/Evidence until lifted; audited. | `legal_holds` (BUILD-21 revision) | FROZEN §13.9 |
| Retention | Audit 7 y; Events/Tasks 3 y; Evidence default 3 y by classification; reports/imports/indexes follow source. Tests use injected clocks. | `retention.*` policy keys | D-258 |
| Document / Chunk | Uploaded source file and its FTS+vector chunks for hybrid search. | `documents`; `document_chunks.embedding vector(768)`, `search_vector` | FROZEN §11.16 |
| Hybrid search | Structured filters + PostgreSQL FTS + pgvector semantic retrieval, permission-filtered before retrieval. | `POST /search` | ADR-005 |

## AI

| Term | Definition | Code name | Canonical reference |
|---|---|---|---|
| AI Read | Grounded, non-mutating question answering with citations. | `POST /ai/read` | FROZEN §11.10 |
| AI Recommend | Non-mutating recommendation. | `POST /ai/recommend` | FROZEN §11.10 |
| AI Act | AI-prepared typed command executed through the normal command service with identical RBAC/guards/reauth/idempotency/audit. Allowlisted commands only; Event lifecycle, validation, publish, package, admin/security never. | `POST /ai/act/prepare`, `POST /ai/act/confirm` | D-228 |
| AI control profile | `CONSERVATIVE` (confirm every Act with diff), `BALANCED` (confirm cross-Team and T0/T1; batch-confirm own-scope low-risk), `FAST_EXECUTION` (own-scope low-risk executes without per-action reconfirmation). Excluded commands stay excluded. | `ai_control_profile`; `dr_events.ai_control_profile`; policy `ai.control_profile` | D-228 |
| AI provider | Admin/environment-selected backend adapter: Synthetic (first) or direct Anthropic; Foundry V2. Keys backend-only. | `AI_PROVIDER`; `GET/PUT /admin/ai` | D-249 |
| Embedding model | `nomic-embed-text-v1.5`, 768 d, sentence-transformers in the worker; change triggers re-embedding. | `EMBEDDING_MODEL`, `EMBEDDING_DIM` | D-233 |
| Speech-to-text | faster-whisper provider returning editable text; never executes commands. | `POST /speech/transcribe`; `SPEECH_PROVIDER` | D-232 |
| Untrusted content | Retrieved runbooks, comments, PDFs, evidence and search results: data, never instruction authority. | prompt assembly boundary | SECURITY_REVIEW §8 |
| AI summaries | Feature-flagged narrative summaries. | `AI_SUMMARIES_ENABLED` | FROZEN §11.9 |

## Platform and process

| Term | Definition | Code name | Canonical reference |
|---|---|---|---|
| Idempotency-Key | UUID header required on every command POST; 24 h retention; replay returns the stored response. | header `Idempotency-Key`; `idempotency_keys` (0002); error `IDEMPOTENCY_KEY_REQUIRED` | D-215; ADR-030 |
| expected_version | Optimistic concurrency token sent with commands; mismatch → `409 CONCURRENCY_CONFLICT` (except Manager precedence). | `*.version`; request `expected_version` | API_CONTRACT principle 4 |
| Outbox event | Row written in the same transaction as a mutation and later published to Redis for WebSocket fan-out. | `outbox_events` (0002) | D-242 |
| target_type | Closed polymorphic target enum: `DR_EVENT, DR_APPLICATION, APPLICATION, WORK_STREAM, TASK, TASK_DEPENDENCY, MILESTONE, BLOCKER, ISSUE_FINDING, VALIDATION, IMPORT_JOB, PLAN, PLAN_VERSION, REPORT, DOCUMENT, ALERT`. | `target_type` enum (0002); `TargetType` | D-216 |
| Correlation ID | Request/job/provider-call identifier carried through logs and the error envelope. | `error.correlation_id`; header `X-Correlation-ID` | SECURITY_REVIEW §4 |
| Transition service | The only code path that changes a lifecycle status. | `apps/api/app/<module>/transition_service.py` | ARCHITECTURE "Layering" |
| Object store | Azurite locally / Azure Blob in Azure via one adapter. | `ObjectStore` port; `AZURE_STORAGE_CONNECTION_STRING` | D-217 |
| Malware scan status | `PENDING`, `CLEAN`, `INFECTED`; only `CLEAN` evidence counts toward completion. | `evidence_items.malware_scan_status`; `MALWARE_SCANNER = mock \| defender` | D-245 |
| Degraded state | UI mode when API/WebSocket is unreachable: read-only banner, no simulated writes. | web `DegradedBanner` | FROZEN §3.9 |
| Golden scenario | Fictitious East US 2 → Central US regional DR fixture used by seed and E2E (17 steps). | `make seed`; Playwright `@golden` | D-236; TEST_STRATEGY |
| Load seed | Fictitious 500 Applications / 5,000 Tasks / 200-user dataset. | `make seed-load` | D-259 |
| BUILD-NN | One of 24 ordered increments; the PR/milestone unit; lettered sessions (06a/06b) merge as one PR. | branch `feat/build-NN-<slug>`, tag `v0.NN.0` | D-202, D-203 |
| D-record | A `[DECIDED]` product decision in `FREEZE_ADDENDUM.md` (D-201…D-261); the only way product behaviour changes. | `FREEZE_ADDENDUM.md` | OPEN_QUESTIONS.md |
| 10-second rule | Core UX acceptance: a Coordinator identifies what is failing and what needs action within 10 s of opening the Command Center. | VERIFY B2.3 | D-261 |
| V1 / V2 / V3 | V1 = this POC-to-production release; V2/V3 = reserved capabilities (ITSM sync, Graph sync, Teams/Slack, Foundry, …). | `V2_V3_ROADMAP.md` | FROZEN §18 |

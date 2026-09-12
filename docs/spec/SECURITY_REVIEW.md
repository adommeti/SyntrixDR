**Reconciled 2026-09-12 against FREEZE_ADDENDUM.md (D-201…D-261).**

# DR Command Center — Frozen Security Review

**Reviewer role:** application security + cloud security + AI security + operational resilience.

## 1. Security posture

[DECIDED] The Command Center is an internal high-impact operational system. Compromise can misroute resources, falsify recovery status, expose evidence, or trigger dangerous DR actions. Security controls therefore apply to both conventional application actions and AI-mediated actions.

## 2. Identity

### Primary

- Entra ID OIDC.
- Internal User maps Entra subject/object identifier to application roles/scopes.
- Authentication success never implies authorization.

### Auth topology (D-239)

- FastAPI owns auth: Entra OIDC via FastAPI. (D-239)
- Session cookie: HttpOnly, Secure (prod), SameSite=Lax; Redis-backed session. (D-239)
- CSRF protection on state-changing requests. (D-239)
- Same-origin `/api` routing from Next.js to FastAPI. (D-239)
- WebSocket auth via the same session. (D-239)
- Local auth enters the same session/RBAC system. (D-239)
- No access tokens in browser JavaScript. (D-239)

### Local fallback

- Supported for testing/fallback.
- No production Local user is pre-created. (D-235)
- Global Admin explicitly creates one when operationally required.
- NIST-style length-based password policy; avoid arbitrary complexity games.
- Parameters: Argon2id; password 12–128 chars, no composition rules, breached-password denylist. (D-235)
- Rate limiting, failed-login tracking, lockout, reset workflow and audit: 10 failures/15 min → 30-min lockout; reset via emailed single-use token, 30-min expiry. (D-235)
- Sessions: 8 h absolute, 30 min idle; 5-min privileged reauth window. (D-235)
- Privileged Local account MFA: TOTP required for Local GLOBAL_ADMIN, configurable for others. (D-235)
- Every Local login clearly identified in audit.

### Directory outage

- Existing valid sessions may continue until normal expiry where feasible.
- Explicit Local accounts are fallback.
- No hidden automatic bypass of Entra policy.

## 3. Authorization

### Visibility

- Default: DR Event participants only.
- Participant scoping is an explicit `dr_event_participants` table, not an inference at query time. Auto-enrolment when a User: holds an Event-scoped role; is current Task assignee; is Blocker owner; is any-slot System/Application or Business Owner of an in-scope Application; leads a Work Stream; is member or manager of an Owning Team with Tasks in the Event. Global Admins are implicit participants of every Event. Auditor/Executive receive a separately granted global read-only role or per-Event enrolment — never by job title. (D-222)
- Inside an Event: broad cross-Team read so participants can understand dependencies and blockers.
- Sensitive admin/security configuration remains role restricted.

### Write scopes

- Global Admin: global configuration + full Event lifecycle.
- DR Coordinator: Event-wide command and cross-Team authority.
- Work Stream Lead: scoped work-stream mutation/validation/override where policy allows.
- Application Owner: application technical plan/work/validation.
- Business Owner: read/comment/business confirmation; no mandatory technical gate.
- Manager: own-Team resource operations/review.
- Executor: own/Team-authorized work; cross-Team comments; eligible volunteering.

### High-risk reauthentication

Require policy-driven reauth/MFA for at least:

- Start DR / network cut
- Start Failback
- Close DR
- Cancel DR
- force start / dependency override / closure exception
- official report publish
- full package export/import
- privileged role assignment
- security/global policy change
- Local admin/user creation

V1 has no dual-control requirement; audit is mandatory.

## 4. Web/session security

- Secure, HttpOnly, SameSite cookies where cookies are used.
- CSRF protection for cookie-authenticated mutation requests.
- Explicit CORS allowlist; no wildcard production CORS.
- Security headers: CSP appropriate to Next.js deployment, HSTS, frame protection/frame-ancestors, nosniff, referrer policy.
- Rate limit login, password reset, search, AI, export/import and mutation bursts.
- Stable safe error envelope; never return stack traces/secrets to clients.
- Correlation IDs across web/API/jobs/provider calls.

## 5. API security

1. All mutations authorize server side.
2. Object-level authorization is required; UUID knowledge is not authorization.
3. Transition service rechecks authorization and guards even if route already checked.
4. Optimistic concurrency prevents silent lost updates. The single sanctioned exception is Manager precedence on own-Team assignments (`MANAGER_PRECEDENCE`, both actions audited, affected users notified); every other stale write returns `409 CONCURRENCY_CONFLICT`. (D-214)
5. `Idempotency-Key` header is **required on every command POST** (400 `IDEMPOTENCY_KEY_REQUIRED` if missing); retained 24 h; GET/queries exempt; a pure pre-signed-upload handshake may be exempt but the domain command attaching the artifact requires it. (D-215)
6. Input schemas deny unknown dangerous fields where practical.
7. File/external URLs are validated; server-side fetch must guard SSRF if later introduced.

## 6. File/evidence security

- Admin-configurable MIME/extension allowlist; default `[pdf,png,jpg,jpeg,txt,log,csv,xlsx,docx,zip,json]`. (D-225)
- Default max upload 100 MB.
- Do not trust client MIME/filename.
- Production malware scan/quarantine before evidence is treated as clean/available: Azure Blob + Microsoft Defender for Storage; scan result updates `malware_scan_status`; INFECTED/unscanned evidence can't satisfy completion. (D-245)
- Local/test scanner adapter: EICAR payload → INFECTED, else CLEAN. (D-245)
- Blob objects private by default; issue time-limited authorized access when required.
- Evidence metadata records uploader, checksum, scan status/time and target.
- External evidence links are references, not implicitly trusted content.

## 7. Export/import package security

- AES-256-GCM authenticated encryption with a random 256-bit DEK; DEK wrapped by an Azure Key Vault key (prod) or env-provided dev key (local); no plaintext key in package. (D-230)
- Integrity/manifest verification before import mutation. Manifest = `{package_version, tenant_id, dr_event_id, exported_at, exported_by, schema_version, content_hash (SHA-256), entity_counts}`. (D-230)
- Manifest contains tenant/company ID; mismatch rejected by default. `tenant_id` = Entra tenant GUID in Azure, `DRCC_TENANT_ID` config locally; Dev→Test import allowed when tenant IDs match. (D-230)
- Production key material in Key Vault.
- Exact restore and clone-as-new both require high-risk authorization/reauth.
- Import validates schema/version and performs staged validation before commit.
- Exported evidence access follows current export authorization.
- No V1 dual-control requirement.

## 8. AI security

### Trust boundary

- All provider calls from backend; no provider secret in browser.
- Provider/model configured by Admin/environment.
- Synthetic + Anthropic V1; Foundry V2.
- AI usage can be disabled without disabling DR execution.

### Prompt-injection / retrieved-content controls

1. Retrieved runbooks, comments, PDFs, evidence and search content are **untrusted data**.
2. Retrieved text cannot redefine system policy, RBAC or tool permissions.
3. Permission-filter before retrieval/context building, not after generation.
4. Structured relation is authoritative over semantic inference.
5. AI-generated cross-Application dependency inference is labeled and cited rather than silently persisted.
6. Act requests convert into typed command intents, display confirmation where required, then invoke normal application command service.
7. AI never writes directly to database/repository.
8. Tool/action allowlist, typed parameters and transition guards are mandatory.
9. Act allowlist: task start/block/resume/submit-validation/assign/volunteer; blocker assign/start/resolve; milestone confirm; issue create/review/resolve; comment create; alert acknowledge/snooze. (D-228)
10. **Never via AI**: any Event lifecycle command, Task/App validation, report publish, package export/import, role/policy/admin/security configuration, local-user creation. Excluded commands stay excluded in every profile. (D-228)
11. Control profiles (`ai.control_profile`, default CONSERVATIVE): CONSERVATIVE = confirm every Act with diff preview; BALANCED = explicit confirmation for cross-Team changes and Tier 0/1 work, one batch confirmation for own-scope low-risk; FAST_EXECUTION = own-scope low-risk allowlisted actions execute without per-action reconfirmation. RBAC/idempotency/guards/audit always apply. (D-228, D-225)

### Data governance

- Environment opt-in for AI provider use.
- Do not intentionally send secrets/passwords/tokens to models.
- Record provider/model/correlation/latency/token usage and action metadata.
- Do not log sensitive prompt/document content by default.
- Provider retention/training/privacy settings are deployment configuration and security review items before production enablement.

## 9. Speech-to-text

- Provider-agnostic adapter.
- Transcription is returned as editable text; user reviews before submission.
- Speech-derived commands receive identical Act confirmation/RBAC/reauth controls.
- Audio retention should be minimized; do not retain audio by default unless an explicit later policy requires it.

## 10. Data security and cryptography

- TLS in transit.
- Azure-managed encryption at rest for managed services.
- Key Vault for production secrets/keys.
- No separate application field-level encryption requirement V1.
- Secrets injected at runtime; never committed to repository or `.env.example` values.
- Rotate provider/SMTP/export secrets through operational process.

## 11. Audit and tamper resistance

Audit all:

- auth/local-account security actions
- role/scope assignments
- DR lifecycle transitions
- Task transitions/assignment changes
- Dependency/Milestone changes
- Blocker/Issue changes
- RTO/RPO validation/breach acknowledgements
- Override/exception actions and reason
- policy/health-weight/SLA changes
- AI Act prepare/confirm/execute
- report publish
- package export/import
- evidence delete/retention/legal hold

Audit is append-only to application actors. Retention = 7 years.

## 12. Supply-chain security

Repository is **private**; no GHAS assumed. If GHAS appears: add CodeQL, Dependabot, secret scanning. (D-246)

CI/CD must run:

- Python lint/type/test
- TypeScript lint/type/test
- SAST: Semgrep, Bandit (D-246)
- Python/Node dependency vulnerability scanning: pip-audit, pnpm audit (D-246)
- secret scanning: gitleaks (D-246)
- SBOM generation: Syft (D-246)
- container image scan: Grype (D-246)
- IaC lint/security checks: Checkov (Bicep) (D-246)
- migration verification (`alembic check` model↔migration drift) (D-247)
- contracts drift check (`openapi-typescript` regeneration) (D-251)

Known Critical release vulnerability blocks release unless an explicit emergency exception is documented and approved by the appropriate privileged role. Gate: no unresolved Critical. (D-246)

Patch targets: Critical 7d, High 30d, Medium 60d, Low 90d.

## 13. Telemetry/SIEM

- Structured security/audit logs to Log Analytics/Application Insights.
- Sentinel-ready schemas/correlation IDs now.
- Sentinel integration/analytics later.
- Alert on auth anomalies, repeated denied high-risk operations, export/import failures, malware detections, privilege changes and audit pipeline failures.

## 14. Threat scenarios and mitigations

| Threat | Impact | Mitigation |
|---|---|---|
| Unauthorized task/failback action | High | server RBAC + transition guards + high-risk reauth + audit |
| Coordinator account takeover | Critical | Entra/MFA + reauth + anomaly telemetry + scoped sessions |
| Prompt injection in attached runbook | High | untrusted retrieval boundary + typed tools + pre-retrieval ACL + no direct DB writes |
| Malicious evidence file | High | allowlist + 100MB cap + malware scanning/quarantine |
| Cross-tenant package import | High | tenant-bound encrypted manifest + integrity verification |
| Lost update during live DR | High | optimistic concurrency + domain conflict rules (Manager precedence is the only override, audited — D-214) + mandatory Idempotency-Key on command POSTs (D-215) |
| Exfiltration through AI provider | High | Admin-controlled providers, backend secrets, retrieval ACLs, prompt minimization, no secret logging |
| Audit tampering | High | append-only path, restricted DB permissions, retention/monitoring |
| DR tool fails in protected region | Critical | host outside protected failure domain + own RTO/RPO/backup drills |
| Email unavailable | Medium | committed DR actions remain valid; in-app source of truth; retry/telemetry |

## 15. Security release gates

- threat model reviewed
- RBAC/IDOR tests pass
- high-risk reauth tests pass
- AI tool-permission/prompt-injection tests pass
- file scan tests pass
- encryption/import negative tests pass
- audit completeness test passes
- no unaccepted Critical vulnerability
- backup/restore drill passes
- production secret scan clean
- privacy/provider review completed before production AI enablement

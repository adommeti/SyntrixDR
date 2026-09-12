# DR Command Center — V1 RBAC / Operational Authority Matrix

**Reconciled 2026-09-12 against FREEZE_ADDENDUM.md (D-201…D-261).**

| Capability | Global Admin | DR Coordinator | Work Stream Lead | App/System Owner | Business Owner | Manager | Executor |
|---|---:|---:|---:|---:|---:|---:|---:|
| Read Event participant content | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Global read-only (Auditor/Executive) — separately granted role, or per-Event enrolment; never by job title (D-222) | grants | grants / enrols | — | — | — | — | — |
| Global policy/config | ✓ | scoped | — | — | — | — | — |
| Create PLANNED DR Event (D-213) | ✓ | ✓ | — | ✓ own-Application subset only | — | — | — |
| Activate / start-failover / mark-failed-over / start-failback / close / cancel (D-213) | ✓ | ✓ | — | — | — | — | — |
| Manage Event participants (explicit enrolment) (D-222) | ✓ | ✓ | — | — | — | — | — |
| Cross-Team assignment | ✓ | ✓ | stream-scoped where policy permits | — | — | no | volunteer only |
| Own-Team reassignment | ✓ | ✓ | ✓ in scope | ✓ in scope | — | ✓ (MANAGER_PRECEDENCE on stale Coordinator/Admin assignment, D-214) | ✓ where eligible |
| Change Task metadata | ✓ | ✓ | stream | app | — | own Team where allowed | own/Team work |
| Change dependencies | ✓ | ✓ | stream per policy | app per policy | — | Team per policy | Team per policy |
| Confirm shared Milestone | ✓ | ✓ | ✓ | — | — | — | — |
| Validate application work | ✓ | ✓ | — | ✓ any slot (Primary Accountable) (D-254) | optional business note | — | — |
| Validate shared Work Stream work | ✓ | ✓ | ✓ | — | — | — | — |
| Validate Task (standard) — `READY_FOR_VALIDATION → COMPLETED`; never the Task's own executor (D-209) | ✓ | ✓ | ✓ shared-stream Tasks | ✓ Application-scoped Tasks, any slot | — | — | no (no self-complete) |
| Create/resolve/verify Blocker (verify moves VERIFIED→CLOSED atomically; Owning Team verifies) (D-211) | ✓ | ✓ | ✓ | ✓ | comment | ✓ scope | ✓ eligible |
| Start Blocker (claim) — `ASSIGNED → IN_PROGRESS` (D-211) | ✓ | ✓ | ✓ scope | ✓ scope | — | ✓ scope | ✓ assigned resolver |
| Create Issue/Finding | ✓ | ✓ | ✓ | ✓ | ✓ comment/create if permitted | ✓ | ✓ |
| Comment/@mention cross-Team | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Override cross-Work Stream guard | ✓ | ✓ | no | no | no | no | no |
| Override within Work Stream | ✓ | ✓ | ✓ where policy allows | — | — | — | — |
| Closure exception override (monitoring closure warning) (D-227) | ✓ | ✓ | — | — | — | — | — |
| Resolve Needs Review | ✓ | ✓ Event-wide | stream | app | — | managed scope | assigned scope if policy allows |
| Publish official report | ✓ | ✓ | — | — | — | — | — |
| Export/import full package | ✓ | ✓ if authorized | — | — | — | — | — |
| Create Local fallback user | ✓ | no by default | — | — | — | — | — |
| AI Act (allowlisted commands only, caller's own RBAC) (D-228) | ✓ within own RBAC | ✓ within own RBAC | ✓ within own RBAC | ✓ within own RBAC | ✓ within own RBAC | ✓ within own RBAC | ✓ within own RBAC |

## Security notes

- High-risk actions require the configured reauthentication/MFA policy.
- Authorization is enforced server-side; the table is not a UI-only contract.
- Event participants are the default read boundary. Inside the Event, cross-Team readability is intentional.
- No standalone Validator role exists in V1.
- Application/System Owner and Business Owner relationships support Primary/Secondary/Tertiary slots; technical validation belongs to Application/System Owner, not Business Owner. Any System Owner slot may perform it; the Primary is Accountable. (D-254)
- Manager precedence (D-214): a Team Manager's assignment on an own-Team Task with a stale `expected_version` is accepted when the intervening change was a Coordinator/Admin assignment; version increments, both actions are audited, the resolution is recorded as `MANAGER_PRECEDENCE` and affected users are notified. Every other stale write returns `409 CONCURRENCY_CONFLICT`. This is a domain override, not a bypass of RBAC scope.
- Idempotency (D-215): every command POST requires an `Idempotency-Key` header (400 `IDEMPOTENCY_KEY_REQUIRED` if missing), retained 24 h; GET/queries exempt. A pure pre-signed-upload handshake may be exempt, but the domain command attaching the artifact requires it.
- Participant-scoped visibility (D-222): Event read access is granted by an explicit `dr_event_participants` row. Users are auto-enrolled when they hold an Event-scoped role, are current Task assignee, are Blocker owner, are any-slot System/Application or Business Owner of an in-scope Application, lead a Work Stream, or are member/manager of an Owning Team with Tasks in the Event. Global Admins are implicit participants of every Event. Auditor/Executive visibility is a separately granted global read-only role or per-Event enrolment, never inferred from job title. WebSocket authorization uses the same boundary.
- Event lifecycle authority (D-213): Application/System Owners may create PLANNED Events scoped to their own Application(s) but hold no later lifecycle authority unless they also hold Admin/Coordinator authority.
- AI Act (D-228): AI can invoke only the allowlisted commands and always under the invoking user's own RBAC, idempotency, guards and audit. Event lifecycle, Task/App validation, report publish, package export/import, role/policy/admin/security configuration and local-user creation are never available via AI in any control profile.

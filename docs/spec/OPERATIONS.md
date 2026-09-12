**Reconciled 2026-09-12 against FREEZE_ADDENDUM.md (D-201…D-261).**

# DR Command Center — V1 Operations & Maintenance

## Service objectives

- Availability objective: 99.9%.
- Command Center RTO: 60 minutes.
- Command Center RPO: 15 minutes.
- Live connected update target: <=10 seconds.
- Normal API average target: <500 ms under POC load.

## Hosting stance

The Command Center must run outside the protected workload failure domain it coordinates. POC public ingress is allowed; Production should use private ingress/network controls appropriate to the organization.

## Environments

- Local: full Docker-based MacBook development (PostgreSQL+pgvector, Redis, Azurite, Mailpit); BUILD-01…21 run end to end locally with no Azure credentials. (D-217, D-248)
- Dev Azure resource group. (D-248)
- Test Azure resource group. (D-248)
- Production isolated resource group/subscription placement according to enterprise standards.
- Azure targets: Container Apps, Azure Database for PostgreSQL, Blob (+ Microsoft Defender for Storage), Key Vault, Log Analytics/App Insights, **Azure Managed Redis** (preferred) as Celery broker, session store and real-time pub/sub. Azure credentials are a prerequisite only for BUILD-22/23. (D-248, D-245, D-239, D-242)

## Worker sizing

- Background work runs as Celery workers on Redis (Azure Managed Redis in Azure). (D-241, D-248)
- PDF generation uses Playwright/Chromium print in the background worker container; the worker image must ship Chromium and be sized for headless-browser memory. (D-244)
- Embeddings run as `nomic-embed-text-v1.5` via sentence-transformers inside the worker (768 dimensions, identical locally and in Azure POC); the worker holds the model in memory, so size CPU/RAM for the model and for re-embedding backlogs after a model/dimension change. (D-233)
- Speech-to-text runs as local faster-whisper in the worker behind a provider interface; size accordingly. (D-232)

## Backup and recovery

- Azure Database for PostgreSQL PITR: 35 days Production.
- Object storage protection/versioning according to production storage configuration.
- Retention policy is not a substitute for operational backup.
- Run restore drills before Production and periodically thereafter.
- Validate report/evidence/document references after restore.
- Record RTO/RPO result of Command Center restore drill.

## Support

- Business-hours standard support.
- On-call coverage during scheduled/live DR activity.
- Severity-based escalation during active DR.
- No permanent 24/7 V1 support promise.

## Vulnerability/patch SLA

- Critical: 7 days.
- High: 30 days.
- Medium: 60 days.
- Low: 90 days.
- Monthly normal maintenance cadence.
- Emergency maintenance permitted for critical operational/security issues.

## Observability

OpenTelemetry traces/metrics/logs feed Application Insights/Azure Monitor/Log Analytics. Sentinel-ready structured logs are retained; Sentinel is a later integration.

Monitor at least:

- API latency/error rate
- WebSocket connections/reconnect/fanout lag
- PostgreSQL connection pool/CPU/storage/replication/PITR health
- Redis/job backlog/failures/retries (Celery queues; outbox publish lag) (D-241, D-242)
- Blob upload/malware scan failures (Defender for Storage `malware_scan_status` updates) (D-245)
- PDF worker (Playwright/Chromium) failures/duration (D-244)
- email send/retry failures
- AI provider latency/errors/tokens per provider/model
- search/indexing/re-embedding backlog
- auth failures/high-risk denied operations
- audit pipeline failures
- package export/import failures

## Email operations

SMTP provider configured in Admin/secure environment config (secret references; SMTP credentials never in policy JSON). Mailpit locally; Gmail-compatible/test SMTP may be used in development/POC. Teams/Slack are disabled "Coming Soon". Delivery failure is logged/retried and never rolls back committed DR work. (D-256, D-225)

## Retention operations

- Audit 7 years.
- Events/Tasks 3 years.
- Evidence class-configurable, default 3 years.
- Legal hold suppresses purge.
- Reports/imports/semantic indexes track relevant source retention.
- Embedding-model changes schedule re-embedding of retained source material.

## Release process

Repository is private; no GHAS assumed. CI security stack: Semgrep, Bandit, pip-audit, pnpm audit, gitleaks, Syft (SBOM), Grype (container), Checkov (Bicep). If GHAS appears: add CodeQL, Dependabot, secret scanning. Gate: no unresolved Critical. (D-246)

Branching: `feat/<milestone>-<slug>` and `fix/<slug>`; `main` protected; squash-merge only; linear history; tag `v0.NN.0` after each merged BUILD-NN; Conventional Commits. (D-202)

1. PR checks.
2. build/test/security scans/SBOM (D-246 stack).
3. Alembic migration validation (`alembic check` drift gate) (D-247).
4. immutable container images.
5. deploy Dev/Test.
6. smoke/E2E.
7. backup/rollback readiness confirmation.
8. privileged Production approval according to organizational release process.
9. run migration + deployment.
10. smoke/telemetry check.
11. rollback if health gates fail.

## Rollback

Application rollback uses previous immutable image/config. Schema changes must be backward-compatible where possible. Destructive schema migrations require staged expand/migrate/contract deployment and cannot rely on simple code rollback.

## FinOps

- Azure budgets and cost alerts.
- Track Container Apps, PostgreSQL, object storage, telemetry, Azure Managed Redis and AI provider usage separately. (D-248)
- AI provider/model usage metrics exposed without logging sensitive prompts.
- POC sizing should start small and scale after measured load tests.

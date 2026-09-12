# DR Command Center — Multidisciplinary Review Board

## Product / DR & Business Continuity SME

Accepted conclusions:

- Task completion is not Application recovery success.
- RTO and RPO are independent first-class Application outcomes.
- Shared infrastructure Work Streams and application-specific work must coexist.
- Failback is independent and required by default.
- Readiness gates are sufficient V1; no separate approval bureaucracy.
- Baseline drift and post-event reconstruction are essential.

## Principal Architecture review

Accepted conclusions:

- modular monolith, not microservices
- Python/FastAPI backend
- Next.js modern frontend
- PostgreSQL + pgvector
- WebSockets
- local Mac containers match Production architecture closely
- Azure Container Apps and managed PostgreSQL
- provider abstractions for AI/email/object storage/jobs
- host outside protected workload failure domain

## Security/Privacy review

Accepted conclusions:

- Event-participant visibility by default
- server-side scoped authorization
- high-risk reauth/MFA policy
- secure sessions/CORS/CSRF/rate limiting
- Key Vault
- encrypted tenant-bound export packages
- file malware scanning
- AI retrieved data untrusted; pre-retrieval ACL
- supply-chain scanning/SBOM
- Log Analytics now, Sentinel later

## SRE / Operations review

Accepted conclusions:

- own RTO/RPO 60m/15m
- 99.9% V1 objective
- 35-day Production DB PITR
- backup/restore + rollback release gates
- business-hours support + DR-window on-call
- patch SLA 7/30/60/90 days
- observable job/realtime/provider pipelines
- FinOps budgets/alerts

## Data / AI review

Accepted conclusions:

- relational source of truth
- structured dependencies authoritative
- pgvector and FTS for supplemental semantic retrieval
- provider-agnostic Synthetic/Anthropic V1
- embeddings separate from chat model
- nomic-embed-text-v1.5 / 768 default
- AI Read/Recommend/Act separated
- AI can be turned off
- citations/provenance for document-derived claims

## UI/UX & Accessibility review

Accepted conclusions:

- world-class command-center experience, not CRUD styling
- Figma clickable prototype before implementation sign-off
- 10-second comprehension rule
- treemap + weighted dial + critical panels + resource sidebar
- consistent hover/deep-card interaction
- Light/Dark V1
- WCAG 2.1 AA
- desktop primary with responsive core views

## QA/Test review

Accepted conclusions:

- centralized state-transition tests
- deterministic dependency/property tests
- AI-off E2E
- load test 500 Apps/5,000 Tasks/200 concurrent users
- real-time <=10 sec acceptance
- file/AI/security negative tests
- backup/rollback tests
- retention fake-clock tests

## Freeze gate

[DECIDED] These accepted reviewer conclusions are folded into the authoritative frozen specification. Implementers should not treat reviewer notes as separate optional advice when they appear as frozen decisions in the root documents.

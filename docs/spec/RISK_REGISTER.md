# DR Command Center — Frozen V1 Risk Register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Scope overload in 10-week POC | High | High | frozen Must list, weekly exit gates, defer V2/V3 integrations |
| UX becomes enterprise CRUD instead of command center | Medium | High | Figma-first, formal design signoff, 10-second comprehension gate |
| DR tool unavailable during target-region incident | Low/Med | Critical | host outside protected failure domain, RTO/RPO, backup/restore drills |
| Dependency graph incomplete | High | Medium | partial graph useful; manual edits; semantic discovery labeled supplemental |
| Wrong dependency blocks execution | Medium | High | policy controls, advisory edges, override reason/audit, Needs Review |
| Concurrent assignment overwrite | Medium | High | optimistic concurrency + explicit manager precedence rule |
| AI hallucination changes operational state | Medium | Critical | AI Act typed commands, confirmation, RBAC/guards, no direct DB access |
| Prompt injection in docs | High | High | untrusted-content boundary, retrieval ACL, typed tools, tests |
| AI provider unavailable | Medium | Medium | manual-first workflow; provider adapter; AI-off E2E |
| AI data leakage | Low/Med | High | backend-only keys, provider opt-in, prompt minimization, ACL before retrieval |
| Malicious evidence upload | Medium | High | allowlist, size cap, malware scan/quarantine |
| Export package leakage/tampering | Low | High | AES-256-GCM, Key Vault, integrity, tenant binding, high-risk reauth |
| Retention deletes legal evidence | Low/Med | High | legal hold, classification policies, purge audit, fake-clock tests |
| Audit pipeline incomplete | Low/Med | Critical | append-only events, golden-scenario coverage assertion, alert on failure |
| Email unavailable during DR | Medium | Low/Med | in-app source of truth, retry/log, DR mutation never rolls back |
| Health dial masks Tier 0 problem | Medium | High | no cap but tier weighting + large/red treemap + critical issue panel + explanation |
| Forecast appears overly authoritative | Medium | Medium | label deterministic estimate, expose inputs, no ML claim |
| RPO source timestamp inaccurate | Medium | High | evidence/validation note, explicit unknown state, audit provenance |
| Local fallback account becomes backdoor | Low | Critical | explicit creation only, strong policy/MFA, audit, production monitoring |
| PostgreSQL/pgvector scale insufficient later | Low V1 | Medium | abstraction/query metrics; external search service is future option |
| Blob/file references break after restore | Low | High | restore drills include evidence/document link validation |
| Teams/Slack expectation confusion | Medium | Low | clearly disabled Coming Soon; email/in-app are documented V1 channels |
| V2 ITSM sync creates source-of-truth conflicts | Future | High | external IDs now; explicit source precedence designed before integration |

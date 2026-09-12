**Reconciled 2026-09-12 against FREEZE_ADDENDUM.md (D-201…D-261).**

# DR Command Center — V1 Limitations & Design Considerations

## Deliberate V1 limitations

1. No functional Teams or Slack integration; UI may show Coming Soon. In-app + email are functional.
2. No ServiceNow/Remedy/CMDB sync or alert suppression.
3. No Microsoft Graph manager/org sync; hierarchy is Admin-managed in V1.
4. No Azure AI Foundry provider adapter; Synthetic/Anthropic are V1 providers.
5. No public/external status page.
6. No calendar invite/recurrence engine; dates only.
7. No skill proficiency scoring.
8. No mandatory Business Owner approval gate.
9. No formal multi-approver pre-DR approval chain.
10. No dual-control requirement for privileged actions/package operations.
11. No ML forecast; deterministic rules-only critical path.
12. No Pinecone/Chroma/external vector DB requirement.
13. No microservices/Kubernetes.
14. No permanent 24/7 V1 support commitment.
15. No SMS/voice calling; phone is contact data only.
16. No special application field-level encryption requirement beyond platform encryption/TLS.
17. No offline-write mode when API/database is unavailable.
18. POC browser certification is latest Chrome/Edge; broader browser matrix is post-POC if needed.
19. No MinIO; Azurite is the local object store, reached through the same `azure-storage-blob` `ObjectStore` adapter used for Azure Blob. (D-217)
20. No WeasyPrint; PDF is Playwright/Chromium print in the background worker. (D-244)
21. Not V1 endpoints: Event Pause/Resume, Task Skip, `/tasks/{id}/complete`, generic `/validations/{id}/approve|reject`, generic `/ai/chat`. Event cancellation exists; pause does not. (D-207)

## Critical considerations

- Command Center must live outside the workload failure domain it coordinates.
- Manager precedence is an intentional override of optimistic concurrency for own-Team assignments: a Manager's stale-version assignment on a Task owned by their Team is accepted over an intervening Coordinator/Global Admin assignment, recorded as `MANAGER_PRECEDENCE`, audited on both sides and notified. Every other stale write is a `409 CONCURRENCY_CONFLICT`; this override must not be generalised. (D-214)
- The POC being single tenant does not eliminate tenant/company binding in export packages.
- RTO and RPO are recovery outcomes; Task completion is not sufficient proof of recovery.
- Health dial is a roll-up, not a substitute for the treemap/critical-issue panel.
- A Tier 0 failure does not hard-cap the dial; criticality is expressed through tier weights and treemap size/color.
- Semantic search can discover likely relationships but cannot silently override structured dependency truth.
- AI model/provider choice is configuration; domain logic cannot depend on one provider's proprietary behavior.
- Evidence file links may point externally; the product cannot guarantee external retention/availability unless the file is copied into managed storage.
- Retention policy and legal hold must be tested with simulated time, never actual multi-year waits.
- PostgreSQL/pgvector is appropriate for V1 scale but metrics should make later search-tier extraction possible if needed.

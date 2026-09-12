# DR Command Center — V2 / V3 Roadmap and Deferred Scope

Nothing in this file is required to declare the frozen V1 POC complete unless explicitly promoted by a future product change.

## V2 candidates

1. ServiceNow integration for Application/person/Tier/service master data.
2. Remedy/other ITSM connector abstraction.
3. ITSM/monitoring alert suppression/restoration during approved DR windows.
4. Microsoft Graph manager/org sync.
5. Functional Microsoft Teams notifications/workflows.
6. Functional Slack integration where organization needs it.
7. Azure AI Foundry provider adapter.
8. Formal compliance-framework mapping and evidence-control mapping.
9. Calendar invites/recurring DR scheduling.
10. Skill proficiency levels and richer skills taxonomy.
11. Configurable mandatory Business Owner validation gates for selected Applications.
12. AI-generated draft failback plans on Coordinator/Manager request.
13. More advanced historical trend analytics.
14. Optional public/external status page if product need is established.
15. Additional dependency types beyond Finish-to-Start.
16. Richer Issue/Finding problem-management lifecycle.

## V3 / maturity candidates

1. ML/statistical recovery forecasting after enough clean historical data exists.
2. Sophisticated resource optimization/scheduling recommendations.
3. Automated discovery of unmodeled dependencies with human promotion workflow.
4. Multi-tenant SaaS productization.
5. On-premises deployment productization.
6. Cross-company/package-sharing capabilities only if security/compliance later permits them.
7. Advanced multi-region/active-active Command Center topology if business SLO requires it.

## Deferred rationale

- V1 optimizes for a real DR execution platform, not integration breadth.
- Manual-first workflow prevents AI/integration availability from becoming a DR dependency.
- Postponing Graph/ITSM avoids source-of-truth conflicts before the core domain is proven.
- Deterministic forecasting is more explainable for V1; ML waits for sufficient history.
- Email is enough to validate outbound notification abstraction; Teams/Slack follow later.

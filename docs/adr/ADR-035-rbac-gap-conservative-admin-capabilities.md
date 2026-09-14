# ADR-035 — Conservative Admin-only capabilities for RBAC_MATRIX gaps

**Status:** Accepted
**Date:** 2026-09-14
**Increment:** BUILD-03
**Relates to:** RBAC_MATRIX.md, `apps/api/app/users_teams_org/authorization.py`, ADR-034

## Context
RBAC_MATRIX.md models 26 explicit capability rows, but BUILD-03 needed to gate three new command
classes — Application master-data CRUD, `application_owners` slot management, and Tier defaults admin —
that have no corresponding row in the matrix at all. Unlike a marker gap (ADR-034's concern: how to encode
a *known* grant), this is an *absence* gap: the frozen spec is silent on who besides the implicit "someone
configures this" may perform these actions. The spec-auditor and reviewer agents both independently flagged
this silence and needed a documented, repeatable answer rather than an ad hoc per-PR judgment call.

## Decision
When RBAC_MATRIX.md has no row for a capability a new command needs, encode it as a new `Capability` enum
member with a `GRANTS` entry of exactly `{"GLOBAL_ADMIN": True}` — Admin-only, no scoped carve-out for any
other role — until a `spec/<slug>` PR from the product owner adds a real row. This is the same reasoning
FROZEN_DECISIONS.md already applies elsewhere ("no `[OPEN]` seams; most conservative reading, flagged"):
absence of a granted role is denial, not an invitation to guess who else should have it. `MANAGE_APPLICATION_CATALOG`
and `MANAGE_TIERS` (BUILD-03) are the first two capabilities minted this way; the PR that adds them must say so
explicitly in its Risks section, and the code comment on the enum member must cite this ADR.
Read-only endpoints for the same resource are a separate decision: they stay open to any authenticated user
unless RBAC_MATRIX explicitly restricts the *read* side, since D-222 participant-scoping governs Event
*content* visibility, not catalog/reference-data visibility.

## Alternatives rejected
- **Silently allow Coordinator or App Owner write access "since they're close to the data"** — invents a
  grant RBAC_MATRIX doesn't state, exactly the `[OPEN]`-seam pattern FROZEN_DECISIONS.md forbids.
- **Block the increment until the product owner amends RBAC_MATRIX.md** — the frozen-spec-silence hard rule
  (`.claude/skills/drcc-build-increment`) says implement the conservative reading and flag it, not stall.
- **A generic "capability requires explicit spec citation" enum wrapper that fails fast at import time if
  no RBAC_MATRIX row exists** — rejected as over-engineering for two capabilities; revisit if a future
  increment mints a third or fourth gap-filling capability and the pattern needs enforcing mechanically.

## Consequences
- Any reviewer or spec-auditor scanning `authorization.py` can `grep` for a comment citing this ADR to find
  every gap-filling capability without re-deriving the reasoning each time.
- If the product owner later grants a non-Admin role access via a `spec/<slug>` PR, the fix is a one-line
  `GRANTS` dict change plus removing the ADR-035 citation comment — no route/command code changes needed,
  since `AuthorizationService.require()` already reads the grant from data.
- A future increment that needs a similar gap-filling capability should follow the same pattern rather than
  inventing a new convention, and should reference this ADR in its own PR's Risks section.

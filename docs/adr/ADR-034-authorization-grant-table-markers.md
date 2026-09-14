# ADR-034 — Authorization grant-table marker design

**Status:** Accepted
**Date:** 2026-09-14
**Increment:** BUILD-02
**Relates to:** RBAC_MATRIX.md, `apps/api/app/users_teams_org/authorization.py`

## Context
RBAC_MATRIX.md's 26 capability rows are not uniformly boolean per role — a cell can mean "granted at
any scope the role holds", "granted only when the role is assigned at the exact scope passed to the
check", "granted only for a GLOBAL-scoped assignment of that role", or (Manager only) "granted when the
actor manages the Team named by the scope, independent of `role_assignments` entirely". The frozen spec
states this as prose per row; encoding `AuthorizationService.require()`/`.can()` required picking a
concrete in-code representation for these four semantics. That representation is an engineering choice,
not a product decision — RBAC_MATRIX's actual grants are unchanged either way.

## Decision
`GRANTS: dict[Capability, dict[str, bool | Literal["SCOPE", "OWN_TEAM"]]]` — one dict literal per
capability, mirroring the spec table cell-for-cell:
- `True` — role holder is granted the capability at any scope it holds an assignment for.
- `"SCOPE"` — granted only when `role_assignments.scope_type`/`scope_id` for that role exactly match the
  `Scope` passed to `require()`/`.can()`.
- `"OWN_TEAM"` — `MANAGER` only; checked via `teams.manager_user_id == actor_id` against
  `scope.owning_team_id`, never via `role_assignments` (there is no `TEAM` `role_scope_type` in the
  schema). Structurally different from the other three markers because it isn't a role-assignment lookup
  at all.
- Missing entry for a role — denied (`no`/`—` in RBAC_MATRIX renders as simply absent from the dict).

`GLOBAL_ADMIN` and `GLOBAL_READONLY` short-circuit before the table lookup: GLOBAL_ADMIN bypasses every
capability (gated additionally on TOTP enrolment for LOCAL identities per D-235); GLOBAL_READONLY
bypasses only capabilities in the separate `READ_ONLY_CAPABILITIES` frozenset, never reaching `GRANTS`.

## Alternatives rejected
- **Per-capability `check_x(...)` functions** — would scatter RBAC_MATRIX's 26 rows across 26 bespoke
  functions instead of one reviewable table; harder to spot-check against the spec doc directly.
- **A generic `scope_type` field on `role_assignments` covering Team scope** — would let `OWN_TEAM` reuse
  the same `role_assignments` lookup as `SCOPE`, but the schema has no `TEAM` scope and RBAC_MATRIX ties
  Manager's own-Team grant to `teams.manager_user_id`, not a role assignment; adding a schema-level scope
  type nobody else needs was rejected as scope creep on a frozen schema.

## Consequences
- Adding a new capability is a one-line dict entry, directly diffable against the spec table.
- A future module needing a scope kind beyond GLOBAL/DR_EVENT/WORK_STREAM/APPLICATION/OWN_TEAM must
  extend this same four-marker vocabulary rather than inventing a fifth ad hoc one.
- Test coverage: `apps/api/tests/test_authz_matrix.py` parametrizes over every `Capability` member
  (one granted + one denied case per RBAC_MATRIX row), plus targeted tests for `SCOPE` mismatch,
  `OWN_TEAM` cross-Team denial, and GLOBAL_READONLY's narrow bypass.

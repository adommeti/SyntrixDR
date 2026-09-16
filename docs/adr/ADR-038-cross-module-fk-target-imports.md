# ADR-038 — Cross-module `models.py` imports for FK-target registration

**Status:** Accepted
**Date:** 2026-09-15
**Increment:** BUILD-04
**Relates to:** ADR-032 (incremental Alembic model adoption) · `.claude/rules/python-api.md`

## Context

`.claude/rules/python-api.md`'s cross-module rule says: "import another module's `commands.py`/
`queries.py`, never its `repository.py` or `models.py` (except type-only imports guarded by
`TYPE_CHECKING`)." ADR-032 already carves out one exception — the `core/external_refs.py`
FK-resolution-stub pattern — for FK targets whose owning module doesn't exist yet
(`tasks`/`task_dependencies`/`milestones` as of BUILD-04).

BUILD-04's `dr_events/models.py` and `plans_import/models.py` need `Application`/`Tier`/`User` as
FK targets too, but those owning modules (`applications_catalog`, `users_teams_org`) already exist
with real ORM-mapped classes. A fresh code review during BUILD-04 flagged the existing (session a)
non-`TYPE_CHECKING` import of `User` as a rule violation, and BUILD-04 extended the same pattern to
`Application`/`Tier` — surfacing the same objection twice. This ADR settles it so it isn't
re-litigated on every subsequent module.

## Decision

A module's `models.py` may import another module's real ORM-mapped class directly (not
`TYPE_CHECKING`-guarded) **only** to register that class on `Base.metadata` before mapper
configuration, when:

1. The FK target's owning module already exists with a real model (if it doesn't yet, use the
   `core/external_refs.py` stub pattern instead — ADR-032).
2. The import is written as a no-op registration line, not consumed elsewhere in the file (e.g.
   `_ = (User, Application, Tier)  # registers cross-module FK targets on Base.metadata`), so it's
   visibly a metadata-registration shim, not a layering shortcut for business logic.
3. `ForeignKey(...)` targets are still declared as lazy strings (`"applications.id"`), never a
   direct class reference — the import's only job is registration, not the FK declaration itself.

`TYPE_CHECKING` guarding is explicitly wrong here: the import must run at real import time so
`Base.metadata` has the class registered regardless of which test file imports this module first
(the exact failure mode this pattern prevents — `NoReferencedTableError` in isolated test runs).

## Alternatives rejected

- **`TYPE_CHECKING`-guard the import** (the rule's literal exception) — doesn't work: a
  type-only import is erased at runtime, so `Base.metadata` never actually gets the class.
- **Use the `core/external_refs.py` stub pattern for every FK target, including ones with real
  models** — would register a second, conflicting `Table` object for a class SQLAlchemy already
  maps, risking "table already defined" errors or divergent column definitions between the stub
  and the real model.
- **Route through `commands.py`/`queries.py`** — not applicable; this isn't a business-logic call,
  it's ORM registry bootstrapping that must happen at class-definition time, before any command
  or query runs.

## Consequences

- Future modules needing a real cross-module FK target should follow this same shape (bare import
  + `_ = (...)` registration line) rather than reopening the layering question per PR.
- `spec-auditor`/`reviewer` agents should treat this shape as pre-approved, not a fresh finding,
  when the three conditions above hold — but should still flag it if a future import starts being
  used for anything beyond registration (relationship access, isinstance checks, etc.), since that
  would be the actual layering violation the base rule guards against.
- No product behavior changes; this is purely an internal module-boundary convention.

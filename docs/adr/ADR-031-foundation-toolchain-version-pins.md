# ADR-031 — Foundation toolchain: TypeScript 6.0.3 and ESLint 9.39.5, not the newest majors

**Status:** Accepted
**Date:** 2026-09-13
**Increment:** BUILD-01
**Relates to:** D-240 (exact version pin at BUILD-01, lockfiles committed, no floating versions in CI)

## Context
D-240 requires an exact-pinned toolchain with no floating versions, decided at BUILD-01. At the time of
scaffolding, npm's `latest` dist-tags were TypeScript 7.0.2 (the Go-native compiler rewrite), ESLint
10.10.0, Next.js 16.3.5, React 19.3.0 and Tailwind 4.3.3 — all released very recently relative to the
increment date. D-240 says "exact version pinned," not "the newest major," and picking the literal
newest release turned out to have two concrete, verified breakages rather than being a style question:

- `typescript-eslint` 8.70.0 (the only release line, including canary) declares a peer range of
  `typescript: >=4.8.4 <6.1.0` — TypeScript 7.0.2 is outside it, and no TS7-compatible `typescript-eslint`
  exists anywhere on the registry.
- `eslint-config-next` 16.3.5 bundles `eslint-plugin-react` 7.37.5, whose own peer range caps at
  `eslint: ^9.7`. Running it under ESLint 10 crashed with `TypeError: context.getFilename is not a
  function` on every file — a real rule-context API removal in ESLint 10 that `eslint-plugin-react`
  hasn't caught up to.

## Decision
Pin `typescript` to `6.0.3` (latest release inside `typescript-eslint`'s supported range) and `eslint`
to `9.39.5` (latest 9.x, the line `eslint-config-next`/`eslint-plugin-react` actually support).
`next`, `react`, `react-dom` and `tailwindcss` stay on their current majors (16 / 19 / 4). `eslint.config.mjs`
imports `eslint-config-next`'s array export directly — it already ships native flat config; `FlatCompat`
was tried first and separately crashed converting a modern flat plugin object through the legacy schema
validator (`TypeError: Converting circular structure to JSON`), so it isn't needed once ESLint is on 9.

## Alternatives rejected
- **Pin everything to `latest`, including TS7/ESLint10** — rejected: TypeScript-aware linting would not
  run at all (crash), and there is no supported path to fix it from this repo's side; it depends on
  upstream `typescript-eslint`/`eslint-plugin-react` releases that don't exist yet.
- **Drop `eslint-config-next` and hand-roll a minimal flat config under ESLint 10** — rejected: loses
  Next's maintained recommended rule set and the project rule to use `eslint-config-next` directly: not
  worth it just to chase the newest ESLint minor.

## Consequences
- Bumping `typescript` past `6.1.0` or `eslint` past `9.x` requires re-checking `typescript-eslint`'s and
  `eslint-plugin-react`'s peer ranges first — don't "helpfully" float these forward without re-verifying.
- `apps/web/package.json` pins are exact (no `^`/`~`); `pnpm-lock.yaml` is committed (D-240).
- Follow-up: revisit this pin once `typescript-eslint` ships TS7 support and `eslint-plugin-react` ships
  ESLint 10 support — check both, not just one, since either alone still blocks the other pin.

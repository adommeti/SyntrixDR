# Syntrix DR Command Center (SyntrixDR)

Syntrix DR Command Center — a real-time, AI-assisted internal command platform for planning, executing, validating, failing back, auditing and reporting
disaster-recovery exercises and real incidents. Manual-first: every critical workflow works with AI disabled.

**Repository:** https://github.com/adommeti/SyntrixDR (private) · **Code name:** `drcc` (env prefix `DRCC_`, image and resource names)  
**Status:** V1 POC in build. Spec frozen 2026-09-11, reconciled 2026-09-12 (`docs/spec/FREEZE_ADDENDUM.md`).

## Stack

Next.js (App Router, TypeScript strict, Tailwind, shadcn/ui, TanStack Query) · FastAPI (Python 3.12, Pydantic v2, SQLAlchemy 2,
Alembic) · PostgreSQL 16 + pgvector · Redis + Celery · WebSockets · Azurite/Azure Blob · Entra ID OIDC + Local fallback ·
Synthetic/Anthropic AI adapters · Azure Container Apps + Bicep.

## Quick start (macOS)

```bash
brew install uv node@22 pnpm gh libpq && brew install --cask docker   # once
cp .env.example .env
make dev            # postgres+pgvector, redis, azurite, mailpit
make install        # uv sync + pnpm install + playwright browsers
make db-upgrade
make seed
cd apps/api && uv run uvicorn app.main:app --reload --port 8000 &   # API
pnpm --filter web dev                                               # web on :3000
make verify         # lint + typecheck + tests + migration + contracts drift
```

Mailpit UI: http://localhost:8025 · API docs: http://localhost:8000/docs · Azurite blob: http://127.0.0.1:10000

## Repository map

| Path | Purpose |
|---|---|
| `docs/spec/` | Frozen canon (read-only on code branches; CI enforces). Start with `FREEZE_ADDENDUM.md`, `FROZEN_DECISIONS.md`. |
| `docs/adr/` · `docs/reviews/` | Engineering decisions · review/sign-off evidence |
| `apps/api` · `apps/web` | FastAPI modular monolith · Next.js command center |
| `packages/contracts` · `packages/ui` · `packages/config` | Generated API types · shared UI primitives · shared lint/ts config |
| `infra/bicep` · `infra/docker` · `infra/runner` | Azure IaC · local container init · self-hosted CI runner preparation |
| `tests/e2e` · `tests/load` | Playwright · k6 |

## Working agreement

One `BUILD-NN` increment = one `feat/build-NN-<slug>` branch = one squash-merged PR titled `BUILD-NN: …`, tagged `v0.NN.0`.
`main` is protected; CI (`.github/workflows/ci.yml`) runs repo-hygiene, spec-frozen check, api, web, security, and e2e (on
`main` or the `e2e` label) on the self-hosted Linux runner (labels `self-hosted, linux, syntrixdr-linux`; host prep in
`infra/runner/README.md`). Verification checklist: `docs/spec/VERIFY.md`.

## Licence

Internal. All rights reserved.

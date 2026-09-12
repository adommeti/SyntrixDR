# DR Command Center — developer entry points
#
# POSIX-make friendly: .PHONY, simple variables, no GNU-only functions.
# Python runs through `uv run` from apps/api; web through `pnpm --filter web`;
# platform services through `docker compose` (D-240, D-241, D-250).
#
# `make help` lists targets. `make verify` is the pre-"done" gate (docs/spec/VERIFY.md).

.POSIX:
.SUFFIXES:

SHELL = /bin/sh

COMPOSE   = docker compose
API_DIR   = apps/api
WEB_DIR   = apps/web
UV        = uv run
PNPM_WEB  = pnpm --filter web
PYTEST    = $(UV) pytest
ALEMBIC   = $(UV) alembic
K6        = k6

# Tune per invocation: make test-api PYTEST_ARGS="-x -k tasks"
PYTEST_ARGS =
# Extra Playwright args: make e2e E2E_ARGS="--grep @ai-off"
E2E_ARGS =
# Alembic revision message for `make db-revision`.
MSG = change

.PHONY: help dev dev-down verify-env install lint format typecheck \
        test test-api test-web test-auth test-domain test-transitions \
        e2e load-smoke verify \
        db-upgrade db-downgrade-one db-revision db-check \
        contracts security seed seed-load clean release-verify

help:
	@echo "DR Command Center"
	@echo ""
	@echo "  dev               start postgres/redis/azurite/mailpit (default compose profile)"
	@echo "  dev-down          stop services (volumes kept; 'make clean' removes them)"
	@echo "  verify-env        check .env, docker, uv, pnpm, node, python versions and service health"
	@echo "  install           install Python (uv sync) and Node (pnpm install) dependencies"
	@echo "  lint              ruff + eslint + prettier --check"
	@echo "  format            ruff format + prettier --write"
	@echo "  typecheck         pyright + tsc --noEmit"
	@echo "  test              test-api + test-web"
	@echo "  test-api          pytest (all markers)"
	@echo "  test-web          vitest run"
	@echo "  test-auth         pytest -m auth        (BUILD-02 authorization matrix)"
	@echo "  test-domain       pytest -m domain      (BUILD-03 domain rules)"
	@echo "  test-transitions  pytest -m transitions (BUILD-04 lifecycle + invalid transitions)"
	@echo "  e2e               playwright (tests/e2e) against E2E_BASE_URL"
	@echo "  load-smoke        k6 smoke (tests/load/smoke.js) against LOAD_BASE_URL"
	@echo "  verify            lint + typecheck + test + db-check + contracts (drift check)"
	@echo "  db-upgrade        alembic upgrade head"
	@echo "  db-downgrade-one  alembic downgrade -1"
	@echo "  db-revision       alembic revision --autogenerate (make db-revision MSG=\"add x\"; review before commit; D-247)"
	@echo "  db-check          upgrade from empty + alembic check (model/migration drift)"
	@echo "  contracts         regenerate packages/contracts from OpenAPI and fail on drift (D-251)"
	@echo "  security          scripts/security.sh — semgrep bandit pip-audit pnpm-audit gitleaks syft grype checkov (D-246)"
	@echo "  seed              golden East US 2 -> Central US scenario (D-236)"
	@echo "  seed-load         500 Applications / 5,000 Tasks POC load seed (D-259)"
	@echo "  clean             stop services, drop volumes, remove caches/build output"
	@echo "  release-verify    verify + e2e + load-smoke + security + AI-off e2e (BUILD-24 gate)"

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
dev:
	@test -f .env || { echo "ERROR: .env missing. cp .env.example .env and set SESSION_SECRET/CSRF_SECRET."; exit 1; }
	$(COMPOSE) up -d --wait postgres redis azurite mailpit
	@echo "postgres :5432  redis :6379  azurite :10000  mailpit smtp :1025 / ui http://localhost:8025"

dev-down:
	$(COMPOSE) --profile apps --profile test down --remove-orphans

verify-env:
	@fail=0; \
	echo "== tools"; \
	for t in docker uv pnpm node python3; do \
	  if command -v $$t >/dev/null 2>&1; then printf '  %-8s %s\n' $$t "$$($$t --version 2>&1 | head -n 1)"; else echo "  MISSING $$t"; fail=1; fi; \
	done; \
	$(COMPOSE) version >/dev/null 2>&1 || { echo "  MISSING docker compose v2"; fail=1; }; \
	node -e 'const v=process.versions.node.split(".")[0]; process.exit(v>=22?0:1)' 2>/dev/null || { echo "  Node 22 LTS required (D-240)"; fail=1; }; \
	python3 -c 'import sys; sys.exit(0 if sys.version_info[:2]>=(3,12) else 1)' 2>/dev/null || { echo "  Python 3.12+ required (D-240)"; fail=1; }; \
	echo "== env"; \
	if [ -f .env ]; then \
	  for k in SESSION_SECRET CSRF_SECRET DATABASE_URL REDIS_URL AZURE_STORAGE_CONNECTION_STRING; do \
	    if grep -Eq "^$$k=.+" .env; then echo "  ok      $$k"; else echo "  MISSING $$k in .env"; fail=1; fi; \
	  done; \
	else echo "  MISSING .env (cp .env.example .env)"; fail=1; fi; \
	echo "== services"; \
	for s in postgres redis azurite mailpit; do \
	  st=$$($(COMPOSE) ps --format '{{.Health}}' $$s 2>/dev/null || true); \
	  case "$$st" in healthy) echo "  healthy $$s";; "") echo "  down    $$s (make dev)";; *) echo "  $$st $$s"; fail=1;; esac; \
	done; \
	[ $$fail -eq 0 ] && echo "verify-env: OK" || { echo "verify-env: FAILED"; exit 1; }

install:
	cd $(API_DIR) && uv sync --all-groups
	pnpm install --frozen-lockfile
	$(PNPM_WEB) exec playwright install --with-deps chromium

# ---------------------------------------------------------------------------
# Static quality
# ---------------------------------------------------------------------------
lint:
	cd $(API_DIR) && $(UV) ruff check . && $(UV) ruff format --check .
	$(PNPM_WEB) lint
	pnpm exec prettier --check "apps/web/**/*.{ts,tsx,css,md}" "packages/**/*.{ts,tsx,json,md}"

format:
	cd $(API_DIR) && $(UV) ruff check --fix . && $(UV) ruff format .
	pnpm exec prettier --write "apps/web/**/*.{ts,tsx,css,md}" "packages/**/*.{ts,tsx,json,md}"

typecheck:
	cd $(API_DIR) && $(UV) pyright
	$(PNPM_WEB) typecheck

# ---------------------------------------------------------------------------
# Tests (pytest markers: auth, domain, transitions, integration, api, ai, files)
# ---------------------------------------------------------------------------
test: test-api test-web

test-api:
	cd $(API_DIR) && $(PYTEST) -q $(PYTEST_ARGS)

test-web:
	$(PNPM_WEB) exec vitest run

test-auth:
	cd $(API_DIR) && $(PYTEST) -q -m auth $(PYTEST_ARGS)

test-domain:
	cd $(API_DIR) && $(PYTEST) -q -m domain $(PYTEST_ARGS)

test-transitions:
	cd $(API_DIR) && $(PYTEST) -q -m transitions $(PYTEST_ARGS)

e2e:
	pnpm exec playwright test --config tests/e2e/playwright.config.ts $(E2E_ARGS)

load-smoke:
	@command -v $(K6) >/dev/null 2>&1 || { echo "k6 not installed: https://grafana.com/docs/k6/latest/set-up/install-k6/"; exit 1; }
	$(K6) run --env BASE_URL="$${LOAD_BASE_URL:-http://localhost:8000}" tests/load/smoke.js

# The pre-"done" gate. Order matters: cheap checks first.
verify: lint typecheck test db-check contracts
	@echo "verify: OK"

# ---------------------------------------------------------------------------
# Database (D-247: schema_v1.sql = 0001, reconciliation = 0002, hand-written models)
# ---------------------------------------------------------------------------
db-upgrade:
	cd $(API_DIR) && $(ALEMBIC) upgrade head

db-downgrade-one:
	cd $(API_DIR) && $(ALEMBIC) downgrade -1

db-revision:
	cd $(API_DIR) && $(ALEMBIC) revision --autogenerate -m "$(MSG)"
	@echo "Review the generated revision: autogenerate never redefines the domain (D-247)."

# Migration up-from-empty + model/migration drift. Uses the `test` compose profile
# so the developer volume is never touched (D-250).
db-check:
	$(COMPOSE) --profile test up -d --wait postgres-test
	cd $(API_DIR) && DATABASE_URL="$${TEST_DATABASE_URL:-postgresql+psycopg://drcc:drcc@localhost:5433/drcc_test}" \
	  sh -c '$(ALEMBIC) downgrade base && $(ALEMBIC) upgrade head && $(ALEMBIC) check && $(ALEMBIC) downgrade -1 && $(ALEMBIC) upgrade head'
	@echo "db-check: OK"

# ---------------------------------------------------------------------------
# Contracts (D-251): FastAPI OpenAPI -> packages/contracts via openapi-typescript.
# CI fails when the committed output differs from a fresh generation.
# ---------------------------------------------------------------------------
contracts:
	cd $(API_DIR) && $(UV) python -m app.tooling.export_openapi --out ../../packages/contracts/openapi.json
	pnpm --filter @drcc/contracts generate
	@if ! git diff --quiet -- packages/contracts; then \
	  echo "contracts: DRIFT — packages/contracts differs from the API. Commit the regenerated files."; \
	  git --no-pager diff --stat -- packages/contracts; exit 1; \
	fi
	@echo "contracts: OK"

# ---------------------------------------------------------------------------
# Security (D-246). Tools not installed print SKIP; findings exit non-zero.
# ---------------------------------------------------------------------------
security:
	sh scripts/security.sh

# ---------------------------------------------------------------------------
# Seeds (fictitious data only, D-236)
# ---------------------------------------------------------------------------
seed:
	cd $(API_DIR) && $(UV) python -m app.tooling.seed --scenario golden

seed-load:
	cd $(API_DIR) && $(UV) python -m app.tooling.seed --scenario load --applications 500 --tasks 5000

# ---------------------------------------------------------------------------
# Housekeeping
# ---------------------------------------------------------------------------
clean:
	$(COMPOSE) --profile apps --profile test down -v --remove-orphans
	rm -rf $(API_DIR)/.pytest_cache $(API_DIR)/.ruff_cache $(API_DIR)/.mypy_cache $(API_DIR)/htmlcov $(API_DIR)/.coverage
	rm -rf $(WEB_DIR)/.next $(WEB_DIR)/coverage tests/e2e/test-results tests/e2e/playwright-report
	find . -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true

# ---------------------------------------------------------------------------
# Release gate (BUILD-24; TEST_STRATEGY.md + SECURITY_REVIEW.md §15).
# Manual gates (WCAG review, 10-second UX sign-off, backup/restore and rollback
# drills, Figma sign-off) are recorded in VERIFY.md, not automated here.
# ---------------------------------------------------------------------------
release-verify: verify security e2e load-smoke
	AI_ENABLED=false $(MAKE) e2e E2E_ARGS="--grep @ai-off"
	@echo "release-verify: automated gates OK. Complete the manual gate table in docs/spec/VERIFY.md."

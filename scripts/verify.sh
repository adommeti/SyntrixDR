#!/usr/bin/env bash
# SyntrixDR verification gate. Used locally (--quick <scopes> before finishing a change; full before a PR) and by CI.
#   scripts/verify.sh                 full: lint, typecheck, api tests, web tests, db-check, contracts drift
#   scripts/verify.sh --quick api web quick: lint+typecheck+unit tests for the named scopes only
#   scripts/verify.sh --migrate-fresh  additionally: upgrade from an empty DB and run alembic check
# Exits non-zero on the first failing stage and prints a compact tail. Stages for unscaffolded apps are SKIPped.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

QUICK=0; MIGRATE_FRESH=0; SCOPES=()
for a in "$@"; do
  case "$a" in
    --quick) QUICK=1 ;;
    --migrate-fresh) MIGRATE_FRESH=1 ;;
    api|web|infra) SCOPES+=("$a") ;;
    *) echo "unknown arg: $a" >&2; exit 64 ;;
  esac
done
[ ${#SCOPES[@]} -eq 0 ] && SCOPES=(api web infra)
in_scope() { for s in "${SCOPES[@]}"; do [ "$s" = "$1" ] && return 0; done; return 1; }

fail=0
run() { # run <label> <command...>
  local label="$1"; shift
  printf '▶ %s\n' "$label"
  local out
  if out="$("$@" 2>&1)"; then
    printf '  ✔ %s\n' "$label"
  else
    printf '  ✘ %s\n%s\n' "$label" "$(printf '%s' "$out" | tail -n 40 | sed 's/^/    /')"
    fail=1
    return 1
  fi
}

API_READY=0; [ -f apps/api/pyproject.toml ] && API_READY=1
WEB_READY=0; [ -f apps/web/package.json ] && WEB_READY=1

# ---------------- api ----------------
if in_scope api; then
  if [ $API_READY = 1 ]; then
    ( cd apps/api
      run "ruff check"   uv run ruff check . || exit 1
      run "ruff format --check" uv run ruff format --check . || exit 1
      run "pyright"      uv run pyright || exit 1
      if [ $QUICK = 1 ]; then
        # Exit 5 = "no tests collected", which is expected before any BUILD adds
        # domain/transitions/auth-marked tests; only a real failure (any other
        # nonzero exit) should fail the quick gate.
        run "pytest (unit: domain+transitions+auth)" bash -c \
          'uv run pytest -q -m "domain or transitions or auth" -x --no-header -p no:cacheprovider; ec=$?; [ "$ec" = 5 ] && exit 0; exit "$ec"' \
          || exit 1
      else
        run "pytest (all)" uv run pytest -q --no-header -p no:cacheprovider || exit 1
      fi
    ) || fail=1
  else
    echo "  – api: SKIP (apps/api not scaffolded)"
  fi
fi

# ---------------- web ----------------
if in_scope web; then
  if [ $WEB_READY = 1 ]; then
    run "tsc --noEmit"  pnpm --filter web exec tsc --noEmit || true
    run "eslint"        pnpm --filter web lint || true
    run "prettier --check" pnpm --filter web exec prettier --check . || true
    run "vitest"        pnpm --filter web test -- --run || true
    if [ $QUICK = 0 ]; then run "next build" pnpm --filter web build || true; fi
  else
    echo "  – web: SKIP (apps/web not scaffolded)"
  fi
fi

# ---------------- infra ----------------
if in_scope infra && [ $QUICK = 0 ]; then
  if ls infra/bicep/*.bicep >/dev/null 2>&1 && command -v az >/dev/null 2>&1; then
    for f in infra/bicep/*.bicep; do run "bicep build $f" az bicep build --file "$f" --stdout || true; done
  else
    echo "  – infra: SKIP (no bicep files or az cli)"
  fi
fi

# ---------------- db + contracts (full only) ----------------
if [ $QUICK = 0 ] && in_scope api && [ $API_READY = 1 ]; then
  if [ $MIGRATE_FRESH = 1 ] || [ "${CI:-}" = "true" ]; then
    run "db-check (empty → head, alembic check, down/up)" make db-check || true
  else
    ( cd apps/api && run "alembic check" uv run alembic check ) || true
  fi
  run "contracts drift" make contracts || true
fi

if [ $fail = 0 ]; then echo "verify: PASS (${SCOPES[*]}${QUICK:+ quick})"; exit 0; fi
echo "verify: FAIL (${SCOPES[*]})"; exit 1

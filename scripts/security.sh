#!/bin/sh
# DR Command Center — CI security toolchain (D-246, SECURITY_REVIEW.md §12)
#
# Private repo, no GHAS assumed. Runs, in order:
#   semgrep   SAST (Python + TypeScript)
#   bandit    Python SAST
#   pip-audit Python dependency vulnerabilities
#   pnpm      Node dependency vulnerabilities (pnpm audit)
#   gitleaks  secret scanning (working tree + history)
#   syft      SBOM generation (SPDX JSON) for repo and built images
#   grype     vulnerability scan of the SBOM / images
#   checkov   Bicep / IaC misconfiguration scan
#
# Contract:
#   - A tool that is not installed prints "SKIP <tool>: <install hint>" and does
#     NOT fail the run (local developers may lack some tools).
#   - Exit non-zero only when a tool that DID run reports findings at or above
#     the gate. Gate (FROZEN §17.5): no unresolved Critical. Locally, HIGH is
#     reported but does not block; set SECURITY_FAIL_ON=high to tighten.
#   - Set SECURITY_REQUIRE_ALL=1 (CI) to turn SKIP into FAIL — CI must have
#     every tool installed so a missing scanner cannot silently pass a gate.
#
# Usage: sh scripts/security.sh            (from repo root; `make security`)
#        SECURITY_FAIL_ON=high SECURITY_REQUIRE_ALL=1 sh scripts/security.sh

set -u

ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT" || exit 2

OUT_DIR="${SECURITY_OUT_DIR:-$ROOT/.security}"
mkdir -p "$OUT_DIR"

FAIL_ON="${SECURITY_FAIL_ON:-critical}"      # critical | high
REQUIRE_ALL="${SECURITY_REQUIRE_ALL:-0}"
API_DIR="$ROOT/apps/api"
WEB_DIR="$ROOT/apps/web"
BICEP_DIR="$ROOT/infra/bicep"
IMAGES="${SECURITY_IMAGES:-}"                # optional: "drcc-api:local drcc-web:local"

failures=0
skips=0
ran=0

say()  { printf '%s\n' "$*"; }
head_() { say ""; say "== $1"; }
skip() {
  skips=$((skips + 1))
  say "SKIP $1: $2"
  if [ "$REQUIRE_ALL" = "1" ]; then
    say "FAIL $1: SECURITY_REQUIRE_ALL=1 and tool missing"
    failures=$((failures + 1))
  fi
}
fail() { failures=$((failures + 1)); say "FAIL $1: $2"; }
pass() { say "PASS $1"; }
have() { command -v "$1" >/dev/null 2>&1; }

# Severity gate helper: given counts, decide.
gate() {
  tool=$1; crit=$2; high=$3
  if [ "$crit" -gt 0 ]; then
    fail "$tool" "$crit critical finding(s)"
  elif [ "$FAIL_ON" = "high" ] && [ "$high" -gt 0 ]; then
    fail "$tool" "$high high finding(s) (SECURITY_FAIL_ON=high)"
  else
    [ "$high" -gt 0 ] && say "WARN $tool: $high high finding(s) (not blocking at FAIL_ON=$FAIL_ON)"
    pass "$tool"
  fi
}

# JSON count helper — uses python3 (always present: the API is Python).
jcount() { python3 -c "import json,sys; print($2)" "$1" 2>/dev/null || echo 0; }

# ---------------------------------------------------------------------------
head_ "semgrep (SAST)"
if have semgrep; then
  ran=$((ran + 1))
  semgrep scan --config p/ci --config p/python --config p/typescript --config p/secrets \
    --metrics=off --error --json --output "$OUT_DIR/semgrep.json" \
    --exclude node_modules --exclude .next --exclude .venv "$ROOT" >/dev/null 2>&1
  rc=$?
  errs=$(jcount "$OUT_DIR/semgrep.json" "sum(1 for r in json.load(open(sys.argv[1]))['results'] if r['extra']['severity']=='ERROR')")
  warns=$(jcount "$OUT_DIR/semgrep.json" "sum(1 for r in json.load(open(sys.argv[1]))['results'] if r['extra']['severity']=='WARNING')")
  if [ "$rc" -gt 1 ]; then fail semgrep "tool error rc=$rc"; else gate semgrep "$errs" "$warns"; fi
else
  skip semgrep "pipx install semgrep  (or: uv tool install semgrep)"
fi

# ---------------------------------------------------------------------------
head_ "bandit (Python SAST)"
if [ -d "$API_DIR" ]; then
  if have bandit; then
    ran=$((ran + 1))
    bandit -r "$API_DIR/app" -q -f json -o "$OUT_DIR/bandit.json" -x tests >/dev/null 2>&1
    high=$(jcount "$OUT_DIR/bandit.json" "sum(1 for r in json.load(open(sys.argv[1]))['results'] if r['issue_severity']=='HIGH' and r['issue_confidence'] in ('HIGH','MEDIUM'))")
    # bandit has no CRITICAL level; treat HIGH/HIGH-confidence as the blocking tier.
    gate bandit "$high" 0
  else
    skip bandit "uv tool install bandit"
  fi
else
  say "SKIP bandit: apps/api not present yet"
fi

# ---------------------------------------------------------------------------
head_ "pip-audit (Python dependencies)"
if [ -f "$API_DIR/uv.lock" ]; then
  if have pip-audit; then
    ran=$((ran + 1))
    ( cd "$API_DIR" && uv export --frozen --no-hashes --all-groups -o "$OUT_DIR/requirements.txt" >/dev/null 2>&1 ) \
      && pip-audit -r "$OUT_DIR/requirements.txt" --format json --output "$OUT_DIR/pip-audit.json" --progress-spinner off >/dev/null 2>&1
    rc=$?
    if [ "$rc" -gt 1 ]; then
      fail pip-audit "tool error rc=$rc"
    else
      # pip-audit does not score severity; any known vulnerability blocks (patch SLA is separate).
      vulns=$(jcount "$OUT_DIR/pip-audit.json" "sum(len(d.get('vulns',[])) for d in json.load(open(sys.argv[1]))['dependencies'])")
      gate pip-audit "$vulns" 0
    fi
  else
    skip pip-audit "uv tool install pip-audit"
  fi
else
  say "SKIP pip-audit: apps/api/uv.lock not present yet"
fi

# ---------------------------------------------------------------------------
head_ "pnpm audit (Node dependencies)"
if [ -f "$ROOT/pnpm-lock.yaml" ]; then
  if have pnpm; then
    ran=$((ran + 1))
    pnpm audit --json --prod > "$OUT_DIR/pnpm-audit.json" 2>/dev/null
    crit=$(jcount "$OUT_DIR/pnpm-audit.json" "json.load(open(sys.argv[1]))['metadata']['vulnerabilities'].get('critical',0)")
    high=$(jcount "$OUT_DIR/pnpm-audit.json" "json.load(open(sys.argv[1]))['metadata']['vulnerabilities'].get('high',0)")
    gate pnpm-audit "$crit" "$high"
  else
    skip pnpm-audit "corepack enable && corepack prepare pnpm@latest --activate"
  fi
else
  say "SKIP pnpm-audit: pnpm-lock.yaml not present yet"
fi

# ---------------------------------------------------------------------------
head_ "gitleaks (secrets)"
if have gitleaks; then
  ran=$((ran + 1))
  if [ -d "$ROOT/.git" ]; then
    gitleaks git --no-banner --redact --exit-code 1 --report-format json --report-path "$OUT_DIR/gitleaks.json" "$ROOT" >/dev/null 2>&1
  else
    gitleaks dir --no-banner --redact --exit-code 1 --report-format json --report-path "$OUT_DIR/gitleaks.json" "$ROOT" >/dev/null 2>&1
  fi
  rc=$?
  case "$rc" in
    0) pass gitleaks ;;
    1) fail gitleaks "leaks found — see $OUT_DIR/gitleaks.json" ;;
    *) fail gitleaks "tool error rc=$rc" ;;
  esac
else
  skip gitleaks "brew install gitleaks  (or: https://github.com/gitleaks/gitleaks/releases)"
fi

# ---------------------------------------------------------------------------
head_ "syft (SBOM)"
sbom_ok=0
if have syft; then
  ran=$((ran + 1))
  syft scan "dir:$ROOT" --exclude './node_modules/**' --exclude './**/.venv/**' --exclude './**/.next/**' \
    -o spdx-json="$OUT_DIR/sbom.repo.spdx.json" -q >/dev/null 2>&1 && sbom_ok=1
  for img in $IMAGES; do
    name=$(printf '%s' "$img" | tr '/:' '__')
    syft scan "$img" -o spdx-json="$OUT_DIR/sbom.$name.spdx.json" -q >/dev/null 2>&1 || fail syft "could not scan image $img"
  done
  [ "$sbom_ok" = "1" ] && pass syft || fail syft "SBOM generation failed"
else
  skip syft "brew install syft  (or: curl -sSfL https://raw.githubusercontent.com/anchore/syft/main/install.sh | sh)"
fi

# ---------------------------------------------------------------------------
head_ "grype (vulnerabilities from SBOM / images)"
if have grype; then
  ran=$((ran + 1))
  targets=""
  [ "$sbom_ok" = "1" ] && targets="sbom:$OUT_DIR/sbom.repo.spdx.json"
  for img in $IMAGES; do targets="$targets $img"; done
  [ -z "$targets" ] && targets="dir:$ROOT"
  crit=0; high=0
  for t in $targets; do
    name=$(printf '%s' "$t" | tr '/:' '__')
    grype "$t" -o json --file "$OUT_DIR/grype.$name.json" -q >/dev/null 2>&1 || { fail grype "tool error on $t"; continue; }
    c=$(jcount "$OUT_DIR/grype.$name.json" "sum(1 for m in json.load(open(sys.argv[1]))['matches'] if m['vulnerability']['severity']=='Critical')")
    h=$(jcount "$OUT_DIR/grype.$name.json" "sum(1 for m in json.load(open(sys.argv[1]))['matches'] if m['vulnerability']['severity']=='High')")
    crit=$((crit + c)); high=$((high + h))
  done
  gate grype "$crit" "$high"
else
  skip grype "brew install grype  (or: curl -sSfL https://raw.githubusercontent.com/anchore/grype/main/install.sh | sh)"
fi

# ---------------------------------------------------------------------------
head_ "checkov (Bicep / IaC)"
if [ -d "$BICEP_DIR" ] && [ -n "$(find "$BICEP_DIR" -name '*.bicep' -print -quit 2>/dev/null)" ]; then
  if have checkov; then
    ran=$((ran + 1))
    checkov -d "$BICEP_DIR" --framework bicep --quiet --compact -o json --output-file-path "$OUT_DIR" >/dev/null 2>&1
    rc=$?
    # checkov writes results_json.json; count failed checks by severity when available.
    if [ -f "$OUT_DIR/results_json.json" ]; then
      failed=$(jcount "$OUT_DIR/results_json.json" "(lambda d: sum(len(x['results']['failed_checks']) for x in (d if isinstance(d,list) else [d])))(json.load(open(sys.argv[1])))")
      crit=$(jcount "$OUT_DIR/results_json.json" "(lambda d: sum(1 for x in (d if isinstance(d,list) else [d]) for c in x['results']['failed_checks'] if (c.get('severity') or '').upper()=='CRITICAL'))(json.load(open(sys.argv[1])))")
      high=$(jcount "$OUT_DIR/results_json.json" "(lambda d: sum(1 for x in (d if isinstance(d,list) else [d]) for c in x['results']['failed_checks'] if (c.get('severity') or '').upper()=='HIGH'))(json.load(open(sys.argv[1])))")
      [ "$failed" -gt 0 ] && say "INFO checkov: $failed failed check(s) total (severity requires a Prisma/Bridgecrew API key; ungraded checks are WARN)"
      gate checkov "$crit" "$high"
    elif [ "$rc" -ne 0 ]; then
      fail checkov "tool error rc=$rc"
    else
      pass checkov
    fi
  else
    skip checkov "uv tool install checkov"
  fi
else
  say "SKIP checkov: no .bicep files under infra/bicep yet (BUILD-22)"
fi

# ---------------------------------------------------------------------------
say ""
say "== summary: ran=$ran skipped=$skips failed=$failures (gate: fail on $FAIL_ON) reports: $OUT_DIR"
if [ "$failures" -gt 0 ]; then
  say "security: FAILED"
  exit 1
fi
say "security: OK"
exit 0

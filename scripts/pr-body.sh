#!/usr/bin/env bash
# Render the PR body for BUILD-NN from the plan file and the PR template.
#   scripts/pr-body.sh NN > body.md
set -euo pipefail
cd "$(dirname "$0")/.."
NN="$(printf '%02d' "$((10#${1:?NN}))")"
plan="docs/plan/increments/BUILD-${NN}.plan.md"
prompt="$(ls docs/build-prompts/BUILD-${NN}-*.md | head -n1)"
[ -f "$plan" ] || { echo "missing $plan" >&2; exit 1; }
# Multi-session increments append a new "## Evidence table"/"## Review verdicts" per review
# round, sometimes with a parenthetical suffix (e.g. "## Evidence table (final)") -- match by
# prefix and take the LAST occurrence (the final, consolidated one), not the first stale one.
section() {
  awk -v h="## $1" 'index($0,h)==1{c=1;buf="";next} /^## /{c=0} c{buf=buf $0 "\n"} END{printf "%s", buf}' "$plan"
}
title="$(grep -m1 -E '^\*\*Goal:\*\*' "$prompt" | sed -E 's/^\*\*Goal:\*\* *//')"
cat <<EOF
## BUILD-${NN}: ${title}

**Prompt:** \`${prompt}\`  ·  **Plan:** \`${plan}\`

### D-records / spec sections implemented
$(grep -oE 'D-2[0-9]{2}' "$prompt" | sort -u | tr '\n' ' ')

### Scope
$(section "Scope this session")

### Commands / endpoints
$(section "Commands/endpoints (API_CONTRACT rows) and their transition service + guards + audit action names")

### Migration
$(section "Migration? (yes/no; revision name; downgrade strategy)")

### Evidence table
$(section "Evidence table")

### Review verdicts
$(section "Review verdicts")

### Checklist
- [ ] \`make verify\` green locally; CI green
- [ ] no \`docs/spec/**\` changes in this PR
- [ ] no skip/xfail added; audit + outbox asserted for every state change
- [ ] Figma-gated screens: sign-off referenced or "data/skeleton only"
- [ ] AI-disabled path exercised where a critical workflow was touched
- [ ] Independent second-opinion review attached (required for 06/08/10/16/20)
EOF

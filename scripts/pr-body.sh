#!/usr/bin/env bash
# Render the PR body for BUILD-NN from the plan file and the PR template.
#   scripts/pr-body.sh NN > body.md
set -euo pipefail
cd "$(dirname "$0")/.."
NN="$(printf '%02d' "$((10#${1:?NN}))")"
plan="docs/plan/increments/BUILD-${NN}.plan.md"
prompt="$(ls docs/build-prompts/BUILD-${NN}-*.md | head -n1)"
[ -f "$plan" ] || { echo "missing $plan" >&2; exit 1; }
section() { awk -v h="## $1" 'BEGIN{p=0} $0==h{p=1;next} /^## /{if(p){exit}} p' "$plan"; }
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

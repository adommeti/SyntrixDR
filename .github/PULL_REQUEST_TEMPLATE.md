## BUILD-NN: <goal>

**Prompt:** `docs/build-prompts/BUILD-NN-<slug>.md` · **Plan:** `docs/plan/increments/BUILD-NN.plan.md`

### D-records / spec sections implemented
<!-- D-2nn list; STATE_MACHINES §…; API_CONTRACT rows -->

### Scope
<!-- from the plan -->

### Commands / endpoints → transition service → audit action
<!-- table -->

### Migration
<!-- revision id · reversible? · expand/contract stages · backfill volume — or "none" -->

### Evidence table
<!-- evidence table per docs/spec/VERIFY.md Part C -->

### Review verdicts
- Spec audit: PASS/FAIL (commit)
- Verification run: PASS/FAIL
- Code review: APPROVE/REQUEST_CHANGES
- Independent second-opinion review (06/08/10/16/20 or security/auth/schema/dependency changes): …

### Checklist
- [ ] `make verify` green locally; CI green
- [ ] no `docs/spec/**` changes in this PR
- [ ] no skip/xfail added; audit + outbox asserted for every state change
- [ ] Figma-gated screens: sign-off referenced in `docs/reviews/figma-signoff.md` or "data/skeleton only"
- [ ] AI-disabled path exercised where a critical workflow was touched
- [ ] PR title is `BUILD-NN: …` (squash-merge title is checked by `scripts/new-increment.sh`)

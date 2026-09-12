**Reconciled 2026-09-12 against FREEZE_ADDENDUM.md (D-201…D-261).**

# DR Command Center — World-Class V1 UI/UX Specification

## Experience mandate

[DECIDED] This must feel like a modern operational command product, not a traditional enterprise CRUD application. Inspiration is the fluidity and clarity of modern work-management products, but the design must be purpose-built for DR urgency, dependency awareness and auditability.

[DECIDED] Figma-first clickable prototype and formal UI/UX sign-off are release gates.

[DECIDED] **10-second comprehension rule:** within ten seconds of opening the live Command Center, a Coordinator should be able to answer: What is failing? What is at risk? Who is working it? What needs my action next?

## Design principles

1. Status before detail.
2. Exception-first during live execution.
3. Progressive disclosure: hover summary → Show More deep card.
4. Same object looks/behaves consistently everywhere.
5. Never rely on color alone; use icon/text/state plus color.
6. Dense enough for command operations, not visually noisy.
7. Real-time changes should be visible but not create distracting motion.
8. Dangerous/high-impact actions are deliberate and explain consequences.
9. AI is contextual, explainable and clearly separated from authoritative state.
10. Keyboard/accessibility behavior is first-class.

## Accessibility and themes

- WCAG 2.1 AA.
- Light and Dark mode in V1; persisted preference.
- Treemap/health colors need accessible contrast and text/icon reinforcement.
- Latest Chrome/Edge certified.
- Desktop primary; core pages responsive for tablet/phone.
- Focus management, keyboard navigation and screen-reader labels required.

## Information architecture

[DECIDED] This route map is authoritative, as amended by D-212. Master §15.3 `/dr/...` routes are superseded. There is no `/history` route; historical comparison is a tab on `/applications/[applicationId]`. (D-212)

```text
/
  /my-dr
  /plans                                   (D-212)
  /plans/[planId]                          (D-212)
  /dr-events
  /dr-events/[eventId]/command-center
  /dr-events/[eventId]/applications
  /dr-events/[eventId]/applications/[drApplicationId]
  /dr-events/[eventId]/work-streams
  /dr-events/[eventId]/work-streams/[workStreamId]
  /dr-events/[eventId]/tasks
  /dr-events/[eventId]/tasks/[taskId]
  /dr-events/[eventId]/dependencies
  /dr-events/[eventId]/blockers
  /dr-events/[eventId]/issues
  /dr-events/[eventId]/reviews             (Needs Review) (D-212)
  /dr-events/[eventId]/resources           (D-212)
  /dr-events/[eventId]/evidence
  /dr-events/[eventId]/timeline
  /dr-events/[eventId]/report
  /search
  /applications
  /applications/[applicationId]            (Application history is a tab here; no /history) (D-212)
  /people                                  (operational/resource; kept) (D-212)
  /admin
    /tiers
    /policies
    /users-teams                           (identity/team admin; kept) (D-212)
    /notifications                         (Email functional; Teams/Slack disabled "Coming Soon") (D-212)
    /ai
    /files
    /retention
```

## Coordinator Command Center

### At-a-glance upper viewport

1. Event identity, canonical state and elapsed time.
2. Network-cut/common recovery-start timestamp.
3. Weighted overall health dial.
4. Application treemap.
5. RTO at-risk / breached counts.
6. RPO failed/unknown counts.
7. Critical Blockers and Issues needing action.
8. Current phase and next manual gate.
9. Compact AI chat launcher/panel.

### Health dial

- normalized score 0–100. (D-223)
- Task Score = 100 × COMPLETED eligible Tasks / eligible Tasks (eligible = non-CANCELLED; no extra Blocked penalty). (D-223)
- Validation Score = 100 if the Application's required technical validation for the current recovery phase passed (failback validation when failback is active), else 0. (D-223)
- Application Health = 0.70 × Task Score + 0.30 × Validation Score. (D-223)
- Event Health = Σ(App Health × effective tier weight) / Σ(effective tier weight); weights T0–T4 = 100/75/50/30/20. (D-223)
- no Tier 0 hard cap. (D-223)
- Dial bands: GREEN ≥ 85; YELLOW 60–84.99; RED < 60. (D-223)
- Overlays never change the numeric dial. (D-223)
- Zero-Application Event: dial displays "—", not 100. (D-223)
- click/hover explains exact weighted contributors.
- AI query “Why is the dial 75?” produces the same calculation explanation, not a separate invented interpretation.

### Application treemap

- one rectangle per in-scope Application; true squarified treemap (D3), not a grid. (D-243)
- size represents Tier/criticality weighting.
- color bands by Application Health: GREEN ≥ 85; LIGHT_ORANGE 60–84.99; DARK_ORANGE 40–59.99; RED < 40. (D-223)
- overlays only worsen the visual band: RTO breach → RED; RPO breach → RED; SLA/RTO warning reached → ≥ DARK_ORANGE; active Blocker on T0/T1 App → ≥ DARK_ORANGE; active Blocker on T2–T4 App → ≥ LIGHT_ORANGE. (D-223)
- critical T0/T1 remains visually prominent through size + color intensity.
- hover: App, Tier, RTO target, elapsed, forecast, RPO, current state, owner, Blocker summary.
- click: Application deep view.

### Resource sidebar

Show avatar/mascot, Team, skills, active/ready/blocked counts, high-tier assignments, next required action and workload indicators. Coordinator can open deep card and execute authorized reassignment.

## My DR

Top tiles:

- Ready — derived NOT_STARTED Tasks whose HARD dependencies/gates are satisfied.
- In Progress.
- Blocked.
- SLA/RTO Risk.
- Needs My Confirmation.
- Completed.

Below tiles:

- My active Tasks.
- Blocked/waiting items.
- Items needing my validation/confirmation.
- Applications/Work Streams I own/lead.
- @mentions.
- Recent activity.

## Task board

- Kanban/swimlane based on canonical Task states: `NOT_STARTED, IN_PROGRESS, BLOCKED, READY_FOR_VALIDATION, COMPLETED, CANCELLED`. (D-252)
- Derived Ready Tasks get a clear ready indicator within NOT_STARTED lane/filter.
- drag/drop only invokes legal transition commands.
- no drop into COMPLETED: every Task reaches `COMPLETED` only via `READY_FOR_VALIDATION → validate` by an Application/System Owner (any slot) or Work Stream Lead; Executors can never self-complete; validation is always required (`needs_specific_validation=true` only adds task-specific criteria/evidence). (D-209)
- when a Blocker is verified/closed the Task card returns to `IN_PROGRESS`; it moves to `READY_FOR_VALIDATION` only through an explicit submit-validation action. (D-252)
- invalid drop snaps back and explains dependency/permission/validation guard.
- labels, assignee, Team, Tier, Application, Work Stream, risk and validation filters.
- private Saved Views and Coordinator-published shared live-linked views.

## Task quick card

Hover/popover shows:

- title + short description
- state / derived Ready
- Owning Team + Current Assignee
- expected vs elapsed duration
- Application/Work Stream
- dependencies/gates summary
- active Blocker reason/owner/since
- evidence/validation status
- latest comment

Show More opens full Task detail including audit history.

## Application deep view

Tabs/sections:

1. Overview/health.
2. Failover.
3. Failback.
4. RTO/RPO.
5. Dependencies.
6. Tasks/Milestones.
7. Blockers/Issues.
8. Evidence.
9. Validation/business-confirmation note.
10. Timeline/audit.
11. Historical comparisons (the Application history tab on `/applications/[applicationId]`; there is no `/history` route). (D-212)
12. AI context (“Ask about this Application”).

## Work Stream view

- progress/health and next gate.
- Stream Lead.
- Team/resource workload.
- Tasks/Milestones.
- Applications depending on stream milestones.
- Blockers and Issues.
- manual gate confirmation action.

## Dependency map

- distinguish HARD solid vs ADVISORY dashed.
- highlight blocked path and downstream impacted Apps.
- selected node shows why it cannot proceed.
- cycle-creating edit rejected with clear explanation.
- do not render all enterprise relations by default; focus on Event scope.

## Blockers vs Issues

Blocker UI emphasizes urgency, routing/team responsible, age, escalation and affected downstream objects.

Blocker actions expose exactly four commands: `assign → ASSIGNED`, `start → IN_PROGRESS`, `resolve → RESOLVED`, `verify → VERIFIED` then `CLOSED` atomically (both transitions audited). Owning Team accountability never changes. (D-211)

Issue/Finding UI emphasizes observation/risk/deviation/lesson, review status and owner. An Issue does not display as execution-blocking unless explicitly linked as such.

## RTO/RPO UX

Every Application should expose:

- target RTO
- elapsed/actual RTO
- forecast validation time
- pass/risk/breach
- target RPO
- pre-DR reference timestamp
- recovered-data timestamp/data-loss calculation
- pass/fail/unknown

Breach alert requires acknowledgement + reason while unrelated work continues.

## Event timeline

Human-readable live sequence of meaningful events rather than raw database logs. Filters by phase, Application, Work Stream, user, state, override, Blocker/Issue, RTO/RPO and AI action.

## AI panel

- contextual to current page/Event/object.
- clear Read / Recommend / Act distinction.
- citations/source chips for document-derived statements.
- Act preview lists exact state change and affected objects.
- high-risk action triggers reauth policy.
- low-confidence suggestions show Needs Review.
- speech input button; transcription appears as editable text before send/submit.
- graceful “AI unavailable” state with normal product controls intact.

## Reports

- Draft banner until published.
- factual summary separate from editable narrative.
- recovery timeline, RTO/RPO, baseline drift, Blockers/Issues, validation, monitoring, exceptions, evidence links and audit summary.
- PDF, CSV, Excel, RACI and full-package exports.

## Admin

### Tiers / health
- T0–T4 SLA/RTO defaults and weights.
- health formula shown as frozen default and event-effective settings.

### Notifications
- Email: active configuration/test form via Admin-configured SMTP (secret references); Mailpit locally. (D-256)
- Teams: disabled “Coming Soon”. (D-256)
- Slack: disabled “Coming Soon”. (D-256)

### AI
- provider: Synthetic / Anthropic; future Foundry shown only if feature-enabled later. First provider Synthetic, initial chat model Kimi K3 if available; model IDs Admin-configurable, never hard-coded. (D-249)
- model, endpoint, secret reference, feature flags, AI control profile.
- AI control profile selector: CONSERVATIVE (default; confirm every Act with diff preview), BALANCED (explicit confirmation for cross-Team changes and Tier 0/1 work, one batch confirmation for own-scope low-risk), FAST_EXECUTION (own-scope low-risk allowlisted actions execute without per-action reconfirmation). Excluded commands (Event lifecycle, Task/App validation, report publish, package export/import, role/policy/admin/security configuration, local-user creation) stay excluded in every profile. (D-228)
- never display raw production secret after save.

### Retention/files
- evidence classification retention.
- legal hold controls.
- file allowlist default `[pdf,png,jpg,jpeg,txt,log,csv,xlsx,docx,zip,json]` and 100MB default limit (`file.max_mb=100`); all uploads still undergo MIME/content validation and malware scanning. (D-225)

## Design System / Figma Gate

[DECIDED] Figma is not complete. A **Design System / Figma Gate** milestone precedes visually final Command Center work. (D-231)

- BUILD-01…11 proceed without waiting for the gate. (D-231)
- The web shell proceeds: layout, auth shell, nav, theme, responsive grid, design-token plumbing, generic Deep Card framework. (D-231)
- BUILD-12 (Coordinator Command Center) and BUILD-13 (My DR, Kanban, deep cards) and visual-heavy parts of later UI increments require Figma sign-off. (D-231)
- Use the Figma integration if available, else manual token export. (D-231)
- Figma/UI-UX review is a formal V1 release gate; the 10-second comprehension rule is the core UX acceptance test. (D-261)

Gate deliverables (D-231):

1. Command Center
2. My DR
3. Application deep view
4. Task Board
5. Task deep view
6. People/resource views
7. Needs Review
8. Admin
9. Reports
10. Light + Dark
11. tablet/mobile states
12. hover/deep-card behaviour
13. loading/empty/error states

## Figma deliverables

1. Product IA/navigation map.
2. Design tokens/light+dark.
3. Desktop Coordinator Command Center.
4. 500-App treemap stress-state design.
5. My DR.
6. Task Kanban.
7. Task hover/deep card.
8. Application deep view.
9. Work Stream/gate view.
10. Dependency graph/blocker impact.
11. Issue/Needs Review flows.
12. Evidence upload/scan states.
13. AI Read/Recommend/Act confirmation.
14. Report Draft→Publish.
15. Admin email/AI/security settings.
16. Tablet/mobile responsive core flows.
17. Empty/loading/error/degraded/AI-off states.
18. Interactive clickable prototype used for 10-second comprehension test.

## UX sign-off criteria

- 10-second comprehension test passes with representative Coordinators.
- WCAG 2.1 AA design review passes.
- Critical workflows represented in both themes.
- No critical action exists only in hover.
- Color-independent status cues exist.
- 500-App treemap remains usable.
- AI unavailable/degraded states are designed.
- Concurrent-update conflict UX is designed.

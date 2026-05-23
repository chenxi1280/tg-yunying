# PRD Implementation Alignment Audit

## Goal

Align `docs/tg-ops-platform-prd.md` and related design docs with the real frontend, backend, permission, and manual-test behavior in `/Users/xida/PycharmProjects/tg-yunying`.

## Scope

- PRD and design promises from `docs/tg-ops-platform-prd.md`, `docs/tg-ops-platform.md`, and module design docs.
- Actual implementation in `frontend/src/app`, `backend/app`, migrations, permissions, and available tests.
- Manual browser validation for user-visible workflows after the static audit identifies candidates.
- No code fixes in this audit phase unless the user explicitly switches from audit to implementation.

## Supervision Model

- Main auditor: builds the canonical checklist, assigns evidence rules, consolidates final gap list.
- Sidecar auditors: independently inspect disjoint feature surfaces and challenge main-auditor conclusions.
- Manual tester: validates high-risk user-visible gaps through UI/API flows after static evidence is gathered.

## Evidence Rules

Each finding must include:

- PRD or design citation with file and line.
- Implementation evidence with file and line, or explicit "not found" search terms.
- Status: `implemented`, `partial`, `missing`, `suspected`.
- Severity: `P0`, `P1`, `P2`, `P3`.
- Suggested acceptance test or manual test path.

## Phases

- [x] Phase 0: Recognize ZIP upload as a symptom of PRD-implementation drift.
- [x] Phase 1: Create audit framework and launch read-only supervision agents.
- [ ] Phase 2: Extract canonical PRD feature inventory. In progress; main headings and first API inventory extracted.
- [ ] Phase 3: Map implementation evidence per feature. In progress; material center and selected API surfaces mapped.
- [x] Phase 4: Consolidate supervised findings and de-duplicate. First pass complete with F001-F016.
- [ ] Phase 5: Run targeted manual UI/API validation for high-risk gaps.
- [ ] Phase 6: Produce implementation backlog with fix order and acceptance criteria.

## Initial Risk Areas

- Material center ZIP package import.
- Material center import jobs, groups, references, refresh-cache, cache config, and detail APIs.
- System settings material cache channel config should accept admin-friendly links and normalize to runtime peer values.
- Source-filter override audit semantics.
- Message sending API naming/detail/precheck drift.
- Listener event/error/reset and operation metrics export/report drift.
- Material center grouping, reference relationships, and cache health detail.
- Route/menu/permission drift.
- Task center feature breadth versus wizard/detail implementation.
- Listener/rules/media relay behavior versus PRD.
- Account security and profile/avatar batch flows.
- Admin manual claims versus visible product behavior.

## Open Questions

- Whether to treat design-doc-only promises outside the PRD as required implementation scope.
- Whether all missing items should be fixed immediately or batched by severity after audit.

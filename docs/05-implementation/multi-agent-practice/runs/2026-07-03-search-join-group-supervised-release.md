# 2026-07-03 Search Join Group Supervised Implementation

## Scope

- intake_id: `intake-2026-07-02-search-join-group-prd-merge-001`
- lane: `search-join-group`
- level: L2
- status: local QA pass, release pending

## Local Evidence

- Subagent PRD coverage review found gaps in keyword validation, protocol sample gate, proxy egress guard, linked dispatch, permission rule and locked paths.
- Follow-up implementation added:
  - `bot_protocol_samples` model and migration table.
  - strict non-empty keyword material and 64-char lowercase hex hash validation.
  - planner protocol sample lookup from active scrubbed DB samples.
  - dispatcher proxy guard before real gateway execution.
  - linked dispatch creation after `membership_observed`.
  - `tasks.create.search_join_group` backend permission and operator template permission.
- Verification:
  - supervised focused tests: 27 passed.
  - full local backend no-postgres suite: 653 passed / 798 deselected.
  - backend compileall: passed.
  - migration py_compile: passed.
  - frontend build: passed.
  - git diff --check: passed.

## Release Gate

- GitHub Actions deploy: pending.
- Production health: pending.
- `/task-center` public reachability: pending.
- Production migration table check: pending.

## Unproven

- Real target bot protocol sample collection.
- Real proxy egress guard with Clash / airport nodes.
- Node capacity, failover and all-nodes-down admin notification.
- Authorization-slot environment binding, warmup and execution lock production behavior.
- Seven-day search join gray release.

## Notes

This run can prove code deployment and fail-closed boundaries after release. It must not be used to claim real search-join gray success until production protocol samples, proxy egress and target bot execution evidence exist.

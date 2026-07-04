# 2026-07-04 Search Join Pacing Release

## Scope

- intake_id: `intake-2026-07-02-search-join-group-prd-merge-001`
- lane: `search-join-group`
- level: L2
- status: release gate passed, production health ok, real gray still unproven

## Change Summary

- Added `search_join_group`-only pacing and account cap behavior:
  - per-account total action limit.
  - per-account daily action limit.
  - per-account cooldown days.
  - per-keyword per-account daily limit.
  - task daily action limit.
  - daily / hourly skip probability.
  - per-action skip probability.
  - hourly / daily jitter.
- Persisted pacing decisions so repeated planner ticks reuse the same daily, hourly and action-level random results.
- Kept realtime pacing random decisions rule-based; no LLM is called on the hot path.
- Preserved generic task pacing updates for non-search tasks while rejecting explicit search_join-only pacing fields.

## Supervision And Fixes

Read-only supervision found four release blockers before deployment:

- duplicate skipped actions could be created for the same persisted skipped decision.
- jitter could move an action outside the current hour bucket semantics.
- search_join config update could partially commit `type_config` before pacing validation failed.
- daily limit counting scanned all lifetime actions in Python instead of constraining the SQL date range.

All four blockers were fixed before release.

## Local Evidence

- `backend/.venv/bin/python -m pytest -q backend/tests/test_search_join_group_config.py backend/tests/test_search_join_group_executor.py backend/tests/test_search_join_group_dataflow.py backend/tests/test_merge_integrity.py` -> 36 passed.
- `backend/.venv/bin/python -m pytest -q -m no_postgres backend/tests/test_ai_gateway.py backend/tests/test_channel_membership_strategy.py backend/tests/test_frontend_permission_gating.py backend/tests/test_search_join_group_config.py backend/tests/test_search_join_group_dataflow.py backend/tests/test_search_join_group_executor.py backend/tests/test_workflow.py` -> 192 passed / 177 deselected.
- `backend/.venv/bin/python -m compileall backend/app backend/migrations` -> passed.
- `npm --prefix frontend run build` -> passed.
- `git diff --check` -> passed.

## Release Gate

- GitHub Actions deploy: passed.
- Run ID: `28694612968`.
- Release head: `52c97c93b47d52781f4d6e4b0b47f431a13e49fc`.
- GitHub Actions URL: `https://github.com/chenxi1280/tg-yunying/actions/runs/28694612968`.
- Jobs:
  - `checks`: passed.
  - `build-images`: passed.
  - `deploy`: passed.
- Deploy step: `Deploy via SSH release script` passed.

## Production Evidence

- Public API health after deploy:
  - `https://tgyunying.telema.cn/api/health` returned HTTP 200 and `{"status":"ok"}`.
- Public task center reachability after deploy:
  - `https://tgyunying.telema.cn/task-center` returned HTTP 200 text/html.
  - `Last-Modified: Sat, 04 Jul 2026 04:19:42 GMT`.

## Unproven

- Real Zhengzhou three-account search-join gray execution was not proven by this release run.
- Real target bot protocol sample collection, real proxy egress, airport node capacity/failover, authorization-slot environment stack, warmup and execution lock production loop remain separate production verification items.

## Notes

This run proves the pacing code and release gate are live and healthy. It must not be used to claim real target-group search/join success until a separate live gray task produces execution evidence.

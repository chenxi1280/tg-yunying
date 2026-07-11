# Production Page Bounded Loading Design

## Status

- User approval: approved for implementation on 2026-07-10.
- Classification: L2 / P1 / standard team.
- Release Gate: required.
- Database migration: none.
- Worker/runtime configuration change: none.

## Problem

The production task editor blocks on `GET /api/operation-targets`. A real authenticated request returned 3,810 rows and about 1.91 MB of decoded JSON in 17.288 seconds, while the shared frontend client aborts at 15 seconds. The same unbounded endpoint blocks the operation overview, target management, rules, archives, message sending, and group-to-task deep link.

`GET /api/tasks` is a separate unbounded list path. A production response for 67 tasks was about 207 KB, completed in about 3.43 seconds when successful, and was also observed returning nginx 502. The 502's direct infrastructure cause remains unproven, but the code path is confirmed to return wide full-list projections and to perform per-batch item queries for account-security system tasks.

## Goals

- Make every first-party operation-target consumer use a bounded query.
- Keep task creation and editing usable without loading every target before opening the modal.
- Replace the task center's full-list payload with a paged list projection plus global summary and quick-group facets.
- Preserve tenant isolation, existing permissions, stale-response protection, visible errors, write/detail endpoints, and the 15-second timeout.
- Keep the old unbounded read contracts temporarily available only for compatibility while all first-party consumers migrate.

## Operation Target Contract

Extend `GET /api/operation-targets` with:

- `page`: optional positive integer.
- `page_size`: optional integer, maximum 100.
- `q`: bounded text search across title, username, Telegram peer, authorization status, and exact numeric ID.
- `ids`: at most 100 target IDs for selected-value hydration.
- `linked_group_id`: exact group deep-link lookup.
- `capability`: one of `send`, `listen`, `archive`, or `task`.
- Existing `target_type` and `account_id` remain supported.

When pagination is requested, the response remains `list[OperationTargetOut]` and returns `X-Total-Count`, `X-Page`, and `X-Page-Size`. Stable ordering is `OperationTarget.id DESC`.

The service must form the filtered target page before relationship aggregation. Send/listener/total account counts are database conditional aggregates grouped by group ID. No list request may materialize all matching `TgGroupAccount` ORM rows and count them in Python.

`ids` and `target_ids` use repeated query parameters (`ids=1&ids=2`, `target_ids=1&target_ids=2`). `GET /api/operation-targets/runtime-summary` accepts the current page's `target_ids`. `ids`, `linked_group_id`, and `account_id` remain constrained by the current tenant and permission middleware.

## Task List Contract

Add `GET /api/tasks/page` with:

- `page`, `page_size` with a maximum of 100.
- Existing `type` and `status` filters.
- `q` for task ID, name, type/status label, target/channel summary, and visible error text.
- `group_key` for the target-group plus associated-channel facet.

The response is `TaskListPageOut`:

```text
items: TaskListItemOut[]
total: integer
page: integer
page_size: integer
summary: { total, running, failed }
groups: TaskListGroupOut[]
```

`TaskListItemOut` contains only list fields: identity, name/type/status/priority, next run, error, cached/runtime-summary counters, runtime stage, target summary, grouping labels/key, and timestamps. It does not contain full `account_config`, `pacing_config`, `failure_policy`, or `type_config`. Editing continues to use `GET /api/tasks/{task_id}`.

Ordinary tasks and account-security system task projections participate in one stable order (`priority ASC, created_at DESC, source_kind ASC, stable_id DESC`), filter, count, and page. Account-security batch list statistics use batch columns or one grouped query; they may not issue one item query per batch. Global summary and quick groups are computed for the active type/status/search scope before `group_key` and pagination are applied, so paging and quick-group selection do not change their counts. The top-level `total` is computed after `group_key`.

## Frontend Behavior

- Target management uses server paging and search. Refresh, polling, and write-follow-up refresh preserve the current query.
- Reusable remote target selection loads the first bounded page on open, searches remotely, and hydrates selected IDs that are outside the current page.
- Task create/edit modal opens before target options finish loading. Target loading failure is displayed in the modal and does not silently become an empty successful list.
- Overview loads only the current target page and requests matching runtime summaries.
- Rules and archives load target options only when their create/edit UI opens. Archives request `capability=archive`.
- Message sending scopes remote target queries to the selected account. An old account's response cannot overwrite the new account's options.
- AppShell resolves group deep links with `linked_group_id` rather than a full group-target list.
- Task center loads `/tasks/page`, uses server paging/search/group filters, renders stats from `summary`, and polls only the current query every 60 seconds.

Every request identity includes the state that determines its result: page, page size, search, filters, account, selected IDs, task/modal session, or group key. Existing request-sequence checks remain mandatory.

## Compatibility and Rollback

- Existing unparameterized `/api/operation-targets` and `/api/tasks` remain during this release for non-migrated compatibility, but no first-party page may call them for a full list.
- Response truncation is never silent: bounded first-party calls always send explicit page parameters.
- No cache fallback, stale-data success, mock success, or timeout increase is permitted.
- There is no data migration. Rollback is a coordinated frontend/backend code rollback.

## Acceptance

- Target paging, search, capability filters, ID hydration, linked-group lookup, account scope, tenant isolation, and stable ordering are covered by automated tests.
- With thousands of targets, query count stays constant with page size and a 50-row response is below 100 KB.
- Task paging includes ordinary and system tasks, eliminates batch-item N+1, keeps summary/group counts global, and returns less than 100 KB for 20 rows.
- All first-party target consumers have source-level regressions proving no unparameterized full-list call remains.
- Production authenticated checks show p95 below 2 seconds, p99 below 5 seconds, zero 408/499/502 during the agreed serial and concurrent sample, and task edit target selection usable within 2 seconds.
- CI/deploy success alone does not establish `production_fixed`; the final state requires production UI, API timing, and nginx/backend evidence.

# Search Click Daily Target Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `executing-plans` task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make normal `search_join_group` tasks pursue a configured number of confirmed target clicks each local day, so an eligible task can pursue 80 confirmed clicks per day until its configured deadline without completing after its first cumulative 80. Existing target visibility, account capacity, and risk limits remain hard prerequisites.

**Architecture:** Keep `search_rank_deboost` and existing `target_count` tasks on their current lifetime-cap contract. New normal-search tasks persist `daily_target_count` in `Task.type_config`; target-progress aggregation filters confirmed and held actions by the task-local day, resets automatically on the next day, and never marks a daily-target task completed merely because the current day is met. `max_actions_per_day` remains an explicit hard Action budget and must not be lower than the daily confirmed target.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2, pytest, React, TypeScript, Ant Design, Vite.

---

## File Map

- `docs/01-product/tg-ops-platform-prd.md`: change the normal search-click business contract from lifecycle target to daily confirmed target.
- `docs/03-feature-designs/search-click-boost-prd.md`: define the local-day calculation, target fact, rollover, cap, deadline, and migration semantics.
- `docs/00-index/project-dataflow-index.md` and `docs/00-index/project-structure-index.md`: index the new daily target request, progress helper, planner gate, and detail rendering.
- `backend/app/schemas/task_center.py`: add the normal-search `daily_target_count` request/update contract and reject a hard daily Action budget below it.
- `backend/app/services/task_center/service.py`: persist/modify `daily_target_count`, generate the daily task name, and clear only unstarted legacy plans during a mode migration.
- `backend/app/services/task_center/search_click_target_progress.py`: aggregate current-local-day confirmed/held facts for normal daily tasks while preserving the old lifetime behavior for legacy and rank tasks.
- `backend/app/services/task_center/hourly_stats.py`: convert remaining daily confirmed demand into the current-hour goal using the remaining local-day curve weights.
- `backend/app/services/task_center/executors/search_join_group.py`: cap normal search planning with the current day's remaining daily target, without setting `Task.status=completed` at a daily boundary.
- `frontend/src/app/views/TaskCenterWizardSections.tsx`, `taskCenterViewModel.ts`, `TaskCenterView.tsx`, and `TaskCenterDetailModal.tsx`: collect/display a daily confirmed target for normal search tasks and retain cumulative wording for rank tasks.
- `backend/tests/test_search_click_target_progress.py`, `test_search_join_group_config.py`, `test_search_join_group_executor.py`, and `test_frontend_permission_gating.py`: regression coverage.

## Task 1: Product and data-flow contract

**Files:**

- Modify: `docs/01-product/tg-ops-platform-prd.md`
- Modify: `docs/03-feature-designs/search-click-boost-prd.md`
- Modify: `docs/00-index/project-dataflow-index.md`
- Modify: `docs/00-index/project-structure-index.md`

- [x] **Step 1: Specify the daily target truth source.**

  Document that only normal `search_join_group` receives `daily_target_count`; one day counts an Action only when `status=success` and `result.join_status=membership_observed` (or `membership_observed=true`). Pending, claiming, executing, and unknown Actions for the same task-local day hold a slot; failed/skipped Actions release it.

- [x] **Step 2: Specify rollover and deadline behavior.**

  Define that the target resets at the task timezone's local midnight, meeting it sets `search_click_target.state=daily_target_met` but leaves the task `running`, and `scheduled_end` remains the only normal completion boundary. State that legacy `target_count` tasks and all `search_rank_deboost` tasks retain lifecycle completion semantics.

- [x] **Step 3: Specify the safety relation.**

  Require `max_actions_per_day >= daily_target_count`; it is an explicit Action hard budget, not a fake-success fallback. If failures/skips prevent the daily confirmed target, expose the shortfall rather than sending above the configured hard budget.

- [x] **Step 4: Verify documentation consistency.**

  Run:

  ```bash
    rg -n "daily_target_count|每日确认目标|daily_target_met|target_count_reached" \
    docs/01-product docs/03-feature-designs docs/00-index
  ```

  Expected: normal search-click docs identify daily target semantics; rank and legacy wording remains lifecycle-scoped.

## Task 2: Define daily progress with failing tests

**Files:**

- Modify: `backend/tests/test_search_click_target_progress.py`
- Modify: `backend/app/services/task_center/search_click_target_progress.py`

- [x] **Step 1: Write the failing daily-target rollover test.**

  Add a normal task with `type_config={"daily_target_count": 2}`, two current-local-day confirmed `search_join` Actions, and one prior-day confirmed Action. Assert:

  ```python
  progress = reconcile_search_click_target_progress(session, task)
  assert progress.confirmed_count == 2
  assert progress.state == "daily_target_met"
  assert task.status == "running"
  assert "completion_reason" not in task.stats
  ```

- [x] **Step 2: Write the failing held-slot test.**

  Add a current-day confirmed Action plus a current-day pending Action to a `daily_target_count=3` task. Assert `confirmed_count == 1`, `held_count == 1`, and `remaining_slot_count == 1`; add tomorrow's pending Action and assert it does not consume today's slot.

- [x] **Step 3: Preserve the legacy regression.**

  Keep the existing `target_count=1` test and assert it still sets `Task.status == "completed"` with `completion_reason="target_count_reached"`.

- [x] **Step 4: Verify RED.**

  Run:

  ```bash
  perl -e 'alarm 60; exec @ARGV' /Users/xida/PycharmProjects/tg-yunying/backend/.venv/bin/python -m pytest -q -p no:cacheprovider \
    backend/tests/test_search_click_target_progress.py
  ```

  Expected: the new daily tests fail because `daily_target_count` is not recognized and the current implementation only aggregates lifetime facts.

- [x] **Step 5: Implement the smallest dual-mode progress helper.**

  Add a mode-aware target resolver: `daily_target_count` uses local-day bounds around `coalesce(Action.executed_at, Action.scheduled_at)` and writes the target count, `local_date`, `confirmed_count`, `held_count`, `remaining_slot_count`, and `daily_target_met`; the existing `target_count` path remains unchanged.

- [x] **Step 6: Verify GREEN.**

  Re-run the Task 2 command. Expected: all progress tests pass.

## Task 3: Define the normal-search API and planner contract with failing tests

**Files:**

- Modify: `backend/tests/test_search_join_group_config.py`
- Modify: `backend/tests/test_search_join_group_executor.py`
- Modify: `backend/app/schemas/task_center.py`
- Modify: `backend/app/services/task_center/service.py`
- Modify: `backend/app/services/task_center/executors/search_join_group.py`

- [x] **Step 1: Write the failing simple-create contract test.**

  Create `SearchJoinGroupSimpleTaskCreate` with `daily_target_count=80`, `max_actions_per_day=80`, and a future deadline. Assert the persisted task has `type_config["daily_target_count"] == 80`, no `target_count`, and a name containing `每日 80 次`.

- [x] **Step 2: Write the failing invalid-budget test.**

  Construct the same request with `daily_target_count=80` and `max_actions_per_day=79`. Assert validation raises an error containing `每日执行上限不能低于每日确认目标`.

- [x] **Step 3: Write the failing planner cap test.**

  Give a running normal task `daily_target_count=3`, one confirmed current-day Action, one held current-day Action, and executable account environments. Assert `build_task_plan` creates at most one new Action and does not set the task completed.

- [x] **Step 4: Verify RED.**

  Run:

  ```bash
  perl -e 'alarm 60; exec @ARGV' /Users/xida/PycharmProjects/tg-yunying/backend/.venv/bin/python -m pytest -q -p no:cacheprovider \
    backend/tests/test_search_join_group_config.py \
    backend/tests/test_search_join_group_executor.py
  ```

  Expected: request validation rejects the new field or creation writes lifetime `target_count`; the planner test still follows the old cap.

- [x] **Step 5: Implement the normal-search-only daily request and migration path.**

  Add `daily_target_count` only to normal-search simple create/update schemas. Persist it in `type_config`, derive the descriptive daily name, enforce the hard-budget relation at service boundaries, and on a legacy-to-daily edit clear only `pending`/`claiming` unstarted Actions before requeueing. Do not change rank-task request or completion behavior.

- [x] **Step 6: Use daily progress in normal planning.**

  Reuse the mode-aware progress result at the beginning/end of `build_plan`; a met daily target returns zero new actions for that local day but does not clear `next_run_at` or change `Task.status`. Before applying the per-round cap, derive the current hour's `goal` from the remaining daily confirmations and the remaining `hourly_round_curve` weights, taking the higher of that result and `hourly_min_successful_joins`; this prevents a daily target of 80 from being silently capped by a legacy hourly goal of 1.

- [x] **Step 7: Verify GREEN.**

  Re-run the Task 3 command. Expected: simple creation, validation, and planner behavior pass.

## Task 4: Frontend wording and payload contract

**Files:**

- Modify: `backend/tests/test_frontend_permission_gating.py`
- Modify: `frontend/src/app/views/TaskCenterWizardSections.tsx`
- Modify: `frontend/src/app/views/taskCenterViewModel.ts`
- Modify: `frontend/src/app/views/TaskCenterView.tsx`
- Modify: `frontend/src/app/views/TaskCenterDetailModal.tsx`

- [x] **Step 1: Write a failing frontend-source contract test.**

  Assert normal simple search-click forms/payloads use `daily_target_count`, display `每天目标次数`, and render `今日已确认`; assert rank forms keep `target_count` and lifetime wording.

- [x] **Step 2: Verify RED.**

  Run:

  ```bash
  perl -e 'alarm 60; exec @ARGV' /Users/xida/PycharmProjects/tg-yunying/backend/.venv/bin/python -m pytest -q -p no:cacheprovider \
    backend/tests/test_frontend_permission_gating.py
  ```

  Expected: static assertions fail because both task types currently use `target_count` and generic cumulative progress text.

- [x] **Step 3: Implement the minimal conditional UI.**

  For normal search only, rename the input, submit/edit `daily_target_count`, preserve `max_actions_per_day` as the explicit hard cap, and show the task-local date with confirmed/held/remaining daily progress. Leave rank UI/API unchanged.

- [x] **Step 4: Verify GREEN and build.**

  Run:

  ```bash
  perl -e 'alarm 60; exec @ARGV' /Users/xida/PycharmProjects/tg-yunying/backend/.venv/bin/python -m pytest -q -p no:cacheprovider \
    backend/tests/test_frontend_permission_gating.py
  npm run build
  ```

  Expected: tests and TypeScript/Vite build pass.

## Task 5: Regression, release, and live migration

**Files:**

- Verify: focused backend/frontend test files above
- Verify: `.github/workflows/deploy-production.yml`
- Mutate after release: production task `fdb48029-4fda-4801-818d-0c509da37ea3` through its authenticated normal-search config endpoint

- [x] **Step 1: Run focused regression and diff checks.**

  ```bash
  perl -e 'alarm 60; exec @ARGV' /Users/xida/PycharmProjects/tg-yunying/backend/.venv/bin/python -m pytest -q -p no:cacheprovider \
    backend/tests/test_search_click_target_progress.py \
    backend/tests/test_search_join_group_config.py \
    backend/tests/test_search_join_group_executor.py \
    backend/tests/test_search_click_contract_regressions.py \
    backend/tests/test_frontend_permission_gating.py
  git diff --check
  ```

  Expected: all targeted tests pass with no whitespace errors.

- [ ] **Step 2: Promote through the required release path.**

  Commit the scoped change on `master`, fast-forward/promote it to `release`, push `release`, and wait for the `Deploy Production` workflow to finish successfully. Do not patch production application code over SSH.

- [x] **Step 3: Preflight the current account pool before migration.**

  Read production facts for the task's normal account group: active accounts, executable authorization/environment bindings, enabled proxy egress, and current pending Action count. Do not claim 80/day is feasible without this proof.

- [ ] **Step 4: Migrate the current task through the API (blocked by production facts).**

  Do not submit the migration while the target has stopped with `target_not_in_results` and the selected group has only 55 usable, fully prepared accounts under a per-account daily limit of 1. Those facts cap real actions below 80/day, so a PATCH would restart an ineligible stopped task without resolving its cause.

- [ ] **Step 5: Verify live facts (blocked pending a searchable target and sufficient approved capacity).**

  After the blockers are resolved, confirm task config has `daily_target_count=80` and no legacy `target_count`, stats show today’s daily target with no `target_count_reached`, workers are healthy, and later distinguish planned/pending actions from actual `ExecutionAttempt` and `membership_observed` facts.

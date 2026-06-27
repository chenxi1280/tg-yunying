# Task Account Coverage Display Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show today's account participation coverage for AI active group and channel browse / like / comment tasks.

**Architecture:** Compute coverage once in backend task payload stats, reusing the same daily action status semantics as the planner. Frontend renders the returned stats in the task list and detail modal, plus local cycle/message group coverage columns derived from existing action rows.

**Tech Stack:** FastAPI service layer, SQLAlchemy ORM, Pydantic response passthrough, React + Ant Design.

---

### Task 1: Backend Coverage Stats

**Files:**
- Modify: `backend/app/services/task_center/account_pool.py`
- Modify: `backend/app/services/task_center/details.py`
- Modify: `backend/app/services/task_center/service.py`
- Test: `backend/tests/test_task_account_pool.py`

- [ ] Add a failing test that creates six active accounts, one AI task, same-day `pending` / `success` actions for two unique accounts, an old action for a third account, and an unrelated action for a fourth account. Assert the returned coverage is `covered_count=2`, `eligible_count=6`, `coverage_percent=33`.
- [ ] Run the direct test function and confirm it fails because coverage helper is missing.
- [ ] Implement a helper that maps task type to action types, counts eligible active accounts from task `account_config` without `max_concurrent` truncation, and counts same-day unique action accounts for the mapped action types and statuses `pending/executing/success`.
- [ ] Attach `account_coverage` to task stats in list and detail payloads.
- [ ] Run the direct backend test and compile checks.

### Task 2: Frontend Coverage Display

**Files:**
- Modify: `frontend/src/app/types/taskCenter.ts`
- Modify: `frontend/src/app/views/taskCenterViewModel.ts`
- Modify: `frontend/src/app/views/TaskCenterView.tsx`
- Modify: `frontend/src/app/views/TaskCenterDetailModal.tsx`
- Test: `backend/tests/test_frontend_permission_gating.py`

- [ ] Add a failing static frontend test asserting the task center source contains `账号覆盖` and `今日账号参与覆盖`.
- [ ] Add a small formatter in `taskCenterViewModel.ts` that renders `账号覆盖 covered/eligible，percent%` or `账号覆盖 -` when unavailable.
- [ ] Render the formatter in the task list execution statistics.
- [ ] Render `今日账号参与覆盖` in detail descriptions.
- [ ] Add local coverage columns for AI cycles and channel message groups.
- [ ] Run frontend static test and `npm run build`.

### Task 3: Docs And Verification

**Files:**
- Modify: `docs/01-product/tg-ops-platform-prd.md`
- Modify: `docs/01-product/tg-ops-platform.md`

- [ ] Document that account coverage is a runtime visibility metric for AI active group and channel interaction tasks.
- [ ] Run backend compile checks.
- [ ] Run targeted direct tests.
- [ ] Run `git diff --check`.

# Search Join Group Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the PRD-backed `search_join_group` task type in task center with backend contracts, planning, dispatch visibility, frontend creation/detail support, validation, QA evidence, and release deployment.

**Architecture:** Add `search_join_group` as a first-class task center type. The backend stores PRD research fields in `Task.type_config`, plans auditable `search_join` actions, keeps ranking observations and linked-task dispatches in dedicated tables/models, and exposes details through existing task APIs. The first implementation is fail-closed: real Telegram execution requires gateway support and protocol samples; missing prerequisites produce explicit skipped/failed facts rather than silent success.

**Tech Stack:** FastAPI, SQLAlchemy ORM, Alembic migrations, Pydantic v2 schemas, pytest, React/TypeScript, Vite.

---

### Task 1: Backend schemas and create API

**Files:**
- Modify: `backend/app/schemas/task_center.py`
- Modify: `backend/app/services/task_center/config_fields.py`
- Modify: `backend/app/services/task_center/service.py`
- Modify: `backend/app/api/routers/task_center.py`
- Modify: `backend/app/models/enums.py`
- Test: `backend/tests/test_search_join_group_config.py`

- [x] **Step 1: Write failing tests**

Create tests that import `SearchJoinGroupTaskCreate`, call `create_search_join_group_task`, and assert:
- valid payload persists `type='search_join_group'` with `execution_mode='mtproto_userbot'`;
- keywords are normalized into hashed entries and raw keyword text is not stored in `type_config`;
- non-target safe navigation rejects totals greater than 3 and rejects decoy join;
- create-and-start route starts the task and validates precheck.

- [x] **Step 2: Run red tests**

Run: `cd backend && timeout 60 .venv/bin/pytest -q -m no_postgres tests/test_search_join_group_config.py`
Expected: fail because schema/service/router do not exist.

- [x] **Step 3: Implement schemas and service entrypoints**

Add `SearchJoinGroupConfig`, `SearchJoinGroupTaskCreate`, and `SearchJoinGroupTaskConfigUpdate`; register them in `TYPE_CONFIG_MODELS`, `TASK_CREATE_MODELS`, `TYPE_SETTINGS_FIELDS`; add service functions `create_search_join_group_task`, `create_and_start_search_join_group_task`, `update_search_join_group_config`; add router endpoints `/api/tasks/search-join-group` and `/api/tasks/search-join-group/create-and-start`.

- [x] **Step 4: Run green tests**

Run the same test command and require PASS.

### Task 2: Data models and migrations

**Files:**
- Modify: `backend/app/models/task_center.py`
- Modify: `backend/app/models/__init__.py`
- Create: `backend/migrations/versions/0075_search_join_group.py`
- Test: `backend/tests/test_search_join_group_dataflow.py`

- [x] **Step 1: Write failing tests**

Tests assert ORM tables/models exist for:
- `SearchJoinRankObservation`
- `SearchJoinLinkedTaskDispatch`
- optional support tables for protocol samples and proxy subscriptions if not already present.

- [x] **Step 2: Run red tests**

Run: `cd backend && timeout 60 .venv/bin/pytest -q -m no_postgres tests/test_search_join_group_dataflow.py`
Expected: fail because models/tables are missing.

- [x] **Step 3: Implement models and migration**

Add dedicated ORM classes and Alembic migration with indexes aligned to PRD query patterns.

- [x] **Step 4: Run green tests**

Run the same test command and require PASS.

### Task 3: Planner and action payload

**Files:**
- Modify: `backend/app/services/task_center/payloads.py`
- Create: `backend/app/services/task_center/executors/search_join_group.py`
- Modify: `backend/app/services/task_center/executors/__init__.py`
- Modify: `backend/app/services/task_center/stats.py`
- Test: `backend/tests/test_search_join_group_executor.py`

- [x] **Step 1: Write failing tests**

Tests assert planner creates only `action_type='search_join'` actions, respects hourly success deficit, caps non-target navigation at 3, writes payload metadata for target relevance/content health/Jisou ecosystem/paid ads, and never creates non-target join payloads.

- [x] **Step 2: Run red tests**

Run: `cd backend && timeout 60 .venv/bin/pytest -q -m no_postgres tests/test_search_join_group_executor.py`
Expected: fail because payload/executor are missing.

- [x] **Step 3: Implement planner**

Create `SearchJoinPayload`, `create_search_join_action`, executor planning functions, and search-join hourly stats. Planner must fail closed on missing protocol samples/proxy/client metadata by setting visible `task.last_error` / stats and creating no real action.

- [x] **Step 4: Run green tests**

Run the same test command and require PASS.

### Task 4: Dispatcher fail-closed execution and linked task records

**Files:**
- Modify: `backend/app/services/task_center/dispatcher.py`
- Create/Modify: `backend/app/services/task_center/search_join_linking.py`
- Test: `backend/tests/test_search_join_group_linked_tasks.py`

- [x] **Step 1: Write failing tests**

Tests assert dispatching `search_join` without gateway support does not fake success; it marks action `skipped`/`failed` with explicit `search_join_gateway_unavailable`, and linked task dispatch is created only after a successful membership-observed result with cooldown/revalidation metadata.

- [x] **Step 2: Run red tests**

Run: `cd backend && timeout 60 .venv/bin/pytest -q -m no_postgres tests/test_search_join_group_linked_tasks.py`
Expected: fail because dispatcher/linking are missing.

- [x] **Step 3: Implement fail-closed dispatcher and linking service**

Add `search_join` branch in dispatcher, using gateway method only if present; otherwise mark skipped with explicit unsupported status. Add deterministic linked task dispatch creation helpers without silently mutating AI tasks.

- [x] **Step 4: Run green tests**

Run the same test command and require PASS.

### Task 5: Frontend task center support

**Files:**
- Modify: `frontend/src/app/types/taskCenter.ts`
- Modify: `frontend/src/app/views/TaskCenterWizardSections.tsx`
- Modify: `frontend/src/app/views/TaskCenterView.tsx`
- Modify: `frontend/src/app/views/TaskCenterDetailModal.tsx`
- Modify: `frontend/src/app/views/taskCenterListGrouping.ts`
- Test: `backend/tests/test_frontend_permission_gating.py`

- [x] **Step 1: Write failing static contract tests**

Add tests that assert frontend contains `search_join_group`, uses `/tasks/search-join-group`, exposes research fields, safe navigation fields, linked task status, and ranking observation labels.

- [x] **Step 2: Run red tests**

Run: `cd backend && timeout 60 .venv/bin/pytest -q -m no_postgres tests/test_frontend_permission_gating.py -k search_join_group`
Expected: fail because UI strings/types/routes are missing.

- [x] **Step 3: Implement frontend support**

Add the task type to TypeScript types, creation wizard config, API submit path, task labels, list grouping, detail stats panels, and warnings.

- [x] **Step 4: Run frontend build**

Run: `cd frontend && npm run build`
Expected: PASS.

### Task 6: Index, QA, release, production deployment

**Files:**
- Modify: `docs/00-index/project-structure-index.md`
- Modify: `docs/05-implementation/multi-agent-practice/agent-status-board.md`
- Modify: `docs/05-implementation/multi-agent-practice/worklog/dev.md`
- Modify: `docs/05-implementation/multi-agent-practice/worklog/qa.md`

- [x] **Step 1: Run full targeted validation**

Run backend search-join tests, task-center affected tests, `python -m compileall app`, `git diff --check`, and frontend build.

- [ ] **Step 2: Subagent review**

Use subagents to compare implementation against PRD checklist and perform code quality review.

- [x] **Step 3: Update indexes and worklogs**

Record code entrypoints, validation evidence, QA pass/unproven items, and release gate status.

- [ ] **Step 4: Deploy**

Follow project release path `master -> release -> GitHub Actions Deploy Production`, inspect CI/deploy run, and verify live production endpoints/worker health before claiming production completion.

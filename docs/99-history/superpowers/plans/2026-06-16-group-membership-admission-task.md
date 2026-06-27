# Group Membership Admission Task Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the independent `group_membership_admission` task type that locks selected account-pool accounts at start time, drives each account through target group admission, and only marks accounts complete after a real test message succeeds.

**Architecture:** Reuse the existing task-center `Task`/`Action` execution pipeline and existing `ensure_target_membership` action. Add a focused admission item table and service that owns snapshot locking, account-level phases, membership-action linkage, test-message action creation, and aggregate task stats. Frontend support is added after backend API behavior is covered.

**Tech Stack:** FastAPI, SQLAlchemy, Alembic, Pydantic, pytest with in-memory SQLite for focused tests, React/TypeScript task-center UI.

---

### Task 1: Backend Type, Schema, Model, And Migration

**Files:**
- Modify: `backend/app/models/enums.py`
- Modify: `backend/app/models/task_center.py`
- Modify: `backend/app/models/__init__.py`
- Modify: `backend/app/schemas/task_center.py`
- Modify: `backend/app/services/task_center/config_fields.py`
- Create: `backend/migrations/versions/0060_group_membership_admission.py`
- Test: `backend/tests/test_group_membership_admission.py`

- [ ] **Step 1: Write the failing schema/model test**

Create `backend/tests/test_group_membership_admission.py` with tests proving:
- `GroupMembershipAdmissionTaskCreate` accepts target, account groups, schedule window, pacing, and AI test message settings.
- Missing account groups fails validation.
- The SQLAlchemy metadata creates `task_membership_admission_items`.

Run:
```bash
PYTHONPATH=backend pytest -q backend/tests/test_group_membership_admission.py::test_group_membership_admission_schema_requires_account_groups
```
Expected: fails because the schema does not exist.

- [ ] **Step 2: Add the task type schema and config registration**

Add:
- `TaskTypeValue` includes `"group_membership_admission"`.
- `GroupMembershipAdmissionPacingConfig`
- `GroupMembershipAdmissionTestMessageConfig`
- `GroupMembershipAdmissionConfig`
- `GroupMembershipAdmissionTaskCreate`
- `GroupMembershipAdmissionTaskConfigUpdate` if settings updates are needed later.
- `TYPE_CONFIG_MODELS["group_membership_admission"]`
- `TASK_CREATE_MODELS["group_membership_admission"]`
- `TYPE_SETTINGS_FIELDS["group_membership_admission"]`

The config must enforce:
- `target_operation_target_id > 0`
- `account_group_ids` has at least one id
- `schedule_end_at > schedule_start_at` when both are present
- `test_message.mode == "ai_random"`
- `test_message.min_chars <= test_message.max_chars`

- [ ] **Step 3: Add the item model and migration**

Add `TaskMembershipAdmissionItem` with:
- `id`
- `tenant_id`
- `task_id`
- `account_id`
- `target_id`
- `phase`
- `membership_action_id`
- `test_message_action_id`
- `test_message_text`
- `test_message_id`
- `delete_after_send`
- `delete_status`
- `failure_type`
- `failure_detail`
- `manual_required`
- `completed_at`
- `created_at`
- `updated_at`

Add a unique constraint on `(task_id, account_id)` and indexes for task/phase/manual status.

- [ ] **Step 4: Run focused tests and compile checks**

Run:
```bash
PYTHONPATH=backend pytest -q backend/tests/test_group_membership_admission.py
PYTHONPATH=backend python -m py_compile backend/app/models/enums.py backend/app/models/task_center.py backend/app/schemas/task_center.py backend/app/services/task_center/config_fields.py
git diff --check
```
Expected: tests pass and commands exit 0.

- [ ] **Step 5: Commit**

```bash
git add backend/tests/test_group_membership_admission.py backend/app/models/enums.py backend/app/models/task_center.py backend/app/models/__init__.py backend/app/schemas/task_center.py backend/app/services/task_center/config_fields.py backend/migrations/versions/0060_group_membership_admission.py
git commit -m "feat: add group membership admission data model"
```

### Task 2: Creation API And Snapshot Locking

**Files:**
- Modify: `backend/app/api/routers/task_center.py`
- Modify: `backend/app/services/task_center/service.py`
- Create: `backend/app/services/task_center/membership_admission.py`
- Test: `backend/tests/test_group_membership_admission.py`

- [ ] **Step 1: Write failing service tests**

Add tests proving:
- `create_group_membership_admission_task` stores a draft task with type `group_membership_admission`.
- `create_and_start_group_membership_admission_task` starts the task but does not include accounts outside selected pools.
- `lock_membership_admission_snapshot` creates one item per active account in the selected pools.
- Locking twice does not add newly added pool accounts.

Run:
```bash
PYTHONPATH=backend pytest -q backend/tests/test_group_membership_admission.py::test_locks_snapshot_once_from_selected_account_pools
```
Expected: fails because service helpers do not exist.

- [ ] **Step 2: Implement task creation endpoints and service functions**

Add:
- `POST /api/tasks/group-membership-admission`
- `POST /api/tasks/group-membership-admission/create-and-start`
- `create_group_membership_admission_task`
- `create_and_start_group_membership_admission_task`

The create functions should use existing `_create_task` and `_create_and_start_task`.

- [ ] **Step 3: Implement snapshot locking**

In `membership_admission.py`, implement:
- `lock_membership_admission_snapshot(session, task, now=None)`
- query `TgAccount.pool_id.in_(account_group_ids)`
- include only tenant-matching, non-deleted accounts
- create `TaskMembershipAdmissionItem` rows with `phase="pending"`
- update task stats with snapshot totals
- do nothing if any item already exists for that task

- [ ] **Step 4: Run focused tests**

Run:
```bash
PYTHONPATH=backend pytest -q backend/tests/test_group_membership_admission.py
PYTHONPATH=backend python -m py_compile backend/app/api/routers/task_center.py backend/app/services/task_center/service.py backend/app/services/task_center/membership_admission.py
git diff --check
```

- [ ] **Step 5: Commit**

```bash
git add backend/tests/test_group_membership_admission.py backend/app/api/routers/task_center.py backend/app/services/task_center/service.py backend/app/services/task_center/membership_admission.py
git commit -m "feat: create group membership admission tasks"
```

### Task 3: Membership Action Orchestration And Item Phase Sync

**Files:**
- Modify: `backend/app/services/task_center/membership_admission.py`
- Modify: `backend/app/services/task_center/service.py`
- Test: `backend/tests/test_group_membership_admission.py`

- [ ] **Step 1: Write failing orchestration tests**

Add tests proving:
- Pending items create `ensure_target_membership` actions with `require_send=True`.
- A successful membership action moves the item to `test_message_pending`.
- A membership action classified as group-admin/manual sets `phase="waiting_approval"` and `manual_required=True`.
- A failed unrecoverable membership action sets `phase="failed"` and copies failure fields.

Run:
```bash
PYTHONPATH=backend pytest -q backend/tests/test_group_membership_admission.py::test_membership_success_moves_item_to_test_message_pending
```
Expected: fails before orchestration exists.

- [ ] **Step 2: Implement membership action planning**

Implement:
- `plan_membership_admission_actions(session, task, now=None, limit=None)`
- find `phase="pending"` items
- create `create_membership_action` with payload containing target peer, target id, target display, target type group, and `require_send=True`
- store `membership_action_id`
- set `phase="joining"`

- [ ] **Step 3: Implement membership result sync**

Implement:
- `sync_membership_admission_items(session, task)`
- read linked membership actions
- use existing membership recovery classifier for failed/skipped actions
- success joined/already_joined -> `phase="test_message_pending"`
- manual/operator cases -> `phase="waiting_approval"`, `manual_required=True`
- unrecoverable failures -> `phase="failed"`
- refresh task stats.

- [ ] **Step 4: Run focused tests**

Run:
```bash
PYTHONPATH=backend pytest -q backend/tests/test_group_membership_admission.py
git diff --check
```

- [ ] **Step 5: Commit**

```bash
git add backend/tests/test_group_membership_admission.py backend/app/services/task_center/membership_admission.py backend/app/services/task_center/service.py
git commit -m "feat: orchestrate group admission membership actions"
```

### Task 4: Real Test Message Actions

**Files:**
- Modify: `backend/app/services/task_center/membership_admission.py`
- Modify: `backend/app/services/task_center/dispatcher.py` if delete-after-send needs dispatch result handling
- Test: `backend/tests/test_group_membership_admission.py`

- [ ] **Step 1: Write failing test-message tests**

Add tests proving:
- `phase="test_message_pending"` items create `send_message` actions.
- The generated payload contains `ai_generation_status="pending"` and marks the action as admission test.
- A successful test message action sets `phase="completed"`, stores text/message id, and writes `completed_at`.
- A failed test message action sets `phase="failed"` with `failure_type="test_message_failed"`.

Run:
```bash
PYTHONPATH=backend pytest -q backend/tests/test_group_membership_admission.py::test_test_message_success_completes_item
```
Expected: fails before test message orchestration exists.

- [ ] **Step 2: Implement test message planning**

Implement:
- `plan_membership_admission_test_messages(session, task, now=None, limit=None)`
- create `send_message` action for each `test_message_pending` item
- payload uses target group id, operation target id, `ai_generation_status="pending"`, and a task marker like `profile_scene="group_membership_admission_test"`
- store `test_message_action_id`
- set `phase="testing_message"`

- [ ] **Step 3: Implement test message result sync**

Extend `sync_membership_admission_items`:
- success -> `phase="completed"`, store action payload text, `remote_message_id`, `completed_at`
- failed -> `phase="failed"`, copy action failure
- deletion is recorded as `delete_status="not_requested"` for the first delivery unless delete action support is implemented in the same iteration.

- [ ] **Step 4: Run focused tests**

Run:
```bash
PYTHONPATH=backend pytest -q backend/tests/test_group_membership_admission.py
git diff --check
```

- [ ] **Step 5: Commit**

```bash
git add backend/tests/test_group_membership_admission.py backend/app/services/task_center/membership_admission.py backend/app/services/task_center/dispatcher.py
git commit -m "feat: verify group admission with test messages"
```

### Task 5: Detail API And Operator Actions

**Files:**
- Modify: `backend/app/api/routers/task_center.py`
- Modify: `backend/app/schemas/task_center.py`
- Modify: `backend/app/services/task_center/service.py`
- Modify: `backend/app/services/task_center/membership_admission.py`
- Test: `backend/tests/test_group_membership_admission.py`

- [ ] **Step 1: Write failing API/service tests**

Add tests proving:
- detail payload includes `membership_admission_phase` and `membership_admission_items`.
- retrying one item clears failed state and returns it to `pending`.
- retrying failed items affects only failed items.
- marking manual handled clears `manual_required` and returns the item to a checkable phase.

- [ ] **Step 2: Implement detail projection**

Add item projection fields:
- account display info
- phase
- membership/test action ids
- test message text/id
- failure classification/detail
- manual flag
- completed time

- [ ] **Step 3: Implement operator endpoints**

Add endpoints:
- `POST /api/tasks/{task_id}/membership-admission/items/{item_id}/retry`
- `POST /api/tasks/{task_id}/membership-admission/retry-failed`
- `POST /api/tasks/{task_id}/membership-admission/items/{item_id}/manual-handled`
- `GET /api/tasks/{task_id}/membership-admission/items/export`

- [ ] **Step 4: Run tests**

Run:
```bash
PYTHONPATH=backend pytest -q backend/tests/test_group_membership_admission.py
git diff --check
```

- [ ] **Step 5: Commit**

```bash
git add backend/tests/test_group_membership_admission.py backend/app/api/routers/task_center.py backend/app/schemas/task_center.py backend/app/services/task_center/service.py backend/app/services/task_center/membership_admission.py
git commit -m "feat: expose group admission progress"
```

### Task 6: Frontend Task Creation And Detail View

**Files:**
- Modify: `frontend/src/app/types/taskCenter.ts`
- Modify: `frontend/src/app/views/taskCenterViewModel.ts`
- Modify: `frontend/src/app/views/TaskCenterView.tsx`
- Modify: `frontend/src/app/views/TaskCenterWizardSections.tsx`
- Create or modify: focused frontend tests if current repo has a matching test harness
- Test: `backend/tests/test_frontend_permission_gating.py`

- [ ] **Step 1: Write failing static/frontend tests**

Add static tests asserting:
- `TASK_TYPES` includes `group_membership_admission` with label “群聊准入任务”.
- create endpoint maps to `/tasks/group-membership-admission`.
- wizard fields for this task include target group, account groups, schedule, pacing, and test message settings.
- the AI activity hard-hourly fields are not required for this task.

- [ ] **Step 2: Add TypeScript types and view-model mapping**

Update `TaskCenterTaskType`, endpoint maps, initial values, submit fields, and labels.

- [ ] **Step 3: Add form UI**

Add fields:
- target operation target
- account group multi-select
- schedule start/end
- max concurrent
- per minute
- delete test message switch

- [ ] **Step 4: Add detail UI**

Show admission stats and account-level item table with retry/manual actions.

- [ ] **Step 5: Run frontend verification**

Run:
```bash
PYTHONPATH=backend pytest -q backend/tests/test_frontend_permission_gating.py
npm --prefix frontend run build
git diff --check
```

- [ ] **Step 6: Commit**

```bash
git add frontend/src/app/types/taskCenter.ts frontend/src/app/views/taskCenterViewModel.ts frontend/src/app/views/TaskCenterView.tsx frontend/src/app/views/TaskCenterWizardSections.tsx backend/tests/test_frontend_permission_gating.py
git commit -m "feat: add group admission task UI"
```

### Task 7: Final Verification And Release Prep

**Files:**
- All modified files

- [ ] **Step 1: Run backend focused tests**

```bash
PYTHONPATH=backend pytest -q backend/tests/test_group_membership_admission.py backend/tests/test_task_center_membership_items.py
```

- [ ] **Step 2: Run compile/static checks**

```bash
PYTHONPATH=backend python -m py_compile backend/app/models/enums.py backend/app/models/task_center.py backend/app/schemas/task_center.py backend/app/services/task_center/config_fields.py backend/app/services/task_center/service.py backend/app/services/task_center/membership_admission.py backend/app/api/routers/task_center.py
npm --prefix frontend run build
git diff --check
```

- [ ] **Step 3: Audit design coverage**

Re-read `docs/99-history/superpowers/specs/2026-06-16-group-membership-admission-task-design.md` and confirm every explicit requirement has current code or an explicit documented limitation.

- [ ] **Step 4: Commit final fixes if needed**

```bash
git status --short
git add <changed files>
git commit -m "fix: complete group admission verification"
```

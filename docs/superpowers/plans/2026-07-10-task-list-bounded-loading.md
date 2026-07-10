# Task List Bounded Loading Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the task center's wide full-list response with a stable paged list projection that preserves global statistics, quick groups, search, system-task projections, detail editing, and stale-response safety.

**Architecture:** Add a dedicated `/api/tasks/page` contract and a focused task-list page service. The service builds lightweight ordinary-task and account-security-batch index rows, filters and facets them, hydrates only the requested page, and reads batch counters without per-batch item queries. The frontend holds explicit server query state and keeps full task configuration behind the existing detail endpoint.

**Tech Stack:** FastAPI, SQLAlchemy 2, Pydantic 2, React 19, TypeScript, Ant Design, pytest.

---

### Task 1: Define the paged task-list contract with RED tests

**Files:**
- Modify: `backend/app/schemas/task_center.py`
- Modify: `backend/tests/test_operations_center_runtime.py`
- Modify: `backend/tests/test_account_security.py`
- Modify: `backend/tests/test_workflow.py`

- [ ] **Step 1: Write service-level RED tests**

Create ordinary tasks and account-security batches with overlapping timestamps. Exercise the wished-for API:

```python
page = list_task_page(
    session,
    tenant_id=1,
    page=2,
    page_size=20,
    task_type=None,
    status=None,
    q="",
    group_key=None,
)
assert page.total == 67
assert len(page.items) == 20
assert page.summary.total == 67
assert sum(group.task_count for group in page.groups) == 67
assert all(not hasattr(item, "type_config") for item in page.items)
```

Add tests for type/status/q/group filters, global summary unaffected by page, group counts unaffected by selected page, stable priority/created/id ordering, and tenant isolation.

- [ ] **Step 2: Add an N+1 RED test for system batches**

Capture SQL while listing 50 account-security batches. Assert the number of `tg_account_security_batch_items` statements stays at one grouped query or zero when persisted batch counters are sufficient. Add a second run with five batches and assert query count does not grow with batch count.

- [ ] **Step 3: Add route RED tests**

Test `GET /api/tasks/page?page=1&page_size=20`, response schema, 422 bounds, search/group filters, permissions, and that full configs are absent while `GET /api/tasks/{id}` still contains them.

- [ ] **Step 4: Verify RED**

Run:

```bash
TEST_DATABASE_URL='postgresql+psycopg://tg_yunying:tg_yunying@127.0.0.1:5432/tg_yunying_test?connect_timeout=3' backend/.venv/bin/python -c 'import subprocess,sys; result=subprocess.run(sys.argv[1:], timeout=60); raise SystemExit(result.returncode)' backend/.venv/bin/pytest -q backend/tests/test_operations_center_runtime.py backend/tests/test_account_security.py backend/tests/test_workflow.py -k 'task_list_page or task_page or profile_batch_list_query_count'
```

Expected failures: `TaskListPageOut` / `list_task_page` / `/api/tasks/page` are missing.

- [ ] **Step 5: Commit RED tests and schema shape**

```bash
git add backend/app/schemas/task_center.py backend/tests/test_operations_center_runtime.py backend/tests/test_account_security.py backend/tests/test_workflow.py
git commit -m "test: define bounded task list contract"
```

### Task 2: Build lightweight task and system-batch indexes

**Files:**
- Create: `backend/app/services/task_center/list_page.py`
- Modify: `backend/app/services/task_center/profile_batch_projection.py`
- Modify: `backend/app/services/task_center/details.py`
- Modify: `backend/app/services/task_center/__init__.py`
- Test: `backend/tests/test_operations_center_runtime.py`
- Test: `backend/tests/test_account_security.py`

- [ ] **Step 1: Add immutable index and result types**

Implement focused dataclasses in `list_page.py`:

```python
@dataclass(frozen=True)
class TaskListIndexRow:
    id: str
    source_kind: str
    name: str
    task_type: str
    status: str
    priority: int
    created_at: datetime
    updated_at: datetime
    target_summary: str
    target_group_label: str
    associated_channel_label: str
    group_key: str
    search_text: str


@dataclass(frozen=True)
class TaskListPageResult:
    items: list[dict[str, Any]]
    total: int
    page: int
    page_size: int
    summary: dict[str, int]
    groups: list[dict[str, Any]]
```

- [ ] **Step 2: Build ordinary-task index rows without list payloads**

Load active tasks for the tenant with stable ordering inputs. Reuse one batched target/channel context to compute visible target and search text, then compute backend group labels/key using the existing frontend semantics. Do not call `_task_payload()` for rows outside the requested page.

- [ ] **Step 3: Build system-batch index rows without item N+1**

Use `TgAccountSecurityBatch.total_count/success_count/skipped_count/failed_count/status` for list counters. If latest failure requires item data, select it for all candidate batch IDs in one grouped/window query. Do not call `_batch_items()` from the list path.

- [ ] **Step 4: Filter, facet, sort, and page identities**

Apply tenant/type/status/q to index rows, compute summary and quick groups for that scope, then apply `group_key`, stable sort `(priority ASC, created_at DESC, source_kind ASC, stable_id DESC)`, total, and page slice. A bounded lightweight index/facet scan is allowed; never build full `TaskOut` or full configuration for rows outside the slice. Only hydrate the sliced identities.

- [ ] **Step 5: Hydrate ordinary page items**

Fetch runtime summaries only for current-page task IDs and produce the compact payload:

```python
{
    "id": task.id,
    "tenant_id": task.tenant_id,
    "name": task.name,
    "type": task.type,
    "status": task.status,
    "priority": task.priority,
    "next_run_at": task.next_run_at,
    "last_error": task.last_error,
    "stats": _list_stats(task, summary),
    "runtime_stage": derive_task_runtime_stage(task, summary=summary),
    "target_summary": index.target_summary,
    "target_group_label": index.target_group_label,
    "associated_channel_label": index.associated_channel_label,
    "group_key": index.group_key,
    "created_at": task.created_at,
    "updated_at": task.updated_at,
}
```

Hydrate system-batch items from batch columns and the shared aggregate map.

- [ ] **Step 6: Run GREEN and commit**

Run:

```bash
TEST_DATABASE_URL='postgresql+psycopg://tg_yunying:tg_yunying@127.0.0.1:5432/tg_yunying_test?connect_timeout=3' backend/.venv/bin/python -c 'import subprocess,sys; result=subprocess.run(sys.argv[1:], timeout=60); raise SystemExit(result.returncode)' backend/.venv/bin/pytest -q backend/tests/test_operations_center_runtime.py backend/tests/test_account_security.py -k 'task_list_page or profile_batch_list_query_count'
```

Expected: pagination/facets pass and batch item SQL count stays constant.

```bash
git add backend/app/services/task_center/list_page.py backend/app/services/task_center/profile_batch_projection.py backend/app/services/task_center/details.py backend/app/services/task_center/__init__.py backend/tests/test_operations_center_runtime.py backend/tests/test_account_security.py
git commit -m "fix: build compact task list pages"
```

### Task 3: Add schemas and `/api/tasks/page`

**Files:**
- Modify: `backend/app/schemas/task_center.py`
- Modify: `backend/app/api/routers/task_center.py`
- Test: `backend/tests/test_workflow.py`

- [ ] **Step 1: Implement explicit list schemas**

Add:

```python
class TaskListItemOut(ApiModel):
    id: str
    tenant_id: int
    name: str
    type: str
    status: str
    priority: int
    next_run_at: datetime | None
    last_error: str
    stats: dict[str, Any] = Field(default_factory=dict)
    runtime_stage: dict[str, Any] = Field(default_factory=dict)
    target_summary: str = ""
    target_group_label: str = ""
    associated_channel_label: str = ""
    group_key: str = ""
    created_at: datetime
    updated_at: datetime


class TaskListSummaryOut(ApiModel):
    total: int = 0
    running: int = 0
    failed: int = 0


class TaskListGroupOut(ApiModel):
    key: str
    target_group_label: str
    associated_channel_label: str
    task_count: int
    running_count: int
    failed_count: int


class TaskListPageOut(ApiModel):
    items: list[TaskListItemOut]
    total: int
    page: int
    page_size: int
    summary: TaskListSummaryOut
    groups: list[TaskListGroupOut]
```

- [ ] **Step 2: Add the static route before `/{task_id}`**

```python
@router.get("/api/tasks/page", response_model=TaskListPageOut)
def get_task_page(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    type: str | None = None,
    status: str | None = None,
    q: str = Query(default="", max_length=160),
    group_key: str | None = Query(default=None, max_length=240),
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    return list_task_page(
        session,
        tenant_id=current_user.tenant_id or 1,
        page=page,
        page_size=page_size,
        task_type=type,
        status=status,
        q=q,
        group_key=group_key,
    )
```

Keep old `/api/tasks` and detail/write endpoints unchanged for compatibility.

- [ ] **Step 3: Run route GREEN and compatibility regression**

Run:

```bash
TEST_DATABASE_URL='postgresql+psycopg://tg_yunying:tg_yunying@127.0.0.1:5432/tg_yunying_test?connect_timeout=3' backend/.venv/bin/python -c 'import subprocess,sys; result=subprocess.run(sys.argv[1:], timeout=60); raise SystemExit(result.returncode)' backend/.venv/bin/pytest -q backend/tests/test_workflow.py -k 'task_page or task_lifecycle or task_permissions'
```

Confirm `/api/tasks/page` is not consumed by `/{task_id}` and old `/api/tasks` still returns its historical list.

- [ ] **Step 4: Commit**

```bash
git add backend/app/schemas/task_center.py backend/app/api/routers/task_center.py backend/tests/test_workflow.py
git commit -m "feat: expose compact task list pages"
```

### Task 4: Move Task Center to server query state

**Files:**
- Modify: `frontend/src/app/types/taskCenter.ts`
- Modify: `frontend/src/app/views/TaskCenterView.tsx`
- Modify: `frontend/src/app/views/taskCenterListGrouping.ts`
- Test: `backend/tests/test_task_center_view_dataflow.py`
- Test: `backend/tests/test_frontend_permission_gating.py`

- [ ] **Step 1: Write frontend RED tests**

Assert TaskCenter:

- calls ``api<TaskListPage>(`/tasks/page?${params.toString()}`)`` rather than `/tasks`;
- includes page/page_size/type/q/group_key in the request signature;
- renders stats from `response.summary` and group options from `response.groups`;
- uses server total for table pagination;
- 60-second polling and write refresh reload the current query;
- detail/edit still uses `/tasks/{id}` and full `TaskCenterTask`;
- stale responses cannot replace newer page/filter/search state.

- [ ] **Step 2: Verify RED**

Run both no-postgres test files with a 60-second hard timeout. Expected failures identify the current full `/tasks` call and client-side grouping.

- [ ] **Step 3: Add frontend page types**

Define `TaskCenterListItem`, `TaskCenterListSummary`, `TaskCenterListGroup`, and `TaskCenterListPage`. Keep `TaskCenterTask` as the full detail type. List-specific grouping helpers use server-provided labels/key and no longer read `type_config`.

- [ ] **Step 4: Implement explicit server state**

Add immutable list query state:

```typescript
type TaskListQuery = Readonly<{
  page: number;
  pageSize: number;
  type: TaskTypeFilter;
  q: string;
  groupKey: string;
}>;
```

Build the URL with `URLSearchParams`, fetch page and scheduling settings in parallel, bind the request sequence to a serialized query signature, and update items/summary/groups/page total only while active.

- [ ] **Step 5: Replace client controls**

Use `Input.Search` or a controlled search with explicit submission, server group options, and AntD table pagination `{ current, pageSize, total, onChange }`. Reset page to 1 when type, q, or group changes. Stats cards use global summary values, not current page length.

- [ ] **Step 6: Preserve action and deep-link behavior**

Lifecycle buttons can use compact list items because they require identity/status only. Opening details always fetches the full detail object. After create, save, lifecycle, admission, source-filter, or delete succeeds, refresh the current server query and preserve the existing “operation succeeded, refresh failed” distinction.

- [ ] **Step 7: Run GREEN, build, and commit**

```bash
backend/.venv/bin/python -c 'import subprocess,sys; result=subprocess.run(sys.argv[1:], timeout=60); raise SystemExit(result.returncode)' backend/.venv/bin/pytest -q -m no_postgres backend/tests/test_task_center_view_dataflow.py backend/tests/test_frontend_permission_gating.py -k 'task_center or task_list'
cd frontend && npm run build
git add frontend/src/app/types/taskCenter.ts frontend/src/app/views/TaskCenterView.tsx frontend/src/app/views/taskCenterListGrouping.ts backend/tests/test_task_center_view_dataflow.py backend/tests/test_frontend_permission_gating.py
git commit -m "fix: page task center list on the server"
```

### Task 5: Prove bounded response and stable semantics

**Files:**
- Modify: `backend/tests/test_operations_center_runtime.py`
- Modify: `backend/tests/test_account_security.py`
- Modify: `backend/tests/test_workflow.py`
- Modify: `backend/tests/test_task_center_view_dataflow.py`

- [ ] **Step 1: Add response-size and scale tests**

Seed at least 120 ordinary tasks and 50 system batches. Serialize a 20-row `TaskListPageOut` with Pydantic and assert encoded UTF-8 size is below 100,000 bytes. Assert page SQL count stays constant when batch count grows from 5 to 50.

- [ ] **Step 2: Add facet semantic tests**

Assert summary is calculated after type/q but before group/page, group counts reflect all matching rows, selecting a group updates total but not the available group facet counts, and page 1/page 2 have no duplicate IDs.

- [ ] **Step 3: Add legacy/detail regressions**

Assert old `/api/tasks` still works for compatibility and `/api/tasks/{id}` still returns all four config objects needed by edit.

- [ ] **Step 4: Run the expanded GREEN suite**

Run:

```bash
TEST_DATABASE_URL='postgresql+psycopg://tg_yunying:tg_yunying@127.0.0.1:5432/tg_yunying_test?connect_timeout=3' backend/.venv/bin/python -c 'import subprocess,sys; result=subprocess.run(sys.argv[1:], timeout=60); raise SystemExit(result.returncode)' backend/.venv/bin/pytest -q backend/tests/test_operations_center_runtime.py backend/tests/test_account_security.py backend/tests/test_workflow.py backend/tests/test_task_center_view_dataflow.py -k 'task_list or task_page or profile_batch'
```

Expected: all pass with no N+1 assertion or response-size failure.

- [ ] **Step 5: Commit**

```bash
git add backend/tests/test_operations_center_runtime.py backend/tests/test_account_security.py backend/tests/test_workflow.py backend/tests/test_task_center_view_dataflow.py
git commit -m "test: cover task list scale and facets"
```

### Task 6: Verify the task-list slice

**Files:**
- Modify only files required by failures found during this task

- [ ] **Step 1: Run focused backend tests**

Run:

```bash
TEST_DATABASE_URL='postgresql+psycopg://tg_yunying:tg_yunying@127.0.0.1:5432/tg_yunying_test?connect_timeout=3' backend/.venv/bin/python -c 'import subprocess,sys; result=subprocess.run(sys.argv[1:], timeout=60); raise SystemExit(result.returncode)' backend/.venv/bin/pytest -q backend/tests/test_operations_center_runtime.py backend/tests/test_account_security.py backend/tests/test_workflow.py -k 'task_list or task_page or runtime_summary or profile_batch'
```

- [ ] **Step 2: Run no-postgres UI contracts and build**

Run:

```bash
backend/.venv/bin/python -c 'import subprocess,sys; result=subprocess.run(sys.argv[1:], timeout=60); raise SystemExit(result.returncode)' backend/.venv/bin/pytest -q -m no_postgres backend/tests/test_task_center_view_dataflow.py backend/tests/test_frontend_permission_gating.py -k 'task_center or task_list'
cd frontend && npm run build
```

- [ ] **Step 3: Run quality checks**

```bash
git diff --check
rg -n "api<TaskCenterTask\[\]>\(`/tasks" frontend/src/app/views/TaskCenterView.tsx
```

Expected: no unbounded first-party task-list call remains; detail calls are still present.

- [ ] **Step 4: Record exact evidence**

Update the active run record and planning progress with test counts, build result, response-size result, and any production evidence still unproven.

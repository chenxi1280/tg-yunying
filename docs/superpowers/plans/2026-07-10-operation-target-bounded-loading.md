# Operation Target Bounded Loading Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace every first-party full operation-target read with a tenant-safe, searchable, paged query backed by SQL aggregates and selected-ID hydration.

**Architecture:** Add a focused operation-target list-query module that joins targets to groups and conditional link-count aggregates without materializing relationship rows. Keep the existing output schema and compatibility path, then migrate management tables and selectors to explicit bounded requests through a shared frontend loader.

**Tech Stack:** FastAPI, SQLAlchemy 2, Pydantic 2, React 19, TypeScript, Ant Design, pytest.

---

### Task 1: Add failing backend list-query tests

**Files:**
- Modify: `backend/tests/test_operations_center_runtime.py`
- Modify: `backend/tests/test_workflow.py`

- [ ] **Step 1: Write the failing service tests**

Add tests that create more targets than one page, attach multiple send/listener accounts, record SQL statements with `before_cursor_execute`, and exercise the wished-for service API:

```python
def _seed_operation_target_page_fixture(session: Session, target_count: int) -> None:
    session.add(Tenant(id=1, name="默认运营空间"))
    for target_id in range(1, target_count + 1):
        peer_id = f"-100{target_id}"
        session.add(
            OperationTarget(
                id=target_id,
                tenant_id=1,
                target_type="group",
                tg_peer_id=peer_id,
                title=f"目标 {target_id}",
                can_send=True,
                auth_status="已授权运营",
            )
        )
        session.add(
            TgGroup(
                id=target_id,
                tenant_id=1,
                tg_peer_id=peer_id,
                title=f"目标 {target_id}",
                can_send=True,
                auth_status="已授权运营",
            )
        )
        account_id = 100_000 + target_id
        session.add(TgAccount(id=account_id, tenant_id=1, display_name=f"账号 {target_id}", phone_masked=f"+86{account_id}", status="在线"))
        session.add(TgGroupAccount(tenant_id=1, group_id=target_id, account_id=account_id, can_send=True, is_listener=target_id % 2 == 0))
    session.commit()


def test_operation_target_page_is_stable_bounded_and_aggregated():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    statements: list[str] = []
    event.listen(engine, "before_cursor_execute", lambda _conn, _cursor, statement, _params, _context, _many: statements.append(statement))
    with Session(engine) as session:
        _seed_operation_target_page_fixture(session, target_count=125)
        rows, total = list_operation_targets_page(
            session,
            OperationTargetListQuery(tenant_id=1, page=2, page_size=50),
        )
    assert total == 125
    assert len(rows) == 50
    assert [row["id"] for row in rows] == sorted((row["id"] for row in rows), reverse=True)
    link_queries = [sql.lower() for sql in statements if "tg_group_accounts" in sql.lower()]
    assert len(link_queries) <= 2
    assert all(" count(" in sql or "count(" in sql for sql in link_queries)
    assert all("tg_group_accounts.id" not in sql for sql in link_queries)
```

Add separate tests for `q`, `ids`, `linked_group_id`, `target_type`, `account_id`, every capability value, and tenant isolation. Include equal-order edge cases and assert ID descending tie-breaking.

- [ ] **Step 2: Run the tests and verify RED**

Run with a 60-second Python subprocess timeout:

```bash
TEST_DATABASE_URL='postgresql+psycopg://tg_yunying:tg_yunying@127.0.0.1:5432/tg_yunying_test?connect_timeout=3' backend/.venv/bin/python -c 'import subprocess,sys; result=subprocess.run(sys.argv[1:], timeout=60); raise SystemExit(result.returncode)' backend/.venv/bin/pytest -q backend/tests/test_operations_center_runtime.py backend/tests/test_workflow.py -k 'operation_target_page or operation_target_filter'
```

Expected: collection succeeds and tests fail because `OperationTargetListQuery` / `list_operation_targets_page` and the new route parameters do not exist.

- [ ] **Step 3: Commit the red tests**

```bash
git add backend/tests/test_operations_center_runtime.py backend/tests/test_workflow.py
git commit -m "test: define bounded operation target reads"
```

### Task 2: Implement the aggregate page query

**Files:**
- Create: `backend/app/services/operation_target_list.py`
- Modify: `backend/app/services/operations.py`
- Modify: `backend/app/services/__init__.py` only if this package currently exports operation services there
- Test: `backend/tests/test_operations_center_runtime.py`

- [ ] **Step 1: Add immutable query and link-summary types**

Implement the public API in the new module:

```python
@dataclass(frozen=True)
class OperationTargetListQuery:
    tenant_id: int
    page: int | None = None
    page_size: int | None = None
    target_type: str | None = None
    account_id: int | None = None
    q: str = ""
    ids: tuple[int, ...] = ()
    linked_group_id: int | None = None
    capability: str | None = None


def list_operation_targets_page(
    session: Session,
    query: OperationTargetListQuery,
) -> tuple[list[dict[str, Any]], int]:
    base = _operation_target_rows(query)
    total = session.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = session.execute(_paged_target_rows(base, query)).all()
    return [_operation_target_row_payload(row) for row in rows], int(total)
```

Keep functions at or below project complexity limits by separating base filters, link aggregate, capability expressions, page application, and payload projection.

- [ ] **Step 2: Build conditional SQL aggregates**

Use one grouped subquery with these values:

```python
select(
    TgGroupAccount.tenant_id.label("tenant_id"),
    TgGroupAccount.group_id.label("group_id"),
    func.count(TgGroupAccount.id).label("all_count"),
    func.count(TgGroupAccount.id).filter(TgGroupAccount.can_send.is_(True)).label("send_count"),
    func.count(TgGroupAccount.id).filter(TgGroupAccount.is_listener.is_(True)).label("listener_count"),
).group_by(TgGroupAccount.tenant_id, TgGroupAccount.group_id)
```

Join it to `TgGroup` and `OperationTarget`. Build `can_send`, `can_listen`, `can_archive`, `can_task`, and capability labels from target/group columns plus aggregate counts. Never fetch `TgGroupAccount` entities for a list response.

- [ ] **Step 3: Implement filters and validation**

Normalize `q` once, cap it at 120 characters, cap `ids` at 100, validate `capability` against `send/listen/archive/task`, and apply all filters before count and pagination. `account_id` must verify the account belongs to the same tenant and is not deleted. Use parameterized SQLAlchemy expressions for `ILIKE` and numeric ID matching.

- [ ] **Step 4: Preserve the compatibility service**

Change `filter_operation_targets()` to call the new query without pagination:

```python
rows, _total = list_operation_targets_page(
    session,
    OperationTargetListQuery(
        tenant_id=tenant_id,
        target_type=target_type,
        account_id=account_id,
    ),
)
return rows
```

Delete only helper functions made unused by this change. Keep detail and write paths unchanged.

- [ ] **Step 5: Run GREEN and focused regressions**

Run the RED command, then:

```bash
TEST_DATABASE_URL='postgresql+psycopg://tg_yunying:tg_yunying@127.0.0.1:5432/tg_yunying_test?connect_timeout=3' backend/.venv/bin/python -c 'import subprocess,sys; result=subprocess.run(sys.argv[1:], timeout=60); raise SystemExit(result.returncode)' backend/.venv/bin/pytest -q backend/tests/test_operations_center_runtime.py backend/tests/test_workflow.py -k 'operation_target'
```

Expected: new tests and existing operation-target behavior pass within 60 seconds.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/operation_target_list.py backend/app/services/operations.py backend/tests/test_operations_center_runtime.py backend/tests/test_workflow.py
git commit -m "fix: bound operation target aggregation"
```

### Task 3: Expose bounded API and runtime-summary hydration

**Files:**
- Modify: `backend/app/api/routers/operations.py`
- Modify: `backend/app/api/routers/operations_center.py`
- Modify: `backend/app/services/runtime_summary.py`
- Modify: `backend/app/schemas/operations.py` only if query parsing needs a reusable validated type
- Test: `backend/tests/test_workflow.py`
- Test: `backend/tests/test_operations_center_runtime.py`

- [ ] **Step 1: Add route-level RED tests**

Test `page=2&page_size=20`, search, repeated `ids`, `linked_group_id`, capability, account scope, pagination headers, invalid capability, more than 100 IDs, and cross-tenant lookup. Test runtime summary with `target_ids=1&target_ids=2`.

- [ ] **Step 2: Verify RED**

Run:

```bash
TEST_DATABASE_URL='postgresql+psycopg://tg_yunying:tg_yunying@127.0.0.1:5432/tg_yunying_test?connect_timeout=3' backend/.venv/bin/python -c 'import subprocess,sys; result=subprocess.run(sys.argv[1:], timeout=60); raise SystemExit(result.returncode)' backend/.venv/bin/pytest -q backend/tests/test_workflow.py backend/tests/test_operations_center_runtime.py -k 'operation_target_page or operation_target_hydration or runtime_summary_target_ids'
```

Expected: failures show the route ignores page/filter parameters or runtime summary returns unrelated targets.

- [ ] **Step 3: Implement the route contract**

Use FastAPI `Query` validation and `Response` headers:

```python
@router.get("/api/operation-targets", response_model=list[OperationTargetOut])
def get_operation_targets(
    response: Response,
    target_type: str | None = None,
    account_id: int | None = None,
    page: int | None = Query(default=None, ge=1),
    page_size: int | None = Query(default=None, ge=1, le=100),
    q: str = Query(default="", max_length=120),
    ids: list[int] = Query(default=[]),
    linked_group_id: int | None = Query(default=None, ge=1),
    capability: str | None = Query(default=None, pattern="^(send|listen|archive|task)$"),
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    normalized_page = page or (1 if page_size is not None else None)
    normalized_page_size = page_size or (20 if normalized_page is not None else None)
    query = OperationTargetListQuery(
        tenant_id=current_user.tenant_id or 1,
        page=normalized_page,
        page_size=normalized_page_size,
        target_type=target_type,
        account_id=account_id,
        q=q,
        ids=tuple(ids),
        linked_group_id=linked_group_id,
        capability=capability,
    )
    rows, total = list_operation_targets_page(session, query)
    if normalized_page is not None and normalized_page_size is not None:
        _set_page_headers(response, total, normalized_page, normalized_page_size)
    return rows
```

If only one of `page/page_size` is supplied, normalize to page 1 and size 20 rather than silently returning unbounded data.

- [ ] **Step 4: Bound runtime summaries**

Add repeated `target_ids` query parsing and apply tenant-safe `TargetRuntimeSummary.target_id.in_(target_ids)` filtering before selecting rows. An empty filter preserves the old compatibility route.

- [ ] **Step 5: Run GREEN and commit**

Run the RED command plus permission middleware tests, then commit:

```bash
git add backend/app/api/routers/operations.py backend/app/api/routers/operations_center.py backend/app/services/runtime_summary.py backend/tests/test_workflow.py backend/tests/test_operations_center_runtime.py
git commit -m "feat: expose paged operation target reads"
```

### Task 4: Add reusable remote target loading

**Files:**
- Create: `frontend/src/app/components/OperationTargetSelect.tsx`
- Create: `frontend/src/app/hooks/useOperationTargetOptions.ts`
- Modify: `frontend/src/app/types/operations.ts`
- Test: `backend/tests/test_frontend_permission_gating.py`
- Test: `backend/tests/test_task_center_view_dataflow.py`

- [ ] **Step 1: Write source-contract RED tests**

Assert the shared loader always sends explicit `page/page_size`, uses `apiWithMeta`, merges selected IDs, binds request sequence to account/filter/search state, and does not change the 15-second client timeout. Assert TaskCenter no longer contains `api<OperationTarget[]>('/operation-targets')`.

- [ ] **Step 2: Verify RED**

```bash
backend/.venv/bin/python -c 'import subprocess,sys; result=subprocess.run(sys.argv[1:], timeout=60); raise SystemExit(result.returncode)' backend/.venv/bin/pytest -q -m no_postgres backend/tests/test_frontend_permission_gating.py backend/tests/test_task_center_view_dataflow.py -k 'remote_operation_target or target_support'
```

Expected: tests fail because the shared hook/component do not exist and TaskCenter still makes an unbounded request.

- [ ] **Step 3: Implement the hook**

Expose an immutable query shape and a request-sequenced loader:

```typescript
export type OperationTargetOptionQuery = Readonly<{
  q?: string;
  targetType?: 'group' | 'channel';
  accountId?: number;
  capability?: 'send' | 'listen' | 'archive' | 'task';
  ids?: readonly number[];
}>;

export function useOperationTargetOptions(query: OperationTargetOptionQuery) {
  // return { targets, loading, error, total, search, ensureIds, reload }
}
```

Each request builds `/operation-targets?page=1&page_size=50`; search and selected-ID hydration use separate request identities, and results merge by target ID without mutating prior arrays.

- [ ] **Step 4: Implement the select component**

`OperationTargetSelect` wraps Ant Design `Select`, supports single/multiple mode, remote search, loading/error state, selected-ID hydration, and `onTargetsLoaded` so parent forms can resolve selected names. It must not silently clear values when a search result page excludes them.

- [ ] **Step 5: Run GREEN, build, and commit**

```bash
backend/.venv/bin/python -c 'import subprocess,sys; result=subprocess.run(sys.argv[1:], timeout=60); raise SystemExit(result.returncode)' backend/.venv/bin/pytest -q -m no_postgres backend/tests/test_frontend_permission_gating.py backend/tests/test_task_center_view_dataflow.py -k 'remote_operation_target or target_support'
cd frontend && npm run build
git add frontend/src/app/components/OperationTargetSelect.tsx frontend/src/app/hooks/useOperationTargetOptions.ts frontend/src/app/types/operations.ts backend/tests/test_frontend_permission_gating.py backend/tests/test_task_center_view_dataflow.py
git commit -m "feat: add remote operation target selection"
```

### Task 5: Migrate all first-party consumers

**Files:**
- Modify: `frontend/src/app/views/OperationTargetsView.tsx`
- Modify: `frontend/src/app/views/OverviewView.tsx`
- Modify: `frontend/src/app/views/TaskCenterView.tsx`
- Modify: `frontend/src/app/views/TaskCenterWizardSections.tsx`
- Modify: `frontend/src/app/views/RulesCenterView.tsx`
- Modify: `frontend/src/app/views/RulesCenterConfig.tsx`
- Modify: `frontend/src/app/views/ArchivesView.tsx`
- Modify: `frontend/src/app/views/MessageSendingView.tsx`
- Modify: `frontend/src/app/AppShell.tsx`
- Test: `backend/tests/test_operation_targets_view_dataflow.py`
- Test: `backend/tests/test_task_center_view_dataflow.py`
- Test: `backend/tests/test_rules_center_view_dataflow.py`
- Test: `backend/tests/test_archives_view_dataflow.py`
- Test: `backend/tests/test_frontend_message_sending_dataflow.py`
- Test: `backend/tests/test_account_deep_link_dataflow.py`

- [ ] **Step 1: Write RED tests for every consumer**

Assert:

- target management sends page, size, and `q`, reads total headers, and refreshes the current query;
- overview sends the current target IDs to runtime summary;
- task modal uses the remote component and opens before options resolve;
- rules/archives load only when their edit/create UI opens;
- archives sends `capability=archive`;
- message sending sends `account_id` and rejects stale account responses;
- AppShell sends `linked_group_id`;
- `rg`-equivalent source assertions find no first-party unparameterized target-list call.

- [ ] **Step 2: Verify RED**

Run all listed no-postgres dataflow files. Expected failures identify each remaining unbounded consumer.

- [ ] **Step 3: Migrate target management and overview**

Replace target management's local search/pagination with explicit page state and `apiWithMeta`. Overview target workbench uses its own page state and fetches matching runtime summaries by `target_ids`. Plan target selection uses `OperationTargetSelect`.

- [ ] **Step 4: Migrate selectors and deep links**

Task, rules, archives, and message sending use `OperationTargetSelect` with the correct type/capability/account scope. AppShell performs a one-row linked-group lookup. Keep existing permission checks and error wording.

- [ ] **Step 5: Preserve stale-response and write-refresh semantics**

Every page's active request identity includes page, page size, q, account, capability, or modal session. Write actions refresh only the active query and continue to distinguish write success from refresh failure.

- [ ] **Step 6: Run GREEN, build, and commit**

```bash
backend/.venv/bin/python -c 'import subprocess,sys; result=subprocess.run(sys.argv[1:], timeout=60); raise SystemExit(result.returncode)' backend/.venv/bin/pytest -q -m no_postgres backend/tests/test_operation_targets_view_dataflow.py backend/tests/test_task_center_view_dataflow.py backend/tests/test_rules_center_view_dataflow.py backend/tests/test_archives_view_dataflow.py backend/tests/test_frontend_message_sending_dataflow.py backend/tests/test_account_deep_link_dataflow.py backend/tests/test_frontend_permission_gating.py
cd frontend && npm run build
git add frontend/src/app backend/tests/test_operation_targets_view_dataflow.py backend/tests/test_task_center_view_dataflow.py backend/tests/test_rules_center_view_dataflow.py backend/tests/test_archives_view_dataflow.py backend/tests/test_frontend_message_sending_dataflow.py backend/tests/test_account_deep_link_dataflow.py backend/tests/test_frontend_permission_gating.py
git commit -m "fix: migrate operation target consumers to bounded reads"
```

### Task 6: Verify the operation-target slice

**Files:**
- Modify only files required by failures found in this task

- [ ] **Step 1: Run focused backend verification**

Run the operation-target service/API tests with the PostgreSQL test URL and a 60-second hard timeout.

- [ ] **Step 2: Run frontend dataflow and build verification**

Run all consumer dataflow tests and `npm run build`.

- [ ] **Step 3: Run static quality gates**

```bash
git diff --check
rg -n "api<OperationTarget\[\]>\('/operation-targets(?:\?|')" frontend/src/app
```

Expected: diff check is clean; any remaining call contains explicit bounded parameters or is a documented compatibility-only backend response path.

- [ ] **Step 4: Record evidence and commit fixes**

Update the active run record and planning progress with exact test counts and any blocked production evidence, then commit only verified corrections.

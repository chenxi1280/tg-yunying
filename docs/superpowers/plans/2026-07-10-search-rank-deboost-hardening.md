# Search Rank Deboost Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按已批准 PRD 实现多个降权专用账号组、全入口用途隔离、分组运行代理、真实 Telegram Gateway、逐点击 reservation 和前端完整工作流。

**Architecture:** `AccountPool.pool_purpose` 是用途真相源，`TgAccount.account_identity` 是事务内同步投影；统一策略模块为所有账号动作提供硬边界。降权任务按账号所在分组复用 active `AccountGroupProxyBinding`，Gateway 使用绑定的 `runtime_proxy_id` 同端点完成出口探测和 Telethon 连接，Planner 在创建 Action 前原子占用单次点击 reservation。

**Tech Stack:** Python 3.12、FastAPI、SQLAlchemy 2、Alembic、Telethon、pytest、React 19、TypeScript、Ant Design、Vite。

---

## File Map

- Create `backend/app/services/account_usage_policy.py`: 唯一账号用途判定、查询过滤和动作授权边界。
- Create `backend/app/integrations/telegram/search_rank_deboost.py`: 纯 Telethon 搜索、解析、安全点击与逐点击 outcome。
- Create `backend/app/services/task_center/search_rank_deboost_reservations.py`: reservation 锁、配额、状态迁移和恢复。
- Create `backend/migrations/versions/0087_search_rank_deboost_hardening.py`: 模型字段、reservation 表、索引和存量数据回填。
- Modify `backend/app/models/tenants.py`, `backend/app/models/search_rank_deboost.py`, `backend/app/models/__init__.py`: 持久化模型。
- Modify `backend/app/schemas/accounts.py`, `backend/app/schemas/task_center.py`: API 请求与响应契约。
- Modify `backend/app/services/account_pools.py`, `backend/app/api/routers/account_pools.py`, `backend/app/api/routers/accounts.py`: 多分组生命周期和原子用途迁移。
- Modify `backend/app/services/proxy_group_binding_service.py`: 分组绑定复用、切换、显式解绑和运行代理闸门。
- Modify `backend/app/integrations/telegram/gateway.py`, `backend/app/integrations/telegram/contracts.py`: 生产 Gateway 方法与不可变代理凭证。
- Modify `backend/app/services/task_center/search_rank_deboost.py`, `service.py`, `executors/search_rank_deboost_planner.py`, `executors/search_rank_deboost_runtime.py`, `dispatcher.py`, `payloads.py`: 准备态、规划、执行和统计事实。
- Modify ordinary-task selectors and worker entrypoints discovered by contract tests: 普通任务、旧 MessageTask/Campaign、Listener、资料、面具、2FA 和设备清理统一用途守卫。
- Modify `frontend/src/app/types/accounts.ts`, `frontend/src/app/types/system.ts`, `frontend/src/app/types/taskCenter.ts`, account modal/context files, `AccountsView.tsx`, `TaskCenterView.tsx`, wizard/detail components and `taskCenterViewModel.ts`: 分组资产、账号选择和 readiness UI。
- Test with focused files under `backend/tests/`; production behavior tests must call real class methods and may not inject missing methods with `raising=False`。

### Task 1: Persistence and account usage policy

**Files:**
- Create: `backend/migrations/versions/0087_search_rank_deboost_hardening.py`
- Create: `backend/app/services/account_usage_policy.py`
- Modify: `backend/app/models/tenants.py`
- Modify: `backend/app/models/search_rank_deboost.py`
- Modify: `backend/app/models/__init__.py`
- Modify: `backend/app/schemas/accounts.py`
- Modify: `backend/app/schemas/task_center.py`
- Test: `backend/tests/test_search_rank_deboost_hardening_models.py`
- Test: `backend/tests/test_account_usage_policy.py`

- [ ] **Step 1: Write failing model and policy tests**

```python
def test_pool_purpose_is_truth_and_identity_is_projection(session, tenant):
    pool = AccountPool(tenant_id=tenant.id, name="rank-a", pool_purpose="rank_deboost")
    account = TgAccount(tenant_id=tenant.id, pool=pool, account_identity="normal", display_name="a", phone_masked="1")
    session.add_all([pool, account])
    session.flush()
    assert account_usage(account, pool) == "mismatch"
    sync_account_usage(session, account, pool, "tester")
    assert account.account_identity == "rank_deboost"

def test_reservation_action_id_is_unique(session, rank_task, account, rank_pool):
    session.add(SearchRankDeboostClickReservation(action_id="same", tenant_id=1, task_id=rank_task.id,
        account_id=account.id, account_pool_id=rank_pool.id, keyword_hash="h", local_date=date.today(),
        hour_bucket=now(), expires_at=now() + timedelta(minutes=10)))
    session.flush()
    session.add(SearchRankDeboostClickReservation(action_id="same", tenant_id=1, task_id=rank_task.id,
        account_id=account.id, account_pool_id=rank_pool.id, keyword_hash="h2", local_date=date.today(),
        hour_bucket=now(), expires_at=now() + timedelta(minutes=10)))
    with pytest.raises(IntegrityError):
        session.flush()
```

- [ ] **Step 2: Verify RED**

Run: `cd backend && /Users/xida/PycharmProjects/tg-yunying/backend/.venv/bin/python -m pytest -q tests/test_search_rank_deboost_hardening_models.py tests/test_account_usage_policy.py`

Expected: collection/import failure because the policy module, fields and reservation model do not exist.

- [ ] **Step 3: Add migration and model fields**

Implement exact fields from the approved design:

```python
class SearchRankDeboostClickReservation(Base):
    __tablename__ = "search_rank_deboost_click_reservations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    task_id: Mapped[str] = mapped_column(String(36))
    action_id: Mapped[str] = mapped_column(String(36), unique=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id"))
    account_pool_id: Mapped[int] = mapped_column(ForeignKey("account_pools.id"))
    keyword_hash: Mapped[str] = mapped_column(String(64))
    local_date: Mapped[date] = mapped_column(Date)
    hour_bucket: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reserved_count: Mapped[int] = mapped_column(Integer, default=1)
    consumed_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(24), default="reserved")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
```

Add pool enablement fields and binding `runtime_proxy_id`, `last_probe_at`, `last_probe_error`. Migration `down_revision` must match the repository head and migration must backfill account identity from pool purpose, pause historical running rank tasks, and mark bindings without runtime proxy as `needs_runtime_proxy` without fabricating a proxy.

- [ ] **Step 4: Implement the central policy**

```python
def account_usage(account: TgAccount, pool: AccountPool | None) -> str:
    if pool is None or pool.tenant_id != account.tenant_id:
        return "mismatch"
    purpose = normalize_pool_purpose(pool.pool_purpose, pool.system_key)
    return purpose if account.account_identity == purpose else "mismatch"

def assert_account_action_allowed(account: TgAccount, pool: AccountPool | None, action_kind: str) -> None:
    usage = account_usage(account, pool)
    if action_kind in AUTHORIZATION_ASSET_ACTIONS:
        return
    if usage == "mismatch" or action_kind not in ALLOWED_ACTIONS[usage]:
        raise ValueError("account_purpose_mismatch" if usage == "mismatch" else "account_purpose_forbidden")
```

`apply_operational_account_filters` must exclude both dedicated identities and dedicated pool purpose/system key. `apply_rank_deboost_account_filters` must require enabled rank pool and matching identity. `sync_account_usage` locks account and pool, validates tenant/enabled state, updates both fields and returns an immutable change summary.

- [ ] **Step 5: Verify GREEN and migration shape**

Run focused tests plus `python -m alembic upgrade head --sql` against the project Alembic configuration. Expected: tests pass; generated SQL contains all fields, indexes and no destructive downgrade of historical statistics.

- [ ] **Step 6: Commit**

```bash
git add backend/app/models backend/app/schemas backend/app/services/account_usage_policy.py backend/migrations/versions/0087_search_rank_deboost_hardening.py backend/tests/test_search_rank_deboost_hardening_models.py backend/tests/test_account_usage_policy.py
git commit -m "feat: add rank deboost persistence and usage policy"
```

### Task 2: Multiple rank pools and hard account isolation

**Files:**
- Modify: `backend/app/services/account_pools.py`
- Modify: `backend/app/api/routers/account_pools.py`
- Modify: `backend/app/api/routers/accounts.py`
- Modify: ordinary account selectors under `backend/app/services/`
- Test: `backend/tests/test_rank_deboost_pool_lifecycle.py`
- Test: `backend/tests/test_account_usage_hard_boundaries.py`
- Test: `backend/tests/test_search_rank_deboost_account_isolation.py`

- [ ] **Step 1: Write failing lifecycle and boundary tests**

Cover creating two custom rank pools, one idempotent default pool, enable/disable, immutable purpose, forbidden deletion, normal-to-rank and rank-to-normal atomic movement, and disabled destination rejection. Parametrize the hard boundary over ordinary task all/group/manual, MessageTask/Campaign, listener, profile initialization, mask initialization, 2FA rotation and device cleanup.

```python
@pytest.mark.parametrize("selection_mode", ["all", "group", "manual"])
def test_ordinary_selection_excludes_dedicated_accounts(session, selection_mode, normal_account, code_account, rank_account):
    ids = select_ordinary_accounts(session, tenant_id=1, account_config=selection(selection_mode, [normal_account, code_account, rank_account]))
    assert ids == [normal_account.id]
```

- [ ] **Step 2: Verify RED**

Run the three focused files. Expected: custom pool lifecycle and boundary cases fail against current single-group/identity-only behavior.

- [ ] **Step 3: Implement lifecycle and atomic movement**

`POST /api/account-pools/rank-deboost` with body always creates a custom non-system group; empty body delegates to the default endpoint during compatibility. `PATCH` accepts `is_enabled` and disable metadata but rejects purpose/system changes. `move_account_pool` delegates to `sync_account_usage`, cancels incompatible pending actions in the same transaction, audits before/after, and no longer blocks valid normal-to-rank movement.

- [ ] **Step 4: Route all action boundaries through policy**

Replace identity-only filters and direct account dispatches with `apply_operational_account_filters`, `apply_rank_deboost_account_filters`, or `assert_account_action_allowed`. Login, re-login, authorization diagnostics, standby session repair, read-only health/device diagnostics and official code reading remain allowed for all purposes; external operational/security mutations obey the matrix.

- [ ] **Step 5: Verify GREEN and regressions**

Run focused tests and existing account/task permission tests. Expected: all selectors and direct service calls fail closed on mismatch and dedicated accounts cannot enter ordinary actions.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services backend/app/api/routers backend/tests/test_rank_deboost_pool_lifecycle.py backend/tests/test_account_usage_hard_boundaries.py backend/tests/test_search_rank_deboost_account_isolation.py
git commit -m "feat: enforce dedicated account usage boundaries"
```

### Task 3: Persistent group runtime proxy bindings

**Files:**
- Modify: `backend/app/services/proxy_group_binding_service.py`
- Modify: `backend/app/services/proxy_airport_accounts.py`
- Modify: `backend/app/models/proxy_airport.py`
- Modify: `backend/app/api/routers/account_pools.py`
- Modify: `backend/app/schemas/accounts.py`
- Modify: `backend/app/services/account_pools.py`
- Test: `backend/tests/test_search_rank_deboost_group_proxy_binding.py`
- Test: `backend/tests/test_rank_deboost_runtime_proxy_binding.py`

- [ ] **Step 1: Write failing binding tests**

Test idempotent reuse of the same active binding, explicit switch increments generation, task stop/delete leaves binding active, unbind is rejected while running/paused tasks reference the pool, raw VMess/VLESS/SS nodes without materialized `runtime_proxy_id` are rejected, and SOCKS/HTTP runtime proxies are accepted only when enabled and reachable by validation.

- [ ] **Step 2: Verify RED**

Run the two binding files. Expected: current service rejects reuse, lacks runtime proxy and allows task-owned lifecycle assumptions.

- [ ] **Step 3: Implement binding asset APIs**

`PUT /api/account-pools/{pool_id}/rank-deboost-proxy-binding` accepts airport node and resolves a tenant-owned executable `AccountProxy`. Same node/runtime pair returns the existing binding. Switching unbinds the old row and creates generation + 1, then skips stale pending actions and pauses referencing rank tasks. `DELETE` checks running/paused references and never runs as a side effect of task stop/delete.

分组出口观测必须使用独立的 `account_group_proxy_binding_id` 关联或显式 polymorphic scope；不得把分组绑定 ID 写入当前指向 `account_proxy_bindings.id` 的 `proxy_exit_ip_observations.proxy_binding_id`。节点独占检查必须由数据库锁/唯一约束串行化，不能只依赖先查后写。

- [ ] **Step 4: Add fail-closed executable proxy guard**

Allowed protocols are `socks5`, `socks4`, `http`, `https`. Validate protocol, host, port, status and tenant. Never reinterpret VMess/VLESS/SS airport fields as SOCKS. Binding snapshots expose runtime proxy, current exit, probe timestamps, generation and reference count without returning credentials.

- [ ] **Step 5: Verify GREEN and commit**

Run binding tests plus task stop/delete tests, then commit:

```bash
git add backend/app/services/proxy_group_binding_service.py backend/app/api/routers/account_pools.py backend/app/schemas/accounts.py backend/app/services/account_pools.py backend/tests/test_search_rank_deboost_group_proxy_binding.py backend/tests/test_rank_deboost_runtime_proxy_binding.py
git commit -m "feat: persist rank deboost runtime proxy bindings"
```

### Task 4: Real Telegram Gateway and same-proxy egress proof

**Files:**
- Create: `backend/app/integrations/telegram/search_rank_deboost.py`
- Modify: `backend/app/integrations/telegram/gateway.py`
- Modify: `backend/app/integrations/telegram/contracts.py`
- Modify: `backend/app/config.py`
- Test: `backend/tests/test_search_rank_deboost_gateway.py`
- Test: `backend/tests/test_search_rank_deboost_protocol_sample_gate.py`

- [ ] **Step 1: Write failing production-class tests**

Instantiate `TelethonTelegramGateway` and assert both `search_rank_deboost_candidates` and `execute_search_rank_deboost` exist without monkeypatching missing methods. With fake Telethon message objects, prove only one `navigate_only` callback is clicked, join/external/unknown buttons are never clicked, candidate parsing tracks pages and positions, `unknown_after_click` is returned after an ambiguous click exception, and no direct client is created when egress probe fails.

- [ ] **Step 2: Verify RED**

Run gateway tests. Expected: methods are absent and production class contract fails.

- [ ] **Step 3: Implement pure conversation executor**

Expose functions that accept an authorized client and immutable request, send `/start` and keyword, parse controlled pages, classify button effects from protocol samples, select one safe competitor and call `message.click(row, col)` once. The module returns the approved `execution_status` and `click_outcomes`; it performs no business-table writes and invokes no join/import/follow/vote API.

- [ ] **Step 4: Implement Gateway wrappers and egress proof**

Add `RANK_DEBOOST_EGRESS_PROBE_URL`. Resolve credentials from the group runtime proxy, probe that HTTPS URL through the exact proxy, compare observed exit, and include proxy fingerprint in the Telethon client cache key. Call the pure executor with session ciphertext, credentials and decrypted keyword. Probe or authorization failure returns explicit status and never falls back to account proxy or direct connection.

- [ ] **Step 5: Verify GREEN and commit**

Run gateway and existing Telethon/search-join regression tests, then commit:

```bash
git add backend/app/integrations/telegram backend/app/config.py backend/tests/test_search_rank_deboost_gateway.py backend/tests/test_search_rank_deboost_protocol_sample_gate.py
git commit -m "feat: execute rank deboost through real telegram gateway"
```

### Task 5: Reservation planner, factual runtime and atomic task readiness

**Files:**
- Create: `backend/app/services/task_center/search_rank_deboost_reservations.py`
- Modify: `backend/app/services/task_center/payloads.py`
- Modify: `backend/app/services/task_center/executors/search_rank_deboost_planner.py`
- Modify: `backend/app/services/task_center/executors/search_rank_deboost_runtime.py`
- Modify: `backend/app/services/task_center/dispatcher.py`
- Modify: `backend/app/services/task_center/search_rank_deboost.py`
- Modify: `backend/app/services/task_center/service.py`
- Modify: `backend/app/schemas/task_center.py`
- Test: `backend/tests/test_search_rank_deboost_reservations.py`
- Test: `backend/tests/test_search_rank_deboost_executor.py`
- Test: `backend/tests/test_search_rank_deboost_e2e.py`
- Test: `backend/tests/test_search_rank_deboost_task_atomicity.py`

- [ ] **Step 1: Write failing account-scope and reservation tests**

Test `all` selects every eligible account across all enabled rank pools, `group` selects one enabled pool, `manual` intersects explicit IDs, and none impose a hidden count-10 cap. Concurrent planners must not exceed account/day, account+keyword/day, pool/day or task/hour limits. One action maps to one reservation and `max_clicks=1`.

- [ ] **Step 2: Write failing runtime and atomicity tests**

Test `confirmed` alone creates one stat and consumes reservation; `observed_no_click` releases/skips without stat; pre-click failures release; `unknown_after_click` remains quota-consuming `unknown` and is never automatically retried. Verify create-and-start rollback leaves no task, exempt row, action, reservation or binding, and stop/delete never unbinds group assets.

- [ ] **Step 3: Verify RED**

Run the four files. Expected: current planner has single-pool/hard-cap behavior, current runtime writes inferred stats, and create-and-start leaves a committed draft on failure.

- [ ] **Step 4: Implement reservation service**

Acquire task row lock plus deterministic account/pool locks before counting active `reserved`, `consumed`, `unknown`. Insert reservation and Action in one transaction. Provide `consume`, `release`, `mark_unknown`, `release_expired` functions with guarded source states and audit records. SQLite tests use row-lock-equivalent serialization; PostgreSQL path uses advisory locks keyed by tenant and shared dimension.

- [ ] **Step 5: Rebuild planner and runtime contracts**

Planner resolves normalized `account_config`, reads each account pool binding and snapshots binding generation/runtime proxy ID. It creates exactly one action per account+keyword opportunity and reserves one click. Dispatcher performs final usage, binding generation and credential guards. Runtime accepts Gateway outcomes verbatim; it does not recompute or simulate clicks and only writes `SearchRankDeboostActionStat` from `status=confirmed` outcome fields.

- [ ] **Step 6: Make task readiness atomic**

Draft create validates enabled groups and executable bindings but may persist `pending_real_search`. `start_task` locks draft, performs real candidate search/readiness and commits running state once. `create_and_start` uses one transaction and rolls back every artifact on any readiness failure. Edits/pause/stop/delete release only this task's unstarted reservations/actions.

- [ ] **Step 7: Verify GREEN and commit**

Run the four focused files and all existing rank files, then commit:

```bash
git add backend/app/services/task_center backend/app/schemas/task_center.py backend/tests/test_search_rank_deboost_reservations.py backend/tests/test_search_rank_deboost_executor.py backend/tests/test_search_rank_deboost_e2e.py backend/tests/test_search_rank_deboost_task_atomicity.py
git commit -m "feat: make rank deboost planning and execution factual"
```

### Task 6: Frontend account groups, selectors and readiness

**Files:**
- Modify: `frontend/src/app/types/accounts.ts`
- Modify: `frontend/src/app/types/system.ts`
- Modify: `frontend/src/app/types/taskCenter.ts`
- Modify: `frontend/src/app/views/AccountModals.tsx`
- Modify: `frontend/src/app/AppModals.tsx`
- Modify: `frontend/src/app/context/accountActions.ts`
- Modify: `frontend/src/app/context/defaults.ts`
- Modify: `frontend/src/app/context/types.ts`
- Modify: `frontend/src/app/views/AccountsView.tsx`
- Modify: `frontend/src/app/views/AccountMasksView.tsx`
- Modify: `frontend/src/app/views/TaskCenterView.tsx`
- Modify: `frontend/src/app/views/TaskCenterWizardSections.tsx`
- Modify: `frontend/src/app/views/TaskCenterDetailModal.tsx`
- Modify: `frontend/src/app/views/taskCenterViewModel.ts`
- Test: `backend/tests/test_search_rank_deboost_frontend_contract.py`
- Test: `backend/tests/test_task_center_view_dataflow.py`
- Test: `backend/tests/test_frontend_permission_gating.py`

- [ ] **Step 1: Write failing source-contract tests**

Assert the account center exposes normal/rank group type, enable toggle and binding action; normal task all/group/manual options use eligible ordinary accounts; rank task exposes all/group/manual dedicated account modes and no longer submits task-level `proxy_airport_node_id`; readiness renders per-pool binding gaps and separate observed/clicked/no-click/unknown counts.

- [ ] **Step 2: Verify RED**

Run the three frontend contract files. Expected: current single rank group and hidden rank selector behavior fails.

- [ ] **Step 3: Implement typed view-model helpers**

Add pure helpers `isOperationalAccount`, `isEligibleRankAccount`, `rankPoolSummaries`, and `accountSelectionPreview`. Use backend summary counts as authoritative and keep frontend filtering aligned for ergonomics. Normalize submit payload to `account_config={selection_mode, account_group_id, account_ids, max_concurrent}`.

- [ ] **Step 4: Implement account center and task UI**

Add group purpose selection, rank badges, enable/disable control, proxy binding configuration and usage-change confirmation. Restore rank account selection segmented control, show all enabled rank pools and missing-binding blockers, disable ordinary dedicated choices, and render execution/readiness states without instructional feature prose.

- [ ] **Step 5: Verify GREEN, build and commit**

Run contract tests and `cd frontend && npm run build`. Expected: TypeScript and Vite build pass with no new warnings beyond the existing chunk-size warning.

```bash
git add frontend/src backend/tests/test_search_rank_deboost_frontend_contract.py backend/tests/test_task_center_view_dataflow.py backend/tests/test_frontend_permission_gating.py
git commit -m "feat: expose rank deboost group workflows"
```

### Task 7: Integrated QA, indexes and release evidence

**Files:**
- Modify: `docs/00-index/project-structure-index.md`
- Modify: `docs/00-index/project-dataflow-index.md`
- Modify: `.planning/search-rank-deboost-hardening/task_plan.md`
- Modify: `.planning/search-rank-deboost-hardening/progress.md`
- Test: all backend and frontend checks

- [ ] **Step 1: Run focused rank suite**

Run all `backend/tests/test_search_rank_deboost_*.py`, account usage, pool lifecycle, binding, permission and task dataflow tests under the 60-second backend timeout. Expected: all pass.

- [ ] **Step 2: Run full no-PostgreSQL backend suite**

Run: `cd backend && /usr/bin/perl -e 'alarm shift; exec @ARGV' 60 /Users/xida/PycharmProjects/tg-yunying/backend/.venv/bin/python -m pytest -q -m no_postgres`

Expected: pass with only documented pre-existing warnings.

- [ ] **Step 3: Run PostgreSQL-dependent migration/concurrency checks**

Use the repository's configured test PostgreSQL when available. If unavailable, mark this evidence `blocked`, not passed; still validate migration SQL and SQLite behavior separately.

- [ ] **Step 4: Run frontend build and diff checks**

Run `cd frontend && npm run build`, `git diff --check`, placeholder scan, and inspect no secret or generated `dist` file is staged.

- [ ] **Step 5: Synchronize indexes and evidence status**

Update structure/dataflow indexes with actual file names and route count. Record `qa_pass`, `postgres_concurrency_pass|blocked`, `production_unproven`; never write `production_fixed` without E4.

- [ ] **Step 6: Commit**

```bash
git add docs/00-index .planning/search-rank-deboost-hardening
git commit -m "docs: record rank deboost implementation evidence"
```

## Plan Self-Review

- Spec coverage: account truth source, multiple groups, hard isolation, persistent runtime proxy, same-proxy probe, real Gateway, one click/action, reservation, atomic readiness, frontend and release evidence each map to a task.
- Placeholder scan: the plan contains no deferred implementation marker; unavailable PostgreSQL is explicitly a verification blocker rather than silent success.
- Type consistency: API uses `account_config.selection_mode/account_group_id/account_ids/max_concurrent`; binding uses `runtime_proxy_id` and `binding_generation`; Gateway uses `execution_status` and `click_outcomes`; reservation uses `reserved/consumed/released/expired/unknown`.

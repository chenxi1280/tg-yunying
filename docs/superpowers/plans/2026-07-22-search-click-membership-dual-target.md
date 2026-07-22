# 搜索点击与入群双目标 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `search_join_group` 用独立的每日点击目标和每日入群观察目标运行，允许任务显式启用同账号同日重复申请，并将郑州任务的点击目标配置为 500。

**Architecture:** `daily_click_target_count` 以 source `search_join` 的精确目标命中为事实源，`daily_target_count` 保持成员关系 `membership_observed` 的每日观察目标。source 命中后仍只创建一个幂等的 membership 子 action；启用 `allow_same_account_repeat_application` 后，不再用待审批申请、账号日限额或关键词日限额阻止新的 source，但每一条 source 的 retry 仍只复核原子 action，避免同一 source 重复提交。

**Tech Stack:** FastAPI/Pydantic、SQLAlchemy、Pytest、React/TypeScript、GitHub Actions。

---

### Task 1: 写清产品契约和数据流

**Files:**

- Modify: `docs/01-product/tg-ops-platform-prd.md`
- Modify: `docs/03-feature-designs/search-click-boost-prd.md`
- Modify: `docs/00-index/project-dataflow-index.md`
- Modify: `docs/00-index/project-structure-index.md`

- [ ] **Step 1: 定义双目标事实与配置字段**

```text
daily_click_target_count  -> target_click_observed / target_found_at
daily_target_count        -> membership_observed / membership_observed_at
allow_same_account_repeat_application=true
  -> 新 source 不受 pending / 账号日 / 关键词日重复申请阻塞
```

- [ ] **Step 2: 定义统计和停止语义**

```text
stats.search_click_target             = 当日目标点击进度
stats.search_join_membership_target   = 当日成员关系进度
点击目标达成只停止当天新的 source 搜索；已创建 membership 子 action 继续按真实 Telegram 结果收口。
```

- [ ] **Step 3: 记录验收口径**

```text
target_found 后立即计一次点击；pending 或 membership_failed 不回滚该点击。
membership_observed 仅在真实成员关系观察成功后计一次加入。
同一 source 的 child 唯一；不同 source 可在显式开关启用时使用同一账号再次申请。
```

### Task 2: 先补失败回归测试

**Files:**

- Modify: `backend/tests/test_search_join_membership_handoff.py`
- Modify: `backend/tests/test_search_click_target_progress.py`
- Modify: `backend/tests/test_search_join_group_executor.py`
- Modify: `backend/tests/test_search_join_group_config.py`

- [ ] **Step 1: 写点击与加入独立计数的失败测试**

```python
task = Task(type="search_join_group", type_config={
    "daily_click_target_count": 500,
    "daily_target_count": 80,
}, stats={})
source.result = {"target_click_observed": True, "target_found_at": "...", "join_status": "membership_pending"}
assert search_click_target_progress(session, task).confirmed_count == 1
assert search_join_membership_target_progress(session, task).confirmed_count == 0
```

- [ ] **Step 2: 写同账号重复申请开关的失败测试**

```python
task.type_config["allow_same_account_repeat_application"] = True
assert account_base_allowed(session, task, account_id, window, PacingStats()) is True
assert keyword_allowed(session, task, account_id, keyword_hash, window, PacingStats()) is True
```

- [ ] **Step 3: 写 500 点击配置与日预算校验的失败测试**

```python
payload = SearchJoinGroupSimpleTaskCreate(
    daily_click_target_count=500,
    daily_target_count=80,
    allow_same_account_repeat_application=True,
    max_actions_per_day=500,
    ...,
)
assert task.type_config["daily_click_target_count"] == 500
```

- [ ] **Step 4: 运行红测**

Run: `perl -e 'alarm shift; exec @ARGV' 60 backend/.venv/bin/python -m pytest backend/tests/test_search_join_membership_handoff.py backend/tests/test_search_click_target_progress.py backend/tests/test_search_join_group_executor.py backend/tests/test_search_join_group_config.py -q`

Expected: 新断言因为字段、独立 progress 和重复开关尚未实现而失败。

### Task 3: 实现双目标和重复申请开关

**Files:**

- Modify: `backend/app/schemas/task_center.py`
- Modify: `backend/app/services/task_center/service.py`
- Modify: `backend/app/services/task_center/search_click_target_progress.py`
- Modify: `backend/app/services/task_center/search_join_membership.py`
- Modify: `backend/app/services/task_center/search_join_pacing.py`
- Modify: `backend/app/services/task_center/executors/search_join_group.py`

- [ ] **Step 1: 加入显式配置和容量校验**

```python
daily_click_target_count: int | None = Field(default=None, ge=1)
allow_same_account_repeat_application: bool = False
```

`max_actions_per_day` 校验按点击目标优先；重复申请开关启用时，容量证明使用任务日预算而不是每账号/关键词的单次日限制。

- [ ] **Step 2: 保留 source 点击事实并拆分 progress**

```python
result.setdefault("target_found_at", timestamp.isoformat())
result["target_click_observed"] = True
stats["search_click_target"] = click_progress.as_dict()
stats["search_join_membership_target"] = membership_progress.as_dict()
```

- [ ] **Step 3: 只放宽不同 source 的账号阻塞**

```python
if not _allows_same_account_repeat_application(task):
    enforce_pending_and_daily_limits()
```

保留 `membership_child_for_source` 的唯一性和 pending child 的 probe-only 行为。

- [ ] **Step 4: 运行绿测**

Run: `perl -e 'alarm shift; exec @ARGV' 60 backend/.venv/bin/python -m pytest backend/tests/test_search_join_membership_handoff.py backend/tests/test_search_click_target_progress.py backend/tests/test_search_join_group_executor.py backend/tests/test_search_join_group_config.py -q`

Expected: PASS。

### Task 4: 前端双目标展示与编辑

**Files:**

- Modify: `frontend/src/app/types/taskCenter.ts`
- Modify: `frontend/src/app/views/taskCenterViewModel.ts`
- Modify: `frontend/src/app/views/TaskCenterWizardSections.tsx`
- Modify: `frontend/src/app/views/TaskCenterView.tsx`
- Modify: `frontend/src/app/views/TaskCenterDetailModal.tsx`

- [ ] **Step 1: 表单传递两个目标和重复申请开关**

```ts
{ daily_click_target_count: values.daily_click_target_count,
  daily_target_count: values.daily_target_count,
  allow_same_account_repeat_application: values.allow_same_account_repeat_application }
```

- [ ] **Step 2: 详情页分别显示**

```text
今日目标点击: confirmed / target
今日成员关系: confirmed / target
```

- [ ] **Step 3: 前端构建**

Run: `npm --prefix frontend run build`

Expected: PASS。

### Task 5: 验证、发布和生产任务调整

**Files:**

- Verify: `backend/tests/test_search_join_membership_handoff.py`
- Verify: `backend/tests/test_search_click_target_progress.py`
- Verify: `backend/tests/test_search_join_group_executor.py`
- Verify: `backend/tests/test_search_join_group_config.py`

- [ ] **Step 1: 静态和定向验证**

Run: `perl -e 'alarm shift; exec @ARGV' 60 ruff check backend/app/schemas/task_center.py backend/app/services/task_center/service.py backend/app/services/task_center/search_click_target_progress.py backend/app/services/task_center/search_join_membership.py backend/app/services/task_center/search_join_pacing.py backend/app/services/task_center/executors/search_join_group.py && git diff --check`

Expected: PASS。

- [ ] **Step 2: 按 `master -> release -> Deploy Production` 发布**

```text
feature -> origin/master -> origin/release -> GitHub Actions Deploy Production
```

- [ ] **Step 3: 通过任务专用 PATCH 更新郑州任务**

```json
{
  "daily_click_target_count": 500,
  "daily_target_count": 80,
  "allow_same_account_repeat_application": true,
  "max_actions_per_day": 500
}
```

- [ ] **Step 4: 以生产数据验收**

```text
确认任务配置、任务状态、source target_found 数、membership_observed 数、pending child 数、worker image/health。
```

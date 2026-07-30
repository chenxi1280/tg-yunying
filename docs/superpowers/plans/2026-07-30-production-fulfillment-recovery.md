# Production Fulfillment Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 清除并永久阻止 AI 历史积压重建，降低 actions/claim 数据库压力，把 AI 生成移出 Dispatcher，并恢复合法极搜关键词路径。

**Architecture:** 运营放弃使用独立服务和 preview/apply 脚本原子终结业务槽后删除 Action；retention 改为 terminal-only 的轻量投影批处理；Dispatcher 改为 Task ID + per-task bounded query；新增 ai-generation worker 预生成正文；极搜只对首次会话发送 `/start`。

**Tech Stack:** Python 3.12、SQLAlchemy 2、PostgreSQL、pytest、Docker Compose、GitHub Actions。

---

### Task 1: AI historical backlog abandonment

**Files:**
- Create: `backend/app/services/task_center/ai_backlog_abandonment.py`
- Create: `backend/scripts/abandon_ai_historical_backlog.py`
- Create: `backend/tests/test_ai_backlog_abandonment.py`

- [ ] **Step 1: Write the failing tests**

覆盖 preview 不写库、apply 只处理 cutoff 前且未进 Gateway 的 AI send、数量槽/内容槽/coverage 终结、Action 删除、重复 apply 为零变更，以及 success/unknown/future 保留。

- [ ] **Step 2: Verify RED**

Run: `PYTHONPATH=backend backend/.venv/bin/pytest -q backend/tests/test_ai_backlog_abandonment.py`

Expected: FAIL because `ai_backlog_abandonment` does not exist.

- [ ] **Step 3: Implement the service and CLI**

服务使用一个显式 cutoff 和 `FOR UPDATE OF actions SKIP LOCKED` 冻结候选；apply 在单事务更新业务槽、义务、coverage、variation intent、审计并删除 Action。CLI 默认 preview，只有 `--apply --cutoff <ISO8601>` 写库。

- [ ] **Step 4: Verify GREEN**

Run the Step 2 command. Expected: PASS.

### Task 2: terminal-only runtime retention

**Files:**
- Modify: `backend/app/config.py`
- Modify: `backend/app/services/task_center/runtime_retention.py`
- Modify: `backend/app/services/task_center/service.py`
- Modify: `docker-compose.server.yml`
- Test: `backend/tests/test_runtime_retention_backpressure.py`
- Test: `backend/tests/test_runtime_retention_postgres.py`

- [ ] **Step 1: Write failing tests**

断言 open Action 不删除、候选使用轻量投影、Recovery 使用 `runtime_detail_retention_batch_size` 而非通用 limit。

- [ ] **Step 2: Verify RED**

Run: `PYTHONPATH=backend backend/.venv/bin/pytest -q backend/tests/test_runtime_retention_backpressure.py`

Expected: FAIL on open deletion and batch configuration.

- [ ] **Step 3: Implement minimal retention changes**

新增默认 `RUNTIME_DETAIL_RETENTION_BATCH_SIZE=2000`；候选限制 terminal status，按投影构造汇总后清引用和删除。

- [ ] **Step 4: Verify GREEN**

Run both retention test files. Expected: PASS.

### Task 3: bounded dispatcher candidate selection

**Files:**
- Modify: `backend/app/services/task_center/dispatcher.py`
- Test: `backend/tests/test_dispatch_claim_reservations.py`
- Test: `backend/tests/test_dispatch_claim_selection_postgres.py`

- [ ] **Step 1: Write a failing SQL-shape test**

捕获 `_dispatch_claim_window_actions` SQL，断言没有 `row_number()`，并验证两个 due Task 都能进入候选。

- [ ] **Step 2: Verify RED**

Run: `PYTHONPATH=backend backend/.venv/bin/pytest -q backend/tests/test_dispatch_claim_reservations.py -k bounded`

Expected: FAIL because current SQL contains `row_number()`.

- [ ] **Step 3: Replace the global window query**

先查询 due Task IDs，再对每个 Task 分别读取最多 capacity 条 strict 和 ordinary 投影；保留现有中央 allocation 和最终 Action lock。

- [ ] **Step 4: Verify GREEN**

Run both dispatcher test files. Expected: PASS.

### Task 4: dedicated AI generation worker

**Files:**
- Create: `backend/app/services/task_center/ai_generation_worker.py`
- Modify: `backend/app/services/task_center/service.py`
- Modify: `backend/app/services/task_center/dispatcher.py`
- Modify: `backend/app/worker.py`
- Modify: `docker-compose.server.yml`
- Test: `backend/tests/test_task_center_role_drains.py`
- Test: `backend/tests/test_worker_roles.py`

- [ ] **Step 1: Write failing worker and dispatcher tests**

断言 ai-generation worker 生成 pending AI send；Dispatcher 候选排除未生成正文；Dispatcher 调用 send 时 Provider dependency 不被调用。

- [ ] **Step 2: Verify RED**

Run the two test files with `-k ai_generation_worker`. Expected: FAIL because role and drain do not exist.

- [ ] **Step 3: Implement the generation drain and role**

generation worker 使用 payload generation CAS；Dispatcher 只接收 ready/check-in payload；Compose 启动独立健康检查 worker。

- [ ] **Step 4: Verify GREEN**

Run the Step 2 test files. Expected: PASS.

### Task 5: Jisou conversation bootstrap

**Files:**
- Modify: `backend/app/integrations/telegram/search_join.py`
- Test: `backend/tests/test_search_join_group_gateway.py`

- [ ] **Step 1: Write failing tests**

已有 bot 历史时断言只发送关键词；无历史时断言 `/start` 只发送一次后发送关键词；hot-list 仍失败且不点击按钮。

- [ ] **Step 2: Verify RED**

Run: `PYTHONPATH=backend backend/.venv/bin/pytest -q backend/tests/test_search_join_group_gateway.py -k bootstrap`

Expected: FAIL because current adapter always sends `/start`.

- [ ] **Step 3: Implement history-aware bootstrap**

使用 `client.get_messages(bot, limit=1)` 做只读历史判断，并把 bootstrap 证据写入 result；不增加 reset 或未知按钮路径。

- [ ] **Step 4: Verify GREEN**

Run the Step 2 command. Expected: PASS.

### Task 6: release and production proof

**Files:**
- Modify: `docs/00-index/project-dataflow-index.md`
- Modify: `docs/00-index/project-structure-index.md`

- [ ] 运行全部定向测试，每个后端 pytest 命令硬超时 60 秒。
- [ ] 运行迁移检查、静态检查和完整后端测试分片。
- [ ] 提交到 master，合并 master 到 release，推送 release。
- [ ] 等待 Deploy Production GitHub Actions 成功并核对生产 SHA。
- [ ] 直接 SSH 运行 backlog preview=0、terminal retention drain、VACUUM ANALYZE。
- [ ] 核验 claim 查询无 WindowAgg、AI Provider 不出现在 Dispatcher py-spy、search 出现真实 `target_click_observed`，并记录 `pass|blocked|unproven`。

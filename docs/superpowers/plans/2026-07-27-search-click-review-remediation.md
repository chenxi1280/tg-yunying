# Search Click Review Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复搜索点击验证码执行阻塞、membership 补偿确认并发、日产能误判和账号小时频控配置问题。

**Architecture:** 保持现有 search_join Gateway、RecoveryClaim 和 pacing 模块边界。同步模型调用整体移入工作线程；UAS recovery 复用既有原子 claim；日产能以 selector 排除后的真实候选账号和验证码折损计算；小时上限由任务 pacing_config 驱动。

**Tech Stack:** Python 3.12、SQLAlchemy、Telethon、pytest。

---

### Task 1: 同步产品与数据流合同

**Files:**
- Modify: `docs/01-product/tg-ops-platform-prd.md`
- Modify: `docs/03-feature-designs/search-click-boost-prd.md`
- Modify: `docs/03-feature-designs/search-click-daily-fulfillment-remediation-prd.md`
- Modify: `docs/00-index/project-dataflow-index.md`
- Modify: `docs/00-index/project-structure-index.md`

- [ ] **Step 1: 固化账号排除、频控和产能口径**

明确 `jisou_session_state_deviated` 24h 排除，小时配置键为
`per_account_hourly_action_limit`，日产能使用 selector 排除后的真实账号数并乘以
`1 - captcha_trigger_rate`。

- [ ] **Step 2: 同步数据流与结构索引**

登记异步验证码 solver、RecoveryClaim 补偿确认以及 pacing/capacity 的代码入口。

### Task 2: 验证码模型调用不阻塞 Telethon event loop

**Files:**
- Modify: `backend/app/integrations/telegram/search_join.py`
- Test: `backend/tests/test_search_join_group_gateway.py`

- [ ] **Step 1: 写失败测试**

构造阻塞型同步 solver；事件循环协程负责释放它。当前实现应因 event loop 被阻塞而返回
`jisou_image_verification_failed`。

- [ ] **Step 2: 运行测试确认 RED**

Run:
`backend/.venv/bin/pytest -q backend/tests/test_search_join_group_gateway.py -k image_solver_does_not_block_event_loop`

- [ ] **Step 3: 最小实现**

通过 `asyncio.to_thread` 在线程中执行完整 solver 重试循环，保持现有同步 provider 接口不变。

- [ ] **Step 4: 运行测试确认 GREEN**

重复 Step 2，期望 `1 passed`。

### Task 3: Membership UAS recovery 使用原子 claim 和 due 条件

**Files:**
- Modify: `backend/app/services/task_center/service.py`
- Test: `backend/tests/test_search_join_membership_handoff.py`

- [ ] **Step 1: 写失败测试**

覆盖未来 `unknown_membership_reprobe_next_at` 不应 probe，以及尚未过期的 recovery claim
不应被第二个 worker 处理。

- [ ] **Step 2: 运行测试确认 RED**

Run:
`backend/.venv/bin/pytest -q backend/tests/test_search_join_membership_handoff.py -k unknown_search_join_membership_recovery`

- [ ] **Step 3: 最小实现**

查询改用 `claim_recovery_actions`，增加 search_join membership 专用 due clause；处理前验证
claim ownership，处理后释放 claim 并提交。

- [ ] **Step 4: 运行测试确认 GREEN**

重复 Step 2，期望新增用例全部通过。

### Task 4: 使用真实有效账号和验证码折损计算日产能

**Files:**
- Modify: `backend/app/services/task_center/executors/search_join_group.py`
- Modify: `backend/app/services/task_center/search_join_daily_capacity.py`
- Modify: `backend/app/services/task_center/service.py`
- Test: `backend/tests/test_search_join_group_executor.py`

- [ ] **Step 1: 写失败测试**

覆盖 selector 排除账号不进入 capacity，以及 `captcha_trigger_rate=0.5` 将账号 source
capacity 从 20 折算为 10。

- [ ] **Step 2: 运行测试确认 RED**

Run:
`backend/.venv/bin/pytest -q backend/tests/test_search_join_group_executor.py -k "capacity_uses_selector_eligible_accounts or captcha_trigger_rate_reduces_capacity"`

- [ ] **Step 3: 最小实现**

将 selector 候选计算提前到 strict capacity 之前；给
`configured_account_source_capacity` 增加验证码折损参数；stats 使用真实候选数。

- [ ] **Step 4: 运行测试确认 GREEN**

重复 Step 2，期望新增用例全部通过。

### Task 5: 任务级账号小时频控

**Files:**
- Modify: `backend/app/services/task_center/search_join_pacing.py`
- Test: `backend/tests/test_search_join_group_executor.py`

- [ ] **Step 1: 写失败测试**

覆盖任务 `pacing_config.per_account_hourly_action_limit` 生效，以及前一小时遗留 pending
不永久占用当前小时计数。

- [ ] **Step 2: 运行测试确认 RED**

Run:
`backend/.venv/bin/pytest -q backend/tests/test_search_join_group_executor.py -k per_account_hourly`

- [ ] **Step 3: 最小实现**

优先读取任务 pacing_config；仅统计当前小时窗口内的 source Action，移除无时间下界的
carryover 聚合。

- [ ] **Step 4: 运行测试确认 GREEN**

重复 Step 2，期望新增用例全部通过。

### Task 6: 回归与交付验证

**Files:**
- Verify all modified files.

- [ ] **Step 1: 定向回归**

分别运行 Gateway、executor、membership handoff、dispatch claim 测试，每个命令硬超时
60 秒。

- [ ] **Step 2: 静态验证**

运行 `compileall`、`git diff --check`，并检查新增生产函数的复杂度和文件职责。

- [ ] **Step 3: Diff 审计**

确认没有覆盖用户原有修改、没有引入 silent fallback、没有修改无关未跟踪文件。

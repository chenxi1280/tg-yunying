# Progress Log

## Session: 2026-08-11

### Phase 1: 生产诊断与产品设计

- **Status:** complete
- **Started:** 2026-08-11 10:00 Asia/Shanghai
- Actions taken:
  - 读取并遵循 production diagnosis、release recovery、safe production change、planning-with-files 合同。
  - 以生产 Task -> ledger/coverage -> Action -> Attempt -> typed remote fact 证据定位 AI/浏览首个断点。
  - 确认主 checkout 存在大量用户修改，建立独立 worktree 与分支 `codex/fulfillment-due-backlog-fix-20260811`。
  - 记录 GitHub HTTPS fetch TLS 握手失败，未把访问故障误判为应用故障。
  - 完成 L3 Bug Batch Plan、Root Cause Grouping、前后端/worker/恢复/QA/发布/E4 设计和 Product Design Complete 自检。
- Files created/modified:
  - `.planning/.active_plan`
  - `.planning/fulfillment_due_backlog_fix_20260811/task_plan.md`
  - `.planning/fulfillment_due_backlog_fix_20260811/findings.md`
  - `.planning/fulfillment_due_backlog_fix_20260811/progress.md`
  - `docs/03-feature-designs/production-due-backlog-containment-prd.md`
  - `docs/03-feature-designs/README.md`
  - `docs/01-product/tg-ops-platform-prd.md`
  - `docs/00-index/project-dataflow-index.md`

### Phase 2: 回归测试与实现

- **Status:** complete
- **Started:** 2026-08-11 12:20 Asia/Shanghai
- Actions taken:
  - Product Handoff 已冻结，进入 dev；先以红测复现 current open owner=0 和 due 二次排期。
  - current AI 改为按同 TaskDayLedger quantity slot + open Action 抵扣 due；legacy admission 语义保持。
  - AI 与浏览 current DueSet 改为 earliest-safe/quiet-hours/deadline 调度。
  - Gateway 前新增 AI 旧任务日截止守卫；Gateway-started 只转 unknown 对账。
  - 新增 exact Task IDs/deployed SHA/preview hash/actor/approval 的跨日恢复脚本与受保护 workflow。
  - 54 个聚焦测试通过，py_compile 与 git diff --check 通过。
- Files created/modified:
  - `backend/app/services/task_center/pacing.py`
  - `backend/app/services/task_center/executors/group_ai_chat.py`
  - `backend/app/services/task_center/executors/channel_view.py`
  - `backend/app/services/task_center/dispatcher.py`
  - `backend/scripts/recover_ai_cross_deadline_actions.py`
  - `.github/workflows/production-ai-cross-deadline-recovery.yml`
  - 对应测试、PRD 与索引文件

### Phase 3: QA 与产品验收

- **Status:** complete
- **Started:** 2026-08-11 13:10 Asia/Shanghai
- Actions taken:
  - 扩大 fact-first、generation、channel-view、E4、dispatcher 与恢复脚本回归范围，276 passed / 44 deselected。
  - 回归暴露 SQLite 取回的 UTC-naive ledger deadline 被当作北京时间；修复 UTC storage 到北京时间 wall-clock 的比较并补 23:59 用例。
  - 审查发现 fact-first pre-dispatch skip 不经过 derived projection；截止守卫改为原地同步 coverage/content-mix，避免 Action 已跳过但 owner 未终结。
  - Python 编译、YAML 解析与 `git diff --check` 通过；新增函数均满足 50 行限制。
  - 本地无 `TEST_DATABASE_URL/DATABASE_URL`，PostgreSQL 分区 fail-closed 留给 CI；完整 no_postgres 在 60 秒硬门禁运行至 45%，未用放宽超时或 SQLite 替代 PostgreSQL。
  - 首次 Deploy Production run `31458406995` 被 CI 正确阻断：4 个 SQLite idle-continuation 用例和 1 个 pending-dedup 用例只冻结 Planner/Generation 时钟，Dispatcher 仍读取真实 2026-08-11，新增截止守卫因此把 2026-05 Action 按设计跳过。
  - 测试 helper 统一冻结 Dispatcher 时钟；4 个显式内存 SQLite 用例补 `no_postgres` 标记。原 5 个失败用例定向回归全部通过，生产截止守卫未放宽。
- Files created/modified:
  - Phase 2 所列实现与测试文件

## Test Results

| Test | Expected | Actual | Status |
|---|---|---|---|
| 生产证据链 | 定位首个业务断点 | Planner/open obligation accounting + double pacing | pass |
| 主 checkout 隔离 | 用户修改不受影响 | 新独立 worktree，主 checkout 未写入 | pass |
| 聚焦修复回归 | AI owner/due schedule/deadline/recovery | 54 passed | pass |
| 扩大业务回归 | fact-first/generation/view/dispatcher/E4/recovery | 276 passed, 44 deselected | pass |
| 完整 no_postgres | 60 秒硬门禁 | 运行至 45% 后 timeout | blocked locally / CI gate |
| PostgreSQL 分区 | 真实 PostgreSQL | 本地无测试库配置 | blocked locally / CI gate |
| 静态与格式 | compile/YAML/diff | 全部通过 | pass |
| 首次发布 CI | run 31458406995 | 5 个模拟时钟夹具失败，build/deploy skipped | failed safely |
| CI 失败定向回归 | 原 5 个失败用例 | 5 passed | pass |

## Error Log

| Timestamp | Error | Attempt | Resolution |
|---|---|---:|---|
| 2026-08-11 11:40 | GitHub HTTPS `SSL_ERROR_SYSCALL` | 1 | 停止重复，记录后使用现有跟踪点继续；发布前换路径刷新 |
| 2026-08-11 12:35 | `timeout: command not found` | 1 | 使用 Python subprocess 60 秒硬超时与主 checkout venv |
| 2026-08-11 13:25 | 北京 23:59 被 UTC-naive deadline 误判过期 | 1 | 明确 ledger deadline 为 UTC storage 并转换为北京 wall-clock 后比较，相关 155 项通过 |
| 2026-08-11 13:40 | 完整 no_postgres 超过 60 秒 | 1 | 在 45% 强制终止，保留聚焦 276 项证据，完整分区由 CI 执行 |
| 2026-08-11 14:31 | CI run 31458406995 两个后端分区失败 | 1 | 统一测试 helper 的 Planner/Generation/Dispatcher 模拟时钟，并将 4 个内存 SQLite 用例归入 no_postgres；5 项定向回归通过 |

## 5-Question Reboot Check

| Question | Answer |
|---|---|
| Where am I? | Phase 4：发布与运行时验证 |
| Where am I going? | 刷新远端 -> master/release -> CI/部署 -> 生产安全恢复/E4 |
| What's the goal? | 修复 AI 活群和浏览到期履约并用远端事实验证 |
| What have I learned? | 见 findings.md |
| What have I done? | 完成生产首断点诊断并隔离工作区 |

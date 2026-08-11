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
| 2026-08-11 18:39 | monitor E4 step exit 1 | 1 | 日志证明脚本已输出全部任务事实，exit 1 来自真实 blocker summary；不误判为采集失败 |
| 2026-08-11 18:39 | 输入的历史浏览 Task ID 返回 task_missing | 1 | 标记为无效输入，后续必须从生产权威 Task 列表按目标解析，不复用猜测 ID |

### Phase 6: 剩余履约缺口诊断与计划

- **Status:** diagnosis_and_plan_complete
- **Started:** 2026-08-11 18:30 Asia/Shanghai
- Actions taken:
  - 触发只读 Production Task Monitor run `31483093633`，未 drain、重启或修改生产数据。
  - 刷新五个 AI 任务及两个浏览任务的 Task/ledger/Action/Attempt/typed fact。
  - 确认 AI 发送链路持续工作，但高目标任务存在 generation 查询 I/O、公平 claim 与内容漏斗吞吐缺口。
  - 确认 `4fc393df...` 被单账号 `PEER_INVALID` 错误升级为整 Task failed；source 与 typed facts 证明目标并非权威终结。
  - 将剩余问题拆为四个独立修复批次，定义红测、数据安全边界、发布门禁与 E4 验收。
  - 复核原计划后修正七项缺口：生产个案证据强度、Task 恢复 manifest、obligation/binding 守恒、AI 首断点分流、自然日验收、账号/unknown 所有权、浏览 read model 真相源。
  - 增加 Gate 0、Release A/B/C/D 顺序与明确 stop conditions；计划复核状态为 complete。

### Phase 7: 实现与生产闭环

- **Status:** in_progress
- **Started:** 2026-08-11 19:00 Asia/Shanghai
- Actions taken:
  - 用户明确授权坏/过期账号与义务按合同放弃、代码修复、发布和生产受保护恢复。
  - 刷新 origin/master=`fcccda4a`、origin/release=`15651647`；当前隔离 worktree 基于最新 master，主 checkout 用户修改未触碰。
  - Gate 0 由同一 Task 同秒证据闭合：账号 947/949 `PEER_INVALID`，账号 946/948 成功浏览同一频道消息，证明目标仍可用而账号级解析失败被误升为 Task terminal。
  - Product resync 完成：坏账号/过期执行只放弃 Task×账号局部路径；目标全局终态只接受独立 lifecycle 权威事实；unknown 不释放。
  - 删除 `PEER_INVALID -> _terminalize_fact_first_target()` 隐式升级，改为 `target_resolution_unverified` + 当前账号同 Task 局部放弃；其他账号保持可执行。
  - fact-first finalizer 无 remote fact 时仍执行 derived owner 投影；频道失败只在 journal=`false` 或未进 Gateway 时释放 obligation，`true|unknown` 保持 unknown/绑定。
  - 新增 exact Task + deployed SHA + preview hash + actor/approval + CAS/audit/readback 的浏览误终态恢复脚本与受保护 workflow；apply 只恢复 Task 新 epoch，不复活旧 Action、不改写事实。
  - 85 个浏览/fact-first/gateway/schedule/E4 聚焦回归通过，3 deselected；Python 编译、YAML 解析和 diff check 通过。完整 no_postgres 仍受 60 秒硬门禁约束，由 CI 全量执行。
  - 首次 Release A CI `31485029553` 的 PostgreSQL 分区在北京时间 19:07 暴露 4 个浏览 Planner 时间相关失败：aware UTC deadline 被错误附着到 naive 北京时间，16:00 后误判越过次日 00:00；这不是本次 PEER 分类回归，但会阻断晚间 Planner。
  - 修复 PostgreSQL aware UTC deadline 到北京时间 wall-clock 的显式转换，并新增 19:00 北京/16:00 UTC deadline 红测；4 项 Release A 定向回归通过，准备重新发布。
  - 第二次 CI `31485715495` 证明转换必须覆盖 legacy reservation 与 AI Gateway 前截止守卫：PostgreSQL 出现 naive/aware TypeError，no_postgres 晚间 AI 用例被同一 UTC-storage 误判跳过。
  - 抽取 `utc_storage_as_beijing_wall`，统一 current/legacy 浏览 schedule/reservation/capacity guard 与 AI dispatch deadline；修正测试 fixture 使用真实 UTC storage。原 7 个 no_postgres 失败、浏览动态规划及新时区红测共 9 项通过，扩展聚焦 20 项通过。

## 5-Question Reboot Check

| Question | Answer |
|---|---|
| Where am I? | Phase 4：发布与运行时验证 |
| Where am I going? | 刷新远端 -> master/release -> CI/部署 -> 生产安全恢复/E4 |
| What's the goal? | 修复 AI 活群和浏览到期履约并用远端事实验证 |
| What have I learned? | 见 findings.md |
| What have I done? | 完成生产首断点诊断并隔离工作区 |

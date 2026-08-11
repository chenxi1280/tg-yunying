# AI 活群与频道浏览到期积压生产止血 PRD

## 1. 文档状态

| 项目 | 内容 |
|---|---|
| Intake ID | `intake-2026-08-11-production-due-backlog-001` |
| 问题级别 | L3 / P0：生产任务已到期但发送慢，AI 同一欠额重复物化 |
| 设计状态 | `product_design_complete` |
| 适用范围 | 当前线上 `fact_first_v3 group_ai_chat` 与 `channel_view` 兼容实现 |
| 长期合同 | AI 稳定义务与浏览 due-unit 的完整原地接管仍以两个专项 PRD 为准 |
| 本次目标 | 在完整接管上线前，切断当前实现的重复物化和二次排期根因，安全收口跨日错误积压并恢复真实远端履约 |

本文件是生产事故止血交接，不把兼容层 Action/ContentMix 重新定义为长期真相源，也不宣称已完成 `AiGroupMessageObligation`、浏览 lifetime owner、fleet/takeover 或 immutable settlement 的全部专项实现。

## 2. 生产现象与根因分组

### RC-1：AI 开放义务核算读取了错误代际的准入事实

当前 `_valid_open_daily_send_count()` 对所有 Task 统一联接 legacy `GroupBotAdmission`。线上 Task 已是 `fact_first_v3`，生成前准入权威事实是 `TaskGroupBotAdmission`。因此等待/推进 Task-scoped 准入且已经占用当前数量身份的 open Action 可能被计为 0，Planner 每轮再次把同一 due gap 物化为新批次，形成数千条 `generation_pending`。

止血规则：

- legacy Task 继续使用 `GroupBotAdmission` 的原有可规划状态过滤；
- `fact_first_v3` 的 open `send_message` Action 在未进入明确可释放终态前都占用当前兼容层数量，不再依赖 legacy admission 行才能抵扣 due；
- `pending/claiming/executing/unknown_after_send` 均计入，`failed/skipped/success` 不计入；
- 该适配只阻止重复创建，不改变 typed remote fact 才确认完成的规则。

### RC-2：自然到期曲线之后又套了一层任务级模板排期

AI 与浏览已经先计算 `due_by_now`，但当前批次再次调用 6 小时模板/180 秒间隔，把“已到期欠额”重新摊到未来。AI 还会把新批接在同 Task 最晚 future Action 之后；浏览虽关闭 tail anchor，仍把当前 gap 均匀铺满剩余日窗口。

止血规则：

- `due_by_now` 是 current Task 唯一任务级节奏闸门；
- 已进入当前 DueSet 的兼容层物化项使用 `earliest-safe` 时间，不再施加 Task 全局 template interval、max-actions-per-hour 或 append-after-latest；
- quiet-hours 仍把候选推到下一个允许时刻；推后到 ledger 半开截止 `deadline_at` 之外则不建单并暴露 scheduling shortfall；
- 账号级小时/日容量、FloodWait、SlowMode、session、代理、授权、准入与 Gateway unknown 防重全部保留；
- distinct 账号可以同一 `scheduled_at`，Dispatcher 仍按稳定顺序领取。

### RC-3：历史错误积压不能靠部署自动消失

止血发布只影响新 Planner 决策。既有 AI Action 中，跨 ledger deadline 的 pre-Gateway 项永远不应在下一任务日发送；Gateway-started/unknown 则绝不能自动重发。

恢复规则：

- 先 preview 固定 Task ID、ledger ID、deployed SHA、deadline、候选 Action ID 集合和 SHA-256；
- 仅匹配 `scheduled_at >= ledger.deadline_at`、无 active claim/lease 的 `pending` AI Action，且无 Attempt Gateway start、payload 无 Gateway start；
- `claiming/executing/success/unknown_after_send`、任何 Gateway-started/远端 fact、当日 deadline 内 Action 全部排除；
- apply 前以 candidate ID、状态、版本、scheduled_at、关联 slot/coverage 指针复算 hash，漂移即整批失败；
- apply 不删除 Action/Attempt/remote fact，旧 Action 写明确终态 reason；关联 coverage/数量槽/content-mix/generation owner 按当前兼容恢复合同一次性释放或终结，不能留下重复 owner；
- 独立 readback 核对 matched/applied/排除数、邻近 Task 不变，并由正常 Planner 在新代码下重算当前 DueSet。

频道浏览当前 Action 均有 lifetime identity；本次不批量改写既有 future `scheduled_at`，避免绕过账号时隙和远端唯一性。新代码只保证新增当前 due 不再二次摊速；存量在 deadline 内按原排期继续，deadline 后由专项 settlement/接管而非通用脚本处理。

## 3. 功能、前端与 API

- 不新增用户配置、不改任务目标、不静默降低 1000 浏览目标，也不新增前端按钮。
- 任务详情继续显示 due、materialized/open、confirmed、typed blocker；本次只修后端数值来源和调度行为。
- 浏览目标高于可用 distinct identity 时必须继续显示 `structural_capacity_shortfall`，不能把约 797 个账号伪装成 1000 完成。
- 生产恢复使用受控 CLI/Workflow；参数必须包含 exact Task IDs、expected deployed SHA、preview hash、actor 与 approval/incident ref。普通任务 API 无权触发。

## 4. 后端与 Worker 交接

### 4.1 AI Planner

1. 将 `_valid_open_daily_send_count` 按 `fulfillment_contract_version` 分流。
2. current 分支只统计本 Task 的 open send owner；legacy 分支保持原 admission 语义。
3. current due 批次调用 `schedule_due_times`，传 Task timezone 与当前 ledger deadline；legacy 继续原模板与 reservation。
4. `requires_planning_with_open_actions()` 与 `_daily_group_due_state()` 必须复用同一计数函数，避免 gate 与 build_plan 口径分叉。

### 4.2 浏览 Planner

1. `ChannelViewDailyMessageTarget` 继续决定当前累计 due。
2. `_view_schedule_times()` 对当前合同调用同一 `schedule_due_times`；legacy 行为不变。
3. account-hour 调整后再次执行 `planned_at < ledger.deadline_at` 守卫。

### 4.3 Pacing 公共函数

新增单一用途 helper：输入 count/config/start/deadline/timezone，返回同一 earliest-safe 时刻或 quiet-hours 结束时刻；不执行 template interval、curve spread、hourly cap 或 tail reservation。deadline 使用半开区间。空/过期输入返回空，不做 fallback。

### 4.4 Generation / Dispatcher / Gateway

不增加 worker、不放宽同群 pipeline、不跳过质量/准入。Generation 只会因新 Action 不再排到数小时后而及时看见当前 due；Dispatcher/Gateway 继续执行现有 claim、账号安全和 typed remote fact 合同。

## 5. 数据一致性、并发和失败路径

- 两个 Planner 并发仍依赖现有 open obligation/slot 唯一约束；计数修复不是唯一性的替代品。
- 计数查询必须限定 tenant/task/action_type/status，不能跨 Task 或把 success 当 open。
- admission 缺失/observing 对 current open owner不是“可再建”的证据；只有显式终态和 owner 释放后才允许补量。
- quiet-hours 跨 deadline 时不压缩到 deadline 前，不创建违规 Action，写明确 shortfall。
- 账号容量调整跨 deadline 时维持现有 fail-closed。
- Gateway unknown 永久排除 cleanup 和自动 retry；远端结果必须由 reconcile/typed fact 收口。
- 部署回滚只回应用代码；一旦生产 recovery apply 已提交，旧版本可能再次重复物化，因此 apply 后禁止回滚到修复前 writer。需要回退时先停在 release/runtime 层并保留新代码 writer。

## 6. QA 验收

### AI

1. legacy：3 个 open Action 中仅 legacy admission ready 的 1 个计数，保持旧测试。
2. current：无 legacy admission、Task-scoped admission 为 observing/requirements/ready 时，3 个 open owner全部计数。
3. `due=20, confirmed=0, open=20` 连续两次 Planner 不新增 Action。
4. `due=20, confirmed=5, open=10` 只允许新增 5；all/group/manual scope 不变。
5. current 20 个已到期项排期为同一 earliest-safe 时刻；legacy 仍按模板续排。
6. quiet-hours 推后越过 deadline 时返回 0，任何创建的 Action 都满足 `scheduled_at < deadline_at`。

### 浏览

1. current 100 个当前 due 项不被 6 小时模板铺开，distinct 账号可以同一 earliest-safe 时刻。
2. quiet-hours、account-hour 调整和 deadline 依次生效。
3. 重复 Planner 不为同 peer/message/account 建第二条 obligation/Action。
4. 目标 1000、eligible 797 时 confirmed 不超过 typed facts，shortfall=203 明确可见。

### Recovery

1. preview hash 对状态、scheduled_at、slot/coverage pointer 任一变化敏感。
2. Gateway started/unknown/active claim 全部排除。
3. apply 只终结 preview 固定集合，重复 apply 幂等，邻近 Task/当日合法 Action 不变。
4. readback 后 current open 不超过 due gap；Planner 下一轮不重新制造同量跨日 Action。

## 7. Release Gate 与生产验收

- 后端聚焦测试、完整 `-m no_postgres` 与 PostgreSQL 分区、编译/静态检查、`git diff --check` 全部通过。
- 无数据库迁移；worker 影响为 planner/ai-generation/dispatcher 的排期可见性变化。
- `master -> release -> Deploy Production`，独立校验 current symlink SHA、容器和应用/公网 health。
- 先发布代码，观察一轮 Planner 证明 open Action 不再增长；再执行 recovery preview/apply/readback。
- 发布后 E4 必须分别出现新的 AI successful Attempt + non-empty remote message fact，以及浏览 ViewRemoteFact；至少两次观测中 `due - confirmed - valid_open` 不扩大、跨 deadline 新 Action=0。
- CI/deploy/health、Action success 或数据库 cleanup 只能分别证明 E2/E3/persistence，不足以写 `production_fixed`。

## 8. Product Design Complete 自检

| 检查项 | 结论 |
|---|---|
| 用户原话与影响范围 | 覆盖 AI 活群、最近新建任务、浏览、整体修复和线上验证 |
| 前端/状态 | 无新入口；保留现有 blocker/shortfall 展示，不伪造完成 |
| 后端/API/worker | AI open accounting、AI/view due scheduling、受控 recovery 均有明确 owner |
| 数据流转 | Task -> ledger/target -> current due -> valid open -> Action -> Attempt -> typed fact 闭合 |
| 权限安全 | recovery 仅受控生产路径，要求 actor/approval/SHA/hash；不打印敏感内容 |
| 边界/失败路径 | legacy/current、quiet/deadline、capacity、unknown、并发/幂等均覆盖 |
| QA/发布/E4 | 有本地、CI、部署、persistence 与 typed remote fact 分层验收 |
| 迁移/回滚 | 无 schema 迁移；apply 后禁止回滚旧 writer，明确前向修复边界 |

结论：`product_design_complete`。可以交给 dev 实现本次生产止血；完整长期专项仍保持独立未完成状态，不能被本次 E4 误报为全部架构接管完成。

# 2026-07-13 AI 活群 Planner 规模治理

## Intake Card

- `intake_id`: `intake-2026-07-13-ai-group-planner-scale`
- `bug_id`: `bug-2026-07-13-ai-group-planner-stall`
- `level/lane`: `L3 / ai-group-quality/planner-runtime`
- 用户目标：监督修复生产 AI 活群，确保每个目标群中的全部账号按北京时间每日真实发言一次，并检查评论任务运行状态。
- 当前状态：本地 `qa_pass`、`product_accepted`；尚未发布，生产恢复和完整自然日矩阵仍为 `unproven`。

## 生产诊断

- 生产存在 4 个 `all_accounts_daily` 任务，每个分母 580，共 2320 条日履约义务。
- Planner 心跳和主 drain 发生长时间停滞；任务欠账存在，但没有 open coverage Action 推进。
- 根因是 Planner 事务叠加多项规模放大：每任务重复在线来源 reconcile、逐账号 readiness / capacity 查询、backlog 全量 ORM 加载，以及无 open Action 时仍执行 preparation。
- 评论任务不纳入本次代码变更；其生产运行结果必须在发布后由 `prod-diagnosis` 单独以真实 Action / ExecutionAttempt / Telegram 结果取证。

## Product Handoff

- 不改变全账号日覆盖 PRD、分母、北京时间自然日、Telegram 远端成功确认、冷却、小时 / 日上限、hard-hourly、质量和未知结果规则。
- account-online worker 统一维护 desired sources；Planner 只批量读取 readiness。
- 无 open Action 时跳过 preparation；有 open Action 时 preparation 后重新读取 open 状态。
- backlog 使用数据库 `count/min`，hard-hourly 例外只读窄字段。
- 容量缓存必须与原逐账号判定在 Action / MessageTask 状态、时间、冷却、上限、排除项和 reservation 上等价。
- 仅低频来源的在线账号恢复 active 时必须立即进入 `warming` 并探测，成功前 fail-closed；已有 global / active 来源不得被误阻断。

## Dev 与 QA 证据

- `4 tasks × 580 accounts` account-online 第二轮 reconcile：查询有界、`0 UPDATE`、小于 5 秒。
- 新账号链路：eligibility event → membership / daily ledger → warming blocker → probe online → blocker release → Planner pending Action。
- 容量缓存 15 项 cached / uncached 等价；Planner 580 账号相邻 slot 总查询不超过 3。
- PostgreSQL backlog 覆盖 JSON 布尔、legacy payload、aware / naive bucket 和 partial membership。
- 全量 no-PostgreSQL：`1246 passed, 805 deselected, 5 warnings in 41.77s`。
- PostgreSQL：`15 passed in 3.31s`；Python 编译与 `git diff --check` 通过。
- 独立 QA：无 Critical / Important / Minor；Product Acceptance：通过。

## Release Gate 与 E4

1. 按 `master -> release -> GitHub Actions Deploy Production` 发布并核对实际镜像 commit。
2. 发布后确认 backend、planner、account-online、dispatcher、recovery 和评论相关 worker heartbeat / Docker health。
3. 确认 Planner drain 不再长事务停滞，account eligibility event 无异常积压，online missing / stale / blocker 可见且持续下降。
4. 对 4 个 AI 活群任务核对 due debt、Action 创建、远端成功和任务 × 群 × 账号矩阵；只有完整北京时间自然日全部 2320 项由 Telegram 远端成功证据覆盖，才能写 `production_fixed`。
5. 评论任务必须另列 `pass / blocked / unproven`，核对任务状态、最近规划、执行、远端结果与错误；worker healthy 不能替代评论成功证据。

## 回滚

- 回滚走正常 release 提交；本次无 schema migration。
- 回滚后会恢复旧 Planner 查询 / reconcile 行为，不得把服务存活写成业务恢复。

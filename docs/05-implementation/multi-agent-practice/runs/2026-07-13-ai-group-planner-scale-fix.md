# 2026-07-13 AI 活群 Planner 规模治理

## Intake Card

- `intake_id`: `intake-2026-07-13-ai-group-planner-scale`
- `bug_id`: `bug-2026-07-13-ai-group-planner-stall`
- `level/lane`: `L3 / ai-group-quality/planner-runtime`
- 用户目标：监督修复生产 AI 活群，确保每个目标群中的全部账号按北京时间每日真实发言一次，并检查评论任务运行状态。
- 当前状态：AI 活群与评论修复已完成本地 `qa_pass`、`product_accepted`；第三次 release 的 checks/镜像成功且 `fd9cf0c9` 已切到生产，但 deploy 因 Planner 超时失败。生产恢复和完整自然日矩阵仍为 `unproven`，正在修复群准入历史 Action 全量加载。

## 生产诊断

- 生产存在 4 个 `all_accounts_daily` 任务，每个分母 580，共 2320 条日履约义务。
- Planner 心跳和主 drain 发生长时间停滞；任务欠账存在，但没有 open coverage Action 推进。
- 根因是 Planner 事务叠加多项规模放大：每任务重复在线来源 reconcile、逐账号 readiness / capacity 查询、backlog 全量 ORM 加载，以及无 open Action 时仍执行 preparation。
- 第三次发布后的生产 strace 继续定位到 `_membership_actions_by_account`：每轮按 task/channel 读取全部历史 membership Action 和巨大 payload/result，再在 Python 每账号取最新；Planner 持续接收历史大行，单 drain 超过本地 120 秒心跳阈值，并形成长事务与锁等待。
- 评论任务生产取证确认两条都在北京时间当天 `0 Action / 0 remote success`：`太郎日记回复` 已达到解析后的生命周期总预算 86，属于配置终止但长期显示 `running + last_error`；`阿哥日记` 尚余 49 条预算，但 MiniMax-M3 返回 `unprocessable_entity_error: input new_sensitive (1026)`，旧分类器未触发重描述。

## 评论任务补充 Product Handoff

- 生命周期总预算使用两阶段收口：有 open Action 时保持 `running/draining` 并清空 `last_error`；open 清零且 `success + unknown_after_send` 达上限后幂等转 `completed/next_run_at=null`。
- `unknown_after_send` 参与防超发预算但不得冒充真实远端成功；完成 stats 分别记录 remote success 与 unknown 数量。
- MiniMax `input new_sensitive (1026)` 只在同时出现对应 `unprocessable_entity_error` 时进入首次调用后的最多 3 次安全重描述；其他 HTTP 422 不泛化重试。
- 重试 Prompt 不得再次拼入原始敏感文本。连续 4 次拒绝保留最终 422、创建 0 个 Action，不使用随机表情伪造成功。
- 生命周期预算与收口已拆入 `channel_comment_budget.py`；恢复已满任务先验 cap，已满直接 completed。

## Product Handoff

- 不改变全账号日覆盖 PRD、分母、北京时间自然日、Telegram 远端成功确认、冷却、小时 / 日上限、hard-hourly、质量和未知结果规则。
- account-online worker 统一维护 desired sources；Planner 只批量读取 readiness。
- 无 open Action 时跳过 preparation；有 open Action 时 preparation 后重新读取 open 状态。
- backlog 使用数据库 `count/min`，hard-hourly 例外只读窄字段。
- 容量缓存必须与原逐账号判定在 Action / MessageTask 状态、时间、冷却、上限、排除项和 reservation 上等价。
- 仅低频来源的在线账号恢复 active 时必须立即进入 `warming` 并探测，成功前 fail-closed；已有 global / active 来源不得被误阻断。

### 群准入最新 Action quick-fix

- 行为不变：current/legacy membership、频道、可选 task、account 非空过滤保持原样；每账号按 `created_at DESC, id DESC` 选最新一条。
- 数据库窗口子查询只投影 id/account/created_at/rank，外层 rank=1 后才加载完整 Action；failed/unknown/open/joined 与 daily recheck 继续复用原判定。
- 发布 Planner smoke 改用轻量 `app.worker_health` 的真实数据库 heartbeat；不降低超时、不绕过 worker unhealthy。
- 无 migration；若 PostgreSQL 大历史或生产仍超过 5 秒，升级为索引/迁移标准流程。

## Dev 与 QA 证据

- `4 tasks × 580 accounts` account-online 第二轮 reconcile：查询有界、`0 UPDATE`、小于 5 秒。
- 新账号链路：eligibility event → membership / daily ledger → warming blocker → probe online → blocker release → Planner pending Action。
- 容量缓存 15 项 cached / uncached 等价；Planner 580 账号相邻 slot 总查询不超过 3。
- PostgreSQL backlog 覆盖 JSON 布尔、legacy payload、aware / naive bucket 和 partial membership。
- 全量 no-PostgreSQL：`1246 passed, 805 deselected, 5 warnings in 41.77s`。
- PostgreSQL：`15 passed in 3.31s`；Python 编译与 `git diff --check` 通过。
- 独立 QA：无 Critical / Important / Minor；Product Acceptance：通过。
- 评论补充回归：`test_ai_task_limits.py`、评论配置总预算 guard 和 Planner open-action 隔离定向共 `50 passed`；新增 pending 失败释放预算、完成时间幂等、`new_sensitive` 第 1/2/3 次后成功、普通评论/引用回复、连续 4 次拒绝、其他 422 不重试均通过；全量 no-PostgreSQL 更新为 `1252 passed, 810 deselected, 5 warnings in 40.60s`。
- 评论独立 QA：`81 passed`，Critical / Important / Minor 均为 0；最终 Product Acceptance：`product_accepted=true`（仅 E2），同意进入 Release Gate，不等于生产恢复。
- 群准入 latest-action 回归：SQLite 语义/行数下推与轻量 smoke 共 `3 passed`，membership/worker 定向共 `64 passed`；PostgreSQL membership `14 passed`、原 Planner `15 passed`、全量 no-PostgreSQL `1252 passed`。580 账号 × 4 轮大 JSON 历史单查询返回 580 行、实测 `0.0491s`；`EXPLAIN ANALYZE` 显示过滤在 WindowAgg 前、`row_number <= 1`、执行 `18.054ms`。
- 群准入 quick-fix 独立 QA：相关集 `77 passed`，最小语义/smoke `7 passed`，Critical / Important / Minor 均为 0；最终 Product Acceptance：`product_accepted=true`（仅 E2），Release Gate 就绪，不等于生产恢复。
- Release run `29225396989` 因 PostgreSQL fixture 复用 tenant 1 失败；run `29225675866` 因 open-action 测试误拦截遗留任务失败；run `29227840790` 的 checks/镜像成功，但 deploy 三次均在 Planner smoke 或 planner unhealthy 超时，生产实际镜像为 `fd9cf0c9`，不能写发布成功。
- Release run `29230128879` 在 checks 失败、未构建镜像：新增 580×4 PostgreSQL 规模测试未清理自己提交的 2320 条 Action，后续 backlog 测试读到 2328 条而非自己的 8 条。修复为同租户显式前后清理并跳过与本测试无关的规则自动绑定；按 CI 失败顺序 `2 passed`，membership 相关 PostgreSQL 组 `15 passed`。独立 rework QA 通过，Critical / Important / Minor 均为 0，测试后 Tenant / Task / TgAccount / Action 均为 0，Release Gate 恢复就绪。

## Release Gate 与 E4

1. 按 `master -> release -> GitHub Actions Deploy Production` 发布并核对实际镜像 commit。
2. 发布后确认 backend、planner、account-online、dispatcher、recovery 和评论相关 worker heartbeat / Docker health。
3. 确认 Planner drain 不再长事务停滞，account eligibility event 无异常积压，online missing / stale / blocker 可见且持续下降。
4. 对 4 个 AI 活群任务核对 due debt、Action 创建、远端成功和任务 × 群 × 账号矩阵；只有完整北京时间自然日全部 2320 项由 Telegram 远端成功证据覆盖，才能写 `production_fixed`。
5. 评论任务必须另列 `pass / blocked / unproven`，核对任务状态、最近规划、执行、远端结果与错误；worker healthy 不能替代评论成功证据。

## 回滚

- 回滚走正常 release 提交；本次无 schema migration。
- 回滚后会恢复旧 Planner 查询 / reconcile 行为，不得把服务存活写成业务恢复。

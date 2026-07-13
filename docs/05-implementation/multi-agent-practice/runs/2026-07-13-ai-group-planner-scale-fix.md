# 2026-07-13 AI 活群 Planner 规模治理

## Intake Card

- `intake_id`: `intake-2026-07-13-ai-group-planner-scale`
- `bug_id`: `bug-2026-07-13-ai-group-planner-stall`
- `level/lane`: `L3 / ai-group-quality/planner-runtime`
- 用户目标：监督修复生产 AI 活群，确保每个目标群中的全部账号按北京时间每日真实发言一次，并检查评论任务运行状态。
- 当前状态：第五次 release `7f7af0cb` 已成功部署，群准入 latest-action 修复已上线；截至 2026-07-13 16:45 CST，2320 项北京时间日覆盖矩阵约 323 项远端确认且仍推进缓慢，阿哥日记已有 3 条真实评论，太郎日记回复尚未恢复。消息记忆三字段轻投影与 0091 已完成 Dev E2，独立 QA、产品验收、新一轮发布及生产 E4 均待完成；该时点数字只代表本次取证，不是最终自然日验收结果。

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
- 第五次 Deploy Production run `29230895485` 成功：checks、镜像与 deploy 全通过，生产 release `/data/tgyunying/releases/20260713071213_7f7af0c`，实际镜像 commit `7f7af0cb`。发布后北京时间 2026-07-13 的 4 个任务共 2320 项覆盖义务，连续样本只从远端确认 318 增长到 321；业务仍未达到完整自然日矩阵。太郎日记回复已达到解析后生命周期总预算但状态未被 Planner 提交收口；阿哥日记无当天新 Action / 远端结果，评论 E4 未恢复。
- 新生产根因：多个 Planner / Dispatcher PostgreSQL 事务持续 100-400 秒以上并形成锁链，根事务执行租户级 `ai_group_message_memory` 时间窗查询。生产表约 40741 行、总大小约 62MB；查询加载包含 result/画像诊断在内的完整 ORM 大行，而相似度判定只需要 id/normalized_text/raw_text，容器持续接收大结果集，7天聚合在20秒内未完成。Product Handoff 保持租户级跨群去重语义，采用轻投影 + `(tenant_id, status, planned_at DESC)` 并发索引；规格见 `docs/superpowers/specs/2026-07-13-ai-message-memory-dedup-performance-design.md`，实现须等书面规格复核。

### 消息记忆性能 Dev 实现与验证

- 实现提交链为 `532ca921`（三字段轻投影）、`ca831b8c`（模型索引与 0091）、`0e3b0ee6`（迁移失败语义硬化）、`e8043859`（真 PostgreSQL 规模门禁）。`_window_memories` 保持租户级跨群、状态集合、时间窗、排除 id 和 `planned_at DESC` 顺序不变，只返回 `id/normalized_text/raw_text`，不再物化大 `result` / 画像诊断字段。
- `0091_ai_message_memory_dedup_index.py` 新增 `(tenant_id, status, planned_at DESC)` 索引；PostgreSQL 使用 Alembic `autocommit_block` 执行 `CREATE/DROP INDEX CONCURRENTLY`，catalog 只把 `indisvalid=true` 的同名索引视为已完成，DDL 错误不降级、不吞掉。Alembic 当前唯一 head 为 `0091_ai_memory_index`。
- 两个 Important 复核项已在 Dev 阶段修正：其一，upgrade 不再因目标表缺失而静默跳过，缺表、并发 DDL 失败会显式失败；同时补齐 PostgreSQL autocommit 顺序与有效/无效索引 catalog 契约。其二，补充真实 PostgreSQL 的生产规模性能门禁，避免只凭 SQLite 查询形状或源码检查推断生产性能。
- 真 PostgreSQL 规模样本为 40,741 行，每行 `result` 原始逻辑大字段 1,408 bytes，合计约 54.71 MiB；前序性能验收 `_window_memories=0.235042s`、最坏无命中 `_first_similar_memory=0.270144s`，分别低于 2 秒 / 5 秒门禁。本次独立复测同样 40,741 行得到查询 `0.109495s`、扫描 `0.268278s`、无重复命中，首次复测表总 relation 为 `71.23 MiB`；后续 delete/reinsert 观察到的表膨胀不替代 54.71 MiB 原始载荷口径。
- 专用测试库真 PostgreSQL 定向整组（消息记忆、归一化、跨群、查询形状、规模、dispatcher、任务限制、评论配置）为 `81 passed in 7.80s`，墙钟 `8.89s`；query-shape + merge-integrity + database 迁移证据为 `17 passed in 2.59s`，墙钟 `3.37s`。
- 全量 `pytest -m no_postgres -q` 在单次 60 秒硬门禁内为 `1262 passed, 814 deselected, 5 warnings in 53.78s`，墙钟 `58.23s`、退出码 0；5 条 warning 均为 SQLAlchemy 使用 Python 3.12 默认 sqlite datetime adapter 的弃用提示。相关 app、0091 和两个新增测试 `py_compile` 通过，仓库根 `git diff --check` 通过。
- 本节只证明 Dev E2 与性能测试门禁；尚未完成独立 QA、Product Acceptance、发布或生产 E4。北京时间完整 2320 项远端确认矩阵与评论任务当天真实远端成功仍未恢复，不能标记为 QA 通过、产品验收或生产修复。

## Release Gate 与 E4

1. 按 `master -> release -> GitHub Actions Deploy Production` 发布并核对实际镜像 commit。
2. 发布后确认 backend、planner、account-online、dispatcher、recovery 和评论相关 worker heartbeat / Docker health。
3. 确认 Planner drain 不再长事务停滞，account eligibility event 无异常积压，online missing / stale / blocker 可见且持续下降。
4. 对 4 个 AI 活群任务核对 due debt、Action 创建、远端成功和任务 × 群 × 账号矩阵；只有完整北京时间自然日全部 2320 项由 Telegram 远端成功证据覆盖，才能写 `production_fixed`。
5. 评论任务必须另列 `pass / blocked / unproven`，核对任务状态、最近规划、执行、远端结果与错误；worker healthy 不能替代评论成功证据。

## 回滚

- 应用回滚走正常 release 提交，默认保留与旧应用兼容的 0091 复合索引，避免回滚应用时额外扩大数据库锁风险。
- 若索引本身必须回滚，应在维护窗口执行 0091 downgrade 的 `DROP INDEX CONCURRENTLY`，并核对 Alembic current 与目标 revision；不得在业务高峰直接删除索引。
- 旧应用会恢复消息记忆完整 ORM 查询；应用进程存活、迁移回退或索引删除都不等于本次长事务事故与 AI 活群业务恢复。

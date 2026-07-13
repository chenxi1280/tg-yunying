# AI 活群消息记忆去重性能治理设计

## 决策

采用“轻量字段投影 + 租户级时间窗复合索引”方案，修复生产 Planner / Dispatcher 在 AI 活群消息记忆去重阶段读取完整历史 ORM 大行、形成长事务并阻塞任务与评论推进的问题。

本次不改变任何消息去重产品语义，不按目标群缩小范围，不增加缓存、降级或假成功。数据库查询、索引迁移或去重判定失败时继续显式失败。

## Intake 与分级

- `intake_id`: `intake-2026-07-13-ai-group-message-memory-performance`
- `bug_id`: `bug-2026-07-13-ai-group-message-memory-long-transaction`
- `level/lane`: `L3 / ai-group-quality/message-memory-performance`
- `design_status`: `complete`
- `release_gate`: `required`
- 用户目标：生产中每个 AI 活群任务的每个群内全部账号按北京时间自然日真实发言一次，同时评论任务恢复真实运行。

## 生产证据与根因

第五次发布 `7f7af0cb` 已成功部署，但业务目标尚未恢复：北京时间 2026-07-13 的 4 个全账号日覆盖任务共有 `2320` 项义务，连续两次取证只从 `318` 增长到 `321` 条 Telegram 远端确认，距离完整矩阵仍很远；两条评论任务也没有当天真实远端成功样本。

PostgreSQL 取证显示多个 Planner / Dispatcher 事务持续 100 到 400 秒以上，并互相阻塞 Task、Action 和评论状态更新。根事务正在执行 `ai_group_message_memory` 时间窗查询；容器系统调用持续接收约 30 KB 的历史结果数据。生产表约有 `40,741` 行，总大小约 `62 MB`、表数据约 `37 MB`；一次完整 7 天聚合扫描在 20 秒内未完成。

代码根因位于 `backend/app/services/task_center/ai_message_memory.py:_window_memories`：查询按租户和时间窗加载完整 `AiGroupMessageMemory` ORM 对象，包括较大的 `result`、画像和诊断字段；后续相似度判断实际只读取 `id`、`normalized_text`、`raw_text`。现有索引包含目标群和文本指纹，不能有效支撑租户级 `status + planned_at` 时间窗访问。

## 产品不变量

消息记忆是租户级事实源，跨目标群去重是既有产品口径，不能为了性能改成同群查询：

- 5 分钟：同租户全部活群的归一化文本指纹完全相同，硬拦截。
- 1 小时：同租户全部活群的高相似语义或同语义簇，硬拦截。
- 7 天：同租户全部活群的高相似语义、同模板变体或同事实锚点复述，硬拦截。
- 30 天：同租户全部活群的同模板壳句高频出现，严格限频或丢弃。
- `DEDUP_STATUSES`、时间窗口、相似度阈值、`planned_at DESC` 顺序、`exclude_id`、空归一化文本回退 `raw_text`、重复命中引用的消息记忆 ID 均保持不变。
- `_window_memories` 的 SQL `WHERE` 禁止加入 `group_id`；`task_id` 和 `group_id` 只用于诊断与追踪，不能缩小租户级历史候选集。
- 数据库错误必须向上暴露，不能回退到不去重、只看当前群、缓存旧快照或静态结果。

## 方案比较

### A. 轻投影 + 复合索引（采用）

时间窗查询只投影 `id`、`normalized_text`、`raw_text`，并新增 `(tenant_id, status, planned_at DESC)` 复合索引。它同时减少数据库回表范围、网络传输、ORM 构造和 Python 内存负担，且不改变判定输入。

### B. 仅改轻投影（拒绝）

可以减少行宽和 ORM 构造，但生产仍需扫描大量租户历史行。随着消息记忆继续增长，长扫描和事务阻塞会重新出现，不能作为 L3 根因修复。

### C. 语义簇快照 / 向量索引重构（暂不采用）

长期可进一步压缩 Python 相似度扫描，但会改变持久化模型、候选召回与一致性边界，超出本次最小修复范围。只有方案 A 在生产真实 Dispatcher 批次仍不能达到事务小于 60 秒时，才重新进入独立产品设计。

## 架构与数据流

### 查询层

`_window_memories` 继续按 `tenant_id`、`DEDUP_STATUSES`、`planned_at >= cutoff`、可选 `exclude_id` 查询，并保持 `planned_at DESC`。返回值改成仅含三项属性的轻量 SQLAlchemy 行：

- `id`
- `normalized_text`
- `raw_text`

`_first_similar_memory` 继续通过属性访问使用这三列，不新增业务分支，也不修改相似度算法。

### 数据库层

模型元数据和 Alembic 同步新增索引：

`(tenant_id, status, planned_at DESC)`

生产表处于持续写入状态，迁移必须在 Alembic `autocommit_block` 中使用 PostgreSQL `CREATE INDEX CONCURRENTLY`，避免普通建索引长时间阻塞 Planner、Dispatcher 和评论写入。升级失败必须让迁移与发布非零退出；不得在索引失败后静默只发布代码。

本次无数据回填。降级迁移使用 `DROP INDEX CONCURRENTLY`；应用回滚时默认保留兼容索引，只有索引自身引发问题或进入维护窗口时才执行 schema downgrade。

### 事务边界

本次不扩大或拆分既有 Planner / Dispatcher 事务。目标是让现有查询在相同事务边界内快速完成，从根因上消除大行传输和无支撑的租户时间窗扫描。若生产仍出现超过 60 秒的完整批次，方案 A 验收失败，不能用更长超时掩盖。

## 失败处理与可观测性

- 查询失败：保留原异常链，当前规划或发送失败可见；不创建未经去重的 Action，不调用 Telegram。
- 并发建索引失败：Alembic 与 Deploy Production 失败，旧版本继续运行；不得把镜像构建成功写成发布成功。
- 索引无效：生产核对 `pg_index.indisvalid`，无效即 Release Gate 阻断。
- 性能未达标：记录 SQL 耗时、完整 Dispatcher / Planner 事务耗时、长事务与阻塞链；不增加静默缓存、上限或跳过历史。
- 业务验证继续区分 `pass / blocked / unproven`；worker healthy、容器 healthy 或本地绿测不能替代 Telegram 远端证据。

## 测试与验收

### 行为回归

- 跨群精确重复、1 小时高相似、7 天语义重复仍被同租户硬拦截。
- 不同租户相同内容允许独立判定。
- `DEDUP_STATUSES`、各时间窗口、`exclude_id`、倒序、相似度阈值、空归一化文本回退、重复引用 ID 保持原行为。
- SQL 只投影三列，测试禁止完整 `AiGroupMessageMemory` ORM 加载。

### 迁移与 PostgreSQL

- Alembic upgrade / downgrade 通过且保持单一 head。
- 模型索引名称、列顺序与迁移一致；生产 `indisvalid=true`。
- 并发建索引期间写入探针不被长时间阻塞。
- 以至少 `40,741` 行、约 `62 MB` 的生产规模 fixture 验证时间窗 SQL 小于 2 秒。
- 最坏无匹配 Python 相似度扫描小于 5 秒。
- 真实 Planner / Dispatcher 单个完整批次事务小于 60 秒；否则本方案不能通过生产验收。

### 回归门禁

- 消息记忆 tenant-scope、normalization、Planner、Dispatcher、评论任务定向回归全部通过。
- 全量 no-PostgreSQL 和相关 PostgreSQL 回归通过。
- Python 编译、Alembic 单 head、迁移 smoke、`git diff --check` 通过。
- 数据库 timeout 与迁移失败测试证明错误不会被吞掉。

## 发布、回滚与 E4

发布路径保持 `master -> release -> GitHub Actions Deploy Production`。Release Gate 必须确认实际 release 和镜像 commit、Alembic 版本、索引有效性，以及 backend、planner、dispatcher、account-online、recovery 和评论相关 worker 健康。

性能恢复至少需要连续三个真实 Planner / Dispatcher 周期满足：

- 完整事务均小于 60 秒；
- 不再出现本根因导致的 400 秒级阻塞事务和大行持续接收；
- AI 活群覆盖账本持续推进；
- 评论任务开始产生新的 Action、Attempt 与远端结果。

最终 `production_fixed` 仍必须由业务目标决定：4 个任务在同一北京时间自然日的完整 `2320` 项任务 × 群 × 账号矩阵均有 Telegram 远端成功证据；评论任务另有新 `Action.success + ExecutionAttempt.success + remote_message_id` 证据。任何 `unknown_after_send`、任务状态字段、worker 心跳或容器健康都不能冒充远端成功。

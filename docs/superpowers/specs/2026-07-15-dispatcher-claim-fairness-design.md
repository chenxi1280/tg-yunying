# Dispatcher 领取批次与评论防饥饿设计

## 1. 生产问题

生产有 4 个 Dispatcher，每个 worker 命令传入 `limit=100`，但数据库连接预算把单 worker 的真实并发限制为 13。当前实现先把最多 100 条 action 全部置为 `claiming`，再逐条检查账号、限流和运行资源。资源确认尚未处理到批次尾部时，统一的 60 秒 claim 已过期，Recovery 会把这些 action 恢复为 `pending` 并写入 `claim_expired`。

评论、点赞、浏览任务当前均为优先级 3。生产同时存在约千条到期点赞、近两百条浏览和 2 条到期评论；只按计划时间排序会让低量评论持续排在批量动作之后，即使修复 claim 过期也不能保证评论及时执行。

## 2. 方案比较与选择

### 方案 A：对齐领取量并增加评论排序（采用）

- 每轮领取量不超过本 worker 的真实 Dispatcher 并发能力。
- 保留 AI 活跃群每小时硬目标的现有最高排序。
- 在相同硬目标等级和 Task priority 下，`channel_comment` 的目标准入和 `post_comment` 先于普通批量动作，再按 `scheduled_at / created_at` 排序。

改动小、行为可验证，不改变任务配置、账号限额和 Telegram 风控规则。评论量低，不会显著挤压点赞和浏览吞吐。

### 方案 B：按任务类型轮询配额

每批对所有 task type 做 round-robin。公平性更强，但需要多次锁定查询或扩大候选集，跨 4 个 worker 的一致性和锁竞争更复杂，不适合作为本次生产快修。

### 方案 C：只延长 claim 租约

可以减少 `claim_expired`，但仍会一次占住 100 条运行资源，并且不能解决评论排在大批量动作后的饥饿，因此不采用。

## 3. 运行时设计

`_drain_task_dispatcher` 先计算 `_dispatcher_concurrency()`，再把 `min(requested_limit, effective_concurrency)` 传入 `claim_actions`。生产 PostgreSQL 路径的领取、确认和执行使用同一个有效并发值，避免“领取 100、执行 13”的不一致；SQLite 测试路径仍串行执行，保留数据库错误隔离测试语义。

`claim_actions` 和只读 `due_actions` 共享排序口径：

1. AI 活跃群过期硬目标和硬目标准入动作；
2. 当前小时硬目标发送；
3. 普通动作；
4. 同一层级内按 Task priority；
5. 同一优先级内 `channel_comment` 任务的目标准入和 `post_comment` 先于其他动作；
6. 最后按计划时间和创建时间稳定排序。

评论链路优先只影响已经到期的 action，不提前执行未来评论，不跳过账号在线、账号容量、Redis token bucket、账号 in-flight、目标权限或 Telegram Gateway 校验。

## 4. 失败与恢复

- 未取得运行资源的 action 继续显式释放为 `pending`，保留现有原因与重试时间。
- claim 真的超过租约时，Recovery 仍按现有规则写 `claim_expired`，不隐藏异常。
- 不延长 claim 租约，不新增静默 fallback，不把失败评论标记为成功。

## 5. 验收

### 自动化

- worker 请求 `limit=100`、有效并发为 13 时，`claim_actions` 实际收到 13。
- 到期普通动作更早、Task priority 相同时，到期 `post_comment` 先被领取。
- AI 群每小时硬目标仍先于评论。
- Dispatcher 既有执行、数据库错误隔离和 claim 回收测试通过。

### 生产 E4

- 线上 4 个 Dispatcher 健康，版本与发布提交一致。
- 新产生的 `claim_expired` 不再由单轮 100/实际 13 的批次不一致持续增长。
- 当前 2 条到期评论离开反复 `pending -> claiming -> claim_expired` 循环，进入 `success` 或暴露一个非 `claim_expired` 的真实 Telegram/账号失败。
- 评论成功必须以 action 的 `success`、`executed_at` 和真实 `telegram_msg_id` 为证据；worker 心跳不能替代成功证明。

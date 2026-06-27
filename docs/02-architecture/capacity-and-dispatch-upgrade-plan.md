# TG 运营管理平台容量与调度架构升级方案

> 本文用于承接 100 个账号向 1000 个账号规模演进的技术架构方案。  
> 评估口径基于当前仓库的静态代码结构与产品设计文档，不等同于线上压测结论。  
> 当前目标不是推翻现有系统，而是在已有 Task / Action / Account / Rule / RiskControl 模型上升级执行层、容量层、风控层和观测层。

---

## 1. 背景与目标

当前平台已经具备任务中心、监听中心、规则中心、风控中心、账号中心、运营目标中心和独立 worker 的基础能力。它可以支撑 100 个左右账号的运营试运行，但未来如果扩展到：

- 1000 个左右 TG 账号；
- 20 到 30 个持续运行任务；
- 多类任务并行，包括 AI 活跃群、转发监听、频道浏览、频道点赞、频道评论 / 回复；
- 大量账号按规则、风控、代理和目标权限参与执行；

就不能再依赖单 worker 顺序 drain 和单进程串行 Telegram API 调用。

升级目标：

- 支持 1000+ 账号的统一管理和分批调度。
- 支持 20-30 个持续任务长期运行。
- 支持多个 worker 安全并发领取执行项。
- 支持账号、代理、目标群、任务类型多维限流。
- 支持监听、任务规划、执行投递、失败恢复解耦。
- 支持运营人员在页面上看到任务为什么慢、为什么没发、哪里被限流、哪个账号或代理异常。
- 支持后续通过压测给出明确容量参数，而不是只做经验判断。

---

## 2. 当前架构判断

当前架构方向是正确的，但执行层还没有达到 1000 账号并发运营形态。

### 2.1 已有基础

当前系统已经具备以下可扩展基础：

- 生产部署中 API 与 worker 可以分离，队列优先使用 Redis。
- 任务中心已经形成 Task / Action 两层模型。
- Action 已有 `pending`、`executing`、`success`、`failed`、`skipped` 等状态。
- Action 已有 `lease_owner` 和 `lease_expires_at`，可以识别卡住的执行项并恢复。
- worker 已有 heartbeat 表，可记录执行进程状态。
- 账号容量检查已经包含账号冷却、小时上限和日上限。
- 监听运行层已经能按源群聚合采集，并把采集结果唤醒订阅任务。
- Telethon 网关已经有后台事件循环和 client cache，避免每个操作都从零创建连接。

这些基础说明平台不需要推翻重建。

### 2.2 当前瓶颈

当前主要瓶颈集中在执行吞吐和并发安全：

- worker 主循环按顺序 drain，慢任务会拖慢后续任务。
- 任务中心先选 due task，再逐个 build plan，再顺序 dispatch due action。
- 单个 action 调用 Telegram API 时会同步等待返回，慢 TG 请求会占住当前执行路径。
- 多 worker 横向扩容前缺少完整的数据库原子抢占机制。
- action lease 能恢复卡住状态，但不能替代并发领取时的 `claim` 语义。
- 监听采集、任务规划、Action 执行仍在同一个 drain 节奏里，规模变大后容易互相拖慢。
- 账号容量检查会频繁查询历史 action / message task，数据量上来后需要索引、聚合或快照支撑。
- 1000 个 Telethon client 如果集中在单进程，会带来内存、连接、代理、文件描述符和 event loop 压力。

结论：

```text
100 个账号：当前架构可继续迭代使用。
1000 个账号：需要先升级调度执行层，再谈稳定承载。
```

---

## 3. 目标执行架构

升级后的执行架构应拆成五类后台能力：

```text
Planner Worker
  负责扫描运行中任务，把 Task 拆成 Action。

Dispatcher Worker
  负责原子领取 pending Action，执行 Telegram API，并回写结果。

Listener Worker
  负责群、频道、讨论区、评论和上下文采集。

Recovery Worker
  负责执行超时、租约过期、worker 失联、失败重试和任务状态修复。

Metrics Worker
  负责队列积压、任务延迟、账号容量、代理健康和运营指标快照。
```

首期可以仍然复用一个 Python worker 入口，通过环境变量区分 worker 类型；不必一开始拆成多个仓库或服务。

### 3.1 Planner

Planner 只负责把任务拆成待执行 Action，不直接调用 Telegram API。

职责：

- 扫描 `running` 且 `next_run_at <= now` 的任务。
- 检查任务结束时间、每日上限、静默时间、下一轮调度时间。
- 检查全局和单任务 pending 积压，超过阈值时暂停规划或降低规划频率。
- 调用任务类型对应的 plan builder。
- 生成 pending action。
- 更新 task `next_run_at` 和统计快照。

约束：

- Planner 必须幂等，重复执行不能重复生成同一批 action。
- Planner 批次键统一采用 `plan_batch_key = task_id + 计划时间戳`。计划时间戳不是每次运行的当前时间，而是任务本轮规划的稳定时间戳，例如 `planned_slot_at`、`source_event_at`、`cycle_started_at` 或按任务节奏归一化后的 `scheduled_at`。
- Action 明细去重键统一采用 `action_dedupe_key = plan_batch_key + 业务维度`。不同任务类型必须追加自己的业务维度，例如频道任务追加 `message_id + action_type`，转发监听追加 `source_event_key + target_id`，AI 活跃群追加 `cycle_id + turn_index + account_role`。
- Planner 重跑时先按 `plan_batch_key` 判断本轮是否已经规划，再按 `action_dedupe_key` 补齐缺失 action；不能因为同一批次已存在而误拦同一窗口里的多条频道消息或多个源群事件。
- Planner 不持有长事务。
- Planner 不执行 TG API。
- Planner 不负责账号最终抢占，只做计划层账号建议。
- Planner 必须有积压保护：`max_pending_global`、`max_pending_per_task`、`oldest_pending_age_seconds` 任一超阈值时，本轮只更新任务调度状态或延后 `next_run_at`，不能继续无限生成 action 压垮数据库。

### 3.2 Dispatcher

Dispatcher 是 1000 账号规模的核心。

职责：

- 从数据库原子领取 due action。
- 给 action 写入 `executing`、`lease_owner`、`lease_expires_at`。
- 执行 Telegram API。
- 根据返回结果更新 action、account、task、risk event。
- 对 FloodWait、SlowMode、账号受限、代理异常、目标权限不足、内容拦截做分类处理。

领取流程必须拆成两段短事务，不能在数据库行锁事务里等待 Redis、代理探测或外部 API。

```text
阶段 1：DB 短事务预领取
BEGIN
  SELECT candidate actions
  JOIN tasks ON tasks.id = actions.task_id
  WHERE actions.status = 'pending'
    AND actions.scheduled_at <= now()
    AND tasks.status = 'running'
    AND tasks.deleted_at IS NULL
  ORDER BY tasks.priority ASC,
           actions.scheduled_at ASC,
           actions.created_at ASC
  FOR UPDATE SKIP LOCKED
  LIMIT claim_limit

  在 claim 阶段完成最终账号选择 / 转派 / 延后判断
  写入最终 account_id、claim_owner、claim_token、claim_expires_at

  UPDATE selected actions
  SET status = 'claiming',
      claim_owner = current_worker,
      claim_token = request_id,
      claim_expires_at = now() + claim_seconds
COMMIT

阶段 2：事务外获取运行资源
  获取 Redis token bucket quota reservation
  获取 account in-flight lock / semaphore
  获取 proxy / target / media 运行配额

阶段 3：DB 短事务确认执行
BEGIN
  UPDATE resource 通过的 actions
  SET status = 'executing',
      lease_owner = current_worker,
      lease_expires_at = now() + lease_seconds
  WHERE status = 'claiming'
    AND claim_owner = current_worker
    AND claim_token = request_id

  未拿到资源的 action 恢复为 pending，延后 scheduled_at，清空 claim 字段
COMMIT

并发执行已领取 actions
逐条短事务回写结果
```

要求：

- 多个 dispatcher worker 同时运行时不能重复执行同一条 action。
- 每个 worker 内部使用有界并发，例如 20-50。
- 并发度必须受账号、代理、目标和全局 Telegram API 限制约束。
- TG API 调用期间不持有数据库事务。
- claim 阶段必须按任务状态、任务优先级、任务类型权重、每任务 claim 配额、账号池公平性和账号容量来选择执行项。
- 账号转派必须在 claim 阶段完成。Dispatcher 领取 action 时确定最终 `account_id`；如果原账号不可用，先在同任务账号池中选择替代账号；如果没有可用账号，则延后 action，不进入 TG 调用阶段。
- 账号分片在 claim 前生效。若启用 `ACCOUNT_SHARD_TOTAL / ACCOUNT_SHARD_INDEX`，当前 worker 只能在自身账号分片内选择最终账号；本分片没有可用账号时直接延后 action，不能先跨分片转派再重新入队，避免反复 claim。
- claim 成功但 Redis token 未拿到时，action 不能进入 `executing`；必须保持 `pending` 并把 `scheduled_at` 延后到 token 可用时间，避免监控里的 `executing` 被限流等待污染。
- `claiming` 只是短暂预领取状态，不代表已经调用 TG；`claim_expires_at` 过期后由 Recovery 恢复为 `pending`。
- 同一 `account_id` 默认只能被一个 worker 同时使用。账号并发必须通过 Redis in-flight key、Redis semaphore 或数据库条件锁实现，不能只依赖 token bucket。

### 3.2.1 Claim 公平性与配置

Dispatcher 不能只按全局时间排序抢任务。claim 配置必须支持：

| 配置 | 说明 |
| --- | --- |
| `task_priority` | 任务优先级，数值越小越优先。 |
| `task_type_weight` | 任务类型权重，例如 AI 活跃、转发监听、频道互动可配置不同配额。 |
| `max_claim_per_task` | 单轮 claim 中每个任务最多领取多少 action，避免大任务吃满队列。 |
| `max_claim_per_account_pool` | 单轮 claim 中每个账号池最多领取多少 action，避免一个账号池长期占用。 |
| `account_fairness_window_seconds` | 账号公平窗口，窗口内已大量使用的账号降低优先级。 |
| `media_claim_ratio` | 媒体 action 在单轮 claim 中的占比，避免媒体任务拖慢文本任务。 |
| `min_claim_per_task_type` | 单轮 claim 中低优先级任务类型的最低保护配额，避免长期饥饿。 |
| `max_starvation_seconds` | action 等待超过该时间后触发 aging，逐步提升 claim 优先级。 |

配置归属：

- 全局默认值归入风控中心的全局调度策略。
- 任务可以覆盖部分节奏参数和账号池偏好。
- 任务级配置不能突破全局硬上限，例如全局账号小时上限、代理出口上限、全局 TG API 速率和媒体发送上限。

默认 claim 排序：

```text
任务优先级
  ↓
任务类型权重
  ↓
每任务 claim 配额
  ↓
账号池公平性
  ↓
等待时间 aging / 防饥饿
  ↓
action.scheduled_at
  ↓
action.created_at
```

### 3.2.2 执行尝试与结果未知状态

发送类 action 必须区分“明确失败可重试”和“已经调用 Telegram，但结果未知”。

新增执行尝试口径：

```text
execution_attempts
  id
  action_id
  worker_id
  account_id
  attempt_no
  status
  call_started_at
  before_call_at
  gateway_call_started_at
  after_call_at
  remote_message_id
  failure_type
  failure_detail
  result_snapshot
```

Action 状态建议增加或明确使用：

| 状态 | 含义 | 是否可自动重试 |
| --- | --- | --- |
| `pending` | 等待领取 | 是 |
| `claiming` | 已被 worker 短暂预领取，尚未拿齐 Redis / 账号运行资源 | 是，claim 超时后恢复 |
| `executing` | 已领取，尚未确认 TG 调用结果 | 否 |
| `unknown_after_send` | 已调用 TG，但本地回写失败、超时或结果未知 | 否，需人工或补偿确认 |
| `retryable_failed` | 明确失败且按策略允许重试 | 是 |
| `failed` | 明确失败且不再自动重试 | 否 |
| `success` | 明确成功 | 否 |
| `skipped` | 策略跳过 | 否 |

执行流程：

```text
claim action
  ↓
写 execution_attempts: before_call
  ↓
进入 Telegram Gateway 调用边界前写 gateway_call_started_at
  ↓
调用 Telegram Gateway
  ↓
如果 TG 返回明确成功：action = success，attempt = success
如果 TG 返回明确失败：action = retryable_failed / failed，attempt = failed
如果 gateway_call_started_at 已写入后进程崩溃、DB 回写失败或超时无法确认：
  进程本身不一定有机会写 action 状态
  Recovery 根据 executing action + 未完成 execution_attempt 推断为 unknown_after_send
  action = unknown_after_send
  attempt = result_unknown
如果仅写入 before_call 但尚未进入 Gateway 调用边界即崩溃：
  Recovery 不得推断 unknown_after_send
  action 恢复为 pending 或 retryable_failed
  attempt = call_not_started
```

Recovery 遇到 `unknown_after_send` 不得直接重发。系统必须先通过远端消息 ID、最近消息探测、人工确认或专门补偿任务判断是否已经发送，避免重复发言。

### 3.3 Listener

Listener 从任务执行链路中独立出来。

职责：

- 按群 / 频道 / 讨论区聚合采集。
- 多个任务订阅同一来源时，只采集一次。
- 采集结果写入消息快照或事件表。
- 任务从快照消费事件，不直接反复拉 TG。
- 监听失败只影响监听状态，不阻塞发送 dispatch。

监听维度：

- 源群消息。
- 目标群上下文。
- 频道新消息。
- 频道评论树。
- 频道回复。
- Reaction 或后续互动事件。

### 3.3.1 事件水位与唯一事件口径

Listener 独立后必须持久化事件水位，不能只依赖进程内采集窗口。

每个监听来源维护：

```text
listener_source_state
  source_type
  source_peer_id
  account_id
  shard_key
  lease_owner
  lease_expires_at
  last_remote_message_id
  last_event_at
  backfill_until
  collect_window_seconds
  last_error
```

事件唯一键：

```text
source_type + source_peer_id + remote_message_id + event_type
```

对于媒体相册或组合消息，需要增加 `media_group_id`；对于频道评论和回复，需要增加 `parent_message_id` 和 `comment_message_id`；对于编辑 / 删除事件，需要追加事件版本或事件动作。

Listener 必须处理：

- source claim：同一个 `source_type + source_peer_id + account_id` 同一时间只能由一个 Listener worker 采集；worker 通过 `lease_owner / lease_expires_at` 领取 source，避免多个 worker 重复拉取 TG 并互相覆盖水位。
- 回补窗口：每次采集允许向前回补少量消息，避免网络抖动漏采。
- 去重：写入事件前先按唯一键检查。
- bot 消息过滤：默认过滤 Telegram bot 来源消息，避免机器人消息触发转发和 AI 活跃。
- media group 聚合：同一相册不能拆成多条互相独立的转发事件。
- 编辑事件：默认记录为新事件版本，是否触发转发由任务规则决定。
- 删除事件：默认只记录状态，不主动撤回历史发送，后续可配置同步删除策略。

### 3.4 Recovery

Recovery 负责系统自愈。

职责：

- 扫描 lease 过期的 executing action。
- 扫描 worker heartbeat 失联的 action。
- 根据失败策略决定标记失败、延后重试、暂停任务或停止任务。
- 修复持续任务误完成、无 action 卡住、监听错误残留等状态。
- 记录恢复原因，方便运营和工程排查。

要求：

- Recovery 不能盲目重发已经可能成功的 action。
- 对发送类 action，超时恢复默认标记失败并进入既有重试策略，而不是直接再次发送。
- 恢复记录必须能在任务详情和运营数据中下钻。

### 3.5 Metrics

Metrics 负责把运行状态从“查日志”变成“页面可看懂”。

核心指标：

- pending action 数。
- executing action 数。
- 最老 pending 等待时间。
- 最近 5 分钟成功数、失败数、跳过数。
- TG API 平均耗时和 P95 耗时。
- FloodWait 次数。
- SlowMode 次数。
- 账号受限次数。
- 代理异常次数。
- worker heartbeat 状态。
- 每个任务的积压数量和最新错误。
- 每个账号的最近执行、冷却、限流、失败原因。

---

## 4. 账号容量与风控调度

1000 账号规模下，账号不是普通列表，而是调度资源。

### 4.1 账号进入执行前的检查链路

每条 action 执行前需要经过：

```text
账号状态
  ↓
账号 session 可用性
  ↓
开发者应用凭据
  ↓
代理绑定和代理健康
  ↓
账号小时 / 日限制
  ↓
账号冷却
  ↓
目标群 / 频道权限
  ↓
目标慢速模式
  ↓
规则中心内容校验
  ↓
Telegram Gateway
```

### 4.2 限流维度

建议至少保留这些限流维度：

| 维度 | 说明 |
| --- | --- |
| 全局并发 | 平台整体同时执行 TG API 的最大数量 |
| 任务并发 | 单个任务同时执行的 action 数 |
| 账号并发 | 同一个账号同一时间最多执行 1 个或少量 action |
| 账号小时上限 | 单账号每小时发送 / 互动动作上限 |
| 账号日上限 | 单账号每日发送 / 互动动作上限 |
| 账号冷却 | 单账号两次动作之间的最小间隔 |
| 代理并发 | 同一代理出口同时承载的账号动作数量 |
| 代理失败熔断 | 代理失败率过高时暂停使用 |
| 目标群慢速 | 目标群 slow mode 或群级冷却 |
| 任务类型节奏 | AI 活跃、转发、频道互动分别配置节奏 |

多 worker 限流统一使用 Redis token bucket。进程内锁只能作为单 worker 内部保护，不能作为跨 worker 的最终限流依据。

Redis key 约定：

```text
rate:global:tg_api
rate:task:{task_id}
rate:task_type:{task_type}
rate:account:{account_id}
rate:proxy:{proxy_id}
rate:target:{target_id}
rate:media
```

领取 action 后、调用 Telegram Gateway 前必须先获取对应 token。拿不到 token 时不调用 TG API，action 延后到 token 可用时间。

实现约束：

- token 获取必须使用 Redis 原子 Lua 脚本或事务，不能用非原子的读后写。
- Redis 不可用时默认 fail-closed：暂停 Dispatcher 对 Telegram Gateway 的调用，或只允许明确配置的极低保守速率；不能 fail-open 继续发送。
- token 获取发生在 action 进入 `executing` 之前；未拿到 token 的 action 保持 `pending` 并延后 `scheduled_at`。
- token 获取建议采用带 `request_id` 和 TTL 的 reservation。DB 条件更新进入 `executing` 成功后才确认消耗；如果 DB 更新失败或 worker 崩溃，reservation 自动过期释放。
- 如果短期实现只能直接消耗 token，也必须接受“保守浪费 token、不执行 action”的结果；不能因为 token 已扣减但 DB 更新失败而绕过 DB 状态直接调用 TG。
- 账号 in-flight lock 与 token bucket 分开处理：token bucket 管速率，in-flight lock 管同一账号是否正在被 worker 使用。

token bucket 参数由配置决定：

| 配置 | 说明 |
| --- | --- |
| `global_tg_rate_per_second` | 全平台 TG API 总速率。 |
| `task_rate_per_minute` | 单任务执行速率。 |
| `task_type_rate_per_minute` | 单任务类型执行速率。 |
| `account_rate_per_hour` | 单账号小时动作上限。 |
| `account_cooldown_seconds` | 单账号动作冷却。 |
| `proxy_rate_per_minute` | 单代理出口速率。 |
| `target_rate_per_minute` | 单目标群 / 频道速率。 |
| `media_rate_per_minute` | 媒体发送速率。 |

### 4.3 调度资源状态

建议形成运行时资源视图：

```text
account_id -> runtime_state
  status
  worker_id
  proxy_id
  last_action_at
  cooldown_until
  flood_wait_until
  daily_used
  hourly_used
  last_rpc_error
  health_score

proxy_id -> runtime_state
  status
  current_concurrency
  failure_rate
  cooldown_until

target_id -> runtime_state
  slowmode_until
  daily_used
  last_permission_error
```

短期可以从数据库实时查询得到，长期通过 Redis token bucket 和快照表提升性能。Redis 中的 token 状态负责实时限流，数据库快照负责运营展示和审计追溯。

---

## 5. Telegram Gateway 升级

当前 Telethon Gateway 的 client 生命周期已抽离到 `backend/app/telethon_lifecycle.py`。首期增强包括：

- 后台 event loop 统一管理。
- `api_id + session` 维度 client cache。
- `TELETHON_CLIENT_CACHE_SIZE` 控制单进程最大 client 数。
- `TELETHON_CLIENT_IDLE_SECONDS` 控制 idle 释放。
- 连接超时和业务操作超时参数化。
- FastAPI shutdown 时统一 disconnect。

1000 账号阶段仍需要继续用真实压测校准 cache size、idle TTL、文件描述符、代理出口和 shard 范围。

### 5.1 Client 分片

不要让单个进程持有全部账号 client。

建议：

- 按账号 ID hash 分配到 dispatcher worker。
- 或按账号池 / 代理池分配 worker。
- worker 只加载自己负责账号的 client。
- client cache 设置容量上限和空闲回收时间。
- worker 停止时优雅断开 client。

### 5.2 错误分类

Telegram 错误必须转成稳定业务状态：

| Telegram 情况 | 平台动作 |
| --- | --- |
| FloodWait | 账号冷却到指定时间，action 延后或失败重试 |
| SlowMode | 目标群冷却，任务延后 |
| ChatWriteForbidden | 标记目标权限异常，必要时暂停目标 |
| PeerInvalid | 标记目标不可用，提示重新同步 |
| UserDeactivated / Banned | 标记账号受限或不可用 |
| 代理连接失败 | 标记代理异常，不直接误判账号 |
| session 失效 | 标记账号需重新登录 |

### 5.3 媒体发送

媒体发送比文本发送更重，需要单独限制：

- 媒体下载 / 重传并发上限。
- TG 缓存引用失效时进入素材回退。
- 媒体失败不应阻塞普通文本发送队列。
- 大文件任务进入低优先级队列。

---

## 6. 数据库与索引优化

1000 账号规模下，数据库压力主要来自 action 查询、容量统计和任务详情统计。

### 6.1 必要索引

建议确认或补充：

| 表 | 索引 | 用途 |
| --- | --- | --- |
| `actions` | `(status, scheduled_at, created_at)` | due action claim |
| `actions` | `(task_id, status)` | 任务详情统计 |
| `actions` | `(tenant_id, account_id, status, scheduled_at)` | 账号容量统计 |
| `actions` | `(lease_owner, lease_expires_at)` | worker 恢复 |
| `actions` | `UNIQUE (tenant_id, action_dedupe_key)` | action 幂等去重 |
| `actions` | `(status, claim_expires_at)` | claiming 超时恢复 |
| `execution_attempts` | `UNIQUE (action_id, attempt_no)` | 执行尝试幂等 |
| `listener_events` | `UNIQUE (tenant_id, unique_event_key)` | 监听事件去重 |
| `listener_source_state` | `(shard_key, lease_expires_at)` | listener source claim |
| `daily_runtime_stats` | `UNIQUE (stat_date, dimension_type, dimension_id, metric_name)` | 日汇总幂等 upsert |
| `tasks` | `(status, next_run_at, priority)` | planner 扫描 |
| `worker_heartbeats` | `(process_type, last_seen_at)` | worker 健康 |
| `group_context_messages` | `(group_id, sent_at)` | 群上下文 |
| `channel_messages` | `(tenant_id, channel_target_id, message_id)` | 频道消息去重 |
| `message_fingerprints` | `(source_group_id, fingerprint)` | 转发去重 |

### 6.2 统计快照

任务列表不要依赖大量实时 count。

建议分阶段：

1. 先保留实时统计，但补索引。
2. 增加 task runtime stats 快照。
3. 增加 account runtime stats 快照。
4. 前端列表优先读快照，详情页再按需查明细。

### 6.3 数据库连接池与并发公式

worker 并发不能只看 TG API 吞吐，还必须受 PostgreSQL 连接池约束。

容量公式：

```text
api_pool
+ planner_worker_count * planner_db_connections
+ dispatcher_worker_count * dispatcher_db_connections_per_worker
+ listener_worker_count * listener_db_connections_per_worker
+ recovery_worker_count * recovery_db_connections_per_worker
+ metrics_worker_count * metrics_db_connections_per_worker
+ reserved_admin_connections
< postgres_max_connections
```

Dispatcher 单 worker 数据库连接估算：

```text
dispatcher_db_connections_per_worker
= min(DISPATCHER_CONCURRENCY, DB_WRITEBACK_CONCURRENCY)
  + claim_connection
  + metrics_connection
```

原则：

- TG API 调用期间不持有数据库连接。
- claim 使用 1 个短事务连接。
- 并发回写要有 `DB_WRITEBACK_CONCURRENCY` 上限，不能等于无限并发。
- API 服务、后台 worker、迁移脚本和人工排查必须预留连接。
- 压测输出必须同时包含 TG 吞吐和数据库连接池等待时间。

### 6.4 数据保留与分区策略

当前项目尚未上线，不需要设计复杂历史迁移；但新执行架构必须从一开始带数据保留策略。

保留规则：

```text
运行明细数据默认保留最近 5 个自然日，第 6 天滚动删除第 1 天的全部运行明细。
```

5 天清理不是一次性全表清空，而是按天滚动清理：

```text
第 1 天产生明细
第 2-5 天继续保留第 1 天明细
第 6 天先把第 1 天数据汇总成总数，再删除第 1 天全部运行明细
第 7 天同理处理第 2 天明细
```

清理日期按系统业务时区的自然日计算，默认使用 Asia/Shanghai。删除前必须先确认日汇总总数已经刷新，删除过程中写入清理审计。超过 5 天窗口后，不再为了未闭环、未知结果或人工待处理保留单条运行明细。

清理后长期只保留汇总总数，不保留单条运行明细。汇总口径至少包括：

- 按任务的成功、失败、跳过、未知、重试和发送总数。
- 按账号的发送总数、成功数、失败数、FloodWait 次数、受限次数。
- 按目标的发送总数、成功数、失败数、慢速模式命中次数。
- 按任务类型的执行总数、成功数、失败数和跳过数。
- 按日期的全局执行总数、TG API 调用总数、媒体发送总数。
- 按状态的窗口外删除总数、未知总数、未闭环总数、清理时仍待处理总数。

5 天窗口内需要保留并处理的状态：

- `success`
- `failed`
- `skipped`
- `pending`
- `executing`
- `retryable_failed`
- `unknown_after_send`

5 天清理对象：

- `actions` 执行明细。
- `execution_attempts` 执行尝试明细。
- listener 原始事件和采集快照。
- group context 临时上下文。
- worker heartbeat 历史。
- metrics runtime snapshots。
- 临时规则命中明细和转发批次运行明细。

不按 5 天删除的对象：

- 账号、账号池、代理配置。
- 运营目标。
- 任务定义和任务配置。
- 规则集和规则版本。
- 风控策略配置。
- 素材库配置。
- 必须长期留存的审计摘要。
- 任务 / 账号 / 目标 / 任务类型 / 日期维度的日汇总总数。
- 清理任务审计，包括清理日期、删除行数和按状态汇总的删除总数。

实现建议：

- 明细表按 `created_at` 或 `executed_at` 建索引。
- 数据量上来后按天分区。
- 清理任务每天运行，按自然日滚动删除 5 天窗口外的全部运行明细。
- 清理前先刷新任务 / 账号 / 目标 / 任务类型 / 全局日期的日汇总总数，保证删明细后仍能看到长期总量。
- 日汇总写入必须幂等，按 `stat_date + dimension_type + dimension_id + metric_name` upsert，清理任务重跑不能重复加总。
- 清理任务要记录每次删除的日期分区、删除行数、按状态删除总数和汇总刷新版本。
- 5 天窗口外不保留单条异常列表；运营页面只展示汇总后的未知总数、未闭环总数和清理审计。

---

## 7. P1 实施前置约束

P1 开工前必须先确认以下约束，避免实现时重新返工。

1. `plan_batch_key` 和 `action_dedupe_key` 必须同时落地。`plan_batch_key = task_id + 计划时间戳` 只表示一轮规划批次；`action_dedupe_key` 才负责防止单条 action 重复。
2. claim 前先按当前 worker 账号分片筛选可用账号池，claim 阶段只在本分片内完成最终账号选择和账号转派；本分片无可用账号时延后 action。
3. Redis token bucket 必须在 action 进入 `executing` 前获取；未拿到 token 的 action 保持 `pending` 并延后 `scheduled_at`。
4. Redis token bucket 必须使用原子 Lua 或事务；Redis 不可用时默认 fail-closed，不允许 fail-open 继续发送。
5. `execution_attempts` 必须在调用 Telegram Gateway 前写入 `call_started_at / before_call_at`；Recovery 根据未完成 attempt 推断 `unknown_after_send`。
6. `unknown_after_send` 不允许自动重发，只能通过远端探测、人工确认或补偿任务闭环。
7. 5 天清理按自然日滚动执行，第 6 天先汇总第 1 天总数，再删除第 1 天全部运行明细；长期只保留日汇总总数和清理审计，不再保留窗口外单条异常明细。
8. claim 公平性配置归入风控中心全局调度策略，任务级配置只能收窄或调整偏好，不能突破全局硬上限。
9. claim 必须两段式短事务实现，Redis token、账号 in-flight lock、代理和目标配额都不能放在 `FOR UPDATE SKIP LOCKED` 的数据库行锁事务里等待。
10. `unknown_after_send` 只能由已经写入 `gateway_call_started_at` 的 attempt 推断；尚未进入 Gateway 调用边界的 attempt 只能恢复为 `pending` 或明确失败。
11. Planner 必须有全局和单任务 pending 积压保护，超过阈值时停止继续生成 action。
12. action、listener event、execution attempt 和 daily stats 必须有数据库唯一约束支撑幂等。

---

## 8. 前端产品升级

架构升级必须让运营人员看得懂。

### 7.1 任务中心

任务列表新增：

- 待执行数。
- 执行中数。
- 最老待执行等待时间。
- 最近执行时间。
- 下一轮执行时间。
- 最近失败原因。
- 使用账号数。
- 不可用账号数。
- 风控拦截数。
- 规则命中数。

任务详情新增：

- Action 队列明细。
- 账号执行分布。
- 目标执行分布。
- 失败原因聚合。
- 重试和延后原因。
- lease / worker 信息。

### 7.2 账号中心

账号列表新增运行状态：

- 当前是否被 worker 使用。
- 冷却到什么时候。
- FloodWait 到什么时候。
- 今日已用量。
- 小时已用量。
- 最近执行任务。
- 最近 Telegram 错误。
- 绑定代理状态。

### 7.3 Worker 运行面板

运营概览或系统设置中增加 Worker 状态：

- worker ID。
- worker 类型。
- 最近心跳。
- 当前 claim 数。
- 成功 / 失败 / 超时数。
- 负责账号范围。
- 最近错误。

### 7.4 风控中心

风控中心展示：

- 账号限流命中。
- 代理熔断。
- 目标慢速模式。
- FloodWait 聚合。
- 自动转派记录。
- 延后执行记录。
- 任务暂停 / 停止策略命中。

---

## 9. 部署与扩容建议

短期仍可使用 Docker Compose，但 worker 应按类型和并发拆开。

推荐生产结构：

```text
backend api x 1-2
planner worker x 1
dispatcher worker x N
listener worker x N
recovery worker x 1
metrics worker x 1
postgres
redis
```

关键环境参数：

```text
WORKER_ROLE=dispatcher
DISPATCHER_CONCURRENCY=30
ACTION_CLAIM_LIMIT=100
ACTION_LEASE_SECONDS=1800
ACCOUNT_SHARD_TOTAL=4
ACCOUNT_SHARD_INDEX=0
LISTENER_CLAIM_LIMIT=50
LISTENER_CONCURRENCY=10
RECOVERY_INTERVAL_SECONDS=30
METRICS_INTERVAL_SECONDS=15
```

扩容原则：

- 先保证数据库原子 claim，再增加 dispatcher 数量。
- 先限制单账号和单代理并发，再提高 worker 并发。
- 监听和发送分开扩容。
- 媒体发送和文本发送分开限流。
- 不用简单提高单 worker `limit` 代替架构升级。

---

## 10. 实施分期

当前项目尚未上线，本轮不设计线上迁移、灰度回滚和旧 worker 兼容切换方案；实施时直接按开发环境和当前主线架构推进。

### P0：容量升级设计落档

目标：

```text
统一团队对 1000 账号架构的理解，明确不推翻现有模型。
```

交付：

- 本方案文档。
- `docs/01-product/tg-ops-platform.md` 同步容量升级口径。
- 明确第一阶段先做 Action claim 与 Dispatcher，而不是先重做前端或重写 Telethon。

验收：

- 文档清楚说明当前瓶颈、目标架构和实施顺序。

### P1：Action 原子领取与多 worker 安全执行

目标：

```text
允许多个 dispatcher worker 并行执行，不重复执行 action。
```

改造内容：

- 新增 action claim 服务。
- 使用 `FOR UPDATE SKIP LOCKED` 原子预领取 pending action，claim 条件必须包含任务状态、任务未删除、任务优先级和 action 到期时间。
- claim 改为两段式短事务：DB 预领取写入 `claiming`、`claim_owner`、`claim_token`、`claim_expires_at`，事务外获取 Redis token bucket reservation、账号 in-flight lock、代理和目标配额，再通过条件更新写入 `executing`、`lease_owner`、`lease_expires_at`。
- claim 阶段提前完成最终账号选择和账号转派。
- claim 阶段按每任务 claim 配额、任务优先级、账号池公平性、任务类型权重和等待时间 aging 分配执行项。
- 落地 `plan_batch_key` 和 `action_dedupe_key`，避免 Planner 重跑造成重复 action。
- 新增 `execution_attempts` 或等价执行尝试记录，并用 `gateway_call_started_at` 明确是否已经进入 Telegram Gateway 调用边界。
- 新增数据库唯一约束，覆盖 action 幂等、listener event 去重、execution attempt 幂等和 daily stats 幂等汇总。
- Planner 增加 pending 积压保护，超过全局或单任务阈值时暂停继续生成 action。
- dispatch 只执行已经 claim 到的 action。
- 补多 worker 并发测试。

验收：

- 同时启动 2-4 个 dispatcher worker。
- 同一批 pending action 不会重复执行。
- worker 异常退出后，lease 过期 action 能被 recovery 标记并走重试策略。
- 已调用 TG 但结果未知的 action 进入 `unknown_after_send`，不会被自动重复发送。
- Redis 不可用时 Dispatcher 不会 fail-open 继续发送。
- 同一账号不会被多个 worker 同时使用。

### P2：Dispatcher 有界并发与限流

目标：

```text
提升吞吐，同时保护账号、代理和目标。
```

改造内容：

- worker 内部并发池。
- Redis token bucket 跨 worker 限流。
- 账号 in-flight lock / semaphore。
- 账号级 token bucket。
- 代理级 token bucket。
- 任务级、任务类型级和全局 TG API token bucket。
- 目标群 slowmode / 冷却约束。
- FloodWait 自动冷却。
- dispatch 结果分类指标。
- 数据库连接池与 worker 并发公式配置。

验收：

- 单 worker 可并发执行 action。
- 同一账号不会被并发滥用。
- FloodWait 后账号自动冷却。
- 代理异常不会误伤账号。
- worker 并发提升后，PostgreSQL 连接池等待时间在可接受范围内。

### P3：Listener 独立化

目标：

```text
监听采集不再拖慢任务发送。
```

改造内容：

- 独立 listener worker。
- listener source claim。
- 群 / 频道采集窗口持久化。
- 监听来源水位持久化。
- 事件唯一键。
- 回补窗口、bot 消息过滤、media group 聚合、编辑 / 删除事件口径。
- 监听状态面板。
- 采集失败和发送失败分开统计。

验收：

- 同一个源群在一个窗口内只采集一次。
- 多任务共享同一批监听事件。
- 监听压力大时，dispatcher 仍能继续执行发送 action。
- 重复采集不会产生重复事件，短暂采集失败后可按水位回补。

### P4：运行指标与前端可观测

目标：

```text
运营人员能在页面判断任务为什么慢、为什么没发。
```

改造内容：

- pending / executing / oldest_pending_seconds 指标。
- worker 心跳面板。
- 任务积压面板。
- 账号运行状态。
- 风控命中下钻。
- 代理异常下钻。

验收：

- 任务详情能看到执行瓶颈。
- 账号详情能看到冷却、限流、FloodWait、最近错误。
- 运营概览能看到 worker 是否失联和队列是否积压。

### P5：1000 账号压测与参数固化

目标：

```text
用数据确认容量边界。
```

已新增首期容量模型脚本 `backend/scripts/run_capacity_benchmark.py`，并生成 `docs/02-architecture/reports/capacity-report-100-300-1000.md` / `reports/capacity/latest.json`。该模型覆盖以下压测内容：

- 1000 个模拟账号。
- 20-30 个持续任务。
- 不同任务类型混合。
- 大量 pending action claim。
- 多 dispatcher 并行执行。
- Redis token bucket 限流。
- listener 与 dispatcher 同时运行。
- 数据库容量统计查询。
- 任务列表和详情查询。
- 5 天数据清理任务。

输出：

- 推荐 worker 数量。
- 推荐 dispatcher 并发。
- 推荐 claim limit。
- 推荐 PostgreSQL 连接池。
- 推荐 Redis 参数。
- 单机容量边界。
- 扩容方案。

验收：

- 输出明确容量口径，例如：在指定机器配置和参数下，每分钟可稳定处理多少 action，P95 延迟是多少，队列最大积压可接受范围是多少。

---

## 11. 第一阶段建议

第一刀不要先大改 Telethon，也不要先做复杂前端。

最优先做：

```text
Action 原子领取
  ↓
Dispatcher 多 worker 安全执行
  ↓
基础队列指标
  ↓
再做有界并发和限流
```

原因：

- 这是 1000 账号架构的地基。
- 现有 Task / Action 模型可以直接承接。
- 风险比重写任务中心小。
- 做完以后才能安全横向扩 worker。
- 后续账号分片、代理限流、前端观测和压测都依赖这个基础。

最终判断：

```text
当前平台具备扩展到 1000 账号的模型基础，
但必须先把执行层从顺序 drain 升级为原子 claim + 多 worker + 有界并发 + 多维限流。
```

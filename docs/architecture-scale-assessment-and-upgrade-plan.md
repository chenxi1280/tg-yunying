# TG 运营管理平台架构容量评估与升级优化方案

> 评估日期：2026-05-16  
> 评估口径：基于当前代码静态审计，不等同于线上压测结果。  
> 业务目标：当前支撑约 100 个账号运营，未来演进到约 1000 个账号、20-30 个持续任务并行运营。

---

## 1. 结论先行

当前架构方向是对的，不需要推翻重做。系统已经有 `Task -> Action`、独立 worker、PostgreSQL、Redis 队列、Action claim、执行租约、worker heartbeat、Redis token bucket、账号容量检查和监听运行层这些扩展基础。

但如果按当前生产部署方式，只启动一个 `worker` 容器，并且让它在同一个 drain 循环里处理账号同步、监听采集、素材缓存、旧任务兼容、任务规划和 Action 投递，那么它可以支撑 100 个账号左右的试运行，不建议直接承诺稳定支撑 1000 个账号和 20-30 个持续任务。

1000 账号规模真正要看的不是账号表能不能存 1000 条，而是：

- 每分钟会生成多少 Action。
- 最老 pending Action 等待多久。
- Telegram API 平均和 P95 延迟是多少。
- FloodWait、SlowMode、账号受限、代理异常是否被限流和隔离。
- worker 能否横向扩容且不重复执行。
- 数据库连接池是否被 worker 并发耗尽。
- 监听采集是否会拖慢发送投递。

建议判断：

| 规模 | 当前状态判断 | 原因 |
| --- | --- | --- |
| 100 个账号 | 可以继续作为试运行目标 | 已有任务中心、队列、租约、限流、容量检查和基本恢复能力 |
| 300-500 个账号 | 需要先完成 worker 角色拆分和有界并发 | 单 worker 串行 drain 会出现积压和互相拖慢 |
| 1000 个账号 | 需要执行层升级后再承诺 | 需要多 Dispatcher worker、多 Listener worker、连接池预算、运行指标和压测参数 |

---

## 2. 当前代码里的可扩展基础

### 2.1 任务模型基础

当前系统已经不是简单定时器直接发消息，而是有任务中心模型：

- `backend/app/models/task_center.py` 定义了 `tasks`、`actions`、`execution_attempts`、`listener_source_state`、`worker_heartbeats`。
- `actions` 已经有 `pending`、`claiming`、`executing`、成功、失败、跳过等运行状态。
- `actions` 已经有 `lease_owner`、`lease_expires_at`，可用于执行超时恢复。
- `actions` 已经有 `claim_owner`、`claim_token`、`claim_expires_at`，为多 worker claim 打基础。
- `execution_attempts` 已经记录 gateway 调用边界，能区分“没开始调用”和“已调用但结果未知”。

这说明后续重点应该是补齐执行层，而不是重写产品模型。

### 2.2 Dispatcher 基础

`backend/app/services/task_center/dispatcher.py` 已经具备一些关键能力：

- `claim_actions(...)` 使用两阶段 claim。
- PostgreSQL 场景下使用 `FOR UPDATE SKIP LOCKED`。
- claim 成功后才进入 `executing`。
- Redis token bucket 可以做全局、任务、任务类型、账号、代理、目标和媒体限流。
- Redis 不可用时默认 fail-closed。
- 单进程内已有账号 in-flight 保护。

这是很重要的进展，说明 P1 的核心方向已经开始落地。

### 2.3 Recovery 和可观测基础

`backend/app/services/task_center/service.py` 里已经有：

- `record_worker_heartbeat(...)`。
- `recover_expired_claims(...)`。
- `_recover_stale_executing_actions(...)`。
- `_planning_backlog_blocked(...)`。
- `max_pending_global`、`max_pending_per_task`、`oldest_pending_age_seconds` 这些积压保护配置。

这些能力可以避免系统无限生成 Action，也可以在 worker 失联后做恢复。

### 2.4 监听运行层基础

`backend/app/services/task_center/listener_runtime.py` 已经把监听源按群聚合，并有 `listener_source_state` 的 lease 概念，避免同一个源群被多个采集流程重复拉取。

这说明监听也已经有可拆分成独立 worker 的基础。

---

## 3. 当前主要风险

### 3.1 worker 入口仍是串行总线

`backend/app/worker.py` 的 `drain_once(...)` 在一个循环里依次处理：

- message task 队列；
- 账号资料同步；
- 账号资源同步；
- group listener；
- source media cache；
- material cache；
- legacy campaign；
- legacy operation task；
- task center；
- archives；
- 临时文件清理。

这会带来一个实际问题：任何一个 drain 阶段慢了，后面的阶段都会被拖慢。100 个账号时还能靠循环频率消化，1000 个账号时会变成积压来源。

### 3.2 claim 有了，但有界并发还没真正用起来

配置里已经有：

- `WORKER_ROLE`
- `DISPATCHER_CONCURRENCY`
- `ACCOUNT_SHARD_TOTAL`
- `ACCOUNT_SHARD_INDEX`

但当前代码里没有看到这些配置真正控制 worker 角色、Dispatcher 并发池和账号分片。`drain_task_center(...)` claim 到 actions 后仍然是：

```text
for action in claimed:
    dispatch_action(session, action)
```

也就是说，claim 机制已经具备横向扩容基础，但单 worker 内部吞吐还没有释放，worker 角色也还没有完成隔离。

### 3.3 单账号 in-flight 目前主要是进程内保护

`dispatcher.py` 中 `_IN_FLIGHT_ACCOUNTS` 是进程内集合，只能保护同一个 worker 进程。数据库唯一索引 `uq_actions_executing_account` 能兜住跨进程同账号 executing，但如果未来要让多个 worker 高并发跑，还需要明确：

- 是否允许同账号并发执行不同安全动作；
- 如果不允许，跨进程 in-flight 应使用 Redis semaphore 或数据库条件锁；
- 账号分片要在 claim 前生效，避免 worker 反复抢到自己不能执行的账号。

### 3.4 监听、规划、执行还没有物理隔离

监听采集和发送投递的资源特点不同：

- 监听更像拉取上下文和源消息，容易受 TG 读取延迟影响。
- Dispatcher 更像发送、点赞、评论和回写结果，受 FloodWait、SlowMode、代理和目标权限影响。
- Planner 更像数据库规划和去重，不应该等待 TG 网络。

代码层已经支持按 role drain，但部署层仍需要确保生产实际启动 planner / dispatcher / listener / recovery / metrics 等独立 worker。规模扩大后，不能再把所有角色长期放在一个综合 worker 节奏里运行。

### 3.5 大文件和多职责文件仍然存在

当前代码已经比早期拆分过，但仍有明显的大文件和多职责文件。

| 文件 | 当前行数 | 本轮拆分状态 | 仍需收敛 |
| --- | ---: | --- | --- |
| `backend/app/services/operations_center.py` | 780 | 已抽默认规则、监听域、风控指标、规则指标、工具、规则集版本管理 | 规则测试和部分运营聚合仍可继续拆 |
| `frontend/src/app/context.tsx` | 797 | 已抽默认值、刷新、pending action、认证、账号、消息、系统配置、素材/关键词和 modal 编排动作 | 后续只保留 Provider 装配、状态声明和轻量派生值 |
| `backend/app/services/task_center/service.py` | 960 | 已抽字段映射、预检、详情、工具、reviews、stats、配置归一化 | CRUD、planner、recovery、role drain 仍可继续分域 |
| `backend/app/integrations/telegram/gateway.py` | 943 | 已抽契约、Mock、Telethon 生命周期、内容采集、媒体发送、目标解析；旧 `app.gateways` 兼容出口已删除 | Telethon login、profile、channel action 仍可继续拆 |
| `frontend/src/app/views/TaskCenterView.tsx` | 873 | 已抽 view-model、创建/编辑向导、详情弹窗 | 列表交互和 action table columns 仍可继续组件化 |
| `frontend/src/app/views/RulesCenterView.tsx` | 892 | 已抽 `RulesCenterConfig.tsx` | 规则列表、测试器、发布面板仍可继续拆 |
| `backend/app/services/task_center/dispatcher.py` | 727 | 已抽 `runtime_resources.py` | claim、结果处理、gateway 边界仍可继续分文件 |
| `backend/app/services/operations.py` | 1524 | 本轮未处理 | 旧运营任务逻辑和新运营目标/任务逻辑边界需要继续收敛 |
| `frontend/src/app/types/` | 最大文件 275 | 已按 system/accounts/risk/content/messaging/archives/operations/taskCenter/ui 拆分，`../types` 导入保持兼容 | 后续新增类型进入对应 domain 文件 |

这些文件不是马上导致系统不能跑的根因，但会让后续 1000 账号能力建设变慢，尤其是任务中心和 gateway。本轮已经完成第一轮 P6 边界拆分并通过构建/回归；`context.tsx`、`operations.py` 仍不应继续堆新逻辑，后续新增能力要进入对应 domain 模块。

### 3.6 同类“已设计但未完全生效”清单

本轮重点复核了是否存在“配置、模型、文档或测试已经出现，但真实运行路径还没有完全接上”的情况。结论如下：

| 能力 | 当前状态 | 已有基础 | 缺口 | 实施优先级 |
| --- | --- | --- | --- | --- |
| Worker 角色拆分 | 代码已落地，部署需确认 | `app.worker --role`、`WORKER_ROLE`、手动 drain role 参数、role 测试已存在 | 生产 compose / 守护进程需要按 role 启动并监控 | P1 |
| Planner / Dispatcher / Listener / Recovery / Metrics 分工 | 代码已分流，service 主文件仍偏集中 | `drain_task_planner/dispatcher/listener/recovery/metrics` 已存在 | 继续把 role drain 从 `service.py` 拆到独立模块，部署侧做物理隔离 | P1 |
| Dispatcher 有界并发 | 已接入，需压测验证 | `DISPATCHER_CONCURRENCY`、线程池并发、账号 in-flight 保护已接入 | 需要 PostgreSQL / Redis / 真实任务链路压测确认连接池和吞吐边界 | P2 |
| Action 原子 claim | 已落地 | `claiming`、`claim_owner`、`claim_token`、`FOR UPDATE SKIP LOCKED`、claim 超时恢复、测试覆盖、账号分片过滤已存在 | 继续优化公平调度和观测指标 | P1-P2 |
| Redis token bucket | 已部分落地 | 全局、任务、任务类型、账号、代理、目标、媒体限流 key 已有，Redis 不可用默认 fail-closed | 当前实现是“立即消耗 token + reservation 标记”，释放 reservation 只删除标记，不回滚 token；这比 fail-open 安全，但不是完整 quota reservation / confirm / refund 语义 | P2 |
| PostgreSQL 连接池参数化 | 已初步落地 | `database.py` 已按 `DB_POOL_SIZE`、`DB_MAX_OVERFLOW`、`DB_POOL_TIMEOUT`、`DB_POOL_RECYCLE` 配置 PostgreSQL engine | 仍需把连接预算写入部署参数和 Dispatcher 并发验收，避免 worker 数放大后耗尽连接 | P2 |
| 跨进程账号 in-flight | 部分兜底 | 进程内 `_IN_FLIGHT_ACCOUNTS`，数据库唯一索引 `uq_actions_executing_account`，冲突后恢复 pending | 没有 Redis semaphore / lock；多 worker 下会靠 DB 唯一约束挡冲突，吞吐和可观测性不如 claim 前锁定 | P3 |
| 账号分片 | 已接入 claim 路径 | `ACCOUNT_SHARD_TOTAL`、`ACCOUNT_SHARD_INDEX` 配置和 claim 过滤已存在 | 扩缩 shard 时需要运维流程和压测报告 | P3 |
| Listener 独立化 | role 路径已具备，部署需确认 | `listener_source_state`、source lease、同窗口采集去重、listener role 已存在 | 生产需独立 listener worker，避免回退到综合 worker | P4 |
| Metrics Worker | 基础已落地，指标仍可增强 | metrics role、heartbeat、运行数据查询、daily runtime stats 已存在 | 继续补队列深度、oldest pending、TG 延迟、FloodWait、DB 等待等快照指标 | P5 |
| Worker heartbeat 按角色展示 | 已接入角色写入 | `worker_heartbeats.process_type` 写入真实 role | 继续在运营概览强化按 role 展示和告警 | P1 |
| Embedded worker | 仍是综合 worker | 开发环境可随 API 启动 embedded worker | embedded worker 调用同一个 `run_worker()`，角色拆分后必须限制为 dev-only all 或明确 role，生产必须保持禁用 | P1 |
| 手动 drain endpoint | 仍是综合 drain | `/api/worker/drain-once` 仅非生产可用 | 角色拆分后需要支持 dev/test 指定 role，避免调试时误触发所有 drain | P1 |

因此，后续实施不能只按“新增功能”理解，而是要把已有半成品能力接成真实运行闭环。

---

## 4. 目标架构

建议把后台执行拆成五类 worker 角色，第一阶段仍可共用同一个 Python 包和镜像，只通过命令或环境变量切换角色。

```text
Backend API
  只处理管理后台 API、鉴权、配置、查询、任务创建和人工操作。

Planner Worker
  扫描 running 任务，根据规则、节奏、目标和账号池生成 pending Action。

Dispatcher Worker
  原子领取 pending Action，执行 TG API，回写 Action / Attempt / Task / Account / Risk 状态。

Listener Worker
  采集群、频道、讨论区和源消息上下文，维护监听水位，唤醒订阅任务。

Recovery Worker
  恢复 claim 超时、lease 超时、worker 失联、结果未知和可重试失败。

Metrics Worker
  聚合 pending 深度、最老等待时间、账号容量、FloodWait、代理健康、worker 心跳。
```

关键原则：

- Planner 不调用 TG。
- Dispatcher 不规划任务。
- Listener 不发送消息。
- Recovery 不生成新业务动作，只做恢复和状态修复。
- Metrics 不影响业务执行，只做快照和告警。

---

## 5. 分阶段升级方案

### P0：当前态校准和压测口径

目标：先把容量问题从“感觉能不能撑”变成可测量问题。

动作：

- 固化指标：pending action 数、oldest pending age、每分钟成功/失败 Action、TG API P50/P95、FloodWait 次数、worker heartbeat、DB 连接池等待。
- 新增开发压测脚本或 seed 脚本，模拟 100、300、1000 账号和 20-30 任务。
- 明确不同任务类型的动作量模型：AI 活跃群、转发监听、频道浏览、频道点赞、频道评论不能用同一个 QPS 口径。

验收：

- 能回答“当前配置下每分钟能稳定消化多少 Action”。
- 能看到积压从什么时候开始增长。

### P1：worker 角色拆分

目标：先把互相拖慢的问题拆开。

动作：

- `app.worker` 增加 `--role`，默认读取 `WORKER_ROLE`，支持 `all`、`legacy`、`planner`、`dispatcher`、`listener`、`recovery`、`metrics`。
- `run_worker()` 和 `drain_once()` 增加 role 参数；保留 `all` 兼容开发环境，但生产 compose 不使用 `all`。
- 从 `drain_task_center()` 中拆出可单独调用的 role drain：
  - `drain_task_planner(...)`：只做任务激活、停止条件、失败重试、积压保护、build plan、next_run_at。
  - `drain_task_dispatcher(...)`：只做 claim actions、dispatch actions、回写任务统计。
  - `drain_task_recovery(...)`：只做 expired claim、stale executing、review 过期、runtime retention。
  - `drain_task_listener(...)`：只调用 listener runtime。
  - `drain_task_metrics(...)`：先可以只写 heartbeat 和运行快照，后续增强指标。
- `record_worker_heartbeat(...)` 必须写入真实 `process_type`，例如 `planner`、`dispatcher`、`listener`、`recovery`、`metrics`。
- `docker-compose.server.yml` 拆出多个 worker 服务，先保守配置：
  - `planner x 1`
  - `dispatcher x 2`
  - `listener x 1`
  - `recovery x 1`
  - `metrics x 1`
- legacy drain 继续留在 `all` 或单独 `legacy`，避免污染新任务中心主路径。
- API embedded worker 仅允许开发环境使用 `all` 或指定 role；生产继续保持 `ENABLE_EMBEDDED_WORKER=false`。
- 非生产 `/api/worker/drain-once` 增加 role 参数或内部默认只调用 `all`，并在响应里返回各 role 的处理数，方便调试。

验收：

- 停掉 Listener worker 不影响 Dispatcher 已有 Action 投递。
- Dispatcher 慢不会阻塞 Planner 更新任务 next_run_at。
- worker heartbeat 能按角色展示。
- `python -m app.worker --once --role planner` 只规划任务，不执行 TG。
- `python -m app.worker --once --role dispatcher` 只领取和执行 Action，不采集监听源。
- `python -m app.worker --once --role recovery` 能恢复超时 claim / executing，但不生成新 Action。

### P2：Dispatcher 有界并发

目标：释放单 worker 吞吐，同时不压垮账号、代理、目标和数据库。

动作：

- 让 `DISPATCHER_CONCURRENCY` 真正控制 Dispatcher 内部并发。
- claim 后不要共用一个长生命周期 session 顺序执行所有 action。
- 每个 Action 执行拆成短事务：
  - 读取 Action 和账号信息；
  - 提交并释放 DB 连接；
  - 调用 TG Gateway；
  - 开新事务回写结果。
- 并发池只并发已经 claim 成功的 action；未 claim 的 action 不能进入执行池。
- 单个 action 的 gateway 调用前必须写入 `ExecutionAttempt.gateway_call_started_at`，避免 worker 崩溃后误重发。
- Redis token bucket 当前是立即扣 token；P2 可以先保留保守扣减语义，但文档和代码命名要避免误称为“可回滚 reservation”。如果要实现完整 reservation，需要补 confirm/refund Lua 脚本。
- 数据库连接池按公式配置：

```text
需要连接数
  = backend_api_connections
  + planner_workers * planner_connections
  + dispatcher_workers * dispatcher_concurrency * db_connection_per_action
  + listener_workers * listener_connections
  + recovery_connections
  + metrics_connections
  + maintenance_reserve
```

验收：

- 单 Dispatcher worker 内可并发执行多个不同账号 Action。
- 同一账号不会被多个 worker 同时使用。
- DB 连接池不会因为并发执行被打满。
- Redis 不可用且 fail-closed 时，Dispatcher 不调用 TG。
- 一个 action 已进入 gateway 调用边界后，worker 异常退出会进入 `unknown_after_send` 或人工确认路径，不自动重复发送。

### P3：跨进程账号 in-flight 和账号分片

目标：让 1000 账号时横向扩 worker 是可控的。

动作：

- 启用 Redis account in-flight lock / semaphore。
- `ACCOUNT_SHARD_TOTAL`、`ACCOUNT_SHARD_INDEX` 在 claim 前生效。
- 按账号 ID、账号池或代理池做 worker 分片。
- 每个 worker 只加载自己负责账号的 Telethon client。
- claim 查询前先按 shard 过滤可执行账号，账号转派也只能在当前 shard 内选择。
- 如果当前 shard 没有可用账号，action 应延后并记录原因，不能跨 shard 转派后又被其他 worker 反复抢占。
- DB 唯一索引 `uq_actions_executing_account` 保留为最后兜底，但不作为主要调度机制。

验收：

- 同一账号不会在两个 dispatcher 进程里同时执行。
- 新增 dispatcher worker 后吞吐提升，重复执行数为 0。
- 单 worker 内 Telethon client 数量可控。
- 某个 shard worker 停掉后，该 shard 的 action 会积压或由明确的重分片机制接管，不会被其他 shard 随机抢占。

### P4：Listener 独立扩容

目标：监听采集不拖慢发送，发送也不拖慢监听。

动作：

- Listener worker 按 source shard 采集。
- listener source claim 使用短 lease。
- 源群/频道水位持久化。
- 采集失败、上下文为空、权限异常与发送失败分开统计。
- `drain_task_center()` 不再隐式先跑 listener runtime；listener 只由 `listener` role 执行。
- Listener worker 写入 `process_type="listener"` heartbeat。
- Listener 采集成功后只唤醒订阅任务或写入上下文，不直接发送消息。

验收：

- 同一源群同一窗口只采集一次。
- 多 Listener worker 不重复拉取同一 source。
- 源消息到 Action 生成的延迟可观测。
- 监听压力大时，Dispatcher 的 pending action 仍可继续消化。

### P5：Metrics 快照和容量面板

目标：让容量判断有数据，不再只靠日志和实时 SQL。

动作：

- 新增 metrics role，周期性写入运行快照。
- 首期指标：
  - pending / claiming / executing action 数；
  - oldest pending age；
  - 每分钟 claimed / success / failed / skipped；
  - unknown_after_send 数；
  - worker heartbeat 按角色统计；
  - Redis token bucket 限流次数和等待秒数；
  - FloodWait / SlowMode / 账号受限 / 代理异常次数；
  - DB 查询耗时或连接池等待指标。
- 运营数据页面优先读快照，缺失时再回退到实时查询。
- `daily_runtime_stats` 继续用于历史保留汇总，不把它误当实时 metrics worker。

验收：

- 页面能看到 2 分钟内各 role worker 是否在线。
- 页面能看到当前积压、最老等待时间和最近 5 分钟处理速率。
- 压测时可以通过 metrics 判断瓶颈在 Planner、Dispatcher、Listener、TG API、Redis 还是 DB。

### P6：大文件拆分和边界收敛

目标：降低后续功能迭代成本，不在一个大文件里继续堆逻辑。

建议拆分：

| 当前文件 | 建议拆分方向 |
| --- | --- |
| `task_center/service.py` | `tasks_crud.py`、`planner.py`、`drain.py`、`recovery.py`、`details.py`、`reviews.py` |
| `task_center/dispatcher.py` | `claim.py`、`runtime_resources.py`、`rate_limits.py`、`dispatch.py`、`result_handlers.py` |
| `gateways.py` | `gateway/base.py`、`gateway/mock.py`、`gateway/telethon_client.py`、`gateway/telethon_send.py`、`gateway/telethon_login.py`、`gateway/media.py` |
| `operations_center.py` | `overview.py`、`target_summary.py`、`listener_summary.py`、`rule_bootstrap.py`、`risk_summary.py` |
| `frontend/src/app/context.tsx` | React Query hooks、mutation hooks、modal state、selection state、domain API 拆开 |
| `TaskCenterView.tsx` | `TaskList`、`TaskWizard`、`TaskEditor`、`TaskDetailDrawer`、`TaskActionTable`、`taskFormMapping.ts` |
| `RulesCenterView.tsx` | `RuleSetList`、`RuleVersionEditor`、`RuleTester`、`RulePublishPanel` |

拆分原则：

- 先拆无行为变化的模块边界，再做并发能力。
- 每次只拆一个主文件，必须配套测试或构建验证。
- 不为“看起来优雅”拆分，优先拆影响扩容的执行路径和 gateway。
- 优先拆 `task_center/service.py` 和 `dispatcher.py`，因为它们直接影响 role 分流、并发执行和测试隔离。

---

## 6. 不建议的做法

- 不建议只把 `WORKER_DRAIN_LIMIT` 从 100 改到 1000。这只会让单轮阻塞更久。
- 不建议只增加 worker 容器数量，而不确认 claim、账号 in-flight、Redis 限流和 DB 连接池。
- 不建议把 1000 个 Telethon client 全塞进一个进程。
- 不建议让 API 进程继续承担 embedded worker 职责。
- 不建议继续把任务创建、任务规划、执行投递、监听采集和运行恢复都堆在同一个服务文件里。
- 不建议把 `DISPATCHER_CONCURRENCY`、`ACCOUNT_SHARD_TOTAL` 这类“已存在配置”当成“已生效能力”。
- 不建议把实时运营数据查询当成 Metrics Worker；实时查询能看结果，不等于有容量快照和瓶颈定位。
- 不建议在数据库行锁事务里等待 Redis、代理探测或 Telegram API。

---

## 7. 推荐实施顺序

```text
第 1 步：修正 worker role 入口，让配置真正生效
第 2 步：把 task center drain 拆成 planner / dispatcher / listener / recovery / metrics 可单独调用的函数
第 3 步：更新生产 compose，启动多角色 worker
第 4 步：先补 PostgreSQL 连接池参数，再接入 Dispatcher 有界并发和短 session 执行
第 5 步：补 Redis 跨进程账号 in-flight 和账号分片
第 6 步：Listener 独立 worker 和 source shard 扩容
第 7 步：Metrics 快照和容量面板
第 8 步：压测脚本验证 100 / 300 / 1000 账号
第 9 步：task_center / dispatcher / gateway / frontend 大文件拆分
```

最优先级不是继续加功能，而是先让执行层能稳定回答：

```text
系统现在积压多少？
最老任务等了多久？
哪个账号被限流？
哪个代理异常？
哪个 worker 还活着？
每分钟能安全消化多少 Action？
```

---

## 8. 最终判断

当前系统可以作为 100 账号规模的试运行底座继续推进。它已经有比普通定时任务系统更好的结构基础，尤其是 Task / Action、claim、lease、heartbeat、Redis token bucket 和监听 source lease。

但 1000 账号、20-30 个持续任务不是简单“多起几个 worker”就能安全解决。下一阶段应该把执行层从“单 worker 综合 drain”升级成“按角色拆分 + 多 Dispatcher worker + 有界并发 + 跨进程账号锁 + 多维限流 + 指标观测”。

完成 P1-P4 后，再通过压测给出明确容量参数，才适合对外承诺 1000 账号运营规模。

当前最容易误判的是：`WORKER_ROLE`、`DISPATCHER_CONCURRENCY`、`ACCOUNT_SHARD_TOTAL` 已经出现在配置里，但它们还没有真正控制运行时行为。实施时要先把这些配置接到真实执行路径上，再谈扩容参数。

---

## 9. 仍需补充设计的地方

下面这些不是马上写代码前的小细节，而是实施前应先定清楚的设计口径。否则代码容易做成“能跑一版”，但压到 1000 账号时又要返工。

### 9.1 Worker role 运行契约

需要补清楚每个 role 的输入、输出、是否允许副作用和是否允许调用 TG。

| Role | 输入 | 允许写入 | 禁止事项 |
| --- | --- | --- | --- |
| `planner` | `running` tasks、监听上下文、规则版本、账号池快照 | `actions`、`tasks.next_run_at`、任务 stats | 禁止调用 TG Gateway |
| `dispatcher` | due pending actions | action result、execution attempts、账号运行状态、任务 stats | 禁止生成新业务 Action |
| `listener` | listener source、源群/频道、监听账号 | 上下文消息、listener source state、唤醒任务 next_run_at | 禁止发送业务消息 |
| `recovery` | claiming/executing/heartbeat 超时数据 | action 恢复状态、attempt 结果未知、任务错误摘要 | 禁止调用 TG |
| `metrics` | action、task、worker、账号、代理、Redis 指标 | runtime snapshots、聚合指标 | 禁止改变业务状态 |
| `legacy` | 旧 message task / campaign / operation task | 旧兼容表 | 禁止进入新版 Task Center 主流程 |

还需要明确：

- `all` 只用于开发和一次性兼容，不作为生产推荐。
- 每个 role 的 heartbeat `process_type` 必须固定，不能都写 `task_center`。
- 每个 role 的 `--once` 行为必须可测试。
- 角色拆分后，`/api/worker/drain-once` 的调试入口也必须带 role，否则非生产调试会误触发全链路。

### 9.2 Action 状态机和失败恢复

现在已有 `pending / claiming / executing / success / failed / skipped / unknown_after_send / retryable_failed` 等状态，但实施并发前需要画清状态机。

必须补充：

- `pending -> claiming -> executing -> success/failed/skipped` 的唯一主路径。
- `claiming -> pending` 的原因：claim 超时、运行资源不足、账号分片不匹配、Redis 限流、账号 in-flight 冲突。
- `executing -> unknown_after_send` 的条件：已经写入 `gateway_call_started_at`，但 worker 崩溃或 lease 过期。
- `executing -> failed` 的条件：没有进入 gateway 调用边界，或确定失败。
- `unknown_after_send` 的人工处理方式：人工确认成功、人工确认失败、补偿查询、禁止自动重复发送。
- `retryable_failed` 与 `failed` 的边界：哪些错误允许自动重试，哪些必须人工介入。

实施前建议把状态机写成表格并加测试，避免 Dispatcher 并发后发生重复发送。

### 9.3 Redis token bucket 语义

当前代码已经能做 Redis token bucket 限流，但语义更接近“先扣 token，失败时不退回”，不是完整的 reservation / confirm / refund。

这里要做一个明确选择：

方案 A：保守扣减

- 优点：实现简单，安全，不会绕过限流。
- 缺点：claim 后如果 DB 确认失败或 worker 崩溃，token 会浪费，吞吐会偏低。
- 适合：第一阶段先让多 worker 安全跑起来。

方案 B：完整 reservation

- claim 前创建带 TTL 的 reservation。
- DB 确认 `executing` 成功后 confirm 消耗。
- DB 确认失败或 action 未进入 gateway 前 refund。
- worker 崩溃后 reservation TTL 自动释放。

建议第一阶段采用方案 A，但代码和文档不要把它描述成“可回滚 reservation”。等 Dispatcher 并发稳定后再升级方案 B。

### 9.4 账号分片和故障接管

账号分片不只是 `account_id % total`，还要设计 worker 停掉后的接管规则。

本轮补充了首期实现：

- 新增 `backend/app/telethon_lifecycle.py`，从 Telegram 网关适配器抽离后台 event loop、client cache、idle TTL、LRU 上限、连接超时和 shutdown disconnect。
- 新增 `backend/app/integrations/telegram/contracts.py`，从 Telegram 网关适配器抽离 Gateway 对外数据契约。
- 新增配置 `TELETHON_CLIENT_CACHE_SIZE`、`TELETHON_CLIENT_IDLE_SECONDS`、`TELETHON_CLIENT_CONNECT_TIMEOUT_SECONDS`、`TELETHON_OPERATION_TIMEOUT_SECONDS`。
- FastAPI lifespan 停止时调用 Telethon lifecycle shutdown，避免 worker / API 进程退出后遗留连接。

仍需要在真实压测中继续校准：

- 分片维度：按账号 ID、账号池、代理池还是运营目标。
- 分片生效点：必须在 claim 前生效，账号转派也只能在本 shard 内完成。
- 本 shard 无账号时：action 延后并记录 `shard_no_available_account`，不能跨 shard 偷跑。
- worker 故障时：是保持该 shard 积压，还是由 standby worker 接管。
- 扩容/缩容时：`ACCOUNT_SHARD_TOTAL` 改变会导致账号重新分布，需要明确是否允许运行中切换。

建议第一阶段不要频繁动态调整 shard total。先固定 `ACCOUNT_SHARD_TOTAL`，通过增加同分片 standby 或手工调整部署来接管。

### 9.5 PostgreSQL 连接池设计

当前 `create_engine(...)` 已经按环境变量显式配置 `pool_size`、`max_overflow`、`pool_timeout` 和 `pool_recycle`。多 worker + Dispatcher 并发后，仍必须把连接池预算纳入部署参数，否则最先出问题的可能不是 TG，而是 DB 连接耗尽。

需要补充：

- 环境变量：
  - `DB_POOL_SIZE`
  - `DB_MAX_OVERFLOW`
  - `DB_POOL_TIMEOUT`
  - `DB_POOL_RECYCLE`
- 不同 role 的连接预算：
  - API 常驻连接；
  - Planner 扫描连接；
  - Dispatcher 并发 action 的短事务连接；
  - Listener 采集写入连接；
  - Recovery 扫描连接；
  - Metrics 快照连接；
  - 迁移和人工排查预留连接。
- 监控指标：连接池等待时间、连接获取失败次数、慢查询、事务持续时间。

实施原则：Dispatcher 并发数必须受 DB 连接预算约束，不允许只按 TG API 速率调大。

### 9.6 Telethon client 生命周期

1000 个账号时，Telethon client cache 是容量核心之一，需要单独设计。

需要补充：

- 每个 Dispatcher worker 最多加载多少账号 client。
- client idle 多久释放。
- worker 停止时如何优雅 disconnect。
- session 失效、代理切换、账号受限时如何清理 cache。
- 同一账号是否允许跨 worker 同时保持 client。
- 文件描述符、内存、event loop 队列的限制。
- listener client 和 sender client 是否共享，还是按 role 分开。

建议先按账号 shard 限制每个 worker 的 client 范围，再根据 `TELETHON_CLIENT_CACHE_SIZE` 和 idle TTL 校准单进程上限。

### 9.7 Metrics 快照表和页面口径

当前运营数据可以实时查，但还缺“运行快照”的设计。

需要补充一张或一组 runtime metrics 表，至少包含：

```text
runtime_metric_snapshots
  id
  captured_at
  metric_name
  dimension_type
  dimension_id
  metric_value
  tags_json
```

首期指标：

- `actions.pending.count`
- `actions.claiming.count`
- `actions.executing.count`
- `actions.oldest_pending_age_seconds`
- `actions.claimed_per_minute`
- `actions.success_per_minute`
- `actions.failed_per_minute`
- `gateway.flood_wait.count`
- `gateway.slowmode.count`
- `worker.active.count`
- `worker.stale.count`
- `redis.token_limited.count`
- `db.pool_wait_ms.p95`

页面需要能回答：

- 现在是不是积压。
- 积压在 Planner、Dispatcher、Listener、TG API、Redis 还是 DB。
- 哪个 worker 掉线。
- 哪个账号、代理、目标造成限流最多。

### 9.8 压测模型和容量报告格式

1000 账号能力必须通过压测固化。当前已新增首期容量模型脚本和报告：

- `backend/scripts/run_capacity_benchmark.py`
- `reports/capacity/latest.json`
- `docs/capacity-report-100-300-1000.md`

该脚本覆盖 100 / 300 / 1000 账号、fast / slow / flood_wait / slowmode / unknown_after_send mock gateway 模式，并输出 worker 数、并发数、claim limit、PostgreSQL pool、吞吐、oldest pending、unknown_after_send、重复发送数和单机边界。

注意：这仍是 mock gateway 容量模型，不是线上 TG API 实测结论。最终发布验收必须接 PostgreSQL / Redis，并补真实任务创建与执行测试。

压测数据和报告格式：

压测场景至少包括：

- 100 账号 / 5 任务，作为当前试运行基线。
- 300 账号 / 10 任务，作为中间容量。
- 1000 账号 / 20-30 任务，作为目标容量。
- TG Gateway mock 快速返回。
- TG Gateway mock 慢返回。
- FloodWait / SlowMode / account limited 注入。
- Redis 不可用。
- DB 连接池紧张。
- worker 异常退出和重启。

容量报告应输出：

- 推荐 worker 数量。
- 推荐 `DISPATCHER_CONCURRENCY`。
- 推荐 `ACTION_CLAIM_LIMIT`。
- 推荐 `DB_POOL_SIZE / MAX_OVERFLOW`。
- 每分钟处理量。
- oldest pending P95。
- unknown_after_send 数。
- 重复发送数必须为 0。

### 9.9 部署和运维设计

生产部署需要从“一个 worker”变成“多个角色 worker”，需要补充部署口径：

- compose 服务命名：`worker-planner`、`worker-dispatcher-1`、`worker-dispatcher-2`、`worker-listener`、`worker-recovery`、`worker-metrics`。
- 每类 worker 的 restart policy。
- 每类 worker 的环境变量差异。
- 每类 worker 的日志标签。
- 每类 worker 的健康检查方式。
- 如何临时扩 Dispatcher。
- 如何停 Listener 而不影响 Dispatcher。
- 如何确认线上没有 embedded worker。

如果后续走 GitHub Actions 发布，还需要把 compose 变更、环境变量模板、发布后巡检一起纳入。

### 9.10 大文件拆分的设计顺序

大文件拆分不应该和并发改造搅在一起一次性做完。建议补充具体顺序：

1. 先从 `task_center/service.py` 抽 role drain 函数，保持导出兼容。
2. 再拆 `dispatcher.py`，先拆 claim / runtime resources / rate limits，不改行为。
3. 再拆 Gateway，先拆接口和 Telethon client lifecycle。
4. 最后拆前端 `TaskCenterView.tsx` 和 `context.tsx`。

每一步验收：

- 原测试通过。
- 行为不变。
- 导入路径直接收敛到 `app.integrations.telegram`。
- 文档同步。

### 9.11 风控处置和限流结果口径

当前有失败类型和风控中心基础，但多 worker 后要补清楚哪些错误影响账号、哪些影响代理、哪些影响目标。

需要补充：

- FloodWait 是否写入账号冷却，冷却多久。
- SlowMode 是否写入目标冷却，冷却多久。
- 代理不可达是否阻塞该代理下账号。
- 账号受限是否自动暂停账号。
- 内容拦截是否允许改写后重试。
- unknown_after_send 是否进入人工队列。
- 同一错误重复出现多少次触发告警。

这个设计会影响 Dispatcher result handler、RiskControl、Metrics 和前端提示。

### 9.12 数据保留和审计

`runtime_retention` 已经能把旧 Action 汇总到 `daily_runtime_stats` 后清理，但并发执行后需要补充：

- 未解决状态是否允许被清理。
- `unknown_after_send` 保留多久。
- execution attempts 保留多久。
- 人工确认记录保留在哪里。
- 清理后任务详情如何展示历史统计。

建议：`unknown_after_send` 和人工未处理项不要被普通保留周期清理，除非已经人工确认或归档。

---

## 10. 已纳入整体升级的代码优化清单

本节用于把本轮代码体检发现的优化点收进后续整体升级范围。后续实施时不要把这些点当成临时修补，而应按下面的阶段和验收口径统一改造。

### 10.1 必须先做的执行层优化

| 优化项 | 当前代码现状 | 升级目标 | 涉及文件 | 阶段 |
| --- | --- | --- | --- | --- |
| Worker role 真正生效 | `WORKER_ROLE` 已有配置，但 `app.worker` 仍只有综合 `drain_once()` | `--role` / `WORKER_ROLE` 控制 `planner`、`dispatcher`、`listener`、`recovery`、`metrics` 的独立 drain | `backend/app/worker.py`、`backend/app/services/task_center/service.py`、`backend/app/main.py`、`backend/app/api/routers/system.py`、`docker-compose.server.yml` | P1 |
| Task Center drain 拆分 | `drain_task_center()` 同时做 listener、recovery、planner、dispatcher、retention | 拆成可单独调用的 `drain_task_planner()`、`drain_task_dispatcher()`、`drain_task_listener()`、`drain_task_recovery()`、`drain_task_metrics()` | `backend/app/services/task_center/service.py`，必要时新增 `drain.py`、`planner.py`、`recovery.py` | P1 |
| Heartbeat 按角色写入 | 当前 task center heartbeat 不能区分真实 role | `worker_heartbeats.process_type` 写入真实角色，页面和 metrics 能按角色判断在线状态 | `backend/app/services/task_center/heartbeat.py`、`backend/app/services/task_center/service.py`、`backend/app/services/operations_center.py` | P1 |
| Dispatcher 有界并发 | `DISPATCHER_CONCURRENCY` 已有配置，但 claim 后仍串行 `dispatch_action()` | 同一个 Dispatcher worker 内按配置并发执行不同账号 action，且每个 action 使用短 session | `backend/app/services/task_center/service.py`、`backend/app/services/task_center/dispatcher.py` | P2 |
| PostgreSQL 连接池参数化 | 已有 `DB_POOL_SIZE`、`DB_MAX_OVERFLOW`、`DB_POOL_TIMEOUT`、`DB_POOL_RECYCLE` 配置 | 用连接预算约束 worker 数、dispatcher 并发和部署环境模板 | `backend/app/config.py`、`backend/app/database.py`、部署环境模板 | P2 |
| Gateway 调用边界更清晰 | dispatcher 中混有 claim、资源预留、gateway 调用、结果处理 | gateway 调用前后状态可恢复，`unknown_after_send` 不自动重复发送 | `backend/app/services/task_center/dispatcher.py`、`backend/app/integrations/telegram/gateway.py`、executor 文件 | P2 |

### 10.2 横向扩容和容量稳定性优化

| 优化项 | 当前代码现状 | 升级目标 | 涉及文件 | 阶段 |
| --- | --- | --- | --- | --- |
| 跨进程账号 in-flight | 主要靠进程内 `_IN_FLIGHT_ACCOUNTS`，DB 唯一索引兜底 | 引入 Redis account lock / semaphore，claim 前尽量避免抢到不可执行账号 | `backend/app/services/task_center/dispatcher.py`，可拆到 `runtime_resources.py` | P3 |
| 账号分片生效 | `ACCOUNT_SHARD_TOTAL` / `ACCOUNT_SHARD_INDEX` 配置存在，但 claim 和转派未使用 | claim 查询、账号转派、Telethon client 加载都限制在当前 shard | `backend/app/services/task_center/dispatcher.py`、`backend/app/services/task_center/account_pool.py`、`backend/app/services/accounts.py` | P3 |
| Listener 独立 worker | listener runtime 仍由 task center 综合 drain 触发 | listener role 独立采集，source lease / shard 防重复，慢监听不拖慢发送 | `backend/app/services/task_center/listener_runtime.py`、`backend/app/services/task_center/service.py` | P4 |
| Metrics 快照 | 运营数据主要实时查询，缺少容量快照 | 新增 metrics role 和 runtime snapshots，记录 pending、oldest age、吞吐、限流、worker、DB 等指标 | `backend/app/models/task_center.py` 或新增 metrics 模型、`backend/app/services/operations_center.py`、前端运营数据页 | P5 |
| 压测脚本 | 已新增 mock gateway 容量模型和 100 / 300 / 1000 账号报告 | 继续接真实 PostgreSQL / Redis 压测，并把真实任务创建、claim、执行、metrics 纳入验收 | `backend/scripts/run_capacity_benchmark.py`、`docs/capacity-report-100-300-1000.md` | P5 |

### 10.3 大文件拆分优化

这些拆分不作为第一步容量瓶颈修复，但必须纳入整体升级，否则后续并发、风控、metrics 会继续堆进大文件。

本轮已完成 P6 第一批拆分，旧导入兼容层已删除：

- `backend/app/integrations/telegram/contracts.py`：Gateway 数据契约。
- `backend/app/integrations/telegram/mock.py`：Mock / 本地模拟 Gateway，避免与真实 Telethon 适配器混在同一文件。
- `backend/app/telethon_lifecycle.py`：Telethon event loop、client cache、idle prune、shutdown。
- `backend/app/services/task_center/config_fields.py`：任务中心配置字段映射。
- `backend/app/services/task_center/precheck.py`：任务创建前预检，聚合目标能力、规则版本、风控预检和账号容量缺口。
- `backend/app/services/task_center/details.py`：任务详情 payload、频道子任务分组、AI cycle、转发批次和账号画像展示聚合。
- `backend/app/services/task_center/utils.py`：任务中心通用解析工具。
- `backend/app/services/task_center/runtime_resources.py`：Dispatcher Redis token bucket、账号 in-flight 和运行资源释放。
- `backend/app/services/task_center/reviews.py`：任务审核列表、通过、驳回和状态校验。
- `backend/app/services/task_center/stats.py`：任务统计刷新、失败重试、下次运行时间计算。
- `backend/app/services/task_center/config_normalization.py`：任务配置默认值、类型配置校验、规则绑定和运营目标引用归一化。
- `backend/app/services/operations_center_defaults.py`：运营中心默认规则集和系统规则常量。
- `backend/app/services/operations_center_listener.py`：监听中心 summary、备用账号切换、事件去重和最近事件查询。
- `backend/app/services/operations_center_risk.py`：运营数据里的风控、运行积压和 worker 心跳风险指标。
- `backend/app/services/operations_center_rule_metrics.py`：规则中心摘要、冲突、执行趋势、转化和交叉维度指标。
- `backend/app/services/operations_center_rule_sets.py`：规则集 CRUD、版本创建、发布、回滚和绑定任务查询。
- `backend/app/services/operations_center_utils.py`：运营中心通用解析 / 时间格式化工具。
- `backend/app/integrations/telegram/telethon_content.py`：Telethon 群/频道消息采集、评论采集、素材缓存。
- `backend/app/integrations/telegram/telethon_media.py`：Telethon 媒体发送、TG 缓存重传、custom emoji 发送。
- `backend/app/integrations/telegram/telethon_utils.py`：Telethon 目标解析和发送目标转换。
- `frontend/src/app/types/`：前端 API / UI 类型按领域拆分，保留 `../types` 目录索引导出。
- `frontend/src/app/views/taskCenterViewModel.ts`：任务中心页面纯 view-model、字段映射和格式化函数。
- `frontend/src/app/views/TaskCenterWizardSections.tsx`：任务创建/编辑向导子组件。
- `frontend/src/app/views/TaskCenterDetailModal.tsx`：任务详情、执行计划、AI cycle、转发批次和频道消息明细弹窗。
- `frontend/src/app/context/defaults.ts`：App Provider 表单默认值。
- `frontend/src/app/context/refresh.ts`：App Provider 全局刷新聚合查询。
- `frontend/src/app/context/actionRunner.ts`：前端操作 loading / pending action hook。
- `frontend/src/app/context/authActions.ts`：登录、注册、验证码、修改密码和退出登录动作。
- `frontend/src/app/context/accountActions.ts`：账号详情、账号登录、账号分组、账号克隆、验证辅助和资料同步动作。
- `frontend/src/app/context/messageActions.ts`：私发、批量发送、取消、派发、重试和手动 drain 动作。
- `frontend/src/app/context/systemActions.ts`：开发者应用、租户配额、后台用户、AI 供应商和提示词动作。
- `frontend/src/app/context/modalState.ts`：结果提示、错误解析、确认弹窗和关闭弹窗。
- `frontend/src/app/context/contentActions.ts`：素材和关键词规则动作。
- `frontend/src/app/views/RulesCenterConfig.tsx`：规则中心配置表单、规则 JSON 和可视化配置互转工具。

拆分后关键文件当前行数：`operations_center.py` 780、`task_center/service.py` 960、`gateways.py` 943、`TaskCenterView.tsx` 873、`RulesCenterView.tsx` 892、`dispatcher.py` 727、`context.tsx` 797。
`context.tsx` 已从大文件风险中降下来，本轮已经把监听、规则指标、风控指标、规则集版本、预检、详情、reviews、stats、配置归一化、Telethon 内容/媒体、Mock Gateway、规则配置表单、任务详情弹窗、认证、账号、消息、系统配置、素材关键词、modal、全局刷新和 pending action 从主文件拆出；后续新增能力必须继续进入 domain 模块，不再回填主文件。

本轮已执行的验证命令：

```bash
cd frontend && npm run build
cd backend && APP_ENV=test .venv/bin/python -m compileall -q app tests scripts
cd backend && APP_ENV=test .venv/bin/python -m pytest -q tests/test_operations_center_runtime.py tests/test_capacity_benchmark.py tests/test_telethon_lifecycle.py tests/test_task_center_capacity_dispatch.py tests/test_worker_roles.py tests/test_task_center_role_drains.py
cd backend && APP_ENV=test .venv/bin/python -m pytest -q tests/test_workflow.py::test_worker_drain_once_api_accepts_role tests/test_workflow.py::test_worker_drain_once_api_rejects_unknown_role tests/test_workflow.py::test_task_center_group_ai_chat_creates_and_dispatches_actions tests/test_workflow.py::test_task_center_group_ai_chat_cycles_and_picks_up_new_context tests/test_workflow.py::test_task_center_group_relay_continues_for_new_source_messages tests/test_workflow.py::test_task_center_reset_channel_view_rebuilds_from_latest_messages
git diff --check
```

结果：前端构建通过；后端编译通过；运营中心 runtime + 容量模型 + Telethon lifecycle + 任务中心容量调度 + worker role 组合回归已通过；PostgreSQL 真实任务链路精选回归已通过。最新一次补充拆分后重新执行了前端构建、后端编译和关键 worker / task center / Telethon / rules 回归。它们证明本轮重构没有破坏关键任务创建 / drain / 执行路径，但仍不是 Redis 开启和真实 TG API 的容量实压。

| 文件 | 当前问题 | 拆分目标 | 建议阶段 |
| --- | --- | --- | --- |
| `backend/app/services/task_center/service.py` | CRUD、规划、恢复、role drain 仍集中 | 继续拆 tasks_crud / planner / recovery / drain | P1-P6 |
| `backend/app/services/task_center/dispatcher.py` | claim、资源预留、Redis 限流、gateway 分发、结果处理集中 | 拆成 claim、runtime resources、rate limits、dispatch、result handlers | P2-P6 |
| `backend/app/integrations/telegram/gateway.py` | Telethon login、profile、channel action 仍在主适配器 | 继续拆 telethon_login / telethon_profile / telethon_channel_actions | P2-P6 |
| `backend/app/services/operations_center.py` | 规则测试和部分运营聚合仍集中 | 继续拆 rule_tester / operations_reports | P5-P6 |
| `frontend/src/app/context.tsx` | Provider 仍集中声明大量 state，但业务动作已拆 | 后续可逐步迁移 selection state / React Query hooks，当前不再作为 P6 阻塞项 | P6 |
| `frontend/src/app/views/TaskCenterView.tsx` | 页面已降到 873 行，列表交互和 columns 仍可组件化 | 继续拆 TaskList / TaskActionTableColumns | P6 |
| `frontend/src/app/views/RulesCenterView.tsx` | 配置表单已拆，测试器和发布面板仍在主视图 | 继续拆 RuleSetList / RuleTester / RulePublishPanel | P6 |

### 10.4 本轮确认的实施优先级

后续整体升级建议按下面顺序排，不建议先做纯 UI 或纯文件美化：

1. `WORKER_ROLE` 接入真实运行路径。
2. `drain_task_center()` 拆成 role drain。
3. 生产 compose 拆多角色 worker。
4. PostgreSQL 连接池参数化和连接预算。
5. Dispatcher 有界并发和短 session 执行。
6. Redis 跨进程账号 in-flight。
7. 账号分片进入 claim 和转派路径。
8. Listener 独立 worker。
9. Metrics 快照和容量面板。
10. 压测脚本和 100 / 300 / 1000 账号容量报告。
11. 大文件拆分和前端状态收敛。

验收底线：

- 不能重复发送同一个 action。
- 不能让 Redis 不可用时 fail-open。
- 不能让 Dispatcher 并发超过 DB 连接池预算。
- 不能让 Listener 慢采集阻塞已生成 Action 的发送。
- 不能把 `WORKER_ROLE`、`DISPATCHER_CONCURRENCY`、`ACCOUNT_SHARD_TOTAL` 继续停留在“配置存在但不生效”的状态。

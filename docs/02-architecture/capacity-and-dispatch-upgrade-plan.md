# TG 运营管理平台容量与调度架构升级方案

> 本文用于承接 100 个账号向 1000 个账号规模演进的技术架构方案。  
> 评估口径基于当前仓库的静态代码结构与产品设计文档，不等同于线上压测结论。  
> 当前目标不是推翻现有系统，而是在已有 Task / Action / Account / Rule / RiskControl 模型上升级执行层、容量层、风控层和观测层。

> **2026-07-28 履约优先架构覆盖：** 本文件早期章节中的 AI 日覆盖容量证明、硬小时目标、活动窗口、群本地冷却和“容量不足即停止建单”均已废止。当前产品合同以 `all-task-fulfillment-recovery-prd.md`、`ai-group-daily-group-target-redesign-prd.md` 和 `search-click-daily-fulfillment-remediation-prd.md` 为准；旧模块只作为迁移定位与历史审计入口。

当前 AI 活群执行架构固定为：

```text
群自然日目标 + 冻结账号全覆盖
  -> 24 小时非零权重规划（静默时间只降权）
  -> 目标群准入
  -> 主 AI 正常内容生成与质量检查（最多 3 轮）
  -> 不同备用 AI 正常内容生成与质量检查（最多 3 轮）
  -> AI 精确“签到”确定性兜底
  -> Telegram 真实发送
  -> 无需可见性核验：Action + Attempt + remote_message_id 原子确认群日账本
  -> 需要可见性核验：pending_visibility_hold -> visible_confirmed -> 原子确认群日账本
```

- `effective_daily_target = max(daily_message_target, frozen_account_count)`；每个冻结账号当天至少确认 1 条。
- `DISPATCHER_SCOPE_CAPACITY`、账号互斥、Telegram 限流只保护运行时，不得降低业务目标、缩小冻结分母或返回 AI 日容量 PlanAbort。
- 目标群准入和正常内容质量继续保留；登录态、授权代理路线、Telegram 权限与远端回执是不可伪造的执行事实，不包装成可取消的业务门禁。
- 缺面具、授权代理路线切换，或主 AI 3 轮加不同备用 AI 3 轮均无可用候选时，AI 使用绑定原义务的精确 `签到`；评论使用单个 Unicode 表情文本。无可用授权传输路线时进入 `waiting_transport`，不得直连、假成功或吞错。
- AI 活群继续使用 `GroupBotAdmission`：join → 可信群管提示 → 关注要求频道 → 精确确认/挑战 → membership 与 can_send 复检。
- 纯搜索点击固定 `task_type=search_click + search_execution_mode=click_only`，正式 API/权限为 `/api/tasks/search-click + tasks.create.search_click`；只执行 search/target match/click。可执行协议必须证明 `membership_side_effect=none`，Gateway 审计 `membership_mutating_rpc_invoked=false`，旧 `join_candidate`/副作用未知样本不进 eligibility。`target_click_observed` 后 ordinal 结束，固定 `admission_lane_claims=0`，禁止创建 membership/admission 子 Action。“搜索点击加入”仅登记为后续独立模式，本轮不设计其架构。
- 图片算式验证码只使用真实状态：`required` 进入当前 source 的 AI 识别流程且不排除账号，按 bot/message/image/ordered-callback 冻结 `challenge_fingerprint_hash`；AI 调用/批准重试不消费 click 限额、目标或额外 Dispatcher 份额，也不计入 AI 活群/评论的主 AI 三轮、备用 AI 三轮或业务 AI 生成次数。只有同一 fingerprint 的单次批准答案提交取得明确远端通过回执，或进入已审批 `search_category_page|group_result_page`，才写 `solved` 并继续；仅离开原页、超时、hot-list、unknown 或新 fingerprint 不算通过。识别不设置业务固定 AI 轮数/递归次数，单供应商候选不合格继续下一健康已审批供应商，供应商/传输暂不可用保持 required；只有当前健康已审批识别链确实无安全答案或同 fingerprint 被远端明确拒绝，才写 `failed` 并形成账号—协议路径 24h eligibility 排除。当前 Action 保留既有账号 session ownership，challenge 收口前同一账号—协议会话不得被另一搜索 Action 并发改写。容量和完成计算禁止使用触发率、AI 历史成功率或概率折损。
- AI 发送后可见性使用同一 post-Gateway hold：逻辑事实 `pending_visibility_hold` 与 `unknown_after_send` 各义务只占 1，共同进入 `unknown_after_send_hold_count`；现有 `PendingVisibilityCredit` 只是兼容物理名。`visible_confirmed` 才原子完成 Action、群日主槽和可选 coverage；`post_send_intercepted` 不计成功，`admission_abandoned` 不缩冻结账号分母或释放 coverage 主槽。
- 调度术语固定：中央 task-lane-to-shard 求解器为 `DispatchLaneShardSolver`，搜索路径求解器为 `SearchClickAssignmentSolver`；中央 Window 版本为 `dispatch_allocation_epoch`，搜索候选快照为 `search_click_assignment_epoch`。首次搜索 outcome 用 `SearchClickAssignmentEpoch` 承载释放；outcome 已 finalize 后的 assignment 释放必须使用唯一 `DispatchAllocationReleaseBatch`。两者都为每个 unit 写跨状态永久唯一的 `DispatchAllocationExclusion(window,reservation_id,fulfillment_lane_claim_ordinal)`。尚可领取 Window 的首批非空释放只开启一个 pending rebuild wave，wave 内后续 release batch 仅推进 `rebuild_input_version`；空集合和已结束 Window 都不推动中央 epoch。极搜账号 24h 协议安全排除只是带过期时间的 path eligibility 输入，不是中央 exclusion。不带限定词的旧名称仅作历史文本，不得进入新 schema/API。

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

Planner 只负责把任务拆成待执行义务，不直接调用 Telegram API。除搜索 click 外可在 Planner 物化 Action；搜索 Planner 只冻结 ordinal/只读候选，当前 Claim Window commit 取得中央份额后才创建 assignment 与可执行 Action。

职责：

- 扫描 `running` 且 `next_run_at <= now` 的任务。
- 检查任务结束时间、任务类型适用的每日上限、静默权重和下一轮调度时间；AI 活群不得因日容量或静默时间停止规划。
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
- claim 阶段必须先按业务任务的到期债务执行 60 秒 Claim Window 最低轮转和最大余数分配，再在任务已获份额内按账号池公平性、账号容量与安全资格选择执行项。
- 只有业务义务允许换号时，账号转派才在 claim 阶段完成。AI coverage 主发送槽、已绑定的搜索 source、reply/内容义务等固定账号合同不得因容量改绑；它们在原账号不可用时保持对应 waiting/blocker。可换号的普通 Action 由 Dispatcher 领取时确定最终 `account_id`；如果同任务账号池没有合法替代账号，则延后 action，不进入 TG 调用阶段。
- 账号分片在 claim 前生效。若启用 `ACCOUNT_SHARD_TOTAL / ACCOUNT_SHARD_INDEX`，当前 worker 只能在自身账号分片内选择最终账号；本分片没有可用账号时直接延后 action，不能先跨分片转派再重新入队，避免反复 claim。
- claim 成功但 Redis token 未拿到时，action 不能进入 `executing`；必须保持 `pending` 并把 `scheduled_at` 延后到 token 可用时间，避免监控里的 `executing` 被限流等待污染。
- `claiming` 只是短暂预领取状态，不代表已经调用 TG；`claim_expires_at` 过期后由 Recovery 恢复为 `pending`。
- 同一 `account_id` 默认只能被一个 worker 同时使用。账号并发必须通过 Redis in-flight key、Redis semaphore 或数据库条件锁实现，不能只依赖 token bucket。

### 3.2.1 Claim Window 公平合同

Dispatcher 不能只按全局时间、静态任务优先级或固定任务类型顺序抢任务。`DispatchClaimWindow` 使用版本化 `claim_window_seconds`，默认 60 秒；同一 `dispatcher_scope` 的所有 worker 必须读取同一版本。

当前产品只有一个业务用户和一个业务租户。`dispatcher_scope` 表示该用户下多个 Dispatcher worker、账号 shard 与任务类型共享的真实执行容量域，不是多个租户之间的配额或公平域。本次只做 scope -> 父业务任务 -> lane -> shard 的分配，不增加 `TenantAllocation`、租户权重或租户级 cursor；现有 `tenant_id` 只保留作数据隔离、唯一键和审计命名空间。

每个到期业务任务持久化：

| 字段 | 说明 |
| --- | --- |
| `due_claimable_count/by_shard` | 当前 Window 跨 shard 总可领取数及每个 shard 的候选数。 |
| `obligation_demand_buckets` | 各业务义务/同 deadline Window bucket 的 debt、剩余 Window、due candidates 与 required；TaskAllocation 只存有界摘要/hash。 |
| `required_claims` | `min(task_due_claimable_count, sum(min(obligation_due, max(1, ceil(obligation_debt / obligation_windows)))))`。 |
| `last_opportunity_window` | 上次获得可映射 Reservation 的 Window；用于机会公平。 |
| `last_claimed_window` | 上次 `_confirm_claim` 成功的 Window；仅用于执行诊断，不代替机会公平。 |
| `allocation_cursor` | `dispatcher_scope` 级任务轮转位置；worker 重启不能归零，不能在每个 shard 为同一任务重复最低份额。 |

`remaining_claim_windows` 对有 deadline 的任务按剩余时间计算，最少为 1；允许 late/recovery 收口的过期 Action 固定为 1 且不能反写按时完成。无 deadline 的 continuous/ordinary 任务使用版本化 `continuous_fairness_horizon_windows`（默认 60），业务 debt 只取该 horizon 内已经到期的欠额，禁止用 lifetime backlog 放大份额。该 horizon 只服务公平计算，不改变任务目标或生命周期。

上表先按 `lane + deadline Window + pacing class` 聚合业务义务：相同 deadline 的频道消息/AI coverage/群日 volume/search 目标合并 debt 后只做一次 ceil，不同 deadline 分 bucket；父任务 `required_claims` 为各 bucket required 之和再受 task due candidates 上限约束。不得把任务总债务除以最晚 deadline，也不得逐个微义务 ceil 放大；父任务份额内按最早 deadline、未满足比例和义务 cursor 选 Action。

分配顺序固定为：

```text
所有 required_claims > 0 的任务
  -> 以 allocation_business_task_id=coalesce(admission_execution_sponsor_task_id,parent_task_id,task_id) 聚合父业务任务
  -> 跨全部 shard 按 scope cursor 做最低保护轮转，每任务最多先分 1 个
  -> 容量不足时在下一 Window 从未服务位置继续
  -> 剩余容量按未满足 required_claims 比例使用最大余数法
  -> 同余数按 last_opportunity_window 最旧、cursor、task_id 稳定决胜
  -> DispatchClaimTaskAllocation 固化任务总份额
  -> 在父任务获配份额内按 fulfillment/admission lane cursor 固化 lane 份额
  -> 由 DispatchLaneShardSolver 在单次精确 task-lane-to-shard 三层匹配中映射到有候选且有容量的 shard
  -> 按账号池安全资格选择 Action
```

约束：

- `ai_group_daily`、`channel_comment`、`channel_like`、`channel_view`、`search_click` 和 ordinary 父业务任务进入同一公平合同；membership/admission child 以父任务 ID 聚合，不得另取最低份额。不得设置 search 永远优先 AI、AI 永远优先频道或媒体永远降级的全局静态顺序。
- 父任务同时存在 fulfillment/admission claimable debt 且获配 `>=2` 时至少各 1；只获配 1 时使用持久 lane cursor 跨 Window 轮转。`target_admission_retry` 和其他已设计的 admission child 只在 admission lane 内优先，不得抢占其他任务 Reservation或永久饿死 ready fulfillment；纯搜索点击没有 admission child。
- 同一 admission key 被多个父任务复用时，以唯一 `AdmissionExecutionLease` 选一个 sponsor 父任务出资；其他父任务不创建重复 Action/Reservation，ready 后只复检共享事实。pre-Gateway 可 CAS 换 sponsor，Gateway-started/unknown 不转。
- admission sponsor election/rebind 在独立短事务提交，不与 Scope/Window/Action 锁同持；Reservation 固化 lease version，claim/Gateway 前版本不符则释放并等下一 `dispatch_allocation_epoch`。
- `DispatchClaimTaskAllocation` 对 `(window, dispatch_allocation_epoch, tenant, allocation_business_task_id)` 唯一，`DispatchClaimShardAllocation` 对 `(window, dispatch_allocation_epoch, shard)` 唯一，Reservation 继承同一 epoch；其中 tenant 是当前单一业务租户的隔离键，不产生 tenant 级二次分配。各 lane/shard Reservation 不得超过对应 lane/任务/shard 份额。单次精确匹配须在存在可行 task-lane-to-shard 映射时填满容量；最终无法映射的需求写 `shard_mapping_insufficient`。
- Window 的中央权重发布状态只允许 `allocation_state=rebuild_required|ready`，并保存单调递增的 `rebuild_input_version` 与最近已发布版本的 `ready_rebuild_snapshot_hash`。TaskAllocation/ShardAllocation/Reservation 各保存所属版本的 `dispatch_rebuild_snapshot_hash`。尚可领取 Window 在 `ready` 收到首批非空释放时只把中央 epoch 加 1 并开启一个 pending rebuild wave；`rebuild_required` 期间的后续释放复用该 pending epoch，只增加输入版本。新 epoch 的三类 allocation 行、相同 hash 与 `ready` 必须原子提交；旧 epoch 未释放承诺仍按自身版本正常收口。Window 已结束后只收口释放事实，不再创建无用途的新 epoch。
- 通用 claim 锁顺序固定为 `Scope -> Window -> TaskAllocation -> ShardAllocation -> Reservation -> Action`。纯搜索点击在 Reservation 与 Action 之间固定追加 `search carrier（如有） -> SearchClickOpportunityAssignment -> 搜索 consumptive 子预留`；commit、`_confirm_claim`、Gateway 前最终守卫、release 与 Reconciler 使用同一顺序，缺失层只跳过不得换序。
- `max_claim_per_task`、`max_claim_per_account_pool` 和共享 scope capacity 只保护本轮运行时；不能转换为业务目标、完成门禁或全天容量不足结论。
- 账号、代理、目标、Telegram 速率和媒体安全限制继续在获配份额内校验。无安全 Action 可领时保留真实 blocker，并在下一 `dispatch_allocation_epoch` 重新计算。
- 未获配任务必须记录需求、获配数量、cursor、`shared_dispatch_capacity_insufficient` 和下一 Window，不得静默 pending。
- scope cursor 在 allocation 短事务内持久推进；Reservation 机会与实际 claim 分开记录，资格变化或无安全 Action 不得让同一任务长期占据 cursor 首位。

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

### 3.2.3 共享 claim 容量与热事务边界（2026-07-28 修订）

本节对应产品真相源 `docs/03-feature-designs/all-task-fulfillment-recovery-prd.md`，用于支撑 AI 活群、评论、点赞、浏览和搜索点击的统一履约恢复。

生产 deadlock 与全局 claim 只有 20 的事实证明，以下三个参数不能继续共用一个 `min()` 结果：

| 参数 | 权威含义 |
| --- | --- |
| `ACTION_CLAIM_LIMIT` | 单次数据库候选查询和 claim 批量上限 |
| `DISPATCHER_CONCURRENCY` | 单个 Dispatcher 进程的执行并发 |
| `DISPATCHER_SCOPE_CAPACITY` | 所有共享同一 `dispatcher_scope` 的 worker 合计在途上限 |

共享 scope 容量由部署拓扑显式配置，且必须同时满足：

```text
DISPATCHER_SCOPE_CAPACITY
<= active_dispatcher_count * DISPATCHER_CONCURRENCY
<= database_writeback_connection_budget
<= telegram_gateway_safe_inflight_budget
```

当前 `ACTION_CLAIM_LIMIT=100` 不能直接解释为全局容量 100。各 worker 上报相同 scope、容量和配置版本；版本不一致时停止新增 claim 并暴露 `dispatcher_scope_capacity_mismatch`，不由最后写入的 worker 覆盖全局值。

claim 热事务锁顺序固定为：

```text
DispatchClaimScope
-> DispatchClaimWindow
-> DispatchClaimTaskAllocation
-> DispatchClaimShardAllocation
-> DispatchClaimReservation
-> Action
```

`DispatchClaimTaskAllocation` 不得省略：Allocation 事务、各 claim worker、reconciliation 和故障恢复必须全部复用上述六级顺序。禁止任何入口从 ShardAllocation/Reservation 反向锁 TaskAllocation；需要重算任务份额时先结束当前事务，再从 Scope 开始新事务。

该事务不得更新 `Task.stats`、`Task.last_error`、AI coverage 或频道消息履约账本。未服务任务统计从 Reservation/Allocation 派生，由独立 reconciliation 短事务更新。

Planner 日履约收口固定拆成三个提交边界：

1. 读取不可变规划输入并提交 Task 运行边界；
2. 按主键稳定顺序批量更新任务专用 ledger；
3. 追加 fulfillment decision/audit。

任何一步的投影失败不得回滚前一步已经正确落库的 Action、Attempt 或远端事实，但必须显式记录错误并让后续 reconciliation 重建；不得捕获后静默跳过。

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

#### 4.2.1 搜索点击共享机会分配

搜索完成优先不能把 `account × keyword × authorization_slot × proxy_route` 的候选组合数当成安全容量。同一账号额度、关键词额度、授权槽、代理或 Gateway budget 可以出现在多条候选路径中，必须先做共享资源约束匹配：

```text
task_day_ledger + target + click_obligation_ordinal
  -> enumerate candidate paths with resource key/version vector
  -> read-only future-window projection (no writes/holds)
  OR current DispatchClaimWindow
  -> Scope / Window(allocation_state=ready)
  -> epoch-stamped TaskAllocation / search fulfillment lane / Shard Reservation
  -> unique SearchClickAssignmentEpoch(open) within granted shares
  -> SearchClickAssignmentSolver deterministic lexicographic exact matching
     (max click assignments -> max served due tasks -> max-min task fairness
      -> stable path order)
  -> CAS search consumptive/eligibility sub-reservations
  -> bind existing central Reservation
  -> SearchClickOpportunityAssignment
  -> bind Action
  -> one outcome finalize
     (assignments + optional non-empty release set + join/start one rebuild wave)
```

每个 `(dispatch_claim_window_id, dispatch_allocation_epoch)` 先建立唯一持久 `SearchClickAssignmentEpoch`，保存 `search_click_assignment_epoch/solver_problem_hash/solver_input_hash/solver_owner_lease_id/solver_claimed_at/state=open|finalized/solver_result/release_unit_set_hash/outcome_hash/next_dispatch_allocation_epoch(nullable)/rebuild_input_version_after(nullable)`。problem hash 是 carrier-independent 的业务问题图，规范化包含 `solver_contract_version`、稳定业务义务、连通分量候选/资源和相关公平输入，排除 Window/dispatch/search epoch、Reservation/ordinal/assignment ID、carrier 派生份额、worker/lease、时间和随机值；input hash 在其上加入本次 carrier、精确 Reservation unit/version 与中央份额版本。字段集合或排序规则变化时提升两个 payload 内的 contract version，不新增独立状态列。release hash 对稳定排序的 `(window,reservation,ordinal,reason_code,resource_snapshot_hash)` 精确集合计算，空集合也保存确定性空 hash；outcome hash 同时覆盖 carrier 身份、problem/input hash、solver result、全部 matched assignment identity/version、release hash和实际 wave epoch/input version。

唯一行必须在创建为 `open` 的同一事务原子绑定当前有效 worker lease；唯一键冲突的其他 worker只能回读。只有仍持有该 `solver_owner_lease_id` 的 worker 可以执行一次计算和 outcome finalize；lease 仅作存活 fencing，健康 owner 求解期间持续续租，固定租约时长、心跳周期或续租次数不得变成 solver deadline。只有进程失联、fencing token 失效或明确丢失续租所有权时，recovery 才直接按 `abandoned` finalize，不转移 ownership、不重跑求解，也不建立 attempt/history。finalized 重放只回读同一 problem/input/release/outcome/wave 结果，任一身份、hash 或版本不一致都保持 `release_fact_incomplete`。该行是首次 `no_candidate|optimal|abandoned` 及其 release set 的权威幂等载体，即使没有 assignment 也必须存在，但不得承载 finalize 后 assignment 的再次释放。

`SearchClickOpportunityAssignment` 不是 Telegram 成功事实，也不进入运营配置；它只在当前 Window `allocation_state=ready` 的 commit 模式创建并引用 epoch，保存 ordinal、`assignment_version/expiry`、`dispatch_claim_window_id/task_allocation_id/reservation_id/fulfillment_lane_claim_ordinal`、capacity window、账号/关键词 quota key 与版本、授权版本、代理 binding generation、协议样本版本、Gateway capacity 版本、Action 绑定和 `reserved|action_bound|claimed|gateway_started|unknown|consumed|released` 状态。面向业务 `scheduled_end` 的只读 projection 必须先以同一快照的全部任务债务逐 Window 重放中央 TaskAllocation/lane/shard 分配，不能把全部 scope capacity 给搜索；它不写 assignment/Action/hold，并返回 `projection_not_reserved=true`。当前 Window 只有 assignment、搜索专属子预留和中央 Reservation 原子绑定成功才进入 `committed_click_opportunity_count`。assignment 不计算技术 `latest_safe_start_at`，也不引入协议/Dispatcher/Gateway 性能预算。一个 search assignment epoch 只求解一次并只成功 finalize 一次；`optimal` 释放 unmatched，`no_candidate|abandoned` 释放全部未领取 unit。`optimal` finalize 必须同时确认 Window 仍可领取、`allocation_state=ready` 且当前 `dispatch_allocation_epoch` 与 search epoch 完全一致；Window 正在 rebuild、已经在更高 epoch 回到 ready 或已结束时都只能 `abandoned`。其 release set 分别加入当前 wave、从更高 ready 版本开启下一 wave，或仅收口事实。空集合不改变中央版本。

账号尚未求解时，search source 的中央份额使用虚拟 ShardAllocation `(1,0)`；同一完整输入中的普通 Action 始终按生产 `runtime_account_shard_total` 建 shard demand，不能因搜索 Planner 抢先建窗而降为 `(1,0)`。求解并固化 assignment 后，执行归属按 `assignment.account_id % runtime_account_shard_total` 唯一路由，confirm/release 仍锁原虚拟 ShardAllocation/Reservation。不得用虚拟 `(1,0)` 与四 worker 配置做 total/index 等值判断，否则所有预绑定 Action 都会被跳过；非归属 worker 只从本地 plan 排除，不产生 release。
>
> **P0 持久快照闭环：** `SearchSolverSnapshotAssembler` 是 problem/input/component hash 的唯一生产者。它在同一一致性数据库快照内建立不可变 `SearchSolverProblemSnapshot`、全部 component 的稳定 node/edge/resource/fairness payload 与 hash，以及每个 Reservation/ordinal 唯一的 component binding；共享资源/fairness key 必须连接到同一 component，无候选 unit 也有零边 component。open epoch、完整 snapshot/component/binding、两个 hash 和 owner lease 同事务落库后才调用 `SearchClickAssignmentSolver`。solver 只能读持久快照，不得额外查库；owner 丢失 recovery 使用原 binding/hash 释放，exclusion supersede 也复用同一 Assembler/canonicalization。禁止恢复时重组旧图、手拼第二套 hash、半快照 open 或把组装错误冒充求解结果。
>
> `stable_component_key` 必须由 contract version 与稳定排序的业务义务、候选 edge、资源 node、fairness node 身份确定，不能使用随机 ID或包含 carrier/worker/时间；component hash 再覆盖全部当前值/version。所有影响 solver 输出的读取都必须进入 canonical payload，最低包括 `hard_safe_remaining_capacity`、同一冻结 `account_quota_key/capacity_window_key` 内的 `confirmed_click_count_today`、持久 `last_click_opportunity_at`、`persistent_account_cursor` 及来源 version。正常 `no_candidate|optimal` finalize 前必须在短 PostgreSQL `SERIALIZABLE` 事务内用同一 Assembler/候选谓词做只读 revalidation，重算 problem/input hash并比较所有 source version；任一 phantom、资源、排序、公平或 carrier 输入漂移即整轮 abandoned，按原 binding 释放全部未领取 unit并重建分片权重。serialization/数据库/CAS 失败整批回滚，recovery 只按原快照 abandoned；不得提交、自动重放或重新求解旧结果。

当前 epoch 的 `claim_class=search_click` fulfillment Reservation 从中央 `ready` 发布到首次 `SearchClickAssignmentEpoch` finalize 前，全部 unit 由搜索物化管线独占。通用无 Action/unclaimed/expiry reclaimer 必须跳过，不能把“求解尚未创建 assignment/Action”误判为空占。Window 可领取且结果行缺失时，首个有效 worker创建 open 行并绑定 lease后正常单次求解；Window 已结束但结果行仍缺失时，recovery 在一个事务创建并直接 finalize abandoned，solver 调用数为 0。任务暂停、停止、删除或 due 消失只让 optimal 前置失败并由原 epoch释放，不能建立另一类首次 carrier。首次 outcome finalize 后，每个来源 search Reservation 必须满足 `bound_count + claimed_count + released_count = reserved_claims`；之后 bound unit 只走 release batch，claimed unit继续 Gateway/Attempt 收口。通用 reclaimer 若触碰这些 unit，写 `search_reservation_ownership_violation` 并隔离，不得接受为合法释放。

`allocation_state=ready` 只控制新中央版本和新 search epoch/assignment 发布。optimal 同时生成 matched 与 unmatched 时，unmatched release 可把 Window 置为 `rebuild_required`；已经绑定的旧 epoch Action 在来源 Reservation/assignment/资源/Action version 仍有效、Window 与业务 deadline 未结束时继续 `_confirm_claim -> Gateway`，不等待新 ready，也不读取未发布权重。只有 Window/业务到期或 Gateway 前资格失效才走 release batch；任何新未绑定份额仍必须等待新权重和 ready 原子提交。

同一 click ordinal 同时最多一条非 released assignment；同一 `(dispatch_claim_reservation_id,fulfillment_lane_claim_ordinal)` 也同时最多一条非 released assignment，claim ordinal 必须落在 `1..reserved_claims`，避免一份中央容量槽被重复绑定。Reservation 增加 `bound_count/released_count`，满足 `bound_count + claimed_count + released_count <= reserved_claims`；assignment 绑定时增加 bound，claim 时从 bound 转 claimed。搜索 epoch 首次 finalize 前的 unmatched 由该 epoch outcome 释放；epoch finalized 后，`reserved|action_bound` assignment 因 Gateway 前路径失效、Action 不再到期或 assignment 到期而释放时，必须由唯一 `DispatchAllocationReleaseBatch` 原子执行 assignment `-> released`、`bound_count -= 1`、`released_count += 1`、Task/shard/Window unclaimed 各减 1并插入永久 exclusion，不能改写或重开原 search epoch。账号/关键词等调用后可能消耗的额度使用搜索专属 `consumptive` 子预留；授权、协议和代理有效性使用版本化 `eligibility`。Dispatcher/Gateway 及全任务共享代理的 `inflight` 只由中央 `DispatchClaimReservation` 占用一次，assignment 仅保存引用，禁止二次预留。Gateway 调用结束即释放中央 inflight，unknown 只继续占用 ordinal 和可能已消费 quota hold，不能无限占用在途容量。只有尚未 `_confirm_claim` 的 `reserved|action_bound` assignment 使用不晚于 Claim Window 结束的 expiry；`_confirm_claim` 同一 CAS 将中央 Reservation unit 转 active、Action 转 executing、assignment 转 `claimed`，此后 Window 结束不得释放，直到 Gateway/Attempt 收口。多个搜索 Task 共享搜索资源账本；projection 扣除既有中央份额/资源事实但不写 hold，commit 受每 Task `fulfillment_lane_claims` 和 shard Reservation 上限约束。纯 click 匹配依次固定当前 Window 最大 assignment、最大受服务到期父任务数和按 remaining click 比例的最大最小任务公平向量；任务最优值固定后，账号严格按 `hard_safe_remaining_capacity DESC -> confirmed_click_count_today ASC -> last_click_opportunity_at ASC -> persistent_account_cursor ASC` 稳定决胜。每个 Task 的 `assignment_fairness_key=(allocation_business_task_id,task_day_ledger_id,target_id)` 连接其候选；不建立 admission distinct/budget 目标。当前单用户部署不增加 tenant 级公平层。

`SearchClickAssignmentSolver` 在冻结的无事务快照上完成。commit 事务重新按 `Scope -> Window -> TaskAllocation -> ShardAllocation -> DispatchClaimReservation` 复核中央版本，再依次锁 search epoch carrier、按稳定 unit key 处理既有 assignment、按稳定 resource key CAS 搜索 consumptive 子预留，最后创建或锁 Action；`_confirm_claim`、Gateway 前最终守卫、release 和 Reconciler 复用该相对顺序。禁止持锁求解，或从 Action、assignment、搜索资源/carrier 反向锁中央行。

若某个中央 search fulfillment 份额无法求解或绑定，不在原份额重试。系统把本 epoch 全部未领取 `(dispatch_claim_reservation_id, fulfillment_lane_claim_ordinal)` 组成 release set；同一 outcome finalize 事务为每个 unit 写 `DispatchAllocationExclusion(window,source_reservation_id,source_ordinal,task,lane,shard,resource_snapshot_hash,reason,evidence,release_carrier_type,release_carrier_id)`，按 Reservation 汇总增加 `released_count`、按原 Task/shard/Window 汇总扣减 unclaimed，把 `SearchClickAssignmentEpoch` 从 open CAS 为 finalized。release set 非空且 Window 尚可领取时：`ready` 只递增一次 `dispatch_allocation_epoch`、把 Window 置为 `allocation_state=rebuild_required`、递增 `rebuild_input_version` 并保存 pending epoch；Window 已为 `rebuild_required` 时只递增输入版本并加入现有 wave。Window 已结束时只收口释放事实，不生成新 epoch。集合为空时中央状态不变。claimed/active/bound unit、其他有效旧 Reservation 与公平 cursor 不回退，业务 click 欠额不减少。

`DispatchAllocationReleaseBatch` 专门承载 search epoch finalized 后的释放，至少保存 `dispatch_claim_window_id/source_dispatch_allocation_epoch/release_trigger_type/release_trigger_key/candidate_unit_set_hash/candidate_unit_count/release_unit_set_hash/release_unit_count/already_released_unit_count/precondition_lost_unit_count/outcome/outcome_hash/next_dispatch_allocation_epoch/rebuild_input_version_after/finalized_at`。唯一键固定为 `(window,release_trigger_type,release_trigger_key)`；trigger 必须由 assignment/version 的 Gateway 前终态、Action/version 不再到期或 Window/source epoch 到期等不可变事实派生，禁止使用随机 batch ID、worker ID 或扫描时间。candidate hash 是不可变 trigger 的完整输入身份，release hash 是统一锁内分类后实际释放的 effective set 结果。

每个 candidate unit 必须在 `DispatchAllocationReleaseBatchItem` 保存 batch、Reservation/ordinal、assignment/expected version、nullable bound Action/expected Action version、`effective_released|already_released|precondition_lost`、锁内 observed assignment/Action state/version 和 nullable first carrier，唯一键为 `(batch,reservation,ordinal)`。candidate hash 对全部稳定 item 输入及两个 expected version 取 hash，release hash 只对 effective item 取 hash。batch `outcome_hash` 覆盖 carrier 的 Window/source epoch/trigger、candidate hash、稳定排序后的全部 item 分类及 expected/observed assignment/Action version、first carrier、release hash、三类 count、outcome 和实际 next epoch/input version。batch 与全部 item 必须和 Action/assignment/计数释放、outcome hash及 wave 在同一事务提交；汇总计数不能替代 unit 分类事实。

batch 汇总必须从 item 唯一重算，满足 `candidate_unit_count = release_unit_count + already_released_unit_count + precondition_lost_unit_count`。`applied` 固定为 `release_unit_count > 0` 且两个 no-op 计数均为 0，`no_op` 固定为 `release_unit_count = 0`，`mixed` 固定为 `release_unit_count > 0` 且至少一个 no-op 计数大于 0；空 candidate 同样 finalize no-op。任一 count/hash/outcome 不守恒时禁止提交。finalized 重放只有 candidate、逐 item 结果、release/count/outcome、wave 与重算 outcome hash 全部一致才只读返回；同 candidate 的结果错绑保持 `release_fact_incomplete`，不同 candidate 返回 `release_batch_input_conflict`。

`precondition_lost` 只使冻结旧 assignment/Action version 的 trigger 成为 finalized no-op。状态机禁止从 claim/Gateway/unknown/consumed 倒退到 `reserved|action_bound`；observed 已越过该边界时永不再释放。只有 observed 仍为新的 `reserved|action_bound` pre-Gateway version（例如并发 replacement/资格复核仅推进 version）且释放条件仍成立，产生该版本的状态变更事务/outbox 才按新版本生成新的 trigger key和 candidate hash。禁止重开旧 batch或无状态事件轮询重试，也不能让 Gateway 前新版本占用因旧 no-op 永久泄漏。

release batch 在取得中央前缀与 carrier 后，按稳定 unit key 锁 assignment、按稳定 resource key 锁搜索 consumptive 子预留，最后按稳定 Action ID 分类：指定版本仍为 `reserved|action_bound`、不存在任何状态 exclusion，且 nullable bound Action 不存在或仍为 expected version 的 pre-Gateway 状态时才进入 effective set；`action_bound` 的 Action 必须已是匹配 trigger 的 pre-Gateway terminal，或由本事务转为原因对应的 `failed|skipped` 终态。已经 `released`、永久 exclusion 存在且原 Action 已不可领取时记 `already_released` 并回读原 carrier；已经 `claimed|gateway_started|unknown|consumed`，或 assignment/Action 版本漂移、Action 已 executing/Gateway-started 时记 `precondition_lost`。后两类不写 exclusion、不改计数或 Action；assignment/exclusion 已释放但 Action 仍可领取等矛盾使本事务整批回滚，随后按下方独立 quarantine 协议处理。batch 的分类、effective set、bound Action 终态/lease/active、assignment、exclusion、bound/released/unclaimed 计数和 rebuild wave 在一个事务直接 finalize；保留 Action 绑定作证据，提交后禁止 `assignment=released + Action pending|claiming`。effective set 为空仍写 no-op batch且不推动 rebuild。这样不同 release trigger 命中同一 unit，以及 release 与 `_confirm_claim` 的竞争，都只能有一个释放/领取结果，不会把永久唯一键冲突变成无限重试。同 trigger 同 candidate hash 重放只回读，不同 candidate hash 返回 `release_batch_input_conflict`；全部 effective unit 必须一次提交。

每条 exclusion 的 `release_count=1`，永久唯一键为 `(window,source_reservation_id,source_ordinal)`；`resource_snapshot_hash` 是释放证据和新权重适用性字段，不属于幂等键，carrier 只能是首次 search outcome 或后续 release batch。该 hash 按 `reason_code` 只包含本 unit 的 Window、Task/ledger/target、solver input 或 assignment/Action expected version，以及实际导致无法绑定、资源饱和或终结的额度窗口、授权、代理、协议/CAPTCHA、Gateway 容量 key/version；无关 Task/shard、worker/lease、扫描时间和随机值禁止进入。只有这些相关规范化字段变化才可 supersede；`no_feasible_search_path|search_solver_abandoned` 固定绑定原业务问题分量的 `solver_problem_component_hash`，完整 `solver_input_hash` 只用于 carrier outcome 幂等，换 Window epoch、Reservation、ordinal 或 worker 不构成业务问题变化。exclusion 后续转为 `superseded|expired` 也不能为同一旧 unit 再插一行或再次增加 `released_count`；新事实下重新获配必须使用新 epoch 的新 Reservation/ordinal。两类 carrier 共用 finalize helper：事务按 `Scope -> Window -> TaskAllocation -> ShardAllocation -> Reservation` 加锁，同层多行按主键排序，再锁 carrier、按 `(reservation_id,ordinal,assignment_id)` 锁既有 assignment、按稳定 resource key 锁搜索 consumptive 子预留，最后锁 Action；不存在的层只跳过不得换序。首次 `optimal` 先验证 matched ordinals 与 release ordinals 互斥、全部 matched 绑定及 release set 的 CAS 前置条件，再验证 Window 仍可领取、ready 且当前 dispatch epoch 与 search epoch 完全一致；任一条件失效就改为 `abandoned`，不写部分 assignment，并释放全部仍未领取 unit。不能因 Window 已在更高 epoch 重建回 ready 而提交旧 matched。首次 outcome 的 unit 不得已被占用，并守卫 `bound_count + new_matched_count + claimed_count + released_count + unbound_release_count <= reserved_claims`。后续 batch 只对 effective set 守卫 `bound_count >= bound_release_count` 并原子执行 `bound_count -= bound_release_count/released_count += bound_release_count`；`already_released|precondition_lost` 不进入 release count。Task/shard/Window 的原 unclaimed 均不得小于 effective release count。真实状态/计数矛盾时整批回滚。合法 no-op 分类不进入 reconcile/retry。重放只回读相同 carrier/outcome/hash，不能部分释放、双扣或在同一 wave重复递增 epoch。

> **P0 exclusion 适用性确认：** `no_feasible_search_path|search_solver_abandoned` 的每个 unit 必须从 `solver_problem_hash` 投影其连通分量 `solver_problem_component_hash`，并把后者写入 `resource_snapshot_hash`。仅新建 epoch、换 Reservation/ordinal/worker 或推进 carrier 版本不得改变 component hash；只有该分量的业务义务、候选、资源、公平输入或 contract version 变化才 supersede。否则会形成“abandoned -> 重建 -> 仅因 epoch 改变而再次获配”的循环。

矛盾状态不能在将被回滚的 release 事务中声称已 quarantine。回滚后独立 consistency writer 重新取得同一中央锁前缀；仅在矛盾仍存在时，以 `(window,reservation,ordinal,issue_fingerprint)` 幂等持久化 active `consistency_quarantine`、observed assignment/Action/exclusion/claim/Gateway/count version 与原 trigger。active issue 暂停该 trigger 的定时重试和包含该 unit 的原子 batch，其他独立 unit/trigger/任务继续。

`DispatchReservationReconciler` 分支前先验证合法 release fact set：首次 outcome 必须为 finalized search epoch + `release_unit_set_hash` 内 unit + matching exclusion，post-finalize 必须为 finalized release batch + `effective_released` matching item + matching exclusion，且 carrier/unit/hash/reason/version/计数一致；只有 carrier、只有 exclusion、缺 item 或错绑时保持 `release_fact_incomplete` 对象级 quarantine，不能自动判 released。完整事实只允许四个互斥分支：①合法 release fact set 且无 claim/Gateway，以逐 unit 事实为权威；存在 assignment 时对齐为 released，首次 outcome 的未绑定 unit 保持无 assignment；终结仍可领取的 bound Action并清 lease/active，再重算各层摘要，使该 unit 只贡献一次 released；②只有 released assignment、无任何 release 组件且无 claim/Gateway，按 Action 绑定恢复 `reserved|action_bound`、推进版本并产生新 trigger；③有 claim/Gateway且无任何 release 组件，不回滚远端边界，只把 assignment/Reservation 对齐到 `claimed|gateway_started|unknown|consumed`；④合法 release fact set 与 claim/Gateway 同时存在时写 `release_claim_fact_conflict`，保持该 unit active quarantine，禁止自动删除 release 组件、回滚 Gateway、选边、调整该 unit 的 released/claimed 计数、resolve 或忙重试。只有前三个分支提交后才 resolve并事件唤醒原 trigger；第四分支只隔离该 unit，完整 click evidence可照实入账，但相关 ledger 在 quarantine 清除前不得通过 E4。任何路径都不重跑搜索求解，也不把对象隔离升级为新整任务门禁。

中央 allocation 行必须固化 `dispatch_allocation_epoch`：TaskAllocation 唯一键包含 `(window,epoch,tenant,business_task)`，ShardAllocation 唯一键包含 `(window,epoch,shard)`，Reservation 继承同一 epoch 且不得改绑。`DispatchLaneShardSolver` 只在 `rebuild_required` 下冻结 `dispatch_allocation_epoch + rebuild_input_version + dispatch_rebuild_snapshot_hash`；hash 的规范化 payload 包含 `(window,pending_epoch,rebuild_input_version)`、全部 task/lane/shard 的 due/eligibility 稳定键/当前值/版本、active exclusion 的 unit/state/reason/resource snapshot、全部仍有效旧 Reservation 的身份/承诺计数/版本、scope/shard 容量与影响分配的配置值/版本，稳定排序后取 hash，排除 worker/lease、扫描或墙钟时间、进程身份和随机值。计算在无事务快照上进行，提交前按中央锁序重读同一规范化输入并重算 hash；epoch、input version 或 hash 任一变化都使旧结果提交失败，包括未产生 release batch 的 due、资格、容量或配置变化。成功提交必须一次写入全部新 epoch allocation/reservation，每行固化同一 `dispatch_rebuild_snapshot_hash`，同时将 Window 的 `ready_rebuild_snapshot_hash` 写成该值并 CAS 为 `ready`。计算、CAS、数据库错误或崩溃都丢弃未发布权重，由下一 drain 从最新事实重新创建，禁止部分权重可领取；零余额也提交带 hash 的空 `ready`。Window 已结束不再重建该 Window。旧 released unit 不可再 claim，已 bound/claimed/active 和其他未释放旧 Reservation 继续按自身版本收口。相同 reason-scoped snapshot 下，新 epoch 以 active exclusion unit 数扣减原 task/lane/shard 可再次获配数，但其他 shard/资源向量仍可获配；只有该 reason 直接依赖的规范化资源版本变化才将旧项 superseded，无关 Task/shard、worker/lease 或扫描时间变化保持 active；Window 结束 expired。

`dispatch_rebuild_snapshot_hash` 必须覆盖 solver 实际读取且影响输出的全部业务输入。除上条最低集合外，至少还包括 `dispatch_rebuild_contract_version`、Scope/Window/Shard capacity/active/unclaimed 当前值与版本、scope/task-lane/shard fairness cursor 与版本、parent/sponsor 聚合输入；新增影响输出的读取必须进入 payload并提升 contract version，版本只属于 hash payload。纯诊断字段、worker/lease、时间、进程和随机值不得进入。旧 Window claim、并发 Window cursor 或 sponsor 变化即使没有推进 `rebuild_input_version`，也必须使 precommit hash 不一致。

用 immutable `DispatchRebuildInput` 消除双真相源：assembler 负责完整数据库读取、稳定排序/序列化和 hash；`DispatchLaneShardSolver(input)` 必须是无数据库/全局状态读取的纯计算，并在输出上回传同一 input hash。precommit 在中央锁序内重新运行 assembler；禁止只复核一个手工 version vector，而让 solver 读取未入 hash 的旁路事实。

原子发布事务固定使用 PostgreSQL `SERIALIZABLE`：短事务先按中央锁序取得 Scope/Window，再重新 assemble 完整 input、比较 hash并写入全部新 allocation/reservation、Window ready/hash。assembler 的行查询与候选谓词必须都在该事务内，使 rehash 后到 commit 前的 update/phantom 触发 serialization failure。失败即回滚并废弃 solver 输出，禁止框架自动以旧输出重放；下一 drain 重新 assemble/solve。若底层不是 PostgreSQL，只能替换为覆盖相同行集和谓词的显式 version-row/predicate fencing，不能退化为 Window 行锁或单一版本 CAS。

`dispatch_rebuild_contract_version` 或搜索 `solver_contract_version` 的发布禁止新旧 Dispatcher 混跑。版本只属于各自 hash payload，不保存 solver 运行历史；部署先停止旧版本取得新 ownership，确认旧进程全部终止且无旧版本数据库事务仍可提交，之后才启动新版本。旧内存输出全部作废，pending rebuild 由新版本重新 assemble/solve；旧 owner 遗留 open search epoch 在 fence 后直接 abandoned 并释放未领取 unit。无法证明旧版本已失去写资格时 Release Gate 失败，不能以混合版本 canary、旧输出恢复或 ownership 转移绕过。

finalized `SearchClickAssignmentEpoch`、`DispatchAllocationReleaseBatch`、`DispatchAllocationReleaseBatchItem`、`DispatchAllocationExclusion` 与来源 Reservation 在迟到 writer 仍可访问期间不得单独物理删除；联合归档前必须先 fence 旧 worker。归档只能把大 payload 冷存，主库永久保留不可删除/复用的 carrier key/hash、batch item candidate unit、assignment/Action expected+observed version、classification/first-carrier 引用与 `(window,reservation,ordinal,released)` identity tombstone，不能通过清理重新获得 carrier 或 unit 唯一键，也不能让逐 unit outcome 只能依赖日志还原。

最大最小任务公平只覆盖当前至少有一条真实 eligibility 路径的 due Task；无路径 Task 保留 blocker，但不清零其他 Task 的合法机会。以冻结值计算 `task_fairness_ratio=assigned_count/max(remaining_click_count,1)`，对升序 ratio 向量做字典序最大化；离散余数用业务 `scheduled_end`、最久未获机会和持久 cursor 决胜，不使用综合分值。

候选生成只保留真实存在且通过当前 eligibility 的账号—授权槽—代理绑定—关键词路径，并按资源向量去重；合法 repeat 路径不能因账号遍历顺序被预先删除，也禁止理论笛卡尔积后 top-N 截断。冻结 `search_click_assignment_epoch` 后，以“共享同一 click ordinal、任一资源 key 或同一 task fairness key”为边建立候选约束图并拆成互不共享约束/目标的连通分量；每个分量只使用 `x[ordinal,path]` 与 `z[task]` 独立求解，保证每 ordinal 最多一条路径、每资源窗口 usage 不超 available、受服务 Task 有已选路径。commit 模式另强制 `sum(x_task) <= fulfillment_lane_claims(task)`，并使 shard usage 不超过绑定的中央 Reservation；projection 使用同一上限的只读未来窗口估计但不写占位。目标依次固定 assignment 数、served-task 数、最大最小任务公平向量和稳定 path 顺序，后阶段不得降低前阶段最优值；不建立 `y[task,account]`、admission distinct/budget key 或 distinct gain 阶段。task fairness key 连接跨 ordinal 的公平目标，保证分量最优向量之和仍是全局字典序最优。结果闭集为 `no_candidate|optimal|abandoned`：`optimal` 提交已证明匹配并释放 unmatched unit；`no_candidate|abandoned` 释放全部未领取 unit。尚可领取 Window 的非空集合加入唯一 pending rebuild wave，空集合只 finalize 搜索 epoch，已结束 Window 只收口释放事实。相同 epoch 不重试，禁止以贪心、遍历顺序或不可验证部分解降级。

架构合同不设置求解器技术 deadline、性能预算、图规模基线或 p99 指标，也不为这些指标增加 retry/降级状态。实现可采用等价的数据结构，但不能抽样、固定 batch、top-N 截断或提交部分解；本次 search epoch 无法一次返回完整可验证结果时直接 `abandoned`，把全部未领取 Reservation 组成一个 release set 整批原子释放。尚可领取 Window 的非空集合加入唯一 pending rebuild wave；集合为空时只 finalize 为 `abandoned`，已结束 Window 只收口事实，均不改变中央 epoch。

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
- `DISPATCHER_SCOPE_CAPACITY` 必须使用本节连接预算验证，不能由 `ACTION_CLAIM_LIMIT`、单个 worker 配置或当前 Action 数量反推。

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
- 使用 `FOR UPDATE SKIP LOCKED` 原子预领取 pending action，claim 条件必须包含任务状态、任务未删除、有效的 Window Reservation 和 action 到期时间。
- claim 改为两段式短事务：DB 预领取写入 `claiming`、`claim_owner`、`claim_token`、`claim_expires_at`，事务外获取 Redis token bucket reservation、账号 in-flight lock、代理和目标配额，再通过条件更新写入 `executing`、`lease_owner`、`lease_expires_at`。
- claim 阶段提前完成最终账号选择和账号转派。
- claim 阶段按 §3.2.1 的最低保护轮转与最大余数份额分配执行项；账号池公平性和等待时间只在已获任务份额内决胜。
- 落地 `plan_batch_key` 和 `action_dedupe_key`，避免 Planner 重跑造成重复 action。
- 新增 `execution_attempts` 或等价执行尝试记录，并用 `gateway_call_started_at` 明确是否已经进入 Telegram Gateway 调用边界。
- 新增数据库唯一约束，覆盖 action 幂等、listener event 去重、execution attempt 幂等和 daily stats 幂等汇总。
- Planner 增加有界 pending 积压保护；达到本轮队列上限时延后新增并记录下一决策时间，但保留业务欠额，不得形成全天停止、降低目标或完成结论。
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
- Telegram 远端 slowmode 与 FloodWait 约束；AI 不再设置业务侧群本地冷却 gate，远端等待期间保留未完成债务并继续重排。
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

# 生产 Planner、拟人排期与内存压力修复 PRD

## 0. 文档状态

| 项 | 结论 |
| --- | --- |
| Intake ID | intake-2026-08-17-planner-pacing-memory-001 |
| 分级 | L3 / P0 资源风险 + P0 来源突发 + P1 拟人节奏，必须走标准生产事故流与 Release Gate |
| 设计状态 | product_design_complete_v2 / implementation_resynced_2026-08-19 |
| 实现状态 | local_first_phase_complete / targeted_qa_pass；source cursor/admission、跨 Task 总容量、curve-aware pairwise gap、headroom 和 typed shortfall 已实现，生产配置、压力与 E4 待证明 |
| 生产状态 | pacing_partial / quantity_and_humanization_E4_unproven / production_fixed=unproven |
| 证据基线 | 2026-08-17 诊断快照只作历史证据；当前版本和线上状态必须按部署 SHA、运行配置与 typed remote fact 重新读回 |
| 权威关系 | 本文规范性取代生产稳定性 PRD 中 Planner 资源和旧 AI fail-open gate 口径，并补正拟人节奏 PRD 的跨批恢复；不改变各任务 stable owner、typed remote fact、unknown 与数量结算合同 |
| 操作边界 | 本轮只读 SSH、数据库、日志、代码与文档设计；未重启、改配置、改数据、重试、reconcile、部署或调用 Telegram 业务操作 |

“拟人化排期仍有明显拥挤”精确定义为：业务 due 已分散，但 overdue 技术批次会在 now 附近重复起算 release；同时现有最终闸门只覆盖 AI，且数据库异常可 fail-open，导致多个账号的真实 Gateway 调用仍在少数分钟或同秒重叠。它不等于“任务全部瞬间完成”。当前只能证明密度仍高，不能把 Action.executed_at 当成 Telegram 完成事实。

## 1. 原始需求、范围与成功条件

用户要求先到正式环境查明“任务是否仍短时间直接完成”和“内存为什么仍高”，再设计修复并深度检查遗漏。

本 PRD 必须同时做到：

1. Planner 单轮工作量只与 dirty/due Task 和本轮有界候选相关，不随历史 Action、全账号 scope 或旧 blocker 总量线性增长。
2. Planner 不持有 Telegram client、session 或 update loop；频道消息只能由 Listener 观察并持久化，Planner 只读新鲜快照。
3. AI、浏览、评论、点赞都使用一致的 source/period/plan/lifecycle 身份；跨批 release cursor 单调，真实 Gateway call-start 也受来源级最终闸门约束。
4. 不用重启、强制 GC、缩业务目标、丢任务、降低 typed fact 标准、静默 fallback 或单纯加内存上限伪装修复。
5. 迁移、旧 backlog、滚动发布、API 版本、数据清理和资源限额均有显式停止线。
6. 资源 E3、排期 E3 和任务 E4 分开验收；三者全部通过后才能写 production_fixed。

不在范围：

- 本文不重新定义 AI/浏览/评论/点赞的数量义务、自然键或 Telegram 成功事实。
- 本文不授权生产清理、补跑、重排、重启、配置修改或发布。
- 独立 Python/native leak 在修复确定性放大项并完成 6/24 小时曲线前保持 unproven。

## 2. 当前生产事实与首个破损边界

### 2.1 资源事实

| 边界 | 22:09 当前读数 | 状态 |
| --- | ---: | --- |
| 宿主 | 4 vCPU / 7.3 GiB；MemAvailable 约 640 MiB；swap 已用约 1.12 GiB | failed：聚合资源压力 |
| Planner | CPU 约 84%；PSS 约 660 MiB；Anonymous 约 658 MiB；AnonHugePages 约 320 MiB；无 memory limit | failed：当前最大单进程 CPU/私有内存 owner |
| Dispatcher ×2 | 各约 397～400 MiB / 512 MiB | degraded：显著常驻项 |
| AI Generation ×3 | 各约 374～394 MiB | degraded：宿主聚合基线很高 |
| OOM/restart | Planner RestartCount=0、OOMKilled=false | pass 仅代表当前无 OOM；不代表资源健康 |

Planner 短样本没有证明单调增长，所以“泄漏”仍为 unproven。已证明的是无界 ORM/历史扫描、超大 Task.stats 重写、远程 client 生命周期和长 drain 共同维持高水位。

### 2.2 工作放大与热行

- 6 个 running AI Task 中，5 个各约 1,210 条准入 scope；单 Task 历史 Action 约 9,745～20,693。
- running 浏览 Task 历史 Action 最高约 46,609；Action 表约 182k live rows。
- Planner 热路径加载完整 TgAccount/Action ORM，而不是 count、exists 或有界投影。
- Task.stats 为 47～118 KiB；membership_summary 写入数百个账号 ID。
- conversation_quality_active_blockers 单 Task 可达 588～1,534 个 key，只有 0～3 个对应当前 open Action；大量终态和失配 key 仍被整图重写。
- 一轮 processed=3353 耗时 244 秒；processed 混合扫描、复用、重写和新建，不能解释真实工作。

首个资源破损边界是 Planner 的无界读取与 Task 热行写入；宿主总预算是并行平台风险。不能先把结论写成单一内存泄漏。

### 2.3 Planner 远程角色越界

精确调用链已静态定位：

channel_view / channel_like build_plan
→ channel_scope
→ collect_channel_messages
→ gateway.fetch_channel_messages

Planner 容器也实际建立 Telegram TCP 并持续记录 Telethon update。结论从“调用者待证明”更新为 failed：频道消息读取由 Planner 直接触发。该边界必须由 Listener snapshot 取代。

### 2.4 排期与最终执行拥挤

- 现有 recovery cursor 只看当前函数批次的 frozen release；后续 overdue 批次会重新从 now 附近起步。
- AI 当前 Dispatcher gate 使用固定 8 秒、在 worker 内 sleep；SQLAlchemyError/ValueError 仅 warning，随后仍可调用 Gateway。
- view/comment/like 没有等价的来源级 Gateway call-start 闸门。
- 最近 45 分钟 Action.executed_at 的 AI 来源峰值约 12～15/min；它只能证明执行记录密度，typed remote fact 密度仍 unproven。
- 当前历史/open backlog 很大且混有暂停、旧 plan 和旧 lifecycle；单个 like Task 约 2,109 open，comment Task 约 370 open。T2 不能在未分类时整体接管。

## 3. 根因与设计归属

| 编号 | 根因 | 设计归属 |
| --- | --- | --- |
| RC-R1 | 全 scope、历史 Action 和完整 ORM 行进入 Planner 热路径 | 增量 admission 投影 + 有界候选 |
| RC-R2 | membership/quality blocker 大数组或大 map 锁 Task 行重写 | 独立 active blocker/read model + compact summary |
| RC-R3 | 多角色直接写 next_run_at，固定轮询覆盖事件，Task 行继续变热 | 独立 TaskPlannerWakeState + capability 激活 |
| RC-R4 | Planner 直接调用频道 Gateway 并持有 Telethon | Listener subscription/snapshot 唯一远程 owner |
| RC-R5 | processed 无阶段、SQL、ORM、PSS/cgroup 解释力 | Planner 自采样和阶段指标 |
| RC-P1 | recovery cursor 是 batch-local | 四类 stable owner 的 source-wide cursor |
| RC-P2 | AI 最终 gate sleep 且 fail-open，其他三类缺 gate | SourcePacingState + SourcePacingAdmission |
| RC-M1 | 旧 backlog 缺 source/period/lifecycle/plan 分类 | preview manifest + 分 Task 激活 |
| RC-H1 | 多容器共同挤压宿主 | cgroup 预算公式 + 分 train 隔离 |
| RC-L1 | 独立 leak | unproven；确定性修复后重测 |

## 4. 规范数据模型

以下新增对象都是可重建投影或调用准入，不是第二套数量 owner。

### 4.1 TaskPlannerWakeState

每 Task 唯一一行，字段至少包含：

- tenant_id、task_id、lifecycle_epoch
- wake_revision、planned_revision、not_before_at、reason_code
- last_started_at、last_completed_at、version、updated_at

wake_task_planner 必须是唯一写入口：

1. 同 lifecycle 原子 wake_revision + 1。
2. not_before_at 取现值与新值中更早者；reason 使用枚举，不写自由文本或 payload。
3. 旧 lifecycle 事件返回 wake_stale_epoch，不得唤醒新生命周期。
4. Planner 开始时冻结 revision；提交时 CAS planned_revision。执行中发生的新 revision 不能被本轮 next decision 覆盖。
5. Task.next_run_at 在迁移期只做兼容镜像；v2 激活后禁止任何业务模块直接写。

发布前 writer inventory 必须覆盖：创建/启动/暂停恢复/配置、membership/admission、Listener/source、Generation/quality、Dispatcher/Attempt/fact、reconcile/recovery 和 Planner 自身 timer。静态检查禁止新增 Task.next_run_at 直接写。

### 4.2 TaskAdmissionProjection 与有界候选

TaskAdmissionProjection 每 Task/lifecycle 一行：

- scope_revision、item_revision、candidate/joined/pending/failed/unknown/ready counts
- captured_at、reconciled_at、version

TaskMembershipAdmissionItem 增加 eligibility_rank、eligibility_revision、planner_last_selected_at；只在真实 item 状态变化时递增 item/projection revision。Planner 使用覆盖索引按：

eligibility_rank
→ planner_last_selected_at NULLS FIRST
→ item id

做 keyset 查询，一次最多 plan_limit + recovery_sample。选中行在同一短事务更新 planner_last_selected_at，保证 1,210 个候选不会永远只选前 20 个。不得先把全部 ID 拉入 Python；低频 reconciliation 使用 updated_at/id checkpoint 修复漏事件，不替代正常增量 writer。

Task.stats.membership_summary_v2 只含 counts、stage、revision、captured_at 和最多 10 个脱敏样本引用，大小不超过 2 KiB。详情明细继续走分页 items API。

### 4.3 TaskRuntimeActiveBlockerProjection

每个当前 active blocker 一行，唯一键：

tenant + task + lifecycle + blocker_domain + scope_key_hash

字段包含 blocker_code、source_type、source_id_hash、source_revision、opened_at、updated_at。Action、GenerationJob、variation、Attempt 和 remote fact 继续是真相；投影可删除已清除行并从源事实重建。

要求：

- blocker 开启/清除与源状态同事务，或通过 transactional outbox 投影；不得只改 Task.stats。
- projector reconciliation 按 source updated_at/id checkpoint 有界补偿。
- TaskRuntimeSummary 增加 lifecycle_epoch 和 blocker_revision，只聚合当前 lifecycle 的 active count、code counts、oldest_at、revision 和最多 10 个样本；lifecycle 变化时以 CAS 重置，quality summary 不超过 2 KiB。
- conversation_quality_active_blockers 旧 map 不再写；终态或无源 key 不得保留为 active。

### 4.4 Listener source subscription 与 snapshot

新增 TaskSourceSubscription：

- tenant、task、lifecycle、source_type、source_peer_hash、listener_source_state_id
- required_snapshot_revision、state、created_at、updated_at

扩展 ListenerSourceState：

- snapshot_revision、snapshot_status、observed_at、fresh_until_at、next_probe_at、last_error_code

合同：

1. Task 启动/目标变化只登记 subscription 并 wake Listener，不做 Telegram 调用。
2. Listener 在数据库事务外 fetch；短事务内 upsert ChannelMessage、递增 snapshot revision、更新 freshness，再通过 outbox 唤醒所有订阅 Task。
3. fresh_until_at = observed_at + 2 × 冻结的 collect_window_seconds；倍率为合同常量，配置变更只影响下一次 snapshot。
4. fresh + ready + 0 条消息是权威空快照；pending、stale、unavailable、error 必须区分。
5. Planner 只读 snapshot。无新鲜快照时写 channel_source_snapshot_pending/stale/unavailable，并排到 next_probe_at；不得调用 Gateway 或复用过期结果伪装成功。
6. Planner 角色触达 Gateway/Telethon 入口必须 fail-fast 为 planner_remote_io_forbidden，并记录调用模块和阶段，不记录敏感参数。

### 4.5 四类 pacing owner 身份

四类 owner 增加统一只读身份列：

- task_lifecycle_epoch
- pacing_period_key
- pacing_source_key_hash

并建立 tenant/task/lifecycle/period/source/plan/release 组合索引。

| 任务类型 | 数量 owner 不变 | source identity | period/lifecycle | cursor 纳入行 |
| --- | --- | --- | --- | --- |
| AI 活群 | TaskGroupDailyMessageSlot | 目标群 target_operation_target_id | TaskDayLedger 自然日 + Task lifecycle | 同 plan、身份完整、release 非空的所有 stable slot |
| 浏览 | ViewFulfillmentObligation | 频道 target peer | TaskDayLedger 自然日 + Task lifecycle | 经 ledger/target 绑定的同 plan stable obligation |
| 评论 | CommentFulfillmentObligation | 来源频道 target peer | 专项滚动 24h period + Task lifecycle | 同 revision/plan 的 stable obligation |
| 点赞 | ReactionFulfillmentObligation | 来源频道 target peer | 专项滚动 24h period + Task lifecycle | 同 reaction contract/plan 的 stable obligation |

open、claimed、Gateway-started、unknown、confirmed、terminal owner 只要属于同一冻结身份，都占用原 release slot；状态终结不释放历史时间身份。不同 lifecycle、period、plan，或 retired/superseded 身份不进入当前 cursor。

在稳定排序取得 source advisory lock 后，从全部匹配 owner 读取 max release 和 max ordinal；新 freeze 与 cursor 前进同事务。只有当前 source/plan 从无 frozen release 时才可用首次 recovery anchor。冲突写 pacing_source_cursor_conflict 并停止该 source 新物化，不回退 batch-local 算法。

### 4.6 SourcePacingState 与 SourcePacingAdmission

SourcePacingState 每 tenant/pacing_domain/source 一行；pacing_domain 区分 AI send、view、comment、reaction，但同一 domain、同一真实群/频道跨 Task 共用时间线，防止两个 Task 各自合规却在远端形成突发：

- next_call_not_before_at、last_call_started_at、last_capacity_plan_hash、last_capacity_slot_ordinal、revision、version

SourcePacingAdmission 每个真实远程尝试一行：

- owner_type/id、action_id、attempt_id、source identity、quantity plan hash、source capacity plan hash/slot ordinal
- planned_release_at、call_not_before_at、pairwise_source_gap_seconds
- state：reserved、call_started、remote_unknown、finished、cancelled_pre_gateway
- version、created_at、updated_at

最终调用流程：

1. claim 只能建立 reserved admission，不在 Dispatcher sleep；未来时间把 Action defer 到 call_not_before_at 并释放 worker slot。
2. Gateway 入口前必须锁共享 SourcePacingState；更低 ordinal 若仍是可执行 pending 则禁止抢跑，pre-Gateway terminal 可跳过但保留历史占用。以最后 call-started/unknown slot 和 current slot 的 frozen timestamps 取得当前 pairwise gap，再计算 `max(owner release, account pacing, last_call_started + pairwise gap)`；不得沿用上一大 gap 抹平下一高峰。
3. 尚未到时只 defer，不调用远端；已到时 CAS admission 为 call_started，并在同一短事务更新 last_call_started_at，提交后才允许 Gateway call。
4. DB、锁、版本或身份失败写 pacing_source_admission_unavailable/conflict，保持 pre-Gateway，禁止远程调用。旧 AI warning 后继续发送的 fail-open 行为被本文废止。
5. remote_unknown 保留 owner/admission/Attempt，不自动重发；reconcile 仍按任务 typed fact 合同处理。
6. pairwise gap 只来自冻结 SourcePacingCapacityPlan 的相邻 slot 与策略最小间隔，不使用固定 8 秒或 `period duration / 单 Task plan_total`；period 内配置编辑不改变已冻结 plan。

release density 和 Gateway call-start density都必须满足冻结 capacity slots；typed remote fact density是 E4 观察结果，受 Telegram 完成时间影响，不写成可由本地强制的绝对定律。

### 4.7 跨 Task 来源容量与恢复 headroom

`SourcePacingCapacityPlan` 是调用容量投影，不是 quantity owner、业务 due owner 或完成事实。唯一 scope 为 `(tenant_id, pacing_domain, real_source_key, capacity_window_start/end, capacity_policy_revision)`；不同 Task、ledger、时区或滚动 period 只要 UTC 时间窗重叠且命中同一真实 source，都进入同一计划。

`SourcePacingCapacityPolicyVersion` 冻结 domain 级 hourly curve、minimum gap、hourly ceiling、telemetry window、headroom floor、provider retry slots、批准人和 hash；approved 后不可原地修改。Task 只能引用 approved revision，缺失或过期时 preflight blocked。

计划至少冻结：hourly curve/hash、policy minimum gap、capacity slot timestamps、已有 owner 占用、新 owner assignment、replacement headroom、occupied/available/deficit、last safe release、revision/version。业务 owner 追加 `source_capacity_plan_hash/source_capacity_slot_ordinal`，其 `due_at` 不变，`release_not_before_at=max(due_at, assigned_capacity_slot_at)`。

容量准入流程：

1. Task start、目标增加或新 period materialization 前按稳定 source 顺序取 advisory lock，读取所有 current lifecycle 的 frozen release、Gateway unknown 和 typed fact 占用；不能只聚合同 Task、同 ledger 或当前技术批次。
2. 使用与业务 due 相同的版本化 hourly activity curve 生成 source capacity slots；curve 权重 0 表示该小时不分配新 release，低活跃必须显式配置正权重，禁止 0→1 静默改写。策略 minimum gap/小时上限必须来自 approved policy，不得反推单 Task 平均 gap。
3. `required = incoming due owners + replacement_headroom`；headroom=`ceil(incoming × max(pre_gateway_replan_rate_p95, approved_floor)) + approved_provider_retry_slots`。缺少可用 telemetry 时必须使用显式批准的 canary policy，否则 preflight blocked；headroom 不创建消息，只保留替代位置。
4. 已冻结的其他 Task capacity slot 永不重排；只把 incoming owners 按 due/ordinal 分配到 deadline 前剩余 slot。若不足，preview/start 返回 `source_capacity_unavailable` 及 required/occupied/headroom/available/deficit，任务保持 draft/blocked，不缩目标、不先启动后等待必然 shortfall。
5. 运行中不可预见的容量损失只使用已冻结 headroom 逐 owner 替代；无 slot 时该 owner late，deadline 后 CAS 写唯一 `pacing_capacity_shortfall`。`Task.stats` 只投影 shortfall fact。

同批 AI slot 必须独立推进：某一 slot 没有合法 capacity point、内容或账号时，不得取消同批其他已分配 slot；也不得由其他 slot 的成功冲抵该 quantity owner。

## 5. 业务数据流与失败语义

### 5.1 Planner

Task writer
→ wake_task_planner
→ TaskPlannerWakeState revision
→ Planner 读取 compact projection / stable owner / fresh Listener snapshot
→ 有界 plan
→ 短事务冻结 owner、Action 与 planned revision

无 dirty revision 且无时间 due 时不进入业务规划。2 秒 worker heartbeat 可保留为进程活性，不触发全 Task 重算。

### 5.2 来源远程观察

TaskSourceSubscription
→ Listener lease
→ Telegram fetch（事务外）
→ ChannelMessage + ListenerSourceState snapshot revision（短事务）
→ outbox wake
→ Planner 只读

### 5.3 真实调用

stable owner release
→ Action claim
→ AccountPacingReservation
→ SourcePacingAdmission reserved/defer
→ Gateway 前 source CAS
→ ExecutionAttempt
→ typed remote fact / unknown

Action success、executed_at、页面 completed、容器 healthy 都不能代替 typed remote fact。

## 6. API、前端与权限

### 6.1 API 版本

- GET /api/tasks/{id} 默认返回 membership_summary_version=2 和 runtime_blocker_summary_version=2，不返回账号 ID 数组或 blocker 全图。
- GET /api/tasks/{id}/membership-admission/items 继续提供权限化分页明细。
- T0 过渡期显式 summary_version=1 可从数据库按需生成旧完整响应，但不再持久化到 Task.stats；完成消费者登记后返回 410 summary_version_retired，不允许截断数组或双写大 JSON。
- 新增 GET /api/system/runtime/planner-pressure，权限 system.view，响应版本 planner_pressure_v1。
- Task plan preview/start 响应增加 source capacity scope/hash、required/occupied/headroom/available/deficit、configured/effective curve 和 last safe release；preview hash 与 start CAS 不一致时必须失败。

planner_pressure_v1 至少返回：

- state：fresh、stale、unavailable、degraded
- captured_at、sample_interval_seconds、release_sha、worker_id_hash
- rss/pss/private_dirty/anonymous/anon_huge_pages
- cgroup current/peak/limit/events
- cpu、thread_count、telethon_client_count
- drain p50/p95、rows/SQL/dirty/due、latest blocker code

不返回账号、手机号、session、代理、正文、Prompt 或原始 source ID。

### 6.2 前端信息架构

1. 任务详情 pacing panel 分开显示 due、source capacity release、Gateway call-start、typed fact 四条小时/1m/5m 密度；同时显示 quantity plan/source capacity hash、pairwise gap、headroom 使用和 blocked/unproven 状态。
2. 任务详情 membership 和质量 blocker 只显示 count/类型/有界样本；明细点击进入分页接口。
3. 系统设置新增二级页“运行状态”，不复用现有素材/Prompt 资源配置页。Planner 卡片展示 PSS/cgroup/CPU/drain/Telethon 和 fresh/stale/unavailable。
4. stale 数据必须保留最后采样时间并置灰；不得继续显示绿色健康。
5. 页面不提供 restart、GC、清缓存、改限额、重排或 resend 按钮。

## 7. Planner 自采样与资源预算

Planner 每 10 秒从自身进程采集：

- /proc/self/smaps_rollup：PSS、PrivateDirty、Anonymous、AnonHugePages
- cgroup 自动识别：v2 读取 memory.current、memory.peak、memory.max、memory.events；v1 读取 memory.usage_in_bytes、memory.max_usage_in_bytes、memory.limit_in_bytes、memory.failcnt
- process CPU delta、线程数、Telethon client 数
- 当前 drain 阶段计数、ORM rows、SQL count/time、created/reused/retired/deferred

样本必须记录 cgroup_version 和统一字段 current/peak/limit/event_count；生产当前为 cgroup v1，采样器不得因没有 v2 文件而返回空值。样本由 Planner 自己写有界 WorkerRuntimeResourceSample；其他容器不得跨容器读取 /proc。原始样本保留 24 小时，5 分钟 rollup 保留 7 天，按时间分批删除，不与业务 owner/fact 共表。

freshness：

- 距 captured_at 不超过 2 个 sample interval：fresh
- 超过 2 个 interval 但 worker heartbeat 仍有效：stale
- 无样本或 heartbeat 失效：unavailable
- 资源或 SLO 越线：degraded

资源 hard limit 不能根据 PSS 直接填写。T4 候选值使用 cgroup 24 小时数据：

ceil_to_64MiB(max(startup_peak_p99, warm_working_set_p99) × 1.25)

reservation 取 warm working set p95；pids limit 取线程/子进程 p99 加 20%。若公式结果超过平台分配预算，T4 blocked，先继续降工作集或扩容，不用更小 limit 制造 restart loop。Planner PSS ≤450 MiB 是优化验收目标，不是 hard limit 计算口径。

## 8. 存量分类、迁移与清理保护

### 8.1 Schema 与在线索引

1. T0 只做 additive nullable column/new table；禁止带 volatile default 的大表重写。
2. 大表索引用 CREATE INDEX CONCURRENTLY，并在 Alembic autocommit block 中单独执行；不得与 schema transaction 混用。
3. 每个索引有精确名称、目标列、EXPLAIN 预期、pg_stat_progress_create_index 监控和 pg_index.indisvalid readback。
4. 失败产生 invalid index 时只对精确名称 DROP INDEX CONCURRENTLY 后重试；不得模糊删除。
5. backfill 按主键 keyset 每批 500 行，持久化 checkpoint；单事务目标不超过 2 秒，重复执行幂等。
6. MemAvailable <1.5 GiB、数据库 5 分钟 CPU >70%、复制延迟 >30 秒或 lock timeout 时暂停 train；不在当前 640 MiB 压力下强推大表迁移。

### 8.2 pacing backlog preview

按 Task/type/source/lifecycle/period/plan 输出 exact manifest：

- typed fact / terminal：仅作为已占用历史，不改写。
- Gateway-started / unknown：保留并 reconcile，不重发。
- current pre-Gateway 且身份完整：保留 owner release，首次 claim 时进入 source admission。
- current owner 按真实 source/UTC overlap 生成 capacity preview；可完整保留既有 release 且剩余容量足够时标 `capacity_feasible`，否则标 `capacity_deficit` 并停止该 source 新 materialization，不改既有 owner/目标。
- old lifecycle、paused、retired/superseded：不进入 current call，不自动复活。
- 缺 source/period/plan 身份：pacing_legacy_identity_unresolved，阻断该 Task/source 激活。

T2 按 Task/source 灰度，不要求暂停的 comment/like 为 AI/view 让路，也不能用 AI/view E4 代替它们。

### 8.3 Task.stats cleanup

单独 preview/apply，目标仅包括：

- membership_summary 旧账号 ID 数组
- conversation_quality_active_blockers 旧全图

每个 Task 固定 tenant/task/lifecycle、旧 stats hash、expected SHA、actor/approval。apply 前重算 active projection/count；漂移则逐 Task 拒绝。Action、Attempt、GenerationJob、variation、owner、unknown 和 typed fact 不删除、不改写。T3 与 schema/index train、资源 limit train 分窗。

## 9. 滚动发布与回滚

| Train | 内容 | 激活条件 |
| --- | --- | --- |
| T0 Schema | 新投影、身份列、并发索引、v2 API；writer dual-publish | migration/index readback，旧行为不变 |
| T1 Boundary/Wake | Listener snapshot、compact projection、所有 writer 发布 wake v2、Planner shadow compare | API、Listener、Generation、Dispatcher、Recovery、Planner 心跳均报告同 SHA + capability |
| T2 Pacing | 四类 source cursor + 跨 Task capacity plan + Gateway 前 source admission；按 Task/source canary | aggregate demand feasibility、backlog manifest、shadow curve/release/call diff 通过 |
| T3 Cleanup | 停写旧大 stats，v1 consumer cutoff，精确 cleanup | v2 第一方 readback，hash preview 通过 |
| T4 Resource | 按 cgroup 公式设置 reservation/hard/pids | T1～T3 后 24h 基线、宿主预算和启动峰值通过 |

能力闸门：

- T0/T1 前半段，新 writer 同时发布 wake v2 和兼容 next_run_at；旧 Planner 仍读旧值，新 Planner 只 shadow。
- 所有活跃角色 capability 齐全并排空旧进程后，原子切换 planner_wake_contract=v2。
- 激活后 next_run_at 只做只读兼容镜像；禁止静默回退旧 writer、全量轮询或 Planner 远程 fetch。
- 若 T1/T2 失败，停止新 materialization/call，保留 additive schema、owner、admission、Attempt、unknown 和 fact，前向修复后恢复。
- T4 可回滚资源配置，但不得批量重启或把连续触限当回收策略。

## 10. QA、性能与生产 E4

### 10.1 正确性与并发

1. 1,210 scope、20k Action/Task、6 AI Task fixture：gate 只返回 count/revision，候选 ORM ≤20，历史 Action ORM=0。
2. 候选公平性：连续有界轮转后 1,210 个同优先级 item 均被访问；新高优先级可抢占，但低优先级有 starvation 观测和上界。
3. 1,534 个旧 quality blockers，其中仅 3 个 active：投影为 3，旧 map cleanup 后 summary ≤2 KiB，源事实 hash 不变。
4. 13 类 wake writer 覆盖规划中事件、旧 epoch、重复事件、回滚、暂停恢复和 mixed-version；无丢 wake、无热循环、v2 后无直接 next_run_at 写。
5. Planner 调用任何 Gateway/Telethon API 必须失败为 planner_remote_io_forbidden；soak 中 Telethon client/thread 增量为 0。
6. Listener 覆盖 fresh empty、stale、error、revision 竞态和 outbox 重放；Planner 不远程 fallback。
7. 四类各做 20 batches × 20 owners、双 Planner、回滚/重启：release cursor 单调，索引命中，不全表扫。
8. Gateway gate 注入 DB/lock/version 失败：四类均无远程调用；future admission defer 后 worker slot 释放，无 sleep。
9. 同来源两个 Task 各 100 due：要么冻结 200 个互不冲突的 capacity slots 并保持各自 quantity owner，要么在 start 前以精确 deficit 阻断；不得共享 cursor 却只提供单 Task 容量。
10. 高低峰 curve 下，capacity release/call_started 小时直方图在预注册容差内匹配 frozen curve；0 权重小时发送为 0，低活跃正权重仍可发送。
11. 注入 Provider/quality/context/account 延迟：只使用 headroom 逐 slot 替代，不一分钟追量；合法同批 slot 继续，deadline shortfall 与 owner 一一对应且不重复。
12. migration 覆盖 concurrent index 失败/invalid 恢复、checkpoint resume、stop line 和 mixed SHA。

### 10.2 性能与资源

- 生产等价全轮 p95 ≤30 秒；单 Task p95 ≤5 秒；无 dirty/due 的业务轮 p95 ≤2 秒。
- 单 Task ORM rows ≤200，其中账号候选 ≤20；SQL 数固定，不随历史 Action 总量线性增长。
- membership summary ≤2 KiB，quality blocker summary ≤2 KiB；Task.stats 不含账号全集或 blocker 全图。
- Planner warm PSS p95 ≤450 MiB；6 小时首尾小时差≤64 MiB且≤10%；CPU正常负载 p95≤50%单核，无 due 时 5 分钟平均≤10%单核。
- 24 小时 cgroup 无 oom/oom_kill、无 limit restart；宿主 MemAvailable≥1.5 GiB且无持续 swap-in/out。未达保持 resource_capacity_degraded。
- runtime UI/API freshness 状态与心跳/样本一致，不能把 stale 当 fresh。

### 10.3 排期与业务证据

每类按：

due
→ owner release
→ account reservation
→ source admission/call_started
→ Attempt/Gateway
→ typed remote fact

逐层验收。

- shadow 阶段不创建新 Action、不调用 Telegram；同 period 重放逐 slot一致。
- canary 至少 AI、view 各 1 个 Task 完整自然日；comment/like 只有真实 active canary 才能通过，否则各自 E4 blocked。
- release 和 call_started 的 1m/5m 密度满足 source plan；跨技术批边界无重新从 now 起步。
- 聚合 due owner 必须全部对应 frozen capacity slot，或在 start 前形成 capacity blocker；运行期最终每个 due owner 恰好对应 confirmed typed fact、Gateway unknown 或唯一 typed shortfall。
- AI 只认 canonical send fact/quantity binding；view、comment、reaction 只认各自 typed remote fact。
- current typed fact 密度和最终数量按 task/source 独立报告 pass/failed/blocked/unproven。

只有资源 E3、四类适用的 pacing E3 和请求范围内的任务 E4 全部通过，才允许 production_fixed。

## 11. Product Design Complete 自检

| 检查项 | 结论 |
| --- | --- |
| 用户原话 | 已覆盖线上短时完成、内存高、具体原因、PRD 修复和遗漏复核 |
| 前端/API | v2 summary、分页、运行状态 IA、freshness、权限和 v1 cutoff 已冻结 |
| 后端/worker | Listener、wake、跨 Task capacity/admission、逐 slot 守恒、有界候选与四类 Gateway gate 完整 |
| 数据边界 | stable owner/fact/unknown 不变；capacity plan 只分配调用位置，typed shortfall 按 owner 唯一结算 |
| 并发/幂等 | revision/CAS、source lock、mixed-version capability、outbox、checkpoint 完整 |
| 迁移/清理 | concurrent index、资源停止线、旧 backlog 分类、hash preview/apply 完整 |
| 资源 | PSS 与 cgroup 口径分离；v1/v2 自动识别、limit 公式、freshness、retention 和宿主预算完整 |
| 失败/回滚 | stale snapshot、legacy identity、DB/lock、cursor conflict、触限均显式失败 |
| QA/E4 | 本地、PostgreSQL、性能、6/24h、四类排期和 typed fact 分层完整 |

结论：product_design_complete_v2 / local_first_phase_complete / targeted_qa_pass / production_fixed=unproven。发布与验收不得把固定 limit、重启、强制 GC、扩大 swap、缩目标、单 Task 平均 gap、Action success 或健康检查单独作为完成。

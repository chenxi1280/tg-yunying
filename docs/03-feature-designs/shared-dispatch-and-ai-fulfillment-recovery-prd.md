# 共享调度、锁序与 AI 履约恢复专项 PRD

## 0. 文档状态

- `intake_id`: `intake-2026-08-01-shared-dispatch-recovery-001`
- `message_id`: `pdc-2026-08-01-shared-dispatch-recovery-001`
- `related_incident`: `incident-2026-08-01-production-fulfillment-stall-001`
- `owner_agent`: `product`
- `level`: `L3`
- `priority`: `P0 shared_dispatch / P0 deadlock / P0 legacy_takeover / P1 task_recovery`
- `design_status`: `complete`
- `design_review_status`: `closed_2026-08-01_phase15_3P0_2P1_resync`
- `implementation_status`: `implemented_local_phase15_audit_repairs`
- `qa_status`: `local_no_postgres_pass_postgresql_blocked`
- `product_acceptance_status`: `pending`
- `production_status`: `blocked_unreleased`
- `evidence_level`: `E4 incident / E2 design / local targeted QA`
- `created_at`: `2026-08-01`
- `last_updated_at`: `2026-08-01`
- `truth_sources`:
  - `docs/01-product/tg-ops-platform-prd.md` §2.18
  - `docs/03-feature-designs/all-task-fulfillment-recovery-prd.md` §5、§9、§10.1
  - `docs/03-feature-designs/ai-conversation-humanization-and-group-bot-admission-prd.md` §15.8、§15.9
  - `docs/03-feature-designs/search-click-daily-fulfillment-remediation-prd.md`
  - `docs/00-index/project-dataflow-index.md`
  - `docs/04-ops/deployment/PRODUCTION_RUNTIME.md`

> 本文是 2026-08-01 生产履约停滞事故的专项修复设计。它不改变五类任务的业务目标、远端事实口径、验证码安全边界或群管准入规则；它将既有总 PRD 中已经定义的共享调度、锁序、搜索预绑定和存量接管合同，收敛为可直接交接开发、QA 和发布验证的实施边界。

> 2026-08-01 实现记录：代码、前向迁移、Stage A/B/C 发布编排和定向 no-PostgreSQL QA 已形成本地候选；生产环境模板中的遗留容量 `52` 已同步修正为 `26`。当前测试 PostgreSQL 无法连接，因此 PostgreSQL 并发、迁移实库重放和独立 Gateway journal 事务证据仍为 blocked；GitHub Actions、生产激活、Telegram canary 与完整自然日 E4 均未执行，不能写 `qa_pass`、`product_accepted` 或 `production_fixed`。

> 2026-08-01 完成性审计 resync：本地候选又确认 3 个 P0 和 2 个 P1——群管频道 follow/callback 未进入 B0/journal/B1、旧 worker heartbeat 未显式退役、新 worker 的业务 drain 会覆盖合同版本 metadata、membership unknown 存在两套竞争恢复协议、生产 embedded worker 未 fail closed。以下合同是 Phase 15 修补的先行产品真相源；修补与本地 QA 完成前保持 `production_status=blocked_unreleased`。

> 2026-08-01 Phase 15 本地实现收口：上述 3 个 P0、2 个 P1已修补；反向回归另补齐精确recovery claim CAS、inconclusive/stale双入口释放、存量无Attempt升级、membership `require_send`证据语义和发布cutoff纳秒精度。相关no-PostgreSQL回归与静态闸门通过；测试PostgreSQL仍不可连接，Actions、发布、生产runtime与Telegram E4均未执行，因此`product_acceptance_status=pending`、`production_status=blocked_unreleased`保持不变。

## 1. Intake Card

- `source`: `user + prod-diagnosis + independent review`
- `raw_input`: 线上任务执行异常，要求查明原因并编写完整修复 PRD。
- `suspected_type`: `production_incident + shared_dispatch_contract_drift + rolling_upgrade_compatibility`
- `affected_surface`: AI 活群、搜索点击、评论、点赞、浏览、准入 Action、Dispatcher、Planner、AI generation、Recovery、PostgreSQL、任务详情运行态。
- `production_related`: `true`
- `initial_evidence_level`: `E4`
- `route`: `prod-diagnosis -> product -> dev -> qa -> product -> prod-diagnosis`
- `release_gate_required`: `true`

### 1.1 用户目标

1. 恢复到期任务获取共享 Dispatcher 份额的能力，不允许搜索或任一任务类型固定独占。
2. 消除共享 claim、Action 终结和 Task 统计之间的 PostgreSQL 反向锁序。
3. 安全接管缺失 `group_content_scope_v1` 的历史 AI Action，不放宽跨群内容防护。
4. 恢复三个 AI 活群的真实 Telegram 消息增长，同时保留 listener、reply target、准入和 unknown 防重门禁。
5. 让搜索点击继续按真实 `target_click_observed` 履约，并把软件吞吐故障与账号安全容量不足分开报告。
6. 不把尚未到期或没有动态输入的评论、点赞任务误报为生产故障。

### 1.2 已确认生产事实

| 事实 | 状态 | 说明 |
| --- | --- | --- |
| 生产版本 | `confirmed` | release `87fe0bf0`；merge tree 与第一父提交相同，不能归因于 merge conflict |
| AI 新版本后停增 | `confirmed` | 新容器约 09:35 启动后，三个 AI 活群均无新增业务确认 |
| 搜索仍可增长 | `confirmed` | 复采样从 311 增至 329，Gateway/点击链不是整体停机 |
| 两套分片拓扑 | `confirmed` | 搜索 fulfillment writer 使用 `ACCOUNT_SHARD_TOTAL=1`；两个 Dispatcher 使用 `ACCOUNT_SHARD_TOTAL=2` |
| 共享容量过配 | `confirmed` | scope capacity=52；每 worker 有效 DB 并发 13，两个 worker 合计约 26 |
| 跨 epoch 旧 unclaimed 被累计 | `confirmed` | Window totals 会包含 prior allocations 的 unclaimed counter |
| PostgreSQL deadlock | `confirmed` | 10:04、10:13 出现 Scope→Task FK 与 Task→Scope 的锁环 |
| AI 第二层 blocker | `confirmed` | scope contract、context freshness/expiry、reply target、group-bot admission 均有真实计数 |
| 评论停滞 | `disproven` | 复核时 140 个 Action 均未到期，due=0 |
| Telegram 孤儿消息 | `unproven` | deadlock 可能发生在 Gateway 后、DB 提交前，必须逐 Attempt/远端核验，不能推定存在或不存在 |

### 1.3 事故根因分层

1. **公共调度层**：同一 Window 被不同 writer 以不同 `shard_total` 构造；精确分片匹配使普通 ready Action 无法消费错误拓扑 Reservation。
2. **容量守恒层**：两 Dispatcher 的真实执行预算约 26，却预分配 52；旧 epoch 未领取份额继续计入新分配，放大 assignment expiry 与普通任务饥饿。
3. **事务锁序层**：allocation 先持 Scope 再因 TaskAllocation 外键请求 Task；AI quality/finalize 先持 Task 再请求 Scope。
4. **滚动升级层**：消费者开始强制 `group_content_scope_v1` 时，历史 open Action 未先完成等价快照接管或重规划。
5. **任务业务层**：郑州师范仍有 listener/reply/发送后拦截；郑州楼凤仍有群管准入和存量拦截；搜索仍有验证码、账号 cooldown 和安全容量不足。

## 2. 产品决策

### 2.1 总体决策

采用“单一中央调度合同 + 短事务固定锁序 + 存量 Action 证据化接管”的修复方式：

- 普通 Action 的 Dispatcher 分片拓扑只有一个生产真相源。
- 搜索 obligation 使用中央虚拟 `(1,0)`；从 Window `ready` 发布到唯一首次 outcome finalize 前，来源 Reservation 全部由搜索物化流程独占，finalize 后仅有效 `bound` unit 可以跨重建受保护。
- Window 容量等于当前部署拓扑可真实执行的合计预算，不使用旧 worker 数量推算值。
- Claim 热事务只维护中央 claim ledger；Task 统计和业务投影在中央 claim 前缀锁之后处理。
- 现有 AI 内容 scope、listener、reply、准入、验证码和 unknown 门禁继续 fail closed。
- 存量接管只处理未进入 Gateway 的 Action；远端事实和 unknown 不做猜测性修复。

### 2.2 P0 目标

- 所有共享 Window writer 对普通 Action 使用同一 `runtime_account_shard_total=2`。
- 生产 `DISPATCHER_SCOPE_CAPACITY` 与两个 Dispatcher 的真实合计执行预算一致，目标值为 26。
- 任一时刻满足 `scope.active_claim_count + window.effective_unclaimed_count <= scope.claim_capacity`。
- 旧 epoch 保留真实 active claim、当前合同且仍有到期 Action的普通 unclaimed、尚未首次 finalize的 search materialization-owned unit和已 bound search unit；旧拓扑/版本普通 binding必须释放后按原 Action/义务进入新 epoch，普通无到期 Action的unclaimed可直接释放，搜索 unit只能由首次 outcome/release batch判定。
- Planner、Dispatcher、AI generation、Recovery 并发 30 分钟 PostgreSQL deadlock=0。
- 所有 pre-Gateway open AI Action要么具有合法 scope contract，要么进入明确的重规划/隔离终态；Gateway-started或unknown必须各自绑定唯一 remote reconcile case。
- 三个 AI 活群出现部署后的新 `ExecutionAttempt + remote_message_id`，并正确确认原账号 coverage。

### 2.3 P1 目标

- 郑州师范 listener gap、过期上下文和缺失 reply target 可恢复重规划，不形成永久循环。
- 郑州楼凤准入等待不重复消费 fulfillment Reservation，合法 ready/probe 账号持续获得份额。
- 搜索 assignment expiry 回到由真实 Gateway 前失效产生的水平，不再由 scope 过度分配主导。
- 任务详情能区分 `shared_dispatch_blocked`、`content_contract_replan`、`waiting_context`、`waiting_admission`、`insufficient_safe_capacity` 和 `waiting_dynamic_input`。

### 2.4 非目标

- 不改变 AI、评论、点赞、浏览或搜索的目标数量和远端成功口径。
- 不设置固定 `search > AI`、`AI > channel` 或按任务类型硬编码的优先级。
- 不扩大账号组、绕过账号安全额度、放宽验证码共识、自动点击未知 callback。
- 不跳过 `group_content_scope_v1`、listener watermark、reply target 或群管准入校验。
- 不通过增加 Dispatcher、扩大数据库连接池、延长 Claim Window、直接清库或定时重启掩盖根因。
- 不自动重发 `unknown_after_send`、Gateway-started 或可能存在远端事实的 Action。
- 不建设新的运营后台、OCR 多实例 HA、持久任务队列或跨租户公平层。

## 3. 术语与合同版本

| 名称 | 定义 |
| --- | --- |
| `runtime_account_shard_total` | 当前 release 内所有普通 Action claim worker 的唯一总分片数；生产为 2 |
| `runtime_account_shard_index` | 当前 Dispatcher 的唯一分片索引，取值 `[0, runtime_account_shard_total)` |
| `virtual_search_shard` | 搜索 assignment 物化前的中央虚拟分片，固定 `(1,0)`，不代表执行 worker 拓扑 |
| `effective_worker_capacity` | `min(DISPATCHER_CONCURRENCY, DB_POOL_SIZE + DB_MAX_OVERFLOW - DB_POOL_CONTROL_RESERVE)` |
| `scope_capacity` | 当前 scope 中所有预期 Dispatcher 的 `effective_worker_capacity` 合计 |
| `search_materialization_owned_count` | Window `ready` 发布后、唯一 `SearchClickAssignmentEpoch` 首次 finalize 前，仍由搜索物化流程独占的来源 Reservation unit 数量 |
| `protected_bound_count` | 旧 epoch 中仍绑定有效 pre-Gateway 搜索 assignment 的数量 |
| `effective_unclaimed_count` | 全部当前合同且仍可领取的普通 unclaimed + `search_materialization_owned_count` + 全部有效 `protected_bound_count`；不含旧拓扑/版本、已释放或已确认不再到期的普通份额 |
| `central_lock_prefix` | `Scope -> Window -> TaskAllocation -> ShardAllocation -> Reservation` |

本次实现提升 `dispatch_rebuild_contract_version`。该版本仅进入规范化 rebuild hash；不得作为数据库历史业务事实。版本变化必须执行全量 release fence，禁止新旧 writer 混跑。

## 4. 目标架构

```text
统一部署拓扑配置
  -> runtime_account_shard_total=2
  -> Dispatcher-1 index=0 / Dispatcher-2 index=1
  -> Planner/search writer 只读取 total，不拥有普通 Action shard index

到期普通 Action
  -> DispatchLaneShardSolver
  -> TaskAllocation
  -> ShardAllocation(2,0|1)
  -> Reservation
  -> 归属 Dispatcher claim

搜索 click obligation
  -> 中央 fulfillment 份额
  -> virtual ShardAllocation(1,0)
  -> ready 后由唯一 SearchClickAssignmentEpoch 独占首次 outcome
  -> SearchClickAssignmentSolver
  -> 首次 finalize 原子生成 bound 或 released 逐 unit 事实
  -> finalize 后只有有效 bound unit 受保护
  -> assignment.account_id % runtime_account_shard_total
  -> 唯一归属 Dispatcher confirm/Gateway

Action 执行与终结
  -> B0 提交 gateway_call_started_at，保留 active claim
  -> Telegram Gateway
  -> B1 no_autoflush 获取 central_lock_prefix
  -> claim release + Action/Attempt + 业务账本/coverage/content/admission 原子提交
  -> Task quality/runtime stats 独立短事务聚合
```

## 5. 功能设计

### 5.1 唯一运行时分片拓扑

新增必填生产配置 `DISPATCH_RUNTIME_SHARD_TOTAL`：

- 生产固定为 `2`，由 release 配置传入 backend、planner、recovery、AI generation 和两个 Dispatcher。
- Dispatcher 仍保留 `ACCOUNT_SHARD_TOTAL/INDEX` 作为账号归属执行参数，但启动时必须验证 `ACCOUNT_SHARD_TOTAL == DISPATCH_RUNTIME_SHARD_TOTAL`。
- 非 Dispatcher writer 不使用 `ACCOUNT_SHARD_INDEX` 生成普通 Action demand；它们只读取 `DISPATCH_RUNTIME_SHARD_TOTAL`，由中央 solver 映射全部 due demand。
- `DispatchClaimScope` 保存当前 `runtime_shard_total`、`topology_fingerprint` 与 `capacity_config_fingerprint`。两个 fingerprint 都使用 UTF-8 canonical JSON（字段名排序、数组按 shard index 升序、紧凑分隔符）后计算 SHA-256，并保存 `fingerprint_schema_version=dispatch_topology_v1`。
- `topology_fingerprint` payload 精确为 `fingerprint_schema_version`、`dispatcher_scope`、`runtime_shard_total`、`expected_shard_indexes`、`dispatch_rebuild_contract_version`；`capacity_config_fingerprint` payload 精确为前述 topology hash、每 shard 的 `DISPATCHER_CONCURRENCY/DB_POOL_SIZE/DB_MAX_OVERFLOW/DB_POOL_CONTROL_RESERVE/effective_worker_capacity` 与 `DISPATCHER_SCOPE_CAPACITY`。
- hostname、pid、worker/lease identity、heartbeat 时间、当前 live shard 集合、扫描时间和随机值禁止进入 fingerprint；这些易变事实只能进入 liveness 投影。
- 首次建立或合同版本切换只能在 `Scope` 行锁内 CAS 写入；任一 writer 只能回读同一 canonical payload，不得按本进程观察顺序重算不同 hash。
- 任一 writer 发现数据库 scope fingerprint 与本地配置不一致时，不创建或领取新 claim，显式写 `dispatcher_topology_mismatch`；已 Gateway-started Action仍按原事实收口。
- 任务保持 `running`，运行态为 `blocked/shared_dispatch_configuration`，不能伪造完成或自动切换 scope。

每个预期 shard 使用唯一 `DispatchRuntimeShardState(dispatcher_scope, shard_index)` 保存当前 owner、最近 heartbeat、配置 hash、`liveness_version` 与 `live|recycling|stale`。`DISPATCH_SHARD_STALE_SECONDS=120`：

- 120 秒内的 graceful recycle 只把 shard 标记 `recycling`，不改变 topology/capacity fingerprint，也不把账号重新路由给其他 shard。
- 超过阈值仍无同配置 heartbeat 时标记 `stale`，禁止给该 shard 创建新普通 Reservation，搜索 solver 也不得选择归属该 shard 的账号 path；受影响任务显示 `dispatcher_shard_unavailable`。
- 当前 Window 的实际新增预算为所有 live shard 剩余预算之和；单 shard 新 allocation 不得超过该 shard 的 `effective_worker_capacity - active - effective_unclaimed`，因此缺一个 shard 时最多新增 13，而不是继续按 26 过配。
- shard liveness 的业务当前时间与数据库 `heartbeat_at` 必须先按平台北京时间语义归一后再转 UTC 比较。`_now()` 返回的无时区值是北京时间墙钟，不得直接附加 UTC；否则两个真实 live shard 会被误判为过期并让全部 Reservation 以 `shared_dispatch_capacity_insufficient` 写零。修复不得通过扩大 stale 秒数掩盖时区错误。
- 同 index、同配置的新 owner 恢复 heartbeat 后，在 `Scope` 行锁内递增 `liveness_version` 并触发一次 Window rebuild；它只能恢复本 shard 的新分配，不能接管其他 shard 的账号或重放已 Gateway-started Action。

### 5.2 共享容量合同

生产目标参数：

| 参数 | 值 | 计算依据 |
| --- | ---: | --- |
| `DISPATCHER_CONCURRENCY` | 20 | 当前显式配置 |
| `DB_POOL_SIZE` | 5 | 当前显式配置 |
| `DB_MAX_OVERFLOW` | 10 | 当前显式配置 |
| `DB_POOL_CONTROL_RESERVE` | 2 | 保留控制/终结事务连接 |
| 单 worker effective capacity | 13 | `min(20, 5+10-2)` |
| Dispatcher 数量 | 2 | 固定两个 shard |
| `DISPATCHER_SCOPE_CAPACITY` | 26 | `13 * 2` |

规则：

1. `ACTION_CLAIM_LIMIT` 不是 scope capacity，不能直接进入共享容量。
2. 所有共享 worker 必须读取相同 capacity/fingerprint；不一致时 fail closed。
3. Window 先计算 `scope_available = capacity - active - effective_unclaimed`，再以所有 live shard 的剩余预算之和取上界；最终 `available = min(scope_available, sum(live_shard_available))`，且 solver 同时满足逐 shard 上限。
4. 容量不足只形成 `shared_dispatch_capacity_insufficient` 并进入下一 Window，不减少任务目标、不终止 Task。
5. 任何任务类型不得通过 urgency 或 `is_strict` 永久占满全部 Window；既有父任务最低机会与 lane/shard 公平继续生效。
6. Dispatcher 临时 graceful recycle 不改变配置总容量；该 shard 未领取份额在 Window/release contract 下自然释放，不能转给非归属 worker。超过 liveness 阈值后只降低实际新增预算并暴露 blocker，不改 fingerprint 或账号归属。

### 5.3 普通 Action 与搜索 obligation 的 demand 边界

- 通用 Dispatcher demand 只来自当前 due、pending、running、满足执行前基础资格的真实 Action。
- 未物化搜索 obligation 不得伪装成普通 Action demand。
- 搜索 fulfillment writer 可以在中央分配事务中提交搜索业务 debt，但普通 Action 部分必须用统一 `runtime_account_shard_total` 构造。
- 搜索 source 仍固定虚拟 `(1,0)`；它不允许被普通 `_selected_actions` 消费。
- 搜索 assignment 物化后，根据冻结 `assignment.account_id % runtime_account_shard_total` 路由给唯一 Dispatcher；非归属 worker只跳过，不释放、不改绑。
- 搜索不能以巨量 remaining 获得固定类别优先；中央 task/lane 公平完成后，搜索 solver 只能在本任务已获 fulfillment 份额内选择 path。

### 5.4 跨 epoch Reservation 守恒

Window rebuild 前必须按以下顺序处理旧事实：

1. 锁定 central lock prefix。
2. 重算真实 active claim。
3. 对旧 epoch Reservation 逐条分类：
   - `claimed`：计入 scope active，不计入 unclaimed。
   - `search materialization owned`：来源是 search fulfillment，且唯一首次 outcome 尚未 finalized；全部 remaining unit 计入 `search_materialization_owned_count`，通用 reclaimer 必须跳过。
   - `bound search assignment` 且 Window/Action/版本仍有效：计入 `protected_bound_count`。
   - `released`：不计容量。
   - `ordinary current-contract unclaimed`：存在仍可领取的到期 Action，且 shard total/index、合同版本与当前 runtime一致；继续可领取并计入 effective unclaimed。
   - `ordinary stale-contract unclaimed`：旧 shard topology或合同版本不可由当前 worker领取；在同一事务释放旧 dispatch binding、清除 Action的旧 Reservation metadata并写 `dispatch_binding_replan_required`，原 Action/义务以当前拓扑进入新 demand，禁止终结业务义务或复制 Action。
   - `ordinary no-longer-due unclaimed`：Action已终态、不再到期或任务不再 running；按 `unclaimed_action_no_longer_due` 调整普通 Reservation counter、reason和version。
4. 两类 ordinary release都不得写搜索 `DispatchAllocationExclusion`；`window.effective_unclaimed_count` 等于全部 current-contract普通unclaimed、`search_materialization_owned_count` 与全部有效 `protected_bound_count` 之和。
5. 从最新完整输入建立新 epoch；旧 allocation 行保留审计，但其普通 unclaimed counter 不再直接进入新 available 计算。
6. 新 TaskAllocation、ShardAllocation、Reservation、Window ready 和同一 rebuild hash 原子提交。

搜索首次 outcome 的所有权固定为：

- Window `ready` 发布后，只要 search fulfillment Reservation 已存在且首次 outcome 尚未 finalized，即使 `bound_count=0`，也不得被 no-Action、unclaimed、expiry 或通用 rebuild release。
- 唯一 `SearchClickAssignmentEpoch` 必须承载首次 `optimal|no_candidate|abandoned` 结果。owner 丢失或 Window 已结束时由 recovery 创建/取得该唯一 carrier 并直接 finalize `abandoned`，以逐 unit exclusion 释放未绑定份额；禁止通用 reclaimer 代写该结果。
- 首次 finalize 必须在一个事务内完成 assignment binding、全部 unmatched release/exclusion、Reservation counter 与 outcome hash；提交后每个来源 Reservation 满足 `bound + claimed + released = reserved`。
- 首次 finalize 后，只有有效 bound assignment 继续受保护；后续失效只能经唯一 `DispatchAllocationReleaseBatch` 逐 unit 释放。
- `DispatchAllocationExclusion` 只服务搜索 `(window,reservation,fulfillment_lane_claim_ordinal)` unit，必须具有 search epoch 或 release batch carrier；普通 Reservation 永远不创建伪 ordinal 或伪 carrier。
- 搜索 assignment 在原 Window 已结束后发生 Gateway 前释放时，仍要按原 Reservation/Allocation/Window 扣减历史 `unclaimed_allocated_count` 并写唯一 release carrier；但已结束 Window 的 `effective_unclaimed_count` 已退出当前容量预算，必须保持为 `0`，不得再次扣减。只有尚可领取的 Window 才同时扣减 `unclaimed_allocated_count` 与 `effective_unclaimed_count` 并开启/加入 rebuild wave。该分支必须在任何 effective 负数检查之前按同一业务时钟判定，避免历史预绑定释放让 Dispatcher 整轮 drain 失败。

强制不变量：

- `scope.active_claim_count >= 0`
- `window.effective_unclaimed_count >= 0`
- `active + effective_unclaimed <= capacity`
- `reservation.bound_count + claimed_count + released_count <= reserved_claims`
- 搜索首次 outcome 未 finalized 时，通用 release 对该来源 Reservation 的写入数必须为 0
- 搜索首次 outcome finalized 后等式必须收口为 `bound + claimed + released = reserved`
- 同一 `(window, reservation, ordinal)` 最多释放一次，旧 unit 不复活

任一不变量失败时，当前对象进入 `dispatch_ledger_invariant_failed` quarantine；不得自动调数字、删记录或继续发布部分 ready。

### 5.5 统一锁序与事务拆分

所有 claim、confirm、release、reconcile 使用：

`Scope -> Window -> TaskAllocation -> ShardAllocation -> Reservation -> search carrier/assignment（如有） -> Action -> Task/业务账本`

#### 事务 A：中央 claim ledger

- 使用 `session.no_autoflush` 获取完整 central lock prefix。
- 获取前禁止 Task stats、Coverage、ContentMix、Admission、Account 等 ORM dirty 对象 autoflush。
- 完成 claim confirm/release、Reservation counter、Action dispatch binding 的原子更新。
- Claim 热事务禁止 `UPDATE tasks`。

#### 事务 B0：Gateway 调用边界

- 外部 Telegram RPC 前必须先用独立短事务持久化 `ExecutionAttempt.gateway_call_started_at`、冻结目标和 Action identity；该事务提交成功后才允许调用 Gateway。
- B0 同时为每个 Attempt 冻结唯一 `gateway_request_identity`，写入 Action result 与 Attempt snapshot；identity 不依赖进程、重试次数或提交时间，后续 evidence journal、只读远端核验和 case CAS均以它关联。
- `gateway_request_identity`、`gateway_request_fingerprint`、`gateway_target_fingerprint` 是 Attempt 的不可变 B0 快照。Gateway 返回后的成功、失败、延期、membership 重排或 projection 只能把新结果字段合并进 Attempt snapshot，禁止用 Action 当前 result 整体覆盖并丢失这三个冻结字段；否则必须显式失败，不能把权威 `remote_mutation_started=false` 误降为 unknown。
- B0 覆盖全部有远端副作用的 Gateway 调用，包括正文/评论发送、浏览、点赞、join/invite，以及群管频道 follow 与精确 callback；不得因它们属于“准入辅助动作”而直接调用 Gateway。群管 follow/callback 的冻结请求必须包含 admission id/version、账号、目标群或频道、source message、trusted bot、button row/col/text 等实际输入。
- 该事务只建立远端调用防重边界，不释放 dispatch claim、不确认业务成功。

#### 事务 B1：Gateway 后原子终结

- Gateway 返回后新建一个数据库事务，先以 `session.no_autoflush` 取得 central lock prefix，再锁 search carrier/assignment、Action、ExecutionAttempt 与类型专用业务账本。
- send/comment 的正常 Gateway 结果必须在任何 AI memory、speaker/stance、Task stats、coverage 或 ContentMix 写入前取得 central dispatch prefix；禁止先持有这些业务行再倒序等待 `DispatchClaimScope`。若 Gateway 后需要继续执行准入/群管外部 RPC，必须先结束外部调用，随后再开启同一 B1 锁序，不能持锁调用 Telegram。
- dispatch claim release、Action 终态、ExecutionAttempt 结果、coverage、quantity/content、评论/点赞/浏览/search ledger、membership 与群管 admission 专用事实必须在该同一事务中提交；中间禁止 commit，也禁止在 claim 已释放后另开事务补 Action/Attempt。
- 浏览/点赞的类型专用远端事实只允许由 B1 收尾投影确认一次。Gateway 返回路径负责携带冻结源消息 `remote_fact_id` 和 mutation 证据，不得先创建 View/ReactionRemoteFact 后再由通用收尾重复创建；在 `SessionLocal(autoflush=false)` 下也必须保证单 Action 单远端事实对象、单 INSERT。
- B1 任一步失败必须整体 rollback。最后已提交状态仍是 `gateway_call_started_at + active claim + executing/待核验`，不得形成 `dispatch_claim_active=false` 但 Action 仍 executing 的持久空窗。
- Recovery 发现 B0 已提交而 B1 未提交时，使用同一锁序在一个短事务写 `Action.status=unknown_after_send`、`Action.result.error_code=content_contract_remote_reconcile_required`、`ExecutionAttempt.status=result_unknown`，释放 active claim并把业务义务转为 unknown hold；随后只能进入远端核验，不得自动重发。
- Gateway-started/unknown 继续按原防重合同，不因事务拆分重发。

#### 发布激活账本收敛范围

- `reconcile-ledger` 在业务 writer 已 fence 且 candidate 为 preparing 时运行。它只对尚未结束的 Claim Window 执行 allocation/reservation/未领取份额完整守恒；对已结束 Window 只按真实 `executing + dispatch_claim_active` Action 修复 Window/Allocation active 投影，修复后 closed active drift 必须为 0。
- 已结束 Window 的历史 unclaimed、搜索首次 outcome、bound unit 与 release batch 仍由原 Search epoch/release-wave 和常规回收协议收口；激活流程不得重新取得 owner、不得扫描并改写全部历史 Reservation，也不得因历史审计行存在而延长当前业务 fence。
- 上述 closed active 必须为0只适用于 writer 已 fence 的激活事务。合同 active 后，Window 结束时仍在 Gateway/B1 生命周期内的 `executing + dispatch_claim_active` Action 可以继续占用原 Window active claim，直至其原子终结释放；这是合法跨窗在途，不得被 post-deploy `verify-active` 误判为 drift。运行期只读验证必须先锁定 Scope 以冻结 claim/release 前缀，再证明 Scope active 等于真实 active Action 数，且 Window/Allocation active 投影与每条 Action 冻结的 Window/Allocation binding 精确相等；任一缺失或错绑仍 fail closed。同一 ORM Session 若已在 candidate 校验无锁读取 Scope，后续行锁查询必须强制用数据库最新值覆盖 identity map；禁止用旧 Scope 缓存与锁后的新 Action 集合拼接成混合快照。刷新只修复观察一致性，不得自动改写账本或吞掉真实漂移。任何 pre-Gateway 门禁、目标失效、群发送限流或准入窗口忙分支，只要把持有 active claim 的 Action 从 `executing` 改为 `pending|skipped|failed`，就必须与 Scope/Window/Allocation claim 释放在同一事务提交；只有 Action 继续保持 `executing + dispatch_claim_active` 的 Gateway attempt/journal 边界允许提前提交。禁止先提交 Action 状态、再由外层事务补释放 claim。
- `effective_unclaimed_count` 只在 `bucket_end > observed_at` 的 live Window 内参与容量；时钟跨过 `bucket_end` 后逻辑 effective 无条件为0，数据库中尚未被后续 owner 触碰的非零值仅是历史未领取快照，与 `unclaimed_allocated_count` 一样继续供唯一 release carrier 审计。运行期校验不得要求一个没有写事务的时钟边界自动改行，也不得把 closed 存储值重新计入容量；但 live Window 的 stored effective、Allocation/Reservation 和容量守恒仍必须严格一致。
- Scope、目标 Window 与 Allocation 必须按稳定顺序批量锁定/装载；禁止对每个历史 Window 再分别查询 Allocation、Reservation 和 due Action。发布复杂度必须取决于未结束 Window 与残留 active 投影集合，而不是主库累计历史 Window/Reservation 总量。
- live/closed Window 必须由数据库使用同一个 `observed_at` 表达式完成分类并返回分类结果；禁止把数据库载入的 offset-naive 时间与应用层 offset-aware 时间在 Python 直接比较。
- 生产数据库 Session 即使关闭 autoflush，收敛后的 Window/Allocation/Scope 投影也必须在同一事务内显式 flush 后再执行聚合 invariant 查询；flush 或校验失败均整体回滚，禁止因读取事务内旧值误报漂移，也禁止拆成提前提交来绕过校验。

#### 事务 C：Task 统计投影

- `record_quality_event`、`clear_quality_blocker` 和 runtime summary 在独立短事务聚合。
- Task stats 失败只显式记录 projection error，不回滚 Action/远端事实，也不能保持 claim 占用。
- 统计事务不得再次进入 dispatch claim release。

### 5.6 AI 存量 scope contract 接管

提供内部一次性 `preview/apply` 操作，不新增公开运营 API。

#### Preview 分类

| 分类 | 条件 | 处理 |
| --- | --- | --- |
| `already_current` | open Action 已有合法 `group_content_scope_v1` | 不写 |
| `equivalent_snapshot_safe` | 未进 Gateway；Task/group/account、chat identity、CycleSlot/数量槽、context/reply/memory 均可由同 ledger 证明 | apply 补等价 scope snapshot |
| `replan_required` | 未进 Gateway，但 payload/正文无效，或 context/reply/memory/slot 任一证据缺失或已过期 | 终态 `content_contract_replan_required`，释放原绑定并按原义务重规划 |
| `remote_reconcile_required` | 已有 Gateway start 或发送结果未知 | 不改 Action；进入真实远端核验 |
| `immutable_terminal` | success、confirmed、visible_confirmed 或其他终态远端事实 | 不改 |
| `quarantine` | 跨 Task/group/account、唯一键或 ledger 事实互相矛盾 | 对象级隔离，零自动写入 |

#### Apply 规则

- preview 必须在全部业务 worker 已 fence 后运行；新 batch 只选择 `pending|claiming|executing|retryable_failed|unknown_after_send` 可变候选，历史不可变终态 Action 在查询层排除，不创建重复 noop item。它在同一快照中冻结候选 Action ID、observed action state hash、分类、分类输入 hash、cutoff 和 actor，为每项创建不可变 `AiContentScopeTakeoverItem`，再对按 Action ID 排序的全部 item 计算 batch classification hash；supersede 已有 batch 时仍可按原 item 精确收口。
- batch 状态为 `previewed|applying|blocked|completed`；item 状态为 `pending|applied|noop|conflict|quarantined`。preview 只写 batch/item 控制事实，不修改 Action 或业务账本。
- `action_state_hash` 是以下字段的版本化 canonical JSON SHA-256：Action identity、tenant/task/type、status、account、scheduled time、claim/lease owner/token/expiry、quantity/content slot identity、retry_count，以及分类实际读取的 payload/result scope/Gateway键；正文只进入已有正文 fingerprint，不把明文复制进 item。时间统一UTC ISO-8601，map键排序，禁止使用PostgreSQL `xmin`、进程时间或把 `retry_count` 冒充行版本。
- apply 输入包含 preview batch id、classification hash、expected counts 和 actor。首次 apply 前必须锁 batch并确认全部 pending item 的 `action_state_hash`/分类输入 hash仍与 preview相同；任一漂移则 batch=`blocked`、对应 item=`conflict`，本次业务写入为 0。
- 首次校验通过后 batch 进入 `applying`。使用小批次 `FOR UPDATE OF actions SKIP LOCKED`，每批按 item ID稳定排序、独立提交 Action/业务账本、item outcome 和 AuditLog；`applied|noop|quarantined` item 永不再次写业务状态。
- 每批提交后持久化 processed/applied/noop/conflict/quarantine counts 与最后 item cursor。进程崩溃或部署中断后，使用同一 batch/hash 从首个 `pending` item继续；不得重新用已修改数据库计算一个必然漂移的全量 hash。
- 生产 Session 关闭 autoflush 时，首次 conflict 标记及每批 Action/item outcome 必须在同一事务内显式 flush 后再聚合 counts 和判断 batch finish；禁止 counters 读取旧 item 状态，flush/聚合失败仍整体回滚当前批次。
- 每个 pending item实际处理时仍必须在Action行锁内重算并核对 frozen action state hash/input hash；首次全量校验后出现的漂移把该 item记为conflict并立即阻止后续批次，不允许因batch已是applying就跳过CAS。
- 仅当全部 item 离开 pending 且 `conflict=0` 时 batch=`completed`。出现 conflict 时已经安全提交的 item保持有效，新 preview 只允许覆盖旧 batch 未处理/conflict 的 Action，并引用 superseded batch；不得回滚已提交 item或重复修改。
- Stage C 所认的 takeover completed 是 batch chain闭包：最新 head batch为completed，且从初始 preview到所有 superseding batch的候选并集不存在 pending/conflict、同一Action最多一个业务写入outcome。
- 补快照不能改变正文、direct/reply、目标群、账号、素材类型、数量槽或 scheduled time。
- replan 必须复用原 coverage/quantity/content obligation，不创建第二份业务目标。
- success、unknown、Gateway-started 永远不进入自动重排。
- `immutable_terminal` 仅用于旧 batch/supersede 的防御性分类；新 preview 的运行成本必须随可变候选规模增长，不得随全部历史终态 Action 无界增长。
- 对 `completed` batch 重复 apply 返回原 counts/hash，新增写入为 0；对 `applying` batch重复 apply只续跑 pending item。
- 即使发布脚本顺序错误，Dispatcher、AI generation、Planner 和 Recovery也必须对缺 `group_content_scope_v1` 且未带 completed takeover item 的历史 Action执行 `legacy_content_scope_takeover_pending` claim gate，禁止调用 Provider/Gateway或抢先终结；该 gate只在 batch completed 后解除。

#### Gateway unknown 远端核验闭环

每个 `remote_reconcile_required` Attempt 建立唯一 `RemoteReconcileCase(action_id, execution_attempt_id)`；内部受控 workflow 支持 preview/apply，不开放运营批量重发按钮。核验只允许使用冻结账号、目标 peer/chat identity、Gateway request identity、发送时间窗口、正文/媒体 fingerprint 和只读 Telegram 历史，不调用发送、编辑、删除、callback、join 或 follow RPC。

| 核验结果 | 充分条件 | 原子处理 |
| --- | --- | --- |
| `remote_confirmed` | 发送类：同账号、同 peer、限定时间窗内找到唯一精确 fingerprint并取得非空新 remote message ID；浏览/点赞类：同一 Gateway request identity 的独立 journal明确 `remote_mutation_started=true`，且冻结账号、peer、源消息 ID、reaction与完整 canonical payload hash一致；membership 类：冻结账号/peer的权威只读 probe 唯一确认 joined/can-send；群管 follow/callback：同一 request identity 的独立 journal记录成功且带匹配 action type 的类型化 remote fact；歧义匹配不算 | central prefix 后在同一事务写 Attempt success/远端事实键、Action success、类型专用远端事实、membership/admission、coverage/quantity/content确认并释放 claim |
| `remote_absence_proven` | 仅接受 Gateway 持久 request identity 给出的权威 `remote_mutation_started=false` 或 Telegram明确拒绝且无远端写入；仅凭历史中“没找到消息”不充分 | pre-Gateway failure终态、释放 claim和旧 runtime占位，原业务义务按原 slot/ordinal重规划一次 |
| `inconclusive` | 历史缺失、消息可能删除、多个近似匹配、接口超时或证据不足 | Action/Attempt保持 unknown hold，释放 runtime claim但不释放业务义务、不计成功、不自动重发 |

- `unknown_after_send` 只能占用防重 memory与业务义务 hold，不能提前写账号群 stance/已发言事实；`remote_absence_proven` 必须覆盖“历史 Attempt 有 Gateway start，但权威证据明确 no-mutation”的反例：旧 Attempt只保留为审计历史，不得再触发 Gateway-started terminal规则；AI CycleSlot清空 `current_action_id` 并置 `replan_required`，原数量槽回到 `open`，原 AI message memory由 `unknown_after_send` 转为 `failed/replan_required`，历史上以 Action ID写入的 unknown stance占位必须失效，不得继续占用重复拦截或已发言资格。
- `remote_confirmed` 对 AI 发送必须同步类型专用事实：AI message memory写 success、真实 remote message ID与 sent_at，账号群立场记忆以真实 remote ID覆盖 unknown占位；任一类型专用事实写入失败都使 B1/CAS 整体失败，不能只把 Action标成 success。
- 浏览/点赞成功不得伪造“新消息 ID”：Attempt/case保存冻结源消息 ID作为 `remote_fact_id`，分别调用 View/Reaction确认器创建唯一远端事实；journal request fingerprint必须覆盖完整 canonical Action payload hash，target fingerprint至少覆盖账号、peer、源消息 ID、channel message ID与reaction，不能因白名单漏字段把不同操作判成同一证据。
- membership unknown 只允许一个终结状态机：Recovery 的权威只读 probe必须先找到同一 Action/Attempt 的 `RemoteReconcileCase`，以冻结账号、peer、target revision和 probe 结果生成类型化 `remote_fact_id`，再调用统一 evidence CAS；禁止在 case 外先改 Action/Attempt。probe必须遵守冻结payload的`require_send`：false只要求fresh Session可resolve/可访问，true才要求当前可发言，不能把只读频道订阅误判为权限失败。存量 `unknown_after_send` 若没有 ExecutionAttempt，或最新遗留Attempt缺少`gateway_request_identity`/request fingerprint，Recovery 必须在持有精确 claim 后保留旧Attempt并追加持久化 `legacy_unknown_read_only_recovery` 的 result-unknown Attempt，记录来源Attempt、冻结当前request identity/payload并建立case，提交B0后才允许发起只读probe；该Attempt不伪造原mutation成功或remote ID，也不得回填旧Attempt。confirmed在同一事务写`ChannelMembership=joined`与admission item，失败/超时保持case/action unknown，不建立第二套成功路径。
- 群管 follow/callback 成功 journal 使用 `action_type + gateway_request_identity` 形成类型化 `remote_fact_id`。remote confirmed apply必须重放对应业务事实：follow调用 `mark_channel_follow_completed`，callback只记录 `accepted_waiting_bot_confirmation`并继续等待群管真实回执；二者都不得直接把 admission 猜成 ready。
- case冻结 `expected_action_state_hash + expected_attempt_state_hash`；Attempt hash覆盖 identity、status、Gateway前后时间、remote ID、failure、result fingerprint，排除扫描时间。通用 Action hash保留全部dispatch/recovery claim与lease。membership Recovery只有在证明当前持有精确 `RecoveryClaim.token`、且仅排除这个token后的shadow hash仍匹配case旧expected时，才把case expected推进为包含当前claim的完整Action hash；apply始终使用完整hash，其他owner/token/expiry变化必须冲突。inconclusive外层释放本次claim后还必须再次推进expected，保证下一轮精确claim可重入。apply在Action/Attempt行锁内重算两个hash并连同`evidence_hash`做CAS，重复相同evidence零写；不同结论、hash漂移或证据冲突进入`remote_reconcile_conflict` quarantine。禁止用不存在的ORM version、`retry_count`或数据库私有`xmin`代替该合同。
- 新 case 的 expected hash 必须在 Action/Attempt/Case 定向 flush 后 refresh 数据库持久表示再计算，避免 JSON tuple/list、时区或驱动规范化造成“创建即漂移”。既有 conflict 只有在原 conflict evidence hash 与本次权威 evidence 完全一致、审批者显式提交当前 Action/Attempt hash、Action/Attempt 仍保持 unknown 且受保护 workflow 记录冲突复核审计时，才可在同一事务把 case 重置为 pending 后立即执行统一 evidence CAS；任一 hash/evidence/state 不符继续隔离，禁止直接改业务表。
- `remote_confirmed` 与 `remote_absence_proven` 必须写 actor、证据来源、脱敏 fingerprint、核验时间和远端 ID/明确失败码；禁止保存正文或凭证副本。
- deadline 到达时 `inconclusive` 计入 unknown/held shortfall并使 E4 未通过；安全防重优先于自动补量，不能为了让任务显示完成而猜测 absence。
- Gateway 返回后、B1 开始前必须把最小结果证据写入独立 `GatewayRequestEvidenceJournal` 短事务：唯一 request identity、Action/Attempt、账号、目标与请求 payload 的脱敏 fingerprint、`remote_message_id`、明确失败码、`remote_mutation_started=true|false|unknown`和观察时间。journal 不写正文、peer明文或凭证，不改变 Action/业务账本；相同 identity 只能幂等重放完全相同的 evidence，不同 evidence 必须冲突隔离。journal 提交失败必须显式记录，此时 B1仍可按真实 Gateway 结果收口，但若 B1也失败则 case只能 `inconclusive`。仅 journal 明确 `remote_mutation_started=false` 才能支持 absence-proven；普通超时、连接中断或“历史没找到”仍不充分。
- journal 写入必须读取 B0 已提交的 Attempt 父行及其冻结快照；任何 B1 结果合并都不得先删除冻结键。PostgreSQL 与 SQLite 回归必须分别覆盖“Action result 被 membership 延期结果整体替换”以及“autoflush=false 的 view/reaction 成功收尾”，证明前者仍能写 no-mutation journal、后者不会产生唯一键冲突。
- 生产核验入口固定为受保护的 `Production Remote Reconcile` GitHub Actions workflow。preview 只读取指定 case 和选定证据源，输出完整 evidence fingerprint；apply 必须再次锁定同一生产 release、case、证据源和 preview fingerprint，并提交 actor 与 approval ref 后调用统一 evidence CAS。worker 不自动消费发送型 journal，workflow 也不得调用任何写 Telegram RPC；journal 已有唯一 remote ID 时只原子回填 Attempt、Action 和类型专用业务事实，重复 apply 必须零写。workflow 缺失、release 漂移、fingerprint 漂移、证据 inconclusive 或 conflict 均阻断，不允许以手工 SQL 或重发替代。

### 5.7 三个 AI 活群专项恢复

#### 郑州大学

- 公共调度修复后，合法 ready Action 应直接获得 `(2,0|1)` Reservation。
- 不为该任务增加专项优先级；其恢复用于证明普通合法 Action 不再被搜索虚拟份额阻塞。
- 验收以新 Attempt、非空 remote_message_id、Action/Attempt/coverage 同账号为准。

#### 郑州师范

- `context_freshness_unproven`：Listener 必须以持久 cursor 追平到 `contiguous`，有 gap/error 时继续 waiting，不调用 Provider。
- `context_expired`：释放旧 generation 内容，不改原义务，按最新同群上下文重生成。
- `reply_target_missing`：旧 Action 终态并回到原 reply 槽重规划；Planner 必须重新选择合法 reply，不能把引用义务静默改成 direct，也不能冻结不存在的引用。
- 托管账号的历史成功消息可作为 `own_history` 引用目标，但必须由同 tenant、同 Task、同目标群的既往 `Action.status=success` 与其最新成功 `ExecutionAttempt.remote_message_id` 共同证明，且既往 Action 冻结正文非空；正常发送与 `remote_confirmed` 对账成功复用同一合同。此类出站消息无需写入 `GroupContextMessage`，因为 Listener 必须继续排除托管账号出站内容；Planner、Phase B 本地 guard 与 Gateway 前 scope validator 必须复用上述权威事实，禁止一方可选、一方误拒。远端存在性校验必须按冻结的 `reply_to_message_id` 精确读取单条消息，不能用“最近 N 条消息中未出现”证明目标不存在；精确读取确认缺失或当前发送账号不可访问时才写 `reply_target_missing`，并在 Action result 冻结 `reply_target_observation=remote_missing_or_inaccessible`、目标 ID 与准入 probe 摘要。Planner 必须在同 tenant/Task/group 的后续候选查询中排除已有该权威远端观察的目标，禁止失败槽释放后再次选择同一已证伪目标形成无限重试；本地 scope/账号 link guard 的拒绝不得污染全局目标池。存量精确读取失败若已记录 `validation_stage=ai_reply_target` 且错误详情不是本地 guard 固定文案，按同一远端观察兼容识别。池耗尽后继续走显式 reply shortfall/原槽等待或既有覆盖回补合同，不得把引用义务静默改成 direct。跨 Task、跨群、缺成功 Attempt、空 remote ID 或空冻结正文均不得进入引用池。
- `own_history` 分页必须先在数据库查询中按同租户、同目标群跨任务排除已被 `pending|claiming|executing|unknown_after_send` Action 占用的 `reply_to_message_id`，再按成功时间取本轮上限；禁止“先截取最新 N 条、再在内存排除在途目标”，否则最新窗口耗尽时会漏掉更早但仍合法的成功消息并制造 `reply_target_missing`。`success` 只证明一次引用发送已经完成，不得把被引用消息永久占用；完成后该目标可在后续 Cycle 再次进入候选池，同一规划批次仍须去重，任何在途或远端结果未知的引用继续硬排除。查询必须保持有界，最终候选仍在 Action 创建前复核一次在途占用状态以覆盖并发。
- `post_send_intercepted`：保留失败事实；账号回到 admission 流程，不计 coverage，不自动重发同一正文。

#### 郑州楼凤

- `group_bot_admission_wait` 不消费普通正文 fulfillment Reservation。
- ready、唯一 post-follow visibility probe、可当次进入 probe 的账号优先于 waiting admission，但仍服从跨任务公平。
- `legacy_group_bot_intercepted` 与历史 duplicate 保留终态，不回 pending。
- `ai_generation_output_count_mismatch` 只重规划受影响 CycleSlot，不重置整日 Cycle 或冻结账号分母。

### 5.8 搜索点击恢复边界

- 保持 Claim Window 60 秒，不用延长 TTL 掩盖过度分配。
- 只有中央份额、assignment、Action 和全部搜索资源 CAS 同时成功才算 committed opportunity。
- 未在 Window 内 confirm 的 assignment 按 `search_assignment_expired` 原子释放；修复后该原因应反映真实执行不及，而不是 scope capacity 翻倍过配。
- OCR worker 的 busy、验证码 required/failed、transport、cooldown、assignment expiry 分开统计。
- 不放宽 2/3 共识，不允许模型单票、未知 callback 或 direct click fallback。
- 完成只认 `target_click_observed=true` 的完整 click evidence。
- 若 `hard_safe_remaining_capacity < remaining_click_count`，任务保持 running，并展示 `production_blocked: insufficient_safe_capacity`；不得扩展账号 scope或把 projection 当 confirmed。

### 5.9 评论、点赞、浏览边界

- `pending>0、due=0` 显示 `scheduled_future`，不显示 blocked。
- `message_scope=dynamic_new` 且没有新 source/message obligation 时显示 `waiting_dynamic_input`。
- 有 due Action、无 active claim 时进入共享调度诊断；公共修复后按跨任务公平领取。
- 评论 `context_bound_schedule_window_seconds` 只限制 Planner 创建 reply Action 时的近端排期，不得把 `Action.created_at` 或排队时长当作引用目标 TTL。到期执行时只要冻结的 `ChannelMessageComment` 仍属于同租户、同频道目标和同源消息，必须继续生成并发送；只有目标真实缺失、删除或不可访问才以 `reply_target_missing` 终结并让原 ordinal 重规划，不能因为计划提前创建而写 `reply_target_stale`。
- 评论 Action 在 Gateway 前失败并释放 `CommentFulfillmentObligation.current_action_id` 后，通用 `failure_policy` 不得把旧 Action 重新置为 `pending`；只能由 Planner 为同一 ordinal 建立新 attempt，否则旧 Action 与 replacement 会同时到期并被 `comment_obligation_superseded` 连续跳过。
- 评论重规划每轮最多领取本轮 `message_comment_quantities` 已分配的义务数，不能把该消息全部未绑定义务一次性跨小时建完。reply ordinal 必须从当前仍存在的同租户、同频道目标、同源消息评论池重新冻结 `reply_to_message_id`；池不足时保持该 reply ordinal 未绑定等待，不得复用已被 `reply_target_missing|reply_target_stale` 证伪的旧快照，也不得降级为 direct。
- 评论、reaction、view 仍只以各自远端事实键确认，不因调度修复改变数量口径。

## 6. 数据与配置设计

### 6.1 数据模型

| 对象 | 变更 |
| --- | --- |
| `DispatchClaimScope` | 增加/固化 `runtime_shard_total`、`topology_fingerprint`、`capacity_config_fingerprint`、`fingerprint_schema_version`、`candidate_contract_version`、`active_contract_version`、`contract_activation_state=preparing|active`；版本变化受锁保护 |
| `DispatchRuntimeShardState` | 新增；唯一键 `(dispatcher_scope, shard_index)`，保存 expected capacity/config hash、current worker/lease、heartbeat、`live|recycling|stale`、`liveness_version` |
| `DispatchClaimWindow` | 新增非空 `effective_unclaimed_count`，它是新合同唯一容量扣减字段；旧 `unclaimed_allocated_count` 只保留历史兼容审计，新 writer 禁止读取它决定 available |
| `DispatchClaimShardAllocation` | 新增非空 `dispatch_contract_version`，在 allocation 创建时冻结当前 rebuild contract version；历史 epoch 保留审计，空/不同版本按 stale-contract 处理，不从不可逆 hash 猜版本；普通 Action shard total 必须等于 runtime total，搜索虚拟分片例外有明确 business kind |
| `DispatchClaimReservation` | 继续使用 reserved/bound/claimed/released 守恒；新增行为不允许负数或超上界 |
| `AiContentScopeTakeoverBatch` | 新增；保存 cutoff、actor、classification hash/counts、状态、cursor、各 outcome count、源/被 supersede batch 与 release/config version |
| `AiContentScopeTakeoverItem` | 新增；唯一键 `(batch_id, action_id)`，保存 observed action state hash、classification/input hash、`pending|applied|noop|conflict|quarantined`、outcome 与 processed_at |
| `RemoteReconcileCase` | 新增；唯一键 `(action_id, execution_attempt_id)`，保存 expected Action/Attempt state hash、evidence hash、`pending|remote_confirmed|remote_absence_proven|inconclusive|conflict`、actor和脱敏审计 |
| `GatewayRequestEvidenceJournal` | 新增；request identity唯一，保存 Action/Attempt、脱敏request/result fingerprint、remote ID、明确mutation状态与`recorded|conflict`；独立于B1持久化 |
| `Action.result` | 记录 `content_contract_takeover_outcome`、batch id、原 contract 状态、replan reason，不保存正文副本 |
| `AuditLog` | 记录 preview/apply actor、cutoff、分类计数、hash、release/config version |

数据库迁移必须幂等检查真实列；既有行的 topology 字段与 allocation `dispatch_contract_version` 不猜测回填为成功事实。旧 `unclaimed_allocated_count` 不回填为新 `effective_unclaimed_count`；新 release 在全部 writer fence 后持锁重算 active、search materialization-owned、protected bound 与普通 due unclaimed，守恒通过后才写新字段并建立 fingerprint。只有部署配置和两个 expected shard heartbeat 同时一致，首次 Window 才可 ready。

### 6.2 配置

新增或调整：

- `DISPATCH_RUNTIME_SHARD_TOTAL=2`：所有共享 writer 必填。
- `DISPATCHER_SCOPE_CAPACITY=26`：两 worker 生产目标。
- `DB_POOL_CONTROL_RESERVE=2`：显式化现有效并发预留值。
- `DISPATCH_SHARD_STALE_SECONDS=120`：配置/liveness 分离后的 shard 失联阈值。
- `DISPATCH_TOPOLOGY_FINGERPRINT_SCHEMA_VERSION=dispatch_topology_v1`：canonical payload版本。
- `dispatch_rebuild_contract_version`：提升版本并进入 hash。

配置校验发生在 worker 启动和取得新 Window ownership 前；非法值使进程明确失败或 scope fail closed，不使用默认值静默继续。

### 6.3 API 与前端

- 不新增任务创建、编辑、暂停、恢复 API。
- 复用任务详情/日履约接口展示标准化 `runtime_state/blocker_code/blocker_stage/next_decision_at`。
- 列表摘要只显示业务可理解状态，不展示 raw shard id 或数据库锁信息。
- 详情诊断可展示脱敏字段：当前/期望 topology、各 shard live/recycling/stale、configured/live capacity、active、effective unclaimed、最新 Window、等待阶段。
- Preview/apply 为受控内部脚本或 workflow 输入，不暴露前端按钮。
- apply 与 remote reconcile apply只允许受保护 GitHub Actions production environment的部署身份执行，并必须显式提供 actor、batch/case identity、expected hash与审批记录；普通后台用户和 worker service identity无调用权限。

## 7. 状态与错误合同

| Code | 层级 | 是否终态 | 恢复路径 |
| --- | --- | --- | --- |
| `dispatcher_topology_mismatch` | scope | 否 | 发布配置/contract version 一致后重建 Window |
| `dispatcher_scope_capacity_mismatch` | scope | 否 | 修正 capacity fingerprint 后重建 |
| `dispatcher_shard_unavailable` | scope/task | 否 | 同 index、同配置 heartbeat恢复后按 liveness version重建；不跨 shard接管 |
| `dispatch_ledger_invariant_failed` | object quarantine | 否 | 人工审计具体 Window/Reservation，不自动调数 |
| `shared_dispatch_capacity_insufficient` | task/window | 否 | 下一 Window 公平重算 |
| `dispatch_binding_replan_required` | Action/dispatch binding | 否 | 清旧拓扑/版本Reservation metadata，以同Action/义务进入当前runtime demand |
| `legacy_content_scope_takeover_pending` | Action | 否 | fenced takeover batch完成前禁止 claim/Provider/Gateway |
| `content_scope_takeover_conflict` | batch/item | 否 | 保留已提交 item，新 preview只覆盖 conflict/pending 项 |
| `content_contract_replan_required` | Action | 当前 Action 终态 | 原业务义务生成新 Action |
| `content_contract_remote_reconcile_required` | Action | 否 | 建立唯一 case，只读远端核验，不自动重发 |
| `remote_reconcile_inconclusive` | case/Action | 否 | unknown hold；新证据到达前不释放业务义务 |
| `remote_reconcile_conflict` | object quarantine | 否 | 人工审计冲突 evidence/版本，自动流程不选边 |
| `context_freshness_unproven` | Action | 否 | Listener cursor contiguous 后继续 |
| `reply_target_missing` | Action | 当前 Action 终态 | 原槽按当前上下文重新规划 |
| `group_bot_admission_wait` | coverage/admission | 否 | follow/confirm/probe/membership/can-send |
| `insufficient_safe_capacity` | task projection | 否 | 安全资源变化或业务目标调整 |
| `waiting_dynamic_input` | task projection | 否 | 新消息进入后规划 |

## 8. 存量迁移与发布方案

### 8.1 Stage A：全量 writer fence、迁移与候选版本就绪

1. CI 先以 PostgreSQL 并发、迁移重放和 crash injection 证明新合同。
2. 部署时停止旧 Planner、Dispatcher、search owner、AI generation 和 Recovery，阻止旧版本取得新 ownership；等待旧进程及其可提交事务归零。
   - worker优雅退出必须把自身 `WorkerHeartbeat.status` 显式写为 `stopped`并更新时间，保留历史行；不得删除heartbeat或依赖新容器覆盖旧identity。
   - 部署在启动 candidate worker前按已停止服务的精确 worker identity执行受控retire；若进程未确认停止则禁止retire。`verify-ready`只忽略已`stopped`行，任何新鲜`active`旧合同 writer仍 fail closed。
3. 只启动新 backend执行幂等 schema migration；在 `Scope` 行锁内写 `candidate_contract_version`、`contract_activation_state=preparing`，此时 `active_contract_version` 不变。
   - 生产配置必须在 backend启动前拒绝`ENABLE_EMBEDDED_WORKER=true`；模板默认false不等于校验通过，Settings和部署入口均须fail closed。
4. 启动全部新 worker进入 fenced readiness：上报 candidate config/shard heartbeat，但 `preparing` 状态下禁止创建 Window、claim、generation、recovery terminal或 Gateway 调用。
5. 验证 expected shard `{0,1}`、canonical fingerprint、每 shard capacity 13 与总 configured capacity 26；任一缺失继续 fenced，不得降级启动。
6. 由唯一 migration owner 收口旧 open search epoch：只按旧 carrier/snapshot直接 finalize abandoned/release，不接管或重跑旧 solver 解。
7. migration owner锁定停机后遗留 active claim：未进Gateway且owner已失效的按原Action恢复为可领取并释放claim；已进Gateway的原子释放runtime claim、保持业务unknown hold并建立唯一RemoteReconcileCase；搜索bound/claim仍使用原search carrier/version合同，禁止通用删计数。
8. 持锁从真实 active、普通 due、search materialization-owned 和 protected bound 重算新 `effective_unclaimed_count`；守恒失败保持 preparing并 quarantine。

Stage A 尚未修改业务 Action 时，可停止 candidate worker、回滚本 release并重新走 GitHub Actions；回滚后仍是 `production_blocked`，不能把旧故障版本声明恢复。

### 8.2 Stage B：保持 fence 的 AI 存量接管

1. 在所有业务 worker仍为 `preparing` 时运行 preview，持久化 batch/items、分类计数、hash和样本 ID，不改业务状态。
2. Product/QA 核对 success/unknown/Gateway-started item均为零自动修改，并确认 remote case只读核验边界。
3. apply 按 item cursor小批提交安全补快照或 replan terminal；每批同步 item outcome与 AuditLog。
4. 注入中途崩溃后必须以同 batch续跑到 `completed`；重复 completed apply新增写入为 0。
5. 任一 conflict/quarantine 使 batch blocked且 release保持 preparing；新 preview只接管未处理/conflict项，直到最终 batch completed。
6. 受影响 running Task只登记 activation 后唤醒标记；draft/paused/stopped Task不自动启动。

Stage B 是前向数据迁移，不执行逆向改写。任何已 applied item保留审计；禁止回滚到不理解 takeover batch/item、remote case或新计数字段的旧 binary。

### 8.3 Stage C：原子激活、业务 canary 与放量

1. 在 `Scope` 行锁内再次验证 candidate fingerprint、两个 live shard、takeover completed、旧 writer归零和全部 ledger守恒。
2. 同一事务把 `active_contract_version=candidate_contract_version`、`contract_activation_state=active`，发布首个新 Window ready/hash并递增 liveness/input version；不存在 worker已运行但合同尚未接管的窗口。
3. 激活事务提交后，发布脚本和 GitHub post-deploy 必须再次执行只读 `verify-active`：要求 active/candidate版本完全一致、两个 shard仍 live、无旧 writer、全部 ledger守恒且实际 capacity=26；只打印 `status` 或仅 `verify-ready` 不构成激活成功，任一失败立即使 Release Gate失败。
   - `verify-active` 可能恰在新的60秒 Claim Window 结束边界运行；它必须按运行期跨窗在途合同验证真实Action支撑的 active 投影，并按 `bucket_end` 把 closed Window 的逻辑 effective 视为0，不能要求数据库时钟自动产生写事务，也不能复用“writer fence后 closed active 必须为0”的激活前置条件。
   - candidate 无锁读取与 Scope 行锁升级复用同一 Session 时，加锁读取必须强制 refresh；并发 claim/release 后仍使用 identity map 旧值属于校验缺陷，不属于真实账本漂移。
   - pre-Gateway 阻断/延后不能先提交非 executing 状态再等待外层 finalize；QA 必须在每个显式 commit 边界证明 `dispatch_claim_active=true` 的 Action 仍为 executing，或同一提交已释放完整账本前缀。
   - 生产 Session 使用 `autoflush=false`。外层 finalize 在 Action 已改为非 executing 后重算 active Action 投影前，必须在同一事务显式 flush Action 状态；否则 SQL 会继续读到数据库旧的 executing 行并保留 Window/Allocation active 计数。该 flush 不是提前 commit，后续任一释放步骤失败仍须整体回滚。
4. worker只在观察到 active版本完全匹配后开始 Planner/generation/claim/recovery；任何旧版本或 mismatch继续零新写入。
   - worker loop冻结的`dispatch_contract_version`是heartbeat必备metadata。Planner/Dispatcher/Recovery等业务drain刷新同一heartbeat时只能合并业务字段，禁止整体覆盖或删除合同版本；`verify-active`必须在至少一次真实drain刷新后仍通过。
5. 先观察现有 running任务，不额外创建真实 Telegram测试任务；连续30分钟检查窗口守恒、deadlock、claim、Gateway和业务事实。
6. AI每个目标至少出现1条新的账号一致远端确认；搜索继续出现新的完整 click evidence。
7. canary通过后保持现有任务自然履约；完整自然日达标前仍为 `production_observing`。

## 9. QA 验收

### 9.1 自动化测试

| 场景 | 必须证明 |
| --- | --- |
| topology 配置一致 | Planner/search writer/两 Dispatcher 对普通 Action 均使用 total=2；搜索虚拟 source 保持 `(1,0)` |
| topology 不一致 | 任一 writer total/fingerprint 漂移时新 claim=0，显式 mismatch，不影响已 Gateway-started 收口；迁移后残留的越界 stale shard行只作历史忽略，不得触发数组越界，fresh旧 writer仍由独立 heartbeat gate阻断 |
| fingerprint canonical | 不同进程、输入 map/worker观察顺序得到同一 hash；pid/lease/heartbeat/time变化不改变 hash；任一受控配置字段变化必改变 hash |
| capacity 计算 | 两 worker 参数 20/5/10/2 得到 scope 26；52 配置被校验拒绝而非继续过配 |
| shard liveness | shard短 recycle不跨 shard接管；超过120秒后该 shard新 Reservation=0、live新增预算=13并显示 unavailable；同配置恢复只触发一次 rebuild |
| 搜索首次 outcome 所有权 | ready至首次 finalize期间即使 bound=0，通用 reclaimer写入=0；owner丢失/Window结束只由唯一 search epoch abandoned并逐 unit release |
| 普通跨 epoch unbound | allocation冻结的`dispatch_contract_version`与当前版本一致且仍due的普通旧unclaimed继续可领取并计数；空/旧版本或旧拓扑binding原子释放并以同Action/义务进入新demand；无到期事实则释放；三者均不写`DispatchAllocationExclusion` |
| 跨 epoch bound | 有效 search bound unit 继续可由唯一归属 Dispatcher confirm，不被普通 reclaimer 释放 |
| 守恒竞态 | 双 writer/rebuild/release 下 counter 不负、不超 capacity、不双释放 |
| 过期 Window 的预绑定释放 | 构造历史 `unclaimed_allocated_count > 0`、`effective_unclaimed_count = 0` 且 assignment 仍为 `action_bound`；精确 release 后历史 unclaimed 减一、effective 保持零、不建 rebuild wave、Action/Reservation/Exclusion 原子收口，Dispatcher 后续仍能领取其他任务 |
| shard liveness 时间语义 | 以无时区北京时间 `now` 对比 PostgreSQL `+08:00` heartbeat；120秒内两 shard 各保留13容量，超过窗口才归零，禁止把北京时间无时区值解释成UTC |
| PostgreSQL 锁序 | allocation 与 AI pre-Gateway reject/finalize 并发，30 分钟压力无 deadlock；claim 热事务无 `UPDATE tasks` |
| 激活历史规模 | 构造大量已结束Window/Reservation与少量closed active drift；激活只完整重算未结束Window，批量清零closed active投影且不改历史unclaimed/search owner，不出现逐历史Window N+1 |
| 激活 dirty Session | 使用生产同款`autoflush=false`，active投影在同事务显式flush后才执行聚合校验；校验读到新投影且最终一次提交，异常时整体回滚 |
| 激活时间语义 | PostgreSQL返回offset-naive Window时间且应用`observed_at`为offset-aware；分类完全由数据库表达式完成，激活不得产生naive/aware比较异常 |
| dirty Session | Action、Task、coverage 已 dirty 时，release 仍先取得 central lock prefix，禁止 autoflush 反向锁 |
| Gateway 后原子性 | 在 claim release、Action、Attempt、各业务账本写入点逐一注入异常，B1整体 rollback；数据库永不出现 claim inactive + Action executing空窗 |
| Gateway evidence journal | B1失败后仍能按request identity读取已提交remote ID或明确no-mutation；相同evidence重放零写、不同evidence冲突；journal失败+B1失败只能inconclusive |
| B0冻结快照保持 | Gateway后Action result被成功/失败/延期结果替换时，Attempt的request identity与两个fingerprint保持B0原值；明确no-mutation仍写journal而不转unknown |
| 群管 follow/callback 原子性 | 两类动作Gateway前都有Attempt/B0；成功/明确失败写独立journal；B1 crash后由类型化fact恢复follow/admission或callback waiting状态，零重复follow/callback |
| membership单一核验协议 | unknown membership权威reprobe通过同一RemoteReconcileCase expected hash/evidence CAS确认并写joined；存量无Attempt或最新Attempt缺冻结request identity时，保留旧Attempt并追加read-only recovery Attempt/Case；超时后释放精确claim并可由新claim重入；不得遗留pending case，也不得Action先成功后case conflict |
| legacy scope safe | 证据完整 open Action 只补等价字段，正文/目标/账号/direct-reply/slot 全不变 |
| legacy scope replan | 缺证据 Action 终态、释放原绑定、原义务只生成一个 replacement |
| takeover fence | preparing期间 Planner/Dispatcher/generation/Recovery/Gateway业务写入均为0；只有 completed batch并原子 active后开始领取 |
| worker heartbeat退役 | stop后的精确worker行变`stopped`且保留审计；fresh active旧合同仍阻断；正常stop -> stage -> verify-ready不等待120秒且不自阻断 |
| heartbeat metadata保持 | worker loop后至少执行一次Planner/Dispatcher/Recovery drain，`dispatch_contract_version`仍存在且verify-active通过 |
| production embedded fail closed | `APP_ENV=production + ENABLE_EMBEDDED_WORKER=true`在Settings和deploy env校验均失败，backend不能在Stage A前启动业务writer |
| takeover crash resume | 处理首批后kill进程，同一 batch从首个 pending续跑；已 applied零重复写，最终 counts/hash守恒 |
| takeover drift | 首次apply前Action canonical state hash漂移使整批业务写入=0且batch blocked；中途conflict保留已提交item，新batch只覆盖剩余项；retry_count不作为伪version |
| takeover batch chain | blocked batch被新preview supersede后，Stage C只在整个chain无pending/conflict且每Action最多一个业务outcome时激活 |
| fence遗留 active claim | 旧owner失效且未进Gateway的恢复可领取；Gateway-started转唯一remote case并释放runtime claim；search claim按原carrier守恒，容量无永久泄漏 |
| Gateway/unknown immutable | Gateway-started、unknown、success preview/apply 零修改、零自动重发 |
| remote reconcile | exact唯一远端事实才能confirmed；权威no-mutation才能replan；没找到/歧义/超时只能inconclusive；Action/Attempt state hash + evidence CAS重放零写、冲突隔离 |
| remote absence原槽恢复 | 构造已有 Gateway start 的 AI unknown；权威 no-mutation 后旧 Attempt保留审计、CycleSlot清空Action并replan、数量槽open、message memory释放；不得误记terminal或复制业务义务 |
| remote confirmed类型事实 | AI unknown由唯一远端事实确认后，Action/Attempt、quantity/content/coverage、message memory及stance remote ID在同一B1/CAS事务一致；任一注入失败全回滚 |
| 浏览/点赞 B1 crash | Gateway journal明确mutation=true且冻结源消息/reaction一致，B1回滚后apply重建唯一View/ReactionRemoteFact；重复apply零写，payload或reaction漂移进入conflict，不伪造新消息ID |
| 浏览/点赞单点落事实 | PostgreSQL `autoflush=false` 下成功路径每个Action只创建并提交一个View/ReactionRemoteFact；Gateway路径与B1 projection不得形成同事务双INSERT |
| 郑州师范 context | gap 保持 waiting；contiguous 后生成；过期上下文只重规划原槽 |
| reply target | missing 终态并重规划；新 Action 只引用当前同群真实消息或按规则 direct |
| 评论 reply ordinal 重建 | 旧 Action 终态且不被通用重试复活；replacement 复用原 obligation/ordinal、按本轮小时预算领取并刷新真实 reply target；池不足保持 open，禁止 superseded 自耗和 reply 降级 |
| 楼凤 admission | waiting 不占正文份额；ready/probe 可执行；intercepted 不计 coverage |
| 搜索 Window | 只提交实际可 bind 数量，expiry 原子释放；无延长 TTL/无普通 claim fallback |
| 非 AI 状态 | future 显示 scheduled；无动态输入显示 waiting；due 才进入共享调度 blocker |
| 混合高债务公平 | 同时注入巨量 search debt、三个 AI任务及到期评论/点赞/浏览，连续多个 Window中每个 eligible父任务获得持久 cursor最低机会，任一 strict/urgency类别不得独占全部 capacity |
| 评论结构失败回归 | 构造 `reply_target_missing` 后义务已释放且任务开启通用重试；旧 Action 保持终态，Planner 只建一个同 ordinal replacement，引用新评论并在真实 Gateway 成功后写非空 remote message id |

所有 backend 单测使用 `backend/.venv`，单次命令硬超时 60 秒；PostgreSQL marker 与 no_postgres 分区都必须通过，不能删测、skip 或 continue-on-error。

### 9.2 Release Gate

必须同时通过：

1. 主 PRD、专项 PRD、数据流索引、结构索引和生产运行文档同步。
2. migration upgrade/重复 upgrade 幂等，真实生产列检查通过。
3. backend `no_postgres` 与 PostgreSQL 分区全绿，frontend build 全绿。
4. 新旧 contract writer fencing 可证明；preparing期间业务写入为0，旧 owner无写资格，takeover completed后才原子 active。
5. canonical fingerprint一致，生产两个 Dispatcher均 live、每 shard有效并发13、总 configured/live capacity均为26。
6. 部署后容器/API healthy，但仅作为运行前置，不作为业务完成。
7. PostgreSQL 30 分钟 deadlock=0，Window/Reservation 守恒。
8. takeover batch/item counts守恒且无 pending/conflict；remote case无未解释的自动确认或重发。
9. AI 和搜索取得新的 E4 远端事实。

### 9.3 生产验收分级

| 证据 | 状态 |
| --- | --- |
| 代码、迁移、配置和测试完成 | `implementation_complete / qa_pass` |
| Actions、容器、API、配置 fingerprint 通过 | `release_runtime_pass` |
| 30 分钟无 deadlock且 AI/search 有新远端事实 | `E3 production_canary_pass` |
| 完整自然日五类任务分别达到自身目标 | `E4 production_fixed` |
| 软件恢复但搜索安全容量仍小于目标 | `production_blocked: insufficient_safe_capacity` |
| Telegram 孤儿消息尚未逐条核验 | `unproven: remote_reconcile_pending` |

`qa_pass` 不等于 product accepted；`product_accepted` 不等于 production fixed。

## 10. 可观测性与运营展示

必须输出以下有界指标，不保存正文、Prompt、Telegram 私密内容或凭证：

- scope topology/capacity fingerprint schema与hash、activation state、configured/live capacity、各 shard liveness/version/last heartbeat。
- 每 Window 当前 epoch、普通 shard 分布、搜索 virtual reserved/materialization-owned/bound/claimed/released。
- 每任务 required/reserved/claimed、未获配原因和下一 Window。
- deadlock、serialization abort、topology mismatch、ledger invariant quarantine 计数。
- AI takeover batch状态、pending/applied/noop/conflict/quarantined、cursor和分类 hash。
- remote reconcile pending/confirmed/absence-proven/inconclusive/conflict及最老 pending age；不输出正文、peer或凭证。
- AI generation/context/reply/admission blocker 分布。
- 搜索 assignment expiry、OCR busy、verification required/failed、cooldown、confirmed click。
- 业务确认仍读取任务专用远端事实，不从这些运行指标推导成功。

## 11. 风险与控制

| 风险 | 控制 |
| --- | --- |
| topology 迁移期间新旧 writer 混跑 | contract version fence；无法证明旧写资格失效则 Release Gate 失败 |
| capacity 从 52 降为 26 被误解为业务降量 | 明确它是同一时刻真实在途，不减少目标；跨 Window 持续履约 |
| 单 shard长期失联仍按26分配 | topology fingerprint保持稳定，liveness单独降实际新增预算；stale shard不获新份额且显式 blocked |
| 搜索 strict debt 再次垄断 | 保留跨父任务最低机会与 lane/shard solver，不新增固定类型优先 |
| 通用回收误伤搜索首次 outcome | 首次 finalize前全 Reservation由搜索物化流程独占；finalize后只有 bound受保护，release必须有合法 search carrier/unit |
| 统计拆事务丢失运营计数 | Action/result 先保存事件；独立聚合可重放，业务事实不依赖 Task.stats |
| 历史正文跨群误发 | scope 安全校验不放宽；证据不全只 replan，不补猜测字段 |
| takeover小批中途崩溃 | 持久 batch/item/cursor从 pending续跑，已 applied item不重算、不回滚、不重复写 |
| deadlock 后远端已发但 DB 未记账 | B1原子 rollback；Gateway-started进入唯一 remote case，只有精确远端事实或权威 no-mutation才能收口 |
| 搜索目标客观不可达 | 独立显示 safe capacity blocker，不扩账号 scope、不伪造 click |

## 12. Product Design Complete 自检

| 检查项 | 结论 |
| --- | --- |
| 用户原始要求 | 已覆盖公共调度、AI、搜索及非故障任务边界 |
| 功能设计 | 唯一 topology、配置/liveness分离容量、搜索首次 outcome所有权、全量远端mutation B0/B1、可恢复接管、单一远端核验和分任务恢复完整 |
| 前端状态 | 复用现有详情，定义 scheduled/waiting/blocked/unproven 状态 |
| 后端/API/Worker | 配置、scope/shard state、solver、claim、generation、recovery、takeover batch/item和remote reconcile workflow均定义 |
| 数据流转 | Task debt -> allocation -> search first-outcome ownership -> reservation -> Action -> Attempt B0 -> Gateway -> B1/remote case -> ledger 完整 |
| 权限安全 | 不新增公开写 API；内部 apply 有 actor/hash/audit；安全闸门不放宽 |
| 失败路径 | mismatch、shard stale、invariant、deadlock、serialization、takeover conflict/crash、remote inconclusive/quarantine和容量不足均显式 |
| 并发/幂等 | 锁序、preparing/active fence、worker heartbeat退役/metadata合并、epoch、release unit、batch/item cursor、remote evidence CAS与重放均覆盖 |
| 数据一致性 | counter 守恒、远端事实不可猜测、旧业务义务不复制 |
| 发布/迁移 | Stage A fence/readiness、Stage B fenced takeover、Stage C原子激活、可回退边界、Release Gate和自然日E4已定义 |
| QA | 自动化、PostgreSQL并发、crash/drift注入、混合公平、remote reconcile、canary和完整日验收已定义 |
| 遗漏复核 | 未引入固定优先级、隐式 fallback、账号扩 scope、验证码放宽或 mock success |
| 逆向审查闭环 | 原3个P0/6个P1及完成性审计新增3个P0/2个P1均已映射到功能合同、发布顺序、失败路径和QA场景 |
| `design_status` | `complete` |

## 13. Product Handoff

### 13.1 Dev 交付包

- `dispatch-core`：唯一 topology、canonical fingerprint、shard liveness、跨 epoch守恒与rebuild。
- `dispatch-locking`：central lock prefix、B0/B1原子边界、Task stats延后聚合。
- `ai-takeover`：持久 batch/item/cursor、scope preview/apply、等价快照和replan。
- `remote-reconcile`：唯一 case、只读证据、confirmed/absence-proven/inconclusive CAS收口。
- `ai-runtime`：context/reply/admission 的原义务重规划与份额资格。
- `search-runtime`：virtual shard、bound 保护、归属路由、expiry/release 守恒。
- `observability`：统一 blocker、指标与任务详情投影。
- `release`：配置、migration、preparing/active contract fence、Stage A/B/C workflow与诊断。

多个可写 Agent 并行前必须登记 `locked_paths`，由同一 `merge_owner` 合并；不得让两个 Agent 同时修改中央 allocation/ledger 文件。

### 13.2 QA 交付包

- 以 §9 的每行场景建立需求到测试映射。
- 先跑红测证明当前 1/2 shard 混用、52/26 过配、旧 epoch 泄漏和反向锁序。
- 修复后逐项变绿，再执行生产一致 PostgreSQL并发、release fence、takeover crash resume和migration replay。
- 生产验收按 runtime、canary、完整自然日三层分别签字。

### 13.3 Prod-diagnosis 回收口径

发布后重新查询真实生产：

- 当前 release/容器配置和 scope fingerprint。
- Window/epoch/shard/reservation 守恒。
- PostgreSQL deadlock 与当前 blocking。
- 三个 AI 的 coverage、Action、Attempt、remote_message_id。
- 搜索的 assignment、verification、cooldown 和 `target_click_observed`。
- 评论/点赞/浏览的 due 输入与远端事实。

只有完整自然日目标全部达成，才能把本 L3 标记为 `production_fixed`。

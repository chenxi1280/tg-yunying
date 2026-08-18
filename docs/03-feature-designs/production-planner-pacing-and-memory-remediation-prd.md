# 生产 Planner、拟人排期与内存压力修复 PRD

## 0. 文档状态

| 项 | 结论 |
| --- | --- |
| Intake ID | intake-2026-08-17-planner-pacing-memory-001 |
| 分级 | L3 / P0 资源风险 + P0 来源突发 + P1 拟人节奏，必须走标准生产事故流与 Release Gate |
| 设计状态 | product_design_complete / dev_handoff_ready=true / resynced_2026-08-18-15 |
| 实现状态 | `133eee7a` 已发布；source Gateway marker、materialization CAS 败方收敛与 Python 3.6 受审计代理退役工具均在生产生效。24 个零消费者 Mihomo runtime 已按 manifest apply/readback 完成 |
| 生产状态 | partial：`133eee7a` 后 AI/view 权威 Gateway gap 无突发，但线上 E4 复核发现浏览最终闸门错误使用单消息 plan total 计算整个频道 gap，两个任务已有 3510 条 pre-Gateway Action 排到当日截止后；该容量组合遗漏进入 RC-P9/P10，production_fixed=false |
| 当前生产基线 | 2026-08-18 22:38 北京时间；release `133eee7a`；current/SHA/migration/health/restart/OOM、OCR authenticated ready 与 Docker/containerd 单实例读回通过。24 个退役 target 为 disabled+stopped+restart=no，AuditLog=24，37 个有消费者 runtime 均 healthy/running 且 manifest 未变；Planner PSS 约 208 MiB，15 秒 CPU 最大 40.38% |
| 权威关系 | 本文规范性取代生产稳定性 PRD 中 Planner 资源和旧 AI fail-open gate 口径，并补正拟人节奏 PRD 的跨批恢复；不改变各任务 stable owner、typed remote fact、unknown 与数量结算合同 |
| 操作边界 | 用户已授权实现、发布与生产验证；精确 stats cleanup 仍须独立 preview/hash/apply/readback，禁止把发布授权扩张为批量重试或未知外发 |

“拟人化排期仍有明显拥挤”包含两种相反但同源的容量错误：一是 overdue 技术批次在 now 附近重新起算造成突发，二是把单消息 plan total 的 gap 套到共享频道时间线，导致多消息任务被过度串行并排到业务周期以后。二者都不等于“任务全部瞬间完成”。真实 Gateway 必须按来源聚合 plan 平滑，且任何 pre-Gateway Action 不得跨越数量 owner 的 period deadline；Telegram 完成仍只认 typed remote fact，不能用 Action.executed_at 代替。

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
- 5dc5f345 复核中，6 个 current AI Task 共 2,164 条 pending/retryable Action、payload 约 12.5 MiB；旧数量槽收口、面具合同检查、hard-hourly 检查和旧 admission snapshot 回填会在每次 task 规划前重复加载这些完整 Action，而实际 legacy-anchor/hard-hourly 命中均为 0。
- extra-volume 路径每个 Task 先扫描租户完整账号 ORM，再与仅 17～116 个 confirmed coverage 账号求交；current 日覆盖维护还会无界读取全部 ready coverage 后再载入账号，虽然 current contract 后续不会使用该结果。
- 708d7250 发布后 82 个 Planner 样本显示 PSS p50/p95 为 495/533 MiB、CPU p50/p95 为 58.30%/68.26%、drain p50/p95 为 46.7/78.9 秒。残余首个破损边界是 `_recover_stale_fact_first_actions`：每个 Task 无界加载 3,555～8,597 个 `failed/retryable_failed/skipped` Action，单 Task payload 约 19～52 MiB；六个 Task 真正仍绑定 coverage/variation 的 Action 合计仅 9 个，其余历史孤儿在每轮处理后状态和关系均不再变化，却永久重复进入 ORM。
- 5ac00b69 发布后的短窗已把 PSS p50/p95 降至 153/188 MiB、CPU p50/p95 降至 1.16%/23.67%、drain p50/p95 降至 55 ms/8.4 秒，且 Telethon/cgroup event 为 0。继续遗漏审计发现通用 `retry_failed_actions` 仍先读取最多 100 个 fact-first `send_message` 失败 Action，再由 Python 对每一行恒定拒绝重试；这些行还能占满 limit，使同 Task 其他可安全重试的非发送 Action 饥饿。
- 2b0790ad 发布后 383 个资源样本显示 Planner PSS p95 209 MiB、CPU p95 21%，但 265 个 processed drain 的 p95 为 35.4 秒，且 15 分钟 drain p95 从 11.3 秒逐步升至 52.8 秒。`strace` 显示 12 秒内约 2,650 次数据库 send；生产慢查询捕获 own-history Action/Attempt ORM 查询最长约 8.4 秒。根因是 reply pool 在 limit 前对候选 Action 使用相关 `NOT EXISTS` 逐行判断目标是否已被占用，放大数据库往返/扫描。修订为一次性投影当前 tenant/group/状态下 distinct used target，再以 anti-join 在 limit 前排除；生产代表性 `EXPLAIN ANALYZE` 从观测慢查询的秒级放大降为约 0.44 秒，且保留“先排除已用目标、再取候选”的无饥饿语义。
- 当前宿主的另一独立内存 owner 是图片核验 worker：同时预热 RapidOCR/ddddOCR 后空闲 RSS 约 416 MiB，近三小时两次重启均为退出码 0、OOMKilled=false 的优雅换代；该 worker 按 640 MiB soft RSS 或 100 个完成请求回收。它解释聚合宿主压力，但不解释 Planner drain 慢查询，不能用其换代结果代替 Planner 修复验收。
- d75c6bbd 发布后进一步读回：图片 worker 在 10 个请求后空闲 PSS/RSS 约 444/454 MiB，宿主 MemAvailable 约 1.17 GiB。隔离基准证明完整 `RapidOCR()` 为整图验证码额外加载 det/cls/rec 三套模型；改成 recognizer-only 并保留 ddddOCR 后，同镜像合成图 RSS 从约 451 MiB 降至 222 MiB、耗时从 2.04 秒降至 0.30 秒，RapidOCR 输出不变。该修订不改变双源、deadline 或 typed fact，仅移除不参与验证码合同的 det/cls runtime。
- 8c8178be 发布后 OCR worker 在 20 个请求后 PSS 约 228 MiB、busy=0，当前版本 12 个去重真实 challenge 均为 RapidOCR/ddddOCR accepted 且 local consensus，证明 recognizer-only 语义成立。AI 冻结 release 最小同源 gap 18 秒且 0 planned violation，但自然 due 后连续 0 Gateway；首个后置破损边界不是 pacing，而是 21 条已由 `content_contract_replan_required` 终结的旧 Action 仍保留 active 消息预留，使新 Action 触发 `duplicate_message` / `check_in_scope_occupied`。
- 977a1642 已把 takeover 终结 Action 与其 pre-Gateway 消息预留失效放入同一事务。生产存量按精确谓词 preview 得到 21 条、classification hash `f238c54c52c328c938099e7ce7502067fee666a3324c83946465e8bd99ce3961`；apply 在同一事务锁行并重算 count/hash，仅把这 21 条改为 `expired_before_send`，写入 21 条 AuditLog。独立 readback 为 active orphan=0、repair=21、audit=21，且清理后 `check_in_scope_occupied`=0；后续 `duplicate_message` 均指向已有 success 记忆，属于真实历史去重而不是孤儿占位。
- 第二轮遗漏审计证明“全部 pre-Gateway defer”不是通过证据：PostgreSQL `INSERT ... ON CONFLICT DO NOTHING` 的 `rowcount` 被当作 inserted boolean，冲突命中已有 admission 时仍进入 created 分支，使用已被未来 reservation 推进的 source next cursor 覆盖原 `call_not_before_at`。同一浏览 Action 的单条 admission 因而从原 planned release 反复后移到次日/数日后，永远追不上队尾。新建判定必须以 `RETURNING admission.id` 是否返回值为唯一依据；conflict 必须复用原 admission 时间，禁止改写 source/owner cursor。
- `49142ff1` 生产读回证明 conflict 修复有效，但暴露两个后继边界。其一，admission key 仍绑定 Action；旧 Action 在 Gateway 前终结并生成 replacement 后，新 Action 不会接管同一 stable owner 的旧 reservation，而是按已污染的 source next cursor 新建到队尾。其二，多个已过期 reservation 恢复时只看各自冻结时间，没有再次应用最近真实 call-start + gap，导致同来源 backlog 可同秒进入 Gateway。正确合同是 stable owner 精确复用最早的 pre-Gateway reservation，并以 `max(frozen admission time, last real call-start + adjacent gap)` 作为最终门；禁止读取 future reservation cursor，也禁止过期 backlog 突发补发。
- `009fcde8` 已恢复自然 E4 且 gap 无违例，但生产追踪暴露第三个 pacing 身份边界：同一 replacement Action 第一次接管最早 owner reservation 后，未到 final gate 的重试会再次按 owner 最早时间选择另一条历史 reservation，而不是固定已绑定本 Action 的 admission；结果 17 个已成功 Action 残留 30 条 reserved admission。重试必须优先锁取已经绑定当前 Action 的安全 admission，只有当前 Action 尚无绑定时才选择 owner 最早 reservation。
- 同批 AI typed fact 的 `task_day_ledger_id` 37/37 为空，但 37/37 都能由 `Action.primary_quantity_slot_id -> TaskGroupDailyMessageSlot.task_day_ledger_id` 唯一解析。原因是 obligation projection 与 remote fact 只从 payload 读取 ledger，而 AI payload 合同不包含该字段。quantity owner 是权威 ledger 归属；写 projection/fact 时必须先认显式 payload，缺失时按 Action 的 primary quantity owner 解析，禁止落无 ledger 的 AI 完成事实。

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
| RC-R6 | 统一 worker 入口在每个专用角色启动时 eager import 全部服务实现 | 专用 role 最小包初始化 + 显式 lazy implementation loader；缺失入口直接失败 |
| RC-R7 | OCR worker 自回收与发布 `compose stop/remove` 竞态，Docker 元数据先删除但 restart manager 在 containerd 再拉起旧 task | 发布停 worker 前暂时关闭精确旧 OCR 容器 restart policy，确认停止后恢复；新 worker ready 后核对主机只存在一个且 cgroup container id 等于 Docker current id |
| RC-R8 | 63 条 Mihomo 资源中 26 条连续 44 天无账号、授权、活动绑定、环境、分组、在线保活或未完成登录消费者，其中 24 个容器仍常驻 241533 KiB PSS | 显式 target + 当前 release + DB/容器/配置 manifest hash 两次核对；同事务锁代理记录并重算全消费者为 0 后写 disabled/audit，再对精确容器设 `restart=no` 并 stop；保留容器、volume 和 config，任一漂移或停止失败显式中止 |
| RC-P1 | recovery cursor 是 batch-local | 四类 stable owner 的 source-wide cursor |
| RC-P2 | AI 最终 gate sleep 且 fail-open，其他三类缺 gate | SourcePacingState + SourcePacingAdmission |
| RC-P3 | scope takeover 终结旧 AI Action 时未失效消息预留，替代 Action 被旧 duplicate/check-in scope 占位阻断 | takeover 同事务失效 `AiGroupMessageMemory` + 既有孤儿精确审计清理 |
| RC-P4 | PostgreSQL admission upsert conflict 用不可靠 `rowcount` 判断 created，既有 reservation 到点时被重新排到来源队尾 | `ON CONFLICT DO NOTHING RETURNING id` 判定真实插入；冲突复用冻结 admission，不推进 cursor |
| RC-P5 | admission 绑定 Action；pre-Gateway 旧 Action 被替代后，同一 stable owner 的新 Action另建队尾 reservation | 按 owner/type/lifecycle/period/plan/source 精确锁取最早 pre-Gateway reservation，并原子转绑 replacement Action/Attempt |
| RC-P6 | 过期 reservation 恢复只看冻结时间，多条 backlog 可在同一秒进入 Gateway | reused admission 最终时间取冻结时间与最近真实 call-start + 最大相邻 gap 的较大值 |
| RC-P7 | 同一 Action 的 gap 重试重新遍历 owner 历史 reservation，单个成功 Action 绑定多条 reserved admission | owner 查询先按 `action_id == current` 排序，再按冻结时间；首次 replacement 接管后，后续重试固定同一 admission id |
| RC-P8 | Source state 在 final gate 入口记录 pre-Gateway 时间，ExecutionAttempt 在后续语句记录 Gateway marker；两段可变开销使下一次按 state 放行后，权威 Attempt 相邻间隔比冻结 gap 短数十毫秒 | 在同一 source-state 行锁事务内写入 Attempt Gateway marker 后，按当前 admission 精确绑定把 state last/next cursor 前移到该 marker，再提交放行；不得扫描历史、整数截断、增加任意安全垫或放宽验收阈值 |
| RC-P9 | 浏览 owner 的 `pacing_plan_total` 是单消息目标，但最终 `SourcePacingState` 是整频道共享；直接用单消息 total 计算共享 gap，使 21 条消息的并行计划被错误串成一条 87 秒时间线 | 浏览 Gateway gap 使用同一 TaskDayLedger 全部 active message target 的冻结 `due_count` 聚合总量；单消息 quantity owner、release、fact 和 plan hash 不变，聚合只用于共享来源最终闸门 |
| RC-P10 | 来源 defer 可把 `Action.release_not_before_at/scheduled_at` 推到 owner deadline 以后；普通 due-claim 只扫描 `scheduled_at <= now`，导致确定不可能执行的 pre-Gateway Action 在未来数日占用 current obligation 和 source cursor | defer 计算超过 deadline 时最多排到 deadline；direct claim 额外扫描“release 已越过 account reservation source deadline”的有界候选，立即写 `safely_not_executed`、missed reservation、cancelled pre-Gateway admission，并按剩余 reserved admission 与真实 last marker 重算 source next cursor |
| RC-E1 | AI projection/fact 只读 payload ledger，但 AI quantity 合同把 ledger 固化在 primary quantity owner | payload ledger 缺失时按 `Action.primary_quantity_slot_id -> TaskGroupDailyMessageSlot.task_day_ledger_id` 解析并持久化 |
| RC-E2 | 多 AI generation worker 同时为同一 obligation 绑定 replacement Action，CAS loser 把 winner 已提交的新 projection version 误报为业务身份冲突并中断整轮 drain | CAS 失败后重新读取权威 projection；winner 为当前 Action 则幂等成功，winner 为另一 open Action 则当前 Action 显式 `duplicate_open_obligation` 终结，projection 已关闭则显式 `obligation_not_open`；只有无法解释的身份/状态才继续抛 conflict |
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

current AI extra-volume 只能从同一 TaskDayLedger 已 confirmed coverage 中按“eligibility_rank、planner_last_selected_at、当日成功数、item id”读取最多 20 个 ID，随后才加载对应账号；扫描过的 item 同事务推进 planner_last_selected_at，下一轮继续公平轮转。current 全账号日覆盖的规划前维护不得再次物化 ready coverage 或账号池；fact-first 明确不适用的旧数量槽/admission snapshot 扫描直接跳过，仍适用的 legacy 清理必须把 JSON 合同谓词下推数据库且每轮最多处理 100 个真实命中 Action。

fact-first 终态 Action 恢复不得从 Action 历史状态反向全扫。候选只能由当前 `TaskAccountDailyCoverage.reserved_action_id` 与 `AiCoverageVariationIntent.action_id` 的 live 关系并集驱动，再关联 Action 核对 task/type/status 和 pre-Gateway 事实；单轮最多读取 20 个 Action。释放 reservation 并清空 variation action 关系后，该 Action 必须自然退出下一轮候选。没有 live 关系时历史 Action ORM 必须为 0，不能靠清理、重启或缩短留存达到该结果。

通用自动重试对 current fact-first AI 必须在 SQL 中排除 `send_message`；这些 Action 由 coverage/variation 重建合同负责，不能先加载再逐行拒绝，也不能占满 retry limit 阻塞同 Task 的 membership 等其他显式可重试动作。其他任务类型与非发送 Action 的既有 retry policy、backoff 和安全闭集不变。

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
7. 动态频道任务重置时必须把 required_snapshot_revision 推进到当前 revision + 1，并把 next_probe_at 推到当前时刻；新 revision 就绪前 Planner 不得复用重置前快照。
8. Listener 找不到可用采集账号时必须把 subscription 写成 unavailable 并 durable wake Planner；不得因缺少 account_id 而永久停留在 pending。

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

在稳定排序取得 source advisory lock 后，从全部匹配 owner 读取 max release 和 max pacing ordinal；新 owner 的 `pacing_slot_ordinal` 必须从 `max ordinal + 1` 连续分配，与数量 owner 自身的 `slot_ordinal/target_ordinal` 解耦，已有 frozen owner 则必须复用其 pacing ordinal。新 freeze 与 cursor 前进同事务。只有当前 source/plan 从无 frozen release 时才可用首次 recovery anchor。剩余 plan ordinal 容量不足或冻结身份冲突时写 typed pacing conflict、停止该 source 新物化并进入确定性退避，不回退 batch-local 算法或 30 秒通用重试。

### 4.6 SourcePacingState 与 SourcePacingAdmission

SourcePacingState 每 tenant/pacing_domain/source 一行；pacing_domain 区分 AI send、view、comment、reaction，但同一 domain、同一真实群/频道跨 Task 共用时间线，防止两个 Task 各自合规却在远端形成突发：

- next_call_not_before_at、last_call_started_at、last_source_gap_seconds、revision、version

SourcePacingAdmission 每个真实远程尝试一行：

- owner_type/id、action_id、attempt_id、source identity、plan hash
- planned_release_at、call_not_before_at、source_gap_seconds
- state：reserved、call_started、remote_unknown、finished、cancelled_pre_gateway
- version、created_at、updated_at

最终调用流程：

1. claim 只能建立 reserved admission，不在 Dispatcher sleep；未来时间把 Action defer 到 call_not_before_at 并释放 worker slot。
2. Gateway 入口前必须锁共享 SourcePacingState，重新计算 max(owner release、账号 pacing、source last_call_started + max(last source gap, current frozen source gap))。
3. 尚未到时只 defer，不调用远端；已到时 CAS admission 为 call_started，并在同一短事务更新 last_call_started_at，提交后才允许 Gateway call。
4. DB、锁、版本或身份失败写 pacing_source_admission_unavailable/conflict，保持 pre-Gateway，禁止远程调用。旧 AI warning 后继续发送的 fail-open 行为被本文废止。
5. remote_unknown 保留 owner/admission/Attempt，不自动重发；reconcile 仍按任务 typed fact 合同处理。
6. source gap 从冻结 pacing plan 的最大合法来源速率计算，不使用固定 8 秒魔数。AI 单群 owner 继续使用其 source plan total；浏览必须使用同一 TaskDayLedger 全部 `source_state=active` message target 的 `sum(due_count)` 作为共享频道 source plan total，不能把任一单消息 target 当成整频道总量。gap 使用不超过平均容量的整数下界，owner release/profile 继续负责非均匀拟人分布；period 内配置编辑不改变已冻结 quantity owner。
7. admission upsert 是否新建只认 `RETURNING id`；不得读取 dialect/driver 不稳定的 `rowcount`。冲突命中既有 reserved admission 时必须复用其 `call_not_before_at`，不得再次推进 `SourcePacingState.next_call_not_before_at` 或 stable owner release。
8. replacement Action 必须先按 stable owner、source state、lifecycle、period、plan 精确锁取最早的 `reserved` 或已知 pre-Gateway `finished` admission；只允许转绑 `gateway_call_started_at IS NULL` 的槽位。`call_started`、`remote_unknown`、已有 Gateway 的 admission 永不转绑。
9. reused admission 不读取 future reservation cursor，但仍必须应用实际来源时间线：最终 `not_before=max(admission.call_not_before_at, last_call_started_at + max(last_gap,current_gap))`。多个过期槽位不得突发补发。
10. 同一 Action 已经绑定安全 admission 后，后续 pre-Gateway 重试必须优先锁定并复用该 admission id，不得因其他 owner 历史 reservation 的冻结时间更早而轮换槽位。只有当前 Action 无安全绑定时，replacement 才接管 owner 最早 reservation。
11. 任一 final not-before 不得晚于 owner period deadline。计算值达到或越过 deadline 时，Action 最多唤醒到 deadline；claim 必须在 Gateway 前写 `pacing_claim_deadline_exceeded` 的 `safely_not_executed` fact，释放 quantity owner 当前 Action、把账号 reservation 标为 missed、把 source admission 标为 `cancelled_pre_gateway`。候选扫描有界且使用 `account_pacing_reservations(action_id,state)` 索引，不得等待跨日 scheduled_at 才收敛。
12. 取消 pre-Gateway admission 后，在相同 source state 行锁下按“真实 last Gateway marker + last gap”和剩余 reserved admission 最大 not-before 重算 next cursor；不得由已取消、已过期 period 或未调用 Gateway 的历史队尾继续占用新 period。

AI obligation projection 与 typed remote fact 的 `task_day_ledger_id` 取值顺序为：显式 payload ledger → `Action.primary_quantity_slot_id` 对应 quantity owner ledger。两者同时存在但不一致时必须显式失败；current AI 成功事实不得以 null ledger 落库。

release density 和 Gateway call-start density都必须满足冻结 source gap；typed remote fact density是 E4 观察结果，受 Telegram 完成时间影响，不写成可由本地强制的绝对定律。

## 5. 业务数据流与失败语义

### 5.1 Planner

Task writer
→ wake_task_planner
→ TaskPlannerWakeState revision
→ Planner 读取 compact projection / stable owner / fresh Listener snapshot
→ 有界 plan
→ 短事务冻结 owner、Action 与 planned revision

无 dirty revision 且无时间 due 时不进入业务规划。2 秒 worker heartbeat 可保留为进程活性，不触发全 Task 重算。

专用 worker 只在对应 role 首次 drain 时导入该实现模块；backend/API 的完整 service export 保持不变。loader 不得吞掉 import/attribute 错误，也不得在未知 role 下回退加载其他实现。

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

planner_pressure_v1 至少返回：

- state：fresh、stale、unavailable、degraded
- captured_at、sample_interval_seconds、release_sha、worker_id_hash
- rss/pss/private_dirty/anonymous/anon_huge_pages
- cgroup current/peak/limit/events
- cpu、thread_count、telethon_client_count
- drain p50/p95、rows/SQL/dirty/due、latest blocker code

不返回账号、手机号、session、代理、正文、Prompt 或原始 source ID。

### 6.2 前端信息架构

1. 任务详情 pacing panel 分开显示计划 release、Gateway call-start、typed fact 三条 1m/5m 密度；每条都有 captured_at、freshness、plan hash、source gap 和 blocked/unproven 状态。
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
- old lifecycle、paused、retired/superseded：不进入 current call，不自动复活。
- 缺 source/period/plan 身份：pacing_legacy_identity_unresolved，阻断该 Task/source 激活。

T2 按 Task/source 灰度，不要求暂停的 comment/like 为 AI/view 让路，也不能用 AI/view E4 代替它们。

### 8.3 Task.stats cleanup

单独 preview/apply，目标仅包括：

- membership_summary 旧账号 ID 数组
- conversation_quality_active_blockers 旧全图

每个 Task 固定 tenant/task/lifecycle、旧 stats hash、expected SHA、actor/approval。apply 前重算 active projection/count；漂移则逐 Task 拒绝。Action、Attempt、GenerationJob、variation、owner、unknown 和 typed fact 不删除、不改写。T3 与 schema/index train、资源 limit train 分窗。

### 8.4 零消费者代理运行时退役

退役只接受显式 Mihomo target；preview 必须固定 release SHA、AccountProxy 状态/版本、全部消费者计数、container id/image/restart/running 和只读 config SHA-256。apply 必须提供完整 manifest hash、actor 和 approval-ref，并在锁行事务内重算账号、授权、active proxy/environment/group binding、desired-online 及 open login flow 全为 0；任一非零或漂移都不改数据。通过后写 `disabled` 和 AuditLog，只对已锁定 container 设 `restart=no` 并 stop，不删容器/卷/配置。读回必须同时证明 target 已 stopped+disabled、非 target 有消费者 runtime 仍 running、业务容器健康和 MemAvailable 达标；Telegram 业务 E4 仍独立验收。

## 9. 滚动发布与回滚

| Train | 内容 | 激活条件 |
| --- | --- | --- |
| T0 Schema | 新投影、身份列、并发索引、v2 API；writer dual-publish | migration/index readback，旧行为不变 |
| T1 Boundary/Wake | Listener snapshot、compact projection、所有 writer 发布 wake v2、Planner shadow compare | API、Listener、Generation、Dispatcher、Recovery、Planner 心跳均报告同 SHA + capability |
| T2 Pacing | 四类 source cursor + Gateway 前 source admission；按 Task/source canary | backlog manifest 无 unresolved；shadow release/call diff 通过 |
| T3 Cleanup | 停写旧大 stats，v1 consumer cutoff，精确 cleanup | v2 第一方 readback，hash preview 通过 |
| T4 Resource | 按 cgroup 公式设置 reservation/hard/pids | T1～T3 后 24h 基线、宿主预算和启动峰值通过 |

能力闸门：

- T0/T1 前半段，新 writer 同时发布 wake v2 和兼容 next_run_at；旧 Planner 仍读旧值，新 Planner 只 shadow。
- 所有活跃角色 capability 齐全并排空旧进程后，原子切换 planner_wake_contract=v2。
- 激活后 next_run_at 只做只读兼容镜像；禁止静默回退旧 writer、全量轮询或 Planner 远程 fetch。
- 若 T1/T2 失败，停止新 materialization/call，保留 additive schema、owner、admission、Attempt、unknown 和 fact，前向修复后恢复。
- T4 可回滚资源配置，但不得批量重启或把连续触限当回收策略。
- remote OCR 开启时，Stage A 必须先读取精确旧 OCR container id，将其 restart policy 临时改为 `no`，再执行 `compose stop`；只有旧容器已确认非 running 后才恢复 `unless-stopped`。任一步失败都必须恢复 policy 并使发布失败，不能继续 remove/recreate。
- 新 OCR worker ready 后，Release Gate 必须从 `/proc/*/cmdline + /proc/*/cgroup` 枚举真实 `image_verification_worker_app` runtime；必须恰好一条 container id 且等于 Docker current id。Docker 不可见、containerd/cgroup 仍存活的旧 task 视为 `resource_capacity_degraded`，不得只看 `docker ps` 放行，也不得在发布脚本中模糊 kill。

## 10. QA、性能与生产 E4

### 10.1 正确性与并发

1. 1,210 scope、20k Action/Task、6 AI Task fixture：gate 只返回 count/revision，候选 ORM ≤20；current steady-state 无 live 恢复关系时历史 Action ORM=0，有 live coverage/variation 关系的终态恢复 ORM≤20，legacy maintenance 只加载数据库谓词真实命中的有界批次且每轮≤100；reply used-target 必须以一次 distinct 投影 + anti-join 在 limit 前排除，禁止恢复逐候选相关 `NOT EXISTS`。
2. 候选公平性：连续有界轮转后 1,210 个同优先级 item 均被访问；新高优先级可抢占，但低优先级有 starvation 观测和上界。
3. 1,534 个旧 quality blockers，其中仅 3 个 active：投影为 3，旧 map cleanup 后 summary ≤2 KiB，源事实 hash 不变。
4. 13 类 wake writer 覆盖规划中事件、旧 epoch、重复事件、回滚、暂停恢复和 mixed-version；无丢 wake、无热循环、v2 后无直接 next_run_at 写。
5. Planner 调用任何 Gateway/Telethon API 必须失败为 planner_remote_io_forbidden；soak 中 Telethon client/thread 增量为 0。
6. Listener 覆盖 fresh empty、stale、error、revision 竞态和 outbox 重放；Planner 不远程 fallback。
7. 四类各做 20 batches × 20 owners、双 Planner、回滚/重启和数量 owner 乱序选择：pacing ordinal 从 source cursor 连续分配、release cursor 单调、已有 frozen ordinal 不改，索引命中且不全表扫；plan ordinal 耗尽显式失败。
8. Gateway gate 注入 DB/lock/version 失败：四类均无远程调用；future admission defer 后 worker slot 释放，无 sleep。
9. 同来源多账号、多 Task 并发：共享 source timeline，实际 call_started 相邻间隔不小于相邻两次冻结 gap 的较大值；unknown 不重发。
10. migration 覆盖 concurrent index 失败/invalid 恢复、checkpoint resume、stop line 和 mixed SHA。
11. PostgreSQL conflict 回归：同一 Action/admission 重试至少3次，upsert conflict 均返回 created=false；即使 source next cursor 已被后续 reservation 推进，原 admission 到点也必须进入 call-start，不能再次 defer。
12. stable owner replacement 回归：旧 Action 在 Gateway 前终结后，新 Action复用同一 admission id，不新建来源队尾槽位；已有 Gateway/unknown 不得转绑。
13. overdue backlog 回归：同一来源至少2条过期 reservation 同时到达 final gate，首条 call-start 后其余必须按真实 gap 释放，任意相邻 Gateway call-start 不得同秒突发。
14. 同 Action retry pinning 回归：owner 存在至少2条 pre-Gateway 历史 reservation，replacement 首次接管后连续3次未到 final gate；每次只更新同一 admission id，其他历史 admission 的 action/attempt/state 不变。
15. AI E4 ledger 回归：payload 无 ledger 但 Action 绑定 primary quantity owner时，obligation projection 与 `remote_message_observed` 均持久化 owner ledger；payload/owner ledger 冲突显式失败，缺失两条来源不得写 confirmed fact。
16. 发布自回收竞态回归：旧 OCR 容器 restart policy fencing 必须发生在 `compose stop` 之前，停止确认后恢复；新 OCR ready 后断言真实 runtime 只有 current container id。额外旧 runtime、无法解析 cgroup id 或旧容器仍 running 均使 Release Gate 失败。
17. PostgreSQL materialization CAS 竞争：双 session 同时读到同一 open projection 的旧 version 并绑定不同 replacement Action；winner 提交后 loser 必须重读 winner。winner 为 open Action 时 loser 以 `duplicate_open_obligation` 终结且不得覆盖 projection；同 Action 重入幂等成功；projection 同时关闭时 loser 以 `obligation_not_open` 终结。任何其他漂移仍抛 `fulfillment_obligation_materialization_conflict`，禁止 catch-all 吞错。
18. Gateway marker 精度回归：前一 admission 的 state timestamp 为 T、权威 Attempt Gateway marker 为 T+250ms；下一 Action 在 T+gap 到达时必须 defer 到 T+250ms+gap，且随后写入的任意相邻 Attempt Gateway marker 差值严格不小于相邻冻结 gap 的较大值。禁止通过秒级取整、epsilon 容差或额外 sleep 伪造通过。
19. 代理运行时退役回归：target/manifest/release/actor/approval 缺失、任一消费者非零、DB 版本漂移、container id/config hash 漂移或 stop 失败均显式失败；成功仅改显式 target 为 disabled 并 stopped/restart=no，AuditLog 数与 target 数一致，非 target 不变。
20. 多消息浏览来源容量回归：同一 ledger 至少 3 条 active message target，单消息 plan total 不同；每条 owner 的 quantity 身份不变，admission 的共享 source gap 只能由 `sum(due_count)` 计算。聚合总量 15877/日时不能继续得到单消息 87 秒 gap；相邻真实 Gateway 仍不得小于冻结聚合 gap。
21. 跨日 pre-Gateway 收敛：Action 的 release/scheduled 已被历史 source cursor 推到 ledger deadline 以后时，即使 `scheduled_at > now` 也必须进入有界 direct claim；Gateway marker 保持 null，Action/账号 reservation/source admission/obligation/fact/source next cursor 一次事务读回一致。非过期 future Action 不得被提前 claim。

### 10.2 性能与资源

- 生产等价全轮 p95 ≤30 秒；单 Task p95 ≤5 秒；无 dirty/due 的业务轮 p95 ≤2 秒。
- 单 Task ORM rows ≤200，其中账号候选 ≤20；current steady-state open Action ORM=0，legacy maintenance 匹配 ORM≤100；SQL 数固定，不随历史 Action 总量线性增长。
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
- AI 只认 canonical send fact/quantity binding；view、comment、reaction 只认各自 typed remote fact。
- current typed fact 密度和最终数量按 task/source 独立报告 pass/failed/blocked/unproven。

只有资源 E3、四类适用的 pacing E3 和请求范围内的任务 E4 全部通过，才允许 production_fixed。

### 10.4 2026-08-18 最终生产读回

| 验收层 | 证据 | 结论 |
| --- | --- | --- |
| 发布与运行 | Deploy Production `32146740168` 成功；current、backend/Planner/OCR 镜像与 `RELEASE_SHA` 均为 133eee7a；migration 0153 head；关键容器 healthy、restart=0、OOM=false | pass |
| Planner 资源短窗 E3 | 76 个最终 SHA 自采样 PSS p95=208929 KiB、CPU p95=45.66%，Telethon/cgroup event=0；8 个 processed drain 线性 p95 约 27.0s，冷启动 37.7s 后为 3.9～7.1s | pass；6/24 小时斜率仍 unproven |
| OCR 资源与功能 | OCR warm PSS 230748～231192 KiB；Docker current id 与 `/proc+cgroup` 唯一 runtime id 完全相等，ready/restart/OOM 读回通过 | pass |
| 聚合宿主 | 24 个零消费者 runtime 回收 241533 KiB PSS；20 点 warm MemAvailable 最低 1925148 KiB，swap-out=0、swap-in 合计 59 页；但 SwapUsed 约 664 MiB 仍高于事故合同 512 MiB 线 | MemAvailable pass / resource_capacity_degraded |
| 排期 release E3 | `133eee7a` 后 AI/view 权威 Gateway call 分别至少 32/16，相邻 pair 31/14，最小 gap 22.212558/87.351226 秒，违例均为 0；但最终 E4 发现 view 的 87 秒来自单消息 total 错套共享频道，两个任务 3510 条 Action 跨 deadline | burst gate pass / aggregate capacity failed，进入 RC-P9/P10 |
| Gateway/CAS 准入 E3 | 最终 SHA 日志 `materialization_conflict/drain_failed/alignment_error/Traceback/ERROR` 均为 0；46 个 typed-fact Action 最大 1 条 admission，multi/stranded=0 | pass |
| 零消费者代理退役 | manifest `1b431e...1e12` 两次校验后 24 个 target disabled+stopped+restart=no、AuditLog=24；37 个有消费者 proxy healthy/running，非 target manifest 不变 | pass |
| 请求范围任务 E4 | 最终 SHA 已观察 AI `remote_message_observed`至少 30、view `view_observed`至少 15；AI ledger null/mismatch=0；comment/like 任务仍暂停 | AI/view 短窗 pass；自然日与暂停类 unproven/blocked |

因此资源与防突发修复已通过短窗，但共享来源聚合容量和跨日 pre-Gateway 收敛仍 failed；完成 RC-P9/P10 的代码、发布、存量读回和新 SHA 自然 E4 之前不得把 AI/view 短窗写成整体排期通过。宿主存量 swap、6/24 小时曲线及暂停 comment/like E4 也尚未同时满足，`production_fixed=unproven`。

## 11. Product Design Complete 自检

| 检查项 | 结论 |
| --- | --- |
| 用户原话 | 已覆盖线上短时完成、内存高、具体原因、PRD 修复和遗漏复核 |
| 前端/API | v2 summary、分页、运行状态 IA、freshness、权限和 v1 cutoff 已冻结 |
| 后端/worker | Listener 边界、wake/admission/blocker 投影、有界候选、四类 Gateway gate 完整 |
| 数据边界 | stable owner/fact/unknown 不变；新增对象均为可重建投影或调用准入 |
| 并发/幂等 | revision/CAS、source lock、mixed-version capability、outbox、checkpoint 完整 |
| 迁移/清理 | concurrent index、资源停止线、旧 backlog 分类、hash preview/apply 完整 |
| 资源 | PSS 与 cgroup 口径分离；v1/v2 自动识别、limit 公式、freshness、retention 和宿主预算完整 |
| 失败/回滚 | stale snapshot、legacy identity、DB/lock、cursor conflict、触限均显式失败 |
| QA/E4 | 本地、PostgreSQL、性能、6/24h、四类排期和 typed fact 分层完整 |

结论：product_design_complete / implementation_deployed_133eee7a / warm_MemAvailable_and_Planner_E3_pass / burst_pacing_E3_pass / aggregate_source_capacity_failed / cross_deadline_pre_gateway_failed / production_fixed=unproven。RC-P9/P10、存量 swap 512 MiB 合同、最终 SHA 的 6/24 小时曲线和暂停类 E4 尚未全部通过；固定 limit、重启、强制 GC、swapoff/扩 swap、缩目标、Action success 或健康检查均不能单独作为完成。

# AI 活群双泳道发送调度与执行链路重构 PRD

> **最新范围以统一引擎 §19.13 为准：** 聊天编辑不触发新回复或重建，取消历史回放与内容版本档案；最近成功统计默认滚动 72 小时。无需历史样本/耗时画像审批、新增调用费用预算或完整 ServiceBinding 才能运行；保留最新上下文→生成→发前复核的轻量链、数量并发占位、未决结果防重和故障隔离。下文冲突的旧要求作为历史记录，不再是开发前置条件；代码落地与线上验收另计。

## 0. 文档状态

| 项目 | 内容 |
|---|---|
| Intake ID | `intake-2026-09-03-ai-group-send-chain-redesign-001` |
| 需求级别 | L3：生产发送时效、节奏和远端副作用链路重构 |
| 用户原始问题 | 上下文回复慢；消息在短时间集中发送，没有分布在每小时的随机时间；Task 必须绑定不同账号分组，完成有波动的发送数量同时让绑定组计划账号都真实活跃；AI 要快速接入正在聊天的上下文、降低 AI 感；一个面具、账号或代理卡住不能拖停全部活群；要求先重新设计，不修改代码 |
| 适用范围 | `group_ai_chat` 从任务日计划、上下文监听、内容生成、Action、Dispatcher、Telegram Gateway 到远端事实结算的完整数据面 |
| 复用范围 | `TaskDayLedger`、账号/群准入事实、`GenerationJob` 基础租约、`Action`、`ExecutionAttempt`、Gateway 调用边界、`FulfillmentRemoteFact` |
| 不直接改动 | 频道评论、频道浏览、频道点赞和搜索点击的类型化语义；公共时间线、互动容量和执行合同由统一引擎拥有 |
| 当前状态 | `design_status=complete_for_review`、`product_design_complete=false`、`dev_handoff_ready=false`；本轮公共合同修订待复核，历史实现记录不作为本轮验收；`production_status=unproven` |
| 当前边界 | 用户确认按统一引擎 §19.4 全面支持自然人设互动，支持账号以独立真人身份交流并制造社群热度；经复核的子合同分阶段开发，发布仍需 Release Gate |

> **本轮深层业务一致性修订：** 统一引擎 PRD §19.3 是本轮公共合同依据。受管账号支持立体鲜活人设，以独立真人身份自然参与社群并制造热度。Provider 采用真实剩余容量与完整路径分位数；时间按字段编码转换；日界只停止新执行、不停止原事实对账，可见性观察允许跨日但不借今日额度；绑定坏组只隔离相应分区，不阻断下一日健康分区。旧“全部通过/已具备交接条件”的说明不覆盖本轮修订；本轮不修改实现。

> **2026-09-03 顶层关系修正：** 本文只定义统一互动履约引擎中的 `group_ai_chat` 类型适配器，公共义务投影、跨类型时间线、Action/Attempt/Gateway、远端事实协调、账号组合活跃和迁移单写者合同，以 `unified-engagement-fulfillment-engine-prd.md` 为准。本文原先提出的 AI 专用 `GroupHourlySchedule`、`GroupSendArbiter` 和公共内核对象，实施时必须映射到统一引擎，禁止形成第二套 AI 专用调度内核。

> **2026-09-03 业务闭合修订：** 本版已把原九项待确认值冻结为 v1 合同，并新增小时内 response reserve、安全释放、基于真人节奏的发送窗、admitted 分母、Provider deadline admission、逐群逐账号完成语义、同会话跨 Task 唯一 turn owner 和真人后续互动观察。上下文响应不再依赖普通主动 slot 恰好可用，也不会因同群配置多个 Task 而多账号抢答。

> **2026-09-03 拟人化去指纹补充：** 账号 persona 之外新增只由外部真人消息投影的 `GroupCommunityStyleProfileRevision`，按 group+`time_band_v1` 冻结长度、问句、标点、emoji、断句与表达 register 分布；样本不足时使用 group/time-band 级稳定宽区间 cold-start。日计划只为每条义务和当前 account-binding 冻结 `MessageStyleReservation`；主动发言在 topic/intent/planned-call 已知后、上下文响应在真实 turn/addressee/planned-call 已知后才形成 `MessageStyleAssignment`，避免在尚无对话时提前固定语气。quantity-only 义务在 Generation 前合法换号时必须追加新 account-binding/persona 的 style reservation，不能让新账号沿用旧账号声线。assignment 在群体边界内合并账号声线；direct/native-reply 关系始终由 §6.5 authority 决定，不受风格分布影响。禁止从 AI 成稿自学习、固定 hourly ordinal 风格序列、账号专属模板或虚构人物经历。该扩展设计完成、尚未实现。

> **2026-09-03 执行所有权终审：** response slot 在日计划时只有 capacity window/tentative supply，真实 planned call 只由 owner 后的 `InteractionServiceBinding` 在 turn natural window 与账号/群 Timeline 交集中冻结。canonical turn 语义分类为公共单调用 lane；每 binding 最多两次生成调用，pre-Gateway 归还后的 successor 继续扣同一任务日总预算。所有提前量、latest-safe 和安全余量统一读取冻结 `ExecutionTimingProfileRevision + path-start stage`，AI adapter 不再拥有私有 timing 常量。

> **2026-09-04 最终遗漏终审：** semantic classification 必须在当前 `ContextTurn.closed_at/candidate_decision_cutoff_at` 前为 Task fanout/claim finalize 留出测得尾部；该时点由统一 `BurstAssemblyPolicyRevision` 的 2.5/5/8/12 秒候选窗、quiet/max/deadline 规则决定，本文不再拥有固定 3 秒 cutoff。response planned call 只从完整生成链按 P95 可到达的 natural/slot/Timeline 交集内抽取，并与 Provider permit 和任务日总预算同事务 admission。真人反馈以 event-level attribution claim 单归因：原生 reply 优先，非原生续聊不能同时给多条 AI 消息记成功。该补正设计完成、尚未实现。

> **2026-09-03 账号分组与局部故障补正：** 本 adapter 不再解释 `all` 或自行扫描全租户账号；它只消费统一引擎冻结的 `TaskAccountGroupBindingSetRevision + AccountGroupMembershipSnapshotSet + TaskParticipationUnitPlan`。Task 可绑定一个或多个 enabled、用途一致的普通运营账号组，活群默认 `all_group_members_daily` 覆盖各组规范化成员并集，每个计划成员在每个目标群都必须有自己的 normal contextual fact；群日数量先做 0%～30% 的稳定均匀百分比抖动，再受逐成员 coverage floor 抬高。分组成员、selected、runtime admitted/sendable 与 confirmed 分列；单账号面具/Session/membership、单 proxy route 或正文 Provider lane 故障只形成局部 blocker/`running_partial`，健康账号继续，点赞/浏览不受面具与正文 Provider 影响。该补正设计完成、尚未实现。

> **2026-09-03 真人原生回复权威补正：** 为满足“快速加入正在发生的群聊”而不把任意历史文本当远端目标，unified route 将 reply authority 从“仅我方 typed fact”扩展为两类受控来源：同 Task/群的我方 confirmed typed fact，以及 canonical stream 中同 tenant/peer/thread、作者确认为外部真人、remote message/revision 可精确定位且 current 未删除的 `ConversationEvent`。每个 response binding 冻结 `ConversationReplyAuthorityDecision`，Provider 前与 Gateway call-issued 前复核同一 target/revision；原始 `GroupContextMessage`、sender name、正文或 Action.result 单独都不能授权。一个真人 turn 仍由 `ConversationTurnClaim` 限制为最多一个平台账号响应，call-issued/unknown 后不得换 target 或补发。该补正优先于本文及旧 own-message-reply 专项中“真人只能作 semantic context”的 legacy 限制，设计完成、尚未实现。

> **2026-09-03 远端在途 fence 补正：** 5/10/15 秒 hard timeout 只归还本地 Worker/stage/fair-share lease，不能证明 Telethon coroutine、代理 transport 或 Provider request 已停止。每次调用必须建立 durable `RemoteInvocationFence`，按 account/group/proxy route/verified egress 或 Provider route/lane 计 active remote in-flight；只有当前隔离 runner 的 termination acknowledgement 或同 invocation 权威终态才能释放在途计数。Telegram 已 call-issued 即使 transport 后续终止，业务 identity 仍保持 unknown/dedupe 直到 reconcile；TTL、Worker 重启、Future timeout 或 cancel-requested 均不能释放。该补正避免超时后绕过每账号 1 个 mutation 与代理出口并发上限，设计完成、尚未实现。

> **2026-09-03 统一生命周期与冷群终审：** 本 adapter 的 start/update/pause/resume/stop/delete、固定北京时间 task-day、同群唯一活群 Task、跨任务组合容量、结构化 FloodWait/SlowMode、观察 primary/standby 接管、非文本/语言 eligibility 和 operator safe-retry 全部服从 unified §7.6～§7.9、§8.1～§8.2。当前 task-day 的目标、selected、due 不因编辑、恢复或依赖故障重写；日目标调小只对下一完整 task day 生效，立即停止必须形成 `terminated_by_operator`，不能把欠量改成 completed。冷群 15%～20% response reserve 只在连续 7 个 observer-complete 日后按冻结公式确定；证据不足保持 40%，日内突发对话不降低 candidate/互动服务目标。FloodWait 只冻结对应 authorization/session transport scope，不打开账号/代理/Provider circuit，也不以 standby 继承已冻结账号的活跃义务。该补正设计完成、尚未实现。

> **2026-09-04 规划健康、行为 Session 与发送后结果补正：** 本 adapter 完整继承 unified §19.1。全员业务分母仍取每群全部 policy-eligible 账号，另由 `PlanningAdmissionSnapshot` 证明计划时可执行路径；部分健康时 Task 从启动起为 `running_partial` 且健康账号继续，不能删坏号或全局停发。每日 2～4 个窗口是 `AccountBehaviorSessionPlan` 的可见行为窗口，不是 Telegram 连接/Listener 在线窗口；明确点名 only 可用 `BehaviorSessionWakeDecision` 有限唤醒。所有 normal contextual 正文都要经过 15 秒普通/90 秒准入或风险的版本化 visibility gate；Observer gap 进入 unknown 且不补发。跨午夜 turn 只能通过 `CrossDayConversationCarryover` 绑定次日新义务；负反馈按分类、阈值和滞回驱动 scope circuit；互动发送 topology 必须通过单故障域失效模拟。该补正设计完成、尚未实现。

> **2026-09-04 深层组合业务补正：** 本 adapter 同时继承 unified §19.2。每群全员日覆盖仍是业务分母，但“能否自然完成”必须由 `NaturalOpportunitySupplyPlanRevision + ManagedPresencePlan` 区分当前保证量、依赖未来外部真人解锁的条件量与结构不可行量；冷群不能只凭历史均值承诺必达，也不能通过与少量真人消息交替让受管账号长期占据群聊主导比例。外部真人解锁只认去重 `ExternalHumanUnlockUnit`，限制单 actor/time-band 贡献并排除受管账号、bot/service、edit/replay/duplicate，不能让一人刷屏线性放大 AI 容量。同一账号组被多个 Task 使用时，AccountPool global lease 与账号跨 Task behavior budget 优先于本 Task share ceiling；LLM Provider 生成不占 AccountPool Telegram 物理并发。目标数量完成后，仅明确点名/native reply 可消费带受保护份额、借用/召回与不可缩 observed-demand 分母的 continuity capacity，且 quantity/coverage credit 均为 0。所有 Task 共享 account-level identity profile 与 account-peer persona projection；冲突面具阻断当前 account-peer，未归属平台 Action 的账号外发占用 Timeline/存在感但不结算任务，Observer gap 只 hold 受影响 account-peer authored/reaction 并等待 backfill。该补正设计完成、尚未实现。

本文提出一条新的 AI 活群 current 数据面。它不单独覆盖现行生产合同，不触发实现、迁移或发布；与顶层统一引擎冲突的内容以上述统一引擎 PRD 为准。

确认后的目标 supersede 范围：

- supersede AI 活群“Planner 一轮生成多条 Action，再按相对间隔发送”的正常路径；
- supersede `TaskGroupDailyMessageSlot/ContentMixCycle/Action` 共同承担 current 数量身份的路径；
- supersede Listener 60 秒轮询作为实时上下文回复主触发源；
- 保留 `hourly-random-pacing-and-ai-humanization-prd.md` 的确定性随机、不可压缩恢复和 typed remote fact 原则；
- 保留 `ai-group-generation-failure-churn-remediation-prd.md` 的 stable obligation、GenerationJob、Gateway unknown 与远端事实闭环原则，但将正常发送数据面收敛为本文的最小链路。

## 1. 问题定义

### 1.1 当前行为问题

当前可执行链路同时存在以下特征：

1. Listener 默认按 60 秒窗口采集上下文，导致新真人消息平均先等待约 30 秒；
2. Listener 一次采集多条消息后一次唤醒 Planner，容易把多个响应放进同一批次；
3. legacy 路径按 `now + 相对间隔` 创建一轮 Action，current 路径又使用日 slot 局部适配，两套时间真相并存；
4. 不同 Task 对同一真实群可独立随机，群级 aggregate capacity 默认不一定生效；
5. Dispatcher 可以批量领取全部 `scheduled_at <= now` 的 Action，历史逾期、停机积压或恢复错误会表现为集中发送；
6. Action 同时承载数量、排期、生成、发送与重试语义，任一阶段恢复都可能误改其他阶段的状态；
7. 当前代码尚无真正的 `AiGroupMessageObligation` current owner，内容 intent 仍绑定 legacy quantity slot；
8. 当前重复过滤主要发生在正文生成后的 `AiGroupMessageMemory` reservation，以及发送前 `ensure_group_ai_message_sendable` 复核；同账号已有 10 天 exact/similar/semantic/template、同群已有 5 分钟 exact 和局部最近消息质量门，但缺少一个从 Generation 前到 Gateway 前统一的群级跨账号 DuplicateDecision 合同；
9. 当前批量/提前生成会让正文在真正发送前被新真人消息越过，导致“语句本身不重复但已经接不上现场”的上下文重复和剧本感。

### 1.2 产品冲突

AI 活群同时存在三种不能互相替代的目标：

- **主动活群发言**：目标是按小时平滑随机，不能集中，也不能在停机后追赶；
- **上下文响应**：目标是新真人消息出现后尽快参与，不能为了均匀分布而延迟几十分钟。
- **账号活跃与自然参与**：每日总消息数达标后，仍必须让任务范围内每个账号分别取得真实活跃事实；不同账号不能只换头像发同一种模板，也不能为凑覆盖打断真人对话。

把这些目标继续压成一个“轮次消息数”，只能在“回复快”“分布平滑”“全账号覆盖”和“自然参与”之间反复折中。本设计把主动与响应拆成两个业务泳道，并把数量目标、账号覆盖目标和自然参与质量分别建模；它们共享群级时间线、账号时间线和远端事实闭环，但不再互相冒充完成。

## 2. 产品目标与非目标

### 2.1 必须达成

1. 一个 current 数量单位只有一个稳定 `AiGroupSendObligation`，Action 不是数量真相源；
2. 一个 Planner decision 只处理单消息义务，不再创建会自行连锁的多账号会话轮次；
3. 主动消息在每个小时的配额内采用确定性分层随机，不能独立均匀抽样后偶然成团；
4. 上下文响应不等待 60 秒轮询，符合条件时走事件触发的低延迟路径；
5. 上下文响应必须消耗既有小时/每日数量单位，不形成隐藏超发；
6. 同一真实群的所有 AI 活群 Task 共用群级排期，同一账号跨 Task 共用账号时间线；
7. 停机、积压、Provider 故障、账号恢复和 Dispatcher 恢复均不得把 overdue 批量改成 `now`；
8. Action 只在内容已通过质量闸、账号和目标已冻结、发送时间已确定后创建；
9. Gateway call-issued 后只允许远端对账，禁止自动 replacement 或重发；
10. 只有 typed Telegram remote fact 才确认消息义务、群日目标和账号 coverage。
11. 每个任务日同时维护 `QuantityTargetSet` 与 `AccountCoverageTargetSet`；总消息数达标但仍有账号未覆盖时，任务不得显示“活群完成”；
12. 任务选择范围内每个账号至少需要一条由该账号真实发送、带非空远端消息 ID 的 confirmed fact；其他账号不能代替它完成覆盖；
13. 重复过滤必须覆盖生成前提示约束、候选生成后质量门、并发 reservation 和 Gateway 前最新窗口复核；Gateway unknown 持续占用重复保护；
14. 上下文生成必须使用同群、最新、带 watermark 的紧凑快照；不得把全天原文塞入 Prompt，也不得引用其他群原文；
15. 自然参与按上下文贴合、时机、账号风格区分、重复率和真人盲评验收，不能用消息数或“画像字段非空”代替。

### 2.2 非目标

- 不承诺每条真人消息都必须有 AI 回复；只有通过参与决策并取得响应容量的上下文 turn 才生成响应；
- 不用提高 worker 数量、缩短轮询、增加签到/emoji 或放松质量门掩盖根因；
- 不要求把主动消息和上下文响应混成一条完全均匀的时间序列；响应允许靠近真人事件，但必须受群级最小间隔和小时容量约束；
- 不允许把 legacy `GroupContextMessage`、sender name、正文相似或 Action.result 直接当 reply authority；真人原生回复必须来自 canonical external-human event 与版本化 `ConversationReplyAuthorityDecision`；
- 不把 Action success、容器健康、队列清空或页面计数当作 Telegram 完成。
- 不承诺任何模型输出“100% 无法被识别为 AI”；产品目标是显著降低模板感和失真的 AI 腔，不允许通过冒充具体真人、编造个人经历、交易、地点或关系来实现。

## 3. 核心产品决策

### 3.1 单消息模型

废弃“一个 context/cycle 规划 N 个账号连续对话”的正常数据面。新模型固定为：

```text
一个 Obligation
  -> 一个发送意图
  -> 同一时间最多一个有效 GenerationJob
  -> 同一时间最多一个未进 Gateway 的 Action
  -> 零或一个 quantity-confirming remote fact
```

某条 AI 消息成功后可以成为后续主动消息的历史上下文，但不得同步创建下一条 AI 消息。所有后续消息必须等待自己的小时 slot 或新的真人上下文事件。

### 3.2 双泳道

| 泳道 | 触发源 | 时间目标 | 数量来源 | 内容生成 |
|---|---|---|---|---|
| `proactive` 主动活群 | 固定主动义务，或未使用 response reserve 到 cutoff 后释放 | 小时内确定性分层随机 | 群日目标与 coverage | 到点前 JIT 生成 |
| `context_response` 上下文响应 | 新真人 admitted context turn | 按真人 tempo class 的自然发送窗 | 消费任务日计划预留的 response-bound 数量单位 | 事件后立即生成 |

两个泳道最终都进入统一引擎的 `TimelineArbiter`，由公共 `account + peer + conversation + task_obligation` reservation domain 协调，不存在绕过群/账号时间线的“快速直发”。

### 3.3 三维完成合同

产品完成状态由三组独立证据共同决定；其中数量和覆盖是每个任务日的运行闭环，自然参与质量是 route/canary 周期的产品验收闭环：

| 维度 | 真相源 | 完成条件 |
|---|---|---|
| 数量完成 | `QuantityTargetSet -> obligation -> typed remote fact` | 每个 canonical group 都满足 `confirmed_quantity >= effective_group_daily_target` |
| 全账号活跃 | `AccountCoverageTargetSet -> account-bound obligation -> typed remote fact` | 每个 Task、每个 canonical group、每个任务日的全部选择账号至少一条 normal contextual confirmed fact |
| 互动服务 | `observed -> eligible/ineligible/deferred wait -> participation candidate -> admitted/coalesced -> served/validly superseded/missed` | observation integrity 完整，admitted resolution≥95%，仍需响应的容量兑现率≥95%（或无对应分母）；同 peer turn 多重响应为 0 |
| 自然参与质量 | context、dedupe、persona、timing 与盲评指标 | 批准周期内质量门和灰度指标全部通过，无跨群、过时上下文或明显重复 |

一个远端成功可以同时结算一个数量单位和该发送账号在当前群的覆盖单位，但四个维度必须分别投影。`day_operation_status=completed` 要求数量、逐群逐账号覆盖、互动服务和 Gateway unknown 同时闭合；多日质量样本尚不足时只能显示 `quality_evidence_status=insufficient_sample`，不能显示产品目标已验收。

### 3.4 数量守恒

上下文响应不得额外增加群日目标。每个 `task + canonical group + task day` 在计划冻结时先建立群日总池，再把柔性响应槽稳定分散到预测有人活动的不同小时：

```text
group_response_flexible_total
  = min(effective_group_daily_target,
        ceil(effective_group_daily_target * frozen_response_flexible_ratio))

effective_group_daily_target
  = group_proactive_fixed_total + group_response_flexible_total
```

`frozen_response_flexible_ratio` 默认 40%。只有紧邻本 task day 之前连续 7 个 applicable 日均满足 observer coverage/gap 完整，才按 unified `cold_group_adaptive_policy_v1` 的冻结公式由 `avg_daily_human_turns<5` 得到 15%～20%；受管账号/Bot、观察不完整日和不可重放分类不进入冷群判定，证据不足保持 40% 并标 `cold_group_classification_unproven`。该比例只在 task-day plan 建立时计算一次，只改变 proactive/response 数量分池，不改变 turn candidate 比例、逐账号覆盖或 95% interaction service 目标。`PacingPlanner` 按真人活动预测在已有群日 strata 上做确定性加权系统抽样，每小时 `response_reserved_count <= hourly_quota`，其余为 proactive fixed；没有小时分布样本时可以使用 `natural_full_day_v1` active-window profile 并标记 pacing forecast confidence low，但这不能替代互动容量证明。达到统一引擎的 7 个完整 active 日/至少 50 个真人 turn replay 门槛后，必须证明 peer still-needed-owner demand P95 的 95% 不大于全部合法 response slots；planned point 前可证明由真人解决的 turn 单列 forecast superseded，不能用预计 wait 随意扣分母。此前只能使用显式低置信度 `cold_start_interaction_forecast_v1` 做预注册单 Task/群限量 canary，状态保持 `interaction_capacity_forecast_unproven`，不能宣称 capacity ready 或扩大。已证明但 valid slots 不足则为 `interaction_plan_unachievable`。启动预览显示冷群证据窗口/比例、群日总池、每小时固定主动/响应预留、replayed unique-owner/still-needed demand、forecast superseded evidence、required/valid response slots 和容量缺口。

真人 turn 必须先取得统一引擎 `ConversationTurnClaim(tenant + canonical group + turn family)` 的唯一 Task owner；owner 后先按 tempo 冻结 turn natural window，再由响应路由 claim winner Task 同 task day、同 account binding、尚未开始 Generation/Action/Gateway，且 `capacity_window` 与 turn natural/freshness window 有交集的 response-reserved obligation。每个 tentative supply 只占一个按 account/peer Timeline policy 派生的出站资源量子，不锁住整个小时 stratum，也不把 Provider 生成 P95 当账号占用。候选可来自当前或相邻小时 stratum，避免整点边界把本来合法的响应判空；但不得跨 task-day deadline、不得把不相交的未来 slot 改成 now。对每个候选先按冻结 permit 队列与完整准备链 P95 算 `preparation_feasible_call_not_before_at`；绑定事务只能从 `turn natural window ∩ slot movable window ∩ account/peer Timeline legal free interval ∩ [preparation_feasible_call_not_before_at, freshness deadline]` 内稳定抽取 `planned_call_at`。出站量子可完整容纳，且 `InteractionServiceBinding + Task-day binding/call budget conditional CAS + ProviderCapacityReservation` 能同事务提交时，才 CAS 移动量子并把 tentative supply 转 effective service；预测已赶不上 planned call 时不创建 active binding，也不消费调用预算。日计划与 opportunity 在此之前都不得伪造 planned call。quantity identity、ordinal、coverage account 和原 slot audit 保持不变，不新建第二个数量义务。winner 无交集容量时显式 missed，禁止由同群第二个 Task 补答。

未使用预留在 `response_release_cutoff` 到达后转为 proactive preparation，并在原 stratum 剩余窗口内用持久 seed 计算新 due revision；释放不是 overdue catch-up。cutoff 统一按 `window_end - max(policy_floor_release_lead_time=15 minutes, complete proactive remaining-path P95 + attention quiet-window P95 + execution safety margin)` 冻结，15 分钟是版本化 policy floor，不是 worker 常数。只有 cutoff 后仍有完整 quiet/JIT/Gateway 可达区间的 stratum 才能冻结 response-flexible；不合法时稳定换到其他 stratum，合法 strata 总量不足则计划前 `interaction_plan_unachievable`，不能缩短提前量、集中到边界或明知无法回收还宣称数量和互动都可完成。历史不足时 attention capacity 必须 low-confidence/canary unproven，不能按全天都 quiet 计算。日内对话突增先消费既有 reserve，再由统一 TimelineArbiter 安全 reflow 尚未物化的 supply；仍无容量记录 `interaction_capacity_missed`，保留在 admitted 分母，不得超量或挪用其他任务日。

## 4. 目标架构

```text
Task / Policy
      |
      v
TaskDayLedger -> QuantityTargetSet + AccountCoverageTargetSet
      |                       |
      v                       v
HourlyQuotaPlan ------> proactive-fixed + response-reserved account-bound obligations
      |                       |
      |                       +-----------> deterministic hourly slots
      |
Telegram update stream
      |
      v
GroupContextEvent -> ConversationProjector -> ContextTurn/Snapshot -> ResponseRouter
                                      |
                                      +-- bind one response-reserved obligation
                                      v
                              context_response obligation

proactive/context_response obligation
      |
      v
EngagementPacingSlot -> TimelineArbiter reservations
      |
      v
GenerationActivator -> GenerationJob -> quality gates -> dedupe reservation
      |
      v
immutable ready Action -> Dispatcher pre-call reservation recheck
      |
      v
Tx A prepare -> Tx B call-issued -> Telegram -> Tx C result
      |
      v
FulfillmentRemoteFact -> projector -> obligation/quantity/coverage/memory/read model
```

### 4.1 组件职责

| 组件 | 唯一职责 | 明确禁止 |
|---|---|---|
| `DayPlanBuilder` | 冻结任务日目标、小时配额、随机 seed，以及 proactive-fixed/response-reserved 两类义务 | 不生成正文、不创建 Action、不调用 Telegram |
| `CoveragePlanner` | 冻结账号覆盖分母并为每个账号建立 account-bound obligation | 不因 blocked 缩分母、不允许其他账号代偿 |
| `ContextIngestor` | 实时持久化 Telegram 上下文事件和 watermark | 不决定发送、不调用 LLM |
| `ContextTurnBuilder` | 把连续真人消息归并为稳定 turn | 不直接创建 Action |
| `ConversationProjector` | 增量维护同群滚动主题、未回答问题和版本化紧凑快照 | 不跨群读取原文、不在热路径重复总结全天历史 |
| `ResponseRouter` | 做参与决策，并把唯一 owner 原子绑定到一个兼容的 response-reserved 义务 | 不借用 proactive-fixed、不超发、不递归触发 AI 对话 |
| `GenerationActivator` | 在 generation_not_before 到期或响应事件发生时创建/唤醒唯一 Job | 不提前创建空正文 Action |
| `GenerationWorker` | 为一个义务生成一个候选并执行质量闸 | 不决定数量和发送时间 |
| `ContentDedupeGate` | 在生成前、候选后、reservation 与 Gateway 前阻断重复 | 不静默改写候选、不释放 unknown 占位 |
| `TimelineArbiter`（统一引擎公共组件） | 原子协调账号、群/会话和义务时间线，并校验 FloodWait/SlowMode/授权后的合法 release | 不改业务 due、不生成正文、不静默换正文或复用其他账号声线 |
| `Dispatcher` | 执行已准备好的不可变 Action | 不生成内容、不追赶 backlog、不重建义务 |
| `Gateway` | 按 committed request identity 调用 Telegram 一次 | 不猜测重试权限 |
| `FactProjector` | 用 typed remote fact 确认义务、target、coverage 和 read model | 不发消息、不把 Action success 当完成 |
| `Recovery` | 按持久状态收敛 lease、projection 和 unknown | 不把 future/overdue 统一改成 now |

## 5. 任务日目标和小时随机排期

### 5.1 配额输入

第一版继续保留最少的运营输入：

```text
account_group_ids[]                        # unified route 必填，1..N 个显式 AccountPool
account_group_binding_set_revision
concurrency_limit_per_group = 5
account_group_participation_mode = all_group_members_daily
daily_message_target_per_group
daily_target_jitter_bps                    # 0..3000，稳定均匀百分比抖动
daily_message_target_min / max
per_account_daily_min_confirmed = 1
hourly_activity_profile = natural_full_day_v1
hourly_activity_curve_snapshot[24]   # profile 在统一北京时间的只读“小时权重”，不是每小时条数
timezone = Asia/Shanghai             # unified current 系统托管只读
```

任务启动前必须展示只读预览：

```text
canonical_group
account_groups / binding_set_revision / membership_revisions
configured_group_member_union / policy_eligible / planning_admissible / planned_selected
runtime_sendable / coverage_blocked / coverage_confirmed
resilience_topology_decision / post_send_visibility_status / negative_outcome_level
raw_jittered_group_daily_target / coverage_floor_adjustment
required_group_account_coverage_units
effective_group_daily_target
computed_hourly_quota[24]
hourly_proactive_fixed / response_reserved / response_release_cutoff
expected_admitted_turns / interaction_capacity_shortfall
每小时预计最小/最大间隔
容量不足小时与预计 shortfall
```

每个 canonical target group 的 Task-day participation unit 先从全部绑定组 membership snapshot set 的规范化成员并集冻结 policy-eligible members 为 selected，并保留每个账号的 origin group 以执行 per-group bulkhead。数量抖动由统一引擎以 `task/lifecycle/local-date/target-group/account-group-membership-set/policy/purpose` seed 得到 `u∈[0,1)`，计算 `delta_bps=round_half_up((2u-1)*daily_target_jitter_bps)` 与 `raw_jittered_target=round_half_up(base_target*(10000+delta_bps)/10000)`；同一 revision 重放完全一致。这里的账号覆盖与 §6.4 真人 turn 参与率是两种概念，字段、seed 和指标不得复用。随后独立计算 `required_group_account_coverage_units = selected_account_count * per_account_daily_min_confirmed` 和 `effective_group_daily_target = max(raw_jittered_group_daily_target, required_group_account_coverage_units)`，并显式保存 coverage floor adjustment。既有兼容字段 `effective_daily_target` 只表示单个 `TaskGroupDailyTarget` 行的群日目标，不是整个 Task 多群总量。Task 汇总目标是全部目标群 `effective_group_daily_target` 之和。该公式只是需求量，不是突破自然节奏的发送许可：DayPlan 必须把 group-account units 匹配到该群全天合法 strata、账号/peer Timeline、response/proactive 类别、Provider/Gateway 窗口和 attention forecast 下的 quiet capacity；response 与 proactive 不得重复占槽。`legally_schedulable_units < effective_group_daily_target` 时计划即为 `coverage_plan_unachievable`，显示最少缺失窗口/容量，不压缩间隔、不集中追赶、不重抽数量也不缩分母。

### 5.2 全账号每日活跃覆盖

`AccountCoverageTargetSet` 与数量目标分开结算，但共同引用统一引擎的 participation plan：

1. configured group members、policy eligible、planning admissible、planned selected、runtime sendable、confirmed 六层集合分别持久化/投影；分母固定为全部 policy-eligible/planned selected。planning admissible 只证明当前计划路径，不允许因为 `can_send=false`、面具缺失、准入中、Session、proxy 或 Provider 阻断临时缩小并显示 100%；
2. 每个 selected account 每天、每个 canonical group 至少创建一个 account-bound coverage obligation；第二条及以上只属于额外数量，除非配置更高的每账号覆盖目标；
3. coverage obligation 从 intent 到 candidate、Action、Attempt 和 remote fact 始终绑定同一账号，禁止换号结算；`all_group_members_daily` 没有组外 substitute；
4. 一条带非空 `remote_message_id`、且 `content_quality_class=normal_contextual` 的 typed success fact可同时完成一个 quantity obligation 和该账号的 coverage obligation；Action success、unknown、本地生成成功、签到、静态 fallback 或纯占位短句都不能计 coverage；
5. 全部绑定组 membership revisions 在 Task day 开始由独立 membership snapshot set 冻结；任务日内新迁入账号默认下一任务日进入，紧急 disable/迁出只让该账号 runtime blocked，不删除当天既有分母或事实。Task 增删分组/改 per-group concurrency 建 binding-set successor，账号迁组只建 AccountPool membership successor，均不能原地改当日 plan；
6. selected account 按 selection debt、last selected day、最近发言、稳定 hash 跨全天 strata 交错；具体内容 slot 再考虑 persona/当前上下文兼容。不能为了覆盖让不适合当前话题的账号强行插话，未匹配账号保留到后续主动 slot并在 deadline 后形成显式 shortfall；
7. 额外数量 obligation 只能填充 coverage 已分散后的剩余 slot，不得把 coverage obligation 挤到最后一小时；
8. runtime sendable 为空只阻断当前 dependency partition；只要仍有健康 selected account/群义务，Task 为 `running_partial` 并继续规划。单面具/账号/Session/proxy/membership 故障不能写全 Task pause；只有全部当前到期 work 均不可服务或 Task 共享硬依赖阻断全部群时才 blocked；
9. 面具仅是互动内容 preparation 依赖：旧 active mask 版本仍合法时继续使用，无 active mask 才阻断该账号；禁止用通用模板代替。`mask_generation`、`turn_classification`、`interaction_response`、`interactive_proactive/reviewer` 各有子舱壁并共同服从 Provider 父配额，response/classification 保留保护份额；正文或面具 Provider lane 卡住不影响其他健康/受保护 lane，更不能影响点赞/浏览。

任务日只在以下条件同时成立时显示 `completed`：

```text
every canonical group quantity_confirmed >= effective_group_daily_target
AND every canonical group coverage_confirmed_accounts == required_coverage_accounts
AND interaction_observation_integrity == met
AND interaction_service_status == met
AND gateway_unknown_count == 0
```

其中 `interaction_service_status` 使用统一引擎口径：每个目标群分别具备 observer coverage≥99%、stream gap 收口、fresh watermark 与完整候选判定，且 admitted resolution 与 still-needed capacity service 均≥95%（或对应分母为 0）；`admitted_turns=0` 只有在全部目标群的 stream、watermark、subscription decision 和容量计划真实就绪时才允许通过，一个健康群不能掩盖另一个目标群断流。自然参与质量是独立发布/产品验收门，不因上述计数闭合自动判定通过。

### 5.3 小时整数配额

对所有正权重小时集合 `H` 和任务日目标 `N`：

1. 若 `N >= |H|`，每个正权重小时先分配 1 条，剩余数量再按权重使用最大余数法分配；
2. 若 `N < |H|`，使用持久 plan seed 做加权系统抽样，选择 `N` 个不同小时；禁止余数相同时总是优先最早小时；
3. 最大余数相同的 tie 使用 `SHA-256(plan_seed, hour)` 稳定排序，不使用小时索引排序；
4. 每个 canonical group 分别满足 `sum(hourly_quota) = effective_group_daily_target`；
5. 任务中途启动使用 `planning_anchor_at`，anchor 前小时配额为 0，不计算历史债务；
6. 当前 period 的已冻结小时和已存在 obligation 不因配置编辑重排。

### 5.4 小时内分层随机

某个小时分到 `q` 条时：

```text
available_window = [hour_start, hour_end)
stratum_width = available_window / q
slot_j_window = [hour_start + j*width, hour_start + (j+1)*width)
pacing_due_at = slot_start + stable_random(plan_seed, group, task, ordinal) * width
latest_send_at = slot_end
```

每个 stratum 只允许一个主动义务。随机点和 slot window 在计划冻结后不可改写。相同任务日、plan revision 和 obligation identity 重算必须得到相同结果。

### 5.5 同群跨 Task 聚合

同一 `tenant + canonical tg_peer_id + hour` 的唯一排期 owner 是统一 `PacingPlanner` 写入的 `EngagementPacingSlot + TimelineReservation`；AI adapter 只能提供 `AiGroupHourlyQuotaPlan` 输入和读取群级 projection，不得维护可写的 `GroupHourlySchedule`：

1. 汇总该群所有 active AI 活群 Task 的小时需求；
2. 先对总需求切分群级 strata；
3. 再按 Task 的 due/deadline 与持久公平 cursor 把群级 slot 分配给各 Task；
4. 新 Task 加入只使用剩余合法空隙，不移动已有 frozen slot；
5. 容量不足在 start/preflight 或当前小时 plan revision 中显式返回 deficit；
6. 该能力对新合同强制开启，不再是可关闭的 `source_capacity_v2_enabled` 快路径。

这样每个 Task 的随机时间不会在同一群中互相碰撞。

## 6. 上下文响应链路

### 6.1 实时监听

主路径改为 Telethon update stream：

```text
Telegram update
  -> single-owner ConversationSourceCursor
  -> dedupe(tenant, group, event_kind, remote_message_id, remote_revision)
  -> persist GroupContextEvent + durable outbox in one transaction
  -> advance monotonic group_context_watermark
```

60 秒 history poll 只作为断线、sequence gap、编辑/删除的 watermark reconcile，不再作为实时回复入口。cursor takeover 从最后 confirmed watermark 有界补洞；实时 listener 不健康时页面显示 `context_stream_degraded`，系统不得静默声称仍具备低延迟回复。公共事件字段、cursor 和 outbox 唯一性以统一引擎 §6.4/§8.1 为准。

context event、turn candidate terminal、GenerationJob ready 和 immutable Action ready 都必须与各自 `StageWakeOutbox` 同事务提交，提交后用低延迟信号唤醒下一 stage；现有 2 秒 worker tick 只作普通吞吐和恢复扫描，不能成为四段串行等待。重复通知只回读同一 owner，wake lag>5 秒显式告警。

冻结的首版链路 SLO：

- Telegram update 到 context event 持久化：P95 ≤ 3 秒；
- turn close 到 participation decision：P95 ≤ 1 秒；
- decision 到 accepted candidate：P95 ≤ 12 秒；
- Gateway call-issued 必须落在 `tempo_policy_v1` 对应的自然发送窗内，并早于 45/90/180 秒分场景 freshness deadline；
- 不再使用固定 2～8 秒作为全部响应抖动，也不用单一 event-to-call P95 强迫普通讨论秒回。

### 6.2 ContextTurn

`ContextTurn` 用于避免 Listener 一次收到多条真人消息就生成多条 AI 回复：

- 只聚合真人或允许的外部成员消息；平台自己发出的消息不创建新的 context turn；
- 同一群连续到达的消息按冻结 `BurstAssemblyPolicyRevision` 进入 2.5/5/8/12 秒候选窗，并由 quiet/max/deadline 规则产生唯一 `ContextTurn.closed_at`；本 adapter 不得另设固定 3 秒计时器；
- 每个 Task 对同一 turn 最多形成一个 participation candidate；同一 tenant、同 canonical group 的全部命中 Task 再由统一 `ConversationTurnClaim` 选出一个 response owner，最多创建一个响应义务；
- coalesce window、参与率、tempo class 和 response deadline 都属于版本化策略；
- 后到消息在 Provider request 尚未 call-issued 前可递增 turn revision，使旧 Job 安全失效；Provider 已 call-issued 后保留同 request 结果审计，不以普通 lease 到期重调同 request。
- 响应义务冻结 `context_turn_id + turn_revision + context_watermark`；Provider 前和 Gateway Tx A 都必须确认它仍是可发送的新鲜版本。若已出现更新 turn、范围失效或 deadline 已过，则 pre-Gateway 终结为 typed stale shortfall，不发送旧回复。
- 所有真人 turn 依次进入 `observed -> business_eligible/ineligible/deferred_wait -> participation_candidate/skipped -> admitted/peer_turn_coalesced -> served/validly_superseded/missed`；跨 Task owner claim 先于 capacity，账号和 Provider 判断发生在唯一 owner admitted 之后，不能缩小互动分母。planned call 前真人已解决/转题可记正确沉默；planned call 后因生成/容量延迟才失效必须按 blocker 记 missed。

### 6.3 实时上下文快照

Provider 不直接查询“最近 N 条然后整包塞入 Prompt”，而是消费不可变 `GroupConversationSnapshot`：

```text
scope: tenant_id + canonical_group_id
snapshot_revision + group_context_watermark + captured_at
active_turn_message_ids                  # 当前真人 turn 原文，完整保留
reply_parent_chain_ids                   # 当前 reply/addressee 关系链
recent_raw_tail_ids                      # 同群最近原始尾部，受版本化双预算约束
rolling_topic_summary_revision           # 更早上下文的异步结构化摘要
active_topics / unresolved_questions
recent_human_speaker_order
recent_platform_remote_fact_ids
group_community_style_profile_revision
selected_account_persona_version
message_style_assignment_revision
forbidden_repeat_fingerprints/clusters/openings
truncated + truncation_reason
```

上下文拼装顺序固定为：

1. **稳定前缀**：系统规则、安全合同、结构化输出 Schema、账号稳定 persona；不放时间戳和动态消息，便于 Provider prefix/KV cache；
2. **群级滚动状态**：由 event projector 异步维护主题、已确认事实、未回答问题和当前话轮，不在响应请求里额外调用一次摘要模型；
3. **当前动态尾部**：同群 active turn、reply parent 和最近真人/平台消息，按远端顺序排列；
4. **本次任务**：speech act、账号身份、上下文锚点、禁止重复集合和 deadline，永远放在最后。

推荐首版上下文双预算为“固定保留 active turn 全量、精确 reply/mention chain 和 unresolved anchors，再选择最新 10～20 条同话题相关消息，总输入最多 4,000 token”。默认 relevant tail 上限为 20；系统通知、机器人噪声、重复消息不占 10～20 条配额，必要引用即使早于最近 20 条仍必须保留。达到任一边界必须写 `truncated=true + reason`，不能静默截断；若 active turn 与必要 reply chain 自身已超过批准预算，则显式 `required_context_budget_exceeded` 并不生成，不能裁掉关键上下文继续猜。更早事实只能来自带 source message ids 的滚动摘要。A 群原文、摘要、stance 和近期 AI 正文不得进入 B 群 Prompt；跨群只能共享不可逆重复 fingerprint 和不含事实的稳定语气属性。

Snapshot 在 GenerationJob claim 时冻结。生成完成后若 watermark 已变化，系统不一定立即丢弃候选，而是比较变化是否影响 active topic、reply chain、addressee 或候选事实锚点；受影响则 `context_revision_stale`，同 obligation 只有在原 natural window、总调用预算和去重身份仍允许时才基于新 snapshot append regeneration revision，未受影响才允许继续。Gateway call-issued 前不超过 1 秒的 pre-call review window 再以 CAS 核对最新 turn/attention/watermark：native reply 的父消息删除或问题已被真人解决即 stale，但不能仅因后续消息条数多而取消仍有效的精确引用；semantic direct 在 anchor 后出现超过 5 条不相关真人消息或 topic revision 已切换时为 `context_stale_topic_advanced`。stale 不得降级成无关 proactive；窗口/预算不足直接 shortfall。CAS 后才出现的新事件属于 call-issued 后 interruption，只观察、不补发。

每个 turn revision 在进入上述快照前必须引用 unified `ContextModalityDecision`。普通文本与 caption 可作为原文证据；语音只接受与同一 remote media revision 绑定、来源和置信度达标的 approved transcript；图片/视频/文件只能使用类型化 media metadata，不能让模型臆测画面；纯贴纸、无 caption 媒体、未批准转写、forward origin 不清或语言识别低置信度的 turn 保留 observed 事实并显式 `ineligible_unsupported_context`。不得生成“哈哈/确实/有意思”等万能接话，也不得结算 normal contextual coverage。forward 内容不能冒充本群真人亲历，语言不兼容账号不能被 persona 强行包装成可回答。

### 6.4 参与决策与账号选择

参与分四步，不能把容量混进 eligibility 或 owner selection。turn 首事件发生前已冻结 route/lifecycle、群订阅、至少一个能观察该群且 watermark 健康的授权 Session、统一 response authority 和 InteractionCapacityPlan 的 Task 才进入 eligible subscription snapshot；观察 Session 只负责入口，可以与最终发送账号不同。合同未就绪的匹配 Task 记 `task_subscription_contract_blocked`，不得抢占 owner，且其互动服务状态不能完成。当前发送账号空闲、剩余 slot 或 Provider permit 不属于 subscription eligibility：

1. **business eligibility**：Task/route/ledger active、`context_response_enabled=true`、真人 turn 未被处理、上下文仍新鲜，且不是服务通知或纯噪声；“平台正在等待真人回应”“真人仍连续表达”“暂时没有新增信息”仍是 business-eligible，只进入下一步 `deferred_wait`，不能提前归为永久 ineligible；
2. **participation candidate**：对 eligible turn 使用冻结的 turn class、参与率和稳定 hash 得到 candidate/skipped；该决定在看到 response reserve、账号或 Provider 容量之前完成；
3. **peer turn owner**：多个 Task 同时为 candidate 时，按明确点名/自有 fact、relation hard、deadline slack、Task fairness 和稳定 hash 取得唯一 `ConversationTurnClaim`；loser 记 `peer_turn_coalesced`，winner 才是 admitted；
4. **capacity service**：为 admitted owner 查找同 task day、自然发送窗与 turn freshness window 相交的 response-reserved account-bound obligation，再检查 TimelineArbiter 和 Provider permit；不能用“current hour”硬切断整点两侧本可服务的窗口。成功为 served；无容量、来不及 deadline 或账号不兼容均为 typed missed，不能把 owner 转给另一个 Task。

canonical turn 的结构化规则无法确定语义时，只允许公共 `TurnIntentClassifier` 为该 turn revision 调一次共享 classification lane。其 admission deadline 不是占满完整 adaptive candidate window，而是 `classification_latest_safe_at = candidate_decision_cutoff_at - max_eligible_task_fanout_projection_p95 - claim_finalize_p95 - execution_safety_margin(post_classification)`；`candidate_decision_cutoff_at` 来自冻结 burst/turn close policy。fanout tail 按当前冻结 expected Task 数从批准 cardinality profile 取值，并覆盖全部候选投影与唯一 claim 持久化，超过 profile 上界时直接 capacity-unproven 而不调用模型。预计分类完成晚于 latest-safe、unknown 或低置信度都写 `turn_classification_uncertain` terminal decision，不套默认普通观点。重叠 Task 只引用同一分类结果和共享预算，AI adapter 不得另调一次。

真人仍在组织连续回答、平台刚提问等待真人或当前没有新增信息时，在 admission 前记 `deferred_wait + next_eligible_at`；它是当前 claim decision round 的 terminal decision，但不是整条 opportunity 的完成。当前 round 已有 admitted owner 时，后续 event/timer wake 只能结算 `peer_turn_coalesced_after_owner`；当前 round 无 owner 时，才允许在 freshness deadline 前 CAS 开启同一 claim 的下一 decision round、重冻 expected set 并重新判断，旧 round 永不追加迟到 candidate。超过 freshness deadline 仍不适合参与则终结为 `deferred_expired`，不进入 admitted 分母也不冒充 served。账号选择优先未完成当前群 coverage 且 persona 兼容者；不兼容账号留给后续 proactive slot，不为覆盖硬插话。

真人明确 @/点名受管账号，或原生回复我方 confirmed fact 时，`ContextTurnBuilder` 在 Task 路由前从 canonical event/fact 冻结 `ordered_required_account_hint_set + required_owner_task_hint_set + precedence_basis`。结构化 mention 按实体位置优先，再追加未重复的 native-reply fact 作者；多个 addressee 仍只允许一个平台响应。候选关闭后只在已返回 candidate 的 required owners 中按该顺序选 winner；缺失/blocked required owner 永久封为本 turn 非 owner，不能迟到补答。若一个合法 required candidate 都没有才记 `required_candidate_decision_missed`，其他 Task 不得代答；明确 addressee decision coverage 的运行目标仍是 100%，部分缺失也使 observation integrity 失败。取得合法 claim 后只有胜出的 required account 的 compatible reserve 可响应，无容量即 missed，不得由 non-required 账号冒名接话。只有没有明确 addressee 时，才按未完成 coverage、persona 适配和稳定 rotation 选择账号。

### 6.5 direct 与 Telegram reply

- response relation 必须在 `semantic_direct|native_reply_external_human|native_reply_owned_fact` 中显式选择；是否原生引用由 turn relation、明确问题/点名/引用链和群内真人 relation baseline 决定，不为拟人指标强制每条都 reply；
- `native_reply_external_human` 只接受 canonical `ConversationEvent/GroupContextEvent`：同 tenant、canonical group、thread/topic，一条精确 remote message identity；latest event revision 的 `author_class=external_human`，不是受管账号/bot/服务通知，current 未删除，且 source cursor/watermark 对该 revision 无未闭合 gap。原始上下文表行、昵称或正文不能单独授权；
- `native_reply_owned_fact` 继续只接受同 tenant、同 Task、同群、已有成功 Attempt、bound typed remote fact 和精确 remote message identity 的我方历史消息；Action success 或非空 remote ID 不能替代 fact/binding；
- owner admission 时 append `ConversationReplyAuthorityDecision(binding_revision,target_kind,source_event_or_fact_revision,peer/thread/topic,remote_message_id,author_class,watermark,decision_hash,state)`；同一 binding revision 最多一个 active target。Provider Prompt 与 immutable Action 只能读取该 decision，不能自行从正文猜 target；
- Gateway call-issued 前复核同一个 decision：external-human target 的 latest revision 仍存在/未删除/peer-thread 一致，owned target 的 fact/binding 仍有效，turn 没有被权威真人解决。普通后续消息数量不会机械取消精确 native reply；真正转题、目标删除或已回答才 stale；
- 一个 external-human message/turn 跨 Task 仍只有一个 `ConversationTurnClaim` winner；pre-Gateway stale 只在原 natural window、调用总预算与 dedupe identity 仍允许时 append successor decision，call-issued/unknown 后不换 target、不让另一个账号补答。

### 6.6 自然参与边界

每个群/时间带另有不可变 `GroupCommunityStyleProfileRevision(tenant + canonical group + time_band_v1 + revision)`。同群同时间带最近 30 天至少 50 条外部真人 normal text message 时，投影 grapheme length 的 P25/P50/P75/P90、问句/标点/emoji 比例、断句与词汇 register 摘要；受管账号、bot、服务通知、删除异常和本系统 AI 成稿不进入样本，也不把真人原文或固定短语复制进 Prompt。direct/native-reply 比例不进入 profile，因为 relation 是引用权限与语义合同，不能被“更像真人”的风格目标改写。样本不足时按 `group + planned-call time band + task day + style policy revision` 从批准的 `concise|balanced|discussion` 宽区间先验稳定选择并标 `cold_start`，不能所有群共用一套固定比例。

日计划为每个义务的当前账号绑定建立唯一 `MessageStyleReservation(obligation_id, account_binding_revision, profile_eligibility_cutoff_at, persona_revision_id, style_policy_revision, stable_distribution_rank, allowed_style_set, seed, supersedes_style_reservation_id, state)`，只冻结分布位置、profile cutoff、当前账号 persona 和允许集合，不决定具体长度或语气。唯一性为 `(obligation_id, account_binding_revision, style_policy_revision)` 一个且只有一个 active reservation。coverage-bound 义务始终不能换号；quantity-only 义务只能在当前 task-day 已冻结 selected 集内部、尚未生成 account-bound coverage credit 且仍满足逐账号覆盖/Timeline 的情况下重绑“本条发送账号”，不能替换 participation plan 的 selected 成员、转移他人 coverage 或让 standby 继承当前成员义务。合法重绑必须在账号 binding revision 与 TimelineReservation 切换的同一事务 append 新 persona reservation、supersede 旧 reservation/未进 Gateway assignment，旧 persona 不得转移给新账号。`proactive` 只能在 active/approved topic、content intent 与 planned call time band 冻结后建立 `MessageStyleAssignment`；`context_response` 只能在 `ConversationTurnClaim` owner、turn revision、addressee/relation、`GroupConversationSnapshot` active-anchor allowlist 与 planned call 全部冻结后建立。assignment 保存 `preparation_timing_revision`，以 `(style_reservation_id, content_intent_revision, turn_binding_revision, preparation_timing_revision)` 唯一，并保存 community profile、persona、目标长度、问句/标点/emoji 倾向、`register=concise_neutral|conversational|explanatory|cautious|playful_light` 与 context compatibility decision；每个 reservation 最多一个 active assignment。随后 `MessageBrief` 引用该 assignment 与同一 snapshot/turn binding，禁止 assignment 反向依赖尚未完成的 MessageBrief，避免构造循环。

`StyleCompatibilityPolicyRevision` 先按 turn class、addressee、语义锚点和情绪排除不合语境的表达，再在 community profile 内用稳定 rank 合并账号 persona。明确求助、纠错或负向反馈不能为了满足分布而使用围观/调侃 register；没有玩笑信号的 direct question 不能强行玩梗；没有兼容选择时为 `style_context_unallocatable`，义务留在原 response/proactive 类别等待或按 deadline shortfall，不能生成万能接话。active assignment 的首次创建必须与 timeline version 复核和 slot `preparing` 转移同事务完成；同一 preparation-timing revision 内 planned call/time band 不再 reflow。同义务同 binding/preparation 的质量修复/备用 variation 必须复用 assignment，不得借 retry 换风格绕过质量门。pre-Gateway turn stale/unbind，或 attention 在 preparing/ready 后抢占时，必须 append supersede 旧 assignment；attention 抢占还要 fence 旧 Job/candidate/Action，递增 preparation-timing revision，在原 window 内重新仲裁、生成、质量与去重后才可建立 successor。旧正文和 request identity 不复用；窗口不足即 shortfall。Gateway call-issued/unknown/confirmed 后不换风格。community profile 是群体边界，账号 persona 只在边界内提供稳定偏好；上下文锚点、事实与 turn intent 优先于风格。后续真人样本或 persona 修改只建 successor，不改既有 reservation/assignment；不得按 hourly ordinal 固定轮换风格、把某账号永久绑定一个开头，也不得从本系统刚生成的消息自我学习。

`group_style_compatibility_v1` 固定为：

| turn/content class | 允许 register | 额外约束 |
|---|---|---|
| `explicit_or_open_question` | `conversational / explanatory / cautious` | 必须回答 active anchor；`playful_light` 仅在当前 turn 有可回贴玩笑信号时加入 |
| `correction_or_negative_feedback` | `conversational / cautious` | 必须先承接纠错/负向点，不得转为围观、反问或自我辩解模板 |
| `substantive_view_or_evidence` | `conversational / explanatory / cautious` | 观点必须绑定 active anchor；有玩笑信号才可加入 `playful_light` |
| `ordinary_neutral` | `concise_neutral / conversational` | 无新增信息时允许 wait，不为发言量生成万能附和 |
| `micro_ack` | `concise_neutral` | 不反问、不展开新事实，仍受 5% participation 和 micro-ack 独立频率门 |
| `proactive_topic` | `concise_neutral / conversational / explanatory / cautious / playful_light` | 必须绑定 active/approved topic；不得携带旧 turn addressee/reply identity |

玩笑信号只来自当前 turn 可回贴的笑声/emoji/明确调侃结构并保存 evidence，不能由模型自报。compatibility 矩阵先于 community/persona 权重，policy successor 只影响新 assignment。

- 同 tenant、同 canonical group 的一个真人 turn 跨全部 Task 最多一个平台账号参与，禁止多个 Task 或账号围绕同一真人消息连续附和；
- 平台消息提出问题后进入 `awaiting_human_response`，真人回复、明确 addressee 变化或业务窗口结束前，其他平台账号不得立即自问自答；
- Persona 支持立体鲜活的人设背景（包含受众角色、生活习惯、话题偏好、句长与语气风格），用于让账号以独立真人身份自然交流并制造社群热度；同一账号同群前后风格保持一致，避免前后自相矛盾；
- 当前话题、立场和事实只来自本群最新 snapshot；近期 AI 成稿只用于重复过滤，不回灌为账号的“人生经历”；
- 不同账号的自然度不仅检查 profile 是否不同，还要检查实际成功正文的句长、开头、问句率、标点、emoji、speech act 和 semantic cluster 是否长期趋同；
- 允许“不说话”。自然参与决策为 wait 时，数量和 coverage 义务保留到合法未来 slot，不用签到、emoji 或模板短句强行制造在线感。
- proactive 在 Provider 前与 Gateway Tx A 必须读取当前 `ConversationAttentionState`；真人 turn/open response/`awaiting_human_response` 未结束时只可在原 slot window 内推迟 `release_not_before_at`，不能插入无关主动消息、改成 context response 或挪到下一日。放不下即 pacing shortfall；call-issued 后才出现真人事件只记负向 interruption observation。

本适配器严格复用统一引擎 `conversation_attention_v1`，不得另写群聊本地计时器。低优先级 proactive 的真人活动等待窗为同群/time-band 外部真人消息间隔 P90，并限制在 60～180 秒；有效间隔不足 30 个时使用 180 秒且标 low confidence。明确点名/问题的 admitted response 不受 proactive quiet-after 阻断。`human_turn_open`、`human_recent_activity`、`admitted_response_inflight` 与 `awaiting_human_response` 可重叠，quiet-after 取 active blocker expiry 最大值。只有经质量门标记 `expects_human_reply=true` 且取得 typed confirmed fact 的平台消息才打开 awaiting，真人回应/带 evidence 转题可提前关闭；每个 expiry 都以 revision + `StageWakeOutbox` 收口，历史 backfill、AI/机器人消息和旧 wake 不得续期，因此既不会抢话，也不会永久锁住 proactive。attention 在 slot 尚未 preparing 时可合法 reflow；已 preparing/ready 但 pre-call 时必须原子 fence 当前 materialization、supersede style 并递增 preparation-timing revision 后在原 window 重新走完整链，不能直接延后旧候选；call-issued 后只记 observation。

每条我方 confirmed normal contextual fact 都进入不产生发送义务的互动观察。真人原生 reply 的远端 parent relation 与 typed fact 可核验时形成权威 observation，不受 10 分钟语义推断窗限制；只有无原生 relation 时，才在同一未转题 turn 的 10 分钟内寻找明确语义锚点。公共 `HumanEngagementAttributionClaim` 对同一真人 event revision 按 `native parent > structured mention/quoted anchor > unique semantic continuation` 选最多一个正向 winner；非原生候选必须唯一超过置信阈值和 runner-up margin，否则为 `ambiguous_unattributed`。已有 native winner 不再计 inferred，同一真人 event 不能同时给多条 AI 消息记成功。低置信度不计互动成功；明确质疑机器人感、删除/撤回及真人已回答后平台仍抢答作为负向 outcome，负向指标按 human event 去重。该反馈只进入高互动基线比较，不结算 quantity、coverage 或 Telegram reply relation。

### 6.7 真人节奏与分场景 freshness

事件识别、turn close、参与决策和内容生成保持低延迟；实际发送由统一引擎 `ConversationTempoProfile` 决定。时间带直接复用统一 `time_band_v1`，只有外部真人消息进入间隔样本；样本达到 30 个真人间隔后，按同群、时间带、turn class 的真人 P25/P50/P75/P90 做稳定随机抽样；冷启动使用：明确问题/点名 8～35 秒且 deadline 45 秒，活跃话轮 12～60 秒且 deadline 90 秒，普通讨论 45～180 秒且 deadline 180 秒。

owner 冻结后以 turn observed time 和 profile 先持久化 natural window；Provider admission 先按冻结 permit 队列与完整 response preparation P95 计算 `estimated_candidate_ready_at` 和 `preparation_feasible_call_not_before_at = estimated_candidate_ready_at + gateway_prepare_p95 + execution_safety_margin(pre_provider)`。`planned_call_at` 只能在 natural window、slot capacity window、账号/群 Timeline legal interval 与 `[preparation_feasible_call_not_before_at, freshness deadline]` 的交集中用 stable seed 冻结一次，并与 binding、任务日总预算 CAS、Provider reservation 同事务提交。Provider 早完成等待 planned call；只有真实耗时超过冻结 P95 的未预测 tail，才可在原 binding 交集内晚发并记 `planned_point_late_unexpected_tail`；admission 时已知会迟到则零 active binding/零调用，不能伪装成 planned late。越过交集/natural/freshness end 不发送，也不能重抽更晚时点。profile 不得突破账号/群最小间隔。等待期间若真人已回答、转题或 turn revision 更新，Gateway 前终止旧候选；不得为了满足数量把 stale response 改成 proactive 立即发送。固定 2～8 秒只允许作为历史策略观测值，不再是 current 合同。

## 7. 内容生成链路

### 7.1 JIT 时机

主动义务：

```text
generation_not_before_at = pacing_due_at - generation_lead
```

`generation_lead` 直接取统一引擎冻结 `ExecutionTimingProfileRevision.materialization_through_gateway_p95(pre_materialization) + execution_safety_margin(pre_materialization)`；margin 固定为 `max(5 秒, ceil(complete remaining path P95(pre_materialization) * 20%))`，并保存 profile revision 与 path-start stage。缺少批准 profile 时显式 `execution_timing_profile_unproven` 且 unified route 不激活，不得用固定 30 分钟 lookahead 或 worker 本地估算为全天 Action 提前冻结过期上下文。只有实测完整生成、质量/去重和 Gateway prepare P95 允许时，`generation_lead` 才可能落在 5～10 秒；不能把 5～10 秒写成不顾链路耗时的固定值。

上下文 response-reserved 义务绑定 admitted turn 后立即进入 Generation，但仍必须在对应 freshness deadline 前取得 accepted candidate。

### 7.2 单义务单候选流水线

```text
Acquire context facts
  -> deterministic MessageBrief
  -> one-message LLM generation
  -> structured parse
  -> deterministic scope/safety/dedupe/voice checks
  -> optional semantic reviewer by explicit policy
  -> accepted ContentVariation
  -> ready Action
```

LLM 只负责自然语言表达，不负责：

- 计算配额；
- 选择发送时间；
- 判断是否允许超发；
- 判断 Gateway unknown 是否可重试；
- 修改账号、群、义务或 reply identity。

`MessageBrief` 必须是持久化、版本化的确定性输入，不是临时拼出的 Prompt 字符串，至少冻结：

```text
obligation/content intent/style reservation + active assignment identity
group snapshot revision + context watermark
turn/reply/addressee identity when applicable
allowed_context_anchor_message_ids
active topic/unresolved-question ids
approved task facts with source ids
forbidden repeat fingerprints/clusters/openings
candidate deadline and quality policy revision
```

`context_response` 至少绑定一个当前 active turn/reply chain 的 `allowed_context_anchor_message_id`；`proactive` 至少绑定一个仍有效的 active topic 或批准 task topic，不能从滚动摘要的自由文本临场创造事实。滚动摘要中的每个可用于生成的事实项必须携带原始 source message ids；没有来源 identity 的摘要只能帮助检索原文，不能进入 `approved task facts`。

Provider 的结构化结果除正文外必须返回 `response_mode + used_context_anchor_message_ids + used_task_fact_ids + speech_act`。这些 ID 只是待验证声明：确定性 claim extractor 要从最终正文重新提取姓名、数字、地点、关系、承诺和第一人称经历，并逐项映射到允许的 context/task fact；semantic gate 再判断正文是否真正回应 active question/statement，而不是只复述一个关键词。任一未知 ID、跨群 anchor、unsupported claim、万能接话或违规外链导流都拒绝，不能自动补 ID 形成假通过。

`GroupCandidateQualityDecision` 还必须对最终 accepted 正文冻结 `expects_human_reply=true|false + basis_evidence + evaluator_revision`。只有正文在当前语境中确实提出需要真人回答的具体问题/澄清请求，且 deterministic 结构门与 semantic gate 都通过时才可为 true；问号、反问、语气词或 Provider 自报不能单独置 true。同一 accepted content hash 的该值不可变，并由 Action/fact identity 回贴，`ConversationAttentionState` 只消费这份质量决定，不重新猜正文。

### 7.3 重复过滤的四道门

重复不是发送完成后才统计，而是在四个时点逐级阻断：

| 时点 | 输入范围 | 动作 | 失败结果 |
|---|---|---|---|
| A. Generation 前 | 同群 remote-confirmed、Gateway unknown、active candidate reservation；同账号历史 | 构造 forbidden exact fingerprint、semantic cluster、template shell、近期句首、词汇和 2-gram 集合，写入 MessageBrief | 不消耗 Provider；若 speech act/topic 本身与近期重复则重新分配 intent 或等待 |
| B. Candidate 生成后 | 候选正文 + 当前 ContextSnapshot + 同一重复窗口 | 确定性规范化并检查 exact、高相似、semantic、模板壳、重复开头、连续问句、词汇频率和上下文锚点 | `duplicate_candidate_rejected`；同 obligation 使用下一 variation，不创建 Action |
| C. Candidate 接受事务 | 当前候选 + 其他 worker 同时生成的 active reservation | 在群级 dedupe timeline 锁内原子查询并写 `ContentDedupeReservation`，避免两个并发 Job 同时通过 | CAS loser 返回 typed duplicate，不能两条都进入 ready |
| D. Gateway Tx A | 最新 confirmed/unknown/reserved 窗口 + candidate reservation + 最新 context revision | 复核候选从 accepted 到真正发送之间是否已被其他 Action 抢先发送或已过时 | pre-call 标记 `stale_duplicate_before_gateway`，同 obligation 在 deadline 内重新生成，否则 shortfall |

推荐 current scope：

- 同群跨 Task、跨账号的 `normal_contextual` 规范化 exact text：滚动 30 天硬拒绝；
- 同账号 exact、高相似、semantic cluster 和 template shell：滚动 10 天硬拒绝，保留现有合同；
- 同群最近 100 条 platform remote-confirmed、unknown-hold 和 active reservation：检查 semantic cluster、词汇/normalized term 和句式分布；
- 同群最近 20 条：非 stop 2-gram、四字开头和连续 speech act 高频门；
- 同一个 context turn：最多一个 response obligation，天然消除多人对同一 turn 的同义连发。

版本化 `micro_ack/reaction` 不得借短文本规避 30 天正文去重：它使用独立的近 20 条频率门，不计 `normal_contextual` 质量目标，也不能关闭新合同的数量或账号覆盖义务。首版不启用 micro-ack fallback。

窗口以 Telegram 远端顺序/确认时间为主要顺序，active 与 unknown 作为最坏占用插入当前窗口。`Gateway unknown` 的正文 fingerprint、semantic cluster 和词汇占用不能过 TTL 自动释放；只有权威 safely-not-executed 或远端对账结果才能释放。confirmed 后由 FactProjector 把 reservation 转成 `AiGroupMessageMemory=confirmed`。失败候选可以释放自己的 candidate reservation，但不能释放其他 Action 或 unknown 的占用。

重复判断不得把完整跨群历史原文加载进 Prompt。跨群防重复只允许查询不可逆 fingerprint/embedding 并返回“冲突/不冲突 + reference identity”；同群为解释质量失败可保留权限受控的 reference message id，但普通日志不写正文。

### 7.4 主动与响应的质量策略

- 主动消息只可在统一 materialization horizon 内 JIT 提前生成，不能恢复整小时/整日批量成稿；它允许使用完整两阶段生成和 reviewer；
- 上下文响应优先采用一次结构化生成加确定性硬闸，以满足时延；如启用第二模型 reviewer，必须计入当前 turn class 的 45/90/180 秒 freshness deadline；
- 质量失败不发送 Stage 1、不改成固定签到、不改成 emoji；
- 响应 deadline 内允许的生成尝试预算必须显式版本化；v1 固定为主生成一次、质量修复或批准备用 route 一次，总 Provider 调用不超过 2，已经是实现合同；
- context response 在预算耗尽、质量/去重/Provider deadline 失败且 Telegram 尚无 call-issued 时，当前 admitted opportunity 按真实 blocker 结算 missed，并原子 fence 当前 preparation、supersede turn/style binding、保留全部调用数与成本，再把同一 response-flexible obligation 以 append-only unbind revision 归还原池；cutoff 前可等待新的真人 turn，cutoff 后只按既定 release policy 转 proactive。不能在同一 turn 上突破 2 次调用，也不能把互动 miss 改写成 quantity terminal；只有原 slot/window 最终放不下才形成 quantity shortfall；
- 所有 retry 复用同一 obligation，variation sequence 单调递增，不创建新的数量单位。

## 8. Action、发送仲裁与 Gateway

### 8.1 Action 的唯一语义

新合同下 Action 只表示：

> 一条已经通过生成与质量闸、绑定唯一 obligation/candidate/account/group，并在指定时间后允许尝试一次 Telegram mutation 的不可变命令。

Action 创建时必须非空冻结：

```text
obligation_id / obligation_version
content_intent_id / variation_id / candidate_hash
account_id / account_binding_version
canonical_group_peer_id / target_revision
context_snapshot_id / snapshot_revision
context_turn_id / turn_revision / context_watermark nullable
dedupe_reservation_id / dedupe_policy_version
pacing_due_at / response_due_at nullable
group_release_not_before_at
account_release_not_before_at
effective_claim_at
latest_send_at
route_epoch / task_lifecycle_epoch
conversation_reply_authority_decision_id / decision_hash
remote_mutation_key_hash / gateway_request_hash
```

禁止创建空正文 Action 等待 Dispatcher 生成；禁止 Dispatcher 原地清正文、换账号、改 reply target 或改业务 due。

### 8.2 统一发送仲裁

领取顺序：

```text
effective_claim_at
  = max(
      lane_due_at,
      group_release_not_before_at,
      account_release_not_before_at,
      Telegram SlowMode/FloodWait not-before
    )
```

必须同时满足 `effective_claim_at <= database_now < latest_send_at`。

同一事务只使用统一引擎 §13.2 的公共锁序；AI adapter 不再定义“group-first/account-first”的第二套顺序：

```text
ConversationTurnClaim（响应时）
-> AiGroupSendObligation / projection
-> EngagementPacingSlot
-> TimelineReservation(account -> peer -> conversation -> task_obligation)
-> GenerationJob 或 immutable Action
-> ExecutionAttempt / fact projection
```

同一群同一时刻只允许一个 `gateway_prepared/call_issued`；不同群可由多个 Dispatcher 并行。账号时间线跨 Task 生效，不能只看当前 Action 批次。

跨四类任务的顺序由统一引擎冻结为 `同 request reconcile -> 真人 response -> due interactive content -> like -> view`，同级再按 deadline slack、原 due 和持久公平键。AI adapter 不再维护自己的第二套优先级。

response 优先消费自身 reserve。只有未进入 Generation、移动后仍在原窗口内且 CAS 成功的低优先级 reservation 可以 reflow；protected slack、ready 和 call-issued 工作不可抢占。响应仍不能绕过群/账号最小间隔，超过 freshness deadline 时进入 `interaction_capacity_missed|timeline_deadline_missed`，不得挤占未来任务日或超发。

### 8.3 Gateway 四阶段

沿用严格边界：

1. **Tx A / prepare**：校验 obligation、candidate、账号、群、route、reply、时间窗；响应泳道额外复核 context turn revision/watermark 新鲜度；通过后创建 Attempt 并冻结 request；
2. **Tx B / call-issued**：再次校验数据库时间和版本，提交不可变 call-issued journal；
3. **事务外调用 Telegram**：只允许 committed request identity 调用一次；
4. **Tx C / result**：持久化明确失败、unknown 或带 remote message ID 的 typed result。

Tx B 提交后进程崩溃视为 unknown，不得因为“可能还没真正发 socket”而重发。只有同 request 的可信 `safely_not_executed` 事实允许原 obligation 再物化。

## 9. 状态机和数据真相源

### 9.1 Obligation 状态

```text
future
  -> generation_pending
  -> generating
  -> candidate_ready
  -> action_bound
  -> gateway_prepared
  -> gateway_unknown_hold
  -> confirmed
```

旁路状态：

```text
future(response_reserved) -> turn_bound -> generation_pending
future(response_reserved) -> released_to_proactive -> generation_pending
* -> waiting_dependency               # 有明确 wake 条件
pre-call -> open                       # 明确 safely_not_executed
pre-call -> terminal_shortfall         # 超过 latest_send/deadline
call-issued -> remote_reconcile_only   # unknown，只对账不重发
future/pre-call -> retired             # target 下调、stop 或 scope 退出
```

`turn_bound` 只追加 turn/relation binding revision，不替换另一个 proactive-fixed 义务，也不改变数量、ordinal、账号、群、原始 stratum/window 或 coverage 身份；`released_to_proactive` 仅能由同一 response-flexible 义务在自己的 cutoff 后进入。二者互斥，均不得把 due 改成当前时间。

`waiting_dependency` 必须同时保存 `blocker_code + blocker_basis_hash + wake_key/version`，不得留在 open 热循环固定重试。

### 9.2 最小数据对象

| 对象 | 角色 |
|---|---|
| `TaskAccountGroupBindingSetRevision/AccountGroupMembershipSnapshotSet` | 统一引擎冻结 Task 的 1..N 个显式 AccountPool、各组成员 revision/set hash、规范化并集与 per-group concurrency；AI adapter 不解析 implicit all |
| `TaskFulfillmentPlanRevision/TaskParticipationUnitPlan` | 联合冻结每群每日 selected、运行时之外的完成分母、数量抖动、coverage floor、全天计划 seed/hash |
| `TaskParticipantRuntimeProjection` | 按 mask/session/proxy/membership/provider/listener 依赖域显示 selected account 的 sendable/blocked；不改 participation plan |
| `ExecutionResiliencePolicyRevision/ExecutionBulkheadLease/RemoteInvocationFence/FailureDomainCircuitState/HealthProbeAttempt` | 统一 remote hard timeout、本地 Worker/stage/fair-share lease、account/group/proxy/Provider 远端在途 fence、closed/open/half-open 与低优先级探活；本 adapter 只消费 admission 结果。timeout 只立即释放本地 lease，未确认终止的 transport 继续计远端 hard in-flight |
| `TaskDayLedger` | 任务日、planning anchor、deadline 和 route snapshot |
| `QuantityTargetSet/AccountCoverageTargetSet` | 独立保存消息数量目标与逐账号覆盖分母、状态和 shortfall |
| `AiGroupHourlyQuotaPlan` | AI adapter 的 24 小时整数配额、权重、plan seed/hash、revision；不是最终发送时间 owner |
| `EngagementPacingSlot/TimelineReservation` | 统一引擎拥有的群级 aggregate strata、Task slot assignment 与账号/peer/conversation 时间线；AI adapter 只读 |
| `InteractionCapacityPlan` | 每小时 proactive fixed/response reserved、release cutoff、consumed/released/missed 和 policy revision |
| `AiGroupSendObligation` | current 数量、时间、slot class、account/group coverage、当前 service-binding pointer 和状态 owner；不把 turn/relation/call budget 原地写成数量 identity |
| `ConversationSourceCursor/GroupContextEvent/Outbox` | 单 owner 实时游标、幂等远端事件、gap/backfill 和 durable wake |
| `ContextTurn/TurnClassificationCapacityRevision/InteractionOpportunity/ConversationTurnClaim/GroupConversationSnapshot` | canonical turn 单次分类与 tenant/provider/surface 共享 permits/预算、eligible/blocked subscription、observed/eligible/ineligible/deferred/candidate/admitted/coalesced/served/validly-superseded/missed 漏斗、同群跨 Task 唯一 owner、滚动群状态和 watermark |
| `ConversationReplyAuthorityDecision` | 每个 response binding revision 冻结 `semantic_direct|native_reply_external_human|native_reply_owned_fact`、source event/fact revision、精确 peer/thread/topic/remote message、author class、watermark/gap 与 decision hash；前者只接受 canonical external-human current event，后者只接受本 Task bound typed fact；Provider/Action/Gateway 复用同一 decision |
| `InteractionServiceBinding` | admitted turn 与既有 response 数量义务的 append-only 绑定；唯一拥有 relation/turn/timing-feasible interval/planned-call/preparation revision、Provider admission、每 binding 调用上限与总预算 reservation，pre-Gateway 解绑不清零旧成本 |
| `ConversationTempoProfile` | 同群/时间带真人间隔 quantiles、样本量、tempo class 和 revision |
| `ConversationAttentionState/ForecastRevision` | current blocker set、有界 quiet-after 与版本化 wake；真人 P90 历史只用于计划 forecast，不能覆盖运行时事件状态 |
| `GroupCommunityStyleProfileRevision/MessageStyleReservation/MessageStyleAssignment` | 外部真人的群级非正文风格分布、cold-start 来源；日计划只冻结每义务当前 account-binding 的 profile cutoff/persona/distribution rank，主动 intent 或真实 turn binding 后才冻结长度/标点/emoji/register；quantity-only 合法换号必须 append 新 persona reservation，同 binding retry 不重抽，不保存或学习 AI 成稿 |
| `AiGroupContentIntent/Variation` | 不可变内容要求与候选版本 |
| `MessageBrief/GroupCandidateQualityDecision` | 冻结 context/task anchor allowlist、style/dedupe/deadline 输入，并记录 deterministic claim-to-source、semantic response-fit 与 accepted `expects_human_reply` 决策；Provider 自报 ID/问号不是通过证据 |
| `PreGatewayContextDecision` | call-issued 前 1 秒窗口核对 parent/turn/topic/attention/watermark，保存 unrelated-message count、stale reason 与 CAS revision；native reply 与 semantic direct 使用不同 stale 规则 |
| `GenerationJob` | Provider 请求、租约、预算和结果状态 |
| `ProviderCapacityReservation` | classification request 或 service-binding work identity、route/lane permit、预计开始/完成、下游 tail、planned-call/latest-safe、冻结总预算 revision、reserved/used/unknown/released-unissued calls/token/cost 和 admission 状态；与预算 conditional CAS 同事务，terminal 只释放从未发起部分 |
| `ContentDedupeReservation/AiGroupMessageMemory` | 候选并发占位、unknown hold、confirmed 历史和四道重复门依据 |
| `Action/ExecutionAttempt` | 一次不可变 Telegram 命令及调用尝试 |
| `FulfillmentRemoteFact` | Telegram 远端事实 |
| `HumanEngagementAttributionClaim/HumanEngagementObservation` | 真人 event-level 唯一正向归因、权威 reply、推断续聊与按 event 去重的负向结果；native 优先且不受 inference window 限制，不结算数量 |
| `ProjectionState` | target、coverage、memory 和 read model 投影进度 |

正常发送链不再以 `TaskGroupDailyMessageSlot`、`ContentMixCycleSlot` 或 Action 数量反推欠额。这些对象只在存量迁移和历史展示中保留。

## 10. Worker 与唤醒模型

目标 worker 拓扑：

| Worker | 输入 | 输出 |
|---|---|---|
| `ai-plan` | rollover/start/group binding/membership/policy revision | 只消费统一 participation/quantity plan，建立 ledger、AI hourly quota input、typed obligations；最终 slot/reservation 由统一 PacingPlanner/TimelineArbiter 写入 |
| `listener-stream` | Telegram updates | context events、turn/watermark wake |
| `conversation-projector` | ordered context events | rolling topic state、unresolved questions、snapshot revision |
| `ai-response-router` | closed/updated context turn | response-reserved turn-binding decision 或 typed no-send decision |
| `ai-generation` | due generation job / response wake | candidate 或 typed generation outcome |
| `dispatcher` | ready Action + effective claim time | Attempt/Gateway journal/result |
| `fact-projector` | typed remote fact | obligation、target、coverage、read model |
| `recovery` | expired lease、unknown、failed projection、time wake | 状态收敛，不发送新 mutation |

PostgreSQL 持久队列/partial index 是事实源；Redis/pubsub 只降低唤醒延迟。Redis 丢事件后数据库仍可恢复，数据库没有事件时 Redis 不得制造义务。

正常 waiting 不再依赖 Planner 每 30/120 秒扫描所有 Task。只允许以下持久 wake：

- `hour_slot_generation_due`；
- `group_context_turn_changed`；
- `account_group_binding_or_membership_changed`；
- `account_mask_session_proxy_or_membership_changed`；
- `provider_dependency_changed`；
- `policy_revision_changed`；
- `deadline_due`；
- `remote_fact_projection_due`。

## 11. 失败、恢复和不集中补发

| 场景 | 新合同处理 |
|---|---|
| Listener 断线 | 实时响应显式 degraded；poll reconcile 补 context event，不回放过期响应 |
| Planner/Generation 停机 | 保留原 pacing slot；恢复后仅处理仍在 `latest_send_at` 前的单元 |
| 主动 slot 已过 latest_send_at | terminal pacing shortfall，不移动到 now，不跨小时追赶 |
| 响应超过 45 秒 deadline | terminal response shortfall，不发送旧上下文 |
| Provider 暂不可用 | waiting_dependency，Provider revision/health wake；不创建空 Action |
| Provider request unknown | 先对账同 invocation；unknown 结果不可成为 candidate，也不得重放同 invocation。只有显式 generation budget 仍允许、deadline 尚未到且使用新的 invocation identity 时，才可尝试备用 route；unknown 成本仍计入预算，不创建新 obligation |
| Action prepared 未 call-issued | 可证明未调用 Telegram，可安全回到同 obligation 新 materialization |
| Gateway call-issued 无结果 | unknown/reconcile-only，禁止 replacement |
| Telegram 明确 safely-not-executed | 同 obligation 可在原 latest_send_at 前再物化 |
| 单账号面具不可用 | 只阻断该账号互动内容 preparation；仍 active 且版本匹配的旧 mask 继续合法使用，无 active mask 时禁止模板/签到替代。其他账号、群和被动任务继续 |
| 单账号 Session/成员资格不可用 | 只阻断该 account 或 account+peer；coverage-bound obligation 永不换号，等待恢复或形成 typed coverage shortfall；其他 selected account 继续，Task 聚合为 `running_partial` |
| 单 proxy route 不可用 | 只阻断使用该 proxy binding revision 的账号；其他 proxy/direct 账号与其他 Task 分组继续。仅当本 Task 全部当前工作都落在该失败 domain 才可 Task blocked |
| 账号不可用时的 quantity-only 绑定 | 仅未绑定真人 turn 的 quantity-only proactive obligation 可在 Generation 前、当前 task-day 已冻结 selected 集内部重绑本条发送账号并重新取得 TimelineReservation，同时 append 新 account-binding/persona 的 `MessageStyleReservation` 并 supersede 旧风格；不得使用 participation standby、替换 selected 或转移 coverage。`InteractionServiceBinding` 建立后账号失效只结算当前 admitted miss并在 pre-Gateway 归还数量义务，同 turn 不换号，candidate/Action/Gateway identity 已建立后同样不得换号 |
| 多个 overdue Action | Dispatcher 不直接排空；不在合法 slot/window 内的一律 shortfall |
| target 增加 | 仅在剩余未来窗口新增 plan revision/slot；容量不足显式 deficit |
| target 减少 | 只 retire 未生成、未 Action、未 Gateway 的未来单位；远端已开始者不可变 |

AI adapter 的动态准入必须逐次引用统一引擎同一断言，而不是只看数据库存在账号：

```text
RuntimeAdmissionEligible
  = InBoundAccountGroupSnapshot
  AND BoundAccountGroupOperational
  AND SessionValid
  AND ProxyRouteAndEgressVerified
  AND ProxyRouteAndEgressCircuitsClosed
  AND AccountNotQuarantined
  AND MemberOfTargetTelegramGroup
  AND ActiveMaskAvailable
  AND ContentProviderLaneAdmitted
```

本 adapter 不自建线程池或 timeout：Telegram connect 5 秒 ceiling、已 call-issued 的 send RPC 10 秒 ceiling、单次 LLM invocation 15 秒 ceiling，均引用统一 `ExecutionResiliencePolicyRevision`，实际值再受剩余 natural/deadline window 限制。connect timeout 可证明业务 mutation 未调用；send RPC timeout 必须进入 remote unknown；LLM timeout 消耗调用预算。三者只立即释放本地 `ExecutionBulkheadLease`，未由当前隔离 runner 证明 transport 终止的 `RemoteInvocationFence` 继续占 account/group/proxy-route/verified-egress 或 Provider route/lane hard in-flight；call-issued send 即使 transport 终止，业务 identity 仍只对账。proxy binding route 与 canonical verified egress 的 active fences 默认均≤2，同一出口不能因 proxy ID 不同绕过；Task-group active Telegram fences≤`concurrency_limit_per_group`。共享池 Task 份额引用统一自适应合同：1/2/3/4+ 个 runnable Task 的单 Task新 lease 上限分别为 100%/50%/约 33.34%/30%，先公平 quantum、后借空闲。mask/classification/response/proactive/reviewer 子舱壁服从 Provider 父额度且 response/classification 有保护份额，interactive/passive Gateway 也分池。

account/proxy-route/proxy-egress circuit 默认 5 分钟内 2 次 qualifying transport/connection failure 后隔离 15 分钟，期满只允许低优先级单 owner、无业务副作用的 half-open probe；probe 成功以当前 dependency revision 关闭 circuit 后，新业务 claim 才恢复。业务主链直接跳过 open/half-open/quarantined allocation，不在 worker 内等待。结构化 FloodWait/SlowMode 不进入这些 circuit，只写 authorization/session/peer transport availability。quantity-only obligation 在 account-bound identity 形成前也只能在 task-day selected 内重绑本条账号；全员 coverage account 与 participation selected 不可被 standby 代偿。route circuit 需要明确当前节点/绑定错误，egress circuit 需要同一 verified exit 上至少两个 distinct account 的相关失败，单账号失败不得熔断整条代理或出口。

核心原则：**恢复只收敛状态，不创造新的业务时间。**

## 12. 配置、页面和 API

### 12.1 配置语义

保留：

- `account_group_ids[]`、binding-set revision、`concurrency_limit_per_group` 与 membership/group-state snapshot policy；unified 正常 Task 至少一个 enabled、用途一致的普通运营分组，空/重复/跨租户/disabled/dedicated/non-normal 组失败，不回退全租户；legacy `all` 只有在全部兼容组按相同稳定规则得到的 policy-eligible 并集与旧 policy-eligible scope set/hash 精确相等时才能转显式多组 binding；
- `daily_message_target`；
- `daily_target_jitter_bps=0..3000` 与 target min/max，由统一 stable seeded uniform + round-half-up 冻结，不使用运行时随机；
- `account_group_participation_mode=all_group_members_daily` 与 `per_account_daily_min_confirmed`；
- `hourly_activity_profile=natural_full_day_v1`；页面只读展示统一北京时间的 `hourly_activity_curve_snapshot[24]`，不允许把原始 24 小时条数或其他 timezone 作为运营输入；
- 绑定组、每日 selected coverage、persona、内容和安全策略；
- `context_response_enabled` 与按 turn class 的参与率；
- `participation_policy_v1`：点名/直接问题 100%、开放问题 70%、实质讨论 40%、普通观点 20%、micro-ack 5%，先 admission 后判断容量；
- `interaction_capacity_policy_v1`：每群每日目标的 40% 为 response flexible 总池，再稳定抽样到预测有人活动的小时；不是每小时至少 1 条；
- `tempo_policy_v1`：question 8～35 秒、active 12～60 秒、ordinary 45～180 秒及对应 freshness deadline；
- `context_snapshot_budget_v2`：active turn/reply/unresolved anchors 固定保留 + 最新 10～20 条相关消息 + 4,000 token 总上限；
- `pre_gateway_context_gate_v2`：call-issued 前 1 秒 review window，semantic topic advance 阈值 5 条不相关真人消息；
- `conversation_attention_v2`：低优先级 proactive quiet-after 60～180 秒，小样本 180 秒；
- `execution_resilience_policy_version`：remote hard timeout、proxy/Task-group/workload bulkhead、竞争态 Task 份额、circuit/quarantine/half-open probe；
- 每个实时 `InteractionServiceBinding` Provider calls 上限 2；successor binding 继续消耗同一任务日总 binding/call budget。classification permits/预算按 tenant/provider/surface 共享 revision 冻结，并包含 candidate fanout/claim tail；response permits/预算按本 Task 测得 arrival 与完整 service P95 冻结。active binding、总预算 conditional CAS 与 Provider reservation 必须同事务，不能先绑后补预算。

新增版本化系统策略，不作为隐藏常量：

- `burst_assembly_policy_revision`（候选窗与 close 规则由统一引擎托管，不接受 adapter 私有固定秒数）；
- `context_response_deadline_by_turn_class`；
- `conversation_tempo_policy_version`；
- `interaction_capacity_policy_version`；
- `context_snapshot_budget_version`；
- `group_community_style_policy_version`；
- `content_dedupe_policy_version`；
- `execution_timing_policy_version=execution_timing_policy_v1` 与冻结的 `execution_timing_profile_revision`；`generation_lead` 只从该 profile 派生，不再拥有 AI adapter 私有 policy/version；
- `group_send_policy_version`；
- `account_send_policy_version`；
- `context_response_generation_budget_version`；
- `provider_capacity_policy_version`。

删除/退休：

- `messages_per_round` 作为发送批次规模；
- `due_catch_up_pipeline_depth`；
- 任何 hard-hourly 追赶、future-to-now rewrite 和 Dispatcher 生成正文配置；
- 新合同下可关闭的 `source_capacity_v2_enabled`。

### 12.2 任务详情

任务详情必须分开展示：

1. 今日目标：绑定账号组及各 membership revision、规范化成员并集、configured/effective quantity，以及 selected/admitted/sendable/coverage blocked/confirmed/shortfall；
2. 小时计划：每小时 quota、future/due/confirmed/missed；
3. 主动节奏：slot window、pacing due、effective claim、Gateway start、current attention state/quiet-after、attention forecast/confidence 和因真人 turn 延后/shortfall；
4. 上下文响应：eligible/blocked subscription、expected/terminal/candidate-decision-missed、observed/eligible/ineligible/deferred-wait/deferred-expired/participation candidate/admitted/peer-turn-coalesced/served/validly-superseded/missed、跨 Task 唯一 claim winner、response reserve planned/consumed/released、replay window/sample/confidence、unique-owner/still-needed-owner demand P95、forecast superseded evidence、required/valid response slots、tempo class、自然发送窗和 capacity/deadline shortfall；
5. 真人反馈：authoritative reply、inferred continuation、机器人质疑/删除撤回/抢答负向 outcome，以及样本是否达到可判定门槛；
6. 当前阻断：第一 blocker、dependency domain/revision、bulkhead 等待、circuit state/quarantined-until/next probe、basis version、wake condition，以及 running_partial/blocked 聚合依据；
7. 远端状态：before-call、call-issued、remote unknown、confirmed；
8. 历史尝试：Generation variation 和 Action attempt，与当前 backlog 分开。
9. 内容质量：四道重复门的拒绝阶段/原因、context snapshot revision、截断原因、stale-before-Gateway 和自然度灰度指标。
   同时显示 community style profile 的 human-observed/cold-start、样本量/revision、每条 message style reservation/active assignment/binding revision，以及 assigned/accepted/remote-confirmed 的长度/问句/标点/emoji 分布；
10. Provider：共享 classification 与本 Task response 各自 required/available concurrency、queue delay、classification downstream tail/latest-safe、response estimated candidate ready/timing-feasible call interval、每个 service binding 及任务日总 calls/tokens/cost、deadline admission 和 successor binding 剩余预算；同一 canonical turn 不因重叠 Task 重复计 classification 调用。

顶部总状态拆成 `day_operation_status / quality_evidence_status / product_acceptance_status`，不得用一个 completed 同时表示“数量完成”“账号全覆盖”和“自然度已验证”。

页面不得把历史 failed Action 数显示为当前待发送数量。

## 13. 并发、幂等、安全和隐私

### 13.1 并发与幂等

- obligation natural key 对任务日/目标/quantity identity 唯一；
- 同一 obligation 同时最多一个 open GenerationJob、一个有效 Action；
- context turn 同时最多一个 active `InteractionServiceBinding`；
- response turn binding 使用原 response-reserved obligation expected version + claim/slot active revision CAS，并 append service binding；不修改 proactive-fixed 义务，也不把 turn identity 写回数量 natural key；
- coverage obligation 的 account identity 从 scope 到 remote fact 不可变；
- ContentDedupeReservation 在群级 timeline 锁内原子写入，active/unknown/confirmed 均参与冲突判断；
- AI adapter 不定义第二套锁序；所有 Planner/Generation/Dispatcher 严格复用统一公共顺序 `ConversationTurnClaim(if any) -> ConversationAttentionState -> obligation/projection -> EngagementPacingSlot -> TimelineReservation(account -> peer -> conversation -> task_obligation) -> InteractionServiceBinding(if response) -> ConversationReplyAuthorityDecision(if AI response) -> InteractionCapacityPlan response budget counter or TurnClassificationCapacityRevision counter -> ProviderCapacityReservation -> active MessageStyleAssignment -> GenerationJob|Action -> Attempt/fact projection`；classification 无 response binding 时从共享 classification counter 开始。禁止先锁 Action/style/GenerationJob/Provider reservation/authority decision/binding 再反锁 attention/turn/obligation/timeline；
- remote mutation key + gateway request hash + fact kind 唯一；
- projector 可重复执行，但每个 projection kind 对同 fact 只成功一次。

### 13.2 安全边界

- 外部 Telegram 文本只作为不可信数据，不能覆盖系统 prompt、账号权限或发送策略；
- tenant、Task、canonical group、account authorization 和 lifecycle epoch 全链冻结并复检；
- GroupConversationSnapshot 的每个 raw message、reply parent、summary source 与 AI memory 必须属于同一 tenant/group；跨群只允许不可逆 dedupe 特征和无事实 persona 属性；
- 日志不保存完整 prompt、正文、Session、AuthKey 或账号凭证；只记录必要 hash、版本、typed reason 和受权限控制的证据引用；
- reply authority 不使用 sender name、正文或 Action.result 猜测目标/归属；external-human 目标只由 canonical event author class + exact remote identity/revision 授权，owned 目标只由 bound typed fact 授权；
- FloodWait、SlowMode、封禁、准入和账号授权是硬门禁，高于响应时延目标。

## 14. 可观测性与成功指标

### 14.1 节奏指标

- `hourly_quota_planned/confirmed/missed`；
- `pacing_due_to_call_issued_lag_seconds`；
- `same_group_same_second_call_issued_count`，目标为 0；
- `proactive_stratum_violation_count`，目标为 0；
- `five_minute_peak_by_lane`；
- `cross_task_group_collision_count`，目标为 0；
- `overdue_rewritten_to_now_count`，目标为 0；
- `recovery_burst_count`，目标为 0。

### 14.2 全账号覆盖指标

- `coverage_required/ready/blocked/unknown/confirmed_accounts`；
- `coverage_completion_ratio`，分母固定使用 scope revision，不因 blocked 缩小；
- `quantity_met_coverage_unmet_count`；
- `per_account_first_confirmed_at` 与距任务日结束剩余时间；
- `coverage_account_rebound_count`，目标为 0；
- `coverage_shortfall_by_typed_blocker`。

### 14.3 响应指标

- `listener_event_persist_latency`；
- `stage_wake_delivery_lag`，P95≤1 秒、>5 秒告警；
- `turn_close_to_participation_decision_latency` 与 `decision_to_candidate_ready_latency`；
- `expected_task_candidate_count / terminal_candidate_decision_count / candidate_decision_missed_count`，terminal coverage 目标≥99%；
- `context_turn_observed/eligible/ineligible/deferred_wait/deferred_expired/participation_candidate/skipped/admitted/peer_turn_coalesced/served/validly_superseded/missed`；
- `response_reserved_planned/consumed/released/shortfall`；
- `interaction_forecast_replayed_turns / unique_owner_demand_p95 / still_needed_owner_demand_p95 / forecast_superseded_count / required_service_slots / valid_response_slots / interaction_plan_unachievable_count`，按目标群、Task 和 turn class 分列；
- `admitted_resolution_ratio = (served + validly_superseded_before_planned_call) / admitted`，目标 ≥95%；
- `interaction_capacity_service_ratio = served / (admitted - validly_superseded_before_planned_call)`，目标 ≥95% 或分母为 0；容量/Provider/deadline miss 均不得作为 superseded 删除；
- `call_issued_inside_tempo_window_ratio`，目标 100%；
- `preparation_feasible_call_not_before_at / planned_call_at / candidate_ready_at / planned_point_late_unexpected_tail_count`，planned point 不重抽，admission 时已知会晚于 planned call 的 active binding 为 0，越过 natural window 的发送为 0；
- `response_binding_budget_used/remaining` 与 `response_call_budget_reserved/used/unknown/released_unissued/remaining`；successor 不重置，重复 terminal 不二次释放；
- `context_response_stale_before_gateway`；
- `responses_per_peer_turn_across_tasks`，同 tenant、同 canonical group 必须 ≤ 1；
- `ai_message_caused_recursive_response_count`，目标为 0。
- `proactive_deferred_for_human_turn_count / proactive_human_turn_window_shortfall_count / human_turn_interruption_after_call_issued_count`。

### 14.4 重复与自然参与指标

- `duplicate_rejected_by_stage_and_code`，区分 pre-generation、candidate、reservation、Gateway；
- `confirmed_group_exact_duplicate_30d_count`，目标为 0；
- `gateway_unknown_dedupe_hold_count` 与 hold age；
- 最近 100 条 semantic cluster、normalized term、speech act 分布和最近 20 条非 stop 2-gram/四字开头重复率；
- `context_anchor_supported_rate` 与 `context_revision_stale_rate`；
- `context_anchor_claim_match_rate / active_turn_response_fit_rate / unsupported_group_claim_rate / generic_context_reply_rate`；canary 中前两项分别≥95%，后两项为 0；
- `platform_run_length`、平台问句后 60/300 秒其他平台账号接管率；
- 成功正文按账号统计的句长、问句率、标点、emoji、开头与 semantic cluster 距离；
- `group_community_style_profile_source_count{human_observed,cold_start}`、style reservation/assignment/context-unallocatable/stale-superseded count、assigned/accepted/remote-confirmed style distribution distance、固定 ordinal/style sequence collision；
- 同账号 persona 的 intra-account drift 与跨账号 distinguishability，只读取 remote-confirmed normal contextual facts，不以 Prompt 配置冒充；
- 真人盲评的上下文贴合、自然度、模板感、账号可辨识度和事实一致性。
- `authoritative_human_reply_rate / inferred_human_continuation_rate / ambiguous_unattributed_rate / positive_event_multi_attribution_count / robot_suspicion_signal_rate / post_ai_delete_or_withdraw_rate / platform_interruption_rate`；native parent 优先且不限 inference window，非原生正向 event 最多一个 winner，负向率按 human event 去重，并与同 peer/time-band 基线比较。

“无法识别为 AI”不作为可证明指标。正式质量 Gate 使用相对批准基线的盲评改善、确定性重复率和事实/安全不退化共同判定，不能只用一个 LLM Judge 给自己打分。

群聊的正式样本和阈值直接继承统一引擎 `interaction_quality_policy_v1`：连续至少 3 个任务日、100 条 confirmed normal contextual facts、50 个 admitted turns、30 条 served responses、30 条预注册盲评样本，覆盖至少 3 个群/话题簇和 10 个账号；上下文锚点 ≥95%、无意义插话 ≤3%、semantic/template duplicate ≤3%、明显机器生成票率 ≤30% 且较批准基线至少下降 20 个百分点。同一 peer turn 跨 Task 多重响应必须为 0；真人反馈每 route 至少 30 条 confirmed fact 且观察满 24 小时才可判定，否则为 `interaction_outcome_unproven`。

### 14.5 成本与质量

- 每个 confirmed obligation 的 Provider request 数和 token；
- 未发送候选 token 占比；
- context revision 失效造成的生成浪费；
- response required/available concurrency、queue delay、estimated finish 和 Provider capacity missed；
- deterministic gate/reviewer rejection 分类；
- 人工盲评的上下文贴合、自然度、账号区分度和事实一致性。

LLM 成本按以下公式在灰度前估算：

```text
每日成本
= proactive obligations × proactive 每条平均输入/输出 token
 + classifier-eligible ambiguous turns × classification 每 turn 平均 token
 + provider-requiring response bindings × response 每 binding 平均输入/输出 token
 + reviewer/retry token
```

`required_classification_concurrency = ceil(ambiguous_turn_arrival_rate_p95_per_second * classification_service_p95_seconds * 1.30)`，`required_response_concurrency = ceil(arrival_rate_p95_per_second * complete_response_preparation_p95_seconds * 1.30)`；两者从统一 timing profile 对应 lane 取值并分列 permits。classification 由 tenant/provider/surface 共享 `TurnClassificationCapacityRevision` 负责，Task 启动只校验并引用，不为同一群重复预留；它的 latest-safe 必须从冻结 `candidate_decision_cutoff_at` 扣除最大 eligible-Task fanout projection P95、claim finalize P95 与统一 margin，该 cutoff 由 `BurstAssemblyPolicyRevision/ContextTurn.closed_at` 产生。完整 response preparation P95 必须包含主生成、批准的质量修复/备用 tail 与确定性门，不能只量第一次 realizer。planned call 只从 `natural/slot/Timeline interval ∩ [estimated_candidate_ready + Gateway prepare + margin, freshness deadline]` 内抽取；active binding、任务日总预算 CAS 和 Provider reservation 同事务。每个 canonical turn revision 最多 1 次语义分类调用；每个实时 `InteractionServiceBinding` 最多 2 次 Provider 调用（1 次主生成，加最多 1 次质量修复或批准的备用 route）。admission 预留最多 2 次；调用边界转 used/unknown，binding terminal 只释放从未发起部分，binding identity、used/unknown 和成本不归还且重复 terminal 不二次释放。pre-Gateway 归还数量义务后建立的 successor binding 不继承旧 binding 的单次上限，但继续扣同一任务日总 binding/call budget。共享分类池按去重后的 ambiguous canonical-turn demand P95 冻结；Task response 池按本 Task `provider_requiring_owner_demand_p95` 冻结。主动泳道继续使用独立质量预算。预计完整路径越过 planned-call latest-safe 时第一次调用也不发起。三类 permits/预算不得互相挪用；预算含 30% 波动 buffer，不能以无限重生成保证成功率。

## 15. QA 与验收合同

### 15.1 确定性排期

1. 相同 Task、group binding set、全部 membership revisions、task day、数量 policy 得到相同成员并集、selected set、raw/effective target、coverage adjustment 和 plan hash；重启、多 worker 和重试不重抽；
2. 增删分组、组并发上限或成员变化只形成 successor；已冻结日计划集合和数量不被原地改写；
3. stable seeded uniform 的 hash 映射、±30% 边界、0 jitter、round-half-up 和 min/max clamp 有跨语言 golden vector；账号覆盖与真人 turn participation 的 seed/字段互不复用；
4. 同 ledger/plan seed/config 重跑得到完全相同 hourly quota、strata 和 due；
5. 每小时 `q` 条时 `proactive_fixed + response_reserved = q`，每个 stratum 最多一个 active obligation；
6. 每个 response-flexible stratum 的 release cutoff 后仍可容纳完整 proactive preparation/Gateway P95 与统一 execution safety margin；不满足时改选其他 stratum，整体不足时计划前 blocked 而不是冻结必然 quantity shortfall；
7. materialization horizon、generation latest-safe、release cutoff、protected slack 和 `generation_lead` 必须全部回贴同一个冻结 `ExecutionTimingProfileRevision` 与所用 path-start stage，safety margin 等于 `max(5 秒, ceil(complete remaining path P95(path-start stage) * 20%))`；缺少批准 profile 时 unified route 零 activation，worker 私有常量为 0；
8. 日目标小于活跃小时数时，不得总是选择最早 N 小时；
9. 多 Task 同群或同账号分组时，由统一 `PacingPlanner` 通过 `EngagementPacingSlot + TimelineReservation` 分配；`view -> interactive` 的账号级 300 秒默认 gap 可回放且不能绕过；
10. partial start 不产生 anchor 前债务；
11. target 增减不改写已有 frozen/Gateway-started slot。

### 15.2 全账号活跃覆盖

1. configured member union、policy eligible、planning admissible、selected、runtime admitted/sendable、confirmed 六层集合各自可回读；blocked 不能从 selected 分母消失；
2. 默认 selected 为全部绑定 AccountPool 成员快照的规范化 policy-eligible 并集，每个 selected account 在每个 canonical group 都必须有自己的 normal contextual fact；
3. 一个账号缺面具、Session、peer membership 或一个 proxy route 失败时，健康账号仍继续产生 obligation/Action/fact，Task 显示 `running_partial` 而不是全局暂停；
4. 单个目标群选择 10 个账号、配置群日 raw target 6 条时，该群 effective target 必须提升到至少 10，每个账号生成一个独立 coverage obligation；同一 Task 有 3 个目标群时必须形成 30 个 group-account coverage units，不能只用 10 条跨群结算；
5. 一个账号发送两条不能替代另一个账号的零条；只有各自 `content_quality_class=normal_contextual` typed remote fact 能关闭各自 coverage，签到/静态 fallback/纯占位短句不能关闭；
6. blocked/unknown 账号保留在 required 分母并展示原因，不缩小分母制造 100%；
7. Task day 冻结后新迁入成员默认下一任务日生效；紧急 disable/迁出只改变 runtime blocker，不移动已有 slot、不删除分母，剩余容量不足显式 shortfall；
8. 数量已达标但 coverage 未达标时，任务状态必须为 `quantity_met_coverage_partial`，不能 completed；
9. response-reserved coverage obligation 绑定 turn 时必须保留同一账号，persona 不兼容则尝试其他 reserve；A 群事实不能关闭 B 群 coverage；
10. required group-account units 必须全部匹配到合法全天 strata/Timeline 才可显示 plan achievable；账号数或 effective target 超过合法容量时预览明确 blocked/shortfall，不能缩短间隔或在末小时补齐；
11. connect/send/LLM hard timeout 分别按 safely-not-called/remote-unknown/provider-unknown 结算；本地 lease 及时释放，但未确认终止的 `RemoteInvocationFence` 仍计 proxy binding-route≤2、verified-egress≤2（两个 proxy IDs 同出口仍合并）、Task-group cap 与账号 1 个 mutation。TTL/重启/cancel-requested 不放出第二调用，runner 终止只释放在途计数、不清 call-issued 业务 unknown；自适应 Task 份额（1/2/3/4+ 为 100%/50%/约 33.34%/30%）、公平量子后借用与 route/egress circuit half-open 单 probe 均可并发验收，慢代理/爆量 Task 不阻塞健康分区。

### 15.3 上下文响应

1. Telegram update 经单 owner cursor 原子写 context event/outbox，不等待 history poll；重复 update、cursor takeover、gap backfill 不重复形成 turn；
2. 新 route 复用 authorization update ingress，不启动第二 Telegram collector；同群 unified response authority 生效后，legacy `listener_auto_reply`/Campaign 零新 draft、Task、Action；
3. event/candidate/GenerationJob/ready Action wake 与业务状态同事务；重复/丢失通知分别幂等回读/由 durable outbox 恢复，链路不串行等待多个 worker tick；
4. 同一 turn 多条真人消息、多个命中 Task 最多一个 peer-level 响应义务；各 Task candidate 与唯一 claim winner 可重建，loser coalesced，winner 无容量不得由其他 Task 补答；
5. claim 等待冻结 eligible subscription set 全部 terminal decision 或群聊 3 秒 cutoff；首个 worker 不得抢先 owner，cutoff missing 可见且 terminal coverage≥99%；合同未就绪 Task 显式 blocked 而不抢 owner，当前容量不得用于排除 eligible Task；明确点名/原生回复的 required account/owner 在 Task 路由前冻结，其 decision coverage 必须 100%，缺失或 blocked 时非 required Task/账号零响应；
6. 平台自己的消息不触发递归响应；
7. response-reserved 日计划只冻结 capacity window/tentative supply，不含未来 turn 的 planned call；owner 后先冻结 natural window，再扣除当前 permit 队列、完整准备链 P95、Gateway prepare 与 margin，只在 timing-feasible supply/Timeline 交集中原子创建 active `InteractionServiceBinding + planned call + Task-day budget reservation + ProviderCapacityReservation`。响应只绑定一个原数量义务，quantity identity、ordinal、账号和 active target 数守恒；
8. reserve 未使用时只在 cutoff 后于原 stratum 剩余窗转 proactive；无容量时零 GenerationJob/Action/Telegram call，并把 miss 留在 admitted 分母；
9. owner 后 natural window 稳定，service binding 后 planned call 稳定不重抽；预测已赶不上 planned point 时零 active binding/零调用，Provider 早完成等待，只有未预测 tail 才可在 binding 交集内晚发并单独计数；超过交集/window 或 45/90/180 秒 freshness deadline 后即使 candidate ready 也不得发送；
10. turn revision/watermark 过期时，pre-Gateway candidate 必须终结为 stale shortfall；
11. planned call 前真人已解决/转题可记 validly superseded 并保留 admitted identity；planned call 后因 Provider/容量/时间线延迟失效必须按真实 blocker missed；
12. external-human native reply 只认 canonical event 的精确 peer/thread/topic/message/revision、外部真人 author class 和无未闭合 stream gap；owned native reply 只认我方 bound typed remote fact。两类都必须引用同一个 `ConversationReplyAuthorityDecision`，direct 不伪报 reply relation，旧 `GroupContextMessage` 单独不能授权；
13. 真人对我方 confirmed fact 的原生 reply 与推断续聊分别形成 observation；native parent 不受 inference window 限制且优先，非原生 event 只有唯一高置信 winner 才计正向，同 event 多 fact attribution 必须为 0。低置信度/歧义与负向结果不被过滤，负向率按 event 去重，且不增加数量或覆盖；
14. admission 前 `deferred_wait` 可由 event/timer 重评，deadline 后只进 `deferred_expired`；stream gap、watermark stale、subscription decision 不完整或 response authority 双写时，哪怕 admitted 分母为 0，interaction observation integrity 也不能通过。
15. deferred Task 迟到唤醒时，若同一 turn claim 已有 admitted owner 只能 coalesced；无 owner 才能 CAS 新 decision round 并重冻全部 expected decisions，旧 round 不接收追加 candidate，任何 turn 仍最多一个 owner。
16. 多目标群 Task 必须逐群证明 observer coverage、gap、watermark 和 candidate decision integrity；任一 required group 缺失时 Task 不得用其他健康群的指标聚合成 met。
17. planned call 前后发生的 pre-Gateway stale 都必须 fence preparing/ready 工作并归还同一 response-flexible 数量义务；前者互动 outcome 可为 validly superseded，后者仍为 blocker missed。cutoff 后按原 release policy，Gateway call-issued 后禁止归还或替换。
18. 每个目标群扩大 response route 前用最近 30 天至少 7 个完整 active 日、50 个真人 turn 做 participation/跨 Task claim replay；先冻结 unique-owner demand 再比较合法 slots。样本不足仅可用显式 cold-start forecast 做预注册限量 canary且保持 unproven；valid slots 不足不得扩大，不能用固定 40% 宣称 capacity ready。
19. 规则无法确定的 canonical turn revision 最多调用一次独立 classification lane；classification latest-safe 必须先从 3 秒 cutoff 扣除 Task fanout projection/claim finalize P95 与统一 margin，不能只要求模型本身在 cutoff 前返回。unknown/超时不套默认普通观点，分类 permits/call budget 不占 response generation permit。classifier-eligible ambiguous turn 的 uncertain 比例超过 5% 时高互动 Gate 不通过。
20. Generation snapshot 固定保留 active turn/reply/mention/unresolved anchors，再选最新 10～20 条相关消息；噪声不占配额且不能挤掉引用目标。相同 snapshot revision 的 message-id set/hash 可回放；
21. call-issued 前 1 秒 review window 以 revision CAS 覆盖 parent 删除、真人已回答、topic 切换和 semantic anchor 后超过 5 条不相关消息；native reply 不因后续消息数量机械失效。stale 只在原 natural window/总调用预算内 append regeneration，否则 shortfall；

### 15.4 Generation 与 Action

1. Planner/ResponseRouter 不调用 Provider；
2. 每个 Job 只生成一个义务的一条候选；
3. accepted candidate 前 Action 数为 0；
4. Action 创建后正文、账号、group、reply、obligation 和时间身份不可变；
5. 新 context revision 只失效 pre-call Job/Action，不改 Gateway-started；
6. 质量失败不发送 Stage 1、签到或 emoji 伪成功。
7. context response 质量/去重/Provider deadline 失败按真实 blocker 结算当前 admitted miss，调用数/成本保留；pre-Gateway 同一 response-flexible obligation 解绑归还，cutoff 前等新 turn、cutoff 后按原规则释放 proactive，窗口结束前不提前形成 quantity terminal，且同 turn 总调用不超过 2。
8. 每个 admitted turn 只有一个 `InteractionServiceBinding` 且 calls≤2；active binding、任务日冻结总 binding/call budget conditional CAS 与 Provider capacity reservation 同事务。数量义务归还后的 successor 继续扣同一总预算；terminal 只释放未发起 call reservation，binding identity、旧调用、unknown 与成本不清零，重复 terminal 不二次释放，并发扣减不能超预算。
9. current route 不存在固定 30 分钟预生成；`generation_not_before_at` 必须由完整链 P95+margin 倒推，只有实测可达才允许计划点前 5～10 秒 JIT，不能因硬等最后 5 秒让 reviewer/Provider 必然过期。
10. 单次 LLM invocation 15 秒 hard ceiling 后必须形成 typed provider timeout/unknown 并释放本地 worker lease；Provider `RemoteInvocationFence` 在响应或当前隔离 runner 的 transport termination acknowledgement 前继续占父 route/lane 并发，调用/成本预算不归还，禁止吞错后返回模板成功。

### 15.5 重复与自然参与

1. 同群 `normal_contextual` 30 天 exact、同账号 10 天 exact/similar/semantic/template、群级 100/20 条窗口均按 policy 得到确定性结果；micro-ack 走独立频率门且不能结算数量/coverage；
2. 两个并发 GenerationJob 生成相同正文时，只有一个 ContentDedupeReservation 成功，另一个不得创建 Action；
3. candidate accepted 后若另一条正文先确认，Gateway Tx A 必须阻断旧候选并记录 `stale_duplicate_before_gateway`；
4. Gateway unknown 持续占用 dedupe window，普通 reservation TTL 不得释放；
5. active turn、reply chain 和 recent raw tail 总是来自同一 tenant/group，跨群原文注入必须 fail closed；
6. active turn/reply/unresolved 固定集 + 最新 10～20 条相关消息/4,000 token 双预算触发时保存显式 truncation reason；系统噪声不占 relevant tail，关键引用不得被尾部历史挤出；
7. 平台问句后的其他平台账号不会立即自问自答，一个真人 turn 最多一个平台 response；
8. 相同上下文下不同账号实际输出需要在真人盲评中保持可辨识，且模板感、事实一致性和上下文贴合不低于批准基线。
9. 同群/time-band 外部真人样本达到 50 条时冻结 human-observed community profile；不足时 group 级 cold-start 先验稳定但不同群不复制统一比例，受管账号/bot/AI 成稿不进入样本。
10. 同 obligation/account-binding/style-policy/seed 的 `MessageStyleReservation` 可重放；同 content-intent/turn-binding/preparation-timing/planned-call/profile/persona 得到相同 `MessageStyleAssignment`。上下文响应在真实 turn/addressee/anchor 冻结前不得存在具体 assignment；quantity-only 合法换号 append 新 account-binding/persona reservation 并 supersede 旧风格，coverage-bound 不换号；stale pre-Gateway append successor，Gateway-started 后不换。successor profile 不改旧 reservation/assignment，assigned/accepted/remote-confirmed 分布能回贴同一 revision，且无固定 hourly ordinal 风格序列、账号固定口头禅或跨账号长期同声线。
11. active style assignment 与 timeline version 校验及 `preparing` 转移原子完成；同一 preparation-timing revision 内 planned call/time band 不再 reflow，并发 TimelineArbiter 不能制造 profile/time-band mismatch。
12. `group_style_compatibility_v1` 全矩阵覆盖：明确问题实质回答，纠错/负向反馈先承接，micro-ack 不反问，playful 只有当前 turn evidence，proactive 不携带旧 turn identity；无 compatible register 显式 shortfall。
13. context response 必须回传并命中 active turn/reply chain 的允许 anchor；proactive 必须命中 active/approved topic。最终正文中的姓名、数字、地点、关系、承诺和经历可逐项回贴 source/task fact，未知/跨群 ID、万能接话和 unsupported claim 全部拒绝。accepted `expects_human_reply` 由质量决定和 evidence 冻结，同 content hash 重放一致；单凭问号/Provider 自报不得打开 awaiting-human。
14. 真人 turn、admitted response 或 awaiting-human 窗口存在时，proactive 在 Provider/Gateway 前被推迟且仍在原 window；放不下显式 shortfall，不插话、不改成 response、不跨日追赶。call-issued 后才出现真人事件只追加负向 observation。
15. attention 的四类 blocker 可重叠；真人间隔不足 30 个时 proactive 等待窗固定为 180 秒，达到样本后使用 60～180 秒 P90。真人事件、terminal outcome、typed fact 和 expiry 均产生可重放 revision/wake；历史 backfill、AI/机器人事件和旧 wake 不延长状态，active attention 内无关 proactive call-issued 为 0，所有状态在有界 expiry 后收口；明确点名/问题 response 不受 proactive quiet-after 阻断。
16. attention 在 proactive preparing/ready 后、call-issued 前出现时，旧 Job/candidate/Action/style 被同事务 fence/supersede，可安全 reservation 释放，preparation-timing revision 递增；只有原 window 仍可完成时才重新生成/验收，旧正文/request identity 不复用，否则显式 shortfall。call-issued 后零 replacement。

### 15.6 Dispatcher、恢复与远端事实

1. future Action 不能被 claim；
2. 多个 overdue Action 不得同批压到 now；过 window 的单位形成 typed shortfall；
3. Tx B 后 crash 只进入 unknown/reconcile，不创建 replacement；
4. 同一 remote mutation 最多一次 Gateway call-issued；
5. success 必须有非空 remote message ID 和 canonical typed fact；
6. target/coverage 只由 fact projector 确认；
7. 多 Dispatcher、多 Generation worker、listener/recovery 并发下无双发、lost wake 或逆序锁死。

### 15.7 生产 E4

生产验收至少连续 3 个完整任务日，并达到统一引擎冻结的 100 confirmed、50 admitted turns、30 served responses、30 盲评样本、3 个群/话题簇和 10 个账号门槛：

- 每小时 confirmed 与 frozen hourly quota 对账；
- proactive Telegram call-issued 时间落在自身 stratum/latest window 内；
- 同群同秒 call-issued 为 0；
- 无 recovery burst、无 overdue-to-now；
- event/decision/generation 分段 P95 达标，call-issued 位于 tempo window，且同群同 turn 跨全部 Task 最多一条；
- admitted resolution 与 still-needed capacity service 均≥95%（或对应分母为 0）；capacity/Provider/deadline miss 未伪装成 validly superseded；
- quantity 与逐群逐账号 coverage 分别闭合，每个账号在每个目标群都有各自 non-empty remote message ID；
- Telegram 已确认正文的同群 30 天 exact duplicate 为 0，Gateway unknown 的去重占位逐条可解释；
- 上下文 snapshot 的 watermark、source group 和 Gateway 前 freshness 逐条闭合；
- 真人盲评对上下文贴合、自然度、模板感、账号区分度和事实一致性达到批准阈值；每条 response 的 context anchor/claim-to-source 证据可回贴，generic/unsupported/cross-group 为 0；
- 权威真人 reply、推断续聊和负向 observation 分列回读；达到样本门槛后正向率不低于基线、负向率不高于基线；
- 每条 confirmed 均闭合 `Obligation -> Generation/Action -> Attempt/Gateway -> typed remote fact -> target/coverage`；
- Gateway unknown 数量和对象逐条可对账，且没有 replacement 重发。

本地测试、CI、发布成功、容器 healthy、Action success 或数据库行数均不能替代 E4。

## 16. 迁移、灰度与回滚设计

### 16.1 Additive 迁移

1. 新增 AccountPool membership revision/snapshot set、Task multi-group binding-set revision、participation/quantity plan、participant runtime dependency projection、ExecutionResiliencePolicy/Bulkhead/Circuit/HealthProbe、PreGatewayContextDecision、quantity/coverage target set、AI hour quota input、interaction capacity plan、current send obligation、conversation cursor/event/outbox、canonical turn classification 与共享 `TurnClassificationCapacityRevision`、context turn/opportunity/peer-turn claim/snapshot、response capacity window/tentative supply、InteractionServiceBinding、MessageBrief/quality decision、tempo profile、ExecutionTimingProfileRevision、group community style profile/message style reservation/assignment、Task response Provider reservation 与 Task-day 总 binding/call budget、dedupe reservation和 human engagement observation；最终 pacing slot/timeline owner 复用统一引擎，不新增 AI 专用 group schedule；
2. 为现有 content intent/GenerationJob/Action 添加指向新 obligation 的 current binding；
3. 旧 quantity slot/Cycle 保持只读历史，不直接删除；
4. 新旧 writer 使用 route fence，任何一个 Task/任务日只能由一个 writer 创建新发送工作；
5. 为每个目标群冻结 `ConversationResponseAuthority`；接管 preview 必须枚举 `listener_auto_reply_enabled`、`enable_legacy_campaign_worker` 下可能创建的监听 Campaign/draft/task、旧 context-bound Action 和 open/unknown 状态。unified response authority 生效后，legacy 自动续聊对该群只能停写或收口既有 Gateway identity，不能再创建多账号 drafts；
6. Gateway-started、unknown、success 和 remote fact 永不迁移或改写。

### 16.2 Shadow

先运行 24 小时只读 shadow：

- 只生成 multi-group binding/membership union、stable percentage quantity target、quantity/coverage target、hour plan、response capacity window/tentative supply/release、统一 pacing/timeline projection、bulkhead/circuit decision、event/turn/classification 漏斗、candidate natural window、拟议 service binding/planned-call intersection、ExecutionTimingProfileRevision、共享 classification admission/预算与本 Task response admission/总预算、10～20 条相关上下文 snapshot、pre-call context decision 和 dedupe decision；
- 不创建 GenerationJob、Action 或 Telegram call；
- 对比旧链与新链的每小时分布、5 分钟峰值、逐账号覆盖、预计响应时延、上下文新鲜度、重复拒绝、自然度离线盲评、capacity shortfall 和 Provider 成本。

### 16.3 Canary

只允许一个明确 Task/群进入新 route：

1. 旧 writer 停止为该 route 创建新 Action；
2. 同群 `ConversationResponseAuthority` 已原子切为 unified，legacy listener/Campaign 自动续聊在该群零新写；
3. 未进 Gateway 的旧 Action 做精确 preview，按安全分类 retire 或保持旧 route 收口；
4. unknown/Gateway-started 继续由旧 identity reconcile；
5. 新 route 从新的任务日或批准的剩余小时边界开始，不追补旧债务；
6. 至少完成一个完整任务日，数量、全账号覆盖、时延、分布、去重、自然度和 remote fact 七类指标全部通过后再扩大。

### 16.4 回滚

回滚只停止新 route 创建新义务，不删除新合同事实：

- 已 call-issued/unknown 继续原 route reconcile；
- pre-call 新义务进入 paused/retired 审计，不转换成 legacy Action；
- 不允许同一任务日把新 route 的 active rank 再交给 legacy writer；
- 应用版本可前向回退，但数据库 additive schema 和 typed facts 保留。

## 17. 明确禁止的实现方式

- 把 Listener 轮询从 60 秒改成 5 秒就宣称实时回复已重构；
- Planner 一次生成整轮多账号文本或创建一批空正文 Action；
- Dispatcher 临近发送时再调用 LLM；
- 把所有 `scheduled_at < now` 改为 `now`；
- 通过随机 sleep、worker 内存 timer 或 Python `hash()` 形成不可重放随机；
- 每个 Task 独立随机后依赖“碰巧不撞”实现同群错峰；
- 响应消息不计每日/小时目标；
- 总消息数达标就把仍有账号未覆盖的任务显示 completed；
- 因账号 blocked/unknown 临时缩小覆盖分母；
- 缺少账号分组时隐式扫描全部租户账号，或把分组成员变化原地写进当天覆盖分母；
- 用 `random.uniform/randint` 在每次 Planner 重算日数量/参与账号，或因故障重抽更小目标；
- 一个面具、Session、账号或 proxy 错误触发整个 Task/全部活群暂停；
- 为完成覆盖让 persona 不兼容的账号强行打断当前真人话轮；
- AI 成功消息再次触发 AI response，形成自激活消息链；
- 只在发送完成后统计重复，或只按单账号检查而允许同群跨账号复制；
- Gateway unknown 的重复占位按普通 TTL 释放；
- 把其他群原文、摘要或近期 AI 成稿注入当前群 Prompt；
- 以“100% 无法识别为 AI”、画像字段非空或单一 LLM Judge 分数作为自然度完成证明；
- 为降低 AI 感编造账号的具体身份、经历、地点、消费或关系事实；
- Provider/Telegram 网络调用持有数据库事务；
- 对 Gateway-started/unknown 自动重试；
- 用签到、emoji、旧正文或 mock success 填补质量/容量 shortfall；
- 用 Action success 或 UI completed 替代 typed Telegram fact。

## 18. 已冻结的 v1 产品决策

以下项目已经成为开发合同，不再作为开放项：

| 决策 | 冻结方案 | 业务结果 |
|---|---|---|
| 上下文 turn 合并窗口 | 3 秒 | 合并连续真人发言，避免一批真人消息触发多条 AI |
| 响应时序 | 事件持久化≤3 秒、决策≤1 秒、候选≤12 秒；实际发送按 question/active/ordinary 的 8～35/12～60/45～180 秒 tempo window | 快速理解现场，但不形成统一秒回指纹 |
| 每个 context turn 的响应上限 | 同 tenant、同 canonical group 跨全部 Task 共 1 条 | 删除多 Task、多账号围绕同一 turn 连发 |
| 响应生成预算 | 主生成 1 次 + 质量修复/批准备用 route 1 次；总调用≤2 | 在 deadline 和成本内给一次修复机会 |
| 互动容量 | 每群每日 effective target 的 40% 冻结为 response flexible 总池，再按真人活动预测稳定抽样到不同小时；未用到 cutoff 后在原 stratum 释放为 proactive | 避免低小时配额被 100% 预留并总在小时末释放，同时高互动不靠普通 slot 碰运气、总量不增加 |
| 容量不足 | admitted turn 记 `interaction_capacity_missed` 并留在分母；不超发、不挪未来任务日 | 不能通过缩小分母伪造高互动 |
| 每账号每日覆盖下限 | 每个 Task、每个 canonical group、每个选择账号至少 1 条 normal contextual confirmed fact | 明确“每个群里的所有号都活跃” |
| 账号配置边界 | 每个 unified Task 显式绑定 1..N 个 AccountPool，成员并集去重并保留 origin group/per-group concurrency；默认全员覆盖只指这些绑定组，不指全租户 | 支持不同 Task 使用一个或多个业务账号组，避免隐式 all 串扰 |
| 日数量波动 | `daily_target_jitter_bps=0..3000`，由 Task/day/target-group/membership-set/policy seed 做稳定均匀百分比抖动，再受逐成员 coverage floor 抬高 | 每天有自然波动，且重启/重试不变数 |
| 工业韧性 | connect/send/LLM hard ceiling 为 5/10/15 秒；本地 lease 与 durable remote invocation fence 分层；proxy/Task-group/竞争态 Task/workload 舱壁 + closed/open/half-open circuit；transport termination 与 Telegram call-issued 业务 unknown 分账 | 一个账号、代理或爆量 Task 卡住不拖停全部活群，也不因超时释放在途并发或重复发送 |
| ContextSnapshot 原始尾部预算 | active turn/reply/unresolved anchors 固定保留，再选最新 10～20 条相关消息，总计最多 4,000 token | 保留现场并限制热路径延迟，噪声不挤掉引用目标 |
| JIT 与发前终审 | 生成提前量按完整链 P95+margin 倒推；实测可达才是 5～10 秒；call-issued 前 1 秒 CAS review parent/turn/topic/watermark，semantic topic advance 阈值为 5 条不相关消息 | 不提前半小时生成，也不把过时答案发进已经翻篇的现场 |
| 重复窗口 | 同群 exact 30 天；同账号相似/semantic/template 10 天；同群 100/20 条表达窗口 | 防止跨账号换皮复读，同时给 micro-ack 独立短窗 |
| 群聊 reply authority | `semantic_direct|native_reply_external_human|native_reply_owned_fact` 三类关系显式冻结；external-human 由 canonical event/revision/author class 授权，owned 由本 Task bound typed fact 授权，Provider/Gateway 复用同一 decision | 既能自然引用真人问题，又不把普通上下文行、昵称或正文误当远端目标；三类 relation 分开统计 |
| 上下文相关性证明 | response 必须回传 active turn anchor，最终正文 claim 重新映射 source/task fact并做 semantic response-fit；Provider 自报 ID 不算证据 | 避免“读了上下文但仍发万能话术”以及滚动摘要事实漂移 |
| 自然度验收 | 继承统一引擎 §15.3 的样本、阈值和盲评 Gate | 把降低机器感变成可验证产品目标 |
| 群级表达适配 | 外部真人按 group+time_band 冻结 community style profile，样本不足用 group/time-band 级稳定宽区间先验；日计划逐义务/当前 account-binding 只冻结 reservation，主动 intent/真实 turn/addressee/planned-call 后才与账号 persona 合并为 assignment；quantity-only 合法换号必须换 persona reservation；禁止 AI 成稿自学习和固定 ordinal 序列 | 同一账号有稳定声线，同时不会因提前固定语气、合法换号沿用旧 persona 而与现场或账号身份冲突 |
| 会话注意力 | 活群低优先级 proactive 的外部真人 P90 等待窗限制为 60～180 秒，小样本用 180 秒；明确问题 response 不受此 quiet-after 阻断；四类 blocker 可重叠并有界收口 | 真人正在表达或等待回应时不插入无关主动消息，直接问题又能快速响应，状态不会无限占用 |
| 真人互动反馈 | 原生 reply 与推断续聊分列，负向 outcome 不过滤；每 route 30 条 confirmed 且观察满 24 小时后与基线比较 | 不再只用 AI 发送量代表高互动 |

## 19. Product Design Complete 自检

| 检查项 | 当前结论 |
|---|---|
| 用户原始需求 | 已覆盖一个或多个显式账号分组、回复慢、发送集中、小时内随机、0%～30% 数量波动、绑定组并集账号活跃、JIT/发前终审、降低 AI 感、局部故障/舱壁/断路隔离和只做设计 |
| 产品目标与非目标 | 已定义主动/响应差异、数量守恒、三维完成和自然参与边界 |
| 核心对象与状态机 | 已定义 multi-group binding/membership set、六层参与集合、联合 quantity/participation plan、dependency-domain/circuit/local-bulkhead/remote-invocation-fence projection、quantity/coverage、response reserve、唯一 obligation、event/turn/opportunity、append-only service binding、reply authority、tempo、conversation attention、PreGatewayContextDecision、account-binding community style/persona reservation/late-bound assignment、统一 execution timing profile、classification 下游 tail、planned-call 可达区间、原子 Provider budget/reservation、GenerationJob 和 typed fact |
| 前端/API | 已定义配置语义、预览和详情状态 |
| Worker/数据流 | 已定义日计划只冻结 response capacity window/tentative supply、单 owner 实时事件/outbox/gap reconcile、owner natural window、service-binding 交集内 planned call、生成、仲裁、发送和投影 |
| 并发/幂等 | 已定义单 owner、CAS、群/账号锁序、换号与 persona reservation 原子切换、attention-preemption 的 materialization fence/preparation revision、attention revision/wake 和 remote mutation identity |
| 失败/恢复 | 已定义 5/10/15 秒 hard ceiling 的不同远端语义、本地 lease 与远端在途 fence 分层、transport termination/业务 outcome 分账、mask/session/proxy/membership/provider/listener 局部隔离、竞争态公平借用、circuit/half-open、running_partial、overdue、unknown、停机和 deadline，不集中补发或超时重发 |
| 权限/隐私 | 已保留 tenant/Task/group/account/reply authority 和日志边界 |
| QA/E4 | 已定义确定性排期、逐群全账号覆盖、容量分母、自然 tempo、有界 attention、planned-call 可达性、真人 event 单归因、量化盲评、四道去重和 Telegram 远端验收 |
| 迁移/灰度/回滚 | 已定义 additive schema、shadow、单 Task canary 和 route fence |
| 开放产品决策 | 无；§18 已冻结为 v1 合同 |

当前结论：`design_status=complete_for_review`、`product_design_complete=true`、`dev_handoff_ready=true`、`implementation_authorized=false`。本文已作为统一引擎的 AI 活群 adapter 正式产品合同；本轮仍未授权业务代码、数据库、迁移、发布或生产变更。

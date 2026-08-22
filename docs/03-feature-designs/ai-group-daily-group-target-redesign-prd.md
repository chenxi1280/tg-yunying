# AI 活群“群日目标 + 全账号必达 + 账号面具内容记忆”重构 PRD

> **2026-08-09 current failure-state supersede：** 本文的群日目标、动态 coverage、内容质量和统一 check-in 业务要求继续有效；但 current `fact_first_v3` 的 ledger-bound quantity ordinal/due、aggregate allocation/assignment、current intent pointer/variation、dirty-clock wake/projector、scoped claim、fleet inventory item+task enrollment+task-day route/lifecycle fence、原子 ledger bootstrap/takeover activate、Gateway call-issued、ledger-level read-model API、存量 preparing→quiescence→readback 接管和失败 UI 以 `ai-group-generation-failure-churn-remediation-prd.md` 为准。本文中“所有开放义务立即执行”不得覆盖主 PRD 2026-08-07 `natural_full_day due_by_now`；“质量失败释放后继续补”只允许复用同一稳定 due unit 且 external basis 改变，禁止立即创建新 Action 身份或重置 3+3；legacy quantity slot/ContentMix 不得恢复为 current 发送真相源。本文原 `product_design_complete` 只描述未冲突的历史业务设计，不是 current dev handoff 状态。

## 1. 文档状态

| 项目 | 内容 |
| --- | --- |
| Intake ID | `intake-2026-07-27-ai-group-daily-group-target-001` |
| 需求级别 | L3：现网长期无法按时按量完成 |
| 设计状态 | `historical_product_design_complete`；current dev handoff、实现状态和 Release Gate 只认上方 failure-state 专项，尚未实现/发布/E4 |
| 适用任务 | `group_ai_chat` |
| 产品目标 | 每个目标群按自然日完成配置总发送量，并保证该任务当日动态必达账号每个至少真实成功发送 1 条 |
| 内容质量目标 | 正常正文绑定发送账号和该账号面具并执行同账号最近 10 天硬去重；统一兜底正文只能是精确 `签到`，计群日数量，账号未覆盖时同时完成 coverage，但不计高质量正文 |
| 保留硬门禁 | 目标群准入、账号登录/可发事实、正常正文账号面具、内容安全与质量、Telegram 真实限制；签到仍经过远端发送门禁 |
| 删除运行门禁 | 日覆盖容量阻断、硬小时目标、AI 活群活动时段禁发、AI 活群本地群日上限/群冷却阻断 |

> **2026-08-08 extra-volume 候选资格补充：** §5.2 第 2 条的“已覆盖 ready 账号”明确指当前任务日 ledger coverage 已 `confirmed`，同时账号在线、Task-scoped 群准入通过且存在 active/usable 面具。缺面具账号的 coverage 签到成功可同时计 1 条群日总量，但不得领取独立 extra-volume 义务；普通正文生成耗尽后的统一签到补量也只能复用上述已覆盖且有 active 面具的账号身份。Planner 必须先过滤完整候选集再按成功数/最久未发/稳定 ID 截取，单个缺面具账号不得饿死后续合格账号。

本文 supersede 以下旧口径：

- 不可变 `frozen_account_count`、冻结后不可缩小的账号清单，以及把历史 `TaskMembershipAdmissionItem` 直接复制到新任务日的行为；
- 暂停、停止或删除任务继续占用 Planner/Generation/Dispatcher 范围、份额或运行投影；
- 把多个 AI 活群任务作为一条串行队列依次排空；
- 仅依靠同群全局 `context_version`，导致一个并发结果提交后其余 direct 结果全部作废；
- GenerationJob 只改 Action 状态而不原子写 owner/lease/`generating`，使多个 worker 重复判断为空闲；
- `per_account_daily_min_messages / per_account_daily_max_messages` 作为运营目标；
- 旧配置按“冻结账号数 × 每账号最低条数”迁移，或任何旧 Task 运行状态迁移；
- `hard_hourly_target_enabled / hourly_min_messages / hard_hourly_strategy`；
- “剩余理论容量不足就停止创建发送 Action”；
- `TgGroup.active_window` 对 `group_ai_chat` 的禁止发送语义；
- `TgGroup.daily_limit / group_cooldown_seconds / legacy_group_slot` 对 `group_ai_chat` 的本地发送阻断语义；
- 同租户所有活群共享 1 小时、7 天、30 天历史内容硬去重；
- 把 `check_in_fallback` 与 `mask_missing_check_in` 设计为两套去重、计数和重试规则；新合同统一为一种 `check_in` 内容来源。

本文只细化 AI 活群群日目标，不单独定义其他任务类型；频道评论、点赞、浏览与纯搜索点击的当前执行节奏统一服从分类恢复/合同闭合 PRD 的“任务专用义务 + 阶段真实资源空闲即执行”，不得从本文件推导其保留旧 Window、速率、静默权重或预扣。旧 `search_join_group` 不在当前合同内。

## 2. 产品决策

1. 运营只配置单个群的 `daily_message_target`。
2. 每个任务、目标群、账号、自然日独立判定覆盖；当前任务的动态必达账号每天至少成功发送 1 条。
3. 默认群日目标等于创建时该任务的当前合格账号数；运行时必达账号数动态变化，不修改用户配置总量。
4. 所有开放义务立即可执行；只要 Planner、Generation 或 Dispatcher 有真实空闲槽位就持续补满，不计算时间节奏、速率、静默权重或任务份额。
5. 未准入或不可发只阻塞对应账号；其他 ready 账号继续。
6. 统一 `签到` 可用于缺面具 coverage 或普通正文生成耗尽后的数量兜底；正文固定、事实链一致，不拆两套规则。
7. 每条 AI 活群正文必须由该账号当前固化的 active 面具参与生成和校验。
8. 历史内容硬去重窗口统一为滚动 10 天，所有权是账号；其他账号说过相同或相似内容，不得硬阻塞当前账号。
9. 只有 Telegram 真实发送成功且有非空 `remote_message_id` 才完成群日总量和账号覆盖。
10. 可自动恢复账号保留本任务义务并自动回流；无合法恢复路径的账号当日放弃，不保留永久残留分母。
11. 所有 running AI 活群任务独立并发；同账号非冲突 RPC 也可并发，adapter 必须按 request identity 隔离 transport。
12. 全系统只允许一个 active AI Provider key；多个模型共享该 key 并独立发起生成。

## 3. 唯一目标合同

### 3.1 运营配置

创建和编辑只暴露一个数量目标，并可选配置 C2 预关注频道：

```text
daily_message_target: int >= 1
group_ai_prejoin_channel_ids: TelegramPublicChannelRef[0..3]
```

含义：该任务的单个目标群在任务时区自然日内需要取得的真实成功消息总数。

`group_ai_prejoin_channel_ids` 必须持久化到 `tasks` 独立 JSON 字段，默认 `[]`。创建和编辑页直接接收公开 `https://t.me/<username>`、`@username` 或公开 username；服务端统一归一化成 username 引用、去重并限制最多 3 个，拒绝私密邀请链接、消息地址和非 Telegram 域名。不得只存 `type_config`、缓存或群级规则。0～3 个频道无依赖时并发关注，全部成功后再进入 join/群管提示阶段。

账号已经加入目标群时也不能跳过这一步：准入 Action 复用或被历史 `already_joined` 跳过后，第一次 fact-first 正文发送前仍必须执行配置频道检查。成功关注写入账号-目标 `configured_channel_follow` 事实，后续 Action 只复用该事实；未全部成功则当前正文保持 pending，不能进入 C2 观察或主互动。

配置频道成功且账号已在群后，以数据库时间和该账号 viewer cursor 开始连续 30 秒观察，并冻结 `surface_kind=target_group_control_stream + surface_peer_id + viewer_account_id + viewer_authorization_id + listener_instance_epoch + listener_policy_version + observed_start_cursor + surface_identity_hash`。30 秒内同一 surface 没有该账号的可信群管提示、cursor 连续且 `observation_gap=false` 时，保存 `observed_end_cursor`，写 `post_follow_visibility(outcome=no_prompt_30s_passed)` 并视为群机器人验证通过；30 秒内出现提示立即按提示执行。网络、Session、listener、surface identity 或 cursor gap 不能当成“没有提示”，必须递增 version 并重建观察区间或按账号不可发送合同放弃；私聊/其他 peer 不驱动 ready。

不再暴露：

```text
per_account_daily_min_messages
per_account_daily_max_messages
hard_hourly_target_enabled
hourly_min_messages
hard_hourly_strategy
```

新任务默认值：

```text
daily_message_target = 创建时当前任务合格账号数
```

若创建时账号范围尚未确定，页面显示当前任务资格预计和可变说明；任务启动后按当前事实建立动态账号范围，不把预计值冻结成不可缩小分母。

### 3.2 全账号覆盖

每个任务、目标群、账号、自然日只有一条覆盖义务：

```text
TaskAccountDailyCoverage.target_count = 1
```

当日实际目标：

```text
current_required_account_count =
  count(task_account_state in {eligible, recovering, completed})

planned_daily_target =
  max(daily_message_target, current_required_account_count)

effective_daily_target = planned_daily_target  # 兼容读模型名

quantity_overflow_count =
  scheduler_oversend_count

target_reduction_overage_count =
  confirmed facts legal under their frozen Gateway target revision
  but above a later reduced current target
```

运营配置小于当前必达账号数时：

- 不拒绝创建或启动；
- 保存运营配置值；
- 页面明确展示“本任务当前必达账号至少 1 条，因此当前最低目标为 N 条”；
- Planner 按实际目标执行。

当账号进入 `abandoned_for_day` 时，`current_required_account_count` 可下调，但 `planned_daily_target` 不低于用户配置值。已确认总量只用于 overflow/审计，不反向抬高计划目标；已完成账号始终保留 `completed` 事实。

每次计划目标变化递增 `planned_target_revision`。不保存 completion ordinal；稳定数量义务就是唯一执行身份。Planner 按当前目标幂等补齐或取消未进 Gateway 的稳定义务；Gateway 前只对当前义务执行单行 `action_bound -> gateway_started` CAS并冻结 target revision/target，不锁任务日账本、不做中央预算预扣。账号后来放弃导致目标下调时，旧 revision 内合法开始的事实只计 `target_reduction_overage_count`，不阻断 E4；下调后逐义务 CAS 取消多余开放义务。

### 3.3 先新建、切 route，再删除旧 Task

运营先按当前确认配置直接创建 `prepared` 新 Task ID，不复制旧目标事实、覆盖、Action、ContentMix、签到、准入、账号范围或完成量。真实 prepared canary 只验证完整 remote fact 链，不计算吞吐、容量、required rate 或预计完成量。activation manifest CAS 唯一 route epoch 后，新 Task从 0运行、旧 Task同时失去 Gateway 权限；随后仅保存旧 Gateway-started/unknown/confirmed 远端副作用的最小防重 tombstone，并物理删除旧 Task 主记录与全部可重建 runtime/config。

若同一自然日切换，新 Task 的 confirmed/gateway_started/unknown 均从 0 开始，旧 Task 成功不抵扣新目标；创建时必须显式确认可能产生同日额外执行。旧 tombstone 只阻止旧 remote mutation identity 重放，不参与新 Task 目标和覆盖。旧 Task删除失败不停止新 Task，但旧 Task始终受 route epoch fence。

## 4. 任务内每日动态账号范围与首日语义

### 4.1 自然日时间边界与动态范围

任务时区每天 00:00 为运行中任务建立不可变的时间边界，但账号范围不冻结：

- `target_date`；
- `task_day_ledger_id`、`period_start_at`、`deadline_at`、`timezone_revision`；
- `configured_message_target`；
- 任务内账号资格初始投影和事实版本。

账号范围唯一键为 `(task_id, group_id, account_id, task_day_ledger_id)`。当日新增合格账号立即加入；可恢复 blocker 进入 `recovering` 并自动复探。Telegram/Session 权威返回 `session_invalid|session_revoked|session_unauthorized|need_relogin|cannot_send|write_forbidden|account_restricted|account_banned` 时立即在该 Task 日进入 `abandoned_for_day` 并释放未进 Gateway 义务，同日不自动复活；目标群解散/删除则终结该 Task 目标，不把账号全局判废。FloodWait/SlowMode 在 retry 早于 deadline 时只是延后，网络 timeout/unknown 不能误判放弃。同一账号在其他任务的状态不随之改写。

账号资格在以下事件后立即刷新：任务启动/恢复、账号登录或健康变化、授权/代理切换、membership/can_send/群管准入事实变化、任务账号选择配置变化、定时 reconciliation。定时对账是补偿机制，不代替事件驱动刷新。

### 4.2 暂停、停止与删除

- 只有主记录存在且 `running` 的新合同 Task 可产生新义务、GenerationJob、claim 和 Action。
- 暂停/停止立即清理未进 Gateway 的当前/未来 coverage、数量 Action、worker claim/lease、ContentMix 绑定和实际槽位；恢复时按当前事实重建。
- 删除任务先 fence lifecycle 并创建唯一 `task_delete_operation_id`，冻结待 tombstone/待删除集合的 count/hash；最小远端 tombstone 写完且 count/hash 校验通过后，物理删除 Task 主记录和可重建 runtime/config。崩溃重放只消费同一 operation item/checkpoint，不能扩大集合。需要再运行时创建新 Task ID/新任务日账本，不恢复原 Task。
- 任何清理以 `task_id` 为精确边界，禁止旧任务账号关系影响新任务或新任务日。

### 4.3 新任务首日

自然日中途新建、启动或大规模换群的任务进入：

```text
daily_fulfillment_phase = admission_warming
```

预热日规则：

- ready 账号立即开始发送，不等待全部账号准入；
- 仍展示完整账号范围、准入缺口和已完成量；
- 不承诺不足 24 小时的预热日达到完整自然日 SLA；
- 下一北京时间自然日 00:00 起进入 `full_day_committed`，按完整日验收。

不得用 `admission_warming` 无限延期：任务连续跨过自然日后仍有准入缺口，必须展示为真实 `admission_blocked`，不能重置预热期。

## 5. 资源空闲即执行

### 5.1 即时物化

系统不计算 `due_by_now`、24 小时权重、静默降量、required rate、任务份额或预扣。所有未完成 coverage 与数量义务创建后立即开放：

```text
remaining_target =
max(0, planned_daily_target - confirmed_count - gateway_started_count - unknown_hold_count)

generation_free = max(0, healthy_generation_slots - generating_count)
interaction_free = max(0, healthy_interaction_slots - executing_interaction_count)
```

Planner、Generation 和 Dispatcher 由义务创建、账号/准入事实变化、Action 终态、lease 释放和 worker 空闲事件唤醒。每轮只按实际空闲槽位 JIT 物化，资源释放后立即补下一条；不得为全天目标预建大量陈旧 Action，也不得等待下一 Window。

### 5.2 选择账号

1. 优先选择当日未覆盖、已准入、在线且可发的账号；有面具走正常生成，无面具且该账号本 Task 日尚未使用签到时可创建统一 `签到`。
2. 当前 ready 未覆盖账号均已有有效 Action 后，若群总量仍欠缺，从当前任务日 coverage 已 `confirmed`、在线、Task-scoped 群准入通过且存在 active/usable 面具的 ready 账号中，按“当天成功数最少优先 + 最久未发优先 + 稳定账号 ID”选择额外消息账号；必须先扫描并过滤完整候选集再截取本轮数量。
3. 额外消息只计群日总量，不创建第二条账号覆盖义务。
4. 质量失败释放该 Action 的内容预约和覆盖预约，以新 variation 重新规划；不得把失败计成功。

### 5.3 多任务独立并发

- 每个 running Task 独立计算剩余总量和当前必达账号，不将 4000/5000/800/800 等多任务作为一条串行队列。
- 每轮先从每个 running Task 领取至多一条 ready 义务，再按 `opened_at, task_id, obligation_id` 填满剩余空闲槽位；不持久化配额、份额、`DispatchReservation` 或 `TaskAllocation`。
- Planner 对所有 Task 建立独立义务；AI Generation 以独立 `GenerationJob` 并发；Dispatcher 对每个 ready Action 独立领取，不等待其他 Task 排空。
- 同一账号可同时为不同任务发起非冲突 Telegram RPC，不建立“任务抢账号”、账号内 task cursor 或全局单 inflight。仅同一远程副作用 key 幂等串行；账号 FloodWait 约束该账号后续 RPC，群 SlowMode 只约束对应 peer，均不改写任务资格。
- 共享 Provider/worker/数据库容量不足时，显示每任务开放义务、实际并发数和安全 blocker；不得用全局串行、冻结分母或静默限量掩盖。

### 5.4 direct 独立提交、生命周期与单 Provider Key

同群 direct 义务可用稳定 `generation_sequence` 审计，但每个 ready 结果独立重检并提交，不等待较早 sequence；reply/强上下文结果继续按目标 revision CAS。GenerationJob 领取时原子写 `generating + owner + lease_epoch`，旧 owner 晚结果不能提交。

数据库只允许一个 active `ai_provider_key_version`；所有模型共享该 key 的总 `max_inflight/RPM/TPM`，仅在 Provider 明确存在模型级限制时再取模型子 token。任一 token 领取失败不得半消费。key 轮换时旧 in-flight 按旧 version 对账，新 job 只读新 active version；0 个或多于 1 个 active key 均显式阻断。任务启停删推进 `task_lifecycle_epoch`，Generation 领取和 Gateway 前均复核当前 running epoch。完整状态、失败、删除重建和 QA 合同见 `task-fulfillment-contract-closure-prd.md` §3、§8、§11–12。

## 6. 门禁重新归类

| 检查项 | 新行为 | 影响范围 |
| --- | --- | --- |
| 剩余日容量估算 | 只展示风险，不停止规划 | 无硬阻断 |
| 硬小时目标 | 删除 | 不再生成或统计 |
| AI 活群活动窗口 | 删除禁发 | 全天可规划和发送 |
| 静默时间/静默权重 | 从 AI 活群执行合同删除 | 不参与规划、领取或发送 |
| 本地群日上限/群冷却 | 对 `group_ai_chat` 删除 | 不影响其他任务类型 |
| 目标群 membership / bot admission | 保留 | 只阻塞对应账号 |
| 账号登录、在线、权限、安全策略 | 保留 | 只阻塞对应账号 |
| 账号面具可用性 | 正常正文保留 | 不满足正常正文时可按统一签到合同兜底；签到不冒充高质量正文 |
| 内容安全、事实、上下文、面具匹配、10 天去重 | 保留 | 只阻塞对应候选/Action |
| Telegram SlowMode/FloodWait/权限结果 | 保留 | 按真实返回延后或失败 |
| `unknown_after_send` | deadline 前保留占位并核验；deadline 后转 `remote_reconcile_only/closed_with_unknown_shortfall` | 禁止自动重发；保留最小 tombstone |

任何账号级 blocker 都不得返回任务级 `PlanAbort`。当全部账号暂时不可执行时，任务显示结构化等待及下一次检查时间，但不伪造完成。

## 7. 账号面具关联的 10 天内容质量

### 7.1 正常正文必须固化账号与面具

除 §7.4 的缺面具覆盖专用 `签到` 外，所有 `group_ai_chat/send_message` 都必须在生成前取得该账号可用 active 面具，并在 Action 与消息记忆中固化：

```text
account_id
account_mask_id
account_mask_lineage_id
account_mask_version
account_mask_contract_version
mask_snapshot_hash
```

其中：

- `account_id` 是内容历史所有权；
- `account_mask_id / lineage_id` 标识该账号面具资产及其连续谱系；
- `account_mask_version` 是本条消息实际使用的不可变版本；
- `mask_snapshot_hash` 用于证明生成和发送前校验使用同一面具快照。

面具在 Action 创建后更新时：

- 已固化有效版本、尚未进入 Gateway 的 Action继续使用固化版本；
- 面具被停用或判为 unusable 时，未进 Gateway 的 Action 显式跳过并重新规划；
- 缺面具 Action 不得生成普通正文或群总量额外消息；
- 缺面具账号只允许创建一条绑定当日 coverage 的精确 `签到`；
- 单账号缺面具不得阻塞其他账号。

### 7.2 10 天硬去重所有权

时间型内容硬门禁统一为滚动 10 × 24 小时：

```text
dedupe_owner = tenant_id + account_id
dedupe_window = [candidate_time - 10 days, candidate_time)
```

同一账号在最近 10 天内出现以下任一情况即拒绝候选：

- 归一化文本完全相同；
- 高相似语义或相同语义簇；
- 同模板壳句只替换少量词；
- 对同一事实锚点、人物或话题重复相同观点；
- 与该账号当前短期立场明显冲突且没有新事实支持。

面具版本更新、回滚或重建不得清空该账号的 10 天内容历史。查询始终覆盖同一 `account_id` 最近 10 天，并携带命中的历史 `account_mask_id/version` 供解释；`lineage_id` 用于分析同一面具谱系的重复密度，不作为绕过去重的分区键。

其他账号的历史内容：

- 不进入当前账号的时间型硬去重查询；
- 不得产生 `duplicate_message` 硬阻断；
- 可作为生成阶段的多样性提示和质量统计，但只能是软提示，不能减少当前账号的履约机会。

这意味着账号 A 说过的内容不会直接封死账号 B；账号 A 自己 10 天内不能重复同类表达。

### 7.3 非时间型质量门

以下检查不受 10 天窗口影响，继续逐候选执行：

- 敏感内容和出站内容安全；
- 真实上下文、引用对象和事实锚点；
- 账号面具的语气、句长、表达习惯和身份方向匹配；
- AI 元话术、模板指令泄露、无意义文本；
- 生成数量、slot 映射、账号映射和输出结构合同。

质量门只允许“通过”或“带原因拒绝”，禁止静默改写后发送。

### 7.4 `签到` 规则

系统只保留一种 `content_source=check_in`，不得再分 `check_in_fallback` 与 `mask_missing_check_in` 两套语义。可用触发原因为 `mask_missing` 或 `normal_generation_exhausted`，但两者使用完全相同的事实、计数、去重和重试合同：

- 正文只能是精确 `签到`；
- 必须绑定自己的数量义务；账号当天尚未覆盖时，同时绑定该账号唯一 `coverage_ledger_id`，已覆盖时只完成数量义务；
- 数据库对 `(task_id,group_id,account_id,task_day_ledger_id)` 建 `content_source=check_in` 的非终态/unknown/confirmed partial unique；同一账号在同一群、同一 Task 日最多真实发送一次 `签到`，不得靠换义务、换触发原因或 pre-accept 重建之外的新 attempt 绕过；
- 幂等身份来自稳定主义务与 `remote_mutation_key`，不另建可递增的 `fallback_attempt_no`；同一义务最多一条非终态 Action；
- 明确 pre-transport/pre-accept 失败时释放同一义务回 `open` 后可重新物化；`unknown_after_send` 保持原义务和 mutation identity 占位，只复探、不创建替代发送；
- 不要求 `account_mask_id/version`；必须保存 `trigger_reason` 与当时 `mask_status`，但面具可用与否不改变签到事实合同；
- 不进入普通正文 10 天精确、语义或模板硬去重，只受主义务唯一性与远端 unknown 防重；
- 仍经过准入、在线、账号安全、出站安全、会话轮换和 Telegram 真实限制；
- 真实成功后计群日总量；绑定 coverage 时同时完成该账号当天 coverage；永远不计高质量 AI 正文、reply 或面具内容指标；
- reply/强引用义务不得降级为签到。正常 direct 义务只有在版本化生成流程明确耗尽且 policy 允许时才可转为签到；不得用签到掩盖 Provider、准入或 transport 故障。
- 当全部可用账号本 Task 日都已使用一次签到，且合法正常正文仍不足以覆盖剩余数量义务时，显示 `content_capacity_gap` 与缺口数；不得重复签到、伪造正文或静默缩小目标。

所有签到失败都不能伪造成功。

### 7.4.1 面具状态与签到触发

`trigger_reason=mask_missing` 只允许账号属于正常运营用途、没有可用 active 面具，且面具资产/生成状态属于以下之一：

```text
missing
queued
generating
retry_wait
manual_required
```

以下状态不得用签到绕过账号用途或身份禁用：

```text
disabled
unusable
identity_invalid
account_usage in {code_receiver, rank_deboost, mismatch}
```

已有 active 面具但新版本正在生成或生成失败时，继续使用旧 active 版本，不属于 `mask_missing`；若正常 direct 生成流程明确耗尽，才可使用 `normal_generation_exhausted`。`mask_status`、生成 item 状态、触发原因和签到资格必须分别保存，不能用一个布尔值混合。

### 7.5 质量失败后的继续履约

`duplicate_message`、`voice_profile_mismatch` 等质量拒绝：

1. 当前候选/Action 以明确质量原因终结；
2. 释放未进 Gateway 的消息记忆预约和覆盖预约；
3. 为同一账号创建新的 `content_variation_key`；
4. 新生成请求必须带入该账号 10 天内命中的禁用语义、当前面具快照和可用新角度；
5. Planner 后续继续补该账号欠额，不降低质量门，也不停止其他账号。

系统必须展示而不是隐藏内容产能：

```text
generated_candidate_count
accepted_candidate_count
quality_acceptance_rate
duplicate_rejection_count
mask_mismatch_count
accounts_exhausted_current_context_count
```

某账号当前上下文暂时没有合法新表达时，只标记该账号 `quality_waiting_context`；出现新上下文、面具更新或禁用内容离开 10 天窗口后重新唤醒。

## 8. 消息记忆数据合同

`AiGroupMessageMemory` 至少保存：

| 字段 | 含义 |
| --- | --- |
| `tenant_id/account_id` | 10 天去重所有者 |
| `task_id/group_id/action_id` | 来源与执行链 |
| `account_mask_id/lineage_id/version/contract_version` | 正常正文使用的面具证据；`check_in` 可为空 |
| `mask_status/content_source/check_in_trigger_reason` | 正常正文为 `generated`；签到统一为 `check_in + mask_missing|normal_generation_exhausted` |
| `mask_generation_status/check_in_eligible` | 面具状态、恢复进度及当时签到资格 |
| `obligation_type/obligation_id/remote_mutation_key` | 正文或签到均绑定稳定主义务与远端防重身份，不保存递增签到 attempt 序号 |
| `mask_snapshot_hash/profile_match_score/profile_match_reason` | 面具匹配证据 |
| `raw_text/normalized_text/text_fingerprint` | 原文和精确去重 |
| `semantic_cluster/template_shell_key/fact_anchor_key/stance` | 语义、模板、事实与立场 |
| `reservation_key/status` | 并发预约与生命周期 |
| `planned_at/gateway_started_at/sent_at/dedupe_expires_at` | 时间边界 |
| `duplicate_reference_id/duplicate_reason` | 命中解释 |

参与 10 天硬去重的状态：

```text
reserved
pending
claiming
executing
unknown_after_send
success
```

上述状态过滤同时要求 `content_source != check_in`；签到只参与主义务/可选 coverage 唯一性和 `unknown_after_send` 防重，不参与普通内容相似度查询。

明确未进入 Gateway 的失败在释放本义务 ContentMix/materialization 绑定后不再阻塞新候选，但记录保留审计。`unknown_after_send` 在 deadline 前继续占位；deadline 后释放执行槽并转远端只对账终态，最小 mutation identity 永久防止重复发送。

在线硬去重数据至少保留到 `dedupe_expires_at`。超过 10 天的内容可留审计归档，但不得继续参与硬去重。

## 9. 日目标数据模型

任务日群目标 `TaskGroupDailyTarget` 保留不可变时间边界和配置目标，账号数改为可重算投影：

| 字段 | 含义 |
| --- | --- |
| `tenant_id/task_id/group_id/target_date` | 唯一日目标 |
| `daily_fulfillment_phase` | `admission_warming/full_day_committed` |
| `scope_refreshed_at/scope_fact_version` | 任务内账号范围最近刷新时间和事实版本 |
| `configured_message_target` | 运营配置值 |
| `current_required_account_count` | 当前 `eligible/recovering/completed` 账号数，可重算 |
| `eligible/recovering/abandoned/completed_account_count` | 任务内动态资格分桶 |
| `planned_target_revision/effective_message_target` | revision 每次目标变化递增；目标为配置值与当前必达数的最大值，不吸收已确认数 |
| `confirmed_message_count` | 远端确认总数缓存 |
| `gateway_started_count/unknown_hold_count/target_reduction_overage_count/scheduler_oversend_count` | 由唯一远端事实异步投影；Gateway 前只做单义务 CAS，不锁账本或预扣；目标下调合法旧事实与真实重复调度分账 |
| `coverage_confirmed_account_count` | 已至少发送 1 条账号数缓存 |
| `open/generating/executing_count` | 当前即时运行投影，只作诊断，不形成速率或时间权重 |

每个账号的动态范围行至少保存 `task_id/group_id/account_id/task_day_ledger_id/state/reason/fact_version/recovery_path/updated_at`。缓存必须可从当前账号、授权、在线、membership、admission、Coverage、Action 和 Attempt 事实重算，不能成为成功真相。

C2 准入使用两层数据：Task 专属 `TaskGroupBotAdmission` 唯一 `(task_id,account_id,group_id)`，只保存当前 policy/version、所引用事实和 ready CAS；Task 独立 `account_group_admission_facts` 不含 `task_id`，四类 `fact_kind` 固定为 `configured_channel_follow|dynamic_channel_follow|requirement_confirmation|post_follow_visibility`。多个 Task 可引用同一仍新鲜远端事实，但各自计算 ready，不能共享 Task 状态。

其中无提示通过必须保存 `observation_started_at/no_prompt_pass_at/observed_start_cursor/observed_end_cursor/observation_gap/surface_kind/surface_peer_id/viewer_account_id/viewer_authorization_id/listener_instance_epoch/listener_policy_version/surface_identity_hash/outcome=no_prompt_30s_passed`；只有数据库时间达到 30 秒、同一 observation/surface version 且零 gap 才能 ready。

C3 物化数据库约束固定为三条：`FulfillmentObligationProjection` 对 `(obligation_type,obligation_id)` 唯一；ContentMix 投影对 `(obligation_type,obligation_id,materialization_version)` 唯一；Action 对同一义务只允许一条非终态记录的 partial unique index。统一签到另有 `(task_id,group_id,account_id,task_day_ledger_id)` partial unique。物化可分短事务恢复，但应用层检查不能替代数据库唯一约束。

## 10. 前端与诊断

创建/编辑页：

- 只显示“该群每天发送总量”；
- 默认等于当前账号数；
- 明示“该任务当日动态必达账号每个至少成功发送 1 条；账号数会随资格事实刷新”；
- 删除每账号最少/最多、硬小时目标；
- 删除 AI 活群静默权重/静默降量配置；
- 内容质量说明为“每个账号按自己的面具生成，并检查该账号最近 10 天内容”。

任务详情分别展示：

- 群日目标：配置、实际、已确认、Gateway-started、unknown、欠额；
- 账号覆盖：当前必达、eligible、recovering、abandoned_for_day、已确认、未覆盖；
- 任务并发：开放义务、Planner/Generation/Dispatcher 实际并发、资源 blocker 和最近进度；
- 日阶段：预热日或完整承诺日；
- 准入、账号、缺面具、质量、Telegram 结果分别统计；
- 内容质量：候选数、接受率、账号级 10 天重复、面具不匹配及代表样例；
- 每条消息可下钻账号、面具版本、重复参照和远端消息 ID。

不再展示硬小时卡片、日容量阻断或跨账号重复阻断。

## 11. 真实完成事实

群总量和账号覆盖只统计：

```text
Action.status = success
ExecutionAttempt.status = success
ExecutionAttempt.remote_message_id 非空
Action.account_id = ExecutionAttempt.account_id
Action 正常正文的 account_mask_version 非空；
或 Action.content_source = check_in 且绑定稳定数量义务，并在账号未覆盖时绑定唯一 coverage_ledger_id
AiGroupMessageMemory.account_id = Action.account_id
正常正文：AiGroupMessageMemory.account_mask_version = Action.account_mask_version
签到：AiGroupMessageMemory.content_source = check_in 且 trigger_reason 合法
```

`pending`、Action 创建、AI 生成成功、入群成功和 `unknown_after_send` 均不计完成。

## 12. 验收标准

### 12.1 产品与新旧 Task 切换

1. 新任务只配置群日总量，默认等于创建时该任务当前合格账号数，运行中账号范围动态刷新。
2. 全部旧 Task 不迁移；prepared 新 Task先按当前确认配置创建，route epoch切换后从 0运行，再为旧 Task保留最小远端 tombstone并物理删除；新 Task 不复制旧目标事实、账号范围、覆盖、签到、Action 或 ContentMix。
3. 同日重建必须显式确认，新 Task 账本从 0 开始；旧成功不抵扣新目标，旧 mutation identity 不重放。
4. 新 Task 覆盖行统一 `target_count=1`。
5. 中途启动显示预热日，下一自然日进入完整承诺日，不能连续重置预热。
6. 新合同 prepared canary 在切 route 前直接执行并取得完整 AI remote fact 链；不做吞吐/容量预测。canary 失败时不得切 route或删除旧 Task，同一 Task 不被新旧 writer 双写。

### 12.2 规划与发送

1. 部分账号未准入或离线时，其他 ready 账号仍持续规划和发送；缺面具但其他条件满足的账号创建覆盖专用 `签到`。
2. 所有开放义务立即可执行；释放任一 worker 槽后立即补下一条，不等待 due-by-now、速率、静默时间或 Window。
3. AI 活群不因 `active_window`、`daily_limit`、`group_cooldown_seconds` 停止。
4. 不产生新的硬小时 Action、速率/静默/份额统计或容量 `PlanAbort`。
5. 同时运行 4000/5000/800/800 四 Task 时，每轮先各取一条 ready 义务，再填满剩余空闲槽位，不得串行排空。
6. 多个 direct GenerationJob 可同时调用同一 active Provider key，后位 ready 不等待前位 sequence；reply/强上下文 CAS 仍生效。
7. 暂停/停止/删除任务不再产生新义务或 claim，未进 Gateway 运行残留被清理，已进 Gateway/unknown 只对账。
8. Telegram 权威 Session 失效、需要重登或账号不可发送时当日立即放弃；群解散时终结目标；网络 unknown 不误判放弃。
9. C2 配置频道完成且已在群后，连续 30 秒无可信提示并且零 observation gap 自动通过；29 秒、期间出现提示或 cursor/listener gap 均不得通过。

### 12.3 内容质量

1. 所有正常 AI 活群正文都有账号和面具版本证据；无面具正文只能是合法统一 `check_in`。
2. 同账号 10 天内精确、语义、模板或同观点重复被拒绝。
3. 不同账号历史内容不会产生跨账号 `duplicate_message` 硬阻断。
4. 面具升级或回滚后，同账号 10 天历史仍参与去重。
5. 统一签到正文只能是精确 `签到`；可计群日数量，账号未覆盖时同时完成 coverage，但同账号同群同 Task 日最多一次，且不计高质量正文或 reply。
6. 质量失败重新生成新 variation，失败 Action 不计覆盖或群总量。
7. 超过 10 天的内容不再参与硬去重。
8. disabled、unusable、identity_invalid 和非普通运营用途不能借签到绕过。
9. 签到明确 pre-accept 失败释放同一主义务后重新物化；`unknown_after_send` 时不能创建替代发送。
10. `check_in_fallback` 与 `mask_missing_check_in` 不再产生新数据；数据库与代码只使用统一 `check_in`。
11. 全部可用账号签到已用尽且正常正文不足时必须显示 `content_capacity_gap`，不得重复签到或下调目标。

### 12.4 完整生产日

只对 `full_day_committed` 的北京时间完整自然日验收。每个运行任务必须满足：

```text
coverage_confirmed_account_count = current_required_account_count
confirmed_message_count >= planned_daily_target
AND scheduler_oversend_count == 0
AND gateway_calls_started_after_first_target_reached == 0
recoverable account incorrectly abandoned count = 0
unrecoverable account left in active required scope count = 0
paused/stopped/deleted task active runtime residue count = 0
deleted task primary row count = 0
running task starvation window count = 0
strict account/action/attempt mismatch = 0
success attempt without remote_message_id = 0
success message without mask evidence or valid check_in evidence = 0
ineligible check_in count = 0
check_in counted as high_quality or reply count = 0
duplicate open/unknown check_in obligation count = 0
duplicate check_in per task/group/account/task-day count = 0
content_capacity_gap hidden while target remains count = 0
cross_account_duplicate_hard_block_count = 0
new hard_hourly Action count = 0
daily_coverage_capacity_insufficient count = 0
active_window AI skip/defer count = 0
```

`target_reduction_overage_count` 必须逐条验证 Gateway 冻结的旧 target revision/target，但不作为失败条件；缺少该证据的超量归入 `scheduler_oversend_count`。

E4 的账号覆盖必达分母必须与 `current_required_account_count` 同口径：`abandoned_for_day` 已由权威账号/Session/权限事实释放当日未进 Gateway 义务，不再计入 required，但仍必须在运行明细中按 blocker 原因展示；`ready/reserved/blocked/unknown/confirmed/pending_admission` 仍属于当前 scope，不能因未完成或程序错误从分母消失。

生成合同程序错误修复部署后，存量 `blocked/generation_contract` 行不会自动假定安全。只允许显式 Task 集合、当前任务日、精确 blocker、旧 Action 已终态且零 Gateway marker 的 preview/hash/CAS/AuditLog 恢复；恢复只把 coverage 重新开放为 ready 并唤醒原 Task，历史 Action/Attempt 不删除、不原地重试，后续仍须新 Action 的真实远端事实完成 coverage。

同时报告准入完成率、内容接受率和按账号拆分的 10 天重复拒绝。全部成立才能标记 `production_fixed`。

## 13. Product Handoff

开发顺序：

1. 以当前专项合同实现；主 PRD、主数据流索引已同步产品合同，项目结构索引最后按真实代码入口更新；
2. 落地群日目标、任务内动态账号范围、自动恢复/当日放弃、预热日和即时物化模型；
3. 将消息记忆硬去重改为账号所有权并固化面具证据；
4. 删除容量、硬小时、活动窗口和本地群槽位阻断；
5. 调整 Planner、Generation、Dispatcher 的多任务资源空闲即执行、direct 独立提交、单 active Provider key、前端和诊断；
6. 运营先直接创建 prepared 新 Task，以显式 allowlist 让其中一个真实 Task直接执行 canary并取得完整 remote fact 链；未通过不得切 route或删除旧 Task；
7. canary 通过后 CAS activation route epoch，使 prepared 新 Task从 0运行并同时 fence旧 Task；随后按精确 manifest 物理删除旧 Task，只留最小远端 tombstone；
8. 走 `master -> release -> GitHub Actions Deploy Production`；
9. 使用完整北京时间自然日完成 E4 验收。

QA 必须验证准入、账号面具和质量仍能阻断对应账号/Action，其他 ready 账号不会被连带停止，且其他任务类型规则未被误删。

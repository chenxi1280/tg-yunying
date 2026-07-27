# AI 活群每日履约收口修复 PRD

## 1. 文档状态

| 项目 | 内容 |
| --- | --- |
| 需求级别 | L3 生产问题修复 |
| 设计状态 | complete（2026-07-27 日覆盖、同群发送领取槽位、群管频道 follow 与账号级 callback 来源隔离、实时确认按钮来源与账号归属交叉修订） |
| 变更状态 | 先前 release 已完成群管控制提示分类、恢复与并发准入修复；本次按 Release Gate 修复六条已由生产证据确认的日履约链路：未进入 Telegram Gateway 的 overdue Action 被误标为远端未知、跨 Window claim 账本计数漂移使 Recovery 因 `dispatch claim ledger underflow` 回滚、Dispatcher 加锁取行时用通用静态时间排序覆盖已分配的 `DispatchClaimPlan` 同优先级候选顺序、多个 worker 在 Gateway 前同时领取同一 `legacy_group_slot` 群的正文 Action、已审计可信的全群频道规则因没有单一账号归属而未创建 pre-send follow，以及 listener 入库的当前 confirmation callback 已在 Telegram 侧变更或被 20 条监听窗口挤出。既有 target admission、硬小时、搜索 membership/source、任务优先级、频道评论和公平优先级仍是计划的必需前缀。真实 Telegram 结果与完整自然日验收仍须以 Action + Attempt + remote_message_id 和日账本证明；不得以 worker 存活、Action 创建或 ledger 释放代替。 |
| 适用任务 | account_coverage_mode=all_accounts_daily 的 group_ai_chat |
| 统计时区 | Asia/Shanghai |
| 关联线上证据 | 2026-07-25 完整日账本与 2026-07-26 生产只读取证 |

本专项细化并补正以下文档的当前缺口：

- ai-group-all-accounts-daily-coverage-prd.md 的每日账本、失败回补、容量和验收口径；
- ai-group-dispatcher-ai-generation-transaction-design.md 的批量输出完整性、质量拒绝与生成审计口径；
- ai-group-hard-hourly-target-prd.md 的硬小时与日覆盖同时存在时的规划入口；
- ai-group-send-continuity-and-terminal-targets-prd.md 的明确失败再规划语义。

目标引用生命周期、target_ref_invalid、group_dissolved、群管准入、账号轮换、Telegram 限流和 unknown_after_send 继续以原专项为准；本文件不改变这些安全边界。

## 2. 生产问题与已证事实

2026-07-25 的远端确认日账本如下。分子只统计成功 Action、成功 ExecutionAttempt 和非空 remote_message_id 对应同一覆盖行的事实。

| 任务 | 远端确认 / 冻结分母 | 未完成事实 |
| --- | ---: | --- |
| 天津 | 790 / 797 | 2 条 cannot_send，5 条 ready |
| 石家庄 | 787 / 797 | 10 条 ready |
| 郑州师范 | 674 / 797 | 121 条 cannot_send，1 条 membership_permission_denied，1 条 ready |
| 郑州楼凤 | 750 / 797 | 47 条 ready |

已确认的根因链：

1. 多个任务持续出现 duplicate_message；原始文本为 AI 活群生成内容重复：1h_similar 与 AI 活群生成内容重复：7d_semantic。此类 Action 是质量终态，原 Action 不会自动重试。
2. 郑州师范的 122 条准入阻塞中，121 条原始原因是账号在目标群不可发言，另 1 条是账号无法加入/访问目标：已提交入群申请，等待审批后才能发言。天津另有 2 条账号在目标群不可发言。
3. AI 批量生成出现 ai_generation_output_count_mismatch 与 ai_generation_slot_mapping_mismatch；失败发生在 Telegram Gateway 之前。
4. 当日剩余 63 条 ready 覆盖行中，11 条没有关联 send_message Action，52 条虽关联 Action 但未得到远端确认。现有 open Action 分支会在 hard_hourly 没有缺口时跳过仍有 daily coverage debt 的规划。
5. 2026-07-27 的四个运行中 AI 活群任务同时出现 `coverage_action_overdue`；抽样的 pending Action 没有 ExecutionAttempt、也没有 `gateway_call_started_at`，却被读模型写成 `unknown + remote_reconcile`。这不是远端未知，而是 Dispatcher 积压；若继续以 unknown 占位，终态 `execution_timeout` 的旧 reservation 又不会被 Recovery 扫描释放。
6. Recovery worker 的真实堆栈持续报 `dispatch claim ledger underflow`。过期的 pre-Gateway generation Action 恢复为 pending 后，旧 Window/Allocation 的 active counter 已为零而 Scope 仍有 active；释放抛错导致事务回滚、Recovery claim 留存、stale executing Action 长期占用全局 20 个领取槽位。四个任务实际 Gateway-start 间隔约 46–74 秒，低于每小时 120 条硬目标所需的约 30 秒一条；群冷却 15 秒本身不是该吞吐缺口的根因。
7. 第一轮修复发布后，pre-Gateway overdue 与 ledger underflow 已能收口，且存在带 `remote_message_id` 的新成功发送；但 `DispatchClaimPlan` 已按 reservation、allocation epoch 和公平 cursor 选出候选，`_locked_claim_plan_candidates` 却再次按通用 `claim_action_ordering` 的旧 `scheduled_at` / `created_at` 队列排序。历史大积压使同优先级的旧同群 Action 反复先被领取、随后命中真实 `GroupSendSlotBlock` 的群慢速模式并延后，已分配但未领取的 shard 份额无法转给计划中的其他目标。根因修复必须保留既有类别、任务和公平优先级，再以计划位置取代旧时间排序；发布后抽样实际间隔仍约 35–104 秒，未达到 120 条/小时所需的约 30 秒一条。该问题不能通过放宽群冷却或切换 `send_limit_mode` 掩盖。
8. 计划顺序修复后，多个 Dispatcher worker 仍可在同一事务窗口领取同一 `legacy_group_slot` 群的多个 pending `send_message`；群行锁和 `GroupSendSlotBlock` 只在内容已准备、Gateway 即将调用时才取得。首条进入 Gateway 后，其余已领取 Action 才被群冷却延后，造成生成、worker 和 claim 份额浪费，实测远端间隔仍高于 120 条/小时所需的约 30 秒。该问题不是群冷却过严，而是同群“在途发送”和下一合法发送时刻没有进入领取边界。
9. 生产群管控制上下文已出现带精确公开频道 URL 和 confirmation callback 的广播提示，但 bot 的 Telegram 角色为 `unknown`。现有安全合同因此正确拒绝其自动信任；即使运营随后为同一 peer 建立审计 policy，当前 listener 在多个 waiting admission 且无收件人时直接返回，不能在正文前为每个任务账号创建频道 follow。结果是已有正文在群管处被拦截，或 admission 保持 `group_bot_rule_unattributed`。根因不是“频道无需关注”，而是缺少“已审计可信、无明确收件人的全群规则”的逐账号展开路径。
10. 2026-07-27 生产抽样中，`GroupContextMessage` 的控制消息在 Telegram 发送后才由 listener 写入；例如远端发送于 14:05:20、上下文入库于 14:05:33，而 callback 在 14:06 后执行时已返回 `group_bot_confirmation_button_mismatch`。首次实时复核发布后已观察到 Action 从 `3660525` 换绑到 `3660532` 并成功 callback，但同一群 `listener_context_limit=20` 且消息密集，另有 Action 只能写 `group_bot_confirmation_source_stale`：当前窗口未包含匹配提示。精确 source 读取发布后，生产又确认群管会在同一群按账号分别发送提示；若当前窗口只按 peer、频道集合和按钮形态选最新一条，不同账号的 Action 会被一起换绑到同一条提示（抽样为 `3660914`），并产生真实 `group_bot_confirmation_button_failed`。因此 `GroupBotAdmission.source_message_id` 的历史 source/version 校验虽能淘汰旧 backlog，却不能保证有限窗口的候选属于即将点击的账号。根因是 click 边界缺少“新窗口提示必须明确归属绑定账号”的复核；精确 source 仍是该账号的可靠回退来源。

因此，本需求不是提高一个统计数字，而是确保每个覆盖义务都有可解释、可恢复且不越过安全门的下一步。

## 3. 产品目标与非目标

### 3.1 目标

1. 全部账号日覆盖的冻结分母永不因 cannot_send、membership_permission_denied、质量失败或待审批而缩小。
2. 对每条未完成义务明确显示：可继续履约、外部阻塞、待远端核验或配置不可达；不得只有模糊的 ready。
3. 内容重复必须驱动一次新的、可审计的生成意图，而不是反复生成同一语义簇或绕过质量门。
4. 批量输出数量或 slot 映射不可信时，整批不得进入 Gateway，且所有受影响预约必须释放；只有受控的生成契约修复完成后才可重新规划。
5. 日覆盖 debt 必须独立于 hard_hourly deficit 计算。硬小时达标不能成为忽略当日未完成覆盖的理由。
6. 操作人员可以定位账号权限、入群审批、质量拒绝、生成契约、调度未建单五类问题，并看到正确处理入口。
7. 群管机器人无频道引用、观察未闭合和策略未决必须分别显示；它们不能被笼统写成“需要关注频道”，也不能因 UI/历史 result 的陈旧错误字段掩盖已远端成功的覆盖事实。
8. 当群管频道/“我已加入”只存在于内联按钮时，日覆盖链路必须以原消息、可信 peer、精确按钮和真实回执为准；历史已入库提示重新观测到同一 peer 的按钮时只补齐安全摘要；可信 peer 的普通推广内容也不得进入控制状态机、创建频道 follow 或把等待账号写成未归属，且同群新 admission 必须串行，避免把一条面向单账号的提示误套用给当天多个覆盖账号。带明确收件人但不匹配当前账号的群管提示不得降级归属给唯一 waiting account；仅当 active source-bound policy 后，同 peer 的两条不同来源提示重复给出完全相同的频道集合和确认 callback 形态时，才可作为标准化规则按当前 scope 展开。因 `group_bot_control_prompt_unverified` 暂停的旧 follow 事实不阻塞新世代的有效频道集合；只有显式 restart，或已审计规则重新观察到不同 source message 且当前频道引用仍一致时，才能重建同一频道 Action，日覆盖分母和未完成 blocker 不得因此缩小。
9. `ExecutionAttempt.gateway_call_started_at` 是唯一的 Telegram 远端不确定性边界：它为空的 overdue Action 必须显示为 Dispatcher 积压并保留 reservation；它非空的 overdue Action 才可进入 `unknown + remote_reconcile`，两者都不得自动重复建单。
10. Dispatcher Scope、Window、Allocation 的 active counter 是可重算投影，不是独立事实源。任何终态释放必须以同 scope 内仍为 `executing + dispatch_claim_active` 的 Action 重新核对 exact binding；计数漂移必须留审计证据，但不得让单条旧 Action 阻塞整批 Recovery。
11. `DispatchClaimPlan.candidate_action_ids` 是当前 Window 已获 allocation 和公平仲裁后的同优先级领取顺序。行锁阶段必须先保留既有 target admission、硬小时、搜索 membership/source、任务优先级、频道评论和公平优先级，再在同一优先级内按该序列跳过已不符合条件或被其他 worker 锁住的行；不得再用旧 `scheduled_at` / `created_at` 队列改写顺序。
12. 修复调度顺序不得改变 `legacy_group_slot` 的群日限额、15 秒群冷却、账号容量、准入、内容质量或 `unknown_after_send` 规则；`account_only` 仍只能由运营显式 canary 切换，不能作为本问题的默认处理。
13. 同一 `legacy_group_slot` 群在 Dispatcher 层同一时刻至多允许一条正文 Action 处于 `claiming/executing`；Gateway 首次取得真实发送槽位时必须持久记录该群下一合法领取时刻。其他目标和 `account_only` 群不得被这一群的冷却或在途 Action 挤占。
14. 广播式“先关注频道才能发言”只可在 exact bot peer 已是管理员、或由 `targets.manage` 对同群同 peer 建立 active `explicit_bot_confirmation` / `follow_sufficient` policy 后，按当前任务 scope 的既有 admission 逐账号创建精确 follow/callback Action。带明确但不匹配收件人的提示默认仍不得展开；仅 active policy 后同 peer 的两条不同来源提示重复给出完全相同的频道集合和确认 callback 形态时，可作为标准化规则展开。未知 peer、普通推广和历史无来源消息仍不得批量展开。
15. 每次群管 confirmation callback 在 Telegram click 前，必须使用该 Action 绑定账号先按当前 `source_message_id` 精确读取 Telegram 消息（不受 `listener_context_limit` 限制），再读取最新群窗口寻找更晚的可信控制提示；只接受同一 trusted bot peer、当前 admission 的精确频道集合和 callback 形态均匹配的控制消息。窗口中与原 source 不同的候选还必须带明确收件人，且按现有 username/display 归属规则唯一匹配该 Action 绑定账号；无收件人或归属其他账号的窗口消息不得覆盖该账号 source。若找到这样的同账号更晚来源，优先换绑；否则精确来源仍有效时必须继续使用。将安全按钮摘要写入 `GroupContextMessage` 审计后，只原子换绑该 Action 与该 admission 的 source/row/col/text 再点击。两个读取都未给出匹配来源、读取失败或 Telegram 在读取后再次变更按钮时，Action 必须显式写 `group_bot_confirmation_live_fetch_failed` 或 `group_bot_confirmation_source_stale` 并在 15 秒后重试；不得调用旧 callback、终态失败或伪造确认成功。

### 3.2 非目标

- 不降低 1h_similar、7d_semantic、内容政策、账号面具、群管准入、轮换、FloodWait 或账号容量门槛。
- 不把 签到、模板、静态文本或 Action 创建成功当作覆盖成功。
- 不自动把 cannot_send、membership_permission_denied、target_ref_invalid 或 admission_abandoned 从冻结分母删除。
- 不对 unknown_after_send 重发，也不以超时推定成功或失败。
- 不把供应商输出异常直接归因于某个模型；供应商原始响应未持久化时，只能报告 generation contract 失败。
- 不为提高吞吐静默改为 `account_only`、缩短群冷却，或把群慢速模式写成成功。

## 4. 核心事实、状态与完成口径

### 4.1 每日履约读模型

每个运行任务每天必须生成并展示 daily_fulfillment 摘要：

~~~text
frozen_denominator_count
confirmed_count
ready_count
reserved_or_sending_count
unknown_hold_count
blocked_count
overdue_open_count
full_shortfall_count
valid_future_open_cover_count
ready_to_plan_count
blocked_shortfall_count
blocker_counts
sendable_capacity_count
daily_outcome
next_decision_at
~~~

daily_outcome 只能为：

| 状态 | 含义 |
| --- | --- |
| feasible | 当前没有确定终态阻塞，且已知账号与时间容量足以继续尝试完成全部冻结义务 |
| at_risk | 尚有未完成义务，但没有已证硬阻塞；需要等待质量、调度、容量窗口或远端结果 |
| blocked | 存在确定阻塞，或现有已知容量不足以完成冻结分母 |
| met | frozen_denominator_count 与 confirmed_count 相等，且没有未收口 unknown |

daily_outcome 是任务当日履约状态，不替代 Task 生命周期。Task 可以保持 running，同时当日状态为 blocked；它绝不能显示为完成。

### 4.2 覆盖行状态

继续使用 TaskAccountDailyCoverage 的 pending_admission、admission_running、ready、reserved、sending、unknown、confirmed、blocked 状态。新增以下强制字段语义，而非新增一个会掩盖真实状态的泛化状态：

| 字段 | 要求 |
| --- | --- |
| blocker_code | 最近一次阻断的原始码，例如 duplicate_message、cannot_send、membership_permission_denied、ai_generation_output_count_mismatch |
| blocker_stage | admission、planning、generation_contract、quality、dispatcher、gateway、remote_reconcile |
| next_decision_at | 允许 Planner 再次决定该义务的最早时间；不是伪造成功或强制重发时间 |
| last_action_id | 最近关联 Action，无 Action 时必须显式为空 |
| reservation_token | 仅限同一 Planner 短事务中“已 CAS 预约、Action 尚未插入”的临时所有权 token；不得伪装为 Action ID，Action flush 后必须绑定 `reserved_action_id` 并清空 token |
| recovery_path | replan_with_new_variation、generation_contract_repair、permission_recheck、manual_approval、dispatcher_recheck、remote_reconcile、target_reference_repair 之一 |

completed 仍只在同一覆盖行关联成功 Action、成功 ExecutionAttempt 和非空 remote_message_id 后写入。

### 4.3 持久化与审计合同

本专项新增的事实不能只存在于 `Task.stats` 或某一次 Action 的可覆盖 JSON 中。为使日结论、分页详情和重启后的 Planner 使用同一事实源，开发必须新增以下持久化结构及迁移：

| 结构 | 最小字段与约束 | 用途 |
| --- | --- | --- |
| `TaskAccountDailyCoverage` 扩展列 | `blocker_stage`、`next_decision_at`、`recovery_path`、`last_action_id`、`reservation_token` | 当前覆盖义务的可查询状态；`last_success_action_id` 和 `reserved_action_id` 不替代最近处理 Action；未插入 Action 的预约只可由 token 表示 |
| `AiCoverageVariationIntent` | `tenant_id`、`coverage_ledger_id`、`action_id`、`content_variation_key`、`context_version`、`intent_snapshot_hash`、`outcome`；唯一约束 `(coverage_ledger_id, content_variation_key)` | 保证同一覆盖义务不会再次使用相同变体，并保留质量拒绝的变体摘要 |
| `TaskDailyFulfillmentDecision` | `tenant_id`、`task_id`、`coverage_date`、`decided_at`、`full_shortfall_count`、`valid_future_open_cover_count`、`unknown_hold_count`、`ready_to_plan_count`、`blocked_shortfall_count`、`required_new`、`reason`、`next_decision_at` | 追加式记录每一次规划或跳过决定；任务 stats 只能缓存最新摘要 |
| `AiGenerationContractAudit` | `generation_attempt_id`、`request_id`、`provider_id`、`model_id`、`prompt_contract_version`、`parser_version`、`expected_slot_count`、`received_slot_count`、slot/sequence 摘要、`error_code`、受限响应摘要 | 一批一次的生成合同审计；敏感原始响应加密并按受限权限读取 |

`content_variation_key` 是不可变意图标识，不等价于“生成文本一定不重复”。Planner 先以 `reservation_token` CAS 锁定 coverage 行，再持久化 `action_id=null` 的 `AiCoverageVariationIntent`；随后插入并 flush Action，才在同一短事务把 intent.action_id 与 coverage.reserved_action_id 绑定到该真实 Action 并清空 token。任一步重复或失败都只释放同一 token 的预约。质量门仍以真实文本指纹和语义簇作最终判断。所有写入均以 coverage 行和 Action 的 compare-and-swap 条件保护，不能由后到的重试覆盖先前审计。

## 5. 修复设计

### 5.1 权限和准入阻塞：保留分母、给出可执行处理路径

1. cannot_send、membership_permission_denied、join_request_pending、target_ref_invalid 与群管准入未完成必须保留在冻结分母中。
   - `required_channel_follow_pending` 只表示已由可信 bot 从正文或同一消息的精确 URL 按钮记录广播频道引用；`group_bot_policy_unresolved` 表示观察到期但没有可用的 `not_required` policy；`observation_stale` 表示缺少或截断观察证据。三者均不能计确认完成，也不能互相替换文案。callback click 成功仍只是等待 bot confirmation，不能写 confirmed。
   - `group_bot_admission_window_busy` 是 admission 串行化的可恢复计划等待，不是 Telegram 发送成功、也不是 permission blocker；它不改变冻结分母。该群已有 ready 账号仍可继续补当日欠额。
   - 观察中的账号不能阻止同一日其他 `can_send=true` 且 admission ready 账号继续规划；全量分母、欠额和 blocker 仍完整保留。
2. can_send=false 的覆盖行不得创建正文 send_message；系统只允许创建对应的准入、权限复检或运营可见的人工处理动作。
3. membership_permission_denied 与等待审批必须显示等待审批后才能发言，不能把申请已提交写成 membership_observed 或覆盖成功。
4. 当同一任务日存在确定权限阻塞时，daily_outcome=blocked，并展示理论可完成上限：

~~~text
maximum_confirmable_count = frozen_denominator_count - terminal_permission_blocked_count
~~~

5. 权限变化、管理员邀请完成或目标成员关系复检成功后，原 blocker 行才可转为 ready，并在同一事务重写 targeted_at 与 next_decision_at；不得因旧日游标已经推进而永久停在 ready。
6. 青岛师范学院等 target_ref_invalid 继续走目标引用修复专用流程。不得因为日目标未完成把它恢复为 active 或写成 group_dissolved。

### 5.1.1 已审计群管频道规则的 pre-send 展开

1. listener 在来源过滤、精确控制提示识别后，若文本没有明确收件人、含精确公开频道 URL 且多个 waiting admission 无法归属，只能在来源为管理员 bot 或存在同 group + peer 的 active source-bound policy 时进入全群规则路径。明确收件人不匹配时，只有同 peer 已在当前 listener 上下文中提供另一条不同 source message、且两条消息的精确频道集合与确认 callback 形态相同，才可作为标准化规则进入该路径。
2. 候选限于运行中 `group_ai_chat` 的持久账号 scope 内、同群且未终态的现有 `GroupBotAdmission`；不同 trusted peer、没有运行任务绑定、`blocked`、`abandoned` 的 admission 一律跳过。不得仅因账户在 `TgGroupAccount` 中存在而伪造 admission 或扩大任务分母。
3. 无明确收件人的可信全群提示可为每个候选创建自己的 `group_bot_channel_follow` 和必要的精确 callback Action。由两条显式收件人提示识别出的标准化规则只证明频道集合属于群级规则：它只能逐账号写 required-channel rows 并创建 follow Action，不得把该个人提示写入其他 admission 的 `source_message_id`，不得创建、替换或重启其他账号的 callback。已有明确归属账号的 source/callback 必须保留；历史 `trusted_repeatable_recipient_rule` 污染的 source 必须清空，其开放 callback 写 `group_bot_confirmation_superseded`，等待该账号自己的明确提示后再规划 callback。
4. 唯一保留的 callback 必须精确匹配 `GroupBotAdmission.source_message_id` 当前指向的账号级控制消息，不能仅按最早创建时间保留；同 peer、明确归属同账号的有效新提示更新该 source 后，Planner 必须把旧 source 的 pending callback 标记 `group_bot_confirmation_superseded` 并创建新 source 的精确 Action。Dispatcher 的 claim 确认和 Gateway 前复检也必须先执行这一 source/version 判定，再执行账号 usage/capacity/shard policy；旧 source 不得因 `global_account_policy` 延后而遗留为开放 Action、不得领取运行资源或调用 Gateway。
5. 完成 follow 后，Dispatcher 必须以该 callback 的绑定账号先精确读取 source message，再读取最新群窗口；精确来源用于避免高频消息挤出 20 条监听窗口时误判 stale。窗口中的不同 source 只有带明确收件人并唯一匹配绑定账号时才可作为更晚来源覆盖；其他账号或无收件人提示只能保留上下文，不能换绑或点击。两个读取均复核 exact trusted peer、当前 required_channel_refs 和 callback 按钮；将实时安全摘要持久化后，只换绑该 Action 与 admission 的当前 source/按钮再调用 Telegram。读取不到匹配按钮或 Gateway 报 `group_bot_confirmation_button_mismatch` 时，写明确 stale/fetch blocker 并在 15 秒后重试，不调用旧 callback，也不终态化为 `group_bot_confirmation_button_failed`。follow 和 callback 成功都不直接写 ready；正文 `send_message` 仍在 admission gate 之后。
6. policy 只是受限信任根，不是历史消息重放开关。策略生效后必须由 listener 再观察到同 peer 的有效控制事件；没有新证据时保留 blocker，并在任务详情显示 `group_bot_policy_unresolved` / `group_bot_rule_unattributed` 的真实区别。
7. 存量 `group_bot_control_prompt_unverified` follow 只可在本路径收到不同 source message 且 channel_ref 仍在当前精确集合时原地 rearm；旧 Action 保留审计，其他 blocked follow 不得批量复活。

### 5.1.2 存量任务账号准入补齐与关注后真实可见性探测

1. `group_bot_admission_required=true` 的运行中 `group_ai_chat`，其持久任务 scope 内账号在任何正文进入生成或 Telegram Gateway 前都必须存在同 tenant + group + account 的 `GroupBotAdmission`。缺行时 Dispatcher 只能从当前 Action 的 task/account/group 和持久 membership item 创建 admission，并把正文延后；不得继续返回 `legacy_send_until_reviewed`，不得把 Telegram 返回 message_id 直接计入日覆盖。
2. admission 补齐使用当前 listener cursor 作为观察基线，不伪造已确认；随后由 listener 的新控制事件或仍在当前窗口的已审计规则创建频道 follow。准入 scope 由显式 `group_bot_admission_required=true`、该账号的持久 membership item 或既有 `GroupBotAdmission` 任一事实确认；无运行任务绑定、无上述持久 scope 事实或 `group_bot_admission_required=false` 不在本自动补齐路径，避免历史裸配置测试/任务被无关门禁抢占原业务校验。
3. 显式确认策略下，精确频道 follow 全部成功但没有账号级 callback source 时，允许该 admission/version 恰好一条正文进入 `post_follow_visibility_probe`。该正文必须创建 `PendingVisibilityCredit`，Action 先写 `unknown_after_send`，不得先计 hard-hourly 或 daily success；同 admission 的其他正文保持 pending。
4. probe 的真实 message_id 在可见性窗口后仍可由同账号读取时，才把 admission 写 `group_bot_admission_ready`、`post_send_visibility_state=visible_confirmed` 并按原账本幂等计成功。消息不可见、群管拦截或 Gateway 明确权限失败时写 `post_send_intercepted`，不计成功；listener 若取得该账号明确提示，可继续走账号级 callback。
5. 标准化显式收件人规则重复观测时，若当前频道 follow 已全部成功，不得把 admission 从 `awaiting_group_bot_confirmation` / `post_follow_visibility_probe` 重置为 `required_channel_follow_pending`；它只更新群级频道证据。
6. `PendingVisibilityCredit.created_at` 从 SQLite、PostgreSQL 或生产连接返回时，恢复扫描必须先归一到统一的北京时间墙上时钟再计算 hold age；禁止 naive/aware 直接相减中断全部任务的 recovery cycle，也不得因时区转换把超时 hold 当成功。

### 5.2 内容多样性和重复质量失败

每一个待生成的非引用或引用 slot 必须拥有不可变 content_variation_key。它由任务、目标引用 epoch、北京时间日期、账号、Cycle、话题方向、讨论老师、行为类型、引用身份和一个新鲜上下文版本派生；只保存摘要或哈希，不把完整敏感上下文写入公共 stats。

生成和质量处理规则：

1. Dispatcher 生成前读取该目标在 1 小时和 7 天窗口内已接受文本的指纹、语义簇，以及本任务当天已被质量拒绝的 variation 摘要。
2. 出现 duplicate_message 时，当前 Action 以终态质量失败收口，写入原始原因、重复窗口、内容指纹摘要、语义簇、content_variation_key 和质量阶段；释放自身 coverage reservation。
3. 同一 Action 不得原地改写文本或再次调用 AI。下一次只能由 Planner 创建新的 Action，且新 Action 的 content_variation_key 必须不同，并使用更晚的上下文版本或不同的已配置话题、老师、行为类型组合。
4. 质量拒绝后的覆盖行回到 ready，blocker_code 保留 duplicate_message，recovery_path=replan_with_new_variation。若没有新的合法 variation 或可用上下文，则保持 at_risk 并记录原因，不制造模板补量。
5. 签到仍是既有三层生成全部失败后的唯一确定性兜底，但它必须通过同一重复、签到配额、轮换、准入和出站门。若签到命中 duplicate_message 或 check_in_repeat，当前 Action 失败并释放预约；不得再次用签到重试或将其计为成功。
6. 任务详情必须按 1h_similar、7d_semantic、semantic_cluster、check_in_repeat 分开展示质量拒绝数、受影响覆盖义务数、最近 variation 摘要和下一次可决策时间。

### 5.3 批量 AI 输出契约失败

批量输出的 slot_id、account_id、coverage_ledger_id、序号和数量必须与 Planner 固定的批次快照完全一一对应。

当出现 ai_generation_output_count_mismatch、ai_generation_slot_mapping_mismatch、ai_generation_output_sequence_duplicate、ai_generation_output_sequence_mismatch、ai_generation_reply_sequence_mismatch 或 ai_generation_reply_sequence_unexpected 时：

1. 整批不得部分进入 ai_generation_status=ready，也不得调用 Telegram Gateway；同一 `generation_attempt_id` 的所有 slot 必须一起终态化。
2. `AiGenerationContractAudit` 必须写入 generation_attempt_id、request_id、provider/model、prompt_contract_version、parser_version、expected/received slot 数量、slot 与 sequence 摘要、原始错误码和受限响应摘要。这样才能区分 provider 输出、解析器和固定 slot 快照哪一层不一致；未取到该证据时只能写 `generation_contract_evidence_missing`，不能猜测模型根因。
3. 每条受影响 coverage reservation 在同一短事务释放，但覆盖行转为 `blocked`，`blocker_stage=generation_contract`，`recovery_path=generation_contract_repair`。它不是内容质量失败，严禁标记为 `replan_with_new_variation`。
4. 只有出现以下明确事件之一，覆盖行才可从该 blocker 回到 ready：已批准的 provider / prompt-contract / parser 版本变更，或运营人员在受限界面确认合同已修复并留下审计理由。不得用隐式模型降级、换话题、模板或签到绕过该 blocker。
5. 不能证明 slot 归属的任何候选文本不得写入消息记忆、不得关联账号、不得作为后续重复判断基线。
6. 原始 provider 响应只允许加密保存并受单独权限与保留期控制；页面、普通日志和 `Action.result` 只能展示脱敏摘要。

### 5.4 日覆盖债务与硬小时的独立规划

当前问题来自把 hard_hourly 是否仍需规划作为 all_accounts_daily 是否允许绕过 open Action 门禁的前提。新规则如下：

~~~text
full_shortfall = frozen_denominator_count - confirmed_count
valid_future_open_cover = state=reserved / sending 且同一 coverage 行关联有效 future Action 的数量
unknown_hold = state=unknown 的未确认覆盖行数量
blocked_shortfall = state=blocked 的未确认覆盖行数量
ready_to_plan = state=ready 且通过当前准入、权限、时间与账号容量门的覆盖行数量
required_new = ready_to_plan

daily_planning_required = required_new > 0
hard_hourly_planning_required = hard_hourly_required_new > 0
~~~

`ready`、`reserved/sending`、`unknown` 和 `blocked` 是互斥状态。`valid_future_open_cover` 只能来自 `reserved/sending` 行，绝不能再从 `ready_to_plan` 扣减；若查询到 ready 行仍关联有效 future Action，必须先在短事务把该行纠正为 reserved/sending 或记录数据不一致 blocker，不能用算术抵消掩盖双状态。`full_shortfall` 用于履约结论；`required_new` 只用于新建可发送的覆盖 Action，二者不得互相替代。

1. daily_planning_required 不得依赖 hard_hourly_planning_required。
2. 同任务存在 open Action 时，Planner 必须分别计算 future_open、overdue_open、valid_future_open_cover、unknown_hold、blocked_shortfall 与 required_new。`gateway_call_started_at is null` 的 overdue open 维持 `reserved + dispatcher_lag + dispatcher_recheck`，不抵扣 future_open、不得新建重复 Action，也不得伪装为 remote unknown；只有已跨越 Gateway 边界的 overdue open 才进入 `unknown + coverage_action_overdue + remote_reconcile`。
3. hard_hourly 已经达标、缺口为零或下一小时检查未到，都不能让 required_new 被跳过。
4. 全局 pending 上限、任务 pending 上限或 Planner 无可用处理槽时，不得静默跳过 daily debt，也不得绕过既有容量门。必须写 `planner_capacity_insufficient`、当前 backlog 快照和 next_decision_at；该状态使 daily_outcome 至少为 at_risk，不能显示 feasible。
5. 每次规划或未建单必须追加 `TaskDailyFulfillmentDecision`，至少含 full_shortfall、valid_future_open_cover、unknown_hold、blocked_shortfall、ready_to_plan、required_new、hard_hourly_required_new、选择或跳过原因、next_decision_at。不能只靠 last_error 推测。
6. required_new 大于零且可发账号存在时，next_decision_at 必须是 daily_coverage_next_check_at；若容量、权限、质量、生成合同、Planner backlog 或未知结果阻塞，也必须写明确 blocker 和重新检查条件。
7. 该规则只允许为 `state=ready` 的义务创建新 Action；已处于 reserved/sending/unknown 的义务禁止再建单，不能因为日债务存在无限堆积 Action。

### 5.4.1 Dispatcher claim 账本与 stale Recovery 收口

1. `DispatchClaimScope.active_claim_count`、`DispatchClaimWindow.active_claim_count` 和 `DispatchClaimShardAllocation.active_claim_count` 都是同 scope 中 `status=executing && result.dispatch_claim_active=true` 的投影。释放时必须锁定原 binding 后按仍在执行的 Action 重算 Scope、该 Window 及全部 allocation，再把当前 Action 标为 inactive；不得仅凭某个旧 Window 的零计数抛出 underflow。
2. 如果重算前后的计数不符合“当前终态 Action 释放前应多一条”的关系，Action.result 必须追加 `dispatch_claim_release_reconciliation`（before/after/binding），让漂移可追溯；它不是静默忽略，也不是 claim 成功的替代事实。
3. binding、scope、window、allocation 或 reservation 缺失仍是明确 RuntimeError，禁止跳过或伪造释放。
4. stale Recovery 将 Action 变为 terminal 后必须同步 coverage：pre-Gateway `failed/execution_timeout` 释放 `reserved`、`sending` 和历史误标的 `unknown + coverage_action_overdue` reservation 并按真实 error 回到 ready；Gateway-started 的 `unknown_after_send` 仍保持 unknown，不得重发。
5. 覆盖终态 Recovery 查询必须包含上述历史 unknown 行，并由覆盖日期、更新时间、reserved_action_id 的部分索引支撑；迁移必须原子替换旧只覆盖 `reserved/sending` 的索引条件。

### 5.4.2 Dispatcher claim 计划顺序

1. `plan_dispatch_claims` 先基于 Scope、Window、Shard Allocation、Reservation、allocation epoch 和公平 cursor 生成有界的 `candidate_action_ids`。锁行查询的排序固定为：既有 target admission、硬小时、搜索 membership/source、任务优先级、频道评论和公平优先级，随后才是这个序列的位置；同一优先级内必须保留该相对顺序。
2. PostgreSQL 的 `FOR UPDATE SKIP LOCKED`、状态变化、到期时间或任务停止只允许使对应候选行缺席；`claim_limit` 取剩余候选中的前 N 条。不得从未获本轮 reservation 的 Action 补位，也不得以 `scheduled_at`、`created_at` 或历史 backlog 年龄替代计划位置。
3. 计划顺序不重写既有类别或任务优先级，但它是同优先级内唯一的公平 tie-break。这样每个 shard 已分配的份额会首先服务其计划目标，而不是被历史同群 backlog 反复抢占。
4. `GroupSendSlotBlock` 仍在 Telegram Gateway 前按原 `legacy_group_slot` 校验并保留真实慢速模式延后事实；本修复只避免错误领取顺序制造无谓 slow-mode churn，不改变安全门或允许并发越过群冷却。

### 5.4.3 同群发送领取槽位

1. `TgGroup.next_group_send_slot_at` 是 `legacy_group_slot` 的持久投影：仅当群行锁内的 active-window、目标身份、群日上限与冷却校验全部通过，且 ExecutionAttempt 即将写入 `before_call/gateway_call_started` 时，才以与校验相同的北京时间 policy clock 写为 `now + group_cooldown_seconds` 并与 attempt 同事务提交。该值在 Gateway 返回前只是当前 Action 的临时槽位预约，必须在 Action.result 留预约时刻以防清除后来 Action 的槽位：成功或 `unknown_after_send` 保留正常冷却；Gateway 返回已知失败时，`FloodWait` / `SlowMode` 用 Telegram 明确秒数改写为精确重试槽位，其他已知失败只清除自己的预约。它不替代 ExecutionAttempt，也不把未进入 Gateway 的 Action 计为已发送。
2. Dispatcher 在生成 claim plan 前排除 `next_group_send_slot_at > now` 的 legacy 群正文；在锁定候选 Action 后，必须再以 `TgGroup FOR UPDATE SKIP LOCKED` 取得同群锁、复查该时刻和同群 `claiming/executing` Action，并在一个 claim batch 中只保留计划顺序最靠前的一条同群正文。未获群锁或仍在途的候选保持 pending，不能被标为成功、unknown 或 slow-mode。
3. `account_only` 与 `account_only_with_group_daily_limit` 不使用同群领取槽位；它们继续沿各自群日上限和账号安全边界执行。Gateway 的 `group_send_slot_block` 始终保留为最终竞争条件和历史 Action 兼容保护。
4. 群槽位阻塞只能释放该群的 claim 候选；同一 Window 内其余已分配目标仍按 `DispatchClaimPlan` 顺序可被领取。不得因一个群的冷却把整个 shard 或 Dispatcher Scope 置为等待。

### 5.5 任务中心、接口和审计

新增只读接口 GET /api/tasks/{task_id}/daily-fulfillment?date=YYYY-MM-DD，返回：

- 当日 frozen denominator、confirmed、ready、reserved、sending、unknown、blocked；
- maximum_confirmable_count、daily_outcome、blocker_counts、next_decision_at；
- 覆盖行分页摘要，含 account、状态、blocker_code、blocker_stage、last_action_id、recovery_path 与远端消息 ID；
- 质量漏斗和 generation contract 漏斗；不返回完整 Prompt 或敏感原文。

任务详情、任务列表和运营异常页必须同时显示：

- 全部分母完成度与可发送子集完成度，二者不可互相替代；
- 权限阻塞、内容重复、批量映射失败、未建单 ready、unknown 的独立数量；
- 当日 blocked 或 at_risk 的原因和下一步入口。

已有 membership-admission retry、目标引用修复和 unknown 核验入口继续复用；新 UI 不得提供“忽略 blocker 并记完成”的按钮。

## 6. 数据流与并发边界

~~~text
冻结当日账号分母
  -> 准入/权限事实 + 覆盖行 blocker / recovery_path
  -> full_shortfall / future_open / pre-Gateway overdue / unknown_hold / required_new 决策审计
  -> Planner 选择唯一 variation intent 并预约
  -> Dispatcher claim（Scope -> Window -> Allocation -> Reservation -> DispatchClaimPlan candidate 顺序）
  -> legacy 群 next slot / 同群在途 Action 预过滤
  -> 既有 claim 优先级 + 同一 candidate 顺序内 `FOR UPDATE SKIP LOCKED` + 群行锁，仅跳过不可领取行，不按旧排期重排
  -> Dispatcher 批量输出完整性校验
  -> generation_contract blocker，或内容质量门
  -> pre-Gateway overdue：reserved + dispatcher_lag，等待 Recovery / Dispatcher，不重发
  -> Telegram Gateway boundary：群行锁内写 Action-owned next_group_send_slot_at 与 ExecutionAttempt.gateway_call_started_at
  -> 成功/unknown 保留槽位；已知失败按精确 Telegram retry 重写或仅释放自己的临时槽位
  -> post-Gateway overdue：unknown + remote_reconcile，不重发
  -> ExecutionAttempt + remote_message_id
  -> 覆盖 confirmed 与 daily_fulfillment 投影
  -> stale Recovery：终态 Action -> exact claim counter reconcile -> coverage sync
~~~

- Planner 只做数据库读写和 slot 编排，不调用 AI 或 Telegram。TaskDailyFulfillmentDecision、token 化 coverage reservation 与 `action_id=null` 的 AiCoverageVariationIntent 必须先落库；Action flush 成功后才允许绑定两个 Action 外键。
- Dispatcher 的外部 AI 与 Telegram 调用均在数据库事务外；预约、质量写入、释放和最终 credit 均用短事务加 compare-and-swap。
- 同一 coverage 行只能有一个有效 reservation；批次契约失败与质量失败必须按 action_id 幂等释放，重复消费不能释放其他 Action 的预约。
- claim ledger 的释放先以持久 Action 的执行态重算，再清空本 Action 的 `dispatch_claim_active`；重算发现漂移必须把 before/after 写入 Action.result，不能抛 underflow 使 Recovery 整批回滚。
- Dispatcher 只在 `DispatchClaimPlan.candidate_action_ids` 内取锁；既有类别、任务和公平优先级保留，同一优先级内必须保持计划顺序。`SKIP LOCKED` 或资格变化导致的缺席不能触发按旧排期的静态排序回填，避免同群历史 backlog 抢占其他已分配 shard 份额。
- 同群 legacy 发送在 claim 阶段和 Gateway 阶段都由同一 `TgGroup` 行事实约束：claim 阶段只允许一个 in-flight candidate，Gateway 通过后才持久推进下一槽位；任一阶段竞争失败都保留 pending，不伪造远端结果。
- 同一 generation contract batch 只允许有一条 AiGenerationContractAudit；合同失败不会生成新的 variation intent。需要恢复时必须由已审计的 contract revision 或受限人工决定创建新的 Action。
- 每个 Action 的 variation 与 generation attempt 只可追加审计，不能用后来的重试覆盖先前失败事实。
- target_reference_revision、tenant、task、group、account 和 coverage_ledger_id 必须在 Planner、生成落库、Gateway 前和 finalization 四个边界一致；任一不一致不得发送。
- `TaskDailyCoveragePlanCursor` 是 `(tenant_id, task_id, coverage_date)` 的唯一 Planner 串行点。已有游标只能锁定该游标行；首次创建必须以该唯一键 `INSERT ... ON CONFLICT DO NOTHING` 后重新锁定游标行。Planner 不能在已持有 Action 或 coverage 写入时再锁 `tasks` 行，因为 Dispatcher 会先处理 Action 再回写 Task 统计，反向锁序必须显式消除；并发创建竞争者必须重新读取同一游标，不能丢弃本轮 coverage 决策。

## 7. QA 验收

| 场景 | 必须证明 |
| --- | --- |
| 1h_similar 或 7d_semantic | 原 Action 终态失败、预约释放、覆盖不计成功；下一 Action 使用不同 variation，未绕过质量门 |
| 已用 variation 再次被选择 | AiCoverageVariationIntent 的唯一约束拒绝重复 key；Planner 选择新的已配置意图或明确写无合法 variation，不创建模板补量 |
| 签到重复 | 签到不被当作特殊成功；命中重复后不再次用签到重发 |
| 批量少返回、重复序号或错 slot | 全批零 Gateway 调用；每个 slot 释放预约、写同一 AiGenerationContractAudit 并转为 generation_contract blocker；不因换 variation 自动重试 |
| 合同版本修复 | 只有新批准的 provider / prompt-contract / parser 版本或受限人工确认，才能把 generation_contract blocker 转回 ready；审计保留前后版本与操作者 |
| hard_hourly 已达标且仍有日覆盖 debt | required_new 仍触发规划决策；有足够容量时新建缺失 Action |
| 有 future_open 与 overdue_open | 两者分开计数；过期 open 不可伪装抵扣当前缺口 |
| overdue 且未进入 Gateway | 覆盖行保留 `reserved + dispatcher_lag`，`overdue_open_count` 递增、daily_outcome=at_risk；不得写 unknown 或创建第二条 Action |
| overdue 且已进入 Gateway | 覆盖行写 `unknown + coverage_action_overdue`，不重发，等待远端核验 |
| 历史 pre-Gateway unknown 终态 | Recovery 识别无 `gateway_call_started_at`，释放 reservation、保留真实失败码并回到 ready |
| 跨 Window claim counter 漂移 | Recovery 不因 underflow 回滚；Scope/Window/Allocation 按仍在执行的 binding 重算，Action 留 `dispatch_claim_release_reconciliation` 审计 |
| DispatchClaimPlan 与旧 backlog 排期相反 | 同一类别、任务和公平优先级内，加锁领取的 Action 顺序严格等于 `candidate_action_ids` 前缀；`scheduled_at` 更早的旧同群 Action 不得越过已分配的计划候选 |
| 计划内一条 Action 已被其他 worker 锁住 | `SKIP LOCKED` 后只从同一计划、同一优先级的剩余候选继续领取；不得改按未分配 Action 或旧时间排序补位 |
| legacy 群冷却仍启用 | 计划顺序修复不改变 `GroupSendSlotBlock`；真实慢速模式仍延后并可审计，连续可发送目标的远端发送间隔才可作为吞吐验收依据 |
| 同一 legacy 群多个 due Action、多个 worker | 同一 claim batch 只领取计划最靠前的一条；并发 worker 未取得 `TgGroup` 锁的候选保持 pending；首条 Gateway 槽位提交后 `next_group_send_slot_at` 阻止后续 Action 进入生成/Gateway，其他群仍可领取 |
| 已知 Gateway 失败后的同群槽位 | 账号受限、权限等已知失败只释放本 Action 持有的临时预约，不得清除后续 Action 槽位；FloodWait / SlowMode 以 Telegram 返回秒数覆盖预约。随后可发送账号可继续领取，未知结果仍保持槽位且不重发 |
| account_only 群 | 不读取或写入 legacy 的 group claim slot；原账号级并发和可选群日上限保持不变 |
| unknown-role bot 的广播频道规则 | 无 source-bound policy 时不创建 follow、不变更 admission；有同 group+peer 审计 policy 且 listener 再观察到有效、无收件人提示时，仅为运行中 scope 的既有 admission 创建逐账号 exact follow/callback，绝不创建正文或直接 ready |
| 两条不同显式收件人提示形成标准化频道规则 | 只批量展开 required-channel follow；不得覆盖其他账号的 admission source 或创建 callback。已有账号级 source/callback 保留，历史标准化污染 callback 写 superseded 并等待本账号提示 |
| 运行中任务正文账号缺少 GroupBotAdmission | Gateway 前按 task scope 补建 admission 并延后正文；不得走 legacy 放行、不得生成正文或计日覆盖成功 |
| 频道 follow 全成功但账号级 callback source 缺失 | 同 admission/version 只放行一条 post-follow visibility probe；远端可见才 ready/计成功，被拦截则保持真实 blocker |
| 上线前遗留、listener 滞后、窗口截断或机器人重发的 confirmation callback | 唯一 callback 必须先匹配当前 admission 的 `source_message_id`；旧 source 在 claim 和 Gateway 前写 `group_bot_confirmation_superseded`，新 source 重建精确按钮。通过该检查后，点击前必须同一账号先按 source ID 精确读取、再读取最新窗口的 trusted peer + exact channel refs + callback；窗口若有更晚有效来源则优先，精确读取不受监听窗口截断影响。持久化安全摘要并只换绑该 Action/admission；两个读取均未匹配或 Gateway 再报 mismatch 时写 `group_bot_confirmation_source_stale`，15 秒后重试且不调用旧 callback。即使账号处于全局冷却，旧项也不得延后、占用 dispatcher 领取资源或触发 Telegram callback |
| Planner 全局 backlog | 不绕过 pending 上限；写 planner_capacity_insufficient 与下一检查时间，daily_outcome 不得显示 feasible |
| 已有覆盖游标与 Dispatcher 并发 | Planner 只锁 `TaskDailyCoveragePlanCursor`，不再锁 `tasks` 行；PostgreSQL 不得出现 Planner/Dispatcher 的反向锁序或丢弃 coverage 决策 |
| cannot_send | 留在冻结分母、daily_outcome=blocked、无正文 Gateway 调用 |
| 入群申请待审批 | 不计 membership 或覆盖成功，显示 membership_permission_denied 或 join_request_pending 的真实原文 |
| unknown_after_send | 保持占位、不重发、不计成功，直到远端核验 |
| 青岛目标引用无效 | 继续 paused/target_ref_invalid，不改写为群解散或完成 |

## 8. 发布门与生产验收

1. 先完成数据库迁移、回归测试、前端类型检查和 docs/index 一致性检查。
2. 以 canary 任务验证内容重复、批量映射失败、权限阻塞、pre/post-Gateway overdue、stale claim recovery、claim 计划顺序、同群 claim slot、已审计群管频道规则和实时 confirmation 来源复核九类链路；canary 不得降低质量、权限或群冷却门。
3. 发布必须走 master -> release -> GitHub Actions Deploy Production。
4. 生产验收必须覆盖一个完整 Asia/Shanghai 自然日。每个任务导出冻结分母、覆盖账本、Action、ExecutionAttempt 和 remote_message_id 链路，并同时证明 stale executing/active claim 不再挤占 scope、同优先级计划候选未被历史同群 Action 的旧时间排序抢占、同群没有并发 `claiming/executing` 正文、已审计频道 follow 在正文前完成，以及 callback 点击审计来自实时同账号读取的可信精确按钮。部署后的短窗口只能证明修复生效：在 `listener_context_limit` 内不含原 source 的情形，仍应观察到 exact-source lookup 或换绑到更晚来源；应有 source refresh 或显式 stale retry，且不存在该窗口新增的 `group_bot_confirmation_button_failed` mismatch；连续可发送目标的远端确认间隔应达到每小时 120 条所需的约 30 秒一条；完整自然日才可证明任务按目标完成或显示真实外部 blocker。
5. 只有 full denominator=confirmed 且无 unknown 时，任务日可写 met。若存在真实外部阻塞，结论只能是 production_blocked；若缺少远端证据，结论只能是 production_unproven。

## 9. Product Design Complete 自检

| 检查项 | 结论 |
| --- | --- |
| 原始问题 | 内容重复、权限阻塞、生成映射失败、日覆盖漏规划、同群并发领取、群管频道规则未展开均已覆盖 |
| 前端状态 | 全分母、可发送子集、质量/契约/权限/调度 blocker 与处理入口已定义 |
| 后端与 Worker | Planner、Dispatcher、Action、覆盖账本、ExecutionAttempt、短事务边界已定义 |
| 数据流 | 从冻结义务到远端确认的完整链路已定义 |
| 权限与安全 | 复用既有权限入口，禁止跳过门禁，敏感生成内容不下放 |
| 边界与幂等 | unknown、target_ref_invalid、batch mismatch、重复质量、并发 reservation 已覆盖 |
| QA 与发布 | 回归、canary、完整自然日 E4 证据已定义 |
| design_status | complete |

### 9.1 当前 release 实现映射

- `daily_fulfillment.py` 必须按 ExecutionAttempt Gateway 边界区分 overdue：未进入 Gateway 的 Action 维持 `reserved + dispatcher_lag`，已进入 Gateway 的才为 `unknown + coverage_action_overdue`；所有 Action 与统计时钟的比较统一归一到任务统计时区，详情投影 `overdue_open_count` 与 blocker_counts。
- `dispatch_claim_ledger.py` 在终态释放时按仍为 `executing + dispatch_claim_active` 的 Action 重算 exact Scope/Window/Allocation counter；发现跨 Window 漂移时写 Action 审计而非抛 underflow。`service._recover_claimed_stale_action` 完成终态后同步 coverage，迁移替换覆盖 terminal unknown 的 Recovery 索引。
- `dispatcher._locked_claim_plan_candidates` 必须按既有 claim 优先级、再按 `DispatchClaimPlan.candidate_action_ids` 锁取 Action；`scheduled_at` / `created_at` 不能覆盖已获份额的同优先级计划。回归测试必须构造“计划优先 Action 的排期较晚”场景，并保留频道评论、硬小时和 membership 的既有优先级，证明旧 backlog 不会反超。
- `group_bot_global_rules.py` 将标准化显式收件人提示限制为 follow-only：只传播频道集合和 URL，不传播个人 callback source；清理历史标准化污染 source/callback 时保留账号自己已归属的 source。`group_bot_admission.plan_confirmation_button_action` 继续以 `GroupBotAdmission.source_message_id` 作为 callback 的唯一来源：新有效账号级控制消息到达时替换旧 source 的 pending Action；`dispatcher._confirm_action_claim_candidate` 和 Gateway 前复检同样先核对该 source/version，再执行账号 usage、shard 与容量 policy。`group_bot_confirmation_refresh.py` 在 click 边界先按 source ID 精确拉取、再以绑定账号拉取当前窗口的可信控制消息，校验 peer、当前频道集合和 callback 形态；窗口中存在更晚来源时换绑，窗口截断时仍可用精确来源，写 `GroupContextMessage` 安全摘要并只换绑该 Action/admission。两个读取失败或 Telegram 再变更时显式 stale retry。因此旧按钮不会被 `global_account_policy` 无限延后或误点，当前实时精确按钮才可进入 Telegram。
- `group_ai_chat.py` 先以 `reservation_token` CAS reservation，再持久化 `action_id=null` 的 `AiCoverageVariationIntent`，随后创建并 flush Action，最后绑定两个真实 Action 外键；重复 variation intent 明确释放同一 token reservation。迁移 `0123_coverage_reservation_binding.py` 提供该临时 token。
- 全局 Planner 无槽位时，`service.py` 对当日 ready debt 写 `planner_capacity_insufficient`、`next_decision_at` 和追加式 `TaskDailyFulfillmentDecision`，日履约不再静默显示 feasible。
- 迁移 `0121_daily_fulfillment_contracts.py` 持久化覆盖扩展列、variation intent、每日决定和 generation contract audit；自动化回归覆盖 intent 顺序、重复拒绝、overdue 与 backlog。
- `daily_coverage_planning.py` 对已有 `TaskDailyCoveragePlanCursor` 只执行游标行锁；首次创建通过唯一键冲突收敛后再锁定同一游标，消除 Planner 在 Action 处理之后反向锁 `tasks` 行造成的生产死锁。

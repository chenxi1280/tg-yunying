# AI 活群每日履约收口修复 PRD

## 1. 文档状态

| 项目 | 内容 |
| --- | --- |
| 需求级别 | L3 生产问题修复 |
| 设计状态 | complete（2026-07-27 群管控制提示分类、恢复与并发准入交叉修订） |
| 变更状态 | 先前 release 已完成基线/观察与成功事实的发布；本次将继续按 Release Gate 发布“按钮协议、来源信任前置、单群 admission 串行化、`group_bot_channel_follow` 适配 `actions.action_type` 30 字符上限、误判暂停后仅以新有效 source 恢复当前 follow 集”修复。真实 Telegram 结果与完整自然日验收仍须以 Action + Attempt + remote_message_id 和日账本证明。群管机器人准入完整细节以 `ai-conversation-humanization-and-group-bot-admission-prd.md` §5.3–§5.7.1 为准。 |
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
8. 当群管频道/“我已加入”只存在于内联按钮时，日覆盖链路必须以原消息、可信 peer、精确按钮和真实回执为准；历史已入库提示重新观测到同一 peer 的按钮时只补齐安全摘要；可信 peer 的普通推广内容也不得进入控制状态机、创建频道 follow 或把等待账号写成未归属，且同群新 admission 必须串行，避免把一条面向单账号的提示误套用给当天多个覆盖账号。因 `group_bot_control_prompt_unverified` 暂停的旧 follow 事实不阻塞新世代的有效频道集合；只有显式 restart 后的新 source message 才能重建同一频道 Action，日覆盖分母和未完成 blocker 不得因此缩小。

### 3.2 非目标

- 不降低 1h_similar、7d_semantic、内容政策、账号面具、群管准入、轮换、FloodWait 或账号容量门槛。
- 不把 签到、模板、静态文本或 Action 创建成功当作覆盖成功。
- 不自动把 cannot_send、membership_permission_denied、target_ref_invalid 或 admission_abandoned 从冻结分母删除。
- 不对 unknown_after_send 重发，也不以超时推定成功或失败。
- 不把供应商输出异常直接归因于某个模型；供应商原始响应未持久化时，只能报告 generation contract 失败。

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
| blocker_stage | admission、planning、generation_contract、quality、gateway、remote_reconcile |
| next_decision_at | 允许 Planner 再次决定该义务的最早时间；不是伪造成功或强制重发时间 |
| last_action_id | 最近关联 Action，无 Action 时必须显式为空 |
| reservation_token | 仅限同一 Planner 短事务中“已 CAS 预约、Action 尚未插入”的临时所有权 token；不得伪装为 Action ID，Action flush 后必须绑定 `reserved_action_id` 并清空 token |
| recovery_path | replan_with_new_variation、generation_contract_repair、permission_recheck、manual_approval、remote_reconcile、target_reference_repair 之一 |

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
2. 同任务存在 open Action 时，Planner 必须分别计算 future_open、overdue_open、valid_future_open_cover、unknown_hold、blocked_shortfall 与 required_new；有效 future open 必须把同一 coverage 行维持为 reserved/sending，overdue open 不得抵扣，也不得让其覆盖行继续伪装为 reserved。
3. hard_hourly 已经达标、缺口为零或下一小时检查未到，都不能让 required_new 被跳过。
4. 全局 pending 上限、任务 pending 上限或 Planner 无可用处理槽时，不得静默跳过 daily debt，也不得绕过既有容量门。必须写 `planner_capacity_insufficient`、当前 backlog 快照和 next_decision_at；该状态使 daily_outcome 至少为 at_risk，不能显示 feasible。
5. 每次规划或未建单必须追加 `TaskDailyFulfillmentDecision`，至少含 full_shortfall、valid_future_open_cover、unknown_hold、blocked_shortfall、ready_to_plan、required_new、hard_hourly_required_new、选择或跳过原因、next_decision_at。不能只靠 last_error 推测。
6. required_new 大于零且可发账号存在时，next_decision_at 必须是 daily_coverage_next_check_at；若容量、权限、质量、生成合同、Planner backlog 或未知结果阻塞，也必须写明确 blocker 和重新检查条件。
7. 该规则只允许为 `state=ready` 的义务创建新 Action；已处于 reserved/sending/unknown 的义务禁止再建单，不能因为日债务存在无限堆积 Action。

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
  -> full_shortfall / open_cover / unknown_hold / required_new 决策审计
  -> Planner 选择唯一 variation intent 并预约
  -> Dispatcher 批量输出完整性校验
  -> generation_contract blocker，或内容质量门
  -> Telegram Gateway
  -> ExecutionAttempt + remote_message_id
  -> 覆盖 confirmed 与 daily_fulfillment 投影
~~~

- Planner 只做数据库读写和 slot 编排，不调用 AI 或 Telegram。TaskDailyFulfillmentDecision、token 化 coverage reservation 与 `action_id=null` 的 AiCoverageVariationIntent 必须先落库；Action flush 成功后才允许绑定两个 Action 外键。
- Dispatcher 的外部 AI 与 Telegram 调用均在数据库事务外；预约、质量写入、释放和最终 credit 均用短事务加 compare-and-swap。
- 同一 coverage 行只能有一个有效 reservation；批次契约失败与质量失败必须按 action_id 幂等释放，重复消费不能释放其他 Action 的预约。
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
| Planner 全局 backlog | 不绕过 pending 上限；写 planner_capacity_insufficient 与下一检查时间，daily_outcome 不得显示 feasible |
| 已有覆盖游标与 Dispatcher 并发 | Planner 只锁 `TaskDailyCoveragePlanCursor`，不再锁 `tasks` 行；PostgreSQL 不得出现 Planner/Dispatcher 的反向锁序或丢弃 coverage 决策 |
| cannot_send | 留在冻结分母、daily_outcome=blocked、无正文 Gateway 调用 |
| 入群申请待审批 | 不计 membership 或覆盖成功，显示 membership_permission_denied 或 join_request_pending 的真实原文 |
| unknown_after_send | 保持占位、不重发、不计成功，直到远端核验 |
| 青岛目标引用无效 | 继续 paused/target_ref_invalid，不改写为群解散或完成 |

## 8. 发布门与生产验收

1. 先完成数据库迁移、回归测试、前端类型检查和 docs/index 一致性检查。
2. 以 canary 任务验证内容重复、批量映射失败、权限阻塞和 open Action 日债务四类链路；canary 不得降低质量或权限门。
3. 发布必须走 master -> release -> GitHub Actions Deploy Production。
4. 生产验收必须覆盖一个完整 Asia/Shanghai 自然日。每个任务导出冻结分母、覆盖账本、Action、ExecutionAttempt 和 remote_message_id 链路。
5. 只有 full denominator=confirmed 且无 unknown 时，任务日可写 met。若存在真实外部阻塞，结论只能是 production_blocked；若缺少远端证据，结论只能是 production_unproven。

## 9. Product Design Complete 自检

| 检查项 | 结论 |
| --- | --- |
| 原始问题 | 内容重复、权限阻塞、生成映射失败、日覆盖漏规划均已覆盖 |
| 前端状态 | 全分母、可发送子集、质量/契约/权限/调度 blocker 与处理入口已定义 |
| 后端与 Worker | Planner、Dispatcher、Action、覆盖账本、ExecutionAttempt、短事务边界已定义 |
| 数据流 | 从冻结义务到远端确认的完整链路已定义 |
| 权限与安全 | 复用既有权限入口，禁止跳过门禁，敏感生成内容不下放 |
| 边界与幂等 | unknown、target_ref_invalid、batch mismatch、重复质量、并发 reservation 已覆盖 |
| QA 与发布 | 回归、canary、完整自然日 E4 证据已定义 |
| design_status | complete |

### 9.1 当前 release 实现映射

- `daily_fulfillment.py` 将 overdue open 覆盖行转为 `unknown + coverage_action_overdue`，不再抵扣 `required_new`；所有 Action 与统计时钟的比较统一归一到任务统计时区，详情投影 `overdue_open_count`。
- `group_ai_chat.py` 先以 `reservation_token` CAS reservation，再持久化 `action_id=null` 的 `AiCoverageVariationIntent`，随后创建并 flush Action，最后绑定两个真实 Action 外键；重复 variation intent 明确释放同一 token reservation。迁移 `0123_coverage_reservation_binding.py` 提供该临时 token。
- 全局 Planner 无槽位时，`service.py` 对当日 ready debt 写 `planner_capacity_insufficient`、`next_decision_at` 和追加式 `TaskDailyFulfillmentDecision`，日履约不再静默显示 feasible。
- 迁移 `0121_daily_fulfillment_contracts.py` 持久化覆盖扩展列、variation intent、每日决定和 generation contract audit；自动化回归覆盖 intent 顺序、重复拒绝、overdue 与 backlog。
- `daily_coverage_planning.py` 对已有 `TaskDailyCoveragePlanCursor` 只执行游标行锁；首次创建通过唯一键冲突收敛后再锁定同一游标，消除 Planner 在 Action 处理之后反向锁 `tasks` 行造成的生产死锁。

# AI 活跃群与频道评论 Dispatcher AI 生成事务边界专项设计

> **2026-08-04 partial current contract：** 本文当前只保留“Planner 不持有外部调用事务、GenerationJob 以单行 CAS claim 后关闭事务调用 Provider、生成结果用 job version 单行 CAS 提交”的边界。Telegram 远端结果必须先追加唯一 `fulfillment_remote_fact`，其余对象由 projector 收敛；本文后续任何跨表最终结算或锁序不生效。冻结账号分母、账号级单 executing、同群单 ready、direct 严格上下文相等、本地 reply 缺失即终结、租户跨群硬去重、ContentMix/数量槽拥有义务等后续表述均为 `historical_do_not_implement`。当前并发、义务、C8 远端探针和提交合同以 `task-fulfillment-classified-recovery-prd.md`、`task-fulfillment-contract-closure-prd.md` 为准。

> **2026-07-31 resync：** AI 活群 normal 的生成粒度由批量 sibling 预写改为单 Action late binding，跨群 scope validator 与过期同槽重生成以 `ai-conversation-humanization-and-group-bot-admission-prd.md` §15 为准。频道评论和本文 Phase A/B/C 事务边界不变。

- 日期：2026-07-14
- 等级：L3
- 状态：`design_status=partial_current_transaction_boundary_only`、`resync=2026-08-04`
- 范围：AI 活跃群 reply / normal 与频道评论 direct / reply 的文本生成、质量过滤、预算/覆盖预约与真实发送。
- 触发证据：独立 QA I1 证明 Planner 在锁定 Task、任务日游标和 coverage 后，仍通过 quality -> `generate_group` / `generate_reply` -> Grok 同步外呼；生产取证同时证明 `channel_comment.build_plan` 在开放 Session 中执行 AI 生成和重描述，且评论与群活跃共用 Planner / Dispatcher 领取链路。

## 1. 决策与不变量

采用 Phase A / B / C。Planner 只做数据库编排；Dispatcher claim 提交后执行全部外部 AI 轮次；生成结果在短事务完成内容、记忆、去重和质量落库，再进入现有无事务 Telegram Gateway 与短事务 finalize。

不采用“AI 后直接 commit”，因为这会拆散 Action 创建、coverage reservation 和 cursor 推进；不采用“只在 Grok 前 commit”，因为 MiniMax、显式模型、reply 生成和 provider-backed 质量/改写仍可能持有事务。

以下业务语义不可改变：

- `messages_per_round` 决定 Cycle 的 Turn 数及 slot 映射，20 条 coverage 批限只切分 Planner 数据库事务；不得据此批量预写未来 normal 正文。
- 当前账号范围以任务内动态 scope 为准，`FulfillmentObligation` 是数量事实源；pending、生成成功、质量通过或 Action 创建都不计完成。
- 只有成功 Action、成功 ExecutionAttempt 和非空 `remote_message_id` 同时存在才确认覆盖。
- 频道评论累计目标、每小时预算、`reply_min_per_message` 和生命周期总上限保持原语义；pending、生成完成或质量通过均不计评论成功。
- reply 最小值、话题、讨论老师、账号面具、行为类型、连发结构、语义去重和内容政策不降级。
- Planner 禁止调用 AI Provider、Grok、Telegram Gateway、远端上下文采集或其他网络外呼；只读取已持久化事实。

## 2. Phase A：Planner 原子编排

Phase A 每个数据库事务最多处理 20 个 coverage slot，并原子完成：

1. 按任务日 keyset 游标读取到期 `ready` 义务，按原 `messages_per_round` 计算 Cycle 和 slot。
2. 为每个 slot 创建 `status=pending`、`ai_generation_status=pending` 的 Action。
3. 条件预约对应 coverage，并 compare-and-swap 推进任务日游标。
4. 保存后续生成所需的不可变编排快照，不生成文本、不写消息记忆、不执行外部质量轮次。

任一 Action 创建、coverage 条件预约或游标推进失败时，当前不超过 20 条的 Phase A 批次整体回滚。一个 30/60 Turn Cycle 可由 20+10 或 20+20+20 多个原子批次组成，但所有 Action 使用相同 `cycle_id` 和唯一 `slot_id`，不能因切批改变账号顺序、Turn 数或分母。

每个 pending Action 至少保存：`cycle_id`、`slot_id/slot_index`、`account_id`、`coverage_ledger_id`、`scheduled_at`、reply/normal 模式、`reply_to_message_id`、上下文快照 ID、账号面具版本、行为类型、话题、讨论老师、连发位置、prompt 输入摘要与质量规则版本。`(task_id, cycle_id, slot_id)` 和 coverage 条件预约共同阻止重复 slot。

频道评论 Phase A 只计算消息累计缺口、小时预算、账号、direct/reply 类型、固定引用目标和排期，创建 `status=pending`、`ai_generation_status=pending` 的 `post_comment` Action。评论 `action_dedupe_key` 由任务、频道消息、账号、direct/reply、引用目标和规划槽位等稳定事实组成，不依赖尚未生成的文本；Planner 不调用 AI 生成、重描述或 provider-backed 质量判断。

## 3. Phase B：Dispatcher 无事务外部 AI

Dispatcher 先在短事务 claim Action，写入 lease token、`ai_generation_status=generating`、generation attempt id 和 request id，然后提交并关闭事务。AI 活群 normal 每次只生成当前即将发送的一条 Action；reply 继续按冻结目标单条生成。频道评论仍按其专项合同处理。任何 generation sibling 查询即使 task/generation 相同，也不得把未来 AI 活群 slot 合并进本次 Provider 请求。

AI 活群 generation worker 为每个 Action 建立独立 GenerationJob；多个非冲突 Job 可并发调用 Provider，不共享 generation claim token。一个 drain 可继续处理其他独立 Action，但必须记录本轮已经访问的 Action id，避免 freshness pending Action 被立即重复领取自旋。claim token 和 generation attempt CAS 只 fence 当前 Action；不建立账号级或群级单 executing，只有同一 GenerationJob、同一远程副作用身份和 Provider 真实额度使用幂等/容量约束。

提交 claim 后，在无数据库事务区间完成：

- reply：重新确认目标消息仍存在、账号当前可发且 Telegram 远端仍可引用；随后使用 Phase A 固定的目标、slot、面具和规则生成。`context_bound_schedule_window_seconds` 只约束 Phase A 的近端排期，不得把 `Action.created_at` 或生成队列等待时长当作 reply 目标 TTL。
- normal：刷新目标群最新上下文，再使用 Phase A 固定的 slot、面具、话题、老师和行为类型生成。
- Provider 每次 attempt 启动前，以主数量槽所属 `TaskDayLedger.deadline_at` 减去当前时间计算剩余预算；剩余时间小于实际 AI 请求超时时写 `ai_generation_deadline_budget_exhausted`，不启动下一次外呼，也不把未执行的 attempt 算成六轮失败。该错误在 pre-Gateway finalize 中直接终结原 CycleSlot/主数量槽并记录内容 shortfall，不回 `replan_required` 重建空转。
- 全部 provider-backed 生成、fallback、改写或质量判断，包括 MiniMax、显式模型和 Grok。
- 频道评论 direct / reply：使用 Phase A 固定的频道消息、评论引用目标、账号和规则生成或重描述；整个生成链与 AI Provider 调用期间 `session.in_transaction()` 必须为 false。

normal Action 即使已经在 Phase C 持久化为 `ai_generation_status=ready`，Gateway 前仍必须比较当前最新真人上下文 ID 与生成时的 `context_snapshot_message_id`。如果出现更新上下文，旧正文不得直接发送：同一 Action/slot/coverage 保持不变，旧消息记忆显式转为 `expired_before_send/generation_context_superseded`，清除旧正文和生成缓存，以新 generation attempt 重新进入 Phase B/C。reply Action 继续使用冻结引用目标，不因群内其他新消息改成 normal 或更换引用。

本地 reply 目标缺失时先进入 C8 `local_target_unresolved`：完成 listener resync，并使用固化随机顺序的合格查看账号做远端精确探针。只有满足闭合专项负面证据与 CAS 的 Telegram 权威删除才终结为 `remote_target_deleted`；远端仍存在则修复索引并重物化原义务。不得静默改成 normal，也不得伪造 reply 指标。

## 4. Phase C：短事务质量落库

Phase C 以 claim lease token 和 generation attempt id 条件更新，拒绝过期 worker 写入。单批事务必须少于 5 秒，并完成：

- 校验 AI 输出与 `slot_id -> account_id -> coverage_ledger_id` 一一对应；
- 执行无需外部网络的内容清洗、指纹、语义簇、内容政策、账号面具和 DB 记忆重复检查；同一 generation 批次只读取一次租户级 7 天轻量消息窗口，1 小时窗口由该快照过滤，本批已接受 slot 立即写入批内快照；后续 slot 通过 `updated_at` 覆盖窗口增量合并其他 Dispatcher 新提交记录，保持租户级跨群、同批和并发去重口径但不逐 slot 重扫历史窗口；历史相似度仍等价于 `max(SequenceMatcher ratio, char Jaccard)`，实现可用字符集合和字符多重集计算严格可达上界，提前排除不可能达到阈值的行并缓存字符画像，但不得抽样、截断历史或改变 0.78 / 0.80 阈值；
- 通过时原子写 Action 文本/生成审计、`AiGroupMessageMemory` 预约和 `ai_generation_status=ready`；
- 重复、质量不足、内容政策失败或 reply 失效时，原子写可见原因、终结 Action 并释放自己的 coverage reservation；同批其他 slot 按各自结果处理；
- 整个 Phase C 提交失败时不允许部分 slot 进入 ready，也不允许进入 Telegram Gateway。

频道评论不写 `AiGroupMessageMemory`，但必须复用公共出站内容过滤、评论质量规则和相同的 lease token / generation attempt CAS。只有文本、生成审计和 `ai_generation_status=ready` 在短事务成功提交后，`post_comment` 才能进入 Telegram Gateway；reply 目标失效必须显式终结，不能降级为 direct。

Phase C 成功提交后才进入现有发送链：账号与权限最终检查短事务 -> 关闭事务 -> Telegram Gateway -> ExecutionAttempt / Action / coverage 短事务 finalize。任何外部调用期间 `session.in_transaction()` 必须为 false。

## 5. 重试、未知与幂等

- Action id、dedupe key、cycle/slot 和 coverage reservation 在生成重试期间保持不变；generation attempt id 只标识一次真实 AI 外呼。
- Phase B claim 已提交但未开始外呼时，可由 lease recovery 重领同一 Action；已进入外呼但未完成 Phase C 时，旧 attempt 标记 `ai_result_persist_unknown`，不得标记为 Telegram `unknown_after_send`。
- AI 返回成功但 Phase C 落库失败时，群内没有可见副作用。恢复后重用同一 Action/slot/coverage，按 provider 能力复用 request id，否则创建新 generation attempt；重新生成结果仍须经过完整去重和质量门。不得创建第二个有效预约。
- Phase C 已提交 `ai_generation_status=ready` 后，重复消费通常只读取已持久化文本；唯一重新生成条件是 normal Action 在 Gateway 前观察到更新的真人上下文，此时必须按第 3 节显式过期旧消息记忆并创建新 attempt。
- AI provider/fallback 最终失败或质量最终拒绝时，Action 以 `generation_failed` 或明确质量错误终结，并在同一事务释放自己的 coverage / 预算预约；后续由 Planner 按原 `ContentMixCycleSlot` 的 `replan_required` 重建，不另建替代内容义务。静态签到只在该槽没有任何 `pending` ContentMix 义务时允许，不能用空 `material_intent` 替代持久化证明。
- Phase C 已经写入显式业务终态的 `AiGenerationUnavailable` 必须清空当前生成 claim/lease，并只结束当前批次；AI generation worker 在同一 drain 中继续处理后续独立 Action。程序异常、数据库错误、claim fencing 丢失或没有持久化明确终态的失败继续使本轮 drain 失败并暴露，不得静默跳过。
- Telegram Gateway 调用后结果不明继续使用 `unknown_after_send`，保留 coverage unknown 且禁止自动重发；AI 生成未知和 Telegram 发送未知必须分开统计。

## 6. 可观测状态

`Action.payload` 保存编排快照、`ai_generation_status`、lease/attempt/request id 和生成历史，`Action.result` 保存终态失败阶段；不新增另一份 coverage、评论预算或成功事实。任务详情和运行日志至少区分 `generation_pending`、`generation_claimed`、`generation_ready`、`generation_failed`、`ai_result_persist_unknown`、`quality_rejected`、`reply_target_stale`、`gateway_unknown`。每次 generation attempt 记录 action、任务类型、cycle/slot、provider/model、开始/结束时间、outcome、失败阶段和 lease owner；不得把 provider 成功等同于 Action、coverage 或评论成功。

现有任务详情权限继续约束生成状态和错误下钻，不新增前端写入口；状态摘要不得暴露完整 prompt 或非本租户上下文。所有 claim、reply target、Action、coverage 和 message memory 查询同时校验 tenant/task/target group，跨租户 id 不得被生成或回写。

## 7. E2 验收

- 在所有 AI Provider、Grok、Telegram Gateway 和远端上下文入口断言 Planner 调用次数为 0；Planner 每个 coverage 批事务少于 5 秒。
- 真 PostgreSQL 验证 10/30/60 `messages_per_round` 映射保持 10、20+10、20+20+20，580 条义务多批完整编排，分母不变且无重复 slot / reservation。
- 注入 Phase A 任意 slot 创建、预约和 cursor CAS 失败，证明当前批 Action、coverage、cursor 全部回滚。
- reply 目标在 Phase A 后删除、过期、权限丢失时不调用 AI/Telegram、不转 normal、Action 可见终结且 coverage 回到 ready；有效 reply 保持目标和引用指标。
- normal 在 Phase B 使用最新上下文；已 ready 但尚未进入 Gateway 的 normal Action 遇到更新上下文时，真 PostgreSQL 回归必须证明旧消息记忆过期、同一 Action/slot 重新生成且发送正文包含新上下文；同批 AI 输出缺失、额外、重复或错绑 slot 时不得串账号，错误 slot 不进入 Gateway。
- 内容重复、面具不符、内容政策和质量不足均在 Phase C 终结对应 Action 并释放 coverage；通过 slot 的文本、记忆和状态原子提交。
- 在 AI 成功返回后注入 Phase C commit 失败，证明无 Telegram 调用、旧 attempt 可见为生成结果落库未知，恢复只重试同一 Action/slot且无第二个有效预约。
- 两个 Dispatcher 并发 claim 不重复外呼同一 attempt；Phase B claim、Phase C、发送前检查和 finalize 每个数据库事务均 `<5s`，全部外部 AI / Telegram 调用期间无数据库事务。
- 同一 worker 一次领取 2 条以上共享 claim token 的 normal pending sibling 时，只有一个生成入口处于 Phase B/C；Action 更新集合不重叠，PostgreSQL 无 `UPDATE actions ... deadlock detected`，同批 sibling 最终都得到 ready 文本或各自可见终态。
- 真 PostgreSQL 覆盖频道评论 direct / reply：两个 Planner 不重复创建同一评论 Action，两个 Dispatcher 不重复 claim / 外呼；AI 返回后 Phase C 崩溃只恢复同一 Action，reply 目标失效不转 direct，生命周期总上限完成态不被 Recovery 复活。
- 群活跃与频道评论 Planner / Dispatcher 并发运行时无死锁，热 Task、Action、coverage、预算和 stats 行没有持续 `>5s` 锁等待；worker 本地健康心跳在长 drain 内按周期刷新，不能仅在 drain 开始时写一次。
- `unknown_after_send` 不重发，generation unknown 不计远端 unknown；跨租户 Action、coverage、context 和 message memory 隔离。

## 8. E4、发布与回滚

E4 要求发布后连续至少 3 个完整 planner/dispatcher/metrics cycles 全部 worker healthy、无 `>60s` transaction、无持续 `>5s` lock waiter、无新增 deadlock。群活跃覆盖必须按发布时已批准的任务范围动态生成当日分母，并从当前远端确认数连续增长到义务清零或逐账号显式 blocker；频道评论发布前已有的 9 条 overdue 必须清零或逐条给出可复核 blocker，并出现新的成功 ExecutionAttempt 与非空 `remote_message_id`。完整北京时间自然日最终以全部已批准群 × 当日应覆盖账号的 Telegram 远端成功矩阵验收。worker healthy、AI provider success 或 pending Action 增长都不能替代 E4。

Phase A/B/C 必须同一 release 启用，禁止 Planner 外呼兼容分支或失败后回退 Planner 同步生成。发布脚本先停止 planner/dispatcher，清理已确认属于旧版本且没有远端副作用的数据库会话和过期 claim，再按 planner -> dispatcher -> metrics/recovery 分阶段恢复。应用回滚保留 Action、ExecutionAttempt、coverage、评论预算、游标和生成审计数据；暂停频道评论发送，且旧代码不得发送空文本 pending Action。禁止回滚到 Planner 同步生成，也禁止自动重发 `unknown_after_send`。

## 9. 生产配置边界

- 本次代码修复不把离线、需重登、session 失效、代理异常或目标权限不足账号伪装成可发言；这些账号继续以明确 blocker 从每日可完成矩阵中下钻。
- 郑州师范任务当前账号范围与“所有群所有账号”目标不一致。是否改为全账号、是否纳入本次完整日分母，必须以已批准的生产任务范围和可发言容量为准；发布不得静默改配置。
- 两个青岛任务当前暂停，且一个目标标识无效、另一个真实目标仅部分账号可发送。发布不得自动恢复两个任务；恢复前只允许选择经 Telegram 实测可发送的唯一目标并保留不可用账号清单。
- 因此核心事务设计为 `complete`；郑州范围和青岛目标/启停属于独立生产配置决定，保持 `blocked`，不阻断代码进入 dev，但阻断相应群的 E4 完成声明。

## 10. 当前适用范围自检

本文只有“三阶段事务边界与外部调用不持有数据库事务”可作为当前实现输入，状态为 `partial_current_transaction_boundary_only`。账号范围、义务、生成并发、context、C8、Provider 额度和 E4 必须回到两份 task-fulfillment 当前专项，不得以本文历史 complete 结论单独进入 dev。

# AI 活跃群与频道评论 Dispatcher AI 生成事务边界专项设计

- 日期：2026-07-14
- 等级：L3
- 状态：`design_status=complete`、`resync=true`
- 范围：AI 活跃群 reply / normal 与频道评论 direct / reply 的文本生成、质量过滤、预算/覆盖预约与真实发送。
- 触发证据：独立 QA I1 证明 Planner 在锁定 Task、任务日游标和 coverage 后，仍通过 quality -> `generate_group` / `generate_reply` -> Grok 同步外呼；生产取证同时证明 `channel_comment.build_plan` 在开放 Session 中执行 AI 生成和重描述，且评论与群活跃共用 Planner / Dispatcher 领取链路。

## 1. 决策与不变量

采用 Phase A / B / C。Planner 只做数据库编排；Dispatcher claim 提交后执行全部外部 AI 轮次；生成结果在短事务完成内容、记忆、去重和质量落库，再进入现有无事务 Telegram Gateway 与短事务 finalize。

不采用“AI 后直接 commit”，因为这会拆散 Action 创建、coverage reservation 和 cursor 推进；不采用“只在 Grok 前 commit”，因为 MiniMax、显式模型、reply 生成和 provider-backed 质量/改写仍可能持有事务。

以下业务语义不可改变：

- `messages_per_round` 决定 Cycle 的 Turn 数及 slot 映射，20 条 coverage 批限只切分数据库事务。
- `TaskAccountDailyCoverage` 仍是 580 分母事实源；pending、生成成功、质量通过或 Action 创建都不计完成。
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

Dispatcher 先在短事务 claim Action，写入 lease token、`ai_generation_status=generating`、generation attempt id 和 request id，然后提交并关闭事务。reply 与 normal 分批，只有同任务、同 Cycle、同 generation mode、临近执行且规则版本一致的 sibling 可进入同一批；每批输出必须按 `slot_id` 一一映射，缺失、额外或重复 slot 都是显式失败。

同一 worker 一次 claim 中，共用 `ai_generation_claim_token` 的 normal pending sibling 只能由最早 Action 作为当前生成批次入口。worker 发现 claim 批次包含这种共享生成批次时，必须按领取顺序串行推进该 claim 批次，先由入口 Action 完成批量生成和 Phase C，再让已得到 `ready` 文本的 sibling 进入发送；不得把每个 pending sibling 同时提交到线程池，使多个线程各自加载并更新重叠的 Action 集合。这个串行边界只约束同一 claim 内的共享 AI 生成事务，不减少应领取、应生成或应发送的 Action 总量，其他不共享生成批次的 Dispatcher Action 保持原并发执行。

提交 claim 后，在无数据库事务区间完成：

- reply：重新确认目标消息仍存在、可引用且未超出 `context_bound_schedule_window_seconds`；随后使用 Phase A 固定的目标、slot、面具和规则生成。
- normal：刷新目标群最新上下文，再使用 Phase A 固定的 slot、面具、话题、老师和行为类型生成。
- 全部 provider-backed 生成、fallback、改写或质量判断，包括 MiniMax、显式模型和 Grok。
- 频道评论 direct / reply：使用 Phase A 固定的频道消息、评论引用目标、账号和规则生成或重描述；整个生成链与 AI Provider 调用期间 `session.in_transaction()` 必须为 false。

reply 目标消息消失、被删除、不可访问或过期时，不得静默改成 normal，也不得伪造 reply 指标。Dispatcher 不调用 AI，进入 Phase C 将 Action 终结为 `reply_target_missing` / `reply_target_stale` 并释放 coverage；义务回到 `ready`，下一 Cycle 基于新上下文编排。

## 4. Phase C：短事务质量落库

Phase C 以 claim lease token 和 generation attempt id 条件更新，拒绝过期 worker 写入。单批事务必须少于 5 秒，并完成：

- 校验 AI 输出与 `slot_id -> account_id -> coverage_ledger_id` 一一对应；
- 执行无需外部网络的内容清洗、指纹、语义簇、内容政策、账号面具和 DB 记忆重复检查；同一 generation 批次只读取一次租户级 7 天轻量消息窗口，1 小时窗口由该快照过滤，本批已接受 slot 立即写入批内快照；后续 slot 通过 `updated_at` 覆盖窗口增量合并其他 Dispatcher 新提交记录，保持租户级跨群、同批和并发去重口径但不逐 slot 重扫历史窗口；
- 通过时原子写 Action 文本/生成审计、`AiGroupMessageMemory` 预约和 `ai_generation_status=ready`；
- 重复、质量不足、内容政策失败或 reply 失效时，原子写可见原因、终结 Action 并释放自己的 coverage reservation；同批其他 slot 按各自结果处理；
- 整个 Phase C 提交失败时不允许部分 slot 进入 ready，也不允许进入 Telegram Gateway。

频道评论不写 `AiGroupMessageMemory`，但必须复用公共出站内容过滤、评论质量规则和相同的 lease token / generation attempt CAS。只有文本、生成审计和 `ai_generation_status=ready` 在短事务成功提交后，`post_comment` 才能进入 Telegram Gateway；reply 目标失效必须显式终结，不能降级为 direct。

Phase C 成功提交后才进入现有发送链：账号与权限最终检查短事务 -> 关闭事务 -> Telegram Gateway -> ExecutionAttempt / Action / coverage 短事务 finalize。任何外部调用期间 `session.in_transaction()` 必须为 false。

## 5. 重试、未知与幂等

- Action id、dedupe key、cycle/slot 和 coverage reservation 在生成重试期间保持不变；generation attempt id 只标识一次真实 AI 外呼。
- Phase B claim 已提交但未开始外呼时，可由 lease recovery 重领同一 Action；已进入外呼但未完成 Phase C 时，旧 attempt 标记 `ai_result_persist_unknown`，不得标记为 Telegram `unknown_after_send`。
- AI 返回成功但 Phase C 落库失败时，群内没有可见副作用。恢复后重用同一 Action/slot/coverage，按 provider 能力复用 request id，否则创建新 generation attempt；重新生成结果仍须经过完整去重和质量门。不得创建第二个有效预约。
- Phase C 已提交 `ai_generation_status=ready` 后，重复消费只读取已持久化文本，不再次调用 AI。
- AI provider/fallback 最终失败或质量最终拒绝时，Action 以 `generation_failed` 或明确质量错误终结，并在同一事务释放自己的 coverage / 预算预约；后续由 Planner 为仍未完成义务创建新 Cycle，不把失败 Action 伪装为成功。
- Telegram Gateway 调用后结果不明继续使用 `unknown_after_send`，保留 coverage unknown 且禁止自动重发；AI 生成未知和 Telegram 发送未知必须分开统计。

## 6. 可观测状态

`Action.payload` 保存编排快照、`ai_generation_status`、lease/attempt/request id 和生成历史，`Action.result` 保存终态失败阶段；不新增另一份 coverage、评论预算或成功事实。任务详情和运行日志至少区分 `generation_pending`、`generation_claimed`、`generation_ready`、`generation_failed`、`ai_result_persist_unknown`、`quality_rejected`、`reply_target_stale`、`gateway_unknown`。每次 generation attempt 记录 action、任务类型、cycle/slot、provider/model、开始/结束时间、outcome、失败阶段和 lease owner；不得把 provider 成功等同于 Action、coverage 或评论成功。

现有任务详情权限继续约束生成状态和错误下钻，不新增前端写入口；状态摘要不得暴露完整 prompt 或非本租户上下文。所有 claim、reply target、Action、coverage 和 message memory 查询同时校验 tenant/task/target group，跨租户 id 不得被生成或回写。

## 7. E2 验收

- 在所有 AI Provider、Grok、Telegram Gateway 和远端上下文入口断言 Planner 调用次数为 0；Planner 每个 coverage 批事务少于 5 秒。
- 真 PostgreSQL 验证 10/30/60 `messages_per_round` 映射保持 10、20+10、20+20+20，580 条义务多批完整编排，分母不变且无重复 slot / reservation。
- 注入 Phase A 任意 slot 创建、预约和 cursor CAS 失败，证明当前批 Action、coverage、cursor 全部回滚。
- reply 目标在 Phase A 后删除、过期、权限丢失时不调用 AI/Telegram、不转 normal、Action 可见终结且 coverage 回到 ready；有效 reply 保持目标和引用指标。
- normal 在 Phase B 使用最新上下文；同批 AI 输出缺失、额外、重复或错绑 slot 时不得串账号，错误 slot 不进入 Gateway。
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

## 10. Product Design Complete

原始问题、群活跃 reply/normal、频道评论 direct/reply、三阶段职责、pending 原子性、slot 映射、预算/覆盖释放、生成/发送未知分层、并发幂等、worker 健康、事务指标、动态分母、`messages_per_round`、生命周期总上限、迁移/回滚和 E2/E4 均已定义；核心 `design_status=complete`、`resync=true`。进入 dev 前以本文件覆盖此前“Planner 可预生成 reply/comment 文本”及“只提交 Grok 前事务”的旧口径；郑州范围与青岛目标/启停仍按第 9 节保持生产配置 `blocked`。

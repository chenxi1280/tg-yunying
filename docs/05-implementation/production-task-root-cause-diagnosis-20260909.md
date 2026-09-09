# 2026-09-09 线上任务根因诊断

- intake_id: production-task-root-cause-20260909
- source: 用户要求“再看看线上的任务的状态，具体是什么问题找清楚”
- level: L3；severity: P1
- status: reproduced / mixed_partial_progress；不能标记 production_fixed
- scope: 24 个 running Task：活群 10、评论 2、点赞 6、浏览 5、搜索点击 1。
- authorization: 本轮诊断；生产操作全部为只读 SQL、日志、运行状态读取。未执行 drain、claim、重试、重启、清理或业务数据变更。
- observation: 2026-09-09 16:43–16:52 Asia/Shanghai。汇总快照为 16:51:22，同一 REPEATABLE READ READ ONLY 事务。其他专项样本保留各自时间，不能混成同一时刻。
- release: `/data/tgyunying/releases/20260909061103_8fc01975`；`/api/health` 返回 ok。
- code anchor: 本地 HEAD `43bb3cbb1d79260448086b67bffeaeb4930866f7`；核对 legacy_generation_timing.py、comment_generation_worker.py、direct_action_claims.py、source_pacing_release.py 相对线上 8fc01975 无差异。

## 汇总事实

16:51:22：10 个活群当天到期 12,177、确认 199、缺口 11,978。最近 30 分钟有 32 条活群 remote_message_observed（6 个 Task）、52 条 view_observed（5 个 Task）、14 条 target_click_observed（1 个 Task）；评论及点赞无对应新增远端事实。此处不计 membership_observed、remote_outcome_unknown。

账号熔断 closed 94、open 0；代理路由 closed 28、open 6。账号熔断恢复不等于账号准入、活动窗口、额度和消息可见性通过。

## 根因与证据

### R1：评论生成配置合同不一致，异常后遗留 Action 领取

observed_evidence:

- 两条评论 Task 的 ai_two_stage_enabled=false、ai_model 为空、ai_semantic_reviewer_model 为空。
- comment-generation 日志重复 `ValueError: generation_timing_legacy_reviewer_missing`；20 分钟日志有 96 条匹配行（含 traceback 重复，不能当 96 次独立失败）。
- 郑州楼凤评论有 24 条 executing / generating；样本 `f65aaf7b-9137-4a71-9e7c-f53cd262e338` 的 generation_job_id 为空，关联 obligation `5b46ef96-3269-46f6-af89-3654a3ed47f3` 没有持久 GenerationJob，ExecutionAttempt=0。

root_cause / code_path:

1. `comment_generation_worker._claim_comment_generation` 先提交 Action 的 executing、generating 和领取租约。
2. `comment_generation_dispatch._generation_config` 调用 timing binding。
3. `legacy_generation_timing.legacy_generation_execution_path` 对任何 channel_comment 都要求 reviewer；线上旧评论配置为空，在 Provider 调用前抛 ValueError。
4. `_process_comment_generation` 只捕获三类预期异常，ValueError 向外冒泡，末尾 `_release_comment_generation_claim` 没有执行；Job 准备事务回滚，先前已提交的 Action 领取仍在。

结论：不是 Provider 慢，也不是评论已在发送。已确认配置/运行合同冲突与异常领取收口缺口。是否应迁移为完整评论合同或调整合法旧配置路径，属于产品合同决策；诊断没有私自填模型或删除审核。

### R2：郑州大学、天津音乐消息发出后明确不可见，准入恢复缺少可信控制事实

- 郑州大学 16:08–16:27 的 6 条成功 send_message Attempt 均有远端消息 ID，但对应 PostSendVisibilityObservation 全部为 post_send_intercepted / terminal_reason=not_visible，observer_gap 为空，Action 最终 failed。
- 样本 Action `32073f10-1cfd-4c67-9119-970878590e0f`：16:26:59 收到发送结果，16:27:26 可见性检查 not_visible；恢复状态 blocked，原因 post_send_control_missing。
- 天津音乐 Action `5cda71ec-f672-4e09-b3a0-1ef240a66544`：16:14:23 收到发送结果，16:14:54 检查 not_visible；恢复原因 post_send_control_source_untrusted。
- 同账号 TaskGroupBotAdmission 已变为 post_send_intercepted。
- 对照：天津一品楼 `b16b7fac-f859-4f06-8cfd-44844c539db4` 在同一机制下 visible_confirmed，并有 remote_message_observed。

code_path: `dispatcher.recover_pending_visibility_credits` → `_probe_post_send_visibility` → `task_group_bot_post_send_recovery.recover_post_send_interception`。只有 probe.ok=true 且 visible=false 才返回 not_visible；查询异常返回未知。

结论：没有证据支持“消息事实漏写”。这里是不满足可见性合同而正确不记完成。不可见的具体删除者/群管规则尚未由权威控制事件确定；不把 intercepted 标签直接等同于已查明哪个 bot 删了消息。

### R3：美美备用的 extra-volume 计划集中在少数已覆盖账号，超过其实际组合额度

- 账号 29 的当日 authored_message portfolio allowance=3，budget confirmed 已用 3。
- 45 分钟内有 11 次 task_account_portfolio_capacity_exhausted，全部是账号 29。
- 待发送队列仍有该账号 23 条 ready、22 条 generation pending；另外两名主要账号 39、346 分别有 45、35 条 generation pending。
- 当天 coverage 有 426 条 pending_admission，ready=0 的早前快照不能解释为系统没有可继续参与额外量的已确认账号。

code_path: `executors/group_ai_chat._daily_group_extra_accounts` / `_eligible_daily_group_extra_accounts` 按已覆盖、在线、准入、画像筛选额外量候选并按成功数排序；该路径没有按 task/account portfolio 的剩余额度扣除已确认和已计划工作。`engagement_runtime_capacity._assert_portfolio_capacity` 在执行前按真实 allowance/used 拒绝。

结论：规划分配与执行额度没有闭合，执行层反复拒绝，代理切换无法消除该矛盾。

### R4：点赞存在取消预约残留、活动窗口/过期工作和远端能力阻塞

- 太郎日记：15 条到期 pending like Action，其 AccountPacingReservation 全为 cancelled。
- 郑州精品：64 条到期 pending，其中 43 条预约 cancelled、21 条 bound。
- 阿哥日记：45 条到期 pending，其中 17 条预约 cancelled、28 条 bound。
- cancelled 样本变更时间在 9 月 6 日；样本 Action 没有 ExecutionAttempt。`direct_action_claims._has_claimable_account_reservation` 只允许 reserved/bound，因此这些残留永远进不了当前领取候选；对应 obligation 仍 pending，状态没有闭合。
- 合法 bound 的部分点赞处于 session_rank=1；另有过期工作 deadline_rank=1，排在当前有效工作之后。当前快照只能证明后置等待，不能仅凭排序代码量化持续饥饿时间。
- 成都阿楠当时 27 条 pending 点赞全在未来，最早 20:46；郑州楼凤 368 条 pending 最早 16:53。不能把这些未来工作当当时应该立刻执行的错误。
- 西安焦点 intake 有 4 条 accepted 来源、source_available，但无 ReactionFulfillmentObligation；Task 精确能力阻塞记录 channel_message_id=865、capability_mode=none、reason_code=reaction_capability_unavailable。最近运行的是 ensure_target_membership，不是 like_message。

code_path: `direct_action_claims._has_claimable_account_reservation`、`_ranked_candidate_query`，`executors/channel_like_capability.message_reaction_plan`。历史哪次操作取消了预约未在本轮追溯至审计主体；当前破损状态和不领取原因已确认。

### R5：AI 生成未知结果集中在 15 秒单次 HTTP 上限，审核阶段占多数

- 45 分钟样本：44 个 provider_result_unknown，其中 34 个为 MiniMax-M2.5 的 group_semantic_review。
- 34 个审核未知的 latency_ms：14762–14812，中位 14787.5；成功审核中位 10688ms，部分接近同一截止点。
- ProviderHttpExchange 标为 AiHttpResultUnknown，样本 local_termination_confirmed=true；正常成功调用也持续存在。
- `generation_invocation_budget.MAX_LLM_INVOCATION_SECONDS=15`；隔离 HTTP transport 预留终止回收时间，截止后保存未知，普通生成不重领 unknown。
- 同窗口另有 HTTP 422、结构化输出不合法等失败，不能全部归为超时。

结论：现有时限与真实耗时尾部冲突，形成不可普通重试的历史占位。尚不能由此断定供应商内部延迟原因，也不能将时限直接调大作为已获授权的修复；PRD 明确当前 15 秒合同。

### R6：原“到时 ready”数字高估即时可执行量

16:50 左右专项快照：1082 条 pending/ready Action 的 scheduled_at 已到，但只有 22 条的账号处于当天已持久化活动窗口；其余 1060 条不在该窗口。这个数量不等于最终可发送 22 条，还须通过来源节奏、准入和资源检查。

`dispatch_session_priority` 与 `account_pacing_guard` 读取活动窗口；`source_pacing_admission` 再检查来源预约。必须按这两层解释积压，不能将全部 ready 队列直接称为 dispatcher 漏领。

并发方面，16:32:53 有 source_pacing_states DeadlockDetected，调用链 `direct_action_claims._claim_rows` → `release_source_pacing_admissions_before_gateway`；另有生成 worker 死锁日志和 TelethonOperationTimeout。它们造成局部失败，但没有足够证据将其宣称为全部低履约的唯一根因。

## 逐任务证据矩阵

服务列“pass”表示共用生产服务可访问、任务更新推进；不代表每个 Telegram 账号健康。远端列为 16:51:22 前 30 分钟 typed fact 数，0 不等于当天从未成功。

| unit | service | schedule/ledger | Action | Attempt/Gateway | remote fact | first blocker / status |
| --- | --- | --- | --- | --- | --- | --- |
| 活群 三亚 | pass | 到期1350/确认23 | 有ready | 有真实发送 | message 9 | 活动窗口/来源节奏、生成unknown；部分推进 |
| 活群 天津一品楼 | pass | 1069/2 | 有ready | 最近窗口无新发送 | message 0 | 多数账号不在活动窗；此前16:19有可见事实，当前未达标 |
| 活群 天津音乐 | pass | 1441/0 | ready及unknown | 成功后not_visible，另有unknown | message 0 | R2；failed/unproven |
| 活群 成都怡红院 | pass | 1334/68 | ready及生成中 | 有发送；入群Attempt另有权限错误 | message 3 | 窗口/节奏、准入及unknown；部分推进 |
| 活群 美美备用 | pass | 614/6 | 超额度账号仍有ready | portfolio拒绝 | message 0 | R3；failed |
| 活群 西安天上人间 | pass | 1207/10 | 有ready | 有发送 | message 6 | 活动窗口/来源节奏、历史unknown；部分推进 |
| 活群 郑州大学 | pass | 1238/1 | 有ready及历史unknown | 6次成功后均not_visible | message 0 | R2；failed |
| 活群 郑州学生会 | pass | 1423/21 | 有ready | 有发送 | message 7 | 窗口/节奏、历史unknown；部分推进 |
| 活群 郑州师范 | pass | 1197/56 | 有ready | 有发送 | message 6 | 窗口/节奏、367待准入；部分推进 |
| 活群 郑州楼凤 | pass | 1304/12 | 有ready | 有发送及unknown | message 1 | 窗口/节奏、130待准入；部分推进 |
| 评论 郑州楼凤 | pass | 34 pending义务 | 24 executing/generating | 样本Attempt0/Job0 | message 0 | R1；failed |
| 评论 阿哥日记 | pass | 13 pending义务；25过期 | 早前1条ready过期 | 最近无评论调用 | message 0 | 过期工作、容量缺口；同类reviewer配置缺失 |
| 点赞 太郎日记 | pass | 250 pending义务 | 15到期但预约cancelled | 这些样本Attempt0 | reaction 0 | R4；failed |
| 点赞 成都阿楠 | pass | 27 pending义务 | 当时全部未来 | 最近无点赞调用 | reaction 0 | 未来节奏及过期清算工作；尚未证明日履约 |
| 点赞 西安焦点 | pass | 来源有，点赞义务0 | 有成员Action | 成员成功并非点赞 | reaction 0 | reaction_capability_unavailable；failed |
| 点赞 郑州楼凤 | pass | 368 pending义务 | 当时全部未来 | 最近无点赞调用 | reaction 0 | 未来节奏及过期清算工作；尚未证明日履约 |
| 点赞 郑州精品 | pass | 425 pending义务 | 43到期预约cancelled，另21 bound | 最近主要成员操作 | reaction 0 | R4与活动窗口；failed |
| 点赞 阿哥日记 | pass | 207 pending义务 | 17到期预约cancelled，另28 bound | 最近无点赞调用 | reaction 0 | R4与过期工作；failed |
| 浏览 太郎日记 | pass | 有开放义务和过期积压 | 持续领取 | 有成功、节奏跳过 | view 8 | 部分推进；总量履约未核准 |
| 浏览 成都阿楠 | pass | 同上 | 持续领取 | 有成功、节奏跳过及unknown | view 9 | 部分推进；总量履约未核准 |
| 浏览 西安焦点 | pass | 同上 | 浏览与成员操作 | 有真实成功 | view 16 | 部分推进；总量履约未核准 |
| 浏览 郑州精品 | pass | 同上 | 浏览与成员操作 | 有真实成功及unknown | view 9 | 部分推进；总量履约未核准 |
| 浏览 阿哥日记 | pass | 同上 | 持续领取 | 有真实成功及unknown | view 10 | 部分推进；总量履约未核准 |
| 搜索点击 河南郑州学生会 | pass | assignment持续推进 | 有执行中 | 有真实点击及少量unknown | click 14 | 点击链路pass；日总量/入群目标未核准 |

## 结论边界与工程路由

- safe_online_repro: 只读重放候选 SELECT、精确 Action/Attempt/hold/observation 关联和日志时间线；没有调用会改变状态的 claim/revalidate 函数。
- observed_evidence: 以上均来自当前生产读取；代码只用于解释已发生路径。
- inference: extra-volume 的规划容量缺口、评论异常不释放领取得到现有代码与生产反例共同支持；活动窗口外积压不自动等于代码缺陷。
- blocked: 本轮没有访问阻塞。
- unproven: 不可见消息的删除者与完整群管规则、历史预约取消的审计主体、Provider 内部延迟原因、浏览和搜索全日验收仍未证明。
- next_route: 将 R1/R3/R4 的明确软件或数据合同缺口纳入 product → dev → qa → product → prod-diagnosis；R2 的可信控制事实与 R5 的时间合同独立处理。本文仅诊断，没有实现或发布这些修复。
- 口径纠正：统计发送必须限定 action_type=send_message；成员成功、过渡remote_outcome_unknown、普通Provider成功都不算最终消息履约。ready+scheduled_due也不能替代活动窗口和调用前准入。

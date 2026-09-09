# AI 活群准入 epoch、远端事实与目标错绑修复 PRD

> **2026-09-08 管理员救活设计修复：** §14为当前原因分类、触发、权限处置、真实actor和回接发言合同；保留§13受保护配置/历史unknown恢复。本切片仅修改设计，`resync=true`、尚未实现或生产验收。

> 日期：2026-08-28
> 分级：L3 / 线上多任务准入未履约
> 流程：`prod-diagnosis -> product -> dev -> qa -> product -> prod-diagnosis`
> 状态：`design_status=product_design_complete`、`implementation_status=pending`、`production_fixed=unproven`

> Release Gate 记录：首个候选 `b9f7b383` 未部署；PostgreSQL shard 0 发现仅有 username OperationTarget、尚未持久化 TgGroup 的历史任务被提前阻断。修正口径为：只有 Task 显式同时保存 target/group 时才先做双身份一致性门禁；username-only 任务仍先进入 membership gate 建立准入 Action，再由成功准入补齐群事实。

## 1. Intake Card

### 1.1 用户原话与范围

用户要求检查并修复西安天上人间、郑州学生会、郑州师范、郑州大学、郑州楼凤、成都怡红院等线上 AI 活群任务未完成问题，并确认所有 AI 活群加入链路是否存在同类缺陷。用户明确成都怡红院公开链接为 `https://t.me/CD_yhy`，救援账号也是管理员，预期先关注配置频道、加入目标群，再完成群管机器人准入和发言验证。

### 1.2 生产复现

- 8 个未删除的 `group_ai_chat` 任务均为 running。
- 7 个任务存在 2516 个旧 lifecycle epoch 且无任何 Attempt 的准入 Action，其中 1251 个普通 membership、1265 个 `invite_group_account` 救援 Action。
- claimant 要求 `Action.task_lifecycle_epoch == Task.task_lifecycle_epoch`，上述 Action 因而永远不可领取。
- `invite_group_account` 创建/刷新没有绑定当前 task epoch，任务 epoch 大于 1 时必然失效。
- 普通 membership 可在首次启动推进 epoch 前生成，启动后成为旧 epoch Action；现有 planner 又把旧 pending 当作 open，拒绝补建。
- membership Gateway 成功、Action/Attempt success 且没有 `remote_message_id` 时，fact-first 映射为 `remote_outcome_unknown`，无法形成 `membership_observed`。
- 成都 Task 的 `target_operation_target_id` 与 `target_group_id` 不属于同一 canonical peer；正确 `CD_yhy` 群已有账号关系，错绑群没有。

### 1.3 事故等级与影响

- L3：线上真实 Telegram 准入链路被阻断，worker/容器健康不能替代业务完成。
- 影响对象：全部 AI 活群任务的普通加入、救援邀请与成员事实投影；目标错绑的 Task 还会把群管准入、覆盖和发送计划投向不同群身份。

## 2. 根因分组

### RC-1 类型化事实缺失

`ensure_target_membership` / `ensure_channel_membership` 的成功事实不依赖消息 ID。只有 Action 和最新 Attempt 均为 success，且 `membership_status` 为 `joined` 或 `already_joined`，才生成 `membership_observed`。待审批、permission denied、无明确 membership 状态、Attempt 非 success 均不得生成该事实。

### RC-2 lifecycle epoch 失配

所有 admission Action 创建时必须固化当前 `task_lifecycle_epoch`。任务启动/恢复若推进 epoch，只有旧 epoch、尚未进入 Gateway、没有任何 ExecutionAttempt 的 admission Action 才能被审计式重建到当前 epoch；旧 Action 标记 `skipped/stale_lifecycle_epoch_replanned`，新 Action 使用新 ID、新 dedupe key 和当前 epoch。已有 Attempt、Gateway started、unknown 或终态行保持不可变，不得自动重试。

### RC-3 Task 目标身份错绑

`group_ai_chat` 的 `target_operation_target_id` 与 `target_group_id` 必须解析为同租户、同 Telegram peer 的 canonical target/group。两者同时存在但不一致时，planner 显式写 `target_identity_mismatch` blocker，不得继续选择任一“看起来可发”的同名群。

历史修复必须由受保护的 preview/apply 命令执行：精确 task ID、期望 task epoch、config revision、旧 target/group、目标 username、预览 fingerprint、actor 和 approval reference 缺一不可。apply 必须锁定 Task，复核 fingerprint 无漂移，且目标身份唯一。

## 3. 正常数据流

```text
Task(current epoch, canonical target/group)
  -> TaskMembershipAdmissionItem(account, canonical target)
  -> Action(current epoch, frozen target snapshot)
  -> ExecutionAttempt(Gateway boundary)
  -> membership_observed
  -> membership projection / TgGroupAccount / daily coverage
  -> group-bot admission
  -> send Action / remote_message_observed
```

救援链路：

```text
membership permission blocker
  -> invite_group_account(admin account, target account, current task epoch)
  -> Telegram invite or target-account invite-link join
  -> target-account membership readback
  -> membership_observed
  -> admission item resumes
```

管理员自身在线、是群管理员或 invite API success 都不单独等于目标账号已加入；最终必须以目标账号成员/发言能力观察收口。

## 4. 生命周期与幂等

### 4.1 允许重建

同时满足以下条件：

1. Task 为 running/pending 且未删除；
2. Action 类型属于 `ensure_channel_membership`、`ensure_target_membership`、`invite_group_account`；
3. Action 为 pending 且 epoch 小于 Task 当前 epoch；
4. 不存在任何 ExecutionAttempt；
5. 不存在 Gateway started、remote ID、unknown 或远端事实；
6. payload 目标仍与 Task canonical target/group 一致；
7. 同一旧 Action 在当前 epoch 尚无 replacement。

### 4.2 重建结果

- 旧 Action：`skipped`，记录 `stale_lifecycle_epoch_replanned`、旧/新 epoch 和 replacement ID。
- 新 Action：复制业务 payload，绑定当前 epoch，生成包含 `source_action_id + current_epoch` 的 dedupe key，状态 pending。
- `TaskMembershipAdmissionItem.membership_action_id/rescue_action_id` 若指向旧 Action，原子改绑新 Action。
- 重复运行 preview/apply 不再创建第二个 replacement。

### 4.3 禁止重建

任一 Attempt、Gateway started、unknown、remote fact、目标身份漂移或 active lease 存在时均阻断该行，保留原事实进入 reconcile；不得通过改 epoch 或改 status 让其再次发送。

## 5. 成都目标修复

- 唯一允许的新公开身份：username `CD_yhy`。
- 必须唯一解析到同租户 OperationTarget，再按相同 Telegram peer 唯一解析 TgGroup。
- 旧 config、准入项和当日覆盖只能在“没有目标群 Gateway 副作用”的守卫下重绑；若存在已发送或 unknown Action，停止 apply 并转人工分段迁移。
- 目标修复推进 config revision 和 lifecycle epoch；旧未执行计划明确 skipped，准入项重置为 pending 并改绑 canonical target，当日未确认覆盖改绑 canonical group 后重新计算。
- 历史已确认远端事实、历史 ledger、历史 Action/Attempt 不修改、不删除。

## 6. 预关注频道口径

AI 活群执行顺序固定为：配置的 `group_ai_prejoin_channel_ids` 全部确认关注 -> 加入目标群 -> 群管机器人准入 -> 发言验证/日覆盖。配置为空时不得臆造频道。成都现有预关注事实若绑定正确 `CD_yhy` 群可保留；目标修复后必须按 Task canonical group 重新投影，不得从错绑 group 读取。

## 7. API、权限、审计与失败语义

- 本次不新增普通用户 API；生产恢复使用运维脚本，默认 preview。
- apply 参数必须包含 `--task-id`、`--expected-epoch`、`--expected-config-revision`、`--expected-fingerprint`、`--actor`、`--approval-reference`。
- 多任务 epoch 恢复必须显式列出 task IDs；不能默认全库 apply。
- 审计记录 before/after hash、旧/新 target/group、旧/新 epoch、重建/阻断计数和 approval reference；不得记录手机号、Session、AuthKey、OTP 或账号邀请引用明文。
- preview 数量与诊断批准范围不符、fingerprint 漂移、身份不唯一、存在 Gateway/unknown、活动 lease 或生产 SHA 不兼容时 fail closed。

## 8. 回滚与恢复

- 代码回滚前需确认没有新格式事实或 recovery audit 被旧代码误读；否则 rollback 为 unproven。

## 9. 生产恢复配置兼容补充（2026-08-29）

- 受保护恢复必须继续复用 `update_task_settings` 的完整配置校验，禁止为修复目标身份而绕过 schema 直接改 `Task.type_config`。
- 线上 8 个 AI 活群任务均已由既有受审补丁写入 `adult_prompt_enabled=true` 与 `content_route=adult_service`；这两个字段是当前 Prompt 路由运行时仍会读取的受控兼容合同，任务设置正规化必须保留它们。
- `adult_prompt_enabled` 只接受布尔值；`content_route` 只接受既有明确路由枚举，未知值继续 fail closed。两个字段不加入普通设置页可编辑字段白名单，本次只修复“已有合法线上配置无法经正规设置更新”的合同断裂。
- Release Gate 首次 apply 在任何数据库写入前因 `extra_forbidden` 失败；修复候选必须新增“正规化保留受控旧路由、拒绝未知路由”回归测试，重新通过完整 CI、部署 SHA 校验和最新 fingerprint 后方可再次 apply。

## 10. 每日权限复检动作 epoch 补充（2026-08-29）

- 第一轮恢复后的独立回读发现，`membership_recovery_daily_permission_recheck` 会批量插入新的 `ensure_target_membership`，但插入行没有显式携带 Task 当前 lifecycle epoch，数据库默认写成 1；这会让刚生成的动作再次无法被 claimant 领取。
- 所有准入重试创建路径都必须在创建时显式绑定 `task.task_lifecycle_epoch`，不能依赖模型默认值。回归用非 1 epoch 任务验证每日权限复检的全部新动作均绑定当前 epoch。
- 已生成的这类旧 epoch 动作仍只允许按“pending、零 Attempt、canonical `channel_target_id`”条件由同一受保护恢复重建；已有 Attempt 的行继续禁止重放。

## 11. 同日目标切换的日账本补充（2026-08-29）

- 成都切换到 `CD_yhy` 后，独立 planner 回读出现 `daily_group_target_ledger_missing`：同一 Task 当日 ledger 已有旧目标 slots，旧实现只判断“ledger 是否已有任意 slot”便提前返回，导致新目标的 `TaskGroupDailyTarget.task_day_ledger_id` 为空。
- 当日 group slot 物化必须按 `task_day_ledger_id + target_operation_target_id` 判断，而非仅按 ledger；同日换目标时保留旧目标 slots 作为历史，不覆盖、不复用，并为新目标创建独立 slots。
- 新目标 `TaskGroupDailyTarget` 与新目标当日 coverage 必须绑定同一个现存 TaskDayLedger；coverage 查询必须同时限定 `group_id`，禁止同账号的新旧目标 coverage 相互覆盖。
- Action 重建是追加式：旧 Action 保留 skipped，新 Action 若未进入 Gateway可由同一审计操作取消；一旦进入 Gateway 只能 reconcile，不能回滚重发。
- 成都目标 apply 后若新 Action 尚未进入 Gateway，可用审计 before snapshot 做受保护反向配置恢复；已有新目标 Gateway 副作用后禁止自动反向切换。

## 9. QA 验收

### 9.1 单元/集成

- success + `membership_status=joined/already_joined` + 无 message ID -> `membership_observed`。
- success 但无明确 membership status、pending approval、failed/unknown -> 不得 confirmed。
- 救援 Action 创建与刷新均写当前 epoch。
- 旧 epoch pending、零 Attempt admission Action只重建一次并改绑 admission item。
- 有 Attempt/Gateway/unknown 的旧 Action不重建。
- OperationTarget/TgGroup peer 不一致时 planner 阻断。
- 成都恢复 preview fingerprint、CAS 漂移、精确目标和幂等 apply 测试。

### 9.2 Release Gate

1. source：候选 SHA、diff、基线和工作树清洁；
2. local：定向测试、相关测试、编译/静态检查；
3. CI：候选 SHA 必需 jobs 通过；
4. deployment：生产 current release 和 runtime SHA 一致；
5. business：逐任务 Task -> Action -> Attempt -> typed remote fact，另读回成都 canonical peer。

## 10. 生产验收与状态语言

- `preview_only`：仅确认匹配和计划变化；
- `persisted_verified`：数据库/config/action 读回符合预期；
- `remote_effect_verified`：Telegram 类型化成员事实成立；
- 只有原事故范围中全部要求完成，才能写 `production_fixed`。

对每个任务至少报告：task epoch、canonical target/group、旧 Action 处理数、新 Action 数、Attempt 状态、`membership_observed` 数、未完成 blocker。部署成功、worker 健康、Action 数或 invite API success 均不能替代 E4。

## 11. Product Design Complete 自检

- 已覆盖用户原话、全部 AI 活群扫描和成都公开链接。
- 已覆盖普通加入、救援管理员、预关注、群管准入、目标身份、fact-first 投影。
- 已覆盖失败/unknown、并发/CAS、幂等、审计、敏感信息、回滚和 E4。
- 不新增 silent fallback，不修改已有远端未知事实，不用 mock success。
- `design_status=product_design_complete`，可进入 dev；L3 Release Gate 与生产 readback 必须保留。

## 12. Product Handoff

- Dev：最小修改 fact mapper、救援 Action epoch、admission epoch replan、group target invariant、受保护恢复服务/脚本。
- QA：先提交失败回归，再验证允许/禁止重建矩阵及目标错绑 preview/apply。
- Product：按本 PRD 逐项验收，不把 qa_pass 当产品接受。
- Prod diagnosis：发布后以精确任务链和 Telegram typed remote fact 复核；未达到 E4 时保持 `production_fixed=unproven`。

## 13. 成都救援管理员错绑与 listener 远端失真补充（2026-08-30）

### 13.1 新增用户事实与生产复现

用户在 Telegram Desktop 中打开“成都怡红院”，明确说明当前账号已经是群管理员，并要求继续修复。该人工观察只证明桌面账号可访问目标群，不能直接证明生产 `Tenant.group_rescue_admin_account_id` 指向同一 Session。

生产只读关联得到以下事实：

1. Task、OperationTarget、TgGroup 和 125 条当前 epoch 救援 Action 的目标引用均解析为同一个 `CD_yhy` peer，本轮不是 target/group 再次错绑；
2. 当前生产救援 Session 与截图账号身份不一致，且该 Session 对目标 peer 的参与者读取为非成员，因此历史邀请必然缺少管理员权限；
3. 生产存在两条本地账号行映射到截图中的同一 Telegram 身份；两条远端均为 `ChannelParticipantAdmin`，具有 `invite_users` 与 `ban_users` 权限，但只有 display 精确匹配、current authorization 健康且零 open Action 的行可安全冻结为专职救援账号；
4. 当前 listener source 的本地投影为在线/可用，远端 `GetParticipant` 与 GetHistory 却返回 private/banned；排除救援身份后，另有多个不同 Telegram 身份的账号通过成员和 GetHistory 只读验证；
5. 当前 epoch 的救援 Action 中，113 条已进入 Gateway、journal 为 recorded 但 `remote_mutation_state=unknown`，不得因管理员配置修正而原地恢复 pending。

### 13.2 救援管理员绑定合同

`group_rescue_admin_account_id` 是本地账号行，不是昵称或 Telegram 身份的别名。本次只证明候选是 exact target 的管理员，因此新增 Task `type_config.group_rescue_admin_account_id` 作为目标级覆盖，Tenant 字段仅保留 legacy fallback；禁止凭一个群的 rights 改写 tenant 全局管理员并影响其他群。针对生产事故的管理员切换必须走受保护的 `preview -> apply -> readback`，并冻结：

- exact tenant、task、epoch、config revision、operation target、group；
- 当前救援账号行、候选救援账号行及二者 authorization generation；
- 候选账号未删除、在线、Session 存在、current authorization 为 current/healthy/active；
- 候选 Session 的远端 self identity 摘要；
- 三种目标引用解析到同一 peer；
- 候选对该 peer 的参与者类型为 admin/creator，且 `invite_users=true`；需要解除群限制时还必须 `ban_users=true`；
- 候选账号全局 open Action 数为 0，避免切为专职救援后使普通业务 Action 被 `rescue_admin_reserved` 跳过；
- 同一远端 self identity 的其他本地行只作为重复身份风险记录，不得同时充当第二个覆盖身份或 listener 来扩大完成分母。

候选只在该 Task/target 的救援 claim 中保留；它仍可按既有合同服务其他任务，不能因 target 级覆盖而从 tenant 全局账号池或其他任务 coverage 分母移除。

apply 前必须锁定 Task，复核 deployed SHA、预览 fingerprint、当前 Task 覆盖值/legacy fallback、候选 authorization/session 摘要、open Action 数、目标引用和远端 rights 均无漂移。apply 只修改 exact Task 的救援账号覆盖并写 AuditLog；不触碰 Tenant 全局管理员、其他 task、账号、Action/Attempt 或 Telegram。独立 readback 必须再次读取 Task 覆盖并用新 Session 复核远端 self/peer/admin rights。

普通设置 API 现有“在线 + Session”校验仍可用于日常配置，但不能代替本次目标特定的远端管理员证明；不得为了让设置保存成功而静默降级远端校验。

### 13.3 历史 unknown 救援 Action 的恢复合同

管理员绑定修正不构成旧远端结果的对账证据。所有已有 Gateway-started、`closed_unknown|unknown_after_send`、journal unknown 的救援 Action、Attempt 和 evidence journal 保持不可变，禁止：

- 把原 Action 改回 pending；
- 清空 Attempt 或 Gateway started；
- 复用原 gateway request identity；
- 仅凭当前管理员权限或错误文案推断旧邀请未发生；
- 批量把 unknown 改成 failed 后调用现有 refresh 路径。

受保护恢复按 exact Task/current epoch/current group 扫描 admission item 与其救援 Action，并由新管理员对每个目标账号做只读当前成员核对：

```text
old rescue Action/Attempt/journal (immutable)
  -> admin resolves frozen target_account_ref
  -> GetParticipant(target peer, target account)
     -> member: append membership_observed/reconcile fact; admission resumes without invite
     -> UserNotParticipant: create replacement invite_group_account with new Action ID and gateway request identity
     -> resolve/permission/FloodWait/transport unknown: keep blocked; no replacement
```

replacement 必须绑定 current task epoch、canonical group/target、当前救援账号、原 trigger account，并用 `source_action_id + current_epoch + recovery_manifest_hash` 形成唯一 dedupe；旧 Action 保留，admission item 只在同一事务 CAS 改绑 replacement。重复 apply 不得创建第二条 replacement。

历史生产数据允许 admission item 的 `rescue_action_id` 为空，但只限同 tenant、Task、target、target account 均精确匹配且当前没有指向无关救援 Action。preview 必须把 item 的 phase、rescue status 和 rescue action 摘要纳入 fingerprint；member 分支可在保持空指针的同时写类型化成员事实并完成投影，absent 分支才把 item 改绑到新 replacement。重复 apply 时只接受由该 source、epoch、target account 和 target 精确派生的既有 replacement；item 指向其他 Action 或精确 item 不存在时继续阻断，不能按账号模糊补写。

member 的类型化远端事实必须同时释放同 Task、当日、同 group/account、未确认、无 reservation 的 `blocked|pending_admission|unknown` coverage；生产历史中的 `unknown + membership_permission_denied|目标无效` 不能在已证明目标账号真实为成员后继续阻断。只允许清空该 exact coverage 的 blocker 并转 `ready + recovery_path=membership_observed`；历史日、已有 reservation、已确认或其他账号/目标一律不改。

只读成员核对发生 FloodWait 时保存明确 checkpoint 并停止本批；不得把未探测项目当缺席。若目标已是成员，只有 target-account 精确成员事实才允许推进 admission；管理员可邀请、invite API success 或本地 `TgGroupAccount` 均不单独确认目标成员。

### 13.4 新邀请失败的 mutation-state 语义

Gateway 对 `InviteToChannelRequest` 的异常必须保留具体错误分类。只有 Telegram 在请求接受前明确拒绝的类型（例如 admin required、当前调用账号无法访问目标 peer、目标实体解析失败）才能返回 `remote_mutation_started=false`；超时、连接中断、RPC 结果丢失或无法判断是否已受理继续返回 unknown。不得仅按中文错误文本包含“权限”统一断言 false。

新邀请确定失败且 `remote_mutation_started=false` 时可以形成新的失败事实并由显式恢复重规划；unknown 继续进入 RemoteReconcileCase，不自动重发。

### 13.5 listener 账号合同

listener 选择不能只相信 `TgGroupAccount.can_send/is_listener` 和账号在线投影。目标群已有 listener error 或 source 连续 private/banned 时，恢复必须在不发送消息的情况下验证候选：

1. 候选不是救援管理员行，也不与救援管理员共享同一远端 self identity；
2. 候选 current authorization 健康、Session 已授权；
3. 候选对 canonical target 的 `GetParticipant` 成功；
4. GetHistory 最小只读请求成功；
5. exact Task 的 `history_fetch_account_id` 通过 config revision/CAS 和审计更新，或由 runtime 使用带远端失败证据的有界 failover；本次生产恢复采用前者，避免在未完成通用 failover 设计时扩大范围。

旧 listener source、cursor 与错误记录不删除。新 source 必须从当前持久 cursor 按现有连续水位规则继续；只有新 context message/cursor readback 才证明 listener 恢复，设置字段 readback 不等于 E4。

### 13.6 本次受保护变更范围与顺序

本次精确顺序固定为：

1. preview 冻结当前部署 SHA、Task/epoch/revision、target/group、旧/新救援账号、远端 rights、零 open Action、listener 候选、113 条 unknown 与其不可回放集合；
2. apply 以同一 fingerprint 写入 exact Task 的救援账号覆盖，并把已远端验证的不同身份 listener 写入 `history_fetch_account_id`，推进 config revision，写单一恢复审计；Tenant 全局管理员与其他 Task 保持不变；
3. 独立 readback 核对救援绑定、Task config revision、listener Session 的成员/GetHistory、neighbor tenant/task 未变化；
4. 只读构建成员恢复 manifest；只对 `UserNotParticipant` 且无 replacement 的项目追加新救援 Action；不确定项保持 blocked；
5. Dispatcher 按新 Action 执行邀请，随后目标账号自己的 membership probe 形成 `membership_observed`；
6. listener 产生新上下文事实后，Planner/Generation/Dispatcher 继续原任务；最终按 due/coverage/typed remote facts 验收。

本次不修复账号初始化，不清理重复本地账号行，不对其他 AI 活群任务批量切换管理员或 listener，不删除旧 unknown。

### 13.7 QA 与验收补充

- 设置恢复 preview：候选昵称相同但远端身份不同、同身份多本地行、候选有 open Action、非成员、无 invite rights、peer 漂移、revision/SHA 漂移均阻断。
- apply：Tenant/Task 锁和 fingerprint CAS；重复 apply 幂等；AuditLog 含 approval reference 与 before/after hash，不含账号、Session、AuthKey、手机号或 target ref 明文。
- unknown 恢复：旧 Action/Attempt/journal 字段逐列不变；member/absent/inconclusive 三分支；只有 absent 创建唯一 replacement；重复 apply 零新增。
- Gateway：确定 pre-accept rejection 为 false；timeout/connection reset 为 unknown；错误分类不靠中文模糊匹配。
- listener：当前本地可用但远端 private 的账号不能继续选中；救援同 identity 的重复行排除；验证通过候选写入后从既有 cursor 连续采集。
- Release Gate：定向 no-PostgreSQL、真实 PostgreSQL/CAS、完整 CI、部署 SHA、受保护 preview/apply/readback、远端成员与 listener context 事实分别提供证据。
- 状态语言：配置与 revision 读回只能写 `persisted_verified`；replacement Action 入队只能写 `recovery_scheduled`；目标账号成员事实为 `remote_effect_verified`；原 Task 的 due/coverage 和 context/消息事实全部闭合后才允许 `production_fixed`。

### 13.8 Product Design Complete 再自检

- 已覆盖用户新增事实“截图账号已是管理员”，并区分桌面观察、生产 Session identity 和远端 rights。
- 已覆盖救援配置、重复身份、本地 open Action、目标解析、listener、旧 unknown、replacement 幂等、并发/CAS、FloodWait、审计、敏感信息、回滚和 E4。
- 已明确旧 unknown 不原地重试、不修改账号初始化、不扩大其他任务。
- `design_status=product_design_complete` 保持成立；可进入 dev，但必须先更新数据流索引并按本节 Product Handoff 实现。

## 14. 管理员救活按当前原因闭合（2026-09-08）

### 14.1 Intake、授权与状态

共享统一引擎§19.65的`intake-20260908-throughput-humanization-design-repair`，L3，用户本轮授权修复PRD设计。`design_status=complete`、`resync=true`、`implementation_status=not_started_for_this_slice`、`production_status=unproven`。本节优先于主PRD§3.4.1“所有权限失败累积四次再邀请”“管理员只能是全局值”的旧解释；保留本文件§13的target绑定、远端rights、旧unknown防重和受保护恢复。

代码已有邀请、导出邀请链接、解除限制、普通账号通过链接加入能力，但解除限制目前仅在非互相联系人后的特定链接失败分支触发；“已在群”直接投影invite_success。本节补齐原因分支和调用所有权，不把既有RPC函数存在当作救活已完成。

### 14.2 管理员范围与授权事实

- 解析顺序固定为本Task的`type_config.group_rescue_admin_account_id`优先，否则使用Tenant默认值。Tenant默认专职管理员按原合同从普通业务池排除；仅Task指定的管理员只在对应Task/target被保留，不从整个租户或其他Task分母移除。同一远端self的重复本地行不能充当第二个可并发actor或覆盖身份。
- 系统配置仍用原`system.manage`保存救援开关/管理员；Task覆盖只经现有受保护恢复入口修改，本轮不新增公开写API。关闭救援时只记录原因/等待，不调用管理员。已启用救援按原已授权的任务账号和目标执行本节可恢复动作，不要求每次常规救援再次点确认。
- 执行前以真实Session核验self、canonical peer、当前管理员rights及authorization generation；邀请需要invite_users，解除目标账号限制需要ban_users，审批需要对应管理权限。在线/本地“管理员”标签不能替代远端证明。管理员失效或丢权限仅阻断使用该管理员的当前步骤，健康普通账号和无此依赖的Task继续。
- 自动解除只处理授权救援范围内的账号/目标限制；有现存运营明确禁止恢复标记或明确人工禁言决定时沿原人工处置状态，不因救援优先级覆盖它。来源或限制性质不明先只读核实，不从普通GROUP_PERMISSION_DENIED猜测需要解除。

### 14.3 原因、动作和返回路径

| 当前权威事实 | 正常处理 | 完成/后续判定 |
| --- | --- | --- |
| 未加入，当前无未决申请/调用且普通加入可用 | 原正常加入；确需管理员邀请时创建对应救援步骤 | 成员事实→当前群管要求→权限复检 |
| 未加入且当前限制阻止加入，可由已授权管理员恢复 | 有ban_users则解除该账号限制，再按当前合法邀请/加入协议 | 每步各自回读；解除成功不计成员成功 |
| 已在群但账号被限制发言，确认属于可自动救援范围 | 有ban_users则执行解除限制，不重复邀请已在群账号 | 当前权限回读→原bot确认/观察→ready |
| 待管理员审批且有当前申请事实 | 使用当前管理员有权审批的原申请；不重复提交加入申请 | 审批回执后仍核对成员与群管准入 |
| 已入群但未过bot验证/必需关注 | 转频道成员设计§15–§16的题目/依赖处理 | 当前题目通过+权限复检；不靠重复邀请或解除绕过bot |
| 本地can_send=false，远端当前权限正常 | 用当前权威事实修复投影，核对bot/观察要求 | 满足全部条件才ready，不调用无必要的邀请/解除 |
| 非互相联系人导致邀请被明确拒绝 | 管理员按既有授权导出邀请链接，被救账号自行加入 | 两个actor各自占用/回执；链接不可用先核查原因，不能只按错误字符串决定解除 |
| 管理员不在群/无权、目标错绑、账号失效或运营明确禁止 | 保存具体原因及相应受保护配置/人工恢复条件 | 不创建假成功或重复权限探测业务动作 |
| 已调用但结果unknown | 同一原invocation只读对账，保持原owner | 有权威终态再决定下一阶段；失败状态/TTL不是重放许可 |

已在群的邀请幂等成功只更新`invite_success/member observed`，不得清掉仍有效的禁言、bot要求或失败原因。救援总完成定义为该账号当前准入ready；“已恢复发言”还要求原合法主业务产生可见远端消息。没有到期主业务或来源时显示“准入已恢复、发言待验证”，不发送额外测试消息凑验收。

### 14.4 触发、连续失败与恢复事件

1. 当前正式准入或发送失败带明确账号/目标权限事实时立即进入原因分类，已有足够证据且可恢复就创建唯一对应步骤，不必制造四次相同失败。连续失败数继续作观测字段；同一Action/Attempt只计一次，重复页面刷新、Planner扫描和监听事件不能增加次数。
2. 模糊网络/权限字符串、Listener私有/不可读只触发所属对象的只读核查，不算普通账号发送失败；Listener自身失去成员资格走其授权范围的恢复并重新证明GetHistory/watermark，不能把换listener等同全部任务账号已准入。unknown不计为已确定权限失败。
3. 触发身份为tenant、Task/current epoch、canonical target、target account/auth generation及当前失败事实/准入requirement version；同一当前恢复原因只允许一个active救援owner。事件新增不能在已有未决副作用旁创建第二条邀请/解除；新权威题目/新限制事件才能开启新的合法阶段。
4. 已有普通业务不继续对已确认不能发言账号制造失败；健康账号继续。相关成员/权限/验证通过事件在持久投影提交时唤醒原准入和Task，由正常Planner重算合法剩余供给；原发言调用unknown保持原义务对账，不因ready恢复重发。
5. 管理员切换只改变未调用、经版本CAS确认的后续处置身份。旧管理员的Gateway-started Action/Attempt/journal保留；旧unknown依据§13.3精确当前成员对账，不能把改配置当旧调用未发生证明。

### 14.5 步骤、真实actor与限流

- 复用现有救援Action关联、payload版本化阶段、Attempt/journal和account-scoped facts，不另建总任务引擎。每个副作用步骤冻结独立Action/command身份、实际actor和request hash，同一执行Action不得中途换account_id或用管理员Attempt包住普通账号调用。原invite_group_account保留管理员邀请职责；解除/导出/审批按明确step purpose形成独立标准Action，普通账号加入复用ensure_target_membership并绑定被救账号。各步骤payload引用同一source_rescue_action_id、当前原因版本和唯一step key，CAS只创建一个后继，使用既有Action唯一性/审计机制；根救援状态由各步骤事实投影，不能由其中一个Action.success直接宣布完成。
- 下一阶段只在前一阶段的必要权威结果已提交后开始。数据库事务不跨网络；阶段完成即释放可释放的本地执行名额，bot等待/审批/冷却持久化并退出线程。管理员操作占管理员资源，被救账号自行加入占被救账号资源；切换actor必须重新取得资格/账号互斥/池/出口资源，不沿用管理员令牌覆盖被救账号调用。
- 救援使用既有管理/准入操作类别容量，不消耗普通发言quantity或伪造普通发言额度；不能以`group_rescue_capacity_override`绕过真实账号/Session并发、既有适用行为节奏或Telegram背压。某管理操作不适用普通发言额度时，明确记为not_applicable而不是跳过全部容量检查。
- FloodWait以原Gateway结构化返回及实际受限authorization/session/方法或peer作用域持久化，由同actor跨Task共享；Task.stats只做展示，不是限流唯一真相。不得从展示文本猜全局冻结范围或只冻结一个Task；未知范围沿既有Gateway保守合同处理，保持其他不冲突actor工作。
- 同invocation未知只对账；明确未调用步骤才能安全重试。物理transport结束证据与业务终态分开，崩溃/超时/管理员切换不解除同操作防重。多个步骤部分成功时从未完成的当前步骤恢复，不从第一步重新邀请/导出/解除。

### 14.6 界面、存量、回滚与QA

界面沿原账号准入明细展示当前原因、当前步骤、管理员范围/可用性、等待条件与下一复检时间，并分列“处置已执行”“准入已恢复”“发言已验证”。保留原失败证据，不把已经邀请成功覆盖仍失败的can_send；不展示凭据、邀请链接明文或无权限访问的验证材料。

存量只读preview冻结部署SHA、Task/目标/管理员世代、原Action/Attempt/journal、当前成员/权限/题目和替代owner集合；本轮不apply。明确未调用项经既有受保护入口迁移阶段，已有unknown/成功只补权威投影，不原地重置pending。没有当前权限/题目证据时报告缺失，不批量重试；回滚停止新阶段，已开始步骤由原版本对账，不清除部分成功证据。

QA必须包含：在群禁言直接解除而非重复邀请、未入群受限先解除再加入、bot要求不被解除替代、当前审批、非联系人双actor流程、管理员丢权限/跨Task共用FloodWait、普通业务资源仍独立、重复事件只一条owner、步骤成功后DB失败/unknown、换管理员/目标/epoch、暂停/删除、ready回接及已有发言unknown不重放。无真实权限恢复或对应可见发言样本时保持该层unproven；程序发布不等于救活。

Product Design Complete：触发事实、原因表、现有能力继承、权限范围、所有权、真实actor、资源与时间、界面、幂等并发、旧unknown/回滚和逐层验收均已闭合，转dev交接但不在本轮实施。

## 2026-09-10 活群修复合同同步

加入和发送按账号/目标独立；正文候选在 SQL LIMIT 前过滤当前准入账号。加入结果投影必须与实际 Gateway 使用的群引用及 Task canonical 目标身份一致，不能依群名猜测或把同名其它群的成功计给本群；历史未知不重放，已有加入事实也不能伪造 C2 ready。 具体实现、开关、测试与发布证据以[AI活群可执行供给、主题与故障兜底修复](ai-group-executable-supply-and-fallback-20260910-prd.md)为准；本条为本切片 `resync`，不改写历史验收记录，也不扩展评论路径。

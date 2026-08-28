# AI 活群准入 epoch、远端事实与目标错绑修复 PRD

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

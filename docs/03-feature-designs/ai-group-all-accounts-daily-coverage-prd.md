# AI 活跃群“全部账号”每日发言履约专项 PRD

## 1. 文档状态

| 项目 | 内容 |
| --- | --- |
| 需求级别 | L3 生产问题修复 |
| 设计状态 | `complete` |
| 产品口径 | 选择“全部可用账号”的 AI 活跃群任务，必须让全部目标账号在每个目标群每天至少真实成功发言 1 次 |
| 任务类型 | 复用 `group_ai_chat`，不新增用户可选任务类型 |
| 统计时区 | `Asia/Shanghai` 自然日 |
| 生产完成条件 | 一个完整北京时间自然日的“任务 × 群 × 账号”矩阵全部完成，或未完成项均以阻塞事实展示；不能用本地测试或部署成功替代 |

## 2. 背景与问题

现有 `all_accounts_daily` 只在 Planner 运行时动态选择当前已准入、可发言账号，并从成功 Action 反查当日覆盖。生产检查证明该实现没有满足“全部账号任务中的每个账号每天都要发言”：

- 任务会频繁重新扫描账号范围，任务数和账号数增长后查询成本按“任务数 × 账号数 × 规划频率”放大。
- 未入群、不可发言、离线或受限账号会被排除出可发言分母，页面可能显示一个被缩小后的覆盖率。
- 上下文过期会跳过同一 Cycle 的剩余 Action，账号的日覆盖义务没有独立持久状态。
- 内容重复或质量过滤失败后，只留下失败 Action，没有可靠的账号级覆盖欠账闭环。
- 覆盖选择、AI 内容生成和上下文生命周期耦合，直接“立即补发”可能制造孤立短句、模板内容或不自然对话。
- 群日上限、活跃窗口和任务小时容量未与全账号目标做一致性预检，配置可能从一开始就无法履约。

2026-07-10 生产规模快照为约 609 个任务候选账号、5 个“全部账号”运行任务，最低形成约 3045 个“群 × 账号”日义务。目标群活跃窗口普遍为 `09:00-23:00`，单群约需 44 条成功消息/小时；当前群 `daily_limit=120` 与单群约 609 条/日的硬目标冲突。该快照只用于容量设计，实际目标数必须由系统按当日账号事实计算。

## 3. 用户原始需求

1. 所有平台内正常、普通身份且 Session 可用的账号，都必须自动加入每个选择“全部账号”的 AI 活跃群任务。
2. 系统必须推动这些账号完成目标群准入。
3. 只有账号在对应目标群真实成功发言后，才算当日完成。
4. 入群失败、不可发言、受限、离线或结果未知的账号必须保留为阻塞项，不能从分母移除后显示 100%。
5. 新录入且满足条件的账号必须自动进入已有“全部账号”任务，不要求人工编辑任务。
6. 账号范围同步不能让每个任务每轮扫描全平台账号。
7. 账号补覆盖不能破坏现有 AI 模拟聊天的话题、上下文、账号人设、回复关系、连发结构和内容质量。

## 4. 产品目标与非目标

### 4.1 产品目标

- 建立持久化的任务账号目标关系和每日覆盖账本。
- 将账号范围同步从“每任务每轮全量扫描”改为“创建时快照 + 账号事件增量同步 + 租户级一致性核对”。
- 将“谁需要发言”的覆盖调度与“说什么、如何自然接话”的 AI 对话生成解耦。
- 按任务活跃窗口和运营曲线平滑分配日覆盖义务，不在临近午夜时突击补量。
- 使用 Telegram 远端消息 ID 确认完成，所有未完成状态可审计。
- 在任务启动和配置更新前证明容量可达；容量不足时明确阻断，不能伪造可完成。

### 4.2 非目标

- 不新增 `task_type`。
- 不绕过账号健康、登录状态、群准入、Telegram 风控、账号容量、群冷却、内容政策或 AI 质量门。
- 不用固定模板、通用短句或 mock success 强行补覆盖；用户选定 A 方案后，唯一例外是本文 10.3 定义的、三层文本生成均失败后且可独立关闭的显式 `emoji_react` 质量兜底。
- 不把 `pending`、`failed`、`skipped`、`unknown_after_send` 或仅创建过 Action 视为完成。
- 不把“立即回补”解释为失败后立即向群发送另一条消息。

## 5. 核心定义

### 5.1 全部账号任务

同时满足以下条件的 `group_ai_chat` 任务：

- `account_config.selection_mode=all`，或旧任务缺少该字段且按 `all` 解释；
- 有效配置经归一化后为 `account_coverage_mode=all_accounts_daily`。

指定账号分组和手动账号任务继续遵循各自范围，不被强制扩大为全平台账号。

### 5.2 目标账号

账号首次进入某北京时间自然日的覆盖目标集合时，必须满足：

- 同租户；
- `status=在线`；
- `account_identity=normal`；
- Session 存在且可解密；
- 未删除、未禁用、未封禁；
- 不属于接码专用、搜索降权专用或救援管理员保留账号；
- 满足任务公共账号池的安全隔离规则。

目标账号是否已入群、是否可发言、当前是否在线保活、是否缺账号面具，不影响其进入当日分母；这些事实决定阻塞状态，不决定是否从分母消失。

### 5.3 当日目标冻结

- 每个北京时间自然日开始时，从任务持久化账号关系生成当日覆盖账本。
- 当天新录入或新恢复为 Session 可用的账号，在目标群活跃窗口结束前进入当日账本。
- 账号进入当日账本后，即使随后离线、受限、Session 失效或不可发言，当日仍保留在分母并显示阻塞。
- 活跃窗口结束后才满足条件的账号，从下一自然日开始履约。
- 删除账号不影响历史账本；后续自然日不再创建新义务。

### 5.4 完成

一个账号的一条当日覆盖义务只有同时满足以下条件才算完成：

```text
Action.status = success
ExecutionAttempt.status = success
ExecutionAttempt.remote_message_id 非空
task_id、group_id、account_id、coverage_date 与覆盖账本一致
```

默认目标为每账号每天 1 条，配置为 2 条时必须分别存在 2 条远端确认成功记录。

## 6. 方案决策

采用“持久化目标关系 + 每日覆盖账本 + 对话片段调度”方案。

不采用：

- 每任务每轮全量扫描：实现简单，但无法随任务数和账号数稳定扩展。
- 仅使用 Redis 账号集合：查询快，但无法可靠支撑重启恢复、日冻结、历史审计和并发去重。
- 失败后立即发送模板补量：会破坏 AI 对话质量并扩大重复和风控风险。

## 7. 账号范围同步设计

### 7.1 初始快照

创建或首次迁移“全部账号”任务时，一次性读取目标账号并批量建立 `TaskMembershipAdmissionItem`，并补齐任务要求的已发布默认规则绑定。存量运行任务在首次被 Planner 识别为 `all_accounts_daily`、但尚无任何持久化账号关系时，执行一次同样的 scope bootstrap；后续常规 Planner 只读取账本，不重新扫描平台账号。该表作为任务与账号的持久化目标关系，既包含已准入账号，也包含待准入和阻塞账号。

### 7.2 增量事件

账号录入、Session 登录成功、账号身份变化、删除或安全状态变化时，在同一数据库事务内写入账号范围事件。内部同步器消费事件并：

1. 找到同租户运行中或待启动的“全部账号”AI 活跃群任务。
2. 幂等新增或更新对应 `TaskMembershipAdmissionItem`。
3. 如果目标群活跃窗口尚未结束，幂等新增当日覆盖账本。
4. 更新账号在线保活来源。
5. 记录同步数量、失败原因和最后消费位置。

事件只处理发生变化的账号。新增 1 个账号且存在 5 个相关任务时，主要写入规模约为 5 条任务关系和 5 条当日义务，而不是重新扫描全部旧账号。

### 7.3 一致性核对

保留租户级低频一致性核对，但它必须：

- 每次只生成一份租户目标账号快照；
- 批量对比所有“全部账号”任务关系；
- 仅修复差异，不为每个任务重复查询全平台账号；
- 暴露扫描时间、发现差异数、修复数和失败数；
- 失败时显式告警，不能静默回退到每任务全量扫描。

Planner 常规循环不得直接全量扫描 `tg_accounts` 来重建任务账号范围。

## 8. 每日覆盖账本设计

新增 `TaskAccountDailyCoverage`，唯一约束为：

```text
(tenant_id, task_id, group_id, account_id, coverage_date)
```

建议字段：

| 字段 | 说明 |
| --- | --- |
| `target_count` | 当日目标成功消息数，1-2 |
| `confirmed_count` | 已有 Telegram 远端确认数 |
| `state` | 当前履约状态 |
| `membership_item_id` | 对应准入关系 |
| `reserved_action_id` | 当前预约的主发送 Action |
| `last_success_action_id` | 最近成功 Action |
| `last_remote_message_id` | 最近 Telegram 消息 ID |
| `blocker_code` / `blocker_detail` | 当前阻塞事实 |
| `next_eligible_at` | 下一次允许进入对话规划的时间 |
| `targeted_at` / `completed_at` | 进入目标与完成时间 |
| `created_at` / `updated_at` | 审计时间 |

状态机：

```text
pending_admission -> admission_running -> ready -> reserved -> sending -> confirmed
                         |                 |          |
                         v                 v          v
                      blocked           ready      ready/unknown
```

状态要求：

- `blocked` 保留在目标分母，记录准入失败、不可发言、离线、受限、Session 失效、缺面具或容量不足。
- 内容生成失败、内容重复、上下文过期或发送前失败时释放 `reserved_action_id`，回到 `ready` 或对应阻塞状态。
- 准入、群可发言或在线事实使账本从非 `ready` 转为 `ready` 时，必须把该转换时刻写为新的 `targeted_at` 调度位置；不得保留早于已推进任务日游标的旧账号事件时间。保持 `ready` 的账本行不得反复改写该位置。
- 群发言权限重查成功后，必须按 `target_group_id` 或 `target_operation_target_id` 解析同一目标群，增量刷新受影响 `draft`、`pending`、`running`、`paused` 全部账号任务的账本；暂停或待启动任务后续启动时不得继续保留旧 `cannot_send` blocker。
- `unknown_after_send` 进入 `unknown`，不计完成，也不能立即重发同一义务；必须先走现有远端复核或人工处理。
- `confirmed_count >= target_count` 后进入 `confirmed`。

## 9. 准入履约设计

- 新目标关系未满足 `TgGroupAccount.can_send=true` 时，复用现有目标准入 Action 推进入群和可发言复检。
- 多个任务同时要求同一账号加入多个群时，准入 Action 必须经过账号全局容量和冷却调度，不能同时并发入群。
- 已入群但不可发言的账号进入 `cannot_send` 阻塞，不生成主互动 Action。
- `cannot_send` / `membership_permission_denied` 群权限阻塞在后续北京时间自然日最多自动复检一次；`unknown_after_send` 只有完成远端补偿复核且复核明确失败为群权限问题后，才适用同一跨日复检。复检仍走原目标准入 Action 和四小时错峰窗口。同一任务、账号、自然日失败后不得再次周期重试，尚未复核的 unknown、目标引用无效和账号不可用不适用该复检。
- 需要关注频道、点击按钮、回答验证或等待人工处理时，沿用现有验证任务和恢复链路。
- 入群失败、验证失败和结果未知必须保留原始 Telegram 错误；不能通过删除任务关系让覆盖率升高。
- 目标 username、peer 或邀请链接无法解析时，按目标级 `target_ref_invalid` 阻塞全部未准入账号；目标引用未变化前不得按账号周期性重复创建相同准入 Action。运营目标的 peer、username 或邀请链接变化后，系统必须用最新引用重新排队未准入账号，或原地刷新仍处于 `pending` 的同一准入 Action。
- 存量目标若仅保存公开链接而没有稳定 Telegram peer，只能由已关联、可发言且具备 Session 的观察账号实际拉取群快照，以公开 username 精确匹配后，在同一事务中更新原 `OperationTarget` 和原 `TgGroup` 的 peer；任务、账本和成员关系继续引用原 ID。若快照没有唯一稳定 peer，或稳定 peer 已归属另一目标/群，必须显式中止，不得按中文标题猜测、创建重复目标或覆盖既有记录；成功规范化必须写审计，再导出新邀请链接并批量重试准入。
- 独立 `target_admission_retry` 的终态不能只更新自己的重试任务：必须按 `target_operation_target_id` 解析目标群，并把同租户、同群且处于 `draft`、`pending`、`running`、`paused` 的全部账号 AI 活群任务中对应账号账本重新按实际 `TgGroupAccount` 状态刷新。准入成功后旧 `cannot_send` 必须回流 `ready` 并重写 `targeted_at`；失败或未知仍按原始 Telegram 结果保留为 `blocked` 或 `unknown`。

## 10. 覆盖调度与 AI 内容解耦

### 10.1 职责边界

覆盖调度器只决定“哪些未完成账号应进入下一段对话”，不生成文案。

现有 AI 对话引擎继续决定：

- 话题方向和讨论老师；
- 真人上下文和引用对象；
- 账号面具、人设、短期立场和表达方式；
- 多账号角色分工、连发结构和转场；
- 事实锚点、语义去重、内容政策和质量过滤。

### 10.2 对话片段

每次规划按三阶段执行：Phase A Planner 在不超过 20 条的短事务中固定 Cycle/slot、账号面具、行为类型、话题和 reply target，原子创建 `ai_generation_status=pending` Action、预约 coverage 并推进任务日游标；Phase B Dispatcher claim 提交后在无数据库事务区间完成 reply/normal 的全部外部 AI 生成与 provider-backed 质量轮次；Phase C 在短事务完成 slot 映射、内容、消息记忆、重复和质量落库，通过后才进入无事务 Telegram Gateway 与短事务 finalize。Planner 禁止 AI、Grok、Telegram 或远端上下文外呼。

完整事务、重试和验收合同见 `docs/03-feature-designs/ai-group-dispatcher-ai-generation-transaction-design.md`。

`messages_per_round` 继续作为单个 Cycle 的 Turn 上限。系统可以根据小时履约目标启动多个 Cycle，但不得修改用户手动设置的单轮 Turn 上限。

### 10.3 失败回补

“立即回补”只表示立即恢复账号的未完成状态并唤醒后续规划，不表示立即向群发送补量消息：

- 内容重复或质量失败：普通文本候选按 M3、M2.5、Grok 三层依次补写。三层均不合格时，已绑定当日 coverage 且非引用的 slot 在租户开启 `ai_group_static_fallback_enabled` 时转为显式 `emoji_react`；其他情况仍由 Phase C 终结 Action、释放预约并等待最新上下文。
- 批量 AI 生成中任一同批 Action 在发送前进入失败或跳过终态时，必须立即释放该 Action 自己的覆盖预约并写入 blocker；不能只同步当前 Dispatcher Action，导致同批其他账号永久停在 `reserved`。
- 新账号准入完成后，账本从 `pending_admission` / `blocked` 进入 `ready` 必须重新进入当前任务日 keyset 游标之后的调度位置；否则其他批次已推进游标时，该账号会永久停在 `ready` 而没有 `send_message`。该重新排队只发生在状态转换，不能在常规 readiness 刷新中持续改变排序。
- 上下文过期：只废弃当前上下文绑定对话片段中仍依赖该上下文或引用锚点的剩余 Action；同一 Cycle 内标记为硬目标、没有 `reply_to_message_id`、并由 Dispatcher 延迟生成文案的普通补量 Action 不得被连带跳过，仍按原 AI prompt、账号面具、话题和质量门生成后发送。被废弃 Action 的相关账号回到 `ready`，不得丢失覆盖义务。
- 每日覆盖债务存在且本轮没有引用回复目标时，Planner 只规划携带账号面具、话题、讨论老师、行为类型和覆盖账本 ID 的延迟生成 Action，不在规划阶段提前冻结普通发言文案。Dispatcher 只批量生成临近执行窗口内的 Action，并在调用原 AI 生成与质量链前刷新目标群最新真人上下文及上下文快照；尚未生成的未来覆盖 Action 不得因旧快照过期被同轮清理。
- 普通 AI 模拟聊天 Cycle 继续严格执行 `reply_min_per_round`。当可引用对象少于该最小值且仍有到期每日覆盖债务时，本轮必须显式转为覆盖回补 Cycle：不得创建数量不足、伪装成达标的引用回复，本轮全部覆盖 Action 按普通发言延迟生成，且不计入普通聊天 Cycle 的引用回复指标。覆盖回补仍必须保留账号面具、话题、讨论老师和行为类型，并经过最新真人上下文刷新、原 AI 生成、语义去重、内容政策、在线状态校验和 Telegram 真实发送确认；禁止模板短句，但允许按上述三层失败契约使用显式、可审计且仍经去重的 `emoji_react`。没有每日覆盖债务时仍等待足量引用对象，不得降低用户设置的最少引用回复数。
- 普通 AI 模拟聊天在没有新真人上下文时继续按空闲续聊配置等待。存在到期每日覆盖债务时不得在 Planner 的“等待新上下文”门禁提前返回；覆盖回补 Action 仍只保存生成 slot，Dispatcher 临近执行时重新读取目标群最新真人上下文并走原质量链。该例外只作用于覆盖债务，不改变普通聊天的上下文等待规则。
- 当日到期覆盖债务大于 0 时，Planner 必须写入两分钟后的 `daily_coverage_next_check_at`，任务调度器取硬小时检查、覆盖检查和普通自然曲线中的最早时间；不得继续按晚间低频曲线等待 7.5–15 分钟。该检查只读取任务当日覆盖账本并扣除 `reserved/sending`，不重新扫描平台账号；债务清零后删除覆盖检查时间并恢复普通自然曲线。
- Planner 的 open-action 门禁对普通任务保持不变；全账号每日覆盖任务必须先用当日账本计算到期债务，并扣除 `reserved/sending` 义务。扣除后债务仍大于 0 时，即使同任务还有少量 open 发送 Action，也必须继续规划其他 ready 账号；不得让单个因账号限频顺延的 Action 阻塞整群覆盖。该判断只读取任务当日覆盖账本，不重新扫描平台账号。
- reply Action 只允许排入未来 5 分钟近端窗口；Planner 仅保存引用目标和上下文快照，不预生成文本。Dispatcher 外呼前重新确认目标消息仍存在、可引用且未过期；失效时不得转为 normal，必须终结 Action、释放 coverage 并由下一轮按新上下文编排。任务显式配置 `context_bound_schedule_window_seconds` 时按显式值执行。
- 账号暂时离线或冷却：记录 `next_eligible_at`，到期后重新参与自然对话。
- 发送失败：按现有失败类型和风控策略处理；可重试失败释放预约后等待下一次合适 Cycle。
- 结果未知：先复核，不立即重复发送。

全账号日覆盖禁止模板短句或伪 AI 成功。三层模型均失败时，只有上述显式 `emoji_react` 可用；必须写入 `quality_fallback=emoji_react`、`human_quality_decision=explicit_static_quality_fallback`、`generation_source/fallback_stage=static_safe_fallback` 和原始失败原因，并继续通过消息记忆与真实发送确认。开关关闭或兜底仍被门禁拒绝时，覆盖账本保持未完成并展示具体原因。

## 11. 时间与节奏设计

### 11.1 日目标分配

当日总目标为：

```text
目标账号数 × per_account_daily_min_messages
```

系统使用任务 `hourly_activity_curve` 在目标群活跃窗口内分配日目标；未配置曲线时按活跃小时均匀分配。每小时计算：

```text
当前时刻累计应完成数 - 当前已远端确认数 = 当前履约欠账
```

当前小时只为欠账和本小时目标规划必要 Cycle，避免全天均匀高频或临近窗口结束集中突击。

### 11.2 容量预检

任务创建、启动、账号范围变化和节奏配置变化时，后端必须重新计算：

- 当日目标消息数；
- 活跃窗口可用秒数；
- 群冷却允许的理论槽位；
- 启用硬小时目标时，单小时 `hourly_min_messages`、当前小时规划缺口（含历史补量）与群冷却理论槽位；
- `max_actions_per_hour` 在活跃窗口内的任务容量；
- 账号全局小时/日容量和冷却；
- 群 `daily_limit`；
- 预计准入缺口和当日剩余时间。

只有以下条件全部成立，才允许标记为“容量可履约”：

```text
group.daily_limit >= 当日目标消息数 + 已保留的普通对话预算
群冷却理论槽位 >= 当日目标消息数
任务小时容量总和 >= 当日目标消息数
账号聚合容量 >= 当日目标消息数
启用硬小时目标时：floor(3600 / group_cooldown_seconds) >= max(hourly_min_messages, 当前小时规划缺口)（未设置群冷却时不限制）
```

系统不得静默提高群日上限、降低群冷却或绕过风险配置。容量不足时，预检和任务详情必须展示当前值、最低需要值和差额，并阻止新任务启动；已有运行任务进入显式 `coverage_capacity_blocked` 或 `hard_hourly_group_cooldown_insufficient` 运行阻塞，不得显示可按时完成。硬小时目标或当前小时补量超过群冷却单小时槽位时，Planner 不得继续创建必然在 bucket 到期后跳过的 `hard_hourly_target=true` Action；Recovery 必须将遗留的 pending/claiming 硬小时 Action 标记为 `hard_hourly_group_cooldown_insufficient` 并释放覆盖预约，避免继续挤占点赞、浏览或评论调度。若同一 `all_accounts_daily` 任务的日覆盖容量证明仍为 `sufficient`，该硬小时阻塞不得停止日覆盖：Planner 必须继续按 `daily_coverage_due_debt` 创建不携带 `hard_hourly_target`、`hard_hourly_bucket` 或 `hard_hourly_deficit_at_plan` 的覆盖 Action，且详情页分别展示硬小时阻塞和日覆盖进度。运营人员应用推荐值并保存后才生效，所有调整写审计日志。

全账号容量证明始终以冻结的全部目标账号为分母，`pending_admission` 和 `cannot_send` 账号不得从中删除，也不得被伪造为完成；但准入失败不能反向停止已经确认 `can_send=true` 的账号发言。若全账号容量缺口只来自待准入或不可发账号，而当前可发账号按其剩余覆盖义务计算的容量为 `sufficient`，Planner 必须继续为可发账号创建 `send_message`，任务运行阶段显示为部分履约并同时保留全账号覆盖未达标和准入缺口。只有当前可发账号自身的剩余目标容量也不足时，才停止创建发送 Action。部分履约绝不等同于全账号日目标完成。

Dispatcher 取件时，显式 `target_admission_retry` 仍优先处理；其余常规 Action 中，AI 硬小时优先级必须高于 `search_join_membership` 和严格 `search_join`，避免搜索点击积压持续挤占 AI 活群目标。`hard_hourly_target=true` 且可直接执行的 `send_message` 必须先于一般 `ensure_target_membership` 和 `ensure_channel_membership`。已记录 `required_channel_admission_pending` 的发送仅在同任务、同账号仍存在未完成准入 Action 时排在该前置之后；能直接解除这类发送前置条件的准入 Action 必须先于普通硬小时发言，避免已知阻塞发送反复占用取件名额或其前置准入被持续饿死。前置准入成功后，该发送必须恢复为可直接执行的硬小时优先级，不得继续被无关入群队列饿死。其他准入 Action 仍不抢占已确认 `can_send=true` 账号的硬小时发言优先级。任务列表的硬小时状态必须由当前 bucket 的 Action 成功事实定期刷新，不能沿用历史 bucket 的 `met` 快照。

Telegram 调用前还必须执行最终运行时校验：Dispatcher 在 `TgGroup` 行锁内先核对当前北京时间是否处于 `active_window`，再统计本群已持久化的 `before_call`、`gateway_call_started`、`success`、`result_unknown` 槽位及旧消息发送成功事实；仅在活动时段、群日上限和群冷却均允许时，写入并提交当前 `ExecutionAttempt(before_call)`，随后才可调用 Telegram。活动时段外的 Action 必须延后到下一次群活跃窗口开始；命中群日上限时，Action 必须延后到下一自然日的群活跃窗口开始；命中群冷却时延后到冷却结束。三者都不调用 Telegram，也不得落入通用的一秒重试；覆盖预约和消息记忆继续保留，不能伪造失败或完成。

### 11.3 当前生产容量裁决

当前约 609 个目标账号、`09:00-23:00` 活跃窗口、60 秒群冷却，理论上每群约有 840 个发送槽位，满足单账号每日 1 条的最低目标；但 `daily_limit=120` 不满足约 609 条最低目标。后续生产修复必须先显式调整群日上限或目标范围，不能只发布 Planner 代码后声明恢复。

## 12. 并发、幂等与性能

- `TaskMembershipAdmissionItem` 对 `(task_id, account_id)` 保持唯一。
- `TaskAccountDailyCoverage` 使用日唯一约束，防止重复义务。
- Planner 每批最多 20 条，只在数据库短事务通过原子状态更新或 `SELECT ... FOR UPDATE SKIP LOCKED` 创建 pending Action、预约 `ready` 行并推进游标；不得在事务内外呼。
- Action 使用现有 `action_dedupe_key`，并包含覆盖账本 ID 和目标序号。
- `cycle_id + slot_id` 固定账号、coverage、reply/normal、面具、话题和连发位置；Dispatcher AI 批次必须按 slot 一一返回，不能因切批串账号或改变 `messages_per_round`。
- 成功结果与账本 `confirmed_count` 在同一事务中回写；重复消费同一远端结果不得重复计数。
- Planner 只按 `(task_id, coverage_date, state, next_eligible_at)` 索引分页读取欠账，不扫描全部账号。
- 在线保活必须让全部 `desired_online=true` 账号进入处理，不得设置隐藏的账号总量上限或内部前 N 个截断；到期状态按显式页大小分页，未覆盖账号由后续页或后续 drain 继续处理。Telegram 健康探测按显式配置受控并发并使用独立的有界超时，结果按完成顺序流式返回，主线程逐条落库，不能等待整页完成后集中提交；账号、凭证读取和状态落库必须留在数据库 Session 所在线程。主线程冻结本批账号与凭证后必须先结束读取事务再进入 Telegram 网络调用，逐结果提交必须保留本批已加载状态，禁止因 `expire_on_commit` 隐式逐账号回表形成数据库连接抖动或中断整批。分页、并发和单次网络超时只控制调度吞吐，不限制服务上线账号总量；完整探测周期必须短于 `stale_after` 窗口，不能因串行探测或整批等待持续制造假离线。
- 列表和详情摘要使用账本聚合或每日统计快照；账号明细走分页接口，不把几百个账号 ID 写入 `Task.stats`。
- 以当前 5 个任务、约 609 个目标账号计算，每日约 3045 条账本记录，属于普通数据库可控规模。

## 13. API 与前端设计

### 13.1 API

现有创建和更新接口继续承载覆盖配置：

- `POST /api/tasks/group-ai-chat`
- `POST /api/tasks/group-ai-chat/create-and-start`
- `PATCH /api/tasks/{task_id}/group-ai-chat`

详情增加或稳定投影：

| 字段 | 说明 |
| --- | --- |
| `target_account_count` | 当日冻结目标总数，完成率分母 |
| `confirmed_account_count` | 已远端确认完成账号数 |
| `remaining_account_count` | 未完成账号数 |
| `pending_admission_count` | 待准入和准入中账号数 |
| `cannot_send_count` | 已关联但不可发言账号数 |
| `offline_or_session_blocked_count` | 在线或 Session 阻塞数 |
| `unknown_after_send_count` | 结果未知数 |
| `coverage_percent` | `confirmed / target` |
| `capacity_status` | `sufficient` / `blocked` |
| `required_daily_messages` | 当日最低目标消息数 |
| `required_hourly_rate` | 按剩余窗口计算的最低成功速率 |

新增分页明细接口：

```text
GET /api/tasks/{task_id}/account-coverage?date=YYYY-MM-DD&state=&page=&page_size=
```

接口返回账号、准入状态、覆盖状态、成功数、远端消息 ID、最后失败、下一可执行时间和阻塞原因。

### 13.2 前端

任务详情必须展示：

- 今日真实覆盖：`远端确认完成 / 当日全部目标账号`；
- 状态分解：待准入、不可发言、离线/Session、内容阻塞、发送失败、结果未知、已完成；
- 容量证明：目标量、群日上限、小时容量、理论槽位、差额；
- 按状态筛选和分页查看账号级明细；
- 新账号最近同步时间和账号范围事件积压；
- “应用推荐容量”操作必须要求确认并写审计，不能静默保存。

页面不得使用 ready 子集作为覆盖率分母，也不得把 Action 数、计划数或 `unknown_after_send` 显示为成功。

## 14. Listener 边界

覆盖账本不依赖 Listener 才能保存账号义务，但自然对话仍依赖最新群上下文。Listener 的 Telegram 拉取必须有明确超时；单个群拉取阻塞不得卡住整个 listener 主循环。

- 本地主循环心跳和数据库周期心跳必须区分，周期线程存活不能覆盖主循环停滞。
- 上下文不可用时，AI 引擎按现有接话/暖场/沉默规则决定是否生成；覆盖调度不得为了补量强制生成低质量内容。
- Listener 不健康时显示 `listener_blocked`，覆盖账本继续保留欠账。

## 15. 权限、安全与审计

- 只有具备任务编辑权限的后台用户可调整覆盖模式和容量。
- TG bot 继续只允许租户管理员修改允许暴露的 AI 活群设置，不开放群日容量静默调整。
- 账号范围事件、目标关系变化、容量调整、准入操作、覆盖预约、释放、成功确认和人工处理全部写审计。
- 不记录明文 Session、手机号、代理凭据或 AI 供应商密钥。

## 16. 边界场景

| 场景 | 产品要求 |
| --- | --- |
| 任务创建时没有目标账号 | 阻止启动，显示账号范围为空 |
| 新账号在活跃窗口内就绪 | 当日加入目标、创建准入义务和覆盖账本 |
| 新账号在活跃窗口结束后就绪 | 自动进入下一自然日，不制造无法发送的当日义务 |
| 账号进入目标后离线或 Session 失效 | 当日不缩分母，显示阻塞；恢复后继续履约 |
| 账号未入群或不可发言 | 继续准入/复检，不生成主发送，不计完成 |
| 内容重复或质量失败 | 不发送，不计完成，账号回到待编排状态 |
| 上下文过期 | 仅废弃相关对话片段，覆盖义务保留 |
| Telegram 返回结果未知 | 不计完成且不立即重发，先复核 |
| 容量不足 | 新任务阻止启动；运行任务显式阻塞并展示差额 |
| 多 Planner 并发 | 同一日义务只能被一个 Action 预约 |
| 修改为非全部账号模式 | 停止创建新的全账号义务；已生成的未来覆盖 Action 按配置重排，历史账本保留 |
| 任务暂停 | 不继续准入或发言；当日账本保留并标记任务暂停阻塞 |

## 17. 数据迁移与兼容

1. 旧 `selection_mode=all` 的 `group_ai_chat` 即使存量 `account_coverage_mode=natural`，继续按 `all_accounts_daily` 有效模式处理。
2. 发布迁移为运行中和待启动的全部账号任务批量建立 `TaskMembershipAdmissionItem`。
3. 为发布当日建立 `TaskAccountDailyCoverage`，只从当日真实远端确认成功记录回填 `confirmed_count`。
4. `pending`、`failed`、`skipped`、`unknown_after_send` 不回填完成。
5. 旧动态扫描统计保留只读对照一个发布周期，不再作为页面主口径；对照不一致必须告警。
6. 指定账号分组和手动账号任务不迁移为全平台范围。
7. 迁移后执行容量预检；不足的生产任务显示 `coverage_capacity_blocked`，由运营确认推荐容量后恢复履约。

回滚时保留新增账本和审计数据，停用新协调器并恢复旧 Planner 读取路径；不得删除已确认的远端成功事实。回滚只代表运行路径恢复，不代表当日覆盖完成。

## 18. QA 验收

### 18.1 账号范围与性能

- 创建全部账号任务时一次性建立目标关系和当日账本。
- 新账号 Session 就绪后自动进入所有相关任务，无需编辑任务。
- Planner 常规循环不全量扫描 `tg_accounts`。
- 一致性核对只做租户级一次快照，并可见报告差异。
- 600 账号、10 任务规模下，目标同步和欠账查询使用批量写入与索引分页，不出现逐账号逐任务 N+1。

### 18.2 日覆盖事实

- 当日目标进入后离线、受限或不可发言，分母不减少。
- 只有成功 Action、成功 Attempt 和非空远端消息 ID 同时存在才计完成。
- 失败、跳过、待执行和未知发送均不计完成。
- 每账号目标为 2 时，单条成功不能提前完成。
- 并发 Planner 不会重复预约同一账号同一目标序号。

### 18.3 AI 内容质量

- 覆盖层只改变账号 slot 优先级，不改变话题、老师、账号面具、引用和质量门。
- 失败后不发送模板或通用短句补量；仅允许 10.3 定义的显式 `emoji_react` 质量兜底，且不计为高质量 AI 文本。
- 上下文过期只废弃当前片段，下一片段使用新上下文重新生成。
- 同一对话片段保持多账号角色分工、账号表达差异和语义去重。
- reply 目标消失/过期不转 normal；AI 成功但落库失败只记生成结果未知，不得混同 `unknown_after_send` 或进入 Telegram。

### 18.4 容量与页面

- 目标量超过群日上限、小时容量或冷却理论槽位时，预检明确阻止启动。
- 页面分母等于当日目标总数，不等于 ready 数。
- 所有阻塞分类可下钻到账号和原始错误。
- 容量推荐不会自动覆盖用户配置；应用后有审计记录。

### 18.5 生产验收

- 发布后账号范围事件无积压，新账号能进入所有相关任务账本。
- Listener 主循环心跳持续新鲜，单群超时不会拖死整个循环。
- 覆盖账本中的 `confirmed` 均能关联 Telegram 远端消息 ID。
- 观察一个完整北京时间自然日，逐任务导出“群 × 账号”矩阵；所有目标账号均完成才可写 `production_fixed`。
- 若存在未完成账号，必须以准入、权限、在线、内容、容量、发送或未知结果分类展示，状态只能是 `production_blocked` 或 `production_unproven`。

## 19. Release Gate

本需求为 L3 生产问题修复，`production_related=true`，必须按：

```text
prod-diagnosis -> product -> dev -> qa -> product -> release gate -> prod-diagnosis
```

Release Gate 至少验证：

1. 数据迁移和回滚脚本可重复执行。
2. 账号事件增量同步与租户级一致性核对通过。
3. 日账本并发预约、失败释放和远端确认测试通过。
4. AI 对话内容回归通过，不出现覆盖模板补量。
5. 容量预检对当前生产任务给出明确结果。
6. listener 超时和主循环健康检查通过。
7. `master -> release -> GitHub Actions Deploy Production` 发布成功。
8. 发布后使用 SSH 和生产数据库做真实矩阵取证。

`qa_pass` 不等于产品接受，`product_accepted` 不等于线上恢复，部署成功也不等于完整自然日覆盖已经成立。

## 20. Product Handoff

### 20.1 Dev 必须交付

- 账号范围事件和增量同步器。
- 任务账号持久关系初始化与迁移。
- `TaskAccountDailyCoverage` 模型、迁移、状态机和分页查询。
- 覆盖欠账调度、原子预约、失败释放和远端确认回写。
- 目标准入与覆盖账本联动。
- 容量预检、推荐值和运行阻塞。
- AI 对话片段与覆盖账号 slot 解耦，移除覆盖模式的低质量兜底补量。
- listener Telegram 拉取超时和主循环健康语义修复。
- 前端真实分母、状态分解、容量证明和账号级明细。
- 数据流转索引、结构索引、迁移说明和生产诊断脚本。

### 20.2 QA 必须提供

- 红绿回归证据和完整自动化测试结果。
- 600 账号、多任务并发与查询次数证据。
- AI 内容对比样本，证明覆盖调度未破坏自然对话。
- 容量不足、准入失败、内容失败、上下文过期、结果未知和 listener 超时测试。
- 生产验收查询或脚本输出格式。

## 21. Product Design Complete 自检

| 检查项 | 结论 |
| --- | --- |
| 原始需求全部覆盖 | 是：全平台账号、自动入任务、自动准入、每日真实发言、阻塞不缩分母、新账号自动加入 |
| 功能与前端状态完整 | 是：目标、准入、ready、预约、发送、确认、阻塞、容量和分页明细均有定义 |
| 后端/API/Worker 设计完整 | 是：事件同步、持久关系、日账本、调度、Dispatcher、Listener、迁移和回滚均已定义 |
| 数据流转完整 | 是：账号事件到目标关系、日账本、AI 对话、真实发送和远端确认闭环明确 |
| 权限与安全完整 | 是：权限、审计、敏感数据和风险上限均有约束 |
| 边界场景完整 | 是：新增、掉线、受限、未入群、质量失败、上下文过期、未知发送、暂停和配置变更均有口径 |
| 并发与幂等完整 | 是：唯一约束、原子预约、Action 去重和成功幂等回写明确 |
| 发布、迁移与回滚完整 | 是：L3 Release Gate、迁移、容量处理、回滚和生产 E4 证据明确 |
| design_status | `complete` |

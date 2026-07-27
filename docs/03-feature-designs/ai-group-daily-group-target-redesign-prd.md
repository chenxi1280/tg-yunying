# AI 活群“群日目标 + 全账号必达 + 账号面具内容记忆”重构 PRD

## 1. 文档状态

| 项目 | 内容 |
| --- | --- |
| Intake ID | `intake-2026-07-27-ai-group-daily-group-target-001` |
| 需求级别 | L3：现网长期无法按时按量完成 |
| 设计状态 | implemented_locally（代码与迁移工具已实现；发布和生产恢复尚未验证） |
| 适用任务 | `group_ai_chat` |
| 产品目标 | 每个目标群按自然日完成配置总发送量，并保证冻结范围内每个账号至少真实成功发送 1 条 |
| 内容质量目标 | 正常正文绑定发送账号和该账号面具并执行同账号最近 10 天硬去重；缺面具账号仅以精确 `签到` 完成当天账号覆盖，不用于额外补量 |
| 保留硬门禁 | 目标群准入、账号登录/可发事实、正常正文账号面具、内容安全与质量、Telegram 真实限制；缺面具 coverage 走受限签到 |
| 删除运行门禁 | 日覆盖容量阻断、硬小时目标、AI 活群活动时段禁发、AI 活群本地群日上限/群冷却阻断 |

本文 supersede 以下旧口径：

- `per_account_daily_min_messages / per_account_daily_max_messages` 作为运营目标；
- 旧配置按“冻结账号数 × 每账号最低条数”迁移；
- `hard_hourly_target_enabled / hourly_min_messages / hard_hourly_strategy`；
- “剩余理论容量不足就停止创建发送 Action”；
- `TgGroup.active_window` 对 `group_ai_chat` 的禁止发送语义；
- `TgGroup.daily_limit / group_cooldown_seconds / legacy_group_slot` 对 `group_ai_chat` 的本地发送阻断语义；
- 同租户所有活群共享 1 小时、7 天、30 天历史内容硬去重；
- 所有日覆盖都固定发送 `签到`、绕过账号面具或普通内容质量管线；新合同只允许“缺面具账号的当日最低覆盖”使用受限 `签到`。

本文不改变频道评论、搜索加群、搜索点击、转发等其他任务类型的窗口和调度规则。

## 2. 产品决策

1. 运营只配置单个群的 `daily_message_target`。
2. 冻结范围内每个账号每天至少成功发送 1 条；这是账号级覆盖义务，不是“所有账号合计 1 条”。
3. 默认群日目标等于冻结账号数，旧“每账号 2 条”不能自动迁移成两倍目标。
4. Planner 只按当前应完成进度持续补债，不再先证明全天一定能完成。
5. 静默时段降低权重和批量，但保持非零发送。
6. 未准入或不可发只阻塞对应账号；缺面具账号走受限 `签到` 覆盖，其他 ready 账号继续。
7. 每条 AI 活群正文必须由该账号当前固化的 active 面具参与生成和校验。
8. 历史内容硬去重窗口统一为滚动 10 天，所有权是账号；其他账号说过相同或相似内容，不得硬阻塞当前账号。
9. 只有 Telegram 真实发送成功且有非空 `remote_message_id` 才完成群日总量和账号覆盖。

## 3. 唯一目标合同

### 3.1 运营配置

创建和编辑只暴露：

```text
daily_message_target: int >= 1
```

含义：该任务的单个目标群在任务时区自然日内需要取得的真实成功消息总数。

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
daily_message_target = 当前任务冻结账号数
```

若创建时账号范围尚未确定，页面先显示预计账号数；任务启动冻结范围后写入正式日目标快照。

### 3.2 全账号覆盖

每个任务、目标群、账号、自然日只有一条覆盖义务：

```text
TaskAccountDailyCoverage.target_count = 1
```

当日实际目标：

```text
effective_daily_target = max(daily_message_target, frozen_account_count)
```

运营配置小于冻结账号数时：

- 不拒绝创建或启动；
- 保存运营配置值；
- 页面明确展示“全账号至少 1 条，因此实际最低目标为 N 条”；
- Planner 按实际目标执行。

### 3.3 存量迁移

旧任务迁移规则固定为：

```text
daily_message_target = frozen_account_count
```

不得读取旧 `per_account_daily_min_messages=2` 并迁移为 `frozen_account_count × 2`。旧字段只留历史审计，不再形成新目标。

若任务已经存在由新合同人工保存的 `daily_message_target`，保留该显式值，再应用账号数下限。迁移必须 dry-run、逐任务审计且幂等。

旧覆盖行统一改成 `target_count=1`；已真实确认至少 1 条的账号视为覆盖完成。历史 Action、Attempt 和远端结果不改写。

存量开放 `direct_check_in` 按当前事实迁移：

- 已绑定未完成 coverage，且账号确实没有任何可用 active 面具、面具状态允许兜底：转换为 `mask_missing_check_in`，保留原 admission、可见性和账号绑定；
- 已绑定未完成 coverage，但账号已有可用 active 面具：原 Action 标记 `superseded_by_masked_coverage_generation`，释放未进 Gateway 的预约并用当前面具重新规划；
- 未绑定 coverage：标记 `superseded_by_daily_group_target`，不得作为额外补量继续发送；
- 已进入 Gateway、`unknown_after_send` 或历史 success：不改写真实结果，继续按远端核验和审计口径处理。

## 4. 日范围冻结与首日语义

### 4.1 自然日冻结

北京时间每天 00:00 为运行中的任务建立：

- `target_date`；
- `scope_frozen_at`；
- `frozen_account_count`；
- 冻结账号清单；
- `configured_message_target`；
- `effective_message_target`；
- 当日面具可用性和准入状态的初始投影。

当天新增账号从下一个自然日进入正式分母；账号被禁用、未准入、缺面具或临时离线不得从已经冻结的分母删除，只更新账号级 blocker。

### 4.2 新任务首日

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

## 5. 全天节奏，不设小时硬目标

### 5.1 累计应完成量

系统使用 24 个非零小时权重形成累计进度，不创建小时完成义务：

```text
due_by_now =
floor(
  effective_daily_target
  × elapsed_cumulative_weight
  ÷ full_day_total_weight
)
```

静默时段权重低于正常时段但必须大于 0。运营不配置每小时目标；权重只决定“现在应规划多少”，不形成小时门禁或小时失败状态。

### 5.2 本轮规划需求

```text
volume_need_now =
max(0, due_by_now - confirmed_message_count - valid_open_send_count)

coverage_need_now =
当日进度应覆盖、当前 ready、未确认且没有有效开放 Action 的账号数

planning_need =
max(volume_need_now, coverage_need_now)

batch_size =
min(max_concurrent - current_open_count, planning_need)
```

`batch_size <= 0` 只表示当前无需新增或队列已满，不得写成全天容量不足。Planner 下次按进度、Action 终态或账号状态变化继续计算。

### 5.3 两阶段选账号

1. 优先选择当日未覆盖、已准入、在线且可发的账号；有面具走正常生成，无面具只可创建覆盖专用 `签到`。
2. 当前应覆盖账号均已有有效 Action 后，若群总量仍欠缺，从已覆盖账号中按“当天成功数最少优先 + 最久未发优先 + 稳定轮换”选择额外消息账号。
3. 额外消息只计群日总量，不创建第二条账号覆盖义务。
4. 质量失败释放该 Action 的内容预约和覆盖预约，以新 variation 重新规划；不得把失败计成功。

Dispatcher 必须给已到期的 AI 群日债务公平领取机会，不能被搜索、准入或历史积压长期饿死；这是一条调度公平合同，不是新的容量门禁。

## 6. 门禁重新归类

| 检查项 | 新行为 | 影响范围 |
| --- | --- | --- |
| 剩余日容量估算 | 只展示风险，不停止规划 | 无硬阻断 |
| 硬小时目标 | 删除 | 不再生成或统计 |
| AI 活群活动窗口 | 删除禁发 | 全天可规划和发送 |
| 静默时间 | 非零降量 | 仅影响节奏 |
| 本地群日上限/群冷却 | 对 `group_ai_chat` 删除 | 不影响其他任务类型 |
| 目标群 membership / bot admission | 保留 | 只阻塞对应账号 |
| 账号登录、在线、权限、安全策略 | 保留 | 只阻塞对应账号 |
| 账号面具可用性 | 正常正文保留 | 缺面具仅允许当日覆盖专用 `签到`，禁止额外补量 |
| 内容安全、事实、上下文、面具匹配、10 天去重 | 保留 | 只阻塞对应候选/Action |
| Telegram SlowMode/FloodWait/权限结果 | 保留 | 按真实返回延后或失败 |
| `unknown_after_send` | 保留占位，先核验 | 禁止自动重发 |

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

固定 `签到` 不是正常账号的日覆盖主路径。它有两种来源：

1. `check_in_fallback`：面具可用账号的普通非引用最后兜底。它仍绑定面具并参与该账号 10 天硬去重。
2. `mask_missing_check_in`：缺面具账号的当日最低覆盖专用兜底。

`mask_missing_check_in` 固定规则：

- 正文只能是精确 `签到`；
- 必须绑定 `coverage_ledger_id`，且该覆盖行属于同一任务、群、账号和日期；
- 持久兜底义务键为 `task_id + group_id + account_id + target_date + content_source`；
- Action 幂等键追加 `fallback_attempt_no`；同一兜底义务同时最多一条 open/unknown Action；
- 明确未进入 Gateway 或 Telegram 明确失败且可安全重试时，关闭原 Action、释放预约、递增 `fallback_attempt_no` 后再建；`unknown_after_send` 继续占位，严禁新建替代 Action；
- 不要求 `account_mask_id/version`，但必须写 `mask_status=missing`；
- 不进入普通 10 天精确、语义或模板重复硬门禁，否则无法每日兜底；
- 仍经过准入、在线、账号安全、出站安全、会话轮换和 Telegram 真实限制；
- 只完成该账号当天 `target_count=1` 的覆盖，不得用于群总量额外补量、引用回复或高质量内容指标；
- 面具恢复后新建 Action 立即恢复正常面具生成；已进入 Gateway 的兜底保留真实结果，未进入 Gateway 的合法兜底可继续执行，不需竞态改写。

引用回复不得降级为任何 `签到`；所有兜底失败都不能伪造成功。

### 7.4.1 哪些面具状态允许缺失兜底

只有账号属于正常运营用途、没有可用 active 面具，且面具资产/生成状态属于以下之一时允许：

```text
missing
queued
generating
retry_wait
manual_required
```

以下状态不得用签到绕过：

```text
disabled
unusable
identity_invalid
account_usage in {code_receiver, rank_deboost, mismatch}
```

已有 active 面具但新版本正在生成或生成失败时，继续使用旧 active 版本，不属于缺面具兜底。`mask_status`、生成 item 状态和兜底资格必须分别保存，不能用一个布尔值混合。

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
| `account_mask_id/lineage_id/version/contract_version` | 正常正文使用的面具证据；`mask_missing_check_in` 为空 |
| `mask_status/content_source` | `active + generated/check_in_fallback` 或 `missing + mask_missing_check_in` |
| `mask_generation_status/fallback_eligible` | 缺失原因、恢复进度及是否允许签到 |
| `fallback_obligation_key/fallback_attempt_no` | 缺面具每日兜底义务和安全重试序号 |
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

上述状态过滤同时要求 `content_source != mask_missing_check_in`；缺面具签到只参与 coverage 唯一性和 `unknown_after_send` 防重，不参与普通内容相似度查询。

明确未进入 Gateway 的失败在释放原子预约后不再阻塞新候选，但记录保留审计。`unknown_after_send` 在核验前继续占位，防止同账号重复发送。

在线硬去重数据至少保留到 `dedupe_expires_at`。超过 10 天的内容可留审计归档，但不得继续参与硬去重。

## 9. 日目标数据模型

新增每日群目标快照 `TaskGroupDailyTarget`：

| 字段 | 含义 |
| --- | --- |
| `tenant_id/task_id/group_id/target_date` | 唯一日目标 |
| `daily_fulfillment_phase` | `admission_warming/full_day_committed` |
| `scope_frozen_at` | 当日范围冻结时间 |
| `configured_message_target` | 运营配置值 |
| `frozen_account_count` | 当日冻结账号数 |
| `effective_message_target` | 两者最大值 |
| `confirmed_message_count` | 远端确认总数缓存 |
| `coverage_confirmed_account_count` | 已至少发送 1 条账号数缓存 |
| `due_message_count` | 当前累计应完成量 |

缓存必须可从 Coverage、Action、Attempt 重算，不能成为成功真相。

## 10. 前端与诊断

创建/编辑页：

- 只显示“该群每天发送总量”；
- 默认等于当前账号数；
- 明示“所有账号每天至少成功发送 1 条”；
- 删除每账号最少/最多、硬小时目标；
- 静默文案改为“降量发送”；
- 内容质量说明为“每个账号按自己的面具生成，并检查该账号最近 10 天内容”。

任务详情分别展示：

- 群日目标：配置、实际、当前应完成、已确认、欠额；
- 账号覆盖：冻结、已确认、未覆盖；
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
或 Action.content_source = mask_missing_check_in 且绑定唯一 coverage_ledger_id
AiGroupMessageMemory.account_id = Action.account_id
正常正文：AiGroupMessageMemory.account_mask_version = Action.account_mask_version
缺面具签到：AiGroupMessageMemory.mask_status = missing
```

`pending`、Action 创建、AI 生成成功、入群成功和 `unknown_after_send` 均不计完成。

## 12. 验收标准

### 12.1 产品与迁移

1. 新任务只配置群日总量，默认等于账号数。
2. 旧每账号 2 条任务迁移后目标等于冻结账号数，不产生两倍目标。
3. 覆盖行统一 `target_count=1`。
4. 中途启动显示预热日，下一自然日进入完整承诺日，不能连续重置预热。

### 12.2 规划与发送

1. 部分账号未准入或离线时，其他 ready 账号仍持续规划和发送；缺面具但其他条件满足的账号创建覆盖专用 `签到`。
2. 预计当天无法完成时，Planner 仍按 `due_by_now` 创建有界 Action。
3. 静默时间存在非零发送，且平均批量低于正常时段。
4. AI 活群不因 `active_window`、`daily_limit`、`group_cooldown_seconds` 停止。
5. 不产生新的硬小时 Action、统计或容量 `PlanAbort`。
6. Dispatcher 有竞争负载时，已到期群日债务仍持续获得领取机会。

### 12.3 内容质量

1. 所有正常 AI 活群正文都有账号和面具版本证据；无证据消息只能是合法 `mask_missing_check_in`。
2. 同账号 10 天内精确、语义、模板或同观点重复被拒绝。
3. 不同账号历史内容不会产生跨账号 `duplicate_message` 硬阻断。
4. 面具升级或回滚后，同账号 10 天历史仍参与去重。
5. 缺面具账号只生成一条绑定当日 coverage 的精确 `签到`，不生成普通正文或额外补量；面具恢复后回到正常生成。
6. 质量失败重新生成新 variation，失败 Action 不计覆盖或群总量。
7. 超过 10 天的内容不再参与硬去重。
8. disabled、unusable、identity_invalid 和非普通运营用途不能借缺面具签到绕过。
9. 缺面具签到明确失败可以递增 attempt 安全重试，`unknown_after_send` 时不能创建替代发送。

### 12.4 完整生产日

只对 `full_day_committed` 的北京时间完整自然日验收。每个运行任务必须满足：

```text
coverage_confirmed_account_count = frozen_account_count
confirmed_message_count >= effective_message_target
strict account/action/attempt mismatch = 0
success attempt without remote_message_id = 0
success message without mask evidence or valid mask_missing_check_in evidence = 0
ineligible mask_missing_check_in count = 0
mask_missing_check_in extra-volume credit count = 0
duplicate open/unknown mask_missing_check_in obligation count = 0
cross_account_duplicate_hard_block_count = 0
new hard_hourly Action count = 0
daily_coverage_capacity_insufficient count = 0
active_window AI skip/defer count = 0
```

同时报告准入完成率、内容接受率和按账号拆分的 10 天重复拒绝。全部成立才能标记 `production_fixed`。

## 13. Product Handoff

开发顺序：

1. 修正总 PRD、专项 PRD和数据流索引中的旧签到、跨账号去重和旧目标迁移口径；
2. 落地群日目标、范围冻结、预热日和累计进度模型；
3. 将消息记忆硬去重改为账号所有权并固化面具证据；
4. 删除容量、硬小时、活动窗口和本地群槽位阻断；
5. 调整 Planner、Dispatcher 公平性、前端和诊断；
6. 执行可审计存量迁移；
7. 走 `master -> release -> GitHub Actions Deploy Production`；
8. 使用完整北京时间自然日完成 E4 验收。

QA 必须验证准入、账号面具和质量仍能阻断对应账号/Action，其他 ready 账号不会被连带停止，且其他任务类型规则未被误删。

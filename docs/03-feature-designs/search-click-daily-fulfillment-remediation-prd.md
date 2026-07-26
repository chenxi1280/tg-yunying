# 搜索目标群点击每日履约修复 PRD

## 1. 文档状态

| 项目 | 内容 |
| --- | --- |
| 需求级别 | L3 生产问题修复 |
| 设计状态 | complete |
| 变更状态 | 开发实现与本地回归已纳入当前 `release`；部署、真实 Telegram 结果与完整自然日验收仍待 Release Gate 证明 |
| 适用任务 | strict_daily_target=true 的 search_join_group |
| 统计时区 | Task.timezone，默认 Asia/Shanghai |
| 关联线上证据 | 2026-07-26 生产只读取证 |

本专项补正 search-click-boost-prd.md 中的严格日目标容量、claim 仲裁、极搜协议状态与事实观测合同。它覆盖旧文档中“严格搜索优先级”相互矛盾的表述：严格目标不能被 AI hard_hourly 饿死，也不能通过静态绝对排序反过来饿死 AI；必须按可证明的当期履约需求分配 Dispatcher 份额。

## 2. 生产问题与已证事实

当前运行任务 河南郑州学生会 搜索目标群点击 每日点击 1000 次（加入目标 80 次）在 2026-07-26 03:09 的事实为：

| 指标 | 事实 |
| --- | --- |
| 点击确认 | 0 / 1000 |
| membership_observed 确认 | 0 / 80 |
| 待执行 Action | 47 条 search_join，2 条 search_join_membership |
| 当前执行状态 | 49 条均为 pending，无 claim owner、claim token 或 Gateway 开始事实 |

已证根因：

1. 配置 max_actions_per_hour=20，日曲线只有 20 个可执行小时；严格上界为 20 × 20 = 400，全天满跑上界也只有 480，小于点击目标 1000。按 actions_per_round=1 与当前曲线权重，常规曲线产能更低。
2. 同时到期队列中有 334 条 AI hard_hourly，而搜索仅 47 条严格 source 与 2 条 membership child。现有 claim 排序把 hard_hourly 放在严格搜索前，fairness 记录 higher_priority_due；搜索 Action 长期不被领取。
3. 历史上 8 条 source 已进入 Gateway，三次重试后终态为 jisou_group_selector_missing，原始详情为 极搜群聊类型选择按钮缺失。
4. 实机证据表明该错误混合了两种不同状态：部分账号收到正常搜索结果分类页，部分账号关键词后进入 热搜排行榜 页面。后者不是“已在正确搜索结果页但群聊 selector 缺失”。
5. join_request_pending 和 membership_pending 均不是 membership_observed。远端申请待审批无法实现 80 的成员关系确认目标。

## 3. 产品目标与非目标

### 3.1 目标

1. 在创建、编辑、日切和运行中都能证明每日点击目标是否在当前时间、曲线、静默、日预算、账号能力和 Dispatcher 份额下可达。
2. 不可达目标必须在开始前拒绝，或在存量运行任务中显式报告 daily_target_capacity_insufficient；不能继续显示为正常严格履约。
3. 严格搜索点击与 AI hard_hourly 同时存在时，二者按照实时需求获得可审计的 Dispatcher claim 份额；任何一方都不能长期饿死另一方。
4. 极搜会话状态偏离、真实 selector 缺失、机器人验证和未知协议页面必须分开记录，不能使用一个 selector_missing 掩盖全部问题。
5. 点击、申请待审批、membership_observed、Gateway 开始和 ExecutionAttempt 必须各自有独立事实，不得用 stats 或 pending 代替成功。

### 3.2 非目标

- 不提高账号、代理、授权槽位、IP、全局冷却、Telegram Gateway 或机器人协议的安全上限来硬冲目标。
- 不自动修改用户设定的 max_actions_per_hour、静默窗口、日目标或账号池。
- 不把 join_request_pending、membership_pending、unknown_after_send、Action success 但缺少事实字段写成 membership_observed。
- 不在极搜未知页面点击外部 URL、unknown effect button 或未被协议样本批准的 callback。
- 不以临时 exclude_task_ids、手工 drain 或 mock click 作为日目标修复。

## 4. 每日目标可达性合同

### 4.1 三层容量与事实边界

系统必须同时计算常规曲线、严格排程和远端确认三个不同层次的量；三者不得互相替代。

~~~text
normal_curve_capacity =
  sum over 可执行窗口:
    min(max_actions_per_hour, actions_per_round × hourly_round_curve[hour])

strict_hour_ceiling =
  sum over 可执行窗口:
    min(max_actions_per_hour, 该窗口扣除已 claim / 已 Gateway source 后的剩余小时槽位)

account_source_capacity =
  所有合格账号在该日剩余可用 source 槽位之和

max_source_attempts =
  min(剩余 max_actions_per_day, strict_hour_ceiling, account_source_capacity)

strict_planning_capacity =
  max_source_attempts 扣除严格排程中已确定不可执行的行为节奏决策
~~~

`source_capacity` 仅保留为兼容字段，其语义固定等同 `max_source_attempts`；新接口、页面和错误详情必须使用 `max_source_attempts`。它是最多可进入真实 source 尝试的上界，不是目标群一定出现、点击一定被 Telegram 确认的承诺。`target_click_observed` 才是唯一点击完成事实。

可执行窗口必须同时满足：

- 在 Task 时区的目标自然日内，且尚未越过 scheduled_end；
- 未完全落入 quiet_hours，按窗口剩余分钟计算部分小时，不能把已过时间重新计入；
- hourly_round_curve 对应小时大于零；
- 账号日限额、全局冷却、next slot、授权槽位、代理、分片资格和协议样本均处于可执行状态；
- 当前 bucket 已存在的 source carry、claim 或 Gateway 尝试已从该 bucket 和账号槽位扣除。

运行时必须显式返回 `occupied_source_count`、`current_hour_source_occupied` 与 `current_hour_available`。当前严格 slot 的行为跳过键固定为 `strict_capacity_action_key(timezone, effective_date, hour, slot)`；创建 Action 时将相同 key 写入 `planning_slot_key`，使创建/编辑预检、日切重算与运行 Planner 使用同一 task ID 与同一确定性种子。

`normal_curve_capacity` 只展示普通节奏，`strict_hour_ceiling` 是不考虑行为跳过时的小时硬上界，`strict_planning_capacity` 才是 strict Planner 可以实际排入的 source 数。严格模式不得静默忽略 `daily/hourly/action skip_probability`、整窗口跳过或其他行为节奏决策：预检和运行规划必须使用同一确定性调度种子，已判定 skip 的窗口或 Action 直接从 `strict_planning_capacity` 扣除，并持久化原因。若需要更高容量，只能由受控编辑修改节奏配置，不能由 strict Planner 私自绕过。

每日点击目标大于 `strict_planning_capacity` 时，系统必须返回 `daily_target_capacity_insufficient`，携带上述分量、已扣除窗口、账号 blocker 和最早可调整项。目标位于 `normal_curve_capacity` 与 `strict_planning_capacity` 之间时，只能在 `strict_daily_target=true` 下运行；Planner 必须按剩余目标和剩余可执行窗口形成严格追赶计划，不能仍按普通曲线的一轮一条慢速节奏宣称可达。

`capacity_feasible` 只是排程必要条件。只要目标尚未由远端事实确认，点击结果仍是 `at_risk`；当 `confirmed_click_count + 尚未消费的 strict_planning_capacity < daily_click_target_count` 时，当日状态才是数学上不可达的 `blocked`。

### 4.2 创建、编辑与存量任务

1. 新建和编辑严格每日点击任务前，必须以首个完整可执行自然日计算 `strict_planning_capacity`。`daily_click_target_count` 超过该值时拒绝保存或启动；页面同时展示“今天剩余容量预览”，但不得用部分天的不足拒绝一个从明日才开始的有效任务。
2. existing task 在日切、配置变更、账号池显著变化、分片变化或剩余时间容量下降时，以当前自然日的真实剩余窗口重新计算。若已不可达：
   - 任务保持 running 仅用于收口已进入 Gateway、已 claim 或成员关系 child；
   - 写 daily_outcome=blocked、blocker_code=daily_target_capacity_insufficient；
   - 停止创建会让已知上界进一步失真的新 source；
   - 运营必须通过受控编辑调整目标、账号池、可执行时段或每小时上限后才恢复严格规划。
3. 每日结论以 `Task.timezone + local_date` 为键，不得把今天的 blocked 或 at_risk 粘连到明天。创建、编辑预检、今天剩余预览和运行时重算必须明确标注各自的 effective_date 与完整/部分天语义。
4. 不得因 replacement budget 存在就把失败 source 当作额外初始容量。replacement 只能消耗已预留的 source 尝试槽位用于收口真实未命中，不能证明 1000 点击可达。
5. 页面必须显示 normal_curve_capacity、strict_hour_ceiling、account_source_capacity、max_source_attempts、strict_planning_capacity、行为节奏扣减原因、当前 confirmed、remaining、有效日期和日结论。

### 4.3 点击与成员关系双目标

daily_click_target_count 只由 target_click_observed 与 target_found_at 统计。daily_target_count 只由 membership_observed 与 membership_observed_at 统计。

成员关系目标的预检分为两层：

| 结果 | 条件 | 产品含义 |
| --- | --- | --- |
| membership_capacity_feasible | daily_target_count 不超过 max_source_attempts，且存在可执行 membership child 路径 | 可以尝试，但不承诺远端审批结果 |
| membership_external_risk | 目标群只返回申请待审批、无直接加入证据或 child 被外部权限阻断 | 当日成员关系目标 at_risk 或 blocked，不能显示可保证达成 |
| membership_observed | Telegram 真实成员关系复核成功 | 唯一可增加成员关系确认数的事实 |

申请已提交只能写 join_request_pending 或 membership_pending，并保留 source 点击事实；不得增加 membership_observed。

## 5. Dispatcher 份额仲裁

### 5.1 设计原则

固定排序不足以解决两个同时严格的任务族。系统采用“先保留不可替代准入，再按当期履约需求分配严格份额”的合同：

1. 与同一 task、同一 account、同一目标绑定的 target_admission_retry 先处理。
2. 已产生 target_click_observed 的 search_join_membership child 在严格份额内优先于新的 source，避免已点击事实无法收口。
3. 严格 search source 与 AI hard_hourly send 都按剩余时间内的 required_claims 申请 Dispatcher 份额。
4. 若总 required_claims 超过当前可用 Dispatcher 份额，系统显式写 shared_dispatch_capacity_insufficient；不得让一个类别默默吞掉所有 slot。
5. 普通 Action 只使用严格份额分配后的剩余 capacity。

### 5.2 全局 Claim Window、分片 Allocation 与 Reservation

四个 Dispatcher 不能各自把同一份并发 capacity 预留四次。容量权威分四层：`DispatchClaimScope` 保存跨 Window 的 executing active ledger，`DispatchClaimWindow` 只管理当前 bucket 的分配，`DispatchClaimShardAllocation` 把其中一部分授予账号 shard，`DispatchClaimReservation` 才把该 shard 的份额分给严格类别：

| 对象 | 字段 | 说明 |
| --- | --- | --- |
| DispatchClaimScope | dispatcher_scope、claim_capacity、active_claim_count、version | 一个真实共享 worker/队列/数据库 claim 域唯一一行；每次规划先按所有 `executing + dispatch_claim_active` Action reconcile，跨 bucket 的 active claim 始终占用全局容量 |
| DispatchClaimWindow | dispatcher_scope、bucket_start、bucket_end | 唯一定位真实共享 worker/队列/数据库 claim 域的一个窗口；唯一约束为 `(dispatcher_scope, bucket_start, bucket_end)` |
| DispatchClaimWindow | claim_capacity、active_claim_count、unclaimed_allocated_count、allocation_epoch、version | `claim_capacity` 是该 scope 的有效 claim_limit / 并发总额，不得由 Action 数量推测；全局不变量为 `active_claim_count + unclaimed_allocated_count <= claim_capacity` |
| DispatchClaimShardAllocation | dispatch_claim_window_id、account_shard_total、account_shard_index | 一个 Window 对一个账号 shard 的已授予份额；唯一约束为 `(window, shard_total, shard_index)`，不能让每个 shard 各自拥有完整全局 capacity |
| DispatchClaimShardAllocation | required_claims、active_claim_count、unclaimed_allocated_count、reason、version | 记录 shard 实际可领取候选造成的需求和获配；其 active / unclaimed 总和必须与全局 Window 账一致 |
| DispatchClaimReservation | dispatch_claim_shard_allocation_id、tenant_id、task_id、claim_class、bucket_start | Reservation 只能附属于一个 shard allocation；唯一约束为 `(shard_allocation, tenant, task, class)` |
| DispatchClaimReservation | required_claims、reserved_claims、claimed_count、urgency_score、reason、version | 记录类别需求、获配、真实成功 claim 与 CAS 版本；`claimed_count <= reserved_claims`，未消费额为 `reserved_claims - claimed_count` |

`dispatcher_scope` 必须等于实际共享 worker/队列/数据库 claim 域的稳定标识；`account_shard_total/index` 必须来自当前 account shard 配置。tenant 级 `DispatchFairnessCursor` 不能代替全局 scope 或 shard 的仲裁；公平轮转 cursor 的作用域必须与 `dispatcher_scope + shard` 一致。

普通 Action 为了让 Window 的 `active_claim_count + unclaimed_allocated_count` 保持真实全局账，也可以以 `claim_class=ordinary`（或既有非严格类别）的 Reservation 记录其剩余份额；它只能在严格类别分配完成后建立，绝不能减少或复用严格类别尚未消费的 Reservation。`allocation_epoch` 是同一 Window/shard 重新分配和同分轮转的持久化种子，未消费 Reservation 只能在新的 epoch 明确释放原因后才可重分配。

每个 Window 在短事务中先读取真实有效总 capacity、全 scope active claims 和所有 due shard 的严格需求；先给 shard 授予不超过全局余额的 Allocation，再在 shard 内分配 Reservation。顺序为：不可替代的 `target_admission_retry`、已点击的 membership child、严格 search source 与 AI hard-hourly；后两类按 `urgency_score` 比例并以持久化轮转处理同分。无论有几个 Dispatcher 进程，所有 shard 的 active 加未消费预留总和都不得超过同一全局 Window capacity。这样既不会让 AI hard_hourly 永久压制搜索，也不会让 1000 条搜索 source 静态排在 AI 前面耗尽全部 worker。

若 aggregate `required_claims` 超过全局 Window 或某个 shard Allocation 的可用余额，所有受影响严格任务必须写 `shared_dispatch_capacity_insufficient` 及其 scope、shard、epoch、所需与可用数量；这只说明 shared claim 资源不足，不得承诺目标一定不能由其他尚未计算的远端因素完成。

### 5.3 可验证的领取规则

一次 claim 只能领取仍有 `reserved_claims - claimed_count`、且通过账号、静默、截止、授权环境和 Gateway 前置条件的 Action。只有 `_confirm_claim` 成功后，才可在同一短事务增加 Reservation.claimed_count、将对应 shard/window 的 `unclaimed_allocated_count` 减一并将其 `active_claim_count` 加一；失败、过期或安全门拒绝不得消费份额。Action.result 必须记录：

~~~text
dispatch_claim_class
dispatch_reservation_id
dispatch_claim_window_id
dispatch_claim_shard_allocation_id
dispatch_claim_scope
dispatch_claim_shard
dispatch_allocation_epoch
dispatch_reservation_reason
dispatch_urgency_score
dispatch_unserved_strict_classes
~~~

一个严格类别有 due Action、`reserved_claims > claimed_count` 且通过安全门时，另一个严格类别不得占用其 Reservation。无可领取安全 Action 时，Reservation 必须保留真实 reason，而不是把它转给其他类别后仍报告该任务可达；只有新的 `allocation_epoch` 在同 scope/shard 重新核算后，才可释放或再分配未消费份额，并写明 release reason。Action 终结时必须从 window 与 shard 的 active 账释放对应 claim。任何 Reservation 只保证 claim 机会，不保证 Telegram 目标出现、点击或成员关系确认。

## 6. 极搜协议与会话状态

### 6.1 页面相位分类

关键词发送后的机器人响应必须先按已审核、版本化的 `BotProtocolSample` 指纹分类。分类优先级固定为 `verification_page > hot_list_page > search_category_page > group_result_page > unknown_page`；不能仅因“没有群聊 selector”就推断它是搜索分类页。响应不匹配任何当前样本时必为 `unknown_page`。

| page_phase | 处置 |
| --- | --- |
| search_category_page | 允许按当前批准的群聊 selector 进入群聊结果页 |
| group_result_page | 允许精确目标匹配、分页和目标点击 |
| hot_list_page | 进入一次受控同机器人会话重置，不得直接查找群聊 selector |
| verification_page | 写 bot_human_verification_required，停止 Action 并告警 |
| unknown_page | 写 jisou_protocol_page_unknown，不点击任何未知 button |

### 6.2 受控会话重置

当 `page_phase=hot_list_page` 时，同一 source Action 只允许执行一次以下同机器人重置序列：

~~~text
发送 /start
  -> 等待响应
  -> 发送原关键词
  -> 再次分类 page_phase
~~~

执行重置前必须以 `action_id + recovery_kind=hot_list_reset` 在 `SearchJoinProtocolTrace`（或等价恢复账本）中原子创建唯一记录并 CAS 标记 `reset_started`；该记录必须携带批准的 protocol sample version。重复投递、worker 重启、超时恢复或同一 Action 的重试看到既有记录后，不得再次发送 `/start`，只可读取已有恢复结果或收口为 `jisou_session_state_deviated`。没有匹配的已批准样本版本时，重置不得执行，写 `jisou_protocol_page_unknown`。

重置后仍不是 `search_category_page` 或 `group_result_page` 时，Action 终态为 `jisou_session_state_deviated`。不得点击 热搜排行榜 页面中的外部 URL、未知 callback 或 群组导航 外跳链接；不得将该结果写成 `jisou_group_selector_missing`。

只有已经确认是 search_category_page，且没有协议样本批准的群聊 selector 时，才写 jisou_group_selector_missing。该错误才进入当前账号的 selector_missing 可用性排除；jisou_session_state_deviated 只按正常账号冷却和后续协议复核处理，不能误伤为 24 小时 selector 不可用。

### 6.3 协议样本与审计

每次 source 必须写 `SearchJoinProtocolTrace`，至少含：

- bot_username、protocol_sample_version、page_phase；
- attempt_no、event_type、分类输入摘要、重置账本状态和前后 page_phase；
- 每个 button 的 row、col、button_type、effect、text_length、导航标识及批准样本匹配标记；
- 被选择 selector 的位置、批准样本版本和点击结果；
- 重置是否执行、唯一 recovery_kind、次数和前后页面相位；
- 关键词只保留 hash，不得写明文。

只有经过人工审核且属于协议类别的受控文案，才可持久化固定的 `normalized_text` 枚举值；其他机器人正文、目标群名、用户内容和按钮原文不得落库，只能保留不可逆 hash、分类、长度、类型、effect 和导航标识。按钮文案或布局变体只有在新的真实样本经人工审核并版本化后才能加入 selector 规则；不得在生产对未知文案做模糊点击。

### 6.4 已审批样本 JSON 合同

极搜的 active `BotProtocolSample.structure_json` 必须包含一个完整、版本化的 `page_fingerprints` 集合；仅有旧版 `buttons` / `effect` 摘要不构成可执行协议样本。四个必需相位为 `verification_page`、`hot_list_page`、`search_category_page`、`group_result_page`，每个相位可有多个已审批变体。允许字段仅为：

~~~json
{
  "page_fingerprints": [
    {"page_phase": "verification_page", "text_enums": ["human_verification"]},
    {"page_phase": "hot_list_page", "text_enums": ["hot_list"]},
    {
      "page_phase": "search_category_page",
      "button_text_enums_any": ["jisou_group_category", "jisou_channel_category"],
      "selector_rules": [
        {
          "row": 0,
          "col": 0,
          "button_type": "callback_data",
          "effect": "unknown",
          "normalized_text": "jisou_group_category"
        }
      ]
    },
    {"page_phase": "group_result_page", "button_effects_any": ["join_candidate", "navigate_only"]}
  ]
}
~~~

`normalized_text` 只能取受控枚举 `human_verification`、`hot_list`、`jisou_group_category`、`jisou_channel_category`；运行时只在内存中以对应受控文案比对，落库只保留 hash、长度与 `approved_sample_match`。`selector_rules` 必须固定 row、col、callback 类型、effect 和 `jisou_group_category` 枚举；不能按包含关系、未知 callback 或动态原文猜测 selector。`group_result_page` 可使用已批准的 `join_candidate` / `navigate_only` effect 变体，但只允许点击被当前指纹命中的目标或分页位置。

Planner 必须把校验后的 profile 与 `protocol_sample_version` 一起冻结进 source payload；Dispatcher 对旧 Action 或缺 profile 的极搜 payload 在 Gateway 前写 `protocol_sample_invalid` 并结束 Attempt，不得回退到硬编码猜测。最后一道顺序固定为：先复核任务仍 running、截止与静默窗口、授权/代理环境，再校验 profile，最后才写 `gateway_call_started_at` 并调用 Gateway；缺 profile 不得遮蔽 `task_not_active`、`scheduled_end_reached`、`quiet_hours_active` 或授权环境原始错误。旧样本需要由人工基于真实、脱敏采集重新审核和版本化；系统不得自动把旧 `buttons` 摘要升级为已审批指纹。

## 7. ExecutionAttempt 与事实观测

source Action 在 Gateway 调用前必须原子创建或取得同一个 `ExecutionAttempt(attempt_no)`，先写 `before_gateway`，再写 `gateway_call_started_at`。Gateway 成功、明确失败、超时或进程异常均回写该 Attempt：调用后无法确认结果时写 `unknown_after_send`，调用前被门禁跳过时写 `skipped_before_gateway`。不得为一次 Gateway 调用新建多个 Attempt，也不得把 `Action.result.gateway_call_started` 当作唯一事实源。

任务详情必须将以下量分开：

- pending / claiming / executing；
- 已进入 Gateway 但结果未知；
- target_click_observed；
- join_request_pending；
- membership_pending；
- membership_observed；
- jisou_session_state_deviated；
- jisou_group_selector_missing；
- daily_target_capacity_insufficient；
- shared_dispatch_capacity_insufficient。

历史缺少 ExecutionAttempt 的 Action 只作为历史观测缺口展示，不能被反推为“没有进入 Gateway”或“已成功”。

## 8. 前端、接口与权限

创建和专用编辑预检必须返回：

~~~text
normal_curve_capacity
strict_hour_ceiling
account_source_capacity
max_source_attempts
strict_planning_capacity
behavior_pacing_unavailable_count
behavior_pacing_unavailable_reasons
capacity_effective_date
capacity_day_kind
dispatch_scope_summary
membership_capacity_status
remaining_executable_hours
daily_outcome_preview
~~~

任务详情新增当日履约面板：

- 点击与成员关系两个独立进度；
- 当前自然日容量证明、完整/部分天语义、行为节奏扣减和不足原因；
- Claim Window / Reservation 按 scope、shard、类别、需求、预留、已领取和未服务原因；
- 极搜 page_phase、重置、selector、验证和协议版本；
- 账号级阻塞列表与原始错误。

所有编辑仍要求 tasks.manage；查看协议 trace、账号和成员关系明细仍受 tasks.view 与目标访问权限控制。没有“强制成功”“跳过协议”或“自动降低目标”的写入口。

## 9. QA 验收

| 场景 | 必须证明 |
| --- | --- |
| 20 个可执行小时、20 次每小时、1000 点击目标 | 预检拒绝，返回 strict_hour_ceiling=400 与 strict_planning_capacity |
| 目标在常规曲线与严格上界之间 | strict 模式按剩余小时形成追赶计划；普通曲线不被误报为足够 |
| strict 配置仍含整窗口或 Action skip | 跳过决策进入 strict_planning_capacity 扣减和审计；Planner 不得绕过行为节奏假报可达 |
| 今日仅剩部分窗口、明日可完整执行 | 分别展示当前部分日和首个完整日；今天 blocked 不粘连到明天 |
| 存量任务日中变得不可达 | daily_outcome=blocked，已有 Gateway/child 正常收口，不再假装严格目标可达 |
| 目标群未出现在结果中 | source 尝试可计入 max_source_attempts，但不增加 target_click_observed；日结果保持 at_risk 或按剩余尝试变为 blocked |
| 两个 shard、多个 Dispatcher 同时 claim | 同一 scope 的全局 Window 及各 shard Allocation 都不超配；claimed_count 不超过 reserved_claims，aggregate capacity 不被多进程重复预留 |
| 同时存在严格搜索与 AI hard_hourly | 二者均有持久 Window / Reservation；无长期 pending 饥饿；容量不足时双方收到 shared_dispatch_capacity_insufficient |
| 已点击 child 与新 source | child 先获得自己的严格份额，且不改变 source 的授权槽位 |
| 热搜排行榜页重复投递或恢复 | hot_list_reset 账本唯一，最多发送一次 /start；仍偏离时写 jisou_session_state_deviated，零未知 button 点击 |
| 正确分类页无 selector | 才写 jisou_group_selector_missing；未知文本不以原文持久化 |
| 验证页面 | 写 bot_human_verification_required，不误报 selector 缺失 |
| 待审批 | 仍为 membership_pending，不增加 membership_observed |
| Gateway 开始后异常 | 同一 ExecutionAttempt 收口为 unknown_after_send，未知状态不计成功 |

## 10. 发布门与生产验收

1. 先完成容量预检、Reservation 并发、极搜页面相位、ExecutionAttempt 和前端投影的自动化回归。
2. 用真实协议样本和小账号池 canary 验证 group_result_page、hot_list_page、selector 缺失和待审批四条路径；未通过样本不得扩大任务。
3. 发布必须走 master -> release -> GitHub Actions Deploy Production。
4. 发布后先验证至少一个可执行小时内 Reservation 的分配、claim、Gateway 和事实回写，再观察完整 Asia/Shanghai 自然日。
5. 只有 click confirmed 大于等于 daily_click_target_count 且 membership observed 大于等于 daily_target_count，且无未收口 unknown，才可写 production_fixed。否则按 production_blocked 或 production_unproven 报告。

## 11. Product Design Complete 自检

| 检查项 | 结论 |
| --- | --- |
| 原始问题 | 不可达容量、行为跳过、claim 跨分片超配、极搜状态混淆、membership 未确认、观测缺口均已覆盖 |
| 前端状态 | 容量、双目标、Reservation、协议相位和账号 blocker 已定义 |
| 后端与 Worker | 预检、Planner、Dispatcher、Reservation、Gateway 与 Attempt 合同已定义 |
| 数据流 | 从目标配置到点击、成员关系和远端事实链路已定义 |
| 权限与安全 | 保留所有账号、代理、协议和 Telegram 门禁；禁止未知按钮回退 |
| 边界与并发 | scope/shard Window、四 worker 份额、日切、部分天、静默、截止、待审批、未知结果与 reset 幂等已覆盖 |
| QA 与发布 | 回归、canary、完整自然日 E4 证据已定义 |
| design_status | complete |

### 11.1 当前 release 实现映射

- `search_join_daily_capacity.py` 用当前/后续小时 source 占用和行为决策扣减严格容量；`search_join_pacing.py` 查询 carry、claim、Gateway source 占用。
- `executors/search_join_group.py` 以剩余目标和剩余可执行小时追赶，严格模式不再受普通 `actions_per_round` 上限截断；不足时把 `daily_target_capacity_insufficient` 及容量证明写入 `stats.search_join_stats.daily_fulfillment`。
- `dispatch_claim_*.py` 以 `DispatchClaimScope` 把跨 Window 的 active claim 纳入容量账本，详情同时显示当前 Window 与全局 scope 数值。
- 迁移 `0122_dispatch_claims_protocol_trace.py` 创建 scope/window/shard/reservation 与极搜协议 trace；自动化回归覆盖跨 Window 容量、严格 carry 扣减、种子一致性和 slot 去重。

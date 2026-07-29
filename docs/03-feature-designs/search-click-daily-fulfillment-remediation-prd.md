# 搜索点击每日履约修复 PRD

## 1. 文档状态

| 项目 | 内容 |
| --- | --- |
| 需求级别 | L3 生产问题修复 |
| 设计状态 | complete |
| 设计复核 | 2026-07-28 已闭合纯 `search_click`、唯一新建入口、稳定 ordinal、projection/commit、跨 Task assignment、start CAS、首次 Reservation 独占、持久 problem/component/unit 快照、提交前全输入 `SERIALIZABLE` 重验、一次性搜索 outcome、逐 unit release item 守恒、重叠 trigger/release-vs-claim 竞态、`precondition_lost` 禁止 Gateway 后倒退、CAPTCHA 明确远端通过事实及回滚后独立 quarantine；搜索求解或绑定不能完成时放弃全部未领取 unit，仅非空有效释放通过中央新 epoch 原子重建分片权重 |
| 变更状态 | 2026-07-28 搜索点击固定为纯 click；“搜索点击加入”仅登记为后续独立模式，本轮不设计、不实现 |
| 适用任务 | `task_type=search_click`、`search_execution_mode=click_only`；`strict_daily_target` 仅作存量兼容 |
| 统计时区 | Task.timezone，默认 Asia/Shanghai |
| 关联线上证据 | 2026-07-26 生产只读取证 |

本专项补正 search-click-boost-prd.md 中的严格日目标容量、claim 仲裁、极搜协议状态和点击事实观测合同。搜索 click source 与 AI 群日到期发送必须按当期履约需求分配 Dispatcher 份额；历史 `AI hard_hourly` 只保留取证含义，不再是当前 claim class。

## 2. 生产问题与已证事实

历史混合任务“河南郑州学生会 搜索目标群点击”在 2026-07-26 03:09 的事实为：

| 指标 | 事实 |
| --- | --- |
| 点击确认 | 0 / 1000 |
| membership_observed 确认 | 0 / 80 |
| 待执行 Action | 47 条 search_join，2 条 search_join_membership |
| 当前执行状态 | 49 条均为 pending，无 claim owner、claim token 或 Gateway 开始事实 |

已证根因：

1. 配置 max_actions_per_hour=20，日曲线只有 20 个可执行小时；严格上界为 20 × 20 = 400，全天满跑上界也只有 480，小于点击目标 1000。按 actions_per_round=1 与当前曲线权重，常规曲线产能更低。
2. 历史同时到期队列中有 334 条 AI hard_hourly，而搜索仅 47 条严格 source 与 2 条 membership child。旧 claim 排序把 hard_hourly 放在严格搜索前，fairness 记录 higher_priority_due；该证据用于证明固定优先级会导致搜索 Action 长期不被领取，新实现不得继续创建 hard-hourly 类别。
3. 历史上 8 条 source 已进入 Gateway，三次重试后终态为 jisou_group_selector_missing，原始详情为 极搜群聊类型选择按钮缺失。
4. 实机证据表明该错误混合了两种不同状态：部分账号收到正常搜索结果分类页，部分账号关键词后进入 热搜排行榜 页面。后者不是“已在正确搜索结果页但群聊 selector 缺失”。
5. 该历史任务还混入了 membership 义务，导致 click 产量问题与准入问题互相遮蔽；本 PRD 将纯 click 合同独立出来，历史 membership 事实只作事故取证，不进入纯搜索点击成功口径。

## 3. 产品目标与非目标

### 3.1 目标

1. 合法配置直接创建成功，不执行容量预检、不要求风险确认；任务开始后才建立真实 task-day ledger 并计算运行风险。
2. `daily_click_target_count` 是必须持续追赶的业务目标。曲线、静默、skip 和 jitter 只用于安全容量有余量时分散执行，不能减少日目标或形成永久跳过。
3. 严格搜索点击与 AI 群日到期发送同时存在时，二者按照实时需求获得可审计的 Dispatcher claim 份额；任何一方都不能长期饿死另一方。
4. 极搜会话状态偏离、真实 selector 缺失、机器人验证和未知协议页面必须分开记录，不能使用一个 selector_missing 掩盖全部问题。
5. 点击、Gateway 开始和 ExecutionAttempt 必须各自有独立事实，不得用 stats、Action success 或 pending 代替 click 成功。
6. “搜索点击”不再承载 membership/admission 义务；运营只配置目标、关键词、每日 click 数和账号组，账号路径与重试顺序由系统自动完成。

### 3.2 非目标

- 不提高账号、代理、授权槽位、IP、全局冷却、Telegram Gateway 或机器人协议的安全上限来硬冲目标。
- 不自动修改用户设定的日目标或账号池；完成优先调度可以在既有硬安全上限内压缩普通曲线、skip、jitter 和静默低权重。
- 不把 unknown_after_send、Action success 但缺少 click 事实字段写成 `target_click_observed`。
- 不在本 PRD 设计或实现“搜索点击加入”；只保留其后续独立模式占位。
- 不在极搜未知页面点击外部 URL、unknown effect button 或未被协议样本批准的 callback。
- 不以临时 exclude_task_ids、手工 drain 或 mock click 作为日目标修复。

## 4. 每日目标可达性合同

### 4.1 运行期完成优先合同

系统在任务开始后同时计算业务欠额、硬安全可执行量和远端确认；创建阶段不计算这些值。

~~~text
remaining_click_count =
  max(daily_click_target_count - confirmed_click_count, 0)

planning_click_deficit =
  max(due_click_target_count - confirmed_click_count - held_click_count - unknown_click_count, 0)

hard_safe_attempt_capacity =
  min(
    账号/关键词剩余安全额度,
    授权与代理可用额度,
    协议样本与当前实际 CAPTCHA 状态可执行额度,
    Gateway/Dispatcher 在 deadline 前剩余合法额度
  )

catch_up_required =
  projected_eligible_attempt_capacity_before_deadline < remaining_click_count
~~~

`target_click_observed` 是唯一点击完成事实。`remaining_click_count` 是业务欠额，绝不扣除 held/unknown；`planning_click_deficit` 才扣除在途和 unknown 以防重复建单。`due_click_target_count` 由软曲线产生，但进入 catch-up 后必须按剩余时间向完整日目标加速，不能长期停留在低曲线。`hard_safe_attempt_capacity` 只是当前可合法创建/执行的尝试量，不承诺目标一定出现，也不替代完成数。

`projected_eligible_attempt_capacity_before_deadline` 来自不写资源的只读精确 projection，只计算当前硬安全事实允许、可在业务 deadline 前获得中央 Claim Window 机会的“尝试上界”，并明确标记 `projection_not_reserved=true`。它不能命名为或解释成 projected confirmed clicks，也不能与 `confirmed_click_count` 相加后声称可完成；上界小于欠额时可以证明存在容量风险，上界大于等于欠额只表示仍有尝试空间，不承诺目标会出现或点击会确认。

CAPTCHA 不使用触发率或 AI 历史成功率预测。尚未进入验证页的路径只可贡献一次 eligible attempt，不贡献任何预测确认；本次 source 一旦实际进入 `verification_image_page`，必须冻结 `challenge_fingerprint_hash=hash(bot_peer,message_id,image_hash,ordered_callback_fingerprint)`。只有该 fingerprint 的单次批准答案提交取得明确远端通过回执，或进入已审批的 `search_category_page|group_result_page`，才能写入 `jisou_image_verification_solved` 并继续目标搜索/点击；仅离开原页、消息消失、超时、hot-list、unknown 或出现新 fingerprint 都不能冒充 solved，`required|failed` 以及 required 下的 unavailable/unknown 原因对可继续执行的 click opportunity 贡献 0。验证码 AI 调用及其批准重试不消费账号/关键词 click 限额、任务 click 目标或额外 Dispatcher/Gateway click 份额，也不计入 AI 活群/评论的主 AI 三轮、备用 AI 三轮或业务 AI 生成次数；它只复用当前 source Action 的在途占位和既有账号 session ownership，在当前 fingerprint 收口前不得让另一条搜索 Action 并发改写同一账号—协议会话。识别不设置业务固定 AI 轮数或递归次数；单个供应商候选未通过 confidence/按钮矩阵校验时只转下一个健康已审批供应商，供应商/传输暂不可用或读取结果不确定时状态都保持 `required`，另写 `verification_ai_unavailable|verification_result_unknown` 原因，不新增第四种验证码状态。只有全部当前健康已审批供应商确实返回无安全答案，或远端以同一 challenge fingerprint 明确拒绝单次提交时才写 `failed`；新 fingerprint 重新进入 required。每个 fingerprint 只允许一次 Telegram 按钮提交。最终仍只有验证通过后同一 Attempt 的完整 click evidence 写成 `target_click_observed` 才完成 click。

`committed_click_opportunity_count` 只统计当前 Window 已绑定中央 Reservation 的未失效 assignment。Gateway 结果 unknown 对新增投影贡献 0，已失效、过期或只有软节奏计划的 Action 也贡献 0。每份 ledger 把 `daily_click_target_snapshot` 表示为稳定的 `click_obligation_ordinal=1..N`；Planner 可按欠额惰性冻结 ordinal 身份，但 executable Action 只能由当前 Window commit 事务创建。数据库 partial unique 保证同一 ordinal 在 `pending|claiming|executing|unknown_after_send|success` 中最多一条当前 Action；明确失败 replacement 先终结旧 current Action，再复用原 ordinal并递增 Action/ExecutionAttempt，不能以新 Action ID生成新义务。

当前搜索 ledger 只有同时满足以下条件才可写 `daily_outcome=met`：`confirmed_click_count=daily_click_target_snapshot`，逐 ordinal 均由各自唯一完整 click evidence 确认，且 `held_count=unknown_count=terminal_shortfall=quantity_overflow_count=open_excess_count=0`，不存在影响该 ledger/ordinal 的 active `consistency_quarantine`。仅仅 `remaining_click_count=0`、存在 assignment、找到目标、验证码 `solved` 或 Action 数达到目标都不能单独判定 met。

系统可以枚举 `account × keyword × authorization_slot × proxy_route` 候选路径，但候选路径共享账号日额度、关键词额度、授权槽位、代理和 Gateway 容量，不能按笛卡尔积数量相加。每条路径必须形成资源向量：

~~~text
account_quota_key/version
keyword_quota_key/version
authorization_slot_id/version
proxy_route_id/binding_generation
protocol_sample_version
gateway_capacity_key/version
~~~

本专项固定使用以下全称，禁止用不带限定词的“精确求解器”或 “allocation epoch”：

- `DispatchLaneShardSolver`：中央全任务求解器，只把已获 task-lane 份额映射到 shard，不选择搜索账号路径；
- `SearchClickAssignmentSolver`：搜索内部求解器，只在中央已经授予的 fulfillment 份额内匹配 click ordinal 与账号路径；
- `dispatch_allocation_epoch`：现有 `DispatchClaimWindow.allocation_epoch` 的业务/API 名称；每个 TaskAllocation/ShardAllocation/Reservation 都固化所属 epoch。Window 仍可领取且 `allocation_state=ready` 时，第一批非空 release set 才把 epoch 递增一次并开启一个 pending rebuild wave；wave 尚为 `rebuild_required` 时后续释放加入同一 pending epoch，只递增 `rebuild_input_version`，不能再次递增 epoch。Window 已结束时只收口释放事实，由下一 Window 自然建立新权重；
- `dispatch_rebuild_snapshot_hash`：pending epoch 中央分片重建的规范化输入身份；覆盖 due/eligibility、active exclusion、有效旧 Reservation、scope/shard 容量及相关配置当前值/版本，提交前重算。成功发布后 Window 的 `ready_rebuild_snapshot_hash` 与每条新 TaskAllocation/ShardAllocation/Reservation 的 `dispatch_rebuild_snapshot_hash` 必须相等；它不等于搜索 `solver_input_hash` 或逐 unit exclusion 的 `resource_snapshot_hash`；
- `SearchClickAssignmentEpoch` / `search_click_assignment_epoch`：绑定当前 `(dispatch_claim_window_id, dispatch_allocation_epoch)` 的唯一持久结果行和搜索候选/资源冻结快照；一个 epoch 只求解和 finalize 一次，不能在原分配上重试。即使结果为 `no_candidate|abandoned`、没有任何 assignment，也必须由该行保存结果和释放幂等状态；
- `DispatchAllocationReleaseBatch`：搜索 epoch 已 finalize 后，assignment 在 Gateway 前明确失效、Action 不再到期或 Window 结束等事件释放一个或多个中央 unit 的持久幂等载体；同一稳定 trigger 只能 finalize 一次；
- `DispatchAllocationExclusion`：精确且跨状态永久绑定 `(dispatch_claim_reservation_id, fulfillment_lane_claim_ordinal)` 的单个已释放中央份额事实；它只影响来源 Window 的重新获配，不是永久账号/任务黑名单，也不是极搜账号 24h 协议安全排除；
- `membership_admission`：加入目标群、关注频道、确认/验证及 membership/can-send 复检链。纯搜索点击不进入该链，API permission 与搜索 path eligibility 也不得称为 admission。

现有 `lane=admission/admission_lane_claims` 只作为 `membership_admission` 执行份额的兼容物理名；纯搜索点击固定为 0，不能拿该字段表达账号 eligibility、目标匹配或未来“搜索点击加入”。

`jisou_selector_accounts` 的 24h 排除继续作为显式安全 eligibility 事实：必须保存账号、协议错误原因和 `expires_at`，有效期内只把对应账号—协议路径从候选图移除；它不减少 `remaining_click_count`、不结束 Task、不阻塞其他账号，也不等于 `DispatchAllocationExclusion`。当前 search epoch 的单次求解读取包含该状态的完整冻结候选图；若结果为 `no_candidate|abandoned`，才按被放弃的中央 Reservation unit 创建后者。尚可领取 Window 的非空 release set 只开启或加入唯一 pending rebuild wave，空集合只 finalize 当前搜索 epoch，已结束 Window 只收口释放事实；均不在原份额重映射或重试。

搜索求解固定分为只读投影与当前 Window 提交两种模式，二者不能共用“已预留”语义：

1. `projection` 在 Planner/详情计算中先基于同一冻结时刻的全部任务债务、active/unknown 和 scope capacity，逐个未来 Window 只读重放中央 TaskAllocation/lane/shard 算法，再只对模拟得到的 search fulfillment 份额执行相同精确求解；不得假设搜索独占未来 scope。它不创建 `SearchClickOpportunityAssignment`、Action、claim 或任何资源 reservation；结果只作为保守风险投影，不保证已取得执行槽。
2. `commit` 只能从中央 Claim Window 的 `allocation_state=ready`、且当前 `dispatch_allocation_epoch` 已按全部任务整批写入 `DispatchClaimTaskAllocation`、search fulfillment lane 份额和 shard `DispatchClaimReservation` 的快照开始。当前单用户 scope 内获得该 Window search fulfillment 份额的任务共用该中央版本唯一的持久 `SearchClickAssignmentEpoch`；每个 Task 的 `sum(x)` 不得超过其 `fulfillment_lane_claims`，每个 shard 不得超过现有 Reservation。求解后的匹配项在 outcome finalize 短事务按稳定 resource key 顺序 CAS 搜索专属 consumptive/eligibility 子预留，绑定既有 `DispatchClaimReservation`，创建 `SearchClickOpportunityAssignment`，最后创建/绑定 Action。一个 epoch 只允许一次求解和一次成功 finalize：没有候选、无法证明结果、资源版本变化、容量不足或 CAS 失败时，不在原份额循环重试；当前 outcome 的全部未领取 unit 组成一个 release set。`optimal` finalize 必须同时满足 Window 仍可领取、`allocation_state=ready` 且 `Window.dispatch_allocation_epoch == SearchClickAssignmentEpoch.dispatch_allocation_epoch`；任一不满足都说明该求解快照已过期，只能改为 `abandoned`。若 Window 正在 rebuild，释放加入同一 pending wave；若 Window 已在更高 epoch 回到 ready，释放从当前 ready 版本开启下一 wave；若 Window 已结束，只收口释放事实。空集合只 finalize 当前结果，不制造空重分配。

Dispatcher/Gateway 共享 inflight 只能由中央 `DispatchClaimReservation` 占用一次；assignment 保存对应 key/version 供 claim/Gateway 前复核，但不得再建立第二份 inflight 预留。代理若是全任务共享在途资源同样复用中央 Reservation；只有搜索协议专属 quota 才建立其子预留。`hard_safe_attempt_capacity` 分开返回只读 projected 与当前 Window committed 数，只有 committed assignment 才可生成可 claim Action。

assignment 不再计算 `latest_safe_start_at`，也不使用协议耗时预算、Dispatcher/Gateway 性能预算或求解器技术 deadline 参与分配。任务配置的 `scheduled_end` 仅作为业务停止边界和按时/late 归属；同一最大匹配的稳定多解按 `hard_safe_remaining_capacity DESC -> confirmed_click_count_today ASC -> last_click_opportunity_at ASC -> persistent_account_cursor ASC` 决胜，容量、匹配和顺序均由系统管理，运营不能配置。

软节奏产生的到期量已经落后于真实欠额，或当前已匹配机会不足以覆盖 `planning_click_deficit` 时，立即进入 catch-up，并把本轮 `due_click_target_count` 提升到完整日目标。此时只对同一 ordinal 保留 open/unknown 防重；若其他 ordinal 仍有已匹配合法路径，不能因为另一个 source pending 或 unknown 而停止继续补量。

硬门禁只有：任务/目标有效性、账号与关键词安全额度、账号冷却、授权槽位与代理、协议样本、CAPTCHA、精确目标匹配、Dispatcher/Gateway 安全在途量、scheduled_end 和 unknown 防重。普通 `hourly_round_curve`、`actions_per_round`、daily/hourly/action skip probability、jitter 和 quiet-hours 是软节奏：

- 安全容量充足时按原配置分散；
- 任何小时权重不得为 0，静默时段只使用较低非零权重；
- `next_run_at` 与 Action `scheduled_at` 不得调用“跳到 quiet end/下一非零曲线小时”的旧硬门禁；运行时先移除 quiet 跳转语义并把曲线 0 归一为最小权重 1。接管发布必须一次性唤醒仍被旧静默结束时间推迟的 running 纯点击任务，使失主 open epoch 与当日欠额立即进入下一 Planner cycle，之后按正常软节奏计算且重复接管不覆盖；
- 软 skip 不创建 `skipped_by_behavior_pacing` 终态，不扣减日目标；
- `catch_up_required=true` 时忽略软 skip、压缩 jitter，并在硬安全上限内提升到最大合法速率；
- 软节奏恢复只影响未来 Action，不改写已进入 Gateway 或 unknown 的事实。

存量 `max_actions_per_hour/max_actions_per_day/strict_daily_target` 不再是完成门禁：新建界面不要求这些字段，旧值只作为安全容量充足时的软分散提示。真正的硬上限由账号/关键词/授权槽位/代理/Gateway 等系统安全策略实时聚合，不允许旧 Task 级 Action cap 在目标尚未完成时直接停止规划。

运行时显式返回 `remaining_click_count`、`planning_click_deficit`、`projected_eligible_attempt_capacity_before_deadline`、`projection_not_reserved`、`committed_click_opportunity_count`、`hard_safe_attempt_capacity`、`occupied_source_count`、`current_hour_hard_safe_occupied/current_hour_hard_safe_available`、`catch_up_required` 和硬 blocker。不得返回或兼容投影 `projected_confirmable_clicks_before_deadline`；小时可用量必须从系统硬安全策略聚合，不能从旧任务级 `max_actions_per_hour` 回填。`strict_capacity_action_key` 只在 Task 已启动并持久化真实 `task_day_ledger_id` 后生成；创建接口、编辑接口和 UI 不需要临时 ledger、预检 token 或确定性 seed。

PostgreSQL 中的 `scheduled_at/executed_at` 与当前时间必须统一转换为 Task 时区 aware datetime。carry、claim、Gateway 和 unknown 必须先扣除，不能重复建单。

当硬安全容量暂时不足时，任务保持 running，显示 `daily_target_capacity_insufficient`，并在账号、代理、协议、审批或 Dispatcher 容量变化后自动重算。系统不得因该状态完成任务、缩小目标或停止其他可执行账号。未命中 source 明确终态后，Planner 可创建 replacement attempt；replacement 仍受硬安全边界约束，且 open/unknown 始终防止重复。

### 4.2 创建、启动、编辑与存量任务

1. 新建只做请求结构校验：必填字段、调用者同时具备 `tasks.manage + tasks.create.search_click`、同用户可见的账号组/目标引用、引用类型、模式固定值和数值范围；缺任一权限返回 403，不可见引用不得泄露存在性。公开目标 inline 输入只做字符串语法、规范化 username 和本地唯一性检查，可在同一事务 upsert 尚未远端解析的目标引用；不得在创建事务调用 Telegram resolve 或任何远端 capability probe。合法请求直接持久化 `task_type=search_click`，不读取账号在线、Telegram 权限、代理、协议或容量，不显示风险确认、不预创建 source；该步骤不得命名为任务预检。
2. 普通创建成功后由运营启动；`create-and-start` 先提交 Task，再复用同一个启动入口。启动事务创建首份真实 task-day ledger，Planner 首轮读取当时账号、代理、协议、时间和 Dispatcher 事实。
3. 编辑不做容量预检。`search_execution_mode` 创建后不可修改；影响目标数、时区或账号范围的变更按专项 pending revision 规则生效，运行风险由新旧 ledger 各自按冻结快照计算。
4. 运行中硬容量下降时任务保持 running，已进入 Gateway/claim 的 source 继续收口；新的 source 只按当前硬安全容量及未被有效 Action 占位的欠额创建。容量恢复后自动继续，无需重启。
5. 每日结论以 `task_day_ledger_id` 为键；本地日期不能合并多份 ledger。详情显示 ledger ID、effective date、冻结时区、UTC period、完整/部分日、confirmed/remaining、catch-up 和硬 blocker。

Task 时区和 `task_day_ledger_id` 只切换业务点击目标，不重置账号日限额、关键词日限额、小时冷却、授权锁或 unknown hold。安全额度继续按 `AccountUsagePolicy` 的滚动窗口/策略时区扣减；transition ledger 容量证明必须先扣除同一安全窗口已消费量。

### 4.3 搜索点击模式与范围边界

本 PRD 只设计用户可见任务模式“搜索点击”。创建输入固定为：

```text
task_type: search_click
search_execution_mode: click_only
daily_click_target_count: int
```

1. “搜索点击”只执行关键词搜索、结果翻页、精确目标匹配和目标点击；唯一数量目标是 `daily_click_target_count`。
2. 该模式不得接收 `join_target_group_after_click`、`daily_admission_target_count`、成员目标或任何准入参数；携带这些字段统一返回 `422 field_not_allowed_for_click_only`。
3. 该模式不得创建 `search_join_membership`、join、follow、confirm、challenge、membership probe 或 can-send probe Action；已有成员关系也不能改变 click 成功口径。
4. `search_execution_mode` 创建后不可修改。模式、任务类型或 admission 字段不能通过编辑接口互相转换。
5. “搜索点击加入”登记为后续独立任务模式 `click_and_join`，但其字段、状态机、账号选择、准入目标、迁移、API、QA 和发布规则均不属于本 PRD，`design_status=not_started`。本轮不得实现、推断或复用旧入群开关来替代其专项设计。
6. 存量同时包含 click 与 membership/admission 行为的任务标记为 `legacy_mixed_search_join`，保持原 Task、Action、Attempt 和远端事实不变；在“搜索点击加入”专项 PRD 完成前，不自动迁为 `click_only|click_and_join`，也不允许通过纯搜索点击编辑入口修改。

“搜索点击”的账号选择只服务 click 履约。在全部硬安全资格相同的候选中，按下列稳定顺序决胜：

```text
hard_safe_remaining_capacity DESC
-> confirmed_click_count_today ASC
-> last_click_opportunity_at ASC
-> persistent_account_cursor
```

账号选择顺序、真实容量、协议/CAPTCHA、授权、代理和 replacement 均由系统管理；运营不能配置账号优先级。合法 repeat 可以补 click，但不得绕过账号/关键词安全额度。

每份 task-day ledger 只冻结 `daily_click_target_snapshot`。目标、时区或账号范围的变更只写 pending config revision 并固定在当前 `deadline_at` 生效；在 deadline 前，当前 source 和完成合同不改写。重复编辑使用 `Task.config_revision` CAS 且不延后 effective_at。

source 必须冻结：

```text
task_day_ledger_id
daily_click_target_snapshot
obligation_local_date
timezone_snapshot
timezone_revision
period_start_at
deadline_at
click_obligation_ordinal
source_click_action_id
```

`SearchClickAssignmentEpoch` 至少持久化：

~~~text
id / dispatch_claim_window_id / dispatch_allocation_epoch
search_click_assignment_epoch / solver_problem_hash / solver_input_hash
solver_owner_lease_id / solver_claimed_at
state = open | finalized
solver_result = no_candidate | optimal | abandoned
matched_count / unmatched_unit_count / release_unit_count
release_unit_set_hash / outcome_hash
next_dispatch_allocation_epoch (nullable)
rebuild_input_version_after (nullable)
created_at / finalized_at
~~~

搜索输入必须拆成两个不可混用的 hash。`solver_problem_hash` 是 carrier-independent 的业务问题图身份：规范化包含 `solver_contract_version`、稳定业务义务键、各连通分量的候选路径、账号/关键词/授权/代理/协议/CAPTCHA/Gateway 等资源 key/value/version，以及真正参与该分量公平目标的 due/remaining/cursor 输入；排除 Window/dispatch/search epoch、TaskAllocation/Reservation/ordinal/assignment ID、仅由本次 carrier 产生的份额计数、worker/lease、扫描或墙钟时间、进程身份和随机值。`solver_input_hash` 则在该 problem hash 之外加入 carrier 的 Window/dispatch/search epoch、精确 Reservation unit 集、中央份额/Reservation 版本和本次提交所需的全部 carrier-specific 输入。字段集合或排序规则变化必须提升 `solver_contract_version`，版本同时进入 problem/input 两个 payload但不新增独立状态列。这样完整 input hash 负责本 epoch outcome 幂等，carrier 自身变化不会冒充“业务问题已经变化”。

上述“全部影响输出的输入”不是可选摘要。账号决胜所读取的 `hard_safe_remaining_capacity`、`confirmed_click_count_today`、`last_click_opportunity_at`、`persistent_account_cursor`，以及各字段的来源 key/version，都必须进入所属 component payload、`solver_problem_component_hash` 和聚合 `solver_problem_hash`；缺任一项均不得提交求解结果。`confirmed_click_count_today` 只表示同一冻结 `account_quota_key/capacity_window_key` 内已取得远端 click 事实的数量，不读取服务器自然日或提交时墙钟；snapshot 必须同时保存该窗口 key、计数值和计数 version。`last_click_opportunity_at` 是已持久化的业务公平事实，不是 `now()`；cursor 同样保存 value/version。任何新增会影响候选、约束、目标或 tie-break 的读取，都必须进入 canonical payload 并提升 `solver_contract_version`。

两个 hash、连通分量和 unit 归属只能由唯一 `SearchSolverSnapshotAssembler` 生成。Assembler 在同一一致性数据库快照内建立不可变 `SearchSolverProblemSnapshot`、全部 `SearchSolverProblemComponent(stable_component_key, canonical_nodes_edges_fairness, solver_problem_component_hash)` 与 `SearchSolverCarrierUnitBinding(reservation_id,ordinal,stable_component_key)`；共享任一资源 key 或 `assignment_fairness_key` 的节点必须进入同一分量，每个 carrier unit 必须且只能绑定一个分量，无候选路径的 unit 也要形成带实际 eligibility/resource 版本的零边分量。`solver_problem_hash` 从全部稳定排序的 component payload 唯一重算，`solver_input_hash` 再加入稳定排序的 carrier binding 与 Reservation/version。epoch open 行、完整快照聚合、全部 component/binding 与 owner lease 必须一个事务原子落库后才能调用求解器；`SearchClickAssignmentSolver` 只能接收该持久快照，不得在计算中额外查库或读取全局可变状态。owner 丢失后的 recovery 必须使用原 binding/component hash 生成逐 unit exclusion，禁止重新组图猜测；active exclusion 的 supersede 也只能由同一 Assembler/同一 canonicalization 对当前业务分量重算，禁止另一套手拼 hash。无法形成完整一致快照时不得调用 solver、不得留下半条 open epoch或部分 payload；数据库/一致性错误显式失败或进入对象级 quarantine，不得伪装成 `no_candidate|optimal`。

`stable_component_key` 不是随机 ID：它由 `solver_contract_version` 与该分量稳定排序后的业务义务、候选 edge、资源 node 和 fairness node 身份计算，排除资源当前值、carrier/Reservation/ordinal、worker、时间和随机值；`solver_problem_component_hash` 再覆盖该 key 以及全部 canonical node/edge/fairness 的当前值与版本。增加或删除候选、资源或公平连接导致分量拆分/合并时，受影响 key/hash 必须改变；只改变资源值或公平计数时 key 保持、hash 改变。Snapshot 内每个会影响 solver 输出的数据库读取必须能由 canonical payload 和 source version 反向枚举，禁止 solver 使用未入快照的隐式默认值、缓存或进程状态。

正常计算得到 `no_candidate|optimal` 后，finalize 前必须在一个短 PostgreSQL `SERIALIZABLE` 事务中按同一候选谓词和稳定 source key 运行同一个 Assembler 的只读 revalidation 模式，重算当前 `solver_problem_hash/solver_input_hash` 并逐项比较所有影响输出的 source version；该 revalidation 不能覆盖原持久快照。随后才按统一中央锁序验证 Window、Reservation、assignment、Action 与资源 CAS并提交唯一 outcome。任一候选 phantom、资源/额度、`confirmed_click_count_today`、机会时间、cursor、eligibility、中央份额或版本漂移，即使 Window epoch/input version 未变化，也必须把原 epoch 改为 `abandoned`，不提交旧 `optimal|no_candidate`，并按原 snapshot/binding 将全部仍未领取 unit 原子释放后加入唯一 rebuild wave。序列化冲突（SQLSTATE `40001`）无论发生在锁定、写入还是 commit，都必须先整批回滚旧事务，再用新事务按原 binding 直接 finalize 为 `abandoned`、释放全部未领取 unit 并触发唯一 rebuild wave；不得让 ORM/驱动重放旧 solver 结果，也不得重新求解同一 epoch。数据库不可写等导致 abandoned 事务本身无法提交的错误必须显式失败并保持 open，由 owner-loss recovery 继续按原 binding/hash 收口。

唯一键为 `(dispatch_claim_window_id, dispatch_allocation_epoch)`。创建 `open + 完整持久快照 + solver_problem_hash + solver_input_hash` 的同一事务必须绑定当前有效的 `solver_owner_lease_id/solver_claimed_at`；唯一键冲突的其他 worker 只回读该行，禁止并发求解。只有仍持有该 worker lease 的 owner 可以执行一次计算并以 `open -> finalized` CAS 保存唯一结果。该 lease 仅用于 worker 存活 fencing，健康 owner 在求解期间持续续租；不得把固定租约时长、心跳周期或续租次数解释为 solver deadline、性能预算或自动 abandoned 条件。`release_unit_set_hash` 对按稳定顺序排列的 `(window,reservation,ordinal,reason_code,resource_snapshot_hash)` 精确释放集合计算；空集合也保存确定性空 hash，不能只存 count。`outcome_hash` 必须覆盖 carrier 的 `(dispatch_claim_window_id,dispatch_allocation_epoch,search_click_assignment_epoch)`、`solver_problem_hash`、`solver_input_hash`、`solver_result`、全部 matched assignment 的稳定 identity/version、`release_unit_set_hash`、实际 `next_dispatch_allocation_epoch` 和 `rebuild_input_version_after`；非空 release set 即使 Window 已结束而二者为 null，也能由 release hash 与 Window 状态证明收口结果。只有 owner 进程失联、fencing token 失效或明确丢失续租所有权时，recovery 才直接按 `abandoned` finalize并释放该 epoch 全部未领取 unit；不得因求解耗时本身宣告丢失 lease，不转移 solver ownership、不重跑求解，也不新增 solver attempt/history。release wave 判断已结束 Window 时必须将 PostgreSQL aware 时间与业务 naive 北京时间统一为北京时间语义，不能因两种表示直接比较而回滚 recovery。正常 finalize 的短 `SERIALIZABLE` 事务必须先按 Window → TaskAllocation → ShardAllocation → Reservation 锁定中央分配行，再读取 owner 与当前候选并重算输入；不能先建立旧快照再等待中央分配锁。已 `finalized` 的重放只回读同一 `solver_problem_hash/solver_input_hash/release_unit_set_hash/outcome_hash/next_dispatch_allocation_epoch/rebuild_input_version_after`，不能再次释放或创建新 assignment；任一字段或 carrier 身份不一致都进入 `release_fact_incomplete`，不得选择其中一版继续。该结果行、problem snapshot/component/unit binding、release batch item 和 unit-level exclusion 在来源 Window、Reservation 或任何迟到 outcome 仍可访问期间不得物理删除；Window 归档必须把上述事实与 Reservation 一起移出活跃写域，并先以 fencing 阻止迟到 worker，不能通过清理历史重新获得唯一键或丢失逐 unit 分类。

`SearchClickOpportunityAssignment` 至少持久化：

~~~text
task_day_ledger_id / target_id / click_obligation_ordinal
dispatch_allocation_epoch / search_click_assignment_epoch_id
solver_input_hash
assignment_version / assignment_expires_at
capacity_window_key
dispatch_claim_window_id / dispatch_claim_task_allocation_id
dispatch_claim_reservation_id / fulfillment_lane_claim_ordinal
account_id / account_quota_key / account_quota_version
keyword_quota_key / keyword_quota_version
authorization_slot_id / authorization_version
proxy_route_id / proxy_binding_generation
protocol_sample_version
gateway_capacity_key / gateway_capacity_version
assigned_action_id
state = reserved | action_bound | claimed | gateway_started | unknown | consumed | released
~~~

同一 `(task_day_ledger_id,target_id,click_obligation_ordinal)` 同时最多一条非 `released` assignment；同一 `(dispatch_claim_reservation_id,fulfillment_lane_claim_ordinal)` 也同时最多绑定一条非 `released` assignment，且 ordinal 必须位于 `1..reserved_claims`。assignment 绑定成功时同事务增加 Reservation `bound_count`；`_confirm_claim` 时把该 unit 从 bound 转 claimed，放弃未领取 unit 时从 bound 或可绑定状态转 released。assignment 只证明已取得当前规划资源，不是点击成功；`consumed` 也必须由其 Action/ExecutionAttempt 的真实 `target_click_observed` 触发。assignment、Action 和资源 reservation 的绑定/释放必须使用版本 CAS，禁止一个 assignment 指向两条当前 Action、一个中央份额槽被多条 assignment 使用或多个 Task 重复消费同一资源单位。

资源状态必须区分：

- `consumptive`：账号/关键词等调用后可能已经消费的额度；仅 commit 模式在中央份额内建立搜索专属子预留，Gateway 调用结果 unknown 时继续 hold 到远端核验或对应安全窗口结束。
- `inflight`：Dispatcher/Gateway 及全任务共享代理同时在途量；只引用中央 `DispatchClaimReservation`，不得由 assignment 重复预留。Gateway 调用结束即释放，unknown 不得无限占用。
- `eligibility`：授权、协议样本和代理有效性；不永久扣配额，但必须在 claim/Gateway 前按冻结版本复核。

所有资源都固化 `capacity_window_key`。projection 必须先扣除中央 Window 已提交份额及其他任务类型的同资源事实，但不得写 hold；commit 直接受当前 Window 的 TaskAllocation/Reservation 上限约束，搜索 Task 不能另算一份。只有 `reserved|action_bound` 且尚未 `_confirm_claim` 的 assignment 使用 `assignment_expires_at <= Claim Window.bucket_end`。搜索 epoch outcome 内的求解失败、绑定 CAS 失败或 unmatched unit 由 `SearchClickAssignmentEpoch` 承载 release set；epoch 已 finalize 后，`reserved|action_bound` assignment 因 Gateway 前安全复核失败、任务/Action 不再到期或 assignment 到期而释放时，必须改由唯一 `DispatchAllocationReleaseBatch` 承载，不能回写或重开原 search epoch。释放 bound assignment 时同一事务执行 assignment `reserved|action_bound -> released`、Reservation `bound_count -= 1/released_count += 1`、Task/shard/Window `unclaimed_allocated_count -= 1` 并写永久 exclusion；不允许只改 Action 或只释放搜索子预留。Window 结束时仍未领取的 assignments 使用一个稳定的 window-expiry release batch 收口，但已结束 Window 不再创建无用途的中央 epoch；下一 Claim Window 从最新欠额自然创建新权重。`_confirm_claim` 必须在同一 CAS 中把中央 Reservation unit 从 bound 转 claimed、Action 转 executing、assignment 转 `claimed`，此后 Window 结束不得自动释放，assignment 按 Gateway/Attempt 进入 `gateway_started|unknown|consumed|released`。Gateway-started/unknown 继续占用 click ordinal，但只保留可能已消费的 quota hold。

中央 `claim_class=search_click` 的 fulfillment Reservation 从 `ready` 发布起到首次 `SearchClickAssignmentEpoch` finalize 前，由搜索物化流程独占全部 unit ordinal。通用 `unclaimed_action_no_longer_due`、无 Action Reservation 回收或普通 Action expiry reclaimer 必须跳过这些 Reservation，不能因为 assignment/Action 尚未创建就提前释放。若 Window 可领取但结果行尚不存在，首个有效 worker 按唯一键创建 open epoch并绑定自身 lease后执行一次求解；若 Window 在结果行创建前已经结束，recovery 直接在一个事务创建并 finalize `abandoned`，不调用求解器。已有 open epoch 的 owner 存活时其他 worker只等待/回读；owner lease 丢失时按既定合同直接 abandoned。任务暂停、停止、删除、due 变化或 Window 结束只使首次 outcome 的 optimal 前置失效，由该 epoch 释放全部仍未领取 unit，不得另建通用 release carrier。首次 outcome finalize 后，每个来源搜索 Reservation 必须满足 `bound_count + claimed_count + released_count = reserved_claims`；此后只有 bound assignment 使用 release batch，claimed 继续收口。若通用 reclaimer 已碰触上述 unit，属于 `search_reservation_ownership_violation` 一致性隔离，不得当作合法重叠 trigger。

`allocation_state=ready` 只控制新中央版本和新 `SearchClickAssignmentEpoch/assignment` 的物化，不得卡住旧 epoch 已原子绑定的 Action。同一 optimal outcome 因 unmatched release 把 Window 置为 `rebuild_required` 后，仍处于 `reserved|action_bound` 的 matched assignment 只要来源 Reservation/assignment/资源/Action version 仍有效、Window 尚未结束且业务 deadline 未到，必须继续按来源 epoch执行 `_confirm_claim -> Gateway`；它不读取或消费尚未发布的新权重。Window 已结束、任务不再到期或任一 Gateway 前资格失效时才由稳定 release batch 放弃。新 epoch 的未绑定份额必须等新权重与 `ready` 原子发布后才能领取。

全局匹配使用纯 click 的多阶段字典序目标：先最大化当前 Window 可提交的 assignment 总数；在不减少总数的解中，最大化获得至少 1 条 assignment 的到期父任务数，并按业务 `scheduled_end`、最久未获机会和持久 task cursor 对无法同时覆盖的任务稳定决胜；随后按冻结 `remaining_click_count` 做最大最小公平，避免剩余 assignment 永远集中到同一 Task；最后严格按账号 `hard_safe_remaining_capacity DESC -> confirmed_click_count_today ASC -> last_click_opportunity_at ASC -> persistent_account_cursor ASC` 决胜。每个 Task 建立 `assignment_fairness_key=(allocation_business_task_id,task_day_ledger_id,target_id)`；不建立 admission distinct/budget 目标。

跨 Task 公平只对当前 `search_click_assignment_epoch` 至少有一条真实 eligibility 路径的 due Task 求解；无路径 Task 继续显示缺失资源，不能用它把可执行 Task 的总 assignment 降为 0。冻结 `remaining_click_count` 后定义 `task_fairness_ratio=assigned_count/max(remaining_click_count,1)`，按从小到大排序后的 ratio 向量做字典序最大化；离散余数以业务 `scheduled_end`、最久未获机会和持久 cursor 决胜，不使用不可解释加权总分。`optimal` 必须为每个 due Task 保存 `task_assignment_count` 与 `task_unmatched_reason=no_eligibility|resource_saturated|fairness_deferred|null`，使“未获机会”可与“没有路径”区分。

候选生成不得展开理论笛卡尔积后截断：只为真实存在且已通过当前 eligibility 的账号、授权槽、当前代理绑定和关键词组合建立路径，并按完整资源向量去重。`search_click_assignment_epoch` 绑定当前 `dispatch_allocation_epoch`，冻结候选、资源版本和 `solver_input_hash` 后，把共享同一 click ordinal、任一资源 key 或同一 `assignment_fairness_key` 的候选拆成互不共享约束/目标的连通分量。每个 epoch 只求解一次。每个分量以 `x[ordinal,path] ∈ {0,1}` 求解，并为到期 Task 建立 `z[task] ∈ {0,1}`：强制每个 ordinal 最多一条路径、每个资源窗口的已占用量加本批 usage 不超过 available，且 `z` 只能由该 Task 至少一条已选路径激活。目标依次固定最优值：`sum(x)` -> `sum(z)` -> 按 remaining click 比例的最大最小公平向量 -> 稳定 path tie-break；任一后续阶段不得降低前一阶段最优值。`assignment_fairness_key` 把跨 ordinal 的任务公平目标连接到同一分量，各分量最优向量之和才构成全局字典序最优。允许使用带最优证书的确定性约束求解或等价精确算法，不允许首个可行贪心、固定 top-N、部分结果或账号遍历顺序替代“完成优先”；无法给出完整可验证结果时直接进入 `abandoned`。

本 PRD 不定义求解器技术 deadline、性能预算、图规模基线、p99 指标或为性能达标而设计的重试/降级分支。候选图可以由实现选择等价表示，但不得抽样、固定 top-N、提交部分解或改变上述目标顺序；本次 epoch 无法一次返回完整可验证结果时直接记 `abandoned` 并释放全部未领取 Reservation unit。尚可领取 Window 的非空 release set 只开启或加入唯一 pending rebuild wave；空集合和已结束 Window 不推动中央版本。

求解结果闭集为 `no_candidate|optimal|abandoned`：

- `no_candidate`：整个当前快照没有任何 eligibility 路径，不调用求解器；在 epoch 结果行保存 `no_candidate`，把当前 search fulfillment Reservation 的全部未领取 unit 作为一个 `release_unit_set` 原子释放；尚可领取 Window 的非空集合开启或加入唯一 pending rebuild wave，空集合和已结束 Window 不推动中央版本，各 Task 继续显示缺失资源；
- `optimal`：在 epoch 结果行保存 `matched_count/served_due_task_count/task_assignment_counts/task_unmatched_reasons/task_fairness_vector_hash/unmatched_ordinal_count/saturated_resource_keys`；同一 outcome finalize 事务先验证全部 matched 绑定，再原子绑定全部 assignment，并把全部 unmatched unit 作为一个 release set 释放。matched 前置条件失效，或 Window 不再同时满足“仍可领取 + ready + 当前 dispatch epoch 与 search epoch 完全一致”时不进入 optimal 写入，直接改为 `abandoned` 并释放全部仍未领取 unit；SQLSTATE `40001` 回滚旧结果后也必须在新事务直接 abandoned/release/rebuild，只有数据库不可写使该新事务无法提交时才保持 open 并显式报错。全过程不允许留下部分 assignment；非空集合只开启或加入当前唯一 rebuild wave；
- `abandoned`：求解器无法返回完整可验证结果、reservation/resource CAS 未能绑定、finalize 时 Window 已进入 rebuild wave/已发布更高 dispatch epoch/已经结束，或 worker 恢复到遗留 `open` epoch；不提交 incumbent/部分解，在 epoch 结果行保存 `abandoned/search_assignment_abandoned`，把当前 epoch 全部未领取 unit 作为一个集合原子释放；尚可领取 Window 的非空集合开启或加入当前唯一 wave，空集合只保存结果，已结束 Window 只收口事实，不在相同输入上重试。

三种结果都不得终结未确认的 click ordinal、缩小目标或改写旧 assignment/远端事实。

source Action 在当前 Claim Window 的 commit 事务末尾绑定其任务日 ledger、`click_obligation_ordinal`、assignment 和中央 Reservation；projection 绝不创建 Action。同一候选路径可在资源容量允许时匹配多个 ordinal，但每个 matched assignment 必须创建独立 Action，且 Action 的初始持久 payload 与 `action_dedupe_key` 必须在首次 flush 前同时包含 `search_click_obligation_id + search_click_assignment_id + dispatch_reservation_id + fulfillment_lane_claim_ordinal`。禁止先按相同账号/关键词/目标 payload 去重再回写绑定字段，也禁止把同一 Action 绑定给两个 assignment；任一一对一绑定无法整批成立时，`optimal` 事务整体回滚并按 `abandoned` 释放原 unit，不得留下部分 assignment。存量接管后，任何未绑定 `search_click_obligation_id` 的旧 source 均不得再次领取新 Claim Window：无 Gateway attempt 的写 `skipped`；已有明确 success/failed 的按最后 Attempt 保留对应历史终态；Gateway 已开始但结果不确定的写 `unknown_after_send` 并保留 Attempt。接管必须在每次 Planner 扫描的失败重试与 backlog 判断之前执行，不能因 Task 已盖章 `all_task_v2` 而跳过；接管后按数据库事实刷新 backlog，通用失败重试永久忽略未绑定新义务的旧 source，禁止旧 failed Action 被重新置为 pending。已绑定 Action 的 Dispatcher plan 与原子 confirm 必须把 PostgreSQL aware `bucket_end` 和业务当前时间统一为 `Asia/Shanghai` aware 时间后判断 Window 是否开放；confirm 加锁顺序固定为 `Scope → Window → ShardAllocation → Reservation → Assignment`，与通用共享 claim 一致，禁止 assignment/reservation 反向锁回中央行。时区表示差异和并发锁序都不得异常退出整轮 drain，也不得让该 Action 回落到通用 claim。Gateway 不得在 `deadline_at` 后开始。Gateway 已开始但事实在 deadline 后确认时仍归原 ledger并记 late。`source_click_action_id` 只表示本次执行来源，不参与 click 义务唯一键。

`dispatch_prebound=true` 的 Action 只可使用原 assignment/Reservation/Window。plan 即使发现 Window 已结束，也必须保留该预绑定身份进入原子 confirm 的精确释放分支，禁止加入普通 Action 的新 allocation。账号全局安全策略、账号 shard、运行资源或 confirm CAS 使本 Window 无法执行时，不延后复用旧 Action；按 `search_resource_saturated|search_reservation_cas_abandoned|search_assignment_expired` 中的真实原因创建唯一 release batch，终结 Action、释放 unit 并加入同一 rebuild wave。release 自身同样按 `Scope → Window → ShardAllocation → Reservation → Assignment` 加锁。

release 的 batch、item、exclusion、Reservation/Window 计数与 rebuild wave 是一个原子事务。实现必须在读取 exclusion 生成 `release_unit_set_hash`、再生成 outcome hash前显式 flush，不得依赖 SQLAlchemy autoflush；生产 `SessionLocal(autoflush=false)` 和 QA 会话必须得到相同结果。flush、hash 或最终校验任一步失败都整体回滚。

预绑定 plan 必须读取原 `DispatchClaimShardAllocation.account_shard_total/index`，只向完全匹配的 Dispatcher 暴露 candidate。非归属 Dispatcher 看见同一 Action 时只从本 worker plan 排除，不能写 Action、不能释放 assignment、不能生成 exclusion；归属 worker 锁定后才按账号安全策略和实时资源继续 confirm。这样 shard 是并行路由，不是失败原因。

任务时区中途修改时，当前 ledger、source 和 Attempt 继续使用旧 `timezone_snapshot/deadline_at`；配置先进入 `pending_timezone`，在旧 deadline 建立新时区 ledger。若该时刻不是新时区 00:00，先建立 `timezone_transition` 过渡 ledger，随后才进入完整日；相邻 UTC 区间必须首尾相接。过渡 ledger 尽力完成但不纳入完整日 SLA。

点击只由同一 ExecutionAttempt 内完整的远端点击事实统计：

~~~text
target_click_observed = true
target_found_at
target_identity_snapshot
approved_button_fingerprint_hash
click_invoked_at
approved_protocol_outcome
membership_side_effect = none
membership_mutating_rpc_invoked = false
remote_confirmed_at
click_evidence_hash
~~~

`target_found_at` 只证明找到了目标，不单独完成 click；必须先有 `gateway_call_started_at/click_invoked_at`，再由已批准协议可观测结果确认目标按钮调用成功，并证明本次执行没有调用成员关系变更 RPC。只完成页面匹配、只找到按钮、调用超时、响应无法归类、membership 副作用未知或 evidence 缺字段时进入明确失败或 `unknown_after_send`，不得把 `target_click_observed` 猜成 true。原始 callback/页面正文不落库，只保存已批准指纹、受控 outcome 和 evidence hash。

非空 `click_evidence_hash` 必须与同一 ExecutionAttempt、同一 click ordinal 建立唯一所有权；另一个 Task/ledger/ordinal 不得复用。同一远端事实发生时间早于当前 ledger `period_start_at` 时只作为历史状态，不能倒灌为本日 click。唯一冲突进入该 ordinal 的 `consistency_quarantine/remote_fact_owned_elsewhere`，其他 ordinal 继续。

纯搜索点击在 `target_click_observed` 后即结束该 ordinal，不产生任何入群、成员关系或 can-send 后续步骤。

## 5. Dispatcher 份额仲裁

### 5.1 设计原则

固定排序不足以解决多个同时到期的任务族。全局容量和最低服务机会以 `all-task-fulfillment-recovery-prd.md` §5.3 的 60 秒 Claim Window、持久 cursor、每任务最低 1 个轮转机会和剩余需求最大余数分配为唯一合同：

1. 搜索点击只有 fulfillment lane，不建立 admission lane 或 child claim。
2. 严格 search source、AI 群日到期 send、评论、点赞、浏览等到期任务都按 `required_claims` 参与最低轮转和剩余比例分配；不存在 search 永远高于 AI/频道互动的固定全局顺序。
3. 若总 required_claims 超过当前可用 Dispatcher 份额，系统显式写 shared_dispatch_capacity_insufficient；不得让一个类别默默吞掉所有 slot。
4. ordinary 及评论、点赞、浏览同样按业务任务进入最低轮转与剩余最大余数分配，不得只使用搜索/AI 之后的残余 capacity。

### 5.2 全局 Claim Window、分片 Allocation 与 Reservation

四个 Dispatcher 不能各自把同一份并发 capacity 预留四次。容量权威分五层：`DispatchClaimScope` 保存跨 Window 的 executing active ledger，`DispatchClaimWindow` 管理当前 bucket，`DispatchClaimTaskAllocation` 先跨 shard 固化业务任务总份额，`DispatchClaimShardAllocation` 保存各账号 shard 可用容量，`DispatchClaimReservation` 才把任务份额映射到 shard：

当前产品只有一个业务用户和一个业务租户。这里的“全局”只表示该用户下多个 worker、账号 shard 与任务类型共享同一执行容量，不做 tenant 配额、tenant 权重或 tenant 间公平。下表保留 `tenant_id` 只是现有数据隔离、唯一键和审计字段；它在当前部署中是常量命名空间，不能再派生一层 TenantAllocation。

| 对象 | 字段 | 说明 |
| --- | --- | --- |
| DispatchClaimScope | dispatcher_scope、claim_capacity、active_claim_count、version | 一个真实共享 worker/队列/数据库 claim 域唯一一行；每次规划先按所有 `executing + dispatch_claim_active` Action reconcile，跨 bucket 的 active claim 始终占用全局容量 |
| DispatchClaimWindow | dispatcher_scope、bucket_start、bucket_end | 唯一定位真实共享 worker/队列/数据库 claim 域的一个窗口；唯一约束为 `(dispatcher_scope, bucket_start, bucket_end)` |
| DispatchClaimWindow | claim_capacity、active_claim_count、unclaimed_allocated_count、allocation_epoch、allocation_state、rebuild_input_version、ready_rebuild_snapshot_hash、version | 物理字段 `allocation_epoch` 对外和跨文档统一称 `dispatch_allocation_epoch`，`allocation_state` 只允许 `rebuild_required|ready`。每次中央 Allocation 前按真实 `executing + dispatch_claim_active` Action reconcile 当前 Window；`claim_capacity` 是该 scope 的有效 claim_limit / 并发总额，不得由历史计数或 Action 数量推测；全局不变量为 `active_claim_count + unclaimed_allocated_count <= claim_capacity`。pending rebuild wave 内新增释放只递增 `rebuild_input_version`，不再次递增 epoch；`ready_rebuild_snapshot_hash` 保存最近一次已原子发布 ready 的规范化输入 hash |
| DispatchClaimTaskAllocation | dispatch_claim_window_id、dispatch_allocation_epoch、dispatch_rebuild_snapshot_hash、tenant_id、allocation_business_task_id、required_claims、allocated_claims、fulfillment_lane_claims、admission_lane_claims、task_lane_cursor、last_opportunity_window、last_claimed_window、cursor_version、version | 跨所有 shard 的父业务任务总份额；唯一约束为 `(window, dispatch_allocation_epoch, tenant, allocation_business_task_id)`。通用模型保留 admission 字段供其他已设计任务使用，但纯搜索点击固定 `admission_lane_claims=0`；每个 epoch 的行不可改绑到其他 epoch，Reservation 机会与真实 claim 分开记录，同一父任务不能在每个 shard 重复获得最低 1 个；本 epoch 全部中央 allocation 行的 rebuild hash 必须相同 |
| DispatchClaimShardAllocation | dispatch_claim_window_id、dispatch_allocation_epoch、dispatch_rebuild_snapshot_hash、account_shard_total、account_shard_index | 一个 Window 在一个 epoch 对一个账号 shard 的已授予份额；唯一约束为 `(window, dispatch_allocation_epoch, shard_total, shard_index)`，不能让每个 shard 各自拥有完整全局 capacity；hash 必须等于同 epoch TaskAllocation/Reservation 与 Window ready hash |
| DispatchClaimShardAllocation | required_claims、active_claim_count、unclaimed_allocated_count、reason、version | 记录 shard 实际可领取候选造成的需求和获配；其 active / unclaimed 总和必须与全局 Window 账一致 |
| DispatchClaimReservation | task_allocation_id、dispatch_claim_shard_allocation_id、dispatch_allocation_epoch、dispatch_rebuild_snapshot_hash、tenant_id、allocation_business_task_id、action_task_id、claim_lane、claim_class、bucket_start | Reservation 同时附属于同一 epoch 的父 task allocation 和 shard allocation；唯一约束为 `(shard_allocation, tenant, allocation_business_task_id, lane, class)`；`claim_lane/class` 只用于父任务内路由，不能建立固定全局类别顺序；hash 必须等于所属 TaskAllocation、ShardAllocation 与 Window ready hash |
| DispatchClaimReservation | required_claims、reserved_claims、bound_count、claimed_count、released_count、urgency_score、reason、version | 记录类别需求、获配、已绑定、真实 claim、已释放与 CAS 版本；`bound_count + claimed_count + released_count <= reserved_claims`，可继续绑定量为 `reserved_claims - bound_count - claimed_count - released_count` |

`dispatcher_scope` 必须等于实际共享 worker/队列/数据库 claim 域的稳定标识；`account_shard_total/index` 必须来自当前 account shard 配置。任务最低轮转的 `DispatchFairnessCursor` 作用域固定为 `dispatcher_scope`，不得使用 tenant 或 shard 级 cursor 让跨多 shard 的任务重复受益；任务份额映射到 shard 时另用 `(dispatcher_scope, allocation_business_task_id)` 的 shard cursor 只做稳定多解决胜。纯搜索点击的全部份额都进入 fulfillment lane。

ordinary 及其他非搜索任务同样建立自己的 Reservation 并参与中央最低轮转/最大余数算法；它们不能复用其他任务尚未消费的 Reservation，搜索也不能只因“严格”标签抢占其最低份额。`dispatch_allocation_epoch` 是同一 Window 内中央 task/lane/shard 重新分配和同分轮转的持久化种子，未消费 Reservation 只能在新的 dispatch epoch 明确释放原因后才可重分配。

每个 Window 在短事务中先读取真实有效总 capacity、全 scope active claims 和 `task × lane × shard` due 需求矩阵；先按中央合同为父业务任务做全局最低轮转和剩余最大余数分配，写 `DispatchClaimTaskAllocation` 及 lane 份额，再用确定性最大流/等价精确三层匹配把 task-lane allocation 映射到有候选且有剩余容量的 shard。任务下所有 Reservation 之和不得超过 `allocated_claims`，lane 不得超过自身份额，shard 下 Reservation 之和不得超过 shard capacity；存在可行映射时不得因贪心顺序闲置容量。`urgency_score` 只作为展示与审计派生值，不得覆盖中央算法。无论有几个 Dispatcher 进程，所有 shard 的 active 加未消费预留总和都不得超过同一全局 Window capacity。

搜索 source 是该顺序的特例，不是例外：due 矩阵从未占位 click ordinal 的只读真实路径生成；中央 TaskAllocation/fulfillment lane/Shard Reservation 必须先提交，随后搜索 commit 求解器才在这些份额上限内绑定账号路径、搜索专属 quota 子预留、assignment 和 Action。所有搜索 assignment 的 `sum(x_task)` 不得超过该 Task 的 fulfillment lane 份额；中央 Reservation ID 一对一绑定被选 assignment，不能再新增 Dispatcher/Gateway inflight。

若搜索求解、候选绑定或资源 CAS 不能完成，不在原 `search_click_assignment_epoch` 重试。系统把该 epoch 需要放弃的全部未领取 `(dispatch_claim_reservation_id, fulfillment_lane_claim_ordinal)` 组成 `release_unit_set`，由该 `SearchClickAssignmentEpoch` 作为首次 release carrier；一次 outcome finalize 短事务为集合内每个 unit 写一条 `DispatchAllocationExclusion`，按 Reservation 汇总增加 `released_count`，按 Task/shard/Window 汇总减少 `unclaimed_allocated_count`。集合为空时 `next_dispatch_allocation_epoch=null`，不改变中央 epoch。集合非空且 Window 仍可领取时，若 Window 为 `ready`，事务只递增一次 `dispatch_allocation_epoch`、置 `rebuild_required`、递增 `rebuild_input_version` 并开启该 pending epoch 的 rebuild wave；若 Window 已为 `rebuild_required`，本 release 直接加入现有 wave，只递增 `rebuild_input_version`，不得再递增 epoch。Window 已结束时只提交释放事实和计数，`next_dispatch_allocation_epoch=null`，下一 Window 自然重新分配。已 bound/claimed/active unit、其他仍有效 Reservation、既有 assignment 和公平 cursor 不回退，click 业务欠额不减少。

`SearchClickAssignmentEpoch` 已 finalized 后的释放不得改写原 outcome，统一使用 `DispatchAllocationReleaseBatch`：

```text
dispatch_claim_window_id / source_dispatch_allocation_epoch
release_trigger_type / release_trigger_key
candidate_unit_set_hash / candidate_unit_count
release_unit_set_hash / release_unit_count
already_released_unit_count / precondition_lost_unit_count
outcome = applied | no_op | mixed
outcome_hash
next_dispatch_allocation_epoch (nullable)
rebuild_input_version_after (nullable)
finalized_at
```

每个候选 unit 同时持久化一条不可变 `DispatchAllocationReleaseBatchItem`：

```text
release_batch_id
dispatch_claim_reservation_id / fulfillment_lane_claim_ordinal
search_click_assignment_id / expected_assignment_version
bound_action_id / expected_action_version (nullable)
classification = effective_released | already_released | precondition_lost
observed_assignment_state / observed_assignment_version
observed_action_state / observed_action_version (nullable)
satisfied_by_release_carrier_type / satisfied_by_release_carrier_id (nullable)
```

唯一键为 `(release_batch_id, dispatch_claim_reservation_id, fulfillment_lane_claim_ordinal)`。`candidate_unit_set_hash` 对按稳定 unit key 排序后的完整 item 输入、expected assignment version 与 nullable expected Action version 取 hash，`release_unit_set_hash` 只对 `effective_released` item 取 hash；`already_released` 必须保存首个 exclusion carrier，`precondition_lost` 必须保存取得锁时的 assignment/Action 状态与版本证据。`outcome_hash` 对 carrier 的 `(window,source_dispatch_allocation_epoch,release_trigger_type,release_trigger_key)`、candidate hash、按稳定 unit key 排序的全部 item 分类及 expected/observed assignment/Action version、首 carrier 引用、release hash、三类 count、`outcome` 和实际 `next_dispatch_allocation_epoch/rebuild_input_version_after` 统一计算；任何字段都不得依赖日志补齐。batch、全部 item、Action/assignment 状态、计数/exclusion、outcome hash 和 wave 更新同事务提交，禁止只留汇总计数或依赖日志重建分类。

batch 汇总必须由 item 唯一重算，并同时满足 `candidate_unit_count = release_unit_count + already_released_unit_count + precondition_lost_unit_count`。`outcome=applied` 仅用于“release 非空且其余两类均为 0”，`outcome=no_op` 仅用于 `release_unit_count=0`，`outcome=mixed` 仅用于“release 非空且至少一种 no-op 分类非空”；candidate 为空同样是显式 `no_op`。任一 count、hash、outcome 与 item 不一致时不得 finalize。

唯一键为 `(dispatch_claim_window_id, release_trigger_type, release_trigger_key)`。`release_trigger_key` 必须来自不可变因果事实及其版本，例如 `search_assignment_pre_gateway_terminal:{assignment_id}:{assignment_version}`、`action_no_longer_due:{action_id}:{action_version}` 或 `search_assignment_window_expiry:{window_id}:{source_dispatch_allocation_epoch}`，不得使用随机 batch id、worker id 或扫描时间。`candidate_unit_set_hash` 对该不可变 trigger 派生的完整候选 unit 集合取 hash，是输入幂等身份；`release_unit_set_hash` 只对取得提交锁后仍可实际释放的有效子集取 hash，是结果证据。

batch 先取得中央 `Scope -> Window -> TaskAllocation -> ShardAllocation -> Reservation` 前缀和 release carrier，再按 `(reservation_id, ordinal, assignment_id)` 稳定顺序锁 assignment，随后按稳定 resource key 锁搜索 consumptive 子预留，最后按稳定 Action ID 锁 nullable bound Action并分类。`_confirm_claim`、Gateway 前最终守卫、release 与 Reconciler 必须使用同一扩展顺序；不存在 carrier 或搜索子预留的阶段只跳过对应层，禁止先锁 Action、assignment 或搜索资源再反向取得上游层。assignment 仍为 trigger 指定版本的 `reserved|action_bound`、没有任何状态 exclusion，且其 Action 不存在或仍为 expected version 的 pre-Gateway 状态时，才进入 effective release set；`action_bound` 的 Action 必须已经是与 trigger 一致的 pre-Gateway terminal，或能在本事务转为该原因对应的 `failed|skipped` terminal。assignment 已为 `released`、永久 exclusion 已存在且原 bound Action 已不可领取时记 `already_released` 并回读首个 carrier，不新增 exclusion、不改计数；assignment 已进入 `claimed|gateway_started|unknown|consumed`，或 assignment/Action 版本变化、Action 已 `executing`/存在 Gateway 边界时记 `precondition_lost`，不允许释放或改写 Action。“assignment 仍 bound 但 exclusion 已存在”“assignment 已 released 但 exclusion 缺失”或“assignment/exclusion 已释放但原 Action 仍可领取”等不一致状态不能分类。这样同一 assignment 的 expiry、Action 终态和 Window expiry 三种不同 trigger 并发时，只有第一个有效 carrier释放，后到 trigger 幂等 no-op，不会因永久唯一键冲突而无限重试；release 与 `_confirm_claim` 并发时也只能由 release 或 claim 一方成功。

`precondition_lost` 只终结触发时冻结的旧版本因果，不代表“任意新版本都已释放”。状态机禁止从 `claimed|gateway_started|unknown|consumed` 倒退到 `reserved|action_bound`；observed 已越过 claim/Gateway 边界时，该 unit 永不再生成 release trigger。只有 observed 仍处于新的 `reserved|action_bound` pre-Gateway 版本——例如并发 replacement/资格复核只推进了 assignment 或 Action version——且“不再到期/已过期/资格终结”条件对该新版本仍成立，产生该新版本的状态变更事务或其 outbox 才必须生成全新 `release_trigger_key`。它不是旧 batch 重试，不能复用旧 candidate hash；没有版本变化事件不得轮询造新 trigger。这样既不会回滚远端边界，也不会让仍在 Gateway 前的新版本占用永久泄漏。

batch、候选分类、effective release set 的 assignment/Action 状态变更、exclusion、计数与 rebuild wave 更新在同一事务直接 finalize。每个 `effective_released + action_bound` item 必须使绑定 Action 在提交后处于与不可变 trigger 一致的 pre-Gateway `failed|skipped` 终态、清除 claim lease/active 标记并保留 assignment/Action 绑定作证据；已经是正确终态时幂等回读，禁止覆盖其他既有终态原因。提交后不得存在 `assignment=released` 但原 Action 仍为 `pending|claiming` 的组合。即使 effective release set 为空，也要以 `no_op` finalize 该 trigger，且不推动 rebuild wave。已存在同 trigger 且 `candidate_unit_set_hash` 相同，只有逐 item、release、count、outcome、wave 和重算 `outcome_hash` 全部一致时才只读回读；任一结果字段错绑进入 `release_fact_incomplete`。同 trigger 候选 hash 不同写 `release_batch_input_conflict` 并整批回滚。`applied|mixed` 中的全部 effective unit 必须一次提交，禁止事务级部分释放。

若发现无法分类的一致性矛盾，release 事务必须全部回滚，不能声称已在该事务“写入 quarantine”。回滚后由独立 consistency writer 重新按相同中央锁前缀读取；矛盾仍存在时，以 `(window,reservation,ordinal,issue_fingerprint)` 幂等持久化 active `consistency_quarantine`，保存 assignment、bound Action、exclusion、claim/Gateway 与计数的 observed state/version 及原 trigger。该 trigger 在 issue resolved 事件前不再定时重试，包含该 unit 的原子 batch 暂停，其他不共享该 unit 的 trigger、任务和 ordinal 继续。

Reconciler 进入分支前必须先验证“合法 release fact set”。首次 outcome 的合法集合由 `finalized SearchClickAssignmentEpoch + release_unit_set_hash 中的该 unit + 指向该 epoch 的 matching exclusion` 组成；post-finalize 释放的合法集合由 `finalized DispatchAllocationReleaseBatch + classification=effective_released 的 matching item + 指向该 batch 的 matching exclusion` 组成。carrier、unit key、hash、reason、版本和计数必须一致；缺件、错绑或 hash/版本不一致只可保持 `release_fact_incomplete` 对象级 quarantine，自动 Reconciler 不得把“只有 carrier”或“只有 exclusion”当成已释放。

`DispatchReservationReconciler` 对通过上述完整性验证的事实只能执行下列四个互斥分支：

1. 合法 release fact set 已存在且无 claim/Gateway：以该逐 unit 事实为权威；存在 assignment 时把它对齐为 `released`，首次 outcome 释放的未绑定 unit 则保持无 assignment；若原 bound Action 仍可领取，则按 carrier 原因原子转为对应 pre-Gateway `failed|skipped`、清除 lease/active并保留绑定；最后从逐 unit 事实重算 Reservation/Task/shard/Window 摘要，使该 unit 只贡献一次 released、零 bound/claimed，禁止再次递增 release。
2. assignment 为 `released`，但不存在任何 release carrier/item/exclusion，且无 claim/Gateway：按当前 Action 绑定恢复为 `reserved|action_bound`，递增 assignment version，并由新版本产生稳定 release trigger。存在半套或不一致 release 组件时不得进入本分支，只能保持 `release_fact_incomplete` quarantine。
3. claim/Gateway 已存在且不存在任何 release carrier/item/exclusion：绝不回滚远端边界；assignment 与 Reservation 只可向 `claimed|gateway_started|unknown|consumed` 对齐，并按已提交事实重算摘要。
4. 合法 release fact set 与 claim/Gateway 同时存在：写 `release_claim_fact_conflict` 并保持该 unit 的 active quarantine。自动 reconciler 不得删除 release 组件、回滚 Gateway、选择 release 或 claim 一方，也不得调整该 unit 的 released/claimed 计数；不能写 resolved 事件或定时重试。若完整 click evidence 已确认，click 事实仍按真实远端事实入账，但该 quarantine 清除前相关 ledger 不得通过 E4。

只有前三个无冲突分支提交后才 resolve issue并唤醒原 trigger；原 trigger随后按当前事实 finalize 为 effective/already-released/precondition-lost，绝不重跑搜索求解。第四分支仅隔离该 unit，不阻塞其他独立任务和 ordinal。这样隔离是对象级一致性处置，不是新增整任务结构门禁，也不会形成忙循环。

`DispatchAllocationExclusion` 固定为当前 Window 内的持久事实：

```text
dispatch_claim_window_id
source_dispatch_allocation_epoch
source_search_click_assignment_epoch
source_dispatch_claim_reservation_id
source_fulfillment_lane_claim_ordinal
allocation_business_task_id / lane / shard_id
resource_snapshot_hash / reason_code / evidence_hash
release_carrier_type / release_carrier_id
state = active | superseded | expired
created_at / superseded_at / expired_at
```

每条 exclusion 固定 `release_count=1`，`release_carrier_type/id` 只能指向首次 search outcome 或后续 release batch 二者之一。永久唯一键为 `(dispatch_claim_window_id, source_dispatch_claim_reservation_id, source_fulfillment_lane_claim_ordinal)`；`resource_snapshot_hash` 是释放证据和 active exclusion 对新权重的适用性字段，不参与幂等唯一键。该 hash 必须是按 `reason_code` 规范化的“相关资源快照”：固定包含 Window、Task/ledger/target、assignment/Action expected version，以及真正导致本 unit 无法绑定、饱和或终结的账号额度窗口、关键词额度、授权、代理、协议/CAPTCHA、Gateway 容量等资源 key/version；不得包含无关 Task、无关 shard、worker/lease、扫描时间或随机值。对于 `no_feasible_search_path|search_solver_abandoned`，系统必须从 carrier-independent `solver_problem_hash` 按该 unit 所在连通分量投影出 `solver_problem_component_hash` 并把它作为 `resource_snapshot_hash`；该 component hash 使用稳定业务义务/候选/资源/公平输入，明确排除 Window/dispatch/search epoch、Reservation/ordinal/assignment ID 和仅由本次 carrier 产生的份额计数。完整 `solver_input_hash` 只用于 outcome 幂等，禁止用它判断 exclusion 是否 superseded。只有该原因直接依赖的业务问题分量或资源版本发生变化，active exclusion 才能转 `superseded`；仅创建新中央/search epoch、换 worker、换 Reservation ID、推进 carrier 版本或改变无关资源都必须保持 active，防止“abandoned -> 重建 -> 同问题再分配”循环。exclusion 转 `superseded|expired` 后，同一旧 Reservation unit 仍不得再插入第二行、再次增加 `released_count` 或恢复 claim；新事实下重新获配必须来自新 epoch 的新 Reservation/ordinal。epoch、release batch、release batch item、exclusion 与来源 Reservation 在活跃写域中必须共同保留；不得单独清理 item、expired exclusion 或 finalized carrier 使逐 unit 结果和唯一键失真。联合归档只能在旧 writer 全部 fence 后冷存 payload，主库必须永久保留最小不可变 identity tombstone：carrier key/hash、batch item 的 candidate unit、assignment/Action expected+observed version、分类/首 carrier 引用与 `(window,reservation,ordinal)` unit key/released 状态，且 tombstone 唯一键不可删除或复用。这些 tombstone 是幂等身份，不是运营启动历史。`reason_code` 只允许 `no_feasible_search_path|search_resource_saturated|protocol_ineligible_for_snapshot|search_solver_abandoned|search_reservation_cas_abandoned|search_assignment_pre_gateway_terminal|search_assignment_expired|unclaimed_action_no_longer_due`。

首次 search outcome 与后续 release batch 共用一套 finalize helper，固定按 `Scope -> Window -> TaskAllocation -> ShardAllocation -> Reservation` 加锁，同层多行按主键稳定排序；随后依次锁 release carrier、既有 assignment、搜索 consumptive 子预留和 Action，其中不存在的层直接跳过但顺序不可交换。首次 outcome 复核 `SearchClickAssignmentEpoch.state=open`，后续释放复核稳定 trigger 尚未 finalized。`optimal` 必须先验证 matched ordinals 与 release ordinals 互斥、全部 matched 绑定和 release set 的 CAS 前置条件，再验证 Window 仍可领取、`allocation_state=ready` 且当前 `dispatch_allocation_epoch` 与 search epoch 完全一致；任一条件不满足，本次最终结果直接改为 `abandoned`，不写部分 assignment，并把全部仍未领取 unit 作为 release set。不能因更高 epoch 已重建回 `ready` 就把旧 search epoch 误当成当前版本。

事务逐个验证 matched/release ordinals 均位于 `1..reserved_claims`。首次 outcome 的 matched/release unit 不得被既有 assignment、claim、active 事实或任何状态 exclusion 占用，并按 Reservation 验证 `bound_count + new_matched_count + claimed_count + released_count + unbound_release_count <= reserved_claims`。post-finalize batch 不含 unbound unit；先按上条把候选分类，只有 effective release set 中已经绑定的 `reserved|action_bound` assignment 进入计数转移。它验证 `bound_count >= bound_release_count`，更新前后 `bound_count + claimed_count + released_count` 总和守恒且不超过 `reserved_claims`。`already_released|precondition_lost` unit 不进入本次 release count，也不重复执行任何计数或 Action 变更。原 Task/shard/Window 的 `unclaimed_allocated_count` 均不得小于本次 effective release count。全部通过后才一次性插入 assignment/exclusion并更新：首次 matched 执行 `bound_count += new_matched_count`；首次 outcome 释放未绑定 unit 执行 `released_count += unbound_release_count`；后续 batch 同时终结仍可领取的 bound Action、执行 `bound_count -= bound_release_count/released_count += bound_release_count` 和 assignment `reserved|action_bound -> released`；所有实际 release 统一按原 Task/shard/Window 扣减 unclaimed。

非空 effective release set 对 rebuild wave 的更新固定为：Window 尚可领取且为 `ready` 时 `dispatch_allocation_epoch += 1`、`allocation_state=rebuild_required`、`rebuild_input_version += 1`；Window 已为 `rebuild_required` 时只 `rebuild_input_version += 1` 并复用当前 pending epoch；Window 已结束时二者均不变。release carrier 保存实际 `next_dispatch_allocation_epoch/rebuild_input_version_after`。effective 集合为空时 batch 仍可按 trigger finalize 为 no-op，但不修改 Window。任一步数据库错误、CAS、唯一键或计数守卫失败则整笔回滚，search epoch 仍 open或 release batch 不存在；`DispatchReservationReconciler` 只处理真实计数/状态不一致，先在同一锁序下按 assignment、claim 与永久 exclusion 事实重算 Reservation/Task/shard/Window 摘要计数，再继续 finalize 同一 `abandoned` outcome或同一 release trigger。合法的 `already_released|precondition_lost` 是终态分类，不进入 reconcile/retry。全过程绝不重跑搜索求解或猜测 unit；重入只允许确认相同 carrier/outcome/hash 已 finalized，不能部分释放、双扣、改写 claimed unit或在同一 pending wave重复递增 epoch。

`DispatchLaneShardSolver` 只在 `allocation_state=rebuild_required` 时为当前 pending epoch 读取最新 due、eligibility、全部仍有效的旧 epoch Reservation 承诺和 active exclusion，按相同 task/lane/shard/reason-scoped resource snapshot 的 active unit 数扣减本轮可再次获配数，再计算可用余额的完整新分片权重。无事务快照必须同时冻结 `dispatch_allocation_epoch + rebuild_input_version + dispatch_rebuild_snapshot_hash`。该 hash 的规范化 payload 固定包含 `(window,pending_epoch,rebuild_input_version)`、全部 task/lane/shard 的 due/eligibility 稳定键/当前值/版本、全部 active exclusion 的 unit/state/reason/resource snapshot、全部仍有效旧 Reservation 的身份/承诺计数/版本，以及 scope/shard 容量与影响分配结果的配置值/版本；稳定排序后取 hash，禁止包含 worker/lease、扫描或墙钟时间、进程身份和随机值。提交前必须在中央锁序内重新读取同一规范化输入并重算 hash；epoch、input version 或 hash 任一变化，即使没有 release batch 推进 input version，也整批丢弃旧权重并由下一 drain 从最新事实重建。成功提交时，Window 的 `ready_rebuild_snapshot_hash` 与本 epoch 全部 TaskAllocation/ShardAllocation/Reservation 的 `dispatch_rebuild_snapshot_hash` 必须写成同一值，并和 `allocation_state=ready` 原子发布；零余额也发布带该 hash 的空 `ready`。计算、版本复核、数据库错误或 worker 崩溃同样不得发布部分权重。重建期间已 bound/claimed/active 及其他未释放旧 Reservation 仍按各自版本收口，已 released unit 永远不可再 claim；新 epoch Reservation 只有随 `ready` 原子提交后才可领取。Window 已结束则不再运行本 solver，由下一 Window 创建新 allocation。其他 shard 或资源向量仍可获得份额；只有 reason-scoped 相关资源快照改变时旧 exclusion 才转 `superseded`，业务欠额可在新事实下重新参与；Window 结束统一 `expired`。排除事实不得跨 Window、任务日或目标复用，不得由软 pacing、账号排序或无关事实变化产生，也不得减少 click 欠额。

上述 payload 清单是最低集合，不是允许漏项的白名单。规范化 payload 还必须包含 `dispatch_rebuild_contract_version`、`DispatchClaimScope/Window/Shard` 的 capacity/active/unclaimed 当前值与版本、所有参与决胜的 scope/task-lane/shard fairness cursor 与版本，以及 `allocation_business_task_id` 的 parent/sponsor 聚合输入；凡 `DispatchLaneShardSolver` 新增任何会影响 allocation/reservation 输出的业务读取，都必须加入 payload 并提升 contract version。contract version 只进入 hash payload，不新增独立状态列；纯诊断字段不影响输出时不得进入。由此，旧 Window 的 claim 完成/新增、并发 Window 推进 cursor 或 sponsor 变化即使没有 release batch，也会在 precommit rehash 被拒绝。

实现边界必须用唯一不可变 `DispatchRebuildInput` 固化上述 payload：assembler 从数据库构造、稳定序列化并取 hash，`DispatchLaneShardSolver` 只能接收该对象且不得自行查库或读取全局状态；allocation/reservation 输出同时带回其 input hash。precommit 在中央锁内重新构造同一对象，而不是只重查手工挑选的版本列。这样“hash 的字段集合”与“solver 真正使用的输入”不会分叉。

precommit rehash 与新 allocation/reservation/Window ready 写入必须处于同一个短 PostgreSQL `SERIALIZABLE` 事务，并按中央锁序先锁 Scope/Window；该事务内重新构造完整 `DispatchRebuildInput`、比较原 hash，再写全部结果。SERIALIZABLE 必须覆盖 assembler 的行读取和候选谓词，防止 rehash 完成到 commit 之间的更新或 phantom 插入漏过。任何 serialization failure、版本/CAS 冲突或 hash 不等都直接回滚并丢弃本批计算结果；禁止 ORM/驱动自动用旧结果重放事务，下一 drain 必须从新 input 重新计算。若实现不用 PostgreSQL SERIALIZABLE，只允许使用能够证明覆盖同一行集与候选谓词的显式 version-row/predicate fencing 等价方案，不能只锁 Window 或复核标量版本。

`dispatch_rebuild_contract_version` 或搜索 `solver_contract_version` 升级禁止新旧 Dispatcher 滚动混跑。版本只在规范化 hash payload 内，不另建运行历史；因此 Release Gate 必须先停止旧版本取得新的中央重建/search epoch ownership，等待并证明所有旧 Dispatcher 进程已终止且不存在旧版本仍可提交的数据库事务，再启动新版本。旧进程内尚未提交的 solver 输出全部作废；`rebuild_required` Window、尚未建立 search epoch 的 ready 份额继续保留，由新版本重新 assemble/solve。若旧 owner 已留下 open `SearchClickAssignmentEpoch`，必须先 fence 旧 owner，再按既定 recovery 直接 `abandoned` 并释放未领取 unit，不能把 ownership 转给新版本或沿用旧输出。无法证明旧版本已失去写资格时发布失败，不得让两个 contract version 同时提交；这是一条部署一致性栅栏，不是 solver deadline、任务 blocker 或业务重试次数。

搜索精确求解在冻结的无事务快照上运行。commit 时必须重新从 `Scope -> Window -> TaskAllocation -> ShardAllocation -> DispatchClaimReservation` 取得版本化前缀，随后锁 search epoch carrier；按稳定 assignment key处理既有 assignment、按稳定 resource key CAS 账号/关键词 quota 子预留，最后创建/锁 Action。`_confirm_claim`、Gateway 前最终守卫、release 与 Reconciler 复用该相对顺序；禁止求解期间持锁，或先锁 Action、assignment、搜索资源再反向锁中央分配行/carrier。

若 aggregate `required_claims` 超过全局 Window、或 TaskAllocation 无法完整映射到 shard 可用余额，所有受影响任务必须写 `shared_dispatch_capacity_insufficient` / `shard_mapping_insufficient` 及其 scope、shard、epoch、所需与可用数量；这只说明 shared claim 资源不足，不得承诺目标一定不能由其他尚未计算的远端因素完成。

### 5.3 可验证的领取规则

一次 claim 只能领取仍有 `reserved_claims - bound_count - claimed_count - released_count`、已经按软节奏到期、且通过账号、截止、授权环境和 Gateway 硬前置条件的 Action。quiet-hours 只参与 planned time 的非零低权重计算，不是 claim 拒绝条件。只有 `_confirm_claim` 成功后，才可在同一短事务把对应 unit 从 bound 转 claimed、将对应 shard/window 的 `unclaimed_allocated_count` 减一并将其 `active_claim_count` 加一；失败、过期或安全门拒绝不得消费份额。Action.result 必须记录：

~~~text
dispatch_claim_class
dispatch_reservation_id
dispatch_claim_window_id
dispatch_claim_task_allocation_id
dispatch_claim_shard_allocation_id
dispatch_allocation_business_task_id
dispatch_claim_lane
dispatch_claim_scope
dispatch_claim_shard
dispatch_allocation_epoch
dispatch_reservation_reason
dispatch_urgency_score
dispatch_unserved_strict_classes
~~~

一个业务任务有 due Action、仍有未 bound/claimed/released unit 且通过安全门时，其他任务不得直接占用其 Reservation。普通任务无可领取安全 Action时按 §5.2 的 unit release 合同放弃该份额；纯搜索点击在首次 search outcome 前没有 Action 是正常物化阶段，只能由该 `SearchClickAssignmentEpoch` 绑定或释放，finalize 后才由 assignment 对应 release batch 处理。实际 release set 非空才立即重建分片权重，空集合不得推动中央版本。不能保留无 owner 的空占，也不能在不写 exclusion 的情况下转给其他任务。Action 终结时必须从 window 与 shard 的 active 账释放对应 claim。任何 Reservation 只保证 claim 机会，不保证 Telegram 目标出现或点击。

## 6. 极搜协议与会话状态

### 6.1 页面相位分类

关键词发送后的机器人响应必须先按已审核、版本化的 `BotProtocolSample` 指纹分类。分类优先级固定为 `verification_page > hot_list_page > search_category_page > group_result_page > unknown_page`；不能仅因“没有群聊 selector”就推断它是搜索分类页。响应不匹配任何当前样本时必为 `unknown_page`。

| page_phase | 处置 |
| --- | --- |
| search_category_page | 允许按当前批准的群聊 selector 进入群聊结果页 |
| group_result_page | 允许精确目标匹配、分页和目标点击 |
| hot_list_page | PRD §2.19: 直接写 jisou_session_state_deviated，账号 24h 排除，不尝试重置 |
| verification_page | 写 `bot_human_verification_required`；图片算式验证码进入 `jisou_image_verification_required -> solved|failed` 实际状态链，`required` 不终结当前 Action、不触发 24h 排除 |
| unknown_page | 写 jisou_session_state_deviated，账号 24h 排除，不点击任何未知 button |

### 6.2 热搜页处置（PRD §2.19 已更新）

当 `page_phase=hot_list_page` 时，PRD §2.19 已禁止受控会话重置（`/cancel`、`/start`、重发关键词线上验证不可行，极搜把关键词当文本回显不执行搜索）。当前处置：

- 直接写 `jisou_session_state_deviated`，账号 24h 排除；
- 不得点击 热搜排行榜 页面中的外部 URL、未知 callback 或 群组导航 外跳链接；
- 不得将该结果写成 `jisou_group_selector_missing`；
- 不得发送 `/cancel`、`/start` 或重发关键词作为恢复手段。

只有已经确认是 search_category_page，且没有协议样本批准的群聊 selector 时，才写 `jisou_group_selector_missing`。该错误不触发账号 24 小时排除，只用于协议样本复核。`jisou_image_verification_required` 只是当前 Action 的识别中状态；真实通过后写 `jisou_image_verification_solved` 并继续同一 source，最终失败才写 `jisou_image_verification_failed` 并按主 PRD §2.19.3 触发账号—协议路径 24 小时排除。`jisou_session_state_deviated` 继续触发 24 小时排除。

账号级小时安全额度由系统 `AccountUsagePolicy`、关键词/授权槽位策略和 Telegram 实时限制聚合，不由任务表单配置。存量 `pacing_config.per_account_hourly_action_limit` 只保留为容量充足时的软分散提示，不能在日 click 欠额尚存时形成停止门禁；小时计数只统计对应系统安全窗口内真实占用额度的 `search_click` source，不得让更早窗口遗留的 pending Action 永久占用当前窗口额度。图片算式验证码的 AI 识别调用和批准重试不进入该 source 限额、账号/关键词 click 限额或 click 目标计数。

运行期硬安全产能必须在极搜 24 小时账号排除后按当前事实计算，有效账号数使用本轮真实 selector 候选数，禁止使用 `captcha_trigger_rate`、AI 历史成功率或任何概率折损公式。尚未进入验证码页的路径只可按其他硬资格进入 eligible attempt capacity；已经出现图片验证码的路径只有真实写入 `jisou_image_verification_solved` 后才恢复 click opportunity，`required|failed` 及 required 下的 unavailable/unknown 原因均不能计预测确认或 confirmed。该状态只用于 blocker、catch-up 和系统选号，不阻止创建。`daily_fulfillment.effective_account_count` 必须记录真实候选数，不能从静态配置回填。

### 6.3 协议样本与审计

每次 source 必须写 `SearchJoinProtocolTrace`，至少含：

- bot_username、protocol_sample_version、page_phase；
- attempt_no、event_type、分类输入摘要、前后 page_phase；历史重置字段只读保留，新 Action 固定写 `recovery_kind=not_applicable/reset_executed=false`；
- 每个 button 的 row、col、button_type、effect、text_length、导航标识及批准样本匹配标记；
- 目标按钮分开记录 `click_effect` 与 `membership_side_effect`；纯点击只接受 `membership_side_effect=none`，并记录 `membership_mutating_rpc_invoked=false`；
- 被选择 selector 的位置、批准样本版本和点击结果；
- 历史行已有的 reset 次数/前后相位只读展示为历史事故字段；新 Action 禁止写 reset 事件、次数或恢复成功；
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
    {
      "page_phase": "group_result_page",
      "button_effects_any": ["target_open_only", "navigate_only"],
      "membership_side_effects_allowed": ["none"]
    }
  ]
}
~~~

`normalized_text` 只能取受控枚举 `human_verification`、`hot_list`、`jisou_group_category`、`jisou_channel_category`；运行时只在内存中以对应受控文案比对，落库只保留 hash、长度与 `approved_sample_match`。`selector_rules` 必须固定 row、col、callback 类型、effect 和 `jisou_group_category` 枚举；不能按包含关系、未知 callback 或动态原文猜测 selector。`group_result_page` 只允许使用已批准的 `target_open_only` / `navigate_only` effect 变体，并且只点击被当前指纹命中的目标或分页位置。

纯点击的按钮协议还必须满足：

1. `click_effect=target_open_only|navigate_only` 只表示批准的目标按钮调用或 Telegram 内部目标预览/resolve；`membership_side_effect` 必须独立为 `none`。
2. `membership_side_effect=join|request_to_join|follow|unknown` 的路径不能进入 `click_only` eligibility；该账号/协议路径写 `click_only_membership_effect_not_allowed`，其他合法路径继续。
3. Gateway 对 `click_only` 禁止调用 `JoinChannelRequest`、`ImportChatInviteRequest`、申请入群、关注频道、群管确认和 can-send 复检；调用前审计必须为 `membership_mutating_rpc_invoked=false`。
4. 任意旧样本只有 `join_candidate` 或没有 `membership_side_effect` 时，默认写 `click_only_membership_effect_unproven` 并等待人工基于真实脱敏样本重新分类；不得用已有 membership 状态证明“无副作用”。纯 click profile 的 group result 必须至少包含一个 `target_open_only`，只有 `navigate_only` 不能进入 eligibility。唯一可自动迁移的闭集例外是历史解析器自身生成的 `jisou-v2-2026-07-28`：该解析器把所有 Telegram 内部 URL 统一标成 `join_candidate`，但纯点击 Executor 对该控件只调用目标 URL、从不调用 join/request/follow/confirm/can-send RPC。发布接管必须把精确匹配“已脱敏 active 样本 + group result effects 仅为 `join_candidate|navigate_only` + 未声明成员副作用”的旧行保留为 inactive 历史，并创建 `jisou-click-only-v3-2026-07-29` replacement，将 Telegram 内部 URL 固化为 `target_open_only`、声明 `membership_side_effects_allowed=[none]`、写审计。结构、版本或 effect 集合任一不匹配仍禁止自动迁移。
5. 该阻塞只在 Task 启动后形成运行路径 blocker，不影响结构合法 Task 创建，也不得触发未来 `click_and_join` 模式。

Planner 必须把校验后的 profile 与 `protocol_sample_version` 一起冻结进 source payload；Dispatcher 对旧 Action 或缺 profile 的极搜 payload 在 Gateway 前写 `protocol_sample_invalid` 并结束 Attempt，不得回退到硬编码猜测。最后一道顺序固定为：先复核任务仍 running、scheduled_end、账号安全和授权/代理环境，再校验 profile，最后才写 `gateway_call_started_at` 并调用 Gateway。quiet-hours 是 Planner 的非零低权重，不是 Gateway 硬门禁，不得再写 `quiet_hours_active` 终态；缺 profile 也不得遮蔽 `task_not_active`、`scheduled_end_reached` 或授权环境原始错误。旧样本需要由人工基于真实、脱敏采集重新审核和版本化；系统不得自动把旧 `buttons` 摘要升级为已审批指纹。

## 7. ExecutionAttempt 与事实观测

source Action 在 Gateway 调用前必须原子创建或取得同一个 `ExecutionAttempt(attempt_no)`，先写 `before_gateway`，再写 `gateway_call_started_at`。Gateway 成功、明确失败、超时或进程异常均回写该 Attempt：调用后无法确认结果时写 `unknown_after_send`，调用前被门禁跳过时写 `skipped_before_gateway`。不得为一次 Gateway 调用新建多个 Attempt，也不得把 `Action.result.gateway_call_started` 当作唯一事实源。

任务详情必须将以下量分开：

- pending / claiming / executing；
- 已进入 Gateway 但结果未知；
- target_click_observed；
- jisou_session_state_deviated；
- jisou_group_selector_missing；
- daily_target_capacity_insufficient；
- shared_dispatch_capacity_insufficient。

历史缺少 ExecutionAttempt 的 Action 只作为历史观测缺口展示，不能被反推为“没有进入 Gateway”或“已成功”。

## 8. 前端、接口与权限

创建和专用编辑只做结构校验。成功响应返回：

```text
POST /api/tasks/search-click
POST /api/tasks/search-click/create-and-start
permission = tasks.manage + tasks.create.search_click
task_type = search_click
search_execution_mode = click_only
```

以上是本轮唯一允许新建任务的业务入口。存量 `/api/tasks/search-join-group` 及 `tasks.create.search_join_group` 只作旧任务读取、审计和迁移识别，旧创建请求固定返回 `410 legacy_search_join_create_retired` 并指向 `/api/tasks/search-click`；不得由兼容路由代建或规范化创建新的纯搜索点击任务，也不得出现在新前端、公开 API、权限配置、日志业务名或后续“搜索点击加入”授权中。

~~~text
task_id
task_status
create_status
start_status
runtime_state
runtime_blocker_codes
request_fingerprint
start_operation_id
start_operation_version
start_operation_legacy_untracked
search_execution_mode
daily_click_target_count
~~~

不得在创建前请求临时 ledger、容量 seed 或风险确认。请求携带稳定 `client_request_id`，后端对规范化目标、账号范围、关键词、`search_execution_mode=click_only`、每日 click 目标和 `start_requested` 生成并持久化 `request_fingerprint`。同键同 fingerprint 返回原 Task；同键不同 fingerprint 返回 `409 idempotency_key_reused`，不得覆盖或静默返回旧配置。首次创建返回 201，幂等重放返回 200。

`start_status` 只允许 `not_requested|started|start_failed`，`runtime_state` 只允许 `not_started|runnable|waiting`；账号、代理、协议或安全容量暂不可用时，启动事务仍成功，返回 `start_status=started/runtime_state=waiting`，Task 保持 running。`create-and-start` 先幂等持久化 Task，再以由 `client_request_id` 派生的唯一 `start_operation_id` 进入统一 start 流程。启动事务失败必须整体回滚该事务内的 Task 状态、ledger、assignment 和启动事件，Task 保持 `draft`；首次响应仍以 HTTP 201 返回原 `task_id/create_status=created/start_status=start_failed`。

每个 Task 允许 0 或 1 条当前 `TaskStartOperation`，有行时以 `task_id` 唯一；新合同下的真实 start/create-and-start 必须建立该行。当前行保存单调 `operation_version` 作为并发栅栏，但不保存历史 payload。相同 key 重试把 `failed/start_failure_code` 原行覆盖为 `processing` 并把 version 加 1。新 key 重试失败启动或重启 stopped Task 时必须携带 `replaces_start_operation_id + replaces_start_operation_version=current`，并在 Task 行锁内对 tuple 做 CAS 后整体覆盖同一行、version 加 1；不保存启动 attempt 或旧状态。B 回滚后的独立 failure writer 必须以 B 冻结的 `(expected_previous_start_operation_id, expected_previous_start_operation_version)` 重新锁 Task/current row 并 CAS，仅在 tuple 未变化且 Task 仍 draft/stopped 时写本轮 failed 和 `operation_version=expected+1`；current tuple 已被 same/new key 重试推进或 Task 已 running 时只能回读，不能写 failed。请求 replace tuple CAS 不等返回 `409 stale_start_operation`，其他请求正在 processing 返回 `409 start_in_progress`。成功后只保留 `started` 并清空 failure；若上次已经提交但响应丢失，任何 key 都回读既有 running/ledger，不能重复启动或新建 Task。

发布前已 running/paused 的存量搜索 Task 不从历史日志补造 operation；重复 start 或同一 ledger 内 resume 只回读现有 Task/ledger，返回 `start_operation_id=null/start_operation_version=null/start_operation_legacy_untracked=true`，启动事务调用数为 0。resume 不创建或覆盖 operation。存量 draft/stopped 同样不补历史，首次真实启动才创建 version 1 当前行；存量 running/paused 明确停止后的首次真实重启也从 version 1 建立当前行。该兼容不允许 Task 因缺 operation 重复启动。

零当前行的响应固定区分：新建但未请求 start 的 draft 为 `start_status=not_requested`、ID/version null、legacy_untracked=false；发布前存量 running/paused 为 `started/null/null/true`；发布前存量 draft/stopped 为 `not_requested/null/null/true`。真实 start 一旦进入 processing/started/failed，ID/version 必须非空且 legacy_untracked=false。

任务详情新增当日履约面板：

- 当前 `task_day_ledger_id`、本地日期、冻结时区/revision、UTC period 和 `day_phase`；时区过渡 ledger 与完整日不得合并；
- 单一 click 进度，不展示 admission-ready、membership 或入群开关；
- `remaining_click_count/planning_click_deficit`、只读 `projected_eligible_attempt_capacity_before_deadline/projection_not_reserved`、当前 Window `committed_click_opportunity_count`、`hard_safe_attempt_capacity`、catch-up 状态、完整/部分天语义和硬安全不足原因；明确标注 attempt upper bound，不展示预测确认量；软 skip/jitter/quiet 不显示为目标扣减；
- 系统当前账号路径选择原因；该信息只读，不开放账号优先级；
- Claim Window / Reservation 按 scope、shard、类别、需求、预留、已绑定、已领取、已释放和未服务原因；诊断详情分列 `dispatch_allocation_epoch`、`rebuild_input_version`、Window `ready_rebuild_snapshot_hash`、allocation/reservation `dispatch_rebuild_snapshot_hash`、`search_click_assignment_epoch`、`solver_problem_hash/solver_input_hash/result`、unit-level `solver_problem_component_hash`/active/superseded exclusion 和分片权重重建结果，运营不可编辑；
- 极搜 page_phase、selector、验证和协议版本；新执行不展示可操作 reset；
- 账号级阻塞列表与原始错误。

所有编辑仍要求 tasks.manage；查看协议 trace 和账号明细仍受 tasks.view 与目标访问权限控制。没有“强制成功”“跳过协议”或“自动降低目标”的写入口。

详情查询以 `task_day_ledger_id` 为权威参数；兼容 `date=` 在同一显示日期命中多份 ledger 时返回 `409 ambiguous_task_day_ledger` 及候选摘要，不得按本地日期合并 click。

## 9. QA 验收

| 场景 | 必须证明 |
| --- | --- |
| 创建 1000 点击目标 | 合法请求直接创建 Task；不调用容量预检、不需要确认、不产生临时 ledger/source |
| 创建权限与运行事实分层 | 缺 `tasks.manage` 或 `tasks.create.search_click` 任一权限返回 403、不可见引用不泄露存在性、mode/目标字段非法返回 422；inline 公开目标只允许本地规范化/upsert pending 引用，创建事务不得调用 Telegram resolve/probe；账号在线、Telegram 权限、代理、协议和容量均不得在创建前读取 |
| create-and-start | Task 先持久化，再由统一 start 创建真实 ledger；Planner 首轮才返回运行容量和 blocker |
| create-and-start 启动事务失败后重试 | 首次 HTTP 201 保留已创建 task_id 且启动事务无残留 ledger/assignment；相同 client_request_id/fingerprint 复用同一 start operation，重试时覆盖该行旧 failure 并推进 `operation_version`，成功后只保留 started，不产生第二条任务、历史 payload或第二份 ledger |
| 启动成功但当前无安全路径 | `start_status=started`、Task=running、`runtime_state=waiting`；不得把运行 blocker 写成 `start_failed`。相同 key 重放、new key 携带 replace ID/version tuple CAS、不同 key 并发均只产生单 ledger；迟到 replace 返回 stale，processing 冲突返回 `start_in_progress` |
| 旧启动失败落账迟到 | 分别让 same key/new key 在旧 B 回滚后先进入新一轮 processing/started，再恢复旧 failure writer；expected previous ID/version tuple CAS 失败，只能回读新 current，不能覆盖为 failed |
| 存量任务无 start operation | running/paused 任务只回读 started/ledger，operation ID/version 为空并标记 legacy_untracked，启动调用数为 0；paused resume 不写 operation；draft/stopped 首次真实启动才创建唯一 version 1 当前行，不补历史 |
| 幂等键被不同配置复用 | 同一 client_request_id 携带不同目标、click 数或 start mode 返回 `409 idempotency_key_reused`，原 Task 不被覆盖 |
| 纯点击字段边界 | 携带 join switch、admission 目标或成员目标返回 `422 field_not_allowed_for_click_only`，不创建 child |
| 旧搜索创建路由 | `/api/tasks/search-join-group` 的新建请求返回 `410 legacy_search_join_create_retired` 且不创建 Task；旧路由/权限只读存量与迁移识别，不能代建 `search_click` |
| 存量混合任务 | 发布 takeover 把未结束 Task 幂等迁移为 `search_click + click_only`，终结未进 Gateway 的 membership child；已进 Gateway child、历史远端事实和原绑定只读保留，不从纯点击编辑入口改写 |
| 20 个可执行小时、20 次每小时、1000 点击目标 | 任务保持 running，显示硬安全容量缺口并在允许速率内持续追赶；不伪造 Action/成功、不因风险停止任务 |
| 普通曲线低于目标 | 自动进入 catch-up；在硬安全上限内压缩软 pacing，不继续按一轮一条慢速节奏 |
| 0 click confirmed、1000 个 held/unknown source | `remaining_click_count` 仍为 1000；只有 `planning_click_deficit` 因防重暂为 0，任务不得显示完成 |
| 存量 max_actions_per_hour/day 小于日目标 | 旧值不形成完成门禁；系统按真实账号/关键词/授权/代理/Gateway 硬安全容量追赶并展示聚合上限 |
| 配置含整窗口或 Action skip | soft skip 不产生终态、不扣减日目标；quiet 使用非零低权重，catch-up 时按最大合法速率执行 |
| CAPTCHA 投影语义 | API/schema/UI 不存在 `projected_confirmable_clicks_before_deadline`；未进入验证页只计 eligible attempt 上界，进入验证页后只有真实 solved 才恢复 click opportunity，任何 projection/assignment 都不能计 confirmed |
| 今日仅剩部分窗口、明日可完整执行 | 分别展示当前部分日和首个完整日；今天 blocked 不粘连到明天 |
| 存量任务日中变得不可达 | `daily_outcome=blocked` 只表示当前证据下的履约风险/结论，Task 仍 running；已有 Gateway source 正常收口，其他合法路径继续重算和追赶，不把 blocked 当停止 Planner 的终态，也不假装严格目标可达 |
| 只找到目标或按钮、尚无可证点击结果 | 不增加 `target_click_observed`；证据完整才 confirmed，调用后结果不可辨时进入 unknown |
| 旧 `join_candidate` 或成员副作用未知 | 除上文精确 `jisou-v2` 解析语义迁移外，不进入纯 click eligibility，写 `click_only_membership_effect_unproven|not_allowed`；不得调用任何 join/request/follow/confirm/can-send RPC，其他合法账号路径继续 |
| click evidence 跨 Task 复用 | 同一非空 evidence hash 只能绑定一个 Attempt/ordinal；事实早于 ledger 起点不得倒灌，冲突 ordinal quarantine 而其他 ordinal 继续 |
| 目标群未出现在结果中 | source 明确失败且不增加 target_click_observed；释放 assignment 后允许同一 click ordinal 在硬安全约束内 replacement，open/unknown 不重复 |
| 共享资源笛卡尔积 | 一个账号额度对应多个 keyword/授权/代理组合时，projection 只按共享资源约束精确匹配且标记未预留；当前执行机会只按绑定中央 Reservation 的 committed assignment 统计，不能把候选组合数相加 |
| 两个搜索 Task 共享账号/关键词额度 | 全局只读 projection 对共享单位去重且不写 hold，两个 Task 的 projected 总和不超过真实共享额度；当前 commit 中每个资源单位只被一个 assignment 子预留占用，CAS 冲突整项回滚重算 |
| 约束求解失败或候选量大 | 候选按 ordinal + 共享资源 + task fairness 连通分量拆分；依次证明最大 click 数、最大受服务到期任务数和冻结 remaining 比例的最大最小任务公平向量；`optimal` 原子提交全部合法匹配并把 unmatched 组成一个 release set，`no_candidate|abandoned` 把全部未领取 unit 组成一个 release set；尚可领取 Window 的非空集合只开启或加入唯一 pending rebuild wave，禁止 top-N/贪心/部分解或原 epoch 重试 |
| 两类求解器边界 | `DispatchLaneShardSolver` 只映射中央 task-lane 份额到 shard；`SearchClickAssignmentSolver` 只在既有 fulfillment 份额内选择 click path。搜索求解器不得增加中央份额，中央求解器不得推断账号 click 成功 |
| 两类 epoch 边界 | 尚可领取 Window 从 `ready` 的首批非空释放只创建一次 pending `dispatch_allocation_epoch`；同一 wave 后续释放只递增 `rebuild_input_version`，权重发布为 ready 后该中央版本只建立一次 `search_click_assignment_epoch`。搜索 epoch 只求解/提交一次，失败直接释放全部未领取 unit；已结束 Window 不建立新 epoch。任一版本变化均不重置 ledger、ordinal、安全额度或 unknown |
| 两个 worker 同时发现同一中央版本 | open epoch 创建事务只允许一个 `solver_owner_lease_id` 成功；另一个 worker 只回读且不得求解。只有 owner 进程失联、fencing token 失效或续租所有权丢失后 recovery 才直接 `abandoned`，不转交、不重算且不新增 attempt/history |
| owner lease 不形成隐藏 deadline | 健康 owner 的求解跨过多个单次租约周期时持续续租，不得因耗时、心跳次数或续租次数 abandoned；只有模拟进程失联、fencing token 失效或续租所有权丢失才由 recovery abandoned |
| open 后 owner 丢失 | open 前必须已原子持久化完整 problem snapshot、全部 component 与每个 Reservation/ordinal 的唯一 binding；recovery 只用原 binding/component hash 释放全部未领取 unit，重新组图和额外 solver 查库调用数为 0。缺 component/binding、共享 resource/fairness key 被拆分或 payload/hash 不一致时零释放、零重建并进入对象级 quarantine |
| finalized outcome 幂等重放 | 由 carrier Window/dispatch/search epoch、原 `solver_problem_hash/solver_input_hash`、全部 matched assignment identity/version、精确 release set 及实际 next dispatch epoch/input version 重算同一 `outcome_hash`；重复调用零新增 assignment/exclusion、零计数变化、零 epoch/input version 变化。任一 carrier/problem/input/unit/reason/resource/wave 字段错绑时保持 `release_fact_incomplete`，不能选择另一版继续 |
| ready search Reservation 尚无 Action/epoch | 通用 unclaimed/no-Action reclaimer 调用数为 0；首个有效 worker创建唯一 open epoch。若 Window 已结束，recovery 创建并直接 finalize abandoned、释放全部 unit且求解调用数为 0 |
| search epoch open 时任务停止或 due 消失 | 不由通用 reclaimer 抢先释放；optimal 前置失效后由原 epoch abandoned 并一次释放仍未领取 unit。首次 finalize 后该来源 Reservation 满足 `bound+claimed+released=reserved` |
| 分片权重重建崩溃 | 非空 release set 提交时只把 Window 推进到新 epoch 的 `rebuild_required`；新 epoch 全部 allocation/reservation、相同 `dispatch_rebuild_snapshot_hash`、Window `ready_rebuild_snapshot_hash` 与 `ready` 原子提交。计算、CAS、数据库错误或 worker 崩溃均丢弃未提交权重并从最新快照重建，旧 released unit 不可领取，已 bound/claimed/active 和其他有效旧 Reservation 不回退 |
| 分片重建输入竞态 | 冻结快照后，不新增 release batch也不推进 `rebuild_input_version`，分别改变 due、eligibility、active exclusion、有效旧 Reservation 计数、Scope/Window/Shard active/unclaimed、任一 fairness cursor、parent/sponsor 聚合输入、容量、相关配置或 `dispatch_rebuild_contract_version`；提交前重新构造完整 `DispatchRebuildInput` 并取 hash，必须拒绝全部旧权重，下一 drain 按最新事实发布。只改变 worker/lease、扫描时间、墙钟时间、进程身份、随机值或不参与输出的诊断字段时 hash 不变；正常和零余额发布都要求 solver input hash、Window hash和每条新 allocation/reservation hash一致，且 solver 无额外 DB/global 读取 |
| rehash 后提交竞态 | 在 SERIALIZABLE precommit 已读取 eligibility/due/claim 谓词、尚未 commit 时并发更新已有输入或插入新候选；提交必须 serialization abort、写入 0 行且不由驱动拿旧权重自动重放。下一 drain 重新 assemble/solve；正常提交必须证明 rehash、全部新行、Window hash 与 ready 同一事务 |
| 搜索结果 precommit 漂移 | 冻结 search snapshot 后新增/删除候选，或改变冻结账号额度窗口内的剩余容量、已确认 click 数、持久机会时间、cursor、eligibility、中央份额/version；即使 Window epoch/input version 未变，旧 `optimal|no_candidate` 也必须写入 0 条 assignment并整轮 abandoned/release/rebuild。worker/lease、墙钟、扫描时间和纯诊断字段不得改变 problem hash；serialization/CAS/驱动旧结果重放均不能形成第二次 solver 调用或部分提交 |
| `optimal` finalize 后 assignment 在 Gateway 前失效 | 不重开或改写原 search epoch；以稳定 assignment/version trigger 原子 finalize 唯一 `DispatchAllocationReleaseBatch`，assignment 转 released、Reservation bound 减 1/released 加 1、各层 unclaimed 减 1并写永久 exclusion |
| release batch item 守恒 | 每个 candidate 恰有一条不可变 item；`candidate_unit_count = release_unit_count + already_released_unit_count + precondition_lost_unit_count`，candidate/release hash 与 `applied|no_op|mixed` 均可从 item 唯一重算。空 candidate 和全 no-op 都 finalize no-op 且不推动 rebuild |
| finalized release batch 幂等重放 | 用 carrier、candidate hash、逐 item expected/observed version 与分类/首 carrier、release hash、三类 count、outcome 及实际 wave 版本重算同一 `outcome_hash`；完全一致时零写回读，任一 item/result/wave 错绑保持 `release_fact_incomplete`；同 trigger 候选 hash 不同仍为 `release_batch_input_conflict` |
| 同一 assignment 被多个 release trigger 命中 | expiry、Action 不再到期与 Window expiry 并发：第一个有效 trigger 原子释放；后到 trigger 锁内识别永久 exclusion并以 no-op finalize，回读首个 carrier，不重复 exclusion/计数、不进入无限冲突重试 |
| release 与 claim 并发 | release 先赢则 assignment released、claim CAS 失败且不得执行；claim 先赢则 release batch 将该 unit 记 `precondition_lost` 并 no-op，绝不能释放 claimed/Gateway-started unit |
| release 后迟到 Action worker | release 事务同时以 expected Action version 把 bound Action 终结并清 lease/active；迟到 claim/Gateway CAS 必须失败。不得出现 assignment/exclusion 已释放但 Action 仍 pending/claiming，item 可回放 observed Action 状态与版本 |
| search 扩展锁序 | commit、`_confirm_claim`、Gateway 最终守卫、release 与 Reconciler 全部按中央前缀 → carrier（如有）→ assignment → 搜索 consumptive 子预留 → Action；并发回归无反向锁序或 deadlock，缺失层只能跳过不能换序 |
| release 一致性矛盾 | 注入只有 carrier、只有 exclusion、carrier/item/exclusion hash 或版本错绑、bound assignment + 完整 release fact set、released assignment 无任何 release 组件与计数漂移；release 事务无半写。半套/错绑事实保持 `release_fact_incomplete` quarantine且不得自动判 released；完整合法集合且无 claim/Gateway 时才把 assignment/Action 与各层摘要对齐为逐 unit 单次 released。原 trigger 不定时忙重试，其他义务继续，且不重跑搜索求解 |
| release 与 Gateway 事实同时存在 | 只有完整合法 release fact set 与 claim/Gateway 并存才写 `release_claim_fact_conflict` 并保持该 unit active quarantine；半套 release 组件仍归 `release_fact_incomplete`。自动 reconciler 不得删除 release 组件、回滚 Gateway、选边或调整该 unit 的 released/claimed 计数，不得写 resolved 或忙重试。完整 click evidence 可按真实事实入账，但相关 ledger 在 quarantine 清除前不得通过 E4 |
| rebuild 尚未 ready 又发生第二批释放 | 第二批加入同一 pending epoch，只递增 `rebuild_input_version`；旧权重 CAS 失败并整批丢弃，新求解读取两批释放。两个 batch 均可幂等回读，中央 epoch 只递增一次 |
| optimal 同时产生 matched 与 unmatched | unmatched 触发 `rebuild_required` 后，matched 的旧 epoch bound Action 仍可在 Window/deadline/版本有效时完成 `_confirm_claim -> Gateway`；不得因 Window 不再 ready 卡死或错误释放，也不得读取未发布新权重 |
| 旧 search epoch finalize 时 Window 已在更高 epoch 回到 ready | 仍必须因 `Window.dispatch_allocation_epoch != SearchClickAssignmentEpoch.dispatch_allocation_epoch` 改为 `abandoned`，不得提交旧 matched；其未领取 unit 从当前 ready 版本开启下一 rebuild wave |
| Window 已结束仍有未领取 assignment | 稳定 window-expiry batch 原子收口 assignment/exclusion/计数，但不为已结束 Window 新建 epoch或运行无用途权重重建；下一 Window 从真实欠额重新分配 |
| finalized carrier、release batch item 或 expired exclusion 清理 | 在来源 Window/Reservation 或迟到 writer 仍可访问时拒绝单独删除；联合归档必须先 fence 迟到 worker，且永久保留 item 的 candidate unit、assignment/Action expected+observed version、逐 unit 分类/首 carrier 引用，不能因清理后事实或唯一键消失而重复求解、释放或扣减 |
| exclusion snapshot 适用性 | 对 `no_feasible_search_path|search_solver_abandoned`，只创建新 dispatch/search epoch、换 Reservation/ordinal/worker 或改变 carrier-specific 份额时，`solver_problem_component_hash` 必须不变且原 active exclusion 不得 supersede；只有该 unit 连通分量的稳定业务义务、候选、资源、fairness 或 contract version 变化才允许 supersede。其他 reason 同样只看直接依赖的额度、授权、代理、协议/CAPTCHA、Gateway 或 assignment/Action version；无关 Task/shard/扫描时间不得触发。无论状态如何，旧 Reservation/ordinal 永远保持 released |
| 候选图无法一次完成求解 | 不以 solver deadline、性能预算、图规模或 p99 阈值建立重试/降级合同；当前 epoch 直接 `abandoned` 并释放全部未领取 unit。尚可领取 Window 的非空集合只开启或加入唯一 pending rebuild wave，已结束 Window 只收口事实，禁止提交部分解 |
| 两个 shard、多个 Dispatcher 同时 claim | 同一 scope 的全局 Window 及各 shard Allocation 都不超配；claimed_count 不超过 reserved_claims，aggregate capacity 不被多进程重复预留 |
| 旧 Window Reservation 对应 Action 已终态、暂停、删除或延后 | 非搜索 Reservation 才由通用回收器按“运行中任务 + 到期 pending Action”判断并写 `unclaimed_action_no_longer_due`；search Reservation 在首次 outcome 前必须跳过，首次 finalize 后只有 bound assignment 可由稳定 trigger 的 release batch 使用该原因释放。新到期任务只获得已经按各自合法 carrier 释放并重建后的份额 |
| 同时存在纯搜索点击与 AI 群日到期发送 | 二者均有持久 Window / Reservation；无长期 pending 饥饿；容量不足时双方收到 shared_dispatch_capacity_insufficient |
| 搜索、AI、评论、点赞、浏览同时到期 | 多个 Window 后每个仍有 due Action 的任务均获得持久最低轮转机会；任务多于容量时 cursor 从上次未服务位置继续 |
| 热搜排行榜页重复投递或恢复 | PRD §2.19 已禁止热搜页重置（线上验证不可行）；`hot_list_page` 直接写 `jisou_session_state_deviated` 并按已批准协议写带原因/到期时间的 24h eligibility 排除，零未知 button 点击；该排除不缩 click 欠额，其他合法账号/路径继续，且不得直接当作 `DispatchAllocationExclusion` |
| 正确分类页无 selector | 才写 jisou_group_selector_missing；未知文本不以原文持久化 |
| 图片算式验证页 | 冻结 challenge fingerprint 并写 required，不误报 selector 缺失。单供应商候选不合格继续下一健康已审批供应商；供应商/传输暂不可用保持 required。每 fingerprint 最多一次 Telegram 提交，只有明确远端通过回执或已审批搜索分类/结果页才 solved；仅离开原页、新 fingerprint、hot-list、unknown 均不得 solved。AI 调用/批准重试的 click 与业务 AI 限额增量为 0，同一账号—协议会话不得被并发 Action 改写 |
| 相同候选集重复规划/worker 重启 | 在 click/受服务任务/任务公平最优值不下降后，严格按 `hard_safe_remaining_capacity DESC -> confirmed_click_count_today ASC -> last_click_opportunity_at ASC -> persistent_account_cursor ASC` 得到同一可解释顺序；运营无排序字段 |
| 目标点击后 | ordinal 在 click 事实确认后结束，不创建 membership/admission/can-send 后续 Action |
| Gateway 开始后异常 | 同一 ExecutionAttempt 收口为 unknown_after_send，未知状态不计成功 |
| projection 不预占 | Planner/详情重复计算未来容量不新增 assignment、Action、claim 或 quota hold；结果标记 `projection_not_reserved=true`，且不能进入 held/committed 计数 |
| commit 服从中央份额 | 先完成全任务 TaskAllocation、search fulfillment lane 和 shard Reservation，后运行 `SearchClickAssignmentSolver`；每 Task assignment 数不超过份额，Dispatcher/Gateway inflight 只有一份中央 Reservation，四 worker 并发不能重复预留；未领取 assignment 随 Window 过期，已 `_confirm_claim` 的 assignment 原子转 `claimed` 并跨 Window 保留到 Attempt 收口 |
| 搜索份额无法绑定 | 不在原 assignment epoch 重试；对每个未领取 `(reservation_id, fulfillment_lane_claim_ordinal)` 原子写 exclusion、增加 released_count、扣减原 unclaimed。Window ready 时非空集合开启一个新 epoch/rebuild wave；已 rebuild_required 时加入同一 wave而不再递增 epoch；Window 已结束时只收口事实。claimed/active/cursor 不回退，click 欠额不减少 |
| 排除集合生命周期 | 一个 exclusion 永久唯一绑定一个旧 Reservation/ordinal，跨 `active|superseded|expired` 都不能重复释放；同 snapshot 下 active unit 数扣减原 task/lane/shard 可再次获配量，其他 shard 仍可获配。资源快照变化转 superseded、Window 结束转 expired；旧 unit 仍保持 released，新事实只能用新 Reservation/ordinal，不跨任务日/目标复用 |
| 图片算式验证码 | `required` 只进入识别流程且不触发 24h 排除；AI 调用/批准重试不占 click 限额且无业务固定 AI 轮数/递归次数；供应商/传输暂不可用保持 required。同 fingerprint 的单次批准提交只有取得明确远端通过回执或已审批搜索分类/结果页才 `solved` 并继续，离开原页/新 fingerprint/hot-list/unknown 均不算；只有识别链确实无安全答案或同 fingerprint 被远端明确拒绝才 `failed` 并排除账号—协议路径；任何概率成功率都不进入容量或完成计算 |

## 10. 发布门与生产验收

1. 先完成直接创建/启动后运行评估、完成优先 pacing、Reservation 并发、极搜页面相位、ExecutionAttempt 和前端投影的自动化回归。
2. 用真实协议样本和小账号池 canary 验证 group_result_page、hot_list_page、selector 缺失和验证页面路径；未通过样本不得扩大任务。
3. 发布必须走 master -> release -> GitHub Actions Deploy Production；两类 solver contract version 变化时禁止新旧 Dispatcher 混跑，先停止旧版本取得新 ownership，并以真实进程/数据库事务清单证明旧版本已失去写资格后才启动新版本。旧内存结果不得跨版本恢复。
4. 发布后先验证至少一个可执行小时内 Reservation 的分配、claim、Gateway 和事实回写，再按每个 canary 任务冻结的 `timezone_snapshot` 观察完整自然日。
5. canary 必须证明 `DispatchLaneShardSolver` 与 `SearchClickAssignmentSolver` 不互相越权、一个 search assignment epoch 只执行一次；`no_candidate|abandoned|unmatched` 及 post-finalize assignment 失效都按 Reservation unit 精确释放。Window ready 的首批非空释放只开启一个 pending rebuild wave，wave 内后续 release batch 只更新 `rebuild_input_version`，已结束 Window 不做空重建；重复 finalize 不会二次释放，active exclusion 能按资源变化 superseded、按 Window 结束 expired，finalized carrier/release batch item/expired exclusion 不被提前清理。
6. 只有 deadline 前 `confirmed_click_count = daily_click_target_snapshot`，逐 ordinal 均有唯一完整 click evidence，且 `held_count=unknown_count=terminal_shortfall=quantity_overflow_count=open_excess_count=0`、不存在影响该 ledger/ordinal 的 active `consistency_quarantine`，才可写搜索履约通过。目标达成后未进 Gateway 的 excess source 必须终结；late click 只作事实收口，不补写旧 ledger 通过。否则按 production_blocked 或 production_unproven 报告。

## 11. Product Design Complete 自检

| 检查项 | 结论 |
| --- | --- |
| 原始问题 | 不可达容量、共享额度笛卡尔积高估、行为跳过、claim 跨分片超配、极搜状态混淆和点击观测缺口均已覆盖；搜索点击加入明确不在本轮范围 |
| 前端状态 | 直接创建、201/200/403/404/409/422 结果、启动结果与 runtime state 分离、运行期真实匹配容量/catch-up、单一 click 目标、系统账号选择、Reservation、协议相位和账号 blocker 已定义 |
| 后端与 Worker | 创建 fingerprint/start operation、Task -> operation 并发锁、启动建 ledger、稳定 click ordinal、无写入 projection、中央 TaskAllocation/Reservation 后的 commit、`DispatchLaneShardSolver`/`SearchClickAssignmentSolver`、搜索 Reservation 从 ready 到首次 outcome finalize 的独占归属、唯一持久 `SearchClickAssignmentEpoch` outcome、post-finalize `DispatchAllocationReleaseBatch` 与逐 candidate item 分类守恒、逐 epoch 不可改绑的 allocation/reservation、单 pending rebuild wave、完整 immutable `DispatchRebuildInput`、三类 allocation/Window 同一 rebuild hash、SERIALIZABLE 原子发布、contract version 禁止新旧 Dispatcher 混跑、unit-level 永久 `DispatchAllocationExclusion`/identity tombstone、独立 quarantine writer 与 release set 整批一次 finalize、跨 Task ordinal/资源/task-fairness 连通分量多阶段字典序求解、`SearchClickOpportunityAssignment`、完成优先 Planner、完整 click 证据、Dispatcher、Gateway 与 Attempt 合同已定义 |
| 数据流 | 从直接创建、启动建账、ordinal 欠额、只读 projection、全任务公平份额内 commit 共享资源 assignment 到完成优先 click 和远端事实链路已定义 |
| 权限与安全 | 保留所有账号、代理、协议和 Telegram 门禁；禁止未知按钮回退；纯点击协议必须证明无 membership 副作用并禁止成员关系变更 RPC |
| 边界与并发 | scope/shard Window、全任务最低轮转、四 worker 份额、共享账号/关键词/授权/代理/Gateway 容量不重复投影、assignment CAS、solver no-candidate/optimal/abandoned、post-finalize bound release、逐 unit batch item、并发 release 合并同一 rebuild wave、完整 input 漂移与 rehash-to-commit update/phantom、contract version 切换、已结束 Window 不空重建、carrier/item/exclusion 保留、两类 epoch 不混用、exclusion 精确单位/替换/过期、最大 click -> 最大受服务任务 -> 最大最小任务公平的逐阶段最优证明、启动同键/异键并发和状态覆盖、不可变 task-day ledger、legacy 混合任务隔离、日切、部分天、无缝时区过渡、静默、截止、未知结果与“禁止新 reset、历史字段只读”已覆盖 |
| QA 与发布 | 回归、canary、完整自然日 E4 证据已定义 |
| design_status | complete |

### 11.1 当前 release 实现映射

- 当前 release 的 `search_join_daily_capacity.py` 仍会把行为 skip 扣出严格容量，创建/编辑仍存在容量预检语义；本次合同要求 `resync`：容量只在启动后运行评估，soft pacing 不再减少目标，catch-up 在硬安全上限内追赶。
- `executors/search_join_group.py` 已有剩余目标追赶入口，但当前 `planning_slot_key/strict_capacity_action_key` 仍是执行容量槽，不是稳定 click 义务。需迁移为 ledger `click_obligation_ordinal`，新增每个中央版本唯一的 `SearchClickAssignmentEpoch`、跨 Task `SearchClickOpportunityAssignment`/资源 reservation，并由 `SearchClickAssignmentSolver` 精确匹配；首次 outcome release 与 post-finalize `DispatchAllocationReleaseBatch` 均需整批原子提交。尚可领取 Window 的首批非空释放只开启一个 pending rebuild wave，后续 batch 只推进 `rebuild_input_version`，空集合和已结束 Window 不推动中央版本。纯搜索点击必须在 click 事实后结束，禁止创建 admission child。
- `dispatch_claim_*.py` 以 `DispatchClaimScope` 把跨 Window 的 active claim 纳入容量账本；每次 Allocation 前从真实 executing Action 回写 Window/shard active 计数。当前通用“无到期 pending Action 即回收”入口必须 `resync`：非搜索 Reservation 可写 `unclaimed_action_no_longer_due`，search Reservation 在首次 outcome 前由搜索物化独占、通用回收必须跳过，首次 finalize 后只能由 assignment 稳定 trigger 的 `DispatchAllocationReleaseBatch` 释放。详情同时显示当前 Window、全局 scope、来源 carrier 与逐 unit 分类。
- 迁移 `0122_dispatch_claims_protocol_trace.py` 创建 scope/window/shard/reservation 与极搜协议 trace；历史 reset 字段只读保留，新 Action 固定 not_applicable/false。

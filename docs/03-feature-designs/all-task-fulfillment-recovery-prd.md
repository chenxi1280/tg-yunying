# 全任务按时按量履约恢复 PRD

## 1. 文档状态

| 项目 | 内容 |
| --- | --- |
| Intake ID | `intake-2026-07-28-all-task-fulfillment-recovery-001` |
| 需求级别 | L3：生产任务长期无法按时按量完成 |
| 适用任务 | `group_ai_chat`、`channel_comment`、`channel_like`、`channel_view`、纯搜索点击 `task_type=search_click + search_execution_mode=click_only` |
| 设计状态 | `complete` |
| 设计复核 | 2026-07-29 已补齐生产运行中任务自动接管、旧混合搜索转纯点击、热搜失败 12 小时路径排除、任务级软上限统一归一化、Dispatcher 稳定 Window、AI 准入账号级并行及自然日 deadline 收口；发送门禁修复不改变 AI 活群/评论的引用、图片、表情与普通 emoji 内容占比 |
| 开发交接 | `dev_handoff_ready=true`；2026-07-29 最新完成优先合同 supersede “存量只审计不接管”和“小任务级上限可截断履约”，必须按本文完成代码、存量数据接管和生产 E4 |
| 生产状态 | `production_unproven`；容器健康、Action 创建或本地测试均不代表恢复 |
| 统计时区 | 默认 `Asia/Shanghai`，实际以 `Task.timezone` 为准 |
| 证据基线 | 2026-07-28 生产只读取证与当前 `release` 实现审查 |

本文是五类任务共同履约、调度、前端展示和生产验收的当前专项真相源。各任务原有安全、内容和协议专项继续有效，但冲突时按下列优先级解释：

1. AI 群日目标、全账号覆盖、账号面具和内容记忆以 `ai-group-daily-group-target-redesign-prd.md` 为准。
2. 搜索协议细节以 `search-click-daily-fulfillment-remediation-prd.md` 为准；本文修订其容量、重复执行和生产放量口径。
3. 本文 supersede 旧“评论与群聊共用签到兜底”、搜索点击混入 membership/admission，以及 AI 3 轮失败后沉默/失败的合同；“搜索点击加入”仅登记为后续独立模式，本轮不设计、不实现。
4. 本文 supersede 主 PRD 中频道评论 `max_total_comments` 单独触发 `completed`、reaction unavailable 关闭整帖、浏览任务级上限可以小于逐消息当日目标、频道任务自定义账号小时/日上限、重复搜索绕过账号/关键词日限额的旧表述；AI 活群/评论/点赞/浏览/纯搜索点击的通用小时软上限，以及评论/点赞/浏览/纯搜索点击的任务级与任务内账号级履约软上限，统一归一为 `1_000_000`，只作系统异常门禁，不作目标或完成条件。Telegram、账号全局、授权、代理、协议和内容安全硬边界不属于此软门禁。
5. `Task.status` 与当日/当消息履约状态分离；本文不新增一种 Task 主状态。
6. 本文 supersede 旧 AI 群管准入的“同群一次只允许一个新账号”窗口；互斥范围固定为 `target_group_id + account_id + admission_generation`，同群其他账号不得因某账号仍在观察/follow/confirm 而等待。

## 2. 当前生产事实与结论

### 2.1 取证快照

| 范围 | 生产事实 | 当前结论 |
| --- | --- | --- |
| Planner/Dispatcher | 发布后出现 1 次 Planner、3 次 Dispatcher PostgreSQL deadlock | 热事务交叉更新 `tasks`、覆盖账本和 claim 投影，吞吐不可持续 |
| Dispatcher claim | 4 个 Dispatcher 共用一个 scope；代码把 `ACTION_CLAIM_LIMIT=100` 与单 worker `DISPATCHER_CONCURRENCY=20` 取最小，scope 只有 20，且一个 60 秒 Window 内因 Action 状态变化反复重建 12-18 个 epoch，仍有 9-17 个份额空置 | 查询批量、单 worker 执行并发和全局 scope 容量语义混淆；运行态需求变化错误触发全 Window 重建 |
| AI 活群 | 3 个 running、5 个 stopped；运行任务部署新代码后仍没有当前 `TaskDayLedger/TaskGroupDailyMessageSlot`；共享群管准入把同群任一 unresolved/stale 账号扩成全群 busy | 新合同只在显式 start 建账，存量 running 被遗漏；准入必须只阻塞对应账号，其他账号并行继续 |
| 评论 | 单消息目标 80；任务 lifetime cap 解析为 65/80，旧任务在 79/86 附近完成 | 任务级生命周期 cap 错误截断逐消息目标 |
| 点赞 | 950 条 open，仅 2 条当天到期；`moderate_6h` 被 24 小时曲线扩展；58 次 reaction unavailable 被计入已用账号 | deadline 失效，失败事实占用成功目标 |
| 浏览 | 两条消息当日目标合计 2,000，任务日安全上限 500；快照为 78 success + 424 open | 配置自相矛盾，任何调度算法都无法在上限内完成 |
| 搜索点击 | 生产只有一条运行中 `legacy_mixed_search_join`，新 `search_click` 账本/义务均为 0；发布后 0 click success，hot-list、验证码和 session deviation 持续失败 | 必须把存量当前执行合同切为纯点击并建立 ledger/ordinal；Action 规模不等于目标事实 |
| 搜索容量 | 63 个候选账号，账号日限额 2；57 个账号已超过 2，平均 10.49、最大 20 | repeat 模式绕过安全限额；63×2 只能证明原账号池最多提供 126 次原始合法尝试，不能称为 126 次可确认点击；CAPTCHA 只有实际 solved 后才可继续，目标点击仍只认远端事实 |

### 2.2 方案可解决边界

本方案可以直接解决：

- Planner/Dispatcher deadlock、claim 容量语义和工作类别互阻；
- Action/Attempt/远端事实与履约账本不一致；
- 评论 lifetime cap 提前完成、点赞失败占成功额度、浏览自相矛盾配置；
- 6 小时模板被 24 小时曲线覆盖；
- 搜索 repeat 模式绕过限额、协议未验证即批量进入真实 Gateway 执行和假容量展示。

本方案不能通过代码凭空解决：

- 已停止任务是否应重新启动；必须由运营人员显式决定。
- 搜索 1000/日所需的真实安全账号容量。按 2 次/账号/日计算，即使每次原始合法尝试都最终确认点击，也至少需要 500 个合格账号；当前 63 个账号相差 437 个。该算式只证明尝试容量不足，不使用 CAPTCHA/协议/目标命中概率预测确认量；验证码只有本次实际 solved 后才允许继续。
- Telegram 权限、管理员审批、SlowMode、FloodWait、目标不支持评论/reaction 或 CAPTCHA 识别失败。

这些外部条件必须在运行态按影响范围显示为 `at_risk|waiting_*|blocked`，不能阻止合法任务创建，也不能通过放宽安全限额、伪造成功或无限建单掩盖。

## 3. 产品目标与非目标

### 3.1 产品目标

1. 五类任务全部使用统一、可下钻的新履约模型；部署后自动接管运行中任务，不要求人工 stop/start。
2. 任何完成结论都可追到同一任务粒度的 Action、ExecutionAttempt 和 Telegram 远端事实。
3. Planner 只为 deadline 前可执行且未被有效 Action 占位的欠额形成有限执行义务；评论/点赞/浏览可直接建 Action，搜索只冻结 ordinal 与只读候选，必须等当前 Claim Window commit 取得中央份额后才建可执行 Action。
4. 创建接口只执行请求结构校验：必填字段、调用者授权、同用户可见引用、引用类型与任务声明用途的静态兼容性，以及数量/内容合同能否形成唯一义务；这一步不得命名或实现为“任务预检”。五类任务的授权矩阵固定为：AI 活群、评论、点赞、浏览只要求 `tasks.manage`；纯搜索点击必须同时具备 `tasks.manage + tasks.create.search_click`，并持久化 `task_type=search_click`。本次不新增其余四类任务的专项创建权限。调用者缺少任一必需权限直接返回 403，不持久化 Task；Telegram 运行权限、账号在线、准入、代理、Provider、协议和容量一律不在创建前读取。不执行容量预检、不要求风险确认；外部资源不足不阻止创建或启动，运行后由 Planner 持续显示真实 blocker、自动排序并追赶目标。
5. 任务生命周期、当日履约和逐消息履约分离，任何 task-level cap 都不能替代 per-account/per-message 目标。
6. 存量接管先生成逐 task 审计预览，再在同一发布流程幂等 apply：运行中任务立即接管并续跑；paused/stopped 只升级合同、不自动启动；completed/deleted 历史不改。接管重复执行不得重复 ledger、slot、obligation 或 Action。

### 3.2 非目标

- 不降低业务配置目标；任务内的账号日/小时/账号关键词日次数等履约软上限统一归一为 `1_000_000`，旧搜索 skip 概率和任务内账号冷却归零。账号全局安全、Telegram 风险、授权、代理、协议和 unknown 防重硬门禁保持不变，不能借统一软门禁绕过。
- 不把 `pending/claiming/executing/unknown_after_send/skipped` 计为成功。
- 不使用 mock、未记录的静默 fallback、未知按钮模糊点击或未审批协议样本；本文明确的签到/表情确定性兜底必须留下原失败原因和远端事实。
- 不把 worker/container/health 绿色、claim 成功、Action 创建或本地测试写成 `production_fixed`。
- 不修改已经进入 Gateway 的 Action 历史结果。
- 不修改 AI 活群、频道评论既有内容编排：原生引用回复数量、direct/reply 拆分、普通文本 emoji 习惯/占比、图片/表情包/custom emoji 等素材比例、素材意图映射与素材规则不因本次门禁修复被重算、降级或关闭。

### 3.3 调度、求解与准入统一术语

下列名称是主 PRD、专项 PRD、架构、数据流、API、日志和 QA 的唯一业务口径；禁止继续用不带限定词的“精确求解器”“allocation epoch”“排除集合”或“admission”让实现自行猜测：

| 术语 | 唯一含义 | 禁止混用 |
| --- | --- | --- |
| `DispatchLaneShardSolver` | 中央 Dispatcher 在一个 Claim Window 内，把父业务任务已经获得的 fulfillment/admission lane 份额精确映射到有候选且有容量的 shard；目标是在不超过 task、lane、shard 上限的前提下避免可行容量被贪心闲置 | 不选择搜索账号/关键词路径，不确认远端成功 |
| `SearchClickAssignmentSolver` | 纯搜索点击在中央 fulfillment 份额上限内，把稳定 click ordinal 精确匹配到账号、关键词、授权槽位、代理、协议和 Gateway 路径；按“最大 click 数 → 最大受服务到期任务数 → 最大最小任务公平 → 稳定路径”逐阶段固定最优值 | 不重新分配中央份额，不建立 admission 目标，不得用首个可行贪心、固定 top-N 或超时部分解 |
| `dispatch_allocation_epoch` | `DispatchClaimWindow.allocation_epoch` 的业务/API 名称；TaskAllocation/ShardAllocation/Reservation 固化所属 epoch。Window 仍可领取且为 `ready` 时，首批非空 release set 只递增一次并开启 pending rebuild wave；wave 内后续释放复用该 epoch并只递增 `rebuild_input_version`，新权重整批发布后回到 `ready` | 不表示任务日、点击目标、账号额度或搜索候选快照；不得因未提交权重、搜索过程、空集合、wave 内后续 batch 或已结束 Window 递增 |
| `dispatch_rebuild_snapshot_hash` | pending epoch 分片重建的规范化输入身份；覆盖 due/eligibility、active exclusion、有效旧 Reservation、scope/shard 容量及影响分配的配置当前值/版本，提交前重算；成功后 Window 以 `ready_rebuild_snapshot_hash` 保存同值，三类新 allocation 行各保存 `dispatch_rebuild_snapshot_hash` | 不用 worker/lease、时间、进程或随机值凑版本；不能用 `rebuild_input_version` 单独替代，也不等于搜索 `solver_input_hash` 或 exclusion `resource_snapshot_hash` |
| `search_click_assignment_epoch` | 绑定 `(dispatch_claim_window_id, dispatch_allocation_epoch)` 的一次搜索候选与资源版本快照；一个 epoch 只求解和提交一次，首次 outcome 直接释放未领取 unit | 不在原中央份额上重试；不得承载该 epoch finalize 后的 assignment 释放，也不得继续命名为 `search_click_allocation_epoch` |
| `DispatchAllocationReleaseBatch` | search epoch 已 finalize 后，以稳定 trigger 处理一个或多个候选中央 unit 的持久载体；锁内分类 effective/already-released/precondition-lost，全部 effective unit、状态、exclusion、计数与 rebuild wave 原子提交，空 effective set 也以 no-op 收口 trigger | 不使用随机 batch/worker/扫描时间作幂等键，不改写原 search outcome，不把重叠 trigger 的永久唯一键冲突变成重试 |
| `DispatchAllocationExclusion` | 精确且跨状态永久绑定 `(dispatch_claim_reservation_id, fulfillment_lane_claim_ordinal)` 的一个已释放中央份额事实，只影响来源 Window 的重新获配 | 不是账号/任务永久黑名单，不跨 Window，不减少业务欠额，也不等于极搜 `jisou_selector_accounts` 的 12 小时协议安全 eligibility 排除 |
| `membership_admission` | 账号为目标群完成 membership probe、join、可信群管频道关注、精确确认/验证、membership 与 can-send 复检的业务前置链 | 不表示 API 权限、搜索 target match、一般 eligibility 或纯搜索点击；纯搜索点击固定 `admission_lane_claims=0` |

`AdmissionExecutionLease` 只为已经设计的 `membership_admission` 去重真实 join/follow/confirm 执行；本次五类任务的接口授权只使用 `tasks.manage` 与纯搜索点击专用的 `tasks.create.search_click`：AI 活群、评论、点赞、浏览不设置专项创建权限。`tasks.create.search_join_group` 只作存量兼容，不能授权任何新建任务；`targets.manage` 等其他业务权限保持其既有边界。搜索路径是否当前可执行继续使用 `eligibility`。未来“搜索点击加入”在独立 PRD 完成前不得引用 admission lane、lease 或状态机。

现有数据库/内部枚举中的 `lane=admission` 和 `admission_lane_claims` 是 `membership_admission` 执行份额的兼容物理名，不是第三种 admission 定义；新 API、日志和页面必须展示 `lane_business_kind=membership_admission`。它不得承载 API 授权、搜索 eligibility、纯 click 或尚未设计的“搜索点击加入”。

极搜 `jisou_selector_accounts` 的 12 小时排除是带 `reason_code/expires_at` 的账号—协议路径安全资格事实，在当前单用户 scope 的全部搜索任务间共享，只令该路径在有效期内 `eligibility=false`；它不减少 click 欠额、不停止其他账号/路径，也不能单独触发中央份额重分配。识别为 `hot_list_page` 时当前尝试直接失败并写 `jisou_hot_list_page`，同一账号—协议路径从失败事实时间起排除 12 小时；不执行 reset、不点击未知按钮、不在当前尝试继续搜索。`jisou_image_verification_required` 不进入该排除。图片算式验证码按 `bot_peer + message_id + image_hash + ordered_callback_fingerprint` 冻结 challenge；只有同一 fingerprint 的单次批准答案提交取得明确远端通过回执，或进入已审批搜索分类/结果页，才可写真实 `solved` 并继续。仅离开原页、超时、hot-list、unknown 或出现新 fingerprint 均不能冒充通过；新 fingerprint 重新进入 required。识别不设置业务固定 AI 轮数或递归次数，单个供应商候选不合格只继续下一健康已审批供应商，供应商/传输暂不可用保持 required；只有全部当前健康已审批供应商确实返回无安全答案，或同 fingerprint 的单次提交被远端明确拒绝，才写最终 `failed` 并排除。AI 识别调用和批准重试不占 click 限额，也不计入 AI 活群/评论的主 AI 三轮、备用 AI 三轮或业务 AI 生成次数；当前 Action 复用既有账号 session ownership，challenge 收口前不得由另一搜索 Action 并发改写同一账号—协议会话。搜索求解、候选或 CAS 无法完成时，才按未领取 Reservation unit 另写 `DispatchAllocationExclusion`；仅非空 release set 重建分片权重。

## 4. 统一履约合同

### 4.1 履约粒度

| 任务 | 目标粒度 | 目标数 | 成功事实 | deadline |
| --- | --- | --- | --- | --- |
| AI 活群 | task + target group + `task_day_ledger_id`；另有 account coverage | `effective_daily_target`；每冻结账号至少 1 | 无需可见性核验：successful `send_message` Attempt + 非空 `remote_message_id`；需要核验：再具备 `visible_confirmed`；coverage 绑定匹配 | ledger deadline |
| 评论 | task + channel message | 消息纳入任务时一次性解析并固化的目标 | successful `post_comment` Attempt + 非空远端评论 ID | `message_admitted_at + pacing_window` |
| 点赞 | task + channel message | 消息纳入任务时一次性解析并固化的目标 | successful `react_message` Attempt + 远端 reaction 确认 | `message_admitted_at + pacing_window` |
| 浏览 | task + channel message + `task_day_ledger_id`；另有累计目标 | 每消息每日目标和累计目标分别固化 | successful `view_message` Attempt + Telegram Gateway 确认 | ledger deadline |
| 搜索点击 | task + target + `task_day_ledger_id` | `daily_click_target_count` | `target_click_observed=true`；确认后 ordinal 结束，不产生 membership/admission 子义务 | ledger deadline 或更早的 `scheduled_end` |

数量 jitter 只能在目标消息首次进入任务账本时解析一次并持久化；后续 Planner 不得重复抖动目标。

### 4.2 统一读模型

任务详情和列表摘要必须返回：

```text
target_count
due_target_count
confirmed_count
late_confirmed_count
held_count
unknown_count
terminal_shortfall
failed_attempt_count
remaining_count
planning_deficit_count
quantity_overflow_count
open_excess_count
projected_capacity_before_deadline
deadline_at
quantity_status
content_mix_status
acceptance_status
blocking_codes
calculated_at
```

字段定义：

- `target_count`：当前业务粒度冻结的精确目标；除专项明确声明为下限外，不授权超发。
- `due_target_count`：按当前 pacing 截至 `calculated_at` 累计应完成数，范围为 `0..target_count`；无渐进节奏的到期目标可直接等于 `target_count`。
- `confirmed_count`：在业务 deadline 内完成、唯一可减少履约欠额的去重远端确认数。
- `late_confirmed_count`：远端事实时间晚于 deadline 的真实成功；只作事实收口，不减少原按时欠额。
- `held_count`：deadline 前仍有效的 pending/claiming/executing；只防止重复规划。
- `unknown_count`：已进入 Gateway 但结果未知；防重复，不计成功。
- `terminal_shortfall`：按业务逻辑槽位去重、当前仍未被合法重建/替代的终态缺口；同一槽位的历史失败 Attempt 另计 `failed_attempt_count`，不得反复累加成业务缺口。
- `failed_attempt_count`：历史明确失败/跳过 Attempt 诊断数，不参与目标、欠额或完成计算。
- `remaining_count = max(target_count - confirmed_count, 0)`。
- `planning_deficit_count = max(due_target_count - confirmed_count - held_count - unknown_count, 0)`；Planner 只对该值建单。
- `quantity_overflow_count = max(confirmed_count - target_count, 0)`。
- `open_excess_count = max(confirmed_count + held_count + unknown_count - target_count, 0)`；其中 pre-Gateway excess 必须在 Gateway 前终结，unknown 继续核验且禁止再补发。
- `projected_capacity_before_deadline`：基于当前真实 eligibility、硬安全上限和可执行时间的只读尝试容量上界，不使用协议成功率、验证码触发率、AI 历史成功率或其他概率折损，也不含未证假设。对搜索点击的专项字段固定为 `projected_eligible_attempt_capacity_before_deadline`；不得命名或解释为 projected confirmed clicks。

`primary_quantity_slot_id` 只用于 AI 群日发送。AI 的主槽由不可变 `TaskGroupDailyMessageSlot(slot_kind=coverage|extra_volume)` 持久化，coverage 槽与 `TaskAccountDailyCoverage` 一对一，全部槽位数精确等于群日目标。它解决的是“一条远端消息同时确认群日总量、一个账号 coverage 和内容子维度，但群日总量只能计 1”的问题；每条 AI 消息最多完成一个账号覆盖义务，每个 coverage 也最多绑定一条远端消息。

评论、点赞、浏览和 click 不新增通用 quantity slot 表，也不强制写 `primary_quantity_slot_id`。它们分别使用已冻结的消息、revision、账号、任务日和任务内稳定 ordinal 等天然业务义务键，并以唯一远端事实键幂等确认；重复 finalize 必须回读原结果，不能跨 Task/业务义务重复增加计数：

| 任务 | 业务义务键 | 唯一远端事实归属 |
| --- | --- | --- |
| 评论 | `(task_id, channel_message_id, comment_plan_revision, target_ordinal)` | 非空 `(telegram_discussion_peer_id, remote_comment_id)` 只能归属一个评论 ordinal |
| 点赞 | `(task_id, channel_message_id, account_id, reaction_contract_version)` | 同一账号对同一远端消息的已确认 reaction state 只能完成一个当前义务 |
| 浏览 | 日目标使用 `(task_day_ledger_id, channel_message_id, view_source_key)`；累计目标从相同 distinct view facts 聚合 | 同一 view source 对同一远端消息的 Gateway confirmed fact 在同一 ledger 只计一次；同一事实可同时进入该消息累计聚合，但不能重复插入 |
| click | `(task_day_ledger_id, target_id, click_obligation_ordinal)` | 同一 ExecutionAttempt 必须同时具备 `gateway_call_started_at`、`target_found_at`、`target_identity_snapshot`、`approved_button_fingerprint_hash`、`click_invoked_at`、`approved_protocol_outcome`、`remote_confirmed_at` 与 `click_evidence_hash`，才能把该 ordinal 的 `target_click_observed` 确认为 true；`source_action_id/execution_attempt_id` 只保留执行来源 |

上述天然键必须在 Action 进入 claim 前持久化，不能等远端成功后临时拼接。每个键允许保留历史 pre-Gateway terminal Action/Attempt，但数据库 partial unique 必须保证 `status in (pending, claiming, executing, unknown_after_send, success)` 时同时最多一条当前 Action；明确失败后的 replacement 必须先使旧 Action 离开该 current set，再复用原业务键并递增 attempt，不得生成新的业务义务：

1. 评论首次纳入消息时原子冻结全部 `target_ordinal`；`comment_plan_revision + target_ordinal` 是 owner，Action 保存 `comment_action_attempt_no`。
2. 点赞的 `reaction_contract_version` 是消息首次纳入任务时冻结的不可变 reaction 模式、specific reaction 或 allowed set 版本。配置编辑只影响之后新纳入的消息；同一 `task + message + account` 同时最多一个 active version，旧版只读且不能与新版重复计数。
3. 浏览的 `view_source_key` 固定为 `account:{account_id}`；Session、授权槽位、代理或 client metadata 变化不产生新的 view source。数据库唯一键为 `(task_day_ledger_id, channel_message_id, account_id)`。
4. click ledger 按 `1..daily_click_target_snapshot` 冻结 `click_obligation_ordinal`；可按欠额惰性物化，但必须在 ledger 行锁内选择最小未占用 ordinal。replacement Action 和其全部 ExecutionAttempt 继续使用同一 ordinal；`source_action_id` 仅作远端事实 provenance，不能成为或改变业务义务身份。

不使用通用 quantity slot 不等于允许复用同一远端副作用。各任务类型必须在 confirmed 短事务内同时取得自己的远端事实所有权：

- 评论：`(telegram_discussion_peer_id, remote_comment_id)` 全局唯一绑定一个评论 ordinal。
- 点赞：`(target_peer_id, channel_message_id, account_id)` 同时最多一个开放或已确认的 reaction 所有权；已有未改变的远端 reaction 不能被后创建 Task 重新计数。只有显式新合同要求改变 reaction 且 Gateway 返回新的 `reaction_state_revision/reaction_evidence_hash` 时，才形成新事实，旧事实保持只读。
- 浏览：`(target_peer_id, channel_message_id, account_id)` 是 Telegram 生命周期唯一 view source fact；它只能绑定一个任务义务。后创建或重叠 Task 只能选择其他账号，不能因 Session/代理变化再次计 view。
- click：非空 `click_evidence_hash` 与同一 `ExecutionAttempt` 唯一绑定一个 click ordinal；同一 evidence 不得被另一 Task/ordinal 复用。新的 click 必须是新的合法 Attempt 和新的完整协议证据。

这些是类型专用唯一键/事实表，不新增 `primary_quantity_slot_id` 或通用数量槽。远端事实发生时间早于 Task/ledger 义务起点时只能作为历史状态，不得倒灌为本义务成功；所有权冲突进入受影响对象的 `consistency_quarantine|remote_fact_owned_elsewhere`，其他独立义务继续。

发布接管必须把未结束评论/点赞/浏览 Task 的存量 `success` Action 回填为上述义务与唯一远端事实，把仍在 Gateway 前且有稳定天然义务键的 Action 绑定到当前义务并补齐 payload 中的 obligation/ledger ID。重复 lifetime source 只保留首个事实所有权，后续重复不计第二个完成量；新 Planner 完成数只读取远端事实，pending/current 义务只占规划额度。接管脚本提供 preview/apply，逐 Task 事务执行；部署先停止全部 Planner/Dispatcher/Listener/Recovery 等 worker，在仅 backend 可写且任何旧 Action 都不能进入 Gateway 的窗口中完成 preview 与 apply，再恢复 worker。结构非法只暂停对应 Task、写入 `task_contract_invalid` blocker 并继续接管其他 Task；非预期脚本/数据库失败才中止发布且不得恢复 worker。运行期 Dispatcher 不得临时改写整个 Task 合同；新建 Task 直接带新合同，显式启动时再幂等接管单 Task。

点赞和浏览义务把 `account_id` 作为远端副作用身份的一部分；payload 已带 `reaction_fulfillment_obligation_id|view_fulfillment_obligation_id` 的 Action 在 claim 时禁止改派账号。历史 Action 若已被错误改派，payload 当前绑定与原义务不一致时必须先原子释放原义务，再允许其用原账号重建；已成功但尚待远端事实 finalize、且 payload 仍绑定同一义务的 Action 继续占位，不能被当成终态失败重建。不得让一个 Action 同时占住两个账号义务。

评论/点赞不是自然日任务时不得虚构 `task_day_ledger_id`；浏览与 click 的任务日身份则必须保留。Planner 在锁定对应业务账本后按 `planning_deficit_count` 原子取得有限义务：评论/点赞/浏览创建 Action，click 只冻结稳定 ordinal，当前 Claim Window commit 才在中央份额内创建 assignment/Action。目标满足后终结 pre-Gateway excess。membership、`GroupBotAdmission.ready` 等可共享前置事实可以被多任务复检，但不能替代各任务自己的发送/click/admission 完成义务。

按时归属必须使用 `remote_confirmed_at`：优先 Telegram/协议返回的远端事件时间，其次是同一 ExecutionAttempt 在 Gateway 成功回执时原子记录的确认时间；普通 `Action.updated_at`、reconcile 执行时间或页面读取时间不得替代。无法证明事实发生在 deadline 内时进入 `confirmation_time_unproven`/unknown，不能猜测计入 `confirmed_count`。

#### 4.2.1 日账本身份与时区切换

任何自然日任务都先建立不可变 `task_day_ledger_id`，并冻结：

```text
timezone_snapshot / timezone_revision
obligation_local_date
period_start_at / deadline_at   # UTC aware, [start, end)
day_phase = partial_start | timezone_transition | full_day_committed
```

账号/消息/click 等日义务必须外键到 `task_day_ledger_id`；`local_date` 只用于展示和查询，不能单独作为跨时区唯一身份。任务时区修改时：

1. 当前 ledger、Action、Attempt 和 child 继续使用旧 `timezone_snapshot/deadline_at`；
2. 保存 `pending_timezone`，`timezone_effective_at` 固定为当前 ledger 的 `deadline_at`；该时刻前禁止在新时区冻结第二份 ledger；
3. 在 effective_at 起建立新时区 ledger。若该时刻不是新时区 00:00，则建立 `[effective_at, 下一新时区午夜)` 的 `timezone_transition` 过渡日，`planning_anchor_at=effective_at`；随后才进入完整日；
4. 任务连续处于 running 时，相邻 ledger 的 UTC 区间必须首尾相接且不重叠：`next.period_start_at = previous.deadline_at`。worker 重启或重复 apply 不得重置 effective_at；
5. 过渡日按首日剩余权重尽力完成，但不纳入完整任务本地日 SLA。历史事实只按其 `task_day_ledger_id` 归属，禁止用当前时区重新解释；
6. 尚无 ledger 的 draft 任务修改时区立即生效。pending 期间再次修改时区必须使用 `Task.config_revision` CAS：保留原 `timezone_effective_at`，只替换待生效时区并写前后审计；改回当前时区可显式取消 pending；
7. 时区必须是可解析 IANA 标识。DST 跳时日以两个本地午夜对应的真实 UTC 区间为准；不存在的小时不产生时长，重复小时分别按同一小时权重累计。`full_day_committed` 表示完整任务本地日，不能硬编码为 24 个真实小时。

task-day ledger 只决定业务目标归属，不能重置账号/关键词安全额度、Telegram FloodWait/SlowMode、授权锁、代理冷却、unknown hold 或内容冷却。上述安全状态继续使用各自既有滚动窗口或策略时区；时区切换过渡 ledger 必须扣除同一安全窗口已消费量，禁止通过改 Task 时区获得新额度。

暂停/停止边界固定：

- 在当前 ledger deadline 前暂停后又恢复，继续同一 ledger、目标、anchor 和 pending timezone；暂停时间不从 deadline 扣除，也不能用恢复动作重置欠额。
- 暂停、停止或删除不会改写当前 ledger 的 period 和历史事实；生命周期变化追加 `TaskDayLedgerLifecycleEvent(event_type, occurred_at, task_revision)`，并终结未进 Gateway Action。若原 deadline 到达仍未完成，保留 `missed + task_paused|task_stopped` 事实。
- 非 running 期间不建立每日 ledger。跨过一个或多个 deadline 后恢复时，以恢复时已生效的时区从 `resume_at` 建立 `partial_start` ledger；停机区间是有审计的非运行 gap，不伪造成连续履约，也不补建空 ledger。
- pending timezone 的 effective_at 在非 running 期间仍按审计时刻更新当前任务时区，但不创建 transition ledger；恢复时按上一条建立 partial-start ledger。

### 4.3 履约状态

| 状态 | 判定 |
| --- | --- |
| `met` | `confirmed_count = target_count`，逐消息/逐账号子目标全部达标，且 `held_count=unknown_count=terminal_shortfall=quantity_overflow_count=open_excess_count=0`，不存在影响该 ledger/义务的 active `consistency_quarantine` |
| `at_risk` | 尚未截止，仍可能完成，但低于 `due_target_count`、保守容量尚未证明足够，或仍有会改变最终结论的 unknown/open excess |
| `blocked` | 尚未截止，但非 AI 任务的专项安全合同已证硬上界使 `confirmed_count + projected_capacity_before_deadline < target_count`，或已发生不可逆 quantity overflow/配置/权限/协议 blocker；AI 活群的日容量预测不足只能是 `at_risk/completion_risk`，不得单独置为 blocked |
| `missed` | 已过 deadline 且未 `met` |

`Task.status=running` 可以同时有 `acceptance_status=blocked/missed`。任务不得因为达到 task-level 计划上限而自动写 `completed`。

`missed` 不会释放 `unknown_after_send`、不会授权替代重发，也不终止远端 reconciliation。后续若以可审计远端时间证明该 unknown 实际在原 deadline 前成功，且最终数量恰好等于目标、无其他 open/unknown 并满足子目标，才可重算为 `met`；造成超量时保留 overflow，deadline 后成功只单列 late。任何状态修正都必须保留前后快照和证据，不得仅凭本地完成时间回填。

当 `confirmed_count` 达到目标时，所有尚未进入 Gateway 的 excess Action 必须以稳定业务槽位顺序终结为 `target_already_satisfied` 并释放 Reservation；Gateway-started/unknown 不能取消，只能保持 hold 直至核验。若其后确认造成超发，保留真实成功并将数量状态置为不可逆违规，禁止删除事实或用次日少发抵消。

### 4.3.1 AI 发送后可见性三个 P0 决议

以下三项是 AI 活群群日总量、冻结账号覆盖、Planner 防重、Action/Attempt 状态和 admission 操作的共同不变量，supersede 旧 hard-hourly credit/durable-debt 口径：

| P0 | 唯一决议 |
| --- | --- |
| P0-1：`pending_visibility` 是否占规划名额 | 占 1 个且只占 1 个。它与 `unknown_after_send` 共用 post-Gateway 未确认占位，计入兼容字段 `unknown_after_send_hold_count`；不得再增加第三个公式项，也不得为同一 `primary_quantity_slot_id` 创建替代发送 |
| P0-2：`post_send_intercepted` 后永久不可 ready | 被拦截 Action 明确失败并释放自身占位，但原 coverage 主槽和冻结账号分母不删除。未显式放弃时持续等待重新准入；运营 `admission_abandoned` 只停止该账号自动准入并跳过未进 Gateway Action，coverage 主槽保持 `blocked`，deadline 后只能 `missed`。其他 ready 账号只能完成自己的 coverage 和尚未分配的 extra-volume 槽，不能替代该账号 |
| P0-3：Attempt 成功且有 remote id 是否立即计群日/覆盖 | 需要可见性核验时不能。Gateway 回执只建立 `pending_visibility` 占位；只有 `visible_confirmed` 才在一个短事务内完成 Action、群日主槽和匹配 coverage。无需可见性核验的普通成功仍按真实 Attempt + remote id 直接确认 |

统一投影公式为：

```text
post_gateway_unconfirmed_hold_count =
  pending_visibility_count + unknown_after_send_count

unknown_after_send_hold_count = post_gateway_unconfirmed_hold_count
  # 兼容字段名；不得只统计 unknown_after_send

planning_reservation =
  eligible_open_count + post_gateway_unconfirmed_hold_count
```

在全任务读模型中，`eligible_open_count` 投影为 `held_count`；`pending_visibility_count + unknown_after_send_count` 合计投影为 `unknown_count`，并按 `hold_reason` 分列，不能同时计入 `held_count`。因此既有 `planning_deficit_count = due - confirmed - held - unknown` 公式不增加新减项。

状态与写入顺序固定：

1. Gateway 返回 non-empty `remote_message_id` 且本条需要可见性核验时，Attempt 可以保存传输边界成功，但 Action 只能进入 `pending_visibility`，业务成功仍为 false。逻辑事实命名为 `pending_visibility_hold`；现有物理模型 `PendingVisibilityCredit/pending_visibility_credits` 仅作兼容表名，任何 API、页面、统计和验收均不得把其中的 `credit` 理解为已计成功。
2. `pending_visibility_hold` 对 `action_id` 唯一，并绑定原 `task_day_ledger_id + primary_quantity_slot_id + optional coverage_id + remote_message_id + admission_version`；另以 partial unique 保证同一 `primary_quantity_slot_id` 同时最多一个 open/unknown hold。重复 Recovery/多 worker 必须回读同一 hold，不得再占一个槽。
3. `visible_confirmed` 在同一短事务中锁定 hold、Action、主槽、可选 coverage 和远端事实所有权；幂等关闭 hold、Action 转 success、群日 confirmed 精确 `+1`，并仅在绑定 coverage 未完成时确认该账号覆盖。任一唯一键/CAS 冲突先使确认事务整项回滚；随后由独立 writer 重读仍冲突的对象并持久化对象级 `consistency_quarantine`，不得把会被回滚的 quarantine 当作已落账，也不得出现“群日已加但 coverage/Action 未确认”。
4. `post_send_intercepted|visibility_confirmed_failed` 在同一短事务关闭 hold、把 Action 写为明确失败并撤回该 admission 的 ready；群日和 coverage 均不增加。原主槽保留并在账号重新 `admission_ready` 后才允许下一 tick 递增 attempt 建新 Action，不能在 blocked 期间循环试发。
5. 完整核验窗口结束仍无法判断时，Action 进入 `unknown_after_send`，原 hold、主槽和远端证据继续保留；这是同一 Action/同一逻辑 hold 的状态迁移，必须在同一事务把 `pending_visibility_count -1`、`unknown_after_send_count +1`，不得新增第二条 hold 或让合计占位变成 2。不得超时当成功、超时当失败或自动重发。只读远端核验或带证据人工裁决复用第 3/4 条原子终结。
6. `admission_abandoned` 只能由 `targets.manage` 在 preview、reason、evidence 和 `expected_admission_version` 校验后写入；系统不得因超时、积压或一次 intercepted 自动写入。已经 Gateway-started、`pending_visibility` 或 unknown 的 Action 不受 abandon 改写。reopen 递增 `admission_version`，不回写历史任务日。

### 4.4 配置错误与外部容量不足

创建/编辑不做容量预检或风险确认，只分结构校验与运行期事实：

| 类型 | 例子 | 处置 |
| --- | --- | --- |
| API 调用者无权 | AI 活群、评论、点赞、浏览缺少 `tasks.manage`；纯搜索点击缺少 `tasks.manage` 或 `tasks.create.search_click`；或请求引用不属于当前用户且不可见 | 返回 403/不泄露对象存在性的 404，不创建 Task；它是 API 边界，不进入任务 blocker，也不是容量预检 |
| 结构性配置冲突 | 必填目标/账号范围缺失；引用类型与任务声明用途静态不兼容；内容规则 `required>max` 或版本不可解析 | 返回 422，指出字段和冲突；评论/浏览低任务级软上限不再由请求决定，系统直接归一为 `1_000_000` |
| AI 账号范围为空 | 请求没有账号范围引用 | 创建时 `account_scope_reference_missing` 422；若账号组/全部账号范围引用合法但启动时暂时解析为 0 个普通运营账号，则 Task 已创建并保持 running，显示 `account_scope_empty_runtime`，账号进入范围后自动继续 |
| AI 活群日容量预测不足 | 理论日容量低于群日目标、部分账号尚未准入 | 仅提示 `completion_risk`，不拒绝、不暂停、不产生 `PlanAbort`；按 `due_by_now` 和未占位欠额持续建单 |
| 其他任务外部容量不足 | 搜索账号池不足、动态新消息使当日需求增加 | 允许创建/启动；只在运行详情显示 `blocked/at_risk`，按所有当前合法容量持续追赶 |

外部容量恢复后 Planner 自动继续未完成欠额；不得缩小原目标或把 blocker 改写为完成。

结构性硬阻塞采用封闭清单，不得继续发明新的整任务门禁：

1. `task_contract_invalid`：必填字段、当前用户可见的目标/账号范围引用、引用类型与声明用途的静态兼容性、数量合同或任务类型结构无法形成唯一合法 Task；API 调用者授权失败在持久化前返回 403/404，不写成该 blocker；
2. `target_reference_terminal|remote_capability_denied`：目标引用已确认终态，或 Telegram 对具体账号/目标/动作明确给出不可恢复禁止；只阻塞被证明的作用域；
3. `account_identity_invalid`：Task 创建后发现账号被永久删除、运行身份/用途状态失效，或授权资产与账号身份不一致；只阻塞该账号；
4. `reply_target_unrecoverable`：显式“回复指定消息/评论”的单目标合同已确认目标终态失效，或普通 reply 作用域已经关闭/到达 deadline 且始终不存在合法替代对象；只阻塞该 reply 槽，不能降级 direct。普通 reply 槽在 deadline 前暂时没有候选只能写 `reply_target_waiting`，不属于结构硬阻塞；
5. `content_contract_unreplayable`：`ContentMixContract` 结构非法、版本不可重放或历史内容归属存在无法审计消除的歧义；
6. `fallback_outbound_policy_blocked`：精确 `签到` / 评论单表情本身被明确出站安全策略禁止。

缺面具、主/备用 AI 失败、已验证代理路线切换、等待入群审批、暂时无 can-send、Dispatcher 份额不足和传输路线暂不可用均不是结构性终态：分别进入确定性内容兜底、准入等待、公平重排或 `waiting_transport`。Gateway 已开始但结果未知是防重 hold，也不是结构性失败。任何 blocker 默认只影响对应账号、目标或逻辑槽；只有全部剩余义务均被同一不可恢复结构事实覆盖时，任务级状态才可为 `blocked`。

创建期与运行期的账号边界必须分开：调用者无权访问账号范围时在 API 边界返回 403/不泄露存在性的 404；当前用户可见但引用不存在、类型错误或与任务声明用途静态不兼容时，属于请求级 `task_contract_invalid` 并返回 422。引用本身合法，但具体账号在启动后或运行中被删除、用途状态变化、授权资产漂移或身份失效时，属于账号级 `account_identity_invalid`。后者不得回滚 Task、缩小已冻结分母或阻塞其他账号，也不得被新增为另一种创建预检。

数据库唯一键冲突、远端事实已绑定另一个义务、Action payload 损坏或历史归属无法立即判定时，进入 `consistency_quarantine` 并告警/reconcile；不得猜测计数、不得复用该事实，也不得把系统数据问题扩展成第 7 类产品门禁。只有受影响对象暂停，其他独立义务继续。

Planner 以父 Task 为最小异常隔离单元：单个 Task 的代码异常或对象一致性异常必须回滚该 Task 当前规划事务，记录包含错误类型、摘要和时间的 `planner_runtime_error` 并保留异常日志，随后继续规划同轮其他 Task；故障 Task 延迟 30 秒后重试，成功规划时清除该诊断。该机制只隔离系统故障，不能把失败 Action、义务或远端事实写成成功，也不能据此缩小目标或完成 Task。

运行中 Task 在接管/Planner 首次物化时命中 `task_contract_invalid`，必须写入 `fulfillment_takeover_status=blocked`、具体错误和检查时间，将该 Task 显式置为 `paused`，不得让异常退出 Planner worker，也不得继续每轮忙重试；修正配置并由用户恢复后重新执行接管。对象级一致性问题仍按上一段隔离，不能借此暂停整个 Task。

### 4.5 内容编排非回归合同

本次修复只回答“发送义务是否继续创建、何时调度、失败后如何继续履约”，不改变“这一条原本应以什么形态发送”。下列既有合同必须原样保留：

| 任务 | 受保护内容合同 | 禁止行为 |
| --- | --- | --- |
| AI 活群 | `reply_min_per_round`、direct/reply 槽位、每个 Action attempt 的 `reply_to_message_id` 快照、账号面具 `emoji_policy`、正常文本 emoji 习惯、`material_intent/allow_material`、既有图片/表情包/custom emoji 每轮比例、意图映射与素材冷却 | 删除门禁时把引用槽位改普通消息；原地改写某个 attempt 的引用对象；用签到吞掉图片/表情包槽位；重新默认比例 |
| 频道评论 | `comment_mode`、`reply_min_per_message`、direct/reply 槽位、每个 Action attempt 的 `reply_to_message_id` 快照，以及任务/规则已启用的普通文本 emoji、图片、表情包或 custom emoji 占比 | 用单表情兜底把 reply 改 direct；原地改写某个 attempt 的引用对象；把兜底 Unicode 表情冒充正常文本 emoji 或图片/表情包配额 |

执行顺序固定：

1. 先按原任务配置冻结总发送义务，并由既有 Planner 拆出不可变 direct/reply 关系槽；首次物化 Action 时选择并冻结该 attempt 的具体 `reply_to_message_id`。需要素材的正常内容继续按既有素材规则选择，不因日容量、硬小时或活动时段门禁删除而改变。
2. 门禁与节奏层只能决定 `scheduled_at/next_retry_at/claim`，不得原地改写既有 Action attempt 的 `reply_to_message_id`、关系类型、已冻结素材类型或占比归属。只有 Gateway 前确认引用对象失效时，才可保留同一 reply 逻辑槽、递增 `slot_attempt` 并在新 Action 中冻结另一个合法引用对象；旧 Action 快照永久保留。
3. `签到` / 单 Unicode 表情是原数量/关系槽位的局部内容兜底：必须由原逻辑槽位发送并保留 direct/reply 关系。原槽已有图片、表情包、custom emoji 或 normal-text-emoji 义务时，保留的是内容义务及审计归属，不代表兜底 payload 必须或允许携带该素材；是否原槽共载只由下一步兼容矩阵决定，不兼容时先转派，纯文本永远不能消费素材配额。
4. Phase C 一旦决定使用确定性兜底，必须在冻结 outbound payload 前按下表检查共载兼容性。兼容时，原 Action 同时携带并按真实远端类型确认该素材；不兼容时，必须先在短事务中把原内容义务标记 `released_before_gateway`，再以 `assigned_action_id + assignment_version` CAS 转派到同 scope 尚未进入 Gateway 的合法正常槽位。CAS 成功后兜底 Action 固化 `planned_material_kind=none/content_source=fallback`；CAS 失败必须重读，禁止两条 Action 同时携带同一义务。
5. 到 deadline 仍未补齐时分别报告 `reply_mix_shortfall` / `material_mix_shortfall`；超过既有素材上限、冷却或正常 emoji 策略时报告 `material_mix_overflow` / `material_cooldown_violation` / `normal_emoji_policy_violation`。总量已完成不能把内容占比违规伪装成达标，内容占比违规也不能反向抹掉真实总发送成功。
6. 素材未命中、缓存未就绪或媒体 Gateway 失败继续执行既有素材专项的换素材/等待/文本降级/跳过规则，不得被本次签到或评论表情兜底改写；若既有策略最终只发出文本，该次只按真实文本确认总量，素材缺口仍留在 mix 账本。

| 内容义务 | 与当前确定性兜底共载合同 | Gateway 前处置 |
| --- | --- | --- |
| direct/reply | 必须兼容；它是关系义务，不是素材类型 | 原槽位和 `relation_kind` 不变；reply 继续使用该 attempt 冻结的合法 `reply_to_message_id` |
| `normal_text_emoji` | 不兼容；精确 `签到` 和评论单 Unicode 表情均不属于正常正文 emoji | 先转派到正常纯文本槽位；无合法槽位则保留 shortfall |
| `image` | 仅当已批准 Gateway profile 明确支持该图片与兜底正文同消息发送，且远端实际类型可核验时兼容 | 兼容则原槽携带；否则发送前转派 |
| `sticker` | 不兼容当前精确正文兜底合同 | 发送前转派；不得把纯文本成功记成 sticker |
| `custom_emoji` | 不兼容当前精确 `签到` / 单 Unicode 表情合同 | 发送前转派；不得把普通 Unicode 字符记成 custom emoji |

同 scope 已无合法转派槽位时，兜底仍可按原数量槽发送并完成数量义务，但内容义务保持未完成，deadline 后形成明确 shortfall；不得为了补素材额外增加超过冻结总量的消息。

内容编排按原业务作用域核算，不按技术批次重置：

- AI 活群：`ai:{task_id}:{target_operation_target_id}:{cycle_id}:{config_revision}`；
- 频道评论：`comment:{task_id}:{channel_message_id}:{comment_plan_revision}`。`comment_plan_revision` 在消息首次固化目标时创建；同一消息后续补差额、数据库切批、失败重领和 Gateway 前 reply 重建都复用该 revision。运行中配置修改只影响修改后新纳入的频道消息；已纳入消息的 revision、目标 ordinal 和内容合同永不重置。

数据库每批 20 条、Dispatcher claim 批次、静默降量或失败重领都不能创建新的 mix 分母；逻辑 Cycle 实际 Turn 少于配置值时，引用目标只能是 `min(reply_min_per_round, logical_cycle_turn_count)`，不能借拆小批次多次套用最小值，也不能借合批降低最小值。素材规则中的“每轮”同样绑定原 `cycle_id`。

#### 4.5.1 ContentMix Cycle 生命周期

AI 每个自然对话 Cycle 必须持久化 `ContentMixCycle` 与完整 `ContentMixCycleSlot`，不能只把 `cycle_id/slot_id` 放在随后创建的 Action payload：

```text
ContentMixCycle
  task_id / target_operation_target_id / task_day_ledger_id
  cycle_seq / cycle_id / config_revision
  scope_total_slots / allocation_seed
  allocation_closed_at
  materialization_status = pending | partial | complete
  materialized_slot_count
  settlement_status = open | settled
  settlement_outcome = met | shortfall | missed | null
  settled_at

ContentMixCycleSlot
  cycle_id / slot_index
  primary_quantity_slot_id
  relation_kind / reply_requirement_key / initial_reply_to_message_id
  slot_attempt / current_action_id
  slot_state = unmaterialized | pending | gateway_started | unknown
             | confirmed | replan_required | terminal
```

生命周期固定：

1. Planner 锁定当前 target-day 的 cycle cursor，选择尚未绑定 Cycle 的 AI 主发送槽，并在一个短事务中创建 Cycle、全部 CycleSlot、`ContentMixContract` 和 `policy_min` 义务；唯一键为 `(task_id, target_operation_target_id, task_day_ledger_id, cycle_seq)`，同一 `primary_quantity_slot_id` 只能绑定一个 CycleSlot。
2. `scope_total_slots`、关系槽、reply 选择合同和内容分母在该事务提交时一次性冻结并写 `allocation_closed_at`。`relation_kind` 永远不变；`initial_reply_to_message_id` 只作首个 attempt 审计，具体引用对象以每个 Action attempt 的不可变快照为准。事务任一步失败整体回滚，不留下半个 Cycle。
3. 后续每批最多 20 条 Action 只是把已存在的 CycleSlot 物化为执行 Action。Action 唯一键为 `(cycle_slot_id, slot_attempt)`，CycleSlot 以 `current_action_id` 指向当前尝试；同一 attempt 重放返回原 Action，数据库保证同一 CycleSlot 同时最多一个 current open/unknown/success Action。20+10、20+20+20、多个 claim、worker 重启、静默降量、主 AI 三轮、备用 AI 三轮和签到全部复用原 `cycle_id/slot_index/slot_attempt`，不得新建 Cycle 或重算分母。`materialized_slot_count` 统计至少有一条 Action 历史的 CycleSlot，0/部分/全部分别投影 pending/partial/complete；首次全部物化后不因重试回退，不能靠非原子累加猜测。
4. pre-Gateway Action 明确终态且逻辑槽可恢复时，先以 CycleSlot 行锁/CAS 写 `slot_state=replan_required`，释放的只是旧 Action 的 runtime claim/发送占位，不释放 `primary_quantity_slot_id`、coverage 义务、关系槽或内容义务；再原子递增 `slot_attempt`、创建新的 `(cycle_slot_id, slot_attempt)` Action 并替换 `current_action_id`。reply 槽可在新 attempt 选择另一个合法引用对象，但 `relation_kind=reply` 不变，旧 Action 的 `reply_to_message_id` 永不改写。历史 terminal Action 保留。重建时只从旧 Action 解析并校验 Cycle/规则/素材合同元数据，再与本轮新生成的可发送 payload 合并后执行完整校验；`reply_target_stale` 等失败 Action 允许空正文，不能先套用“可发送正文非空”校验而阻断重建。Gateway-started/unknown/success 禁止创建替代。未物化或 `replan_required` 的 CycleSlot 由恢复 worker继续，不得因进程重启丢失。旧 Cycle 尚 open 时允许为其他未绑定主发送槽创建新 Cycle，但两个 Cycle 不得共享 `primary_quantity_slot_id`。
5. 配置修改不改写已分配关闭的 Cycle。新配置只用于生效后新建的 Cycle；旧 Cycle 的 Action、Attempt、远端事实和 content mix 继续按原 `config_revision` 收口。settled Cycle 完全只读且禁止原地 retry；若产品另有明确的人工恢复操作，必须建立独立 recovery revision，不能重开旧 Cycle、改写旧日 outcome 或再次消费旧主发送槽。
6. deadline 到达时，尚未物化、`replan_required` 或未进 Gateway 的 CycleSlot 以稳定 slot 顺序写明确 `deadline_reached_unmaterialized|deadline_reached_before_gateway`，对应数量/内容 shortfall；Gateway-started/unknown 继续保持 hold，不得为了结算强制终态。
7. Cycle 只有在所有 CycleSlot 的 `slot_state` 均为 `confirmed|terminal`，且不存在 pending/gateway_started/unknown/replan_required、全部 ContentMixObligation 已完成或形成明确 shortfall 后，才由幂等 reconciliation 写 `settlement_status=settled`、`settlement_outcome` 与 `settled_at`。unknown 在核验完成前保持 open；deadline 只改变履约结论，不允许删除 Cycle、重开分母或阻塞其他独立 Cycle。

`settlement_outcome` 判定顺序固定：全部主发送槽均在 deadline 内 confirmed 且内容义务均 met 为 `met`；存在不可恢复 quantity/content/reply 缺口但结算发生在 deadline 前，或数量已按时完成但内容仍有明确缺口，为 `shortfall`；结算发生在 deadline 后且仍有任一主发送槽未取得 on-time confirmed 为 `missed`。late fact 保留但不能把 `missed` 改为 `met`。Cycle 的 outcome 只描述该 Cycle，任务日最终状态仍由所有主发送槽和所有 Cycle 汇总，不允许用单个 Cycle 的 `met` 完成整日。

频道评论不新增另一套 Cycle：`comment_plan_revision` 就是消息级内容作用域生命周期。首次规划必须在一个短事务冻结该消息全部目标 ordinal、direct/reply 关系、reply 选择合同、`ContentMixContract` 和 `policy_min` 义务；补差额、技术切批、主/备用 AI、单表情兜底和重试只物化尚未完成的原 ordinal。reply 对象在 Gateway 前失效时，原 ordinal 保持 reply，只用递增 attempt 的新 Action 冻结新对象。运行中配置修改只让修改后新纳入的消息使用新 revision；同一频道消息既有 revision 不重置。

`quantity_status` 与 `content_mix_status` 分开：前者回答数量义务是否完成，后者回答既有引用/素材编排是否保持。完整修复的 E4 验收要求两者都为 `met`；不能靠拆状态降低产品验收标准。

任务详情与生产验收至少分列 `planned/success/shortfall/overflow` 的 direct、reply、normal_text_emoji、image、sticker/custom emoji 数量，并单列 `check_in_fallback_count/comment_emoji_fallback_count`。确定性兜底不得进入正常文本 emoji 或图片/表情素材比例的成功分子，也不得缩小或重算原计划分母/目标数、重置上限或冷却；只有实际保留原引用关系或素材类型时，才同时确认相应内容槽位。

归因规则固定：reply 兜底只有远端实际携带原 `reply_to_message_id` 才计 reply；`normal_text_emoji` 只统计 `content_source=normal` 的正常正文，`comment_emoji_fallback` 永远单列；图片/sticker/custom emoji 兜底只有原槽位已归属该素材类型且远端实际消息类型一致时才计该素材槽位。

不新增任务级运营比例字段，但现有 RuleSet / 素材策略必须在业务作用域建立时解析为不可变 `ContentMixContract`；禁止继续依赖任意 JSON 在执行时临时解释。合同至少保存：

```text
content_mix_scope_key / content_contract_version
scope_total_slots / allocation_seed
reply_min_required_count
reply_planned_count / direct_planned_count
normal_text_emoji_required_count / normal_text_emoji_max_count
image_required_count / image_max_count
sticker_required_count / sticker_max_count
custom_emoji_required_count / custom_emoji_max_count
material_policy_rule_set_id / material_policy_rule_set_version
target_resolution_trace
```

`scope_total_slots` 永远等于该逻辑作用域在确定性兜底判断前冻结的原始总槽位数；签到、评论单表情、技术切批、失败重领或 early fallback 均不得缩小它。fallback-only 只表示该槽位不运行正常素材 selector、不计入 normal 素材 planned/success 分子，并不改变显式比例的原分母或已计算 required/max count。

解析规则固定：

1. 已有策略表达“至少比例”时用 `ceil(scope_total_slots × ratio)`；表达“最多比例”时用 `floor(...)`；多个精确比例同时存在时使用按素材类型稳定排序的最大余数法，且总数不得超过作用域总槽位。
2. 已有策略只有 intent、冷却或“最多”规则、没有最低比例时，不虚构最低目标；`required_count=0`。但相同配置、上下文、`allocation_seed` 和旧选择器为“已通过质量并准备进入发送链的正常候选”实际选出的计划槽位，必须在 Phase C 接受该候选的同一事务冻结为 `selector_plan` 内容义务；被质量拒绝的候选只留 attempt trace，不形成 planned 义务。后续传输兜底、重领或技术切批不得把已经形成的图片、表情包、custom emoji 或正常文本 emoji 计划改写为纯文本成功。
3. relation 先由既有 Planner 按原规则拆出逻辑槽位，再冻结 `reply_planned_count/direct_planned_count`；二者之和必须等于 `scope_total_slots`。`reply_min_required_count=min(configured_reply_min, scope_total_slots)`，Planner 至少预留这么多 reply 逻辑槽位，并保留原规则本来会选择的额外 reply；不能仅用最低值反推 direct 数。当前没有合法引用对象时槽位进入 `reply_target_waiting`，不能把 reply 数降下来。
4. 解析后 `required_count > max_count`、同一互斥维度内的最低数之和超过总槽位或策略版本无法重放时为 `content_mix_policy_invalid`；创建/编辑阶段映射为 `task_contract_invalid` 并返回 422，不新增第七类结构门禁。若运行时 relation splitter 未预留应有 reply 槽位，则为可恢复的 `content_contract_replan_required` 并重建未进 Gateway 槽位；只有合同版本确实无法重放时才升级为闭集内 `content_contract_unreplayable`。不得把候选不足误报成配置 422。素材种类属于一个互斥维度，relation 与素材可在同一槽位叠加，不能把 reply + image 误判为两条。存量任务保持 running，但正常内容进入显式 replan，不得猜测比例或把确定性兜底算成正常素材。
5. 缺面具等发生在正常素材意图产生之前的兜底槽位单列为 fallback-only，不虚构 normal 素材选择或成功；`scope_total_slots` 和显式比例分母保持不变。若 `policy_min` 或已形成的 `selector_plan` 素材义务分配给该槽位，仍须按下述义务转派规则补齐。

Action 复用现有关系与素材事实，并把规划时合同快照保留到结果：

```text
relation_kind = direct | reply
reply_to_message_id
content_mix_scope_key / content_contract_version
material_policy_rule_set_id / material_policy_rule_set_version
material_intent / allow_material
planned_material_kind = unresolved | none | image | sticker | custom_emoji
planned_normal_text_emoji = unresolved | yes | no
content_source = normal | check_in_fallback | comment_emoji_fallback
```

`relation_kind` 在 Planner 拆槽时确定；素材策略快照随 Action 固化。`planned_material_kind` 与 `planned_normal_text_emoji` 初始可为 `unresolved`，只在既有内容/素材选择阶段或内容义务分配时解析为最终值，解析后对该 Action 不可变。读模型从合同、义务绑定、这些字段和远端实际 message type 派生 `content_mix`，不得通过正文包含某个 Unicode 表情反推成表情包/custom emoji 成功。

关系槽位不可降级：pre-Gateway 发现 `reply_to_message_id` 失效时，当前 Action 以 `reply_target_invalid` 终结并释放该 `content_mix_slot_key` 的发送占位；Planner 只能用同一 `slot_id/content_contract_version/relation_kind=reply` 和递增的 `slot_attempt` 选择新的合法引用对象重建 Action。不得把 reply 改 direct、不得额外增加总槽位。没有合法引用对象时保持 `reply_target_waiting`，deadline 后为 `reply_mix_shortfall`；Gateway-started 或 unknown 槽位继续占位，禁止替换。direct 槽位也不得被重建成 reply 来掩盖另一个 reply 缺口。

每个必须保留的内容槽位形成 `ContentMixObligation(scope_key, mix_kind, obligation_source, ordinal)`；`mix_kind=normal_text_emoji|image|sticker|custom_emoji`，`obligation_source=policy_min|selector_plan`，唯一键为 `(tenant_id, scope_key, mix_kind, obligation_source, ordinal)`。`policy_min` 在合同建立时复用既有 RuleSet 的槽位分配算法及同一 `allocation_seed`，按 `mix_kind + ordinal` 稳定绑定逻辑槽位；不得另写一套分配器改变原选择结果。该槽位后续 selector 只能在已指定 kind 内选择具体标签/资产，不再创建第二份义务。既有选择器在无最低绑定的槽位为已通过质量的正常候选确定素材类型时，必须在 Phase C 接受候选、固化 planned 字段的同一短事务中创建 `selector_plan`；被拒绝候选的临时选择不创建义务。迁移/重放旧计划时，已经由同槽同 kind `policy_min` 覆盖的选择不重复创建，只有额外实际计划槽位才新增。义务通过 `assigned_action_id + assignment_version` CAS 指向该逻辑槽位当前 Action。缺面具等在素材 intent 前触发且没有 `policy_min` 时不创建 `selector_plan`；已经完成选择后再转为不能实际携带该 kind 的确定性兜底时，两类义务按相同规则处理：

1. 总量仍按真实远端结果确认；
2. 内容义务保持未完成，并优先转派到同一 scope 尚未进入 Gateway、对该 `mix_kind` 合法且仍未占用冲突义务的正常槽位；normal-text-emoji 只能转给正常纯文本槽位，媒体 kind 按既有素材规则选择；
3. 转派只改变 obligation 的当前绑定并追加审计，不改写原 Action 的历史快照；新绑定 Action 在 Gateway 前固化相同 `mix_kind`；
4. 转派前必须在同一短事务把原 Action 对该义务标为 `released_before_gateway`；Gateway-started/unknown 或仍可能按原 kind 成功的 Action 禁止转派。双 Planner/Dispatcher 只能有一个 CAS 成功；
5. scope 已无剩余合法槽位时不得超出总量补发，deadline 后明确 `material_mix_shortfall`。数量可为 `met`，但完整验收不得通过。

普通 pre-Gateway 失败但逻辑槽位仍会按相同内容合同重建时，义务保留在原 `content_mix_slot_key`，仅以 CAS 更新 `assigned_action_id` 到递增 `slot_attempt` 的新 Action；只有该逻辑槽位确定转为不携带该 kind 的 fallback 时才允许跨槽位转派。

并发与幂等复用 Action 事实：`content_mix_slot_key = tenant_id + task_id + content_mix_scope_key + slot_id + content_contract_version` 标识逻辑槽位，`slot_attempt` 标识该槽位的重建次数；数据库必须保证同一逻辑槽位同时最多一条 `open/unknown/success` Action，历史 pre-Gateway terminal Attempt 可并存。pre-Gateway 明确失败释放该 slot 的发送占位但关系类型不变；`unknown_after_send` 和 success 永久阻止该逻辑槽位创建替代；仅在 Action 取得业务成功后，以 Attempt 的实际 reply/media/normal-text-emoji 类型确认内容事实，其中需可见性核验的 AI 消息必须先有 `visible_confirmed`。`content_mix` 聚合从合同、义务、Action 和 Attempt 重算，不另设可被并发直接累加的成功计数器。

内容构成状态固定为：

| 状态 | 判定 |
| --- | --- |
| `not_applicable` | 该任务类型没有内容构成合同。 |
| `met` | 所有 `policy_min`、已冻结 `selector_plan` 内容义务及 direct/reply 关系槽位均由远端真实类型确认，无 overflow/cooldown/policy violation，且该 scope 已关闭或不存在会改变结论的 open/unknown 槽位。 |
| `at_risk` | 尚未到 deadline，当前未完成但仍有合法未进 Gateway 槽位可以补齐，或 unknown 仍需远端核验。 |
| `blocked` | 尚未到 deadline，但已证无合法槽位可补齐，或已发生不可逆 overflow/cooldown/policy violation。 |
| `missed` | 已过 deadline 且未满足 `met`。 |

任务列表和详情同时返回 `quantity_status`、`content_mix_status` 与 `acceptance_status`。组合算法固定：已过 deadline 且任一适用维度非 `met` 时为 `missed`；未截止时任一适用维度为 `blocked` 则为 `blocked`，否则任一为 `at_risk` 则为 `at_risk`；只有所有适用维度均为 `met` 才为 `met`。其他没有内容构成合同的任务，`content_mix_status=not_applicable` 且 `acceptance_status=quantity_status`。兼容字段 `status` 在迁移期固定等于 `acceptance_status`，不得继续只投影数量状态；界面必须同时展示两个底层维度，不能用组合状态隐藏真实数量成功。

## 5. 共用调度与事务设计

### 5.1 三种容量不得混用

| 参数 | 含义 |
| --- | --- |
| `ACTION_CLAIM_LIMIT` | 单次数据库候选查询/claim 批量上限 |
| `DISPATCHER_CONCURRENCY` | 单个 Dispatcher 进程同时执行的 Action 上限 |
| `DISPATCHER_SCOPE_CAPACITY` | 所有共享同一 `dispatcher_scope` 的 worker 合计在途上限 |

`DISPATCHER_SCOPE_CAPACITY` 必须由部署拓扑显式配置，并满足：

```text
scope capacity
<= active dispatcher count × per-worker concurrency
<= database writeback connection budget
<= Telegram Gateway safe inflight budget
```

生产不能直接把当前 `ACTION_CLAIM_LIMIT=100` 当作 scope capacity。所有共享 worker 对同一 scope 必须读取相同的配置版本；不一致时停止新增 claim 并显示 `dispatcher_scope_capacity_mismatch`。

同一 `ready` Window 的 Action 新增、claim、executing、success/failed 状态变化不得触发整窗重建。当前 epoch 尚有未领取 Reservation 时继续消费原不可变分配；只有明确释放形成非空 release set，或原 epoch 的全部可领取 Reservation 已消费且仍有新到期需求时，才开启唯一 pending rebuild wave。任务只要取得大于 0 的份额即为 `allocated`，不得因“部分小于 required”把整个任务标成 `shared_dispatch_capacity_insufficient`。

盖章 `fulfillment_contract_version=all_task_v2` 的五类任务不再读取旧 `max_pending_global|max_pending_per_task|oldest_pending_age_seconds` 作为 Planner 数量门禁。旧 backlog 只服务未迁移任务；新模型由不可变义务、同义务单 open/unknown Action、中央 Reservation 和 scope in-flight capacity 防止重复与过载。接管必须清除五类任务遗留 `planner_backlog_*` 和陈旧 `shared_dispatch_capacity_insufficient`，搜索同时清除 `search_join_stats/daily_target_capacity_insufficient`，浏览清除旧 `task_daily_view_safety_cap` 命中错误，并把运行中任务的 `next_run_at` 推到当前时刻重新规划。`DISPATCHER_SCOPE_CAPACITY` 只限制同一时刻真实在途量，不得减少总目标、形成任务终态或阻止后续 Window 继续履约。

### 5.2 锁顺序与事务边界

claim 事务锁顺序固定：

```text
DispatchClaimScope
-> DispatchClaimWindow
-> DispatchClaimTaskAllocation
-> DispatchClaimShardAllocation
-> DispatchClaimReservation
-> Action
```

搜索 commit 的 `SearchClickAssignmentEpoch`、账号/关键词 quota 子预留与 `SearchClickOpportunityAssignment` 归入 `DispatchClaimReservation` 层：`SearchClickAssignmentSolver` 在无事务快照上完成；求解期读取事务必须先明确结束，再从无查询的新事务起点设置 PostgreSQL `SERIALIZABLE`，禁止在已有查询的 active transaction 内执行 `SET TRANSACTION`。outcome finalize 随后先锁定并复核上述 `Scope -> Window -> TaskAllocation -> ShardAllocation -> Reservation` 中央前缀，再依次锁 search epoch/release batch carrier、按稳定 unit key 锁既有 assignment、按稳定 resource key CAS 搜索 consumptive 子预留，最后锁/创建 Action；不存在的层只可跳过，不可换序。`_confirm_claim`、Gateway 前最终守卫、post-finalize release 与 Reconciler 必须复用同一扩展顺序。release set 的 exclusion、汇总计数和 search epoch finalized 同事务提交；bound assignment 释放不能重开 epoch。Window ready 的首批非空释放开启一个 pending rebuild wave；已处于 `rebuild_required` 的后续 batch 只增加 `rebuild_input_version`，已结束 Window 只收口事实。不得在求解期间持锁，也不得先锁 Action、assignment、搜索资源或 carrier 再反向取得上游层。

claim 事务不得更新 `Task.stats` 或每日覆盖账本。Planner 日履约收口固定拆成：

1. 短事务读取不可变规划输入并提交任务运行边界；
2. 按主键稳定顺序批量更新 coverage/message ledger 并提交；
3. 追加 fulfillment decision/audit 并提交。

统计快照在独立 reconciliation 事务派生；投影写失败必须显式告警，但不能回滚已经正确写入的 Action/Attempt 事实。

### 5.3 全任务 Claim Window 公平合同

`DispatchClaimWindow` 使用版本化 `claim_window_seconds`（默认 60 秒）；同一 `dispatcher_scope` 的所有 worker 必须读取相同值和配置版本。每个 Window 将到期候选按业务任务拆为 `membership_admission`、`ai_group_daily`、`channel_comment`、`channel_like`、`channel_view`、`search_click` 和 `ordinary`，不得把评论/点赞/浏览全部压入一个永远排在末尾的无保护队列。

当前产品只有一个业务用户/一个业务租户。`dispatcher_scope` 表示多个 Dispatcher worker、账号 shard 和任务类型共享的真实执行容量域，不表示多个用户之间的资源竞争；`tenant_id` 继续保留在唯一键、查询和审计中用于数据隔离，但当前分配算法不增加“租户级再分一次”的调度层。若未来引入多个独立付费用户共享同一 scope，必须另立产品设计后再增加 tenant -> task 两级公平，不能在本次实现中预埋推测性逻辑。

每个父业务任务先把业务义务（AI 群日/账号、频道消息、搜索 ledger/目标）按 `lane + deadline Window + pacing class` 聚合成需求 bucket，再跨账号 shard 聚合并持久化：

```text
due_claimable_count
due_claimable_by_lane_and_shard
obligation_demand_bucket = {
  bucket_key,
  obligation_count,
  remaining_business_debt,
  remaining_claim_windows,
  required_claims_for_bucket
}
required_claims = min(
  due_claimable_count,
  sum(required_claims_for_bucket)
)
last_opportunity_window
last_claimed_window
```

`required_claims_for_bucket = min(due_claimable_for_bucket, max(1, ceil(bucket_debt/bucket_windows)))`。相同 deadline 的 580 个账号欠额应先合并计算，不能因每账号 `ceil(1/windows)=1` 把每 Window 需求放大为 580；频道评论/点赞/浏览的不同 deadline 又必须分 bucket，不能用最晚 deadline 稀释早到期消息。TaskAllocation 只持久化总量、bucket 摘要/hash 和最早 deadline，不得把无界消息明细塞进热行。

`allocation_business_task_id = coalesce(admission_execution_sponsor_task_id, parent_task_id, task_id)`；已设计的准入 child 和其他前置子任务必须归入一个父业务任务，不能以自己的 Task ID 另取一份全局最低保护。纯搜索点击没有 search admission child；未来“搜索点击加入”在专项 PRD 完成前也不能预占该模型。`DispatchClaimTaskAllocation` 固化 `dispatch_allocation_epoch`，唯一键为 `(dispatch_claim_window_id, dispatch_allocation_epoch, tenant_id, allocation_business_task_id)`，保存上述需求、`allocated_claims`、父任务内部 lane 分配、`last_opportunity_window`、`last_claimed_window`、全局公平 cursor 版本和 CAS version。`DispatchClaimShardAllocation` 同样以 `(window, dispatch_allocation_epoch, shard_total, shard_index)` 唯一，`DispatchClaimReservation` 继承同一 epoch 且不能改绑。获得可映射 Reservation 时更新 `last_opportunity_window`，`_confirm_claim` 成功后才更新 `last_claimed_window`；两者不得混用。全局 cursor 在 allocation 短事务结束时推进，即使某任务随后资格变化也不能让同一任务在下一 Window 永远占首位。`DispatchClaimReservation` 再把该父任务已获份额映射到具体 `DispatchClaimShardAllocation`；不得直接在每个 shard 独立给同一任务或其 child 重复最低份额。

同一 `tenant + target + account + admission_version` 的 GroupBotAdmission 事实可被多个父任务复用，但真实 join/follow/confirm 执行只能有一份 `AdmissionExecutionLease`：

1. lease 唯一键为上述 admission key，保存 `sponsor_business_task_id/action_id/version/lease_state`；一个开放 lease 只能由一个父任务 admission lane 提供 Reservation；
2. sponsor 从当前 running、确实被该 admission 阻塞的父任务中按最旧 sponsor opportunity、scope cursor、task id 稳定选择；其他父任务只引用该 lease，不创建重复 Telegram Action 或额外 Reservation；
3. pre-Gateway sponsor 停止、Action 终态或失去资格时可 CAS 转给下一父任务并写审计；Gateway-started/unknown 不得转 sponsor 或重复执行；
4. admission ready 后共享事实 fan-out 只触发各 AI 父任务重新复检 membership/can_send。它本身不确认 AI 发送或 search click；纯搜索点击不读取该事实，存量 search membership 只作 `legacy_mixed_search_join` 历史审计，未来“搜索点击加入”尚无日目标合同。
5. admission 执行锁的最小作用域是 `(target_group_id, account_id, admission_generation)`；同群其他账号的 `unresolved|stale|waiting` 不得形成群级 busy。相同账号/世代仍由唯一 `AdmissionExecutionLease` 串行，运行配置的 admission 并发只限制真实在途数，不减少任何账号义务。

lease 选 sponsor/换 sponsor 使用独立短事务，只锁 admission lease 与等待者索引，必须在 Dispatch allocation 事务之前提交；禁止同时持有 lease 锁与 Scope/Window/Action 锁。TaskAllocation/Reservation 固化 `admission_execution_lease_id/version`，claim 与 Gateway 前复核版本；版本不符时释放未消费 Reservation并等待下一 `dispatch_allocation_epoch`，不能在 claim 热事务内现场换 sponsor。

时间字段口径固定：

- 有 deadline 的有限/日任务：`remaining_claim_windows=max(1, ceil((deadline_at-now)/claim_window_seconds))`，`remaining_business_debt` 取该 deadline 前尚未确认的业务欠额；
- 已过业务 deadline 但合同允许继续事实收口的 late/recovery Action：`remaining_claim_windows=1`，只使用其明确 late/recovery debt，不得反写按时完成；
- 无 deadline 的 continuous/ordinary 任务：使用版本化 `continuous_fairness_horizon_windows`，默认 60 个 Window；`remaining_business_debt` 只取该 horizon 内已到期的业务欠额，不得用 lifetime 历史 backlog 无限放大份额；
- 以上参数只计算公平份额，不改变业务 deadline、目标或完成状态；配置版本随 Window 固化，同一 Window 内不得漂移。

分配算法固定为：

1. 先对全 scope 所有 `required_claims>0` 的任务做最低保护轮转：按持久化 `dispatcher_scope` 全局 cursor，每个任务跨全部 shard 最多先分 1 个；任务数大于当前容量时，下个 Window 必须从上次未服务位置继续。
2. 剩余容量按未满足 `required_claims` 比例使用最大余数法分配；同余数按 `last_opportunity_window` 最旧优先，再按持久 cursor、task_id 稳定决胜；实际 claim 时间只用于诊断，不覆盖机会公平顺序。
3. 父任务内部使用 `fulfillment` 与 `admission` 两条通用 lane。两条均有 claimable debt 且父任务获配 `>=2` 时至少各得 1；只获配 1 时按持久化 `task_lane_cursor` 跨 Window 轮转，不能永久饿死 ready 发送或已设计准入。纯搜索点击只有 fulfillment lane，固定 `admission_lane_claims=0`，不得创建 admission child。lane 分配随 TaskAllocation 固化并写审计。
4. `DispatchLaneShardSolver` 将每个 `TaskAllocation` 的 task-lane 份额依据 `due_claimable_by_lane_and_shard` 和各 shard 剩余容量做单次确定性最大流/等价精确三层匹配，并在同一求解中把不可映射余额给下一合格 task-lane；必须满足 task-lane Reservation 不超过 lane 份额、父任务总 Reservation 不超过 task allocation、shard 总 Reservation 不超过 shard capacity，并在存在可行映射时不得因贪心顺序闲置容量。多解时按任务全局顺序、lane cursor、任务内 shard cursor、shard id 稳定决胜；最终仍无法映射的需求写 `shard_mapping_insufficient`，不能空占或让跨多 shard 的任务重复获益。
5. 没有任何“search 永远排在 AI 前”或“AI 永远排在频道互动前”的固定全局顺序。未获配任务写 `shared_dispatch_capacity_insufficient`、需求、获配、cursor 和下一 Window，不得静默 pending。
6. Reservation 只保证 claim 机会，不绕过账号、目标、`membership_admission`、协议、内容质量、Telegram 或 unknown 防重门禁；无可领取安全 Action 时保留原始 blocker，并通过稳定 release batch 释放未领取 unit；尚可领取 Window 加入当前/新 rebuild wave，已结束 Window 由下一 Window 重新核算。

中央分片权重发布使用 Window 屏障：常态 `allocation_state=ready`。尚可领取 Window 的首批非空释放把 epoch 增加 1、置 `rebuild_required` 并增加 `rebuild_input_version`，形成唯一 pending rebuild wave；wave 内后续释放不再递增 epoch，只更新 `rebuild_input_version`。`DispatchLaneShardSolver` 在无事务快照上冻结 `dispatch_allocation_epoch + rebuild_input_version + dispatch_rebuild_snapshot_hash`；该 hash 规范化覆盖 pending carrier、全部 task/lane/shard 的 due/eligibility 当前值与版本、active exclusion、全部仍有效旧 Reservation 承诺/计数/版本、scope/shard 容量及影响分配的配置值/版本，并排除 worker/lease、时间、进程和随机值。提交前在中央锁序内重读同一规范化输入并重算 hash；epoch、input version 或 hash 任一变化都整批丢弃旧权重，即使变化没有触发新的 release batch。成功时一次性插入该 epoch 的全部 TaskAllocation/ShardAllocation/Reservation，各行固化同一 `dispatch_rebuild_snapshot_hash`，Window 同时写 `ready_rebuild_snapshot_hash` 后才把状态 CAS 为 `ready`。计算、数据库错误或 worker 崩溃同样不发布部分权重。下一次 drain 从最新事实重建，零可分配余额也必须提交带该 hash 的空 `ready` 结果。Window 已结束不再创建或发布该 Window 权重。旧 epoch 已 released unit 不可再 claim；已 bound/claimed/active 和其他未释放旧 Reservation 继续按自身版本收口并计入容量。

重建 hash 必须是“所有影响 solver 输出的业务读取”的完整规范化序列，而不是上条最小枚举的任意子集。payload 还必须包含 `dispatch_rebuild_contract_version`、Scope/Window/Shard capacity/active/unclaimed 当前值与版本、所有 scope/task-lane/shard fairness cursor 与版本，以及 parent/sponsor 聚合输入；新增影响输出的读取时必须纳入 payload 并提升 contract version，版本只在 hash payload 内。worker/lease、时间、进程、随机值及不影响输出的诊断字段不得进入。这样跨 Window claim、cursor 或 sponsor 在计算期间变化，即使 `rebuild_input_version` 未动，也会被提交前重算拒绝。

唯一 immutable `DispatchRebuildInput` 是该集合的实现边界：assembler 负责从数据库构造、稳定序列化和取 hash；`DispatchLaneShardSolver` 是只接收该对象的纯函数，禁止自行查库或读取全局状态；precommit 在锁内重新构造完整对象并比 hash。不得分别维护“用于 hash 的字段列表”和“solver 实际读取列表”。

precommit assembler、hash 比较、全部新 allocation/reservation 与 Window ready 发布必须在同一个短 PostgreSQL `SERIALIZABLE` 事务内完成，并先按中央锁序锁 Scope/Window。SERIALIZABLE 读取集必须包含完整 input 的行与候选谓词，封住 rehash 后、commit 前的更新和 phantom；发生 serialization failure/CAS/hash 不等时整笔回滚，ORM/驱动不得拿旧权重自动重放，下一 drain 从新 input 重新 solve。非 PostgreSQL 实现只有在提供覆盖相同行集和谓词的可证明等价 fencing 时才可替代，单锁 Window 或单个 version 不等价。

中央 `dispatch_rebuild_contract_version` 或搜索 `solver_contract_version` 变化时禁止新旧 Dispatcher 滚动混跑。版本只进入各自规范化 hash payload，不保留运行历史；发布必须先阻止旧版本取得新 ownership，确认所有旧 Dispatcher 进程已终止且无旧版本数据库事务仍可提交，再启动新版本。旧内存 solver 输出一律丢弃，`rebuild_required` Window 由新版本重建；旧 owner 遗留的 open search epoch 在 fence 后直接按 `abandoned` 收口并释放未领取 unit，不转移 ownership、不沿用旧解。旧版本未被证明失去写资格时 Release Gate 失败；不得把该发布栅栏解释为 solver deadline、任务 blocker 或业务重试。

父任务/lane 内选择 Action 时先按业务义务 deadline 最早、未满足比例最高、义务 cursor 最旧稳定排序，再应用账号/分片安全资格；同一频道消息或账号不得吞掉父任务全部份额。该内部顺序只分配已获父任务份额，不建立新的全局类别优先级。

AI 准入积压不得阻止 `admission_ready` 账号的 `ai_group_daily`。纯搜索点击不进入 `membership_admission` 状态机；“搜索点击加入”待独立 PRD。一个频道消息的不可用 reaction 不得阻止其他消息；搜索 protocol 未通过 canary 时只阻止搜索批量 source。

### 5.4 deadline-aware pacing

先确定业务窗口，再把 operation curve 归一到窗口内。`moderate_6h` 的所有 planned time 必须在 6 小时内；自然日任务不得排到下一自然日。AI 活群例外为 24 个小时均可执行，静默小时只有较低非零权重，不得返回 `quiet_hours_active` 或活动窗口跳过；其容量预测只作风险提示。其他任务若当前安全速率无法在 deadline 前完成，返回 `pacing_capacity_insufficient`，不能延长 deadline。

五类新履约任务在计算 `scheduled_at/next_run_at` 前必须把 `quiet_hours` 从“跳到静默结束时间”的硬门禁转换为低权重，并把 `hourly_activity_curve` 的 0 归一为最小权重 1；静默配置仍可降低排序权重，但任何 running 任务都不能因此把下一轮整体推迟到静默结束。接管发布时，缺少当前软节奏合同版本的 running 任务必须一次性把遗留未来 `next_run_at` 唤醒为当前时间并写版本标记；重复接管不得反复覆盖正常下一轮时间。纯搜索点击的失主 open epoch 回收同样不能等待旧静默结束。

## 6. 分任务修复合同

### 6.1 AI 活群

1. 运营只配置每个群当天发送多少条；配置粒度为 `task + target_group`，多目标时逐群独立，不能共享或平均分。`effective_daily_target=max(daily_message_target,frozen_account_count)`，冻结范围内所有账号在每个目标群每天至少真实成功 1 条。
   ledger 冻结时必须形成每账号一个不可互换的 coverage 主发送槽，以及 `effective_daily_target - frozen_account_count` 个 extra-volume 主发送槽。blocked coverage 槽不得被其他账号的额外消息占用；额外槽只能分配给已经完成 coverage 的账号。
2. 日覆盖容量门禁、硬小时 bucket/credit/claim class 和 AI 活群活动时段门禁全部删除；静默时段只降量且小时权重必须大于 0。
3. 目标准入固定执行“实时 membership probe → 入群 → 识别可信群管提示 → 关注所需频道 → 精确确认/验证 → membership + can_send 复检”；只完成其中一步不能 ready。
4. 正常内容先由主 AI 最多生成并校验 3 轮；全部失败后切换到与主 AI 不同的备用 AI，再最多生成并校验 3 轮。每轮 Provider 解析/健康查询或调用返回不可用时，必须显式回滚该轮遗留的只读事务、记录该轮失败，再从无数据库事务的 Phase B 边界进入下一轮；不得让 `open database transaction` 把第二轮、备用 AI 或最终兜底截断。缺面具、已验证授权代理路线发生切换或主/备用六轮均无可用候选时，原发送义务强制改用精确 `签到`。兜底保留原数量槽、direct/reply、当前 attempt 的 `reply_to_message_id` 及素材义务审计，但 outbound payload 是否携带原素材必须先执行 §4.5 兼容矩阵；不兼容义务在 Gateway 前 CAS 转派，不能把纯文本签到记作素材成功。签到可计账号覆盖和群日总量，但只有真实远端消息 ID 才成功。
5. 没有任何可用授权/代理路线时写 `waiting_transport`，恢复后继续；不得直连或因选择签到伪造成功。admission backlog 只阻塞相应账号，其他 ready 账号继续。
6. `check_in_direct` 历史记录不能仅凭 source 名计入。reconcile 必须验证 task、target、account、local date、原义务、Action success、Attempt success 和非空 remote ID；已进入 Gateway/历史 success 保留真实审计，不改写结果。
7. coverage 确认数必须等于同账号/任务/日期可追溯的 distinct remote facts；不相等时显示 `coverage_reconciliation_required`。
8. 5 个 stopped 任务只进入审计清单，不自动启动。
9. 每份群日目标使用不可变 `task_day_ledger_id`，冻结 `timezone_snapshot/timezone_revision/period_start_at/deadline_at/day_phase`；账号 coverage、Action 和 Attempt 必须绑定该 ID，不能只按 `target_date` 归属。时区中途修改严格执行 §4.2.1：当前 ledger 收口后从同一 UTC 边界建立新时区过渡 ledger，禁止重叠、缺口或按当前时区重解释历史事实。
10. “所有账号”冻结范围固定为任务账号关系中同租户、未删除、`status=active`、普通运营用途且未被永久身份/安全边界排除的账号。在线、Session 暂不可用、代理、面具、membership、can_send 不作为冻结前排除条件，只形成账号 blocker；冻结后禁用、删除、用途变化、identity 失效或租户迁移也不缩小该 ledger 分母，保留 tombstone snapshot 并写 `account_scope_changed`。下一份 ledger 再按新事实建立范围。
11. 自然日中途启动的首日固定 `planning_anchor_at=running_at`；时区切换过渡日固定 `planning_anchor_at=timezone_effective_at`。两者的 `due_by_now` 都只在 `[planning_anchor_at, deadline]` 的剩余非零权重内归一：anchor 时为 0、deadline 时等于完整有效群日目标；分别标记 `partial_start/timezone_transition`、尽力完成但不纳入完整任务本地日 SLA。AI 前端可将 `partial_start` 展示为“准入预热日”，但存储/API 不使用 AI 专属枚举。只有从任务本地 00:00 开始的 ledger 标记 `full_day_committed`；暂停/恢复不得重置 anchor。

### 6.2 频道评论

1. `completion_mode=continuous`：`dynamic_new` 默认，持续监听，不存在 lifetime 自动完成。
2. `completion_mode=finite_batch`：`specific/date_range/latest_n` 使用；全部已解析消息逐条达到固化目标后才完成。
3. `max_total_comments` 不再是运营可调的完成或规划上限；创建、编辑、启动和存量接管统一写 `1_000_000`。详情只把达到该值展示为 `task_gate_limit_reached` 异常门禁，不能单独触发完成或降低逐消息目标。
   `max_comments_per_account_per_hour` 同步固定为 `1_000_000`；账号选择与发送顺序由系统按账号全局硬安全容量排序，任务内低值不得延期或吞掉评论义务。
4. `unknown_after_send` 只占防重复 hold；不得与 success 相加后触发 `completed`。
5. 正常评论先由主 AI 最多生成并校验 3 轮；全部失败后切换到不同的备用 AI，再最多生成并校验 3 轮。各轮不可用后的事务收口与 AI 活群相同：显式回滚只读轮次事务后继续，不能以事务边界错误终结原评论义务。缺面具、已验证授权代理路线发生切换或主/备用六轮均无可用候选时，原 `post_comment` Action 使用一个审核白名单中的 Unicode 表情文本兜底，写 `comment_fallback_kind=emoji_text` 和原始原因。它不是 reaction/点赞，不改变原数量 ordinal 或 direct/reply 槽位；已有内容义务先按 §4.5 兼容矩阵共载或 Gateway 前 CAS 转派，纯文本表情不能消费正常文本 emoji、图片、表情包或 custom emoji 配额。
   固定白名单为 `👍 / 🙂 / 👏`，按发送义务键稳定轮换且正文只能包含一个表情；该白名单不提供关闭开关。
6. reply 评论必须保留 `relation_kind=reply`：当前 `reply_to_message_id` 在 Gateway 前失效时终结当前 Action，并在同一 reply 槽递增 attempt、选择新的合法引用对象；不得改 direct。只有显式单目标已终态失效，或普通 reply 到 deadline 仍无任何合法替代对象时，才写 `reply_target_unrecoverable`。表情兜底仍需讨论区可用、目标准入和真实远端评论 ID；无传输路线时写 `waiting_transport`，不得伪造成功。
7. AI 质量失败、讨论区不可用和账号不可评论分别写 blocker；其他账号和消息继续。
8. 存量 `lifetime_cap_reached` 不自动复活；审计后由运营显式选择迁移为 continuous 或新 finite batch。
9. 正常 AI 评论使用发送账号当前 active 账号面具，并在 Phase A 固化 `account_mask_id/account_mask_version/mask_snapshot_hash`；不存在可用 active 面具才属于 `mask_missing`。普通文本 emoji 习惯从该快照读取；单表情兜底不进入正常 emoji 分子。历史未启用账号面具的规则版本须迁移为显式 `comment_mask_policy=required`，未进 Gateway Action 重排，历史成功和 unknown 不改写。

### 6.3 频道点赞

1. reaction 只有远端成功才增加 `confirmed_count`。
2. random 模式可在已配置 `allowed_reactions` 内选择账号实际支持的 reaction；specific 模式不得替换用户指定 reaction。
3. `REACTION_INVALID/EMPTY/NOT_AVAILABLE` 不占成功目标。该账号/消息的 attempt 保留；仍有其他合格账号或允许 reaction 时继续补欠额。
4. 所有允许 reaction 对该消息均不可用时，消息为 `reaction_capability_unavailable`，不能关闭整任务或伪造完成。
5. success 与 `unknown_after_send` 防止同账号同消息立即重复；明确失败只有在安全重试条件满足时重新选择。
6. `max_likes_per_account_per_hour` 在创建、编辑、启动和存量接管固定为 `1_000_000`；reaction unavailable、failed、skipped 不占有效账号成功额度。系统仍按账号全局硬安全容量排序。

### 6.4 频道浏览

1. 每消息每日目标和累计目标独立；任务级门禁不能替代、缩减或完成逐消息目标。
2. `task_daily_view_safety_cap` 在创建、编辑、启动和存量接管统一写 `1_000_000`，不再接受更小值；当前生产的 `500` 必须随发布直接归一。该值是异常门禁，不是日目标、逐消息目标或完成条件。
   `max_views_per_account_per_day` 同步固定为 `1_000_000`；同一账号/消息的 lifetime view 远端事实唯一性继续生效，不能用更小的任务内账号上限截断任务。
3. dynamic_new 每轮按当日 active message 欠额重算；只有真实规划量触及 `1_000_000` 才显示 `task_gate_limit_reached`，不自动关闭已完成帖子，也不缩减新增消息义务。
4. 每日消息只统计 distinct successful remote view facts；open/unknown/failed 分列。
5. 当天未达标在日切写 `missed`；次日创建新 daily ledger，累计总目标继续保留真实欠额。
6. 每消息每日浏览义务绑定 `task_day_ledger_id`，唯一键至少包含 `(task_day_ledger_id, channel_message_id)`；本地日期不作唯一身份。时区切换按 §4.2.1 创建连续 ledger，旧日成功数不搬到 transition ledger，累计总目标事实仍连续保留。

### 6.5 搜索点击

1. 合法请求直接创建 Task；不计算容量、不创建临时 ledger、不弹容量确认。Task 开始执行后，Planner 才创建真实 `task_day_ledger_id`、读取当前账号/协议/时间事实并持续规划。
2. `daily_click_target_count` 是完成目标，不是普通节奏建议。详情必须分开显示 `remaining_click_count=target-confirmed` 与扣除 held/unknown 防重复建单的 `planning_click_deficit`，在途或未知永远不能让真实欠额变成 0。`hourly_round_curve`、存量 `max_actions_per_hour/max_actions_per_day`、daily/hourly/action skip probability、jitter 和 quiet-hours 只在安全容量有余量时分散动作，不得产生永久 skip、把小时权重降为 0 或减少日目标；当剩余时间下的预测安全完成量低于剩余目标时进入 `catch_up`，按账号/关键词/Gateway 等硬安全上限允许的最大合法速率追赶。
3. 新纯搜索点击不接收 `allow_same_account_repeat_application`；该字段只作 legacy mixed task 只读兼容。合法 repeat 由系统在 click 欠额下自动选择，但不得绕过账号日限额、关键词日限额、小时冷却、授权槽位、代理和 Gateway 去重。上述安全限制、协议样本、CAPTCHA、目标精确匹配和 unknown 防重始终是硬边界。
4. Planner 先为 ledger 的真实欠额冻结稳定 `click_obligation_ordinal`，再只读枚举 `account × keyword × authorization_slot × proxy_route` 候选路径；候选路径不是可直接相加的“独立机会”。每条路径必须携带账号/关键词额度 key、授权槽位 key、代理路线 key、协议样本版本和 Gateway 容量 key。未来风险使用不写 Action/assignment/hold 的只读 `projection`；当前 Window 只有在 `allocation_state=ready` 且当前 epoch 的全任务 `DispatchClaimTaskAllocation`、search fulfillment lane 和 shard `DispatchClaimReservation` 已整批发布后，才由 `SearchClickAssignmentSolver` 在已获份额内匹配并创建 assignment/Action。Dispatcher/Gateway 共享 inflight 只由中央 Reservation 占用一次。

   每个 `(dispatch_claim_window_id, dispatch_allocation_epoch)` 唯一对应一条持久 `SearchClickAssignmentEpoch`。创建 `state=open + solver_problem_hash + solver_input_hash` 的同一事务必须绑定当前有效 `solver_owner_lease_id/solver_claimed_at`；唯一键冲突的其他 worker 只回读，不得并发求解。只有仍持有该 lease 的 owner 可执行一次搜索求解和一次成功 outcome finalize。owner lease 只作存活 fencing，健康 owner 在求解期间持续续租，固定租约时长、心跳周期或续租次数不得成为隐藏的 solver deadline；只有进程失联、fencing token 失效或明确丢失续租所有权时，recovery 才直接按 `abandoned` finalize，不转移 ownership、不重跑搜索求解，也不新增 attempt/history。每次纯搜索点击规划必须先按创建时间扫描并锁定全部 `state=open` epoch，不得只查询当前 Window；活跃 owner 只回读并跳过，失去 owner 的历史 Window 在同一事务写全量 release/exclusion/outcome 后收口，随后当前 Window 才可建立新分片权重与新 epoch。release wave 判断 Window 是否结束前必须把 PostgreSQL aware 时间和业务 naive 北京时间统一到北京时间比较，不能让时区表示差异回滚整轮 recovery。正常 finalize 的短 `SERIALIZABLE` 事务第一批业务读取必须按 Window → TaskAllocation → ShardAllocation → Reservation 锁定中央分配事实，锁后再核对 owner 并重建当前 solver 输入；先读取 owner/候选建立旧快照、再等待中央锁属于非法顺序。结果闭集为 `no_candidate|optimal|abandoned`，并保存精确 `release_unit_set_hash/outcome_hash/next_dispatch_allocation_epoch(nullable)/rebuild_input_version_after(nullable)/finalized_at`。release hash 对稳定排序的 `(window,reservation,ordinal,reason_code,resource_snapshot_hash)` 计算，空集合也保存确定性 hash，不能只存 count；outcome hash 覆盖 carrier 的 Window/dispatch/search epoch 身份、`solver_problem_hash`、`solver_input_hash`、solver result、全部 matched assignment identity/version、release hash 和实际 wave epoch/input version。已 finalized 重放只回读同一 problem/input/release/outcome/wave 结果，任一字段不一致保持 `release_fact_incomplete`。即使没有 assignment，也由该行承载首次结果和 release set 幂等；它不得承载 finalize 后 assignment 的再次释放。`optimal` 原子提交全部可验证匹配并释放 unmatched，`no_candidate|abandoned` 释放全部未领取 unit。`optimal` finalize 必须同时验证 Window 仍可领取、`allocation_state=ready` 且当前 `dispatch_allocation_epoch` 与该 search epoch 完全一致；Window 正在 rebuild、已发布更高 epoch 后重新 ready，或已经结束时都只能改为 `abandoned`。释放分别加入现有 wave、从当前 ready 版本开启下一 wave，或仅收口事实。

   当前 epoch 的 `claim_class=search_click` fulfillment Reservation 从中央 `ready` 发布到首次 search outcome finalize 前由搜索物化流程独占。通用 `unclaimed_action_no_longer_due`、无 Action Reservation 回收和普通 expiry reclaimer 必须跳过；assignment/Action 尚未创建是搜索求解前的正常状态，不是空占。Window 可领取且 epoch 行尚不存在时，首个有效 worker 创建 open 行并绑定自身 lease后执行一次求解；若 Window 已在建行前结束，recovery 在一个事务创建并直接 finalize abandoned，求解调用数为 0。任务暂停/停止/删除、due 消失或 Window 结束只使 optimal 前置失效，由该 epoch释放全部仍未领取 unit，不能另建通用 carrier。首次 outcome finalize 后，每个来源 search Reservation 必须满足 `bound_count + claimed_count + released_count = reserved_claims`；之后只有 bound assignment 进入 release batch，claimed unit继续收口。通用 reclaimer 若已触碰这些 unit，属于 `search_reservation_ownership_violation` 一致性隔离。

   `allocation_state=ready` 只允许创建新中央版本和新 search epoch/assignment，不是旧 bound Action 的 claim 门禁。同一 optimal outcome 的 unmatched release 把 Window 置为 `rebuild_required` 后，matched assignment 只要来源 Reservation、assignment、搜索资源与 Action version 有效，且 Window/业务 deadline 未结束，就继续按来源 epoch `_confirm_claim -> Gateway`，不得读取未发布新权重或因状态不再 ready 被卡死。Window 已结束、Action 不再到期或 Gateway 前资格失效时才走稳定 release batch。未绑定的新 epoch份额仍须等待新权重与 `ready` 原子发布。

   每个释放事实精确绑定 `(dispatch_claim_reservation_id, fulfillment_lane_claim_ordinal)`；同一 finalize 事务插入全部 `DispatchAllocationExclusion`、按 Reservation 汇总更新 `released_count`、按 Task/shard/Window 汇总扣减 unclaimed。集合为空时中央状态不变。集合非空且 Window 尚可领取时：`ready` 只递增一次 `dispatch_allocation_epoch`、置 `rebuild_required` 并增加 `rebuild_input_version`；已为 `rebuild_required` 时复用当前 pending epoch，只增加 `rebuild_input_version`。Window 已结束时只收口事实，不新建无用途 epoch。已 bound/claimed/active 份额、其他有效旧 Reservation 和公平 cursor 不回退，业务 click 欠额不减少。finalized epoch、后续 release batch、release batch item、exclusion 与来源 Reservation 在迟到 writer 仍可访问期间必须共同保留；联合归档前先 fence 旧 worker，不能通过清理逐 unit 分类或历史重新获得唯一键。

   `projected_eligible_attempt_capacity_before_deadline` 只按业务 `scheduled_end` 标记未预留的尝试上界，`committed_click_opportunity_count` 才表示当前 Window 已取得的机会；二者都不是预测确认数。CAPTCHA 不使用触发率或 AI 历史成功率预测：尚未进入验证页只能计 eligible attempt；实际进入验证页后，只有同一 `challenge_fingerprint_hash` 的单次批准答案提交取得明确远端通过回执或已审批搜索分类/结果页，才写 `jisou_image_verification_solved` 并继续；仅离开原页、新 fingerprint、hot-list、unknown、`required|failed` 及 required 下的 unavailable/unknown 原因均对可继续 click opportunity 贡献 0。验证码状态闭集只有 `required|solved|failed`，不新增 unknown 状态。验证码 AI 调用及批准重试不占账号/关键词 click 限额、任务 click 目标或额外 Dispatcher/Gateway 份额，也不进入 AI 活群/评论的主/备用 AI 生成轮次或业务 AI 生成次数。assignment 不计算 `latest_safe_start_at`，不引入协议/Dispatcher/Gateway 性能预算或求解器技术 deadline；同一最大匹配按 `hard_safe_remaining_capacity DESC -> confirmed_click_count_today ASC -> last_click_opportunity_at ASC -> persistent_account_cursor ASC` 稳定决胜。运营不能配置容量、匹配或顺序。软排程无法完成真实欠额时立即把 `due_click_target_count` 提到完整日目标并 catch-up。未命中 source 明确失败后可以为同一 ordinal 建立 replacement Action/Attempt；open/unknown 继续占用同一 ordinal。系统持续尝试直到真实 `target_click_observed` 达标或当前所有硬安全路径暂不可用，不以行为节奏 skip 结束当日。

   `SearchClickAssignmentEpoch` 至少固化：

   ```text
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
   ```

   `solver_problem_hash` 与 `solver_input_hash` 不得合并。前者是 carrier-independent 的业务问题图 hash：包含 `solver_contract_version`、稳定业务义务键、各连通分量候选路径、相关资源 key/value/version 和真正参与该分量公平目标的 due/remaining/cursor，排除 Window/dispatch/search epoch、TaskAllocation/Reservation/ordinal/assignment ID、carrier 派生份额、worker/lease、时间和随机值。后者在 problem hash 外加入本次 carrier epoch、精确 Reservation unit 集及中央份额/Reservation 版本，负责当前 outcome 幂等。字段集合或排序规则变化必须提升 `solver_contract_version`，版本进入两个 payload但不新增独立状态列。

   所有会影响候选、约束、目标或 tie-break 的读取都必须入 hash，不能把排序字段当展示摘要。最低包括账号 `hard_safe_remaining_capacity`、同一冻结 `account_quota_key/capacity_window_key` 内的 `confirmed_click_count_today`、持久化 `last_click_opportunity_at`、`persistent_account_cursor` 及各自 source key/value/version；这里的 `today` 不读取服务器日期或提交时墙钟。新增影响输出的读取必须进入 canonical payload并提升 `solver_contract_version`。

   两个 hash 及其分量/unit 映射只能由唯一 `SearchSolverSnapshotAssembler` 产生。Assembler 在一个一致性数据库快照中建立不可变 `SearchSolverProblemSnapshot`、全部 `SearchSolverProblemComponent(stable_component_key,canonical_nodes_edges_fairness,solver_problem_component_hash)` 和每个 `(reservation_id,ordinal)` 唯一的 `SearchSolverCarrierUnitBinding`；共享资源 key 或 `assignment_fairness_key` 必须合并到同一分量，无候选 unit 也保存实际 eligibility/resource 版本的零边分量。open epoch、完整 snapshot/component/binding、两个 hash 与 owner lease 同事务原子落库后才允许调用 solver。solver 只读该持久快照，禁止额外查库/读全局；owner 丢失 recovery 直接用原 binding/component hash 释放，不能重新组图。active exclusion 的 current component hash 也只能由同一 Assembler/canonicalization 重算，禁止两套 hash 逻辑。无法形成完整一致快照时不调用 solver、不保留半条 open epoch或部分 payload，显式失败或对象级 quarantine，不能冒充 `no_candidate|optimal`。

   `stable_component_key` 由 contract version 与稳定排序后的业务义务、候选 edge、资源 node、fairness node 身份确定，不能用随机 ID，也不包含当前值、carrier、worker或时间；component hash 再覆盖该 key 及全部 canonical 当前值/version。分量拆分/合并必须改变受影响 key/hash，只有值变化时 key 保持而 hash 改变。每个影响 solver 输出的读取必须能由 snapshot payload/source version 反向枚举，禁止隐式默认值、缓存或进程全局输入。

   正常 `no_candidate|optimal` finalize 前，必须在短 PostgreSQL `SERIALIZABLE` 事务内以相同候选谓词/source key运行同一 Assembler 的只读 revalidation，重算 problem/input hash并逐项比较全部输出影响 version，且不得覆盖原 snapshot；通过后才按统一锁序做 Window/Reservation/assignment/Action/resource CAS并提交。候选 phantom、额度、账号已确认 click 数、机会时间、cursor、eligibility、中央份额或任何版本漂移都使原 epoch 整轮 `abandoned`，禁止提交旧解，并按原 binding 释放全部仍未领取 unit后加入唯一 rebuild wave。SQLSTATE `40001` 无论发生在锁定、写入或 commit，旧事务回滚后都必须在新事务按原 binding 直接 abandoned/release/rebuild，禁止驱动重放旧解或在同一 epoch 重跑 solver；数据库不可写导致该新事务无法提交时显式失败并保持 open，由 owner-loss recovery 直接以原 binding/hash 收口。

   唯一键为 `(dispatch_claim_window_id, dispatch_allocation_epoch)`。`open -> finalized` 只允许一次 CAS；已 finalized 的重放只回读相同 outcome，不能再次释放、递增 dispatch epoch 或创建 assignment。

   `DispatchAllocationReleaseBatch` 至少固化：

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

   每个候选 unit 另建不可变 `DispatchAllocationReleaseBatchItem(release_batch_id,reservation_id,fulfillment_lane_claim_ordinal,search_click_assignment_id,expected_assignment_version,bound_action_id,expected_action_version,classification,observed_assignment_state,observed_assignment_version,observed_action_state,observed_action_version,satisfied_by_release_carrier_type/id)`，唯一键为 `(release_batch_id,reservation_id,ordinal)`。classification 只允许 `effective_released|already_released|precondition_lost`；candidate hash 由全部稳定 item 输入、expected assignment version 与 nullable expected Action version 计算，release hash 只由 effective item 计算。already-released item 必须引用首个 exclusion carrier，precondition-lost item 必须保存锁内 assignment/Action 状态与版本证据。batch `outcome_hash` 统一覆盖 carrier 的 Window/source epoch/trigger 身份、candidate hash、按稳定 unit key 排序的全部 item 分类及 expected/observed assignment/Action version、首 carrier、release hash、三类 count、outcome 与实际 next epoch/input version。batch、全部 item、Action/assignment 状态、计数/exclusion、outcome hash 与 wave 同事务提交，不能只存汇总计数后依赖日志猜测。

   item 与 batch 汇总必须精确守恒：`candidate_unit_count = release_unit_count + already_released_unit_count + precondition_lost_unit_count`。`release_unit_count > 0` 且两个 no-op 计数均为 0 才是 `applied`，`release_unit_count = 0` 才是 `no_op`，`release_unit_count > 0` 且至少一个 no-op 计数大于 0 才是 `mixed`；空 candidate 也是显式 no-op。任一 count/hash/outcome 无法从 item 唯一重算时不得 finalize。finalized 重放只有 candidate、全部 item 结果、release/count/outcome、wave 与重算 outcome hash 完全一致时才零写回读；同 trigger 同 candidate 但结果错绑保持 `release_fact_incomplete`，candidate 不同才返回 `release_batch_input_conflict`。

   唯一键为 `(dispatch_claim_window_id, release_trigger_type, release_trigger_key)`。trigger 必须来自不可变事实及版本，例如 assignment/version 的 Gateway 前终态、Action/version 不再到期或 Window/source epoch 到期；禁止随机 batch id、worker id 或扫描时间。`candidate_unit_set_hash` 是不可变 trigger 派生的完整候选集合输入身份；`release_unit_set_hash` 是取得统一锁序后仍可实际释放的 effective 集合结果。

   batch 只服务 search epoch finalized 后的释放，并在锁内按 `(reservation_id,ordinal,assignment_id)` 稳定顺序逐 unit、再按稳定 Action ID 分类：trigger 指定版本仍为 `reserved|action_bound`、无任何状态 exclusion，且 nullable bound Action 不存在或仍为 expected version 的 pre-Gateway 状态时进入 effective set；`action_bound` 的 Action 必须已是匹配 trigger 的 pre-Gateway terminal，或由本事务转为对应 `failed|skipped` 终态。assignment 已 `released`、永久 exclusion 已存在且原 Action 已不可领取时记 `already_released`、回读首个 carrier且不改计数；assignment 已 `claimed|gateway_started|unknown|consumed`，或 assignment/Action 版本变化、Action 已 executing/Gateway-started 时记 `precondition_lost`、禁止释放或改写 Action。assignment/exclusion 已释放但 Action 仍可领取属于一致性矛盾。不同 trigger 同时命中同一 unit 时，只有第一个有效 carrier释放，后到 trigger 以 no-op 收口；release 与 `_confirm_claim` 竞争时也只能一方成功。候选分类、effective set、bound Action 终态/lease/active、assignment、exclusion、bound/released/unclaimed 计数与 rebuild wave 在一个事务直接 finalize；保留 assignment/Action 绑定作证据，提交后禁止 `assignment=released + Action pending|claiming`。effective set 为空仍持久化 no-op batch，但不推动 rebuild。失败全部回滚；同 trigger 同 candidate hash 重放只回读，不同 candidate hash 整批报 `release_batch_input_conflict`，`applied|mixed` 的全部 effective unit 禁止事务级部分释放。

   `precondition_lost` 仅终结旧 expected version 的 trigger。状态机禁止从 claim/Gateway/unknown/consumed 倒退到 `reserved|action_bound`；observed 已越过该边界时永不再生成释放。只有 observed 仍是新的 `reserved|action_bound` pre-Gateway 版本（如并发 replacement/资格复核仅推进 assignment 或 Action version），且原释放条件对新版本仍成立，产生新版本的状态变更事务或 outbox 才按新 assignment/Action version 生成全新 trigger key和 candidate hash。禁止重开旧 batch，也禁止没有版本变化事件的轮询重试；Gateway 前新版本的可释放占用不能因旧 no-op 永久泄漏。

   assignment、bound Action、exclusion、claim/Gateway 与计数互相矛盾而无法分类时，release 事务必须先整批回滚；quarantine 不能写在随后回滚的同一事务里。独立 consistency writer 重新取得相同中央锁并复核，矛盾仍存在才以 `(window,reservation,ordinal,issue_fingerprint)` 幂等保存 active `consistency_quarantine`、全部 observed state/version 和原 trigger。该 trigger 在 issue resolved 前不做定时重试，包含该 unit 的 batch 暂停，其他独立任务/ordinal/trigger 继续。

   Reconciler 分支前先验证合法 release fact set：首次 outcome 必须同时存在 finalized search epoch、其 `release_unit_set_hash` 内的 unit 和 matching exclusion；post-finalize 必须同时存在 finalized release batch、`effective_released` matching item 和 matching exclusion，且 carrier/unit/hash/reason/version/计数一致。只有 carrier、只有 exclusion、缺 item 或错绑时保持 `release_fact_incomplete` 对象级 quarantine，不能自动判 released。完整事实只允许四个互斥分支：①合法 release fact set 且无 claim/Gateway，以逐 unit 事实为权威；存在 assignment 时对齐为 released，首次 outcome 的未绑定 unit 保持无 assignment；终结仍可领取的 bound Action并清 lease/active，再重算各层摘要，使该 unit 只贡献一次 released；②只有 released assignment、无任何 release 组件且无 claim/Gateway，按 Action 绑定恢复 `reserved|action_bound`、递增 version并产生新 trigger；③有 claim/Gateway且无任何 release 组件，不回滚远端边界，只把 assignment/Reservation 对齐到 `claimed|gateway_started|unknown|consumed`；④合法 release fact set 与 claim/Gateway 同时存在时写 `release_claim_fact_conflict` 并保持该 unit active quarantine，自动 reconciler 不得删除 release 组件、回滚 Gateway、选边或调整该 unit 的 released/claimed 计数，也不得 resolve 或忙重试。前三个分支提交后才 resolve并唤醒原 trigger；第四分支仅隔离该 unit，完整 click evidence仍按真实事实入账，但相关 ledger 在 quarantine 清除前不得通过 E4。对象级隔离不扩展结构硬阻塞闭集，也不形成忙循环；任何分支都不重跑搜索求解。

   `SearchClickOpportunityAssignment` 是搜索专项内部规划事实，不是运营配置、通用 quantity slot 或远端成功事实，至少固化：

   ```text
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
   protocol_sample_version / gateway_capacity_key / gateway_capacity_version
   assigned_action_id
   state = reserved | action_bound | claimed | gateway_started | unknown | consumed | released
   ```

   同一 click 义务同时最多一条非 `released` assignment；同一 `(dispatch_claim_reservation_id,fulfillment_lane_claim_ordinal)` 也同时最多一条非 `released` assignment，且 claim ordinal 必须位于 `1..reserved_claims`。assignment 绑定时增加 Reservation `bound_count`，claim 时从 bound 转 claimed，放弃时转 released；`bound_count + claimed_count + released_count <= reserved_claims`。只有 commit 模式可以在同一短事务按稳定资源 key 顺序 CAS 搜索专属 `consumptive` 子预留和 `eligibility` 版本，并固化资源时间窗口。Dispatcher/Gateway 及全任务共享代理的 `inflight` 必须复用已经授予的中央 `DispatchClaimReservation`，assignment 只保存其 ID/key/version，不得建立第二份在途预留。projection 先扣除现有中央份额/已提交事实后只读求解，不写 hold。所有 running 搜索任务共享账号/关键词等搜索资源账本，不得由各 Task 分别投影一次。

   `SearchClickAssignmentSolver` 使用纯 click 的多阶段字典序目标：先最大化当前 Window 可提交 assignment 总数；在不减少总数的解中，最大化获得至少 1 条 assignment 的到期父任务数，并按业务 `scheduled_end`、最久未获机会和持久 task cursor 对无法同时覆盖的任务稳定决胜；随后按冻结 `remaining_click_count` 做最大最小任务公平，避免剩余机会永远集中到同一 Task；最后严格按账号 `hard_safe_remaining_capacity DESC -> confirmed_click_count_today ASC -> last_click_opportunity_at ASC -> persistent_account_cursor ASC` 决胜。commit 还必须加入 `sum(x_task) <= fulfillment_lane_claims(task)` 与 shard Reservation 上限，不能绕过中央全任务公平份额。每个 Task 建立 `assignment_fairness_key=(allocation_business_task_id,task_day_ledger_id,target_id)`；纯搜索点击不建立 admission distinct/budget 目标。只有尚未 `_confirm_claim` 的 `reserved|action_bound` assignment 使用 `assignment_expires_at <= Claim Window.bucket_end`。搜索 epoch 首次求解/绑定失败与 unmatched 由该 epoch outcome 释放；epoch finalized 后的 Gateway 前路径失效、Action 不再到期或 assignment 过期改由唯一 release batch 释放，不能重开原 epoch。释放 bound unit 时同一事务执行 assignment `reserved|action_bound -> released`、Reservation `bound_count -= 1/released_count += 1`、各层 unclaimed 减 1 并写永久 exclusion；只释放搜索子预留不算完成。Window 结束时用稳定 expiry batch 收口，但不为已结束 Window创建 epoch；下一 Window从真实 click 欠额重新分配。`_confirm_claim` 同一 CAS 把 Reservation 从 bound 转 claimed、Action 转 executing、assignment 转 `claimed`，此后 Window 结束不得自动释放。Gateway 调用结束后释放中央 inflight，unknown 只继续占用原 click ordinal 和可能已经消费的 `consumptive` quota hold，直至远端核验或对应安全额度窗口结束；不得无限占用 Dispatcher/Gateway/代理在途容量。`eligibility` 在 claim 与 Gateway 前重新复核，不作为永久配额。

   跨 Task 公平只对当前 `search_click_assignment_epoch` 至少有一条真实 eligibility 路径的 due Task 求解；无路径 Task 继续显示缺失资源，不能把其他可执行 Task 的总 assignment 降为 0。冻结 `remaining_click_count` 后定义 `task_fairness_ratio=assigned_count/max(remaining_click_count,1)`，对从小到大排序后的 ratio 向量做字典序最大化；离散余数按业务 `scheduled_end`、最久未获机会和持久 task cursor 决胜，不使用不可解释加权总分。每个 due Task 都要保存 `task_assignment_count` 与 `task_unmatched_reason=no_eligibility|resource_saturated|fairness_deferred|null`。

   每个 `search_click_assignment_epoch` 必须绑定当前 `dispatch_allocation_epoch`，先冻结候选路径、资源版本与 `solver_input_hash`，再把共享同一 click ordinal、任一资源 key 或同一 `assignment_fairness_key` 的候选拆成互不共享约束/目标的连通分量。一个 epoch 只求解一次。每个分量以 `x[ordinal,path] ∈ {0,1}` 求解，并为到期 Task 建立 `z[task] ∈ {0,1}`：固定约束为“每个 ordinal 最多一条路径”“每个资源窗口的已占用量 + 本批 usage 不超过 available”“`z` 只能由该 Task 至少一条已选路径激活”。目标依次固定最优值：`sum(x)` -> `sum(z)` -> 按 remaining click 比例的最大最小公平向量 -> 稳定 path tie-break；任一后续阶段不得降低前一阶段最优值。`assignment_fairness_key` 把跨 ordinal 的任务公平目标连接进同一分量，因此各分量最优向量之和才是全局字典序最优。只允许使用能证明全部阶段最优的确定性约束求解、等价精确算法或可验证最优证书；禁止以首个可行贪心、账号顺序截断、候选笛卡尔积或不可验证的部分结果冒充容量。求解结果固定为：

   - `no_candidate`：整个当前冻结快照没有任何 eligibility 路径；不调用求解器，在 epoch 行保存结果，把全部未领取 Reservation unit 作为一个集合原子释放，按各 Task 缺失/失效资源投影运行 blocker；集合非空时加入当前唯一 rebuild wave，集合为空时中央版本不变；
   - `optimal`：在 epoch 行记录 `matched_count/served_due_task_count/task_assignment_counts/task_unmatched_reasons/task_fairness_vector_hash/unmatched_ordinal_count/saturated_resource_keys`；同一 finalize 事务先验证全部 matched 绑定，再提交全部已证明 assignment并释放 unmatched set。任一 matched 前置条件失效时不进入 optimal 写入，直接改为 `abandoned` 并释放全部仍未领取 unit；SQLSTATE `40001` 回滚旧事务后改由新事务直接 abandoned/release/rebuild，只有数据库不可写使新事务也无法提交时才保持 open并显式报错，不留下部分 assignment；
   - `abandoned`：求解器无法返回完整可验证结果、reservation/resource CAS 无法绑定、finalize 时 Window 已进入 rebuild wave/已发布更高 dispatch epoch/已经结束，或恢复到遗留 open epoch；不提交 incumbent/部分解，在 epoch 行记录 `search_assignment_abandoned`，把全部未领取 unit 作为一个集合原子释放；尚可领取 Window 的非空集合开启或加入当前唯一 rebuild wave，集合为空只 finalize 结果，已结束 Window 只收口事实，不在相同输入上重试。

   `DispatchAllocationExclusion` 至少保存：

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

   每条 exclusion 固定 `release_count=1`，carrier 只能指向首次 search outcome 或后续 release batch。永久唯一键为 `(dispatch_claim_window_id, source_dispatch_claim_reservation_id, source_fulfillment_lane_claim_ordinal)`；`resource_snapshot_hash` 只作为释放证据及 active exclusion 的权重适用性字段，不参与幂等键。该 hash 按 `reason_code` 只固化与本 unit 失败直接相关的 Window、Task/ledger/target、assignment/Action expected version，以及账号/关键词额度窗口、授权、代理、协议/CAPTCHA、Gateway 容量等资源 key/version；禁止放入无关 Task/shard、worker/lease、扫描时间或随机值。`no_feasible_search_path|search_solver_abandoned` 必须从 carrier-independent `solver_problem_hash` 投影本 unit 所在连通分量的 `solver_problem_component_hash` 作为 resource hash；完整 `solver_input_hash` 禁止参与 supersede 判断。只有该分量的稳定业务义务、候选、资源、公平输入或 contract version 变化才 supersede；单纯新建 dispatch/search epoch、换 Reservation/ordinal/worker 或改变 carrier 派生份额必须保持 active，避免同问题循环获配。记录转为 `superseded|expired` 后，同一旧 unit 仍不得新增第二条 exclusion、再次增加 `released_count` 或恢复 claim；新事实只能获得新 epoch 的新 Reservation/ordinal。finalized carrier、release batch item、exclusion 与来源 Reservation 在迟到 writer 仍可访问时不得单独物理删除；联合归档必须先 fence 旧 worker，只能冷存 payload，主库永久保留不可删除/复用的 carrier key/hash、batch item candidate unit、assignment/Action expected+observed version、classification/first-carrier 引用与 `(window,reservation,ordinal,released)` identity tombstone。这是运行幂等身份，不是 TaskStartOperation 之类的运营历史。`reason_code` 只允许 `no_feasible_search_path|search_resource_saturated|protocol_ineligible_for_snapshot|search_solver_abandoned|search_reservation_cas_abandoned|search_assignment_pre_gateway_terminal|search_assignment_expired|unclaimed_action_no_longer_due`。

   首次 search outcome 与后续 release batch 共用 finalize helper，事务按 `Scope -> Window -> TaskAllocation -> ShardAllocation -> Reservation` 加锁，同层多行按主键稳定排序，再锁 carrier。`optimal` 必须在任何 assignment 写入前验证 matched/release ordinals 互斥、全部 matched 绑定、release set 前置条件，以及 Window 仍可领取、`allocation_state=ready`、当前 `dispatch_allocation_epoch` 与 search epoch 完全一致；任一条件失效时改为 `abandoned`，不写部分 assignment。即使 Window 已从一次重建回到 `ready`，epoch 不一致也仍是过期结果。所有 ordinals 必须在 `1..reserved_claims`。首次 outcome 的 unit 不得被 assignment、claim、active 事实或任意状态 exclusion 占用，并守卫 `bound + new_matched + claimed + released + unbound_release <= reserved`。后续 batch 先做 `effective|already_released|precondition_lost` 分类，只有 effective set 进入 `bound >= bound_release` 守卫，并以 `bound -= bound_release/released += bound_release` 和 assignment `reserved|action_bound -> released` 原子转移；另外两类不改计数、不新增 exclusion。各层 unclaimed 只需且必须覆盖本次 effective release count。

   非空 effective release set 在尚可领取 Window 中按状态更新：`ready` 时 epoch 只加 1、置 `rebuild_required` 并递增 `rebuild_input_version`；已经 `rebuild_required` 时只递增 `rebuild_input_version` 并复用当前 pending epoch；Window 已结束时不改变 epoch/state。effective 集合为空时 batch 仍可 no-op finalize，但不改 Window。任一步失败整批回滚，search epoch 仍 open或 release batch 不存在；只有 assignment/exclusion/计数真实不一致才由 `DispatchReservationReconciler` 按事实重算后继续原 carrier。合法 `already_released|precondition_lost` 不重试、不 reconcile，也不重跑求解。重放只允许确认相同 carrier/outcome/hash，不能部分释放、双扣或在同一 wave 再增 epoch。

   `DispatchLaneShardSolver` 的无事务重建快照必须冻结 `dispatch_allocation_epoch + rebuild_input_version + dispatch_rebuild_snapshot_hash`。该 hash 的规范化 payload 固定包含 `(window,pending_epoch,rebuild_input_version)`、全部 task/lane/shard 的 due/eligibility 稳定键/当前值/版本、全部 active exclusion 的 unit/state/reason/resource snapshot、全部仍有效旧 Reservation 的身份/承诺计数/版本，以及 scope/shard 容量与影响分配结果的配置值/版本；稳定排序后取 hash，worker/lease、扫描或墙钟时间、进程身份和随机值不得进入。提交前在中央锁序内重新读取同一输入并重算 hash；epoch、input version 或 hash 任一变化都整批丢弃旧权重，即使没有 release batch 推进 input version。成功提交时，Window 的 `ready_rebuild_snapshot_hash` 与本 epoch 全部 TaskAllocation/ShardAllocation/Reservation 的 `dispatch_rebuild_snapshot_hash` 必须相同，并与 `ready` 原子发布；零余额也发布带 hash 的空 `ready`。其他 shard/资源路径仍可获配。只有 `reason_code` 直接依赖的规范化资源 snapshot 改变时旧项才转 `superseded`，无关 Task/shard、worker/lease 或观察时间变化保持 active；Window 结束统一 `expired`。旧 unit 始终保持 released，新事实只能用新 Reservation/ordinal，不得跨 Window、跨任务日或跨目标复用，也不得减少业务欠额。

   本条的 hash 输入同样遵守完整性规则：最低集合之外，还必须包含 `dispatch_rebuild_contract_version`、Scope/Window/Shard active/unclaimed 与版本、全部 fairness cursor 与版本、parent/sponsor 聚合输入；凡 solver 读取且会影响输出的业务字段均进入规范化 payload。任何漏项都属于结构错误，禁止以“epoch/input version 没变”提交。

   候选生成只枚举真实存在且已通过 eligibility 的账号—授权槽—当前代理绑定—关键词路径，并按完整资源向量去重；不得先构造全部理论笛卡尔积再依靠固定 top-N 截断，否则可能把实际可行路径排除并违反完成优先。

   本 PRD 不设置求解器技术 deadline、性能预算、图规模基线、p99 指标，也不为这些指标设计 retry 或降级路径。实现可选择等价的数据结构，但不得抽样、batch 截断、固定 top-N 或提交不可验证的部分解；当前 epoch 无法一次完成时按 `abandoned` 释放全部未领取 unit。尚可领取 Window 的非空 release set 加入唯一 pending rebuild wave；已结束 Window 只收口事实，由下一 Window 重新分配。
5. 每个协议版本先通过受控 canary；图片验证码继续使用已批准流程。`hot_list_page`、`verification_image_page`、`search_category_page`、`group_result_page`、`unknown_page` 分开记录；`hot_list_page` 直接失败并排除当前账号—协议路径 12 小时，禁止通过 reset、未知按钮或外链恢复。
6. 只有完整 click evidence 才写 `target_click_observed`；证据必须包含已审批 `click_effect`、`membership_side_effect=none` 和 `membership_mutating_rpc_invoked=false`，且 group result profile 至少有一个 `target_open_only`，只有 `navigate_only` 不能获配目标点击。任意旧 `join_candidate`、成员副作用未知或任何 join/request/follow/confirm/can-send RPC 默认不得进入纯 click eligibility；唯一例外是旧解析器精确版本 `jisou-v2-2026-07-28` 产生的 Telegram 内部 URL 误分类，发布接管须保留旧样本为 inactive 历史并创建审计化 `jisou-click-only-v3-2026-07-29` replacement，将目标 URL 重分类为 `target_open_only` 且成员副作用固定为 `none`。版本、结构或 effect 集合不精确匹配时仍禁止自动迁移。确认后该 ordinal 结束，不创建 membership/admission/can-send 子 Action。
7. Planner 先为 source 义务冻结不可变 `task_day_ledger_id`；当前 Window commit 创建 executable source Action 时继承该 ledger，并冻结 `obligation_local_date/timezone_snapshot/timezone_revision/period_start_at/deadline_at`。Gateway 不得在该 ledger 的 deadline 后开始。
8. “搜索点击加入”只登记为后续独立任务模式，`design_status=not_started`；本 PRD 不定义其字段、准入流程或 QA。当前未结束的存量混合 click+membership Task 在接管时转为 `task_type=search_click + search_execution_mode=click_only`：以后只履约原 click 目标，移除运行配置中的 join/admission 目标，终结未进入 Gateway 的 membership child；既有 Task 审计、Action、Attempt、unknown、membership 与远端事实保持只读历史，不改绑、不删除、不计 click 成功。
9. 运行中五类任务切换采用明确的合同分界：已经绑定新类型义务的 Action 原位续跑；未绑定新义务且未进入 Gateway 的旧 AI `send_message`、旧 search source 与 membership child 显式 `skipped + legacy_action_retired_by_fulfillment_takeover`，释放 claim/lease 后由新 ledger/义务重新规划。Gateway 已开始、success、unknown 的旧 AI/混合搜索 Action 保留历史，不猜测映射到新合同，也不计新合同完成；因此新 AI ContentMix 仍按完整新目标冻结引用/图片/表情占比，纯 click 仍只认满足无成员副作用合同的完整证据。评论、点赞、浏览因存在稳定天然义务键，可证明 remote ID/state/source 的旧成功回填类型专用事实，开放 Action 绑定原义务；证据缺失的旧成功进入 unknown，不伪造成 confirmed。

## 7. 前端、接口与权限

### 7.1 创建校验与运行评估

创建接口只返回结构校验结果和已持久化 Task；不调用容量预检、不要求 warning 确认，也不在创建前生成 `task_day_ledger_id`：

inline 公开目标只能做本地语法、规范化 username 和同用户唯一性处理；允许与 Task 在事务 A 内 upsert `resolution_state=pending` 的目标引用，但不得调用 Telegram resolve、membership/capability probe。远端解析失败在启动后形成目标 scope 运行态，不能回滚已创建 Task。

纯搜索点击的正式入口固定为 `POST /api/tasks/search-click` 与 `/api/tasks/search-click/create-and-start`，权限固定为 `tasks.manage + tasks.create.search_click`，持久化 `task_type=search_click + search_execution_mode=click_only`。旧 `search-join-group` 路由、任务类型和权限只用于兼容读取或迁移识别；旧创建请求固定返回 `410 legacy_search_join_create_retired`，不能代建 `search_click`，也不能成为新前端、公开接口、日志业务名或新建授权。

```text
structural_config_errors
task_id
task_status
create_status = created | existing_idempotent
start_status = not_requested | started | start_failed
start_failure_code
runtime_state = not_started | runnable | waiting
runtime_blocker_codes
request_fingerprint
start_operation_id
start_operation_version
start_operation_legacy_untracked
```

所有创建接口必须接收稳定 `client_request_id`/幂等键，并以“当前用户 + 任务类型 + client_request_id”唯一。后端对规范化后的任务类型、目标/账号引用、数量/内容/开关配置和 `start_requested` 生成 `request_fingerprint`；字段顺序、空白和等价默认值不影响 fingerprint，`client_request_id`、服务端时间和运行期事实不进入 fingerprint。事务 A 必须将 fingerprint 与 Task/创建审计一起提交：

- 同一幂等键且 fingerprint 相同：返回原 Task；若原请求包含 start，只回读或继续同一 start operation，不创建第二条 Task。
- 同一幂等键但 fingerprint 不同：返回 `409 idempotency_key_reused`，同时给出原 `task_id` 和不含敏感值的冲突字段名；不得静默返回旧配置，也不得覆盖旧 Task。
- 结构冲突返回 422 且不创建 Task；首次成功创建返回 201，幂等重放返回 200。

Task 必须持久化 `created_by_user_id/create_task_type/client_request_id/request_fingerprint`，并在包含 soft-deleted Task 的全表范围保持 `(created_by_user_id, create_task_type, client_request_id)` 唯一，删除任务不能释放幂等键。每个 Task 允许 0 或 1 条当前 `TaskStartOperation`，有行时以 `task_id` 唯一；新合同生效后的真实 start/create-and-start 必须建立该当前行，不建立启动 attempt 表或历史版本。启动失败后的显式重试可复用原 key，也可用新 key 覆盖该当前行，停止后的明确重新启动同样覆盖当前行：

```text
task_id / start_operation_id / operation_version / requested_by_user_id
source = create_and_start | explicit_start
status = processing | started | failed
task_day_ledger_id / start_failure_code
requested_at / updated_at
```

`TaskStartOperation.status` 只描述“启动事务有没有把 Task 成功推进到 running”，不得承载随资源变化而变化的运行态；其状态闭集为 `processing|started|failed`。上方响应中的 `start_status` 同样只允许 `not_requested|started|start_failed`，账号、准入、传输、容量或协议暂不可用必须通过独立 `runtime_state=waiting` 与 `runtime_blocker_codes` 返回。`runtime_state` 后续可变，已经 `started` 的 operation 不得随之回退。

发布前已经处于 running/paused 的存量 Task 不回填虚构 start operation，也不从旧日志拼装历史：重复 start 或同一 ledger 内 resume 直接按现有 Task 与当前 ledger 返回 `start_status=started`，并返回 `start_operation_id=null/start_operation_version=null/start_operation_legacy_untracked=true`，不得再次运行启动事务。`resume` 不是新 start，不创建或覆盖 operation，只恢复原 ledger。该 Task 后续被明确 stopped 后首次真实重启时，才创建 version 1 的当前 operation 并把 legacy 标记置 false。存量 draft/stopped Task 同样不补历史行，首次真实启动时创建 version 1。此兼容只允许“零当前行”，不能绕过新启动的唯一行、Task 锁或 ledger 幂等。

零当前行的响应不能靠 null 猜测：新合同下仅创建未启动的 draft 返回 `not_requested/null/null/legacy_untracked=false`；发布前存量 running/paused 返回 `started/null/null/true`；发布前存量 draft/stopped 返回 `not_requested/null/null/true`。任一真实 start 成功或失败落账后都必须有非空 ID/version 且 legacy 标记为 false。

`task_id` 是唯一键，`start_operation_id` 是当前请求身份而不是历史主键；`operation_version` 是当前行单调递增的并发栅栏，不保存旧版本 payload，也不构成启动历史。事务 B 先锁 Task 行，再创建/锁定该唯一 operation，并与 Task/ledger 一起提交；同一 Task 的并发 start 因 Task 行锁串行化。相同 key 把 `failed` 原行覆盖为 `processing` 时也必须执行 `operation_version += 1`。使用新 key 重试失败启动或重启 stopped Task 时，请求必须同时提交 `replaces_start_operation_id + replaces_start_operation_version`，并在 Task 锁内 CAS 等于当前行的 ID/version 后，才把该行的 identity/source/requested_by/status/ledger/failure 整体覆盖并把 version 加 1。

若 B 回滚，ledger/Cycle/assignment 和本次 version 递增均不存在。独立失败落账事务必须重新按 Task -> StartOperation 加锁，并以 B 开始时冻结的 `(expected_previous_start_operation_id, expected_previous_start_operation_version)` 做 CAS；仅当 Task 仍为 `draft|stopped` 且 current ID/version 仍等于 expected（首次启动允许 current row 不存在，约定 expected version=0）时，才插入/覆盖为本请求的 `failed/start_failure_code`，并写入本次 `operation_version=expected+1`。若当前 tuple 已变化或 Task 已 running，失败落账只返回最新 current operation，绝不能覆盖新的 `processing|started`。因此旧 failure writer 即使与新的同 key 重试使用相同 `start_operation_id`，也会因 version 已推进而失败。请求侧 replace tuple CAS 不等返回 `409 stale_start_operation` 并返回最新 current ID/version；当前行是其他请求的 `processing` 时返回 `409 start_in_progress`，不得抢占。成功后只保留 `started` 并清空 failure。若 Task 已 running 且当前 operation 已 `started`，任何 same/new key 重放都只回读当前 started/ledger，不能再次启动。两个请求并发覆盖同一 `draft|stopped|failed` Task 时，只有先取得 Task 行锁并推进 version 的请求能执行；后请求回读最新状态或 tuple CAS 失败，始终不写第二行、失败历史或第二份 ledger。

普通创建成功后由运营启动；`create-and-start` 必须先在事务 A 提交 Task，再在事务 B 进入与 `POST /tasks/{id}/start` 相同的启动流程。事务 B 使用唯一 `start_operation_id`；create-and-start 固定由 `client_request_id` 派生，普通 start 请求必须为本次启动意图自带稳定幂等键。启动失败后可复用原键并推进当前 `operation_version`，也可由下一次显式重试提交 `new start_operation_id + replaces_start_operation_id/version=current` 覆盖唯一当前行；两者都不得产生历史 attempt。同一 Task 已提交 running/ledger 时，任何 key 只回读 `start_status=started`，不得重复建立 ledger、Cycle 或启动事件。容量、准入、Provider、代理或协议暂不可用属于已成功启动后的 `runtime_state=waiting`，Task 为 running、start operation 为 `started`；首次创建响应仍为 201，幂等重放为 200。

若事务 B 发生真实系统错误，B 内 Task 状态变化、ledger/Cycle 和启动事件必须整体回滚，Task 保持事务 A 已提交的 `Task.status=draft`，响应中的 `create_status=created` 只表示创建事务已成功，不新增 Task 主状态；首次响应仍返回 HTTP 201、原 `task_id/create_status=created/start_status=start_failed/start_failure_code`，页面明确提示“任务已创建，启动失败”。相同请求重试返回 HTTP 200，复用原 Task 和同一 `start_operation_id` 重试启动；若上次实际上已提交但响应丢失，则直接回读已启动结果，不得再次启动，也不得把已创建误报成整体创建失败。

启动与 Planner 首轮才创建真实 ledger、计算 theoretical/observed safe capacity、remaining time、blocking codes 和 catch-up 状态。容量风险只展示在任务详情，不阻止启动、不需要第二次确认。目标、账号、协议和安全硬门禁仍按各专项执行。

### 7.2 任务详情

- 顶部统一展示 target/due/confirmed/late/held/unknown/remaining/overflow/open excess/deadline，以及 `quantity_status/content_mix_status/acceptance_status`。
- 自然日任务先按 `task_day_ledger_id` 下钻并展示本地日期、冻结时区、UTC period 和 day phase；AI 再下钻到 account 并展示准入、主/备用各最多 3 轮、签到与 transport；评论/点赞/浏览下钻到 channel message，评论单列 emoji fallback；AI/评论同时展示引用与图片/表情素材 `planned/success/shortfall`，确定性兜底单列；搜索只显示 click 目标与进度，不显示入群开关或 admission-ready。
- AI/评论的 content mix 下钻同时分列 `policy_min` 与 `selector_plan` 义务来源；两者合并形成 planned 总数，但同一逻辑槽位不得重复计数。
- 每个 blocker 展示阶段、原始码、影响数量、下一决策时间和可执行处理入口。
- 任务列表的 completed/running 不能替代履约状态；`running + blocked` 必须可见。

日履约详情 API 以 `task_day_ledger_id` 为权威查询参数。兼容 `date=` 仅在该日期唯一映射到一份 ledger 时可解析；若时区切换导致同一显示日期对应多份 ledger，返回 `409 ambiguous_task_day_ledger` 和候选 ledger 摘要，不得静默合并。

### 7.3 权限

- 查看汇总需要 `tasks.view`；查看账号、协议 trace 和远端证据继续受目标/账号明细权限限制。
- AI 活群、评论、点赞、浏览的创建与创建并启动只要求 `tasks.manage`，不设置或隐含任何专项创建权限；纯搜索点击同时要求 `tasks.manage + tasks.create.search_click`，缺任一权限返回 403。正式业务接口为 `POST /api/tasks/search-click[/create-and-start]`；旧 `search-join-group` 路由/权限只作存量读取与迁移识别，创建请求固定返回 410且不能授权或代建新任务。
- 修改配置、apply 存量修复、启停任务需要 `tasks.manage` 并写一批一条审计。
- 不提供“强制成功”“忽略安全限额”“批量启动全部 stopped 任务”入口。

## 8. 存量审计与修复

统一审计脚本默认 dry-run，输出：

- 当前配置与目标粒度冲突；
- 每任务/账号/消息的真实确认、在途、未知和欠额；
- 可安全跳过的过期 pre-Gateway Action；
- 建议配置、预计容量和需要人工决定的任务状态；
- AI 旧 direct check-in 的逐条资格与拒绝原因。
- AI/评论开放 Action 是否缺 `content_mix_scope_key/content_contract_version`，以及可从 `reply_to_message_id`、规则集版本、material trace/segment 精确重建还是存在歧义。
- 存量 running/paused/draft/stopped Task 是否缺当前 `TaskStartOperation`；该结果只用于投影 `start_operation_legacy_untracked`，不得据此补造启动历史、再次启动 running Task或把 paused resume 当成新 start。

只有显式 `--apply --task-id ID` 或 `--apply --task-type TYPE` 可写入。apply 只能：

- 重建可派生 fulfillment snapshot；
- 按专项稳定顺序建立/回填 AI `TaskGroupDailyMessageSlot` 与已有 Action/Attempt/远端事实绑定；只能绑定已证同 ledger 事实，超出槽位保留 overflow，归属不明不得猜测；
- 将已过 deadline、未进入 Gateway 的 open Action 写为明确 skipped；
- 修正符合当前事实合同的 ledger 绑定；
- 为尚未进入 Gateway 且证据完整的 Action 补写等价内容合同快照；证据不完整时显式 `content_contract_replan_required` 并按同一配置 revision 重排，不能猜测 direct/reply 或素材类型；
- 写 audit log。

apply 不得为存量 Task 回填虚构 `TaskStartOperation`。存量 running/paused Task 继续依据当前 Task/ledger 事实运行或恢复，resume 不写 operation；存量 draft/stopped Task 在下一次真实 start 时才创建 version 1 当前行。

apply 不得修改 success、unknown_after_send、Gateway-started，不得补 remote ID，不得自动启动任务。历史已发送结果只按真实 `reply_to_message_id`、远端媒体类型和原 `content_source` 重建只读 mix 投影，不能回写成另一种内容类型。

## 9. QA 验收

| 场景 | 必须证明 |
| --- | --- |
| Planner 与 4 Dispatcher 并发 | PostgreSQL 并发回归无 deadlock；通用入口严格使用 `Scope -> Window -> TaskAllocation -> ShardAllocation -> Reservation -> Action`，搜索入口在 Reservation 后固定追加 `carrier（如有） -> assignment -> consumptive 子预留 -> Action`，缺失层只跳过不换序；claim 热事务无 `UPDATE tasks` |
| 容量参数 | batch limit、worker concurrency、scope capacity 分离；配置不一致 fail closed |
| 合法任务直接创建 | 缺少可用账号、即时容量、代理路线、AI provider、搜索审批样本或准入 sponsor 时，结构合法的任务仍返回创建成功；启动后才建立 ledger/Cycle，并把对应运行态原因投影为 `at_risk|waiting_*|blocked` |
| 创建权限、结构与运行态分层 | AI 活群、评论、点赞、浏览缺 `tasks.manage` 返回 403，具备 `tasks.manage` 时不得再要求不存在的专项创建权限；纯搜索点击缺 `tasks.manage` 或 `tasks.create.search_click` 返回 403；不可见跨用户引用不泄露对象；当前用户可见但静态合同非法返回 422；inline 公开目标可本地 upsert pending 引用但创建事务不得调用 Telegram resolve/probe；账号在线、Telegram 权限、代理、准入、Provider、协议和容量不得在创建前读取或改变创建结果 |
| 创建并启动状态分层 | 资源暂不可用时 `start_status=started`、Task=running、`runtime_state=waiting`；事务 B 真失败才 `start_status=start_failed` 且 Task=draft。same/new start key 并发均最多建立一份 ledger：每轮真实重试推进当前 `operation_version`，新 key 必须以 `replaces_start_operation_id/version` CAS 当前行，后请求看到 started 时回读成功，撞到 processing 时返回 `start_in_progress` |
| create-and-start 幂等与部分成功 | 相同 `client_request_id + request_fingerprint` 的超时重试只产生一个 Task；同键不同 fingerprint 返回 409 且不覆盖 Task 配置；事务 A 创建成功、事务 B 注入失败时 HTTP 201 仍带原 `task_id/start_failed` 且 B 无残留 ledger。失败后 same key 覆盖并推进 version；new key 只有 replace CAS 当前 ID/version 成功才覆盖。成功后只保留 started，不产生历史行，迟到旧请求无法覆盖当前 identity/version |
| 存量 Task 无 start operation | 存量 running/paused Task 返回 started + legacy_untracked，operation ID/version 为空且启动调用数为 0；paused resume 只恢复原 ledger、不写 operation；存量 draft/stopped 不回填历史，首次真实 start 才创建唯一 version 1 当前行 |
| 启动失败落账竞态 | 分别覆盖 same key 与 new key：B 回滚后暂停独立 failure writer，让下一轮重试先进入 processing/started；恢复旧 writer 时其 expected previous ID/version tuple CAS 必须失败并回读 current，不能把新一轮覆盖为 failed。断言只保留一条 current row，version 单调且无历史 payload |
| 结构阻塞闭集 | 只有 `task_contract_invalid`、目标终态失效/远端能力明确拒绝、账号身份非法、显式单 reply 目标终态失效或普通 reply 到 deadline 仍无候选、内容合同不可重放、兜底发送被明确安全策略禁止可以形成结构硬阻塞；deadline 前临时无 reply 候选只 waiting。数据库唯一冲突、事实归属冲突和损坏 payload 进入对象级 `consistency_quarantine`，不能扩展出隐式第七类任务门禁 |
| AI 旧 direct check-in | 合格事实计入；无 coverage/远端证据/资格不符不计入 |
| AI 总量与账号覆盖双维度 | 同一远端消息只占一个主发送槽、群日总量只加 1；若绑定未覆盖账号则同步完成该账号唯一 coverage，不能拆成两条发送或让一条消息覆盖两个账号 |
| P0-1 待可见性规划占位 | `pending_visibility` 与 `unknown_after_send` 各义务只占 1，统一进入 `unknown_after_send_hold_count/unknown_count`；不另加第三公式项、不同时进入 held、不创建同主槽替代 Action |
| P0-2 拦截与 abandon | `post_send_intercepted` 关闭当前 hold 且不计群日/coverage；账号未 ready 前不循环试发。`admission_abandoned` 只能由 `targets.manage` 带 preview/evidence/version 写入，不能缩冻结分母或完成 coverage；其他账号不能消费该 coverage 主槽，deadline 后该账号为 missed |
| P0-3 可见性原子确认 | 需要核验的 Attempt 即使有 remote id 也只进入 `pending_visibility`；`visible_confirmed` 在一个事务内关闭 hold、完成 Action、群日主槽及可选 coverage。并发 finalize 只成功一次，注入任一 CAS/唯一键失败时全部回滚 |
| AI admission 大积压 | ready 账号继续产生 content send |
| AI blocked coverage 名额保留 | 当群日目标等于冻结账号数时，已覆盖账号不能用额外消息提前占满目标；blocked 账号恢复后仍有自己的主发送槽且不会造成超量 |
| AI 三类特殊情况 | 缺面具、已验证代理路线切换直接生成精确签到；主 AI 3 轮与备用 AI 3 轮均无候选后生成精确签到；无传输路线保持 waiting，不伪造成功 |
| 候选内容安全拒绝 | 主 AI 第 1/2 轮记录拒绝并重试同一主槽，第 3 轮切备用 AI；备用第 1/2 轮继续，备用第 3 轮才转安全签到/单表情；只有兜底自身被明确策略禁止才形成硬 blocker |
| 内容编排非回归 | 相同配置、上下文和随机种子下，删除门禁前后的 direct/reply 槽位及既有图片/表情素材选择一致；只允许 scheduled time、claim 时机和 fallback 标记变化 |
| 技术切批与静默降量 | 10/30/60 Turn、20 条数据库切批、多个 claim 和静默小批量均沿用原 mix scope；引用最小值和每轮素材计数不重置、不重复套用 |
| content mix 并发 | 双 Planner/Dispatcher 下同一 `content_mix_slot_key` 只有一个 open/unknown；pre-Gateway 失败释放，unknown 占位，远端实际类型可重算 |
| ContentMix Cycle 生命周期 | Cycle 与全部 Slot/合同/`policy_min` 原子冻结；Action 以 `(cycle_slot_id,slot_attempt)` 唯一，CycleSlot 只指向一个 current Action，pre-Gateway 重建递增 attempt 并保留历史；技术切批不新建 Cycle；deadline 时仅终结未物化/replan_required/pre-Gateway 槽，Gateway-started/unknown 保持核验；全部槽明确后才 settle，已 settle Cycle 不重开且配置只影响新 Cycle |
| 兜底与占比 | 签到/评论表情保留原 reply 关系；Phase C 按兼容矩阵在 Gateway 前决定同槽共载或先 CAS 转派，未实际携带原素材类型时不计素材配额，并在配置总量内优先补齐，不能发送后补账、静默稀释或超量补发 |
| 兜底早于素材意图 | 缺面具槽位不虚构 intent、`selector_plan` 或 normal 素材 planned/success；原 `scope_total_slots` 与显式比例分母不变，有 `policy_min` 时通过 CAS 转派且总发送数不变 |
| 选择器计划非回归 | 只有 max/intent、无最低比例时，旧选择器实际选出的图片/表情/normal emoji 槽位形成 `selector_plan`；选择后兜底须转派或明确 shortfall，不能把 planned 数降为 0 |
| 最后一个素材槽位兜底 | 总量可真实完成；无剩余合法槽位时不超量发送，content_mix 明确 shortfall、acceptance 不得显示 met |
| 全任务 Dispatcher 竞争 | AI、评论、点赞、浏览、搜索和准入在多 Window 下均获得最低轮转机会；不存在固定 search>AI 或 AI>频道顺序，cursor 重启后仍连续 |
| 单用户 Dispatcher scope | `dispatcher_scope` 仅表示 worker/shard/task-type 共享容量域；不存在租户配额、租户权重或租户公平层，所有任务在同一用户域内按 deadline、欠额、可执行性和轮转排序 |
| 跨 shard 公平 | 同一父任务及 child 在 2/4 个 shard 有候选时每 Window 只获得一次最低保护；task-lane-to-shard 存在可行匹配时不闲置容量，无法映射需求显式报告 |
| 两类精确求解器不混用 | `DispatchLaneShardSolver` 只映射 task-lane 到 shard；`SearchClickAssignmentSolver` 只在已获 fulfillment 份额内绑定 click path。任何一层不得修改另一层目标、份额或远端完成事实 |
| 两类 epoch 不混用 | 中央分配或分片权重重建递增 `dispatch_allocation_epoch`，每个中央版本只建立一次 `search_click_assignment_epoch`；求解/绑定失败释放 unit 后直接进入下一 dispatch epoch，不在原 epoch 重试。两者均不重置任务日、ordinal、额度或 unknown |
| 分片权重原子发布 | Window ready 的首批非空 release set 只开启一个 pending epoch/rebuild wave；wave 内后续 release batch 仅递增 `rebuild_input_version`，不得产生第二个中间 epoch。新 epoch 全部 allocation/reservation、相同 `dispatch_rebuild_snapshot_hash`、Window `ready_rebuild_snapshot_hash` 与 `ready` 一次提交；快照后新增释放、任一规范化资源变化、CAS、数据库错误或崩溃均丢弃未发布权重并从最新事实重建，禁止部分权重可领取。已结束 Window 不做无用途重建；released unit 不复活，旧有效承诺不回退 |
| 分片重建输入竞态 | 不创建 release batch且不推进 input version，分别改变 due、eligibility、active exclusion、有效旧 Reservation 计数、Scope/Window/Shard active/unclaimed、任一 fairness cursor、parent/sponsor 聚合输入、容量、相关配置和 `dispatch_rebuild_contract_version`；提交前重建完整 `DispatchRebuildInput` 并重算 hash，必须使旧提交写入 0 行，下一 drain 发布最新事实。worker/lease、扫描或墙钟时间、进程身份、随机值及不影响输出的诊断字段不得改变 hash；solver 不得额外查库/读全局；零余额也必须以 solver input、Window/allocation/reservation 同 hash 的空 `ready` 发布 |
| 分片重建提交窗口 | SERIALIZABLE precommit 完成 input 读取后、commit 前并发更新已有输入或插入符合候选谓词的新行，提交必须 serialization abort且零发布；禁止驱动以旧 solver 输出自动重试事务，下一 drain 重新 assemble/solve。正常路径断言 rehash、全部新行、Window hash/ready 同事务 |
| 需求 bucket 取整 | 相同 deadline 的 580 个账号先聚合再 ceil，不逐账号放大；不同 deadline 的消息分别计算，不被最晚 deadline 稀释 |
| AI 共享准入竞争 | 多个 AI 父任务同时等待同一账号/目标时仅一个 `AdmissionExecutionLease` sponsor 产生 join/follow/confirm Action；其他 AI 父任务共享 ready 事实但独立复检；纯搜索点击不创建或引用 lease |
| 无 deadline continuous | 只使用版本化 fairness horizon 内的到期债务，lifetime backlog 不放大份额；仍跨 Window 获得最低轮转 |
| 两条评论消息各目标 80 | 全局 80 时不完成；两条分别 80 才满足 |
| 评论三类特殊情况 | 使用 Unicode 表情文本完成原 direct/reply Action；不转成 reaction，真实远端评论 ID 才计数 |
| 评论缺面具 | Phase A 能证明 active 面具不存在并转单表情；有面具时固化版本并保持普通 emoji 策略，不能把“未读取面具”误判为缺失 |
| 点赞 unavailable | 不增加 confirmed，不关闭其他账号/消息 |
| 非 AI 数量归属 | 评论按 `(task_id,channel_message_id,comment_plan_revision,target_ordinal)`、点赞按 `(task_id,channel_message_id,account_id,reaction_contract_version)`、浏览按 `(task_day_ledger_id,channel_message_id,account_id)`、click 按 `(task_day_ledger_id,target_id,click_obligation_ordinal)` 去重；每键同时最多一个 open/unknown/success，replacement 复用原键；评论/点赞不虚构任务日，浏览/click 保留任务日，均不要求 `primary_quantity_slot_id` |
| 非 AI 远端事实跨 Task 所有权 | 同一评论 remote ID、同一账号/消息的未变化 reaction state、同一账号/消息的 lifetime view fact、同一 click evidence hash 各只能完成一个业务义务；事实早于义务起点不得倒灌，所有权冲突只隔离受影响对象并显示 `remote_fact_owned_elsewhere` |
| click 远端事实完整性 | 只有同一 Attempt 同时具备 Gateway 开始、目标身份、批准按钮指纹、click 调用、批准协议 outcome、确认时间和 evidence hash 才确认 ordinal；仅找到目标/按钮、调用超时或 outcome 不可分类进入失败/unknown，不得写 `target_click_observed=true` |
| 纯 click 无成员副作用 | 只有 `membership_side_effect=none` 且 `membership_mutating_rpc_invoked=false` 才可确认 click；除精确 `jisou-v2` 解析语义迁移外，旧 `join_candidate` 或副作用未知形成运行路径 blocker，不调用 join/request/follow/confirm/can-send，其他合法路径继续 |
| `moderate_6h` | 所有 Action 在 6 小时 deadline 内；容量不足显式 blocked |
| 两条浏览消息各 1000、cap 500 | 创建/编辑直接成功并规范化任务门禁与任务内账号门禁为 `1_000_000`；存量接管同样改写为 `1_000_000`，继续补足两条消息各自欠额 |
| 运行中五类任务接管 | 已绑定新义务的 Action 原位续跑；未绑定且未进 Gateway 的旧 AI/search Action 显式终结并由新模型重排；评论/点赞/浏览按稳定天然键迁移；Gateway-started/success/unknown 历史不改绑、不猜测计入新合同 |
| 系统选择合法 search repeat | 账号/关键词日限额仍生效；新纯点击 API 不接收 `allow_same_account_repeat_application` |
| 搜索 63 账号×2、目标 1000 | 原始合法尝试上界仅 126，因此可证明容量缺口；页面不得把 126 展示为预测确认量，且任务不超安全额度建单 |
| 搜索共享硬额度匹配 | 同一账号额度可与多个 keyword/授权/代理组合时，原始笛卡尔积不得作为容量；只读 projection 必须对共享资源做精确去重并标记未预留，当前 Window 只有全部搜索专属子预留 CAS 成功且绑定中央 Reservation 的 `SearchClickOpportunityAssignment` 才计 committed opportunity，双 Task 不能重复使用同一资源单位 |
| 搜索 projection/commit 与中央份额 | 未来容量 projection 不写 assignment/Action/hold；当前 Window 必须先完成全任务 TaskAllocation、search fulfillment lane 和 shard Reservation，再在每 Task 已获份额内 commit assignment/Action。Dispatcher/Gateway inflight 只占一份中央 Reservation，搜索不得预占或重复预留；未点击账号容量不足时合法 repeat 仍可保住更高优先级 click/fairness 最优值 |
| 搜索精确求解结果 | 共享约束图按 ordinal、资源 key 和 task fairness key 拆连通分量；依次证明最大 click assignment 数、最大受服务到期任务数和冻结 remaining 比例的最大最小任务公平向量；每个中央版本唯一的 `SearchClickAssignmentEpoch` 即使零 assignment 也保存结果。`optimal` 原子提交全部合法匹配并整批释放 unmatched，`no_candidate|abandoned` 整批释放全部未领取 unit；遗留 open 直接 abandoned，禁止原 epoch 重试、贪心/top-N/不可验证部分解 |
| 搜索 owner lease 与 finalized 重放 | 健康 owner 跨多个租约周期持续续租，耗时本身不触发 abandoned；仅进程失联/fencing 所有权丢失才 recovery abandoned。finalized outcome 由 carrier identity、原 solver input、matched identity/version、精确 release set 与实际 wave epoch/input version 唯一重算；同结果重放零写入，任一错绑保持 `release_fact_incomplete` |
| 搜索持久输入快照 | open 前原子保存完整 problem snapshot、全部 component 与每个 Reservation/ordinal 唯一 binding；共享 resource/fairness key 不得跨 component，无候选 unit 有零边分量。owner 丢失 recovery 只用原 binding/component hash，重组旧图和 solver 额外查库均为 0；缺件、错绑或 payload/hash 不一致时零释放、零重建并对象级 quarantine。supersede 与 solver 输入复用同一 Assembler/canonicalization |
| 搜索结果提交快照 | `stable_component_key` 可由稳定业务身份重算且不含随机/carrier/worker/时间；所有账号排序字段及 source version 可从 payload 反向枚举。冻结后新增/删除候选或改变额度、已确认 click 数、机会时间、cursor、eligibility、中央份额/version时，即使 Window epoch 未变，旧 `optimal|no_candidate` 也整轮 abandoned/release/rebuild且写入 0 条 assignment；serialization/CAS/驱动旧结果重放不得产生部分提交或第二次 solver 调用 |
| 搜索首次 outcome 所有权 | ready search Reservation 尚无 Action/epoch 时，通用 no-Action/unclaimed reclaimer 必须跳过；首个 worker 创建唯一 epoch，Window 已结束则 recovery 建行并直接 abandoned且不调用 solver。任务停止/due 消失只能使原 epoch abandoned；finalize 后每个来源 Reservation 精确满足 `bound+claimed+released=reserved` |
| search rebuild 与旧 bound claim | optimal 同时产生 matched/unmatched 时，unmatched 可触发 `rebuild_required`，但匹配成功的旧 epoch Action 在 Window/deadline/版本有效时仍须 claim/Gateway；不得等待新 ready、读取未发布权重或误释放 |
| 搜索旧 epoch 防提交 | `optimal` finalize 同时校验 Window 尚可领取、ready 且当前 dispatch epoch 与 search epoch 完全一致；分别注入 Window 正在 rebuild、已在更高 epoch 回到 ready、已结束三种状态，都只能 abandoned，不能提交旧 matched |
| 搜索 post-finalize 释放 | `optimal` 已 finalized 后注入 assignment Gateway 前失败、Action 不再到期和 Window expiry：不得改写 search outcome；同 trigger 同 candidate hash 只 finalize 一条 release batch。bound/released/unclaimed/assignment/exclusion 任一点失败整批回滚；两个 batch 在同一 pending wave 中只产生一个中央 epoch，第二批使旧 rebuild 快照失效 |
| 搜索 release batch item 守恒 | 每个 candidate 恰有一条不可变 item；`candidate_unit_count = release_unit_count + already_released_unit_count + precondition_lost_unit_count`，candidate/release hash 与 `applied|no_op|mixed` 都能从 item 唯一重算。空 candidate 和全 no-op 都 finalize no-op 且不推动 rebuild |
| 搜索 finalized release batch 重放 | carrier、candidate hash、逐 item expected/observed version与分类/首 carrier、release hash、三类 count、outcome 和实际 wave 版本共同重算 `outcome_hash`；完全一致时零写回读，结果或 wave 错绑保持 `release_fact_incomplete`，同 trigger 不同 candidate 才返回 input conflict |
| 搜索重复释放原因并发 | 同一 assignment 同时被 expiry、Action 终态和 Window expiry 命中时，首个有效 trigger释放并拥有 exclusion；后到 trigger 分类为 `already_released`、no-op finalize且回读首 carrier，不双扣、不因唯一键冲突重试 |
| 搜索 release/claim 竞争 | release 先提交时以 expected assignment/Action version 同时终结 bound Action、清 lease/active并释放 assignment，迟到 claim/Gateway CAS 失败；claim 先提交时 batch 分类 `precondition_lost` 且不得释放或改写 claimed/Gateway-started unit。四 worker 并发下 assignment、Reservation 和 Action 只能形成一种结果 |
| 搜索 release 一致性矛盾 | 注入只有 carrier、只有 exclusion、post-finalize 缺 effective item、carrier/item/exclusion hash 或版本错绑、完整合法 release + claim/Gateway、released assignment 无任何 release 组件与计数漂移；release 事务全回滚，独立 quarantine writer 复核后持久化。半套事实保持 `release_fact_incomplete`；完整 release 无 claim才按逐 unit 事实对齐，孤立 released 才恢复并推进版本，只有 claim/Gateway 才向远端边界对齐，完整 release 与 claim/Gateway 并存则保持 `release_claim_fact_conflict`。冲突分支不选边、不调该 unit 计数、不 resolve或忙重试；其他独立义务继续，任何分支都不重跑搜索求解 |
| 搜索排除集合生命周期 | 每条 `DispatchAllocationExclusion` 跨全部状态永久唯一绑定一个旧 Reservation/ordinal，释放计数固定为 1。首次 solver outcome 由 search epoch 承载；finalized 后的 bound assignment 失效由稳定 trigger 的 `DispatchAllocationReleaseBatch` 承载并原子执行 assignment released、bound 减、released 加、各层 unclaimed 减。ready 首批释放开启 wave，rebuild_required 后续 batch 只更新 input version，Window 结束只收口；相同 carrier 重放不双扣。`no_feasible_search_path|search_solver_abandoned` 只随该 unit 的 carrier-independent `solver_problem_component_hash` 变化而 supersede；新 epoch/Reservation/worker 本身不触发。其他 reason 只随直接相关额度、授权、代理、协议/CAPTCHA、Gateway 或 assignment/Action version 变化；无关 Task/shard/扫描时间不得触发。Window 结束 expired 也不复活旧 unit；carrier/item/exclusion 未 fence 前不得清理，item 的逐 unit 分类与首 carrier 引用永久可追溯，新事实只能使用新 Reservation/ordinal |
| 搜索纯点击边界 | 创建只接受 `click_only + daily_click_target_count`；join switch、admission 目标或成员目标返回 422；click 确认后不创建 child |
| 搜索 CAPTCHA 实际通过 | 每个 challenge fingerprint 最多一次 Telegram 提交；只有同 fingerprint 的明确远端通过回执或已审批搜索分类/结果页才 solved。仅离开原页、新 fingerprint、hot-list、unknown 不算通过；单供应商候选不合格继续下一健康已审批供应商，供应商/传输暂不可用保持 required。识别调用/批准重试的 click、Dispatcher 份额和业务 AI 轮次增量均为 0，challenge 收口前同一账号—协议会话不能被另一搜索 Action 并发改写 |
| 搜索旧创建入口 | 旧 `/search-join-group` 新建请求固定 410 且零 Task 写入；只允许旧任务读取/审计/迁移识别，不得静默规范化成当前纯 click |
| 搜索存量混合任务 | 未结束 Task 幂等转为纯 `search_click`，后续 admission child=0；pre-Gateway membership child 终结，Gateway-started/success/unknown 和全部历史事实不改绑；completed/deleted 只读 |
| 搜索完成优先自动排序 | 不要求运营配置账号容量或账号优先级；合法 repeat 可补 click 但不得绕过安全额度；系统在纯 click 最优值相同的路径中严格按 `hard_safe_remaining_capacity DESC -> confirmed_click_count_today ASC -> last_click_opportunity_at ASC -> persistent_account_cursor ASC` 决胜 |
| 搜索软节奏追赶 | 曲线、轮次、skip、jitter 和静默降量仅影响排序与批量；欠额或落后时自动压缩 jitter、取消软 skip 并提高安全范围内的 dispatch 密度，静默期仍保留非零发送；不得产生 `skipped_by_behavior_pacing` 终态 |
| 搜索 CAPTCHA | `required` 不排除账号；AI 调用/批准重试不占 click 限额或目标且无业务固定 AI 轮数/递归次数，供应商/传输暂不可用保持 required；同 fingerprint 的单次批准提交只有取得明确远端通过回执或已审批搜索分类/结果页才 `solved` 并继续，离开原页/新 fingerprint/hot-list/unknown 均不算；识别链确实无安全答案或同 fingerprint 被远端明确拒绝才 `failed` 并排除账号—协议路径；禁止概率折损容量 |
| 极搜会话偏移 | `hot_list_page` 直接记录 `jisou_hot_list_page` 失败并排除当前账号—协议路径 12 小时；其他非预期页记录 `jisou_session_state_deviated`。均不得发送 `/cancel`、`/start`、重发关键词、点击未知 callback 或执行外部导航，新的 Action 默认 `reset_executed=false` 且历史 reset 字段只读 |
| 存量新履约接管 | 部署后运行中五类 Task 在 Planner 扫描 open/backlog 之前幂等建立当前合同；paused/stopped 只升级合同不启动；重复 reconcile 的 ledger/slot/obligation/Action 增量均为 0 |
| 统一任务门禁上限 | 新建、编辑、启动与存量接管后的五类 `pacing_config.max_actions_per_hour`，以及 `task_daily_view_safety_cap|max_views_per_account_per_day|max_likes_per_account_per_hour|max_total_comments|max_comments_per_account_per_hour|search_click max_actions_per_day|per_account_daily_action_limit|per_account_hourly_action_limit|per_keyword_account_daily_limit` 均为 `1_000_000`；搜索 task-local skip 概率与账号冷却归零，旧 backlog 数量门禁不再作用于 `all_task_v2`。低值和陈旧 blocker 不能截断目标或触发 completed，账号全局安全、Telegram/授权/代理/协议/unknown 硬门禁保持原值 |
| 搜索点击完成 | 完整 evidence 写 `target_click_observed` 后 ordinal 结束，无 membership/admission/can-send 后续动作 |
| 搜索点击加入模式 | 仅确认是后续独立模式；本轮不得实现或拿旧开关代替专项设计 |
| AI 任务时区变更 | 当前 ledger 继续使用旧 timezone；其 deadline 起建立新时区 `timezone_transition` ledger，UTC 区间首尾相接、无重复/遗漏冻结账号；过渡日不混入完整日 SLA |
| AI 群日目标有当前 ledger 时编辑 | 当前 ledger、MessageSlot 和 pacing snapshot 不变；pending 目标固定在当前 deadline 生效，重复编辑 CAS 更新待生效值但不延后生效时刻；届时非 running 不建空 ledger |
| 搜索任务时区变更 | source 继续绑定原 `task_day_ledger_id`；新时区建立连续 transition ledger，即使本地日期文本重复也不得合并 |
| DST 与日期查询 | 23/25 小时本地日按真实 UTC 午夜边界建账；重复显示日期命中多 ledger 时 `date=` 返回 409 及候选，不合并 |
| 暂停与跨日恢复 | deadline 前恢复沿用原 ledger/anchor；跨 deadline 的暂停日保留 missed，非运行 gap 不补账，恢复从 `partial_start` 建新 ledger |
| 目标并发收口 | confirmed 达精确目标后，pre-Gateway excess 稳定终结；Gateway/unknown 保留核验，晚到成功造成 overflow 时事实不删除、不跨日抵消 |
| unknown_after_send | 防重复但不计成功；不能自动重发 |
| 日切 | 旧日未达写 missed，新日独立建账，不搬运成功数 |

## 10. 发布、回滚与生产验收

发布顺序固定：

1. **A：事务与 claim**——deadlock、锁顺序、scope capacity。
2. **B：统一履约与 AI**——读模型、deadline、AI 账本 reconcile。
3. **C：频道互动**——评论、点赞、浏览。
4. **D：搜索点击**——安全限额、协议 canary、真实 click；不包含搜索点击加入。
5. **E：存量审计与整日验收**。

每包必须走 `master -> release -> GitHub Actions Deploy Production`，记录 release SHA；失败通过 revert 当前包 commit 后重新走同一路径，不在线上补代码。两类 solver contract version 变化的包必须采用 Dispatcher 全量 fence 切换：先阻止旧版本取得新 ownership并确认旧进程/可提交事务归零，再启动新版本；禁止混合版本 canary 或沿用旧内存权重。

生产 E4 门：

- A：连续 30 分钟并跨完整 Planner/Dispatcher drain，deadlock=0，reservation 守恒。
- B：AI 每账号 coverage、群日总量及已配置引用/素材占比可追到 Action、Attempt、remote ID；需可见性核验的消息还须追到 `pending_visibility_hold -> visible_confirmed`。同一主槽只有一个 post-Gateway 未确认占位，拦截/abandon 不缩冻结分母，可见确认无部分提交；总量与内容账本分别相等且 content mix shortfall/overflow/策略违规均为 0。
- C：受控真实消息分别取得评论、reaction、view 的逐消息目标；评论已配置引用/素材 content mix 违规为 0，单表情兜底未混入正常文本 emoji 或素材成功数。
- D：只验 `search_execution_mode=click_only`：先接管运行中旧混合搜索并 canary，再放量；每个完成 ordinal 都有完整 click evidence、无新增 membership 副作用、无 admission lane/lease/child。`dispatch_allocation_epoch`、`search_click_assignment_epoch`、首次 search Reservation 独占期、`DispatchAllocationReleaseBatch`、逐 candidate 的 `DispatchAllocationReleaseBatchItem`、永久 unit `DispatchAllocationExclusion` 与对象级 `consistency_quarantine` 均可审计，且每个来源 Reservation 首次 outcome 后满足 `bound_count + claimed_count + released_count = reserved_claims`；旧 membership 历史事实保持原绑定。“搜索点击加入”不进入本轮 E4。
- E：在任务时区完成一个完整自然日；五类均达到各自目标才可写 `production_fixed`。

若代码已部署但搜索账号容量仍不足，结论必须为 `production_blocked: insufficient_safe_capacity`，不能写修复完成。

## 11. Product Design Complete 自检

| 检查项 | 结论 |
| --- | --- |
| 用户原始问题 | 五类任务的按时、按量和共同调度均已覆盖；引用及图片/表情内容占比有可计算合同、义务转派和独立验收 |
| 功能设计 | 合法任务直接创建、启动后运行态求解、统一履约、AI 待可见性三个 P0、两类 solver/epoch、搜索 Reservation 首次 outcome 独占、永久 unit exclusion、post-finalize release batch/item 与对象级一致性隔离、`membership_admission`、逐任务合同、稳定非 AI 天然义务键与类型专用事实所有权、纯搜索点击共享资源/task-fairness 分量化多阶段字典序精确匹配、ContentMix Cycle 与兜底兼容矩阵、存量修复和发布分包已定义；搜索点击加入明确不在本轮范围 |
| 前端状态 | 创建成功、启动结果与可变运行态三者分离；首次 201/幂等 200/冲突 409、目标、真实确认、欠额、deadline、容量、blocker、waiting、quantity/content_mix/acceptance 三状态及 planned-success-shortfall 已定义 |
| 后端/API/Worker | API 授权/静态合同/运行事实分层，创建 fingerprint 与 start operation 幂等；启动后建立 ledger/Cycle；pending visibility 单占位与 visible-confirmed 原子事务、abandon 权限/版本；内容合同/义务 CAS、Cycle 物化与结算、单用户共享 scope、中央六级锁序及搜索扩展锁序、公平 claim、两类 solver/epoch、完整 immutable `DispatchRebuildInput`、三类 allocation/Window 同一 rebuild hash、SERIALIZABLE 原子发布、contract version 全量 fence 切换、搜索 Reservation 首次归属、release batch/item 分类守恒、永久 exclusion/identity tombstone 与独立 quarantine writer 生命周期、搜索 ordinal/assignment 依次证明最大 click、最大受服务任务和最大最小任务公平、pacing 追赶、完整 click 证据、Gateway 事实和极搜 no-reset 已定义 |
| 数据流 | 结构校验 -> fingerprint 幂等 Task 创建成功 -> start operation 建立不可变 task-day ledger；AI 原子冻结 coverage+extra 主发送槽/ContentMix Cycle，非 AI 持久化自然 obligation key 和远端事实 owner；搜索先做无写入 projection，当前 Window 在全任务 TaskAllocation/Reservation 后才 commit assignment 和 Action；随后 Attempt -> remote fact -> 总量、账号覆盖与内容占比 snapshot；时区切换区间连续且历史事实不重解释 |
| 权限安全 | 调用者授权失败不写 Task blocker且不泄露跨用户对象；保留账号、目标、内容、协议、CAPTCHA 和 unknown 门禁 |
| 边界场景 | 静态引用冲突与运行账号身份失效分离、结构阻塞闭集、同键不同 fingerprint、启动响应丢失、同/不同 start key 并发、启动状态与 runtime waiting 分离、对象级一致性隔离与跨 Task 事实所有权、日切、DST、歧义日期查询、暂停/跨日恢复、legacy mixed search 隔离、任务时区变更、动态消息、目标并发收口与 overflow、共享资源不重复投影、精确求解失败不提交部分结果、完整重建 input 漂移、rehash-to-commit update/phantom、contract version 切换、极搜会话偏移 no-reset、Cycle deadline/结算、兜底素材不兼容、配置冲突、外部容量、并发和回滚已覆盖 |
| QA | 自动化、canary、整日 E4 和 blocked/unproven 结论已定义 |
| design_status | `complete` |

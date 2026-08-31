# AI 活群词库每日主题轮换、任务话题参与度与自然词库专项设计 PRD

| 属性 | 内容 |
| :--- | :--- |
| **文档版本** | v1.2.0 |
| **需求类型** | L2 内容质量与任务配置能力 |
| **当前阶段** | dev 与本地 QA 完成，进入 Product Acceptance |
| **设计状态** | Product Design Complete / Ready for Dev |
| **生效任务** | `group_ai_chat` |
| **首期路由范围** | `group_ai_chat` 的显式 general/adult route；按 route 使用独立词库，不做弱词推断 |
| **关联权威 PRD** | `docs/01-product/tg-ops-platform-prd.md`、`docs/03-feature-designs/ai-group-generation-failure-churn-remediation-prd.md`、`docs/03-feature-designs/ai-group-chat-quality-and-token-optimization-prd.md`、`docs/03-feature-designs/ai-group-topic-teacher-burst-prd.md` |
| **实现状态** | 本地核心链路与第二轮差距修补已实现；23 文件定向回归 457 passed、176 deselected，最终质量门复验 74 passed；Phase 0 真实 Provider 基线、PostgreSQL 并发、发布与 Telegram 七日 E4 未证明 |

---

## 0. Intake Card 与原始需求

### 0.1 用户原始需求

1. 解决 AI 活群重复词过多、连续发问和模板化表达问题；
2. System Prompt 支持每日轮换与更丰富的自然口语词库；
3. **主要兼容任务已经配置的话题，不能被每日轮换覆盖或带偏**；
4. **话题参与度支持配置，最高 30%**。

### 0.2 本 PRD 对“话题参与度”的定义

“话题参与度”指：同一任务、目标群和任务日内，普通 AI 正文中由系统**主动分配任务 `topic_directions` 作为主线**的最大比例。

它不等于现有 `participation_rate`。现有字段仍表示一轮中参与发言的账号比例，不得改名、复用或改变其既有履约语义。

### 0.3 Product Handoff 摘要

- 产品目标：降低重复词和模板句，同时保证任务话题、真人上下文和事实锚点优先；
- 新增字段：`topic_participation_rate`，范围 `0.00～0.30`；产品文案统一为“任务话题占比上限”。新建任务必须显式确认，UI 可以推荐 `0.30`，API 不得静默代填；
- 词库每日主题独立轮换，只选择兼容的词汇与表达风格，不新增业务话题，也不进入任务话题参与度计算；
- `AiGroupContentAllocationPlan` 是话题占比、reply、material 和 act type 的唯一 aggregate owner；话题配额按该 plan 的稳定 task-day 正文 ordinal 分配，不另建第二预算 owner；
- 多 worker、跨批次、重试和远端结果未知都必须守住 30% 硬上限；
- 话题额度不足时只允许少用任务话题，不得让数量义务、账号 coverage 或远端发送总量产生新增 shortfall；
- 本文完成后才允许进入 dev，product 不在本阶段修改代码或生产配置。

---

## 1. 背景、问题与目标

### 1.1 当前问题

1. 固定成人 System Prompt 长期暴露相同高显著词，模型容易反复使用“水头”“开课”等少数词；
2. 多账号共享相似 Prompt 后，经常连续生成疑问句，出现明显机器人节奏；
3. 原草案把“每日主题”同时当作语气和内容方向，可能覆盖 planner 已冻结的任务话题；
4. 把整份大词表直接塞进固定 System Prompt 会增加 Token，并可能形成新的高频词坍缩；
5. 现有任务级 `topic_directions` 已进入 slot、生成、消息记忆和 Action 链，不能另建一套隐式话题来源；
6. 内容比例若按 `random() < 0.3` 独立抽样，无法在小批次、并发 worker、重试和跨批次场景保证真正不超过 30%。

### 1.2 建设目标

1. 建立 7 天确定性**群可见面词库调色板**轮换；调色板只影响兼容表面表达权重，不决定 relation、act type 或任务话题；
2. general/adult route family 各建立不少于 120 个带主题、分类、route、act type 和事实属性的自然表达单元；
3. 按 slot 稳定采样少量兼容词，并结合近期使用记录冷却；
4. 新增可配置的任务话题参与度，硬上限为 30%；
5. 保持任务话题、老师、引用目标、persona、内容 route、事实锚点、安全门和数量履约合同不变；
6. 让配置、计划、生成、质量门、Action 和远端事实都能解释某条消息为什么使用或没有使用任务话题。
7. 用同一 Task 的改造前远端样本建立词频、问句、句首、语义簇、Token 和调用成本基线，改造后以真实 Telegram 可见正文闭环验收。

### 1.3 非目标

- 不改变 `daily_message_target`、coverage、stable obligation、Action 或 Telegram Gateway 的数量真相源；
- 不使用话题参与度限制到期业务义务、账号覆盖或发送总量；
- 不修改频道评论、点赞、浏览和搜索任务的配置语义；
- 不根据群名、城市、大学、老师等弱词推断成人 route；
- 不允许 Prompt 词库成为人物、地点、经历或服务事实来源；
- 不允许词库每日主题重新分配或覆盖 `relation_kind`、`act_type`、`stance`、reply、material、teacher、persona 或任何已冻结 content assignment；
- 不允许以 accepted candidate、Provider 成功或 Action success 代替 Telegram remote-confirmed 正文质量样本；
- 不在本产品阶段热更新生产文件、重启服务或修改存量任务配置。

### 1.4 与既有专项的优先级

- 对 `topic_directions` 的 slot 覆盖范围、任务话题上限和词库使用，本 PRD supersede `ai-group-topic-teacher-burst-prd.md` 中“每个 slot 都分配任务话题”的旧口径；`teacher_targets` 仍为独立内容维度，不进入任务话题 30% 分子；
- 对词汇是否必用、单词频率和内容验收，本 PRD supersede `ai-group-chat-quality-and-token-optimization-prd.md` 中“每条成人消息必须包含至少一个核心暗语”的旧口径；行业词允许使用但不得强制出现；
- 数量、coverage、reply/material/act-type owner、intent immutable、GenerationJob、Action 和 settlement 继续以 `ai-group-generation-failure-churn-remediation-prd.md` 为准；本文只能扩展既有 owner 字段，不能创建平行 owner。

---

## 2. 概念分层与不可破坏的优先级

### 2.1 三个正交维度

| 维度 | 真相源 | 作用 | 能否改变任务业务话题 |
| :--- | :--- | :--- | :---: |
| 任务话题 | `topic_directions` 及冻结 slot snapshot | 决定少量消息主动围绕什么业务主线表达 | 是唯一主动业务话题源 |
| 话题占比上限 | `AiGroupContentAllocationPlan.topic_rate_bps` + task-day cumulative ordinal | 决定哪些普通正文 assignment 可以主动使用任务话题 | 只做 aggregate 内容分配 |
| 词库每日主题 | `daily_vocabulary_theme_id` | 为全部兼容 general/adult 正文 slot 调整词汇类别和表面表达权重 | 否；不进入任务话题比例 |
| 自然词汇样本 | `vocabulary_sample_ids` | 为当前 slot 提供少量表达候选 | 否 |
| 讨论老师 | `teacher_targets` 及 allocation/intent snapshot | 决定是否围绕某个老师对象表达 | 独立维度；不受任务话题 30% 限制 |

### 2.2 强制优先级

从高到低固定为：

1. 未成年人、联系方式、URL、露骨内容等安全红线；
2. 当前 content route、结构化输出合同和发送前确定性质量门；
3. `AiGroupContentAllocationPlan` 已冻结的 `relation_kind`、`act_type`、`stance`、reply/material 要求和内容构成；
4. 真人上下文、原生引用目标及可证明事实锚点；
5. planner 已冻结的 `topic_direction`、`teacher_target`、`reply_target`、`material_intent` 和 slot identity；
6. 账号 persona、账号表达卡和明确的任务风格配置；
7. 任务话题占比上限的 allocation 结果；
8. 词库每日主题的表面表达权重；
9. 当前 slot 的自然词汇样本。

低优先级层不得覆盖、删除、猜测或重写高优先级层。

### 2.3 任务话题兼容铁律

- 词库每日主题只能改变“用哪些兼容词、怎么说”，不能改变“说什么”；
- 只有 `topic_mode=configured_topic` 的 slot 才能主动把 `topic_directions` 作为内容主线；
- `topic_mode=human_context` 必须优先承接真人消息，即使该消息与任务话题不同；
- `topic_mode=group_free_chat` 不得因为当天风格或词库而隐式引入任务话题；
- 任务话题为空时，不得把词库每日主题、词汇样本或 `TgGroup.topic_direction` 伪装成已配置任务话题；
- `active_topic_direction` 和 slot 级 `topic_direction` 同时存在时，以 slot 冻结值为准；
- 已冻结 intent 的 topic/teacher/reply/persona 在重试中不可改写。
- `relation_kind`、`act_type`、`stance`、reply/material requirement 先于每日主题冻结；每日主题只能在相同 assignment 内选择兼容说法，不能把陈述改成提问、把同意改成分歧或把 direct 改成 reply；
- `teacher_targets` 不进入 `topic_participation_rate` 的分子或分母，但仍受 relation、act type、真人上下文和事实合同约束；详情页必须单独展示 teacher planned/remote ratio，避免把“任务话题最多 30%”误读成“全部任务导向内容最多 30%”。

---

## 3. 配置合同

### 3.1 新增字段

| 字段 | 类型 | 默认值 | 校验 | 含义 |
| :--- | :--- | :---: | :--- | :--- |
| `topic_participation_rate` | decimal | 无静默默认 | `0 <= value <= 0.30`，最多两位小数 | 主动使用任务配置话题的最大比例；新建/启用新合同必须显式确认 |

后端 API/配置使用精确小数，Web 展示为整数百分比 `0%～30%`。进入 task-day ledger 的 content-policy snapshot 时必须规范化为整数 `topic_rate_bps=0～3000` 并冻结；每个 `AiGroupContentAllocationPlan` 复制同一 ledger 值并作为唯一分配 owner，运行时配额和比例门只做整数运算，不直接用二进制浮点比较。Web 新建页可以预选推荐值 `30%`，但保存前必须由用户确认；API 缺字段返回明确 422，不得把推荐值当服务端默认。

### 3.2 字段语义

- `0`：禁止系统主动分配任务配置话题；真人消息自然谈到同类内容时仍可承接；
- `0.10`：最多 10% 普通正文 slot 主动使用配置话题；
- `0.30`：最多 30%，也是产品允许的最高值；
- 配置值是**上限**，不是强制完成目标；无安全内容、无兼容上下文或无配置话题时，实际比例可以更低；
- 大于 `0.30`、小于 `0`、NaN、Infinity、字符串或精度非法的请求必须返回明确 422，不截断、不四舍五入、不静默回退；
- `participation_rate` 继续表示参与账号比例，前后端文案必须与新字段明确区分。
- 产品展示名统一为“任务话题占比上限”，不得仅显示“话题参与度”；详情同时显示配置上限、预计当日普通正文数、预计最多任务话题条数和实际条数；
- 预计最多条数为 `floor(expected_normal_text_count * topic_rate_bps / 10000)`；`expected_normal_text_count` 来自当前 effective daily target、已知 coverage/check-in 能力和 content-plan capacity preview，只是保存前估算，不能替代运行时 allocation/remote 分母。结果为 0 时保存前明确提示“按当前预计正文量，今日可能不会主动使用任务话题”，不能把合法的 0 展示成系统故障；
- `1%` 至少需要 100 条、`5%` 至少 20 条、`10%` 至少 10 条、`20%` 至少 5 条、`30%` 至少 4 条普通正文才可能出现首条任务话题；这是硬上限的数学结果，不得用突破比例的“至少一条”兜底。

### 3.3 配置生效时间

- 新建且未启动任务：首次任务日直接使用保存值；
- 已运行任务修改比例：该字段是 task-day content-policy snapshot，保存为下一 `Asia/Shanghai` 任务日生效；不得归入普通 `participation_*` 的 next-allocation-plan 即时变更；
- 当日已冻结的 stable obligation、intent 和 Action 不改写；
- UI 必须显示“当前任务日值”“下个任务日生效值”和生效日期；
- 暂停/恢复不重置当日话题预算；停止后新启动若仍是同一任务日，继续使用同一任务日预算；
- Task clone 必须显式复制该字段，不能依赖默认值偷偷变化。
- `topic_directions`、`teacher_targets` 仍属于 unit generation/content：修改后只对尚无 Action、无 active Generation、无 call-issued 的新 intent revision 生效；已有 allocation plan 的 rate、task-day ordinal 和已分配 vector 不重置；
- TG Bot 保存话题/老师后必须返回“新内容单元生效，今日话题占比上限不变”，Web 详情按 generation-policy revision 展示旧/新 snapshot 的生效范围；
- 混合修改 rate 与 topic/teacher 时在一个事务返回每字段的 `effective_scope/effective_revision/effective_at`：rate=`next_task_day`，topic/teacher=`new_intent`，任一字段失败则整笔不生效。

### 3.4 权限与审计

- 使用现有 AI 活群任务编辑权限；
- 每次修改记录旧值、新值、当前 config revision、next-effective task day、actor 和时间；
- 详情页必须读回实际生效值，不使用表单默认值冒充后端配置；
- TG Bot 首期只显示比例摘要，不提供编辑入口，保持其只编辑话题/讨论老师的既有轻量边界。

### 3.5 与词库每日主题完全解耦

- `topic_participation_rate` 的分子只统计 `topic_mode=configured_topic`，也就是系统主动采用任务 `topic_directions` 的普通正文；
- `daily_vocabulary_theme_id`、`vocabulary_sample_ids` 以及消息是否实际使用某个词库词，全部不进入任务话题比例的分子或分母；
- `topic_participation_rate=0` 时，只禁止系统主动采用任务配置话题，词库每日主题仍按日期正常轮换并对全部兼容 general/adult 正文 slot 采样；
- `topic_participation_rate` 从 0 调整到 30%，不得改变当天 `daily_vocabulary_theme_id`；跨日词库主题轮换也不得推进、消耗或补偿任务话题额度；
- `configured_topic`、`human_context`、`group_free_chat` 三种模式都可以使用当天词库主题的兼容表达；是否获得具体词汇样本只由 route、事实、act type、冷却和安全门决定；
- 一条 `configured_topic` 消息可以同时使用词库每日主题，但它进入 30% 分子是因为冻结的任务话题来源，不是因为词库主题；
- 词库每日主题没有“参与度 30%”限制，也不得新增同名比例字段。
- `teacher_targets` 是独立任务配置维度，不计入任务话题比例；它可以高于 30%，但必须单独展示 teacher ratio，并且不能覆盖真人 reply、relation/act type 或事实锚点；
- 任务话题实际使用低于上限是合法结果，不形成额度欠账、不跨日补偿，也不产生需要补发的任务话题 shortfall。

---

## 4. 30% 硬上限与稳定分配算法

### 4.1 配额域

话题分配必须并入现有 `AiGroupContentAllocationPlan`。该 plan 继续是 reply、material、act type、teacher 和 topic mode 的唯一 aggregate owner；task-day cursor 只签发永不复用的普通正文 ordinal，不拥有比例，也不创建第二套 budget/slot 表。

task-day 统计域固定为：

```text
(tenant_id, task_id, target_group_id, task_day_ledger_id, topic_rate_revision)
```

每个 `AiGroupContentAllocationPlan` 必须冻结 `topic_rate_bps`、本 plan 起止 `normal_text_ordinal`、`topic_mode` vector 和配置 snapshot hash；技术批次只能消费 plan assignment，不能重新计算比例。分母是该域内已由 allocation plan 分配的普通 AI 正文 assignment，以下内容不进入分母：

- deterministic `签到`；
- emoji reaction；
- membership/admission/control Action；
- 频道评论等其他任务；
- 尚未取得稳定普通正文 identity 的临时候选。

`content_contract_revision`、topic/teacher generation-policy revision 变化不得重置 task-day ordinal 或 rate；rate 只在下一 task-day ledger 创建时切换。多个 plan 的 allocated counts 汇总后仍必须满足同一 task-day 上限。

### 4.2 topic mode

每个普通正文 assignment 在创建 immutable intent 前必须由 `AiGroupContentAllocationPlan` 冻结一个值：

```text
topic_mode = configured_topic | human_context | group_free_chat
```

| 模式 | 选择条件 | 是否消耗话题额度 |
| :--- | :--- | :---: |
| `configured_topic` | ordinal 获得计划资格、任务有合法话题、当前 slot 不受冲突真人回复约束，且已取得远端话题容量 reservation | 是 |
| `human_context` | 存在合法真人上下文或引用目标，需要优先承接 | 否 |
| `group_free_chat` | 无合适真人上下文，且本 ordinal 未获得/无法使用话题额度 | 否 |

真人上下文碰巧与配置话题语义相似时，只要 slot 的来源是当前真人消息，仍记录为 `human_context`；模型输出后不得反向改标签来规避预算。

`topic_mode` 不能改变 allocation plan 已冻结的 `relation_kind`、`act_type`、`stance`、reply/material requirement 或 teacher assignment。若 configured topic 与这些字段不兼容，只能在 intent 创建前选择 `human_context/group_free_chat`，不重抽其他业务维度。

### 4.3 前缀硬上限公式

设当前普通正文稳定 ordinal 为 `n`，冻结整数比例为 `b=topic_rate_bps`：

```text
eligible(n, b) = floor(n * b / 10000) > floor((n - 1) * b / 10000)
```

只有 `eligible=true` 的 ordinal 才有**计划资格**申请远端话题容量 reservation。比如 `b=3000` 时，首批计划资格 ordinal 为 4、7、10、14……；是否最终成为 `configured_topic` 还要通过 §4.5 的远端容量检查。任意 plan 前缀与 task-day 汇总都不会超过 30%。

附加规则：

- 不使用 `random() < topic_participation_rate` 作为额度真相源；
- ordinal 与 stable obligation identity 绑定，失败、重试、worker takeover 和重放不得重新抽签；
- 获得计划资格但没有合法任务话题、与真人引用冲突或尚无远端容量 reservation 时，在 immutable intent 创建前分配为 `human_context/group_free_chat`，该资格不向后补偿；
- 不得为了“尽量达到 30%”创建额外消息或改变业务数量目标；
- 多个 `topic_directions` 仍沿用既有权重和近期使用量分配，只在 `configured_topic` slot 中选取。

### 4.4 并发与幂等

- 配额分配必须由 `AiGroupContentAllocationPlan` 的 plan cursor/version CAS 完成，并与 assignment reservation 处于同一事务；不得由 executor、Prompt builder、Generation worker 或 Dispatcher另算；
- 唯一 identity 至少包含 allocation plan、plan unit ordinal、task-day normal ordinal 和 topic-rate revision；
- 两个 generation worker 不得为同一 ordinal 分配两个 topic mode；
- 已存在 intent 时返回已有分配，不推进新的预算游标；
- task-day ledger、topic-rate revision、route、lifecycle epoch 或目标群漂移时 fail closed，不从旧域借用额度；
- 任务删除、停止或重建不得让同一远端业务义务获得第二次话题额度。
- replacement、takeover 和同一 obligation 的新 variation 复用原 allocation assignment/topic mode；技术批次边界和 worker 重启不得新建 allocation plan 规避上限；
- topic/teacher 配置修改只影响新 intent snapshot，不重置 task-day ordinal、topic-rate revision 或既有 plan vector。

### 4.5 生成前远端容量 reservation 与 Gateway 守卫

只约束 plan 比例仍可能因 non-topic pre-Gateway 失败而使 Telegram 可见比例超过上限。为避免生成后等待和数量欠量，远端比例约束必须前移到 immutable intent/Provider 之前：

- allocation plan 为 eligible assignment 选择 `configured_topic` 前，读取同统计域 Telegram 已确认普通正文数 `C`、其中 `configured_topic` 已确认数 `T`、`configured_topic` 远端结果未知数 `U`，以及 active topic reservation 数 `R`；
- 按最坏情况守上限：`configured_topic` unknown 可能已经发送，必须同时进入分子和分母；non-topic unknown 可能没有发送，不得进入容量分母；普通 non-topic 的未发送 reservation 也不得提前扩大分母；
- 只有 `(T + U + R + 1) * 10000 <= (C + U + R + 1) * topic_rate_bps` 时，才能在 allocation plan 事务内 CAS 创建唯一 `topic_remote_capacity_reservation`，随后冻结 `configured_topic` intent；
- 无远端容量时不创建 topic intent、不调用 Provider、不创建 Action，也不等待到 deadline；当前尚未冻结的 assignment 正常选择兼容的 `human_context/group_free_chat`，从而继续完成同一数量/coverage 义务；
- reservation 与 allocation assignment/intent identity 绑定；pre-Gateway 明确失败释放 reservation，Gateway-started/unknown 转为保护计数，confirmed 转为远端事实计数；
- Gateway 只复核 reservation identity、rate revision 和远端事实投影，不在发送时第一次分配预算。缺 reservation 或投影漂移是 `topic_capacity_contract_invalid`，属于实现合同错误，禁止发送；它不得作为普通业务 scarcity 状态长期等待；
- 不再定义 `topic_participation_budget_wait` 或 `topic_participation_deadline_shortfall`。任务话题是上限内的可选内容构成，少用不形成欠账；数量与 coverage 沿现有 obligation/rematerialization/settlement 合同独立闭环；
- Gateway-started/unknown 永不因比例原因重放。

### 4.6 比例公式

```text
planned_topic_ratio = configured_topic_assignment_count / allocated_normal_text_assignment_count
remote_boundary_topic_ratio = configured_topic_confirmed_or_unknown / (normal_text_confirmed + configured_topic_unknown)
```

比较时使用整数交叉相乘：`topic_count * 10000 <= normal_count * topic_rate_bps`，不得依赖浮点除法结果。remote-boundary 使用最坏情况分母：全部 remote-confirmed normal body 加 `configured_topic` unknown，不得用 non-topic unknown 扩大容量。allocation-plan/task-day planned、active reservation 与 remote-boundary 三层都必须 `<= topic_participation_rate <= 0.30`；分母为 0 时状态为 `not_applicable`，不能展示为 0% 已通过。配置值是上限，实际值低于上限不形成 shortfall。

---

## 5. 7 天词库每日主题与表达风格轮换

### 5.1 稳定日期与算法

运行时日期固定使用 task-day ledger 的 `Asia/Shanghai` 日期，不能由 Provider、系统本地时区或用户 Prompt 提供。主题作用域是群成员实际看到的会话面，而不是单个 Task：

```text
surface_scope_key = (tenant_id, canonical_target_group_id, content_route_family)
surface_offset = uint64(sha256("ai-group-theme-v2:" + canonical(surface_scope_key))[0:8]) % 7
day_index = (task_day.toordinal() + surface_offset) % 7
```

要求：

- `sha256()[0:8]` 指原始 digest 的前 8 个字节，并按 unsigned big-endian 解析为 `uint64`；
- 禁止使用 Python 内置 `hash()`；
- 同一群可见面、同一路由族、同一天跨 Task、进程、重启和 worker 必须一致，避免同群多个 Task 各用不同主题；
- 同一群可见面连续七天必须完整覆盖七种词汇调色板；
- 不同目标群通过稳定 offset 错峰；
- 测试可以注入 `date_key`，生产调用必须从冻结 task-day context 取得日期。

### 5.2 风格矩阵

下表是词库内部的每日调色板，只调整**已冻结 act type 内**候选词类和表面表达的权重，不分配 speech act，不携带人物、地点、老师、服务、环境或经历事实，也不属于任务 `topic_directions`：

| ID | 调色板 | 兼容表面表达 | 禁止事项 |
| :---: | :--- | :--- | :--- |
| 0 | 短促随性 | 对当前 act type 选择 8～12 汉字的短连接词、语气词和收尾词 | 不使用 2 字附和或 `+1` 绕过长度门 |
| 1 | 陈述质感 | 当 act type 本来就是 statement/evaluation 时提高评价、感叹类词权重；其他 act type 使用中性变体 | 不把 question/reply 改成评价，不凭空评价具体对象 |
| 2 | 求证质感 | 当 act type 本来就是 question/detail_follow 时提高具体求证词权重；其他 act type 使用谨慎措辞 | 不制造新问题，不得泛问“大家怎么看” |
| 3 | 承接质感 | 对 reply/supplement 使用转折、承接和收尾表达 | 不把 direct 改成 reply，不新增事实 |
| 4 | 保留质感 | 当 stance 已冻结为 disagreement/reserved 时使用克制分歧词；其他 stance 使用中性保留词 | 不改变 stance，不攻击用户、不制造事实 |
| 5 | 轻松质感 | 在已允许的 mood/persona 内提高轻松语气词权重，可选最多一个 emoji | 不改变 mood，不用调侃掩盖事实问题 |
| 6 | 均衡质感 | 在当前 allocation plan 已冻结的 act-type mix 内均衡选择低频表面变体 | 不重新分配 act type，不让多个账号生成同构句式 |

每日主题是候选权重，不是“当天全部消息都采用同一种句式”。所有 compatible slot 都读取当天主题，但每个 slot 仍允许 0 个样本；不得为主题新增参与率字段，也不得把主题解释为必须命中比例。

### 5.3 generic warmup 特例

现有权威合同保持不变：

- 无可用真人上下文时只能生成不指向具体人物、资源、地点或服务的开放问句；
- 禁止无对象附和、求推荐、评价具体资源或声称个人经历；
- generic warmup 的 `act_type=question` 由 allocation plan 冻结；每日调色板只能选择 question-compatible 表面词，不能把它改成陈述、分歧或附和；
- `generic_warmup` 仍执行成人 route 的 8～20 汉字硬门；
- 不允许通过静态短句、签到或 emoji 冒充正常 AI 正文成功。

连续发问由 aggregate content owner 控制，而不是靠每日主题碰运气：

- `AiGroupContentAllocationPlan` 必须冻结 question count/vector；在存在兼容非问句 act 的正常 Cycle 中，任意连续 3 个普通正文 assignment 不得全为 question；
- 对最近 10 条 Telegram remote-confirmed normal body，question 占比目标不高于 40%；窗口不足 10 条时只执行“不得连续 3 问”；
- 若当前只有 generic warmup 才合法且会形成连续第 3 问，则该内容 assignment 按现有 `quality_wait/content_shortfall` 合同等待新上下文或结算，不能让词库主题强造无依据陈述，也不能用固定模板冒充正文；
- 该质量 shortfall 属于既有上下文/行为能力不足，不是任务话题额度 shortfall；不得推进或补偿任务话题配额。

### 5.4 现有风格配置兼容

- `system_prompt_override` 非空时，继续受系统安全、事实、JSON 和 slot identity 硬合同约束；其明确风格要求高于词库每日主题的表达风格，但不改变 `daily_vocabulary_theme_id`。冲突词汇过滤后允许空样本，并记录 `daily_vocabulary_theme_sample_suppressed_by_override`；
- `tone != auto` 时显式 tone 优先，词库每日主题只能选择兼容表达变体；
- `account_personas`、slot persona 和表达卡优先于词库每日主题的表达风格；
- `slang_terms` 与内置词库统一走 route、安全、事实和近期冷却门，不因用户配置而强制输出；
- general 与 adult route family 都启用各自词库；general 只能读取 general catalog，adult 只能读取显式 adult route 兼容 catalog，不因群名或话题弱词自动切换路由；
- 详情页必须展示 `daily_vocabulary_theme_effective_state=active|partially_suppressed|suppressed_by_override|effective_pool_low|not_applicable` 及原因；theme ID 存在但被 override/tone/persona 全部压制时不得显示为“正常生效”。

---

## 6. 大规模自然词库合同

### 6.1 词库不是事实库

自然词汇只定义表达可能性，不证明群里存在相应人物、地点、老师、经历、环境或服务。模型不得把被抽中的词写成已发生事实。

### 6.2 词汇单元结构

首期 general/adult route family 各不少于 120 个唯一 `vocabulary_id`，每项至少包含：

```json
{
  "vocabulary_id": "adult_attitude_reliable_v1",
  "category": "attitude",
  "surface_terms": ["态度挺稳", "说话实在"],
  "normalized_term_ids": ["态度稳", "说话实在"],
  "theme_tags": [1, 5, 6],
  "theme_weights": {"1": 3, "5": 1, "6": 2},
  "allowed_routes": ["adult_service"],
  "allowed_act_types": ["short_react", "detail_follow"],
  "allowed_stances": ["neutral", "positive"],
  "fact_class": "expression_only",
  "rarity": "common"
}
```

`theme_tags` 不能为空，且只能引用已发布的七个 `daily_vocabulary_theme_id`；`theme_weights` 只在已经通过 route/act-type/stance/fact 兼容过滤后排序，不能让高权重词绕过上游 assignment。

### 6.3 分类要求

至少覆盖以下类别，每类不少于 12 个唯一单元：

1. 外观与照片真实性表达；
2. 态度与沟通感受；
3. 配合与节奏表达；
4. 环境与到达便利表达；
5. 档期与时间安排表达；
6. 避坑、观望和求证表达；
7. 陈述、附和、分歧和情绪表达；
8. 极简但满足长度合同的自然回应；
9. 日常转折、承接和收尾表达；
10. 账号 persona 差异化表达。

分类数量允许交叉，但每个 route family 按规范化后的唯一表达单元计数必须不少于 120；不得把同一词的标点或同义拼写伪装成多个单元。catalog validator 还必须枚举发布 manifest 声明支持的 `route × theme × act_type × stance/fact_class` compatibility cell：每个 cell 在冷却前至少有 12 个唯一单元，否则 catalog 不能发布；运行时冷却后少于 4 个时标记 `effective_pool_low` 并允许 0 样本，不能反复调用 Provider 试图制造词汇。

### 6.4 按 slot 稳定采样

采样种子：

```text
sha256(surface_scope_key, task_day, allocation_plan_id, plan_unit_ordinal, daily_vocabulary_theme_id, vocabulary_catalog_version)
```

规则：

- 每个 slot 最多注入 2 个词汇单元；允许 0 个；
- 只从当前 `daily_vocabulary_theme_id/theme_tags`、content route、act type、stance、topic mode 和事实类别都兼容的集合采样；每日主题不能只进入 seed 而不进入候选过滤/权重；
- 任务话题中的专有名词不进入通用词库，也不能被通用词替换；
- `generic_warmup` 不采样需要对象或事实锚点的单元；
- 候选选择和 group-visible 频率 reservation 先在 allocation assignment CAS 中完成；只有 reservation 成功后才把 `vocabulary_sample_ids/normalized_term_ids` 冻结进 immutable intent，Provider 重试不换样本逃避合同；
- Prompt 明确说明词汇是可选表达提示，不要求全部使用。

### 6.5 近期冷却与重复门

冷却域为同一 `surface_scope_key=(tenant + target_group + content_route_family)`，跨 Task、跨账号共享，防止同群多个任务轮流重复同一个显眼词或各用不同词频窗口。

- 采样时扫描近期 Telegram remote-confirmed/unknown-hold message memory，以及尚未进 Gateway 的 active vocabulary reservation；不能只看当前账号或当前 Task；accepted 但已明确 pre-Gateway 失败的候选释放 reservation，不计群可见使用；
- 最近 20 条内已经实际使用的 rare 单元不再采样；
- 最近 10 条内已经实际使用的 common 单元不再采样；
- 最近 100 条 remote-confirmed/unknown-hold 普通正文内，任何单一 `vocabulary_id` 与任何单一 `normalized_term_id` 的保护使用比例都不得超过 5%；
- 相同规范化 2-gram 在连续 20 条内最多出现 2 次；中文 2-gram 统计必须排除版本化 stop-bigram（如无法表达业务重复的常用虚词），stop list 变更推进 quality-policy version；
- 冻结 `topic_direction_snapshot` 中的专有名词、老师名和必须原样保留的任务短语不进入通用 vocabulary/2-gram 频率门，但完整句仍受精确重复和 semantic cluster 门约束；
- 精确重复和既有 semantic cluster 重复继续由现有质量门拒绝；
- 冷却后没有候选时返回空样本，不回退到高频词；
- 词频 reservation 冲突必须在 intent/Provider 前按固定候选顺序选择下一个兼容单元；候选耗尽返回空样本。intent 冻结后不允许因并发冲突重抽；出现 reservation/intent 漂移时记录 `vocabulary_reservation_contract_invalid` 并走现有 contract recovery，不能用同一高频样本重复消耗 Provider；
- 生成后从最终候选正文确定性提取 `vocabulary_used_ids`、`vocabulary_used_term_ids` 和规范化表面短语 fingerprint。模型自报不作为事实；未命中原词但形成同义/片段重复时仍由 surface 2-gram、句首和 semantic cluster 指标捕获；
- 词频门失败必须记录具体 `vocabulary_frequency_exceeded`，不得静默改写成固定模板。

### 6.6 Token、调用成本与吞吐预算

- 不把完整 catalog 注入 Prompt；每个 slot 仍最多注入 2 个短表达单元，并记录新增 input token 数；
- 复用现有 `ai_content_max_cost_per_slot`、`ai_content_daily_budget` 和 Provider usage ledger，词库/主题不得建立旁路预算；
- Phase 0 prototype 与生产 canary 都计算 `input_tokens/provider_calls/retries/cost` 除以 Telegram remote-confirmed normal body，不能以 accepted candidate 作分母；
- 相对同 Task、同 route、相近日量的改造前基线，单条远端确认正文的 P50 input tokens、Provider calls 和估算成本任一增长超过 15%，或 remote-confirmed throughput 下降超过 10%，Release Gate 失败；
- 预算耗尽、Provider 拒绝和质量重试沿用现有 typed wait/shortfall，不允许为词库功能新增静态正文、mock success 或无限重试。

---

## 7. Prompt、计划与运行时数据流

### 7.1 数据流

```text
Task + target group + task-day/config revision
  -> stable normal-text obligation
  -> AiGroupContentAllocationPlan (sole aggregate owner)
  -> task-day normal ordinal + deterministic topic eligibility
  -> remote topic-capacity reservation before intent
  -> frozen relation/act-type/stance/topic_mode + slot topic/teacher/reply/material/persona
  -> group-visible deterministic daily_vocabulary_theme_id
  -> theme/route/fact/act-type/stance compatible vocabulary reservation + recent cooldown
  -> GenerationJob prompt layers
  -> Provider structured output
  -> deterministic length/safety/fact/topic/frequency/duplicate gates
  -> accepted variation + AiGroupMessageMemory
  -> ready Action payload snapshots
  -> Gateway topic/vocabulary reservation identity guard
  -> ExecutionAttempt / Telegram Gateway
  -> typed remote fact or protected unknown
  -> planned/remote topic ratio and vocabulary usage projection
```

### 7.2 Prompt 分层

Prompt 必须分层组装，不生成一份每日巨型字符串覆盖全部上下文：

1. 不变的系统安全和结构化输出合同；
2. 显式 content route 合同；
3. 当前 context source 和事实锚点；
4. 冻结 allocation plan、relation/act type/stance、slot topic/teacher/reply/persona/material；
5. `topic_mode`、topic capacity reservation 与“上限不是目标”说明；
6. 独立的词库每日主题、兼容表达变体和“不计入任务话题比例”标记；
7. 最多 2 个可选词汇单元；
8. 近期禁用词/句式摘要和输出 contract。

### 7.3 必须冻结和透传的字段

以下字段至少进入 immutable intent、GenerationJob/prompt snapshot、消息记忆或 Action payload 的对应可审计位置：

```text
content_contract_revision
allocation_plan_id
plan_unit_ordinal
relation_kind
act_type
stance
topic_participation_rate
topic_rate_bps
topic_ratio_scope_key
normal_text_ordinal
topic_budget_eligible
topic_mode
topic_direction_snapshot
teacher_target_snapshot
topic_capacity_reservation_id
daily_vocabulary_theme_id
daily_vocabulary_theme_version
daily_vocabulary_theme_effective_state
vocabulary_catalog_version
vocabulary_sample_ids
vocabulary_normalized_term_ids
vocabulary_reservation_id
vocabulary_used_ids
vocabulary_used_term_ids
surface_phrase_fingerprints
```

生成后通过确定性匹配提取 `vocabulary_used_ids/vocabulary_used_term_ids/surface_phrase_fingerprints`；模型自报字段不能作为使用事实。

### 7.4 接口方向

实现可以使用不可变 context 对象，避免继续扩展松散参数：

```python
@dataclass(frozen=True)
class DailyExpressionContext:
    surface_scope_key: str
    task_day: date
    allocation_plan_id: str
    plan_unit_ordinal: int
    relation_kind: str
    act_type: str
    stance: str
    topic_mode: str
    vocabulary_theme_id: int
    vocabulary_sample_ids: tuple[str, ...]
```

`build_group_prompt` 必须消费 allocation plan/intent 已冻结的 context，不得自行重新选择话题额度、每日主题、词汇 reservation、act type、stance 或 slot topic。Prompt builder 只是确定性 render 阶段，不拥有 acquire/prepare 阶段的业务分配。

---

## 8. 安全、事实与失败语义

### 8.1 保持的安全合同

- 未成年人、联系方式、URL、引流和露骨内容继续由现有确定性质量门执行；
- Prompt 中出现“禁止”文字不等于已通过安全验收；
- 成人 route 继续执行 8～20 汉字边界，普通 route 不套用该限制；
- safe context 的经历、地点和服务陈述必须有清洗后正文同类锚点；
- 行业词、任务话题、词库样本、群名、老师名和非空 message ID 本身都不能替代事实锚点；
- Provider 拒绝、解析失败和质量失败必须保留既有 typed failure，不生成 mock/silent fallback。

### 8.2 失败状态

| 状态 | 含义 | 是否可重试 |
| :--- | :--- | :---: |
| `topic_capacity_unavailable` | eligible assignment 在 intent 前没有远端话题容量 | 不是失败；本 assignment 分配为兼容 non-topic，数量继续 |
| `topic_capacity_contract_invalid` | 已冻结 topic intent 缺 reservation 或 Gateway 投影漂移 | fail closed；按 current contract recovery 修复，不作为普通等待态 |
| `topic_contract_revision_drift` | config/task-day/target/epoch 与冻结合同漂移 | fail closed，由 owner 重建合法 intent |
| `vocabulary_pool_exhausted` | 冷却后无兼容词 | 允许空样本继续生成 |
| `vocabulary_effective_pool_low` | compatibility cell 冷却后少于 4 个单元 | 允许空样本继续生成，展示降级状态，不重试 Provider |
| `vocabulary_frequency_exceeded` | 最终正文造成 term/表面短语频率超限 | 同 intent 质量重试，不换 topic mode/act type；不得重抽冻结样本 |
| `vocabulary_reservation_contract_invalid` | sample reservation、intent 或 surface scope 漂移 | Provider 前重建合法 intent；Provider 后走 contract recovery，不循环调用 |
| `daily_theme_contract_invalid` | theme/version/date 不可复现 | 阻断生成，不回退固定 Prompt |

不得再产生 `topic_participation_budget_wait` 或 `topic_participation_deadline_shortfall`。Provider-started unknown 和 Telegram `unknown_after_send` 沿用现有 unknown 防重合同；不能因为想调整话题比例或词汇 reservation 而重放。

---

## 9. API、Web 与详情可观测性

### 9.1 后端 API

- `GroupAIChatTaskCreate` 与启用新 writer 的 canonical config 增加必填 `topic_participation_rate`；`TaskSettingsUpdate` 增加可选更新字段；存量详情/read model 允许该字段为 null，但必须同时返回 `topic_policy_state=legacy_unconfirmed`，不能因缺字段导致任务不可读；
- create、update、detail、clone、audit 走同一规范化和校验；
- OpenAPI 展示小数范围 `0～0.30` 和“任务话题占比上限；非目标值；不限制词库主题/老师讨论”的说明；
- 不接受旧 `participation_rate` 作为别名；
- 不新增第二套 legacy 字段或字符串百分比字段。

### 9.2 Web

创建、编辑和任务详情增加“任务话题占比上限”控件：

- 展示为百分比，最小 0、最大 30、步长 1；新建页可以推荐 30%，但保存前必须显式确认；
- 帮助文案：“这是任务 `topic_directions` 的普通正文占比上限，不是必须达到的目标；词库每日主题和讨论老师不计入该比例；真人回复不为凑比例转题；不影响账号参与比例和每日总量。”；
- 与“参与账号比例”分开显示，不放在同一标签或同名字段；
- 运行中修改时提示下一任务日生效；
- 表单根据预计普通正文数实时显示 `floor(expected_normal × rate)`；预计为 0 时给出明确提示并允许用户确认保存；
- 详情显示当前/下一任务日配置值、预计最多条数、allocation-plan/task-day planned ratio、active reservation、remote-boundary ratio、分子/分母、topic capacity 状态；
- 详情单独显示 teacher planned/remote ratio，以及每日主题 ID、route family、effective state、有效池大小和 override/tone/persona 抑制原因；
- 分母为 0 显示“不适用”，不能显示绿色 0%。

### 9.3 TG Bot

- 设置摘要中显示当前/下一任务日“任务话题占比上限”、预计最多条数和“词库主题/讨论老师不计入该比例”；
- 首期不允许 Bot 修改比例；
- Bot 继续只编辑话题方向和讨论老师；
- Bot 修改话题列表不能改变已经冻结的 task-day rate revision；保存成功时返回新话题只影响新 intent 的生效说明。

---

## 10. 兼容、迁移、发布与回滚

### 10.1 存量任务兼容

- 新字段缺失的存量 `group_ai_chat` 配置在新合同启用前先进入只读 inventory；
- inventory 输出精确 Task、目标群、运行状态、配置 revision、话题数量、预计普通正文数、0%～30% 各档预计最多条数和待确认状态；
- 不为存量任务静默写 `0.30`。每个精确 Task 必须由运营明确选择 `0～0.30` 并进入 preview；未确认任务保持 `legacy_topic_policy` 且不得启用新 writer，页面显示“待确认任务话题上限”；
- cutover 只为已确认的精确目标写入所选 `topic_participation_rate`、topic-rate revision 和新 writer fence；
- 已有 `topic_directions`、teacher、persona、账号、数量目标、排期和 route 不改写；
- 配置写入需要 preview fingerprint、SHA、actor/approval、事务内漂移复核、审计和独立 readback；
- 未通过 readback 的 Task 不启用新 writer；
- 无话题任务即使显式选择 0.30，实际 `configured_topic` count 仍为 0；UI 必须显示“未配置任务话题，实际为 0”。

### 10.2 Phase 0 手工原型与基线 Gate

在 schema、migration 或运行 owner 开发前，先用目标 Provider 对 general/adult 各至少四类代表场景做手工原型：有真人 direct、有真人 reply、无上下文 generic warmup、配置 topic/teacher 冲突场景。每类至少生成 20 条候选并记录 route、assignment、Prompt tokens、输出、质量失败和人工评分。

原型必须证明：结构化输出可解析；主题不改变 topic/relation/act type/stance；词汇可选而非机械全用；无连续 3 问；相对当前 Prompt 的 top-10 表面词占比、重复句首率和 semantic-cluster 重复率均下降至少 30%；P50 input tokens 增长不超过 15%。任一项失败则 `Prototype Gate=failed`，不得进入 schema/worker 实施。

### 10.3 发布路径

- Release Level：L2；
- 正式路径：`master -> release -> Deploy Production Workflow`；
- 禁止把单文件复制到容器和只重启 backend 作为正式发布；
- backend、planner、全部 AI generation worker、dispatcher/gateway 必须读回同一部署 SHA；
- 本地测试、CI、部署、runtime、生成质量和 Telegram typed remote fact 分层记录；
- 先对一个精确任务 canary，再逐任务启用；单任务通过不等于所有任务完成。

### 10.4 回滚

代码和配置分开回滚：

1. 停止为新 obligation 创建新合同 intent；
2. 保护 Provider-started、Gateway-started、unknown 和已有 typed remote fact；
3. 等待或前向结算旧 revision 的 open item，不重放；
4. 切回上一 Prompt/quality writer revision；
5. 按 inventory snapshot 精确恢复或移除 `topic_participation_rate`；
6. 独立读回 Task 配置、runtime SHA、open owner、planned/remote ratio；
7. 代码回滚不得冒充配置回滚，配置回滚不得删除历史消息记忆和远端事实。

---

## 11. QA 与验收标准

### 11.1 配置与兼容测试

| 编号 | 场景 | 预期 |
| :--- | :--- | :--- |
| CFG-001 | create/update 传 0、0.10、0.30 | 保存并原值读回 |
| CFG-002 | 传负数、0.3001、1、NaN、字符串 | 明确 422，零写 |
| CFG-003 | create 缺少新字段 | 明确 422；UI 推荐值不能成为 API 静默默认 |
| CFG-004 | 同时配置 `participation_rate` 和新字段 | 两字段语义互不影响 |
| CFG-005 | 运行中修改比例 | 下一任务日生效，当日 plan/intent 不变 |
| CFG-006 | 同时修改 rate 与 topic/teacher | 返回 next_task_day/new_intent 两类 effective scope；失败则整笔零写 |
| CFG-007 | clone Task | 显式复制新字段，不依赖推荐值 |
| CFG-008 | Web 预计正文 1/3/4/10 条，rate=30% | 分别显示最多 0/0/1/3 条；0 条有明确提示 |
| CFG-009 | 存量任务未确认 rate | 保持 legacy writer，页面待确认；不得静默写 30% |
| CFG-010 | TG Bot 摘要 | 可读但不可编辑；明确词库/老师不计比例 |

### 11.2 话题优先级与比例测试

| 编号 | 场景 | 预期 |
| :--- | :--- | :--- |
| TOP-001 | `b=3000`、ordinal 1～100 | 任意前缀 `configured_topic` count 不超过 floor(n×3000/10000) |
| TOP-002 | `b=0` | 零 `configured_topic` slot，但 `daily_vocabulary_theme_id` 和兼容词汇采样仍正常 |
| TOP-003 | 多 worker 并发同 ordinal | 只有一个 allocation assignment/topic mode，且唯一 owner 是 `AiGroupContentAllocationPlan` |
| TOP-004 | 同 intent 重试/takeover | topic mode、topic snapshot 和 ordinal 不变 |
| TOP-005 | 有冲突真人引用且 ordinal 有额度 | 使用 human_context，额度不补偿 |
| TOP-006 | 任务无 `topic_directions` | 不把 group fallback/词库每日主题当任务配置话题 |
| TOP-007 | slot topic 与 active topic 不同 | slot topic 优先且原样透传 |
| TOP-008 | eligible ordinal 尚无 remote capacity | intent/Provider/Action 增量为 0；assignment 变为兼容 non-topic，数量继续且无 topic shortfall |
| TOP-009 | 并发 topic capacity reservation | 只有满足远端整数上限的 reservation 成功；其余 assignment 正常走 non-topic |
| TOP-010 | fact-first 到期批次 | 新字段不截断数量义务和账号 coverage |
| TOP-011 | 0.01～0.30 跨语言/进程计算 | 统一使用 bps 整数结果，无浮点边界漂移 |
| TOP-012 | 任意词库每日主题/词汇样本 | 不进入任务话题比例分子或分母 |
| TOP-013 | `configured_topic` 同时使用当天词库 | 只因任务话题来源计入一次分子，词库主题不重复计数 |
| TOP-014 | topic Action Gateway-started/unknown | reservation 转保护计数且禁止重放 |
| TOP-015 | relation/act type/stance 与每日主题冲突 | 原 assignment 原样保留，主题只选兼容表面词或空样本 |
| TOP-016 | teacher 分配覆盖 60% slot | topic ratio 仍只统计 `configured_topic`；teacher ratio 单独显示 60% |
| TOP-017 | 一个逻辑 Cycle 被切成多个 20 条技术批次 | 复用同一 allocation plan/vector，不重置 task-day ordinal 或比例 |

### 11.3 词库每日主题测试

| 编号 | 场景 | 预期 |
| :--- | :--- | :--- |
| ROT-001 | 同群可见面/day 跨进程和重启 | `daily_vocabulary_theme_id` 一致 |
| ROT-002 | 同群可见面连续七天 | 七种调色板各出现一次 |
| ROT-003 | 同群两个 Task 同 route 同日 | 主题一致、冷却共享；不同群按稳定 offset 错峰 |
| ROT-004 | 生产时区不是 UTC+8 | 仍以 Asia/Shanghai task day 为准 |
| ROT-005 | generic warmup 遇陈述/调侃主题 | 使用兼容开放问句，不生成无对象附和 |
| ROT-006 | `system_prompt_override` 冲突 | 显式风格优先、theme ID 不变、冲突样本可为空，安全/slot 合同不变 |
| ROT-007 | 成人长度边界 7/8/20/21 汉字 | 仅 8/20 边界内通过 |
| ROT-008 | 任务话题上限 0%/30% | 同 surface scope/day 的词库每日主题选择完全一致 |
| ROT-009 | general/adult 两 route family | 分别只读自己的 catalog，不因弱词串 route |
| ROT-010 | statement/question/reply 同日同主题 | act type 不变，只选各自兼容 surface variant |

### 11.4 词库与质量测试

| 编号 | 场景 | 预期 |
| :--- | :--- | :--- |
| VOC-001 | catalog 校验 | general/adult 各不少于 120；theme tags/weights、route、act type、stance、fact 字段完整 |
| VOC-002 | compatibility manifest | 每个发布 cell 冷却前至少 12 个唯一单元；不足则 catalog 不可发布 |
| VOC-003 | 同 intent Provider 重试 | sample/reservation IDs 稳定，不重新抽词 |
| VOC-004 | 多 worker 同群高频候选 | reservation CAS 只允许合法数量；冲突在 Provider 前选择下一候选或空样本 |
| VOC-005 | 最近窗口高频词 | vocabulary ID、normalized term 和 surface phrase 都被冷却，不回退固定模板 |
| VOC-006 | 冷却后少于 4 个或无兼容词 | effective_pool_low/空样本继续，Provider 不因空池重试 |
| VOC-007 | generic warmup | 不注入需要人物/地点/经历锚点的单元；不得形成连续第 3 问 |
| VOC-008 | 100 条 remote-confirmed/unknown-hold 样本 | 单一 vocabulary/term ID 保护使用率不超过 5%，精确重复为 0 |
| VOC-009 | 20 条 remote-confirmed 窗口 | 非 stop 规范化 2-gram 最多重复 2 次，rare 单元不重复采样 |
| VOC-010 | 任务话题含专有名词 | 专有名词原样保留，不被词库替换或扩写成事实 |
| VOC-011 | override/tone/persona 压制 | effective state 和原因准确，不显示为正常生效 |
| VOC-012 | 最近 10 条有兼容非问句上下文 | question 不超过 4 条且无连续 3 问；assignment act-type 守恒 |

### 11.5 自动化回归入口

dev/qa 至少新增或更新：

- `backend/tests/test_ai_group_daily_vocabulary_theme.py`；
- `backend/tests/test_ai_group_topic_participation.py`；
- `backend/tests/test_ai_group_content_allocation_plan.py`；
- `backend/tests/test_ai_group_vocabulary_sampling.py`；
- `backend/tests/test_ai_group_safe_prompt.py`；
- `backend/tests/test_ai_group_diversity_optimization.py`；
- `backend/tests/test_ai_generation_quality_pipeline.py`；
- `backend/tests/test_ai_generation_phase_boundaries.py`；
- `backend/tests/test_group_ai_chat_dataflow.py`；
- `backend/tests/test_dispatcher_dataflow.py`；
- `backend/tests/test_task_center_config_normalization.py`；
- `backend/tests/test_frontend_permission_gating.py`。

后端测试使用 `backend/.venv` 且单次命令硬超时 60 秒。需要 PostgreSQL 的并发唯一性测试必须在真实 PostgreSQL 测试环境执行，不能用 SQLite 结果替代。

### 11.6 生产验收

单 Task canary 必须选择具备每日不少于 100 条 Telegram remote-confirmed normal body 能力的精确任务；没有满足该样本资格的任务时，E4 状态保持 `unproven`，不得降低样本门或用 accepted/unknown 补足。canary 先冻结同 Task、同 route、相近日量的改造前 remote-confirmed 基线，再连续覆盖 7 个完整 task day，以证明全部词库主题而不是单日样本。至少证明：

1. deployed SHA 与 backend/planner/AI generation worker/dispatcher 一致；
2. 当前/下一 task-day 配置 readback 正确；
3. 每个 task day 至少抽取 100 条带 typed remote fact 的 Telegram remote-confirmed normal body；accepted-only、Action success 和 unknown 不进入质量通过样本；
4. 每日精确重复为 0，单一 vocabulary ID/normalized term ID 使用率不超过 5%，非 stop 2-gram 满足窗口门；
5. 任意连续 3 条不全是问句；最近 10 条存在兼容非问句上下文时，问句不超过 4 条；
6. 相对基线，top-10 表面词占比、重复句首率和 semantic-cluster 重复率均下降至少 30%；
7. allocation-plan/task-day planned、active reservation 与 remote-boundary topic ratio 均不超过配置值和 30%；
8. 抽样 `configured_topic` 消息都与冻结 slot topic 相符；抽样 relation/act type/stance/teacher 与 allocation assignment 一致；
9. 抽样 `human_context` 消息不因词库每日主题或任务话题额度转题；
10. rate 设为 0% 与 30% 的同群同日主题完全一致；无远端话题容量时数量/coverage 无新增 shortfall；
11. 单条 remote-confirmed normal body 的 P50 input tokens、Provider calls 和估算成本相对基线增长均不超过 15%，throughput 下降不超过 10%；
12. 0 条违反现有确定性内容合同；每条 Telegram 完成结论有对应 ExecutionAttempt/Gateway/typed remote fact。

---

## 12. Product Design Complete 自检

| 检查项 | 状态 | 说明 |
| :--- | :---: | :--- |
| 原始需求覆盖 | 完成 | 词库每日主题独立轮换、丰富词库、任务话题兼容、任务话题最高30%均有合同 |
| 功能与字段设计 | 完成 | 独立必填字段、无静默默认、范围、低量预览、生效时间和 teacher 独立口径完整 |
| 前端状态 | 完成 | 创建显式确认、预计条数、编辑/详情 effective scope、主题有效状态和 TG Bot 边界完整 |
| 后端/API/worker | 完成 | allocation plan 唯一 owner、task-day ordinal、生成前 remote reservation、intent、Prompt、质量门和投影完整 |
| 数据流转 | 完成 | 从 Task/ledger/allocation plan 到 typed remote fact 及比例/词频投影闭合，话题不足不影响数量 |
| 权限与安全 | 完成 | 权限、红线、事实锚点和失败语义保持 fail closed |
| 并发/幂等 | 完成 | stable ordinal、唯一 identity、重试/takeover/unknown 语义明确 |
| 兼容与迁移 | 完成 | 冲突专项 supersede、存量逐 Task 确认、精确 apply/readback 和版本切换明确 |
| QA 与验收 | 完成 | Phase 0 原型、配置、唯一 owner、比例、轮换、有效池、成本、并发和 7 日 remote-confirmed E4 均有验收 |
| 发布与回滚 | 完成 | L2、正式 workflow、canary、前向结算和配置/代码分离回滚明确 |

### 12.1 Dev 实施顺序

1. 先完成 §10.2 Phase 0 手工原型与 baseline Gate；失败则停止，不开发 schema/worker；
2. 扩展现有 `AiGroupContentAllocationPlan`、task-day content-policy snapshot 和 immutable intent；不得创建第二 topic budget owner；
3. 实现 schema/API、无静默默认、逐 Task 迁移确认、Web/TG Bot 有效范围和低量预览；
4. 实现生成前 topic capacity reservation、group-visible daily theme、route-family catalog、vocabulary reservation/冷却与 Prompt 分层；
5. 实现确定性 surface frequency/question mix 质量门、Gateway reservation guard、成本和详情投影；
6. 更新项目结构/数据流索引和全部定向测试；
7. QA 通过后回 product 验收；Product accepted 后进入 Release Gate 和单 Task 七日生产 canary；
8. 只有七个 task day 的真实 Telegram remote-confirmed 质量、数量/coverage、比例、成本与吞吐证据闭合后，才能声明生产效果完成。

### 12.2 2026-08-31 实现核对与证据边界

本轮已完成以下本地实现：

1. migration `0184_ai_group_content_alloc` 新增 canonical `AiGroupContentAllocationPlan/AiGroupContentIntent`，按 task-day quantity slot 冻结稳定 ordinal、topic mode、teacher snapshot、question count/vector、每日主题、词库样本、配置快照、生命周期、目标引用 revision 和 reservation identity；有真人上下文的连续三问/十条四问限制在 intent 前改选兼容 act，只有 question 合法的 generic warmup 则显式进入 `generic_warmup_question_mix_wait`，不伪造成陈述；
2. 任务话题占比配置严格限制为数值 `0～0.30`、最多两位小数；新建/全量更新必填，存量读取投影 `legacy_unconfirmed`；运行中修改保存为当前任务日值与次日待生效值，不改写当日 plan；
3. 话题容量在 immutable intent 与 Provider 前按 typed remote fact 最坏情况校验，Gateway 前再次校验 task/tenant/ledger/task-day/target/reference revision/lifecycle/route/surface/config snapshot/reservation；`configured_topic` unknown/active reservation 受保护，non-topic unknown 不提供分母；无容量分配为 `human_context/group_free_chat`，不产生数量 shortfall；多个话题方向按配置 weight 与同一可见面近期冻结用量选择；
4. daily theme 只读取冻结 task day，按群可见面与 route family 轮换；general/adult 词库、兼容采样、冷却、surface term Prompt 注入和 override/professional-tone 抑制状态已接通；catalog 发布显式 compatibility manifest，分类少于 12 或 cell 少于 12 不发布，运行时冷却后不足则空样本并显式投影 `effective_pool_low`；
5. Web 已提供 0～30% 显式确认、按当日 effective target 的低量预计、当前/下一任务日展示和 0/0“不适用”；Task API 返回 rate/topic/teacher 的 `effective_scope/effective_revision/effective_at`，内容策略修改单独推进 revision 并写 old/new/scope 审计；任务详情独立投影 topic/teacher/theme/pool；TG Bot 只读比例，编辑话题/老师后明确仅新 intent 生效且当日比例不变；
6. 生成仍复用现有结构化输出和质量 pipeline，没有新增 Provider stage；生成结果记录 vocabulary/normalized-term/去 stop 的 surface 2-gram fingerprint，最终质量门对 100 条词项窗口和 20 条短语窗口执行 `vocabulary_frequency_exceeded` fail-closed，现有消息记忆继续执行精确、高相似和 semantic-cluster 去重。

2026-08-31 第三轮审查后的实施修正合同：

1. `AiGroupContentIntent` 自插入起就是 active allocation/vocabulary/question reservation；在 Action 尚未创建时也必须进入后续批次的最坏容量和冷却投影。投影按 intent identity 去重，同一 quantity slot 的 replacement Action 不得重复计数。
2. 纯 rate/topic/teacher/tone/persona/词库内容策略修改不得调用通用 `_clear_unfinished_plan`，不删除、supersede 或释放已冻结 intent/GenerationJob/Action；只有 target、quantity、timezone/pacing 等义务结构改动才能进入原有 reset 合同。
3. content generation policy 使用独立 server-owned revision，不借用 `Task.config_revision` 来伪装逐字段生效范围。rate 的 current/next 与 topic/teacher 分别记录真实 revision/effective_at；本次未改的字段不更新时间、不重复写审计。
4. Provider/Gateway 必须逐项比对 intent 冻结的 ordinal、eligibility、relation、act、stance、theme effective state、catalog version、sample/surface/normalized-term/reservation；payload builder 不得从当前 item/profile 重算这些业务字段。
5. 词频查询必须先在 SQL 限定 `surface_scope_key`，再取 100/20 条窗口；不得先取 tenant 最新 N 条后在进程内过滤。topic direction 专有词、teacher 名和必须原样短语在 fingerprint 前确定性剔除。
6. compatibility manifest 是版本化的固定发布合同，validator 必须从 manifest 反向验证每个 cell 仍有至少 12 个单元；不能从当前 catalog 过滤生成 manifest 让能力静默消失。
7. `remote_topic_ratio` 按 remote-boundary 口径同时包含 configured-topic unknown；confirmed-only 数据如需展示必须使用不同字段名，不得以 0% 掩盖已占用的最坏容量。

2026-09-01 第四轮业务闭环修正合同：

1. 通用暖场只在**当前 slot 没有真人 reply/material/topic/teacher 等业务上下文**时成立；planner 必须在冻结前直接把该 slot 的 act 选为 `question`，不能先冻结普通 act 再由下游整批拒绝。仍有历史真人 reply target 的 slot 继续按 `human_context` 处理，不得因当前批次 `usable_rows` 为空而误判为 generic warmup。
2. 分配期容量与 Telegram 发送期容量使用两个明确投影。分配期把 active configured-topic reservation 纳入最坏分子，并可用 active normal reservation 证明计划前缀；Gateway 在目标群行锁内只使用 remote-confirmed normal、configured-topic unknown hold 与本次 configured-topic candidate 计算远端可见前缀，**尚未发送的 active normal 永远不能充当 Telegram 分母**。因此任何真实可见前缀都不得超过配置值和 30%。
3. source/material/reply 容量裁剪必须发生在 intent 冻结前，未进入可执行 schedule 的 assignment 不得创建幽灵 reservation。intent 生命周期以 quantity obligation 为主：数量槽仍为 open/reserved/replan-required 时，即使旧 Action 已明确失败，intent 仍为 active 且 replacement 复用同一 identity；只有数量槽合法终止且无 typed remote fact 时才 released，禁止 release 后再以同一 slot 重复占额。
4. canonical generation slot 的 `stance` 为必填冻结字段；缺失或不在 manifest 的 stance 必须在 intent/Provider 前显式失败，不能以空值绕过 compatibility cell。generic question 使用冻结的中性 stance，克制分歧使用 reserved stance，其他 act 使用确定性 stance 映射。
5. vocabulary sampler 必须同时消费 `topic_mode` 和可引用事实证据。`context_bound` 单元只有在 reply/topic/material 中存在其规范化事实锚点时才可候选；没有证据的 `group_free_chat` 与 generic warmup 只能使用 `expression_only` 单元或空样本。general/adult 两条 route 都必须发布足够的自然 `expression_only` compatibility cells；配置俚语只提供释义，不是强制输出通道，也不得绕过 act/stance/事实/冷却合同。
6. 冷却窗口以**消息条数**计数而不是把 sample id 展平后切片。active intent 使用 reservation；remote-confirmed/unknown 使用最终生成文本实际命中的 sample/term/fingerprint，未实际使用的 reservation 不得记为历史命中。question mix 与显眼表达窗口跨 task day 连续；question owner 按同一目标群聚合且不因 route 切换重置，route family 只保留为词汇 compatibility/cooldown 维度。
7. 已确认/unknown 消息的先后顺序以 typed remote fact `observed_at` 为首选，其次才是 execution/action 时间；不得以 Action 创建顺序冒充 Telegram 可见顺序。
8. participation rate 的 effective day、pending promotion 与计划 owner 全部使用任务 timezone。只要当前任务日已有冻结 plan，无论任务此刻 running/paused/stopped，修改都只能进入下一任务日；到达 `effective_task_day` 后读写路径必须原子晋升 pending，Task API 不得继续返回过期 current。
9. 同一 task day 存在多个 route/account plan 时，问题窗口、topic/teacher remote ratio、unknown hold 与详情读模型都必须聚合全部有效 plan；页面分别展示 `confirmed`、`unknown_hold`、`capacity_denominator` 与最坏比例，禁止用 confirmed-only 的 `0/3` 文案配上包含 unknown 的 `25%`。

第四轮新增验收反例：

| ID | 场景 | 必须结果 |
|---|---|---|
| TOP-016 | 三条 active normal 尚未发送，第 4 条 configured-topic 准备进 Gateway | 按 Telegram 可见前缀判定为 1/1 并阻断，不得按 reservation 误判为 1/4 |
| TOP-017 | configured-topic unknown hold 后 replacement Action | 复用同一 intent/quantity identity，unknown 只计一次且禁止重放 |
| CNT-011 | 无当前来源、无历史 reply 的 3 个 generic slots | planner 直接冻结 3 个 question；满足窗口才生成，不因普通 act cycle 自相矛盾 |
| CNT-012 | 当前来源为空但仍有历史真人 reply target | 继续 human-context reply，不进入 generic warmup 整批阻断 |
| VOC-011 | 一个消息 reservation 含 2 个 sample，最近有 20 条消息 | 冷却窗口覆盖完整 20 条消息，不缩短成 10 条 |
| VOC-012 | remote-confirmed 文本未使用已 reservation 的词 | 该词不计 confirmed 历史命中；active intent 仍按 reservation 防并发重复 |
| VOC-013 | stance 缺失或 context-bound 无事实锚点 | manifest/采样 fail closed 或返回空样本，不得选出不兼容词 |
| CFG-010 | paused task 当日已有 plan 后修改 rate | 当前 plan/current readback 不变，pending 在任务时区下一任务日晋升 |
| READ-006 | 同日两个 route plan 且其中一个有 unknown topic | 聚合展示 confirmed、unknown hold、capacity denominator 与最坏比例，问题 owner 不重置 |

本地证据只证明 schema、owner、Prompt、worker 接线和 read model 的确定性行为，不替代以下 Gate：

- §10.2 的真实目标 Provider 手工原型、人工评分、P50 token 与相对基线质量指标尚未执行；
- PostgreSQL `FOR UPDATE` 并发竞争、迁移升级/降级、真实 route 全矩阵与 group-visible surface-frequency 压测尚未取证；
- CI、`master -> release`、生产 runtime、单 Task canary 和七个 task day Telegram typed remote fact 均未执行，因此 `production_fixed=unproven`。

---

*本 PRD 已有本地实现与定向测试证据，但 CI、release、runtime 和 Telegram 业务效果仍须分别取证；不得由本地 QA 推导为生产完成。*

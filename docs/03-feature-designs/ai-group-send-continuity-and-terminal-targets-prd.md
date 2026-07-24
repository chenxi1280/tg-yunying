# AI 活群发送连续性与终态目标处置优化 PRD

## 1. 文档状态

| 项目 | 内容 |
| --- | --- |
| 需求级别 | L3 生产可靠性与目标处置优化 |
| 设计状态 | `complete` |
| 修订说明 | 2026-07-24 评审修订合订：义务归属 credit、unknown 占位、planning_rate / 公平调度、Phase A 身份灰度、多目标粒度、canary 体感；§5.5 青岛 `qdsfxy` 预置 `target_ref_invalid`（非解散）；§5.1/§5.4 补 invalid 任务·Action·覆盖处置与自动写入门槛；§7.3.4 明确失败 Action 再规划；§7.5 死锁 / §9.0 时区为前置基线；§9 发布插队顺序；总 PRD / 实现计划 / 数据流同步同一口径 |
| 产品范围 | Phase A 覆盖所有 Telegram 出站入口的终态目标拦截；Phase B 覆盖 `group_ai_chat` 的每小时硬目标连续履约 |
| 统计时区 | 任务配置时区；未配置时沿用平台 `Asia/Shanghai` 口径 |
| 上位文档 | `docs/01-product/tg-ops-platform-prd.md` §8.4 |
| 关联文档 | `ai-group-all-accounts-daily-coverage-prd.md`、`ai-group-hard-hourly-target-prd.md`、`docs/00-index/project-dataflow-index.md` |
| 实现计划 | `docs/superpowers/plans/2026-07-24-ai-group-send-continuity-terminal-targets.md` |

本文对“目标生命周期、引用版本、跨小时硬目标、未知发送、群发送策略、灰度发布”具有专项优先级。关联专项 PRD 中与本文冲突的旧口径，仅在未启用本能力的历史运行路径下保留为历史说明，不得用于新实现。

## 2. 背景与问题

当前 AI 活群存在四类会让任务长期无法收口或显示失真的问题：

1. 群日限额和群冷却对所有账号共同生效，一个账号占槽后会阻断其他可用账号。
2. 硬小时 Action 跨过原计划小时会被跳过，最近 24 小时统计又会让更早缺口消失。
3. Telegram 调用已开始但本地结果未知时，若把它从开放动作中移除又继续补量，会重复发送；若永久算作开放动作或整目标停规划，又会让任务长期卡死。
4. 群已解散、目标引用失效、权限不可访问被混为同一种错误，导致系统反复尝试或错误显示完成。

## 3. 目标、范围与非目标

### 3.1 目标

- 对同一 Telegram 目标建立可审计的生命周期和引用版本，避免旧引用、旧欠账和新引用混算。
- 已人工确认解散的群立即停止未进入 Telegram Gateway 的出站动作，界面统一提示“群里已被解散，已跳过本目标”。
- AI 活群每小时硬目标以可持久化、可重算的真实成功账本处理跨小时欠账；不因 24 小时展示窗口而丢失事实。
- **未完成的硬小时义务跨小时继续履约，直到该计划桶的义务被成功 credit 关闭、目标终态、引用被 supersede 或任务被显式停止。**
- 任何成功计数必须由成功 `ExecutionAttempt` 和非空 Telegram 远端消息 ID 支撑。
- 不破坏账号容量、活动时段、目标权限、内容质量、Telegram 风控和既有严格搜索任务的调度优先级。

### 3.2 分期范围与用户体感边界

| 阶段 | 范围 | 交付结果 | 用户体感 |
| --- | --- | --- | --- |
| Phase A：目标终态 | `group_ai_chat`、`group_relay`、listener 自动回复、`message_tasks`、Campaign / 旧任务兼容发送、人工发送 | 所有 Telegram 出站动作进入 Gateway 前都经过同一 `OutboundTargetGate`；已解散 / 引用无效目标不再发出新消息 | 解散群停止重试；**默认不改变**同群多账号互挡 |
| Phase B：硬小时连续性 | 仅 `group_ai_chat` | 目标版本隔离、小时桶、义务归属 credit、跨小时欠账、unknown 占位、任务中心展示和公平调度 | 跨小时继续补齐；仍受账号容量与活动窗口约束 |
| Phase B canary：`account_only` | 运营显式选定的合格群 | 关闭群冷却（及可选关闭群日限额） | **这才是“账号 A 不挡 B/C/D”的体感修复**；默认全量群仍为 `legacy_group_slot` |

### 3.3 非目标

- 不根据 `PEER_INVALID`、群名称、标题相似度或一次账号视角自动判断“群已解散”。
- 不删除历史 Action、ExecutionAttempt、远端消息 ID、账本 credit、原始 Telegram 错误或生命周期审计。
- 不把跳过、`unknown_after_send`、AI draft 就绪、Action 已创建或任务 `paused` 伪装为成功或完成。
- 不用硬小时目标绕过账号小时/日容量、同账号并发、活动窗口、内容门禁或 Telegram Gateway 最终校验。
- 不把 Phase B 的硬小时账本扩展到频道评论、浏览、点赞或转发监听的产量目标。
- 不把发布锚点之前的历史统计缺口虚构为可追讨 debt。
- 不把搜索点击 / 评论产量问题纳入本专修（调度优先级保持既有严格顺序，但不改其产量算法）。

## 4. 核心定义与事实源

### 4.1 目标身份与引用版本

`OperationTarget` 是租户内运营目标的身份真相源。每次生效引用变更或受控重新激活都递增 `reference_revision`，从 `1` 开始。

```text
Target identity = tenant_id + operation_target_id
Target epoch    = tenant_id + operation_target_id + reference_revision
```

任务配置固定 `target_operation_target_id` 与 `target_reference_revision`；每个出站 Action 固定同一对值，且携带创建时的目标引用快照。不得仅依赖会被后续编辑覆盖的 `target_group_id`、title 或 username 决定实际发送对象。

引用变更后：

- 尚未进入 Gateway 的旧 revision Action 标记 `skipped / target_reference_superseded`，不改绑到新引用。
- 已进入 Gateway 的旧 revision Action 保持原结果或 `unknown_after_send`，由远端核验收口。
- 旧 revision 的硬小时欠账归档为历史事实，不自动转移到新 revision。
- 新 revision 从生效时所在小时建立新桶和新基线，只有重新通过目标能力检查后才可规划。

### 4.2 目标生命周期

`lifecycle_status` 只有以下状态：

| 状态 | 含义 | 是否允许新出站 |
| --- | --- | --- |
| `active` | 当前引用可继续按正常准入、能力和策略校验 | 允许，仍须通过所有出站门 |
| `target_ref_invalid` | 有确定的引用解析证据，当前引用需要运营人员更新 | 不允许对该引用自动重试发送或准入 |
| `group_dissolved` | 运营人员已根据外部证据确认群不存在 | 不允许；未进入 Gateway 的动作必须跳过 |

`target_resolution_unverified` 是诊断 / blocker，不是生命周期终态：例如 `ChannelInvalidError`、无访问权限、单账号无法解析实体、同步数据矛盾等场景，生命周期仍保持 `active`，系统必须保留原始错误并要求只读探测或人工判断。

### 4.3 发送事实与未知结果

| 名称 | 事实要求 | 是否可计成功 | 规划影响 |
| --- | --- | --- | --- |
| 已确认成功 | `Action.status=success`、`ExecutionAttempt.status=success`、远端 message id 非空，且目标 revision 一致；credit 写入**计划桶** | 是 | 关闭 1 个计划义务 |
| 可抵扣开放动作 | 尚未进入 Gateway 且目标、账号、内容、引用 revision 都仍有效的 `pending/claiming/executing` 动作 | 否 | 计入 `eligible_open_count` |
| `unknown_after_send` | 已进入 Gateway，但本地无法确认远端结果 | 否 | 计入 `unknown_after_send_hold_count`（占位 1，**禁止替代重发该 Action**）；**不**整目标停规划 |
| 已跳过 | 尚未进入 Gateway 时被终态、失效引用或显式停止拦截 | 否 | 不占开放、不计成功 |

成功永远不是 UI toast、AI 文案、Action payload 或任务状态的推断结果。

### 4.4 硬小时目标粒度

| 任务形态 | `hourly_min_messages` 含义 | 桶与欠账 |
| --- | --- | --- |
| 单目标 `group_ai_chat` | 该任务对该唯一目标的每小时义务 | 一任务一目标一 epoch 一套桶 |
| 多目标 `group_ai_chat` | **每个目标各自**承担完整的 `hourly_min_messages`（不做任务内均分） | 桶键含 `operation_target_id`；debt / planning 按目标 epoch 独立计算 |

若未来产品要“多目标共享一个小时总量”，必须另开需求改配置模型；本专修不实现共享池。

## 5. 目标处置流程

### 5.1 错误分类规则

Telegram Gateway 与目标生命周期服务必须保留原始异常名称、原始 detail、账号、目标、引用 revision 和 trace。分类规则如下：

| 证据 | 系统处置 |
| --- | --- |
| 明确的 username / 链接语法无效、精确 username 不存在、绑定目标缺少必要 peer 且无法解析 | 标记 `target_ref_invalid`，停止该 revision 自动重试，并执行 §5.1.2 侧效应 |
| `PeerIdInvalidError`、`ChannelInvalidError` 或类似错误但无法区分“引用错误 / 无权限 / 账号视角不可见” | 写 `target_resolution_unverified`，不自动标记引用无效或群解散 |
| 人工核对可访问入口、管理员确认或可信外部证据确认群已解散 | 运营人员显式标记 `group_dissolved` |

不得实现 `PEER_INVALID -> group_dissolved` 的隐式规则。

### 5.1.1 自动写入 `target_ref_invalid` 的证据门槛

系统**可以**在满足下列**全部**条件时自动写 `target_ref_invalid`（仍须写审计与原始错误）：

1. 错误可归因于**绑定引用本身**（精确 username 不存在、链接/username 语法无效、目标缺少必要 peer 且无法解析），而非单账号被踢、禁言、无权限、FloodWait、网络超时。
2. 错误绑定到精确 `operation_target_id + reference_revision`，不得按标题或模糊 peer 推断。
3. 不得仅因**单个账号**一次失败就写引用无效：若同目标同 revision 下仍存在其他账号近期 `can_send=true` 或成功发送证据，只记账号侧失败 / `target_resolution_unverified`，不升级生命周期。
4. 模糊 `PEER_INVALID` / `ChannelInvalidError` **禁止**自动写 `target_ref_invalid`。

运营人员也可通过**受控预置接口**（与 §5.2 同属 lifecycle 专用面，不得混入通用编辑表单）对精确目标提交 `lifecycle_status=target_ref_invalid`、`reason`、`evidence_ref`、`expected_lifecycle_version`，用于发布前数据操作（如 §5.5）。预置与自动写入触发同一套 §5.1.2 侧效应。

### 5.1.2 `target_ref_invalid` 写入后的任务 / Action / 覆盖处置

与 `group_dissolved` 对称但文案与恢复路径不同：

| 对象 | 处置 |
| --- | --- |
| 未进入 Gateway 的同租户、同目标、同 revision 出站 Action | `skipped / target_ref_invalid`；用户可见引导为“目标引用无效，请更新有效邀请链接或用户名”，**不得**使用解散文案 |
| 已进入 Gateway 或 `unknown_after_send` | 不改写为 skipped；保留远端核验 |
| 每日覆盖行 | 保留在分母；`blocked / target_ref_invalid`，`next_eligible_at=null`；不得 `release` 回 `ready` |
| 单目标任务 | `paused`（或等价结构 blocker + 清空下一次硬小时检查），`last_error` 指向引用无效；**不得** `completed`，也不得在无效引用上继续硬小时规划堆 debt |
| 多目标任务 | 仅跳过该目标 / 该 revision；其他 active 目标继续；任务整体不因单目标 invalid 写 `completed` |
| 新规划 / claim / Gateway | `OutboundTargetGate` 一律拦截该 revision |
| 恢复 | 仅 §5.4 引用修复；不得静默清除 |

### 5.2 “标记群已解散”

运营目标详情提供受控操作，要求：

1. 仅 `target_type=group` 且拥有 `targets.manage` 的用户可操作。
2. 提交 `expected_lifecycle_version`、必填 `reason` 和可追溯 `evidence_ref`；版本不一致返回 `409`，前端必须刷新后重新确认。
3. 首先执行无副作用 impact preview，展示该精确租户目标 / revision 下：待执行和未开始 Gateway 动作数、Gateway 已开始 / unknown 动作数、受影响覆盖预约数、将暂停的单目标任务数。
4. 仅在用户确认 preview 后写 `group_dissolved`、审计和动作状态；不能由前端本地 state 提前显示成功。
5. 所有尚未进入 Gateway 的相关动作写 `skipped / target_group_dissolved`，错误文案固定为“群里已被解散，已跳过本目标”。
6. 已进入 Gateway 或 `unknown_after_send` 的动作不可改写为 skipped，保留远端核验路径。
7. 单目标任务暂停并清空下一次硬小时检查；多目标任务仅跳过该目标。二者都不得写 `completed`。

每日覆盖行保持在分母，写 `blocked / target_group_dissolved` 和 `next_eligible_at=null`；不得调用会把覆盖预约释放为 `ready` 的通用函数。

### 5.3 `group_dissolved` 重新激活

`group_dissolved` 不能自动恢复。运营人员必须提交新的或重新核验后的引用、理由和当前版本；服务端递增 `reference_revision`，生成审计事件，并要求至少一个可用账号完成目标能力 / `can_send` 检查后才可恢复为 `active`。旧 epoch 的未完成 debt、Action 和成功 credit 永不转移。

### 5.4 `target_ref_invalid` 恢复

`target_ref_invalid` 也不是可自动静默清除的状态。恢复路径：

1. 拥有 `targets.manage` 的用户通过**专用引用更新 / 修复接口**提交新的或重新核验后的真实 peer / username / 邀请链接、理由与 `expected_lifecycle_version`。
2. 服务端递增 `reference_revision`，写审计；旧 revision 未开始 Gateway 的 Action 标 `target_reference_superseded`（若尚未按 §5.1.2 跳过）。
3. 在至少一个可用账号通过目标能力 / `can_send` 检查前，生命周期可先为 `active` 但硬小时与出站仍受能力门禁阻挡；检查通过后正常规划，并允许解除单目标任务的 pause / 结构 blocker。
4. 仅改展示标题、备注或不改变实际发送引用的字段，**不得**清除 `target_ref_invalid`，也不得递增 revision。
5. 旧 epoch 的 debt / credit / unknown 事实不迁移到新 revision。

预置入口、自动写入与引用修复均须返回目标 ID、revision、lifecycle 版本、影响计数与 trace；全部写审计。

### 5.5 青岛师范学院案例

“青岛师范学院”的 `qdsfxy` 报错 `No user has "qdsfxy" as username` 属于 §5.1 定义的“精确 username 不存在”证据，只构成当前引用失效，**不构成群解散证据**。本次上线将该目标作为**产品已批准的精确数据操作**处理：

1. 发布前只读核对租户、`OperationTarget` ID、peer、username 和原始错误（不用名称模糊匹配）。
2. 经受控 lifecycle **预置接口**写入 `target_ref_invalid`（**不是** `group_dissolved`），填写原因与证据引用，再启用 Gate。当前命令入口为 `backend/scripts/preseed_target_ref_invalid.py`，强制同时传入 `--tenant-id --target-id --expected-peer-id --expected-username --expected-lifecycle-version --reason --evidence-ref`；任一精确字段不匹配即失败，默认不执行名称匹配或批量预置。
3. 第一轮调度按 §5.1.2：未开始 Gateway 的相关动作 `skipped / target_ref_invalid`；单目标任务 pause / 结构 blocker；覆盖行 `blocked / target_ref_invalid`；页面与任务详情引导**引用修复**，文案为“目标引用无效…”，**禁止**显示“群里已被解散，已跳过本目标”。
4. 操作只作用于精确目标 ID，不把 `qdsfxy` 或同名群写成代码特例。
5. 若后续取得群已解散的**独立外部证据**，再由运营人员按 §5.2 显式标记 `group_dissolved` 并改用解散文案。

## 6. 统一出站门与群发送策略

### 6.1 `OutboundTargetGate`

所有 Telegram 出站路径必须在规划、claim 和 Gateway 调用前使用同一门禁，至少检查：

```text
tenant isolation
-> operation_target_id + reference_revision match
-> lifecycle_status
-> active window
-> target capability / membership precondition
-> account capacity and concurrency
-> task / content / risk policy
```

该门禁覆盖 Phase A 范围内所有发送入口，不能只在 `group_ai_chat` 中实现后宣称“所有任务都已跳过”。

### 6.2 目标身份解析与阻断灰度

历史出站路径可能缺少稳定 `OperationTarget` 映射。处置分阶段，禁止按标题模糊匹配放行：

| 阶段 | 无法唯一解析目标身份时的行为 |
| --- | --- |
| feature-off / dual-read | 只记 `target_identity_unresolved` 诊断与指标，**不阻断**既有发送（避免误伤） |
| Phase A canary | 对已绑定 OT 的路径强制 lifecycle；未绑定路径继续诊断，按入口白名单逐步 enforce |
| Phase A 全量 | 无法唯一解析时硬阻断 `target_identity_unresolved`；全量前必须有 unresolved 存量清零 / 可接受阈值 |

同租户、同 peer 的**唯一**补解析允许使用；不唯一或无 peer 时不得猜测。

dual-read / canary 的诊断必须落到 Action `result.outbound_target_gate_diagnostic`，或对没有 Action 的直接发送入口写 `AuditLog`；不得只写 worker 日志后丢失。`full` 的配置保存必须先检查未进入 Gateway 的 Action、消息任务和运营尝试身份库存，存在 unresolved 行即拒绝切换。

### 6.3 群发送策略

| `send_limit_mode` | 群日限额 | 群冷却 | 账号自身容量 / 冷却 | 活动时段 |
| --- | --- | --- | --- | --- |
| `legacy_group_slot` | 启用 | 启用 | 始终启用 | 始终启用 |
| `account_only` | 不启用 | 不启用 | 始终启用 | 始终启用 |
| `account_only_with_group_daily_limit` | 启用 | 不启用 | 始终启用 | 始终启用 |

第一版发布时，所有已存在和新同步的群都显式保持 `legacy_group_slot`。只有运营人员在合格 canary 目标上显式选择 `account_only` 或 `account_only_with_group_daily_limit` 后，才改变策略；不得把“新群”静默默认成新模式。

变更 `send_limit_mode` 需要可审计权限（建议不低于群策略管理权限，且写审计：操作者、前后模式、目标/群 ID）。`account_only` **不是**无限制发送：活动时段、账号日/小时容量、同账号并发、内容与风控、Telegram FloodWait/SlowMode 仍全部生效。Telegram 返回的单账号限制只延后该账号对应 Action，不得重新引入无 Telegram 证据的全群固定冷却。

Canary 观察项至少包括：多账号近同时发言是否引发群慢速 / 平台风控、重复消息率、远端成功 message id 与 credit 一致性。

## 7. 硬小时连续性账本（Phase B）

### 7.1 小时桶与配置版本

`Task.config_revision` 是服务端维护的单调版本：目标引用、任务时区、硬小时目标或有效硬小时策略发生保存时原子递增，客户端不能自行指定。硬小时桶固定该版本，变更只创建后续桶。

硬小时桶唯一键：

```text
(tenant_id, task_id, operation_target_id, target_reference_revision, bucket_key)
```

桶保存 `bucket_start`、`bucket_end`、任务时区、目标值、`task_config_revision`、终态 blocker 和汇总计数。任务时区、目标值或策略变更仅从下一个小时桶生效，不得改写历史桶或已创建 Action 的归属。

### 7.2 精确一次成功 credit（义务归属）

建立 `TaskHardHourlyDeliveryCredit`：

```text
UNIQUE(action_id)
INDEX(bucket_id, executed_at)
```

credit 包含：

- `bucket_id`：**计划桶**（与 Action payload 的 `hard_hourly_bucket` / 创建时义务一致）
- `action_id`、`execution_attempt_id`
- `executed_at`：实际成功时间（审计与展示）
- `remote_message_id`：非空

只有在同一短事务中确认成功 Attempt 与非空远端 message id 后，成功插入 credit 才能增加**计划桶**的 `success_count`。重试、补偿核验、重复回写或多 worker 竞争都不能让一条 Telegram Action 产生两份成功计数。

**产品定论：义务归属，不是“实际小时产量桶”。**

| 维度 | 规则 |
| --- | --- |
| 关闭义务 | credit 写入并增加 **计划桶** `success_count` |
| 实际时间 | `executed_at` 仅用于审计、SLA、页面“实际发送时间” |
| 跨小时成功 | 14:55 计划、15:03 成功 → **14 点桶缺口下降**；**不得**因 credit 把 15 点桶 `success_count` +1 而留下 14 点永久 debt |
| 展示 | 可同时展示“计划小时完成度”与“实际发送时间线”；完成判定只看计划桶义务 |

禁止实现“credit 进实际小时 + 计划桶仍计债务”的双计模型。

原始 credit 和关联 Attempt 按执行事实保留策略归档，不得以清理为名删除或重算已确认成功；小时桶汇总和审计链必须持续保留。

### 7.3 欠账、规划速率与未知发送

#### 7.3.1 定义

```text
current_hour_deficit =
  max(goal_current_bucket - confirmed_success_current_bucket, 0)

durable_debt =
  sum over past buckets in same (task, target epoch):
    max(goal - confirmed_success, 0)
  # 排除：已终态目标、superseded epoch、发布锚点前仅作历史展示的缺口

eligible_open_count =
  尚未进入 Gateway、且目标/账号/内容/revision 仍有效的
  pending/claiming/executing 硬小时 Action 数（按目标 epoch）

unknown_after_send_hold_count =
  同一目标 epoch 下、仍未核验收口的 hard-hour unknown_after_send 数
  # 每个 unknown 只占 1 个规划名额，禁止对同一 Action 替代重发

planning_reservation = eligible_open_count + unknown_after_send_hold_count

planning_rate =
  hourly_min_messages
  + min(durable_debt, hourly_min_messages)
# 继承并替代旧 hard_hourly 的 planning_rate / backfill 上限语义；
# durable_debt 取代仅 24h 窗口内的 backfill 统计作为债务事实源。

required_new = min(
  planning_rate,
  max(0, current_hour_deficit + durable_debt - planning_reservation)
)
```

#### 7.3.2 未知发送（收窄后的语义）

- `unknown_after_send` **不计成功**、**不自动重发**、**无静默超时释放**。
- 每个 unknown **只占位 1**，通过 `planning_reservation` 防止对该义务再规划一条替代消息。
- **不得**因为存在任意一条 unknown 就令 `required_new = 0` 或整目标 epoch 停止全部硬小时规划。
- UI 对存在 unknown 的目标 / 任务展示 `awaiting_confirmation`（或部分待核验），与“仍可补其余缺口”可同时成立。
- 只读远端核验或运营基于证据裁决后：确认成功则 credit 入**计划桶**；确认未发送则按产品既有规则释放占位并允许对该义务重新规划；确认失败则记失败且不计成功。
- 必须提供核验入口、人工裁决路径，以及对 `unknown_after_send_hold_count` / 最早 unknown 年龄的告警；没有“超时当成功”或“超时当失败后静默丢弃证据”的路径。

#### 7.3.3 跨小时继续与容量窗口

- 跨小时 Action 若尚未进入 Gateway 且引用、目标、账号和内容仍有效，可以继续调度；**不得再写** `hard_hourly_bucket_expired`。
- 当前 tick 只按一次 `required_new` 创建有限 Action，并写下一次检查点，不得 `while required_new > 0` 循环即时补发。
- 活动窗外或账号容量打满时：**欠账只累计不发送**；达标可能跨多个自然小时甚至自然日。任务不得因“仍有 debt 但当前不可发”被标为 `completed`。
- 硬小时不得再走 `enforce_capacity=False` 或任何容量绕过路径。

#### 7.3.4 明确失败 Action 与再规划

- 明确失败（`failed` / 可判定的非 Gateway 未知终态）的硬小时 Action：**不计成功**、**不占** `planning_reservation`。
- 在目标仍为 `active`、引用 revision 仍有效、计划桶义务未关闭时，由下一 tick 的 `required_new` **受控重建**新的 Action；不得把失败伪装成成功关闭义务。
- 重建必须遵守既有 dedupe / 退避 / 账号冷却 / 内容质量门禁：不得对同一失败 Action 原地无限重试而不退避，也不得复用已失败 Attempt 冒充新发送。
- 因 `target_ref_invalid` / `group_dissolved` / `target_reference_superseded` 跳过的 Action 不进入“失败再规划”，只走对应生命周期恢复路径。

### 7.4 调度公平性

严格搜索日目标的既有顺序保持：

```text
target_admission_retry
-> search_join_membership
-> search_join
-> AI hard-hourly
-> ordinary
```

**“一轮”的可测定义：** 在 Dispatcher 的**一次 claim 类别选择**（一次持久化 cursor 决策）中，若本次选择了 AI hard-hourly，且当时存在已到期的 ordinary 动作、且无更高优先级项，则**下一次** claim 类别选择在同样前提下必须选择 ordinary。不得用“一个 worker 进程内碰巧夹杂”或“批次里碰巧有一条 ordinary”冒充公平。

调度 cursor、前次类别和选择 reason 必须持久化并可在运行统计查询。

锁序对齐（§7.5）**不得**改变本节严格搜索优先级，也不得借 fast_track 把普通任务整体插到搜索类动作之前。

### 7.5 并发与事务边界（死锁收敛）

修复前，线上 planner / recovery 的 fast_track 与 dispatcher claim 在 `actions` 表上以不同行锁顺序并发，产生 PostgreSQL 死锁（批量 `UPDATE actions SET scheduled_at, result WHERE id=...`，200+ 组参数），中断规划导致 Action 堆积未调度，进而被写为 `hard_hourly_bucket_expired`。本节为 Phase B 前置基线要求。

**修复前根因**：fast_track（recovery 的全局扫描、planner 的 task+channel 范围）把一批 pending Action 的 `scheduled_at` 从未来拉到现在并改写 `result`；同时 dispatcher 的 `claim_actions` 用 `SELECT ... FOR UPDATE SKIP LOCKED` 后逐行 `UPDATE actions SET status, claim_*, result`。两个事务 UPDATE 同一批行，但行锁获取顺序不同（fast_track 按 `scheduled_at, created_at`，dispatcher 按 `claim_rank, priority, scheduled_at, created_at`），形成循环等待。

**收敛要求**：

1. 新建的 hard-hourly membership Action 直接按 claim 顺序排入近期时刻，不得在 Planner 中再批量改写既有 Action。仅 Recovery 的 fast_track 可改写既有 future Action 的 `scheduled_at` / `result`；它必须**分批提交**，单事务处理行数不得超过配置阈值（建议 ≤ 50），且每批提交后短暂释放锁，不得一次性 executemany 数百行。
2. fast_track 与 dispatcher claim 的行锁顺序必须**一致**：fast_track 的 SELECT 排序键必须与 dispatcher `claim_actions` 的 ORDER BY 对齐（至少前置 `claim_rank, priority, scheduled_at, created_at`），避免不同事务以不同顺序争抢同一批行锁。
3. fast_track 的事务边界必须**只覆盖 Action 行更新**，`FOR UPDATE` 只锁 `actions`；任务统计、小时桶汇总、credit、cursor 持久化均在 Action 提交释放锁后的独立短事务中完成。
4. Phase B 新引入的 `TaskHardHourlyDeliveryCredit` 插入、小时桶 `success_count` 自增必须使用短事务，且按 `bucket_id` 串行化（建议 `SELECT ... FOR UPDATE` 锁桶行后再 `INSERT credit` + `UPDATE bucket`），避免多 worker 对同桶并发插入 credit 产生死锁或重复计数。
5. 调度 cursor 持久化（§7.4）先在独立短事务写入本次 claim 类别选择并提交，再进入 `Action FOR UPDATE`；Action 上只写同一选择的审计字段，cursor 读写不得阻塞 Action 行锁。
6. planner 批量创建新 Action（含 `_reactivate_auto_verification_memberships` 的 `bulk_insert_mappings`）不受 Action 行 UPDATE 死锁影响，但同一事务内不得再 UPDATE 其他已存在 Action；INSERT 与 UPDATE 必须分事务。

**验收（可测门槛）**：

- canary / 预发：连续 **≥ 2 小时** 或 **≥ 100 次** planner/recovery drain 无新增 `DeadlockDetected`；
- fast_track 单事务 UPDATE 行数硬上限默认 **50**（可配置但须可观测），超限必须分批；
- dispatcher claim 无因 fast_track 长持锁导致的持续 `lock_timeout`；
- 运行指标：`deadlock_detected_total`、fast_track 批大小、`lock_timeout` 次数可查。

## 8. 页面、接口与审计

### 8.1 页面要求

- 运营目标列表和详情显示 `正常`、`目标引用无效`、`群已解散` 三类状态及最近审计摘要。
- `target_ref_invalid` 引导**引用修复**（§5.4），禁用依赖该引用的发送 / 建群 AI / 准入重试，**不是**“标记解散”文案；`group_dissolved` 显示终态原因与解散文案。
- 任务中心显示：本小时计划桶 `confirmed_success / goal`、历史 `durable_debt`、可抵扣开放动作、`unknown_after_send_hold_count`、目标 revision、终态 blocker、最早 debt 时间、下一步可执行条件；有跨小时成功时展示实际发送时间但不改计划桶完成语义。
- 存在 `durable_debt` / unknown / 终态 / 容量不足时，状态文案区分“补发中”“待远端核验”“账号容量不足”“活动窗外”“目标引用无效”“目标群已解散”；**禁止**渲染为已完成。
- `target_ref_invalid` 与 `group_dissolved` 对应任务均显示暂停 / 阻塞而不是完成；二者颜色、文案、可操作项必须不同。
- Action 详情保留原始错误、终态动作和远端核验入口。
- 群策略编辑展示各模式仍受活动时段和账号容量约束，并明示：默认 `legacy_group_slot` 下同群账号仍可能互挡；仅 `account_only*` canary 解除群冷却互挡。

### 8.2 接口与审计契约

下列操作均走专用 lifecycle / 引用接口，不得混入通用目标编辑表单：

- `group_dissolved` 预览与确认（§5.2）
- `target_ref_invalid` 受控预置（§5.1.1）与引用修复（§5.4）
- `group_dissolved` 重新激活（§5.3）

均必须返回目标 ID、引用 revision、生命周期版本、影响计数、原始证据引用和 trace。所有状态变更写审计：操作者、理由、证据、前后 lifecycle、前后引用、受影响 Action 数和任务数。

## 9. 数据迁移、灰度与回滚

**推荐发布顺序（插队后的完整链路）：**

```text
0a. 时区一致性基线修复（可独立发布，不依赖 feature Gate）
0b. fast_track / claim 死锁收敛（可独立发布或与 Phase A 并行，须在 Phase B 全量前完成）
1. schema + dual-read，ai_group_send_continuity_v1=off
2. 可重跑迁移与 target identity / open Action 桶锚点 reconcile
3. Phase A canary（已绑定 OT 路径 enforce lifecycle）
4. Phase A 全量（target_identity_unresolved 存量可接受后硬阻断）
5. 青岛等精确目标预置 target_ref_invalid（§5.5），再观察一轮调度
6. Phase B canary（硬小时账本）
7. 显式 account_only* 群策略 canary
```

任一步失败停在该阶段；worker 不得静默读取半迁移语义。

0. **时区一致性基线修复（前置 0a）**：Phase B 账本（小时桶、credit、debt、unknown 核验、cursor）全部依赖 datetime 比较。线上 unknown_after_send 处置路径已出现 `can't compare offset-naive and offset-aware datetimes`（ExecutionAttempt failure_detail 实证）；代码中以 `replace(tzinfo=None)` 做“归一化”的模式属系统性隐患。进 Phase B 前必须完成：
   - 统一所有 datetime 比较入口为 offset-aware；DB 读取的 `DateTime(timezone=True)` 字段保留 tzinfo，运行时构造的时间一律带 task zone 或 UTC tzinfo。
   - 消除 `replace(tzinfo=None)` 作为归一化手段的模式；确需比较时用 `astimezone(common_zone)` 而非剥离 tzinfo。
   - 验收以回归用例 + 生产 `datetime_timezone_compare_error_total=0` 为准，不绑死源码处数。
   - 此修复不依赖 Phase B feature Gate，须独立先行发布并回归。
0b. **死锁收敛（前置 0b）**：落实 §7.5；验收门槛见该节。须在 Phase B 全量前完成。
1. 先部署可向后兼容的 schema、读路径和 feature-off Gate；不得让 worker 在 schema 未准备时读取新语义。
2. 再执行可重跑的数据迁移：补齐目标 lifecycle / revision、Action target snapshot、当前 open Action 的可追溯桶锚点和账本索引。先运行 `backend/scripts/reconcile_outbound_target_identity.py --tenant-id <tenant>` 获取只读 inventory，再以 `--apply` 只回填同租户、精确 peer / channel target 匹配的无 Gateway 行；非零但找不到的显式 target ID 绝不猜测性回填。迁移版本号以实现时 Alembic head 为准（计划稿中的编号仅为草案）。
3. 历史 `hard_hourly_bucket_expired`、旧 skipped 记录保持历史，不自动复活；**发布锚点之前的缺口只作为历史展示，不虚构为新 debt**。生产 E4 只验收锚点之后的连续履约。
4. 全部群先保持 `legacy_group_slot`；选定非问题目标做显式 canary 后再切换 `account_only*`。
5. `ai_group_send_continuity_v1` 必须是运营可见、可审计的启用状态，而不是 silent fallback。
6. 回滚只停止新路径或把明确 canary 切回 `legacy_group_slot`；不删除 lifecycle 审计、账本、credit 或未知发送事实，也不自动恢复已解散或引用无效目标。
## 10. 观测与验收

### 10.1 运行指标

至少输出并展示：

- `hard_hourly_durable_debt`、`hard_hourly_oldest_debt_age`、`hard_hourly_unknown_after_send_hold_count`、最早 unknown 年龄；
- `target_terminal_skipped_total`、`target_ref_invalid_total`、`target_resolution_unverified_total`、`target_identity_unresolved_total`；
- `dispatch_claim_share_by_task_class`、调度 cursor 和连续选择原因；
- `send_limit_mode`、目标 revision、终态 blocker、最近 remote message id；
- `pass / blocked / unproven` 的生产证据状态；
- 并发健康：`deadlock_detected_total`、fast_track 批量 UPDATE 单事务行数、dispatcher `lock_timeout` 次数；
- 时区健康：`datetime_timezone_compare_error_total`（offset-naive/aware 比较异常计数）。

### 10.2 QA 验收

- 同一 Action 的多次成功回写只能产生一个 hard-hour credit，且只增加**计划桶**成功数一次。
- 14:59 计划、15:02 成功：14 点计划桶缺口下降；15 点计划桶不因该条 credit 被误记成功；页面可查实际发送时间。
- 24 小时后历史欠账仍参与当前 epoch 的受控规划（锚点之后产生的 debt）。
- 目标有 1 条 `unknown_after_send` 时：该义务不替代重发，但其余 `required_new` 仍可按公式创建；不得整目标 `required_new=0`。
- 远端核验成功后只入计划桶 credit 一次；核验确认未发送后才允许对该义务重新规划。
- 当前引用变更后旧 revision 的未开始 Action 不会发往新引用，旧 debt 不会迁移。
- `target_ref_invalid` 仅能通过引用修复恢复；仅改标题不能清除。
- 写入 `target_ref_invalid` 后：未开始 Action 为 `skipped / target_ref_invalid`、覆盖 `blocked`、单目标任务 pause/blocker，且文案不是解散。
- 单账号权限/被踢类失败不得把整目标升级为 `target_ref_invalid`。
- 明确失败 Action 不占 reservation，下一 tick 可受控重建；不得失败当成功。
- `PEER_INVALID` 的模糊情况不自动写 `group_dissolved`；`qdsfxy` 预置为 `target_ref_invalid` 并引导引用修复，群解散需独立外部证据；页面不得显示解散文案。
- 标记解散只影响同租户、同目标、同 revision 的未开始出站动作；已进入 Gateway 的动作保持核验。
- listener 自动回复、手动发送和 Campaign 等非 AI 活群入口在 Phase A 全量后同样被已解散 / 引用无效目标拦截；feature-off 阶段 unresolved 不误伤。
- 明确 canary 目标切换 `account_only` 后，同群不同账号只受自身容量、活动时段和最终风险门限制；未切换群保持旧策略。
- 当普通任务到期时，硬小时不会在连续两次 claim 类别选择中独占（见 §7.4）。
- 活动窗外只累计 debt 不发送；窗口恢复后继续补齐。
- fast_track 与 dispatcher claim 并发场景下满足 §7.5 门槛（≥2h 或 ≥100 drain 无 DeadlockDetected；单事务行数 ≤50）。
- Phase B 账本全链路 datetime 比较无 `offset-naive and offset-aware` 异常；`datetime_timezone_compare_error_total=0`。

### 10.3 生产验收

`qa_pass`、部署成功或页面 toast 都不是生产完成。只有在 release 后取得真实 Telegram 远端消息 ID、成功 Attempt、账本 credit、审计记录、Action 状态和页面一致性证据时，相关结论才可写 `pass`。缺少真实远端证据为 `unproven`；目标、账号、迁移或权限无法继续时为 `blocked`，必须保留原始错误。

Canary 成功标准（Phase B + `account_only`）示例门槛（可按租户调整，但必须事先写明）：

- 连续 N 个有活动窗口的小时内，计划桶 `confirmed_success / goal` 达到约定比例，或 `durable_debt` 单调收敛且原因可解释（容量 / 窗口 / TG 限制，而非 bucket_expired 或群冷却互挡）；
- 同群 ≥2 账号在 `account_only` 下可并行成功发送，且无重复 remote message / 双 credit；
- 无新增 `hard_hourly_bucket_expired`；unknown 仅走核验占位，不出现替代重发。

# AI 活跃群每小时硬目标 OPS

## 1. 运行原则
每小时硬目标是 AI 活跃群的强运营模式。它的目标是让系统主动追赶每小时最低真实发送量，而不是只靠自然曲线等待下一轮。

运行原则：
- 真实成功只认 `send_message` 的 `success`。
- 未达标必须暴露原因。
- 系统可以强推规划，但不能绕过账号安全、目标权限、TG 限制、内容风控和 AI 质量检查。
- 运营人员看到的 pending 必须拆成“当前小时待执行”“未来计划”“已过期未执行”。
- 任务中心是执行事实源，运营中心负责聚合和处理影响。
- 3 个线上 AI 活跃群必须按端到端链路巡检：入群完成、验证完成、`can_send` 判定、小米 MiMo/Mino draft 成功、dispatcher 消化、硬小时目标达成。
- “已入群”“验证已提交”“pending 已创建”都不是完成；只有当前小时 `send_message success` 达到目标才是硬目标达成。

## 2. 关键指标

### 2.1 任务级指标
每个启用硬目标的 AI 活跃群任务需要持续产生以下指标：
| 指标 | 含义 |
| --- | --- |
| `hard_hourly_goal` | 当前小时目标发送量 |
| `hard_hourly_success_count` | 当前小时已成功发送 |
| `hard_hourly_open_count` | 当前小时已规划未完成发送 |
| `hard_hourly_overdue_open_count` | 当前小时已过计划时间但仍未完成发送 |
| `hard_hourly_deficit` | 当前小时缺口 |
| `hard_hourly_status` | `met` / `catching_up` / `blocked` / `missed` |
| `hard_hourly_last_check_at` | 最近一次硬目标检查时间 |
| `hard_hourly_last_planned_count` | 最近一次强推创建的发送动作数 |
| `hard_hourly_pipeline` | 入群、验证、`can_send`、MiMo/Mino draft、dispatcher、硬目标分阶段状态 |
| `hard_hourly_last_blockers` | 最近一次未补足原因分布 |
| `hard_hourly_recent_buckets` | 最近 24 个小时的达标和阻塞摘要 |
| `hard_hourly_backfill_debt` | 已结束小时缺口扣减后续已送超额后的净补量债务 |
| `hard_hourly_backfill_planning_deficit` | 扣减当前小时已规划超额后仍需继续创建的补量动作数 |
| `hard_hourly_backfill_delivery_deficit` | 扣减当前小时已送超额后仍需真实送达的补量数 |

### 2.2 Action 级指标
硬目标补量 action 需要带：
| 字段 | 含义 |
| --- | --- |
| `hard_hourly_target` | 是否由硬目标补量生成 |
| `hard_hourly_bucket` | 所属小时桶 |
| `hard_hourly_deficit_at_plan` | 规划时缺口 |
| `cycle_id` | AI 活跃群轮次 |
| `turn_index` | 本轮发言序号 |
| `error_code=required_channel_followed_retry` | 发送阶段发现必需频道已自动关注，原发送动作等待重发 |
| `required_channels_followed` | 本次自动关注成功的必需频道列表 |
| `prerequisite_channel_followed` | 原发送动作重发前置频道关注已完成 |

### 2.3 全局指标
运营中心或监控应聚合：
- 硬目标任务数。
- 当前小时未达标任务数。
- 最近 24 小时未达标小时数。
- 硬目标成功发送量。
- 硬目标补量 action 数。
- 已过期未执行硬目标 action 数。
- 未达标原因 Top N。

## 3. 状态与告警

### 3.1 状态定义
| 状态 | 触发条件 | 运营含义 |
| --- | --- | --- |
| `met` | 当前小时成功数达到目标 | 正常 |
| `catching_up` | 当前小时未达标但仍有待执行或正在补量 | 观察 |
| `blocked` | 当前小时未达标，且存在明确阻塞原因 | 需要处理 |
| `missed` | 小时结束后仍未达标 | 异常 |

### 3.2 告警规则
第一版告警以任务详情和运营中心 issue 为主，不要求立即接入外部告警系统。

必须生成 issue 的情况：
- 当前小时结束后 `success_current_hour < hourly_min_messages`。
- 连续两个小时处于 `missed`。
- 当前小时 `blocked` 且阻塞原因是账号不可用、目标不可发、AI 生成不可用、TG FloodWait 或 dispatcher 延迟。
- `unknown_after_send` 出现在硬目标 action 中。

issue 建议字段：

```text
issue_type = hard_hourly_target_missed
severity = medium / high
target_id
task_id
hour_bucket
goal
success_count
open_count
deficit
blockers
suggested_action
```

严重级别：

| 级别 | 规则 |
| --- | --- |
| high | 连续两个小时 missed，或账号全不可用，或目标不可发 |
| medium | 单小时 missed，或 AI / 质量 / 容量导致部分缺口 |
| low | 当前小时 catching_up，无明确阻塞 |

## 4. 未达标原因分类
未达标原因必须尽量归一化，便于统计和处理。
| 原因码 | 说明 | 处理方向 |
| --- | --- | --- |
| `account_capacity` | 账号小时 / 日容量不足 | 增加账号、调整账号组、检查账号冷却 |
| `account_unavailable` | 账号离线、受限、需重登、健康不可用 | 账号中心处理 |
| `target_membership_pending` | 目标群入群 / 准入动作未完成 | 查看 membership 阶段 |
| `target_join_pending` | 账号尚未真正加入目标群 | 检查邀请链接、目标解析、账号入群动作 |
| `target_verification_pending` | 群管理验证未完成，包括验证码、加减验证、人工审批、关注频道要求 | 查看 verification task 和 membership item |
| `target_verification_failed` | 自动验证失败或需要人工处理 | 查看验证码读取、MiMo/Mino 识别、提交和复检记录 |
| `verification_context_unreadable` | VerificationTask 可自动处理，但验证聊天 / 图片 / 按钮上下文不可读，常见于 `private`、`lack permission`、`banned` 或 `GetHistoryRequest` | 核对 join ref、verification peer、send peer、reader account 和 submit account 是否一致 |
| `target_required_channel_pending` | 入群前要求关注一个或多个频道，尚未全部完成 | 查看每个必需频道的关注动作和结果 |
| `target_can_send_blocked` | 已入群但账号仍不可发言、被禁言或 `can_send=false` | 复检群权限并更新账号-目标关系 |
| `target_permission` | 目标群不可发、账号被禁言、权限不足 | 目标中心处理 |
| `no_context` | 无真人上下文或无可用历史 | 开启硬目标暖场、补充学习来源 |
| `quality_filter` | AI 候选被重复、低置信、事实锚点规则拦截 | 调整提示词 / 质量规则 |
| `ai_generation_unavailable` | AI 服务不可用或生成失败 | 检查 AI 配置和供应商 |
| `ai_mino_draft_unavailable` | 小米 MiMo/Mino 文本 draft 不可用、空内容或 malformed JSON | 检查 MiMo/Mino 供应商健康和任务 AI 配置 |
| `content_policy` | 内容规则、敏感词、外链、@ 成员拦截 | 检查规则中心 |
| `tg_rate_limit` | FloodWait、TG 限速或接口拒绝 | 等待冷却，降低目标或换号 |
| `dispatcher_lag` | 已到计划时间但 dispatcher 未及时执行 | 检查 worker 和队列 |
| `unknown_after_send` | 进入 TG 调用边界后本地结果未知 | 人工确认或补偿确认 |

不得使用“系统正常”“流程正常”掩盖这些原因。

## 5. 时间窗口和数据一致性

### 5.1 时间窗口
硬目标按任务时区计算小时桶。任务未配置时区或时区非法时，使用北京时间。排查时必须确认：
- `executed_at` 是否已经归一化到任务时区。
- `scheduled_at` 是否落在当前小时。
- API 输出的小时桶、数据库查询窗口和页面展示是否一致。

### 5.2 待执行拆分
硬目标的待执行必须拆成两类：
| 类别 | 计入缺口覆盖 | 含义 |
| --- | --- | --- |
| `future_open_current_hour` | 是 | `scheduled_at >= now` 且 `< hour_end` 的 pending / claiming / executing |
| `overdue_open_current_hour` | 否 | `scheduled_at < now` 但仍未成功、失败或跳过 |

过期待执行不能用于抵扣缺口。它代表 dispatcher、claim、执行或 recovery 需要排查。

### 5.3 API 一致性
以下接口的硬目标统计必须一致：
```text
GET /api/tasks
GET /api/tasks/{task_id}
GET /api/tasks/{task_id}/stats
```

如果详情或 stats 接口刷新了 `task.stats`，列表接口也必须能返回同一份刷新后的硬目标字段。值班时发现三者不一致，应按统计刷新 bug 处理，而不是按任务未达标处理。

## 6. 日常巡检流程

### 6.1 每小时巡检
运营人员或值班人员检查：
1. 任务列表中 AI 活跃群硬目标状态。
2. 当前小时 `成功 / 目标`。
3. `hard_hourly_deficit` 是否大于 0。
4. 缺口是否已经有待执行 action 覆盖。
5. 是否存在 `hard_hourly_overdue_open_count`。
6. 未达标原因是否集中在账号、目标、AI、质量或 dispatcher。
7. 端到端阶段是否卡在入群、验证、`can_send`、MiMo/Mino draft 或 dispatcher。

判断口径：

```text
成功数达到目标 -> 正常
成功数未达标，但未来待执行覆盖缺口且计划时间未到 -> 追赶中
成功数未达标，待执行不足，且 blocker 明确 -> 异常
成功数未达标，过期待执行较多 -> dispatcher_lag
小时结束仍未达标 -> 异常
```

### 6.2 任务详情排查
进入任务详情后按顺序看：
1. 硬目标执行区。
2. 当前小时 action 明细。
3. 失败 / 跳过 / unknown_after_send。
4. membership 阶段。
5. verification 阶段：验证码、加减验证、关注多个频道、人工审批、MiMo/Mino 图片识别。
6. `can_send` 复检结果和账号-目标关系。
7. 账号容量和冷却。
8. MiMo/Mino 文本 draft 记录和质量过滤。
9. listener_runtime 最近采集时间。
10. dispatcher worker 心跳。
11. 最近 24 小时硬目标桶。

### 6.3 Action 明细口径
排查“很多消息没发”时，必须拆分：
```text
当前小时成功发送
当前小时待执行
当前小时已到期未执行
未来计划发送
失败发送
跳过发送
入群 / 关注前置动作
验证码 / 加减验证 / 关注多个频道前置动作
已入群但 can_send=false 的账号
MiMo/Mino draft 成功 / 失败 / malformed JSON
频道互动动作
```

不能只看首页 `pending_actions` 总数。

## 7. 现场处理手册

### 7.1 账号容量不足

现象：

- `hard_hourly_last_blockers.account_capacity` 较高。
- 多数候选账号被冷却或小时上限挡住。

处理：

- 检查任务账号范围是否太小。
- 增加可发送账号。
- 检查账号健康、代理、登录状态。
- 检查账号小时 / 日容量配置。
- 检查 `allow_account_repeat` 是否被任务配置限制；硬目标模式应临时允许复用。

### 7.2 无上下文或等待新消息

现象：

- `last_error` 包含“暂无新的真人上下文”。
- 硬目标缺口存在，但没有生成足够 action。

处理：

- 确认硬目标模式已经启用强制空闲续聊。
- 检查目标群是否有最近监听数据。
- 检查学习来源和目标画像是否可用。
- 如果质量门全部拦截，查看 `quality_filter` 细分原因。

硬目标模式下，单纯“等待真人上下文”不能作为长期停发理由；系统必须尝试暖场或明确记录质量 / 上下文阻塞。

### 7.3 目标准入未完成

现象：

- pending 中存在 `ensure_target_membership`。
- `membership_stage` 是 `membership_running` 或 `membership_partial`。
- membership item 的失败原因包含群管理 bot、图形验证码、`captcha`、`challenge_required`、`manual_required` 或“需要验证”。
- 账号已经加入目标群，但账号-目标关系仍未写回 `can_send=true`。
- 验证消息要求先关注一个或多个频道，或要求回答加减验证 / 算术验证。

处理：

- 查看 membership-items。
- 确认可入群账号数量。
- 先把失败拆成关注频道、邀请链接失效、目标权限、群管理 bot 图片验证码、人工审批、账号限制和 Telegram API 失败，不得统一写成普通“加入失败”。
- 入群完成只代表 membership 阶段通过；必须继续看 verification 阶段和 `can_send` 复检。`can_send=false` 时主发送动作不得创建，原因写为 `target_can_send_blocked`。
- 需要关注多个频道时，每个必需频道都要有独立的 ensure membership 结果；任一频道未关注成功时，目标群状态仍是 `target_required_channel_pending`，不能把部分关注成功写成群准入完成。
- 已跳过的 `membership_permission_denied` 如果错误详情包含必需频道链接或“需要关注频道”，硬目标模式应生成 `hard_hourly_required_channel_retry` 准入重试动作；重试动作未执行前，这些账号应回到 `need_join`，不应永久计入 failed。
- 加减验证 / 算术验证应从最近验证消息中提取题目、生成答案、提交并复检 `can_send`；题目读取失败、答案低置信、提交失败或复检失败时，分别记录为读取 / 识别 / 提交 / 复检失败，不得合并成“验证失败”。
- `target_admission_retry` 中出现“未解析到群关联频道”“仍未获群发言权限”或“群无权限或账号不可发言”时，先按疑似群管理验证处理：重新加入 / 复检触发当前验证，读取验证码上下文，图片验证码走 MiMo，提交后再复检；不能直接把整批账号标记为人工处理。
- 如果准入执行项被标记为“已进入 TG 调用边界但本地结果未知”，先由恢复守护补偿复检目标能力。复检确认可发言的账号改为成功并写回可发言关系；复检仍失败或无法复检时才保留结果未知，避免把实际已加入账号当作失败缺口。
- 如果页面显示“没有读取到最近验证聊天信息”，先按验证聊天读取空态处理：核对目标 peer、账号是否仍能读取群历史、验证消息是否已过期 / 被删除、账号 session 是否有效，以及是否出现 `GetHistoryRequest` 权限错误。
- 出现 `private`、`lack permission`、`banned` 或 `GetHistoryRequest` 时，单独记录为 `verification_context_unreadable`。先核对本次入群使用的 `join_ref`、验证任务读取的 peer、发送规划使用的 send peer 是否一致；不能反复用 stale numeric peer 或不可读账号重试图片验证码。
- 通过 username / invite 触发准入时，验证任务应读取本次成功准入的公共 username 或已证明可读的 send peer；只有 `can_send=true` 的同目标 group 才能进入硬目标可发账号池。
- 单账号弹窗点击“重新读取”时，应先重新加入 / 重试准入来触发当前验证码上下文；如果加入后直接可发言，则关闭待处理；如果仍需验证码，系统再读取最新验证消息。
- 验证码读取允许双账号协作：加入账号负责触发入群和提交验证码；同目标中已可读历史的账号可负责读取群管理 bot / 管理员验证码消息和图片。
- 图片验证码分支必须确认系统设置里存在健康的 MiMo 视觉供应商，且任务启用了 `ai_assisted_verification`。DeepSeek 等纯文本供应商不能处理图片验证码。
- 对图片验证码账号，检查准入明细中的验证消息、图片摘要、MiMo 答案、置信度、发送结果和复检结果；缺少任一环节时按该环节记录失败原因。
- MiMo 未配置、图片不可下载、识别低置信、答案发送失败或复检仍不可发言时，标记人工处理，不再自动反复尝试同一张验证码。
- 主发送不足时记录 `target_membership_pending`，不应写成普通发送失败。
- 只有入群完成、验证完成、必需频道关注完成且 `can_send=true` 后，才允许把该账号计入硬目标可发账号池。

### 7.3.1 发送阶段要求关注频道

现象：

- 群里出现管理员或学院助手提示“您需要关注我们的频道才能发言”。
- 用户发出的消息被删除，action 失败类型是 `group_permission_denied`。
- 失败详情或按钮中包含一个或多个频道 username / t.me 链接。

处理：

- Dispatcher 应解析失败详情中的必需频道，逐个执行自动关注。
- 如果验证消息同时带“我已加入 / 我已关注 / 完成验证”确认按钮，全部必需频道关注成功后必须点击该确认按钮；按钮点击失败时保留真实失败原因，不能直接写成已可发言。
- 关注成功后必须复检目标群 `can_send`，复检通过才允许把原 `send_message` action 重新置为 `pending`。
- 重排 action 的 result 应包含 `error_code=required_channel_followed_retry`、`required_channels_followed` 和 `prerequisite_channel_followed=true`，用于区分“已补前置条件等待重发”和普通失败。
- 只有后续真实重发成功才计入 `hard_hourly_success_count`；自动关注本身不能抵扣硬目标。
- 如果频道解析、关注或复检失败，保留真实失败原因并进入 `target_required_channel_pending` 或 `target_can_send_blocked` 排查，不得写成成功或静默跳过。

### 7.4 AI 生成不足

现象：

- 强推缺口较大，但实际创建 action 很少。
- AI 生成记录为空或候选全被过滤。

处理：

- 检查小米 MiMo/Mino Provider 健康，确认任务 `ai_provider_id` 或租户默认供应商指向 MiMo/Mino。
- 检查 prompt、目标画像、黑话模板和上下文。
- 检查重复度、事实锚点和低置信静默规则。
- 失败必须保留 `ai_generation_unavailable`、`ai_mino_draft_unavailable` 或 `quality_filter`，不能把目标当作已完成。
- MiMo/Mino 返回空内容、拒答文案、非 JSON draft、字段缺失或数量不足时，按 AI draft 阶段阻塞处理；不允许用本地模板、mock 文案或其他供应商补齐目标。

### 7.5 Dispatcher 延迟

现象：

- 当前小时有大量 `scheduled_at <= now` 的 pending / claiming / executing。
- worker heartbeat 异常或 claim 堆积。

处理：

- 检查 worker role：planner、dispatcher、listener、recovery。
- 检查 `claiming` 过期恢复。
- 检查运行指标 `actions.oldest_pending_age_seconds`。
- 检查部署版本和 worker 日志。
- 对比 `future_open_current_hour` 与 `overdue_open_current_hour`，确认缺口是否真的被未来计划覆盖。

### 7.6 强推失败反复重试

现象：

- 当前小时有缺口。
- 最近多次强推均创建 0 条 action。
- AI 调用、质量过滤或账号容量失败次数快速增加。

处理：

- 查看 `hard_hourly_last_blockers`。
- 查看下一次硬目标检查时间。
- 如果是 `quality_filter`，检查提示词、重复度和事实锚点。
- 如果是 `ai_generation_unavailable`，检查供应商健康和 token。
- 如果是 `account_capacity`，检查账号下一容量窗口。
- 不能通过降低目标或清空缺口来让状态变绿；只能处理根因或由运营人员关闭硬目标。

## 8. 发布与回滚

### 8.1 发布前检查

- 旧任务未启用硬目标时行为不变。
- 创建 / 编辑 AI 活跃群任务可保存硬目标字段。
- Planner 可根据缺口创建补量 action。
- 任务列表 / 详情 / stats 展示一致。
- 预检能显示容量风险。
- 单元和集成测试覆盖硬目标计算、补量、阻塞原因和列表展示。
- 小时桶使用任务时区，且 aware / naive datetime 不会导致统计丢失。
- 已过期 pending 不抵扣缺口。
- 最近 24 小时桶能保留上一小时 missed 状态。
- 目标群准入测试覆盖入群、验证码、加减验证、关注多个频道、`can_send=false` 阻塞和复检成功。
- AI 生成测试覆盖小米 MiMo/Mino 健康供应商、malformed JSON、空内容和供应商不可用，不允许 mock 成功。

### 8.2 发布流程

Telegram 相关项目按现有流程：

```text
master -> release -> push release -> GitHub Actions Deploy Production
```

发布后检查：

- `/api/health` 正常。
- `/api/tasks` 中旧任务 stats 正常。
- 新硬目标测试任务可以创建。
- 当前小时硬目标进度正确。
- `/api/tasks`、`/api/tasks/{id}`、`/api/tasks/{id}/stats` 的硬目标字段一致。
- 3 个线上 AI 活跃群逐一核对端到端阶段：入群、验证、`can_send`、MiMo/Mino draft、dispatcher、小时达标。
- overview 24 小时活动统计仍为非零。

### 8.3 回滚口径

如果硬目标发布后影响普通任务：

- 先关闭任务级 `hard_hourly_target_enabled`。
- 如果是前端展示问题，保留后端字段，临时隐藏 UI。
- 如果是 planner 误创建大量 action，暂停相关任务并检查补量 action 标记。
- 如果影响全局 worker，回滚 release 到上一成功版本。

## 9. 验收用例

### 9.1 低目标可达场景

配置：

```text
hourly_min_messages = 10
账号数量 >= 10
目标群可发送
```

预期：

- 当前小时不足 10 条时自动补量。
- 成功达到 10 条后停止硬目标补量。
- 任务 stats 显示 `met`。

### 9.2 高目标不可达场景

配置：

```text
hourly_min_messages = 100
可用账号容量明显不足
```

预期：

- 系统持续强推规划。
- 已创建 action 真实执行。
- 未补足部分显示 `account_capacity` 或其他明确原因。
- 小时结束后生成 missed 状态或 issue。

### 9.3 无上下文场景

配置：

```text
hourly_min_messages = 10
目标群无新真人消息
```

预期：

- 系统尝试空闲续聊 / 暖场。
- 如果质量门通过，创建补量 action。
- 如果质量门不通过，记录 `no_context` 或 `quality_filter`。

### 9.4 已有未来 pending 场景

配置：

```text
目标 10
成功 6
当前小时待执行 4
```

预期：

- 缺口为 0。
- 不重复规划 20 条。

### 9.5 已过期 pending 场景

配置：

```text
目标 10
成功 6
当前小时 pending 4，但 scheduled_at 已过
```

预期：

- 系统标记 dispatcher 延迟或执行滞后。
- 不把已过期 pending 当作成功。
- 如仍有时间，继续补量或推动 recovery。

### 9.6 列表 / 详情一致性场景

配置：

```text
任务 stats 里有旧 hard_hourly 字段
当前小时实际已经恢复或达标
```

预期：

- `/api/tasks` 不展示旧状态。
- `/api/tasks/{id}` 不展示旧状态。
- `/api/tasks/{id}/stats` 不展示旧状态。
- 三个接口的目标、成功、待执行、缺口和状态一致。

### 9.7 群准入验证场景

配置：

```text
目标群需要验证码 / 加减验证 / 关注多个频道
hourly_min_messages = 10
```

预期：

- 系统先生成 `ensure_target_membership`，不会提前创建主发送动作。
- 图片验证码使用 MiMo 视觉供应商；加减验证从最近验证消息读取题目并提交答案。
- 需要关注多个频道时，每个频道关注动作独立展示成功 / 失败。
- 验证提交后必须复检 `can_send`；复检通过后才进入 AI draft 和发送规划。
- 验证上下文读取失败时，`hard_hourly_last_blockers` 必须写 `verification_context_unreadable`；不能写成 MiMo 识别中或验证码已自动处理。
- 任一环节失败时，`hard_hourly_last_blockers` 写入对应 membership / verification / can_send 原因。

### 9.8 MiMo/Mino draft 场景

配置：

```text
hourly_min_messages = 10
目标群已 can_send
MiMo/Mino Provider 健康
```

预期：

- AI 活跃群文本 draft 使用小米 MiMo/Mino。
- MiMo/Mino 返回可解析 JSON 且候选通过质量门时，创建硬目标发送动作。
- MiMo/Mino 返回 malformed JSON、空内容、拒答或不可用时，任务记录 `ai_mino_draft_unavailable` / `ai_generation_unavailable`，不生成 mock 成功消息。

## 10. 数据核对 SQL 口径

当前小时成功发送：

```sql
select count(*)
from actions
where task_id = :task_id
  and task_type = 'group_ai_chat'
  and action_type = 'send_message'
  and status = 'success'
  and executed_at >= :hour_start
  and executed_at < :hour_end;
```

当前小时待执行覆盖：

```sql
select count(*)
from actions
where task_id = :task_id
  and task_type = 'group_ai_chat'
  and action_type = 'send_message'
  and status in ('pending', 'claiming', 'executing')
  and scheduled_at >= :now
  and scheduled_at < :hour_end;
```

当前小时已过期未执行：

```sql
select count(*)
from actions
where task_id = :task_id
  and task_type = 'group_ai_chat'
  and action_type = 'send_message'
  and status in ('pending', 'claiming', 'executing')
  and scheduled_at >= :hour_start
  and scheduled_at < :now;
```

硬目标补量 action：

```sql
select id, account_id, status, scheduled_at, executed_at, result
from actions
where task_id = :task_id
  and action_type = 'send_message'
  and payload ->> 'hard_hourly_target' = 'true'
order by scheduled_at desc;
```

最近 24 小时硬目标桶建议直接看任务 stats：

```text
hard_hourly_recent_buckets
```

## 11. 值班结论模板

正常：

```text
AI 活跃群硬目标正常。本小时目标 10，成功 10，缺口 0，无硬目标阻塞。
```

追赶中：

```text
AI 活跃群硬目标追赶中。本小时目标 10，成功 7，当前小时待执行 3，缺口已被待执行覆盖，计划时间未到。
```

执行滞后：

```text
AI 活跃群硬目标存在执行滞后。本小时目标 10，成功 6，未来待执行 1，已过期未执行 3，缺口 3。需检查 dispatcher / worker 心跳和 claim 恢复。
```

异常：

```text
AI 活跃群硬目标未达成。本小时目标 10，成功 7，待执行 1，缺口 2。主要原因：账号容量不足 1、AI 质量过滤 1。已生成运营异常，需处理账号容量和目标权限。
```

# AI 活跃群全账号日覆盖模式专项 PRD

## 背景

运营需要一种 AI 活跃群任务模式：在一天 24 小时内，让当前任务账号范围内每个可发言账号都在目标群发送 1-2 条消息，用于拉高整体群活跃度。该能力与现有 AI 活跃群的目标、AI 内容生成、准入、风控和发送链路一致，差异只在账号覆盖目标和跨轮排程优先级。

## 产品决策

本期复用现有 `group_ai_chat` 任务类型，不新增独立任务类型。

原因：

- 用户原始需求明确“整体与现有 AI 活群任务类似”，核心差异是“每个账号都要发言”。
- 现有 AI 活群已包含目标群准入、AI 生成、小时硬目标、参与账号覆盖、Dispatcher 和详情统计；新增任务类型会复制入口、配置、调度和验收链路。
- 复用 `group_ai_chat` 可以保持旧任务兼容，新增模式默认关闭，只影响用户显式开启的任务。

## 范围

包含：

- `group_ai_chat` 新增全账号日覆盖模式配置。
- 创建任务、编辑任务和任务详情页可设置并展示该模式。
- 预检和详情展示覆盖目标、预计容量缺口和阻塞原因。
- Planner 在 24 小时窗口内优先补齐未覆盖账号，每个可发言账号完成 1-2 条成功 `send_message`。
- Action payload 保留覆盖审计字段，便于 QA 和线上排障确认。

不包含：

- 不新增新的用户可选 task_type。
- 不绕过目标群准入、账号容量、账号健康、小时硬上限、全局风控和 AI 内容质量过滤。
- 不把未准入、发送失败、`unknown_after_send`、skipped 或 pending action 计入覆盖完成。
- 不访问生产环境，不做线上恢复声明。

## 配置模型

新增字段写入 `Task.type_config`：

| 字段 | 类型 | 默认值 | 校验 | 说明 |
| --- | --- | --- | --- | --- |
| `account_coverage_mode` | enum: `natural` / `all_accounts_daily` | `natural` | 只允许枚举值 | `natural` 保持现有 AI 活群分配；`all_accounts_daily` 开启全账号日覆盖 |
| `per_account_daily_min_messages` | integer | `1` | `1 <= min <= max <= 2` | 每个可发言账号在日窗口内最少成功消息数 |
| `per_account_daily_max_messages` | integer | `2` | `1 <= min <= max <= 2` | 每个可发言账号在日窗口内最多用于覆盖目标的成功消息数 |
| `coverage_window_hours` | integer | `24` | 本期固定 24 | 使用任务设置的一天运行窗口；前端不暴露任意小时配置 |

旧任务缺少字段时等价于：

```json
{
  "account_coverage_mode": "natural",
  "per_account_daily_min_messages": 1,
  "per_account_daily_max_messages": 2,
  "coverage_window_hours": 24
}
```

## 前端设计

创建 / 编辑任务表单在 AI 活跃群配置区增加“全账号日覆盖”：

- 开关：`全账号日覆盖模式`，默认关闭。
- 数字输入：`每个账号最少消息数`，默认 1，只允许 1-2。
- 数字输入：`每个账号最多消息数`，默认 2，只允许 1-2，且不得小于最少值。
- 说明文案必须表达：系统会在 24 小时内尽量让每个可发言账号完成 1-2 条消息；实际完成受准入、账号容量、风控、AI 内容和小时硬上限影响。

任务详情页新增覆盖摘要：

- 覆盖模式：关闭 / 全账号日覆盖。
- 今日覆盖：已完成账号数 / 当前账号范围总数 / 百分比。
- 剩余覆盖：未完成账号数、未准入账号数、受限账号数。
- 阻塞原因：目标不可发言、准入验证中、账号容量不足、AI 候选不足、质量过滤、发送失败等。
- 近期待补账号列表：账号、已成功消息数、目标消息数、最近失败 / 跳过原因。

前端不得把计划 action 数显示为覆盖成功数；覆盖成功只看成功 `send_message`。

## 后端 / API / Worker 设计

API：

- `POST /api/tasks/group-ai-chat` 和 `POST /api/tasks/group-ai-chat/create-and-start` 接受新增字段。
- `PATCH /api/tasks/{task_id}/group-ai-chat` 支持更新新增字段。
- `GET /api/tasks` 和 `GET /api/tasks/{task_id}` 投影当前覆盖配置和覆盖摘要。
- 配置校验失败必须返回明确错误，不做 silent fallback。

Planner：

- 先读取任务账号范围、目标 ready pool、今日成功 `send_message` 事实和账号风控容量。
- `account_coverage_mode=natural` 时保持现有 AI 活群账号分配逻辑。
- `account_coverage_mode=all_accounts_daily` 时，当前轮优先选择 `coverage_remaining > 0` 的可用账号。
- 每个账号的覆盖完成只以当前 24 小时窗口内成功 `send_message` 计数；失败、pending、skipped、`unknown_after_send` 不计入。
- 当前小时预算不足时，只规划可落入小时硬上限内的 action，剩余覆盖留到后续轮次。
- 未准入账号不生成主发送 action；准入成功后进入后续轮次补覆盖。
- 当 AI 候选不足或质量过滤导致不能生成足够消息时，记录缺口原因，不生成空消息或 mock success。

Action payload：

```json
{
  "account_coverage_mode": "all_accounts_daily",
  "coverage_window_date": "YYYY-MM-DD",
  "coverage_target_per_account": 1,
  "coverage_account_completed_before_action": 0,
  "coverage_account_remaining_before_action": 1,
  "coverage_reason": "daily_account_coverage"
}
```

Dispatcher 不因覆盖模式改变发送准入和风控，只消费 Planner 生成的普通 `send_message` action。

## 数据流转设计

```text
Web 创建 / 编辑 / 详情保存
  -> group_ai_chat config schema 校验
  -> Task.type_config 写入 account coverage 字段
  -> Planner 读取任务账号范围、ready pool、日内成功 send_message
  -> 计算每账号 coverage_remaining
  -> 按小时硬上限和账号容量生成 send_message action
  -> Dispatcher 真实发送
  -> 执行结果回写 Action / ExecutionAttempt
  -> 列表和详情投影覆盖摘要、剩余账号和阻塞原因
```

## 权限与安全

- 只有具备任务创建 / 编辑权限的后台用户可设置。
- TG bot 已有 AI 活群设置能力如复用该配置，必须继续只允许租户 `admin_chat_id` 修改，并调用同一后端校验。
- 覆盖模式不能突破账号风控、群准入和发送权限。

## 边界场景

| 场景 | 产品要求 |
| --- | --- |
| 当前账号范围为空 | 预检提示无可覆盖账号，禁止启动或要求用户调整账号范围 |
| 账号未完成准入 | 不生成主发送；详情显示准入中 / 验证中 / 人工处理 |
| 可发言账号很多但小时预算不足 | 不突破预算，详情显示预计无法在 24 小时内完成 |
| AI 候选不足或质量过滤 | 不用模板补量；记录 AI 候选不足或质量过滤缺口 |
| 单账号已经成功 2 条 | 不再为覆盖目标选择该账号；普通分配仍受现有重复规则限制 |
| 修改账号范围或覆盖配置 | 清理未来未执行主互动计划并按新配置重排；历史成功事实保留 |
| 发送后状态未知 | 不计入覆盖完成，由现有 unknown / recovery 机制处理 |

## QA 验收口径

后端：

- 创建和更新 `group_ai_chat` 可接受新增字段；非法枚举、min/max 越界、min 大于 max 必须失败。
- 旧任务缺少字段时保持现有 `natural` 行为。
- 开启全账号日覆盖后，Planner 优先为未覆盖可发言账号生成 `send_message`，并带覆盖 payload 元数据。
- 覆盖 action 不突破 `messages_per_round`、每小时硬目标、账号容量、准入状态和风控。
- 发送失败、pending、skipped、`unknown_after_send` 不计入覆盖完成。

前端：

- 创建 / 编辑表单可设置全账号日覆盖和 1-2 条消息范围。
- 任务详情页展示覆盖配置、覆盖进度、剩余账号和阻塞原因。
- 保存 payload 与后端 schema 一致，校验错误明确展示。

跨入口：

- Web 与 TG bot 如都支持修改该配置，必须写入同一 `PATCH /api/tasks/{task_id}/group-ai-chat` 校验链路。
- QA pass 只代表功能验收通过，不代表生产恢复。

## Release Gate

本需求为 L2 功能变更，`production_related=false`，但影响任务中心配置、AI 活群 Planner、Action payload 和 Web 表单，必须走 Release Gate。生产验证不在本期产品验收内，除非后续发布流程明确要求。

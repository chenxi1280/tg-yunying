# AI 活跃群每小时硬目标 PRD

## 1. 背景

线上 AI 活跃群任务已经支持 `messages_per_round`、`max_actions_per_hour` 和 24 小时活跃曲线，但这些配置本质是“每轮生成量”“每小时上限”和“自然分布权重”。当运营人员把目标设置得很高时，系统仍可能因为自然曲线、账号冷却、上下文等待、入群前置动作或质量过滤，把待发送动作分散到后续时间。

新的产品诉求是：运营人员可以直接设置“每小时最低发送多少条”，系统按硬目标主动强推规划和发送，而不是只靠自然节奏慢慢补。

## 2. 目标

- AI 活跃群任务支持配置每小时最低成功发送量。
- 开启硬目标后，系统在每个自然小时内主动计算缺口并追加规划。
- 系统可以自动提高本小时规划强度，包括提高每轮发言数、缩短计划间隔、允许账号复用、提前触发空闲续聊。
- 未达标时必须展示真实原因，不允许把跳过、待执行、未知结果或失败伪装成成功。
- 硬目标不能绕过账号安全、Telegram 明确限制、内容风控、目标权限和执行失败事实。
- 任务中心和运营数据必须能区分“自然节奏未到”和“硬目标未达成”。

## 3. 非目标

- 不新增单独的任务类型；该能力只属于 `group_ai_chat`。
- 不把频道浏览、点赞、评论或转发监听改成硬目标模式。
- 不用 mock、模板成功或静默降级补足目标。
- 不承诺在账号不可用、目标不可发、TG FloodWait、AI 生成失败或内容风控拒绝时仍能成功发送。
- 不替代全局风控中心；硬目标只提高任务规划积极性，不能取消平台安全边界。

## 4. 核心概念

### 4.1 每小时硬目标

新增任务配置：

```json
{
  "hard_hourly_target_enabled": true,
  "hourly_min_messages": 60,
  "hard_hourly_strategy": "force_planning"
}
```

字段说明：

| 字段 | 类型 | 默认值 | 含义 |
| --- | --- | --- | --- |
| `hard_hourly_target_enabled` | boolean | `false` | 是否启用每小时硬目标 |
| `hourly_min_messages` | integer | `null` | 每个自然小时最低成功发送条数 |
| `hard_hourly_strategy` | string | `force_planning` | 未达标时强推规划，第一版固定为该值 |

旧任务默认不启用硬目标，保持现有自然曲线行为。

### 4.2 当前小时窗口

按任务所在时区计算自然小时：

```text
hour_start = 当前小时整点
hour_end = 下一小时整点
```

第一版沿用系统现有北京时间口径。后续如果任务时区已经被完整使用，应以任务 `timezone` 为准。

### 4.3 硬目标进度

当前小时进度只统计 AI 活跃群真实发言动作：

```text
action_type = send_message
task_type = group_ai_chat
status = success
executed_at in [hour_start, hour_end)
```

不计入：

- `pending`、`claiming`、`executing`
- `failed`
- `skipped`
- `unknown_after_send`
- `ensure_target_membership`
- 频道浏览、点赞、评论动作

### 4.4 规划覆盖量

为了避免重复生成，系统同时统计当前小时已规划但未成功的发言动作：

```text
open_current_hour = pending + claiming + executing
scheduled_at in [now, hour_end)
action_type = send_message
task_type = group_ai_chat
```

硬目标缺口：

```text
deficit = hourly_min_messages - success_current_hour - open_current_hour
```

当 `deficit > 0` 时，Planner 必须尝试追加规划。

## 5. 用户故事

1. 运营人员创建 AI 活跃群任务时，可以设置“每小时最低发送量 60 条”。
2. 到 20:30 时，本小时只成功发送 20 条，且只剩 10 条待执行，系统自动识别缺口 30 条并追加规划。
3. 追加规划时，系统自动提高本轮生成数量，并把计划时间压缩到本小时剩余窗口内。
4. 如果没有新真人上下文，系统不再一直等待默认空闲间隔，而是立即尝试空闲续聊或暖场。
5. 如果最终因账号容量不足只发出 45 条，任务详情必须显示“硬目标未达成：账号容量不足 / TG 限制 / AI 生成不足”等原因。
6. 运营人员在任务列表中能看到当前小时完成度，例如 `45 / 60`，而不是只看到 pending 总数。

## 6. 产品交互

### 6.1 创建和编辑任务

AI 活跃群任务高级设置增加“每小时硬目标”区域：

| 控件 | 默认值 | 说明 |
| --- | --- | --- |
| 启用每小时硬目标 | 关闭 | 开启后显示下面字段 |
| 每小时最低发送量 | 空 | 必填，最小 1 |
| 未达标处理 | 强推规划 | 第一版固定，不提供其他选项 |

开启硬目标后，页面提示：

```text
系统会自动提高规划强度以追赶本小时最低发送量；真实执行仍受账号容量、目标权限、TG 限制、AI 质量和风控限制约束，未达标原因会在任务详情中展示。
```

### 6.2 任务列表

AI 活跃群任务卡片增加硬目标摘要：

```text
本小时硬目标 45 / 60
缺口 15
状态：追赶中 / 已达标 / 未达标
```

状态规则：

| 状态 | 规则 |
| --- | --- |
| 已达标 | `success_current_hour >= hourly_min_messages` |
| 追赶中 | 当前小时未结束，且存在缺口或追赶规划 |
| 未达标 | 当前小时结束后仍未达到目标 |
| 未启用 | 未开启硬目标 |

### 6.3 任务详情

任务详情增加“硬目标执行”区：

- 当前小时目标。
- 当前小时成功发送。
- 当前小时已规划待执行。
- 当前小时缺口。
- 最近一次强推规划时间。
- 最近一次强推生成数量。
- 最近一次未达标原因。
- 当前小时失败原因分布。

示例：

```text
20:00-21:00
目标 60，已成功 45，待执行 3，缺口 12
最近强推：20:42，追加规划 18 条，实际创建 15 条
未达标原因：账号容量不足 7，AI 质量过滤 3，TG FloodWait 2
```

### 6.4 运营数据

运营数据页应能按 AI 活跃群展示：

- 硬目标小时数。
- 达标小时数。
- 未达标小时数。
- 未达标原因 Top N。
- 目标发送量 vs 实际成功发送量。

第一版可以先放在任务详情和任务列表，运营数据聚合在后续接入。

## 7. 后端配置与统计

### 7.1 任务配置

字段放入 `group_ai_chat` 的 `type_config`：

```json
{
  "hard_hourly_target_enabled": true,
  "hourly_min_messages": 60,
  "hard_hourly_strategy": "force_planning"
}
```

放在 `type_config` 而不是 `pacing_config` 的原因：

- 它只适用于 AI 活跃群。
- 它影响 AI 生成、上下文等待、每轮发言数和账号复用，不只是时间排布。
- 旧 pacing 的 `max_actions_per_hour` 仍作为自然节奏和普通上限配置。

### 7.2 任务统计

`task.stats` 增加：

```json
{
  "hard_hourly_target_enabled": true,
  "hard_hourly_goal": 60,
  "hard_hourly_bucket": "2026-06-07T20:00:00+08:00",
  "hard_hourly_success_count": 45,
  "hard_hourly_open_count": 3,
  "hard_hourly_deficit": 12,
  "hard_hourly_status": "catching_up",
  "hard_hourly_last_check_at": "2026-06-07T20:42:15+08:00",
  "hard_hourly_last_planned_count": 15,
  "hard_hourly_last_blockers": {
    "account_capacity": 7,
    "quality_filter": 3,
    "tg_rate_limit": 2
  }
}
```

状态取值：

| 状态 | 含义 |
| --- | --- |
| `disabled` | 未开启硬目标 |
| `met` | 本小时已达标 |
| `catching_up` | 本小时未达标，系统正在强推规划 |
| `blocked` | 本小时仍未达标且当前存在明确阻塞 |
| `missed` | 小时结束后未达标 |

### 7.3 Action 标记

硬目标补量创建的发送动作需要在 payload 或 result 中标记：

```json
{
  "hard_hourly_target": true,
  "hard_hourly_bucket": "2026-06-07T20:00:00+08:00",
  "hard_hourly_deficit_at_plan": 30
}
```

该标记用于详情、统计、审计和排障，不影响发送内容。

## 8. Planner 行为

### 8.1 调度入口

当 AI 活跃群任务启用硬目标时，Planner 每次处理任务都先执行硬目标控制器：

```text
读取当前小时目标
  -> 统计本小时成功发送
  -> 统计本小时已规划待执行
  -> 计算缺口
  -> 无缺口：走普通 AI 活跃群规划
  -> 有缺口：进入强推规划
```

### 8.2 强推规划

强推规划要求：

- 本轮有效发言数优先按 `deficit` 计算。
- `messages_per_round_mode=auto` 时，硬目标控制器可以临时提高本轮数量。
- `messages_per_round_mode=manual` 时，硬目标控制器仍可以临时突破该轮次值，因为硬目标优先于自然轮次。
- `allow_account_repeat` 临时视为 `true`，但仍受账号容量、目标权限和风控限制。
- 发送计划时间压缩到当前小时剩余窗口。
- 如果当前小时剩余时间不足，仍尽量创建最近可执行时间；执行层如果因容量或 TG 限制延后，必须记录未达标原因。

### 8.3 上下文等待

硬目标启用后，默认等待新真人上下文的行为需要调整：

- 如果有缺口，系统应立即尝试空闲续聊或暖场，而不是继续等待默认 `idle_continuation_seconds`。
- 如果没有任何可用上下文，允许使用目标画像、历史摘要和 bootstrap prompt 生成暖场内容。
- 生成内容仍必须通过事实锚点、重复度、低置信度和内容规则检查。
- 如果质量门拒绝所有候选，记录 `quality_filter` 或 `no_context_quality_blocked`，不能伪造发送。

### 8.4 账号和容量

硬目标可以提高规划强度，但执行前仍必须检查：

- 账号在线 / 可用。
- 账号在目标群具备发送能力。
- 账号全局冷却。
- 账号小时 / 日容量。
- 目标群频控。
- TG FloodWait、账号受限、被禁言等真实结果。

当容量不足时：

- 已有可发账号继续创建可执行动作。
- 不可发部分记录缺口和原因。
- 不把容量不足的部分记为 skipped 发送成功。

### 8.5 与 `max_actions_per_hour` 的关系

普通模式下，`max_actions_per_hour` 是每小时上限。

硬目标模式下：

- `hourly_min_messages` 是本小时必须追赶的目标。
- `max_actions_per_hour` 仍作为自然模式上限和预检参考。
- 如果 `hourly_min_messages > max_actions_per_hour`，硬目标控制器以 `hourly_min_messages` 为优先目标，但详情必须提示“硬目标超过普通小时上限”。
- 真实执行仍以账号容量、风控中心和 TG 结果为最终边界。

## 9. API 变更

### 9.1 创建任务

`POST /api/tasks/group-ai-chat`

新增字段：

```json
{
  "hard_hourly_target_enabled": true,
  "hourly_min_messages": 60,
  "hard_hourly_strategy": "force_planning"
}
```

### 9.2 编辑任务

`PATCH /api/tasks/{task_id}/group-ai-chat`

允许更新同样字段。更新后从下一次 planner tick 生效，不重写已存在 action。

### 9.3 任务列表 / 详情 / Stats

现有任务输出的 `stats` 中增加硬目标字段。前端不需要新增单独查询接口。

### 9.4 预检

`POST /api/tasks/precheck` 增加硬目标预检摘要：

```json
{
  "hard_hourly_target": {
    "enabled": true,
    "hourly_min_messages": 60,
    "estimated_hourly_capacity": 42,
    "capacity_gap": 18,
    "warnings": [
      "硬目标高于当前账号容量，可能持续未达标"
    ]
  }
}
```

预检只提示风险，不因为目标过高直接禁止创建；但目标群不存在、无可用账号、规则绑定非法等既有硬错误仍按原逻辑处理。

## 10. 验收口径

### 10.1 后端验收

- 旧任务未配置硬目标时，行为不变。
- 开启 `hourly_min_messages=60` 后，当前小时成功 + 待执行小于 60 时，planner 创建补量发送动作。
- 补量动作带 `hard_hourly_target=true` 和小时桶。
- 当前小时达标后，不再继续为硬目标追加动作。
- 当前小时无真人新上下文时，硬目标可以触发空闲续聊 / 暖场。
- 质量过滤、内容规则、账号容量、目标权限和 TG 限制导致无法补足时，stats 记录原因。
- `max_actions_per_hour` 小于硬目标时，不阻止硬目标规划，但 stats / 详情显示目标超过普通上限。
- 已存在未来 pending 动作会计入 `open_current_hour`，避免重复规划。

### 10.2 前端验收

- 创建 AI 活跃群任务时可开启硬目标并填写每小时最低发送量。
- 编辑已建 AI 活跃群任务时可开启、关闭或调整硬目标。
- 任务列表展示当前小时 `成功 / 目标`、缺口和状态。
- 任务详情展示硬目标小时桶、成功数、待执行数、缺口、最近强推和阻塞原因。
- 未开启硬目标的任务不展示多余噪音。

### 10.3 线上验收

- 新建一个测试 AI 活跃群任务，设置较低硬目标，例如 3 条 / 小时，确认当前小时缺口会触发追加规划。
- 设置高于账号容量的硬目标，确认系统不会伪造成功，会展示容量不足或 TG 限制。
- 对比 `/api/overview`、`/api/tasks`、`/api/tasks/{id}/stats` 和 action 明细，确认成功数、待执行数和缺口一致。
- 暂停任务后，硬目标不再追加规划。
- 恢复任务后，从恢复后的当前小时继续计算目标。

## 11. 实施优先级

第一阶段：

- 后端 schema、配置保存、硬目标统计。
- Planner 缺口计算和强推规划。
- 任务列表 / 详情 stats 输出。
- 前端创建 / 编辑 / 列表 / 详情展示。
- 回归测试。

第二阶段：

- 运营数据聚合硬目标达标率。
- 未达标 issue 上卷到运营中心。
- 更细的小时历史报表。


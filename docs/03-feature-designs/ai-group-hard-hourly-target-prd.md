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
- 线上指定的 3 个 AI 活跃群必须按同一条端到端链路验收：入群完成、验证完成、`can_send` 判定、MiMo/Mino draft 成功、发送动作被 dispatcher 消化、硬小时目标达成，任一阶段未完成都不能写成已达标。
- AI 活跃群硬目标场景的文本 draft 使用小米 MiMo/Mino 健康供应商；图片验证码识别使用健康的多模态视觉供应商（MiMo/Mino 或 MiniMax）。供应商不可用、返回空内容、返回非 JSON draft 或图片识别失败时，必须暴露为 AI / 验证阻塞，不允许静默换成 mock、模板或不具备对应能力的供应商伪造成功。

## 3. 非目标

- 不新增单独的任务类型；该能力只属于 `group_ai_chat`。
- 不把频道浏览、点赞、评论或转发监听改成硬目标模式。
- 不用 mock、模板成功或静默降级补足目标。
- 不承诺在账号不可用、目标不可发、TG FloodWait、AI 生成失败或内容风控拒绝时仍能成功发送。
- 不替代全局风控中心；硬目标只提高任务规划积极性，不能取消平台安全边界。
- 不把“已加入目标群”当作“已可发送”。群成员关系、群管理验证、关联频道关注和 `can_send=true` 是独立验收门。

## 4. 核心概念

### 4.1 每小时硬目标

新增任务配置：

```json
{
  "hard_hourly_target_enabled": true,
  "hourly_min_messages": 10,
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

小时窗口必须使用任务时区计算。任务未配置时区或时区非法时，回退到系统现有北京时间口径。统计时必须先把 `scheduled_at`、`executed_at` 归一化到该时区，再落入小时桶，避免 aware / naive datetime key 不一致导致统计为 0。

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

已过计划时间但仍未执行的 action 不计入“未来待执行覆盖”，应单独统计为 `overdue_open_count` 并进入 `dispatcher_lag` 或执行滞后诊断。这样可以避免“pending 数量看起来覆盖缺口，但实际都已经过期没发”的误判。

硬目标缺口：

```text
deficit = hourly_min_messages - success_current_hour - future_open_current_hour
```

当 `deficit > 0` 时，Planner 必须尝试追加规划。

### 4.5 端到端完成链路

硬目标达成不是单一发送计数，而是以下阶段全部串通后的结果：

| 阶段 | 完成条件 | 未完成原因码 |
| --- | --- | --- |
| 入群完成 | 账号已加入目标群或通过邀请链接进入目标群 | `target_join_pending` / `target_membership_pending` |
| 验证完成 | 群管理 bot、验证码、加减验证、人工审批或关注频道要求已处理并复检通过 | `target_verification_pending` / `target_verification_failed` |
| 验证上下文可读 | 自动验证能读取本次准入触发的验证聊天 / 图片 / 按钮上下文 | `verification_context_unreadable` |
| `can_send` 判定 | 账号-目标关系明确写回 `can_send=true`，且未被禁言 / 权限不足拦截 | `target_can_send_blocked` |
| MiMo/Mino draft 成功 | 健康的小米 MiMo/Mino 供应商返回可解析、可通过质量门的 AI 候选 | `ai_generation_unavailable` / `quality_filter` |
| 发送动作被 dispatcher 消化 | `send_message` action 已到计划时间并被 dispatcher claim / 执行 / 回写结果 | `dispatcher_lag` |
| 硬小时目标达成 | 当前小时真实 `send_message success` 达到 `hourly_min_messages` | `hard_hourly_missed` |

任务详情必须同时展示这些阶段，不能只展示一个泛化的“任务运行中”。当阶段卡住时，缺口仍保留在 `hard_hourly_deficit`，并把对应原因写入 `hard_hourly_last_blockers`。

准入引用和发送引用必须分开验收：`join_ref` 可来自邀请链接、username 或历史 peer；验证上下文读取和 `can_send` 复检必须使用本次准入成功的可读 peer；最终发送规划必须使用已证明 `can_send=true` 的 send peer。不能把 stale numeric peer 上的旧本地群记录当作本次准入成功证据。

## 5. 用户故事

1. 运营人员创建 AI 活跃群任务时，可以设置“每小时最低发送量 10 条”。
2. 到 20:30 时，本小时只成功发送 4 条，且只剩 1 条待执行，系统自动识别缺口 5 条并追加规划。
3. 追加规划时，系统自动提高本轮生成数量，并把计划时间压缩到本小时剩余窗口内。
4. 如果没有新真人上下文，系统不再一直等待默认空闲间隔，而是立即尝试空闲续聊或暖场。
5. 如果最终因账号容量不足只发出 7 条，任务详情必须显示“硬目标未达成：账号容量不足 / TG 限制 / AI 生成不足”等原因。
6. 运营人员在任务列表中能看到当前小时完成度，例如 `7 / 10`，而不是只看到 pending 总数。

## 6. 产品交互

### 6.1 创建和编辑任务

AI 活跃群任务高级设置增加“每小时硬目标”区域：

| 控件 | 默认值 | 说明 |
| --- | --- | --- |
| 启用每小时硬目标 | 开启 | AI 活跃群必须启用每小时硬目标 |
| 每小时最低发送量 | 10 | 必填，最小 10 |
| 未达标处理 | 强推规划 | 第一版固定，不提供其他选项 |

开启硬目标后，页面提示：

```text
系统会自动提高规划强度以追赶本小时最低发送量；真实执行仍受账号容量、目标权限、TG 限制、AI 质量和风控限制约束，未达标原因会在任务详情中展示。
```

### 6.2 任务列表

AI 活跃群任务卡片增加硬目标摘要：

```text
本小时硬目标 7 / 10
缺口 3
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
- 端到端阶段状态：入群、验证、`can_send`、MiMo/Mino draft、dispatcher、硬目标。
- 最近一次强推规划时间。
- 最近一次强推生成数量。
- 最近一次未达标原因。
- 当前小时失败原因分布。

示例：

```text
20:00-21:00
目标 10，已成功 7，待执行 1，缺口 2
最近强推：20:42，追加规划 5 条，实际创建 4 条
未达标原因：账号容量不足 1，AI 质量过滤 1
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
  "hourly_min_messages": 10,
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
  "hard_hourly_goal": 10,
  "hard_hourly_bucket": "2026-06-07T20:00:00+08:00",
  "hard_hourly_success_count": 7,
  "hard_hourly_open_count": 0,
  "hard_hourly_overdue_open_count": 0,
  "hard_hourly_deficit": 3,
  "hard_hourly_status": "catching_up",
  "hard_hourly_last_check_at": "2026-06-07T20:42:15+08:00",
  "hard_hourly_last_planned_count": 3,
  "hard_hourly_pipeline": {
    "membership": "ready",
    "verification": "ready",
    "can_send": "ready",
    "ai_draft": "ready",
    "dispatcher": "ready",
    "hourly_target": "catching_up"
  },
  "hard_hourly_last_blockers": {
    "account_capacity": 2,
    "quality_filter": 1
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

`task.stats` 同时保留最近 24 个小时桶摘要，避免小时切换后丢失未达标事实：

```json
{
  "hard_hourly_recent_buckets": [
    {
      "bucket": "2026-06-07T20:00:00+08:00",
      "goal": 10,
      "success_count": 7,
      "future_open_count": 0,
      "overdue_open_count": 0,
      "deficit": 3,
      "status": "missed",
      "blockers": {
        "account_capacity": 2,
        "quality_filter": 1
      }
    }
  ]
}
```

第一版可以把最近 24 小时摘要放在 `task.stats`；如果后续运营数据需要跨天聚合，再落入 `RuntimeMetricSnapshot` 或专门的小时统计表。

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

当 `send_message` 已进入 Telegram 调用并被群管理提示“需要关注频道才能发言”或消息被删除时，不能把该动作写成成功，也不能永久降级为普通发送失败。Dispatcher 必须从失败详情、按钮或关联频道中解析必需频道，完成自动关注；如果验证消息带“我已加入 / 我已关注 / 完成验证”确认按钮，必须点击确认后再复检目标群 `can_send`。复检通过后，原发送动作回到 `pending`，并在 result 中记录：

```json
{
  "error_code": "required_channel_followed_retry",
  "required_channels_followed": ["qiyue201"],
  "prerequisite_channel_followed": true
}
```

如果必需频道解析、关注或复检失败，动作保持真实失败原因；不得用模板发送、mock 成功或跳过消息来抵扣硬小时目标。

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

进入强推规划前必须先执行目标准入门：

- 目标群未加入时生成或复用 `ensure_target_membership`，不能直接规划发送。
- 已加入但验证未完成时，继续处理验证码 / 加减验证 / 关注频道 / 人工审批队列。
- 已加入且验证通过后，必须复检账号在目标群的 `can_send` 能力。
- 只有 `can_send=true` 的账号可以进入 AI draft 和发送动作规划。
- 硬目标模式可以加速准入动作和验证重试，但不能把未验证账号当成可发送账号。

### 8.2 强推规划

强推规划要求：

- 本轮有效发言数优先按 `deficit` 计算。
- `messages_per_round_mode=auto` 时，硬目标控制器可以临时提高本轮数量。
- `messages_per_round_mode=manual` 时，硬目标控制器仍可以临时突破该轮次值，因为硬目标优先于自然轮次。
- `allow_account_repeat` 临时视为 `true`，但仍受账号容量、目标权限和风控限制。
- 发送计划时间压缩到当前小时剩余窗口。
- 如果当前小时剩余时间不足，仍尽量创建最近可执行时间；执行层如果因容量或 TG 限制延后，必须记录未达标原因。
- 单次强推未能创建任何发送 action 时，必须记录原因并设置下一次检查时间，不能在同一 planner tick 中无限重试 AI 生成或质量过滤。

### 8.3 上下文等待

硬目标启用后，默认等待新真人上下文的行为需要调整：

- 如果有缺口，系统应立即尝试空闲续聊或暖场，而不是继续等待默认 `idle_continuation_seconds`。
- 如果没有任何可用上下文，允许使用目标画像、历史摘要和 bootstrap prompt 生成暖场内容。
- 生成内容仍必须通过事实锚点、重复度、低置信度和内容规则检查。
- 如果质量门拒绝所有候选，记录 `quality_filter` 或 `no_context_quality_blocked`，不能伪造发送。
- 文本 draft 必须使用健康的小米 MiMo/Mino 供应商。若任务配置了非 MiMo/Mino 供应商或租户默认供应商不是 MiMo/Mino，硬目标任务应暴露配置错误或 AI 不可用；不得自动降级到 mock、本地模板或其他供应商。

### 8.4 账号和容量

硬目标可以提高规划强度，但执行前仍必须检查：

- 账号在线 / 可用。
- 账号已经完成入群、验证和关联频道关注要求。
- 账号在目标群具备发送能力，且账号-目标关系明确为 `can_send=true`。
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

### 8.6 强推失败后的重试节奏

硬目标是强推目标，不是无限循环。缺口存在但本次强推没有创建 action 时，Planner 必须把失败原因写入 stats，并按原因设置下一次检查时间：

| 原因 | 下一次检查 |
| --- | --- |
| `account_capacity` | 账号下一次容量窗口或 1 分钟后，取较早的可执行时间 |
| `target_membership_pending` | membership 下一次计划时间或 1 分钟后 |
| `target_join_pending` | 入群动作下一次计划时间或 1 分钟后 |
| `target_verification_pending` | 验证读取 / 自动提交下一次计划时间或 1 分钟后 |
| `target_can_send_blocked` | 权限复检或人工处理后重试 |
| `no_context` | 30-60 秒后重试空闲续聊 / 暖场 |
| `quality_filter` | 60-180 秒后重试，并记录过滤原因 |
| `ai_generation_unavailable` | 1-5 分钟后重试 |
| `tg_rate_limit` | FloodWait / TG 返回的可重试时间 |
| `dispatcher_lag` | 不新增大量重复 action，优先等待 recovery / dispatcher |

这个节奏是透明的运行控制，不是静默降级：目标值不降低，缺口不清零，任务详情必须显示“仍未达标”和下一次检查时间。

## 9. API 变更

### 9.1 创建任务

`POST /api/tasks/group-ai-chat`

新增字段：

```json
{
  "hard_hourly_target_enabled": true,
  "hourly_min_messages": 10,
  "hard_hourly_strategy": "force_planning"
}
```

### 9.2 编辑任务

`PATCH /api/tasks/{task_id}/group-ai-chat`

允许更新同样字段。更新后从下一次 planner tick 生效，不重写已存在 action。

### 9.3 任务列表 / 详情 / Stats

现有任务输出的 `stats` 中增加硬目标字段。前端不需要新增单独查询接口。

任务列表、任务详情和 `/stats` 三个入口必须使用同一套硬目标统计函数。列表接口不能直接返回旧 `task.stats` JSON；如果在读请求中刷新 stats，必须保证响应和后续列表读取都使用刷新后的值，避免详情已清理但列表仍显示旧状态。

### 9.4 预检

`POST /api/tasks/precheck` 增加硬目标预检摘要：

```json
{
  "hard_hourly_target": {
    "enabled": true,
    "hourly_min_messages": 10,
    "estimated_hourly_capacity": 7,
    "capacity_gap": 3,
    "warnings": [
      "硬目标高于当前账号容量，可能持续未达标"
    ]
  }
}
```

预检只提示风险，不因为目标过高直接禁止创建；但目标群不存在、无可用账号、规则绑定非法等既有硬错误仍按原逻辑处理。

预检必须把硬目标和当前账号容量分开展示：

- `hourly_min_messages`：运营人员要求的硬目标。
- `estimated_hourly_capacity`：按当前账号、容量、目标权限估算的真实可达量。
- `capacity_gap`：硬目标和估算容量之间的差距。
- `hard_target_over_capacity`：硬目标是否超过估算容量。

不得因为 `max_actions_per_hour` 低于硬目标就把容量估算截断为普通上限；硬目标模式下普通上限只作为风险提示。

## 10. 实现触点

第一版实现必须覆盖以下触点，避免只改 planner 导致 UI 或统计仍旧：

| 触点 | 要求 |
| --- | --- |
| `backend/app/schemas/task_center.py` | `GroupAIChatConfig`、创建、编辑和预览请求增加硬目标字段 |
| `backend/app/services/task_center/config_fields.py` | 允许新字段通过任务配置白名单 |
| `backend/app/services/task_center/precheck.py` | 返回硬目标容量预检摘要 |
| `backend/app/services/task_center/executors/group_ai_chat.py` | 计算当前小时缺口、强推生成、压缩计划时间、写 action 标记 |
| `backend/app/services/task_center/channel_membership.py` | AI 活跃群目标先完成入群、验证、关联频道关注和 `can_send` 复检 |
| `backend/app/services/membership_challenges.py` | 图片验证码使用健康的多模态视觉供应商（MiMo/Mino 或 MiniMax），记录读取、识别、提交和复检结果 |
| `backend/app/services/task_center/pacing.py` | 支持硬目标剩余窗口内的计划时间分配 |
| `backend/app/services/task_center/stats.py` | 统一刷新当前小时和最近 24 小时硬目标 stats |
| `backend/app/services/task_center/service.py` | 列表、详情、stats、reset、resume 使用同一统计口径 |
| `frontend/src/app/views/TaskCenterView.tsx` | 创建、编辑、保存 payload 增加硬目标字段 |
| `frontend/src/app/views/TaskCenterWizardSections.tsx` | 增加硬目标表单和说明 |
| `frontend/src/app/views/taskCenterViewModel.ts` | 默认值、提交字段、编辑字段和摘要展示 |
| `frontend/src/app/types/taskCenter.ts` | 类型定义增加硬目标字段和 stats 字段 |

重置任务时应清空当前硬目标运行态和最近小时桶，但保留任务配置；暂停任务时不追加规划；恢复任务时从恢复后的当前小时重新计算目标。

## 11. 验收口径

### 11.1 后端验收

- 旧任务未配置硬目标时，行为不变。
- 开启 `hourly_min_messages=10` 后，当前小时成功 + 待执行小于 10 时，planner 创建补量发送动作。
- 补量动作带 `hard_hourly_target=true` 和小时桶。
- 当前小时达标后，不再继续为硬目标追加动作。
- 目标群未加入时先生成 / 执行 `ensure_target_membership`，发送动作不会绕过准入阶段。
- 入群后如果遇到群管理 bot、图片验证码、加减验证、人工审批或要求关注多个频道，任务详情必须显示验证阶段和具体失败 / 等待原因。
- `can_auto_resolve=true` 只代表可尝试自动验证；如果读取验证聊天失败，例如 `private`、`lack permission`、`banned` 或 `GetHistoryRequest`，必须记录 `verification_context_unreadable`，不得算作自动验证已完成。
- join ref、verification peer 和 send peer 必须分开展示和验收；通过 username / invite 完成准入时，不能继续用旧 numeric peer 读取验证码或规划发送。
- 需要关注多个频道才能入群时，每个频道关注动作要有独立结果；全部必需频道满足后才允许复检目标群 `can_send`。
- 已跳过的准入失败如果错误详情包含必需频道链接或“需要关注频道”提示，硬目标模式必须按重试周期重新生成准入动作，让自动关注和 `can_send` 复检继续推进；不能把这类账号永久留在失败池。
- 发送阶段被管理员提示“需要关注频道才能发言”或消息被删除时，dispatcher 必须自动关注必需频道、复检 `can_send`，并把原发送动作重排；只有重发成功才计入硬目标成功数。
- 验证完成但 `can_send=false` 时，不创建主发送动作，原因记录为 `target_can_send_blocked`。
- 文本 draft 使用小米 MiMo/Mino 健康供应商；MiMo/Mino 不可用、返回空内容或 malformed JSON 时记录 `ai_generation_unavailable`，不得走 mock 或模板成功。
- 当前小时无真人新上下文时，硬目标可以触发空闲续聊 / 暖场。
- 质量过滤、内容规则、账号容量、目标权限和 TG 限制导致无法补足时，stats 记录原因。
- `max_actions_per_hour` 小于硬目标时，不阻止硬目标规划，但 stats / 详情显示目标超过普通上限。
- 已存在未来 pending 动作会计入 `open_current_hour`，避免重复规划。
- 已过期 pending 不计入未来待执行覆盖，必须进入 overdue / dispatcher lag 口径。
- 小时切换后，上一小时未达标结果保留在最近 24 小时桶中。
- `/api/tasks`、`/api/tasks/{id}` 和 `/api/tasks/{id}/stats` 三个入口返回的硬目标 stats 一致。

### 11.2 前端验收

- 创建 AI 活跃群任务时可开启硬目标并填写每小时最低发送量。
- 编辑已建 AI 活跃群任务时可开启、关闭或调整硬目标。
- 任务列表展示当前小时 `成功 / 目标`、缺口和状态。
- 任务详情展示硬目标小时桶、成功数、待执行数、缺口、最近强推和阻塞原因。
- 未开启硬目标的任务不展示多余噪音。
- 当前小时存在已过期 pending 时，详情必须显示执行滞后，而不是把它当成可覆盖缺口的待执行。

### 11.3 线上验收

- 新建一个测试 AI 活跃群任务，设置最低硬目标 10 条 / 小时，确认当前小时缺口会触发追加规划。
- 设置高于账号容量的硬目标，确认系统不会伪造成功，会展示容量不足或 TG 限制。
- 对 3 个线上 AI 活跃群逐一核对：入群完成、验证完成、`can_send`、MiMo/Mino draft、dispatcher 消化和硬目标完成度，不允许只用任务 running 或 pending 数量证明达标。
- 线上验收必须区分“已结束小时”和“当前小时追赶中”：已结束小时保留真实 `missed` 记录，不回写伪造为 `met`；历史缺口必须通过后续真实 `send_message success` 超额补偿，并让 `hard_hourly_backfill_debt=0`。当前小时允许 `catching_up`，但必须满足 `success_count + future_open_count >= hourly_min_messages` 且 `overdue_open_count=0`，否则仍视为未验收。生产 AI 活群质量诊断必须使用当前 action 现场重算 hard-hourly stats；running 任务出现无待执行规划、待执行不足、已过期待执行、`blocked`、当前 `missed` 或历史 `hard_hourly_backfill_debt > 0` 时输出 `AI_GROUP_QUALITY_HARD_HOURLY_GATE_FAILED`，不能仅凭任务 running、pending 或旧 stats 放行。
- 用至少一个需要验证码 / 加减验证 / 关注多个频道的目标验证准入链路；未完成时应停在 membership / verification blocker，完成后应写回 `can_send=true` 并进入发送规划。
- 人为让 MiMo/Mino 返回 malformed JSON 或关闭健康供应商时，任务必须显示 AI draft 阻塞，不能生成 mock 成功消息。
- 对比 `/api/overview`、`/api/tasks`、`/api/tasks/{id}/stats` 和 action 明细，确认成功数、待执行数和缺口一致。
- 暂停任务后，硬目标不再追加规划。
- 恢复任务后，从恢复后的当前小时继续计算目标。
- 人为制造已过期 pending 或 dispatcher 停滞时，确认系统标记 `dispatcher_lag`，不把过期 pending 当作已覆盖缺口。

## 12. 实施优先级

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

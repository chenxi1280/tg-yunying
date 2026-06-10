# AI 活跃群每小时硬目标 OPS

## 1. 运行原则
每小时硬目标是 AI 活跃群的强运营模式。它的目标是让系统主动追赶每小时最低真实发送量，而不是只靠自然曲线等待下一轮。

运行原则：
- 真实成功只认 `send_message` 的 `success`。
- 未达标必须暴露原因。
- 系统可以强推规划，但不能绕过账号安全、目标权限、TG 限制、内容风控和 AI 质量检查。
- 运营人员看到的 pending 必须拆成“当前小时待执行”“未来计划”“已过期未执行”。
- 任务中心是执行事实源，运营中心负责聚合和处理影响。

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
| `hard_hourly_last_blockers` | 最近一次未补足原因分布 |
| `hard_hourly_recent_buckets` | 最近 24 个小时的达标和阻塞摘要 |

### 2.2 Action 级指标
硬目标补量 action 需要带：
| 字段 | 含义 |
| --- | --- |
| `hard_hourly_target` | 是否由硬目标补量生成 |
| `hard_hourly_bucket` | 所属小时桶 |
| `hard_hourly_deficit_at_plan` | 规划时缺口 |
| `cycle_id` | AI 活跃群轮次 |
| `turn_index` | 本轮发言序号 |

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
| `target_permission` | 目标群不可发、账号被禁言、权限不足 | 目标中心处理 |
| `no_context` | 无真人上下文或无可用历史 | 开启硬目标暖场、补充学习来源 |
| `quality_filter` | AI 候选被重复、低置信、事实锚点规则拦截 | 调整提示词 / 质量规则 |
| `ai_generation_unavailable` | AI 服务不可用或生成失败 | 检查 AI 配置和供应商 |
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
5. 账号容量和冷却。
6. AI 生成记录和质量过滤。
7. listener_runtime 最近采集时间。
8. dispatcher worker 心跳。
9. 最近 24 小时硬目标桶。

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

处理：

- 查看 membership-items。
- 确认可入群账号数量。
- 先把失败拆成关注频道、邀请链接失效、目标权限、群管理 bot 图片验证码、人工审批、账号限制和 Telegram API 失败，不得统一写成普通“加入失败”。
- `target_admission_retry` 中出现“未解析到群关联频道”“仍未获群发言权限”或“群无权限或账号不可发言”时，先按疑似群管理验证处理：重新加入 / 复检触发当前验证，读取验证码上下文，图片验证码走 MiMo，提交后再复检；不能直接把整批账号标记为人工处理。
- 如果页面显示“没有读取到最近验证聊天信息”，先按验证聊天读取空态处理：核对目标 peer、账号是否仍能读取群历史、验证消息是否已过期 / 被删除、账号 session 是否有效，以及是否出现 `GetHistoryRequest` 权限错误。
- 单账号弹窗点击“重新读取”时，应先重新加入 / 重试准入来触发当前验证码上下文；如果加入后直接可发言，则关闭待处理；如果仍需验证码，系统再读取最新验证消息。
- 验证码读取允许双账号协作：加入账号负责触发入群和提交验证码；同目标中已可读历史的账号可负责读取群管理 bot / 管理员验证码消息和图片。
- 图片验证码分支必须确认系统设置里存在健康的 MiMo 视觉供应商，且任务启用了 `ai_assisted_verification`。DeepSeek 等纯文本供应商不能处理图片验证码。
- 对图片验证码账号，检查准入明细中的验证消息、图片摘要、MiMo 答案、置信度、发送结果和复检结果；缺少任一环节时按该环节记录失败原因。
- MiMo 未配置、图片不可下载、识别低置信、答案发送失败或复检仍不可发言时，标记人工处理，不再自动反复尝试同一张验证码。
- 主发送不足时记录 `target_membership_pending`，不应写成普通发送失败。

### 7.4 AI 生成不足

现象：

- 强推缺口较大，但实际创建 action 很少。
- AI 生成记录为空或候选全被过滤。

处理：

- 检查 AI Provider 健康。
- 检查 prompt、目标画像、黑话模板和上下文。
- 检查重复度、事实锚点和低置信静默规则。
- 失败必须保留 `ai_generation_unavailable` 或 `quality_filter`，不能把目标当作已完成。

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
hourly_min_messages = 3
账号数量 >= 3
目标群可发送
```

预期：

- 当前小时不足 3 条时自动补量。
- 成功达到 3 条后停止硬目标补量。
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
目标 60
成功 40
当前小时待执行 20
```

预期：

- 缺口为 0。
- 不重复规划 20 条。

### 9.5 已过期 pending 场景

配置：

```text
目标 60
成功 40
当前小时 pending 20，但 scheduled_at 已过
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
AI 活跃群硬目标正常。本小时目标 60，成功 63，缺口 0，无硬目标阻塞。
```

追赶中：

```text
AI 活跃群硬目标追赶中。本小时目标 60，成功 42，当前小时待执行 18，缺口已被待执行覆盖，计划时间未到。
```

执行滞后：

```text
AI 活跃群硬目标存在执行滞后。本小时目标 60，成功 40，未来待执行 5，已过期未执行 15，缺口 15。需检查 dispatcher / worker 心跳和 claim 恢复。
```

异常：

```text
AI 活跃群硬目标未达成。本小时目标 60，成功 45，待执行 3，缺口 12。主要原因：账号容量不足 7、AI 质量过滤 3、TG FloodWait 2。已生成运营异常，需处理账号容量和目标权限。
```

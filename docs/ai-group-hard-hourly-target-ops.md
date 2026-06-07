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
| `hard_hourly_deficit` | 当前小时缺口 |
| `hard_hourly_status` | `met` / `catching_up` / `blocked` / `missed` |
| `hard_hourly_last_check_at` | 最近一次硬目标检查时间 |
| `hard_hourly_last_planned_count` | 最近一次强推创建的发送动作数 |
| `hard_hourly_last_blockers` | 最近一次未补足原因分布 |

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

## 5. 日常巡检流程

### 5.1 每小时巡检

运营人员或值班人员检查：

1. 任务列表中 AI 活跃群硬目标状态。
2. 当前小时 `成功 / 目标`。
3. `hard_hourly_deficit` 是否大于 0。
4. 缺口是否已经有待执行 action 覆盖。
5. 未达标原因是否集中在账号、目标、AI、质量或 dispatcher。

判断口径：

```text
成功数达到目标 -> 正常
成功数未达标，但待执行覆盖缺口且计划时间未到 -> 追赶中
成功数未达标，待执行不足，且 blocker 明确 -> 异常
小时结束仍未达标 -> 异常
```

### 5.2 任务详情排查

进入任务详情后按顺序看：

1. 硬目标执行区。
2. 当前小时 action 明细。
3. 失败 / 跳过 / unknown_after_send。
4. membership 阶段。
5. 账号容量和冷却。
6. AI 生成记录和质量过滤。
7. listener_runtime 最近采集时间。
8. dispatcher worker 心跳。

### 5.3 Action 明细口径

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

## 6. 现场处理手册

### 6.1 账号容量不足

现象：

- `hard_hourly_last_blockers.account_capacity` 较高。
- 多数候选账号被冷却或小时上限挡住。

处理：

- 检查任务账号范围是否太小。
- 增加可发送账号。
- 检查账号健康、代理、登录状态。
- 检查账号小时 / 日容量配置。
- 检查 `allow_account_repeat` 是否被任务配置限制；硬目标模式应临时允许复用。

### 6.2 无上下文或等待新消息

现象：

- `last_error` 包含“暂无新的真人上下文”。
- 硬目标缺口存在，但没有生成足够 action。

处理：

- 确认硬目标模式已经启用强制空闲续聊。
- 检查目标群是否有最近监听数据。
- 检查学习来源和目标画像是否可用。
- 如果质量门全部拦截，查看 `quality_filter` 细分原因。

硬目标模式下，单纯“等待真人上下文”不能作为长期停发理由；系统必须尝试暖场或明确记录质量 / 上下文阻塞。

### 6.3 目标准入未完成

现象：

- pending 中存在 `ensure_target_membership`。
- `membership_stage` 是 `membership_running` 或 `membership_partial`。

处理：

- 查看 membership-items。
- 确认可入群账号数量。
- 处理验证码、关注频道、邀请链接失效或目标权限问题。
- 主发送不足时记录 `target_membership_pending`，不应写成普通发送失败。

### 6.4 AI 生成不足

现象：

- 强推缺口较大，但实际创建 action 很少。
- AI 生成记录为空或候选全被过滤。

处理：

- 检查 AI Provider 健康。
- 检查 prompt、目标画像、黑话模板和上下文。
- 检查重复度、事实锚点和低置信静默规则。
- 失败必须保留 `ai_generation_unavailable` 或 `quality_filter`，不能把目标当作已完成。

### 6.5 Dispatcher 延迟

现象：

- 当前小时有大量 `scheduled_at <= now` 的 pending / claiming / executing。
- worker heartbeat 异常或 claim 堆积。

处理：

- 检查 worker role：planner、dispatcher、listener、recovery。
- 检查 `claiming` 过期恢复。
- 检查运行指标 `actions.oldest_pending_age_seconds`。
- 检查部署版本和 worker 日志。

## 7. 发布与回滚

### 7.1 发布前检查

- 旧任务未启用硬目标时行为不变。
- 创建 / 编辑 AI 活跃群任务可保存硬目标字段。
- Planner 可根据缺口创建补量 action。
- 任务列表 / 详情 / stats 展示一致。
- 预检能显示容量风险。
- 单元和集成测试覆盖硬目标计算、补量、阻塞原因和列表展示。

### 7.2 发布流程

Telegram 相关项目按现有流程：

```text
master -> release -> push release -> GitHub Actions Deploy Production
```

发布后检查：

- `/api/health` 正常。
- `/api/tasks` 中旧任务 stats 正常。
- 新硬目标测试任务可以创建。
- 当前小时硬目标进度正确。
- overview 24 小时活动统计仍为非零。

### 7.3 回滚口径

如果硬目标发布后影响普通任务：

- 先关闭任务级 `hard_hourly_target_enabled`。
- 如果是前端展示问题，保留后端字段，临时隐藏 UI。
- 如果是 planner 误创建大量 action，暂停相关任务并检查补量 action 标记。
- 如果影响全局 worker，回滚 release 到上一成功版本。

## 8. 验收用例

### 8.1 低目标可达场景

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

### 8.2 高目标不可达场景

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

### 8.3 无上下文场景

配置：

```text
hourly_min_messages = 10
目标群无新真人消息
```

预期：

- 系统尝试空闲续聊 / 暖场。
- 如果质量门通过，创建补量 action。
- 如果质量门不通过，记录 `no_context` 或 `quality_filter`。

### 8.4 已有未来 pending 场景

配置：

```text
目标 60
成功 40
当前小时待执行 20
```

预期：

- 缺口为 0。
- 不重复规划 20 条。

### 8.5 已过期 pending 场景

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

## 9. 数据核对 SQL 口径

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

硬目标补量 action：

```sql
select id, account_id, status, scheduled_at, executed_at, result
from actions
where task_id = :task_id
  and action_type = 'send_message'
  and payload ->> 'hard_hourly_target' = 'true'
order by scheduled_at desc;
```

## 10. 值班结论模板

正常：

```text
AI 活跃群硬目标正常。本小时目标 60，成功 63，缺口 0，无硬目标阻塞。
```

追赶中：

```text
AI 活跃群硬目标追赶中。本小时目标 60，成功 42，当前小时待执行 18，缺口已被待执行覆盖，计划时间未到。
```

异常：

```text
AI 活跃群硬目标未达成。本小时目标 60，成功 45，待执行 3，缺口 12。主要原因：账号容量不足 7、AI 质量过滤 3、TG FloodWait 2。已生成运营异常，需处理账号容量和目标权限。
```


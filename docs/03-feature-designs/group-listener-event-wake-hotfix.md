# 群监听新增消息事件唤醒热修 PRD

## 1. 问题与范围

- 级别：L3。新增来源消息已经持久化，但 `TaskPlannerWakeState.not_before_at` 仍可能保留下一拟人化窗口，导致 `group_relay` 目标未及时收到消息。
- 根因：群监听成功路径只提前 `Task.next_run_at`，没有推进 0153 后 Planner 的持久唤醒真相源；频道监听已有事件唤醒，群监听契约缺失。
- 本次只修复“新增群上下文触发 Planner”边界，不改变来源群、目标群、发送账号、内容过滤、去重窗口和 Action 发送节奏。

## 2. 产品合同

1. 群监听一次采集 `inserted > 0` 时，每个订阅中的运行/待运行任务必须记录一次 `group_context_inserted` Planner wake，`not_before_at` 不晚于消息采集时间。
2. 同时保留 `Task.next_run_at` 的兼容投影；Planner 选取以持久 wake 为准，不能再被下一小时拟人化窗口压住。
3. `inserted == 0` 只更新监听健康信息，不产生无意义 wake，避免空轮询放大资源占用。
4. 事件唤醒只允许重新规划；发送仍经过原有过滤、目标解析、去重、账号容量、Action 和 Dispatcher，不改写任何目标引用。
5. 已进入 `unknown_after_send` 的 Action 不重试、不释放目标数量归属，本热修不触碰该状态。

## 3. 数据流与边界

`GroupContextMessage 写入 -> listener_runtime 成功回读 inserted -> TaskPlannerWakeState revision/not_before_at -> Planner -> group_relay 解析原目标 -> Action -> Telegram 远端事实`

- 并发：`wake_task_planner` 在任务维度加锁并递增 revision；Planner 正在运行时到达的新消息由 late-wake 语义保留，不丢事件。
- 幂等：同一采集批次只对订阅任务各 wake 一次；内容级重复仍由 relay fingerprint 拦截。
- 失败：wake 状态不可建立时事务失败并暴露错误，不静默退回只写 `Task.next_run_at`。
- 迁移：无需新增表或字段。
- 回滚：回滚代码即可；不删除 wake 或消息记录。

## 4. 验收与发布门禁

- 自动化：监听采集新增消息后，relay/AI 两类任务均出现 `group_context_inserted` wake 且时间被提前；空采集不 wake。
- PostgreSQL 回归：连续 relay 新增第二条来源消息后产生第二个成功 Action，发送目标 ID/peer 与首轮配置一致。
- 线上：部署 SHA、关键容器、异常日志通过；只读验证新消息 wake 链和目标守恒。没有真实新来源事件时标记 E4 `unproven`，不得用健康检查代替。

`design_status=complete`

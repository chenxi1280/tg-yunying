# AI 活群日覆盖与 Dispatcher Recovery 修复计划

## 目标与完成标准

修复已确认的三条线上阻塞链路：

1. 未到 Telegram Gateway 的 overdue 覆盖 Action 不再被写为 remote unknown；其覆盖行保留 reservation、显示 `dispatcher_lag`，不重复建单。
2. 跨 Window claim counter 漂移不再让 Recovery 因 `dispatch claim ledger underflow` 回滚；过期 Action 能收口并释放全局领取容量。
3. 领取时不再用通用旧时间排期覆盖 `DispatchClaimPlan` 的 allocation/fairness 同优先级顺序；既有类别、任务和公平优先级保持不变，历史同群 backlog 不应反复先领取并触发群慢速模式，从而挤占已分配目标的容量。

完成不以 worker 健康或本地测试代替。生产 E4 需要 Release Action 成功、stale executing 实际下降、Scope active claim 恢复、新的成功 Action 有 `ExecutionAttempt.remote_message_id`，并以完整自然日的任务目标验证。

## 实施顺序

1. 先写回归测试：pre/post-Gateway overdue 分流、legacy unknown terminal 释放、跨 Window counter drift release、stale Recovery coverage sync，以及计划候选顺序与旧排期相反时的锁行顺序。
2. 实现 `daily_fulfillment` Gateway 边界判定；扩展 terminal coverage recovery 状态集。
3. 实现 `release_dispatch_claim` exact counter reconcile 与 Action 审计；让 stale Recovery 在终态后同步 coverage。
4. 添加迁移，原子替换 terminal recovery 部分索引，使其覆盖 `reserved/sending/unknown`。
5. 用 SQL `CASE` 或等价数据库顺序表达 `candidate_action_ids`，保留既有 claim 优先级后仅在同优先级序列内做 `FOR UPDATE SKIP LOCKED`；不扩大候选集合、不放宽群冷却。
6. 运行定向测试、编译和 diff 检查；只提交本计划涉及文件。
7. 推送 `release`，等待 Deploy Production 成功，确认部署 SHA；采样 Recovery 日志、executing/claim scope、计划份额、群慢速模式 churn 和新远端发送事实。

## 边界

- 不降低群冷却、内容质量、账号、准入、风控或 `unknown_after_send` 门。
- 无 `gateway_call_started_at` 的 Action 不能被当作 Telegram 远端未知；有该事实的 Action 不能自动重发。
- ledger binding 缺失继续显式失败；仅 counter 投影漂移允许按持久 Action 事实修复。
- `legacy_group_slot`、15 秒群冷却和 `account_only` 的显式 canary 边界不变；本计划只修复已获 claim 份额的领取顺序。
- 搜索点击的本地未提交改动不在本次范围内。

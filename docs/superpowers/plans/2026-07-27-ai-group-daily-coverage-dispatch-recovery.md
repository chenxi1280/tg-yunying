# AI 活群日覆盖与 Dispatcher Recovery 修复计划

## 目标与完成标准

修复已确认的五条线上阻塞链路：

1. 未到 Telegram Gateway 的 overdue 覆盖 Action 不再被写为 remote unknown；其覆盖行保留 reservation、显示 `dispatcher_lag`，不重复建单。
2. 跨 Window claim counter 漂移不再让 Recovery 因 `dispatch claim ledger underflow` 回滚；过期 Action 能收口并释放全局领取容量。
3. 领取时不再用通用旧时间排期覆盖 `DispatchClaimPlan` 的 allocation/fairness 同优先级顺序；既有类别、任务和公平优先级保持不变，历史同群 backlog 不应反复先领取并触发群慢速模式，从而挤占已分配目标的容量。
4. 同一 `legacy_group_slot` 群不能被多个 worker 在 Gateway 前同时领取；真实 Gateway 槽位提交后，下一合法时刻必须持久化并在下一轮 claim 前被识别，不能靠放宽群冷却补吞吐。
5. 群管 bot 的广播式频道要求在来源已由管理员身份或目标级审计 policy 确认时，必须在正文前按运行中任务 scope 为每个既有 admission 创建精确频道 follow/callback；未知 bot、明确收件人不匹配和普通推广继续不动作。

完成不以 worker 健康或本地测试代替。生产 E4 需要 Release Action 成功、stale executing 实际下降、Scope active claim 恢复、新的成功 Action 有 `ExecutionAttempt.remote_message_id`，并以完整自然日的任务目标验证。

## 实施顺序

1. 先写回归测试：pre/post-Gateway overdue 分流、legacy unknown terminal 释放、跨 Window counter drift release、stale Recovery coverage sync、计划候选顺序与旧排期相反时的锁行顺序、同群 claim slot 及可信全群频道规则。
2. 实现 `daily_fulfillment` Gateway 边界判定；扩展 terminal coverage recovery 状态集。
3. 实现 `release_dispatch_claim` exact counter reconcile 与 Action 审计；让 stale Recovery 在终态后同步 coverage。
4. 添加迁移，原子替换 terminal recovery 部分索引，使其覆盖 `reserved/sending/unknown`。
5. 用 SQL `CASE` 或等价数据库顺序表达 `candidate_action_ids`，保留既有 claim 优先级后仅在同优先级序列内做 `FOR UPDATE SKIP LOCKED`；不扩大候选集合、不放宽群冷却。
6. 新增 `TgGroup.next_group_send_slot_at` 迁移；claim plan 前过滤未来 legacy 槽位，Action 行锁后取得 `TgGroup FOR UPDATE SKIP LOCKED` 并只保留每群计划最靠前的一条。Gateway 最终校验通过后与 `ExecutionAttempt.gateway_call_started` 同事务推进下一槽位。
7. 在 listener 的来源过滤和精确提示识别之后，只有管理员 bot 或同 group+peer 的 active source-bound policy 才能对无明确收件人的广播规则做 scope 内逐账号展开；follow/callback 必须保留原 source message、peer、URL/按钮和 admission/version。
8. 运行定向测试、迁移可逆测试、编译和 diff 检查；只提交本计划涉及文件。
9. 推送 `release`，等待 Deploy Production 成功，确认部署 SHA；采样 Recovery 日志、executing/claim scope、计划份额、同群并发领取、群慢速模式 churn、频道 follow/confirmation 和新远端发送事实。

## 边界

- 不降低群冷却、内容质量、账号、准入、风控或 `unknown_after_send` 门。
- 无 `gateway_call_started_at` 的 Action 不能被当作 Telegram 远端未知；有该事实的 Action 不能自动重发。
- ledger binding 缺失继续显式失败；仅 counter 投影漂移允许按持久 Action 事实修复。
- `legacy_group_slot`、15 秒群冷却和 `account_only` 的显式 canary 边界不变；本计划只修复已获 claim 份额的领取顺序。
- `next_group_send_slot_at` 是领取协调投影，不是发送成功事实；成功仍须 Action、ExecutionAttempt 与非空 `remote_message_id`。
- source-bound policy 只是未知角色 bot 的受限信任根；它不能重放无来源历史消息、直接设置 ready 或跳过 confirmation。
- 搜索点击的本地未提交改动不在本次范围内。

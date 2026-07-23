# 2026-07-23 搜索点击 Redis 账号锁遗留恢复

## Intake / 生产诊断

- 用户目标：郑州 `search_join_group` 任务当天完成 `500` 次目标点击和 `80` 个实际入群确认；同账号可重复申请，点击与入群分别计数。
- 线上任务：`fdb48029-4fda-4801-818d-0c509da37ea3`。
- 现场证据：生产于 2026-07-23 13:29 CST 重新发布并重启 worker 后，任务存在已到期的 `search_join` / `search_join_membership` action；其 `claim_released_reason=account_inflight_conflict`。
- 根因证据：Redis `inflight:account:<id>` 的持有 token 指向已 `failed` 的其他任务 action，TTL 仍约 24 分钟。旧 worker 被重启后进程内清理无法执行，持久 Redis 锁未随 action 终态释放。

## 分级与设计

- level：L3；影响生产 dispatcher、账号资源锁和 Telegram 执行链路，不能按 L0/L1 quick fix 处理。
- 产品口径不变：`target_found` 只计点击；`membership_observed` 才计实际入群。`join_request_pending`、`membership_pending` 不得计为已加入。
- 修改范围：仅在领取 action 时识别“锁 token 明确属于已终态 action”的孤儿 Redis 账号锁，使用 token 比较删除后重试一次当前领取。
- 安全边界：不清理锁持有者为 `claiming` 或 `executing` 的锁；不删除 token 不能解析为 action id 的锁；不改变 Telegram 调用、任务配额、账号选择或入群计数。

## 验收

- 红测：已终态 holder 的 Redis 锁会阻塞新的 action 领取。
- 绿测：新 action 可领取并写入可见 `terminal_holder_lock_recovered_action_id` 事实；仍执行 holder 的锁继续阻塞。
- 发布后 E4：生产 worker 健康；原任务的 overdue action 不再因终态 holder 的 `account_inflight_conflict` 长时间滞留；点击和入群事实分别复核。

## 升级 / 回滚条件

- 若锁 holder 仍为 `executing` 却被清理、或出现同账号并发 Telegram 调用，立即回滚此提交并保留 Redis / action 证据。
- 若 Telegram 管理员未审批入群申请，`membership_observed` 仍为 0 属于外部审批阻塞，不能通过代码或计数口径绕过。

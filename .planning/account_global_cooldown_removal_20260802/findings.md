# Findings

- 2026-08-02 最新只读 E4：搜索任务 `fdb48029-4fda-4801-818d-0c509da37ea3` 为 `0/1000`，Action/Attempt/assignment 均为 0，`last_error=account_cooldown`。
- 搜索任务自身 `cooldown_per_account_minutes=0`、`per_account_cooldown_days=0`；当前阻塞来自共享 `SchedulingSetting.default_account_cooldown_seconds`。
- `account_capacity_decision()` 把全部 Action/MessageTask 的过去与未来占用纳入全局冷却，持续任务可让短冷却反复续期。
- 当前 `normalize_fulfillment_scheduling_settings()` 只归一小时/日上限，没有归零全局短冷却，这是发布后仍保留旧值的直接缺口。
- 现有测试明确断言接管后仍保留 `default_account_cooldown_seconds=30`，说明旧行为是被测试冻结的产品合同，不是偶发配置漂移；本次必须先更新 PRD 与红测。
- 正式部署脚本调用 `takeover_all_task_fulfillment.py`，其 apply 路径会执行 `normalize_fulfillment_scheduling_settings()` 并提交，因此最小生产修复入口明确，无需手工改线上数据库。
- 主 PRD 当前写“账号短冷却只作软延后”；但实际候选生成会直接过滤账号，持续跨任务占用可形成饥饿。本次产品口径应改为单租户五类履约任务全局短冷却固定为 0。
- 风控中心仍暴露“账号全局冷却”编辑项，AI 配置与风控策略两个写入口都允许重新写入非零值；仅部署归零不足以防复发。修复需移除前端编辑项，并在两个请求 schema 上明确拒绝非零值。
- 首次 release `20260802064335_dffd9593` 已证明平台/租户配置均为 0，但上线后两次 E4 的搜索任务运行态完全不变。`_open_actions_state()` 会把 AI 任务的到期 open Action 时间反写为仍过期的 `next_run_at`；`_normal_planner_task_ids(limit=100)` 因此反复领取同一批队首，其他已到期任务可被饿死。
- 完整恢复需要两项最小补充：接管清除遗留 `last_error=account_cooldown` 并唤醒；已有到期 open Action 的 AI 任务延后 30 秒再检查，让其他任务进入下一次 Planner 领取。未来 Action 仍保留真实计划时间。
- `Deploy Production` 诊断 run `30737496043` 的 faulthandler 证明 Planner 并非空闲：手工 drain 先等待生产 Planner 持有的 `TaskGroupDailyTarget FOR UPDATE`，随后连续两分钟停在 `group_ai_scope.successful_own_history_reply_facts()`。旧 SQL 的 Attempt 子查询先对生产全表 `status=success + remote_message_id<>''` 按 action 聚合，Task/群过滤在外层，历史量增长后形成分钟级查询。
- 根因修复应从当前 Task/群的成功 Action 出发，利用既有 `(action_id, attempt_no)` 唯一索引相关读取每个 Action 最新成功 remote ID；不能以跳过 AI、超时吞错或减少目标规避阻塞。
- `b0c0216d` 上线后搜索 `next_run_at` 从 `15:53` 推进到 `15:56`，证明 Planner 已返回；但 reservation/epoch/assignment 仍为 0。`dispatch_reservations._scope_demands()` 在 `4b0e1016` 被改为只返回已物化 Action demand，普通 Dispatcher 总是先把每分钟 Window 置为 ready；搜索 Planner 后到时不会重分配，因此未物化搜索义务永远拿不到第一份中央 reservation。
- 搜索 demand 必须在任一 Window 创建入口合并，同时保留普通 Task parent-first 份额。此前为恢复 AI 容量删除搜索 demand 的前置原因是 Planner 长阻塞导致 reservation 无人消费；§2.9 已修复该查询，不能继续用删除搜索需求掩盖竞态。

# Findings

- 2026-08-02 最新只读 E4：搜索任务 `fdb48029-4fda-4801-818d-0c509da37ea3` 为 `0/1000`，Action/Attempt/assignment 均为 0，`last_error=account_cooldown`。
- 搜索任务自身 `cooldown_per_account_minutes=0`、`per_account_cooldown_days=0`；当前阻塞来自共享 `SchedulingSetting.default_account_cooldown_seconds`。
- `account_capacity_decision()` 把全部 Action/MessageTask 的过去与未来占用纳入全局冷却，持续任务可让短冷却反复续期。
- 当前 `normalize_fulfillment_scheduling_settings()` 只归一小时/日上限，没有归零全局短冷却，这是发布后仍保留旧值的直接缺口。
- 现有测试明确断言接管后仍保留 `default_account_cooldown_seconds=30`，说明旧行为是被测试冻结的产品合同，不是偶发配置漂移；本次必须先更新 PRD 与红测。
- 正式部署脚本调用 `takeover_all_task_fulfillment.py`，其 apply 路径会执行 `normalize_fulfillment_scheduling_settings()` 并提交，因此最小生产修复入口明确，无需手工改线上数据库。
- 主 PRD 当前写“账号短冷却只作软延后”；但实际候选生成会直接过滤账号，持续跨任务占用可形成饥饿。本次产品口径应改为单租户五类履约任务全局短冷却固定为 0。
- 风控中心仍暴露“账号全局冷却”编辑项，AI 配置与风控策略两个写入口都允许重新写入非零值；仅部署归零不足以防复发。修复需移除前端编辑项，并在两个请求 schema 上明确拒绝非零值。

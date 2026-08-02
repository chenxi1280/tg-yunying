# 全局账号冷却取消与生产恢复计划

## Goal

取消跨任务 `default_account_cooldown_seconds` 对重复任务的阻塞，并通过正式发布把生产配置归一为 0；保留 Telegram 远端限流、授权、代理、验证码及真实业务事实门禁。

## Success Criteria

- 产品与数据流口径明确：重复任务不受跨任务短冷却阻断。
- 接管/发布对平台与租户调度配置幂等写入 `default_account_cooldown_seconds=0`。
- 红测覆盖存量非零值被归零、重复执行无漂移、审计字段正确。
- 定向后端测试与发布质量门通过。
- `master -> release -> Deploy Production` 成功，生产当前 release 与目标 SHA 一致。
- 发布后搜索任务不再以 `account_cooldown` 停在 Action 创建前，并出现新的 Action/Attempt 或 `target_click_observed` 证据。

## Phases

1. [complete] Product / diagnosis：核对正式 PRD、接管入口、部署调用与现有测试。
2. [complete] Dev：先写红测，再最小修改归一化与文档/索引。
3. [complete] QA / product acceptance：运行定向测试、静态检查和 diff 审查。
4. [in_progress] Release：提交、推送 master/release，等待 Actions 与生产切换。
5. [pending] Production E4：复核生产配置、任务账本和真实搜索点击事实。

## Boundaries

- 不修改用户现有脏工作区文件。
- 不删除通用配置字段/API；本次只冻结当前单租户履约策略为 0，避免扩大无关兼容面。
- 不把容器健康、CI 或 Action.success 单独视为搜索完成。

## Errors Encountered

- 生产 SSH 直连在 banner 阶段超时；发布与生产取证优先使用现有 GitHub Actions 审计通道。
- 新合同红测首次运行按预期失败：存量 `default_account_cooldown_seconds=30` 接管后仍为 30。
- 非 `no_postgres` 旧回归选择在收集阶段被测试 PostgreSQL `172.28.232.109:5432` 断开阻塞，未伪报通过。
- 首次生产 E4 连续两次显示搜索任务仍保持发布前 `last_error=account_cooldown`、过期 `next_run_at` 且 Action/Attempt/epoch 全为 0；生产 AI 任务同时存在过期 open Action 队首，暴露 Planner limit 饥饿与遗留错误未唤醒缺口。

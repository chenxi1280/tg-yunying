# Progress

## 2026-08-02

- 已确认 checkout：`release@9a53e298`；存在用户/其他会话规划与文档改动，全部保持不动。
- 已确认技能文件 `tg-yunying-release-recovery`、`production-actions-release` 为占位模板，执行以项目 AGENTS、正式 PRD、Actions 和生产 E4 为准。
- 已建立独立 scoped 计划，进入 Product / diagnosis。
- 已定位旧合同：接管只归一账号小时/日上限，测试要求保留短冷却；部署 apply 会调用该接管入口。
- Product 决策：不删除通用字段/API，发布接管对当前平台/租户存量行幂等归零；远端 FloodWait/授权/代理/验证码继续 fail closed。
- 已更新主 PRD、专项 PRD、数据流和结构索引，Product Design Complete。
- 红测 `test_single_user_scheduling_limits_and_cooldown_are_normalized_idempotently` 按预期失败：`30 != 0`，证明实现缺口。
- 已实现平台/租户调度行的冷却幂等归零、审计明细和部署日志字段；两个配置 API 明确拒绝非零值，风控中心移除编辑项并显示“已取消”。
- 后端定向回归 `backend/tests/test_fulfillment_takeover.py`：`15 passed`；前端 `npm run build`：通过。
- 旧运行时测试选择在收集阶段因本机 PostgreSQL 测试库断连而阻塞，未运行、未计为通过。
- Python `compileall` 与 `git diff --check` 通过；远端 `master/release` 均仍为基线 `9a53e298`，可以按正式顺序发布。
- 首次提交 `dffd9593` 已发布为 `20260802064335_dffd9593`；Actions `30736171661` 全绿，生产平台/租户 `account_cooldown_seconds` 均为 0 且二次 preview `changed=false`。
- 上线后 E4 `30736583658`、`30736645753` 均显示搜索任务未被 Planner 重领；定位为遗留错误未清除叠加到期 AI open Action 队首饥饿。
- 新增两个红测后最小修复：接管清遗留冷却错误并立即唤醒，Planner 对到期 open Action 延后 30 秒；定向红测已转绿 `2 passed`。
- 两个相关文件的完整无 PostgreSQL 回归为 `38 passed, 8 deselected`；Python `compileall`、`git diff --check` 通过，远端仍与首次提交 `dffd9593` 对齐。

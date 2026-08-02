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

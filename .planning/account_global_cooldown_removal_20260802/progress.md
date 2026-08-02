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
- 第二次提交 `e05690ad` 已由 Actions `30736911541` 全绿发布为 `20260802070808_e05690ad`；生产接管清除了搜索任务遗留冷却错误并唤醒，但搜索仍未产生 Action。
- 同 SHA 诊断重发版 `20260802072358_e05690ad` 的 run `30737496043` 用 faulthandler 取得分钟级阻塞堆栈：Planner 卡在 AI own-history 的全表 `ExecutionAttempt` 聚合，240 秒超时；E4 同时确认搜索仍为 0 Action/Attempt。
- 已新增 SQL 形状红测，旧实现命中 `GROUP BY execution_attempts.action_id` 失败；改为从当前 Action 相关读取最新成功 Attempt 后转绿，并覆盖多 Attempt 最新 remote ID 与 open reply 占用排除，`1 passed`。
- 第三次提交 `b0c0216d` 由 Actions `30738255265` 全绿发布为 `20260802074846_b0c0216d`；生产冷却仍为 0，Planner 已推进搜索 next_run，但搜索仍无中央 reservation。
- 新增普通 Dispatcher 先创建 Window 的红测，旧实现因缺 `SEARCH_RESERVATION_KEY` 失败；恢复共享 search demand 合并后，search 与 ordinary 各获一个 parent-first reservation，搜索 Planner 可从同一 ready Window 读取 fulfillment unit；完整文件 `8 passed`。
- 第四次提交 `ae59c2fb` 由 Actions `30739006884` 全绿发布为 `20260802081150_ae59c2fb`；生产已持续产生搜索 reservation，但 assignment/action 仍为 0。
- 生产只读求解快照确认最近 12 轮每轮 16–20 个本任务 demand、65 条候选路径、1040–1300 容量，均在创建后约 0.1 秒 `abandoned`，不是窗口超时或账号不足。
- 新增心跳 upsert 红测，修复前返回旧 fencing token 并按预期失败；`populate_existing=True` 修复后，worker/heartbeat/search epoch/solver 相关回归 `56 passed`。
- 根因提交 `8d790240` 已由 Actions `30740237386` 全绿发布为 `20260802084952_8d790240`；平台/租户全局冷却均为 0，共享调度合同 `active_verified`、2 个分片 live。
- 发布后搜索任务新增 40 个 assignment（37 consumed、3 expired release）、37 个 Attempt；两轮 solver 分别 `optimal/matched=19`、`optimal/matched=21`，冷却与 fencing 修复取得生产 E4 调度证据。
- 新 Attempt 的远端结果为 37/37 `search_transport_unavailable/TimeoutError`，尚无 `target_click_observed`；只读出口探测确认旧 16/16 Mihomo 节点全部失效。
- 手工运行正式代理刷新 `30740981229`；质量门全绿，但当前订阅的 63/63 新节点全部出口探测失败，未改生产 DB/绑定。历史成功 run `30534529674` 使用相同 `skip-cert-verify=false`，故不做未经授权的 TLS 安全降级。

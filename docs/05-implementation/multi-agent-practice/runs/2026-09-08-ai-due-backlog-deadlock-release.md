# AI活群历史到期积压死锁解除与大模型资源止血发布

- intake_id: `ai-due-backlog-containment-20260908`
- level: L3
- release_owner / rollback_owner: Antigravity
- 用户授权：用户明确要求修复历史积压与窗口过期导致的 AI 活群无法正常执行问题，并监督线上任务正常执行。
- 范围：
  1. `backend/app/services/task_center/direct_action_claims.py`：解耦 Dispatcher 认领与 message_text 生成依赖，截止用尽动作置顶优先认领并通过 `settle_fact_first_action_before_gateway` 安全结算为 `skipped` + `safely_not_executed` 事实，将 `AccountPacingReservation` 状态标记为 `missed`。
  2. `backend/app/services/task_center/ai_generation_parallel.py`：在候选查询中过滤截止过期动作（`~_deadline_expired_action`），并在 `_claim_one` 中通过 `_is_deadline_expired` 执行防御性就地安全结算，彻底阻止过期动作调用大模型 API。
  3. `backend/scripts/abandon_channel_historical_backlog.py`：支持 `--include-ai-group` 与 `--task-type group_ai_chat`，以 `fact_first_v3` 规范安全下线历史积压动作。
  4. 索引与文档：同步更新 PRD、数据流转索引与结构索引。
- QA 验收：
  - 定向新增测试 `backend/tests/test_due_backlog_containment.py` 4 项全过。
  - 回归测试 `test_abandon_channel_historical_backlog.py`（6 项）、`test_ai_backlog_abandonment.py`（3 项）、`test_pacing_contract_integration.py`（16 项）、`test_fulfillment_fact_first_v3.py`（33 项）、`test_dispatch_claim_reservations.py`（27 项）全部通过。
  - `python -m compileall backend/app backend/scripts backend/tests` 零报错；`git diff --check` 无空白冲突。
- Release Gate：
  - 代码推送到 master，快进 release 分支。
  - 触发 GitHub Actions `Deploy Production` 工作流。
  - 校验容器运行 SHA、健康检查 `/api/health`。
  - 监督早晨 07:00/08:00 首个活跃窗口，验证履约事实生成。
- rollback：本次无数据库 schema 迁移，回滚只需切回上一个 release commit。
- status: ready for commit and release

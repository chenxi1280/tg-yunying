# Worklog: dev

## 2026-07-08 硅谷 recovery CPU 重复 membership reprobe 与 healthcheck CPU 返工

- message_id: 2026-07-08-sv-recovery-cpu-backpressure-dev-rework-dedup-healthcheck-001
- action: 处理 `3a93079d` 发布后 E4 暴露的历史 unknown membership 积压放大 Telegram probe，以及生产 worker healthcheck 自身启动 Python / 查询 DB 造成 CPU 热点的问题。
- input: `3a93079d` 已部署后，recovery 过期 executing 清零且 CPU 低位，但 3 分钟内仍有大量 `Server closed` 日志，DB 中 running membership `unknown_after_send` 仍有近千条未标记记录；进程榜显示 `python -m app.worker_health --role dispatcher/planner` 和 `python -m tg_v_chat.healthcheck` 曾占据 CPU 第一梯队。
- output: 同账号 + 同目标的 membership reprobe 结果会传播到重复 unknown 行，避免逐条重复 probe；worker 主循环写本地 heartbeat 文件，生产 Docker healthcheck 改为 shell 读取本地 heartbeat，不再启动 `python -m app.worker_health` / 查询 DB。
- evidence: RED：`test_recovery_marks_duplicate_identity_probe_rows_failed` 先失败，证明旧逻辑会留下同 identity 重复行；修复后定向 recovery/lifecycle/gateway `19 passed`，全量 no_postgres `805 passed`。healthcheck 修复新增 `test_worker_writes_local_healthcheck_heartbeat` 和 `test_server_compose_worker_healthcheck_uses_local_heartbeat`；修复 CI 路径后 `backend/tests/test_worker_roles.py` `16 passed`，全量 no_postgres `807 passed, 781 deselected, 5 warnings`，`compileall` passed，`git diff --check` passed。
- decision: dev 返工和 Release Gate 修复完成；`2ce949f4`、`a0f0605a`、`f4c66fc5` 均已推送 `master` / `release`，最终生产 E4 交由 prod-diagnosis 记录。
- next_agent: prod-diagnosis
- unresolved: 无 dev 阶段阻断项。

## 2026-07-08 硅谷 recovery CPU stale failed result 覆盖返工

- message_id: 2026-07-08-sv-recovery-cpu-backpressure-dev-rework-stale-failed-result-001
- action: 处理 `53b83635` 发布后生产 E4 暴露的 stale executing failed result 被覆盖问题。
- input: 五次发布后 recovery 使用 `53b83635` 且 healthy，公网 `/api/health` 正常，`worker drain failed=0`、`Task was destroyed=0`、stale membership executing=0；但 recovery 5 分钟 `Server closed=649`，DB `unknown_membership_reprobe_status=''` 约 863、`failed_reprobe=0`。
- output: stale executing + gateway_started membership probe 得到 `OperationResult(False)` 后，保留 `unknown_membership_reprobe_status=failed` 和错误详情，清空 lease 并退出 `executing`，不再被 `_mark_stale_executing_action` 覆盖回普通 `unknown_after_send`。
- evidence: RED：`test_stale_executing_membership_failed_probe_clears_lease_and_stops_reprobe` 先失败，证明旧逻辑第二轮仍会 probe 同一 stale action；修复后定向 recovery/lifecycle/gateway `18 passed`，全量 no_postgres `804 passed, 781 deselected, 5 warnings`，`compileall` passed，`git diff --check` passed。
- decision: 五次发布 E4 failed；stale failed result 覆盖返工本地门禁通过，待重新发布和最终 E4。
- next_agent: qa
- unresolved: release deploy、生产日志/CPU/DB 验证 pending。

## 2026-07-08 硅谷 recovery CPU failed probe client 返工

- message_id: 2026-07-08-sv-recovery-cpu-backpressure-dev-rework-probe-client-001
- action: 处理 `f8d8f703` 发布后生产 E4 暴露的 failed probe Telethon client 持续重连。
- input: 四次发布后 backend/recovery 已使用 `f8d8f703` 且 healthy，公网 `/api/health` 正常，`worker drain failed=0`、stale membership executing=0；但 recovery 10 分钟内仍有 `Server closed=471`，日志约每 0.45 秒一次，DB `unknown_membership_reprobe_status=''` 约 850 且 `failed_reprobe=0`。
- output: `probe_target_capabilities` 返回失败结果后会 invalidate 对应 Telethon cached client；recovery 对 `OperationResult(False)` 显式写入 `unknown_membership_reprobe_status=failed`、`error_code`、`error_message` 和 `unknown_membership_reprobe_error`，下一轮查询不再重复 probe 同一 action。
- evidence: RED：`test_probe_failure_invalidates_cached_client` 先失败，证明旧 gateway 不释放失败 client；`test_recovery_marks_failed_probe_result_and_skips_next_round` 先失败，证明旧 failed 落库缺少可见错误详情。修复后定向 recovery/lifecycle/gateway `17 passed`，全量 no_postgres `803 passed, 781 deselected, 5 warnings`，`compileall` passed，`git diff --check` passed。
- decision: 四次发布 E4 failed；failed probe client 返工本地门禁通过，待重新发布和最终 E4。
- next_agent: qa
- unresolved: Deploy Production 和生产 E4 pending。

## 2026-07-08 硅谷 recovery CPU 背压修复 Release Gate

- message_id: 2026-07-08-sv-recovery-cpu-backpressure-dev-release-gate-001
- action: 作为 dev / release gate owner 推进硅谷 recovery CPU 背压修复发布门禁。
- input: 2026-07-08-sv-recovery-cpu-backpressure-product-acceptance-001
- output: `release_gate_passed_deployed_pending_e4`
- evidence: 最新 release candidate `889e94635541bf937f4fc259f06435f7397fbc5e` 已推送 `master` / `release`。本地最新 SHA 补跑 `backend/.venv/bin/python -m compileall -q backend/app` passed；`backend/tests/test_task_recovery_backpressure.py` 3 passed；`git diff --check` passed。Deploy Production run `28921986236` success：checks、build-images、deploy 均通过，`Deploy via SSH release script` success；公网 `/api/health` 和 `/task-center` 均 HTTP 200。
- decision: Release Gate passed；已真实投递 prod-diagnosis 做 E4 production verification。不能写 `production_fixed`，等待生产 CPU/load、recovery worker、Telegram reprobe 冷却与 Telethon timeout residue 复核。
- next_agent: prod-diagnosis
- handoff_delivery_status: sent
- handoff_message_id: 2026-07-08-sv-recovery-cpu-backpressure-dev-to-prod-diagnosis-e4-001
- unresolved: E4 production verification pending；线上 CPU 是否下降、`worker drain failed` 是否归零、`telegram_probe_timeout` 冷却字段和 recovery 继续处理其他项仍待 prod-diagnosis 证明。

## 2026-07-08 硅谷 recovery CPU 背压修复连接失败返工

- message_id: 2026-07-08-sv-recovery-cpu-backpressure-dev-rework-connection-001
- action: 处理 `889e9463` 发布后生产 E4 暴露的 `ConnectionError` 分支。
- input: 生产硅谷新镜像落地后，`tgyunying-worker-recovery` 仍有 `worker drain failed=5`，栈为 `_recover_unknown_membership_action -> gateway.probe_target_capabilities -> TelethonClientLifecycle.run -> client.connect`，异常 `ConnectionError: Connection to Telegram failed 5 time(s)`；日志前缀出现 `Could not connect to proxy tgyunying-mihomo-024:7890`。
- output: `probe_target_capabilities` 的 `ConnectionError` 现在写入 `telegram_probe_connection_error`、`unknown_membership_reprobe_status=connection_error`、`unknown_membership_reprobe_next_at` 并进入冷却；stale executing membership 同样退出 `executing` 并清空旧 lease。
- evidence: RED：`test_stale_executing_membership_connection_error_clears_lease_and_cools_down` 先失败，证明旧代码会让 `ConnectionError` 冒泡打断 recovery；修复后 `backend/tests/test_task_recovery_backpressure.py` `4 passed`，联合 Telethon lifecycle `13 passed`，全量 no_postgres `799 passed, 781 deselected, 5 warnings`，`compileall` passed，`git diff --check` passed。
- decision: `889e9463` 首次生产 E4 failed；连接失败返工本地门禁已通过，待重新发布。
- next_agent: qa
- unresolved: 重新推送 release、Deploy Production 和二次 E4 均 pending。

## 2026-07-08 硅谷 recovery CPU Telethon connect failure lifecycle 返工

- message_id: 2026-07-08-sv-recovery-cpu-backpressure-dev-rework-lifecycle-connect-001
- action: 处理 `f96dfa4e` 发布后生产 E4 暴露的 Telethon connect failure 后台任务残留。
- input: 二次发布后 `worker drain failed=0`，但 `tgyunying-worker-recovery` CPU 约 97%，5 分钟内 `Task was destroyed but it is pending=796`、`Server closed=784`，missing proxy `tgyunying-mihomo-024` 仍出现；DB stale executing 已清成 `unknown_after_send`，说明 recovery 状态机不是剩余 CPU 根因。
- output: `TelethonClientLifecycle.get_or_create_client` 在新建 client 的 `connect()` 失败时立即 `_disconnect_quietly(client)`，避免失败 client 的 auto-reconnect / send / recv coroutine 留在后台。
- evidence: RED：`test_telethon_lifecycle_disconnects_new_client_after_connect_failure` 先失败，证明旧实现 `disconnect_count=0`；修复后 `backend/tests/test_telethon_lifecycle.py` `10 passed`，`backend/tests/test_task_recovery_backpressure.py` `4 passed`，全量 no_postgres `800 passed, 781 deselected, 5 warnings`，`compileall` passed，`git diff --check` passed。
- production_stopgap: 2026-07-08 14:55:58 CST 已停止线上 `tgyunying-worker-recovery` 临时止血，未写数据库；发布新 lifecycle 修复后由 deploy 重新拉起。
- decision: `f96dfa4e` 二次 E4 failed；需要第三次发布 lifecycle cleanup 修复。
- next_agent: qa
- unresolved: 重新发布和三次 E4 pending。

## 2026-07-08 硅谷 recovery CPU unknown membership batch 饥饿返工

- message_id: 2026-07-08-sv-recovery-cpu-backpressure-dev-rework-reprobe-query-001
- action: 修复 unknown membership 补偿复检 batch 被已 failed / cooldown 旧行占满导致待处理行饥饿的问题。
- input: `3d378414` 发布后 recovery 已无 `worker drain failed`、pending task 和 missing proxy 024，但仍有周期性 CPU 峰值；生产 DB 显示 running `unknown_after_send` membership 中有大量 unset 行，同时已 failed / cooldown 行仍参与 batch 查询，容易占满 `limit=10`。
- output: `_recover_existing_unknown_membership_actions` 查询 batch 时增加 `_unknown_membership_reprobe_due_clause(now)`，SQL 层排除 `unknown_membership_reprobe_status=failed` 和 cooldown 未到期的 `timeout/connection_error` 行，避免旧行饥饿真正待处理项。
- evidence: RED：`test_recovery_skips_failed_reprobe_rows_when_selecting_batch` 先失败，证明旧代码 calls 为空；修复后 `backend/tests/test_task_recovery_backpressure.py` `5 passed`，`backend/tests/test_telethon_lifecycle.py` `10 passed`，全量 no_postgres `801 passed, 781 deselected, 5 warnings`，`compileall` passed，`git diff --check` passed。
- decision: batch 饥饿返工本地通过，待重新发布并做最终 E4。
- next_agent: qa
- unresolved: 第四次发布和最终生产 E4 pending。

## 2026-07-08 硅谷 recovery CPU 持续升高修复 Development Complete

- message_id: 2026-07-08-sv-recovery-cpu-backpressure-devcomplete-001
- action: 修复硅谷生产 `tgyunying-worker-recovery` 对历史 `unknown_after_send` membership 无上限补偿复检、Telegram 探测超时打断整轮 drain 并遗留 Telethon 后台协程的问题。
- input: 生产直连 `47.251.126.134` 发现 4 核服务器 load 长期 8+；`tgyunying-worker-recovery` 单次 CPU 到 148.69%；10 分钟内 `worker drain failed` 18 次；数据库 `unknown_after_send=1043`、running unknown membership=126、stale executing=27。
- output: `drain_task_recovery(limit=...)` 传递真实 recovery limit；stale executing 查询加批量上限；existing unknown membership 复检按 drain limit 和账号+目标去重，单轮 TG reprobe 上限为 10；Telegram reprobe `TimeoutError` 写入 `telegram_probe_timeout`、`unknown_membership_reprobe_status=timeout`、`unknown_membership_reprobe_next_at`，不再抛出打断整轮 recovery；Telethon lifecycle 在 operation timeout 后 `future.cancel()`，避免超时 coroutine 留在后台继续重连。
- evidence: RED：`backend/tests/test_task_recovery_backpressure.py` 先失败，证明旧代码单轮复检 4 条且 TimeoutError 会抛出；`backend/tests/test_telethon_lifecycle.py::test_telethon_lifecycle_cancels_coroutine_after_operation_timeout` 先失败，证明旧 lifecycle 不取消超时 coroutine。QA 返工红测 `test_stale_executing_membership_timeout_clears_lease_and_cools_down` 先失败，证明 stale executing membership timeout 后仍保留 `executing` 和旧 lease；修复后目标背压测试 `3 passed`，联合 Telethon lifecycle `12 passed`，worker/recovery 相关 `17 passed, 10 deselected`，全量 no_postgres 60 秒门禁 `798 passed, 781 deselected, 5 warnings`；`backend/.venv/bin/python -m compileall -q backend/app` passed；`git diff --check` passed。
- production_stopgap: 2026-07-08 14:04:52 CST 已通过 SSH 仅停止旧 `tgyunying-worker-recovery` 容器，未改数据库；停止后 load 样本为 `7.16, 7.83, 8.24`，仍需发布新代码并复核。
- decision: status=local_verified_pending_release；release_gate=pending；production_fixed=unproven。
- next_agent: qa
- unresolved: 首次 push 触发的 Deploy Production run `28921622212` 已因 QA 返工取消；返工提交尚未重新推送 release；生产新镜像、recovery 重启后 `worker drain failed` 归零和 CPU 降载仍需 E4 复核。

## 2026-07-06 AI 活群 hard-hourly 分布护栏补齐 Development Complete（本地验证）

- message_id: 2026-07-06-ai-group-hard-hourly-distribution-guard-002
- action: 在已发布账号轮转修复基础上补齐旧偏斜 open action 重规划、账号在线样本可见性和新批次分布门禁。
- input: 用户追问“核心出现的问题是什么 / 需要重新优化吗 / 你来补齐”，要求线上检查测试直接 SSH、不用 actions。
- output: `group_ai_chat.prepare_open_actions_for_planning` 会在重规划前跳过 hard-hourly 偏斜旧 open action 并写 `hard_hourly_distribution_skew_replan`；`build_plan` 在写 Action 前阻断偏斜新批次并写 `account_distribution_skew`；在线筛选 stats 增加 selected/ready/offline/sample account ids。
- evidence: 新增 red/green 回归 `test_group_ai_chat_hard_hourly_skips_skewed_open_actions_for_replan`、`test_group_ai_chat_hard_hourly_records_offline_account_samples`、`test_group_ai_chat_hard_hourly_blocks_skewed_new_plan`；专项 PRD、数据流索引、结构索引已同步。
- decision: status=local_verified_pending_release；release_gate=pending；production_verification=unproven。
- next_agent: qa
- unresolved: 尚未推送 release、未 SSH 直连生产复核新分布护栏。

## 2026-07-06 AI 活群 hard-hourly 账号轮转修复 Development Complete（本地验证）

- message_id: 2026-07-06-ai-group-hard-hourly-account-rotation-001
- action: 修复 hard-hourly 追量时历史账号记忆把刚发过的账号重新推到下一轮第一位，导致线上表现为少数账号甚至单账号集中发言的问题。
- input: 用户反馈“线上 ai 活群好像都是一个账号在发送消息”；生产诊断先卡在 `AI_GROUP_QUALITY_ONLINE_GATE_FAILED`，但代码路径和子代理只读审查均确认 planner 存在账号分布过度集中机制。
- output: `group_ai_chat._hard_hourly_round_config` 为追量轮次打显式 `hard_hourly_planning` 标记；`_prioritize_accounts_for_plan` 在 hard-hourly 非全账号日覆盖场景保留 `_rotate_accounts` 轮转顺序，不再让 `account_memories` 把刚有历史发言的账号排回第一位；保留 `allow_account_repeat=True`，当本轮 turn 数超过可用账号数或连发配置要求时仍可复用账号。
- evidence: 新增 red/green 回归 `test_group_ai_chat_hard_hourly_preserves_cycle_rotation_over_account_memory`，旧实现下账号序列为 `[101, 102, 103]`，修复后为 `[102, 103, 101]`；定向新增测试 1 passed，相邻 slot 纯函数测试 2 passed；专项 PRD、数据流索引和结构索引已同步。
- decision: status=local_verified_pending_broader_tests；release_gate=pending；production_verification=unproven。
- next_agent: qa
- unresolved: 尚未完成全量本地验证、release 发布和生产 AI 活群质量诊断；线上账号分布改善需发布后用真实生产 action 分布和诊断确认。

## 2026-07-02 托管 2FA 密码受控查看 Development Complete

- message_id: 2026-07-02-managed-2fa-reveal-devcomplete-001
- action: 按 PRD 实现账号详情托管 2FA 密码受控查看和复制
- input: 2026-07-02-managed-2fa-reveal-product-001
- output: 新增 `managed-2fa/reveal` 后端接口，具备权限校验、密文解密返回和审计，不采集查看原因；前端“托管 2FA”面板新增查看托管密码、短时展示和复制按钮
- evidence: `backend/.venv/bin/python -m pytest -q -m no_postgres backend/tests/test_account_managed_2fa_dataflow.py backend/tests/test_account_security.py::test_managed_two_fa_reveal_returns_decrypted_password_and_audits` -> 3 passed；py_compile passed；`npm --prefix frontend run build` passed；`git diff --check` passed
- decision: status=local_verified_pending_release；release_gate=pending；production_verification=unproven
- next_agent: qa
- unresolved: 未真实投递 QA 线程；未发布生产

## 2026-06-27

- message_id: 2026-06-27-docs-practice-devcomplete-001
- action: 建立四 Agent 协作材料
- input: 2026-06-27-docs-practice-plan-001
- output: 新增登记表、模板、四个 worklog、演练记录，并更新实施目录 README
- evidence: `docs/05-implementation/multi-agent-practice/`
- decision: status=ready_for_validation
- next_agent: qa
- unresolved: 真实开发线程已返回 Development Complete；QA 线程正在验收

## 2026-06-27 index responsibility supplement

- message_id: 2026-06-27-index-maintenance-dev-001
- action: 补充开发 Agent 的代码架构和项目逻辑结构索引责任
- input: 执行 / 开发 Agent 需要生成并维护项目结构索引，方便后续修改
- output: 新增 `index-maintenance.md`，并将 `project-structure-index.md` 纳入 dev 工作区
- evidence: `docs/00-index/project-structure-index.md`
- decision: dev 交接给 qa 前必须说明代码结构索引是否更新；涉及 API/worker/页面流转时同步说明数据流转索引
- next_agent: qa
- unresolved: 本次只补协作规则，不重建全量项目结构索引

## 2026-06-28 AI 活群话题老师连发配置 Development Complete

- message_id: 2026-06-28-ai-group-topic-teacher-burst-devcomplete-001
- action: 接管 `codex/ai-group-topic-teacher-burst` 草稿并完成 dev 复核
- input: 2026-06-28-ai-group-topic-teacher-burst-product-001
- output: 新增话题方向、聊天对象老师、同账号 2-4 条连发、Web 设置展示和 TG bot 管理员设置入口
- evidence: `backend/.venv/bin/python -m pytest -q -m no_postgres backend/tests/test_ai_group_hard_hourly_target.py backend/tests/test_task_center_config_normalization.py backend/tests/test_task_center_capacity_dispatch.py backend/tests/test_telegram_bot_group_ai_settings.py` -> 13 passed, 97 deselected；`npm run build` -> built；`git diff --check` -> clean
- decision: status=ready_for_qa；Release Gate 仍需 CI / release deploy
- next_agent: qa
- unresolved: 未访问生产环境；E3/E4 unproven

## 2026-06-28 hard-hourly min 10 Release Gate Ready

- message_id: 2026-06-28-hard-hourly-min-10-devcomplete-001
- action: 将 AI 活群每小时硬目标默认/最低值从 60 调整为 10，并补历史配置迁移
- input: 2026-06-28-hard-hourly-min-10-001
- output: schema、前端常量、PRD、ops 文档和 Alembic 数据迁移同步到 10
- evidence: 同本轮定向测试和前端 build
- decision: status=ready_for_release_gate
- next_agent: qa
- unresolved: CI/deploy evidence not yet recorded

## 2026-07-03 搜索自动入群 Development Complete（本地验证）

- message_id: 2026-07-03-search-join-group-devcomplete-001
- action: 按 PRD 新功能设计实现任务中心第 6 类 `search_join_group` 的首版代码闭环
- input: 2026-07-02-search-join-group-prd-merge-001
- output: 后端新增创建/启动/配置接口、schema、配置字段、ORM 模型、Alembic 迁移、`search_join` payload、planner、小时执行统计、dispatcher fail-closed 分支和联动投递记录服务；前端新增任务类型、创建端点、向导字段、payload 构造、规则中心类型、任务详情“搜索入群统计”Tab 和快速分组识别。
- evidence: `backend/.venv/bin/pytest -q -m no_postgres` 覆盖 search_join 定向、task-center 相关回归和 frontend gating -> 212 passed / 79 deselected；`backend/.venv/bin/python -m compileall app` passed；`backend/.venv/bin/python -m py_compile backend/migrations/versions/0075_search_join_group.py` passed；`npm --prefix frontend run build` passed；`git diff --check` passed。
- decision: status=local_verified_pending_release；ready_for_validation=local；release_gate=pending；production_verification=unproven。
- next_agent: qa
- unresolved: 未真实投递 QA 长期线程；未执行 GitHub Actions release / production deploy；真实 MTProto gateway 执行器仍 fail-closed，需要协议样本和 gateway 接入后才能生产灰度。

## 2026-07-03 搜索自动入群监督补缺 Development Complete（本地验证）

- message_id: 2026-07-03-search-join-group-supervised-fix-devcomplete-001
- action: 按只读监督子代理发现的缺口补齐 `search_join_group` 可执行边界
- input: 子代理指出关键词空列表会导致 planner 取模崩溃、协议样本闸门依赖不可通过的 config 魔法字段、真实 gateway 缺 proxy egress guard、成功入群后未自动写 linked dispatch、后端专项权限缺失。
- output: schema 强制 keywords / keyword_hashes 至少一个且 hash 为 64 位小写 hex；新增 `bot_protocol_samples` 模型与迁移，planner 改查活跃且已脱敏的真实协议样本；search_join action 默认 `proxy_egress_guard=missing`，dispatcher 在真实 gateway 前要求 `verified`，缺失时失败且不调用 gateway；membership_observed 成功后按 `linked_task_policy` 写 `SearchJoinLinkedTaskDispatch`；新增 `tasks.create.search_join_group` 后端权限规则和运营管理员模板权限。
- evidence: `backend/.venv/bin/python - <<'PY' ... pytest -q -m no_postgres ... PY` -> 653 passed / 798 deselected；`backend/.venv/bin/python -m compileall backend/app` passed；`backend/.venv/bin/python -m py_compile backend/migrations/versions/0075_search_join_group.py` passed；`npm --prefix frontend run build` passed；`git diff --check` passed。
- decision: status=local_verified_pending_release；release_gate=pending；production_verification=unproven。
- next_agent: qa
- unresolved: 机场订阅/节点容量/failover/出口 IP 观测/授权槽位环境栈/warmup/执行锁等基础设施仍仅 PRD 化或 fail-closed，未达到真实灰度可运行；发布后只能证明代码上线和 fail-closed 边界，不能宣称 7 天搜索入群灰度完成。

## 2026-07-03 搜索自动入群 Release Gate Complete

- message_id: 2026-07-03-search-join-group-release-gate-dev-001
- action: 将监督补缺后的 `search_join_group` 代码合入生产发布路径并完成 Release Gate 记录
- input: 2026-07-03-search-join-group-supervised-fix-local-qa-001
- output: `master` 和 `release` 已推进到 `32b0257b1694f5dd8b5ea73cc159bb8e670d300a`；Deploy Production run `28644819954` 通过 checks、build-images、deploy；生产 release `20260703071946_32b0257` live。
- evidence: GitHub Actions run `28644819954`；公网 `https://tgyunying.telema.cn/api/health` HTTP 200 `{"status":"ok"}`；公网 `/task-center` HTTP 200；运行记录 `runs/2026-07-03-search-join-group-supervised-release.md`。
- decision: release_gate=passed；production_health=ok；handoff_delivery_status=sent。
- next_agent: product
- unresolved: product acceptance 未确认；真实目标机器人协议样本、真实代理出口、机场节点容灾、授权槽位环境栈和 7 天灰度仍 unproven，当前实现保持 fail-closed。

## 2026-07-03 接码专用账号只接码限制 Development Complete（本地验证）

- message_id: 2026-07-03-code-receiver-restriction-devcomplete-001
- action: 将接码专用分组账号限制为只用于接码、授权资产诊断和备用 session 补齐 / 自愈
- input: 用户确认“接码专用分组只需用于接码；不改名字、不改 2FA 密码、不参与任务；接码账号允许备用 session 补齐 / 自愈”
- output: PRD、账号安全专项、数据流转索引和项目结构索引同步；登录后自动资料初始化排除 `code_receiver`，不创建资料批次、不初始化账号面具；账号安全预检和 worker 对资料、username、头像、设置 2FA、设备清理动作硬跳过；消息发送公共入口和旧私聊入口阻断接码账号；备用 session 补齐 / 自愈未纳入禁用集合。
- evidence: 先新增 red tests 并确认失败；实现后 `python -m pytest -q backend/tests/test_account_profile_auto_initialization.py backend/tests/test_task_account_pool.py backend/tests/test_account_center_prd_contracts.py -m no_postgres` -> 57 passed；py_compile changed backend files passed；`git diff --check` passed。
- decision: status=local_verified_pending_release；release_gate=pending；production_verification=unproven。
- next_agent: qa
- unresolved: 未真实投递 QA 长期线程；未执行 GitHub Actions release / production deploy。

## 2026-07-04 账号面具环境与全局 Clash 配置 Development Complete（本地验证）

- message_id: 2026-07-04-account-mask-environment-devcomplete-001
- action: 按 PRD 补齐账号面具一级菜单、系统配置全局 Clash 订阅、账号 + TG 开发者应用 + 授权槽位代理/指纹绑定和 search_join 授权槽位运行时使用。
- input: 2026-07-04-account-mask-environment-prd-001；用户确认“账号面具”一级菜单、系统配置只放一个全局 Clash 订阅、单账号代理/指纹按 TG 开发者应用 api_id/api_hash 和授权槽位绑定，指纹修改只影响下一次连接 / 重登 / 新 session 初始化。
- output: 新增 `proxy_airport_subscriptions/proxy_airport_nodes` ORM 与迁移；新增账号环境 schema/service/API；系统配置新增 Clash 配置 Tab；账号面具新增账号代理、授权指纹、异常与审计 Tab；权限新增 `account_masks.view`、`account_environment.manage`，运营管理员模板同步；search_join planner payload 写入 developer app / authorization / metadata，dispatcher 按 payload 指定授权槽位 session 和开发者应用 credentials 执行，禁止回退账号主 session；账号环境列表从 `tg_account_authorization_snapshots` 投影远端观测指纹并区分 pending / matched / mismatch。
- evidence: 先补 red tests 并确认失败：legacy binding 会重复生成、0078 迁移缺回填、Clash GET 无权限、远端观测字段缺失、dispatcher 使用账号主 session；修复后定向套件 189 passed；最终本地 gate `backend/.venv/bin/pytest -q -m no_postgres` -> 718 passed / 789 deselected；`npm --prefix frontend run build` passed；changed backend py_compile passed；`git diff --check` passed。
- decision: status=local_verified_pending_release；subagent_supervision=done_with_P1_fixed；release_gate=pending；production_verification=unproven。
- next_agent: qa
- unresolved: 未执行 GitHub Actions release / production deploy；真实机场订阅拉取/节点同步和真实远端 Telegram 授权快照刷新仍需生产环境验证。

## 2026-07-04 授权槽位代理事实源修正 Development Complete（本地验证）

- message_id: 2026-07-04-account-proxy-slot-runtime-fix-devcomplete-001
- action: 按最新 PRD 重新梳理账号面具、全局 Clash、授权槽位代理/指纹和 search_join 执行链路，并修复子代理监督指出的运行时缺口。
- input: 用户要求完整梳理并写入 PRD；监督子代理指出 search_join 仍可能使用旧授权代理、`account_proxy_bindings` 缺槽位唯一约束、代理重绑会撞唯一索引、Dispatcher 未校验代理绑定行本身。
- output: PRD 补齐系统设置 Clash 配置入口、授权指纹远端观测边界和区域一致性矩阵口径；`account_proxy_bindings` 增加授权槽位字段与 active 槽位唯一索引；账号环境保存同槽位换代理时关闭旧 active 绑定；search_join planner 已写入 `environment_binding_id/proxy_binding_id/proxy_id`，Dispatcher 强制回查 `account_environment_bindings` 和 `account_proxy_bindings`，代理绑定失效、错槽位或漂移时 fail closed，不回退本机直连或旧授权代理。
- evidence: 子代理 `019f2c2f-21bd-79a1-8e1c-a73810e9e45b` 只读复核发现 2 个 blocker，已补测试并修复；定向 `backend/.venv/bin/python -m pytest backend/tests/test_search_join_group_linked_tasks.py backend/tests/test_search_join_group_executor.py backend/tests/test_account_environment_bindings.py backend/tests/test_merge_integrity.py -q` -> 39 passed；全量 no_postgres 60s gate -> 728 passed / 787 deselected；`npm --prefix frontend run build` passed；changed backend compile/py_compile passed；`git diff --check` passed；旧口径扫描无命中。
- decision: status=local_verified_pending_release；subagent_supervision=done_with_blockers_fixed；release_gate=pending；production_verification=unproven。
- next_agent: qa
- unresolved: 未执行 GitHub Actions release / production deploy；真实生产 Clash 订阅同步、节点出口观测、远端 Telegram 授权快照刷新和郑州 3 账号真实加入测试仍需生产验证。

## 2026-07-06 Clash 新增订阅优先级冲突 UX quick_fix（本地验证）

- message_id: 2026-07-06-clash-add-priority-ux-devcomplete-001
- action: 修复系统设置 Clash 订阅源池新增时默认优先级撞到已有启用订阅后只显示错误码的问题。
- input: 线上截图显示新增 `admin2` 时页面提示 `proxy_airport_subscription_priority_conflict`；现有启用订阅包含 priority=10，新表单默认 priority 也为 10。
- output: 前端新增 `nextAvailablePriority`，按已启用订阅自动选择下一个可用优先级；保留后端“启用订阅优先级不能重复”的 PRD 规则；将 `proxy_airport_subscription_priority_conflict` 翻译成中文可操作提示。
- evidence: 先新增前端合同 red test 并确认失败；修复后 `backend/.venv/bin/pytest backend/tests/test_account_mask_frontend_contracts.py -q` -> 5 passed；`backend/.venv/bin/pytest backend/tests/test_proxy_airport_subscription.py -q` -> 15 passed；`npm --prefix frontend run build` passed。
- decision: status=local_verified_pending_release；release_gate=pending；production_verification=unproven。
- next_agent: qa
- unresolved: 尚未推送 release / Deploy Production；线上页面是否加载新静态包需发布后复核。

## 2026-07-04 搜索目标群点击任务小时容量热修 Development Complete（本地验证）

- message_id: 2026-07-04-search-join-hourly-cap-null-hotfix-devcomplete-001
- action: 修复线上郑州 3 账号搜索目标群点击任务创建后一直 0 actions 的容量合并缺口。
- input: 生产浏览器 API 证据显示任务 `62cbbb12-dccd-4208-b5fe-820a7ffa98d7` 的 `type_config.max_actions_per_hour=3` 已落库，但 `stats.search_join_stats.hourly_execution.max_actions_per_hour=0`、`capacity=0`，原因是 `pacing_config.max_actions_per_hour=null` 覆盖了业务小时上限。
- output: 新增 `runtime_search_join_config`，`search_join_group` planner 和小时统计共用运行时配置合并；`pacing_config` 中的 `None` 不再覆盖 `type_config` 的有效值，显式 `0` 仍保留为关闭容量；schema 和前端只对 search_join 允许该 0 语义；PRD、专项设计和项目结构索引同步该边界。
- evidence: 子代理监督指出 API/schema 路径仍不允许 0，已补 `SearchJoinPacingConfig`、前端任务类型 min 值和专项 PRD；`backend/.venv/bin/python -m pytest backend/tests/test_search_join_group_config.py backend/tests/test_search_join_group_executor.py backend/tests/test_search_join_group_runtime_config.py backend/tests/test_frontend_permission_gating.py::test_search_join_group_frontend_exposes_pacing_controls_and_details backend/tests/test_frontend_permission_gating.py::test_task_center_runtime_form_exposes_hour_limit_without_generic_task_daily_cap -q` -> 34 passed；`backend/.venv/bin/python -m compileall -q backend/app` passed；`npm --prefix frontend run build` passed；`git diff --check` passed。
- decision: status=local_verified_pending_release；release_gate=pending；production_verification=unproven。
- next_agent: qa
- unresolved: 尚未推送 release/master；尚未执行 Deploy Production；生产任务仍需部署后重新触发或等待 planner 验证 actions 生成与真实加入结果。

## 2026-07-05 Clash 多订阅源池 Development Complete（本地验证）

- message_id: 2026-07-05-clash-multi-subscription-devcomplete-001
- action: 按 PRD v0.20 实现系统配置 Clash 多订阅源池、主备优先级、订阅级同步和 search_join 全部启用订阅不可用 fail-closed。
- input: 2026-07-05-clash-multi-subscription-prd-001；用户确认 Clash 支持配置多个地址，主地址不可用时使用备用地址。
- output: `ProxyAirportSubscription` 增加 name / priority / enabled / failover policy 等字段，`ProxyAirportNode` 增加出口观测字段，新增 `ProxyNodeFailoverEvent` 和迁移 `0081_proxy_airport_multi_subscription.py`；新增 `proxy_airport_pool.py` 实现 list/create/patch/sync-by-id/failover 选择，保留旧 `/api/proxy-airport-subscription` 主订阅兼容入口；系统路由新增 `/api/proxy-airport-subscriptions` 集合 API；前端系统配置 Clash 页面改为多订阅列表，支持新增、编辑已有订阅地址 / 名称 / 优先级、启用状态、逐条同步和状态展示；search_join planner 在已有启用订阅但全部无健康源时写 `airport_all_subscriptions_unavailable` 并不生成真实 action；同时修复任务详情 stats 刷新不会重算 search_join 小时容量的问题。
- evidence: 先补 red tests 并确认失败：多订阅 schema/service 缺失、plural route/permission 缺失、前端仍是单订阅、search_join 全订阅不可用仍生成 action、详情 stats 仍显示 `max_actions_per_hour=0`；修复后定向 `backend/.venv/bin/python -m pytest backend/tests/test_proxy_airport_subscription.py backend/tests/test_search_join_group_executor.py backend/tests/test_search_join_group_runtime_config.py backend/tests/test_account_mask_frontend_contracts.py backend/tests/test_permission_vocabulary.py backend/tests/test_system_actions_dataflow.py backend/tests/test_merge_integrity.py -q` -> 68 passed；Hegel 只读监督发现 plural sync 未跑健康探测、全订阅不可用未发管理员通知两个 P0，已补 `health_checker=check_proxy_airport_node` 和租户 Bot 通知并复测；用户补充“多个地址、主掉备用”后补前端已有订阅编辑入口，合同测试 1 passed、frontend build passed、`git diff --check` passed；最终全量 no_postgres 60s gate -> 757 passed / 787 deselected；changed backend py_compile passed；代码行数检查全部低于 500 行。
- decision: status=local_verified_pending_release；subagent_supervision=done_with_P0_fixed；release_gate=pending；production_verification=unproven。
- next_agent: qa
- unresolved: 真实出口 IP 观测、健康节点自动固定到授权槽位、运行中同订阅/备用订阅自动切换、`proxy_node_failover_events` 写入和 warmup 重置仍需后续实现或生产证据；尚未执行 GitHub Actions release / production deploy。

## 2026-07-05 Clash 运行中 failover 绑定 Development Complete（本地验证）

- message_id: 2026-07-05-clash-failover-runtime-devcomplete-001
- action: 补齐 Clash 多订阅源池在 search_join 运行中代理失败后的授权槽位绑定切换、事件记录、出口观测和 warmup 重置。
- input: 用户要求 Clash 支持多个地址，主地址掉了使用备用地址；监督子代理指出 `proxy_exit_ip_observations`、`account_proxy_warmup_states`、当前绑定节点级 failover 和 Dispatcher 代理失败切换仍缺代码证明。
- output: 新增 `proxy_airport_failover.py`，在当前授权槽位 active airport binding 上优先同订阅切健康节点，同订阅无候选时按订阅 priority 切备用订阅健康节点；旧 `AccountProxyBinding` 置 inactive，新 binding 继承账号 / 开发者应用 / 授权槽位并写 `proxy_airport_node_id`、已有 observed exit IP、`binding_generation` 和 `last_failover_at`；写 `ProxyNodeFailoverEvent`、有出口观测时写 `ProxyExitIpObservation`、始终写 `AccountProxyWarmupState`，并把 active `AccountEnvironmentBinding.proxy_binding_id` 指向新绑定。Dispatcher 在 search_join 代理类失败后调用 failover，action result 写 `proxy_failover_status` / `proxy_failover_event_id`；切换成功后刷新同任务同账号 pending search_join payload 的 `proxy_binding_id`；无候选时写管理员通知状态；代理 protocol / host / port 缺失时在 gateway 前 fail closed。
- evidence: 先补 red tests 并确认失败：缺 `AccountProxyWarmupState` / 运行中代理失败不会生成新 binding / environment binding 不会重指向 / 代理 host 为空仍会调用 gateway；Copernicus 只读复核指出 3 个 blocker：候选节点错误依赖 observed exit IP、pending action 仍携带旧 binding、运行中全订阅不可用不通知管理员。已补 red tests 并修复，定向 `backend/.venv/bin/python -m pytest backend/tests/test_proxy_airport_subscription.py backend/tests/test_proxy_airport_failover.py backend/tests/test_search_join_group_linked_tasks.py backend/tests/test_merge_integrity.py -q` -> 30 passed。
- decision: status=local_verified_pending_release；subagent_supervision=done_with_P0_P1_fixed；release_gate=pending；production_verification=unproven。
- next_agent: qa
- unresolved: 尚未执行全量 no_postgres、frontend build、GitHub Actions release / production deploy；真实生产 Clash 订阅拉取、真实出口 IP、真实运行中 failover 和郑州 3 账号线上任务复测仍需生产证据。

## 2026-07-05 Clash 多订阅主备一致性 Development Complete（本地验证）

- message_id: 2026-07-05-clash-multi-subscription-consistency-devcomplete-001
- action: 按 PRD v0.21 修复 Clash 多订阅主备一致性缺口，并补齐线上配置脚本的机场订阅 / 节点 / 授权槽位绑定事实源
- input: 用户确认 Clash 支持多个订阅地址，主地址掉了使用备用；本轮审计发现 schema/model 默认 `failover_policy=priority`、前端不可配置 failover/自动切回字段、运行中 failover 新 binding 复用旧 `proxy_id`、线上配置脚本未写 `ProxyAirportSubscription/ProxyAirportNode/proxy_airport_node_id`
- output: `ProxyAirportSubscription` schema/model/migration 默认改为 `same_subscription_first`、默认不自动切回且冷却 1440 分钟，新增迁移 `0083_proxy_airport_failover_policy_defaults.py` 归一已部署旧值并关闭旧自动切回；`ProxyAirportSubscriptionView.tsx` 支持创建/编辑切换策略并展示切回冷却，首版自动切回开关禁用，后端拒绝 `auto_failback_enabled=true`；`proxy_airport_failover.py` 基于目标机场节点创建 / 复用 `AccountProxy`，新 `AccountProxyBinding.proxy_id` 指向目标节点代理资源，并同步 `AccountEnvironmentBinding.proxy_binding_id/proxy_id`；dispatcher retarget pending search_join action 时同步 `runtime_environment.proxy_binding_id/proxy_id`；`.github/scripts/configure_clash_search_join_live.py` 在 apply_db 阶段写入订阅源、机场节点池，并为环境绑定创建 active scoped `AccountProxyBinding` 后写入 `proxy_airport_node_id`
- evidence: 先补 red tests 并确认失败：默认策略仍为 `priority`、前端缺 failover 字段、live 脚本未写机场订阅/节点、failover 新 binding 仍复用旧 proxy；Mendel 监督指出自动切回可开启但无运行时实现、出口观测前置口径矛盾、0083 未跟踪；Chandrasekhar 监督指出 pending payload 未同步 `proxy_id`、live apply_db 未指向 active scoped binding。均已补测试并修复：相关套件 `backend/.venv/bin/python -m pytest backend/tests/test_proxy_airport_subscription.py backend/tests/test_proxy_airport_failover.py backend/tests/test_live_clash_config_script_contracts.py backend/tests/test_account_mask_frontend_contracts.py backend/tests/test_search_join_group_linked_tasks.py backend/tests/test_merge_integrity.py -q` -> 40 passed；全量 `backend/.venv/bin/python -m pytest -q -m no_postgres` -> 768 passed / 787 deselected；`npm --prefix frontend run build`、changed py_compile、`git diff --check` 均通过
- decision: status=local_verified_pending_release；subagent_supervision=done_with_blockers_fixed；release_gate=pending；production_verification=unproven
- next_agent: qa
- unresolved: 尚未执行 GitHub Actions release / production deploy；真实生产 Clash 订阅拉取、真实出口 IP、真实运行中 failover 和郑州 3 账号线上任务复测仍需生产证据

## 2026-07-05 Clash live apply client metadata 修复（本地验证）

- message_id: 2026-07-05-clash-live-apply-client-metadata-fix-001
- action: 修复生产直连配置 Clash 时 `apply_db` 在全账号环境绑定阶段因缺少现成 client metadata 失败的问题。
- input: 生产 SSH 直连执行 Clash 配置时 16 个候选节点 egress 全部 OK，DB preflight 通过 `account_count=612`、郑州目标命中，但 `apply_db` 报 `account environment binding failed: needs_client_metadata`。
- output: `client_metadata` 服务新增公开 `ensure_or_create_search_join_environment`，在授权槽位和健康代理存在时生成真实 `AccountEnvironmentBinding` / `AccountProxyBinding` / client identity；live 配置脚本全账号环境绑定阶段改用该入口，仍对缺少授权或健康代理的账号显式失败，不走默认 MTProto 指纹或静默跳过。
- evidence: 新增服务层回归证明缺少现成环境绑定时会创建完整 client metadata；live 脚本合同测试要求使用 ensure-or-create 入口；定向 `backend/.venv/bin/python -m pytest backend/tests/test_account_environment_bindings.py backend/tests/test_live_clash_config_script_contracts.py backend/tests/test_proxy_airport_subscription.py backend/tests/test_search_join_group_linked_tasks.py -q` -> 38 passed；全量 `backend/.venv/bin/python -m pytest -q -m no_postgres` -> 769 passed / 787 deselected；changed py_compile、`git diff --check` 通过。
- decision: status=local_verified_pending_release；release_gate=pending；production_verification=partial_failed_then_fixed_locally。
- next_agent: qa
- unresolved: 修复提交尚未重新发布；生产 DB apply / 郑州 3 账号任务仍需重新 SSH 直连验证。

## 2026-07-05 Clash live apply 旧节点退役修复（本地验证）

- message_id: 2026-07-05-clash-live-apply-retire-absent-nodes-001
- action: 修复生产 live apply 只保留 16 个 mihomo 容器后，DB 仍残留历史 healthy 机场节点的问题。
- input: 第二次生产 SSH apply 成功后，任务 action 使用 001/002/003 节点正常，但 dispatcher 日志仍出现历史动作尝试 `tgyunying-mihomo-056:7890`；生产 DB 总 healthy 机场节点数高于本次上线节点数，说明缺少“本次订阅不存在节点退役”步骤。
- output: `.github/scripts/configure_clash_search_join_live.py` 新增 `retire_absent_airport_nodes`，在每次 apply_db 同步当前订阅节点后，将同订阅下不在本次解析结果里的旧节点置为 `unhealthy`，`last_error=not_present_in_latest_live_apply`，避免 failover 继续选择已下线容器。
- evidence: live 脚本合同测试新增退役函数与错误码断言；定向 `backend/.venv/bin/python -m pytest backend/tests/test_live_clash_config_script_contracts.py backend/tests/test_proxy_airport_failover.py backend/tests/test_proxy_airport_subscription.py -q` -> 21 passed；全量 `backend/.venv/bin/python -m pytest -q -m no_postgres` -> 769 passed / 787 deselected；脚本 py_compile、`git diff --check` 通过。
- decision: status=local_verified_pending_release；release_gate=pending；production_verification=needs_reapply。
- next_agent: qa
- unresolved: 修复提交尚未重新发布；生产需要重新 apply_db 以把历史 017-064 等节点标为 unhealthy。

## 2026-07-05 Clash live apply 搜索关键词修复（本地验证）

- message_id: 2026-07-05-clash-live-apply-search-keyword-fix-001
- action: 修复 live 脚本把目标筛选词和搜索关键词绑死导致郑州线上测试搜索过宽的问题。
- input: 最新生产 3 账号任务真实执行后，2 个账号 `target_not_in_results`，只返回 2 页 / 53 条结果且目标群 `xiaozisk` 未出现；1 个账号遇到搜索机器人人机验证。生产库显示目标群 title=`郑州平价资源（交流群）`、username=`xiaozisk`，需要支持用更精确关键词验收。
- output: `.github/scripts/configure_clash_search_join_live.py` 新增 `CLASH_SEARCH_KEYWORD` / `search_keyword()`；目标群仍按 `CLASH_TARGET_QUERY` 选择，任务搜索关键词可单独设置为 username 或精确标题。
- evidence: live 脚本合同测试新增 `CLASH_SEARCH_KEYWORD` 断言；`backend/.venv/bin/python -m pytest backend/tests/test_live_clash_config_script_contracts.py -q` -> 1 passed；全量 `backend/.venv/bin/python -m pytest -q -m no_postgres` -> 769 passed / 787 deselected；脚本 py_compile、`git diff --check` 通过。
- decision: status=local_verified_pending_release；release_gate=pending；production_verification=needs_precise_keyword_retest。
- next_agent: qa
- unresolved: 修复提交尚未重新发布；需要以 `CLASH_TARGET_QUERY=郑州`、`CLASH_SEARCH_KEYWORD=xiaozisk` 重新创建 3 账号线上任务。

## 2026-07-06 账号面具按账号分组批量绑定代理 Development Complete（本地验证）

- message_id: 2026-07-06-account-mask-pool-proxy-batch-bind-devcomplete-001
- action: 按用户确认的 A 方案，在“账号面具 / 账号代理”支持选择账号中心分组批量绑定代理，系统设置 Clash 页继续只维护订阅源池。
- input: 用户确认“在账号面具/账号代理里按账号中心分组批量绑定代理，系统设置 Clash 页不做账号分配”。
- output: 主 PRD、数据流转索引和项目结构索引已同步边界；新增 `AccountEnvironmentProxyBatchBindRequest/Out` 和 `backend/app/services/account_environment_bulk.py`；新增 `POST /api/account-environment-bindings/batch-proxy-bind`，权限为 `account_environment.manage`；前端账号面具页新增账号中心分组、代理资源、授权槽位和变更原因的批量绑定入口，提示“只更新已有授权环境”，系统设置 Clash 页不出现账号分组选择。
- evidence: 先补 RED 测试并确认缺 schema/service 时失败；修复后 `backend/.venv/bin/python -m pytest -q backend/tests/test_account_environment_bulk_proxy_binding.py backend/tests/test_account_environment_bindings.py backend/tests/test_account_mask_frontend_contracts.py backend/tests/test_permission_vocabulary.py -m no_postgres` -> 27 passed；`backend/.venv/bin/python -m py_compile backend/app/services/account_environment_bulk.py backend/app/schemas/account_environment.py backend/app/api/routers/ai_config.py backend/app/permission_middleware.py` passed；`npm --prefix frontend run build` passed；`git diff --check` passed。
- decision: status=local_verified_pending_release；release_gate=pending；production_verification=unproven。
- next_agent: qa
- unresolved: 尚未推送 release/master；尚未执行 Deploy Production；线上账号面具页面与批量绑定真实数据流仍需发布后复核。

## 2026-07-06 账号面具批量绑定 Clash 节点 Development Complete（本地验证）

- message_id: 2026-07-06-account-mask-clash-node-batch-bind-devcomplete-001
- action: 修复账号面具“账号代理”批量绑定无法选择 Clash 代理节点的问题。
- input: 用户反馈“面具 中的账号代理，没办法去配置 clash的代理有问题”。
- output: 新增 `ProxyAirportNodeOut`、`GET /api/account-environment-bindings/proxy-airport-nodes` 和 `backend/app/services/proxy_airport_accounts.py`；账号面具页加载健康 Clash 节点并在本地代理 / Clash 节点间二选一；`AccountEnvironmentProxyBatchBindRequest` 支持 `proxy_airport_node_id`，批量绑定时校验节点来自已启用且同步健康的订阅，复用 / 创建 `AccountProxy` 后写入 `AccountProxyBinding.proxy_airport_node_id`、出口观测字段和 `AccountEnvironmentBinding.proxy_id`。运行中 failover 改为复用同一 `proxy_for_airport_node` 逻辑。
- evidence: RED：新增 Clash 节点批量绑定、异常节点拒绝、前端合同和权限合同测试后，定向测试按预期 4 failed / 2 passed；GREEN：`backend/.venv/bin/python -m pytest -q backend/tests/test_account_environment_bulk_proxy_binding.py backend/tests/test_account_environment_bindings.py backend/tests/test_proxy_airport_failover.py backend/tests/test_account_mask_frontend_contracts.py backend/tests/test_permission_vocabulary.py -m no_postgres` -> 34 passed；`backend/.venv/bin/python -m py_compile ...` passed；`npm --prefix frontend run build` passed（仅 Vite 大 chunk 警告）。
- decision: status=local_verified_pending_release；release_gate=pending；production_verification=unproven。
- next_agent: qa
- unresolved: 尚未推送 release/master；尚未执行 Deploy Production；线上账号面具页面和生产 Clash 节点绑定需发布后复核。

## 2026-07-06 频道评论敏感 provider 边界 Development Complete（历史记录，已撤销）

- message_id: 2026-07-06-channel-comment-sensitive-provider-boundary-devcomplete-001
- action: 曾尝试为频道评论 / 引用回复增加敏感上下文改写和 MiniMax 旧模型重试。
- input: 生产 SSH 只读排查确认任务 `b2fa5cb5-5878-4b29-ba5c-6bc61f92d59e` 当时默认走 MiniMax-M3 并出现 provider 拒绝。
- output: 该实现已按用户后续“模型支持暗示，不要再做处理”的口径撤销，不进入最终发布。
- evidence: 历史本地验证不再作为当前发布证据；当前证据见 2026-07-06-ai-sensitive-processing-reverted-devcomplete-001。
- decision: superseded_by=2026-07-06-ai-sensitive-processing-reverted-devcomplete-001；该敏感上下文改写和 MiniMax 旧模型降级不再作为发布内容。
- next_agent: qa
- unresolved: 已被后续撤销记录覆盖。

## 2026-07-06 账号代理 / 授权指纹 Tab 重复展示修复（本地验证）

- message_id: 2026-07-06-account-mask-tabs-split-devcomplete-001
- action: 修复账号面具中“账号代理”和“授权指纹”Tab 内容相同的问题。
- input: 用户反馈“现在的 账号代理 授权指纹 tab里面是一样的内容”。
- output: 根因是 `proxies` 和 `fingerprints` 两个 Tab 共用同一个 `environmentTable`；已拆成 `proxyTable` 和 `fingerprintTable`。账号代理 Tab 只展示搜索、代理批量绑定、代理状态和生效边界；授权指纹 Tab 展示远端观测刷新、配置指纹、远端观测和一致性状态。
- evidence: 新增 RED/GREEN 前端合同测试 `test_account_masks_view_separates_proxy_and_fingerprint_tabs`；`backend/tests/test_account_mask_frontend_contracts.py -m no_postgres` -> 7 passed；`npm --prefix frontend run build` passed（仅 Vite 大 chunk 警告）。
- decision: status=local_verified_pending_release；release_gate=pending；production_verification=unproven。
- next_agent: qa
- unresolved: 尚未推送 release/master；线上静态包和真实页面点击需发布后复核。

## 2026-07-06 Code Review 修复：账号代理批量绑定与 MiniMax 回退边界（本地验证）

- message_id: 2026-07-06-account-mask-proxy-review-fixes-devcomplete-001
- action: 修复未提交更改 review 中指出的三个问题。
- input: review findings：批量绑定只改每账号一条授权槽位；generic safety refusal 不应触发旧 MiniMax 模型回退；前端选项 `Promise.all` 导致本地代理接口失败会阻断 Clash 节点绑定。
- output: `account_environment_bulk.py` 改为更新账号在目标 `session_role` 下所有 active 授权环境，成功账号数仍按账号去重；账号代理批量面板改用 `Promise.allSettled` 独立加载账号分组、本地代理和 Clash 节点，并显示单项失败。MiniMax 敏感处理已按用户后续口径撤销。
- evidence: RED：新增回归先失败，分别暴露第二个 active 环境未更新、前端仍使用 `Promise.all`；GREEN：相关账号代理和前端合同测试通过。AI provider 敏感改写/降级不再作为本次发布内容。
- decision: status=local_verified_pending_release；release_gate=pending；production_verification=unproven。
- next_agent: qa
- unresolved: 尚未推送 release/master；尚未执行 Deploy Production；线上账号面具真实 UI 和 provider 行为仍需发布后复核。

## 2026-07-06 用户确认模型支持暗示后的 AI 处理撤销（本地验证）

- message_id: 2026-07-06-ai-sensitive-processing-reverted-devcomplete-001
- action: 按用户最新口径撤销本地敏感上下文暗示词改写和 MiniMax 旧模型自动降级。
- input: 用户反馈“现在模型已经支持暗示了，不要在做处理”。
- output: `_sanitize_sensitive_context` 改为原文透传，不再替换敏感/暗示上下文；删除新增的 `情趣内衣/小小j/双峰/含住` 等暗示词映射；删除 MiniMax `new_sensitive/1026` 后自动尝试 M2.7/M2.5 的逻辑；删除新增的 `test_ai_sensitive_context_sanitization.py`。保留系统提示中的输出边界：只围绕原文事实，不新增联系线索、成本细节、邀约或促成信息。
- evidence: release run `28798964065` 在 `checks / Backend checks` 阶段失败，未进入 build-images/deploy，生产未部署该提交；本地修复后验证：`backend/tests/test_ai_gateway.py::test_sensitive_group_context_is_preserved_before_provider_prompt` + 账号代理/前端/权限定向 no_postgres -> 15 passed；changed backend py_compile passed；`npm --prefix frontend run build` passed（仅 Vite 大 chunk warning）；`git diff --check` passed。未标记 no_postgres 的两个 `test_ai_gateway.py` 旧断言用例本地被 PostgreSQL conftest 阻断，待 CI PostgreSQL 环境验证。
- decision: status=local_verified_pending_release；release_gate=pending；production_verification=unproven。
- next_agent: qa
- unresolved: 需要重新跑本地相关回归、提交修复并重新推送 `master -> release`。

## 2026-07-07 频道 AI 评论过程性内容泄露修复（本地验证）

- message_id: 2026-07-07-channel-comment-ai-meta-filter-devcomplete-001
- action: 修复频道评论候选和已创建评论 action 会放过 `<think>` / “让我分析...” / “让我仔细分析这个请求”等 AI 过程性内容的问题。
- input: 用户提供 Telegram 截图，评论区出现 `<think>`、`让我分析这个频道内容`、`让我仔细分析这个请求`、`这是一个要求生成 Telegram 频道评论的任务` 等明显非真人评论。
- output: `content_filters.looks_like_ai_meta_content` 新增 AI 过程性内容识别；`ai_generator.clean_channel_comment_contents` 在生成阶段丢弃这类候选；`dispatcher._dispatch_comment` 在 Telegram gateway 前复用公共出站内容过滤器，命中时以 `content_policy` / `拦截 AI 过程性内容` 可见失败，不调用 TG。
- evidence: RED：新增两条回归先失败，证明频道评论清洗放过 `<think>` / “让我分析”，且发送前不会按 content_policy 失败；GREEN：`backend/tests/test_ai_gateway.py::test_channel_comment_rejects_thinking_and_analysis_meta_text` + `backend/tests/test_operations_center_runtime.py::test_channel_comment_pre_send_validation_blocks_ai_meta_text` -> 2 passed；`backend/.venv/bin/python -m compileall -q backend/app` passed；`git diff --check` passed。
- decision: status=local_verified_pending_release；release_gate=pending；production_verification=blocked_by_ssh_network。
- next_agent: qa
- unresolved: 较宽的 5 条组合测试因本地 PostgreSQL 测试库不可用被 pytest reset gate 阻断，未执行；尚未推送 release / Deploy Production；当前环境到生产 SSH 和公网均超时，不能证明线上已恢复。

## 2026-07-10 AI 活群“全部账号”每日履约账本 Development Complete（本地待 QA）

- message_id: 2026-07-10-ai-all-account-daily-ledger-devcomplete-001
- action: 按专项 PRD 将“全部账号”任务从动态缩小分母改为持久账号关系、事件增量同步和北京时间每日覆盖账本。
- input: 所有正常、Session 可用普通账号自动加入每个全部账号任务并推进目标群；只有目标群真实成功发言才完成；入群失败、受限、离线和 Session 失效继续显示阻塞；避免任务多时重复全量扫描；失败回补不得破坏 AI 模拟聊天内容。
- output: 新增 `TaskAccountDailyCoverage` / `AccountEligibilityEvent` 与 0088 迁移；任务创建初始化持久账号范围，账号录入/登录/健康/用途变化写增量事件，Planner 前消费事件并执行租户级低频核对；准入和 Planner 从持久关系/账本读取；覆盖 Action 原子预约，成功必须对应成功 ExecutionAttempt 和非空 Telegram `remote_message_id`；失败释放后终结旧 Action 并重新规划自然上下文，unknown 不立即重发；全部账号模式禁止模板/表情补量；容量不足显式阻塞；任务详情新增真实分母摘要和分页账号明细；回填脚本支持存量任务幂等修复；群消息拉取增加整体超时。
- evidence: TDD 新增链路 41 passed；任务中心/Planner/Dispatcher/准入相关回归 142 passed（239 deselected）；账号录入/用途/安全、Gateway 和 recovery 边界 116 passed（92 deselected）；`python -m compileall` passed；Alembic `heads` 为单头 0088，0087->0088 离线 SQL 62 行且迁移 SQLite 升降级测试通过；前端 `tsc + vite build` passed（仅既有大 chunk 警告）；新增模块函数均不超过 50 行；`git diff --check` passed。全历史 `alembic upgrade head --sql` 仍被既有 0002 迁移的离线 inspect 限制阻断，与 0088 无关。
- decision: status=local_verified_pending_qa；release_gate=required_pending；production_verification=unproven。
- next_agent: qa
- unresolved: 尚未发布；未执行生产回填；未经过完整北京时间自然日验证，不能标记 `production_fixed`。

## 2026-07-11 生产核心页面有界加载 Development Complete

- message_id: `2026-07-11-production-page-performance-devcomplete-001`
- action: 修复生产 3,810 个运营目标和任务中心全量读取导致的慢页、15 秒超时与任务编辑不可用。
- output: `/api/operation-targets` 新增有界分页、搜索、ids 回显、关联群与能力过滤，并只聚合当前页；runtime summary 支持 `target_ids`；新增 `/api/tasks/page` 统一普通任务与账号安全批任务的轻量分页、统计、分组和当前页水合；七个第一方目标消费者均改为显式有界；任务创建/编辑先打开壳层再远程加载目标；浏览器分页头通过 CORS 显式暴露。
- evidence: 规模回归覆盖 170 条任务跨页无重复、100 条响应 <100KB、系统批次全计数与当前页 runtime hydration；目标分页覆盖 3,810 / 10,000 规模、组合过滤、租户隔离与常量级 SQL。全量 no-postgres `1044 passed, 806 deselected, 5 warnings in 29.11s`；PostgreSQL 定向 `20 passed`；frontend production build、compileall、diff check 通过。
- decision: `development_complete=true`；`release_gate=pending`；`production_fixed=unproven`。
- next_agent: qa
- unresolved: 尚未合并并推送 `master -> release`，未执行 Deploy Production，生产 p95/p99、30 次串行 + 10 并发和 502 日志复核仍待 E4。

## 2026-07-11 搜索排名观察与 Listener 主线集成 Development Complete

- message_id: `2026-07-11-rank-listener-mainline-integration-devcomplete-001`
- action: 将本地搜索排名观察强化分支和未提交 Listener 补充修复重新集成到当前主线。
- output: 真实 Telethon 搜索/安全点击 Gateway、专用账号用途隔离、多降权分组、持久 runtime proxy、逐点击 reservation 和前端分组流程已进入集成分支；Listener 严格单账号读取，不受发送容量/冷却影响，空任务范围不扩散。已发布 `0087` 保持不变，新增 `0089` active 节点唯一索引迁移。
- evidence: no-PostgreSQL `1180 passed, 806 deselected`；PostgreSQL 805 项分批覆盖通过；PostgreSQL task row lock 并发 reservation 测试通过；`0001 -> 0089`、`0089 -> 0088 -> 0089` 迁移通过；前端 build、compileall、diff check 通过。
- decision: `development_complete=true`；`release_gate=local_pass`；`production_verification=unproven`。
- next_agent: qa
- unresolved: 真实 Telegram 协议样本、代理出口、灰度账号点击和生产 E4 尚未验证。

## 2026-07-13 AI 活群 Planner 规模治理 Development Complete

- message_id: `2026-07-13-ai-group-planner-scale-devcomplete-001`
- output: 移除 Planner 每任务在线全量 reconcile；readiness、容量和 backlog 改为批量 / 聚合读取；无 open Action 跳过 preparation；在线来源无变化时零 UPDATE；低频恢复 active 时立即 warming + probe。
- evidence: TDD 覆盖 4×580、容量全维等价、新账号 E2E、低频转 active 和 PostgreSQL backlog；全量 no-PostgreSQL `1246 passed`，PostgreSQL `15 passed`，编译和 diff check 通过。
- decision: `development_complete=true`；`qa_pass=true`；`product_accepted=true`；`release_gate=ready_pending_publish`。
- unresolved: 尚未发布；完整北京时间自然日矩阵和评论任务生产结果 unproven。

## 2026-07-14 AI 交互恢复 Release Checks Rework

- message_id: `2026-07-14-ai-interaction-release-rework-devcomplete-001`
- output: workflow 测试改按异步 metrics 与持久 listener 输入验收；覆盖测试按北京时间建账；generation recovery 测试清理遗留 pending Action；生成质量失败终态释放账号 runtime reservation。
- evidence: 失败前严格第二周期回归稳定暴露 `account_inflight_conflict`；修复后原 12 项 `12 passed`、workflow 全文件 `104 passed / 14 skipped`、相关 generation/comment/coverage `38 passed`。
- decision: `development_complete=true`；不恢复 Dispatcher 热路径全历史 stats，不以 pending 冒充发送成功；转独立 re-QA。
- unresolved: 新 commit 尚未发布，生产仍为 `fecdcfae`。

## 2026-07-15 Runtime Retention 长事务 Development Complete

- message_id: `2026-07-15-runtime-retention-batch-devcomplete-001`
- root_cause: 生产新 recovery 真实复现 `cleanup_runtime_details` 全量载入并单事务删除所有过期 Action，DELETE 超过 6 分钟且拖慢 planner/dispatcher；不是旧容器残留。
- output: 按 recovery `limit` 确定性选取最早创建的过期 Action并 `FOR UPDATE SKIP LOCKED`；每批独立汇总、删子表/Action、记审计，统计用原子 upsert 跨批累加。Coverage/Admission 长期记录清空 Action 引用，SearchRank 动作预约随 Action 删除。生产 EXPLAIN 复用 `ix_actions_created_at`，避免 effective-time 排序触发全表扫描。
- evidence: TDD 红灯为 `batch_size` 不支持；绿灯验证 3 条按 2+1 删除、total=3。独立 review 的 1 Critical/2 Important 由真 PG 全外键与双 session 回归关闭；合并相关测试 `40 passed`，workflow `104 passed / 14 skipped`。
- decision: `development_complete=true`；转 re-QA，尚未重新发布。
- unresolved: 完整 Release Checks、新 recovery 每轮事务时长、评论远端成功与北京时间自然日覆盖 E4。

## 2026-07-15 Runtime Retention 外键扫描 Development Complete

- message_id: `2026-07-15-runtime-retention-fk-index-devcomplete-002`
- root_cause: 生产 100 条 Action DELETE 仍持续 1 至 3 分钟；9 个 Action 外键中 Review 1 列、Coverage 2 列、Admission 4 列缺少 leading index，外键校验重复全表扫描。
- output: 0095 使用并发 DDL 补齐 7 个外键索引并同步模型元数据；不调整清理批次、保留期和群/评论配置。
- evidence: 迁移幂等/可逆及 PostgreSQL concurrent DDL `9 passed`；相关 retention/comment/coverage/recovery `128 passed`。
- decision: `development_complete=true`；转 re-QA/Product 后发布。

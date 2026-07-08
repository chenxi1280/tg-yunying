# Worklog: qa

## 2026-07-08 硅谷 recovery CPU dedupe 与 worker healthcheck QA

- message_id: 2026-07-08-sv-recovery-cpu-backpressure-qa-dedup-healthcheck-001
- action: 对重复 membership reprobe 结果传播和 worker 本地 heartbeat healthcheck 做本地回归。
- input: 2026-07-08-sv-recovery-cpu-backpressure-dev-rework-dedup-healthcheck-001。
- output: local_qa_pass_dedup_and_local_healthcheck
- evidence: 红测 `test_recovery_marks_duplicate_identity_probe_rows_failed` 先失败，证明同账号同目标的重复 `unknown_after_send` 行不会随一次 failed probe 显式标记；修复后该用例转绿。定向 recovery/lifecycle/gateway `19 passed`；worker healthcheck 测试 `16 passed, 5 warnings`；最终全量 no_postgres `807 passed, 781 deselected, 5 warnings`；`compileall` passed；`git diff --check` passed。
- decision: 本地 QA 通过；重复 membership 积压不会再放大 recovery probe，worker 容器健康检查不再依赖启动 Python / DB 查询。待 prod-diagnosis 使用生产 E4 确认 CPU/load。
- next_agent: prod-diagnosis
- unresolved: 无本地 QA 阻断项。

## 2026-07-08 硅谷 recovery CPU stale failed result QA

- message_id: 2026-07-08-sv-recovery-cpu-backpressure-qa-stale-failed-result-001
- action: 对 stale executing membership failed probe 保留 failed result 做本地回归。
- input: 2026-07-08-sv-recovery-cpu-backpressure-dev-rework-stale-failed-result-001。
- output: local_qa_pass_stale_failed_result_preserved
- evidence: 红测 `test_stale_executing_membership_failed_probe_clears_lease_and_stops_reprobe` 先失败，证明旧逻辑会把 failed result 覆盖成普通 `unknown_after_send` 并在下一轮重复 probe；修复后定向 recovery/lifecycle/gateway `18 passed`，全量 no_postgres `804 passed, 781 deselected, 5 warnings`，`compileall` passed，`git diff --check` passed。
- decision: 本地 QA 通过；待重新发布和生产 E4。
- next_agent: dev
- unresolved: release deploy、生产日志/CPU/DB 验证 pending。

## 2026-07-08 硅谷 recovery CPU failed probe client QA

- message_id: 2026-07-08-sv-recovery-cpu-backpressure-qa-probe-client-001
- action: 对 failed probe Telethon client 释放和 failed 落库做本地回归。
- input: 2026-07-08-sv-recovery-cpu-backpressure-dev-rework-probe-client-001。
- output: local_targeted_qa_pass_probe_client_cleanup
- evidence: 红测 `test_probe_failure_invalidates_cached_client` 先失败，证明失败 probe 结果不会释放 cached client；红测 `test_recovery_marks_failed_probe_result_and_skips_next_round` 先失败，证明 failed 落库缺少可见错误详情。修复后定向 recovery/lifecycle/gateway `17 passed`，全量 no_postgres `803 passed, 781 deselected, 5 warnings`，`compileall` passed，`git diff --check` passed。
- decision: 本地 QA 通过；待重新发布和生产 E4。
- next_agent: dev
- unresolved: release deploy、生产日志/CPU/DB 验证 pending。

## 2026-07-08 硅谷 recovery CPU 背压修复本地 QA

- message_id: 2026-07-08-sv-recovery-cpu-backpressure-local-qa-001
- action: 对 recovery CPU 持续升高修复做本地自动化验收。
- input: 2026-07-08-sv-recovery-cpu-backpressure-devcomplete-001。
- output: local_qa_pass_for_recovery_backpressure_and_timeout_cleanup
- evidence: `backend/tests/test_task_recovery_backpressure.py` 覆盖 unknown membership 单轮复检上限、Telegram reprobe timeout 显式落库，以及 stale executing membership timeout 后清空旧 lease 并进入冷却；`backend/tests/test_telethon_lifecycle.py` 覆盖 operation timeout 后 coroutine cancel。验证通过：目标背压测试 `3 passed`；联合 Telethon lifecycle `12 passed`；worker/recovery 相关 `17 passed, 10 deselected`；全量 no_postgres 60 秒门禁 `798 passed, 781 deselected, 5 warnings`；`compileall` 和 `git diff --check` 通过。
- decision: 本地 QA 通过；QA 返工指出的 stale executing timeout tight loop 已用红测覆盖并修复。该修复没有引入 silent fallback，TimeoutError 会写入可见 `telegram_probe_timeout` 和下一次冷却时间，且 stale executing 路径会退出 `executing` 并清空 lease。
- next_agent: product
- unresolved: CI / release deploy / 生产 recovery 容器重启、CPU 降载、`worker drain failed` 清零仍 unproven。

## 2026-07-08 硅谷 recovery CPU 背压连接失败本地 QA

- message_id: 2026-07-08-sv-recovery-cpu-backpressure-qa-connection-rework-001
- action: 对生产 E4 暴露的 Telegram probe `ConnectionError` 分支做本地回归。
- input: 2026-07-08-sv-recovery-cpu-backpressure-dev-rework-connection-001。
- output: local_targeted_qa_pass_connection_error_cooldown
- evidence: 红测 `test_stale_executing_membership_connection_error_clears_lease_and_cools_down` 先失败，证明旧代码会让 `ConnectionError` 冒泡打断 recovery；修复后 `backend/tests/test_task_recovery_backpressure.py` `4 passed`，联合 Telethon lifecycle `13 passed`，全量 no_postgres `799 passed, 781 deselected, 5 warnings`，`compileall` passed，`git diff --check` passed。
- decision: 本地 QA 通过；连接失败会显式写入 `telegram_probe_connection_error` 和下一次冷却时间，且 stale executing 路径会退出 `executing` 并清空 lease。
- next_agent: dev
- unresolved: 重新发布和生产 E4 pending。

## 2026-07-08 硅谷 recovery CPU Telethon lifecycle 本地 QA

- message_id: 2026-07-08-sv-recovery-cpu-backpressure-qa-lifecycle-connect-001
- action: 对 Telethon connect failure 后台任务清理做本地回归。
- input: 2026-07-08-sv-recovery-cpu-backpressure-dev-rework-lifecycle-connect-001。
- output: local_targeted_qa_pass_lifecycle_connect_cleanup
- evidence: 红测 `test_telethon_lifecycle_disconnects_new_client_after_connect_failure` 先失败，证明旧实现 connect failure 后不会 disconnect 新 client；修复后 `backend/tests/test_telethon_lifecycle.py` `10 passed`，`backend/tests/test_task_recovery_backpressure.py` `4 passed`，全量 no_postgres `800 passed, 781 deselected, 5 warnings`，`compileall` passed，`git diff --check` passed。
- decision: 本地 QA 通过；connect failure 后新 client 会被断开且不进入 cache。
- next_agent: dev
- unresolved: 重新发布和生产 E4 pending。

## 2026-07-08 硅谷 recovery CPU unknown membership batch 饥饿 QA

- message_id: 2026-07-08-sv-recovery-cpu-backpressure-qa-reprobe-query-001
- action: 对 unknown membership 补偿复检 batch 查询饥饿做本地回归。
- input: 2026-07-08-sv-recovery-cpu-backpressure-dev-rework-reprobe-query-001。
- output: local_qa_pass_reprobe_query_due_filter
- evidence: 红测 `test_recovery_skips_failed_reprobe_rows_when_selecting_batch` 先失败，证明旧查询会被已 failed 行占满而不 probe 后面的 due 行；修复后 recovery 背压测试 `5 passed`，Telethon lifecycle `10 passed`，全量 no_postgres `801 passed, 781 deselected, 5 warnings`，`compileall` passed，`git diff --check` passed。
- decision: 本地 QA 通过；batch 查询会跳过 failed / cooldown 未到期行。
- next_agent: dev
- unresolved: 重新发布和生产 E4 pending。

## 2026-07-06 AI 活群 hard-hourly 分布护栏补齐 QA

- message_id: 2026-07-06-ai-group-hard-hourly-distribution-guard-qa-002
- action: 对 hard-hourly 旧偏斜队列清理、在线样本可见性和新批次分布门禁做本地自动化验收。
- input: 2026-07-06-ai-group-hard-hourly-distribution-guard-002
- output: qa_pass
- evidence: 新增三条定向回归 3 passed / 50 deselected；`backend/tests/test_ai_group_hard_hourly_target.py -m no_postgres` 17 passed / 36 deselected；`backend/tests/test_ai_group_quality_diagnostics.py -k "hard_hourly or account_online"` 10 passed / 20 deselected；`backend/.venv/bin/python -m compileall -q app` 和 `git diff --check` 通过。
- decision: 本地 QA 证明偏斜旧 open action 会被显式跳过重规划，账号在线不足有样本可查，新 hard-hourly 偏斜批次会在写库前被阻断；生产恢复仍需 release 后 SSH 直连验证。
- next_agent: prod-diagnosis
- unresolved: 未推送 release / 未生产 SSH 复核。

## 2026-07-06 AI 活群 hard-hourly 账号轮转修复 QA

- message_id: 2026-07-06-ai-group-hard-hourly-account-rotation-qa-001
- action: 对 dev complete 的 hard-hourly 账号轮转修复做定向自动化验收和监督意见复核。
- input: 2026-07-06-ai-group-hard-hourly-account-rotation-001
- output: qa_pass
- evidence: 监督子代理 James 复核代码无阻塞，仅指出状态板应先交 QA，已修正；定向新增回归 `test_group_ai_chat_hard_hourly_preserves_cycle_rotation_over_account_memory` 1 passed；hard-hourly 文件级 no_postgres 14 passed / 36 deselected；相关 AI 活群 hard-hourly + dataflow no_postgres 34 passed / 36 deselected；全量 no_postgres 60s gate 771 passed / 787 deselected；`backend/.venv/bin/python -m compileall -q backend/app` 和 `git diff --check` 通过。
- decision: 本地 QA 证明 hard-hourly 追量不会再被历史账号记忆压制轮转，且普通非 hard-hourly 记忆优先、全账号日覆盖和需要复用的 hard-hourly 能力未被本地测试覆盖为回归。
- next_agent: prod-diagnosis
- unresolved: 尚未发布 release / Deploy Production；线上 AI 活群真实账号分布和质量诊断仍 unproven。

## 2026-06-27

- message_id: 2026-06-27-docs-practice-qa-001
- action: 独立验收四 Agent 文档级演练材料
- input: 2026-06-27-docs-practice-devcomplete-001
- output: pass
- evidence: agent registry、五个模板、四个 worklog、完整 runs 记录均已存在；业务代码未修改
- decision: status=pass
- next_agent: prod-diagnosis
- unresolved: 真实生产问题复核不适用于本次文档级演练

## 2026-06-28 AI 活群话题老师连发配置 QA

- message_id: 2026-06-28-ai-group-topic-teacher-burst-qa-001
- action: 对 dev complete 的 AI 活群话题/老师/连发/Web/TG bot 配置做定向自动化验收
- input: 2026-06-28-ai-group-topic-teacher-burst-devcomplete-001
- output: qa_pass
- evidence: no_postgres 定向后端测试 13 passed, 97 deselected；frontend `npm run build` 成功；`git diff --check` 成功
- decision: schema 校验、planner payload、同账号连发、TG bot admin 权限与保存、Web 编译通过
- next_agent: product
- unresolved: CI / release deploy / production verification unproven

## 2026-06-28 hard-hourly min 10 QA

- message_id: 2026-06-28-hard-hourly-min-10-qa-001
- action: 对 AI 活群硬小时默认 10 的 schema、迁移、前端常量和文档同步做定向验收
- input: 2026-06-28-hard-hourly-min-10-devcomplete-001
- output: qa_pass
- evidence: no_postgres 定向后端测试 13 passed, 97 deselected；frontend `npm run build` 成功；`git diff --check` 成功
- decision: 低于 10 被拒绝，旧默认 60 迁移到 10，前端 build 通过
- next_agent: product
- unresolved: CI / release deploy unproven

## 2026-07-03 搜索自动入群本地 QA

- message_id: 2026-07-03-search-join-group-local-qa-001
- action: 对 `search_join_group` 本地实现做定向自动化验收
- input: 2026-07-03-search-join-group-devcomplete-001
- output: local_qa_pass_for_code_contracts
- evidence: search_join 定向、task-center 相关回归和 frontend gating 合并验证 212 passed / 79 deselected；backend compileall passed；迁移脚本 py_compile passed；frontend build passed；git diff --check passed。
- decision: schema / model / migration / planner / dispatcher fail-closed / linked dispatch / frontend 创建和详情契约通过本地自动化验证。
- next_agent: product
- unresolved: 这不是独立 QA 线程 ACK；CI、release deploy、真实生产 E4、真实 MTProto 搜索入群 gateway 灰度均 unproven。

## 2026-07-03 搜索自动入群监督补缺本地 QA

- message_id: 2026-07-03-search-join-group-supervised-fix-local-qa-001
- action: 对监督补缺后的 `search_join_group` 代码做本地自动化复验
- input: 2026-07-03-search-join-group-supervised-fix-devcomplete-001
- output: local_qa_pass_for_fail_closed_code_contracts
- evidence: 监督补缺定向套件 27 passed；全量 no_postgres 653 passed / 798 deselected；backend compileall passed；迁移脚本 py_compile passed；frontend build passed；git diff --check passed。
- decision: 关键词校验、协议样本表闸门、planner fail-closed、proxy egress guard、gateway 成功后联动记录、专项权限和前端 build 均有本地证据。
- next_agent: product
- unresolved: 这不是独立 QA 线程 ACK；CI / release deploy / 生产健康 / 真实代理出口 / 真实目标机器人协议样本 / 7 天灰度仍 unproven。

## 2026-07-06 Clash 新增订阅优先级冲突 UX 本地 QA

- message_id: 2026-07-06-clash-add-priority-ux-local-qa-001
- action: 对系统设置 Clash 新增订阅默认优先级和错误提示做本地定向验收。
- input: 2026-07-06-clash-add-priority-ux-devcomplete-001。
- output: local_qa_pass_for_clash_add_priority_ux_contract。
- evidence: 前端合同测试覆盖 `nextAvailablePriority`、中文优先级冲突提示和新增表单自动回填优先级；`backend/.venv/bin/pytest backend/tests/test_account_mask_frontend_contracts.py -q` -> 5 passed；后端订阅规则回归 `backend/.venv/bin/pytest backend/tests/test_proxy_airport_subscription.py -q` -> 15 passed；`npm --prefix frontend run build` passed。
- decision: 本地合同验收通过；不声明线上页面已经刷新。
- next_agent: prod-diagnosis
- unresolved: CI / Deploy Production / 线上静态包和真实新增操作仍 unproven。

## 2026-07-03 搜索自动入群 Release Gate QA Handoff

- message_id: 2026-07-03-search-join-group-release-gate-qa-001
- action: 接收监督补缺本地 QA 后的生产发布证据，并记录真实 QA 线程投递
- input: 2026-07-03-search-join-group-supervised-fix-devcomplete-001
- output: qa_pass 范围升级为“本地契约通过 + Release Gate 通过 + 生产健康通过”；已真实投递 QA 线程 `019f07c7-1c0d-72a2-95fe-9f618aff0a00`。
- evidence: 本地 no_postgres 653 passed / 798 deselected；frontend build passed；Deploy Production run `28644819954` passed；生产 release `20260703071946_32b0257`；公网 `/api/health` 和 `/task-center` HTTP 200。
- decision: qa_pass_for_code_and_release_health；不声明真实搜索入群灰度通过。
- next_agent: product
- unresolved: QA 线程 ACK 未返回；product acceptance 未确认；真实协议样本、代理出口和 7 天灰度未验证。

## 2026-07-03 接码专用账号只接码限制本地 QA

- message_id: 2026-07-03-code-receiver-restriction-local-qa-001
- action: 对接码专用账号限制做本地自动化复验
- input: 2026-07-03-code-receiver-restriction-devcomplete-001
- output: local_qa_pass_for_code_receiver_contracts
- evidence: `python -m pytest -q backend/tests/test_account_profile_auto_initialization.py backend/tests/test_task_account_pool.py backend/tests/test_account_center_prd_contracts.py -m no_postgres` -> 57 passed；py_compile changed backend files passed；`git diff --check` passed。
- decision: 接码账号不会触发登录后资料初始化 / 账号面具初始化；资料/2FA/设备清理批次预检和 worker 执行前会跳过；消息发送公共入口和旧私聊入口阻断；备用 session 补齐 / 自愈未被禁用。
- next_agent: product
- unresolved: 这不是独立 QA 线程 ACK；CI / release deploy / 生产验证仍 unproven。

## 2026-07-04 账号面具环境与全局 Clash 配置本地 QA

- message_id: 2026-07-04-account-mask-environment-local-qa-001
- action: 对账号面具一级菜单、全局 Clash 配置、账号环境绑定、授权槽位 runtime 和迁移权限缺口做本地自动化验收。
- input: 2026-07-04-account-mask-environment-devcomplete-001；监督子代理指出 migration head / 旧 binding 回填 / Clash GET 权限 / 远端观测展示缺口。
- output: local_qa_pass_for_account_mask_environment_contracts
- evidence: 监督缺口 red tests 已先失败后修复；合并定向套件 189 passed；最终本地 gate `backend/.venv/bin/pytest -q -m no_postgres` -> 718 passed / 789 deselected；frontend build passed；changed backend py_compile passed；`git diff --check` passed。覆盖点包括 `0078` 单 head 和回填 SQL、legacy NULL app binding 复用、Clash GET `system.view` 权限、账号环境远端观测 matched/mismatch、dispatcher 使用授权槽位 session、账号面具一级菜单和系统配置 Clash Tab。
- decision: 本地合同验收通过；不声明生产真实订阅同步或真实 Telegram 远端授权快照已刷新。
- next_agent: product
- unresolved: CI / release deploy / 生产健康 / 生产数据迁移和真实配置验证仍 unproven。

## 2026-07-04 授权槽位代理事实源修正本地 QA

- message_id: 2026-07-04-account-proxy-slot-runtime-fix-local-qa-001
- action: 对 PRD 补洞、授权槽位代理绑定唯一约束、账号面具代理重绑、search_join Dispatcher 双事实源校验做本地自动化验收。
- input: 2026-07-04-account-proxy-slot-runtime-fix-devcomplete-001；监督子代理指出代理重绑唯一索引冲突和 Dispatcher 未校验 `account_proxy_bindings` 行本身。
- output: local_qa_pass_for_slot_proxy_runtime_contracts
- evidence: 定向 39 passed；全量 no_postgres 60s gate 728 passed / 787 deselected；frontend build passed；backend compileall 和 0079 migration py_compile passed；`git diff --check` passed。覆盖点包括同槽位换代理时旧 `AccountProxyBinding` 置 inactive、`0079` 单 head、Dispatcher 不使用 `TgAccountAuthorization.proxy_id` 覆盖环境代理、失效/错槽位 `account_proxy_bindings` fail closed、search_join 实时 pacing/random 不调用 LLM。
- decision: 本地合同验收通过；不声明生产真实订阅同步、真实出口 IP 观测、远端授权设备已立即改变或真实搜索入群灰度通过。
- next_agent: prod-diagnosis
- unresolved: CI / release deploy / 生产健康 / 生产数据迁移和真实配置验证仍 unproven。

## 2026-07-04 搜索目标群点击任务小时容量热修本地 QA

- message_id: 2026-07-04-search-join-hourly-cap-null-hotfix-local-qa-001
- action: 对 `pacing_config.max_actions_per_hour=null` 覆盖 `type_config.max_actions_per_hour` 的线上复现缺口做本地回归验收。
- input: 2026-07-04-search-join-hourly-cap-null-hotfix-devcomplete-001。
- output: local_qa_pass_for_search_join_runtime_config_contracts
- evidence: 新增 `test_search_join_group_runtime_config.py` 覆盖 null 不覆盖和显式 0 关闭容量；`test_search_join_group_config.py` 覆盖 create / config update / settings update API 路径允许 search_join 0；前端静态合同覆盖 search_join min=0、其他任务保持 min=1；定向套件 34 passed；backend compileall passed；frontend build passed；`git diff --check` passed。
- decision: 本地合同验收通过；不声明生产任务已生成 actions 或真实加入成功。
- next_agent: prod-diagnosis
- unresolved: CI / release deploy / 生产健康 / 郑州 3 账号真实搜索点击 / 加入结果仍 unproven。

## 2026-07-05 Clash 多订阅源池本地 QA

- message_id: 2026-07-05-clash-multi-subscription-local-qa-001
- action: 对 Clash 多订阅源池、plural API、前端新增 / 编辑列表、search_join 全订阅不可用 fail-closed、管理员通知和小时 stats 刷新做本地自动化验收。
- input: 2026-07-05-clash-multi-subscription-devcomplete-001；Hegel 只读监督指出 plural sync 未跑健康探测、全订阅不可用未发管理员通知两个 P0。
- output: local_qa_pass_for_multi_subscription_contracts
- evidence: P0 red tests 已先失败后修复；定向 `backend/.venv/bin/python -m pytest backend/tests/test_proxy_airport_subscription.py backend/tests/test_search_join_group_executor.py backend/tests/test_search_join_group_runtime_config.py backend/tests/test_account_mask_frontend_contracts.py backend/tests/test_permission_vocabulary.py backend/tests/test_system_actions_dataflow.py backend/tests/test_merge_integrity.py -q` -> 68 passed；补 P0 后相关定向 -> 38 passed；全量 no_postgres 60s gate -> 757 passed / 787 deselected；用户补充“多个地址、主掉备用”后补前端已有订阅编辑入口，合同测试 1 passed、frontend build passed、`git diff --check` passed；changed backend py_compile passed；行数门禁通过。
- decision: 本地合同验收通过；plural sync 会执行健康探测，全订阅不可用会写 `airport_all_subscriptions_unavailable`、不生成 action 并尝试租户 Bot 管理员通知。
- next_agent: prod-diagnosis
- unresolved: CI / release deploy / 生产健康未验证；真实订阅拉取、真实节点健康、真实出口 IP、运行中 failover event、warmup 重置和郑州线上任务复测仍 unproven。

## 2026-07-05 Clash 运行中 failover 绑定本地 QA

- message_id: 2026-07-05-clash-failover-runtime-local-qa-001
- action: 对 search_join 代理失败触发同订阅 / 备用订阅 failover、绑定切换、事件、已有出口观测转存、warmup 重置、pending payload 重指向、管理员通知状态和代理配置 fail-closed 做本地自动化验收。
- input: 2026-07-05-clash-failover-runtime-devcomplete-001；监督子代理指出多订阅源池仍缺运行时 failover 数据闭环。
- output: local_qa_pass_for_runtime_failover_contracts
- evidence: Red tests 已先失败后修复；Copernicus 只读复核指出候选节点错误依赖 observed exit IP、pending action 仍携带旧 binding、运行中全订阅不可用不通知管理员，均已补红测并修复；定向 `backend/.venv/bin/python -m pytest backend/tests/test_proxy_airport_subscription.py backend/tests/test_proxy_airport_failover.py backend/tests/test_search_join_group_linked_tasks.py backend/tests/test_merge_integrity.py -q` -> 30 passed。覆盖点包括同订阅健康节点优先、备用订阅兜底、健康备用节点未观测出口 IP 也能切换、全部无候选 fail closed、`proxy_node_failover_events` 详情、`proxy_exit_ip_observations` 有则记录、`account_proxy_warmup_states`、`account_environment_bindings.proxy_binding_id` 重指向、pending action payload 重指向、管理员通知状态，以及代理 host 缺失不调用 gateway。
- decision: 本地合同验收通过；不声明生产真实订阅同步、真实出口 IP 或真实 search_join failover 已发生。
- next_agent: prod-diagnosis
- unresolved: 全量 no_postgres、frontend build、CI / release deploy / 生产健康和郑州线上任务复测仍 unproven。

## 2026-07-06 账号面具按账号分组批量绑定代理本地 QA

- message_id: 2026-07-06-account-mask-pool-proxy-batch-bind-local-qa-001
- action: 对“账号面具 / 账号代理”按账号中心分组批量绑定代理做本地自动化验收。
- input: 2026-07-06-account-mask-pool-proxy-batch-bind-devcomplete-001。
- output: local_qa_pass_for_pool_scoped_environment_proxy_binding
- evidence: 新增后端服务 RED/GREEN 覆盖：分组内 2 个账号只有已有 active 授权环境的账号被更新，缺少授权环境账号 skipped；接码专用分组被拒绝；权限合同要求 `POST /api/account-environment-bindings/batch-proxy-bind` 使用 `account_environment.manage`；前端合同要求账号面具页加载 `/account-pools`、`/account-proxies` 并调用 batch-proxy-bind，系统设置 Clash 页不含该入口。定向 `backend/.venv/bin/python -m pytest -q backend/tests/test_account_environment_bulk_proxy_binding.py backend/tests/test_account_environment_bindings.py backend/tests/test_account_mask_frontend_contracts.py backend/tests/test_permission_vocabulary.py -m no_postgres` -> 27 passed；后端 py_compile、frontend build、`git diff --check` 通过。
- decision: 本地合同验收通过；分组选择只作为账号范围，不会自动创建缺失授权环境，也不会启用系统设置 Clash 订阅源。
- next_agent: prod-diagnosis
- unresolved: CI / release deploy / 生产健康 / 线上账号面具静态包和真实分组批量绑定仍 unproven。

## 2026-07-06 账号面具批量绑定 Clash 节点本地 QA

- message_id: 2026-07-06-account-mask-clash-node-batch-bind-local-qa-001
- action: 对“账号面具 / 账号代理”支持选择 Clash 节点做本地自动化验收。
- input: 2026-07-06-account-mask-clash-node-batch-bind-devcomplete-001。
- output: local_qa_pass_for_pool_scoped_clash_node_binding
- evidence: RED/GREEN 覆盖：批量请求可带 `proxy_airport_node_id`；只允许绑定已启用、已同步且 healthy 的 Clash 节点；绑定后 `AccountProxyBinding.proxy_airport_node_id`、出口观测和 `AccountEnvironmentBinding.proxy_id` 写入正确；异常节点返回 `proxy_airport_node_not_available`；账号面具页读取 `/account-environment-bindings/proxy-airport-nodes` 并可选择“Clash 节点”；权限合同要求该读接口为 `account_masks.view`。定向 `backend/.venv/bin/python -m pytest -q backend/tests/test_account_environment_bulk_proxy_binding.py backend/tests/test_account_environment_bindings.py backend/tests/test_proxy_airport_failover.py backend/tests/test_account_mask_frontend_contracts.py backend/tests/test_permission_vocabulary.py -m no_postgres` -> 34 passed；后端 py_compile passed；`npm --prefix frontend run build` passed（仅 Vite 大 chunk 警告）。
- decision: 本地合同验收通过；账号面具可从健康 Clash 节点选择代理来源，系统设置 Clash 页仍不做账号分配。
- next_agent: prod-diagnosis
- unresolved: CI / release deploy / 生产健康 / 线上账号面具静态包和真实 Clash 节点批量绑定仍 unproven。

## 2026-07-06 频道评论敏感 provider 边界本地 QA（历史记录，已撤销）

- message_id: 2026-07-06-channel-comment-sensitive-provider-boundary-local-qa-001
- action: 曾对频道评论 / 引用回复敏感上下文改写和 MiniMax 旧模型重试做本地自动化验收。
- input: 2026-07-06-channel-comment-sensitive-provider-boundary-devcomplete-001。
- output: local_qa_pass_for_channel_comment_sensitive_provider_boundary
- evidence: 历史本地验证不再作为当前发布证据；当前证据见 2026-07-06-ai-sensitive-processing-reverted-local-qa-001。
- decision: superseded_by=2026-07-06-ai-sensitive-processing-reverted-local-qa-001；该敏感上下文改写和 MiniMax 旧模型降级不再作为发布内容。
- next_agent: prod-diagnosis
- unresolved: 已被后续撤销记录覆盖。

## 2026-07-06 账号代理 / 授权指纹 Tab 重复展示本地 QA

- message_id: 2026-07-06-account-mask-tabs-split-local-qa-001
- action: 对账号面具“账号代理”和“授权指纹”Tab 内容拆分做本地自动化验收。
- input: 2026-07-06-account-mask-tabs-split-devcomplete-001。
- output: local_qa_pass_for_account_mask_tab_split
- evidence: 合同测试要求存在 `proxyTable` 和 `fingerprintTable`，账号代理 Tab 指向 `proxyTable`，授权指纹 Tab 指向 `fingerprintTable`，不再使用共享 `environmentTable`，且 `BatchProxyBindingPanel` 只出现在代理 Tab。定向 `backend/.venv/bin/python -m pytest -q backend/tests/test_account_mask_frontend_contracts.py -m no_postgres` -> 7 passed；`npm --prefix frontend run build` passed（仅 Vite 大 chunk 警告）。
- decision: 本地合同验收通过；账号代理和授权指纹 Tab 已拆成不同内容块。
- next_agent: prod-diagnosis
- unresolved: CI / release deploy / 生产健康 / 线上账号面具页面真实点击仍 unproven。

## 2026-07-06 Code Review 修复本地 QA

- message_id: 2026-07-06-account-mask-proxy-review-fixes-local-qa-001
- action: 对账号代理批量绑定、MiniMax 回退边界和前端选项加载隔离做本地自动化验收。
- input: 2026-07-06-account-mask-proxy-review-fixes-devcomplete-001。
- output: local_qa_pass_for_review_fixes
- evidence: RED/GREEN 覆盖：同一账号同一 `session_role` 下多个 active 授权环境全部更新代理，成功账号数按账号去重；账号代理批量面板使用 `Promise.allSettled` 独立加载账号中心分组、本地代理和 Clash 节点，并为单项失败展示中文错误。AI provider 敏感改写/降级已按用户后续口径撤销，不再作为本次发布内容。
- decision: 本地合同验收通过；不声明生产已恢复。
- next_agent: prod-diagnosis
- unresolved: CI / release deploy / 生产健康 / 线上账号面具页面真实点击和 provider 实际 422 行为仍 unproven。

## 2026-07-06 AI 敏感上下文处理撤销本地 QA

- message_id: 2026-07-06-ai-sensitive-processing-reverted-local-qa-001
- action: 对用户确认“模型支持暗示，不要再做处理”后的代码撤销做本地自动化验收。
- input: 2026-07-06-ai-sensitive-processing-reverted-devcomplete-001。
- output: local_qa_pass_for_sensitive_processing_revert
- evidence: 本地验证：`perl -e 'alarm 60; exec @ARGV' backend/.venv/bin/python -m pytest -q backend/tests/test_ai_gateway.py::test_sensitive_group_context_is_preserved_before_provider_prompt backend/tests/test_account_environment_bulk_proxy_binding.py backend/tests/test_account_mask_frontend_contracts.py backend/tests/test_permission_vocabulary.py::test_sensitive_read_routes_have_explicit_least_privilege_rules -m no_postgres` -> 15 passed；changed backend py_compile passed；`npm --prefix frontend run build` passed（仅 Vite 大 chunk warning）；`git diff --check` passed。未标记 no_postgres 的两个 `test_ai_gateway.py` 旧断言用例本地被 PostgreSQL conftest 阻断，待 CI PostgreSQL 环境验证。
- decision: 本地合同验收通过；不声明生产已恢复；release run `28798964065` 已失败且未部署。
- next_agent: prod-diagnosis
- unresolved: 需要重新跑 CI / release deploy / 生产健康。

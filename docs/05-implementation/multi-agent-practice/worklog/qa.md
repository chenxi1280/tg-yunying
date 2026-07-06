# Worklog: qa

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

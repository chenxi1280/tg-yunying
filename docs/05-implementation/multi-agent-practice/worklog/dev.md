# Worklog: dev

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

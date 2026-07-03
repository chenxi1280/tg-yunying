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

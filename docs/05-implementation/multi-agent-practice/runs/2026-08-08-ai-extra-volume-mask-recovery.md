# 2026-08-08 AI extra-volume 缺面具饿死修复

## Intake / Incident

- message_id: `2026-08-08-ai-extra-volume-mask-starvation-001`
- intake_id: `intake-2026-08-08-ai-extra-volume-mask-starvation`
- level: `L3`
- owner_agent: `prod-diagnosis`
- current_agent: `product`
- evidence_level: `E1`
- status: `product_design_complete`
- handoff_delivery_status: `acknowledged`
- target_thread: `current-thread`
- user_goal: 修复线上 AI 活群任务有时不启动，并完成正式发布与 Telegram 真实远端验证。

## 生产第一断点

- 生产 release `9d99319f` 的 Planner、Generation、Dispatcher worker 均 healthy；5 个 running AI 活群中 4 个持续产生远端成功，排除全局 worker/代理故障。
- `郑州大学` 任务 `a52e84f2-8663-4b00-bbbe-196fb626b28d` 在 20:57 后停止真实发送；当日 `due=3848, confirmed=805`。
- 806 个 ready admission 中 805 个有 active/usable 面具，账号 949 是唯一缺面具账号；其面具生成状态为 `manual_required/voice_profile_provider_timeout`。
- extra-volume Planner 每轮把当日成功数为 0 的账号 949 排第一，创建无 coverage、无面具证据的普通生成 Action；Dispatcher 在 Gateway 前以 `account_mask_evidence_missing` 正确拒绝。20:57 至 22:48 共 92 次，`ExecutionAttempt=0`，后续合格账号被饿死。

## Product Design Complete

### 原始需求覆盖矩阵

| user_requirement | product_decision | frontend_design | backend/dataflow_design | qa_acceptance | status |
| --- | --- | --- | --- | --- | --- |
| 修复“有时没启动” | 修复 Planner 候选资格，不放宽发送门禁 | 无组件/API 变化；沿用真实 shortage/blocker 展示 | extra-volume 只选 current-ledger confirmed + active mask + online + Task admission 的账号 | 混合候选时缺面具账号被排除，合格账号继续 | complete |
| 完成线上验证 | 走 master→release→Actions | 无前端发布特例 | 以 deployed SHA、Action→Attempt→remote fact→ledger 增长闭环 | 发布后目标任务出现新非空 remote id，且无新增同类 Planner 失败 | complete |

### 功能与状态设计

- happy_path: Planner 扫描完整任务账号集，排除当前任务日 coverage 未 confirmed 或无 active/usable 面具者，再按成功数、最久未发、稳定 ID 截取 extra-volume 候选。
- alternate_path: 缺面具账号仍可在自己的 coverage 未完成时走精确 `签到`；同一远端成功同时计 coverage 和群日总量。
- error_state: 全部 extra-volume 候选不合格时不创建 Action，保留显式账号/面具容量等待；面具生成 worker 独立推进。
- state_machine: 不新增状态或迁移；既有失败 Action 保持终态，下一 Planner tick 自然选择合格账号。
- permissions/security: 不改权限、账号用途、准入、在线、内容安全或 Gateway 门禁。

### 后端 / API / Worker

- affected_api: none
- affected_services: `executors/group_ai_chat.py::_daily_group_extra_accounts`
- affected_workers: Planner；Generation/Dispatcher 合同不变。
- data_models/migrations: none
- idempotency: 沿用 TaskDayLedger、coverage、Action dedupe 与 remote mutation identity。
- concurrency: 候选资格只读当前 ledger/profile；Action 物化仍由现有事务、槽和唯一键仲裁。
- failure_handling: 不自动重试或伪造面具，不删除旧 Action/Attempt，不引入 fallback。

### QA / Release Gate

- current-ledger confirmed + active mask 的账号可补量；missing/manual_required 面具、coverage unknown/abandoned/not-present 均不可补量。
- 缺面具零成功账号排第一时，Planner 必须继续扫描并选择后续合格账号。
- coverage 专用签到既有回归必须继续通过；Dispatcher `account_mask_evidence_missing` 继续 fail-closed。
- 全部不合格时返回空候选且不创建普通 Action。
- 发布路径必须为 `master -> release -> GitHub Actions Deploy Production`，无数据库迁移和生产数据写入。
- E4 必须在发布 anchor 后证明：目标任务新 Action 有成功 Attempt 和非空 `remote_message_id`，remote message fact 存在，群日 confirmed 增长，且没有新增 `account_mask_evidence_missing` 饿死循环。

### 深度自检

- 隐含场景：单个缺面具、前页全缺后页可用、全部缺面具、跨日旧 coverage、面具 disabled/空摘要、准入或在线并发变化。
- 数据一致性：资格必须绑定 target 当前 `task_day_ledger_id`，不能只按 coverage 日期或历史成功推断。
- 发布风险：master/release 历史分叉但当前 tree 相同；用正常 release merge，不 force push。
- 回滚：代码可回退到上一不可变 release；不涉及 schema/data rollback。
- open_questions: none
- design_status: `complete`
- dev_handoff_ready: `true`

## 阶段记录

| phase | status | evidence |
| --- | --- | --- |
| prod-diagnosis | complete | Task/ledger/Action/Attempt/面具状态只读证据 |
| product | complete | 主 PRD、面具专项、群日专项、数据流索引已同步 |
| dev | pending | 先红测后最小实现 |
| qa | pending | 定向与相关回归、Release Gate |
| product acceptance | pending | 对照本页验收矩阵 |
| production verification | pending | 发布后 Telegram E4 |

## Development Complete

- implementation: `_daily_group_extra_accounts` 在排序和截取前调用 `_eligible_daily_group_extra_accounts`；后者以当前 daily target 的 `task_day_ledger_id` 查询 `state=confirmed AND confirmed_count>=target_count`，再与 active/usable 面具取交集。
- unchanged: Dispatcher 面具证据门禁、coverage 签到、Action/Attempt/remote fact、API/schema/frontend。
- red_evidence: 修复前混合候选错误选择 `[1,3]` 而不是 `[2,4]`；全部缺面具时错误返回账号 1。
- green_evidence: 两条根因用例均通过。
- index_updates: 主 PRD、两个专项 PRD、数据流索引、结构索引均已同步。
- dev_status: `complete`

## QA Validation Report

- targeted: `test_ai_group_daily_group_target.py + test_mask_missing_check_in.py`，29 passed。
- related_no_postgres: `test_ai_group_daily_coverage_planner.py + test_group_ai_chat_dataflow.py + test_fulfillment_fact_first_v3.py + test_ai_generation_phase_boundaries.py`，105 passed，只有 SQLite 不支持反射 expression index 的既有 warning。
- static: Ruff 目标文件通过；compileall 通过；`git diff --check` 通过。
- critical_findings: 0
- important_findings: 0
- migration_required: false
- qa_pass: true
- evidence_level: E2

## Product Acceptance

- 原始“有时没有启动”已被还原为可验证的候选饿死错误，修复点位于第一错误决策而非放宽 Dispatcher。
- 单个缺面具账号不再阻塞合格账号；缺面具 coverage 签到和全部无资格时的显式等待边界保留。
- 无额外产品范围、无 silent fallback、无生产数据修改。
- product_accepted: true
- production_fixed: false
- acceptance_status: `release_ready_e2`

## Release Gate

- candidate_branch: `codex/ai-extra-volume-mask-fix-20260808`
- source_base: `origin/master@489d41ebf83866a010f747c35eb1798030d77578`
- release_path: `master -> release -> GitHub Actions Deploy Production`
- tree_state: 主目录 dirty worktree 已隔离并保持不变；候选来自独立 clean worktree。
- change_scope: Planner 候选过滤、两条回归、产品/数据流/结构/运行记录。
- migrations: none
- data_change: none
- rollback: 回退本候选提交并走同一 release workflow；无需 schema/data rollback。
- required_ci: workflow backend no_postgres、PostgreSQL partition、frontend/build-images/deploy 全部成功。
- production_acceptance: deployed SHA + healthy runtime 仅为 E3；仍必须取得目标 Task 发布后成功 Attempt、非空 remote id、remote fact、群日 confirmed 增长及同类失败不再新增的 E4。
- gate_status: `ready`

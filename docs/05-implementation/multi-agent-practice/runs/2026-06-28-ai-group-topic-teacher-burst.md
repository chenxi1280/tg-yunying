# 2026-06-28 AI 活群话题老师连发配置流程记录

## Intake Card

- intake_id: intake-ai-group-topic-teacher-burst-001
- source: user
- raw_input: 每个 AI 活群任务支持多个话题方向和聊天对象老师，账号调用 AI 时围绕这些方向聊；支持一个账号连续发 2-4 条消息；Web 详情页面可设置；TG bot 内可选择并完整设置 AI 活群任务
- created_at: 2026-06-28
- owner_agent: product
- suspected_type: feature
- affected_surface: Task Center / group_ai_chat / Web task detail / Telegram Bot settings / planner-worker
- production_related: false
- initial_evidence_level: E1
- next_route: triage

## 原始描述

用户要求新增 AI 活群任务配置能力：

- 任务级多个话题方向。
- 任务级多个聊天对象老师。
- AI 生成内容围绕话题方向和老师展开。
- 支持同一个账号连续发送 2-4 条消息模拟真人补充。
- Web 任务详情页支持设置。
- TG bot 内支持选择并完整设置 AI 活群任务。

## 已知证据

- 现有 `group_ai_chat` 已有单字段 `topic_hint`、`account_personas`、`messages_per_round` 和 `allow_account_repeat`。
- 当前线程曾误将 product 任务直接实现，产生 `codex/ai-group-topic-teacher-burst` 分支草稿；该分支不等于 dev 完成或产品接受。

## 需要补充的问题

无。产品口径已确认：

- “老师”是聊天对象/目标对象，不是账号 persona，也不要求绑定真实 TG 用户。
- TG bot 本期做 bot 内完整设置，不只跳转 Web。

## 下一步路由

进入 L2 standard：product -> dev -> qa -> product。

## Triage Card

- intake_id: intake-ai-group-topic-teacher-burst-001
- from_agent: product
- level: L2
- route: standard
- cost_tier: standard_team
- evidence_level: E1
- production_related: false
- release_gate_required: true
- production_verification_required: false
- locked_paths: docs/01-product/tg-ops-platform-prd.md; docs/03-feature-designs/ai-group-topic-teacher-burst-prd.md; docs/00-index/project-dataflow-index.md; backend/app/schemas/task_center.py; backend/app/services/task_center; backend/app/api/routers; frontend/src/app/views/TaskCenter*; frontend/src/app/views/taskCenterViewModel.ts; frontend/src/app/types/taskCenter.ts
- depends_on: none
- escalation_triggers: 需要真实生产验证、影响非 group_ai_chat 任务、修改部署脚本、引入 silent fallback、TG bot 权限无法证明

## 分级理由

这是跨 PRD、后端 schema/service/planner、Web 任务详情页、TG bot 入口、测试与数据流转索引的功能变更，属于 L2。当前没有线上事故输入，不进入 L3；也不是局部 quick_fix。

## 产品范围

- 新增 `topic_directions`：每个任务多个话题方向。
- 新增 `teacher_targets`：每个任务多个聊天对象老师。
- 新增 `consecutive_message_enabled/min/max/probability`：同账号 2-4 条连发模拟。
- Web 任务详情页可查看/设置。
- TG bot 内可选择任务、查看设置、保存设置。
- 执行器 action payload 必须保留 topic / teacher / burst 审计字段。

## 数据流转判断

- product_docs: `docs/01-product/tg-ops-platform-prd.md`, `docs/03-feature-designs/ai-group-topic-teacher-burst-prd.md`
- dataflow_index: updated
- affected_business_objects: Task, Action, ExecutionAttempt, RuntimeMetricSnapshot, WorkerHeartbeat
- affected_pages: TaskCenterView, TaskCenterDetailModal, TaskCenterWizardSections
- affected_api_or_worker_flows: `POST /api/tasks/group-ai-chat`, `PATCH /api/tasks/{task_id}/group-ai-chat`, TG bot settings/update route, group_ai_chat planner-worker

## Ready 条件

- PRD/专项设计/数据流转索引已给出产品口径。
- 验收标准已覆盖 schema、planner、Web、TG bot、QA 边界。
- `locked_paths` 已登记。
- 当前已有 dev draft 分支可供复核，但 dev 必须自行确认和修正，不能直接把草稿视为完成。

## Product Handoff To Dev

- message_id: 2026-06-28-ai-group-topic-teacher-burst-product-001
- intake_id: intake-ai-group-topic-teacher-burst-001
- batch_id:
- bug_id:
- from_agent: product
- to_agent: dev
- message_type: implement
- related_incident:
- related_version: ai-group-topic-teacher-burst-2026-06-28
- task_type: implement
- level: L2
- priority: P1
- evidence_level: E1
- cost_tier: standard_team
- created_at: 2026-06-28
- source_thread: product
- target_thread: 019f07c6-f550-73e3-998b-b130da2c1898
- reply_to_message_id:
- supersedes_message_id:
- idempotency_key: ai-group-topic-teacher-burst-2026-06-28
- expected_ack: true
- expected_ack_deadline:
- handoff_quality: complete
- status: new
- ready_status: ready
- release_gate: pending
- locked_paths: docs/01-product/tg-ops-platform-prd.md; docs/03-feature-designs/ai-group-topic-teacher-burst-prd.md; docs/00-index/project-dataflow-index.md; backend/app/schemas/task_center.py; backend/app/services/task_center; backend/app/api/routers; frontend/src/app/views/TaskCenter*; frontend/src/app/views/taskCenterViewModel.ts; frontend/src/app/types/taskCenter.ts
- merge_owner: dev
- depends_on:

### 背景

用户需要 AI 活群任务支持多个话题方向、多个聊天对象老师、同账号 2-4 条连发模拟，并要求 Web 详情页与 TG bot 内完整设置。

当前产品线程曾误直接实现，产生 `codex/ai-group-topic-teacher-burst` 分支草稿。该草稿只能作为 dev 输入，不能视为 dev 完成、QA 通过或产品接受。

### 本次要你做什么

请作为 dev Agent 接管实现：

1. 复核 PRD、专项设计、数据流转索引是否符合产品口径。
2. 检查当前 draft 分支实现，可复用则继续修正，不可复用则重做。
3. 完成后端 schema、配置白名单、planner、payload、TG bot 服务/路由、Web 设置入口。
4. 补齐自动化测试和必要的项目结构索引更新。
5. 输出 Development Complete 给 QA 和 product。

### 输入材料

- `docs/01-product/tg-ops-platform-prd.md`
- `docs/03-feature-designs/ai-group-topic-teacher-burst-prd.md`
- `docs/00-index/project-dataflow-index.md`
- `docs/00-index/project-structure-index.md`
- `backend/app/schemas/task_center.py`
- `backend/app/services/task_center/`
- `backend/app/api/routers/`
- `frontend/src/app/views/TaskCenterView.tsx`
- `frontend/src/app/views/TaskCenterDetailModal.tsx`
- `frontend/src/app/views/TaskCenterWizardSections.tsx`
- `frontend/src/app/views/taskCenterViewModel.ts`
- draft branch: `codex/ai-group-topic-teacher-burst`

### Ready 检查

- prd_or_scope_ready: true
- acceptance_ready: true
- dataflow_ready: true
- locked_paths_ready: true
- depends_on_ready: true

### 索引沉淀

- product_docs: updated
- dataflow_index: updated
- structure_index: dev must update if entrypoints/modules/API/worker/page flow changed
- affected_business_objects: Task, Action, ExecutionAttempt, RuntimeMetricSnapshot, WorkerHeartbeat
- affected_pages: TaskCenterView, TaskCenterDetailModal, TaskCenterWizardSections
- affected_api_or_worker_flows: group_ai_chat create/update, TG bot settings/update, planner-worker build_plan
- changed_entrypoints: dev to confirm
- changed_modules: dev to confirm
- changed_data_models: JSON type_config/action payload fields only unless dev determines migration needed
- index_updates: updated
- index_update_reason: group_ai_chat 数据流新增 topic/teacher/burst 配置和 TG bot 设置入口

### 必须遵守的边界

- 不访问生产环境。
- 不运行部署。
- 不把 draft branch 当完成。
- 不修改与 `group_ai_chat` 无关任务类型行为。
- 不引入 silent fallback、mock success 或吞错。
- TG bot 权限必须只允许租户 `admin_chat_id` 修改。
- 连发模拟不得突破 `messages_per_round`、硬小时目标、账号容量、目标权限、风控和内容质量过滤。

### 锁定范围

- locked_paths: 同上。
- must_not_touch: 生产部署脚本、真实生产环境配置、无关任务类型行为、无关 UI 大改。

### 完成标准

- 后端 create/update/settings 路径接受并校验新字段。
- 旧 `topic_hint` 兼容，列表为空时可作为回退。
- planner 每轮写入 `topic_direction`、`teacher_target`、`burst_id`、`burst_index`、`burst_size` 到 action payload。
- 连发开启且轮次足够时，同账号生成 2-4 条连续 action；关闭时保持旧分配逻辑。
- Web 详情页可查看和设置，创建/编辑表单同步支持。
- TG bot 可列任务、查看设置、保存设置，并复用后端校验。
- 自动化测试覆盖 schema、planner、Web build、TG bot 权限与保存。
- `project-structure-index.md` 按 dev 变更结论更新或说明 unchanged。

### 需要回传的内容

- Development Complete 固定格式。
- 修改文件清单。
- 新增字段/API/Bot 命令说明。
- 测试命令与结果。
- 索引更新结论。
- 未验证项和风险。
- 下一步投递 QA 的 handoff。

### ACK 规则

dev 必须先回复：

- `acknowledged`：输入完整且职责匹配。
- `missing_inputs`：列出缺口。
- `rejected`：说明应交给谁。

## QA Acceptance Draft

QA 后续按以下标准验收：

- 创建和更新 `group_ai_chat` 时接受新字段；非法空标题、非法权重、连发 min/max 越界失败。
- 旧任务只有 `topic_hint` 时仍可生成，并可在 payload 看到回退话题。
- 连发开启且轮次足够时，同一账号产生 2-4 条连续 action，带完整 burst 元数据。
- 连发关闭时保持原多账号分配逻辑。
- 连发不超过 `messages_per_round` 和硬小时目标预算。
- Web 详情页可展示、编辑、保存话题方向/老师/连发配置。
- TG bot 非 admin chat id 修改配置被拒绝。
- TG bot admin 可选择 AI 活群任务、查看设置、保存设置。
- QA pass 只代表功能验收，不代表生产恢复。

## Development Complete

- message_id: 2026-06-28-ai-group-topic-teacher-burst-devcomplete-001
- status: ready_for_qa
- changed_backend: schema/config whitelist/planner/payload/AI prompt/TG bot route/service
- changed_frontend: TaskCenterView create/edit payload、TaskCenterWizardSections 表单、TaskCenterDetailModal AI 设置展示
- changed_docs: PRD、专项设计、数据流转索引、项目结构索引、协作状态
- tests: no_postgres 定向后端测试 13 passed, 97 deselected；frontend `npm run build` 成功；`git diff --check` 成功
- unresolved: CI / release deploy / production verification unproven

## QA Acceptance

- message_id: 2026-06-28-ai-group-topic-teacher-burst-qa-001
- status: qa_pass
- evidence_level: E2
- evidence: schema、planner payload、同账号连发、TG bot admin 权限与保存、Web build 均有自动化或构建证据
- next_route: product_acceptance

## Product Acceptance

- message_id: 2026-06-28-ai-group-topic-teacher-burst-product-acceptance-001
- status: product_accepted
- evidence_level: E2
- decision: 接受本地功能范围，进入 release gate；未把 QA pass 写成生产恢复
- next_route: release_gate

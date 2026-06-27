# tg-yunying Agent Status Board

本表是多 Agent 共享状态，不是聊天摘要。AI 每次接到任务或完成阶段后，都应增量更新本表；没有足够信息时写 `unproven` 或 `blocked`，不要编造完成状态。

| message_id | intake_id | batch_id | bug_id | level | lane | owner_agent | current_agent | status | evidence_level | ready_status | done_status | next_agent | handoff_required | handoff_delivery_status | target_thread | handoff_message_id | ack_deadline | retry_count | supervisor_action | locked_paths | depends_on | sla_deadline | release_gate | last_update | blocking_reason |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-06-27-docs-practice-incident-001 | intake-docs-practice-001 |  |  | L0 | docs | product | prod-diagnosis | document_flow_verified | E1 | ready | done |  | false | not_required |  |  |  | 0 | none | docs/05-implementation/multi-agent-practice |  |  | not_required | 2026-06-27 |  |
| 2026-06-27-agent-protocol-upgrade-001 | intake-agent-protocol-upgrade-001 |  |  | L1 | docs | product | main | ready_for_review | E1 | ready | done |  | false | not_required |  |  |  | 0 | none | AGENTS.md; docs/05-implementation/multi-agent-practice |  |  | not_required | 2026-06-27 |  |
| 2026-06-27-agent-protocol-thread-sync-001 | intake-agent-protocol-upgrade-001 |  |  | L1 | docs | product | all-agents | acknowledged | E1 | ready | done |  | false | acknowledged | product/dev/qa/prod-diagnosis | 2026-06-27-agent-protocol-thread-sync-001 |  | 0 | none | AGENTS.md; docs/05-implementation/multi-agent-practice | 2026-06-27-agent-protocol-upgrade-001 |  | not_required | 2026-06-27 |  |
| 2026-06-28-ai-group-topic-teacher-burst-product-001 | intake-ai-group-topic-teacher-burst-001 |  |  | L2 | feature | product | qa | local_qa_pass_release_gate_pending | E3 | ready | qa_pass | product | false | not_required |  |  |  | 0 | none | docs/01-product/tg-ops-platform-prd.md; docs/03-feature-designs/ai-group-topic-teacher-burst-prd.md; docs/00-index/project-dataflow-index.md; docs/00-index/project-structure-index.md; backend/app/api/routers/system.py; backend/app/api/routers/telegram_bot.py; backend/app/models/tenants.py; backend/app/schemas/auth.py; backend/app/services/telegram_bot_settings.py; backend/app/services/tenant_bot_settings.py; backend/migrations/versions/0067_tenant_bot_settings.py; frontend/src/app/AppShell.tsx; frontend/src/app/views/SystemConfigView.tsx; frontend/src/app/views/TelegramBotSettingsView.tsx; frontend/src/app/views/TaskCenterDetailModal.tsx; frontend/src/app/views/TaskCenterView.tsx; frontend/src/app/types/system.ts |  |  | pending | 2026-06-28 | Local targeted pytest and frontend build passed; release gate still pending because CI/deploy/production evidence not recorded |
| 2026-06-28-bug-batch-product-acceptance-bb-p0-a-001 | intake-2026-06-27-bug-audit-001 | batch-2026-06-27-critical-high-remediation | BB-P0-A | L3 | duplicate-send-runtime | product | dev | product_accepted | E2 | ready | product_accepted | dev | true | sent | dev | 2026-06-28-bug-batch-bb-p0-a-release-gate-dev-001 | 2026-06-28 | 0 | monitor_release_gate_ack | backend/app/services/task_center/runtime_resources.py; backend/app/services/task_center/payloads.py; backend/app/services/task_center/membership_admission.py; backend/app/services/task_center/service.py; backend/app/services/task_center/stats.py; docs/00-index/project-dataflow-index.md; docs/00-index/project-structure-index.md | 2026-06-28-bug-batch-qa-bb-p0-a-001 |  | pending | 2026-06-28 | Release Gate pending; CI/E3, deployment, E4 production verification unproven |
| 2026-06-28-hard-hourly-min-10-001 | intake-hard-hourly-min-10-001 |  |  | L1 | hard-hourly-config | product | dev | product_accepted_release_ready | E2 | ready | product_accepted | dev | true | sent | dev | 2026-06-28-hard-hourly-min-10-release-gate-dev-001 | 2026-06-28 | 0 | monitor_release_gate_ack | backend/app/schemas/task_center.py; backend/migrations/versions/0066_ai_group_hard_target_10.py; backend/tests/test_ai_group_hard_hourly_target.py; frontend/src/app/views/taskCenterViewModel.ts; frontend/src/app/views/TaskCenterWizardSections.tsx; docs/01-product/tg-ops-platform-prd.md; docs/03-feature-designs/ai-group-hard-hourly-target-prd.md; docs/04-ops/ai-group-hard-hourly-target-ops.md; docs/00-index/project-structure-index.md |  |  | pending | 2026-06-28 | Release Gate pending; CI/deploy evidence not yet recorded |
| 2026-06-28-handoff-supervision-upgrade-001 | intake-handoff-supervision-upgrade-001 |  |  | L1 | docs | main | all-agents | protocol_updated_partial_ack | E1 | ready | not_done | dev | true | pending | dev | 2026-06-28-handoff-supervision-thread-sync-001 | 2026-06-28 | 0 | product_qa_prod_diagnosis_acknowledged_dev_pending | AGENTS.md; docs/05-implementation/multi-agent-practice |  |  | not_required | 2026-06-28 | product/qa/prod-diagnosis acknowledged; dev thread message sent but ACK still pending |

## 状态约束

- `ready_status` 只能是 `missing_inputs`、`ready`、`blocked`。
- `done_status` 只能是 `not_done`、`qa_pass`、`product_accepted`、`production_fixed`、`blocked`、`unproven`、`done`。
- L3 不能用 E0-E3 写 `production_fixed`。
- `blocked` 必须写 `blocking_reason` 和 `next_agent`。
- `failed` 必须回到 dev；`product_rejected` 必须回到 product/dev 重新定范围或修复。
- `release_gate=pending/failed/blocked` 的 L2/L3 任务不能关闭。
- 批量任务中单个 `bug_id` failed 时，必须剥离返工，不能拖住已通过项。
- `handoff_required=true` 时，`handoff_delivery_status` 必须是 `pending`、`sent`、`acknowledged`、`timeout`、`blocked` 之一。
- 当前阶段要写 `done_status=done/qa_pass/product_accepted/production_fixed`，其下游交接必须是 `sent`、`acknowledged` 或 `not_required`；不能在 `pending/timeout` 时宣称阶段闭环。
- `handoff_delivery_status=blocked` 必须写 `blocking_reason`、`target_thread`、完整待发送消息位置或正文。
- `retry_count` 每次由 `flow-supervisor` 重投时加 1；重投原因写入 `supervisor_action`。

## evidence_level

| level | 含义 |
| --- | --- |
| E0 | 口头描述或未验证假设 |
| E1 | 文档、截图、静态检查或本地只读证据 |
| E2 | 本地自动化测试或可复现本地证据 |
| E3 | CI、构建、预发或可重复集成证据 |
| E4 | 真实生产环境证据 |

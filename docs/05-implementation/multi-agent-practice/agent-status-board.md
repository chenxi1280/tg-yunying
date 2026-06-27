# tg-yunying Agent Status Board

本表是多 Agent 共享状态，不是聊天摘要。AI 每次接到任务或完成阶段后，都应增量更新本表；没有足够信息时写 `unproven` 或 `blocked`，不要编造完成状态。

| message_id | intake_id | batch_id | bug_id | level | lane | owner_agent | current_agent | status | evidence_level | ready_status | done_status | next_agent | locked_paths | depends_on | sla_deadline | release_gate | last_update | blocking_reason |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-06-27-docs-practice-incident-001 | intake-docs-practice-001 |  |  | L0 | docs | product | prod-diagnosis | document_flow_verified | E1 | ready | done |  | docs/05-implementation/multi-agent-practice |  |  | not_required | 2026-06-27 |  |
| 2026-06-27-agent-protocol-upgrade-001 | intake-agent-protocol-upgrade-001 |  |  | L1 | docs | product | main | ready_for_review | E1 | ready | done |  | AGENTS.md; docs/05-implementation/multi-agent-practice |  |  | not_required | 2026-06-27 |  |
| 2026-06-27-agent-protocol-thread-sync-001 | intake-agent-protocol-upgrade-001 |  |  | L1 | docs | product | all-agents | acknowledged | E1 | ready | done |  | AGENTS.md; docs/05-implementation/multi-agent-practice | 2026-06-27-agent-protocol-upgrade-001 |  | not_required | 2026-06-27 |  |

## 状态约束

- `ready_status` 只能是 `missing_inputs`、`ready`、`blocked`。
- `done_status` 只能是 `not_done`、`qa_pass`、`product_accepted`、`production_fixed`、`blocked`、`unproven`、`done`。
- L3 不能用 E0-E3 写 `production_fixed`。
- `blocked` 必须写 `blocking_reason` 和 `next_agent`。
- `failed` 必须回到 dev；`product_rejected` 必须回到 product/dev 重新定范围或修复。
- `release_gate=pending/failed/blocked` 的 L2/L3 任务不能关闭。
- 批量任务中单个 `bug_id` failed 时，必须剥离返工，不能拖住已通过项。

## evidence_level

| level | 含义 |
| --- | --- |
| E0 | 口头描述或未验证假设 |
| E1 | 文档、截图、静态检查或本地只读证据 |
| E2 | 本地自动化测试或可复现本地证据 |
| E3 | CI、构建、预发或可重复集成证据 |
| E4 | 真实生产环境证据 |

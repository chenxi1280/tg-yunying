# Agent Handoff Message Template

- message_id:
- intake_id:
- batch_id:
- bug_id:
- from_agent:
- to_agent:
- message_type: intake | triage | implement | verify | fix | recheck | product_acceptance | production_verify | resync | rule_backfill
- related_incident:
- related_version:
- task_type: incident | plan | implement | verify | fix | recheck | production_verify | postmortem
- level: L0 | L1 | L2 | L3
- priority: P0 | P1 | P2 | P3
- evidence_level: E0 | E1 | E2 | E3 | E4
- cost_tier: single_agent | light_agents | standard_team | full_team
- created_at:
- source_thread:
- target_thread:
- reply_to_message_id:
- supersedes_message_id:
- idempotency_key:
- expected_ack: true | false
- expected_ack_deadline:
- handoff_required: true | false
- handoff_delivery_status: pending | sent | acknowledged | timeout | blocked | not_required
- delivery_required: true | false
- target_thread_id:
- handoff_message_id:
- sent_at:
- ack_deadline:
- retry_count:
- requires_orchestrator_send: true | false
- orchestrator_send_reason:
- handoff_quality: complete | partial | missing_inputs
- status: new | acknowledged | in_progress | ready_for_validation | failed | blocked | unproven | production_fixed | production_failed | done
- ready_status: missing_inputs | ready | blocked
- release_gate: not_required | pending | passed | failed | blocked
- locked_paths:
- merge_owner:
- depends_on:

## 背景

## 本次要你做什么

## 输入材料

## Ready 检查

- prd_or_scope_ready:
- acceptance_ready:
- dataflow_ready:
- locked_paths_ready:
- depends_on_ready:

## 索引沉淀

- product_docs:
- dataflow_index:
- structure_index:
- affected_business_objects:
- affected_pages:
- affected_api_or_worker_flows:
- changed_entrypoints:
- changed_modules:
- changed_data_models:
- index_updates: updated | unchanged | unproven
- index_update_reason:

## 必须遵守的边界

## 锁定范围

- locked_paths:
- must_not_touch:

## 完成标准

## 需要回传的内容

## 下游真实投递

- next_agent:
- must_send_thread_message: true | false
- thread_message_sent: true | false
- target_thread:
- target_thread_id:
- sent_message_summary:
- handoff_delivery_status: pending | sent | acknowledged | timeout | blocked | not_required
- ack_expected: true | false
- ack_deadline:
- retry_count:
- requires_orchestrator_send: true | false
- blocked_reason:

只要 `next_agent` 不为空，本节必须填写。`notify_xxx: true`、`next_agent: xxx` 或“需要通知”不算投递完成；没有真实投递能力时必须把完整待发送消息写入本节，并设为 `requires_orchestrator_send=true`。

## ACK 规则

接收方必须先校验本消息：

- 输入完整且职责匹配：回复 `acknowledged`。
- 缺少必要输入：回复 `missing_inputs`，列出缺口。
- 职责不匹配：回复 `rejected`，说明应该交给谁。

# Development Complete Template

- message_id:
- intake_id:
- batch_id:
- bug_id:
- from_agent: dev
- to_agent: product, qa
- related_incident:
- related_version:
- reply_to_message_id:
- level: L0 | L1 | L2 | L3
- evidence_level: E1 | E2 | E3 | E4
- locked_paths:
- merge_owner:
- release_gate: not_required | pending | passed | failed | blocked
- status: ready_for_validation | blocked | partial
- next_agent: qa | product | none
- handoff_delivery_status: pending | sent | acknowledged | timeout | blocked | not_required

## 实现摘要

## 修改文件

## 代码索引沉淀

- structure_index: updated | unchanged | unproven
- dataflow_index: updated | unchanged | unproven
- changed_entrypoints:
- changed_modules:
- changed_data_models:
- changed_api_or_worker_flows:
- index_update_reason:

## 验证命令和结果

## 未验证 / 风险

## 请求验收的项目

## 下游投递

- notify_qa: true | false
- notify_product: true | false
- qa_validation_scope:
- qa_thread_id:
- qa_handoff_message_id:
- qa_handoff_sent_at:
- qa_ack_deadline:
- product_thread_id:
- product_notice_message_id:
- handoff_delivery_status: pending | sent | acknowledged | timeout | blocked | not_required
- requires_orchestrator_send: true | false
- blocked_reason:

`status=ready_for_validation` 时必须真实投递 QA。没有 `qa_handoff_message_id` 或 `handoff_delivery_status=sent/acknowledged` 时，开发阶段只能写 `handoff_pending` 或 `blocked`，不能写完成。

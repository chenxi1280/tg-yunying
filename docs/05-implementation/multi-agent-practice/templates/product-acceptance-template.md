# Product Acceptance Report Template

- message_id:
- intake_id:
- batch_id:
- bug_id:
- from_agent: product
- to_agent: dev, qa, prod-diagnosis
- reply_to_message_id:
- level: L0 | L1 | L2 | L3
- status: product_accepted | product_rejected | blocked | unproven
- evidence_level: E1 | E2 | E3 | E4
- production_verification_required: true | false
- next_agent: dev | qa | prod-diagnosis | none
- handoff_delivery_status: pending | sent | acknowledged | timeout | blocked | not_required

## 对照原始需求

## 产品范围检查

## 数据流转 / 索引检查

- product_docs:
- dataflow_index: updated | unchanged | unproven
- structure_index: updated | unchanged | unproven

## 接受项

## 拒绝项 / 需要返工

## 下一步

- notify_prod_diagnosis: true | false
- notify_dev_rework: true | false
- notify_release_gate_owner: true | false
- prod_diagnosis_thread_id:
- production_verify_message_id:
- dev_thread_id:
- dev_rework_or_release_message_id:
- qa_thread_id:
- qa_recheck_message_id:
- ack_deadline:
- handoff_delivery_status: pending | sent | acknowledged | timeout | blocked | not_required
- requires_orchestrator_send: true | false
- blocked_reason:

`product_accepted` 只表示产品验收通过；如果 `release_gate=pending` 或 `production_verification_required=true`，必须继续真实投递 dev/ops/prod-diagnosis。`product_rejected` 时必须真实投递 dev 返工。

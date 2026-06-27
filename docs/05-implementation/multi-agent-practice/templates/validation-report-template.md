# Validation Report Template

- message_id:
- intake_id:
- batch_id:
- bug_id:
- from_agent: qa
- to_agent: product, dev, prod-diagnosis
- related_incident:
- related_version:
- reply_to_message_id:
- level: L0 | L1 | L2 | L3
- evidence_level: E1 | E2 | E3 | E4
- status: qa_pass | failed | blocked | unproven
- validator: qa
- release_gate: not_required | pending | passed | failed | blocked
- next_agent: product | dev | prod-diagnosis | none
- handoff_delivery_status: pending | sent | acknowledged | timeout | blocked | not_required

## 通过项

## 不通过项

## 阻塞项

## 未证明项

## 需要开发 Agent 修复的问题

## 是否升级

- should_escalate: true | false
- new_level:
- escalation_reason:

## 下游投递

- notify_product_acceptance: true | false
- notify_dev_rework: true | false
- product_thread_id:
- product_acceptance_message_id:
- product_acceptance_sent_at:
- dev_thread_id:
- dev_rework_message_id:
- prod_diagnosis_thread_id:
- production_recheck_message_id:
- ack_deadline:
- handoff_delivery_status: pending | sent | acknowledged | timeout | blocked | not_required
- requires_orchestrator_send: true | false
- blocked_reason:

`status=qa_pass` 时必须真实投递 product 验收；`status=failed` 时必须真实投递 dev 返工。只写 `notify_product_acceptance=true` 或 `notify_dev_rework=true` 不算完成。

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

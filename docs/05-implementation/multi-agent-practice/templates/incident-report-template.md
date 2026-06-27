# Incident Report Template

- message_id:
- intake_id:
- from_agent: prod-diagnosis
- to_agent: product
- level: L2 | L3
- severity: P0 | P1 | P2 | P3
- evidence_level: E0 | E1 | E2 | E3 | E4
- status: new | reproduced | suspected_root_cause | needs_product_scope | blocked | unproven
- source:
- affected_scope:
- first_seen_at:
- evidence_links:
- related_thread:

## 现象

## 复现路径

## 线上证据

按 E0-E4 标注证据，不足以证明生产恢复的结论必须写 `unproven`。

## 影响范围

## 初步判断

## 建议分级

- suggested_level:
- production_related: true | false
- release_gate_required: true | false
- production_verification_required: true | false

## 建议产品 Agent 决策的问题

## 需要开发或验收补充的证据

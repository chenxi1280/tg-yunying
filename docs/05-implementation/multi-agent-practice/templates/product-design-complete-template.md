# Product Design Complete Template

- message_id:
- intake_id:
- from_agent: product
- to_agent: dev
- related_incident:
- related_version:
- level: L0 | L1 | L2 | L3
- design_status: complete | partial | blocked
- evidence_level: E0 | E1 | E2 | E3 | E4
- next_agent: dev | none
- handoff_delivery_status: pending | sent | acknowledged | timeout | blocked | not_required

## 原始需求覆盖矩阵

| user_requirement | product_decision | functional_design | frontend_design | backend_design | dataflow_design | qa_acceptance | status |
| --- | --- | --- | --- | --- | --- | --- | --- |

## 功能设计

- user_goals:
- entrypoints:
- happy_path:
- alternate_paths:
- error_states:
- permission_rules:
- state_machine:

## 前端设计

- affected_pages:
- components:
- form_fields:
- display_states:
- loading_empty_error_states:
- interaction_rules:
- validation_rules:

## 后端 / API / Worker 设计

- affected_api:
- affected_services:
- affected_workers:
- data_models:
- migrations:
- idempotency:
- concurrency:
- failure_handling:

## 数据流转设计

- source_data:
- transformations:
- storage:
- read_paths:
- write_paths:
- downstream_consumers:
- consistency_rules:
- dataflow_index_update: updated | unchanged | required | blocked

## QA 验收口径

- acceptance_cases:
- regression_scope:
- evidence_required:
- release_gate_required: true | false
- production_verification_required: true | false

## 深度自检

- uncovered_user_words:
- missed_scenarios:
- edge_cases:
- failure_modes:
- security_or_permission_risks:
- data_consistency_risks:
- release_or_migration_risks:
- rollback_considerations:
- open_questions:

## 设计结论

- design_status: complete | partial | blocked
- missing_inputs:
- cannot_handoff_reason:
- dev_handoff_ready: true | false

`design_status=partial/blocked` 或 `dev_handoff_ready=false` 时，不得投递 dev，也不得声明产品设计完成。用户在 product 线程要求“执行/实现/修复”时，product 只能输出本模板和 dev handoff，不能自行改代码。

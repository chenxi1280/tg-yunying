# Bug Batch Plan Template

- batch_id:
- intake_id:
- from_agent: product
- to_agent: dev, qa
- merge_owner:
- created_at:
- release_gate_required: true | false

## 批量问题列表

| bug_id | level | lane | summary | locked_paths | depends_on | can_parallel | route | escalation_triggers |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |

## Root Cause Grouping

- suspected_common_root:
- shared_api_or_worker:
- shared_state_machine:
- shared_dataflow:
- grouping_decision: split | merge | investigate_first

## 并行策略

## 合并顺序

## QA 验收顺序

## 失败剥离规则

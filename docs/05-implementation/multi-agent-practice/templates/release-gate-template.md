# Release Gate Template

- message_id:
- intake_id:
- from_agent: dev | ops
- to_agent: product, qa, prod-diagnosis
- level: L2 | L3
- release_mode: none | local_only | github_actions | manual_ops
- release_owner:
- rollback_owner:
- status: pending | passed | failed | blocked | not_required

## 上线范围

## 必须满足

- ci_or_build:
- backend_tests:
- frontend_build:
- migration_impact:
- worker_impact:
- external_platform_impact:
- rollback_plan:
- observe_window:

## 发布后复核

- production_probe:
- logs_or_actions:
- owner:

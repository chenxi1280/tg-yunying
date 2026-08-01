# 2026-08-01 Runtime Retention 审计引用清理

## Intake Card

- source: 用户要求删除可安全删除的线上遗留 Action，降低 `actions` 数量。
- level: L3。
- flow: `prod-diagnosis -> product -> dev -> qa -> product -> prod-diagnosis`。
- merge_owner: current-thread。
- locked_paths:
  - `docs/03-feature-designs/all-task-fulfillment-recovery-prd.md`
  - `docs/00-index/project-dataflow-index.md`
  - `docs/00-index/project-structure-index.md`
  - `backend/app/services/task_center/runtime_retention.py`
  - `backend/app/services/task_center/ai_content_scope_takeover_apply.py`
  - `backend/tests/test_runtime_retention_postgres.py`
  - takeover chain定向测试文件

## Prod Diagnosis

- 生产基线：2026-08-01 18:35 `actions=84,703`、terminal 81,399。
- 现行合同为保留5个完整自然日；当前到期候选0，不允许缩短保留期制造删除量。
- 下一自然日预计14,250条进入候选，实际执行以前序稳定查询为准。
- 新0134逐Action审计外键未进入runtime retention从属清理清单，会阻塞到期Action删除。

## Product Handoff

- design_status: complete。
- dev_handoff_ready: true。
- frontend/API: 无变化。
- candidate: 仅到期`success|failed|skipped`；open/unknown零删除。
- atomic order: 统计 -> 新旧从属明细/引用 -> Attempt/Review -> Action -> RuntimeCleanupAudit；同一事务。
- audit: 新增三类从属删除精确计数；保留takeover batch摘要/hash。
- safety: takeover item cardinality缺失时旧batch不能再次作为activation head。
- rollback: 可停cleanup/回滚代码；已按5天合同删除的runtime detail只能由数据库备份恢复。

## Stage Status

- prod-diagnosis: complete。
- product: complete。
- dev: complete；已补齐三类0134从属明细删除、精确审计和takeover item cardinality守卫。
- qa: in_progress；定向no-postgres 31项通过，完整PostgreSQL回归等待GitHub Actions测试库。
- product acceptance: pending。
- production E4: pending。

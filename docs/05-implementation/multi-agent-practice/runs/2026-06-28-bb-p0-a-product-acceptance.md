# 2026-06-28 BB-P0-A Product Acceptance

- message_id: 2026-06-28-bug-batch-product-acceptance-bb-p0-a-001
- intake_id: intake-2026-06-27-bug-audit-001
- batch_id: batch-2026-06-27-critical-high-remediation
- bug_id: BB-P0-A
- from_agent: product
- to_agent: dev, qa, prod-diagnosis
- reply_to_message_id: 2026-06-28-bug-batch-qa-to-product-bb-p0-a-001
- level: L3
- status: product_accepted
- evidence_level: E2
- production_verification_required: true

## 对照原始需求

BB-P0-A 目标是修复 duplicate-send-runtime 风险，避免任务中心在 runtime reservation、action dedupe、membership admission snapshot、unknown_after_send retry / recovery 场景中产生重复发送、重复准入或错误重排。

QA 已给出 E2 独立验收：

- action-owned runtime reservation：旧 action release 不会释放后续 holder 的 in-flight account reservation。
- action dedupe：AI 生成、上下文快照、素材等待等动态字段不参与去重，`message_text` 等业务身份字段仍参与去重。
- membership admission snapshot：nested transaction 捕获唯一键竞态，冲突后回读既有 item。
- `unknown_after_send`：不被 target admission 自动 retry 重排为 pending。
- recovery：existing unknown membership action 每轮按 account_id + channel_target_id + channel_id 限一次 probe。

产品判断：上述范围覆盖 BB-P0-A 的产品验收目标。

## 产品范围检查

- 接受项：duplicate-send-runtime 相关的 runtime reservation、action dedupe、membership admission snapshot、unknown_after_send retry/recovery。
- 不接受为线上恢复：QA pass 只证明本地 E2，不证明 CI、发布或生产修复。
- Release Gate：仍为 pending，L3 不关闭。

## 数据流转 / 索引检查

- product_docs: unchanged；本次是 bug remediation，产品状态机口径已在 PRD 中覆盖 unknown_after_send 不自动重复发送。
- dataflow_index: updated；`BG-004` / `BG-005` / `BG-007` 已覆盖 planner、dispatcher、recovery、runtime resource、dedupe 口径。
- structure_index: updated；`runtime_resources.py`、`payloads.py`、`service.py`、`stats.py` 等模块职责已覆盖。

## 接受项

- `product_accepted`
- evidence_level: E2
- done_status: product_accepted

## 拒绝项 / 需要返工

无产品范围返工项。

## 下一步

- notify_prod_diagnosis: false
- notify_dev_rework: false
- next_route: release_gate

Release Gate / CI / 部署通过后，必须再投递 prod-diagnosis 做 E4 production verification；未完成前不得写 `production_fixed`。

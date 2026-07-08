# 2026-07-08 硅谷 recovery CPU 背压修复

## Incident

- intake_id: intake-2026-07-08-sv-recovery-cpu-backpressure
- bug_id: bug-2026-07-08-sv-recovery-cpu-backpressure
- lane: task-center/recovery
- level: L3
- production_related: true
- release_gate_required: true
- production_verification_required: true

硅谷生产 CPU 持续升高。已复现的根因方向是 recovery 对历史 `unknown_after_send` membership 高频 Telegram 补偿复检，且 Telegram `TimeoutError` 会打断整轮 recovery 并留下 Telethon 后台 coroutine。

## Dev Complete

- message_id: 2026-07-08-sv-recovery-cpu-backpressure-devcomplete-001
- status: local_verified_pending_release

实现范围：

- `drain_task_recovery(limit=...)` 传递真实 recovery limit。
- stale executing 查询加批量上限。
- existing unknown membership 复检按 drain limit 与账号+目标去重。
- 单轮 Telegram reprobe 上限为 10。
- Telegram reprobe `TimeoutError` 写入 `telegram_probe_timeout`、`unknown_membership_reprobe_status=timeout`、`unknown_membership_reprobe_next_at`，并进入冷却。
- `TimeoutError` 不再抛出打断整轮 recovery。
- stale executing membership action 在 reprobe timeout 后退出 `executing`、清空旧 lease，并保留 `telegram_probe_timeout` 冷却字段，避免下一轮 recovery 继续被 expired lease 选中形成 tight loop。
- Telethon lifecycle 在 operation timeout 后取消 `future`，避免超时 coroutine 留在后台继续重连。

## QA Validation

- message_id: 2026-07-08-sv-recovery-cpu-backpressure-local-qa-001
- status: qa_pass
- evidence_level: E2
- release_gate: pending
- production_verification: unproven

QA 证据：

- 目标背压测试 `3 passed`，其中包含 QA 返工红测：stale executing membership timeout 后不得保留 `executing` 和旧 lease。
- 联合 Telethon lifecycle `12 passed`。
- worker/recovery 相关 `17 passed, 10 deselected`。
- 全量 no_postgres 60 秒门禁 `798 passed, 781 deselected, 5 warnings`。
- compileall passed。
- `git diff --check` passed。

QA 通过范围：

- `unknown_after_send` membership 只允许有界补偿复检。
- Telegram 探测超时必须写 `telegram_probe_timeout` 和下一次冷却时间。
- stale executing membership timeout 后必须退出 `executing` 并清空 lease，不能被下一轮 stale recovery 紧密重复扫描。
- 超时不能高频重试，不能打断整轮 recovery。
- Telethon timeout 不留下后台 coroutine 继续重连。

## Product Acceptance

- message_id: 2026-07-08-sv-recovery-cpu-backpressure-product-acceptance-001
- status: product_accepted
- evidence_level: E2_local_qa
- accepted_scope: local code fix + QA validation + PRD/dataflow sync
- release_gate: pending
- production_verification_required: true
- next_agent: dev
- handoff_delivery_status: sent
- target_thread: 019f07c6-f550-73e3-998b-b130da2c1898

产品接受项：

- 有界补偿复检符合产品口径，避免 historical `unknown_after_send` membership 反复拉起 Telegram probe。
- TimeoutError 显式落库为 `telegram_probe_timeout`，并写下一次冷却时间。
- Recovery 不能因单个 Telegram probe 超时而整轮失败。
- Telethon timeout 后清理后台 future，避免持续重连造成额外 CPU 背压。
- PRD 和数据流转索引已同步 recovery 经 Telegram Gateway 做有界补偿复检的口径。

产品未接受 / 未证明项：

- Release Gate 未完成。
- 新镜像未发布到硅谷生产。
- 硅谷 CPU / load 是否下降未取得 E4 证据。
- `tgyunying-worker-recovery` 是否停止高 CPU、高频 probe 和后台 coroutine 残留未取得生产证据。
- 不能写 `production_fixed`。

已真实投递 dev 做 Release Gate。Release Gate 通过并部署后，必须交回 prod-diagnosis 做 E4 production verification。

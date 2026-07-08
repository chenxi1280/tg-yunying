# 2026-07-08 硅谷 recovery CPU 背压修复

## Incident

- intake_id: intake-2026-07-08-sv-recovery-cpu-backpressure
- bug_id: bug-2026-07-08-sv-recovery-cpu-backpressure
- lane: task-center/recovery
- level: L3
- production_related: true
- release_gate_required: true
- production_verification_required: true

硅谷生产 CPU 持续升高。已复现的根因方向是 recovery 对历史 `unknown_after_send` membership 高频 Telegram 补偿复检，且 Telegram `TimeoutError` / `ConnectionError` 会打断整轮 recovery 并留下 Telethon 后台 coroutine。

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
- 生产复核发现同一路径还有 Telegram `ConnectionError` 分支；已追加 `telegram_probe_connection_error`、`unknown_membership_reprobe_status=connection_error` 和冷却字段，stale executing 路径同样退出 `executing` 并清空旧 lease。
- Telethon lifecycle 在 operation timeout 后取消 `future`，避免超时 coroutine 留在后台继续重连。

## QA Validation

- message_id: 2026-07-08-sv-recovery-cpu-backpressure-local-qa-001
- status: qa_pass
- evidence_level: E2
- release_gate: pending
- production_verification: unproven

QA 证据：

- 目标背压测试 `4 passed`，其中包含 QA 返工红测：stale executing membership timeout / connection error 后不得保留 `executing` 和旧 lease。
- 联合 Telethon lifecycle `12 passed`。
- worker/recovery 相关 `17 passed, 10 deselected`。
- 全量 no_postgres 60 秒门禁 `799 passed, 781 deselected, 5 warnings`。
- compileall passed。
- `git diff --check` passed。

QA 通过范围：

- `unknown_after_send` membership 只允许有界补偿复检。
- Telegram 探测超时必须写 `telegram_probe_timeout` 和下一次冷却时间。
- Telegram 探测连接失败必须写 `telegram_probe_connection_error` 和下一次冷却时间。
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

## Release Gate

- message_id: 2026-07-08-sv-recovery-cpu-backpressure-dev-release-gate-001
- status: release_gate_passed_deployed_pending_e4
- evidence_level: E3_release_deployed
- release_gate: passed
- release_candidate: `889e94635541bf937f4fc259f06435f7397fbc5e`
- deploy_run: `28921986236`
- deploy_url: https://github.com/chenxi1280/tg-yunying/actions/runs/28921986236

Release Gate 证据：

- 最新 release candidate `889e94635541bf937f4fc259f06435f7397fbc5e` 已推送 `master` / `release`。
- 最新 SHA 本地补跑：`compileall` passed；`backend/tests/test_task_recovery_backpressure.py` `3 passed`；`git diff --check` passed。
- GitHub Actions Deploy Production run `28921986236` success：checks、build-images、deploy 均通过；backend checks 与 frontend build 在 checks job 中通过；backend / frontend static image 均已 build and push；`Deploy via SSH release script` 通过。
- `28921986236` 发布后的首次生产 E4 未通过：新镜像落地后 recovery 仍有 `worker drain failed=5`，栈为 `ConnectionError: Connection to Telegram failed 5 time(s)`，并伴随 `Could not connect to proxy tgyunying-mihomo-024:7890`；因此追加连接失败冷却补丁，不能把 `889e9463` 写成 production_fixed。
- 连接失败返工本地门禁：`backend/tests/test_task_recovery_backpressure.py backend/tests/test_telethon_lifecycle.py` `13 passed`；全量 no_postgres `799 passed, 781 deselected, 5 warnings`；`compileall` passed；`git diff --check` passed。
- `f96dfa4e` 二次发布后 E4 仍未通过：`worker drain failed=0`，但 recovery CPU 回到约 97%，5 分钟内 `Task was destroyed but it is pending=796`、`Server closed=784`。进一步定位为 Telethon lifecycle 在 `client.connect()` 失败时没有断开新建 client，导致 auto-reconnect / send / recv coroutine 留在后台；已临时停止线上 `tgyunying-worker-recovery` 止血，未写数据库。
- Telethon lifecycle cleanup 本地门禁：`backend/tests/test_telethon_lifecycle.py` `10 passed`；`backend/tests/test_task_recovery_backpressure.py` `4 passed`；全量 no_postgres `800 passed, 781 deselected, 5 warnings`；`compileall` passed；`git diff --check` passed。
- 线上三次发布后 recovery 错误日志归零，但仍有周期性 CPU 峰值；DB 发现 running unknown membership 中 `failed` / cooldown 行仍会进入 SQL batch，`limit=10` 被旧行占满后真实待处理行饥饿。已补 SQL 层 `_unknown_membership_reprobe_due_clause`，查询 batch 时排除已 `failed` 和 cooldown 未到期行；本地门禁：`backend/tests/test_task_recovery_backpressure.py` `5 passed`；`backend/tests/test_telethon_lifecycle.py` `10 passed`；全量 no_postgres `801 passed, 781 deselected, 5 warnings`；`compileall` passed；`git diff --check` passed。
- `f8d8f703` 四次发布后 E4 仍未通过：backend/recovery 已换到 `f8d8f703` 且 healthy，`worker drain failed=0`、stale membership executing=0，但 recovery 10 分钟内仍有 `Server closed=471`，日志约每 0.45 秒一次；DB `unknown_membership_reprobe_status=''` 约 850，`failed_reprobe=0`。定位为 failed probe 的 Telethon client 留在 cache 中持续自动重连，且 `OperationResult(False)` 未保留 failure detail。已补 `probe_failure_invalidates_cached_client` 和 `test_recovery_marks_failed_probe_result_and_skips_next_round` 红测；本地门禁：定向 recovery/lifecycle/gateway `17 passed`；全量 no_postgres `803 passed, 781 deselected, 5 warnings`；`compileall` passed；`git diff --check` passed。待重新发布和 E4。
- `53b83635` 五次发布后 E4 仍未通过：backend/recovery 已换到 `53b83635` 且 healthy，公网 `/api/health` ok，`worker drain failed=0`、`Task was destroyed=0`、stale membership executing=0，但 recovery 5 分钟内 `Server closed=649`，DB `unknown_membership_reprobe_status=''` 约 863、`failed_reprobe=0`。进一步定位为 stale executing + gateway_started 分支在 probe `ok=False` 后，先写 failed result 又被 `_mark_stale_executing_action` 覆盖回普通 `unknown_after_send`，导致下一轮继续复检。已补 `test_stale_executing_membership_failed_probe_clears_lease_and_stops_reprobe` 红测并修复为清 lease 后保留 failed result；本地门禁：定向 recovery/lifecycle/gateway `18 passed`；全量 no_postgres `804 passed, 781 deselected, 5 warnings`；`compileall` passed；`git diff --check` passed。待第六次发布和 E4。
- 手动 workflow_dispatch run `28921792615` 在旧 commit `85b12f158406826c50dd37ba8725d94e53883913` 上 checks 通过后被取消，不作为发布证据。
- release push run `28921622212` 也因后续返工取消，不作为发布证据。
- 发布后只读公网 smoke：`https://tgyunying.telema.cn/api/health` HTTP 200，`https://tgyunying.telema.cn/task-center` HTTP 200。

## Production Verification Handoff

- message_id: 2026-07-08-sv-recovery-cpu-backpressure-dev-to-prod-diagnosis-e4-001
- from_agent: dev
- to_agent: prod-diagnosis
- target_thread: 019f07c6-92b5-7c50-b7e2-2f18a107e006
- handoff_delivery_status: sent
- production_verification_required: true

prod-diagnosis 需继续验证：

- 硅谷生产 CPU/load 是否下降。
- `tgyunying-worker-recovery` 是否不再因 historical `unknown_after_send` membership 高频 Telegram probe 拉高 CPU。
- `TimeoutError` 是否写入 `telegram_probe_timeout`、`unknown_membership_reprobe_status=timeout` 和冷却字段。
- Telethon 是否无超时后台 coroutine 残留。
- task-center recovery 是否继续处理其他项。

当前结论：Release Gate passed / deployed；production_fixed 仍 unproven，必须等待 prod-diagnosis E4。

# Malaysia Session DR v2.16 Implementation And Production Rollout

## Goal

实现 v2.16 MY standby_2 全链路，完成代码、数据库迁移、自动化测试、标准生产发布、2 个精确账号 canary 迁移与 Telegram/readback 验收；仅在 canary 全部通过且无 unknown 后，才允许冻结动态全量分母并开始全量迁移。

## Scope

- 文档真相源：v2.17 专项 PRD、实施合同、总 PRD、数据流索引。
- 代码：授权模型、不可变 wake bundle 双副本、MY SSH 镜像/inventory、operation 状态机、API/worker、账号详情读模型、迁移/恢复/擦除、指标和部署配置。
- 验证：单元/集成/迁移/并发/故障注入/前端构建，发布 SHA 与运行时 readback，2 账号 Telegram 真实 E4。
- 生产变更：精确 target IDs、preview/fingerprint、actor/approval ref、CAS apply、独立 readback；canary 未通过时不创建全量批次。

## Non-goals

- MY 不运行业务消息、listener、Planner、Dispatcher 或同步。
- 不复制现有 SV Session 到 MY，不申请第四套 Developer App，不新增第三个 Telegram 出口 IP。
- 不绕过验证码、2FA、FloodWait、48 小时设备撤销门槛或 unknown 对账。
- 不清理或改写当前工作区已有未提交文件和无关补丁。

## Phases

### Phase 1: Checkout, Runtime And Implementation Baseline

**Status:** completed

- 建立独立 codex worktree/branch，导入已完成的 v2.16 文档而不修改用户工作区。
- 读取当前模型、服务、API、worker、迁移、Compose、workflow 和生产访问合同。
- 核对远端 master/release、当前部署 SHA、MY 主机/SSH 镜像/固定 IP 配置是否存在。

### Phase 2: Schema And Durable Storage Core

**Status:** completed

- 实现 additive schema、logical slot/generation、bundle/copy/inventory/restore fact、operation、receipt 和约束。
- 实现不可变本地卷/对象副本、wrapped DEK/KMS adapter、digest/readback、MY inventory 和 prepared decision replay。
- 验证 migration up、空库/存量库兼容、不可变和幂等约束。

### Phase 3: Provision And Standby_2 Migration State Machine

**Status:** partial

- 实现 MY claim/login-input、真实登录、双副本、隔离 restore probe、slot CAS、recovery gate、retained/rollback window。
- 已完成不重登的失败冻结语义；unknown/orphan coordinator、中心库 restore hold/reconcile、48 小时 decommission、最终 exact-set、rollback-window close 和分步 erase 尚未实现。

### Phase 4: API, UI, Metrics And Cross-module Fences

**Status:** in_progress

- 实现授权/设备/operation/API、账号详情登录设备和 recoverability 状态。
- 实现 resolver/Gateway/current generation、在线/listener/sync stale-generation 屏障。
- 实现指标、告警、部署配置和运行说明。
- 2026-08-20 复核发现 Developer App 固定角色 API/UI、新账号默认角色、完整紧急唤起与跨模块 generation 屏障尚未完成；不得按完整 PRD 标记 completed。
- Developer App 固定角色 API/UI、新账号默认 `primary_sv` 和真实 KMS DEK 包装已补齐；`local_activate/restore_sv_pair/emergency_reauthorize_primary` 及其业务 generation fence 仍是未实现硬缺口。

### Phase 5: Migration-core Automated QA And Release Gate

**Status:** completed

- 定向与完整后端测试、前端构建、migration/Compose/workflow 检查。
- 故障注入覆盖 fsync/object/KMS/restore/CAS/DB rollback/partial erase。
- 形成 immutable candidate SHA 和 Release Gate，标准 `master -> release -> Deploy Production`。

### Phase 6: Production Read-only Preview And Two-account Slot Canary

**Status:** completed

- 读取生产部署 SHA、运行配置、A/B/C 映射、MY node/egress/SSH mirror storage readiness。
- 精确选择 2 个账号并冻结 ID/tenant/old state/fingerprint；必须不存在 open lease/operation/unknown。
- 通过正式 audited operation 迁移，逐账号 readback 本地+SSH 镜像双副本、恢复密钥、inventory、restore gate、slot、3+1 retained 和 Telegram exact set。
- 硬闸门：生产必须先读回 App A/B/C 角色映射、MY 固定出口、持续新鲜 heartbeat、create-only SSH 镜像、恢复密钥双机备份和运行中 authorization-dr worker。
- 生产账号 27/28 已读回 MY generation 2 current、双副本 2/2、restore probe passed、旧 SV retained+protected；`slot_canary_pass=2/2`。

### Phase 7: Expanded Migration Batch Reconciliation

**Status:** blocked

- 历史扩量批次已形成 `241 succeeded + 22 failed + 8 reconcile_unknown = 271`，runtime 已切 `off`。
- 在正式 unknown reconcile coordinator 完成并逐项收口 8 unknown 前不得恢复 claim 或继续扩量。
- 当前 `succeeded` 只表示 slot cutover succeeded；旧 SV decommission、最终 exact-set 与 rollback-window close 完成前不得写 migration final succeeded。

### Phase 8: Full PRD Runtime Recovery And Lifecycle

**Status:** pending

- 实现 `local_activate`、`restore_sv_pair`、`drill_wake`、`emergency_reauthorize_primary` 和跨 ExecutionAttempt/Gateway/online/listener/sync generation fence。
- 实现中心库旧备恢复协调器、两阶段账号删除、授权 decommission、分步 erase 与 unknown receipt。
- 完成 PostgreSQL、并发、故障注入、替代 MY 主机恢复、P2 10 账号运行恢复 canary 和任务类型 Telegram E4。

## Stop Conditions

- 生产 2 个账号身份、tenant、actor 或 approval ref 无法精确解析。
- MY SSH 镜像/独立 inventory/固定出口任一未配置或不可读回。
- 当前部署 SHA 与候选合同不一致，或 migration/runtime gate 未通过。
- canary 任一出现 unknown、AuthKeyDuplicated、保护 hash 缺失、双副本不足、restore probe 失败或 slot decision conflict。
- Telegram 登录需要未授权的人工作用、验证码/2FA 无受控输入，或触发权威等待。

## Success Criteria

- 本地与 CI gates 全通过，生产运行 exact SHA readback。
- 2/2 slot canary 账号：新 MY AuthKey/hash/generation 独立，recoverable_copy_count=2，恢复密钥/inventory/restore gate 通过，旧 SV retained+protected，Telegram exact set 完整，无业务从 MY 执行。
- 扩量批次 N 与 outcome counts 守恒，unknown 不伪装成功；slot cutover 与 migration final 使用不同终态。
- 生产状态、Telegram 授权结果和业务发送结果分层报告。

## Errors Encountered

| Error | Attempt | Resolution |
| --- | --- | --- |
| Deploy Production PostgreSQL gate failed because test setup had already created the current model schema and migration `0157` attempted to create `authorization_dr_runtime_contracts` again. | 1 | `0157` now detects the complete target schema and returns idempotently; added a metadata-create-all regression test and reran 270 related no-PostgreSQL tests. |
| Second Deploy Production gate passed migration execution but retained old `0156` head assertions and legacy device-cleanup/standby_2 test contracts. | 2 | Updated the Alembic head assertions, moved cleanup tests to the current SV executor + over-48h + idempotency contract, blocked SV `standby_2` with `manual_required`, and corrected all-failed cleanup outcome semantics. |
| Third PostgreSQL gate skipped all four updated cleanup executions even though their login age was 49 hours; SQLite did not reproduce it. | 3 | Found timezone-aware `telegram_login_at` receiving naive Beijing values under UTC PostgreSQL. SV and MY authorization creation now persist explicit Beijing-aware login timestamps; tests use the same cross-timezone contract. |
| Fourth PostgreSQL gate executed cleanup but failed when final readback replaced authorization snapshots still referenced by durable cleanup targets. | 4 | Cleanup targets now retain their own encrypted hash/digest while their snapshot reference is nullable with `ON DELETE SET NULL`; readback explicitly detaches those references before replacing snapshots. |
| Production shared `.env` contained the DR identity, but Compose did not pass it into the backend container. | 5 | Added both DR runtime variables to the shared backend environment anchor, added a regression test, and completed release run `32354502968`. |
| Direct Mac-to-MY SSH reached TCP/22 but timed out before the server banner. | 6 | Verified both MY keys, then configured and tested `ProxyJump prod-silicon-root`; root and admin aliases now reach the MY host through the existing Silicon Valley key path. |
| MY Docker credentials could not pull the private tg-yunying backend image; the first uncompressed SSH pipe was disconnected before `docker load` completed. | 7 | Built a 154 MB gzip archive on Silicon Valley, recorded SHA-256, forwarded only an ephemeral SSH agent signing capability, and used resumable server-to-server rsync. MY SHA matched before `docker load`; no private key or Session file was copied. |
| 本地 `test_workflow.py` Developer App API 用例需要 PostgreSQL，但当前 shell 未配置 `TEST_DATABASE_URL`。 | 8 | 未绕过数据库闸门；SQLite 定向合同、compileall 和前端构建在本地执行，真实 PostgreSQL 交给标准 GitHub Actions gate。 |
| 首次 compileall 从仓库根目录扫描 `app/tests`，输出 `Can't list` 且未形成有效证明。 | 9 | 改到 `backend/` 目录重跑 `python -m compileall -q app tests`，成功且无输出。 |

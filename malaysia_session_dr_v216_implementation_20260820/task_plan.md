# Malaysia Session DR v2.16 Implementation And Production Rollout

## Goal

实现 v2.16 MY standby_2 全链路，完成代码、数据库迁移、自动化测试、标准生产发布、2 个精确账号 canary 迁移与 Telegram/readback 验收；仅在 canary 全部通过且无 unknown 后，才允许冻结动态全量分母并开始全量迁移。

## Scope

- 文档真相源：v2.16 专项 PRD、实施合同、总 PRD、数据流索引。
- 代码：授权模型、不可变 wake bundle 双副本、MY KMS/对象存储/inventory、operation 状态机、API/worker、账号详情读模型、迁移/恢复/擦除、指标和部署配置。
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
- 核对远端 master/release、当前部署 SHA、MY 主机/KMS/对象存储/固定 IP 配置是否存在。

### Phase 2: Schema And Durable Storage Core

**Status:** completed

- 实现 additive schema、logical slot/generation、bundle/copy/inventory/restore fact、operation、receipt 和约束。
- 实现不可变本地卷/对象副本、wrapped DEK/KMS adapter、digest/readback、MY inventory 和 prepared decision replay。
- 验证 migration up、空库/存量库兼容、不可变和幂等约束。

### Phase 3: Provision, Migration And Recovery State Machines

**Status:** completed

- 实现 MY claim/login-input、真实登录、双副本、隔离 restore probe、slot CAS、recovery gate、retained/rollback window。
- 实现中心库 restore hold/reconcile、单副本修复、不重登和分步 erase。
- 实现 48 小时 decommission、unknown/orphan/exact-set 对账。

### Phase 4: API, UI, Metrics And Cross-module Fences

**Status:** completed

- 实现授权/设备/operation/API、账号详情登录设备和 recoverability 状态。
- 实现 resolver/Gateway/current generation、在线/listener/sync stale-generation 屏障。
- 实现指标、告警、部署配置和运行说明。

### Phase 5: Automated QA And Release Gate

**Status:** in_progress

- 定向与完整后端测试、前端构建、migration/Compose/workflow 检查。
- 故障注入覆盖 fsync/object/KMS/restore/CAS/DB rollback/partial erase。
- 形成 immutable candidate SHA 和 Release Gate，标准 `master -> release -> Deploy Production`。

### Phase 6: Production Read-only Preview And Two-account Canary

**Status:** pending

- 读取生产部署 SHA、运行配置、A/B/C 映射、MY node/egress/KMS/object storage readiness。
- 精确选择 2 个账号并冻结 ID/tenant/old state/fingerprint；必须不存在 open lease/operation/unknown。
- 通过正式 audited operation 迁移，逐账号 readback 双副本/KMS/inventory/restore gate/slot/3+1 retained 和 Telegram exact set。

### Phase 7: Canary Acceptance And Full Rollout

**Status:** pending

- canary 两个账号均 remote_effect_verified 且观察窗无 AuthKeyDuplicated/FloodWait/unknown/副本降级后，创建全量 preview。
- 冻结动态 N/fingerprint，逐账号串行迁移；持续对账 outcome counts，不因失败缩分母。
- 全量完成后独立 readback，区分 persisted、Telegram authorization 和业务 runtime。

## Stop Conditions

- 生产 2 个账号身份、tenant、actor 或 approval ref 无法精确解析。
- MY KMS/对象存储/独立 inventory/固定出口任一未配置或不可读回。
- 当前部署 SHA 与候选合同不一致，或 migration/runtime gate 未通过。
- canary 任一出现 unknown、AuthKeyDuplicated、保护 hash 缺失、双副本不足、restore probe 失败或 slot decision conflict。
- Telegram 登录需要未授权的人工作用、验证码/2FA 无受控输入，或触发权威等待。

## Success Criteria

- 本地与 CI gates 全通过，生产运行 exact SHA readback。
- 2/2 canary 账号：新 MY AuthKey/hash/generation 独立，recoverable_copy_count=2，KMS/inventory/restore gate 通过，旧 SV retained+protected，Telegram exact set 完整，无业务从 MY 执行。
- 全量只有在 2/2 canary 通过后开始；最终 N 与 outcome counts 守恒，unknown 不伪装成功。
- 生产状态、Telegram 授权结果和业务发送结果分层报告。

## Errors Encountered

| Error | Attempt | Resolution |
| --- | --- | --- |
| Deploy Production PostgreSQL gate failed because test setup had already created the current model schema and migration `0157` attempted to create `authorization_dr_runtime_contracts` again. | 1 | `0157` now detects the complete target schema and returns idempotently; added a metadata-create-all regression test and reran 270 related no-PostgreSQL tests. |
| Second Deploy Production gate passed migration execution but retained old `0156` head assertions and legacy device-cleanup/standby_2 test contracts. | 2 | Updated the Alembic head assertions, moved cleanup tests to the current SV executor + over-48h + idempotency contract, blocked SV `standby_2` with `manual_required`, and corrected all-failed cleanup outcome semantics. |
| Third PostgreSQL gate skipped all four updated cleanup executions even though their login age was 49 hours; SQLite did not reproduce it. | 3 | Found timezone-aware `telegram_login_at` receiving naive Beijing values under UTC PostgreSQL. SV and MY authorization creation now persist explicit Beijing-aware login timestamps; tests use the same cross-timezone contract. |
| Fourth PostgreSQL gate executed cleanup but failed when final readback replaced authorization snapshots still referenced by durable cleanup targets. | 4 | Cleanup targets now retain their own encrypted hash/digest while their snapshot reference is nullable with `ON DELETE SET NULL`; readback explicitly detaches those references before replacing snapshots. |

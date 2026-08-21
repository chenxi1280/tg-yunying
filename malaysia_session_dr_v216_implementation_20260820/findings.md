# Findings

## 2026-08-21 Current Truth

- 文档中的账号 24/25 `0/2_reconcile_unknown` 是早期失败 canary，不是当前两账号验收结果；后续账号 27/28 已达到 slot-level `2/2 pass`。
- 当前数据库 operation 的 `succeeded` 发生在 recovery gate + slot CAS 后，语义是 `slot_cutover_succeeded`；旧 SV 远端授权未 decommission、最终 exact-set 未回读且 `rollback_window_closed_at` 未写时，不能解释为 `migration_succeeded`。
- 账号 26 的 unknown operation 已有同 operation、generation 2 的 MY 本地密封包，但无 SV 镜像、中心 inventory/receipt；它是 `local_only`，不得再写成“无 Bundle”，也不得自动重登。
- 最新扩量批次 271 项数量守恒为 `241 succeeded + 22 failed + 5 manual_required + 3 reconcile_unknown`；全局剩余 unknown 精确为账号 24/25/26/67/87/111，runtime `off` 是当前正确生产停止态。
- typed historical failure 的正式 unknown coordinator 已发布并完成 5 项受控 apply/readback；local-only/dual-copy/orphan 续跑入口仍未实现。完整 `local_activate`、`restore_sv_pair`、`emergency_reauthorize_primary`、中心恢复、decommission/erase 和跨业务 generation fence 也仍缺失。
- release SHA `e8cd88dc496a56c586a5bcc502d81a318b76d7a9`、run `32456712129`、Alembic `0158_dr_reconcile`、SV/MY 同 image ID 与 MY 精确运行 SHA 均已独立读回；这证明 P0A 已部署，不等于完整 PRD 或迁移最终完成。
- `.planning/malaysia_session_dr_v216_implementation_20260820` 是早期 ignored 快照；本目录为 tracked 规划真相源，旧快照不得用于当前完成度判断。

## Initial Baseline

- 当前 checkout 为 `release@63b7c0071fe56201d3ed1ee9062c4d0c03115b89`，存在用户未提交的 PRD、索引、`.planning/.active_plan` 和四个 patch 文件。
- v2.16 两份 MY 专项文档当前为 untracked；实现分支必须包含它们及关联真相源，不能只提交代码。
- 当前生产发布合同仍是 `master -> release -> GitHub Actions Deploy Production`；生产运行面在硅谷 `/data/tgyunying`。
- 生产变更必须使用精确账号 ID、preview/fingerprint、actor/approval ref、CAS apply 和独立 readback；2 账号 canary 未完成前禁止全量迁移。
- 代码只读检索尚未发现 wake bundle/copy/inventory/restore gate 实现，需从 additive schema 开始。

## Risks To Resolve

- 独立开发 worktree需导入当前 dirty 文档但不能带入无关 patch 或覆盖 release checkout。
- 真实 MY 服务器、固定出口、对象存储和 KMS 配置是否存在尚未确认。
- 2 个生产账号不能凭“看起来健康”选取，必须从生产 preview 精确冻结，并避开 open operation/unknown/人工验证码 blocker。

## 2026-08-20 Production Read-only Findings

- Silicon Valley production is running SHA `63b7c0071fe56201d3ed1ee9062c4d0c03115b89`; backend and workers are healthy, but this is not migration proof.
- Production has three active Developer Apps and 1335 non-deleted accounts.
- App C/SV healthy standby_2 candidates total 391. Of these, 276 have App A current primary plus App B healthy standby_1; 115 have App B current primary plus an App A standby_repair Session and no healthy standby_1.
- Canary IDs 24 and 25 are online members of the 276-account double-SV-ready cohort. They are candidates only; no production migration batch has been created or approved yet.
- MY host TCP/22 responds, but SSH stalls during banner exchange for both configured admin/root identities. No MY deployment or host mutation has been performed.
- GitHub currently has no MY host, OSS, KMS/KEK or DR internal identity secrets. A Silicon-only release cannot establish MY durability by itself.

## 2026-08-20 First Release Gate Finding

- Feature PR #58 and release PR #59 merged; the candidate reached Deploy Production run `32345159607` at release SHA `3f194aecd72ed05ff5b859871dcf7ee66ee2c1a2`.
- The PostgreSQL job failed before image build/deploy with `DuplicateTable` on `authorization_dr_runtime_contracts`; production therefore remained on `63b7c0071fe56201d3ed1ee9062c4d0c03115b89` and no migration state was created.
- Root cause is the CI compatibility path: current `Base.metadata.create_all` already materializes the target schema before Alembic replays `0157`. The migration now treats an independently verified complete target schema as already applied, while a genuinely old production schema still executes the additive migration.

## 2026-08-20 Second Release Gate Finding

- Release SHA `c1deef70591440bcecf0e3236ebc82adda99367a` reached Deploy Production run `32345671294`; frontend passed and both backend matrices completed, but image build and deployment remained skipped.
- PostgreSQL migration execution passed. Remaining failures were stale assertions that the migration head was still `0156`, plus legacy account-security tests that omitted the v2 cleanup executor/login age/idempotency contract or still expected SV to create `standby_2`.
- The current product contract is explicit: device cleanup is standalone and keeps only recorded protected platform authorization hashes; every other active authorization, including an unrecorded official client anchor, is a cleanup target. `standby_2` creation is exclusive to the MY DR migration flow.

## 2026-08-20 Third Release Gate Finding

- Release SHA `a25d64c62f065e29d80ed0668b8794c39bb206f2` reached run `32347088271`; frontend and all 3330 no-PostgreSQL tests passed. The PostgreSQL matrix had only four failures, all showing zero drainable cleanup items.
- Under PostgreSQL UTC, a naive Beijing timestamp written to `TIMESTAMP WITH TIME ZONE` was interpreted as UTC and read back eight hours later. A 49-hour test login therefore appeared only 41 hours old and was correctly skipped by the 48-hour business rule for the wrong timestamp reason.
- New SV and MY authorization creation now writes an explicitly timezone-aware Beijing login instant. This keeps eligibility conservative and stable regardless of the PostgreSQL session timezone.

## 2026-08-20 Fourth Release Gate Finding

- Release SHA `bd386ef95c22fc477166d6b4613c8a953b1b1d29` reached run `32348177594`; frontend and no-PostgreSQL passed again. PostgreSQL advanced all four cleanup tests into execution, confirming the login-time correction.
- Final readback deletes and recreates remote authorization snapshots. Durable cleanup targets referenced the pre-effect snapshot with a non-null foreign key, so PostgreSQL rejected the delete while SQLite tests did not enforce the same boundary.
- The target already stores its own encrypted authorization hash and digest. Its snapshot reference is now an optional provenance pointer: final readback clears the pointer, retains the target/result evidence, then replaces the live snapshot set.

## 2026-08-20 Production And Malaysia Runtime Readback

- Deploy Production run `32349179289` first completed the full v2.16 release at SHA `e8cbfcfa2a545d047315d90d18a2a4863d5c9f33`; migration head, five DR tables and authorization columns were independently read back. Runtime contract and migration batch counts remained zero.
- A production wiring gap then surfaced: `AUTHORIZATION_DR_INTERNAL_TOKEN` and `AUTHORIZATION_DR_REQUIRE_MTLS` existed in application settings but not in the shared Compose environment anchor. PR #68/#69 fixed the contract, and run `32354502968` fully passed and deployed SHA `3b81db2f2abc3ad492df5b503a011cff8391ae2a`.
- The Malaysia host is an Alibaba Cloud lightweight application server, not ECS. It is running with the expected static egress and has enough Docker, disk and memory capacity. Existing tgmsg production also uses this host, `/data/tgmsg` release directories, and dedicated root/admin/GitHub Actions authorized keys.
- Direct Mac-to-MY SSH stalls before the banner while Silicon Valley-to-MY returns `OpenSSH_8.0` immediately. Both MY local keys are valid. The local aliases now use `ProxyJump prod-silicon-root`, and both root and admin logins have been read back successfully.
- The Silicon Valley backend now receives the DR identity at runtime and Nginx proxies the internal DR path. From MY, a heartbeat without the token returns 401; with the shared token it returns 200 and persists `my-node-1` as ready with zero active clients.
- MY has the deployment scripts, persistent wake directory and verified backend image, but the worker remains intentionally stopped. The cloud account has only one Chengdu OSS bucket with versioning disabled, no MY OSS runtime credentials, and no instance RAM role. Creating the MY bucket and dedicated AccessKey is the remaining prerequisite before any Session migration batch may exist.

## 2026-08-20 Full PRD Completion Audit

- Production has three healthy Developer Apps, but runtime contract, App role assignments, MY egress assignments, migration batches, operations, wake bundles and copies are all zero.
- MY `/opt/tgyunying-authorization-dr/node.env` does not exist and no authorization-dr worker container is running. The stored manual heartbeat is stale and cannot satisfy the 120-second readiness gate.
- Developer App credentials remain centrally managed as intended, but the App C -> `standby_2_my` mapping is CLI-only and has not been applied. The Developer App API/UI does not expose role or distinct-account capacity, and new account assignment still uses legacy round-robin behavior.
- The account-detail security view and direct no-precheck 48-hour cleanup path exist, but the dedicated authorization-device query/refresh contract and complete login-age display remain partial.
- The worker now wraps each bundle DEK through Alibaba Cloud KMS and records the returned ciphertext blob/key version; restore probes decrypt through KMS with the same encryption context. Production still requires an independently readable Malaysia KMS key and dedicated credentials before this can become a runtime fact.
- Current code migrates only existing healthy App C/SV `standby_2` sources. It does not provision missing MY standby authorization for the remainder of the 1335-account population.
- Full `local_activate`, `restore_sv_pair`, `emergency_reauthorize_primary`, drill, decommission and generic send/listener/sync generation fencing are not implemented. Core canary migration release is not full-PRD completion.

## 2026-08-20 Developer App Role And Exact MY Image Readback

- Release run `32361582241` passed all CI/build/deploy jobs and production now runs exact release SHA `ab85ae2369f993d3bcfc602dc8bae49f75f850ef`; runtime OpenAPI includes `PUT /api/developer-apps/slot-assignments`.
- A preview/fingerprint/CAS apply established the requested fixed topology: App 1 is Silicon Valley primary, App 2 is Silicon Valley standby_1, and App 3 is Malaysia standby_2. The runtime contract remains `off`, so this configuration cannot create migration operations yet.
- Production readback after the role change still reports zero migration batches, operations, wake bundles and copies. No existing Session has been removed, replaced or migrated.
- MY cannot pull the private GHCR image anonymously. The exact production image was archived on SV, hashed, transferred directly to MY with resumable rsync and ephemeral agent forwarding, verified again, then loaded. Existing tgmsg and other MY containers were not restarted or replaced.
- MY-to-control-plane DNS/TLS/routing is proven by an unauthenticated heartbeat receiving 401. Production has the internal token and explicitly disables mTLS for this phase; `node.env` can therefore omit client certificate paths.
- The remaining runtime blocker is not SSH connectivity. The Alibaba account currently has no Malaysia OSS bucket, no Malaysia KMS software instance/user key, no dedicated RAM credentials, and no instance role. The minimum observed KMS software instance purchase is about CNY 4,998 per month, so purchase requires explicit cost approval or an explicit PRD change to a non-KMS recovery-key design.

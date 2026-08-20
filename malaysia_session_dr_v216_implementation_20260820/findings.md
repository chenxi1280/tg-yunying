# Findings

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

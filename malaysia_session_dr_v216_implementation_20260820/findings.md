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

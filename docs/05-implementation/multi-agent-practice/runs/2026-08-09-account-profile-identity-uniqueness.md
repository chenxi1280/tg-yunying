# 2026-08-09 账号昵称唯一性与头像素材治理

## Intake

- `intake_id`: `intake-2026-08-09-account-profile-identity-uniqueness`
- `level`: `L3`
- `source`: user
- `scope`: 存量重复昵称、后续普通账号注册/资料更新唯一性、昵称随机化、许可头像素材补充
- `production_mutation`: 需要，但本记录建立时尚未 apply

## Product Design Complete

- 专项 PRD：`docs/03-feature-designs/account-profile-identity-uniqueness-prd.md`
- 总 PRD、账号安全设计、数据流和结构索引均已同步。
- 普通运营账号按 `tenant_id + normalized name_key` 唯一；接码/降权用途不进入资料自动化。
- 存量改名和头像导入分别使用独立 preview/apply/readback，不能互相代替成功。

## 生产只读基线

2026-08-09 对 tenant 1 active 普通运营账号只读聚合：

- 账号：892
- 重复组：49
- 重复组账号：533
- 每组保留一个后的改名目标：484
- 已审核、上传、TG cache ready 图片：295
- 上述图片的不同素材指纹：295

该基线只用于发现；apply 前必须以已部署 SHA 重新生成 manifest 和 canonical SHA-256。

## Development Complete

- `TgAccountProfileNameClaim` + `0143_account_profile_name_claims.py`：数据库唯一占名和 keeper backfill。
- `account_profile_identity.py`：NFKC/零宽/空白/casefold 规范化、9 类随机模板、全租户不可用集合和禁用词过滤。
- 注册、资料确认、手工改名、远端同步统一走 claim；普通账号 Telegram first name 固定等于 display name，last name 置空。
- `account_profile_duplicate_reconcile.py`：精确重复组、keeper、全目标预检、分批旧值复核、manifest SHA、重复 apply 批次复用及逐账号 Telegram profile 回读。
- 删除历史 `account_profile_half_rename.py` 及其“改一半”workflow 入口。
- `AvatarMaterialSource` + `0144_avatar_material_sources.py`：来源、许可、署名、内容 SHA、感知哈希。
- `account_avatar_material_import.py`：固定 17 个 Commons 非真人候选，显式节流、无重定向、限大小和 preview/apply/readback。

## QA

- 账号安全、唯一性、迁移、头像、workflow 上限、apply 幂等及远端回读定向回归：65 passed、44 deselected。
- 旧素材上传路径测试需要 PostgreSQL fixture，本地无 `TEST_DATABASE_URL`，blocked。
- 新增 PostgreSQL 并发争名测试，等待 CI 的 PostgreSQL 分区执行。
- compileall、workflow YAML、Alembic single-head、`git diff --check`：passed。
- 首次 release workflow 解析失败，根因是 `workflow_dispatch` 输入超过 25 上限；拆分后 CI 正常创建 jobs。
- 拆分后的 CI 完整 `no_postgres`：2779 passed、783 deselected；前端正式构建通过。
- PostgreSQL 分区：764 passed、14 skipped、2 xfailed、3 failed；失败均为保留现有 display name 且 TG first name 为空时未预先 claim 最终实际发送名，已补红测和修复，等待同 SHA CI 重跑。
- 17 个 Commons 候选真实下载：17/17 可解码，许可/署名完整，候选间无 SHA/感知哈希冲突。

## Release Gate

- `release_mode`: `github_actions`
- `release_path`: `master -> release -> Deploy Production`；生产数据操作另走 `Production Account Profile Identity Operations`
- `migration_impact`: 新增 0143/0144；0143 只 backfill 普通账号当前昵称 keeper，不改 Telegram。
- `worker_impact`: account-security worker 在 Gateway 前校验 claim；material-cache 负责新素材缓存。
- `external_platform_impact`: 发布本身不改 Telegram；后续账号改名批次才调用 Telegram。
- `rollback_plan`: 新表和 claim 保留；停止新批次，不恢复旧重复名。迁移应用后应用降级安全性未证明，不执行猜测回滚。
- `status`: `postgres_final_name_claim_fix_pending_ci`

## 当前结论

- `local_qa`: pass for focused scope
- `postgres_concurrency`: passed in CI collection; full partition still failed on three adjacent account-security assertions
- `release`: failed before build/deploy; follow-up fix pending
- `production_name_apply`: not_started
- `production_avatar_apply`: not_started
- `telegram_readback`: unproven
- `production_fixed`: false

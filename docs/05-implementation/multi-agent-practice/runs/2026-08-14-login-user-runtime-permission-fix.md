# 2026-08-14 登录账号权限首屏加载修复

## Incident / Intake

- 影响对象：平台登录账号 `useradd03`，不是 Telegram 账号。
- 线上首个失败边界：账号添加专员刷新页面时，前端无条件请求 `GET /api/config/runtime`；后端按既有 `system.view` 权限正确返回 403，页面误显示“后端未连接或接口异常：permission denied”。
- 分级：L3。该问题影响生产登录入口，但不修改 Telegram Session、验证码、授权资产或账号数据。

## Product Design Complete

- 保留 `/api/config/runtime -> system.view` 和越权审计，不为账号添加专员扩大系统权限。
- 新增 `GET /api/tg-accounts/creation-capability -> accounts.create`，响应仅为 `can_create_tg_account`。
- 前端先读取 `/api/auth/me`：只有 `system.view` 用户读取完整运行时配置；只有 `accounts.create` 用户读取账号创建能力投影。
- 无系统设置权限且没有可用开发者应用时，账号页提示联系管理员，不跳转到无权限的系统设置页。

## Release Gate

- candidate SHA：待提交。
- migration impact：无。
- worker / Telegram impact：无；只读开发者应用健康计数。
- local checks：Python 编译通过；`test_permission_vocabulary.py` 12 passed；`test_frontend_permission_gating.py` 154 passed；前端 `npm run build` 通过；`git diff --check` 通过。
- PostgreSQL integration：本地未配置 `TEST_DATABASE_URL`，`test_workflow.py::test_admin_users_permission_lifecycle_and_legacy_subscription_endpoints_removed` 未运行；`Deploy Production` 的 PostgreSQL 服务矩阵必须作为 CI 门禁。
- rollback：发布失败或生产验证失败时，按 `Deploy Production` 当前 release symlink 回退到上一不可变 release；本次没有 schema migration 或数据修复。

## Production Verification Contract

1. Actions 的 backend-checks（含 PostgreSQL）、frontend-checks、镜像构建和 deploy 全部成功。
2. 部署 SHA 与候选 SHA 一致；backend、worker 和 `/api/health` 正常。
3. `useradd03` 的权限仍不包含 `system.view`，但包含 `accounts.create`。
4. 以该平台登录账号刷新账号页后，不再产生 `GET /api/config/runtime missing=system.view` 审计；账号创建能力接口返回 200 且只包含 `can_create_tg_account`。
5. 只有以上事实均完成后，才可写 `production_fixed`。

## Current Status

`release_pending`。本记录不把本地构建或接口单测视为生产恢复证据。

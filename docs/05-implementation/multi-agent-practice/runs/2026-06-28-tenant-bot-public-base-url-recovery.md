# 2026-06-28 Tenant TG Bot Public Base URL Recovery

## Incident Report

- message_id: 2026-06-28-tenant-bot-public-base-url-incident-001
- intake_id: intake-2026-06-28-tenant-bot-public-base-url-001
- from_agent: prod-diagnosis
- to_agent: product
- level: L3
- severity: P1
- evidence_level: E4
- status: root_cause_found
- source: user reported online TG bot token/admin user id configured but bot did not respond
- affected_scope: tenant TG Bot webhook registration and inbound `/start` / `/admin` / `/ai_group` handling
- first_seen_at: 2026-06-28
- evidence_links: production API `GET /api/tenant-bot-settings`; production `POST /api/tenant-bot-settings/webhook/refresh`
- related_thread: current Codex thread

## 现象

线上租户 TG Bot 已配置 token、管理员 Chat ID 和 AI 活群 Bot 开关，但 Telegram 私聊机器人没有响应。

## 线上证据

- E4：`GET /api/tenant-bot-settings` 返回 `telegram_bot_configured=true`、`admin_chat_id=7677366761`、`ai_group_bot_enabled=true`，但 `telegram_bot_webhook_url` 为空，`telegram_bot_webhook_status=not_configured`。
- E4：执行线上 webhook refresh 后返回 `telegram_bot_webhook_status=registration_failed`，错误为 `PUBLIC_APP_BASE_URL 未配置，无法生成公网 webhook URL`。

## 根因

后端 `tenant_bot_settings` 生成 Telegram webhook URL 只读取 `PUBLIC_APP_BASE_URL`。生产部署链路只下发 `TGYUNYING_WEB_HOST`，`docker-compose.server.yml` 也没有把 `PUBLIC_APP_BASE_URL` 传进 backend/worker 容器，导致健康检查通过但 webhook 注册失败。

## 修复范围

- `.env.production.example` 增加 `PUBLIC_APP_BASE_URL` 示例。
- `docker-compose.server.yml` backend/worker 共享环境显式要求 `PUBLIC_APP_BASE_URL`。
- `deploy/docker-env.sh` 把 `PUBLIC_APP_BASE_URL` 纳入运行前必填检查。
- `deploy/release.sh` 在 `TGYUNYING_WEB_HOST` 存在时显式生成并下发 `PUBLIC_APP_BASE_URL=https://<host>`。
- `backend/tests/test_frontend_permission_gating.py` 增加部署契约回归测试。
- `docs/04-ops/deployment/PRODUCTION_RUNTIME.md` 补充 Bot webhook 生产配置要求。

## 本地验证

- Red：新增 `test_production_deploy_passes_public_app_base_url_for_tenant_bot_webhook` 后失败，证明当前部署契约未传 `PUBLIC_APP_BASE_URL`。
- Green：修复后该测试通过。
- Targeted：`backend/tests/test_tenant_bot_settings.py`、`backend/tests/test_permission_vocabulary.py`、相关部署静态契约测试通过。
- Shell：`bash -n deploy/release.sh deploy/docker-env.sh deploy/compose-up.sh deploy/server-install-release.sh` 通过。

## Release Gate

- release_mode: github_actions
- release_owner: main
- status: pending
- production_verification_required: true

## 发布后复核

- 触发 `Deploy Production` 成功。
- 再次调用 `POST /api/tenant-bot-settings/webhook/refresh`，期望 `telegram_bot_webhook_status=registered` 且 Telegram 当前 URL 等于系统期望 URL。
- 真实 Telegram 私聊 Bot `/start` 或 `/admin` 有可见回复后，prod-diagnosis 才能写 `production_fixed`。

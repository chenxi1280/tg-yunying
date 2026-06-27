# Tenant TG Bot Webhook Registration PRD

## 背景

线上租户 TG Bot 已保存 token 和 Admin Chat ID 后，真实 Telegram 客户端发送 `/start`、`/admin` 已读但无回复。诊断口径指向系统只保存配置和出站测试能力，没有在保存后注册 Telegram webhook，也没有查询 Telegram 当前 webhook URL。

本任务为 L3 生产相关 incident fix。QA pass 不等于线上恢复；Release Gate 通过并部署后，必须由 prod-diagnosis 做 E4 production verification。

## 产品口径

- 保存有效 `telegram_bot_token` 和 `admin_chat_id` 后，系统必须自动调用 Telegram `setWebhook`。
- 注册 URL 必须是完整公网 URL，格式为 `https://<public-host>/api/telegram-bot/webhook/{tenant_id}/{webhook_secret}`。
- `setWebhook` 后必须调用 `getWebhookInfo`，且 Telegram 当前 URL 与系统期望 URL 一致时才可标记 `registered`。
- `POST /api/tenant-bot-settings/test-message` 只验证出站 `sendMessage`，不能把 webhook 状态改成 `registered`。
- 清空 token 或显式删除 webhook 时必须调用 `deleteWebhook` 并回写状态。

## Webhook 状态

| status | 含义 |
| --- | --- |
| `not_configured` | token 或 Admin Chat ID 缺失 |
| `registering` | 正在注册或刷新 |
| `registered` | `setWebhook` 成功且 `getWebhookInfo.url` 与期望 URL 一致 |
| `registration_failed` | `setWebhook` 或生成公网 URL 失败 |
| `url_mismatch` | Telegram 当前 URL 与期望 URL 不一致 |
| `query_failed` | `getWebhookInfo` 失败 |
| `deleted` | 已调用 `deleteWebhook` 或 token 被清空 |

## API 设计

- `GET /api/tenant-bot-settings` 返回 webhook 状态、期望 URL、Telegram 当前 URL、最后检查时间和错误摘要。
- `PATCH /api/tenant-bot-settings` 保存配置后自动注册并查询 webhook。
- `POST /api/tenant-bot-settings/webhook/refresh` 手动重新注册并查询 webhook。
- `DELETE /api/tenant-bot-settings/webhook` 调用 Telegram `deleteWebhook` 并回写状态。
- `POST /api/tenant-bot-settings/test-message` 仅发送测试消息，不改变 webhook 可用状态。

## 命令行为

| 场景 | 行为 |
| --- | --- |
| 授权管理员 `/start` | 回复 Bot 已连接；AI 活群关闭时提示去 Web 开启 |
| 授权管理员 `/admin` | 回复 webhook 状态和 AI 活群开关状态 |
| 授权管理员 `/ai_group` | 开启时返回 AI 活群任务入口，关闭时返回未启用说明 |
| 非管理员 Chat ID | 回复当前聊天未授权，并记录拒绝审计 |

## QA 验收

- 保存有效 token/admin chat id 时调用 `setWebhook` 和 `getWebhookInfo`，URL 一致后状态为 `registered`。
- `setWebhook` 失败时状态为 `registration_failed`，错误摘要可见。
- `getWebhookInfo.url` 不一致时状态为 `url_mismatch`，期望 URL 和当前 URL 均可见。
- 测试发送成功但 webhook 失败时，页面仍显示 webhook 不可用。
- 删除 webhook 时调用 `deleteWebhook` 并回写 `deleted`。
- `/start`、`/admin`、`/ai_group` 对授权管理员有可见回复；AI 活群关闭时仍说明状态。
- 非管理员 Chat ID 收到未授权回复，不能修改配置。

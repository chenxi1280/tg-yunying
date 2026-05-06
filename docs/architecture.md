# 技术架构设计

## 技术栈

- 前端：React + TypeScript + Vite
- 后端：FastAPI + SQLAlchemy
- 数据库：SQLite 开发态，生产替换 PostgreSQL
- 队列：开发态同步模拟，生产替换 Redis 队列 + 独立 Worker
- TG 客户端：`TelegramGateway` 接口，生产实现使用 Telethon

## 服务边界

- `api-server`：业务 API、权限、多租户、任务创建与审核。
- `tg-session-service`：TG 登录、session 管理、账号同步。
- `task-scheduler`：任务拆分、限频、认领。
- `tg-worker`：实际 TG API 调用。
- `ai-service`：话术生成和内容检查。
- `archive-service`：群消息和成员归档。
- `audit-service`：审计记录。

当前仓库以一个 FastAPI app 承载这些边界，代码按模型、服务和适配器分层，便于后续拆服务。

## 配置与迁移

- 配置集中在 `app/config.py`，通过环境变量管理数据库、CORS、TG API、队列和验证码有效期。
- 数据库元数据仍可在开发态自动建表，长期 schema 管理入口已预留在 Alembic。
- 初始迁移位于 `migrations/versions/0001_initial.py`。

## 任务执行原则

发送任务必须按以下结构实现：

1. 短事务认领任务。
2. 释放数据库连接。
3. 调用 TG API。
4. 短事务回写结果。

这样可以避免 TG 网络调用占用数据库连接，也能把 FloodWait、慢速模式、权限不足等错误分类为业务状态。

## TG Gateway

`TelegramGateway` 是业务层唯一依赖的 TG 接口，当前默认使用 mock 实现。`TelethonTelegramGateway` 已预留真实接入边界：

- `start_login()`：验证码 / QR 登录挑战。
- `finish_login()`：验证码和 2FA 验证，返回 session。
- `check_account_health()`：检查账号 session 状态。
- `list_groups()`：同步账号可见群、权限和慢速模式信息。
- `send_message()`：发送消息并把 Telethon 错误归类为业务失败类型。

真实接入时只补 Telethon adapter，业务 API、队列、审计、前端流程保持不变。

## 安全原则

- 所有核心表带 `tenant_id`。
- TG session 只保存加密密文。
- 验证码短时展示，过期不可见。
- 验证码查看、任务审核、消息发送、成员导出都写审计。
- 不跨租户复用账号、群、素材、成员归档。

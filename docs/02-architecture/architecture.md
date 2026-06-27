# 技术架构设计

## 技术栈

- 前端：React + TypeScript + Vite
- 后端：FastAPI + SQLAlchemy
- 数据库：PostgreSQL
- 队列：开发态同步模拟，生产替换 Redis 队列 + 独立 Worker
- TG 客户端：`TelegramGateway` 接口，生产实现使用 Telethon

## 服务边界

- `api-server`：业务 API、登录保护、账号、运营目标、任务、监听、规则、数据和审计。
- `tg-session-service`：TG 登录、session 管理、账号同步和资产同步。
- `task-center`：频道互动、AI 活跃群、转发监听群任务编排。
- `listener-center`：按频道 / 源群聚合监听，标准化事件并分发给订阅任务。
- `rule-center`：系统级过滤、转换、路由、账号分配、限速和重试规则。
- `execution-center`：任务拆分、账号分配、限频、认领、自动校验和结果回写。
- `tg-worker`：实际 TG API 调用。
- `ai-service`：结构化内容生成、上下文摘要、自动校验和用量记录。
- `archive-service`：群消息和成员归档。
- `audit-service`：自动生成、过滤、转换、发送、跳过、失败、重试和敏感操作留痕。

当前仓库以一个 FastAPI app 承载这些边界，代码按模型、服务和适配器分层，便于后续拆服务。

## 配置与迁移

- 配置集中在 `app/config.py`，通过环境变量管理数据库、CORS、TG API、队列和验证码有效期。
- 数据库 schema 通过 Alembic 迁移管理。
- 初始迁移位于 `migrations/versions/0001_initial.py`。

## 任务执行原则

发送和任务动作必须按以下结构实现：

1. 短事务认领任务。
2. 释放数据库连接。
3. 调用 TG API。
4. 短事务回写结果。

这样可以避免 TG 网络调用占用数据库连接，也能把 FloodWait、慢速模式、权限不足、内容拦截、上下文过期等错误分类为业务状态。

新版任务中心的核心结构：

- 频道互动任务：父任务 → 频道消息 → 动作子任务 → 账号执行项。
- AI 活跃群任务：父任务 → Cycle → Turn → 自动校验 → 账号发送。
- 转发监听群任务：父任务 → 源群事件 → 过滤 / 转换 / 路由 → 转发批次 → 目标群发送项。

AI 活跃群和转发监听群不走人工审核。任务创建后通过自动校验、规则过滤、风控、限速、上下文检查、失败重试和审计记录保证可控。

## TG Gateway

`TelegramGateway` 是业务层唯一依赖的 TG 接口，当前默认使用 mock 实现。`TelethonTelegramGateway` 已预留真实接入边界：

- `start_login()`：验证码 / QR 登录挑战。
- `finish_login()`：验证码和 2FA 验证，返回 session。
- `check_account_health()`：检查账号 session 状态。
- `list_groups()`：同步账号可见群、权限和慢速模式信息。
- `send_message()`：发送消息并把 Telethon 错误归类为业务失败类型。

真实接入时只补 Telethon adapter，业务 API、队列、审计、前端流程保持不变。

## 安全原则

- 当前产品主线是单运营后台，历史 `tenant_id` 字段作为兼容隔离边界保留。
- TG session 只保存加密密文。
- 验证码短时展示，过期不可见。
- 验证码查看、开发者应用修改、AI Key 修改、规则发布、任务启停、消息发送、归档导出都写审计。
- 消息发送、任务中心和归档优先选择已确认的运营目标；手动输入目标必须提示未验证风险。

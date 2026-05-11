# TG 运营管理平台

新版 TG 运营管理平台是一个面向 Telegram 运营团队的统一后台，用于集中管理 TG 开发者应用、TG 账号、账号分组、运营目标、消息发送、任务中心、监听中心、规则中心、AI 内容生成、归档、运营数据和审计记录。

项目主线不再是多租户 SaaS、卡密订阅、旧 Campaign 或 AI 草稿人工审核流。当前主线是：

```text
账号接入 -> 资产同步 -> 运营目标确认 -> 规则配置 -> 任务创建
-> 监听 / AI / Worker 执行 -> 任务详情追踪 -> 数据复盘 -> 审计留痕
```

平台只围绕自有、授权、可运营的 TG 账号和目标设计。AI 活跃群和转发监听群不做人工审核；系统通过自动校验、规则过滤、风控、限速、上下文检查、失败重试和审计记录来保证任务可控。

## 当前能力

- TG 开发者应用池：维护 Telegram `api_id/api_hash`，账号登录时绑定可用应用。
- TG 账号中心：新增账号、验证码/二维码/2FA 登录、健康检查、资料同步、联系人与群频道同步、账号分组。
- 运营目标中心：把账号同步出的群、频道整理为可发送、可监听、可归档、可创建任务的运营目标。
- 消息发送中心：支持私聊、群聊、频道、批量、定时发送，保留发送记录、失败原因和重试入口。
- 任务中心：当前已落地 5 类任务：AI 活跃群、转发监听群、频道浏览、频道点赞、频道评论/回复。
- 监听中心：前端按频道/源群聚合展示监听关联，worker 在短窗口内对同一监听对象只采集一次，任务共享落库事件口径。
- 规则中心：集中展示系统级自动校验、转发处理、账号分配、重试策略和关键词规则。
- AI 内容中心：通过 OpenAI-Compatible 供应商生成群活跃内容、频道评论/回复、转发改写，并记录 token 用量。
- 归档中心：采集群历史消息、成员清单和新群初始化方案，支持查看、导出、重跑。
- 运营数据与审计：展示 AI 用量、执行统计、失败记录、验证码查看、账号/任务/规则/归档操作留痕。

旧 `/api/campaigns`、`/api/ai-drafts` 与 `/api/review-*` 已隔离为兼容路由，正常开发/生产默认不注册；确需迁移验证时显式设置 `ENABLE_LEGACY_CAMPAIGN_ROUTES=true` 或 `ENABLE_LEGACY_REVIEW_ROUTES=true`。`/api/message-tasks` 仍服务消息发送中心的发送记录、派发、重试和取消；自动化任务入口默认使用 `/api/tasks` 任务中心。

## 技术栈

- 后端：Python 3.12 + FastAPI + SQLAlchemy + Alembic
- 前端：React + TypeScript + Vite + Ant Design
- 数据库：PostgreSQL
- 队列：本地 sync worker 或 Redis
- TG 接入：`TelegramGateway` 抽象，开发默认 mock，真实环境使用 Telethon

## 运行配置

后端支持以下环境变量：

- `APP_ENV`：运行环境，默认 `development`
- `DATABASE_URL`：PostgreSQL 连接，项目不再支持 SQLite fallback
- `AUTO_MIGRATE_ON_START`：默认关闭，建议显式执行 `alembic upgrade head`
- `SEED_DEMO_DATA`：默认关闭，打开后才写入演示账号、群聊和素材
- `TEST_DATABASE_URL`：pytest 使用的 PostgreSQL 测试库
- `CORS_ORIGINS`：允许的前端地址
- `SESSION_SECRET_KEY`：TG session 加密密钥，生产必须替换
- `TG_GATEWAY_MODE`：`mock` 或 `telethon`
- `TG_API_ID` / `TG_API_HASH`：本地开发者应用 seed 来源
- `QUEUE_BACKEND`：`sync` 或 `redis`
- `REDIS_URL`：Redis 队列地址
- `LOGIN_CODE_TTL_SECONDS`：验证码展示有效期
- `ENABLE_LEGACY_CAMPAIGN_ROUTES`：默认仅测试环境开启；开发/生产需要迁移旧 Campaign 或 AI Draft 路由时才显式开启
- `ENABLE_LEGACY_OPERATION_TASK_ROUTES`：默认仅测试环境开启；开发/生产需要迁移旧 OperationTask 路由时才显式开启
- `ENABLE_LEGACY_REVIEW_ROUTES`：默认仅测试环境开启；开发/生产需要迁移旧 ReviewQueue 人工处理路由时才显式开启
- `ENABLE_LEGACY_REVIEW_DISPATCH_GATE`：默认关闭；仅需要恢复旧 ReviewQueue 阻塞发送队列时临时开启
- `ENABLE_LEGACY_CAMPAIGN_WORKER` / `ENABLE_LEGACY_OPERATION_TASK_WORKER`：默认关闭；仅迁移旧连续任务或旧 OperationTask 调度时临时开启

首次空库管理员账号从 `.env` 读取：

- `ADMIN_BOOTSTRAP_USERNAME`：默认 `admin`
- `ADMIN_BOOTSTRAP_PASSWORD`：默认 `admin123`
- `ADMIN_BOOTSTRAP_EMAIL`：可选

这些变量只在数据库没有用户时用于创建第一个管理员；已有用户不会被覆盖密码。

## 本地启动

### 后端

```bash
docker compose up -d postgres redis

cd backend
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
alembic upgrade head
uvicorn app.main:app --reload --port 8000
```

也可以使用模块入口：

```bash
cd backend
python -m app
```

开发环境默认在 API 服务启动时内置启动 worker，持续消费发送队列、AI 活跃群、群监听、账号同步、归档和任务中心动作。旧 Campaign 连续任务和旧 OperationTask 调度默认关闭，仅作为迁移兼容能力保留；确需临时启用时显式设置 `ENABLE_LEGACY_CAMPAIGN_WORKER=true` 或 `ENABLE_LEGACY_OPERATION_TASK_WORKER=true`。生产环境建议单独启动 worker：

```bash
cd backend
. .venv/bin/activate
python -m app.worker
```

只消费一次：

```bash
python -m app.worker --once
```

### 前端

```bash
cd frontend
npm install
npm run dev -- --host 127.0.0.1 --port 5173
```

前端默认请求 `http://127.0.0.1:8000/api`，可用 `VITE_API_BASE` 覆盖。

## 核心 API

- `POST /api/auth/login`：后台登录并返回 Bearer token
- `GET /api/auth/me`：当前登录用户和角色
- `GET /api/config/runtime`：运行配置和能力开关
- `GET/POST/PATCH /api/developer-apps`：TG 开发者应用管理
- `GET/POST/PATCH /api/ai-providers`：AI 供应商配置
- `GET/POST/PATCH /api/content-keyword-rules`：关键词规则配置
- `GET/POST /api/tg-accounts`：TG 账号列表和新增
- `POST /api/tg-accounts/{id}/login/start|verify|qr/check`：账号登录流程
- `POST /api/tg-accounts/{id}/sync-groups|sync-targets|health-check`：账号同步与健康检查
- `GET/POST/PATCH /api/operation-targets`：运营目标
- `GET /api/channel-messages`：频道消息库
- `POST /api/message-send-tasks/batch`：批量消息发送
- `GET /api/tasks`：新版任务中心列表
- `POST /api/tasks/group-ai-chat/create-and-start`：AI 活跃群任务
- `POST /api/tasks/group-relay/create-and-start`：转发监听群任务
- `POST /api/tasks/channel-view|channel-like|channel-comment/create-and-start`：频道互动任务
- `POST /api/tasks/{id}/start|pause|resume|retry|reset|stop`：任务生命周期
- `GET /api/listeners/summary`：按频道 / 源群聚合的监听订阅快照
- `GET /api/rules/summary`：系统规则、关键词规则、转发任务绑定规则摘要
- `GET /api/operation-metrics/summary`：运营数据汇总，覆盖账号、目标、发送、频道互动、AI 活跃、转发监听、归档、AI 用量和失败风险
- `GET/POST /api/rule-sets`：系统级转发规则集列表和创建
- `POST /api/rule-sets/{id}/versions`：为规则集创建新版本
- `POST /api/rule-sets/{id}/versions/{version_id}/publish`：发布规则集版本并切换活动版本
- `POST /api/rules/test`：关键词规则测试器
- `GET /api/archives` / `GET /api/archives/{id}`：归档列表与详情
- `GET /api/audit-logs`：审计记录，支持操作人、动作、对象类型、对象 ID、账号 ID、目标 ID、任务 ID、状态、关键词和时间范围筛选
- `POST /api/worker/drain-once`：开发环境消费一次队列

## 关键边界

- 只能使用已接入账号。
- 只能操作已确认的运营目标。
- 只能监听已授权源群、群聊、频道或讨论组。
- 任务必须有时间窗口、账号池、频控和执行记录。
- AI 活跃群按 Cycle/Turn 口径设计，任务详情会从执行项中展示 `cycle_id`、Turn、账号角色、意图、上下文消息和发送结果。
- AI 活跃群支持静默期低频模式、每日爬坡和上下文过期跳过；执行项会携带上下文快照，发送前发现新消息超过阈值会自动跳过并留痕。
- 转发监听按系统级规则集、规则版本、源消息事件和目标群发送项设计；任务可绑定 `rule_set_id` / `rule_set_version_id`，也可通过 `target_group_ids` 与规则版本 `routing` 做多目标分发，详情会展示转发批次、源事件、原文、转换后内容、发送账号和结果。
- 频道互动任务按频道消息维度展示动作子任务，任务详情会展示每条消息的目标数、完成数、失败数、重复数、容量缺口和账号执行项。
- 运营数据页接入真实汇总接口，不再只展示 AI Token；账号、目标、发送、频道互动、AI 活跃群、转发监听、归档、失败风险都会按现有表实时统计。
- 审计记录页支持高级筛选，能按操作人、动作、对象、账号、目标、任务、状态、关键词和时间范围回溯自动生成、规则命中、发送、跳过、失败与重试。
- 所有自动生成、过滤、转换、发送、跳过、失败、重试都必须可追踪、可解释、可审计。

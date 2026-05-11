# TG 运营管理平台 v1

一个面向多客户代运营的小规模 TG 运营管理平台原型。当前版本实现了产品设计中的核心闭环：

- 多租户账号、群聊、任务、素材、归档与审计的数据模型
- 平台级 Telegram 开发者应用池，TG 账号登录时按纯轮询绑定 API 凭证
- AI 供应商、提示词模板、客户 AI 策略与发送抖动配置
- TG 账号登录流程的安全状态机和验证码短时展示记录
- 群活跃任务创建、AI 草稿生成、素材/表情包绑定、人工审核、任务拆分
- 后台 Worker 发送模拟、失败分类、报表聚合
- 群聊内容与成员归档、新群初始化方案
- Web 管理台 + 手机 H5 响应式运营界面

当前 TG 能力通过 `TelegramGateway` 接口抽象，开发默认使用 mock gateway；真实接入时先在“开发者应用池”维护 Telegram `api_id/api_hash`，再配置 `TG_GATEWAY_MODE=telethon` 切换真实 Telethon 接入边界，并保持 API/任务/审计流程不变。

真实 Telethon 模式下，验证码登录需要账号记录里保留可登录的完整手机号；如果账号只保存脱敏手机号，系统会拒绝启动真实验证码登录，避免拿脱敏展示字段误发真实 TG 请求。环境变量 `TG_API_ID/TG_API_HASH` 只作为开发者应用池的本地 seed 来源，不再是长期单一凭证主路径。

AI 默认使用本地 `mock://openai-compatible` 供应商保持演示和测试可运行；真实接入 MiMo 或 DeepSeek 时，在“AI 配置”维护 OpenAI-Compatible `base_url`、`model_name`、`api_key` 和鉴权 header。`api_key` 加密保存，接口响应不返回明文。

## 运行配置

后端支持以下环境变量：

- `APP_ENV`：运行环境，默认 `development`
- `DATABASE_URL`：数据库连接，默认本地 PostgreSQL：`postgresql+psycopg://tg_yunying:tg_yunying@127.0.0.1:5432/tg_yunying?connect_timeout=3`
- `AUTO_MIGRATE_ON_START`：默认关闭；建议用 `alembic upgrade head` 显式迁移，避免启动请求被远端数据库迁移阻塞
- `SEED_DEMO_DATA`：默认关闭；打开后才会初始化演示 TG 账号、群聊和素材
- `TEST_DATABASE_URL`：测试数据库连接，运行 pytest 时必须显式配置 PostgreSQL 测试库
- `CORS_ORIGINS`：允许的前端地址
- `SESSION_SECRET_KEY`：TG session 加密密钥，生产必须替换
- `TG_GATEWAY_MODE`：`mock` 或 `telethon`
- `TG_API_ID` / `TG_API_HASH`：真实 TG API 凭证
- `QUEUE_BACKEND`：`sync` 或 `redis`；未显式配置时，测试环境用 `sync`，运行环境检测到 `REDIS_URL` 会默认使用 `redis`
- `REDIS_URL`：Redis 队列地址
- `LOGIN_CODE_TTL_SECONDS`：验证码展示有效期

开发环境只初始化最小本地工作区和后台用户，不再默认写入演示 TG 账号、群聊和素材。首次空库管理员账号从 `.env` 读取：

- `ADMIN_BOOTSTRAP_USERNAME`：登录用户名，默认 `admin`
- `ADMIN_BOOTSTRAP_PASSWORD`：首次登录密码，默认 `admin123`
- `ADMIN_BOOTSTRAP_EMAIL`：可选；未配置时系统会生成内部邮箱 `<username>@bootstrap.local`

这些变量只在数据库没有用户时用于创建第一个管理员；已有用户不会在启动时被覆盖密码。登录后可在右上角“修改密码”更新密码。

## 本地启动

### 后端

PyCharm 可以直接选择顶部运行配置 `Backend API`，点运行按钮启动后端。

这个入口读取 `.env` 里的 PostgreSQL 和队列配置；项目不再支持 SQLite fallback。
如果项目里存在 `backend/.venv`，即使 PyCharm 临时用了全局 Python，入口也会自动切换到项目虚拟环境运行。

也可以在命令行用同一个 Python 模块入口启动：

```bash
cd backend
python -m app
```

```bash
docker compose up -d postgres redis

cd backend
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
alembic upgrade head
uvicorn app.main:app --reload --port 8000
```

如果不使用 Docker，需要先在 PostgreSQL 中创建对应数据库和用户：

```sql
CREATE USER tg_yunying WITH PASSWORD 'tg_yunying';
CREATE DATABASE tg_yunying OWNER tg_yunying;
```

开发环境默认在 API 服务启动时内置启动 worker，所以本地只启动后端服务也会持续消费发送队列、AI 活跃群、群监听、账号同步、归档和任务中心动作。

生产环境默认不启用内置 worker，避免多 API 实例重复消费；生产或排查时仍可单独启动 worker：

```bash
cd backend
. .venv/bin/activate
python -m app.worker
```

开发排查时如只想消费一次，可使用：

```bash
python -m app.worker --once
```

默认 worker 每 2 秒轮询一次；可用 `--interval` 和 `--limit` 调整轮询间隔和每轮处理量。

如需显式控制内置 worker，可通过 `.env` 设置：

```bash
ENABLE_EMBEDDED_WORKER=true
EMBEDDED_WORKER_INTERVAL_SECONDS=2
EMBEDDED_WORKER_LIMIT=100
```

### 前端

```bash
cd frontend
npm install
npm run dev -- --host 127.0.0.1 --port 5173
```

前端默认请求 `http://127.0.0.1:8000/api`，可用 `VITE_API_BASE` 覆盖。

## 新增 API 能力

- `POST /api/auth/login`：后台登录并返回 Bearer token
- `GET /api/auth/me`：当前登录用户、角色和租户上下文
- `POST /api/auth/logout`：前端退出登录占位接口
- `GET /api/config/runtime`：运行配置和能力开关
- `GET /api/developer-apps`：平台管理员查看开发者应用池
- `POST /api/developer-apps`：平台管理员新增开发者应用，`api_hash` 加密保存且不返回
- `PATCH /api/developer-apps/{id}`：更新应用名称、容量、启停或轮换 `api_hash`
- `POST /api/developer-apps/{id}/check|enable|disable`：检查、启用、禁用开发者应用
- `GET/POST/PATCH /api/ai-providers`：AI 供应商配置，支持 OpenAI-Compatible，`api_key` 加密保存且不返回
- `POST /api/ai-providers/{id}/check`：检查 AI 供应商连通性
- `GET/POST/PATCH /api/prompt-templates`：平台或客户提示词模板
- `GET/PATCH /api/tenant-ai-settings`：客户 AI 默认模型、温度、token 和 fallback 策略
- `GET/PATCH /api/scheduling-settings`：发送抖动、批次间隔和时间窗策略
- `GET/POST/PATCH /api/materials`：素材库，支持文本、图片、表情包、文件、链接和组合消息
- `GET /api/tg-accounts/{id}/login-flows`：登录流程记录
- `POST /api/tg-accounts/{id}/login/start`：启动验证码或 QR 登录
- `POST /api/tg-accounts/{id}/login/verify`：提交验证码或 2FA
- `POST /api/tg-accounts/{id}/login/qr/check`：检查 QR 登录状态
- `POST /api/tg-accounts/{id}/sync-groups`：同步账号所在群
- `POST /api/tg-accounts/{id}/health-check`：账号健康检查
- `POST /api/groups/{id}/authorize`：群运营授权
- `POST /api/campaigns/{id}/approve-all`：批量审核草稿并入队
- `POST /api/message-tasks/{id}/retry`：失败任务重试
- `POST /api/worker/drain-once`：开发环境消费一次队列，生产环境禁用
- `GET /api/archives/{id}`：归档详情，包含消息和成员清单

## 关键边界

- 只面向客户授权账号与授权群聊。
- v1 默认半自动审核，不做无人审核直接发送。
- AI 只生成草稿和素材建议，不直接越过审核发送。
- 发送任务按 `scheduled_at` 到期后由 Worker 消费，未到时间的任务保持排队。
- 群聊复制做“内容归档 + 成员清单 + 新群初始化辅助”，不承诺无感迁移成员。
- Web API 只创建/审核任务，TG 发送由 Worker 执行，避免请求线程被 TG 网络调用阻塞。

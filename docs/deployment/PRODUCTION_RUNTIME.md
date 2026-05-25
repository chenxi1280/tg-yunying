# TG 运营管理平台生产部署说明

本项目生产部署沿用现有几个 TG 项目的发布模型：GitHub Actions 构建镜像，SSH 到服务器安装 release，服务器保留共享环境文件和运行数据。

## 目标服务器

- 部署目标：美国硅谷服务器，不使用旧生产服务器。
- 默认目录：`/data/tgyunying`
- 共享配置：`/data/tgyunying/shared/.env`
- 媒体目录：`/data/tgyunying/shared/media`
- 前端静态目录：`/data/infra/www/<域名>/current`
- Docker 网络：默认接入已有 `infra_default`

## GitHub 配置

Repository secrets:

- `SILICON_VALLEY_PRODUCTION_HOST`
- `SILICON_VALLEY_PRODUCTION_USER`
- `SILICON_VALLEY_PRODUCTION_PORT`
- `SILICON_VALLEY_PRODUCTION_SSH_PRIVATE_KEY`
- `GHCR_TOKEN`，如果默认 `GITHUB_TOKEN` 无法被服务器拉取 GHCR 私有镜像
- `GHCR_USERNAME`，可选，默认使用触发 Actions 的账号

Repository variables:

- `SILICON_VALLEY_PRODUCTION_BASE_DIR`，默认 `/data/tgyunying`
- `SILICON_VALLEY_RELEASE_BRANCHES`，默认 `release`
- `TGYUNYING_WEB_HOST`，例如 `tgyunying.example.com`
- `TGYUNYING_FRONTEND_STATIC_BASE_DIR`，例如 `/data/infra/www/tgyunying.example.com`
- `POST_DEPLOY_CHECKS_ENABLED`，默认 `true`
- `TGYUNYING_CHECK_HOST_NGINX`，默认 `true`
- `TGYUNYING_CHECK_PUBLIC_URLS`，默认 `true`
- `SSH_CONNECT_TIMEOUT`，默认 `60` 秒，控制 Actions 到服务器 SSH/SCP 建连等待时间
- `RELEASE_SSH_ATTEMPTS`，默认 `3`，控制发布脚本 SSH/SCP 重试次数
- `RELEASE_SSH_RETRY_DELAY`，默认 `10` 秒，控制发布脚本 SSH/SCP 重试间隔

正式自动部署只监听 `release` 分支，也保留 `workflow_dispatch` 手动触发。

## 首次服务器准备

服务器需要已经具备：

- Docker 与 Docker Compose plugin
- 可被部署用户执行的 Docker 权限
- 已存在的基础设施网络，例如 `infra_default`
- PostgreSQL 与 Redis 服务，并能被 `tgyunying-backend` 容器通过 `DATABASE_URL` / `REDIS_URL` 访问
- 宿主 Nginx，可代理 `/api/` 和 `/media/` 到 `127.0.0.1:18090`

首次 release 会创建 `/data/tgyunying/shared/.env`。脚本会从 `.env.production.example` 复制模板后中止，填完真实值后重新触发部署即可。

关键值必须替换：

- `DATABASE_URL`
- `REDIS_URL`
- `SESSION_SECRET_KEY`
- `ADMIN_BOOTSTRAP_PASSWORD`
- `CORS_ORIGINS`
- `TGYUNYING_WEB_HOST`
- `TGYUNYING_FRONTEND_STATIC_BASE_DIR`

生产环境不要开启 `ENABLE_EMBEDDED_WORKER`。compose 会单独启动 backend 以及 planner / dispatcher / listener / recovery / account-security / metrics worker。`account-security` worker 会先推进素材 TG 缓存再执行资料初始化，避免头像素材尚未暂存完成就更新资料；排障或扩容时也可以单独运行 `python -m app.worker --role material-cache`。

worker 容器不暴露 backend API 端口，健康检查不能使用 `curl 127.0.0.1:8000/api/health`。生产 compose 使用 `python -m app.worker --healthcheck --role "$WORKER_ROLE"` 检查对应角色最近 2 分钟心跳；如果某个 worker unhealthy，先看 `worker_heartbeats`、容器日志和数据库连接，而不是先排查 backend API。

## Nginx

参考配置在 `deploy/nginx/tgyunying.conf.example`。

核心代理口径：

- 静态前端：`root /data/infra/www/<域名>/current`
- 后端 API：`/api/ -> http://127.0.0.1:18090/api/`
- 媒体文件：`/media/ -> http://127.0.0.1:18090/media/`
- 健康检查：`/healthz -> http://127.0.0.1:18090/api/health`

## 发布验证

发布后脚本会区分三层状态：

1. 容器层：`tgyunying-backend` healthy，`tgyunying-worker-planner`、`tgyunying-worker-dispatcher-*`、`tgyunying-worker-listener`、`tgyunying-worker-recovery`、`tgyunying-worker-account-security`、`tgyunying-worker-metrics` healthy
2. 本机应用层：`http://127.0.0.1:18090/api/health`
3. 宿主 Nginx / 公网入口：`https://<域名>/` 与 `https://<域名>/api/health`

常用手工检查：

```bash
docker ps --filter name=tgyunying
curl -fsS http://127.0.0.1:18090/api/health
curl -fsS --resolve tgyunying.example.com:443:127.0.0.1 https://tgyunying.example.com/api/health
docker compose exec -T worker-planner python -m app.worker --healthcheck --role planner
```

如果本机 API 正常但公网失败，优先检查宿主 Nginx 配置和域名证书，不要先改应用代码。

如果 Actions 在 `Checking SSH connectivity` 或 `Uploading release archive` 阶段出现 `Connection timed out during banner exchange`，说明失败发生在 SSH 握手/服务端 banner 返回之前，应用容器还没有进入发布流程。优先检查生产服务器 SSH 端口、安全组/防火墙、`sshd` 负载或 `MaxStartups` 限制，以及 GitHub secret 里的端口是否真的是 SSH 服务。

## 线上登录 / 任务创建排障口径

2026-05-25 线上排查确认过一类组合故障：运营反馈“登录了 2 个账号还是报错、无法登录 / 创建任务失败”，但 Nginx、数据库和任务状态显示前端症状与后端事实不完全一致。类似问题必须先看线上日志和数据库事实，再判断是否改代码。

### 快速判定

1. 看公网入口是否正常：

```bash
curl -fsS https://tgyunying.telema.cn/api/health
curl -i https://tgyunying.telema.cn/api/auth/me
```

`/api/health` 返回 `200` 且 `/api/auth/me` 无 token 返回 `401 missing bearer token` 时，公网入口和认证中间件基本正常，不能把问题先归因到 Nginx 或登录态整体失效。

2. 看账号登录链路：

```bash
docker logs --since 8h --tail 500 tgyunying-backend
grep 'POST /api/tg-accounts' /var/log/nginx/*access*.log | tail -80
```

重点确认：

- `POST /api/tg-accounts/{id}/login/start` 是否返回 `500`。
- 是否有后续 `POST /api/tg-accounts/{id}/login/verify`。
- 数据库里该账号是否有 `login_flow`、`session` 和审计记录。

2026-05-25 事故样例中，账号 `142` 在 `23:29:12` 执行 `login/start` 返回 `500`，没有进入验证码校验；账号 `143` 在 `23:30:11` `login/start` 返回 `200`，随后两次 `login/verify` 完成验证码和 2FA，状态变为在线。因此用户看到的“添加了 2 个账号”不等于“2 个账号都登录成功”。

修复要求：

- backend 日志必须暴露 `login/start` 的 trace、账号、开发者应用和 Telegram / Telethon 错误分类。
- 失败必须落登录流或审计，前端账号详情能看到失败原因。
- API 不能只返回裸 `500`；必须返回可展示错误和下一步处理建议。

### 任务创建超时与后端已提交

Nginx `499` 表示客户端主动断开连接。任务中心出现 `POST /api/tasks/precheck` 或 `POST /api/tasks/*/create-and-start` 的 `499` 时，不能直接判断后端失败，要继续查任务表和审计。

```bash
grep 'POST /api/tasks' /var/log/nginx/*access*.log | tail -120
docker exec -i tgyunying-backend python - <<'PY'
from app.db import SessionLocal
from app.models.task_center import Task

with SessionLocal() as db:
    rows = db.query(Task).order_by(Task.created_at.desc()).limit(5).all()
    for task in rows:
        print(task.id, task.type, task.name, task.status, task.created_at, task.last_error)
PY
```

2026-05-25 事故样例中，前端在 `23:45:33` 断开 `channel-like/create-and-start` 请求并记录为 `499`，但后端在 `23:45:51` 写入任务 `8eb67fd5-b45e-46dd-bd52-9f9ac2743d41` 并启动。根因方向不是“任务一定没创建”，而是“前端 15 秒超时与后端同步长事务造成状态不一致”。

修复要求：

- `precheck`、`create`、`create-and-start` 返回 `trace_id` 和容量摘要。
- 可能超过前端普通请求超时时间的创建启动流程必须返回任务 ID、异步操作 ID 或可轮询状态。
- 前端遇到请求超时后必须核验后端是否已创建，不能直接提示失败并诱导重复点击。

### 频道点赞容量和并发上限

频道点赞任务需要同时检查每帖目标点赞数、账号范围、有效参与账号和并发上限。2026-05-25 事故样例中，任务配置每条消息目标点赞 `30`，但 `max_concurrent=20`，账号池选择被并发上限截断为 20 个参与账号，所以即使新增 2 个账号也不会让该任务达到每帖 30 赞。

排障时看任务配置和运行摘要：

```bash
docker exec -i tgyunying-backend python - <<'PY'
from app.db import SessionLocal
from app.models.task_center import Task

task_id = "8eb67fd5-b45e-46dd-bd52-9f9ac2743d41"
with SessionLocal() as db:
    task = db.query(Task).filter(Task.id == task_id).one()
    print("type=", task.type)
    print("status=", task.status)
    print("account_config=", task.account_config)
    print("type_config=", task.type_config)
    print("stats=", task.stats)
    print("last_error=", task.last_error)
PY
```

修复要求：

- 预检、确认页、任务详情和运营异常必须拆分展示 `target_per_message`、`effective_account_count`、`max_concurrent` 和 `capacity_shortfall`。
- `max_concurrent` 截断参与账号时必须明确提示“并发上限限制了参与账号数”。
- “没有可新增的有效点赞账号”只能用于真实没有可规划 action 的场景，不能掩盖容量配置不匹配。

### Worker 健康检查噪音

如果 `docker ps` 显示 worker 容器 `unhealthy`，先看健康检查日志：

```bash
docker inspect --format '{{json .State.Health}}' tgyunying-worker-dispatcher-1
docker logs --tail 200 tgyunying-worker-dispatcher-1
```

2026-05-25 排查中，worker 的 unhealthy 原因是健康检查在 worker 容器内 curl `127.0.0.1:8000`，但 worker 不运行 backend API 服务。这是健康检查配置噪音，不等于 dispatcher / listener / recovery 业务一定失败。

修复要求：

- backend 容器继续使用 `/api/health`。
- worker 容器改用 worker 进程存活、heartbeat、角色 drain 或队列积压指标。
- 发布检查必须分开报告 backend health、worker health、队列 drain 和公网 health。

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

`workflow_dispatch` 常用诊断开关：

- `run_production_diagnostics`: 部署后探测 planner drain 和 AI 硬小时任务量。
- `run_ai_group_quality_diagnostics`: 部署后只读检查 AI 活群质量链路，输出 worker 心跳、账号表达卡覆盖、30 天消息记忆状态、近 24 小时重复文本风险、每个 AI 活群任务的话题 / 讨论老师配置、账号在线摘要、最近 action 的 `ai_message_memory_id` 和表达卡版本。
- `reconcile_account_profiles`: 检查并补齐账号资料初始化，同时补齐缺失的 AI 活群账号表达卡；表达卡按小批次调用真实 AI 供应商生成，生成协议使用紧凑 JSONL 并保留旧 pipe 行解析兼容，按提交批次独立落库。批量结构化输出格式错误时，系统会拆成单账号继续请求同一个真实 AI 供应商；单账号仍格式错误、或真实 AI 供应商返回 429 / quota exhausted 时，脚本必须输出 `ACCOUNT_PROFILE_RECONCILE_PROGRESS` / `ACCOUNT_PROFILE_RECONCILE` 结构化进度并让 release gate 失败，下次额度恢复或协议修复后从剩余缺失账号继续跑，不能伪造成功或静默生成通用表达卡。
- `run_tianjin_diagnostics` / `run_tianjin_blocked_account_diagnostics`: 天津目标群准入和阻塞账号专项诊断。

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
- `PUBLIC_APP_BASE_URL`，例如 `https://tgyunying.telema.cn`，用于生成 Telegram Bot webhook 公网回调地址
- `TGYUNYING_FRONTEND_STATIC_BASE_DIR`

后端在 `APP_ENV=production` 时会拒绝默认 bootstrap 管理员密码 `admin123`，因此 `ADMIN_BOOTSTRAP_PASSWORD` / `ADMIN_PASSWORD` 必须显式设置为强随机值。

生产环境不要开启 `ENABLE_EMBEDDED_WORKER`。compose 会单独启动 backend 以及 planner / dispatcher / listener / recovery / account-security / metrics worker。`account-security` worker 会先推进素材 TG 缓存再执行资料初始化，避免头像素材尚未暂存完成就更新资料；排障或扩容时也可以单独运行 `python -m app.worker --role material-cache`。

worker 容器不暴露 backend API 端口，健康检查不能使用 `curl 127.0.0.1:8000/api/health`。生产 compose 使用 `python -m app.worker_health --role "$WORKER_ROLE"` 检查对应角色最近 2 分钟心跳；如果某个 worker unhealthy，先看 `worker_heartbeats`、容器日志和数据库连接，而不是先排查 backend API。

## Nginx

参考配置在 `deploy/nginx/tgyunying.conf.example`。

核心代理口径：

- 静态前端：`root /data/infra/www/<域名>/current`
- 静态资源：`/assets/` 必须开启 7 天 immutable 缓存，并开启 gzip；首屏 JS 裸传会显著拖慢跨境和代理链路加载。
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
docker compose exec -T worker-planner python -m app.worker_health --role planner
```

如果本机 API 正常但公网失败，优先检查宿主 Nginx 配置和域名证书，不要先改应用代码。

如果 Actions 在 `Checking SSH connectivity` 或 `Uploading release archive` 阶段出现 `Connection timed out during banner exchange`，说明失败发生在 SSH 握手/服务端 banner 返回之前，应用容器还没有进入发布流程。优先检查生产服务器 SSH 端口、安全组/防火墙、`sshd` 负载或 `MaxStartups` 限制，以及 GitHub secret 里的端口是否真的是 SSH 服务。

租户 TG Bot 保存 token 和管理员 Chat ID 后，会用 `PUBLIC_APP_BASE_URL` 生成 `https://<host>/api/telegram-bot/webhook/{tenant_id}/{webhook_secret}` 并注册到 Telegram。生产部署必须把该变量传入 backend/worker 容器；只配置 `TGYUNYING_WEB_HOST` 只能通过健康检查，不能保证 webhook 注册可用。

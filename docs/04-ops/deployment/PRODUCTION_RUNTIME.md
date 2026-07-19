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
- `run_ai_group_quality_diagnostics`: 部署后检查 AI 活群质量链路，输出 worker 心跳、账号表达卡覆盖、30 天消息记忆状态、近 24 小时重复文本风险、每个 AI 活群任务的话题 / 讨论老师配置、账号在线摘要、最近 action 的 `ai_message_memory_id` 和表达卡版本。诊断会等待账号在线摘要刷新，online gate 通过后触发 hard-hourly planner drain 并输出 `AI_GROUP_QUALITY_HARD_HOURLY_DRAIN`；drain 会把唤醒结果和当前 running 任务中仍有可重试 `planning_deficit > 0` 的任务合并去重，避免 `_wake_hard_hourly_tasks` 因 `next_check_at` 等节流返回空时漏掉质量补偿任务，paused 历史任务只保留为诊断样本不参与补偿；drain 会在既有 100 次总尝试上限内多轮补齐没有结构 blocker 的任务，`duplicate_message`、`content_policy`、`quality_filter` 等输出质量过滤 blocker 只用于继续补计划，最终仍必须由成功或待执行动作覆盖目标；再重新检查当前小时是否已有足够成功或待执行动作；若 drain 后全部 hard-hourly blocker 仅为 `dispatcher_lag`，诊断会输出 `AI_GROUP_QUALITY_HARD_HOURLY_WAIT` 并最多等待 120 秒重采样，生成不可用、规则缺失、表达卡缺失、目标权限等结构 blocker 不等待、不降级。workflow 外层 `timeout 1200` 必须长于脚本 900 秒 online gate 加 planner drain / dispatcher settle。仍存在 desired 账号未 online、stale、missing、blocked、需重登或 offline 时输出 `AI_GROUP_QUALITY_ONLINE_GATE_FAILED` 并让 release gate 失败，不能用 worker 存活掩盖账号在线缺口。近 24 小时 `pending`、`claiming`、`executing` 中出现会继续发送的重复文本时输出 `AI_GROUP_QUALITY_RECENT_DUPLICATE_GATE_FAILED` 并失败；已 `success` / `unknown_after_send` 的历史重复输出为 `sent_duplicate_observations`，只作为质量债观察，不单独阻断当前发布；失败 / 跳过记录只保留为诊断样本，不单独阻断。账号在线状态的 stale 截止时间按普通 / 低频 probe 间隔加宽限计算，普通活跃账号为 5 分钟 probe + 10 分钟宽限；诊断等待窗口必须覆盖这 15 分钟普通活跃探活窗口。stale 后会立即重排 probe，且 account-online drain 在 probe 批次打满 limit 时会延后一轮 stale 标记，避免部署重启或 backlog 后健康账号在下一次探测前被系统自身过早标记 offline。
- AI generation Phase C 若持续出现 `SELECT ai_group_message_memory.id, normalized_text, raw_text` 且 Dispatcher 长时间停在 `provider_call_started / generation_claimed`，先核对部署版本是否包含 generation 级 `DuplicateMemoryBatch` 和 `ix_ai_group_message_memory_tenant_status_updated`。生产约万级 7 天租户历史窗口必须每个 generation 批次只装载一次，后续 slot 只走 `updated_at` 覆盖窗口增量查询；逐 slot 重扫属于吞吐回归，完全不刷新又会漏掉其他 Dispatcher 并发提交，均不能通过缩小到单群、跳过去重或提高 worker 数掩盖。
- 若 SQL 已降为每 generation 一次但 Dispatcher Python 线程仍持续占 CPU、Phase C 事务超过 5 秒，核对相似度路径是否先执行字符 Jaccard / 序列匹配可达上界剪枝并使用有界字符画像缓存。剪枝结果必须与原 `max(SequenceMatcher ratio, char Jaccard) >= threshold` 完全等价；禁止通过降低历史数量或放宽阈值换吞吐。
  - `AI_GROUP_REALISM_AUDIT_PRE_ONLINE`: 在 online gate 等待前输出运行中 AI 活群近期消息的只读审计，标记模板 AI 腔和缺少账号面具主题锚点的样本；该审计不替代 online / hard-hourly release gate，也不会因旧消息样本直接中断发布。
- `reconcile_account_profiles`: 检查并补齐账号资料初始化，同时补齐缺失的 AI 活群账号表达卡；表达卡按小批次调用真实 AI 供应商生成，生成协议使用紧凑 JSONL 并保留旧 pipe 行解析兼容，按提交批次独立落库。批量结构化输出格式错误时，系统会拆成单账号继续请求同一个真实 AI 供应商；单账号仍格式错误、或真实 AI 供应商返回 429 / quota exhausted 时，脚本必须输出 `ACCOUNT_PROFILE_RECONCILE_PROGRESS` / `ACCOUNT_PROFILE_RECONCILE` 结构化进度并让 release gate 失败，下次额度恢复或协议修复后从剩余缺失账号继续跑，不能伪造成功或静默生成通用表达卡。
- `update_account_masks_direction`: 在生产容器内执行 `.github/scripts/update_account_masks_direction.py`，把所有 active 账号写入新的 active 账号面具版本，方向统一为“伪装嫖客 / 男性 / 色情”相关口径；旧 active 面具置为 `superseded`，新版本写 `AuditLog` 并刷新 Redis 面具缓存。脚本输出 `ACCOUNT_MASK_DIRECTION_UPDATE`，其中 `target_account_count` 必须等于 `verified_active_count` 才算成功；找不到 active 账号或写入后校验不一致时直接失败。
- `configure_clash_search_join_live`: 配置生产 Mihomo / Clash 节点并创建搜索加群 smoke task。`clash_search_join_apply=false` 时只做订阅解析和节点出口预检，不写 DB；`clash_search_join_apply=true` 才会写入代理绑定和搜索加群测试任务。`clash_skip_cert_verify` 默认为 `false`，只有遇到订阅节点证书链异常且确认要放宽 Mihomo TLS 校验时才显式设为 `true`。
- `run_tianjin_diagnostics` / `run_tianjin_blocked_account_diagnostics`: 天津目标群准入和阻塞账号专项诊断。

生产任务通道约定：`search_join_group` / `search_join` 是唯一强制使用 Clash 代理的任务链路；`group_ai_chat`、`channel_view`、`channel_like`、`channel_comment` 的账号健康探测和实际互动调用走账号直连凭证，不因 Clash 节点不可用而阻塞活群、浏览、点赞或评论任务。搜索加群仍通过授权环境绑定和健康代理节点 fail closed。

### AI 活跃群 Grok CLI Bridge

- 生产 Linux 必须在 `/root/.grok/bin/grok` 安装并完成授权，`grok models` 必须包含 `grok-4.5`。发布 workflow 的 `admin` 部署账号通过 `sudo -n` 在部署前检查 root 的 CLI / 模型，部署后检查 planner 容器内可执行文件；任一检查失败则发布失败，不把 Grok 静默视为可用。
- 后端镜像必须安装 `git`，供 Bridge 在临时目录执行 `git init`；发布后预检同时检查 planner 容器内 Grok 可执行文件和 `git --version`。
- `docker-compose.server.yml` 将 `${GROK_CLI_HOME_DIR:-/root/.grok}` 挂载到 backend、planner 和四个 dispatcher；共享锁默认位于 `/root/.grok/tgyunying-cli.lock`，同一服务器只允许一个 Grok 生成进程。
- 默认环境为 `GROK_CLI_ENABLED=true`、`GROK_CLI_MODEL=grok-4.5`、`GROK_CLI_TIMEOUT_SECONDS=90`。租户仍可通过 `ai_group_grok_fallback_enabled` 单独关闭 Grok 阶段，通过 `ai_group_static_fallback_enabled` 关闭静态兜底。
- 静态兜底仅适用于已绑定当日覆盖账本的非引用 AI 活群 action；生产验收要同时核对 `quality_fallback=emoji_react`、`generation_source/fallback_stage=static_safe_fallback`、原始 `fallback_reason`、消息记忆预占和真实 Telegram 结果，不能只看 Action 变为 ready。
- 全账号每日覆盖验收必须逐条核对 `TaskAccountDailyCoverage.account_id = Action.account_id = ExecutionAttempt.account_id`，且 Attempt 为 success、`remote_message_id` 非空；只比较 confirmed 总数会漏掉运行时换号造成的误确认。
- 发送型 `unknown_after_send` 不得直接重发；远端按账号 peer、目标群、调用前后时间窗和原文确认消息不存在后，才可将 Action / Attempt 记为 `remote_message_absent_confirmed`，并释放处于 `unknown` 的原账号覆盖预约重新规划。
- 若 pending Action 持续出现 `account_inflight_conflict`，必须交叉核对同账号是否仍有数据库 `claiming/executing` Action；数据库无占用但冲突持续说明 Dispatcher 进程内 reservation 泄漏，生产版本必须由 `dispatch_action finally` 统一释放，不能等待 30 分钟 Redis TTL 或把账号误判为离线。
- Bridge 固定使用 `--no-memory --no-subagents --disable-web-search --permission-mode dontAsk --verbatim`；只保存有界错误码、模型阶段和耗时，不保存 Prompt、推理过程、授权资料或密钥。
- 生产验收必须分层：CLI / 模型预检通过不等于任务恢复；还需在受控测试任务中观察 `fallback_stage`、`actual_model`、`generation_attempts` 和最终 Action，且测试前不得触发真实 Telegram 发送。

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

worker 容器不暴露 backend API 端口，健康检查不能使用 `curl 127.0.0.1:8000/api/health`。生产 compose 的 Docker healthcheck 读取 worker 主循环写入的本地 heartbeat 文件（默认 `/tmp/tgyunying-worker-heartbeat`），避免每 20 秒为每个 worker 启动 Python 并查询 DB；业务观测仍看 `worker_heartbeats` 表。如果某个 worker unhealthy，先看容器内 heartbeat 文件时间、`worker_heartbeats`、容器日志和数据库连接，而不是先排查 backend API。

发布替换 worker 容器后，Recovery 先限定为当前 `executing` Action 的 lease owner，再以过期 heartbeat 的完整 `worker_id` 或 `hostname + pid` 匹配租约；heartbeat ID 末尾的角色后缀不参与 legacy 租约匹配。没有 executing lease 时不得扫描历史 heartbeat。未进入 Telegram Gateway 的旧容器执行项应立即按 `stale_worker` 回收，已进入 Gateway 的仍按 unknown 防重复口径处理。

账号在线保活默认使用 `ACCOUNT_ONLINE_WORKER_DRAIN_LIMIT=1000` 作为单轮分页数量，使用 `ACCOUNT_ONLINE_PROBE_CONCURRENCY=32` 控制同一时刻的 Telegram 健康探测数，并使用独立的 `ACCOUNT_ONLINE_PROBE_TIMEOUT_SECONDS=30` 限制单个健康探测，不能继承普通业务 Telegram 调用的 300 秒超时。三者只控制处理吞吐，不是账号上线名额：全部 `desired_online=true` 账号都必须进入状态机，账号池超过单页时由后续 drain 继续处理，不得在服务内部再次按前 N 个账号截断。数据库读取和状态落库留在 worker 主线程，探测线程只执行 Telegram 网络调用；结果按完成顺序流式返回，主线程逐条提交，不能等待整页全部探测结束后集中落库。`last_probe_at` 保留各账号实际完成时间；同一 drain 批次的 `next_probe_at` 和成功 stale 窗口则统一不早于本批最后一个网络探测完成后的对应间隔，避免批次耗时超过 5 分钟时早完成账号已再次到期。每个探测线程通过 `check_account_health_isolated` 在本线程独立 asyncio 事件循环中执行一次性 Telethon client，不能把 32 路探测重新提交到 process-wide 事件循环；正常发送、监听和登录仍使用原业务生命周期及持久 client cache。健康探测 client 在 `finally` 中断开，30 秒探测超时后最多等待 5 秒有界断连和 1 秒调度余量，调用返回时清理已收口，且清理错误不得覆盖原始 Telegram 错误。生产验收需同时检查 account-online 批次没有成片 `account_health_probe_failed / TimeoutError`，探测期间 TCP 连接保持在配置并发附近并在批次后回落。并发和超时参数必须为正数，非法配置会使服务明确启动失败。

account-online 主线程冻结本批账号和凭证后必须先提交并结束读取事务，再启动 Telegram 调用；逐结果提交期间本批 ORM 对象保持已加载状态，不得因 `expire_on_commit` 触发逐账号隐式 SELECT。线上出现 `connection timeout expired` 且堆栈位于提交后的 ORM 属性读取时，按该事务边界检查，不能先扩大数据库连接池掩盖。生产验收同时要求没有 drain 级 `ConnectionTimeout`。

Dispatcher 若一次 claim 包含共享 `ai_generation_claim_token` 的 normal pending `send_message`，该 worker 会按领取顺序串行推进这一个 claim 批次，避免多个线程同时加载并更新重叠 Action 集合。生产验收必须检查 PostgreSQL 日志在发布后不再新增 `UPDATE actions ... deadlock detected`，并同时确认覆盖继续增长；该串行边界不是 action、账号或任务总量限制。

Recovery 必须依次提交前序 Action 修复、连续 Task 状态修复，再进入 stale Action claim，确保任一提交都不会同时刷新 dirty Task 与 Action。线上若 `worker drain failed` 同时出现 `UPDATE tasks` / `UPDATE actions ... deadlock detected`，应检查这三个事务边界；不得靠扩大连接池、降低 worker 数量或限制账号总量掩盖。

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
docker compose exec -T worker-planner sh -lc 'now=$(date +%s); last=$(cat "${WORKER_LOCAL_HEALTHCHECK_FILE:-/tmp/tgyunying-worker-heartbeat}" 2>/dev/null || echo 0); echo "age=$((now - last))s"; test $((now - last)) -le 120'
```

如果本机 API 正常但公网失败，优先检查宿主 Nginx 配置和域名证书，不要先改应用代码。

如果 Actions 在 `Checking SSH connectivity` 或 `Uploading release archive` 阶段出现 `Connection timed out during banner exchange`，说明失败发生在 SSH 握手/服务端 banner 返回之前，应用容器还没有进入发布流程。优先检查生产服务器 SSH 端口、安全组/防火墙、`sshd` 负载或 `MaxStartups` 限制，以及 GitHub secret 里的端口是否真的是 SSH 服务。

租户 TG Bot 保存 token 和管理员 Chat ID 后，会用 `PUBLIC_APP_BASE_URL` 生成 `https://<host>/api/telegram-bot/webhook/{tenant_id}/{webhook_secret}` 并注册到 Telegram。生产部署必须把该变量传入 backend/worker 容器；只配置 `TGYUNYING_WEB_HOST` 只能通过健康检查，不能保证 webhook 注册可用。

## search_rank_deboost 任务灰度发布约束

`search_rank_deboost`（搜索排名观察任务）是与 `search_join_group` 平行的新任务类型，用于灰度观察搜索结果曝光、点击行为和风控边界。该任务不得对外承诺“降低对方排名”；排名变化只能作为观察指标。首版上线必须按以下灰度约束执行，未通过约束不得全量推开。

### 真实执行闸门

- 当前代码已实现真实 `TelethonTelegramGateway.search_rank_deboost_candidates/execute_search_rank_deboost`、同代理出口探测和逐点击事实结果；生产状态仍为 `production_unproven`，必须通过协议样本、迁移、真实代理出口和 1-2 个灰度账号的 E4 验证后才能标记生产可用，不得用 monkeypatch/fixture 替代。
- 任务创建只进入 `draft` 准备态；`create_and_start` / `start_task` 必须同时满足真实豁免群已从生产 Gateway 搜索结果中选出、生产类显式实现 `search_rank_deboost_candidates/execute_search_rank_deboost`、协议样本和全部涉及分组持久代理绑定预检通过，才能进入 `running`。
- `search_rank_deboost_exempt_groups.exempt_group_username=pending_real_search` 只表示待接入真实搜索结果；Planner 遇到该占位值必须以 `exempt_group_pending_real_search` 阻断，不得生成 action。
- 全账号任务上线后若标准 Planner drain 超过 60 秒，先在 `pg_stat_activity` 核对是否出现按单个 `account_id` 重复执行的 Action / MessageTask `min/max/count` 容量查询。正常实现必须由 `AccountCapacityCache` 批量预取同一候选池的小时、自然日和冷却占用；不得通过恢复账号数量上限解决。生产验收要求 Planner drain 可完成、查询数不随账号数线性增长，并看到真实 Action / coverage 增长。
- 若 planner / PostgreSQL CPU 持续升高，额外核对 running `channel_view`、`channel_like`、`channel_comment` 的 `message_scope=dynamic_new`：`next_run_at` 必须位于当前时刻之后最多一个 `listener_interval_seconds`，不能整体落后 8 小时。浏览/点赞的历史去重必须只读取当前 `channel_message_id` / Telegram `message_id` 集合，并命中 `ix_actions_channel_planner_message_history` 或 `ix_actions_channel_planner_legacy_history`；浏览的完成数和日配额必须批量聚合，日配额查询命中 `ix_actions_channel_view_daily_capacity`。不得把整段 task Action 历史拉回 Python，也不得靠提高 worker interval 或缩短 Action 留存掩盖。Recovery 的当日 `reserved/sending` coverage 释放查询应命中 `ix_task_daily_coverage_recovery_terminal`；空结果不应扫描整个覆盖账本。
- 若 hard-hourly Planner / PostgreSQL CPU 持续升高，确认 Alembic 至少为 `0110_hard_hourly_recovery_cpu`。最近 24 小时历史必须用 `executed_at` 分支和“仅 scheduled_at 命中”分支的非重叠 `UNION ALL`，分别命中 `ix_actions_hard_hourly_history_executed` 和 `ix_actions_hard_hourly_history_scheduled`；每条 Action 只能归类一次，不能为 24 个小时桶反复扫描完整列表。metrics 已按五分钟采集，不能把改变采样频率当作该根因修复。
- 若 Recovery / PostgreSQL CPU 持续升高，先比较 `actions.status='executing'` 数量和 stale heartbeat 数量。stale heartbeat 历史很多而没有 executing lease 时，恢复路径不应读取 heartbeat 表；有 lease 时只查询精确 owner，分别使用 `ix_actions_executing_lease_owner`、唯一 `worker_id` 与 `ix_worker_heartbeats_host_pid_last_seen_at`。同一轮 pending hard-hourly membership fast-track 只应执行一次。
- 若 Recovery / PostgreSQL CPU 持续升高，检查 `actions` 的过期明细清理和 `runtime_cleanup_audits` checkpoint 查询。前者必须命中 `ix_actions_runtime_detail_retention`，空结果不得扫描完整 Action 历史；后者必须命中 `ix_runtime_cleanup_audits_kind_created_at`，不能每轮 JSON 全表扫描审计记录。`RUNTIME_DETAIL_CLEANUP_INTERVAL_SECONDS` 与 `RUNTIME_METRIC_CLEANUP_INTERVAL_SECONDS` 默认均为 300；installer 只会将共享 `.env` 中旧默认的精确值 `60` 升级为 `300`，其他显式值保持不变。Action 留存仍为 5 天，不能通过缩短留存替代索引和节流修复。
- 若 `group_context_messages` 最近上下文读取出现 `DataFileRead` 且超过 10 秒，确认 Alembic 至少为 `0103_group_context_recent_index`，并核对 `ix_group_context_messages_tenant_group_recent` 为 valid/ready。精确 SQL 应按 tenant/group 使用该索引取得 `sent_at DESC, id DESC LIMIT N`，不得只依赖全表 `sent_at` 索引。
- 若全账号日覆盖显示 `账号在线状态不可用`，不得只看 task stats 的少量 sample；应交叉核对当日 ready coverage、目标群 `can_send`、`tg_account_online_state` 未 stale 的在线交集。若在线交集充足而 Planner 只记录 1 个前排离线候选，说明仍存在“先截断、后过滤”的旧选号路径，不能归因于主机负载或服务账号总量限制。
- Dispatcher 不得用 `account_group_proxy_bindings.observed_exit_ip` 自证出口；真实执行必须由 Gateway 使用分组 `runtime_proxy_id` 对应的 SOCKS/HTTP 端点完成当前 HTTPS 出口探测，并用同一代理指纹创建 Telethon client。缺失、漂移、协议不支持或 binding 非 active 时写 `proxy_egress_guard_failed`，不得回退本机直连、账号旧代理或授权槽位代理。
- 每个 action 最多一次 `navigate_only` 真实点击；只有 Gateway 返回 `click_outcomes.status=confirmed` 才写成功点击统计。`observed_no_click` 不计点击成功，`unknown_after_click` 占用配额且不得自动重试。

### 灰度账号范围

- 首次真实环境验收只使用一个启用降权专用组和 1-2 个已养号账号，先证明用途隔离、同端点出口、真实搜索和单次安全点击闭环。
- 产品层 `selection_mode=all` 的语义必须是所有启用降权组中的一致可用账号，不设置与该语义冲突的账号数硬上限；风险通过每 action 一次点击、账号/关键词/分组 IP/任务小时 reservation、冷却和分组启禁用控制。
- 扩量前必须确认普通任务 all/group/manual 均排除降权账号，并按分组逐步启用；不得通过减少候选集、静默抽样或回退普通账号伪装“全部账号”。

### 协议样本采集门槛

发布前必须先完成协议样本采集门槛：`bot_protocol_samples` 中 `sample_purpose=rank_deboost`、`bot_code=jisou` 至少采集：

- jisou `/start` 响应样本 ≥ 2 个账号
- 关键词搜索响应样本 ≥ 5 个关键词，记录原始 button text、button type、callback_data hash、url、button effect 分类
- 翻页响应样本 ≥ 3 次分页
- 竞争群结果项按钮结构样本 ≥ 3 种 button effect 类型（navigate_only / join_candidate / external_http_url / unknown 至少覆盖 3 种）
- 出口防泄漏样本 ≥ 3 次（与 search_join_group 一致）

未完成样本采集时 Executor 只能跑 fixture 和预检，不得进入真实灰度执行；任务创建接口必须以「协议样本不足，请先完成样本采集」拒绝启动。

### 共享 IP 风险观察周期

- 分组内多账号共享 1 个 Clash 出口 IP，分组级共享出口 IP 每日点击上限默认 50（`group_ip_daily_click_limit`），需连续 7 天观察风控数据后再扩量。
- 观察指标：`group_ip_daily_click_limit` 触顶告警（分组共享 IP 触顶）、IP 漂移告警（`rank_deboost_group_ip_drift`）；触顶时建议切换节点或降低节奏。
- 观察期内出现目标群排名异常波动或竞争群集体消失等反作弊迹象时，立即暂停灰度并复盘。

### 灰度扩量条件

7 天共享 IP 风险观察期满后，必须同时满足以下条件才允许分批扩量，任一未达标继续观察：

- 连续 3 天无 `join_button_violation`（误点加入按钮自检告警）。
- 连续 3 天无 `account_isolation_violation`（账号组隔离硬过滤告警）。
- 连续 3 天 `group_ip` 触顶占比 < 20%（触顶天数 / 观察天数）。

扩量仍按 5-10 账号一档分批增量，单批增量后重置观察窗口；未达扩量条件不得解除灰度约束。

### 与 search_join_group 平行运行

- `search_rank_deboost` 与 `search_join_group` 平行运行，互不影响、互不依赖。
- `search_join_group` 仍守 PRD §4.10 「非目标结果只做 `navigate_only` 安全浏览，且总量默认 ≤3」原约束，降权任务的开例外不得回灌到 search_join_group 链路。

### 发布前必须验证

发布前必须验证以下硬约束全部生效，任一未通过不得上线：

1. 账号组隔离硬过滤生效：`pool_purpose=rank_deboost` 分组内账号不被其他任务通过「全部可用账号」语义误选；同一账号不得同时存在于 rank_deboost 分组和普通分组。
2. 分组级代理绑定节点独占校验生效：同一节点不得同时被授权槽位级 `account_proxy_bindings` 和降权分组级 `account_group_proxy_bindings` 复用；分组级绑定节点容量 = 分组账号数，不再守 `max_authorizations_per_node_default=1`。
3. 误点加入按钮自检告警生效：Executor 误点 `join_candidate` 按钮时立即停止 action、写 `search_rank_deboost_action_stats.join_button_violation=true`、风控中心生成 `rank_deboost_join_button_violation` 告警，并暂停该账号后续 action 直到人工确认。

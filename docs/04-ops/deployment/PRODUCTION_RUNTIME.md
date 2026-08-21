# TG 运营管理平台生产部署说明

## 马来西亚授权灾备节点

MY 节点只运行 `authorization-dr-node`，不运行消息、listener、Planner、Dispatcher 或同步。部署入口为 `deploy/malaysia/deploy-authorization-dr-node.sh`，固定读取 `/opt/tgyunying-authorization-dr/node.env`。生产一期显式使用 `MY_WAKE_STORAGE_MODE=ssh_mirror`：worker 把一份不可变密文写入 MY 持久卷，再通过专用受限 SSH 身份把第二份 create-only 密文和 inventory 写入硅谷 `/data/tgyunying/shared/authorization-dr-snapshots`。专用恢复密钥以 root-only 文件保存在 MY，并备份到硅谷运维目录；普通 backend/worker 不挂载该密钥。脚本在启动前校验控制面、固定出口、SSH 目标、identity、known_hosts、恢复密钥，以及专用身份对远端镜像目录的可达、可穿越和可写权限；任一项失败时不启动 worker。硅谷父目录保持原 owner/mode，只给 `tgyunying-dr` 添加 `--x` ACL，目标镜像目录保持该用户独占 `0700`，避免目标目录归属正确但父目录不可穿越。`kms_oss` 仍是显式可选模式，不是生产一期硬依赖，也不会被自动选择。

MY 无 GHCR 拉取凭据时，精确 release 镜像先经 SSH 传输、摘要校验并 `docker load`，随后显式设置 `AUTHORIZATION_DR_IMAGE_MODE=local`；脚本只在本机已存在完整 `TGYUNYING_BACKEND_IMAGE` 时启动。`registry` 模式仍会真实 pull，二者不会自动互相回退。

两账号 ABC canary 前使用 `deploy/authorization-canonical-backfill.sh`。必须先 `--mode preview` 保存 fingerprint，再以不同 requester/approver 执行 `--mode apply --expected-fingerprint ...`；它只改数据库授权投影，不连接 Telegram。账号 pointer 缺失但存在唯一 `is_current primary/SV` 且 Session/App 精确一致时只链接已有行，禁止重复创建；其余才创建新 A 行。apply 后必须 `--mode status` 读回，`missing_session/session_unreadable/current_conflict` 不得计入 canonical 成功。仅对精确 canary 候选使用 `qualify-preview/qualify-apply`，后者会使用既有 A Session 做 Telegram identity probe；Telegram current hash=0 时只允许用 preview 冻结且同账号的健康 SV peer observer 唯一解析非零 hash，observer 不成为码源。该流程不创建新登录、不替换 Session、不切主；B 新登录遇到 hash=0 使用相同唯一解析规则。

单账号 B/C 完成后使用 `deploy/authorization-abc-backup.sh --mode verify-preview` 冻结 E4 fingerprint，再以独立 idempotency key 和异人审批执行 `--mode verify-apply`。该入口先持久化 operation，再让 A 向 Saved Messages 只发送一次；成功后读回 A/B Telegram 身份、C 双副本/restore probe、runtime off 和 MY active client=0，并把 remote message ID 写入审计。发送结果不明进入 `reconcile_unknown`，相同 key 只能返回原 operation，禁止重发。

B/C challenge 的 Telegram 服务消息时间按固定 3 秒 clock-skew/秒精度容差绑定；容差窗内必须恰好一条符合消息，否则 fail closed。旧 operation 因验证码过期终态失败后不得复用 idempotency key 或 flow；只有完成根因修复、重新 preview 并获得新批准，才可用新 key 发起一次新 challenge。

生产顺序固定为：发布候选 SHA -> 读回 App A=`primary_sv`、App B=`standby_1_sv`、App C=`standby_2_my` -> 在两机配置受限 SSH mirror 身份和恢复密钥双机备份 -> 为专用用户配置父目录 `--x` ACL，并从 MY 容器按正式 identity/known_hosts 执行远端目录访问检查 -> 通过 SSH 写入 root-only `node.env` 并启动 worker -> 读回固定出口和持续新鲜 heartbeat -> runtime preview/fingerprint/apply -> 两个互相独立的单账号 canary。第一个账号未完成全链路验收时不审批第二个；canary 未达到本地+SSH 镜像双副本、恢复密钥解封、inventory、restore probe、slot CAS、旧 SV retained/protected 和 Telegram exact-set 全部事实时，不创建全量批次。

## 账号批量自动登录发布闸门（默认关闭）

批量登录新增 migration `0148_account_batch_login`、内置管理员主体兼容 migration `0149_batch_login_principal` 和独立 `worker-account-login`。共享环境必须显式配置 `ACCOUNT_BATCH_LOGIN_MODE=off|reconcile_only|enabled`、正数 `ACCOUNT_BATCH_LOGIN_WORKER_CONCURRENCY` 与 `ACCOUNT_BATCH_LOGIN_DEVELOPER_APP_CONCURRENCY`；切到 `enabled` 时还必须配置正数 `ACCOUNT_BATCH_LOGIN_HOST_CONCURRENCY` 与 `ACCOUNT_BATCH_LOGIN_HOST_MIN_INTERVAL_SECONDS`。worker concurrency 默认 4，只控制同批/跨批同时执行的 item phase，不能替代 host/Developer App 持久 rate bucket。其余可调参数为单行上限/300 秒总预算/120 秒验证码窗口/轮询间隔/凭据保留/24 小时对账窗口以及 phone fingerprint 当前与 accepted versions。缺失或非法值启动失败，不允许以隐藏前端入口代替后端 mode gate。

发布顺序固定为：migration → 对每个租户运行 `backend/scripts/backfill_account_phone_aliases.py --tenant-id <id>` preview，确认零冲突后以 `--apply --actor <执行人> --approval-ref <审批引用>` apply/readback → 以 `reconcile_only` 启动 backend 与 `worker-account-login` → 核对 worker heartbeat、DNS/TLS/peer IP、开发者应用并发桶和提醒 outbox → 前端与受控权限 → 切 `enabled` 做单号真实 E4。回滚先切 `reconcile_only`，继续未解对账和提醒投递；只有 unresolved、远端 started 和待投递 outbox 全部清零后才可切 `off`。迁移/worker healthy、自动化测试或部署 SHA 都不等于 Telegram 授权成功，E4 仍须核对 batch/item/flow、权威授权、目标分组、UUID binding/备注和 initial/correction 提醒。

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

发布质量门不减少测试：`backend-checks` 用两个独立 runner 分别执行 `-m no_postgres` 与 `-m "not no_postgres"`，两者是完整测试集合的互补分区；`frontend-checks` 独立并行执行 `npm ci` 和正式构建。`build-images` 必须等待两个后端分区和前端全部成功，`deploy` 还必须等待镜像完成。任何分区失败都阻止生产发布，不能通过删测试、增加 skip 或让某一分区 `continue-on-error` 缩短时长。空 PostgreSQL 测试库由 `0001_initial` 使用当前 metadata 建表，后续新增列迁移必须先检查真实列并保持幂等；reset/migration 失败必须输出底层异常，不能只保留笼统连接错误。

`Deploy Production` 的 `workflow_dispatch` 常用诊断开关：

- `run_production_diagnostics`: 部署后探测 planner drain 和 AI 硬小时任务量。
- `run_ai_group_quality_diagnostics`: 部署后检查 AI 活群质量链路，输出 worker 心跳、账号表达卡覆盖、30 天消息记忆状态、近 24 小时重复文本风险、每个 AI 活群任务的话题 / 讨论老师配置、账号在线摘要、最近 action 的 `ai_message_memory_id` 和表达卡版本。诊断会等待账号在线摘要刷新，online gate 通过后触发 hard-hourly planner drain 并输出 `AI_GROUP_QUALITY_HARD_HOURLY_DRAIN`；drain 会把唤醒结果和当前 running 任务中仍有可重试 `planning_deficit > 0` 的任务合并去重，避免 `_wake_hard_hourly_tasks` 因 `next_check_at` 等节流返回空时漏掉质量补偿任务，paused 历史任务只保留为诊断样本不参与补偿；drain 会在既有 100 次总尝试上限内多轮补齐没有结构 blocker 的任务，`duplicate_message`、`content_policy`、`quality_filter` 等输出质量过滤 blocker 只用于继续补计划，最终仍必须由成功或待执行动作覆盖目标；再重新检查当前小时是否已有足够成功或待执行动作；若 drain 后全部 hard-hourly blocker 仅为 `dispatcher_lag`，诊断会输出 `AI_GROUP_QUALITY_HARD_HOURLY_WAIT` 并最多等待 120 秒重采样，生成不可用、规则缺失、表达卡缺失、目标权限等结构 blocker 不等待、不降级。workflow 外层 `timeout 1200` 必须长于脚本 900 秒 online gate 加 planner drain / dispatcher settle。仍存在 desired 账号未 online、stale、missing、blocked、需重登或 offline 时输出 `AI_GROUP_QUALITY_ONLINE_GATE_FAILED` 并让 release gate 失败，不能用 worker 存活掩盖账号在线缺口。近 24 小时 `pending`、`claiming`、`executing` 中出现会继续发送的重复文本时输出 `AI_GROUP_QUALITY_RECENT_DUPLICATE_GATE_FAILED` 并失败；已 `success` / `unknown_after_send` 的历史重复输出为 `sent_duplicate_observations`，只作为质量债观察，不单独阻断当前发布；失败 / 跳过记录只保留为诊断样本，不单独阻断。质量载荷与拟人审计只检查已经生成非空 `message_text` 的 Action：尚处于生成前的空壳 `pending/claiming/executing` 只进入状态计数，不得因尚无 `ai_message_memory_id` 误报失败；一旦正文非空，表达卡版本、消息记忆、质量决策、生成来源和 act 类型仍必须全部存在。`style_only_v2` 只约束语气、句长和表达习惯，拟人审计不得因正文没有复述面具摘要中的价格、位置、服务等主题而报 `mask_theme_missing`；正文主题必须继续服从任务和真实群上下文。日目标闸门的 `new_hard_hourly_action_count` 只统计当前 `target_date` 当天新建的 Action，历史 hard-hourly 审计记录不得永久阻断新日账本。账号在线状态的 stale 截止时间按普通 / 低频 probe 间隔加宽限计算，普通活跃账号为 5 分钟 probe + 10 分钟宽限；诊断等待窗口必须覆盖这 15 分钟普通活跃探活窗口。stale 后会立即重排 probe，且 account-online drain 在 probe 批次打满 limit 时会延后一轮 stale 标记，避免部署重启或 backlog 后健康账号在下一次探测前被系统自身过早标记 offline。
- AI generation Phase C 若持续出现 `SELECT ai_group_message_memory.id, normalized_text, raw_text` 且 Dispatcher 长时间停在 `provider_call_started / generation_claimed`，先核对部署版本是否包含 generation 级 `DuplicateMemoryBatch` 和 `ix_ai_group_message_memory_tenant_status_updated`。生产约万级 7 天租户历史窗口必须每个 generation 批次只装载一次，后续 slot 只走 `updated_at` 覆盖窗口增量查询；逐 slot 重扫属于吞吐回归，完全不刷新又会漏掉其他 Dispatcher 并发提交，均不能通过缩小到单群、跳过去重或提高 worker 数掩盖。
- 若 SQL 已降为每 generation 一次但 Dispatcher Python 线程仍持续占 CPU、Phase C 事务超过 5 秒，核对相似度路径是否先执行字符 Jaccard / 序列匹配可达上界剪枝并使用有界字符画像缓存。剪枝结果必须与原 `max(SequenceMatcher ratio, char Jaccard) >= threshold` 完全等价；禁止通过降低历史数量或放宽阈值换吞吐。
  - `AI_GROUP_REALISM_AUDIT_PRE_ONLINE`: 在 online gate 等待前输出运行中 AI 活群近期非空正文的只读审计，标记模板 AI 腔；该审计遵守 `style_only_v2`，不要求正文复述面具业务主题，也不替代 online / hard-hourly release gate。
- `reconcile_account_profiles`: 检查并补齐账号资料初始化，同时补齐缺失的 AI 活群账号表达卡；表达卡按小批次调用真实 AI 供应商生成，生成协议使用紧凑 JSONL 并保留旧 pipe 行解析兼容，按提交批次独立落库。批量结构化输出格式错误时，系统会拆成单账号继续请求同一个真实 AI 供应商；单账号仍格式错误、或真实 AI 供应商返回 429 / quota exhausted 时，脚本必须输出 `ACCOUNT_PROFILE_RECONCILE_PROGRESS` / `ACCOUNT_PROFILE_RECONCILE` 结构化进度并让 release gate 失败，下次额度恢复或协议修复后从剩余缺失账号继续跑，不能伪造成功或静默生成通用表达卡。
- `update_account_masks_direction`: 在生产容器内执行 `.github/scripts/update_account_masks_direction.py`，把所有 active 账号写入新的成年男性日常社交方向 active 面具版本；脚本不得写入敏感交易措辞。旧 active 面具置为 `superseded`，新版本写 `AuditLog` 并刷新 Redis 面具缓存。脚本输出 `ACCOUNT_MASK_DIRECTION_UPDATE`，其中 `target_account_count` 必须等于 `verified_active_count` 才算成功；找不到 active 账号或写入后校验不一致时直接失败。
- `configure_clash_search_join_live`: 配置生产 Mihomo / Clash 节点并创建搜索加群 smoke task。`clash_search_join_apply=false` 时只做订阅解析和节点出口预检，不写 DB；`clash_search_join_apply=true` 才会写入代理绑定和搜索加群测试任务。`clash_skip_cert_verify` 默认为 `false`，只有遇到订阅节点证书链异常且确认要放宽 Mihomo TLS 校验时才显式设为 `true`。
- `restore_mihomo_runtime.py`: 仅用于已保存配置存在、但同名 `tgyunying-mihomo-*` 容器缺失的基础设施恢复。先以固定镜像 digest 执行 preview，记录 config / DB proxy / target 三个 manifest SHA-256；apply 必须提供这三个 hash、当前部署 SHA 和 approval ref，只创建 config 与 DB 名称的精确交集并从 backend 通过 SOCKS5H 做真实出口验证。任一出口失败只删除本次创建的容器并失败，不重绑账号、授权或环境，不创建 smoke task；无配置且零消费者的 DB proxy 只有显式开关才可通过既有审计健康检查标记为 unhealthy。
- `retire_unused_mihomo_runtime.py`: 仅处理显式 `--target`，preview 固定当前 release、DB state/全消费者计数、container id/image/restart 和只读 config hash；apply 必须提供同一 manifest hash、actor 与 approval ref，在 DB 行锁下重算账号、授权、active proxy/environment/group binding、desired-online 和 open login flow 全为 0 后才写 disabled/AuditLog，然后精确执行 `restart=no + stop`。不删容器、volume 或 config；读回必须核对逐 target AuditLog、stopped 状态与非 target manifest 不变。生产宿主只有 Python 3.6，该 host 脚本不得使用更高版本语法；DB 事务仍在 Python 3.12 backend 容器内执行。
- `run_tianjin_diagnostics` / `run_tianjin_blocked_account_diagnostics`: 天津目标群准入和阻塞账号专项诊断。

账号身份专项生产操作使用独立的 `Production Account Profile Identity Operations` workflow，避免部署 workflow 超过 GitHub `workflow_dispatch` 顶层输入上限：

- `operation=profile_dedupe`：运行 `.github/scripts/account_profile_duplicate_reconcile.py`。先用 `preview + seed` 输出 active 普通运营账号重复组、keeper、精确旧/新名和 canonical SHA-256；`apply` 必须提供相同 seed、preview SHA 和 approval ref，旧值或 deployed SHA 漂移即失败，只创建正式账号安全批次；相同 manifest 的重复 apply 复用已有批次，只补尚未建批次的目标。`readback` 必须提供原 preview SHA，先确认同一 manifest 的全部 batch item 成功，再逐账号拉取 Telegram profile，严格校验 first name 等于新昵称且 last name 为空，同时复算剩余重复组。发布成功、批次已创建或数据库昵称已更新都不能替代该远端回读。
- `operation=login_batch_initialize`：运行 `.github/scripts/account_login_batch_profile_initialize.py`。preview 以精确终态 `login_batch_ids` 中 `succeeded/succeeded_with_warning` 的成功登录账号并集（可留空，要求最近 7 天最多 20 个终态批次的全部成功账号并集精确命中）、`expected_target_count`、匿名群风格 `style_group_ids`、seed 和 deployed SHA 冻结 canonical manifest；“新登录”允许已有账号重新登录，不等于新建数据库账号。跨批重复账号按 `account_id` 去重并采用最大 login item ID 的最新成功快照，failed/unresolved/skipped 行不纳入目标。若某个前置批次混有测试用已有账号重登，必须显式把该 batch ID 放进 `created_only_batch_ids`，只保留有同事务创建审计的成功项；该输入必须是 login batch 子集并进入 manifest/hash，不能按数量截断。群原始 sender identity/姓名不进入输出，新名字不得复制来源完整名，头像只使用具备 `AvatarMaterialSource` 许可事实、非真人、已审核且 TG cache ready 的 avatar 素材。apply 必须显式提供 preview 输出的 login batch/group IDs、created-only batch IDs、manifest SHA 和 approval ref；目标旧值、登录 item version、用途、开放资料批次或部署 SHA 任一冲突即零写入。正式账号安全批次每批最多 50，先全部 staging，邻居 snapshot audit 落库后再一次性 claim/激活；staging 中断不会调用 Telegram。readback 逐账号核对 name claim、本地资料、Telegram first/last name、远端头像感知指纹与邻居 hash；头像指纹必须模拟 Telegram 居中正方形裁切并以 LANCZOS 缩放后比较，不能把长方形原图整幅拉伸。名字成功但头像 waiting/failed、pull 失败、任意头像存在但内容不符或邻居漂移都不得报告完成。本操作不写发送 Task/Action/ExecutionAttempt/ledger/coverage，apply 前后必须另行核对发送目标守恒。
- 风格证据在最近 30 天内按 `created_at/id` 最早稳定顺序读取，并在群之间 round-robin 冻结精确 100 个匿名 sender；新消息不能改变已审批 manifest。头像从稳定 ID 前缀中取最多目标数个 ready 素材，再按 seed 排序并 round-robin；不依赖运行中 `usage_count`。样本过期、素材失效、目标或 claim 漂移仍必须显式重新 preview。
- `operation=avatar_import`：运行 `.github/scripts/account_avatar_material_import.py`。固定 17 个 Wikimedia Commons 非真人候选，preview 会下载 Commons 返回的受控缩略图并校验许可、署名、MIME、尺寸、SHA-256 和感知哈希；manifest 不含会随导入改变的状态字段，`apply` 必须提供 preview SHA 和 approval ref，部分执行后可用同一 SHA 续跑；apply 不把整批图片常驻内存，而是每张素材启动一个全新外部 Python 进程，重新下载、复核内容/感知哈希和尺寸、提交事务并关闭 Session 后显式退出，主进程逐张校验 exit code，确保 Pillow/native 内存由操作系统回收且来源漂移显式失败。已导入 source page 若已 ready 或本地文件仍存在则幂等跳过；若未 ready 且本地临时文件已被 TTL 清理，则仅在来源元数据和全部指纹与数据库及本次 manifest 完全一致时恢复原 Material 文件并写审计，不创建重复素材。未知许可、来源漂移、重定向、超限、精确重复和近似重复均显式失败。`readback` 必须确认 17/17 来源已审核，且 material-cache 已写入 TG cache peer、message 和 account，才可报告可作为头像来源；未 ready 项同时输出内容路径/文件存在性和最近缓存错误用于定位队头阻塞。
- 每次操作必须填写当前已部署的完整 40 位 release SHA；workflow 自身 checkout SHA 和生产 `current` symlink 任一不一致即失败。`apply` 必须填写 preview 输出的 manifest SHA-256 与 approval ref，`readback` 必须填写同一 manifest SHA-256。
- `login_batch_initialize` preview 还会冻结带时区的 `style_sample_cutoff_at`；apply/readback 必须原样回传。样本只读取该截止时间之前的近期消息，避免活跃群新消息及滚动保留改变已审批 manifest。

生产任务通道约定：`search_join_group` / `search_join` 是唯一强制使用 Clash 代理的任务链路；`group_ai_chat`、`channel_view`、`channel_like`、`channel_comment` 的账号健康探测和实际互动调用走账号直连凭证，不因 Clash 节点不可用而阻塞活群、浏览、点赞或评论任务。搜索加群仍通过授权环境绑定和健康代理节点 fail closed。

### AI 活跃群 Grok CLI Bridge

- 生产 Linux 必须在 `/root/.grok/bin/grok` 安装并完成授权，`grok models` 必须包含 `grok-4.5`。发布 workflow 的 `admin` 部署账号通过 `sudo -n` 在部署前检查 root 的 CLI / 模型，部署后检查 planner 容器内可执行文件；任一检查失败则发布失败，不把 Grok 静默视为可用。
- 后端镜像必须安装 `git`，供 Bridge 在临时目录执行 `git init`；发布后预检同时检查 planner 容器内 Grok 可执行文件和 `git --version`。
- `docker-compose.server.yml` 将 `${GROK_CLI_HOME_DIR:-/root/.grok}` 挂载到 backend、planner 和四个 dispatcher；共享锁默认位于 `/root/.grok/tgyunying-cli.lock`，同一服务器只允许一个 Grok 生成进程。
- 默认环境为 `GROK_CLI_ENABLED=true`、`GROK_CLI_MODEL=grok-4.5`、`GROK_CLI_TIMEOUT_SECONDS=90`。租户仍可通过 `ai_group_grok_fallback_enabled` 单独关闭 Grok 阶段，通过 `ai_group_static_fallback_enabled` 关闭静态兜底。
- AI 活群任务未显式配置 `ai_model` 时，主阶段必须通过任务指定或租户健康 Provider 选择器执行；租户默认 Provider 已禁用时继续选择当前健康 Provider，不得硬编码 MiniMax-M3 而跳过健康 MiMo v2.5。静态签到兜底适用于已绑定 `primary_quantity_slot_id` 的 coverage 与 extra-volume 数量槽，保留原 direct/reply 关系；生产验收要同时核对 `quality_fallback=check_in_fallback`、`generation_source/fallback_stage=static_safe_fallback`、原始 `fallback_reason`、消息记忆预占和真实 Telegram 结果，不能只看 Action 变为 ready。
- 发布接管必须把五类任务级数量软上限和当前单用户 `SchedulingSetting.default_account_hour_limit/default_account_day_limit` 一并幂等归一为 `1_000_000`；接管 preview/apply/再次 preview 必须分别显示待变更、已应用和零漂移，apply 仅在值变化时写一条 `scheduling_setting` 审计。账号短冷却仍只作软延后，Telegram FloodWait/SlowMode、授权、代理、目标准入、内容质量和 unknown 防重不在该归一范围。
- 若健康 MiMo 已存在但 AI Action 长期保持 `ai_generation_status=pending`，先按 admission state 统计 `ready/post_follow_visibility_probe/waiting` 与最近 Reservation。waiting 正文不得排在 ready/probe 正文前反复领取 fulfillment 份额；无法原子切入唯一 probe 的 waiting 正文必须在 Gateway 前终态收口，lease/claim/binding、Coverage 和数量/内容槽归零。修复验收必须同时看到 ready/probe Action 先被 claim，以及后续 `actual_model=mimo-v2.5` 的真实 Generation/Attempt 事实。
- 若 admission 已是 `post_follow_visibility_probe` 而 Action 没有 probe 标记，按存量无绑定探针恢复：同一事务补写唯一 `probe_action_id` 与 Action probe/admission 字段，再进入健康 MiMo。不得把该状态继续退回 `group_bot_admission_wait`，也不得并发创建第二条探针。
- 发布后用 `pg_stat_activity + pg_blocking_pids` 连续检查 listener：不得出现 listener 写事务阻塞 dispatcher 的 `group_bot_admissions`/`transactionid` 等待。可信群管 confirmation Action 查询必须按 admission id/version 在数据库过滤；未出现 `provider_call_started_at/actual_model` 时结论写“调用前被阻塞”，不能写“MiMo 不可用”。
- 全账号每日覆盖验收必须逐条核对 `TaskAccountDailyCoverage.account_id = Action.account_id = ExecutionAttempt.account_id`，且 Attempt 为 success、`remote_message_id` 非空；只比较 confirmed 总数会漏掉运行时换号造成的误确认。
- 发送型 `unknown_after_send` 不得直接重发；远端按账号 peer、目标群、调用前后时间窗和原文确认消息不存在后，才可将 Action / Attempt 记为 `remote_message_absent_confirmed`，并释放处于 `unknown` 的原账号覆盖预约重新规划。
- `orphaned_source_pacing_reconcile_v2` 只允许修正 pre-Gateway 时钟与失效占用。preview manifest 必须冻结 task/ledger/quantity owner/account、OperationTarget peer+revision、ChannelMessage 本地+远端 ID、payload 与 daily/total/effective/due 数量目标；apply 在同一事务行锁重算并在写后复核，任一发送目标或数量漂移整批零写入失败。`safely_not_executed` 只重开原 owner，不能冲抵 due 或更换接收目标；replacement 继续继承相同 owner/目标快照。
- 群发送权限救援的连续失败排序必须先用 `as_beijing` 统一 executed/scheduled/fallback 时间；PostgreSQL aware 时间、legacy naive 时间或缺失时间混排不得让 Dispatcher 抛异常。若异常发生在 Gateway call-start 后，Action/Attempt 必须保持 `unknown_after_send/result_unknown` 与 `remote_outcome_unknown`，禁止因热修发布直接重发或释放 quantity owner。
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

生产环境不要开启 `ENABLE_EMBEDDED_WORKER`。compose 会单独启动 backend 以及 planner / dispatcher / listener / recovery / account-security / material-cache / metrics worker。`material-cache` 独立推进素材 TG 暂存，`account-security` 只处理账号安全和资料批次；需要头像的批次项仍等待 `cache_ready_status=ready`。两个队列必须使用独立容器和 heartbeat，避免素材远端调用阻塞纯昵称、2FA 或设备清理。临时诊断可以单独运行 `python -m app.worker --role material-cache`，但不得与常驻 worker 并发处理同一素材。

素材 TG 暂存每张文件使用一次性 Telethon client，调用完成或异常后都断开，不进入进程级 client cache。线上出现前几张 ready、后续全部长期 `not_cached` 时，要同时核对 material-cache heartbeat、当前最小未缓存 material 和远端上传耗时；不能仅凭容器 healthy 判断队列正常，也不能用重启反复重传未知结果。

worker 容器不暴露 backend API 端口，健康检查不能使用 `curl 127.0.0.1:8000/api/health`。生产 compose 的 Docker healthcheck 读取 worker 主循环写入的本地 heartbeat 文件（默认 `/tmp/tgyunying-worker-heartbeat`），避免每 20 秒为每个 worker 启动 Python 并查询 DB；业务观测仍看 `worker_heartbeats` 表。如果某个 worker unhealthy，先看容器内 heartbeat 文件时间、`worker_heartbeats`、容器日志和数据库连接，而不是先排查 backend API。

发布替换 worker 容器后，Recovery 先限定为当前 `executing` Action 的 lease owner，再以过期 heartbeat 的完整 `worker_id` 或 `hostname + pid` 匹配租约；heartbeat ID 末尾的角色后缀不参与 legacy 租约匹配。没有 executing lease 时不得扫描历史 heartbeat。未进入 Telegram Gateway 的旧容器执行项应立即按 `stale_worker` 回收，已进入 Gateway 的仍按 unknown 防重复口径处理。

发布 Stage B 的主动 fence 收口不能只看共享 `dispatch_claim_active`：专用 `worker-search-dispatcher` 的 current `search_click/search_join` executing Action 没有该标记。全部旧业务 worker 已停止且新 worker 尚未激活时，必须逐批行锁分类这两类集合；搜索 pre-Gateway 保留同一 Action/assignment 恢复 pending，Gateway-started 转唯一 unknown remote case 且 direct assignment 转 `gateway_unknown`，禁止重发、释放防重身份或改写目标；deadline 后必须正常进入 `closed_unknown`，不得遗留永久 executing assignment。读回仍有匹配 executing 行时不得 activate。

搜索 unknown 投影不能只存在于发布 fence：普通 stale worker、lease expiry、dispatcher DB error 与发布切换最终都必须由 fact-first finalizer 校验同一 Action/assignment/obligation 和 Gateway-started Attempt 后，幂等写 `gateway_unknown/unknown_after_send`；重复 finalizer 不增加 version。deadline closure 允许把仍绑定同一 unknown Action 的 legacy `executing` assignment 收口为 `closed_unknown`。生产历史终态 assignment 清理不得随发布静默批量执行，必须另走精确 preview/hash/apply/readback。

账号在线保活默认使用 `ACCOUNT_ONLINE_WORKER_DRAIN_LIMIT=1000` 作为单轮分页数量，使用 `ACCOUNT_ONLINE_PROBE_CONCURRENCY=32` 控制同一时刻的 Telegram 健康探测数，并使用独立的 `ACCOUNT_ONLINE_PROBE_TIMEOUT_SECONDS=30` 限制单个健康探测，不能继承普通业务 Telegram 调用的 300 秒超时。三者只控制处理吞吐，不是账号上线名额：全部 `desired_online=true` 账号都必须进入状态机，账号池超过单页时由后续 drain 继续处理，不得在服务内部再次按前 N 个账号截断。账号在线探测必须使用当前账号 `proxy_id` 对应的代理凭据，禁止从应用服务器公网出口直连；`login_required` 状态达到 `ONLINE_LOGIN_REQUIRED_RETRY_AFTER` 后仍须重新探测，不能永久排除在队列外。数据库读取和状态落库留在 worker 主线程，探测线程只执行 Telegram 网络调用；结果按完成顺序流式返回，主线程逐条提交，不能等待整页全部探测结束后集中落库。`last_probe_at` 保留各账号实际完成时间；同一 drain 批次的 `next_probe_at` 和成功 stale 窗口则统一不早于本批最后一个网络探测完成后的对应间隔，避免批次耗时超过 5 分钟时早完成账号已再次到期。每个探测线程通过 `check_account_health_isolated` 在本线程独立 asyncio 事件循环中执行一次性 Telethon client，不能把 32 路探测重新提交到 process-wide 生命周期；正常发送、监听和登录仍使用原业务生命周期及持久 client cache。健康探测 client 在 `finally` 中断开，30 秒探测超时后最多等待 5 秒有界断连和 1 秒调度余量，调用返回时清理已收口，且清理错误不得覆盖原始 Telegram 错误。生产验收需同时检查 account-online 批次没有成片 `account_health_probe_failed / TimeoutError`，探测期间 TCP 连接保持在配置并发附近并在批次后回落。并发和超时参数必须为正数，非法配置会使服务明确启动失败。

生产迁移和灾备切换必须保持单活：启动新主机任何 Telegram worker 前，先通过 SSH 脚本停止旧主机全部 `tgyunying-*` worker/backend，并独立回读旧主机不存在运行中的 compose 容器；再核对新主机 hostname、公网出口、`current` release SHA 和代理运行时。旧、新主机不得同时连接同一数据库并运行 Telegram worker。发现双活时先隔离旧运行栈、暂停 `account-online`，再做授权恢复；只重启容器会继续制造 `AuthKeyDuplicatedError`。

account-online 主线程冻结本批账号和凭证后必须先提交并结束读取事务，再启动 Telegram 调用；逐结果提交期间本批 ORM 对象保持已加载状态，不得因 `expire_on_commit` 触发逐账号隐式 SELECT。线上出现 `connection timeout expired` 且堆栈位于提交后的 ORM 属性读取时，按该事务边界检查，不能先扩大数据库连接池掩盖。生产验收同时要求没有 drain 级 `ConnectionTimeout`。

Dispatcher 若一次 claim 包含共享 `ai_generation_claim_token` 的 normal pending `send_message`，该 worker 会按领取顺序串行推进这一个 claim 批次，避免多个线程同时加载并更新重叠 Action 集合。生产验收必须检查 PostgreSQL 日志在发布后不再新增 `UPDATE actions ... deadlock detected`，并同时确认覆盖继续增长；该串行边界不是 action、账号或任务总量限制。

### Dispatcher/OCR 内存治理

2026-08-16 planner 全局 OOM 事故：自 08-15 16:31 起 planner 容器（无 memory limit）因 channel_like future 物化（单 Task 2109 条 pending、排至 2027-01）RSS 冲至 ~900MB，被全局 OOM killer 循环杀 29+ 次（RestartCount 最高 43），load 一度 516+，规划角色停摆。经用户批准按 `production-stability-and-fulfillment-remediation-prd.md` RC-0b/O0 执行三段止血：① 5 个 running channel_like Task、② 3 个 channel_comment Task 经 `pause_task` 服务受控暂停（actor=`prod-c0b-planner-oom-mitigation`，AuditLog 齐全，Action/在途未动）；③ like/comment 暂停后剩余物化源为 group_ai_chat 规划装载（AI lane 不可暂停），按 O0 加 4GiB `/swapfile`（`vm.swappiness=10`，fstab 与 `/etc/sysctl.d/99-tgyunying-swap.conf` 持久化）。02:06:25 后 OOM 停止、planner 存活、AI 活群规划恢复产出。硬边界：未部署 T2（source JIT + future 回收）前不得 resume 这 8 个 Task；禁止手工/定时重启掩盖；swap 使用持续超 512MiB 或持续 swap-in/out 按 `resource_capacity_degraded` 处置。事故基线与执行 artifact 见该 PRD §2.2/§2.3。

2026-08-16 晚间增量（PRD §2.4）：group_ai_chat 目标上调触发 `pacing_owner_immutable_conflict` 死循环——郑州师范/郑州楼凤因账号范围变化目标提升至 1064/1063，但已冻结 slot `pacing_plan_total` 停在 877/876，planner 每 30 秒全量重规划并回滚，两 Task 隐性停摆 4.5 小时零产出。经用户批准 pause 两 Task（actor=`prod-ai-pacing-conflict-mitigation`），pause 后冲突归零、load 0.89。**修复已发布**：release `f60256a0`（run `31958097254`，2026-08-17 00:27 上线）实现 freeze 单调上调迁移 + `PacingOwnerImmutableConflict` typed blocker 1 小时退避；00:33 两 Task 受控 resume（epoch 3），验证冲突 0、08-17 新账本 slot/target 自洽（1051/830）、恢复产出 Action、blocker 清除、planner 452MiB/load 2.5。planner 高 RSS 残留为长活进程历史峰值驻留，非活跃负载。

2026-08-14 事故复核确认生产 `ab1418cb` 的 OCR worker 仍在 partial timeout 时以退出码 70 终止 generation，且 Compose 仅以 `/health` 判活；累计重启超过 190 次并与 load 620、SSH banner 抖动及一次 global OOM 同窗。修复发布必须同时满足：partial completed source 被保留、timeout source 显式化、generation 进入 draining 并拒绝新请求、启动阶段预热 RapidOCR/ddddOCR、Docker 使用带内部 token 的 `/internal/v1/image-verification/ready`。只看容器 healthy 或公网 API 200 不得关闭事故；两路均超时仍按 unknown native state fail closed。

2026-08-01 硅谷生产 OOM 专项已按 `docs/03-feature-designs/dispatcher-ocr-memory-isolation-and-graceful-recycle-prd.md` 启用 Stage B；运维仍不得直接给 Dispatcher 加定时重启或手工循环重启。P0 删除每个验证码 challenge 的三路并发/假取消，RapidOCR/ddddOCR 各使用一个进程级固定槽；槽等待、OCR、最多一个模型的首请求/reasoning retry 共用同一 remaining budget，不建设 OCR 业务队列。late result、旧 fingerprint 或过期 challenge 禁止点击。Dispatcher 达到经 Release Gate 计算的资源阈值或收到 SIGTERM 后停止下一轮 claim，只有当前 futures、owned Action、open Gateway 与 Telethon client 全部收口才正常退出；运行时自动回收通过不参与 Action/session 的最小 rolling lease 保证单 shard，并由重启后的同 shard 首个成功 heartbeat compare-and-release 前任 lease。Stage B canary 已证明两个不同账号在 45 秒 contract 内由两路本地 OCR 同票并真实搜索成功；完整自然日、1287 次图片、模型 tail 和 3 次回收周期仍为 E4 `unproven`。

P1 已实现 Docker 私网单实例 `image-verification-worker`：使用独立 `Dockerfile.image-verification-worker` 和不可变 `TGYUNYING_IMAGE_VERIFICATION_IMAGE`，RapidOCR/ddddOCR/ONNX 与 `libgl` 仅安装在该镜像，Backend/Dispatcher 镜像不再携带 native OCR 依赖；生产启用验证码 contract 时 `IMAGE_VERIFICATION_OCR_BACKEND` 必须为 `remote`，`local` 仅保留给开发与未启用 contract 的测试。Worker 使用 deterministic request ID、同步 POST、最小状态 GET、worker generation 和内存状态；running 不被 TTL 驱逐，terminal TTL 不短于最大请求预算加恢复观察窗，busy 立即拒绝，不部署等待/持久队列或 HA，也不允许 Dispatcher native fallback。普通 `/health` 只证明进程存活；发布检查必须带私网 token 调用 `/internal/v1/image-verification/ready`，实际初始化 RapidOCR/ddddOCR，证明 non-root/read-only 容器内两引擎可用。正常回收仅在当前请求终态后，由 `IMAGE_VERIFICATION_WORKER_RECYCLE_REQUEST_LIMIT` 或 `IMAGE_VERIFICATION_WORKER_SOFT_RSS_BYTES` 任一阈值触发；`IMAGE_VERIFICATION_WORKER_MEMORY_LIMIT` 只是 hard protection，不能替代软回收。`deploy/docker-env.sh` 仅在 `IMAGE_VERIFICATION_CONTRACT_ENABLED=true` 时加载 `docker-compose.dispatcher-runtime.yml`，仅在 `IMAGE_VERIFICATION_OCR_BACKEND=remote` 时加载 `docker-compose.image-verification.yml`；两个 override 对 memory limit、stop grace、token、payload/deadline 边界均 fail closed 要求显式环境变量。上线前必须用至少两个受控账号执行 immediate、本地 OCR p95、OCR+模型 tail 三个真实正确 callback 档位，只有两个账号都 accepted 的最慢档位才能成为校准证据下界，并据此填写 callback/headroom/model tail、Dispatcher recycle/hard limit、worker soft/hard limit/TTL/stop grace；页面 `>=70.42s` 可见不能代替 callback accepted。swap 或 hard limit 只能降低整机失控风险，不能替代业务安全 drain、OCR 隔离、内存 soak 或真实搜索 ledger 验收。

验证码 contract 的生产参数由 GitHub Actions repository variables 管理，私网 token 由 repository secret `TGYUNYING_IMAGE_VERIFICATION_WORKER_TOKEN` 管理；`Deploy Production` 将本次不可变配置与镜像 SHA 一起写入 release 私有 `.image.env`，不在服务器手工补代码或漂移共享 `.env`。首次发布必须保持 `IMAGE_VERIFICATION_CONTRACT_ENABLED=false`，先证明新代码、迁移、镜像与既有任务运行正常；完成上述 callback canary 后才把该变量切为 `true` 并重新 workflow-dispatch 同一 release。启用 run 若 readiness、参数关系、worker memory、Dispatcher drain 或 post-deploy checks 任一失败，Actions 必须失败并保留上一稳定 release 作为回滚锚点。

2026-08-14 release 合同回写：`314c6d9d` 的 Deploy Production run `31809489025` 已执行到 `deploy/release.sh` live release 阶段，但 workflow 后续重复运行全量 `scripts.takeover_all_task_fulfillment` 并超时。该失败不是 OCR worker 代码合同失败，而是发布顺序偏差。全量 takeover 只能由 `deploy/compose-up.sh` 在 Stage B 零业务 writer 窗口内执行；`deploy/release.sh` 返回后，workflow 只能保留有界只读 `verify-active`、OCR functional `/ready`、容器与公网健康检查。出现类似“release live 后 gate 失败”时，先标记 `release_gate_failed / production_fixed_unproven`，再按失败 gate 修复；不得二次手工跑全量 takeover 来冒充发布验证。

Recovery 必须依次提交前序 Action 修复、连续 Task 状态修复，再进入 stale Action claim，确保任一提交都不会同时刷新 dirty Task 与 Action。线上若 `worker drain failed` 同时出现 `UPDATE tasks` / `UPDATE actions ... deadlock detected`，应检查这三个事务边界；不得靠扩大连接池、降低 worker 数量或限制账号总量掩盖。

## 共享调度与 AI 履约恢复发布合同（2026-08-01）

本节对应专项 PRD `shared-dispatch-and-ai-fulfillment-recovery-prd.md` 和 DF-324。生产唯一合同固定为两个 Dispatcher shard、每 shard实际并发13、scope总容量26：`DISPATCHER_CONCURRENCY=20`、默认数据库连接预算`5+10`、`DB_POOL_CONTROL_RESERVE=2`、`DISPATCH_RUNTIME_SHARD_TOTAL=2`、`DISPATCHER_SCOPE_CAPACITY=26`。shard heartbeat超过`DISPATCH_SHARD_STALE_SECONDS=120`后不再取得新份额；禁止跨 shard接管账号。fingerprint schema为`dispatch_topology_v1`，rebuild contract为`dispatch-rebuild-v3`。这些值在`.env.production.example`、`docker-env.sh`和server compose中必须完全一致；遗留共享`.env`中的容量52或`ENABLE_EMBEDDED_WORKER=true`会被Settings与发布入口双重拒绝，不能静默沿用。planner、ai-generation、两个dispatcher和recovery使用compose稳定worker ID；业务drain heartbeat只能合并metadata，不能删除合同版本；新worker退出必须把自身heartbeat显式写为`stopped`。

`deploy/compose-up.sh`是唯一自动发布顺序：

1. Stage A：停止全部旧worker，在stop完成后记录纳秒UTC边界，只启动backend执行0134迁移；先用`python -m scripts.manage_shared_dispatch_contract retire-stopped-writers --actor <actor> --approval-ref <ref> --stopped-before <cutoff>`只退役该边界前仍为active的fenced writer heartbeat并写审计，再用`stage`建立preparing候选。随后启动新worker进入fenced readiness，两个Dispatcher heartbeat都就绪后执行`verify-ready`。不得靠等待120秒自然过期、删除heartbeat或忽略fresh旧writer通过闸门。
2. Stage B：在全部业务writer仍为零写的前提下执行`reconcile-ledger`，再执行全任务takeover；AI scope必须先`takeover_ai_content_scope preview`取得`batch_id`、`classification_hash`和完整`classification_counts`，apply时原样提交这三项及actor/approval。中途退出后继续同一batch的pending item；出现drift/conflict不得激活。
3. Stage C：只有整个takeover batch chain completed、账本守恒且无旧writer时，才执行`activate --takeover-head-batch-id <id>`。事务提交后必须立刻执行只读`verify-active`，再次证明active/candidate版本一致、两个shard live、configured/live capacity均为26且账本守恒；只看容器healthy、`status`或`verify-ready`均不构成激活成功。

`Deploy Production` workflow 不再拥有独立的 all-task takeover 步骤；它只能调用 `deploy/release.sh`，并在该脚本完成后执行只读、短时、有边界的运行验证。若未来需要改变 takeover 范围或重跑策略，必须先更新本节 Stage B/C 合同和对应 PRD，再改 workflow；禁止把长耗时数据接管脚本放在 release live 后的 post-deploy health check 中。

远端不确定结果通过受保护的`Production Remote Reconcile` workflow处理：preview 固定生产 release 与 case 后只读取证，默认来源为脱敏 Gateway journal，并同时输出 case expected 与当前 Action/Attempt hash；需要 Telegram 历史时显式选择`telegram-history`。普通 apply 必须提交 preview 返回的完整`evidence_fingerprint`以及 actor/approval ref，workflow 再次核对同一 production symlink 后才执行。若 case 已因持久表示规范化漂移进入 conflict，只有 evidence fingerprint 未变且审批者同时提交 preview 的当前 Action/Attempt hash 时才可勾选`resolve_conflict`；workflow 会写冲突复核审计并在同一事务执行统一 CAS，输出 conflict/inconclusive 必须失败。禁止绕过 workflow 在生产直接 apply，禁止把发送型 unknown 交给 worker 自动重发。journal 不保存消息正文、Prompt、peer 凭证或授权资料，只保存冻结请求/目标 hash、远端 mutation 边界和类型化 fact：send/comment 使用新`remote_message_id`，view/reaction 没有新消息 ID，使用冻结源消息`remote_fact_id`重建唯一`ViewRemoteFact`/`ReactionRemoteFact`；membership 权威 reprobe 按冻结`require_send`确认已加入可访问或可发言并写 joined，群管 follow/callback 重放对应 admission 事实。存量 membership unknown 无 Attempt 时先持久建立 read-only recovery Attempt/Case 再 probe；所有类型完整 canonical Action payload 均进入 request hash，账号、目标、claim、reaction、源消息或准入版本漂移一律 conflict。

只有Gateway的权威`remote_mutation_started=false`才能证明远端未发生。Telegram历史没有找到、超时或歧义都只能是inconclusive，禁止自动重发；AI的权威no-mutation会保留旧Attempt审计并原子清空原CycleSlot action、重开quantity slot、释放message memory和失效unknown stance，再由原义务重规划。exact唯一远端fact才允许confirmed并原子同步任务专用账本。

回退边界：Stage A尚未修改业务Action时，可停止候选worker并回到上一不可变release；0134为前向迁移，不能对已迁移数据库执行应用降级或猜测回填。Stage B已提交的takeover item不得反向改写，只能按batch状态断点续跑或由新preview显式supersede。Stage C激活后若Release Gate失败，保持事故状态为`production_blocked`并基于真实账本修复，禁止清库、绕过fence或自动重发unknown。

截至2026-08-01，本合同只有本地候选实现和定向no-PostgreSQL证据；测试PostgreSQL不可连接，PostgreSQL并发/迁移实库证据为blocked。GitHub Actions、生产`verify-active`、30分钟deadlock=0、Telegram canary及完整自然日五类任务E4均未执行，不能标记`qa_pass`、`product_accepted`或`production_fixed`。

## Nginx

参考配置在 `deploy/nginx/tgyunying.conf.example`。

核心代理口径：

- 静态前端：`root /data/infra/www/<域名>/current`
- 静态资源：`/assets/` 必须开启 7 天 immutable 缓存，并开启 gzip；首屏 JS 裸传会显著拖慢跨境和代理链路加载。
- 后端 API：`/api/ -> http://127.0.0.1:18090/api/`
- 媒体文件：`/media/ -> http://127.0.0.1:18090/media/`
- 健康检查：`/healthz -> http://127.0.0.1:18090/api/health`

## 发布验证

当前生产相对稳定版本基线见
[`2026-07-31-production-stable-baseline.md`](../../05-implementation/multi-agent-practice/runs/2026-07-31-production-stable-baseline.md)。
该记录使用不可变 commit SHA、生产 release ID、Deploy Production run 和线上只读核对证据，
作为下一版本开发及必要回退时的比较基点。

发布后脚本会区分三层状态：

1. 容器层：`tgyunying-backend` healthy，`tgyunying-worker-planner`、`tgyunying-worker-dispatcher-1/2`、`tgyunying-worker-listener`、`tgyunying-worker-recovery`、`tgyunying-worker-account-security`、`tgyunying-worker-metrics` healthy；4 核生产机固定使用 2 个 dispatcher / 2 个账号分片，避免 4 个 claim worker 在共享 scope 与 actions 索引上形成 CPU、IO 和行锁争用
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
- 若 hard-hourly Planner / PostgreSQL CPU 持续升高，确认 Alembic 至少为 `0110_hard_hourly_recovery_cpu`。最近 24 小时历史必须用 `executed_at` 分支和“仅 scheduled_at 命中”分支的非重叠 `UNION ALL`，分别命中 `ix_actions_hard_hourly_history_executed` 和 `ix_actions_hard_hourly_history_scheduled`；每条 Action 只能归类一次，不能为 24 个小时桶反复扫描完整列表；wake 选中的任务必须把 progress 快照带入同一轮后续 planner Session，不能跨 Session 重算。
- 若 metrics / PostgreSQL CPU 在五分钟采集窗口持续升高，确认 Alembic 至少为 `0111_metrics_summary_anchor`。AI `voice_profile_anchor_rewritten=true` 计数必须使用静态 JSON 条件并命中 `ix_actions_task_voice_anchor_fact`，不得扫描任务全部 `send_message` 历史；metrics 已按五分钟采集，不能只靠继续降低采样频率掩盖索引缺口。
- 若 Recovery / PostgreSQL CPU 持续升高，先比较 `actions.status='executing'` 数量和 stale heartbeat 数量。stale heartbeat 历史很多而没有 executing lease 时，恢复路径不应读取 heartbeat 表；有 lease 时只查询精确 owner，分别使用 `ix_actions_executing_lease_owner`、唯一 `worker_id` 与 `ix_worker_heartbeats_host_pid_last_seen_at`。hard-hourly membership recovery 只读取未设置或已到期的 `hard_hourly_next_check_at`，未来 checkpoint 不得在每个 recovery drain 重扫 membership Action 历史；同一轮 pending hard-hourly membership fast-track 只应执行一次。
- 若 Recovery / PostgreSQL CPU 持续升高，检查 `actions` 的过期明细清理和 `runtime_cleanup_audits` checkpoint 查询。前者必须命中 `ix_actions_runtime_detail_retention`，空结果不得扫描完整 Action 历史；后者必须命中 `ix_runtime_cleanup_audits_kind_created_at`，不能每轮 JSON 全表扫描审计记录。`RUNTIME_DETAIL_CLEANUP_INTERVAL_SECONDS` 与 `RUNTIME_METRIC_CLEANUP_INTERVAL_SECONDS` 默认均为 300；installer 只会将共享 `.env` 中旧默认的精确值 `60` 升级为 `300`，其他显式值保持不变。Action 留存仍为 5 天，不能通过缩短留存替代索引和节流修复。
- 若 PostgreSQL 日志持续出现 `DELETE FROM actions` 外键错误，必须按目标 Action 集合核对所有 `actions.id` 外键。`task_hard_hourly_delivery_credits` 等从属运行明细应先删除，`ai_coverage_variation_intents.action_id` 等长期审计引用应先置空，再删除 Action；不得让 Recovery 每轮重试同一失败批次，也不得关闭外键或直接级联删除覆盖账本。
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

## 2026-08-01 每日履约生产诊断

`group_ai_chat` 当前生产合同为群日/全账号每日履约，历史 hard-hourly 仅审计。
`run_production_diagnostics` 的 Planner 探针必须调用公开 `drain_task_planner`；任何
异常直接失败。随后运行 `.github/scripts/task_fulfillment_e4_diagnostics.py` 读取
事故 Task 的自然日账本、Attempt 与类型化远端事实。旧的
`_wake_hard_hourly_tasks/_hard_hourly_due_candidate` 不得再出现在生产 workflow。

AI 生成运行三个独立容器：`tgyunying-worker-ai-generation`、
`tgyunying-worker-ai-generation-2`、`tgyunying-worker-ai-generation-3`。三个实例只提供
跨群并行；同一 `tenant_id + group_id` 最多一个 generating/ready open Action。发布健康
检查必须逐一验证三个容器，心跳必须使用不同 worker ID。

事故任务发布后的持续观察使用 `.github/workflows/production-task-monitor.yml`，不得为了
再次读取 E4 而重跑 Deploy Production。监控 workflow 要求显式 `deployed_sha` 并校验
`/data/tgyunying/current` 短 SHA，随后只读取既有容器 health，并将 `.github/scripts/task_fulfillment_e4_diagnostics.py`
通过 stdin 送入当前 backend 容器执行只读查询；禁止 build/pull/restart、Planner drain、
claim 或数据库写入。`release_live_at` 必须显式传入原发布锚点，监控启动时间不得替代发布
时间。监控失败表示仍有业务 blocker，不授权自动发布或重启；通过仍须同时满足五个 Task
当前自然日账本的权威远端事实。

### 评论与 AI 活群引用比例线上校正

运行中任务的引用比例校正使用
`.github/workflows/production-reply-ratio-control.yml`。必须先以 `apply=false` 预检，workflow
会校验当前 production release SHA，以及 backend、Planner、三个 AI 生成 worker、两个
Dispatcher、Listener 和 Recovery 健康，再读取运行中评论、AI 活群任务的配置、未完成
Action 与最近真实远端成功事实。监控同时输出未完成 Action 的最早/最晚计划时间、目标
任务的 Planner 排队位次和 Planner 心跳；不得把已过期 `next_run_at` 仅解释为配置问题。
评论 10% 换算为
`ceil(target_comments_per_message × 10%)` 的 `reply_min_per_message`，并要求
`comment_mode=mixed`；AI 活群 20% 换算为 `ceil(messages_per_round × 20%)` 的
`reply_min_per_round`。不得把百分数 `10`、`20` 直接写入整数下限。

只在预检显示 `needs_change=true` 后以 `apply=true` 写入。写入复用正式任务配置更新路径，
因此会校验完整类型配置、增加连续性 revision、清除未完成计划并立即重排；不会修改已经
取得远端结果的 Action。配置复核 `remaining_mismatch_count=0` 只表示设置已生效，业务恢复
仍必须在变更锚点之后看到 `ExecutionAttempt.status=success`、非空 `remote_message_id`，且
对应 Action payload 的 `reply_to_message_id` 非空。

## 2026-08-04 fact-first_v3 全任务切换

本次切换不迁移旧 Task。先部署 `0137_fulfillment_v2`、`0138_physical_delete_hot_indexes`、`0139_task_delete_fk_indexes`、`0140_task_delete_recursive_fk_indexes` 与新 worker，再使用
`backend/scripts/manage_fulfillment_v3_cutover.py` 对仍在运行的旧 Task 建立同配置
`prepared` 新 Task；AI Task 必须绑定租户唯一默认 Provider，四个群目标保持
4000、5000、800、800。新 Task 的当日账本从 0 开始，暂停、停止、已完成和已软删除
旧 Task 不建替代任务，但必须进入 manifest 的 old set。

切换固定为以下顺序：

1. `inventory` 冻结精确旧 Task 集合；`create-prepared` 只创建仍需继续执行的替代 Task。
2. `prepare-manifest --apply` 只启动一个 canary；canary 必须形成任务类型匹配的
   `FulfillmentRemoteFact`，健康检查、Action success 或本地测试均不能替代。
3. `activate` 通过 manifest version CAS 一次切换路由；所有新 Task 立即 running，旧 Task
   立即 stopped，Gateway 同时校验 route、Task 状态与 `task_lifecycle_epoch`。
4. `delete-manifest` 按 Task 分事务执行 fencing、运行快照、tombstone 写入与复核、物理删除。
   每个失败阶段保留 `TaskDeleteOperation.resume_stage/stage_version`，只允许从同阶段继续；
   不得跳过仍在 claiming/executing 的 Action，也不得扩大 manifest 外的删除范围。

新合同验收必须同时证明：四个 AI Task 均有独立进行中的 Action/Attempt 且持续新增
`remote_message_observed`；搜索 Task 一 Task 一目标、由 `search-dispatcher` 执行并产生
`target_click_observed`；C2 事实按账号和 observation surface 归属；无法发送的账号只在当前
Task/当日被放弃；目标解散或引用失效终结该 Task；旧 Task 与其 Action/Attempt 已不存在，
仅保留 manifest、删除操作审计与必要远端防重 tombstone。以上任一项未证明，状态保持
`production_unproven`，修复后重新发布并从当前可恢复阶段继续。

## 2026-08-19 群监听事件唤醒与发送目标守恒

群监听采集写入新 `GroupContextMessage` 后，必须同时出现
`TaskPlannerWakeState.reason_code=group_context_inserted`，且 `not_before_at` 不晚于监听采集时间。
只看到 `Task.next_run_at` 提前不算通过；0153 后 Planner 以持久 wake 为调度真相源。空采集不得
增加 wake revision。该 wake 只触发重新规划，relay 的来源、目标 operation target、目标 peer、
内容过滤、fingerprint 去重和 Dispatcher 发送路径保持不变。

发布验证必须只读比较新增事件前后的 wake revision、Action 的目标 ID/peer 与远端结果；
`unknown_after_send` 不得重试。没有部署后真实新增来源消息时，事件到目标的 E4 仍标记
`unproven`，不得用容器健康或本地测试替代。

Listener 新增 wake 后，生产日志还必须验证 Planner/Listener 在 `task_planner_wake_states`
和 `tasks` 上 deadlock=0。group-ai Planner 的中间提交会释放首个 wake 行锁，因此重载 Task 后
必须先再次 `mark_task_planner_started`，再进入 build/flush；只在异常捕获后重试不能替代锁序修复。

## 2026-08-22 A 保护的 ABC 两账号 canary

生产执行始终从 runtime=`off` 开始，并且账号逐个执行。SSH 只调用当前 backend 容器中的正式脚本，不传任何 Telegram secret：

```bash
bash deploy/authorization-abc-backup.sh --mode preview --tenant-id <tenant> --account-id <account> --idempotency-key <unique-key>
bash deploy/authorization-abc-backup.sh --mode apply --tenant-id <tenant> --account-id <account> --idempotency-key <unique-key> --expected-fingerprint <preview-fingerprint> --requested-by <requester> --approved-by <different-approver> --approval-ref <ticket> --runtime-image-sha <deployed-full-sha>
bash deploy/authorization-abc-backup.sh --mode status --tenant-id <tenant> --account-id <account>
```

`apply` 先在 SV 创建 B，再为同一账号创建并只开放一个 C operation；C 成功后 runtime 自动回到 `off`。B/C 失败不得切换、覆盖或撤销 A。出现 `reconcile_unknown`、A generation/fact/connection 漂移、非预期远端设备、MY capability/SHA 不匹配或 runtime 未自动关闭时立即停止，不执行第二账号。第一个账号只有在 A/B/C 分别完成 Telegram 授权读回，A 完成 Saved Messages 发送读回，C 双副本和 restore probe 通过且 MY active client=0 后才算通过；两个账号通过前不得建立 10 账号批次。

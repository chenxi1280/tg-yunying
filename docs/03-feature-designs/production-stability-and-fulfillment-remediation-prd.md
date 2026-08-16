# 硅谷生产 CPU / 内存稳定性与履约完整修复 PRD

## 1. 文档状态

| 项目 | 内容 |
|---|---|
| Intake ID | `intake-2026-08-15-production-stability-remediation-001` |
| 问题级别 | L3 / P0：宿主 CPU / 内存临界、Planner/Dispatcher 持续负载、OCR 常驻与请求突发、AI 活群与纯搜索履约未达目标、点赞链路半瘫、TG 登录代码缺陷、Worker 零日志 |
| 设计状态 | `product_design_complete / resynced_2026-08-16（含 C0b planner 物化止血合同）` |
| 适用范围 | 硅谷生产（47.77.184.233）宿主机、全部应用容器、planner/dispatcher/generation/OCR/登录链路 |
| 明确排除 | API 安全层（webhook secret、/media、审计手机号等）另行立项；本产品单租户运行，多租户隔离类问题不进入本次范围 |
| 关联文档 | [task-fulfillment-classified-recovery-prd.md](task-fulfillment-classified-recovery-prd.md)（评论/点赞来源义务与 `due_by_now`）、[ai-group-generation-failure-churn-remediation-prd.md](ai-group-generation-failure-churn-remediation-prd.md)（current AI 唯一合同）、[channel-view-planner-starvation-remediation-prd.md](channel-view-planner-starvation-remediation-prd.md)（current view 唯一合同）、[dispatcher-ocr-memory-isolation-and-graceful-recycle-prd.md](dispatcher-ocr-memory-isolation-and-graceful-recycle-prd.md)（OCR 隔离、takeover owner 与回收）、[account-login-group-navigation-recovery-prd.md](account-login-group-navigation-recovery-prd.md)（登录 flow 合同）、[ai-group-provider-fallback-and-safe-prompt-design.md](ai-group-provider-fallback-and-safe-prompt-design.md)（Provider adapter，运行时为单 active key） |
| 证据状态 | `historical_production_readonly_verified / partially_refreshed_2026-08-16`：2026-08-15 资源、数据库、容器日志和发布代码为直接只读证据；2026-08-16 凌晨经 SSH ControlMaster 短窗完成宿主/容器/dmesg 只读复核与 C0b 止血（见 §2.2，`observed`）；OCR `/ready` 与完整资源 soak 复读仍 `blocked/production_unproven`；历史 TypeError 在当前发布后的复现仍为 `unproven` |
| 本次目标 | 先用精确搜索停流量与 OCR 安全排空解除整机风险，再根治 Planner/Dispatcher 查询与物化放大、OCR 4+3 变体计算和重启不可解释问题；同时恢复 AI/搜索/点赞/浏览执行链路、收口登录存量 flow、修复 Telethon 与 Worker 可观测性、补齐发布 E4 闸门；不改变 fact_first_v3 履约合同、typed remote fact 语义与唯一 active Provider 运行时约束 |
| 2026-08-16 新增原始需求 | “线上不只是内存，CPU 也很高；确认是否同一问题、是否本服务导致；近期 OCR 调用少，能否先停或降负载；整理全部问题并形成完整修复 PRD” |

### 1.1 当前合同与变更边界

1. 本文是生产事故修复的编排与 Release Gate，不复制第二套履约真相源。冲突时按下列唯一 owner 执行：
   - AI 数量、义务、GenerationJob、variation、Action writer、duplicate/reopen 与 fleet takeover 只认 `ai-group-generation-failure-churn-remediation-prd.md`；正常正文在 accepted variation 与 message memory ready 前 `Action=0`。
   - `channel_view` 的 message target、due ordinal、binding、route/lifecycle epoch、migration 与 settlement 只认 `channel-view-planner-starvation-remediation-prd.md`。
   - `channel_comment/channel_like` 的 source-scoped ordinal、滚动 24 小时 pacing、`due_by_now` 与 recovery 只认 `task-fulfillment-classified-recovery-prd.md` §4.5/§11.2。
   - 全量 takeover 的唯一自动 owner 是 `deploy/compose-up.sh` Stage B（`scripts.takeover_all_task_fulfillment` preview/apply 调用块）；release 返回后只允许有界只读 `verify-active`，不得设计第二个 takeover 调用者。
2. 本文只增加 incident-scoped 的 Provider admission、查询/轮询治理、资源与日志、受控存量修复、登录/Telethon/Gateway 安全修复和 L3 E4 编排；不得借事故快修恢复 legacy Action-first AI、账号天然键 view 或 future-tail source 排期。
3. 实现发现上游专项尚未部署时，相关 release train 标记 `blocked_by_authoritative_contract`；不得以兼容分支绕过 migration/fleet gate。任何状态名、唯一键、writer owner 或迁移顺序与上述专项不一致时，开发必须停止并触发 product `resync`。

### 1.2 2026-08-16 执行结论

| 用户问题 | 产品结论 | 证据边界 |
|---|---|---|
| CPU 和内存是不是同一个问题 | 不是单一泄漏，而是同一台小规格宿主上的多根因叠加：Planner/Dispatcher 持续查询与物化同时吃 CPU/RSS；OCR 双 native 引擎常驻 RSS、单请求并行吃 CPU；旁路容器与无 swap/限额放大整机风险 | Planner/OCR/宿主快照为 `observed`；每个进程的精确 heap/native allocation 仍为 `unproven` |
| 是不是本服务导致 | 平台服务是主要贡献者之一，尤其 Planner、OCR、Dispatcher；但 61 个 mihomo 与旁路应用也持续占用宿主，不能把整机问题归给单一容器 | 只按逐容器 cgroup 与宿主进程证据归因，不按 UI 或容器数量猜测 |
| OCR 最近没调用为何仍高 | 双引擎在启动时预热并由进程级缓存常驻，所以约 500MiB 空闲 RSS 可以是正常 warm baseline；健康检查缓存命中，不执行验证码推理。若“无 request + generation 稳定”仍持续高 CPU，则不正常，必须区分重启预热循环、遗留 active native 调用和监控窗口误判 | 当前无请求持续高 CPU的具体一项原因尚未完成线上相关性读回，状态 `unproven` |
| OCR 能否先停 | 可以作为 P0 临时止血，但必须先固定并暂停全部精确 consumer Task，等待 Action/Assignment/ProtocolSession/Gateway/OCR request 排空，再按审批只停独立 OCR overlay；不能直接 kill，也不能停整个业务 compose | 停服只证明释放 OCR 资源，不证明搜索履约恢复 |
| 怎样长期降负载 | Planner JIT/有界查询优先解决持续负载；OCR增加请求/重启观测和per-source `adaptive_variants_v2`，各引擎先跑base、校准不通过再补自己的完整变体，使符合predicate的请求可从4+3次推理降为1+1；资源预算、swap和hard limit只做故障隔离 | fast predicate必须先shadow/canary，无准确率或callback E4回退才可全量 |

## 2. 历史生产事实基线（2026-08-15 12:25～12:30 北京时间复核）

本节所有数量均为时点快照，不得作为长期常量。证据分为：`observed`（本轮直接读到）、`inference`（多项证据支持但缺少原始明细）、`blocked`（访问边界阻断）和 `unproven`（现有证据不足）。

> 2026-08-16 resync：当前线上 SSH 在认证前的 banner exchange 仍受阻，本节数值没有被升级成“现在值”。实现、止血与发布前必须重新生成相同口径的只读 artifact；无法访问时只允许写 `blocked/production_unproven`，不得用本节旧数值、健康检查或发布 SHA代替当前资源事实。

部署事实：

- 宿主机 4 CPU、7.3GiB 内存，used 6999MiB / available **218MiB**，**无 swap**，load `3.71/3.79/3.64`；该快照 boot 未检出 OOM victim，但不能据此降低下一次尖峰风险。
- 该快照 61 个 mihomo合计约858MiB（另有 clash-ss）；tg-v-chat / tg-reporter等旁路应用继续与主平台争用同一宿主资源。
- 当时发布 SHA `b517e1cf`，生产 symlink 指向 `20260815021109_b517e1cf`；Deploy Production成功，但其中`Probe production planner drain`与`Verify incident task fulfillment E4 facts`均被跳过，发布成功不构成履约恢复证明。
- 该时点 PolarDB（108/500连接）、Redis有密码；数据库侧无瓶颈证据。
- 当时 Provider表只有 MiniMax-M3 active，但其health check停留在2026-07-30；配置active不等于当前可用。`resolve_tenant_id()`固定返回1在已确认单租户合同下不构成本次缺陷。

资源与队列事实（`observed`）：

| 消耗者 | 快照值 | 结论 |
|---|---:|---|
| worker-planner | 896.3MiB / 80.77% CPU，无 memory/CPU limit | 当前最大单进程内存与持续 CPU 消耗者 |
| image-verification-worker | 497.3MiB / 768MiB，173.11% CPU，RestartCount=37 | 双 native OCR warm RSS与单请求多变体 CPU 峰值均可解释部分现象；37 次重启原因未分类，仅凭计数不能定性为频繁主动回收、OOM 或内存泄漏 |
| dispatcher-1/2 | 325.4MiB / 331.5MiB，各限 512MiB | 均已超过上限 63%，仍有冲顶风险 |
| 61 个 mihomo | 合计 858.4MiB，均值 14.1MiB | 数量型常驻开销显著 |
| 核心应用容器 | planner、AI generation、dispatcher 等默认 root；除 dispatcher/OCR 外无 memory limit | 故障隔离不足；OCR 例外为 UID/GID 65532 且限 768MiB |

- 数据库 `actions=182799`、`group_context_messages=1203828`；快照 live Task 19个，其中running且due 15个。
- future pending共**2757**：`like_message=2384`（最远2026-12-31 06:13）、`post_comment=373`（最远2026-09-01）。快照due pending的like_message为0，但这只说明它们被排到未来，不是吞吐恢复。

履约事实：

- AI 大目标群仍系统性欠产：8-13 已核对样本达成约 40%～65%；学生会、天津在 8-14 分别约 33%/34%。小目标群 8-13/8-14 基本完成，8-15 截止快照也基本完成当前 due，支持“容量而非任务配置”判断。同标题多 group_id 使郑州大学/西安的 8-14 精确拆分暂为 `blocked`，不得按标题宽聚合宣称单群结果。
- 当日 Action 主错误码确认 **`ai_generation_failed=498`**；唯一 active Provider 为 MiniMax-M3，3 个 generation worker 又缺少共享 Provider 限流，因此“429 配额瓶颈”为高可信 `inference`。由于主错误码已折叠、核心日志为空，不能写成“498 条全部已逐条证明为 HTTP 429”。
- 内容重复 `10d_similar=454`、`10d_exact=421`，合计 **875**；该安全门本身正确，但高目标下重复碰撞已成为第二容量瓶颈。
- search_click 当日 `failed=1803 / success=756`，756 条已有 `target_click_observed` E4；1798 个 Attempt 首先停在 `jisou_image_verification_required`，其中仅 323 个不同 Action 能直接检出 `verification_local_ocr_timeout`。因此“验证码/OCR 边界是主阻塞”成立，“100% 都是 OCR 状态查询超时”是 `unproven`。
- channel_like 近 2 小时执行 14 条、ReactionRemoteFact E4 11 条；逐日 E4 为 8-10=356、8-11=91、8-12=114、8-13=100、8-14=102、8-15 截止快照=38。结论是持续显著降速，不再沿用“8-10=618、8-12 起归零”的不一致口径。
- channel_view 按 distinct Action 复核为 **258 条失败**，全部为目标实体解析失败，集中在“郑州楼凤”单 Task；多 Attempt join 得到的 963 行不作为 Action 数。

登录事实：

- tg_accounts：在线 865 / Session失效 176 / 需重新登录 36 / 等待验证码 30 / 等待2FA 6 / 异常 3 / 待登录 1。
- 近 7 天登录 flow 失败：**`TypeError: bytes or str expected, not NoneType` 2 次**、验证码错误或已失效 4 次、手机号被 TG 永久封禁 1 次。
- 全部历史 `waiting-code + challenge_sent_at=NULL` 为 134 行；按当前 30 个等待验证码账号的“最新 flow”缩窄，29 个缺 challenge，其中 **21 个**仍是迁移前 shape，才是本次存量修复集合。
- `b517e1cf` 上线后新建的 2 个 flow 均有 `challenge_sent_at`；仍在 waiting-code 的 flow 同时有加密 temporary session/hash，说明新 challenge binding 路径通过。当前代码已显式拦截空 session/hash；2 次 TypeError 是历史事实，但发布后是否复现仍为 `unproven`。

可观测性事实：

- planner / 3 个 ai-generation / account-security / recovery 的 Docker json-file 日志为 0 字节；backend 总计仅 488 字节且近 2 小时为 0；dispatcher-1/2 近 2 小时仅 3465/259 字节。0 字节不代表 Worker 未运行，但正常 drain、阶段耗时与错误上下文不可观测。
- `worker_heartbeats` 中存在 `status=active` 但 `last_seen` 已过期 11～14 天的历史行；任何健康投影必须同时检查 freshness，不能只按 status 计数。
- search-dispatcher 当前容器 `Server closed the connection` 累计 **655 次**；它证明代理/Telethon 链路不稳定，但不能单独证明缓存失效缺陷是全部 655 次的唯一原因。
- OCR `/ready` 最新复核被 SSH banner timeout 阻断，状态为 `blocked`；Docker `healthy` 不能升级为 RapidOCR+ddddOCR 双引擎 ready。

### 2.1 CPU、RSS、调用量与重启必须分开判定

| 现象 | 正常解释 | 异常判据 | 必需证据 |
|---|---|---|---|
| OCR 空闲 RSS约 500MiB | lifespan 预热 RapidOCR/ddddOCR，`lru_cache(maxsize=1)` 保持 native 模型与 mapping | 同输入/同请求量下跨 generation 基线持续抬升，或达到 hard limit/OOM | generation、uptime、RSS/cgroup、启动峰值、请求终态后基线与 recycle 原因 |
| OCR 单请求接近 200% CPU | RapidOCR 原图+3蓝色 mask、ddddOCR 3 crop 在两个单线程 executor 并行 | 无 active request、无 generation 变化仍连续高 CPU；或单请求超 deadline 后线程继续运行 | request started/completed、active age、逐 source/variant duration、cgroup CPU delta |
| `/ready` 每 15 秒调用 | 引擎已预热时只命中缓存，证明 functional readiness | 每次 `/ready` 都重新初始化说明 generation/restart loop或缓存生命周期异常 | generation/uptime、ready duration、restart delta、启动日志 |
| `RestartCount=37` | deploy recreate、soft recycle、deadline drain 均可能增加 | 原因缺失、OOMKilled、exit 70、healthcheck kill或短周期 restart loop | primary reason、trigger、exit code、OOMKilled、signal、old/new generation、owned work |
| Planner/Dispatcher 持续 CPU/RSS | 有 due work时允许有界处理 | `due_open=0` 仍高频扫描、查询数随 Task 变为 2N、一次 materialize 全量 future Action | 每轮 SQL数/耗时/候选数、due分类、RSS/CPU、最早下一 due |

### 2.2 2026-08-16 凌晨复核基线（00:00–01:45 北京时间，`observed`）

SSH banner 交换持续超时与发布脚本 `Connection timed out during banner exchange` 签名一致；诊断经 SSH ControlMaster 复用短窗完成，全部为只读取证（C0b apply 除外，见 §2.3/RC-0b）。

- 当前 release symlink `/data/tgyunying/releases/20260815130559_621e1a00`，晚于 §2 基线的 `b517e1cf`；宿主 4C/7.3GiB 无 swap，used 6351MiB / available 760–866MiB，load 一度 `516/566/488`。
- **Planner 全局 OOM 风暴（本次新增最高优先级事实）**：dmesg 记录自 08-15 16:31 起 `Out of memory: Killed process … (python)` 共 29+ 次，victim 全部位于 planner 容器（`e690e95b…`），anon-rss 610–917MB，间隔最短约 90 秒；容器 `state=restarting / exit=137 / RestartCount 10→43`，`HostConfig.Memory=0`（无内存上限，对照 dispatcher 各 512MiB）。实测 RSS 轨迹：启动 ~65MB → 空转 drain ~181MB → 进入 like/comment 重规划轮后 3–4 分钟内冲至 ~900MB 被杀；规划角色在重启退避中实际停摆，AI 活群与 like/comment 的新规划同窗中断。
- **future pending 继续增长**：pause 前 running channel_like 5 个 Task 合计 2576 条 pending（§2 快照为 2384），其中“郑州楼凤”单 Task 2109 条、最远排至 2027-01-16，证实 RC-1/RC-4“沿最远 open Action 续排”仍在推进且为 planner 物化主源。
- channel_comment running 3 个 Task（阿哥日记/郑州楼凤/成都阿楠）；group_ai_chat running 6 个 Task，全部不受 C0b 影响。
- 旁路容器与 §2 口径一致方向：66 个 mihomo 实测合计约 950MiB（§2 记 61 个 858MiB，需在下一轮完整 readback 统一清点后 resync RC-7 公式输入）；tg-v-chat×3、tg-reporter 持续同宿主争用。
- planner 日志出现 PolarDB `Temporary failure in name resolution`：判定为内存压力下的伴随症状，不单独立项、不得以扩大 DNS/连接池掩盖。
- OCR worker 本轮实测 323.9MiB/768MiB、RestartCount 未新增（Up 2 hours healthy）；`/ready` 复核仍 `blocked`。

**基线结论**：§1.2“CPU/内存多根因叠加”判断维持，但 planner future 物化已从“高负载”恶化为“全局 OOM 风暴”，宿主满足 RC-0/RC-0b 适用条件；C0（搜索停流量）不解决本风暴，planner 侧止血为独立前置动作。

### 2.3 2026-08-16 凌晨止血执行记录（C0b + O0，均已获用户批准）

**第一轮（channel_like，01:44:10）**

- 审批：用户显式批准“缺口二止血”（approval ref `user-approved-gap2-20260816`）。
- apply 集合（精确 task_id，执行时全部 `status=running`）：`e0cf24ce-6757-4db8-86c5-b01f1332135e`（天津新闻，0 future pending）、`a74ddf48-4820-48f2-9268-a5c9a69a46cb`（郑州吃瓜，160）、`7562f38a-4297-4dc6-8051-ab3209bb8e0e`（郑州楼凤，2109）、`d8aedaa1-b97b-45df-9a24-035de9b6352d`（阿哥日记，114）、`6c39f49d-8f2d-4c79-b81e-4fd4cdbc6c19`（成都阿楠，193）。
- 执行方式：backend 容器内复用 `app.services.task_center.service.pause_task(session, tenant_id, task_id, actor)`，actor=`prod-c0b-planner-oom-mitigation`；只推进 `status=paused / next_run_at=NULL / task_lifecycle_epoch+1` 并写 AuditLog（5 条），未触碰任何 Action/Attempt/lease/assignment/在途。
- 结果：**不足以止住风暴**——01:46:39 至 01:51:13 仍有 5+ 次 planner OOM（victim 710–770MB），证明 like 非唯一物化源。

**第二轮（channel_comment 升级，01:56:15）**

- 审批：按 RC-0b 第 1/4 条升级路径单独获批（approval ref 追加 `escalation-comment-user-approved-20260816`）。
- apply 集合：`64f009db-7212-4402-8665-cd4ea8817572`（郑州楼凤，370 future pending）、`16c8bbc2-9465-4eb2-bfab-65ad52048b2c`（阿哥日记，9）、`e6e295d8-746f-4015-9be9-8e71ecbbfd54`（成都阿楠，2）；同一 `pause_task` 边界与 AuditLog。
- 结果：**仍不足以止住**——02:03:13、02:03:59、02:04:44、02:06:25 仍有 OOM。此时 running 任务仅剩 6 个 `group_ai_chat`（232 pending），确立关键事实：**planner 处理 group_ai_chat 的规划物化本身即需 ~740MB**，AI lane 是被保护对象、不可 pause，只能由容量缓冲 + T2 根治。

**第三轮（O0 swap，02:05 前后）**

- preflight：根盘可用 20GiB（≥12GiB）、inode 6% used，通过；OOM storm 中执行的理由：C0b 已按合同移除 like/comment 物化源，剩余源（AI lane 物化）不可暂停，swap 为 PRD O0 预设的故障隔离手段。
- 执行：`fallocate 4G /swapfile` → `chmod 600` → `mkswap/swapon` → `/etc/fstab` 持久化 → `vm.swappiness=10` 并写 `/etc/sysctl.d/99-tgyunying-swap.conf`；readback（swapon 4294963200B、fstab、swappiness）全部通过。
- 效果（02:15:50 / 02:21:44 readback）：最后一次 OOM 为 02:06:25，此后 ≥15 分钟无新增 victim（总数定格 39）；planner 当前实例自 02:06:35 起持续存活（此前每 3–4 分钟被杀一次）；load 由 ~314 回落至 45/32/127；swap used 492MiB/4GiB；**AI 活群规划恢复**——02:06:30 后新建 `group_ai_chat` Action 2489 条（停摆期补偿性追赶，持续产出至 02:20:25），channel_view 552、search_click 40 同步恢复。planner 长稳、追赶速率合理性与 24 小时 RC-7 资源 soak 仍待验证，状态 `production_unproven`。

**C0b 恢复边界重申**：like/comment 共 8 个 Task 保持 paused，未部署 T2 前不得 resume；group_ai_chat 全程未动。

### 2.4 2026-08-16 晚间增量：group_ai_chat 目标上调触发 pacing 冻结冲突死循环（已止血，修复归属 AI 专项）

**现象（`observed`，release `20260816111917_0b85b83e`）**：郑州师范（`7162e305-fb51-4a67-92ea-d0caffd2bbb3`）与郑州楼凤（`2d8af940-69d8-45ab-ab0c-0a1715843d3f`）于 18:23:53 同一秒因 `current_required_account_count_changed` 把 planned_daily_target 提升至 1064/1063（revision 5/3），但当日已冻结 `TaskGroupDailyMessageSlot` 的 `pacing_plan_total` 停在 877/876、open slot 仅 800/876。planner 90 分钟日志 32 次 `pacing_owner_immutable_conflict`，两 Task 每 30 秒重试一轮全量规划并整体回滚，自 18:23 起零新 Action（隐性停摆 4.5 小时）；其余 4 个大目标任务（3333/4800/4000/4800，revision=1）slot plan_total 与 target 一致、零冲突，反证仅“目标上调”路径触发。

**机制（代码证实）**：`daily_group_target.py::_apply_current_target` 目标变化只写 target 行与 revision，无已冻结 slot 迁移路径；`ai_pacing.py` 的 `plan_total=max(effective_plan_total, 旧冻结值)` 在目标上调时取新值；`pacing_persistence.py::_assert_frozen_identity` 对 `current_total != plan_total` 一律 `ValueError`（immutable 语义拒绝合法上调，max() 只保护下调）；`service.py::_record_planner_runtime_error` 把该确定性冲突当通用运行时异常按 30 秒退避重试，形成不可自愈循环。

**止血（22:54:20，用户批准 `user-approved-pacing-conflict-pause-20260816`）**：两 Task 经 `pause_task` 服务受控暂停（actor=`prod-ai-pacing-conflict-mitigation`，epoch 1→2，AuditLog 2 条，Action/在途未动）。验证：pause 后 5 分钟窗口 0 次新冲突、0 次 planner_task_failed，load 0.89，MemAvailable 回升至 619MiB。planner RSS ~730MiB 为长活进程历史峰值驻留（匿名页不归还 OS），非活跃负载，恢复基线需进程重建并观察。

**修复归属（AI 专项，不在本文另建合同）**：P0-A——`pacing_owner_immutable_conflict` 必须从通用 runtime error 分离为 typed blocker（如 `ai_pacing_target_revision_conflict`），确定性冲突不得 30 秒重试；P0-B——目标上调时未绑定 active Action 的 open slot 迁移路径（retire+新 revision 重建，或 plan_total 单调上调），必须按 `ai-group-generation-failure-churn-remediation-prd.md` 的 immutable settlement/quantity ordinal 合同设计。两 Task 的 resume 以 P0-A/P0-B 部署且单 Task canary 通过为前置；P1 planner 批量规划内存上限治理仍属 RC-1/T2。

**P0-A/P0-B 实现（2026-08-17 00:27 已发布上线，release `f60256a0`，Deploy Production run `31958097254` 全绿）**：

- P0-B 选择"`plan_total` 单调上调"迁移路径（§2.4 允许的两路径之一）：`pacing_persistence.py::_assert_frozen_identity` 在 identity（`pacing_plan_hash/pacing_slot_ordinal`）一致且新 `plan_total` 严格大于已冻结值时返回迁移信号，`freeze_pacing_owner` 据此升级 `pacing_plan_total/pacing_due_at/release_not_before_at`。走到 freeze 的 owner 恒为"无绑定 active Action 的 open slot"（`ai_pacing._available_quantity_slots` 的 `~bound_action` 过滤），因此迁移不触碰 `quantity_ordinal`、`due_unit_key`、immutable settlement 与已绑定 Action 的冻结身份；plan_total 下调、plan_hash/ordinal 漂移、total 不变的 due 漂移仍 raise。存量冲突数据无需回填：部署后下一轮 planner drain 即按新路径升级（郑州师范 877→1064、郑州楼凤 876→1063 惰性迁移）。
- P0-A：新增 `PacingOwnerImmutableConflict(ValueError)` 类型；`service.py::_drain_task_planner` 在通用 `Exception` 之前专捕该类型，经 `_record_planner_pacing_conflict` 写入 `stats["planner_pacing_target_conflict"]` typed blocker（error_type/message/recorded_at）并以 `PLANNER_PACING_CONFLICT_RETRY_SECONDS=3600` 退避（原通用路径 30 秒）；`_clear_planner_runtime_error` 在规划成功时同时清除该 blocker。冲突自愈出口：单调上调迁移（部署后）、次日新账本/slot 重建、或运营显式处理；blocker 经既有 stats 通道对前端可见，无 silent fallback。
- QA（红→绿，`backend/.venv`，`-m no_postgres`）：`test_freeze_pacing_owner_allows_monotonic_target_increase`、`test_freeze_pacing_owner_rejects_identity_regression`（hash 漂移/下调/due 单独漂移三类仍拒绝）、`test_planner_pacing_conflict_uses_typed_blocker_and_long_backoff`（含 55 分钟下界退避断言与 `planner_runtime_error` 不落断言）、`test_planner_clears_pacing_conflict_blocker_after_success`；回归 345 项 planner/AI/pacing 相关测试全绿。发布仍须走完整 `no_postgres` + PostgreSQL 两分区与 Release Gate。
- 生效后动作（2026-08-17 00:33 已执行）：郑州师范/郑州楼凤经 `resume_task` 受控恢复（epoch 2→3，approval ref `fix-deployed-f60256a0-resume`）。**生产验证（00:33–00:38，`observed`）**：resume 后连续多轮采样 `pacing_owner_immutable_conflict=0`、`planner_task_failed=0`；恰逢跨日，08-17 新账本按新目标自洽重建——郑州楼凤 1051 个 open slot 与 `planned_daily_target=1051` 一致、郑州师范 830/830 一致（新日 `current_required_account_count_changed` revision 2 正常重算），跨日重建作为冲突自愈出口被真实证明；两 Task 恢复产出（师范 433 条、楼凤 207+ 条新 Action，持续新增）；`planner_pacing_target_conflict`/`planner_runtime_error` blocker 均已清除；宿主 load 2.5、MemAvailable 1398MiB、planner 452MiB。08-16 旧账本 slot 保持 876/877 冻结历史由次日 settlement 收口，不在本次迁移范围。P1 planner 单轮内存上限治理仍属 T2。

## 3. 根因分组与修复规则

### RC-0：精确搜索停流量与 OCR 安全停服/恢复（P0 临时止血）

适用条件：当前资源 readback 证明宿主进入风险区，或 OCR 在无业务调用时仍异常消耗 CPU/RSS，且业务方接受纯搜索暂时停止。此流程只降低风险，不关闭 RC-1/RC-3/RC-7 根因修复，也不构成 `production_fixed`。

1. **固定 consumer 闭集**：以 expected deployed SHA 的静态 call-site inventory 和生产 Task readback共同确定能到达 `ImageVerificationWorkerClient` 的 consumer。当前代码候选闭集为 `search_click` 与 legacy `search_join_group`；`search_rank_deboost` 未发现该 remote OCR 调用，不得因名称含“搜索”被顺带暂停。最终 apply 输入必须是精确 `tenant_id/task_id/type/status/lifecycle_epoch/config_hash` 清单，不能只传 task type、标题或模糊时间窗。
2. **preview artifact**：同一只读快照保存 expected/current SHA、consumer inventory hash、每个 Task 旧状态/epoch、Action 各状态、SearchClickAssignment/ProtocolSession phase、ExecutionAttempt/Gateway journal 的 mutation 边界、OCR instance/generation/uptime/active request/recent request count/RSS/CPU、preview hash、actor 与 approval ref。当前事实无法读取时流程 `blocked`，不得直接 stop。
3. **暂停新领取**：apply 逐 Task 复核旧值/hash 后调用既有 pause 服务，推进 lifecycle epoch、写 `status=paused/next_run_at=NULL` 和 AuditLog。不得调用 stop/delete，不得批量改写 pending Action、清 lease、释放 assignment、终结 obligation或重试 unknown。Dispatcher 的 `Task.status=running` claim 门禁负责阻止新领取。
4. **排空原 owner**：已 `claiming|executing`、已进入协议会话或 Gateway 的工作继续由原 owner 按原 mutation identity 收口。quiescence 必须同时满足：精确 consumer Task 全为 paused；搜索 Action 无 `claiming|executing`；无未终态 assignment/session；无 Gateway-started、callback unknown或远端 reconcile hold；OCR `/health` 为 idle 且 `active_request_id` 为空。等待上限取 `max(2 × worker max budget, Gateway shutdown timeout + recovery observation)`；超时保持 `quiescing` 并报警，禁止 kill/重置。
5. **只停独立 OCR overlay**：quiescence readback 与 preview hash一致后，才允许按审批停止 `image-verification-worker` 单服务。backend、Planner、Dispatcher、AI、view、like、comment 和代理基础设施继续运行；不得执行全量 compose down/restart。停后保存容器 absent/stopped、宿主 RSS/CPU/MemAvailable 与 consumer Task 仍 paused 的 readback。
6. **恢复顺序**：先启动同一批准镜像/contract 的 OCR 服务，等待 token-auth `/internal/v1/image-verification/ready` 返回双引擎、记录新 generation/startup peak，并确认 warmup 观察窗内无 restart loop；再只恢复一个精确 Task做 canary，取得 OCR terminal + callback/search E4 后恢复其余清单。任何人绕过 workflow 提前 resume 时必须显式得到 `image_verification_degraded`，不得本地 fallback或自动点击。
7. **派生状态，不造第二套真相源**：运维 artifact 按现有事实投影 `active|quiescing|suspended|warming|ready_idle|degraded`；不新增全局业务状态表。`suspended` 只表示 Task paused、无在途、OCR stopped；不表示历史 obligation fulfilled。preview/apply/readback 可重复执行，状态/epoch/SHA 漂移时整批停止。

### RC-0b：Planner 超前物化止血——channel_like/channel_comment Task 受控暂停（P0 临时止血，2026-08-16 已执行首轮）

适用条件：资源 readback 证明 planner 因 like/comment future 物化进入 OOM 风暴——判据为 dmesg 连续 ≥2 次 OOM victim 位于 planner 容器内进程，或 planner 处于 `restarting` 循环且 victim anon-rss 显著高于空转基线——且业务方接受 like/comment 规划暂停。本流程只切断物化输入，不修复 RC-1/RC-4 根因，不构成 `production_fixed`；与 C0（搜索/OCR）互不替代，满足各自条件时独立执行。2026-08-16 首轮执行教训（§2.3）：like→comment 两轮暂停后风暴仍不止，剩余物化源是 `group_ai_chat` 的规划装载——AI lane 永不进入闭集，此时唯一合规出口是 O0 swap 容量缓冲 + T2 根治，不得以暂停 AI 任务或手工重启收口。

1. **固定 consumer 闭集**：以生产只读 inventory 确定当前 `status=running` 且 `deleted_at IS NULL` 的 `channel_like` Task 精确清单（`tenant_id/task_id/lifecycle_epoch/future pending 数与最远 scheduled_at`）；首轮只暂停 channel_like。channel_comment 仅在 like 暂停后风暴仍持续（连续 ≥2 个 planner 生命周期仍有新增 OOM victim）且获得用户单独批准后进入同一流程。`group_ai_chat`、`channel_view`、`search_*` 任务一律不在闭集内，不得因“降负载”顺带暂停。
2. **preview artifact**：保存 expected/current release SHA、上述精确 Task 清单、planner 容器 state/RestartCount/OOM victim 计数与时间戳、宿主 load/MemAvailable、preview hash、actor 与 approval ref；SSH/DB 不可读时流程 `blocked`，不得盲改。
3. **apply 边界**：逐 Task 复核旧 `status/epoch` 后复用既有 `pause_task` 服务（`status=paused / next_run_at=NULL / lifecycle epoch+1 / AuditLog`）；禁止 `stop/delete`（会把 pending Action 批量改 skipped）、禁止直接 SQL 改写、批量终结 pending Action、清 lease/assignment 或触碰 `claiming/executing/Gateway` 在途。在途 Action 继续由原 owner 收口，planner 的 `status=running` claim 门禁负责阻止新规划。
4. **止血判定**：apply 后观察至少 30 分钟：无新增 planner OOM victim、planner 容器脱离 restarting 循环且 RSS 回到空转基线量级、宿主 load 单调回落、`MemAvailable` 不再逼近 0。pause 生效前已开跑的旧 drain 轮造成的一次 OOM 不计入失败。未达标 → 按升级路径处理（channel_comment 进入第 1 条单独审批流程 → 风暴仍不止则视为 RC-7 O0 前置的“storm 已被移除”条件不成立，重新评估容量），禁止以手工重启/定时重启掩盖。
5. **恢复合同（硬边界）**：被暂停 Task 只能在 T2（source JIT + future Action 受控回收）发布并通过其 canary 后，按 preview→逐 Task resume→readback 恢复；恢复前必须完成 future pending 回收 preview。禁止在旧 writer 仍在的 release 上直接 resume——那会使 2109 条级别的 future 队列立即重新物化并复发风暴。
6. **派生状态**：like/comment lane 以 `like_lane_suspended`（Task paused、无在途、planner 稳定）经既有 blocker 通道投影，不新增状态表；AI/view/search lane 的 typed E4 产出必须在止血观察窗内不回退，否则视为止血失败并升级。

### RC-1：Planner 超前物化 + Dispatcher claim N+1（内存/CPU 主因）

现象：channel_like 一轮构建全部消息×账号缺口；current contract 使用 `deadline_at=None`，随后以现有最远 open Action 为锚继续串行排期，形成 2384 条跨月至 12 月的 future pending。Planner 每 2 秒重复 drain，current-contract 又直接绕过 backlog gate。另一个独立问题位于 Dispatcher：`_due_claim_task_ids()` 无 SQL limit，随后每个 task 各执行 strict/non-strict 两条查询。Planner 的普通 task cursor 虽无 SQL limit，但应用层会在 limit 处 break，不能与 Dispatcher 的无界扫描混为一谈。

2026-08-16 凌晨增量证据（§2.3）：暂停全部 like/comment Task 后，planner 处理仅剩的 6 个 running `group_ai_chat` Task 仍在每轮把 RSS 推至 ~740MB 触发 OOM——**AI lane 的规划装载（候选账号、覆盖账本、历史/上下文窗口的进程内物化）同样是本条要治理的无界物化源**，纳入 T2 的查询/物化窗口治理范围；但 AI 数量/义务/GenerationJob 语义 owner 不变（RC-1 规则 7），T2 不得改变 DueSet 语义。

规则：

1. **JIT 物化，不设 future Action 窗口**：`channel_like/channel_comment` 只为按来源消息滚动 24 小时 pacing 已进入 `due_by_now` 的 source ordinal 建立当前物化；未到期 ordinal 留在任务专用义务账本，不提前创建 3 天/7 天 Action。已到期 Action 可因合法拟人间隔得到晚于 `now` 的 `scheduled_at`，但不得晚于该来源 `deadline_at`，也不得沿 Task 历史最远 future Action 跨来源、跨窗口平移。
2. **来源义务与迁移**：current source owner 使用 `(task_id, source_message_id, source_revision, obligation_kind, ordinal)`；`pacing_anchor_at/due_at/deadline_at/materialization_version/lifecycle_epoch/current_action_id` 必须按 classified-recovery 合同持久化并可审计。现有仅按 task/message/account/version 的 reaction 行属于 legacy shape；Source train 必须先做 additive migration、backfill/classification 与唯一约束，再启用 JIT writer。账号是 ordinal 的可审计执行绑定和 remote-fact 维度，不得替代 source ordinal 分母。
3. **backlog gate 与唤醒**：gate 分列 `due_open/due_materialized/not_yet_due/gateway_hold/unknown/confirmed/terminal_shortfall`；只有 `due_open` 与当前真实 interaction slot 参与本轮物化。`not_yet_due` 不算 backlog，保存下一 `due_at` 并按事件/最早 due 唤醒；无 due 时不得固定 2 秒空转。
4. **存量回收顺序**：先部署 migration 与新 writer fence，再对 future pending 做 preview。`success/remote fact` 保留并绑定；`claiming/executing/Gateway-started/unknown` 保持原 owner 和 reconcile；只有 `pending`、无 claim/lease/Gateway start、且不属于当前 `due_by_now` 的固定集合可 CAS 终结为 `retired_pre_gateway_future_materialization`，同时释放旧 `current_action_id`。仍在有效窗口的 ordinal 回到 `unmaterialized/open` 等待其真实 `due_at`；已过 deadline 的 ordinal由唯一 settlement owner写 shortfall，禁止 Planner 立即重建。
5. **查询有界化**：Planner task 查询把应用端 break 下推为 SQL limit；Dispatcher 以持久公平游标/分窗一次取回 task+候选，消除“无 limit task 列表 + 每 task 两查询”；单轮查询数必须为固定上界，不能随 due task 数形成 2N。
6. **轮询与观测**：Planner 无事件且无到期义务时默认 10 秒兜底轮询；有最近 `due_at` 时睡眠到该时点或事件唤醒。调大间隔不得掩盖查询/物化缺陷；按 role 输出 drain 数量/耗时、SQL 数、`due_open/not_yet_due/materialized`、RSS、CPU 和本轮最大 `scheduled_at/deadline_at`。
7. **group_ai_chat 共享路径回归守卫（T2 硬放行条件）**：本条修改的 `_normal_planner_task_ids`、`_plan_due_task`、Dispatcher 有界查询与公平游标是全部任务类型共享的代码路径。T2 canary 必须显式包含至少一个 running `group_ai_chat` 大目标群 Task，证明：planner drain 前后该 Task 的 DueSet/quantity ordinal/round_goal 语义不变（数量 owner 仍为 AI 专项合同）、normal 正文在 accepted variation 与 message memory ready 前 `Action=0` 不变量保持、无新增空正文 Action、AI generation job 语义不变；release 后 24 小时 AI lane typed remote fact 速率不低于 T2 前基线。任一不成立即 T2 失败回滚，不得以 like/comment 指标达标放行。

### RC-2：单 Provider 共享容量 + 10d 重复碰撞（大目标群履约主因）

现象：3 个 ai-generation 进程各自批量 claim 并按 claim 数并发调用唯一 active MiniMax-M3；代码没有 Provider/API-key 级共享速率桶、`Retry-After` 协调或跨进程 cooldown。DB 能确认 498 个 generation failed，429 归因为高可信推断；另有 875 个 10d exact/similar 重复拦截。

规则：

1. 保持 0141“唯一 active Provider”合同；P0 容量决策只能是升配现有 Token Plan，或在真实小流量验证后原子切换到足额 key。多 Provider 并发/failover 另行立项。
2. 建立三个 generation 进程共享的 Provider admission/cooldown，key 绑定 provider identity、非敏感 key fingerprint 与配置版本，value 保存 `retry_at/reason/source_status/version`。每个进程必须在**领取 GenerationJob 前**和**真正发起 Provider HTTP 前**各检查一次；429 读取合法 `Retry-After` 后以原子 max/CAS 延长 `retry_at`。共享状态不可读时写 `provider_admission_unavailable` 并停止领取/调用，禁止回落成本地重试。
3. 某 worker 收到 429 后，尚未发起 HTTP 的已领取 job 必须按 obligation/job/version CAS 释放 generation lease并进入 `waiting_dependency` 或权威 AI 专项定义的等价可恢复状态；不得创建 Action、不得写 generation failed。已经在途的请求允许单次结算，不能取消后再发。cooldown key 缺失或进程重启时，只允许一个跨进程 probe token 发起首个请求，其他 worker 等待其结果，避免重启惊群。
4. `generation_pending` 是 stable obligation/GenerationJob 阶段，不是 `Action.status`。正常正文在 accepted variation 和 message memory ready 前 `Action=0`；429 恢复继续同一 obligation/job。legacy 空正文 Action 只允许由 AI 专项 final takeover manifest 分类，不得由本 PRD新增或重开。
5. 保留 10d final duplicate gate，不降低阈值换吞吐。exact 使用索引查询，similar 复用 `AiGroupMessageMemory` 的按账号、按 10 天窗口、单 drain 生命周期的增量 batch cache；禁止把全租户/全任务/120 万上下文或 10 天正文全集物化进每个 job。duplicate 只终结当前 variation；相同 external basis 不重置轮次，只有 AI 专项规定的新 basis version 才重开同一 obligation，不伪造消息、不产生 remote fact。
6. 配额调整后仍不能支撑目标时，任务详情通过既有 blocker 投影显示 `provider_capacity_shortfall`；目标调整必须是显式产品决策，禁止静默维持不可达目标。

### RC-3：图片验证/OCR 与代理链路共同阻塞 search_click

现象：1803 个 search Action失败、756个产生`target_click_observed`；1798个Attempt首先停在图片验证required边界，但仅323个Action可直接证明local OCR timeout。OCR容器快照为高CPU、高驻留内存且`RestartCount=37`，重启原因未分类；双引擎readiness又因SSH banner timeout未能复核。同时search-dispatcher有655次连接被服务端关闭。

规则：

1. 先按 Action/Attempt 统一分类 `verification_required -> local_timeout / engine_error / worker_unavailable / transport / other`，禁止再把外层 required 等同于 100% local timeout。
2. **解释当前资源机制**：worker lifespan 会预热 RapidOCR/ddddOCR，两个 engine 使用进程级 `lru_cache(maxsize=1)`，所以空闲 RSS不主动归零；`/ready` 在同 generation 内只命中缓存，不执行图像推理。当前每个业务 request 并行执行 RapidOCR 原图+3蓝色 mask（4 次）与 ddddOCR 3 个 crop（3 次），因此单请求短时接近 200% CPU可以是预期峰值，但不是空闲持续 CPU的解释。
3. **空闲异常判定**：`request_started=0` 或连续 10 分钟 `request_status=idle`、generation/uptime稳定时，OCR 5 分钟 CPU平均必须低于 RC-7预算；超标依次核对 generation/restart delta、ready duration、active request age、两路 source start/terminal、cgroup CPU delta。只允许归类为 `restart_warmup_loop|native_request_stuck|metrics_window_stale|unclassified`，没有闭合证据时保持 `unproven`，不得写“近期没调用所以肯定是泄漏”。
4. RC-1/RC-7 释放宿主 CPU/内存后，使用 token-authenticated `/internal/v1/image-verification/ready` 验证 RapidOCR+ddddOCR；`/health` 或 Docker healthy 仅代表 liveness。health 另补 `active_request_started_at/age`、last terminal、recent request rate、ready duration 和当前 recycle trigger；不得暴露 request 图片、候选原值或业务正文。
5. **重启原因闭集**：每次 old→new generation 都必须记录一个 primary reason与可选 triggers：`deploy_recreate|recycle_request_limit|recycle_soft_rss|deadline_drain|abnormal_native_exit_70|healthcheck_unhealthy|oom_killed|external_signal|unknown_exit`，并保存 exit code、OOMKilled、signal、restart delta、退出前 active request/owned work 和 RSS。没有原因的 RestartCount 增长直接触发 `image_verification_restart_unclassified`，不能把主动回收误报为泄漏或 OOM。
6. **禁止错误止血**：不得先降低 768MiB hard limit、soft RSS 或 request-limit制造每请求重启；不得用 CPU quota把已校准 callback deadline拖穿；不得切回 Dispatcher local OCR、关闭 2-source 合同或增加 silent fallback。阈值调整只能在 startup/P99/soak 基线后进入独立 Resource train。
7. **`adaptive_variants_v2` per-source fast path**：同一 request内RapidOCR先执行原图、ddddOCR先执行全图；每个source只依据自己的版本化校准谓词决定是否省略自己的剩余变体，worker不接收candidate answer set、不执行候选匹配或跨source共识。RapidOCR可使用真实confidence+输出shape；ddddOCR当前固定0.80不能单独作为fast依据，只有shadow证明的独立shape/stability谓词存在时才可启用，否则该source保持完整3 crop。predicate version/config hash必须进入OCR contract与request审计。
8. **完整慢路径保持安全**：任一source的base未通过自己的predicate、late或engine error时，只为该source补齐现有变体：RapidOCR剩余3个蓝色mask、ddddOCR剩余2个crop；最终仍按现行“同一engine所有高置信结果只能归一成一个候选”聚合一票。允许`1+1|1+3|4+1|4+3`四种可观测路径；fast不满足不是失败。Dispatcher继续独占候选精确匹配、每engine一票、跨source共识、fingerprint复核与callback CAS；不得降低置信阈值、2-source共识、换题/unknown语义或增加模型/OCR供应商fallback。
9. **shadow 与灰度**：先对批准的脱敏/合成golden set和生产shadow执行完整4+3，同时按source计算“若predicate开启”的最终source候选/安全状态；每个source只有fast/full unsafe差异为0、候选决策差异为0、deadline不回退且样本覆盖通过才能单独启用，再按5%→25%→100%请求灰度。任一source mismatch、callback accepted/search E4回退或异常重启立即只关闭该source fast predicate，回到remote完整变体，不回local OCR。
   最小运行配置固定为`IMAGE_VERIFICATION_VARIANT_MODE=full|shadow|adaptive`、`IMAGE_VERIFICATION_VARIANT_POLICY_HASH`与`IMAGE_VERIFICATION_VARIANT_CANARY_PERCENT`；source predicate/阈值封装在版本化policy artifact，不拆成可随意调整的一组环境变量。`shadow|adaptive`缺少匹配contract的policy hash时启动失败；回滚必须显式切`full`并readback，禁止运行时静默降级。
10. Dispatcher等待deadline必须依据生产P99配置，并保留“POST结果未知时禁止重复POST”的安全语义；超时后只查询同一deterministic request。adaptive变体全部在一次worker request/generation内完成，不增加第二POST、队列、phase状态机或新的unknown身份。
11. 每个terminal response与结构化metric输出各source的`path=fast|full`、总体`variant_profile=1+1|1+3|4+1|4+3`、variant count/duration、总wall time、process/cgroup CPU-seconds、RSS before/after、busy/draining/recycle reason；candidate原值、图片与OCR原文不落日志/metrics。
12. search E4 只认 `target_click_observed=true`；OCR 完成、代理连通、Action success、fast path命中或资源下降均不是点击完成证明。

### RC-4：channel_like 无 deadline 批量排期导致当前履约被推向未来

现象：快照近2小时只有11个Reaction E4；due pending=0，但future pending有2384条。代码已经给出直接机制：一次构造全部deficit，`coverage_remaining`只递减、不限制quantity，`deadline_at=None`，再沿最远open Action串行续排。因此“快照无due”是超前排期结果，不是目标已完成。

规则：

1. 每条来源消息按冻结的逐消息 reaction target 建立 source ordinal，并以 `pacing_anchor=max(source_observed_at, task_activation_anchor)` 的完整滚动 24 小时曲线计算 `due_by_now`。本轮 quantity 上界是 `DueSet - confirmed ReactionRemoteFact - Gateway/unknown hold - 有效 pre-call owner` 的基数，再与 distinct eligible account 和真实 interaction slot 取最小值。
2. `daily_uncovered_account_count/coverage_remaining` 只用于同一 Task 内账号选择优先级和覆盖投影，不是逐消息 required 分母，也不得作为跨消息总量 cap。第一条消息使用一个账号，不得阻止该账号在另一条消息承担独立 ordinal；同一消息仍受 `(target_peer,message,account)` lifetime remote identity 防重。
3. 每个物化 Action 冻结 obligation/source revision/ordinal、account、reaction contract、`due_at/deadline_at/materialization_version/lifecycle_epoch`；合法拟人排期超过 deadline 时不建 Action，ordinal 保持未物化并由 deadline settlement 写 typed shortfall，不沿历史队尾跨月续排。
4. 存量 2384 条按 RC-1 的 migration→writer fence→preview/apply/readback 顺序处理；不得删除 obligation/Attempt/ReactionRemoteFact，不得把 future Action 批量改成 `now`，也不得在旧 writer 仍可运行时释放 obligation。
5. 验收分母为每个 source revision 的已到期 ordinal DueSet，分子只认唯一绑定的 ReactionRemoteFact；`success Action`、coverage 变量、无报错或未经定义的历史“618/日”均不能结算。
6. like lane 的 claim 公平性必须与 AI/view/search 分 lane 观测；修复不得靠挤占其他类型吞吐达标。

### RC-5：历史登录 TypeError 已防御，新 flow 通过但存量未收口

现象：近 7 天 2 次 TypeError、4 次 invalid code、1 次永久封禁均已复核。`b517e1cf` 已对空 temporary session/hash 显式报错，发布后新建 flow 的 challenge binding 通过；问题转为 21 个当前等待账号仍由迁移前 latest flow 支撑，以及历史异常缺少堆栈。

规则：

1. 不重复实现已经存在的 None guard；补齐回归测试，要求 session/hash 缺失返回 typed `login_flow_not_resumable`，不得出现裸 TypeError/500，并通过 RC-6 记录脱敏完整堆栈。
2. 清理 preview 同时报告“历史 shape 行数”和“当前业务候选账号数”；apply 只处理 latest flow 仍为 waiting-code、challenge binding 缺失、超过 48 小时且账号仍处于等待态的固定集合。当前快照候选为 21，不得把 134 条历史行全部当成待写集合。
3. apply 只 supersede flow 并把账号投影恢复到可重登入口；不删除/改写历史，不自动请求新验证码。新 challenge 仍须由用户显式发送动作创建。
4. Session失效 176、需重新登录 36 的补登属于运营动作；平台只保证入口、状态与错误可诊断，不建自动重登或静默换 session。

### RC-6：Worker 日志缺失 + Telethon 客户端失效不完整

现象：核心 Worker 日志为 0 或近空，498 个 generation failure 的嵌套错误、历史 TypeError 堆栈和大量 OCR 现场无法还原。Telethon cache key 包含 client metadata fingerprint，但 `invalidate_client(credentials, raw_session)` 以空 metadata 重算 key，带 metadata 创建的真实 entry 可能无法 pop；655 次连接关闭与该缺陷相关，但非唯一原因证明。

规则：

1. `worker.py` 统一初始化 stdout 结构化日志，级别可配置且默认 INFO；覆盖 role、drain 数量/耗时、Task/Action/Attempt trace、Provider 状态码分类、OCR request phase、登录 flow 异常和 Gateway 失败。
2. 日志脱敏是硬约束：session、密码、2FA、验证码、token、手机号和消息正文不得明文输出，禁止打印整 payload。
3. compose 全服务明确 `json-file` 的 `max-size/max-file`；日志轮转不能以关闭 INFO/ERROR 为代价。
4. Telethon invalidation 必须使用与创建 client 完全一致的 key 输入，或按明确 cache entry identity 删除；新增真实 metadata cache entry 的 create→invalidate→disconnect 测试，不能只 mock “方法被调用”。
5. `Server closed the connection` 按 proxy/account/cache-key/attempt 分类计数；只有远端错误下降和 client cache 正确回收同时成立，才能关闭本项。
6. Worker heartbeat 的 active 判定必须带最大 freshness 窗口；过期行显示 stale，不得继续参与在线 worker 数或 release health 结论。

### RC-7：宿主机与容器资源隔离不足

规则：

1. **容量公式与 non-platform 集合**：以发布前readback的物理内存`H`为准；若仍为7.3GiB规格，必须满足`platform_normal_p99 <= H - 1.5GiB(MemAvailable目标) - 0.5GiB(host/system reserve) - max(1.5GiB, non_platform_30m_p99*1.15)`。`non_platform`固定为平台release compose（`docker-compose.server.yml`）之外的常驻容器：61×mihomo、`tgyunying-clash-ss`、`app-infra-sing-box`、`app-infra-redis`、`tg-v-chat-{bot,worker,listener}`、`tg-reporter-app`；宿主内核/docker daemon/nginx归入0.5GiB host reserve，不重复计入。2026-08-15实测non_platform≈1376MiB（×1.15≈1.55GiB），公式允许平台预算≈3.75GiB（≈3845MiB）；首轮committed预算统一取下表**3760MiB**，正文/QA/E4均以3760MiB为唯一口径。发布前规格或non-platform集合变化必须重新计算并product resync；达不到时本train阻塞，选择迁移旁路应用/无绑定mihomo或扩容，不得靠swap、降低日志或压低OOM阈值强行放行。
2. **服务预算表**：先在日志可见且 RC-1 修复后的 canary/soak 记录 startup peak、30 分钟 P99 RSS、进程数和 native memory，再按 `mem_limit=向上取整到64MiB(max(P99*1.25,startup_peak*1.10))` 提交 compose。下表是当前主机不可超出的 normal P99 分组预算和首轮 hard-limit 上界；任一服务实测需要更高值时不得单独抬高，必须重新平衡整机预算并 resync：

   | 服务/分组 | normal P99 预算 | 首轮 hard-limit 上界 |
   |---|---:|---:|
   | planner | 384MiB | 512MiB，仅 RC-1 soak 通过后启用 |
   | dispatcher-1/2 合计 | 768MiB | 保留各 512MiB |
   | image-verification-worker | 600MiB | 保留 768MiB |
   | 3 个 ai-generation 合计 | 600MiB | 各 320MiB |
   | backend + search-dispatcher + listener + account-online | 768MiB | 单服务不高于 320MiB |
   | recovery + account-security + material-cache + voice-profile + ai-memory + metrics | 640MiB | 单服务不高于 192MiB |
   | **平台合计** | **3760MiB** | hard limit 只作故障隔离，不作为正常容量承诺 |

   `pids_limit=max(64, ceil(process_count_p99*2))`，同样由 readback 生成；禁止把未经 soak 的表值一次性施加给全部容器。达到 limit 时必须记录 `OOMKilled/exit_code/restart_count/owned work`，writer 在退出前停止新 claim 并按各自 lease/unknown 合同收口。
3. **swap 操作合同**：P0 固定增加 4GiB `/swapfile`，执行前要求根盘可用空间至少 12GiB、inode/磁盘 I/O 无告警，且 OOM storm 已被移除——storm 进行中时必须先执行 RC-0/RC-0b 止血并确认停止（≥15 分钟无新增 OOM victim），不得把创建 swap 当作 storm 的第一止血手段，也不得在 storm 未解除时以“前置不满足”永久阻塞 O0（止血完成即视为前置满足）；文件权限 `0600`，完成 `mkswap/swapon` 后写入 `/etc/fstab`，设置并 readback `vm.swappiness=10`。验收必须保存 `swapon --show --bytes`、`free -b`、`sysctl vm.swappiness` 和重启后持久化证据。持续 15 分钟 swap 使用超过 512MiB或出现持续 swap-in/out 即标记 `resource_capacity_degraded`；只有业务压力解除且 swap 使用归零后才允许按审批回滚，运行中不得直接 `swapoff` 制造内存尖峰。
4. **逐服务启用**：资源限制在独立 Resource train 中按 `planner -> ai-generation单实例 -> 其余generation -> 小worker -> backend/search/listener/account-online` 滚动，每一步至少观察 30 分钟并验证 claim/lease/E4 无回退；dispatcher/OCR 只复核现有限额，不与代码 train 同窗改值。
5. 盘点 61 个 mihomo 的活跃账号出口绑定；未绑定实例只能按精确实例清单、旧值/hash、actor/approval 和连接 readback 下线。是否合并实例属于独立容量设计，不进入本 PRD。
6. 核心应用从 root 迁移到固定非 root UID/GID；OCR 已满足。权限、volume 读写、临时目录、healthcheck 与回滚需逐服务验证，作为独立 P1 Hardening train，不与 P0 swap、Planner 或 Provider 发布同窗。
7. **CPU 与稳定基线**：以下为4vCPU规格的首轮Release Gate，不是监控展示建议；发布前CPU规格变化必须重新核算并product resync。宿主24小时窗口的5分钟non-idle CPU P95`<=70%`，不得连续10分钟`>85%`；Planner在`due_open=0`时5分钟平均`<=10%`单核、正常生产高水位P95`<=50%`单核且单轮SQL数为固定上界；两个Dispatcher合计5分钟CPU P95`<=80%`单核且查询不随due Task形成2N；OCR`ready_idle`且10分钟无request时5分钟平均`<=5%`单核，active单请求允许短时接近200%，但fast-path-eligible样本`cpu_seconds_per_terminal_request` P95相对完整4+3基线至少下降30%，按生产shadow流量结构加权的总体P95至少下降20%。同时满足24小时`MemAvailable>=1.5GiB`、平台normal P99`<=3760MiB`、无OOM/持续swap-in/out；任一阈值在真实业务负载下不可达时必须product resync、迁移旁路服务或扩容，禁止静默放宽阈值。

### RC-8：channel_view 目标实体失效

现象：258 个 distinct Action 全部因目标实体解析失败，集中于“郑州楼凤”单 Task；无 ViewRemoteFact，属于目标资源失效而不是 Dispatcher 吞吐不足。

规则：

1. 对目标频道执行现有资源同步/实体解析预检，输出 old target identity、最新解析结果和账号可访问性；无法解析时任务保持 blocker，不继续批量创建必失败 Action。
2. 目标修复是受控生产写操作。preview 固定 `task_id/tenant_id/old operation_target_id/old peer_id/source_revision/target_revision/route_epoch/lifecycle_epoch/task config hash`、精确新 `operation_target_id/peer_id`、账号访问探针和当前 writer route；apply 必须提交 expected deployed SHA、preview hash、actor、approval ref，并对 Task config revision 与 route/lifecycle epoch 做 CAS。按标题、用户名相似度或模糊搜索结果不得成为 apply 输入。
3. 本 PRD 不独立定义 view schema/writer。只有 `channel-view-planner-starvation-remediation-prd.md` 要求的 additive migration、lifetime owner、Action binding、route/fleet gate 已部署并 `verify-active` 后，才允许 apply target rebind；否则 RC-8 保持 `blocked_by_authoritative_contract`，Task 继续 `target_entity_unresolved`。
4. apply 只推进权威 view 合同规定的 target revision、route/lifecycle epoch 和 source fingerprint，不直接创建 Action。既有失败 Action/Attempt 保留；旧目标的 Gateway-started/unknown 继续只对旧 identity reconcile，禁止被新目标事实结算。新 Action 只能由激活后的 current view writer按新 due unit/binding 创建。
5. readback 必须同时证明 Task 新 target identity、current route/epoch、旧 Action 未改写、旧/新 target 无交叉 binding；生产验收只认对应新 target/revision/route epoch 的 ViewRemoteFact 增量。

### RC-9：Release success 与业务 E4 闸门断裂

现象：`b517e1cf` 部署、symlink 与容器启动均通过，但 planner drain 和 incident E4 两个验证步骤被 skip；因此 workflow success 无法证明本 PRD 任一业务项恢复。此前“部署后重复 takeover”是已知线索，但本轮未取得完整 Action/owner/epoch 链，状态保持 `unproven`，不能直接写成已证实根因。

规则：

1. 每个 L3 release train 必须提交不可变 `incident_e4_manifest`：`intake_id/train_id/expected_sha/release_live_at`、精确 Task ID、task type、target identity、contract/target/route/lifecycle epoch、证据起止时间和所需 E4 检查。manifest 为空、仍引用固定历史 Task ID或与 runtime SHA/epoch 不符时 workflow 失败；不得用通用 `run_production_diagnostics=false` 跳过。
2. L3 workflow 的 planner drain、资源 readback 与 manifest 指定的分类型 E4 必须真实执行；证据访问受阻时步骤输出 `blocked` 并令产品状态保持 `production_unproven`，不得以 `skipped/pass` 收口。发布 artifact 保存 manifest、expected/current symlink/runtime SHA、迁移版本、compose config hash、容器资源 readback 和每项查询结果。
3. SSH/生产访问受阻时，deploy 可单独记录 `release_passed`，但产品状态保持 `production_unproven`；恢复访问后补证，不能追认此前未执行的 E4。
4. 不新增“第二次 takeover no-op”语义：全量 takeover 仍只由 `deploy/compose-up.sh` Stage B 在零业务 writer 窗口执行，按权威专项逐 Task/manifest 分类；`claiming/executing/unknown_after_send` 由 Dispatcher/Recovery/remote reconcile owner处理，不能让一个 Task 的 hold 阻断其他独立安全 Task。release 返回后 workflow 只能执行有界只读 `verify-active`，静态/集成测试必须断言不存在第二个 all-task takeover 调用点。

### RC-10：Gateway 异常边界与通用 retry 可重发成功/未知 Action

现象：Telegram gateway 在 target resolve 抛错时，异常分支会读取尚未初始化的 `remote_message_id`；通用 `retry_task(failed_only=false)` 对非 search-rank Task 查询全部 Action，后续又无条件允许普通类型进入 pending，可能重发 `success/unknown_after_send`。

规则：

1. `_send_async` 在任何可能抛错的语句前初始化远端 mutation 状态。target resolve 或 send 调用前失败必须返回 `remote_mutation_started=false`；任一 segment 已得到 remote message identity 后的异常必须保留该 identity，并进入对应 partial/unknown reconcile，禁止自动重发整个批次。
2. 通用 retry 的可重试集合是闭集：Action 处于 `failed|skipped|cancelled`，最新 Attempt 无 `gateway_call_started_at/remote_message_id`，且 typed result 明确 `remote_mutation_started=false` 或 `pre_accept_rejected`。`failed_only=false` 只能扩大 UI 选择范围，不能扩大安全状态集合。
3. `success/claiming/executing/unknown_after_send/pending_visibility`、任何 Gateway-started/remote identity/evidence gap 一律拒绝自动 retry并返回 typed blocker；task-type recovery 可以重开同一 obligation，但不得新建分母、删除 Attempt/remote fact或绕过 request identity。
4. eligibility 判断必须先于任何 Action 字段清空；`_prepare_action_retry` 只能处理已通过统一安全谓词的 Action。批量 retry 使用行锁/CAS 固定集合，状态漂移整批停止并输出 ID/readback。

## 4. 功能、前端与 API

- 不新增前端页面与 OCR 停服按钮；任务详情沿用现有 due/materialized/confirmed/blocker 展示，新增 `provider_capacity_shortfall`、`provider_admission_unavailable`、`image_verification_degraded`、`image_verification_restart_unclassified`、`target_entity_unresolved`、`blocked_by_authoritative_contract`、`unsafe_retry_rejected`、`resource_capacity_degraded`、`production_e4_blocked`、`like_lane_suspended` 等既有 blocker 通道取值。
- blocker 必须显示当前任务/目标/epoch 的新鲜事实，不得把历史错误、容器 healthy 或 deploy success 投影为当前履约状态。
- source pacing、DueSet和final duplicate gate是产品合同，不开放配置；planner兜底间隔、OCR等待deadline、variant mode/policy hash/canary percent、Provider cooldown上限和日志级别为发布配置，不开放用户自助修改，非法组合启动失败。
- 受控生产操作包括搜索/OCR quiesce/stop/resume、future Action 回收、僵尸 flow supersede、channel_view target rebind 和无绑定 mihomo 下线。全部走独立 CLI/Workflow，参数必须含 exact 集合/目标、expected deployed SHA、当前 contract/route/lifecycle epoch、old-value/config hash、preview hash、actor 与 approval ref；普通任务 API 无权停容器，preview 与 apply 之间发生任一漂移即整批停止。

### 4.1 搜索/OCR 运维控制面

- 单 Task 继续复用 `POST /api/tasks/{task_id}/pause|resume` 与现有租户/用户审计；C0 workflow只编排这些服务调用和独立 OCR overlay，不新增批量直接写库 API。
- workflow 输出派生 lane state：`active`（有 running consumer）、`quiescing`（consumer已暂停但仍有在途）、`suspended`（consumer paused、无在途、OCR stopped）、`warming`（OCR启动但 functional ready未通过）、`ready_idle`（ready且无active request）、`degraded`（consumer running但OCR未ready/重启原因不明）。状态每次从 Task/Action/Assignment/Session/Gateway/worker事实重算，不单独持久化或作为履约事实。
- resume workflow 先验证 OCR ready与 contract，再逐 Task恢复；普通用户在 OCR stopped时直接 resume只会得到显式 blocker并 fail closed。系统不得因此自动切 local OCR、自动重启 Task、删除 pending Action或声称搜索完成。
- 所有 preview/apply/readback artifact和日志只保留 ID/hash/状态/计数/耗时，不包含验证码图片、OCR原文、按钮原文、消息正文、session、token、代理凭据或手机号。

## 5. 后端与 Worker 交接

1. `services/task_center/service.py`、`dispatcher.py`、`executors/channel_like.py`、`executors/channel_comment*.py`（targets/preparation/schedule/budget）、source obligation/settlement 与 migration：`due_by_now` JIT、source ordinal/deadline、backlog/wake、coverage 仅作账号优先级、Dispatcher 有界查询与公平游标（RC-1/RC-4）。
2. `backend/scripts/` + 独立生产 workflow：legacy reaction/source backfill classification、future Action preview/apply/readback；脚本必须按 RC-1 状态分类并复用权威 obligation/remote-fact 终结合同。
3. `services/task_center/ai_generation_worker.py`、Provider adapter/gateway：共享 Provider admission/cooldown、claim/pre-call 双重 fence、429/Retry-After、已 claim job 释放和有界 duplicate memory（RC-2）。不得修改 AI 数量 owner 或新增 normal 空正文 Action；Provider 切换/升配进入运维 runbook。
4. `image_verification_worker*.py`、`image_verification_ocr.py`、`services/image_verification_client.py`、`image_verification_sources.py`、search dispatcher：OCR readiness、request phase/age、per-source `adaptive_variants_v2`、逐变体耗时/CPU、重启原因与验证码失败内层分类（RC-3）。
5. 登录服务仅补当前 guard 的回归与 typed error/日志，不重复实现已有 None 防御；新增 latest legacy waiting-flow 存量脚本（RC-5）。
6. `worker.py`、compose、`telethon_lifecycle.py`：统一日志初始化、日志轮转、metadata-aware invalidation、heartbeat freshness；资源预算/readback 和非 root 迁移分别进入 Resource/Hardening train（RC-6/RC-7）。
7. channel_view/资源同步链路只实现精确预检和受控 rebinding workflow；migration、binding、epoch 与 writer 必须复用 view 权威专项，不在本 PRD 另建兼容路径（RC-8）。
8. Deploy Production workflow：消费 `incident_e4_manifest`，强制执行该 train 的 planner/resource/typed E4；删除固定历史 Task ID和 release live 后的任何 all-task takeover 调用（RC-9）。
9. `integrations/telegram/gateway.py::_send_async` 与 `service.py::retry_task` 按 RC-10 的 mutation/retry 闭集先写红测再修复；不得把它们作为无独立验收的顺手改动。
10. `docs/04-ops/deployment/PRODUCTION_RUNTIME.md`/runbook 记录容量公式、swap、逐服务 budget/readback、回滚与告警；代码入口、worker 拓扑、脚本、workflow 变化同步结构索引，obligation/Action/flow/remote fact 变化同步数据流转索引。
11. 独立 C0 workflow/脚本只做 consumer inventory、Task pause/resume编排、quiescence readback和单 OCR overlay stop/start；必须调用现有服务边界并输出 immutable manifest，不允许通用 SQL update、全量 compose操作或把 `unknown` 强制收口。

## 6. 数据一致性、并发和失败路径

- reaction/source migration 必须 additive-first：先建 nullable current 字段/索引和 backfill operation，再按 remote fact/Gateway evidence/legacy action 分类；无法唯一映射 source revision/ordinal 的行进入 migration blocker，不猜测。final unique/current writer 只在 backfill manifest sealed 后启用；旧自然键保留为 legacy partial constraint，回滚不得重新取得 current writer 资格。
- 存量 Action 回收与任何批量写必须幂等、可重放、preview hash 校验失败整批中止；不删除 Action/Attempt/remote fact。一个 Task/source 的 conflict 只阻断该 operation item，不得让无关 Task 隐式部分 apply；operation 最终状态明确 `completed|blocked|failed`。
- Planner 并发依赖 source ordinal/current Action/remote identity 的唯一约束；只物化 `due_by_now`，不建立 future Action 窗口，不回写或缩小 DueSet。未到期/未物化不能算 skipped/fulfilled；deadline 只由类型专项 settlement owner收口。
- Provider cooldown 使用现有 Redis 保存所有 generation 进程共享的原子状态，key 至少绑定 active provider identity 与配置版本，value 为带来源的 `retry_at`；状态不可读时显式停止新 claim，不允许回落为进程本地无限重试。
- 429 期间已领取、未发 HTTP 的 GenerationJob 释放 lease并回到权威 AI 可恢复状态；normal Action 仍为 0。Provider in-flight、generation persist unknown 和 Telegram `unknown_after_send` 是三个不同状态域，不能互相恢复或自动重试。
- 10d duplicate 只能拒绝当前 variation，不能把义务结算 fulfilled；相同 basis 不重置轮次，新 basis 按 AI 专项重开同一义务。exact/similar 查询必须有界且按账号/窗口索引或增量 cache，不得全量物化 corpus。
- 僵尸 flow supersede 与 0147 的 supersede 指针语义一致（脚本串行 + 行锁）；新验证码只能由用户后续显式动作触发。
- channel_view target 变更必须由受控 operation CAS 推进权威 target/route/lifecycle epoch；旧失败/unknown Action 不得被新目标 ViewRemoteFact 反向结算，view current contract 未激活时禁止 apply。
- all-task takeover 只在 compose Stage B；post-release 只读 verify。逐 Task/manifest 的冲突按权威 takeover operation 阻塞该 item，禁止创造“一个 unknown 停全库”或“第二次 takeover 自动 no-op”的新共享语义。
- generic retry 必须先通过 RC-10 安全谓词再清空字段；任何 Gateway-started/remote identity/evidence gap 永不进入自动 retry。
- C0 pause 与 OCR stop分两阶段提交：Task pause已写入后若 quiescence失败，保持 paused并输出 blocker，不回滚为 running；OCR只有在所有 precondition同一 readback成立时才停止。恢复也分两阶段：OCR ready失败时 Task保持 paused，不进行补偿性本地识别或自动 resume。
- `adaptive_variants_v2`不改变OcrRequest候选边界或HTTP状态机；每source的predicate/version、实际variant path与结果进入同一request审计，deterministic request重放返回同一terminal事实。generation变化仍解释为unknown并按既有恢复合同处理，不能创建第二request绕过幂等。
- per-source fast predicate只能来自版本化golden/shadow校准artifact；配置缺失、版本/hash不匹配或样本Gate未通过时只能用显式`full`模式运行remote完整变体。该模式不是错误fallback，也不得退回Dispatcher local OCR。
- swap、代码、数据操作、mem_limit 与 non-root 分 train；任何服务因 limit 退出必须显式留痕，不能以 `restart: unless-stopped` 覆盖未完成工作。正常 P99 超预算时阻塞并调整容量，不能依赖 swap 承载稳态。
- 回滚按 train：additive migration 可保留但旧 writer 不得复活；future Action/flow/target apply 不做逆向数据改写；Provider admission 回滚不得恢复无共享限流的多进程猛打；swap 在仍有压力或已使用时不得移除；其余无数据 mutation 的代码 train 才允许回到上一稳定 SHA。
- 不允许 silent fallback：OCR、Provider shared state、日志初始化或实体解析失败时都必须暴露 typed blocker，不得返回 mock success、模板假消息或跳过 E4。

## 7. QA 验收

### RC-0b Planner 物化止血

- `C0b-QA-1`：preview 只纳入 running channel_like 精确 Task 清单；`group_ai_chat/channel_view/search_*` 与 channel_comment（未获单独批准时）不得误入；任一 Task status/epoch 在 preview→apply 间漂移即整批停止。
- `C0b-QA-2`：pause 经由 `pause_task` 服务完成，AuditLog 齐全且只改 `status/next_run_at/epoch`；注入 claiming/executing Action、Gateway-started、assignment 在途时均不被触碰、不被终结，Dispatcher claim 门禁在 paused 后不再领取新 Action。
- `C0b-QA-3`：apply 后 30 分钟观察窗内 planner 无新增 OOM victim、脱离 restarting 循环、RSS 回空转基线量级；宿主 load 单调回落；AI/view/comment/search lane typed E4 不回退。旧 drain 轮的一次 OOM 不计失败，但必须能从 dmesg 时间戳与 pause 写入时间区分。
- `C0b-QA-4`：在未部署 T2 的 release 上直接 resume 被暂停 Task 被流程拒绝并要求 T2 前置；T2 canary 通过后的 resume 走 preview→逐 Task→readback，且 future pending 已按 RC-1 规则 4 回收完毕。

### RC-0 搜索停流量与 OCR 安全停启

- `C0-QA-1`：preview 只纳入 expected SHA静态 inventory确认可到达 remote OCR 的精确 Task；同名非 consumer与`search_rank_deboost`不得误入，任一 Task status/epoch/config hash漂移时 apply整批停止。
- `C0-QA-2`：pause后新 pending搜索 Action不能被 claim；原有 claiming/executing继续由旧 owner结算。分别注入 OCR running、Gateway-started、callback unknown和未终态 ProtocolSession，quiescence均保持 blocked且 stop调用次数为0。
- `C0-QA-3`：完全排空后只停止 OCR overlay，backend/Planner/Dispatcher与 AI/view/like/comment继续健康并产生各自 typed E4；重复 stop/readback幂等，不清 Action/Attempt/assignment/session/fact。
- `C0-QA-4`：恢复必须先达到双引擎 token `/ready`、同 contract/generation稳定，再 resume一个 canary Task；预热失败、restart loop或contract mismatch时全部 Task保持 paused，无 local OCR、重复 POST/callback或自动补跑。

### RC-1/RC-4 Planner、Dispatcher 与点赞

1. `pacing_anchor` 时 due=0；晚采集的 50 点赞/80 评论来源只按完整滚动 24 小时曲线物化当前 `due_by_now`，未到期 ordinal 的 Action 数为 0，不能生成 3/7 天 future Action或沿历史尾部跨窗口平移。
2. source migration/backfill 在 PostgreSQL 验证 current key、due/deadline/materialization/lifecycle 字段；同一 source ordinal 并发最多一个 current Action，无法唯一映射的 legacy 行进入 blocker，不猜测绑定。
3. 多条消息共用同一账号池时，每条消息分别达到自身 DueSet；`coverage_remaining=0` 不得阻止其他消息的独立 ordinal，且同一 message/account remote identity 不重复。
4. Dispatcher due task/candidate 查询数不产生 2N；SQL 自身含 limit/持久游标，多 Task 公平轮转，空闲 Planner 无 due 时不固定 2 秒查询。
5. 回收脚本按 `fact/success`、`claiming/executing/Gateway/unknown`、安全 future pending 三类输出；preview hash 对状态/版本/义务指针敏感，apply 仅终结第三类并释放指针，重复 apply 幂等且旧 writer 已 fenced。
6. T2 canary 含至少一个 running `group_ai_chat` 大目标群 Task（RC-1 规则 7）：drain 前后 DueSet/quantity ordinal/round_goal 语义不变、normal 正文 `Action=0` 不变量保持、无新增空正文 Action、generation job 语义不变；canary 与 release 后 24 小时 AI lane typed remote fact 速率不低于基线。

### RC-2 Provider 与重复内容

6. 三个模拟 generation 进程中任一个收到 429 后，其他进程在 DB claim 前和 Provider HTTP 前均被共享 cooldown 拦截；Retry-After 原子延长，已 claim 未调用的 job 释放 lease，已在途请求只结算一次。
7. Redis/cooldown 不可读时产生 `provider_admission_unavailable` 并停止 claim/call；key 缺失或三进程同时重启时只有一个 probe token 请求，不能形成惊群。
8. normal AI 在 accepted variation/memory ready 前 Action 数为 0；429、capacity waiting、duplicate reject 均不创建空正文 Action、不新增义务、不写 Telegram unknown。
9. exact/similar final gate 拒绝时无 Gateway send/remote fact；相同 basis 不重置 variation 轮次。去重查询只加载当前账号10天窗口的增量 batch，内存/SQL有界且不降低阈值。
10. Provider 切换 runbook 在 staging 演练 0141 唯一 active 原子切换、失败回滚与小流量验证。

### RC-3 OCR/Search

11. 测试矩阵分别产生 local timeout、engine error、worker unavailable、transport error，Action/Attempt 保存的内外层分类准确且互不折叠。
12. `/ready` 只有两个引擎均初始化完成才 pass；`/health`、容器 healthy、单引擎完成均不得通过 readiness 断言。
13. 注入超过 deadline 的 request 后只查询原 request，不重复 POST；未取得 `target_click_observed` 时 search 履约保持未完成。

- `OCR-QA-1`：同 generation连续调用 `/ready` 不增加 request/source/variant计数；worker `ready_idle` 10分钟的CPU采样满足 RC-7，若强制重启则能关联 warmup CPU与新 generation。
- `OCR-QA-2`：每个 deploy、request-limit、soft-RSS、deadline drain、exit 70、healthcheck、OOM、signal场景均产出唯一 primary reason、exit/OOM/signal/readback；无法解释的 restart触发 `image_verification_restart_unclassified`。
- `OCR-QA-3`：shadow corpus对每个source同时计算base predicate与完整变体最终结果；fast/full候选与unsafe差异必须为0，覆盖数学/字母、低置信、分歧、候选不命中、late、单引擎异常和多变体内部冲突；ddddOCR固定0.80不得被测试误当成可区分confidence。
- `OCR-QA-4`：两个source均fast时执行1+1；分别只让一侧fast时执行1+3/4+1；均不fast时精确4+3。同engine仍最多一票，跨source共识/callback CAS最多一次，deterministic request重复调用不增加推理或callback。
- `OCR-QA-5`：worker请求仍只携带现有candidate hash而不携带candidate原值；认证内网terminal response只允许携带Dispatcher投票所需的有界OCR candidates，日志、metrics、持久artifact不得出现OCR原文、图片、按钮原文或业务正文。
- `OCR-QA-6`：`shadow|adaptive`缺policy hash/contract不匹配时启动失败；5%/25%/100%灰度分别比较callback accepted、search `target_click_observed/required`、deadline、busy、restart与CPU-seconds，总体生产mix的CPU-seconds P95至少下降20%；任一Gate回退显式切`full`并readback，保留remote完整变体，不切local。

### RC-5 登录

14. session/hash 缺失返回 typed `login_flow_not_resumable`，无 TypeError/500；异常日志有堆栈且敏感字段掩码。
15. preview 区分历史 134 shape 与当前 latest-flow 业务候选；apply 只 supersede preview 固定集合，账号回到可重登入口，不自动发验证码，AuditLog 完整且重复 apply 幂等。

### RC-6/RC-7 日志、Telethon 与资源

16. 全部 role 的 Docker 日志可见 INFO drain/耗时与 ERROR trace；脱敏扫描无 session、密码、验证码、2FA、token、手机号和消息正文。
17. 用非空 client metadata 创建真实缓存 entry 后，invalidate 必须删除并断开同一 entry；空/不同 metadata 不得误删其他 client。
18. 容量 artifact 使用本 PRD公式计算且 normal P99 合计不超过 3760MiB；4GiB swap 的磁盘 preflight、权限、swappiness、fstab、重启 readback 和 degraded 告警均通过。
19. Resource train 每次只滚动一个服务/分组；compose 合并配置断言 mem/pids/logging，触限时退出原因、owner/lease 与未完成工作可观测，30 分钟内无 restart loop和 typed E4 回退。
20. Hardening train 逐服务验证固定非 root UID/GID、volume/临时目录权限、healthcheck 和 rollback；单服务失败只回滚该服务且不改变 T7 已确认的资源预算。

- `RES-QA-1`：生产等价负载连续24小时验证宿主/Planner/Dispatcher/OCR CPU阈值、`MemAvailable`、3760MiB平台P99、swap与OOM；OCR active峰值与idle CPU分开统计，不能用全天平均掩盖空闲高CPU或请求尖峰。

### RC-8/RC-9 实体与发布闸门

21. view 权威 migration/route 未 `verify-active` 时 rebinding 被阻止；精确 preview/apply 后推进新 target/revision/route epoch，旧 Action 不改写，旧/新 binding 不交叉，新 ViewRemoteFact 只结算新目标。
22. L3 workflow 拒绝空 manifest、固定历史 Task ID、SHA/epoch 不匹配和 diagnostics skip；planner/resource/typed E4 访问失败输出 blocked/fail，不能显示 skipped/pass。
23. 静态和集成测试证明 all-task takeover 只由 compose Stage B 调用一次；release 返回后只读 `verify-active`。一个 Task 的 executing/unknown hold 不阻断其他独立安全 Task 的权威 takeover item。

### RC-10 Gateway 与 retry

24. target resolve 在任何 send 前抛错时无 `UnboundLocalError` 且 `remote_mutation_started=false`；首个 segment 成功后第二个失败时保留 remote identity并进入 partial/unknown reconcile，不重发已成功 segment。
25. 对 `success/claiming/executing/unknown_after_send/pending_visibility/Gateway-started/remote-id-present` 逐一调用 `retry_task(failed_only=true|false)` 均不得改成 pending；只有 typed pre-transport/pre-accept 的 terminal Action 可重开。
26. retry preview 与 apply 间注入状态/版本漂移时整批停止；成功 Action、Attempt、remote fact 字段完全不变，并输出 `unsafe_retry_rejected`/readback。

## 8. Release Gate 与生产验收

- 发布路径 `master -> release -> Deploy Production`；后端测试默认 `backend/.venv`，完整 `-m no_postgres` 与 PostgreSQL 分区、`git diff --check` 通过。
- **需要 additive schema migration**：Source train 增加/回填 reaction/source ordinal 的 revision、pacing/due/deadline、materialization/lifecycle 和 current binding；final constraint 只能在 manifest sealed 后激活。AI/view 所需 schema/fleet migration 只执行各自权威专项，本文不复制。所有 migration 必须先兼容旧读、后切 writer，不删除历史字段/行。
- C0与OCR adaptive contract不新增业务数据库表；复用Task/AuditLog、Action/Attempt、SearchAssignment/ProtocolSession、WorkerHeartbeat、结构化日志与runtime metrics。图片只存在于当前请求内存，worker不新增candidate原值输入，禁止为本次优化新建图片/按钮/OCR原文审计表。
- O0 使用独立 ops artifact；每个代码 train 使用独立 SHA、migration/config hash、canary、rollback decision 和 `incident_e4_manifest`。前一 train 的 `release_passed`/E4 不能替代后一 train，禁止把下表合并为一次全量滚动：

  | 顺序 | Release train | 范围 | 放行条件 | 回滚/停止线 |
  |---|---|---|---|---|
  | C0 | Emergency search/OCR load shed | exact consumer inventory、Task pause、quiescence、可选 OCR stop与预热恢复 | current SHA/readback可得；无在途/Gateway unknown；明确 approval；其他 lane E4不回退 | 任一 in-flight/漂移即保持 Task paused并停止；禁止 kill、清状态或全量 compose操作 |
  | C0b | Emergency planner materialization load shed（2026-08-16 首轮已执行，见 §2.3） | 精确 running channel_like Task 受控 pause、storm 观察、resume 严格 gated by T2 | current SHA/readback可得；明确 approval；30 分钟无新增 planner OOM victim、load 回落、其他 lane E4不回退 | 风暴不止时 comment 进入单独审批，仍失败转 O0 前置重评；禁止 kill/清状态/定时重启掩盖；未部署 T2 前不得 resume |
  | O0 | Emergency capacity baseline | 4GiB swap、磁盘/readback、现状 RSS/CPU/进程/日志 artifact | 不重启业务容器；OOM storm 已按 C0/C0b 移除（≥15 分钟无新增 victim）；swap 持久化 readback | preflight 失败不创建；已使用 swap 时不 `swapoff` |
  | T1 | Observability & outbound safety | Worker 日志、heartbeat freshness、Telethon invalidation、RC-10 Gateway/retry | 全 role 日志/脱敏、retry 红测、无业务数据 migration | 无远端 mutation/data apply时可回上一 SHA；出现 unknown 不自动重试 |
  | T2 | Source JIT & query pressure | source additive migration、Planner/Dispatcher 有界查询、like/comment JIT、future Action 受控回收、C0b Task 恢复 | migration/backfill sealed、新 writer fenced、单 Task canary后才 apply cleanup；canary 必须含 running group_ai_chat Task 且 AI lane E4 不回退（RC-1 规则 7）；future pending 回收完成后才允许 resume C0b Task | apply 后禁止旧 writer 复活；冲突时停在新 release 前向修复 |
  | T3 | Provider admission | claim/pre-call cooldown、429 分类、已 claim job 释放、有界 dedupe cache | current AI route/authority `verify-active`、单 Provider 小流量、Action=0 invariant | 不得回到无共享 admission 的多进程并发；保持 fail-closed |
  | T4 | Search/OCR observability & adaptive load | OCR 内层分类、functional readiness、request age/variant/CPU、restart原因、`adaptive_variants_v2`、同 request 查询、代理/Telethon指标 | shadow差异为0；5%→25%→100%逐级 Gate；双引擎 `/ready`、受控账号 callback/search E4 | 关闭fast path回remote完整4+3；保留同 request/unknown，禁止local fallback、重复 POST/click |
  | T5 | Login data repair | legacy latest waiting-flow preview/supersede | exact preview hash与逐项 approval | 数据不逆向改写；失败项保持 blocker，历史 flow 保留 |
  | T6 | View target repair | 精确 target preflight/rebind/readback | view migration/route 已 verify-active；独立 preview/approval | 失败保持 `target_entity_unresolved`；旧 fact/Action 保留 |
  | T7 | Resource isolation | 按预算逐服务 mem/pids与宿主/role CPU SLO | 前序 train 形成 startup/P99/CPU-seconds数据；每步30分钟 soak，最终24小时资源与 E4 | 任一触限/restart/CPU持续超线/E4回退立即停止后续服务，不批量抬限 |
  | T8 | Non-root hardening | 逐服务固定 UID/GID、volume/临时目录/healthcheck | T7 稳定且单服务权限矩阵通过 | 只回滚受影响服务，不改变已确认资源预算 |

- L3 workflow 必须消费该 train manifest并实际运行对应 planner/resource/typed E4；SSH/数据库/Telegram 证据不可得时标记 `blocked/production_unproven`。release live 后不得再次运行 all-task takeover。
- E4 分项证据（缺一不得写 `production_fixed`）：
  - RC-0b：被暂停 like Task 的 future pending 已按 RC-1 规则 4 完成回收分类；planner 恢复对该 lane 正常 JIT 规划后连续 2 个完整来源滚动窗口无 storm 复发；AI/view/comment/search lane typed E4 在止血与恢复全程不回退；resume 后 like 履约按 DueSet 结算而非历史 coverage 变量；
  - RC-0：精确 consumer清单全部 paused且无在途/unknown后才停止 OCR；停服后 OCR cgroup资源释放、其他任务 lane继续产生 typed remote fact；恢复先 ready后单Task canary，未出现重复 POST/callback、义务丢失或绕过 workflow 的本地识别；
  - RC-1/RC-4：连续 2 个完整来源滚动窗口无 not-yet-due/deadline 外新 Action；按 `DueSet = confirmed fact + open/pre-call + Gateway/unknown hold + terminal shortfall` 逐 source revision守恒；承诺目标的完成判定必须 `confirmed fact=DueSet` 且其余集合为0，不能把 hold/shortfall 当成功；planner CPU/RSS/SQL P99稳定且其他 lane 不回退；
  - RC-2：真实 429 可分类，claim/pre-call cooldown与已 claim job 释放生效；大目标群 immutable settlement 连续 2 日达到经配额核算可承诺目标，duplicate final gate/阈值不降低、无空正文/重复义务；
  - RC-3：验证码失败具备完整内层分类，local timeout占verification-required低于10%；`ready_idle` OCR CPU达标、24小时无未分类restart；per-source predicate eligible样本CPU-seconds P95较完整4+3基线下降至少30%、按生产mix加权的总体P95至少下降20%，各source fast/full unsafe与候选差异为0；受控与真实任务的search_click `target_click_observed/required`达到任务目标；
  - RC-5：当前 latest legacy waiting-flow 候选清零且有 AuditLog，连续 7 天无新增裸 TypeError；历史 flow 行仍保留；
  - RC-6：生产真实错误能从日志还原 trace，metadata client invalidation readback 证明精确 entry 已断开；
  - RC-7：平台 normal P99不超3760MiB，正常峰值 `MemAvailable>=1.5GiB` 持续24小时；宿主5分钟CPU P95不超70%且无连续10分钟高于85%，Planner/Dispatcher/OCR分角色满足RC-7阈值；无OOM、无持续swap-in/out，swap超阈值和容器触限/重启均有明确原因与owner readback；
  - RC-8：受控 operation 的 old/new target/config/route readback闭合，修复目标产生新 ViewRemoteFact且 task/target revision/route epoch 匹配，旧目标事实未串绑；
  - RC-9：workflow artifact 保存 manifest、expected/current SHA、migration/config hash、资源 readback与必需 E4；无固定历史 Task ID、无必需步骤 skip、无 post-release 第二次 all-task takeover；
  - RC-10：生产与 canary 均无新增 gateway `UnboundLocalError`；generic retry 对 success/unknown/Gateway-started 的重开数为 0，安全 pre-call retry 可沿原义务完成且不重复远端副作用。
- 任一 E4 未达成时保持对应 `production_regression_fix_pending` / 部分完成状态，禁止合并宣称。

## 9. Product Design Complete 自检

| 检查项 | 结论 |
|---|---|
| 用户原话覆盖 | CPU/内存是否同因、是否本服务导致、OCR近期无调用为何常驻、能否先停/降载，以及 AI 大目标、重复内容、点赞、搜索、浏览实体、登录、日志、代理/Telethon、Gateway/retry、root/限额与发布验证均映射到 RC-0～RC-10；2026-08-16 凌晨实测 planner OOM 风暴（§2.2）与用户批准的止血（§2.3）落入 RC-0b |
| 当前合同 | AI/view/source pacing/takeover 分别引用唯一权威专项；本文不恢复 legacy AI Action-first、账号天然键 view、future-tail source 排期或第二 takeover owner |
| 前端/状态 | 无新页面/停服按钮；capacity/admission/OCR/entity/unsafe-retry/resource/E4 走既有 blocker，OCR lane只从现有事实派生六态，不伪造第二套完成状态 |
| 后端/API/worker | C0 exact manifest、Task pause/resume、quiescence、OCR overlay停启，Planner/Dispatcher、source migration、Provider cooldown、adaptive OCR、登录、日志、Telethon、Gateway/retry、compose、实体同步和 workflow 均有明确 owner |
| 数据流转 | source ordinal additive migration与受控回收有分类/唯一键/settlement；OCR每source的base→可选剩余变体保持同deterministic request/generation，Dispatcher继续拥有候选/跨源票；AI normal Action=0；flow supersede与view rebind均保留历史和typed fact边界 |
| 权限安全 | C0/Action/flow/view/mihomo受控操作要求exact target、old-value/hash、actor/approval/SHA/epoch、preview/apply/readback；OCR worker不新增candidate原值输入，图片/OCR原文/凭据不落持久日志（API安全层另行立项） |
| 边界/失败路径 | pause后在途、quiescence超时、OCR cold start/restart原因不明、fast/full分歧、Provider重启惊群、duplicate basis、OCR unknown、实体专项未激活、Gateway partial、unsafe retry、swap/OOM、状态漂移、分 train回滚与证据 blocked均覆盖；C0b 增补：止血后风暴复发（comment 升级）、旧 drain 轮残余 OOM 判定、未部署 T2 前禁止 resume、T2 共享路径对 group_ai_chat 的回归守卫 |
| QA/发布/E4 | 26项基线QA + 11项C0/OCR/资源定向QA + 4项C0b定向QA，RC-0/RC-0b～RC-10均有分项生产证据；manifest不得为空/硬编码/skip，post-release只读verify，未达保持 pending/unproven |
| 迁移/回滚 | C0/OCR不新增业务表，reaction/source为additive migration；AI/view前置迁移沿权威专项。OCR回滚只关闭fast path回remote完整变体，不回local；数据apply不逆向改写，旧writer不复活；资源、代码和数据分train、独立停止线 |

结论：`product_design_complete / resynced_2026-08-16（含 C0b）`。开发必须按 C0/C0b/O0/T1～T8 独立 release train 实现和验收；任何实现期新增本文未列出的持久字段、改变 authoritative writer/唯一 active Provider/typed remote fact、扩大 retry 或受控 apply 集合、把 fast path降级为单 OCR/本地 fallback、合并 release train，均触发 product `resync`，不能只改代码。

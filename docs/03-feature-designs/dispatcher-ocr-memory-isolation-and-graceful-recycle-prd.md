# Dispatcher / OCR 内存隔离与优雅回收专项 PRD

## 0. 文档状态

- `intake_id`: `intake-2026-07-31-dispatcher-ocr-memory-001`
- `message_id`: `pdc-2026-07-31-dispatcher-ocr-memory-001`
- `related_incident`: `incident-2026-07-31-silicon-dispatcher-oom-001`
- `owner_agent`: `product`
- `level`: `L3`
- `priority`: `P0 containment / P1 root_cause_removal`
- `design_status`: `complete`
- `scope_revision`: `minimal_p0_p1_2026-07-31`
- `evidence_level`: `E4 incident / E3 production canary / E4 observing`
- `handoff_delivery_status`: `dev_implemented_qa_targeted_pass`
- `implementation_status`: `released_stage_b`
- `production_status`: `observing_unproven`
- `captcha_latency_validation`: `45s_contract / two_account_local_ocr_consensus_accepted / model_tail_unproven`
- `created_at`: `2026-07-31`
- `last_updated_at`: `2026-08-01`
- `truth_sources`:
  - `docs/01-product/tg-ops-platform-prd.md` §2.19、§2.20、§5.3
  - `docs/03-feature-designs/search-click-daily-fulfillment-remediation-prd.md`
  - `docs/00-index/project-dataflow-index.md` DF-178C
  - `docs/04-ops/deployment/PRODUCTION_RUNTIME.md`

> P0/P1 已按 `master -> release -> GitHub Actions` 发布到硅谷 Stage B，并取得两个不同账号的真实本地 OCR 共识与后续搜索成功证据。完整自然日、1287 次真实图片验证、OCR+模型 tail 及 3 次安全回收周期仍未满足，因此事故仍不得写 `production_fixed`。

### 0.2 2026-08-01 生产发布与 canary 证据

- Stage A 根治版 `1ff176fa` 由 Actions run `30666436804` 发布；14 分钟真实流量内新建搜索 epoch 正常 finalized，搜索成功 50 次，AI 成功 7 次，PostgreSQL/Action 新死锁均为 0，历史损坏 epoch 只保留一个 active quarantine 且不再循环重试。
- Stage B 使用同一代码由 Actions run `30667404412` 启用 `IMAGE_VERIFICATION_CONTRACT_ENABLED=true` 与 `IMAGE_VERIFICATION_OCR_BACKEND=remote`；线上 release 为 `20260731215058_1ff176fa`。两路 Dispatcher 均为 512 MiB/120 秒，OCR worker 为 768 MiB/90 秒；容器健康、restart=0、OOMKilled=false。
- 账号 171 与 105 的真实 challenge 均在 45 秒 callback contract 内由 RapidOCR+ddddOCR 同票形成 `consensus_submitted`，`contract_version=image-verification-v1-20260801`、`consensus_source=local_ocr`、`model_started=false`，随后同一 Action 取得真实搜索目标点击成功。
- 单实例 busy-fast-fail 按 §6.1 显式产生 `verification_local_ocr_busy`，未点击、未排除账号且 Planner 继续补机会；Stage B 前 15 分钟搜索成功 22 次。是否触发 §2.4 队列/HA 后置立项，必须等完整日容量 E4，不以瞬时 busy 比例直接改架构。
- 当前仅可写 `E3 production canary pass / E4 observing`：尚无完整自然日、1287 次图片、OCR+模型 tail 双账号 callback accepted、3 次阈值安全回收和当日搜索完整 ledger 达标证据。

### 0.1 2026-07-31 实现记录

- P0：`services/image_verification_runtime.py`、`image_verification_sources.py`、`search_join_image_solver.py` 与 `ai_gateway.py` 已实现共享固定槽、本地优先、单模型 hedge、active registry 和统一 remaining budget；`search_join.py` 已实现 message identity/deadline audit 与 callback 前同 fingerprint 复读。
- P0 生命周期：`dispatcher_lifecycle.py`、`worker.py` 与 `telethon_lifecycle.py` 已实现阈值/SIGTERM stop-new-claim、DB/attempt/future safe probe、断连失败显式阻断的 strict Telethon shutdown 和 successor-heartbeat 释放的 Redis rolling lease。
- P1：`image_verification_worker.py`、`image_verification_worker_{contract,config}.py`、`image_verification_client.py` 及两个 Compose override 已实现解码前单请求 busy-fast-fail、deterministic POST/GET、`expired` terminal fact、generation unknown、私网认证、请求终态后的请求数/soft RSS 回收、容器隔离与 remote no-native-fallback。
- 当前证据仅为本地自动化与静态检查；生产参数仍必须来自两个受控账号 callback canary 和 production-like memory/drain 测试，不能使用代码默认值推断。
- 当前最终相关回归为 `352 passed, 30 deselected, 5 warnings`；完整 `backend/tests -m no_postgres` 在 60 秒硬上限运行到 57% 且此前无失败，但未跑完，严格记为 incomplete。PostgreSQL/Redis 集成、镜像构建、Actions、真实 callback canary 和 memory soak 均未完成。

## 1. Intake Card

- `source`: `user + prod-diagnosis`
- `raw_input`: 按代码优化和根治方案形成优化 PRD；评估 Dispatcher 是否应在使用完成后重启释放内存。
- `suspected_type`: `online_issue + runtime_architecture`
- `affected_surface`: 硅谷生产主机、两个 Dispatcher、极搜图片验证码、搜索点击履约、Telegram session、Recovery、Docker/WorkerHeartbeat 运行观测。
- `production_related`: `true`
- `initial_evidence_level`: `E4`
- `next_route`: `product_design_complete -> dev -> qa -> product -> prod-diagnosis`

### 1.1 已知生产事实

1. 2026-07-31 同一旧 boot 内出现两次整机 OOM；内核分别杀死约 1.72 GiB、2.51 GiB RSS 的 Python 进程，生产 `execution_attempts.worker_id` 将两个 victim 精确映射到两个 Dispatcher 容器。
2. OOM 前两个 worker 的 Action 以 `search_join` 为主；第二个 victim 有 12 个被中断或结果未知的调用，接近当前单 Dispatcher 13 路有效并发。
3. 当前图片 challenge 会为每题创建三线程 executor，并发启动 RapidOCR、ddddOCR 与多模态模型。两路 OCR 先形成共识时，`cancel()` 不能保证停止已经运行的 model future，`shutdown(wait=False)` 也不等于资源已经释放。
4. RapidOCR 与 ddddOCR native engine 由进程级缓存长期驻留；已有串行锁只能限制同时推理，不能缩短 native 运行时的生命周期。
5. 最终卡死前当前 release 已处理 761 个 `search_join`，记录 1287 次 image verification attempt。主机约 7.4 GiB、无 swap，相关容器无 memory limit。
6. 重启后两个 Dispatcher RSS 回到约 261/265 MiB，容器与 API 恢复健康；但当时 native OCR 库尚未重新加载，因此只证明 liveness，不证明搜索/OCR 高负载稳定。
7. 时延/窗口实测：生产同镜像、1 GiB/1 CPU 的 RapidOCR 四变体 cold 为 18.3015 秒、峰值 RSS 626.4 MiB、无 OOM；本地 ddddOCR cold/warm 为 0.5918/0.0988 秒，RapidOCR cold/warm 为 9.6196/9.3240 秒。生产视觉模型 6 次脱敏合成图均正确，首轮 3.0686 秒，后 5 次 p50 2.0124 秒、max 4.5817 秒，但 30 秒 timeout 与空 final content 二次请求仍可令尾部接近 60 秒。两个真实账号只发冻结关键词、不点击答案时，同一验证码 70.42 秒仍保持相同消息/revision/图片/10 个按钮，数分钟后消失；这只证明可见下界和页面生命周期，不证明 70 秒 callback 被接受。

### 1.2 根因结论边界

| 结论 | 状态 | 说明 |
| --- | --- | --- |
| 主机因内存耗尽卡死 | `confirmed` | kernel OOM 与整机行为一致 |
| 两次 OOM victim 都是 Dispatcher | `confirmed` | 容器 ID、worker_id、PID 和 Action 事实闭合 |
| 极搜验证码链是主要触发负载 | `highly_likely` | OOM 前动作组成、并发与发布时序均指向该链 |
| per-challenge executor、native OCR 常驻与未结束 model future 造成峰值/滞留 | `likely` | 代码机制与重启后 RSS 回落一致 |
| 最终卡死前最后一笔 native allocator 的精确归因 | `unproven` | 没有进程级 heap/native profile |

本方案不把 `likely` 写成已经完成的精确根因证明；P0 先消除已知危险结构并提供回收能力，P1 用进程隔离从架构上切断 Dispatcher 与 native OCR 的共同生命周期。

## 2. 产品决策

### 2.1 对“Dispatcher 使用完成后重启释放”的回答

可以回收，但不能“每个 Action 做完立即重启”，也不能由外部 cron 定时 `restart/kill`：

- 每个 Action 后重启会反复重建 Telegram client、数据库连接和运行缓存，降低履约，并扩大 Gateway 前后结果未知窗口。
- 定时硬重启无法知道线程、Telegram RPC、数据库 CAS 或 callback 是否已结束，可能造成重复点击、unknown 和份额泄漏。
- 正确做法是 **阈值触发的 drain-and-recycle**：停止新 claim，等待当前 drain 的全部 future 和业务状态收口，只在安全检查点主动正常退出，由 Docker restart policy 拉起。
- Dispatcher 回收是 P0 止血，不是根治。P1 必须让 native OCR 离开 Dispatcher，使日常 OCR 内存回收不再依赖重启 Telegram 执行进程。

### 2.2 目标

- 消除每个 challenge 三路并发和“已运行 future 假取消”的内存峰值来源。
- 在不重复 Telegram callback、不破坏 claim/assignment/unknown 契约的前提下，让单个 Dispatcher 可滚动优雅回收。
- 将 RapidOCR/ddddOCR 迁移到独立、低并发、可回收的内部运行单元。
- 使用现有 Action/result、ExecutionAttempt、WorkerHeartbeat 与结构化日志记录图片验证、资源阈值和回收事实；P0/P1 不新增挑战级运营后台。
- 将验证码观测、各识别源耗时、模型 hedge、callback 最迟提交和回执耗时纳入同一 deadline 预算，避免内存优化把识别关键路径拉长到验证码失效。
- 保持 2/3 共识、批准 callback、远端 solved 事实、Dispatcher 公平份额与业务日目标不变。

### 2.3 非目标

- 不提高或降低搜索点击、AI 活群、评论、点赞、浏览的任务目标或份额。
- 不放宽验证码 2/3 共识，不允许模型单票点击，不新增验证码绕过能力。
- 不把 swap、扩容主机、降低 Dispatcher 数量或降低业务并发当根治。
- 不允许同时回收两个 Dispatcher shard，不用容器 healthy 代替业务完成。
- 不保存验证码图片，不给 OCR worker Telegram session、业务数据库写权限或 AI Provider 密钥。
- 不增加 mock success、静默 fallback 或“出错后继续点一个最像答案”的路径。
- P0 不新增一套 session/action 执行锁，不重构既有 Action lease、账号 inflight、fingerprint 与 callback unknown/CAS 边界。
- P0/P1 不建设持久优先级队列、EDF 调度、多 OCR worker HA、事件溯源/长期归档或自动 callback 校准平台；这些只有独立证据证明需要时才另立需求。
- 本轮不增加运营中心的 worker 回收按钮、挑战级 deadline/hedge 页面或新的前端权限。

### 2.4 明确后置项

| 后置能力 | 本轮替代方式 | 重新立项触发条件 |
| --- | --- | --- |
| 完整 session fencing token / 锁序重构 | 复用 Action lease、账号 inflight、fingerprint 与 callback unknown/CAS | 故障注入或生产出现旧 worker/Recovery 对同 fingerprint 重复 callback |
| 持久 FIFO/EDF OCR 队列 | 固定槽 deadline-aware wait；P1 busy 显式拒绝 | P1 capacity canary 证明无排队时验证码成功率无法达标，且不是模型/OCR时延问题 |
| 多 OCR worker HA/扩缩容 | P1 单实例、Docker restart、generation unknown | 单实例容量或可用性 E4 不达标 |
| 验证码事件溯源表/长期归档 | 现有 Action/result、ExecutionAttempt 与结构化日志 | 现有字段无法完成重复 callback/时延/内存验收 |
| 运营中心 worker/挑战页面 | WorkerHeartbeat、容器指标、日志与 Action/result | 线上运维无法用现有证据在规定时间内定位 drain blocker |
| 自动 callback 校准平台 | Release Gate 受控账号最小 canary | bot/protocol 窗口高频变化导致人工 canary 无法维护 |

## 3. 设计总览

### 3.1 分阶段目标架构

```text
P0
Dispatcher
  -> 在现有 search_join audit 记录 challenge_observed_at / callback_submit_deadline
  -> RapidOCR / ddddOCR 各使用一个进程级固定槽；槽等待受同一 deadline 限制，不建设持久队列
  -> 本地同票且早于 model_hedge_at：不启动模型；分歧/到 hedge 点：最多一个模型
  -> 任意两张安全票同票且仍在窗口：唯一 callback CAS；过期：deadline_exceeded，不点击
  -> 保存 attempt / callback / 远端事实；drain 边界检查后单 shard 安全退出并拉起

P1
Dispatcher（Telegram 与业务事实 owner）
  -> Docker 私网调用 image-verification-worker
       -> 单请求执行；busy 立即显式拒绝，不等待、不建设业务队列
       -> request_id 可查询 running/completed/failed/expired；generation 丢失显式 unknown
       -> RapidOCR + ddddOCR 返回两张本地票与逐源耗时
       -> 独立内存上限 / 请求边界 recycle
  -> Dispatcher 按同一 deadline 必要时 hedge 一个已审批模型
  -> 2/3 共识、callback CAS、远端事实
```

### 3.2 责任边界

| 能力 | Dispatcher | image-verification-worker |
| --- | --- | --- |
| Action / assignment / claim owner | 是 | 否 |
| Telegram session 与页面读取 | 是 | 否 |
| 下载当前 challenge 图片 | 是 | 否 |
| RapidOCR / ddddOCR native inference | P0 是，P1 否 | P1 是 |
| 多模态模型选择与调用 | 是 | 否 |
| 2/3 共识与候选约束 | 是 | 否，只返回各引擎单票 |
| callback CAS 与点击 | 是 | 否 |
| solved / rejected / unknown 远端判定 | 是 | 否 |
| 业务数据库写入 | 是 | 否 |
| 请求状态 | 现有 Action/result + deterministic request_id | 仅保存短期最小 request 状态，无业务结果 |
| 自身内存回收 | 业务安全点优雅回收 | 当前 request 收口后的独立回收 |

## 4. P0：验证码 deadline-aware 有界识别

本节自生效合同版本起 supersede 主 PRD §2.19.2 第 2 步“三路同时启动”和 §2.19.6 对“模型已提前启动”的旧验收描述；其余 2/3 投票、候选约束、换题预算、单次 callback CAS 和远端 solved 规则保持不变。

### 4.1 决策流程

1. Dispatcher 冻结 `challenge_fingerprint_hash`、有序 callback 候选、题型与图片 hash，在现有 search_join audit 写入 `challenge_observed_at`、contract version 和由生产校准参数计算的 `callback_submit_deadline`；P0 不新增数据库表。
2. RapidOCR 和 ddddOCR 分别使用一个进程级共享固定执行槽；两者可在单题内有界重叠，但不得由每个 Action 创建 executor。获取槽的等待属于当前调用的 deadline-aware wait，不持久化、不重排，也不形成独立业务队列；等待超过本题剩余识别预算时，该 source 显式记 `timeout`。每个引擎的预处理变体仍只能聚合成该引擎的一张票。
3. 对每张票执行题型解析、字符规范化和 callback 候选精确匹配；不安全答案记为 `unsafe_or_no_candidate`，不参与跨源投票。任意两张已完成安全票相同即形成 2/3 共识，不要求第三源完成。
4. 两张安全本地票在 `model_hedge_at` 前相同：写 `local_consensus`，`model_status=not_started_local_consensus`，立即进入提交前页面复核与 callback CAS。
5. 两张已完成安全本地票不一致，或到达 `model_hedge_at` 仍未形成两张同票时，才启动一个当前健康且已审批的多模态模型。同一 challenge 最多一次模型调用；不串行切换多个 provider 拼票。
6. 模型票必须命中 callback 候选，并与任一安全本地票一致才形成共识。两张本地票均已完成且都不安全时，模型单票不可能满足 2/3：未启动则写 `not_started_no_safe_local_vote`；已因 hedge 启动的结果只记审计，不得点击。
7. 形成共识后必须重新读取同一 message/fingerprint；只有当前时间早于 `callback_submit_deadline` 且页面仍为同一 challenge 才允许进入唯一 callback CAS。页面变化或超过 deadline 写显式 required/deadline blocker，按既有 challenge 预算换题，不得点击旧答案。
8. 未形成共识时禁止点击；进入 callback 后沿用 8 秒页面回执轮询和 unknown 防重合同。页面仍可见、callback RPC 已发出、页面回执成功必须分别记录，不能互相替代。

### 4.2 Deadline 与 hedge 计算

- `verified_callback_acceptance_seconds` 必须来自发布前受控账号的真实延迟 callback canary；本轮只读影子得到的 `page_visible >=70.42s` 不能代替该值。
- `callback_submit_deadline = challenge_observed_at + verified_callback_acceptance_seconds - callback_submit_headroom_seconds`。headroom 覆盖提交前同 fingerprint 复读、callback RPC p99、时钟/调度抖动，并由 canary 证据固化，不能写拍脑袋常量。
- `model_hedge_at = callback_submit_deadline - model_tail_budget_seconds`。模型 tail budget 使用当前审批 provider 的生产 p95/p99、timeout/retry 合同和网络余量计算；若剩余预算不足以完成一次模型请求，模型以 `verification_model_budget_insufficient` 显式不启动。
- OCR 槽等待、OCR 推理、模型首请求与 reasoning retry 共用同一个 `callback_submit_deadline`。每次模型网络请求的 timeout 必须为 `min(provider_timeout, remaining_budget)`；只有剩余预算足够覆盖 retry 最小预算时才允许 retry，禁止首请求和 retry 各自跑满 30 秒。
- P0 在同一 Dispatcher 进程中使用 UTC 时间写审计，使用 monotonic remaining budget 控制槽等待和网络 timeout；P1 跨进程请求同时传 `deadline_at` 与发送时的 `remaining_budget_ms`，worker 取更小值，避免 wall-clock 调整扩大预算。
- 晚到结果只记 `late_result`，不得进入共识或 callback，且等待超时不代表已运行 native/model 调用已经释放。未完成 callback canary 校准时，新 contract fail closed 为 `verification_deadline_not_calibrated`，不得沿用 Telethon 60 秒、页面可见时间或模型正常 p50。

### 4.3 执行限制

- 禁止为单个 challenge 创建三线程 executor。
- 禁止把 `Future.cancel()`、`shutdown(wait=False)` 或超时返回视为已释放底层模型/native 资源。
- 外部模型调用和 OCR 推理期间不得持有数据库事务。
- P0 在进程内执行 OCR 时必须使用进程级共享固定执行槽；同一 native engine 的有效并发固定为 1，不得由 Dispatcher Action 并发放大。RapidOCR 与 ddddOCR 是否在单题内重叠由该服务级调度器控制并受内存预算验证，不得退化成严格串行后无 deadline 等模型。
- 固定槽只限制 OCR native 并发，不获得 Action、账号、session 或 callback 所有权；P0 继续使用既有 Action lease、账号 inflight、fingerprint 复核和 callback unknown/CAS。
- 模型只按第 5 步调用一次。模型已经启动后，即使两张本地票随后形成共识，callback 可按同 fingerprint/deadline 继续，但该 model future 必须保留在进程级 active registry，直到真实完成或其剩余预算耗尽；不得以 `cancel()` 成功与否决定资源已释放。

## 5. P0：Dispatcher drain-and-recycle

### 5.1 状态机

| 状态 | 进入条件 | 允许行为 | 离开条件 |
| --- | --- | --- | --- |
| `active` | worker 启动并取得 shard 身份 | heartbeat、claim、执行 | 达到回收触发条件 |
| `recycle_requested` | 自动阈值或 SIGTERM | 本轮已 claim 项继续执行；拒绝下一轮 claim | 自动回收取得最小 rolling-recycle lease；发布停机直接进入 draining |
| `draining` | 自动回收已取得 lease；或计划停机已收到 SIGTERM | 维持 heartbeat，等待 futures/业务边界收口 | 全部安全条件通过或出现 blocker |
| `drain_blocked` | 安全条件在观测窗内不能闭合 | 不新 claim，持续暴露 blocker 和告警 | blocker 被事实性收口后回到 draining；不自动回 active |
| `safe_to_exit` | 安全谓词全部为真 | 关闭 Telethon/client、连接与 executor，写退出审计 | 正常退出码 0 |
| `restarted` | Docker 拉起新 instance | 写 started/restarted 审计并恢复 active | 正常运行 |

禁止设置“等待超时后直接 kill 并当成功”的分支。异常退出仍由现有 Recovery 按 Gateway 边界处理，但不能被产品设计成正常回收路径。

### 5.2 唯一安全检查点

自动回收检查只允许发生在 `drain_task_dispatcher` 本轮已 claim futures 全部返回之后、下一次 claim 之前。任何 Action future、model future 或 OCR 调用仍在运行时都不能进入 `safe_to_exit`。

### 5.3 `safe_to_exit` 必须同时满足

- worker lifecycle state 不是 `active`，claim 入口已 fail closed。
- 当前进程内 Action futures、模型 futures、OCR 调用均为 0。
- 数据库中没有 `claim_owner/worker_id` 属于该 worker instance 的 `claiming|executing` Action。
- 没有该 worker 发起且仍处于 `gateway_call_started/callback_submitting`、结果尚未持久化的 Attempt。
- 所有本轮 dispatch reservation 已按统一 `finally` 释放或绑定到明确的 durable Action/unknown 事实。
- 图片验证 fingerprint、deadline、逐源票和 callback/unknown 事实已写入现有 Action/result 或 ExecutionAttempt；没有把只存在进程内的“已点击”当成已提交事实。
- Telethon clients 已完成断连；断连错误必须显式记录并阻止“安全退出成功”审计。
- 现有 WorkerHeartbeat metadata/结构化退出日志已写 trigger、RSS、处理量和安全检查快照；P0 不新增 lifecycle audit 表。

### 5.4 触发条件与资源预算

回收条件由配置显式启用，任一条件满足即可请求回收：

- 进程 RSS 达到 `DISPATCHER_RECYCLE_SOFT_RSS_BYTES`。
- 容器 `memory.current` 达到 `DISPATCHER_RECYCLE_SOFT_CGROUP_BYTES`。
- 自启动以来完成的图片 challenge 达到 `DISPATCHER_RECYCLE_OCR_ATTEMPT_LIMIT`。
- 运行时长达到 `DISPATCHER_RECYCLE_MAX_UPTIME_SECONDS`。

阈值不得在代码中写拍脑袋常量。每台主机的 Dispatcher 可用预算按以下输入计算并记录在 Release Gate：

```text
dispatcher_total_budget_p0
  = host_memory
  - host_os_and_docker_reserve
  - p95_other_containers_including_database_and_redis
  - database_and_redis_growth_headroom_above_that_p95

per_dispatcher_hard_budget_p0
  = dispatcher_total_budget_p0 / dispatcher_replica_count

soft_recycle_threshold
  < per_dispatcher_hard_budget_p0
    - p99_memory_growth_during_drain
    - measured_restart_and_client_shutdown_headroom
```

`p95_other_containers_including_database_and_redis` 已包含数据库与 Redis 当前占用；下一项只计算其在观测窗内高于该 p95 的增长余量，禁止重复扣减。P1 另行加入 `image_verification_worker_hard_budget` 后重算 Dispatcher 预算。Release owner 必须用两台 Dispatcher 同时加载 RapidOCR/ddddOCR 的 production-like canary 填入环境变量并保存快照；容器 hard limit 是宿主保护线，不能替代 soft recycle。

### 5.5 双 Dispatcher 协调

- 自动阈值回收复用 Redis 实现一个最小 rolling-recycle lease，保证同一时刻最多一个 Dispatcher 处于 `draining|safe_to_exit`；lease 使用 owner token 做 compare-and-renew/release，TTL 大于 canary p99 drain 并在 draining 期间续租。该 lease 不参与 Action claim、账号/session 所有权或 callback。
- lease 只需要 worker instance UUID、shard、requested_at、expires_at；新实例不得复用旧 instance ID。
- lease 不可用、续租失败或 owner token 不匹配时，当前 worker 保持停止新 claim 并写 `drain_blocked=recycle_lease_unavailable|lost`，不得把自己记为 safe-to-exit；恢复 lease 事实后再继续。计划发布的 SIGTERM drain 不依赖该运行时自动回收 lease。
- 一个 Dispatcher drain 期间，另一个保持原公平份额和安全并发；不得把全部搜索或 AI 业务强行迁移到单 shard。
- 第二个 Dispatcher 只能在第一个新实例 heartbeat 稳定、shard identity 正确、回收 lease 已释放后开始回收。

### 5.6 SIGTERM 与发布停机

- worker 收到 SIGTERM 后立即设置进程级 stop/drain event；主循环不再进入下一轮 claim，当前 `drain_task_dispatcher` 按原业务结果完成后执行 `safe_to_exit` 检查。
- Compose `stop_grace_period` 必须大于 production-like canary 测得的当前轮 p99 drain + client disconnect headroom；超过 grace 被容器终止只能写 `abnormal_exit`，不能写 graceful recycle。
- 运行时自动回收必须逐 shard；计划发布可以在现有全 worker fence 窗口内让两个 Dispatcher 同时停止领取，但仍必须分别完成自己的 current batch/unknown 边界，不能把发布 stop 当作自动滚动回收成功证据。
- 逐 Action 重启、固定时间 cron restart 和“超时后 kill 记成功”均不进入 P0。

## 6. P1：独立 image-verification-worker

### 6.1 部署与隔离

- 新增独立容器/进程角色 `image-verification-worker`，只接入 Docker 私网，不映射宿主端口。
- 仅加载 RapidOCR 与 ddddOCR；同时处理的 challenge 请求数固定为 1。请求处理期间两个引擎各占一个服务级固定槽并可在单题内有界重叠。worker 不建立等待队列、持久队列、优先级队列或 EDF；已有 request running 时，新请求立即返回 `verification_local_ocr_busy`。
- 不挂载 Telegram session，不注入 AI Provider、数据库业务账号或租户密钥，不执行 callback。
- 容器必须有 soft recycle threshold、hard memory limit、请求计数和当前 request 状态；正常回收只在当前 request 进入终态后发生。native 调用超过 deadline 仍未返回时，整个 OCR worker generation 可以异常退出并由 Docker 重建，但必须向 Dispatcher 暴露 generation changed/unknown，不能伪造 completed。
- Dispatcher P1 路径禁止本地 native OCR fallback。服务不可用必须显式返回 required/transport blocker，不能在 Dispatcher 内重新加载 OCR 隐藏故障。

### 6.2 内部请求合同

`POST /internal/v1/image-verification/ocr`：

| 字段 | 规则 |
| --- | --- |
| `request_id` | 由 `action_id + challenge_fingerprint_hash + contract_version` 确定性生成；相同输入查询/重试复用，输入 hash 不同不得复用 |
| `action_id` | 仅作关联 ID，不授权业务写入 |
| `challenge_fingerprint_hash` | 必填且与 Dispatcher 持久 attempt 一致 |
| `image_bytes` / `mime_type` | 仅请求内存中传递；类型、尺寸和解码边界必须校验 |
| `verification_kind` | `math` 或 `alphanumeric` 受控枚举 |
| `candidate_hash` | 有序候选的 hash，用于请求一致性；原候选可在 Dispatcher 侧做最终匹配 |
| `deadline_at` | Dispatcher 计算的绝对 recognition deadline；worker 拒绝无 deadline 或已过期请求 |
| `remaining_budget_ms` | Dispatcher 发送前按 monotonic timer 计算；worker 以它和本地 `deadline_at-now` 的较小值执行 |
| `contract_version` | 必须与运行版本一致，不兼容时 fail closed |

同步完成时响应只包含 `request_id`、两引擎各自的规范化候选/状态/started_at/completed_at/duration/late 标记、`worker_instance_id`、`worker_generation`、contract version 和有界错误码。图片、模型 prompt、Telegram 正文和按钮原文不得出现在响应、状态存储或日志中。

最小请求状态合同：

- `GET /internal/v1/image-verification/ocr/{request_id}` 只返回 `running|completed|failed|expired|unknown`、输入 hash、worker generation、started/completed 时间和完成结果；不返回图片。
- worker 在内存中保存 request 状态；running 记录不得被 TTL 驱逐，terminal TTL 必须不短于最大 verification deadline + recovery 观察窗。不引入业务数据库、Redis 队列或持久任务队列。重复 POST 输入 hash 相同且状态为 running 时返回同一 running；completed/failed/expired 返回同一终态；同 request_id 输入 hash 不同返回 contract conflict。
- Dispatcher HTTP timeout 后先查询同 request_id。相同 generation 仍 running 时禁止重发；generation 已变化或状态明确 unknown 时，必须先重新读取 Telegram 同一 message/fingerprint，只有页面未变且剩余 deadline 足够才可重新发起。
- worker generation 丢失使 request 状态不可恢复时显式返回/推断 `unknown`，不能把 404、连接断开或容器重启解释为未执行。

### 6.3 错误处理

| 错误码 | 业务处置 | 是否排除账号—协议路径 |
| --- | --- | --- |
| `verification_local_ocr_unavailable` | 保持 required，持久化 transport blocker，稍后复用同 fingerprint 恢复 | 否 |
| `verification_local_ocr_busy` | 已有 request running；立即拒绝，不等待、不调用 callback | 否 |
| `verification_local_ocr_timeout` | 客户端停止等待并查询 request 状态；只有 completed/failed/expired 或 generation changed 才算已知，running/unknown 不得立即重发 | 否 |
| `verification_deadline_not_calibrated` | 新 contract 禁止执行；Release Gate 补齐真实 callback canary 参数后再启用 | 否 |
| `verification_deadline_exceeded` | 不接受新票、不 callback；只复读同一页面并按 challenge 预算处理 | 否 |
| `verification_model_budget_insufficient` | 剩余提交预算不足以完成一次模型请求，模型不启动或显式有界终止 | 否 |
| `verification_payload_invalid` | 当前 attempt 显式失败并报警，禁止调用模型/点击 | 否 |
| `verification_payload_too_large` | 拒绝处理并报警，禁止静默压缩后继续 | 否 |
| `verification_contract_mismatch` | Release Gate/runtime contract 失败，禁止 fallback | 否 |
| `verification_no_safe_local_vote` | 不调用模型；按现有 challenge 预算保持 required/换题 | 否 |
| `verification_consensus_missing` | 禁止点击；按现有 challenge 预算处理 | 否 |
| `verification_callback_result_unknown` | 只复探远端页面，禁止再次 callback | 否；也不得写 solved |
| `dispatcher_recycle_drain_blocked` | worker 保持 draining 并告警，不硬退出 | 不适用 |

只有既有 `jisou_image_verification_failed` 明确条件成立时才触发 12 小时排除；基础设施、传输、回收或一次无共识不得伪装成业务最终失败。

## 7. 最小审计、幂等与恢复

### 7.1 P0 复用现有结果存储

P0 不新增 `SearchJoinImageVerificationAttempt`、source event 或 lifecycle 表。每个 challenge 继续写入现有 search_join result / `Action.result` / `ExecutionAttempt`，至少补齐：

- `challenge_fingerprint_hash`、bot peer hash、message ID/revision、image hash、candidate hash、challenge ordinal 与 contract version。
- `challenge_observed_at`、`model_hedge_at`、`callback_submit_deadline` 和校准证据版本。
- RapidOCR、ddddOCR、model 的 `not_started|waiting_slot|running|complete|unsafe|timeout|late|failed`、耗时和一张规范化 vote；模型启动原因只允许 `local_divergence|deadline_hedge`。
- `consensus_source`、模型是否实际启动、callback submitted/response 时间、最终 page classification 和全部显式错误码。

图片字节、机器人正文、按钮原文、Prompt、密钥和 Telegram session 不落库。P0 不做通用状态机迁移；既有 `required|failed|success` 与 callback unknown 防重口径保持不变。

### 7.2 既有所有权边界

- P0/P1 复用既有 Action lease、进程内/Redis 账号 inflight、同 fingerprint 复核和 callback unknown/CAS；不增加第四套 session fence，也不改变这些既有锁的顺序或 TTL。
- 同一 fingerprint 最多提交一次 callback。提交前必须重新读取同 message/fingerprint 并检查 deadline；Telegram RPC 已发出但结果不明时沿用既有 unknown 防重，只远端复探，不重新点击。
- OCR 固定槽和 P1 OCR request 都不拥有 Action、session 或 callback；OCR 完成/失败只能产生识别票或 blocker。

### 7.3 P1 同题恢复

- Dispatcher 从当前 Telegram page 取得并审计 `bot_peer_hash + message_id/revision + image_hash + callback_matrix_hash`；不保存图片。恢复时重新读取同一消息并重新计算 fingerprint。
- HTTP timeout 后先按 deterministic `request_id` 查询 worker。相同 generation 为 `running` 时继续等待到本题剩余 deadline 或保持 required，禁止重发。
- worker generation 已变化/明确 unknown 时，只有重新读取仍为同一 fingerprint 且剩余预算足够，才允许重新发送同一 deterministic request；页面变化、消息消失或 deadline 已过直接保持 required/deadline blocker。
- 模型调用不做跨进程恢复。Dispatcher 异常退出且无法证明模型未执行时，不为旧 challenge 再发模型；重新读取页面后按新/同 fingerprint 和剩余 deadline 决定 required，绝不据旧内存票点击。

外部 OCR、模型和 Telegram 调用期间不新增长数据库事务。重复 request/fingerprint 只能返回或补写同一事实，不能产生第二次 callback。

## 8. 最小可观测性

本轮不新增 `WorkerLifecycleAudit` 表、运营中心页面或人工回收按钮。实现复用现有 WorkerHeartbeat metadata、容器指标与结构化日志，至少暴露：

- worker instance、role/shard、release SHA、contract version、`active|recycle_requested|draining|drain_blocked|safe_to_exit`。
- RSS、cgroup memory、uptime、处理 challenge 数、recycle trigger。
- 当前 Action/model/OCR future 数、Gateway 是否 open、safe-to-exit blocker。
- OCR 槽 wait/execute duration、模型是否启动、模型 request/retry duration、deadline exceeded、late result。
- P1 request_id、worker generation、`running|completed|failed|expired|unknown` 和 busy 次数；不得记录图片或原文。

正常退出写结构化 `safe_to_exit` 日志；进程死亡只能由 Docker/新实例观测为 abnormal，不补造 graceful。生产验证从日志、Heartbeat、容器和 Action/result 汇总证据，不以新前端为交付条件。

## 9. 安全与权限

- 内部 OCR endpoint 只允许 Docker 私网访问，并使用服务身份或等价的请求认证；非法/过期身份明确拒绝。
- 校验 MIME、解码后尺寸、像素数、请求大小、枚举和 contract version；拒绝畸形图片，防止解码炸弹。
- 图片只在请求内存和处理期存在，处理结束立即释放引用；日志只存 hash、尺寸、状态与耗时。
- OCR worker 运行非 root、只读文件系统、最小 Linux capability，不挂载 session/media 持久目录。
- 不把密钥、Prompt、Telegram 原文、图片或按钮原文写入 lifecycle/verification audit。
- P1 request 状态 TTL 到期只删除内存记录；状态缺失必须结合 worker generation 解释为 unknown，不能默认未执行。

## 10. 发布、配置切换与回滚

### 10.1 Stage A：P0 止血

1. 不做数据库 migration；在现有 search_join audit/Action result 增加 deadline、逐源耗时、模型调用和回收证据字段。
2. 实现进程级 RapidOCR/ddddOCR 固定槽、统一 remaining budget、最多一个模型、active model registry、SIGTERM drain 和最小 rolling-recycle lease。
3. Compose 为两个 Dispatcher 设置经 canary 证明的 memory hard limit 与 stop grace；同一 release 使用同一 verification contract version。
4. 先在一个 Dispatcher/shard canary，验证模型调用次数、deadline、RSS 和 callback 为 0 重复，再切第二个 shard；运行时自动回收始终单 shard。

### 10.2 Stage B：P1 根治

1. 部署单实例 `image-verification-worker`、私网认证、memory limit、request 状态 GET 与 worker generation 观测；不部署持久队列或多实例 HA。
2. 先让一个 Dispatcher 使用 deterministic request_id 远端 OCR，故障时显式 required，禁止本地 native fallback；通过后切第二个 Dispatcher。
3. 切换成功后删除 Dispatcher 镜像中的 native OCR 运行入口和依赖加载路径；不得长期保留静默双路径。
4. 验证同 request 重复 POST、HTTP timeout 后 GET、worker 重启 unknown、同 fingerprint 重试和页面变化禁止重试，再认定 P1 request/recovery 合同完成。

### 10.3 回滚底线

- Stage B 失败可回到 Stage A 的进程内共享固定 OCR 执行槽 + deadline-aware 单模型 hedge + Dispatcher 优雅回收。
- 禁止回滚到“每 challenge 三线程并行 + 已运行模型 future 假取消”的事故结构。
- P0/P1 不依赖新数据库 migration；回滚只切执行路径和镜像，不删除既有 Action/result、ExecutionAttempt、callback unknown 或远端事实。
- 任一回滚保留既有 Action lease、账号 inflight、fingerprint 复核、callback unknown/CAS，不得清空后重试。
- swap 只能作为单独经批准的主机缓冲措施，并记录容量与磁盘风险；它不改变本 PRD 完成口径。

## 11. QA 与验收

### 11.1 单元测试

- 两本地 OCR 在 `model_hedge_at` 前同票时模型调用次数为 0；本地分歧立即且只调用一个模型，本地仍 pending 到 hedge 点也只启动一个模型，且模型必须与安全本地票一致才形成 2/3。
- 两本地源均完成且无安全票时模型不得单票 callback；deadline 未校准、结果晚到、fingerprint 变化或模型预算不足时 callback 次数均为 0。
- 本地 OCR 先完成、模型先完成和三源近同时完成的竞态均只产生一个 consensus 和一个 callback CAS。
- 同引擎多预处理只形成一票；候选不命中不能进入共识。
- 不创建 per-challenge 三线程 executor，不存在已运行 model future 被当作成功取消的路径。
- 生命周期状态转换、rolling-recycle lease 竞争、SIGTERM 与 blocker 均为确定结果；lease 不参与 Action/session 所有权。
- `safe_to_exit` 任一谓词为假时禁止退出；两个 shard 不能同时进入 draining。
- 现有 search_join audit 完整记录 deadline/votes/model/callback；fingerprint/callback CAS 重复执行不产生第二次点击。
- 每次模型首请求/reasoning retry timeout 均不超过调用当时 remaining budget，预算不足时 retry 次数为 0。

### 11.2 集成与故障注入

- 使用真实 PostgreSQL/Redis 验证既有 Action lease、账号 inflight、callback unknown/CAS 与最小 recycle lease 不互相改变锁序且无 deadlock；本轮没有新 session fence。
- 在 OCR 槽等待、OCR 后、模型中、callback 前、callback 后/结果落库前、Telethon 断连中分别发送 SIGTERM，验证 current batch 收口、unknown 防重和重复 callback 为 0。
- OCR worker timeout/unavailable/contract mismatch/非法图片/deadline exceeded 均显式失败，Dispatcher 不加载 native OCR、不调用 callback。
- 用至少两个受控测试账号分别执行 immediate、实测本地 OCR p95、实测 OCR+模型 tail 三个等待档位的真实正确 callback；每个档位都记录页面可见、callback RPC、callback accepted 和回执完成。只采用两个账号都 accepted 的最慢档位作为 `verified_callback_acceptance_seconds` 证据下界，再扣除 callback/headroom；任一账号失败则使用更短已验证档位，不继续向线上扩大等待。
- 注入 RapidOCR 18–20 秒、模型 30 秒 timeout/二次请求、网络抖动，证明模型会在反推 hedge 点启动或因预算不足停止，且旧 challenge 不被点击。
- Dispatcher draining 后没有新 claim；所有 future 和 ownership 归零才退出，新实例 shard 身份与 heartbeat 正确。
- DB/Redis/OCR 服务短暂不可用不能被记录为 solved、healthy 或最终验证码失败。
- P1 验证重复 POST 返回同一 request、running GET 不触发重发、worker generation 变化返回 unknown、同 message/fingerprint 才允许重新发起；不建立持久队列。

### 11.3 内存 soak

- 使用经过脱敏批准的测试图集连续完成不少于事故前已观测的 1287 次 challenge，并额外覆盖到 1500 次；输入包含 hedge 前本地同票、本地分歧、deadline hedge、无安全票、模型 timeout/late result 和重复 request_id。
- 记录 RSS、cgroup memory、native mapping、请求量、回收点和回收后基线；趋势必须形成平台或按阈值优雅回收，不得跨多轮持续单调上升直到 hard limit。
- P1 验收要求 Dispatcher 全程不加载 RapidOCR/ddddOCR native runtime；OCR worker 回收后内存回到经 canary 确认的基线区间。
- hard limit 不能先于 soft recycle 触发；不得出现 host OOM、container OOMKill、强制 kill 或未收口 Gateway。

### 11.4 产品验收

- WorkerHeartbeat、结构化日志、容器指标和 Action/result 能解释为什么回收、卡在哪里、何时安全退出；本轮不以新运营页面作为验收项。
- 搜索点击 2/3 共识、候选约束、同 fingerprint 单击和远端 solved 判据没有放宽。
- AI、搜索与其他任务的 Claim Window 公平份额、日目标、账号安全门和 unknown 规则无变化。
- OCR/模型调用不计入 click 配额或额外 Dispatcher/Gateway 份额。
- 文档、数据流索引、结构索引和生产运行说明与最终实现同步。

### 11.5 Release Gate

- `release_mode`: `github_actions`
- backend 完整测试、PostgreSQL/Redis 集成测试、镜像构建与 P0/P1 路径回滚演练必须通过；本轮无新业务 migration。
- 必须用受控测试账号完成多个等待档位的真实正确 callback canary，固化 `verified_callback_acceptance_seconds`、`callback_submit_headroom_seconds`、`model_tail_budget_seconds` 及证据版本；只有页面仍可见或 OCR/model benchmark 通过不能放行新 contract。
- P0 必须证明两个 Dispatcher 的固定 OCR 槽、模型 active registry、hard memory limit、stop grace 与单 shard 自动回收；P1 必须证明 OCR 私网、deterministic request 状态、generation unknown、无本地 fallback 与独立 memory limit。
- 发布必须走 `master -> release -> GitHub Actions Deploy Production`；不得在线上手改代码。
- 回滚 owner、预算计算、观察窗口、生产探针和异常停止条件必须在发布前填写。
- Stage A 与 Stage B 独立过 Gate；Stage A 通过不代表 P1 根治完成。

### 11.6 E4 / `production_fixed`

以下条件全部满足，且时间取“至少一个完整自然日”与“真实图片验证累计达到事故样本量 1287 次”两者较晚者：

- kernel 无新增 global OOM，相关容器无 OOMKill/强杀；宿主 memory/swap/iowait 处于已批准预算内。
- 至少观察 3 次阈值触发的完整 `recycle_requested -> draining -> safe_to_exit -> restarted` 周期，两个 shard 从未同时 draining。
- 每次回收前 owned Action/open Gateway/future 均事实归零；没有新增重复 callback、结果覆盖或 reservation/claim 泄漏。
- Dispatcher RSS 不随 OCR 请求量持续单调增长；P1 中 Dispatcher 未加载 native OCR，OCR worker 内存可通过自身回收释放。
- 当日搜索点击仍按主 PRD §2.19.6 的完整 ledger 公式达标；worker/API healthy、验证码 solved 数或 Action 数均不能替代业务验收。

不满足上述任一条件时只能写 `unproven` 或 `production_failed`，不能因重启后暂时低 RSS 写 `production_fixed`。

## 12. Product Design Complete

- `route`: `product -> dev`；`level`: `L3`；`design_status`: `complete`
- `evidence_level`: `E4 incident / E2 local automated implementation proof`
- `next_agent`: `qa/product/prod-diagnosis`；`handoff_delivery_status`: `dev_implemented_qa_targeted_pass`

### 12.1 原始需求覆盖矩阵

| user_requirement | product_decision | functional_design | frontend_design | backend_design | dataflow_design | qa_acceptance | status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 按代码优化 | deadline-aware 有界 OCR，删除每题三路并发/假取消 | §4 | 本轮不新增前端 | 每引擎固定槽、单模型、统一 remaining budget | §7.1 | §11.1-11.3 | covered |
| 验证码等待不能超时 | 真实 callback canary 校准；OCR/模型/retry 共用 deadline，过期禁止点击 | §4 | 本轮不新增前端 | bounded slot wait、模型 timeout 不重置、提交前 fingerprint 复核 | §7 | §11.1-11.5 | covered |
| Dispatcher 使用后释放 | 阈值/SIGTERM 触发、业务安全点退出，不逐 Action/定时硬重启 | §5 | 本轮不新增前端 | stop event、active model registry、最小 rolling-recycle lease | §5、§8 | §11.1-11.4 | covered |
| 根治内存问题 | native OCR 独立单 worker 与容器预算 | §6 | 本轮不新增前端 | 私网 OCR service、deterministic request 状态、移除 Dispatcher native OCR | §6-9 | §11.2-11.6 | covered |
| 控制复杂度 | 不新增 session fence、持久队列、HA、新业务 migration 或运营后台 | §2.3 | not_applicable | 复用既有 Action/账号/callback 边界 | §7 | 全文审查 | covered |

### 12.2 完整性检查

- `scope/entrypoints`: 已覆盖代码止血、回收、根治；入口为 Dispatcher challenge、worker loop、OCR POST/GET、SIGTERM/自动 recycle。
- `happy/alternate/error`: 已覆盖本地共识/单模型 2/3、deadline 未校准/已过、busy/late/unknown/generation changed、新 fingerprint、recycle/recovery；§6.3 无 silent fallback。
- `permissions/frontend`: P1 仅内部服务身份；`frontend_states=not_applicable`，无新用户权限、页面或操作入口。
- `backend/idempotency`: 固定槽、统一 deadline、SIGTERM、deterministic request ID、generation unknown；复用 Action lease/账号 inflight/fingerprint/callback CAS，rolling lease 只用于回收。
- `data/security/release`: 现有 result 审计、unknown 防重、同 fingerprint 重读、私网最小权限；不持久化图片/Prompt/session，无业务 migration，Stage A/B 和回滚已定义。
- `qa_evidence`: unit/integration/soak/E4 与业务 ledger 双门；`dataflow_index_update=updated`。
- `open_questions`: `none`；`missing_inputs`: `none`；`dev_implementation`: `implemented_unreleased`。

## 13. Product Handoff 与实现状态

- `message_id`: `handoff-2026-07-31-dispatcher-ocr-memory-dev-001`
- `route/type`: `product -> dev`；`implement / incident / L3 / P0`
- `evidence/status`: `E4 incident + E2 local automated`；`post_review_findings_fixed / qa_targeted_pass`；`release_gate=blocked_by_container_and_live_evidence`
- `handoff`: `required=true / delivery_status=qa_targeted_pass / quality=targeted_and_broad_no_postgres_pass`
- `locked_paths`: 当前实现仅修改本专项列明的 backend、测试、Compose、部署脚本和索引/运行文档；未覆盖无关工作树变更。
- `merge_owner`: 当前工作树 owner；尚未 commit、push 或发布。
- `depends_on`: Stage B 依赖 Stage A QA 通过和安全 rollback floor 可用。

### 13.1 开发范围

- P0：复用现有 search_join audit，增加固定 OCR 槽、统一 deadline/模型 budget、active model registry、SIGTERM drain、最小 rolling-recycle lease、容器预算和测试；无新表、无前端。
- P1：单实例 image-verification-worker、私网 POST/GET、deterministic request ID、内存 TTL 状态、generation unknown、同题身份重读、容器预算、Dispatcher 切换和删除 native OCR 本地生产入口；无持久队列/HA。
- 同步最终代码入口到结构索引，最终运行参数和操作步骤到 `PRODUCTION_RUNTIME.md`。

### 13.2 必须遵守

- 不直接在线上补代码；发布走 GitHub Actions。
- 不增加 hard restart、silent fallback、mock success 或单票点击。
- 不改变搜索/AI 份额、任务目标、账号安全或 unknown 防重。
- 每阶段先复现/测试，再实现；L3 必须回到 prod-diagnosis 做 E4 复核。

### 13.3 完成回传

- commit/PR/Actions、完整测试、soak 曲线与容器预算；明确本轮无业务 migration。
- 固定槽/remaining budget/SIGTERM/request status 故障注入结果、既有锁序未改变和重复 callback 为 0 的证据。
- Stage A/Stage B 各自 Release Gate、回滚演练和生产验证报告。

### 13.4 2026-08-01 实现后审查修复闸门

- P0：验证码 callback RPC 发出后页面回执超时/断连必须进入 `verification_callback_result_unknown`，Action/Attempt/纯点击 obligation 保持 unknown 占位；同账号、机器人和 challenge fingerprint 未闭环前，跨 Action callback 次数必须仍为 1。
- P1：Worker 的单请求 admission 必须覆盖图片 Base64 解码/Pillow 校验/native OCR 全阶段；cheap contract/deadline 校验在占位前完成，busy 请求不得进入图片解码。
- P1：Dispatcher 只接受 request ID、contract version、worker generation 和两路唯一受控 source 完整匹配的 remote completed 响应；畸形 JSON、字段类型、空/重复/未知 source 全部显式 `verification_contract_mismatch|verification_local_ocr_unavailable`，不得进入通用 search failure 或 deadline 忙循环。
- P1：实际 callback 调用前执行最后一次 monotonic deadline check；`safe_to_exit` heartbeat 持久化失败必须保持 drain blocked；successor lease ack 在可观测 TTL 窗内重试。
- P1：OCR Worker 启动不得执行 `app.services.__init__` 聚合导入；生产 Dispatcher 镜像不安装 native OCR 依赖，OCR Worker 使用独立派生镜像。`/health` 只作 liveness，发布闸门必须另跑受认证 functional readiness，确认两个本地引擎可初始化并完成最小探针。
- Release Gate：shell 预检必须拒绝零/负数、callback/headroom/model tail 相对关系错误、terminal TTL 小于 max budget + recovery window，以及无法解析的 memory/stop grace；完整 PostgreSQL/Redis、镜像构建、functional readiness、callback canary、soak 和 E4 仍分别验收。

本 handoff 的实现后审查项已修复并恢复 `qa_targeted_pass`：合并定向回归 `273 passed`，宽范围 no-postgres 共有 2395 项首轮通过，20 项环境/顺序失败均隔离复跑通过；本地 RapidOCR/ddddOCR functional init 通过。完整 PostgreSQL/Redis 集成、双镜像构建、non-root/read-only 容器 readiness、soak、Actions、发布与生产 E4 仍为 Release Gate，不能写 `production_fixed`。

# 硅谷生产稳定性与履约修复 PRD

## 1. 文档状态

| 项目 | 内容 |
|---|---|
| Intake ID | `intake-2026-08-15-production-stability-remediation-001` |
| 问题级别 | L3 / P0：宿主内存临界无 swap、AI 活群与纯搜索履约未达目标、点赞链路半瘫、TG 登录代码缺陷、Worker 零日志 |
| 设计状态 | `product_design_complete / resynced_2026-08-15` |
| 适用范围 | 硅谷生产（47.77.184.233）宿主机、全部应用容器、planner/dispatcher/generation/OCR/登录链路 |
| 明确排除 | API 安全层（webhook secret、/media、审计手机号等）另行立项；本产品单租户运行，多租户隔离类问题不进入本次范围 |
| 关联文档 | [task-fulfillment-classified-recovery-prd.md](task-fulfillment-classified-recovery-prd.md)（评论/点赞来源义务与 `due_by_now`）、[ai-group-generation-failure-churn-remediation-prd.md](ai-group-generation-failure-churn-remediation-prd.md)（current AI 唯一合同）、[channel-view-planner-starvation-remediation-prd.md](channel-view-planner-starvation-remediation-prd.md)（current view 唯一合同）、[dispatcher-ocr-memory-isolation-and-graceful-recycle-prd.md](dispatcher-ocr-memory-isolation-and-graceful-recycle-prd.md)（OCR 隔离、takeover owner 与回收）、[account-login-group-navigation-recovery-prd.md](account-login-group-navigation-recovery-prd.md)（登录 flow 合同）、[ai-group-provider-fallback-and-safe-prompt-design.md](ai-group-provider-fallback-and-safe-prompt-design.md)（Provider adapter，运行时为单 active key） |
| 证据状态 | `production_readonly_verified`：资源、数据库、容器日志、当前发布代码已核对；OCR 双引擎当前 readiness 因 SSH banner timeout 为 `blocked`；历史 TypeError 在当前发布后的复现为 `unproven` |
| 本次目标 | 恢复宿主内存安全裕量、解除 AI/搜索吞吐瓶颈、恢复点赞与浏览执行链路、收口登录存量 flow、修复 Telethon 客户端失效与 Worker 可观测性、补齐发布 E4 闸门；不改变 fact_first_v3 履约合同、typed remote fact 语义与唯一 active Provider 运行时约束 |

### 1.1 当前合同与变更边界

1. 本文是生产事故修复的编排与 Release Gate，不复制第二套履约真相源。冲突时按下列唯一 owner 执行：
   - AI 数量、义务、GenerationJob、variation、Action writer、duplicate/reopen 与 fleet takeover 只认 `ai-group-generation-failure-churn-remediation-prd.md`；正常正文在 accepted variation 与 message memory ready 前 `Action=0`。
   - `channel_view` 的 message target、due ordinal、binding、route/lifecycle epoch、migration 与 settlement 只认 `channel-view-planner-starvation-remediation-prd.md`。
   - `channel_comment/channel_like` 的 source-scoped ordinal、滚动 24 小时 pacing、`due_by_now` 与 recovery 只认 `task-fulfillment-classified-recovery-prd.md` §4.5/§11.2。
   - 全量 takeover 的唯一自动 owner 是 `deploy/compose-up.sh` Stage B（`scripts.takeover_all_task_fulfillment` preview/apply 调用块）；release 返回后只允许有界只读 `verify-active`，不得设计第二个 takeover 调用者。
2. 本文只增加 incident-scoped 的 Provider admission、查询/轮询治理、资源与日志、受控存量修复、登录/Telethon/Gateway 安全修复和 L3 E4 编排；不得借事故快修恢复 legacy Action-first AI、账号天然键 view 或 future-tail source 排期。
3. 实现发现上游专项尚未部署时，相关 release train 标记 `blocked_by_authoritative_contract`；不得以兼容分支绕过 migration/fleet gate。任何状态名、唯一键、writer owner 或迁移顺序与上述专项不一致时，开发必须停止并触发 product `resync`。

## 2. 生产事实基线（2026-08-15 12:25～12:30 北京时间复核）

本节所有数量均为时点快照，不得作为长期常量。证据分为：`observed`（本轮直接读到）、`inference`（多项证据支持但缺少原始明细）、`blocked`（访问边界阻断）和 `unproven`（现有证据不足）。

部署事实：

- 宿主机 4 CPU、7.3GiB 内存，used 6999MiB / available **218MiB**，**无 swap**，load `3.71/3.79/3.64`；当前 boot 未检出 OOM victim，但不能据此降低下一次尖峰风险。
- 61 个 mihomo 当前合计约 858MiB（另有 clash-ss）；tg-v-chat / tg-reporter 等旁路应用继续与主平台争用同一宿主资源。
- 当前发布 SHA `b517e1cf`，生产 symlink 指向 `20260815021109_b517e1cf`；Deploy Production 成功，但其中 `Probe production planner drain` 与 `Verify incident task fulfillment E4 facts` 均被跳过，发布成功不构成履约恢复证明。
- PolarDB（108/500 连接）、Redis 有密码；数据库侧无瓶颈证据。
- Provider 表只有 MiniMax-M3 active，但其 health check 停留在 2026-07-30；配置 active 不等于当前可用。`resolve_tenant_id()` 固定返回 1 在已确认单租户合同下不构成本次缺陷。

资源与队列事实（`observed`）：

| 消耗者 | 当前值 | 结论 |
|---|---:|---|
| worker-planner | 896.3MiB / 80.77% CPU，无 memory/CPU limit | 当前最大单进程内存与持续 CPU 消耗者 |
| image-verification-worker | 497.3MiB / 768MiB，173.11% CPU，RestartCount=37 | 高 native OCR 常驻内存并频繁回收；仅凭此不能定性为内存泄漏 |
| dispatcher-1/2 | 325.4MiB / 331.5MiB，各限 512MiB | 均已超过上限 63%，仍有冲顶风险 |
| 61 个 mihomo | 合计 858.4MiB，均值 14.1MiB | 数量型常驻开销显著 |
| 核心应用容器 | planner、AI generation、dispatcher 等默认 root；除 dispatcher/OCR 外无 memory limit | 故障隔离不足；OCR 例外为 UID/GID 65532 且限 768MiB |

- 数据库 `actions=182799`、`group_context_messages=1203828`；当前 live Task 19 个，其中 running 且 due 15 个。
- future pending 共 **2757**：`like_message=2384`（最远 2026-12-31 06:13）、`post_comment=373`（最远 2026-09-01）。当前 due pending 的 like_message 为 0，但这只说明它们被排到未来，不是吞吐恢复。

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

## 3. 根因分组与修复规则

### RC-1：Planner 超前物化 + Dispatcher claim N+1（内存/CPU 主因）

现象：channel_like 一轮构建全部消息×账号缺口；current contract 使用 `deadline_at=None`，随后以现有最远 open Action 为锚继续串行排期，形成 2384 条跨月至 12 月的 future pending。Planner 每 2 秒重复 drain，current-contract 又直接绕过 backlog gate。另一个独立问题位于 Dispatcher：`_due_claim_task_ids()` 无 SQL limit，随后每个 task 各执行 strict/non-strict 两条查询。Planner 的普通 task cursor 虽无 SQL limit，但应用层会在 limit 处 break，不能与 Dispatcher 的无界扫描混为一谈。

规则：

1. **JIT 物化，不设 future Action 窗口**：`channel_like/channel_comment` 只为按来源消息滚动 24 小时 pacing 已进入 `due_by_now` 的 source ordinal 建立当前物化；未到期 ordinal 留在任务专用义务账本，不提前创建 3 天/7 天 Action。已到期 Action 可因合法拟人间隔得到晚于 `now` 的 `scheduled_at`，但不得晚于该来源 `deadline_at`，也不得沿 Task 历史最远 future Action 跨来源、跨窗口平移。
2. **来源义务与迁移**：current source owner 使用 `(task_id, source_message_id, source_revision, obligation_kind, ordinal)`；`pacing_anchor_at/due_at/deadline_at/materialization_version/lifecycle_epoch/current_action_id` 必须按 classified-recovery 合同持久化并可审计。现有仅按 task/message/account/version 的 reaction 行属于 legacy shape；Source train 必须先做 additive migration、backfill/classification 与唯一约束，再启用 JIT writer。账号是 ordinal 的可审计执行绑定和 remote-fact 维度，不得替代 source ordinal 分母。
3. **backlog gate 与唤醒**：gate 分列 `due_open/due_materialized/not_yet_due/gateway_hold/unknown/confirmed/terminal_shortfall`；只有 `due_open` 与当前真实 interaction slot 参与本轮物化。`not_yet_due` 不算 backlog，保存下一 `due_at` 并按事件/最早 due 唤醒；无 due 时不得固定 2 秒空转。
4. **存量回收顺序**：先部署 migration 与新 writer fence，再对 future pending 做 preview。`success/remote fact` 保留并绑定；`claiming/executing/Gateway-started/unknown` 保持原 owner 和 reconcile；只有 `pending`、无 claim/lease/Gateway start、且不属于当前 `due_by_now` 的固定集合可 CAS 终结为 `retired_pre_gateway_future_materialization`，同时释放旧 `current_action_id`。仍在有效窗口的 ordinal 回到 `unmaterialized/open` 等待其真实 `due_at`；已过 deadline 的 ordinal由唯一 settlement owner写 shortfall，禁止 Planner 立即重建。
5. **查询有界化**：Planner task 查询把应用端 break 下推为 SQL limit；Dispatcher 以持久公平游标/分窗一次取回 task+候选，消除“无 limit task 列表 + 每 task 两查询”；单轮查询数必须为固定上界，不能随 due task 数形成 2N。
6. **轮询与观测**：Planner 无事件且无到期义务时默认 10 秒兜底轮询；有最近 `due_at` 时睡眠到该时点或事件唤醒。调大间隔不得掩盖查询/物化缺陷；按 role 输出 drain 数量/耗时、SQL 数、`due_open/not_yet_due/materialized`、RSS、CPU 和本轮最大 `scheduled_at/deadline_at`。

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

现象：1803 个 search Action 失败、756 个产生 `target_click_observed`；1798 个 Attempt 首先停在图片验证 required 边界，但仅 323 个 Action 可直接证明 local OCR timeout。OCR 容器高 CPU、高驻留内存且频繁 recycle；当前双引擎 readiness 又因 SSH banner timeout 未能复核。同时 search-dispatcher 有 655 次连接被服务端关闭。

规则：

1. 先按 Action/Attempt 统一分类 `verification_required -> local_timeout / engine_error / worker_unavailable / transport / other`，禁止再把外层 required 等同于 100% local timeout。
2. RC-1/RC-7 释放宿主 CPU/内存后，使用 token-authenticated `/internal/v1/image-verification/ready` 验证 RapidOCR+ddddOCR；`/health` 或 Docker healthy 仅代表 liveness。
3. OCR 隔离与回收继续遵循关联 PRD；RestartCount 必须按 `recycle_requested / deadline_drain / healthcheck / OOM / external_signal` 记录原因，不能把主动回收误报为泄漏或 OOM。
4. Dispatcher 等待 deadline 必须依据生产 P99 配置，并保留“POST 结果未知时禁止重复 POST”的安全语义；超时后可继续查询同一 request，不得新建重复识别请求。
5. search E4 只认 `target_click_observed=true`；OCR 完成、代理连通、Action success 均不是点击完成证明。

### RC-4：channel_like 无 deadline 批量排期导致当前履约被推向未来

现象：近 2 小时只有 11 个 Reaction E4；当前 due pending=0，但 future pending 有 2384 条。代码已经给出直接机制：一次构造全部 deficit，`coverage_remaining` 只递减、不限制 quantity，`deadline_at=None`，再沿最远 open Action 串行续排。因此“当前无 due”是超前排期结果，不是目标已完成。

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

1. **容量公式与 non-platform 集合**：以发布前 readback 的物理内存 `H` 为准；当前 7.3GiB 主机必须满足 `platform_normal_p99 <= H - 1.5GiB(MemAvailable目标) - 0.5GiB(host/system reserve) - max(1.5GiB, non_platform_30m_p99*1.15)`。`non_platform` 固定为平台 release compose（`docker-compose.server.yml`）之外的常驻容器：61×mihomo、`tgyunying-clash-ss`、`app-infra-sing-box`、`app-infra-redis`、`tg-v-chat-{bot,worker,listener}`、`tg-reporter-app`；宿主内核/docker daemon/nginx 归入 0.5GiB host reserve，不重复计入。2026-08-15 实测 non_platform ≈1376MiB（×1.15≈1.55GiB），公式允许平台预算 ≈3.75GiB（≈3845MiB）；首轮 committed 预算统一取下表 **3760MiB**，正文/QA/E4 均以 3760MiB 为唯一口径。达不到时本 train 阻塞，选择迁移旁路应用/无绑定 mihomo 或扩容，不得靠 swap、降低日志或压低 OOM 阈值强行放行。
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
3. **swap 操作合同**：P0 固定增加 4GiB `/swapfile`，执行前要求根盘可用空间至少 12GiB、inode/磁盘 I/O 无告警且当前无 OOM storm；文件权限 `0600`，完成 `mkswap/swapon` 后写入 `/etc/fstab`，设置并 readback `vm.swappiness=10`。验收必须保存 `swapon --show --bytes`、`free -b`、`sysctl vm.swappiness` 和重启后持久化证据。持续 15 分钟 swap 使用超过 512MiB或出现持续 swap-in/out 即标记 `resource_capacity_degraded`；只有业务压力解除且 swap 使用归零后才允许按审批回滚，运行中不得直接 `swapoff` 制造内存尖峰。
4. **逐服务启用**：资源限制在独立 Resource train 中按 `planner -> ai-generation单实例 -> 其余generation -> 小worker -> backend/search/listener/account-online` 滚动，每一步至少观察 30 分钟并验证 claim/lease/E4 无回退；dispatcher/OCR 只复核现有限额，不与代码 train 同窗改值。
5. 盘点 61 个 mihomo 的活跃账号出口绑定；未绑定实例只能按精确实例清单、旧值/hash、actor/approval 和连接 readback 下线。是否合并实例属于独立容量设计，不进入本 PRD。
6. 核心应用从 root 迁移到固定非 root UID/GID；OCR 已满足。权限、volume 读写、临时目录、healthcheck 与回滚需逐服务验证，作为独立 P1 Hardening train，不与 P0 swap、Planner 或 Provider 发布同窗。

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

- 不新增前端页面与按钮；任务详情沿用现有 due/materialized/confirmed/blocker 展示，新增 `provider_capacity_shortfall`、`provider_admission_unavailable`、`image_verification_degraded`、`target_entity_unresolved`、`blocked_by_authoritative_contract`、`unsafe_retry_rejected`、`resource_capacity_degraded`、`production_e4_blocked` 等既有 blocker 通道取值。
- blocker 必须显示当前任务/目标/epoch 的新鲜事实，不得把历史错误、容器 healthy 或 deploy success 投影为当前履约状态。
- source pacing、DueSet 和 final duplicate gate 是产品合同，不开放配置；planner 兜底间隔、OCR 等待 deadline、Provider cooldown 上限和日志级别为后端配置，不开放用户自助修改。
- 受控生产操作包括 future Action 回收、僵尸 flow supersede、channel_view target rebind 和无绑定 mihomo 下线。全部走独立 CLI/Workflow，参数必须含 exact 集合/目标、expected deployed SHA、当前 contract/route/lifecycle epoch、old-value/config hash、preview hash、actor 与 approval ref；普通任务 API 无权触发，preview 与 apply 之间发生任一漂移即整批停止。

## 5. 后端与 Worker 交接

1. `services/task_center/service.py`、`dispatcher.py`、`executors/channel_like.py`、`executors/channel_comment*.py`（targets/preparation/schedule/budget）、source obligation/settlement 与 migration：`due_by_now` JIT、source ordinal/deadline、backlog/wake、coverage 仅作账号优先级、Dispatcher 有界查询与公平游标（RC-1/RC-4）。
2. `backend/scripts/` + 独立生产 workflow：legacy reaction/source backfill classification、future Action preview/apply/readback；脚本必须按 RC-1 状态分类并复用权威 obligation/remote-fact 终结合同。
3. `services/task_center/ai_generation_worker.py`、Provider adapter/gateway：共享 Provider admission/cooldown、claim/pre-call 双重 fence、429/Retry-After、已 claim job 释放和有界 duplicate memory（RC-2）。不得修改 AI 数量 owner 或新增 normal 空正文 Action；Provider 切换/升配进入运维 runbook。
4. `image_verification_worker*.py`、`services/image_verification_client.py`、search dispatcher：OCR readiness、request phase、重启原因与验证码失败内层分类（RC-3）。
5. 登录服务仅补当前 guard 的回归与 typed error/日志，不重复实现已有 None 防御；新增 latest legacy waiting-flow 存量脚本（RC-5）。
6. `worker.py`、compose、`telethon_lifecycle.py`：统一日志初始化、日志轮转、metadata-aware invalidation、heartbeat freshness；资源预算/readback 和非 root 迁移分别进入 Resource/Hardening train（RC-6/RC-7）。
7. channel_view/资源同步链路只实现精确预检和受控 rebinding workflow；migration、binding、epoch 与 writer 必须复用 view 权威专项，不在本 PRD 另建兼容路径（RC-8）。
8. Deploy Production workflow：消费 `incident_e4_manifest`，强制执行该 train 的 planner/resource/typed E4；删除固定历史 Task ID和 release live 后的任何 all-task takeover 调用（RC-9）。
9. `integrations/telegram/gateway.py::_send_async` 与 `service.py::retry_task` 按 RC-10 的 mutation/retry 闭集先写红测再修复；不得把它们作为无独立验收的顺手改动。
10. `docs/04-ops/deployment/PRODUCTION_RUNTIME.md`/runbook 记录容量公式、swap、逐服务 budget/readback、回滚与告警；代码入口、worker 拓扑、脚本、workflow 变化同步结构索引，obligation/Action/flow/remote fact 变化同步数据流转索引。

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
- swap、代码、数据操作、mem_limit 与 non-root 分 train；任何服务因 limit 退出必须显式留痕，不能以 `restart: unless-stopped` 覆盖未完成工作。正常 P99 超预算时阻塞并调整容量，不能依赖 swap 承载稳态。
- 回滚按 train：additive migration 可保留但旧 writer 不得复活；future Action/flow/target apply 不做逆向数据改写；Provider admission 回滚不得恢复无共享限流的多进程猛打；swap 在仍有压力或已使用时不得移除；其余无数据 mutation 的代码 train 才允许回到上一稳定 SHA。
- 不允许 silent fallback：OCR、Provider shared state、日志初始化或实体解析失败时都必须暴露 typed blocker，不得返回 mock success、模板假消息或跳过 E4。

## 7. QA 验收

### RC-1/RC-4 Planner、Dispatcher 与点赞

1. `pacing_anchor` 时 due=0；晚采集的 50 点赞/80 评论来源只按完整滚动 24 小时曲线物化当前 `due_by_now`，未到期 ordinal 的 Action 数为 0，不能生成 3/7 天 future Action或沿历史尾部跨窗口平移。
2. source migration/backfill 在 PostgreSQL 验证 current key、due/deadline/materialization/lifecycle 字段；同一 source ordinal 并发最多一个 current Action，无法唯一映射的 legacy 行进入 blocker，不猜测绑定。
3. 多条消息共用同一账号池时，每条消息分别达到自身 DueSet；`coverage_remaining=0` 不得阻止其他消息的独立 ordinal，且同一 message/account remote identity 不重复。
4. Dispatcher due task/candidate 查询数不产生 2N；SQL 自身含 limit/持久游标，多 Task 公平轮转，空闲 Planner 无 due 时不固定 2 秒查询。
5. 回收脚本按 `fact/success`、`claiming/executing/Gateway/unknown`、安全 future pending 三类输出；preview hash 对状态/版本/义务指针敏感，apply 仅终结第三类并释放指针，重复 apply 幂等且旧 writer 已 fenced。

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

### RC-5 登录

14. session/hash 缺失返回 typed `login_flow_not_resumable`，无 TypeError/500；异常日志有堆栈且敏感字段掩码。
15. preview 区分历史 134 shape 与当前 latest-flow 业务候选；apply 只 supersede preview 固定集合，账号回到可重登入口，不自动发验证码，AuditLog 完整且重复 apply 幂等。

### RC-6/RC-7 日志、Telethon 与资源

16. 全部 role 的 Docker 日志可见 INFO drain/耗时与 ERROR trace；脱敏扫描无 session、密码、验证码、2FA、token、手机号和消息正文。
17. 用非空 client metadata 创建真实缓存 entry 后，invalidate 必须删除并断开同一 entry；空/不同 metadata 不得误删其他 client。
18. 容量 artifact 使用本 PRD公式计算且 normal P99 合计不超过 3760MiB；4GiB swap 的磁盘 preflight、权限、swappiness、fstab、重启 readback 和 degraded 告警均通过。
19. Resource train 每次只滚动一个服务/分组；compose 合并配置断言 mem/pids/logging，触限时退出原因、owner/lease 与未完成工作可观测，30 分钟内无 restart loop和 typed E4 回退。
20. Hardening train 逐服务验证固定非 root UID/GID、volume/临时目录权限、healthcheck 和 rollback；单服务失败只回滚该服务且不改变 T7 已确认的资源预算。

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
- O0 使用独立 ops artifact；每个代码 train 使用独立 SHA、migration/config hash、canary、rollback decision 和 `incident_e4_manifest`。前一 train 的 `release_passed`/E4 不能替代后一 train，禁止把下表合并为一次全量滚动：

  | 顺序 | Release train | 范围 | 放行条件 | 回滚/停止线 |
  |---|---|---|---|---|
  | O0 | Emergency capacity baseline | 4GiB swap、磁盘/readback、现状 RSS/CPU/进程/日志 artifact | 不重启业务容器；swap 持久化 readback | preflight 失败不创建；已使用 swap 时不 `swapoff` |
  | T1 | Observability & outbound safety | Worker 日志、heartbeat freshness、Telethon invalidation、RC-10 Gateway/retry | 全 role 日志/脱敏、retry 红测、无业务数据 migration | 无远端 mutation/data apply时可回上一 SHA；出现 unknown 不自动重试 |
  | T2 | Source JIT & query pressure | source additive migration、Planner/Dispatcher 有界查询、like/comment JIT、future Action 受控回收 | migration/backfill sealed、新 writer fenced、单 Task canary后才 apply cleanup | apply 后禁止旧 writer 复活；冲突时停在新 release 前向修复 |
  | T3 | Provider admission | claim/pre-call cooldown、429 分类、已 claim job 释放、有界 dedupe cache | current AI route/authority `verify-active`、单 Provider 小流量、Action=0 invariant | 不得回到无共享 admission 的多进程并发；保持 fail-closed |
  | T4 | Search/OCR reliability | OCR 内层分类、functional readiness、同 request 查询、代理/Telethon指标 | 双引擎 `/ready`、受控账号 callback/search E4 | 保留同 request/unknown，禁止重复 POST/click |
  | T5 | Login data repair | legacy latest waiting-flow preview/supersede | exact preview hash与逐项 approval | 数据不逆向改写；失败项保持 blocker，历史 flow 保留 |
  | T6 | View target repair | 精确 target preflight/rebind/readback | view migration/route 已 verify-active；独立 preview/approval | 失败保持 `target_entity_unresolved`；旧 fact/Action 保留 |
  | T7 | Resource isolation | 按预算逐服务 mem/pids | 前序 train 形成 startup/P99 数据；每步30分钟 soak与 E4 | 任一触限/restart/E4回退立即停止后续服务，不批量抬限 |
  | T8 | Non-root hardening | 逐服务固定 UID/GID、volume/临时目录/healthcheck | T7 稳定且单服务权限矩阵通过 | 只回滚受影响服务，不改变已确认资源预算 |

- L3 workflow 必须消费该 train manifest并实际运行对应 planner/resource/typed E4；SSH/数据库/Telegram 证据不可得时标记 `blocked/production_unproven`。release live 后不得再次运行 all-task takeover。
- E4 分项证据（缺一不得写 `production_fixed`）：
  - RC-1/RC-4：连续 2 个完整来源滚动窗口无 not-yet-due/deadline 外新 Action；按 `DueSet = confirmed fact + open/pre-call + Gateway/unknown hold + terminal shortfall` 逐 source revision守恒；承诺目标的完成判定必须 `confirmed fact=DueSet` 且其余集合为0，不能把 hold/shortfall 当成功；planner CPU/RSS/SQL P99稳定且其他 lane 不回退；
  - RC-2：真实 429 可分类，claim/pre-call cooldown与已 claim job 释放生效；大目标群 immutable settlement 连续 2 日达到经配额核算可承诺目标，duplicate final gate/阈值不降低、无空正文/重复义务；
  - RC-3：验证码失败具备完整内层分类，local timeout 占 verification-required 低于 10%，search_click `target_click_observed/required` 达到任务目标；
  - RC-5：当前 latest legacy waiting-flow 候选清零且有 AuditLog，连续 7 天无新增裸 TypeError；历史 flow 行仍保留；
  - RC-6：生产真实错误能从日志还原 trace，metadata client invalidation readback 证明精确 entry 已断开；
  - RC-7：平台 normal P99 不超 3760MiB，正常峰值 `MemAvailable >= 1.5GiB` 持续 24 小时，无 OOM、无持续 swap-in/out；swap 超阈值和容器触限/重启均有明确原因与 owner readback；
  - RC-8：受控 operation 的 old/new target/config/route readback闭合，修复目标产生新 ViewRemoteFact且 task/target revision/route epoch 匹配，旧目标事实未串绑；
  - RC-9：workflow artifact 保存 manifest、expected/current SHA、migration/config hash、资源 readback与必需 E4；无固定历史 Task ID、无必需步骤 skip、无 post-release 第二次 all-task takeover；
  - RC-10：生产与 canary 均无新增 gateway `UnboundLocalError`；generic retry 对 success/unknown/Gateway-started 的重开数为 0，安全 pre-call retry 可沿原义务完成且不重复远端副作用。
- 任一 E4 未达成时保持对应 `production_regression_fix_pending` / 部分完成状态，禁止合并宣称。

## 9. Product Design Complete 自检

| 检查项 | 结论 |
|---|---|
| 用户原话覆盖 | 内存、AI 大目标、重复内容、点赞、搜索/OCR、浏览实体、登录、日志、代理/Telethon、Gateway/retry、root/限额、单租户与发布验证均映射到 RC-1～RC-10 |
| 当前合同 | AI/view/source pacing/takeover 分别引用唯一权威专项；本文不恢复 legacy AI Action-first、账号天然键 view、future-tail source 排期或第二 takeover owner |
| 前端/状态 | 无新入口；capacity/admission/OCR/entity/unsafe-retry/resource/E4 走既有 blocker 通道，不伪造完成 |
| 后端/API/worker | Planner/Dispatcher、source migration、Provider claim/pre-call cooldown、OCR、登录、日志、Telethon、Gateway/retry、compose、实体同步和 workflow 均有明确 owner |
| 数据流转 | source ordinal additive migration与受控回收有分类/唯一键/settlement；AI normal Action=0；flow supersede与view rebind均保留历史和 typed fact边界 |
| 权限安全 | Action/flow/view/mihomo 受控操作要求 exact target、old-value/hash、actor/approval/SHA/epoch、preview/apply/readback；日志脱敏为硬约束（API 安全层另行立项） |
| 边界/失败路径 | Provider shared state/重启惊群、duplicate basis、OCR unknown、实体专项未激活、Gateway partial、unsafe retry、swap/OOM、状态漂移、分 train 回滚与证据 blocked 均覆盖 |
| QA/发布/E4 | 26 项 QA + RC-1～RC-10 分项生产证据；manifest 不得为空/硬编码/skip，post-release 只读 verify，未达保持 pending/unproven |
| 迁移/回滚 | reaction/source 为 additive migration；AI/view 前置迁移沿权威专项。数据 apply 不逆向改写，旧 writer 不复活；资源、代码和数据分 train、独立停止线 |

结论：`product_design_complete / resynced_2026-08-15`。开发必须按 O0/T1～T8 独立 release train 实现和验收；任何实现期新增本文未列出的持久字段、改变 authoritative writer/唯一 active Provider/typed remote fact、扩大 retry 或受控 apply 集合、合并 release train，均触发 product `resync`，不能只改代码。

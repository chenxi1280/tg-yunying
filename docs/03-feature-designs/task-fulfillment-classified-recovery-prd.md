# 生产任务分类履约恢复 PRD（产品确认稿）

## 1. 文档状态

| 项目 | 内容 |
| --- | --- |
| Intake ID | `intake-2026-08-03-task-fulfillment-classified-recovery-001` |
| 需求级别 | L3：生产多类任务长期按时按量履约失败 |
| 适用任务 | `group_ai_chat`、`channel_comment`、`channel_like`、`channel_view`、纯 `search_click` |
| 文档状态 | `approved_product_handoff` |
| 设计状态 | `product_design_complete`；2026-08-08 生产复核补充 AI 仅引用我方消息、无法解析目标实体的准入终态、quiet-hours 后置去折叠和历史 unknown/绑定收口；闭合合同见 `task-fulfillment-contract-closure-prd.md` |
| 开发交接 | `dev_handoff_ready=true`；本文与闭合专项是本轮唯一实现合同，主 PRD与主数据流索引已同步产品合同；项目结构索引只在代码与 QA 稳定后按真实入口更新 |
| 生产状态 | `production_blocked`；部署、健康或局部远端事实不等于当日履约完成 |
| 统计时区 | 默认 `Asia/Shanghai`，以 `Task.timezone` 为准 |

本文在用户确认后，按明确章节 supersede 以下冲突口径：

- 2026-08-04 本文及主 PRD 中将 `fact_first_v3` 等同于“全部义务立即 due/资源空闲即清空”的口径；该口径仅对仍明确采用即时 solver 的任务类型有效，`group_ai_chat/channel_comment/channel_like/channel_view` 必须按 §4.5 的拟人节奏执行；

- `ai-group-daily-group-target-redesign-prd.md`、`all-task-fulfillment-recovery-prd.md`和主 PRD 中“任务日冻结账号分母、当日不可缩小、暂停/删除任务仍保留运行义务”的部分；
- `all-task-fulfillment-recovery-prd.md` 中由 ContentMix 持有数量义务、搜索与普通互动共享执行容量的部分，以及 `shared-dispatch-and-ai-fulfillment-recovery-prd.md` 的全部共享 active claim/中央 TaskAllocation/Reservation 方案；
- `dispatcher-ocr-memory-isolation-and-graceful-recycle-prd.md` 中 AI/VLM 参与验证码识别与 2/3 共识的部分；
- `ai-conversation-humanization-and-group-bot-admission-prd.md` 中与“任务配置频道前置关注”冲突的入群顺序，但保留可信来源、账号归属、可见性和 unknown 防重合同；
- `ai-group-all-accounts-daily-coverage-prd.md`、`ai-group-daily-fulfillment-remediation-prd.md` 和 `ai-group-admission-quantity-slot-convergence-prd.md` 的运行合同；三者仅保留事故、历史数据和迁移取证，不再作为当前开发或 QA 依据；
- `ai-group-dispatcher-ai-generation-transaction-design.md` 中冻结账号分母、账号级单 executing、direct 严格 context equality、ContentMix 拥有数量义务和本地缺引用即终结的部分；仅保留“外部 Provider 调用不持有数据库事务”的边界；
- `ai-group-provider-fallback-and-safe-prompt-design.md` 中验证码 AI/VLM、固定 M3/M2.5/Grok 生产拓扑、文本表情兜底和按配置记录拆分同一实际 key 额度的部分；仅保留输入安全、统一输出契约和 Provider 适配原则；
- `search-click-boost-prd.md` 的 `search_join_group`、membership/admission、创建前容量证明和点击后加入语义；纯搜索点击只认本文件、闭合专项和重写后的 `search-click-daily-fulfillment-remediation-prd.md`；
- `ai-group-send-continuity-and-terminal-targets-prd.md` 的 Phase B 硬小时、共享 Claim、群本地槽位和活动窗口部分；仅保留目标生命周期、引用版本和 unknown 安全边界；
- 其他任务安全、权限、远端事实、账号限流、跨群隔离和审计合同继续有效。

本文的状态机、fencing、搜索交接、transport、AI 有序并发和量化 E4 细节以 `task-fulfillment-contract-closure-prd.md` 为强制组成部分；两份文件必须作为同一 Product Handoff 实现和验收。

## 2. 用户原始需求与术语修订

### 2.1 用户决策

1. AI 活群任务支持最多 3 个选填“需关注频道”；配置后先关注频道，再加入目标群并发言。
2. 群机器人提示必须绑定到被拦截账号；使用该账号 Telegram 视角读取提示，分别记录关注、确认和可见性事实。
3. C3 需要比“继续修 ContentMix 槽”更清晰、可重建的方案。
4. 搜索验证码不使用 AI/VLM；依次使用两个 OCR，A 不可用或被明确判错时尝试 B，B 被明确判错后刷新 challenge。
5. AI 生成使用多个模型，应按独立生成作业并发；共享同一个 API key 不构成全局串行理由。
6. 搜索与普通 Telegram 互动必须分开处理。
7. C7 Recovery 与 C8 上游义务由本 PRD 完整设计。
8. AI 活群不冻结不可变的账号分母；每个任务、每个目标群独立按当日实时资格维护账号范围。
9. 账号可自动恢复时回流原任务；有权威事实证明当日不可恢复时放弃该任务，不让历史范围永久残留。
10. 暂停、停止或删除任务立即退出活跃调度和容量统计，清理未进 Gateway 的当前/未来运行义务。
11. 多个运行任务是独立并发义务；不允许一个任务先排空队列后才执行下一个。
12. 同一账号在不同任务中也并行处理；不设“任务抢账号”或账号内任务公平 cursor，只对同一远程副作用身份做幂等和冲突串行。
13. 搜索使用事件驱动连续 solver；开放点击义务与可执行路径同时存在时立即建立 assignment 并执行，不存在搜索 Window、跨 Window 预扣或二次容量分配。
14. 账号绑定采用“展示名精确匹配 + 同一可信群管提示中的群聊要求链接/按钮”组合证据；可信提示可包含任意多个独立要求点击，不限制为 0–1 个。
15. 业务切换先由运营直接创建 prepared 新 Task ID；唯一 route epoch CAS 让新 Task 从 0 运行并同时 fence 旧 Task，随后异步物理删除旧 Task 主记录与可重建运行数据；仅在独立 tombstone 表保留防重与 Gateway 对账身份。
16. C8 远程引用探针从当前可访问候选账号中随机选取；确定性账号不可用时仅从本次候选池 `-1` 后继续，不冻结全局账号。
17. 所有旧合同 Task（包括 running）不迁移运行状态；发布切换时统一 fence、保存最小远端防重事实、物理删除，再由运营按新合同创建新 Task。
18. 同日删除重建的新 Task 从 `0` 建立自己的目标和完成量；旧 Task 当日成功不抵扣新 Task，创建确认必须明确提示可能增加当日实际执行量。
19. `fact_first_v3` 不创建任务份额或中央 Reservation，但不得覆盖任务类型自己的执行时机；AI 活群、频道评论、频道点赞和频道浏览按 §4.5 的 `natural_full_day` 计算 `due_by_now`，只执行当前累计到期义务。
20. AI Provider 只允许一个 active Provider key；多个模型共享该 key 的同一真实额度，可并发但不得各自复制整套额度。
21. 一个搜索 Task 只允许一个目标；多目标必须创建多个独立 Task 并行执行。
22. `签到` 不进入普通正文 10 天去重，可计群日总量并可同时完成对应账号 coverage，但不得替代 reply 或高质量 AI 正文指标。
23. ContentMix 投影、义务与 Action 的幂等身份必须有数据库唯一约束，不能只靠应用层查询防重。
24. AI 活群 Telegram 原生引用只允许同 tenant、同 Task、同群的历史成功 Action + 成功 Attempt 远端消息；真人 listener 消息只作上下文。
25. `resolve_telethon_target()` 已完成账号 dialog 回退后仍返回“无法解析 PeerChannel”时，当前账号/Task/目标路径当日终结，不能每 30 秒无限重启 observation；不把该事实传播成全局账号终态。
26. 多条 Action 离开 quiet-hours 后必须保留原相邻间隔和 `max_actions_per_hour` 下限；禁止全部映射到窗口开始同一秒。
27. 历史 unknown 只允许从已存在的 Gateway-started Attempt 生成 `remote_outcome_unknown` 事实并补 deadline，不得重发；终态 Action 绑定的未确认频道义务只清空绑定并回到 open，不伪造远端完成。

### 2.2 取消“冻结账号分母”，改为任务内每日动态账号范围

系统仍保留不可变的 `task_day_ledger_id`、时区和业务时间边界，但不再保留不可缩小的 `frozen_account_count`。当日账号范围是任务级动态集合：

- 唯一键为 `(task_id, target_group_id, account_id, task_day_ledger_id)`，不存在跨任务的全局 coverage 分母或全局义务；日期与时区统一由日账本解释。
- 账号在任务 A 未准入，只阻塞 A；它在任务 B/C/D 的资格和履约独立判定。只有 Session 失效、封禁等账号本体事实可同时影响多任务。
- 当日新增且通过当前任务资格检查的账号立即加入；从可恢复 blocker 恢复的账号立即回流。
- 暂时离线、群管准入等待、可切换授权/代理线路归为 `recovering`，保留当日义务并自动复探。
- 账号已删除/禁用/封禁，或没有任何可用当前或备用授权路线，或权威目标群事实证明当日永久不可发，归为 `abandoned_for_day`；终结未进 Gateway 的账号义务，从当前必达数中移除。
- 已真实完成的账号保留 `completed` 事实，后续资格变化不撤销成功，也不把一条成功倒灌给其他任务。

## 3. 问题分类与目标

| 分类 | 核心问题 | 本 PRD 目标 |
| --- | --- | --- |
| C1 目标与任务内实时容量 | 不可变账号分母、历史任务残留和任务间串行使配置目标无法快速履约 | 任务内动态账号范围、不可恢复义务放弃、非运行任务清理、多任务公平并发 |
| C2 资格与准入 | 配置频道、动态群管提示、账号归属和可见性事实断裂 | 建立账号级前置关注与动态准入闭环 |
| C3 义务物化 | 数量槽、ContentMix、Action 互相占用，旧槽导致新义务无法建单 | 义务账本唯一、Action 可重建、ContentMix 不持有数量 |
| C4 执行通道 | 搜索 Window/代理/OCR 与普通互动争抢同一执行容量 | 搜索和普通互动进程、队列、容量独立 |
| C5 AI 生成 | Provider 调用被同群单 ready 全程串行，吞吐无法追赶 | direct 独立并发提交、强上下文版本 CAS、单 active Provider key |
| C6 搜索验证 | OCR+模型共识延迟高、资源重、结果边界复杂 | 仅双 OCR 顺序验证，无 AI 热路径 |
| C7 Recovery | 旧 Action、unknown、持久审计引用和 reserved 跨日不收敛 | 按义务恢复、按远端边界防重、清理不破坏审计 |
| C8 上游义务 | 没有新消息与 listener 故障都显示 0 Action/running | 区分等待上游、监听故障、义务待物化和执行阻塞 |

### 3.1 非目标

- 不降低 800/4000 等已确认的任务配置总量；仅按当前任务资格事实动态维护账号覆盖数，不修改历史远端事实。
- 不把关注成功直接写成群管准入成功。
- 不在 callback 或发送结果 unknown 时自动重试。
- 不使用 AI/VLM、浏览器 Agent 或人工猜测验证码答案。
- 不让搜索代理失败阻塞 AI 活群、浏览、点赞或评论。
- 不通过增加 worker 掩盖账号无资格、义务错账或外部资源不可用。

## 4. 总体架构

```text
        任务日账本 + 任务内动态账号范围 / 来源消息
        ↓
任务专用义务账本（唯一业务真相）
        ↓
资格与准入门（账号级）
        ↓
义务物化器 → ContentMix 只生成内容计划 → Action
        ↓
AI Generation Lane（仅 group_ai_chat）
        ↓
Interaction Lane 或 Search Lane（完全隔离）
        ↓
ExecutionAttempt → 任务专用远端事实
        ↓
义务 confirmed / unknown_hold / blocked / shortfall
```

### 4.1 C1 数量合同

运营仍按 `task_id + target_group_id` 配置 `daily_message_target`。平台不把多个任务的目标合并成一条串行队列，也不用一个任务的账号状态缩小另一个任务的范围。

```text
current_required_account_count =
  count(task_account_state in {eligible, recovering, completed})

planned_daily_target =
  max(configured_daily_message_target, current_required_account_count)

effective_daily_target = planned_daily_target  # 兼容读模型名，不再吸收 confirmed

scheduler_oversend_count =
  confirmed facts accepted after the task-day ledger had no
  available gateway budget under the current target revision

target_reduction_overage_count =
  confirmed/gateway-started facts legal under their Gateway target revision
  but above the later reduced current target

extra_volume_remaining =
  max(0, planned_daily_target - confirmed_message_count - open_quantity_count)
```

- `current_required_account_count` 随任务内资格事实变化，不写入不可变冻结字段。
- `configured_daily_message_target` 是该任务的固定业务目标；账号动态移除不得把已确认总量倒扣，也不得修改其他任务的目标。
- 若当前必达账号数高于配置目标，`planned_daily_target` 随当前任务必达数上调；不可恢复账号放弃后可下调，但不低于配置目标。`confirmed_message_count` 只进入完成量和超发审计，不反向抬高计划目标。
- 每次目标变化递增 `planned_target_revision`。不再持久化或分配 `completion_ordinal`；稳定的数量义务本身就是执行身份。Planner 按目标幂等补齐稳定义务，Gateway 前仅对当前义务执行单行 `action_bound -> gateway_started` CAS，不锁任务日账本、不做中央预扣。明确 pre-transport/pre-accept 失败将同一义务释放回 `open`，不烧掉名额；unknown 保留原义务占位。目标下调时按“额外数量优先、最新创建优先”逐义务 CAS 取消未进 Gateway 的多余义务，coverage 义务不得被误删。
- 数量义务与账号覆盖义务分账：一条真实消息可同时完成“本任务总量 1”和“本任务该账号覆盖 1”，但不得完成另一任务的义务。

### 4.2 C1 账号动态状态机

```text
discovered -> evaluating -> eligible -> completed
                         \-> recovering -> eligible
                         \-> abandoned_for_day

eligible/recovering -> abandoned_for_day
```

- `recovering`：存在已验证可用的自动恢复路径，例如备用授权、可用代理、已知准入流程或可重新探测的短暂离线。它保留本任务义务，但不占用正文 AI 生成和发送 ready 位。
- `abandoned_for_day`：Telegram/Session 权威确认该账号当前无法发送，或当前任务日没有合法执行路线。必须保存原因、事实版本和判定时间，终结未进 Gateway 的当日 coverage/Action，释放义务、worker claim/lease、ContentMix 绑定和实际槽位。
- `session_invalid|session_revoked|session_unauthorized|need_relogin|write_forbidden|account_restricted|account_banned|cannot_send` 一经权威确认，立即在该 Task/目标/任务日放弃；同日不自动复活旧义务。目标群 `target_dissolved|peer_invalid|target_deleted` 是目标级终态，终结该 Task/目标，不能把账号全局判废。
- Telegram client 已尝试 `get_entity` 和当前账号 dialog 枚举后仍无法解析目标 `PeerChannel`，记为 `target_entity_unresolvable`：只终结当前账号 + Task + 目标的当日准入/未进 Gateway Action，coverage 保留显式 shortfall 并在下一任务日重新评估；不能继续 observation gap 循环，也不能据此判定目标对所有账号失效。
- FloodWait/SlowMode 只在明确 `retry_at < deadline_at` 时延后；跨过 deadline 才放弃任务日。网络 timeout、listener/cursor gap 和 unknown 不属于权威无法发送证据。
- 已放弃旧事实版本和旧 Action 不复活；新任务日重新按当日事实评估，定时扫描或重启不能复活旧任务日。该状态只作用于当前 Task/目标，不写全局冻结。
- 已进 Gateway 的 unknown 继续走远程对账，不得通过“放弃账号”绕过防重。
- Session 权威事实可按 `account_id + authorization_slot_id + fact_version` 被多个 Task 读取，但 abandon 必须由每个 Task/目标/任务日独立物化；只有使用同一失效授权槽位的 Task 分别终结。`CHANNEL_PRIVATE` 等无法区分账号无权与目标不存在的结果只终结当前执行路径，不得传播成全局账号事实；独立 peer 证据才能把目标升级为 `target_terminal`。具体 Telegram 结果映射以合同闭合 PRD §2.2 为准。

### 4.3 C1 任务生命周期与残留清理

- 只有主记录存在且 `status=running` 的任务能进入 Planner、Generation、Dispatcher、Recovery 新工作和容量统计；新合同不读取软删除兼容状态。
- 启停删原子推进 `task_lifecycle_epoch`；Planner、Generation、Action、worker claim/lease 和 Attempt 固化 epoch，领取与 Gateway 前均须 CAS 当前 running epoch。旧 epoch 未进 Gateway 的工作撤销释放，Gateway-started 转 unknown 对账。
- 删除事务先推进 lifecycle epoch 并 fence writer，再以唯一 delete operation 冻结 tombstone/delete 集合与 count/hash；先完整写最小远端 tombstone并核对一致，之后才物理删除 runtime、config、剩余子表和 Task 主记录。中途崩溃复用同一 operation，不得先删后补证据。删除后不存在“恢复原 Task”，重新运行必须创建新 Task ID。
- 清理必须以 `task_id` 精确级联；不允许已暂停/删除任务的 account scope、coverage、Action、claim 或统计继续影响运行任务。

### 4.4 C1 多任务并发调度

- 任务启动、任务日开始或目标/账号范围变化时立即建立或终结自己的稳定义务；义务存在不等于已经到期。四类拟人任务按 §4.5 计算当前累计到期量，不创建任务份额或中央 Reservation。
- Planner 只建立主义务；AI 只为当前已到期且真实执行槽可用的义务创建有限 Action，评论/浏览/点赞允许建立有界 future Action，但必须依靠 `scheduled_at` 保持不可领取。禁止把全天目标一次预建成可立即领取的陈旧 Action 队列。每轮先查询所有 running Task 的窄字段候选 ID，再以单行 version CAS 领取到期义务；不使用 `FOR UPDATE/SKIP LOCKED` 或跨 Task 锁。
- 同一账号可由不同任务并发发起 Telegram RPC，不做账号内任务抢占或公平轮转。仅对同一 `remote_mutation_key`/同一 callback/同一群同一消息的冲突副作执行幂等 CAS；账号级 FloodWait 约束该账号后续 RPC，群级 SlowMode 只约束对应 peer，二者都不改写任务资格。
- Session transport 必须以 `rpc_id + authorization_id + task_id + action_id + remote_mutation_key` 隔离并发请求、响应、timeout 和 cancel；不支持安全并发的 adapter 要使用独立 transport channel/client instance，不能退回账号级全局串行，也不能让一个请求覆盖另一个请求的上下文或结果。
- 生产展示既报每任务履约，也报聚合履约。例如 `4000 + 5000 + 800 + 800 = 10600`是四个同时运行的独立目标，不是要依次排空的四段队列。
- Provider、数据库或 worker 真实资源已满时展示当前使用量、等待义务和具体安全 blocker；不展示或计算任务“获配份额”，也不得通过冻结分母、全局串行或让单任务排空后再执行其他任务来掩盖。到 deadline 仍无法自然完成时写显式 shortfall，禁止压缩剩余义务形成突发补量。

### 4.5 评论、浏览、点赞与 AI 活群非压缩拟人节奏

- `fact_first_v3` 与执行节奏正交：typed remote fact、Gateway unknown、防重、准入、恢复和 projector 合同保持不变；只有 Action 的到期量和 `scheduled_at` 改由任务类型节奏决定。
- 四类任务统一使用 `pacing_anchor=max(period_start,task_activation_anchor,source_observed_at when source-scoped)`；在 anchor 精确时刻累计到期量为 0。AI/浏览的 pacing period 是当前任务自然日；评论/点赞的 pacing period 是来源消息首次采集后的滚动 24 小时。
- 当前到期量为 `max(1,floor(pacing_target * curve_weight(pacing_anchor,now) / full_24h_curve_weight))`。分母必须是完整任务自然日或完整来源滚动 24 小时，禁止使用 `pacing_anchor..deadline` 的剩余曲线权重，禁止 `_fit_before_deadline` 或等价逻辑把完整目标缩放进短窗口。已确认、Gateway-started、unknown hold 和有效 open Action 必须从当前到期量中扣除。
- `channel_comment/channel_like/channel_view` 只为当前累计到期缺口建单并保留 `schedule_times()` 生成的 future `scheduled_at`。Planner、takeover 和 Recovery 不得在建单后把 future pending Action 批量改成当前时间；晚采集来源在当前自然日只形成按可执行时段折算的量，余量保持 open/shortfall，不突发追赶。
- quiet-hours 调整必须顺序处理：第一条移到合法窗口后，后续 Action 至少保留调整前相邻间隔；若再次落入 quiet-hours，继续移到下一个合法窗口并从该点保留间隔。`max_actions_per_hour` 的间隔下限在后置调整后仍必须成立，禁止多个时间落到同一窗口起点。
- 拟人间隔同时约束同批和跨 Planner 批次：`channel_comment/channel_like/channel_view/group_ai_chat` 每次物化前，必须读取同 Task、同 `action_type` 尚未终结 Action 的最晚 `scheduled_at`，新批首条至少排在该时间加当前节奏最小间隔之后，后续条目保持原相邻间隔。任务窗口放不下时少建或不建并等待下一窗口，禁止因为每轮只补一条而反复落到同一曲线整点，也禁止压缩到 deadline。
- AI 每轮只物化当前缺口；同批 `scheduled_at` 使用正常期/启动期/低频期的现有间隔与 jitter，不得因 fact-first 改为同一个 `now`。绕过 legacy 账号容量时只能绕过旧容量判断，不能改写已经计算的 `planned_at`。
- Planner 有当前到期欠额时可按现有 debt recheck 节奏继续推进；没有欠额时按 `next_run_after_task()` 等待下一个曲线时点。禁止对这四类任务固定每 2 秒无节奏追赶。
- 暂停、恢复、晚启动或容量不足不回填已逝时间：只在剩余任务日曲线内推进，deadline 到达后将欠额投影为 `terminal_shortfall/content_capacity_gap`。不通过提高本地上限、缩短间隔或重写历史 Action 伪装完成。

## 5. C2：AI 活群账号资格与群准入

### 5.1 任务配置

`group_ai_chat` 新增或规范化字段：

| 字段 | 规则 |
| --- | --- |
| `tasks.group_ai_prejoin_channel_ids` | 独立表字段，`UUID[] NOT NULL DEFAULT '{}'`；0–3 个去重后的 `OperationTarget` 频道 ID，保存时校验同租户、类型为频道并推进 Task 配置 revision；不得只藏在通用 JSON 配置中 |
| `prejoin_follow_required` | 配置非空时固定为 true；不得由运行时静默关闭 |
| `group_bot_admission_policy_id` | 目标群可信 bot/完成策略，继续复用既有 policy |

配置频道属于运营已知的前置要求；群机器人提示可以在入群后给出新的账号级频道或 confirmation callback。当前 admission 世代的最终必需集合为：

```text
required_channels = configured_prejoin_channels ∪ trusted_bot_prompt_channels
```

任何一侧存在无效、无法访问或非频道引用时显式 blocked，不得忽略该项继续发言。

### 5.2 单账号准入流程

1. 校验账号 Session、目标群和配置频道身份。
2. 使用该账号并发关注配置中的 0–3 个频道；每个频道建立独立 `configured_channel_follow` 事实并记录 `pending|success|blocked|unknown`，只有同一频道远程副作用幂等串行。
3. 全部配置频道为 `success` 后，记录目标群 `join_start_cursor`，再执行加入目标群。
4. 使用该账号自己的 Telegram 视角读取入群后的群管控制消息；只接受既有可信来源规则。
5. 提示含明确收件人时，使用“归一化展示名精确匹配 + 同一可信 bot 原消息内的群聊要求链接/按钮”组合绑定被拦截账号；只有展示名或只有链接都不得单独绑定。该组合还必须在当前同群待准入账号中唯一；展示名重名且链接无法区分时写 `recipient_ambiguous`，等待 reply/viewer-specific 新证据，不猜测账号。username、viewer peer 和 reply relation 作为附加审计证据，不匹配不得写为群级规则。
6. 提取该可信提示中全部动态频道和 requirement link/callback，为该账号、当前 `admission_version` 按每个稳定 `requirement_action_key` 建立独立子动作；不设 0–1 个总数上限。
7. 完成各 requirement action 自身依赖的动态频道关注后，对原 message/fingerprint 中所有尚未成功且互不冲突的 requirement action 并发执行精确点击；相同 `remote_mutation_key`、相同 callback 或同一按钮身份才幂等串行。每个 action key 最多一次成功，unknown 时只复探该指纹，不再次点击；某一 action 阻塞不得暂停其他无依赖 action。
8. 有可信提示的路径执行 `post_follow_visibility_probe`；只有全部 requirement 完成且探针消息在完整观察窗内仍可见，才进入 `group_bot_admission_ready`。没有可信提示的路径不发送探针，以连续 30 秒零提示且零 observation gap 的 `no_prompt_30s_passed` 事实闭合。
9. 首条 AI 正文继续执行发送后可见性核验；Telegram 返回 message id 但随后被群管删除，不计成功。

没有可信群管提示时采用固定闭合规则：配置频道全部成功且已确认在群后，以数据库时间持久化 `observation_started_at/no_prompt_pass_at/observation_version`，并冻结 `surface_kind=target_group_control_stream + surface_peer_id + viewer_account_id + viewer_authorization_id + listener_instance_epoch + listener_policy_version + observed_start_cursor + observed_end_cursor + surface_identity_hash`。连续 30 秒内同一 surface 零可信提示、cursor 连续且 `observation_gap=false`，写 `post_follow_visibility(outcome=no_prompt_30s_passed)` 并视为群机器人验证通过；不在事务内 sleep。30 秒内出现提示立即转 requirement；网络、Session、listener、surface identity 或 cursor gap 不是“无提示”，必须递增 version 并重建连续观察或按账号不可发送合同放弃。私聊/其他 peer 不驱动 ready。ready 后到首条正文 Gateway 前出现新可信提示，旧 ready 失效。

### 5.3 幂等与并发

- 每个 Task 独立拥有 `TaskGroupBotAdmission` 投影，唯一键为 `(task_id, account_id, group_id)`；其他 Task 的 ready 不能使本 Task ready。投影只保存本 Task 当前 policy/admission version、所引用事实和 ready CAS，不拥有 Telegram 原始事实。
- 四类 Telegram 远端事实统一落在 Task 无关的 `account_group_admission_facts`，以 `fact_kind=configured_channel_follow|dynamic_channel_follow|requirement_confirmation|post_follow_visibility` 区分。业务唯一键不含 `task_id`，而由 `account_id + target_peer/channel_id + remote_mutation_or_observation_identity + fact_version` 构成；多个 Task 可引用同一仍新鲜事实，但各自重新计算 admission ready。配置频道事实不要求伪造 bot `source_message_id`。
- 同一账号同一 admission 世代每个频道最多一个 open follow，每个 `source_message_id + fingerprint + requirement_action_key` 最多一个 open click；一条可信提示可有任意多个不同 action key。
- 不限 action 总数，但单个不可变 message fingerprint 只扫描和物化该消息快照内的有限 key 集合；重复/等价 URL 或按钮规范化为同一 key，新要求必须来自新的 source/fingerprint/version，禁止在单事务中无界追逐 bot 新提示。
- 不同账号、同一账号的不同非冲突 requirement action 都可并发；只有同一远程副作用身份幂等串行。
- admission 未 ready 时保留原履约义务，不调用 AI Provider，不占用正文 ready 队列。
- 任务重启复用成功关注事实；规则集合、可信 bot、rejoin 或目标 revision 变化时递增 `admission_version`。
- 控制消息、按钮集合、配置频道、observation cursor 或 surface identity 变化必须递增 `requirement_set_version` 并重算集合 hash。进入 ready 只使用一条 admission 行的 expected version/hash CAS，同时验证 observed end cursor、surface identity、零 observation gap、全部 action success、零 open/unknown 和同版本 visibility fact；不显式加锁。任一变化保持未完成。

### 5.4 失败与展示

| 状态 | 含义 | 处理 |
| --- | --- | --- |
| `prejoin_follow_pending` | 配置频道尚未关注完成 | 只执行 follow，不生成正文 |
| `awaiting_bot_prompt` | 已入群，等待账号级提示/观察闭合 | listener 持续取证 |
| `awaiting_confirmation` | 动态频道已完成，等待 callback | 精确 source/fingerprint 执行 |
| `post_follow_visibility_probe` | 协议完成，等待可见性 | 保留义务，不计成功 |
| `ready` | 可进入正文生成 | 仍需 Gateway 前复核 version |
| `blocked` | 频道无效、账号无权限、提示冲突等 | 显示精确 blocker 和账号 |

## 6. C3：义务账本驱动的可重建物化

### 6.1 核心决策

不再让 ContentMix Cycle/Slot 成为数量和覆盖的所有者。各任务既有专用账本继续作为业务真相：

- AI 账号覆盖：`TaskAccountDailyCoverage`；
- AI 群日数量：`TaskGroupDailyMessageSlot`；
- 评论、点赞、浏览：各自消息义务/ordinal；
- 搜索点击：稳定 `SearchClickObligation` UUID；不保存 click/completion ordinal。

ContentMix 只决定 direct/reply、话题、素材、引用关系和内容组合。删除或重建 ContentMix 不得删除、复制、确认或换号完成数量义务。

### 6.2 统一义务投影

新增统一只读/物化投影 `FulfillmentObligationProjection`，不复制第二套目标总账：

| 字段 | 含义 |
| --- | --- |
| `obligation_type + obligation_id` | 指向任务专用账本的稳定身份 |
| `task_id + target_date/revision` | 任务与业务窗口 |
| `account_id` | 账号覆盖义务必填；纯数量义务可空 |
| `state` | `open|recovering|materializing|action_bound|executing|unknown_hold|confirmed|abandoned_for_day|cancelled_by_task_lifecycle|blocked|shortfall|remote_reconcile_only` |
| `active_action_id` | 当前唯一 open Action，可空 |
| `materialization_version` | 每次合法重建递增 |
| `deadline_at` | 业务截止时间 |

Action 必须写 `obligation_type`、`obligation_id`、`materialization_version`，并以数据库唯一约束保证同一义务同时最多一条 open Action。远端成功先追加唯一 `fulfillment_remote_fact` 作为业务真相；Attempt、Action、主义务、可选 coverage 和 worker claim/lease 由 projector 分别单行 CAS 收敛，不使用跨表原子事务或锁序。已有 confirmed/unknown fact 时任何 worker 都不得再次进入 Gateway。

数据库必须同时保证：`FulfillmentObligationProjection(obligation_type,obligation_id)` 唯一、ContentMix 投影 `(obligation_type,obligation_id,materialization_version)` 唯一，以及 Action 对同一义务的非终态 partial unique。任一步唯一键冲突只回读既有行，不创建第二份内容计划或第二条 Action。

### 6.3 物化状态机

```text
open
  → materializing
  → action_bound
  → executing
  → confirmed

action_bound / executing-before-gateway
  → open（原义务、原账号、version+1）

gateway_started + result_unknown
  → unknown_hold → remote_reconcile → confirmed | open | blocked

资格、来源或截止不可满足
  → blocked | shortfall
```

- 物化器按任务、deadline 和类别读取 partial index 覆盖的窄字段候选 ID，再以单行 version CAS 领取义务；不扫描全部历史 Action，不使用显式行锁。
- 未进入 Gateway 的 Action 明确失败时，终结旧 Action并重开同一义务；不得创建新的数量槽。
- 已进入 Gateway 或 unknown 时，禁止物化第二条 Action，必须先完成远端对账。
- `unknown_hold -> open` 只接受同 request identity 的 Gateway journal 证明 `remote_mutation_started=false`，或 Telegram/adapter 返回明确的 pre-accept rejection；消息历史不存在、当前 reaction 不存在、页面/fingerprint 未变化、超时、history gap 或换账号看不到都不能证明未执行。真实幂等“设置期望状态”adapter 只能重放同一 mutation identity，不能创建新义务。完整字段和 CAS 见闭合专项 PRD §7.1。
- 账号覆盖义务不得换号；纯额外数量义务可在同一 task/group 范围重新选合法账号。
- 任务日开始时建立配置总量义务，账号覆盖义务随本任务动态资格集合增加、恢复或放弃；不再因“没有未绑定 ContentMix 槽”产生 `quantity_slots_unavailable`。

### 6.4 ContentMix 新边界

- ContentMix 输入是一组已领取义务，不拥有或冻结这些义务。
- ContentMix 创建失败只释放 materialization lease，不改变义务目标。
- reply 目标在 AI 调用前重新验证；失效时为同一义务重新选择合法 relation。
- 一个旧 reply 等待不得阻塞其他 direct/coverage 义务。
- 物化阶段允许分短事务：先以单行 CAS 领取义务，再按唯一键创建 ContentMix，最后按唯一键绑定 Action；任一步失败都只释放物化 lease 并保持原义务可重建。远端成功采用事实先行：先提交唯一 remote fact，再由幂等 projector 分别收敛 Attempt、Action、义务、coverage 和 lease，不存在跨表锁顺序。

## 7. C6：搜索验证码双 OCR 顺序识别

### 7.1 产品决策

- OCR A 固定为 RapidOCR，OCR B 固定为 ddddOCR。
- 搜索验证码路径禁止调用 AI/VLM；相关 Provider key、模型和 fallback 不注入 image-verification worker。
- 每次 OCR 输出都必须规范化并精确命中 Telegram 当前 callback 候选，禁止自由文本猜测。

### 7.2 执行流程

1. 冻结当前 `challenge_fingerprint`、图片 hash、message revision 和 callback 候选。
2. 运行 RapidOCR。输出为空、格式非法、无候选匹配或运行失败时，不点击，进入 ddddOCR。
3. RapidOCR 输出合法时，提交前复读同一 fingerprint；以 `(challenge_fingerprint,normalized_answer)` 做 callback CAS，每个不同合法答案最多提交一次；A/B 得到相同答案时不得形成第二次 callback。
4. Telegram 权威返回 `answer_rejected` 且页面仍是同一 fingerprint 时，允许使用已计算或新执行的 ddddOCR 答案；若拒绝已产生新 fingerprint，则旧 challenge 终结，新 challenge 从 A 开始，不在旧 fingerprint 上尝试 B；callback 结果 unknown 时禁止尝试 B。
5. ddddOCR 为空/非法时不点击；B 无安全答案或其 callback 被权威判错后，先读取远端是否已产生新 fingerprint。已换题则记录 `challenge_replaced_on_rejection` 并从 A 重开；仍为同 fingerprint 时，只有 `BotProtocolSample.refresh_mode=approved_refresh_callback` 且按钮身份精确匹配才允许一次 refresh。没有审批 refresh 动作时写 `refresh_not_supported` 并结束 Attempt。
6. A/B 同答案且 A 已被拒绝时不得重复点击；`challenge_safety_policy` 只定义预算/deadline，不能创造 refresh 方法。`/cancel`、`/start`、重发关键词、点击未知按钮和本地页面刷新都禁止。deadline 过期、预算耗尽、refresh unknown 或 challenge 消失时显式结束当前 Attempt。

### 7.3 状态与验收

记录 `ocr_source`、规范化候选、识别耗时、callback 状态、fingerprint、refresh 前后身份和精确错误码；不保存图片、按钮原文或 Session。

成功只认 Telegram 页面进入已审批的搜索结果状态；验证码 solved、页面可见或 Action success 都不等于点击完成，最终点击仍只认 `target_click_observed=true`。

## 8. C5：AI 生成并发与上下文提交

### 8.1 并发模型

- 每个 AI Action 对应独立 `GenerationJob`，不同 Action 可并发调用同一或不同模型。
- 系统只允许一个 active `ai_provider_key_version`，密钥由 KMS/secret manager 保存。所有模型共享该 key 的单一 `max_inflight/RPM/TPM` token bucket；Job 取得 key token及可选 model 子限制后调用，不设置全局单请求锁，也不为模型复制一整套 key 额度。
- key 轮换时旧 in-flight Job只在旧 key version 对账到终态，新 Job只使用新 active version；429/quota 更新该唯一 key 的冷却，timeout usage unknown 按冻结估算保留消费。
- 不同群、不同模型天然并发；同群 direct 义务也并发计算并在各自通过发送前检查后立即 ready/发送，`generation_sequence` 仅用于审计，不得等待更早 direct。reply/强上下文结果仍按自身 revision CAS。

### 8.2 上下文版本与提交 CAS

1. GenerationJob 冻结 `tenant_id + group_id + task_id + action_id + obligation_id + context_version + generation_sequence`；领取时原子写 owner、lease epoch 和 `generating`。
2. Provider 调用期间不持有数据库事务、账号锁或 Telegram Session。
3. direct 且无引用/素材强依赖的结果返回后，CAS 只验证 Action/义务、task/group/scope、admission/version 和 `generation_sequence` 仍有效；不要求当前 `context_version == context_snapshot_version`，改在发送前执行最新上下文的重复、矛盾、质量轻量检查。
4. reply、强上下文或素材绑定结果仍严格 CAS `context_version + reply/source revision`；失败记 `generation_context_superseded`并重开原义务。direct 只在轻量检查确认冲突时写 `generation_direct_conflict_superseded`，不得因其他 direct 先发送就整批作废。
5. 同一义务只有一个结果可进入 `ready`；重复/晚到 Provider 结果只保留有界审计。

### 8.3 发送边界

- AI 计算并发不等于 Telegram 发送并发。
- Gateway 继续执行远程副作用幂等、群 SlowMode、账号 FloodWait、准入 version、上下文新鲜度、远端 unknown 和重复内容门禁；不建立账号级全局单 inflight 来串行不同任务。
- direct Action 不因计划顺序等待前序 Action；只有同一远程副作用、账号 FloodWait、群 SlowMode或 reply/强上下文依赖允许串行。每条 Action 仍独立执行发送前复核。

## 9. C4：搜索与普通 Telegram 独立执行通道

### 9.1 通道划分

| 通道 | 任务 | 网络 | 容量与 worker |
| --- | --- | --- | --- |
| `interaction_lane` | AI 活群、评论、点赞、浏览 | 账号直连凭证 | 独立 worker/lease、heartbeat、实际执行槽 |
| `search_lane` | 仅纯 `search_click + click_only` | 指定 Clash/Mihomo 绑定 | 独立连续 solver、proxy/OCR worker、真实空闲执行槽 |

两条通道可以共享 Task/Action/Attempt 表和中央任务公平读模型，但不得共享 active claim capacity、账号代理候选、OCR 资源、失败熔断或 worker 健康结论。

### 9.2 运行规则

- search worker 不得领取普通互动 Action；interaction worker 不得领取搜索 Action。
- 搜索 solver 由“开放义务、候选事实变化、assignment 终态、worker 空闲”事件持续唤醒；有可执行路径时立即原子落库 assignment/Action并执行，不存在 Window。仅 lifecycle/deadline/资格/binding/dedupe 失效时取消，不得换成普通 Action 或改绑路径。
- assignment 行本身是数据库持久待执行工作；通知丢失或 worker 重启后按数据库时间、owner fencing 和过期 lease 接管同一 assignment。持久 `SearchProtocolSession` 至少保存 `assignment_created|keyword_sent|hot_list_page|group_category|verification_required|result_page|target_found|click_started|click_unknown|completed|failed`、page fingerprint、request identity、viewer cursor 和分页/challenge identity。每次远端转换先提交 phase CAS；`hot_list_page` 只能点击版本化协议样本批准的“群聊/群组” selector，禁止靠内存状态或 AI 猜测。
- 搜索必须分别维护订阅解析、节点真实 egress、账号授权、active binding、assignment binding version 和 Gateway transport 事实；没有真实出口时只阻断 search lane，普通互动继续运行。
- 任一通道扩容前必须证明主机、PostgreSQL、Redis 和连接池预算；不能从另一通道借用未审计容量。
- 通道级健康必须包含最近 claim、Attempt 和任务专用远端事实，不只看容器。

## 10. C7：Recovery、账本与远端事实收敛

### 10.1 清理边界

- terminal Action 若被 takeover item、remote reconcile case、Gateway evidence journal 或其他持久业务事实引用，不进入物理删除。
- 所有 `action_id` 持久引用建立精确索引；候选查询先用 `NOT EXISTS` 排除，不靠删除时报外键错误。
- runtime detail、metric、Action 清理各自使用独立 checkpoint、batch 和短事务。
- 单批失败记录精确表/Action/错误并停止该清理类别；不得回滚本轮已完成的准入、义务或 remote reconcile。

### 10.2 义务恢复

- `active_action_id` 指向不存在或 terminal pre-Gateway Action：重开原义务并递增 materialization version。
- Action open 但 lease owner heartbeat 过期：先以 `populate_existing=True` 或等价新 Session 强制重读 owner heartbeat 和 Action，再以旧 `owner_id + owner_fencing_epoch + action_version + lease_expires_at` 做单行 CAS，递增 fencing epoch 并接管；不显式锁两行。旧 owner 的续租、结果和终结提交都必须带原 epoch，不匹配写 `stale_owner_rejected`。未进 Gateway 可回收，已进 Gateway 转 `unknown_hold`。
- heartbeat/lease 是否过期只使用同一 PostgreSQL 事务内的 `clock_timestamp()`，全部时间为 UTC-aware `timestamptz`；禁止用 worker 本地时钟、容器时区或 naive datetime 参与接管 CAS。
- coverage、数量槽、ContentMix 和 Action 绑定不一致时，以任务专用义务账本为准重建投影，不新增目标。
- `reaction/view` 义务若仍指向 `failed|skipped|cancelled` Action 且不存在 typed remote fact，CAS 清空 `current_action_id` 并恢复 `open`；不得把旧日义务重新批量执行，也不得改写已 confirmed 义务。
- 历史 `unknown_after_send` 缺 deadline 时，仅当最新 Attempt 已进入 Gateway 且仍无权威 remote outcome，才幂等追加 `remote_outcome_unknown` 事实并从修复时刻建立 reconcile deadline；deadline 到期只转 `closed_unknown/closed_with_unknown_shortfall`，禁止重发或记成功。
- 每次状态修复写 `RecoveryAudit`，包含 before/after hash、义务身份、旧 Action 和原因。

### 10.3 跨日收口

- 新任务日前先生成上一日未确认义务汇总：`recovering|abandoned_for_day|unknown_hold|shortfall`，不把它们改写成功。
- 新日建立新的任务日时间边界和配置数量义务，再从当前任务资格事实重建动态账号范围；不复制上一日或历史 `TaskMembershipAdmissionItem` 范围。历史 unknown 继续占用原远端防重身份，但不得错误占用新日同账号义务。
- 相同账号新日是否可再次履约，按新的资格事实和明确跨日安全规则判断，不从旧失败无限快速重试。

## 11. C8：上游来源与连续任务状态

### 11.1 状态区分

| 状态 | 判定 |
| --- | --- |
| `waiting_for_source` | listener 健康，当前规则窗口确实没有新来源消息 |
| `listener_stalled` | 版本化 policy 的 heartbeat/cursor/success-poll 任一 stale-after 超期，无法证明没有新消息 |
| `source_unresolved` | 收到来源但目标实体/revision/权限无法确认 |
| `obligation_open` | 已创建消息义务，尚未物化 Action |
| `execution_blocked` | 已有 Action，卡在资格、claim、Gateway 或远端结果 |
| `source_completed` | 当前来源消息全部 ordinal 有真实终态 |

Task 主状态仍可为 running；任务中心必须展示上述当前业务状态，不能用“0 Action”同时表示正常等待和故障。

### 11.2 来源义务生成

- 以 `(task_id, source_message_id, source_revision, obligation_kind, ordinal)` 建唯一义务。
- listener 重复采集不得重复建立义务；revision 变化只影响未进入 Gateway 的旧义务。
- 来源删除或权限消失时，未执行义务进入 `source_unresolved|shortfall`，已进入 Gateway 的保持远端对账边界。
- `dynamic_new` 任务没有新消息时不制造空 Action，也不写失败。

### 11.3 评论引用目标

- `reply_target_missing` 先记 `local_target_unresolved`：校验 listener cursor、幂等 resync，然后从同租户、已确认加入对应 peer 且 Session 可用的候选账号池中随机选取一个，精确读取 Telegram 原 message id。权限/未入群/Session 终态失败将该账号从本次候选池 `-1` 并保存带版本、时效和账号/peer 作用域的 typed fact，下一 run 按仍新鲜事实过滤，但不冻结账号或改写其他任务资格。网络/timeout/unknown 不扣候选池，不过本轮继续尝试其他未探测账号；候选池耗尽或全部 transport unknown 仍为 `source_unresolved`，不写 `remote_target_deleted`。只有查看账号能读该 peer 且 `history_visible_from <= source_created_at` 时，“消息不存在”才形成候选负面证据；最终删除结论必须 CAS probe run、expected source revision/state hash、listener cursor，并确认没有更新正向 listener/probe fact。迟到负面结果写 `stale_negative_probe_rejected`；晚入群或 history gap 仍为 unresolved。
- 任务模式允许 direct 时，可以把同一 ordinal 重新物化为 direct comment，并明确记录 `reply_unavailable_direct_allowed`。
- 任务要求 reply 时，没有合法目标就保持 blocked/shortfall；不得静默改为直接评论。
- 账号无评论权限与引用目标缺失分开记录和补位。

## 12. API、前端与权限

### 12.1 API/读模型

任务详情增加：

- 任务日动态范围：配置总量、当前必达账号数、eligible、recovering、abandoned_for_day、confirmed、shortfall，并显示上次刷新时间和事实版本；
- 多任务并发：每任务目标、confirmed、立即可执行义务、等待真实资源数、Planner/Generation/Dispatcher 实际并发数和最近进度；不计算需求速率，不展示“抢到账号/份额”；
- 义务物化：open、recovering、materializing、action_bound、unknown_hold、confirmed、abandoned、lifecycle-cancelled、remote-reconcile-only；
- 通道状态：interaction/search worker、scope capacity、最近 Attempt 和远端事实；
- 来源状态：waiting、listener stalled、source unresolved、obligation open；
- OCR：A/B/refresh 状态和错误码，不返回图片或答案原文。

新增以下版本化策略读写合同：

| policy | 必填字段 | 读写 API | 失效行为 |
| --- | --- | --- | --- |
| `provider_capacity` | 唯一 active Provider key version 的 `max_inflight/RPM/TPM`、可选 model 子限制、usage unknown 计费、cooldown、`effective_at/revision` | `GET/PATCH /api/ops/fulfillment-policies/provider-capacity` | key 或必需额度缺失时显式 `provider_capacity_policy_missing|stale`；禁止同时激活第二个 Provider key |
| `search_execution` | assignment business deadline、binding freshness、worker 空闲槽观测、`revision` | `GET/PATCH /api/ops/fulfillment-policies/search-execution` | 禁止创建新 assignment；已落库 assignment 按冻结 revision 执行 |
| `challenge_safety` | OCR A/B、refresh 预算/deadline、`revision`；实际 refresh 方法只能来自版本化 `BotProtocolSample.refresh_mode` | `GET/PATCH /api/ops/fulfillment-policies/challenge-safety` | policy 缺失写 `challenge_safety_policy_missing|stale`；协议无 refresh 写 `refresh_not_supported` |
| `listener_freshness` | heartbeat/cursor/success-poll stale-after、`revision` | `GET/PATCH /api/ops/fulfillment-policies/listener-freshness` | `listener_stalled` |
| `generation_lease` | lease ttl、renew interval、takeover grace、`revision` | `GET/PATCH /api/ops/fulfillment-policies/generation-lease` | 停止新 claim，不接管无版本 owner |
| `recovery_lease` | heartbeat stale-after、lease ttl、takeover grace、`revision` | `GET/PATCH /api/ops/fulfillment-policies/recovery-lease` | 停止 takeover，不猜测 owner 失效 |
| `fulfillment_metrics` | remote fact observation window、sample freshness、`revision` | `GET/PATCH /api/ops/fulfillment-policies/fulfillment-metrics` | 只影响诊断展示，不参与义务是否立即执行 |

所有 PATCH 必须接收 `expected_revision + change_reason`，使用 CAS 生成不可变 policy version，返回影响范围与 validation 结果；Action/Assignment/GenerationJob/Listener 在创建时固化对应 `policy_version`，后续修改不重解释已开始副作用。前端提供当前值、版本历史、校验错误、影响范围和回滚到已存版本，不允许静默使用代码默认值。

### 12.2 写权限

- 任务频道配置：`tasks.manage`；
- 任务物理删除：`tasks.manage + explicit_delete_confirmation`；确认绑定 `task_id + task_title + expected_task_lifecycle_epoch`，以 Task 单行 epoch CAS 启动 delete operation，版本不一致即拒绝；不执行显式行锁。审计保存 operator/reason/approval/before-after hash；远端 tombstone 只保留 mutation/request/fact hash 与 reconcile 状态，不保存 config hash 或可恢复配置；
- 删除请求在 `fencing` 提交并隐藏 Task 后返回 `202 + task_delete_operation_id + state`；`GET /api/task-delete-operations/{operation_id}` 供前端读取阶段、计数、checkpoint 与精确错误，`POST .../{operation_id}/resume` 仅允许 `tasks.manage + approval_ref + expected_stage_version + expected_snapshot_hash` 在同一 snapshot 下恢复。只有查询到 `committed` 才显示“物理删除完成”，不得让 HTTP 请求等待全部 tombstone/删除批次；
- 群管 policy/可信 bot：`targets.manage`；可信来源与多 requirement action 解析规则变更必须保存原 message/fingerprint 审计；
- Provider/search/challenge/listener/generation/recovery policy 只读：`ops.read`；修改：`ops.policy.manage + approval_ref`；
- policy rollback 是创建新 revision，不物理删除历史版本；审计必须记录 operator、reason、approval、before/after revision 和影响任务；
- unknown remote reconcile：既有受保护生产 workflow，并强制使用闭合专项 PRD §7.1 的 mutation-specific negative evidence；任何 `open` apply 需受保护权限、expected hash 和 evidence version CAS；
- Recovery preview：`ops.read`，apply：`ops.manage + approval_ref`；
- 旧合同全量删除 preview：`ops.read`；apply：`ops.manage + approval_ref + expected_delete_set_hash`，删除集合包含所有旧合同状态的 Task，逐 Task 复核 epoch/version/hash，漂移项拒绝且不得扩张集合；
- 禁止前端提供“批量 ready”“清空 ContentMix”“unknown 重发”按钮。

## 13. 先新建、切 route、再删除旧 Task（禁止任务迁移）

1. 每个 release train 先部署 inactive-by-default 新 writer，由运营直接按当前确认配置创建全新的 `prepared` Task ID；只保存新请求自己的 config hash，不复制旧账本、完成量、账号范围、准入、ContentMix 或 Action。
2. 从 prepared 集合选一个真实 Task 做 allowlist canary，直接按正常义务执行；canary 只要求形成完整 typed remote fact 链，不计算吞吐、required rate、预计完成量、P95/P99 或折损系数。未形成事实链时不得切 route 或删除旧 Task。
3. activation manifest 精确绑定 `old_task_ids + expected old epoch/version + prepared new_task_ids + new_config_set_hash + route_epoch`。激活只 CAS 一条 route epoch；成功后全部旧 Task ID 同时失去 Gateway 权限，全部 prepared 新 Task 从 0 运行，不需要等待第二次人工重建。
4. route 切换后才为旧 Task 启动持久删除 operation；未进 Gateway 的运行义务、Action、ContentMix 和 lease 按 lifecycle 终结，`success|gateway_started|unknown` 只写与 Task 外键解耦的最小 tombstone/reconcile 事实。
5. 最小 tombstone 校验完成后物理删除旧 Task 主记录与全部可重建 runtime/config。删除批失败不停止新 Task，但旧 Task 始终被 route epoch fence，不能重新启动或进入 Gateway。
6. 同日新 Task 的 `confirmed/gateway_started/unknown=0`，旧 Task 当日成功不抵扣新目标。创建页和确认请求必须确认 `same_day_recreate_resets_progress=true`。
7. 不存在 migration preview、quarantine、resolution、shadow 双写或旧 Task active 兼容路径。route 切换前可删除 prepared Task；route 一旦切换就永久禁止恢复旧 Task/旧 writer，无论新 Task 是否已进入 Gateway，故障只允许停止受影响新 Task并前向修复新合同。

## 14. QA 与验收

### 14.0 C1 动态范围与多任务并发

- 同一账号分别在四个任务建立独立资格/覆盖行；A 群准入阻塞不改写 B/C/D。
- 任务运行中新增合格账号立即加入；暂时离线恢复后回流；无合法恢复路线时进入 `abandoned_for_day` 并释放未进 Gateway 义务。
- Telegram 权威返回 Session 失效、需要重新登录或账号/目标不可发送时，当日立即放弃；群已解散时终结目标而不全局冻结账号；网络 unknown 不误判放弃。
- 新 Task 只从当前 797 合格事实建立范围，不读取旧 Task 的历史 800 范围。
- 高目标 revision 已合法发送后账号放弃导致当前目标下调时，只记 target-reduction overage；下调后逐义务 CAS 取消尚未 Gateway 的多余义务，不使用账本预算锁。明确 pre-accept 失败将同一义务释放回 open，不出现替代义务或重复 mutation。
- 暂停/停止/删除任务后，Planner/Generation/Dispatcher 新领取、活跃容量和任务统计立即为 0；未进 Gateway 残留被清理，unknown 仍只远程对账。
- 同时启动目标 4000/5000/800/800 的四任务；四任务必须同时持续产生 GenerationJob/Action/Attempt，同一账号也能为不同任务并发发起非冲突 RPC，不存在账号抢占或任务间转移使用权；自然日最终逐任务满足计划目标且无超发。
- `channel_comment/channel_like/channel_view` 建单后 future `scheduled_at` 不得被 Planner 同事务改成当前时间；80 评论、50 点赞、796 浏览的晚采集来源红测必须只物化当前累计到期缺口，不能单批生成完整目标。
- AI 日目标在 `planning_anchor_at` 时 `due=0`，完整日中途只增长到曲线累计量；晚启动 800 目标在剩余 6 小时内不得放大成 800。`immediate=True`、fact-first 同批全 `now`、剩余日分母和容量槽改写 `planned_at` 的红测必须失败。
- 暂停恢复或中途创建任务不追赶已逝时段；剩余曲线容量不足时输出 shortfall/blocker，不能在短窗口清空剩余日目标。

### 14.1 AI 准入

- 配置 0/1/3 个频道，验证 `tasks.group_ai_prejoin_channel_ids` 持久化、去重和“并发关注全部成功后再入群”；四类准入事实分别可下钻。
- bot 提示与配置频道相同、补充、冲突、私链、明确收件人不匹配、多 bot；原 source 删除/移窗时按被拦截账号重绑或 probe，读取异常不得误判删除。
- 两个账号并发提示不得串绑；展示名相同但群聊要求链接不同时不得误绑，展示名和要求链接都相同时必须 `recipient_ambiguous` 而不是猜测账号；同一可信提示内 0/1/2/N 个 requirement action 均按 action key 执行，重复/等价按钮不产生重复 follow/click。
- 在 ready CAS 前并发追加可信提示或推进 observation cursor，旧 requirement set/visibility 版本必须 CAS 失败并回到未完成，不能提前发正文。
- 配置频道完成并已在群后，连续 30 秒无可信提示且 observation 无 gap 自动 ready；29 秒不得通过，30 秒内出现提示必须进入 requirement，listener/cursor gap 不得当成“无提示”。
- message id 后被群管删除不得确认覆盖；可见后才写正式 credit。

### 14.2 C3 义务物化

- 同一义务同时最多一条 open Action；双 worker 并发不重建两次。
- ContentMix 失败、reply 失效、pre-Gateway Action 失败均重开原义务，不新建数量槽。
- Gateway unknown 阻止第二 Action；远端确认后正确 confirmed/open。
- AI/评论、reaction、view、search 分别注入 positive、pre-transport/pre-accept negative 和 evidence gap；只有绑定同 request identity 的 `remote_mutation_started=false|pre_accept_rejected` 能重开。远端历史/当前状态不存在、页面不变、未查到或超时均不能重发。
- 一个旧 reply 等待不阻塞其他 direct/coverage 义务。

### 14.3 搜索与 OCR

- RapidOCR 无输出/非法 → 同 fingerprint ddddOCR；A 权威 rejected 后同 fingerprint 才能 B，远端已换 fingerprint 则新 challenge 从 A 开始。
- B 无安全答案或 rejected 后分别验证远端自动换题、已审批 refresh callback 和 `refresh_not_supported`；`/cancel|/start|重发关键词` 调用数必须为 0。callback/refresh unknown、A/B 相同答案、deadline 或安全预算到期均不得重复点击；只有权威新 fingerprint 才能重开。
- 验证链不加载/调用任何 AI Provider。
- 搜索 proxy 全部失败时普通互动 Action/Attempt/远端事实继续增长。
- assignment 通知丢失、search worker 重启和 lease 接管后仍从已持久 `SearchProtocolSession` phase 继续；极搜热榜页、群聊分类页、结果页与验证码页转换均有 CAS，不能重复发关键词或猜 selector。

### 14.4 AI 生成并发

- 同 key 多 Action 在配置并发内真实重叠；不持有数据库长事务。
- 同一时刻只能有一个 active Provider key version；两个模型并发共享该 key 总额度，key 轮换保留旧 in-flight usage，token 领取失败不产生调用。
- 同群多个 direct 结果真重叠并分别立即发送，不等待较小 generation sequence；reply/强上下文仍严格 CAS。
- 晚结果、429、timeout、跨群 scope mismatch 不覆盖有效结果、不进入 Gateway。
- Generation 并发提升后，重复 remote、上下文重生和群管拦截不得上升。

### 14.5 C7/C8

- 7896 等规模持久引用 Action 不进入删除，合法 terminal Action 仍能批量清理。
- cleanup 某批失败不回滚其他 Recovery 类别的已提交结果。
- waiting_for_source 与三个 listener stale-after 可被生产故障注入明确区分。
- comment source/revision 重复采集不重复义务；本地 reply 缺失时远端存在/删除/unknown 精确分态，reply/direct 合同按配置执行。
- C8 探针候选池每次随机选取；权限/未入群/Session 终态失败仅对本次池 `-1` 并保存有时效的 typed fact，下一 run 过滤仍新鲜事实；网络 unknown 不 `-1` 但本轮继续其他账号，池耗尽不得判定远端删除；账号入群晚于来源或 history gap 时不得把不可见误判为删除。
- C8 负面 probe 与 listener 正向 resync 并发时，旧负面结果必须因 source state/cursor CAS 失败，不得覆盖新事实。

### 14.6 E4

每类任务独立验收完整链：

```text
义务 → 资格/准入 → Action → ExecutionAttempt → 任务专用远端事实 → 目标完成
```

- AI/评论：非空 `remote_message_id`，AI 还需 visible confirmed 和账号覆盖一致；
- 浏览/点赞：对应 typed remote fact；
- 搜索：`target_click_observed=true`；
- runtime healthy、Actions green、Action success 均不能替代上述事实。

## 15. 发布、回滚与顺序

发布拆成“可独立部署但未激活的组件 batch”和“依赖闭合后统一激活并做端到端 E4 的 release train”，不能要求上游组件在下游尚未部署时先通过完整任务 E4：

1. 基础 batch：新 schema、policy、writer fence、lifecycle、delete operation、最小 remote tombstone；仅做 schema/fence/delete 组件验收；
2. AI release train：C7 Recovery + C3 义务投影 + C1 动态范围/任务日到期量 + C2 Task 独立准入 + C5 AI 并发/单 Provider key；全部组件定向 QA 通过后一次激活该 train，再做 AI 完整自然日 E4；
3. Search release train：search/interaction 通道隔离 + assignment 直接执行 + 双 OCR 无 AI；两项共同激活后再做搜索完整 E4，不能让搜索通道先等待尚未发布的 OCR；
4. Source release train：C8 随机探针、负面结果 CAS、来源状态和页面读模型；定向 QA 后做评论/点赞/浏览 source-window E4；
5. 每个 train 内组件可分提交、分部署为 inactive；先创建 prepared 新 Task并用其中一个真实 Task直接执行 canary，取得完整远端事实链后 CAS route epoch，再异步删除该 train 的精确旧 Task 集合。canary 不做容量或吞吐计算，任何组件不做同 Task shadow 双写。

每个组件 batch 经 `master -> release -> Deploy Production` 后先做 component acceptance；同一 train 依赖全部到位后，prepared canary 直接执行并产生端到端 remote fact，再由唯一 route epoch 一次切换旧/new Task 写资格，随后删除旧合同 Task。不存在旧字段双写或同 Task新旧双写；新 writer产生远端副作用后只能前向修复。

## 16. Product Design Complete 自检

| 检查项 | 状态 |
| --- | --- |
| 用户原始需求与 Annotation 术语 | complete |
| C1–C8 状态机、fencing、并发、恢复与 E4 | complete；以闭合专项 PRD 为准 |
| 动态目标缩小与超发归因 | complete；稳定主义务、单义务 Gateway CAS和唯一远端事实取代 completion ordinal/账本预算锁，pre-accept 释放同一义务 |
| 物理删除崩溃一致性 | complete；持久 stage/item/checkpoint、tombstone_verified 前零删除、failed 同 snapshot resume、同 operation 重放不扩集 |
| C2 ready / C8 negative 并发竞争 | complete；均绑定事实版本/hash/cursor 做 CAS，迟到结论拒绝 |
| Gateway unknown 重开证据 | complete；只有同 request identity 的 pre-transport/pre-accept negative 可重开，远端“当前不存在/页面不变”不是未执行证明 |
| Provider 同 key RPM/TPM/最大并发 | contract_complete；只允许一个 active Provider key version，多模型共享其真实额度，rotation 与旧 usage 已定义 |
| 独立 search/interaction worker 热查询 | contract_complete；partial index、当前空闲槽有界 batch、EXPLAIN/延迟/零 deadlock 标准已定义，性能不参与业务量计算 |
| C2 连续 30 秒无提示通过 | complete；绑定 target-group surface、viewer authorization、listener epoch、DB 时间和连续 cursor，29 秒/断流/surface 变化不得通过 |
| Telegram 无法发送统一放弃 | complete；Session/权限权威事实按授权槽复用、按 Task 日独立物化，目标解散终结目标，unknown 不误判 |
| Search 唤醒与页面状态持久化 | complete；assignment 是持久工作，通知可丢，所有 hot-list/category/result/challenge/click phase 先 CAS 后前进 |
| 无 Reservation 的最快并行执行 | complete；四类真实阶段槽 JIT 领取，多 Task 同时推进，不创建份额、Window、预扣或中央 Reservation |
| 四类拟人任务节奏 | complete；AI 活群、频道评论、频道点赞、频道浏览只领取当前累计到期义务，partial_start/晚采集来源从 anchor=0 起算，完整 24 小时分母不被剩余窗口替换，future Action 不被全局提前 |
| 新建/切换/删除顺序 | complete；prepared 新 Task先创建，真实 canary 事实链后 CAS route epoch，新 Task从 0运行再异步删除旧 Task |
| ordinal、Provider、签到与 tombstone 统一 | complete；义务 UUID 可释放重领、单 active key、多模型共享、direct 签到任务日唯一、只留最小远端防重事实 |
| Recovery 权威时钟 | complete；lease/heartbeat takeover 只认同事务 PostgreSQL clock_timestamp() 与 UTC-aware timestamptz |
| 永久 unknown 运营终态 | complete；deadline 后转 remote_reconcile_only/closed_with_unknown_shortfall，释放槽位、保留 tombstone且禁止强制成功或重发 |
| 旧合同处理 | complete；全部旧 Task 不迁移，route 先切到 prepared 新 Task，再保留最小远端防重事实并物理删除旧 Task |
| 组件发布与端到端 E4 依赖 | complete；组件可部署为 inactive，canary 只证明远端事实链、不做容量预测；最终 production_fixed 仍只认各任务真实 E4 |
| Product Design Complete | `complete`；`dev_handoff_ready=true`，尚未表示 dev、QA、发布或生产完成 |

## 17. Product Handoff（已生效）

Dev 必须按 release batch 拆分实现；本次节奏修复覆盖 `channel_comment/channel_view/channel_like/group_ai_chat` 的 future Action、累计 due、partial-start/晚采集来源、完整 24 小时分母、同批排期和 next-run 红测，并保持单函数、单文件和复杂度限制。主 PRD、主数据流已同步产品合同；代码与 QA 稳定后只按真实入口更新项目结构索引，并对实现造成的数据流差异做 resync。QA 必须提供真实 PostgreSQL并发、故障注入、完整相关回归、静态检查和旧 Task删除 preview。Product 只验收需求与状态合同；最终 `production_fixed` 只能由发布后的完整任务窗口任务专用远端 E4 证据给出。

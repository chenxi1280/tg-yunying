# AI 活群仅引用我方消息专项设计

| 项目 | 结论 |
|---|---|
| 需求日期 | 2026-08-06 |
| 需求级别 | L2：生产 AI 活群行为变更 |
| 适用范围 | `group_ai_chat` 引用回复 |
| 不适用范围 | 普通 AI 活群发言、频道评论 / 回复、群管机器人控制消息 |
| 设计状态 | `design_status=complete` |
| 开发交接 | `dev_handoff_ready=true` |
| 发布状态 | 2026-08-13 性能修复已发布；AI 发送与一条有源频道浏览取得发布后 E4，另一条无源任务保持 `production_unproven` |

> **2026-09-03 unified route 适用性补正：** 本文“仅引用我方消息”继续约束 legacy own-history reply pool 及其已发布查询/事实边界，不再限制统一互动履约引擎。unified `group_ai_chat` 允许 `semantic_direct|native_reply_external_human|native_reply_owned_fact`：external-human 必须由 canonical `ConversationEvent` 的 exact peer/thread/topic/message/revision、`author_class=external_human`、current 未删除和闭合 watermark 授权；owned 仍按本文 bound typed fact 边界授权。每个 response binding 的 `ConversationReplyAuthorityDecision` 由 Provider、Action 与 Gateway 共同复核；原始 `GroupContextMessage`、昵称、正文或 Action.result 仍不能授权。完整 current 合同见 `unified-engagement-fulfillment-engine-prd.md` §8.4 与 `ai-group-dual-lane-send-chain-redesign-prd.md` §6.5；当前仅设计，未实施、未发布。

## 0. 2026-08-13 生产查询压力事故补充

### 0.1 Incident / 分级

- `incident_id`: `incident-ai-own-history-query-pressure-20260813`
- `level`: L3
- `symptom`: AI 活群、浏览及新建任务吞吐下降，API/SSH 间歇超时；重启后再次出现高负载。
- `first_broken_boundary`: 4 vCPU 生产主机上的 PostgreSQL/Planner CPU 饱和，而非 Telegram Gateway、AI Provider、OOM、磁盘满或数据库锁链。
- `observed_query`: `successful_own_history_reply_facts()` 在一个已有 4,349 条成功 Action 的任务上，对候选 Action 重复执行最新成功 Attempt 相关子查询；Planner、Generation guard 与 Gateway scope guard 被多个 worker 并发放大。
- `production_anchor`: `14bebb2b15d5d1391ac05d9ee3307a16e5e28a16`；所有数值均为 2026-08-13 重启后只读快照，发布后必须重查。

### 0.2 不变的产品语义

本次只修复查询边界，不改变引用来源、reply 数量、任务排期或失败语义。候选仍必须同时满足同 tenant、同 Task、同群、成功 Action、非空冻结正文、最新成功 Attempt 和非空远端消息 ID；跨 Task 在途占用仍须在 limit 前排除，Provider 前与 Gateway 前仍使用同一权威合同。禁止以 `Action.result`、Listener 上下文、缩小业务目标、减少 worker 可见任务或跳过引用校验换取性能。

### 0.3 查询与并发合同

1. Planner 选池必须先以 `tenant_id + task_id + group_id + success` 缩小 Action，再一次性关联每个候选的最新成功 Attempt；同一 Attempt 结果不能在 SELECT、过滤和占用排除中重复计算。
2. Generation/Gateway 已持有冻结 `reply_to_message_id` 时，必须先由 `ExecutionAttempt.remote_message_id` 精确命中，再反查并验证所属 Action；禁止从任务全部成功 Action 开始扫描。
3. `execution_attempts` 必须具有与精确事实查找一致的部分索引：`remote_message_id` 非空且 `status=success`，覆盖 `action_id/attempt_no`。迁移在 PostgreSQL 使用 concurrent + idempotent 创建，发布不可长时间锁写表。
4. 批量候选查询最多返回配置 limit；精确 guard 最多读取一条权威 Action。数据库执行计划不得退化为全表 Action/Attempt 顺序扫描。
5. 不新增缓存、静默降级或兜底成功；索引/查询异常必须显式失败并留日志。

### 0.4 验收与发布闸门

- E2：原 own-history 语义回归全部通过；新增 PostgreSQL 生产规模测试，证明精确 guard 使用 Attempt remote-id 索引、选池有界，且跨 Task/群/缺 Attempt/空正文/在途占用行为不变。
- E3：正式 `master -> release -> GitHub Actions` 发布；生产 SHA、迁移 head、容器身份和健康检查一致；PostgreSQL/Planner 连续采样不再在无业务突发时压满 4 核，同 SQL 不再批量长驻。
- E4：发布锚点后，受影响 AI 任务出现新的成功 Attempt 与 canonical send remote fact/quantity binding；频道浏览分别检查 `ViewRemoteFact` 和 `remote_fact_gap`。任何一个任务没有类型化远端事实时只能写 `production_unproven/failed`，不能以负载回落代替。
- rollback：迁移只新增索引，可前向保留；应用可回到上一不可变 release，但若回退会恢复高压查询，只能作为短时故障隔离，不能作为最终修复。不得删除 Action/Attempt/远端事实。

### 0.5 Product Design Complete 补充自检

| 检查项 | 结论 |
|---|---|
| 原始症状与根因 | 已覆盖重启后复发、AI/浏览共同变慢及第一故障边界 |
| 产品语义 | 明确完全不改变 own-history、reply、排期和 unknown 合同 |
| 后端/Worker/数据流 | 明确 Planner 批量查询、Generation/Gateway 精确 guard 与索引 |
| 并发/幂等/失败 | 多 worker 放大、concurrent migration、显式失败均已定义 |
| 安全与权限 | tenant/Task/group 硬隔离不放宽，不打印消息正文或凭据 |
| QA/发布/回滚 | E2/E3/E4、资源门、正式发布和不可伪造完成均已定义 |

补充结论：`design_status=complete`、`dev_handoff_ready=true`、`release_gate=required`。

### 0.6 2026-08-13 发布与生产验收

- `master`: `d52884be6e628771c939f7d1aa0f698cc6233f71`；`release/production`: `321cf61cbba68cb29bc031c26dd7109f67c206e5`；Deploy Production `31663929691` 成功。
- 生产迁移包含 `0146_ai_reply_remote_fact_index`；当前 head 为 `0147_login_challenge_binding`，索引 `ix_execution_attempts_success_remote` 的 `indisvalid/indisready` 均为 true。
- 发布后 AI 任务 `f2832260-c7ef-4ec2-8055-8ff5262b4734`、`7162e305-fb51-4a67-92ea-d0caffd2bbb3` 分别产生 12、5 条成功非空远端消息 ID；发送主链 `production_fixed`，当日目标缺口仍按业务履约单独保留。
- 有源频道浏览任务 `4fc393df-a258-4e1f-a3d1-e916d2c59361` 产生 64 条发布后 `ViewRemoteFact` 且 `remote_fact_gap=0`，执行链 `production_fixed`；目标 1000 高于当时约 947 个可参与账号，仍有结构性容量/当日物化缺口。
- `fa75ca69-2377-4282-80c1-20f66ebbd086` 当日 `source_message_count=0`、`required=materialized=confirmed=0`，没有发布后 typed fact；只能记 `production_unproven/waiting_for_source`，不能据此判定 worker 故障或修复完成。
- 重启后事故采样负载约 9；发布后 11:52 负载降至 2.84，CPU idle 54%–71%，Planner 退出高占用榜且无持续长驻查询。该资源证据只证明查询压力修复，不替代上述 typed E4。

## 1. Intake Card

- `intake_id`: `intake-ai-group-own-message-reply-20260806`
- `source`: user
- `raw_input`: “我们新的需求ai活群引用回复的 消息只引用回复我们的消息，不引用其他人的”
- `owner_agent`: product
- `suspected_type`: feature
- `affected_surface`: AI 活群 Planner、AI generation guard、Dispatcher Gateway guard、任务详情诊断
- `production_related`: true
- `initial_evidence_level`: E1（用户需求 + 当前代码静态证据）
- `next_route`: L2 standard，`product -> dev -> qa -> product -> release gate`

### 1.1 原始需求覆盖

用户要求 AI 活群的引用回复只能引用我方消息，不能引用群内其他人的消息。本专项中的“我方消息”固定定义为：

1. 与当前 Action 同 tenant、同 `group_ai_chat` Task、同目标群；
2. 由平台托管账号通过历史 `send_message` Action 成功发出；
3. 同时存在 `Action.status=success`、非空冻结正文，以及该 Action 最新 `ExecutionAttempt.status=success + remote_message_id`；
4. `Action.result` 中的本地字段、仅有 success 状态、监听消息文案或 sender 名称均不能单独证明我方归属。

该定义是严格的最小充分集合。其他 Task 即使也由平台运行，本期不进入当前 Task 的引用池，避免跨任务语境和归属混用；它仍满足“绝不引用他人”的上限要求。

## 2. 功能设计

### 2.1 正常路径

1. Planner 先确定本轮 Turn 和不可变 reply 槽。
2. reply 候选只查询当前 Task 的权威历史成功出站事实，结果标记 `reply_target_source=own_history`。
3. 在途 `pending|claiming|executing|unknown_after_send` 已占用的同一远端消息 ID 继续排除；已明确完成的历史引用不永久占用目标。
4. Action 冻结 `reply_to_message_id`、作者 / 预览和 `reply_target_source=own_history` 后进入 AI generation。
5. Provider 前和 Gateway 前都重新校验该消息仍能由同一权威 Action/Attempt 链证明，且当前账号仍具备目标群传输权限。
6. Telegram 远端精确读取仍需证明该消息存在且当前发送账号可引用；真实发送必须携带冻结的 `reply_to_message_id`。

### 2.2 普通群上下文

Listener 采集的其他成员消息继续用于：

- 判断实时话题、接话 / 暖场模式和上下文新鲜度；
- 为普通 direct 发言提供事实锚点；
- 触发既有真人消息打断、speaker rotation 和过期重生成规则。

Listener 消息不得进入 AI 活群 reply pool，也不得仅因同群存在 `GroupContextMessage` 就通过引用归属校验。普通上下文与 Telegram 原生引用目标是两个独立概念。

### 2.3 候选不足与失败路径

- `reply_min_per_round=0` 时不查询引用池，普通 direct 流程不变。
- 我方历史成功消息少于本 Cycle 所需 reply 数时，继续记录 `reply_target_shortfall_count`，对普通 Cycle 显示“我方可引用消息不足，等待本任务产生可引用消息”。
- 已有到期日覆盖债务时，保留现有显式 `coverage_reply_shortfall_cycle_count` 合同：本次创建 `reply_min_required_count=0` 的 direct coverage 回补 Cycle；任务配置不变，后续有我方候选的新 Cycle 仍执行 reply 要求。该路径不是引用回复，也不得携带他人 `reply_to_message_id`。
- 已排队但尚未进入 Gateway 的 `human_context` 引用 Action 在 Provider 前或 Gateway 前必须明确失败并让原 reply 槽进入重规划，不能原地改为 direct，也不能继续发送。
- Gateway 已开始、`unknown_after_send` 或 success 的历史 Action 不改写；它们按远端事实收口，不能伪造为满足新合同的新证据。
- 当前我方候选远端删除或不可访问时，沿既有 `reply_target_missing` 和同 reply 槽递增 attempt 重建合同处理；不得回选真人消息。

## 3. 前端与运营状态

- 不新增任务配置项，不改变 `reply_min_per_round` 表单和 API。
- 任务详情的引用不足文案应表达“我方可引用消息不足”，不能继续写成泛化的“等待监听到可回复消息”。
- 既有 `reply_target_source` 下钻中，AI 活群新 Action 只能出现 `own_history`；`human_context` 仅作为历史 Action 审计值展示，不代表仍允许发送。
- 空态、错误态必须区分：我方历史不足、本地归属不成立、远端目标缺失、当前账号不可访问。

## 4. 后端、数据流与一致性

### 4.1 影响入口

- Planner：`executors/group_ai_chat.py::_group_reply_target_pool`
- 权威归属查询：`group_ai_scope.py::successful_own_history_reply_facts`
- Provider 前与 Gateway 前 scope：`group_ai_scope.py::validate_group_ai_content_scope`
- 本地 reply guard：`ai_generation_guards.py::validate_local_reply_target`

### 4.2 数据模型与迁移

- 不新增表、字段或 API schema；新增 `ExecutionAttempt(remote_message_id, action_id, attempt_no DESC)` 成功非空部分索引，支撑精确远端事实反查。
- 复用 `Action`、`ExecutionAttempt`、`reply_to_message_id` 和 `reply_target_source`；迁移采用 PostgreSQL concurrent + idempotent 创建，避免阻塞出站写入。
- `GroupContextMessage` 继续保存上下文，但不再证明 AI 活群 reply 归属。

### 4.3 并发与幂等

- 候选查询继续先在数据库中排除跨 Task 在途占用，再做有界 limit；Action 创建前继续复核占用，避免并发重复冻结。
- 每个 Action attempt 的 `reply_to_message_id` 仍不可变；失效只能终结旧 Action，并在同 reply 槽创建递增 attempt。
- Gateway-started、unknown 和 success 不创建替代 Action。
- 双层 guard 使用同一权威查询合同，不能 Planner 允许一套、发送前再接受另一套。

### 4.4 权限与安全

- 同 tenant、同 Task、同群是硬隔离条件；跨租户、跨 Task、跨群全部拒绝。
- 当前发送账号仍必须有 `TgGroupAccount.can_send=true`，并通过准入、在线、授权代理、FloodWait / SlowMode 和 Gateway 最终检查。
- 不根据 sender_name、正文或历史 result 猜测归属，防止误把他人消息标为我方。

## 5. QA 验收口径

### 5.1 必测用例

1. 只有两条真人 `GroupContextMessage`、没有本 Task 权威成功出站时，reply pool 为空。
2. 同 Task、同群的成功 Action 但没有成功 Attempt 时，不进入 reply pool。
3. 同 Task、同群的成功 Action + 成功 Attempt + 远端消息 ID 时，进入 reply pool，source 为 `own_history`。
4. 跨 Task、跨群、跨 tenant 的成功出站均不进入当前池。
5. 真人消息与我方消息同时存在时，只选择我方消息。
6. 已排队 `human_context` reply Action 在 Provider 调用前失败，Provider 调用次数为 0；不得生成 `签到` 假装 reply。
7. 同一存量 Action 在 Gateway 前再次被 scope guard 拦截，Telegram Gateway 调用次数为 0。
8. 我方候选不足时，普通 Cycle 等待并展示我方不足；有日覆盖债务时只创建显式 direct coverage，所有 payload 的 `reply_to_message_id` 为空。
9. 在途去重、完成后可复用、远端目标失效重建、unknown 防重原回归保持通过。
10. 频道评论 reply pool 行为完全不变。

### 5.2 证据分层

- E2：定向单元 / 集成测试、diff check、代码静态检查。
- E3：发布版本、worker healthy、旧 pending human-context Action 未越过 Gateway。
- E4：新版本真实 AI 活群成功 Attempt 同时满足非空 `remote_message_id`、非空 `reply_to_message_id`，且被引用目标可反查为同 Task 历史成功 Attempt；抽样中不得出现 `human_context` 新引用成功。

## 6. 发布与回滚

- 需要 Release Gate；默认走 `master -> release -> GitHub Actions Deploy Production`。
- 发布前统计未进入 Gateway 的 `group_ai_chat` + `reply_target_source=human_context` Action 数，发布后确认它们被新 guard 拦截 / 重规划，没有继续产生 Telegram 出站。
- 回滚只回退应用版本，不删除历史 Action/Attempt，不改写远端事实，不重新开放真人引用。
- 如果回滚版本会恢复真人引用，必须暂停受影响 `group_ai_chat` Task，不能带着旧行为继续运行。

## 7. Product Design Complete 自检

| 检查项 | 结论 |
|---|---|
| 原始需求 | 已覆盖“只引用我们的消息、不引用其他人的” |
| 功能 / 前端 / 后端 / Worker | 已覆盖；无新配置和 API |
| 数据流与索引 | 总 PRD、专项设计、数据流索引同步 |
| 权限安全 | 同 tenant/Task/group + 权威 Attempt，禁止文本猜测 |
| 失败路径 | 候选不足、存量 pending、远端失效、unknown 均已定义 |
| 并发幂等 | 在途排重、attempt 不可变、Gateway 边界已定义 |
| 数据一致性 | 不迁移、不改历史远端事实，双层 guard 同合同 |
| QA / 发布 / 回滚 | 已定义 E2/E3/E4 与 Release Gate |
| 开放问题 | 无；跨 Task 我方消息复用不在本期范围 |

结论：`design_status=complete`、`dev_handoff_ready=true`、`resync=true`。开发必须先写红测，再最小修改 Planner 与双层发送前校验；QA 通过不等于已发布，发布成功不等于真实 Telegram E4 完成。

## 8. Product Handoff

- `message_id`: `2026-08-06-ai-group-own-message-reply-product-001`
- `intake_id`: `intake-ai-group-own-message-reply-20260806`
- `from_agent`: product
- `to_agent`: dev
- `message_type`: implement
- `level`: L2
- `evidence_level`: E1
- `ready_status`: ready
- `release_gate`: pending
- `locked_paths`: `docs/01-product/tg-ops-platform-prd.md`; `docs/03-feature-designs/ai-group-own-message-reply-prd.md`; `docs/00-index/project-dataflow-index.md`; `backend/app/services/task_center/executors/group_ai_chat.py`; `backend/app/services/task_center/group_ai_scope.py`; `backend/app/services/task_center/ai_generation_guards.py`; 定向 tests
- `merge_owner`: dev

开发成功标准：所有新 AI 活群 reply Action 的本地候选和发送前归属均只能由当前 Task 的权威成功 Action/Attempt 证明；监听到的他人消息可参与上下文生成，但不得成为 Telegram `reply_to_message_id`。

# AI 活群仅引用我方消息专项设计

| 项目 | 结论 |
|---|---|
| 需求日期 | 2026-08-06 |
| 需求级别 | L2：生产 AI 活群行为变更 |
| 适用范围 | `group_ai_chat` 引用回复 |
| 不适用范围 | 普通 AI 活群发言、频道评论 / 回复、群管机器人控制消息 |
| 设计状态 | `design_status=complete` |
| 开发交接 | `dev_handoff_ready=true` |
| 发布状态 | `release_gate=pending`，未发布、未取得 Telegram E4 |

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

- 不新增表、字段、索引或 API schema。
- 复用 `Action`、`ExecutionAttempt`、`reply_to_message_id` 和 `reply_target_source`。
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

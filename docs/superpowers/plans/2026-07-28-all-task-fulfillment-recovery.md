# 全任务按时按量恢复实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use `subagent-driven-development` or `executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 AI 活群、评论、点赞、浏览和搜索点击在保留目标/账号/内容/Telegram 安全边界的前提下，以真实远端事实按时按量履约。

**Architecture:** 创建阶段只持久化结构合法 Task，启动后建立不可变 task-day ledger、AI 主发送槽和 ContentMix Cycle；所有到期业务任务在单用户 Dispatcher scope 内按父任务 Claim Window 公平领取。Planner 只规划未完成义务，Dispatcher 通过主/备用 AI、确定性兜底、AI 群管准入和真实 Gateway 收口；纯搜索点击在真实 click 事实后直接终结 ordinal，Action/ExecutionAttempt/remote fact 为唯一完成证据。

**Tech Stack:** Python 3.12、FastAPI、SQLAlchemy、PostgreSQL、Redis、Telethon、React/TypeScript/Vite、Pytest、Alembic、GitHub Actions。

---

## 0. 状态与目标

| 项目 | 内容 |
| --- | --- |
| 需求级别 | L3 生产履约修复 |
| 产品设计 | `complete` |
| 实现状态 | `resync_required` |
| 范围 | AI 活群、频道评论、点赞、浏览、搜索点击 |
| 总目标 | 以真实 Telegram 完成为第一优先级，同时保留不可绕过的准入、安全与远端事实 |
| 完成边界 | 自动化 QA、release、runtime、Action/Attempt/remote fact 和完整自然日 E4 均通过 |

本计划执行以下产品真相源：

- `docs/03-feature-designs/all-task-fulfillment-recovery-prd.md`
- `docs/03-feature-designs/ai-group-daily-group-target-redesign-prd.md`
- `docs/03-feature-designs/ai-conversation-humanization-and-group-bot-admission-prd.md`
- `docs/03-feature-designs/search-click-daily-fulfillment-remediation-prd.md`
- `docs/01-product/tg-ops-platform-prd.md`

本计划不把本地测试、容器健康、Action 已创建、claim 成功或 AI Provider 健康当作生产恢复。

### 0.1 Dev 执行协议

本文是 product 已完成的 Product Handoff，不在本阶段写实现代码。Task 1-9 是开发包，必须逐包执行以下固定红绿顺序，不能先实现后补测试；Task 10 是 1-9 全部完成后的独立自动化/集成闸门，Task 11 只能在 Task 10 通过后执行 release 与生产 E4。三阶段均属于 Task 1-11 完成交接的一部分，不能只完成 1-9 就宣称计划完成：

1. 在本 Task 列出的测试文件中新增精确失败用例，断言本 Task 的状态名、唯一键、远端事实和并发不变量；
2. 使用下列 60 秒硬超时命令运行本 Task 的定向测试，预期先以目标断言失败；
3. 只修改本 Task “主要文件”列出的最小实现；若真实调用入口不同，先更新项目结构索引并在 Intake 标记 resync，不能静默扩大路径；
4. 重跑同一命令，预期全部 PASS；再运行相邻回归文件；
5. 运行 `git diff --check`、前端构建或 Alembic 检查；每个可独立回滚的 Task 单独提交，之后才进入下一 Task。

后端定向测试统一命令：

```bash
backend/.venv/bin/python -c 'import subprocess,sys; sys.exit(subprocess.run(sys.argv[1:], timeout=60).returncode)' backend/.venv/bin/python -m pytest <test-files> -q
```

PostgreSQL 并发测试在已配置 `TEST_DATABASE_URL` 的 CI/测试环境运行同一命令；未配置时只能标记 `blocked`，不能用 SQLite 结果替代。前端验证命令为 `npm --prefix frontend run build`；迁移验证至少运行 `backend/.venv/bin/alembic upgrade head` 和对应 migration test。每个 Task 的首个定向命令与最低断言如下：

| Task | 测试文件 | 首轮必须失败/末轮必须通过的核心断言 |
| --- | --- | --- |
| 1 | `backend/tests/test_all_task_fulfillment_contract.py`、`backend/tests/test_content_mix_cycle.py`、`backend/tests/test_task_creation_idempotency.py` | 非 AI 稳定天然键/attempt 幂等；AI 一条远端消息只消费一个主槽；`pending_visibility`/unknown 各占位 1 且不计 confirmed；可见确认原子；Cycle/Slot 原子；同键不同 fingerprint 409、start operation 不重复、启动结果与 runtime waiting 分离 |
| 2 | `backend/tests/test_dispatch_fairness.py`、`backend/tests/test_dispatch_claim_reservations.py`、`backend/tests/test_single_user_dispatch_scope.py` | scope 容量守恒、跨 shard 父任务只获一次最低份额、逐 epoch allocation/reservation 不改绑、完整 immutable rebuild input/hash、SERIALIZABLE `rebuild_required -> ready` 整批发布、无 TenantAllocation、无固定任务类型饥饿 |
| 3 | `backend/tests/test_task_creation_runtime_boundary.py`、`backend/tests/test_group_ai_send_limits.py` | 零 ready/容量不足仍创建；启动后显示 blocker；hard-hourly/活动窗口/本地群 gate 不再阻断 |
| 4 | `backend/tests/test_ai_group_daily_group_target.py`、`backend/tests/test_ai_group_daily_coverage_planner.py`、`backend/tests/test_task_account_daily_coverage.py` | 群日目标、冻结账号全覆盖、coverage/extra 分槽、静默非零权重、时区 ledger 连续 |
| 5 | `backend/tests/test_ai_generation_phase_boundaries.py`、`backend/tests/test_ai_generation_quality_pipeline.py`、`backend/tests/test_ai_generation_material_policy.py`、新增 `backend/tests/test_ai_post_send_visibility.py` | 主 3/备用 3/签到、reply attempt 不改写、Gateway 前素材兼容/转派、素材义务不被兜底吞掉、共享 AdmissionLease 单执行；intercepted/unknown/visible-confirmed 与 abandon 权限、占位、分母、重入边界 |
| 6 | `backend/tests/test_channel_comment_dispatch_generation.py`、`backend/tests/test_channel_comment_planner_boundaries.py`、`backend/tests/test_channel_comment_rule_snapshots.py` | 逐消息目标不被 lifetime cap 截断、active 面具快照、评论单表情、reply/素材合同不回归 |
| 7 | `backend/tests/test_search_join_completion_first.py`、`backend/tests/test_search_join_opportunity_assignments.py`、`backend/tests/test_search_join_group_config.py`、`backend/tests/test_permission_vocabulary.py`、新增 `backend/tests/test_search_click_only_contract.py`、`backend/tests/test_search_legacy_mode_isolation.py`、`backend/tests/test_jisou_no_reset.py` | 固定 `task_type=search_click + click_only`、拒绝 admission 字段、`tasks.manage + tasks.create.search_click` 双权限、稳定 click ordinal、两类 solver/epoch 边界、首次 outcome 与 post-finalize `DispatchAllocationReleaseBatch`、unit-level 永久 exclusion、单 pending rebuild wave、完整 rebuild hash、rehash-to-commit 竞态、contract version 禁止新旧 Dispatcher 混跑、已结束 Window 不重建、carrier 联合保留、权重原子发布、验证码 required/solved/failed 实际状态、完整 click 证据、点击后无 child、系统账号排序、hot-list/unknown 零 reset、legacy mixed 不被改写 |
| 8 | `backend/tests/test_task_center_view_dataflow.py`、`backend/tests/test_frontend_permission_gating.py`，随后 `npm --prefix frontend run build` | 创建页无容量确认；权限编辑器可配置纯搜索点击专项权限；无专项权限时其他四类仍可创建且纯搜索点击不可选；详情分列 quantity/content/admission/catch-up；TypeScript 与 Vite 构建通过 |
| 9 | `backend/tests/test_ai_group_daily_target_migration.py`、`backend/tests/test_channel_comment_history_migration.py`、新增专项 migration tests | dry-run 逐项审计；旧事实不改绑；存量 click+membership 仅标记 `legacy_mixed_search_join`，不自动迁为纯点击或未来加入模式；时区区间连续；历史 reset/unknown/success 只读 |
| 10 | Task 1-9 全部定向测试、PostgreSQL 并发套件、完整后端回归、前端测试/构建、Alembic upgrade/downgrade 检查 | 文档/schema/API/迁移/UI/runtime 状态一致；不得用 SQLite 跳过并发，不得用部分绿测替代自动化闸门 |
| 11 | GitHub Actions `Deploy Production`、runtime health、生产 ledger/Action/Attempt/remote fact 查询和完整自然日 E4 | release、worker、真实 Telegram 五类任务目标与内容构成同时满足；任一证据缺失保持 blocked/unproven |

## 1. 最终产品合同

### 1.0 创建与运行边界

- 结构合法的任务直接创建成功，不执行容量、准入、传输、协议或风险预检，也不要求 warning 确认。
- “创建并启动”必须先提交 Task；随后建立任务日 ledger、冻结运行范围并计算 blocker。运行 blocker 不得回滚已经创建的 Task。
- 创建阶段只允许因调用者授权失败（403）、不可见引用（不泄露存在性的 404）、必填/当前用户静态引用/类型/数量内容合同非法（422）或幂等键冲突（409）返回 4xx；合法引用中的具体账号在运行中删除、用途变化、授权资产漂移或身份失效只形成账号级 blocker。
- 容量不足、待审批、临时不可发、代理/授权暂不可用、协议/CAPTCHA 状态和 AI Provider 可用性均在运行期持续重算。

### 1.1 AI 活群

| 维度 | 最终合同 |
| --- | --- |
| 日目标 | 每个目标群配置一个 `daily_message_target` |
| 全账号 | `effective_daily_target = max(daily_message_target, frozen_account_count)`；冻结账号每人至少确认 1 条 |
| 日容量门禁 | 删除 |
| 硬小时目标 | 删除字段、规划、补偿、统计和发布门 |
| 活动时段门禁 | 删除 |
| 静默时间 | 使用非零低权重，只减少发送量 |
| 群本地日限额/冷却 | 不再阻断 AI 规划；Telegram SlowMode/FloodWait 仍是真实限制 |
| 保留业务门禁 | 目标群准入、正常内容质量 |
| 正常内容 | 绑定账号 active 面具；同账号滚动 10 天质量检查；主 AI 最多 3 轮，备用 AI 最多 3 轮 |
| 内容编排 | 每个逻辑 Cycle 原子建立 `ContentMixCycle`、全部 `ContentMixCycleSlot` 与不可变 `ContentMixContract`；保留 `reply_min_per_round`、direct/reply、每个 Action attempt 的 `reply_to_message_id` 快照、账号面具 emoji 习惯，以及既有正常文本 emoji、图片/表情包/custom emoji 比例与规则；技术切批和门禁删除不得重算 |
| 强制兜底 | 缺面具或已验证授权代理路线切换时直接签到；主/备用六轮无候选时发送精确 `签到` |
| 无传输路线 | `waiting_transport`，不直连、不假成功 |
| 完成事实 | 成功 Action + 成功 Attempt + 非空远端消息 ID + 对应群日/账号义务 |

`签到` 不参加普通正文的面具匹配和 10 天语义去重，但必须：

- 绑定原 `task/group/account/date/obligation`；
- 保留原 direct/reply、当前 attempt 的有效 `reply_to_message_id` 和已冻结素材义务/占比审计；是否在兜底 payload 共载原素材必须按兼容矩阵决定，不兼容时在 Gateway 前先 CAS 转派。引用对象 Gateway 前失效时只在同一 reply 槽以新 attempt 换合法对象，旧快照不改写；未实际携带原素材时不能消费素材配额；
- 保存 `fallback_reason` 和实际授权/代理路线；
- 继续经过目标准入、账号用途、会话轮换、敏感内容与 Telegram 真实限制；
- 可同时计入账号覆盖和群日总量；
- 遇到 `unknown_after_send` 时占住原义务，禁止创建替代发送。

缺面具发生在正常素材意图产生之前时，该槽位默认是 fallback-only，不虚构 selector/normal 素材 planned/success；兜底前冻结的 `scope_total_slots` 与显式比例分母保持不变。若 `policy_min` 或已经形成的 `selector_plan` 素材 `ContentMixObligation` 分配给该槽位，则必须在签到 outbound payload 冻结前通过版本化 CAS 转给同一 Cycle 尚未进入 Gateway 的正常槽位；转派与 fallback Action 固化同一短事务完成，禁止发送后补账。没有合法槽位时记录 `material_mix_shortfall`，不得超出群日总量补发。

### 1.2 AI 活群目标群共享准入

AI 活群账号使用同一状态机；当前纯搜索点击不进入该状态机，后续“搜索点击加入”模式本次不设计：

```text
membership_probe
  -> join_target_group
  -> observe_trusted_group_bot_prompt
  -> follow_required_channels
  -> execute_exact_confirmation_or_challenge
  -> membership_and_can_send_reprobe
  -> admission_ready
```

硬边界：

- membership 只是中间事实；
- “已关注频道”不能单独等于准入完成；
- callback、验证码、确认消息必须绑定正确账号、目标群和当前 admission 世代；
- 未 ready 只阻塞该账号，其他 ready 账号继续；
- 准入成功后的首条消息需要真实可见性事实；
- 目标群解散、身份不合法或 Telegram 明确禁止时显式阻塞，不能签到绕过。

### 1.3 频道评论

正常评论在 Phase A 冻结该账号 active 面具；找不到 active 面具才构成“缺面具”。正常评论仍走上下文、内容政策和质量流程，先由主 AI 最多 3 轮生成，再由不同的备用 AI 最多 3 轮生成。以下场景转为单个 Unicode 表情文本：

- 缺面具；
- 授权/代理异常后已经切换到可用路线；
- 主/备用六轮均没有可用候选。

固定池为 `👍 / 🙂 / 👏`，按稳定键轮换。兜底仍是 `post_comment`，不是 reaction：

- 回复评论必须保留原 `reply_to_message_id`；
- 单表情只替换正文，不得重算 `reply_min_per_message` 或消费既有图片/表情包素材配额；
- 引用目标失效时显式失败，不能降级为普通评论；
- 只有非空远端评论 ID 才完成；
- 无传输路线进入 `waiting_transport`；
- 表情兜底不可关闭。

每条频道消息的首次规划建立不可变 `comment_plan_revision` 与 `ContentMixContract`；后续补差额、数据库切批和失败重领复用该 revision。已分配素材义务的评论决定转纯文本表情时，必须在 outbound payload 冻结前按 AI 活群相同的同作用域 CAS 规则转派；无合法槽位时报告内容短缺，不得追加超出逐消息目标的评论。

### 1.4 搜索点击

当前新建任务固定为纯点击模式：

```text
search_execution_mode: click_only
daily_click_target_count: int
```

- 唯一数量目标是 `daily_click_target_count`。流程只包含搜索、分页、精确目标匹配和批准点击；同一 ExecutionAttempt 的 `target_click_observed` 成立后，该 `click_obligation_ordinal` 终结。
- 纯点击请求携带 `join_target_group_after_click`、`daily_admission_target_count`、成员目标或任何 admission 字段时返回 `422 field_not_allowed_for_click_only`；不得创建 membership、join、follow、confirm、challenge 或 can-send child，`admission_lane_claims` 恒为 0。
- `search_execution_mode` 创建后不可修改；模式、任务类型与 admission 字段不能通过编辑接口互相转换。
- Planner 冻结 `task_day_ledger_id + target_id + click_obligation_ordinal`。当前 Window commit 创建 source Action 并固化义务本地日、时区 revision、UTC period 和 deadline；明确失败且无 click 事实的 replacement 复用原 ordinal，open/unknown 继续占位防重。
- 候选保留全部满足硬安全条件的合法账号/关键词/授权/代理路径。`SearchClickAssignmentSolver` 依次固定最大 click assignment 数、最大受服务到期任务数、按冻结剩余 click 比例的最大最小公平向量和稳定 path 顺序；不建立 admission distinct/budget 目标，也不重新分配中央份额。
- 账号安全余量、当日已确认 click 数、最久未获机会和持久账号 cursor 只在前述完成量与跨 Task 公平最优值不下降时决胜，运营不配置容量或账号优先级；合法 repeat 可以补量，但不能绕过硬安全额度。
- 曲线、`actions_per_round`、skip、jitter 和静默都是软节奏；系统按最晚安全启动时间倒排，越过前进入 catch-up，把 due 提升到完整日目标、压缩 jitter、忽略行为 skip，静默保持非零低权重。
- 账号/关键词安全额度、授权身份、代理、协议/CAPTCHA、精确目标匹配、Gateway/Dispatcher 安全容量、deadline 和 unknown 防重仍是硬边界。
- “搜索点击加入”只登记为后续独立模式 `click_and_join`，`design_status=not_started`；本计划不定义或实现其字段、状态机、账号选择、准入目标、迁移、API、QA 或发布合同。
- 存量包含 click 与 membership/admission 行为的任务仅标记 `legacy_mixed_search_join`，保持 Task、Action、Attempt 与远端事实不变；不自动迁为 `click_only|click_and_join`，也不能从纯点击创建/编辑入口改写。

### 1.5 点赞与浏览

- 目标粒度保持“每条远端消息”的目标量。
- 节奏模板窗口是完成截止时间，不能被 24 小时曲线扩张。
- reaction unavailable 不计成功，也不永久消耗该账号的有效成功额度。
- 浏览的任务级安全上限不能静默截断每消息目标。
- 结构性配置冲突在保存时拒绝；外部实时容量不足展示 `at_risk/blocked`，不创建超出安全协议的 Action。

## 2. 共用履约与事务地基

### Task 1：固定统一事实合同

主要文件：

- `backend/app/services/task_center/daily_fulfillment.py`
- `backend/app/services/task_center/service.py`
- `backend/app/services/task_center/dispatcher.py`
- `backend/app/models/task_center.py`
- `backend/app/schemas/task_center.py`

- [ ] 为每类任务投影 `target/confirmed/open/unknown/remaining/deadline/blockers`；AI/评论同时投影 `quantity_status/content_mix_status/acceptance_status`，兼容 `status=acceptance_status`。
- [ ] `confirmed` 只从真实 Action、ExecutionAttempt 和远端 ID 派生。
- [ ] `primary_quantity_slot_id` 只用于 AI 群日主发送槽：AI 单条消息群日总量只加 1，同时可原子完成同 Action 绑定的唯一账号 coverage 和内容子维度，禁止拆成两条发送或覆盖两个账号。
- [ ] 评论、点赞、浏览和 click 不新增通用 quantity slot，也不强制绑定 `primary_quantity_slot_id`：评论使用 `(task_id,channel_message_id,comment_plan_revision,target_ordinal)` 并递增 `comment_action_attempt_no`；点赞在消息首次纳入时冻结 `reaction_contract_version`，同 task/message/account 同时最多一个 active version；浏览 `view_source_key=account:{account_id}`，Session/代理变化不得形成新 source；click 使用 `(task_day_ledger_id,target_id,click_obligation_ordinal)`，ordinal 在 ledger 锁内从 `1..daily_click_target_snapshot` 稳定取得。数据库 partial unique 保证同一键在 `pending|claiming|executing|unknown_after_send|success` 中最多一条当前 Action；replacement 先终结旧 current Action再复用原键，`source_action_id/ExecutionAttempt` 只作执行来源；重复 finalize 回读原结果。评论/点赞不得虚构任务日，浏览/click 不得跨 ledger 重复计数。
- [ ] 增加类型专用远端事实所有权：评论 remote ID 全局唯一绑定一个 ordinal；同账号/消息的未变化 reaction state 与 lifetime view fact 各只能完成一个 Task 义务；click evidence hash 只绑定一个 Attempt/ordinal。事实发生早于 Task/ledger 义务起点时不得倒灌，归属冲突只隔离受影响对象并写 `remote_fact_owned_elsewhere`，不得阻塞其他独立义务。
- [ ] 新增不可变 `TaskGroupDailyMessageSlot`：ledger 冻结时原子创建每账号一个 coverage 槽和固定 extra-volume 槽，Action/Attempt 绑定槽 ID；同槽同时最多一个 open/unknown/success，pre-Gateway terminal attempt 可历史并存。
- [ ] 统一 post-Gateway 未确认占位：业务/API/UI/指标使用 `pending_visibility_hold`；现有 `PendingVisibilityCredit/pending_visibility_credits` 只保留为兼容物理名，绝不表示 credit/success。`pending_visibility_count + unknown_after_send_count` 组成 `post_gateway_unconfirmed_hold_count`，兼容投影为 `unknown_after_send_hold_count`；每个 Action/主槽只占 1，不能同时计入 `held_count`，公式不新增第三项。
- [ ] `pending_visibility_hold` 以 `action_id` 唯一并绑定 `task_day_ledger_id + primary_quantity_slot_id + optional coverage_id + remote_message_id + admission_version`；同一 `primary_quantity_slot_id` 以 partial unique 保证同时最多一个 open/unknown hold。重复 Recovery/多 worker 只回读原 hold；主槽存在 pending visibility 或 unknown 时禁止建立替代 Action。
- [ ] `unknown_after_send` 与 `pending_visibility` 均不计完成，也不自动重发。需要可见性核验时，即使 Attempt 已有 non-empty remote id，Action 也只能进入 `pending_visibility`。
- [ ] `visible_confirmed` 在一个短事务内锁定 hold、Action、主槽、可选 coverage 与远端事实所有权，幂等关闭 hold、Action 转 success、群日 confirmed 精确 `+1`，并至多确认该 Action 绑定的一个 coverage。唯一键或 CAS 冲突整项回滚并仅隔离该对象，禁止半确认。
- [ ] 任务完成状态不再由 lifetime Action 数或旧 `Task.stats` 推断。
- [ ] 作用域创建时把已发布 RuleSet 解析成不可变 `ContentMixContract`；“至少”用 ceil、“最多”用 floor、多个精确比例用稳定最大余数法。创建/编辑时非法合同映射 `task_contract_invalid` 返回 422；运行时可恢复分配错误写 `content_contract_replan_required`，只有版本确实不可重放才写闭集内 `content_contract_unreplayable`。
- [ ] AI Planner 在一个短事务中创建 `ContentMixCycle`、全部 `ContentMixCycleSlot`、合同与 `policy_min`，固化 `allocation_closed_at`；Action 每批最多 20 条并以 `(cycle_slot_id,slot_attempt)` 唯一，CycleSlot 用 `current_action_id/slot_state` 指向当前尝试。同 attempt 重放幂等，只有 pre-Gateway 明确终态可递增 attempt；失败恢复、worker 重启、静默降量和六轮生成均复用同一 cycle。物化状态按已有 Action 历史重算，首次 complete 后不回退。
- [ ] Cycle 仅在全部槽位 confirmed/terminal、无 pending/gateway_started/unknown/replan_required 且数量/内容义务均结算后进入 settled 并写 met/shortfall/missed outcome；deadline 后未物化/replan_required/未进 Gateway 槽明确终结，unknown 保持 open。配置 revision 只影响新 Cycle，settled 不重开；评论使用 `comment_plan_revision` 的同等冻结/结算语义，不创建伪 Cycle。
- [ ] 每个需要保留的 `normal_text_emoji|image|sticker|custom_emoji` 槽位建立唯一 `ContentMixObligation(scope, mix_kind, obligation_source, ordinal)`；`policy_min` 在合同建立时复用旧 RuleSet 槽位算法和同一 seed 先绑定逻辑 slot，selector 在该槽只选同 kind 资产；无最低绑定槽位只有在 Phase C 接受正常候选时才新增 `selector_plan`，被拒绝候选只留 trace，迁移/重放时同槽同 kind 不重复。义务以 `assigned_action_id + assignment_version` CAS 指向当前 Action；普通重建沿用 slot。确定性兜底在冻结 outbound payload 前按兼容矩阵处理：normal-text-emoji/sticker/当前 custom-emoji 必须先释放并转派，image 只有已批准 Gateway profile 可证明共载时留原槽；转派和 fallback Action 固化同一短事务完成，禁止发送后补账。
- [ ] Action 固化 `relation_kind/reply_to_message_id/content_mix_scope_key/content_contract_version/material_policy_rule_set_version/planned_material_kind/planned_normal_text_emoji/content_source`；planned 字段从 `unresolved` 经既有选择器解析后不可变。
- [ ] `content_mix_slot_key` 允许历史 terminal attempt，但同时最多一个 open/unknown/success；pre-Gateway 失败释放发送占位但不吞掉最低内容义务，unknown/success 阻止替代，成功按远端 reply/media/normal-text-emoji 类型确认。
- [ ] 统一读模型增加 `due_target_count/planning_deficit_count/quantity_overflow_count/open_excess_count`；目标达成后终结 pre-Gateway excess，Gateway-started/unknown 只核验不补发，超发事实不删除或跨日抵消。
- [ ] 增加 `late_confirmed_count/failed_attempt_count/remote_confirmed_at`；普通 updated/reconcile 时间不能证明 deadline 内成功，时间不可证进入 unknown。
- [ ] reply 目标在 Gateway 前失效时只以同一逻辑 slot、相同 reply 关系、递增 `slot_attempt` 和新合法引用对象重建；不得降级 direct 或超量，unknown 不重建。
- [ ] `reply_to_message_id` 是 Action attempt 的不可变快照，不是 CycleSlot 可原地改写字段；重建只释放旧 Action 的 runtime claim/发送占位，主发送槽、coverage、reply 关系和内容义务继续绑定原 CycleSlot。
- [ ] Task 持久化 `created_by_user_id/create_task_type/client_request_id/request_fingerprint`，全表包含 soft-deleted 行保持创建幂等唯一；同键同 fingerprint 回读，不同 fingerprint 返回 `409 idempotency_key_reused`。新增每 Task 0..1 条当前 `TaskStartOperation(task_id UNIQUE,start_operation_id,operation_version,source,status,ledger_id,failure_code)`，`status=processing|started|failed`；新合同下真实 start/create-and-start 必须建立当前行，`operation_version` 只是当前行单调并发栅栏，不保留历史 payload。same key 把旧 `failed` 覆盖为 `processing` 时 version 加 1。new key 重试失败启动或重启 stopped Task 必须提交 `replaces_start_operation_id/version=current`，在 Task -> StartOperation 行锁内 tuple CAS 后整体覆盖当前行并推进 version；CAS 不等返回 `stale_start_operation`，其他请求 processing 返回 `start_in_progress`。成功后只保留 `started` 并清空 failure，不建立 `TaskStartAttempt`、失败版本 payload或旧 operation 行。可变 `runtime_state=runnable|waiting` 与启动结果分离。事务 B 失败整体回滚运行写入和本轮 version；独立短事务写 failed 时必须使用 B 开始时冻结的 expected previous ID/version tuple，重新按 Task -> StartOperation 加锁并 CAS，仅在 Task 仍为 draft/stopped 且 current tuple 未变化时写本轮 failed/version。current 已被 same/new key 重试推进或 Task 已 running 时只能回读，绝不能覆盖新的 processing/started。首次 201/重放 200，已提交但响应丢失时任何 key 只回读既有 ledger/Cycle。same/new key 并发最多建立一份 ledger。发布前已 running/paused 的存量 Task 不补造 operation，只投影 null ID/version + `start_operation_legacy_untracked=true` 且禁止重复启动；paused resume 只恢复原 ledger、不写 operation。存量 draft/stopped 或明确 stopped 后的首次真实启动才创建 version 1。
- [ ] 创建请求只检查调用者 `tasks.manage`、同用户可见引用和引用类型/任务声明用途的静态兼容性；无权返回 403/不泄露存在性的 404。inline 公开目标只做本地语法/规范化 username，可与 Task 同事务 upsert `resolution_state=pending` 引用，不得调用 Telegram resolve/probe，也不得读取账号在线、Telegram 权限、代理、准入、Provider、协议或容量。运行中账号删除、用途变化、授权资产漂移或身份失效写账号级 `account_identity_invalid`，不回滚 Task、缩小冻结分母或阻塞其他账号。
- [ ] 投影失败显式记录，由 reconciliation 重建，不回滚已落库远端事实。

验收：

- [ ] 同一份底层事实只能投影出一个完成结论。
- [ ] AI 一条远端消息在群日总量仅计 1、最多完成一个账号 coverage；同一 primary slot / remote fact 的重复 finalize 幂等。
- [ ] pending、`pending_visibility`、failed、skipped、unknown 均不能让任务提前完成；统一读模型中 pending visibility/unknown 只进入 `unknown_count` 并按 `hold_reason` 分列。
- [ ] 可见确认事务注入任一唯一键/CAS 失败时，Action、hold、主槽、coverage 和远端事实所有权均不发生部分提交。
- [ ] 历史成功的旧错误不会覆盖当前成功事实。

### Task 2：消除 Planner/Dispatcher 热事务冲突

主要文件：

- `backend/app/services/task_center/dispatch_claim_selection.py`
- `backend/app/services/task_center/dispatch_claim_reconciliation.py`
- `backend/app/services/task_center/dispatch_reservations.py`
- `backend/app/services/task_center/service.py`

- [ ] 固定通用 claim 锁顺序：Scope → Window → TaskAllocation → ShardAllocation → Reservation → Action。纯搜索在 Reservation 与 Action 之间固定追加 `search carrier（如有） -> SearchClickOpportunityAssignment -> 搜索 consumptive 子预留`；commit、`_confirm_claim`、Gateway 前最终守卫、release 与 Reconciler 共用顺序，缺失层只跳过不得换序。
- [ ] claim 热事务禁止更新 Task.stats、AI 群日账本或频道消息履约账本。
- [ ] 分离 `ACTION_CLAIM_LIMIT`、单 worker 并发和共享 scope 在途容量。
- [ ] 共享 scope 容量只保护运行时，不转换为 AI 业务目标门禁。
- [ ] 当前部署只有一个业务用户/一个业务租户；`dispatcher_scope` 只表示该用户的 worker、账号 shard 与任务类型共享容量，不实现 tenant 级二次公平。`tenant_id` 继续用于隔离、唯一键和审计。
- [ ] 统一 lane 业务名：现有 `lane=admission/admission_lane_claims` 只承载 `membership_admission` 执行份额，对外投影 `lane_business_kind=membership_admission`；不得承载 API permission、搜索 eligibility、纯 click 或未来未设计模式。
- [ ] 建立默认 60 秒版本化 Claim Window；所有有债务任务跨全部 shard 先按 scope cursor 获得每任务最多 1 个最低轮转机会，剩余容量按未满足 `required_claims` 使用最大余数法形成 `DispatchClaimTaskAllocation`。既有物理字段 `DispatchClaimWindow.allocation_epoch` 的唯一业务/API 名称为 `dispatch_allocation_epoch`；只在非空 release set 提交并要求同一 Window 重建中央 TaskAllocation/ShardAllocation/Reservation 时递增，未提交的权重计算、搜索求解过程和空集合不得递增。
- [ ] `DispatchClaimTaskAllocation/DispatchClaimShardAllocation/DispatchClaimReservation` 固化所属 `dispatch_allocation_epoch` 且不得改绑；Window 增加 `allocation_state=rebuild_required|ready`。新 epoch 的全部 allocation/reservation 与 ready 必须原子发布；计算、CAS、数据库错误或 worker 崩溃均丢弃未发布权重并从最新快照重建，零余额也提交空 ready。released unit 不复活，已 bound/claimed/active 和其他有效旧 Reservation 继续收口。
- [ ] `DispatchLaneShardSolver` 使用确定性最大流或等价精确 task-lane-to-shard 三层匹配，把父任务已经获得的 lane 份额映射到 shard Reservation；同一父任务及 child 不得按 shard 重复最低份额，存在可行映射时不得闲置容量。它不得选择搜索账号/关键词路径，也不得确认远端成功。
- [ ] 以 `allocation_business_task_id=coalesce(admission_execution_sponsor_task_id,parent_task_id,task_id)` 聚合，准入 child 不另取全局份额；父任务 fulfillment/admission 同时可领取时，份额 >=2 各至少 1、份额 1 按持久 lane cursor 跨 Window 轮转。
- [ ] 同一 tenant/target/account/admission version 建唯一 `AdmissionExecutionLease`；一个 sponsor 父任务出资，其他父任务复用事实不重复 Action/Reservation，pre-Gateway 可 CAS 换 sponsor，Gateway-started/unknown 不转。
- [ ] sponsor election/rebind 独立短事务，禁止与 Scope/Window/Action 同时加锁；Reservation 固化 lease version，claim/Gateway 前失配释放并等下一 `dispatch_allocation_epoch`。
- [ ] deadline 任务按剩余 Window 计算；late/recovery 固定 1 个 Window 且不反写按时完成；continuous/ordinary 使用版本化默认 60 Window horizon，只统计 horizon 内到期债务。
- [ ] 多消息/账号任务按 lane + deadline Window + pacing class 聚合 debt 后计算各 bucket required claims；禁止逐微义务 ceil 放大，也禁止最晚 deadline 稀释早到期项；份额内按最早 deadline、未满足比例和义务 cursor。
- [ ] 删除 search > AI、AI > 频道等固定全局排序；AI admission retry 只在对应 AI 父任务 admission lane 内优先，纯搜索点击不创建 admission lane 或 child。
- [ ] Planner/Dispatcher deadlock 使用 PostgreSQL 并发测试复现并回归。

验收：

- [ ] 并发 Planner/Dispatcher 无已知锁顺序反转；另以 search commit/claim/Gateway-final-guard/release/Reconciler 交叉并发证明扩展锁序无 deadlock。
- [ ] 多 Dispatcher 不再退化为一台取得全部 20 个 claim。
- [ ] AI、评论、点赞、浏览、搜索与准入跨多个 Window 均可获得机会；cursor 在 worker 重启后连续。
- [ ] 跨 2/4 个 shard 的单一父任务及 child 每个 Window 仍只获得一次最低保护；task/lane/shard 交叉候选能完成精确映射，无法映射需求显式报告。
- [ ] `last_opportunity_window`、`last_claimed_window` 与 scope cursor 分账；资格变化/空 Reservation 不会让同一任务重复占据下一 Window 首位。
- [ ] 同一父任务在 admission backlog 与 ready fulfillment 并存时两条 lane 均跨 Window 前进，child 不会获得第二份最低保护。
- [ ] 多个 AI 父任务共享同一 admission key 时只发生一次真实 join/follow/confirm，ready 后各 AI 任务独立复检自身完成合同；纯搜索点击不引用该 key 或 lease。
- [ ] 同一评论/点赞/浏览父任务含多个不同 deadline 消息时，早到期义务不被后到期债务稀释，单条消息不吞掉全部父任务份额。
- [ ] claim 扩容后数据库连接和 Telegram 在途量仍有界。

## 3. AI 活群实现包

### Task 3：删除旧门禁和硬小时路径

主要文件：

- `backend/app/services/task_center/coverage_capacity.py`
- `backend/app/services/task_center/hard_hourly.py`
- `backend/app/services/task_center/hard_hourly_history.py`
- `backend/app/services/task_center/hard_hourly_pacing.py`
- `backend/app/services/task_center/executors/group_ai_chat.py`
- `backend/app/services/task_center/service.py`
- `.github/scripts/ai_group_quality_diagnostics.py`

- [ ] 删除 AI 日覆盖容量 precheck/PlanAbort 调用；创建接口只执行结构校验。
- [ ] 启动后运行投影返回 `completion_risk` 与 blocker，固定 `warning_requires_confirmation=false`；不得增加确认页或阻断合法 Task 创建。
- [ ] 删除新 hard-hourly Action、checkpoint、补偿 debt 和 release gate。
- [ ] 删除 AI `active_window`、本地群日限额和群冷却阻断。
- [ ] 旧 hard-hourly 数据只读保留审计，迁移后不再驱动运行。
- [ ] 诊断脚本断言没有新增 hard-hourly Action、容量 PlanAbort 或活动窗口 skip。
- [ ] 门禁删除不得修改 `reply_min_*`、direct/reply 拆槽、`material_intent/allow_material` 或素材规则输入。

### Task 4：落实群日目标与 24 小时规划

主要文件：

- `backend/app/services/task_center/daily_group_target.py`
- `backend/app/services/task_center/daily_coverage.py`
- `backend/app/services/task_center/daily_coverage_planning.py`
- `backend/app/services/task_center/executors/group_ai_chat.py`

- [ ] 任务日冻结 `timezone_snapshot`、本地日期和精确账号范围；分母只排除非 active、已删除、非普通运营用途或永久身份/安全不合格账号。
- [ ] `daily_message_target` 持久化在 task+target-group 粒度；多群分别建 ledger/MessageSlot/coverage，禁止共享或平均 task 总量。
- [ ] online/session/proxy/mask/membership/can_send 只形成 blocker，不从冻结分母删除；冻结后状态变化保留 tombstone，当天新增账号从下一 ledger 生效；时区修改在当前 ledger deadline 以 pending revision 生效并按需建立 `timezone_transition` ledger。
- [ ] `admission_abandoned` 不删除冻结账号、不缩小当日分母、不完成或释放其 coverage 主槽，也不进入旧 durable-debt 排除；其他账号只能完成自身 coverage 与尚未分配的 extra-volume，deadline 后该账号 coverage 只能记 `missed`。
- [ ] 首日中途启动固定 `planning_anchor_at=running_at`，从 0 在剩余权重内增长到完整目标；重复暂停/启动不重置，下一任务日才进入完整 SLA。
- [ ] 有当前未截止 ledger 时修改群日目标/24 小时权重只写 pending revision，并固定在当前 deadline 生效；当前 ledger/MessageSlot/pacing snapshot 不变，重复编辑 CAS 更新值但不延后 effective_at；生效时非 running 不建空 ledger，resume 的 partial-start 使用新值。
- [ ] 计算有效目标、累计应完成、真实确认、有效 open 和 planning need。
- [ ] 每个冻结账号建立不可互换的 coverage 主发送槽，剩余目标建立 extra-volume 槽；累计进度按 `coverage_due=min(frozen,ceil(due*frozen/effective))`、`extra_due=due-coverage_due` 分池，blocked coverage 不得被 extra 消息抢占，已到期 extra 仍可独立发送。
- [ ] 24 个小时权重全部大于 0，静默小时权重更低。
- [ ] 未准入/无路线账号保留债务，但不阻断其他账号。
- [ ] 有界批次只做队列背压，不形成全天停止结论。

### Task 5：共享准入与完成优先内容链

主要文件：

- `backend/app/services/task_center/group_bot_admission.py`
- `backend/app/models/group_bot_admission.py`
- `backend/app/services/task_center/dispatcher.py`
- `backend/app/services/task_center/executors/group_ai_chat.py`

- [ ] AI 账号主动 join，并完成可信群管要求的频道关注/确认/挑战。
- [ ] 准入状态按账号、群、policy version 和 source generation 幂等持久化。
- [ ] 正常正文固定为主 AI 最多 3 轮、备用 AI 最多 3 轮，逐轮保存 `provider_stage/provider_round/generation_round_total` 和质量拒绝原因；主/备用不得是同一 Provider/模型，不得存在隐藏第三层。
- [ ] 普通候选的内容安全/质量拒绝只消耗当前 Provider 轮次；主第 3 轮失败切备用第 1 轮，备用第 3 轮失败后进入确定性兜底。只有兜底自身被明确出站策略禁止才写 `fallback_outbound_policy_blocked`。
- [ ] 三类特殊场景原义务转精确 `签到`，无开关；只替换槽位正文，保留 reply 关系和内容合同；早于素材意图的缺面具槽位标为 fallback-only。
- [ ] `policy_min` 或已形成的 `selector_plan` 素材义务若分配给兜底槽位，通过版本化 CAS 转派到同 Cycle 未进 Gateway 的正常槽位；无合法槽位时报告 shortfall，不超出群日目标补发。
- [ ] 授权/代理异常先切换到已验证路线；无路线进入 `waiting_transport`。
- [ ] `post_send_intercepted|visibility_confirmed_failed` 在同一短事务关闭当前 `pending_visibility_hold`、把 Action 写为明确失败并撤销当前 admission ready；群日和 coverage 均不增加，原主槽保留。只有账号重新 `admission_ready` 后，下一 tick 才能对原主槽递增 attempt 建新 Action。
- [ ] 完整可见性核验窗口结束仍无法判断时，Action 进入 `unknown_after_send`，hold、主槽、远端证据和 coverage 占位保留；不得超时猜成功、猜失败或创建替代签到。
- [ ] `admission_abandoned` 只能由具备 `targets.manage` 的运营在 preview、reason、evidence 与 `expected_admission_version` 校验后写入；系统不得因超时、积压或一次 intercepted 自动 abandon。它只停止该账号后续自动 admission 并终结未进 Gateway Action，不改写 Gateway-started、pending visibility、unknown 或历史事实；reopen 递增 `admission_version`，不回写历史任务日。
- [ ] 无需可见性核验的普通远端成功沿用真实 Attempt + non-empty remote id 原子确认群日总量和账号覆盖；需要核验的路径必须等 `visible_confirmed`，不得把传输边界成功当业务完成。

测试重点：

- [ ] 缺面具不再停住账号，签到可完成覆盖和额外群日义务。
- [ ] 主第 1/2 轮失败继续主 AI，主第 3 轮失败切备用；备用第 1/2 轮继续备用，备用第 3 轮失败转签到，总真实调用不超过 6。
- [ ] 相同配置、上下文和随机种子下，门禁删除前后的 direct/reply 及正常图片/表情素材槽位一致。
- [ ] 10/30/60 Turn、20 条数据库切批、多个 claim 和静默小批量不重置 mix scope、引用最小值或每轮素材计数。
- [ ] 双 Planner/Dispatcher 不重复预约同一 mix slot，失败/unknown/success 的释放与占位守恒。
- [ ] 缺面具在素材意图前发生时不会虚构素材最低数；最后一个素材槽转签到时，剩余正常槽位转派或明确 shortfall，不吞义务、不超量。
- [ ] reply 目标失效重建仍保持同一 reply slot；无新引用对象时 waiting/shortfall，不转 direct，Gateway-started/unknown 不替换。
- [ ] 纯文本签到不计图片/表情素材配额；内容缺口优先在剩余总量槽位内补齐，不隐式超量。
- [ ] 其他账号的重复历史、准入或 transport blocker 不连带阻塞当前账号。
- [ ] `unknown_after_send` 不产生替代签到。
- [ ] `post_send_intercepted` 只关闭当前 hold，不删除 coverage 槽；账号未 ready 前不循环发送，恢复 ready 后同槽仅建一个新 attempt。
- [ ] abandon 缺 permission/preview/reason/evidence/version 任一项均拒绝；成功 abandon 不缩分母、不完成 coverage、不影响已进 Gateway Action，deadline 后如实 missed。

## 4. 评论实现包

### Task 6：修复每消息目标与表情兜底

主要文件：

- `backend/app/services/task_center/executors/channel_comment.py`
- `backend/app/services/task_center/executors/channel_comment_budget.py`
- `backend/app/services/task_center/executors/channel_comment_targets.py`
- `backend/app/services/task_center/dispatcher.py`

- [ ] 移除任务 lifetime cap 对单消息目标的错误截断。
- [ ] 正常评论固定为主 AI 最多 3 轮、备用 AI 最多 3 轮，并保留 Provider 阶段、轮次与质量拒绝事实。
- [ ] Phase A 冻结账号 active 面具或 explicit missing，并把 `comment_mask_policy=required` 迁移到存量规则；“缺面具”不得由执行时临时猜测。
- [ ] 首次逐消息规划冻结 `comment_plan_revision`、`ContentMixContract` 和 `policy_min` 素材义务；既有选择器实际选出的额外素材槽位形成 `selector_plan` 义务，补差额/切批/失败重领不得重置 scope。
- [ ] 三类特殊场景生成稳定单表情 `post_comment`，但不修改原 direct/reply 或既有素材占比归属。
- [ ] 回复关系、原消息、账号、授权路线与义务键保持不变。
- [ ] 明确发送失败可重试；Gateway 后未知不得重复评论。

验收：

- [ ] 目标 80 不会在 65/80 或 79/86 提前完成。
- [ ] 表情评论拥有远端评论 ID，且不被统计成 reaction。
- [ ] reply 目标失效不会生成普通评论。
- [ ] 单表情 direct/reply 统计正确，且不会被计入图片/表情包正常素材配额。
- [ ] 已分配素材义务的评论决定转表情时，只能在 outbound payload 冻结前向同一消息/revision 的未进 Gateway 正常槽位 CAS 转派；无槽位时为 shortfall，不超出逐消息目标补发。

## 5. 搜索点击实现包

### Task 7：纯搜索点击与存量混合任务隔离

主要文件：

- `backend/app/schemas/task_center.py`
- `backend/app/api/routers/task_center.py`
- `backend/app/auth.py`
- `backend/app/permission_middleware.py`
- `backend/app/models/task_center.py`
- `backend/app/services/task_center/search_join_daily_capacity.py`
- `backend/app/services/task_center/search_join_opportunity_assignments.py`
- `backend/app/services/task_center/search_click_target_progress.py`
- `backend/app/services/task_center/search_join_config.py`
- `backend/app/services/task_center/executors/search_join_group.py`
- `backend/app/services/task_center/search_join_facts.py`
- `backend/app/services/task_center/search_join_protocol.py`
- `backend/app/integrations/telegram/search_join.py`
- `backend/tests/test_permission_vocabulary.py`

- [ ] 新建搜索任务只通过 `POST /api/tasks/search-click[/create-and-start]` 固定持久化 `task_type=search_click`、`search_execution_mode=click_only` 和 `daily_click_target_count`；模式不可编辑。请求出现 `join_target_group_after_click`、`daily_admission_target_count`、成员目标或其他 admission 字段时返回 `422 field_not_allowed_for_click_only`。旧 `/search-join-group` 创建请求固定返回 `410 legacy_search_join_create_retired` 且零 Task 写入；旧 `search_join_*` 只作存量读取、迁移识别/物理兼容，不得成为当前业务身份或隐藏创建入口。
- [ ] 创建接口只校验调用者同时具备 `tasks.manage + tasks.create.search_click`、同用户可见目标/账号组引用、纯点击字段与数值范围；缺任一权限返回 403，不可见引用返回不泄露存在性的 404，结构合法即返回已创建 Task。创建并启动先提交 Task，再建立 ledger 和运行 blocker，不读取 Telegram/账号/代理/协议/容量，不执行 preflight 或 warning confirm。
- [ ] 搜索创建接口复用 Task 1 的 `client_request_id + request_fingerprint + start_operation_id` 合同；同键不同配置返回 409，启动失败无残留 ledger/assignment，相同请求只重试或回读原 Task。启动成功但资源暂不可用时必须返回 `start_status=started/runtime_state=waiting`，不能写成 start failure。
- [ ] 对存量配置和历史 Action 做 dry-run 分类；任何包含 membership/admission 配置或 child 事实的任务标记为内部只读 `legacy_mixed_search_join`。不得自动改成 `click_only` 或尚未设计的 `click_and_join`，不得修改、删除或重绑历史 Action/Attempt/remote fact，也不得从纯点击编辑入口修改。
- [ ] `click_only` 从 Planner、Dispatcher 到 Gateway 全链路禁止创建或领取 membership/admission/join/follow/confirm/challenge/can-send Action，`admission_lane_claims=0`；真实 click 确认后直接终结 ordinal。
- [ ] 升级协议样本合同，目标按钮分开固化 `click_effect` 与 `membership_side_effect`。纯点击 eligibility 只接受 `membership_side_effect=none`，Gateway 调用前断言 `membership_mutating_rpc_invoked=false`；旧 `join_candidate` 或副作用未知样本写运行 blocker，不自动升级，也不调用 JoinChannel/ImportChatInvite/request/follow/confirm/can-send。
- [ ] Planner 分离不扣 held/unknown 的 `remaining_click_count` 与防重建单的 `planning_click_deficit`；在 ledger 锁内为欠额取得稳定 `click_obligation_ordinal`，每个 ordinal 同时最多一个 open/unknown/success，明确失败 replacement 复用原 ordinal。
- [ ] 搜索求解拆成 `projection|commit`：Planner/详情的未来 projection 先按同一快照的全部任务债务逐 Window 只读重放中央 TaskAllocation/lane/shard，再对模拟 search 份额使用 `SearchClickAssignmentSolver`；不能假设搜索独占 scope，不创建 assignment、Action、claim 或任何 hold，并返回 `projection_not_reserved=true`。commit 必须等待 Window `allocation_state=ready`，且当前 `dispatch_allocation_epoch` 的真实全任务 `DispatchClaimTaskAllocation`、search fulfillment lane 和 shard `DispatchClaimReservation` 已由 `DispatchLaneShardSolver` 整批发布，只在每 Task 已获份额内生成 assignment/Action。assignment 持久化 `dispatch_claim_window_id/task_allocation_id/reservation_id/fulfillment_lane_claim_ordinal`；对非 released 行唯一约束 `(reservation_id,fulfillment_lane_claim_ordinal)` 且 ordinal 属于 `1..reserved_claims`。Dispatcher/Gateway 及全任务共享代理 inflight 只绑定一份中央 Reservation，禁止 Planner 预占和 assignment 二次预留。
- [ ] 新增唯一 `SearchClickAssignmentEpoch(dispatch_claim_window_id,dispatch_allocation_epoch,search_click_assignment_epoch,solver_problem_hash,solver_input_hash,solver_owner_lease_id,solver_claimed_at,state,solver_result,release_unit_set_hash,outcome_hash,next_dispatch_allocation_epoch nullable,rebuild_input_version_after nullable)`，唯一键为 `(dispatch_claim_window_id,dispatch_allocation_epoch)`。problem hash 规范化包含 `solver_contract_version`、稳定业务义务和连通分量候选/资源/公平输入，排除 Window/dispatch/search epoch、TaskAllocation/Reservation/ordinal/assignment ID、carrier 派生份额、worker/lease、时间和随机值；input hash 在其上加入当前 carrier、精确 Reservation unit/version 与中央份额版本。字段集合或排序规则变化只提升两个 payload 内的 contract version，不新增独立状态列。release hash 对稳定排序的 `(window,reservation,ordinal,reason_code,resource_snapshot_hash)` 精确集合计算，空集合也保存确定性 hash；outcome hash 同时覆盖 carrier 身份、problem/input hash、solver result、全部 matched assignment identity/version、release hash和实际 wave epoch/input version。创建 open 行的同一事务绑定当前有效 worker lease，唯一冲突者只回读。lease 只作进程存活 fencing，健康 owner 求解期间持续续租，固定租约时长、心跳周期或续租次数不得成为隐藏 solver deadline。只有 owner 失联、fencing token 失效或明确丢失续租所有权时才 abandoned，不转移 ownership、不重跑求解或保存 solver attempt/history。已 finalized 重放必须返回完全相同的 problem/input/release/outcome/wave 字段，任一不一致保持 `release_fact_incomplete`。即使 `no_candidate|abandoned` 没有 assignment也保存结果和释放幂等。新增 `SearchClickOpportunityAssignment` 与搜索专属资源子预留，assignment 引用 epoch；共享同一 click ordinal、任一资源 key 或同一 `assignment_fairness_key` 的候选进入同一连通分量。
- [ ] `no_feasible_search_path|search_solver_abandoned` 的每个 unit 以本连通分量 `solver_problem_component_hash` 作为 exclusion resource hash；完整 input hash 禁止参与 supersede。仅新建 epoch、换 Reservation/worker 或推进 carrier 版本时 component hash 不变，只有相关业务问题分量或 contract version 改变才允许重新获配。
- [ ] 实现唯一 `SearchSolverSnapshotAssembler`：在同一一致性数据库快照中构造并持久化 `SearchSolverProblemSnapshot`、全部 `SearchSolverProblemComponent(stable_component_key,canonical_nodes_edges_fairness,component_hash)` 与每个 Reservation/ordinal 唯一的 `SearchSolverCarrierUnitBinding`。共享任一 resource/fairness key 的节点必须在同一分量，无候选 unit 也建立零边分量。open epoch、完整 snapshot/component/binding、problem/input hash 与 owner lease 一个事务落库后才允许 solver 调用；solver 只读该快照且额外查库调用数为 0。owner 丢失 recovery 用原 binding/hash 释放，supersede 复用同一 Assembler/canonicalization。组装失败不得留下 open 半行/部分 payload或伪造 `no_candidate|optimal`。
- [ ] `stable_component_key` 由 `solver_contract_version` 与稳定排序的业务义务、候选 edge、resource/fairness node 身份确定，禁止随机 ID、carrier/worker/时间；component hash 再覆盖全部当前值/version。Assembler 必须覆盖 solver 的所有输出影响读取，最低包括 `hard_safe_remaining_capacity`、同一冻结 `account_quota_key/capacity_window_key` 内的 `confirmed_click_count_today`、持久 `last_click_opportunity_at`、`persistent_account_cursor` 及来源 version；`today` 不能读取服务器日期或提交时墙钟。新增候选、约束、目标或 tie-break 输入必须进入 canonical payload并提升 contract version。
- [ ] `precondition_lost` 只终结旧 expected assignment/Action version 的 trigger：observed 已进入 `claimed|gateway_started|unknown|consumed` 时状态绝不倒退且永不再释放；observed 仍是新的 Gateway 前 `reserved|action_bound` version，且原释放条件仍成立时，只能由产生该新版本的状态变更事务/outbox 生成全新 trigger key/candidate hash。旧 batch 不重开，无版本事件不轮询，不能让 Gateway 前新版本因旧 no-op 永久占用。
- [ ] 正常 `no_candidate|optimal` finalize 前，在短 PostgreSQL `SERIALIZABLE` 事务内以同一候选谓词/source key运行同一 Assembler 的只读 revalidation，重算 problem/input hash并逐项比较所有输出影响 source version，不能覆盖原 snapshot。候选 phantom、额度、账号已确认 click 数、机会时间、cursor、eligibility、中央份额或任一版本漂移时，原 epoch 整轮 `abandoned`，不提交旧解，按原 binding 释放全部仍未领取 unit并加入唯一 rebuild wave。serialization、数据库或 CAS 失败整批回滚且保持 open；recovery 仅按原 snapshot finalize abandoned，禁止 ORM/驱动重放旧 solver 结果或在同一 epoch 重新求解。
- [ ] `SearchClickAssignmentSolver` 在每个分量只使用 `x[ordinal,path]` 与 `z[task]` 做带最优证明的确定性 `0/1` 多阶段字典序求解，目标依次固定为：最大 click assignment 数、最大受服务到期任务数、按冻结 `assigned_count/max(remaining_click_count,1)` 的最大最小任务公平向量、稳定 path 顺序。不得建立 `y[task,account]`、admission distinct/budget key 或 admission 增益阶段，后阶段不得降低前阶段最优值。
- [ ] 约束同时保证 ordinal 唯一、`sum(x_task) <= fulfillment_lane_claims(task)`、shard usage 不超过中央 Reservation，以及带 `capacity_window_key/version` 的账号/关键词 consumptive quota 与授权/协议/代理 eligibility 不超可用量。结果闭集为 `no_candidate|optimal|abandoned`：整个快照无路径写 `no_candidate`；完整可验证结果写 `optimal` 并保存 matched/served-task/task-assignment-counts/task-unmatched-reasons/fairness-vector/unmatched/saturated；无法一次完成求解或任一 reservation/resource CAS 无法绑定写 `abandoned`，不得提交部分解。
- [ ] 一个 `search_click_assignment_epoch` 只求解一次并只成功 finalize 一次。`optimal` 必须先验证 matched/release ordinals 互斥、全部 matched 绑定和 release set 的 CAS 前置条件，再锁定 Window 验证 `window_claimable && allocation_state=ready && Window.dispatch_allocation_epoch=SearchClickAssignmentEpoch.dispatch_allocation_epoch`；任一条件失效就改为 `abandoned`，包括 Window 已经在更高 epoch 重建回 ready，不提交部分/过期 assignment，并把全部仍未领取 unit 组成首次 release set。`no_candidate|abandoned` 同样把全部未领取 unit 组成首次 release set。集合内每个 unit 精确绑定 `source_dispatch_claim_reservation_id + source_fulfillment_lane_claim_ordinal` 并固定 `release_count=1`；该二元组在同一 Window 跨 `active|superseded|expired` 永久唯一，resource hash 不进入幂等键。事务按 `Scope -> Window -> TaskAllocation -> ShardAllocation -> Reservation` 加锁，同层多行按主键排序，再依次锁 epoch carrier、既有 assignment、搜索 consumptive 子预留和 Action；不存在的层只跳过不得换序。验证所有 ordinal 未被占用、`bound + new_matched + claimed + released + unbound_release <= reserved` 和各层 unclaimed 非负后，一次性写全部 assignment/exclusion、更新 bound/released/unclaimed并把 search epoch CAS 为 finalized。空集合不改中央状态；非空集合按下条加入 rebuild wave。任一数据库错误、CAS、唯一键或计数守卫失败整批回滚且 epoch 保持 open；计数漂移先按 assignment/claim/永久 exclusion 事实 reconcile，再完成同一 abandoned outcome，不重跑搜索求解。禁止部分绑定、部分释放或双扣。
- [ ] 将当前 epoch 的 `claim_class=search_click` fulfillment Reservation 标记为搜索物化流程独占语义：从中央 ready 发布到首次 search outcome finalize，通用 no-Action/unclaimed/expiry reclaimer 必须跳过。Window 可领取且结果行缺失时首个有效 worker创建 open epoch并执行一次；Window 已结束且结果行缺失时 recovery 在同一事务创建并直接 finalize abandoned，solver 调用数为 0。任务暂停/停止/删除或 due 消失只使原 epoch optimal 前置失效，不得另造 carrier。首次 outcome finalize 后每个来源 Reservation 强制 `bound_count + claimed_count + released_count = reserved_claims`；违反所有权写 `search_reservation_ownership_violation` 并隔离。
- [ ] `allocation_state=ready` 只控制新中央版本和新 search epoch/assignment。回归 optimal 同时生成 matched/unmatched：unmatched 触发 `rebuild_required` 后，matched 的旧 epoch Action 在来源 Reservation/assignment/资源/Action version、Window 与业务 deadline 有效时仍可 `_confirm_claim -> Gateway`；不得等待新 ready、读取未发布权重或误释放，失效后才走稳定 release batch。
- [ ] 新增 `DispatchAllocationReleaseBatch` 承载 search epoch finalized 后的释放，至少保存 Window/source epoch、`release_trigger_type/release_trigger_key`、`candidate_unit_set_hash/candidate_unit_count`、`release_unit_set_hash/release_unit_count`、`already_released_unit_count/precondition_lost_unit_count`、outcome/outcome hash、next dispatch epoch、`rebuild_input_version_after/finalized_at`；唯一键为 `(window,release_trigger_type,release_trigger_key)`。另为每个 candidate 写唯一 `(batch,reservation,ordinal)` 的 `DispatchAllocationReleaseBatchItem`，保存 assignment/expected version、nullable bound Action/expected Action version、`effective_released|already_released|precondition_lost`、observed assignment/Action state/version 和首 carrier。强制 `candidate_unit_count = release_unit_count + already_released_unit_count + precondition_lost_unit_count`；`applied` 仅对应 release 非空且两个 no-op 计数均为 0，`no_op` 仅对应 release 为 0，`mixed` 仅对应 release 非空且至少一个 no-op 计数大于 0。trigger 只能由 assignment/version Gateway 前终态、Action/version 不再到期或 Window/source epoch 到期等不可变事实派生，不能使用随机 batch、worker 或扫描时间。candidate hash 固化完整 item 输入与两个 expected version；outcome hash 固化 carrier/candidate、全部 item 分类与 expected/observed version/首 carrier、release/count/outcome 和实际 wave 版本。统一锁内按稳定 unit key/Action ID 只把 assignment 和 nullable bound Action 均仍为指定版本、Action 未进入 Gateway且无 exclusion 的 unit 放入 effective release set。已由其他 carrier 释放且 Action 已不可领取的 unit 分类 `already_released` 并回读首 carrier；已 claim/Gateway-started 或任一版本变化分类 `precondition_lost`；二者均 no-op。同一事务将全部 item、effective unit 的 bound Action 终态/lease/active、assignment `-> released`、Reservation `bound -= release/released += release`、Task/shard/Window unclaimed 扣减、永久 exclusion 和 batch finalize 一次提交；保留 Action 绑定作证据，禁止 released assignment 对应 pending/claiming Action。effective set 为空也 finalize no-op 但不推动 rebuild。同 trigger 同 candidate hash 只有逐 item/result/wave/outcome hash 全部一致才零写回读，结果错绑保持 `release_fact_incomplete`；不同 candidate hash 返回 `release_batch_input_conflict`。不能重开或改写原 search epoch。
- [ ] 无法分类的 assignment/Action/exclusion/claim/Gateway/计数矛盾必须让 release 事务先整批回滚；独立 consistency writer 再按相同锁序复核，仍矛盾才以 `(window,reservation,ordinal,issue_fingerprint)` 幂等持久化 active 对象级 quarantine 和全部 observed version。该 trigger 在 resolved 事件前不做定时重试，包含该 unit 的 batch 暂停，其他独立义务继续。Reconciler 分支前先验证合法 release fact set：首次 outcome 为 finalized search epoch + `release_unit_set_hash` 内 unit + matching exclusion，post-finalize 为 finalized batch + `effective_released` matching item + matching exclusion；缺件、错绑或 hash/版本不一致保持 `release_fact_incomplete`，不能自动判 released。完整事实四分支固定为：合法 release fact set 且无 claim/Gateway时，以逐 unit 事实为权威；存在 assignment 时对齐为 released，首次 outcome 的未绑定 unit 保持无 assignment；终结遗留 Action并重算各层摘要，使该 unit 只贡献一次 released；孤立 released 且无任何 release 组件时恢复 `reserved|action_bound`、推进版本并生成新 trigger；只有 claim/Gateway且无任何 release 组件时向远端事实对齐且不回滚；合法 release fact set 与 claim/Gateway 同时存在时写 `release_claim_fact_conflict`，禁止自动删除 release 组件、回滚 Gateway、选边、调整该 unit 的 released/claimed 计数、resolve 或忙重试。只有前三个分支提交后才唤醒 trigger；冲突 unit 的真实 click evidence可入账，但相关 ledger 在 quarantine 清除前不得通过 E4。所有分支都不重跑搜索求解，也不形成第七类结构门禁。
- [ ] `DispatchClaimWindow` 新增 `rebuild_input_version/ready_rebuild_snapshot_hash`，TaskAllocation/ShardAllocation/Reservation 固化所属 epoch并新增 `dispatch_rebuild_snapshot_hash`。尚可领取 Window 的首批非空释放在 `ready` 时只执行一次 `dispatch_allocation_epoch += 1`、置 `rebuild_required` 并增加输入版本，开启唯一 pending rebuild wave；wave 内后续 outcome/batch 只增加输入版本并复用 pending epoch。Window 已结束只收口释放事实，空集合不改中央状态。`DispatchLaneShardSolver` 冻结 pending epoch、input version 与规范化 rebuild hash；payload 固定覆盖 `(window,pending_epoch,rebuild_input_version)`、全部 task/lane/shard due/eligibility 稳定键/当前值/版本、active exclusion 的 unit/state/reason/resource snapshot、全部仍有效旧 Reservation 身份/承诺计数/版本、scope/shard 容量与影响分配的配置值/版本，排除 worker/lease、扫描或墙钟时间、进程身份和随机值。提交前按中央锁序重读同一输入并重算 hash；epoch、input version 或 hash 任一变化均丢弃整批，即使变化没有推进 input version。所有新 epoch 行、Window `ready_rebuild_snapshot_hash` 与 `ready` 必须固化同一 hash并一次提交；零余额也发布带 hash 的空 ready。版本复核失败、数据库错误或 worker 崩溃均从最新事实重建。其他 shard/任务仍可获得份额，released unit 不复活，claimed/active、其他有效旧 Reservation/cursor 与 click 欠额不回退。exclusion 的 snapshot 必须按 reason 只固化本 unit 失败直接依赖的 Window、Task/ledger/target、solver input 或 assignment/Action version及相关额度、授权、代理、协议/CAPTCHA、Gateway 资源版本；无关 Task/shard、worker/lease、扫描时间或随机值变化不得 supersede。只有相关规范化资源变化时 exclusion 才转 `superseded`，Window 结束时 `expired`；`no_feasible_search_path|search_solver_abandoned` 固定绑定原业务问题分量的 `solver_problem_component_hash`，完整 input hash 禁止参与 supersede。它不跨 Window，也不形成账号/任务黑名单。
- [ ] rebuild hash 的输入清单必须由 `DispatchLaneShardSolver` 实际影响输出的业务读取反向校验，而不是手工挑选子集。最低追加 `dispatch_rebuild_contract_version`、Scope/Window/Shard capacity/active/unclaimed 当前值与版本、全部 scope/task-lane/shard fairness cursor 与版本、parent/sponsor 聚合输入；新增影响输出的读取必须进入 payload并提升 contract version，版本只属于 hash payload。纯诊断字段、worker/lease、时间、进程和随机值不得进入。
- [ ] 新增 immutable `DispatchRebuildInput` assembler，统一完成数据库读取、稳定排序/序列化和 hash；`DispatchLaneShardSolver` 只接受该对象并作为纯函数返回携带同一 hash 的完整分配结果，禁止 solver 内部查库或读取进程全局状态。precommit 必须在中央锁内重新运行同一 assembler，不能另写一套不完整 version-vector 校验。
- [ ] precommit assembler、hash 比较、全部新 allocation/reservation 与 Window ready/hash 写入使用一个短 PostgreSQL `SERIALIZABLE` 事务，先按中央锁序锁 Scope/Window，并让完整 input 的行读取和候选谓词都参与冲突检测。serialization failure/CAS/hash 不等全部回滚且废弃 solver 输出；禁用 ORM/驱动对该事务的旧结果自动重放，下一 drain 重新 assemble/solve。
- [ ] `dispatch_rebuild_contract_version` 或搜索 `solver_contract_version` 变化时禁止新旧 Dispatcher 滚动混跑。Release Gate 先阻止旧版本取得新 ownership，确认旧进程全部终止且无旧版本数据库事务仍可提交，再启动新版本；旧内存 solver 输出全部作废。pending rebuild 由新版本重新 assemble/solve；旧 owner 遗留 open search epoch 在 fence 后直接 abandoned并释放未领取 unit，不转交 ownership、不沿用旧解。
- [ ] finalized `SearchClickAssignmentEpoch`、`DispatchAllocationReleaseBatch`、`DispatchAllocationReleaseBatchItem`、`DispatchAllocationExclusion` 与来源 Reservation 在迟到 writer 仍可访问期间不得单独物理删除；联合归档前先 fence 旧 worker。归档只冷存 payload，主库永久保留 carrier key/hash、batch item candidate unit、assignment/Action expected+observed version、classification/first-carrier 引用与 `(window,reservation,ordinal,released)` identity tombstone；状态迁移不得释放唯一键、丢失逐 unit outcome 或恢复旧 unit claim。
- [ ] 本 PRD 不设置 solver 技术 deadline、性能预算、图规模基线或 p99 指标，也不为这些指标增加 retry/降级分支。健康 solver owner 的 fencing lease 可持续续租，租约时长/心跳/续租次数不得成为隐藏 deadline；只有明确求解失败、无法返回完整可验证结果、owner 失联或 fencing 所有权丢失才 `abandoned`。禁止笛卡尔积计数、贪心、固定 top-N 或部分解。只有 `reserved|action_bound` 且未 `_confirm_claim` 的 assignment expiry 不晚于 Claim Window；确认领取时同一 CAS 把中央 Reservation 转 active、Action 转 executing、assignment 转 `claimed`，此后 Window 结束不得释放。Gateway 前失效/到期用 release batch 直接放弃并加入当前/新 rebuild wave；Gateway 调用结束释放中央 inflight，unknown 仅保留 ordinal 和可能已消费 quota hold，不能无限占用在途容量。
- [ ] 不暴露账号容量、账号优先级或 account cursor 配置。系统在最大 click、最大受服务任务数和跨 Task 公平向量固定后，严格按 `hard_safe_remaining_capacity DESC -> confirmed_click_count_today ASC -> last_click_opportunity_at ASC -> persistent_account_cursor ASC` 稳定决胜；业务 `scheduled_end` 只用于任务停止和按时/late 归属，不派生 assignment 技术 deadline 或性能预算。软曲线落后时立即把 due 提升到完整日目标并进入 catch-up；这些字段不得预过滤合法 repeat 路径。
- [ ] click confirmed 必须来自同一 ExecutionAttempt 的 target identity、批准 button fingerprint、Gateway/click invocation、批准 protocol outcome、`membership_side_effect=none`、`membership_mutating_rpc_invoked=false`、remote_confirmed_at 和 evidence hash；只找到目标/按钮不计成功。确认事务完成 Action、Attempt、远端事实和 ordinal 后不得派生 child。
- [ ] source 固化 `task_day_ledger_id`、义务本地日期、任务时区 revision、UTC period、deadline 和 `click_obligation_ordinal`；每份 ledger 只冻结 `daily_click_target_snapshot`。
- [ ] 目标数、时区或账号范围编辑只写 pending revision，并固定从当前 deadline 后生效；重复编辑不延后 effective_at。deadline 后确认只记原 ledger late，旧 ledger missed 不回写；新 ledger 建立新的 click ordinal，不借用旧日 click 事实。
- [ ] 极搜 `hot_list_page/unknown_page` 直接写 `jisou_session_state_deviated`；禁止 `/cancel`、`/start`、重发关键词、外链或未知 callback reset。按已批准安全协议将对应账号—协议路径写入带 `reason_code/expires_at` 的 24h eligibility 排除，不减少 click 欠额、不停止其他账号，也不等同 `DispatchAllocationExclusion`。图片算式验证码冻结 `challenge_fingerprint_hash` 并只认实际 `jisou_image_verification_required -> solved|failed`：`required` 不触发 24h 排除；只有同一 fingerprint 的单次批准答案提交取得明确远端通过回执或进入已审批搜索分类/结果页才写 `solved` 并继续。仅离开原页、超时、hot-list、unknown 或新 fingerprint 不算通过，新 fingerprint 重新 required；单供应商候选不合格继续下一个，供应商/传输暂不可用保持 required，只有全部当前健康已审批供应商确实无安全答案或同 fingerprint 被远端明确拒绝才最终 failed。验证码识别 AI 和批准重试不占 click 限额、目标或额外中央份额，也不进入 AI 活群/评论的主/备用 AI 生成轮次或业务 AI 生成次数，不使用触发概率/历史成功率估算容量；当前 Action 保留既有账号 session ownership，challenge 收口前同一账号—协议会话不得被另一搜索 Action 并发改写。新 Action 固定 `recovery_kind=not_applicable/reset_executed=false`，历史 reset 字段只读，不新增 reset Action/事件/次数。

验收：

- [ ] 新建、详情和运行事实均固定显示 `click_only`；任何 admission 字段返回 422，任何 membership/admission child 创建或领取调用数为 0。
- [ ] 旧 `/search-join-group` 创建 API 返回 410 且不创建/规范化 Task；旧任务读取与迁移识别仍可用，旧权限不能授权新建纯 click。
- [ ] `target_click_observed` 确认后 ordinal 直接完成；按钮发现、membership、can-send 或历史 admission 事实均不能替代 click 成功。
- [ ] 旧 `join_candidate`、成员副作用未知和任一成员关系变更 RPC 均不能完成纯 click；其他合法账号/协议路径继续，且该 blocker 不影响结构合法 Task 创建。
- [ ] CAPTCHA 只按 `required|solved|failed` 实际状态验收且不得新增第四种验证码状态；供应商/传输暂不可用或识别结果不确定仍保持 required，只另写 unavailable/unknown reason。识别 AI/批准重试不占 click 限额、目标或额外份额，也不计入 AI 活群/评论的主/备用 AI 轮次或业务 AI 生成次数；不设置业务固定 AI 轮数/递归次数。同 fingerprint 的单次批准提交只有取得明确远端通过回执或已审批搜索分类/结果页才 `solved` 并继续；仅离开原页、消息消失、超时、新 fingerprint、hot-list、unknown 都不得 solved。识别链确实无安全答案或同 fingerprint 被远端明确拒绝才 `failed` 并形成 24h eligibility 排除。每个 fingerprint 最多一次 Telegram 提交，新 fingerprint 重新 required。代理切换和 unknown click 均保留真实协议状态；只有明确无 click 事实的失败可释放 ordinal 给 replacement。
- [ ] 搜索 projection 字段固定为 `projected_eligible_attempt_capacity_before_deadline`，只表示未预留尝试上界；schema/API/UI/诊断不得出现 `projected_confirmable_clicks_before_deadline` 或把 projection/assignment 加入 confirmed。未进入验证页可计 eligible attempt，进入后只有真实 solved 才恢复 click opportunity。
- [ ] 系统账号排序、catch-up、静默非零执行和合法 repeat 补量有确定性回归；账号选择不能降低最大 click 数、最大受服务任务数或跨 Task 公平向量。
- [ ] 一个 source pending/unknown 时，只防重其 click ordinal；其他 ordinal 仍有已提交 assignment 时继续补完整日目标。
- [ ] 两个搜索 Task 共享同一账号/关键词/授权/代理容量时，projected capacity 之和不得超过资源账本真实剩余量；reservation/resource 绑定 CAS 冲突使当前 search epoch 直接 `abandoned`，不提交部分 assignment。全部未领取 unit 形成一个 release set并整批原子释放；尚可领取 Window 的首批非空集合只递增一次 `dispatch_allocation_epoch`、置 `rebuild_required`，同 wave 后续释放只推进 `rebuild_input_version`。新权重按 epoch+input version+`dispatch_rebuild_snapshot_hash` 与 ready 原子发布，不在原中央份额开启新的 search epoch。若释放事务冲突则整笔回滚；计算期间再释放、相关资源变化或重建失败均丢弃整批未发布权重并从最新 Window 快照重建，不能双扣或暴露部分权重。
- [ ] 同一冻结候选图按 ordinal + 资源 + task fairness key 拆连通分量；依次证明最大 click 数、最大受服务任务数、最大最小任务公平向量和稳定 path 顺序。`no_candidate|optimal|abandoned` 三条路径均有回归；当前 epoch 无法一次给出完整结果时不得提交 incumbent/部分 assignment 或终结 ordinal。
- [ ] 两类求解器和两类 epoch 的职责边界有确定性回归：搜索求解不能修改中央份额，中央求解不能选择账号/关键词路径；一个 search epoch 不重试、不保存 attempt，`no_candidate|abandoned|optimal.unmatched` 均由唯一 epoch 结果行一次成功 finalize 首次 release set；epoch finalized 后的释放只能由唯一 release batch 承载。尚可领取 Window 的非空集合只开启或加入唯一 pending rebuild wave，已结束 Window 只收口事实。
- [ ] 两个 worker 并发发现同一 `(window,dispatch epoch)` 时，只有 open 行中唯一 `solver_owner_lease_id` 的 owner 可以调用求解器，冲突 worker 的求解调用数为 0；健康 owner 超过任意单次租约周期时能够续租且不因耗时 abandoned，只有模拟进程失联/fencing 所有权丢失后 recovery 才直接 `abandoned`，新 worker 不接管求解且不生成 solver attempt/history。
- [ ] finalized search outcome 幂等回放：从 carrier identity、原 `solver_problem_hash/solver_input_hash`、全部 matched assignment identity/version、按稳定顺序重算的 release set、实际 next dispatch epoch/input version 重算出同一 `outcome_hash`；重复调用的 assignment/exclusion/计数/epoch/input version 增量均为 0。篡改 carrier、problem/input、release unit、reason/resource hash 或 wave 版本任一项时保持 `release_fact_incomplete`，不得回读另一版、再次释放或创建 assignment。
- [ ] 冻结 search epoch 后分别注入 Window `rebuild_required`、更高 dispatch epoch 已重新 `ready`、Window ended；三种状态的旧 epoch `optimal` 提交数均为 0，只能 abandoned 并按当前 Window 状态加入/开启 wave或只收口事实。
- [ ] 每条排除事实跨状态永久唯一绑定被放弃的 Reservation/ordinal unit；首次 outcome 与后续 release batch 都在同一整批事务完成全部实际 exclusion、对应的 bound/released/unclaimed 转移和 carrier finalize。测试 post-finalize Gateway 前路径失效、Action 不再到期与 expiry 三种稳定 trigger：同 trigger 同 candidate hash 只回读，不同 candidate hash 冲突；三个不同 trigger 并发命中同一 assignment 时只允许首个有效 carrier释放，后到 trigger `already_released` no-op且不重试。再与 `_confirm_claim` 并发，release 先赢则 claim CAS 失败，claim 先赢则 batch `precondition_lost` no-op，claimed/Gateway-started unit绝不释放。混合集合必须一次释放全部 effective unit并对已释放/前置丢失 unit分类，不能事务级半写。不同 release batch 并发加入同一 pending wave时中央 epoch 只增加一次、`rebuild_input_version` 反映全部实际释放。Window 已结束时只收口事实且不创建 epoch。新 epoch allocation/reservation、Window ready 与同一 `dispatch_rebuild_snapshot_hash` 原子发布，输入变化或重建失败不得暴露部分权重。对 `no_feasible_search_path|search_solver_abandoned`，仅新建 dispatch/search epoch、换 Reservation/ordinal/worker 或推进 carrier 版本时 `solver_problem_component_hash` 必须保持不变且 exclusion 继续 active；改变该 unit 连通分量的业务义务、候选、资源、公平输入或 contract version 才 superseded。其他 reason 只随直接相关额度、授权、代理、协议/CAPTCHA、Gateway 或 assignment/Action version 变化；无关 Task/shard/扫描时间不得触发。Window 结束即 expired，但旧 unit 始终保持 released；新事实只能使用新 Reservation/ordinal。真实状态/计数矛盾时 release 先整批回滚，独立 quarantine writer 复核并暂停原 trigger，reconcile 提交与 resolved 事件后才唤醒；合法 no-op 不 reconcile，不得进入无限重试。极搜账号 24h eligibility 排除只作为候选输入，不能直接冒充中央 exclusion；到期后路径重新进入候选快照。
- [ ] 增加自失效循环回归：冻结完全相同的业务义务/候选/资源/公平图，使 search epoch abandoned 并触发中央重建；仅更换 dispatch/search epoch、TaskAllocation/Reservation ID、unit ordinal和 worker 后，原 unit 的 `solver_problem_component_hash` 必须相同，active exclusion 继续扣除同 task/lane/shard 的同问题份额，不得再次把相同问题交给搜索 solver。再分别改变相关候选资格、额度/授权/代理/协议/CAPTCHA/Gateway 版本、fairness 输入或 `solver_contract_version`，只有受影响连通分量的 hash 变化并 supersede；改变无关分量不得连带解禁。
- [ ] 增加搜索快照恢复回归：在 open epoch 与 solver 调用之间杀死 owner，recovery 必须仅凭已持久化 problem snapshot/component/unit binding 为全部未领取 unit 生成与原图一致的 component exclusion，重新组图/额外查库调用数为 0。分别删除一个 component、unit binding 或修改 canonical payload，必须保持 `release_fact_incomplete`/对象级 quarantine 且零释放、零重建；共享 resource/fairness key 被错误拆分时快照事务整体失败。用同一 Assembler 对当前事实重算 component，证明 supersede 与 solver 输入没有第二套 hash 口径。
- [ ] 增加搜索 precommit 回归：冻结 solver snapshot 后，分别新增/删除候选 edge，或改变账号剩余安全容量、冻结额度窗口内已确认 click 数、持久机会时间、cursor、eligibility、中央 Reservation/version；即使 Window epoch/input version 未变，旧 `optimal|no_candidate` 也必须写入 0 条 assignment并整轮 abandoned/release/rebuild。仅改变 worker/lease、墙钟、扫描时间或诊断字段时 problem hash 不变。断言所有账号决胜字段都能从 canonical payload/source version 反向枚举；遗漏任一字段使合同测试失败。模拟 serialization abort、CAS 冲突和驱动自动重放，均不得提交旧解、部分 assignment 或第二次 solver 调用。
- [ ] 分片重建快照竞态必须有确定性回归：冻结重建输入后，在不创建 release batch、也不增加 `rebuild_input_version` 的情况下分别改变 due、eligibility、active exclusion、有效旧 Reservation 计数、Scope/Window/Shard active/unclaimed、任一 fairness cursor、parent/sponsor 聚合输入、容量、相关配置和 `dispatch_rebuild_contract_version`；precommit 重新构造完整 `DispatchRebuildInput` 后，旧提交都必须因 hash 不一致而写入 0 行，下一 drain 以最新事实发布。仅改变 worker/lease、扫描时间、墙钟时间、进程身份、随机种子或纯诊断字段时 hash 必须不变。测试同时禁止 solver 额外查库/读全局，并在成功与零余额两条路径断言 solver input、Window `ready_rebuild_snapshot_hash` 与每条新 TaskAllocation/ShardAllocation/Reservation 的 `dispatch_rebuild_snapshot_hash` 完全一致，且不存在部分发布。
- [ ] 增加 rehash-to-commit 竞态：在 SERIALIZABLE precommit 读完 input 后、commit 前并发更新已读行和插入满足候选谓词的新行，两个旧提交都必须 serialization abort、零 allocation/reservation/ready 写入；确认框架未拿旧输出自动重试，下一 drain 重新 assemble/solve 后才发布。
- [ ] finalized release batch 重放分别篡改 item classification、observed version、first carrier、三类 count、outcome 或 next epoch/input version，必须因重算 `outcome_hash` 不一致保持 `release_fact_incomplete` 且零计数变化；完全一致才零写回读。同 trigger candidate hash 变化仍独立返回 `release_batch_input_conflict`。
- [ ] 增加 Action 双版本竞态：暂停 release 在取得 assignment 后、取得 Action 前，让迟到 worker claim；只能得到 `precondition_lost` 且不改 Action。再让 release 先取得 assignment/Action expected version并提交，断言 Action 已按 trigger 终结、lease/active 清空、绑定保留，迟到 claim/Gateway CAS 失败；禁止任何 `assignment=released + Action pending|claiming` 组合。already-released 只有在原 Action 已不可领取时才合法，否则进入独立 quarantine。
- [ ] 增加 `precondition_lost` 后续版本回归：旧 trigger 以 observed 新版本 no-op finalize 后，若状态已进入 claim/Gateway/unknown/consumed，断言状态机绝不倒退到 `reserved|action_bound` 且永不生成新 release。若 observed 只是仍在 Gateway 前的 assignment/Action 新版本（例如并发 replacement/资格复核推进 version）且释放条件仍成立，只能由产生该新版本的状态变化事务/outbox 生成新 trigger并释放。断言旧 batch 不重开、无轮询忙重试、新 trigger 不复用旧 candidate hash，且 Reservation 不泄漏。
- [ ] 覆盖 release fact set 完整性和 Reconciler 四个互斥分支：只有 carrier、只有 exclusion、缺 effective item、hash/版本错绑均保持 `release_fact_incomplete` 且不得自动判 released；完整合法 release 无 claim时对齐 assignment/Action并从逐 unit 事实重算摘要；孤立 released 且无任何 release 组件时恢复并推进版本；只有 claim/Gateway且无任何 release 组件时向远端事实对齐；完整合法 release fact set 与 claim/Gateway 同时存在时保持 `release_claim_fact_conflict` active quarantine，不删除 release 组件、不回滚 Gateway、不选边、不调整该 unit released/claimed 计数、不 resolve或忙重试。完整 click evidence可入账，但相关 ledger 的 E4 必须被阻断。前三类提交后才允许 resolve并唤醒原 trigger。
- [ ] carrier/item/exclusion/来源 Reservation 保留与联合归档有确定性回归：仍可能迟到写入时拒绝单独删除；旧 worker 未 fence 时拒绝归档；完成 fence 后只冷存 payload，主库 carrier/unit identity tombstone 与 batch item 的 candidate unit、assignment/Action expected+observed version、classification/first-carrier 引用仍唯一且不可删除，迟到重放不能复活旧 Reservation或丢失 no-op 事实。
- [ ] `search_rank_deboost` 缺豁免群或协议样本时，结构合法 Task 仍先创建；启动 operation 为 `started`，该 scope 的 `runtime_state=waiting`，补齐事实后自动继续，不回滚 Task。
- [ ] hot-list/unknown 不触发任何 reset 命令或 callback；历史 reset 字段不被新执行改写。
- [ ] deadline 前/后 click 确认、时区修改和新日新 click 均有确定性回归。
- [ ] 存量混合任务只显示 `legacy_mixed_search_join`，历史事实不变，不能从纯点击入口编辑；未来 `click_and_join` 不得出现在可创建模式、API schema、迁移目标或本次 QA/release gate 中。
- [ ] 日目标超出实时安全容量时不伪造 Action 或成功。
- [ ] 权限词表包含 `tasks.create.search_click` 且运营角色模板按产品口径授予；`POST /api/tasks/search-click[/create-and-start]` 同时要求 `tasks.manage + tasks.create.search_click`，只具备前者时返回 403。AI 活群、评论、点赞、浏览的新建入口只要求 `tasks.manage`，不得发明或校验四类专项权限；旧 `tasks.create.search_join_group` 不得授权任何新建任务，旧创建路由继续固定返回 410。

## 6. 前端与可观测实现包

### Task 8：任务向导和详情

主要文件：

- `frontend/src/app/views/TaskCenterWizardSections.tsx`
- `frontend/src/app/views/TaskCenterDetailModal.tsx`
- `frontend/src/app/views/taskCenterViewModel.ts`
- `frontend/src/app/AppModals.tsx`
- `frontend/src/app/AppShell.tsx`
- `frontend/src/app/views/TaskCenterView.tsx`
- `backend/tests/test_frontend_permission_gating.py`
- `docs/00-index/project-structure-index.md`

- [ ] AI 只配置每群每日发送量；移除硬小时、活动窗口和容量 gate 字段。
- [ ] 创建确认页只展示输入摘要与结构错误；结构合法直接创建，不展示必须确认的容量/准入/传输 warning。运行事实统一在创建后的详情页展示。
- [ ] 创建响应区分首次 201、幂等重放 200、同键不同 fingerprint/并发启动冲突的 409、授权 403/404 和结构错误 422；`start_status` 与 `runtime_state` 分开展示。`start_failed` 页面必须保留 task_id 并只提供“重试启动原任务”，`started + waiting` 跳转任务详情且不能再次提交创建表单。
- [ ] 静默时间明确显示“降量发送，不停发”。
- [ ] 展示配置群日目标、冻结账号数和有效目标。
- [ ] 搜索创建页固定展示“搜索点击”与每日 click 目标，不展示入群开关、入群日目标或 `click_and_join` 选项；提交体固定 `search_execution_mode=click_only`。
- [ ] 权限管理页可独立配置 `tasks.create.search_click`，前后端“运营管理员”模板在该权限上保持一致。无 `tasks.manage` 时不显示创建入口；仅有 `tasks.manage` 时仍可创建 AI 活群、评论、点赞、浏览，但纯搜索点击类型禁用且提交前再次拒绝；同时具备 `tasks.manage + tasks.create.search_click` 时才允许选择并提交纯搜索点击。
- [ ] 搜索不展示账号容量或账号优先级配置；详情只读展示系统选择原因、`remaining_click_count/planning_click_deficit/hard_safe_attempt_capacity/catch_up_required`、click 事实链和 blocker，不展示 admission 阶段。
- [ ] AI 详情把逻辑状态显示为“待群内可见确认（`pending_visibility_hold`）”，不得沿用物理表名中的 credit 暗示已成功；pending visibility 和 unknown 分列原因，但总计只进入一次 unknown 占位。
- [ ] 搜索诊断只读展示 `dispatch_allocation_epoch/search_click_assignment_epoch/solver_input_hash/solver_result`、两类求解器结果、Reservation 的 bound/claimed/released 计数及当前 Window unit-level exclusion；不得展示 solver attempt，也不得把 exclusion 显示成永久禁用或运营可配置项。
- [ ] 自然日详情以 `task_day_ledger_id` 选择，展示本地日期、冻结时区/revision、UTC period 和 day phase；`date=` 多义时返回候选，不合并。
- [ ] 详情拆分准入、内容轮次、签到/表情、transport 和远端结果。
- [ ] 详情分列 direct/reply/normal text emoji/image/sticker/custom emoji 的 planned/success/shortfall/overflow，并将确定性兜底单列。
- [ ] AI/评论同时展示 `quantity_status`、`content_mix_status` 和 `acceptance_status`；总量已完成但内容构成 shortfall 时不得显示完整达标。
- [ ] 点赞/浏览/评论显示每消息目标与真实剩余量。
- [ ] blocked、at_risk、unknown、waiting_transport 不能显示为完成。

## 7. 数据迁移与兼容

### Task 9：显式迁移

- [ ] AI 旧每账号条数只作为迁移输入；新默认群日目标等于冻结账号数。
- [ ] 删除/忽略新运行合同中的 hard-hourly、active-window 和 local group slot 字段。
- [ ] 存量 hard-hourly Action 不转成成功；未发送项按群日义务重新审计。
- [ ] 搜索存量只做分类和隔离：带 membership/admission 配置或 child 事实的任务标记 `legacy_mixed_search_join`，不自动迁为 `click_only` 或未来 `click_and_join`；历史 Task、ledger、Action、Attempt、unknown 和远端事实全部保持原绑定。
- [ ] 为自然日任务建立/回填不可变 `task_day_ledger_id`、`timezone_revision`、UTC period 和 day phase；本地日期不再单独充当唯一身份。
- [ ] AI 任务为当前 ledger 回填固定时区、planning anchor 和账号范围快照；Coverage 唯一键迁移为 `(task_day_ledger_id, account_id)`，范围变更只影响下一 ledger。
- [ ] 回填 AI MessageSlot：每账号最早按时成功先占 coverage，剩余成功占 extra，随后才绑定 unknown/held；超出槽位的成功保留 overflow、pre-Gateway excess 终结、Gateway unknown 保留，归属不明只审计。
- [ ] 现有 `PendingVisibilityCredit/pending_visibility_credits` 只按兼容物理名保留或等价迁移为逻辑 `pending_visibility_hold`；开放行不得回填群日/coverage confirmed。每行必须唯一绑定原 action/主槽/可选 coverage/remote id/admission version；证据不完整进入对象级审计，不猜测成功。
- [ ] 时区修改先写 pending effective_at；从旧 ledger deadline 建立新时区 transition ledger，验证相邻区间无重叠/缺口且历史 Action/Attempt/fact 不改绑。
- [ ] 频道浏览每消息每日义务迁移为 `(task_day_ledger_id, channel_message_id)`，累计总目标不迁入日账本且真实历史成功不搬日。
- [ ] 非 AI 天然键迁移：评论保留原 plan revision/ordinal；点赞从消息首次规划配置冻结 `reaction_contract_version`；浏览 `view_source_key` 只回填 `account:{account_id}`；Session/代理变更不生成新 source。
- [ ] 评论存量规则显式迁移 `comment_mask_policy=required`；无法证明 active 面具快照的开放 Action 在 Gateway 前重排，Gateway-started/success/unknown 只读保留。
- [ ] 仅对 `legacy_mixed_search_join` 的存量 source/child 按既有规划/Gateway 边界证据回填同一 `task_day_ledger_id` 与时间边界；无法证明归属的条目进入审计，不猜测计入任一 ledger。新 `click_only` 不存在 child。
- [ ] 搜索每份 ledger 按已证成功、Gateway-started/unknown、有效 open、pre-Gateway terminal 的稳定顺序分配 `click_obligation_ordinal`；同一既有远端事实只绑定一个 ordinal。归属冲突或证据不足进入 `consistency_quarantine`，不能继续使用 `source_action_id` 生成新业务义务。新 `SearchClickOpportunityAssignment` 只为未进 Gateway 的当前欠额建立，历史 success/unknown 不重配资源。
- [ ] 既有 `DispatchClaimWindow.allocation_epoch` 对外统一投影为 `dispatch_allocation_epoch`；搜索 assignment 若无法证明中央 epoch、搜索候选快照与 solver input 的绑定，只进入 dry-run 审计，不补造 `search_click_assignment_epoch` 或 `DispatchAllocationExclusion`。
- [ ] 存量 Task 不伪造未知创建请求 fingerprint：能够从原创建审计完整重建时才回填；否则标记 `idempotency_legacy_unproven` 并只允许以 task_id 使用显式 start API。新创建请求必须完整写入 fingerprint/start operation。
- [ ] 存量 Task 不回填虚构 `TaskStartOperation`：running/paused Task 按现有 Task/ledger 回读 started，operation ID/version 为 null并标记 legacy_untracked，重复 start 不得再次执行；paused resume 只恢复同一 ledger且不写 operation。draft/stopped 在下一次真实 start、running/paused Task 在明确 stopped 后的首次真实重启时才创建唯一 version 1 当前行。
- [ ] 审计开放 AI/评论 Action 的内容合同快照；证据完整才等价补写，歧义项在 pre-Gateway 显式 replan，Gateway-started/success/unknown 只读保留真实类型。
- [ ] 所有迁移先 dry-run，输出 task_id、旧值、新值、原因和影响量。
- [ ] 迁移脚本不自动启动 paused/stopped/completed 任务。

## 8. QA、发布与 E4

### Task 10：自动化闸门

- [ ] 后端单测、PostgreSQL 并发测试、前端测试和类型检查通过。
- [ ] 文档、schema、API、迁移、UI 和 runtime 状态名完全一致。
- [ ] 旧 hard-hourly、活动窗口和容量 gate 的新建路径测试必须失败。
- [ ] AI 签到、评论表情和纯搜索点击都覆盖正常、失败、未知和并发幂等；搜索点击不产生 admission child。
- [ ] 增加创建无运行预检回归：结构合法但零 ready 账号/容量不足/待审批/Provider 不健康仍创建成功；启动后 blocker 可见且事实恢复后自动继续。
- [ ] 增加创建幂等矩阵：同键同 fingerprint 首次 201/重放 200、同键不同配置 409、事务 B 失败无残留 ledger/Cycle/assignment、响应丢失后任何 key 只回读 started、same key 失败重试覆盖当前行并推进 version、new key 携带正确 `replaces_start_operation_id/version` 后覆盖当前行、错误/迟到 replace 返回 stale、其他请求 processing 返回 `start_in_progress`、并发最多单 ledger、停止后 new key tuple CAS 覆盖，且全程无历史 operation payload。分别注入“B 回滚后暂停旧 failure writer -> same key/new key 新一轮进入 processing/started -> 恢复旧 writer”的竞态，断言 expected previous ID/version tuple CAS 失败、current 不被覆盖且 version 单调。
- [ ] 增加零当前 start operation 与迁移回归：新建未启动 draft 返回 `not_requested/null/null/legacy=false`；存量 running/paused 返回 `started/null/null/legacy=true` 且 start 调用数为 0，paused resume 只恢复原 ledger、不写 operation；存量 draft/stopped 返回 `not_requested/null/null/legacy=true` 且不补历史，首次真实 start 建 version 1；存量 running/paused 明确 stopped 后真实重启也只建一条 version 1 当前行。任何 processing/started/failed 行都必须 ID/version 非空且 legacy=false。
- [ ] 增加账号/目标校验边界：无调用权限返回 403、不可见跨用户引用返回不泄露存在性的 404、当前用户可见但引用类型与任务用途冲突返回 422；inline 公开目标创建只写 pending 引用且 Telegram resolve/probe 调用数为 0；合法引用中的账号运行时删除、用途变化或授权漂移只阻塞该账号，其他账号继续。
- [ ] 增加主 AI 3 轮、备用 AI 3 轮、六轮后确定性兜底与 Provider 阶段审计回归；缺面具/已验证路线切换不得虚构六次调用。
- [ ] 增加内容编排快照非回归：门禁开关差异只能改变排期/领取，不能改变引用槽位和正常素材比例；兜底不能跨配额记账。
- [ ] 增加内容合同解析、早期缺面具、最终素材槽、并发 CAS 转派、数量/内容复合状态回归。
- [ ] 增加确定性兜底素材兼容矩阵：normal-text-emoji/sticker/custom-emoji 必须在 Gateway 前先转派，image 只有批准 profile 可核验时共载；CAS 失败不得发送后补账或重复绑定。
- [ ] 增加全任务 Claim Window 多窗口公平、cursor 持久化和最大余数稳定性回归；AI admission 只在 AI 父任务内部参与，不进入纯搜索点击 lane。
- [ ] 增加单用户 dispatcher scope 回归：同一 scope 只做父业务任务公平，不生成 tenant 级 allocation；隔离键和审计中的 tenant_id 仍保留。
- [ ] 增加 P0-1 占位守恒回归：同一主槽从 pre-Gateway open → `pending_visibility` → `unknown_after_send` 的全过程只占 1；pending→unknown 在同一逻辑 hold 上完成 `-1/+1` 状态迁移，不插入第二条 hold；兼容 `unknown_after_send_hold_count` 与统一 `unknown_count` 不重复累计，且任何阶段均不建立替代 Action。
- [ ] 增加 P0-2 拦截/放弃回归：intercepted 原子关闭当前 hold、失败且不计群日/coverage；未 ready 前无循环试发；`admission_abandoned` 的 permission/preview/reason/evidence/version 缺一即拒绝，成功后分母与 coverage 主槽不变，其他账号不能替代，reopen 只递增 admission version。
- [ ] 增加 P0-3 可见确认 PostgreSQL 并发回归：Attempt+remote id 先不计 confirmed，四 worker 并发 `visible_confirmed` 只成功一次；在 Action、hold、主槽、coverage、远端事实所有权任一 CAS/唯一键处注入失败时确认事务整项回滚，随后独立 writer 复核并持久化对象级 quarantine，不能因同事务回滚丢失隔离事实。
- [ ] 增加 AI ContentMix Cycle 原子分配、20 条物化切批恢复、配置仅影响新 Cycle、settled 条件及评论 revision 生命周期回归。
- [ ] 增加非 AI 自然义务键幂等回归，证明评论 ordinal/reaction contract/account view source/click ordinal 无 `primary_quantity_slot_id` 仍不会重复计数；每键同时最多一个 open/unknown/success，replacement 复用原键，评论/点赞不产生伪 task-day，浏览/click 不跨 ledger 归属。
- [ ] 增加非 AI 远端事实跨 Task 所有权回归：重复评论 remote ID、未变化 reaction、重复 lifetime view 与重复 click evidence 均不能完成第二个义务；事实早于义务起点不能倒灌，冲突对象 quarantine 后其他义务继续。
- [ ] 增加搜索软 pacing catch-up、系统账号排序、合法 repeat 补量与极搜零 reset 回归；断言 admission lane/lease/child 调用数为 0。
- [ ] 增加搜索共享资源匹配回归：候选笛卡尔积不计容量、ordinal + 资源 + task fairness key 连通分量的多阶段字典序精确最优解、最大 click 数不因后续目标下降、资源足够时每个有 eligibility 的到期 Task 至少一条、额外机会按冻结 remaining 比例最大最小公平、稳定 path tie-break、全局无路径 `no_candidate`、局部无路径 `optimal.no_eligibility` 且其他 Task 继续、`optimal` 部分匹配的 served-task/fairness/unmatched/saturated、无法返回完整结果时 `abandoned` 零提交。重复 projection 零写入；commit 的每 Task assignment 不超过中央 fulfillment 份额，双 Task 不重复使用账号/关键词/授权/代理资源，四 worker 只共享一份 Dispatcher/Gateway inflight Reservation；资源绑定 CAS 失败进入 abandoned/unit release，Gateway 调用结束释放中央 inflight，unknown 继续占 ordinal/可能已消费 quota hold但不永久占用在途容量。
- [ ] 增加求解器与 epoch 边界回归：`DispatchLaneShardSolver` 只映射 task-lane→shard，`SearchClickAssignmentSolver` 只在 ready 的已获份额内匹配 path；一个持久 search epoch 只求解一次、只成功 finalize 一次且无 attempt/retry，`no_candidate|abandoned` 即使零 assignment 也有结果行；候选/资源变化、绑定失败或遗留 open 恢复均放弃未领取 unit。健康 owner 跨租约周期续租不视为超时，owner 失联才 abandoned。`optimal` 必须按 Window claimable+ready+exact epoch 三条件提交，更高 epoch 已 ready 仍拒绝旧结果。首次 outcome 与 post-finalize release batch 的 carrier 职责不得互换；释放只开启或加入唯一 pending wave，且不重置 ledger/ordinal/quota/unknown。finalized outcome 以 carrier/input/matched/release/wave 全量 hash 幂等回读，任一错绑进入 `release_fact_incomplete`。
- [ ] 增加 search Reservation 首次 outcome 所有权回归：ready 后暂停 epoch worker，运行通用 no-Action/unclaimed/expiry reclaimer，断言释放调用数与 exclusion 增量均为 0；再覆盖正常建 epoch、Window 先结束时无 solver 直接 abandoned、open 期间任务停止/due 消失由原 epoch abandoned。每条路径 finalize 后断言来源 Reservation `bound+claimed+released=reserved`，不存在第二个首次 carrier。
- [ ] 增加同 Window release set 与排除生命周期回归：`optimal.unmatched|no_candidate|abandoned` 对首次集合内每个未领取 Reservation/ordinal 写一条跨状态永久唯一的 `DispatchAllocationExclusion`，同一事务以 released 上界和各层 unclaimed 非负守卫汇总计数并 finalize search epoch；post-finalize batch 对 effective bound assignment 另以 `bound >= release` 守卫原子执行 assignment released、bound/released/unclaimed 转移。逐 item 断言 candidate 三类数量守恒、hash/outcome 可唯一重算；candidate 集合为空或全部分类 no-op 时中央状态不变。尚可领取 Window 的首批非空 effective release 只递增一次 epoch并置 rebuild_required，wave 内后续 batch 只递增 input version，已结束 Window 不重建。加入同 unit 多 trigger、release-vs-claim、混合 effective/already-released/precondition-lost 回归；合法 no-op 必须终结 trigger且不 reconcile/retry。在 effective 集合第一个/中间/最后一个 unit、计数守卫或 carrier CAS 注入失败时整批回滚。再注入 assignment/exclusion 矛盾，断言 release 事务无半写、独立 quarantine 可见、定时扫描不忙重试、其他 unit/任务继续；reconcile 提交后事件唤醒原 trigger并按新事实收口。重建期间输入变化、新权重第一个/中间/最后一行、ready CAS 或 worker 崩溃失败时结果均不可见且从最新快照整批重建。claimed/active、其他有效旧 Reservation 和 cursor 不回退；只有 reason-scoped 相关资源 hash 改变时 superseded，无关 Task/shard/worker/扫描时间变化时保持 active，Window 结束时 expired，但旧 unit 始终不可再释放或 claim。
- [ ] 增加“无求解性能合同”回归：schema、配置、诊断和状态机都不出现 solver deadline、性能预算、图规模基线、p99、solver attempt 或部分解状态；健康 owner 跨多个 lease 周期持续续租不 abandoned，明确失败/失联才允许 `abandoned -> unit release`。尚可领取 Window 的非空释放加入唯一 pending rebuild wave；已结束 Window 只收口事实。
- [ ] 增加 click 远端事实完整性回归：同一 Attempt 缺 Gateway 开始、目标身份、批准按钮指纹、click 调用、批准 outcome、确认时间或 evidence hash 任一字段都不得确认 ordinal。
- [ ] 增加 AI 任务时区/冻结范围、评论 active 面具、搜索跨日 late 与新 ledger 新 click 回归；不建立跨日 membership/admission child。
- [ ] 增加本地日期重复、正负时区切换、transition ledger、相邻 UTC 区间连续、暂停/恢复不重置 effective_at 及历史事实不改绑回归。
- [ ] 增加 `date=` 多 ledger 返回 409、pending timezone revision CAS、停止任务不建新 ledger，以及 DST 23/25 小时本地日权重回归。
- [ ] 增加同一未截止 ledger 暂停/恢复不重置 anchor、暂停跨 deadline 不建空 ledger、旧 ledger 如实 missed、恢复创建 partial-start ledger 的回归。
- [ ] 增加改 Task 时区不重置账号/关键词安全额度、Telegram/代理/内容冷却、授权锁和 unknown hold 的回归。
- [ ] 增加目标达成后 pre-Gateway excess 终结、Gateway unknown 后确认导致 overflow、禁止跨日抵消和数量验收不通过回归。
- [ ] 所有后端测试单次命令限制 60 秒。

### Task 11：release 与生产 E4

发布路径固定为 `master -> release -> GitHub Actions Deploy Production`。

生产闸门：

- [ ] GitHub Actions 与部署健康通过。
- [ ] 若两类 solver contract version 有变化，部署证据证明旧 Dispatcher 已停止取得新 ownership、旧进程和可提交事务均归零后才启动新版本；无混合版本 canary、无旧内存权重恢复，遗留 open epoch/rebuild 按新合同收口。
- [ ] worker 无持续 deadlock、claim 饥饿或任务级长事务。
- [ ] AI 完整自然日达到群日目标，冻结账号全部至少 1 条。
- [ ] AI P0 生产事实守恒：pending visibility/unknown 对每个主槽合计只占 1；需核验 remote id 在 `visible_confirmed` 前不计群日/coverage；并发 finalize 无部分提交；intercept/abandon 不缩冻结分母且未被其他账号替代。
- [ ] 静默小时存在非零发送，且平均发送量低于正常小时。
- [ ] AI 三类兜底均有真实远端 ID；生成失败样本具有主 3 轮、备用 3 轮审计，无路线样本保持 `waiting_transport`。
- [ ] 评论逐消息达到目标，表情兜底有远端评论 ID。
- [ ] AI/评论的引用及普通 emoji/图片/表情素材 `planned/success/shortfall/overflow` 与远端事实一致，content mix 违规=0，fallback 未被混入正常占比。
- [ ] AI/评论 `quantity_status` 与 `content_mix_status` 均为 `met`，`acceptance_status=met`；Contract/Obligation/Action/Attempt 可重算结果一致。
- [ ] 点赞、浏览按逐消息目标和截止时间完成。
- [ ] 纯搜索点击只有在 `confirmed_click_count = daily_click_target_snapshot`、每条成功具备唯一完整 click 事实、held/unknown/terminal shortfall/overflow/open excess 均为 0 且无 active consistency quarantine 时才为 met；账号由系统自动排序，极搜无 reset 动作，且未创建任何 membership/admission child。
- [ ] 搜索诊断中的两类 solver/epoch 绑定正确；`no_candidate|abandoned|optimal.unmatched` 的未领取 unit 均已原子释放，只有非空 release set 才重建分片权重，重复 finalize 无双扣；active exclusion 仅属当前 Window/资源快照且能按资源变化 superseded、按 Window 结束 expired。
- [ ] 搜索 click 的按时/late 归属正确，旧日 missed 不回写，新日完成有新 click 证据；存量 `legacy_mixed_search_join` 的历史事实未被改写。
- [ ] 同一生产窗口内 AI、评论、点赞、浏览、纯搜索点击和 AI 准入均有与到期债务一致的 Claim Window 份额，无固定类别饥饿。
- [ ] 所有 `unknown_after_send` 均未产生自动重复发送。

只有以上自动化、release、runtime 和真实 Telegram E4 同时成立，才允许写 `production_fixed`。

## 9. 设计完整性结论

本设计能解决当前“任务被内部门禁提前停止、目标粒度错误、兜底后仍不执行、搜索点击被额外准入链拖住、统计早于真实成功”的系统性问题。开发交接同时冻结：AI `pending_visibility` 单占位、intercept/abandon 不缩覆盖分母、`visible_confirmed` 原子计账三项 P0；`DispatchLaneShardSolver` 与 `SearchClickAssignmentSolver`、`dispatch_allocation_epoch` 与 `search_click_assignment_epoch`、unit-level `DispatchAllocationExclusion`、唯一 search epoch outcome、非空 release set 一次释放、完整 immutable rebuild input/hash、SERIALIZABLE 原子发布和 contract version 全量 fence 切换的唯一含义；同时冻结 `membership_admission` 边界、非 AI 稳定天然义务键、纯搜索点击共享资源 assignment、中央六级锁序与搜索扩展锁序、Gateway 前素材兼容/转派、创建 fingerprint/start operation 幂等、静态引用错误与运行账号身份失效的边界。Task 1-9 开发、Task 10 独立自动化闸门和 Task 11 release/E4 缺一不可。设计不能也不应把无登录态、无授权传输路线、目标群解散或 Telegram 明确拒绝伪造成完成；这些情况保留义务并显示准确 blocker。完成优先的含义是：只要存在合法真实执行路径，就持续规划、使用确定性兜底并追到远端完成，而不是降低目标或吞掉失败。“搜索点击加入”仅为后续独立模式占位，不属于本计划的开发或验收范围。

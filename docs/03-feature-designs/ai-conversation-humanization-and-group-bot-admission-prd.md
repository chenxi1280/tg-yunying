# AI 群聊、频道评论真人化与群管机器人准入专项 PRD

## 1. 文档状态

> **2026-08-23 当前合同边界：** 本文只保留真人化内容、可信提示识别和历史事故取证。本文所有 C2 数据模型、hard-hourly/冻结分母、迁移、ClaimClass、Task 内远端事实所有权、串行 admission 和旧签到配额/重试规则均为 `historical_do_not_implement`。当前 C2 必须使用：`tasks.group_ai_prejoin_channel_ids` 独立 JSON 字段持久化 0～3 个由公开 `t.me` 地址或 username 归一化得到的频道引用并并发关注；Task 专属 `TaskGroupBotAdmission` 仅做投影；`account_group_admission_facts` 以无 `task_id` 的四类远端事实 `configured_channel_follow|dynamic_channel_follow|requirement_confirmation|post_follow_visibility` 供多个 Task 引用；展示名+要求链接精确绑定；不同可信 action 按依赖图并发。签到统一为 `content_source=check_in`。完整唯一合同见 `task-fulfillment-classified-recovery-prd.md`、`task-fulfillment-contract-closure-prd.md` 与 `ai-group-daily-group-target-redesign-prd.md`。

> **2026-08-03 C2 最新 supersede：** AI 活群先关注任务配置的 0–3 个频道，再入群读取可信群管提示。明确收件人按“归一化展示名精确匹配 + 同一可信 bot 原消息内的群聊要求链接/按钮”组合绑定；一条可信提示可有 0/1/2/N 个 requirement action，不限总数。每个 `source_message_id + fingerprint + requirement_action_key` 最多一个 open click/一次 success，unknown 只复探不重点；不同非冲突 requirement action 可并发。本条取代本文后续“每 admission 最多一条 confirmation action”和“同账号整个 admission 全串行”的冲突表述；可信来源、按钮精确身份、完成事件、可见性和 unknown 防重仍保留。完整当前合同见 `task-fulfillment-classified-recovery-prd.md` §5 和 `task-fulfillment-contract-closure-prd.md` §6。

| 项目 | 内容 |
| --- | --- |
| 需求级别 | L2 产品能力升级（上线后影响生产 AI 活群 / 频道评论行为） |
| 设计状态 | `partially_superseded`：仅真人化内容参考可复用；C2/签到/调度运行合同不得据本文进入 dev |
| 修订说明 | 2026-07-25 评审修补合订 + **continuity 交叉 P0/P1 合订**：① 待可见性核验计入 `unknown_after_send_hold_count`；② `admission_abandoned` 释放永久不可准入硬小时 debt；③ `pending_visibility_credit` 延后真实 credit；④ follow/观察 action 复用 `target_admission_retry` 档且限 tenant+task+account；⑤ 定义 `admission_version`；⑥ C1/C2 action 边界与存量 unknown 走 continuity 裁决。2026-07-27 首次补齐：入群前基线游标、每轮 listener observation 落库、控制事件优先后闭合、存量无基线显式重启观察，以及成功终态压过历史临时错误展示。**同日生产复核再补齐：来源信任必须早于归属；可信 peer 只是候选来源而不是“每条消息都是控制指令”；频道与确认动作可仅存在于 Telegram 内联按钮；已审计的目标级 bot peer 可作为 unknown role 的受限信任根；同群新入群 admission 必须串行，避免并发提示无法归属；历史已入库的同一 bot 消息重新被监听到按钮时，只回填安全按钮摘要以支持精确恢复；频道 follow 的持久 Action 类型固定为 `group_bot_channel_follow`，必须适配 `actions.action_type` 的 30 字符上限；`required_channel_refs` 只代表当前世代，误判提示暂停后必须由 explicit restart 与不同 source 的新有效控制提示受控 rearm；带明确收件人的控制提示必须精确归属，不能被唯一 waiting account 兜底错配。生产 E4 再次发现“空 admission 放行后 Telegram 先返回 message id、机器人稍后删除并提示订阅”后补齐：空/无证据 admission 的首条正文必须进入完整可见性窗口，窗口结束前不得以瞬时可见确认成功；unknown-role bot 只有在同 peer 重复出现相同精确频道+callback 规则，且与同群开放的 `pending_visibility` 远端消息顺序和时间窗相关时，才可作为 `post_send_intercept_rule` 受限信任根展开逐账号准入。** |
| 产品范围 | 真人化：`group_ai_chat` + `channel_comment`；群管机器人准入：仅 `group_ai_chat` |
| 统计时区 | 任务配置时区；未配置时沿用平台 `Asia/Shanghai` |
| 上位文档 | `docs/01-product/tg-ops-platform-prd.md` |
| 关联文档 | `ai-group-send-continuity-and-terminal-targets-prd.md`（终态目标 / OutboundTargetGate / unknown 占位 / hard-hourly credit 优先）、`ai-group-all-accounts-daily-coverage-prd.md`、`ai-group-hard-hourly-target-prd.md`、`docs/00-index/project-dataflow-index.md` DF-183 |
| 实现计划 | `docs/superpowers/plans/2026-07-25-humanized-group-and-channel-interactions.md` |

### 1.1 专项优先级与 supersede

本文对以下主题具有**新实现优先级**：

1. 会话账号轮换、原生引用默认策略、`签到` 唯一确定性兜底与真人化质量门；
2. AI 活群群管机器人前置准入、`GroupBotAdmissionPolicy`、控制观察与发送后可见性；
3. 与日覆盖 / hard-hourly / membership 的交叉门禁（ready 容量、禁止发送后自动关注重发）。

冲突决议：

| 旧口径来源 | 旧说法 | 本专项生效后 |
| --- | --- | --- |
| `ai-group-hard-hourly-target-prd` | 正文发送后被要求关注频道 → 自动关注 → **原 action 重排重发** | **禁止**。只允许 `post_send_intercepted` / `legacy_group_bot_intercepted` / `unknown_after_send`；义务按 continuity 规则释放或保持占位，不得在同一次正文后静默重发 |
| `ai-group-all-accounts-daily-coverage-prd` | 三层失败后 `emoji_react` 质量兜底 | **改为**精确文本 `签到`（受 `static_safe_fallback` 与签到配额约束）；`emoji_react` 仅作历史说明 |
| `ai-group-topic-teacher-burst-prd` / 旧配置 | `consecutive_message_*` 同账号连发 | **删除**字段与 Planner burst；运行时不保留兼容连发分支 |
| `ai-group-provider-fallback-and-safe-prompt-design` | 静态兜底可含表情池 / 泛化短句 | 群聊与频道评论确定性兜底**仅** `签到` |

**不覆盖、仍以上位/关联专项为准：**

- 目标生命周期 `group_dissolved` / `target_ref_invalid`、`reference_revision`、OutboundTargetGate（continuity PRD）；
- 硬小时计划桶 credit、`unknown_after_send` 占位 1、跨小时 debt（continuity PRD）；
- 日覆盖分母不缩、远端 message id 确认完成（日覆盖 PRD + 本专项可见性附加条件）。

本文不代表代码、迁移或生产发布已经完成。

### 1.2 continuity 交叉 P0/P1 决议清单（已关闭，实现必遵）

| ID | 问题 | 决议 | 落点 |
| --- | --- | --- | --- |
| **P0-1** | 「待可见性核验」是否进 `planning_reservation` | `pending_visibility` **计入** `unknown_after_send_hold_count`（与 unknown 同语义占位 1）；**不**新增公式第三项；`planning_reservation = eligible_open + unknown_hold` **不变** | §5.8.1；continuity §4.3 / §7.3.1 |
| **P0-2** | `post_send_intercepted` 后账号永久不可 ready 时 debt 怎么办 | 长期 waiting **不**自动释放；仅运营 **`admission_abandoned`**（preview+理由+证据+version）后：该账号该目标 epoch 未关闭硬小时义务从 `durable_debt` **排除**；日覆盖分母保留 `blocked/admission_abandoned` | §5.8.2；continuity durable_debt 排除 |
| **P0-3** | Attempt success+remote id 是否立即 hard-hourly credit | **否**（需可见性核验时）：先 `pending_visibility_credit`（不涨 `success_count`），`visible_confirmed` 后同一短事务落正式 `TaskHardHourlyDeliveryCredit`；无需核验的消息仍按 continuity 直接正式 credit | §5.8.3；continuity §7.2 增量 |
| **P1-4** | `group_bot_channel_follow` 的 ClaimClass 档位 | **复用 `target_admission_retry` 档**；就绪集限制 `tenant_id+task_id+account_id`（+目标群），禁止跨任务饿死 `search_join_*` | §8.3；continuity §7.4 注 |
| **P1-5** | `admission_version` 未定义 | 行字段，从 1 起；rejoin / trusted bot peer / 绑定 policy 结论变化 / 新规则集 / intercepted 后重入观察时 `+1`；**≠** `Task.config_revision` | §5.1.1 |
| **P1-6** | C1 存量新建 action 是否受 admission 拦截 | **否**：C1 存量新建 = legacy 路径 + `legacy_send_until_reviewed`；C2 复核完成**只影响之后**新建 action，不回溯已发/open | §10.2.1 |
| **P1-7** | C2 发现的存量 unknown 走哪条路 | **只走 continuity 核验/裁决**；不进 admission 状态机；裁决成功不自动 admission ready | §10.2.2 |

## 2. 背景与问题

1. 同账号在无真人间隔时连续发言、默认不引用回复、泛化模板兜底，使群聊/评论可被识别为机器人。
2. 群管机器人“关注频道后发言”依赖发送失败后补救；首条正文可能已被删，且 `can_send` 与群管放行被混为一谈。
3. 控制观察若无闭合条件，账号会永久等待；若用固定秒数自动放行，又会漏拦延迟机器人提示。
4. 无 `not_required` 策略时所有“无机器人群”会系统性卡死；策略又要求观察证据，形成鸡生蛋。
5. 强制轮换在单账号任务上会与 hard-hourly / 日覆盖目标冲突，若静默放宽又破坏真人化。
6. 上一次终态任务完成 PRD 已明确：不得把引用失败当解散、unknown 不得整目标停规划也不得替代重发；本专项必须复用同一套发送事实语义，而不是另起一套“成功”定义。

## 3. 目标、范围与非目标

### 3.1 目标

- 群聊 / 频道评论在无真人打断时相邻平台消息必须换账号；无替代账号时显式等待，不静默连发。
- 有合格候选时默认产出真实 `reply_to_message_id`；短缺可审计，不伪造引用。
- 质量门拒绝模板壳等“像 AI”内容；唯一确定性文本兜底为可审计的 `签到`。
- AI 活群新入群账号在 AI / 正文 / 测试消息之前完成可审计群管控制处理。
- `can_send` 与 `GroupBotAdmission` 独立；`probe.ok` 不能单独 ready。
- 与 continuity 一致：成功需要 Attempt + 非空远端 message id；需要可见性核验的首条正文在核验通过前不得 credit 覆盖 / 硬小时。

### 3.2 范围

| 能力 | `group_ai_chat` | `channel_comment` |
| --- | --- | --- |
| 会话轮换 | 是（会话键=目标群） | 是（会话键=绑定讨论区） |
| 原生引用默认 | `reply_min_per_round` 默认 1 | `comment_mode=mixed`、`reply_min_per_message` 默认 1 |
| `签到` 兜底 + 质量门 | 是 | 是 |
| 群管机器人准入 | **是** | **否**（讨论区群管规则为本专项非目标） |

### 3.3 非目标

- 不把群管准入扩展到浏览、点赞、转发、人工发送、搜索点击。
- 不把讨论区 / 频道评论的群管机器人 mute 规则纳入本版自动准入（若发生，记 blocker，不静默试发）。
- 不根据固定等待时长、私聊提示、非管理员 bot、成员转发文本自动放行。
- 不把 `PEER_INVALID` / `qdsfxy` / 群名相似写成 `group_dissolved`（沿用 continuity）。
- 不删除历史 Action / Attempt / 覆盖 / 准入审计事实。
- 不因真人化绕过账号容量、活动窗、OutboundTargetGate、内容安全或 Telegram 风控。

## 4. 核心定义与不可混淆事实

| 事实 | 真相源 | 含义 | 禁止伪造 |
| --- | --- | --- | --- |
| Telegram 传输权限 | `TgGroupAccount.can_send` | Telegram 报告是否可发言 | 不得因等关注频道被业务改成 false；不得因准入 ready 把未知改成 true |
| 群管机器人准入 | `GroupBotAdmission` | 该账号在该群是否完成可信群管协议 | 不得用 `probe.ok` / 关注成功单独推进 ready |
| 完成协议 | `GroupBotAdmissionPolicy` | `not_required` / `explicit_bot_confirmation` / `follow_sufficient` | 不得写进任务 JSON 或租户全局默认自动推断 |
| 会话相邻发言 | `ConversationSpeakerTurn` + `ConversationSpeakerState` | 真实远端顺序上的 last platform / human | 不得用 Planner 本地顺序覆盖 |
| 日覆盖义务 | `TaskAccountDailyCoverage` | 当日是否欠发送 | 不得跨账号 rebind `coverage_ledger_id` |
| 硬小时义务 | continuity 计划桶 + credit | 小时目标与跨小时 debt | 不得用 UI 或 payload 推断成功 |
| 目标生命周期 | `OperationTarget.lifecycle_status` + revision | 可否出站 | 群管准入不得改 lifecycle |
| 远端可见性 | 发送后核验记录 | 首条准入后正文是否仍可见 | Gateway 回执 ≠ 群内最终可见 |
| 准入世代 | `GroupBotAdmission.admission_version` | 规则/策略/rejoin 变更世代 | 不得用 `Task.config_revision` 代替 |

发送成功（可计覆盖 / 硬小时 **正式** credit）必须同时满足 continuity 的 Attempt 成功 + 非空远端 message id，且若该消息处于「待可见性核验」，须 `visible_confirmed` 后才落正式 credit。  
**规划占位：** 「待可见性核验」与 `unknown_after_send` **同一占位语义**，计入 continuity 的 `unknown_after_send_hold_count`（见 §5.8.1）；`planning_reservation` 公式**不变**。  
`post_send_intercepted` 不计成功；明确失败后不占 `planning_reservation`（continuity §7.3.4）。

**成功事实展示契约：** `Action.status=success` 且该发送有非空 `telegram_msg_id` / `remote_message_id` 时，Task Center 的结果、失败类型、失败原因和诊断必须以成功事实为准。一次性准入错误（例如 `required_channel_admission_pending`）在成功写回时必须清理；历史 action 即使保留该原始字段用于审计，读取投影和前端也不得将其显示为当前“需关注频道”或失败。`pending` 的准入等待与 `success` 的远端发送绝不可共用同一展示语义。

## 5. 群管机器人准入

### 5.1 状态机

```text
joined / rejoin
  -> awaiting_group_bot_rule          # 控制观察进行中（默认）
       |-> observation_open           # 游标连续追平但闭合窗口未到期，仍观察
       |-> group_bot_rule_clear       # 观察闭合 + 无可信规则 + active not_required
       |-> group_bot_policy_unresolved# 观察闭合 + 无可信规则 + 无 not_required
       |-> required_channel_follow_pending / following_required_channel
       |-> awaiting_group_bot_confirmation
       |-> group_bot_rule_unattributed
       |-> blocked / observation_stale

group_bot_rule_clear + can_send=true + 其他 membership ready
  -> group_bot_admission_ready

awaiting_group_bot_confirmation
  -- confirmation_event | follow_sufficient(policy) --> group_bot_admission_ready

任意 clear/ready
  -- 触发 admission_version 递增的规则/策略/rejoin 事件 --> required_channel_follow_pending 或重新观察（见 §5.1.1）

group_bot_admission_ready
  -- 首条/规则变更后首条正文被可信 bot 拦截 --> post_send_intercepted

任意非终态
  -- 运营显式 admission_abandoned --> abandoned（永久退出 ready 产能，见 §5.8.2）
```

说明：

- `observation_open` 是观察子状态，可与 `awaiting_group_bot_rule` 同属“未闭合”，UI 可合并展示为“观察中”。
- `group_bot_admission_ready` **不等于** membership 总 `ready`：总 ready 仍需 `can_send=true` 且通过验证码等既有相位。
- `abandoned` 是运营终态，不是系统自动推断；放弃后该账号在该群不再自动重新进入观察，除非运营显式 reopen + 新证据。

### 5.1.1 `admission_version` 定义

| 项 | 口径 |
| --- | --- |
| 归属 | `GroupBotAdmission.admission_version`（整数，从 `1` 起）；**不是** `Task.config_revision`，也不是 `OperationTarget.reference_revision` |
| 作用 | ① 失效绑定旧 version 的未进 Gateway action / 覆盖预约；② 判断是否需要再次做「首条正文可见性核验」；③ 审计规则变更世代 |
| 递增条件（任一发生即 `+1`） | ① 新 join/rejoin 成功并重置或新建 admission；② `trusted_bot_peer_id` 变更或 `group_bot_multi_bot_conflict` 后运营指定新 peer；③ 关联 active `GroupBotAdmissionPolicy.policy_version` 对应该 admission 的 completion_policy 生效/撤销导致协议结论变化；④ 可信 bot 下发**新** required channel 规则集（相对当前已记录 refs 有增删）；⑤ 从 `post_send_intercepted` / `blocked` 经运营或系统重新进入观察链路 |
| 不递增 | 仅刷新 `observed_end_cursor`、仅写 transport probe、仅更新展示字段、仅 policy 创建但未绑定到本 admission 的 reconcile 结果尚未应用 |
| 读取语义 | 发送门禁与可见性触发比较 **action 创建时冻结的 `admission_version`** 与 **当前行 version**：不等则未进 Gateway 的 action 必须重核，不得带着旧 version 正文进 Gateway；version 升高后的**下一条**正文重新进入 §5.8 可见性核验 |
| 与任务配置 | `Task.config_revision` 只跟任务话题/硬小时等配置；admission 跨任务共享，换任务不重置 version，除非发生上表递增条件 |

### 5.2 控制观察：闭合条件 ≠ 放行条件

#### 5.2.1 游标契约

- 入群 / rejoin **开始前**记录 `join_start_cursor`（该群当前可读最大远端顺序游标；若不可读则失败并 `observation_stale`，不得假装从 0 开始）。
- 增量读取必须 `cursor > join_start_cursor`，连续推进到监听/Gateway 返回的 `observed_end_cursor`。
- 每次观察批次保存：`join_start_cursor`、`observed_end_cursor`、`listener_account_id`、读取条数、缺口标记、失败码、`observation_version`。
- **禁止**用“最新 N 条历史快照”证明已观察完入群后事件。

#### 5.2.2 观察闭合窗口（只决定何时结束观察，绝不自动放行）

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `group_bot_observation_window_seconds` | `120` | 目标级可覆盖，范围 60–300；仅用于“无新可信规则时何时闭合观察” |
| 游标追平 | 必须 | 窗口到期时仍须 `observed_end_cursor` 连续可达；有 `cursor_gap` / 监听失联 / 限流则**不闭合**，保持 `observation_stale` 或继续 `awaiting` |
| 窗口内收到可信规则 | 立即离开观察 | 进入归属与关注链路，不等待窗口结束 |
| 窗口用途 | 仅闭合 | **不得**因窗口到期写 ready / clear（clear 仍要 `not_required`） |

闭合判定算法：

```text
if cursor_gap or listener_down or read_rate_limited:
  stay awaiting / observation_stale; do not clear
elif trusted_rule_attributed:
  leave observation -> follow/confirm path
elif now >= join_success_at + observation_window
     and continuous_read_to_end:
  if active not_required policy:
    -> group_bot_rule_clear
  else:
    -> group_bot_policy_unresolved  # 可一键建 not_required
else:
  stay observation_open
```

#### 5.2.2.1 运行时观察执行契约（2026-07-27）

1. `join_start_cursor` 必须在 Telegram join/rejoin Gateway 调用**之前**，由同群既有、可用 listener 账号读取当前最大数值远端消息 ID 后写入 membership action result。没有 listener、读取失败、返回无数值游标或目标群未解析时，join 本身可以按既有 Telegram 结果收口，但新建 admission 必须立即进入 `observation_stale`，`failure_code=join_start_cursor_missing` 或明确读取失败码；禁止把空值、`0`、最新 N 条的最低 ID 补写为基线。
2. 每次 `collect_group_context()` 成功取得快照后，必须对同群所有 `awaiting_group_bot_rule` / `observation_open` admission 写一条 `GroupBotAdmissionObservation`，包括 listener account、读取条数、批次最低/最高数值 ID、基线、版本、失败码和 `cursor_gap`。没有新 bot 文本也必须写观察批次，不能只靠收到控制消息时推进状态；若 listener 拉取异常触发 worker rollback，rollback 后仍必须以 `listener_fetch_failed` 单独落库并写 listener 错误，禁止把读失败回滚成无记录。
3. 本版本的“连续追平”定义为：单次成功拉取的最新窗口同时包含不大于 `join_start_cursor` 的数值消息和不小于该基线的当前最大数值消息，且 Gateway 未报告读失败、分页缺口或限流。Telegram 消息 ID 不要求相邻数字；若批次最低 ID 大于基线，则只能证明窗口截断，写 `observation_stale/cursor_gap`，不得闭合。
4. 同一轮顺序固定为：**先**逐快照处理可信群管 bot prompt / confirmation，**再**写批次观察并按窗口时间调用 close。这样同轮的真实规则不能被“无规则闭合”覆盖。
5. `close_observation_if_due()` 只能消费当前 `admission_version` 的有效 observation；没有有效 observation、基线缺失、读失败或 gap 时不得从 waiting/stale 进入 `policy_unresolved`、clear 或 ready。

#### 5.2.2.2 存量无基线 recovery

已上线且 `join_start_cursor` 为空的 admission 不得批量推定为 ready。`targets.manage` 可以调用“重启观察”：服务从该群已持久化 listener context 取得当前最大数值游标，要求 `expected_admission_version + reason + evidence_ref`，递增版本、清空旧观察终点并开始新的 60–300 秒窗口，同时写 `AuditLog`。没有当前数值水位时返回显式 `join_start_cursor_missing`；重启后的放行仍只能来自可信规则完成或闭合观察后的显式目标级 policy。

#### 5.2.3 控制事件处理顺序

控制事件必须在普通上下文去重、忽略 sender、学习样本、引用候选之前处理。空正文、被 ignore 的 bot、不进学习样本，均不得丢弃控制处理。控制正文只进准入审计，不进 AI 上下文。

### 5.3 可信来源、归属与多 bot

控制消息的处理顺序固定为 **来源过滤 → 归属 → 协议解析 → 状态迁移**。任何实现不得先读取 waiting admission、更不得先把 `group_bot_rule_unattributed` 写回，再判断 bot 是否可信。

1. **首次自动信任**只接受 `is_bot=true` 且发送者为群管理员/群主，记录 `trusted_bot_peer_id`。
2. 已有 admission 后，同一 `trusted_bot_peer_id` 的后续规则、关注完成和确认事件可继续被接受。
3. 当 Telegram 无法取得 bot 的管理员角色（`sender_role=unknown`）时，只有 active 的、目标级且审计完整的 `explicit_bot_confirmation` 或 `follow_sufficient` policy 绑定**同一 group + 同一 peer**，才可把该 peer 作为受限信任根；`not_required` 绝不能建立 bot 信任。policy 不是“消息看起来像群管”的猜测，必须由 `targets.manage` 根据原始控制消息、按钮布局和 peer 证据创建。
4. 可信 peer 只表示该 sender **可以进入控制提示识别器**，绝不表示该 peer 的每条发言都是控制事件。可信 peer 的普通推广、内容发布、联系人/频道广告，即使恰好能归属到唯一 waiting account，也只可留只读上下文审计，**不得**改变 admission 的 state、failure_code、trusted peer、source message、频道子动作或 ready 结论。
5. 非 bot、未知 peer、普通成员转发、私聊提示，以及未命中上述任一信任条件的 bot：只可留只读审计/指标，**不得**改变任何 admission 的 state、failure_code、trusted peer、频道子动作或 ready 结论。
6. **多 bot**：另一可信 admin/policy bot 在 admission 未 ready 前发出规则，且当前尚无 trusted peer → 采用该 bot；已绑定 peer 且新 peer 不同 → 仅该 account 写 `group_bot_multi_bot_conflict`（`blocked`），不批量关注，等待运营指定 peer 或撤销后重建观察。
7. 仅在来源已经可信**且消息已通过控制提示识别**后，才按 `@ / username / 展示名 → 回复关系 / 入群服务事件 → 同观察窗口内唯一等待账号` 归属。若控制文本含确定的收件人前缀（例如“`Ray，您需要关注…`”），该收件人必须匹配 waiting account 的 Telegram username 或展示名；不匹配或存在多个匹配时必须拒绝归属，**不得**降级为唯一 waiting account。唯一等待兜底仅适用于不含明确收件人的通用控制提示。

#### 5.3.1 已审计可信的全群频道规则

不含明确收件人的通用控制提示在多个 waiting admission 中无法归属时，默认仍不得批量写入 `group_bot_rule_unattributed` 或创建 follow。只有同时满足下列全部条件，才可按“全群规则”逐账号展开：

1. 消息已通过本节的精确控制提示识别，且必须含同一原消息中的精确公开频道 URL；如有 confirmation callback，它也必须来自该原消息；
2. 来源是 Telegram 已确认的管理员 bot，**或** `targets.manage` 已为同一 `group_id + bot_peer_id` 创建 active `explicit_bot_confirmation` / `follow_sufficient` policy；仅某一历史 admission 曾信任该 peer 不足以授权全群展开；
3. 文本没有明确收件人；`explicit_recipient_unmatched`、`explicit_recipient_ambiguous`、普通推广和完成回执都不得进入本路径。唯一例外是 active source-bound policy 已生效后，同一可信 peer 在当前 listener 上下文中出现至少两条**不同** `source_message_id` 的逐账号提示，且每条的精确频道引用集合和确认 callback 形态完全一致；这证明是可复用的标准化频道准入规则，才可进入本路径。单条不匹配收件人提示仍不得展开；
4. 候选仅限该群现存、未终态的 `GroupBotAdmission`，且账号属于运行中 `group_ai_chat` 的持久 `TaskMembershipAdmissionItem` scope；已绑定另一可信 peer、`blocked`、`abandoned` 或没有运行任务绑定的 admission 必须跳过并保留原事实；
5. 每个候选都复用同一 `source_message_id`、peer、URL/按钮摘要、admission/version，创建其自己的 `group_bot_channel_follow` 与必要的精确 confirmation action。Planner 与 Dispatcher 都必须保证每个 `(task_id, admission_id, admission_version)` 同时最多保留一条 `pending/claiming/executing` confirmation action；重复标准提示只能补充审计，额外 pending callback（包括上线前遗留项）必须在 Gateway 前以 `group_bot_confirmation_superseded` 跳过，不能多次点击。不得创建正文 `send_message`、不得改写 `can_send`、不得直接 ready。
6. 若该 admission 的当前频道引用已有 `status=blocked + failure_code=group_bot_control_prompt_unverified` 的历史 follow，只有本路径再次观察到**不同** `source_message_id`、且该 channel_ref 仍在新消息的精确引用集合中时，才可清空其 Action 绑定并重置为 pending；旧 Action/follow 证据必须保留。相同 source、非当前引用、未知来源或没有 active scope 时一律不得 rearm。

该规则解决“群管向所有成员广播关注频道要求”以及“bot 对不同成员重复展示同一频道准入模板”不能归属单一账号的真实协议，不是未知 bot 的放行捷径。策略生效后的 listener 必须再次观察到该 exact peer 的有效控制事件才可展开；逐账号模板例外还必须保留当前消息及至少一条同签名消息的审计上下文。不得静默重放无审计来源、私聊消息或历史普通上下文。频道 follow 成功后的 ready 仍完全遵守 §5.5 的 confirmation / `follow_sufficient` 规则。

### 5.4 频道关注子动作

- 每个可信提示中的真实频道引用 → 一条 `group_bot_channel_follow`，幂等键 `(admission_id, channel_ref)`。`GroupBotAdmission.required_channel_refs` 是**当前 admission 世代的有效集合**；同一 admission 历史上被拒绝的推广/旧提示 follow 行保留审计，但不得阻塞当前集合的 follow 完成或 confirmation callback。频道引用可来自正文，也可来自同一原始消息的 URL 内联按钮；两者均必须保留其 `source_message_id`。
- `group_bot_channel_follow` 是 `actions.action_type` 的唯一持久类型，必须保持在该字段的 30 字符上限内；`group_bot_required_channel_follows` 仅是频道关注事实表名，不能写入 Action type。该约束必须有自动化长度回归。
- 任务被显式停止时，尚未执行的 follow / confirmation Action 按通用任务合同保留为 `skipped(task_stopped)` 审计事实；同一任务再次启动时，必须从旧 Action 的完整绑定 payload 重建新的 pending 准入子动作，并把未完成 follow 行改绑到新 Action。旧 Action 不得复活或覆盖，新动作创建必须幂等，重复启动不能产生重复 follow / callback。
- 频道引用只接受正文中的精确公开 `https://t.me/<username>` 或同源 URL 按钮；纯 `@username`、展示名、联系人广告或任意 URL 都不足以创建 follow row / Action。可信来源还必须同时具有确定性控制信号：同一消息的精确确认 callback，或正文中可解析的“请/需/先关注、订阅、加入频道/群、验证后发言”等指令。来源可信而控制信号不成立时，只写审计上下文。
- 运营因 `group_bot_control_prompt_unverified` 暂停某 admission 时，既有 follow 行和未进 Gateway Action 必须保留为 blocked/skipped 审计事实，不能批量重置或标成已关注。只有运营显式 restart observation，或 active policy 后 listener 新观察到通过 §5.3.1 广播/重复标准模板分类的**不同 `source_message_id`** 时，才可把该提示中同一 `channel_ref` 的此类 blocked 行清空执行绑定并重新置为 pending；同一历史消息重放、普通推广或其他失败码均不得 rearm。
- `awaiting_group_bot_confirmation` 的完成回执走独立的精确确认模板 / callback 识别；普通内容不得因为来自同一 peer 而先被当成完成回执、再被重新解析为新的频道提示。
- listener 快照必须保存无敏感 callback data 的不可变按钮摘要：`row`、`col`、展示文本、公开 URL、动作类型（`url` / `callback` / `other`）。bot 控制消息必须持久化为审计上下文且 `is_bot=true`，不得进入 AI 提示词、学习样本或引用候选；原始 callback bytes 不得写数据库、日志或前端。
- 对发布前已持久化、`control_buttons=[]` 的 bot 控制消息，listener 重读到**同一 group + message ID + bot peer**且获得安全按钮摘要时，只更新该审计行的 `control_buttons`。该回填本身不得迁移 admission state、修改正文或写 callback data；后续 follow/callback 仍必须走同一原消息的精确 Gateway 校验。
- Gateway 前必须按原始精确 URL 解析并验证为**原提示所指广播频道**；私有邀请须在加入前验证实体。URL 只允许单段公开 `t.me/<username>` 频道引用；群组、跳转实体变化、私链/多段链接、无法解析或不在快照按钮/正文引用集合中的地址均为 `required_channel_ref_invalid`。
- 非频道、无法解析、跳转实体变化、不在原引用集合 → `required_channel_ref_invalid`，不得关注任意链接。
- 全部成功后 → `awaiting_group_bot_confirmation`（除非 policy 为 `follow_sufficient` 且版本有效，见 5.6）。
- 提示要求确认按钮时：只点**同一控制事件**解析出的精确 callback 按钮；禁止按文案猜 callback，禁止从最新消息或其他 bot 消息中找一个“像确认”的按钮。

### 5.5 完成事件识别器（禁止自由文本 NLP 放行）

进入 `group_bot_admission_ready` 仅当下列**之一**：

| 路径 | 必要条件 |
| --- | --- |
| A. `confirmation_event` | 同一 `trusted_bot_peer_id`；事件可归属到该 `account_id`；且满足任一：① 该账号已执行提示中的精确确认按钮且 bot 返回成功/后续确认消息；② bot 文本命中**版本化确认模板表**（默认中文模板：`验证通过` / `可以发言` / `已解除禁言` / `已通过验证` 等，可运营扩展但写审计，**不是**开放 NLP） |
| B. `follow_sufficient` | active `GroupBotAdmissionPolicy` 绑定 **该 group + 该 trusted_bot_peer_id**；全部 required channel follow 成功；policy_version 与 admission 记录一致 |
| C. `not_required` + clear | 见 5.2.2；且独立 `can_send` 等 membership 条件满足 |

**明确禁止：**

- `probe_target_capabilities().ok` 单独 ready；
- 关注频道成功自动 ready（无 B/C）；
- 固定等待时长 ready；
- 发送正文试探后根据删除/报错推断 ready。

精确确认按钮 action 的执行合同：`group_bot_confirmation_button` 必须携带 admission/version、来源消息 ID、可信 bot peer、按钮行列和展示文本。Gateway 用执行账号重新读取**该来源消息**，逐项校验消息 ID、bot peer、按钮坐标、文本和 callback 类型后才调用 Telegram click；任一项不一致时写 `group_bot_confirmation_button_mismatch` 并失败。click 成功只表示“已发出确认”，**不得**直接 ready；仍须由同一可信 bot 的后续确认事件推进 A 路径。

### 5.6 目标级协议 `GroupBotAdmissionPolicy`

持久化字段至少：`tenant_id`、`group_id`、`trusted_bot_peer_id`（`follow_sufficient` 与 `explicit_bot_confirmation` 必填，`not_required` 为空）、`completion_policy`、`evidence_ref`、`reason`、`policy_version`、`status`、`created_by`/`revoked_by`、`effective_at`/`revoked_at`。

约束：

1. partial unique：每群一个 active `not_required`；每群+bot peer 的 active 完成协议必须可追溯到一个明确 policy version。`explicit_bot_confirmation` 的 peer 同时是 unknown role 控制消息的受限信任根，不能创建空 peer 的 explicit policy。
2. 写入/撤销：`targets.manage` + `expected_policy_version` + 理由；冲突 `409`，前端刷新后重提。
3. **`not_required` 运营闭环（解决鸡生蛋）：**
   - 首账号观察闭合且无可信规则 → `group_bot_policy_unresolved`，**不** clear；
   - 详情提供 **impact preview + 一键创建 not_required**，自动带上该次观察的 `evidence_ref`（游标范围、监听账号、空规则证明）；
   - 支持同租户按目标多选批量创建（每条仍独立审计、独立 evidence）；
   - 策略生效后，**已闭合观察且仍 unresolved 的同群 admission** 可 reconcile 为 `group_bot_rule_clear`（不免除**新入群**账号重新观察）。
4. `not_required` 不能跳过新入群账号的游标观察窗口。
5. 策略撤销 / bot peer 变化 / 证据失效：clear/ready 回 `group_bot_policy_unresolved`；未进 Gateway 的覆盖预约释放为 `pending_group_bot_admission`。

### 5.7 与 membership / 验证码 / test_message 的相位

`GroupBotAdmission` 是**跨任务**共享事实（键：`tenant_id + group_id + account_id`，绑定最近一次成功 membership 入群 action / rejoin 世代）。`TaskMembershipAdmissionItem` **不**承载群管状态机，只**投影**摘要字段供任务详情展示。

推荐相位顺序：

```text
not_joined -> joining
  -> [join success: create admission awaiting_group_bot_rule]
  -> group bot observation / required channel follow / confirmation
  -> challenge / captcha（若仍需要）
  -> Telegram can_send 复检
  -> membership ready 当且仅当 can_send=true AND group_bot_admission_ready
  -> 此后才允许 membership test_message（若任务仍配置）
```

#### 5.7.1 同账号 admission 并发与 mutation fencing（2026-08-04 supersede）

同一账号的配置频道关注、目标群 join、动态频道关注、多个 requirement action 和 visibility probe 按依赖图推进；所有依赖已满足且互不冲突的动作并发执行。系统不得对整个 `target_group_id + account_id + admission_generation` 建单执行窗口。只对相同 `remote_mutation_key`、相同 callback/button 身份或同一不可并发 Telegram mutation 建短 lease 与幂等 CAS；同群不同账号、同账号不同非冲突 requirement action 均可并发。

- 相同 mutation 已有 owner 时，仅对应 Action 保持 `pending/mutation_busy`；不得暂停 admission 中其他无依赖动作，不得假装已经 join、不得改写 `can_send`、不得发试探正文。
- mutation lease 的正常释放点是明确成功、pre-Gateway 失败或权威 no-mutation；若 Gateway 结果 unknown，只冻结该 mutation identity 并进入远端对账，不能用固定超时放行或重复点击。
- legacy 的 `group_bot_rule_unattributed` 不能作为新窗口永久锁；它只能经单账号、带版本和证据的 restart observation 重启，禁止批量 reset。
- mutation fencing 只防止同一远程副作用重复执行，不冻结任务日账号范围；其他 requirement action 和同群其他账号必须继续推进，不得形成账号级或群级 busy。

规则：

1. **test_message 不得**用于发现频道要求或群管规则；必须排在 `group_bot_admission_ready` 之后。
2. 若 challenge 阶段同时出现可信群管规则，先推进群管子状态，再继续 challenge。
3. `can_send=false` 且群管仍 waiting：分别展示，不得把 `can_send` 改写为表达群管等待。
4. 旧 `required_channel_prompt` 全群匹配 / 发言后 `_recover_*` 路径：新 `group_ai_chat` **禁用**；仅 legacy action 可记 `legacy_group_bot_intercepted` 后停发，不自动重发。

### 5.8 发送后可见性核验

适用：① 账号首次进入 `group_bot_admission_ready` 后的首条正文；② `admission_version` 递增后的首条正文；③ 为避免旧 C1 账号被全量 admission 门禁抽空而继续放行、但已存在空/无证据 admission 的账号首条正文。第③类是“先发送一条、延后确认业务成功”的兼容探针，不得直接计覆盖。

| 项 | 口径 |
| --- | --- |
| 开始条件 | Gateway 返回非空 `remote_message_id`；Attempt 可先记调用边界成功，但 Action 进入 **`pending_visibility`（待可见性核验）** 子态，**不算**已确认业务成功 |
| 手段 | 无正文：同群增量监听或按 message id 查询；禁止再发第二条试探 |
| 成功 | 到达完整核验窗口后按精确 message id 仍可见，才写 `visible_confirmed`，并允许覆盖确认与 **正式** hard-hourly credit（见 §5.8.1 / §5.8.3）；窗口内瞬时可见不得提前成功 |
| 拦截 | 可信 bot 删除/拒绝，或可靠证明不存在 → `post_send_intercepted`：撤回 ready、停止该账号后续未进 Gateway action、覆盖/硬小时**不计**成功、**不**自动关注重发；该 Action **明确失败**，不占 `planning_reservation`；可在准入重新 ready 后由下一 tick 受控重建**新** Action；若永久无法 ready 见 §5.8.2 |
| 未知 | 核验窗口内无法判定 → 保持 / 转入 `unknown_after_send`，占位语义见 §5.8.1 |
| 核验窗口 | 默认 `post_send_visibility_window_seconds=90`（60–180 可目标覆盖）；窗口结束仍未知 → 保持 unknown + 告警，走 continuity 人工/只读裁决，**无**超时当成功/失败 |
| 人工/只读核验 | 与 continuity unknown 裁决入口复用：确认可见则落正式 credit；确认未发送/已删则按证据记失败或 `post_send_intercepted` |

空/无证据 admission 的兼容探针一旦确认不可见，必须把该账号 admission 写为 `post_send_intercepted`，后续正文进入准入门禁，不得继续逐条试发。listener 若同时观察到 unknown-role bot 的频道要求，只能在以下证据全部成立时把它提升为受限的 `post_send_intercept_rule`：同一 bot peer 至少两条不同 source message；频道 URL 集合和 callback 位置/文本签名完全相同；正文是明确“关注/订阅后发言”控制语义；当前同群存在尚未关闭的 `pending_visibility`，且 bot 消息 ID 在被观察正文之后、创建时间不超过该任务最大 180 秒核验窗口。该信任只用于当前 group 的运行中 membership scope，必须保留 bot peer、两条 source message 与 pending Action/remote id 证据；普通推广、单条提示、无 callback、无开放 hold 或顺序不成立时仍只审计。

#### 5.8.1 「待可见性核验」与 `planning_reservation`（P0，强制）

continuity §7.3.1 公式**保持不变**：

```text
planning_reservation = eligible_open_count + unknown_after_send_hold_count
```

本专项约定：

1. **`pending_visibility`（待可见性核验）不是第三套规划语义**，在 hard-hourly / 日覆盖义务占位上与 `unknown_after_send` **同语义**：每个此类 Action **只占位 1**，计入 `unknown_after_send_hold_count`（实现可用 `hold_reason=pending_visibility | unknown_after_send` 区分展示，但计数共用）。
2. 因此 **不得**另加一项 `pending_visibility_hold_count` 进公式，也不得在 Attempt 已有 remote id 后把义务当作已关闭而从 reservation 中移除。
3. `eligible_open_count` 仍只含**尚未进入 Gateway** 的有效 pending/claiming/executing。
4. 禁止对同一义务在 `pending_visibility` / unknown 占位期间再规划替代 `send_message`。
5. 会话轮换：`pending_visibility` 与 unknown 一样保守占用 last platform speaker。

#### 5.8.2 `post_send_intercepted` 与永久不可准入时的义务释放（P0，强制）

| 情形 | 硬小时 / 日覆盖义务 |
| --- | --- |
| 账号可重新走准入并再次 `group_bot_admission_ready` | 被拦截 Action 计失败、不占 reservation；下一 tick 可受控建**新** Action（continuity §7.3.4） |
| 账号长期 waiting / unresolved / blocked，但运营**未**放弃 | 义务保留在分母/debt；账号侧 blocker 可见；**不得**静默缩分母或假完成 |
| 账号在该群**永久不再履约** | 仅当运营显式写入 **`admission_abandoned`**（见下）后：① 该账号退出该群 ready 产能与自动观察；② 该账号在该目标 epoch 上**尚未 credit 关闭**的硬小时义务从 `durable_debt` **排除**（不再累计、不再 `required_new`）；③ 日覆盖行保持分母，状态 `blocked / admission_abandoned`，`next_eligible_at=null`，不得 release 回 ready 假完成；④ 写审计 |

**`admission_abandoned` 写入门槛（全部满足）：**

1. 权限：**固定** `targets.manage`（与 `GroupBotAdmissionPolicy` 同级；`tasks.manage` **不能** abandon）。
2. 提交：`tenant_id`、`group_id`/`operation_target_id`、`account_id`、`reason`、`evidence_ref`、`expected_admission_version`；版本冲突 `409`。
3. 先 impact preview：将从 `durable_debt` 排除的硬小时义务条数/计划桶列表、将跳过的未进 Gateway action 数、覆盖行影响；preview 无副作用。
4. 确认后：admission → `abandoned`；未进 Gateway 的该账号相关出站 action → `skipped / admission_abandoned`；**已进 Gateway / pending_visibility / unknown 不改写**，仍走 continuity 核验/裁决。
5. **禁止**系统仅因观察超时、policy_unresolved 堆积或单次 intercepted 自动 abandoned。
6. 恢复：仅运营 reopen（`targets.manage` + 新 reason + evidence + `expected_admission_version`）→ 递增 `admission_version` 并重新进入 `awaiting_group_bot_rule`；旧 abandoned 期间排除的 debt **不回补**到历史桶，新义务从 reopen 后的当前/后续桶起算。

对 continuity `durable_debt` 排除条件的增量（本专项生效后）：

```text
durable_debt 排除：
  已终态目标 | superseded epoch | 发布锚点前历史
  | 该义务绑定账号在该目标上 admission_abandoned（审计可追溯）
```

#### 5.8.3 硬小时 / 覆盖 credit 与可见性写入时序（P0，强制）

与 continuity §7.2「Attempt success + remote_message_id」的衔接：

| 阶段 | Attempt / Action | 硬小时 | 日覆盖 |
| --- | --- | --- | --- |
| Gateway 回执 remote id，且本条**需要**可见性核验 | Attempt 可记 gateway 边界成功；Action=`pending_visibility` | **不**插入正式 `TaskHardHourlyDeliveryCredit`；插入 **`pending_visibility_credit`** 占位（`UNIQUE(action_id)`，关联 plan bucket，**不**增加 bucket.`success_count`） | **不**确认完成；行保持 sending/unknown 类占用 |
| Gateway 回执且本条**不需要**可见性核验 | 沿用 continuity：可直接正式 credit | 正式 credit + success_count | 可确认完成 |
| `visible_confirmed` | Action 业务 success | **同一短事务**：删除/关闭 pending 占位 → 插入正式 `TaskHardHourlyDeliveryCredit` → bucket.`success_count`+1（仍 `UNIQUE(action_id)`，与 continuity 精确一次语义一致） | 确认完成 |
| `post_send_intercepted` / 确认失败 | 失败终态 | 关闭 pending 占位，hold 存储状态固定为字段长度安全的 `intercepted`；Action/admission 仍写 `post_send_intercepted`；**无**正式 credit，不占 reservation | 不完成 |
| 窗口结束仍未知 | Action=`unknown_after_send`（hold_reason 可保留 pending_visibility 痕迹） | pending 占位**保留**（仍算 `unknown_after_send_hold_count`）；走 continuity 人工/只读裁决；裁决可见才转正式 credit | 同左 |

规则：

1. **禁止**在 `pending_visibility` 期间把 bucket.`success_count` 或覆盖 confirmed 先加后扣。
2. `pending_visibility_credit` 只服务「需要可见性核验」的消息；普通成功路径不创建该表/该行。
3. 重复 finalize / 多 worker：正式 credit 仍以 `UNIQUE(action_id)` 为幂等；pending 占位同样幂等。
4. 页面可同时展示「调用边界已成功 / 待群内可见确认」，不得显示为 hard-hourly 已达标条数。

### 5.9 正文发送门禁

对 `group_ai_chat` 的 `send_message`（含 hard-hourly / 日覆盖绑定）：

1. Planner / claim / rebind / AI 生成前 / Gateway 前：同一候选条件——`GroupBotAdmission=group_bot_admission_ready` 且 version 有效 + `can_send` + 健康/在线/容量/任务范围/轮换 + OutboundTargetGate。
2. 未 ready：`pending / group_bot_admission_wait`，**不**创建 ExecutionAttempt，**不**调用 AI，**不**调用 Gateway。
3. AI 生成中 admission 降级：废弃已生成正文，释放未进 Gateway 预约，写 `pending_group_bot_admission`；不得带着旧正文换号发送。

## 6. 账号轮换与引用

### 6.1 会话键与事实表

- 群聊：`tenant_id + group_ai_chat + group_id`（跨同群多任务共享）。
- 频道评论：`tenant_id + channel_comment + linked_discussion_group_id`。
- `ConversationSpeakerState`：一行锁/预约/`last_human_cursor`/`last_platform_*`。
- `ConversationSpeakerTurn`：追加事件；`sender_kind ∈ {platform, human, group_bot_control, system}`。
- 业务 slot 另有稳定 `conversation_slot_key`，换号不重复创建同一互动。

### 6.2 相邻定义

- 以真实远端顺序为准：最近一条平台 `success` / `unknown_after_send` / 待可见性核验消息之后，若无 `human` 消息，则下一条必须换账号。
- `group_bot_control` / `system` **不**打断。
- 真人消息推进 `last_human_cursor` 后，同账号可再参与，仍过全部发送门。

### 6.3 无替代账号与容量预检

| 场景 | 处置 |
| --- | --- |
| 有 ≥1 个其他 eligible 账号 | 必须换号；Dispatcher 预约为最终裁决 |
| 仅 1 个 eligible 且无真人打断 | `pending / speaker_rotation_wait`；**禁止**静默同号连发 |
| 带 `coverage_ledger_id` | **禁止**换号；只能延期或按覆盖规则释放原义务后重排 |
| 硬小时 / 日覆盖预检 | `rotatable_ready_account_count < 2` 时写 **warning**（非创建阻断）：任务在无真人打断时只能产出 1 条后进入 rotation wait；容量证明必须把“仅单号可连发窗口”视为**不可持续产能**，不得显示“可稳定完成小时/日目标” |
| 单号任务长期 wait | 覆盖行 `blocked / speaker_rotation_wait` 或保持 ready 但 `next_eligible_at` 延后到有真人/替代号；任务**不得** `completed` |

`speaker_rotation_unavoidable_count`（若出现同号连发成功）视为**缺陷告警**，正常路径必须为 0。

### 6.4 引用

- 群聊默认 `reply_min_per_round=1`；评论默认 `mixed` + `reply_min_per_message=1`；计入原总量，不额外加量。
- 有合格候选时至少一条真实 `reply_to_message_id`；Gateway 必须携带。
- 候选：属当前目的地、未撤回、非当前账号自己、仍支撑正文锚点。
- 短缺：`reply_target_shortfall`，保留安全普通动作；**不**伪造引用、**不**把“回复某人”写入正文。
- 显式单条 `comment_mode=reply` 快捷入口缺目标时失败，不降级。
- 日覆盖债务场景：可引用数不足时，按日覆盖 PRD 转为覆盖回补 Cycle（普通发言），**不得**用假引用凑数；兜底改为 `签到` 规则而非 `emoji_react`。

## 7. 内容质量与 `签到`

> **2026-08-04 当前 supersede：** 对 `group_ai_chat`，正常正文绑定发送账号和固化面具版本，按该账号滚动 10 天去重；签到只保留统一 `content_source=check_in`，正文精确 `签到`，计数量、账号未覆盖时同时计 coverage，可用于额外补量，但不计高质量正文或 reply。以下与本口径冲突的旧类型/配额均废止。

### 7.1 质量门

在生成入库与 Gateway 前执行；只返回允许/拒绝+原因，**禁止**静默改写正文。

拒绝码至少：`template_shell`、`repeated_opening`、`semantic_duplicate`、`missing_context_anchor`、`reply_target_mismatch`、`voice_profile_mismatch`、`check_in_repeat`、`check_in_quota_exceeded`。

账号面具只约束语气、句长、表情习惯、表达偏好与短期立场；任务话题和真实上下文决定正文主题。不得因为面具摘要含“男客、夜场、价格、位置”等词，就要求每个 slot 必须出现价格/位置/服务锚点，更不得把 AI 原文截断后拼接固定问句。面具不匹配只能拒绝该 slot 并进入既有有界补位生成。

AI 活群只消费既有 active 面具，不得修改面具生成、启用、版本或回滚逻辑。正常正文必须固化 `account_mask_id/account_mask_version/mask_snapshot_hash` 和 `voice_profile_contract_version=style_only_v2`。唯一例外是统一 `check_in`，必须固化稳定数量义务、远端 mutation identity、合法 `trigger_reason`，账号未覆盖时追加同 Task/群/账号/日期的 coverage 绑定。历史已有普通正文但缺少合同或面具证据的 Action 在 Gateway 前转 `skipped/voice_profile_anchor_replan`，不得静默改写成签到。

生产验收必须同时证明：active/superseded 面具数量与版本未被本功能改写；发布后新 Provider Action 的合同版本全部为 `style_only_v2`；开放队列不存在缺版本的旧已生成正文；新成功消息不再出现系统固定尾句注入。任务被人工停止时只能写 `unproven`，不得用容器健康或历史消息代替真实发送验收。

Provider 返回正文进入 Phase C 时，必须保留规划阶段已经固化的非空 `generation_source`（如 `human_context`、`bootstrap`）；Provider 结果未提供该字段时不得用空字符串覆盖。带 `AI_GROUP_RELEASE_LIVE_AT` 的生产质量门以该发布时间之后的新 Action 为放行样本，同时保留近 24 小时快照作为历史观察；未提供发布时间时仍按近 24 小时执行门禁。

人工停止后的历史 AI 活群任务必须可按当前合同重新启动。启动预检在构造当前 `GroupAIChatTaskCreate` 前先调用统一 legacy config normalization，删除已废弃的 `consecutive_message_*` 与 `auto_follow_required_channel`，并补齐 `group_bot_admission_required=true`；归一化只作用于预检输入，不改面具数据，也不恢复已移除的连续发言行为。

群管提示可能在账号关注必需频道期间被删除，随后为不同账号产生新的提示。实时消息快照必须携带当前查看账号的 Telegram peer id；当旧 `source_message_id` 不可读时，只能用“可信群管 + 当前频道集合 + 确认按钮 + 显式收件 peer id 等于查看账号”四项同时成立的新提示重绑确认 Action。不得依赖本地账号表 id，也不得因窗口内只有一条提示而猜测归属。

高频群内 20 条 listener 窗口不足以覆盖账号完成两个频道关注前的群管提示。确认刷新固定读取最近 300 条原始消息并使用 `control_only` 模式先过滤无按钮消息，再构建少量控制快照；普通 listener 仍保持原上下文窗口。生产验收需证明深窗口调用未触发执行超时，并看到旧 source 重绑到显式匹配当前 viewer peer id 的新消息。

### 7.2 `签到` 规则

| 项 | 口径 |
| --- | --- |
| 正文 | 精确 `签到` 两字 |
| 日覆盖主路径 | 面具可用账号优先按固化面具生成；统一 `check_in` 成功时若账号未覆盖可同时完成 coverage |
| 普通任务兜底 | 非引用 direct 仅在版本化生成流程明确耗尽且 policy 允许时使用统一 `check_in`；缺面具可用 `trigger_reason=mask_missing` |
| 禁止 | 引用降级为签到；绕过轮换、准入、OutboundTargetGate、群节奏、账号与 coverage 绑定或远端成功证明 |
| 日覆盖审计 | 正常正文固化面具证据；签到固化 `obligation_id/remote_mutation_key/trigger_reason`，未覆盖账号追加 `coverage_ledger_id` |
| 普通兜底审计 | `content_source=check_in`、明确 trigger reason、生成耗尽或面具缺失事实 |
| 重复规则 | `check_in` 不进入普通正文 10 天去重；由主义务唯一键、非终态 Action partial unique 和 unknown 防重 |
| 与覆盖 | 仅真实远端成功、成功 ExecutionAttempt 且非空 `remote_message_id` 才计数量/可选 coverage；签到不计高质量 AI 文本指标或 reply |

普通兜底关闭开关或不满足门禁：Action 可见失败并以新 variation 重新规划，不产生替代文本，不 mock 成功。

## 8. 覆盖、硬小时与调度交叉

### 8.1 Ready 池与容量

- 非 `group_bot_admission_ready` 的账号：**不**进入群聊可发池，**不**计入日覆盖/硬小时 **ready 产能**。
- 日覆盖**分母**仍含未准入账号（blocked / `pending_group_bot_admission`），不得缩分母扮 100%。
- 容量证明：仅对 admission-ready ∧ can_send 账号计算“可完成部分”；全量目标不足时展示部分履约 + 准入缺口，不得伪造成可全完成。

### 8.2 预约与 unknown / 待可见性

对齐 continuity + §5.8.1–§5.8.3：

- 未进 Gateway 的覆盖/硬小时 action 因准入降级 → 释放 reservation，`pending_group_bot_admission` / 等价 blocker。
- 已进 Gateway / `pending_visibility` / `unknown_after_send`：**不**被准入状态改写；**共用** `unknown_after_send_hold_count` 占位 1，禁止替代重发；`planning_reservation` 公式不变。
- `post_send_intercepted`：不计正式 credit；失败不占 planning_reservation；账号退出 ready 直至重新完成准入或 `admission_abandoned`。
- `admission_abandoned`：按 §5.8.2 从 durable_debt 排除该账号未关闭义务，覆盖分母保留 blocked。

### 8.3 Dispatcher 优先级与 ClaimClass 档位（P1，强制）

continuity §7.4 严格顺序保持：

```text
target_admission_retry
-> search_join_membership
-> search_join
-> AI hard-hourly
-> ordinary
```

本专项增量：

| Action 类型 | ClaimClass 档位 | 作用域 | 禁止 |
| --- | --- | --- | --- |
| 群管控制观察、`group_bot_channel_follow`、精确确认按钮类准入子动作 | **复用 `target_admission_retry` 档（最高档）** | 仅当该 action 用于解除**同一 `tenant_id + task_id + account_id`**（及同一目标群）上已存在的 `group_bot_admission_wait` / 未完成 admission 时，才享受本档优先于该账号的 hard-hourly/ordinary `send_message` | **禁止**跨任务、跨账号、跨租户用本档抢占；**禁止**把无 admission 关联的普通入群/搜索动作塞进本解释而插到 `search_join_*` 之前 |
| 与 admission 无关的 `ensure_target_membership` 等 | 维持既有相对 hard-hourly 的顺序（continuity / 日覆盖既有口径） | — | 不得借群管名义整体抬到 search 之前 |

可测含义：

1. 同 task+account 存在到期 `group_bot_channel_follow` 与同账号 hard-hourly `send_message` 时，一次 claim 类别选择必须先取 follow/观察，避免 send 反复 wait。
2. 存在到期严格 `search_join` / `search_join_membership` 时，不得因为任意群管 follow 全局插队而饿死搜索日目标（follow 仅在 **本档内** 与 `target_admission_retry` 同类竞争，且受 tenant+task+account 作用域过滤后的就绪集合约束）。
3. 「一轮」公平定义仍以 continuity §7.4 为准。

### 8.4 目标终态

- `group_dissolved` / `target_ref_invalid` / `target_reference_superseded`：沿用 continuity；群管模块不得改绑目标或改写解散文案。
- `qdsfxy` 等 username 不存在 → 引用无效文案，**禁止**“群里已被解散”。

## 9. API / 前端 / 权限

### 9.1 策略 API（运营目标）

| 接口（逻辑名） | 权限 | 行为 |
| --- | --- | --- |
| `POST .../group-bot-admission-policies` | `targets.manage` | 创建 `not_required` 或 `follow_sufficient`；body：`group_id`/`operation_target_id`、`completion_policy`、`trusted_bot_peer_id?`、`evidence_ref`、`reason`、`expected_policy_version` |
| `POST .../group-bot-admission-policies/{id}/revoke` | `targets.manage` | 撤销；`expected_policy_version` + reason |
| `POST .../group-bot-admission-policies/preview` | `targets.manage` | 无副作用 impact：将 clear 的 admission 数、将释放的预约数 |
| `POST .../group-bot-admissions/reconcile` | `targets.manage` 或任务管理（只读 reconcile 可 tasks.manage） | 策略变更后重核未进 Gateway admission |
| 一键 not_required | `targets.manage` | 从 `group_bot_policy_unresolved` 详情带 evidence 创建 |
| `POST .../group-bot-admissions/{id}/abandon` | `targets.manage` | §5.8.2：`admission_abandoned` + preview + reason + evidence + `expected_admission_version` |
| `POST .../group-bot-admissions/{id}/reopen` | `targets.manage` | 放弃后重新观察；递增 `admission_version` |
| `POST .../group-bot-admissions/{id}/restart-observation` | `targets.manage` | 仅修复存量无基线/观察失效：从已持久化 listener waterline 取新基线，要求 reason、evidence、`expected_admission_version`，递增 version 并写审计；无水位 `422`，不自动 ready |

全部写审计；版本冲突 `409`。

### 9.2 任务中心展示

- 创建/向导：强制文案“账号轮换：必须”“群管准入：必须”；展示可轮换 ready 数、引用候选摘要；**无**连发开关、**无**关闭群管准入开关。
- 详情/Action：互动类型（发言/引用/评论/回复/签到）、上一位账号、轮换原因、质量拒绝码、兜底原因。
- AI 活群账号/准入区：admission 状态、protocol、trusted bot peer、游标范围、频道子 action、确认/可见性、failure_code、version；`required_channel_*` 只在已记录可信 bot 的精确频道引用时显示“需要关注频道”，`group_bot_policy_unresolved` 显示“未发现频道要求，等待目标策略”，`observation_stale` 显示“观察证据不足”；成功 Action 必须显示远端消息 ID/成功，不复用旧准入错误。提示**全文**仅 `tasks.view`+任务权限；列表脱敏。
- 运营目标：策略版本时间线、创建/撤销人、evidence 链接；禁止在任务 JSON 编辑里改 protocol。

### 9.3 预检字段

至少返回：`group_bot_admission_ready_count`、waiting/blocked 计数与 top reasons、`rotatable_ready_account_count`、`speaker_rotation_single_account_warning`、`reply_candidate_count`。

## 10. 存量迁移、Canary 与回滚

### 10.1 迁移原则

1. 先 inventory dry-run，再显式 `--apply`；可按 `--tenant-id`。
2. **不改写** `success` / `failed` / `unknown_after_send` / 已进 Gateway 事实。
3. 清理 active 任务配置：删除 `consecutive_message_*`、删除可关闭的 `auto_follow_required_channel=false`，写入 `group_bot_admission_required=true`；默认引用字段迁移到新默认。
4. **禁止**把旧 `can_send=true` 批量写成 admission ready。

### 10.2 Canary 阶段（防 ready 池抽空）

| 阶段 | 行为 |
| --- | --- |
| C0 inventory | 只统计将创建的存量复核数、将丢弃的连发配置、受影响 task |
| C1 仅新入群 | 运行时 **admission 门禁只对新 join/rejoin 强制**；存量账号保留历史发送资格，相关 action 打 `legacy_send_until_reviewed` 诊断（详情可见） |
| C2 存量复核 | 为 AI 活群引用中的已入群账号创建 `legacy_group_bot_review` 记录；从当前监听水位做**只读**控制观察 + 策略 reconcile |
| C2 ready 规则 | 存量账号在复核完成前：**不计入新策略 ready 产能证明**，但 **C1 仍允许**其发送以免生产硬小时归零；C2 全量 enforce 前必须按租户/目标分批，且 ready 池下降告警可回滚阶段开关 |
| C3 全量 enforce | 所有发送走 admission ready；关闭 `legacy_send_until_reviewed` |

#### 10.2.1 C1/C2 期间 action 边界（P1，强制）

| 问题 | 口径 |
| --- | --- |
| C1 期间**存量账号**上**新创建**的 send action 算 legacy 还是 new？ | **legacy 路径**：不受 `GroupBotAdmission.ready` 拦截，仍走既有 continuity 出站门 + 轮换/质量（若已启用）；payload/result 必须打 `legacy_send_until_reviewed=true` 与当时 canary 阶段标记 |
| C1 期间**新 join/rejoin 账号**的 action | **new 路径**：完整 admission 门禁 |
| C2 某账号复核完成（ready 或 abandoned 或明确 blocker）之后 | **仅影响该时刻之后新创建**的 action：开始 enforce 新 admission 门禁（或 abandoned 跳过）；**不**回溯重评 C1/C2 复核完成前已创建、已发送、pending_visibility、unknown 的 action |
| 复核完成前已存在的 open action | 继续按创建时路径执行收口；不得批量改绑 admission_version 或强制失败 |

#### 10.2.2 C2 复核与存量 unknown（P1，强制）

- C2 复核过程中发现的**历史** `unknown_after_send` / 待核验发送：**只走 continuity 的只读核验与人工裁决路径**，**不**纳入 `GroupBotAdmission` 状态机，不得因 admission 状态改写其结果。
- admission 状态机与 `pending_visibility` 规则 **只覆盖**「该账号在该群已进入 new 路径之后」新产生的发送（C1 新入群 enforce 后，或 C2 复核完成后的新 action，或 C3 全量后）。
- 存量 unknown 裁决为成功时：可写正式 credit（若仍属有效 epoch/桶规则），**不**自动把 admission 标 ready。
- 存量 unknown 裁决为失败/未发送：不自动创建 follow action；若运营要推进准入，走独立 admission 观察/策略，不与该历史 action 绑死。

存量复核完成路径：

- 观察闭合 + `not_required` → clear → ready（仍要 can_send）；
- 发现规则 → 走关注/确认；
- 长期 `can_send=true` 且近 7 日有**已确认**成功发言 credit/远端 id 的账号：允许运营一键 `legacy_evidence_follow` 记入 audit 后进入 clear **仅当**目标已有 `not_required` 或对该 bot 的 `follow_sufficient`；**禁止**无策略自动 ready；
- 运营决定该账号不再在该群履约 → `admission_abandoned`（§5.8.2）。

### 10.3 回滚

- 只关闭新策略调度与 C3 enforce，回退到 C1/C2 开关。
- **不**删除 admission / speaker / 审计 / Attempt。
- **绝不**回滚为“先发一条试试”。

## 11. 观测与告警

至少指标/任务统计：

`speaker_rotation_wait_count`、`speaker_rotation_runtime_rebind_count`、`speaker_rotation_unavoidable_count`、  
`reply_planned_count`、`reply_success_count`、`reply_target_shortfall_count`、  
`check_in_fallback_count`、`check_in_quota_exceeded_count`、`content_quality_rejection_counts`、  
`group_bot_admission_wait_count`、`group_bot_policy_unresolved_count`、`group_bot_unattributed_count`、  
`group_bot_required_channel_*`、`post_send_intercepted_count`、`legacy_group_bot_intercepted_count`、  
`observation_stale_count`、`post_send_visibility_unknown_count`、`legacy_send_until_reviewed_count`。

缺陷级告警（非吞吐优化）：`speaker_rotation_unavoidable_count > 0`、新入群仍出现 `legacy_group_bot_intercepted`、`post_send_intercepted` 突增、`policy_unresolved` 目标堆积、C2→C3 后 ready 池跌破阈值。

## 12. 验收

### 12.1 正向

| 场景 | 必须证明 |
| --- | --- |
| 新账号入群 | 观察/准入完成前无 AI、无正文、无 test_message |
| 无 bot + 有 not_required | 窗口闭合且游标连续 → clear → ready |
| 无 bot + 无策略 | 窗口闭合 → `policy_unresolved`；一键策略后 reconcile clear |
| listener 空轮询 | 无新消息也写 observation；到期后仅依据有效游标批次进入 unresolved / clear |
| 存量无基线 | 显式 restart observation 写审计和新 version；无 listener waterline 明确失败，不批量 ready |
| 历史成功带旧准入错误 | API/页面显示远端成功，不把旧 `required_channel_admission_pending` 显示为当前失败 |
| 可信 bot 要关注 | 仅归属账号建精确 follow；`can_send` 不被业务改写 |
| 空 admission 的未知受控群 | 首条正文先 `pending_visibility`；窗口内不提前计成功；精确 message id 被删除后 action 失败、覆盖不增加；同 peer 重复频道+callback 提示与开放 hold 相关后，逐账号创建精确 follow |
| 双账号同时等 | 无 @ 不批量 follow → unattributed |
| 普通 bot 消息 | 来源不可信时不读取/不污染 waiting admission；不能批量写 `group_bot_rule_unattributed` |
| unknown role 的已审计 peer | 仅 active explicit/follow policy 绑定的相同 peer 可解析控制消息；policy 外 unknown bot 无状态变化 |
| 按钮式频道/确认 | 快照持久化 URL/callback 摘要；follow 只接受精确广播频道 URL；callback 重读原消息逐项核验后 click，click 本身不 ready |
| 历史按钮提示回放 | 已入库的同一 bot 消息为空按钮摘要时，listener 再次读取到的安全摘要只回填该审计行；精确 action 可继续核验，不能依赖猜测或保存 callback data |
| 并发入群 | 同群第二个 membership action 在 Gateway 前写 `group_bot_admission_window_busy`；第一个 admission 收口后才继续 |
| 完成事件 | 仅识别器路径或 follow_sufficient → ready；`probe.ok` 无效 |
| 引用 | 有候选则真实 reply_to；短缺可审计 |
| 轮换 | 无真人间隔相邻账号不同 |
| 签到 | 可审计、不连续、不替引用；受配额 |
| 解散/引用 | 仅独立证据展示对应文案；qdsfxy ≠ 解散 |

### 12.2 负例 / 回归（对齐上次终态 PRD 教训）

| 场景 | 必须证明 |
| --- | --- |
| 观察未闭合（缺口/失联） | 不 clear、不 send |
| 空基线 / 被截断最新窗口 | `observation_stale`，不得以 0、最新快照或等待时间推断连续 |
| 窗口到期但无 not_required | unresolved，不是 ready |
| hard-hourly 旧发送后重发路径 | 新路径不存在；拦截后不自动重发 |
| 日覆盖 emoji_react | 新实现不可再发送；仅签到或 skip |
| 单账号无真人 | 仅 rotation wait；任务不 completed；产能 warning |
| AI 生成中 admission 降级 | 不进 Gateway；释放预约 |
| 可见性窗口结束仍未知 | 保持 unknown 占位，不超时当成功 |
| 可见性窗口内瞬时可见 | 仍保持 `pending_visibility`；必须等完整窗口结束再确认 |
| unknown-role 单条/无关联频道提示 | 不建立 bot 信任、不批量 follow；只有重复精确规则 + 同群开放 hold + 远端顺序/时间窗同时成立才可展开 |
| 待可见性 vs 规划 | `pending_visibility` 计入 `unknown_after_send_hold_count`，不重复规划同一义务 |
| 正式 credit 时序 | 需核验消息先有 `pending_visibility_credit`，`visible_confirmed` 才正式 credit |
| post_send 后永不可 ready | 仅 `admission_abandoned` 排除 durable_debt；覆盖分母 blocked |
| follow ClaimClass | 复用 target_admission_retry 档且限 tenant+task+account；不饿死 search_join |
| admission_version | rejoin/peer/policy/规则集变化递增；与 task.config_revision 无关 |
| C1 存量新建 action | legacy_send_until_reviewed，不受 admission 拦截 |
| C2 后不回溯 | 复核完成前已发送/unknown 不重评；存量 unknown 走 continuity |
| 私聊/非管理员 bot | 不驱动关注 |
| 多 admin bot 冲突 | blocked/multi_bot_conflict，不猜 |
| 迁移 dry-run | 不改 success/unknown 历史 |
| C1 存量 | 生产不因一夜清空 ready 池而 hard-hourly 全 0（分阶段 enforce） |
| credit | 待可见性未确认前 hard-hourly/覆盖不计**正式**成功 |

### 12.3 生产证据层级

| 证据 | 含义 |
| --- | --- |
| payload / Attempt / remote id / 详情一致 | 基础 pass |
| 入群→游标→规则→follow→确认/策略→首条可见性因果链 | 群管 pass |
| 连续 ≥20 条群聊 + ≥20 条评论真实序列 | 轮换/引用 pass |
| 仅 CI/部署 | `unproven`，不得写 production_fixed |

## 13. 深度自检（Product Design Complete）

| 检查项 | 状态 |
| --- | --- |
| 原始诉求：不连发 / 要引用 / 签到兜底 / 像人 / 入群先关注频道 / 解散文案 | 已覆盖 |
| 前端状态与策略入口 | §9 |
| API / 权限 / 版本冲突 | §9.1 |
| 数据流 / 与 continuity unknown·credit | §4 §5.8 §8 |
| 观察闭合 vs 放行 | §5.2 |
| not_required 闭环 | §5.6 |
| membership 相位 | §5.7 |
| 跨 PRD supersede | §1.1 |
| 迁移 canary | §10 |
| 验收负例 | §12.2 |
| continuity 公式 / credit 时序 / abandoned debt | §5.8.1–§5.8.3 |
| ClaimClass 档位 / admission_version / C1–C2 边界 | §5.1.1 §8.3 §10.2.1–§10.2.2 |
| 开放问题 | 无阻塞项；确认模板表初值由实现按 §5.5 落地并可配置扩展 |

**existing_scope_design_status=`complete`，仅 2026-07-27 既有范围可 handoff dev。** 实现必须以本文件 + continuity PRD 为准；二者冲突时以本节 P0 交叉条款修订 continuity 的 credit/debt 增量解释（见 continuity 文首关联说明）；plan 不得弱化闭合/canary/supersede/占位/credit 时序。2026-07-31 新增真人话术修复范围以 §14 的独立闸门为准，当前不得随既有 complete 状态进入开发。

## 14. 优化 AI 活群真人话术待修复清单（2026-07-31）

### 14.1 状态与证据边界

本节承接生产只读真人化评估，只登记 Prompt/回复对以外的新缺口。测试边界固定为：PostgreSQL 只读事务、无 Action/Attempt 创建、无 Telegram Gateway；影子结果只能证明可行性，不能写 `production_fixed`。

| ID | 修复方向 | 当前证据 | 分流状态 |
| --- | --- | --- | --- |
| H-01 | Planner → Dispatcher `chat_mode` 单一真相源 | 最新样本约 76.25%–77.5% 从 `reply` 被重算为 `idle_warmup`；最终 4,559 条消息重放均通过，但被拒 Provider 候选未持久化 | `online_shadow_blocked_by_production_ingress`；候选池测试完成前不改代码 |
| H-02 | 会话级 should-speak / `wait|send + reason` | platform run P50=2、P90=5、P95=8；固定预算 1/2/3 仅保留 28.83%/44.82%/54.56% 发言，5 个观测任务全部受损 | 固定 cap `rejected`；价值调度进入 Product Design |
| H-03 | 租户全局 persona 区分度与 AI self-history 消融 | 884 个 active profile 中 478 个摘要精确重复；当前只在 batch=2 内做相似度检查 | `online_shadow_blocked_by_production_ingress`；先做 no-send A/B |
| H-04 | 真人校准的安全候选排序 | 最新 2,000 条中 1,997 条自动 accepted，但既有盲评没有稳定优势 | `blocked_by_human_gold_set`；先建人标基准集 |
| H-05 | 真人 reply parent / addressee 数据 | 590 条学习样本没有 parent/reply relation；原生最近 200 条仅临时取得 4 对 | `blocked_by_schema`；先设计采集与隐私边界 |
| H-06 | 随机静默 holdout / 因果 outcome | 原始 next-human 率 99.97%、P50 23 秒，被高活跃群饱和，不能归因于 AI | `blocked_by_live_traffic_authority`；未批准 canary 前不改真实发言流量 |
| H-07 | 真人节奏指纹与长度相关输入时延 | 7 天成功 Action 的 schedule lag P50=127 秒、P90=1,145 秒；platform-to-platform P50=18 秒、23.55% 在 5 秒内，但真人 turn 时间被批量入库/秒级精度污染 | `online_inventory_partial_timestamp_precision_limit` |
| H-08 | 实际成稿的跨账号风格趋同监测 | 平台账号实际成稿长度 P10/P50/P90=12.4/13.6/14.6 字，四字开头跨账号碰撞率 66.78%，账号间距离 P90=0.3482 | `online_inventory_pass_design_pending` |
| H-09 | single-message late binding 与发送前上下文重校验 | 5,000 条成功 Action 中 61.16% 在 Gateway 调用前出现新真人消息，21.16% 超过已配置过期阈值；代码仍为 batch=10/单次上下文刷新/一次多 slot 输出 | `online_inventory_pass_provider_ab_pending` |
| H-10 | 平台问句后的真人话轮保护 | 269 个平台问句中 166 个在 60 秒内被另一个平台账号接管，60 秒 floor violation=61.71% | `online_inventory_pass_design_pending` |
| H-11 | A/B 群正文、上下文与生成输入零串群隔离 | 30 天结构错配和跨群 exact sent text 均为 0；但 2,696/7,488 条成功 Action 的 Prompt 明确携带跨任务原文，涉及 5 个群 | `failed_prompt_isolation_design_pending` |

### 14.2 H-01 会话模式合同待修复

1. `chat_mode` 必须由一个共享 resolver 产生并版本化；Planner 写入 Action 后，Dispatcher 禁止用 `reply_to_message_id/history` 另一套规则静默重算。
2. Context refresh 若确实改变模式，必须调用同一 resolver，原子更新 `chat_mode + context_message_ids + anchor_message_ids + mode_reason + contract_version`；字段缺失或不一致显式记录错误，不能静默降为 `idle_warmup`。
3. idle/bootstrap 的事实锚点质量门必须消费 `anchor_message_ids`。存在当前有效锚点时，复述锚点中的细节不能仅因命中“上次/位置/准时/回访”等词面而判 hallucination。
4. 上线前先做同一候选池的 planned/derived 双重影子重放，输出 `mode_drift_count`、`decision_diff_count`、`anchored_false_reject_count`；不得发送消息。

### 14.3 H-02 会话级发言价值调度待设计

1. 新决策层位于任务义务确认之后、Provider 生成之前，只输出 `send|wait + reason + next_eligible_at`；`wait` 不得伪装为 success/skip，也不得释放群日主槽或 coverage 义务。
2. 输入至少覆盖：最近真人/平台远端顺序、collective platform run、真人活跃变化、当前 reply/addressee 锚点、任务欠账与截止时间、账号/群发送门、候选内容价值信号。
3. 禁止把固定连续条数 cap 作为正式方案；生产回放已经证明 cap=1/2/3 会分别丢失 71.17%/55.18%/45.44% 的现有平台发言。
4. 首版必须先影子输出，不影响 Action/Gateway；验收同时看 platform run 分布、任务义务保留率和同群同时间的人类参与基线，不能只优化“少说话”。

### 14.4 H-03 Persona 稳定层与动态层待修复

1. Persona 拆为稳定层（mask、identity、audience、preference、稳定表达摘要）和动态层（群内 stance、当前上下文）；最近 AI 成功文案不得默认回灌稳定身份。
2. Voice profile 生成后必须在同租户全部 active profile 范围做精确与语义碰撞检查，不能只比较当前两个账号；重复时保留显式冲突状态，不用随机改词伪造多样性。
3. no-send A/B 只删除 `近期表达` self-history，其他 persona 与 stance 完全一致；用相同上下文、相同账号和位置交换盲评比较自然度、上下文贴合、模板感与账号可辨识度。
4. 上线验收必须包含 author identification：盲测者在同上下文的多个账号输出中识别归属；画像非空、hash 不同或 similarity=100 均不能单独判通过。

### 14.5 H-04～H-06 前置资产与授权

- H-04：建立版本化真人 pairwise gold set，覆盖普通接话、原生 reply、不同活跃度、事实锚点、模板腔和拗口问句；至少双人独立标注并保留分歧，不得用当前 LLM Judge 自产标签自校准。
- H-05：设计 `reply_to_remote_message_id / parent_sender_kind / parent_content_hash / addressee_account_id` 等关系字段、采集留存周期与脱敏；父正文原文是否持久化属于隐私/产品待决策项。
- H-06：只有用户批准目标群、时间窗、最大影响量和回滚后才能创建随机静默 canary；主要指标必须使用同群同小时 control，显式 reply/reaction 优先于普通 next-human。

### 14.6 H-07 真人节奏指纹待验证

生产聚合已证明平台 burst 与长 schedule lag 存在；真人 turn 的同秒集中度受 listener 批量入库或时间精度污染，尚不能作为可学习的真人节奏分布。以下设计仍需补齐按群/小时分层及真实远端时间口径：

1. 先以无正文聚合比较真人与平台消息的 `inter_message_gap`、整秒/固定间隔集中度、session burst 和 `scheduled_at -> executed_at` lag；必须按群、小时和活跃度分层，不能把全局平均当真人节奏。
2. 若差异成立，调度只学习每个群/时段的经验分布，并按最终文案长度增加输入时延；不得用一个全局随机抖动或固定 sleep 伪装真人。
3. 首版仅影子输出 `proposed_send_at + timing_reason + model_version`，不改变 Action、Claim、欠账或 Gateway；验收需同时观察节奏距离、义务保留率和过期率。

### 14.7 H-08 实际成稿趋同待验证

生产匿名聚合已证明实际发送文本存在窄长度带和跨账号开头趋同，H-08 从“待线上盘点”进入“设计待完成”；该结果是确定性分布证据，不等于真人主观偏好已通过。

1. 评价对象必须是成功发送的实际正文，而不是 profile 是否非空；按账号统计句长、问句率、终止标点、emoji、语气词、开头前缀和 exact collision。
2. 在同群同小时选取真人活跃 speaker 作为参照，比较账号内稳定性、账号间可辨识度和平台整体与真人分布距离；正文只输出匿名聚合，不在诊断结果落原文。
3. 若实际成稿仍趋同，先消除 profile 全局碰撞和 AI self-history，再做真人 author-identification；禁止靠发送后随机替换同义词制造表面差异。

### 14.8 H-09 单条临发送生成待验证

生产只读统计已确认：最近样本中 61.16% 的已成功正文在 `gateway_call_started_at` 前被新真人消息越过，且 21.16% 已超过 payload 配置的消息数过期阈值；因此发送前重校验是已证明的正确性缺口。逐条 Provider 生成是否稳定提升真人感、吞吐和成本仍待 no-send A/B。

1. 规划阶段可保留账号、话题、引用和数量槽，但普通正文只在单条 Action 即将执行时刷新一次最新上下文并生成；同一 Provider 请求不得预写未来多个账号的连续正文。
2. Action 的 slot、幂等键、quantity/coverage 义务和 speaker reservation 不因 late binding 改变；上下文变化导致无法生成时必须显式等待或失败，不能复用旧批次正文。
3. no-send A/B 使用同一生产冻结起点：baseline 一次生成多个 slots，实验组逐条生成并在两条间注入实际后续上下文；位置交换盲评比较剧本感、接话连续性、事实锚点与账号可辨识度。
4. 当前已有上下文过期与批次编排的生产聚合证据，但尚无 production Provider A/B 赢家；在效果和吞吐/成本都通过前不得关闭 batch 路径。

### 14.9 H-10 平台问句后的真人话轮保护待设计

1. 诊断用 60 秒只为排除长时间间隔造成的假阳性，不得直接固化为全局生产 sleep 或连续发言 cap。
2. 平台正文被识别为真实问句后，会话层必须记录 `awaiting_human_response + question_action_id + observed_at + reason`；后续平台 Action 先走与 H-02 共用的 value-aware `wait|send` 决策，不得用另一个账号立即自问自答。
3. 真人新消息、明确指向平台账号的 reply/addressee、问题已被回答或任务截止压力可结束等待；结束原因必须可审计，等待不得释放群日主槽或 coverage 义务。
4. 影子验收同时统计 60/300 秒问后平台接管率、真人首次回应率、任务义务保留率和超期率；只降低消息量不能判通过。

### 14.10 H-11 跨群内容零容忍隔离待修复

1. 隔离主键固定为 `tenant_id + group_id`。A 群真人上下文、reply/addressee、任务话题、老师、AI 历史正文、近期表达、stance、generation cache 和 message memory 原文只能进入 A 群 Prompt 与 A 群 Action；B 群同理。不得以同账号、同租户、同 persona 或“跨任务保持口吻”为由跨群读取正文原文。
2. 可跨群共享的账号信息只限稳定身份/语气的非事实属性，例如句长档位、emoji 策略、语气强度和禁用表达；不得共享群名、任务名、话题、老师、地点、价格、经历、近期正文或可逆还原的内容摘要。跨群 exact/semantic 去重只能使用不可逆 fingerprint/embedding 做“拒绝重复”，不得把 reference raw text 回填 Prompt。
3. `_recent_account_memories()` 和 `account_profile_summaries()` 必须移除其他 group 的正文原文；群内动态 memory 查询必须显式过滤 Action payload `group_id`，历史损坏或缺 group 的行不得参与生成。
4. 每次 Provider 调用前与 Telegram Gateway 调用前必须执行同一 fail-closed 不变量：`Action.tenant_id == Task.tenant_id == TgGroup.tenant_id`、`Action.payload.group_id == Task.type_config.target_group_id == final TgGroup.id`，且所有 `context_message_ids`、snapshot、reply target、AI memory、stance、SpeakerTurn key、账号群链接均属于同一 tenant/group。任一缺失或不一致写 `cross_group_content_scope_mismatch`，终止当前 Action，不改投其他群、不使用签到或旧缓存降级。
5. generation/cache key 必须至少包含 `tenant_id + group_id + task_id + action_id + generation_id + context_snapshot_message_id + scope_contract_version`；缓存读取必须重新校验 scope，不能只凭 generation id、claim token 或同 task 的上游约定。
6. 可观测性只记录 id/hash 和不一致字段，不记录原文。生产持续指标包括 `prompt_cross_group_raw_reference_count`、`pre_provider_scope_reject_count`、`pre_gateway_scope_reject_count` 和 `sent_scope_mismatch_count`；最后一项阈值永久为 0。
7. 上线红测至少覆盖：伪造其他群 context id/snapshot/reply id、memory id、Task/payload group、同 generation sibling、跨群 account history、损坏 tenant、旧 cache 和 SpeakerTurn key；所有用例必须在 Provider/Gateway 前失败，并断言 Telegram 调用次数为 0。
8. 验收必须同时满足：测试矩阵全绿、生产影子扫描 0 mismatch、canary `sent_scope_mismatch_count=0`、无跨群 Prompt 原文；“最近 30 天历史错配为 0”只能作基线，不能单独证明未来绝对安全。

### 14.11 新范围 Product Design Complete 闸门

| 检查项 | 状态 |
| --- | --- |
| H-01 no-send 候选池误杀率 | `blocked_by_production_ingress` |
| H-03 self-history 消融 A/B | `blocked_by_production_ingress` |
| H-02 wait/send 状态机、欠账一致性、API/观测/回滚 | `partial` |
| H-04 人标数据口径与验收阈值 | `blocked_by_human_gold_set` |
| H-05 relation schema、隐私与迁移 | `blocked_by_schema_design` |
| H-06 canary 授权、流量边界与因果指标 | `blocked_by_live_traffic_authority` |
| H-07 timing 生产只读 inventory | `partial_timestamp_precision_limit` |
| H-08 实际成稿区分度与匿名聚合口径 | `partial_online_evidence` |
| H-09 late-binding Provider A/B、成本、幂等与回滚 | `partial_online_evidence_provider_ab_pending` |
| H-10 question-floor 状态机、等待结束条件、欠账一致性与回滚 | `partial_online_evidence` |
| H-11 跨群原文移除、scope 不变量、缓存键、错误码、红测与生产观测 | `partial_online_failure_evidence` |

**humanization_speech_repair_design_status=`partial`，禁止进入 dev。** 只有线上只读项完成、阻塞项补齐产品决策，并同步总 PRD、数据流转索引、结构索引、QA 与回滚口径后，才能重新执行 Product Design Complete 自检。

## 15. 2026-07-31 确定性正确性修复闭环

### 15.1 Intake Card 与实施边界

- 等级：`L3`。生产已证明存在跨群原文进入 Prompt、发送前上下文过期、规则缺失不可见和多份 fallback 合同冲突；其中跨群正文隔离是零容忍安全项。
- 本轮进入 dev 的范围：H-11 全链 scope 不变量、H-09 单 Action late binding 与过期重生成、规则绑定错误可见、确定性签到边界、任务验收增加确定性会话质量维度。
- H-02/H-10 先落可审计影子状态，不以固定连发 cap 或固定 sleep 改变真实流量；H-03/H-04/H-05/H-06/H-07/H-08 的真人金标、关系 schema、静默流量授权和时间精度仍是独立后续验收项，不阻断本轮确定性修复进入 dev，也不得被写成已解决。
- 本轮不改变目标群、任务日目标、账号范围、Telegram 授权或生产启停；不在服务器直接补代码。

### 15.2 单一运行合同

1. 动态内容隔离键固定为 `tenant_id + group_id`，payload 必须携带 `content_scope_contract_version=group_content_scope_v1`、`content_scope_tenant_id`、`content_scope_group_id`、`content_scope_task_id`。四个字段全部为空的历史 pending Action 写精确 `scope_contract_missing`，终结旧 Action 后由既有结算链把原 `ContentMixCycleSlot` 置 `replan_required`、原主数量槽重新 `open`；不迁移旧正文、不新建替代槽、不改投其他群。任一字段部分缺失、值不一致或身份链不一致仍写 `cross_group_content_scope_mismatch` 并 fail closed。
2. Provider 前和 Gateway 前复用同一 scope validator。它校验 Task、Action、TgGroup、payload target、chat peer、context/snapshot/reply target、message memory、账号群链接和 scope snapshot；失败统一写 `cross_group_content_scope_mismatch`，Telegram 调用次数必须为 0，且禁止签到、旧缓存或换群降级。
3. 账号近期正文、任务名、topic、teacher、stance 与可逆摘要仅限当前群；跨群只允许独立 voice profile 中已审核的稳定 style scalar。当前 `_recent_account_memories()` 与 `account_profile_summaries()` 改为显式同群查询，不再读取其他任务/群正文。
4. normal 生成改为单 Action late binding。CycleSlot、数量槽、账号与排期仍在 Planner 冻结；Dispatcher 每次只为即将发送的一条 Action 刷新当前群上下文并调用 Provider，不再用一个请求预写 sibling 正文。
5. ready normal 在 Gateway 前发现更新真人上下文时，旧 memory 标记 `expired_before_send/generation_context_superseded`，同一 Action/slot 清空正文和 cache、回到 `ai_generation_status=pending`，保留数量与内容义务重新生成；不得跳过整 Cycle，也不得释放成伪成功。
6. `chat_mode` 使用 payload 已冻结值；只有上下文刷新调用同一 resolver 并同时更新 mode、context、anchor 与 reason 时才允许变化。Dispatcher 不再用“是否有 history”静默推导另一份 mode。
7. Provider 编排统一为主 Provider 最多 3 次、不同备用 Provider 最多 3 次。旧 M3→M2.5→Grok 每层一次的文字合同被本节 supersede；每次 attempt 的预算复用实际 AI 请求超时，slot 最晚安全发送时间取主数量槽所属 `TaskDayLedger.deadline_at`。下一次完整 attempt 无法在该时间前结束时写 `ai_generation_deadline_budget_exhausted`，把原 CycleSlot/主数量槽终结为 `terminal` 并把仍绑定的内容义务记为 `shortfall`，不得再回 `replan_required` 空转；被预算跳过的 attempt 不算六轮失败，不能据此进入签到。
8. 确定性 `签到` 只允许三类来源：coverage 缺面具、已验证代理路线切换、主/备用六轮正常生成失败。缺面具必须绑定合法 `coverage_ledger_id`；其他原因必须有非空 `primary_quantity_slot_id`。全部路径都必须开启静态兜底、非 scope 错误且通过原账号 10 天签到去重。缺面具的 `extra_volume` 不发送；六轮失败的数量槽只有在其 `ContentMixCycleSlot` 不再绑定当前 Action 的任何 `pending` 内容义务时才可签到，不能只凭 payload 的 `material_intent` 为空推断已经转派。其他配置、规则、target、context、tenant/group 错误一律不得签到。

### 15.3 should-speak、问句保护与履约一致性

1. `should_speak` 位于数量/coverage/content-mix 义务冻结后、Provider 前，只输出 `send|wait`、reason、observed watermark 和 `next_eligible_at`。首版仅影子记录，不改变 Action/Gateway。
2. 平台问句写 `awaiting_human_response` 影子状态；真人新消息、明确 reply/addressee 或已观测回答才能结束。60/300 秒只作指标窗，不是 sleep、TTL 或截止压力强制发言规则。
3. `wait` 不计 success/skip，不释放 coverage、主数量槽、内容义务或 speaker reservation。影子阶段不得以它减少日量；转实发前必须先证明同群同小时 matched control 下真人接话提升且任务义务保留率不退化。
4. 截止压力不能绕过 scope、target、授权、内容安全、上下文新鲜度和问句保护；因此某日可能诚实形成 shortfall，禁止用跨群内容、过期正文或未授权签到伪造达标。

### 15.4 状态、API 与前端

- AI 活群任务新增 `conversation_quality_status=not_applicable|evaluating|met|at_risk|blocked|missed`。确定性安全合同（scope、freshness、规则、reply、persona provenance）逐条计入；真人主观分数只作为 canary/release gate，不因样本不足伪造 `met`。
- `acceptance_status` 对 AI 活群由 `quantity_status + content_mix_status + conversation_quality_status` 组合；其他任务保持现状。详情分列 `pre_provider_scope_reject_count`、`pre_gateway_scope_reject_count`、`context_superseded_requeue_count`、`rule_binding_missing_count`、`check_in_fallback_count` 和 `question_floor_shadow_violation_count`。
- 前端只读展示三维状态与 blocker；不新增一键忽略 scope/freshness 的写入口，不展示跨群原文或完整 Prompt。未知人工项显示 owner、首次发现时间和 backlog age；不能只显示“执行中”。

### 15.5 数据、并发、幂等与失败路径

- 不新增第二套任务成功事实；scope snapshot 与影子观测写 Action payload/result，成功仍只由 Action + ExecutionAttempt + 非空 `remote_message_id` 确认。
- 同一 Action 每次 late-binding attempt 使用新 attempt/request id；Action、CycleSlot、数量槽、coverage reservation 不变。旧 attempt 的 Provider 返回由 fencing 拒绝，不能覆盖新上下文结果。
- listener watermark 缺失、断档或最后成功轮询早于生成快照时，normal 写 `context_freshness_unproven` 并等待；不得把数据库“暂无新消息”等同于远端无新消息。当前 `ListenerSourceState` 尚未形成普通群 listener 的连续 remote cursor 证据，在该采集合同补齐并迁移前，只能把“最后成功轮询不早于快照且无错误”视为最低门槛，不能宣称 cursor-gap 已达标；该缺口进入 Release Gate blocker。reply 仍须远端探活冻结目标。
- scope mismatch、规则缺失、target 不一致是硬失败/重规划；Provider 不可用和新上下文到达是同槽可重试；Gateway 已开始后的 unknown 继续禁止自动重发。所有错误保留原 error code，不被 generic generation failure 覆盖。
- 历史 pending Action 在发布前按 `scope_contract_missing` 转 `replan_required`；不迁移旧正文进入新 scope，不删除历史审计。

### 15.6 QA、Release Gate 与回滚

1. 单元红测覆盖 H-11 §14.10 的全部伪造场景；分别断言 Provider/Gateway 调用为 0、错误码精确、原任务/群不变、无签到降级。
2. 真 PostgreSQL 并发测试覆盖两个 worker、旧 attempt 晚回、上下文在 Provider 后/Gateway 前更新、旧 Action scope 缺失、同 generation 不同群伪造 sibling；不得串 slot、串账号或释放数量义务。
3. fallback 矩阵覆盖 coverage/extra-volume、缺面具/六轮失败/scope mismatch、开关、10 天去重、reply 与素材义务；只允许 §15.2 第 8 条组合进入精确 `签到`。
4. E2 需通过定向与相关全量测试、真实 PostgreSQL、静态复杂度与文档/索引校验。E3 走 `master -> release -> GitHub Actions Deploy Production`，不得 SSH 改代码。
5. E4 先做不发送影子扫描，再做已批准小流量 canary；`sent_scope_mismatch_count` 永久为 0，Prompt 跨群原文为 0，并连续 3 个完整任务日本地日窗口观察确定性质量、数量、content mix 与真人化指标。runtime healthy 不等于 E4。
6. 回滚只回应用和开关，不恢复批量预写、不绕过 scope validator、不重新发送 unknown；必要时暂停 AI 活群发送，保留 Action/attempt/memory/ledger 审计。

### 15.7 Product Design Complete

原始问题、业务目标、前端状态、后端/worker、API 投影、数据流、权限与跨群安全、并发幂等、失败路径、迁移、QA、E2/E3/E4 和回滚已覆盖；与全任务达标、Provider fallback、generation transaction 的冲突由 §15.2 明确 supersede。

`deterministic_humanization_repair_design_status=complete`，允许 H-11、H-09、规则可见性、fallback 边界和确定性质量状态进入 dev。依赖真人金标或真实流量授权的效果项继续保持 `partial/blocked`，不得并入本轮完成声明。

### 15.8 2026-08-01 实现后补强合同

1. Dispatcher 的 speaker rotation 若把 ready Action 从账号 A 攁绑到同群账号 B，必须先把 A 的 message memory 写 `expired_before_send/speaker_rebound`，清空正文、cache、attempt/request 与生成 claim，保留原 Task/group/CycleSlot/数量槽，Action 回 `pending` 并退出本次 Dispatcher；只能由 generation worker 按 B 的当前群画像重新生成。不得在 Dispatcher 调 Provider，也不得把 `ai_generation_not_ready` 记成失败。
2. `context_expire_after_messages` 的正式默认值为 10。新建、更新、启动恢复和 Planner 读取存量配置都必须统一归一；历史缺字段写 10，只有用户显式保存 0 才表示关闭消息数阈值。迁移不得改任务目标、数量、账号或已有远端事实。
3. Provider Prompt 必须实际消费当前群重建的短期 `account_memory`，并逐 slot 安全传入 `account_profile/topic_direction/teacher_target/material_intent/content_guidance`；所有自由文本继续经过同一安全净化，禁止因为恢复人设与内容义务重新引入跨群原文或不安全字段。测试必须断言最终 `GroupPromptBundle.input_payload`，不能只断言 runtime config 存在字段。
4. conversation quality 的计数与 blocker 更新必须在同一 Task 行锁或等价原子 CAS 下读取最新 `Task.stats` 后合并；不同 Action/slot 的并发记录和清理不能互相覆盖。SQLite 只验证 stale-object 合并，Release Gate 保留真实 PostgreSQL 两事务回归。
5. AI 活群详情统一投影 `quantity_status/content_mix_status/conversation_quality_status/acceptance_status`。quantity 只读当前任务日本地日期的 `TaskGroupDailyMessageSlot`：全部 confirmed 为 met，任一 terminal 为 missed，unknown 为 at_risk，其余 evaluating；content mix 只读同日 `ContentMixCycle.settlement_*`：全部 settled/met 为 met，shortfall/missed 为 missed，其余 evaluating。acceptance 仅三维全 met 时为 met；任一 missed 为 missed，任一 blocked 为 blocked，任一 at_risk 为 at_risk，否则 evaluating。
6. 普通群 listener 持久化 `listener_remote_cursor + listener_cursor_status(unproven|contiguous|gap)`。首次成功数值窗口建立 bootstrap cursor；后续窗口必须覆盖旧 cursor，或从旧 cursor 的下一可见消息连续衔接，才能推进 contiguous。窗口截断、空窗口无法证明旧 cursor、非数值 cursor 或拉取失败都不能伪造 contiguous；gap/unproven 阻止 normal Provider。该状态只证明最近两次可见 Telegram 窗口连续，不把轮询时间当远端完整性，也不取代 Gateway 前 scope/context 二次校验。
7. `ai_generation_dispatch.py` 按 guard/freshness、quality stats、request orchestration 分责拆分，每个文件不超过 500 行、函数不超过 50 行；保留旧公共导入的显式 re-export，避免 Dispatcher/测试形成第二套实现。

`post_implementation_repair_design_status=complete`。QA 必须先出现 speaker rebind、旧配置、stale stats、Prompt dead input、三维 acceptance 和 listener gap 的红测，再实施代码；发布与 Telegram E4 继续独立取证。

### 15.9 2026-08-01 任务履约回归补强

1. speaker rotation 为当前 Action 选中账号 B 并写入 `ConversationSpeakerState.reserved_action_id/reserved_account_id` 后，同一 Action 从 generation worker 回到 Dispatcher 时必须幂等复用 B；不得把自己的 reservation 当成“上一个平台账号”再改绑回 A。只有不同 Action 或已结束 reservation 才重新执行账号轮换。
2. Action 从账号 A 改绑到 B 并回 `pending` 前，必须释放本次 Dispatcher 为 A 持有的本地 inflight、Redis account lock 和 token reservation；speaker reservation 仍由当前 Action/B 持有，直到发送结算、失败或显式释放。禁止用新 reservation 覆盖旧 Action reservation 记录后遗留 A 的容量锁。
3. QA 必须覆盖同一 Action 连续两次进入 speaker gate：第一次 A -> B 退回 generation worker，第二次 B 幂等通过且不回 A；同时断言 A 的 runtime/Redis reservation 已释放。该回归属于任务可完成 P0，不以 2395 条通用 no-PostgreSQL 绿测替代。
4. conversation quality 获取 Task 行锁前不得 flush 基于旧快照修改的整块 `Task.stats`。实现必须先读取数据库最新 JSON，再只合并当前事务相对原快照的键级增删，之后写入本次质量计数/blocker；QA 同时保留“并发事务已提交字段”和“当前事务锁前本地字段”，真实 PostgreSQL 两事务仍作为 Release Gate。
5. 普通 listener 有数值 `listener_remote_cursor` 后，下一轮必须从该 cursor 之后按远端正序读取最早未见窗口，禁止继续只拉最新 N 条。满页表示可能仍有积压：允许推进 cursor，但状态保持 `unproven`；未满页或空页才证明已追平并转 `contiguous`。非数值、窗口失败或 cursor 身份不一致继续 fail closed。多 listener 账号只能用单一可用账号的一次窗口证明 cursor，禁止合并重复窗口伪造页满或连续。该恢复路径必须让一次 `gap` 可自动追平，不能永久阻断任务，也不能在追平前放行 Provider。
6. 已预留 speaker 的 Action 若在 generation worker 或 Dispatcher Gateway 前进入 `failed/skipped/retryable_failed`，必须释放该 Action 的 `ConversationSpeakerState` reservation；`pending` 的 speaker rebind/context requeue 必须保留 reservation 供同 Action 幂等续跑。禁止把“生成失败但未发送”的账号当成上一位平台发言者，也禁止失败 Action 让单账号任务永久 `speaker_rotation_wait`。
7. 为覆盖“Action 终态已提交、reservation 释放事务未提交”之间的进程退出，下一次锁定同一会话 speaker state 时必须检查不同 `reserved_action_id` 的 holder；holder 不存在或已终态时先清 reservation，再依据真实 `last_platform_*` 选择账号。当前同一 pending/executing Action 的 reservation 不得被该恢复误清。

`speaker_rebind_fulfillment_repair_design_status=complete`。本节只补账号改绑幂等和资源释放，不改变话术策略、数量义务、准入或 Gateway 安全门。

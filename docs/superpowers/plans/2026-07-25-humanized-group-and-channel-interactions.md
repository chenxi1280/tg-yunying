# 群聊与频道评论真人化互动及群管机器人准入优化 Implementation Plan

> **使用说明：** 本文件是待开发实施方案；按任务逐项实现和验收即可，不代表本轮已经修改任何业务代码。步骤使用 checkbox（`- [ ]`）跟踪。
>
> **2026-07-25 产品修订绑定：** 实现必须服从 `docs/03-feature-designs/ai-conversation-humanization-and-group-bot-admission-prd.md` 评审修补合订版，不得弱化下列约束：观察闭合窗口≠放行、`not_required` 一键闭环、完成事件识别器、可见性 SLA、membership 相位、单账号轮换产能 warning、签到配额、C0–C3 迁移 canary、与 continuity unknown/credit 对齐，以及 hard-hourly 发送后重发 / 日覆盖 `emoji_react` / `consecutive_message_*` 的 supersede。
>
> **continuity 交叉 P0/P1（必须实现）：** `pending_visibility`∈`unknown_after_send_hold_count`；`pending_visibility_credit` 后正式 credit；`admission_abandoned` 排除 durable_debt；follow/观察复用 `target_admission_retry` 档且限 tenant+task+account；定义 `admission_version`；C1 存量新建 action 为 legacy 路径且 C2 不回溯；存量 unknown 只走 continuity 裁决。

**Goal:** 让 `group_ai_chat` 与 `channel_comment` 的已发送消息具备可验证的账号轮换、原生引用回复、`签到` 兜底和去模板化内容质量；同时让 AI 活群账号在入群后先完成群管理机器人的“必需频道关注”准入，再进入可发言账号池。任何无法满足真人化或群管准入约束的 Action 必须明确延期或跳过原因，不能静默退化成同账号连发、泛化 AI 文案或先试发再补救。

**Architecture:** 账号入群成功不等于可发言：每个 AI 活群账号先进入独立、可审计的群管机器人准入状态，按入群后游标监听并精确归属群管提示。提示要求关注频道时，逐个完成精确频道关注，再按该目标群的已审计协议等待同一可信机器人的明确放行，或使用显式配置的 `follow_sufficient`；提示不存在时，只有连续游标观察与目标级 `not_required` 策略共同成立才能放行。`probe_target_capabilities()` 只记录 Telegram 传输层事实，绝不作为群管机器人放行证据。以目标会话为边界新增持久化的“最近发言账号 / 发送预约”状态。Planner 负责生成轮换倾向、引用目标和内容意图；Dispatcher 在 AI 调用和 Telegram Gateway 之间以短事务再次锁定会话状态，保证并发 worker 不会让同一账号相邻发言。内容生成使用统一的真人化质量门，群聊与评论各保留已有的领域规则、原生 `reply_to_message_id` 和规则快照。`签到` 是唯一允许的确定性文案兜底，且必须显式记录来源，不可伪装成 AI 正常生成。

**Tech Stack:** Python 3、FastAPI、SQLAlchemy、PostgreSQL、Alembic、pytest、React/TypeScript。

---

## 0. 已确认的现状与本方案的修正点

| 现状 | 风险 | 本方案的处理 |
| --- | --- | --- |
| `GroupAIChatConfig`、向导和详情页暴露 `consecutive_message_*`；`group_ai_chat._consecutive_burst_plan()` 会故意安排同一账号连发。 | 与“一个账号连续发几条太像 AI”的新口径相反。 | 删除连发配置、Planner 的 burst 计划和展示，不保留开关。 |
| 评论 Planner 只按 slot/index 调 `pick_channel_account()`；群聊的跨轮账号排序也没有以目标会话的最后实际发言为事实。 | 多 task、多轮、延迟重排或并发 worker 后仍会同账号紧挨着发。 | 新增跨 task 的持久化会话发言状态，并在 Gateway 前做最终轮换预约。 |
| 两条链路已经支持 `reply_to_message_id`，但群聊 `reply_min_per_round` 和评论 `reply_min_per_message` 默认都是 `0`；频道评论默认 `comment_mode=comment`。 | 即使已有可引用消息，正常创建的任务也可能全是普通发言/评论。 | 新任务默认使用自动引用策略；有合格候选时至少产出一条原生引用回复。 |
| 现有引用候选不足会直接中断某些规划。 | 为了“必须引用”反而让任务停住，且不容易分清是缺上下文还是系统故障。 | 不伪造引用；记录 `reply_target_shortfall`，保留可安全执行的普通动作，并在候选恢复后优先补引用。 |
| 现有 prompt、关键词过滤、语义去重已存在，但兜底会产生“有人在吗”“这事可以再看看”等泛化句，群聊与评论没有同一份真人化判定与发送前复核。 | 内容仍容易呈现模板腔或重复的 AI 结构。 | 用共享质量门拒绝模板壳、重复起句、脱离上下文和错误引用；唯一固定兜底改为 `签到` 并留痕。 |
| 现有必需频道处理一部分依赖发言后的 `GROUP_PERMISSION_DENIED` 补救；监听侧又把群管机器人提示混在普通上下文入库以后才处理。 | 群管理机器人可能拦截首条真实消息；账号已经入群却尚未完成频道关注时，系统仍可能把它当成可发言账号。 | 入群即建立带游标的群管机器人控制观察；群管消息走独立控制事件、精确关联到入群账号。仅能凭同一可信机器人的明确完成事件、或目标级审计的 `follow_sufficient` 放行；正常 `send_message` 不承担发现或修复职责。 |
| `can_send`、普通权限探测和群管机器人规则当前没有明确区分。 | Telegram 可发不等于群管已放行；若把探测成功当成 ready，会让首条正常消息仍被群管拦截。 | `TgGroupAccount.can_send` 只保留 Telegram 事实；`GroupBotAdmission` 记录协议、来源、游标和准入结论，`probe.ok` 不可推进 ready。 |
| 最近 N 条上下文快照没有服务事件、按钮、回复关系和入群后的连续游标。 | 无法证明提示发生在本次入群之后，也无法安全执行所要求的确认按钮。 | 为群管控制事件补齐远端 ID、发送者角色、回复/服务/按钮和游标契约；严格按 `> join_start_cursor` 增量观察。 |
| Gateway 返回远端 message id 后即按发送成功处理。 | 群管机器人仍可能紧接着删除或拦截正文，覆盖账本和轮换状态会误计为成功。 | 首条准入后正文与规则版本变更后的首条正文必须做无正文可见性核验；被拦截写 `post_send_intercepted`，不自动重发。 |

## 1. 产品口径与不可突破的边界

### 1.1 本次采用的“连续发言”定义

1. 会话键不是 Task ID，而是实际 Telegram 目的地：
   - 群聊：`tenant_id + group_ai_chat + group_id`；同一群的多个 AI 活跃任务共享最后发言账号。
   - 频道评论：`tenant_id + channel_comment + linked_discussion_group_id`；同一讨论区、不同频道消息的评论也共享最后发言账号。
2. **相邻**以 Telegram 会话的真实时间顺序为准：两条由本平台成功发送、或前一条已进入 Gateway 且结果为 `unknown_after_send` 的消息之间没有真人消息时，才构成同账号连续发言；不依赖 Planner 生成顺序，也不依赖 UI 的 Action 排序。群管机器人控制消息不打断连续发言。
3. 若存在另一个同时满足账号状态、目标权限、容量、任务账号范围和覆盖义务的账号，下一条必须换账号；Planner 的建议不构成最终保证，Dispatcher 的预约才是最终裁决。
4. 若没有替代账号且最近一条平台消息后没有真人消息，系统不得偷偷放宽为同账号连发：Action 写为 `pending / speaker_rotation_wait`，等待新的真人上下文或替代账号恢复。引用锚点已经过期时写 `skipped / speaker_rotation_context_expired`，不得改成普通发言冒充完成。
5. 带 `coverage_ledger_id` 的群聊 Action 仍只能由账本绑定账号执行；发生轮换冲突时只能延期或按覆盖账本规则释放原义务后重新规划，绝不能静默改绑到其他账号。

### 1.2 引用回复口径

1. 群聊新任务默认 `reply_min_per_round=1`；频道评论新任务默认 `comment_mode=mixed`、`reply_min_per_message=1`。该数量包含在原总量中，不额外加量。
2. 只要有可用候选，Planner 必须将至少一条 Action 写为真实 `reply_to_message_id`；发送 Gateway 必须携带该字段。UI 标签、Action payload 和远端发送形式必须一致。
3. 群聊优先回复最近的真人消息；频道优先回复已采集的讨论区评论。候选必须属于当前目标、未撤回/未失效、未被当前账号自己发送，且引用内容仍能支撑生成文本。
4. 没有候选时不伪造 `reply_to_message_id`，也不把“回复某人”写进正文；保留普通动作并记录短缺原因、候选数和下一次可重试条件。
5. 单条评论快捷入口的显式 `reply_to_message_ids` 仍是强约束；该入口缺目标时失败，不降级成普通评论。

### 1.3 `签到` 兜底口径

1. `签到` 是两类任务的唯一确定性兜底正文，精确文本为 `签到`；不能再回退到“有人在吗”“这事可以再看看”等泛化模板。
2. 只允许用于**非引用** Action：正常生成和真人化质量重试均无合格候选、且该会话最近一条平台消息不是 `签到` 时，才可使用。
3. 引用 Action 生成失败不能改成未引用的 `签到`；保持原引用语义，明确失败/延期，等待有效引用上下文。
4. `签到` 必须在 payload/result 中写 `content_source=check_in_fallback`、`fallback_reason`、`human_quality_decision=check_in_fallback`，详情页可见。若最近平台消息已是 `签到`，Action 明确延期或跳过，绝不连续发送两个 `签到`。
5. `签到` 仍受租户显式 `static_safe_fallback` 策略控制；关闭时只允许可见地延期/跳过。该策略不能修改固定正文，不能把引用动作降级，也不能绕过轮换、群管准入或出站门禁。

### 1.4 内容真人化质量口径

- 每条内容只完成一个自然意图：回应、追问、补充、轻微反应或签到；不得用一条“总结 + 夸赞 + 号召讨论”凑完整观点。
- 非签到内容必须能对应当前上下文、频道原文或引用目标中的至少一个具体事实；没有事实锚点时只允许具体小问题，不能虚构体验。
- 账号面具、口吻、近期已发内容和起句历史都进入判定；同会话近期不能复用同一模板壳、同一语义簇或同一开头。
- 质量门只拒绝/要求重新生成并写明原因，不对正文做不可追溯的静默改写。

### 1.5 AI 活群入群后的群管机器人准入口径

1. 对 `group_ai_chat`，账号“已入群”只表示 `joined`，**不表示群管机器人已放行**。入群成功时立即创建该账号、该群和该次入群动作绑定的 `awaiting_group_bot_rule` 准入记录，并从 AI 活群可发言候选池排除。
2. `TgGroupAccount.can_send` 只表达 Telegram 的传输/权限事实；群管机器人准入必须由独立 `GroupBotAdmission.state` 表达。任何业务规则不得为了“等待关注频道”把 Telegram 权限改成 `False`，也不得因准入完成把未知的 Telegram 权限改成 `True`。
3. 监听器和入群后的高优先级控制观察必须从该次入群的远端游标开始读取群管理机器人/系统消息；该控制事件在普通上下文去重、忽略发送者、学习样本和 AI 回复候选之前处理。群管提示可留审计副本，但不得成为 AI 学习样本、回复引用候选或生成提示词上下文。
4. 提示必须来自可信群管来源：首次事件要求 `is_bot=True` 且发送者是群管理员/群主，之后只能接受同一机器人 peer 的控制事件。提示必须精确归属到账号：优先使用被 @、用户名、展示名、回复关系或入群事件关联；没有显式身份时，只有同一群、同一观察窗口内**唯一**等待群管规则的账号可归属。归属不唯一时写 `group_bot_rule_unattributed` 并保持等待，绝不把提示批量套用给群内所有账号，也不猜测频道。
5. 已归属提示中的每一个真实频道引用都要单独创建持久化的关注动作；该动作只能关注该可信提示中解析出的频道，并要求 Gateway 验证目标为广播频道。所有引用关注成功后进入 `awaiting_group_bot_confirmation`；只有同一可信机器人给出明确放行/完成事件，或运营在该群明确配置并审计“关注成功即放行”协议时，才将该账号标为 `group_bot_admission_ready`。无正文 `probe_target_capabilities()` 只能记录 Telegram 传输层观测，不能单独作为群管机器人放行证据。
6. 首次控制观察只有在入群后游标连续、且目标级 `not_required` 策略已经审计时，才能写 `group_bot_rule_clear` 并进入 ready；观察失败、远端游标无法追平、来源不可信、提示缺频道、归属不明或协议未配置时均保持非 ready，不生成可执行的正常发言 Action。后续新的可信提示会立即把已 clear/ready 账号降回等待状态，并释放尚未进入 Gateway 的覆盖预约。
7. 普通正文、测试消息和 AI 生成都不能作为“是否需要关注频道”的探测方法。现有发言后恢复逻辑仅保留给历史遗留/状态失真的隔离修复，并标记 `legacy_group_bot_intercepted`；它不得是新账号的正常发现路径，也不得在同一次发言中自动补救后重发。
8. 对已经被群管机器人明确要求的频道，`auto_follow_required_channel=false` 不能成为绕过开关；AI 活群向导不再暴露这种关闭准入的选项。该规则只作用于本次 `group_ai_chat` 群聊准入，不把群管机器人消息扩展为频道评论或普通群上下文策略。

### 1.6 未决协议与存量账号

1. “没有观察到提示”不是放行事实。控制游标、可信来源或目标协议不足时，状态必须是 `group_bot_policy_unresolved`，由运营补充 `not_required`、`explicit_bot_confirmation` 或该群+该机器人 peer 的 `follow_sufficient` 审计配置。
2. 存量已入群账号不得批量改写 `can_send`，也不得因旧值为 `true` 自动通过。它们仅创建“存量群管准入复核”记录；在新策略下未完成复核的账号不计入 ready 容量。
3. 私聊机器人、非管理员机器人、转发文本和来源无法证明的按钮都不能自动驱动关注或放行；必须保留可见阻塞原因。

### 1.7 发送后可见性

1. Gateway 返回 `remote_message_id` 只说明调用边界得到回执。首次完成群管准入后的正文、以及规则版本变化后的首条正文，必须在不发送第二条正文的前提下做远端可见性核验。
2. 可信群管机器人删除/拒绝该消息或可靠复核证明消息不存在时，写 `post_send_intercepted`，停止后续 action、撤回准入 ready；覆盖账本不计完成。
3. 无法确认时保留 `unknown_after_send`，继续占用会话最后发言账号，既不自动重发，也不以该未知状态放宽轮换。

## 2. 目标数据流

```text
任务配置 / 已有任务迁移
  -> AI 活群账号入群 -> 记录 join_start_cursor -> 群管机器人准入观察（账号仍不可发言）
  -> 精确群管提示归属 -> 每个必需频道的关注动作 -> 可信机器人明确放行 / 已审计 follow_sufficient
  -> Telegram 传输权限独立复核 -> ready 账号池
  -> Planner: 回复目标排序 + 轮换倾向 + 内容意图（仅从 ready 账号池选择）
  -> Action payload: 会话键、计划账号、reply_to、内容策略、质量快照、群管准入版本
  -> Dispatcher 短事务: 群管准入门 -> 目标门 -> 会话发言预约 -> 必要时账号重绑
  -> AI 生成 / 质量门: 正常内容 | 明确的 签到 兜底 | 明确失败
  -> Gateway 前复核: target + reply + 会话预约 + 内容过滤
  -> Telegram Gateway
  -> 首条准入后正文/规则变更后首条正文做远端可见性核验
  -> 成功/unknown/post_send_intercepted/失败回写会话状态、Action result、任务统计和详情页
```

群管准入、会话发言预约都必须在外部 AI 调用和 Telegram 调用之外完成，不能持有数据库锁等待模型响应。模型生成失败、内容被拒绝且尚未进入 Gateway 时，只释放属于当前 Action 的预约；`success`、`unknown_after_send` 与待可见性核验的发送都保留最近发言账号，以免未知结果后紧接着同账号再发一条。`post_send_intercepted` 不计成功或覆盖完成，并使该账号退出 ready 池。群管理机器人控制事件永远不能因普通上下文已存在、被忽略或未用于学习而丢失。

## 3. 实施任务

### Task 1: 先补齐总 PRD、专项 PRD 和数据流索引

**Files:**

- Modify: `docs/01-product/tg-ops-platform-prd.md`
- Create: `docs/03-feature-designs/ai-conversation-humanization-and-group-bot-admission-prd.md`
- Modify: `docs/00-index/project-dataflow-index.md`
- Modify: `docs/00-index/project-structure-index.md`
- Modify: `docs/03-feature-designs/README.md`

- [ ] **Step 1: 在总 PRD 的 AI 活跃群、频道评论/回复配置段写入统一产品口径。**

写清“同会话相邻发言必须轮换”“引用默认开启但不得伪造”“无替代账号时延期”“签到兜底的精确正文与审计字段”“内容质量失败不能伪装为成功”，以及“入群后先完成群管机器人频道准入，普通发言不得用于探测或补救”。删除“同账号连发是可配置运营策略”的表述。

- [ ] **Step 2: 新建专项 PRD，固定边界、默认值和验收矩阵。**

专项 PRD 必须至少包含：会话键、账号候选定义、单账号/覆盖账本边界、引用候选优先级与失效处置、`签到` 状态机、群管机器人事件归属和准入状态机、并发与 `unknown_after_send`、迁移范围、发布回滚口径，以及下列验收矩阵。

| 原始诉求 | 可验证结果 |
| --- | --- |
| 群聊不让一个账号连续发多条 | 任一群会话的成功/unknown 发送序列无相邻相同账号；无替代账号时只见 `speaker_rotation_wait`，没有 Gateway 调用。 |
| 评论不让一个账号连续发多条 | 任一频道讨论会话的评论成功/unknown 序列无相邻相同账号。 |
| 群聊、评论有引用回复 | 有候选的群轮、频道消息批次至少一个 Action 带真实 `reply_to_message_id`，且远端调用携带它。 |
| 兜底用签到 | 质量或生成无法得到普通直接内容时，只能产生可审计的 `签到`，不会连续出现。 |
| 说话不像 AI | 模板壳、重复起句、无事实锚点、错误引用和近义复读均有拒绝码；抽样内容与质量统计可回溯。 |
| AI 活群入群后需关注频道 | 新账号不会先发正常消息；群管机器人提示只归属到对应账号，精确频道关注后仅能由同一可信机器人明确放行，或由该群+该机器人 peer 已审计的 `follow_sufficient` 协议放行；Telegram 权限探测不是放行证据。 |
| 群里已被解散 | 仅经目标生命周期的独立证据确认 `group_dissolved` 时跳过，展示“群里已被解散，已跳过本目标”；不能把 `qdsfxy` 等引用解析失败或群名匹配误写成解散。 |

- [ ] **Step 3: 更新索引。**

在数据流索引记录群管控制游标、可信来源、精确频道子 action、完成协议、会话状态服务、Planner、Dispatcher、AI 质量门、Gateway 后可见性核验与回写之间的顺序；在结构索引登记**设计待开发**的 model/service/migration 和前端可见字段，避免把规划文件误写成现有代码，也避免以后把 `reply_to_message_id` 只做成页面外观。

- [ ] **Step 4: 文档自检。**

Run: `rg -n "consecutive_message|签到|speaker_rotation|reply_to_message_id|群管机器人|必需频道|group_bot_admission" docs/01-product docs/03-feature-designs docs/00-index`

Expected: 新口径、数据流和验收标准都可检索；没有把旧“同账号连发”作为可选正常行为的未更新产品描述。

### Task 2: 删除同账号连发能力，收敛任务配置与前端入口

**Files:**

- Modify: `backend/app/schemas/task_center.py`
- Modify: `backend/app/services/task_center/config_fields.py`
- Modify: `backend/app/services/task_center/config_normalization.py`
- Modify: `backend/app/services/task_center/executors/group_ai_chat.py`
- Modify: `backend/app/services/telegram_bot_settings.py`
- Modify: `frontend/src/app/views/taskCenterViewModel.ts`
- Modify: `frontend/src/app/views/TaskCenterWizardSections.tsx`
- Modify: `frontend/src/app/views/TaskCenterView.tsx`
- Modify: `frontend/src/app/views/TaskCenterDetailModal.tsx`
- Test: `backend/tests/test_task_center_config_normalization.py`
- Test: `backend/tests/test_telegram_bot_group_ai_settings.py`

- [ ] **Step 1: 先写配置回归测试。**

```python
def test_group_ai_config_rejects_removed_consecutive_burst_fields():
    with pytest.raises(ValidationError, match="consecutive_message_enabled"):
        GroupAIChatConfig(target_group_id=7, consecutive_message_enabled=True)


def test_normalize_existing_group_task_drops_legacy_burst_fields():
    normalized = normalize_group_ai_interaction_config({
        "target_group_id": 7,
        "consecutive_message_enabled": True,
        "consecutive_message_min": 2,
        "consecutive_message_max": 4,
    })
    assert "consecutive_message_enabled" not in normalized


def test_group_ai_config_cannot_disable_group_bot_required_channel_admission():
    normalized = normalize_group_ai_interaction_config({
        "target_group_id": 7,
        "auto_follow_required_channel": False,
    })
    assert "auto_follow_required_channel" not in normalized
    assert normalized["group_bot_admission_required"] is True
```

- [ ] **Step 2: 移除旧字段及真实执行入口。**

从 `GroupAIChatConfig`、`TaskSettingsUpdate`、`TYPE_SETTINGS_FIELDS`、创建/编辑 payload、view model、向导、详情和 Bot 摘要中删掉 `consecutive_message_enabled`、`consecutive_message_min`、`consecutive_message_max`、`consecutive_message_probability`。删除 `_consecutive_burst_plan()`、`burst_plan` 对账号选择的强制绑定以及只为 burst 服务的展示字段；不要只把默认值改成 `false`。

对 `group_ai_chat` 同时移除 `auto_follow_required_channel` 的可关闭入口：旧 JSON 中的 `false` 在迁移时删除，运行时固定写入 `group_bot_admission_required=true`。不要改动其它任务类型原有的频道关注策略；本次只禁止 AI 活群绕开群管机器人已明确下发的频道准入。

对已保存 Task 的旧 JSON 配置，在一次受控数据迁移中删除旧键后再交给严格 schema 验证；运行时不保留“老 task 仍可连发”的兼容分支。未进入 Gateway 的旧 burst Action 进入本次策略重排，不修改已发送、执行中或 `unknown_after_send` 的历史事实。

- [ ] **Step 3: 设定新的默认引用策略并让页面不可绕开。**

`GroupAIChatConfig.reply_min_per_round` 默认改为 `1`；`ChannelCommentConfig.comment_mode` 默认改为 `mixed`，`reply_min_per_message` 默认改为 `1`。保留显式单评论回复入口，但普通向导只显示“至少引用回复数”和真实候选预检，不提供关闭真人化的“连发”开关或关闭群管频道准入的开关。

- [ ] **Step 4: 更新预检和详情信息。**

向导摘要展示“账号轮换：必须”“可引用候选数 / 当前短缺”“入群后需完成群管机器人准入”；详情移除“同账号连发”卡片，替换为最近发言账号、最近互动类型、轮换等待数、引用计划/成功/短缺数和签到兜底数。

- [ ] **Step 5: 运行定向测试。**

Run（由测试执行器强制 60 秒上限）: `backend/.venv/bin/python -m pytest -q backend/tests/test_task_center_config_normalization.py backend/tests/test_telegram_bot_group_ai_settings.py`

Expected: PASS；任何旧连发字段都不会重新进入运行配置或页面模型。

### Task 3: 建立持久化会话发言轮换状态并保证并发安全

**Files:**

- Create: `backend/app/models/conversation_speaker.py`
- Modify: `backend/app/models/__init__.py`
- Create: `backend/migrations/versions/<next>_conversation_speaker_turn.py`
- Create: `backend/app/services/task_center/conversation_speaker_rotation.py`
- Modify: `backend/app/services/task_center/payloads.py`
- Modify: `backend/app/services/task_center/channel_payloads.py`
- Test: `backend/tests/test_conversation_speaker_rotation.py`
- Test: `backend/tests/test_action_payloads.py`

- [ ] **Step 1: 写会失败的状态机测试。**

```python
def test_reserve_next_speaker_rotates_when_another_eligible_account_exists(session):
    seed_last_speaker(session, surface="group_ai_chat", key="group:7", account_id=101)
    decision = reserve_speaker_turn(session, action=action, candidate_ids=[101, 102])
    assert decision.allowed is True
    assert decision.account_id == 102
    assert decision.reason == "rotated_from_last_speaker"


def test_single_candidate_is_deferred_instead_of_silent_same_account_repeat(session):
    seed_last_speaker(session, surface="channel_comment", key="discussion:9", account_id=101)
    decision = reserve_speaker_turn(session, action=action, candidate_ids=[101])
    assert decision.allowed is False
    assert decision.code == "speaker_rotation_wait"


def test_unknown_after_send_keeps_reservation_for_next_turn(session):
    reserve_speaker_turn(session, action=action, candidate_ids=[101])
    finalize_speaker_turn(session, action=action, outcome="unknown_after_send")
    assert load_turn(session, action).last_account_id == 101


def test_real_human_message_breaks_same_account_adjacency_but_bot_control_does_not(session):
    seed_last_speaker(session, surface="group_ai_chat", key="group:7", account_id=101)
    record_conversation_event(session, key="group:7", sender_kind="group_bot_control", remote_message_id=20)
    assert reserve_speaker_turn(session, action=action, candidate_ids=[101]).code == "speaker_rotation_wait"
    record_conversation_event(session, key="group:7", sender_kind="human", remote_message_id=21)
    assert reserve_speaker_turn(session, action=action, candidate_ids=[101]).account_id == 101
```

- [ ] **Step 2: 建表并建立最小状态契约。**

不要把“最后一次本平台发言”压成唯一事实，否则无法证明真人是否已经打断连续关系。模型至少拆为：

1. `ConversationSpeakerState`：`tenant_id`、`surface`、`conversation_key`、`last_platform_account_id`、`last_platform_action_id`、`last_platform_outcome`、`last_platform_content_source`、`last_human_cursor`、`reserved_account_id`、`reserved_action_id`、`reserved_at`、`version`、审计时间；对 `(tenant_id, surface, conversation_key)` 建唯一索引，用于短事务锁和预约。
2. `ConversationSpeakerTurn`：`tenant_id`、`surface`、`conversation_key`、`remote_message_id`、`remote_cursor`、`sender_kind`（`platform` / `human` / `group_bot_control` / `system`）、`account_id`、`action_id`、`outcome`、`content_source`、`observed_at`；对 `(tenant_id, surface, conversation_key, remote_message_id)` 建唯一索引。真人消息推进 `last_human_cursor`；群管控制和系统事件保留审计，但不打断轮换。

两张表的会话键都不以 Task ID 作为键。监听器和 Gateway 回写必须共享同一远端顺序语义，不能用 Planner 的本地生成顺序替代。

Action payload 增加并严格校验：`conversation_surface`、`conversation_key`、`speaker_rotation_required`、`speaker_selection_reason`、`previous_speaker_account_id`、`content_source`。将可变的账号选择与业务 slot 去耦：新增稳定 `conversation_slot_key` 参与 dedupe，不能因运行时换账号让重新规划重复创建同一条互动。

- [ ] **Step 3: 实现短事务预约服务。**

```python
def reserve_speaker_turn(session: Session, *, action: Action, payload, candidates: list[TgAccount]) -> SpeakerDecision:
    turn = lock_or_create_turn(session, tenant_id=action.tenant_id,
                               surface=payload.conversation_surface,
                               conversation_key=payload.conversation_key)
    blocked_id = turn.reserved_account_id or turn.last_account_id
    alternate = next((item for item in candidates if item.id != blocked_id), None)
    if alternate is None and blocked_id in {item.id for item in candidates}:
        return SpeakerDecision.blocked("speaker_rotation_wait")
    selected = alternate or candidates[0]
    return reserve_for_action(turn, action=action, account_id=selected.id)
```

真实实现必须使用 `INSERT ... ON CONFLICT` 后的 state 行锁或等价 `SELECT ... FOR UPDATE`，而不是进程内缓存。预约前必须按最新持久化 `ConversationSpeakerTurn` 判定“最后平台消息之后是否已有真人消息”；仅真人事件清除同账号相邻限制。候选列表由现有账号池、目标成员关系、容量、账号在线状态和 task scope 共同得出；带覆盖账本的 Action 不得改绑。锁顺序固定为现有目标锁 -> Action 锁 -> 会话 state 锁，Planner 不持久化预约，避免与 target lifecycle 和 Dispatcher 的现有锁顺序相反。

- [ ] **Step 4: 实现结果回写语义。**

`success` 与 `unknown_after_send` 都写入平台 `ConversationSpeakerTurn` 并固化最近账号；在 Gateway 前明确失败、AI 生成失败、质量拒绝或内容为空时仅释放当前 Action 的预约；任何非当前 Action 不得清空别人的预约。监听器看到真人消息时追加 `sender_kind=human`，并在同一短事务推进 state 的 `last_human_cursor`；群管控制和系统事件绝不能推进该字段。重复 finalize 必须幂等，以 `reserved_action_id` 和 `version` 为依据。

- [ ] **Step 5: 并发与 payload 测试。**

Run（由测试执行器强制 60 秒上限）: `backend/.venv/bin/python -m pytest -q backend/tests/test_conversation_speaker_rotation.py backend/tests/test_action_payloads.py`

Expected: PASS；两个并发 Action 不会都预约到同一账号，运行时换账号不会丢失或伪造账号面具与 dedupe 身份。

### Task 4: 将群管理机器人的必需频道要求前置为 AI 活群账号准入

**Files:**

- Create: `backend/app/models/group_bot_admission.py`
- Modify: `backend/app/models/__init__.py`
- Create: `backend/migrations/versions/<next>_group_bot_admission.py`
- Create: `backend/app/services/task_center/group_bot_admission.py`
- Modify: `backend/app/api/routers/operations.py`
- Modify: `backend/app/services/operations.py`
- Modify: `backend/app/services/required_channel_prompts.py`
- Modify: `backend/app/services/group_listener_context_writer.py`
- Modify: `backend/app/services/group_listeners.py`
- Modify: `backend/app/services/task_center/channel_membership.py`
- Modify: `backend/app/services/task_center/dispatcher.py`
- Modify: `backend/app/services/task_center/membership_admission.py`
- Modify: `backend/app/services/task_center/payloads.py`
- Modify: `backend/app/services/task_center/precheck.py`
- Test: `backend/tests/test_group_bot_admission.py`
- Test: `backend/tests/test_required_channel_prompt_admission.py`
- Test: `backend/tests/test_task_center_dispatcher_target_permission.py`
- Test: `backend/tests/test_task_center_membership_items.py`
- Test: `backend/tests/test_operation_targets_view_dataflow.py`

- [ ] **Step 1: 先写“群管机器人拦截前不能发言”的回归测试。**

```python
def test_joined_group_account_waits_for_group_bot_observation_before_any_send(session, gateway):
    admission = record_joined_group_account(session, account_id=11, group_id=7, join_action_id="join-1")
    dispatch_group_ai_action(session, pending_action(account_id=11, group_id=7))
    assert admission.state == "awaiting_group_bot_rule"
    assert gateway.send_calls == []


def test_group_bot_prompt_creates_exact_channel_follow_actions_before_context_filter(session, gateway):
    ingest_group_bot_message(session, group_id=7, message_id="bot-1", text="@clementine 请先关注 @school_news 后发言")
    assert admission_for(session, account_id=11).state == "required_channel_follow_pending"
    assert required_channel_refs_for(session, account_id=11) == ["school_news"]
    assert gateway.send_calls == []


def test_unattributed_group_bot_prompt_never_blocks_or_follows_for_every_group_account(session):
    create_two_waiting_admissions(session, group_id=7, account_ids=[11, 12])
    ingest_group_bot_message(session, group_id=7, message_id="bot-ambiguous", text="新人请先关注 @school_news 才能发言")
    assert admissions_for(session, group_id=7).states == {"group_bot_rule_unattributed"}
    assert follow_actions_for(session, group_id=7) == []


def test_all_required_channels_and_trusted_confirmation_are_needed_before_ready(session, gateway):
    resolve_prompt_for(session, account_id=11, refs=["school_news", "school_notice"], requires_confirmation=True)
    complete_channel_follow(session, account_id=11, ref="school_news")
    assert admission_for(session, account_id=11).state != "group_bot_admission_ready"
    complete_channel_follow(session, account_id=11, ref="school_notice")
    record_trusted_bot_confirmation(session, account_id=11, group_id=7)
    assert admission_for(session, account_id=11).state == "group_bot_admission_ready"
    assert gateway.send_calls == []


def test_follow_sufficient_requires_audited_target_and_bot_peer_policy(session):
    admission = awaiting_bot_confirmation(session, account_id=11, group_id=7)
    complete_all_required_channel_follows(session, admission)
    assert admission.state == "awaiting_group_bot_confirmation"
    configure_follow_sufficient(session, group_id=7, bot_peer_id=900, audited_by="operator")
    reconcile_admission(session, admission)
    assert admission.state == "group_bot_admission_ready"


def test_probe_ok_cannot_be_used_as_group_bot_admission_ready(session):
    admission = awaiting_bot_confirmation(session, account_id=11, group_id=7)
    record_probe_ok(session, account_id=11, group_id=7)
    assert admission.state == "awaiting_group_bot_confirmation"
```

- [ ] **Step 2: 建立不依赖 Task ID 的可审计准入状态。**

新增 `GroupBotAdmissionPolicy` 和 `GroupBotAdmission`。前者以 `(tenant_id, group_id, trusted_bot_peer_id, completion_policy)` 的当前生效版本为身份，记录证据引用、理由、`policy_version`、生效/撤销操作者与时间；`follow_sufficient` 必须绑定已观察到的可信机器人 peer，`not_required` 必须引用连续控制观察，策略写入/撤销使用 `targets.manage + expected_policy_version`。迁移用 partial unique index（或等价原子约束）保证每群一个 active `not_required`、每群+机器人 peer 一个 active `follow_sufficient`，不能先查后写。后者以 `(tenant_id, group_id, account_id, membership_action_id)` 作为幂等身份，记录：入群完成时间、`join_start_cursor` / `observed_end_cursor`、状态、引用的协议与版本、可信群管机器人 peer、群管提示消息 ID 与摘要、精确 `required_channel_refs`、子关注 Action ID、确认结果、Telegram 传输层观测、首条正文可见性结果、失败码与时间。状态至少覆盖 `awaiting_group_bot_rule`、`group_bot_policy_unresolved`、`group_bot_rule_clear`、`required_channel_follow_pending`、`following_required_channel`、`awaiting_group_bot_confirmation`、`group_bot_admission_ready`、`group_bot_rule_unattributed`、`post_send_intercepted`、`blocked`。

账号入群成功时在同一业务事务创建该记录，但**不修改**对应 `TgGroupAccount.can_send`；该字段继续保存 Telegram 权限事实。所有 AI 活群候选池和发送门同时读取 Telegram 权限与准入状态，而不是把两者合并成一个布尔值。`GroupBotAdmission` 变为 ready 不写回 `can_send`；Telegram 权限变化也不伪造群管机器人已放行。不复用 `TaskMembershipAdmissionItem` 承载它，因为后者按任务快照建模，无法表达同一账号跨多个 AI 活群任务共享的一次入群准入事实。

- [ ] **Step 3: 在入群后监听群管控制消息，并做严格账号归属。**

`ensure_target_membership` 开始前记录 `join_start_cursor`，成功后触发该群严格 `> join_start_cursor` 的增量监听。`GroupMessageSnapshot` / 控制事件必须持久化远端消息 ID、发送者 peer/管理员角色、回复关系、服务事件、内联按钮文本与 URL/callback 标识和顺序游标；仅取最新 N 条历史无法作为观察完成依据。`group_listener_context_writer` 要新增独立的群管控制事件入口：先识别 `is_bot` / 系统管理消息和必需频道规则，再决定是否写普通 `GroupContextMessage`；不能因 `ignored_sender`、普通上下文重复、学习过滤或内容为空而跳过控制处理。

归属服务按“明确账号标识 -> 回复/入群事件关联 -> 同窗口唯一等待账号”的顺序匹配；窗口内多账号候选、频道引用缺失、消息不是可信群管来源或远端游标无法证明在入群之后时，写明 `group_bot_rule_unattributed` / `group_bot_rule_unparseable`，不做全局兜底。新 `group_ai_chat` 准入不得再调用当前 `allow_global=True` 的全群账号匹配分支。群管控制文本只保存在准入审计字段，不能进入 `GroupContextMessage` 的 AI 可用检索、学习样本或引用池。

- [ ] **Step 4: 将“关注频道 -> 确认 -> 复检”拆成持久化子动作。**

从已归属的群管提示解析出的每个真实 `@username`、`t.me/...` 或私有邀请引用，各创建一个带 `group_bot_admission_id`、`required_channel_ref`、来源消息 ID 的 `group_bot_required_channel_follow` Action，并按引用+账号幂等。Gateway 在执行前必须解析并确认目标是提示所指的广播频道；非频道、跳转后实体变化、无法解析或不在原始引用集合内都明确失败。将现有 `_recover_group_send_permission_with_linked_channel()` 在新 `group_ai_chat` 准入路径改为“记录并委派子动作”，不再在 membership 或普通发言 Action 内循环直接调用 `gateway.ensure_channel_membership()`；关注失败、引用失效或需要人工确认要停在对应子动作，主群发言 Action 不得被重新排入。

所有子动作成功后，执行群管要求的确认按钮（如提示明确包含该确认动作），并等待同一可信群管机器人给出明确完成事件；仅目标级、同机器人 peer、版本已审计的 `follow_sufficient` 可以替代该完成事件。无正文 `probe_target_capabilities()` 可以记录 Telegram 传输层状态并发现真实 Telegram 权限变化，但不能把 `probe.ok` 写成 `group_bot_admission_ready`。首次控制观察已经从入群游标连续追平、且已有目标级 `not_required` 审计策略时才可写 `group_bot_rule_clear`；不能用等待时长代替证据。策略变更、撤销、可信机器人 peer 变化或游标缺口时，未进入 Gateway 的准入和 action 必须回到 `group_bot_policy_unresolved` 重新核验。

- [ ] **Step 5: 将历史“发言后补救”降级为隔离修复，堵住正常发言路径。**

删除新 `group_ai_chat` 正常发送链路对 `_recover_send_message_required_channel()` 的依赖；Dispatcher 在 AI 生成前和 Gateway 前都校验 `GroupBotAdmission` 的当前版本、状态和复检时间。不是 `group_bot_admission_ready` 时，Action 写 `pending / group_bot_admission_wait`，不创建 `ExecutionAttempt`、不调用 AI、也不调用 `send_message`。

保留原先的发言后识别仅用于已上线旧 Action、迁移时数据缺口或外部状态突变：记录 `legacy_group_bot_intercepted`，将账号拉回新的准入状态机并停止该 Action；不得在同一个真实消息之后“自动关注并重发”。首次完成准入后的正文和机器人规则版本变化后的首条正文都必须无正文确认远端可见；若被可信机器人删除或拒绝，写 `post_send_intercepted`、撤回 ready、停止后续 action，不自动重发。`group_membership_admission` 的测试消息也必须排在群管准入完成之后，不能作为发现频道要求的手段。

- [ ] **Step 6: 运行准入定向回归。**

Run（由测试执行器强制 60 秒上限）: `backend/.venv/bin/python -m pytest -q backend/tests/test_group_bot_admission.py backend/tests/test_required_channel_prompt_admission.py backend/tests/test_task_center_dispatcher_target_permission.py backend/tests/test_task_center_membership_items.py`

Expected: PASS；新入群账号在控制观察完成、可信机器人放行或审计协议满足前没有任何正常文本发送；`probe.ok` 不会推进群管 ready；提示只影响精确账号；多频道、确认、失败、歧义、发送后拦截和历史补救均有可追溯状态。

### Task 5: 让群聊与频道评论 Planner 真正产出轮换和引用动作

**Files:**

- Modify: `backend/app/services/task_center/executors/group_ai_chat.py`
- Modify: `backend/app/services/task_center/executors/channel_comment.py`
- Modify: `backend/app/services/task_center/executors/channel_comment_targets.py`
- Modify: `backend/app/services/task_center/precheck.py`
- Modify: `backend/app/services/task_center/ai_generation_dispatch.py`
- Modify: `backend/app/services/task_center/comment_generation_dispatch.py`
- Test: `backend/tests/test_group_ai_chat_dataflow.py`
- Test: `backend/tests/test_group_ai_send_limits.py`
- Test: `backend/tests/test_channel_comment_dataflow.py`
- Test: `backend/tests/test_channel_comment_planner_boundaries.py`

- [ ] **Step 1: 写 Planner 级失败测试。**

```python
def test_group_plan_never_places_same_account_in_adjacent_slots_when_two_are_ready(session):
    actions = plan_group_round(session, task_with_accounts(101, 102), message_count=4)
    assert [item.account_id for item in actions] == [101, 102, 101, 102]


def test_channel_plan_interleaves_reply_slots_when_comments_are_available(session):
    actions = plan_channel_comments(session, mixed_comment_task(reply_min_per_message=1))
    assert any(action.payload["comment_mode"] == "reply" for action in actions)
    assert all(action.payload["reply_to_message_id"] for action in actions if action.payload["comment_mode"] == "reply")


def test_reply_shortfall_records_reason_but_does_not_forge_a_reply(session):
    actions = plan_channel_comments(session, mixed_comment_task(reply_min_per_message=1, no_reply_targets=True))
    assert all(action.payload["comment_mode"] == "comment" for action in actions)
    assert task.stats["reply_target_shortfall_count"] == 1
```

- [ ] **Step 2: 群聊 Planner 改为“轮换优先 + 引用优先”。**

删除 burst 分支后，`_select_cycle_accounts()`、`_generation_slots_for_plan()` 和 `_prepare_action_slots()` 必须：

1. 先只保留 `GroupBotAdmission=group_bot_admission_ready` 且准入版本未过期的群聊账号，再读取会话状态作为账号排序依据，先选择非最近发言账号；
2. 同一轮候选足够时一号一条交错，不为提高小时目标而重复同一账号；
3. 用现有 `_group_reply_target_pool()` 选择引用目标，优先真人上下文，写入完整 reply metadata；
4. 回复候选不足时写 `reply_target_shortfall_count`、可用数和短缺数，继续创建不伪造的普通 slot；
5. 为每个 slot 写稳定 `conversation_slot_key`、会话键和内容意图，供 Dispatcher 再确认。

不得继续使用只对 `hard_hourly` 生效的 `HARD_HOURLY_MAX_CONSECUTIVE_ACCOUNT_RUN=2` 作为普通群聊的真人化保证；新规则对每个群聊 Action 都有效。

- [ ] **Step 3: 评论 Planner 改为同一会话的轮换和混合引用。**

`_comment_slot_targets()` 不再简单取 `pool[:required]` 后把其余全部设为普通评论。它应按频道消息、讨论区新鲜度、作者、是否已被回复、目标与当前账号是否相同排序，交错产生 reply/direct slot；每一 slot 先带 `conversation_key`，再由 shared rotation service 确认实际发言账号。

普通 `mixed` task 的引用候选不足只产生显式短缺统计和直接评论 slot；显式 `comment_mode=reply` 的单条快捷回复仍保持强失败，不得降级。所有 reply slot 必须经过已有 `_validate_reply_target()`，在目标丢失或过期时不改发普通评论。

- [ ] **Step 4: 预检、默认值和 Action 数据流一起验证。**

预检除“可引用数量”外新增“可参与轮换账号数、上一位发言账号、若仅一号可发则会等待”的说明。对 AI 活群额外返回 `group_bot_admission_ready_count`、等待/阻塞账号数和具体原因；未 ready 的账号不能计入可参与轮换账号数。创建、编辑、预检和最终 payload 都使用相同默认值；不允许前端显示已启用引用、后端却收到 `reply_min=0` 或 `comment_mode=comment`。

- [ ] **Step 5: 运行 Planner 回归。**

Run（由测试执行器强制 60 秒上限）: `backend/.venv/bin/python -m pytest -q backend/tests/test_group_ai_chat_dataflow.py backend/tests/test_group_ai_send_limits.py backend/tests/test_channel_comment_dataflow.py backend/tests/test_channel_comment_planner_boundaries.py`

Expected: PASS；相邻 slot、跨轮排序、reply payload 和短缺处理全部有覆盖。

### Task 6: 引入共享真人化质量门，并把 `签到` 做成唯一可审计兜底

**Files:**

- Create: `backend/app/services/task_center/conversation_content_quality.py`
- Modify: `backend/app/services/task_center/ai_generator.py`
- Modify: `backend/app/services/task_center/ai_generation_pipeline.py`
- Modify: `backend/app/services/task_center/ai_generation_dispatch.py`
- Modify: `backend/app/services/task_center/comment_generation_quality.py`
- Modify: `backend/app/services/task_center/comment_generation_dispatch.py`
- Modify: `backend/app/services/content_filters.py`
- Test: `backend/tests/test_conversation_content_quality.py`
- Test: `backend/tests/test_ai_generation_pipeline.py`
- Test: `backend/tests/test_channel_comment_generation_phases.py`

- [ ] **Step 1: 写出质量门和签到兜底的失败测试。**

```python
def test_quality_rejects_template_shell_and_repeated_opening():
    decision = evaluate_conversation_content(
        content="这个点挺有意思，可以继续聊聊",
        history=["这个点我也留意到了"],
        intent="reaction",
    )
    assert decision.allowed is False
    assert decision.code in {"template_shell", "repeated_opening"}


def test_direct_generation_failure_uses_audited_check_in_not_old_generic_template():
    resolved = resolve_content_fallback(action=direct_action, prior_contents=["正常内容"])
    assert resolved.content == "签到"
    assert resolved.content_source == "check_in_fallback"


def test_reply_generation_failure_never_degrades_to_unlinked_check_in():
    assert resolve_content_fallback(action=reply_action, prior_contents=[]).allowed is False
```

- [ ] **Step 2: 实现一个可复用、无静默重写的质量决策。**

`evaluate_conversation_content()` 的输入必须包含：场景、会话键、正文、内容意图、引用目标、最新已发送/已预约正文、账号面具和允许的上下文锚点。至少返回以下可审计拒绝码：`template_shell`、`repeated_opening`、`semantic_duplicate`、`missing_context_anchor`、`reply_target_mismatch`、`voice_profile_mismatch`、`check_in_repeat`。

保留 `content_filters.filter_outbound_content()` 作为最终共用安全过滤；新质量门既用于生成结果入库，也用于发送前复核，防止旧 pending Action 绕过新规则。质量门返回的是“允许/拒绝 + 原因 + 审计”，不把原句偷偷替换成另一句。

- [ ] **Step 3: 重新组织生成提示词和候选结构。**

群聊、群引用、评论、评论引用 prompt 不再只靠长禁止词清单；每个 slot 显式传入一个单一互动意图、账号口吻、已说内容、允许锚点和 reply target。输出仍保持现有 JSON 契约，但必须回传对应 `slot_id` / `sequence_index`，让引用目标和账号面具不会串槽。

现有的模板词、语义去重、事实锚点、频道规则快照继续使用；新增判定只补足“像 AI”的可测维度，不删除安全、敏感内容或租户禁词过滤。

- [ ] **Step 4: 用统一兜底替换旧泛化 fallback。**

将 `_fallback_contents()` 中的群聊泛化句和其它非长任务 fallback 改为调用 `resolve_content_fallback()`。只有直接 Action 且没有相邻 `签到` 时返回精确 `签到`；否则返回明确拒绝，由原有任务状态机延期/失败。每次采用都更新 payload、Action result、质量统计和详情展示。

- [ ] **Step 5: 运行生成与质量测试。**

Run（由测试执行器强制 60 秒上限）: `backend/.venv/bin/python -m pytest -q backend/tests/test_conversation_content_quality.py backend/tests/test_ai_generation_pipeline.py backend/tests/test_channel_comment_generation_phases.py`

Expected: PASS；旧泛化兜底不再可发送，reply 失败不丢失 reply 语义，质量拒绝均可追溯。

### Task 7: 在 Dispatcher 中把群管准入门、轮换预约、账号重绑和发送结果形成闭环

**Files:**

- Modify: `backend/app/services/task_center/dispatcher.py`
- Modify: `backend/app/services/task_center/ai_generation_dispatch.py`
- Modify: `backend/app/services/task_center/comment_generation_dispatch.py`
- Modify: `backend/app/services/task_center/account_voice_profiles.py`
- Test: `backend/tests/test_task_center_capacity_dispatch.py`
- Test: `backend/tests/test_channel_comment_generation_postgres.py`
- Test: `backend/tests/test_group_ai_dispatch_generation.py`
- Test: `backend/tests/test_group_bot_admission.py`

- [ ] **Step 1: 写发送前并发回归测试。**

```python
def test_dispatcher_rebinds_to_alternate_before_ai_generation(session, gateway):
    mark_last_speaker(session, group_id=7, account_id=101)
    action = pending_group_action(account_id=101, generation_pending=True)
    dispatch_action(session, action)
    assert action.account_id == 102
    assert action.payload["speaker_selection_reason"] == "runtime_rotated"
    assert generated_for_account(gateway, action.id) == 102


def test_dispatcher_does_not_call_gateway_when_rotation_has_no_alternate(session, gateway):
    mark_last_speaker(session, group_id=7, account_id=101)
    action = pending_group_action(account_id=101)
    dispatch_action(session, action)
    assert action.status == "pending"
    assert action.result["error_code"] == "speaker_rotation_wait"
    assert gateway.send_calls == []


def test_dispatcher_never_generates_or_sends_for_group_bot_admission_wait(session, gateway):
    set_group_bot_admission(session, group_id=7, account_id=101, state="awaiting_group_bot_rule")
    action = pending_group_action(account_id=101)
    dispatch_action(session, action)
    assert action.result["error_code"] == "group_bot_admission_wait"
    assert gateway.ai_generation_calls == []
    assert gateway.send_calls == []


def test_post_send_group_bot_interception_stops_following_actions_without_retry(session, gateway):
    action = sent_first_admission_message(session, account_id=101, group_id=7)
    record_trusted_bot_interception(session, action_id=action.id)
    assert action.result["error_code"] == "post_send_intercepted"
    assert admission_for(session, account_id=101).state != "group_bot_admission_ready"
    assert pending_actions_for(session, account_id=101, group_id=7) == []
    assert gateway.retry_calls == []
```

- [ ] **Step 2: 增加发送前两阶段预约，不在模型调用期间持锁。**

在群聊路径中，先锁定并校验 `GroupBotAdmission`（状态、准入版本、协议和可见性状态）以及 payload/目标/成员资格的无内容校验；评论链路不读取群管准入，但同样先完成目标/成员资格校验。两条链路都必须在 AI 调用前通过共享会话轮换服务预约账号，并在 Gateway 前再次校验预约仍归属该 Action。群聊准入未 ready 时立即写 `group_bot_admission_wait`，不进入 AI 生成。如选择发生变化，先重绑 `Action.account_id`、账号面具、账号记忆和待生成内容状态，再调用 AI。AI 调用结束后，Gateway 前再次锁定群聊准入（仅群聊）、目标、Action 和会话 state，确认所有门禁与预约仍属于该 Action，然后才创建 `ExecutionAttempt` 和标记 `gateway_call_started`。

运行时账号重绑若遇到 `coverage_ledger_id`、固定 reply 上下文、账号无法进入目标或没有候选，必须明确延期/跳过，不得以旧账号内容配新账号发送。任何重绑导致已有 `message_text`/`comment_text` 的 Action 都必须废弃该内容并按新账号重新生成，不能借用旧面具文本。

- [ ] **Step 3: 让 success、known failure 和 unknown 正确回写。**

在 `_apply_send_result()`、群聊 `_finalize_group_send()`、评论 finalize 与 `_mark_unknown_after_send()` 中调用同一 `finalize_speaker_turn()`；已进入 Gateway 但未知结果保留预约，已知失败释放预约，成功写最近账号、远端消息 ID 和正文来源。群聊首次通过准入后的正文与规则版本变化后的首条正文先进入待可见性核验：可信机器人拦截时写 `post_send_intercepted`、撤回准入 ready、停止后续 action、覆盖账本不计完成；未知时仍保留 `unknown_after_send`，不自动重发。群管准入在 Gateway 前降级时，尚未进入 Gateway 的 coverage reservation 必须释放为 `pending_group_bot_admission`，已进入 Gateway/未知发送绝不被该状态覆盖。回写失败必须让事务失败并暴露错误，不能写“已发送”后吞掉会话状态错误。

- [ ] **Step 4: 保留现有目标生命周期与引用校验。**

轮换服务和群管准入服务都不能越过 `_lock_outbound_target()`、`evaluate_outbound_target_gate()`、成员资格、`_validate_reply_target()`、账号容量、群冷却/活动窗和内容过滤。目标已解散、引用无效、回复对象过期、群管频道关注失败时按各自终态/等待语义处理，不能因为要“恢复任务”而重绑到其他目标、猜测频道或发普通正文。

- [ ] **Step 5: 运行 Dispatcher 回归。**

Run（由测试执行器强制 60 秒上限）: `backend/.venv/bin/python -m pytest -q backend/tests/test_task_center_capacity_dispatch.py backend/tests/test_channel_comment_generation_postgres.py backend/tests/test_group_ai_dispatch_generation.py backend/tests/test_group_bot_admission.py`

Expected: PASS；群管准入阻断、并发、未知发送、账号重绑、回复校验和真实 Gateway 参数均在测试中可见。

### Task 8: 补齐任务中心可观测性、迁移和发布验证

**Files:**

- Modify: `frontend/src/app/views/TaskCenterDetailModal.tsx`
- Modify: `frontend/src/app/views/TaskCenterWizardSections.tsx`
- Modify: `frontend/src/app/views/TaskCenterView.tsx`
- Modify: `frontend/src/app/views/OperationTargetsView.tsx`
- Modify: `frontend/src/app/views/taskCenterViewModel.ts`
- Modify: `backend/app/services/task_center/service.py`
- Modify: `backend/app/services/task_center/precheck.py`
- Modify: `backend/app/services/task_center/group_bot_admission.py`
- Create: `backend/scripts/reconcile_humanized_interaction_policy.py`
- Modify: `docs/04-ops/deployment/PRODUCTION_RUNTIME.md`
- Test: `frontend` task-center tests/build
- Test: `backend/tests/test_task_center_precheck.py`
- Test: `backend/tests/test_group_bot_admission.py`
- Test: `backend/tests/test_operation_targets_view_dataflow.py`

- [ ] **Step 1: 前端展示真实运行状态。**

创建/编辑页只展示不能绕开的“账号轮换（强制）”“群管频道准入（强制）”说明、可参与轮换账号数和引用候选摘要。详情和 Action 明细展示：`普通发言/引用回复/普通评论/回复评论/签到兜底`、上一位账号、轮换选择原因、等待原因、reply target 作者与预览、质量拒绝码、兜底原因。

AI 活群账号明细额外展示：`awaiting_group_bot_rule / group_bot_policy_unresolved / required_channel_follow_pending / following_required_channel / awaiting_group_bot_confirmation / group_bot_rule_clear / group_bot_admission_ready / post_send_intercepted / blocked`、协议版本、入群动作、观察游标范围、可信群管机器人 peer、来源消息、精确频道引用、每个关注子动作、确认结果、Telegram 传输层观测和首条正文可见性结果。目标详情还需由具备 `targets.manage` 的人员展示/维护 `not_required` 或 `follow_sufficient` 策略的证据、版本和撤销记录；不允许在任务创建页用一个通用开关设置。提示正文仅向有该任务权限的运营人员展示，普通列表只显示脱敏摘要。缺字段显示 `-`，不得根据文案猜测为引用回复或“已关注”。

- [ ] **Step 2: 添加任务统计与告警信号。**

至少记录并返回：`speaker_rotation_wait_count`、`speaker_rotation_runtime_rebind_count`、`speaker_rotation_unavoidable_count`（正常路径应为 0）、`reply_planned_count`、`reply_success_count`、`reply_target_shortfall_count`、`check_in_fallback_count`、`content_quality_rejection_counts`、`last_conversation_speaker_account_id`、`last_conversation_content_source`，以及 `group_bot_admission_wait_count`、`group_bot_policy_unresolved_count`、`group_bot_required_channel_prompt_count`、`group_bot_required_channel_followed_count`、`group_bot_unattributed_count`、`group_bot_admission_blocked_count`、`post_send_intercepted_count`、`legacy_group_bot_intercepted_count`。任何 `speaker_rotation_unavoidable_count > 0`、`post_send_intercepted_count > 0` 或新入群账号的 `legacy_group_bot_intercepted_count > 0` 都视为缺陷告警，不是可接受吞吐优化。

- [ ] **Step 3: 写一次可重跑的迁移脚本，先 inventory 后 apply。**

脚本必须支持 `--tenant-id <id> --dry-run` 和显式 `--apply`：

1. 清理 active `group_ai_chat` task `type_config` 中的旧连发键和 `auto_follow_required_channel=false`；写入强制的 `group_bot_admission_required=true`，但不改动其它任务类型的配置；
2. 将仍为旧默认的群聊 `reply_min_per_round=0`、频道 `comment_mode=comment/reply_min_per_message=0` 转为本 PRD 的自动引用默认，并输出将受影响的 task 数；
3. 为已经入群且被 AI 活群 task 引用、但没有 `GroupBotAdmission` 的账号创建“存量准入复核”记录：保留原 `TgGroupAccount.can_send`，从现有监听水位建立可见控制观察与目标协议复核；未完成复核的账号仅不计入新策略 ready 容量，绝不凭旧 `can_send=True` 直接视为已通过新规则；
4. 只跳过/重排未进入 Gateway 的旧 burst、旧互动策略或尚未通过群管准入的 Action；`executing`、`success`、`failed`、`unknown_after_send` 保留原始事实。历史已被群管机器人拦截的 Action 仅标记 `legacy_group_bot_intercepted`，不自动重发；
5. 不按群名匹配、不修改目标 lifecycle、不修改 `qdsfxy` 等既有引用失效/解散处置；
6. 输出 action/task/会话 state/群管准入记录的精确数量和 reason，支持重复运行不产生二次副作用。

- [ ] **Step 4: 运行全量质量闸门。**

Run（由测试执行器对每项后端测试强制 60 秒上限）:

```bash
backend/.venv/bin/python -m pytest -q \
  backend/tests/test_conversation_speaker_rotation.py \
	  backend/tests/test_conversation_content_quality.py \
	  backend/tests/test_group_bot_admission.py \
	  backend/tests/test_required_channel_prompt_admission.py \
	  backend/tests/test_group_ai_chat_dataflow.py \
  backend/tests/test_channel_comment_dataflow.py \
  backend/tests/test_task_center_capacity_dispatch.py
cd frontend && npm run build
git diff --check
```

Expected: 所有定向后端测试和前端 build 通过，`git diff --check` 无空白错误。若本地测试环境无法连接项目 PostgreSQL 测试库，必须把未运行项标为 `blocked`，不能用单元脚本结果伪装为全量通过。

- [ ] **Step 5: 发布与真实环境验收。**

按 `master -> release -> GitHub Actions Deploy Production` 发布。先运行 dry-run inventory，确认无旧 burst Action 被误改；再选一个有群管理机器人“关注频道后可发言”规则的群，以一个新加入的测试账号验证准入链路；另选一个至少两个可用账号、已有可引用消息的群，以及一个讨论区有可回复评论的频道进行小范围验证。

生产证据需分层记录：

| 证据 | 状态含义 |
| --- | --- |
| Action payload、ExecutionAttempt、Gateway 远端 message id、任务详情一致 | `pass` 的必要条件。 |
| 新账号入群记录、连续控制游标、群管机器人原始消息 ID、精确频道关注 Action、可信机器人确认或目标级审计协议、Telegram 传输层观测与首条正文可见性记录的因果链一致 | 群管准入 `pass` 的必要条件；中间不得出现普通文本发送，`probe.ok` 不能替代机器人放行证据。 |
| 没有第二账号或没有回复候选，Action 显示明确等待/短缺 | `blocked`，不是成功。 |
| 群管提示归属不明、监听游标没有连续追平、协议未配置、频道无效、关注/确认失败或正文被机器人拦截 | `blocked`，账号不能进入/继续留在可发言池。 |
| 仅本地测试、CI 或部署完成，未核对真实 Telegram 结果 | `unproven`，不能写生产已修复。 |

验收至少取连续 20 条群聊和 20 条频道评论的实际序列：无真人消息间隔时相邻账号不重复；有候选批次有真实引用；所有 `签到` 都带 fallback 元数据且没有连续 `签到`；质量拒绝原因和页面统计可回溯。群管机器人样本必须另行证明：账号加入后第一条由系统产生的动作是控制监听/频道准入，而非普通群消息；所有要求频道均完成，首条正常群消息发生在 `group_bot_admission_ready` 之后，并且首条可见性结果可追溯。目标经独立生命周期证据确认解散时，展示“群里已被解散，已跳过本目标”；仅用户名解析失败的样本不得计入该验收。

## 4. 非目标与回滚边界

- 不把账号轮换扩展到浏览、点赞、转发、人工发送或非本次两类互动任务。
- 不把群管机器人提示混入 AI 学习、上下文回复、频道评论或全群账号的泛化规则；它只驱动精确账号的 AI 活群准入。
- 不因内容真人化而绕过目标生命周期、账号容量、群策略、敏感内容过滤、规则快照或 Telegram 最终校验。
- 不把“没有替代账号”伪装成轮换成功；吞吐降低必须以明确 blocker 呈现。
- 回滚只停止新的轮换/质量策略和前端展示，不删除 `ConversationSpeakerTurn`、`GroupBotAdmission`、Action result、ExecutionAttempt、质量拒绝和 `签到` 审计事实；已发送消息永不重写或撤回。群管准入不回滚为“先发一条试试”。

## 5. 方案自检

- [x] 六条原始诉求均有产品口径、后端落点、前端可见性和验收项。
- [x] 覆盖了入群后的群管控制游标、可信来源、精确账号归属、频道关注、完成协议、Telegram 传输层观测、发送后可见性、Planner、Dispatcher、AI 调用、Gateway、并发、`unknown_after_send`、覆盖账本和旧 Action 迁移。
- [x] 没有引入 silent fallback；`签到`、短缺、轮换等待、群管准入等待、歧义提示和质量拒绝均有可见原因。
- [x] 2026-07-25 评审修补：观察闭合、not_required 闭环、完成事件识别器、可见性 SLA、membership 相位、单账号容量、签到配额、C0–C3 canary、跨专项 supersede、与 continuity 终态/unknown/credit 对齐；验收负例写入专项 PRD §12.2。
- [x] continuity 交叉 P0/P1：§5.8.1–§5.8.3 占位/credit/abandoned；§5.1.1 admission_version；§8.3 ClaimClass；§10.2.1–§10.2.2 canary action 边界；continuity PRD 同步增量注。
- [x] 没有把目标生命周期、群管控制消息或历史发送事实纳入本次无关重写；历史被拦截不自动重发。

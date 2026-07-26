# AI 活群群管机器人准入与成功事实修复 Implementation Plan

> **For Codex:** Required skill: use `executing-plans` to implement this plan task-by-task.

**Goal:** 修复 AI 活群的四条已证实故障链路：成功发送仍展示“需关注频道”的陈旧错误、入群观察没有可验证游标、普通 bot 消息在信任校验前污染并发等待账号、以及真实群管协议仅存在于内联频道/确认按钮但监听未采集。上线后任务只依据真实 Telegram 成功事实计完成，群管规则没有证据时显式进入待策略/观察失效，不再伪装成“关注频道”。

**Architecture:** 发送终态在 Dispatcher 写入 `success + remote_message_id` 时清理一次性准入错误；Task Center 读模型和前端也以终态成功优先，兼容历史脏结果而不篡改审计原文。群监听每次成功拉取后先对控制消息做来源信任，再做账号归属；`GroupMessageSnapshot` / `GroupContextMessage` 保存没有 callback bytes 的按钮摘要，以原消息 ID、bot peer、行列、文案和类型创建精确 follow/callback action。入群前以群行锁取得 admission window，禁止多个账号同时 join 后争夺同一条 bot 提示；已有窗口的 action 在 Gateway 前显式 defer。入群前仍由既有 listener 读取游标；无基线进入 `observation_stale`，存量只能带版本/审计 restart 后重新观察。

**Tech Stack:** Python 3 / FastAPI / SQLAlchemy / pytest；React / TypeScript；PostgreSQL 生产环境；GitHub Actions Deploy Production。

## Scope and non-goals

- 覆盖 `group_ai_chat` 的群管机器人准入与 Task Center 发送结果展示。
- 不自动创建 `not_required`，不以 `can_send=true`、等待时间、探测成功或历史成功发送伪造群管放行。
- 不改写历史 `Action.result` 和 `ExecutionAttempt`；历史展示通过读取优先级修正。
- 不放宽账号容量、重复内容、质量、Telegram 风控或全账号日覆盖分母。它们仍作为独立业务阻塞出现在最终线上验收中。

## Product contract

1. 一条 `send_message` 只有 `Action.status=success`、成功 `ExecutionAttempt` 和非空 `remote_message_id` 才是远端成功事实；成功结果不得继续携带会被 UI/读模型识别为当前失败的临时准入错误。
2. 新 join/rejoin 前必须由已有可用 listener 账号读取目标群的最大可读远端游标。无可读基线的 admission 写 `observation_stale/join_start_cursor_missing`，不得默认从零开始。
3. 每一次 listener 拉取都必须为该群观察中的 admission 写一条 `GroupBotAdmissionObservation`。只有观察批次可证明返回窗口覆盖 join 基线且无读取失败，窗口才可以闭合。
4. 控制 bot 规则先处理，观察窗口后闭合。无可信规则且有 active `not_required` 才 ready；无策略只到 `group_bot_policy_unresolved`。
5. 已上线但无游标的 admission 只能通过 `targets.manage` 的显式“重启观察”从当前上下文水位重新开始；请求含 expected version、reason、evidence，API 记录审计。无可读水位返回显式错误。
6. 非可信 bot 消息在来源过滤阶段即停止，不能读取/修改 waiting admissions；unknown role 只有目标级 explicit/follow policy 绑定的 peer 才能作为受限可信来源。
7. 内联按钮的 URL/callback 是协议事实：频道 follow 仅使用同一消息的精确广播频道 URL；确认 click 必须重读同一 source message 并逐项校验 peer、坐标、文本和 callback 类型，click 本身不 ready。
8. 每群同一时刻只有一个 new admission window；第二个 membership action 必须在 Gateway 前写 `group_bot_admission_window_busy` 并等待当前 admission 收口，不能发送试探正文或批量重置历史状态。

## File plan

### Task 1: 更新 PRD、总 PRD 与索引

**Files:**
- Modify: `docs/03-feature-designs/ai-conversation-humanization-and-group-bot-admission-prd.md`
- Modify: `docs/03-feature-designs/ai-group-daily-fulfillment-remediation-prd.md`
- Modify: `docs/01-product/tg-ops-platform-prd.md`
- Modify: `docs/00-index/project-dataflow-index.md`
- Modify: `docs/00-index/project-structure-index.md`

**Steps:**
1. 添加 2026-07-27 第二次修订记录，明确普通 bot 先污染并发 admission、内联按钮未采集、unknown role 的可信 peer 及 admission 串行窗口。
2. 将任务中心文案约束补为三态：真实成功、已知频道要求、观察/策略未决；成功 action 不显示旧错误字段。
3. 在总 PRD 和日履约 PRD 明确该修复不减少日覆盖分母，也不绕过重复内容、容量或 Telegram 权限。
4. 在数据流和结构索引加入 join baseline -> listener observation -> policy/reconcile -> send gate 的实际入口与回归测试。

### Task 2: 先写失败回归测试

**Files:**
- Create: `backend/tests/test_group_bot_control_buttons.py`
- Modify: `backend/tests/test_group_bot_admission.py`
- Modify: `backend/tests/test_group_bot_follow_actions.py`
- Modify: `backend/tests/test_group_bot_admission_observation_recovery.py`

**Steps:**
1. 证明非可信 bot 在来源过滤阶段不读取、不修改多个 waiting admissions；该用例在旧实现中必须失败为 `group_bot_rule_unattributed`。
2. 证明已审计 policy 同 peer 的 unknown-role bot 能从 URL 按钮提取精确频道，控制消息被持久化为非 AI 审计上下文；无 policy 的同一消息仍无状态写入。
3. 证明确认 action 缺少/篡改 source peer、消息 ID、行列、文本或 callback 类型时不调用 click；完全匹配时 click 成功仍保持 `awaiting_group_bot_confirmation`。
4. 证明同群第二个 membership action 在 Gateway 前 defer，且第一个 admission ready/blocked 后才可继续；证明历史 `group_bot_rule_unattributed` 可单账号 restart 而不形成永久窗口锁。
5. 运行新增用例，确认当前实现至少有一条失败，再开始生产代码。

### Task 3: 实现成功事实与群管观察链路

**Files:**
- Modify: `backend/app/services/task_center/dispatcher.py`
- Modify: `backend/app/services/task_center/group_bot_admission.py`
- Modify: `backend/app/services/group_listeners.py`
- Modify: `backend/app/services/group_listener_context_writer.py`
- Modify: `backend/app/services/group_context_messages.py`
- Modify: `backend/app/integrations/telegram/contracts.py`
- Modify: `backend/app/integrations/telegram/telethon_content.py`
- Modify: `backend/app/integrations/telegram/gateway.py`
- Modify: `backend/app/integrations/telegram/mock.py`
- Modify: `backend/app/models/groups.py`
- Create: `backend/migrations/versions/0125_group_bot_control_buttons.py`
- Modify: `backend/app/services/task_center/payloads.py`

**Steps:**
1. 保持已实现的基线/观察/成功事实合同；在 membership Gateway 调用前新增行锁 admission window 的 reserve/defer/release，未知 Gateway 结果必须保持明确窗口状态。
2. 将 `GroupMessageSnapshot` 扩展为安全按钮摘要，listener 把 bot 控制消息持久化到 `GroupContextMessage.control_buttons`；迁移只加可回滚 JSON 列。
3. 将来源资格判断移动到 `attribute_prompt_to_account` 之前；显式 policy peer 支持 unknown role，但 `explicit_bot_confirmation`/`follow_sufficient` 都必须有 trusted peer，`not_required` 不可授权来源。
4. 从正文和同源 URL 按钮提取精确公共频道，计划 follow action；全部 follow 后计划精确 confirmation action，Gateway 重读并校验 source message 后 click，click 不直接 ready。
5. 将 `group_bot_rule_unattributed` 纳入单账号 restart 恢复范围，不创建批量自动恢复或 `not_required`。

### Task 4: 定向与构建验证

**Files:** modified files above

**Steps:**
1. `timeout 60 backend/.venv/bin/pytest` 运行新增和相关 group bot / Task Center 用例。
2. 运行格式/导入/静态检查（项目已有命令可用时）和 `npm --prefix frontend run build`。
3. 执行 `git diff --check`；确认不含用户已有搜索点击改动，并逐文件审阅本次 diff。

### Task 5: release 发布与真实验收

**Files:** release branch + production only

**Steps:**
1. 先检查上一轮 Deploy Production 失败日志；若与 release 当前代码有关，先修复并重新验证。
2. 只提交本计划涉及文件，push `release`，等待 GitHub Actions Deploy Production 成功并确认生产容器 image SHA。
3. 在线上读取新 API/数据库事实：历史成功 action 不再被投影为准入失败；新 listener poll 写 observation；遗留 admission 使用有审计的 restart observation 重新取基线，必要时由明确 evidence 创建 `not_required` 后 reconcile。
4. 至少证明一条修复后的真实 AI 活群 `ExecutionAttempt.success + remote_message_id`，并读取全账号日覆盖账本。最终将群管准入、内容重复、容量等分别标为 `pass`、`blocked` 或 `unproven`，不以本地测试替代业务达标。

## Verification matrix

| Case | Evidence |
| --- | --- |
| 历史 success 含旧错误字段 | API failure fields 为空；UI 显示消息 ID/成功；原始 result 未改写 |
| 新 join 有 listener 基线 | admission 保存 `join_start_cursor`，观察批次从该水位开始 |
| 缺 listener/基线 | `observation_stale/join_start_cursor_missing`，无 send / 无 ready |
| listener 无 bot + 无 policy | 到期后 `group_bot_policy_unresolved` |
| listener 无 bot + explicit not_required | 仅在有效观察后 ready |
| 非可信 bot prompt | 不改变任一 waiting admission；不会把并发账号批量写 unattributed |
| policy 可信 unknown-role bot | 从同源按钮提取精确频道/确认信息，policy 外 unknown bot 无动作 |
| 按钮确认 | Gateway 重读 source message 校验 peer/坐标/文本/type；click 不直接 ready |
| 并发 membership | 同群第二项在 Gateway 前 `group_bot_admission_window_busy`，第一项收口后才进 join |
| 线上存量无基线 | 显式 restart 审计 + 新窗口，不隐式批量 ready |
| 业务完成 | Action + Attempt + remote ID，且日覆盖账本缺口按真实 blocker 展示 |

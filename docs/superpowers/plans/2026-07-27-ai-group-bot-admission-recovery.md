# AI 活群群管机器人准入与成功事实修复 Implementation Plan

> **For Codex:** Required skill: use `executing-plans` to implement this plan task-by-task.

**Goal:** 修复 AI 活群的两条已证实故障链路：成功发送仍展示“需关注频道”的陈旧错误，以及新入群账号没有可验证观察游标、监听轮询未落观察证据而永久卡在群管机器人准入等待。上线后任务只依据真实 Telegram 成功事实计完成，群管规则没有证据时显式进入待策略/观察失效，不再伪装成“关注频道”。

**Architecture:** 发送终态在 Dispatcher 写入 `success + remote_message_id` 时清理一次性准入错误；Task Center 读模型和前端也以终态成功优先，兼容历史脏结果而不篡改审计原文。群监听每次成功拉取后对同群观察中的 admission 写入批次证据，再在处理控制 bot 事件之后按已验证游标决定 `ready` 或 `policy_unresolved`。入群前由已有监听账号读取群当前游标；无法取得基线时明确进入 `observation_stale`。对线上已有无基线记录，提供有权限、带版本和审计证据的“从当前监听水位重启观察”操作，随后仍需可信规则或显式策略才可 ready。

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

## File plan

### Task 1: 更新 PRD、总 PRD 与索引

**Files:**
- Modify: `docs/03-feature-designs/ai-conversation-humanization-and-group-bot-admission-prd.md`
- Modify: `docs/03-feature-designs/ai-group-daily-fulfillment-remediation-prd.md`
- Modify: `docs/01-product/tg-ops-platform-prd.md`
- Modify: `docs/00-index/project-dataflow-index.md`
- Modify: `docs/00-index/project-structure-index.md`

**Steps:**
1. 添加 2026-07-27 修订记录，明确生产根因、成功事实优先级、listener poll 观察写入、缺基线显式 stale 和遗留 admission 的带审计重启观察。
2. 将任务中心文案约束补为三态：真实成功、已知频道要求、观察/策略未决；成功 action 不显示旧错误字段。
3. 在总 PRD 和日履约 PRD 明确该修复不减少日覆盖分母，也不绕过重复内容、容量或 Telegram 权限。
4. 在数据流和结构索引加入 join baseline -> listener observation -> policy/reconcile -> send gate 的实际入口与回归测试。

### Task 2: 先写失败回归测试

**Files:**
- Create: `backend/tests/test_group_bot_admission_observation_recovery.py`
- Create: `backend/tests/test_task_center_success_fact.py`
- Modify: `backend/tests/test_group_bot_admission.py`

**Steps:**
1. 证明成功发送会清除 `required_channel_admission_pending` 等临时错误；Task Center API 对历史 success 行不投影失败类型、原因或诊断；前端源码先判成功/远端 ID。
2. 证明 listener poll 能写 observation、在到期后无策略转 `group_bot_policy_unresolved`，有 `not_required` 才 ready；控制 bot prompt 仍先于闭合处理。
3. 证明缺失 `join_start_cursor` 不会 ready 而会 `observation_stale`；证明带当前监听水位的显式重启会递增 version 并开始新窗口。
4. 运行新增用例，确认当前实现至少有一条失败，再开始生产代码。

### Task 3: 实现成功事实与群管观察链路

**Files:**
- Modify: `backend/app/services/task_center/dispatcher.py`
- Modify: `backend/app/services/task_center/group_bot_admission.py`
- Modify: `backend/app/services/group_listeners.py`
- Modify: `backend/app/services/group_listener_context_writer.py`
- Modify: `backend/app/services/task_center/service.py`
- Modify: `backend/app/api/routers/operations.py`
- Modify: `backend/app/api/models.py` (only if a new restart request schema is required)
- Modify: `frontend/src/app/views/taskCenterViewModel.ts`
- Modify: `frontend/src/app/views/TaskCenterView.tsx`

**Steps:**
1. 在 membership Gateway 调用前以既有 listener account 取得基线游标并保留在 action result；失败留下可诊断事实，不伪造游标。
2. `ensure_admission_after_join` 对无基线进入显式 stale；新增小型观察批次解析、证据校验与到期闭合函数，所有函数遵守项目函数长度限制。
3. listener 在每次 `fetch_group_messages` 后写观察批次；移除控制消息处理前的 prematurely close，保证可信 bot prompt 优先。
4. 增加 `restart observation` 服务/API：仅 `targets.manage`、expected version、reason/evidence、最新群上下文数字游标；记录审计；无水位拒绝。
5. Dispatcher 成功发送清理临时 `error_code/error_message`；读模型和前端对历史成功的旧错误字段降噪，展示 success/remote id 为先。

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
| 可信 bot prompt | 先转 required-channel/confirmation 链，不能被同轮 close 覆盖 |
| 线上存量无基线 | 显式 restart 审计 + 新窗口，不隐式批量 ready |
| 业务完成 | Action + Attempt + remote ID，且日覆盖账本缺口按真实 blocker 展示 |

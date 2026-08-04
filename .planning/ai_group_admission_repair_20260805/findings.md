# Findings

## 当前代码与分支

- 当前工作树：`codex/fulfillment-contract-v2-20260804`，HEAD `8b67d006`，相对 `origin/release=9b0d8802` 有 3 个本地搜索点击提交；工作树没有未提交生产代码改动，但有用户 planning/docs 改动。
- 生产 current：`/data/tgyunying/releases/20260804141041_9b0d8802`；backend/planner/dispatcher 健康。
- 生产学生会任务已标记 `fact_first_v3`，但线上存在 `group_bot_channel_follow` 与 legacy `group_bot_admissions` 存量。

## 线上 AI 证据（2026-08-05）

- 学生会任务配置 `group_ai_prejoin_channel_ids=["zzxshc","zzxshbg"]`，目标 5000，日冻结分母 796。
- 账号状态为 796 个“在线”、1 个“Session失效”；`frozen_account_count` 是日覆盖分母，不是 Telegram frozen 状态。
- 当前频道关注事实：`zzxshbg` 746 success/51 pending，`zzxshc` 744 success/53 pending；合计 1490/1594 success。
- 当前 pending 绑定动作：96 `required_channel_follow_failed`（FloodWait）、6 `admission_version_stale`、2 `账号不可用`。
- 1490 个成功关注 Action 常为 `closed_unknown`，但结果含 `success=true`、typed `remote_fact_id`、`telegram_msg_id=''`、`remote_reconcile_required=true`；不能删除或通用 retry。
- 学生会 legacy `group_bot_admissions`：463 following、124 awaiting confirmation、201 post-send intercepted、9 follow pending、1 policy unresolved、0 ready。
- 学生会 `task_group_bot_admissions`：705 ready、92 missing，与 legacy 表明显分裂。
- `group_bot_admission_policies` 对 group 2821 当前无行；存量 admission 仍写 `explicit_bot_confirmation`。

## 代码根因候选

- `backend/app/services/task_center/group_bot_admission.py:726` 的 `plan_required_channel_follow_actions` 只为 `status='pending'` 且 `row.action_id` 为空的行创建动作；已有旧 action_id 直接跳过。
- `backend/app/services/task_center/group_bot_admission.py:615` 的 rearm 仅覆盖 `blocked + control_prompt_unverified`，不覆盖 FloodWait、账号不可用、admission_version_stale 后的 pending 行。
- `backend/app/services/task_center/dispatcher.py:8765` 的失败收口只写 Action 结果，没有把可安全重建的 follow 行做 CAS 接管/清空旧 action_id。
- `backend/app/services/task_center/task_group_bot_admission_v2.py:201` 创建新 Task 准入行时读取 `_legacy_admission_ready`；这会产生一次性 legacy 导入，不能把旧表 ready 当成当前事实源。

## 待核验

- 当前工作区是否已经包含上述缺陷的用户修复，还是仅包含搜索点击修复。
- 新 C2 requirement/follow fact 的正确业务键、失败状态和远端 mutation 字段，避免把 unknown 误转可重放。
- PRD/数据流转索引需要补充的当前合同位置。
- 发布前测试集合与 GitHub Actions workflow 的当前输入。

## 本轮 Product Design Complete

- fact-first 可信提示只创建 Task-scoped requirement Action，payload 固化 TaskGroup admission UUID/version、原消息、source fingerprint 和 requirement key；旧 GroupBotAdmission 只作 legacy 审计。
- Action 先由 Attempt/GatewayRequestEvidenceJournal 定义 mutation 边界；仅 FloodWait/慢速等明确 false 的证据允许递增 replan_attempt，unknown/true/远端成功保留旧绑定。
- 账号不可用、目标无效、admission version stale 不自动复活同一账号动作；任务重启是显式例外，创建新 Action 但不复活旧 Action。
- 已有 imported legacy-ready TaskGroup projection 在首次复查时重开 30 秒 viewer observation；新 observation 不再从旧 GroupBotAdmission 导入 ready。

## 配置预关注补充根因

- 生产学生会 `tasks.group_ai_prejoin_channel_ids` 为 `['zzxshc', 'zzxshbg']`，但线上 `AccountGroupAdmissionFact.fact_kind='configured_channel_follow'` 为 0。
- `dispatcher._dispatch_channel_membership` 在已存在 `TgGroupAccount` 时直接处理 `already_joined` 并返回，fact-first 的 `ensure_prejoin_channels` 只在新入群路径执行；因此配置频道从未真正进入 Gateway。
- 修复后 `_prepare_group_send` 和已在群的 membership 路径都会先执行配置频道复核；`task_prejoin_channels` 先读取已有 `configured_channel_follow` facts，缺少的频道才调用 Gateway，失败则正文保持 pending。

## QA / 发布门

- 定向套件：64 passed，2 个 SQLAlchemy reflection warning。
- `TEST_DATABASE_URL=sqlite:///:memory: pytest -m no_postgres`：60 秒硬闸门超时，约 47% 已完成；生产 PostgreSQL 测试库 reset 仍报远端连接关闭。
- 生产现有 96 条 FloodWait follow 的 journal `remote_mutation_state='unknown'`，6 条 admission version stale、2 条账号不可用；本轮不会自动清理或盲重试这些存量动作。

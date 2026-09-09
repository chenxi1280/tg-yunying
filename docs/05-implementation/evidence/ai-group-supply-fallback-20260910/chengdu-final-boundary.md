# 成都账号 213 最终执行边界

- 观察时间：2026-09-10 03:40:21.667783 +08:00。
- 生产版本：`b3406f7ec385ace2b0e06a404e9d5dd66d07f1c5`。
- 取证：生产 backend、`transaction_read_only=on`、`repeatable read`；未修改生产、未手动调用 Telegram。
- 范围：Task `e5882928-35b3-41c2-88e0-fa6520df83e8`，Action `0d335a8c-e577-43ff-9d7a-cf929b652ef6`，Job `70583f0d-f7dc-42f5-a522-3baf08a86ab4`，账号 213、目标群内部 ID 5997。

## 结论

状态 **blocked**：该 Action 首个有证据的阻断点是账号与目标群组合的可见性/C2 准入。03:32:54.952774、03:33:41.850707、03:34:29.366889 的三条 `post_follow_visibility` 事实均为 `ChannelPrivateError`，连续观察失败数依次为 1、2、3。准入最终为 `abandoned`，`terminal_reason=observation_gap_limit_reached`，当天终止，`blocker_code=c2_observation_evidence_missing`。

这不是本次主题证据或应急选择修复再次失败的证据：原 Action 03:34:29.459349 被正式准入门置为 `skipped/c2_account_abandoned`，Job 已 `failed/routing`、owner 空、lease 空，没有 candidate/window；Provider exchange、ExecutionAttempt、typed remote message fact、emergency selection 均为 0。payload 的 `generating` 是保留的历史生成标记，不能覆盖权威 Job 已 failed 和 Action 已 skipped 状态。

## 证据矩阵

| 单位 | 运行/Task | ledger/coverage | Action | Attempt/Gateway | typed 消息事实 | 首个阻断 | 状态 |
|---|---|---|---|---|---|---|---|
| 成都账号 213 原义务 | Task running / epoch 2 | 当日 ledger open；coverage abandoned_for_day | skipped / c2_account_abandoned | 0 / 未进入正文 Gateway | 0 | C2 连续 ChannelPrivateError | blocked |

coverage `efb02d44-5790-43e2-936e-c40fa3af91c1` 仍关联原 Action，恢复路径为 `next_task_day_recheck`；原 quantity `c966eade-87d6-4f19-8dbc-f44bd4bf6086` 和 FOP 保留 open，不把未送达义务计为完成。该 Action 已终态，应急选择对其不可达，不能以修改旧 payload 或手动重发绕过准入。

账号本地状态在线、未冻结、Session 和 authorization 存在，存量群账号 link 为 can_send=true。这些存量字段不构成当前目标可见性证明，不能覆盖三条同账号观察错误。群通用 listener_error 也存在，但此次直接终止依据是账号 213 的 C2 typed 观察事实。

## 推断与未证明边界

- observed_evidence：三次 `ChannelPrivateError`、准入当天弃用、Action skipped、Job failed、Provider/正文 Attempt/远端消息/应急 selection 均 0。
- inference：当前账号授权无法读取目标群，使生成前准入失败；本次应急内容流程尚未运行。
- unproven：仅凭 `ChannelPrivateError` 不能进一步区分被移出群、封禁或目标权限变化；本次没有做远端探针。此账号的 Telegram 消息履约未证明。
- next_route：归入现有账号/目标准入外部限制，保持正常任务日重查合同；本次不扩大内容代码修复。

## 代码对应

- `task_group_bot_admission_v2.py`：读取当前账号观察流异常后以异常类名记录 gap。
- `task_group_bot_admission_state.py::restart_with_gap`：连续三次 gap 后以 `observation_gap_limit_reached` 当天弃用。
- `dispatcher.py::_fact_first_group_bot_admission_gate`：`c2_account_abandoned` 跳过原 Action；`_abandon_fact_first_account_for_task` 将 coverage 标记 admission blocker 与次日重查。

原始脱敏证据：[chengdu-final-boundary.json](chengdu-final-boundary.json)。未保存消息正文、主题正文、会话、手机号、令牌或凭证。

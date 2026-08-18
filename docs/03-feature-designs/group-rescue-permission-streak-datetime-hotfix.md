# 群权限救援连续失败时间排序热修设计

## 0. Incident / Product Handoff

- bug_id: bug-2026-08-19-group-rescue-mixed-datetime-sort
- intake_id: intake-2026-08-17-planner-pacing-memory-001
- level: L3 production incident
- route: prod-diagnosis -> product -> dev -> qa -> product -> prod-diagnosis
- release_gate: required
- design_status: product_design_complete / dev_handoff_ready=true

## 1. 生产事实与根因

release `6b8b573c` 上一条 `send_message` 在 Gateway call-start 后进入必需频道权限救援；`permission_failure_count_for_send_action` 对历史 Action 执行 Python 排序时，同时得到 timezone-aware PostgreSQL 时间和无时间行使用的 naive `_now()`，抛出 `TypeError: can't compare offset-naive and offset-aware datetimes`。Action 已按事实边界固化为 `unknown_after_send`，Attempt 为 `result_unknown`，存在 `remote_outcome_unknown` typed fact；禁止直接重发或释放 quantity owner。

## 2. 修复合同

1. `_action_sort_key` 必须先用既有 `as_beijing` 把 executed/scheduled/fallback 时间统一为北京墙钟 naive datetime，再按 `(time, action_id)` 稳定排序。
2. 不改变连续失败阈值、group/account/task scope、目标 peer、消息正文、quantity slot、Gateway、unknown 或 typed fact 语义。
3. 既有 unknown Action 只由原远端 reconcile 合同处理；本热修不修改生产数据、不创建 replacement、不触发重发。
4. 归一化失败不得 silent fallback；输入仍不是 datetime 时应保留显式异常。

## 3. QA / 生产验收

1. 回归必须混合同一 streak 内的 aware executed_at、naive scheduled_at 和 executed/scheduled 均空行；不再抛异常，计数与既有“success/unknown 截断连续失败”语义一致。
2. 原 group rescue 定向测试和静态检查通过，文件继续满足项目复杂度上限。
3. master -> release 发布成功；Backend/Dispatcher 同一 SHA、healthy、restart=0、OOM=false。
4. 发布后新日志中该 TypeError 为 0；原 Action 仍为 unknown 且 remote fact/quantity owner 不变，不能以发布成功声明远端已发送或未发送。

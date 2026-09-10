# AI 活群 `obligation_not_open` 无限重物化与候选队列饥饿修复（2026-09-10）

## 1. 事故现象

2026-09-10 14:36–14:47（Asia/Shanghai）生产只读诊断：10 个 running `group_ai_chat` 任务当日
合计有效目标 18,946、已到期 9,286、已确认 677（**7.3%**）。其中：

| 任务 | 有效目标 | 已到期 | 已确认 | 完成率 |
| --- | ---: | ---: | ---: | ---: |
| 郑州大学 | 1784 | 878 | 2 | 0.2% |
| 天津音乐 | 1865 | 916 | 6 | 0.7% |
| 三亚 | 2212 | 1085 | 123 | 11.3% |
| 西安天上人间 | 2297 | 1135 | 130 | 11.5% |
| 郑州师范 | 1792 | 885 | 95 | 10.7% |
| 其余 5 个 | 8996 | 4387 | 321 | 3.7% |

发布信息：`RELEASE_SHA=093930751c1d90e2e177aebbbe211264f55d1a2f`
（release `20260910134051_09393075`），18 个 worker 全 healthy，2 个 dispatcher shard live。

## 2. 只读取证（未写库、未重启、未改任务）

工具：`.github/scripts/diagnose_ai_group_blockers.py`、
`.github/scripts/recent_group_ai_task_diagnostics.py`、
`.github/scripts/ai_dispatch_admission_diagnostics.py`、
`.github/scripts/ai_generation_runtime_diagnostics.py`，以及只读
`REPEATABLE READ READ ONLY` SQL 探针。

关键事实：

1. 当日 `actions(action_type='send_message')` 中 **skipped 1,788 条，其中
   `obligation_not_open` 1,595 条（85%）**；其余 skip 原因合计不足 300 条。
2. 这 1,595 条只来自 **104 个义务**，平均 14.6 条/义务，单义务最多 **72 条**（天津音乐
   obligation `6a7d1973-ba72-4eb5-b1ba-ad276c35b78c`：72 条 Action、71 skipped、0 success），
   并且到 14:40 仍在继续产生。
3. 逐小时 `obligation_not_open` 单调增长：00h 0 → 06h 129 → 09h 174 → 13h 208 → **14h 275**。
4. `fulfillment_obligation_projections` 中 `remote_reconcile_only` 321 行，最旧 `opened_at`
   为 2026-09-06 16:49（**已滞留 94 小时**）；其中郑州大学 206、天津音乐 82。
5. 关联 Action 状态：郑州大学 168 条关联 `failed`、37 条关联 `success`；
   天津音乐 81 条关联 `failed`。**均非 `unknown_after_send`**，因此
   `close_unknown_after_deadline`（仅处理 `status='unknown_after_send'`）永不收口。
6. 同一时刻 `task_account_daily_coverage` 中 **4,310 行 `state='ready'`、
   `confirmed_count < target_count`、且无 projection** —— 真正的待物化backlog。
7. Planner 单轮 drain 耗时 200–300 秒（`processed=17 took_ms=220798`），
   数据库侧存在 100.9s / 39.2s 长事务与 `Lock/transactionid` 等待。

## 3. 根因

`daily_coverage_planning._ready_row_filters` 用于挑选可物化的 coverage 行，其中
`has_no_terminal_shortfall_projection()` 只排除 `state='terminal_shortfall'`。

但 `fulfillment_remote_facts.ensure_action_obligation` 对任何
`projection.state != 'open'` 的 Action 都直接 `skip_obligation_action(...,
"obligation_not_open")`。因此 `remote_reconcile_only`、`closed_with_unknown_shortfall`、
`confirmed` 这些同样不可物化的状态**没有被排除**：

1. 某次发送得到 `remote_outcome_unknown` → 义务投影置 `remote_reconcile_only`（禁止重发，只允许对账）；
2. 该 coverage 行仍被选中 → 建 Action → claim 阶段被 `obligation_not_open` 跳过；
3. 行仍是 `ready`，`confirmed_count < target_count` 仍成立 → 下一轮再次被选中……

**饥饿放大**：候选按 `targeted_at ASC, account_id ASC, id ASC` 排序，而这批永远无法推进的行
`targeted_at` 最旧，因此**长期占据候选队列头部**，每轮稳定消耗 planner 批次，把 4,310 行
真实待物化行挡在后面。这解释了为什么 09h/10h/13h/14h 出现 170–275 条 skip 而同期
只新增 3–11 条 open Action。

`close_unknown_after_deadline` 无法自愈：它只处理 `status='unknown_after_send'` 的 Action，
而生产上这些投影关联的 Action 终态是 `failed` / `success`，所以投影永远不会离开
`remote_reconcile_only`（实测最久 94 小时）。

## 4. 修复

**合同**：只有 `state='open'` 的义务投影可再次物化；尚无 projection 的行保留首次物化权。

`backend/app/services/task_center/daily_coverage_planning.py`

- `has_no_terminal_shortfall_projection()` → `has_materializable_obligation_projection()`；
- 谓词 `state == 'terminal_shortfall'` → `state != 'open'`。

`backend/app/services/task_center/executors/group_ai_chat.py`

- 更新 import（`:103`）与 `_base_replan_coverage_statement` 用法（`:1063`）。

两条物化入口（`ready_coverage_plan_batch` 与 `_base_replan_coverage_statement`）共用该谓词，
一处修改同时覆盖；`_load_coverage_rows` 只用于统计，不参与候选选择。

## 5. 安全边界

- 生产核验 `task_account_daily_coverage.target_count` 当日 14,234 行**全部为 1**，
  因此排除 `confirmed` 不会误伤"同一 coverage 行需要多次发送"的场景。
- 无 projection 的行仍然可选（首次物化不被阻断）。
- 修复不改变 `ensure_action_obligation`、不重发 unknown、不放宽任何准入/节奏/内容约束；
  仅停止"注定被跳过"的重复物化。
- **零损失核验**：当日 109 个进入该循环的义务，其全部历史共 1,814 条 Action 中
  `success=0`、`unknown=0`（1,698 skipped、116 failed），对应 109 行 coverage 全部未确认。
  因此把它们移出候选集合不减少任何真实履约；同时验证了该循环不产生
  `safely_not_executed` 回流，即不存在"靠重试把投影重新打开"的有效路径。

## 5.1 发布批次基线

本修复并入 `2026-09-10 资源存储修复` 之后发布，冻结基线 `9a2f1fae`
（`origin/master == origin/release`），其中包含已上线的资源修复 `e454c82a`
（应用 SHA，2026-09-10 15:45 部署）与运维脚本修订 `a5704e16`。
资源修复交接文档明确"保留主工作区 `daily_coverage_planning`、`group_ai_chat` 与其他未提交文件，
不纳入本批"，因此两批无路径重叠；本地 `git stash pop` 到新基线无冲突。

## 6. 定向 QA

| 项 | 结果 |
| --- | --- |
| 新增回归 `tests/test_ai_reconcile_only_replan.py` | 红测：旧谓词下 `['reconcile-only', 'unknown-shortfall']` 被错误选中 → FAILED；修复后 PASSED |
| 既有同族回归 `test_ai_terminal_shortfall_replan.py` 等 9 个文件 | 107 passed |
| 全量 `pytest -m no_postgres` | 见第 7 节 |
| `py_compile` 两文件 | 通过 |

## 7. 发布与 E4

- 发布方式：`deploy/local_release.py prepare/deploy` → 镜像直传 → SSH 安装 → 三层健康读回。
- 待记录：prepared SHA、镜像 ID、部署时间、`runtime.json`、部署后逐任务
  `TaskDayLedger → Action → Attempt → typed remote fact` 只读核对。
- 业务验收口径（不得用部署成功代替）：
  1. `obligation_not_open` skip 数按小时应降为 0（或仅剩首次物化竞争）；
  2. 不再存在同一 obligation 关联 >2 条 Action 的情况；
  3. 郑州大学 / 天津音乐当日新增真实 `remote_message_observed` 事实 > 0 并持续增长；
  4. `ready` 且无 projection 的行数应开始下降。
- 未取得以上生产事实前：`implementation_status=implemented_local`、
  `release_status=not_started`、`production_status=unproven`。

## 8. 未包含在本次修复的发现（独立跟进）

- `remote_reconcile_only` 投影在关联 Action 为非 `unknown_after_send` 终态时无收口路径，
  状态机不收敛（本次只停止无效物化，未补收口）。
- Planner 单轮 200–300 秒与 `actions` 表 100s 级长事务、`transactionid` 锁等待，
  是产能上限的独立根因（当日实际真实发送约 86–105 条/小时，需求约 1,050 条/小时）。
- `account_timeline_conflict` / `group_send_pacing_conflict` 把到期 Action 推迟到
  20:42–23:13 或次日 00:00 的节奏语义需单独定性。
- 成都怡红院 254 条 `result_unknown / 群无权限`。
- 美美备用 `AuthKeyDuplicatedError`（同一 session 两个 IP）。

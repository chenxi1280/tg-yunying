# 2026-08-19 搜索发布 Fence、统一 unknown 投影与目标守恒

## Intake Card

- `intake_id`: `intake-2026-08-19-search-dispatch-release-fence`
- `level`: `L3`
- `workflow`: `prod-diagnosis -> product -> dev -> qa -> product -> prod-diagnosis`
- 原始目标：修复生产任务发布后长时间占用、短时拥挤与资源问题，并保证发送目标不受影响。
- 真相源：`docs/03-feature-designs/search-dispatch-release-fence-hotfix.md` 与生产 typed remote fact。

## Root Cause / Product Handoff

- 专用搜索 Dispatcher 不写共享 `dispatch_claim_active`，Stage B 只回收共享 claim，导致发布前 Gateway-started 搜索 Action 等待 30 分钟 lease。
- 首次修复后审计发现普通 stale-worker/lease-expiry Recovery 也遗漏 direct assignment/obligation 的 unknown 投影。
- 合同：Gateway-started 只能收敛 unknown，不重试、不释放防重身份、不改目标、不计点击/发送成功；pre-Gateway 只能恢复同一 Action。

## Development / QA

- `034216e4`: Stage B 将 current pure-search Action 纳入 release fence。
- `70523382`: 所有 fact-first unknown 路径统一调用 `project_search_click_unknown`；deadline 兼容关闭同 Action 绑定的 legacy `executing` assignment。
- 定向回归：27 passed；Ruff、py_compile、diff-check 通过。
- 两次完整 Release Gate：`32189957474`、`32192010188` 均通过前端、no-PostgreSQL、PostgreSQL、镜像与生产部署。

## Production E4

- 首次发布 Stage B 真实回收 2 条搜索 Gateway-started Action：同一 Action、单 Attempt、单 reconcile case，assignment=`gateway_unknown`，`target_click_observed=0`，无重试。
- 最终发布 current=`20260818223317_70523382`，migration=`0154 head`；双 shard live、capacity=26、active_verified，关键容器同 SHA、healthy/restart=0/OOM=false。
- 最终发布 Stage B 真实回收 2 条群发送 Gateway-started Action：单 Attempt、目标绑定保持、仅 `remote_outcome_unknown`，无 `remote_message_observed`、无重试。
- 最终 SHA 后至少 15 条群发送、7 条搜索点击和 14 条浏览真实 typed fact；群发送九类、搜索六类目标合同 mismatch=0。
- AI 21 次、view 11 次 SourcePacing 准入提前调用均为 0；事实跨约 5 分钟持续产生，不是同秒技术排空。
- Planner 6 小时 1850 点：PSS p95=200659 KiB、max=201079 KiB、CPU p95=21.98%、Telethon/cgroup event=0；当前关键进程均无 OOM/restart，宿主短采样无 swap-in/out。

## Remaining Boundaries

- 搜索 release fence：`production_fixed`。
- 普通搜索 stale-worker/lease-expiry 统一投影：已部署且回归通过，但没有自然生产事件，`E4=unproven`。
- 306 条历史 `closed_unknown Action / executing Assignment` 是终态投影债务，不占 current Action/obligation/worker slot；未做无 preview 批量改数。
- 整体 Planner PRD：历史 SwapUsed 仍约 665 MiB，24 小时/自然日与暂停 comment/like E4 未闭环，`production_fixed=unproven`。

## Release Gate Result

`product_design_complete=true / qa_pass=true / product_accepted=true / release_gate=passed / search_release_fence=production_fixed / send_target_conservation=pass / broader_planner_prd=unproven`

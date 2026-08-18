# 搜索专用 Dispatcher 发布 Fence 热修 PRD

## 1. Intake 与线上事实

- 级别：L3 / P0 防重复边界。
- 线上 release：`f49353fd`。
- 现象：一条 `search_click/search_join` Action 在发布前已经写入 `ExecutionAttempt.gateway_call_started_at`，随后 Stage A 停止旧 `worker-search-dispatcher`；发布完成后该 Action 仍保持 `executing/gateway_call_started`，只能等待 30 分钟 lease 到期。
- 根因：Stage B 的 `recover_fenced_dispatch_actions` 只选择带 `dispatch_claim_active=true` 的共享 Dispatcher Action；专用搜索 Dispatcher 的 current Action 不写该共享 claim 标记，因此没有进入发布 fence 分类。
- 影响：远端结果仍未知且不得重发，但搜索义务、账号与 assignment 在 lease 窗口内被额外占用；发布后的业务恢复被延迟。
- 首次发布后遗漏审计：Stage B 已真实收口 2 条搜索 Action，但普通 stale-worker / lease-expiry Recovery 仍通过通用 finalizer 生成 `remote_outcome_unknown`，未把 direct assignment 从 `executing` 投影到 `gateway_unknown`。生产历史共有 306 条这类状态债，其中 305 条 Action/obligation 已终态且不占当前 Action worker 槽，禁止借本热修无 preview 批量改写；1 条当日 unknown 需由兼容 deadline 链路自然收口。

## 2. 产品合同

1. Stage A 已确认所有旧业务 worker 停止后，Stage B 必须在新 writer 仍处于 fenced readiness 时锁定并分类全部 `status=executing` 的 current `search_click/search_join` Action，不依赖共享 dispatch claim 标记。
2. 最新 Attempt 未进入 Gateway：保留同一 Action/义务/assignment/目标身份，恢复为 `pending`，清理旧 lease；不得新建替代 Action。
3. 最新 Attempt 已进入 Gateway：同一 Action 转 `unknown_after_send`，Attempt 转 `result_unknown`，direct assignment 转 `gateway_unknown`，建立唯一 remote reconcile case；不得自动重试、释放防重身份、改写目标或计为 `target_click_observed`。到 unknown deadline 后沿标准链路收口 `closed_unknown`，不得遗留永久 `executing` assignment。
4. 选择范围只增加 `task_type=search_click AND action_type=search_join`。其他未声明共享 claim 的 Action 不允许被顺带接管。
5. Stage B 仍按现有 100 行有界循环处理并逐 Action 写 AuditLog；循环读回 0 后才允许进入 takeover/activate。
6. 发布后验收必须分别读取 deployed SHA、遗留 executing 搜索数、unknown 防重状态和新 `target_click_observed`；健康检查或发布成功不能代替搜索业务事实。
7. 所有 fact-first `search_click/search_join` 的 `unknown_after_send` 必须在统一 finalizer 中幂等投影 assignment/obligation：只有精确 action/obligation 绑定、Gateway-started Attempt、允许的旧状态才能进入 `gateway_unknown/unknown_after_send`；重复 finalizer 不增加 assignment version。deadline closure 对受同一 Action 绑定的历史 `executing` assignment 兼容收口为 `closed_unknown`。
8. 本修复不自动清理 305 条既有终态历史 assignment；它们没有 pending/claiming/executing Action、没有 open obligation，也不计当前 `_free_search_slots`。任何历史批量修正必须另走完整 preview/hash/apply/readback，不能顺带执行。

## 3. 数据流与锁序

`compose stop old workers -> retire old heartbeats -> stage candidate -> new workers fenced ready -> lock executing shared-claim OR current pure-search Action -> classify latest Attempt -> pre-Gateway pending reclaim | Gateway-started unknown + remote case -> reconcile shared ledger -> takeover -> activate -> new search facts`

- 数据库行锁沿用 `for_update(Action)`，并在同一事务更新 Attempt、Action、搜索义务/assignment、remote case 与审计。
- 不读取或修改 Telegram 消息正文、会话、代理凭据与接收目标。
- 本热修无 migration、无前端与 API 变化。

## 4. QA 与发布门禁

- 单元回归：无共享 claim 标记的 current search pre-Gateway Action 被恢复为同一 pending Action；Gateway-started Action 只产生一个 unknown remote case，direct assignment 进入 `gateway_unknown`；重复运行返回 0。
- 目标守恒回归：Action、assignment、obligation、`target_id` 与 `source_action_id` 全部保持原绑定；发布 fence 不创建替代目标或替代 Action。
- 通用 Recovery 回归：统一 unknown 投影重复执行幂等；deadline 对 `gateway_unknown` 与同 Action 绑定的 legacy `executing` 均收口，不能把点击 unknown 计为完成。
- 范围回归：无共享 claim 标记的非搜索 Action不被选择。
- 既有共享 Dispatcher pre-Gateway/Gateway-started、ledger reconcile 与 Release Gate 测试保持通过。
- 生产：发布前遗留搜索 Action必须在 Stage B 收口；发布后遗留 executing 数为 0、无同 Action新增 Attempt、目标身份不变，且后续自然搜索继续产生 `target_click_observed`。

## 5. 首次生产发布证据

- `034216e4` / Actions run `32189957474` 完整成功，Stage B 输出 `recovered_fenced_action_count=2`。
- 两条真实搜索 Action 均为同一 Action、单 Attempt、单 reconcile case，状态为 `unknown_after_send/result_unknown + gateway_unknown`，`target_click_observed=0`。
- 发布后自然产生 4 条新 `target_click_observed`；同时 7 条新群发 typed fact 的 Action/Attempt、群、OperationTarget、冻结 snapshot、content scope、quantity slot、daily target 与 Task config mismatch 全为 0。
- 首次 fence 边界可判 `production_fixed`；通用 stale Recovery 投影遗漏仍需第二次发布验证。

## 6. 统一投影生产发布与终验

- `70523382` / Actions run `32192010188` 完整成功，前端、两组后端、镜像和生产部署全部通过；生产 current 指向 `20260818223317_70523382`，migration 为 `0154_account_pacing_action_state (head)`。
- Stage B 再次真实回收 2 条 Gateway-started 群发送 Action；两条均保持单 Attempt、目标绑定不变，仅写 `remote_outcome_unknown`，没有 `remote_message_observed`、没有重试，证明统一 finalizer 未破坏非搜索发送合同。
- 激活后两 shard 均为 live、capacity=26、`verification_state=active_verified`；Planner、双 Dispatcher、Search Dispatcher、Recovery、Listener 和 AI generation 均运行同一 SHA，healthy/restart=0/OOM=false。
- 最终 SHA 后自然产生 15 条以上群发送 fact 和 7 条以上搜索点击 fact。群发送的 Action/Attempt、TgGroup、OperationTarget、冻结引用、Task config、content scope、quantity slot、daily target 九类 mismatch=0；搜索 Action/Attempt、assignment、OperationTarget peer、Task config 与结果目标 mismatch=0。
- 最终发布没有自然发生新的搜索 stale-worker/lease-expiry 事件，因此统一普通 Recovery 投影只有回归与已部署代码证据，不能伪造生产异常补 E4；该子边界保持 `unproven`。首次发布捕获的搜索 Fence E4 继续成立。
- 生产仍有 306 条历史 `Action=closed_unknown / Assignment=executing` 投影债务；它们没有 open Action/obligation、没有新增 Attempt，不占 current worker slot。未执行无 preview 的批量生产改写。

`design_status=complete_resynced / implementation_status=deployed_70523382 / qa_status=passed_27 / release_fence=production_fixed / generic_stale_projection=production_deployed_e4_unproven / target_conservation=pass`

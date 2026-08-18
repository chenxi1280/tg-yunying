# 搜索专用 Dispatcher 发布 Fence 热修 PRD

## 1. Intake 与线上事实

- 级别：L3 / P0 防重复边界。
- 线上 release：`f49353fd`。
- 现象：一条 `search_click/search_join` Action 在发布前已经写入 `ExecutionAttempt.gateway_call_started_at`，随后 Stage A 停止旧 `worker-search-dispatcher`；发布完成后该 Action 仍保持 `executing/gateway_call_started`，只能等待 30 分钟 lease 到期。
- 根因：Stage B 的 `recover_fenced_dispatch_actions` 只选择带 `dispatch_claim_active=true` 的共享 Dispatcher Action；专用搜索 Dispatcher 的 current Action 不写该共享 claim 标记，因此没有进入发布 fence 分类。
- 影响：远端结果仍未知且不得重发，但搜索义务、账号与 assignment 在 lease 窗口内被额外占用；发布后的业务恢复被延迟。

## 2. 产品合同

1. Stage A 已确认所有旧业务 worker 停止后，Stage B 必须在新 writer 仍处于 fenced readiness 时锁定并分类全部 `status=executing` 的 current `search_click/search_join` Action，不依赖共享 dispatch claim 标记。
2. 最新 Attempt 未进入 Gateway：保留同一 Action/义务/assignment/目标身份，恢复为 `pending`，清理旧 lease；不得新建替代 Action。
3. 最新 Attempt 已进入 Gateway：同一 Action 转 `unknown_after_send`，Attempt 转 `result_unknown`，direct assignment 转 `gateway_unknown`，建立唯一 remote reconcile case；不得自动重试、释放防重身份、改写目标或计为 `target_click_observed`。到 unknown deadline 后沿标准链路收口 `closed_unknown`，不得遗留永久 `executing` assignment。
4. 选择范围只增加 `task_type=search_click AND action_type=search_join`。其他未声明共享 claim 的 Action 不允许被顺带接管。
5. Stage B 仍按现有 100 行有界循环处理并逐 Action 写 AuditLog；循环读回 0 后才允许进入 takeover/activate。
6. 发布后验收必须分别读取 deployed SHA、遗留 executing 搜索数、unknown 防重状态和新 `target_click_observed`；健康检查或发布成功不能代替搜索业务事实。

## 3. 数据流与锁序

`compose stop old workers -> retire old heartbeats -> stage candidate -> new workers fenced ready -> lock executing shared-claim OR current pure-search Action -> classify latest Attempt -> pre-Gateway pending reclaim | Gateway-started unknown + remote case -> reconcile shared ledger -> takeover -> activate -> new search facts`

- 数据库行锁沿用 `for_update(Action)`，并在同一事务更新 Attempt、Action、搜索义务/assignment、remote case 与审计。
- 不读取或修改 Telegram 消息正文、会话、代理凭据与接收目标。
- 本热修无 migration、无前端与 API 变化。

## 4. QA 与发布门禁

- 单元回归：无共享 claim 标记的 current search pre-Gateway Action 被恢复为同一 pending Action；Gateway-started Action 只产生一个 unknown remote case，direct assignment 进入 `gateway_unknown`；重复运行返回 0。
- 目标守恒回归：Action、assignment、obligation、`target_id` 与 `source_action_id` 全部保持原绑定；发布 fence 不创建替代目标或替代 Action。
- 范围回归：无共享 claim 标记的非搜索 Action不被选择。
- 既有共享 Dispatcher pre-Gateway/Gateway-started、ledger reconcile 与 Release Gate 测试保持通过。
- 生产：发布前遗留搜索 Action必须在 Stage B 收口；发布后遗留 executing 数为 0、无同 Action新增 Attempt、目标身份不变，且后续自然搜索继续产生 `target_click_observed`。

`design_status=complete / implementation_status=complete / qa_status=passed_26 / release_status=pending / production_fixed=unproven`

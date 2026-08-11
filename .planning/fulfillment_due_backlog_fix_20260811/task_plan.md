# AI 活群与浏览到期履约整体修复计划

## Goal

修复生产环境 AI 活群与浏览任务的到期量核算、物化和执行节奏，安全处置既有错误积压，并以发布后 Task -> ledger -> Action -> Attempt -> typed remote fact 的 E4 证据确认恢复。

## Current Phase

Phase 7

## Phases

### Phase 1: 生产诊断与产品设计
- [x] 刷新生产 Task/ledger/Action/Attempt/remote fact 证据
- [x] 定位首个业务断点和代码路径
- [x] 补齐 PRD、专项设计、数据流转、QA 与发布/回滚口径
- [x] 完成 Product Design Complete 自检
- **Status:** complete

### Phase 2: 回归测试与实现
- [x] 为 fact_first_v3 当前准入/open obligations 写失败回归
- [x] 修复 AI 到期量的开放义务核算，阻止重复物化
- [x] 移除已到期 AI/浏览义务的二次任务级摊速并守住 deadline
- [x] 更新项目结构索引和必要运维入口
- **Status:** complete

### Phase 3: QA 与产品验收
- [x] 运行聚焦单测、相关 no_postgres 测试和静态检查
- [x] 验证 all/group/manual、幂等、未知远端结果及账号级安全边界
- [x] 完成本地 QA Gate 与 Product Acceptance；PostgreSQL/完整套件交给 CI Release Gate
- **Status:** complete

### Phase 4: 发布与运行时验证
- [x] 推进 master -> release -> Deploy Production
- [x] 校验 immutable SHA、容器/worker、应用与公网健康
- **Status:** complete

### Phase 5: 生产积压安全恢复与 E4
- [x] 只读 preview 精确识别无 Attempt/Gateway 的错误未来/超额 Action
- [x] preview 两次均为零候选，因此未执行 apply、drain、重启或生产数据修改
- [x] 连续观察 AI 活群与浏览的发布后 typed remote facts
- **Status:** complete

### Phase 6: 剩余履约缺口诊断与新修复计划
- [x] 刷新生产 SHA、任务状态、到期量、确认量、Action/Attempt 与 typed remote fact
- [x] 区分已修复的“不发送”故障、当前吞吐缺口、账号级阻塞和浏览结构容量不足
- [x] 定位频道浏览被单账号 `PEER_INVALID` 误升级为整 Task failed 的代码路径；生产个案根因仍待同 peer 的失败后权威证据确认
- [x] 形成分批开发、QA、发布、受保护恢复与 E4 验收计划
- [x] 复核并修正证据强度、恢复守卫、发布拆分与自然日验收口径
- [ ] Product 更新专项 PRD 并完成 Product Design Complete
- [ ] Dev 按 P0/P1 分批实现，不混合生产配置变更
- [ ] QA/Release/Prod-diagnosis 逐批闭环
- **Status:** diagnosis_and_plan_complete

### Phase 7: 代码实现、发布、受保护恢复与生产复核
- [ ] Product 把“坏/过期账号或义务明确放弃”同步到专项 PRD、数据流转和 QA 合同
- [ ] 实现 PEER_INVALID 分类、pre-Gateway obligation 释放、过期/坏账号终态与受保护恢复 workflow
- [ ] 只读确认 AI generation 首断点并仅实现被证明的代码分支
- [ ] 完成定向回归、完整 CI、master -> release 与生产 SHA/runtime 验证
- [ ] 对精确生产 Task 执行 preview/hash-guarded apply/readback
- [ ] 以发布后 AI remote message fact 与 ViewRemoteFact 验证修复
- **Status:** in_progress

## Success Criteria

1. 同一 ledger 的 confirmed + valid open obligations 不超过自然到期量；重复 Planner 轮转不再增长。
2. fact_first_v3 使用 TaskGroupBotAdmission/数量槽位等当前合同事实，不依赖旧 GroupBotAdmission 才能计算开放量。
3. 已到期义务不再被任务级 minimum spacing 二次推迟，且任何 Action 不越过 ledger.deadline；账号级 FloodWait、冷却、session/代理边界不变。
4. 浏览任务按自然到期缺口和真实账号容量物化；目标超过可用账号时明确暴露容量缺口，不伪造完成。
5. 历史积压处置保留 success、claiming/executing、Gateway-started、unknown；只终结精确匹配的 pre-Gateway 错误义务，并完整释放关联预留。
6. 发布后生产 E4 出现新的 AI remote message facts 与浏览 ViewRemoteFact，积压/到期差距按合同收敛。

## Residual Repair Batches

### Gate 0 — 先确认个案，不把代码风险直接当生产根因

- 已观察：`4fc393df...` 当前 Task failed，错误为 `PEER_INVALID`；source state 仍 active；存在发布后 ViewRemoteFact；当前代码会把一次 `PEER_INVALID` 直接升级为 Task terminal。
- 可确认的代码合同回归：现有 PRD 已规定模糊 `PEER_INVALID/ChannelInvalidError` 只能进入 `target_resolution_unverified`，不得自动成为目标解散或全 Task 终态。
- 尚未证明：触发失败的精确 peer/message 在失败时间之后是否仍被其他账号成功解析；历史 fact 和 source active 单独不足以证明该生产个案一定是假终态。
- 开发前只读补证：定位 failure Action/Attempt、失败时间、peer/message、Gateway 边界；随后要求同 peer 的失败后成功 ViewRemoteFact，或权威 listener/target reprobe。若权威证据证明目标真实终结，则不恢复 Task，只修错误分类与提示。

### Batch A — P0 频道浏览错误整任务终结

- 红测 1：一个账号返回模糊 `PEER_INVALID`，Task 保持 running；当前执行路径进入 `target_resolution_unverified`，pre-Gateway obligation/binding 可验证释放。
- 红测 2：同 peer 后续由另一账号成功解析/产生 ViewRemoteFact，不允许 Task terminal。
- 红测 3：独立权威 target-terminal fact 存在时，仍应终结 Task 并收口全部未进 Gateway 的 siblings；不得把真实目标终态降级为账号错误。
- 修复：删除 `PEER_INVALID -> _terminalize_fact_first_target()` 的隐式升级；复用现有 `target_resolution_unverified` 合同。Task terminal 只接受明确的 target lifecycle authority，不以群名、单账号 cache 或一次异常判断。
- terminal 收口必须同步释放每个可证 pre-Gateway 的 View obligation/current action binding；Gateway-started、unknown、confirmed 和 lifetime identity owner 不释放。
- 恢复 preview manifest 必须包含：精确 Task ID、失败 Action/Attempt、失败时间、当前 Task epoch/status/error hash、target lifecycle/version、source revision、ledger、pre-Gateway/unknown/confirmed 集合 count+hash、当前部署 SHA。
- apply 只允许在新代码已部署、preview 未漂移、同 peer 失败后成功 fact 或权威 active reprobe 存在时执行 CAS；创建审计、新 lifecycle epoch 与正常 replan，不复活旧 skipped Action，不改写 ViewRemoteFact。
- readback 必须证明 manifest 守恒、旧 epoch 无新 writer、unknown 未重发、Task running 且新 epoch 出现完整 Action -> Attempt -> ViewRemoteFact 链。
- 若 Gate 0 证明目标真实终结：跳过生产恢复 apply，保留 failed，仅发布分类/诊断修复。

### Batch B0 — P1 AI 吞吐只读定因

- 对生产 fingerprint `90d806e71afa7eca60215140884c2a17` 等 Action 查询执行脱敏 `EXPLAIN (ANALYZE, BUFFERS)`；核对 `ix_actions_ai_generation_due_claim` valid/ready、实际命中、sort、heap read、rows removed 和历史 Action 基数。
- 把 generation 漏斗按 Task 分解为 eligible -> candidate -> claimed -> provider -> quality -> ready -> Attempt -> remote fact，并分别统计 duplicate、context freshness、contract mismatch、check-in handoff。
- 21 个 claim 集中在一个 Task 只记为“公平性风险”；在证明其他 due+generatable Task 被持续饿死前，不直接实现轮转算法。
- Gate 输出只能选一个首断点：candidate query、claim fairness、provider/quality、Dispatcher/Gateway 或仅历史 outage debt；不同首断点不得混成同一代码提交。

### Batch B1 — 仅在 B0 证明后修 AI 性能/公平性/内容断点

- 查询性能分支：使 claim query 与 partial index/order 合同一致；候选查询 p95 不高于生产 2 秒 worker interval，且连续观测无超过 5 秒的同 fingerprint active query；不得通过缩短 Action 留存或增加 worker 掩盖。
- 公平性分支：红测覆盖多个 due+generatable Task；稳定游标保证每个 Task 在算法推导的有界轮数 `ceil(due_task_count / per_round_distinct_task_budget)` 内获得 claim。高 debt 可多拿份额，但不能饿死其他 Task。
- 内容分支：不放宽重复、内容安全和 reply 约束；只修被证实的 variation/context/mapping/check-in handoff 断点。每个分支单独测试和发布。
- 当前自然日因半日故障形成的历史欠量不得靠突发补发强行归零；deadline 后按真实结果 settlement，不跨日搬债。
- 短窗口 E4：至少两个连续 15 分钟窗口有 typed remote fact，所有 due+generatable Task 无饥饿；使用 `confirmed_delta/due_delta` 与 catch-up ETA 判断趋势，不要求每个 5 分钟窗口因抖动严格相等。
- 完整验收：在下一个完整自然日，非结构 blocker 的 Task 必须完成 SettledDueSet；当前残缺自然日只能证明 candidate restored，不能证明自然日 SLA。

### Batch C — P1 AI 账号覆盖与代码缺陷分流

- 运维事实而非代码修复：`session_expired/cannot_send` 进入账号级当日 abandoned/missed；FloodWait 保持安全等待或 deadline missed；不得通过代码伪造可用账号。
- 需独立证据：`membership_permission_denied` 先区分账号无权、目标无权和模糊 peer resolution；不得直接传播到 Task terminal。
- 远端不确定：`unknown_after_send` 仅权威 remote reconcile 可 confirmed/no-mutation，禁止自动重发。
- 独立代码批次：`ai_generation_slot_mapping_mismatch` 按精确 slot/action identity 写红测、修复与审计；不得与账号恢复或批量 coverage 修改混合发布。
- E4：逐账号 `coverage.account_id = Action.account_id = Attempt.account_id`，且 success + 非空 remote id 才 confirmed；其余明确显示 missed/unknown/blocker。

### Batch D — P1 浏览结构容量与产品口径

- `fa75ca69-2377-4282-80c1-20f66ebbd086` 当前 due 基本物化，但 lifetime 目标高于 distinct 可用账号容量。
- 现有详情页虽展示“目标/完成/缺口”，但后端仍以 Action 总数推导 target/capacity_shortfall，不是 current TargetSet/DueSet/MaterializedSet/ViewRemoteFact 真相；计划必须先替换 read model，不能只增加 UI 字段。
- 后端/API/UI 必须展示 `configured target/effective target/current due/materialized/confirmed/eligible distinct identities/structural shortfall/source state`；不得自动降低目标。
- 运营选择只有两条受审计路径：补充合格 distinct 账号，或显式修改后续目标 revision；历史 target/fact 不重写。
- E4：现有事实 `remote_fact_gap=0`；若容量未补足，状态应为 typed structural shortfall 而非“运行失败”或“已完成”。

## Release Order And Stop Conditions

1. Release A 只包含 PEER_INVALID 分类、obligation 收口、诊断和受保护恢复工具；不得夹带 AI 查询/公平性改动。
2. Gate 0 与 Release A E4 通过后，才允许对 `4fc393df...` preview/apply；preview 漂移、同 peer 权威证据不足或存在未解释 Gateway-started/unknown 时停止 apply。
3. B0 是只读诊断，不发布业务改动。B1 只实现 B0 证明的第一个断点；性能、公平性、内容三个分支默认独立发布。
4. Batch C 的 slot mapping 代码修复与 remote reconcile/账号运维分开；任何 unknown 数量变化都需 case-level evidence fingerprint。
5. Batch D read model 可独立发布；账号扩容或目标 revision 是单独运营变更，不得作为代码 E4 的前置假成功。
6. 每个 release 都需完整 CI、deployed SHA、runtime health 和任务类型独立 E4；A 失败不推进 B，B 失败不通过加 worker 绕过。

## Revised Completion Semantics

- `incident_send_continuity_fixed`：发布后 AI/浏览持续出现对应 typed remote facts，只证明“不发送”恢复。
- `short_window_candidate_fixed`：两次 15 分钟窗口证明首断点恢复、无饥饿/重复远端效果，gap 趋势符合可达容量。
- `full_day_fulfillment_fixed`：下一个完整自然日 settlement 满足全部非结构性 DueSet；结构容量不足必须单独为 shortfall，不能标完成。
- `production_fixed`：只对明确验收范围使用，不能把上述三个层级合并成一个总完成状态。

## Decisions Made

| Decision | Rationale |
|---|---|
| 从本地 origin/master 跟踪点建立独立 worktree | 主 checkout 有大量用户未提交修改，必须保持不动 |
| 先修核算与调度合同，再处理历史积压 | 否则 cleanup 后 Planner 会再次制造相同错误 |
| 自然 due curve 是唯一任务级节奏闸门 | 已到期义务应由真实账号/资源容量与安全约束执行，不再二次按模板摊速 |
| 生产修复只接受 typed remote fact | CI、部署、health、Action success 均不能替代 Telegram 远端事实 |

## Errors Encountered

| Error | Attempt | Resolution |
|---|---:|---|
| `git fetch origin master release --prune` 在 GitHub TLS 握手时报 `SSL_ERROR_SYSCALL` | 1 | 记录为访问路径故障；先基于 2026-08-11 02:52 已知 origin/master 跟踪点设计/开发，推送前必须重新 fetch 并校验祖先关系 |
| macOS 缺少 GNU `timeout`，首次红测命令未启动 | 1 | 改用 Python `subprocess.run(..., timeout=60)` 包裹主 checkout 的 `backend/.venv/bin/pytest`，不重复原命令 |
| 完整 `-m no_postgres` 在 60 秒硬门禁内只运行到 45% | 1 | 保留 hard timeout；聚焦 276 项通过，完整套件和 PostgreSQL 分区由 Deploy Production CI Gate 执行 |

## Notes

- 用户已授权完整修复、发布和线上验证，但生产数据 apply 仍必须先 preview、精确目标、漂移守卫、审计和 readback。
- 浏览 1000/消息高于当前约 797 个可用账号属于容量缺口，代码不得静默降低目标或伪造成功。

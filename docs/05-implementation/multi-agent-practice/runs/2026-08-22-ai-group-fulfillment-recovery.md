# 2026-08-22 AI 活群履约恢复

## Intake Card

- Intake ID：`intake-2026-08-22-ai-group-fulfillment-recovery-001`
- 分级：L3 / P0
- 目标：恢复 current `group_ai_chat + fact_first_v3` 任务进入 Gateway 的能力，修复错误降级；不放松质量、账号、节奏或 typed remote fact 门禁。
- 生产验收对象：2026-08-22 线上全部六个 current AI 活群任务。
- 非目标：不压缩逾期工作、不批量改写历史、不伪造成功、不把容器健康或 AI provider 健康当作履约完成。

## 生产现场与首个断点

2026-08-22 11:48 至 12:10（Asia/Shanghai）的只读诊断显示：

- 六个任务均为 `confirmed=0`、`gateway_started=0`、`typed_remote_fact=0`；
- ready pending 持续增长到约 2200 条，最早从 00:17 至 00:53 逾期；
- 双 dispatcher shard 均 `live`，部署 SHA 与六个核心容器通过；数据库无 blocking edge；
- ready Action 的 lifecycle、`fact_first_v3` 合约和 open account pacing reservation 均满足直领条件；
- 最近 Attempt 集中为 `pacing_source_not_before`，Gateway marker 为空；
- 代码边界复核确认 `_candidate_rows()` 未应用当前 worker 的 account shard，而 `_apply_claim_account_policy()` 在执行阶段才以 `account_shard_mismatch` 退回并延迟 Action；
- two-stage 正常拒绝、brief silence 和 realize exhaustion 的占位内容未保存原 `plan.slot_id`，被后置 mapping 守恒校验误报为 `ai_generation_slot_mapping_mismatch`。

第一断点为 Dispatcher pre-Gateway claim/admission；AI provider 多 active/failover 已独立上线并通过 provider 运行态核验，不是本批首断点。

## Product Handoff

`design_status=product_design_complete`。冻结合同：

1. two-stage rejection 必须保留原 slot identity；真正的 missing/swapped/account/coverage mismatch 继续 fail-closed；
2. fact-first direct claim 必须在 claim 查询阶段限定到当前 worker account shard；单分片、无账号 Action 和 execution lane 语义不变；
3. 生产诊断只输出 ID、状态、时间、计数和原因，不输出消息正文、Session、手机号或 provider key；
4. 不使用 channel-view 专用 orphan reconcile 脚本处理 AI；任何后续生产数据纠正都必须 exact task/current ledger/pre-Gateway、preview/CAS/audit/readback；
5. 账号 Session、群权限和发送资格属于外部运行事实，不以代码伪造 ready；
6. duplicate/generation churn 继续遵守 `ai-group-generation-failure-churn-remediation-prd.md` 的稳定义务设计，不以固定等待、放松去重或模板 fallback 快修。

## Dev 交付

- `90c8e468`：two-stage rejected marker 写入 `plan.slot_id`；覆盖 plan rejection、silence、realize exhaustion 和 accepted mapping；新增只读 AI dispatch admission 诊断。
- `78ea9aea`：fact-first direct candidate query 接受 `shard_total/shard_index`，在数据库 claim 前过滤 account shard；dispatcher 注入当前 shard；新增双分片回归。
- `5b013aa8`：诊断增加当前 Action 的 claim release / pacing defer 原因统计。

本地验证：相关 82 passed；`py_compile` 与 `git diff --check` 通过。测试均由 60 秒硬 timeout 包裹。

## Release Gate

- [x] dirty 主工作区未触碰；实现位于隔离 worktree/branch。
- [x] Product Design Complete。
- [x] 定向回归、节奏、生命周期和 dispatcher 测试通过。
- [x] 只读 production monitor 脚本在 feature ref 成功执行。
- [ ] independent QA pass。
- [ ] product acceptance。
- [ ] master -> release CI/deploy。
- [ ] deployed SHA、容器与 provider route readback。
- [ ] 六个任务的 post-release Gateway/typed remote fact E4；无新 slot mapping mismatch、unknown retry 或 double admission。

未满足最后三项前，状态只能是 `unproven`，不得写 `production_fixed`。

## 13:05 生产复检补充 / Bug Batch Plan

第二轮部署后只读证据确认六个 Task 均持续新增非空 `remote_message_id`，且未产生新的 slot mapping mismatch；同时定位两个不会靠等待自行闭合的验收缺陷：

1. E4 直接 `count(TaskAccountDailyCoverage)`，把已经按当前合同 `abandoned_for_day` 并释放义务的行仍计入 required。西安运行有效分母为 1060、旧 E4 为 1211；楼凤有效分母为 854、旧 E4 为 1212。修复只调整 E4 required 聚合，运行明细继续展示 abandoned 原因，不改任何生产目标或 Coverage 行。
2. 修复前形成的少量 `ai_generation_slot_mapping_mismatch` 行仍处于 `blocked/generation_contract`。代码修复只阻止新错行，不能把旧行隐式视作安全；必须新增 exact Task/current date/no-Gateway preview/hash/CAS/AuditLog/readback 路径，释放原 coverage 为 ready 后由 Planner 创建新 Action。

Mini Bug Card：L3/P0；Root Cause Grouping 为“验收投影口径错误”和“前向代码修复缺少存量状态迁移”两组。验收要求：abandoned 不再进入 E4 required；其他未完成状态仍保留；恢复 workflow 对零匹配、hash 漂移、Gateway marker、非精确 blocker 和非 current Task 全部 fail closed；apply 后 blocker identity 清空、Task 被唤醒、AuditLog 和 fresh readback 存在，远端完成仍单独验收。

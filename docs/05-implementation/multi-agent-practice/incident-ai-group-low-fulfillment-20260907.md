# AI 活群低完成量：诊断与发布闭环

- intake_id: AI-GROUP-LOW-FULFILLMENT-20260907
- level: L3
- severity: P1
- source: 用户要求修复低完成量，汇总此前修复后按流程提交、发布并线上监督。
- stage: prod-diagnosis -> product -> dev
- design_status: complete（本轮词频/上下文查询根因）；整体线上验收 pending。
- authorization: 正常修复/测试/提交/发布与只读线上监督；用户已确认 Antigravity 暂停生产修改。
- baseline: master/release d9c4cf75，生产发布标记 3ed0e062；当前未提交修改已保存 patch/hash，保留并一并审核。

## 线上证据（北京时间 2026-09-07 20:26–20:27）

10 个 running group_ai_chat，fact_first_v3。日内到期目标 15470、confirmed 3；仅天津音乐在 07/08/09 时段各一条，同 Action/Attempt/account/ledger 的远端消息事实匹配。其余九个任务零完成。最近两小时 137 次 pacing_claim_deadline_exceeded、11 次 execution_circuit_open、11 次 pacing_source_not_before，均零 Gateway/remote message。

当前 due generation pending 1776、provider_result_unknown 365、ready pending 551。以上不同口径不可相加为日目标。未知结果仍需同身份对账。

20:33 左右 pg_stat_activity 发现多条词频查询同时超过 80 秒；有事务持有 tg_groups 行锁使 Listener 排队。只读 EXPLAIN 显示 actions Parallel Seq Scan，JSON surface 无索引。增加 join tenant 条件单独验证仍超过 15 秒，因此不是只加租户谓词即可解决。需修复真实访问路径；不更改 DB 全局超时或 worker 并发掩盖根因。

## 本轮产品交接

合同见词频 PRD 的 2026-09-07 修复章节。开发：同 tenant/surface 的去重、先过滤后截窗、最新 typed fact 时间；surface 表达式索引及完整上下文时间索引。测试与发布证据在完成后追加。业务状态：production_failed，尚未宣称恢复。

## 开发与 QA 回读

词频 3 个新增反例先红后绿，原 5 项保持通过；两个索引的 SQLite/真实 PG 检查通过。受影响场景 170 项、升级/迁移图组合 15 项、生成质量与 pacing/真实 PG 并发 84 项、最终来源/相册 8 项通过（各组合有少量重叠，不相加作独立总数）。已更新产品合同和结构/数据流索引，进入 Release Gate。线上尚待正式部署与新事实验证。

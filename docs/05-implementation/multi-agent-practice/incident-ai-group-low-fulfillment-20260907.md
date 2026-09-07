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


### 2026-09-07 生产回流：已终结未发送 Action 的 ready 内容槽

正式发布 d7ba60d 后仍出现 `ai_content_window_concurrent_conflict`。只读核验的 20 个样本均为旧 Action 因 `pacing_claim_deadline_exceeded` 进入 skipped，Attempt 已结束且未进入 Gateway，typed fact 为 `safely_not_executed`；但旧 Job 保持 ready/reviewing，slot 保持 candidate_ready，阻挡同一 obligation 的后续序列。现有 terminal-Job 分支及 gateway_bound 分支遗漏这一状态组合。

Product Design Complete：将已存在的“终态 Action + 正面未执行证据”回收合同覆盖至 `candidate_ready + Job ready/reviewing`，与原 `gateway_bound + Job ready/gateway_bound` 采用同等 owner/tenant/task/epoch/obligation/window/job 绑定及全部 Attempt/typed fact 检查。按行锁串行使旧 slot invalidated 并释放 owner/lease；保留 Job/Action/Attempt/fact 和所有远端去重身份，不修改状态或完成量，不放宽 current-obligation 唯一约束。只在后续正式生成绑定原 obligation 时回收，不直接批量改生产数据。active/unknown/身份漂移、未结束 Attempt、已调用却无正确未执行事实、已有 remote ID 均不得回收。

QA 必须先复现 ready/reviewing/candidate_ready + skipped 的冲突，确认回收后同 obligation 的新窗口能够冻结；覆盖原 gateway_bound 及所有证据反例，真实 PostgreSQL 验证 partial unique 和行锁路径。该修复不绕过行为 Session/来源 deadline，过期积压仍按原合同结算。


### 2026-09-07 频道成员前置多账号组合同补正

线上 13 个 running 频道任务（2 评论、6 点赞、5 浏览）均为 selection_mode=group、account_group_ids 含 11 组、旧 account_group_id 为空。成员前置候选读取仅识别单组，导致配置合法但候选为空。多组列表是当前账号范围合同：非空 account_group_ids 优先；只有缺失/空列表时兼容已保存的单组字段；均为空返回空范围，禁止扩大至 all。保留 tenant、active、未删除、普通运营用途与救援管理员排除条件及原排序；manual/all 行为不变。

Product Design Complete：修复候选范围读取，不改变账号组配置、成员版本、原义务/Action/Attempt、关注前置规则或数量目标。抽出成员候选选择模块保留旧公开导入入口；多组、单组兼容、优先级、空范围和用途/租户边界由定向测试覆盖。上线后只读复查候选数和成员前置推进，再以三类 typed 远端事实验收；候选增加不等于关注或履约完成。

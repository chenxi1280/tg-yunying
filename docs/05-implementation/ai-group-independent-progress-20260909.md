# 活群已入群账号独立推进与跨日修复

## Intake / Bug Batch Plan

- intake_id: intake-20260909-ai-group-independent-progress；level=L3；severity=P1。
- 用户要求：发送前受阻直接抛弃、未入群账号不得影响已入群账号、修复两个任务跨日异常。
- baseline：master 7b946217（代码生产0c5e5dc5）；开始时工作区干净。当前Codex负责product→dev→qa→product→prod-diagnosis各阶段，单写者。
- 生产只读：9月8日10任务19272目标、45条真实消息确认；发布后3条；天津音乐/郑州学生会旧ledger仍open且缺当前日完整链。
- A：发送前受阻账号/动作的本轮隔离，保留未完成量及unknown。已提出可选澄清，未回复时按跳过受阻账号处理，不删除未知结果保护。
- B：成员候选批次空导致全Task早退；coverage先LIMIT再按admission过滤，pending_admission/不可发送前缀饿死健康账号。
- C：ledger/目标/slot只在正文规划阶段创建且同事务回滚，入群等待/正文竞争阻止跨日持久化。

## Product Handoff

合同为统一引擎PRD§19.68。成员批次不决定其他账号的正文资格；当前准入集合进入分页之前的SQL条件；日初始化独立提交且幂等，不迁移旧Action/Attempt/unknown/fact。无新增API/表单/迁移；状态继续使用现有摘要、日志和typed blocker。PDC覆盖原话、分母、事务、停止/退役、账号/日期归属及QA，design_status=complete，进入dev。

## QA / Release Gate

- status: local_gate_passed；release_mode: github_actions；release_path: master→release→Prepare Production→Deploy Production。
- backend_tests: 76项通过（成员/分页5、跨日SQLite4、发送前隔离及正式fact结算8、真实PostgreSQL跨事务/锁2、现有相关回归57）；每批硬超时60秒，最长10.51秒。PG16.15独立本地实例54689/tg_yunying_test、随机临时schema及现有advisory-lock校验。compileall与git diff --check通过。新增测试先观察到原实现5个成员/跨日失败、5个发送前等待失败，再修复。
- migration_impact: none；frontend_build: 无前端变更，沿现有CI；worker_impact: Planner/正文候选与准入。
- production_probe: 完整SHA/runtime、两任务当日ledger/target/slot及原日保留、已入群候选跨前缀推进、发布后真实消息。
- rollback: 无破坏迁移；已记录事实和原日身份保持，不能通过重放unknown恢复。
- production_status: unproven；本文件的设计和测试计划不等于生产恢复。

## Dev / Review / Product Acceptance

- dev_complete：新增两个窄模块，正文候选SQL过滤提前，日初始化独立提交，发送前明确未调用动作本轮跳过并释放精确coverage。未知调用、正常pacing与原数量分母保留。
- review：检查全历史Attempt/Gateway/fact证据、tenant/task/account/action归属、停止/退役和原Task/wake锁、独立事务回滚、空admission不回退、投影仍open；无迁移或新增配置。已有超大模块只接入窄入口，新模块分别44/63行，修改函数均不超过50非空行。
- qa_pass=true；product_accepted=local_behavior；生产验收仍须发布后typed事实。
- 发布前直接SSH只读复查出现banner exchange timeout；这仅证明当前读取通道失败，不证明应用状态。

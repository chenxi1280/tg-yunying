# 旧 PRD 对照 P1 修复批次（2026-09-08）

## Intake / Product Handoff

- 用户指令：修复上一轮 §19.63 的 P1 问题（R1–R6）。
- 分级：L3；先修订设计与反向核对，再 dev → qa → product → release / 生产验证。
- 原有 22 份未提交 PRD 修改为本会话前序成果，原样保留；本批次不包含其他独立功能扩展。
- 上位合同：统一引擎 §19.13–19.15、§19.58、§19.61、§19.63。局部设计完整；实现状态逐项登记，不把文档修正当代码完成。

## Bug Batch Plan / Root Cause Grouping

| 组 | 项 | 开发与验收边界 | 当前状态 |
| --- | --- | --- | --- |
| 保留与清理 | R2 / R3 | 先复现 48–72 小时统计依赖丢失及业务引用删除；共享正式候选规则，锁内复核，保护必要依赖；真实 PG 验证并发与清理守恒 | local_qa_pass |
| 范围与内容 | R1 / R4 | 反查轻量 timing、内容 intent/配置生效、正文/应急统计；存在违约入口才改，保留原 owner，不重建已撤销架构 | local_qa_pass |
| 账号与生命周期 | R5 / R6 | 反查冻结参与计划与退役入口；日中成员变化不缩分母，退役记录/映射保留，旧Task不可重启 | local_qa_pass |

## R2 / R3 实施合同

1. 保留原 `recent_success` 的最早有效成功口径与 72 小时窗口；正常统计和清理保护使用同一类型/身份/有效成功判定。
2. 普通清理只消费没有受保护依赖的明细。已在旧清理合同明确允许释放/删除的导航与纯日志仍按原流程；其他 Action/Attempt 外键引用及未结算业务归属保护，不自动级联。
3. preview、自动清理、apply 共用同一选择器与冻结 `as_of`。取得 Action/Attempt 锁后重新验证依赖；并发漂移不提交统计或删除。
4. preview 明确列出被保护引用类型及其统计范围；无业务引用且过期的普通明细继续可清理，不以全局停用清理代替修复。
5. 不删除事实、不改已有 FK、不运行生产维护；独立 PostgreSQL 测试库名固定 `tg_yunying_test`，pytest advisory lock 生效，每条测试命令 60 秒硬超时。

## R4 开发反查 resync

两个公开内容编辑入口仍调用 `_requeue_updated_task`，暂停/停止任务会被重启，运行任务next_run_at被改成now；同值保存被误判成结构改动并清空计划。修复限定纯内容与同值内容保存，不改变独立start/resume/stop命令。新增两个真实服务入口、四种状态和改值/同值回归。

## 验证与 Release Gate

- 必需：新反例先失败；清理前后四类最近成功/逐账号计数相同；业务 owner/事实/unknown、防重与并发资格守恒。
- 定向回归：既有状态TTL、预览/指纹/apply/readback、双worker、轻量生成、内容配置、成员冻结及退役测试。
- 代码审查：查询范围与索引、锁顺序与竞态、UTC/北京时钟、无新增预算/默认配置或未知重放。
- 发布前：代码/文档范围、定向测试、迁移兼容与 SHA 分别核对；生产事实单独验收。
- 当前尚未发布、未执行生产清理，`production_status=unproven`。

## Dev / QA / Product 本地验收记录

| 项 | 最终处理 | 本地证据 | 生产状态 |
| --- | --- | --- | --- |
| R1 | 删除冲突设计要求；当前轻量生成沿用原Job/timeout，不恢复历史画像审批与模型预算表 | `test_generation_timing_binding.py`、`test_engagement_account_binding.py` | unproven |
| R2 | recent_success与清理共用有效成功判定，按最早有效确认保留滚动72小时依赖，四类及逐账号归属不丢失 | `test_recent_task_success.py`、`test_runtime_retention_business_protection.py`、PG统计读回 | unproven |
| R3 | 保留业务引用、活跃owner、待投影和unknown；锁内复核后才汇总删除；普通安全明细与下一可清理batch仍正常处理 | 评论/点赞/浏览真实PG CASCADE引用、并发FK写入锁、preview后新依赖复核、双worker清理 | unproven |
| R4 | 纯内容改值/同值保存不重启、重排、清空Action；内容intent和比例能力沿当前入口保留 | 两个公开服务入口×4状态×2种保存共16例；话题比例、内容所有权、词库/分配计划回归 | unproven |
| R5 | 当前冻结selected与动态可发送状态分开，设计不再要求日中缩分母 | `test_engagement_participation.py` | unproven |
| R6 | 当前退役保持原Task和映射；不恢复旧物理删除切换流程 | `test_task_retirement.py`、`test_engagement_direct_cutover.py`、3项PG退役/调用竞态 | unproven |

- 最终集中定向运行：**215 passed in 30.74s**，包括新保留保护、5项真实PG引用/并发、16项内容更新生命周期及相关既有回归。
- 单独运行的既有PG retention两项与退役PG三项均通过，合计覆盖**220项不同用例**。每次后端测试均由独立进程组60秒硬超时约束；真实PG使用独立本机实例的`tg_yunying_test`及pytest advisory lock。
- 现有Alembic迁移链在隔离测试实例从空库迁移到`0228_account_freeze`成功；本批次不新增迁移、FK变更或TTL配置。
- 本地SQL EXPLAIN ANALYZE样本：5,000条额外Action、5,000条typed fact、5,000条projection，候选batch=100约75.124ms，planning=3.95ms。此为隔离库样本，不是生产容量结论。
- 新增/改动Python通过编译检查，新增函数不超过50行，`git diff --check`通过；旧大文件仅改必要入口，未做相邻重构。
- 可见影响：有业务引用的历史明细会保留更久，实际回收量可能低于旧逻辑；这是保护业务真相所需，未定义安全导航释放前不自动级联。未进行生产清理或数据修补，也不宣称找回历史上已删除的记录。
- 本地产品验收限定本表P1差异和实际代码修复，`local_status=qa_pass`；§19.60/§19.61尚未开发的新应急/异步完整能力不纳入本轮完成声明。
- Release Gate：`candidate_sha=uncommitted_worktree`、`actions_run=none`、`deployed_sha=not_verified_for_this_slice`、`production_status=unproven`。本地改动保留在当前工作区，前序PRD成果完整保留。


## 后续用户需求resync：账号失效不分配（2026-09-08）

用户随后明确“Session失效、冻结这种失效账号不会分配任务”。统一PRD新增§19.64并修订旧资格条款：R5此前对冻结selected的验证只证明历史计划不重抽，不证明分配前已排除失效账号。当前 `engagement_policy_scope` 及existing-plan分配消费者需要按新设计开发；上表R1–R6和220项QA仍是上一批次的历史证据，不适用于本次新增资格验收。此阶段只修改PRD与设计索引，前序代码改动保持原样，`design_status=complete`、`implementation_status=pending_for_this_revision`、`production_status=unproven`。

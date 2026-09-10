# AI生成结算一致性修复与Release Gate（2026-09-10）

- intake_id: AI-GROUP-LOGIC-REPAIR-20260910；L3/P1；release_mode=github_actions。
- owner: 当前任务；独立工作区`/tmp/tgyunying-ai-logic-repair-20260910`，基线af602f73。
- 用户确认范围为群活跃通用逻辑代码错误修复。原主工作区三个未跟踪文档及其他工作区均未修改。
- PRD：`docs/03-feature-designs/ai-generation-settlement-integrity-20260910-prd.md`。

## 生产证据与根因

08:28只读核对生产仍972597a4。三个generation worker实际DataError均为varchar(32)溢出，调用链prepare_topic_or_emergency→mark_emergency_pending→finish_owned_job；传入33字符原因。Task锁冲突调用链settle_parallel_outcome→finish_generation_job→_require_not_retired，发生在旧Action释放事务之后。原Job51a03807已cancelled/action_state_mismatch，Action1171663a仍pending/ready；该历史终态本补丁不自动复活。

08:38 binding错误指纹定位context_route_evidence_missing；部分旧payload有原始中文但清洗后无事实，另一些旧payload仍有事实，因失败事务已回滚，不能从旧payload还原全部失败瞬间输入。中性系统通知反例证实前置检测与绑定检测不一致。

西安任务曾出现目标投影59、真实事实60；08:41:04目标刷新后，目标确认60与按原规则重算60一致，全部成功Action的内容记忆证据有效。本项为Planner周期刷新造成的读模型延迟，未改写计数或业务事实。

## 已实现范围

- 原Action解除生成claim与原Job ready同事务提交；仅处理claim指定Action，保持同owner合法Job版本推进，旧owner/token/epoch和错误hash不能发布。
- 记录不含明文token的结算摘要用于同claim幂等读回。
- 已准备的过期Job在原Action锁下核对同身份、内容hash和原候选窗，只收口本地生成状态；不重生成、不重发，不给已作废窗口恢复发送权。
- Job阶段使用emergency_pending，完整触发原因保存到Action结果和Job证据。无schema变更。
- v2前置用fact_id_map判断可用上下文及配置话题；原清洗、权限、路由及回复身份规则保持。

## 定向验证

- 修复前新增SQLite反例10失败，真实PostgreSQL2失败，分别复现Action半提交、错token、过期ready误取消和字段溢出。
- 首轮修复：61项生成/应急/unknown定向测试通过；另66项生成worker/恢复/话题用例通过。集合有重叠，不累计成唯一用例总数。
- 最新验证：原子结算、窗口保存与拒绝、话题、应急相关72项通过（13.51s）；真实PG行锁、字段约束和Action竞争3项通过（6.39s）。
- Ruff通过，git diff --check通过；修改的生成恢复文件498行、新模块107行，新增/修改函数非空行不超过50。
- 测试使用backend/.venv；每个pytest进程由subprocess timeout=60硬限制。真实PG为本地独立临时实例127.0.0.1:55461/tg_yunying_test，每例隔离schema。

## Release Gate

- status=pending（等待CI与生产验收）；local_gate=passed，代码与PRD自检完成。
- migration_impact=none；frontend/API=none；worker_impact=AI生成成功/恢复及既有应急原因持久化。
- 未调用额外Provider或Telegram进行测试；生产只读查询，不改Task/Action/Job/账号/目标配置。
- 发布按master→release→GitHub Actions Deploy Production；不打开任何额外诊断drain、账号重试或数据维护开关。
- 上线后独立核对SHA/runtime；按发布时间追踪新结算/无字段溢出/无ready误取消，再按Task→ledger→Action→Attempt→typed事实核对。现有准入、远端不可见、未知结果和日目标缺口不能凭本地测试消除。

- release_owner/rollback_owner: 当前任务；rollback_plan: 无新增迁移，出现补丁回归时按正式Actions执行兼容前向修复，不手工恢复数据库或重放历史unknown。
- observe_window: 以本次Deploy实际完成时间为锚点，观察新生成结算和定向typed远端事实；低频路径如未发生则明确unproven。
- artifact_audit: master基线af602f73相对origin/master 972597a4的11个待推提交，最终树差异仅27份文档/历史证据，backend/frontend/deploy工作树一致；本次新增应用改动仅上述生成链。主工作区3个未跟踪文档保留。
- 静态验证：全部修改/新增Python AST解析通过；git diff --check通过。前端与迁移无变化，完整前端构建交由Prepare Production验证。
- 最终关联回归：恢复fencing、reconcile/acceptance、Provider unknown、窗口接管、generation worker共51项通过（10.68s）。

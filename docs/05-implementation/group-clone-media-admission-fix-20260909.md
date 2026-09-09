# 群克隆媒体准入修复与 Release Gate（2026-09-09）

## Intake / Bug Batch Plan

- intake_id：intake-20260909-group-clone-audit；L3/P1；用户已明确“你来修复问题”。
- 范围：独立 group_clone/v2_group_clone 的两个已复现缺陷，媒体规则拒绝失效、相册非首项内容保护失效。原诊断见 `group-clone-audit-20260909.md`。
- 基线：43bb3cbb1d79260448086b67bffeaeb4930866f7；生产基线读回为8fc0197537c6e57b0901e52bc11ca7396f620b7c。仅额外包含 master 已有的一条诊断文档提交。
- 当前 Codex 为单写者及 merge_owner；无并行可写 Agent。原未跟踪入群审批诊断文档保留，不纳入本次提交。

## Product → Dev

先更新专项 PRD §7.1 与本次设计修订，完成原需求、单媒体/相册、空 Caption、输入/输出拒绝、保护标记、实体变换、旧 unknown、UI状态和测试边界自检。design_status=complete/resync=true 指本补丁。

- `sanitize_clone_content` 先执行冻结输入/输出规则；规则通过且媒体 Caption 为空时明确返回允许的空字符串；其他实际拒绝返回 None。
- `materialize_media_items` 保留相册非首项 None，所有项允许后才分配发送 identity；发现拒绝返回 None，由原物化流程将主义务记 filtered。
- `admit_media_events` 对所有相册项执行现有 protected_content 检查，使用原 protected_content 人工审核原因。
- `filter_clone_obligation` 统一过滤终态和 resolved_at，并清除旧等待原因。原配置快照、random_id、Attempt/Gateway、防重复和结算合同不变。
- 更新数据流索引与生产说明。无模块入口、API、模型、worker拓扑变化，因此结构索引不新增模块。

## QA 与审查

- 正式测试初始反例：5 failed / 5 passed，失败分别为输入/输出拒绝的单图、相册首/后项拒绝、相册后项保护。
- 扩展为14个定向用例，覆盖单媒体输入/输出、相册首/后项输入/输出/保护/实体变换，以及空Caption仍受显式输入/输出规则约束。
- 最终群克隆全量定向回归：76 passed，18.15秒。使用backend/.venv，进程硬超时60秒；包含既有合法无Caption、正常双媒体、identity错误、生命周期、API、collector、绑定、顺序与迁移专项。
- 首轮修复暴露共享文本过滤器拒绝空Caption，已在输入/输出规则通过后单独处理合法无文字媒体；不是遇到拒绝自动降级。一次测试fixture导入被静态自动清理，已改为显式模块fixture绑定并重跑，不算生产缺陷。
- 测试替身仅位于Telegram发送边界，正式Planner/Dispatcher可执行；拒绝/保护路径新增发送Action、mutation identity与message mapping均为0，重复规划仍为0。
- F821/F822/F823、新增测试F类、compileall、git diff --check通过。现有materializer的CloneSenderBindingHistory未使用导入是基线已有F401，本补丁未删除无关旧代码。
- 修改生产文件最多495行，修改函数非空行均不超过50。无需前端构建或迁移专项新增；完整CI仍由Prepare Production执行。
- 本地product_accepted：仅这两个缺陷与其回归合同通过。完整Clone交付及生产E4未验收。

## Release Gate

- release_mode：github_actions；release_owner/rollback_owner：当前Codex。
- local_gate：passed；ci_or_build：pending；release_status：pending。
- 路径：master → Prepare Production完整质量检查及镜像 → release快进 → Deploy Production。
- migration_impact：none；worker_impact：同发布版本更新现有worker，不改变角色/并发/资源合同。
- external_platform_impact：本次不创建或启动Clone任务，不执行测试Telegram写操作，不修改线上数据；新规划拒绝载荷不外发。
- rollback_plan：无schema变化；不以回滚恢复有缺陷发送逻辑，优先前向修复。任何回滚仍需新鲜生产状态核对，不重放unknown。
- observe_window：部署完成时间为锚点；独立核对current/backend/worker SHA、health、CloneTask与领域计数。生产无CloneTask，已向用户请求受控源群/目标群/账号或现有Task ID，未明确前不能做真实写入验收。
- 完整Clone PRD §18.2的历史交付缺口不在本次两个缺陷补丁内，保持原状态。

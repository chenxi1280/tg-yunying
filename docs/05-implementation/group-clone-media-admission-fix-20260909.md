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

## 用户指定真实测试与第二轮闭环

用户补充真实测试范围：`https://t.me/zzxshxc` → `https://t.me/t01ces`，目标管理员 `@yangyuyan`。只读解析得到source peer=-1003298633687（OperationTarget 2801 / TgGroup 2821）、target peer=-1003987149407（平台尚未登记）；106/437两条平台账号均匹配该username，实时读取均为目标群creator且delete/pin/manage_topics=true。选用437作为控制账号，不把同一Telegram身份的两个平台记录当成两个不同发送人。

运行前置发现：生产8个AuthorizationUpdateState均为gap，last_error为naive/aware datetime比较失败。按项目闭环返回产品阶段，新增专项PRD租约时区修订；修复Ingress写入校验、Clone precheck、start boundary、领域read model四处比较，使用既有as_beijing转换，不改租约时长、不手工重置gap。

- 修复前36项时区反例：24 failed、12 passed，失败均为带时区数据库时间。
- 修复后群克隆回归：112 passed / 31.20秒，包含76项媒体/既有用例及36项租约用例。
- 独立本机PostgreSQL16实例：127.0.0.1:55456，专用tg_yunying_test数据库；完成blank schema迁移到0229，再执行6项真实timestamptz持久化/expire/readback/Ingress测试，6 passed / 8.88秒；测试事务回滚。未连接生产数据库执行单元测试。
- 当前总定向证据为118个不重叠用例；各后端pytest进程硬超时60秒。
- 首候选fcda17d0的Prepare Production 34331833229完整通过，但未Deploy；最终候选合并本轮修复后重新Prepare，不以旧SHA的通过代替新候选检查。
- 用户已授权指定群测试，原“未提供范围”状态更新为“范围已明确，进行前置配置与正式任务链验收”。本次未授权给源群插入测试消息；Clone从启动后自然事件开始，完成后暂停测试任务，保留映射/unknown证据。

## 发布目录复验与测试配置读回

- 最终候选：42627281e22e8d4708e27fd46dcceea161049592；独立master工作树 `/tmp/tgyunying-clone-release-20260909`。主工作区其他AI任务修复未纳入候选。
- 独立目录112项回归全部通过（43 + 41 + 17 + 5 + 6）；较大批次曾触发60秒硬超时，拆分后所有用例通过，无断言失败。生命周期单独5项耗时9.72秒、Sequencer/Ingress6项9.88秒。未放宽60秒限制。
- 本机GitHub HTTPS出现SSL_ERROR_SYSCALL/EOF，使用现有生产SSH的临时SOCKS转发访问GitHub，TLS认证和GitHub Actions发布路径保持不变；未修改全局代理配置。
- master已推送最终候选，Prepare Production 34334139596进行中。
- 用户指定目标经实时resolve及creator权限复核后，用现有 `_upsert_group_target_from_snapshot` 仅登记该群：OperationTarget=6106、TgGroup=6167、账号437关联=37791；独立读回can_send=true。使用现有目标账号策略服务把可发言标签更新为实时确认的群主，保留业务审计。
- 使用现有规则服务创建专用规则集2/v1/published，限定group_clone，无内容改写。源内容仍受Clone自身protected/unsupported/entities准入约束。
- 本次配置审计actor前缀为 `Codex/user-authorized-clone-test-20260909`，审批来源为本任务用户指定源/目标和管理员；未修改其他群或账号关系。
- 预检的source/target均resolved=true；剩余hard blocks仅为旧版共享Ingress无live owner/lease。预检已提交授权状态供正常Collector领取，未手工修正gap或PTS。
- 发送池只有指定管理员的一个Telegram身份；多源发言人会出现waiting_binding，不能当作完整多账号克隆验收。本次目标先验证真实正式链路，观察到结果后暂停任务并保留事实。

## CI 测试隔离修复

Prepare 34334139596的两个PostgreSQL分片各有1项失败，均为新增租约测试向已有public schema自动插入Tenant时撞上预置id=1；普通分片、前端与镜像通过。修复仅把新增测试切换到仓库现有isolated_postgres/database夹具，每项使用独立schema及其序列，避免公共数据/执行顺序依赖；生产代码和验收合同不变。

本机专用PostgreSQL中先写入public Tenant(id=1)，再运行修订后6项租约持久化/读回测试：6 passed / 9.55秒；保留真实PostgreSQL及60秒硬超时，没有mock数据库。修复后提交新候选并重新执行完整Prepare。

## 最终候选发布

- candidate_sha：d5af1229ae1f0d4b37f11f7debd058e7fbf7401c。
- Prepare Production 34335284023：全部质量分片及镜像构建success。
- release从8fc01975快进到d5af1229，master/release/dispatch三者SHA一致；未纳入另一个任务的aa670b97/01edd500修复。
- Deploy Production 34336057503：2026-09-09北京时间17:40后触发，已通过candidate/prepared校验；以下以实际结束时间和独立线上读回为最终锚点。

## d5af1229 线上读回与第三轮闭环

Deploy 34336057503于2026-09-09 17:43:58北京时间success；current=/data/tgyunying/releases/20260909094124_d5af1229。backend+18 workers完整SHA一致且healthy；本机/public API健康200，Alembic head=0229_admission_gap_count。

正式listener仍有独立调度限制：最近一轮305529ms，超过共享授权90秒租约，状态追赶及租约间歇失效。为用户指定测试，使用正式Collector的原claim/fencing/_drain_claim路径，仅驱动authorization 1/2398；不改PTS、不强制领取未过期lease、不重放unknown，不调用其他任务的reconcile。审计动作“运行受控克隆Collector验收”。4轮后两授权live，正式precheck passed。

随后用现有create_and_start服务创建唯一测试Task ce341c64-878e-482a-8b91-598346d5b885，epoch=1。正常Planner将其置failed/start_failed，last_error=planner_remote_io_forbidden；SourceEvent/obligation/Action/Attempt/RemoteFact/mapping全部0。受控Collector检测Task失败后自动退出；测试Task保持failed及原始错误，不伪装暂停成功，无远端消息可回滚。

根因：_activate_pending_tasks在planner runtime role内调用advance_group_clone_start→fetch_raw_channel_boundary，违反既有Planner IO禁令。按标准闭环返回产品阶段，更新专项PRD和两索引；新增group_clone_start_worker在listener Collector后按Task独立事务执行boundary、running及PlannerWake，Planner跳过pending Clone。保留原权限/租约/远端禁令、失败语义和起始边界。

修复前真实runtime role回归：1 failed，复现planner_remote_io_forbidden。修复后群克隆回归120 passed /21.74秒，含新增8项启动阶段、正常listener调用链、幂等、scope和失败用例。ruff F/静态未定义名与diff-check通过。一次额外role-drains选择触发测试库安全检查而拒绝非测试库名，未重置或写入该数据库；随后只运行正确标记的Clone测试。新代码是两个阶段调用点和独立启动模块，未扩大拆分现有巨大service/listener文件。

下一次发布后需由通用Start重新复核该失败任务；它仍是零发送、旧epoch安全启动记录，不授权重放unknown。共享listener长周期的问题仍独立unproven，受控Collector结果不代表常态调度恢复。

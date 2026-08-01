# Progress

## Session: 2026-08-01

### Phase 1: Intake 与真相源核对
- **Status:** completed
- Actions taken:
  - 读取 planning-with-files 与中文文档技能；中文文档技能为占位模板。
  - 读取 feature design 索引和现有专项 PRD格式。
  - 核对主 PRD、全任务履约 PRD、数据流索引和生产运行规则中的调度/锁序/搜索合同。
  - 确认现有产品设计已经定义正确不变量，本次专项负责把生产偏差、存量迁移和 Release Gate 收口为可交接设计。

### Phase 2: 专项 PRD 编写
- **Status:** completed
- Actions taken:
  - 确定采用独立 L3 专项 PRD，并引用既有总 PRD而不复制全部历史设计。
  - 冻结 P0 范围：唯一拓扑、容量守恒、锁序、AI 存量接管；任务级恢复与搜索安全容量作为 P1/E4。
  - 新建 `shared-dispatch-and-ai-fulfillment-recovery-prd.md`，完成 Intake、目标/非目标、详细设计、数据/配置、迁移、QA、Release Gate、PDC 和 Product Handoff。

### Phase 3: 真相源同步
- **Status:** completed
- Actions taken:
  - 更新 feature design README 索引。
  - 在主 PRD增加 2026-08-01 resync。
  - 在全任务履约 PRD增加 §10.2 专项交接引用。
  - 在数据流索引新增 DF-200。

### Phase 4: 设计审查与交付
- **Status:** completed
- Actions taken:
  - 通读专项 PRD，确认所有章节内容完整且未进入代码实现。
  - 进入链接、格式、需求覆盖和工作区边界检查。
  - 发现并修复新数据流 ID 与既有 DF-200 冲突，最终使用唯一 DF-324。
  - 补齐 Telegram Gateway 调用前 `gateway_call_started_at` 持久化边界。
  - 完成格式、引用目标、唯一数据流 ID和需求章节覆盖检查。
- Files created:
  - `.planning/shared_dispatch_repair_prd_20260801/task_plan.md`
  - `.planning/shared_dispatch_repair_prd_20260801/findings.md`
  - `.planning/shared_dispatch_repair_prd_20260801/progress.md`

## Test Results
| Check | Status | Evidence |
| --- | --- | --- |
| scoped planning isolation | pass | did not modify `.planning/.active_plan` |
| markdown diff check | pass | `git diff --check` clean for tracked docs |
| trailing whitespace | pass | no matches in new PRD/planning files |
| dataflow identity | pass | new flow uses unique `DF-324`; no duplicate heading |
| truth-source targets | pass | all referenced repository files exist |
| PDC coverage | pass | Intake, goals/non-goals, topology, capacity, lock order, takeover, task lanes, data/config, migration, QA, release, handoff present |

## Errors
| Error | Resolution |
| --- | --- |
| project-specific PRD skills contain TODO only | follow AGENTS.md and repository truth sources |
| DF-200 duplicated an existing API dataflow ID | inspected all IDs and renamed the incident flow to unused `DF-324` |

### Phase 5: 逆向设计审查
- **Status:** completed
- Scope:
  - 本轮只审查设计，不修改专项 PRD。
  - 以“是否仍可能导致任务不能完成”为判定标准，对照父级 PRD、当前实现和测试缺口。
- Result:
  - 确认 3 个 P0：搜索首次 outcome 所有权冲突、迁移 fence 顺序错误、preview/apply 不可断点续跑。
  - 确认 6 个 P1：post-Gateway 原子性表述不完整、remote reconcile 无状态闭环、物理计数字段未冻结、topology fingerprint 不规范、长期 shard 缺席与关键 QA 缺口。
  - 结论：当前 `design_status=complete` 过早，应在 P0/P1 合同补齐后再恢复 Product Design Complete。

### Phase 6: 审查问题修订
- **Status:** completed
- Acceptance:
  - 3 个 P0、6 个 P1 均能映射到明确设计条款和 QA 场景。
  - 专项 PRD、父级真相源与 DF-324 不再互相冲突。
  - 本轮仍只修改产品/数据流文档，不实现代码。
- Changes:
  - 已修正专项 PRD的搜索首次 outcome 所有权、effective unclaimed 术语和目标架构。
  - 已冻结 topology/capacity canonical fingerprint、shard liveness、stale 阈值和逐 shard 实际预算。
  - 已拆清 B0/B1 事务边界，禁止 claim-free executing 持久空窗。
  - 已增加可断点续跑的 takeover batch/item 状态机和 migration claim gate。
  - 已增加 remote reconcile found/absence-proven/inconclusive 三分支与 CAS/审计合同。
  - 已把发布顺序改为 preparing fenced readiness -> takeover completed -> 原子 active，不再先启动可写 worker。
  - 已补充 search ownership、shard liveness、B1故障注入、takeover crash/drift、remote reconcile和混合公平 QA。
  - 已同步主 PRD、全任务父级 PRD与 DF-324，删除“只有 bound 才受保护”的首次 outcome冲突口径。
  - 复核时补齐普通旧epoch三分支，确保旧 `(1,0)` binding会重新进入当前 `(2,0|1)` demand，而不是被释放后丢失业务义务。
  - 文档格式、引用目标、DF-324唯一性、代码围栏和9项审查合同检查全部通过。

### Phase 7: 实现基线与红测映射
- **Status:** completed
- Scope:
  - 用户授权按专项PRD完成代码修补并检查额外遗漏。
  - 本地实现与QA不等于GitHub Actions、生产runtime或Telegram E4。
- Actions taken:
  - 确认 `release@87fe0bf0` 与现有 dirty worktree 边界，未触碰用户无关改动。
  - 核对现有 dispatch/search/takeover 模型、配置和迁移基线。
  - 发现 Action/Attempt 无可靠行版本、现有 `retry_count` 不能作为 CAS；先回写专项 PRD 与 DF-324，冻结 canonical state hash 输入与比较规则，再进入代码实现。
  - 将 PRD 合同映射到配置/模型/调度/Gateway/takeover/remote/release 文件和测试入口。
  - 运行实现前定向基线：41 passed in 4.27s。

### Phase 8: 数据模型、配置与合同基础
- **Status:** completed
- Scope:
  - 先增加前向迁移和 ORM，不对既有生产事实猜测回填。
  - topology/state hash 全部采用可独立测试的 canonical JSON SHA-256。
  - 实现跨 epoch 分类前发现 allocation 缺可比较合同版本，已先同步专项PRD和DF-324，再扩展迁移。
- Changes:
  - 新增 scope topology/activation字段、Window effective count、ShardAllocation合同版本和三类控制表。
  - 新增0134幂等前向迁移，历史scope保持preparing、历史allocation版本保持空值，不伪造回填。
  - 新增canonical topology/capacity/state hash和生产配置校验；Action/Attempt并发漂移不再依赖retry_count。
  - 新增Stage candidate、shard heartbeat、旧writer检查和原子activation基础服务。
- Verification:
  - 新模型、迁移重放、fingerprint/state hash、activation定向测试通过。

### Phase 9: 调度、容量与搜索守恒
- **Status:** in_progress
- Changes:
  - Window容量决策改读`effective_unclaimed_count`，旧字段只同步兼容审计。
  - 普通旧epoch按current/stale/no-longer-due三分支处理；stale释放旧binding并保持同Action pending重入demand，零搜索exclusion。
  - current-contract旧epoch reservation可继续被claim并冻结真实allocation epoch。
  - live shard预算限制普通allocation；stale shard新reservation为0，搜索candidate排除归属stale shard账号。
  - 搜索首次outcome保护和现有唯一carrier/release-batch链保持不变。
- Verification:
  - 调度/搜索/迁移/activation定向集合75 passed in 5.84s。
- Remaining:
  - 混合高债务公平已用巨量search debt、三个AI父任务及评论/反应/浏览定向通过；仅PostgreSQL并发仍待可用测试库。

### Phase 10: Gateway B0/B1与远端核验
- **Status:** completed_local_postgresql_blocked
- Changes:
  - B0独立冻结Gateway start、request identity及完整请求/目标fingerprint；B1中claim、Action、Attempt和任务专用账本原子终结。
  - Gateway返回后使用独立脱敏journal；send/comment记录remote message，view/reaction记录类型化remote fact，B1回滚后可重建业务事实。
  - remote reconcile按confirmed/absence-proven/inconclusive/conflict CAS收口；AI权威no-mutation恢复原槽和memory，unknown不写stance。
- Verification:
  - Gateway/remote/channel定向集合41 passed。
  - PostgreSQL独立journal测试已编写，但测试库连接在收集前被服务端关闭，未执行。

### Phase 11: AI存量接管与发布fence
- **Status:** completed_local
- Changes:
  - 实现持久batch/item/cursor、classification/state hash、chunk apply、crash resume、supersede chain和claim hard gate。
  - 发布编排固定为旧writer fence -> migration/stage -> fenced readiness -> ledger/takeover -> activate -> verify-active。
  - candidate shard检查忽略越界的stale旧shard历史，但新鲜旧writer仍阻断激活。
- Verification:
  - takeover/runtime/release/no_postgres组通过；部署Shell、workflow YAML和脚本顺序静态检查通过。

### Phase 12: 文档与最终本地QA
- **Status:** completed_local_postgresql_blocked
- Changes:
  - 同步专项PRD状态、DF-324、结构索引和PRODUCTION_RUNTIME。
  - 发现并修正`.env.production.example`遗留`DISPATCHER_SCOPE_CAPACITY=52`，补齐26/2和fingerprint/contract配置，并增加回归测试。
- Verification:
  - 三组不重叠定向集合共207 passed；模板补充测试另增1项，合计208项本地定向验收通过。
  - frontend正式build通过；Python compileall、YAML、Shell、AST尺寸和`git diff --check`通过。
  - PostgreSQL、Actions、生产runtime、Telegram canary和自然日E4未证明。

### Phase 14: 完成性反向审计
- **Status:** completed_findings_block_release
- Scope:
  - 用户要求继续检查是否还有遗漏问题会导致任务无法完成。
  - 本轮只读审查，不修改专项PRD或代码；结论按P0/P1/P2和pass/blocked/unproven分层。
- Audit matrix:
  - preparing全writer fence；B0/journal/B1/recovery崩溃窗；五类remote fact收口；takeover chain/CAS；跨epoch/search所有权；shard liveness；迁移/配置/发布；PostgreSQL专属语义与测试盲区。
- Findings so far:
  - 已确认3个P0：群管follow/callback绕过B0/journal、旧worker heartbeat无退役导致Stage A自阻断、新worker drain覆盖合同版本导致verify-active自阻断。
  - 已确认2个P1：membership两套unknown恢复协议冲突、embedded worker环境漂移造成stage前写窗口。
  - 公平分配复核通过：scope cursor跨Window轮转，8轮/60轮测试覆盖，未发现新增饥饿缺陷。
  - 定向回归首次尝试发现当前macOS shell无`timeout`命令；改用Python subprocess timeout执行，不据此改变测试结论。
- Verification:
  - 现有runtime/release/group-bot/remote定向集合34 passed；确认测试盲区而非既有回归失败。
  - 接管batch/CAS、跨epoch守恒、shard恢复、公平游标和迁移静态结构未发现新增确定性阻断。
  - 本轮只读审计没有修改专项PRD或业务代码；3个P0未修复前，本地候选状态必须保持blocked_release。

### Phase 15: 审计缺陷修补
- **Status:** local_complete_postgresql_and_release_blocked
- Scope:
  - 用户已明确授权修复Phase 14确认的3个P0和2个P1。
  - 先更新PRD/数据流合同，再以红测驱动最小实现；不发布、不伪造生产E4。
- Initial decisions:
  - heartbeat退役写`stopped`保留历史；metadata在统一heartbeat层合并。
  - membership unknown只通过RemoteReconcileCase CAS闭环。
- Product resync:
  - 已更新专项PRD状态、B0/B1全量mutation、membership/group-bot类型化remote fact、worker退役与metadata合并、embedded fail-closed和对应QA。
  - 已同步父级主PRD与DF-324；代码实现从该合同继续。
- Red tests:
  - 新增`test_completion_audit_repairs.py`覆盖群管B0/journal与remote fact重放、heartbeat合并/退役、worker finally、membership case和embedded/release合同。
  - 首次执行按预期在collection阶段失败：`retire_worker_heartbeat`尚不存在，证明测试先于实现。
- Implementation in progress:
  - heartbeat已增加metadata merge、精确worker stopped；worker finally显式退役。
  - 发布控制已增加受审批`retire-stopped-writers`、compose稳定worker ID和stage前调用；生产Settings/docker-env拒绝embedded worker。
  - remote case已增加类型化fact和membership/group-bot专用confirmed投影；两个群管Gateway入口已接B0/Attempt/journal，Telethon结果补mutation状态。
- Test iteration:
  - 首轮实现后8项中7项通过；membership case失败来自测试fixture没有TelegramDeveloperApp，尚未进入被测probe/CAS逻辑。
  - 测试改为注入credentials resolver，不修改生产行为或添加fallback。
  - membership测试随后暴露B1前`session.refresh()`会覆盖尚未提交的case终态；改为`flush()`保持同事务原子提交，Phase 15新增8项现已全部通过（`8 passed in 1.91s`）。
  - 首次合并跑旧回归集时，整文件选择`test_task_center_role_drains.py`带入未标记的PostgreSQL集成测试；测试库`172.28.232.109:5432`在schema reset前关闭连接，collection阶段中止。已确认其余所选文件均为`no_postgres`，后续拆分执行，不能把数据库阻断计为代码失败或通过。
  - 拆分后9个全局`no_postgres`旧回归文件共`88 passed`；角色drain文件的20个`no_postgres`节点有18项通过、2项因测试替身仍为两参数签名而失败，生产调用点现有合同为`dispatch_action(..., project_task_stats=False)`。该调用点来自Phase 10事务C投影改造，不是Phase 15新改动；需只同步测试替身签名，不改变生产行为。
  - 已同步两处旧测试double接收关键字参数，未改生产事务边界。
  - 自审发现并修正首版claim CAS过宽：通用remote case恢复完整Action hash；membership只有持有精确`RecoveryClaim.token`且移除该token后匹配旧expected时，才把case推进到含当前claim的完整hash。
  - 补充callback类型事实重放、真实planner task heartbeat、worker重启清除stop标记、membership inconclusive后新精确claim重试、批量退役cutoff/角色边界及docker-env embedded拒绝断言。
  - 新的inconclusive重试测试进一步发现：case在写入冷却元数据后仍冻结旧recovery token，外层释放claim却未刷新expected hash，下一轮精确claim仍会被误判漂移。已在外层释放本次claim后同步刷新inconclusive case hash；两处旧测试double也已全部修正。
  - Phase 15新增/activation/release/角色drain合并定向集现为`40 passed, 9 deselected in 4.09s`。
  - 按硬指标完成职责拆分：类型化remote fact投影移入独立模块，core reconciliation降至462行；membership、群管follow和release CLI新增/触碰函数均降至50行内并通过compileall。worker主循环首次拆分后仍为54行，继续抽取循环体后再验收。
  - worker循环体已进一步抽取；核心40项在重构后保持`40 passed`。
  - 扩大到20个相关`no_postgres`文件后为`129 passed, 5 failed`；5项全部是存量membership unknown没有ExecutionAttempt，统一case准备在Gateway probe前明确拒绝，导致旧回归的probe/status断言未发生。这不是放宽CAS的理由，而是存量数据需要显式的read-only recovery Attempt/Case升级路径。
  - 已先把存量无Attempt升级合同同步到专项PRD、父PRD与DF-324；实现只为membership unknown建立带`legacy_unknown_read_only_recovery`标记、冻结identity/payload的result-unknown Attempt，随后仍经统一Case CAS。
  - 新增存量升级定向测试；原5项backpressure/cooldown失败连同Phase 15测试现为`17 passed in 2.76s`。
  - 20个相关`no_postgres`文件复跑现为`135 passed, 5 warnings in 7.78s`。
  - membership evidence新增`require_send`语义并补Gateway测试；首次把整个`test_ai_gateway.py`并入定向集时因该文件含未标记的PostgreSQL测试再次在schema reset前被同一不可用测试库阻断。两条纯Gateway probe测试本身不访问数据库，已补`no_postgres` marker后按node执行；不把数据库collection阻断算作行为失败。
  - Gateway/membership/AI probe相关`no_postgres`集合为`72 passed, 28 deselected in 4.19s`。
  - 当前实际触碰的全部新增/扩展函数均不超过50行；`remote_reconciliation.py=462`、类型投影新模块146行、新测试文件483行；纳秒cutoff可被Python ISO parser正确解析。
  - 结构索引与PRODUCTION_RUNTIME已同步Phase 15模块、命令、退役顺序和证据语义。
  - 首轮静态闸门通过：`git diff --check`、Shell语法、Python compileall、Actions/compose YAML解析均pass；工作区未安装ruff，因此没有伪报ruff通过。
  - 最终广集首轮因两处Phase 15测试double未接收新增`require_send`关键字而为`133 passed, 2 failed`；已只修测试签名，未回退生产接口。
  - 入口一致性复核补齐stale-executing释放claim后的case hash刷新，并在shadow hash不匹配时probe前把case置conflict。新增漂移测试首次还暴露凭据分配发生在CAS之前；已把case准备/冲突隔离前移到凭据解析之前，确保漂移零Telegram调用且不被缺开发者应用遮蔽。Phase 15专项现为`13 passed in 2.12s`。
  - 最终相关广集为`136 passed, 5 warnings in 8.07s`；角色drain+Telethon权限+AI Gateway纯单元集为`60 passed, 37 deselected in 3.69s`。
  - 最终静态闸门仅发现新增专项测试文件增长到508行，超过500行硬限制；业务代码/Shell/YAML/compile/diff检查未在该轮报错。将按worker/release与remote/membership职责拆分测试文件后重跑，不降低阈值。
  - 专项测试已拆为remote/membership与worker/release两文件，合计`13 passed`且两文件均低于500行。
  - 第二轮指标闸门发现Telethon权限probe复杂度12、抽取后的worker loop复杂度15；两者行为测试通过但仍超过10，继续按“解析/权限判断”和“循环/单次drain/等待”拆分，不豁免硬指标。
  - 两处复杂函数已完成职责拆分；worker/Gateway定向集`70 passed, 28 deselected`，最终diff/Shell/compile/YAML/文件大小/函数长度/复杂度闸门全部通过。
  - Phase 15本地实现与复审完成；PostgreSQL、Actions、生产发布/runtime、Telegram canary和自然日E4保持blocked/unproven，不写product accepted或production fixed。

### Phase 16: 生产发布与 E4 恢复验收
- **Status:** in_progress_release_scope_audit
- User authorization:
  - 用户已明确要求按流程部署到线上并持续观察，只有修复问题且任务完成才算结束。
- Release-scope observations:
  - 当前分支为`release`，HEAD与`origin/release`一致，修复改动尚未提交。
  - `.planning/.active_plan`仍指向并行的`two_update_production_release`；不覆盖该全局指针，本专项使用显式计划路径记录发布证据。
  - 工作区同时存在共享调度修复与OCR、AI话术、production freeze等并行计划；提交前必须逐文件核准范围，不能整工作区盲目发布。
  - Deploy Production在`release` push和手工dispatch时触发，Actions包含PostgreSQL与no_postgres两组后端测试、前端构建、镜像构建和后续部署。
- Skill note:
  - `production-actions-release`与`tg-yunying-release-recovery`当前均为空模板；实际闸门以AGENTS.md、workflow和PRODUCTION_RUNTIME为准。
- Scope decision:
  - 纳入共享调度/远端核验代码、0134迁移、发布配置/脚本、相关测试、专项PRD/索引/runtime、原始生产根因计划与prod-diagnosis证据、本专项计划。
  - 排除`.planning/.active_plan`、AI话术评估记录删除、早期稳定基线文件以及OCR/AI humanization等其他未跟踪计划目录。
  - 因`origin/master`是`origin/release`祖先且release已有两次生产合并，最终候选先推master、后推release，不能从落后的master重新合并而回退线上树。
- Pre-release verification:
  - `git diff --check`、compose/deploy Shell语法、Python compileall、workflow/compose YAML解析通过。
  - 本次变动的27个测试文件`no_postgres`分区：`184 passed, 38 deselected in 10.46s`，未触发60秒硬超时。
  - PostgreSQL分区尚待GitHub Actions的Postgres 16门；在其通过前部署job不会运行。
  - 首次commit前`git diff --cached --check`发现专项测试文件末尾多一空行，提交未产生；删除该空行后专项`13 passed in 2.07s`并重新通过diff gate。
  - 已形成候选提交`28b7daeb76973967e33b43d6a244ca75e006b195`（`fix(dispatch): recover shared task fulfillment`）。
  - 远端fetch确认无并发推进，候选同时为`origin/master`和`origin/release`的快进后继。
  - 已先推`master: d2dbd9d7 -> 28b7daeb`，再推`release: 87fe0bf0 -> 28b7daeb`触发Deploy Production。
  - Actions run `30690191293`：frontend pass；`backend-checks (no_postgres)`为`2544 passed, 18 failed, 785 deselected`，deploy未运行。失败按旧测试未进入active合同、stale recovery签名漂移、迁移head断言和dispatch终结语义分组；进入CI修复循环。
  - no-PG 18条已按新合同同步且本地`18 passed in 3.98s`；生产fence未放宽。
  - 同一run PostgreSQL分区为`750 passed, 19 failed, 14 skipped, 2 xfailed`；开始按FK清理、父行fixture、scope、test double和远端mutation证据分组修复。
  - PG失败复核新增真实遗漏：已有旧Attempt但缺冻结request identity时Recovery抛错。已先同步专项PRD/总PRD/DF-324，再实现保留旧Attempt、追加带source id的read-only recovery Attempt；新增回归后专项`14 passed in 2.19s`。
  - 其余PG失败均按真实合同同步：journal FK清理顺序、父子fixture flush、旧AI scope、dispatch test double关键字、已知permission no-mutation与view/reaction成功mutation事实。
  - 第二轮扩大本地no-PG集合`264 passed, 381 deselected in 26.61s`；diff/Shell/compile和新增函数长度/分支闸门通过。
  - 形成CI修复提交`a01aef76b7a176c188ec6bf6f6abf15754867351`；远端无并发变化后先推master、再推release，开始第二轮Deploy Production。
  - 第二轮Actions `30690970723`：no-PG通过，PG剩6条，build/deploy被阻断；已定位B0冻结snapshot被延期result覆盖、view/reaction在autoflush=false下双写RemoteFact，以及legacy membership旧断言三组根因。
  - 已先resync专项PRD、父PRD与DF-324，再按冻结快照保持和B1单点远端事实合同修补代码与回归。
  - 评论membership独立SQLite复现由`gateway_request_identity_missing`转为pass；旧membership Attempt保留/新Attempt成功的回归pass；journal与remote reconciliation单元集`10 passed`。
  - 通过真实`SessionLocal(autoflush=false)`手动执行第二轮PG失败的4个workflow节点，频道浏览/点赞/评论组合、like reset、view reset、失败后重规划均pass，未再产生RemoteFact双INSERT。
  - 定向可归因集合`40 passed in 3.00s`，触碰函数长度、diff、compile和发布Shell语法闸门通过；形成提交`7ed14aceeca862716927062ac2226c10018fb858`并依次快进master/release。
  - 第三轮Deploy Production run `30691867621`已由release push触发，开始监控CI、镜像和部署阶段。
  - 第三轮两组后端、前端和三镜像全部success；生产stage后新容器healthy、两Dispatcher分片live且candidate preparing。
  - deploy卡在`reconcile-ledger`全历史N+1；线上量级为7,971/62,405/320,819，active claim已为0但preparing fence持续。已先补PRD/主PRD/DF-324的激活范围，再实现live Window完整守恒与closed active投影批量修复。
  - 新增关闭Window回归证明只清零Window/Allocation active投影、不改历史Reservation；共享发布相关定向集合`37 passed in 2.72s`，compile、Shell、diff及生产函数长度/参数闸门通过。
  - 形成提交`8bad93a1a635e794ad08757893df868e450bb5aa`并推送master/release；run `30693118550`的frontend、no-PG、PG和三镜像通过，但deploy三次均以`TypeError: can't compare offset-naive and offset-aware datetimes`失败，current未切换且合同保持preparing。
  - 先补PRD/父PRD/DF-324的数据库分类与dirty Session原子校验口径；实现SQL布尔分类、同事务显式flush，并以`autoflush=false + expire_all`新增回归，定向`4 passed`。
  - 形成提交`5181f4be8b9b61790fc8dd2227ebd46730790fe9`并推master/release；run `30693755713`全部CI/镜像通过，生产账本收敛由30多分钟降至不足20秒，但AI scope全历史22,469 item apply在13,200条处被唯一错误quarantine阻断。
  - 已取消该run的重复安装；先补PRD/父PRD/DF-324，再实现open+unknown候选、invalid pre-Gateway replan、apply显式flush与生产同款autoflush=false测试会话；相关集合`70 passed in 5.22s`。
## 2026-08-01 生产 E4 继续修复：过期 Window release

- GitHub run `30694664419` 全部成功，生产 current 切到 `20260801100409_fdbadbb3`；所有核心容器 healthy，迁移为 `0134_shared_dispatch`，合同 `active_verified`，scope 26、两个 shard 各 13。
- 最终 takeover batch `7bab7ee1-ef62-4d65-9372-583bb0a05963` completed：processed 755、applied 1、noop 754、conflict/quarantine 0。
- 业务账本显示发布后新建 283 条 AI pending Action但没有 Attempt；Dispatcher 日志证明遗留搜索 release 的 effective 二次扣减整轮阻塞 claim。
- 已先同步 PRD/主 PRD/DF-324，再补红测并修改 `dispatch_release_wave.py`；定向集合 `75 passed in 9.26s`，`git diff --check`与编译通过。
- 下一门：提交并再次走 master -> release -> Deploy Production，随后验证异常归零和真实 Attempt/remote fact 增长。

## 2026-08-01 生产 E4 继续修复：shard heartbeat 时区

- 过期 Window 修复提交 `9650cd5e` 的 run `30695497470` 已完整通过并部署到 `20260801102838_9650cd5e`。
- 原 assignment 已为 `released/search_assignment_expired`，历史 unclaimed 从27降到26、effective保持0，未建 rebuild wave；对应异常不再出现。
- 新阻断为 allocator 把两个真实 live shard 误判 stale：无时区北京时间被强行标成 UTC，导致预算 `{0:0,1:0}`，所有任务继续零 Reservation/零 Attempt。
- 已先更新专项PRD、主PRD和DF-324；新增生产同形态红测，随后以统一时间兼容helper做最小修复，未扩大stale阈值。
- 红测已转绿；共享runtime与调度相关定向集合`55 passed in 4.95s`，`git diff --check`和编译通过。
- 下一门：提交并再次走 master -> release -> Deploy Production，随后验证预算13+13、Reservation/Attempt和真实Telegram事实。

## 2026-08-01 生产 E4 继续修复：post-deploy跨窗active验证

- 时区提交`65085c66`的run`30696275205`完整通过并部署；后继release`7851ee80`确认以该提交为祖先。
- 后继run内部release verify通过，23秒后的GitHub post-deploy verify因`closed_window_active`失败；这同时给出Dispatcher已恢复真实active claim的生产证据。
- 已取消注定在同一旧合同上失败的排队诊断run`30696967280`，避免重复部署；未取消他人并行release。
- 已先补专项PRD、主PRD与DF-324，冻结preparing/fence与active运行期的不同closed Window语义。
- 跨窗合法、错账失败和顶层`verify-active`生产同形态测试已通过；共享runtime/activation/claim/release/跨epoch/搜索释放集合`57 passed in 5.28s`。
- 运行期验证已拆入独立模块，Scope锁只存在于post-deploy只读验证，不进入常驻writer gate；激活前零active校验保持不变。
- 下一门：完整再发布；之后继续取Attempt/远端事实。

## 2026-08-01 生产 E4 继续修复：Scope identity map 刷新

- `7cf4cf52`完整质量门、发布脚本、接管和内部校验通过；run `30697835240` 的外层 `verify-active` 报 `scope_active_projection`，因此没有声明部署闭环。
- 代码审计确认同一 Session 的 candidate 无锁读取先缓存 Scope，随后 `FOR UPDATE` 未强制刷新；并发 claim/release 后会把旧 Scope 计数与新 Action 集合拼成混合快照。
- 已先更新专项 PRD、主 PRD、DF-324 与结构索引；生产同形态红测先复现同一 `scope_active_projection`，加锁查询强制 `populate_existing` 后转绿。
- 共享调度、激活、claim/release、迁移与 Release Gate 定向集合 `70 passed in 5.29s`；未增加自动对账、重试或校验放宽。

## 2026-08-01 生产 E4 继续修复：pre-Gateway 状态/claim 原子性

- run `30698894709` attempt 2 完整成功，release `20260801121801_4c8a95e1` 上线；容器健康，两个 shard live 13+13，内外两次 `verify-active=active_verified`。
- 同 SHA 诊断 run `30699626576` 再发布时，内部校验通过约25秒后外层报 `scope_active_projection`；这证明 identity map 刷新修复有效但仍存在真实写入窗口。
- 代码审计定位 pre-Gateway 门禁/限流/准入分支先提交 `pending|skipped|failed` Action，随后外层才释放 active claim。已先回写 PRD/DF-324；commit 边界红测稳定捕获 `('failed', true)` 中间态。
- 删除8个阻断/延后分支的提前 commit，由外层 finalize 同事务提交终态与完整 claim 释放；Gateway-started、Attempt journal、仍为 executing 的提交边界保持不变。
- 红测转绿；出站/门禁/群管/频道/容量定向集合 `137 passed, 74 deselected`，共享 claim/激活/runtime/release 集合 `57 passed`，compile 与 diff check 通过。本地 PostgreSQL 地址在 collection 前断连，未计作通过，交由 GitHub Actions 服务容器验证。
- run `30700451808` 的前端、no-postgres、PostgreSQL、三镜像、生产发布和 takeover 均通过；外层验证不再报 Scope 错位，而报 Window/Allocation 层 `runtime_active_projection`。
- 新根因是生产 `autoflush=false` 下 release 在终态未 flush 时重算 active Action，继续读取数据库旧 executing 行并保留下层 active 投影；已先补齐 Phase 22 产品/数据流合同。
- 同一 pre-Gateway 原子测试改用生产 `autoflush=false` 后稳定红出 `scope.active_claim_count=1`；锁完整账本后显式 `flush([action])` 再投影，红测转绿且失败回滚仍保持原子性。
- claim/激活/runtime/Release Gate 集合 `60 passed`；出站/门禁/群管/频道/容量/搜索释放集合 `150 passed, 74 deselected`；compile 与 diff check 通过。
- run `30701189759` 完整质量门、发布、takeover 与内部 verify 通过；外部 verify 不再报 active 投影，而在 `21:24:00.4` 精确报 `closed_window_effective_unclaimed`。
- 该失败是只读校验要求时钟自动写数据库的合同错误；已明确 closed 后逻辑 effective 按 bucket_end 为0、stored unclaimed 只作历史，active 与 live 守恒不放宽。
- 生产同形态红测保留 closed Window 的历史 unclaimed/effective=1，同时保持 active binding 完全一致；修复前稳定报同一错误，按时间派生 logical effective 后转绿。
- 共享 runtime/claim/release/Release Gate 集合 `60 passed`，跨窗/search release语义集合 `28 passed`，compile 与 diff check 通过。

## 2026-08-01 生产 E4 继续修复：每日履约诊断漂移

- `be67adcb` 的 run `30701873412` 完整成功并上线；同 SHA 诊断 run `30702343448` 再次通过 release、takeover 与共享调度验证。
- 诊断在 retired hard-hourly 私有符号上失败，AI 每日质量步骤被跳过；该失败不能归因为任务执行失败，也不能声明业务通过。
- 已先补齐 PRD/DF-325/runtime/结构索引，下一步用公开 Planner drain 与五任务类型化 E4 脚本替换旧诊断。
- workflow 红测先失败于旧描述/旧私有入口；实现后 retired wake/due 符号为 0，Planner 非零退出不再被吞掉。
- 新增 E4 只读闸门，按 AI 群日、search click、channel view 三类权威事实核对五个事故 Task；专项与现有诊断集合 `191 passed`，YAML、AST、diff gate 通过。

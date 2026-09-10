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

- status=passed（发布层）；local_gate=passed；业务层待下节独立回读，不能从Release Gate推导整批production_fixed。
- migration_impact=none；frontend/API=none；worker_impact=AI生成成功/恢复及既有应急原因持久化。
- 未调用额外Provider或Telegram进行测试；生产只读查询，不改Task/Action/Job/账号/目标配置。
- 发布按master→release→GitHub Actions Deploy Production；不打开任何额外诊断drain、账号重试或数据维护开关。
- 上线后独立核对SHA/runtime；按发布时间追踪新结算/无字段溢出/无ready误取消，再按Task→ledger→Action→Attempt→typed事实核对。现有准入、远端不可见、未知结果和日目标缺口不能凭本地测试消除。

- release_owner/rollback_owner: 当前任务；rollback_plan: 无新增迁移，出现补丁回归时按正式Actions执行兼容前向修复，不手工恢复数据库或重放历史unknown。
- observe_window: 以本次Deploy实际完成时间为锚点，观察新生成结算和定向typed远端事实；低频路径如未发生则明确unproven。
- artifact_audit: master基线af602f73相对origin/master 972597a4的11个待推提交，最终树差异仅27份文档/历史证据，backend/frontend/deploy工作树一致；本次新增应用改动仅上述生成链。主工作区3个未跟踪文档保留。
- 静态验证：全部修改/新增Python AST解析通过；git diff --check通过。前端与迁移无变化，完整前端构建交由Prepare Production验证。
- 最终关联回归：恢复fencing、reconcile/acceptance、Provider unknown、窗口接管、generation worker共51项通过（10.68s）。

## 正式发布与独立验收

- candidate_sha/deployed_sha: `e62a0d8073f0f94b631d796b3b36ff83c0b89f9d`。
- master→release按完整SHA快进，GitHub Actions [Prepare Production 34423024405](https://github.com/chenxi1280/tg-yunying/actions/runs/34423024405) / [Deploy Production 34423630957](https://github.com/chenxi1280/tg-yunying/actions/runs/34423630957)均success。
- CI：6个SQLite分片、2个PostgreSQL分片累计7776 passed、14 skipped、2 xfailed；前端检查与3类镜像构建通过。
- release_live_at=`2026-09-10T09:02:52+08:00`，取deploy job实际完成时间。后续事实同时要求Gateway开始与typed观察时间不早于此锚点。
- 独立current：`/data/tgyunying/releases/20260910010011_e62a0d80`；backend和18个worker均running/healthy，完整RELEASE_SHA一致。本地API、宿主Nginx与公网API health均ok。
- 数据库当前head=`0231_ai_group_emergency_history`，与候选源码head一致；相对原生产972597a4无迁移文件差异。本次未运行任何存量apply或额外Provider/Telegram测试调用。
- 发布前快照10个running AI任务均无本补丁新结算/应急证据字段；保留同范围发布后回读，明确区分旧事实与新事实。
- 09:05:57第二次只读快照：成都任务新增`topic_only_topic_evidence_missing`完整原因记录，原33字符原因已在真实生产成功持久化；新Job failed并保留emergency_pending阶段，后续业务动作仍按既有合同处理。该快照发布后typed消息1条，尚无新原子结算receipt对应消息，G1线上完成当时仍unproven。


### 09:10:33最终只读快照

- 新原子结算receipt共4份，4个Job均ready；其中1个完整链路已取得新typed消息，candidate hash一致。G1普通成功结算已获线上E4；过期ready恢复路径本观察窗未自然触发，保持unproven，不主动制造过期或重放旧工作。
- 完整链路：Task `8d64449d-994e-4d46-969e-9349f49066ba` → ledger `d4161690-1fcd-42c3-901a-853988c863f3` → coverage `8577158c-b6b1-4da7-b3f2-4c4a6c26c9cd` → GenerationJob `8489fad4-a029-42a7-ad25-6a2a1a87cf3f` ready → Action `fac6aae7-1926-4c4b-b529-a92b44c654ac` success → Attempt `611fccc0-8dde-4d79-887a-02baca35db7c` Gateway于09:08:55.775408开始 → fact `4368a087-9680-4115-83aa-a35dc6f0f324` remote_message_observed于09:08:59.753432确认。
- G2长原因`topic_only_topic_evidence_missing`真实成功持久化2次，Job稳定阶段emergency_pending；全部新格式应急证据9份（Provider route exhausted 5、quality wait 2、缺话题事实2）。该原因存储修复已获线上证据，不意味着这些Job原生成成功。
- G3前置检测与绑定一致性反例、普通聊天、reply/foreign reference边界测试通过；真实不可用配置话题已在前置明确报错并保留完整原因，未伪造话题。有效配置话题成功切换的自然线上样本本窗口未单独取得，保持场景级unproven。
- 发布后10个运行任务合计6条满足原Gateway/typed时间及身份条件的新消息；其中1条对应本补丁新原子结算，不能把6条全部归因于补丁。新Job状态ready 4、failed 9、unknown 2，后两类不计生成成功。
- 当前到期数量投影3597、确认投影293；coverage必达分母14234个Task-account组合，不是唯一账号数。准入、账号资格、Provider/质量等待、历史未知及可见性仍独立存在，整批目标未达成。
- 09:02:52—09:10:25三个generation worker日志实际返回27/26/29行（每容器tail限制3000），未见varchar(32)溢出、ready窗口失效/Job失主/Action状态失配或Task锁冲突错误；这只证明所示观察范围。
- 结论：`release_passed`；G1普通成功结算及G2原因持久化已获新生产证据；未自然触发分支和整批履约保持`production_unproven`，不标记全量`production_fixed`。
- 证据目录：[ai-generation-settlement-20260910](evidence/ai-generation-settlement-20260910/)，含完整CI摘要、Deploy/Prepare身份、部署前后runtime、只读schema、3轮中间及最终业务快照、限量日志计数。
- 原主工作区3份未跟踪文档始终保留；本次独立本地PostgreSQL测试实例已关闭。后续提交仅记录发布证据，不改变已部署应用代码。

# 线上任务推进修复 / Release Gate（2026-09-09）

## Intake 与范围

- intake_id：production-task-progress-repair-20260909；L3/P1。
- 用户先要求根因诊断后明确“你来修复问题”，进一步澄清保留Task，清理历史卡死Action及相关残留、由原任务创建新工作并恢复发送。未授权重放已调用/unknown、改账号或放宽活动窗/审核/15秒合同。
- 原诊断：`production-task-root-cause-diagnosis-20260909.md`。产品合同：`docs/03-feature-designs/production-task-progress-repair-20260909-prd.md`，R1/R3/R4 design_status=complete/resync=true，按prod-diagnosis→product→dev→qa→product→prod-diagnosis闭环。
- 本次限定：评论实际生成路径/异常领取、活群extra配额一致性、精确点赞取消预约积压逻辑退役及新Action物化。R2消息不可见/可信控制不足、R5 Provider unknown、正常窗口外/future工作保持真实状态。
- 未对Task、已发送或未知Action做物理删除；移出旧可执行队列时保留Action/typed未执行事实和审计，释放仅属于未调用旧工作的规划去重键。

## 在线反查

- 17:19生产current仍为8fc0197537c6e57b0901e52bc11ca7396f620b7c。
- 三个点赞Task：a9654a3b-931b-4133-ba63-03b26ece1523（115）、f251b9a2-29d2-40bf-8abe-567eb9179016（79）、f4302d41-d557-403e-8258-429f7bee3084（165），取消预约共359。全体当前epoch，Action无owner/lease/执行时间、Attempt/Journal/FulfillmentRemoteFact为0、Reaction义务pending。
- 后续只读反查359中352来源有效、7过期，source admission/ReactionRemoteFact=0、slot/epoch/message绑定差异=0、Action.result均空。此处为诊断计数；apply必须重新按当前生产SHA和精确IDs生成preview，不能拿本记录代替快照。
- 评论仍有24条executing/generating；R1发布后以正式stale recovery和正常worker观察，不执行无证据SQL重置。

## Dev 与审查

- R1：合法单阶段只冻结realizer，双阶段/V2默认审核要求不变；公共TimingExecutionPath记录实际审核边界。comment worker finally在独立事务Action行锁后核对原owner/token，准备/载荷异常释放领取且仍抛出；不清理新owner资源。
- R3：extra候选LIMIT前批量聚合原Task/day预算与未被预算表示的待发Action，显式flush；原数量槽/目标解析原日，不按扫描日移账、不双扣。执行端仅task_account_portfolio_capacity_exhausted且全历史未调用可沿原§19.68收口，unknown不跳过。
- R4：精确scope→非敏感snapshot/hash→Task/账号/Action/预约/Reaction义务锁后复验→原未执行结算→旧Action skipped。有效义务open/预约脱离旧Action，expired预约missed；正常Planner建新Action。审核同due/同payload物化时发现旧dedupe会返回旧Action，先用真实create_like_action复现失败，再修复仅本批全历史未调用的旧dedupe墓碑迁移，原key入审计。
- 原生领域结算/Planner保留Task配置、任务epoch、原账号/来源、活动窗和期限；无schema/API/frontend/worker拓扑变化。新增CLI不接入自动全库清理，必须显式SHA/IDs/count/actor/reference，重复同preview只返回原receipt。
- 维护不调用Provider/Telegram，不把新Action、CI、runtime或持久化readback称为业务成功。

## QA

- 评论路径/领取、耗时profile、评论dispatch生成：41 passed / 10.68秒。
- 活群未调用收口、extra组合供给与旧候选分页：32 passed / 13.49秒。
- 取消预约清理/重建：21 passed / 5.06秒，包含有效/过期、Task/Action/预约漂移、epoch/owner/Attempt/Journal/typed fact/ReactionFact/unknown保护、SHA/hash/审计、同preview幂等、新owner接管和同due新Action ID。
- PostgreSQL独立schema：3 passed / 10.70秒（后续最终复跑另记），覆盖extra原日槽聚合与事务可见性、真实行锁冲突零写/恢复、并发owner更新拒绝。
- 73项合批曾在断言全部完成后触及60秒进程硬超时，因此不算该进程gate通过；拆成上述41+32两批均退出0，没有放宽超时。最初PG fixture缺少FK插入顺序已修复；并未改生产模型来迁就测试。
- 新增生产代码及测试ruff F类通过，既有修改文件F821/F822/F823通过；compileall、git diff --check通过。新增模块均小于500行，函数非空小于50行，分支复杂度自检≤10。
- local_qa_pass/product_accepted仅针对R1/R3/R4代码与维护合同。整体生产business仍unproven，R2/R5不能据此写已修复。

### 发布等待期间的最终审查

- 首代码提交aa670b976027fcca301a6c00e06a033454736055，parent为Clone的d5af1229；仅推送本任务分支，未推进master/release。
- R1深检增加“Provider-start已经提交，但保存失败状态又报错”的反例：finally原先会把generating降为普通pending。已用真实worker领取入口复现失败，改为先调用既有generation recovery对原Job CAS到unknown，再保留Action provider_result_unknown并释放自己领取；新worker不能重新调用Provider，错误仍向外抛出。
- R1领取/unknown lineage/原身份恢复最终29 passed / 6.42秒；既有评论phases/job/unknown/恢复扩展40 passed / 8.27秒（有重叠，不与其他集合相加）。R4最终21 passed / 5.06秒；PostgreSQL最终3 passed / 9.52秒。
- 新增scope task hash还覆盖config_revision、account/pacing/failure配置、时间区间；readback允许正常新Action接管后按合法时间线前进，但检查原Task/account绑定不漂移。

## Release Gate

### 生产运行记录（执行中）

- 最终候选ffc08a3d97e05ec6fa51092ff946c8d852c6a32d，Prepare34339633174全部通过，Deploy34340363767于2026-09-09 18:31:24完成；独立current/backend+18workers同SHA且healthy，API ok，alembic0229_admission_gap_count(head)。
- R4新runtime预览hash147578db528fcc98609a0b253f0459c9fcd2f4cb18e709a01b9c6e6c67f97185，共359（115/79/165），352有效/7过期。两次apply均在Task锁后、账号锁阶段NOWAIT冲突而退出，未到结算/审计写入；第二份fresh preview与原hash完全相同、全体仍无任何Attempt/typed事实。
- 原清单含225账号；按照同一已发布CLI精确范围合同按账号分批，使用固定母清单、不扩大IDs，子批独立预览/原子apply/读回。成功批不重新预览或重发，仅对未成功锁忙子批复核后继续。审计目录/data/tgyunying/shared/action-backlog-repair-20260909.Du3q0E（0700，产物0600）。
- 18:59独立REPEATABLE READ/READ ONLY合并57份receipt：原359中300已skipped，59仍pending；293有效义务open、7过期closed_expired；三Task仍running、核心配置hash全部不变，新绑定Action=0。部分完成不能称整批完成；未成功部分继续同原清单fresh preview。

### R4 发布后重建链路二次修复

- 18:53真实只读候选复算：当时已释放的228个有效原义务中，176原due尚未来到；其来源点全部为0。同来源其他义务cursor已排至deadline附近/等于deadline，旧Planner将已有冻结位置作为新供给追加其后，形成错误shortfall。此复算仅原候选集，不虚称完整Planner准入或E4。
- 产品合同先resync，真实_create_like_actions回归RED：0 != 1（2.45秒）；修复后原维护退役→原冻结预约复用→新Action ID/原义务绑定GREEN。来源节奏责任拆到channel_like_pacing.py，原channel_like.py缩至437行，未修改无关物化逻辑。来源容量仍经过原apply_source_capacity_plan，账号仍走原reserve/rearm、活动窗/间隔/期限，unknown与expired不重新开放。
- 定向31 passed/6.18秒（新10+原维护21）；相册物化+新回归14 passed/4.01秒；来源owner/cursor/rebooking/账号接管/点赞分配/准入容量51 passed/13.65秒。扩展首命令未显式指定测试PG库被保护拒绝，零测试执行；随后明确127.0.0.1:55432/tg_yunying_test通过，未修改测试数据库保护。全部后端进程60秒硬超时内退出。
- 已审查协同Clone补丁5303df14（路由读查询移到共享订阅锁前）与06680053（autoflush=False释放holder前flush），未在点赞维护CLI运行时部署。新R4候选还须重新完整Prepare，不能复用5303/0668制品。

### 完整Prepare第一轮反查补正

### R4 历史终态别名补正与下一候选

- 4d947aa89cb47624d9c63cf0c2fda54accf678a5完整Prepare34343254785通过，Deploy34343942728于19:13:05完成；独立current/backend+18workers同SHA且healthy、API ok、alembic0229 head。该结果不等于业务恢复。
- 19:20只读反查，原义务绑定的非原清单ID中有3条实际创建于9月6日且已skipped；不得将“不同于359个旧ID”直接称作发布后新建。另两条真实新建分别为19:00:53（早于4d发布）和19:15:42，均尚无Attempt/typed事实。
- 真实维护→Planner→正式未调用结算→Planner回归复现同一历史终态ID被重绑（RED，2.57秒）。产品先resync：沿原义务action_attempt_no生成下一物化序号并进入稳定payload去重；同代幂等、合法下一代新pending，既有payload缺省0且不回填。不修改未知事实/账号消息全局防重/来源容量/活动窗/账号节奏。
- 生产改动仅LikeMessagePayload新增字段与Planner赋值两行；核心41 passed/7.48秒，真实PG与消息payload/内容/reaction扩展14 passed/19.00秒；所有后端进程60秒硬超时内退出。额外验证同代幂等、pending/unknown/confirmed不新建、历史payload解析。
- 本候选合入协同Clone49691885/460953b0：三个update consumer将JOIN裸FOR UPDATE限定到Delivery行，不再持有只读State/Event/Subscription锁；保留同Delivery串行及绑定更新互斥。协同方真实PG旧3失败/新5通过（7.96秒）、相关135通过（23.01秒）、额外真实绑定并发1通过（3.36秒），本任务审查生产diff。与本修复合并后必须重新完整Prepare，不能复用4d制品。

### 完整Prepare历史反查记录

- 01edd500推送master后Prepare `34337268213`：其余7个后端分片、frontend、三个镜像全部通过；no-postgres shard0的既有`test_stale_comment_generation_releases_runtime_reservation_without_overwriting_new_claim`失败，本地单例1 failed/2.92秒复现，未绕过失败发布。
- 根因：R1旧owner不覆盖新DB领取的保护遗漏旧进程本地reservation释放。PRD先resync补正DB owner与进程内reservation身份分离；确认当前领取owner后捕获旧reservation，通过既有identity-checked释放，只清旧对象，不清新对象；进入时已失去owner不捕获任何资源。
- 保留旧回归断言，补充同Action本地reservation替换、入口无旧reservation、处理前已失去owner反例。16 passed/4.47秒；资源生命周期/评论phases/unknown/身份恢复扩展42 passed/6.06秒；全部进程60秒硬超时内退出0。下一候选须重新完整Prepare，不能复用失败run或跨attempt拼接制品。
- 6105a8ab第二轮Prepare `34338079404`全量成功；合入独立Clone任务已审查/120项QA通过的2839c10f后，汇总Prepare `34338653747`暴露既有相册点赞正向fixture时间漂移：`now/消息created_at`固定9月4日，而Task.created_at为执行当天，`task_pacing_anchor`把来源7天窗口压缩为执行日剩余时间，随机Task ID的分布可能产生合法shortfall（CI 4/5、本地3/4或4/5）。本地原测试循环第二次即失败；仅对齐Task创建时间后20个随机Task样本通过。最终将该正向测试的Task/消息/规划统一为同一北京墙上时钟，避免SQLite reload丢tz产生8小时偏移；保留真实节奏/活动窗口、冻结child身份、数量和幂等全部断言，未改生产节奏或放宽容量。

- release_mode：github_actions；release_owner/rollback_owner：本任务Codex。
- source/candidate：当前共享源码基线包含已提交Clone补丁；只stage本任务明确路径，不包含其他任务未提交改动。Clone当前先行发布窗口与本任务错开；不操作其他任务占用的master目录。
- local_gate：passed；ci_or_build：pending；release_status：pending；production_status：unproven。
- 路径：master→Prepare Production（完整测试/镜像）→release快进→GitHub Actions Deploy Production；具体SHA/run和生产锚点在后续记录。
- migration_impact：none；frontend：本次无改动，Prepare仍执行完整frontend检查；worker：更新现有代码，不改进程数量或工作范围。
- rollback：无schema变化，但不以代码回滚撤回已发送效果。R4apply后旧Action已审计退役，保持原receipt前向验证，不能恢复旧dedupe/预约来重放。任务本身保留。
- 发布后独立核对current/backend/workers完整SHA、health/心跳，再按任务逐类读回配置错误、额外量容量、Action/Attempt/typed事实。
- 维护授权：用户当前任务中明确“不是直接删除任务…历史积压的actions…新建新的”。正式部署后使用精确三点赞Task/Action IDs的preview/hash/apply/readback，actor=Codex-production-task-progress-repair，audit_reference=本任务ID及用户授权消息。漂移整批零写，不自动扩大范围。
- E4：352等数量为当时数据，不是承诺成功量；以实际维护后旧Action退出、新Action不同ID/正确原义务绑定及reaction_observed验收；评论/活群各需新remote_message_observed，unknown和发出不可见保持分账。

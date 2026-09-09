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

- release_mode：github_actions；release_owner/rollback_owner：本任务Codex。
- source/candidate：当前共享源码基线包含已提交Clone补丁；只stage本任务明确路径，不包含其他任务未提交改动。Clone当前先行发布窗口与本任务错开；不操作其他任务占用的master目录。
- local_gate：passed；ci_or_build：pending；release_status：pending；production_status：unproven。
- 路径：master→Prepare Production（完整测试/镜像）→release快进→GitHub Actions Deploy Production；具体SHA/run和生产锚点在后续记录。
- migration_impact：none；frontend：本次无改动，Prepare仍执行完整frontend检查；worker：更新现有代码，不改进程数量或工作范围。
- rollback：无schema变化，但不以代码回滚撤回已发送效果。R4apply后旧Action已审计退役，保持原receipt前向验证，不能恢复旧dedupe/预约来重放。任务本身保留。
- 发布后独立核对current/backend/workers完整SHA、health/心跳，再按任务逐类读回配置错误、额外量容量、Action/Attempt/typed事实。
- 维护授权：用户当前任务中明确“不是直接删除任务…历史积压的actions…新建新的”。正式部署后使用精确三点赞Task/Action IDs的preview/hash/apply/readback，actor=Codex-production-task-progress-repair，audit_reference=本任务ID及用户授权消息。漂移整批零写，不自动扩大范围。
- E4：352等数量为当时数据，不是承诺成功量；以实际维护后旧Action退出、新Action不同ID/正确原义务绑定及reaction_observed验收；评论/活群各需新remote_message_observed，unknown和发出不可见保持分账。

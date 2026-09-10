# 任务执行修复与旧滞留清理（2026-09-10）

## Intake 与授权

intake_id=task-execution-repair-20260910；L3；生产相关；需Release Gate。用户授权：删除整个旧滞留运营任务，可暂停所有任务、删除滞留的其他记录后恢复；其他执行问题修复。prod-diagnosis→product→dev→qa→product→prod-diagnosis。

快照17:37–17:41：24个running；评论2项今天Attempt=0；点赞6项今日远端事实=0；浏览5项持续产出但有旧积压；AI10项到期12760/账本861/严格消息866；搜索310/1000。17:51新生产1e9358f8已替代e454c82a，所有操作前重查版本。

## 批量问题与范围

- R1 清理：两个旧评论Task c89f4bd0-9cb2-4e4c-9305-d3bfed34ce1b、d80c0050-7030-4367-af44-6ff2fcc62b5d按正式delete_task入口删除；其他任务只清理原截止已过且未调用的旧执行记录。unknown、已调用、成功事实不是可直接删除的滞留；保留防重身份、审计和必要墓碑。整Task删除与下属清理分别读回。
- R2 过期结算饥饿：直接领取的deadline_rank让全部有效候选优先。线上评论35个全部rank1，持续有效候选让其得不到结算。修复必须使每轮过期结算与有效执行均可推进，不降低有效活动窗口排序，不用放宽截止或重发unknown解决。
- R3 来源/生成截止：核验已有本地3d987665所修通用截止、评论生成结算与本轮现象的匹配；纳入本轮须重新审查测试，历史本地报告不代表已上线。
- R4 活群/搜索：按当前失败码逐项区分代码缺陷与账号、群权限、内容验证等外部事实；发送成功回执不能代替typed可见消息。需继续诊断，design_status=partial；未定位的分组不得盲改。

## R2设计交接

设计状态complete，仅限过期结算饥饿。每轮直接领取先独立选择原截止已耗尽的候选做现有安全结算，再按原有效候选规则领取；两个阶段复用当前batch limit、lane、shard、排除Task及生命周期/租户/预约条件。过期阶段不调用Gateway，锁后重新校验版本与原截止；任意call/unknown沿现有结算证据规则保留。使用独立截止扫描，避免session优先级导致过期候选永远不被处理；过期任务之间按每任务行号公平推进。

外部API和前端状态不变；无迁移；来源间隔、原截止、日数量、用户暂停状态不变。测试覆盖limit=1且持续有效工作时过期结算仍推进、同Task和跨Task、lane/shard/exclude隔离、unknown保护及并发锁重验。回退代码只回退调度缺陷，不回滚已经审计的真实结算。

## 运维与验收

先准备清理清单及原运行状态，再在短维护窗口停止任务执行worker，等待/记录在途调用退出。保存精确Task/Action/版本/hash及关系影响；采用正式入口、原锁和审计，独立读取删除、余留和邻居状态。恢复本次原运行worker及未删任务，不启动原暂停/停止任务。禁止全库DELETE、级联删除真实未知或成功证据。

本轮不新增兜底、假成功、数量降级或权限绕过。所有后端测试每批硬超时60秒。发布master→release→本地linux/amd64镜像直传，当前候选及生产版本一致后独立runtime及typed业务验收。清理成功与业务恢复分开报告；失败必须恢复可安全恢复的原服务并留下明确残余。

## R3反向检查与本轮采用

源码source_pacing_cursor会把无法在原deadline内调用的频道预约继续推进共享next_call尾部，source_pacing_admission仅ai_send检查not_before>=deadline。这与线上点赞来源period_exhausted及远期排队现象匹配。采用已本地审查的3d987665通用截止补丁，并在本轮重新执行反例和PG验证；原本地“不部署”交付不作本轮上线证据，本轮发布依据当前用户修复线上问题授权。评论准备阶段仅捕获明确截止领域异常，其余异常继续暴露；原Job/Action/义务/预约原子结算且保护Provider/Telegram未知。设计状态complete；新修复不自动重建既有远期预约，存量维护独立审计。

## R5 当前成功事实被历史未知遮蔽

新只读证据：仍running点赞任务41c2598e的Action 88b80efe有11次Attempt，7/9为unknown，11为同账号/epoch成功；Reaction义务confirmed，但通用FulfillmentRemoteFact仅保留第7/9次unknown。根因：_fact_attempt复用用于禁止重试的保守代表Attempt，误把历史unknown当作当前成功结果；project_remote_fact还允许迟到unknown覆盖confirmed。

设计状态complete：仅对频道浏览/点赞、当前Action success，从其result.remote_fact_id及同tenant/Action/account/epoch的成功Attempt取得匹配的既有类型化远端事实；精确核验obligation、账号、来源/消息、反应revision、确认时间。仅满足该完整身份链时生成对应成功通用fact；否则保持原证据语义，不凭success字符串消除unknown。安全重试/释放仍沿用保守action_remote_mutation_evidence，不修改历史Attempt/Journal/Fact、远端未知和租约。

通用projection按tenant/task/obligation锁读；同频道义务已经有真实成功typed通用fact时，迟到unknown或未调用事实不把confirmed降级。保留迟到事实及投影处理标记；不同任务/租户、不同账号/消息/revision、缺typed事实、错epoch/时间不继承成功。无自动重放Telegram、无配置或schema变化。回归覆盖unknown→success、迟到unknown、幂等重复、错误身份、缺事实及保守重放边界。旧Action回填作为精确单项有hash审计的本地投影维护，不能声称新增点赞。

R5反查resync：线上Action/Attempt的remote_fact_id=87是Gateway回执标识，不能当ReactionRemoteFact表UUID主键。按tenant/obligation/account/target/message/revision查询类型化记录；Action与Attempt仅比较彼此Gateway回执标识，确认时间还须不早于所选Attempt实际调用。新增异构ID回归。

# 线上任务推进缺陷修复合同（2026-09-09）

## Intake / Bug Batch Plan

- intake_id: production-task-progress-repair-20260909；L3/P1。
- 用户请求：在24个运行任务根因诊断后明确要求“你来修复问题”。
- 原始证据：`docs/05-implementation/production-task-root-cause-diagnosis-20260909.md`；可变状态必须重新读生产，不能按旧数量执行维护。
- 流转：prod-diagnosis → product → dev → qa → product → prod-diagnosis。各根因切片分别完成设计门，再进入对应开发；不将部分切片通过写成整体完成。
- 发布：master → release → GitHub Actions；独立核对SHA、运行角色和部署后typed远端事实。保留其他任务工作。

| 根因组 | 修复/核对范围 | 设计状态 |
| --- | --- | --- |
| R1 评论路径与领取 | 实际单/双阶段模型路径一致；异常可靠释放自己领取并暴露错误 | complete |
| R3 额外量组合容量 | 规划扣除同任务日原额度、真实占用和未调用计划；不重分配已发送/unknown身份 | complete，见R3 |
| R4 取消预约残留 | 保留Task，旧未调用Action退出队列，原有效义务由Planner重建新Action | complete，按最新用户澄清 |
| R2 发送后不可见 | 保留可见性合同；核对可信控制事实，不伪造成员/可见性成功 | blocked，远端删除原因/可信控制事实不足 |
| R5 Provider未知 | 保留15秒合同和原unknown；核对现有隔离、恢复路径，不擅自延时或重试 | partial |
| R6 活动窗口/节奏 | 真实等待与缺陷分开；不将future或窗口外工作强制提前 | 无行为修改 |

## R1 Product Handoff / Product Design Complete

### 合同

1. 非V2路径的耗时身份必须描述实际Provider调用。`ai_two_stage_enabled=false`的合法旧评论走既有单阶段生成与确定性质量检查，不调用语义reviewer，因此只冻结realizer；双阶段冻结router/realizer/reviewer并继续要求非空reviewer模型。V2和Grounding激活门保持，禁止把本修复作为关闭必要审核的途径。
2. 不写Task配置，不指定新Provider/model，不创建假V2路由，不放宽单次15秒/真实发送期限；既有timing binding不可被重写以绕过path hash冲突。
3. 评论worker领取已提交后，payload验证、准备、生成、持久化任何异常都必须在原事务退出后释放本次owner/token仍拥有的领取。预期生成失败沿既有持久化语义；未预期异常继续向外抛出，不吞错、不返回成功。释放独立事务锁定Action后检查owner/token，不能覆盖新worker的领取。
4. 已持久unknown/cache/失败状态不得被finally重置为可重试；释放只改仍属于本次领取的状态，既有unknown Job及成本不变。
   R1异常边界反查补正：若Provider-start已持久化而结果持久化再次异常，finally不得把generating直接改为普通pending。复用既有generation recovery的原Job CAS转unknown，同时Action进入provider_result_unknown并清理本次owner/token；原异常继续暴露，下个worker不重领Provider。该路径不是缓存成功，不删除旧调用身份；对应真实领取入口失败回归为dev前验收口径，design_status=complete/resync=true。
5. 历史领取使用已有stale recovery合同，不能仅凭租约到期推断远端未调用。若原入口无法处理，需要另行形成精确证据驱动的恢复切片，不直接SQL重置。
6. 完整CI反查补正（resync=true）：DB领取owner与进程本地runtime reservation身份分别管理。处理入口冻结当时已有的本地reservation对象；finally只通过既有identity-checked释放机制清理该对象，即使DB领取已换owner也不遗留旧资源；本地reservation已被新对象替换时保持新对象不动。没有捕获到旧reservation时不释放后来新建的资源。对应旧claim-loss回归和新reservation替换反例均须通过，不得通过删改原回归断言掩盖泄漏。

### 自检和验收

- 真实代码反查：`comment_generation_pipeline.generate_comment_result`按two_stage分支，单阶段通过`evaluate_comment_generation_quality`；`legacy_generation_timing`此前按Task类型额外要求reviewer，与实际路径冲突。
- 新鲜生产确认：两条当前评论Task的two_stage/V2/Grounding均false；线上仍8fc01975。用户修复授权覆盖正常代码闭环，不等于允许任意历史unknown重放。
- 前端/API/schema不变；无迁移；任务/账号/tenant/epoch身份不变；无扩大来源范围或账号池。
- QA：单阶段无reviewer可绑定；双阶段缺reviewer仍拒绝；双阶段完整路径仍包含三角色；payload/准备异常后领取释放且原异常可见；成功/预期失败/unknown已有回归；新owner不可被旧finally覆盖。
- 发布后：检查该配置错误不再新增，原义务推进到真实生成/执行；评论typed远端事实才是业务通过，服务健康不替代履约。
- design_status=complete，仅针对R1；resync=true。其他根因组设计未完成不进入其实现。

开发反查resync：公共`TimingExecutionPath`原样对所有评论要求reviewer及reviewer_started。R1显式传入实际语义审核需求，单阶段移除不存在的角色和测量边界；缺省保持原强制审核，V2调用者不变。既有双阶段/V2的snapshot/hash不变，新单阶段仅创建其真实path，历史binding仍拒绝漂移。必须经`bind_generation_timing_config`完整入口验证，不能只测角色构建器。该补正design_status=complete。

## R3 Product Handoff / Product Design Complete

1. current日extra-volume候选在LIMIT前按同tenant/task/day/account的authored_message组合额度过滤。额度复用active AccountPortfolioLoadReservation；零分配的已有PortfolioFeasibilityPlanRevision仍表示零容量。没有任何组合计划的legacy路径保持原行为，不为本修复创建新预算或降低业务目标。
2. 占用=原任务日reserved/call_issued/unknown/confirmed行为预算amount之和，加上同原任务日pending/claiming/executing/retryable_failed消息Action中尚未被上述预算表示的数量。一个Action的在途预算不能再被pending投影重复计数；多个真实预算仍按运行层原amount计。Provider unknown仍为原Action保留工作名额，不清除或替换身份。
3. 原任务日从显式payload ledger或已冻结主数量槽ledger/日目标ledger解析；不得按本次扫描时间将旧日Action计入新日。生产当前extra Action无payload ledger，但有精确主数量槽和日目标，必须覆盖这一路径。显式ledger与槽冲突仍由现有身份合同拒绝，不自行改绑。
4. 批量聚合后与候选关联，避免每账号扫描整Action表；过滤发生于既有公平游标和LIMIT之前，耗尽账号不得占满本轮候选窗口。保持既有账号资格/准入/面具检查、每轮上限和公平排序。
5. Planner既有Task互斥串行化同Task计划；扫描前flush本事务物化工作以支持autoflush=False，同一事务再次扫描也不能重复分配剩余一条。Dispatcher不创建计划，只将同一Action的未调用占用转为行为预算，因此占用表示转换不可双扣或漏扣。其他Task只能使用其自己的组合份额，执行层原共享总预算与原组合额度检查保持最终权威。
6. 不删除已存在超配Action，不扩大账号额度、不把failed/unknown变成功。对执行阶段明确返回`task_account_portfolio_capacity_exhausted`的消息，沿已有`ai_group_pre_gateway_discard`全历史Attempt/Journal/Fact未调用证明收口：原Action skipped、typed safely_not_executed、释放该次数量占位且原义务保持open；任一历史called/unknown保持原证据不走此路径。配合候选剩余供给过滤，避免旧超配反复领取又按相同账号重建。若所有可用账号组合额度已满，保持真实数量缺口而不是声称修复后目标已完成。
7. QA：额度3已用3拒绝；已用2待发1拒绝；同Action预算与pending不双扣；unknown计占用、released不占；跨任务/租户/日隔离；无计划legacy保持；零分配计划拒绝；超过一页耗尽账号后仍能选中合法候选；同事务第二次扫描看到新增投影。发布后核对不再向耗尽账号新增extra Action，并分开记录历史积压和新真实消息。

无API/前端/schema/迁移变化。该过滤修复现有配额一致性，不是新增限制；原目标、审核、活动窗、账号范围、未知事实保持。design_status=complete；resync=true，允许进入R3 dev。

R3反向检查补正：仅过滤新计划不足以收口旧ready超配；上述第6条复用§19.68已有明确未调用跳过合同，QA同时验证capacity blocker触发安全跳过、旧unknown仍不跳过、正常pacing仍等待、最终typed未执行及原义务open。该补正design_status=complete，不增加新的直接生产维护入口。

## R4 用户澄清：保留Task，清理失效Action并由原义务重建

用户后续明确：“不是直接删除任务，而是任务的历史积压的actions啊、其他的一些历史问题，直接删除，然后新建新的，解决卡死的问题，让任务正常运行发送起来”。此授权覆盖已定位卡死积压的精确清理与重建，不删除Task，不扩大到有效future/窗口外等待，不重放called/unknown。

### 反向检查与处置选择

1. 17:19新鲜生产仍有三个点赞Task合计359条pending/cancelled预约残留，零Attempt、Journal、typed fact及活跃owner。原Reaction义务pending；部分原来源期限已过，需要区分replan与expire。
2. 旧abandon_ai_historical_backlog会将coverage设abandoned且不再补发，不能用于此次恢复目标；硬删Action也可能沿FK删除Reaction义务。使用逻辑退役移出可执行队列（旧Action skipped、明确safely_not_executed、保留审计/防重），释放原义务，由正常Planner物化新的Action ID。不得抹除历史执行证据。
3. 评论领取残留随R1发布后由既有stale-generation恢复收口；活群超配通过R3候选过滤及全历史未调用收口退出。若这些正式路径仍不推进，重新只读定位，不将未知或有效等待混入本点赞维护批次。

### 精确维护合同

- 显式preview/apply/readback CLI；输入tenant_id、精确task_ids/action_ids、expected_count与完整deployed_sha；不提供全库或按年龄默认清理。
- 仅当前running、未退役/删除、fact_first_v3的channel_like任务，当前epoch的pending like_message；原Action未领取/执行，原预约cancelled且tenant/task/account/pacing_slot_key一致；原Reaction义务pending且持有原Action/账号/消息。
- 原Action全部Attempt、Gateway Journal、FulfillmentRemoteFact、该Reaction义务远端事实均为零；Action/result不存在调用或成功/unknown标记。任何证据变化、任务暂停/配置变化、身份冲突均拒绝。预约之外的在途资源存在则拒绝，不猜测可释放。
- preview保留精确IDs、原状态/版本/时间线和原记录hash（不输出正文/凭据）；apply核对SHA/hash及actor/reference，在Task、账号、Action、预约、义务稳定排序锁后重新取证，任一漂移整个事务零写；Task与账号锁避免Planner/Dispatcher并发接管，Action锁保护相关FK写入。
- apply仅在同一事务中将已取消预约规范化为原bound输入，然后调用既有settle_fact_first_action_before_gateway。旧Action终止并留下typed未执行事实；未来来源使用replan_same_obligation=true，原义务open、预约reserved/action_id=None；已过期来源使用false、预约missed，不延长来源期限、不补发过期来源。
- 预约原due/release/effective/source deadline、原Task配置、账号、消息、任务epoch、义务自然键不改。正常Planner重建新的Action并绑定原有效预约，仍经过现有活动窗、容量、准入与来源门，维护本身不调用Telegram/Provider、不强制drain。
- R4反向检查补正：单纯skipped仍可能占原action_dedupe_key；相同due/载荷的正常物化会返回旧Action。仅本批全历史零调用证明通过并完成typed未执行结算后，将旧非空dedupe key移至`retired_uncalled:<旧Action ID>`墓碑空间，原key进入快照与审计。新Action沿正常原key生成；旧Action/Attempt/fact不删，called/unknown绝不释放dedupe。须以真实create_like_action同due/同payload回归验证新ID。design_status=complete/resync=true。
- AuditLog同事务保留原preview/hash、精确目标/旧新状态版本、actor/reference/SHA；相同preview重复apply只返回已有receipt，不能再次转换预约或重复建Action。readback独立Session检查旧Action退出、typed未执行事实、义务/预约已释放或已由不同新Action接管、Task仍running。持久化通过不等于reaction_observed。
- QA覆盖tenant/count、sha/hash、状态/版本/配置漂移、Attempt/journal/fact/unknown、owner/epoch、原时间线保持、有效与已过期分流、同preview幂等、replacement接管后重放不改新owner、真实PostgreSQL锁竞争整批零写。
- 运行补正（resync=true，代码入口不变）：冻结母清单在正常worker行锁竞争下可按账号划成互不重叠的精确子批，子批并集必须等于原359 IDs且不得新增账号/Action。每批独立调用同一已发布CLI的preview/apply/readback并携带母hash/子批序号审计引用；每批原子，成功批保留receipt，锁忙批零写并显式报告，仅对未成功子批fresh preview后继续。该运行批次不是发送容量上限，不改产品数量/活动窗，不停worker、不移除锁或跳过旧值校验。整体完成必须逐批审计并证明成功结果并集恰为母清单；不能将部分成功称为整批完成。

### R4 发布后反查：冻结预约重建合同补正

- 18:53只读原候选复算（不是完整Planner资格证明）：已释放且来源有效的228个义务，源节奏全部返回0点，其中176个原due仍在未来。其他同来源义务的历史cursor已排到来源deadline附近或等于deadline；重建把原冻结位置误当新增供给，继续排在cursor之后，出现虚假的pacing shortfall。
- 原义务open/current_action=None，存在同tenant/task/account/slot、reserved/action=None的原预约，且义务已有冻结due/release时，Planner物化应使用原义务冻结SourcePacingPoint；不能作为新来源数量追加到其他义务cursor之后。原账号预约仍由reserve_account_pacing重新核验账号/任务当前合法间隔、活动窗和来源期限，不提前发送、不延长期限、不跳过锁/准入。
- 只有仍有效的既有未绑定预约可复用；bound、missed、cancelled、缺少冻结时间或非open义务继续原路径或明确拒绝，不释放unknown/held/confirmed身份。复用不改变义务冻结due/release；账号执行effective可按既有正式rearm规则向后调整，不把维护时的时间线快照当成永不可推进。维护CLI本身仍不改原时间线。
- QA必须经_create_like_actions真实入口，先安全退役旧Action，再设置同来源后序义务cursor接近deadline，证明新Action不同ID、绑定原义务、原冻结due不变；验证过期预约不复用、bound/非open不复用、原账号节奏仍有效。不能只测底层create_like_action去重。
- design_status=complete；resync=true。此缺口属于R4重建链路，补正后重新dev/QA/完整Prepare/上线验证。

### R4 历史终态别名去重补正

- 19:20按created_at核验：非原359的绑定不能全部算新Action；三个绑定为9月6日创建、skipped/distorted_far_future_schedule_rebalanced的历史记录。已有物化去重只含稳定payload/plan batch，正常未调用结算后的再次物化仍可能返回上一轮终态Action；不能通过移除本批359个key就宣称所有历史别名解决。
- 新LikeMessagePayload增加非负reaction_action_attempt_no，缺省0明确表示兼容既有已持久化payload；不回填旧Action、不修改其执行/unknown身份。正常Planner仅在原义务open且current_action=None时，从该义务现有action_attempt_no生成下一序号，作为payload稳定去重字段；bind沿现有合同递增该义务计数，不创建另一套计数器。
- 同一原义务/同一次合法重建仍取同一去重身份；正式未调用结算后下一次重建使用下一身份，必须创建新pending Action，不能返回任何旧终态Action。pending/held/unknown/confirmed义务没有创建新代的权限；不改变原RemoteFact/账号消息全局防重复、Gateway证据与unknown reconcile合同。
- Gateway执行仍只使用原reaction参数，不把内部序号发送给Telegram。无数据库迁移、Task配置/API/frontend变化；兼容仅用于已有payload，不是绕过真实执行的fallback。
- QA经真实维护→Planner物化→正式未调用结算→Planner再次物化，验证第二个新ID、pending、原义务/预约绑定、序号递增且不覆盖旧事实；并验证同一序号去重、未知持有不产生新Action、既有payload0仍可解析。验收按created_at/序号和远端事实区分真实新建与历史别名。
- design_status=complete；resync=true，重新进入dev/QA/发布，保留已执行维护审计。

运行执行补正：十账号子批仍频繁因单账号忙而整批零写时，可将未成功子批继续拆成单账号子批；从母清单减去已有成功receipt精确集合生成，不以实时扩大查询替换母清单。运维编排可在同一Python进程顺序调用已发布CLI main，各次preview/apply/readback仍使用CLI创建的独立Session、SHA/hash校验和事务；只有明确55P03锁冲突可记录失败后继续其他账号，任何非锁异常立即停止。全部CLI退出后将所有层级receipt统一并集读回并保存到容器外，之后才能替换容器。该补正不修改生产业务代码或弱化锁/审计合同。

跨发布续作（resync=true）：若正常执行的长事务持续阻止剩余子批，而完整候选已修复相关监听锁持有/重建问题，可在全部维护CLI退出、成功receipt与精确剩余IDs独立读回并归档容器外后，先执行正式Deploy；不为维护杀事务或重启单worker。新runtime续作只能取原母359减去已成功receipt集合，使用新完整deployed SHA重新生成子scope/fresh preview，仍经过原全部校验。旧母清单/preview/receipt/SHA不改写；新阶段文件放独立runtime目录、审计引用保留原母hash，跨阶段成功集合必须互斥且最终恰等原359。独立总读回使用当前部署同一verify服务验证各阶段原receipt，不改审计内旧SHA。

design_status=complete；resync=true。该合同替代已撤回的“恢复原Action预约继续执行”方案；用户明确的清理重建授权已具备，无需重复索要同一授权。R2可信控制与可见性、R5的15秒及unknown合同保持；没有证据时不能承诺全部24任务已恢复。

# AI活群可执行供给、主题与故障兜底修复

## Intake与原始需求

- intake_id: AI-GROUP-SUPPLY-FALLBACK-20260910；L3/P1；merge_owner: 本任务。
- 用户要求“你来解决问题”，逐项明确：加入和发送分开；已加入可正常发送，未加入减少排期；没有上下文自动发送活群主题，同时核查入群、管理员兜底和自动加入任务；其它模型和签到兜底应生效；修复发送排期；解决后重新检查。
- 授权范围：本切片设计、代码、定向QA、正式发布与线上复核。自动入群和救援沿现有配置与真实权限；不把未知Telegram写请求当未发送重试。生产额外维护如必要，必须精确preview/hash/apply/readback。
- 基线839c7b19；独立工作树/tmp/tgyunying-ai-fulfillment-repair-20260910；原工作树两个未跟踪诊断文件保持。
- locked_paths: 本专项PRD及其真相源引用、活群发送供给/上下文/应急选择/来源排期模块及定向测试、必要schema/model/migration、发布Gate；不涉及Clone/登录/评论功能扩展。

## 线上反查与根因分组

- 23:36一致只读快照：10 running活群，日账本922/19391，最近30分钟5任务共44条严格typed消息。不是已恢复。
- 23:50:29：正文ready且scheduled到期1117，其中1116当时在原Session窗口外，仅1同时通过窗口与release；这是当前可发机会不足的证据，不能独自推断全天根因。
- 开放Action的Provider未知431；关联HTTP包含432条unknown且本地调用进程结束已确认，Provider未知/费用记录保持。9月6日至今历史Job不与当前日数量混算。
- 入群1002项待准入；全部10Task auto_join_target/auto_resolve_verification=true；部分pending没有失败原因，部分需远端对账。缺Task级管理员覆盖不能推断租户管理员未配置，须继续按正式解析链核查。
- 成都监听游标连续且轮询更新，但listener_last_error非空；normal watermark入口把所有监听错误当正文阻塞。
- 源代码确认：统一引擎§19.61应急分支此前只完成设计；旧pipeline的two-stage/显式model禁用签到；Provider unknown直接退出候选链；恢复只处理同attempt缓存，未接纯内容unknown的应急发布权交接。

## 产品口径与范围

1. 加入与发送按Task/账号/目标独立。正式 membership 成功及权限读回的本地投影必须使用实际调用所指向的 OperationTarget 身份：payload 引用与 target 原引用相同、或两者是同一规范化公开用户名时，写入 target 原引用对应的 canonical 群记录；剥离 URL 前缀及同名可发群不得把此投影导向另一个本地群。实际调用采用不同目标引用时，不强行映射到 Task 群。成员关系和 GroupBotAdmission 必须写入同一个 canonical TgGroup；只记录加入/权限已证实部分，群管机器人继续原观察/验证状态，绝不因 membership_observed 标成 C2 ready。历史错绑别名群保持审计，不从旧成功回执直接捏造当前成员或机器人完成；须独立核验后按正式恢复流程处理。未加入账号保留加入义务，暂不创建正文Action、调用模型或预占正文发送时刻；已加入且can-send的账号继续自己的数量/覆盖义务。加入成功后只进入剩余合法时段；不把未加入算完成，也不让其它账号抵扣它的独立覆盖。
2. “减少排期”指减少当前发送队列与资源预占，不回写缩小已经冻结的日目标。新计划的保证供给只含当前已准入账号，等待加入属于条件供给，界面/诊断分列。原账号健康、真实成员/发言、C2及目标权限继续独立验证。全日 portfolio 冻结预算保留原需求身份，它不是正文来源时间预约；当前准入集合须贯穿所有覆盖候选分页，并在 SQL LIMIT 前过滤，不可在后续扫描重新放入未准入账号。portfolio 冻结需求保持全账号和原数量，首次分配及剩余 deficit 恢复仅使用当前准入账号集合；该集合是运行时供给条件而非冻结需求变更，因此加入后可恢复原未分配部分。已有账号预算预约不删除、不移给其它账号，既有满额冻结分配仍保留原账号身份。

   准入与供给子切片（口径1/2/4）：`design_status=complete`，入口反查确认首次候选已过滤，但后续日覆盖扫描、fact_first 未物化重排尚可读到未准入前缀；现已贯穿当前准入集合并在 LIMIT 前过滤。全日组合预算在 total_units 小于候选数时可只分给未入群账号，已通过“冻结需求不变、运行时 allocatable 集合”修正首次分配和 deficit 恢复。原成员/发言/账号预算/C2/来源间隔继续校验。00:14:21只读线上反查进一步证明：天津音乐63项、 西安11项成功membership_observed仍pending，Task/Action/Item target一致且canonical引用匹配，但canonical成员link缺失；天津63项GroupBotAdmission落在别名群1251而非5999，西安4项落在5932而非5363（其余7项无该记录）。00:17:50核对天津63项及西安4项实际Gateway引用与原目标公开引用一致（均非numeric peer），其余7项缺匹配依据；根因是membership投影剥离公开URL前缀后选择别名/同名群。本补充只固定同一实际目标引用的本地投影，保留群管机器人独立状态。QA：9个定向文件共127 passed（16.20s），真实PG独立schema双事务晚入群争用1 passed（3.37s），证明冻结需求不变、只追加一个 successor 与两账号共原3单位预约；`git diff --check`通过。canonical membership投影专项及原membership/epoch/机器人scope回归74 passed（10.54s），未改变旧不同实际引用路径的真实目标，不把加入成功当机器人完成。代码尚待总审查/发布/E4。旧已满额分配给未准入账号的预算仍保留，不能宣称已被重分配；加入后使用原账号预算。

3. 普通主动活群在没有可用上下文时走明确topic_only模式：使用配置活群主题，无真人引用/对话续接声明；生成仍遵守内容安全和真实性。监听失败单独报告且继续恢复，不把其整组错误变成主题生成的门槛。真实reply仍必须存在真实引用对象，不将reply悄悄转换为direct。
   - 本子切片仅对`unified_engagement_v1`启用，legacy保留原合同。入口在 `ensure_send_message_content` 的普通生成guards之前决定输入模式；仅处理未ready、无真实reply、无interaction/turn claim的普通direct。已ready正文冻结，真实reply及互动身份继续原校验。
   - `ai_generation_context_mode=topic_only` 与原因持久化在原Action；无本群真人有效上下文、监听未启用/游标不连续/轮询缺失/监听错误/水位落后时清空history、context/anchor/snapshot ID和失效的reply展示字段。原scope身份、账号准入、内容安全/真实性门槛不变；跨群现存引用仍由原scope校验明确拒绝，不能借清理绕过。
   - topic_only在同次普通生成中保持，refresh/prompt rebuild不得重新填入不可证实的历史；prompt明确只主动开启允许话题，不声称引用或续接真人。监听错误保留在TgGroup并在Action记录原因，不写成功游标、不伪造GroupContextMessage。
   - `topic_only`以独立`ai_generation_topic_direction`冻结配置主题，原`topic_mode/topic_direction`及比例/容量历史保留；prompt使用独立主题，结果分列为无上下文主题生成，不计作原普通topic_ratio质量履约。无配置主题明确暴露`topic_only_topic_missing`并进入已授权应急选择，不伪造话题；此处属于既有主题比例合同resync，发布前需连同投影/统计验收。V2绑定与two-stage使用显式“群话题”配置证据，generation_contract记录`context_mode=topic_only`、`evidence_source=configured_topic`、独立主题快照；真人锚点为空。配置主题不能冒充成人真人证据，原route授权与成人证据检查保留。
4. 普通数量义务的可执行供给不能依赖真人先发言；natural-opportunity/presence仍观察质量和互动，但不得以真人为0或连续系统发言阈值把已承诺合法主动主题/签到永久截断。账号/群实际频率与业务预算仍检查。数量入口继续落库 natural-opportunity/presence 原始质量证据，但标记为 quality_observation_only；其 deficit 仅说明自然互动机会不足，不设置普通数量发送阻塞，也不截断已准入的主动正文候选。真实互动续接仍保留原 presence 校验。
5. 模型兜底沿已配置provider route顺序。明确失败/额度不足/可证明本地纯内容调用结束但结果未知，记录原请求与费用后可尝试下一批准候选；每次调用有独立原时限且整体不得越过原latest-safe-send。未知请求不重放，远端硬在途不由本地结束推断释放。不能仅以provider异常字符串泛化绕过合同或权限错误。
6. 正常候选在允许预算内仍不可用时，活群direct使用精确“签到”；真实群回复按§19.61保留reply身份、使用批准表情，不能签到冒充回答。本切片不扩展频道评论路径。
7. emergency策略版本emergency_fallback_v1明确启用（用户本轮已授权）；Task允许显式关闭并审计。旧显式ai_model、two-stage、planned比例与静态签到禁用不构成此独立应急关闭。默认值和实际生效版本必须可读。活群创建/编辑表单提供独立“任务应急兜底”复选框，默认开启，`emergency_fallback_enabled=false`必须原样保存并回填；API创建配置和TaskSettingsUpdate均接收该字段。详情同时显示开关状态、仅统一引擎生效及固定策略版本，解释普通主动发送可签到、真实回复保留引用改用批准表情，数量仍需真实发送且与普通质量分账。租户`ai_group_static_fallback_enabled`保留旧计划兜底含义，表单不读取或覆盖该开关。

## 原义务发布权与持久化设计

- 复用原Task/ledger/quantity slot/coverage/Action载体；新增append-only内容选择事实，冻结policy版本、原正常Job、错误阶段与证据、Action前版本/内容哈希、账号/群/reply、唯一确定内容哈希及materialization revision。它是原Action的内容successor，不复制第二个数量义务，也不新建第二套计费账本。
- 正常生成与应急选择共同通过Action当前版本、status与generation claim token竞争；应急只能在原普通发送明确未调用且没有其它claim时交接。锁顺序沿现有Task→Action/owner；实际Gateway前再次比对当前选择和发送载荷，旧对象/旧token不能越过。
- 纯Provider unknown保留原GenerationJob.state、AiProviderAttempt、HTTP未知和硬占用；撤销其发布权后，晚到结果只能保留审计，不能写ready或发送。无同身份纯内容调用证据时拒绝套用这一例外。
- Telegram的任何call-start、可能写入回执、未知/成功fact或journal拒绝切换；原已call-issued/unknown/confirmed只能正常对账。不能删除旧Action/Attempt或把unknown改failed来制造可重试。
- 相同原义务只能一个应急选择，重复selector/重复claim幂等；同账号同日不同真实义务允许各自签到，旧日一次与普通文本重复规则不能误伤应急。应急消息记忆使用独立选择身份，显式标质量降级。
- 应急不能修改Task状态、数量、原due/day/deadline、target/account/reply或账号预算。发送后仍要求成功Attempt与同身份typed可见消息事实，才计数量与账号覆盖；与正常主题/老师/grounding质量分账。

## 发送排期设计

- 本切片设计已完成入口反查：`admit_source_paced_attempt`先锁同租户/来源/domain的`SourcePacingState`，再锁原owner admission，最后决定是否调用。只对`group_ai_chat/send_message`改用空隙分配；其它任务沿原cursor。来源锁继续覆盖读取其它预约、选择时间、保存预约及call-start，不额外锁其它Task/Action，避免倒置现有锁顺序。
- 新/复用的未调用活群预约，从`max(now, owner/action当前release, action.effective_claim_at, last_call_started_at + max(last_gap, current_gap))`开始，按时间有序扫描其它有效预约，并与原账号既有Session窗口反复求交。当前release保留已持久化的其它约束；不直接回拨历史source defer曾推进的Action/owner release。原`pacing_due_at`、deadline和数量义务不变。
- `next_call_not_before_at`只保留历史cursor兼容投影，不再充当活群分配下限。其它有效未来预约按双方gap最大值保持独占，允许恰好等于间隔的边界；不存在早期空隙才向后推进。过期的未调用点、非running Task、旧epoch或plan hash、终态Action、旧/关闭日账本、超原deadline、早于已更新release/effective或处于原账号窗口外的预约不阻塞新点，历史行保留。
- 已call-start/remote-unknown既不回收也不替换；原Action仍由原前置拒绝与settlement保护，真实来源last-call间隔继续生效。新查询只解释未调用`reserved`未来点，不能将unknown解释成未调用。来源锁内不修改其它预约。
- 统一引擎账号只读取已经冻结的`AccountBehaviorSessionPlan`；无原窗口或截止前无交集则沿现有`pacing_source_period_exhausted`显式延期到原deadline，不制造新计划、不增加窗口、不消费wake。窗口与deadline均采用半开区间。非Session合同以及已有明确wake-consumed凭据维持其原时间语义；不为候选新消费wake。账号/群实际间隔仍由正式claim校验，本分配不会缩小它们。
- 窗口内先处理可以执行的到期账号；未加入/窗口外/无发送资格账号不能在LIMIT或共享资源分配处挤占已就绪工作，此供给入口由独立切片实现。
- 验收必须覆盖：失效next尾部不挤出当前空隙；其它未来点及不等gap；位移后重新进入原窗口、窗口终点/原deadline；同来源跨Task顺序分配；旧epoch/终态/日账本失效；已调用unknown身份不变；现有其它任务cursor回归。真实PostgreSQL锁验证与线上历史release存量复核单列，不以SQLite串行用例代替。

## QA、审查与发布闸门

必须有真实入口反例：
- 同Task部分未入群，已入群持续产出；未加入不生成正文/占来源预约，加入后恢复独立排期；空候选页不能阻断后方已准入。
- 无上下文/监听错误→明确topic_only；不引用历史真人、不伪造会员已入群；真实reply身份保持；主题/安全拒绝仍暴露。
- 主模型失败→下一个已配置模型；纯Provider未知/额度耗尽/正常质量耗尽→签到；正常已ready优先；Telegram未知禁止替代。
- 应急/正常晚到/Dispatcher同时竞争只能一个发送身份；两不同义务同日签到均可完成；重复选择与重启不重抽/重复；真实PG验证锁/CAS。
- 来源空隙与窗口交集、失效尾部、并发同群、未来预约不碰撞、真实gap不缩短、原deadline/半开边界/已调用未知不变。
- 发布后独立SHA/backend+worker/API/迁移，再逐10Task核对当天ledger/可发队列/模型路径/主题及签到选择→真实typed消息；仍有准入/Telegramunknown等外部缺口分项报告。

当前design_status=complete：原始需求、具体集成点、作用域、并发/幂等、权限、UI/API、迁移、正常/应急数量与质量分账及QA合同已闭合，各子切片均先完成产品反查再进入dev。当前implementation_status=complete、qa_status=targeted_pass_full_prepare_pending、production_status=unproven；发布和真实业务结果必须另行验证。

## 应急与模型交接实现合同（design_status=complete）

- 配置API `GroupAIChatConfig.emergency_fallback_enabled` 默认true，仅 `unified_engagement_v1` 活群启用；任务配置可显式false，旧legacy保持原合同。选择事实冻结 `emergency_fallback_v1`，关闭后未发送应急不再进入Gateway。
- `ai_group_emergency_selections`按原Action唯一、primary_quantity_slot+materialization版本联合唯一，记录原payload哈希、Action版本、materialization版本、原Job、账号/群/真实reply/coverage/day身份及纯内容证据。迁移0230新增事实表，0231仅调整历史唯一约束，不修改存量业务记录；已存在事实时禁止drop降级。
- worker在正常生成前按当前开放日账本筛选pending且无claim的provider_result_unknown/emergency_pending，锁Task→Action→原Job。任何已调用Attempt、未知远端身份、Journal或typed消息均禁止交接。只有未到原deadline、原coverage仍绑定Action的数量义务可选择。
- Provider unknown必须有同原Job、同tenant/task/epoch的HTTP unknown且local_termination_confirmed=true，所有未决物理调用均已本地终止；不修改这些HTTP或Job未知。配置路由的下一不同provider/model可在原候选截止前独立调用，旧未知保留费用与硬占用；没有可证明结束的调用继续显式阻塞。 真实入口为draft/structured候选循环→scoped Provider transport→`_start_exchange`，候选循环仅在内部`_ai_group_emergency_enabled=true`及显式route时继续考虑候选，物理调用在原Task/Job锁内再次校验Task.type、unified合同与当前开关。按稳定obligation lineage查询全部未决HTTP及其全部Job关联，必须均属于本批当前Job、相同tenant/task/epoch与execution_path_hash，且均为本地终止已确认的unknown；started/response_received、旧Job/旧epoch和混合关联仍阻塞。新provider/model必须存在于各Job冻结的当前purpose route候选，route ID/revision/hash一致，禁止同logical_request_id重放；同purpose只能按冻结priority向后转到不同provider/model，后续不同purpose的独立阶段可按该阶段冻结route继续。每次调用仍受原15秒上限与原candidate deadline，物理准入再次核对原binding/Job截止。成功返回普通结果；所有候选失败时优先抛出最早unknown，不能被后续普通错误或路由暂缓覆盖，旧HTTP/费用/硬占用与Job未知不改。
- 正常生成在模型候选耗尽、明确provider不可用或结构/质量尝试耗尽后只交接内容权，保存错误。权限/作用域/配置绑定/真实reply失效不转换为应急。quality_wait的耗尽路径保留原义务，不提前落数量shortfall。
- 旧内容维护与Gateway前置先核验独立应急selection事实、原Action/materialization/内容hash/消息memory；合法应急不套旧账号面具或每日签到合同，不释放原coverage。伪造、失配或关闭后的应急选择明确记录独立错误；只有证明整个原义务尚未调用时才按原维护语义释放，已有Telegram证据保持原数量/远端身份待对账。维护扫描按稳定Action游标越过合法应急，不能被固定LIMIT前缀饿死其它旧合同修复。
- direct固定签到；reply从版本化批准Unicode池按原Action哈希确定一个表情且冻结，不改变原act/relation/引用合同。独立消息记忆以selection作为reservation_key，原重复文案或每日签到限制不用于应急；Gateway仍验证内容政策、成员权限、账号在线、真实reply与排期。
- topic_only与emergency保留冻结ContentIntent身份校验，单独质量分账，不能计普通主题比例/grounding成功；其合法数量仍以成功Attempt+typed可见消息事实计数。原已call unknown不会因此重放，晚到生成必须旧token CAS失败。

主题 Gateway 绑定补充：`topic_only_context_v1` 在所有统一活群 GenerationJob（含非V2）冻结配置主题、Task/epoch/obligation/Action/账号/群与主题hash，正常候选持久化再绑定正文hash；Gateway仅在全部身份一致且无真人history/anchor/reply时认可独立主题容量与质量分账。应急选择同时核对传入payload和数据库当前Action完整身份，旧payload不能改变实际目标/引用。

## 合并前最终复核

主题/应急四项独立复核45 passed；新增 Gateway 冻结主题与当前 payload 负例、同日同租户同账号同原义务 typed 质量计数11项通过。membership canonical专项74 passed，helper调整后91 passed；模型 draft/structured真实HTTP与既有回归71 passed，PG当前Job/旧lineage/行锁4 passed。根广覆盖297 passed、前端production build通过；结果分组保留，不累计重复运行冒充独立用例。Release Gate位于 `docs/05-implementation/ai-group-supply-fallback-release-gate-20260910.md`。

只读准入补充：10Task自动入群/自动验证均启用，正式管理员解析为租户账号515且本地在线、未冻结、有Session；远端群成员及邀请权限仍须单列验证。成都显式历史监听账号63失效覆盖了群内可用账号14，导致“没有可用监听账号”；本切片的主动主题分支不再因该错误停生成，真实回复/监听恢复不伪装成功。历史membership别名错投影中67项同引用已证明，另7项不足以证明同引用，均未做生产回填；历史远端未知继续原对账。

## 生产反查修正：义务内容版本必须原子推进

2026-09-10 01:35 生产反查：两条应急 Action 在0 Attempt/0 Gateway时因 `emergency_selection_binding_invalid` 失败。选择事实版本为2，而正式 `ensure_action_obligation → rebind_projection` 将Action版本从2同步回原 `FulfillmentObligationProjection.materialization_version=1`。原设计只推进Action、漏掉权威义务投影；本切片标记resync，进入原设计/实现/QA闭环，首版发布不能声明应急发送恢复。

修正合同（design_status=complete）：selector在原Task→Action锁内先经正式义务注册入口确认/建立原投影并锁定同一FOP，再锁原GenerationJob。投影必须为同tenant/task/epoch/obligation、open、active_action_id为原Action；原Action与投影旧materialization必须一致。选择事实、原Action内容版本、FOP内容版本及投影乐观锁版本在同事务推进；active_action_id、原义务数量/期限/远端身份不变。Gateway同时核验选择、当前Action与投影版本，正式重注册不得回拨。QA必须通过真实 `ensure_action_obligation` 在选择前后执行的入口反例，及原投影非open/foreign owner拒绝场景。已失败存量与其后继Action按当前独立事实单列，不能改写原失败或未知记录来伪造恢复。

选择历史与当前所有权补充（design_status=complete）：`UNIQUE(primary_quantity_slot_id)`误把历史选择当永久发送所有权，阻断原义务安全失败后正式重排。0231迁移仅替换为 `UNIQUE(primary_quantity_slot_id, materialization_version)`，保留每Action唯一、全部历史事实不改写；唯一当前owner仍为同原FOP.active_action_id和materialization版本。后继仅能在旧Action终态、整个原slot无Gateway/远端不确定证据且FOP仍open时，经正式rebind取得更高版本后追加选择。旧选择不获得新Action发布权。已上线但仍为同activeAction且未调用的pending选择，只在完整selection/current身份、内容hash、旧FOP版本恰为selection版本减1、同epoch/同原slot且FOP open时允许原子对齐；记录该选择ID及前后版本，不能对terminal/已换owner/已Gateway调用的历史选择自动对齐。

版本修正最终本地QA：10文件UTC环境130 passed（18.12s）；真实PG原始选择/旧token CAS/注册回拨与历史对齐竞争2 passed（4.41s）；另原FOP并发及0196→0231迁移6 passed（10.81s，部分用例重叠不累加）。所有后端进程硬超时60秒。正式发送前入口对不一致版本仍只读拒绝，批量维护与正式义务注册负责精确对齐并审计。再次完整Prepare与生产E4待执行。

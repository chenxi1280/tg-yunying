# TG 运营管理平台 PRD

> **2026-09-03 Antigravity CLI AI Provider 冻结合同：** 生产 authenticated `agy models` 已移除 `gemini-3.5-flash-medium`，且发布探针以 zero-turn/zero-usage 的 `antigravity_model_invalid` 失败；本轮生产切片因此将第一优先级收敛为已有跨场景 POC 的 `gemini-3.6-flash-medium`，第二优先级仍为 `gemini-3.1-pro-low`，之后才是既有供应商。`gemini-3.7-flash-medium` 的评论样本未通过事实/地点硬门，`gemini-3.8-*` 尚无项目 POC，均不得代替当前主模型。语义审查保持独立既有供应商；五账号整体只有每个 Linux user 分别 OAuth、schema POC、健康读回并通过 guarded route apply 后才可称五 slot 完成，禁止复制 OAuth 或用一个 root HOME 冒充五账号。详见 `docs/03-feature-designs/antigravity-cli-server-provider-design.md`。

> **2026-08-30 Telegram 1:1 群组镜像克隆（group_clone / v2_group_clone）专项合同（当前最高优先级）：**
> 1. 新增独立任务类型 `group_clone`，履约合同版本为 `v2_group_clone`；存量 `group_relay` 保持 `legacy_v1` 不变；
> 2. 一个任务严格绑定一个源群和一个目标群；同一源发言人稳定映射到一个受控马甲账号，换绑时绝不动态修改马甲账号资料或头像；
> 3. 采用四类持久化事实模型：`CloneSourceEvent`（源事实与连续 `stream_order_no`）、`CloneSenderBindingHistory`（发言人状态机与部分唯一索引）、`CloneDeliveryObligation`（18种状态全集）、`CloneMessagePart`（不可变 `random_id` 与远端映射）；
> 4. 平台共享目标群写入权威 `TelegramGroupMutationAuthority`：Clone 必须申请 `exclusive_clone` 独占模式，跨业务拦截其他平台 writer；
> 5. 平台共享 `TelegramAuthorizationUpdateState`：账号级单 Collector 消费 common updates，任务通过订阅交付消费，根除 PTS 竞争；
> 6. 目标群全局 Sequencer：基于单调递增拟人延迟与 Reply/Topic/Album 显式 DAG 保序，队头阻塞生成 `CloneSequencerHeadCase` 并支持可审计的 `visible_gap_accepted`；
> 7. 严格区分 `sender-role`（消息发送与自编辑）与 `control-role`（Topic管理、Pin/Unpin、管理员Delete）；
> 8. 完整专项合同见 `docs/03-feature-designs/telegram-group-clone-1to1-prd.md`。
> 9. 当前已完成共享 collector/difference、文本与消息生命周期主链、非 Send desired-state reconcile、已识别 writer authority、详情人工处置和 guarded pre-mutation cutover/rollback 的本地部分验证，`implementation_status=partial_local_validation`；Album/Poll/媒体实际发送、完整 exclusion rollback、PostgreSQL/真实 Telegram QA 仍为发布硬阻塞。

> **2026-08-30 运行历史存储补正：** Action 明细按终态分层保留，`skipped/success/failed` 默认分别保留 1/2/7 个完整自然日；删除前固化日期、状态、Action 类型和类型化原因汇总。`closed_unknown`、`unknown_after_send`、全部开放/重试态、cancelled 与 typed remote facts 不进入普通时间清理。任务中心窗口外明确显示“明细已按保留策略清理”，运营数据读取长期汇总；生产清理只能走 SHA/fingerprint/actor/ref 守卫的 preview/apply/readback，逻辑清理与物理索引回收分别验收。详见 `docs/03-feature-designs/runtime-storage-retention-and-reclaim-prd.md`。

> **2026-08-26 批量账号逐账号完整初始化当前合同：** 目标普通分组的normal行由后端强制`normal_full_init_v1`，不能由前端关闭；`new_account/already_authorized/relogin`在新A登录或fresh A probe后原子create-or-attach唯一账号级full initialization，旧`create`只读映射为`new_account`。A完成后父行进入非终态`post_initialization_waiting`并释放login lease，真实Telegram固定2FA、同policy平台+远端姓名/头像、B/SV、C/MY双副本/恢复和Saved Messages E4全部读回前不得计完整成功。历史成功只作为当前gap decision的predecessor，姓名/头像逐项补缺；manual/unknown在原operation上通过current-2FA、恢复邮箱或对账收口，后置失败不得重试整条登录，也不得在随后成功时覆盖目标分组等原失败。2FA enabled/本地密文相等或无provenance legacy ciphertext不证明固定密码，candidate只有经Telegram接受后才可信；固定值和临时secret不得硬编码或进入文档/日志。共享owner必须兼容才attach，策略/权限/capability/账号生命周期漂移显式阻断；未重新进入批量登录的账号不扫描。完整合同见`docs/03-feature-designs/account-batch-post-login-full-initialization-prd.md`；当前已完成本地实现和定向 QA，`implementation_status=implemented_local`、`release_status=not_started`、`production_status=unproven`。

> **2026-08-26 当前合同覆盖以下历史口径：** frozen-N 全量批次创建后，只允许一次外部 `--mode sweep --until-exhausted` start/apply；full sweep 不接受 `--max-accounts`，同一 durable supervisor 必须逐账号串行处理全部 remaining pending 到 `pending=0`。`checkpoint_interval=30` 只用于持久审计、守恒和安全门禁复核，完成后自动继续第 31 项及以后，不等待人工续跑。确定性失败统一进入 `manual_required/deferred_issue` 异常队列并继续；SSH/远端 unknown 先对账同一 operation，仍未知且 runner=0、client=0、runtime=off、A current/Session/generation/身份无漂移、同一 operation 不可重放时才 quarantine 为 `deferred_reconcile`。A 漂移、A/B 双失效、2FA、验证码、C 制品/恢复异常都入统一队列，不修改 A、不以 C 在线顶替。只有 A 无漂移、B/SV、C/MY、双副本、restore probe、Saved Messages remote ID 和断连门禁全部通过才计 `succeeded`；首轮必须输出 succeeded、manual_required、deferred_reconcile/unresolved 与 N 守恒，只有 succeeded 全部完成才算 ABC 完成。
> **2026-08-26 第二阶段 deferred recovery：** 首轮 `pending=0` 后，只允许对同一 frozen batch 的 `deferred_reconcile` 子集发起一次外部 `--mode apply --until-exhausted`。启动前生成只读 canonical manifest，冻结 item/account/operation/flow/candidate/stage、A 冻结事实、B/C/E4、runtime/release 与版本，并只对外输出脱敏分组和 manifest hash。二阶段 worker 不处理既有 `manual_required`，不重放 Telegram、不重登、不发码；已由持久事实证明 B/C/E4 和 Saved Messages 远端 ID 全部完成的 item 可 checkpoint-forward 为 `succeeded`，明确终态失败可转 `manual_required`，仍 remote unknown 的 item 只写二阶段重判审计并保留 `deferred_reconcile`。

> **当前 one-shot release gate：** 历史 stopped frozen batch 只有在尚无 one-shot start 审计时，才可由同一次 sweep `preview/apply` 以 fingerprint/CAS 绑定当前 execution release；该控制面绑定不重放 Telegram、不改 A/B/C，不是 `--max-accounts` 续跑或 legacy resume。运行中/暂停、已有 start 或任何并发/安全事实漂移都必须零写入暂停。

> **历史兼容：2026-08-23 全部在线账号 A/B/C 补齐 v2.23 合同：** 批次创建事务先以 `selection_mode=all_online_accounts` 冻结当时全部在线账号为动态 `N`，再逐账号执行 A fresh probe；A 失败、封禁、人工处理、unknown 或冻结后删除都保留在 `N`。A/B 是硅谷主备角色而不是固定 Developer App 名称：新账号默认 A=App A、B=App B，历史切主后 A/B 可交换；补 B 时必须从 `primary_sv`/`standby_1_sv` 两套现有 App 中选择与 current A、App C 都不同的一套并冻结 assignment/version，不增加第四套 App。正常补齐由 current A 作为 B/C 两次独立 challenge 的登录码来源，单账号按 A -> B -> A fence readback -> C 推进；B/SV 健康或 C/MY 已具备完整 bundle/双副本/恢复密钥/inventory/restore probe 时只 readback，不重复登录。B 仅是 SV 本地备用；C 仅在 A/B 双失败事实同时成立时辅助 SV 重建 A，永不 current、永不发送。账号 outcome、B outcome、C outcome 三组各自守恒为 `N`。新的 10 账号批次完成 10/10 后不等待固定时长；`accept` 必须即时重算十个 A 均无漂移，并逐项读回成功 E4 operation 对应的非空 Saved Messages 远端消息 ID，同时要求 runtime off、global unknown=0、MY client=0 和 B/C 三组守恒。任一条件不满足不得冻结全量。通过后全量只建一个 frozen batch；旧 runner 的 max-accounts 规则仅保留为 legacy 兼容事实，不适用于当前 one-shot sweep。

> **历史兼容：2026-08-24 full frozen-N 人工失败继续口径：** 旧 runner 的 `failed/manual_required/reconcile_unknown` 曾首错停批；当前 one-shot sweep 按上方统一异常队列合同处理确定性失败，未知仍必须先对账并满足 quarantine 门禁，A 漂移和全局安全门禁仍显式暂停。旧 DB-only 人工结案事实不得被改写为 succeeded；canary 不适用该例外。

> **2026-08-22 B 登录落库冲突补正：** Telegram 已授权但 DB current-slot insert 冲突时立即停止账号序列并保持 runtime off；禁止重发码或创建第二个设备。只有原 flow 临时 Session 在原 App/代理仍 authorized、与 A 同 UID/不同 AuthKey、A 观察到该 App exact-one 时，才可通过 fingerprint、异人审批和 operation/A/flow/旧 B CAS 原样恢复为 B；旧 B 仅转 non-current protected repair，A、远端设备与账号 generation 不变。证据不唯一保持 unknown，且本次 stop 后不得继续第二账号。

> **2026-08-29 频道浏览每日全账号覆盖与无上限总目标专项修订（当前最高优先级）：** 频道浏览业务按任务自然日计量，采用“按日去重 + 每日全账号滚动覆盖 + 累计目标支持无上限”。因此：
> 1. `ViewRemoteFact` 远端事实为**按日唯一** `(target_peer_id, channel_message_id, account_id, obligation_local_date)`；同一自然日内同一账号对同一消息只记录一次 Gateway 确认，跨天后账号自动恢复资格；
> 2. 每日规划器仅排除“当日已完成/在途”的账号，全库可用账号每日均可参与配置范围内的最新 N 条消息（如 `latest_message_count: 10`）的浏览覆盖；
> 3. 总目标 `per_message_total_view_target` 支持设置为 `0`、`None` 或无上限；有限值是软累计目标，只在新任务日开始时判断是否已达标，未达标则冻结完整当日批次，因此最终累计量允许按一个当日批次粒度超额；达到或超过后，新任务日 target 以 `effective_target=0` 留作审计且不产生 due/Action；API 创建、PATCH 与创建页必须接受显式 `0`；
> 4. 同日跨 Task 并发必须先竞争 `ChannelViewDailyIdentityOwner(peer,message,account,date)`；只有 winner 可以创建并执行 Action。明确未触达 Gateway 的 `pre_gateway` 失败可释放；`call_issued` 默认当日保留，只有该 Action 的**每一条**已启动 Gateway Attempt 都由各自权威 journal/result 明确证明 `remote_mutation_started=false` 时才可释放；任一 Attempt 缺少否定证据或为 `true|unknown`，以及 Owner 已为 `unknown|confirmed` 时，当日均不可释放。完整专项合同见 `docs/03-feature-designs/channel-view-planner-starvation-remediation-prd.md` §1.3。

> **2026-08-19 四类拟人节奏与 AI 内容 current v2 补正（优先于下方 single-Task average gap、统一签到和单 active Provider 旧口径）：** AI/浏览/评论/点赞先按 stable due owner 规划，再以 `tenant+pacing_domain+真实 source+UTC 重叠窗口` 聚合所有 Task 已冻结占用、新需求和 replacement headroom；只有完整通过 source capacity feasibility 才可 start/生成新 period。source release 使用与 due 相同的小时 curve，0 权重不分配，Gateway admission 使用相邻 frozen capacity slots 的 pairwise gap，禁止按单 Task 全天平均值限速。current AI content v2 每 quantity slot 独立走 `GenerationJob.generation_not_before_at + monotonic context revision + route/window/brief + mode Realizer + independent reviewer`；一个 slot 失败不取消同批其他合法 slot，Stage 1/emoji/随机短句/固定“签到”均不得冲抵 current v2 数量。deadline 后每 owner 只能结算一次 typed shortfall，Task.stats 仅作投影。多 Provider 必须先迁完 legacy `is_active` selector，再启用 tenant+purpose route-set。完整合同见 hourly v8、AI 内容 v1.2 主文及两份附录、Planner capacity v2；这些新增合同均为设计完成/待实现，`production_fixed=unproven`。

> **2026-08-17 Planner、拟人排期与内存压力当前合同：** 正式环境当前已不是“整批几秒完成”，但 overdue 技术批次会在 `now` 附近重复起步，且现有 AI final gate 会在 Dispatcher 内等待并可在数据库异常时 fail-open；浏览、评论、点赞缺少等价最终来源闸门。Planner 必须是纯数据库持久化决策角色，频道远程观察只由 Listener 形成带 revision/freshness 的持久快照；Planner 不得持有 TelegramClient/session/update loop。准入、wake 和 active quality blocker 使用独立增量投影，本轮只读 count/revision 与有界公平候选，禁止全账号/全历史 ORM 和 `Task.stats` 大数组/大 map 热行重写。AI/浏览/评论/点赞的 release cursor 按 task/lifecycle/period/source/plan 从 stable owner 跨批单调推进，真实 Gateway call-start 还必须经过无 sleep、失败关闭的 source admission；不新建平行数量 owner。资源限额根据 Planner 自采 cgroup 24 小时工作集公式决定，PSS 只用于归因和优化验收。资源回落、Action success 和容器健康均不能替代 typed remote fact E4。完整合同见 `docs/03-feature-designs/production-planner-pacing-and-memory-remediation-prd.md`；当前仅设计完成，`production_fixed=unproven`。

> **2026-08-20 马来西亚异地备用授权 v2.16 基线（受上方 v2.21 补正）：** 线上现有三套 Developer App 固定为 App A/SV `primary`、App B/SV `standby_1`、App C/MY `standby_2`，分别真实登录三个独立设备，不新增第四套 App。新账号首次登录后即可在详情查看/刷新 Telegram 登录设备；设备清理不做资格预检或倒计时，提交时只读数据库，以 current SV 授权登录时间严格超过 48 小时为可执行条件，不足或缺失即跳过并返回数量和原因。App C 旧 SV 备份通过 MY 全新登录生成新 AuthKey/hash/generation；新 MY Session 必须完成本地卷与独立 SSH 镜像两份不可变副本、恢复密钥 readback、MY 追加 inventory、SSH 镜像隔离 restore probe 和中心 receipt，slot CAS 后旧 SV 仍 retained+protected，恢复闸门通过前不得远端退役。中心库旧备恢复先冻结授权 mutation，并按 MY inventory 最大有效代次只增补回中心。远端撤销、本地/SSH/中心副本和 wrapped DEK 擦除分别回读。其余口径以 v2.21 专项合同为规范真相源。

> **2026-08-16 马来西亚异地备用授权 v2.0 历史基线（受上方 v2.21 当前合同补正）：** `standby_2` 一期固定为 MY 授权灾备资产，马来西亚节点不得领取运营 Task/Action、监听或发送消息。该基线中的 MY 授权激活/current 切换流和单 receipt 退役口径已被 v2.21 禁止，不得作为开发交接。存量迁移、unknown 对账、业务消息边界和通用清理保护仍保留，完整合同见 v2.21 专项及实施验收合同。

> **2026-08-11 AI/浏览到期积压生产止血：** 在 AI stable obligation 与浏览 due-unit 完整原地接管上线前，当前兼容 writer 必须先切断两处线上根因：`fact_first_v3` 的 open `send_message` owner 不得因缺少 legacy `GroupBotAdmission` 被算成 0 并重复物化；AI/浏览已经由 `due_by_now` 释放的当前欠额不得再次套用 Task template、180 秒间隔、max-hour cap 或 future-tail 排期，只执行 quiet-hours、账号硬容量与 ledger 半开 deadline。legacy admission/pacing 语义保持不变。跨 deadline 的 AI pre-Gateway 历史项只能按 exact Task/ledger/Action manifest、SHA-256、actor/approval、compare-and-set、AuditLog 与独立 readback 前向收口；Gateway-started/unknown/typed fact 永不自动重发。完整止血交接及“不等于长期接管完成”边界见 `docs/03-feature-designs/production-due-backlog-containment-prd.md`。

> **2026-08-10 频道浏览 Planner 饥饿补正：** `channel_view` 的required由冻结/append-only `ChannelViewDailyMessageTarget`与稳定due ordinal组成，不再用已选账号的obligation行数代替。route级append-only accrual segments只累计running业务时间，pause/stop不追债，source expire/deadline即使晚处理也按历史as-of冻结。Task级curve只分布软时间，distinct账号不受template 180秒全局串行；新due在各账号同日future Action前/中/后合法空隙绑定，禁止以Task最晚future Action为floor或整批平移。`ViewRemoteFact(peer,message,account,obligation_local_date)` daily unique且一个fact只完成一个Task due unit；其他Task只排除该identity，fact/binding不随owner Task删除。不可匹配unit形成typed structural shortfall，不缩小target。来源由tenant+channel逻辑owner、持久delta/subscription/fanout和target expiry owner推进；完整bootstrap、settlement、lifecycle、fleet/takeover均有crash-replay owner。E4按TargetSet/DueSet/MaterializedSet/Attempt/ViewRemoteFact及各owner backlog分层，obligation-as-required禁止。现有Task以additive schema、final manifest backfill与writer route CAS原地接管，preparing后只能前向修复。完整合同见`docs/03-feature-designs/channel-view-planner-starvation-remediation-prd.md`。

> **2026-08-09 AI 活群生成失败风暴补正：** current `fact_first_v3` 的 coverage 与 extra-volume 都必须先从非空 TaskDayLedger+target operation取得稳定`quantity_ordinal`业务identity、`effective_due_rank/due_at`与`AiGroupMessageObligation`；ordinal永不复用，只有active rank进入当前DueSet，目标缩小时不可撤销高rank转protected overage且不能抵扣低rank缺口。stop的safe pre-call identity原子retire active rank，start-after-stop用更高ordinal重占当前空rank；stop不隐式缩target，未重启到deadline的空rank仍为known shortfall。aggregate content allocation/assignment跨多个最多20条技术批次守住reply/material/act-type，再由带current pointer的immutable intent、variation、GenerationJob、Action和typed remote fact推进。正常正文继续同账号滚动10天去重；精确`签到`改用task/group/account/task-day scoped claim。pre-Gateway duplicate、资格和容量失败在事实basis未变化时进入durable dirty-clock/event或精确时间waiting，同一due unit不创建replacement；deadline后shortfall。Telegram固定prepared commit → committed call-issued/ambiguous hold →事务外call → typed result/fact；remote fact由recovery projector持久重放。`DispatchClaimScope runtime + TaskContractRoute + AI fleet policy/inventory item + task enrollment + task-day route + lifecycle/manifest epoch`采用AND fence；Alembic只建结构，兼容基线全role验证后由受保护workflow building→cutoff scan/barrier/readback→sealed冻结legacy inventory。enrollment一旦preparing永久禁止legacy；首日/跨日ledger+route原子bootstrap。generic retry/reset不得resurrect Action；API以ledger-level read-model revision和whole target-set cursor快照展示。先完成单Task canary，再逐item enrolled/retired并CAS fleet disabled；canary不能冒充整体修复。legacy `TaskGroupDailyMessageSlot/ContentMix`不恢复为真相源，历史通过additive many-to-one alias接管。完整合同见`docs/03-feature-designs/ai-group-generation-failure-churn-remediation-prd.md`。

> **2026-08-09 AI 活群有界批次纠偏（2026-08-10 ownership resync）：** `fact_first_v3` 的“每个Task每轮一次有界物化”指一次Phase A原子事务最多物化20条当前已到期的stable obligation/FOP、allocation assignment与immutable intent，绝不等于固定只物化1条，也不授权Planner为normal body先建pending/空正文Action。normal body逐条转`generation_pending`，由Generation在accepted variation+memory ready后创建Action；deterministic check-in只在scoped claim+memory+intent同事务完整时由Planner创建ready Action，takeover import仅按final manifest例外。服务层只用20条硬事务上限和`daily_coverage_plan_batch_limit`给出一次调用预算，执行器再按due缺口、有效open/unknown owner、distinct ready/online/准入可推进账号和真实Generation/interaction空闲槽收窄。`messages_per_round/max_concurrent/participation_rate/hard-hourly`均不得成为current业务上限；提交后立即轮转其他到期Task。重复/质量/准入失败继续fail-closed并保留同义务等待/回补，禁止放宽质量或制造无界backlog提速。

> **2026-08-08 线上二次纠偏（2026-08-10 current owner resync）：** AI 活群的 Telegram 原生引用只允许绑定同 tenant、同 Task、同目标群且已有bound typed remote fact与非空远端消息 ID 的平台托管历史消息；真人/其他成员消息只能进入生成上下文。引用候选须在规划与生成前各校验一次，候选不足写 `reply_target_shortfall`，不得降级引用真人。群管 surface 在 `get_entity + dialog` 解析后仍返回 “Could not find the input entity” 时，按当前 Task × 账号 × 目标 × 任务日写 `target_entity_unresolvable` 并当日终结该账号准入，不得每 30 秒重启观察，也不得扩大为账号或目标的全局终态。四类互动的 quiet-hours 调整必须保留原计划相邻间距与小时限速，多个时间点不得折叠到静默结束同一秒。存量 Gateway-started 且无权威结果的发送只补 `remote_outcome_unknown + unknown_deadline` 并进入只读对账，禁止重发。浏览终态Action无fact时，只有同request权威pre-transport/no-call-issued证据成立，才可在一个owner-first事务中CAS全局DailyIdentityOwner `pre_gateway->available`并同步终结ActionBinding/Action、把同一due unit恢复可物化；call-issued/unknown绝不重开或释放。点赞按其专项同等级证据执行，任何类型都不得伪造成功或执行过期任务日。

> **2026-08-07 四类互动拟人节奏生产纠偏（2026-08-31 评论 v1.1 补正）：** `fact_first_v3` 只定义 typed remote fact、防重、准入、恢复和 projector，不再等同于“全部义务立即 due”。`group_ai_chat/channel_comment/channel_like/channel_view` 只物化完整 pacing period 中当前累计到期的缺口：AI/浏览按任务自然日，legacy 评论/点赞按来源消息首次采集后的滚动 24 小时；启用 `channel_comment_business_grounding_v1_1` 的新评论消息按 §2.18 从 Telegram `source_published_at` 起冻结三天周期，Listener 晚采集不顺延、不追赶。legacy `pacing_anchor` 仍取 period、任务实际启动/恢复和来源采集时间的较晚者，v1.1 评论以发布时间和剩余曲线为准。容量不足写 shortfall，禁止 Planner、takeover 或 Recovery 把 future Action 批量改成当前时间或在日末压缩追赶。本文中与此冲突的“四类互动资源空闲即清空、不计算 due_by_now”统一为 `historical_do_not_implement`；纯搜索点击不在本次节奏变更范围。专项合同见 `task-fulfillment-classified-recovery-prd.md` §4.5。

> **2026-08-04 分类履约最终合同优先级（2026-08-10范围修订）：** 通用合同由分类与闭合PRD组成，但`group_ai_chat`、`channel_view`分别由本页2026-08-09/10顶部专项修订覆盖。冻结分母、中央Window/预扣、验证码模型投票、仅凭远端当前不存在重开unknown等旧述仍是historical。当前实现使用任务内动态范围、task-type DueSet、稳定unit identity、typed remote fact/binding、interaction/search独立lane、持久search phase、唯一active Provider key、C2 observation surface与永久unknown只对账。`prepared新Task从0+删旧Task`只适用于仍明确采用该release-train的其他任务；AI活群与频道浏览都在全role兼容fence后对原Task执行inventory/manifest/backfill/readback/route原地接管，保持原lifecycle并保留既有事实。

> **2026-08-04 生产切换闭环补充：** Planner 对每个 `fact_first_v3` Task 每轮只物化一个可直接执行批次，随后轮转其他到期 Task；`messages_per_round=40/50/60` 不是全局串行循环次数，禁止单个 4000/5000 目标 AI Task 阻塞搜索或其他任务。旧 Task 删除只集合快照 typed remote/unknown 防重候选并批量写 tombstone，普通 pending/failed/skipped 历史直接随 Task 级联删除；Action/Attempt 子表外键必须有删除热索引，禁止逐 Action 的 N+1 快照、验证或删除。

> **2026-07-31 AI 活群真人化修复口径：** 跨群内容 scope、单 Action late binding、过期同槽重生成、会话质量验收与签到边界统一引用 `docs/03-feature-designs/ai-conversation-humanization-and-group-bot-admission-prd.md` §15；旧批量预写和缺少 scope 限制的 fallback 文字不再作为实现依据。

> 基于 `docs/01-product/tg-ops-platform.md` 拆出的详细产品需求文档。
> 本文用于描述整体功能、页面按钮、业务流程、状态机、数据表、数据流转、执行器、规划器和验收口径。
> 当前日期口径：2026-07-28（Asia/Shanghai）。数据库字段与接口以当前代码为准；已标记 planned 的 2026-07-28 全任务履约修复以专项 PRD 约束后续实现，不能误写为当前已上线能力。

---

## 1. 文档目标

### 1.1 目标

- 把 TG 运营管理平台从总纲设计落到可研发、可测试、可交付的 PRD。
- 明确每个页面的功能、按钮、弹窗、表格、状态和异常处理。
- 明确从账号接入、目标确认、规则配置、任务创建、执行、监听、归档、数据和审计的完整数据流。
- 明确核心数据表、状态字段和表间关系。
- 明确 Planner、Dispatcher、Listener、Recovery、Metrics 等执行器职责。
- 明确当前已实现、应继续收敛和后续增强的边界。

### 1.2 非目标

- 不替代数据库迁移文件和 ORM 模型。
- 不替代接口 schema 文档。
- 不继续沿用旧 Campaign、多租户 SaaS、卡密、订阅套餐作为产品主线。
- 不把 Telegram 官方限制包装成平台可绕过能力。

### 1.3 读者

| 角色 | 关注点 |
| --- | --- |
| 产品 / 交付 | 功能边界、页面流程、按钮和异常提示 |
| 前端 | 页面结构、弹窗、表格、权限和交互状态 |
| 后端 | API、数据表、状态机、执行链路和审计 |
| 测试 | 主流程、异常流、状态流转和回归范围 |
| 运维 | worker、队列、容量、指标、故障恢复 |

---

## 2. 产品总览

### 2.1 产品定位

TG 运营管理平台面向 Telegram 运营团队，用一个后台统一管理：

- TG 账号接入、资料初始化、二步密码托管和登录设备清理。
- 运营目标群、频道、讨论组和联系人。
- 消息发送、AI 活跃群、转发监听群、频道浏览、频道点赞、频道评论/回复。
- 规则、风控、素材、AI 配置、监听、执行、归档、数据和审计。

### 2.2 核心业务闭环

```text
系统初始化
  -> TG 账号接入
  -> 资料初始化 / 二步密码 / 登录设备清理
  -> 资产同步
  -> 任务内确认或创建运营目标
  -> 规则与风控配置
  -> 任务结构校验并直接创建
  -> 启动后建立运行账本与 blocker
  -> Planner 规划 Action
  -> Dispatcher 执行 Telegram 动作
  -> Listener 采集上下文和源事件
  -> Recovery 修复异常状态
  -> Metrics 生成运行快照
  -> 运营数据、归档、审计复盘
```

### 2.3 当前实现导航与目标导航

当前前端导航以 `frontend/src/app/AppShell.tsx` 和 `frontend/src/app/routes.ts` 为实现基准；本 PRD 定义统一重构后的目标口径。重构前不要求立即新增一级菜单，重构时必须保证当前路由可以无损迁移。

当前实现到目标口径的映射：

| 当前菜单 | 当前路由 | 目标菜单 | 目标路由 | 处理口径 |
| --- | --- | --- | --- | --- |
| 运营概览 | `/dashboard` | 运营中心 | `/dashboard` | 文案从概览升级为工作台，承载目标状态、运营方案和异常入口 |
| TG账号管理 | `/accounts` | TG账号管理 | `/accounts` | 保持一级菜单，补齐账号可用性、安全、批次和验证码能力 |
| 运营目标 | `/targets` | 运营目标 | `/targets` | 保持一级菜单，承载目标能力和准入处理 |
| 新增 | `/target-profile` | 目标画像 | `/target-profile` | 新增一级菜单，承载全站唯一 AI 画像、学习来源和样本治理 |
| 消息发送 | `/message-sending` | 消息发送 | `/message-sending` | 保持一级菜单，作为手动发送和小批量发送入口 |
| 任务中心 | `/task-center` | 任务中心 | `/task-center` | 保持一级菜单，只负责执行详情、失败事实和调度控制 |
| 监听中心 | `/listeners` | 监听中心 | `/listeners` | 保持一级菜单，承载源事件采集和水位 |
| 规则中心 | `/rules` | 规则中心 | `/rules` | 保持一级菜单，承载规则集、版本和测试 |
| 风控中心 | `/risk-control` | 风控中心 | `/risk-control` | 保持一级菜单，承载策略、评分和处置 |
| 归档中心 | `/archives` | 归档中心 | `/archives` | 保持一级菜单，承载冷数据检索 |
| 运营数据 | `/usage-reports` | 运营数据 | `/usage-reports` | 保持一级菜单，读取汇总报表 |
| 系统设置 | `/system-config` | 系统设置 | `/system-config` | 保留为平台底座；TG 开发者应用、AI 供应商、提示词、素材运行配置、Clash 订阅源池、后台账号权限作为 Tab；账号面具从系统设置拆为一级菜单 |
| 系统设置 / 旧提示词与素材 Tab | `/system-config` | 素材中心 | `/materials` | 素材从系统设置拆为一级运营资产；旧 Tab 可保留跳转或兼容入口 |
| 审计记录 | `/audit` | 审计记录 | `/audit` | 保持一级菜单，承载操作留痕和导出 |
| 操作手册 | `/manual` | 操作手册 | `/manual` | 保持一级菜单，随 PRD 和前端真实能力同步 |

目标导航为：

| 菜单 | 路由 | 权限 | 说明 |
| --- | --- | --- | --- |
| 运营中心 | `/dashboard` | `overview.view` | 目标工作台、运营方案、异常处理、任务失败聚合和效果复盘 |
| TG账号管理 | `/accounts` | `accounts.view` | 账号登录、同步、安全、资料、分组、代理和恢复 |
| 运营目标 | `/targets` | `targets.view` | 群、频道、讨论组、联系人等可运营对象 |
| 目标画像 | `/target-profile` | `target_profile.view` | 全站唯一 AI 画像、学习来源、历史拉取、样本治理和版本 |
| 消息发送 | `/message-sending` | `message_sending.view` | 手动消息发送和批量发送 |
| 任务中心 | `/task-center` | `tasks.view` | 执行详情、Action 明细、失败事实、调度控制和高级手动创建 |
| 监听中心 | `/listeners` | `listeners.view` | 群/频道监听状态、账号和事件 |
| 规则中心 | `/rules` | `rules.view` | 规则集、规则版本、测试和命中 |
| 风控中心 | `/risk-control` | `risk.view` | 策略、评分、代理、处置队列 |
| 归档中心 | `/archives` | `archives.view` | 群消息、成员、上下文归档 |
| 运营数据 | `/usage-reports` | `usage.view` | 任务、账号、目标、AI 用量和失败统计 |
| 素材中心 | `/materials` | `materials.view` | 表情包库、头像包、图片 / 文件 / 组合消息、素材分组、批量上传和缓存健康 |
| 账号面具 | `/account-masks` | `account_masks.view` | 账号面具、账号代理绑定、授权指纹配置、异常与审计；按账号 + TG 开发者应用 + 授权槽位管理对外表现和环境绑定 |
| 系统设置 | `/system-config` | `system.view` | TG 开发者应用、AI 供应商、提示词、素材运行配置、Clash 订阅源池、后台账号权限 |
| 审计记录 | `/audit` | `audits.view` | 操作审计、筛选和导出 |
| 操作手册 | `/manual` | `manual.view` | 管理员内置操作说明和最近更新 |

素材中心必须作为独立一级菜单。系统设置不再承载素材日常管理，只保留素材缓存账号、缓存会话、上传限制、临时文件 TTL 等运行配置。消息发送、规则中心、任务中心、账号资料初始化只引用素材中心资产。

### 2.4 角色和权限

| 角色 | 默认能力 |
| --- | --- |
| 平台管理员 | 全部菜单、系统配置、权限、开发者应用、AI、风控和审计 |
| 运营主管 | 运营中心、目标、消息、任务、监听、规则、风控、归档、数据和审计查看，部分风控处置 |
| 运营人员 | 创建和维护任务、处理日常异常、消息发送、查看执行结果和操作手册 |
| 账号添加专员 | 账号新增、登录、同步和基础状态检查 |
| 只读观察员 | 运营中心、运营数据、操作手册、审计查看 |

权限分为：

- 菜单权限：控制导航入口，例如 `accounts.view`。
- 按钮权限：控制动作入口，例如 `accounts.create`、`tasks.manage`、`rules.publish`。
- 后端写接口权限：前端隐藏按钮不能替代后端校验。
- 审计权限：敏感导出和危险动作必须留痕。

#### 登录后资源加载边界

- 平台登录成功后，前端必须先读取 `/api/auth/me` 的有效权限集，再请求受保护资源；不得因为全局首屏刷新而调用当前用户无权读取的接口。
- `/api/config/runtime` 是 `system.view` 的系统诊断资源。只有具备 `system.view` 的用户可以请求和展示它；无此权限的快照将其表示为 `null`，不得弹出“后端未连接或接口异常”。
- 拥有 `accounts.create` 但没有 `system.view` 的用户，必须通过仅返回 `can_create_tg_account` 的账号创建能力接口读取新增前置状态；该投影不得暴露应用环境、队列、TG 网关、开发者应用数量或 AI 健康诊断。
- `账号添加专员` 保持 `accounts.view`、`accounts.create`、`accounts.login` 和 `accounts.sync` 的最小能力，可以进入 TG 账号管理并完成新增、登录与同步；不得因缺少系统设置权限而被阻塞。
- 后端对直接越权请求仍返回 403 并写权限拒绝审计。前端按权限省略请求不替代后端守卫，也不授予 `system.view` 作为绕过手段。

按钮级权限矩阵：

| 权限点 | 控制范围 | 敏感 / 审计要求 |
| --- | --- | --- |
| `overview.view` | 运营中心目标工作台、方案摘要、异常列表 | 只读，不暴露原始 payload |
| `operation_plans.manage` | 创建、编辑、暂停方案，生成任务草稿，生成并启动，应用到关联任务 | 应用到运行中任务前必须影响预览、二次确认并写审计 |
| `operation_issues.manage` | 确认处理、忽略、标记解决、上下文处理和深链处理运营异常 | 忽略和标记解决必须填写原因；写入 `source_issue_id` |
| `targets.manage` | 同步目标、修改目标能力、处理准入失败、带目标创建任务或发送消息 | 能力调整和准入重试必须写原因和审计 |
| `target_profile.manage` | 配置全站唯一 AI 画像、学习来源、监听账号、样本质量规则、历史拉取、样本采纳 / 降权 / 剔除、候选重算、重建和回滚 | 学习来源变更、质量规则变更、画像清空、版本回滚和样本状态调整必须写原因和审计 |
| `accounts.view` | 账号列表、账号详情基础信息 | 手机号按完整字段展示，缺失时才使用历史兼容字段 |
| `accounts.batch_login` | 创建、取消、刷新凭据及重试批量登号批次 | 同时要求 `accounts.login`；创建/取消/刷新/重试均写原因、版本与审计 |
| `accounts.code_source_credentials.read` | 查看账号接码备注对应的完整第三方 UUID | 同时要求 `accounts.view`；每次 reveal 二次确认、填写原因、no-store 并写账号/操作者/trace_id 审计 |
| `accounts.security.read` | 查看安全快照、2FA 状态、登录设备 | 查看敏感状态写审计 |
| `accounts.security.batch` | 设置二步密码、清理外部设备、备用 session 补齐、重试 / 取消安全批次 | 危险动作二次确认并写批次审计 |
| `accounts.security.session_manage` | 手动补齐、切换、停用和自愈账号授权 session | 主备切换、备用登录、自愈恢复和停用授权必须写审计 |
| `accounts.security.credential_manage` | 配置、轮换平台托管 2FA 密码策略 | 保存、轮换、查看、导出和自动登录使用都必须写审计；接码专用账号只允许查看 / 复制已托管密码，不允许保存或轮换 |
| `accounts.profile.batch_update` | 批量资料初始化、AI 预览、手工编辑资料 | 写入批次、预览结果和操作者 |
| `accounts.authorizations.manage` | 新增、重登、切换、停用账号主备授权资产 | 主备切换、备用登录和停用授权必须写审计；没有备用授权时只能提示补齐，不阻塞现有账号 |
| `account_masks.view` | 查看账号面具一级菜单、账号面具列表、账号代理绑定、授权指纹状态和异常审计 | 只读入口不得暴露完整代理订阅 URL、节点密钥、session 或 API Hash；手机号展示遵循账号链路完整手机号口径 |
| `account_environment.manage` | 按账号 + TG 开发者应用 + 授权槽位配置代理和授权指纹 | 保存、解绑、批量修改、重排代理、修改指纹、刷新观测状态都必须写审计；代理绑定、出口审计、授权指纹绑定和远端观测粒度都必须落到 `account_id + developer_app_id/api_id + authorization_id/session_role`；缺授权槽位代理、出现同槽位多 active 代理或缺指纹时任务执行必须 fail closed，不得回退本机直连 |
| `accounts.sensitive.read` | 查看完整 session 相关敏感状态、托管密码状态、代理绑定细节 | 必须输入原因，禁止默认导出明文秘密 |
| `accounts.codes.read` | 查看 / 复制 TG 官方验证码、轮询验证码任务 | 二次确认，写查看原因、账号、trace_id |
| `message_sending.manage` | 发送预检、保存草稿、提交发送、取消、重试、派发 | 发送、取消和重试必须写发送批次审计 |
| `tasks.manage` | 创建、编辑、启动、暂停、继续、停止、重试、删除任务 | 删除、停止必须二次确认并填写原因；本次五类履约任务中，AI 活群、评论、点赞、浏览的创建与创建并启动只需要本权限，不新增四类任务的专项创建权限 |
| `tasks.create.search_click` | 创建和启动纯搜索点击任务 | 只对 `task_type=search_click + search_execution_mode=click_only` 生效；创建只校验同用户目标/账号组引用、字段与静态合同。授权槽位、代理、环境栈、decoy、健康和灰度账号事实在 Task 启动成功后进入运行投影，不阻止创建或把 start 误报失败。旧 `tasks.create.search_join_group` 只用于识别待删除旧 Task，旧创建路由固定 410，不能授权、迁移或代建新任务 |
| `tasks.create.search_rank_deboost` | 创建和启动搜索排名观察任务 | 只对 `search_rank_deboost` 生效；创建只校验静态合同。账号用途、分组、持久代理绑定、当前出口、随机豁免群、Gateway contract、协议样本和逐点击配额在启动后评估；缺失时 Task running 且 `runtime_state=waiting` |
| `tasks.membership.manage` | 准入子任务暂停 / 继续、账号级重试准入、重新检测可发言、跳过账号、标记人工处理 | 账号级操作必须写原因、父任务、子任务、目标、账号、来源页面和 trace_id |
| `tasks.membership.challenge.read` | 查看验证问题、AI / MiMo 答案、置信度和原始验证结果 | 敏感查看必须写审计；无权限时只展示阶段和失败类型，不展示问题原文和答案 |
| `tasks.membership.challenge.handle` | 人工录入验证答案、确认重新自动尝试、关闭低置信验证 | 必须二次确认并写处理原因；人工答案不得进入明文导出 |
| `tasks.dispatch_control` | 手动 drain、恢复 stuck action、重置 / 重排计划 | 限平台管理员，必须记录原因和影响范围 |
| `listeners.manage` | 切换监听账号、重置监听水位、处理监听异常 | 重置水位必须二次确认并写审计 |
| `rules.publish` | 发布、回滚、复制规则版本 | 发布和回滚必须写版本差异和审计 |
| `rules.manage` | 新建规则集、编辑草稿、测试规则、任务级来源过滤调整 | 不得静默修改已发布版本；运行中 override 必须写来源任务 |
| `risk.manage` | 修改风控策略、处置账号 / 代理 / 目标风险 | 解除限制、策略修改必须二次确认 |
| `archives.manage` | 创建归档、重新归档、查看详情、导出归档 | 导出必须填写原因并写文件标识 |
| `usage.export` | 运营数据导出和跨维度报表详情 | 大范围导出异步执行并写审计 |
| `materials.upload` | 单个或批量上传素材、头像包、表情包 | 大文件和批量上传进入异步处理，写素材审计 |
| `materials.manage` | 编辑、禁用、版本化素材，维护标签和分组 | 被引用素材不能物理删除，只能禁用或新增版本 |
| `developer_apps.manage` | 新增、编辑、检查、启用、禁用 TG 开发者应用 | 停用已绑定账号的应用必须提示影响范围 |
| `ai.manage` | AI 供应商、默认模型、黑话配置和健康检查 | API Key 等敏感字段禁止默认明文展示 |
| `ai_voice_profiles.manage` | 查看、搜索、编辑、重建和停用账号面具 | 账号面具是账号级全局资产，修改影响该账号参与的所有 AI 活跃群；该权限必须能在后台用户权限弹窗分配，写接口必须同时被权限中间件和路由显式守卫；保存、重建和停用必须写审计。技术权限名暂保留 `ai_voice_profiles.manage`，避免破坏已有权限和审计数据；菜单入口迁移到“账号面具”一级菜单，不再放在系统设置 |
| `prompt_templates.manage` | 新增、编辑、发布、测试提示词模板 | 提示词变更写版本和审计 |
| `proxies.manage` | 代理新增、编辑、检查、禁用、绑定和批量绑定 | 代理归属风控中心；禁用和批量绑定必须写影响范围 |
| `system.manage` | 平台运行配置、素材运行配置、Clash 订阅源池、后台账号权限入口 | 影响全局运行的配置必须健康检查或回滚提示；Clash 配置读取需要 `system.view`，保存、测试、同步和调整优先级需要 `system.manage`；Clash 订阅地址必须加密保存，普通日志和前端列表只展示脱敏摘要 |
| `users.manage` | 后台用户、额度、菜单和按钮权限 | 权限变更更新 `permission_version` 并写审计 |
| `audit.export` | 审计导出、敏感操作检索 | 必须填写导出原因，记录筛选条件和文件标识 |

### 2.5 最近更新口径

前端操作手册必须展示近期已落地能力，避免运营人员只在研发文档里看到变更：

> **当前阅读顺序：** 本表“搜索目标群点击任务”保留 2026-07-21 旧功能长描述用于迁移审计，其中创建前协议/容量预检、成员关系替代 click、行为 skip 优先和运营配置内部容量字段均已失效；必须应用该行后的最新 2026-07-28 supersede，并以 §2.18 和搜索点击专项 PRD 为实现合同。

| 更新项 | 手册展示口径 |
| --- | --- |
| 账号安全加固 | 在 TG 账号管理中说明账号详情的账号安全页、同步设备和 2FA 状态、清理外部设备、设置 2FA、最近安全批次结果 |
| 批量资料初始化 | 在 TG 账号管理中说明先点“资料初始化”再选择账号；账号列表必须拉取当前分组完整结果后再交给 AntD 表格分页展示，不能把接口默认第一页当全量；支持按账号组、筛选、搜索、跨页勾选、区间选择、一键选择资料待初始化账号和一键选择需重新资料初始化账号；备用 session 缺失、备用 session 未登录、健康备用 session 不足 2 个、可从备用 session 激活恢复等筛选也必须在账号管理中可见；一次 AI 批量生成或手工编辑昵称、TG 姓名、username、简介、头像等资料预览，弹窗确认后按批次执行；昵称 / TG 姓名必须同时更新平台展示名和 Telegram 远端 `first_name` / `last_name` |
| 登录后自动资料初始化 | 验证码登录或扫码登录成功后，系统必须检查该账号是否已具备中文展示名、中文 TG `first_name`、`username` 和头像；未满足时自动创建已确认的资料初始化批次，使用本地中文随机昵称生成和素材中心随机头像池，由 `account-security` worker 执行并在任务中心展示结果；已有中文资料和头像的账号不得重复创建批次 |
| 任务内目标输入 | 在任务中心说明可选择已有目标，也可直接粘贴群聊 / 频道 `@username`、公开链接、邀请链接或 peer id |
| 账号-目标准入前置 | 在任务中心说明频道浏览、点赞、评论和 AI 活跃群启动前先检查账号是否已关注 / 已加入；未满足账号先按抖动节奏关注或加入，成功后才进入主互动 |
| 搜索点击任务 | 第 6 类主任务，用户可见名称和业务身份固定为 `task_type=search_click + search_execution_mode=click_only`，正式接口/权限为 `/api/tasks/search-click + tasks.create.search_click`；旧 `search_join_*` 只作待删除旧 Task 识别和历史读取，旧创建路由返回 410，不迁移、不代建当前任务。运营只配置公开目标群、关键词、`daily_click_target_count`、账号组和截止时间；结构合法即创建，账号、代理、授权槽位、协议/CAPTCHA 与安全事实在启动后持续刷新。开放义务在 search worker 有真实空闲槽时直接产生 assignment 并执行，不计算软节奏、Window、预扣或预计容量。执行只包含第三方索引机器人搜索、翻页、精确目标匹配和批准点击；同一 ExecutionAttempt 具备完整 `target_click_observed` 后终结义务 UUID，不加入目标群、不创建 membership/admission/can-send child、不联动后续任务。排名观察是独立复盘事实，不计 click success；外部 HTTP URL 与未知 callback 不执行；极搜 page state 偏离只记录 blocker，不发送 reset。详细合同以 `search-click-daily-fulfillment-remediation-prd.md` 为准。“搜索点击加入”仅登记为后续独立模式，本轮不设计。 |
> 2026-08-04 搜索点击协议说明：`@jisou` 的关键词回复必须先按已审批样本进入“群聊 / 群组”结果类型；固定 70 页只代表旧 payload 的兼容字段，不是搜索或任务终止条件。只有精确公开 username 命中、批准目标按钮被真实调用、远端 click outcome 可确认且 `membership_side_effect=none` 时才完成当前 `SearchClickObligation` UUID；真实没有下一页时记录实际页码和 `no_next_page`，当前 Attempt 失败但任务继续补量。
> 2026-07-28 最新更新：用户可见“搜索点击”固定为纯 click；`target_click_observed` 是唯一完成事实，确认后不得创建 membership/admission/can-send 子 Action。`join_target_group_after_click` 与 `daily_admission_target_count` 不再是纯搜索点击写入字段。“搜索点击加入”只登记为后续独立任务模式，本轮不设计、不实现。
> 2026-07-28 完成优先更新：旧“创建前通过协议、容量与节奏预检”和“行为闸门优先于补量”只作历史实现说明。当前创建只校验权限、公开目标引用、账号组引用、数量字段和合同结构；协议样本、代理/授权、账号容量、验证码与节奏在启动后评估。安全额度和 Gateway 边界仍为硬约束；skip、jitter、曲线与静默只影响系统排序，不能阻止结构合法任务创建或令日目标静默欠量。

| 频道评论异常归因 | 在异常处理中说明频道帖子无法解析到讨论区、频道未绑定讨论组时归因为“该消息无法评论”；账号未关注 / 未加入时先补准入，账号已准入但仍不可评论时展示账号级评论权限异常 |
| 运营方案模板 | 在运营中心说明方案模板位于目标工作台下方，可生成任务草稿、生成并启动、调整关联任务，并且调整前必须展示影响预览 |
| 任务创建动态向导 | 在任务中心说明 5 步创建向导按任务类型动态展示静态字段、目标输入和确认页；账号容量、准入、传输、协议与风险事实在创建并启动后的任务详情展示，不作为创建前置 |
| AI 活跃群群日目标 | 在任务中心只配置每群每日发送总量，并展示本任务当日动态必达账号覆盖；可恢复账号自动回流，不可恢复账号当日放弃。删除小时硬目标、日容量阻断和活动时段禁发，多任务公平并发，静默期只降量。验收以完整自然日群日确认数、动态必达账号覆盖和远端消息 ID 为准 |
| 数据汇总与延迟 | 在运营中心、任务中心和运营数据说明首页读汇总模型，详情按 ID 下钻；汇总延迟时显示最近更新时间和刷新入口 |
| 页面数据加载契约 | 运营中心、运营数据、任务中心、TG 账号管理和风控中心只加载当前页面必要数据；账号、审计、归档、消息任务和系统配置不得作为全局快照随所有页面加载；核心页面切换请求数减少至少 50%，详情继续按需下钻 |
| 导航升级 | 手册菜单名必须使用“运营中心”，并说明素材中心和账号面具是一级菜单；AI 供应商、提示词、素材运行配置、Clash 订阅源池和后台账号权限位于系统设置 Tab |
| 账号面具管理 | 在“账号面具”一级菜单说明账号面具是账号级全局对外表现设定；同菜单支持面具管理、账号代理绑定、授权指纹配置、异常与审计四类 Tab；支持按账号名、username、手机号后四位、状态、TG 开发者应用、授权槽位、代理节点、指纹一致性和更新时间搜索，支持查看、编辑、重建、停用、版本回滚和审计查看 |

> 2026-07-21 生效说明（2026-07-28 最新增量）：纯搜索点击的新建页及 API 只接收 `search_execution_mode=click_only`、目标群、关键词、`daily_click_target_count`、账号组、截止时间、抖动和可选静默；join switch、admission 目标或成员目标返回 422。代理、机器人、单账号风险、停留、重试和资源准备仍由系统托管。截止后不再规划新 Action，未完成不伪造成功。

### 2.6 2026-05-21 更新记录

- PRD 日期口径更新为 2026-05-21，并把今天的同步范围明确为主设计文档、前端操作手册、账号安全专项、频道任务执行异常和当前代码差异。
- 任务内目标输入继续作为新版任务创建主入口：运营人员创建任务时可以选择已有目标，也可以粘贴群聊 / 频道入口，由后端解析并自动创建或复用 `operation_targets`。目标输入只允许在创建任务时使用；编辑任务只能调整已有目标、账号范围、规则和执行参数，不能再通过编辑弹窗新建目标。
- 账号-目标准入前置统一为“先检查账号是否已关注 / 已加入；未满足账号先关注或加入；成功后才进入主互动”。已满足账号不等待未满足账号，准入成功账号追加进入后续主互动容量。
- 频道评论/回复补充异常归因：当 Telethon 返回 `GetDiscussionMessageRequest`、`DiscussionMessage` 或 “message ID used in the peer was invalid” 等讨论区解析错误时，后端应映射为 `COMMENT_UNAVAILABLE`，并提示确认消息 ID 属于频道帖子、频道已绑定讨论组、执行账号可进入讨论组并评论。
- 手机号不脱敏展示本轮覆盖所有涉及账号或联系人的运营链路：账号列表/检索、联系人、消息发送账号与私聊对象、归档成员与消息发送人、风控账号健康分、账号安全批量预览、审计记录和导出日志均优先使用完整 `phone_number`；`phone_masked` 仅作为历史数据缺失完整手机号时的兼容兜底。
- 执行层文档口径保持“已有 planner / dispatcher / listener / recovery / metrics 分角色 drain 能力，但生产多进程拆分、并发配额、token 预留 / 退款、容量面板和压测结论仍需确认”。
- 系统设置和数据模型中的 `tenant_*` 表名仅代表当前代码表结构，不再作为多租户 SaaS、卡密或订阅套餐的产品主线表达。

### 2.7 2026-05-22 更新记录

- 运营中心、任务中心、素材中心和系统设置关系定为：运营中心是日常工作台、运营方案和异常处理入口；任务中心是执行详情、失败事实源和调度控制台；素材中心是表情包、头像包和媒体素材资产管理入口；系统设置只维护平台底座能力。
- AI 活跃群定为“真人接话为主、空闲低频暖场为辅”，补充事实锚点、语义去重、幻觉拦截、低置信沉默和质量留痕。
- 数据库压力治理先不做分库分表，优先采用短事务、汇总读模型、冷热数据边界和按需下钻。
- 本轮继续把模糊流程改成可验收流程：明确数据流转、数据展示、任务执行、失败上卷、读写边界和页面下钻契约。
- 补齐账号中心作为“账号资产与维护中心”的 PRD 口径，并新增全量前端页面设计章节；后续统一重构以前端页面设计、数据流转和执行流程共同作为验收基准。

### 2.8 账号面具、Clash 和授权环境入口更新（2026-07-04 收口）

- “账号面具”升级为一级菜单，承载账号对外表现和授权环境配置。一级菜单下按 Tab 分为“面具管理 / 账号代理 / 授权指纹 / 异常与审计”，不再放在系统设置里。
- 系统设置新增“Clash 配置”Tab，维护租户级多条 Clash 订阅源，作为平台级代理节点来源。系统设置支持新增 / 编辑 / 禁用 / 删除订阅源、设置主备优先级、保存、连通性测试、同步节点、显示最近同步时间、节点数量、健康节点数量和失败原因；读取需要 `system.view`，保存 / 测试 / 同步 / 调整优先级需要 `system.manage`；不在系统设置里分配授权槽位代理。
- Clash 订阅地址必须逐条加密保存。前端列表、审计摘要、任务 stats 和普通日志只能展示脱敏摘要、订阅名称、主备优先级、节点数量、同步状态和失败原因，不得输出完整 URL、token、节点密码或 URI 原文。
- Clash 配置页每条订阅必须明确展示“配置已保存 / 节点同步中 / 节点同步成功 / 节点同步失败”四类状态。保存成功但同步失败时，只能提示该订阅地址已更新且节点不可用，不能把它当作代理池可用；同步失败必须展示可读失败原因和重试入口。
- Clash 订阅“测试 / 同步”必须真实拉取订阅、自动识别 Base64 URI 列表 / Clash YAML / JSON、过滤套餐/流量伪节点，并把解析出的节点写入 `proxy_airport_nodes`；但“已解析节点”不等于“健康节点可用”，只有完成后续出口探测并产生健康节点数后，才能作为授权槽位代理候选。
- 多订阅容灾按 `priority` 从小到大选择：主订阅健康节点可用时优先使用主订阅；主订阅解析失败、健康节点为 0、该订阅节点不可用或出口探测失败时，自动使用下一条启用的备用订阅。已绑定授权槽位不因主备状态变化而立即批量换节点；只有当前绑定节点不可达、节点被移除、出口异常、订阅不可用或人工重排时才切到备用订阅健康节点，并写 `proxy_node_failover_events`。切换订阅或节点后，该授权槽位重新进入 warmup。主订阅恢复后默认不自动切回，避免账号出口频繁变化；如未来需要自动切回，必须配置开关、冷却时间和影响预览。
- 单个账号代理配置在“账号面具 > 账号代理”完成，绑定粒度为 `account_id + developer_app_id/api_id + authorization_id/session_role(primary/standby_1/standby_2)`。同一账号在不同 TG 开发者应用、不同 session key 和主/备用授权槽位下可以使用不同代理节点；每个授权槽位一旦绑定必须长期固定并可审计。
- 授权指纹配置在“账号面具 > 授权指纹”完成，绑定粒度同样为 `account_id + developer_app_id/api_id + authorization_id/session_role(primary/standby_1/standby_2)`。同一账号在不同 TG 开发者应用和不同授权槽位下可以使用不同客户端元数据；缺失或重复时当前 `search_click` 等真实执行任务必须 fail closed。
- 修改授权指纹配置只影响下一次使用该授权槽位建立连接、重登或新 session 初始化时上报的 MTProto 客户端元数据；不能把配置保存成功描述成 Telegram 远端授权设备型号已经立即改变。
- 授权指纹页必须把“配置指纹”和“远端观测指纹”分列展示，并标出 `not_connected / pending_effect / observed_matched / observed_mismatch / unobservable`。`pending_effect` 表示配置已保存但现有 Telegram 授权设备尚未通过重登或新连接重新观测；`unobservable` 表示 Telegram 授权设备快照缺少可比对字段，页面必须展示缺失字段，不能误判为一致；`observed_mismatch` 只能提示重登、刷新授权或人工检查，不能自动修改远端授权。
- 批量更新账号授权指纹只能批量写入配置和审计，不能宣称所有 Telegram 远端授权设备已被立即批量改名 / 改型号；批量结果必须按授权槽位返回 `configured / pending_effect / observed_matched / observed_mismatch / unobservable / failed`。
- “账号面具 > 异常与审计”必须集中展示配置缺失、配置冲突、代理健康、指纹一致性、远端观测差异和最近变更；运营从这里处理环境问题，任务中心只展示执行阻断事实和深链入口。

#### 2.8.1 入口与字段契约

“账号面具”不是单一的人设编辑页，而是账号对外表现和授权环境的统一工作台。页面必须按四个 Tab 拆分，避免运营把人设表达、代理出口和 Telegram 授权设备观测混成同一个配置。

| Tab | 维护对象 | 必填 / 展示字段 | 允许动作 | 不允许动作 |
| --- | --- | --- | --- | --- |
| 面具管理 | 账号级对外表达资产 | 账号、状态、面具名、身份框架、偏好标签、短摘要、版本、生成来源、最近更新时间 | 查看、编辑、批量生成、批量重建、停用、恢复、版本回滚、审计查看 | 直接修改代理、授权槽位、session 或远端设备 |
| 账号代理 | 授权槽位级代理绑定 | `account_id`、TG 开发者应用、`api_id`、`authorization_id`、`session_role`、代理节点、真实出口 IP、健康状态、warmup、最近故障切换 | 单槽位绑定 / 解绑、批量重排代理、查看出口观测、跳转风控处置 | 保存全局 Clash 订阅、绕过代理直连、把一个槽位绑定多个 active 代理 |
| 授权指纹 | 授权槽位级 MTProto 客户端元数据 | 配置指纹、远端观测指纹、一致性状态、缺失字段、最近刷新时间、最近配置人 | 单槽位修改、批量补齐配置、刷新远端观测、提示重登 / 新 session 初始化 | 宣称远端授权设备已立即变更、自动静默重登、用指纹掩盖 API ID / session 不一致 |
| 异常与审计 | 环境阻断事实和变更记录 | 缺代理、缺指纹、多 active 代理、多 observed exit IP、`pending_effect`、`observed_mismatch`、`unobservable`、审计摘要 | 筛选、批量定位、深链到账号代理 / 授权指纹 / 风控中心 / 任务详情 | 伪造已处理、隐藏失败、把配置缺失当作任务成功 |

账号代理和授权指纹的统一绑定键为：

```text
tenant_id + account_id + developer_app_id/api_id + authorization_id + session_role
```

`session_role` 只允许 `primary / standby_1 / standby_2`。同一账号在不同 TG 开发者应用、不同 session key 和不同授权槽位下可以使用不同代理和不同客户端元数据；但同一授权槽位只能有一个 active 代理、一个配置指纹和一个当前可比较的远端观测状态。

#### 2.8.2 Clash 多订阅主备与分阶段状态

系统设置只负责租户级 Clash 订阅源池，不能承担账号代理分配。系统设置的 Clash 页面按订阅源展示“保存、解析、同步、健康探测”四个阶段，并额外展示主备优先级、启用状态、当前主用 / 备用状态、故障原因和最近 failover 时间；“槽位绑定”是完整代理链路的第五阶段，只能在“账号面具 > 账号代理”展示和操作，不能回到系统设置。

| 阶段 | 成功含义 | 页面状态 | 不能宣称 |
| --- | --- | --- | --- |
| 保存 | 订阅地址已加密写入 `proxy_airport_subscriptions`，并带 `priority / enabled / failover_policy` | `configured`，展示脱敏摘要和主备优先级 | 代理节点可用 |
| 解析 | 已拉取单条订阅并识别 Base64 URI 列表 / Clash YAML / JSON | 展示解析节点数和过滤伪节点数 | 节点健康或出口可用 |
| 同步 | 该订阅真实节点已标准化写入 `proxy_airport_nodes` | 展示节点总数、最近同步时间、失败原因 | 授权槽位已经绑定代理 |
| 健康探测 | 至少一个节点完成出口观测并通过健康阈值 | 展示健康节点数、observed exit IP、国家 / ASN / ISP | 所有节点可用 |
| 槽位绑定 | 账号授权槽位已固定到健康节点 | 只在“账号面具 > 账号代理”展示绑定和审计；系统设置最多显示是否存在未绑定槽位的只读提示 | 由系统设置承担账号代理分配 |

健康节点数为 0、订阅解析失败、同步失败或出口观测失败时，该订阅不可作为候选来源。`search_click` Task 结构合法即先创建并成功启动；Planner/Dispatcher 首轮再按主备优先级寻找任一启用且有健康节点的订阅。全部启用订阅都不可用时，该 scope 进入 `runtime_state=waiting` 并写 `airport_all_subscriptions_unavailable`，其他合法 scope 继续；不能用“订阅已保存”或“节点已解析”绕过代理可用性校验，也不能回滚 Task。

主备容灾规则：

- 订阅源字段至少包含 `name`、脱敏摘要、`priority`、`enabled`、`subscription_format`、`status`、`node_count`、`healthy_node_count`、`last_sync_at`、`last_error`、`failover_policy`、`auto_failback_enabled`、`failback_cooldown_minutes`。
- 节点分配优先选择优先级最高且健康节点可用的订阅；同优先级订阅不得同时作为主源，保存时必须拒绝冲突。
- 当前授权槽位绑定节点不通时，先在同订阅内按容量选择下一个健康节点；同订阅无健康节点时，才按优先级切到备用订阅健康节点。
- 切到备用订阅必须写 `proxy_node_failover_events`，记录 `from_subscription_id`、`to_subscription_id`、`from_node_id`、`to_node_id`、原因、操作者或系统触发来源、observed error 和 warmup 重置事实。
- 全部启用订阅都不可用时，不创建新的 Gateway Action 或终态 skip；Task 保持 `running`、scope 写 `runtime_state=waiting + airport_all_subscriptions_unavailable`、click 欠额不减少，并通过租户 Bot 管理员通知链路发送脱敏告警。订阅恢复产生新的 egress/binding fact version，立即唤醒独立 search solver重新选择路径并直接执行；不重建中央分片权重、Window 或 Reservation。
- 主订阅恢复后默认不自动切回；开启自动切回时必须满足健康连续窗口、冷却时间、每账号 / 每小时切回上限和影响预览，并写审计。

#### 2.8.3 搜索目标群点击任务系统托管策略

“搜索点击”是本轮唯一当前模式，业务身份固定为 `task_type=search_click + search_execution_mode=click_only`，正式接口为 `/api/tasks/search-click`，专项权限为 `tasks.create.search_click`。它只完成搜索、翻页、目标匹配和目标点击，不加入目标群，不创建成员关系或群管准入义务。旧 `search_join_group` 任务类型、路由、权限及内部 Action 名只作存量兼容，不能出现在新前端、公开 API 或业务日志。“搜索点击加入”是后续独立模式，当前 `design_status=not_started`。

下列字段只属于旧 `search_join_group` 历史任务事实，不是新建 `search_click` 表单字段，也不参与当前 Planner、solver、claim 或 Gateway。新建页不得展示、编辑或提交；旧 Task物理删除前仅可按权限只读：

| 字段 | 历史语义 | 当前生效位置 |
| --- | --- | --- |
| `per_account_total_action_limit` | 旧任务单账号累计上限 | `historical_read_only` |
| `per_account_daily_action_limit` | 旧任务单账号自然日上限 | `historical_read_only` |
| `per_account_cooldown_days` | 旧任务账号冷却 | `historical_read_only` |
| `per_keyword_account_daily_limit` | 旧任务账号关键词日上限 | `historical_read_only` |
| `max_actions_per_day` | 旧任务软分散提示 | `historical_read_only` |
| `hourly_skip_probability` / `daily_skip_probability` | 旧任务行为采样 | `historical_read_only` |
| `skip_probability_per_action` | 旧任务单动作采样 | `historical_read_only` |
| `hourly_jitter_percent` / `daily_jitter_percent` | 旧任务抖动范围 | `historical_read_only` |

当前 search solver 顺序固定为：开放义务 UUID -> 账号/授权槽位 -> 协议样本 -> 真实代理出口与 active binding -> 客户端元数据 -> 当前安全事实 -> 持久随机候选顺序 -> 按真实空闲 search slot 原子创建 assignment/Action并立即执行。它不计算 planning deficit、catch-up、硬安全容量总和、软 skip、抖动或未来可确认量。前置事实暂不可用时写明确 runtime blocker；失败不减少 click 目标，也不能创建假成功 Action。

新建操作固定为“任务类型 → 目标群 → 关键词与目标次数 → 执行账号组 → 确认”。纯搜索点击的业务数量只开放每日 click 目标，不显示入群开关、admission-ready 目标、速率、日/小时抖动、静默、账号容量或账号优先级。纯搜索点击使用“创建并启动”，`search_rank_deboost` 使用“创建草稿”；二者都不得要求运营补填代理、机器人、单账号策略或其他系统配置。创建接口只检查公开 username、账号组引用和字段结构；候选账号、代理/授权绑定、协议样本和安全额度由启动后的系统运行评估返回可读 blocker，并在任务详情持续更新。

找不到目标群时，`@jisou` Executor 必须先以当前已审批、版本化的 `BotProtocolSample.page_fingerprints` 分类关键词响应；若为普通热搜页，点击同版本协议样本中批准的“群聊 / 群组” selector 进入群分类页，再持续翻页，固定页数不构成停止条件。分类优先级固定为验证页、热搜页、搜索分类页、群聊结果页、未知页。旧版只有 `buttons` 摘要的样本或 Action 缺冻结 profile 时，Planner / Dispatcher 必须写 `protocol_sample_invalid` 并在 Gateway 前停手；不得用硬编码文案猜测。只有热搜页缺少已审批“群聊 / 群组” selector 时才写 `jisou_group_selector_missing` 并终结本 assignment；禁止 `/cancel`、`/start`、重发关键词或点击 telegram_url 外链。图片验证码页写 `jisou_image_verification_required`，固定按 RapidOCR → ddddOCR 顺序识别：A 无效时运行同 fingerprint 的 B；A 被权威拒绝后仍是同 fingerprint 才允许 B，已换 fingerprint 则新 challenge 从 A 开始；B 无安全答案或被拒绝后只接受远端自动换题或协议样本审批的 refresh callback，无动作写 `refresh_not_supported`；搜索链不得加载、调用或等待 AI/VLM。callback unknown 只复探不重点击，旧 fingerprint 不再使用；只有明确远端通过才写 `jisou_image_verification_solved`。Telegram client 建连或只读授权检查失败发生在任何机器人可见写操作之前，必须明确写 `search_transport_unavailable`、阶段和异常类名；原义务保持 open，binding/egress 事实变化后立即唤醒 solver 创建新 assignment，不等待 Window 或重新分片。机器人可见写操作开始后的不可判定结果进入 unknown 防重。OCR/refresh 不占 click 目标。结果 trace 只能保留 hash、长度、按钮类型/effect、审批匹配标记和版本，禁止持久化机器人正文、按钮原文、目标群名或目标行。精确公开 username 命中并产生真实 `target_click_observed` 即完成义务 UUID；确认后禁止另建 membership/admission/can-send child。机器人真实没有“下一页”仍未命中时，Action 写 `target_not_in_results`、`search_end_reason=no_next_page` 和实际 `searched_pages/last_result_page`，义务保持 open 供后续真实路径执行。非目标安全浏览只允许当前样本批准的 `navigate_only`，不加入、不关注、不外跳。

2026-07-28 起，上段的 source 成功事实固定为精确目标命中并取得 `target_click_observed`；纯搜索点击不存在成员关系或 admission 后续合同。

`search_click` 的实时 pacing、random decision、账号是否执行、目标是否点击、是否跳过、是否重排和图片验证码识别都不得调用 LLM/AI/VLM。其他 LLM 只允许用于离线配置建议、关键词生成、目标相关性解释和复盘分析；这些输出必须作为建议或解释进入人工可见页面，不能直接替代 Planner / Executor 决策。

#### 2.8.4 完整性复核后的缺口清单

本轮复核确认：专项 PRD 已覆盖多数设计，但主 PRD 需要补齐以下易误解点，后续研发和 QA 以本表作为验收基准。

| 缺口 / 风险 | 修正口径 |
| --- | --- |
| 系统设置和账号面具边界容易混淆 | 系统设置维护租户级多 Clash 订阅源、主备优先级和同步健康；账号代理和授权指纹都在“账号面具”一级菜单中按授权槽位配置 |
| Clash 保存成功容易被误报为代理可用 | 完整代理链路按保存、解析、同步、健康探测、槽位绑定五阶段验收；前四阶段在系统设置按订阅源展示，槽位绑定只在“账号面具 > 账号代理”展示；单条订阅健康节点数为 0 不可作为候选代理池 |
| Clash 主订阅故障会造成单点风险 | 系统设置支持多个 Clash 订阅地址，按 `priority` 主备容灾；主订阅不可用时使用备用订阅健康节点，全部启用订阅不可用时才停手并通知管理员 |
| “修改指纹”容易被误报为远端设备立即改变 | 保存配置只影响下一次连接 / 重登 / 新 session 初始化；远端事实只能来自授权设备快照 |
| legacy `membership_observed` 容易被误解为当前目标完成 | 它只属于 `legacy_mixed_search_join` 历史事实，不能完成纯搜索点击；当前 `search_click` 不创建 admission child |
| 群聊筛选和未命中终止口径 | `@jisou` 必须先选群聊；固定页数不终止。真实末页未命中写实际页码 / `no_next_page`，action 失败但任务继续运行 |
| decoy 行为容易做成真实加入 | decoy 和非目标安全浏览只允许 `navigate_only`，不得加入、关注或打开外部 HTTP |
| legacy search_join 节奏可能污染当前任务 | 旧每账号上限、任务天/小时上限、skip、jitter、quiet-hours 仅作 `legacy_mixed_search_join` 只读事实；当前 `search_click` 只保留真实 Telegram 安全事实，资源空闲即执行 |
| 实时随机决策如果调用 LLM 会不可复现 | 在线路径禁止调用 AI Gateway / AI Provider / `ai_generator`；只用规则、配置、seeded random 和持久化 pacing decision |
| 线上完成容易被 CI 通过替代 | 发布健康、Clash 同步、出口观测、授权观测刷新和 3 账号 Zhengzhou 真实搜索测试必须分别取证，未取证只能写 `unproven` |
| “所有账号指纹更新绑定”范围容易被误解 | 指所有 active 运营账号中具备可执行授权槽位的账号；接码专用账号、无 session、无 TG 开发者应用、授权不可解密或缺代理的槽位不得硬塞配置，必须逐槽位返回 `configured / pending_effect / failed` 和失败原因 |
| 线上业务测试容易被 Actions smoke 替代 | GitHub Actions 只作为发布和 CI 证据；Clash 应用、全账号环境绑定刷新和 Zhengzhou 3 账号真实纯搜索点击验收必须通过生产服务器 SSH、生产 API 或已登录浏览器直接触发并取证 |
| search_join 旧小时字段阻断日目标 | `type_config.max_actions_per_hour` 与 `max_actions_per_day` 只作旧 Task 历史读取，不进入当前候选、claim、Gateway 或完成判断；新建/编辑不再暴露这两个字段 |

#### 2.8.5 全站前端异步与错误展示契约

本节是全站页面可靠性补充契约，不只属于账号面具或 search_join。任何页面涉及二段刷新、弹窗会话、写动作后的列表 / 详情刷新、定时轮询或深链打开时，都必须遵守以下异步响应隔离和可见错误要求。

- 页面数据加载失败必须显式暴露错误信息，不能在前端把 API 失败静默转换为空数组、空对象、旧汇总或假成功状态。允许保留上一次可用数据时，页面必须同时展示刷新失败错误。
- 页面关键操作失败也必须展示后端返回的 `detail` 或可读错误正文。运营异常处理、任务状态变更、目标同步、系统配置保存等按钮不得只提示“失败”或“操作失败”。
- 运营中心的运营方案创建、预览、生成任务、暂停 / 恢复 / 复制 / 归档、保存、关联任务影响预览、应用关联任务和异常处理失败时，必须展示后端 `detail` 或可读响应正文；按钮回调不能形成不可见 Promise rejection。
- 运营中心的运营方案创建、预览、生成任务、暂停 / 恢复 / 复制 / 归档、保存、关联任务影响预览、应用关联任务和异常处理成功后，如果后续运营中心数据刷新失败，必须提示“运营中心数据刷新失败”并说明原操作已完成；不能误报为方案创建、预览、生成、保存、状态流转、影响预览、应用关联任务或异常处理失败。
- 运营中心首屏加载、手动刷新和方案 / 异常动作后的运营中心数据刷新必须绑定运营中心数据请求序号；连续刷新或多个方案 / 异常动作交错完成时，旧刷新响应不得覆盖最新方案列表、目标列表、运营中心摘要、目标运行汇总、异常列表、loading 或错误提示。
- 运营中心方案创建、预览、生成草稿、生成并启动、暂停 / 恢复 / 复制 / 归档、保存、关联任务影响预览和确认应用必须绑定当前方案动作 key；连续触发不同方案动作或切换影响预览抽屉时，旧动作响应不得清空当前按钮 loading、覆盖当前预览 / 影响结果或展示旧动作错误。
- 运营中心方案保存必须绑定当前 `plan_id + payload` 签名、保存请求序号和方案编辑抽屉会话；保存返回前切换方案、关闭 / 重开编辑抽屉或修改方案名称、描述、目标类型、状态、绑定目标或任务模板时，旧保存响应不得关闭当前抽屉、覆盖提示或触发旧 payload 的成功刷新。
- 运营中心应用关联任务必须绑定当前 `plan_id + reason + confirm_apply` 签名、应用请求序号和影响预览抽屉会话；应用返回前切换影响预览方案、关闭 / 重开抽屉或修改确认原因时，旧应用响应不得覆盖当前影响结果、提示或触发旧原因的成功刷新。
- 消息发送列表中的发送任务创建 / 批量创建、立即执行、重试和取消失败时，必须通过全局操作失败出口展示后端 `detail` 或可读响应正文；不能只依赖页面局部提示、未捕获 Promise 或按钮 loading 状态变化。
- 消息发送页按账号读取“联系人”和“运营目标”时必须分别承载接口结果；任一接口失败只能清空对应数据并展示具体错误，不能把另一个已成功返回的数据一并丢弃，避免联系人同步异常阻断已授权运营目标发送。
- 消息发送页按账号读取联系人和运营目标、以及定时刷新运营目标时，必须绑定发起时的 `account_id + 请求序号`；用户快速切换发送账号或定时刷新与账号切换交错完成时，旧账号响应不得覆盖当前联系人、运营目标、loading 或错误提示。
- 消息发送页发送前风控预检必须绑定发起时的请求序号和发送 payload 签名；用户在预检返回前修改发送账号、目标、内容、素材或定时时间时，旧预检结果不得打开确认弹窗、覆盖当前预检结果或把旧预检错误展示到当前表单。
- 消息发送页确认提交必须绑定已通过风控预检的发送 payload 签名；确认弹窗打开后如果发送账号、目标、内容、素材、发送方式或定时时间发生变化，前端必须阻止提交并提示重新预检，不得用旧预检结果提交新 payload，也不得让确认弹窗展示内容与实际提交 payload 不一致。
- 消息发送页定时刷新发送记录和基础快照时，必须捕获刷新失败并在页面内展示可读错误；不能在 `setInterval` 中用未处理的 `void onRefresh()` 形成不可见 Promise rejection。
- 消息发送的私发任务创建、发送任务创建 / 批量创建、取消、派发、到期队列处理和重试成功后，如果后续消息发送数据刷新或账号 / 账号池详情刷新失败，必须提示“消息发送数据刷新失败”并说明原操作已完成；不能误报为原写动作失败。
- 监听转发任务需要 AI 润色源群消息时，AI 未启用、没有健康供应商、返回空内容或调用失败都必须让本轮监听转发失败并写入可见错误；不能用代码轻改写、模板文案、mock 或本地规则生成 `APPROVED` draft / message task 抵扣成功。
- 监听转发任务的目标群没有可用发送账号时，必须让本轮监听转发失败并写入可见错误，等待账号恢复后重试；不能把 `queued=0` 标记为成功，也不能把源消息静默吞掉。
- 账号中心的健康检查、账号同步和账号 / 账号池私发任务创建失败时，必须通过全局操作失败出口展示后端 `detail` 或可读响应正文；不能只让外层 loading 结束而没有用户可见失败原因。
- 账号中心的账号删除、账号分组创建 / 移动、克隆计划创建 / 执行 / 重试、验证辅助处理、联系人同步、账号全量同步、账号资料保存 / 重试、健康检查和账号同步成功后，如果后续账号列表、账号详情、账号池详情或群详情刷新失败，必须提示“账号中心数据刷新失败”并说明原操作已完成；不能误报为原写动作失败。
- 账号新增成功、验证码登录 / 扫码登录启动成功，以及验证码 / 2FA / 扫码检查推进登录状态成功后，如果后续账号列表或账号详情刷新失败，必须提示“账号中心数据刷新失败”并说明登录或新增动作已完成；不能把刷新失败写进登录表单错误或误报为账号新增、验证码校验、2FA 校验、扫码检查失败。
- 账号中心列表页的可用性汇总读取、可用性重算和批量同步安全状态失败时，必须在账号页内展示后端 `detail` 或可读响应正文；不能只保留“等待汇总”、旧健康分或结束按钮 loading。
- 账号中心列表页的可用性重算和批量同步安全状态成功后，如果后续账号可用性汇总刷新失败，必须提示“账号中心数据刷新失败”并说明原操作已完成；不能误报为可用性重算或安全状态刷新失败，也不能静默保留旧汇总。
- 账号中心列表页的可用性汇总读取、账号列表变化触发的刷新，以及可用性重算 / 批量同步安全状态后的汇总刷新必须绑定账号可用性请求序号；连续刷新或账号列表变化与写动作交错完成时，旧汇总响应不得覆盖最新可用性 Map、loading 或错误提示。
- 账号中心的账号详情读取、验证码入口、账号池详情、账号分组创建 / 移动、克隆计划创建 / 执行 / 重试、联系人同步、账号全量同步、群详情读取、验证码同步、资料保存和资料同步重试失败时，必须通过全局操作失败出口展示后端 `detail` 或可读响应正文；不能只依赖外层 `runWithLoading` 或未捕获 Promise。
- 账号中心资料保存必须绑定发起时的 `account_id + profile payload + avatar file` 签名和保存请求序号；头像上传或资料保存返回前切换账号详情、关闭 / 重开资料弹窗、修改昵称、TG 姓名、简介、头像对象或头像文件时，旧保存响应不得把旧表单提交到新账号、关闭当前弹窗、覆盖提示、触发旧 payload 的成功刷新或清空新保存 busy。
- 账号详情内联系人同步、账号全量同步和资料同步重试必须绑定发起时的 `account_id + action` 和请求序号；同步返回前切换账号详情或触发另一账号同步时，旧同步响应不得刷新新账号详情、覆盖当前提示、触发旧账号成功刷新或清空新同步 busy。
- 账号详情内移动分组、克隆计划创建、克隆计划执行和克隆项重试必须绑定发起时的 `account_id + action` 和请求序号；动作返回前切换账号详情或重复触发同类动作时，旧响应不得刷新新账号详情、切换新账号详情 Tab、重开当前弹窗、覆盖当前提示或清空新动作 busy。
- 账号登录弹窗中的验证码登录启动 / 重发、扫码登录启动 / 检查、验证码提交和二步密码提交必须绑定发起时的 `account_id + action + 请求序号`；请求返回前切换登录账号、重新选择登录方式或再次提交时，旧响应不得覆盖当前登录表单、错误提示、notice、弹窗状态、账号详情刷新或全局 busy。验证码输入框、2FA 输入框和账号新增 / 登录确认表单必须支持 Enter 回车提交，且回车触发的动作与点击主按钮共用同一校验、loading、幂等和错误展示逻辑。
- 账号详情打开、验证码入口、移动分组入口和刷新详情必须绑定当前 `account_id` 与请求序号；账号池详情打开 / 刷新必须绑定当前 `pool_id` 与请求序号；群详情打开 / 刷新必须绑定当前 `group_id` 与请求序号。旧异步响应不得覆盖当前详情、默认 Tab、弹窗类型或全局 busy 状态。
- 账号详情弹窗内的可用性读取 / 重算、账号安全读取 / 刷新 / 批次创建、同步目标和手动发送失败时，必须在弹窗内展示后端 `detail` 或可读响应正文；不能只清除局部 loading 或关闭输入状态。
- 账号详情弹窗内验证聊天读取、重新读取和验证回复提交必须绑定当前 `account_id + verification_task_id + 弹窗会话`；读取失败必须在账号详情弹窗内展示后端 `detail` 或可读响应正文，不能形成未捕获 Promise；关闭或切换验证任务后，旧响应不得写入当前验证聊天、清空当前回复或关闭当前弹窗。
- 账号详情弹窗内同步目标和手动发送完成后，如果后续账号详情刷新失败，必须以“刷新账号详情失败”独立提示；不能把刷新失败误报成同步目标失败或手动发送失败。
- 账号详情弹窗内的可用性读取 / 重算、账号安全读取 / 刷新和账号安全批次创建必须绑定当前 `account_id`；旧账号异步响应不得覆盖当前账号详情 Tab 的数据、loading 或错误提示。
- 账号安全批次抽屉的预检 / AI 资料预览、重抽全部和批次创建必须绑定发起时的 payload 签名与请求序号；用户在预检或创建过程中调整账号、动作、资料策略、备用 session 策略或原因时，旧预检 / 创建响应不得覆盖当前预检、批次结果、loading、确认弹窗或步骤状态。
- 账号详情的授权资产面板和托管 2FA 面板中，授权资产读取、备用授权登录准备、备用登录启动、验证码 / 2FA 校验、QR 登录检查、切换主授权、托管 2FA 保存和轮换失败时，必须在面板内展示后端 `detail` 或可读响应正文；不能只停留在 loading 结束或 AntD 弹窗未处理 rejection。
- 账号详情的授权资产面板和托管 2FA 面板必须绑定当前 `account_id`；用户快速切换账号或关闭详情后，旧账号异步响应不得覆盖当前账号的授权资产、备用登录表单、托管 2FA 输入、loading、错误提示或成功提示。
- 账号授权资产读取、手动刷新、备用登录完成后的授权资产刷新和切换主授权后的授权资产刷新还必须绑定当前 `account_id + 请求序号`；同一账号连续刷新、备用登录完成刷新或切换主授权交错完成时，旧授权资产响应不得覆盖当前授权资产、loading 或错误提示。
- 托管 2FA 保存和轮换还必须绑定当前 `account_id + action + 请求序号 + payload 签名`；同一账号连续保存 / 轮换、保存与轮换交错完成，或请求返回前修改密码 / 原因时，旧响应不得清空当前密码、原因或覆盖当前错误提示；只有当前最新请求可清理 loading。
- 账号授权资产的备用登录弹窗必须绑定当前弹窗会话序号；同一账号内关闭弹窗、重新打开弹窗、重新发起备用登录、提交验证码 / 2FA 或检查 QR 登录后，旧弹窗会话的异步响应不得覆盖当前备用登录资源、`login_flow`、验证码输入、loading、错误提示或成功提示。
- 账号授权资产备用登录的启动请求还必须绑定发起时的登录 payload 签名；运营人员在启动返回前切换备用槽位、登录方式、开发者应用或代理时，旧启动响应不得写入当前 `login_flow`、验证码预填或错误提示；loading 清理必须绑定当前登录会话，避免 payload 变化后旧启动请求无法结束按钮 loading。
- 账号授权资产备用登录完成和主授权切换完成后，如果后续账号详情刷新失败，必须以“刷新账号授权资产失败”独立提示；不能把刷新失败误报成验证码 / QR 检查失败或切换主授权失败。
- 群管理的授权更新、群策略保存、归档创建，以及归档中心的新建归档、详情读取、导出和重跑失败时，必须通过全局操作失败出口或归档页内错误提示展示后端 `detail` 或可读响应正文；不能只让外层 loading 结束或形成不可见 Promise rejection。
- 群管理账号覆盖和监听上下文入口打开群详情失败时，必须展示后端 `detail` 或可读响应正文，并关闭本地详情弹窗；不能让弹窗停留在“正在读取账号覆盖详情”或“正在读取监听上下文”的空态。
- 归档中心和群管理内的归档详情入口打开详情失败时，必须展示后端 `detail` 或可读响应正文，并关闭本地归档详情弹窗；不能让弹窗停留在“正在读取归档详情”的空态或旧详情态。
- 归档详情读取必须绑定当前 `archive_id` 与请求序号；用户快速切换归档或关闭详情后，旧异步响应不得覆盖当前归档详情、清空当前 loading 或关闭当前归档详情弹窗。
- 群授权更新、群策略保存、群入口归档创建、归档导出和归档重跑成功后，如果后续全局数据刷新失败，必须提示“页面数据刷新失败”并说明原操作已完成；不能误报为授权更新、群策略保存、归档创建、归档导出或归档重跑失败。
- 素材中心的素材上传、批量上传、ZIP 导入、保存、禁用、恢复，以及关键词规则新增和保存失败时，必须通过全局操作失败出口展示后端 `detail` 或可读响应正文；前端本地校验失败也必须给出明确可见原因。
- 素材中心的素材上传、批量上传、ZIP 导入、保存、禁用、恢复、素材组保存 / 启停，以及关键词规则新增和保存成功后，如果后续素材中心数据刷新失败，必须提示“素材中心数据刷新失败”并说明原操作已完成；不能把二段刷新失败误报为上传、保存、禁用、恢复、素材组保存 / 启停或关键词保存失败。
- 素材中心列表里的“刷新缓存”只刷新当前素材缓存和素材列表，不得隐式打开素材详情抽屉；只有用户已打开同一素材详情时，刷新缓存成功后才同步刷新该详情抽屉。
- 素材中心素材新增 / 上传 / 批量上传 / ZIP 导入 / 保存必须绑定当前 `material_id + tenant_id + payload + files` 签名和保存请求序号；保存返回前切换素材、关闭 / 重开编辑弹窗、修改标题、类型、标签、内容、来源或文件列表时，旧保存响应不得关闭当前弹窗、清空当前文件、覆盖素材列表、覆盖提示或触发旧 payload 的成功刷新，旧保存也不得清空新保存 busy。
- 素材中心素材禁用 / 恢复必须绑定当前 `material_id + action` 和请求序号；连续禁用、恢复不同素材或禁用与恢复交错完成时，旧操作响应不得覆盖素材列表、覆盖提示、触发旧操作成功刷新或清空新操作 busy。
- 素材中心关键词规则新增 / 保存必须绑定当前 `rule_id + tenant_id + payload` 签名和保存请求序号；保存返回前切换关键词规则、关闭 / 重开编辑弹窗或修改关键词、匹配方式、启用状态、备注时，旧保存响应不得关闭当前弹窗、重置当前表单、覆盖提示、触发旧 payload 的成功刷新或清空新保存 busy。
- 素材中心的素材组弹窗打开、素材组保存 / 启停后的素材组列表刷新必须绑定素材组请求序号；连续打开弹窗或多个素材组写动作交错完成时，旧素材组响应不得覆盖最新素材组列表、loading 或错误提示。
- 素材中心的素材组保存必须绑定当前素材组动作 key、保存请求序号和表单 payload 签名，素材组启停必须绑定当前素材组动作 key；连续保存不同素材组、保存返回前修改素材组表单、保存与启停交错或连续启停不同素材组时，旧动作响应不得清空当前按钮 loading、覆盖当前错误提示、重置当前表单或触发旧动作成功刷新。

- 风控中心的代理检查、代理新增 / 编辑、代理禁用、代理告警处理和忽略失败时，必须展示后端 `detail` 或可读响应正文；确认弹窗中的代理禁用不能形成不可见 Promise rejection。
- 风控中心的代理检查、全局策略保存、代理新增 / 编辑 / 禁用、代理告警处理和忽略成功后，如果后续风控摘要刷新失败，必须提示“风控中心数据刷新失败”并说明原操作已完成；不能误报为代理检查、策略保存、代理保存、代理禁用、告警处理或告警忽略失败。
- 风控中心首屏加载、手动刷新和写动作后的风控摘要刷新必须绑定风控数据请求序号；连续刷新或多个代理 / 策略操作交错完成时，旧刷新响应不得覆盖最新 summary、proxy 列表、loading 或错误提示。
- 风控中心代理检查、代理告警处理和告警忽略的按钮 loading、错误提示和成功后的刷新触发必须绑定当前动作 key；连续处理不同代理或不同告警时，旧动作响应不得清空当前按钮 loading、覆盖当前错误提示或触发旧动作的成功刷新。
- 风控中心全局策略保存和代理新增 / 编辑必须绑定发起时的表单 payload 签名与保存请求序号；保存返回前修改策略或代理表单时，旧保存响应不得关闭当前弹窗、覆盖当前错误提示或触发旧 payload 的成功刷新；loading 清理必须绑定当前保存请求序号，避免旧保存清掉新保存状态。
- 系统设置的后台账号 Token 流水读取、提示词模板新增和保存失败时，必须通过全局操作失败出口展示后端 `detail` 或可读响应正文；不能在编辑弹窗打开或表单提交链路中形成不可见 Promise rejection。
- 后台账号编辑弹窗读取 Token 流水必须绑定当前用户；切换用户时先清空旧流水，读取失败时不得保留上一个后台用户的流水记录。
- 后台账号编辑弹窗读取 Token 流水还必须绑定 `user_id + 请求序号`；用户快速切换后台账号、打开创建用户弹窗或 Token 调整后刷新流水时，旧用户流水响应不得覆盖当前用户流水、错误提示或 busy 状态。
- 系统设置的开发者应用、运营空间配置、群聊救援配置、后台账号、Token 调整、AI 供应商、AI 配置和提示词写动作成功后，如果后续系统设置数据刷新失败，必须提示“系统设置数据刷新失败”并说明原操作已完成；不能把二段刷新失败误报为原写动作失败。
- 系统设置开发者应用新增 / 保存必须绑定当前 `app_id + payload` 签名和写请求序号；保存返回前切换开发者应用、关闭 / 重开编辑弹窗或修改应用名、api_id、api_hash、账号上限、备注、启用状态时，旧保存响应不得关闭当前弹窗、重置当前表单、覆盖当前错误 / 成功提示或触发旧 payload 的成功刷新，旧保存也不得清空新保存 busy。
- 系统设置开发者应用启停 / 检查必须绑定当前 `app_id + action` 和请求序号；连续启停、检查不同应用或启停与检查交错完成时，旧操作响应不得覆盖当前成功 / 错误提示、触发旧操作成功刷新或清空新操作 busy。
- 系统设置运营空间配置保存必须绑定当前 `tenant_id + payload` 签名和写请求序号；保存返回前切换运营空间、关闭 / 重开编辑弹窗或修改名称、套餐、账号配额、任务配额时，旧保存响应不得关闭当前弹窗、覆盖当前错误 / 成功提示或触发旧 payload 的成功刷新，旧保存也不得清空新保存 busy。
- 系统设置群聊救援配置保存必须绑定当前 `tenant_id + payload` 签名和写请求序号；保存返回前切换运营空间、修改启用状态或替换救援管理员账号时，旧保存响应不得覆盖当前提示、触发旧 payload 的成功刷新或清空新保存 busy。
- 系统设置后台账号新增 / 保存必须绑定当前 `user_id + payload` 签名和写请求序号；保存返回前切换后台账号、打开新建账号弹窗或修改姓名、密码、角色、模板、订阅状态、权限、启用状态时，旧保存响应不得关闭当前弹窗、覆盖当前提示或触发旧 payload 的成功刷新，旧保存也不得清空新保存 busy。
- 系统设置后台账号重置密码必须绑定当前 `user_id + new_password` 签名和写请求序号；重置返回前切换后台账号、打开新建账号弹窗或触发另一用户重置时，旧重置响应不得覆盖当前提示或清空新重置 busy。
- 系统设置后台账号 Token 调整必须绑定当前 `user_id + payload` 签名和写请求序号；调整返回前切换后台账号、打开新建账号弹窗、修改调整 Token 数量或原因、触发另一用户 Token 调整时，旧调整响应不得覆盖当前提示、切回旧用户 Token 流水、触发旧 payload 的成功刷新或清空新调整 busy。
- 系统设置 AI 供应商新增 / 保存必须绑定当前 `provider_id + payload` 签名和写请求序号；保存返回前切换 AI 供应商、关闭 / 重开编辑弹窗或修改供应商名称、base_url、模型、API Key、请求头、备注、启用状态时，旧保存响应不得关闭当前弹窗、重置当前表单、覆盖当前错误 / 成功提示或触发旧 payload 的成功刷新，旧保存也不得清空新保存 busy。
- 系统设置 AI 供应商启停 / 检查必须绑定当前 `provider_id + action` 和请求序号；连续启停、检查不同供应商或启停与检查交错完成时，旧操作响应不得覆盖当前成功 / 错误提示、触发旧操作成功刷新或清空新操作 busy。
- 系统设置 AI 配置保存必须绑定当前 `default_provider_id + ai_enabled + fallback_to_mock + temperature + max_tokens` payload 签名和写请求序号；保存返回前切换默认供应商或修改启用状态、回退策略、温度、Token 上限时，旧保存响应不得关闭当前弹窗、覆盖当前提示或触发旧 payload 的成功刷新，旧保存也不得清空新保存 busy。
- 系统设置 AI 配置的最大 Token 上限必须按当前默认供应商模型族校验并在前端同步展示：MiniMax 供应商上限为 250000，其他文本供应商上限为 100000；超过对应上限时后端必须返回可见 400 错误，不得静默截断或保存假成功。
- 全局 `runWithLoading` 必须绑定最近一次全局 busy 请求序号；多个动作并发或同一个 action key 连续触发时，旧动作结束不得清空后发动作的 busy 状态，pending action key 清理不得误删其他仍在进行的同名动作。
- 系统设置提示词模板新增 / 保存必须绑定当前 `template_id + tenant_id + payload` 签名和写请求序号；保存返回前切换提示词模板、关闭 / 重开编辑弹窗或修改名称、类型、内容、启用状态时，旧保存响应不得关闭当前弹窗、重置当前表单、更新模板列表、覆盖当前错误 / 成功提示或触发旧 payload 的成功刷新，旧保存也不得清空新保存 busy。
- 顶部“刷新当前数据”在系统设置页必须同时处理当前 Tab 的二段懒加载失败；开发者应用、后台账号、AI 供应商、提示词和素材运行配置读取失败时必须展示后端 `detail` 或可读响应正文，不能只刷新全局 snapshot 后形成不可见 Promise rejection。非系统设置页的全局刷新失败必须提示“刷新当前数据失败”，不能误报为“系统设置数据读取异常”。
- 系统设置当前 Tab 的二段懒加载和顶部刷新后的 Tab 刷新必须绑定 `tab + 请求序号`；用户快速切换 Tab、离开系统设置或顶部刷新与 Tab 懒加载交错完成时，旧 Tab 响应不得覆盖当前 Tab 数据、loading 或错误提示。
- 全局刷新、顶部“刷新当前数据”、路由切换和写动作后的 snapshot 刷新必须绑定全局刷新请求序号；连续刷新、路由切换或写动作后二段刷新交错完成时，旧 snapshot 不得覆盖当前页面状态、busy 或错误提示。
- 监听中心重置水位弹窗必须绑定当前弹窗会话；用户提交重置后关闭或切换到另一个监听对象时，旧重置响应不得关闭当前弹窗、覆盖当前错误提示或清理错误明细。
- 运营目标列表首屏加载、手动刷新、自动轮询和写动作后的目标列表刷新必须绑定列表请求序号；连续刷新、轮询与写动作交错完成时，旧列表响应不得覆盖最新目标列表、loading 或错误提示。
- 运营目标全量同步必须绑定独立的同步动作请求序号；自动轮询、手动刷新或写动作后的列表刷新不得抢占全量同步的成功 / 失败提示、按钮 loading 或同步结果处理，全量同步成功后再按写动作刷新契约刷新运营目标列表。
- 运营目标新增 / 保存必须绑定发起时的 `target_id + payload` 签名与保存请求序号；保存返回前切换编辑目标、打开新建弹窗或修改目标类型、peer、标题、username、人数、可发送状态、授权状态时，旧保存响应不得关闭当前弹窗、重置当前表单、覆盖当前错误提示或触发旧 payload 的成功刷新；loading 清理必须绑定当前保存请求序号。
- 运营目标详情读取失败后不得继续执行只依赖详情上下文的后续动作；例如打开详情失败时不能继续自动同步目标消息，避免把读取失败和同步副作用串在一起。
- 运营目标详情读取和成功动作后的详情刷新必须绑定当前 `target_id` 与请求序号；用户连续打开同一目标、深链重复聚焦或触发二段刷新时，旧详情响应不得覆盖当前目标详情、loading 或错误提示。
- 运营目标详情内会直接回写详情或刷新详情的自动同步、评论同步、账号策略保存和准入重试响应还必须绑定详情写回请求序号；同一目标内连续触发多个写动作时，旧写动作响应不得覆盖最新详情、清空最新 loading 或覆盖最新错误提示。
- 运营目标新增 / 保存、详情自动同步、评论同步、账号策略保存、准入重试和归档创建成功后，如果后续运营目标列表或目标详情刷新失败，必须提示“运营目标数据刷新失败”并说明原操作已完成；不能误报为目标保存、同步、评论同步、账号策略保存、准入重试或归档创建失败。
- 任务中心列表刷新和外部深链聚焦任务详情失败时，必须使用同一错误解析逻辑展示后端 `detail`、响应正文或 `trace_id`；不能在 `void load()`、轮询或深链消费里形成不可见 Promise rejection，也不能只显示固定“读取失败”。
- 任务中心列表首屏加载、任务类型切换、自动轮询和写动作后的任务列表刷新必须绑定列表请求序号；连续刷新、任务类型切换、轮询和写动作交错完成时，旧任务列表响应不得覆盖最新任务列表、调度配置、loading 或错误提示。
- 任务中心的启动 / 暂停 / 恢复 / 停止 / 重试 / 重置、准入处理、准入失败导出和删除任务按钮状态、错误提示和成功后的刷新触发必须绑定当前动作 key；连续操作不同任务或不同准入项时，旧动作响应不得清空当前按钮 loading、覆盖当前错误提示或触发旧动作成功刷新。
- 任务中心可选诊断预览和编辑页 AI 数量推荐必须绑定发起时的 `task_type + payload` 签名与请求序号；用户在诊断 / 推荐返回前修改任务类型、目标、账号范围、节奏或数量字段时，旧响应不得覆盖当前诊断、推荐值、warning、错误提示或 loading。创建提交必须独立做结构校验，不得依赖或复用旧 payload 的运行诊断结果。
- 任务中心保存任务配置必须绑定发起时的 `task_id + payload` 签名、保存请求序号和当前编辑弹窗会话；用户在保存返回前切换任务详情、关闭 / 重开编辑弹窗或修改任务配置表单时，旧保存响应不得关闭当前弹窗、覆盖当前错误 / 成功提示或触发旧任务配置的成功刷新；loading 清理必须绑定当前保存请求序号，避免旧保存清掉新保存状态。
- 任务中心创建任务、保存任务配置、启动 / 暂停 / 恢复 / 停止 / 重试 / 重置、准入处理、删除任务和来源屏蔽成功后，如果后续任务列表或当前任务详情刷新失败，必须提示“任务中心数据刷新失败”并说明原操作已完成；不能误报为任务创建、配置保存、生命周期动作、准入处理、删除或来源屏蔽失败。
- 任务中心写动作成功后的当前详情刷新和准入处理返回详情必须绑定当前 `task_id`；用户在写动作执行中快速切换任务或关闭详情后，旧任务响应不得重新打开或覆盖当前详情、分页和错误提示。
- 任务中心执行尝试下钻读取失败时，必须在执行尝试弹窗内展示后端 `detail` 或可读错误正文；不能清空弹窗后把 `openActionAttempts` 重新抛给 `void` 按钮回调形成不可见 Promise rejection。
- 任务中心执行尝试下钻读取必须绑定当前 `action_id`；用户快速切换 action 或关闭执行尝试弹窗后，旧 action 的异步响应或失败不得覆盖当前尝试列表、loading 或错误提示。
- 规则中心新建规则集、保存规则配置、复制 / 发布 / 回滚规则版本成功后，如果后续规则中心数据刷新失败，必须提示“规则中心数据刷新失败”并说明原操作已完成；不能误报为规则新建、配置保存、版本复制、发布或回滚失败。
- 规则中心首屏加载、手动刷新和规则写动作后的规则中心数据刷新必须绑定规则中心数据请求序号；连续刷新或多个规则操作交错完成时，旧刷新响应不得覆盖最新规则摘要、规则集、运营目标、转发归因报表、loading 或错误提示。
- 规则中心测试器的规则测试请求必须绑定发起时的测试 payload 签名与请求序号；连续测试、切换规则版本 / 测试类型 / 媒体场景、修改样例或候选输出后，旧测试响应不得覆盖当前测试结果或错误提示；loading 清理必须绑定当前测试请求序号，避免 payload 变化后旧测试请求无法结束按钮 loading。
- 规则中心新建规则集、保存规则配置、复制 / 发布 / 回滚规则版本必须绑定当前规则动作 key、写请求序号和发起时 payload 签名；写动作返回前修改新建表单、规则配置表单、版本操作原因或触发另一规则动作时，旧响应不得关闭当前弹窗、清空当前 loading、覆盖当前错误 / 成功提示或触发旧 payload 的成功刷新。
- 目标画像的学习来源保存、来源同步 / 历史拉取、质量规则保存、样本状态调整、画像重建 / 清空、学习开关调整和版本恢复成功后，如果后续目标画像数据刷新失败，必须提示“目标画像数据刷新失败”并说明原操作已完成；不能误报为来源保存、同步、历史拉取、规则保存、样本调整、重建、清空、开关调整或版本恢复失败。
- 目标画像的首屏加载、手动刷新和写动作后的数据刷新必须绑定画像数据请求序号；连续刷新或多个画像操作交错完成时，旧刷新响应不得覆盖最新画像摘要、学习来源、候选、样本、运行记录、版本、质量规则表单或 loading / error。
- 任务中心详情弹窗的 Action、AI Cycle、频道消息组、转发批次、准入 item 和准入账号明细等分页请求必须绑定当前 `task_id`；用户快速切换任务或关闭弹窗后，旧任务的异步分页响应不得更新当前详情分页状态、total 或错误提示。
- 任务中心详情弹窗的执行计划和执行记录分页请求还必须绑定分页请求序号；同一任务内快速切换页码或刷新详情时，旧分页响应不得覆盖最新页码、rows、total 或 loading。
- 任务中心详情弹窗的 AI Cycle、频道消息组、转发批次和准入 item 子分页请求还必须绑定子分页请求序号；同一任务内快速切换页码或刷新详情时，旧子分页响应不得覆盖最新页码、详情字段、total 或 loading。
- 任务中心详情弹窗的准入账号明细分页和筛选请求还必须绑定准入账号分页请求序号；同一任务内快速切换页码、page size 或筛选条件时，旧准入账号响应不得覆盖最新页码、账号明细、total 或 loading。
- 任务中心详情主请求和写动作后的当前详情刷新还必须绑定详情请求序号；同一任务内连续打开、刷新或写动作后刷新详情时，旧详情响应不得覆盖最新详情、重新触发旧分页加载或清理最新错误提示。
- 任务中心详情主请求和外部深链聚焦任务详情失败时必须绑定当前 `task_id`；用户快速切换任务或关闭详情后，旧任务的异步失败不得覆盖当前任务详情页错误提示。
- 任务中心创建 / 编辑弹窗的账号、目标、规则集、频道消息、评论和提示词等表单支撑数据加载失败时，必须展示后端 `detail` 或可读错误正文；不能在预填、任务类型切换或弹窗打开后的懒加载链路中形成不可见 Promise rejection。
- 任务中心创建 / 编辑弹窗的表单支撑数据加载、任务类型切换和默认规则集回填必须绑定当前表单支撑数据请求序号；用户快速切换任务类型、重复打开弹窗或关闭后重开时，旧任务类型的异步响应不得清理当前 loading、覆盖当前错误提示或把旧类型默认规则集写回当前表单。
- 账号中心深链打开账号详情失败时，必须展示后端 `detail` 或可读错误正文；不能在 `/accounts?account_id=...` 自动打开详情的 Promise 链里只设置 Tab、形成不可见 rejection，或在详情未成功打开时继续覆盖详情默认 Tab。
- 后台账号只能通过系统设置的“后台账号权限”由有权限管理员创建、编辑、停用或重置密码；登录页不提供自助注册，不保留不可达的 `/auth/register` 前端调用或注册请求 schema。
- 生产环境必须在启动阶段拒绝默认 bootstrap 管理员密码 `admin123`；未显式配置 `ADMIN_BOOTSTRAP_PASSWORD` / `ADMIN_PASSWORD` 或仍使用默认值时，后端不得启动，避免默认超级管理员可登录。
- 登录页验证码加载和验证码校验失败必须展示后端返回的 `detail` 或可读响应正文；账号密码校验失败可继续使用泛化文案，避免泄露账号枚举信息。
- 公共 API 客户端的 `ApiError.message` 必须先解析 FastAPI `detail`，包括字符串、Pydantic 校验数组和对象型 `message` / `failure_detail` / `trace_id`，再回退到响应正文或 HTTP 状态；页面和全局操作失败弹窗即使直接展示 `error.message`，也不能把 `{"detail": ...}` 这类 JSON 原样作为主要错误文案。
- CSV / blob 导出等直连 `fetch` 场景必须复用统一 API 响应错误解析，失败时展示后端 `detail`、响应正文或 `trace_id`；遇到管理端认证 401 时必须触发全局登录态过期处理，不能抛原始 `Error(await response.text())` 或普通 `ApiError` 导致用户停留在业务页看到 `token expired`。审计导出 Modal 的确认动作必须捕获导出失败并展示“导出审计记录失败”，不能用 `void exportCsv(...)` 形成不可见 Promise rejection。
- 登录提交失败必须区分错误来源：账号密码 401 使用泛化文案；验证码 token 过期、验证码已使用、服务端 400/500 或网络异常必须展示可行动错误原因并刷新验证码，不能把验证码链路错误误报为账号密码错误或形成未处理 Promise。

#### 2.8.6 2026-07-04 完整梳理结论

本轮按用户最新确认对 PRD 做完整性复核，以下条款作为后续实现、QA 和发布验收的统一口径：

| 领域 | 已确认口径 | PRD 落点 |
| --- | --- | --- |
| 菜单归属 | “账号面具”为一级菜单；系统设置只维护平台底座配置，不承载账号代理或授权指纹分配 | §2.8、§4.1、§8.4 |
| Clash | 系统设置维护多个 Clash 订阅源、主备优先级和同步健康；保存成功、订阅解析、节点同步、节点健康必须在系统设置按订阅源分阶段展示，授权槽位绑定只在“账号面具 > 账号代理”展示 | §2.8、§8.4、§9.7、§20.2 |
| 账号代理 | 单账号代理在“账号面具 > 账号代理”配置，粒度为 `account_id + developer_app_id/api_id + authorization_id/session_role`，同一授权槽位只能有一个 active 代理和一个 observed exit IP | §2.8、§5.2、§11.1 |
| 授权指纹 | 授权指纹在“账号面具 > 授权指纹”配置；配置指纹和远端观测指纹必须分开展示；保存配置只影响下一次连接 / 重登 / 新 session 初始化 | §2.8、§4.1、§5.2、§20.2 |
| 全账号指纹绑定 | 批量绑定范围是 active 运营账号的可执行授权槽位；接码专用、无 session、授权不可解密、缺开发者应用或缺代理的槽位必须失败可见，不能静默跳过或伪造远端变更 | §2.8、§5.2、§20.2 |
| 远端观测 | 远端观测来自 Telegram 授权设备快照；状态必须区分 `pending_effect / observed_matched / observed_mismatch / unobservable`，不得把“配置已保存”写成“远端已更新” | §2.8、§20.2、§21.2 |
| 搜索点击 | 用户可见名称为“搜索点击”；只执行搜索、翻页、目标匹配与点击，成功事实为完整 `target_click_observed`，不得创建 membership/admission child | §2.5、§5.2、§11.2 |
| 搜索群聊结果与未命中 | `@jisou` 关键词后先选群聊；仅精确目标命中结束成功搜索。真实末页未命中记录实际页码并保留任务重试 | §2.5、§11.2 |
| 非目标浏览 | 纯 `search_click + click_only` 不执行 decoy 或非目标点击；只搜索、翻页并点击批准目标。`search_rank_deboost` 的观察点击属于独立任务合同 | §2.5、§5.2、§11.2 |
| legacy search_join 节奏 | 每账号总上限、任务日/小时上限和各类 skip 只作为 `legacy_mixed_search_join` 历史字段；当前 `search_click` 的账号/关键词安全额度由系统事实维护，skip/jitter/quiet-hours 只作可压缩软排序 | §2.5、§5.2、§20.2 |
| legacy search_join 容量字段 | `type_config/pacing_config` 旧小时字段仅供迁移审计，不能形成当前 `search_click` 的容量或小时门禁 | §2.5、§11.2、§20.2 |
| LLM 边界 | `search_click` 实时 pacing / random decision 不调用 LLM；LLM 只用于配置建议、关键词生成、目标相关性解释和复盘分析 | §2.5、§5.2、§11.2 |
| 登录回车 | 登录、验证码和 2FA 主输入必须支持 Enter 回车提交，并复用点击主按钮的校验、loading、幂等和错误展示 | §2.8、§20.2 |
| 线上验收 | CI / build 通过不等于真实生产完成；线上 Clash 同步、授权快照刷新、出口观测和 Zhengzhou 3 账号真实纯搜索点击测试必须用生产服务器 SSH、生产 API 或已登录浏览器直接取证，不能用 Actions smoke 替代业务验收 | §18、§21 |

**`search_rank_deboost` 独立条款**：该任务可按自身专项执行排名观察点击，但不得复用纯 `search_click` 的 click ordinal、target 或完成事实。纯 `search_click` 非目标点击数固定为 0；其唯一成功仍是批准目标的 `target_click_observed`。

### 2.9 2026-05-23 更新记录

- 按 `docs/05-implementation/tg-ops-platform-prd-refactor-checklist.md` 同步 PRD、总设计和前端操作手册口径；本次更新只代表文档面完成收敛，代码重构仍需按清单分批实施。
- 后续实施优先级统一改为 P0-P7：基线和口径收敛、汇总读模型与运营异常、账号资产与可用性中心、目标画像中心、运营方案中心、运营中心重构、任务中心收敛、系统设置/手册/验收闭环。
- PRD 明确“任务中心失败需要上卷到运营中心展示，默认按目标看，点开后看到关联任务失败”；任务中心只负责执行详情、失败事实和调度控制。
- PRD 明确运营中心页面结构：上半部分是目标工作台，下半部分是运营方案 / 策略模板；系统设置不承载运营方案、任务节奏或异常处理。
- 前端内置操作手册必须同步真实菜单和最近更新，包括运营中心、运营方案模板、任务创建动态向导、账号资产与可用性、汇总延迟和系统设置边界。
- 素材运行配置补充用户侧输入口径：普通管理员只填写缓存频道链接、`@username` 或 `t.me/c/...` 链接；系统负责解析为运行所需 peer，不要求用户理解或手填 `-100...` 内部 ID。同时必须提供“缓存执行账号”显式选择，支持按手机号、备注名和 TG username 搜索，避免系统按健康分自动挑错账号。
- 素材运行配置保存必须绑定发起时的缓存配置 payload 签名与保存请求序号；保存返回前修改缓存频道、源媒体频道或缓存执行账号时，旧保存响应不得覆盖当前保存错误、刷新错误、成功提示或警告提示；loading 清理必须绑定当前保存请求序号。

### 2.10 2026-05-24 更新记录

- 频道浏览任务从一次性总量模型调整为帖子级产量模型：创建时选择初始帖子范围，开启持续监听后只纳入任务启动后的新帖。
- 频道浏览配置拆分为每条帖子每日浏览量、每条帖子累计目标浏览量（支持设置具体数值或 0/无上限）、帖子有效期和任务级每日安全上限，避免把所有历史帖子浏览量一起刷高。
- current 频道浏览以 `(tenant_id, target_peer_id, channel_message_id, account_id, obligation_local_date)` 保证自然日内单账号单消息去重；due unit 独立为 `(daily_message_target_id, due_ordinal)`，账号在 pre-Gateway 时作为可复用的当日物化绑定。次日同一账号自动恢复对该消息的浏览资格，继续按日目标执行；当天未完成 unit 由本 ledger immutable settlement 收口，不搬移 Action 冒充新 due。
- 风控中心账号评分必须直接展示扣分原因；低健康分账号即使没有最近失败记录，也要展示“健康分低于任务准入线”等可解释原因。
- 风控中心健康分归因改为硬口径：目标、权限、任务配置和内容规则失败不扣账号健康分，只进入命中记录、处置队列或目标能力异常；只有账号本体、账号运行环境和账号级限流才扣账号健康分。
- 风控中心总览卡片必须能联动下钻：点击降频账号、阻塞账号、待处理处置项、最近 FloodWait、代理告警等指标时，切到对应 Tab 并带上筛选条件；账号行的“账号中心处理”必须跳转到账号详情对应 Tab，且按钮列不能被截断。
- AI 活跃群和频道任务的准入前置账号明细必须展示计划时间和完成时间；已加入 / 已关注 / 已满足准入的账号要展示完成时间，未完成账号展示 `-`。
- 任务账号池必须按账号健康分降低参与权重：健康账号优先进入任务，低健康分账号只能少量参与，严重低分账号不进入任务候选。

### 2.11 2026-05-27 更新记录

- 线上 AI 活跃群排障结论纳入产品口径：同一“没有发送消息”可能同时由任务暂停、目标能力误判、账号未入群、单账号禁言和 AI 供应商超时组成，页面不能把它们合并成“图形验证码”或“群无权限”。
- 群聊准入候选必须覆盖任务账号配置选中的全部在线账号；`max_concurrent` 只限制主互动并发和本轮发送选择，不得截断入群 / 关注准入准备，也不得把发送容量、账号冷却或健康权重过滤误用为准入候选过滤。
- 任务状态展示升级为“主状态 + 派生运行阶段”：`paused` 必须显著提示“不会继续规划或执行”；`running` 必须继续展示 `启动校验中`、`准入补齐中`、`等待 AI`、`等待上下文`、`等待冷却`、`等待下一轮`、`发送中` 等阶段，不能只显示运行中。
- 目标权限诊断必须基于实时 Telegram 证据。普通 supergroup 成员的 Telethon 权限对象可能没有 `send_messages` 字段，缺字段不能直接判定为无发言权限；只有实际禁言、账号不在群、默认禁言、API 发送失败或明确权限错误才能展示为目标发言受限。
- 入群验证提示必须可刷新。机器人 / 图形验证码仅在当前群成员、最近消息或验证任务证据仍存在时展示；如果机器人已不在群、目标能力已恢复，应清除旧诊断并从旧错误中恢复任务。
- AI 供应商超时、空响应或无健康供应商要作为独立运行阶段和运营异常展示，不能混入账号权限或目标准入异常。

### 2.12 2026-05-31 更新记录

- 频道评论 / 回复运行时失败必须拆分归因，不能把所有 `GROUP_PERMISSION_DENIED` 或 “不能评论”统一归为跳过。系统必须区分账号未关注 / 未加入、账号已准入但评论区不可发言、频道消息本身无法评论、Telegram API 其他失败。
- 账号未关注 / 未加入导致无法评论时，评论 action 不应直接失败或跳过；应生成或补齐 `ensure_target_membership` 前置动作，当前评论进入等待准入或延后重试，任务详情展示“等待账号关注 / 加入频道后继续评论”。
- 频道消息本身不可评论时，系统必须标记该 `channel_messages.comment_available=false`，同帖待执行评论进入跳过，展示“该消息无法评论”，并提示可能原因：频道未绑定讨论组、帖子不是频道消息、讨论区入口不可解析或评论已关闭。
- 账号已确认准入但仍被 Telegram 拒绝评论时，只标记该账号对当前频道评论区不可发言，并跳过该账号后续评论；不能把整条频道消息标成不可评论，也不能影响其他账号继续评论。
- 任务详情 Action 列表必须把上述三类频道评论 / 回复异常映射到 `failure_diagnosis`：未准入可恢复展示“等待账号关注 / 加入频道后继续评论”，账号级不可评论展示“该账号对频道评论区不可发言”，消息级不可评论展示“该消息无法评论”；其他 TG / API 原始错误继续保留原始 `failure_type`、`failure_reason`、Trace 和尝试记录。
- 其他未归类的 TG / API 错误必须保留原始 `failure_type`、`error_message` 和 execution attempt，不得改写成泛化文案；页面提示“需要查看尝试记录和 Trace”时必须同时展示原始返回摘要。

### 2.13 2026-06-01 更新记录

- 开发者应用池定位为账号登录和授权容量分担能力，不再把单个 `api_id/api_hash` 作为全平台唯一入口。系统设置必须支持维护多个健康 TG Developer App，并用 `max_accounts`、健康状态和最近分配时间控制新账号分配。
- 账号中心新增“授权资产”口径：核心账号目标态为 1 个主授权和 2 个备用授权，每个授权资产都由 `developer_app + proxy + session` 组成，并且必须提前真实登录成功。仅配置备用开发者应用但没有备用 session，不视为可切换备用。
- 兼容当前实现：现有账号只有 `tg_accounts.developer_app_id + session_ciphertext` 时，系统将其映射为“主授权”。账号管理只提示“未配置备用授权”，不得因此阻塞账号现有任务、同步或登录恢复。
- 当前 v2.16 已把该历史清理口径收紧为：保留所有未撤销我方授权的非零 hash，一键清理其余可精确识别的远端设备；不强制保留官方锚点，不仅按 `api_id` 判断归属。新账号可立即查看设备，清理提交只按已持久化 current SV 登录时间严格超过 48 小时做本地分类，不调用 Telegram 资格预检；接码专用账号仍禁止自动清理其他登录设备，只展示风险和明细。
- 接码专用账号保持“只接码”硬边界：登录时如 Telegram 要求 2FA，平台只能使用并托管当前输入密码以便后续查看 / 备用 session 使用，不得自动轮换为新密码；账号详情托管 2FA 面板只保留查看 / 复制入口，保存和轮换接口必须返回明确错误。
- 主授权异常恢复顺序定为：先通过批准的线路迁移复用当前 session，再用健康授权读取 Telegram 官方 code 刷新非当前故障槽位；需要切换健康备用授权时必须创建 requested operation，并经审批、账号 fence、Gateway drain、owner fencing、CAS 和 readback 后执行。三槽位全部掉线时只能进入人工重新登录、扫码或手动验证码流程。2FA 只作为第二步校验补齐，不作为免验证码的独立登录入口。

### 2.14 2026-06-04 更新记录

- AI 活跃群和 AI 评论新增 Telegram 原生引用回复口径：引用回复必须在 Planner 阶段规划为独立 action，不得在执行层临时决定或用文本假引用替代。
- AI 活跃群配置“每轮最少引用回复数”，AI 评论配置“每条频道消息最少引用回复数”。新建任务默认值均为 `1`，最小值为 `1`；Planner 必须按最少数量创建带 `reply_to_message_id` 的引用回复 action。若当前目标没有合格候选，必须记录 `reply_target_shortfall` 并显式等待/跳过，不能把引用要求关闭、伪造引用或用普通正文替代。
- 2026-08-06 起，AI 活群不提供引用范围选择并固定只引用同 tenant、同 Task、同目标群的托管账号历史成功消息，且必须由成功 Attempt 的远端消息 ID 证明；监听到的其他成员消息只作上下文，不得成为 `reply_to_message_id`。频道评论仍按自身评论候选合同选择，不受此限制。
- 引用候选必须在 Planner/历史池选取和生成前 guard 各校验一次；两次都只能读取上述平台自有历史。候选不足时记录 `reply_target_shortfall` 并保持引用义务未满足，不得从 `GroupContextMessage`、真人消息或其他成员消息补齐。
- 普通消息和引用回复消息必须走不同生成提示词。引用回复在生成前先绑定具体引用对象，Prompt 必须包含被回复消息作者、原文、当前上下文和“本条是引用回复”的生成要求；不做生成后的语义匹配校验。
- 任务详情、Action payload、执行结果和审计必须展示引用关系，包括引用对象 ID、作者、预览、来源、Telegram 远端消息 ID 和失败原因，便于区分内容不自然、引用对象不足和 Telegram 执行失败。

### 2.15 2026-06-10 更新记录

- 入群验证码处理新增“视觉模型辅助”口径。群管理 bot 返回图片验证码时，系统必须下载当前验证图片，使用健康的多模态视觉供应商（MiMo/Mino 或 MiniMax）识别答案，发送验证回复后复检目标可发言能力。
- DeepSeek 等纯文本供应商不得被用于图片验证码识别；不能把视觉识图做成普通 AI 生成失败后的静默兜底。没有健康多模态视觉供应商、图片不可下载、识别低置信或复检失败时，必须写入明确失败原因并进入人工处理。
- 图片验证码尝试必须留痕到准入 action、`target_membership_challenge_attempts`、任务详情和审计记录，包含验证消息、媒体摘要、模型、答案、置信度、发送结果和复检结果。
- 每个账号对同一目标的同一图片验证码 fingerprint 最多自动提交一次 Telegram 验证回复；后续提交必须有新验证消息、人工确认或新问题证据，避免反复触发群管理 bot / Telegram 风控。提交前在当前健康且已审批的视觉供应商间进行识别不属于 Telegram 验证提交次数，也不计入 AI 活群/评论生成轮次、任务目标或发送限额。
- “最近验证聊天为空”必须作为独立准入诊断状态。系统已判定账号需要群管理 bot / 管理员验证，但 `challenge-context` 读取不到当前验证消息时，页面不能只展示空态并让运营人员猜答案；必须展示读取状态、目标 peer、加入账号、读取账号、最近读取时间、读取失败原因或“当前没有可用验证消息”的处理建议。
- 验证聊天弹窗里的“重新读取”不是单纯读取历史消息。它必须先对加入账号重新执行目标入群 / 准入动作，入群成功后立即复检发言能力；如果入群过程或复检返回需要验证码，再读取当前验证码上下文。
- 验证码上下文允许由两个账号协作：加入账号负责触发入群和最终提交验证码；同目标中已可读取群历史的账号可作为读取账号拉取机器人 / 管理员验证码消息和图片。读取账号不得代替加入账号提交验证码。
- 批量 `target_admission_retry` 中的“未解析到群关联频道”“未获群发言权限”“群无权限或账号不可发言”等结果不能直接归为纯人工处理。系统必须先按“重新加入 / 复检触发当前验证 -> 读取验证码上下文 -> 多模态视觉识别图片 -> 加入账号提交 -> 再复检发言能力”的闭环执行；只有刷新后仍无验证上下文、无健康多模态视觉供应商或复检失败时，才进入人工处理。
- 准入动作已进入 Telegram 调用边界但本地 worker 未拿到结果时，不能直接把 `unknown_after_send` 计为最终失败。恢复守护必须先用同账号补偿复检目标能力；复检确认已加入且可发言时写回准入成功和账号-目标可发言关系，复检仍失败或无法复检时才保留结果未知 / 人工确认。
- 准入补偿或后续准入 Action 明确成功后，必须清除该账号的 `manual_required`，并把没有绑定发送 Action 的准入型覆盖 `unknown` 重新按当前群关系计算为 `ready`；仍绑定 `send_message` Action 的发送结果未知账本必须保持 `unknown`，不得借准入成功自动重发或冒充完成。
- 准入汇总必须把 `unknown_after_send` 独立为结果未知账号数，不得计入 need_join 或 failed，也不得为同一账号自动创建新的准入 action；只有人工确认或补偿复检闭环后才能重排。

### 2.16 2026-06-14 更新记录

- 账号备用授权自动补齐新增专项 PRD：`docs/03-feature-designs/account-standby-auto-authorization-prd.md`。线上已有备用 TG Developer App 时，账号管理必须支持一键筛出未补齐 standby session 的账号，并创建 `account_standby_session_provision` 批次完成真实 Telegram 登录、主 session 自动读取 / 轮询验证码、2FA 校验、session 加密保存和健康检查；当 `standby_1`、`standby_2` 都缺失时，同一个账号项必须连续自动补齐两个备用 session。
- Developer App 数量不得等同于备用授权数量。只有已真实登录、可解密、可健康检查的 `standby_1 session` / `standby_2 session` 才计入健康备用 session。
- 自动补齐失败必须暴露明确账号级原因，包括 Developer App 不可用、代理不可用、验证码不可读取、2FA 未托管、托管 2FA 校验失败、Telegram 限制、session 加密保存失败和健康检查失败；不得静默跳过或标记伪成功。
- 主授权在线但缺少备用 session 时只提示恢复风险，不阻塞现有任务；账号详情“授权资产”和账号列表缺口筛选是补齐入口，任务中心负责展示系统批次投影和失败事实。

### 2.17 2026-07-10 生产核心页面有界加载设计

- 新增专项设计 `docs/03-feature-designs/production-page-bounded-loading-design.md`。本轮为 L2 / P1 `standard_team`，线上只读诊断证据为 E4；截至本记录，`design_status=complete`、`dev_handoff_ready=true`，但实现尚未开始，QA、产品验收、Release Gate 和生产修复均未完成。
- 生产 `GET /api/operation-targets` 已观测到 3,810 条、约 1.91 MB、17.288 秒，并触发前端 15 秒 abort；`GET /api/tasks` 已观测到 67 条、约 207 KB、成功样本约 3.43 秒和间歇 502。502 直接原因尚未由 nginx / 容器 / 数据库日志证明，必须保持 `unproven`。
- `GET /api/operation-targets` 的目标契约新增 `page/page_size/q/ids/linked_group_id/capability`，保留 `target_type/account_id`；第一方消费者必须显式有界，旧无新增参数语义暂兼容。分页响应通过 `X-Total-Count/X-Page/X-Page-Size` 返回元数据。
- 运营目标查询必须先分页目标，再只对当前页关联群做 SQL `GROUP BY` 条件计数；禁止全量物化 `TgGroupAccount` ORM 行。目标运行摘要支持按 `target_ids` 读取当前页。
- 任务中心新增 `GET /api/tasks/page` 和 `TaskListPageOut(items,total,page,page_size,summary,groups)`；普通 Task 与账号安全系统任务共同稳定排序、分页、计数，列表项不返回完整 `account_config/pacing_config/failure_policy/type_config`，详情继续按 `/api/tasks/{id}` 下钻，并消除系统批次 items N+1。
- 前端目标态为：运营目标服务端分页搜索；任务中心服务端分页 / 统计 / 分组并每 60 秒轮询当前查询；任务创建 / 编辑弹窗先打开后懒加载目标，支持远程搜索和 ids 回显；Overview 只读当前目标页；Rules / Archives 按需懒加载；MessageSending 按账号远程查询；AppShell 按 `linked_group_id` 定点查询。
- 保持公共 15 秒 timeout、请求序号、权限 / 租户隔离和错误可见性；本轮无迁移、无 worker 行为变化，发布失败或指标不达标时按代码版本回滚，不实现静默旧接口 fallback。

---

### 2.18 2026-07-26 AI 活群与搜索点击每日履约修复方向

生产核验确认：AI 活群和搜索目标群点击都不能以 worker 健康、Action 创建、pending 数量或局部 stats 替代日目标完成。两类任务均以任务时区自然日内的远端事实为准，并保留完整目标分母与原始 blocker。

> **本节现行执行口径（2026-08-04）：** AI/评论/点赞/浏览属于 `interaction_lane`，纯 `search_click + click_only` 属于 `search_lane`；两者只共享 Task/Action/Attempt 表和只读进度汇总，不共享执行槽、heartbeat、worker、代理/OCR 资源或失败熔断。四个 AI 任务和同一账号在不同任务的非冲突 RPC 同时推进，不存在任务抢账号。search solver 在 search worker 真实空闲时原子建立 assignment/Action；落库即成为数据库持久工作，按 lifecycle、业务 deadline、资格、binding、dedupe CAS 直接执行，不存在 Window、TaskAllocation/Reservation、二次分配或预扣。下方要求共享 Dispatcher、中央全任务份额或验证码 AI 的旧段仅保留历史取证，不得实现。

- AI 活群：只冻结任务日时间边界，不冻结不可缩小账号分母。账号范围按 `(task_id,target_group_id,account_id,task_day_ledger_id)` 独立动态维护；`eligible|recovering|completed` 计入当前必达数，Telegram 权威 Session 失效、需重登或不可发送时当日进入 `abandoned_for_day`，群解散则终结目标，其他任务中的同账号状态不受影响。daily fulfillment 必须把 `required_now`、`completed`、`recovering`、`abandoned_for_day`、`unknown_hold` 与 `shortfall` 分开审计。normal正文由同一active Provider key下主/备用模型各最多3轮生成；`mask_missing`只允许未完成coverage的direct义务由Planner走scoped签到分支，`normal_generation_exhausted`只允许coverage已完成的direct extra-volume由Generation写immutable handoff并交回Planner创建签到Action，其他六轮失败写`content_capacity_gap`。频道评论 legacy 使用原审核白名单单表情；启用 `channel_comment_business_grounding_v1_1` 的新消息按专项使用 20 个 Unicode 表情或冻结 `image_meme` 素材池。没有安全传输路线时保持等待。Planner命中任务合同非法时只暂停该Task并记录blocker，不得退出worker或形成忙重试；修正配置后由用户恢复。

#### 2.18-HIST 旧中央搜索、软节奏与共享 Dispatcher（historical_do_not_implement）

本小节直到 §2.18.1 前只保存旧事故设计和审计语义；其中的 Window、TaskAllocation、Reservation、中央份额、软节奏、静默权重、跨表锁序、重建 wave、接管或旧任务迁移均不得进入当前 schema、worker、发布或验收。

- **historical_do_not_implement（旧中央搜索分配）：** 搜索点击：合法请求完成结构校验后直接创建 `task_type=search_click`，创建阶段不做容量证明、不要求风险确认。任务启动后按不可变 `task_day_ledger_id` 建立当日点击目标，用稳定 `click_obligation_ordinal=1..N` 标识业务欠额，分开计算不扣 held/unknown 的真实 remaining 与防重 planning deficit。系统只枚举真实存在且通过 eligibility 的 `account × keyword × authorization_slot × proxy_route` 路径，不能把共享账号、关键词、授权、代理或 Gateway 额度的笛卡尔积当成容量；当前单用户 scope 内所有 running 搜索任务先取得当前 `dispatch_allocation_epoch` 的中央 fulfillment 份额，再建立该中央版本唯一的持久 `SearchClickAssignmentEpoch`。创建 open epoch 的同一事务原子绑定唯一 `solver_owner_lease_id`；唯一键冲突的 worker 只回读。该 lease 只作存活 fencing，健康 owner 求解期间持续续租，固定租约时长或心跳不得成为隐藏 solver deadline；只有进程失联、fencing token 失效或明确丢失续租所有权时才 abandoned，不转移 ownership、不重跑求解或保存 attempt/history。Planner 每轮必须先扫描并锁定所有 Window 的 open epoch：owner 仍活跃则跳过，owner 已失效则在原 Window 直接完整 abandoned/finalize，禁止因为它不属于当前 Window 而永久遗留，也禁止接管原 epoch；收口完成后才计算当前 Window。release wave 判断旧 Window 是否结束时必须把 PostgreSQL aware 时间和业务 naive 北京时间规范到同一北京时间语义，时区表示差异不得回滚 recovery。正常结果 finalize 的短 `SERIALIZABLE` 事务必须先按 Window → TaskAllocation → ShardAllocation → Reservation 锁定中央分配行，之后才读取 owner 和重建当前 solver 输入，禁止先建立旧快照再等待分配锁。该 epoch 即使零 assignment 也保存 `no_candidate|optimal|abandoned`、精确 `release_unit_set_hash`、同时覆盖 carrier-independent `solver_problem_hash`、carrier-specific `solver_input_hash`、matched/release/wave 结果的 outcome hash、`rebuild_input_version_after` 和 finalize 状态，只求解一次并只成功 finalize 一次；已 finalized 重放的 input/hash/wave 任一不一致都进入 `release_fact_incomplete`。它只承载首次 outcome 释放，finalize 后 assignment 的 Gateway 前失效/不再到期/过期必须由稳定 trigger 唯一的 `DispatchAllocationReleaseBatch` 释放。每个释放 unit 永久唯一绑定 Reservation/ordinal。尚可领取 Window 的首批非空释放只开启一个 pending rebuild wave；wave 内后续 batch 只增加 `rebuild_input_version`，不重复增加 dispatch epoch；已结束 Window 只收口释放事实，空集合不改变中央版本。重建只从最新输入重新创建整批分片权重并与 `ready` 原子发布，相同 carrier 重放不双扣，业务欠额不减少。图片算式验证码不做概率预测，`required` 不排除，只有实际 `solved` 才继续，最终 `failed` 才排除账号—协议路径；AI 调用及批准重试不占 click 限额。只有 assignment 及全部资源 reservation 原子提交成功的路径才进入 committed capacity。曲线、存量任务级小时/日 Action cap、`actions_per_round`、skip、jitter 与静默时段都是可压缩的软节奏，静默时段保持较低但非零执行量。账号/关键词安全额度、授权槽位、代理、协议样本、验证码真实状态、Gateway 防重、任务截止时间和 unknown 防重仍是硬边界。运行期硬安全容量不足只形成 blocker并持续重算，不停止任务，也不能用候选组合数、`max_source_attempts`、Action 数或 admission 事实替代 `target_click_observed`。

> **historical_do_not_implement（旧软节奏执行口径）：** quiet-hours 权重、活动曲线、旧未来 `next_run_at` 接管与 catch-up 仅作事故取证；当前五类履约任务不读取、不计算、不迁移这些字段，资源空闲即领取并执行。
> **historical_do_not_implement（旧中央搜索分配）—搜索 projection/commit 边界：** 未来只返回无写入的 `projected_eligible_attempt_capacity_before_deadline` 并标记 `projection_not_reserved=true`；它是硬安全事实下的尝试上界，不是预测确认数，旧名 `projected_confirmable_clicks_before_deadline` 禁止进入 schema/API/UI。只有当前 Claim Window 在中央全任务 TaskAllocation/Reservation 后提交的 assignment 才进入 `committed_click_opportunity_count`；它同样不是 click 成功。真实完成仍只认 `target_click_observed`。
- **historical_do_not_implement（旧共享 Dispatcher）：** 共用 Dispatcher：AI、评论、点赞、浏览、搜索和已设计准入必须先在真实 dispatcher scope 的 `DispatchClaimScope` 内核算跨 Window active capacity，并用真实 `executing + dispatch_claim_active` Action回写 Window/shard active 计数。每个 60 秒 Window 先按 `allocation_business_task_id=coalesce(admission_execution_sponsor_task_id,parent_task_id,task_id)` 聚合，再跨全部 shard 按 scope 级持久 cursor 给所有 `required_claims>0` 的父业务任务最多 1 个最低机会；剩余容量按未满足需求使用最大余数法分配并写 `DispatchClaimTaskAllocation`。父任务同时有 fulfillment/admission 债务时，获配 `>=2` 至少各 1，获配 1 按持久 lane cursor 跨 Window 轮转；纯搜索点击只有 fulfillment lane，固定 `admission_lane_claims=0`。`DispatchLaneShardSolver` 再用单次精确 task-lane-to-shard 三层匹配映射到有候选的 shard。准入 child 不得另取全局份额。同一 target/account/admission version 的已设计共享准入真实执行使用唯一 `AdmissionExecutionLease`，只由一个 sponsor 父任务的 admission lane 出资，成功后共享事实 fan-out。通用 claim 入口统一使用 `Scope -> Window -> TaskAllocation -> ShardAllocation -> Reservation -> Action` 锁序；纯搜索入口在 Reservation 与 Action 之间固定追加 `search carrier（如有） -> assignment -> 搜索 consumptive 子预留`，commit、claim、Gateway 最终守卫、release 与 Reconciler 一致，缺失层只跳过不得换序。任何架构或实现不得省略 TaskAllocation或反向加锁。禁止按 shard 重复最低份额、重复准入、贪心闲置容量、固定 search > AI 或 AI > 频道互动。终态、暂停/删除任务或已延后的 Action 只释放自身未领取槽位并写 `unclaimed_action_no_longer_due`；搜索 assignment 在首次 outcome finalize 后的此类释放必须走唯一 `DispatchAllocationReleaseBatch`，不得重开原 search epoch。任一到期任务无法获得份额时必须显示 `shared_dispatch_capacity_insufficient`。
- **historical_do_not_implement（旧共享 Dispatcher）：** 当前产品只有一个业务用户/一个业务租户；`dispatcher_scope` 是多个 worker、账号 shard 和任务类型共享的执行容量域，不是多租户公平层。`tenant_id` 仅继续用于隔离、唯一键和审计；未来若引入多用户，必须另立产品设计后再增加 tenant 级分配。
- **historical_do_not_implement（旧共享 Dispatcher）：** 调度术语统一：`DispatchLaneShardSolver` 只负责中央 task-lane 到 shard 映射；`SearchClickAssignmentSolver` 只在搜索已获 fulfillment 份额内绑定 click path。现有 `DispatchClaimWindow.allocation_epoch` 对外称 `dispatch_allocation_epoch`；每个中央版本只建立一条唯一 `SearchClickAssignmentEpoch` 结果行。该行同时保存 carrier-independent `solver_problem_hash` 与 carrier-specific `solver_input_hash`；两个 payload 均含 `solver_contract_version`，并覆盖其作用域内全部候选路径、资源、公平与中央份额版本，排除 worker/lease、时间和随机值。首次 outcome 的释放由该 epoch 承载，finalize 后的释放由唯一 `DispatchAllocationReleaseBatch` 承载；两者都为每个实际释放 unit 原子写跨状态永久唯一的 `DispatchAllocationExclusion(dispatch_claim_window_id,dispatch_claim_reservation_id,fulfillment_lane_claim_ordinal)`，并以带上下界守卫的汇总计数扣除原 unclaimed。exclusion 的 `resource_snapshot_hash` 必须按 `reason_code` 只覆盖本 unit 失败直接依赖的 Window、Task/ledger/target、solver input 或 assignment/Action version，以及相关额度、授权、代理、协议/CAPTCHA 和 Gateway 资源版本；无关 Task/shard、worker、lease、扫描时间或随机值不得使其 superseded。`no_feasible_search_path|search_solver_abandoned` 只绑定原业务问题分量的 `solver_problem_component_hash`，不能使用完整 input hash；换 worker、carrier epoch、Reservation 或 ordinal 不得使同一业务问题重新获配。尚可领取 Window 在 `ready` 收到首批非空有效释放时只生成一个 pending epoch、置 `rebuild_required` 并增加 `rebuild_input_version`；wave 内后续 batch 只增加输入版本，已结束 Window 与空有效集合不创建新 epoch。`DispatchLaneShardSolver` 必须冻结 epoch、input version 与规范化 `dispatch_rebuild_snapshot_hash`；该 hash 覆盖 pending carrier、全部 task/lane/shard 的 due/eligibility 当前值与版本、active exclusion、全部仍有效旧 Reservation 承诺/计数/版本、scope/shard 容量及影响分配的配置值/版本，并排除 worker/lease、时间、进程和随机值。提交前重算，三者任一变化都丢弃整批未发布权重，即使资源变化未推进 input version；成功时 Window 的 `ready_rebuild_snapshot_hash` 与全部新 TaskAllocation/ShardAllocation/Reservation 的 `dispatch_rebuild_snapshot_hash` 必须相同并和 `ready` 原子发布，零余额也发布带 hash 的空 ready。计算失败或崩溃同样丢弃未发布权重。exclusion 转 superseded/expired 也不得再次释放旧 unit；新事实只能使用新 Reservation/ordinal。carrier/outcome hash 与永久 unit 唯一键分别防重复 finalize 和双扣，相关 carrier/release batch item/exclusion/Reservation 在迟到 writer 可访问期间共同保留；fence 后只冷存 payload，主库永久保留 carrier key/hash、batch item 的 candidate unit、assignment/Action expected+observed version、逐 unit 分类/首 carrier 引用与 unit released identity tombstone，不能通过清理复活旧 unit或丢失 no-op 证据。该事实不能成为永久账号/任务黑名单或减少 click 欠额。`membership_admission` 仅表示入群、关注、确认/验证与 membership/can-send 复检，不表示 API 权限、搜索 target match 或 path eligibility；现有 `lane=admission/admission_lane_claims` 只是该业务链执行份额的兼容物理名。
> **中央重建 hash 完整性：** 上条 hash 清单只是最低集合。`dispatch_rebuild_snapshot_hash` 必须包含 `dispatch_rebuild_contract_version` 及 `DispatchLaneShardSolver` 所有影响输出的业务读取，至少包括 Scope/Window/Shard capacity/active/unclaimed 当前值与版本、全部 fairness cursor 与版本、parent/sponsor 聚合输入；新增影响输出的读取必须进入 payload并提升 contract version，版本只属于 hash payload。旧 Window claim、并发 Window cursor 或 sponsor 在计算期间变化，即使没有 release batch 推进 input version，也必须拒绝旧提交；worker/lease、时间、进程、随机值和纯诊断字段不得改变 hash。
> **中央重建提交隔离：** precommit 完整 input 重建、hash 比较、全部新 allocation/reservation 与 Window ready/hash 写入必须在一个短 PostgreSQL `SERIALIZABLE` 事务完成，覆盖输入行与候选谓词；rehash 后到 commit 前的 update/phantom 必须使事务 abort。serialization/CAS/hash 失败均废弃旧 solver 输出，由下一 drain 重新 assemble/solve，禁止驱动以旧权重自动重放。仅锁 Window 或复核标量版本不构成等价实现。
>
> **solver 契约版本发布栅栏：** `dispatch_rebuild_contract_version` 或搜索 `solver_contract_version` 变化时禁止新旧 Dispatcher 混跑。版本只进入规范化 hash payload，不保存运行历史；发布先阻止旧版本取得新 ownership，确认旧进程全部终止且无旧版本数据库事务仍可提交，再启动新版本。旧内存结果全部作废，pending rebuild 由新版本重建；旧 owner 的 open search epoch 在 fence 后直接 abandoned 并释放未领取 unit，不转移 ownership或沿用旧解。无法证明旧版本已失去写资格时 Release Gate 失败。
>
> **搜索问题 hash P0 修正：** 前述 `solver_input_hash` 继续承载当前 carrier/outcome 的完整幂等身份，但不得再直接作为 `search_solver_abandoned` exclusion 的 supersede 条件。`SearchClickAssignmentEpoch` 必须同时保存 carrier-independent `solver_problem_hash`：包含 contract version、稳定业务义务、候选/资源和相关公平输入，排除 Window/dispatch/search epoch、Reservation/ordinal/assignment ID、carrier 派生份额、worker/lease、时间和随机值。`no_feasible_search_path|search_solver_abandoned` 的每个 release unit 再按其连通分量投影 `solver_problem_component_hash` 作为 `resource_snapshot_hash`。仅创建新 epoch、换 Reservation/worker 或推进 carrier 版本不得 supersede；只有该分量真实业务输入改变才允许重新获配。outcome hash 必须同时覆盖 problem hash 与 input hash。
>
> **搜索输入快照 P0 修正：** 两个 hash 及 component/unit 映射只能由唯一 `SearchSolverSnapshotAssembler` 产生。Assembler 在同一一致性数据库快照内冻结不可变 problem snapshot、全部 component 的稳定 node/edge/resource/fairness payload/hash，以及每个 Reservation/ordinal 唯一的 component binding；共享资源或 fairness key 不能被拆到不同分量，无候选 unit 也必须有零边分量。open epoch、完整 snapshot/component/binding、两个 hash 与 owner lease 原子落库后才调用 solver；solver 禁止额外查库。owner 丢失 recovery 只使用原 binding/hash，exclusion supersede 也必须复用同一 Assembler/canonicalization，禁止重新组旧图、手拼另一套 hash、留下半快照 open 或把组装错误冒充 `no_candidate|optimal`。
>
> **搜索提交快照 P0 修正：** `stable_component_key` 由 contract version 与稳定业务义务、候选 edge、资源/fairness node 身份确定，不能是随机 ID或包含 carrier/worker/时间；component hash 再覆盖全部当前值/version。所有影响匹配、约束、目标或决胜的读取必须入 payload/hash，最低包括 `hard_safe_remaining_capacity`、同一冻结账号额度窗口内的 `confirmed_click_count_today`、持久机会时间/cursor 及来源 version，不能把 `today` 解释为提交时服务器日期。正常 `no_candidate|optimal` finalize 前，必须在短 PostgreSQL `SERIALIZABLE` 事务内使用同一 Assembler 与候选谓词重算 problem/input hash并逐项复核 source version；任一 phantom、资源、排序、公平或 carrier 漂移即整轮 `abandoned`，按原 binding 释放全部未领取 unit并重建分片权重。SQLSTATE `40001` 无论发生在锁定、写入还是 commit，旧事务回滚后都用新事务按原 binding 直接 abandoned/release/rebuild；数据库不可写导致新事务也无法提交时显式失败并保持 open，由 owner-loss recovery 收口。禁止提交、自动重放或重新求解旧解。
> **搜索释放并发闭环：** `DispatchAllocationReleaseBatch` 同时保存不可变 trigger 的 `candidate_unit_set_hash` 与锁内实际释放的 `release_unit_set_hash`，并为每个候选 unit 保存不可变 batch item、expected/observed assignment version 与 nullable Action version；`candidate_unit_count = release_unit_count + already_released_unit_count + precondition_lost_unit_count` 必须逐 item 守恒，outcome 与三类计数唯一对应。batch `outcome_hash` 必须绑定 Window/source epoch/trigger carrier、candidate hash、稳定排序的全部 item 分类及 expected/observed assignment/Action version、首 carrier、release hash、三类 count、outcome 和实际 next epoch/input version；finalized 重放只有这些字段全部一致才零写回读，错绑保持 `release_fact_incomplete`，同 trigger 不同 candidate 才是 `release_batch_input_conflict`。同一 assignment 被 expiry、Action 终态和 Window expiry 等不同 trigger 同时命中时，只有 assignment 与 bound Action 都仍为 expected version、Action 未进入 Gateway 且无永久 exclusion 的 unit 可以释放；`action_bound` 的有效释放必须在同一事务把 Action 终结为原因对应的 pre-Gateway `failed|skipped`、清 lease/active，并保留绑定证据。已经由其他 carrier 释放且 Action 已不可领取的 unit 记 `already_released` 并引用首 carrier，已经 claimed/Gateway-started 或任一版本变化的 unit 记 `precondition_lost`，二者都以 no-op 收口且不改计数或 Action。assignment/Action/exclusion/claim/Gateway/计数互相矛盾时，release 事务先整批回滚，再由独立 writer 复核并持久化对象级 quarantine。Reconciler 分支前先验证合法 release fact set：首次 outcome 必须为 finalized search epoch + `release_unit_set_hash` 内 unit + matching exclusion，post-finalize 必须为 finalized batch + `effective_released` matching item + matching exclusion；carrier/unit/hash/reason/version/计数不一致，或只有部分组件时保持 `release_fact_incomplete` quarantine，不能自动判 released。完整事实仅按四分支处理：合法 release fact set 且无 claim 时，以逐 unit 事实为权威；存在 assignment 时对齐为 released，首次 outcome 的未绑定 unit 保持无 assignment；终结遗留 Action并重算摘要，使 unit 只贡献一次 released；孤立 released 且无任何 release 组件时恢复 `reserved|action_bound` 并推进版本；只有 claim/Gateway 且无任何 release 组件时向远端事实对齐；合法 release fact set 与 claim/Gateway 同时存在时写 `release_claim_fact_conflict`，禁止自动删 release 组件、回滚 Gateway、选边、调 released/claimed 计数、resolve 或忙重试。只有前三类提交后才唤醒 trigger；冲突 unit 隔离期间完整 click evidence仍照实入账，但相关 ledger 不得通过 E4。effective release set 为空也要 finalize trigger，但不推动 rebuild；所有路径均不重跑搜索求解，也不能靠日志猜测 unit 结果。
>
> `precondition_lost` 只终结冻结旧 expected version 的 trigger，不表示任意 Gateway 前新版本都已释放。状态机禁止从 claim/Gateway/unknown/consumed 倒退到 `reserved|action_bound`；observed 已越过该边界时永不再释放。只有 observed 仍为新的 `reserved|action_bound` pre-Gateway version（例如并发 replacement/资格复核仅推进 version），且释放条件仍成立，产生该版本的状态变更事务/outbox 才以新版本生成全新 trigger key/candidate hash。旧 batch 不重开，无版本变化事件不轮询造 trigger，Gateway 前新版本占用也不能永久泄漏。
> **搜索首次 outcome 所有权：** 当前中央版本的 search fulfillment Reservation 自 `ready` 发布起，到唯一 `SearchClickAssignmentEpoch` 首次 finalize 前由搜索物化流程独占；通用 no-Action、unclaimed 或 expiry reclaimer 必须跳过。Window 可领取时由首个有效 owner 建立并执行一次 epoch；若 Window 在结果行建立前已结束，recovery 建立后直接 abandoned，不调用 solver。任务停止或 due 消失只使原 epoch 的 optimal 前置失效，不产生第二类 carrier。首次 finalize 后每个来源 Reservation 必须满足 `bound+claimed+released=reserved`，后续只有 bound assignment 走 release batch；通用 reclaimer 抢先触碰属于一致性违规。
> **重建期间已落库 assignment：** `allocation_state=ready` 只控制新中央版本和新 search epoch/assignment 发布。optimal 的 unmatched 触发 `rebuild_required` 后，同批已落库 matched assignment 在 lifecycle、业务 deadline、资格、binding 和 dedupe 仍有效时直接进入 Gateway；不等待新 ready、不读取未发布权重、不预扣新 Window、不再校验 Scope 容量。只有上述 Gateway 前事实失效才走稳定 release batch。
- 极搜：必须先按版本化协议样本识别页面相位；热搜排行榜、验证页、未知页和正确分类页缺 selector 是不同错误。运行期禁止发送 `/cancel`、`/start`、重发关键词、点击外链或未知 callback 来 reset 会话；`hot_list_page` 直接写 `jisou_hot_list_page` 失败并将当前账号—协议路径排除 12 小时，`unknown_page` 写 `jisou_session_state_deviated`；验证码按已批准流程处理。该账号级安全事实不减少 click 欠额、不停止其他账号，也不是 `DispatchAllocationExclusion`。当前 search epoch 的单次求解会读取这些 eligibility 事实；若结果为 `no_candidate|abandoned`，按被放弃的中央 Reservation unit 写统一 exclusion。尚可领取 Window 的非空 release set 加入唯一 pending rebuild wave，空集合只 finalize 当前搜索 epoch，已结束 Window 只收口释放事实；不在原份额上重映射或重试。历史 `recovery_kind/reset_executed/reset_action_key` 只读保留，不再创建 reset Action、事件或次数。

跨任务履约当前合同真相源为 `task-fulfillment-classified-recovery-prd.md` 与 `task-fulfillment-contract-closure-prd.md`；AI 群日目标以 `ai-group-daily-group-target-redesign-prd.md` 为专项补充，纯搜索点击以重写后的 `search-click-daily-fulfillment-remediation-prd.md` 为专项补充。`all-task-fulfillment-recovery-prd.md`、旧全账号覆盖/日履约/数量槽专项和 shared-dispatch 方案均为 `historical_do_not_implement`。设计完成、代码、迁移、发布与真实生产 E4 证据仍须分别验收。

> **历史区间标记：** 本节后续凡仍写“冻结分母/`frozen_account_count`”“ContentMix/主数量槽拥有义务”“搜索进入中央 Window/TaskAllocation/Reservation”“账号或同群全局单执行”“旧 `all-task-fulfillment-recovery-prd.md` 为当前交接”的 2026-07/08-01/08-02 段落，全部为 `historical_do_not_implement`，即使旧标题仍含 `supersede/resync/当前` 字样也不再生效。2026-08-03/08-04段只在未被2026-08-09 AI obligation与2026-08-10 channel-view due-unit专项显式修订的部分继续有效；开发、迁移和验收优先读取日期更新的专项与本节修订句。

> **2026-08-03 C1 任务内动态账号范围 supersede：** AI 活群只冻结 `task_day_ledger_id` 的时间/时区边界，不冻结不可缩小账号分母。当前覆盖范围按 `(task_id,target_group_id,account_id,task_day_ledger_id)` 独立动态维护；可恢复账号自动回流，无合法恢复路径时当日 `abandoned_for_day`。暂停/停止/删除任务立即退出新规划、Generation、claim 和容量统计，未进 Gateway 运行残留按 `task_id` 清理。多个 running 任务公平并发；`4000+5000+800+800` 是同时独立目标，不是串行排空。本文后续任何 `frozen_account_count`、“暂停/删除后保留义务”或“单任务先排空”的冲突表述均只作历史审计。详细合同以 `task-fulfillment-classified-recovery-prd.md` §2.2/§4 和 `ai-group-daily-group-target-redesign-prd.md` §3–5 为准。

> **2026-08-04 履约合同闭合 supersede（2026-08-10 范围修订）：** 当前强制合同新增 `scope_fact_version`、`task_lifecycle_epoch`、Gateway 前 fencing、Task 主记录物理删除+最小远端 tombstone、搜索 assignment/极搜 page phase 持久化并直接执行、账号在多任务中并发操作、绑定明确 target-group observation surface 的 C2 连续 30 秒无提示通过、组合展示名+要求链接绑定、同提示多 requirement action、权威 remote fact 先行与 projector 收敛、direct 非严格 context equality/reply 严格 CAS、唯一 active Provider key、双 OCR 安全预算与 C8 随机查看账号候选池 -1。`prepared new Task -> route epoch -> delete old Task` 只保留给明确采用该 release-train 的其他任务；当前 AI 活群与频道浏览故障修复都以原 Task additive route/fence/manifest/readback 原地接管，禁止复制新 Task 后从 0 计量。完整定义见各专项与 `task-fulfillment-contract-closure-prd.md`；本文后续冲突表述仅作历史审计。

> **2026-08-04 完成目标口径 current_contract（2026-08-10 AI obligation resync）：** UI兼容名`planned_daily_target=max(daily_message_target,current_required_account_count)`对应持久`base_planned_target/effective_planned_target`；已确认数不反向抬高计划。AI current数量义务使用稳定UUID与target-local`quantity_ordinal`确定不可变identity，另用`effective_due_rank`确定当前target位置；目标下调只retire安全rank，boundary owner转protected overage，后续增长复用rank位置但分配新identity ordinal。stop-safe取消也必须同事务retire active rank，重启只以更高ordinal补空rank；未重启不缩结算分母。当前DueSet/settlement只认active owner映射，protected overage不补低rank缺口，缺owner rank在deadline为known shortfall。动态账号加入/放弃只推进effective revision，base target与route`target_set_hash`不变。每个义务在Gateway前只对自身执行带deadline、route、lifecycle、intent与version的单行CAS，不锁整本日账、不预扣总预算；Gateway started/unknown/confirmed单向守恒。E4按active-rank DueSet、bound quantity fact、on-time/late/unproven timeliness与immutable settlement验收，不能只用`confirmed>=planned`；deadline后late fact可修历史但不得把missed改为met。本文后续任何`max(...,confirmed)`、无ordinal/rank、按row count代替due或不区分on-time/late的口径仅作历史审计。

> **2026-08-03 账号并行 supersede：** 同一账号可同时为不同任务发起非冲突 Telegram RPC，不建立账号内 task cursor、任务抢占或全局单 inflight。只有同一 `remote_mutation_key`/同 callback/同远端副作用使用幂等 CAS；Telegram 权威 FloodWait 约束账号后续 RPC，SlowMode 只约束对应 peer，二者不改写任务资格。

> **2026-08-04 最终闭合补丁（2026-08-10 current 范围修订）：** AI动态目标由`base/effective target revision + stable obligation UUID + quantity_ordinal/active due rank`持有；频道浏览由peer-message target+due ordinal持有。旧Task删除仍使用持久stage/item/checkpoint，只有最小remote tombstone完整校验后才物理删除。当前AI与浏览存量Task不复制、不从0重算：全role先部署fence-compatible baseline，再按原Task执行inventory、quiescence、final manifest、chunk backfill、readback与class-specific activation。AI分类覆盖never-started、same-period running/paused/stopped、live settling-closed、rollover-eligible、terminal-retired；浏览分类覆盖zero-history draft/pending/scheduled/stopped-never-started、same-period running/paused/stopped、live settling/closed、rollover-eligible、target_reached/wrapping_up closed、terminal settling/retired，非法组合typed blocked。每类保持原lifecycle，只有running类可恢复发送，paused/stopped/terminal不得自动启动；Gateway后只允许reconcile/前向修复。完整字段、状态、发布与QA见两个专项及`task-fulfillment-contract-closure-prd.md`。

> **historical_do_not_implement（旧共享 Dispatcher）—2026-08-01 共享调度与 AI 履约恢复 resync：** 同一 `dispatcher_scope` 的普通 Action 必须由唯一 `runtime_account_shard_total` 映射，生产两个 Dispatcher 固定为 `(2,0)` 与 `(2,1)`；搜索 assignment 物化前的中央 source 继续使用虚拟 `(1,0)`，虚拟分片不得被普通 Action消费。从 Window `ready` 到唯一 `SearchClickAssignmentEpoch` 首次 finalize 前，search fulfillment Reservation 即使 `bound=0` 也由搜索物化流程独占，通用 reclaimer 写入必须为0；finalize 后才只有有效 bound unit跨重建受保护，未匹配/失效 unit必须由合法 search carrier逐 unit release/exclusion。普通旧 epoch无到期 Action的 unclaimed可按普通 counter/reason释放，但不得伪造搜索 ordinal/exclusion。配置 topology/capacity fingerprint使用版本化 canonical payload，和 heartbeat liveness分离；当前2 worker、每 shard有效并发13、总 configured capacity为26，stale shard不获新份额且 live新增预算随之降低，账号不跨 shard接管。所有 claim/confirm/release/reconcile统一按 `Scope -> Window -> TaskAllocation -> ShardAllocation -> Reservation -> search carrier/assignment（如有） -> Action -> Task/业务账本` 加锁；Gateway前 B0只提交 `gateway_call_started_at` 防重边界，Gateway后 claim release、Action/Attempt和类型账本必须在同一 B1事务原子提交，Task stats随后独立投影。历史 AI Action接管必须在全部 writer处于 `preparing` fence时，以持久 batch/item/cursor执行；完成后才原子切换 active合同。success、unknown和Gateway-started不自动改写或重发，只能进入只读远端核验。完整专项设计、迁移、Release Gate与E4口径见 `docs/03-feature-designs/shared-dispatch-and-ai-fulfillment-recovery-prd.md`。

> **2026-08-01 完成性审计修补 resync：** B0/journal/B1覆盖群管频道follow和精确callback等全部远端mutation；成功journal必须带匹配action type的类型化remote fact，B1 crash后的统一RemoteReconcileCase apply重放membership/admission专用事实，不能只改Action。membership unknown的权威reprobe必须进入同一case expected hash/evidence CAS，禁止旧Recovery在case外先改Action/Attempt；存量无Attempt，或最新遗留Attempt缺冻结request identity时，必须保留旧Attempt并追加持久建立read-only recovery Attempt/Case，精确recovery claim获取和释放后都推进完整Action expected hash。worker heartbeat保留历史但优雅退出显式`stopped`，同一worker后续metadata刷新采用合并语义且不得删除`dispatch_contract_version`；正常stop -> stage -> verify-ready不得依赖120秒自然过期。生产backend启动前必须fail closed拒绝`ENABLE_EMBEDDED_WORKER=true`，消除Stage A candidate落库前的旧合同写窗口。

> **historical_do_not_implement（2026-07-28 旧完成优先/兜底合集，2026-08-10 superseded）：** 本段保留旧事故口径，不得实现其中“冻结账号分母、六轮任意direct直接签到、缺面具/代理切换直接签到、旧Phase/Cycle/Reservation owner”等语义。current AI只认stable obligation的monotonic identity ordinal + active due rank、动态scope、aggregate assignment/intent、GenerationJob/variation与两条typed签到分支；current浏览只认peer-message target+due ordinal及global daily owner。评论、点赞、click各按自身专项。所有current字段、迁移和验收以对应专项、`all-task-fulfillment-recovery-prd.md`、`ai-group-daily-group-target-redesign-prd.md`和`search-click-daily-fulfillment-remediation-prd.md`为准。

> **2026-08-10 浏览义务身份修订：** 上一段中“浏览的source固定为account”仅作历史。current业务due unit是`ChannelViewDailyMessageTarget + due_ordinal`，账号只在pre-Gateway作为可释放materialization绑定；`ViewRemoteFact(peer,message,account,obligation_local_date)`继续daily unique并最多bound一个due unit。任何实现不得以已选账号obligation行数代替DueSet。

> **2026-08-10 跨任务账号短冷却与AI任务级open门禁取消 resync：** 当前产品只有一个业务用户/租户，任务目标允许同一账号重复履约；通用`SchedulingSetting.default_account_cooldown_seconds`不再作为账号候选、Planner、claim或Gateway前门禁。正式发布在全部业务writer fenced的接管窗口中，对平台与租户存量调度行幂等归零；重复apply不新增审计或漂移。字段/API暂保留以兼容配置读模型，但current合同禁止写入非零有效值。接管清除`last_error=account_cooldown`并立即唤醒。AI Planner不得因Task已有一条到期open/ready Action就整Task延后：必须在同一snapshot以ActiveDueRankSet对bound fact、Gateway hold、unknown和每个valid pre-call owner做逐rank anti-join，仍对其它MaterializationGap执行本轮0～20有界物化；30秒只可成为该精确owner的next-check/wake，不是Task级gate。单Task本轮后立即让出给下一Task，搜索等其它任务仍按公平轮转。账号小时/日数量上限继续归一为`1_000_000`；Telegram FloodWait/SlowMode、账号授权、代理、协议、验证码、目标权限、unknown和远程副作用key防重不变。同账号非冲突RPC并行，不保留账号单次in-flight/Redis全局互斥。QA必须覆盖一个valid open owner与19个无owner active rank同存时仍物化其余gap、批次后轮转其它Task、非零存量值归零、重复执行和队首公平；发布走`master -> release -> Deploy Production`，以生产配置为0、AI MaterializationGap收敛、搜索Action/Attempt及最终typed remote fact分层验收。若发布失败，代码与配置随原子release不激活，禁止手工在线回填或静默恢复180秒冷却。

> **2026-08-10 current AI own-history事实查询：** reply候选必须从当前tenant/Task/群内`binding_state=bound`的canonical remote fact/quantity binding读取，并要求同一fact保存可引用remote message identity与冻结正文；Action/ExecutionAttempt仅用于provenance和timeliness证据，成功状态或非空remote ID本身不构成reply资格。查询按typed fact/binding scope索引做相关keyset读取，保留in-flight reply排除，禁止全表聚合Attempt；`GroupContextMessage`只作上下文，不得成为own-history引用目标。发布验收分别核对Planner latency、typed binding、ready Action/Attempt和真实远端结果。

> **2026-08-02 搜索 Window 竞态补正：** Planner 查询修复上线后搜索 Task 已按分钟推进，但普通 Dispatcher 先创建的 ready Window 不含未物化搜索义务，搜索 Planner 后到时禁止重分配，导致 reservation/epoch/assignment 永远为 0。中央 Window 的创建/重建需求必须同时合并 open 搜索点击义务和已物化普通 Action，并沿用 parent-first 公平分配，保证普通 Task 与搜索 Task 均有份额；搜索 Planner 只消费已发布 reservation 后再求解路径和创建 Action。禁止让 Dispatcher 代替搜索求解，也禁止再次删除搜索需求来伪装 AI 容量恢复。

> **2026-07-28 AI 可见性 P0 统一口径：**
>
> 1. current AI 的 `pending_visibility` 与 `unknown_after_send` 都属于同一 stable obligation/request 的 post-Gateway 未确认占位；唯一 committed gateway hold 计入 unknown，绝不为同一 active due rank创建replacement。旧 `primary_quantity_slot_id` 与 `PendingVisibilityCredit/pending_visibility_credits` 只作takeover alias/兼容物理名，不是成功credit或current planning owner。
> 2. `post_send_intercepted` 明确失败并关闭当前 hold。存在合法自动恢复路径时账号进入 `recovering` 并保留本任务 coverage；没有合法恢复路径时进入 `abandoned_for_day`，终结未进 Gateway coverage/Action 并从当前必达数移除。任何 abandon 都不得改写或重发 Gateway-started/pending/unknown。
> 3. 需要可见性核验时，Attempt 即使已有 `remote_message_id` 也不能确认群日或 coverage。只有 `visible_confirmed` 才在同一短事务关闭 hold、完成 Action、确认群日主槽和可选 coverage；拦截/确认失败不得增加 confirmed，并发 finalize 通过唯一键/CAS 只允许一次完整提交。

> **2026-07-28 非 AI 事实所有权（2026-08-30 浏览口径补正）：** 不使用通用 quantity slot 不代表同一远端副作用可跨 Task 复用。评论 remote ID 只能归属一个评论 ordinal；同账号/消息的未变化 reaction state 与**同一业务日期内的 daily view fact**各只能完成一个 Task 义务，浏览账号进入下一业务日期后形成新的 daily identity；click evidence hash 只能归属一个 Attempt/ordinal。远端事实早于 Task/ledger 义务起点时只作为历史状态，不得倒灌为完成；归属冲突只隔离受影响对象并显示 `remote_fact_owned_elsewhere`，不得停止其他独立义务。

> **2026-07-28 搜索纯 click 求解：** 跨 Task 公平必须进入共享资源求解器，不能只做求解前排序。候选图按 click ordinal、资源 key 和连接同一 Task 候选的 `assignment_fairness_key` 耦合；目标依次固定最大可提交 click assignment 数、最大受服务到期任务数、按冻结 `assigned/max(remaining,1)` 升序向量的最大最小任务公平和稳定 path 顺序，后阶段不得降低前阶段最优值。整个快照无路径才 `no_candidate`，局部无路径在 `optimal.task_unmatched_reasons` 显示且不清零其他 Task。`optimal` 必须同时返回 matched、served-task、task assignment/unmatched/fairness、unmatched ordinal 和 saturated。
>
> 未点击账号与合法 repeat 路径必须同时进入候选图；“不同账号优先”只能在最大 click、最大受服务任务数和跨 Task 公平向量已经固定的同值解中决胜，不能作为求解前硬筛选而牺牲 click 履约。
>
> **historical_do_not_implement（旧中央搜索分配）：** 搜索容量求解分成无写入的未来 `projection` 与当前 Claim Window 的 `commit`。projection 不创建 assignment、Action、claim 或 quota hold，只显示 `projection_not_reserved=true`；commit 必须等待 Window `allocation_state=ready` 并取得当前 epoch 的全任务 `DispatchClaimTaskAllocation`、search fulfillment lane 和 shard Reservation，再建立该中央版本唯一的 `SearchClickAssignmentEpoch`，由 `SearchClickAssignmentSolver` 在每 Task 已获份额内绑定路径、搜索专属子预留、assignment 与 Action。求解器读取结束后必须关闭该读事务，再从无查询的新事务起点设置 PostgreSQL `SERIALIZABLE` 并执行 finalize；不得在已有 active transaction 中迟到执行 `SET TRANSACTION`。Dispatcher/Gateway 共享 inflight 只允许中央 Reservation 占用一次，搜索不得在 Planner 阶段提前预留或重复预留。一个 epoch 只求解一次并只成功 finalize 一次；`optimal` finalize 必须同时确认 Window 尚可领取、ready 且当前 dispatch epoch 与 search epoch 完全一致，不能因 Window 已在更高 epoch 重建回 ready 而提交旧匹配；不满足时只能 abandoned。`optimal` 的 unmatched 与 `no_candidate|abandoned` 的全部未领取 unit 分别组成首次 release set。epoch finalized 后，bound assignment 的 Gateway 前路径失效、Action 不再到期或到期释放由稳定 trigger 唯一的 `DispatchAllocationReleaseBatch` 承载，不能重开或改写原 outcome。每个 exclusion 固定 Reservation ID、份额 ordinal、首个实际释放 carrier 和 `release_count=1`；同一 carrier finalize 事务一次性写全部 effective exclusion、汇总更新 Reservation `bound/released` 与各层 unclaimed。重叠 trigger 已释放或已 claim 的 unit 只作 no-op 分类。尚可领取 Window 的首批非空 effective release set 只生成一个 pending dispatch epoch并置 `rebuild_required`，wave 内后续实际释放只增加 `rebuild_input_version`；已结束 Window 和空 effective set 不改变中央 epoch。`DispatchLaneShardSolver` 冻结 epoch、input version 与 `dispatch_rebuild_snapshot_hash`，提交前重算同一规范化资源输入；任一不一致都丢弃整批未发布权重，成功时新权重行和 Window ready 固化同一 hash。claimed/active 份额、其他有效旧 Reservation 和公平 cursor 不回退，click 欠额不减少；同 trigger 重放按 candidate hash 回读，不同 trigger 重叠按永久 exclusion/no-op 收口，均不能重复释放。

> **2026-07-28 内容编排非回归边界（2026-08-31 评论 v1.1 补正）：** 上述修复只删除/调整“是否继续创建、何时发送”的门禁和节奏，不修改 AI 活群与频道评论既有 `reply_min_per_round/reply_min_per_message`、direct/reply 拆槽、账号面具 `emoji_policy`、正常文本 emoji 习惯/占比、普通图片/表情包/custom emoji 素材比例、`material_intent/allow_material`、意图映射或素材规则。Planner 必须先冻结关系槽位，并在每个 Action attempt 冻结具体 `reply_to_message_id`；任何 attempt 都不得被原地改写，只有 Gateway 前确认引用对象失效时才可在同一 reply 槽递增 attempt 并选择新合法对象。正常内容仍按原素材链选择；签到/表情兜底只替换原数量/关系槽内容并保留有效引用及已冻结素材义务审计，是否共载素材必须在 Gateway 前按兼容矩阵决定，不兼容义务先 CAS 转派。确定性兜底只能确认总发送量：Unicode 不能消费正常文本 emoji 或素材配额，图片表情包也不能冒充普通 image/sticker/custom emoji；总量、引用占比、普通 emoji/素材占比和两类兜底数量必须分账展示，且不得为补比例隐式超出配置总量。

频道评论必须在 `task+channel_message+comment_plan_revision` 作用域建立不可变 `ContentMixContract`，把现有 RuleSet 比例解析成 required/max count、取整过程、seed 和规则版本；未知版本、最低数超过总槽位或 required>max 必须显式失败，不能套新默认值。评论继续以首次纳入消息时冻结的 `comment_plan_revision` 管理关系、素材与结算，运行中配置修改只影响之后新纳入的消息；`scope_total_slots` 在兜底判断前冻结，任何 fallback 都不得缩小显式比例分母。显式最低素材形成 `obligation_source=policy_min`，旧选择器已经确定的额外普通图片、表情包、custom emoji 或正常文本 emoji 计划形成 `obligation_source=selector_plan`；两类素材义务在兜底未携带相应正常素材时以 `assignment_version` CAS 转派到同 scope 未进 Gateway 的正常槽位。图片表情包只可结算 fallback-eligible 或显式 image_meme 兜底槽，不得满足普通图片/sticker/custom emoji 义务。没有合法槽位时明确 content mix shortfall，禁止超量补发。列表和详情同时展示 `quantity_status/content_mix_status/grounding_quality_status/acceptance_status`；未启用 grounding 的旧 revision 该维度为 `not_applicable`，其余适用维度全部 `met` 才整体 `met`。

频道评论新 v1.1 消息首次规划必须建立唯一 `ChannelCommentPlanContract`：以 Telegram `source_published_at` 起算三天，冻结 stable eligible 账号事实、最接近稳定 60%±5 个百分点的整数 distinct-account 目标、全部 ordinal/direct-reply、一个 ContentMixContract、首个 GroundingSnapshot/semantic capacity、全部首版 Assignment，以及 Task `CommentFallbackPolicySnapshot` 的 20 表情白名单/素材组/显式文字图片 bps 权重与消息级 `ChannelCommentFallbackPoolSnapshot` 的 ready 图片素材版本；`eligible=0` blocked，小池显示 actual bps。`AI_COMMENT_MAX_PER_MESSAGE=80` 与技术 batch 不能截断目标，后续批次只按 due JIT 物化 Action；Task-wide allocation epoch 在开放消息间 max-min 轮转 future Daily Cap reservation，时区切换使用不重叠 UTC capacity period，transition 折算且 rolling 24h 不超过一份 cap。Telegram 编辑使用 append-only Source/Grounding/Assignment successor，只替换未进 Gateway 原 ordinal，不新增数量；删除、pause/resume/stop 按专项终止、释放和剩余曲线结算。GenerationJob 复用公共预算；正常候选冻结 accepted hash，兜底通过每 Plan/kind cursor-backed stable shuffle bag 从 20 Unicode 表情或冻结 `image_meme` asset-version pool 选择，同槽重试不换内容。图片失效仅可在 Gateway 前按冻结池 append 顺延，池耗尽跨 Unicode 必须由 policy 显式允许；Gateway-started/unknown 禁止重选。planned fallback 可在 fallback-eligible plain/relation 槽结算但不计 grounded，emergency fallback 只保 quantity 并暴露质量 shortfall。`quantity_status + content_mix_status + grounding_quality_status` 共同决定 message acceptance；Task current、最近 7/30 天 SLA 与 lifetime outcome 分列。完整合同见 `docs/03-feature-designs/ai-channel-comment-broadcast-and-teacher-relevance-prd.md`。

本段是启用 `channel_comment_business_grounding_v1_1` 后的优先合同，取代本文较早出现的“评论固定滚动 24 小时”“主 3 轮 + 备用 3 轮”“只有 3 个单表情”和“只展示 quantity/content mix”表述；这些旧口径仅继续解释 flag-off legacy 消息，不得与 v1.1 叠加。

> **AI current 合同（取代旧 Cycle/Action-first 路径）：** `fact_first_v3` 以 stable `AiGroupMessageObligation` 承载单调 quantity identity ordinal 与当前 active due rank，再按 aggregate allocation plan/assignment 冻结 immutable content intent；Generation worker 先持久化 `GenerationJob` 与 variation/rejection history，只有合法 variation 或 scoped check-in ready 后才创建 fenced `Action`。随后才进入 `Attempt -> Gateway -> append remote fact -> quantity binding(timeliness) -> target/coverage/content/read-model projector -> immutable settlement`。旧 `ContentMixCycle/ContentMixCycleSlot/primary_quantity_slot_id` 对 current AI 均为 `historical_do_not_implement`，只能作为 takeover alias/只读迁移投影，不能成为数量、内容、Action、准入或恢复 owner。完整合同只认 `ai-group-generation-failure-churn-remediation-prd.md`。

#### 2.18.1 2026-07-27 AI 日覆盖 pre-Gateway 与 claim recovery 修订

生产证据确认，AI 日覆盖 overdue 不能一概视为 Telegram 远端未知。`ExecutionAttempt.gateway_call_started_at` 是唯一边界：为空时覆盖行保持 `reserved + dispatcher_lag + dispatcher_recheck`，仍占自身 reservation、不能生成第二条 Action；非空时才进入 `unknown + coverage_action_overdue + remote_reconcile`，不得重发。历史被误标为 unknown 但无 Gateway 事实、且原 Action 已明确 terminal 的行，必须按真实终态释放 reservation 后重新规划。

日履约汇总读取 `next_decision_at` / `next_eligible_at` 时，必须先归一为北京时间墙上时钟再求最早值；PostgreSQL aware 时间与历史 naive 时间混存不得中断整个 Planner drain，也不得用捕获异常或跳过任务掩盖该失败。

`DispatchClaimScope`、`DispatchClaimWindow`、`DispatchClaimShardAllocation` 的 active count 是可由 `executing + dispatch_claim_active` Action 重算的投影。跨 Window stale Recovery 释放 Action 时必须按原 binding 重算 Scope/Window/Allocation；如果计数漂移，Action.result 记录 before/after 审计后继续释放，不能因某个旧 Window 的零值抛 underflow 并让 Recovery 回滚。binding 缺失仍应显式失败。该修订不放宽质量、账号、准入、风险或 `unknown_after_send` 安全门；AI 活群本地群冷却是否生效以 2026-07-28 完成优先合同为准，当前已删除，不得由本段恢复。

### 2.19 极搜会话状态偏离与图片验证码识别设计（RC-5a/5b/5c/8）

落实 PRD §2.18 极搜口径（line 563「热搜排行榜、验证页、未知页和正确分类页缺 selector 是不同错误」）的详细设计。P0 工单 `TKT-2026-07-25-SEARCH-JOIN-P0` S1/S2 线上实机证据（账号 99 + 165，2026-07-26/27 生产容器）确认原 RC-5「极搜群分类按钮缺失」需拆分为 4 个独立错误码，且图片算式验证码高频出现（推翻原 RC-8「无验证码」结论）。

#### 2.19.1 页面相位分类前置

`_execute_search_pages` 入口必须先对极搜响应页分类，不同相位走不同分支，禁止在任何相位直接报 `jisou_group_selector_missing`：

| 相位 | 判定条件 | 处置 |
| --- | --- | --- |
| `hot_list_page` | 文本含「热搜排行榜/近期热搜/热门搜索」 | 若 row=0/col=0 存在 callback_data `👥`，该页是关键词结果的分类入口，直接点击群分类；缺少该审批 selector 才写 `jisou_hot_list_page` 并排除 12 小时 |
| `verification_image_page` | `MessageMediaPhoto` + 文本含「人机验证/计算结果/captcha」 + ≥8 个 callback_data ASCII 数字或字母数字答案按钮 | 走 §2.19.2 验证码识别流程 |
| `search_category_page` | 含 `👥` callback_data 群分类按钮 | 走原有 `_select_jisou_group_results_page` 流程 |
| `group_result_page` | 正文含 Telegram `MessageEntityTextUrl` 群链接，首个 callback_data 为 `🔄`；翻页控件可能为「下一页」或 `➡️` | 按正文实体的规范化 username 精确匹配目标；仅使用审批的 callback_data 导航控件翻页 |
| `unknown` | 以上都不匹配 | 写 `jisou_session_state_deviated`，账号—协议路径排除 12 小时 |

普通热搜页重置（`/cancel` + `/start` + 重发关键词）已在线上验证**不可行**，禁止再尝试。2026-07-30 的 10 账号完整流程证据进一步确认：验证码批准答案返回的 `hot_list_page` 已经保留原关键词结果，并提供 row=0/col=0 的 callback_data `👥` 分类控件；再次发送关键词只会重新触发验证码并形成循环。无论本次是否出现验证码，都必须直接点击该 `👥` 控件进入群结果，`jisou_post_verification_keyword_replayed` 固定为 `false`。这里只批准 callback_data 群分类，不放宽未知 callback 或 telegram_url 外链禁令。

#### 2.19.2 搜索验证码当前合同：双 OCR 顺序识别、禁止 AI/VLM

搜索 `verification_image_page` 只允许下列状态机：冻结 fingerprint/image/message/callback 候选 → RapidOCR；A 无输出、非法、无候选命中或运行失败时进入同 fingerprint 的 ddddOCR；A 合法则对同 fingerprint 最多提交一次，权威 `answer_rejected` 且页面仍为同 fingerprint 后才允许 B。若 A/B callback 拒绝已经产生新 fingerprint，则由远端换题事实直接从 A 重开；B 无安全答案或拒绝后仍为旧 fingerprint 时，只有版本化 `BotProtocolSample.refresh_mode=approved_refresh_callback` 且按钮精确命中才允许 refresh，没有独立 refresh 动作则 `refresh_not_supported` 并结束 Attempt。challenge policy 只限制预算/deadline，不能创造 refresh；`/cancel`、`/start`、重发关键词、未知按钮和本地刷新均禁止。callback/refresh unknown、旧 fingerprint 和 deadline/budget 耗尽均不得再点。搜索链路不得加载、调用或等待任何多模态模型，OCR/refresh 不计 click 成功，最终仍只认 `target_click_observed`。

#### 2.19.2-H 历史验证码投票方案（`historical_do_not_implement`）

以下 2026-07-30/31 的多模态模型、两票共识、hedge 和关键词换题内容仅保留生产取证历史，已被上节与 `task-fulfillment-contract-closure-prd.md` §9 supersede；不得进入当前代码、policy、数据流、测试或发布镜像。

检测到 `verification_image_page` 时执行：

1. **记录过程状态并下载图片**：以 `bot_peer + message_id + image_hash + ordered_callback_fingerprint` 生成不可变 `challenge_fingerprint_hash`，写 `jisou_image_verification_required`，但不终结当前 Action、不触发账号排除；`client.download_media(message, file=bytes)` 获取验证码图片字节。当前 Action 只持有该 `bot_peer + challenge_fingerprint` 的远程副作用 ownership，禁止同一协议会话/指纹被其他 Action 并发改写；同账号其他非冲突 RPC 可并行。这不消费新的 click 配额、任务目标或 Dispatcher/Gateway 份额。
2. **本地优先有界识别**：同一 immutable challenge 先在 RapidOCR、ddddOCR 两个进程级固定槽执行；两路在 hedge 点前形成相同安全候选时立即决策，模型调用次数必须为 0。只有两路本地分歧，或到 `model_hedge_at` 仍无本地共识时，才启动首个健康已审批多模态模型，且最多一个；模型稳定优先级以生产健康事实选择，当前为 `MiMo mimo-v2.5 → MiniMax-M3 → 其他健康已审批模型`。禁止等待或串行调用多个模型。Tesseract 仅属于离线诊断参考，不随 Dispatcher/OCR Worker 生产镜像安装且不参与生产投票；`pyocr`、`pytesseract` 与同一 OCR 引擎的多种预处理结果均不得重复计票。页面正文含「计算结果/数学题/算式」时，模型 prompt 固定使用“这是数学题，都是全数字，你来给出答案”；其他图片验证码使用“这是是一段数字+字符的字符串你来告诉我结果”。两类 prompt 只补最终答案和紧凑 JSON 输出约束，不把按钮候选发送给模型。
3. **三路独立一票与候选约束（硬约束）**：每个引擎的原图和有限预处理变体先在引擎内部聚合为一个 vote，再进入跨源共识，禁止一个引擎多票。数学题仅在服务端按已验证的单数字操作数与 `+|-|*|/` 语法解析最终值，并以 callback 候选集合约束；字符串题只做数字字母串规范化，不应用算式推断。每路答案都必须精确命中 callback_data 候选；任意两路给出相同安全候选才允许提交。自报置信度、单路多变体一致或单个候选命中均不能替代 2/3 共识。
4. **无共识换题**：本轮三路不能形成共识时禁止点击答案。只有当前页面仍被严格分类为 `verification_image_page`，Executor 才可在同一 Action、账号、session ownership 内重发本 Action 冻结的原关键词以获得新 challenge；不得发送 `/start`、`/cancel`、点击未知 callback，也不得在 `hot_list_page|unknown` 执行该动作。新 fingerprint 必须与旧值不同，否则明确收口为 required/failed 事实。整个 Action 的 challenge 总预算复用 `SchedulingSetting.default_max_retries`，包含无共识换题与错误答案后机器人自动换题，不另设隐藏上限。
5. **点击并以远端事实判定**：在按钮矩阵找 `button_type=callback_data` 且 `text=consensus_answer` 的按钮，对同一 fingerprint 只允许一次 CAS 提交。只有该提交关联的后续机器人回执明确通过，或进入含审批 `👥` selector 的 `hot_list_page`、`search_category_page|group_result_page`，才能写 `jisou_image_verification_solved` 并继续；新 fingerprint 表示本题未通过而非 solved，预算尚余时按新 challenge 重新识别。含审批 `👥` selector 时直接进入群分类，禁止在答案提交成功后重放关键词。
6. **失败、unknown 与审计**：预算耗尽、同 fingerprint 明确拒绝或换题 fingerprint 不变时写清具体失败原因；供应商/传输异常而仍无两路安全共识时保持 `jisou_image_verification_required`，不得冒充成功。实际 callback 前必须再次检查 monotonic submit deadline；callback RPC 已发出但页面回执不可判定时写 `verification_callback_result_unknown`，Action/ExecutionAttempt 进入 `unknown_after_send` 并持久化 challenge fingerprint，纯点击 obligation 保持 unknown 占位。后续 Action 在同账号、机器人和 fingerprint 仍有未闭环 unknown 时禁止再次 callback，只允许远端复读确认页面已变化或明确未执行。Action 保存每个 challenge 的 fingerprint、三个来源的单票结果、候选命中、`model_waited`、共识、换题触发与远端结果，不保存验证码图片。两路 OCR 即使同错也不能直接写 solved，机器人返回新 fingerprint 时必须记录 rejected 并继续；验证码前的 `search_join_execution_failed|search_transport_unavailable` 与识别失败分开统计。

线上历史样本曾出现 4/8 按钮匹配成功，但该比例只作识别质量观测，禁止进入任务容量、账号容量、预计确认量或完成计算。当前无需验证码，或本次已真实写入 `jisou_image_verification_solved`，才允许继续；`required|failed` 以及 required 下的 unavailable/unknown 原因都不能被概率折算成可确认 click。

2026-07-30 同图 A/B 补充证据：真实样本 `4-4=?` 的正确候选为 `0`，旧 prompt 因“答案必须是正整数”返回空答案，而不携带候选的数学题 prompt 返回 `0`（confidence `0.95`）；另一个真实样本出现 `0*6=?`，证明只枚举 `+|-` 仍会遗漏题型。用户短语义 prompt 在 `8-4=?` 上连续两次返回 `4`（confidence `0.95`），但在 `6-2=?` 上也曾连续两次错答 `3`，因此自报置信度和重复一致均不能替代候选精确匹配。生产受控 canary 使用同一短语义 prompt 并行识别两次后，对 message `11363` 提交候选 `7`、对 message `11384` 提交候选 `6`，均取得新的 `search_category_page` 机器人回执（message `11365`、`11386`）；这是两次真实通过，不代表所有题型自动可靠。通用 Tesseract 5.5.1、`pytesseract`、`pyocr`、RapidOCR 和 EasyOCR 在变形字体样本上均未稳定解析完整算式，因此本轮不把任一通用 OCR 包装器作为主识别链或成功兜底。

2026-07-30 追加 10 个不同生产账号 canary（账号 99、167、168、160、229、236、169、244、179、161）：10/10 均出现数学图片验证码，未观察到字符串题型；MiMo v2.5 对每张图并行调用 2 次。账号 99、244 的两次答案分别一致为 `13`、`37`，但不在各自按钮候选中，服务端未点击；其余 8 个账号的双次答案一致且命中候选，每个 fingerprint 只提交一次。账号 167、168 提交后只观察到 `hot_list_page`，没有通过证据；账号 160、229、236、169、179、161 提交后先到 `hot_list_page`，重放一次关键词又得到新的 `verification_image_page`，仍不构成通过。该批最终为 `0/10` 远端确认通过，说明“双次一致 + 高置信 + 候选命中”仍不能证明识图正确，唯一成功判据继续是远端回执或已审批分类/结果页。

2026-07-30 随后按完整点击目标另取 10 个不同生产账号（99、183、220、167、254、168、169、152、248、231）执行临时脚本 canary：账号 99、169 覆盖验证码后 `hot_list_page -> 👥` 分支，其余 8 个账号覆盖无验证码 `hot_list_page -> 👥` 分支；10/10 均进入正文 `MessageEntityTextUrl` 群结果，使用 callback_data 下一页控件翻至第 4 个结果页，精确命中 `https://t.me/zzxshxc`，并由该账号执行 `channels.GetFullChannelRequest` 得到实体 `id=3298633687 / title=河南郑州学生会 / username=zzxshxc`。10/10 均未调用 join/request-to-join 等成员关系变更 RPC。该 canary 证明执行逻辑，不直接写生产 Task/Action/obligation 账本；正式 click 仍须由同一 ExecutionAttempt 保存实体指纹、目标详情 RPC 和无成员副作用证据后结算。

图片算式验证码的 RapidOCR/ddddOCR 识别、远端自动换题事实及已审批 refresh callback 不计入账号/关键词 click 限额、任务 click 目标、source 小时/日限额或额外 Dispatcher/Gateway click 份额；搜索链 AI/VLM 调用数固定为 0。它们只复用当前 source Action 已占用的 search-lane 在途位置。验证通过本身也不完成 click，最终仍必须取得 `target_click_observed`。

#### 2.19.3 错误码细分与 12 小时排除规则

| 错误码 | 触发条件 | 12 小时排除 | 说明 |
| --- | --- | --- | --- |
| `jisou_hot_list_page` | hot_list_page 缺少 row=0/col=0 的审批 callback_data `👥` selector | 是 | 当前尝试直接失败；账号—协议路径临时不可用，重置和关键词重放均不可行 |
| `jisou_session_state_deviated` | unknown 相位 | 是 | 账号级会话状态偏离，重置不可行 |
| `jisou_image_verification_required` | 检测到 verification_image_page | 否 | 当前 Action 正在识别的过程状态 |
| `jisou_image_verification_solved` | 同一 fingerprint 的单次批准答案提交获得明确远端通过回执，或进入已审批搜索分类/结果页 | 否 | 当前 source 可继续；不是 click 成功；仅离开原页面不算 |
| `jisou_image_verification_failed` | challenge 预算耗尽、同一 fingerprint 明确拒绝，或换题后 fingerprint 未变化 | 是 | 单路供应商/传输暂不可用、新 fingerprint 或一次无共识均不算最终失败 |
| `jisou_group_selector_missing` | search_category_page 缺已审批 selector | 否 | 单独评估是否协议样本过期，不自动排除 |

`jisou_selector_accounts` 排除逻辑（`backend/app/services/task_center/jisou_selector_accounts.py`）对 `hot_list_page` 直接写 `jisou_hot_list_page` 失败，并从失败事实起写账号—协议路径 12 小时 eligibility 排除；该事实按当前单用户 scope 的同一 `jisou_flow_contract_version` 在全部搜索任务间共享，避免同一账号立即被另一任务复用同一极搜协议路径。执行合同升级后，旧 Action 缺少或持有其他合同版本的失败仍保留为历史事实，但不得继续阻断新合同的首次尝试；新合同产生的真实失败仍按 12 小时排除。明确 `jisou_image_verification_failed` 使用相同 12 小时有效期。`required|solved|group_selector_missing` 不排除。hot-list 不 reset、不点击未知按钮，其他账号—协议路径继续完成同一 click 欠额。纯搜索点击 Action 一旦进入 Gateway 并取得明确失败回执，该 Action、Attempt 和 assignment 即按原结果终结；通用失败自动重试不得把同一 Action 改回 `pending`、覆盖 `jisou_hot_list_page` 或复用原 assignment。缺口只把原 obligation 回流 `open`，下一 Window 重新求解未排除路径并创建全新的 assignment/Action。

#### 2.19.4 观测盲点修复（RC-5c）

`_button_layout` 必须把已计算的 `normalized_text` 写入 trace（当前代码已计算但未持久化）。`jisou_group_selector_missing` / `jisou_session_state_deviated` / `jisou_image_verification_required|solved|failed` 发生时，trace 必须含每个按钮的 `normalized_text`、`button_type`、`effect`、`url`、`position`、`row`、`col`，便于回放真实按钮文案。仍禁止持久化机器人正文、按钮原文、目标群名（脱敏后 normalized_text 可存）。

`search_join_protocol_traces` 表当前 `COUNT(*)=0`（线上确认从未写入），dev 必须保证失败 trace 实际落表，否则观测盲点无法闭合。

#### 2.19.5 频率控制与搜索 AI/VLM 禁用边界

- **频率控制**：账号级搜索安全额度由系统 `AccountUsagePolicy` 与 Telegram 实时限制聚合，不由任务表单配置；存量 `pacing_config.per_account_hourly_action_limit` 只作容量充足时的软分散提示。小时计数只统计真实消费 search click 额度的 source，不让旧窗口 pending 永久占用；RapidOCR、ddddOCR 和 refresh 不进入 click 计数。`jisou_hot_list_page|jisou_session_state_deviated|jisou_image_verification_failed` 触发 12 小时账号—协议路径排除。
- **AI/VLM 禁用边界**：搜索验证码不得调用 minimax、mimo 或任何多模态模型，不得等待模型、用模型兜底或把模型结果写入现行投票。AI 只可用于其他已明确批准的业务类型；搜索 pacing、账号选择、目标点击和 CAPTCHA 均不使用 AI。

#### 2.19.6 验收口径

- 单元测试：4 种相位分类、固定顺序 RapidOCR → ddddOCR、A 合法时 B 不运行、A 无效或拒绝后仍是同 fingerprint 才运行 B、拒绝自动换题从新 fingerprint 的 A 重开、同 fingerprint 仅已审批 callback 可 refresh、无动作写 refresh_not_supported、任何 AI/VLM 调用数为 0、late result/旧 fingerprint/超过提交 deadline 均不点击、答案必须命中 callback 候选、每个 `(fingerprint,normalized_answer)` 最多提交一次且 A/B 同答案不重复，以及只有明确通过回执才 solved。
- QA 定向：`jisou_group_selector_missing` 日失败率显著下降；`jisou_session_state_deviated` 与 `jisou_image_verification_*` 可区分；`search_join_protocol_traces` 失败 trace 实际落表且含 normalized_text。
- E4 / production_fixed：完整自然日内 `confirmed_click_count = daily_click_target_snapshot`，逐个 `SearchClickObligation.id` 均有唯一完整 click evidence，且 `held_count=unknown_count=terminal_shortfall=quantity_overflow_count=open_excess_count=0`。证据写入工单或 prod-diagnosis worklog；找到目标、验证码通过、assignment/Action 数达到目标或晚到 click 均不能替代该公式。

#### 2.19.7 不允许

- silent 把热搜页 / 验证码页当搜索结果处理。
- mock 验证码识别成功；点击 telegram_url 外链进入子页面。
- 单靠 confidence 阈值放行点击（必须按钮矩阵匹配）。
- 为冲量绕过 `jisou_hot_list_page|jisou_session_state_deviated|jisou_image_verification_failed` 的 12 小时安全排除。
- 未经验证宣称 `production_fixed`。

`design_status=product_design_complete`。当前已同步专项 PRD 与数据流索引；项目结构索引只在代码修改完成后按真实入口更新。

#### 2.19.8 Dispatcher/OCR 内存隔离与优雅回收（2026-08-03 双 OCR supersede）

硅谷生产两次整机 OOM 的 victim 已由 kernel、容器和 `execution_attempts.worker_id` 精确映射到两个 Dispatcher；极搜图片验证是 OOM 前的主要负载，但最终 native allocator 的精确归因仍为 `unproven`。本节从当前 verification contract version 起只保留双 OCR 的进程隔离、deadline、回收和观测合同；任何模型投票、model hedge、模型 active registry 或模型密钥描述均属历史，不得实现。完整资源隔离设计以 `docs/03-feature-designs/dispatcher-ocr-memory-isolation-and-graceful-recycle-prd.md` 为准，但与本 PRD 冲突时以“搜索 AI/VLM 调用数为 0”为最高优先级。

> **合同状态（2026-08-03）：** 2026-07-31 的模型投票实现证据只属于旧 contract，不能证明当前双 OCR 合同已实现。当前状态固定为 `product_design_complete / implementation_unproven / production_unproven`；未完成双 OCR 定向 QA、production-like drain/内存 soak、GitHub Actions 发布和真实 E4 前，不得据本节声明线上已根治。

- 图片验证采用 deadline-aware 顺序状态机：从 `challenge_observed_at` 用真实 callback canary 参数计算 `callback_submit_deadline`；RapidOCR、ddddOCR 各使用一个进程级固定槽，不为每个 Action 创建 executor，也不建设持久 OCR 队列。先运行 RapidOCR；只有 A 无输出、非法、未命中候选、运行失败，或被权威拒绝且页面仍为同 fingerprint 时才运行 ddddOCR；B 无安全答案时不提交答案；B 无安全答案或拒绝后只接受远端自动换题，或在同 fingerprint 上使用协议样本审批的 refresh callback；没有该动作即结束 Attempt。OCR 槽等待、推理、callback 和 refresh 共用同一 remaining budget，每次 timeout 不得重置预算。提交前必须复读同 fingerprint、时间未过 deadline；旧 fingerprint、late result、页面变化或过期均禁止点击。搜索 AI/VLM 调用数固定为 0，候选精确匹配、每个 `(fingerprint,normalized_answer)` 单次 CAS，A/B 同答案不重复、远端明确通过才 solved 均不变。
- 当前线上只读影子证明同一验证码 `>=70.42s` 仍可见、数分钟后已消失；这不是 callback 接受证明。Release Gate 必须用受控账号按等待档位真实提交正确答案，分别记录 page visible、callback accepted、回执完成并固化校准版本；不得把 Telethon `conversation(timeout=60)`、页面可见时间或模型正常 p50 当提交窗口。
- 禁止每个 challenge 创建临时多线程 executor；RapidOCR、ddddOCR 和 Telegram 外部调用不得持有数据库事务。现行搜索进程不得加载模型 SDK、模型权重或模型密钥，也不存在 model future/active registry。
- Dispatcher 在阈值或 SIGTERM 后进入 `recycle_requested -> draining -> safe_to_exit -> restarted`：停止新 claim，在当前 OCR/Telegram futures 全部返回、下一轮 claim 前检查；owned Action、open Gateway、OCR future、reservation 和 Telethon client 未归零时保持 `drain_blocked`。运行时自动回收用仅服务于回收的最小 lease 保证单 shard；它不参与 Action/session 所有权。逐 Action 重启、外部 cron 和超时硬杀记成功均不允许。
- 根治方案将 RapidOCR/ddddOCR 移到 Docker 私网单实例、有 memory limit 的 `image-verification-worker`。P1 使用 deterministic request ID、同步 POST 与最小状态 GET；worker 只保存 TTL 内 `running|completed|failed|expired|unknown`，不建设持久队列/HA，不持有 Telegram session、业务数据库写权限、callback 或 AI 密钥。服务 unavailable/busy/unknown 时显式保持 required，禁止静默回落到 Dispatcher native OCR。
- P0 复用现有 search_join audit、Action.result 与 ExecutionAttempt 保存 fingerprint、message identity hash、deadline、A/B 逐次结果与耗时、refresh、callback 与远端结果，不新增验证码 Attempt 表或 session fence。P1 HTTP timeout 后先查询同 request；generation 变化时必须重读同 message/fingerprint 且仍有预算才可重发。图片、机器人正文和按钮原文不落库；callback unknown 只复探远端，绝不再次点击。
- RSS/cgroup/请求量/uptime 回收阈值与容器 hard limit 必须由主机预算、包含数据库/Redis 的其他容器 p95、其额外增长余量、drain headroom 和副本数计算后在 Release Gate 固化，禁止重复扣减或硬编码 magic number。swap 与重启仅是保护/止血，不是根治完成证据。
- L3 验收必须同时证明无 host/container OOM、至少 3 次单 shard 完整优雅回收、真实图片验证负载不低于事故样本 1287 次、AI/VLM 调用数与模型密钥读取数均为 0、无重复 callback/claim 泄漏、P1 Dispatcher 不再加载 native OCR，以及搜索日 ledger 仍满足 §2.19.6。容器/API healthy 不能替代这些 E4 事实。

### 2.20 搜索入群日目标达成保障设计（RC-3/4/6）

> **2026-07-28 纯点击 supersede：** 本节仅保留历史混合 click+membership 生产证据，不再是当前实现合同。纯搜索点击没有 membership/admission 目标或 child；“搜索点击加入”必须另立专项 PRD，本轮不得用旧 `join_target_group_after_click` 恢复本节设计。
>
> `contract_status=historical_do_not_implement`。本节后续所有“必须”“设计决策”“迁移”“回滚”“验收”均是旧版本记录；当前开发、测试和验收只能读取 §2.18、§2.19 的验证码实际状态合同以及 `search-click-daily-fulfillment-remediation-prd.md`。

**historical_do_not_implement（旧共享 Dispatcher 事故设计）：** 落实 PRD §2.18「共用 Dispatcher」口径（line 562「不能让固定排序长期饿死另一方」）和「搜索点击」口径（line 561 五重校验）的详细设计。P0 工单 S2 线上观测（2026-07-27）确认：任务 `85261b6b…` 截至观测时 `click confirmed=0/1000, mem confirmed=0/80, daily_outcome=blocked, blocker_code=daily_target_capacity_insufficient`，根因为 RC-3 membership UAS、RC-4 claim 饿死、RC-6 账号产能极低三个独立问题。§2.19 修复极搜侧，§2.20 修复任务履约侧，缺一不可。

#### 2.20.1 RC-4 Dispatcher claim 公平性保障

**问题**：`dispatch_claim_reservations` 6h 聚合显示 `hard_hourly required=80795 claimed=6034`，`search_join required=1916 reserved=311 claimed=0`。search_join 的 311 个 reservation 一个都没 claim 成功，capacity 全被 hard_hourly 占满，`reason=shared_dispatch_capacity_insufficient`。这违反 §2.18 line 562「不能让固定排序长期饿死另一方」的已有口径。

**设计决策**：

1. **实时债务份额**：严格搜索与 AI 群日到期 send 共存时，Dispatcher 在 `DispatchClaimScope` allocation 阶段按剩余目标、剩余时间和可领取 Action 计算 `required_claims`；不得继续使用固定 30% 产品份额，也不得让任一类别独占全部可用容量。
2. **公平性算法**：allocation 按 `urgency_score` 比例和持久化轮转处理同分，结果写入 `DispatchClaimShardAllocation` / Reservation；任务详情显示每类 required/reserved/claimed 和未服务原因。共享容量只保护运行时，不改变 AI 或搜索业务目标。
3. **饿死告警**：search_join 连续 3 个 `DispatchClaimWindow`（默认 15 分钟/窗口）reserved > 0 但 claimed = 0 时，写 `dispatch_claim_starvation` 告警，运营中心可见。
4. **禁止**：用 `exclude_task_ids=AI` 临时放行作为常态；silent 把 search_join reservation 记为 claimed；取消 strict 日目标的严格性；静态优先级单独作为完成保证。

#### 2.20.2 RC-3 membership 子动作 UAS 修复

**问题**：`search_join_membership` action 仅 2 条，全部 `unknown_after_send`（`lease_expired` 后 recovery 标记）。click 成功后 membership 子动作执行超时（lease_expired），mem confirmed 长期 0。

**设计决策**：

1. **lease_timeout 调整**：membership 子动作的 lease_timeout 必须覆盖真实 TG 入群确认耗时。当前 lease 过短导致 UAS。新默认值 180s（产品可配置），覆盖 join 请求发送 → TG 服务端处理 → membership 事件回推的全链路。
2. **链路完整性**：click 成功后 membership 子动作必须按「创建 → claim → 执行 → 确认」完整链路执行，每一步有独立超时和错误码。click 的 `target_click_observed=true` 不等于 membership 已确认；membership 必须有独立的 `membership_observed` 事实。
3. **UAS 补偿确认**：`unknown_after_send` 的 membership action 不能长期挂起。recovery 必须在 lease_expired 后执行补偿确认（重新查询 membership 状态），确认成功写 `membership_observed`，确认失败写 `membership_not_observed`，超时（如 10 分钟）未确认写 `membership_confirmation_timeout` 终态关闭。
4. **禁止**：silent 把 UAS 记为 confirmed；mock 入群确认；membership 子动作无超时边界；UAS 长期挂起不终态关闭。

#### 2.20.3 RC-6 账号产能保障

**问题**：pool 3 有 67 个在线账号，但 48h 内 planner 只用了 7 个（acct=90/96/99/102/151/165/221），acct=96 独占 26 个 pending（全未 claim）。`per_account_daily_action_limit=2` + 账号选择范围窄 → 理论日上限仅 14 action（7 账号 × 2），远不够 1000 目标。`pacing_limits` 显示 `per_account_daily_limit_reached=0`（不是冷却问题，是 planner 没用全账号）。

**设计决策**：

1. **planner 账号选择全覆盖**：planner 规划 search_join action 时，账号候选集必须覆盖任务 `account_config` 指定范围内全部 `status=在线` + `health_score >= 55` + 未被 `jisou_selector_accounts` 12 小时排除的账号。禁止只用 7/67。账号选择范围窄（实际候选 < 配置候选 50%）时写 `planner_account_selection_narrow` 告警。
2. **per_account_daily_action_limit 边界**：系统不得为了匹配 1000 日目标自动把账号日安全上限从 2 提升到 23，也不在任务创建页开放绕过字段。该上限由系统安全策略维护；目标始终允许创建，运行期只按当前安全容量持续规划并显式显示缺口。
3. **历史产能预判（已失效）**：旧版曾用“有效账号数 × 日限额 × 验证码概率折损”估算容量；当前合同明确禁止使用 `captcha_trigger_rate` 或 AI 历史成功率。只按当前真实资格状态计算 attempt capacity，验证码只有实际 `solved` 才继续。
4. **禁止**：silent 用 7 个账号冲 1000 目标；为冲量取消 per_account_daily_action_limit 且无产品确认；planner 账号选择范围窄但不告警。

#### 2.20.4 RC-1 静默窗口（已由完成优先合同修订）

线上 21 个 skipped 中大量 `error_code=quiet_hours_active`（03:00-07:38 Asia/Shanghai）证明旧静默与 catch-up 都会制造日欠额。当前五类履约任务删除静默权重、活动曲线、jitter、行为 skip 与 catch-up 计算；Planner/Executor 在真实阶段资源空闲时立即领取并执行，且不得新增 `quiet_hours_active` 终态。安全额度、协议、代理、授权、Gateway 防重和 deadline 不变。

#### 2.20.5 前端任务详情显示

任务详情页必须新增以下字段（只读展示，不在创建/编辑表单可调）：

| 区域 | 新增字段 | 数据源 |
| --- | --- | --- |
| dispatch_claim | `min_reserved_capacity`、公平性 allocation（每类 required/reserved/claimed）、`dispatch_claim_starvation` 告警 | `DispatchClaimShardAllocation` |
| daily_outcome | 历史 `daily_target_capacity_insufficient`、有效账号数和验证码触发率预估 | 历史 planner 投影；当前禁止概率折损 |
| membership | UAS membership action 计数、`membership_confirmation_timeout` 计数 | actions 聚合 |
| 账号选择 | 实际候选账号数 / 配置候选账号数、`planner_account_selection_narrow` 告警 | planner 决策 |

#### 2.20.6 边界场景

- **历史提案：minimax provider 全部不可用**：旧方案曾要求直接写 `jisou_image_verification_failed` 并排除 24h；该处置已失效。当前按 §2.19.2 保持 `jisou_image_verification_required + verification_ai_unavailable`，不降级绕过验证码，也不以供应商暂不可用冒充远端失败。
- **验证码识别成功后再次触发验证码**：按 §2.19.2 第 6 步依据 fingerprint 处理；同一 fingerprint 的再次出现为明确失败，新 fingerprint 重新识别，不使用固定递归次数。
- **dispatcher scope 只有一个严格任务**：min_reserved_capacity 不生效（无竞争方），正常分配。
- **planner 账号全部被 12 小时排除**：写运行期账号—协议路径 eligibility blocker，当前路径暂停规划，不 silent 用空账号池冲目标，也不减少 click 欠额。
- **membership 子动作 click 关联失败**：`source_search_join_action_id` 指向的 click action 不存在或未 confirmed，membership 子动作写 `membership_source_click_invalid` 终态关闭，不执行。

#### 2.20.7 迁移与回滚

- **迁移**：`per_account_daily_action_limit` 默认值变更（2 → N）通过 `pacing_config` JSON 配置，无需数据库迁移；`min_reserved_capacity` 为新增 `DispatchClaimScope` 配置字段，需迁移加列（默认 0.30）；`membership_lease_timeout` 为新增配置，需迁移加列（默认 180s）。dev 必须在 Release Gate 前提供迁移脚本和回滚脚本。
- **历史回滚记录（禁止执行）**：旧版曾允许关闭识别或退回固定排序；当前不得通过关闭识别、概率折损、固定类别排序或接受任务饿死来回滚完成优先合同。
- **发布顺序**：§2.19 和 §2.20 可独立发布，但 E4 验收需两者都在线。建议先发布 §2.20（解除 claim 饿死 + 账号产能 + membership UAS），再发布 §2.19（验证码识别），避免验证码识别触发后账号被排除但产能未补齐的过渡期。

#### 2.20.8 验收口径

- RC-4：search_join 连续 3 个 window reserved > 0 且 claimed = 0 不再出现；任务详情显示 min_reserved_capacity 和公平性 allocation。
- RC-3：membership action `unknown_after_send` 比例显著下降；mem confirmed 随成功 click 增长，不再长期 0；UAS membership action 在 10 分钟内终态关闭。
- RC-6：planner 实际用账号数 ≥ 配置候选 80%；`daily_target_capacity_insufficient` blocker 在产能不足时可见；日目标达成或明确 blocker。
- E4 / production_fixed：完整自然日 click confirmed ≥ 1000 且 mem confirmed ≥ 80（需 §2.19 + §2.20 一起修复）。

#### 2.20.9 不允许

- silent 把 UAS / pending 记为 confirmed success。
- mock 极搜点击 / 入群确认 / minimax 识别。
- 为冲量取消全部静默 / 曲线且无产品书面确认。
- 用 `exclude_task_ids=AI` 临时放行作为常态。
- 未经验证宣称 `production_fixed`。

historical_design_status=complete，`contract_status=historical_do_not_implement`。当前 dev 交接只以 `docs/03-feature-designs/task-fulfillment-classified-recovery-prd.md`、`docs/03-feature-designs/task-fulfillment-contract-closure-prd.md` 及其中明确列出的当前专项为准；`all-task-fulfillment-recovery-prd.md`、旧 capacity/shared-dispatch 方案和旧固定优先级快修只保留历史审计，不得作为实现依据。

> 线上观测证据详见 P0 工单 `docs/04-ops/tickets/2026-07-25-p0-search-join-daily-target-failure.md` S1/S2 章节。诊断脚本 `backend/scripts/diagnose_jisou_selector.py`（含 `--solve-captcha` 只读测试模式）保留用于回归验证。

### 2.21 PRD §2.19 + §2.20 线上可行性与代码现状评估（2026-07-27）

> **历史评估（`contract_status=historical_do_not_implement`）：** 本节只保存 2026-07-27 的线上样本与旧双目标方案评估。其中的“44% 成功率折算产能”、`per_account_daily_action_limit` 提升、membership 目标、固定发布先后、重试成功率推测和旧 RC-3/4/6 实施建议均已失效，不得进入当前容量、调度、迁移或验收。当前只认验证码实际 `required -> solved|failed`；识别 AI/批准重试不占 click 限额或额外份额，实际 `solved` 才继续；纯搜索点击与 unit release/分片权重重建以 §2.18、§2.19 和专项 PRD 为准。

按 PRD §2.19 + §2.20 到线上环境测试方案可行性。由于 SSH 服务端在并行长连接后暂时拒绝连接（sshd 资源耗尽），部分多账号测试待恢复后补跑。基于已有线上证据（账号 99/165）和代码层面分析，评估结论如下。

#### 2.21.1 §2.19 极搜侧可行性

| 子节 | 设计要点 | 代码现状 | 可行性 | dev 待办 |
| --- | --- | --- | --- | --- |
| §2.19.1 相位分类 | 5 相位表格 + 不同分支 | `classify_jisou_page`（`search_join_protocol.py:55`）已有 `hot_list_page` / `verification_page` / `unknown_page` 分类，但 `verification_page` 走 `bot_human_verification_required` 错误码，不区分图片验证码 | ✅ 可行 | 扩展 `verification_page` 为 `verification_image_page`，新增图片验证码检测（`MessageMediaPhoto` + ≥8 数字按钮） |
| §2.19.2 验证码识别 | 历史旧案曾写“6 步流程 + 双重校验 + 递归上限”；递归上限已失效 | 完全不存在 | 历史样本仅证明协议路径可观察，不能折算当前完成量 | 当前合同见 §2.19.2：识别 AI 不设业务固定轮数或递归次数；同 fingerprint 只允许一次经批准的 Telegram 提交，并且只有远端明确通过才继续 |
| §2.19.3 验证码状态 | `required -> solved|failed` + 精确 12 小时排除 | 已有 `jisou_group_selector_missing` / `jisou_hot_list_page` / `jisou_protocol_page_unknown` / `jisou_session_state_deviated`；**缺** `jisou_image_verification_required` / `solved` / `failed` | ✅ 可行 | 新增 3 个状态；`hot_list_page|session_state_deviated|image_verification_failed` 排除，`required|solved|group_selector_missing` 不排除 |
| §2.19.4 观测盲点 | normalized_text 落表 + protocol_traces 实际写入 | `_button_layout`（`search_join.py:354`）已计算 `normalized` 但未写入返回 dict；`record_search_join_protocol_trace`（`dispatcher.py:5133`）有调用但表空 | ✅ 可行 | `_button_layout` 加 `normalized_text` 字段；protocol_traces 表空是 §2.20 RC-4 claim 饿死的连锁反应（action 卡在 pending 没执行到 gateway），§2.20 修复后 trace 会自然落表 |
| §2.19.5 频率控制 | 账号级搜索频率冷却 | 不存在 | ✅ 可行 | 新增账号级频率冷却配置 |

**关键代码差异（dev 必须注意）**：

1. **12 小时排除逻辑按实际结果修复**：`jisou_selector_accounts.py` 不排除 `jisou_group_selector_missing|jisou_image_verification_required|jisou_image_verification_solved`；`jisou_hot_list_page|jisou_session_state_deviated|jisou_image_verification_failed` 排除账号—协议路径。
2. **验证码检测走两条路径**：非 jisou 用 `_human_verification_required`（`search_join.py:701`，substring 匹配），jisou 用 `classify_jisou_page`（`search_join_protocol.py:55`，protocol_profile 指纹匹配）。PRD §2.19.2 要求 jisou 检测到 `verification_image_page` 时走 minimax 识别，需 dev 在 jisou 路径新增图片验证码识别分支，不能复用非 jisou 的 `_human_verification_required`。
3. **protocol_traces 表空是连锁反应**：代码有写入逻辑（`dispatcher.py:5133` 调用 `record_search_join_protocol_trace`），但 action 被 §2.20 RC-4 claim 饿死卡在 pending，根本没执行到 `_record_search_join_protocol_result`（`dispatcher.py:5108`）。§2.20 修复后 trace 会自然落表，不需要单独修复 trace 写入路径。

#### 2.21.2 §2.20 任务履约侧可行性

| 子节 | 设计要点 | 线上现状（2026-07-27 最新证据） | 可行性 | dev 待办 |
| --- | --- | --- | --- | --- |
| §2.20.1 RC-4 claim 公平性 | min_reserved_capacity 30% + 加权公平队列 | `search_join reserved=44 claimed=0`（1h 内），`hard_hourly required=110883 claimed=546` | ✅ 可行 | 新增 `min_reserved_capacity` 字段 + 加权公平队列算法 |
| §2.20.2 RC-3 membership UAS | lease_timeout 180s + 补偿确认 | 2 条 membership action 全 `unknown_after_send`，未修复 | ✅ 可行 | 调整 lease_timeout + 新增补偿确认 + 10 分钟终态关闭 |
| §2.20.3 RC-6 账号产能 | planner 全覆盖 + per_account_daily_action_limit 提升 | 7/67 账号（10.4%），acct=96 独占 26 pending | ✅ 可行 | 修复 planner 账号选择逻辑 + 产品决策 per_account_daily_action_limit |

#### 2.21.3 §2.19 + §2.20 依赖关系

- **历史发布顺序提案已失效**：当前以同一版本整体切换纯 click、12 小时路径排除与新履约合同，不允许拆版本恢复旧混合路径。
- **§2.19.4 protocol_traces 落表依赖 §2.20.1**：action 被 claim 饿死卡在 pending 时根本不会执行到 gateway 调用阶段，trace 自然空。§2.20.1 修复后 trace 会自然落表。
- **§2.19.5 频率控制与 §2.20.3 账号产能关联**：频率冷却（单账号 1h N 次搜索）会降低单账号产能，需与 per_account_daily_action_limit 一起评估。

#### 2.21.4 线上测试结果（2026-07-27 生产容器，SSH 恢复后补跑）

**测试 1：多账号相位分类（§2.21.4-1）**

3 个账号各跑 5-8 轮搜索，验证 PRD §2.19.1 五相位分类覆盖率：

| 账号 | 轮数 | hot_list_page | verification_image_page | search_category_page | group_result_page | unknown |
| --- | --- | --- | --- | --- | --- | --- |
| 99 | 5 | 1 | 4 | 0 | 0 | 0 |
| 165 | 8 | 0 | 8 | 0 | 0 | 0 |
| 221 | 8 | 0 | 8 | 0 | 0 | 0 |

关键发现：
- **账号 99 行为变化（历史诊断，已由 §2.19 新证据修订）**：首次进入 `hot_list_page`（1 次），后续 4 次全部进入 `verification_image_page`。当时把 hot-list 当作偏离页并重发关键词，因而持续触发验证码；当前合同改为仅在缺少审批 callback_data `👥` 时写 `jisou_hot_list_page`，存在 selector 时直接进入群分类，禁止关键词重放。
- **验证码高频确认**：3 个账号 21 轮搜索中，20 轮触发验证码页（95.2%），仅账号 99 首轮是 hot_list_page。**极搜已对所有测试账号强制验证码**，PRD §2.19.2 验证码识别流程是必须的，不是可选的。
- **`verification_image_page` 判定条件验证**：所有验证码页都是 `has_photo=True` + `button_count=10` + `digit_btns=10`（10 个 callback_data 数字按钮），完全符合 PRD §2.19.1 判定条件（`MessageMediaPhoto` + 文本含「人机验证/计算结果」+ ≥8 个 callback_data 数字按钮）。
- **`search_category_page` 和 `group_result_page` 未出现**：3 个账号 21 轮全部没进入正常搜索分类页或群结果页。说明极搜当前对所有账号强制验证码，PRD §2.19.1 的 `search_category_page` / `group_result_page` 分支在当前环境无法实测，但代码路径已存在（`classify_jisou_page` 已有分类），dev 实现后回归测试即可。

**测试 2：验证码识别多账号回归（§2.21.4-2）**

账号 165 + 221 各跑 8 轮 `--solve-captcha`。这些轮次只验证历史样本中的双重校验可观测性，不验证、也不得导出当前合同中的固定重试次数或递归上限：

| 账号 | 轮数 | 按钮匹配成功 | 高置信但被矩阵拦截 | minimax 返回空 | 低置信被阈值过滤 |
| --- | --- | --- | --- | --- | --- |
| 165 | 8 | 3 (37.5%) | 2 (25%) | 1 (12.5%) | 2 (25%) |
| 221 | 8 | 4 (50%) | 1 (12.5%) | 1 (12.5%) | 2 (25%) |
| **合计** | **16** | **7 (43.8%)** | **3 (18.8%)** | **2 (12.5%)** | **4 (25%)** |

关键发现：
- **双重校验有效拦截高置信错答**：3 次高置信但 answer 不在按钮矩阵（165r7 conf=0.85 answer=40、221r1 conf=0.75 answer=26、221r5 conf=0.7 answer=7），全部被矩阵匹配拦截，验证 PRD §2.19.2 第 3 步「answer 必须在按钮矩阵」是必要安全门。若单靠 confidence ≥ 0.70 阈值，这 3 次会误点击错误按钮。
- **历史空返回观测**：2 次返回空（165r4、221r8），占 12.5%。该样本不证明固定重试次数，也不能预测重试后的成功率；当前每个 challenge 只调用首个健康已审批模型一次，供应商/传输暂不可用保持 `required`。
- **历史按钮匹配率约 44%**：7/16 候选答案通过按钮矩阵校验，与之前账号 165 单测的 4/8 (50%) 接近。该比例不是验证码通过率，更不能折算账号产能或预测确认量；只有同 fingerprint 的批准提交取得明确远端通过回执后才允许继续。
- **不存在递归上限验证**：所有轮次都是独立搜索，不是同一次 search action 内的递归。当前合同明确每个 immutable challenge 只调用首个健康已审批模型一次，新的 fingerprint 才能开启新的识别。

**测试 3：minimax provider 状态确认（§2.21.4-3）**

| provider_id | name | model | active | health |
| --- | --- | --- | --- | --- |
| 1 | xiaomi-mino | mimo-v2.5 | False | 禁用 |
| 4 | MiniMax MiniMax-M2.5 | MiniMax-M2.5 | True | 健康 |
| 5 | MiniMax MiniMax-M3 | MiniMax-M3 | True | 健康 |

- **minimax provider 可用**：id=4/5 两个 provider 健康，PRD §2.19.2 调用 `ai_gateway.solve_image_verification` 可行。
- **历史降级观测**：测试 2 中 2 次 minimax 返回空（`AiEmptyFinalContentError`），只证明当时代码直接报错；旧“重试 1–2 次后 failed”结论已失效。当前合同是只调用首个健康已审批模型一次，不等待其他模型；供应商/传输暂不可用则保持 required。
- **所选 provider 不可用场景**：历史未实测。当前验收预期固定为 `required + verification_ai_unavailable`、12 小时排除增量为 0；所选模型真实响应但无安全答案，或同 fingerprint 的单次提交被远端明确拒绝，才是最终 `failed`。

**测试 4：§2.20 线上状态复查（RC-4/3/6）+ §2.19.4 protocol_traces**

| 根因 | 最新证据（2026-07-27 01:40） | PRD 设计验证 |
| --- | --- | --- |
| RC-4 claim 饿死 | `search_join reserved=41 claimed=0`（1h 内），`hard_hourly required=99806 claimed=478` | §2.20.1 min_reserved_capacity 30% 设计必要且可行 |
| RC-3 membership UAS | 2 条 membership action 全 `unknown_after_send`，未修复 | §2.20.2 lease_timeout 180s + 补偿确认设计必要且可行 |
| RC-6 账号产能 | 7/67 账号（10.4%），48h 内 57 actions | §2.20.3 planner 全覆盖 + per_account_daily_action_limit 提升设计必要且可行 |
| RC-5c protocol_traces | `COUNT(*)=0`（仍空） | §2.19.4 观测盲点修复必要；表空是 RC-4 claim 饿死连锁反应，§2.20.1 修复后自然落表 |

#### 2.21.5 综合结论

PRD §2.19 + §2.20 方案**整体可行**，线上测试已验证核心设计要点：

1. **§2.19.1 五相位分类可行**：`verification_image_page` 判定条件（`has_photo=True` + 10 个 callback_data 数字按钮）100% 命中所有验证码页。`hot_list_page` 和 `verification_image_page` 是当前线上主要相位（`search_category_page` / `group_result_page` 因极搜强制验证码未出现，但代码路径已存在）。
2. **§2.19.2 验证码识别双重校验可行且必要**：按钮矩阵匹配拦截了 3 次高置信错答（18.8%），证明单靠 confidence 阈值不足。成功率约 44%，需 §2.20.3 账号产能补齐。
3. **§2.19.2 单模型快速返回设计**：每个 challenge 只调用首个健康模型一次，不因等待第二次或其他模型拉长搜索点击链路。
4. **§2.20 三个根因设计全部必要且可行**：RC-4 仍饿死（search_join claimed=0）、RC-3 仍 UAS、RC-6 仍 10.4% 覆盖率，PRD 设计直接对应线上问题。
5. **§2.19.4 protocol_traces 表空是 §2.20 连锁反应**，不需单独修复。

关键风险点：

1. **12 小时排除逻辑**是高风险改动，必须覆盖 hot-list、unknown、验证码最终失败及到期恢复。
2. **纯 click 与排除合同必须同版发布**，禁止用旧 membership 路径或概率容量折损作为临时兼容。
3. **极搜已对所有测试账号强制验证码**（21 轮 20 轮验证码），PRD §2.19.2 验证码识别是必须的，不是可选的。
4. **minimax 识别成功率约 44%**，需 §2.20.3 账号产能补齐（per_account_daily_action_limit 提升 + planner 全覆盖）才能支撑 1000 日目标。

historical_design_status=complete，`contract_status=historical_do_not_implement`。本节不能作为当前 dev handoff 或 QA 清单。

### 2.22 2026-07-28 全任务按时按量履约恢复

2026-08-01 生产补正：AI 日履约的生成并发按“跨群并行、同群单 ready”执行，禁止同群预生成队列被自身真实发送反复判定为上下文过期；具体门禁、worker 拓扑和 E4 口径以专项 PRD §2.5 为准。

**historical_do_not_implement（旧到期债务签到）：** 旧`due_catch_up_provider_budget_exhausted/due_catch_up_check_in`与同群单ready/数量槽路径已退役；current不得跳过Provider预算后直接创建签到Action，只按AI专项的mask-missing coverage与coverage-complete extra-volume handoff两条分支。

**historical_do_not_implement（旧到期追赶流水线）：** `due_catch_up_pipeline_depth`及ready签到流水线已退役；current每Task轮转一次最多20条stable obligation/Generation work，Action仅按normal Generation ready或typed Planner签到分支产生。

生产复核确认，AI 活群、评论、点赞、浏览和搜索点击虽然使用不同执行器，但当前未达标由三类共同问题叠加造成：

1. Planner、Dispatcher、覆盖账本和 `Task.stats` 在热事务中交叉写入，发布后已经出现 PostgreSQL deadlock；容器存活不等于队列可持续流动。
2. 任务级预算、逐账号/逐消息目标和 deadline 混用，导致评论提前完成、点赞排到 6 小时窗口以外、浏览配置在数学上不可达。
3. Action、ExecutionAttempt 和远端事实没有形成统一完成合同，导致 AI 远端成功未正确审计、reaction unavailable 占用目标、搜索大量建单但真实 click/membership 仍为 0。

本节只保留历史背景。当前完整合同为 `docs/03-feature-designs/task-fulfillment-classified-recovery-prd.md` 与 `docs/03-feature-designs/task-fulfillment-contract-closure-prd.md`；旧 `all-task-fulfillment-recovery-prd.md` 为 `historical_do_not_implement`。以下冲突口径不得作为实现依据：

| 旧口径 | 当前口径 |
| --- | --- |
| `Task.status=completed` 或 task-level cap 达到即可代表业务完成 | Task 生命周期与履约状态分离；只有逐账号/逐消息/逐目标远端确认达到配置目标才为 `met` |
| 评论 `max_total_comments` 可单独触发动态任务 completed | `dynamic_new` 为 continuous；finite batch 必须所有已解析消息逐条达标，`unknown_after_send` 不计完成 |
| reaction unavailable 可关闭同帖或占用成功额度 | 只记录失败 attempt；其他合格账号/reaction 继续补欠额，只有远端 reaction success 计成功 |
| 浏览 task daily cap 可低于已知逐消息当日目标 | 已知范围保存时属于结构配置冲突并返回 422；动态新增导致不足时显示 blocker，不能隐藏未服务消息 |
| 搜索 repeat 模式可绕过账号日限额或关键词日限额 | repeat 只解除旧 membership pending 对新 source 的阻断；所有账号、关键词、小时和 Gateway 安全限额继续生效 |
| `ACTION_CLAIM_LIMIT` 或单 worker 并发可直接代表共享 scope 容量 | 查询批量、单 worker 并发和共享 scope 容量分开；scope 容量由部署拓扑、数据库回写预算和 Gateway 安全在途量共同证明 |
| 只要协议代码可实现即可认为搜索 1000/日可达 | 代码可行性与业务容量可行性分离；63 个账号 × 2 次仅有 126 次协议损耗前理论上限，1000/日至少需要 500 个合格账号当量 |

统一履约读模型固定返回：

```text
target_count
confirmed_count
held_count
unknown_count
terminal_shortfall
remaining_count
projected_capacity_before_deadline
deadline_at
status
blocking_codes
calculated_at
```

其中只有 `confirmed_count` 可以关闭履约欠额。`held_count` 仅防止重复规划，`unknown_count` 只表示远端结果未知，failed/skipped/unavailable 进入 `terminal_shortfall`。履约状态只有：

- `met`：真实确认达到目标，逐账号/逐消息子目标全部达到，`held_count=unknown_count=terminal_shortfall=quantity_overflow_count=open_excess_count=0`，且不存在影响该 ledger/义务的 active `consistency_quarantine`。
- `at_risk`：尚未截止，仍可能完成，但进度或预测容量不足。
- `blocked`：尚未截止，已有配置、权限、协议或安全容量证据证明无法完成。
- `missed`：已过 deadline 且未达到目标。

保存时必须区分“结构配置冲突”和“外部容量不足”。AI 活群/评论/点赞/浏览/纯搜索点击的通用小时软上限，以及评论/点赞/浏览/纯搜索点击的任务级及任务内账号级软上限，不再由运营配置，系统统一持久化 `1_000_000`；账号池不足、部分账号未准入、动态消息增加等外部容量问题允许保存、创建和启动，只在启动后的任务详情/可选只读诊断显示 `blocked/at_risk`、真实容量缺口和处理入口，Planner 只按账号全局、授权、代理、协议、内容和 Telegram 硬安全容量建单并持续重算，不降低业务目标。

浏览/点赞 Planner 的批量候选读取不是远端事实所有权承诺。若 Dispatcher 在候选读取后、单条 Action 创建前并发确认同一账号—消息源，或已有仍有效的 current Action，Planner 必须在创建 Action 前重新读取义务并只跳过该源，继续处理同 Task 的其他欠额；不得创建孤儿 Action，也不得让 `fulfilled_obligation_cannot_be_rebound|fulfillment_obligation_already_bound` 回滚整条 Task。

Dispatcher 的三种容量语义固定为：

```text
ACTION_CLAIM_LIMIT = 单次数据库候选查询/claim 批量
DISPATCHER_CONCURRENCY = 单个 Dispatcher 进程执行并发
DISPATCHER_SCOPE_CAPACITY = 同一共享 scope 的全 worker 合计在途上限
```

`DISPATCHER_SCOPE_CAPACITY` 不能直接写死为当前 100；它必须不高于有效 worker 总槽位、数据库回写连接预算和 Telegram Gateway 安全在途预算。共享 worker 配置版本不一致时停止新增 claim 并暴露错误。claim 热事务只锁 scope、window、allocation、reservation、Action，不更新 `Task.stats` 或覆盖账本；Planner 的运行边界、覆盖更新和履约决策分别使用短事务。

全部远端 mutation 的 B0 必须冻结并提交 Attempt 级 `gateway_request_identity + request/target fingerprint`；Gateway 后的成功、失败、延期或 membership 重排只能合并结果，禁止用 Action 当前 result 覆盖这些不可变字段。浏览/点赞成功只把源消息 `remote_fact_id` 交给 B1，唯一 View/ReactionRemoteFact 由 B1 收尾单点创建；即使数据库 Session 关闭 autoflush，也不得由 Gateway 路径与通用投影在同一事务各插入一份远端事实。

共享调度发布激活不得按主库全历史规模逐 Window 重放账本。业务 writer fence 后，只对未结束 Claim Window 做完整 allocation/reservation 守恒；已结束 Window 只批量修复由真实 active Action 可重建的 Window/Allocation active 投影，历史 unclaimed 和 search outcome/release 继续由其原 owner 协议收口。生产已有数千历史 Window 或数十万 Reservation 时，激活路径仍不得形成逐 Window N+1 或长期持有 Scope 锁阻塞 heartbeat。

激活前 writer 已 fence，closed Window active 投影经修复后必须为0；但合同 active 后，执行跨过60秒 Window 结束边界的真实 `executing + dispatch_claim_active` Action仍合法占用原 Window，直至B1原子释放。post-deploy `verify-active` 必须锁定 Scope 后验证 Scope/Window/Allocation active与Action冻结binding精确一致；不得复用激活前“closed active=0”条件误杀真实在途，也不得放宽为忽略错绑或计数漂移。candidate 只读与 Scope 加锁发生在同一 ORM Session 时，加锁查询必须用数据库最新行覆盖 identity map 缓存；否则并发 claim/release 会把旧 Scope 计数与新 Action 集合拼成不存在的混合快照。刷新不得演变为运行期自动对账或 silent repair。持有 active claim 的 Action 被 pre-Gateway 门禁、目标失效、群发送限流或准入窗口改成 `pending|skipped|failed` 时，Action 状态与 Scope/Window/Allocation claim 释放必须同一事务提交；只有仍保持 `executing + dispatch_claim_active` 的 Attempt/Gateway 边界可以提前提交。禁止先提交非 executing Action 再由外层补账。生产数据库 Session 为 `autoflush=false`，所以释放逻辑在按 SQL 重算 active Action 前必须显式 flush 当前 Action 的非 executing 状态；只 flush 不 commit，完整释放失败仍整体回滚。

`effective_unclaimed_count` 是 live Window 容量投影，不是需要定时器归零的历史事实。`bucket_end <= observed_at` 后其逻辑值立即为0，数据库中尚未被后续 owner 更新的非零存储值只保留历史未领取快照，不得参与容量或导致只读 Release Gate 失败；live Window 仍必须严格核对 stored effective、Reservation/Allocation 与容量守恒。

已结束 Claim Window 中仍受原 search assignment owner 管理的预绑定 unit，在 Gateway 前失效时继续由唯一 release batch 收口：原 Reservation、Allocation 与 Window 的历史 `unclaimed_allocated_count` 按实际 unit 减少；`effective_unclaimed_count` 已不再参与当前 scope 容量，必须保持零且不得二次扣减，也不得为已结束 Window 开启 rebuild wave。过期判断必须先于 effective 负数校验，不能让一条历史搜索释放异常回滚 Dispatcher 整轮 claim并阻塞其他任务。

生产数据库关闭 autoflush 时，激活收敛必须在同一事务内先显式 flush 新投影、再运行聚合 invariant 校验，最后一次提交；不得让校验读取旧数据库值误判失败，也不得用提前提交削弱原子性。

激活使用同一 `observed_at` 划分未结束与已结束 Window 时，分类必须由数据库表达式完成；不得在 Python 直接比较数据库 offset-naive 时间与应用 offset-aware 时间。

运行期 shard liveness 也必须遵守同一平台时钟：应用 `_now()` 的无时区值表示北京时间墙钟，先绑定 `Asia/Shanghai` 再与数据库 aware heartbeat 转 UTC 比较。禁止直接绑定 UTC 或用扩大 stale 窗口补偿；真实 live shard 被误判 stale 时，所有任务会出现 scope 有空闲但 Reservation 全为零的假容量不足。

AI 历史内容 scope 接管的新 preview 只处理 open 与 `unknown_after_send` 可变 Action，历史不可变终态 Action 不重复创建 noop item；运行成本不得随全历史终态 Action 无界增长。未进 Gateway 且 payload/正文无效的 open Action 必须按原 quantity/content slot 进入 `content_contract_replan_required`，不得误作事实矛盾 quarantine 阻塞整次发布。

生产数据库关闭 autoflush 时，AI scope takeover 的首次 conflict 与每批 apply 必须先在同一事务显式 flush Action/item 新状态，再聚合 processed/applied/noop/conflict/quarantine counters 并判断 batch 完成；不得提交与 item 事实不一致的旧计数。

搜索 CAPTCHA 继续使用 §2.19 的双 OCR 顺序合同与按钮候选精确匹配：RapidOCR 无效时进入同 fingerprint 的 ddddOCR；A rejected 后仍为同 fingerprint 才允许 B；A/B callback 拒绝已产生新 fingerprint 时从 A 重开；B 无安全答案或拒绝后仍为旧 fingerprint 仅允许协议样本审批的 refresh callback，无动作写 refresh_not_supported；禁止 AI/VLM、confidence 放行、模型投票、mock 成功、重复点击同一 fingerprint 或跳过候选校验。协议样本未通过真实 `target_click_observed` canary 前不得批量 source。容量不得使用验证码触发率、历史成功率或目标命中率做概率折损；尚未进入验证页的路径只可计 eligible attempt 上界，已出现验证码的路径只有本次实际写入 `jisou_image_verification_solved` 后才恢复 click opportunity，`required|failed|callback unknown` 均不能计预测确认或 confirmed。OCR 与 refresh 不占 click 限额、目标或额外中央份额；运行失败显式保持 required/failed，不触发 AI fallback。账号剩余安全额度仍按真实账本扣减，不得以提升 legacy `per_account_daily_action_limit` 作为默认补容量方案。

发布使用依赖闭合的 release train：Base 组件先部署但不取得业务 Gateway 写资格；AI train 将 Recovery、义务投影、动态目标/C2/AI 并发/Provider 两级额度一起激活后做 AI E4；Search train 将独立通道、assignment 直接执行、双 OCR 无 AI 一起激活后做 click E4；Source train 将评论/点赞/浏览义务、随机探针和负面 CAS 一起激活后做 source-window E4；最后做多任务同日并行 E4。组件测试、本地测试、health 或单个已部署 SHA 均仍为 `production_unproven`，不能替代 train 的端到端远端事实。

## 3. 模块 PRD

## 3.1 运营中心

### 页面目标

让运营人员进入后台后先看到今天要处理哪些目标、哪些任务失败影响了目标、哪些方案正在运行、哪些异常需要人工介入。运营中心是日常工作台、运营方案和异常处理入口。

运营中心分上下两部分：

- 上半部分：目标工作台，默认按运营目标展示状态、异常、任务失败聚合和建议动作。
- 下半部分：运营方案 / 策略模板，维护群活跃、频道互动、转发监听和账号使用策略，并生成或调整任务。

### 目标工作台

默认按目标聚合展示：

| 区域 | 内容 |
| --- | --- |
| 目标状态 | 群 / 频道 / 讨论组标题、类型、活跃度、最近事件、当前方案 |
| 运营异常 | 账号、目标权限、准入、AI 质量、规则、风控、TG 限制、监听、容量等异常 |
| 任务失败聚合 | 关联任务数、失败 action 数、主要失败码、最近失败时间 |
| 建议动作 | 重新登录账号、重新准入、同步目标、检查规则、暂停任务、调整方案；默认打开上下文弹窗 / 抽屉，复杂流程才深链跳转 |
| 效果摘要 | AI 接话率、暖场响应率、频道互动完成度、转发成功率、失败率 |

目标异常展开后展示关联任务失败：

| 字段 | 内容 |
| --- | --- |
| 任务 | 任务名称、类型、状态、来源方案 |
| 失败 | 失败数、失败码、失败原因、最近失败时间 |
| 影响 | 受影响账号、目标能力、预计缺口 |
| 操作 | 查看任务详情、重试失败项、暂停任务、查看目标、处理账号、查看规则；轻处理留在弹窗，中等处理打开抽屉，重处理深链跳转并保留返回位置 |

### 运营中心上下文处理模式

运营中心是日常工作台，不能把所有处理都做成裸跳转。目标异常、建议动作和方案影响预览默认使用“上下文处理模式”：运营人员在目标工作台点开异常后，优先在弹窗或抽屉里完成查看、确认和轻量处理；只有复杂流程才跳到对应页面。

| 处理级别 | 适用动作 | 交互方式 | 关闭 / 完成后 |
| --- | --- | --- | --- |
| 轻处理 | 查看目标异常、查看账号不可用原因、查看规则命中、查看风控限制说明、确认处理、忽略异常、标记已处理、刷新当前 issue 摘要 | 在运营中心打开上下文弹窗 | 保持当前目标展开、筛选、分页和滚动位置；弹窗关闭后可刷新当前目标摘要 |
| 中等处理 | 重试失败项确认、同步资产触发、同步安全状态触发、目标准入失败处理、目标能力小调整、规则版本选择、方案影响预览、任务暂停 / 恢复确认 | 在运营中心打开右侧处理抽屉；抽屉只调用对应模块已定义的小闭环接口，不承载长表单和批量选择 | 抽屉顶部保留来源目标、任务、账号、失败类型和 issue；完成后刷新当前 issue，不重置页面状态 |
| 重处理 | 新增账号、复杂登录流程、批量资料初始化、设置二步密码、清理登录设备、完整规则集编辑、完整运营方案编辑、查看大量 action / attempt 明细 | 深链跳转到对应页面，并携带 `return_to`、`source_issue_id`、`target_id`、`task_id`、默认 Tab 和筛选条件 | 目标页面提供“返回运营中心原位置”；返回后恢复目标展开行、筛选、分页、滚动位置和最近选中的 issue |

上下文弹窗 / 抽屉必须展示来源上下文：

- 目标：`target_id`、目标名称、目标类型、当前状态。
- 异常：`issue_id`、`failure_type`、严重级别、最近失败时间、影响账号数、影响任务数。
- 任务：关联任务摘要、代表 action、最近执行结果。
- 建议：建议动作、预计影响、是否需要原因、是否会写审计。

运营中心不能把其他中心完整搬进来。弹窗和抽屉只做小闭环和摘要处理；需要长表单、批量选择、复杂编辑、敏感配置或大量明细时必须深链跳转。

### 运营方案 / 策略模板

| 方案 | 内容 |
| --- | --- |
| 群活跃方案 | 接话优先、低频暖场、话题种子、AI 账号角色、无人响应降频 |
| 频道互动方案 | 浏览、点赞、评论、回复比例、节奏、消息范围 |
| 转发监听方案 | 源群、目标群、过滤、转换、路由、目标群发送量 |
| 账号使用策略 | 账号范围、冷却、容量、换号和恢复建议；账号安全和资料初始化只提供跳转建议，不在运营中心执行 |

方案可以生成或调整任务；任务中心负责执行和明细。运营中心不展示每条 action 原始明细。

#### 方案模板列表

运营方案 / 策略模板在运营中心下半部分展示，默认按目标类型和任务类型分组。每个模板卡片必须展示：

| 字段 | 说明 |
| --- | --- |
| 方案名称 | 运营人员可识别的名称，例如“群自然活跃-晚高峰”“频道新消息点赞评论” |
| 适用目标 | 群、频道、转发源群、转发目标群、账号分组 |
| 生成任务类型 | `group_ai_chat`、`group_relay`、`channel_view`、`channel_like`、`channel_comment`、`search_click`、`search_rank_deboost` |
| 默认账号范围 | 全部可用账号、指定账号分组、手动账号池 |
| 24 小时曲线 | 全天自然活跃、晚间高峰、工作日双峰、活动预热、低打扰保守或手动曲线 |
| 规则版本 | 已发布规则集和版本；草稿规则不能生成运行任务 |
| AI / 内容策略 | 黑话词表、接话策略、评论方向、改写策略、事实锚点要求 |
| 风控摘要 | 每小时 / 日上限、账号冷却、目标冷却、失败处理策略 |
| 运行状态 | 未生成任务、已生成任务、部分任务异常、需要调整 |
| 最近效果 | 成功率、失败率、AI 跳过率、最近异常、最近更新时间 |

模板列表主按钮：

| 按钮 | 行为 |
| --- | --- |
| 新建方案 | 打开方案编辑抽屉 |
| 从目标生成方案 | 选择目标后带入目标类型、能力、账号覆盖和推荐任务类型 |
| 生成任务草稿 | 根据方案创建 draft 任务，不启动 |
| 生成并启动任务 | 根据方案先幂等创建任务，再走统一 start；运行资源不足时任务仍进入 running 并在详情显示 waiting/blocker |
| 调整关联任务 | 修改方案后批量预览对关联任务的影响，再确认更新 |
| 暂停方案 | 暂停由该方案生成的任务，不删除历史任务和失败事实 |

#### 方案编辑抽屉

方案编辑抽屉分四段：

| 段落 | 字段 |
| --- | --- |
| 基础信息 | 方案名称、说明、适用目标类型、默认启用状态、负责人 |
| 目标范围 | 指定目标、目标标签、目标类型、排除目标、是否允许任务内粘贴新目标 |
| 执行策略 | 任务类型、24 小时曲线、账号范围、规则版本、风控上限、失败策略 |
| 内容 / AI 策略 | AI 黑话词表、话题方向、接话 / 暖场模式、评论方向、转发处理方式、素材策略 |

保存方案只写方案配置，不直接创建 action。只有点击“生成任务草稿”或“生成并启动任务”时才创建任务。

#### 方案生成任务流程

```text
选择方案
  -> 选择目标范围
  -> 选择生成模式：草稿 / 创建并启动
  -> 后端按目标拆分任务草稿
  -> 对每个请求执行调用者授权、目标/账号范围引用、规则版本和数量/内容合同静态校验
  -> 前端展示生成预览：会创建、静态非法、会复用已有任务；不读取运行容量/准入/传输事实
  -> 运营人员确认
  -> 写入 tasks、task_runtime_summary 初始摘要和审计
```

生成预览必须展示：

- 目标数、预计任务数、复用已有任务数。
- 每个目标会生成的任务类型。
- 账号候选数、已满足准入账号数、可准备账号数、不可准备账号数。
- 规则版本和结构化目标；AI 活跃群展示群日配置目标、当前必达/recovering/abandoned/completed 账号预计、0～3 个配置频道和准入子任务预览。创建预览不展示运行容量、24 小时曲线、速率、静默权重、due-by-now、Task 份额或预扣；这些字段不属于当前合同。
- 阻塞项：无可用账号、目标不可解析、规则未发布、目标不可发言、风控阻断。

#### 方案调整任务规则

方案调整不直接覆盖所有运行中任务。前端必须先展示影响预览：

| 变更类型 | 默认处理 |
| --- | --- |
| 文案、说明、负责人 | 只更新方案，不影响任务 |
| 24 小时曲线、群日目标、账号范围、规则版本、AI 策略 | 展示关联任务影响后调用任务配置更新。运行中的 AI 活跃群变更 timezone 或手工群日目标只写下一 TaskDayLedger 的 pending revision；系统 `natural_full_day` 不提供非零小时权重编辑。账号范围按 current task-day scope revision 动态加入/退出并走 typed coverage/target CAS，不清理或重排 Gateway/unknown/confirmed。规则版本与 AI 策略只影响未绑定 current intent；历史事实和 Gateway owner 永不改写 |
| 目标范围新增 | 为新增目标生成任务预览 |
| 目标范围移除 | 提示是否停止关联任务；默认不删除历史任务 |
| 风控上限降低 | 影响任务后续规划，不回滚已完成 action |
| 任务类型移除 | 提示停止对应类型任务；原始失败事实保留在任务中心 |

方案调整后的运行结果仍由任务中心记录；运营中心只展示目标级摘要、异常和建议动作。

### 运营异常模型

运营异常是任务中心失败事实上卷到运营中心后的处理对象。第一版先不做分库分表，但也不能让运营中心实时扫描 `actions`、`execution_attempts` 等明细大表。默认方案是建立或维护运营汇总读模型，由后台增量聚合失败事实、账号状态、目标能力、规则拦截和 AI 质量跳过，运营中心只读目标级异常和摘要；点开异常后再跳到任务中心查明细。

```text
operation_issue
- target_id
- issue_type
- severity
- representative_task_id
- representative_action_id
- affected_task_count
- affected_account_count
- failure_type
- failure_reason
- suggested_action
- handling_mode: modal / drawer / deep_link
- return_to
- claimed_by / claimed_at
- status: open / acknowledged / resolved / ignored

operation_issue_sources
- issue_id
- source_type: task / action / message_task / listener / risk_event
- source_id
- failure_type
- latest_seen_at

operation_issue_accounts
- issue_id
- account_id
- impact_type
- latest_seen_at
```

聚合规则：

- 默认按 `target_id` 聚合；目标异常点开后再看关联任务失败。
- 同一目标下按 `issue_type`、`severity`、`failure_type` 和代表来源合并，完整来源进入 `operation_issue_sources` 分页表。
- 运营中心只展示需要运营判断或影响目标效果的失败；任务中心保留完整 action 明细。
- 运营中心列表和目标工作台默认读取 `operation_issue`、`target_runtime_summary`、`task_runtime_summary` 等汇总读模型，不直接扫执行尝试明细。
- 汇总读模型允许分钟级延迟，并展示最近更新时间；需要精确原始错误时从目标异常下钻到任务中心。
- `COMMENT_UNAVAILABLE`、目标不可发言、准入失败、账号掉线、AI 质量跳过、规则持续拦截、监听无事件、容量不足等都应上卷为运营异常。

### 主要按钮

| 按钮 | 行为 |
| --- | --- |
| 刷新当前数据 | 调用全局 refresh，重新拉取 overview、账号、任务、目标、规则等数据 |
| 查看目标异常 | 打开目标异常抽屉，展示关联任务失败、建议动作和处理方式 |
| 创建 / 调整方案 | 打开运营方案配置，生成或调整任务 |
| 查看任务详情 | 轻量摘要在抽屉内展示；大量 action / attempt 明细深链到任务中心详情并带返回上下文 |
| 处理账号 / 目标 / 规则 | 优先打开上下文弹窗 / 抽屉；复杂流程深链到对应页面并带返回上下文 |

### 数据来源

- `GET /api/overview`
- `target_runtime_summary`
- `task_runtime_summary`
- `account_runtime_summary`
- `operation_issue`
- `tasks` / `actions` / `execution_attempts` 下钻查询
- `tg_accounts`
- `operation_targets`
- `risk-control` 聚合服务

### 页面展示契约

运营中心首页和目标工作台只能读取汇总读模型。页面展示必须能说明数据更新时间，并提供下钻入口，不能在页面请求中全量聚合执行明细。

| 页面区块 | 默认读取 | 展示字段 | 下钻入口 |
| --- | --- | --- | --- |
| 顶部状态卡 | `target_runtime_summary`、`task_runtime_summary`、`account_runtime_summary` | 目标异常数、运行中任务、失败 action、受影响账号、最近更新时间 | 目标异常列表、任务列表、账号列表 |
| 目标工作台 | `target_runtime_summary` + `operation_targets` | 目标名称、类型、状态、当前方案、open issue 数、最近失败时间、效果摘要 | 目标详情、目标异常抽屉 |
| 运营异常列表 | `operation_issue` | `issue_type`、`severity`、`failure_type`、影响任务数、影响账号数、建议动作、状态 | 异常详情、任务中心详情、账号 / 目标 / 规则处理页 |
| 关联任务失败 | `task_runtime_summary` + `operation_issue_sources` | 任务名称、类型、状态、失败数、主要失败码、最近失败时间 | 任务详情、失败 action 明细 |
| 账号影响 | `account_runtime_summary` + `tg_accounts` | 账号状态、FloodWait、受限、失败次数、最近错误 | 账号详情、重新登录、代理处理 |
| 效果摘要 | `daily_runtime_stats` + 目标 / 任务汇总 | AI 接话率、暖场响应率、频道互动完成度、转发成功率、失败率 | 运营数据报表 |

禁止展示路径：

- 运营中心首页不得直接扫描 `actions`、`execution_attempts`、监听消息或 AI 上下文明细。
- 目标工作台不得为了计算失败数做跨任务全量聚合；失败数必须来自汇总读模型。
- 只有用户点开目标异常、任务详情或失败 action 时，才进入明细下钻查询。

---

## 3.2 系统设置

### 页面目标

维护平台运行基础配置，不承载任务节奏、运营方案、目标异常处理和具体风控处置策略。

### 功能块

| 功能块 | 主要对象 | 主要按钮 |
| --- | --- | --- |
| Telegram 开发者应用 | `telegram_developer_apps` | 新增应用、详情、编辑、检查、启用、禁用 |
| AI 供应商 | `ai_providers` | 新增供应商、编辑、检查、启用、禁用 |
| 平台 AI 配置 | `tenant_ai_settings` | 编辑当前平台实例的 AI 配置；表名保留代码兼容，不表达多租户 SaaS 主线 |
| 提示词模板 | `prompt_templates` | 新增提示词、编辑 |
| 素材运行配置 | 素材缓存账号、缓存频道链接、上传限制、临时文件 TTL | 编辑缓存与上传配置、查看缓存健康快捷入口 |
| Clash 配置 | `proxy_airport_subscriptions` / `proxy_airport_nodes` | 保存多个订阅源、设置主备优先级、测试连通性、同步节点、查看同步状态和健康节点数 |
| 后台账号权限 | `app_users` | 新增用户、编辑、重置密码、当前用户自助修改密码、调整额度、配置菜单/按钮权限；顶部操作区必须提供当前用户自助修改密码入口 |
| 平台额度 | `tenants` | 编辑账号额度、任务额度、通知配置；底层表名保留当前实现 |

### 关键规则

- 新增 TG 账号前必须至少有一个健康且可分配的开发者应用。
- 生产环境推荐至少维护 3 个健康 TG Developer App：1 个默认主用池和 2 个备用 / 分担池。应用池用于分散登录和授权容量，不等同于账号备用 session。
- `max_accounts` 表示该开发者应用可承载的账号分配上限。达到上限后，新账号登录不得继续分配到该应用；已有账号不因达到上限被强制迁移。
- 停用开发者应用前必须展示影响范围：已绑定账号数、主授权数、备用授权数、最近 24 小时执行账号数。停用后不再分配新授权；已有授权进入“应用停用风险”提示，按账号授权资产迁移流程处理。
- AI 生成资料、AI 活跃群、AI 评论和 AI 改写都依赖健康 AI 供应商。
- 文本生成供应商支持 OpenAI-compatible 的 DeepSeek、MiMo/Mino 和 MiniMax。MiniMax 文本模型按 `base_url=https://api.minimax.io/v1` 或 `https://api.minimaxi.com/v1`、`api_key_header=Authorization`、`model_name=MiniMax-M3/MiniMax-M2.7/MiniMax-M2.7-highspeed/MiniMax-M2.5` 配置；`MiniMax-M3` 调用时必须显式关闭 thinking，避免推理内容挤占最终 JSON draft。MiniMax 也可作为图片验证码多模态视觉供应商；DeepSeek 等纯文本模型不得用于图片识别。
- 素材日常上传、批量上传、分组、编辑和禁用不在系统设置处理，必须进入素材中心。
- 素材运行配置面向普通管理员时只能要求填写 Telegram 频道链接、公开用户名或 `t.me/c/...` 链接，不能要求用户手动填写 `-100...` peer id；系统保存用户输入的原始链接，并归一化为执行层可用的缓存目标。
- 权限配置修改后必须更新 `permission_version` 并写审计。
- 群活跃话题、AI 暖场间隔、频道互动节奏、转发监听方案、账号使用策略和任务失败处理入口属于运营中心，不在系统设置中编辑。

### 素材缓存频道与执行账号配置

素材缓存频道是平台底座配置，归属系统设置的“提示词与素材运行配置”Tab。页面必须提供三个可独立配置的输入项：

- 素材缓存频道：用于后台上传的图片、表情包、文件和组合消息缓存。
- 源媒体缓存频道：用于转发监听保留源媒体时的临时缓存。
- 缓存执行账号：用于素材缓存频道权限探测和后台素材缓存上传，必须能在系统设置内直接选择。

保存素材运行配置时必须区分 `PATCH /api/materials/cache/config` 保存失败和保存成功后的资源刷新失败。保存失败展示“保存缓存配置失败”及后端错误；保存成功但刷新最新配置 / 健康状态失败时展示“缓存配置刷新失败”，不得误报为保存失败。保存请求还必须绑定发起时的缓存配置 payload 签名与保存请求序号；保存返回前修改缓存频道、源媒体频道或缓存执行账号时，旧保存响应不得覆盖当前保存错误、刷新错误、成功提示或警告提示；loading 清理必须绑定当前保存请求序号。

输入形态必须贴近运营人员认知，支持：

- 公开频道或群的链接，例如 `https://t.me/example_cache`。
- 公开 `@username`，例如 `@example_cache`。
- 私有频道转发链接或消息链接，例如 `https://t.me/c/1234567890/55397`，系统从链接中解析频道部分。
- 已知 peer id 只作为高级兼容输入，不作为页面主提示。

保存时后端必须做归一化和校验：

- `t.me/c/<internal_id>/...` 归一化为执行层可解析的 `-100<internal_id>`。
- `https://t.me/<username>` 归一化为 `@username` 或等价 Telethon 可解析目标。
- 两个缓存频道允许填写同一个链接，适合本地和小规模部署；生产环境推荐分开。
- 缓存执行账号选择器只展示当前租户未删除 TG 账号，选项至少展示备注名、手机号、username、状态和健康分；必须支持按手机号、备注名和 username 搜索。
- 缓存执行账号可为空：为空时兼容旧逻辑，按在线账号健康分从高到低自动尝试；已配置时，保存缓存频道和后台素材缓存必须优先使用该账号。
- 指定缓存执行账号不可用、未登录、未加入缓存频道或没有发消息 / 发帖权限时，页面必须提示“已保存但指定缓存执行账号不可用或无权限”，并说明需要重新登录、加入频道或授予发布权限；不得只提示泛化的“缓存频道不可访问 / 账号无权限”。
- 为保证运行连续性，指定账号失败时后台可以继续尝试其它在线账号作为备用，但页面必须保留指定账号异常提示，避免管理员误以为指定账号已通过。
- 系统设置保存值优先于 `.env`；未保存时继续回退 `SOURCE_MEDIA_CACHE_PEER_ID` 和 `MATERIAL_CACHE_PEER_ID`，保证现有部署兼容。
- 保存成功后，素材中心的缓存健康卡片应立即显示“已配置”，并继续展示最近缓存异常、FloodWait 和待缓存数量。

### 系统设置与 AI / 素材中心边界

系统设置维护“平台底座配置源”，运营中心、任务中心、规则中心和素材中心消费这些能力，但不反向修改底座配置。代理治理分层：Clash 订阅源池、主备优先级和同步健康是平台级节点来源，归系统设置；账号 / 授权槽位代理绑定归“账号面具 > 账号代理”；代理健康、限流、处置和告警归风控中心。系统设置不得分配账号或授权槽位代理，风控中心不得保存订阅密钥。账号面具的账号代理页可以选择账号中心分组作为批量绑定范围，但该动作只更新已有授权环境绑定；同一账号同一 `session_role` 下存在多个 active 授权环境时必须全部更新，成功账号数按账号去重统计；没有授权环境的账号必须跳过并显示原因，不得因为选择分组而自动创建授权环境、启用 Clash 订阅源或让任务静默走代理。

| 能力 | 权属页面 | 可在业务页出现的入口 | 禁止混入 |
| --- | --- | --- | --- |
| TG 开发者应用 | 系统设置 | 账号新增 / 登录前置健康提示 | 具体账号登录流程和批次明细 |
| AI 供应商和默认模型 | 系统设置 | 运营方案、资料初始化、AI 评论、AI 活跃群的健康提示和快捷跳转 | 群活跃话题、评论节奏、任务目标量 |
| 提示词模板和黑话配置 | 系统设置 | 任务创建向导和运营方案中选择已发布模板 | 运行中任务的临时话术覆盖 |
| Clash 订阅源池 | 系统设置 | 账号面具和任务预检展示主备订阅状态、节点同步状态、健康节点数和配置入口 | 授权槽位代理分配、账号中心分组选择、账号级换节点、风控处置 |
| 素材资产库 | 素材中心 | 消息发送、规则、转发监听、AI 活跃群和账号资料初始化中选择素材 | 系统设置、发送记录、转发结果和任务执行明细 |
| 素材运行配置 | 系统设置 | 素材中心展示缓存账号、缓存频道、上传限制和缓存异常健康提示 | 表情包、头像包、图片、文件和组合消息的日常管理 |
| 后台账号与权限 | 系统设置 | 其他页面按权限隐藏按钮或显示无权限状态 | 业务角色临时授权、绕过后端权限校验 |
| 授权槽位代理绑定 | 账号面具 | 任务预检、账号详情和风控中心展示代理健康提示与深链入口；可按账号中心分组批量选择已有授权环境 | 系统设置、任务创建表单、运营方案 |
| 代理健康与处置 | 风控中心 | 账号面具和任务预检展示健康状态、告警和处理入口 | 全局订阅密钥保存、账号面具字段编辑 |

业务页可以提供“去配置”快捷入口。AI、提示词和权限保存动作仍回到系统设置对应 Tab；素材上传、批量上传、分组和编辑保存动作进入素材中心；运营方案只保存对配置源或素材资产的引用和业务参数快照。

---

## 3.3 TG账号管理

### 页面目标

TG账号管理是账号资产与账号维护中心，不是运营中心、消息发送页或风控处置工作台。

账号中心后续升级必须按六层事实组织页面、接口和验收口径：

```text
账号身份 -> 授权资产 -> 登录设备 -> 可用性与容量 -> 执行闭环 -> 记录与追溯
```

这六层不能互相替代：账号分组决定账号能否参与任务；授权资产决定平台是否掌握可用 session；登录设备只描述 Telegram 远端授权事实；可用性与容量决定当前能否被任务使用；执行闭环解释已具备权限后为什么还没有继续处理；记录与追溯负责把验证码、发送、授权、设备和批次结果沉淀为可查事实。

本页只负责：

- 账号接入、登录恢复、验证码 / 扫码 / 2FA 登录流程续办。
- 账号基础资料、完整手机号、username、昵称、头像、账号分组、开发者应用和代理绑定管理。
- 账号授权资产管理：主授权、备用授权、开发者应用、代理、session 健康和切换记录。
- 接码专用账号分组管理和登录 code 读取能力验证。
- Telegram 登录设备分类、非平台设备数量展示和普通运营账号的一键清理。
- 资料、联系人、群、频道等账号资产同步结果展示。
- 资料初始化、二步密码托管、登录设备清理和同步安全状态。
- 账号健康分、账号状态、可用性摘要、容量解释、待处理执行闭环和不可用原因展示。
- TG 官方验证码读取、发送 / 评论 / 回复 / 频道互动执行记录聚合和审计追溯。

本页不负责：

- 不创建或发送消息；联系人发送、小批量发送和素材选择统一在“消息发送”页完成。
- 不配置运营方案；AI 活跃群、转发监听、频道互动等方案统一在“运营中心”维护。
- 不承载风控处置队列或策略编辑；策略、处置、限制解除和命中记录统一在“风控中心”完成。
- 不把“受限账号”作为运营中心的独立列表。运营中心只在目标 / 任务受影响时展示影响摘要，并跳转到账号管理或风控中心处理。

### 页面结构

账号管理页面由四层组成：

```text
摘要层：账号总数、普通运营账号、接码专用账号、在线、登录有问题、部分掉线、全部掉线、非平台设备、受限 / 封禁、同步过期、资料待初始化、待处理执行闭环
筛选层：账号身份、账号分组、登录状态、授权资产、登录设备、可用性、容量、资料状态、安全状态、代理、开发者应用、同步状态、最近批次、执行闭环状态
列表层：账号资产表格 + 资料初始化 / 设置二步密码 / 清理登录设备 / 补齐备用 session / 同步安全状态 / 提取验证码 / 重查待处理
详情层：按六层事实拆分的账号详情 Tab + 账号维护批次中心 + 执行记录聚合
```

摘要层只展示账号资产状态，不展示运营方案、联系人发送入口或风控处置队列。“受限 / 封禁”只能作为账号状态摘要和筛选入口存在；如果受限账号影响目标效果，由运营中心按目标展示影响并跳转，不在运营中心重复铺账号列表。

“登录有问题”是账号中心的快捷搜索入口，专门用于筛出当前没有登录上平台或当前主授权不可用的账号。命中范围包括：待登录、等待验证码、等待扫码、等待2FA、需重新登录、异常、Session 失效，以及授权资产中主授权状态不是 `active` 的账号。最近登录流水的失败类型 / 失败详情只用于原因展示和文本搜索；账号已经恢复为正常状态且主授权为 `active` 后，历史失败不得继续计入“登录有问题”总数。列表必须展示最近登录流水的失败原因，支持按“登录失败”“验证码没收到”“登录验证码没收到”“session 完全失效”等运营口径搜索。账号级受限、健康分偏低、代理异常和备用 session 缺口不自动并入该入口，避免把非登录问题误导为需要重新登录。

`Session失效` 与 `需重新登录` 必须和未完成登录状态一样展示“继续登录”，并始终按原 `account_id` 推进既有登录流。数据库中保存 Session 密文不是 Telegram 当前接受 AuthKey 的充分证据；只有明确授权失效事实才进入该路径，代理或同步失败不得自动改写为 Session 失效。完整已有账号重新授权合同见 `docs/03-feature-designs/existing-account-reauthorization-routing-prd.md`。

### 账号表格核心列

| 列 | 内容 |
| --- | --- |
| 账号 | 头像、平台展示名、TG 昵称、username、完整手机号 |
| 账号身份 | 普通运营账号 / 接码专用账号 / 降权任务专用账号；接码专用账号必须展示“禁止参与任务”；降权任务专用账号必须展示“仅参与 search_rank_deboost 任务” |
| 分组 | 当前账号分组、分组来源、移动分组入口；系统接码专用分组不可删除；降权任务专用分组不可删除，只能禁用 |
| 登录状态 | 在线、待登录、等待验证码、等待扫码、等待2FA、需重新登录、Session 失效、禁用 |
| 账号状态 | 正常、受限、疑似封禁、已封禁、异常；只表达账号生命周期和基础状态 |
| 开发者应用 | 应用名称、健康状态、凭证版本 |
| 授权资产 | primary、standby_1、standby_2 健康状态、部分掉线、全部掉线、可互救刷新、可切换状态 |
| 代理 | 代理名称、代理状态、告警状态 |
| 账号健康分 | 全平台统一 0-100 分；账号中心和风控中心必须同源同值 |
| 可用性摘要 | 可发送、可监听、可加入、可评论、可读验证码、容量解释、不可用原因、下次可重试时间；仅展示事实，不在本页发起发送 |
| 资产同步 | 资料、联系人、群、频道最近同步时间和同步结果 |
| 登录设备 / 安全 | 我们的 TG 应用设备数、非我们的 TG 应用设备数、设备读取状态、是否可清理、2FA 状态、安全待刷新原因 |
| 最近批次 | 最近资料初始化 / 2FA / 设备清理结果、失败原因、trace_id |
| 待处理执行 | 验证待处理、准入待处理、已可发言但待重排、容量冷却等待、最近阻塞原因 |

账号列表头像不得阻塞账号文字和操作入口。已有头像的行在头像容器进入可视区前显示“有头像”，进入可视区后才异步请求并显示“加载中”，成功后显示图片；请求失败明确显示“加载失败”。没有头像的账号继续显示展示名首字，不发头像请求。头像加载状态只属于浏览器展示状态，不改变资料完整度，不触发账号或 Telegram 写操作。
| 操作 | 详情、完成登录 / 继续登录、同步资产、同步安全状态、提取验证码、重查待处理、移动分组、移除 |

手机号不脱敏展示已作为本轮实现项进入全部账号关联链路。当前代码仍保留 `phone_masked` 兼容字段，用于历史数据缺失完整手机号时兜底；新接口和前端展示应统一优先使用完整 `phone_number`，联系人、归档成员、审计记录和导出日志的搜索 / 展示也必须覆盖完整手机号。

### 账号分组边界

账号分组只做账号资源分类和选择范围，不做发送页，也不做人员 / 联系人发送流程。

账号中心分组导航必须保证全部分组可达。分组数量多或名称较长时，应使用独立横向滚动 tablist、滚动按钮或可完整展示的下拉；不能依赖 `Space wrap + Segmented` 让控件被卡片宽度裁切。选中分组在最新分组列表中不存在、被删除、禁用或加载失败时，页面必须展示明确状态并禁用“进入账号分组”；不得静默回退默认分组或第一个分组。

新账号创建的目标分组必须是用户提交时仍可验证的 `AccountPool`。分组列表加载失败、目标组失效或目标组不可用时禁止提交，不能 fallback 到默认组。已有账号从新增入口进入重登时默认保留原分组；若用户希望移动到本次选择的分组，必须在授权成功后执行带 `expected_from_pool_id` 的显式迁移，CAS 失败时只标记迁移失败，不影响授权成功。

账号分组新增系统固定类型“接码专用分组”。该分组是平台内 `AccountPool`，不是 Telegram 群或频道。接码专用分组内放真实 TG 账号，这些账号只允许用于读取 Telegram 官方登录 code、登录恢复、授权资产刷新、健康检查和诊断。

接码专用身份采用“固定分组 + 账号身份字段”双保险：

- 固定分组：系统初始化唯一 `pool_purpose=code_receiver`、`is_system=true`、`system_key=code_receiver` 的 `AccountPool`。
- 账号字段：移入该分组后，账号必须同步写入 `tg_accounts.account_identity=code_receiver` 或等价账号身份投影。
- 任务排除、Planner、Dispatcher、Listener、Recovery 和任务预检必须以账号身份字段为准；固定分组只是管理入口和批量移动入口，不能成为唯一判断条件。
- 如果分组成员关系和账号身份字段不一致，系统必须显示“接码身份待修复”，并以更严格规则处理：禁止参与任务、禁止一键清理设备，只允许修复身份、诊断和读取 code。

接码专用分组硬规则：

- 接码专用账号不得参与任何运营任务、消息发送、监听、AI 活跃群、频道浏览、点赞、评论、回复、目标准入、Planner 候选池、Dispatcher 候选池或 Listener 候选池。
- 禁止只靠前端隐藏；后端任务预检、账号选择、Planner、Dispatcher、Listener、Recovery 都必须排除接码专用账号。
- 接码专用账号可以做登录、重新登录、授权资产刷新、验证码提取、健康检查和只读设备诊断。
- 接码专用账号不允许进入“一键清理非平台设备”批次；即使存在非平台设备，也只展示数量、明细和风险，不自动退出设备。
- 系统只能有一个默认接码专用分组；该分组不可删除，可以重命名展示名，但系统标识不可变。

降权任务专用分组（与接码专用分组并列）：

- `pool_purpose=rank_deboost` 标记降权任务专用分组；该类型分组与 `pool_purpose=code_receiver` 接码专用分组并列，是平台内 `AccountPool`，不是 Telegram 群或频道。同租户可以存在一个系统默认降权组和多个自定义降权组。
- 该类型分组不可删除，只能通过 `is_enabled` 显式禁用；`is_default` 只表示普通账号默认归属，不能作为启用状态。禁用后组内账号仍保持 `rank_deboost` 用途，不得被新任务选用，也不得自动回流普通任务。
- `AccountPool.pool_purpose` 是账号用途真相源，`TgAccount.account_identity` 是同步投影。账号新增、移动和历史修复必须在同一事务内同步 `pool_id + account_identity`；若不一致则进入 `account_purpose_mismatch` 并按更严格用途阻断全部外部动作。
- 组内账号只允许参与 `search_rank_deboost` 任务，以及登录、授权资产诊断、备用 session 补齐 / 自愈、只读设备诊断和健康探测；禁止进入其他任务候选池、旧消息/Campaign、监听、目标准入、资料初始化、账号面具初始化、2FA 设置 / 轮换和一键清理其他登录设备。
- 同一账号始终只有一个 `pool_id`。普通组与降权组之间的用途转换是原子迁移，不得因“当前在普通分组”而拒绝；迁移时必须取消与新用途冲突的 pending 动作、reconcile 在线来源并写前后快照审计。
- 任务候选池必须同时按 `account_identity` 和 `account_pools.pool_purpose/system_key` 做双保险。普通任务 all/group/manual 全部排除降权账号；`search_rank_deboost` all 只选择所有启用降权组中的一致可用账号，group 只选择指定启用降权组，manual 只接受启用降权组内的一致账号。
- 每个降权组持久维护一个可执行分组代理绑定，多个任务复用；任务创建、停止或删除都不拥有绑定生命周期。真实执行必须使用该绑定的 SOCKS/HTTP 运行端点完成当前出口探测和 Telethon 连接。
- 完整字段、API、状态机、Gateway、逐点击配额、迁移和验收设计见 `docs/03-feature-designs/search-rank-deboost-hardening-design.md`。

允许在账号管理中做：

- 新建、重命名、禁用账号分组。
- 查看分组内账号、批量移动账号、批量资料初始化、批量设置 2FA、批量清理设备。
- 普通账号分组可以作为任务创建、消息发送、风控策略和报表筛选的账号范围来源；接码专用分组只能作为账号维护和 code 读取范围，不得作为任何任务账号范围来源。

禁止在账号管理中做：

- 不在“进入账号分组”后展示联系人发送、人员发送、消息编辑或素材选择。
- 不在账号分组里配置运营方案、AI 接话策略、转发规则或任务节奏。
- 不把账号分组当权限隔离。权限仍由后台账号权限和操作审计控制。

### 账号健康分统一口径

全平台只能有一个账号分数，统一命名为“账号健康分”。账号中心、风控中心、任务预检、运营中心异常和报表中展示的账号健康分必须同源同值，不能出现账号中心一个分、风控中心另一个分。

账号健康分回答“这个账号当前是否适合参与运营任务”，取值 0-100，综合以下因素：

- 账号本体：登录状态、session、TG 限制、资料 / 联系人 / 群频道同步状态。
- 运行表现：最近成功执行、账号侧失败率、FloodWait、账号受限和账号级 TG 限流。
- 运行环境：代理绑定、代理健康、代理复用和代理异常聚集。
- 安全与容量：账号安全快照、外部设备 / 2FA 状态、小时 / 日容量、冷却和待重试时间。

目标、权限、任务配置和内容规则失败不得直接扣账号健康分。无评论权限、未通过群限制发言、目标未授权、目标无权限、目标 SlowMode、讨论组不可达、`task_deleted`、任务配置错误、内容拦截和规则拦截，只作为风控命中、处置项、目标能力异常或任务失败事实展示。只有当失败被明确归因为账号本体、账号运行环境或账号级限流时，才允许写入 `score_reasons` 并影响账号健康分。

风控中心不另设“风控分”或“风险分”。风控中心只在同一个账号健康分基础上展示风险等级、当前策略、扣分原因、处置建议和命中记录。风险等级 A/B/C/D/E 是健康分和硬阻塞条件的解释层，不是第二套分数。

实现兼容口径：账号健康分的权威来源统一为 `account_runtime_summary.health_score`。`tg_accounts.health_score` 只作为历史兼容快照和迁移兜底，不作为前端展示的第二套分数。风控服务在运行时叠加代理、安全、容量、失败趋势等调整后，必须写回 `account_runtime_summary.health_score`、`risk_level` 和扣分原因，再供账号中心、风控中心、任务预检和运营中心共同读取。过渡期不允许在前端把“基础健康分”和“风控调整分”同时展示成两个分数。

### 筛选项

| 筛选 | 说明 |
| --- | --- |
| 账号身份 | 普通运营账号、接码专用账号、禁止参与任务账号、降权任务专用账号 |
| 账号分组 | 全部账号、指定分组、未分组、接码专用分组、降权任务专用分组 |
| 登录状态 | 在线、待登录、等待验证码、等待扫码、等待2FA、需重新登录、Session 失效 |
| 快捷搜索 | 登录有问题：当前为待登录、等待验证码、等待扫码、等待2FA、需重新登录、异常、Session 失效或主授权不可用；最近登录失败和验证码没收到保留为原因搜索词，不单独增加当前问题数 |
| 账号状态 | 正常、受限、疑似封禁、已封禁、异常、禁用 |
| 可用性 | 可发送、不可发送、可监听、不可监听、可加入、不可加入、可评论、不可评论、可读验证码、不可读验证码、接码专用不可执行任务、降权专用不可参与其他任务 |
| 容量 | 容量可用、账号冷却中、小时额度已满、日额度已满、当前有 pending/executing 占用、容量汇总待刷新 |
| 登录设备 | 存在非我们的 TG 应用设备、非平台设备数量大于 0、非平台设备数量大于 N、可一键清理非平台设备、不允许一键清理设备、设备列表读取失败、current SV 登录时间未严格超过 48 小时或缺失 |
| 安全状态 | 未做过登录设备清理、最近设备清理失败、未设置 2FA、安全待刷新、设备快照未刷新、授权资产未健康检查、账号可用性汇总过期、TG 远端读取失败 |
| 资料状态 | 资料完整、资料待初始化、需重新资料初始化、资料不完整、无头像、无昵称、无简介、无 username、username 冲突、头像缓存失败、最近资料初始化失败 |
| 同步状态 | 资料同步过期、联系人同步过期、群 / 频道同步过期、同步失败 |
| 批次状态 | 无批次、最近批次成功、最近批次失败、执行中、待重试、已跳过、部分成功、等待头像缓存、预览需重抽 |
| 运行环境 | 开发者应用异常、代理不可达、代理认证失败、同代理异常聚集 |
| 授权资产 | primary 掉线、standby_1 掉线、standby_2 掉线、无备用授权、健康授权槽位不足 1 个 / 2 个 / 3 个、健康备用 session 不足 2 个、standby_1 session 缺失、standby_2 session 缺失、备用 session 未登录、备用 session 不可解密、备用 session 健康检查失败、备用 session 不可激活、可用健康槽位刷新掉线槽位、可从备用 session 激活恢复、三槽位全部掉线、曾登录账号全部掉线、有 1 个健康备用、有 2 个健康备用 |
| 执行闭环 | 有验证待处理、有准入待处理、已可发言但任务未继续、有 action 待重排、待处理重查失败、任务阻塞原因不明 |

### 账号详情 Tab

| Tab | 对应层级 | 展示内容 | 主要操作 |
| --- | --- | --- | --- |
| 基础资料 | 账号身份 | 昵称、username、完整手机号、头像、简介、账号分组、普通运营 / 接码专用身份、开发者应用、代理 | 编辑分组、同步资料、打开资料初始化 |
| 登录 / 验证 | 授权资产 | 当前登录流、验证码、二维码、2FA、Session 状态、最近登录时间、全部掉线入口 | 完成登录、继续登录、重新登录、取消登录 |
| 授权资产 | 授权资产 | primary、standby_1、standby_2 三张槽位卡；展示 Developer App、proxy、session 是否存在、健康状态、最近健康检查、可否读取官方 code、可否执行任务动作、失败原因 | 刷新槽位健康、用健康槽位刷新掉线槽位、激活恢复 primary、全部掉线时进入人工重新登录 / 扫码 / 手动验证码 |
| 登录设备 | 登录设备 | Telegram 远端授权设备列表；标记我们的 TG 应用设备 / 非我们的 TG 应用设备、对应槽位、current SV 登录时间、是否严格超过 48 小时、是否可清理和不可清理原因 | 刷新登录设备；普通运营账号满足条件时可一键移除非平台设备，不满足时按钮置灰并展示原因；接码专用账号只读 |
| 同步资产 | 记录与追溯 | 资料、联系人、群、频道同步结果；联系人只读展示，不提供发送入口 | 同步资产、跳转运营目标查看沉淀目标 |
| 可用性与容量 | 可用性与容量 | 可发送、可监听、可加入、可评论、可读验证码、资料修改能力；小时 / 日剩余额度、冷却、当前占用和不可用原因 | 重算可用性、查看容量解释、跳转风控 / 代理 / 登录处理 |
| TG 官方验证码 | 记录与追溯 | 登录验证码和 Telegram 官方服务 code 消息；展示 code、接收时间、有效期、读取槽位、原始消息摘要和失败原因 | 提取并展示官方验证码、复制验证码 |
| 待处理与执行闭环 | 执行闭环 | 入群问题、人工审批、图片验证码、准入待处理、已可发言但任务未继续、action 待重排、阻塞原因 | 重查目标权限、关闭已满足阻塞、重新排队可继续 action、跳转任务详情 |
| 账号安全 | 登录设备 / 安全 | 我们的 TG 应用设备数、非我们的 TG 应用设备数、2FA、最近安全快照、具体待刷新原因、最近加固结果 | 同步安全状态、普通账号清理非平台设备、设置 2FA、查看批次 |
| 托管 2FA | 授权资产 | 平台托管 2FA 策略、最近使用、轮换、查看 / 导出审计 | 保存、轮换、查看 / 导出审计 |
| 维护批次 | 记录与追溯 | 资料初始化、设置 2FA、清理登录设备、备用 session 补齐 / 自愈批次和失败项 | 查看批次、重试失败项、导出失败原因 |
| 执行记录 | 记录与追溯 | 手动消息 + Task/Action 发言、评论、回复、频道互动、AI 活跃群发言、最近失败、远端 message id | 只读查看；需要发送或任务操作时跳转消息发送 / 任务中心 |
| 审计记录 | 记录与追溯 | 登录、同步、敏感查看、导出、资料和安全动作 | 查看审计详情 |

账号详情默认打开“基础资料”Tab；从运营中心账号影响、任务详情账号失败、风控处置队列跳入时，应按来源打开对应 Tab，例如账号掉线打开“登录 / 验证”，外部设备异常打开“账号安全”，准入失败打开“同步资产 / 可用性”。

### 开发者应用池与账号授权资产

开发者应用池和账号授权资产是两层能力，不能混用：

| 层级 | 解决问题 | 关键对象 | 不能承诺 |
| --- | --- | --- | --- |
| 开发者应用池 | 分担账号登录和授权容量，降低单个 `api_id/api_hash` 风险 | `telegram_developer_apps`、`max_accounts`、健康状态、分配策略 | 不能替代已登录 session，也不能在 session 死亡后凭空恢复账号 |
| 账号授权资产 | 为同一个 TG 账号准备可切换的主备授权 | `developer_app_id`、`proxy_id`、`session_ciphertext`、授权角色、健康状态 | 未提前登录成功的备用配置不能作为可切换授权 |

目标态：

```text
TG账号
  ├─ 主授权 primary session
  ├─ 备用授权 standby_1 session
  └─ 备用授权 standby_2 session
```

授权资产规则：

- 我方设备归属只以“同账号的未撤销我方授权资产保存的唯一非零 Telegram authorization hash”精确匹配。`api_id`、App/设备名、IP/地区、创建/活跃时间只用于展示与一致性校验，不能单独使设备受保护。
- 一个账号的 primary/standby_1/standby_2 必须分别使用三套 Developer App 真实登录，并有三个不同 AuthKey 指纹和三个不同非零 remote authorization hash。SV 两槽可共用同一固定 IP，但仍是两个独立 Telegram 授权设备。
- hash 匹配某条我方授权、但远端 `api_id` 与该授权冻结的 Developer App 快照不一致时，设备仍受保护，但必须产生资产异常 blocker；反之，`api_id` 命中但 hash 不命中时，必须分类为非我方设备。
- 远端 hash 为零/缺失、一个 hash 匹配多条本地资产、我方授权缺失 hash，或设备集读取不完整时分类为“待识别”，禁止当次清理。
- 每个授权资产都必须通过验证码或 QR 完成真实 Telegram 登录。SV 授权必须保存中心加密 Session；MY standby_2 必须保存不依赖 SV 密钥的 MY 密封唤起包和 receipt，才算“可用备用”。
- 所有账号最终目标态都是 1 主 2 备；普通账号可以阶段性先达到 1 主 1 备，高价值账号优先补齐 1 主 2 备；迁移期允许只有主授权。
- 主授权首次登录成功后，账号安全 worker 必须自动创建 `standby_1 session` 和 `standby_2 session` 补齐项。`primary + standby_1` 在唯一 SV 业务出口创建和使用；`standby_2` 必须走 v2.16 MY operation，登录后依次完成 MY 本地不可变副本 fsync、独立对象快照、两份写后读/摘要/KMS 解封、原 client 断连、对象快照隔离 restore probe/断连、MY inventory 和中心 receipt，再提交槽位并休眠。设备清理的 48 小时门槛不得阻塞三套 App 的真实登录与 MY 补齐。验证码不可读取、2FA 未托管、需要人工 QR 或 Telegram 限制触发时，必须写入明确 blocker。
- 主授权验证码登录或扫码登录成功后，账号中心还必须自动检查资料完整度。展示名或 TG 姓名仍为英文 / 占位名、缺少 `username` 或缺少头像时，系统自动创建资料初始化批次；英文 / 占位名账号允许覆盖名称以保证中文资料落到平台展示名和 TG `first_name`，仅缺头像或 username 的中文账号不覆盖已有中文名称。
- 账号列表必须能筛出备用授权缺口，包括 standby_1 Session 缺失/不可解密/健康失败，以及 standby_2 未登录、MY wake bundle/receipt 缺失、MY 本地不可解封或显式演练失败。只选择 Developer App 或出口，但没有真实登录和对应凭据收据，不算可用备用。
- “备用授权不足”不得只按 Developer App 数量判断。standby_1 按 SV Session 授权与即时健康事实计数；standby_2 按 MY 密封包/receipt/qualification、最近显式事实和 MY client=0 计数，不要求 SV 解密其 Session。
- Developer App `assigned_accounts` 必须按该 App 下未撤销平台授权或非终态登录 operation 的不同账号数统计，不能只数 `TgAccount.developer_app_id`；三槽分别占用 App A/B/C 的账号名额，达到 `max_accounts` 时显示具体 App 缺口。
- 一主两备恢复口径：`primary` 有权威失败事实且 SV `standby_1` 即时 probe 通过时，自动执行冻结新业务领取、Gateway drain、current/account projection CAS 和 online/listener/sync/runtime summary 新代次重建，MY 保持休眠。业务解冻后仍是 SV 本地冗余降级，必须修复 logical primary 并受控切回，standby_1 再次 ready 后才恢复完整 1 主 1 备。只有 `primary + standby_1` 均不可用时，才能创建 v2.16 `emergency_reauthorize_primary`；SV login runtime 就绪后先发起新登录，MY 只交付 challenge-bound 官方登录码，最终由 SV 生成新 primary。
- 全部掉线口径：当 `primary`、`standby_1`、`standby_2` 三个槽位全部掉线、不可解密或健康检查失败时，系统不得伪造自动恢复，也不得继续静默重试；历史登录过的账号必须标识为“曾登录账号全部掉线”。全部掉线账号只能进入人工重新登录、扫码登录或手动验证码流程。
- 账号状态定时检查可记录三槽状态，并在 primary 权威失败且 standby_1 即时 probe 通过时自动创建 `local_activate`；它可以把 MY `standby_2` 标记为 `wake_probe_required`，但绝不能自动创建 MY wake permit 或唤起 MY 连接。
- 三槽必须使用不同 Developer App、AuthKey、客户端元数据组合和授权 hash，但一期出口绑定固定：`primary + standby_1 -> primary_regular(SV)`，`standby_2 -> standby_my(MY)`。不得以“槽位必须不同代理”为由新增 IP；若 observed exit IP 与对应固定出口不一致，页面显示“授权槽位出口冲突”并阻断执行。
- 任务执行只使用 SV 当前业务授权。每个 Gateway-bound Attempt 必须冻结 authorization/fact/connection/environment/proxy/fence；固定授权 assignment 在 Gateway 前因切换而释放并重排，已进 Gateway 的旧 Attempt 保持原代次并按远端事实收口。online、listener、联系人/群组/资料同步和 runtime summary 的旧代次结果不得覆盖新 current。
- standby_1 本地切换或新 primary 代次提交后，旧主授权保留为待修复资产，不自动删除；standby_2 不参与该 current 切换。账号软删除立即退出 Planner/Dispatcher/listener/online/sync 候选并禁止 MY 唤起，但授权资产继续可见，直到显式 decommission/erase readback 后进入 `authorization_retired`。
- 当前代码只有 `tg_accounts.developer_app_id + session_ciphertext` 时，映射为主授权。列表和详情展示“未配置备用授权”提示，但不阻塞现有登录、同步、发送和任务执行。
- 新增备用授权需要走完整登录流程；2FA 密码可以在第二步自动补齐，但不能替代 QR、验证码或 future auth token 等第一步授权。
- 系统不得在没有健康授权槽位的情况下提示“已具备无缝切换能力”。

账号管理提示口径：

| 场景 | 页面提示 | 是否阻塞现有能力 |
| --- | --- | --- |
| 没有登录上平台或主授权不可用 | “登录有问题：请继续验证码 / 扫码 / 2FA、重新登录或修复主授权” | 是；进入“登录有问题”快捷搜索 |
| 只有主授权，无备用授权 | “未配置备用授权，主 session 失效时需要扫码或验证码恢复” | 否 |
| 备用 session 缺失或未登录 | “备用 session 未就绪，一主两备不完整” | 否；但进入备用 session 缺口筛选 |
| primary 掉线、硅谷 standby_1 健康 | “正在硅谷自动切换备用 1 / 业务已恢复但本地冗余待修复” | MY 保持休眠；恢复 1 主 1 备前为 degraded |
| primary 与 standby_1 均失败、MY standby_2 具备休眠资格 | “可由马来西亚辅助重新登录，在硅谷生成新主授权” | 阻塞业务，允许创建 emergency-reauthorize operation |
| SV login runtime/出口未就绪 | “等待硅谷登录运行面恢复，马来西亚仍休眠” | 阻塞业务；MY 不连接 |
| standby_2 单独异常或需演练 | “MY 备用授权需修复 / 建议显式演练” | 不阻塞健康 primary；不得自动唤起或切换 |
| 三槽位全部掉线 | “曾登录账号全部掉线：只能人工重新登录 / 扫码 / 手动验证码” | 是 |

### 登录设备分类和清理边界

登录设备列表只描述 Telegram 远端当前授权事实，不替代授权资产表。“活跃授权”表示授权尚未被撤销，不表示 client 正在连接；因此休眠 MY standby_2 仍应在列表中。设备固定分为四类：

- 我方当前设备：唯一非零 hash 精确匹配当前 primary/standby_1/standby_2 授权资产。
- 我方历史设备：唯一非零 hash 精确匹配 candidate/retained/repair/invalid/unknown 且未有撤销 readback 的我方授权资产。
- 非我方设备：hash 非零且不匹配任何未撤销我方授权资产；包括同 `api_id` 额外登录、官方手机/桌面/Web 和未知 API 客户端。
- 待识别设备：hash 为零/缺失、匹配歧义、我方授权缺失非零 hash、或远端设备集读取不完整。

账号详情“活跃授权设备”视图必须展示四类设备、槽位/区域、Developer App、设备/App 元数据、创建/活跃时间、脱敏 IP/国家、归属原因和 observation 时间，不返回 hash 或完整 IP。顶部展示 `remote_active_total/platform_current/platform_retained/external/unresolved/as_of`，并提供“刷新设备”和“一键清理非我方设备”。

普通运营账号的一键清理目标是冻结快照中的全部“非我方设备”：

- 必须保留所有我方当前/历史设备；不强制保留官方手机、桌面或 Web 锚点。
- 未登记为平台授权资产的官方手机、桌面、Web 和历史人工登录也属于非我方设备；一期不提供人工保留白名单，需要继续使用人工客户端的账号不得执行一键清理。
- 创建批次只根据数据库中的 current SV 授权事实与 `telegram_login_at` 分类；严格 `server_now > telegram_login_at + 48h` 才进入 worker，不足、恰好 48 小时或缺失均直接 skipped。
- worker 开始处理账号时使用固定 current SV executor 读取 exact set，并冻结 `snapshot_digest/protected_manifest_version/slot_generations/executor_fact_version/external target hash digests`。任一待识别设备、我方授权 hash 缺失/歧义、固定 executor 不可用或授权 mutation 正在执行时，仅当前账号失败并展示精确原因。
- worker 逐个调用 `account.resetAuthorization(hash)`，不调用 reset-all。RPC 结果 unknown 时只读取 exact set 对账，不重复撤销同一 hash；设备列表读取超时或失败仅使当前账号失败，批次继续。
- 清理成功的唯一口径是：冻结 targets 全部从新鲜 Telegram exact set 消失，所有 protected hashes 仍存在，且没有新增 external/unresolved。执行中新增 external 不自动加入本次目标，本次记部分失败并要求运营重新提交新批次；远端 RPC 返回成功不能单独结案。

接码专用账号禁止一键清理非平台设备：

- 设备列表和非平台设备数量照常展示。
- 清理按钮禁用并说明“接码专用账号禁止自动清理其他登录设备”。
- 接码专用账号的设备异常只作为风险和诊断，不创建设备清理批次项。

如果账号没有备用授权，账号中心只提示恢复风险，不阻塞当前账号继续使用。
- 如果已通过 48 小时本地门槛但 Telegram 仍返回 FRESH 拒绝，当前账号标记 `failed/telegram_fresh_reset_rejected`；不进入等待状态，不记录自动重试时间，不影响批次其他账号。

一键清理非我方设备的确认弹窗必须展示：

- 确认前展示已选账号数、48 小时跳过规则和操作原因，不调用全量资格接口；确认后的创建响应与批次结果展示 `requested_count/eligible_count/skipped_count` 和 `skipped_reason_counts`。跳过原因至少覆盖登录时间未严格超过 48 小时、登录时间缺失、current SV 授权不可用和账号策略禁止清理。
- 最近一次 observation 的我方当前/历史、非我方和待识别数量仅作为参考，并明确最终保护集与清理目标以 worker 执行开始时的 exact set 为准。
- 固定提示“未登记的平台外手机、桌面、Web 与人工登录会在 worker 执行时作为非平台设备退出”。
- 操作原因输入框和一次确认按钮；确认只为 eligible 账号创建异步执行项，不同步刷新所有账号设备。
- 执行详情展示每个账号实际冻结的保护数量、目标数量、失败原因及 exact-set readback；审计保存前后设备计数、snapshot/protected manifest digest 和 target hash digests，不保存 hash 明文。

### 批量动作入口

账号批量动作必须采用“先点动作，再选账号”的流程，不能要求运营人员先在账号列表里找到勾选入口才能开始。账号列表可以保留勾选列，但勾选只是快速带入，不是创建批次的前置条件。

| 按钮 | 启用条件 | 行为 |
| --- | --- | --- |
| 资料初始化 | 有 `accounts.profile.batch_update` 权限 | 打开批量资料初始化抽屉；如果列表已有勾选账号则预填，未勾选则进入选择账号步骤 |
| 设置二步密码 | 有 `accounts.security.batch` 权限 | 打开批量设置二步密码抽屉，只包含 `set_two_fa`；如果列表已有勾选账号则预填 |
| 清理登录设备 | 有 `accounts.security.batch` 权限 | 打开批量清理登录设备抽屉，只包含 `cleanup_devices`；如果列表已有勾选账号则预填 |
| 补齐备用 session | 有 `accounts.security.session_manage` 权限 | 打开备用 session 补齐抽屉，只包含 `provision_standby_session` 或 `self_heal_session`；如果列表已有勾选账号则预填 |
| 同步安全状态 | 有 `accounts.security.read` 权限 | 打开同步安全状态抽屉或单账号详情动作；只同步安全事实，不创建加固批次 |
| 清空选择 | 当前列表已有勾选账号 | 清空当前列表勾选，不影响抽屉内已确认的选择 |

批量入口必须直接可点击：没有预先勾选账号时，也要打开抽屉并进入“选择账号”；不能因为表格未勾选而禁用主入口。已有勾选只作为初始选择。

批量动作抽屉第一步固定为“选择账号”：

- 可以先选择账号组，再在组内慢慢筛选和勾选。
- 支持按状态、账号健康分、在线状态、2FA 状态、是否有头像、资料完整度、需重新资料初始化、安全状态、最近批次状态、备用 session 缺口和可激活恢复状态筛选。
- 支持搜索手机号、昵称、username、账号 ID。
- 支持当前页勾选、跨页累计选择、全组选入后再剔除。
- 账号列表和批量抽屉必须基于完整账号集合做 AntD Table 分页，不得只展示 `/api/tg-accounts` 默认第一页；分页接口需要返回可观测的总数元数据，前端如果仍采用本地分页，必须分页拉齐当前分组全部账号。
- 账号列表和批量抽屉必须支持至少 100 条 / 页，并提供“选择当前筛选前 100 个”“追加当前筛选全部”“只看资料待初始化”“选择资料待初始化”“只看需重新资料初始化”“选择需重新资料初始化”等快捷动作，避免运营人员逐页逐个勾选。
- “需重新资料初始化”用于覆盖已经执行过资料初始化但仍需重新触发的账号，包括最近资料初始化失败、资料初始化被跳过、预览校验失败需重抽、username 候选冲突、头像缓存失败、资料被人工标记为需重做、平台展示名或 TG 姓名仍是占位名等场景；它不同于从未补齐资料的“资料待初始化”。
- 账号列表摘要卡和批量抽屉快捷按钮必须能把“需重新资料初始化”账号直接带入资料初始化抽屉，运营人员确认筛选结果后即可 AI 生成预览或重抽失败项，不需要先手工搜索再逐个勾选。
- 账号列表摘要卡和批量抽屉必须提供“standby_1 session 缺失”“standby_2 session 缺失”“备用 session 未登录”“健康备用 session 不足 2 个”“可从备用 session 激活恢复”等快捷筛选。备用 session 相关筛选用于补齐账号授权资产或触发备用 session 恢复，不触发资料初始化批次；点击后进入授权资产 / 备用登录处理入口。
- 支持区间选择，例如 Shift 点击或等效的“选择这一段”，方便处理分页条数较多时的连续账号。
- 如果从账号列表带入已勾选账号，抽屉内必须继续允许增删账号。
- 抽屉标题和步骤条必须明确当前动作，例如“资料初始化 / 选择账号”“设置二步密码 / 选择账号”，避免运营人员不知道下一步。
- 备用 session 补齐抽屉必须支持按槽位策略选择“自动补齐缺失槽位 / 仅 standby_1 / 仅 standby_2”，展示验证码读取能力、平台托管 2FA、开发者应用健康、代理健康和新登录限制；确认后创建 `account_standby_session_provision` 系统任务投影。
- 清理登录设备抽屉不设置预检页。确认前只展示已选账号数、48 小时跳过规则、最近 observation 的四类计数仅供参考和操作原因；用户确认后的创建事务返回 eligible/skipped 汇总。接码专用账号直接以 `account_cleanup_forbidden` 跳过；worker 执行时发现任一平台授权 hash 无法确认，仅将当前账号标记失败。
- 设置二步密码抽屉必须区分未设置、已设置且平台知道旧密码、已设置但旧密码未知三种状态；旧密码未知的账号进入人工处理，不显示为自动可执行。
- 平台托管 2FA 设置面板必须放在账号安全配置或账号详情安全区，密码设置 / 轮换不默认回显旧密码；需要人工登录时，具备 `accounts.security.credential_manage` 的人员可以按需查看并复制当前托管密码，查看、复制、导出和自动登录使用都写审计，但查看动作不要求填写原因。

确认创建批次不再要求输入固定文案“确认加固”。改为二次确认弹窗：

- 资料初始化、2FA 和备用补齐弹窗按各自预检展示账号总数、可执行数、跳过数、不可执行数和主要风险提示。设备清理弹窗只展示已选账号数与跳过规则，实际 eligible/skipped 数量由确认后的创建响应展示。
- 审计需要原因时，在弹窗中填写“操作原因”；不再输入固定确认短语。
- 点击“确认创建批次”后才创建批次。

“同步安全状态”命名含义：

- 重新读取账号当前 Telegram 安全事实，包括平台可信设备、外部登录设备、2FA 状态、新登录限制和最近安全检查结果。
- 它不是处理动作，不清理设备、不设置密码、不修改资料，不创建安全加固批次。
- 可用于单账号详情，也可作为低频批量工具；批量同步同样先进入选择账号步骤。

### 账号中心缺口补齐契约

本节作为账号中心升级的补齐主契约。后续前端、后端、worker 和 QA 必须围绕以下事实源、状态机和边界实现，不得把技术状态、空统计或后台错误包装成“已处理”。

#### 接码专用身份契约

- 系统必须固定一个接码专用 `AccountPool`，同时为账号写入 `account_identity=code_receiver` 或等价身份投影。
- 进入接码专用分组：先写账号身份，再写分组成员；任一步失败都要回滚或进入“接码身份待修复”，不得出现已入分组但仍可参与任务的中间状态。
- 移出接码专用分组：必须二次确认并写审计；移出后只恢复为“普通账号候选待校验”，不得自动加入任务候选，仍需通过授权、设备、容量和风控预检。
- 任务创建、消息发送、AI 活跃群、监听、目标准入、Planner、Dispatcher、Listener、Recovery 必须读取账号身份字段排除接码账号；分组关系只作为 UI 管理和批量筛选。
- 接码专用账号禁止一键清理非平台设备，禁止进入清理批次，禁止被任务候选 API 返回。

#### 远端授权 hash 归属契约

- 平台保存三槽位授权资产时，必须固化 `developer_app_id + api_id`、AuthKey blind index、唯一非零 `telegram_authorization_hash_ciphertext` 和槽位 generation；三个当前槽位的 App/AuthKey/hash 两两不同。
- 读取 Telegram 远端授权后，逐条以非零 hash 精确匹配同账号的未撤销我方授权资产；匹配当前代次为“我方当前设备”，匹配候选/保留/修复/未知代次为“我方历史设备”。
- hash 非零但未命中任何我方资产时为“非我方设备”，即使 `api_id` 命中三槽之一也不改变结论。
- hash 为零/缺失、匹配歧义、我方资产 hash 不完整或远端读取失败时为“待识别设备”，整个账号不得进入当次一键清理。
- 设备 observation 与清理结果必须保存每条远端授权的 hash 密文/指纹、`api_id`、分类、匹配授权/槽位/代次/事实版本、是否撤销、失败原因和读取时间；页面不返回 hash 明文。

#### 三槽位状态推导公式

每个账号以 `primary / standby_1 / standby_2` 三槽位为唯一授权资产模型。槽位展示状态由以下输入推导，不允许前端自行猜测：

| 推导优先级 | 条件 | 槽位状态 | 页面口径 |
| --- | --- | --- | --- |
| 1 | `disabled_at` 不为空或管理员冻结 | `disabled` | 已停用 |
| 2 | 授权资产记录不存在 | `missing` | 槽位缺失 |
| 3 | SV 槽位 `session_ciphertext` 缺失/不可解密，或 MY standby_2 的 wake bundle/receipt 缺失/本地不可解封 | `manual_required` | 需人工重新登录或修复 MY 包 |
| 4 | 当前存在该槽位刷新任务且未结束 | `refreshing` | 正在刷新 |
| 5 | 当前登录流等待 code | `waiting_code` | 等待验证码 |
| 6 | 当前登录流等待 operation-scoped QR | `waiting_qr` | 等待扫码 |
| 7 | 当前登录流等待 2FA | `waiting_2fa` | 等待二步密码 |
| 8 | 最近健康检查成功且 session 可执行基础 Telegram 探测 | `healthy` | 健康 |
| 9 | Telegram 返回授权失效、会话失效、账号被登出或连续健康检查失败 | `down` | 掉线 |
| 10 | 代理失败、远端读取失败或健康检查超时但 session 未确认失效 | `unknown` | 待刷新 |

账号聚合状态按槽位集合推导：

| 聚合状态 | 推导公式 | 页面口径 |
| --- | --- | --- |
| `all_healthy` | 三槽位均为 `healthy` | 三槽位健康 |
| `partial_ready` | 至少 1 个 `healthy` 且至少 1 个 `missing/unknown/down/manual_required` | 部分可用 |
| `recoverable` | 至少 1 个 `healthy` 且至少 1 个 `down/manual_required/missing` 可发起刷新 | 可用健康槽位刷新 |
| `all_down` | 三槽位均不是 `healthy`，且账号从未成功登录过 | 全部掉线 |
| `previously_logged_in_all_down` | 三槽位均不是 `healthy`，且账号存在历史登录成功记录 | 曾登录账号全部掉线 |
| `identity_blocked` | 账号身份为 `code_receiver` | 接码专用，不参与任务 |

`unknown` 不得当作健康槽位参与任务，也不得当作全部掉线的自动恢复条件；它只能触发同步安全状态、健康检查或人工确认。

#### 互相救援链路契约

健康槽位救援掉线槽位必须按固定链路执行：

```text
选择目标掉线槽位
  -> 获取账号级授权控制 lease，冻结 account_id + target_logical_slot + target_generation
  -> 选择一个 healthy authorization_id/fact_version 作为 code reader
  -> 启动目标槽位登录刷新 flow
  -> 通过冻结的 code reader 读取 Telegram 官方服务 code
  -> 提交 code；如需 2FA，使用平台托管 2FA 或进入 waiting_2fa
  -> 保存目标槽位新 session
  -> 立即健康检查目标槽位
  -> 成功后释放锁并改为 healthy；失败则记录 failure_type / next_retry_at
```

救援链路约束：

- 同一账号同一目标槽位同时只能有一个救援 flow。
- code 读取只允许使用 `healthy` 槽位；`unknown/down/manual_required` 槽位不得作为读取来源。
- code 选择默认取 Telegram 官方服务最新未过期 code；如果多条 code 同时存在，展示候选并默认使用最新一条，同时记录 source_message_id。
- code 过期、未找到 code、远端读取失败、代理失败、Telegram 限制、2FA 未托管都必须进入明确失败状态。
- 三槽位全部不是 `healthy` 时不得创建自动救援 flow，只能进入人工重新登录 / 扫码 / 手动验证码。
- 救援成功不得删除旧授权资产；旧 session 作为历史版本或失败快照留痕，便于审计和回滚排障。

#### TG 官方验证码提取契约

- 点击“提取验证码”必须真实读取 Telegram 官方服务消息，并识别官方登录 code；读取路径需要记录使用的授权槽位、读取时间和源消息时间。
- 成功时直接展示 code、来源消息时间、有效期、读取槽位、源消息摘要和是否已过期；完整 code 查看必须受权限控制并写审计。
- 失败时必须明确区分：无健康授权槽位、未找到 Telegram 官方服务消息、消息中未识别 code、code 已过期、远端读取失败、Telegram 限制、代理失败或 2FA 阻断。
- 不允许在未读取到官方消息时返回空 code、假成功或“稍后再试”式静默兜底。
- 接码专用账号优先服务于验证码读取；普通账号也可以通过健康槽位读取自己的官方验证码，用于主备互相刷新。

#### 可用性与容量解释契约

账号中心不得直接把 `account_cooldown`、`安全待刷新` 或内部哨兵容量值（例如 `99`）这类技术状态裸露给运营人员，必须转成可解释状态：

- `account_cooldown` 展示为“账号冷却中”，并给出冷却来源、恢复时间、剩余等待、最近触发动作和是否影响任务创建。
- “安全待刷新”必须拆分为具体原因：登录设备快照过期、授权资产健康过期、可用性汇总过期、Telegram 远端读取失败、代理读取失败或安全批次结果待回写。
- 容量必须展示小时剩余、日剩余、已占用来源、冷却占用、FloodWait / SlowMode 影响、汇总时间和 stale 标记；如果内部使用 99 作为上限或哨兵值，页面必须解释为业务含义，不得只显示“99”。
- 创建阶段不做账号容量预检；`account_runtime_summary` 只作为列表和可选诊断摘要。启动器、Planner 与 Dispatcher 必须实时重算账号能力，不能让摘要替代执行前校验。

#### 待处理执行闭环契约

账号已经由管理员手动进入目标群聊、频道讨论区或具备发送权限后，系统必须支持对待处理 action 做“重查待处理”。该能力是修复闭环，不是万能重试按钮：

```text
重查待处理
  -> revalidate_account：账号身份、授权槽位、在线状态、容量、风控
  -> revalidate_target：membership、can_send、SlowMode、讨论区 / 评论区能力
  -> resolve_blockers：关闭已满足的验证、准入、人工处理阻塞
  -> rebuild_ready_pool：更新 task_ready_accounts
  -> requeue_actions：只重排原本因这些阻塞停住的 action
  -> report_remaining_blockers：输出仍无法继续的具体原因
```

闭环修复规则：

- 只处理当前账号和当前目标相关的 pending / retryable_failed / blocked action，不扫描全库重排。
- 如果 action 已经 success、cancelled、deleted 或已被新的 action 替代，不得重新排队。
- 重查不得重复扣容量；容量只在 action 真正进入可执行 claim 时占用。
- 重查必须保留原失败事实，并新增一条重查轨迹，不覆盖历史失败原因。
- 如果管理员已让账号入群且复检可发言，系统必须关闭对应准入阻塞并让任务继续，而不是继续显示旧的“待处理消息未处理”。
- 如果仍无法继续，页面必须按账号、目标、容量、规则、AI、Dispatcher、任务状态分组展示剩余阻塞原因。

#### 执行记录统一事实源

账号详情里的发送记录、评论记录、回复记录和互动记录必须聚合旧版手动消息事实和新版 Task/Action 事实：

- 旧事实源至少包含 `message_tasks`、`message_task_attempts`。
- 新事实源至少包含 `tasks`、`actions`、`execution_attempts`，并按账号维度聚合发言、评论、引用回复、频道浏览、点赞、投票、AI 活跃群发言和准入动作。
- 记录必须展示任务来源、目标、动作类型、状态、远端 message id、失败原因、尝试次数和发生时间。
- 同一远端消息或同一 action 不得重复计数；如果旧表与新表同时存在迁移期投影，必须有去重键。
- 页面不能因为只读取旧手动消息表而把发送记录全部展示为 0 条。

统一动作字典：

| 展示动作 | 新版 action_type | 旧事实源映射 |
| --- | --- | --- |
| 群发言 | `group_message`、`ai_group_message` | `message_tasks.target_type=group` |
| 频道评论 | `channel_comment` | `message_tasks.target_type=channel_comment` |
| 引用回复 | `reply_message`、`channel_reply`、`group_reply` | 旧表中带 `reply_to_message_id` 的发送记录 |
| 频道浏览 | `channel_view` | 无旧表则只读新版 action |
| 频道点赞 | `channel_like`、`channel_reaction` | 无旧表则只读新版 action |
| 投票 / 互动 | `channel_vote`、`poll_vote`、`interaction` | 无旧表则只读新版 action |
| 目标准入 | `target_join`、`target_follow`、`target_admission_retry` | 历史 operation task / membership attempt |
| 资料维护 | `account_profile_init` | `tg_account_security_batch_items.profile_status` |
| 授权维护 | `account_standby_session_provision`、`authorization_refresh` | `tg_account_security_batch_items.standby_session_status` |
| 设备清理 | `account_device_cleanup` | `tg_account_security_batch_items.device_cleanup_status` |

统一状态字典：`pending`、`blocked`、`queued`、`executing`、`success`、`retryable_failed`、`failed`、`unknown_after_send`、`skipped`、`cancelled`。旧表状态必须映射到该字典后再展示。

### 单行按钮

| 状态 | 按钮 |
| --- | --- |
| 待登录 / 等待验证码 / 等待扫码 / 等待2FA / 需重新登录 / Session失效 / 异常 | 完成登录 / 继续登录、移除 |
| 在线 | 详情、提取验证码、移动分组、同步资产、同步安全状态 |
| 接码专用账号 | 详情、提取验证码、移动分组、同步资产、同步安全状态、刷新授权资产；禁用清理登录设备和任务相关入口 |
| 三槽位全部掉线 | 详情、人工重新登录 / 扫码 / 手动验证码、同步资产；禁用自动刷新和一键清理 |
| 有待处理执行 | 详情、重查待处理、同步资产、同步安全状态 |
| 其他可查看状态 | 详情、移动分组 |

单行按钮必须受状态和权限双重控制。前端隐藏按钮不能替代后端校验；高风险动作包括移除账号、查看完整验证码、导出完整手机号、清理登录设备、设置 / 重置 2FA，必须写审计。

### 账号登录流程

```text
新增账号
  -> 选择或自动分配开发者应用
  -> 输入手机号
  -> 选择验证码登录或二维码登录
  -> 等待 Telegram 返回 code / QR
  -> 输入验证码或扫码
  -> 如需 2FA，输入二步密码
  -> 写入主授权 session；迁移期兼容写入 tg_accounts.session_ciphertext
  -> 账号状态变为 在线
  -> 同步群、频道、联系人、资料
  -> 触发备用 session 自动补齐：standby_1、standby_2
```

新增入口命中同租户同手机号的未删除账号时，不得创建重复账号或以“账号已在分组”结束流程。创建接口返回带原 `account_id` 的固定结构化 409，前端读取原账号详情后：可重新授权状态进入既有验证码 / QR 登录；在线或其他不可重登状态只打开详情。该响应本身不得发送验证码、移动分组或修改原账号资料。

账号登录、已有账号重登和登录后的可选分组迁移必须按 `docs/03-feature-designs/account-login-group-navigation-recovery-prd.md` 执行。主登录和备用授权 flow 都必须以精确 `flow_id + flow_scope + flow_version + request_seq` 推进；不得只按 `account_id` 查最新 flow，也不得把数据库中未过期验证码记录等同于当前进程可提交。验证码、QR、2FA、重发、取消和迟到响应必须由持久 flow 版本 fence 收口，失败返回类型化错误和 trace，不允许裸 `Internal Server Error`。

验证码登录的平台提交窗口固定为 300 秒，该窗口不是 Telegram 官方绝对过期承诺。普通 start 只能恢复当前、具备 durable challenge binding 的 waiting-code / waiting-2FA / expired flow；迁移前 `waiting-code` 且 `challenge_sent_at` 为空的旧 flow 不可恢复，用户点击“发送验证码”时必须 supersede 它并建立新 challenge。其余 expired flow 仍只有用户显式触发 resend 才能携带旧 flow/version、supersede 旧 challenge 并请求新 code。每个 code challenge 的临时 StringSession 与 `phone_code_hash` 必须加密绑定到唯一 flow；verify 只能使用该 flow 的同一配对。主登录当前默认直连，账号绑定的代理不自动成为主登录 egress；只有备用授权和其他显式选择代理的路径才使用绑定代理。`PhoneCodeInvalidError` 保持当前 flow 可重试，`PhoneCodeExpiredError` 或平台超时进入 expired，二者不得合并成“验证码错误或已失效”。

登录成功结果必须拆为正交投影：`authorization_status` 表示 Telegram 授权是否已持久化，`post_login_sync_status` 表示资料 / 群 / 联系人同步，`pool_transition_status` 表示登录成功后的显式分组迁移。Telegram 授权成功后，后置同步或分组迁移失败不能把登录响应改写成失败或 500；用户为本次登录输入的 2FA 密码不得被隐式托管、保存或轮换。

批量自动登号的当前合同以 `docs/03-feature-designs/account-batch-auto-login-prd.md` 为准：必须 precheck 后显式确认，worker 对已有账号执行新鲜、直连、权威授权探测，不能以数据库 ACTIVE/session 推断在线；新账号先完成接码 baseline 再创建。每行输入的接码 UUID 必须与最终 `account_id` 建立持久绑定：账号列表/详情显示独立、不会被资料初始化覆盖的脱敏接码备注，完整 UUID 加密保存且只允许同租户有 `accounts.code_source_credentials.read` 权限的用户填写原因后显式查看；同一 UUID 对应多账号或替换既有绑定必须阻断并二次确认，禁止把完整值放进 `display_name`、普通列表、导出、日志或提醒。批内按行串行但每轮只推进一个 phase，单行失败或 300 秒仍无法判定时跳至下一行；远程未知记为 unresolved，由独立 reconciler 在 24 小时内收口并通过 correction 提醒修正结果，禁止自动重发/重验。所有成功或已授权账号最终进入所选目标分组；失败/未解行可刷新接码地址后按版本 fence 重试。实现必须具备版本化 fingerprint alias 去重、跨租户/批次公平调度、全 worker 持久限速、`off/reconcile_only/enabled` 后端 mode gate、精确 flow owner/version 和 generation CAS。code/2FA 明文只在 worker 内存短暂使用且禁止进入托管路径；批次事实与 initial/correction 持久提醒原子写入。当前状态为 `product_design_complete / local_implemented / targeted_qa_passed / not_released / production_unproven`，默认 mode=`off`；不得以本地测试、CI、分支合并、部署或 worker health 代替批量登录 E4。

备用授权登录流程与主授权相同，但入口在账号详情“授权资产”Tab。新增备用授权时按 v2.21 固定使用 App B/SV standby_1 或 App C/MY standby_2；登录成功后只写入授权资产。普通人工切主仍要求明确操作和二次确认；v2.21 primary 权威失败后的自动 `local_activate` 是唯一例外，必须走冻结、Gateway drain、原子 CAS 和模块新代次重建状态机。

主授权登录完成后，系统默认进入备用 session 自动补齐流程：

```text
primary session 登录成功
  -> 读取或生成平台托管 2FA 密码策略
  -> 校验并冻结 App B/SV standby_1、App C/MY standby_2 的 assignment/version 与对应出口
  -> 通过账号官方验证码读取能力获取登录验证码
  -> 如 Telegram 要求 2FA，使用平台加密托管的 2FA 密码
  -> 写入 standby_1 session / standby_2 session
  -> 健康检查成功后计入健康备用 session
```

首次登录的二维码和登录流水只作为审计、排障和授权资产绑定依据；不得把历史 QR 当成长期可复用登录凭证。备用 session 自动补齐失败时，账号进入“备用 session 缺口”筛选，并展示具体原因：验证码不可读取、2FA 未托管、需要人工 QR、新登录限制、开发者应用异常、代理异常或 Telegram 返回限制。

登录初始化失败不得只返回 500。后端必须写入失败登录流水，包含 `failure_type`、`failure_detail`、`trace_id`，账号状态进入“异常”，并写审计；接口返回结构化 400，前端展示可追踪错误，不吞掉真实异常。

管理后台登录态过期不能被混同为 TG 账号登录失败。任意账号接入、登录启动、验证码提交、二维码确认或 2FA 提交流程中，如果后端返回管理端认证 401，例如 `token expired`、`permission version expired`、`invalid token` 或缺失 bearer token，前端必须立即清理本地管理 token、关闭当前业务弹窗、回到后台登录态，并提示“登录已过期，请重新登录。”；不得继续展示“操作失败 token expired”，也不得把此类错误写入 TG 账号失败流水。

### 资料初始化流程

```text
点击 资料初始化
  -> 选择账号：按账号组 / 筛选 / 搜索 / 跨页勾选 / 区间选择
  -> 配置生成方式、语言、画像、禁用词、custom_prompt、头像策略
  -> 预检 / AI 生成预览
  -> 不触发实时登录设备扫描，只读取已有安全快照和账号在线状态
  -> 一次 AI 请求生成整批昵称、TG 姓名、简介和 username 候选
  -> AI 超时、mock、无健康供应商或返回不足时使用本地随机网名兜底并展示 warning
  -> 勾选头像但没有可用头像来源时只跳过头像，不阻塞昵称、简介和 username
  -> 预览表单行可编辑
  -> 逐行确认昵称、TG 姓名、username、简介、头像策略、生成来源和 warning
  -> 二次确认弹窗展示账号数量、可执行 / 跳过 / 不可执行数量和操作原因
  -> 创建批次
  -> worker 按账号执行 profile -> username -> avatar
  -> 回写账号资料、批次项和审计
```

#### 资料初始化命名口径

- 默认昵称是自然、随机、生活化的 TG 网名，不是正式姓名。
- 默认提示词示例包括“锅巴洋芋、蕉太狼、早睡失败、小熊便利店、不吃香菜、月亮打烊”。
- 同一批生成必须做差异控制：昵称长度、命名类型、简介字数、简介句式和 username 前缀都不能明显套同一个模板；AI 输出不足或过于模式化时使用本地兜底并在预览里展示 warning。
- 前端必须提供“命名风格提示”输入框，对应 `profile_strategy.custom_prompt`。
- `display_name` 和 `first_name` 可以直接使用同一个网名；`last_name` 可以为空，不强行拆成中文姓氏和名字。
- 昵称唯一性不能只做“同一批内去重”。所有未删除普通运营账号的稳定 `display_name` 必须在租户内按 NFKC、去零宽字符、空白折叠、trim、casefold 后唯一；新增账号、登录后自动资料初始化、批量资料初始化和手工资料修改必须复用数据库名称 claim，不能依赖随机概率或进程内集合。
- 名称 claim 采用历史保留策略：账号改名后旧昵称不立即分配给其他账号。两个并发批次或资料修改争用同一昵称时只能一个成功，另一方明确返回 `display_name_conflict`；Telegram 修改前必须回验 claim 仍归当前账号。
- 本地随机命名不得继续按 `account_id` 对有限名字池取模，也不得默认使用规律序号；应使用持久化批次随机种子、多类词片和多种模板生成，并过滤当前账号、历史 claim 和本批已用名字。候选耗尽时显式失败，不能回退为“用户 + account_id”。
- 存量重复昵称治理必须只选择重复组内 keeper 之外的账号，使用只读 preview manifest、旧值/版本/SHA guard 和账号安全批次执行；历史“按账号 ID 改一半”的工作流不得作为重复治理入口。
- 昵称 / TG 姓名生成结果必须同时更新平台展示名和 Telegram 远端 `first_name` / `last_name`；平台展示名是“新托管账号”“托管账号”或导入占位名等可替换名称时，即使未开启“覆盖已有资料”，也要同步修改 TG 姓名。
- `username_candidates` 必须遵守 Telegram username 规则，只能包含英文、数字和下划线，不能包含中文。
- 批量预览必须一次请求 AI 生成整批账号资料，不能按账号逐个请求模型。
- 资料预览超时时间按账号数量伸缩，避免 50 个账号以上批量生成时被普通接口超时提前打断。
- 预览行必须展示生成来源和异常原因，例如 AI 成功、AI 超时、本地兜底、头像来源缺失、username 不可用或账号离线。
- 确认创建批次必须复用本次预览结果落库，不能再次触发整批 AI 生成或头像随机预览，避免确认阶段超时和预览内容漂移。
- 账号未在线、缺少可用 session、预览需要人工修正或校验阻塞时，系统自动跳过该账号并记录跳过原因；不能阻塞同批次其他可执行账号。
- 资料初始化、设置二步密码和清理登录设备的确认接口只负责创建后台批次；生产环境必须有独立账号安全 worker drain 该批次，前端文案必须说明“已提交后台执行”，不能把创建成功描述成资料已更新成功。
- 确认执行后必须能在批次详情追踪每个账号的 profile、username、avatar 动作结果。
- 头像素材池必须来自素材库或账号资料素材分组；没有可用头像时只跳过头像动作，不阻塞昵称、简介和 username。
- 外部补充头像必须由来源 manifest 驱动，只接收许可清晰的非真人图片；素材需保存来源 URL、许可、署名、SHA-256 和感知哈希，经过素材审核与 TG cache ready 后才能分配。不得直接抓取社交平台头像、搜索缩略图、真人照片或版权不明图片。
- 新注册头像优先从最低使用次数的一组已审核素材中随机选择；存量昵称去重默认不覆盖已有头像，只为缺头像账号补齐。头像多样性是质量约束，昵称唯一性是数据库硬约束。
- 选择“随机素材池”时，系统必须能从素材中心已审核的上传图片 / 头像包中自动分配头像来源，不要求运营人员手工填写 `material:ID`。
- 前端选择“随机头像包”后只展示自动分配说明；手工 `material:ID` / 路径输入只能作为顺序分配的可选覆盖项，不能成为随机头像包主流程。
- username 冲突按候选顺序重试，默认最多 3 个候选；候选全部失败时该账号 `username_status=failed`，资料和头像动作仍可独立成功。
- 批次详情必须支持只重试失败项、只重抽失败项、保留成功项和导出失败原因。
- 操作手册必须用用户可理解语言说明资料初始化、AI 兜底、头像跳过、username 冲突和失败重试。

### 设置二步密码流程

```text
点击 设置二步密码
  -> 选择账号：按账号组 / 筛选 / 搜索 / 跨页勾选 / 区间选择
  -> 抽屉动作范围只显示 设置二步验证
  -> 不展示 AI 命名、头像、username 或资料覆盖配置
  -> 预检账号在线、session 和已有 2FA 状态
  -> 已设置二步验证的账号标记跳过或 warning
  -> 二次确认弹窗展示账号数量、可执行 / 跳过 / 不可执行数量和操作原因
  -> 创建批次
  -> worker 为未设置账号设置平台托管 2FA 密码
  -> 已有 2FA 且平台掌握旧密码时，可替换为平台托管 2FA 密码
  -> 写入安全快照、批次项、失败原因和审计
```

2FA 托管口径：

- 平台在系统设置中配置租户级固定托管 2FA 密码，便于后续由运营手动执行批量动作，把线上账号逐步统一到固定二步验证；固定密码必须加密保存，只允许首次设置，不随账号详情默认返回，不在列表或普通详情字段完整回显。
- 如果账号没有 2FA，设置二步密码批次按平台托管策略设置并记录加密凭据。
- 如果账号已有 2FA 且平台已保存旧密码，允许在二次确认后替换为系统设置中的固定托管 2FA 密码，并写入旧密码使用、固定密码设置和操作者审计。
- 如果账号已有 2FA 但平台不知道旧密码，不能伪装替换成功，必须标记“需旧密码 / 人工处理”。
- 平台托管 2FA 密码只能由授权人员按需查看、复制或导出；账号详情的“托管 2FA”面板通过独立 reveal 接口返回明文，查看、复制、导出和使用都必须写审计，查看动作不要求填写原因。

### 清理登录设备流程

```text
点击 清理登录设备
  -> 选择账号：按账号组 / 筛选 / 搜索 / 跨页勾选 / 区间选择
  -> 抽屉动作范围只显示 清理外部设备
  -> 不展示 AI 命名、头像、username 或二步密码配置
  -> 一次确认弹窗只展示已选账号数、48 小时跳过规则、最近 observation 仅供参考和操作原因
  -> 用户确认后调用创建接口；不先调用全量资格接口
  -> 创建事务只读数据库 current SV authorization 与 telegram_login_at，不调用 Telegram
  -> 严格 server_now > telegram_login_at + 48h 且账号策略允许时为 eligible
  -> 其他账号直接 skipped；同一事务创建批次并保存账号级结果
  -> 创建响应返回 requested/eligible/skipped 数量和 skipped_reason_counts；skipped 不进入 worker
  -> worker 对每个 eligible 账号独立读取 Telegram exact set
  -> 以我方未撤销授权资产的唯一非零 hash 分类并冻结保护集和全部非我方非零 hashes
  -> 待识别、我方 hash 不完整、executor 不可用或读取超时仅使当前账号失败，其他账号继续
  -> worker 只按执行开始时冻结的 hashes 逐个 resetAuthorization，不使用 reset-all
  -> 清理后重新读取 exact set，确认目标全部消失且我方保护集完整
  -> 写入安全快照、批次项、失败原因和审计
```

清理登录设备选择器必须支持筛选“未做过登录设备清理”“外部设备未清理”“最近清理失败”“current SV 登录时间未严格超过 48 小时或缺失”的账号，支持当前筛选全选和跨页累计选择。确认前不得为全部已选账号发起额外资格请求；批次创建事务直接完成本地分类，跳过账号不自动排期，也不创建等待项。必须满足 `requested_count = eligible_count + skipped_count` 且 `skipped_count = sum(skipped_reason_counts)`；运营后续重新提交时按当时数据库登录时间重新判断。已创建批次投影到任务中心，展示请求、可执行、跳过、执行中、成功、失败、unknown、跳过原因汇总、最近失败原因和账号级结果。

### 动作边界

| 入口 | 允许动作 | 禁止混入 |
| --- | --- | --- |
| 资料初始化 | `update_profile`、`update_username`、`update_avatar` | `set_two_fa`、`cleanup_devices` |
| 设置二步密码 | `set_two_fa` | 资料、头像、username、设备清理 |
| 清理登录设备 | `cleanup_devices` | 资料、头像、username、二步密码 |

### 账号状态

| 状态 | 进入条件 | 任务可用性 | 允许操作 |
| --- | --- | --- | --- |
| 在线 | session 可用，最近连接正常 | 可用，但仍要看容量、目标权限和风控 | 详情、提取验证码、移动分组、同步资产、同步安全状态、资料初始化、设置二步密码、清理登录设备 |
| 待登录 | 账号创建但未开始登录 | 不可用 | 开始登录、移除 |
| 等待验证码 | 已发起验证码登录 | 不可用 | 输入验证码、重新发送、取消登录 |
| 等待扫码 | 已发起二维码登录 | 不可用 | 查看二维码、刷新二维码、取消登录 |
| 等待2FA | 需要输入 Telegram 二步密码 | 不可用 | 输入 2FA、取消登录 |
| 需重新登录 | session 失效、凭证不可用或 2FA 失败 | 不可用 | 重新登录、查看失败原因、移除 |
| Session失效 | Telegram 权威侧已明确拒绝或撤销历史授权 | 不可用 | 继续登录、查看失败原因、移除 |
| 受限 / 疑似封禁 / 已封禁 | Telegram 限制发送或互动 | 不可用或只读 | 查看详情、同步限制、禁用、解除后复检 |
| 异常 | 代理、权限、同步、登录、目标能力或风控异常 | 按可用性读模型判断 | 查看详情、同步资产、同步安全状态、重新登录、跳转风控中心或代理处理 |
| 禁用 | 人工停用或移除 | 不可用 | 查看审计、恢复启用或移除 |

状态字段只表达账号登录 / 运行主状态；是否能参与某类任务由账号可用性读模型判断，不能只看 `status=在线`。

### 账号可用性读模型

账号中心必须提供账号级汇总可用性，供账号列表展示、任务中心预检、消息发送预检、运营中心影响摘要和风控中心处置使用。账号中心只展示事实和跳转，不在本页完成发送或风控处置。

| 字段 | 含义 |
| --- | --- |
| `send_available` | 是否可发送消息、AI 活跃群发言和转发目标群发送 |
| `listen_available` | 是否可作为监听账号读取源群 / 频道 |
| `join_available` | 是否可关注频道、加入群聊或重新执行准入 |
| `comment_available` | 是否可对频道帖子评论 / 回复 |
| `profile_available` | 是否可执行资料、头像、username 修改 |
| `verification_available` | 是否可读取 TG 官方验证码或处理验证问题 |
| `capacity_remaining` | 当前小时 / 日剩余容量和冷却状态 |
| `unavailable_reason` | 不可用原因：session、代理、风控、TG 限制、目标权限、容量、人工禁用 |
| `next_retry_at` | FloodWait、SlowMode、允许重试的账号安全动作或冷却结束时间；设备清理的 48 小时跳过和执行时 FRESH 失败不得写入此字段 |
| `summary_updated_at` | 汇总更新时间 |

账号授权资产应进入可用性摘要：

- 主授权不可用时，`unavailable_reason` 必须区分线路异常、session 失效、开发者应用异常和代理异常。
- 有健康备用授权时，账号中心展示“可切换备用”，任务调度可在实现后使用备用授权继续执行。
- 无备用授权时，只展示风险提示，不把账号降为不可用；真正不可用仍以主授权健康、代理、风控和 Telegram 返回为准。
- 外部官方客户端是否存在不参与平台可用性判断；平台恢复能力只看 `primary / standby_1 / standby_2` 授权资产和人工重新登录入口。

账号列表默认读取 `account_runtime_summary`、最新安全快照、最新批次摘要和账号基础表，不直接实时扫描执行明细、设备明细或批次项明细。账号详情打开后再按账号 ID 分页读取设备、批次项、执行记录和验证码。

`account_runtime_summary.failure_trend` 必须保留可下钻来源：近 24 小时执行状态计数、FloodWait / SlowMode 命中次数和原因、安全快照中的平台可信设备 / 2FA / 外部登录设备 / 资料状态、账号安全批次 `next_retry_at`、最近一次风控预检的 `decision`、`risk_level`、原因和建议动作。安全快照出现平台可信设备缺失、2FA 设置失败或待邮箱确认时，账号汇总可用性必须标记不可用；其他安全风险只作为降级/告警信号展示。

如果 `account_runtime_summary.summary_updated_at` 超过可接受窗口，账号列表必须展示“汇总可能延迟”标记和刷新入口；创建并启动在 Task 持久化后、后续启动、Planner 与 Dispatcher claim 前仍必须实时重新计算账号能力，不能只依赖过期汇总，也不能因摘要过期拒绝结构合法的创建。

### 批次中心

账号中心内置批次中心，不新增一级导航。批次中心展示资料初始化、设置 2FA、清理登录设备、备用 session 补齐 / 自愈四类账号批次。

| 区块 | 内容 |
| --- | --- |
| 批次列表 | 批次 ID、动作类型、状态、成功 / 失败 / 跳过 / 待重试、操作者、创建时间、trace_id |
| 批次详情 | 每个账号的 profile、username、avatar、2FA、设备清理状态和失败原因 |
| 失败重试 | 只重试失败项；允许按失败类型筛选，例如账号离线、username 冲突、头像缺失、新登录限制 |
| 审计 | 预检、确认、执行、跳过、失败、重试、取消和敏感配置变更 |

批次失败需要同时满足两类展示：

- 账号中心展示批次明细，告诉运营人员哪些账号失败、为什么失败、是否可重试。
- 如果失败影响目标执行、任务容量或运营效果，Metrics / Recovery 上卷为运营中心“目标 / 任务影响”；运营中心默认按目标展示影响和跳转入口，不展开账号管理式批次明细。

### 2FA 和敏感数据策略

- 手动设置 / 轮换 2FA 时使用系统设置中的租户级固定托管密码并加密保存，不再为每个账号随机生成；发布、登录和备用 session 自动补齐不得自动改动线上账号 2FA，固定密码只允许首次设置，不在普通页面长期明文展示。
- 如需查看、导出或重置 2FA，必须具备敏感权限并写审计。
- TG 官方验证码、完整手机号导出、账号 session 状态、登录设备详情属于敏感信息；查看和导出必须记录操作者、时间、账号、原因和 trace_id。
- 2FA 设置失败、邮箱待确认、旧密码缺失等情况必须在批次项中记录明确失败类型，不能只显示“失败”。

---

## 3.4 运营目标

### 页面目标

把账号同步和任务内输入沉淀得到的群、频道、讨论组、联系人整理成可运营对象，作为消息发送、任务执行、数据复盘和审计归因的底层业务对象。运营目标页不是创建任务的必经入口，也不再提供“新建群聊 / 新建频道目标”作为任务准备流程。

运营目标不承载全站 AI 画像治理，也不直接编辑风控策略。运营目标详情只展示目标是否被选为画像学习来源、当前风险状态、命中原因和跳转入口；画像样本治理进入“目标画像”，策略编辑进入“风控中心”或任务编辑页。

### 目标类型

| 类型 | 能力 |
| --- | --- |
| 群 | 发送、监听、AI 活跃、转发源、转发目标、归档 |
| 频道 | 浏览、点赞、评论、回复、监听新消息、归档 |
| 讨论组 | 频道评论/回复承载、上下文采集 |
| 联系人 / 私聊 | 消息发送，默认不进入持续任务 |

### 主要按钮

| 按钮 | 行为 |
| --- | --- |
| 同步全部目标 | 从在线账号同步群、频道、联系人到 `operation_targets` |
| 目标详情 | 查看授权、关联账号、最近消息、任务和能力 |
| 查看准入状态 | 查看任务触发的关注 / 加入结果、失败原因和可复用账号关系 |
| 处理准入失败账号 | 查看跨任务准入汇总、批量筛选失败账号，并可跳转到具体父任务 / 准入子任务处理 |
| 同步消息 | 对频道目标同步频道消息 |
| 创建任务 | 带目标预填进入任务中心 |
| 消息发送 | 带目标预填进入消息发送 |
| 授权 / 能力调整 | 修改目标可发送、可监听、可归档等能力 |

运营目标页不提供新建群聊或新建频道目标按钮。运营人员需要对新群聊 / 新频道创建任务时，应在任务创建目标步骤粘贴入口；系统在后台自动创建或复用 `operation_targets`。

目标详情中的“最近消息”和“账号覆盖”可以为目标画像提供来源状态，但不能在运营目标页出现画像版本、重建、清空、样本采纳、样本降权或样本剔除等治理动作。任务中心发送频控只保留全局风控与任务级每小时上限两类入口；运营目标页不再提供会影响任务中心发送的隐藏每日上限或群冷却配置。

### 运营目标有界加载契约

- 第一方运营目标列表必须显式发送 `page/page_size`，搜索使用 `q`，编辑态已选目标使用重复 `ids` 参数（如 `ids=1&ids=2`）回显，关联群深链使用 `linked_group_id`，能力筛选使用 `capability=send/listen/archive/task`。
- 有界响应继续返回 `OperationTargetOut[]`，并通过 `X-Total-Count/X-Page/X-Page-Size` 返回分页元数据；空页返回空数组和真实 total，不静默跳到最后一页。
- 旧调用只有在未携带 `page/page_size/q/ids/linked_group_id/capability` 时保留完整匹配集合语义；`target_type/account_id` 继续兼容原过滤。所有第一方页面必须迁移，不能依赖该兼容面。
- 后端必须先完成租户、过滤、count、稳定排序和目标分页，再只为当前页读取关联群并做 SQL 条件聚合；不得全量物化 `TgGroupAccount` 或逐目标 N+1 查询。
- `GET /api/operation-targets/runtime-summary` 支持 `target_ids`；运营中心只读取当前目标页对应的摘要，空当前页不能被解释为全量。

### 任务内目标录入规则

| 输入 | 处理 |
| --- | --- |
| `@channel_name` | 去掉 `@` 后保存 username |
| `https://t.me/channel_name` | 规范为 username |
| `https://t.me/+...` / `joinchat` | 保留邀请链接，用于前置关注 |
| peer id | 用于识别已同步目标，通常不能单独完成主动加入 |

### 目标管理与任务内创建关系

```text
同步目标 / 任务内粘贴目标
  -> 保存 operation_targets
  -> 目标详情展示历史账号-目标关系
  -> 任务中心使用或复用该目标
```

运营目标不要求一开始就有账号已关注或已加入，也不要求运营人员在运营目标页先创建群聊或频道。准入动作主要由任务创建或任务启动触发；任务详情负责处理当前任务范围内的准入重试、可发言复检、标记人工处理和跳过账号，运营目标页负责跨任务准入汇总、批量定位和跳转，不再作为唯一处理入口。任务创建不应只允许选择已关注账号，而是允许选择账号范围，并把账号拆成三类：

| 分类 | 含义 | 任务处理 |
| --- | --- | --- |
| 已满足 | 频道任务已关注；群聊监听源已加入；AI 活跃群和转发目标群已加入且可发言；账号状态可用 | 可直接进入主互动 |
| 可准备 | 未关注 / 未加入，但目标有 `@username`、公开链接或邀请链接可加入 | 先生成关注 / 加入前置动作，成功后进入主互动 |
| 不可准备 | 账号受限、离线、缺少加入入口、peer id 不能主动加入或 TG 返回限制 | 不进入主互动，展示失败原因 |

目标详情必须展示由任务或同步沉淀出来的账号-目标关系：

- 已满足本任务准入账号数：频道已关注、转发源群已加入 / 可读取、AI 活跃群和转发目标群已加入且可发言。
- 未关注 / 未加入但可准备账号数。
- 准备中账号数。
- 失败账号数和失败原因。
- 最近一次准备批次状态。
- 失败账号的处理状态；运营目标页展示跨任务汇总和跳转入口，具体重试 / 复检 / 人工处理在对应任务详情准入抽屉中完成。

### 3.4.1 群聊救援配置

群聊救援用于处理群聊准入和 AI 活跃群中的账号级权限异常。系统只能使用已经具备目标群管理员或邀请权限的 TG 账号，不能自动把一个账号变成所有群的管理员。

配置入口在“系统配置”内，与“TG 开发者应用”“AI 供应商”等配置页签同级，新增独立“群聊救援配置”页签；页签内直接完成启用、救援管理员账号选择和保存。救援配置不得藏在 TG 开发者应用卡片、运营空间详情或运营空间编辑弹窗里，也不得和租户配额保存混用同一个表单动作。配置项包括：

保存群聊救援配置属于系统底座写操作，后端 `PATCH /api/tenant-group-rescue-settings` 必须要求 `system.manage` 权限。前端隐藏或禁用保存按钮不能替代后端权限校验；只具备 `system.view` 的用户只能读取配置状态，不能启用、关闭或替换救援管理员账号。

| 配置项 | 口径 |
| --- | --- |
| 启用状态 | 关闭时不创建救援 action，只暴露普通失败原因 |
| 救援管理员账号 | 只能从本运营空间在线、未删除且有可用 session 的 TG 账号中搜索选择 |
| 连续失败阈值 | v1 固定为 3，不开放前端修改 |

救援管理员账号搜索必须绑定当前搜索请求序号；连续输入、打开下拉刷新或快速切换关键词时，旧搜索响应不得覆盖当前候选账号列表、loading 或错误提示。当前搜索失败必须在群聊救援配置卡片内展示后端 `detail` 或可读错误正文，不能只结束 loading 或保留旧候选列表当作成功。

救援管理员账号是专职处置账号。只要某账号被配置为 `group_rescue_admin_account_id`，普通任务账号选择、账号覆盖统计、群聊准入快照、频道关注前置、消息发送、频道浏览、点赞、评论和转发发送都不得把它当作可参与账号。历史上已经排到该账号的普通 action 必须在 dispatcher 入口明确跳过并记录 `rescue_admin_reserved`，只有 `invite_group_account` 救援 action 可以使用该账号。

触发规则：

- 群聊准入任务按账号、任务、目标群统计真实准入动作的连续权限失败；同一个已完成 action 被详情刷新或调度同步多次时只能计数一次。
- AI 活跃群按账号、任务、目标群统计最近 send action 的连续权限失败；中间出现一次成功发送、非权限类失败或 `unknown_after_send`，连续计数必须断开。
- 连续权限失败超过 3 次后，系统停止继续用该普通账号刷普通重试，并创建一个 `invite_group_account` 救援 action，由救援管理员账号直接邀请触发失败的普通账号入群。同一账号、同一任务、同一目标群只能存在一个未被替换的救援 action。
- 未配置救援管理员账号、救援账号不可用、目标群未同步、被救援账号无法解析时，不创建假成功，任务详情必须显示“救援配置缺失”或真实阻塞原因。

救援 action 规则：

- 执行账号固定为全局救援管理员账号，不参与正式 AI 聊天内容。
- payload 必须包含 `group_id`、`operation_target_id`、`group_peer_id`、`target_account_id`、`target_account_ref`、`trigger_account_id`、`trigger_task_id`、`trigger_reason`。
- Telegram gateway 使用救援管理员账号邀请 `target_account_ref` 对应的普通账号入群；缺少邀请权限、目标群不可访问、被救援账号无法解析、救援账号不是管理员都直接返回失败。
- 被救援账号已在群内按幂等成功处理，但普通账号仍必须重新验证 `can_send=true` 后才算恢复。
- 救援 action 已进入 Telegram 调用边界但本地结果未知时，准入账号明细必须把救援状态同步为 `unknown_after_send`，展示真实错误详情并等待人工确认或补偿复检；不能继续显示为 pending。
- “重试救援”默认按当前全局救援配置重新生成或更新救援 action；运营修改救援账号后，重试必须使用最新配置，而不是复用旧 payload。

任务详情展示：

- 群聊准入任务账号明细展示权限失败次数、救援状态、救援 action id 和 Telegram 原始失败原因。
- AI 活跃群 v1 在触发失败的 send action 和任务执行明细中展示救援状态；后续如需高频运营处理，应升级为账号-目标级救援状态投影，避免运营只在 action 明细中追溯。
- 支持“重查该账号 / 人工已处理”“重试救援”“导出救援失败清单”。救援成功只表示机器人已邀请或已在群内，不表示普通账号准入成功。

---

## 3.4.2 任务内目标输入

任务中心是新目标进入系统的主入口。创建任务时，目标步骤必须支持：

| 输入方式 | 行为 |
| --- | --- |
| 选择已有目标 | 使用已有 `operation_targets.id` |
| 粘贴 `@username` | 后端解析为群聊或频道 username，并自动创建或复用运营目标 |
| 粘贴公开链接 | 后端规范化链接，自动创建或复用运营目标 |
| 粘贴 `https://t.me/+...` / `joinchat` | 保存邀请链接，用于关注频道或加入群聊前置 |
| 输入 peer id | 仅用于识别已同步目标；没有加入入口时不能承诺自动加入 |

任务创建接口必须在后端事务中完成 target upsert，前端不能被要求先跳到运营目标页创建目标。

任务编辑边界：

- 创建任务时允许 `target_type + target_input + target_title` 自动创建或复用目标。
- 编辑任务时不允许输入新目标入口，也不允许通过编辑接口 upsert 新目标。
- 编辑任务可继续切换为已有运营目标，或调整账号范围、规则、节奏、结束时间和失败策略。
- 如果运营人员要对新群聊 / 新频道发起任务，必须从“创建任务”重新进入目标输入流程。

`search_click` 和 `search_rank_deboost` 是上述通用编辑边界的例外：编辑目标群时必须填写完整名称和公开 Telegram 链接，服务端只按公开 username 解析或复用内部目标，不让前端选择或回传 `operation_targets.id`。该例外不适用于其他任务类型；存量 `search_join_group` 只按 legacy 只读/迁移合同处理。

## 3.4.2 目标画像

目标画像是全站唯一的 AI 学习资产，不属于某一个运营目标。所有 AI 活跃群、频道评论和频道评论回复都读取同一份生效画像；任务创建和任务编辑页不能选择“使用哪一份画像”，只能展示当前画像状态和是否可用。

### 页面目标

目标画像页负责说明 AI 正在模仿什么样的群聊语气和评论风格、从哪里学习、哪些样本被采纳，以及当前画像版本如何被 AI 活跃群和频道评论共同使用。页面必须让运营人员一眼看到“现在学的是谁、从哪学、学到了什么、最近什么时候同步、会影响哪些任务”。

### 页面结构

| 区域 | 内容 |
| --- | --- |
| 当前画像 | 全站唯一生效画像名称、版本号、启用状态、最近重建时间、最近 AI 使用时间 |
| 使用范围 | 固定展示 AI 活跃群、频道评论、频道评论回复；不提供按任务或按目标拆分画像 |
| 学习来源 | 已选择的群聊、频道评论区、讨论组、监听账号、最近同步时间、最近向上拉取时间 |
| 学习状态 | 自动同步开关、历史向上拉取进度、待处理样本、已采纳样本、降权样本、剔除样本 |
| 样本质量规则 | 身份过滤、文本过滤、广告模板过滤、质量评分阈值、场景权重和禁学模式 |
| 样本治理 | 按来源、发送人、时间、状态筛选；支持采纳、降权、剔除并填写原因 |
| 版本治理 | 重建画像、查看版本、恢复版本、清空画像；危险动作必须填写原因 |

### 学习来源规则

- 学习来源可以选择一个或多个运营目标，但选择行为发生在目标画像页，不在运营目标详情里治理画像。
- 学习来源选择器默认按运营目标展示，支持按群 / 频道 / 讨论组、是否可监听、是否有监听账号、最近消息时间、关联任务类型筛选。系统可推荐正在运行 AI 活跃群或频道评论任务的目标，但不能自动选中，必须由运营人员确认。
- 来源候选必须同时覆盖群聊运营目标和频道运营目标；频道候选用于学习真实频道评论与讨论区回复，不能只从群聊监听表生成候选。
- 没有监听账号覆盖的目标可以被加入学习来源，但页面必须标记“不可自动同步”，并引导先配置监听账号或重新同步账号覆盖。
- 来源失效时必须保留来源行和最近状态，例如目标被禁用、监听账号离线、无讨论区权限、最近同步失败；不能静默移除来源。
- 群聊来源用于学习自然接话、短句节奏、语气、常见话题、追问方式和禁学内容。
- 频道评论来源用于学习真实读者短评、提问方式、回复方式、对频道原文细节的引用习惯和讨论区语境。
- 监听账号负责采集新消息。监听账号可以来自账号-目标覆盖关系，但“是否用于画像学习”必须在目标画像页明确标注。
- 学习来源保存接口不得信任前端传入的 `listener_account_ids`。后端必须按当前租户和来源目标重新计算可用监听账号集合，只允许保存候选覆盖内的账号；跨租户、已删除、离线或不在该来源覆盖关系中的账号必须返回明确错误，不能写入 `tenant_learning_sources.listener_account_ids`。
- 自动同步新消息是默认行为；历史学习需要支持“向上拉取更多历史”，每次拉取必须展示起止水位、拉取数量、入库样本数和失败原因。
- 画像学习不是静默吸收全部真人消息。系统先形成候选样本，再按质量规则过滤机器人、托管账号、自身账号、系统按钮、模板广告、重复文案和低质量内容；运营人员可以二次采纳、降权或剔除。

### 同步和历史拉取

- 自动同步只采集启用来源的新消息，按来源维护独立水位。水位必须展示为可读状态：最近消息时间、最近远端消息 ID、最近同步结果和失败原因。
- 自动同步和向上拉取历史都必须异步执行，写入运行记录。页面显示 `queued`、`running`、`success`、`partial_success`、`failed` 状态。
- 自动同步、向上拉取历史、候选重算和画像重建失败时，也必须先写入 `tenant_learning_runs.status=failed`、`failure_detail` 和 `trace_id`，再向接口调用方暴露错误；不能只抛错而没有运行记录。
- 同一个来源同一时间只允许一个学习拉取运行中；再次点击必须提示已有运行，而不是并发拉取。
- 向上拉取历史必须允许运营人员选择单次拉取上限，默认值由系统配置给出；页面展示预计耗时和可能触发的账号限流风险。
- 拉取失败不能标记为学习成功；失败来源保留失败原因、执行账号和可重试入口。
- 采集到的消息先进入候选样本，只有 `accepted` 样本参与画像重建；`candidate`、`downweighted` 和 `rejected` 不直接写入生效画像。

### 样本质量规则配置

样本质量规则可配置，但不是规则中心的公开输出规则。它只用于决定哪些学习样本能进入画像候选、哪些降权、哪些剔除。

| 规则类型 | 默认行为 | 可配置项 |
| --- | --- | --- |
| 身份过滤 | 剔除机器人、托管账号、自身账号、源频道身份、运营人员命令 | 是否剔除、账号白名单 / 黑名单、托管账号识别范围 |
| 文本过滤 | 剔除按钮文案、验证码、系统提示、后台操作话术、服务商拒绝语 | 关键词、正则、最短 / 最长长度、命中后剔除或降权 |
| 广告和模板过滤 | 降权或剔除重复广告、固定促销模板、无上下文泛化短句 | 重复窗口、相似度阈值、模板词表 |
| 质量评分 | 短句自然、有具体对象、有追问 / 附和价值的样本优先采纳 | 自动采纳阈值、降权阈值、人工复核阈值 |
| 场景适配 | 群聊样本默认用于接话画像，评论样本默认用于读者短评画像 | 来源场景权重、评论样本是否参与群聊话题权重 |
| 禁学内容 | 内部黑话、后台术语、账号操作命令、敏感链接和联系方式默认进入禁学 | 禁学词表、链接规则、联系方式规则 |

禁学模式支持 `reject` 和 `downweight`：`reject` 直接剔除命中禁学词、链接、联系方式、粗口的样本；`downweight` 保留样本但降权，并保留命中原因，便于运营人工复核。

质量规则变更必须写版本和审计。保存规则只生成新的质量规则版本，不自动改写候选样本状态，也不静默改写已生效画像；需要运营人员点击“按新规则重算候选 / 重建画像”后才影响候选样本和画像版本。

### 画像内容

画像摘要至少包含：

- 风格摘要：常见句长、语气、表情使用、追问和附和方式。
- 话题权重：来源群聊和评论区里被采纳样本的高频主题。
- 句式模式：可复用的问句、附和、补充、转场、轻量吐槽方式。
- 评论模式：频道评论和回复里常见的读者视角、疑问方式和具体细节引用方式。
- 禁学内容：广告模板、后台话术、服务商拒绝语、机器人提示、验证码 / 按钮文本、托管账号发言和运营人员命令。

### AI 活跃群如何使用画像

AI 活跃群仍然以当前任务的目标群上下文为事实来源，画像只提供表达风格和话题倾向，不能替代实时上下文。

- Prompt 拼装必须分层：实时群聊事实、任务配置、全站画像风格、账号角色 / 近期记忆、规则与禁学约束分开传入。模型输出不能把画像摘要里的历史话题当成当前事实。
- 有真人上下文时：Planner 先选择最近可接的真人消息，再把全站画像作为语气、句式和追问方式参考。候选必须锚定当前群里的真人消息、任务话题、素材或账号画像；画像不能让 AI 编造“上次体验”“位置确认”“回访”“准点”等当前上下文没有的事实。
- 规划引用回复时：Planner 先绑定一条当前目标群内可回复消息，再选择执行账号和生成内容。引用回复 Prompt 必须区别于普通发言 Prompt，明确被回复消息作者、原文和“本条是引用回复”的约束；画像只影响回复口吻和句式，不替代被回复消息事实。
- 空闲暖场时：Planner 可以读取画像的话题权重和句式模式，生成少量轻量话题或转场；仍要受任务的无人续聊开关、节奏策略和风控限制约束。
- 沉默时：上下文不足、重复风险高、事实锚点不足、规则命中或画像不可用时，本轮不创建假发言 action，并记录画像状态和 `skip_reason`。
- 多账号发言时：画像提供共同风格底色，账号画像和近期账号记忆负责区分角色，避免所有账号都像同一个人。
- 画像分层：全站运营学习画像提供租户级表达底色，目标群近期上下文和目标群微调优先决定当前话题；画像不能成为事实来源，也不能覆盖目标群实时上下文。
- 画像必须参与候选评分和准入，不只是附加到 Prompt。Planner 需要把 `topic_weights`、`phrase_patterns`、`reply_patterns`、`forbidden_learning` 和质量阈值用于候选排序、降权和丢弃。
- 每条 AI 候选和最终 action 必须记录 `profile_version`、`profile_match_score` 和 `profile_match_reason`，便于排查“画像没生效”“画像导致模板化”或“画像学习方向错误”。
- 画像不可用、样本不足或匹配分低时，AI 仍可基于目标群实时上下文生成；但若同时缺少实时事实锚点，Planner 必须沉默，不能用泛化画像话术补量。
- 真人样本是正向画像的主来源；AI 已发送内容默认只进入消息记忆、去重基线和质量评估，不能直接反哺正向画像，避免“AI 学 AI”导致画像自污染。
- AI 已发送内容只有在人工确认、真实互动效果明确或被标记为高质量复用样本时，才允许低权重进入学习候选；被重复、模板、幻觉或低质量规则丢弃的内容必须作为负向证据或降权样本。

### 频道评论 / 回复如何使用画像

频道评论和频道评论回复必须同时读取频道原文 / 讨论区上下文和全站画像。

- Prompt 拼装必须分层：频道原文事实、被回复评论事实、全站画像读者口吻、任务配置、规则与禁学约束分开传入。频道评论不得直接复述群聊学习样本里的具体人名、价格、地点或内部指令。
- 评论频道消息时：频道原文是事实锚点，画像只影响读者口吻、短评长度、提问方式和讨论倾向。候选必须贴频道消息里的具体词、数字、物品、场景或问题。
- 回复指定评论时：被回复评论和上级频道消息共同作为事实锚点，画像用于决定“像真实读者一样追问、附和、补充还是轻微反驳”。
- 规划引用回复时：Planner 先绑定一条当前频道消息讨论区下可回复评论，再选择执行账号和生成内容。引用回复 Prompt 必须区别于普通频道评论 Prompt，明确被回复评论作者、原文和“本条是回复该评论”的约束；画像只影响读者口吻和回复方式，不替代频道原文或被回复评论事实。
- 候选不足时：AI 不可用、频道原文过短、讨论区不可用、画像样本不足、语义重复或模板化时，不用泛化评论补量，Planner 记录可见跳过原因。
- 评论和回复不能把群聊学习到的内部黑话、后台操作口吻或托管账号命令带入频道讨论区。

### 初始化和旧数据口径

目标画像按全新租户级模型初始化。旧的按 `target_id + profile_scene` 存储的目标画像数据不迁移、不合并、不兼容；旧表和旧接口在新画像上线后退出前端主流程。运行时采集、监听学习刷新、频道评论同步和任务生成都只读写 `tenant_learning_*` 全站画像表，不再新写 `target_learning_*` 旧表。新页面首次打开时如果没有 `tenant_learning_profiles`，后端创建空画像版本 `0`，状态显示为“未学习 / 样本不足”，等待运营人员配置学习来源并拉取样本。

### 与其他模块的边界

- 运营目标：只提供学习来源、目标能力、账号覆盖、最近消息和跳转入口，不承载画像治理。
- 监听中心：负责监听健康、水位、事件和错误；可以从目标画像页深链查看对应监听状态，但不作为画像主配置页。
- 任务中心：创建和编辑任务时展示当前全站画像版本、样本数和可用状态；不允许任务选择不同画像。
- 风控中心：管理全局账号小时/日上限、账号冷却、敏感词、链接白名单和目标例外策略；目标画像页不配置风控策略，任务中心不读取运营目标页隐藏频控。
- 系统设置：管理 AI 供应商、默认模型、提示词模板和黑话词表；目标画像只管理学习结果和样本治理。

## 3.5 消息发送

### 页面目标

支持面向单个或多个目标创建人工消息发送任务，适合运营人员明确发一批消息的场景。

### 功能点

- 选择发送账号或账号范围。
- 选择目标：运营目标、账号联系人、手动对象。
- 输入文本，选择素材。
- 发送前预检查：账号可用性、目标能力、风控限制、规则命中。
- 创建发送任务或批量发送任务。
- 查看任务状态并重试、取消或派发。

消息发送页允许在发送表单内临时创建素材。临时素材创建成功后，如果刷新消息发送基础数据失败，必须展示“刷新消息发送数据失败”并保留已创建素材的本地选择状态；不得把二段刷新失败误报为“创建素材失败”。

### 数据流

```text
前端选择账号和目标
  -> POST /api/risk-control/preflight
  -> 可用账号 / 受限账号 / 阻塞账号
  -> POST /api/message-send-tasks 或 /api/message-send-tasks/batch
  -> message_tasks
  -> message_task_attempts
  -> Telegram Gateway
  -> 回写状态、失败原因和 remote_message_id
  -> Metrics / Recovery 将失败写入 task_runtime_summary / operation_issue / daily_runtime_stats
```

消息发送是手动发送入口，不进入运营方案管理，但失败事实必须进入同一套运营异常闭环。首期可以保留 `message_tasks` / `message_task_attempts` 作为兼容事实源；Metrics 必须把手动发送失败按目标、账号、失败类型上卷为 `operation_issue`，并在 `operation_issue_sources.source_type=message_task` 中保留来源。后续如果消息发送迁移到统一 Task / Action，也必须保持旧发送记录可追溯。

---

## 3.6 任务中心

### 页面目标

运行持续运营任务，把运营中心方案或高级手动配置拆成可追踪的 Task 和 Action。任务中心是执行详情、失败事实源和调度控制台，不再是运营人员发现问题的唯一入口。

任务中心与运营中心关系：

- 任务中心记录完整失败事实：task、action、attempt、账号、目标、失败码、原始错误、重试记录和调度状态。
- 运营中心按目标聚合展示运营异常：哪个目标受影响、哪些任务失败、主要原因是什么、建议怎么处理。
- 运营人员默认从运营中心看到异常，轻量处理在上下文弹窗 / 抽屉完成；复杂流程再深链到任务详情、账号处理、目标处理或规则处理，并保留返回上下文。
- 任务中心仍保留高级手动创建能力，但默认运营路径应从运营中心方案生成或调整任务。

### 任务类型

| 类型 | `tasks.type` | 说明 |
| --- | --- | --- |
| AI 活跃群 | `group_ai_chat` | 在授权群中按上下文和账号画像生成多账号自然对话 |
| 转发监听群 | `group_relay` | 监听源群消息，经规则过滤、转换、路由后转发到目标群 |
| 频道浏览 | `channel_view` | 按帖子级产量给频道消息安排浏览动作，支持初始帖子范围和持续监听新帖 |
| 频道点赞 | `channel_like` | 给频道消息安排 reaction 动作 |
| 频道评论/回复 | `channel_comment` | 在频道讨论区评论或回复指定评论 |
| 搜索点击 | `search_click` + `search_execution_mode=click_only` | 通过第三方索引机器人执行关键词搜索、翻页、目标匹配和目标点击；click 确认后结束，不创建 membership/admission/can-send child；实时 pacing / random decision 不调用 LLM。旧 `search_join_group` 仅作存量兼容；“搜索点击加入”为后续独立模式，本轮未设计 |
| 搜索排名观察任务 | `search_rank_deboost` | 在集搜机器人（首版仅 `@searchbot`/`jisou`）的搜索结果中灰度观察曝光、真实安全导航点击和风控边界；不得承诺“降低竞争群排名”，排名变化只作为观察指标。新建填写单个目标群、搜索关键词、目标次数、黑搜索账号组、每天执行次数、完成截止时间、日/小时抖动和可选静默时段；结构合法即创建，统一 start 成功后再评估运行准备态，缺代理/协议/豁免群时 Task 保持 running 且 `runtime_state=waiting`。代理、机器人、单账号策略和停留由系统托管。系统候选范围仅为所选启用 `pool_purpose=rank_deboost` 分组内的一致账号。点击语义为 `navigate_only`，每个 action 最多一次真实点击，按钮必须与当前搜索结果中的公开 username 精确绑定；没有 username 的目标不得只凭 peer id 执行。Gateway 必须返回逐点击 confirmed/unknown outcome 与实测停留时长，只有 confirmed 才写成功统计。代理模型为「1 分组 = 1 持久运行绑定」，任务复用绑定；Gateway 必须使用同一 SOCKS/HTTP 运行端点完成当前出口探测和 Telethon 连接，不得用绑定旧 IP 自证或回退直连。任务与 `search_click` 平行，互不依赖；实时 pacing / random decision 不调用 LLM。完整设计见 `docs/03-feature-designs/search-rank-deboost-hardening-design.md`。 |

### AI 活跃群话题、讨论老师、真人化互动与群管机器人准入

`group_ai_chat` 必须支持按任务配置多个话题方向和多个讨论老师。话题方向只约束被 `AiGroupContentAllocationPlan` 分配为 `topic_mode=configured_topic` 的普通正文主线，不要求每轮或每个 slot 使用；讨论老师用于描述当前独立 teacher assignment 围绕的人物、小姐、老师称呼或对象，它不是账号 persona，也不要求绑定 Telegram 真实用户。

配置字段：

| 字段 | 含义 |
| --- | --- |
| `topic_directions` | 话题方向列表，每项包含 `title`、`description`、`weight`；Web 和 TG bot 主入口只要求每行一个话题，系统按行顺序自动生成权重；旧 `topic_hint` 通过迁移写入本字段后移除 |
| `topic_participation_rate` | **只针对任务 `topic_directions`** 在普通 AI 正文中的主动占比上限；后端精确小数范围 `0～0.30`，新建/启用必须显式确认，UI 可推荐 `0.30` 但 API 与存量迁移无静默默认。运行时冻结为 task-day ledger 的 `topic_rate_bps=0～3000`，由 `AiGroupContentAllocationPlan` 作为唯一 aggregate owner 分配；词库每日主题、词汇样本和 teacher 不进入分子或分母。它也不是参与账号比例，不截断数量义务、账号 coverage 或发送总量 |
| `teacher_targets` | 讨论老师列表，每项包含 `name`、`description`、`priority`；Web 和 TG bot 主入口只要求每行一个对象，系统按行顺序自动生成优先级。teacher 是独立内容维度，不受任务话题 30% 限制，详情单独展示 planned/remote ratio |
| `reply_min_per_round` | 每个群聊轮次至少一个真实引用回复；没有候选时记录短缺，不伪造回复 |
| `group_bot_admission_required` | 固定为 `true`；AI 活群账号新入群后必须先完成群管机器人准入，不能由任务开关绕过 |

执行规则：

- 每轮规划先由唯一 `AiGroupContentAllocationPlan` 冻结 relation/act type/stance/reply/material、任务话题 mode 和独立 teacher assignment；技术批次只消费同一 plan vector。`configured_topic` 必须在 immutable intent/Provider 前取得按最坏情况计算的容量 reservation：remote-confirmed 正文提供分母，`configured_topic` unknown 同时计入分子/分母，non-topic unknown 不提供容量。无容量时当前尚未冻结 assignment 正常走 `human_context/group_free_chat`，少用任务话题不形成欠账，不创建 topic wait/deadline shortfall，也不影响数量/coverage。Generation Prompt读取同一 intent，accepted variation创建ready Action时才把冻结 identity/摘要复制到 payload。Planner不得为 normal body 先写 Action。
- 词库每日主题按 `surface_scope=(tenant,target group,route family)` 的 `daily_vocabulary_theme_id` 独立轮换，同群多 Task 共用；general/adult 各自使用 120+ route/theme/act-type/stance/fact-aware catalog。主题只调整已冻结 assignment 内的表面词权重，不得改写 relation、act type、stance、任务话题、老师、引用目标、persona 或真人上下文，也不参与 `topic_participation_rate` 计数。词汇先做群可见面 reservation/冷却再冻结进 intent，每 slot 最多 2 项、允许 0 项；`topic_participation_rate=0` 时主题仍正常轮换。Gateway 只复核 topic/vocabulary reservation identity；完整合同、低量预计为 0 的 UI 说明、Phase 0 原型、成本门和七日 remote-confirmed E4 见 `docs/03-feature-designs/ai-group-prompt-daily-rotation-and-rich-vocabulary-prd.md`。
- 同一群聊或频道讨论会话中，连续的两条平台消息必须由不同账号发送；若两条消息之间已出现真人发言，则不再视为同账号连续发言。群管机器人控制消息不打断该规则。没有替代账号时 Action 必须显式等待 `speaker_rotation_wait`，不能为了补量而同账号连发。
- 群聊和频道评论都优先使用 Telegram 原生 `reply_to_message_id`；有合格候选时每批至少一条引用回复，候选不足只记录 `reply_target_shortfall`，不得把“回复某人”伪装进普通正文。
- normal内容先由主AI最多3轮、再由不同备用AI最多3轮生成与真人化质量校验。仅coverage已完成的direct extra-volume在六轮耗尽后由Generation写`normal_generation_exhausted` immutable handoff，Planner消费后才创建精确`签到`；未完成coverage只有`mask_missing`可由Planner不调用Provider直接签到，其他六轮失败保持同义务`content_capacity_gap`。授权路线切换只恢复原内容链，不是签到触发器；无路线为`waiting_transport`。签到不进入normal面具匹配与10天语义去重；reply必须保留原`reply_to_message_id`且引用失效不能降级为direct。
- 群聊和评论必须在发送前执行统一真人化质量门：拒绝模板壳、重复起句、无事实锚点、错误引用、语义复读和不符合账号口吻的候选；质量门只能给出可审计拒绝，不能静默改写正文。
- 2026-07-31 生产只读评估新增「优化 AI 活群真人话术待修复」范围：`chat_mode` 单一真相源、collective should-speak、租户全局 persona 区分度/AI self-history 消融、真人校准 selector、reply relation、静默 holdout、真人节奏指纹、实际成稿趋同、single-message late binding/发送前上下文重校验、平台问句后的真人话轮保护，以及 A/B 群正文与生成输入零串群隔离。固定连续条数 cap 已被生产回放否决；恢复入口后的匿名聚合证明实际成稿处于窄长度带、5,000 条成功 Action 中 61.16% 在 Gateway 调用前被新真人消息越过、21.16% 超过配置过期阈值，平台问句 60 秒内被另一平台账号接管的比例为 61.71%。跨群审计中，30 天 26,435 条 Action、1,183,486 个 context 引用及相关 memory/turn 未发现结构错配或跨群完全相同正文发送，但 2,696/7,488 条成功 Action 的 Prompt 明确携带跨任务原文，涉及 5 个群，因此当前跨群 Prompt 隔离判 `failed`，不得宣称已有绝对保证。A 群的上下文、话题、老师、AI 历史正文和动态 memory 只能进入 A 群；B 群同理。任一 scope 不一致必须在 Provider/Gateway 前以 `cross_group_content_scope_mismatch` fail closed，禁止改投、签到或旧缓存降级。这些是问题证据，不是优化效果：逐条 Provider A/B、真人 gold、完整状态机/欠账/回滚和 H-11 红测仍未完成，专项 PRD §14 继续为 `humanization_speech_repair_design_status=partial`，不得借用 2026-07-27 既有 `design_status=complete` 直接开发或宣称上线。
- 新入群的 AI 活群账号必须先完成群管机器人准入。`TgGroupAccount.can_send` 只表示 Telegram 传输权限，`GroupBotAdmission.state` 单独表示群管机器人规则；两者不得相互伪造。**来源信任必须早于账号归属和状态写入**：普通 bot、未绑定 peer 或 unknown role bot 不得污染等待账号；只有管理员 bot、admission 已绑定的同 peer，或 `targets.manage` 以原始消息/按钮证据审计绑定的目标级 explicit/follow policy peer，才能进入控制提示识别。可信 peer 的普通内容、联系人/频道广告不是控制事件；只有精确公开 `t.me/<username>` 引用加明确关注/加入/验证指令，或同源精确确认 callback，才可迁移 admission 或创建 follow。若控制文本已有明确收件人，必须用“归一化展示名 + 同一可信提示要求链接/按钮”唯一匹配当前 waiting account；重名不可区分时写 `recipient_ambiguous`，不能因同群只有一个 waiting admission 而错配。若是无明确收件人的全群频道规则，只能在 peer 已由管理员身份确认、或同群同 peer 的 source-bound policy 审计确认后，针对运行中任务持久 scope 内的既有 admission 逐账号创建精确 follow/callback；显式收件人不匹配默认不得批量展开，**唯一例外**是 active policy 后同一可信 peer 的两条不同 source message 重复给出完全相同的精确频道集合与确认 callback 形态，证明为标准化准入模板。未知 peer、单条显式收件人提示、普通推广和无 scope admission 一律不得批量展开。频道引用可仅存在于内联 URL 按钮，快照仅持久化按钮坐标、文本、公开 URL 和类型，绝不保存 callback data；Gateway 必须验证原消息、bot peer、按钮坐标/文本/类型，并只关注原提示的广播频道。关注成功和 Telegram 无正文能力探测都不能单独证明群管机器人放行；精确 callback click 本身也不能 ready，仍需要同一可信机器人**可解析完成事件**（后续确认或版本化确认模板表），或运营明确配置并审计的 `follow_sufficient` 协议。`required_channel_refs` 只界定当前 admission 世代的有效 follow 集；`group_bot_control_prompt_unverified` 暂停留下的旧 blocked/skipped 事实不得阻塞当前集，只能在显式 restart，或已审计全群规则重新观察到不同 source message 且 channel_ref 仍在当前精确集合时重新排队。该协议必须写入按“目标群 + 可信机器人 peer”生效的 `GroupBotAdmissionPolicy`，带证据、版本、操作者和撤销事实，不能做成任务 JSON 或租户全局开关。Planner 与 Dispatcher 按 `source_message_id + fingerprint + requirement_action_key` 保证每个 requirement action 最多一个 open/一次 success；同一可信提示有限快照内可有任意多个 action，不同非冲突 action 可并行。join、follow 和 visibility 的先后依赖仍由 admission 状态机约束，不建立账号级全局串行。任何未 ready 状态均不得调用 AI、test_message 或 Telegram 正文发送。专项完整口径见 `docs/03-feature-designs/ai-conversation-humanization-and-group-bot-admission-prd.md`。
- **2026-08-03 群管提示绑定与多点击 supersede：** 明确收件人使用“归一化展示名精确匹配 + 同一可信 bot 原消息中的群聊要求链接/按钮”组合绑定被拦截账号；只有展示名或只有链接不能单独绑定。一条可信提示可产生 0/1/2/N 个独立 requirement action，不设总数上限；每个 `source_message_id + fingerprint + requirement_action_key` 最多一个 open click/一次 success，unknown 只复探不重点。不同非冲突 requirement action 可并发，本条取代上文“每 admission 只有一条 callback”和“join/观察/follow/确认全部串行”的冲突部分，其余可信来源、完成事件、可见性和 unknown 防重合同保留。
- 群管控制观察必须从本次入群前记录的 `join_start_cursor` 严格增量读取至可审计的 `observed_end_cursor`。观察**闭合窗口**（默认 120s，仅决定何时结束观察）与**放行**分离：窗口到期且游标连续、无可信规则时，有 active `not_required` 才写 clear，否则写 `group_bot_policy_unresolved` 并支持运营一键/批量审计创建策略；不得靠固定等待自动 ready。最新 N 条普通上下文、私聊提示、普通成员转发文本或 `probe.ok` 均不是放行证据。
- 首次通过群管准入后的正文，以及 `admission_version` 递增后的首条正文，都必须做无正文远端可见性核验（默认窗口 90s）。Action 进入 `pending_visibility`：**与** `unknown_after_send` **共用** hold 占位 1，禁止对同一义务再规划替代发送。需核验消息在 `visible_confirmed` 前不得增加群日确认数或覆盖确认。可信机器人删除/拒绝写 `post_send_intercepted`、撤回群管 ready、停止后续未进 Gateway Action 且不计完成；覆盖分母保留 blocked。超时不得当成功。
- 待可见性业务事实统一命名为 `pending_visibility_hold`；现有模型/表 `PendingVisibilityCredit/pending_visibility_credits` 只是兼容物理名。需要核验的 Attempt 即使已有 remote id，也只能保存 Gateway 边界成功；`visible_confirmed` 必须在同一短事务关闭 hold、完成 Action、确认群日主义务和可选 coverage。`post_send_intercepted` 关闭当前 hold 后，按自动恢复路径进入 `recovering|abandoned_for_day`；abandoned 可释放未进 Gateway coverage/Action，但不得改写 Gateway-started/pending/unknown。
- `post_follow_visibility_probe` 是一条持久、唯一且可恢复的发送义务。首次进入该状态必须原子绑定当前 Action；同一 Action 在 worker 重启或重新领取后仍可继续，不能被同一 gate 以“准入观察中”退回。绑定 Action 已进入 Gateway、pending visibility 或 unknown 后绝不换绑；仅明确 pre-Gateway terminal 才能由下一条 Action 接管。存量无绑定 probe 由首次符合条件的 Action 补绑。
- **2026-08-04 准入与义务物化统一（2026-08-10 AI current resync）：** ready/post-follow probe 优先；`admission_waiting` 让同一 stable obligation/FOP 进入 typed waiting，并由 admission revision 唤醒，不得以终态、替代 Action、AI 调用或正文发送伪推进。current `fact_first_v3` 数量合同以 `AiGroupMessageObligation` 的 monotonic identity ordinal + active due rank 为 owner；Planner 只做 bounded materialization，依次冻结 allocation/assignment 与 immutable content intent，Generation worker 创建 `GenerationJob/variation`，ready 后才创建 fenced Action。旧 `coverage obligation -> Action`、Action dedupe quantity identity、CycleSlot 与 legacy Planner 均为 `historical_do_not_implement`。远端返回先追加唯一 canonical fact，再以 quantity binding 绑定唯一义务并由 projector 收敛 target/coverage/content/read-model；第二真实事实仍 append 为 unbound conflict，不能靠改 Action 或 ContentMix 猜完成。完整合同见 `ai-group-generation-failure-churn-remediation-prd.md`、`task-fulfillment-classified-recovery-prd.md` 与 `task-fulfillment-contract-closure-prd.md`。
- **2026-08-05 C2 频道关注修复口径：** `fact_first_v3` 的群管提示 requirement 必须绑定 `TaskGroupBotAdmission`，payload 固化 admission version、原消息 ID、source fingerprint 和 requirement key，不得回落旧 `GroupBotAdmission`。任务配置的 `group_ai_prejoin_channel_ids` 即使账号已在目标群、历史 membership Action 为 `already_joined`，也必须在 fact-first 正文前复核并复用/补齐 `configured_channel_follow` facts。FloodWait 等明确 `remote_mutation_started=false` 的失败只保留旧 Action 终态并创建递增 replan Action；Gateway-started/unknown、远端事实和 `closed_unknown` 保留原绑定，账号不可用/目标无效按账号级阻塞处理。频道关注结果必须同时有 Attempt/journal 和 typed remote fact，不能以 `Action=closed_unknown` 或 `telegram_msg_id=''` 单独判定失败/成功。
- 群监听写入 admission 后不得执行未按 admission id/version 下推过滤的 Action 全量读取，也不得跨 Telegram 网络调用持有写事务。MiMo Provider 健康与群准入、listener 数据库阻塞分别报告；未出现 `provider_call_started_at` 时不得归因为 MiMo 不可用。
- 已入群的存量账号不得把旧 `can_send=true` 直接当作新群管准入通过，也不得批量改写该 Telegram 权限字段。迁移分 canary：C1 仅新入群 enforce，存量新建 action 仍可发送但打 `legacy_send_until_reviewed`；C2 复核完成只影响之后新 action，存量 unknown 只走终态/continuity 裁决；全量 enforce 前不得一夜抽空 ready 池。
- 无替代账号且无真人打断时写 `speaker_rotation_wait`；Task 创建并启动后的详情对 `rotatable_ready_account_count < 2` 给质量 warning，不得在创建前请求或阻止启动，也不得为群日目标静默同号连发。`签到` 为 AI 活群唯一确定性文本兜底，不受硬小时配额或活动窗口阻断；历史 `emoji_react` 与同账号 `consecutive_message_*` 连发不得用于 AI 活群新实现。
- `group_bot_channel_follow` / 控制观察 action 归入其父业务任务的 admission lane，仅限同一 `tenant+parent task+account` 解除 admission wait；不能作为独立类别另取全局份额，也不能饿死父任务 ready fulfillment lane。该 Action type 必须适配 `actions.action_type` 的 30 字符存储上限。
- AI 活群任务显式停止时，未执行的频道关注和精确确认动作保留为 `skipped(task_stopped)`；同一任务再次启动时必须基于旧动作的 admission/version/source/账号绑定创建新的 pending 动作，并重新绑定未完成 follow，禁止复活旧动作或重复建单。
- 目标终态、引用 revision 与 unknown 占位仍以 `ai-group-send-continuity-and-terminal-targets-prd.md` 的非硬小时部分为准；群日总量、覆盖确认和已删除的硬小时合同以 2026-07-28 supersede 为准。群管模块不得把 `qdsfxy` 等引用失败写成“群里已被解散”。

交互入口：

- TG bot 是租户级能力，必须先在 Web 系统设置中完成 Bot Token、一个或多个管理员 Chat ID 和 AI 活群 Bot 设置开关配置；Bot Token 保存后不得明文回显，配置、webhook 注册和测试发送必须有明确成功/失败状态。
- 保存有效 Bot Token 和管理员 Chat ID 后必须立即调用 Telegram `setWebhook`，随后调用 `getWebhookInfo` 校验 Telegram 当前 URL 与系统期望公网 URL 一致；只有一致时 webhook 状态才能标记为 `registered`。
- `POST /api/tenant-bot-settings/test-message` 只代表出站 `sendMessage` 成功，不能覆盖 webhook 注册失败、查询失败或 URL mismatch；多管理员 Chat ID 时应向全部管理员发送测试消息。
- Web 必须展示 webhook 状态、期望 URL、Telegram 当前 URL、最后检查时间和错误摘要，并提供刷新 / 删除 webhook 操作；“配置已保存但 webhook 注册失败 / 未注册 / 与预期 URL 不一致”必须显示为不可用。
- Web 任务详情页必须提供 AI 活跃群专项设置区，可查看和保存话题方向、任务话题参与度、讨论老师、引用计划、账号轮换、群管机器人准入和全账号日覆盖配置；创建 / 编辑任务表单必须同步支持同一字段。任务话题参与度显示为 0%～30%，与“参与账号比例”分栏并明确其只控制内容，运行中修改显示当前值、下一任务日生效值和日期。话题方向和讨论老师主入口必须是普通多行文本：每行一个，越靠前权重 / 优先级越高，不要求运营人员手写数组或 JSON。运营人员可把同一组多行话题和讨论老师当作单任务“话题包”维护；当前阶段不新增跨任务模板表，避免为紧急配置链路引入额外迁移和同步成本。
- TG bot 是轻量运营入口。管理员在 bot 内可以选择 AI 活跃群任务、查看当前设置摘要、查看话题 / 讨论老师摘要和当前/下一任务日的话题参与度，并通过按钮分别设置“话题方向”和“讨论老师”；首期不允许 Bot 修改话题参与度。Bot 设置只接受每行一个的多行文本，按顺序生成权重 / 优先级。引用、轮换、话题参与度、群管机器人准入、全账号日覆盖参数、规则集、账号策略等完整配置仍必须到 Web 任务详情编辑。`/ai_group_set <json>` 这类手写配置入口不得作为主入口，旧命令必须返回可见说明。`/start` 和 `/admin` 都必须返回可见菜单或状态说明，不能静默无响应。
- 旧 `topic_hint` 不再作为 AI 活群 UI、API 或运行时配置字段保留；发布迁移必须把旧值写入 `topic_directions` 并移除旧字段，运行时不得再从 `topic_hint` 回退，避免同一任务出现两套话题来源。频道评论任务的 `topic_hint` 是独立字段，不受 AI 活群迁移影响。
- TG bot 入站必须通过租户级 webhook secret 路由到对应运营空间，不能依赖 Telegram update 体内携带业务 `tenant_id`。Chat ID 不在管理员列表、未启用 AI 活群 Bot 设置、任务不存在或配置非法时必须给用户可见拒绝 / 状态说明并记录审计；未启用 AI 活群时仍应回复“bot 已连接但 AI 活群设置未启用”，不得表现为无回复。

### 任务日目标履约（共用 Dispatcher 仲裁为 historical_do_not_implement）

AI 活群、频道评论、频道点赞、频道浏览和搜索点击均必须展示“目标是否可达”和“为何未达”两个独立维度：

| 任务 | 冻结目标 | 唯一成功事实 | 典型不可达原因 |
| --- | --- | --- | --- |
| AI 活群 | TaskDayLedger中群自然日base/effective目标、active due-rank集合与动态账号coverage | canonical remote fact + 唯一`AiGroupMessageQuantityFactBinding(bound)`；coverage只由同fact的typed coverage投影完成 | 目标准入、传输路线、正常内容质量、远端未知、projection/settlement blocker；Action/Attempt/remote ID只是provenance |
| 频道评论 | 消息纳入任务时固化的逐消息评论目标 | success `post_comment` Attempt + 远端评论 ID | 讨论区不可用、账号不可评论、AI 质量、窗口容量 |
| 频道点赞 | 消息纳入任务时固化的逐消息 reaction 目标 | success `react_message` Attempt + 远端 reaction 确认 | reaction 能力、关注关系、账号/窗口容量 |
| 频道浏览 | 每ledger+peer+message TargetSet及active-time累积DueSet，累计目标独立冻结 | permanent `ViewRemoteFact(peer,message,account,obligation_local_date)` + 唯一bound `ChannelViewRemoteFactBinding` | logical source、daily identity、账号时隙、消息有效期；Action/Attempt/Gateway只是provenance/hold |
| 搜索点击 | 当日点击目标 | `target_click_observed` | 安全账号容量、静默/截止、协议、CAPTCHA、目标命中 |

所有任务统一展示 target、target revision、due target、confirmed、held、unknown、remaining、planning deficit、quantity overflow、target-reduction overage、open excess、projected capacity、deadline、status 和 blocking codes；目标是精确业务量，不授权靠多发兜底。通用scalar `planning_deficit=max(due_target-confirmed-held-unknown,0)`只供未被专项替代的简单类型；AI必须按active due-rank anti-join bound fact/Gateway/unknown/pre-call owner，浏览必须按每peer-message DueSet与MaterializedSet做同snapshot anti-join，禁止分别count相减。目标达成或下调后未进 Gateway 的excess owner按各专项收口，Gateway-started/unknown继续核验。pending、claiming、executing、unknown、failed、skipped、申请待审批或 Action 创建都不得算成功。`primary_quantity_slot_id/CycleSlot/ContentMix`对current AI数量仅为`historical_do_not_implement`；current数量owner是stable obligation的monotonic identity ordinal + active due rank及唯一bound quantity fact，一条真实消息可同时投影本任务总量1和同账号coverage1。频道浏览业务键是peer-message target+due ordinal，账号只是materialization binding；按日fact/global DailyIdentityOwner负责跨Task同日防重。

**historical_do_not_implement（旧共享 Dispatcher）：** 共享 Dispatcher 必须先在真实 scope 的 `DispatchClaimScope` 内持久化跨 Window active ledger，再在当前 `DispatchClaimWindow` 按父业务任务最低轮转 + 剩余需求比例创建跨 shard 的 `DispatchClaimTaskAllocation`，固化父任务 fulfillment/admission lane 份额，随后由 `DispatchLaneShardSolver` 做单次精确 task-lane-to-shard 容量匹配并创建 `DispatchClaimShardAllocation/Reservation`，最后领取 Action。TaskAllocation/ShardAllocation/Reservation 均固化 `dispatch_allocation_epoch`；新 epoch 的全部行与 Window `allocation_state=ready` 原子发布，`rebuild_required` 下不允许领取未完整发布的新权重。通用锁顺序固定为 Scope → Window → TaskAllocation → ShardAllocation → Reservation → Action；纯搜索在 Reservation 后按 carrier（如有）→ assignment → consumptive 子预留 → Action 扩展，所有搜索写路径共用顺序。准入子任务沿用父任务 allocation，并在父任务内通过持久 lane cursor 防止任一方向饥饿；共享 GroupBotAdmission 执行另以唯一 lease 选择 sponsor，其他父任务不重复预留。`ACTION_CLAIM_LIMIT` 只表示单次查询/claim 批量，`DISPATCHER_CONCURRENCY` 只表示单 worker 并发，`DISPATCHER_SCOPE_CAPACITY` 才表示同一 scope 的全局在途量；三者不得互相代替。任务详情需显示 scope/Window capacity、task/lane allocation、全局和 shard active/unclaimed、required/reserved/claimed、cursor、`dispatch_allocation_epoch/allocation_state`、准入 sponsor/lease、未服务原因和 `shared_dispatch_capacity_insufficient`；Reservation 只保证领取机会，不等同 Telegram 远端成功。

一个父任务含多个频道消息/账号/日目标时，先按 `lane + deadline Window + pacing class` 聚合 debt，再计算各 bucket required claims 后求和：相同 deadline 的账号欠额不能逐账号 ceil 放大，不同 deadline 的频道消息不能被最晚 deadline 稀释。父任务份额内再按最早 deadline、未满足比例和义务 cursor 稳定选择，避免一条消息或一个账号吞掉全部份额。

### 任务列表

| 区域 | 内容 |
| --- | --- |
| 统计卡片 | 任务总数、执行中、失败任务 |
| 工具条 | 搜索、刷新 |
| 主按钮 | 创建任务 |
| 表格 | 任务名称、类型、状态、目标、账号范围、成功/失败、下次运行、操作 |

任务列表的失败计数是执行事实，不负责完整运营异常聚合。运营中心需要从任务中心读取失败事实并按目标上卷。

### 任务列表有界读模型

- 第一方任务列表使用 `GET /api/tasks/page?page=&page_size=&type=&status=&q=&group_key=`；旧 `GET /api/tasks` 暂保留兼容，但新接口失败后不得静默回退旧全量列表。
- 响应使用 `TaskListPageOut(items,total,page,page_size,summary,groups)`。`items` 只返回列表展示、运行阶段、轻量 stats、目标 / 账号范围摘要和详情入口所需字段，不返回完整 `account_config`、`pacing_config`、`failure_policy`、`type_config`；编辑和完整配置继续读取 `GET /api/tasks/{task_id}`。
- 普通 Task 和 `account_profile_init/account_device_cleanup/account_2fa_setup/account_standby_session_provision` 系统任务必须先投影到同一轻量集合，再共同过滤、稳定排序、计数和分页；稳定顺序为 `priority ASC, created_at DESC, source_kind ASC, stable_id DESC`。
- `summary` 与 `groups` 均基于 `type/status/q` 后、当前 `group_key` 和分页前的完整匹配集合生成；`summary` 固定返回 `total/running/failed`，`groups` 返回稳定 key、完整标签和任务数。顶层 `total` 表示再应用 `group_key` 后的列表总数，任何统计都不能由当前页 rows 反推。
- 账号安全系统任务列表统计必须对候选 batch IDs 做一次 SQL 条件聚合，禁止每个 batch 单独加载全部 items。任务目标 / 频道搜索上下文和 runtime summary 也必须批量补齐，避免逐任务 N+1。
- 任务中心每 60 秒轮询当前 `page/page_size/type/status/q/group_key` 查询；筛选、翻页、手动刷新、轮询和写后刷新继续绑定请求序号，旧响应不得覆盖新查询。

### 任务账号池权重

所有通过任务中心统一账号池选择的任务，都必须按健康分降低低分账号参与权重：

| 健康分 | 任务参与口径 |
| --- | --- |
| `>= 55` | 正常作为候选，仍按健康分从高到低优先 |
| `30-54` | 低权重候选，只按约 1/4 参与比例进入任务账号池 |
| `< 30` | 不进入任务候选，等待账号恢复或人工处置 |

任务账号池、频道 / 群准入前置和 AI 活跃群账号候选必须使用同一个统一账号健康分。不能用 `tg_accounts.health_score` 或单独的基础分绕过风控中心读模型；账号中心、风控中心、任务预检和任务生成必须对同一账号给出一致的参与结论。

### 任务中心数据展示契约

任务中心负责看执行细节，但也不能把所有明细一次性塞进列表。列表读摘要，详情按需分页下钻。

| 页面区块 | 默认读取 | 展示字段 | 查询边界 |
| --- | --- | --- | --- |
| 任务列表 | `tasks` + `task_runtime_summary` | 名称、类型、状态、目标、账号范围、成功 / 失败 / pending、下次运行、最近失败 | 不加载 action 明细 |
| 任务详情顶部 | `tasks` + 目标 / 规则 / 账号摘要 | 任务配置、状态、来源方案、目标解析、规则版本、账号范围 | 单任务 ID 查询 |
| 履约概览 | 统一 `fulfillment` 读模型 | 目标、真实确认、在途、未知、欠额、截止时间、预测安全容量、quantity/content_mix/acceptance 三状态、blocking codes | AI 按账号/自然日，频道按消息，纯搜索按 click ordinal 下钻 |
| 准入前置 | `membership_subtask` 摘要 + 准入 action 统计 | 已满足、待准备、准备中、成功、失败、不可准备、预计完成 | 只查当前任务的准入动作 |
| 执行明细 | `GET /api/tasks/{task_id}/actions` | action 类型、账号、目标、状态、计划时间、执行时间、失败码 | 必须分页，必须支持状态 / 类型 / 时间过滤 |
| 执行尝试 | `execution_attempts` | attempt_no、worker、gateway 开始时间、结果、错误快照 | 默认折叠，点开 action 后加载 |
| 失败事实 | `actions.result` + `execution_attempts.result_snapshot` | failure_type、failure_reason、原始错误、建议动作、是否已上卷运营异常 | 当前任务或当前 action 下钻 |

任务中心状态展示规则：

- 列表上的失败数来自 `task_runtime_summary`，不是实时 count 明细表。
- 列表必须同时展示轻量 `quantity_status`、适用时的 `content_mix_status`、`acceptance_status` 与 `remaining_count`；`Task.status=running` 时也必须允许显示 `blocked/at_risk/missed`，不能只用主状态或数量 met 替代完整履约结论。
- 详情中的 `confirmed_count` 必须由 Action、ExecutionAttempt 和远端事实派生；`Task.stats` 只能缓存最新投影，不能成为独立成功源。
- 详情页打开时可以刷新当前任务的 action 摘要，但明细必须分页加载。
- `unknown_after_send`、人工未处理、准入阻塞和 AI 质量跳过必须在任务详情中可见，并标明是否已经上卷到运营中心。
- 准入 action 的 `unknown_after_send` 必须在准入摘要中作为结果未知 / 人工确认口径展示，不能被聚合成 `completed`、`ready` 或成功准入。
- 准入前置汇总不得把 `skipped` 一概计为成功；只有 `membership_status=joined/already_joined`、历史 `error_code=already_joined` 或 `result.success=true` 的准入动作可计入成功 / ready。`permission_denied`、等待管理员、验证码、人工处理或账号不可用即使 action 状态为 `skipped`，也必须计入阻塞 / 失败 / 人工处理。
- 准入 action 的 `unknown_after_send` 在账号明细 payload 中也必须 `manual_required=true`，确保“只看需人工处理”筛选能命中结果未知账号。
- 任务详情顶部 stats 从准入摘要回填时也必须带 `unknown_after_send_count`，不能只展示 total / success / failed / pending 而漏掉结果未知。
- 准入汇总源头必须输出 `unknown_after_send_count` / `unknown_after_send_account_ids`，并从待准备账号数、失败账号数和预计新建准入 action 数中排除这些账号。
- 运营概览、规则中心执行/维度/趋势/交叉指标和转发归因报表必须把 `unknown_after_send` 计入未闭环异常口径：进入失败/风险计数和失败详情，不得落入普通 pending，也不得从失败率分母或失败详情里消失。
- 风控中心展示 `runtime.unknown_after_send` 指标时，风控详情必须下钻展示对应 `unknown_after_send` action，分类为“结果未知”，不能只取 `failed/skipped` 导致卡片有数量、详情无来源。
- 目标运行汇总的 `failed_action_count`、`affected_task_count`、`latest_failure_at` 必须按未闭环异常集合计算，包括 `failed`、`retryable_failed`、`unknown_after_send`；运营异常详情的 recent actions 也必须使用同一集合，不能让 `unknown_after_send` issue 缺少代表 action。
- 群聊准入快照 item 关联的入群 action 进入 `unknown_after_send` 时，item 必须同步为 `waiting_approval`、`manual_required=true`，并把 `failure_type` 保留为 `unknown_after_send`；它不能继续停留在 `joining`，也不能被计为 completed 或普通 failed。
- 群聊准入快照 item 关联的测试发言 action 进入 `unknown_after_send` 时，item 必须同步为 `waiting_approval`、`manual_required=true`，并把 `failure_type` 保留为 `unknown_after_send`；它不能继续停留在 `testing_message`，也不能被计为 completed 或普通 failed。
- 群聊准入快照 item 关联的删除测试消息 action 进入 `unknown_after_send` 时，item 的 `delete_status` 必须同步为 `unknown_after_send` 并保留未知结果详情；不能继续显示为 deleting。
- 群聊准入快照 item 关联的救援 action 进入 `unknown_after_send` 时，item 的 `rescue_status` 必须同步为 `unknown_after_send`，`rescue_failure_detail` 必须保留 Gateway 边界后的未知结果详情；不能继续显示为 pending。
- 任务详情准入账号表展示 `delete_status=unknown_after_send` 或 `rescue_status=unknown_after_send` 时，必须显示“结果未知”中文标签，不能裸露状态枚举，也不能把救援结果未知显示为“未触发”。
- 任务详情里的“重试 / 重置 / 暂停 / 停止”只改变任务和 action 执行状态，不直接修改运营中心读模型；读模型由 Metrics / Recovery 后台增量同步。
- 列表和详情顶部必须展示派生运行阶段，避免 `running` 掩盖“正在启动 / 等待准入 / 等待 AI / 等待下一轮”，也避免 `paused` 只以弱标签出现。
- `paused` 任务必须使用高强调状态条展示“任务已暂停，不会继续生成或执行新动作”，同时展示暂停来源、暂停时间、最近任务级错误、`next_run_at` 是否为空和“继续”按钮。
- `running` 但当前不发送的任务必须展示原因，例如“准入补齐中”“AI 生成不可用，等待恢复”“上下文不足，本轮沉默”“等待群慢速模式 / 账号冷却”“等待下一轮计划时间”。

派生运行阶段展示表：

| 主状态 / 条件 | 派生阶段 | 展示重点 | 主动作 |
| --- | --- | --- | --- |
| `paused` | 已暂停 | 不继续规划或执行、暂停来源、最后错误、继续入口 | 继续、查看错误、停止 |
| `running` + 启动后尚未完成首轮校验 | 启动校验中 | 目标、账号、规则、AI、风控校验进度 | 刷新、暂停 |
| `running` + 准入存在 pending / running | 准入补齐中 | 已满足、待准备、成功、失败、预计完成；主互动是否已用已满足账号先执行 | 查看准入明细 |
| `running` + AI provider timeout / no healthy provider | 等待 AI | 供应商、模型、错误类型、下次重试时间 | 查看 AI 配置、暂停 |
| `running` + context_empty / low_confidence / quality_skip | 等待上下文 | 最近上下文、沉默原因、下次采集或触发条件 | 查看上下文 |
| `running` + slowmode / cooldown / next_run_at future | 等待冷却 / 下一轮 | 冷却对象、剩余时间、下次运行时间 | 刷新 |
| `running` + due actions executing | 发送中 | 执行中 action 数、账号、目标、最早租约时间 | 查看执行明细 |

### 任务操作按钮

| 按钮 | 状态要求 | 行为 |
| --- | --- | --- |
| 详情 | 任意非删除任务 | 打开任务详情弹窗 |
| 编辑 | 非删除任务 | 打开编辑任务弹窗 |
| 启动 | draft / paused / failed / stopped | 切到 running，设置 next_run_at |
| 暂停 | running | 暂停规划和 dispatch |
| 继续 | paused | 恢复 running |
| 停止 | running / paused | 停止任务，不再规划新 action |
| 重试 | failed，或运行摘要为 partial_success / partial_failed | 按失败策略重新排队；`partial_*` 是摘要状态，不是 Task 主状态 |
| 重置 | 已有执行数据 | 清理运行统计并重新规划 |
| 删除 | 任意存在的任务 | `tasks.manage + explicit_delete_confirmation`，确认绑定 task ID/标题/expected lifecycle epoch；fencing 成功即返回 `202 Accepted + operation_id`，前端显示“删除处理中”，通过 operation 查询阶段、计数、checkpoint 与错误。operation 按 `fencing→snapshot_committed→archiving→archive_verified→deleting→committed` 分阶段短事务执行，`archive_verified` 前零删除；failed 只允许携带审批、expected stage version 与 snapshot hash 恢复同一 operation。只有查询到 `committed` 才显示物理删除完成。tombstone 只留 config hash，不可恢复配置；需再运行则创建新 Task ID/新任务日账本 |

### 任务详情

任务详情弹窗必须在主执行明细前展示“准入前置”区域。它用于说明本任务内关注频道 / 加入群聊的子任务情况，避免运营人员误以为任务卡住或必须先去运营目标页处理。

准入前置展示字段：

| 字段 | 说明 |
| --- | --- |
| 子任务类型 | `target_membership`，覆盖频道关注和群聊加入 |
| 子任务状态 | `not_required`、`pending`、`running`、`partial_success`、`blocked`、`completed`、`failed` |
| 目标 | 当前任务解析出的频道 / 群聊目标、入口类型、是否复用运营目标 |
| 容量统计 | 已满足、待准备、准备中、成功、失败、不可准备 |
| 预计进度 | 基于准入 action 总数、已完成数、批次间隔、退避等待和 FloodWait 估算 |
| 预计完成 | 展示预计剩余时间或预计完成时间；无法估算时展示原因 |
| 当前阶段 | 排队中、加入 / 关注中、等待 FloodWait、等待 AI 回答验证、等待人工处理 |
| 账号明细 | 账号、状态、挑战问题、验证码媒体摘要、AI / MiMo 答案、置信度、是否可发言、计划时间、完成时间和失败原因；点击账号行进入二级详情处理当前任务范围内的重试、复检、人工处理或跳过 |

执行语义：

- 准入前置是任务的可见子任务，不是任务级全局串行锁。
- 已关注频道或已加入群聊的账号必须先进入主互动 action，不等待其他账号加入 / 关注。
- 未关注 / 未加入账号在准入子任务中按抖动、限速和风控执行，成功后追加进入后续主互动容量。
- 部分账号准入失败只影响该账号，不影响已满足账号和准入成功账号继续执行。
- 只有 0 个账号满足准入且 0 个账号准入成功时，主互动才保持阻塞或失败。

准入状态模型必须分三层，不能混用：

| 层级 | 状态来源 | 状态集合 | 用途 |
| --- | --- | --- | --- |
| 父任务主状态 | `tasks.status` | `draft`、`running`、`paused`、`stopped`、`failed`、`completed`、`deleted` | 控制父任务是否允许 Planner / Dispatcher 继续运行 |
| 准入子任务聚合状态 | `membership_subtask.status` 或系统子任务投影 | `not_required`、`pending`、`running`、`partial_success`、`blocked`、`completed`、`failed` | 展示当前目标准入整体是否还在补齐、是否部分成功、是否阻塞 |
| 账号准入阶段 | 准入账号 item 的 `phase` | `not_joined`、`joining`、`channel_follow_required`、`following_channel`、`challenge_required`、`challenge_solving`、`manual_required`、`ready`、`failed` | 展示单账号当前卡在哪一步，并驱动账号级操作按钮 |

`partial_success` 和 `partial_failed` 只能作为运行摘要或准入子任务聚合状态，不能写入父任务 `tasks.status`。账号准入阶段也不能直接写入 `tasks.status`；父任务列表应把它们折算为派生运行阶段，例如“准入补齐中 / 待验证 / 人工处理”。

任务列表展示简化摘要，例如“主任务执行中，准入 3/10，预计 8 分钟补齐”；任务详情展示完整进度、账号级结果和失败原因。

### 创建任务向导

当前创建弹窗为 5 步。创建弹窗只负责配置、结构校验和最终确认，不承载容量、准入、传输等运行明细；任务创建后的长明细必须进入任务详情 Tab 和二级弹窗 / 抽屉，避免在一个弹窗中平铺全部表格。

纯搜索点击与 `search_rank_deboost` 是例外：二者使用“任务类型 → 目标群 → 关键词、目标与执行范围 → 确认创建”的四步专用创建向导。纯搜索点击只展示每日 click 目标，不展示入群开关、admission 目标或成员目标。账号组、截止时间、抖动和静默是运营输入；账号容量、账号执行顺序、授权环境、代理与协议由系统在启动后自动计算，不作为创建前确认项。

| 步骤 | 页面 | 字段 |
| --- | --- | --- |
| 1 | 基础信息 | 任务类型、任务名称、结束时间 |
| 2 | 目标选择 | 群目标、源群、目标群、频道、消息范围、指定消息 |
| 3 | 类型参数 | 规则版本、AI 黑话、内容处理方式、频道动作量、评论方向 |
| 4 | 账号与节奏 | 账号选择、AI 群日目标、24 小时非零累计节奏、准入策略、高级覆盖；AI 单批数量由后端自动计算 |
| 5 | 确认创建 | 用户输入摘要、目标引用、账号范围、数量/内容规则版本和结构校验结果；不展示要求确认的运行容量或 blocker |

### 创建向导字段细节

#### 步骤 1：基础信息

| 字段 | 说明 |
| --- | --- |
| 任务类型 | `group_ai_chat`、`group_relay`、`channel_view`、`channel_like`、`channel_comment` |
| 任务名称 | 必填，默认可由目标名称 + 任务类型生成 |
| 结束时间 | 可选，不填表示持续运行；频道一次性短任务可以设置结束时间 |
| 来源方案 | 从运营中心方案生成时自动带入；手动创建时可为空 |

切换任务类型会重置类型参数，但保留任务名称、结束时间和账号范围；前端必须提示“切换任务类型会清空当前类型配置”。

#### 步骤 2：目标来源

| 任务类型 | 目标字段 | 规则 |
| --- | --- | --- |
| AI 活跃群 | 已有运营目标群、粘贴新群入口、目标名称 | 目标必须是群，主互动要求账号已加入且可发言 |
| 转发监听群 | 源群运营目标、粘贴新源群入口、默认目标群、粘贴新目标群入口、附加目标群 | 源群只要求可监听 / 可读取；目标群要求可发送 |
| 频道浏览 | 已有目标频道、粘贴新频道入口、初始帖子范围、持续监听新帖 | 默认持续监听新帖；初始帖子范围支持最新 N 条、今日新帖、日期范围、指定消息，范围只决定初始帖子池 |
| 频道点赞 | 已有目标频道、粘贴新频道入口、消息范围 | 同频道浏览，额外配置 reaction |
| 频道评论 / 回复 | 已有目标频道、粘贴新频道入口、消息范围、回复对象 | 评论依赖频道帖子、讨论区和账号讨论组权限 |

目标输入支持 `@username`、公开链接、邀请链接和 peer id。前端提交给后端解析；前端不能要求用户先去运营目标页创建目标。

#### 步骤 3：任务配置

| 任务类型 | 必填 / 常用字段 | 高级字段 |
| --- | --- | --- |
| AI 活跃群 | 规则集、规则版本、群日发送目标、话题方向、语气、AI 黑话配置 | 24 小时非零累计节奏、每轮发言模式、手动每轮发言数、准入策略、历史条数、账号记忆条数、账号角色、无人续聊开关、续聊间隔、上下文过期消息数、System Prompt 覆盖 |
| 转发监听群 | 规则集、规则版本、转发处理方式 | 屏蔽机器人消息、屏蔽管理员消息、排除发言人、去重窗口、去重方式、保留媒体、来源标注 |
| 频道浏览 | 初始帖子范围、持续监听新帖、每条帖子每日浏览量、每条帖子累计目标浏览量 | 帖子有效期、分布 / 快速执行；任务日与任务内账号门禁只读固定 `1_000_000` |
| 频道点赞 | 预计每条点赞、Reaction 范围 | 随机 / 指定 reaction；任务内账号门禁只读固定 `1_000_000` |
| 频道评论 / 回复 | 规则集、规则版本、预计每条评论 / 回复、互动方式、评论方向、主题方向 | 回复对象、最大评论长度、System Prompt 覆盖；任务总量与任务内账号门禁只读固定 `1_000_000` |

AI 活跃群必须默认使用“真人接话为主、空闲低频暖场为辅”的策略。正常内容不得用模板话补量；同一active Provider key下主/备用模型各最多3轮仍无可用正文时，只有coverage已完成的direct extra-volume可由Generation写immutable handoff，Planner消费后以同`(task,group,account,task-day)` scoped claim创建精确`签到`；未完成coverage只有`mask_missing` direct分支可直接签到，reply/强引用和其他六轮失败不得签到。签到不引用不存在的事实，仍受目标准入、敏感内容、账号用途、会话轮换和Telegram真实结果约束；没有可用传输路线时保持`waiting_transport`，其余欠量显示`content_capacity_gap`。成人交易/性服务描述只允许在输入边界被识别和过滤，不得把露骨服务词、价格、联系信息、预约、位置或交易编号原样或概括后送入生成Prompt；生成内容不得新增联系方式、价格、邀约或交易撮合信息。

#### AI 活跃群安全上下文、英文 Prompt 与模型回退

AI 活跃群生成前必须先把所有动态文本拆成群名、账号 persona / 画像、话题方向、讨论老师和真人聊天上下文，分别执行安全上下文过滤。任务 ID、群 ID、账号 ID、节奏配置和结构槽位继续保留；价格、联系方式、预约、服务、交易撮合、具体性行为和未成年人 / 年龄歧义语义不得进入生成 Prompt。明确为成年人的既有非露骨外貌、身材、腿部、丝袜 / 高跟鞋、穿搭、性感风格、撩人气质和成人活力话题可以保留并自然接话，但不得由模型凭空新增人物或相关事实。

System Prompt 和 User Prompt 的指令部分统一使用英文，动态上下文可以保留原始中文安全短句；模型必须输出中文群聊内容和固定 JSON。Prompt 必须要求 `safe_context` 复用至少一个现有安全话题或短语，`generic_warmup` 只能生成问候、签到、天气或在场询问；禁止输出 Markdown 围栏、思考过程、解释或额外字段。所有模型输出必须经过同一套 JSON 解析、拒答 / AI 元话术、交易残留、未成年人 / 年龄歧义、上下文锚定、重复和真人感质量门禁，切换模型不得绕过规则中心或发送前过滤。

生产默认生成链固定为：

1. 主模型：由全系统唯一 active `ai_provider_key_version` 支持，最多 3 轮；每轮都执行同一 JSON、内容安全、事实锚点、账号面具、重复和真人感质量合同。
2. 备用模型：仍使用同一 active Provider key，模型可与主模型不同，最多 3 轮；不得激活第二个 key、复制总额度或绕过任何质量合同。
3. 确定性兜底：AI活群仅有两条current分支——`mask_missing`的未完成coverage direct由Planner直接取得scoped claim并创建精确`签到`；coverage已完成的direct extra-volume在两级六轮耗尽后由Generation写handoff，Planner消费handoff后创建签到。其他六轮失败、reply/强引用均不得签到。频道评论按自身专项使用 20 个审核白名单 Unicode 表情或冻结 `image_meme` 图片表情包；文字/图片权重、稳定随机和重试不换内容均以消息级 fallback policy/selection 为准。引用目标失效只允许同reply义务重选合法引用对象，Gateway-started/unknown不重建。

只有在输入已通过安全上下文分类、属于允许生成的普通群聊或明确成年人的非露骨话题时，才允许进入模型回退链。被输入规则判定为交易撮合、联系方式、预约、具体性行为或未成年人风险的内容必须先过滤或转为 `generic_warmup`，不得通过切换模型强行生成。真实 Provider 外呼一次计一轮；超时、网络/配额/未知模型错误、空回复、拒答、JSON 无法解析、候选不足或输出质量门禁失败均消耗当前阶段一轮。主第 3 轮失败后才切备用，备用第 3 轮失败后才进入确定性兜底，不得无限循环。

仅允许选择当前唯一 active Provider key 实际支持的主/备用模型；任何外部 CLI Bridge 或第二 Provider key 不属于当前热路径。模型不可用时按对应阶段记录失败，不得阻塞 Planner 或持有数据库事务等待。

最终兜底不是模型成功。AI 活群唯一允许的确定性正文为 `签到`；必须写入 `generation_source=static_safe_fallback`、统一 `content_source=check_in`、`trigger_reason` 和原始质量拒绝原因。它不进入普通正文面具匹配和 10 天语义去重；数据库保证同账号/群/Task 日最多一次，且只有真实远端消息 ID 才计完成。

每次生成必须记录 `requested_model`、`actual_model`、`provider_key_version`、`model_stage`、`model_round`、`generation_round_total`、`fallback_reason`、每轮耗时/错误、最终 JSON/质量门禁结果和 `generation_source`。任务详情、运行诊断和质量统计必须区分主模型成功、备用模型成功、确定性兜底和结构性失败；任何回退都不得静默记为主模型成功。

频道评论 / 回复必须复用“去模板 + 语义去重 + 质量拒绝”原则。正常评论在 Phase A 固化发送账号 active `account_mask_id/account_mask_version/mask_snapshot_hash`，普通 emoji 习惯从该面具读取；没有可用 active 面具才属于缺面具。正常评论按生效版本执行生成预算；启用 `channel_comment_business_grounding_v1_1` 后，缺面具、事实不足、质量/预算耗尽或可恢复 Provider 路线用尽时，原 `post_comment` 义务按冻结 policy 选择一个 20 白名单 Unicode 表情或一张 ready `image_meme`，写 `fallback_kind`、`fallback_content_kind`、selection identity 和原始原因。它不是 reaction，图片默认无 caption，reply 必须保留原 `reply_to_message_id`。消息不可评论、账号不可评论、目标未准入或没有可用传输路线时仍不得伪造成功；只有非空远端评论 ID 且实际文本/media fingerprint 与冻结选择一致才计数。

频道普通评论候选数少于单条消息本轮请求评论数时，系统必须记录逐槽位候选不足，不能按实际返回数量静默少建 Action 或把剩余目标当作已补齐。每个缺失槽位在同一 `post_comment` Action 和关系槽内进入下一 generation attempt；主 AI 第 1/2 轮继续主 AI，第 3 轮失败后切备用 AI；备用第 1/2 轮继续，备用第 3 轮仍无可用候选时固化单 Unicode 表情兜底。只有兜底正文自身被明确出站策略禁止或关系/账号存在封闭清单内结构硬失败时才终结该义务。

AI 活跃群和频道评论的“生成预览”接口也必须遵守候选数量契约：请求 `count=N` 时，如果 AI 清洗后的候选少于 N 条，接口必须返回明确的候选不足错误，不能返回短列表让运营误以为预览只需要这些内容。

AI 活跃群和频道评论 / 回复都必须读取全站唯一目标画像。画像只提供风格、话题权重、句式和读者口吻，不提供具体事实；具体事实必须来自当前群聊上下文、频道原文、讨论区评论、素材或账号画像。任务配置页只展示当前画像版本、样本数量、学习来源摘要和可用状态，不允许运营人员为单个任务选择另一份画像。

AI 活群当前唯一数量合同是“单群自然日配置总发送量 + 本任务当日动态必达账号每人至少真实成功1条”。创建和编辑只暴露`daily_message_target`；`base_planned_target=daily_message_target`，`effective_planned_target=max(base_planned_target,current_required_account_count)`，API兼容名只读映射到effective值，已确认数不反向抬高计划。每个数量unit由永不复用的target-local`quantity_ordinal`标识identity，并以可受target revision管理的`effective_due_rank`决定当前DueSet位置；rank owner只有active才抵扣当前due，retired保留历史，protected overage只记溢出。动态账号变化只推进effective revision，不改route target-set identity。账号范围以`(task_id,target_group_id,account_id,task_day_ledger_id)`独立动态维护；Telegram权威Session失效、需重登或不可发送时按typed事实进入当日放弃/等待，群解散终结目标。开放active-rank义务按任务日曲线形成当前累计到期量，再由各阶段真实空闲槽执行；不创建中央Window、任务份额或预扣。暂停/停止立即退出新规划并走lifecycle adoption。current failure repair不删除重建当前Task：以task-day route preparing fence/quiescence后additive接管，守恒readback后才active；切换后不恢复legacy writer。多个running任务和同账号非冲突RPC并发，不串行排空；deadline settlement只用SettledRankSet中的authoritative on-time bound fact判定，late/unproven/protected overage分账。完整current口径以AI专项与分类专项为准。

频道任务默认消息范围为 `dynamic_new`，表示持续监听新消息。`specific` 只作为从运营目标详情选择某条消息时的快捷入口，不能把频道任务主流程重新变成手工登记单条消息。`dynamic_new` 的下一次 Planner 检查必须按任务运行时的北京时间加 `listener_interval_seconds` 持久化；不得把 UTC 无时区值写入带时区字段，否则任务会被错误判成持续到期。单轮规划对当前消息集只能读取一次既有 Action 历史，并同时按 `channel_message_id` 和 Telegram `message_id` 映射，不得按每条消息重复扫描。

运行时 Action 明细留存期保持 5 天，不能以缩短留存作为 CPU 修复。Recovery 对过期明细的清理必须先读取持久化 checkpoint，默认每 300 秒至多执行一次；即使当前没有过期行也要写 checkpoint，避免每轮扫描全部 `actions`。checkpoint 查询必须由 `cleanup_kind + created_at` 索引支撑；任务 metrics 采集和 metrics 留存清理均保持五分钟节奏，不能在每个 Recovery 循环扫描历史审计记录。

#### 频道浏览帖子级产量模型

频道浏览任务不能使用一次性总量模型。浏览量是按帖子、按日期、按账号执行行为逐步累积的运营产量，因此任务创建时必须把“选择哪些帖子”和“每条帖子补多少量”分开。

频道浏览配置字段：

| 字段 | 含义 | 默认 / 示例 |
| --- | --- | --- |
| 初始帖子范围 | 创建任务时纳入的帖子池 | 最近 10 条、今日新帖、日期范围、指定帖子 |
| 持续监听新帖 | 任务启动后新发布帖子是否自动进入该任务 | 默认开启 |
| 每条帖子每日浏览量 | 单条帖子每个任务日的业务目标；不是“最多创建多少Action” | 例如 50 |
| 每条帖子累计目标浏览量 | 单条帖子累计达到多少后停止新增日目标；支持设置具体数值或 0/无上限（持续每日履约） | 例如 300 或 0 (无上限) |
| 帖子有效期 | 帖子发布后可补量的时间窗口 | 例如 3 天 |
| 系统任务门禁 | 防止异常配置造成无界规划的统一技术门禁 | 固定 `1_000_000`，不作为业务目标或运营限流字段 |

执行语义：

- “最近 N 条”确定初始或动态维护的消息范围，每日规划时对该范围内的有效消息进行全账号每日循环扫描。
- 开启持续监听后，新帖自动纳入任务，并按相同的每条帖子每日浏览量和累计目标浏览量执行。
- 每个 `target_peer_id + channel_message_id` 在每个 `TaskDayLedger` 拥有唯一 `ChannelViewDailyMessageTarget`，冻结 target operation、peer、source/target revision、`effective_target_count`（无上限时等于 `daily_view_target`；有限累计目标在 attach 时尚未达到则仍等于完整 `daily_view_target`，允许一个当日批次粒度超额；已经达到或超过才为 0）、pacing anchor、active window、累计 due 和 next due ordinal；`completed_view_count` 由当日 typed `ViewRemoteFact` 投影。
- 当累计目标设为 0 或无上限时，任务不设终生总上限截断，每日均按完整的每日浏览量持续履约。若配置了有限累计目标，当累计浏览达到目标总额时不再建立新日 target。
- 频道实体解析失败是账号视角事实：`PEER_INVALID` 或 input entity 缺失只能令当前 Task/账号执行路径进入 `target_resolution_unverified`/当日放弃，不能直接失败整个频道浏览 Task。同一目标存在其他账号成功 typed remote fact 时必须保持目标 active。只有独立权威目标生命周期事实才能终止整条 Task；失败浏览 obligation 也只有在 Gateway journal 明确 `remote_mutation_state=false` 时才可释放，`true|unknown` 必须保留为 unknown。
- 当多个帖子同时需要补量时，Planner 先用完整 24 小时曲线计算每个 message 的 `DueSet`，再对 `DueSet - MaterializedSet` 与当日未浏览账号（通过增广路径最大二分图匹配）进行全账本打散分配。
- 当天未完成的 Action 不滚入次日；本日 target 在 deadline 写 immutable settlement。次日所有可用账号自动恢复对消息的浏览资格，继续按日目标创建新 Action，循环履约。
- `ViewRemoteFact` 按自然日唯一 `(target_peer_id, channel_message_id, account_id, obligation_local_date)`；同一天内单账号单消息去重，跨天后账号可再次产生新的浏览事实。

频道浏览 current 合同的业务和执行去重必须同时包含：

```text
business due unit = daily_message_target_id + due_ordinal
remote identity = target_peer_id + channel_message_id + account_id + obligation_local_date
current action binding = view_fulfillment_obligation_id + materialization_version + account_id
```

频道浏览任务示例：

```text
初始帖子范围：最近 10 条
持续监听新帖：开启
每条帖子每日浏览量：50
每条帖子累计目标浏览量：300
帖子有效期：3 天
系统任务门禁：1_000_000（固定，不可配置）
```

含义是：任务创建时冻结最近10条initial source set；任务启动后的新帖只append；每条帖子每日目标50且lifetime上限300。系统按DueSet与未使用账号身份匹配，容量不足形成typed structural shortfall而不降低目标；`1_000_000`仅作为异常门禁。创建、编辑、启动和存量原地接管都必须覆盖历史低值。

频道浏览兼容基线用两个可机器读的能力位：backend、Planner、全部Dispatcher、recovery与listener必须发布`channel_view_due_unit_fence_v1`；任何能写logical source observation/revision/event的listener或API入口另必须发布`channel_view_source_event_producer_v1`。inventory building前按实际compose/runtime manifest逐实例核对SHA、role、capability bitset、heartbeat与受控source-event→fanout readback，旧实例/lease必须为0；健康接口或同SHA不能代替能力证明。

#### 步骤 4：账号与节奏

| 区块 | 字段 |
| --- | --- |
| 账号选择 | 全部账号、账号分组、手动选择 |
| 节奏字段 | AI 活群、点赞、浏览使用系统托管的 24 小时 `natural_full_day` 曲线并展示只读摘要；评论和纯搜索点击保持各自即时合同，不暴露中央 Window/份额字段 |
| 高级运行 | 只展示异常账号处理和任务类型硬安全事实；worker 并发是运维容量，不是运营配额、预扣或任务份额 |

AI 活群、点赞和浏览不恢复旧硬小时预算或中央执行 Window，但必须使用任务创建时固化的系统 `natural_full_day` 快照；评论和纯搜索点击保持各自合同。任务还受 lifecycle/deadline、Telegram 权威限制、任务类型安全与远端幂等边界约束。

AI 活跃群创建页必须把“群日数量”和“准入策略”拆成独立区块：

| 区块 | 字段 | 说明 |
| --- | --- | --- |
| 群日数量 | 每群每日发送量 | `daily_message_target >= 1`；`base_planned_target=daily_message_target`，`effective_planned_target=max(base_planned_target,current_required_account_count)`；API兼容planned/effective字段都映射到effective，超发单列且不抬高计划 |
| 群日数量 | 单批规划上限 | 只控制一次 Planner 创建量和队列背压，不形成业务目标、小时门禁或完成上限 |
| 群日数量 | 任务内动态账号覆盖 | 当前 `eligible|recovering|completed` 账号每个至少取得 1 条真实成功；可恢复账号自动回流，不可恢复账号当日放弃 |
| 准入策略 | 自动入群 | 默认开启；未加入目标群的账号进入准入子任务 |
| 准入策略 | 需要关注的频道地址 | 直接填写公开 `https://t.me/<username>`、`@username` 或公开 username，0～3 个；服务端归一化、去重后持久化到 `group_ai_prejoin_channel_ids` 独立字段，无依赖时并发关注，全部成功后再入群/观察群管提示 |
| 准入策略 | 群管提示处理 | 按账号视角执行可信 requirement；配置频道完成且已在群后连续 30 秒无可信提示、零 observation gap 即通过 |
| 准入策略 | 准入子任务并发数 | 控制入群、关注和验证动作并发，不截断最终可准备账号池 |
| 准入策略 | 主任务启动条件 | 默认已有 `>=1` 个可发言账号即可启动主互动，不等待全部准入完成 |

群日数量字段校验规则：

| 字段 | 校验 | 默认 / 推荐 |
| --- | --- | --- |
| 每群每日发送量 | 整数且 `>=1`；小于当前必达账号数时允许保存，但当前生效目标自动抬到必达数并明确提示 | 默认等于创建时该任务当前合格账号数；运行中动态刷新 |
| 单批规划上限 | 正整数；只限制一次写入和开放队列规模，不改变 `daily_message_target`、账号覆盖义务或截止结论 | 后端按共享运行容量给出推荐值，不作为运营履约字段 |
| 任务内动态账号覆盖 | 不提供关闭开关或比例字段；当前任务必达账号目标为 1，不可恢复时当日放弃 | 页面展示当前必达/recovering/abandoned/completed 和动态最低目标 |
| 准入子任务并发数 | 整数；范围 `1-50`；只控制并发执行，不截断可准备账号池 | 默认按账号健康和 FloodWait 风险推荐 |

前端保存前必须展示“推荐值 / 用户手动值 / 生效值”三列。后端必须重复校验上述范围；超出范围直接返回表单错误，不能自动截断成合法值。

AI 活跃群创建成功后的运行详情必须展示：

- 配置群日目标、当前任务合格账号预计、当前生效目标和任务时区，并明示账号范围运行中动态变化。
- 当前开放义务、Generation/interaction 实际空闲与执行槽；明确说明这些只影响 JIT 批量，不降低群日目标。
- 已可发言账号、待入群账号、待关注频道账号、待验证账号、人工处理账号。
- 将创建或复用的准入前置子任务，以及预计准入 action、预计完成时间和风险。
- 质量漏斗说明：系统本批请求Turn数是运行态，不是用户目标；AI返回、清洗、去重、事实锚点和质量闸门可能减少normal候选。六轮耗尽后只有coverage已完成的direct extra-volume可经Generation handoff→Planner进入精确签到；`mask_missing` coverage direct独立统计，其他失败显示`content_capacity_gap`。详情必须分别展示handoff、签到与gap。

#### 步骤 5：确认创建

确认页只合并展示可由用户输入静态确定的内容：

- 任务类型、任务名称、结束时间、来源方案。
- 目标解析：新建、复用、无法解析、缺少加入入口。
- 账号选择范围，不展示要求确认的实时容量结论。
- 数量合同、内容合同、规则版本、AI 主/备用配置和静态安全配置。
- 必填字段、权限、目标引用、账号身份用途、数量/内容规则的结构错误。

只有结构校验失败时“创建”与“创建并启动”禁用并精确定位字段；容量不足、待审批、账号暂不可用、传输路线暂不可用和运行期协议事实不在确认页阻止创建。创建并启动必须先持久化 Task，再建立 ledger 和运行投影；运行 blocker 不得回滚已创建 Task。

底部按钮：

| 按钮 | 行为 |
| --- | --- |
| 取消 | 关闭创建弹窗 |
| 上一步 | 回到上一向导步骤 |
| 下一步 | 校验当前步骤并进入下一步 |
| 保存草稿 | 创建 `draft` 任务 |
| 创建并启动 | 创建后立即进入 `running` |

### 账号选择

| 模式 | 字段 |
| --- | --- |
| 全部账号 | `selection_mode=all` |
| 账号分组 | `selection_mode=group`、`account_group_id` |
| 手动选择 | `selection_mode=manual`、`account_ids` |

### 按任务类型到期时机 JIT 执行

AI 活群、点赞和浏览使用任务日 24 小时 `natural_full_day` 曲线；评论和纯搜索点击不使用该曲线。所有类型都不创建中央 TaskAllocation、执行 Window、任务份额或预扣。

```text
remaining_target = max(0, effective_planned_target - bound_confirmed_count - gateway_started_count - unknown_hold_count)
due_now = 0 at planning_anchor; otherwise max(1, floor(effective_planned_target * elapsed_curve_weight / task_day_curve_weight))
materialization_need = cardinality(CurrentDueSet - mutually_exclusive(bound_fact, gateway_started, unknown_hold, valid_pre_call_owner))
generation_free = max(0, healthy_generation_slots - generating_count)
interaction_free = max(0, healthy_interaction_slots - executing_interaction_count)
search_free = max(0, healthy_search_slots - executing_search_count)
ocr_free = max(0, healthy_ocr_slots - running_ocr_count)
```

`CurrentDueSet`按类型展开：AI为active `effective_due_rank`集合，protected overage不抵扣；频道浏览为每个peer-message target的DueSet；其他类型使用自身专项due identity。各阶段只按自己的真实空闲槽 JIT 物化和领取；AI/点赞/浏览释放一槽后也只能补当前已到期义务。每轮先从每个 running Task 至多领取一条 due 义务，再按 `scheduled_at,task_id,obligation_id` 填满剩余槽位。不得创建 `TaskAllocation/DispatchReservation`、任务份额或预扣，也不得让一个 Task 排空后才执行其他 Task。

任务时区自然日仍建立不可变 `task_day_ledger_id` 和 `[period_start_at,deadline_at)`；自然日中途启动属于 `partial_start/admission_warming`，下一完整任务日进入 `full_day_committed`。任务日边界既归属目标和事实，也为 AI/点赞/浏览冻结 `planning_anchor_at`、deadline 和 pacing snapshot；partial-start 在 anchor 的累计 due 为 0。

所有按自然日履约的任务必须先建立不可变 `task_day_ledger_id`，冻结 `timezone_snapshot/timezone_revision/obligation_local_date/period_start_at/deadline_at/day_phase`；`day_phase` 固定为 `partial_start/timezone_transition/full_day_committed`。账号 coverage、频道消息日目标、纯搜索 click 义务、Action 和 Attempt 都以该 ID 归属；本地日期只用于展示，不能作为跨时区唯一键。任务中途修改时区时，当前 ledger 继续用旧边界，`pending_timezone` 从当前 deadline 生效；连续 running 且该时刻不是新时区 00:00 时，先建立首尾相接的 `timezone_transition` 过渡 ledger。pending 期间再次修改使用配置 revision CAS，保留原 effective_at。连续运行 ledger 的 UTC 区间不得重叠或留洞，历史事实不得按当前时区重新解释；预热日和时区过渡日均尽力完成但不进入完整任务本地日 SLA。IANA 时区的 DST 日按两个本地午夜对应的真实 UTC 区间计算，重复/缺失本地小时按对应小时权重累计或跳过，不能假设真实时长恒为 24 小时。task-day ledger 仅切换业务目标归属，不得重置账号/关键词安全额度、Telegram 限流、授权锁、代理/内容冷却或 unknown hold。暂停/停止不改写当前 ledger period；暂停跨过 deadline 不建新 ledger，旧 ledger 如实 missed，恢复时从 `resume_at` 建 `partial_start` ledger。非运行 gap 有审计但不伪造为连续履约。存量 `legacy_mixed_search_join` 的历史 membership/admission 事实维持原 ledger 绑定，但不进入纯点击新合同。

### 目标输入

任务创建向导的目标步骤必须支持：

- 已有运营目标下拉选择。
- 新目标输入框：`target_type`、`target_input`、`target_title`。
- 当使用新目标输入时，结构校验必须展示解析结果：新建目标、复用目标或无法形成稳定目标引用；缺少当前加入入口属于启动后的运行 blocker，不阻止合法目标引用的 Task 创建。

### 可选创建诊断与启动后评估

可选只读诊断接口可以返回以下运行快照，但不得成为创建前置、不得要求用户确认，也不得被创建接口当作授权票据：

- `diagnostic_status`: ready / warning / blocked_runtime。
- `target_resolution`: 目标解析、创建或复用结果。
- 候选账号数、可用账号数、受限账号数、阻塞账号数。
- 已满足账号数、可准备账号数、不可准备账号数。
- 目标能力。
- 预计 action 数。
- 预计关注 / 加入前置动作数。
- `membership_subtask_preview`: 准入子任务预览，包含预计进度、预计耗时、预计完成时间、容量统计和 warning。
- 容量缺口。
- 容量口径：`max_concurrent` 只控制同时执行数量，不截断本轮可参与账号池。
- 规则版本。
- 风控命中。
- 阻塞项和警告。

创建接口只返回结构错误或已持久化 Task。启动器必须重新读取真实事实、建立任务日 ledger 并持续更新上述运行快照；诊断过期、容量不足或外部状态变化不会让已合法创建的 Task 消失或变成创建失败。

### 账号-目标准入前置

频道浏览、点赞、评论、回复、AI 活跃群、转发监听群和转发目标群启动前必须先检查账号对目标的准入状态：

- 准入前置是一等运行对象。AI 活跃群和转发目标群创建 / 启动时，系统必须创建或复用一个任务中心可见的准入前置子任务；子任务与父任务通过 `parent_task_id` / `membership_task_id` 绑定，出现在任务列表、父任务详情和子任务详情中。
- 频道任务中已关注频道的账号标记 `ready`；转发监听源群只要求账号已加入 / 可读取；转发目标群必须要求账号已加入且 Telegram `can_send=True`。AI 活跃群除 Telegram `can_send=True` 外，还必须满足 `GroupBotAdmission.state=group_bot_admission_ready`；二者是独立事实，任何一个不满足都不能进入主互动 action。
- 频道浏览、点赞和评论不能把 `max_concurrent` 当成本轮总参与账号上限。Planner 必须按单条消息目标量扫描有效账号，再由调度层按最大并发执行，避免目标每条 30 但只生成 20 个动作。
- 未关注 / 未加入但有加入入口的账号生成统一准入前置 action。
- 统一准入 action 命名为 `ensure_target_membership`，覆盖频道关注和群聊加入；历史 `ensure_channel_membership` 必须继续兼容展示和执行。
- 准入 action 按抖动、限速、FloodWait 和风控节奏执行。
- AI 活跃群和转发目标群的入群 / 可发言准入前置必须在任务中心列表形成可见进度摘要；运营无需进入执行记录才能看到“加入账号前置任务”的总数、已可发、待准备、验证中 / 人工处理、失败和预计完成。
- AI 活跃群和转发目标群的默认准入排程必须在 4 小时内排完所有待准备账号；`membership_max_concurrent` 只限制同时执行量，不得把 4 小时目标扩展成 6 小时或更长的默认模板。FloodWait、慢速模式、人工审批和验证码上下文不可读可以让单账号超出 4 小时，但必须以账号级状态展示，不能静默延期。
- 主互动只使用准入成功或原本已满足账号；原本已满足账号不等待未满足账号完成准入。已有账号“已加入但不可发言”不能作为 AI 活跃群或转发目标群 ready，需要重新进入准入流程直到达到可发言状态。
- 准入成功账号需要追加进入后续主互动容量，不能只记录成功但不参与任务。
- 0 个账号准入成功时，主互动不规划。

准入前置子任务执行链：

```text
账号范围
  -> 检查是否已加入目标群 / 已关注频道
  -> 未加入目标群：执行入群
  -> AI 活群：记录入群前控制游标并执行账号级群管机器人观察；不同账号、不同非冲突 requirement action 并发
  -> 可信群管机器人要求先关注频道：从正文/内联按钮按精确引用创建 `group_bot_channel_follow` 广播频道关注 action
  -> 对同一可信原消息中每个 requirement action 做重读校验并按 key 精确 click；数量不限，每 key 成功一次，unknown 不重点
  -> 所有当前必需 requirement action 终态后，等待明确放行或目标级审计的 `follow_sufficient` 协议
  -> 入群后检查是否需要验证
  -> 执行按钮 / 文本问答 / 算数题 / 多模态视觉图片验证码识别
  -> 复检 Telegram can_send；群管机器人准入与 Telegram 权限分别留痕
  -> 写入 ready_pool，下一轮主任务可直接选用
```

准入子任务状态必须拆分展示：

| 状态 | 含义 |
| --- | --- |
| `not_joined` | 账号未加入目标群，等待入群 |
| `joining` | 正在入群 |
| `channel_follow_required` | 目标要求关注关联频道后才能发言 |
| `following_channel` | 正在关注关联频道 |
| `awaiting_group_bot_rule` | AI 活群新入群账号正在读取群管机器人控制事件，尚未证明可发言 |
| `awaiting_group_bot_confirmation` | 所有要求频道已完成，但仍等待同一可信机器人放行 |
| `group_bot_policy_unresolved` | 没有可验证的无机器人 / 完成协议，必须显式补充目标级策略 |
| `post_send_intercepted` | 首条正常正文被可信群管机器人删除/拒绝，已撤回群管 ready 并停止后续 action |
| `challenge_required` | 已加入但需要验证 |
| `challenge_solving` | 正在自动 / AI 辅助处理验证 |
| `challenge_context_empty` | 已判定需要验证，但最近验证聊天为空、无机器人 / 管理员验证消息，或当前账号无法读取验证上下文 |
| `captcha_solving` | 正在下载验证图片并调用多模态视觉模型识别 |
| `manual_required` | 需要人工处理，例如图形验证码无法识别或等待群管理员审批 |
| `ready` | 已加入且 Telegram `can_send=True`；AI 活群还必须同时满足 `GroupBotAdmission.state=group_bot_admission_ready`，才可进入主任务发言池 |
| `failed` | 准入失败，保留失败原因和原始错误 |

父任务与准入子任务的调度关系：

- 父任务已有可发言账号时立即运行，不等待准入子任务完成。
- 准入子任务每通过一个账号，必须更新父任务 ready pool；AI 活跃群下一轮自然可选到新账号。
- 父任务详情顶部展示“可发言 / 准入中 / 待验证 / 人工处理 / 失败”汇总，不把准入失败混成主互动失败。
- 子任务可独立暂停、重试、查看明细；暂停子任务只停止新增准入动作，不停止父任务已有可发言账号的主互动。
- 删除或停止父任务时，前端必须提示是否同时停止未完成的准入子任务；默认停止未完成准入动作但保留历史结果。

入群验证处理：

- 固定按钮验证由系统自动点击。
- 文本问题、简单算数题和固定问答优先用本地规则解析，规则无法确定时由 AI 辅助回答。
- 图片验证码由健康的多模态视觉供应商识别，当前支持 MiMo/Mino 和 MiniMax。系统必须从最新验证消息下载图片，组装 `data:{mime};base64,...` 或公网 URL 输入，提示词只要求返回验证码答案和置信度。
- DeepSeek 等纯文本模型不能承担图片验证码识别；如果任务或租户没有健康多模态视觉供应商，直接标记 `manual_required`，失败原因写“未配置可用多模态视觉供应商（MiMo/Mino 或 MiniMax）”。
- 如果普通 `challenge-context` 返回空消息、读取失败或最近消息中没有机器人 / 管理员验证证据，必须进入 `challenge_context_empty` 或 `manual_required`，并展示“未读取到最近验证聊天”。此时不能要求运营人员盲填验证码。
- 操作员点击“重新读取”时，系统必须先重新加入 / 重试准入来刷新验证码上下文，再按“加入账号触发 + 可读账号读取”的策略读取最新验证消息。只有刷新后仍读不到验证码上下文时，才保持 `challenge_context_empty` / `manual_required`。
- 刷新后读到图片验证码时，必须调用多模态视觉模型识别，并由加入账号发送答案；发送后必须复检目标可发言能力。视觉供应商未配置、图片下载失败、低置信或复检失败时，记录为人工处理。
- 批量准入重试同样适用该流程。加入后仍不可发言、未解析到群关联频道、未获群发言权限或群无权限时，默认先创建可自动处理的图片验证任务并读取上下文，不得在未尝试读取当前验证码前直接把 319 个账号落为人工处理。
- 无法识别、识别低置信、图片下载失败、人工审批、TG 明确拒绝或答复后复检仍不可发言时，标记 `manual_required`、`challenge_failed` 或 `failed`。
- 每个账号对同一目标的同一验证问题最多自动尝试一次；重试必须记录新问题、不同图片哈希或人工确认。
- 验证问题、图片消息 ID、媒体哈希、规则 / AI / 多模态视觉答案、置信度、模型、结果、失败原因和原始错误写入 action result、准入子任务详情和父任务准入汇总。

### 频道评论/回复异常归因

频道评论/回复依赖频道帖子、绑定讨论组和账号讨论组权限三个条件同时成立。执行前和执行失败归因必须按下面口径展示：

| 场景 | 归因 | 前端/手册提示 |
| --- | --- | --- |
| 消息 ID 不是频道帖子，或无法通过频道帖子找到讨论区消息 | `COMMENT_UNAVAILABLE` | 请确认消息 ID 属于频道帖子 |
| 频道未绑定讨论组，或讨论组不可见 | `COMMENT_UNAVAILABLE` | 请先确认频道已绑定讨论组 |
| 执行账号未加入讨论组、无评论权限或被 TG 限制 | `COMMENT_UNAVAILABLE` 或账号受限原因 | 请确认执行账号可进入讨论组并评论 |
| 目标实体无法解析 | `PEER_INVALID` | 请重新同步账号群聊/运营目标后再试 |

后端异常映射需要覆盖 Telethon 讨论区解析错误，例如 `GetDiscussionMessageRequest`、`DiscussionMessage` 和 “message ID used in the peer was invalid”。这类错误不能泛化为未知失败，也不能自动重试刷量；应写入 action result、任务详情、运营数据和风控/账号建议。

### Task 状态

| 状态 | 含义 | 可用操作 |
| --- | --- | --- |
| draft | 草稿 | 启动、编辑、删除 |
| running | 运行中 | 暂停、停止、编辑、详情 |
| paused | 已暂停 | 继续、停止、编辑、删除 |
| stopped | 已停止 | 启动、删除 |
| failed | 失败 | 重试、编辑、删除 |
| completed | 已完成 | 详情、重置、删除 |
| deleted | 已删除 | 只读审计 |

`pending` 不作为 Task 主状态；等待执行由 `actions.pending` 表达。`partial_success`、`partial_failed` 只作为任务运行摘要或准入子任务状态，用于展示部分成功 / 部分失败，不进入 `tasks.status` 主枚举。

### Action 状态

| 状态 | 含义 | 自动重试 |
| --- | --- | --- |
| pending | 等待领取 | 是 |
| claiming | 已被 worker 预领取，尚未拿齐运行资源 | claim 超时后恢复 |
| executing | 已领取并执行中 | 否 |
| success | 成功 | 否 |
| failed | 明确失败 | 按失败策略 |
| skipped | 策略跳过 | 否 |
| unknown_after_send | 已进入 TG 调用边界但本地结果未知 | 否，需人工或补偿确认 |

---

## 3.7 监听中心

### 页面目标

查看群、频道、讨论组的监听状态，确认源事件和上下文是否正常流转。

### 页面内容

- 监听对象：群、频道、讨论组。
- 监听账号列表。
- 关联任务。
- 事件积压。
- 最近事件。
- 最近错误。
- 备用账号。

### 主要按钮

| 按钮 | 行为 |
| --- | --- |
| 刷新 | 重新拉取监听汇总 |
| 切换监听账号 | 重新分配指定来源的监听账号，写审计 |
| 展开详情 | 查看监听账号、关联任务和最近事件 |

监听中心的汇总刷新、自动轮询、切换监听账号和重置水位都会写回同一份监听汇总。前端必须为汇总写回绑定当前请求序号，旧汇总响应不得覆盖最新刷新、切换监听账号或重置水位结果；loading 和错误提示也必须只由对应的当前请求更新。监听对象详情里的事件明细和错误明细下钻也必须绑定当前详情请求序号，旧下钻响应不得清空当前详情 loading 或覆盖错误提示。

### Listener 数据流

```text
Listener claim source
  -> 读取 listener_source_state
  -> 使用监听账号拉取 TG 消息 / 评论 / 事件
  -> 按唯一键去重
  -> 写 group_context_messages / channel_messages / source_media_assets
  -> 更新水位 last_remote_message_id / last_event_at
  -> 唤醒依赖事件的任务
```

`group_ai_chat` 的监听读取账号必须从任务账号范围和目标群覆盖关系中选择 1 个 active 普通账号。监听读取不消耗发送容量，也不受发送冷却约束；但不得因当前发送容量不足、冷却未结束或任务账号范围无可用账号而回退为遍历目标群全部账号。严格选择不到监听账号时，listener 必须写入“没有可用监听账号”的可见错误并结束该来源本轮采集，避免单个来源扇出为大批 Telegram 历史拉取请求。

---

## 3.8 规则中心

### 页面目标

维护系统级规则集、规则版本、过滤、转换、路由、账号策略、限速、重试和规则测试。

### 核心概念

| 概念 | 说明 |
| --- | --- |
| 规则集 | 一组规则配置容器 |
| 规则版本 | 可发布、可回滚、可绑定任务的不可变版本 |
| 活动版本 | 当前默认生效版本 |
| 草稿版本 | 可编辑，不允许运行任务绑定 |
| 发布版本 | 任务可绑定 |
| 归档版本 | 历史留存，可复制或回滚 |

### 主要按钮

| 按钮 | 行为 |
| --- | --- |
| 新建规则集 | 创建规则集和默认草稿 |
| 编辑规则配置 | 打开规则配置弹窗 |
| 保存并发布新版本 | 保存配置并生成发布版本 |
| 版本记录 | 查看版本列表 |
| 发布 | 把草稿发布成当前活动版本 |
| 复制 | 复制历史版本为草稿 |
| 回滚 | 从归档版本生成新发布版本 |
| 规则测试 | 输入样本文本，验证过滤、转换、路由和输出校验 |
| 加入不转发名单 | 从转发执行项把来源人加入当前任务的来源过滤 override；不得静默修改已发布规则版本 |

### 规则执行点

- AI 活跃群：输入上下文过滤、候选回复输出校验。
- 转发监听群：源事件过滤、内容转换、目标路由、账号策略、限速。
- 频道评论：AI 评论输出校验。
- 素材选择：规则版本固化素材策略，执行项再固化具体素材 ID 和资产版本。

从执行项加入不转发名单只能写任务级来源过滤 override，记录 `task_id`、`rule_set_version_id`、`sender_peer_id` / `@username`、操作者和原因。若运营人员希望改成长期规则，必须复制为草稿、发布新版本，再由任务显式绑定新版本；已发布规则版本不可被原地修改。

AI 活跃群还需要额外经过质量规则：

- 语义去重：将“照片准 / 没照骗 / 真人没差”“态度稳 / 不催 / 不敷衍”“位置提前发 / 没绕路”“结束回访 / 下次安排”等近义表达识别为同一语义簇，同一轮和近 N 轮内限频；去重基线必须同时读取近期已成功发言、已规划未发送发言和 `unknown_after_send` 发言，避免 Planner 在上一批 action 还未执行时继续生成同款 AI 话术。
- 幻觉拦截：候选内容提到具体经历、服务动作、价格、地址、穿着、到场时间、回访等事实时，必须能追溯到上下文、素材或运营配置；无法追溯时改写或丢弃。
- 上下文锚定：真人聊天正在围绕某个人名、@ 对象、价格、榜单、评价或问题展开时，AI 必须优先接当前对象，不能跳回泛化模板。
- 模板感拦截：连续出现“上次那个”“这点加分”“挺省心”等固定壳句时，按重复风险处理。

#### AI 活跃群消息记忆与去重窗口

AI 活跃群必须引入账号所有的消息记忆作为质量基线，解决同一账号短时间、跨批次和跨群任务重复同款话术的问题。时间型硬去重统一为同租户同账号滚动 10 天；不同账号的历史不能互相硬阻断。

消息记忆至少保存：原文、归一化文本、文本指纹、语义簇、模板壳句 key、事实锚点、租户、目标群、任务、执行账号、action、话题方向、讨论老师、状态、预占 key、规划时间、Gateway 边界、发送时间、10 天去重到期时间、账号面具 ID / 谱系 / 版本 / 合同版本 / 快照哈希、面具匹配分、匹配原因和重复参照。在线记录不得早于 `dedupe_expires_at` 清理；超过 10 天的审计记录不得继续参与硬去重。

`ai-memory` worker 的历史 Action 回填属于消息记忆事实修复，不是可选加速路径。每轮必须保留对最近最多 100 条符合条件历史 Action 的核对，并按 `action_id` 判断消息记忆是否已存在：精确存在时只跳过该条，缺失时必须继续创建回填记录。`action_id` 的存在/缺失点查必须由数据库索引支撑，并在 4 万行以上生产等价数据量下保持有界；数据库查询失败、超时或迁移失败必须显式暴露并使本轮失败，禁止通过缩小批次、禁用回填、缓存假命中、吞错或静默跳过来维持 worker 表面健康。实时硬去重只需覆盖滚动 10 天；更早历史可保留审计，但不得继续进入硬去重查询。

归一化规则必须稳定、可复现，并在 Planner 和 Dispatcher 中一致使用：

- 去除无意义空格、换行差异、重复标点、连续语气词和纯装饰表情差异。
- 统一简繁、大小写、全半角、常见数字写法和常见口头缩写。
- 将 @ 对象、讨论老师昵称、轻量称呼和可变人名抽象为占位符参与指纹计算，同时保留原始值用于排查。
- 将同义短语纳入语义簇，例如“服务好 / 态度稳 / 不敷衍”“身材好 / 条件不错”“照片准 / 没照骗”。
- 归一化结果、语义簇和模板壳句 key 必须写入消息记忆，不能只存在于运行时内存。

Planner、Generation和Dispatcher必须使用同一套账号级消息记忆规范，但写owner分离：

- Planner只冻结assignment/intent与dedupe basis/禁用摘要，不调用Provider、不写normal memory reservation或Action。
- Generation创建job后读取该账号最近10天success、reserved/ready/executing/unknown记忆；accepted variation在Phase C原子CAS memory reservation并同事务创建ready Action，冲突为typed duplicate/rejection。
- Dispatcher发送前按同一归一化函数最终复核Generation并发、上一批ready Action、worker重试和跨任务/群同账号撞车；结果回写同一memory。deterministic签到使用scoped claim/memory，不混入normal reservation。
- `pending`、`reserved`、`claiming`、`executing`、`unknown_after_send`、`success` 都参与去重；只有明确未到达 Telegram 发送网关的失败才可从发送历史里排除。
- 预占位超时不能静默删除，必须按 action 状态转为 `expired_before_send` 或 `failed_before_gateway`，并保留足够时间用于排查并发重复。
- 候选不足时应要求 AI 换角度重写并记录质量拒绝；legacy 频道评论 / 回复仍按旧 3+3 与 3 表情合同收口，v1.1 则在公共生成预算耗尽后按消息首次冻结的 20 Unicode/图片素材 policy 与稳定洗牌选择继续同一 `post_comment`。同一槽重试不换内容；图片只可在 Gateway 前按冻结池 append 顺延，Gateway-started/unknown 禁止重选。回复必须保留有效 `reply_to_message_id`；引用失效时只能在同一 reply 逻辑槽递增 attempt 并选择新合法目标，确无可恢复目标才写 `reply_target_unrecoverable`，不得降级普通评论或伪造成功。

去重窗口和处置口径：

| 窗口 | 范围 | 判定 | 处置 |
| --- | --- | --- | --- |
| 10 天 | 同租户、同账号，跨任务、跨群、跨该账号面具版本 | 归一化精确重复、高相似语义、同语义簇、同模板变体或同事实观点复述 | 硬拦截；使用新 variation、角度、对象或等待新上下文 |
| 10 天外 | 同账号历史 | 任意 | 不再硬拦截，可保留审计 |
| 任意时间 | 其他账号历史 | 相同或相似内容 | 仅作多样性软提示和统计，不得写当前账号 `duplicate_message` |

讨论老师和话题方向只作为生成约束和质量标签，不作为绕过去重的理由。同一个讨论老师被反复夸同一类优点、同一个话题方向被反复换词表达，仍然按语义重复处理。

同批次跨账号多样性只作为生成提示和统计：系统应优先换语义簇、讨论老师、话题方向或回复对象，但不得因为账号 B 与账号 A 相似而硬阻断账号 B。单个候选仍必须通过自己的账号面具、事实、上下文、安全和该账号 10 天历史检查。

讨论老师和话题方向需要参与调度分配：

- Planner 需要记录最近使用的话题方向和讨论老师，优先选择本群近期未过度使用的对象和角度。
- 引用回复优先跟随真实上下文里的对象；空闲暖场才使用配置的话题方向和讨论老师。
- 同一讨论老师连续被同类夸赞时，必须切换角度、切换对象或沉默。

运营侧配置保持简单，只暴露“标准 / 严格 / 极严格”等去重强度，不要求运营手填阈值。系统内部必须保留详细阈值、命中样例、相似度、模板 key、归一化文本和跳过原因，便于排障和 QA 验收。

任务详情和运行日志必须展示质量漏斗：AI 候选数、通过数、重复拦截数、模板壳句拦截数、画像匹配低分数、事实锚点不足数、同批次多样性降权数、最终发送数，以及每类拦截的代表样例。

#### AI 活跃群账号在线保活

AI 活跃群不能只在发送瞬间临时拉起账号。平台需要提供全局账号在线保活能力，默认覆盖所有 active、未禁用、未封禁且有可用 session 的运营账号；运行中的 AI 活跃群、转发和监听任务会把任务账号范围内的账号追加为强需求来源。已进入需重新登录、session 失效、受限、异常等可恢复状态的账号，如果仍保留 session 记录，必须继续保留 `desired_online` 投影和失败原因，用于账号中心、任务详情和运营异常展示及后续恢复；但这些账号不得进入 Planner / Dispatcher 可发候选。禁用、已封禁、待登录 / 等待验证码 / 等待扫码 / 等待 2FA 或账号级安全阻断的账号不得被强行保活。

在线保活的目标是保持账号连接和执行就绪，不产生用户可见消息。保活动作只能做轻量连接检查、session warm、目标能力复检或必要的 session 自愈；不得用发言、点赞、关注或其它会被目标群看到的动作来证明在线。

系统需要为 AI 活跃群维护账号在线状态投影：

- `desired_online`：该账号当前是否因 AI 活跃群、转发或监听任务需要保持在线。
- `desired_sources`：当前要求账号在线的来源，例如全局保活、任务 ID、监听源 ID；用于任务停止、暂停、删除或账号范围调整后做引用计数。
- `online_status`：`online`、`warming`、`offline`、`recovering`、`login_required`、`blocked`。
- `session_kind` / `session_id` / `proxy_id`：当前探测使用的 session 和代理，不允许只按账号保存一个无法追溯的在线布尔值；任务 reconcile 必须按账号当前维度覆盖这些字段，账号从代理切回直连时必须清空旧 `proxy_id`，不能让旧代理记录把健康账号误判为不可规划。
- `last_seen_at`：最近一次 Telegram 连接或网关确认可用时间。
- `last_probe_at` / `last_keepalive_at`：最近一次探测和保活时间。
- `stale_after_at`：超过该时间未成功探测时，状态必须转为 `warming` 或 `offline`，不能继续展示旧的在线状态。
- `failure_type` / `failure_detail`：掉线、代理异常、session 失效、FloodWait、登录态失效等明确原因。
- `recovery_status` / `next_probe_at`：是否正在恢复、下一次探测时间。
- `active_task_count`：当前有多少运行中任务要求该账号保持在线。

Planner 和 Dispatcher 必须把在线状态作为主互动硬前置：

- Planner 只能从 `task_ready_accounts` 且 `online_status=online` 或明确可立即 warm 的账号中生成文本或引用回复 slot。
- 账号掉线导致活跃要求未达标时，原因必须记为 `account_offline`、`login_required`、`session_invalid` 或代理 / 风控原因，不能归为 AI 候选不足，也不能用表情或其他非 `签到` 正文兜底伪装成已完成活跃。
- Dispatcher 发送前必须做最终在线检查；action claim 后账号掉线时，action 进入可见失败或等待恢复状态，由 recovery / 在线保活链路处理，不能静默换号发送。
- 在线恢复成功后，账号重新进入后续轮次分配；已经固化给其它账号的 action 不被后台抢占改写。

在线保活生命周期必须可回收：

- 任务创建、启动、恢复、账号范围变更时，重算 `desired_sources` 和 `active_task_count`。
- 任务暂停时保留任务来源但标记为低频保活；任务停止、删除、结束或账号从任务范围移除时必须移除对应来源。
- 全局保活关闭、账号禁用、账号封禁、账号还没有完成登录流程时，必须清理可执行保活来源并展示阻断原因；账号已经有 session 但探测后变成需重新登录、session 失效、受限或异常时，不得清理 `desired_online` 投影，而是保留 `login_required` / `blocked` 等状态、失败详情和 `next_probe_at`，等待人工处理或恢复链路更新。
- 定时 reconcile 必须扫描运行中任务、监听源和全局保活配置，修正漏写、重复来源、孤儿 `desired_online=true` 和 stale 在线状态；reconcile 只能维护需求来源和待探测状态，不能刷新已有 `online` 状态的 `stale_after_at`，只有真实探测成功才允许续期。
- 存量运行中任务迁移时必须批量补建在线状态行，初始状态为 `warming` 或 `unknown`，不得把缺历史探测记录的账号直接标成在线或离线。

在线保活需要受容量和风控约束：全部 `desired_online=true` 账号都必须进入处理，不得设置隐藏的账号总量上限或内部前 N 个截断；探测和 warm 必须按显式页大小分批、带抖动、可观测，未覆盖账号由后续页或后续 drain 继续处理。Telegram 网络探测并发和健康探测专用超时必须显式配置，分页、并发和单次网络超时只控制调度吞吐，不限制服务上线账号总量；探测结果必须按完成顺序流式返回，主线程逐条落库，不得等待整页全部结束后集中提交。主线程冻结本批账号和凭证后必须先提交并结束数据库读取事务，再进入 Telegram 网络调用；逐结果提交必须保留本批已加载 ORM 状态，禁止提交后对象过期触发逐账号隐式回表，使数据库连接抖动中断整个探测批次。健康探测必须使用一次性 Telethon client，并在成功、未授权或异常路径结束后立即断开，不得把数百个探测 session 留在 account-online 进程的持久业务 client cache 中；业务发送 / 监听所需的持久连接策略不因健康探测改变。每个并发健康探测必须在其探测线程内拥有独立 asyncio 事件循环，不能让所有线程再次汇聚到 process-wide Telethon 事件循环形成单核瓶颈；正常发送、监听和登录仍沿用业务生命周期与连接缓存。探测超时取消必须等待一个有界断连窗口后再返回；断连失败不得覆盖 `connect/is_user_authorized/get_me` 的原始错误，没有原始错误时断连失败必须显式失败。生产活跃账号池按 400+ 账号规模设计时，普通活跃探活周期不得短于 5 分钟，stale 窗口不得短于 15 分钟，单次 account-online drain 默认覆盖当前目标账号池量级；`last_probe_at` 保留每个账号的真实探测完成时间，但同一 drain 批次的 `next_probe_at` 与成功 stale 窗口不得早于该批最后一个网络探测完成时间再加对应间隔，否则整批耗时超过 5 分钟时，早完成账号仍会在批次结束前到期并形成无间隔满池重探。当本轮 probe 批次已经打满 drain limit 时，本轮不得继续批量 stale 标记剩余账号，必须优先让下一轮真实健康探测处理积压，避免部署重启或短时 backlog 后把健康账号成批打成 offline / warming。该策略用于避免“1 分钟重探活 + 4 分钟 stale”或“探活批次未清空就 stale 标记”导致单 worker 永远追不上并反复把真实账号打成 stale；暂停任务和低频来源必须使用更低频探活，不与运行中活群任务抢同一探活节奏。账号、凭证读取和状态落库必须留在数据库 Session 所在线程，子线程只执行 Telegram 健康检查。遇到 FloodWait、代理失败、session 错误或 Telegram 网络异常时必须按账号 / 代理 / session 维度 backoff，写入 `next_probe_at` 和失败原因，不得高频重试。Redis 可以保存连接锁、近期探测热状态和 worker 租约，但账号在线状态事实源必须落库，保证任务详情、运营中心和排障日志一致。

任务详情必须展示 AI 活跃群账号在线覆盖：目标账号数、应在线账号数、当前在线账号数、warming、recovering、login_required、blocked、最近探测时间、掉线原因 Top N 和预计恢复时间。运营人员看到发送量不足时，必须能区分是账号在线不足、目标权限不足、AI 质量不足、去重拦截还是风控限流。

#### AI 活跃群真人感生成策略

AI 活跃群的真人感目标不是让模型自由编造身份，而是让自动发言基于账号面具和任务上下文具备不同账号的表达差异、群聊接话关系、自然节奏、短期立场延续和低模板感。系统必须优先解决“所有账号像同一个模型”“每条都完整总结”“总是自己开话题”“前后态度断裂”等问题。

真人感生成以stable obligation/assignment/intent为独立业务单位。Planner只按aggregate reply/material/act-type合同分配本批最多20条plan unit，冻结账号、面具摘要、行为类型、上下文锚点、话题/老师、短期立场、禁用样例与长度约束并转`generation_pending`，不得直接把slots送Provider或创建normal Action。ai-generation worker为每个义务创建/读取唯一GenerationJob与variation；实现可在无数据库事务区间把多个独立job transport-batch给同一Provider，但每条job/round/evidence/结果独立CAS。部分失败只重试对应job并携带失败原因/禁用集合；主3轮+备用3轮用尽后只写immutable check-in handoff，Planner消费handoff创建ready签到Action，不吞目标、不重跑Provider。

账号面具用于定义账号对外呈现的一套稳定身份感、偏好、语气和互动方式，必须结构化、缓存化、短摘要注入。账号面具是产品层名称；历史代码和数据库里的 `voice_profile` / “表达卡”是兼容技术名，后续迭代逐步通过 API 展示别名迁移，不得破坏已有数据、权限和诊断字段。

账号面具必须满足：

- 面具字段包括面具名称、目标受众身份感、年龄段 / 阶段感、兴趣偏好标签、句长偏好、互动习惯、语气强度、用词偏好、表情倾向、是否爱追问、是否爱附和、是否偶尔轻吐槽、禁用表达。
- 面具性别统一固定为成年男性日常社交视角；面具名称、目标受众身份感、身份框架或短摘要中必须能明确看出男性 / 男生 / 男士 / 老哥 / 大哥 / 老板 / 先生等男性身份，不允许生成女客、女性账号或中性身份面具。面具字段不得使用色情、性交易、寻欢、夜场、楼凤、外围、招嫖等敏感交易措辞。
- 面具允许配置经历设定和消费经历设定；这些内容只作为账号长期表达约束使用，生成内容仍需遵守任务上下文、去重和质量规则，不得脱离上下文新增可核验事实、联系方式、价格、邀约或交易撮合信息。
- 面具是账号级全局资产，同一个账号在所有 AI 活跃群任务里使用同一张 active 面具；任务和目标群只能追加短期上下文、话题和立场，不能为同一账号生成另一套长期面具。
- 面具在账号初始化流程中批量生成；存量账号缺面具时，由“账号面具”一级菜单或任务启动前触发批量补齐。日常 Planner 只读取缓存，不每轮调用 AI 分析。
- 默认面具由 AI 按账号批量随机生成，但必须受严格提示词和差异度校验约束，避免所有账号都生成“自然、随意、真实”这类无效描述。
- “账号面具”必须作为一级菜单，不能继续藏在系统设置 Tab。该菜单至少包含“面具管理 / 账号代理 / 授权指纹 / 异常与审计”四个 Tab。
- “面具管理”支持列表、搜索、查看、编辑、重建、停用、版本回滚和审计查看；搜索至少覆盖账号展示名、username、手机号后四位、账号状态、面具状态、面具名称和更新时间。
- “面具管理”支持批量重建面具、批量停用 / 恢复面具、筛选缺面具账号，并展示批量生成结果的差异度和失败原因。
- “账号代理”按 `account_id + developer_app_id/api_id + authorization_id/session_role` 绑定代理节点。同一账号在不同 TG 开发者应用、不同 session key 和主 / 备用授权槽位下可以使用不同代理出口。账号代理配置入口在“账号面具”菜单内，系统设置只维护 Clash 订阅源池、主备优先级和同步健康，不负责授权槽位代理分配。“账号代理”支持选择账号中心分组做批量绑定范围，批量动作必须显式选择本地代理资源或已同步健康 Clash 节点、授权槽位和变更原因；绑定 Clash 节点时必须在授权槽位代理绑定中记录 `proxy_airport_node_id`，并复用 / 创建对应 `AccountProxy` 连接资源。批量动作按账号中心分组枚举账号，更新组内账号在目标 `session_role` 下所有已有 active 授权环境绑定，成功数按账号去重；跳过缺少授权环境、接码专用分组或不可见账号，并返回逐账号原因。批量绑定不得创建 Clash 订阅源、不得启用 / 禁用订阅源、不得自动创建缺失授权环境，也不得把“配置了代理”解释为任务已启用代理。
- “账号代理”必须展示每个授权槽位的 TG 开发者应用、session_role、当前代理节点、真实出口 IP、健康分、warmup 阶段、最近故障切换、绑定时间和最后使用时间。解绑、换节点或批量重排必须二次确认并写审计；换节点后该授权槽位重新进入 warmup。
- “授权指纹”按 `account_id + developer_app_id/api_id + authorization_id/session_role(primary/standby_1/standby_2)` 绑定客户端元数据，字段至少包含 `platform/device_model/system_version/app_version/lang_code/system_lang_code/lang_pack/region_code/client_identity_key`。不同 TG 开发者应用和不同授权槽位可以拥有不同指纹；同一账号的主 / 备用授权槽位不得复用同一 `client_identity_key` 或同一 `device_model + system_version + app_version` 组合。
- 修改授权指纹配置只影响下一次使用该授权槽位建立连接、重登或新 session 初始化时上报的 MTProto 客户端元数据。保存配置成功只能表示“配置指纹已更新”，不能声明 Telegram 远端授权设备型号已经立即变更；远端实际显示必须通过授权设备快照读取后作为“远端观测指纹”展示。
- “授权指纹”必须同时展示“配置指纹”和“远端观测指纹”，并计算一致性状态：`not_connected`（未连接）、`pending_effect`（待生效）、`observed_matched`（已观测一致）、`observed_mismatch`（观测不一致）、`unobservable`（远端快照缺少可比对字段）。不一致或不可观测时提示运营重登 / 刷新授权 / 人工检查，不能静默改写现有 session，也不能把“配置已保存”当成“远端已观测一致”。
- “异常与审计”展示代理缺失、指纹缺失、同应用多账号指纹重复、主备授权复用代理、同槽位多 active 代理、多出口 IP、配置与远端观测不一致、订阅同步失败、最近保存 / 解绑 / 重建 / 刷新操作。所有写操作必须记录操作者、原因、影响账号、TG 开发者应用、授权槽位、旧值摘要和新值摘要。
- Prompt 中只传一行短摘要，例如“男大感、短句、爱追问、少表情、轻微吐槽、避免绝对判断”，不能传完整历史。
- 面具可以包含目标受众身份感和经历 / 消费经历设定，但不能要求账号冒充真实用户、管理员、认证身份或某个具体自然人；不能包含需要事实证明的身份背书。
- 面具必须以数据库为事实源，支持人工查看、编辑、版本回滚和审计；Redis 只能缓存 `short_prompt_summary` 等热读字段，缓存失效后必须能从数据库恢复。
- 面具编辑按generation-policy/intent revision对尚未call-issued的current obligation生效；已ready且带冻结面具版本的Action继续使用原payload，除非专项Gateway guard判revision不合法并由lifecycle adoption收口。存量缺面具旧Action只能在final takeover manifest中分类为safe legacy alias并终结/重物化current intent；普通Planner不得原地`voice_profile_replan`旧Action或释放legacy reservation。

面具初始化提示词必须满足：

- 每张面具必须输出结构化字段，不能只给“自然、随意、真实、像真人”这类泛化词。
- 每张面具必须固定男性身份；提示词和服务端校验都必须拒绝女性账号 / 中性身份面具，偏好里可以描述偏好的女性类型，但账号自身身份不能变成女性或中性。
- 同批账号必须显式拉开差异，覆盖不同目标受众身份感、句长、互动习惯、语气强度、口头习惯、表情倾向、年龄段、兴趣偏好和消费经历设定。
- 每张面具必须给出 3-5 条可执行表达原则和 3-5 条禁用表达。
- 每张面具必须生成一行 `short_prompt_summary`，供 Planner 低 token 注入。
- 生成后必须做差异度检查；同批面具过于相似时，失败项重新生成，不能直接启用。
- 面具内容不得引用 AI 已发送消息作为正向样本，避免账号人设被 AI 自我污染。

消息行为规划由规则完成，不交给 AI 自由决定。Planner 每轮先根据群聊状态、目标发送量和最近发言历史生成消息行为配比：

| 行为类型 | 使用场景 | 约束 |
| --- | --- | --- |
| `context_reply` | 群里有真人消息、@ 对象、问题或图片 | 优先使用；必须绑定被回复消息或上下文锚点 |
| `short_react` | 轻量附和、补一句短反应 | 必须短，不输出总结型废话 |
| `question` | 追问细节、引导别人接话 | 不能连问同一个对象或同一类问题 |
| `detail_follow` | 围绕当前对象补一个小角度 | 必须贴当前讨论对象，不能编账号面具或上下文之外的具体经历 |
| `light_disagree` | 轻微犹豫、保留意见 | 低频使用，不能制造冲突 |
| `topic_shift` | 当前话题断掉后轻转场 | 只在空闲场景低频使用 |
| `check_in` | `mask_missing`且原义务为未完成coverage direct，或coverage已完成的direct extra-volume在主/备用各3轮耗尽并已有immutable handoff；scoped账号本Task/群/任务日尚未签到 | Planner持有scoped claim后发送精确`签到`；记录`content_source=check_in`、精确trigger reason与六轮证据（如适用），只完成原义务且不计高质量AI文本；reply不得降级 |
| `silence` | 仅适用于没有开放群日/账号义务的可选自然对话 slot | 可以取消可选 slot；不得删除、抵扣或延后开放履约义务 |

历史配置、旧 action、旧短期立场记忆或测试数据里出现的 `light_question`、`side_comment`、`experience`、`追问`、`提问`、`问细节`、`观望`、`保留` 只允许在兼容层读取，并必须在进入 Planner 内部、AI Prompt、Action payload、任务详情、短期立场 DB 写入和 Redis 热缓存前归一为 PRD 词表；未知历史值按兼容口径归到 `detail_follow`，不得继续暴露原始旧值。线上观测、质量统计和 PRD 均只使用上表词表。

默认配比不固定写死。存在真人上下文时，`context_reply`和`short_react`应高于暖场；群聊空闲时可使用少量`topic_shift`或`question`。连续暖场、重复风险高或质量不足时先更换variation：主/备用模型各最多3轮；两级均无候选时仅coverage已完成的direct extra-volume经Generation handoff→Planner转精确`签到`，未完成coverage只允许`mask_missing` direct分支，其他情况显示`content_capacity_gap`。

上下文接话优先级：

1. 当前群最近真人消息、@ 对象、问题、图片、榜单或讨论对象。
2. 本任务配置的话题方向和讨论老师。
3. 租户画像的话题权重和句式模式。

只有前两层都不足时才参考画像做轻量暖场。引用回复、接话回复和对象讨论必须能追溯到目标群实时上下文或任务配置，不能把画像里的历史话题当作当前事实。

账号短期立场记忆用于保持同一账号前后态度连续。系统需要按账号 + 目标群记录最近 24 小时到 7 天的轻量状态：最近说过的主题、讨论老师、态度倾向、是否刚夸过、是否刚质疑过、是否处于观望。生成时同一账号不能在缺少新事实的情况下从“我再看看”突然变成“这个绝对可以”，也不能短时间反复夸同一对象同一优点。

短期立场记忆采用数据库 + Redis 双层：数据库保存最近 24 小时到 7 天的轻量事实源，Redis 缓存最近几轮的热状态。Dispatcher 成功发送或进入 `unknown_after_send` 后必须回写数据库并刷新 Redis；Redis 丢失时只能造成一次读取变慢，不能造成账号立场丢失或发言逻辑降级。

Redis 允许缓存：

- 账号面具短摘要。
- 账号在目标群最近几轮的 act_type、语义簇、讨论老师和话题方向。
- 本轮 slots 分配和短期去重热 key。
- 质量过滤过程中的临时计数。

Redis 不允许作为唯一存储保存长期账号面具、面具版本、人工编辑结果、审计记录、消息记忆事实或短期立场事实。

轻口语化必须可控：

- 允许短句、省略、轻微停顿、自然口语词和少量半句话。
- 不允许为了像真人而批量制造错别字、乱码、低智表达、夸张情绪或账号面具 / 上下文之外的虚假经历。
- 不完整表达必须低频出现，并且不能影响事实清晰度和风控安全。

AI 味废话必须单独拦截。没有具体对象、事实锚点、追问点、态度变化或信息增量的泛化评价应丢弃或重写，例如“这个确实不错”“感觉挺靠谱”“可以关注一下”“有点意思”“看起来还行”。这类句子只有在明确接某条上下文、并带有具体对象或追问时才可放行。

成本控制要求：

- 面具缓存，不每轮重建。
- 消息行为配比用规则生成，不调用 AI。
- 每个模型阶段默认一次批量 AI 请求，输入 slots 数组；质量不合格时只补失败 slot、不重写已通过 slot，同一 active Provider key 下主模型最多 3 轮，随后备用模型最多 3 轮。
- 高质量文本优先；六轮均不合格时，仅coverage已完成的direct extra-volume可由Generation提交唯一handoff并由Planner消费为精确`签到`；未完成coverage只在`mask_missing` direct时由Planner直接签到。reply/强引用不得签到。统一记录`content_source=check_in`、精确trigger reason和原始拒绝证据，不能伪装为高质量AI文本；其他欠量写`content_capacity_gap`。
- 每个 slot 只传短上下文、短面具、短画像摘要和必要禁用样例。
- 当批量生成无法稳定区分账号口气时，最多按账号风格组拆成少量请求，不能退化为每条消息一次请求。

频道 AI 评论也必须额外经过质量规则：候选评论按近期已规划 / 已成功评论做语义去重；命中“参考价值、先收藏、角度不错、值得讨论、继续展开、支持一下、感谢分享”等模板簇时直接丢弃。缺面具、已切换到可用授权代理路线，或主 AI 3 轮加备用 AI 3 轮仍无候选时，原义务使用固定池中的单个 Unicode 表情文本并留下审计；仍是 `post_comment`，不是 reaction，回复关系不得丢失。

---

## 3.9 风控中心

### 页面目标

统一管理账号、代理、目标、内容、限流、冷却、处置队列和策略。

### 页面 Tab

| Tab | 内容 |
| --- | --- |
| 总览 | 当前风控等级、静默状态、处置队列 |
| 全局策略 | 抖动、批次间隔、静默时间、重试策略、账号小时/日上限 |
| 账号健康分 | 全平台统一账号健康分、风险等级、扣分原因、发送容量 |
| 代理资源 | 代理新增、编辑、检查、禁用、绑定授权槽位数 |
| 代理告警 | acknowledge、ignore、resolve |
| 命中记录 | 风控事件和命中原因 |
| 策略审计 | 风控策略、代理资源、代理告警和账号代理绑定的操作留痕 |

代理告警的 `ignore` 是临时忽略，不是关闭告警。前端必须要求填写忽略原因和忽略到期时间；后端没有原因或没有 `ignored_until` 时必须拒绝。忽略到期后仍按代理健康检查和新增命中重新进入观察或告警。

账号健康分列表必须把扣分原因作为独立列展示并纳入搜索。扣分原因至少覆盖登录状态、容量限制、低健康分、代理异常、账号安全快照和最近执行风险；不能只显示一个低分进度条而不解释原因。

账号健康分列表还必须把“非扣分失败”与“扣分原因”分开展示。非扣分失败用于解释近期目标、权限、任务或内容问题，例如无评论权限、群限制发言、目标未授权、任务已删除、内容规则拦截；这些记录不进入 `score_reasons`，也不降低账号健康分。

风控命中记录必须展示归因字段：

| 字段 | 说明 |
| --- | --- |
| 失败原因 | 原始错误归一后的中文原因，禁止只显示“未知错误” |
| 影响对象 | 账号、代理、目标、任务、内容 / 规则、系统运行 |
| 是否扣健康分 | 只有账号本体、账号运行环境、账号级限流为“是” |
| 处置入口 | 账号中心、运营目标、任务详情、规则中心、代理资源或系统日志 |
| 关联对象 | account_id、target_id、task_id、proxy_id、rule_id 等 |

### 主要按钮

| 按钮 | 行为 |
| --- | --- |
| 刷新 | 拉取最新风控汇总 |
| 编辑全局策略 | 打开策略编辑弹窗 |
| 新增代理 | 创建本地代理资源 |
| 编辑代理 | 修改代理名称、协议、端口、容量 |
| 检查代理 | 执行端口和 Telegram 连接探测 |
| 禁用代理 | 禁止该代理继续被调度 |
| 绑定代理 | 给账号授权槽位绑定代理 |
| 批量绑定代理 | 多账号批量绑定代理 |
| 确认告警 | 标记已知晓 |
| 忽略告警 | 一段时间内不再提示 |
| 解决告警 | 标记恢复并写审计 |

### 风控进入任务链路

```text
任务结构校验并直接创建
  -> 启动时检查账号状态、目标能力、代理、安全上限、冷却、规则
  -> 写入可见运行 blocker，不回滚合法 Task
  -> Planner 每轮重新检查
  -> Dispatcher claim 时最终检查 token bucket、in-flight、代理、目标能力
  -> 执行失败写 risk event
  -> 风控中心生成处置项
```

---

## 3.10 素材中心与 AI 内容边界

### 页面目标

素材中心独立维护表情包、头像包、图片、文件、组合消息等运营资产；系统设置维护 AI 供应商、提示词、黑话配置和素材运行配置。AI 内容策略由运营方案、规则中心和系统设置共同提供，素材中心只保存可复用素材资产及其 TG 缓存状态。

素材不是系统底座配置，而是日常运营资产。它可以被消息发送、运营方案、规则中心、任务创建向导、转发监听、AI 活跃群和账号资料初始化引用；权限点和审计归属必须从 `system.manage` 拆到 `materials.*`。

### 功能点

- Material 上传、批量上传、压缩包导入、编辑、分组、标签、禁用、缓存健康查看。
- 表情包库：支持普通图片伪表情、静态 sticker、animated sticker、video sticker 和 custom emoji 的批量入库、标签和能力状态。
- 头像包：支持批量上传头像素材包，供 TG 账号资料初始化随机、按分组或按规则选择。
- 图片、文件、自定义 emoji、组合消息等素材能力。
- 图片压缩包导入：素材中心支持上传 `.zip` 包，包内 `.png`、`.jpg`、`.jpeg` 图片按表情包分组、头像包或普通图片分组导入；单张图片不得超过 500KB。
- 压缩包导入不把 zip 自身作为可发送文件素材，zip 只是批量导入容器；包内非图片、隐藏目录、`__MACOSX`、超过 500KB 或 MIME 校验失败的文件必须跳过并展示失败原因。
- 批量导入结果必须展示包名、默认生成分组 / 素材包名称、解析总数、成功数、失败数、跳过数、重复数、超过 500KB 数和逐文件失败原因，不能静默丢弃。
- 素材引用关系：展示被消息发送、任务、规则版本、运营方案、账号资料初始化批次引用的情况。
- 素材缓存健康：展示缓存账号、缓存频道配置状态、TG 引用版本、FloodWait、失败原因和最近错误；未配置时跳转系统设置填写频道链接。
- 系统设置只保留素材缓存账号、缓存频道链接、上传大小限制、临时文件 TTL 和缓存队列运行参数。

### 素材执行原则

- 平台不把图片原文件当作永久业务主数据，重点保存素材元数据、资产版本、缓存引用和可重发状态。
- 发送时采用 `download_reupload`，由发送账号重新上传。
- 规则版本绑定素材策略，执行项固化本次使用素材和资产版本。

---

## 3.11 归档、运营数据和审计

### 归档中心

| 功能 | 按钮 |
| --- | --- |
| 创建归档 | 选择群/目标，创建归档任务 |
| 查看详情 | 查看消息、成员、上下文 |
| 重新归档 | 对失败或过期归档重新运行 |
| 导出 | 导出 JSON / CSV 等格式 |

### 运营数据

统计维度：

- 任务维度。
- 账号维度。
- 目标维度。
- 规则维度。
- 素材维度。
- AI 用量维度。
- 失败类型维度。

运营数据和运营中心必须先使用汇总读模型，再按需下钻明细。当前阶段不做分库分表，优先通过数据分层降低 PostgreSQL 压力：

| 数据层 | 代表数据 | 默认入口 | 要求 |
| --- | --- | --- | --- |
| 热写事实 | `actions`、`execution_attempts`、监听事件、AI 上下文、网关结果 | 任务中心详情、Recovery、失败下钻 | 只做短事务写入和按 ID / 时间范围查询 |
| 热读汇总 | `target_runtime_summary`、`task_runtime_summary`、`account_runtime_summary`、`operation_issue`、`daily_runtime_stats` | 运营中心、运营数据、任务列表状态卡片 | 后台增量维护，允许分钟级延迟 |
| 冷归档 | 历史执行明细、归档消息、成员快照、清理审计 | 归档中心、审计、导出 | 不参与首页和工作台实时查询 |

页面查询边界：

- 运营中心目标工作台只查热读汇总，不直接扫描 `actions` 和 `execution_attempts`。
- 任务中心列表可读任务级汇总；任务详情按 `task_id`、`action_id`、时间范围查明细。
- 运营数据报表优先读日汇总和维度汇总；跨全量明细的统计必须进入汇总任务或导出流程。
- 热写事实表默认近 5 天可下钻；超过保留期后以日汇总、归档和审计记录为主。

### 审计记录

审计对象：

- 登录、退出。
- 账号新增、登录、移除、同步。
- 资料初始化、设置二步密码、清理登录设备、备用 session 补齐 / 自愈、同步安全状态。
- 开发者应用、AI、素材、规则、风控策略修改。
- 任务创建、启动、暂停、停止、重试、删除。
- 导出和敏感查看。

---

## 3.12 操作手册

### 页面目标

给平台管理员、运营主管和运营人员提供登录后可直接查看的操作说明，覆盖日常操作顺序、上线前检查、任务类型选择、最近更新功能、按菜单操作、异常处理、权限和审计要求。

### 页面结构

| 区块 | 内容 |
| --- | --- |
| 日常操作顺序 | 系统基础配置、接入 TG 账号、进入运营中心查看目标工作台、处理账号 / 目标 / 规则异常、使用运营方案或任务中心发起执行、回到运营中心复盘 |
| 上线前检查 | TG 开发者应用、AI 服务、账号登录、同步资产、账号安全状态、运营目标、目标画像学习来源、规则版本、风控异常 |
| 任务类型选择 | AI 活跃群、转发监听群、频道浏览、频道点赞、频道评论/回复的适用说明 |
| 最近更新功能 | 运营中心日常入口、运营方案模板、目标画像独立、素材中心独立、任务创建动态向导、账号资产与可用性、数据汇总与延迟、导航升级、账号安全加固、批量资料初始化、任务内目标输入、账号-目标准入前置、手机号不脱敏展示 |
| 按菜单操作 | 运营中心、TG 账号管理、运营目标、目标画像、消息发送、素材中心、任务中心、监听中心、规则中心、风控中心、归档中心、运营数据与审计、系统设置 |
| 异常处理速查 | 运营中心目标异常、登录或账号不可用、任务预检查失败、监听无事件、内容被规则拦截、汇总数据延迟、执行结果复盘、账号未满足目标准入 |
| 权限与审计要求 | 菜单和按钮权限、敏感操作审计、自动执行前置确认 |

### 最近更新功能说明

| 功能 | 前端展示要求 |
| --- | --- |
| 运营中心日常入口 | 展示运营人员登录后先进入运营中心：先看目标工作台和 open issue，再展开关联任务失败；建议动作优先打开上下文弹窗 / 抽屉处理，复杂流程再深链跳转账号、目标、规则、风控或任务详情；关闭或返回后仍停留在原目标和原筛选位置 |
| 运营方案模板 | 展示运营中心下半部分的方案模板 / 策略模板，可创建、复制、暂停、恢复、生成任务草稿、生成并启动任务；应用到运行中任务前必须展示影响预览、二次确认和审计 |
| 任务创建动态向导 | 展示任务中心 5 步创建向导会按任务类型动态显示静态字段；默认使用快速创建，高级设置折叠，确认页只展示输入、静态规则和结构错误。容量、风控、准入、传输和 warning 在创建并启动后的详情展示 |
| 账号资产与可用性 | 展示账号中心不只看在线状态，还要展示完整手机号、账号分组、登录状态、同步资产、资料 / 安全状态、授权资产、primary session、standby_1 session、standby_2 session、备用 session 缺口、可激活恢复状态、可发送、可监听、可加入、可评论、可修改资料、可读取验证码、剩余容量、不可用原因和下次可重试时间；发送动作进入消息发送页 |
| 数据汇总与延迟 | 展示运营中心和任务中心列表默认读取汇总读模型，详情按目标、任务、action 或账号下钻；汇总延迟时显示最近更新时间、stale 标记和刷新入口 |
| 导航升级 | 菜单文案使用“运营中心”；素材中心作为一级菜单；AI 供应商、提示词、素材运行配置和后台账号权限位于系统设置 Tab |
| 素材中心 | 展示表情包库、头像包、图片 / 文件 / 组合消息素材、分组标签、批量上传、缓存状态和引用关系；消息发送、规则、AI 活跃群和账号资料初始化只引用素材中心资产 |
| 账号安全加固 | 展示账号详情的账号安全入口，可同步设备和 2FA 状态、清理外部设备、设置 2FA，并查看最近安全批次结果 |
| 批量资料初始化 | 展示先点击资料初始化、再选择账号的流程；账号列表必须拉齐当前分组完整数据后由 AntD Table 分页展示，不能只露出默认 50 条；支持按账号组、筛选、搜索、100 条 / 页、选择当前筛选前 100 个、选择资料待初始化、选择需重新资料初始化、备用 session 缺口筛选、健康备用 session 不足 2 个筛选、可从备用 session 激活恢复筛选、跨页勾选和区间选择；命名风格提示和 AI / 本地兜底预览必须做整批差异控制，可生成或手工编辑昵称、TG 姓名、username、简介、头像等资料，展示生成来源和 warning；昵称 / TG 姓名必须同时更新平台展示名和 TG 远端姓名；弹窗确认后创建批次执行，并在任务中心展示后台执行状态、头像缓存进度和失败原因 |
| 任务内目标输入 | 展示创建任务时可选择已有目标，也可粘贴群聊 / 频道 `@username`、公开链接、邀请链接或 peer id；编辑任务不展示新目标输入 |
| 账号-目标准入前置 | 展示频道任务会检查是否已关注；转发源群检查是否已加入 / 可读取；AI 活跃群和转发目标群检查是否可发言；未满足账号先按抖动节奏关注、加入或重新取得可发言能力，任务详情只展示状态和失败原因，失败账号不进入主互动 |
| 手机号展示 | 所有涉及账号或联系人的列表、联系人、消息发送、归档、风控、账号安全、审计和导出日志优先展示完整手机号；当前 `phone_masked` 仍是历史兼容兜底字段 |

### 完成标准

- 操作手册使用用户可理解的菜单和按钮口径，不展示内部表名、worker 细节或工程调试词。
- 最近更新功能必须和主设计文档、专项设计文档、任务中心和账号中心页面行为一致。
- 操作手册必须把运营中心写成日常入口：默认按目标查看异常，点开后看到关联任务失败和建议动作。
- 操作手册必须说明运营方案模板位于运营中心，不位于系统设置；任务中心只承载执行详情和调度控制。
- 操作手册必须说明素材中心是一级菜单，不位于系统设置；系统设置只保留素材运行配置。
- 操作手册必须说明汇总读模型存在延迟，运营人员看到 stale 标记时要刷新或下钻详情，而不是直接判断任务无异常。
- 任务相关说明必须明确“先检查准入状态”：未关注 / 未加入账号先关注或加入；AI 活跃群和转发目标群还必须具备可发言能力，成功后才互动，且 0 个账号准入成功则不进入主互动。
- 资料初始化、设置二步密码、清理登录设备、备用 session 补齐必须保持独立入口说明，不能重新合并成笼统的安全加固动作；四类批量动作都必须先点动作再选账号，并使用二次确认弹窗，不要求输入固定确认文案。
- 手机号展示说明必须和代码兼容字段分开：PRD 目标是不脱敏，所有涉及账号或联系人的前端展示、搜索和导出优先使用完整 `phone_number`，`phone_masked` 只表示历史兼容兜底。

---

## 3.13 前端页面设计

### 设计目标

前端页面必须把平台能力组织成运营人员能直接操作的工作台，而不是把后端表和工程参数平铺出来。每个页面都要回答四个问题：

- 当前对象是什么：账号、目标、任务、规则、素材、异常或统计。
- 当前状态是什么：正常、等待、失败、受限、不可用、汇总延迟。
- 现在能做什么：按钮、批量操作、上下文弹窗、处理抽屉、深链跳转。
- 失败后去哪处理：本页轻处理、抽屉中处理、深链任务中心或账号 / 目标 / 规则 / 风控，并保留返回上下文。

### 全局页面框架

| 区域 | 设计要求 |
| --- | --- |
| 顶部栏 | 展示当前平台实例、当前用户、刷新入口、全局告警和帮助入口 |
| 左侧导航 | 固定主导航：运营中心、TG账号管理、运营目标、目标画像、消息发送、素材中心、任务中心、监听中心、规则中心、风控中心、归档中心、运营数据、系统设置、审计记录、操作手册；AI 供应商、提示词、素材运行配置和后台账号权限在系统设置 Tab |
| 页面标题区 | 标题、副说明、更新时间、主按钮；不能把关键动作藏到表格行内 |
| 筛选区 | 搜索、状态、类型、时间范围、目标 / 账号范围；筛选项必须能重置 |
| 摘要区 | 使用 3-6 个摘要指标展示本页核心状态；摘要读汇总模型，不实时扫描明细 |
| 主内容区 | 表格、分组列表、工作台或向导；默认分页，禁止一次性加载大表明细 |
| 右侧 / 抽屉 | 用于创建、编辑、预检、详情、失败原因和确认动作 |
| 底部反馈 | loading、empty、error、stale、partial success 都要有明确提示和下一步动作 |

页面默认不使用“说明型大段文字”代替功能。需要帮助文案时放在操作手册或字段 tooltip；主页面优先展示状态、数据和按钮。

### 通用状态

| 状态 | 前端表现 | 用户可做 |
| --- | --- | --- |
| 加载中 | 骨架屏或表格 loading，保留上一次可用数据时标记“正在刷新” | 等待或取消 |
| 空数据 | 展示空状态和下一步主按钮，例如“新增 TG 账号”“创建任务” | 按引导创建 |
| 权限不足 | 隐藏不可用按钮，详情处显示“无权限查看 / 操作” | 联系管理员 |
| 汇总延迟 | 展示最近更新时间和“汇总可能延迟” | 刷新或下钻详情 |
| 部分成功 | 成功、失败、跳过分开展示 | 重试失败项或查看失败原因 |
| 后端错误 | 保留筛选条件，展示错误码、trace_id 和重试按钮 | 重试或复制 trace_id |
| 敏感查看 | 二次确认并写审计 | 输入原因后查看 |

### 页面设计总表

| 页面 | 首屏布局 | 主要操作 | 弹窗 / 抽屉 | 数据读取边界 |
| --- | --- | --- | --- | --- |
| 运营中心 | 上半部分目标工作台，下半部分运营方案 / 策略模板 | 刷新、创建方案、调整方案、处理异常、打开上下文处理、查看任务详情 | 目标异常详情、上下文处理弹窗、建议动作抽屉、方案编辑、深链返回确认 | 只读目标 / 任务 / 账号 / 异常汇总；复杂处理深链跳转并带返回上下文 |
| TG账号管理 | 账号状态摘要、账号身份 / 分组 / 筛选、账号资产表格、动作优先维护入口 | 新增账号、完成登录、同步资产、资料初始化、设置 2FA、清理非平台设备、补齐 / 刷新授权资产、提取验证码、重查待处理、移动分组 | 登录向导、账号详情、批量资料初始化、设置二步密码、清理登录设备、批次详情、待处理重查结果 | 列表读账号基础表和汇总；详情按授权资产、登录设备、验证码、可用性与容量、待处理执行闭环、执行记录分页读取 |
| 运营目标 | 目标摘要、目标类型筛选、目标表格、能力状态 | 同步目标、授权 / 能力调整、处理准入失败、带目标创建任务 | 目标详情、准入处理、能力调整、关联任务 | 列表读目标汇总；详情读账号覆盖和任务关联 |
| 目标画像 | 全站画像摘要、学习来源、样本状态、质量规则、版本状态 | 配置学习来源、刷新学习、向上拉取历史、配置样本质量规则、采纳 / 降权 / 剔除样本、重算候选、重建、回滚、清空 | 学习来源编辑、样本详情、质量规则编辑、历史拉取进度、版本列表、危险动作确认 | 只读全站唯一画像和样本分页；学习拉取、候选重算和重建异步执行 |
| 消息发送 | 快速发送表单、目标 / 联系人选择、素材选择、预检结果 | 保存草稿、发送前预检、提交发送、查看发送记录 | 目标选择、素材选择、预检确认、发送详情 | 手动发送记录分页读取 |
| 素材中心 | 素材类型摘要、缓存健康、素材表格、分组筛选 | 上传素材、批量上传、创建头像包、创建表情包分组、编辑、禁用、刷新缓存 | 素材详情、批量上传、分组编辑、引用关系、缓存错误 | 素材列表分页；大文件和批量导入异步处理 |
| 任务中心 | 任务摘要、任务列表、状态筛选、创建任务按钮 | 创建任务、创建并启动、启动、暂停、继续、停止、重试、重置、编辑、删除 | 创建任务向导、任务详情、Action 明细、执行尝试详情 | 列表读任务汇总；详情按 task/action 分页下钻 |
| 监听中心 | 监听健康摘要、源目标列表、监听账号和水位 | 刷新、切换监听账号、重置水位、查看事件 | 监听详情、最近事件、错误详情 | 默认读监听汇总；事件按 source 分页 |
| 规则中心 | 规则集列表、发布状态、命中摘要、测试器入口 | 新建规则集、编辑草稿、发布、回滚、复制、测试 | 规则编辑、版本对比、测试器、发布确认 | 列表读规则集；测试单次调用后端 |
| 风控中心 | 全局策略、统一账号健康分、代理告警、处置队列 | 编辑策略、代理检查、处置、解除限制、查看命中 | 策略编辑、代理详情、处置确认、命中详情 | 读风控汇总；命中记录分页 |
| 归档中心 | 归档任务状态、消息检索、成员快照 | 创建归档、检索、导出、查看成员 | 归档详情、消息详情、导出确认 | 冷数据检索分页，禁止首页全量扫描 |
| 运营数据 | 日期筛选、目标 / 任务 / 账号 / AI / 规则报表 | 刷新、导出、下钻目标 / 任务 | 报表详情、导出确认 | 优先读日汇总和维度汇总 |
| 系统设置 | 配置分组 Tab：开发者应用、AI 供应商、AI 黑话、提示词、素材运行配置、后台账号权限、运行配置 | 新增、编辑、检查、启用、禁用、保存权限、保存素材运行配置 | 配置编辑、供应商编辑、提示词编辑、权限编辑、健康检查结果 | 只读配置表和健康摘要；不承载素材日常上传和批量管理 |
| 审计记录 | 时间筛选、操作者、动作、对象、结果 | 查看详情、导出 | 审计详情、导出确认 | 按时间和对象分页 |
| 操作手册 | 日常顺序、上线前检查、任务类型、异常速查 | 搜索、复制步骤、跳转页面 | 无强制弹窗 | 静态内容和少量状态链接 |

### 运营中心页面

运营中心是运营人员默认入口，不是工程监控页。

首屏布局：

```text
页面标题：运营中心
  [刷新当前数据] [新建运营方案]

目标工作台
  目标状态摘要卡
  异常目标列表
  目标效果趋势

运营方案 / 策略模板
  群活跃方案
  频道互动方案
  转发监听方案
  账号使用策略
```

目标工作台默认按运营目标展示，不按任务展示。目标行展开后展示关联任务失败、影响账号、主要 failure_type、建议动作和上下文处理入口。运营中心不展示 action 原始 payload；需要原始失败事实时深链跳转任务中心，并带回运营中心原目标、原 issue 和筛选状态。

运营方案区用于配置业务策略模板，例如 AI 活跃群的接话 / 暖场策略、频道互动强度、转发监听规则组合、账号使用策略。账号使用策略只配置账号范围、冷却、容量、换号和恢复建议；账号安全、资料初始化、2FA 和设备清理仍跳转 TG 账号管理执行。保存方案后可以生成任务草稿或调整已有任务配置。

方案模板在前端上必须是“可生成任务的业务卡片”，不是系统配置表。卡片操作包括查看效果、编辑方案、生成任务草稿、生成并启动任务、调整关联任务、暂停方案。编辑方案后必须先展示影响预览，说明会影响哪些目标、哪些任务、哪些规则版本和哪些账号范围。

### TG 账号管理页面

账号管理页面由四层组成：

```text
摘要层：账号总数、普通运营账号、接码专用账号、在线、登录有问题、部分掉线、全部掉线、非平台设备、受限 / 封禁、同步过期、资料待初始化、待处理执行闭环
筛选层：账号身份、分组、登录状态、账号状态、授权资产、登录设备、可用性、容量、安全、资料、同步、批次、代理、执行闭环状态
列表层：账号资产表格 + 资料初始化 / 设置二步密码 / 清理登录设备 / 补齐备用 session / 同步安全状态 / 提取验证码 / 重查待处理
详情层：按六层事实拆分的账号详情 Tab + 账号维护批次中心 + 执行记录聚合
```

账号管理页只做账号资产和维护，不承载消息发送、联系人发送、运营方案或风控处置队列。账号分组默认只是资源分类和选择范围，“进入账号分组”后只能管理组内账号和维护动作，不能出现人员发送、联系人发送、消息编辑或素材选择；这些流程统一在消息发送页。系统固定“接码专用分组”例外：它是任务参与硬边界，组内账号禁止进入所有运营任务候选池，也禁止一键清理其他登录设备。

新增账号使用登录向导：选择开发者应用、输入手机号、选择验证码 / 扫码、处理验证码、处理 2FA、登录成功后同步资产。登录流程的每一步都要可恢复，刷新页面后能继续当前登录状态。

账号详情必须按六层事实承载不同问题：基础资料、登录 / 验证、授权资产、登录设备、同步资产、可用性与容量、TG 官方验证码、待处理与执行闭环、账号安全、托管 2FA、维护批次、执行记录、审计记录。授权资产 Tab 使用 primary、standby_1、standby_2 三个槽位卡展示 session 健康、开发者应用、代理、Telegram 授权设备摘要、最近健康检查、失败原因和补齐 / 激活恢复 / 刷新掉线槽位入口；三槽位全部掉线时只进入人工重新登录、扫码或手动验证码。执行记录必须聚合手动发送和 Task/Action 发言、评论、回复、频道互动、AI 活跃群发言，不能只读取旧 `message_tasks` 导致账号详情显示 0 条。批次中心展示资料初始化、设置 2FA、清理设备、备用 session 补齐 / 自愈四类批次，并支持失败项重试。

运营中心不展示账号管理式的受限账号清单。只有当账号问题影响目标、任务容量或运营效果时，运营中心展示影响摘要、关联目标 / 任务和上下文处理入口；轻量原因查看和同步动作可在弹窗 / 抽屉内完成，复杂登录、资料初始化、2FA、设备清理等明细处理回到账号管理或风控中心，并保留返回运营中心原位置。

### 运营目标页面

运营目标页不承担“任务创建前必须先建目标”的旧流程。它负责管理已经沉淀的群、频道、讨论组、联系人和账号-目标关系。

页面结构：

- 摘要：目标总数、可发送、可监听、准入失败、目标权限异常。
- 目标列表：目标名称、类型、username / peer、能力、授权状态、关联任务、最近异常。
- 目标详情：基础信息、能力、账号覆盖、准入状态、关联任务、近期效果、审计。
- 准入处理：展示未关注、未加入、已加入但不可发言、验证失败账号，并允许重试或标记人工处理。

从任务中心粘贴新目标创建任务后，目标页应能看到该目标的来源、创建任务和后续准入结果。

### 消息发送页面

消息发送是手动发送和小批量发送入口，不替代持续任务。

页面结构：

- 左侧发送表单：发送账号范围、目标类型、目标 / 联系人、消息内容、素材、发送时间。
- 右侧预检结果：账号可用性、目标能力、风控限制、规则命中、预计发送量。
- 底部发送记录：状态、账号、目标、失败原因、重试入口。

提交前必须先预检。预检阻塞时不能提交；预检 warning 时允许管理员确认继续。发送后进入发送记录，失败事实仍可被运营中心上卷。

### 任务中心页面

任务中心是执行详情和调度控制台，不是日常发现异常的唯一入口。

任务中心可创建的普通主任务为 6 类：AI 活跃群、转发监听群、频道浏览、频道点赞、频道评论/回复、搜索目标群点击。准入前置子任务和资料初始化批次属于系统任务 / 子任务投影，不进入普通创建向导，也不增加普通运营任务类型；搜索排名观察继续使用自身灰度入口和账号用途边界。

系统任务 / 子任务投影展示规则：

| 类型 | 来源 | 列表展示 | 可用操作 |
| --- | --- | --- | --- |
| `target_membership` | AI 活跃群、转发目标群和需要准入补齐的频道任务自动创建或复用 | 可作为父任务的内嵌子行展示，也可在列表勾选“显示系统子任务”后单独展示；必须显示父任务、目标、准入进度和当前瓶颈 | 查看详情、暂停子任务、继续子任务、重试失败项；不能从创建向导直接新建 |
| `account_profile_init` | 账号中心资料初始化批次投影 | 只读系统任务行，显示批次状态、头像缓存、失败原因 | 查看、刷新、跳转账号批次详情 |
| `account_device_cleanup` | 账号中心清理登录设备批次投影 | 只读系统任务行，显示外部设备清理、保留平台 session、等待限制和失败原因 | 查看、刷新、跳转账号批次详情 |
| `account_2fa_setup` | 账号中心设置二步密码批次投影 | 只读系统任务行，显示 2FA 设置 / 替换、跳过、待邮箱确认和失败原因 | 查看、刷新、跳转账号批次详情 |
| `account_standby_session_provision` | 备用 session 自动补齐 / 自愈批次投影 | 只读系统任务行，显示 standby_1 / standby_2 登录补齐、验证码读取、2FA 使用 / 新密码轮换、激活恢复和失败原因 | 查看、刷新、跳转账号授权资产详情 |

任务列表默认展示父任务，并在父任务行内显示准入摘要；运营人员展开父任务或开启系统子任务筛选时，才看到 `target_membership` 子任务行，避免列表被准入账号明细刷屏。

任务列表：

- 摘要卡：运行中、失败、暂停、积压、最老等待、最近失败类型。
- 筛选：任务类型、状态、目标、账号范围、来源方案、失败类型、时间。
- 表格列：任务、类型、目标、状态、进度、失败、准入状态、下次运行、来源、操作。
- 默认在表格上方提供类似账号中心“账号分组”的快捷分组筛选条，按“目标群聊 + 关联频道”生成任务分组。快捷项展示分组名称和任务数量；点击后下方仍展示普通任务表格，任务行保留启动、暂停、继续、停止、重试、重置、编辑、删除和详情等操作。
- 当分组数量多或分组名称较长时，快捷分组必须使用下拉选择或可完整换行的控件，不能用单行横向分段控件把分组截断、挤出屏幕或导致后续分组不可选。
- AI 活跃群和转发监听任务优先使用目标群聊作为分组主键；频道浏览、频道点赞、频道评论 / 回复任务优先使用目标频道作为关联频道，并在可解析到讨论组 / 关联群时归入对应目标群聊。未解析到目标群聊或关联频道时必须显式显示“未关联群聊”或“未关联频道”，不能静默落入空白分组。
- 任务类型筛选和搜索先作用于任务行，再用过滤后的任务行生成顶部快捷分组；切换快捷分组时表格回到第一页，分页统计以当前可见任务行为准。

任务详情弹窗必须重构为“顶部摘要 + Tab + 二级详情”：

- 顶部固定摘要：Task 主状态、履约状态、目标数、真实确认数、在途、未知、欠额、deadline、预测安全容量、blocking codes 和最近异常；AI 再显示可发言/准入账号，频道任务按消息下钻，纯搜索只显示 click。
- Tab 1：运行概览，展示统一履约快照、逐粒度进度、原因分解和当前瓶颈；`running + blocked/at_risk/missed` 必须显著可见。
- Tab 2：准入前置，展示准入子任务、账号级状态、待验证 / 人工处理 / 失败原因。
- Tab 3：AI 轮次，展示 Cycle / Turn、请求条数、AI 返回条数、质量过滤条数、最终计划条数、上下文、账号记忆、账号面具版本 / 摘要 / 匹配分、短期立场、消息记忆 ID、语义簇和行为类型。
- Tab 4：执行计划，只展示未来 pending / executing action，支持按类型和账号筛选。
- Tab 5：执行记录，只展示历史执行结果，默认分页，不在弹窗中无限平铺。
- Tab 6：配置，展示当前配置、推荐值差异和保存后是否会重排未来计划。

准入前置 Tab 点击账号行时，必须打开二级弹窗或右侧抽屉，而不是在父弹窗继续平铺表格。二级详情包含：

- 账号基础信息、当前准入阶段、所属父任务和子任务。
- 入群记录、关联频道关注记录、验证记录、可发言复检记录。
- 验证问题、AI / MiMo 识别内容、置信度、答案、结果。
- 原始 TG / API 错误、失败类型、处理建议。
- 操作按钮：重试准入、重新检测可发言、标记人工处理、跳过账号。

创建任务向导固定 5 步：

1. 选择任务类型。
2. 选择或输入目标。
3. 配置类型参数。
4. 选择账号范围和节奏。
5. 确认创建。

向导必须按任务类型动态切换字段。AI 活群只展示群日目标、话题、语气、AI 黑话、账号角色、无人续聊、全账号覆盖、账号轮换说明、0～3 个配置预关注频道和准入策略；单批规划量是后端运行态，不是运营数量字段。频道评论 / 回复展示每条评论 / 回复目标、每条频道消息最少引用回复数和讨论区不可用提示。纯搜索点击只展示单一批准目标、关键词、每日 click 目标、账号组和截止时间，不展示静默、速率、Window、入群开关、admission-ready 目标或理论容量。容量与 blocker 只能在 Task 创建并启动后的详情中展示。

AI 活跃群和频道评论 / 回复不再使用小时/静默速率作为执行合同：

- AI活群不向运营暴露旧硬小时上限、静默停止门禁或`每小时最大发送量`；运行时仍必须读取任务日冻结的`natural_full_day` 24小时curve与非零权重生成active DueRankSet/due_at，静默只改变权重而不形成停发。Generation/interaction并发只表示当前资源，不改变群日目标。
- AI 活跃群单批 Turn 数由当前群日债务、账号覆盖欠额和开放队列空间自动计算；只控制本轮数据库批量，不向运营暴露为数量目标、单账号上限或风控上限。
- `每轮最少引用回复数` 只决定 AI 活跃群本轮至少多少个 Turn 必须是 Telegram 原生引用回复。新建任务默认值和最小值均为 1；普通发言或签到不能冒充引用回复。当前没有合格的本 Task 我方历史成功候选时，后端记录 `reply_target_shortfall`，不把字段降为 0，也不得回选群内其他人的消息。
- `预计每条评论 / 回复` 只决定频道评论对每条频道消息的累计目标，Planner 每轮只补差额，不重复为同一消息满额生成。
- `每条频道消息最少引用回复数` 只决定单条频道消息补计划时至少多少个 action 必须回复讨论区已有消息。新建任务默认值和最小值均为 1；直接评论或单表情兜底不能冒充引用回复。当前没有合格候选时，后端记录 `reply_target_shortfall`，不把字段降为 0。
- 本次发送门禁修复不得新增、删除、重置或重新推荐 AI 活群/评论既有账号面具 emoji 习惯、正常文本 emoji、图片、表情包、sticker、custom emoji 占比和素材规则。相同任务配置、上下文及随机种子下，删除门禁前后的 direct/reply 槽位、正常文本 emoji 决策和正常素材选择必须一致；只允许排期、领取时机及显式 fallback 标记变化。
- AI活群reply/material/act-type占比由aggregate `ContentAllocationPlan/RequirementAssignment`跨最多20条技术批次守恒；Generation/Dispatcher claim、失败重领或静默权重变化不得重置分母、重复套用`reply_min_*`或重新获得素材额度。旧Cycle/ContentMix只作takeover alias/兼容投影。
- AI 活群不再使用参与比例降低日覆盖；Planner 必须优先补当天未覆盖账号，再公平补群日总量。
- AI 活群抖动只影响本轮数量、时间和账号顺序，不得形成空小时；频道评论仍不得突破其账号/任务小时上限。
- 前端必须在字段旁展示口径说明和推荐值来源：AI 群日目标说明“本任务当日动态必达账号每人至少 1 条，计划目标取配置目标与当前必达数的最大值；已确认数只进入进度和超发审计，不反向抬高计划”，24 小时权重说明“静默降量不停发”；频道评论的小时预算继续使用频道专项说明，不得复用到 AI 活群。
- 任务中心列表和详情必须展示AI活群、频道浏览、点赞、评论的今日账号参与覆盖；AI按current allocation plan/assignment与active due-rank分组展示唯一参与账号数、义务数、ready Action数和bound fact数，不再用legacy Cycle/Action数作为完成分母。
- AI 活跃群选择“全部可用账号”时默认启用任务内动态日覆盖。任务日只冻结 `task_day_ledger_id`、时区和业务截止边界；范围身份为 `(task_id,target_group_id,account_id,task_day_ledger_id)`，并以独立 `scope_fact_version` 从当前身份、授权、代理、membership、can_send 和 admission 事实维护。存在合法自动恢复路线时为 recovering 并保留必达义务；当前事实版本无路线时为 abandoned_for_day 并释放未进 Gateway 义务；同日只有权威事实版本变化才可建立新 scope version，旧 Action 不复活。正常正文、签到边界、可见性和远端成功合同保持不变；完整状态见 `task-fulfillment-contract-closure-prd.md` §2。
- coverage obligation绑定账号不可静默转派。账号暂不可发时，未call-issued的同一stable obligation/FOP按typed dependency进入waiting/open并保留active rank；只有权威scope abandon按专项CAS取消或转extra-volume。normal旧Action可证pre-Gateway终结，但不得释放/重建业务义务。coverage确认只来自同账号canonical fact的唯一quantity binding与projector，Action/Attempt只作provenance。
- reply source/context漂移只使对应assignment/intent/GenerationJob按expected revision重评估；同plan其他direct义务不连带失效。normal body在Generation accepted variation+memory后才产生ready Action，Dispatcher只消费ready Action，不做延迟AI生成或读取旧Cycle。
- 到期coverage没有reply source时，Planner只有在aggregate assignment允许direct时才冻结同一stable direct assignment/intent并转`generation_pending`；该coverage义务的normal 3+3耗尽进入`content_capacity_gap`，不能借`normal_generation_exhausted`签到。只有`mask_missing`才可走coverage direct签到；要求reply的义务不得改成direct。
- active-rank到期缺口必须在同一ledger/target快照对bound fact、committed Gateway hold、unknown与有效pre-call owner做集合anti-join；仍有gap时即使存在少量open/ready Action也继续物化其他ready账号，禁止用reserved/sending分别count相减、open-action门禁或平台全表扫描截断。
- reply/强上下文assignment只在近端合法窗口创建Generation work；窗口外义务保持未物化并由typed wake在下一轮用新context生成，不得提前创建一小时远期pending Action后整轮过期。
- AI 活跃群创建提交后先创建父任务；创建并启动或后续启动时，系统建立或复用准入前置子任务。创建成功页必须明确“运行评估将在启动后进行”，任务详情展示准入子任务，不能让运营误以为所有账号已经可发言。
- 配置变更必须按作用域处理：运行中的 `daily_message_target` 与 timezone 只写下一 TaskDayLedger 的 pending revision，当前 ledger/pacing/due_at 不改写；账号范围在 current task-day 以 scope revision 动态加入/退出，使用 stable obligation/coverage/target CAS，不清理 ordinal。引用/素材/准入规则只影响未绑定 unit，既有 immutable intent 不原地改写；安全撤销可类型化终结尚未 call-issued 的 owner。已经 call-issued、Gateway-started/unknown、confirmed 和历史事实不回滚。

AI 活跃群和频道评论 / 回复的推荐值可以按账号范围做轻量计算，但不得把准入或容量诊断变成创建门禁：

- 新建任务时，前端在第 2-4 步选择目标、账号范围或节奏变化后可以触发轻量推荐计算；推荐请求失败或外部运行事实不可用不得阻止结构合法的创建。AI 只回填未被用户手动改过的群日目标和非零分布权重。
- 用户手动修改过的数量字段必须标记为“已手动设置”，后续账号范围变化只提示新推荐值，不静默覆盖。
- 编辑已有任务时不得自动改线上运行配置；页面展示“按当前账号数推荐”的对比值和“一键应用推荐”按钮，用户保存后才生效。
- 确认创建页只展示当前配置值、当前任务合格账号预计和动态生效公式；当前必达/recovering/abandoned/completed、AI 生效群日目标、累计进度、多任务获配份额、运行容量缺口和频道运行预算在启动后的任务详情展示。
- 推荐值必须来自后端同一套计算口径或前后端共享的等价规则；前端不得用与后端不一致的写死默认值。
- AI 活跃群运行详情必须展示本批运行漏斗：请求 Turn 数、主/备用 AI 候选与轮次、质量过滤数量、预计/实际签到兜底数、最终 Action 数和减少/等待原因；该批次不成为运营数量字段。
- AI 活跃群只配置每群 `daily_message_target`，粒度为 `task_id + target_group_id`；多任务/多目标每个 task/group 独立填写、建账、并发和验收。持久base等于配置值，effective等于`max(base,current_required_account_count)`；兼容planned/effective API字段只读映射到current effective，已确认数不反向抬高计划。创建/编辑页展示配置、base/effective revision与动态公式；高于当前预测容量时只展示每任务需求、实际并发和缺口，不得阻止合法规划，也不得把 pending、skipped、failed、unknown_after_send、late或unproven计入on-time成功。

#### 引用回复前端配置设计

引用回复配置必须在任务创建和编辑页明确可见，不能只作为高级 JSON 或隐藏字段。前端只暴露“最少数量”，不提供“引用真人消息 / 引用自己历史消息”的来源选择器。

AI 活跃群配置：

| UI 位置 | 控件 | 默认值 | 前端校验 | 文案说明 |
| --- | --- | --- | --- | --- |
| 创建向导第 3 步“任务配置”的内容编排区 | 数字输入 `每个逻辑 Cycle 最少引用回复数` | 1 | 必须为整数，最小 1；不与已删除的手动每轮数量字段联动 | `该数量包含在群日总发送量内，不额外增加发送量；Cycle Turn 少于配置值时实际要求取二者较小值，没有合格候选时任务显示引用短缺` |
| 编辑任务弹窗“AI 活跃群配置” | 同一数字输入 | 读取现有配置，旧任务缺失时按 1 展示并标注待迁移 | 保存前同创建校验；变更后展示“会影响后续规划，未来未执行主互动 action 将按重排规则处理” | `系统自动从当前群可回复消息和历史成功发言中选择引用对象` |
| 确认创建页 | 只读字段摘要 | 当前表单值 | 只校验正整数和内容合同结构，不查询运行引用池 | `每个逻辑 Cycle 最少引用 X 条；实际引用池与短缺在启动后展示` |

AI 评论 / 回复配置：

| UI 位置 | 控件 | 默认值 | 前端校验 | 文案说明 |
| --- | --- | --- | --- | --- |
| 创建向导第 3 步“任务配置”，紧跟“预计每条评论 / 回复” | 数字输入 `每条消息最少引用回复数` | 1 | 必须为整数，最小 1；不得大于 `预计每条评论 / 回复` | `该数量包含在单条消息补差额内，不额外增加评论目标；没有合格候选时任务显示引用短缺` |
| 编辑任务弹窗“频道评论配置” | 同一数字输入 | 读取现有配置，旧任务缺失时按 1 展示并标注待迁移 | 保存前同创建校验；变更后展示“只影响未来未执行 / 未规划的频道消息补差额” | `系统自动从已采集讨论区评论和历史成功评论中选择引用对象` |
| 确认创建页 | 只读字段摘要 | 当前表单值 | 只校验与单条评论目标的结构关系，不查询运行讨论区 | `每条频道消息最少引用 X 条；实际讨论区能力与短缺在启动后展示` |

前端交互规则：

- 字段只在对应任务类型展示：AI 活跃群显示 `每轮最少引用回复数`；频道评论 / 回复显示 `每条消息最少引用回复数`；其他任务类型不展示。
- 该字段属于“内容编排核心字段”，不放入高级折叠；AI 侧与群日目标并列展示，评论侧与每条评论目标并列展示，便于运营理解它不增加总量。
- AI 字段只校验正整数；逻辑 Cycle Turn 少于配置值时，冻结合同使用 `min(reply_min_per_round, logical_cycle_turn_count)`，页面必须展示实际值，不得增加 Cycle 或群日总量来凑引用。评论字段仍不得大于每条评论 / 回复目标。
- 当账号范围、目标、群日目标或每条评论目标变化时，引用回复字段不被自动覆盖；只展示与当前内容作用域的合法性提示。
- 前端不展示引用对象多选框，不允许运营手动选择具体消息；引用对象选择属于 Planner。
- 确认页必须把引用回复作为独立摘要展示，避免运营只看到总发送量 / 总评论量。
- 详情页和 Action 明细必须用标签区分 `普通发言`、`引用回复`、`普通评论`、`回复评论`，并展示引用作者和预览；缺字段时显示 `-`，不能伪造引用摘要。

任务详情顶部必须先展示准入前置，再展示主执行明细。Action 明细和执行尝试按需展开加载，不能在详情打开时一次性加载全部明细。

任务中心还必须展示账号资料初始化、清理登录设备、设置二步密码和备用 session 自动补齐批次的后台执行状态。这类任务由账号中心或账号安全 worker 发起，不在任务中心创建向导中创建；列表中按“账号资料初始化 / 清理登录设备 / 设置二步密码 / 备用 session 补齐”展示。详情展示批次 ID、账号总数、成功、跳过、失败、等待、正在执行、最近失败原因和账号级结果。任务中心对这类系统任务只提供查看、刷新和跳转账号批次详情，不提供启动、暂停、删除等普通运营任务控制。

系统任务详情必须复用账号批次详情组件，并按 `account_security_batch.system_task_type` 切换展示列：

| 系统任务类型 | 详情重点 |
| --- | --- |
| `account_profile_init` | 资料、username、头像、头像缓存、AI 预览和重抽结果 |
| `account_device_cleanup` | 请求/可执行/跳过数量与跳过原因、三槽与我方历史授权的 protected hash、worker 执行开始时冻结的非我方设备目标、待识别/读取失败/集合漂移、接码专用账号阻断原因，以及清理后目标消失且保护集仍完整的 exact-set 回读；不展示等待倒计时 |
| `account_2fa_setup` | 平台托管 2FA 设置 / 替换、待邮箱确认、旧密码未知跳过和失败原因 |
| `account_standby_session_provision` | 目标槽位、开发者应用、代理、验证码读取、2FA 使用、健康检查、激活恢复和失败原因 |

从系统任务跳转账号详情时必须携带 `tab=authorizations|security|profile|batches` 和 `batch_id`，返回任务中心时恢复原筛选和滚动位置。

### 监听中心页面

监听中心展示事件采集是否健康。

- 摘要：监听源数、运行中监听、积压事件、停滞源、最近错误。
- 源列表：源目标、监听账号、水位、最后消息、积压、关联任务、错误。
- 详情：最近事件、过滤结果、源媒体缓存状态、监听账号切换记录。

监听中心不直接发业务消息；它只负责读取事件、维护水位和暴露监听异常。

### 规则中心页面

规则中心按规则集和版本组织。

- 左侧规则集列表：任务类型、发布状态、当前版本、最近命中。
- 绑定任务弹窗必须绑定当前 `rule_set_id`；用户快速切换规则集或关闭弹窗后，旧规则集异步响应不得覆盖当前绑定任务列表、loading 或错误提示。
- 右侧编辑区：过滤、转换、路由、账号策略、限速、AI 质量校验。
- 测试器：输入样本文本、目标、账号画像，返回过滤结果、转换结果、路由结果和失败原因。
- 版本区：草稿、已发布、历史版本、回滚、复制。

发布规则必须二次确认。任务只能绑定已发布版本；编辑草稿不能影响运行中任务。

### 风控中心页面

风控中心负责运行保护和处置。

- 策略 Tab：小时 / 日限额、账号冷却、目标冷却、并发限制、SlowMode / FloodWait 策略。
- 账号健康分 Tab：统一账号健康分、风险等级、限制、容量、最近失败、扣分原因和处置记录。
- 代理 Tab：代理健康、绑定授权槽位数、认证失败、同代理异常聚集。
- 命中记录 Tab：规则命中、风控阻断、跳过原因、关联任务。
- 处置队列 Tab：需要人工处理的账号、目标、代理和规则异常。

风控总览顶部指标必须支持下钻：点击可用账号、降频账号、阻塞账号时进入账号健康分 Tab 并带上风险等级 / 阻塞原因筛选；点击待处理处置项进入处置队列；点击最近 FloodWait 或代理告警进入命中记录 / 代理 Tab。账号健康分行内的“账号中心处理”必须深链到 `/accounts?account_id=...&tab=...&return_to=risk-control`，按原因默认打开登录 / 验证、账号安全、同步资产或代理信息；按钮列需要固定宽度或使用图标按钮 + tooltip，不能在窄屏或表格滚动时被截断。

风控中心可以修改策略和处置异常，但不承载具体运营方案；运营方案仍在运营中心维护。

`search_rank_deboost` 专属告警类别（在命中记录 Tab 与处置队列 Tab 中展示）：

- `rank_deboost_group_ip_drift`：分组级共享出口 IP 漂移（与该分组最近一次出口观测不一致）；触发后必须暂停该分组所有 action，不做静默 fallback。
- `rank_deboost_node_unreachable`：分组级绑定节点不可达（TCP / TLS 不通、代理认证失败、`proxy_egress_guard` 无法证明出口）；触发后允许同订阅内故障切换，切换失败时暂停该分组所有 action。
- `rank_deboost_join_button_violation`：Executor 误点了 `join_candidate` 按钮（系统自检发现）；触发后立即停止该 action、写 `search_rank_deboost_action_stats` 标记 `join_button_violation=true`、暂停该账号后续 action 直到人工确认。
- `rank_deboost_account_isolation_violation`：降权账号（`pool_purpose=rank_deboost` 分组内账号）被其他任务选用；触发后立即拒绝该任务候选并标记异常，建议运营将账号移出降权分组或移出普通分组。
- `rank_deboost_exempt_group_missing`：任务启动后发现随机豁免群尚未预选（`search_rank_deboost_exempt_groups` 无记录）；Task 已创建并保持 running，该任务运行 scope 进入 `runtime_state=waiting`，并把该 code 写入 `runtime_blocker_codes`；前端展示缺口与重选入口。不得在创建前读取该运行事实或回滚 Task。
- `rank_deboost_all_exempt_clicks`：所有竞争群结果都被白名单豁免（罕见但需可见）；触发后该 action 写 `skip_reason=target_not_in_results` 或等价 skip reason，并在风控中心可见以便运营调整关键词或目标群。

### 素材中心页面

素材中心维护可复用运营素材，不放在系统设置里。

- 素材总览：素材数量、可用数量、待缓存、缓存失败、被引用数量。
- 表情包库：支持批量上传图片伪表情、静态 sticker、animated sticker、video sticker 和 custom emoji；按标签、分组、能力状态筛选。
- 头像包：支持批量上传头像素材包，供资料初始化随机、按分组或按规则选择。
- 图片 / 文件 / 组合消息：支持上传、URL 入库、标签、分组、版本和引用关系。
- 批量上传弹窗：支持多文件选择和 `.zip` 压缩包导入；导入前选择导入类型为表情包分组、头像包或普通图片分组，可用 zip 文件名作为默认分组名并允许修改。
- 压缩包导入结果页：展示每个文件的状态、失败原因和生成的素材 / 素材包；超过 500KB、格式不在 `.png` / `.jpg` / `.jpeg`、重复文件和隐藏目录必须可追踪。
- 素材详情：资产版本、TG 引用版本、使用任务、缓存状态、失败原因、删除限制和审计记录。
- 缓存健康：展示缓存账号、缓存频道配置状态、缓存队列、FloodWait、失败和最近错误；配置跳转到系统设置填写频道链接，不展示为必须手填的内部 peer id。

素材被规则版本、任务或发送记录引用后不能物理删除，只能禁用或新增版本。

### 系统设置 - AI 与提示词 Tab

AI 与提示词 Tab 维护 AI 底座，不承载素材日常管理。

- AI 供应商：名称、模型、健康、默认状态、失败率、最近检查。
- 平台 AI 配置：默认供应商、回退策略、超时、token、温度和质量策略。
- 提示词模板：任务类型、版本、状态、测试入口。
- AI 黑话配置：词表、版本和引用关系。
- 素材运行配置：缓存账号、缓存频道链接、上传大小限制、临时文件 TTL 和缓存队列参数；页面输入面向链接和 `@username`，由后端解析执行层 peer。

### 归档中心页面

归档中心负责历史消息和成员快照检索。

- 归档任务：目标、账号、时间范围、状态、进度、失败原因。
- 消息检索：目标、发送人、手机号、关键词、时间、媒体类型。
- 成员快照：目标、成员数、账号、采集时间、变化摘要。
- 导出：必须二次确认并写审计。

归档中心读取冷数据和归档索引，不参与运营中心首页实时统计。
归档中心新建归档成功后的列表刷新失败，必须以“归档列表刷新失败”单独展示后端错误；不得复用归档目标下拉的错误状态，避免把归档索引刷新问题误报为“归档目标加载失败”。

### 运营数据页面

运营数据页用于复盘，不用于实时调度。

- 总览：发送成功率、AI 接话率、暖场响应率、转发成功率、频道互动完成率。
- 目标维度：目标效果、异常次数、任务贡献、账号覆盖。
- 任务维度：计划量、成功、失败、跳过、重试、平均延迟。
- 账号维度：容量使用、失败率、限制、可用性变化。
- AI 维度：调用量、失败率、兜底率、重复拦截、幻觉拦截、沉默次数。
- 规则 / 素材维度：命中、拦截、素材使用和失败。
- 搜索排名观察任务维度：累计导航观察点击数、加入按钮命中但拒绝点击数、跳过原因分布（`join_button_detected` / `no_navigable_button` / `target_not_in_results` / `group_ip_daily_limit_reached` / `per_account_daily_limit_reached` / `exempt_group_pending_real_search` / `rank_observation_gateway_unavailable` / `protocol_sample_insufficient`）、分组共享出口 IP 当日点击量与触顶告警次数、同账号冷却命中次数、协议样本采集完成度；排名变化仅展示为独立观察快照，不计入任务成功数。

报表默认读汇总和日统计。跨长时间范围导出必须走异步导出任务。
运营数据页的 `/api/operation-metrics/summary` 初始加载和手动刷新必须绑定当前请求序号；用户连续刷新或路由重载时，旧汇总响应不得覆盖最新 metrics、loading 或错误提示。

### 系统设置页面

系统设置只维护平台底座能力。

- Telegram 开发者应用：新增、编辑、检查、启用、禁用、凭证版本。
- AI 供应商：新增、编辑、健康检查、启用、禁用。
- 平台 AI 配置：默认供应商、模型、回退、超时、token 和质量阈值。
- Clash 配置：支持多个 Clash 订阅地址 / 接口配置，展示脱敏订阅名、主备优先级、启用状态、保存状态、同步状态、最近同步时间、节点总数、健康节点数和失败原因；保存、测试、同步和调整优先级写审计，保存成功不等于节点同步成功，不能在系统设置里分配账号或授权槽位代理。
- 素材运行配置：缓存账号、缓存频道链接、上传大小限制、临时文件 TTL 和缓存策略；素材分组、表情包库、头像包和批量上传在素材中心维护。
- 后台账号权限：用户、角色、菜单权限、按钮权限、敏感权限。
- 运行配置：只展示平台运行底座参数，不编辑运营方案和任务节奏。

系统设置不得出现群活跃具体话题、频道互动节奏、转发监听方案或目标异常处理入口。

### 审计记录页面

审计记录页面按时间线和对象检索。

- 筛选：时间、操作者、动作、对象类型、对象 ID、结果、trace_id。
- 表格：动作、对象、操作者、时间、结果、IP / 设备、摘要。
- 详情：请求摘要、变更前后、失败原因、关联任务 / 批次 / 目标。
- 导出：敏感导出必须二次确认并写新的导出审计。

`search_rank_deboost` 任务相关操作的审计字段要求：

- 必须写审计的操作：任务创建、启动、暂停、重试、编辑（含配置字段变更）、随机豁免群重选（`POST /tasks/{id}/search_rank_deboost_reroll_exempt_group`）、分组级代理绑定变更（绑定 / 解绑 / 故障切换）。
- 审计 `snapshot` 必须保留：操作原因、任务 ID、账号分组 ID、`pool_purpose=rank_deboost` 标识、随机豁免群旧值与新值（重选时）、分组级代理绑定节点 ID 旧值与新值（绑定时）、操作者、trace_id、来源页面。
- 审计摘要不得输出 Clash 订阅 URL、节点密码、token 或关键词明文；订阅 URL、节点 URI、Bot token 必须脱敏为 `***` + 末 4 位，关键词必须以 `keyword_hash` 形式记录。
- 随机豁免群重选审计必须包含旧豁免群 ID、新豁免群 ID、操作人、时间、trace_id，不影响已生成的 action 历史。
- 分组级代理绑定变更审计必须包含旧节点 ID、新节点 ID、变更原因（手动 / 故障切换 / 订阅失效）、`proxy_node_failover_events` 关联 ID。

### 操作手册页面

操作手册是前端内置帮助页，面向运营人员而不是研发人员。

- 日常操作顺序：配置基础能力、接入账号、进入运营中心查看目标工作台、展开关联任务失败、处理账号 / 目标 / 规则 / 风控异常、使用运营方案或任务中心发起执行、回到运营中心复盘。
- 上线前检查：开发者应用、AI、账号登录、账号安全、运营目标、目标画像学习来源、规则版本、风控异常。
- 任务类型选择：AI 活跃群、转发监听群、频道浏览、频道点赞、频道评论 / 回复、搜索目标群点击任务。
- 最近更新功能：运营中心日常入口、运营方案模板、目标画像独立、任务创建动态向导、账号资产与可用性、数据汇总延迟、导航升级、账号安全、资料初始化、任务内目标输入、准入前置、AI 接话 / 暖场。

- 异常速查：运营中心目标异常、账号不可用、目标不可用、规则拦截、AI 质量跳过、监听无事件、评论区不可用、汇总数据延迟。

`frontend/src/app/views/AdminManualView.tsx` 的手册内容必须和本章节同步，保证菜单名、按钮名、异常口径和最近更新功能一致。

操作手册必须和真实菜单、按钮、弹窗和异常提示同步，不能写已经不存在的旧 Campaign、卡密、订阅套餐或多租户 SaaS 口径。

### 前端验收口径

- 每个主导航页面必须有首屏摘要、筛选、主内容、主按钮、详情或下钻入口。
- 每个可见按钮必须有后端接口、前端本地行为或明确的只读说明。
- 每个创建 / 编辑 / 危险动作必须有 loading、成功、失败、权限不足和审计处理。
- 所有登录、验证码、2FA、搜索、筛选和配置弹窗的主输入表单必须支持 Enter 回车提交；回车提交必须复用点击主按钮的校验、权限、loading、幂等和错误展示，不能绕过二次确认或发起重复请求。
- 列表页默认读汇总或分页接口；详情页按对象 ID 下钻；前端不得为了展示摘要扫描明细大表。
- 运营目标页必须使用服务端分页和远程搜索；目标列表、total、页码、loading 和 error 绑定当前 `page/page_size/q/target_type/capability` 与请求序号，旧查询不得覆盖新查询。
- 任务中心列表必须使用 `/api/tasks/page` 的服务端分页、搜索、状态 / 类型筛选、统计和分组；每 60 秒只轮询当前 `page/page_size/type/status/q/group_key`，不得重新请求旧全量 `/api/tasks` 后本地分页。
- 任务创建 / 编辑弹窗必须先展示可操作壳层，再懒加载目标候选；目标输入使用远程 `q` 搜索，编辑态使用 `ids` 回显已选项。目标请求失败必须在弹窗内可见，不能阻止弹窗打开或静默改用全量接口。
- 运营中心只读取当前目标页及其 `target_ids` 运行摘要；规则中心和归档中心只在目标选择器实际需要时分页懒加载；消息发送按当前账号远程分页查询目标；AppShell 关联群深链按 `linked_group_id` 定点读取。
- 上述第一方消费者全部必须显式携带分页参数；“懒加载”不能被实现为延迟触发同一个无界请求。
- 系统设置的 Clash 配置页必须能区分保存失败、保存成功但节点同步失败、同步成功但健康节点为 0、健康节点可用四种状态，并展示重试 / 同步入口。
- 账号面具一级菜单必须至少包含“面具管理 / 账号代理 / 授权指纹 / 异常与审计”四个 Tab；账号代理和授权指纹 Tab 必须按 `account_id + developer_app_id/api_id + authorization_id/session_role` 展示，不得只按账号维度聚合导致主备授权或不同 TG 开发者应用混淆。
- 授权指纹页必须把配置指纹、远端观测指纹和一致性状态同时展示；保存后未重登时应显示 `pending_effect`，Telegram 快照缺字段时应显示 `unobservable` 和缺失字段，不能显示“远端已更新”。
- 运营中心、任务中心、账号中心、运营目标和风控中心之间的深链必须携带对象 ID、默认 Tab、`return_to`、来源 issue、筛选条件和必要的滚动 / 展开状态。
- 运营中心异常详情读取和异常处理动作必须绑定当前 `issue_id`；旧异常异步响应不得覆盖当前异常抽屉的数据、loading、错误提示或处理结果。异常处理提交还必须绑定发起时的 `issue_id + action + reason` 签名、提交请求序号和处理原因弹窗会话；提交返回前切换处理动作、关闭 / 重开原因弹窗或修改原因时，旧提交响应不得关闭当前原因弹窗、清空当前原因、覆盖提示或触发旧原因成功刷新。
- 运营目标详情读取、自动同步、评论同步、账号策略保存、准入重试和归档创建后的详情刷新必须绑定当前 `target_id`；旧目标异步响应不得覆盖当前详情、loading 或错误提示。
- 素材详情读取和缓存刷新后的详情回填必须绑定当前 `material_id`；旧素材异步响应不得覆盖当前详情、引用记录、版本记录、loading 或错误提示。素材详情、引用记录和版本记录必须分别承载接口结果，任一接口失败只能清空对应数据并展示具体错误，不能把另外已成功返回的数据一并丢弃。
- 素材缓存刷新成功后的素材列表刷新必须等待并捕获失败；列表刷新失败时展示“刷新素材列表失败”及后端错误，不能形成不可见 Promise rejection，也不能把列表刷新失败误报为“刷新素材缓存失败”。
- 空状态必须给出下一步动作；错误状态必须展示 trace_id 或可复制错误信息。

---

## 4. 数据模型 PRD

### 4.1 核心表分组

| 分组 | 表 |
| --- | --- |
| 平台实例与后台用户 | `tenants`、`app_users`、`user_token_ledgers` |
| 账号与登录 | `telegram_developer_apps`、`tg_accounts`、`tg_account_authorizations`、`tg_login_flows`、`tg_verification_codes` |
| 账号同步资产 | `tg_groups`、`tg_group_accounts`、`tg_contacts`、`tg_account_sync_records`、`tg_account_profile_sync_records`、`tg_account_online_state` |
| 账号安全 | `tg_account_security_snapshots`、`tg_account_authorization_snapshots`、`tg_account_security_batches`、`tg_account_security_batch_items`、`tg_account_profile_batch_rules` |
| 运营目标 | `operation_targets`、`channel_messages`、`channel_message_comments` |
| 目标画像 | `tenant_learning_profiles`、`tenant_learning_sources`、`tenant_learning_samples`、`tenant_learning_quality_rules`、`tenant_learning_profile_versions`、`tenant_learning_runs` |
| 运营方案 | `operation_plan_templates`、`operation_plan_targets`、`operation_plan_task_links`、`operation_plan_generation_runs` |
| 新版任务中心 | `tasks`、`actions`、`execution_attempts` |
| AI 活群质量与会话事实 | `ai_group_message_memory`、`ai_account_voice_profiles`、`ai_account_group_stance_memory`、`conversation_speaker_states`、`conversation_speaker_turns` |
| AI 活群群管准入（专项设计待开发） | `group_bot_admission_policies`、`task_group_bot_admissions`、`account_group_admission_facts` |
| 目标准入 | `target_membership_items`、`target_membership_challenge_attempts`、`task_ready_accounts` |
| 搜索点击环境与结果 | `search_click_obligations`、`search_click_assignments`、`search_protocol_sessions`、`bot_protocol_samples`、`proxy_airport_subscriptions`、`proxy_airport_nodes`、`proxy_exit_ip_observations`、`account_proxy_bindings`、`account_environment_bindings`、`fingerprint_combo_history` |
| 监听与运行 | `listener_source_state`、`worker_heartbeats`、`runtime_metric_snapshots`、`daily_runtime_stats`、`runtime_cleanup_audits` |
| 运营汇总读模型 | `target_runtime_summary`、`task_runtime_summary`、`account_runtime_summary`、`operation_issue`、`operation_issue_sources`、`operation_issue_accounts` |
| 规则 | `rule_sets`、`rule_set_versions` |
| 风控与代理 | `account_proxies`、`account_proxy_bindings`、`proxy_alerts`、`proxy_health_checks` |
| AI 与提示词 | `ai_providers`、`ai_provider_key_versions`、`tenant_ai_settings`、`prompt_templates`、`ai_usage_ledgers` |
| 素材中心 | `materials`、`material_asset_versions`、`material_tg_ref_versions` |
| 转发和媒体缓存 | `message_fingerprints`、`source_media_assets` |
| 手动发送 | `message_tasks`、`message_task_attempts`、`manual_operation_records` |
| 历史兼容表 | `operation_tasks`、`operation_task_attempts`、`campaigns`、`ai_drafts` |
| 归档与审计 | `group_archives`、`archived_messages`、`archived_members`、`audit_logs` |

历史兼容接口不能绕过当前权限矩阵。`operation_tasks`、`operation_task_attempts` 和旧审核队列按任务中心事实源处理：读取要求 `tasks.view`，创建、派发、重试、取消、审核通过和审核拒绝要求 `tasks.manage`。`campaigns` 和 `ai_drafts` 仍可能生成或审批手动发送内容，读取要求 `message_sending.view`，创建、生成、审批、修改、拒绝和取消要求 `message_sending.manage`。

### 4.2 关键表说明

| 表 | 关键字段 | 说明 |
| --- | --- | --- |
| `tg_accounts` | `tenant_id`、`pool_id`、`display_name`、`username`、`phone_number`、`phone_masked`、`account_identity`、`status`、`session_ciphertext`、`developer_app_id`、`proxy_id`、`health_score`、`code_source_host`、`code_source_uuid_ciphertext/fingerprint/hint`、`code_source_binding_status/version`、`deleted_at` | TG 账号主表；`account_identity=normal/code_receiver/rank_deboost/account_purpose_mismatch` 是与当前 `AccountPool.pool_purpose` 同步的执行期用途投影，生产写路径必须原子维护 `pool_id + account_identity`；不一致时按最严格用途阻断外部动作。`session_ciphertext`、`developer_app_id` 是迁移期主授权兼容字段，账号级 `proxy_id` 不作为普通任务或降权任务的默认连接参数。批量登号的 UUID 以加密 binding 持久关联账号，接码备注由 host+hint 派生并独立于 `display_name`；完整值仅经受控 reveal 读取。 |
| `tg_account_authorizations` | `tenant_id`、`account_id`、`logical_slot`、`slot_generation`、`is_slot_current`、`developer_app_id`、`developer_app_api_id_snapshot`、`session_ciphertext/wake_bundle_id`、`credential_storage_scope`、`auth_key_fingerprint_hmac`、`telegram_authorization_hash_ciphertext/hash_fingerprint_hmac`、`remote_authorization_state`、`status`、`health_status`、`fact_version`、`is_current`、`failure_reason`、`disabled_at`、`created_by` | 账号授权资产表；三个当前槽位必须分别使用三套 Developer App、三个 AuthKey 和三个非零远端 hash。SV 槽位保存中心加密 Session，MY standby_2 保存 MY wake bundle/receipt 引用。远端设备归属以 hash 精确匹配为准，`api_id` 只作一致性校验；三槽位全部不可用时只能进入人工重新登录 / 扫码 / 手动验证码 |
| `tg_verification_codes` | `tenant_id`、`account_id`、`authorization_id`、`source_peer`、`source_message_id`、`code_ciphertext`、`code_masked`、`received_at`、`expires_at`、`status`、`failure_type`、`failure_detail` | Telegram 官方验证码事实；只记录真实读取到的官方服务 code 或明确失败原因，展示时受 `accounts.codes.read` 权限和审计控制 |
| `account_pools` | `tenant_id`、`name`、`is_default`、`pool_purpose`、`is_system`、`system_key`、`is_enabled`、`disabled_at`、`disabled_by`、`disable_reason` | 账号分组与用途真相源；`pool_purpose=code_receiver` 且 `system_key=code_receiver` 表示系统接码专用分组。`pool_purpose=rank_deboost` 表示降权任务专用分组，同租户可存在一个系统默认组和多个自定义组；专用组不可删除或改用途，只能显式禁用。禁用不改变组内账号用途，必须显式迁移账号后才能进入普通任务。 |
| `tg_account_authorization_snapshots` | `tenant_id`、`account_id`、`observation_id`、`snapshot_digest`、`snapshot_at`、`remote_authorization_hash_ciphertext/hash_fingerprint_hmac`、`remote_api_id`、`device_model/platform/app_name/app_version`、`date_created/last_active_at`、`ip_masked/country`、`classification=platform_current|platform_retained|external|unresolved`、`matched_authorization_id/logical_slot/slot_generation/fact_version`、`cleanup_status`、`failure_reason` | Telegram 远端授权设备追加快照；按未撤销我方授权的非零 hash 精确分类，不允许前端按 `api_id` 重算。一键清理引用快照/manifest 版本和 target hash digests，不返回 hash 明文 |
| `proxy_airport_subscriptions` | `tenant_id`、`provider`、`name`、`clash_subscription_url_ciphertext`、`subscription_format`、`priority`、`enabled`、`status`、`failover_policy`、`auto_failback_enabled`、`failback_cooldown_minutes`、`last_sync_at`、`last_error`、`node_count`、`healthy_node_count`、`updated_by` | 系统设置里的 Clash 订阅源池；每租户允许多条 enabled 订阅，按 `priority` 主备容灾，优先级不能冲突；完整订阅 URL、token 和节点 URI 必须加密保存并禁止普通日志输出 |
| `proxy_airport_nodes` | `subscription_id`、`node_key`、`name`、`protocol`、`host`、`port`、`country`、`region`、`asn`、`isp`、`status`、`observed_exit_ip`、`last_health_check_at`、`capacity_limit` | 由 Clash 订阅源同步出的标准节点池；节点入口 host 不等于真实出口 IP，必须通过健康检查观测出口事实后才能绑定授权槽位 |
| `account_environment_bindings` | `tenant_id`、`account_id`、`authorization_id`、`session_role`、`developer_app_id`、`developer_app_api_id_snapshot`、`proxy_binding_id`、`device_model`、`system_version`、`app_version`、`platform`、`lang_code`、`system_lang_code`、`lang_pack`、`region_code`、`client_identity_key`、`fingerprint_locked`、`status` | 账号授权环境绑定；客户端元数据和代理绑定粒度都是账号 + TG 开发者应用 + 授权槽位。不同应用和不同槽位可以绑定不同客户端元数据和不同代理节点；search_join 等真实执行任务按本表取指纹和代理，缺失或冲突时 fail closed |
| `fingerprint_combo_history` | `tenant_id`、`account_id`、`authorization_id`、`session_role`、`developer_app_id`、`combo_key`、`device_model`、`system_version`、`app_version`、`platform`、`usage_count`、`status` | 授权指纹组合使用历史和重复度控制；用于发现同应用多账号复用同一设备组合、主备槽位复用指纹等异常 |
| `tg_contacts` | `account_id`、`contact_peer_id`、`display_name`、`username`、`phone_number`、`phone_masked`、`last_sync_at` | 账号联系人；联系人页、消息发送、导出优先展示完整手机号 |
| `operation_targets` | `target_type`、`tg_peer_id`、`title`、`username`、`can_send`、`can_listen`、`auth_status`、`source_type`、`last_synced_account_id` | 用户侧运营目标；来源可以是账号同步、任务创建 upsert 或管理修订 |
| `tenant_learning_profiles` | `tenant_id`、`profile_version`、`status`、`learning_enabled`、`style_summary`、`topic_weights`、`phrase_patterns`、`reply_patterns`、`comment_patterns`、`forbidden_learning`、`source_sample_count`、`last_rebuilt_at`、`last_used_at` | 租户级唯一 AI 画像；所有 AI 活跃群和频道评论 / 回复共同使用 |
| `tenant_learning_sources` | `tenant_id`、`target_id`、`source_kind`、`is_enabled`、`auto_sync_enabled`、`source_status`、`listener_account_ids`、`last_sync_at`、`last_history_pull_at`、`watermark`、`last_failure_detail`、`selected_by`、`selected_at` | 画像学习来源；引用运营目标但不把画像治理放入运营目标详情 |
| `tenant_learning_samples` | `tenant_id`、`source_id`、`source_message_id`、`source_scene`、`sender_*`、`raw_text_hash`、`text`、`learning_status`、`quality_score`、`quality_rule_version`、`reject_reason`、`downweight_reason`、`decision_by`、`decision_at`、`sent_at` | 候选学习样本；先过滤后治理，支持采纳、降权、剔除 |
| `tenant_learning_quality_rules` | `tenant_id`、`rule_version`、`identity_filters`、`text_filters`、`template_filters`、`scoring_thresholds`、`scene_weights`、`forbidden_patterns`、`updated_by`、`updated_at` | 画像样本质量过滤规则；规则变更写版本和审计，变更后需重新计算候选样本并重建画像 |
| `tenant_learning_profile_versions` | `tenant_id`、`profile_version`、`profile_snapshot`、`source_snapshot`、`quality_rule_version`、`sample_count`、`created_by`、`created_at` | 画像版本快照；支持查看、恢复和审计，不按目标拆分 |
| `tenant_learning_runs` | `tenant_id`、`run_type`、`source_id`、`status`、`from_watermark`、`to_watermark`、`pulled_count`、`sample_count`、`accepted_count`、`rejected_count`、`quality_rule_version`、`profile_version`、`failure_detail`、`trace_id` | 自动同步、向上拉取历史、候选重算和画像重建的运行记录 |
| `operation_plan_templates` | `name`、`plan_type`、`status`、`target_scope`、`task_types`、`account_config`、`operation_profile`、`rule_binding`、`ai_strategy`、`risk_policy`、`created_by`、`updated_at` | 运营中心方案 / 策略模板 |
| `operation_plan_targets` | `plan_id`、`target_id`、`target_role`、`status`、`last_preview_result` | 方案覆盖目标和目标预览结果 |
| `operation_plan_task_links` | `plan_id`、`task_id`、`target_id`、`task_type`、`sync_status`、`last_applied_at` | 方案和任务的生成 / 调整关系 |
| `operation_plan_generation_runs` | `plan_id`、`mode`、`status`、`preview_snapshot`、`created_task_ids`、`blocked_reasons`、`trace_id` | 方案生成预览、生成任务和应用关联任务的运行记录 |
| `task_group_daily_targets` | `task_day_ledger_id/target_operation_target_id/legacy_target_id`、`base_target_revision/base_planned_target`、`effective_planned_target_revision/effective_planned_target/next_quantity_ordinal`、`confirmed_message_count/on_time/late/time_unproven/post_settlement_confirmed`、`raw_observed_remote_count/unbound_observed_remote_count/unresolved_terminal_shortfall_count`、`settlement_status/settled_at/settled_target/on_time/coverage/unknown/shortfall counts/snapshot_hash`、`pacing_snapshot_hash/target_effective_at/version` | drop旧full date unique；legacy/current各partial unique且CHECK禁止半空；takeover新建current row并link旧row。confirmed仅为bound quantity binding投影，动态coverage只推进effective revision/read-model，不改base/target-set hash；settled字段由SettlementOperation只写一次 |
| `channel_view_contract_hash_v1`（共享serializer/registry） | NFC UTF-8、object/set C序、UTC六位微秒、整数/null规范、SHA-256；target/due/materialized/matching/source/expiry/settlement/inventory/takeover/fact/tombstone各typed payload | 所有CAS/readback/replay/Release Gate owner同时持久`hash_contract_version`；API/worker/CLI共用helper+golden vectors，未知版本、同count异identity、遗漏守恒字段一律fail-closed |
| `channel_view_daily_message_targets` | `task/ledger/target_operation_target_id/target_peer_id/channel_message_id/source+target revision/pacing anchor/active_until/daily+cumulative remaining+effective target/source state/baseline due+active elapsed/accrual stop elapsed+due/next ordinal/version` | 浏览每ledger+peer+message的TargetSet/DueSet owner；initial冻结、dynamic只append；DueSet由route accrual segments按as-of计算，pause不追债、expire按active_until冻结；current唯一`(ledger,target_peer_id,channel_message_id)`，message ID不得跨peer互斥 |
| `view_fulfillment_obligations` | 既有字段加`daily_message_target_id/target_peer_id/channel_message_id/source_revision/target_revision/due_ordinal/materialization_version/lifecycle_epoch/deadline_at/state/account_id nullable/current_action_id nullable` | current due-unit唯一`peer-message target+due ordinal`；账号为materialization绑定，safe pre-Gateway失败可在同unit递增version换账号，Gateway/unknown/confirmed不可普通重开；same-identity owner conflict保持typed占位，只有权威resolution可在deadline前排除旧identity后重物化，deadline后为known shortfall |
| `channel_view_action_bindings/remote_fact_bindings/remote_fact_observations` | Action binding:`action/obligation/target_peer+message/materialization/account/route+immutable epoch/state/version`；fact binding:`fact/navigation obligation nullable/logical task-ledger-peer-message-target-due key/binding state/timeliness/tombstone/version`；observation:`fact/request/evidence/requested unit/classification/hash` | active route下Dispatcher唯一结构化owner；一个fact只bound一个Task unit。canonical logical daily identity为`(tenant,target_peer_id,channel_message_id,account_id,obligation_local_date)`，物理fact unique为peer+message+account+obligation_local_date；同identity重放只append observation，不造第二fact。不同identity对同due unit的second fact保留为unbound conflict；fact/binding不随Task删除 |
| `channel_view_daily_identity_owners` | `tenant/target_peer/message/account/obligation_local_date/state=available|pre_gateway|call_issued|unknown|confirmed/logical_task_id/nullable action+obligation/request_identity/version/timestamps` | peer+message+account+obligation_local_date物理唯一，非空 action、obligation 分别唯一；Planner 原子 claim，Dispatcher Gateway 前推进 call-issued；未进入transport的pre-Gateway可释放，call-issued仅在该Action全部已启动Gateway Attempt各自具有权威`remote_mutation_started=false`证据时释放，任一Attempt缺证据或为true/unknown以及Owner为unknown/confirmed均当日占位；迁移回填 legacy Action/Gateway/fact，禁止 mixed-fleet check-then-insert 重复浏览 |
| `channel_view_listener_sources/source_observation_events/deltas/subscriptions/fanout_items` | logical source:`tenant+target/current collector epoch/observation+heartbeat+cursor verified+success poll/high-water/version`；event/delta冻结collector evidence与message delta set/count/hash；subscription/fanout有observed version、cursor/count/hash、lease/state/version | collector切换不换subscription；成功empty poll刷新cursor verified。listener不做Task fanout，recovery按持久delta有界推进且event-before无丢失；shared delta不裁决Task expiry |
| `channel_view_source_projections/read_model_revisions/target_expiry_activation_operations/schedules` | projection:`task/ledger/logical source observation+collector/policy/set hashes/source state/version`；read-model:`ledger/current version/target/due/materialized/source+settlement versions`；activation owner:`route/ready/cursor/count+set hash/next-retry/lease/version`；schedule:`target/active_until/activation-ready/next-retry/state/lease/result/version` | dynamic append与settlement item/input/read-model同tx；每target expiry按自身active_until和clock segments冻结。takeover preparing不可领取，class activation只唤醒单一owner，recovery有界激活全部schedule；finite empty source为missed_no_source，不伪装met |
| `channel_view_accrual_clocks/segments` | clock:`route/state/imported baseline/active summary/current seq/segment-set hash/version`；segment:`clock/seq/start/end/active_us/stop reason/version` | append-only running segments是DueSet业务时间真相；pause/stop不累计，late expiry/settlement按historical as-of积分，deadline后不扩DueSet |
| `channel_view_lifecycle_adoptions/items` | owner:`task/enrollment/route/from-to epoch/command/state/discovery/cursor/counts/lease/version`；item:`trigger kind+identity/obligation+binding/state/evidence/lease/version` | pause/stop有界收口所有old-epoch非终态unit；blocked开enrollment blocker，deferred hold只reconcile，resume等待ready且重复命令不推进epoch |
| `channel_view_settlement_operations/target_items` | operation:`ledger/activation-ready/deadline/next-retry/target-input/source-shortfall/aggregate counts+status/set+result hash/lease/version`；item含`unit_cursor/pre_gateway_discovered/safe-released/issued-or-unknown-preserved counts+set hashes/drain-input+complete`、DueSet及on-time/late/unproven/unknown/known-shortfall counts+set hashes、projection barrier、immutable status/result | 任意Task状态deadline drain；takeover preparing固定ready=false/next-retry=NULL，class activation同tx写ready与`max(deadline,db-now)`。deadline先按global owner C序有界释放同request可证未transport的pre_gateway owner，并原子终结Binding/Action/due unit；call-issued/unknown/fact记录永久保留，但占用只覆盖其`obligation_local_date`。drain count/hash闭合且无pre_gateway owner后才写一次immutable结果；未物化ordinal仍由隐式DueSet结算，required fact projection anti-join完整，late history不翻转 |
| `channel_view_ledger_bootstrap_operations` | expected task/enrollment/domain/prior settlement+route与完整result ledger/route/clock/logical source+subscription/projection/targets+expiry/settlement/read-model hashes | first start由TaskStartOperation外层持有，持续running rollover由recovery领取；一个事务建立完整bundle，任一crash重放同result，禁止active route缺source/settlement owner |
| `channel_view_task_domain_revisions` | `task/next-ledger revision+snapshot/future-target-policy revision+hash/source-policy revision+hash/account-scope revision+hash/display/version` | generic/type PATCH共用field-family决策器，mixed request原子；initial selector current immutable，daily/total/active-days只影响新append target，timezone/curve只next-ledger；legacy target alias冲突422、burst拒绝，current ledger/source/target不可被通用配置旁路改写 |
| `channel_view_fleet_policies/legacy_inventory_items/enrollments/planner_contract_routes/contract_blockers/occurrences/resolutions` | tenant fleet/cutoff/membership+runtime hashes；item含logical task+nullable navigation/allowed epochs/tombstone；enrollment+per-ledger route双scope blocker counts；blocker occurrence冻结source revision/snapshot并永久unique；fixed registry | protected builder seal；open legacy item合法写同tx推进allowed epochs。旧occurrence重放不重开，真正新revision可再次fence。takeover按never-started/same-period/live-settling/rollover/terminal-settling/terminal-retired互斥分类保持原状态；zero-history不进rollover、terminal不进live class，某Task preparing后forward-only |
| `channel_view_takeover_operations/manifests/items/checkpoints/source_fences/events` | `item/task/class/state/final manifest/static+source hashes/chunk cursor/counts/lease/version`及immutable item/event identities | 粗preview后先fence/quiescence，再生成final manifest；分块crash replay与A类delta readback后才class-specific activation，不漏并发fact |
| `channel_view_contract_tombstones` | `logical task/enrollment/routes/ledger/source/target/due/fact/binding/observation/daily-owner/daily-transition identity-set counts+hashes/delete operation/committed at/version` | Task删除前归档并把fact/binding/observation/owner导航FK SET NULL；global peer/message/account/date fact+owner unique永久保留，late reconcile/observation只读收口。已标记旧 `0172` 的环境必须通过新增 `0173_channel_view_fact_nav` 前向迁移把 `view_remote_facts.obligation_id` 从 non-null/CASCADE 改为 nullable/SET NULL；回滚发现 NULL 或孤儿导航时拒绝降级，不得删除 canonical fact |
| `ai_group_message_obligations` | `task_day_ledger_id`、`target_operation_target_id`、`quantity_ordinal`、`effective_due_rank/due_rank_state/rank_retired_reason/rank_retired_at`、`coverage_ledger_id`、`due_at`、`deadline_at`、`state`、`blocker_*`、`current_content_intent_id/revision`、`generation_epoch`、`active_action_id`、`confirmed_remote_fact_id`、`route_epoch`、`task_lifecycle_epoch`、`version` | quantity ordinal是永不复用identity；active rank才进入DueSet，retired历史可让新identity复用rank位置，protected overage不抵扣低rank。stop-safe取消原子retire rank，重启用更高ordinal补位；Action只是一轮物化，只有typed bound fact confirmed |
| `ai_group_content_allocation_plans/assignments/intents/variations` | `task_day_ledger_id`、`content_cycle_seq`、`scope_total_units`、`allocation_hash`、`plan_unit_ordinal`、`relation/material/act_type`、`intent_snapshot_hash`、`variation_sequence/key`、`generation_epoch_basis_hash`、`state/version` | current aggregate内容合同跨最多20条技术批次守住比例/minimum；unit intent不可变，same-basis replacement不重置主/备用各3轮 |
| `ai_group_check_in_handoffs` | `obligation/generation_epoch/trigger_reason/generation_job+version/intent+revision/six_round_evidence_hash/state/claim lease/version` | unique(obligation,generation_epoch,trigger_reason)。Generation六轮耗尽只写handoff并转check_in_ready，Planner只消费handoff创建ready签到Action；重放/deadline不双写 |
| `ai_group_wake_clocks/subscriptions/dependency_fanout_events/items` | `wake_key`、`current_version/drained_version`、`row_version/subscription_fence`、`dirty/dirty_seq`、`obligation_id`、`observed_version`、`wake_at`、`source_kind/scope/version`、`event cursor/item target/state/lease/version` | waiting的持久事件/时间唤醒；tenant+group context只用共享clock。profile/online/material等共享源事务只写单scope clock+唯一outbox event，recovery有界fan-out到task-day aggregate，禁止源事务扫描全部Task；reply-source由bound fact推进；订阅fence关闭event-before窗口 |
| `ai_group_message_contract_fleet_policies/legacy_inventory_items/enrollments/routes` | fleet inventory/allowlist字段；enrollment加`open_contract_blocker_count/contract_blocker_revision`；route加`open_blocker_count/blocker_revision/activated_at`及ledger/target-set/route/lifecycle/manifest writer fence | policy/item是在线legacy allowlist，不复用Dispatcher runtime contract；Alembic只DDL，protected bootstrap seal。enrollment blocker跨日fence，route blocker只限当前route；首日/跨日route原子建，takeover readback与Task/enrollment/route同事务激活；item/enrollment/tombstone不随Task物理删除 |
| `ai_group_message_contract_blockers/occurrences/resolutions` | blocker:`enrollment/scope/route/kind/stable identity/state/first occurrence/source snapshot/current resolution/version`；occurrence:`owner/kind/occurrence identity/source revision/snapshot/linked blocker`；resolution含`revision/decision/evidence/new contract/deployed SHA/operator/approval/expected versions+hash/state/result` | open-only partial unique收敛current blocker，occurrence永久unique使旧resolved事件重放不重开；新source revision可创建新open blocker并重新fence。registry固定scope/channel，owner count/revision与open rows同事务守恒，通用retry不能清除 |
| `ai_group_message_lifecycle_adoptions/items/safe_evidence` | owner:`task/enrollment/ledger/route/from-to epoch/command/state/adoption_seq/cursor/counts/manifest/lease/version`；item:`obligation/trigger/item_seq/state/latest evidence/lease/version`；evidence:`deferred item/request/evidence identity+hash/state/result` | pause/stop/imported baseline唯一有界sweep；blocked item打开enrollment blocker并阻止ready。safe evidence append后只重排原deferred item并一次减count，不建第二计数item；resume/start只在ready|complete且两层blocker=0 |
| `ai_group_ledger_bootstrap_operations/request_revisions` | operation含`task/next period/enrollment/caller/source_mode/source ledger-route/state/next_retry/lease/result/version`；request revision冻结expected lifecycle/config/target-set、source route class/imported-baseline/final-manifest hash并记录superseded lineage | first start、持续running自动跨日、resume/start-after-stop跨period唯一owner；自动跨日区分`normal_running`（同tx关闭active-running旧route）与`takeover_closed`（只读已closed接管route证据、绝不重开）。无result的stale request可CAS supersede，已有ledger/route结果只回读；rollover先settlement complete且task/enrollment blocker=0 |
| `ai_group_task_day_settlement_operations/target_items` | operation:`ledger/enrollment/route/target-set/deadline/activation-ready/state/cursors/projection barrier/aggregate five-way counts+set hashes/settlement hash/next-retry/lease/version`；item含SettledRankSet及quantity/coverage的on-time/late/unproven/unknown/known-shortfall、protected-overage count+set hashes、status/settled-at/result | 任意Task状态deadline drain；补齐未物化due为terminal shortfall，等待required fact projections收敛后一次写immutable snapshot；持续增长的late/unproven历史不改settled集合。poison修复成功精确requeue blocked item/operation |
| `ai_group_projection_poison_resolutions` | `projection_state/resolution_revision/old_error_hash/new_projector_contract/deployed_sha/approval/expected_version/decision/state/result_hash` | system.manage受保护append-only恢复；批准只requeue精确poison，成功重投影/守恒后才resolve blocker和settlement，不用generic retry |
| `ai_group_message_task_revisions/history` | `task_id/next_ledger_revision/content_plan_revision/generation_policy_revision/account_scope_revision/restart_revision/current_snapshot_hashes/version`、`domain/revision/snapshot_hash/source_request` | PATCH各字段族的持久revision owner；混合PATCH只推进实际变化域。ledger/plan/intent/scope冻结各自revision，展示/name不重置generation 3+3；start-after-stop由既有TaskStartOperation推进restart revision |
| `ai_group_takeover_operations/manifests/items/chunk_checkpoints` | operation:`inventory item/task/class/request revision/state/final manifest/source fence/expected versions/cursor/count+hash/lease/version`；immutable manifest/item双unique；checkpoint含item range/input-output hash/state/lease/version | 粗preview后先fence/quiescence，再生成数据库final manifest；只按同operation/version分块续跑，全部checkpoint/readback闭合后才能class-specific activation |
| `ai_group_takeover_source_fences/events` | `task_day_ledger_id/route_epoch/a_event_version/reconcile_delta_version/hash/last_event_seq/version` | preparing期间A类高频事实同tx append event并CAS fence；activation重读有限A类static revision vector，任何A变化阻断，B类liveness变化只触发实时重评估 |
| `ai_group_message_contract_tombstones` | `logical_task_id/enrollment_epoch/policy_item_identity/last_route_ledger_target_manifest_source_hash/raw_bound_unbound_hold_projector_counts+identity_hash/delete_operation_id/snapshot_hash/committed_at` | append-only fleet closure与守恒审计；delete把hold/fact/binding/projection/reconcile归档到本表+RemoteMutationTombstone，unknown未结也可在archive readback后删除Task，late fact只更新deleted-task tombstone；item/enrollment retired且membership不变 |
| `ai_group_message_read_model_revisions` | `task_day_ledger_id`、`target_set_hash`、`current_version/version`、`updated_at` | whole target-set summary/obligation/attempt分页快照 owner；所有页面可见 transition 同事务 CAS bump，cursor签完整 normalized filters、enrollment/route/read-model version |
| `ai_group_obligation_legacy_links/check_in_scope_claims` | `legacy_kind/id`、`obligation_id/classification`、`claim_scope/state/owner/version` | 多个 legacy identity 可 additive alias 到一个 current obligation；旧行不改写；scoped check-in 新旧 owner统一占位 |
| `ai_group_message_memory` | `tenant_id`、`group_id`、`task_id`、`task_day_ledger_id`、`action_id/reservation_owner_action_id`、`obligation_id`、`content_variation_id`、`account_id`、面具snapshot字段、`content_source/check_in_trigger_reason/coverage_ledger_id`、内容/指纹字段、`reservation_key/reservation_version`、`status`、Gateway/发送/去重/质量字段 | 正常正文以`tenant_id+account_id`滚动10天去重。check-in使用scope business reservation key并继续受永久unique约束；current scope只建一条memory。safe pre-transport失败把同一行released，wake后以claim+memory CAS推进reservation_version并换新Action owner，不插第二行；Gateway/unknown/success不可再reserve。legacy memory只alias不改写 |
| `ai_account_voice_profiles` | `tenant_id`、`account_id`、`version`、`mask_name`、`audience_archetype`、`identity_frame`、`preference_tags`、`age_band`、`persona_experiences`、`consumption_experiences`、`sentence_length`、`interaction_habits`、`tone_strength`、`lexical_preferences`、`emoji_policy`、`forbidden_expressions`、`short_prompt_summary`、`source`、`status`、`similarity_score`、`quality_status`、`last_rebuilt_at`、`updated_by`、`updated_at` | AI 活跃群账号面具事实源；表名暂保留 `ai_account_voice_profiles` 作为兼容技术名，API 和前端展示统一称为账号面具。同一账号在所有 AI 活跃群中使用同一张全局 active 面具，按账号保存长期对外身份感、偏好标签、年龄段、经历 / 消费经历设定和口气差异；支持“账号面具”一级菜单中搜索、查看、编辑、批量生成、批量重建、停用、版本回滚和审计；Redis 只能缓存短摘要，不得作为唯一存储；面具不得冒充真实用户、管理员或具体自然人 |
| `ai_account_group_stance_memory` | `tenant_id`、`group_id`、`account_id`、`topic_direction`、`teacher_target`、`stance`、`last_act_type`、`last_semantic_cluster`、`last_message_id`、`last_spoken_at`、`window_start_at`、`window_end_at`、`summary`、`updated_at` | 账号在目标群内的短期立场事实源；用于保持 24 小时到 7 天内的态度、对象和话题连续性，避免前后表达断裂；Redis 只缓存最近几轮热状态，丢失后必须可由数据库恢复 |
| `conversation_speaker_states`（专项设计待开发） | `tenant_id`、`surface`、`conversation_key`、`last_platform_account_id`、`last_platform_action_id`、`last_human_cursor`、`last_remote_fact_id`、`version` | 每个真实会话的可重建 speaker 投影；远端事件事实先追加，随后以 conversation key/version 单行 CAS 更新，不建立 speaker/admission 跨表锁、账号锁或预约。CAS 冲突回读最新事实并重新做发送前轻量检查。 |
| `conversation_speaker_turns`（专项设计待开发） | `tenant_id`、`surface`、`conversation_key`、`remote_message_id`、`remote_cursor`、`sender_kind`、`account_id`、`outcome`、`content_source`、`action_id`、`observed_at` | 群聊/频道讨论区真实消息顺序；真人消息才可打断同账号连续发言，群管机器人/系统服务消息不打断。`unknown_after_send` 和待可见性核验消息保守保留占位，不能因本地 Planner 排序覆盖。 |
| `tg_groups` | `tg_peer_id`、`group_type`、`auth_status`、`can_send` | 账号同步得到的群/频道资产 |
| `tg_group_accounts` | `group_id`、`account_id`、`can_send`、`is_listener` | 账号和群/频道的 Telegram 传输能力关系；`can_send` 只表达 Telegram 权限，不能承担 AI 活群群管机器人准入。 |
| `group_bot_admission_policies`（专项设计待开发） | `tenant_id`、`group_id`、`trusted_bot_peer_id`、`completion_policy`、`evidence_ref`、`reason`、`policy_version`、`status`、`created_by`、`revoked_by`、`effective_at`、`revoked_at` | 目标级群管完成协议审计。`follow_sufficient` 与 `explicit_bot_confirmation` 必须绑定目标群和已观察到、已审计的可信机器人 peer；该 peer 才能在 Telegram role 为 unknown 时成为受限控制来源。`not_required` 必须引用连续控制观察，不能建立 bot 信任。写入/撤销需 `targets.manage`、版本并发校验和审计。 |
| `task_group_bot_admissions`（专项设计待开发） | `tenant_id`、`task_id`、`group_id`、`account_id`、`state`、`policy_version`、`admission_version`、`requirement_set_version/hash`、`observation_version`、`surface_kind`、`surface_peer_id`、`viewer_account_id`、`viewer_authorization_id`、`listener_instance_epoch`、`listener_policy_version`、`observed_start_cursor`、`observed_end_cursor`、`surface_identity_hash`、`observation_started_at`、`no_prompt_pass_at`、`observation_gap`、`ready_fact_ids`、`failure_code`、`updated_at` | Task 专属准入投影，唯一 `(task_id,group_id,account_id)`；首版 30 秒窗口只能观察该账号授权视角下的目标群 control stream。起止 cursor、viewer、listener epoch/policy 和 target peer 共同组成不可混用的 observation surface identity；连续 30 秒无可信提示且零 gap 才可按 `no_prompt_30s_passed` ready，新提示推进版本使旧 ready 失效。 |
| `account_group_admission_facts`（专项设计待开发） | `tenant_id`、`group_id`、`account_id`、`target_peer_or_channel_id`、`fact_kind`、`remote_mutation_or_observation_identity`、`surface_identity_hash`、`fact_version`、`outcome`、`source_message_id`、`fingerprint`、`requirement_action_key`、`observed_at` | 不含 `task_id` 的账号视角远端事实；`fact_kind` 仅为 configured/dynamic channel follow、requirement confirmation、post-follow visibility。多个 Task 可引用同一仍新鲜事实，但各自计算 ready；不同 observation surface 的事实禁止拼接成 30 秒无提示结论。 |
| `tg_account_online_state` | `tenant_id`、`account_id`、`desired_online`、`desired_sources`、`online_status`、`session_kind`、`session_id`、`proxy_id`、`last_seen_at`、`last_probe_at`、`last_keepalive_at`、`stale_after_at`、`failure_type`、`failure_detail`、`recovery_status`、`next_probe_at`、`active_task_count`、`reconciled_at`、`updated_at` | 账号在线保活事实源；全局保活和 AI 活跃群、转发、监听任务要求账号持续在线时写入 `desired_online=true`，保活 worker 分批探测和 warm，Planner / Dispatcher 只使用在线就绪账号；掉线、需重登、session 失效、代理异常、stale 状态和需求来源必须可见；任务暂停 / 停止 / 删除或账号范围变更后必须 reconcile 清理来源 |
| `tasks` | `type/status/version/task_lifecycle_epoch/fulfillment_contract_version/account_config/failure_policy/type_config/group_ai_prejoin_channel_ids/stats` | additive`version`是Task行CAS版本，历史backfill=1；所有status/display/lifecycle写推进它，不能拿config_revision代替。纯搜索不读取pacing，四类拟人任务只用pacing计算due/软时间且不得形成任务份额或隐藏数量上限；AI配置频道数组0～3个 |
| `task_start_operations` | 既有`task_id PK/start_operation_id global unique/operation_version/status=processing|started|failed/task_day_ledger_id`加`result_task_lifecycle_epoch/result_route_id/result_route_epoch/result_target_set_hash/result_target_set_hash_version/result_bootstrap_request_revision/result_snapshot_hash/failure_detail` | 每Task 0/1 current row，不新增persistent replaces_id或复合unique。新processing同事务清旧result，started同tx写完整bootstrap identity，failed无result；replace命令以expected current id/version校验并覆盖current row，历史由Audit保留。AI与channel共享这些通用ledger/route/hash结果列，type-specific完整bundle仍由各自BootstrapOperation持有 |
| `search_click_obligations` | `id`、`task_id`、`task_day_ledger_id`、`approved_target_ref`、`state`、`version` | UUID 是点击执行身份，不保存 click/completion ordinal；单目标 Task 每日建立配置数量的稳定义务 |
| `search_click_assignments` | `id`、`obligation_id`、`solver_input_hash`、`assignment_version`、`account_id`、`authorization_id`、`proxy_binding_version`、`execution_policy_version`、`deadline_at`、`state/version`、`owner_id`、`owner_fencing_epoch`、`lease_expires_at` | assignment 行即持久待执行工作；数据库领取和 lease 接管，不依赖通知存活 |
| `search_protocol_sessions` | `assignment_id`、`phase`、`phase_version`、`request_identity`、`viewer_cursor`、`page_fingerprint`、`keyword_id`、`approved_target_ref`、`next_page_identity`、`challenge_fingerprint`、`protocol_sample_version`、`owner_fencing_epoch` | 持久极搜页面状态；热榜、群分类、结果、验证码、点击转换均 CAS，重启从已提交 phase 继续 |
| `fulfillment_remote_facts` | 既有identity字段加`remote_effect_at/confirmation_time_basis/projection_contract_version/required_projection_kinds/count/set_hash` | append-only权威事实只按request/mutation/fact identity唯一，真实第二成功也必须落typed fact；不能按obligation拒绝。remote event或同Attempt原子Gateway成功才可证明时间，created/reconcile时间不可替代；Tx C同事务建齐required ProjectionState |
| `ai_group_message_quantity_fact_bindings` | `remote_fact_id/requested_obligation_id/bound_obligation_id/target_operation_target_id/binding_state/conflict_with_fact_id/confirmation_timeliness/version` | 每fact唯一；bound obligation partial unique。timeliness=`on_time|late|unproven`只按authoritative basis；第二真实fact为unbound conflict并打开enrollment blocker，不确认其他义务/coverage |
| `ai_group_message_quantity_conflict_adjudications` | `unbound_binding_id/decision_revision/conflict_snapshot_hash/decision/evidence_hash/operator/approval_ref/supersedes_id` | append-only裁决不改fact/binding/raw/confirmed/coverage。全部冲突裁决且source/read-model hash稳定后，owner-aware enrollment contract_reopen才可解除blocker；origin route可closed/paused/stopped，普通retry无效 |
| `fulfillment_fact_projection_states` | `fact_id/projection_kind/expected_target_version/state/failure_class/last_error/next_retry_at/lease_owner/epoch/expires/version/projected_at/updated_at` | 唯一fact+kind；required kinds由fact冻结contract registry，settlement按anti-join证明无缺行。recovery按`(next_retry_at,id)` partial index有界CAS drain；retryable failure继续排队，poison/nonretryable打开enrollment blocker，修复不触发业务重发 |
| `task_contract_activation_manifests` | `id,tenant_id,release_train,old_task_ids,new_task_ids,old_set_hash,new_config_set_hash,route_epoch,state,approval_ref,activated_at` | 一个 release train 只有一个 active route epoch；新 Task 先以 `prepared` 独立创建并从 0 建账，canary 直接执行后只 CAS 该单行 route epoch，旧 Task 随即失去 Gateway 权限，再异步逐个 tombstone/物理删除。 |
| `ai_provider_key_versions` | `provider_id`、`secret_ref`、`version`、`active`、`quota_policy_revision`、`active_from`、`retired_at` | secret manager 引用；全系统 partial unique 保证最多一个 active key，所有模型共享其总额度 |
| `actions` | `task_id`、`action_type`、`account_id`、`status`、`claim_*`、`lease_*`、`payload`、`result`、`action_dedupe_key` | 可执行动作；待处理重查只允许更新已满足条件 action 的状态或创建缺失的准入补齐动作，不得绕过 `action_dedupe_key` 生成重复发送 |
| `execution_attempts` | `action_id`、`worker_id`、`attempt_no`、`status`、`gateway_call_started_at`、`result_snapshot` | 执行尝试和结果未知判断 |
| `target_membership_items` | `tenant_id`、`parent_task_id`、`membership_task_id`、`target_id`、`account_id`、`status`、`phase`、`can_send`、`phase_records`、`latest_action_id`、`failure_type`、`failure_detail`、`manual_required`、`next_retry_at`、`ready_at` | 单账号对单目标的准入事实；承载入群、关注、验证、验证聊天读取、图片验证码识别、可发言复检和失败原因 |
| `target_membership_challenge_attempts` | `membership_item_id`、`challenge_type`、`question_hash`、`question_snapshot`、`context_status`、`context_message_count`、`context_failure_detail`、`media_message_id`、`media_fingerprint`、`media_mime_type`、`answer_source`、`answer_text`、`confidence`、`model_name`、`attempt_no`、`status`、`result_snapshot`、`created_by` | 入群验证尝试；记录验证聊天读取、按钮、文本、算数、多模态视觉图片验证码、AI 辅助和人工处理过程 |
| `task_ready_accounts` | `tenant_id`、`task_id`、`target_id`、`account_id`、`membership_item_id`、`ready_status`、`can_send_checked_at`、`capacity_weight`、`expires_at`、`disabled_reason` | 父任务可发言账号池；AI 活跃群和转发目标群只从 ready 账号池规划主互动 |
| `bot_protocol_samples` | `bot_username`、`sample_type`、`sample_hash`、`schema_version`、`structure_json`、`captured_at`、`pii_scrubbed`、`is_active` | 搜索点击任务真实目标机器人协议样本；样本缺失或过期不阻止结构合法 Task 创建，但启动后该 scope 保持 `runtime_state=waiting`，只允许 fixture/只读诊断，不执行真实 click |
| `proxy_airport_subscriptions` | `name`、`clash_subscription_url_encrypted`、`subscription_format`、`priority`、`enabled`、`failover_policy`、`auto_failback_enabled`、`failback_cooldown_minutes`、`max_authorizations_per_node_default`、`all_subscriptions_down_policy`、`notify_admin_on_all_subscriptions_down`、`fetch_interval_minutes`、`last_fetched_at`、`last_fetch_status`、`is_active` | 系统设置 Clash 订阅源；订阅 URL 加密存储，支持 Base64 URI 列表 / Clash YAML / JSON 自动识别，拉取失败、格式识别失败或该订阅健康节点为 0 必须可见，不得静默 fallback；全部启用订阅不可用时复用租户 Bot 通知全部管理员 Chat ID |
| `proxy_airport_nodes` | `subscription_id`、`node_id`、`node_name`、`protocol`、`uri_scheme`、`source_format`、`proxy_host`、`proxy_port`、`node_capacity`、`assigned_authorization_count`、`failover_rank`、`consecutive_failures`、`observed_exit_ip`、`observed_exit_country`、`observed_exit_asn`、`observed_exit_isp`、`exit_ip_stability_score`、`latency_ms`、`health_score`、`is_active` | 订阅解析出的代理节点池；过滤套餐/流量伪节点，节点入口只作为连接配置，风控以真实出口 IP 观测为准，节点按容量随机分配并固定到授权槽位，出口漂移、健康分低于阈值或节点不通时触发授权槽位故障切换或暂停 |
| `proxy_node_failover_events` | `from_subscription_id`、`to_subscription_id`、`account_id`、`developer_app_id`、`authorization_id`、`session_role`、`from_node_id`、`to_node_id`、`reason`、`outcome`、`observed_error`、`admin_notification_status`、`admin_notification_detail`、`admin_notified_at`、`triggered_at` | 搜索目标群点击任务机场节点故障切换审计；当前授权槽位绑定节点不通时优先在同订阅内切到下一个健康节点，同订阅无健康节点时按主备优先级切到备用订阅健康节点；全部启用订阅不可用时记录 `airport_all_subscriptions_unavailable`、推送管理员通知并停止真实操作 |
| `proxy_exit_ip_observations` | `proxy_node_id`、`proxy_binding_id`、`observed_at`、`observed_exit_ip`、`observed_exit_country`、`observed_exit_asn`、`observed_exit_isp`、`check_source`、`raw_response` | 搜索目标群点击任务代理出口 IP 观测历史；用于判断 `airport_clash` 节点真实出口、国家、ASN、ISP 和漂移 |
| `account_proxy_bindings` | `account_id`、`developer_app_id`、`developer_app_api_id_snapshot`、`authorization_id`、`session_role`、`proxy_node_id`、`proxy_provider`、`proxy_type`、`proxy_host`、`proxy_country`、`proxy_asn`、`observed_exit_ip`、`observed_exit_country`、`observed_exit_asn`、`ip_reputation_score`、`last_health_check_at`、`last_failover_at`、`binding_generation`、`binding_scope`、`is_active` | 授权槽位代理绑定；同一授权槽位只能存在一个 active 绑定，同一账号不同 TG 开发者应用、session key 和主 / 备用授权槽位可以绑定不同代理出口。首版要求独享静态住宅 IP 或 `airport_clash` 健康节点；节点故障切换后递增绑定代际并重新进入授权槽位 warmup。`binding_scope` 字段区分绑定粒度：默认 `authorization_slot`（授权槽位级，仍守 `max_authorizations_per_node_default=1`），新增 `group` 枚举值专用于 `search_rank_deboost` 分组级绑定（见 `account_group_proxy_bindings`） |
| `account_group_proxy_bindings` | `account_pool_id`、`proxy_airport_node_id`、`runtime_proxy_id`、`binding_scope='group'`、`binding_generation`、`observed_exit_ip`、`observed_exit_country`、`observed_exit_asn`、`observed_exit_isp`、`exit_ip_stability_score`、`health_score`、`status`、`bound_at`、`last_probe_at`、`last_probe_error` | 降权任务专用分组级持久代理绑定；1 分组 = 1 active 运行端点，多个任务复用。`runtime_proxy_id` 必须指向 Telethon/HTTPS 可执行的 SOCKS/HTTP 端点；原始 VMess/VLESS/SS 节点只有经过 Clash/sing-box materializer 暴露运行端点后才可绑定。任务停止/删除不解绑；切换节点 generation + 1，并使旧 generation action 失效。 |
| `search_rank_deboost_click_reservations` | `tenant_id`、`task_id`、`action_id`、`account_id`、`account_pool_id`、`keyword_hash`、`local_date`、`hour_bucket`、`reserved_count`、`consumed_count`、`status`、`expires_at` | 降权任务逐点击配额预占；每 action 固定预占 1 次点击，按账号、账号+关键词、分组共享 IP 和任务小时窗口在同租户锁内原子计数。pending action 的过期预留释放并跳过；confirmed 后 consumed；Gateway 调用边界后的异常、worker 失联和 `unknown_after_click` 保持 unknown 占用且禁止自动重试；released action 重试前必须重新核验共享配额。 |
| `account_environment_bindings` | `account_id`、`developer_app_id`、`developer_app_api_id_snapshot`、`authorization_id`、`session_role`、`proxy_binding_id`、`device_model`、`system_version`、`app_version`、`platform`、`lang_code`、`system_lang_code`、`region_code`、`client_identity_key`、`fingerprint_locked`、`health_score` | 授权槽位环境栈；客户端元数据和代理绑定粒度都是账号 + TG 开发者应用 + 授权槽位。MTProto 客户端元数据按配置在下一次连接 / 重登 / 新 session 初始化时生效；主/备用授权不得复用客户端元数据组合，也不得复用同一代理节点 |
| `account_proxy_warmup_states` | `account_id`、`developer_app_id`、`authorization_id`、`session_role`、`proxy_binding_id`、`stage`、`stage_started_at`、`first_action_at`、`daily_actions_count`、`total_actions`、`reset_reason` | 搜索目标群点击任务 `(账号, TG 开发者应用, 授权槽位, 代理)` warmup 进度；换代理节点后该授权槽位重新进入 warmup |
| `fingerprint_combo_history` | `developer_app_id`、`combo_key`、`device_model`、`system_version`、`app_version`、`platform`、`assigned_authorization_count`、`first_assigned_at`、`last_assigned_at` | 设备指纹组合审计摘要；用于同应用同组合上限校验、主备复用检查和运行时生成结果追踪 |
| `account_authorization_execution_locks` | 存量兼容只读；迁移后以 `remote_mutation_key`、`action_id`、`authorization_id`、`session_role`、owner epoch、有效期记录 | 仅对同一远程副作用 identity 幂等 fencing；不再作为账号级执行互斥，同账号不同非冲突 Action 可并行 |
| `ip_reputation_history` | `proxy_binding_id`、`checked_at`、`score`、`source`、`raw_response` | 搜索目标群点击任务 IP 信誉历史；来源包括 IPQS、Spamhaus、自有观察等 |
| `search_join_action_stats` | `action_id`、`task_id`、`account_id`、`authorization_id`、`session_role`、`bot_username`、`keyword_hash`、`keyword_display_encrypted`、`business_region`、`account_locale`、`proxy_country`、`target_group_id`、`target_position`、`total_results`、`pre_join_decoy_clicks`、`post_join_safe_navigation`、`post_join_policy`、`join_status`、`dwell_seconds`、`hourly_bucket`、`hourly_execution_target`、`linked_task_status`、`linked_task_block_reason`、`error_code` | 纯搜索点击只写 search/page/match/click、小时执行量、排名轨迹和失败字段；`post_join_*`、`join_status`、联动字段仅为 `legacy_mixed_search_join` 历史只读列，新 `click_only` 不写入也不据此判断完成；日志和 stats 不保存关键词明文 |
| `search_join_rank_observations` | `task_id`、`bot_username`、`keyword_hash`、`keyword_display_encrypted`、`target_group_id`、`observed_position`、`total_results`、`observed_region`、`observation_source`、`paid_keyword_ad_status`、`jisou_ecosystem_status`、`target_relevance_score`、`target_content_health`、`observed_at` | 搜索点击任务排名观察快照；只用于效果复盘和调研规则解释，不计入 click success，不把付费广告、流量联盟或内容健康变化归因到某次 click action |
| `search_join_pacing_decisions` | `task_id`、`decision_scope`、`decision_key`、`tenant_timezone`、`local_date`、`hour_start`、`account_id`、`keyword_hash`、`decision`、`sampled_value`、`threshold`、`scheduled_at`、`reason` | 搜索目标群点击任务节奏采样和跳过决策；保证日 / 小时跳过、小时 / 天抖动在重复 planner tick、worker 重启和 retry 后可复现，并支撑详情页解释 pacing 未规划原因 |
| `search_join_linked_task_dispatches` | `search_join_action_id`、`source_task_id`、`linked_task_id`、`account_id`、`target_group_id`、`link_type`、`status`、`block_reason`、`can_send_checked_at`、`activation_not_before`、`ready_pool_item_id` | 仅保留 `legacy_mixed_search_join` 的历史联动事实；纯搜索点击不得创建联动投递，也不得把该表作为 click 完成证据 |
| `target_runtime_summary` | `target_id`、`status`、`open_issue_count`、`failed_action_count`、`affected_task_count`、`latest_failure_at`、`summary_snapshot` | 运营中心目标工作台读模型；失败计数和最近失败时间覆盖 `failed`、`retryable_failed`、`unknown_after_send` 等未闭环异常 |
| `task_runtime_summary` | `task_id`、`target_id`、`planned_count`、`success_count`、`failed_count`、`pending_count`、`oldest_pending_at`、`latest_failure_type` | 任务列表和运营中心关联任务失败读模型；`latest_failure_type` 必须覆盖 `failed`、`retryable_failed` 和 `unknown_after_send` 等未闭环异常，不能只看普通失败 |
| `account_runtime_summary` | `account_id`、`health_score`、`risk_level`、`score_reasons`、`identity`、`authorization_summary`、`device_summary`、`pending_execution_summary`、`send_available`、`listen_available`、`join_available`、`comment_available`、`profile_available`、`verification_available`、`capacity_remaining`、`capacity_explanation`、`capacity_block_reason`、`unavailable_reason`、`next_retry_at`、`success_count`、`failed_count`、`flood_wait_count`、`restricted_count`、`latest_error_at`、`summary_updated_at` | 账号健康分、身份、授权资产摘要、登录设备摘要、待处理执行摘要、可用性、容量和失败趋势权威读模型；容量不得只展示裸数字，必须能解释小时剩余、日剩余、账号冷却、当前 pending/executing/unknown_after_send 占用和汇总是否过期 |
| `operation_issue` | `target_id`、`issue_type`、`severity`、`representative_task_id`、`representative_action_id`、`affected_task_count`、`affected_account_count`、`failure_type`、`failure_reason`、`suggested_action`、`handling_mode`、`return_to`、`claimed_by`、`claimed_at`、`status` | 运营中心按目标展示异常和处理建议 |
| `operation_issue_sources` | `issue_id`、`source_type`、`source_id`、`failure_type`、`latest_seen_at` | 运营异常来源分页表，覆盖 task、action、message_task、listener、risk_event |
| `operation_issue_accounts` | `issue_id`、`account_id`、`impact_type`、`latest_seen_at` | 运营异常影响账号分页表 |
| `listener_source_state` | `source_type`、`source_peer_id`、`account_id`、`lease_owner/lease_expires_at`、`last_remote_message_id/last_event_at/last_error/collect_window_seconds/updated_at/version` | 群/频道监听或采集水位；频道空成功也推进observation version，进程内防抖不能当来源真相 |
| `rule_sets` | `name`、`status`、`task_types`、`active_version_id` | 规则集 |
| `rule_set_versions` | `version`、`status`、`filters`、`transforms`、`routing`、`account_strategy`、`rate_limits` | 规则版本 |
| `account_proxies` | `protocol`、`host`、`port`、`status`、`alert_status`、`max_bound_accounts` | 本地代理资源 |
| `tg_account_security_batches` | `action_types`、`status`、`profile_strategy`、`avatar_strategy`、`trace_id`、`started_at`、`finished_at` | 安全/资料批次；资料初始化、清理登录设备、设置二步密码、备用 session 补齐都需派生任务中心系统任务投影 |
| `tg_account_security_batch_items` | `batch_id`、`account_id`、`status`、`profile_status`、`username_status`、`avatar_status`、`avatar_source`、`device_cleanup_status`、`two_fa_status`、`standby_session_status`、`failure_type`、`failure_detail`、`next_retry_at` | 单账号批次项；头像缓存状态可由 `avatar_source` 关联素材缓存状态派生，必要时在响应中输出 `avatar_cache_status` |

运营读模型不得反向阻塞核心任务规划。Planner 事务只刷新当前任务规划所需的轻量统计，以及任务级 planned / success / failed / pending、oldest pending、latest failure、runtime stage、目标关联和单个代表失败异常；不得在同一 Planner 事务中遍历任务全部账号并逐个刷新 `account_runtime_summary`、账号容量 / 风险信号或 `operation_issue_accounts`。全账号运营摘要由 metrics worker 或等价独立观测刷新链路负责，按显式 `account_summary_batch_size` 分批且游标续跑，每批独立提交，不能以 batch limit 静默漏掉后续账号。metrics 失败不回退到 Planner 内重算，必须通过 worker heartbeat、错误和摘要 `updated_at` / stale 状态显式暴露。

职责拆分不得削弱运营语义：`account_runtime_summary` 仍保留账号健康分、风险、身份 / 授权 / 设备摘要、发送 / 监听 / 入群 / 评论等可用性、小时 / 日容量、pending / executing / unknown 占用、不可用原因、下一重试时间和 24 小时趋势；`operation_issue`、`operation_issue_sources`、`operation_issue_accounts` 仍保留异常类型、严重度、代表 task / action、failure type / reason、建议动作、handling mode、来源、影响账号、impact type、latest seen 和人工处置状态。分批刷新期间页面读取最后一份已提交快照并显示更新时间，不得伪装为实时；Planner 成功不能清除未被 metrics 重新确认已恢复的异常。
| `archived_members` | `archive_id`、`member_peer_id`、`display_name`、`username`、`phone_number`、`phone_masked`、`snapshot_at` | 归档成员快照；导出优先完整手机号 |
| `audit_logs` | `actor_id`、`action`、`object_type`、`object_id`、`result`、`trace_id`、`reason`、`snapshot` | 审计记录；涉及账号、验证码查看、设备清理和接码身份变更时，snapshot 必须保留操作原因、完整对象 ID 和关键前后状态 |
| `source_media_assets` | `source_peer_id`、`source_message_id`、`cache_status`、`cache_version`、`cache_message_id` | 转发源媒体临时缓存 |
| `worker_heartbeats` | `worker_id`、`process_type`、`status`、`last_seen_at`、`heartbeat_metadata` | worker 存活 |

`search_rank_deboost` 分组级代理绑定例外条款（对 `max_authorizations_per_node_default=1` 约束的例外，与 §2.8 口径复核表中「非目标浏览」行的例外条款并行生效）：

1. 节点容量 = 该分组账号数，不再守 1；同一节点可被该分组内多账号共享出口 IP。
2. 同一节点不得同时被授权槽位级绑定（`binding_scope='authorization_slot'`）和降权分组级绑定（`binding_scope='group'`）复用，避免污染其他任务账号画像；运营尝试在降权分组上绑定已被授权槽位级绑定占用的节点时，系统必须拒绝并返回错误「节点已被授权槽位级绑定占用，不可同时用于降权分组」。
3. 分组级绑定通过 `binding_scope='group'` 字段与授权槽位级 `binding_scope='authorization_slot'` 区分；`account_proxy_bindings.binding_scope` 默认 `authorization_slot`，存量数据保持默认。
4. 分组级绑定节点健康分 < 60、订阅失效、节点消失、出口 IP 漂移时，必须暂停该分组所有 action 并告警（`rank_deboost_group_ip_drift` / `rank_deboost_node_unreachable`），不做静默 fallback；不得回退本机直连或回退授权槽位级代理。
5. 分组级绑定节点故障切换时，只允许在同一订阅内切换到下一个健康节点，新节点容量同样 = 分组账号数；切换后整组账号重新进入 warmup，并写 `proxy_node_failover_events` 审计。
6. Executor 在 action 进入 `executing` 前必须通过分组级绑定代理探测 `observed_exit_ip`，确认与该分组最近一次出口观测一致；代理失败、DNS / TCP 直连、出口 IP 漂移时 action 必须 `skipped` 并写 `proxy_egress_guard_failed`，不得回退本机直连或授权槽位级代理。

账号详情执行记录是聚合读路径，不新增第二套发送事实源。读取时按账号 ID 聚合 `message_tasks`、`message_task_attempts`、`tasks`、`actions`、`execution_attempts` 和必要的历史兼容任务投影，按远端 message id、action id 或兼容 dedupe key 去重，再输出统一的发送 / 评论 / 回复 / 频道互动 / AI 活跃群 / 准入动作记录。

目标准入表约束：

- `target_membership_items` 对同一 `parent_task_id + target_id + account_id` 必须唯一；同一账号重新准入只能更新当前 item 或追加阶段记录，不能生成多个并行 item。
- `membership_task_id` 指向系统子任务投影；如果实现阶段暂不落真实 `tasks` 子行，也必须提供稳定 ID 供任务列表、详情和审计引用。
- `phase_records` 只保存阶段摘要和最近 action 引用，原始执行事实仍以 `actions`、`execution_attempts` 和 `target_membership_challenge_attempts` 为准。
- `task_ready_accounts` 是可查询的父任务 ready pool，不从历史 action 临时扫描推导；当账号离线、被禁言、目标权限变化、人工跳过或 `expires_at` 过期时，必须失效并记录 `disabled_reason`。
- `target_membership_challenge_attempts.question_hash` 用于控制同一账号对同一目标同一验证问题最多自动尝试一次；出现新问题、人工确认或运营手动重试时才允许新增自动尝试。

### 4.3 主要关系

```text
平台实例（底层表 tenants）
  -> app_users
  -> telegram_developer_apps
  -> tg_accounts
      -> account_pools
      -> account_proxy
      -> account_runtime_summary
      -> tg_group_accounts -> tg_groups
      -> tg_contacts
      -> security snapshots / batches

operation_targets
  -> channel_messages
      -> channel_message_comments
  -> tasks.type_config.target_*

operation_plan_templates
  -> operation_plan_targets -> operation_targets
  -> operation_plan_task_links -> tasks
  -> operation_plan_generation_runs -> audit_logs

tasks
  -> actions
      -> execution_attempts

rule_sets
  -> rule_set_versions
  -> tasks.type_config.rule_set_version_id

listener_source_state
  -> group_context_messages / channel_messages / source_media_assets
```

---

## 5. 执行器 PRD

### 5.1 Worker 角色

当前代码已经提供 planner / dispatcher / listener / recovery / metrics 分角色 drain 入口；本节描述这些角色的长期契约。生产是否已稳定拆成多个独立 worker 进程、并发配额、容量面板、token 预留 / 退款是否完整落地，需要结合部署、心跳、指标和压测继续确认。

| Role | 输入 | 输出 | 禁止事项 |
| --- | --- | --- | --- |
| Planner | running tasks、typed DueSet、规则/账号/scope revision | stable obligation/FOP、allocation assignment、immutable intent、`generation_pending`；仅mask-missing或check-in handoff分支可创建ready签到Action | 不调用TG/Provider；normal body不得创建Action或GenerationJob |
| AI Generation | open AI message obligation、GenerationJob、context/profile/policy revision | immutable variation、message memory、ready Action、provider reconcile | 不调用 TG；same-basis 不重置生成预算，不新建业务义务 |
| Dispatcher | due ready actions | execution attempt/Gateway journal、canonical remote fact provenance、账号状态 | 不调用AI Provider、不生成或补写正文、不创建新业务义务/Action；Telegram外调不得持有DB事务 |
| Listener | listener source、监听账号、源目标 | 上下文、监听水位、源媒体缓存、事件 | 不发送业务消息 |
| Recovery | 超时 claim/lease、AI wake/deadline、Gateway reconcile、remote fact projection、worker 失联、unknown | typed state/FOP、target/coverage/wake 投影、任务错误摘要、审计、unknown membership 有界补偿复检 | 不能无上限调用 TG；不能自动重发业务消息；投影失败不能 resurrection Action |
| Account Security | 账号资料初始化、设置二步密码、清理登录设备、备用 session 补齐 / 自愈批次 | 执行 `tg_account_security_batch_items` pending/waiting 项；设备清理只领取 eligible pending 项，不创建 waiting 项；回写 profile / username / avatar / 2FA / device / standby session 结果 | 调用 TG，必须独立运行；普通账号维护凭据默认直连，不读取账号级 `proxy_id` |
| Metrics | action、task、worker、账号、代理、Redis | runtime snapshots、daily stats | 不改变业务状态 |

### 5.2 Planner 规划要求

- 扫描 `running` 且存在开放义务或事实变化的任务；资源释放事件和数据库轮询共同唤醒。
- 检查任务 lifecycle/deadline、任务类型安全事实和当前阶段真实空闲槽；五类当前合同不检查静默期、速率、Window、任务份额或预扣。
- 调用任务类型 plan builder。
- 用 `plan_batch_key` 标记本轮计划。
- 用 `action_dedupe_key` 做业务去重。
- 更新可重建 `tasks.stats`；`next_run_at` 只用于明确远端 retry-at/事实复探，不承担速率或 Window 排程。

AI 活跃群 Planner 需要额外满足：

- 任务日通过 `TaskDayLedger` 固定 `timezone_snapshot`、period、planning anchor、deadline 与系统 `natural_full_day` pacing snapshot；按冻结曲线计算 `due_by_now` 与每个 quantity ordinal 的一次性 `due_at`，future unit 不提前领取。旧“任务日不生成 due-by-now”是 `historical_do_not_implement`。
- 本轮按 coverage/extra 两类不可互换主义务的 open 状态和 Generation/interaction 真实空闲槽形成 JIT `batch_size`；blocked coverage 不转成 extra，资源已满显示 `capacity_gap`，不形成预扣或任务份额。
- 每个本任务当前必达账号当日至少 1 条；先从开放、未覆盖且 ready 的 coverage 义务选择账号。extra-volume 义务只能从当前任务日 ledger 已 `confirmed` 且存在 active/usable 账号面具的 ready 账号中，按成功数最少、最久未发和稳定账号 ID 选择，绝不能抢占 recovering 账号的 coverage 义务。缺面具账号的 coverage 签到远端成功时可同时增加 1 条群日总量，但不得因此领取独立 extra-volume 义务；单账号缺面具不得阻塞其他合格账号继续补量。
- Planner与Generation漏斗必须分账：Planner请求/物化stable unit数、generation_pending数、Provider候选/清洗/质量数、check-in handoff数、ready Action数与终结原因；不得把Planner批次数或Action数冒充完成。
- current AI引用关系在生成前由immutable`ContentAllocationPlan/RequirementAssignment/ContentIntent`冻结；Planner按aggregate合同分配stable obligation并选择由typed bound fact证明的reply source，只enqueue`generation_pending`。ai-generation worker才创建/读取唯一GenerationJob并持久variation；不得先建空正文/pending Action或事后把direct升级为reply。
- 引用候选不足、required material缺失或content basis暂不可用时，原assignment/obligation进入typed durable waiting并由对应source revision唤醒；不得把relation/material降级、另建数量unit或用stats/last_error代替状态owner。
- Generation worker按同intent basis执行主/备用各最多3轮并append variation/rejection。normal accepted variation+memory同事务创建ready Action；只有coverage已完成的direct extra-volume在六轮耗尽时可由Generation终结job并写唯一`AiGroupCheckInHandoff/check_in_ready`，Planner消费handoff以scoped claim+memory+intent创建ready签到Action且不再调用Provider。未完成coverage、reply/material或其他不合资格六轮失败进入`content_capacity_gap`；owner占用/deadline分别进入hold/shortfall。
- Dispatcher只领取ready Action并在Tx A/B复核obligation/intent/variation/route/lifecycle；它不批量补写pending文案。旧`ContentMixContract/CycleSlot/primary quantity slot`只作迁移投影/alias，不是current数量、内容或Action owner。
- 生成预览接口不创建 action，但仍必须按请求数量校验 AI 候选完整性；短列表不是成功预览，必须暴露为 AI 候选不足。
- 运行中保存手工群日目标时写下一 ledger 的 pending config revision；账号范围变更写 current scope revision，并按专项目标 CAS 转换/新增/取消尚未 call-issued 的 stable unit。Gateway-started/unknown/confirmed 事实不改写。引用/素材规则只影响未绑定 current intent；历史事实始终保留。

准入前置子任务 Planner 需要额外满足：

- 以父任务账号范围和目标为输入，生成入群、关注关联频道、验证和可发言复检 action。
- `max_concurrent` 和准入子任务并发只控制同时执行数量，不能截断最终需要准备的账号池。
- AI 活跃群和转发目标群的准入 action 默认使用 4 小时排程窗口：首个待准备账号立即进入队列，最后一个待准备账号计划时间不得晚于子任务创建后 4 小时；到期 admission retry 只在父任务已获 Claim Window 份额内优先。
- 子任务成功的账号必须立即写入父任务可发言池；父任务下一轮可使用新账号。
- 子任务失败、人工处理和等待审批必须保留账号级状态，父任务只扣除不可用容量，不把它们计为主互动失败。
- 每个准入 action 必须带稳定 `action_dedupe_key`，至少包含 `membership_item_id + phase + challenge_question_hash`；同一阶段已经 pending / claiming / executing 时不得重复生成。
- FloodWait、慢速模式、等待管理员审批和人工处理必须写入 `target_membership_items.next_retry_at` 或 `manual_required`，不能通过跳过 action 假装完成。
- 验证问题变化时必须生成新的 `question_hash` 和 challenge attempt；旧问题过期后不得复用旧答案。
- Recovery 只能恢复超时 claim / lease 和重建聚合状态，不能替运营人员自动跳过 `manual_required` 账号。

频道浏览 Planner 需要额外满足：

- 先解析初始帖子池和持续监听帖子池，不得把频道全部历史消息纳入规划。
- 每个ledger把冻结initial source与append-only dynamic source转成唯一`(ledger,target_peer_id,channel_message_id)` target；共享Assembler按route AccrualClock segments计算各message当前DueSet，并与bound fact/Gateway/unknown/identity-conflict/有效ActionBinding做同snapshot anti-join得到MaterializationGap。禁止全天scalar gap或Task级每日安全上限缩小目标。
- 在整个ledger上把所有gap与未用`(tenant,peer,message,account,obligation_local_date)` daily identity及deadline前账号离散时隙做稳定最大匹配；future Action只占自身账号slot，新due可插入同日future前/中/后，不能以Task max future作floor、整批平移或恢复180秒Task全局间隔。
- due unit唯一`(daily_message_target_id,due_ordinal)`，账号只是可替换的pre-Gateway materialization；legacy/current共同CAS全局DailyIdentityOwner后才创建ActionBinding/Action。Gateway后只认永久ViewRemoteFact+唯一bound FactBinding完成，次日/expire仍保留本ledger已累计DueSet并由immutable settlement收口。

频道评论 / 回复 Planner 需要额外满足：

- 先按单条频道消息累计目标和本轮小时预算计算补差额，再按 `reply_min_per_message` 在补差额内拆出不可变引用回复 action；后续节奏、质量失败和单表情兜底均不得把 reply 槽位改成 direct。
- 引用回复 action 必须在 AI 生成前绑定具体讨论区评论或本任务历史成功评论；Planner 只创建带账号、频道消息、direct/reply、引用目标、预算和排期快照的 `ai_generation_status=pending` Action，直接评论和引用回复分别由 Dispatcher 走普通评论 Prompt 和引用回复 Prompt。
- 评论 `action_dedupe_key` 必须只依赖任务、频道消息、账号、direct/reply、引用目标和规划槽位等稳定事实，不依赖尚未生成的文本；Planner 禁止调用 AI 生成、重描述或 provider-backed 质量判断。
- `reply_min_per_message` 不能额外抬高单条频道消息总目标；当本轮补差额小于最少引用回复数时，以本轮补差额为可规划上限，并记录可见差异。
- 引用池不足、讨论区评论未采集、历史成功评论缺少 Telegram 远端消息 ID 或 AI 未生成足够引用回复候选时，不得把直接评论标记成引用回复。

搜索目标群点击任务的创建与 Planner 需要额外满足：

> **历史合同（`contract_status=historical_do_not_implement`）：** 下面从旧 `search_join_group` 双目标开始、到“最终纯点击覆盖”之前的内容只用于说明旧实现和迁移来源；其中的成员目标、admission child、旧权限、旧任务类型与 repeat 规则均不得用于新建、编辑、运行或验收。

- `search_join_group` 新建及专用编辑 API 接收：目标群（`target_title` + `target_link`）、搜索关键词、每日目标点击数 `daily_click_target_count`、每日成员关系观察目标 `daily_target_count`、显式的 `allow_same_account_repeat_application`、单个普通 `account_group_id`、任务每日 source action 基础预算 `max_actions_per_day`、完成截止时间 `scheduled_end`、日/小时抖动 `daily_jitter_percent` / `hourly_jitter_percent` 与可选 `quiet_hours`。存量任务的专用编辑还可受控调整 `actions_per_round`（1..20）、`max_actions_per_hour`（1..500）和 `hourly_min_successful_joins`（1..500），用于把已配置的每日点击目标在真实剩余时段内排程；新建页不暴露这些运行时容量字段。`daily_click_target_count` 按 source `search_join` 的精确目标命中与 `target_found_at` 计数；`daily_target_count` 只按真实 `membership_observed_at` 的本地日期计数，二者绝不互相替代或把 pending/失败伪装为成功。source 命中目标后必须创建唯一 `search_join_membership` 准入子 action；子 action 继承 source 的授权槽位、开发者应用、代理绑定和客户端 metadata，禁止回退账号主 session。source 在 claim 前和 Gateway 前都必须恢复 payload 授权槽位所属账号，且不能因全局账号容量转派；申请待审批时，若本租户已配置该目标群的救援管理员，可由管理员以自身 session 审批，再立即用 source 授权槽位复核成员关系；审批成功本身不能计入成员关系完成。否则同一 source 的子 action只做成员复核，不重复提交。启用 `allow_same_account_repeat_application=true` 后，不再用该账号的未决申请、账号日限额或关键词日限额阻止新的 source；每条新 source 仍各自创建唯一 child 并记录 Telegram 的真实申请结果。点击进度写 `stats.search_click_target`，成员关系进度写 `stats.search_join_membership_target`；当天点击目标达成后停止创建新的 source，但既有 child 继续收口，次日按新的自然日重新计算。跨日仍未进入 Gateway 的 source（`pending` / `claiming` / `executing`）是当日点击槽位和 source 基础预算的 carryover，不能既忽略它又额外创建同等数量的新 source。严格每日目标未满时，`target_admission_retry` 与已点击 child 保留不可替代优先级；严格 source 与 AI hard-hourly 由 `DispatchClaimScope -> DispatchClaimWindow -> DispatchClaimShardAllocation -> DispatchClaimReservation` 的需求/urgency 份额决定领取，普通批量只能使用剩余容量。仅当全局和任务积压硬上限均未触发时，可越过“最旧 pending 年龄”软阻塞补足受剩余槽位约束的计划。严格目标在当天已有 `success` 或 `failed` source 未写 `target_click_observed/target_found_at` 时，每条未命中 source 都使 effective source budget 增加一条 replacement；`pending` / `claiming` / `executing` / `unknown_after_send` 始终占槽位、不会重复补量。replacement 同样受截止时间、静默、账号/授权槽位绑定、全局容量和 Gateway 风控限制，不能伪造真实 Telegram 点击或成员关系结果。`max_actions_per_day` 是 source 基础预算，必须不小于 `daily_click_target_count`（旧任务没有该字段时继续按历史 `daily_target_count` 校验）；完成只由 `scheduled_end` 或运营显式停止触发。每日任务因旧 `scheduled_end` 进入 `completed` 后，运营将截止时间改为未来时必须重新入队，避免既不能继续当天补量、也不能在次日重新计算每日目标。
> **2026-07-28 修订：** 上一条中“repeat 可绕过账号日限额或关键词日限额”已失效。`allow_same_account_repeat_application=true` 只解除旧 membership pending 对新 source 的阻断；账号/关键词日限额、小时冷却、授权槽位、代理、静默和 Gateway 去重继续生效。上一条中的 AI hard-hourly 已退役，当前仲裁对象是 AI 群日债务。冲突表述只保留为历史事故说明，不得用于实现、迁移或验收。

> **2026-07-28 搜索双目标与 admission 日归属 supersede：** 上段的旧 `daily_target_count`、每个 source 无条件创建 membership child、repeat 绕过账号/关键词日限额、AI hard-hourly 固定优先级和本地日期 carryover 均只作历史实现说明。新字段固定为 `daily_click_target_count + join_target_group_after_click + daily_admission_target_count`：开关关闭只收口 click；开启才为已点击账号创建唯一 admission child，并复用 AI 的 join → 可信群管频道关注/确认 → membership + can_send 复检状态机。repeat 不得绕过安全日限额；Dispatcher 使用父业务任务 Claim Window 公平份额，不存在 hard-hourly 类别或 child 对其他父任务的固定抢占。每份 ledger 冻结开关和双目标；当前 ledger 未截止时编辑只写 pending revision，在 deadline 后生效。deadline 前合同不变；关闭生效后，旧 ledger 的 pre-Gateway child 终结为 `join_switch_disabled_after_deadline`，Gateway-started/unknown 只核验已发生的真实结果且不再发起 join/follow/confirm，历史事实和 unknown 不删除。source 在规划时绑定 `task_day_ledger_id` 并冻结 `obligation_local_date/timezone_snapshot/timezone_revision/period_start_at/deadline_at`，child 永远继承该 ledger。仅当任务仍 running 且当前生效开关仍开启时，child 才可跨日继续发起新的准入动作；deadline 后 ready 只进 `late_admission_ready_count`，旧 ledger 保持 missed、不自动计新 ledger。新 ledger 同账号须有绑定该 ledger 的新 click 才建立 `(tenant,task,target,account,task_day_ledger_id)` 唯一 admission 义务，可复用既有 membership/admission 后实时复检 can_send。时区修改从旧 ledger deadline 建立连续的过渡 ledger，本地日期即使重复也不得合并。

> **2026-07-29 最终纯点击与存量接管覆盖：** 紧邻上方“搜索双目标”段也转为历史。当前纯搜索点击固定为 `task_type=search_click + search_execution_mode=click_only + daily_click_target_count`，正式 API/权限为 `/api/tasks/search-click + tasks.create.search_click`；join switch、admission 目标或成员目标不允许写入，click 取得完整远端事实后 ordinal 结束。当前未结束的存量混合 click+membership Task 在发布接管时转为纯 click，后续不再新建 membership/admission child；既有 Gateway-started/success/unknown 与全部历史事实不改绑。“搜索点击加入”仅登记为后续独立模式，字段、状态机、QA 和发布均待专项 PRD。

#### 5.2-HIST 旧任务接管、Reservation 与锁序（historical_do_not_implement）

以下 2026-07-29～2026-07-30 接管、预绑定、Reservation、Claim Window 与跨表锁序只保留事故审计；当前旧 Task 不迁移、不接管、不复活，切路由后逐个删除。

> **2026-07-29 五类任务运行合同分界：** AI 活群、评论、点赞、浏览、纯搜索点击的未结束 Task 均由幂等接管切入新履约模型。已绑定新类型义务的 Action 原位续跑；未绑定且未进 Gateway 的旧 AI/search Action 显式终结、释放运行 claim 后由新义务重排。旧 Gateway-started/success/unknown 保留历史且不猜测计入新合同。评论、点赞、浏览只有在稳定天然键与远端事实证据完整时才回填 confirmed，证据不足保持 unknown。该接管不改变 AI/评论既有 direct/reply、图片、表情包或普通 emoji 占比；新 AI ContentMix 对完整新合同重新冻结比例。

> **2026-07-30 存量 Action 终态补充：** “保留 Gateway 历史”不等于允许未绑定新义务的旧 Action 回到 pending 并领取新 Reservation。接管每次幂等执行都要收口未绑定 `primary_quantity_slot_id/search_click_obligation_id` 的旧 AI/search Action：无 Gateway Attempt 的 skipped，最后 Attempt 已明确 success/failed 的保持对应历史终态，Gateway 已开始但结果不确定的固定为 unknown；历史 Attempt 不删除，也不计入新合同。AI 旧 `coverage_capacity_status/proof`、sendable proof 及“容量不足已停止创建”错误同时清除，避免已取消的群日/小时门禁继续显示 blocked。

> **historical_do_not_implement（2026-08-04 AI 原槽接管旧案）：** fact-first AI 中已经绑定 `ContentMixCycleSlot/primary_quantity_slot_id` 的失败或跳过 Action 不允许被通用 retry 原地复活；Planner 只接管 `pending CycleSlot + 旧终态 Action + 无 gateway_call_started_at` 的精确组合，以当前槽和旧 Action 身份做单行 CAS，成功后释放旧 coverage reservation、恢复原主数量槽，并在同一冻结槽上创建递增 `slot_attempt` 的新 Action。重建始终沿用 `CycleSlot.primary_quantity_slot_id` 及该数量槽的 coverage/account/task-day，禁止借用新一轮其他槽。Gateway-started、unknown、success 不接管。该规则用 CAS 与唯一键收敛，不增加任务锁、账号锁或跨表锁序。 current实现不得沿旧CycleSlot重建Action；只按AI专项final takeover manifest将旧行分类为alias、可证pre-Gateway释放或Gateway hold/fact。

> **2026-07-30 搜索预绑定时间语义补充：** `SearchClickOpportunityAssignment` 已绑定 Action 后，Dispatcher 在 plan 与原子 confirm 两处判断 Claim Window 是否仍开放时，必须把 PostgreSQL 返回的 aware `bucket_end` 与业务当前时间统一为 `Asia/Shanghai` aware 时间后比较。时间对象表示差异不得抛出异常、不得阻断同一 Dispatcher drain 中的 AI/评论/点赞/浏览，也不得把已绑定搜索 Action 重新交给通用未绑定 claim。

> **2026-07-30 Planner 接管顺序补充：** 五类新履约任务的每次 Planner 扫描必须先执行幂等接管并按接管后的数据库事实刷新全局 backlog，再执行合法新 Action 的失败重试与规划。通用 `retry_failed_actions` 永久排除未绑定 `primary_quantity_slot_id/search_click_obligation_id` 的旧 AI/search Action；禁止出现“先复活旧 failed Action → 旧 backlog 阻断接管”的循环。

> **2026-07-30 预绑定 claim 锁序补充：** 搜索预绑定 Action 的原子 confirm 必须与通用共享 claim 使用相同的 `DispatchClaimScope → DispatchClaimWindow → DispatchClaimShardAllocation → DispatchClaimReservation` 锁序，最后才锁 `SearchClickOpportunityAssignment`。禁止 assignment/reservation 反向锁回 Window/scope；四 Dispatcher 并发下 PostgreSQL deadlock 必须为 0。

> **2026-07-30 搜索 finalize 锁序补充：** `SearchClickAssignmentEpoch` finalize 在 `SERIALIZABLE` 事务中必须按 `DispatchClaimWindow → DispatchClaimTaskAllocation → DispatchClaimShardAllocation → DispatchClaimReservation → SearchClickFulfillmentObligation` 取锁。禁止先读取 Reservation 再反向派生 TaskAllocation/ShardAllocation ID；该预读会与并发 rebuild 写入形成序列化冲突，连续整轮 `abandoned`。父 allocation 行必须直接按当前 Window 与 `dispatch_allocation_epoch` 查询并锁定，最后才读取并锁定来源 Reservation。

> **2026-08-04 搜索 assignment 直接执行 supersede：** 纯搜索点击删除 `next_run_at` 对齐 Claim Window 的语义；solver 在 search worker 真实空闲时创建并绑定 assignment。assignment/Action 原子落库后立即成为到 `obligation_deadline_at` 的数据库持久工作，不存在 Window、预扣或第二次 Scope 容量 CAS。仅 lifecycle/deadline/资格/binding/dedupe 失效才唯一取消；`configured_handoff_grace/claim_handoff_deadline` 只读保留存量审计。

> **2026-07-30 代理 failover 原子性补充：** 搜索建连失败触发机场节点切换时，候选节点的协议、host、port 和可执行 `AccountProxy` 必须在修改旧 `AccountProxyBinding`、`AccountEnvironmentBinding` 之前完成验证。候选不可执行时整次切换失败，旧 binding 保持 active 且环境绑定不变；禁止先把旧 binding 置 inactive 后再抛错。Planner 的 search eligibility 同时必须验证环境引用的 proxy binding 仍为 active、未解绑且租户/账号/app/authorization/role/proxy 全部匹配；已不一致路径不得继续生成 Action。

#### 5.2-A 当前直接执行合同

> **2026-08-04 assignment 直接执行：** 纯搜索 Action 只使用 assignment 固化的账号/授权/代理/关键词路径，不回落普通互动 claim、不绑定 Reservation。落库后按真实 search slot 直接执行；lifecycle/deadline/资格/binding/dedupe 在 Gateway 前失效时，以 assignment 单行 CAS 终结并让原义务保持 open，事实变化后 solver立即重新求解，不等待 Window 或 release batch。

> **historical_do_not_implement（2026-07-30 搜索 finalize 锁链）：** `SearchClickAssignmentEpoch/Window/TaskAllocation/ShardAllocation/Reservation` 全链已删除。当前只在 assignment 创建和 Gateway 前使用数据库 `clock_timestamp()` 比较 `obligation_deadline_at`，并以单行 version CAS 推进；不得实现旧 finalize 锁序。

> **2026-08-03 搜索验证码双 OCR 最终 supersede：** 2026-07-31 的三路模型投票仅保留历史取证，不得进入当前实现。现行搜索验证码固定 RapidOCR → ddddOCR 顺序执行：A 无效时运行同 fingerprint 的 B，A rejected 后仍为同 fingerprint 才允许 B；callback 拒绝产生新 fingerprint 时从 A 重开；B 无安全答案或拒绝后仍为旧 fingerprint 仅允许协议样本审批的 refresh callback，无动作写 refresh_not_supported；不得调用或等待任何 AI/VLM。callback unknown 只复探不重点，只有远端通过和最终 `target_click_observed` 才能结算。

> **2026-08-04 搜索账号并发 supersede：** 同一账号可在多任务/多 assignment 中并发发起非冲突 RPC，不强制 `account_session_inflight=1`、不做账号抢占或任务 cursor。solver 可以远程安全事实做稳定路径决胜；同一 `SearchClickObligation` UUID、request identity/远程副作用 key 仍幂等唯一。
>
> **historical_do_not_implement（旧 release/rebuild/shard）：** `release batch/item/unit exclusion/rebuild wave/DispatchClaimShardAllocation` 只保留事故取证；当前 assignment 失效只单行 CAS，原义务仍 open，事实变化或 search slot 空闲立即唤醒 solver。

- 搜索任务结构合法即直接创建成功；启动时建立当前 `task_day_ledger_id` 和稳定 click 义务 UUID。运行只展示 confirmed/open/executing/unknown/shortfall 和 typed blocker，不计算 planning deficit、hard safe attempt capacity、catch-up 或预计完成量。
- 当前未结束旧 search/search_join Task 不接管、不转换、不迁移。运营直接创建 prepared 单目标新 Task，route epoch切换后新 Task从 0执行；旧 Task失去 Gateway 权限并按精确 operation 写最小 tombstone后物理删除。
- **historical_do_not_implement（旧共享 Dispatcher）：** 父任务份额、Claim Window、TaskAllocation、Reservation 和 `shared_dispatch_capacity_insufficient` 均不得实现；当前 search/interaction lane 独立，按各自真实空闲槽直接领取。
- 点击目标未完成时，solver 从当前 eligible snapshot 持久化随机顺序，按该顺序为开放义务选择合法账号/授权/代理/协议路径；明确 pre-Gateway 失败终结 assignment并保持原义务 open，unknown 保持原 identity，失败和尝试数都不减少目标。不存在曲线、`actions_per_round`、skip、jitter、静默或 catch-up。
- 运营不配置账号容量或账号优先级。多个 Search Task 每轮各尝试领取一条开放义务，再填满真实空闲槽；click 确认后不得进入 membership/admission 状态机。
- 每个义务 UUID 具有独立 assignment 和独立 Action；Action 首次持久化前写入 obligation/assignment/request identity。唯一键冲突只回读已有记录，不整批回滚其他义务，不依赖中央 Reservation、lane ordinal、epoch 或权重重建。
- Planner 只处理主记录存在且 `running` 的新合同 Task；暂停、停止、删除或旧 route Task不产生新义务/assignment。当前合同不使用 `max_pending_global|max_pending_per_task|oldest_pending_age_seconds`、中央 Reservation 或 Dispatcher scope控制瞬时负载，只按各阶段实际空闲槽 JIT 物化。
- `search_rank_deboost` 继续接收生命周期 `target_count`：它是任务生命周期的已确认成功数，达到目标后写 `completed + target_count_reached`；其 `max_actions_per_day` 仍是自然日 action 预算。未结束的 `search_join_group` 不再保留运行语义，统一按上一条接管为纯 click；completed/deleted 仅作为历史记录读取。普通搜索只能选择启用的普通账号组；搜索排名观察只能选择启用的 `pool_purpose=rank_deboost` 黑账号组。代理、机器人、授权环境、单账号安全上限、停留、重试和风险参数继续由系统托管，调用方传入这些系统字段必须显式拒绝。
- `scheduled_end` 是真实停止边界：到期后任务停止规划和派发；尚未进入 Gateway 的 assignment/Action 以单行 CAS 写 `scheduled_end_reached`，Gateway 已开始的结果保留真实状态。任务变为非 `running` 时 Gateway 前必须写 `task_not_active` 并停止；当前纯搜索不读取 quiet-hours、日/小时抖动、Window 或 reservation。

- 只处理 `task_type=search_click + search_execution_mode=click_only` 的纯搜索点击任务，按账号授权槽位、搜索机器人、关键词、目标群匹配策略生成 source；现存物理 `action_type=search_join` 仅作内部兼容别名，业务/API/日志必须投影为 `search_click`。只有同一 Attempt 具备完整批准点击证据、`membership_side_effect=none` 且 `membership_mutating_rpc_invoked=false` 后才写 `target_click_observed`。inline keyboard 目标必须为 `target_open_only`；正文 `MessageEntityTextUrl` 目标必须同时保存消息 ID、实体序号、实体 URL 指纹、远端 `channels.GetFullChannelRequest`、精确 entity id/username 与 `target_open_only` effect，正文可见标题或 `get_entity` 本地缓存命中不能单独完成。键盘目标证据要求非负 row/col；正文实体目标没有伪造 row/col，改由实体序号和远端实体字段完成事实门。只有 `navigate_only` 不具备目标点击资格。旧 `join_candidate` 或成员副作用未知的协议样本默认不得进入 pure-click eligibility；仅历史解析器精确版本 `jisou-v2-2026-07-28` 可在发布接管中保留原行为样本为 inactive，并以审计化 replacement 把 Telegram 内部 URL 从误标的 `join_candidate` 重分类为 `target_open_only`、声明无成员副作用。其他版本、结构或 effect 不得自动升级。确认后 ordinal 结束，禁止创建 `search_join_membership` 或调用任何 join/request/follow/confirm/can-send 路径。
- 创建 action 前必须实时校验真实协议样本、`execution_mode=mtproto_userbot`、授权槽位环境栈、授权槽位代理绑定、代理健康、observed exit IP、客户端元数据镜像绑定、关键词允许矩阵和 `(account_id, proxy_binding_id)` warmup 阶段。
- 马来西亚灾备账号的 `primary / standby_1` 按 v2.21 合同共用唯一硅谷业务出口，`standby_2` 固定到唯一 MY 出口且永不进入普通 Task source；三槽必须使用现有 App A/B/C、三个不同 AuthKey 和非零远端 authorization hash。MY client 休眠不改变 standby_2 在 Telegram 设备集中的 active 授权状态。同一账号可在多个 Task/assignment 并发执行不同 remote mutation identity，响应必须按 rpc/request identity 和冻结 authorization/fact/connection generation 回写各自 Attempt；切换时 Gateway 前 assignment 释放重排，Gateway 已开始的旧 Attempt 不改绑、不自动重发。当前业务授权出口漂移、同槽位多 active 绑定、三槽 App/AuthKey/hash 复用或指纹复用时对应路径不可执行；不建立账号级 claiming/executing 锁。
- 授权环境绑定的权威粒度是 `account_id + developer_app_id/api_id + authorization_id/session_role`。同一账号在不同 TG 开发者应用 `api_id/api_hash`、不同 session key 和主 / 备用授权槽位下可以绑定不同客户端元数据和不同代理节点；Planner 创建 action 前和 Executor 派发前都必须验证 payload 授权槽位属于 action 的 `account_id` 且 role 一致。Executor 必须使用授权槽位登录时绑定的同一 TG 开发者应用、指纹配置和代理配置，不能用另一个应用的指纹或代理配置代替，也不能回退本机直连。
- `airport_clash` 节点必须来自已成功拉取、完成 Base64 URI 列表 / Clash YAML / JSON 解析、过滤伪节点、通过健康检查并完成真实出口 IP 观测的启用订阅；随机分配后固定到授权槽位，不按 action 轮换；每个节点绑定授权槽位数不得超过默认容量或单节点覆盖；当前节点不通时优先按 `switch_to_next_healthy_node` 在同订阅内切换，同订阅无健康节点时按主备优先级切到备用订阅健康节点并重置 warmup。全部启用订阅不可用时，该运行 scope 写 `runtime_state=waiting/airport_all_subscriptions_unavailable`，Task 保持 running，禁止创建真实 Telegram 操作；恢复后自动继续。
- 全部启用订阅不可用时必须复用租户 Telegram Bot 通知链路，向 `Tenant.admin_chat_id` 配置的全部管理员 Chat ID 推送脱敏告警；通知失败只记录 `admin_notification_failed` 和审计，不允许因此继续执行或回退直连。
- 小时执行量只作只读观测：按任务时区统计 confirmed、open、executing、unknown 与 remaining，不计算 planning deficit、required rate、软节奏、预计容量或 catch-up，也不反向控制领取。
- 搜索目标群点击任务先执行账号、关键词、授权槽位、代理、协议/CAPTCHA、Gateway、任务状态、deadline 和 unknown 防重等真实安全校验；存量 `max_actions_per_day/max_actions_per_hour`、hourly/daily/action skip、jitter、quiet-hours 一律只读，不参与候选、claim 或 Gateway。
- `search_click` 不存在实时 pacing / random decision。持久随机只用于多个同时合法候选的稳定打散，不形成跳过、延后或目标扣减；LLM 仅用于离线配置建议、关键词生成、目标相关性解释和复盘分析，不得直接决定账号是否搜索、点击、跳过或重排；存量 `search_join_group` 只保留 legacy 识别。
- 搜索点击小时 stats 必须使用独立字段 `search_join_hourly_*` 或 `search_join_stats.hourly_execution`，不得复用 AI 活跃群的 `hard_hourly_*` 发言语义；skipped / failed / 代理全不可用 / decoy-only 浏览不得计入 click 成功或 future 覆盖。
- decoy 关键词比例或 warmup 事实只在 Task 启动后评估，不得阻止结构合法任务创建。比例不满足或仍在 warmup 时，该账号路径暂不进入主目标 assignment并写运行 blocker/next eligible time；主目标义务保持欠额、不得产生终态 skip，其他安全路径继续，事实恢复后自动重算。
- Planner 不调用 Telegram API；目标机器人搜索、翻页、精确匹配、批准点击和必要的点击后结果确认只由 Dispatcher / Executor 执行。纯搜索点击不得执行加入、关注、确认、can-send 复检或入群后策略。
- 用稳定 `action_dedupe_key` 防止同账号同日对同机器人、同关键词、同目标群重复规划超过阈值。
- action payload / result / stats / worker 日志不得保存关键词明文，统一使用 `keyword_hash` 和必要的加密展示字段。
- search_join 的强度解释独立于 AI 活跃群小时轮数和频道动作预算，默认受单账号每日、单授权槽位每日、单关键词每日、单 IP 每日和跨账号同关键词并发限制共同约束。

> **historical_do_not_implement（旧共享 Dispatcher）—2026-07-28 全任务履约 Claim Window：** Dispatcher 先读取真实 `DispatchClaimScope` 并 reconcile 跨 Window active ledger。AI、评论、点赞、浏览、搜索和 ordinary 按 `allocation_business_task_id=coalesce(admission_execution_sponsor_task_id,parent_task_id,task_id)` 聚合，跨全部 shard 按 scope cursor 获得每父业务任务最多 1 个最低机会，剩余容量按未满足 `required_claims` 使用最大余数法写 `DispatchClaimTaskAllocation`。父任务内 fulfillment/admission 同时可领取时，获配 `>=2` 至少各 1，获配 1 按持久 lane cursor 轮转；admission retry 只在 admission lane 内优先。随后由 `DispatchLaneShardSolver` 做单次精确 task-lane-to-shard 三层匹配并映射到 shard Reservation，同一父任务及其 child 不得按 shard 重复最低份额；共享 admission 使用唯一 sponsor lease。无法满足总需求或 shard 映射时必须写需求、获配、cursor、下一 Window 和明确原因。`DispatchClaimPlan.candidate_action_ids` 是 allocation 后的领取顺序；`SKIP LOCKED` 只能跳过不可领取项，禁止按静态 backlog 年龄重排或用 Action/worker 状态替代远端业务成功。

### 5.3 Dispatcher 执行要求

领取分三段：

```text
DB 短事务 claim
  -> status = claiming
  -> 写 claim_owner / claim_token / claim_expires_at

事务外拿运行资源
  -> Redis token bucket
  -> account in-flight lock
  -> proxy / target / media quota

DB 短事务确认执行
  -> status = executing
  -> 写 lease_owner / lease_expires_at
  -> 创建 execution_attempt
  -> 调用 Telegram Gateway
  -> 回写 success / failed / skipped / unknown_after_send
```

要求：

- Telegram API 调用期间不能持有数据库事务。
- AI活群与频道评论的Provider生成、fallback、重描述和质量判断期间不能持有DB事务。独立AI Generation role先短事务claim GenerationJob并提交，在事务外调用Provider，再按job/lease/generation epoch短事务持久variation/rejection；normal accepted variation+memory后原子创建ready Action。Dispatcher只claim ready Action并进入Telegram TxA/B，不调用Provider或补写正文。
- normal内容主3轮+备用3轮仍无候选时，Generation不创建签到Action：仅coverage已完成的direct extra-volume同tx写唯一handoff并转`check_in_ready`，Planner消费handoff创建ready签到Action；未完成coverage、reply/material或其他不合资格分支转typed`content_capacity_gap`，scoped owner占用/deadline分别为hold/shortfall。评论表情沿评论专项。旧primary slot/Cycle/ContentMix不是current owner；Provider persist unknown按同job reconcile，Telegram call-issued后未知才进同request hold，任何分支不得新建义务或自动重发。
- 同一账号可有多个不同任务/不同非冲突 `executing` Action；同一 `remote_mutation_key`、click ordinal、callback action key 或 Gateway request identity 仍只能有一个 executing owner。
- 同账号并行 RPC 必须以 `rpc_id + authorization_id + task_id + action_id + remote_mutation_key` 隔离 transport 上下文和结果。adapter 无法证明单 client 并发安全时使用独立 channel/client instance，禁止退回账号全局串行；一个请求的 timeout/cancel/reconnect 不得串写或重发另一个 Action。
- Dispatcher 单轮预领取量不得大于该 worker 的实际执行并发，避免批次尾部在资源确认前超过 `claim_expires_at`；worker 命令的 drain limit 大于实际并发时，只表示后续轮次继续处理，不得一次占住全部 action。
- 每个 stable AI obligation 对应独立 `GenerationJob`。direct job可按稳定sequence并发调用Provider并进入有界ready buffer，不共用整批串行generation claim token；job以owner/lease epoch/job version幂等持久variation/rejection，只有合法ready结果才创建并冻结Action，Action不得先于job成为generation owner。direct不因其他结果先推进context就整批作废，reply/强上下文仍严格CAS。同账号已有其他非冲突executing Action不是生成候选排除条件；数据库唯一约束按remote mutation/obligation/job粒度，不再使用`uq_actions_executing_account`。
- Generation worker 读取目标群最近真人上下文时，必须按 `tenant_id + group_id + is_bot=false + content<>''` 过滤，并按 `coalesce(sent_at,created_at) DESC,id DESC` 命中完全一致的 partial expression index 后有界取数。只命中旧 `sent_at` 索引再排序、生产并行 `Seq Scan/Sort/Gather`、通过降低并发或扩 `/dev/shm` 掩盖缺索引均不符合验收；三个 worker 并发下必须持续无 PostgreSQL shared-memory 扩容错误并产生远端消息事实。
- `fact_first_v3` AI在Generation前必须按当前`task_id + account_id + target_group_id`读取/推进`TaskGroupBotAdmission`；旧`GroupBotAdmission`和Action内旧state不能作为current门禁。pending admission只让同一stable obligation/FOP进入typed waiting并由admission revision唤醒，不创建空正文Action、legacy reservation或读取Provider凭据；ready后GenerationJob冻结Task admission id/version。缺本地Authorization不能替代Session权威判定。旧`current_authorization_missing`/CycleSlot/ContentMix行只在takeover manifest中分类alias、释放可证pre-Gateway owner或保留Gateway hold，绝不得按旧槽重建current Action、原地retry或占满新批次。
- `fact_first_v3` 正式发送链路覆盖上述存量例外：stable obligation 的 monotonic identity ordinal + active due rank 是数量 owner，allocation/assignment 与 immutable content intent 是内容 owner；GenerationJob/variation ready 后才创建唯一 fenced Action。旧 CycleSlot、primary quantity slot、legacy Planner、`coverage obligation -> Action` 与 Action dedupe quantity identity 只可在 takeover manifest 中作为历史 alias 分类；可证 pre-Gateway owner按新合同释放或重物化，Gateway hold/fact保持原身份，禁止原地 retry或用旧行阻断current batch。ContentMix仅为异步兼容投影。
- interaction、search、Generation、OCR 各自按真实空闲槽独立领取，不做跨任务容量仲裁、Window、TaskAllocation、Reservation 或预扣。每轮先从每个 running Task 至多取一条，再按 `opened_at,task_id,obligation_id` 填满槽位；该顺序只是无状态候选顺序，不生成持久配额。所有 Action 仍必须通过原账号、授权槽位、权限、真实 Telegram 限制和 Gateway 防重校验。
- 热查询先用当前任务日、状态和 due 条件的 partial index 做 keyset 读取，批次固定为 `min(stage_free_slots, stage_claim_batch_limit)`；取得候选 ID 后，每行分别执行 `UPDATE ... WHERE id=? AND state=? AND version=? RETURNING`。禁止 `FOR UPDATE`、`SKIP LOCKED`、OFFSET、跨历史扫描、JSON 排序和跨表显式锁；CAS 失败只从同一有界候选游标继续，不恢复旧 Window/Reservation 顺序。
- 进入 Gateway 调用边界后结果未知，必须标记 `unknown_after_send`，不能自动重发。
- FloodWait、SlowMode、账号受限、代理异常、目标权限不足和内容拦截必须分类。
- Dispatcher 不负责选择引用对象，也不负责把普通消息升级为引用回复。它只读取 action payload 中的 `reply_to_message_id`，并把该值传给 Telegram Gateway 的原生 `reply_to` / 评论回复参数。
- 如果 action payload 标记为引用回复但缺少 `reply_to_message_id`，Dispatcher 必须按配置错误失败并暴露原因，不能静默普通发送。
- 频道评论达到生命周期总上限后保持 `completed/next_run_at=null`；Recovery 不得因 pending 生成或旧错误恢复而重新启动任务。

### 5.4 Listener 要求

- 对同一来源进行 source claim，避免多个 listener 重复拉取。
- 持久化水位。
- 允许短窗口回补。
- 按唯一键去重。
- 默认过滤 bot 消息。
- 相册和 media group 需要聚合。
- 编辑 / 删除事件需要记录版本或状态。
- 群上下文采集必须保留可用于 Telegram 原生回复的远端消息 ID、发送人、发送人类型、内容预览、消息类型和发送时间；没有远端消息 ID 的上下文只能作为普通上下文，不能进入引用池。
- 频道评论采集必须保留 `comment_message_id`、父评论 ID、作者、内容预览、是否 bot、发布时间和所属频道消息；没有评论 ID 的记录不能进入引用池。
- Listener 不判断某条消息是否“适合回复”，只提供可追溯事实；引用对象选择属于 Planner。

### 5.5 Recovery 要求

- `claiming` 超时恢复为 `pending`。
- 未进入 Gateway 的 `executing` 可按策略恢复或失败。
- 已进入 Gateway 的超时必须进入 `unknown_after_send`。
- worker heartbeat 失联时恢复其持有 action。
- 记录恢复原因并暴露在任务详情和运营数据中。
- `unknown_after_send` membership 只允许按 drain limit 和账号+目标去重后有界补偿复检；Telegram 探测超时必须写入 `telegram_probe_timeout`，连接失败必须写入 `telegram_probe_connection_error`，两者都要记录复检时间和下一次冷却时间，不能抛出打断整轮 recovery，也不能高频重试。Recovery 取补偿复检 batch 时必须在查询层排除已 `failed` 和冷却未到期的行，避免旧结果占满 batch 导致真正待处理行饥饿；Telegram probe 返回 `ok=False` 时必须把失败原因显式写入 `unknown_membership_reprobe_status=failed`，且释放失败 probe 的 Telethon client，避免缓存 client 在后台持续重连；stale `executing` 且已进入 gateway 的 membership probe 如果得到 failed result，必须退出 `executing`、清空 lease 并保留 failed result，不能再被通用 stale recovery 覆盖回普通 `unknown_after_send`。
- Worker 心跳 ID 带角色后缀时，Recovery 必须使用心跳记录中的 `hostname + pid` 匹配 Action 的 lease owner；发布替换旧容器后，没有进入 Gateway 的执行项应按 `stale_worker` 立即回收，不能误等完整租约。已进入 Gateway 的执行项仍按结果未知防重复口径处理。
- Recovery 必须把 Action、Task 状态和过期 lease 分别按对象单行 CAS 收敛；禁止把 dirty Task、Action、义务和统计放入同一跨表事务。竞争只回读权威 remote fact 与最新 version，不建立反向锁序。
- 当前任务内动态账号覆盖不使用 `TaskDailyCoveragePlanCursor` 串行点。每个 `(task_id,target_group_id,account_id,task_day_ledger_id)` scope head 以唯一键和单行 version CAS推进；多个账号、多个 Task可并发，Task/coverage 统计由事实 projector 异步汇总。

### 5.6 Metrics 要求

首期快照指标：

- `actions.pending.count`
- `actions.claiming.count`
- `actions.executing.count`
- `actions.oldest_pending_age_seconds`
- `actions.claimed_per_minute`
- `actions.success_per_minute`
- `actions.failed_per_minute`
- worker heartbeat。
- 账号/代理错误。
- FloodWait / SlowMode 次数。
- 引用回复规划和执行指标：计划引用回复数、成功引用回复数、引用对象不足次数、引用 payload 配置错误次数、Telegram 引用回复失败次数。

### 5.7 任务执行全链路流程

任务执行流程必须按阶段拆开，每个阶段都有明确输入、写入和失败去向。

| 阶段 | 触发 | 读取 | 写入 | 失败去向 |
| --- | --- | --- | --- | --- |
| 可选诊断（非创建前置） | 已创建任务详情或独立诊断入口 | 账号、目标、规则、风控、账号-目标关系 | 不写业务状态；可写诊断日志 | 返回运行风险与建议，不签发创建凭据、不产生 `allow/warn/block` 创建门禁 |
| 创建任务 | 保存草稿、创建并启动 | 目标输入、账号范围引用、规则版本、节奏配置及结构合同 | `operation_targets` upsert、`tasks`、审计 | 只有请求本身结构非法才返回表单错误；不生成 action |
| 启动任务 | 创建并启动、手动启动、继续 | `tasks`、任务配置、目标能力 | `tasks.status=running`、`next_run_at`、启动审计 | 状态不允许时返回按钮级错误 |
| Planner 扫描 | `next_run_at <= now` | running tasks、规则版本、账号池、上下文、任务汇总 | 类型化业务owner、`plan_batch_key`、`tasks.next_run_at`、任务stats；current AI输出stable obligation/FOP+assignment+intent并转`generation_pending`，normal Action=0 | 规划失败写任务last_error，不调用TG/Provider |
| 准入规划 | Planner 内部阶段 | 账号-目标关系、目标入口、账号能力 | `ensure_target_membership` 类 action、准入摘要 | 无可准备账号时写 blocked 原因 |
| 主动作规划 | Planner 内部阶段 | 已满足准入账号、任务类型参数、typed DueSet/欠额、账号覆盖与当前阶段真实空闲槽；频道浏览额外读取source target与daily identity | 非AI类型可产出自身ready/pending Action；current AI只物化stable obligation/FOP、aggregate assignment与immutable intent，normal Action由Generation在accepted variation+memory后创建，签到Action仅由Planner两条typed分支创建 | 幂等去重后不足量写typed blocker；AI/浏览只物化当前due，future按专项不可提前领取；资源或deadline不足显式暴露，不创建中央份额/Window/预扣 |
| 引用回复规划 | Planner内部阶段 | 引用数量配置、typed bound remote message source、频道讨论区评论、任务画像和规则 | current AI写RequirementAssignment+immutable intent，Generation ready Action再复制`reply_to_message_id`；评论按其专项可直接写Action蓝图 | 引用对象/候选不足写typed shortfall，relation不降级，不以成功Action代替bound fact |
| Dispatcher claim | ready/pending action到期（类型专项） | actions、running tasks、账号shard；current AI只允许ready正文/签到Action | `claiming`、claim owner、claim token、claim过期时间 | 资源不足恢复原可执行态并写defer reason；不得claim AI generation_pending义务或调用Provider |
| 获取运行资源 | claim 后事务外 | Redis token bucket、远程副作用 key lease、代理、目标能力 | Redis token、mutation-key lease | 限流或同 mutation 冲突时释放 claim，action 延后；不同非冲突 Action 不互斥 |
| 确认执行 | 资源就绪 | claiming action | `executing`、lease owner、`execution_attempts` | 状态竞争失败则释放资源 |
| Gateway 调用 | action executing | action payload、账号 session、目标 peer、素材 | 不持有 DB 事务 | 调用边界后本地超时进入 `unknown_after_send` |
| 结果回写 | Gateway 返回或超时 | action、attempt、gateway result | action status/result、attempt result、任务 stats、账号状态 | 明确失败写 failure_type；未知写 unknown_after_send |
| 汇总读模型更新 | Metrics / Recovery 周期或结果事件 | actions、attempts、tasks、账号、目标、规则命中 | `target_runtime_summary`、`task_runtime_summary`、`account_runtime_summary`、`operation_issue`、`daily_runtime_stats` | 汇总失败不得回滚执行事实，记录 metrics 错误 |
| 页面展示 | 用户打开运营中心 / 任务中心 | 汇总读模型；详情按 ID 查明细 | 不改变业务状态 | 查询失败展示最近更新时间和重试 |

页面展示阶段必须额外计算任务派生运行阶段。主状态只回答“调度是否允许继续”，派生阶段回答“为什么现在没有发 / 正在等什么 / 谁需要处理”。任务列表、任务详情顶部、运营中心关联任务摘要必须共用同一套派生阶段口径。

### 5.8 执行状态流转细节

Task 状态：

| 状态 | 进入条件 | 允许操作 | 退出条件 |
| --- | --- | --- | --- |
| `draft` | 保存草稿 | 编辑、启动、删除 | 启动后 `running`，删除后主行物理不存在 |
| `running` | 创建并启动、启动、继续 | 暂停、停止、详情、编辑受限 | 暂停到 `paused`，停止到 `stopped`，严重失败到 `failed` |
| `paused` | 人工暂停 | 继续、编辑、停止、删除 | 继续后 `running` |
| `stopped` | 人工停止或到达结束时间 | 启动、详情、删除 | 启动后重新规划 |
| `failed` | 任务级不可恢复错误 | 重试、编辑、停止、删除 | 重试后按失败策略恢复 |
| `completed` | 到达任务目标或结束条件 | 详情、重置、删除 | 重置后重新规划 |
| `deleted` | 仅存量迁移过渡态；正式删除后 Task 主行物理不存在 | 独立 tombstone/archive 只读审计 | 不恢复；需运行则新建 Task |

Task 主状态与页面派生阶段不能互相替代：

- `paused`、`stopped` 和 `failed` 是硬停止类状态，必须在任务列表和详情中使用高强调提示；其中 `paused` 还要说明“不会继续生成或执行新动作”。
- `running` 只表示任务允许被 Planner / Dispatcher 处理，不表示一定正在发送消息；页面必须继续展示等待准入、等待 AI、等待上下文、等待冷却、等待下一轮或发送中。
- `last_error` 只能作为最近事实展示，不能在目标能力、账号能力或 AI 健康恢复后继续作为当前诊断。Recovery / Metrics 重建汇总时必须清理或降级已恢复的旧错误。
- 手动继续暂停任务时必须重新设置可解释的 `next_run_at`，并在审计中记录操作者、来源页面和继续前后的派生阶段。

Action 状态：

| 状态 | 进入条件 | 页面含义 | 处理规则 |
| --- | --- | --- | --- |
| `pending` | Planner 生成或 claim 恢复 | 等待执行 | 可被 Dispatcher claim |
| `claiming` | Dispatcher 短事务预领取 | 正在抢占资源 | 超时由 Recovery 恢复 pending |
| `executing` | 资源就绪并创建 attempt | 正在执行 TG 动作 | lease 超时后按是否进入 Gateway 决定 unknown 或 failed |
| `success` | Gateway 明确成功 | 执行成功 | 写汇总，计入成功 |
| `failed` | Gateway 或前置校验明确失败 | 执行失败 | 写 failure_type，上卷运营异常 |
| `skipped` | 规则、质量、风控或容量决定跳过 | 主动跳过 | 需要记录 skip_reason |
| `unknown_after_send` | 已进入 Gateway 边界但本地结果未知 | 可能已发送 | 不自动重发，等待人工确认或有界补偿查询 |

`search_rank_deboost` 任务专属 skip_reason（在 `skipped` 状态下记录）：

| skip_reason | 触发条件 | 处理规则 |
| --- | --- | --- |
| `join_button_detected` | 竞争群结果项含 `join_candidate` 按钮但 Executor 只点开导航按钮、未点加入按钮 | 正常完成；stats 写 `join_button_detected=true`、`joined=false`；不暂停账号 |
| `no_navigable_button` | 竞争群结果项只含 `external_http_url` 或 `unknown` 类型按钮，无法点击 | action skipped；stats 记录失败原因；不切换节点 |
| `target_not_in_results` | 我方目标群未出现在当前搜索结果中 | action skipped；不点击任何竞争群；不影响其他 action |
| `group_ip_daily_limit_reached` | 分组共享出口 IP 当日累计点击已达上限 | 该分组所有账号后续 action skipped；风控中心告警「分组共享 IP 触顶，建议切换节点或降低节奏」 |
| `per_account_daily_limit_reached` | 单账号当日点击竞争群数已达上限 | 后续 action skipped 直到次日租户时区 0 点；不创建新 action |
| `exempt_group_missing` | Task 启动后随机豁免群未预选（`search_rank_deboost_exempt_groups` 无记录） | Task 保持 running；未进 Gateway 的该 scope Action 等待并告警，补齐后自动继续，不回滚创建 |
| `protocol_sample_insufficient` | Task 启动后发现 `bot_protocol_samples` 中 `sample_purpose=rank_deboost`、`bot_code=jisou` 样本数未达阈值 | Task 保持 running；前端展示样本采集进度与缺口，样本达标后自动继续，不回滚创建 |

### 5.9 失败上卷规则

任务中心记录失败事实，运营中心只展示需要运营判断或影响目标效果的异常。

| 失败来源 | 失败事实写入 | 上卷条件 | 运营中心展示 |
| --- | --- | --- | --- |
| 目标权限 | action failed / precheck blocked | 目标不可发送、不可评论、不可监听 | 目标权限异常，建议同步目标或处理权限 |
| 账号状态 | action failed / account summary | 掉线、受限、FloodWait 过多、代理不可用 | 账号异常，建议重新登录、换代理或暂停账号 |
| 准入前置 | membership action failed / blocked | 关注、加入、入群验证失败影响容量 | 准入异常，展示影响账号和预计缺口 |
| TG 限制 | execution_attempt failed | SlowMode、FloodWait、PeerInvalid、COMMENT_UNAVAILABLE | TG 限制异常，展示失败码和建议动作 |
| 规则 / 风控 | action skipped | 同一目标持续拦截或影响任务目标量 | 规则或风控异常，建议查看规则 / 风控 |
| AI 质量 | action skipped / AI generation skipped | 无锚点、重复、幻觉风险导致连续跳过 | AI 质量异常，建议调整方案或话题 |
| AI 运行不可用 | Planner / generation failed | 供应商读超时、空响应、无健康供应商影响计划生成 | AI 供应商异常，展示供应商、错误类型和下次重试 |
| Listener | listener source error / no event | 水位停滞、采集失败、源群无事件 | 监听异常，建议检查监听账号和源目标 |
| 容量不足 | Planner 写 capacity_shortfall | 已满足 + 可准备账号不足以完成目标量 | 容量异常，建议增账号或调低目标 |

同一目标同一类失败在一个汇总窗口内合并为一条 `operation_issue`。不同 `failure_type` 可以在同一 issue 内保留代表性失败码和代表性 action，避免运营中心刷屏。

权限和验证失败必须拆分展示，不能用单一“验证码未通过”覆盖所有原因：

- `target_send_permission_blocked`：目标或群默认能力阻断，影响多个账号时上卷为目标能力异常。
- `account_target_permission_revoked`：单账号对单目标不可发言，例如 Telegram 返回单账号禁言 / `ChannelParticipantBanned`。
- `not_participant`：账号不在群 / 未关注，需要准入补齐。
- `comment_membership_required`：频道评论运行时发现账号未关注 / 未加入目标频道或讨论区，必须补齐 `ensure_target_membership` 后再继续评论，当前评论 action 展示为等待准入，不计入终态失败。
- `comment_account_permission_denied`：账号已满足准入但 Telegram 仍拒绝评论，只影响该账号在当前频道的后续评论。
- `comment_unavailable_message`：频道消息本身无法评论，例如评论入口不可解析、频道未绑定讨论组、消息不是频道帖子或评论已关闭；同帖后续评论应跳过并显示“该消息无法评论”。
- `membership_partial` / `membership_running`：准入仍在进行，主任务可用已满足账号先运行。
- `ai_generation_unavailable`：AI 供应商超时、空响应或无健康供应商。
- `stale_diagnosis`：旧错误与当前目标 / 账号能力不一致，需要 Recovery 重查并清理展示。

---

## 6. 核心数据流

### 6.0 数据流转总则

系统数据流转按“事实写入 -> 汇总读模型 -> 页面展示 -> 明细下钻”组织：

```text
用户操作 / Worker 执行 / Listener 采集
  -> 写热事实表
      tasks / actions / execution_attempts / listener_source_state / context messages
  -> Metrics / Recovery 增量汇总
      target_runtime_summary / task_runtime_summary / account_runtime_summary / operation_issue / daily_runtime_stats
  -> 页面默认读汇总
      运营中心 / 运营数据 / 任务列表
  -> 用户点开后按 ID 下钻
      任务详情 / action 明细 / attempt 明细 / 归档检索
```

数据流转硬规则：

- 写事实优先：执行结果、失败码、原始错误和审计不能只存在于汇总表。
- 读汇总优先：运营中心、运营数据和任务列表不能用明细全表聚合支撑页面展示。
- 下钻按 ID：任务详情、目标异常详情和 action attempt 明细必须有明确 `task_id`、`target_id`、`action_id` 或时间范围。
- 汇总可重算：汇总读模型允许延迟和重算，不能作为唯一事实源。
- 清理先汇总：热事实超过保留期前必须先写入日统计或归档，再清理明细。

### 6.1 账号到目标

```text
tg_accounts 在线
  -> sync groups / contacts / targets
  -> tg_groups / tg_contacts
  -> operation_targets
  -> target detail 聚合关联账号能力
  -> 任务中心 / 消息发送消费 operation_targets
```

### 6.1.1 目标准备

```text
operation_targets
  -> 账号同步、任务创建 target_input、运营目标页管理修订都可以写入
  -> 按 tg_peer_id / username / invite_hash 去重
  -> 历史公开链接解析出稳定 peer 时，如存在同 username 的重复稳定目标/群，只能由 fresh Session 在 `SERIALIZABLE` 事务和同租户行锁内确认重复对象无业务外键、非外键群运行状态、无 Task 配置引用、无 `pending` / `claiming` / `executing` / `retryable_failed` / `unknown_after_send` / `waiting_cache` 运行中 Action 配置引用，且无 `failed` Action 配置引用（为避免失败策略重新排程而保守阻断），并且仅有同租户可迁移账号-群关系时合并；`success` / `skipped` 终态 Action 历史保留但不构成运行中引用。保留被任务引用的既有目标/群 ID 及其群策略，迁移账号关系、删除无引用重复对象并写审计，历史审计事实保留旧 ID；因无外键历史状态无法由数据库自动阻止任意新写入，生产合并必须在相关 writer 已静默、复核运行中状态和配置引用为空的操作窗口执行；任一条件、并发冲突或事务失败不满足时必须回滚并显式保留冲突
  -> 观察账号必须用公开 username 的单目标 `resolve_group_by_public_username` 实测 stable peer 和当前发言权限，禁止为规范化调用全量 `list_groups`；该外部读取必须在 fresh Session 的只读预检阶段完成并结束该事务，只有已解析快照可进入后续 `SERIALIZABLE` 写事务
  -> 写入 source_type、last_synced_account_id、审计和目标能力快照
  -> 检查账号-目标关系
      已满足：记录为可直接使用
      可准备：生成关注频道 / 加入群聊前置动作
      不可准备：记录阻塞原因
  -> 前置动作执行成功
  -> 写入 tg_group_accounts 或目标账号关系
  -> Task 启动后的运行投影复用这批关系；任务创建不依赖
```

准入准备以 Task 已创建后的统一 start / Planner 为主触发。账号同步可以沉淀候选目标和历史账号-目标关系，但不代表目标已可运营。运营目标页允许同步、修订能力、处理准入失败和重新准入，不提供“先新建目标再回任务”的强制准备流程。Task 进入 running 后必须实时检查，不能只相信前端摘要；检查结果只形成账号级 waiting/blocker，不回滚创建或启动。

准入候选覆盖范围必须和主互动发送容量分离：

- 准入候选来自任务账号配置选中的全部在线账号，例如全部可用账号、指定账号分组或手动选择账号；只有账号本体离线、不可用、无 session、被明确排除或账号级安全阻塞时才剔除。
- `max_concurrent`、每轮发言数、账号冷却、健康权重和发送容量只影响主互动规划与发送节奏，不得截断关注 / 入群 / 可发言能力准备。
- 对转发目标群，准入完成条件是已加入且 Telegram 可发言；对 AI 活跃群，除已加入和 Telegram `can_send=True` 外，还必须满足独立的群管机器人准入 ready。权限探测必须区分“缺字段 / 未知”“账号不在群”“默认禁言”“单账号被禁言”“发送 API 明确失败”；不能把缺少 `send_messages` 字段直接判定为 `can_send=False`，也不能把 Telegram 探测成功当成群管机器人放行。
- 群管机器人准入的控制观察必须有入群前 listener 基线和每轮 listener 拉取的持久化 observation 证据；没有基线、读取失败或最新窗口未覆盖基线时显式为 `observation_stale`，不得靠等待时间、`can_send=True`、历史发言或最新 N 条快照自动 ready。控制消息必须先通过来源信任，再参与归属；内联 URL/callback 按钮必须随消息持久化为无 callback data 的摘要，并由 Gateway 对原消息做精确重读校验。无可信频道规则且无目标级 policy 时为 `group_bot_policy_unresolved`，不能展示成“需要关注频道”。线上存量无基线或被历史错误归属的 admission 只能由 `targets.manage` 带版本、理由、证据单账号重启观察；不得批量 reset 或隐式 ready。
- 为兼容存量账号而不启用全量准入硬门禁时，空/无证据 admission 的首条正文也必须进入 `pending_visibility`，完整观察窗口结束前不得因 Telegram 瞬时返回 message id 或短时可读而计群日/coverage 成功。精确 message id 在窗口后不可见时写 `post_send_intercepted` 并停止该账号后续正文。unknown-role bot 只有在同 peer 重复给出相同精确频道 URL 集合与 callback 签名，且 bot 消息与同群开放 `pending_visibility` 的远端消息满足后继顺序和不超过 180 秒的相关窗口时，才可作为当前群的 `post_send_intercept_rule` 受限信任证据；单条提示、普通推广、无 callback 或无开放 hold 均不得批量展开关注。
- 对频道点赞，准入完成条件是账号已关注 / 已加入目标频道。Planner 阶段不得把没有账号-频道关系的账号直接安排点赞；Dispatcher 运行时如果发现账号未关注 / 未加入，应补齐准入动作并延后当前点赞，而不是把该 action 终态失败或直接调用 Telegram 点赞接口。
- 对频道评论 / 回复，准入完成条件是账号已关注 / 已加入频道并能访问对应讨论区。Planner 阶段不得把没有账号-频道关系的账号直接安排评论；Dispatcher 运行时如果发现账号未关注 / 未加入，应补齐准入动作并延后当前评论，而不是把该 action 终态失败或跳过。
- 群机器人、图形验证码和入群问题只在当前证据仍存在时展示。目标群已经没有对应机器人或验证消息时，账号详情和任务详情必须刷新为最新目标能力，不得沿用旧文案。
- Task Center 对成功发送必须先使用 `Action.status=success`、成功 Attempt 和非空 Telegram 远端消息 ID；历史 `result.error_code/error_message` 若来自此前的准入等待，不得把已成功 Action 投影成“需关注频道”、失败类型或运营异常。原始字段可保留审计，但读取展示必须与当前终态一致。

### 6.1.2 运营方案到任务

```text
运营中心编辑方案
  -> 保存 operation_plan_templates
  -> 选择覆盖目标 operation_plan_targets
  -> 生成预览 operation_plan_generation_runs(mode=preview)
      返回预计任务、账号容量、准入动作、风险和阻塞原因
  -> 生成任务草稿 / 生成并启动
      写 tasks
      写 operation_plan_task_links
      写 generation run 结果和审计
  -> 后续调整方案
      先生成影响预览
      确认后更新关联任务配置或创建新任务版本
```

运营方案不会直接执行 Telegram 动作；它只负责把运营目标、账号策略、任务类型、规则、AI 策略和风控策略组合成任务配置。失败分三层展示：

| 阶段 | 失败位置 | 页面展示 | 后续处理 |
| --- | --- | --- | --- |
| 保存方案 | `operation_plan_templates` 校验失败 | 方案编辑抽屉字段错误 | 不创建任务，不影响旧方案 |
| 生成预览 | 目标、账号、规则、AI 或风控不满足 | 预览页展示阻塞和 warning | 允许修改方案或只生成可执行目标 |
| 生成任务 | `tasks` 创建失败或部分目标失败 | 生成结果展示成功、失败、跳过 | 成功任务写 `operation_plan_task_links`；失败项可重试 |
| 应用调整 | 运行中任务影响不明确或权限不足 | 影响预览阻塞，必须人工确认 | 确认后写任务配置、审计和 generation run |

### 6.2 任务创建到执行

```text
前端创建任务向导
  -> POST /api/tasks/{type}/create-and-start
      独立执行权限、字段、目标/账号范围引用和结构合同校验
      后端 upsert operation_targets
  -> 先提交 tasks
  -> start 按任务类型建立task-day ledger/current route与运行blocker；current AI/view不建legacy Cycle
  -> tasks.status = running
  -> Planner 先补齐关注 / 加入前置动作
  -> Planner生成类型化业务owner；current AI生成obligation/FOP+assignment+intent并转generation_pending
  -> Generation写job/variation/memory并为accepted normal创建ready Action；typed签到由Planner分支创建
  -> Dispatcher只claim ready Action并execute
  -> actions.result / execution_attempts
  -> task stats / operation metrics / audit
```

任务创建到执行的读写细节：

| 步骤 | 写入 | 页面立即展示 | 后续消费者 |
| --- | --- | --- | --- |
| 创建请求校验 | 无业务写入 | 表单内只展示必填字段、调用者授权、同用户可见目标引用和数量/内容合同结构错误；不读取 Telegram/账号/代理/准入/容量运行事实 | 创建接口执行同一静态校验；调用者无权返回 403/不泄露对象存在性的 404 |
| 创建任务 | `tasks.created_by_user_id/create_task_type/client_request_id/request_fingerprint`、必要时仅按本地语法/规范化 username upsert `resolution_state=pending` 的 `operation_targets`、审计；soft-delete 不释放创建幂等键，不调用 Telegram resolve/probe | 任务列表出现 draft / running 任务；首次 201、幂等回读 200、同键不同配置 409；不要求容量确认 | Planner / 启动器 |
| 启动运行期评估 | `TaskStartOperation`、`task_day_ledgers`、任务账号/目标快照、运行 blocker 投影；同 start operation 幂等 | 任务详情展示真实账号、准入、传输、容量、内容与安全状态；启动失败保留 task_id | Planner |
| Planner 规划 | 类型化obligation/assignment/intent或非AI actions、`tasks.next_run_at`、`tasks.stats` | current AI先展示`generation_pending/check_in_ready`与Generation漏斗，非AI展示自身Action；不得用pending Action冒充AI已物化正文 | Generation、Dispatcher、任务详情 |
| Dispatcher 执行 | `actions.result`、`execution_attempts`、账号状态 | 任务详情 action 状态变化 | Metrics、Recovery |
| Metrics 汇总 | `task_runtime_summary`、`target_runtime_summary`、`account_runtime_summary`、`operation_issue` | 运营中心、账号中心和任务列表摘要刷新 | 运营中心、账号中心、运营数据 |

前端不得在创建前要求运行资源预检或风险确认。创建只校验请求能否形成合法、唯一、可审计的任务义务；创建并启动或后续启动时，后端才冻结任务日事实并持续复检目标、账号、规则、准入、传输、容量和风控状态。

任务创建请求必须携带稳定 `client_request_id`，按当前用户、任务类型和该键幂等，并持久化规范化任务配置与 `start_requested` 的 `request_fingerprint`。同键同 fingerprint 返回原 Task；同键不同 fingerprint 返回 `409 idempotency_key_reused`，不得覆盖或静默返回旧配置。首次创建返回 201，幂等重放返回 200。`create-and-start` 使用两个短事务：先提交 Task，再以 `TaskStartOperation.start_operation_id` 启动；每个Task仍以`task_id`主键保存0或1条current operation，`start_operation_id`保持全局唯一，不新增persistent replaces_id/history或`(task_id,start_operation_id)`复合unique。replace命令携带expected current id/version；新`processing`同事务清空旧result，`started`与实际ledger/route/target-set/lifecycle结果同事务写完整result identity/hash，`failed`不得保留result。普通任务继续单行CAS；active AI obligation合同的first-start/start-after-stop/rollover-start固定规范顺序`TaskContract/Fleet/Inventory -> Enrollment -> Task -> TaskStartOperation -> ledger/route/bootstrap`，同一事务复用已持有guard/owner锁调用protected lifecycle/bootstrap，不能先锁Task/StartOperation再反锁Enrollment，也不能让外层operation started而内层只建半个route。容量、准入、Provider、代理或协议暂不可用时，Task 与 operation 可进入合法running/started并以typed waiting blocker展示；ledger/route启动事实失败则`start_failed`且无result。迟到writer的expected version不匹配只回读current，不能覆盖新的processing/started。

所有任务读写DTO统一暴露并消费Task行版本：`TaskOut.task_version`必填且等于`tasks.version`；generic/type-specific PATCH body与start/pause/resume/stop/delete命令一律携带`expected_task_version`，不采用各入口自行读取“最新版本”或混用`config_revision`。create返回初始version；create-and-start第二事务冻结第一事务返回的version；每个成功写返回新的`task_version`（delete 202返回accepted version及operation id），stale统一`409 task_version_conflict`并返回current version供重新读取，不隐式重放用户变更。前端表单保存与生命周期按钮必须提交当前详情中的version；409时丢弃本地旧快照、刷新详情并要求用户重新确认。所有router、批量/内部入口与TaskStartOperation契约测试都必须证明同一expected version只有一个CAS winner。

零当前行的 API 投影固定为：当前合同只创建未启动的 draft 返回 `not_requested/null/null`；真实 start 一旦进入 processing、started 或 failed，operation ID/version 必须非空。旧合同 Task 不通过该 API 兼容投影，统一由删除 operation 查询状态。

AI 活跃群和频道评论 / 回复不能共用同一数量合同：AI 按配置群日总量与本任务动态必达账号覆盖计算有效目标，再按累计非零权重规划；频道评论仍按逐消息目标和其专项预算分配：

```text
结构合法的 Task 创建成功
  -> 启动时只冻结任务日时间边界；账号范围 / ready / blocker 按任务内事实动态刷新
  -> AI 活跃群 natural_full_day due_by_now + 当前占位/真实阶段空闲槽
  -> coverage_need + volume_need -> planning_need -> 单次有界 batch
     频道评论按每条消息目标补差额
  -> 按最少引用回复数拆分普通消息和引用回复消息
  -> 建立不可变 ContentMixContract 与 policy_min/selector_plan 素材义务
  -> 引用回复消息先绑定可回复对象，再进入专用 Prompt 生成
  -> AI 先补未覆盖 ready 账号，再按最少成功/最久未发稳定轮转
  -> 时间/账号抖动只改变合法排程
  -> 账号安全、Telegram SlowMode/FloodWait、共享在途容量最终约束本次执行
```

默认推荐公式必须满足以下产品口径，具体系数可配置但不能写死到前端：

- AI 活跃群只推荐 `daily_message_target`，默认等于创建时该任务当前合格账号数；启动后base等于配置值、effective等于`max(base,current_required_account_count)`并随任务内资格事实推进独立revision，兼容planned/effective API字段同值映射到effective，已确认数不抬高计划；不推荐或保存每小时目标/上限。
- AI 活跃群不执行退役 hard-hourly 目标或 quiet-hours 静默停发，但仍按 24 小时 `natural_full_day` 曲线计算 `due_by_now`。页面展示任务日目标、当前累计到期量、真实 open/generating/executing/unknown/confirmed 和阶段空闲槽；这些观测不得降低群日目标或把 future 义务提前领取。
- 频道评论 / 回复默认任务小时量按可评论账号数动态推荐，例如以 `可评论账号数 * 4` 为基准，并设置产品上下限。
- 频道评论 / 回复的每条消息累计评论目标按可评论账号数动态推荐，例如以 `可评论账号数 * 0.6` 为基准，并设置产品上下限。
- 频道评论 / 回复的任务内 `max_comments_per_account_per_hour` 是只读系统异常门禁，固定 `1_000_000`，不再根据账号数推荐或形成任务级硬上限；账号全局硬安全容量由系统调度。
- 当账号数、目标能力或风控导致推荐值下降时，前端必须解释原因；当推荐值高于当前手动值时，只提示“不自动覆盖”。

数量规划不得引入静默降级或假成功。AI 供应商不可用、AI 候选不足、规则过滤、质量闸门、账号容量不足、目标不可互动和准入验证卡住都必须以明确状态、跳过原因或容量缺口展示。

引用回复数量也属于规划硬口径。current AI活群由Planner在aggregate allocation/RequirementAssignment与immutable intent中冻结“每轮最少引用回复数”和typed reply source，normal Action尚不存在；Generation accepted variation后创建ready Action时原样冻结这些identity。频道评论按自身合同在Planner蓝图/Action payload冻结“每条频道消息最少引用回复数”。我方可引用bound fact不足、远端消息identity缺失、讨论区未采集或Generation不足时不得降级为普通消息，必须暴露typed reply shortfall；频道评论候选来源不受AI活群我方引用限制影响。

### 6.3 AI 活跃群

```text
准入子任务持续补齐 ready pool
  -> 已可发言账号进入主任务可用容量
Listener 采集群上下文
  -> Planner 以active DueRankSet anti-join与coverage gap取义务，冻结aggregate assignment/intent并转generation_pending
  -> Generation worker创建/读取唯一GenerationJob，基于事实锚点、画像和intent生成variation
  -> 规则过滤、语义去重、幻觉风险和输出校验；accepted variation+memory原子创建ready Action
  -> 六轮仅coverage已完成的direct extra-volume写handoff，Planner消费；mask_missing coverage由Planner直接签到
  -> Dispatcher只领取ready Action并按Telegram权威限制发送，不执行账号冷却或AI生成
  -> 记录job/variation/handoff、Action/Attempt、typed remote fact/binding和immutable settlement
```

AI 活跃群的默认策略是“接话为主、低频暖场为辅”：

- 最近存在可用真人消息时进入接话模式。系统只围绕最近 3-8 条真人消息、被 @ 的对象、当前人名 / 话题 / 问题生成短句回复，优先追问、附和、吐槽、补充和轻量转场。
- 长时间没有可接真人消息且任务允许空闲续聊时进入低频暖场模式。暖场只允许少量账号抛轻量话题或延续任务主题，不能编造账号面具和任务上下文之外的具体经历、位置、回访、准点、穿着、服务过程等事实。
- 上下文不足、重复风险高、事实锚点不足、规则命中或目标群当前话题不适合接入时，normal候选进入质量拒绝并记录原因。只有当前累计到期的active rank可推进，future继续等待；六轮失败时仅coverage已完成且有active/usable面具的direct extra-volume由Generation写handoff并交Planner签到，`mask_missing`未完成coverage另走Planner direct分支，其余暴露`content_capacity_gap`。
- 每条候选消息必须记录事实锚点，锚点可以是真人消息 ID、当前话题、素材 ID 或账号画像。没有锚点的具体事实必须被丢弃或改写为泛化追问 / 附和。
- 全站目标画像只影响表达方式、常见话题和句式，不允许成为具体事实来源。画像不可用或样本不足时，AI活跃群仍可围绕实时上下文生成；没有开放义务时不制造消息，存在开放义务时按normal 3+3及两条typed签到分支收口，不能把“存在义务”解释为任意direct可签到。
- 同一轮多个账号必须有角色分工，例如起哄、追问、补充、降温、观察，不允许多个托管账号连续表达同一语义。
- AI 活群按 24 小时 `natural_full_day` 曲线和任务日 ledger 计算 `due_by_now`；Generation/interaction 只消费当前到期量，开放但未到期的义务不得提前执行。
- 本轮 Turn 数由 `planning_need` 与开放队列空间自动形成，只控制本轮数据库批次；参与抖动和账号覆盖不得把它变成新的业务总量或小时上限。
- 每轮最少引用回复数在系统计算的本轮 Turn 数内生效，不额外抬高本轮或群日总量；实际要求为 `min(reply_min_per_round, logical_cycle_turn_count)`。Planner 必须先确定本轮 Turn，再从中拆出引用回复 Turn；普通发言 Turn 和引用回复 Turn 使用不同 Prompt。
- AI 活跃群reply source固定来自同tenant、同Task、同目标群且由canonical send remote fact + `AiGroupMessageQuantityFactBinding(bound)`证明的历史我方消息，并要求可引用remote message identity与冻结正文；Action/ExecutionAttempt只作provenance与timeliness evidence，不能单独证明reply source或完成。真人/其他成员消息仍只用于上下文、事实锚点和speaker打断，禁止成为`reply_to_message_id`。Planner assignment、Generation和Gateway必须复用同一typed查询；其他Task/群及已被current intent占用的source排除。完整合同见`ai-group-generation-failure-churn-remediation-prd.md`。
- 引用回复 Turn 必须先绑定具体引用对象，再生成内容。引用回复 Prompt 必须包含被回复消息作者、原文、当前群上下文、任务配置、全站目标画像、账号角色 / 记忆和规则约束，并明确“本条是引用回复，只围绕被引用消息自然接一句，不要复述原文，不要像普通发言展开话题”。
- 账号选择必须优先补同一任务当日未覆盖、已准入且存在安全传输路线的当前必达账号；recovering 保留自身义务，当前事实版本不可恢复则当日 abandoned 并释放未进 Gateway 义务。任何参与比例、批次大小或容量风险都不得降低配置目标或恢复任务级小时预算门禁。
- 同轮默认优先一号一条。即使本轮 Turn 数超过可用账号数，也不得让同号在没有真人消息间隔的情况下连续发送；没有可替代账号时，剩余 Turn 写为 `speaker_rotation_wait`。跨轮复用同样以真实真人消息打断为前提，并受账号小时上限和全局风控约束。
- 系统必须根据群日当前欠额/占位、未覆盖 ready 账号和 Generation/interaction 实际空闲槽计算本轮计划数，不得固定为极低的 1 条、固定 2-5 个账号或固定每小时轮数。
- `planning_need`必须在同一ledger/target快照按active DueRankSet对canonical bound fact、committed Gateway hold、unknown与有效pre-call owner做集合anti-join，再与当前到期coverage gap取并集/较大需求；禁止用分别count的scalar公式。每个due的`fact_first_v3` Task一次Planner轮转只调用一次`build_plan`，批次为`min(planning_need,distinct ready/online/Task-scoped progressable accounts,Generation/interaction free slots,daily_coverage_plan_batch_limit,20)`并立即轮转；不得把批次实现成1条或同Task循环排空。
- 不再提供用户手动“每轮计划发言数”模式；运营数量合同只有 `daily_message_target`，批次上限由运行时控制且不得改变群日总量。
- 详情页必须展示系统本批请求 Turn 数、AI 返回候选数、清洗过滤数、质量过滤数、签到兜底数、最终 Action 数和等待/减少原因。
- 准入子任务通过的新账号必须按新scope revision动态加入后续aggregate allocation/assignment选择池；准入失败、待验证、人工处理账号不得进入主互动。旧ContentMix/natural conversation Cycle不是current owner。
- `全账号日覆盖模式` 属于 `group_ai_chat` 的配置模式，不是新的任务类型。任务账号范围选择“全部可用账号”时必须默认启用该模式，旧 all 账号任务即使存量配置仍为 `natural`，运行和统计也按全账号日覆盖有效口径处理；指定账号分组和手动账号任务不得被强制扩大为全平台范围。
- AI 日覆盖任务在首次启动时建立任务内账号范围关系；任务日 ledger 只冻结时间边界和配置目标，不冻结账号分母。范围投影必须忠实保留创建选择：全部账号取全部合格账号，账号组按 `account_group_id` 取该组内合格账号，手工选择只取 `account_ids`；账号组不得因没有 `account_ids` 被投影为空，也不得扩大成全租户。当日账号录入、Session/身份、membership、can-send 和准入事实变化必须增量刷新该 Task 范围；Telegram 权威 `session_invalid|need_relogin|cannot_send` 当日放弃，目标解散终结目标，均不写账号全局冻结。常规 Planner 读取持久化范围与欠账索引，低频 reconciliation 只补偿事件丢失，不能高频全表扫描或复活已放弃旧任务日义务。
- Planner对完整候选账号池批量读取在线、Session、授权、membership/admission、Telegram FloodWait/SlowMode及current owner事实并在本轮复用；已退役的账号冷却、任务小时/日上限和`max_concurrent`不得作为current截断，也不得通过逐账号`min/max/count`查询或隐藏上限重新引入。查询复杂度必须随扫描页有界。
- Planner 读取目标群最近上下文必须同时按 `tenant_id + group_id` 过滤，并由 `(tenant_id, group_id, sent_at DESC, id DESC)` 或等价索引直接取得有界最新记录；不得先按全租户/全表时间排序再过滤目标群，也不得因上下文历史增长让单轮 Planner 超过 60 秒。
- 每个任务时区自然日 00:00 只建立不可变的时间/时区账本和配置数量义务；“任务 × 群 × 账号”覆盖范围随本任务账号登录/授权/代理/membership/can_send/admission 事实动态刷新。当日新增合格账号立即加入；暂时不可用但存在合法恢复路径时进入 `recovering`，没有合法恢复路径时当日 `abandoned_for_day` 并释放未进 Gateway 义务。已成功事实不撤销，也不得倒灌给其他账号或任务。数量义务与 coverage 分账，一条真实消息可同时完成本任务总量 1 和该账号 coverage 1。
- Planner只选择active-rank缺口并冻结账号、coverage/volume assignment、reply/material/act-type与immutable intent。normal body转`generation_pending`，由Generation执行主3轮/备用3轮并在accepted variation+memory后创建ready Action；`mask_missing`只允许未完成coverage direct由Planner走scoped claim，`normal_generation_exhausted`只允许coverage已完成且有active/usable面具的direct extra-volume由Generation写immutable handoff，再由Planner消费。reply/material/其他六轮失败不得签到；引用失效只能对同义务做合法assignment/intent revision，不能改direct或增总量。Action创建不是完成。
- 覆盖完成的唯一数量事实是canonical发送remote fact通过唯一`AiGroupMessageQuantityFactBinding(bound)`确认同一stable obligation，并由coverage projector把该fact绑定到本Task/群/账号coverage；成功Action、ExecutionAttempt、`remote_message_id`与可见性只作provenance/confirmation-time evidence，任一单独或组合都不能直接加confirmed/coverage。`pending`、`pending_visibility`、`failed`、`skipped`、`unknown_after_send`、未准入、不可发言、风控受限和内容生成失败均不计完成。详情页完成率使用“bound remote fact确认账号数 / 当前任务日必达账号数”，并展示准入、权限、在线、Session、内容、容量、发送、projection和未知结果阻塞。发送型unknown只有同request Gateway journal/adapter typed safely-not-executed证明mutation未开始或明确pre-accept rejection，才允许释放原义务；远端当前未查到消息、超时或换账号不可见均不能证明未发送。
- 群管准入、正常内容质量、传输路线和远端核验是独立事实。日容量预测只写 `completion_risk`，`warning_requires_confirmation=false`，不得形成额外确认步骤或 `PlanAbort`。修复其中一项不得缩小日覆盖分母、停止其他可发送账号的规划，或把剩余问题伪装为任务已完成。
- Dispatcher 不再创建账号级进程内/Redis 单 inflight 占用。对同一 `remote_mutation_key` 建立的短租约必须以 `dispatch_action` 的统一 `finally` 释放；数据库已无该 mutation key 的 executing owner 时不得持续返回冲突。
- 全账号群日目标按`natural_full_day due_by_now`形成active DueRankSet；Planner每轮只做一次最多20条的aggregate plan/技术批，Generation/interaction空闲槽只收窄当前物化，不创建natural conversation Cycle业务owner。容量不足显式暴露且不能停止其他ready账号，也不能突发补齐。AI活群不恢复本地群`daily_limit`、群冷却、活动窗口或硬小时目标；账号、目标准入、在线、安全、内容质量和Telegram真实限制继续生效。
- 全账号日覆盖和参与账号比例的关系：全账号日覆盖是更强的日级履约目标，参与账号比例只能作为普通多轮分配参考，不能降低覆盖账本目标、缩小分母或把失败补量变成低质量内容。

AI 活跃群质量管线必须先做确定性约束，再做 AI 生成，最后做发送前复查：

- Planner启动本轮前读取ready pool、在线状态和授权路线。normal正文固化active面具、短期立场、最新上下文和同账号最近10天消息记忆。`mask_missing`仅对未完成coverage的direct义务取得scoped claim并创建精确`签到`；extra-volume缺面具写typed capacity gap。`proxy_failed`切到已验证路线后继续原assignment/intent链，不触发签到；无路线为`waiting_transport`。其他ready账号继续。
- `all_accounts_daily` 选号必须按显式覆盖扫描页读取 ready 候选并批量判定实时在线状态，再从在线子集按本轮消息预算取账号；候选扫描页大小不能被单轮 Action 预算或当前 `due_debt` 缩成 1，也不得先按 `max_concurrent` / 小时缺口截断、再过滤在线状态，否则靠前离线账号会遮蔽后续在线账号并形成虚假的“账号在线状态不可用”。扫描页只用于候选资格判定，离线页会显式标阻塞并由后续页继续，不改变单轮消息预算、欠账数量、容量、冷却或风控规则，也不构成服务上线账号总量限制。
- `all_accounts_daily` Planner在同一ledger/target/read-model快照取`ActiveDueRankSet`，逐rank anti-join唯一bound quantity fact、committed Gateway hold、unknown hold和current有效pre-call owner得到quantity gap，再与当前scope revision下到期且未被事实/hold/owner占位的coverage key集合取并集形成`planning_need`；禁止分别count后相减。随后只受distinct ready/online/progressable账号数、Generation/interaction真实空闲槽、配置技术批次和硬上限20收窄。准入、面具或在线blocker只影响对应edge/账号，不得清零其他账号或群总量欠额。
- 新实现不得创建 hard-hourly bucket、credit、checkpoint 或 claim class。存量 hard-hourly Action 和统计只由迁移/审计收口，不得继续参与当前 Planner、Dispatcher 份额或任务详情完成口径。
- 全系统同一时刻只允许一个 active `ai_provider_key_version`，所有文本模型共享该 key 的 `max_inflight/RPM/TPM`；任务可选择该 Provider 支持的模型，但不得激活第二个 key 或为模型复制一套总额度。搜索验证码固定 RapidOCR→ddddOCR，不调用任何 AI/VLM Provider。
- 每个logical plan unit先由Planner以aggregate allocation/assignment冻结`act_type`、引用对象、账号、话题方向和讨论对象并创建immutable intent，再由Generation创建/读取唯一GenerationJob。`act_type`只用PRD词表，历史别名在兼容层归一；Prompt、intent/variation、ready Action、详情和短期立场记忆均输出标准值。Generation只能填充已冻结assignment，不得新增账号、扩张plan unit数或改变关系。
- 一轮默认一次transport-batch多个独立GenerationJob；局部job未通过质量过滤时携带自身失败原因、已占用语义簇和账号面具补位。主/备用模型各最多3轮。六轮后只有coverage已完成、有active/usable面具的direct extra-volume可由Generation写immutable handoff并转`check_in_ready`，Planner消费后创建精确`签到`；未完成coverage仅`mask_missing` direct分支可签到，其他写`content_capacity_gap`。各job阶段、轮次和handoff证据独立审计。
- 质量过滤顺序固定为：空内容 / 禁词 / 事实锚点缺失 -> 固化账号面具不匹配 -> 同账号滚动 10 天精确、语义、模板、事实观点重复 -> 同账号短期立场冲突。任何阶段失败都必须留下具体 `quality_decision`、`quality_reason`、账号、面具版本和重复参照。
- 同账号滚动10天去重必须通过数据库原子预占。normal body由Generation在accepted variation Phase C以`tenant_id+account_id+reservation_key`原子CAS message memory并同事务创建ready Action；deterministic check-in复用scoped claim/memory reservation，不进入normal去重。Dispatcher在Gateway前按同一归一化函数与窗口复查，面具升级、回滚或重建不得清空历史。
- `pending`、`reserved`、`claiming`、`executing`、`unknown_after_send`、`success` 都参与同账号 10 天重复判定。明确未进入 Gateway 的失败释放预约后不再阻塞新候选；超过 10 天只保留审计，不再参与硬去重。
- 其他账号的历史内容和同批跨账号相似只作生成多样性提示及统计，不得产生跨账号硬阻断。高质量文本优先；主/备用各3轮失败后，仅coverage已完成且有active/usable面具的direct extra-volume经Generation handoff→Planner使用同`(task,group,account,task-day)` scoped签到。`mask_missing` coverage direct不要求面具匹配但使用独立trigger；两者均不进normal 10天去重，仍需准入、敏感内容、账号用途、轮换和真实远端成功。其他欠量显示`content_capacity_gap`。
- Dispatcher 发送成功、失败、未知发送、账号离线、权限失败都必须回写同一条 `ai_group_message_memory`；成功和未知发送还要更新账号群内短期立场，用于下一轮避免立场跳变。
- 旧任务、存量可登录账号、运行中的 AI 活跃群和监听源必须通过迁移 / reconcile 写入在线需求来源。没有来源的在线状态不得被误认为可用；有运行任务但缺在线状态时，任务详情必须暴露为“在线状态未初始化 / 待保活”，不能归类为 AI 无候选。
- 质量管线的所有决策都必须进入任务详情漏斗：候选数、AI 调用轮次、补位次数、重复命中窗口、模板壳命中、面具低分、立场冲突、在线状态剔除、最终 action 数和 `签到` 兜底数。

### 6.4 转发监听群

```text
Listener 采集源群消息
  -> source event 去重
  -> 规则集版本过滤 / 转换 / 路由
  -> 素材缓存或 source_media_assets 等待
  -> 生成目标群发送 action
  -> Dispatcher 发送
  -> 转发批次、源事件、目标发送项和规则归因
```

### 6.5 频道互动

```text
选择已有频道目标或粘贴新频道入口
  -> 后端 upsert operation_targets
  -> 同步频道消息 / 评论
  -> 创建频道浏览 / 点赞 / 评论任务
  -> 检查候选账号关注状态
      已关注或已确认满足关注条件：进入主互动规划
      未关注：生成 ensure_target_membership action
  -> 已关注账号先执行主互动
  -> 关注成功账号追加进入后续主互动容量
  -> 0 已满足且 0 准入成功则主互动 blocked
```

频道评论 / 回复生成时必须读取全站唯一目标画像，但频道消息和讨论区评论仍是事实锚点。正常评论按生效 generation contract 生成与质量校验；重试请求只能携带安全事实概括，不能把原始敏感词再次拼入 Prompt。启用 v1.1 后，缺面具、事实不足、质量/预算耗尽或可恢复 Provider 路线用尽时，原 `post_comment` Action 从冻结的 20 Unicode/`image_meme` policy 选择，分别写 `content_source=comment_unicode_emoji_fallback|comment_image_meme_fallback`、`fallback_reason` 和 selection identity。reply 保留原引用；引用失效、消息不可评论、账号不可评论或无可用传输路线时不得转 direct 或假成功。只有成功 Attempt、非空远端评论 ID 且文本/media identity 与冻结选择一致才完成评论目标。

频道评论 / 回复运行时异常必须按可恢复性分流：

| 场景 | 系统处理 | 页面展示 |
| --- | --- | --- |
| 账号没有关注 / 加入目标频道或无法进入关联讨论区 | 生成或补齐 `ensure_target_membership`，当前评论延后到准入后重试 | 等待账号关注 / 加入频道后继续评论 |
| 账号已准入但被 Telegram 明确拒绝评论 | 标记该账号对当前频道评论区不可发言，跳过该账号后续评论，其他账号继续 | 该账号对频道评论区不可发言 |
| 频道消息没有可用评论入口 | 标记该消息 `comment_available=false`，同帖待执行评论跳过 | 该消息无法评论 |
| 其他 Telegram / API 错误 | 保留原始失败码、错误消息和尝试记录，不做泛化归因 | 展示原始失败摘要，并提示查看尝试 / Trace |
| 历史 stale 计划或策略替换 | 保留为历史跳过，不上卷为当前失败 | 历史计划已替换 |

频道点赞运行时必须同样先守卫关注关系。账号没有关注 / 加入目标频道时，系统生成或复用 `ensure_target_membership`，当前点赞延后到准入后重试；不得在未关注状态下调用 Telegram reaction 接口，也不得把这类 action 直接记为失败来消耗目标量。频道监听器必须采集 Telegram 声明的 Reaction 能力并区分 `unknown/all/some/none`：新建与存量点赞任务默认 `reaction_scope=all_available`，支持并使用频道声明且普通账号可执行的全部 active 标准 emoji Reaction；自定义/指定 Reaction 默认推荐包含 10 种高频常用互动表情（`👍, ❤️, 🔥, 👏, 🎉, 🤩, 👌, 💯, 🙌, ✨`）；Premium-only Reaction 在没有逐账号 Premium 能力事实前不得进入公共池。点赞任务支持 `message_active_days`（默认 7 天，支持 1~365 天）及 `rolling_window_days` 统一控制单帖点赞有效排期窗口，确保频道历史新旧帖子在设定期限内持续补满点赞欠额，避免过早关闭结算。`reaction_scope=configured` 时，random 模式只能在 `allowed_reactions ∩ 频道可用 Reaction` 中选择。random 模式对每个点赞槽独立等概率抽取，不设置主表情比例、最低数量或固定配额；同一任务、消息和账号排序的结果必须可重放，避免重规划漂移。能力未知或交集为空时不得猜测、替换或创建错误 Action，必须留下 `reaction_capability_unavailable` 可见阻塞。Reaction 能力探测失败必须留下 `reaction_capability_probe_failed`，但已成功读取的共享频道消息快照仍须发布，不能阻塞同频道浏览或评论。specific 模式始终只尝试用户指定 Reaction，不得替换。账号已关注但 Telegram 返回 reaction unavailable 时，只结束本次 attempt，不增加逐消息 confirmed，也不直接关闭整帖；只有已证所有允许 Reaction 对该消息均不可用时，才把该消息履约状态写为 `reaction_capability_unavailable`，任务其他消息继续。

频道评论 / 回复的数量和账号执行规则：

- `预计每条评论 / 回复` 表示每条频道消息的累计目标，不表示每次 Planner 都生成这么多。Planner 必须按已成功、待执行、执行中和已规划 action 补差额。
- `每条频道消息最少引用回复数` 在单条频道消息本轮补差额内生效，不额外抬高该消息总评论 / 回复目标。Planner 必须先确定本轮补多少条，再从中拆出引用回复 action；直接评论和引用回复使用不同 Prompt。
- 频道评论引用池固定来自当前频道消息讨论区下已采集的可回复评论，以及同任务历史成功 `post_comment` action 返回的 Telegram 远端消息 ID。运营人员不需要选择真人评论或自己历史评论范围，系统自动混合挑选可回复对象。
- 引用回复 action 必须先绑定具体评论对象，再生成内容。引用回复 Prompt 必须包含频道原文、被回复评论作者、被回复评论原文、当前讨论区上下文、全站目标画像、任务配置和规则约束，并明确“本条是回复该评论，不是普通频道评论”。
- 多条频道消息同时命中时，先按任务每小时预算在消息之间分配，再按每条消息缺口补计划；不得让单条大目标挤占全部小时预算。
- 同一频道消息下优先不同账号评论。可评论账号充足时，同一账号不应对同一频道消息重复评论；账号不足且目标量较高时，只允许跨时间窗口、跨小时上限复用。
- 回复模式和混合模式的回复也计入同一条频道消息的评论 / 回复总量；任务内账号软上限固定 `1_000_000`，账号全局硬安全容量与 Telegram 限制仍不可绕过。
- 评论数量抖动只能围绕单条消息目标做自然浮动；时间抖动只能改变排程分布；账号抖动只能改变账号选择顺序。三类抖动均不得突破硬上限。
- 评论候选经过主 AI 3 轮和备用 AI 3 轮后仍全部被正常质量闸门过滤时，原 Phase A 蓝图转为审核表情文本兜底；候选失败本身不得写成 ready 或成功，兜底仍须独立内容合同和远端成功事实。
- 评论完成模式固定分为 `continuous` 和 `finite_batch`。`dynamic_new` 默认 `continuous`，持续监听新消息，不存在 task lifetime cap 自动完成；`specific/date_range/latest_n` 可用 `finite_batch`，但必须所有已解析消息逐条达到固化目标后才进入 `completed`。
- `max_total_comments/max_comments_per_account_per_hour` 在创建、编辑、启动与存量接管统一为 `1_000_000`，只作异常门禁，不是完成或规划目标。`pending/claiming/executing` 只占规划 hold，`unknown_after_send` 只占防重复 hold；它们都不能与 success 相加后触发 `completed`。
- 人工 `paused/stopped/deleted` 状态不得被后台收口覆盖。存量带 `completion_reason=lifetime_cap_reached` 的任务不自动复活；审计必须展示原逐消息欠额，由具备权限的运营人员显式选择迁移为 continuous 或新 finite batch，并写审计。
- 频道浏览的 `task_daily_view_safety_cap/max_views_per_account_per_day` 与频道点赞的 `max_likes_per_account_per_hour` 在创建、编辑、启动与存量接管统一为 `1_000_000`，只作异常门禁，不替代或缩减逐消息目标。当前生产低值随发布直接归一；dynamic_new 新消息继续形成完整义务，只有真实规划量触及统一门禁时显示 `task_gate_limit_reached`。

### 6.6 安全与资料批次

```text
点击资料初始化 / 设置二步密码 / 清理登录设备
  -> 进入选择账号步骤，可按账号组、筛选、搜索、跨页勾选或区间选择
  -> 按入口固定 action_types，不在抽屉内混选其他动作
  -> 资料初始化走 profile-preview，一次 AI 请求生成整批资料
  -> 设置二步密码走安全预检；清理登录设备不走预检
  -> 设备清理确认弹窗只展示已选数量、48 小时跳过规则、风险提示和操作原因
  -> 创建 tg_account_security_batches
  -> 设备清理在同一创建事务按 current SV telegram_login_at 严格超过 48 小时分类并创建 eligible/skipped 结果
  -> 创建响应返回 requested/eligible/skipped 与 skipped_reason_counts
  -> 资料初始化批次投影到任务中心，作为系统执行任务展示状态
  -> drain_account_security_batches
  -> update_avatar 项先检查素材 TG 缓存 ready，只使用已缓存完成的头像素材
  -> 头像素材未缓存完成时，批次项进入 waiting_cache / waiting，任务中心展示缓存中、FloodWait、失败或不可恢复原因
  -> Telegram Gateway 执行
  -> 回写 snapshots、items、accounts、avatar_object_key、avatar_preview_url、audit
```

### 6.7 运营中心展示流

```text
用户打开 /dashboard
  -> GET /api/overview
  -> 读取 target_runtime_summary / task_runtime_summary / account_runtime_summary / operation_issue
  -> 展示目标工作台、异常数量、影响任务、建议动作和最近更新时间
  -> 用户点开目标异常
  -> GET /api/operation-issues?target_id=...
  -> 展示代表任务、failure_type、affected_task_count、affected_account_count
  -> 分页读取 operation_issue_sources / operation_issue_accounts
  -> 用户点建议动作
      轻处理：打开上下文弹窗
      中等处理：打开右侧处理抽屉
      重处理：深链跳转对应页面，带 return_to / source_issue_id / target_id / task_id
  -> 弹窗或抽屉完成后刷新当前 issue，不重置目标工作台
  -> 深链返回后恢复目标展开、筛选、分页、滚动位置和最近选中的 issue
```

运营中心展示要求：

- 默认按目标聚合，不按任务列表平铺。
- 每条异常必须能回答：影响哪个目标、关联哪些任务、主要失败码是什么、影响多少账号、建议动作是什么。
- 每个展示数值必须有来源：目标级、任务级、账号级或日级汇总。
- 页面必须展示汇总更新时间；汇总延迟不等同于任务未执行。
- 如果汇总读模型不可用，页面只能展示“汇总暂不可用 / 请稍后刷新”，不能退回明细全表扫描。
- 建议动作必须标注处理方式：`modal`、`drawer` 或 `deep_link`。轻处理和中等处理优先留在运营中心，重处理才跳转。
- 运营中心需要保存本地视图状态：筛选、分页、排序、展开目标、滚动位置和最近选中的 `issue_id`。

### 6.7.1 页面数据加载契约

页面读取按“当前页面必要数据 -> 汇总优先 -> 按需下钻”组织：

- 全局快照只保留登录态、权限和当前页面必需的轻量基础数据，不能在每次路由切换时拉取账号全量、审计记录、归档列表、消息任务、AI 供应商、提示词和租户设置。
- 当前页面必要数据任一接口失败时，刷新流程必须失败并展示错误；不得用 `[]`、`{}` 或旧数据静默替代失败响应。
- 当前页面的关键写操作失败时必须展示后端错误 detail 或响应正文；只展示泛化失败文案视为不可验收。
- 运营中心默认读取 `/api/overview` 和运营异常汇总；目标、任务、账号和 action 只在用户点击对应入口后按 ID 查询。
- 运营数据默认读取 `/api/operation-metrics/summary`；异常账号、风险目标、最近任务和失败执行项保留在汇总返回中，不再触发额外全量明细扫描。
- 任务中心列表只读 `/api/tasks` 和必要调度摘要；创建或编辑任务时再加载账号、账号分组、目标、规则、频道消息和评论等表单支持数据。
- TG 账号管理首屏只加载 20 条账号、账号分组和当前页账号可用性汇总；翻页、分组和搜索由服务端分页驱动，不得先拉完整租户账号再做客户端分页。账号可用性只在账号页进入或当前页账号变化时读取，不能作为全局页面切换请求。
- 风控中心首屏读取风控摘要和代理资源；账号评分、处置队列、命中记录和策略审计通过 Tab 和指标点击筛选展示，不依赖全局账号全量。
- 系统设置按 Tab 加载底座配置：开发者应用、后台账号、AI 供应商、黑话 / 提示词、素材运行配置分别读取各自接口；进入系统设置首屏不得拉取账号全量或素材全量。素材运行配置里的缓存执行账号只读取有界候选页或通过搜索下钻。
- 顶部“刷新当前数据”只刷新当前页面必要数据，不能把所有菜单资源一起刷新。
- 验收标准：`/dashboard`、`/usage-reports`、`/task-center`、`/accounts`、`/risk-control` 页面切换 API 请求数较 2026-06-21 线上基线减少至少 50%，且无关全量列表不随所有页面加载。

### 6.8 任务详情下钻流

```text
用户从任务列表或运营异常进入任务详情
  -> GET /api/tasks/{task_id}
      返回任务配置、目标、规则、账号摘要、membership_subtask、task_runtime_summary、统计摘要和分页入口
  -> GET /api/tasks/{task_id}/membership-items?page=&phase=&manual_required=
      分页返回准入账号 item、阶段记录、验证摘要和可操作状态
  -> GET /api/tasks/{task_id}/actions?page=&page_size=&status=&action_type=&time_range=
      分页返回 action 明细
  -> 用户展开某条 action
  -> GET /api/tasks/{task_id}/actions/{action_id}/attempts
      返回 execution_attempts 和 gateway result snapshot
```

任务详情下钻要求：

- 详情顶部先展示任务状态、派生运行阶段、准入前置和统计摘要，再展示执行明细；首屏不能依赖全量 action、准入账号、AI cycle、relay batch 或 attempt 聚合。
- `GET /api/tasks/{task_id}` 是只读首屏摘要路径，不得触发 `refresh_task_stats()`、汇总重算、执行事实修正或其他写库动作。
- 准入前置账号明细必须由数据库分页和计数支撑；不能先全量构造 rows 再在内存里切片。点击账号行打开二级抽屉后才能加载验证问题、AI / MiMo 答案和原始错误。
- Action 明细必须分页；默认按最近计划 / 最近执行时间倒序。
- Attempt 明细默认折叠，只有展开 action 时加载。
- 失败 action 必须显示 `failure_type`、运营人员可读原因、原始错误入口、是否上卷 `operation_issue`。
- `unknown_after_send` 必须独立标识，不能和普通失败混在一起。
- `task_runtime_summary.latest_failure_type` 和运营异常上卷必须从未闭环异常里选最近一条，包括 `failed`、`retryable_failed` 和 `unknown_after_send`；没有普通 `failed` 时也不能清空最近异常。

### 6.9 汇总读模型更新流

```text
action / attempt 写入完成
  -> Metrics 周期扫描最近变化或消费结果事件
  -> 更新 task_runtime_summary
  -> 更新 target_runtime_summary
  -> 更新 account_runtime_summary
  -> 判断是否创建 / 合并 / 关闭 operation_issue
  -> 写 daily_runtime_stats
```

汇总更新规则：

- 同一任务同一状态的计数可以覆盖更新，不能重复累加。
- 同一目标同一 `issue_type + failure_type` 在汇总窗口内合并，不刷屏。
- 当失败连续消失、任务恢复成功或人工标记 resolved 时，`operation_issue.status` 进入 `resolved`，但历史日统计保留。
- 汇总任务失败不能影响 action 执行事实；下一轮 Metrics 必须可重算。
- 清理热事实前，必须确认对应日期和维度汇总已生成。

---

## 7. 接口清单

接口清单分两类理解：

- 当前已存在接口：重构时必须保持兼容，允许在响应字段上扩展。
- 目标需扩展接口：本 PRD 定义的目标契约，后续统一重构时补齐；未落地前前端不得假装功能已完成。

任务类型接口可以在产品文档中写成 `{type}` 方便理解，但实现必须映射到当前后端已有的具体路径，例如 `group-ai-chat`、`group-relay`、`channel-view`、`channel-like`、`channel-comment`。

### 7.1 账号与安全

- `GET /api/tg-accounts`
- `POST /api/tg-accounts`
- `DELETE /api/tg-accounts/{account_id}`
- `POST /api/tg-accounts/{account_id}/login/start`
- `POST /api/tg-accounts/{account_id}/login/verify`
- `POST /api/tg-accounts/{account_id}/login/qr/check`
- `POST /api/tg-accounts/{account_id}/sync-now`
- `POST /api/tg-accounts/{account_id}/sync-targets`
- `GET /api/tg-accounts/{account_id}/detail`
- `GET /api/account-pools/code-receiver`
- `POST /api/account-pools/code-receiver/accounts`
- `DELETE /api/account-pools/code-receiver/accounts/{account_id}`
- `PATCH /api/tg-accounts/{account_id}/identity`
- `GET /api/tg-accounts/{account_id}/authorizations`
- `POST /api/tg-accounts/{account_id}/authorizations/{authorization_id}/refresh`
- `POST /api/tg-accounts/{account_id}/authorizations/{authorization_id}/activate`
- `POST /api/tg-accounts/{account_id}/authorizations/standby/provision`
- `POST /api/tg-accounts/{account_id}/authorizations/self-heal`
- `GET /api/tg-accounts/{account_id}/authorization-devices`
- `POST /api/tg-accounts/{account_id}/authorization-devices/refresh`
- `POST /api/tg-accounts/{account_id}/authorization-devices/cleanup`
- `GET /api/tg-accounts/{account_id}/authorization-device-cleanups/{operation_id}`
- `GET /api/tg-accounts/availability/summary`
- `GET /api/tg-accounts/{account_id}/availability`
- `POST /api/tg-accounts/availability/rebuild`
- `GET /api/tg-accounts/security/summary`
- `GET /api/tg-accounts/{account_id}/security`
- `POST /api/tg-accounts/{account_id}/security/refresh`
- `POST /api/tg-accounts/security/managed-2fa`
- `POST /api/tg-accounts/security/managed-2fa/rotate`
- `POST /api/tg-accounts/security-batches/precheck`
- `POST /api/tg-accounts/security-batches/profile-preview`
- `POST /api/tg-accounts/security-batches`
- `GET /api/tg-accounts/security-batches`
- `GET /api/tg-accounts/security-batches/{batch_id}`
- `POST /api/tg-accounts/security-batches/{batch_id}/retry`
- `POST /api/tg-accounts/security-batches/{batch_id}/cancel`
- `GET /api/tg-accounts/{account_id}/verification-codes`
- `POST /api/tg-accounts/{account_id}/verification-codes/poll`
- `GET /api/tg-accounts/{account_id}/verification-tasks`
- `POST /api/tg-accounts/{account_id}/pending-execution/recheck`
- `GET /api/tg-accounts/{account_id}/execution-records`

账号可用性接口默认读取 `account_runtime_summary`。`POST /api/tg-accounts/availability/rebuild` 只允许管理员或维护任务调用，用于汇总异常、迁移后或故障恢复时重算；任务创建不依赖该汇总或容量预检，启动器与执行链必须实时校验账号能力。重算时必须合并账号状态、session、代理、容量、FloodWait / SlowMode、最新安全快照、账号安全批次待重试时间和最近风控检查结果，不得把汇总表当唯一事实源。

账号中心补齐接口为目标契约。实现可以在保持既有路径兼容的前提下复用或映射到现有服务，但接口语义必须满足：

- 接码专用分组和账号身份接口必须共同维护唯一系统 `AccountPool` 与 `account_identity=code_receiver`；写入后必须立即影响任务候选集合。
- 授权资产接口必须返回三槽位状态、冻结 Developer App/api_id、远端授权 active/revoked/unknown、聚合状态、可救援关系和全部掉线标识；不返回 Session/AuthKey/hash 明文。
- 设备接口必须在新账号首次登录后立即返回 `platform_current/platform_retained/external/unresolved`、匹配授权/槽位/代次、Developer App、Telegram `date_created` 与脱敏设备元数据，以及 `remote_active_total/platform_current/platform_retained/external/unresolved/as_of/stale/current_sv_login_at/cleanup_button_enabled/cleanup_disabled_reason`。归属由服务端按非零 hash 精确计算，不由前端按 `api_id` 重算；不返回倒计时或资格预检 token。
- 设备清理不得调用 `security-batches/precheck` 或单账号 preview。单账号和批量创建接口都只读数据库，以严格 `server_now > current_sv_login_at + 48h` 分类 eligible/skipped，并返回 `requested_count/eligible_count/skipped_count/skipped_reason_counts`；创建阶段零 Telegram 调用。worker 为 eligible 账号逐个读取并冻结执行开始时 exact set，保护事实不完整或读取超时只使当前账号失败；随后逐 hash reset 并 readback。FRESH 只使当前账号失败，不等待或自动重试；最终状态只能来自 Telegram exact-set readback。
- 验证码接口必须读取 Telegram 官方服务消息并返回结构化成功或失败原因，不得返回假成功。
- 待处理重查接口必须只重新评估并排队满足条件的 action，不能重复创建已存在 action。
- 执行记录接口必须聚合旧版 `message_tasks` 和新版 `tasks/actions/execution_attempts`，按统一动作字典输出，避免账号记录显示为 0 条。

### 7.2 目标和消息

- `GET /api/operation-targets?page=&page_size=&q=&ids=&linked_group_id=&capability=&target_type=&account_id=`
- `POST /api/operation-targets`
- `PATCH /api/operation-targets/{target_id}`
- `GET /api/operation-targets/{target_id}/detail`
- `POST /api/operation-targets/{target_id}/sync-messages`
- `POST /api/operation-targets/sync-all`
- `POST /api/operation-targets/{target_id}/admission/retry`
- `POST /api/operation-targets/{target_id}/capabilities`
- `GET /api/channel-messages`
- `GET /api/channel-comments`
- `GET /api/message-send-tasks`
- `POST /api/message-send-tasks`
- `POST /api/message-send-tasks/batch`
- `GET /api/message-send-tasks/{task_id}`
- `POST /api/message-send-tasks/{task_id}/precheck`
- `POST /api/message-send-tasks/{task_id}/dispatch`
- `POST /api/message-send-tasks/{task_id}/retry`
- `POST /api/message-send-tasks/{task_id}/cancel`

`POST /api/operation-targets` 只用于管理修订或外部显式导入，不作为任务创建前置要求。任务创建仍通过任务接口的 `target_input` 在后端事务内 upsert 目标。目标 upsert 必须返回来源：`source_type=account_sync/task_input/manual_admin`、去重命中、目标能力快照和审计 trace。

`GET /api/operation-targets` 的有界模式默认 `page=1&page_size=20`，最大 `page_size=100`，并返回 `X-Total-Count/X-Page/X-Page-Size`。`q` 搜索标题、username 和 TG peer id；`ids` 使用重复查询参数（如 `ids=1&ids=2`）回显已选目标；`linked_group_id` 用于关联群定点读取；`capability` 只接受 `send/listen/archive/task`。未携带任何新增参数时暂保留旧完整列表兼容语义，原 `target_type/account_id` 过滤不删除；所有第一方消费者必须显式分页。非法参数必须返回可见 4xx，不能忽略后退回全量。

有界实现必须先对 `OperationTarget` 完成租户过滤、count、稳定排序和分页，再只对当前页关联 `TgGroup` 与 `TgGroupAccount` 做批量 SQL 条件聚合；不得把全部关系 ORM 行加载到 Python。`GET /api/operation-targets/runtime-summary` 同步支持 `target_ids`，供运营中心按当前目标页读取摘要。

消息发送接口必须返回发送记录、目标解析、账号预检结果、失败原因和是否已上卷运营异常。取消、重试和派发必须写审计；失败上卷由 Metrics 读取 `message_task_attempts` 或统一 Task / Action 事实完成。

### 7.2.1 目标画像

- `GET /api/target-profile`
- `PATCH /api/target-profile/settings`
- `GET /api/target-profile/usage`
- `GET /api/target-profile/source-candidates`
- `GET /api/target-profile/sources`
- `PUT /api/target-profile/sources`
- `POST /api/target-profile/sources/{source_id}/sync`
- `POST /api/target-profile/sources/{source_id}/pull-history`
- `GET /api/target-profile/runs`
- `GET /api/target-profile/runs/{run_id}`
- `GET /api/target-profile/samples`
- `PATCH /api/target-profile/samples/{sample_id}`
- `GET /api/target-profile/quality-rules`
- `PATCH /api/target-profile/quality-rules`
- `POST /api/target-profile/recompute-candidates`
- `POST /api/target-profile/rebuild`
- `GET /api/target-profile/versions`
- `POST /api/target-profile/versions/{version_id}/restore`
- `POST /api/target-profile/clear`

目标画像接口只服务全站唯一画像，不接收 `target_id + profile_scene` 作为画像身份。旧目标级画像接口不进入新前端主流程，旧数据放弃，不迁移、不合并、不兼容。

写接口统一要求 `target_profile.manage`，读取要求 `target_profile.view`。修改学习来源、监听账号、质量规则、样本状态、恢复版本和清空画像必须写审计；重建、历史拉取、候选重算和单来源同步均为异步任务，接口返回 run id / version id / trace id，前端通过列表或详情轮询状态，不得假装同步完成。

目标画像页的学习来源保存、来源同步 / 历史拉取、质量规则保存、样本状态调整、画像重建 / 清空、学习开关调整、版本恢复和候选重算必须绑定当前动作 key、写请求序号和发起时 payload 签名；写动作返回前修改来源选择、质量规则表单、样本处理原因、危险动作原因或触发另一画像动作时，旧响应不得清空当前 loading、覆盖当前错误 / 成功提示或触发旧 payload 的成功刷新。

`GET /api/target-profile` 必须返回当前版本、状态、样本数、来源摘要、质量规则版本、最近重建时间、最近 AI 使用时间和是否可用于 AI 活跃群 / 频道评论。`GET /api/target-profile/usage` 返回正在读取当前画像的运行中任务数、任务类型分布和最近使用记录，用于页面回答“会影响哪些任务”。

`GET /api/target-profile/source-candidates` 用于来源选择器，必须返回运营目标、目标类型、可监听状态、监听账号覆盖、最近消息时间、关联任务类型、推荐原因和不可自动同步原因。推荐来源只能默认高亮，不能默认勾选。

质量规则接口必须支持配置身份过滤、文本过滤、广告模板过滤、质量评分阈值、场景权重和禁学模式。规则变更只影响后续候选计算；如果要影响当前画像，必须显式执行 `recompute-candidates` 和 `rebuild`，并在版本快照中记录 `quality_rule_version`。

### 7.2.2 运营方案

- `GET /api/operation-plans`
- `POST /api/operation-plans`
- `GET /api/operation-plans/{plan_id}`
- `PATCH /api/operation-plans/{plan_id}`
- `POST /api/operation-plans/{plan_id}/generate-preview`
- `POST /api/operation-plans/{plan_id}/generate-tasks`
- `POST /api/operation-plans/{plan_id}/apply-to-linked-tasks`
- `POST /api/operation-plans/{plan_id}/pause`
- `POST /api/operation-plans/{plan_id}/resume`
- `POST /api/operation-plans/{plan_id}/copy`
- `POST /api/operation-plans/{plan_id}/archive`
- `GET /api/operation-plans/{plan_id}/runs`

方案接口必须返回 `plan_type`、`status=draft/active/paused/archived`、覆盖目标、关联任务、最近预览、最近生成结果、阻塞原因和最后应用时间。生成任务、应用关联任务、暂停、恢复、复制和归档都必须写 `operation_plan_generation_runs` 与审计记录。

### 7.3 任务中心

- `GET /api/tasks`
- `GET /api/tasks/page`
- `POST /api/tasks/precheck`
- `POST /api/tasks/group-ai-chat`
- `POST /api/tasks/group-ai-chat/create-and-start`
- `POST /api/tasks/group-relay`
- `POST /api/tasks/group-relay/create-and-start`
- `POST /api/tasks/channel-view`
- `POST /api/tasks/channel-view/create-and-start`
- `POST /api/tasks/channel-like`
- `POST /api/tasks/channel-like/create-and-start`
- `POST /api/tasks/channel-comment`
- `POST /api/tasks/channel-comment/create-and-start`
- `POST /api/tasks/search-click`
- `POST /api/tasks/search-click/create-and-start`
- `GET /api/tasks/{task_id}`
- `PATCH /api/tasks/{task_id}`
- `PATCH /api/tasks/{task_id}/settings`
- `POST /api/tasks/{task_id}/start`
- `POST /api/tasks/{task_id}/pause`
- `POST /api/tasks/{task_id}/resume`
- `POST /api/tasks/{task_id}/stop`
- `POST /api/tasks/{task_id}/retry`
- `POST /api/tasks/{task_id}/reset`
- `GET /api/tasks/{task_id}/membership-items`
- `POST /api/tasks/{task_id}/membership-items/{item_id}/retry`
- `POST /api/tasks/{task_id}/membership-items/{item_id}/recheck-can-send`
- `POST /api/tasks/{task_id}/membership-items/{item_id}/mark-manual`
- `POST /api/tasks/{task_id}/membership-items/{item_id}/skip`
- `POST /api/tasks/{task_id}/membership-items/{item_id}/challenge-answer`
- `GET /api/tasks/{task_id}/actions`
- `GET /api/tasks/{task_id}/actions/{action_id}/attempts`

`GET /api/tasks/page` 接受 `page/page_size/type/status/q/group_key`，默认 `page=1&page_size=20`，最大 `page_size=100`，返回 `TaskListPageOut(items,total,page,page_size,summary,groups)`。普通 Task 与账号安全系统任务必须共同稳定排序、过滤、计数和分页；列表 item 不得包含完整 `account_config/pacing_config/failure_policy/type_config`，完整配置继续由 `GET /api/tasks/{task_id}` 返回。`summary={total,running,failed}` 与 `groups` 均在 `type/status/q` 后、当前 `group_key` 和分页前生成；顶层 `total` 为应用 `group_key` 后的列表总数。旧 `GET /api/tasks` 暂保留兼容，但第一方任务中心列表不再调用。

系统任务投影必须批量聚合账号安全 batch items，不得按 batch 循环读取 items。普通 Task 的目标 / 频道摘要、运行摘要和分组上下文也必须批量加载；服务端分页不能只是先构造全量 payload 再切片。

`POST /api/tasks/search-click` 和 `/create-and-start` 必须复用任务中心通用结构校验、任务创建、任务启动、审计和错误出口。创建阶段只校验调用者同时具备 `tasks.manage + tasks.create.search_click`、`task_type=search_click`、`search_execution_mode=click_only`、公开目标引用、提交账号组引用/用途、每日 click 目标、截止时间和字段结构；join switch、admission 目标或成员目标返回 `422 field_not_allowed_for_click_only`，其余合法请求直接持久化 Task。真实协议样本、button type/effect、授权槽位环境与代理、客户端元数据、订阅/节点、observed exit IP、mutation-key fencing、warmup、关键词矩阵、系统安全额度、decoy、机器人白名单和通知配置均在启动后由系统持续评估、自动选择并写运行 blocker，不要求运营预先确认。纯点击可执行样本必须证明 `membership_side_effect=none`；旧 `join_candidate` 或副作用未知样本只形成运行 blocker，不能触发加入路径。历史解析器样本不迁移到新 Task；运营确认可继续使用的协议必须按当前 schema 重新生成 active `target_open_only + none` 样本与审计。外部 HTTP URL 首版必须跳过并标记 `external_url_requires_web_profile`；代理失败不得回退本机直连；全部启用订阅不可用时不得搜索或点击，并必须通知配置的管理员群消息接收人；decoy 浏览只允许 `button_effect=navigate_only`。旧曲线、`actions_per_round`、skip、jitter 和 quiet-hours 只读，不进入当前候选、claim 或 Gateway；资源空闲即执行。旧 `/api/tasks/search-join-group` 和 `tasks.create.search_join_group` 只作旧 Task 读取、审计与删除识别，旧创建请求固定返回 `410 legacy_search_join_create_retired`，不得规范化或代建当前 `search_click`。专项口径以 `docs/03-feature-designs/search-click-daily-fulfillment-remediation-prd.md` 为准。

任务中心必须支持系统任务投影，账号安全类系统任务至少包括资料初始化、清理登录设备、设置二步密码和备用 session 自动补齐：

| 字段 | 要求 |
| --- | --- |
| `id` | 使用稳定 ID：`account_security_batch:{batch_id}` |
| `type` | `account_profile_init` / `account_device_cleanup` / `account_2fa_setup` / `account_standby_session_provision` |
| `name` | `资料初始化批次 #{batch_id}` / `清理登录设备批次 #{batch_id}` / `设置二步密码批次 #{batch_id}` / `备用 session 补齐批次 #{batch_id}`，可追加操作原因摘要 |
| `status` | 映射为任务主状态：批次 `running/ready` -> `running`，批次 `succeeded` -> `completed`，批次 `failed` -> `failed`，批次 `partial_success/cancelled` -> `stopped`；原始批次状态放在 `stats.batch_status` |
| `stats` | 必须包含 `total_actions`、`success_count`、`failure_count`、`skipped_count`、`pending_count`、`waiting_cache_count`、`running_count`、`batch_status`、`latest_failure_type` |
| `target_summary` | `账号资料初始化 / {total_count} 个账号`、`清理登录设备 / {total_count} 个账号`、`设置二步密码 / {total_count} 个账号` 或 `备用 session 补齐 / {total_count} 个账号` |
| `search_text` | 必须包含批次 ID、账号名、手机号、username、失败原因、头像素材标题和缓存状态 |
| 操作按钮 | 只允许查看、刷新、跳转账号批次详情；不显示启动、暂停、重置、停止、删除等普通运营任务控制 |

`GET /api/tasks/{task_id}` 当 `task_id` 形如 `account_security_batch:{batch_id}` 时，返回系统任务详情。响应在兼容 `TaskDetailOut` 基础上扩展 `account_security_batch`：

```json
{
  "account_security_batch": {
    "batch_id": 12,
    "system_task_type": "account_profile_init",
    "action_types": ["update_profile", "update_username", "update_avatar"],
    "batch_status": "running",
    "avatar_cache": {
      "total": 22,
      "ready": 18,
      "waiting": 3,
      "failed": 1,
      "flood_wait": 0
    },
    "items": [
      {
        "account_id": 11,
        "display_name": "锅巴洋芋",
        "phone_number": "+8613800010000",
        "status": "waiting",
        "profile_status": "succeeded",
        "username_status": "succeeded",
        "avatar_status": "waiting_cache",
        "avatar_source": "material:701",
        "avatar_cache_status": "refreshing",
        "avatar_preview_url": "",
        "device_cleanup_status": "not_required",
        "two_fa_status": "not_required",
        "standby_session_status": "not_required",
        "failure_type": "waiting_material_cache",
        "failure_detail": "头像素材仍在缓存中"
      }
    ]
  }
}
```

普通 Task 的 `actions` / `execution_attempts` 接口保持原语义；系统任务投影不伪造 `execution_attempts`。如果需要按账号项展示执行记录，放在 `account_security_batch.items` 或等价分页接口，不把账号维护批次项混成普通运营 action。

`POST /api/tasks/precheck` 仅作为运营显式调用的只读诊断兼容接口，不得由创建向导自动调用、不得成为创建前置，也不得产生可供创建消费的 token。各具体任务创建接口和创建并启动接口必须独立完成结构校验并同时支持：

- 已有目标字段：`target_channel_id`、`target_operation_target_id`、`target_operation_target_ids`。
- 创建专用新目标字段：`target_type`、`target_input`、`target_title`。
- 搜索类极简接口的目标字段：`target_title`、`target_link`；只接受可归一化为公开 Telegram username 的链接，内部 `target_operation_target_id` 仅为服务端持久化结果，不能作为请求字段。
- 返回或写入 `target_resolution`，说明目标是新建、复用、无法解析还是缺少加入入口。
- 只有运营另行显式调用该诊断接口时才可返回 `ready_account_count`、`preparable_account_count` 和 `blocked_account_count`；这些运行事实不得参与创建成功判定，纯搜索点击不返回 membership 预估。
- 创建接口只返回结构错误或已持久化 Task。创建并启动接口在 Task 已持久化后建立运行 ledger，并把协议、传输、容量与 completion risk 写入任务详情；不得因这些运行事实回滚已成功创建的 Task。
- AI 活跃群显式只读诊断可返回 `daily_message_target`、`base/effective_planned_target_preview`、对应revision、`current_required_account_count_preview`、`eligible/recovering/abandoned/completed`、兼容`planned_daily_target_preview/effective_daily_target_preview`、DueSet/open/hold/bound on-time/late/unproven、settlement、`quantity_overflow_count`、`scope_fact_version/refreshed_at`、`timezone_snapshot_preview`、Generation/interaction 实际空闲与执行槽、每任务 Planner/Generation/Dispatcher 实际并发数、准入/传输 blocker 和 completion risk；只读预览字段不得接收用户保存。
- 频道浏览的`daily-fulfillment`摘要返回source/target/DueSet/MaterializedSet/on-time/late/unproven/unknown/structural shortfall/settlement与enrollment/route/read-model versions；targets与due-units使用独立keyset端点。cursor签入tenant/task/ledger/enrollment/route epoch/read-model、规范filters/limit/order/last key，每页repeatable-read校验，漂移409，禁止把全量unit塞进Task详情或用`Task.stats`缓存作权威。
- 履约策略页读取/修改 Provider capacity、search execution、challenge safety、listener freshness、generation/recovery lease 和 fulfillment metrics policy。Provider capacity 只允许一个 active `ai_provider_key_version`，展示该 key 的全局 `max_inflight/RPM/TPM` 与各 model 可选子上限；Job 原子取得 key 总 token 及适用的 model 子 token，不得按模型重复计算 key 总额度。`GET`复用现有`system.view`，`PATCH`复用现有`system.manage + approval_ref`，middleware与handler/service双检，必填`expected_revision + change_reason`；修改/回滚都创建新不可变revision并写before/after审计，旧版本不物理删除。
- AI 活跃群创建和配置更新必须支持 `reply_min_per_round` 或等价字段，表示每轮最少 Telegram 原生引用回复数；发送门禁删除、签到兜底和素材选择不得改写该配置或已规划引用槽位。
- 频道评论 / 回复创建和配置更新必须支持 `reply_min_per_message` 或等价字段，表示每条频道消息本轮补计划时最少 Telegram 原生引用回复数；Unicode 或图片表情包兜底都必须保留原 direct/reply 关系。

引用回复功能对任务中心的功能影响：

| 功能面 | AI 活跃群影响 | AI 评论 / 回复影响 |
| --- | --- | --- |
| 创建向导 | 任务配置步骤新增“每个逻辑 Cycle 最少引用回复数”，默认 1；字段说明必须写明该值包含在群日目标内、不额外增加总发送量，Cycle Turn 较少时实际取较小值；无合格候选时明确显示引用短缺 | 任务配置步骤新增“每条频道消息最少引用回复数”，默认 1；字段说明必须写明该值包含在单条消息补差额内，不额外增加总评论目标；无合格候选时明确显示引用短缺 |
| 编辑任务 | 保存该字段后属于会影响后续规划的配置变更；未来未执行主互动 action 需要按既有重排规则处理 | 保存该字段后只影响未来未执行 / 未规划的频道消息补差额；已成功评论和历史 action 不回滚 |
| 创建确认 | 只确认用户输入和结构合法性；可引用消息数量、引用不足、准入和容量在任务创建后由详情页展示，不阻止创建 | 只确认用户输入和结构合法性；已采集评论、引用不足和讨论区能力在创建后由详情页展示，不阻止创建 |
| 前端校验 | `reply_min_per_round` 必须为正整数；实际合同取 `min(configured, logical_cycle_turn_count)`，不得通过增大发送量满足配置 | `reply_min_per_message` 必须为整数且不大于每条评论 / 回复目标；非法时阻止提交，不自动改值 |
| 来源选择 | 不展示引用来源选择器，不展示具体消息多选；只显示可引用消息估算和 warning | 不展示引用来源选择器，不展示具体评论多选；只显示可回复评论估算和 warning |
| Action payload | `send_message` payload 增加引用关系字段：`reply_to_message_id`、`reply_target_label`、`reply_target_author`、`reply_target_preview`、`reply_target_source` | `post_comment` payload 保持 `reply_to_message_id`，并补齐引用关系字段：`reply_target_label`、`reply_target_author`、`reply_target_preview`、`reply_target_source` |
| 任务详情 | AI Cycle / Turn 明细展示普通发言和引用回复类型、引用对象、引用预览、远端消息 ID 和失败原因 | 频道消息子任务聚合中展示直接评论数、引用回复数、引用不足原因；Action 明细展示引用评论摘要 |
| 审计和排障 | 配置变更、引用回复规划不足、引用 payload 配置错误和 Telegram 回复失败必须可追踪 | 配置变更、讨论区评论采集不足、引用回复规划不足和 Telegram 回复失败必须可追踪 |

当前兼容接口与目标扩展接口必须分层处理：

- 现有兼容链路继续通过普通 `ensure_target_membership` / 历史 `ensure_channel_membership` action 执行准入，并通过 `verification_tasks` 承载验证辅助事项；已可自动执行的动作是 `关注频道`、`点击按钮`、`发送验证回复`。
- `GET /api/tasks/{task_id}/membership-items` 当前必须作为兼容投影接口可用：从准入 action、`verification_tasks` 和执行尝试聚合出账号级准入明细，支持分页、阶段筛选和人工处理筛选；不得受任务详情通用 action 展示上限影响。
- `POST /api/tasks/{task_id}/membership-items/{item_id}/...` 是正式落库 `target_membership_items` 后的目标操作接口；在落库前前端不得假调用这些操作，只能展示兼容投影和现有验证任务状态。
- 文本问答、简单算数题和图片验证码在目标设计中必须支持规则 / AI / 多模态视觉辅助，但在网关未返回稳定题面、图片、答案输入位置、置信度和 `question_hash` 前，只能创建 `人工处理` 或可审计的验证辅助任务，不能伪装成已自动处理。
- 前端展示应区分“已自动处理并复检可发言”“已生成验证辅助任务待处理”“目标扩展接口待实现”三类状态，避免运营人员误判任务已经具备完整多模态图片验证码 / 算数验证能力。

`POST /api/tasks/channel-view` 和 `POST /api/tasks/channel-view/create-and-start` 的 `type_config` 必须支持帖子级产量字段：

| 字段 | 说明 |
| --- | --- |
| `initial_message_scope` | 初始帖子范围：`latest_n`、`today_new`、`date_range`、`specific`、`new_only` |
| `latest_message_count` | `latest_n` 时的最近帖子数量，例如 10 |
| `listen_new_messages` | 是否持续监听任务启动后的新帖 |
| `per_message_daily_view_target` | 每条帖子每日浏览量 |
| `per_message_total_view_target` | 每条帖子累计浏览软目标；`0` 表示无上限；有限值允许按已冻结的当日批次粒度超额，达到或超过后下一任务日 target 的 effective 值为 0，不产生 due/Action |
| `message_active_days` | 帖子继续累计新due的有效期；超过后冻结accrued due，既有TargetSet/DueSet不删除 |
| `task_daily_view_safety_cap` | 系统统一任务级异常门禁，固定 `1_000_000`，不允许截断逐消息目标 |
| `max_views_per_account_per_day` | 系统统一任务内账号异常门禁，固定 `1_000_000`；跨 Task 的同日同账号同帖唯一性由 `ChannelViewDailyIdentityOwner` 独立保证 |

频道浏览创建接口直接校验输入结构；可选只读诊断可返回初始帖子数量、持续监听状态、预计当天最大浏览 action、单帖目标、统一任务门禁和账号容量缺口，但不能成为创建前置。接口把任何任务日或任务内账号软上限输入直接规范化并持久化为 `1_000_000`，不因低值拒绝创建；外部账号容量不足或 dynamic_new 后续新增消息形成typed structural blocker，不回滚已创建Task、不缩减逐消息target/due。initial source set在ledger首次规划冻结，动态来源只append；编辑只影响新source/next ledger revision，不能静默重算current TargetSet。current ledger重建只能走受保护manifest/readback并保留历史。

`GET /api/tasks/{task_id}` 和任务列表轻量投影必须提供统一履约字段：

```text
fulfillment.target_count
fulfillment.target_set_hash
fulfillment.due_target_count
fulfillment.due_set_hash
fulfillment.materialized_count
fulfillment.materialized_set_hash
fulfillment.materialization_gap_count
fulfillment.confirmed_count
fulfillment.late_confirmed_count
fulfillment.held_count
fulfillment.unknown_count
fulfillment.terminal_shortfall
fulfillment.failed_attempt_count
fulfillment.remaining_count
fulfillment.planning_deficit_count
fulfillment.quantity_overflow_count
fulfillment.open_excess_count
fulfillment.projected_capacity_before_deadline
fulfillment.structural_capacity_shortfall_count
fulfillment.structural_capacity_shortfall_reasons
fulfillment.source_state
fulfillment.source_projection_version
fulfillment.active_source_set_hash
fulfillment.invalid_binding_count
fulfillment.deadline_at
fulfillment.task_day_ledger_id
fulfillment.timezone_snapshot
fulfillment.timezone_revision
fulfillment.period_start_at
fulfillment.day_phase
fulfillment.quantity_status
fulfillment.content_mix_status
fulfillment.acceptance_status
fulfillment.status  # 迁移期兼容别名，固定等于 acceptance_status
fulfillment.blocking_codes
fulfillment.calculated_at
```

详情接口必须提供按业务粒度分页下钻入口：自然日任务先按 `task_day_ledger_id`，AI 再按账号，评论/点赞/浏览再按频道消息，纯搜索点击再按 `SearchClickObligation.id`。AI/评论另返回 `content_mix.contract/planned/success/shortfall/overflow/fallback/obligations_by_source`；`obligations_by_source` 分列 `policy_min|selector_plan`，合并 planned 时同一逻辑槽位不得重复计数。只有 quantity 和 content_mix 均 met 时 acceptance 才 met。上述字段从持久 Action、ExecutionAttempt、任务专用账本、内容义务和远端事实派生；`Task.stats` 仅是可重建缓存。

`GET /api/tasks/{task_id}/daily-fulfillment` 以 `task_day_ledger_id` 为权威查询参数。兼容 `date=` 仅在该本地日期唯一映射一份 ledger 时解析；命中多份时返回 `409 ambiguous_task_day_ledger` 和候选 `id/timezone/period/day_phase`，禁止合并。频道浏览target唯一`(task_day_ledger_id,target_peer_id,channel_message_id)`，due unit唯一`(daily_message_target_id,due_ordinal)`；账号是materialization绑定，累计目标继续独立保存。浏览下钻同时返回source/target revision、DueSet/MaterializedSet hash、账号slot与shortfall reason。

`PATCH /api/tasks/{task_id}/settings` 不接收 `target_input`、`target_title` 或创建专用 `target_type` 字段。编辑任务只能使用已有目标 ID 和已有配置字段，避免编辑弹窗隐式创建新运营目标。

`GET /api/tasks/{task_id}` 或任务详情聚合接口仅对已设计准入的 AI 活群返回准入子任务摘要；纯搜索点击不得返回或创建 membership/admission 子任务：

- `membership_subtask.status`。
- `membership_subtask.progress_percent`。
- `membership_subtask.estimated_finish_at` 或 `membership_subtask.estimated_remaining_seconds`。
- `membership_subtask.ready_account_count`、`pending_account_count`、`running_account_count`、`success_account_count`、`failed_account_count`、`blocked_account_count`。
- `membership_subtask.current_phase` 和 `membership_subtask.warnings`。
- 账号级准入明细或可分页查询入口。
- AI活跃群额外返回typed`ai_daily_summary`，包含`task_day_ledger_id/target_set_hash/targets[]/enrollment_epoch/route_epoch/read_model_version/as_of`、时区/period/deadline及全target-set aggregate；每个target含operation/revision、配置目标、DueRankSet、coverage/extra的open/generation-pending/check-in-handoff-pending|claimed/ready/confirmed/call-issued/unknown/waiting/shortfall/lifecycle-cancelled与projection lag。另返回scope状态、Generation/interaction空闲槽及最近plan/assignment/intent/job/variation/handoff/ready Action漏斗；不返回Task份额、预扣或legacy Cycle漏斗。义务和尝试历史使用专项签完整normalized query的typed cursor API。
- 任务详情配置区返回 `config_effective_at/current_ledger_unchanged/current_scope_revision/pre_gateway_replan_scope` 或等价字段：timezone/手工群日目标显示从当前 ledger deadline 后生效；账号范围显示 current task-day 动态 scope revision。只有新 revision 实际管辖且尚未 call-issued 的 stable unit 才可类型化转换/取消；Gateway/unknown/confirmed 不改写。

`GET /api/tasks/{task_id}/membership-items` 或等价分页接口必须支持准入账号级详情：

- 分页和过滤：`status`、`phase`、`account_id`、`failure_type`、`manual_required`。
- 返回账号基础信息、目标、当前阶段、最近计划时间、最近完成时间。
- 返回入群、关注频道、验证、可发言复检的阶段记录。
- 返回验证聊天读取状态、消息数量、读取失败原因、验证问题、图片消息 ID、媒体摘要、AI / MiMo 答案、置信度、模型、结果、原始错误和处理建议。
- 支持操作入口所需的 item id：重试准入、重新检测可发言、标记人工处理、跳过账号。

`GET /api/verification-tasks/{task_id}/challenge-context` 必须作为验证聊天读取的权威接口：

- 返回 `context_status`：`ok`、`empty`、`read_failed`、`stale`、`target_inaccessible`。
- 返回 `target_peer_id`、`target_display`、`account_id`、`last_read_at`、`message_count`、`read_failure_detail` 和 `messages`。
- `empty` 表示接口调用成功但没有读到机器人 / 管理员验证消息；前端必须展示“没有读取到最近验证聊天信息”和处理建议，不能只显示空态。
- `read_failed` 或 `target_inaccessible` 必须展示 Telegram 原始失败摘要，例如目标不可访问、账号无权限、session 失效或 `GetHistoryRequest` 失败。
- 返回的消息如果包含媒体，必须带媒体摘要；图片验证码自动识别只能基于该接口返回的当前有效媒体消息。

准入账号级操作接口必须满足：

| 接口 | 权限 | 行为 |
| --- | --- | --- |
| `POST /api/tasks/{task_id}/membership-items/{item_id}/retry` | `tasks.membership.manage` | 只重排该账号未完成或失败的准入阶段；必须要求原因；如果存在同一验证问题且已自动尝试过，必须返回需要人工确认或新问题证据 |
| `POST /api/tasks/{task_id}/membership-items/{item_id}/recheck-can-send` | `tasks.membership.manage` | 创建可发言复检 action；成功后更新 `target_membership_items.can_send` 和 `task_ready_accounts` |
| `POST /api/tasks/{task_id}/membership-items/{item_id}/mark-manual` | `tasks.membership.manage` | 标记人工处理，写 `manual_required=true`、原因和操作者；不自动生成 TG 动作 |
| `POST /api/tasks/{task_id}/membership-items/{item_id}/skip` | `tasks.membership.manage` | 跳过该账号本任务准入，写 `disabled_reason`，并从父任务 ready pool 移除 |
| `POST /api/tasks/{task_id}/membership-items/{item_id}/challenge-answer` | `tasks.membership.challenge.handle` | 提交人工验证答案或确认允许重新 AI / MiMo 尝试；必须写 `target_membership_challenge_attempts` 和审计 |

上述接口只能操作当前 `task_id` 下的 membership item；跨任务批量处理必须先通过运营目标页筛选后逐项写审计，不能用一个接口静默修改多个父任务。

`GET /api/tasks/{task_id}/actions` 必须支持：

- 分页：`page`、`page_size`。
- 过滤：`status`、`action_type`、`account_id`、`failure_type`、`scheduled_from`、`scheduled_to`。
- 排序：默认按 `scheduled_at` 或最近更新时间倒序。
- 返回：action 基础字段、payload 摘要、result 摘要、failure_type、skip_reason、是否 `unknown_after_send`、是否已上卷运营异常。
- 引用回复 action 必须返回引用关系摘要：`reply_to_message_id`、`reply_target_label`、`reply_target_author`、`reply_target_preview`、`reply_target_source`、Telegram 远端消息 ID 和执行失败摘要。任务详情不能只展示“发送 / 评论”而隐藏该 action 是否为引用回复。

`GET /api/tasks/{task_id}/actions/{action_id}/attempts` 必须只返回该 action 的执行尝试，不能顺带加载整任务全部 attempt。

### 7.4 规则、监听、数据

- `GET /api/listeners/summary`
- `POST /api/listeners/{object_type}/{object_id}/switch`
- `POST /api/listeners/{object_type}/{object_id}/reset-watermark`
- `GET /api/listeners/{object_type}/{object_id}/events`
- `GET /api/listeners/{object_type}/{object_id}/errors`
- `GET /api/rule-sets`
- `POST /api/rule-sets`
- `PUT /api/rule-sets/{rule_set_id}/config`
- `POST /api/rule-sets/{rule_set_id}/versions/{version_id}/publish`
- `POST /api/rule-sets/{rule_set_id}/versions/{version_id}/copy`
- `POST /api/rule-sets/{rule_set_id}/versions/{version_id}/rollback`
- `POST /api/rules/test`
- `POST /api/tasks/{task_id}/source-filter-overrides`
- `GET /api/operation-metrics/summary`
- `GET /api/operation-metrics/reports`
- `POST /api/operation-metrics/export`
- `GET /api/operation-issues`
- `GET /api/operation-issues/{issue_id}`
- `POST /api/operation-issues/{issue_id}/acknowledge`
- `POST /api/operation-issues/{issue_id}/resolve`
- `POST /api/operation-issues/{issue_id}/ignore`
- `GET /api/reports`
- `GET /api/archives`
- `POST /api/archives`
- `GET /api/archives/{archive_id}`
- `POST /api/archives/{archive_id}/rerun`
- `POST /api/archives/export`

`GET /api/overview` 和 `GET /api/operation-metrics/summary` 默认返回汇总读模型，不返回 action / attempt 明细。

`GET /api/operation-issues` 必须支持：

- 默认按 `target_id` 聚合。
- 过滤：`target_id`、`issue_type`、`severity`、`status`、`failure_type`。
- 返回：目标摘要、影响任务、影响账号、代表性失败码、建议动作、最近更新时间。
- 每个建议动作必须返回 `handling_mode`：`modal`、`drawer` 或 `deep_link`。
- `modal` / `drawer` 动作必须返回弹窗标题、上下文摘要、确认文案、是否需要原因和审计动作类型。
- `deep_link` 动作必须返回目标页面、对象 ID、默认 Tab、默认筛选和 `return_to` 参数。

`GET /api/operation-issues/{issue_id}` 返回异常详情和关联任务失败摘要；查看原始 action / attempt 必须跳转任务中心接口。

确认处理、解决和忽略异常必须写原因、操作者、处理时间和审计；忽略不删除任务中心失败事实，只改变运营中心处理状态。需要多人协作时使用 `claimed_by / claimed_at` 标记处理负责人，不能把 acknowledge 当成责任人字段。通过上下文弹窗 / 抽屉执行的动作也必须写来源 `source_issue_id` 和 `target_id`，方便回溯。

`POST /api/tasks/{task_id}/source-filter-overrides` 只写当前任务的来源过滤 override，不修改已发布规则版本。请求必须携带来源人稳定标识、来源 action、原因和操作者；如果要长期生效，必须走规则版本复制和发布。

归档接口必须分页返回消息、成员和上下文；导出走异步任务或文件生成流程，必须写导出原因、筛选条件和文件标识审计。

### 7.5 风控和系统

- `GET /api/risk-control/summary`
- `PATCH /api/risk-control/global-policy`
- `POST /api/risk-control/preflight`
- `GET /api/risk-control/hits`
- `POST /api/risk-control/dispositions`
- `POST /api/risk-control/dispositions/{disposition_id}/resolve`
- `POST /api/risk-control/restrictions/{restriction_id}/release`
- `GET /api/account-proxies`
- `POST /api/account-proxies`
- `PATCH /api/account-proxies/{proxy_id}`
- `POST /api/account-proxies/{proxy_id}/check`
- `POST /api/account-proxies/{proxy_id}/disable`
- `POST /api/account-proxies/{proxy_id}/bind-accounts`
- `GET /api/developer-apps`
- `POST /api/developer-apps`
- `PATCH /api/developer-apps/{app_id}`
- `POST /api/developer-apps/{app_id}/check`
- `POST /api/developer-apps/{app_id}/disable`
- `GET /api/ai-providers`
- `POST /api/ai-providers`
- `PATCH /api/ai-providers/{provider_id}`
- `POST /api/ai-providers/{provider_id}/check`
- `GET /api/prompt-templates`
- `POST /api/prompt-templates`
- `PATCH /api/prompt-templates/{template_id}`
- `POST /api/prompt-templates/{template_id}/publish`
- `GET /api/materials`
- `GET /api/materials/{material_id}`
- `POST /api/materials/upload`
- `POST /api/materials/upload/batch`
- `POST /api/materials/upload/zip`
- `GET /api/material-imports/{import_id}`
- `PATCH /api/materials/{material_id}`
- `POST /api/materials/{material_id}/disable`
- `POST /api/materials/{material_id}/versions`
- `GET /api/material-groups`
- `POST /api/material-groups`
- `PATCH /api/material-groups/{group_id}`
- `GET /api/materials/{material_id}/references`
- `POST /api/materials/{material_id}/refresh-cache`
- `GET /api/materials/cache/config`
- `PATCH /api/materials/cache/config`
- `GET /api/audit-logs`
- `GET /api/audit-logs/export`

`POST /api/materials/upload/zip` 用于素材中心压缩包批量导入，参数至少包含导入类型、目标分组 / 素材包名称、允许后缀和单图大小上限；默认只接受 `.png`、`.jpg`、`.jpeg`，单张图片上限 500KB，返回 `import_id` 后异步处理。`GET /api/material-imports/{import_id}` 返回解析总数、成功、失败、跳过、重复、超过大小限制和逐文件原因。

`GET /api/materials/cache/config` 返回素材缓存频道、源媒体缓存频道、缓存执行账号、当前用户输入、归一化状态、配置来源和健康摘要。`PATCH /api/materials/cache/config` 保存频道链接或 `@username`，后端负责解析 `t.me/c/...`、公开链接和高级 peer id，并写入缓存执行账号 ID 和审计；普通用户页面不得要求直接填写 `-100...`。缓存执行账号为空表示沿用自动候选账号逻辑；非空时必须校验账号属于当前租户且未删除。

素材接口归属 `materials.*` 权限；AI 供应商、提示词和黑话配置归属 `ai.*` / `prompt_templates.*` / `system.manage`，不通过素材接口保存。代理接口归属风控中心和 `proxies.manage`，系统设置只展示运行配置和健康提示。

---

## 8. 验收标准

### 8.1 产品验收

- 主流程只围绕账号、运营目标、目标画像、规则、风控、任务、执行、数据和审计。
- 目标画像必须是全站唯一画像；AI 活跃群、频道评论和频道评论回复读取同一份生效版本，任务页不能选择另一份画像。
- 目标画像必须作为一级页面提供当前画像、学习来源、同步状态、历史拉取、候选样本、质量规则、版本和审计入口。
- 样本质量规则必须可配置过滤规则，覆盖身份过滤、文本过滤、广告模板过滤、质量评分阈值、场景权重和禁学模式；规则变更后必须能重新计算候选样本并显式重建画像。
- 运营目标详情不能承载画像版本、样本治理、重建、清空或质量规则配置，也不能直接编辑目标风控策略；只展示画像来源状态、风险状态和跳转入口。
- 目标画像旧数据按放弃处理；旧 `target_id + profile_scene` 数据不迁移、不合并、不兼容，旧画像接口不进入新前端主流程。
- 运营中心必须作为运营人员日常入口，默认按目标展示异常、效果和方案状态；点开目标后展示关联任务失败。
- 运营中心建议动作必须优先支持上下文弹窗 / 抽屉处理；复杂流程深链跳转时必须携带 `return_to` 并能返回原目标、原筛选和原 issue。
- 运营中心方案模板必须能生成任务草稿或创建并启动任务；调整方案必须先展示影响预览，不能静默覆盖运行中任务。
- 运营中心必须读取目标级、任务级、账号级和异常级汇总读模型，不把执行明细大表作为首页实时查询来源。
- 任务中心必须保留完整失败事实和执行详情，但不能成为运营人员发现失败的唯一入口。
- 运营目标和任务中心第一方列表必须服务端有界：当前生产规模下两个列表各自响应小于 2 秒、单页小于 100 KB；任务创建 / 编辑弹窗 2 秒内可操作，目标候选随后远程加载。
- 任务中心列表的普通 Task 与账号安全系统任务必须共同稳定分页、计数、统计和分组；列表 item 不返回四类完整 config，系统批次列表不得按 batch 形成 items N+1。
- Overview、Rules、Archives、MessageSending 和 AppShell 不得继续通过全量运营目标读取完成当前页摘要、下拉选择或定点查找；所有第一方消费者必须使用显式分页、按需搜索或 ID / 关联群定点查询。
- 系统设置只维护平台底座能力，不编辑具体运营方案、任务节奏、素材日常资产或目标异常处理。
- 素材中心必须作为一级运营资源入口，支持表情包、头像包、图片、文件和组合消息的批量上传、分组、缓存状态和引用关系。
- 素材运行配置必须允许普通管理员填写缓存频道链接或 `@username`，系统自动解析和保存运行所需缓存目标；不得把 `-100...` peer id 作为唯一可用输入。系统设置必须提供“缓存执行账号”选择器，支持按手机号、备注名和 username 搜索并保存为运行层优先账号。
- 前端所有可见按钮必须有后端接口或明确的只读行为。
- 按钮级权限矩阵必须覆盖所有主按钮、危险动作和敏感查看；前端隐藏按钮不能替代后端权限校验。
- 接口清单必须标明当前兼容接口和目标扩展接口；前端重构不得调用文档未定义且后端不存在的隐式接口。
- 所有任务类型的创建都不得以运行资源预检、远程能力探测、容量检查或风险确认作为前置；结构校验通过后必须先直接创建成功，启动后再建立 ledger、冻结范围并持续展示运行 blocker。创建向导切换步骤、提交前和后端 create builder 均不得调用 `/tasks/precheck` 或任务类型专属运行预检；该接口只允许保留为显式只读诊断或编辑页人工触发的数量建议，返回值不得阻止创建、覆盖用户已填值或改变提交 payload。
- 任务创建向导必须按当前主任务类型动态展示字段，不能用一套泛化表单隐藏关键业务差异。
- 任务创建必须支持选择已有目标，也支持直接粘贴群聊 / 频道入口并自动创建或复用运营目标。
- 任务创建不能只允许选择已关注 / 已加入账号；必须允许选择账号范围。已满足、可准备、不可准备三类准入/容量只在创建并启动后的详情或可选只读诊断展示，不作为创建前置。
- AI 活跃群和频道评论 / 回复创建页必须按各自数量合同推荐配置；AI 活跃群只推荐每群每日发送量并预览当前任务合格账号数、动态生效目标和 24 小时非零分布权重。legacy 频道评论继续显示每条消息累计目标；启用 v1 时改为预览发布时间起三天、stable eligible/execution-ready、55%～65% effective 与离散 actual bps、最近 30 天消息到达量、三天重叠需求、必填 Daily Cap、公平容量缺口和三开关完整性。任务内账号软上限固定 `1_000_000`，不得截断 v1 required distinct。
- AI 活跃群和频道评论 / 回复创建页必须区分“推荐值”和“用户手动值”。账号范围变化后只能覆盖未被手动修改的字段；已手动修改字段只展示新推荐和差异提示。
- AI 活跃群和频道评论 / 回复编辑页不得静默改动运行中任务的数量配置；必须通过“一键应用推荐”或用户手动保存后生效。
- 前端必须明确展示“AI 活跃群按任务日自然曲线推进，当前到期义务在 Generation/interaction 真实资源空闲时执行；晚启动、暂停恢复或容量不足不会突发补齐”；批次上限只代表当前真实执行槽，不是单账号额度、任务份额或完成门禁。
- 频道任务和群聊任务必须在主互动前检查准入状态；频道未关注账号先关注，频道点赞、频道评论 / 回复都必须在关注成功后才执行，转发源群只要求已加入 / 可读取，AI 活跃群和转发目标群必须加入且可发言，成功后才进入主互动。AI 活跃群和转发目标群准入前置必须作为任务中心可见子任务运行。
- 任务中心列表必须展示 AI 活跃群和转发目标群的准入前置进度摘要，至少包含“加入账号前置任务”、已可发、待准备、验证 / 人工处理、失败和预计完成；详情页继续提供账号级分页、验证记录和失败原因。
- 任务中心列表和详情必须展示AI活群、频道浏览、点赞、评论的今日账号参与覆盖比例。AI任务日冻结scope identity与ledger边界，但required set按权威`scope_fact_version`动态join/abandon并推进effective target revision；online/session/proxy/mask/membership/can_send暂态进入recovering且不得缩小分母，只有无合法恢复路线的权威abandon才退出当前required set并保留审计。任何分母都不得被`max_concurrent`、批次或容量扫描截断。
- 准入准备必须覆盖任务账号配置选中的全部在线账号；`max_concurrent`、每轮发言量、账号冷却和健康权重只能影响主互动规划，不能导致大量账号不进入入群 / 关注准备。
- AI 活跃群运行中必须让任务账号范围内所有 active 且可登录账号持续进入在线保活池；平台全局保活开启时，所有 active 可登录运营账号都必须进入保活池。任务详情必须展示应在线、当前在线、warming、recovering、需重登、阻断、stale 和掉线原因，发送量不足时不能只展示泛化失败。
- 普通 supergroup 成员的权限探测必须正确处理缺失 `send_messages` 字段；缺字段不能直接判定不可发言。只有实际禁言、账号不在群、默认禁言、API 明确失败或 TG 返回的账号级限制才能标记不可发言。
- 任务详情必须以顶部摘要 + Tab + 二级弹窗 / 抽屉展示准入前置子任务的状态、预计进度、预计完成、容量统计、账号级结果、验证记录和失败原因，不能继续在一个弹窗内平铺所有表格。
- 准入前置不能阻塞已满足账号执行主互动；已满足账号先执行，准入成功账号必须动态追加进入主互动 ready pool 并在后续轮次分担发言。
- 任务暂停、启动中、准入补齐中、等待 AI、等待上下文、等待冷却、等待下一轮和发送中必须在任务列表、任务详情顶部和运营中心关联任务摘要中显著展示；`paused` 不能只显示为弱标签。
- 文本问题、简单算数题、固定问答必须由规则 / AI 辅助尝试处理；图片验证码必须由健康的多模态视觉模型尝试处理。失败、低置信、人工审批和等待管理员审批必须作为可见状态留痕。
- 群聊准入运行时发现账号已加入但不能发言时，必须按失败证据生成验证辅助动作：关联频道缺失归为 `关注频道`，机器人按钮归为 `点击按钮`，明确要求发送验证回复归为 `发送验证回复`；无法自动判断、管理员审批和低置信题面必须保持 `人工处理`，不得伪装成自动成功。
- AI 供应商读超时、空响应或无健康供应商必须作为 AI 运行不可用展示，不得被归因为账号权限、图形验证码或目标准入异常。
- 机器人 / 图形验证码诊断必须可刷新；当当前群成员和最近消息不再支持该诊断时，账号详情、任务详情和运营异常必须清理旧文案并展示最新原因。
- 频道评论/回复必须把“账号未准入”“账号已准入但不可评论”“频道消息本身无法评论”“其他 TG/API 原始错误”拆分展示。未准入先补关注 / 加入并延后评论；消息不可评论才展示“该消息无法评论”；账号级权限问题只影响该账号，不得关闭整帖。
- AI 活跃群必须优先接真人上下文，只有在空闲场景才低频暖场；重复风险高、事实无锚点或上下文不足时应沉默并留痕。
- AI 活跃群不得再有每轮 10 条、每小时最大发送量或每小时轮数这类业务完成上限；五类履约任务的通用小时软上限固定为 `1_000_000` 且不对运营展示，纯搜索点击的日软上限亦固定为 `1_000_000`。群日/每消息/每日点击欠额持续保留，单批规划上限、账号安全速率、Telegram SlowMode/FloodWait 和共享在途容量只决定当前能安全执行多少及何时重试，不得降低目标或写成已完成。
- AI 活跃群参与账号比例必须按多轮滚动窗口统计，不能被实现成每轮固定 80% 账号参与，也不能反向抬高本轮计划发言数。
- 频道评论 / 回复必须按每条频道消息累计目标补差额，不能每次 Planner 运行都重新满额生成；多消息同时运行时必须按小时预算分配。
- AI 活群、评论、点赞、浏览和搜索点击必须统一展示 target、confirmed、held、unknown、remaining、deadline、容量和履约状态；只有真实远端确认可以增加 confirmed。
- 动态评论任务不能再因 `max_total_comments` 达到而提前完成；finite batch 必须逐消息达标。reaction unavailable 不得计入点赞成功或直接关闭整帖。评论/点赞/浏览的任务级及任务内账号级软上限固定 `1_000_000`，低值直接规范化且不得截断履约。
- 搜索 repeat 不得绕过账号全局、关键词、授权、代理、协议或 Telegram 硬安全额度；创建阶段不计算理论容量或拒绝目标，启动后由系统按实时路径排序、持续替换失败路径并展示真实 blocker，不能通过预建 Action 冒充可达或完成。
- `Task.status=running` 与 `fulfillment.acceptance_status=blocked/at_risk/missed` 必须可以同时展示；兼容 `fulfillment.status` 等于 acceptance，运营人员能够从 blocker 下钻到配置、账号、消息、协议或远端事实。
- 账号资料初始化必须支持整批 AI 预览、手工编辑和本地兜底。
- 账号资料初始化批次必须进入任务中心可见状态；后台 worker 运行时不能只在账号中心显示“已提交”而无法追踪执行进度。
- 头像更新必须等待素材中心头像包完成 TG 缓存；未缓存完成的头像不能用于更新资料，缓存进度和失败原因必须在任务中心详情可见。
- 账号头像更新成功后必须在账号列表、账号详情、资料初始化批次详情和后续资料同步中回显新头像。
- 设置二步密码和清理登录设备必须是独立入口，不能和资料初始化混在同一个默认动作里。
- 操作手册必须同步展示最近更新功能，并和前端真实菜单、按钮和异常处理口径一致。
- 规则绑定必须使用已发布版本。
- 高风险操作必须写审计。

### 8.2 技术验收

- Planner 幂等，重复运行不重复生成 action。
- Dispatcher 多 worker 下同一 action 不重复执行。
- Planner/Dispatcher PostgreSQL 并发测试必须覆盖真实短事务边界并证明无 deadlock；claim 热事务不得更新 `Task.stats` 或任务履约账本。
- `ACTION_CLAIM_LIMIT`、`DISPATCHER_CONCURRENCY` 和 `DISPATCHER_SCOPE_CAPACITY` 必须有独立配置和测试语义；scope capacity 不得高于有效 worker 总槽位、数据库回写预算或 Gateway 安全在途预算。
- 同一账号不被并发滥用。
- Redis 不可用时不 fail-open。
- 运营目标有界查询必须先分页目标，再只对当前页关联群做 SQL 条件聚合；测试必须证明没有全量 `TgGroupAccount` ORM 物化和逐目标 N+1。
- `/api/tasks/page` 必须在普通 Task 与账号安全系统任务共同集合上执行服务端过滤、稳定排序、total、summary、groups 和分页；不能先构造全部 `TaskOut` 再切片，也不能按系统 batch 逐条加载 items。
- 有界列表、count、关联摘要、`ids/target_ids/linked_group_id/account_id` 查询都必须租户隔离；非法参数显式失败，不得回退无界读取。
- 公共前端 15 秒 timeout 保持不变；分页、搜索、分组、60 秒轮询和目标远程回显必须用请求序号阻止旧响应覆盖新状态。
- 后台登录验证码 token 在 Redis 模式下必须依赖原子消费；Lua / Redis 执行失败时必须 fail closed 并返回可见错误，不能回退到非原子读写导致 token 可被竞态重复使用。
- Telegram 调用结果未知时进入 `unknown_after_send`，不自动重发。
- 统一 fulfillment 投影必须满足 `remaining_count=max(target_count-confirmed_count,0)`；held、unknown、failed、skipped、unavailable 均不得增加 confirmed。
- `moderate_6h` 等 pacing 模板的所有 Action 必须位于自身 deadline 内；operation curve 只能在窗口内分配，容量不足时显式 `pacing_capacity_insufficient`。
- AI generation 和 action payload / result 必须记录接话 / 暖场 / 沉默模式、事实锚点、语义簇、重复风险、幻觉风险和跳过原因。
- AI活跃群同一账号滚动10天内归一化文本、语义簇、模板壳句或同事实观点重复必须为0；重复拦截覆盖该账号跨任务、跨群、跨面具版本的并发GenerationJob、已accepted variation/message reservation、ready/执行中Action和Gateway前最终检查。其他账号历史不得硬阻断当前账号。
- 候选不足时Generation记录质量失败并按账号面具、禁用语义和新`content_variation_key`换角度；同一active Provider key下主/备用各最多3轮仍无候选时，只有coverage已完成、有active/usable面具的direct extra-volume由Generation写immutable handoff并转`check_in_ready`，Planner消费后才可创建精确`签到`ready Action。未完成coverage只有`mask_missing` Planner分支；reply/强引用/material不得降级。签到不进入normal 10天去重，也不得消费未携带素材配额。
- AI 活跃群账号级消息记忆查询只读取重复判定所需轻量字段，并由 `(tenant_id, account_id, status, planned_at DESC)` 或等价索引支撑；同一个 AI generation 批次按涉及账号分别装载滚动 10 天窗口，本批同账号已接受候选立即进入该账号快照。禁止逐 slot 重复全量读取或加载无关大字段；跨账号聚合只用于多样性软提示。
- AI 活跃群 Action payload / result 必须记录消息记忆命中、10 天边界、`account_mask_id/lineage_id/version/contract_version`、`mask_snapshot_hash`、`profile_match_score` 和 `profile_match_reason`。
- AI 活跃群归一化、文本指纹、语义簇和模板壳句 key 在 Planner 与 Dispatcher 中必须一致；相同输入在重复运行中必须得到相同去重结果。
- AI活跃群normal body必须由Generation在accepted variation后先原子CAS账号级message-memory reservation，再在同事务创建ready Action；Planner不得先写normal pending/空正文Action。deterministic check-in只用scoped claim/memory唯一键；并发冲突必须得到typed duplicate原因，不同账号不共用该硬预占键。
- AI 活跃群短时间相同内容问题必须同时覆盖同一轮、本小时、已规划未发送、发送未知和历史成功消息；不能只在 AI Prompt 中提示“不要重复”，也不能只在发送成功后记录。
- AI 活跃群生产质量诊断必须把近 24 小时仍可能继续发送的重复文本作为 release gate blocker；`pending`、`claiming`、`executing` 与已发送 / 发送未知文本构成重复时，必须输出 `AI_GROUP_QUALITY_RECENT_DUPLICATE_GATE_FAILED` 并阻断发布。已 `success` / `unknown_after_send` 的历史重复必须继续输出为 `sent_duplicate_observations`，用于追踪历史质量债，但不能把已不可回滚的历史消息单独作为当前发布 blocker。
- AI 活跃群生产质量诊断还必须检查近 24 小时有效发送 action 的真人感 payload 完整性；仍可能发送或已经作为质量样本的 action 缺少 `account_voice_profile_version`、`ai_message_memory_id`、`human_quality_decision`、`generation_source` 或 `act_type` 时，必须输出 `AI_GROUP_QUALITY_PAYLOAD_GATE_FAILED` 并阻断发布，避免 TG bot / Web 配置未真实进入 AI 讨论链路却被误报为完成。诊断任务还必须在任务快照和 action 样本中输出 `rule_trace.material_intent`、`material_matched_tags`、`material_candidate_count` 和素材选择结果，用于证明 AI 素材意图没有在生成、质量过滤或 action 持久化阶段丢失；同时分列 normal/fallback 的 direct、reply、normal_text_emoji、image、sticker/custom emoji planned/success/shortfall/overflow，证明删除门禁没有重算、稀释或突破原内容占比与冷却。AI 生成提示词只能要求模型输出素材意图和是否允许素材，不允许模型输出素材 ID、素材 URL 或文件地址。
- `ai_group_message_memory.reservation_key` 必须有数据库唯一约束或等价原子锁，重复冲突必须暴露为质量拦截，不得通过查询后插入的竞态窗口放过并发重复。
- AI活跃群Planner/Generation/Dispatcher必须把`tg_account_online_state`作为主互动硬前置；只有`online`且未超过`stale_after_at`的账号才能进入Gateway。`warming`、`recovering`、离线、需重登或session失效记录在线问题，不能归为AI质量不足。代理异常先切换到已验证授权路线，成功后继续原obligation/assignment/intent或ready Action，不因换路线改签到；无路线为`waiting_transport`，不得用表情、泛化短句或直连伪装恢复。
- 在线保活只能做连接、session warm、轻量探测和必要自愈，不得通过目标群可见消息、点赞、关注等动作制造在线证据；探测必须分批、带抖动并落库。
- `desired_online` 必须按全局保活、任务、监听源等来源引用计数维护；任务暂停、停止、删除、账号范围变更和存量任务迁移都必须触发 reconcile，不能留下孤儿在线需求或 stale 在线状态。
- 在线状态必须记录 session 维度，并在专项代理任务中记录授权环境代理维度；普通账号维护和 2FA 不再因账号级历史 `proxy_id` 异常阻断。超过 `stale_after_at` 未成功探测的账号不得继续参与 Planner / Dispatcher，必须转为 warming / offline 并展示最近失败或未探测原因；周期 reconcile 不得把已 stale 的 `online` 状态重新续期。
- 发布迁移后必须为运行中 AI 活跃群、转发任务、监听源和全局保活配置回填 `desired_online` 来源；否则上线后不能把所有账号都当作离线，也不能让缺状态账号绕过在线前置。
- AI 已发送内容默认不得进入正向运营学习画像；实时监听同步、历史拉取和频道评论采集都必须按账号身份排除平台托管账号 / 自身账号 / 机器人发送内容，不能只靠用户名或文案关键词判断。只有人工确认、真实互动效果明确或高质量复用标记的 AI 内容才可低权重进入学习候选。
- 任务详情必须展示 AI 质量漏斗和代表样例，至少覆盖候选数、通过数、重复拦截、模板壳拦截、画像低分、面具低分、事实锚点不足、同批次多样性降权和最终发送数。
- 任务详情必须区分 `duplicate_message`、`template_shell_limited`、`mask_mismatch` / 兼容旧名 `voice_profile_mismatch`、`stance_conflict`、`account_offline`、`context_insufficient`、`quality_fallback`，不能把所有减少原因折叠成 AI 失败。
- AI活跃群current Action payload/result必须记录`obligation_id,content_allocation_plan_id,requirement_assignment_id,content_intent_id+revision,generation_job_id,variation_id nullable,check_in_handoff_id nullable,act_type`、上下文锚点、面具版本/hash、匹配原因、短期立场、消息记忆、语义簇、重写次数和质量结论；legacy`slot_id`仅可nullable只读迁移审计，不能成为必填或current owner。任务详情按这些current identity展示轮次；兼容期可同时保留voice/account mask版本，UI只展示“面具版本”。
- AI 活跃群默认保持一轮批量生成；引入账号面具和消息行为规划后，不得退化为每条消息一次 AI 请求。局部质量失败在同一 active Provider key 下主模型阶段最多 3 轮，随后在备用模型阶段最多 3 轮；不能整轮无限重试，也不能在六轮之外循环补量。
- 同一轮多账号发言必须覆盖不同 `act_type`、账号面具和语义簇；若候选集中出现大比例总结型废话、同类夸赞或无锚点暖场，必须降权、重写或减少发送量。
- 同一账号在同一目标群内的短期立场必须连续；缺少新上下文时，不能在 24 小时窗口内从观望、质疑突然切换为强肯定。
- 账号面具和账号群内短期立场必须以数据库为事实源；Redis 缓存清空、过期或不可用时，系统必须从数据库恢复短摘要和立场，不得随机重建或静默降级为统一口气。
- “账号面具”一级菜单必须提供账号面具管理入口，至少支持搜索、查看、编辑、重建、停用、版本回滚和审计查看；同一账号的面具修改必须影响该账号参与的所有 AI 活跃群任务。
- 账号初始化必须批量生成账号面具；缺面具账号仍可筛选并批量补齐/重建。面具缺失时只有未完成coverage的普通运营账号可走`mask_missing` scoped签到并同时计该原义务的coverage与群日总量；extra-volume保持content capacity gap，不得借签到推进。`disabled/identity_invalid`和非普通运营用途不得绕过身份边界；已有active面具继续使用，签到不停止面具恢复。
- 账号面具初始化提示词必须输出结构化字段、可执行表达原则和禁用表达；生成协议采用每账号一行紧凑 JSONL，服务端保留旧 pipe 行解析兼容；批量结构化输出格式错误时拆成单账号继续请求真实 AI，单账号仍不合格则暴露失败；禁止只生成“自然、随意、真实”等泛化描述；同批面具差异度不足时不得启用。批量补齐 / 批量重建必须返回逐账号结果明细，包含生成状态、版本、差异度、跳过原因和失败原因；批量重建已有 active 面具时必须创建新版本并将旧版本置为 superseded，不能覆盖旧版本或撞唯一约束。
- 面具编辑推进generation-policy/intent revision；current generation_pending job按expected version supersede并由同obligation建立新intent/job，已ready Action按Gateway guard决定继续或由adoption终结。所谓“缺面具时期的open Action重排”仅限final takeover manifest分类后的legacy alias，current mask_missing不会先创建normal Action。
- 账号面具只约束语气、句长、表情习惯、表达偏好和短期立场，不得把价格、位置、时间、真假、服务或体验等业务主题设为每条正文的强制锚点；“说什么”只能由任务话题、讨论对象和真实上下文决定。
- 面具质量门只允许通过或带原因拒绝。拒绝的GenerationJob依次走同一active Provider key下主/备用各最多3轮；六轮仍不合格时，仅coverage已完成且有active/usable面具的direct extra-volume由Generation写handoff，Planner消费后创建精确`签到`，其他写`content_capacity_gap`。`mask_missing` coverage是独立Planner分支。禁止截取AI原文、追加固定尾句或把面具偏好变成正文事实。
- 面具生成、字段校验、active/superseded 版本和回滚逻辑是上游事实，AI 活群不得为了补量修改、重建或降级面具。非日覆盖 Provider 正文通过 style-only 面具门后，Action payload / result 必须写 `voice_profile_contract_version=style_only_v2`；该字段只证明消费者按新合同处理，不产生新的面具版本。
- 历史open Action若记录`voice_profile_anchor_rewritten=true`，或已生成正文但缺当前面具合同证据，只能在takeover final manifest/lifecycle adoption中按可证pre-Gateway identity分类为legacy alias并终结旧Action/memory；Gateway-started/unknown只reconcile。current Planner/Gateway不得原地清正文、释放legacy coverage reservation或重排旧Action；需要推进时由stable obligation与current intent/GenerationJob新建唯一owner。
- 已存在 AI 活群任务在停止后重新启动时，启动器必须先复用统一配置归一化，移除已废弃的 `consecutive_message_*` 与 `auto_follow_required_channel` 字段并采用强制准入合同；这属于启动期迁移与结构校验，不是创建前容量预检。不得因历史 JSON 中保留的旧字段让合法任务无法恢复，也不得借此修改账号面具。
- 必需频道关注完成后，若原群管提示已被删除或移出上下文窗口，确认链路必须从当前被拦截账号的实时窗口寻找同一可信群管新提示。新提示只在“归一化展示名精确匹配 + 同一可信 bot 原消息中的群聊要求链接/按钮”组合成立、且该组合在当前同群待准入账号中唯一时重绑；展示名重名且链接无法区分时写 `recipient_ambiguous`，viewer peer/username/reply relation 作补充审计或消歧证据。重绑后按每个 `requirement_action_key` 建立独立 click，数量不限，每 key 成功一次；单个不可变 fingerprint 只物化该消息有限快照内的 key，重复/等价按钮去重，新要求必须产生新 source/fingerprint/version。禁止把其他账号的相邻提示误归属或在单事务中无界追逐新提示。没有组合匹配时将旧 action 写 `superseded`，从当前 viewer cursor 重新开始连续 30 秒观察；期间出现提示立即重绑，完整 30 秒零提示且零 gap 才通过。读取异常仍保持 `group_bot_confirmation_live_fetch_failed`，不误判 source 已删除或无提示。
- 配置频道完成且已确认在群后，用数据库时间和该账号 viewer cursor 建立连续 30 秒观察。30 秒内无可信群管提示且 `observation_gap=false` 时写 `post_follow_visibility(outcome=no_prompt_30s_passed)` 并视为群机器人验证通过；期间出现提示转 requirement。网络、Session、listener 或 cursor gap 不得当作无提示通过。
- 群管 admission 进入 ready 必须在短事务 CAS `admission_version + observation_version + observed_end_cursor + requirement_set_version/hash`，并同时验证无 observation gap、全部已观察 requirement action success、open/unknown=0、visibility fact 属于同一版本；无提示路径还必须验证数据库时间已达到 `no_prompt_pass_at`。ready 或首条正文 Gateway 前新增可信提示会推进集合/version并使旧 ready 失效；禁止旧 visibility 事实覆盖并发新增要求。
- 群消息高频时，确认源刷新使用独立的最近 300 条控制消息扫描窗口；Gateway 必须先在原始 Telegram 消息上筛选带按钮的候选，再解析发送者与权限，避免把普通聊天和广告全部做重型快照解析。该窗口只用于找到当前账号的可信群管提示，不改变普通 listener 的上下文条数。
- normal AI活群正文使用active账号面具和账号级10天质量管线。`mask_missing`只允许未完成coverage direct由Planner创建签到；normal 3+3耗尽只允许coverage已完成、有active/usable面具的direct extra-volume由Generation写handoff并由Planner消费。两分支均写`content_source=check_in`、精确trigger与scoped claim；数据库保证同`(task_id,group_id,account_id,task_day_ledger_id)`最多一次open/Gateway/unknown/confirmed签到。签到不计高质量AI文本、不进normal去重；reply/material/其他欠量显示`content_capacity_gap`，不得重复签到。
- 频道评论启用 v1.1 后，同样三类触发时按冻结 policy 写 `comment_unicode_emoji_fallback|comment_image_meme_fallback`，从 20 个 Unicode 表情或 ready 图片素材池稳定随机选择，并保留原 channel message、账号和 `reply_to_message_id`。它仍是 `post_comment`，不得转成 reaction；图片默认无 caption。
- AI 活群签到保持其现有合同；频道评论至少启用一种 fallback 类型，两类均启用时显式权重合计 10000，并必须保留正常候选的拒绝原因。授权代理异常只允许切换到已验证路线；没有路线时保持 `waiting_transport`。目标准入、敏感内容、账号用途、unknown 防重和 Telegram 真实结果继续生效；只有非空远端消息/评论 ID 且内容身份一致才成功。
- `ai_group_message_memory` 在线硬去重记录不得早于该行 `dedupe_expires_at` 清理；统一滚动窗口为 10 天。更早记录可保留审计，但不得继续参与硬去重。
- Listener 压力不拖慢发送 action。
- AI prompt 拼装必须分层传入实时事实、任务配置、全站画像、账号画像和规则约束；全站画像不得作为具体事实来源。
- AI 活群 generation worker 必须把当前群短期账号记忆和每个义务的话题、讨论对象、素材意图及表达约束经过安全净化后真正送入 Provider；只写 runtime config、payload 或审计字段但未进入最终 Prompt 视为实现失败。账号资格或 speaker 事实变化时，旧 GenerationJob 只以自身 `job_version` CAS 转 `superseded`，主义务保持 open并建立绑定新账号的新 job；Dispatcher 不同步生成，也不把待生成状态记成业务失败。账号选择不创建 speaker reservation、Redis inflight 锁或 A/B 改绑循环；发送前只依据最新远端事实、义务 identity、账号/授权和 job version 做单行 CAS 复核。
- 当前新 AI 活群 Task 的 `context_expire_after_messages` 缺省为 10；仅用户显式保存 0 才允许关闭消息数阈值。旧合同 Task 不回填、不迁移该字段，按 route fence 后删除。普通群 Listener 必须持久化远端 cursor 连续性，gap/unproven 不得仅凭 `listener_last_polled_at` 放行 normal Provider。
- 普通群 Listener 出现 gap/unproven 后必须从已持久化 cursor 之后正序分页追平；满页保持 unproven，未满页/空页才恢复 contiguous。禁止只读最新窗口导致永久 gap，也禁止合并多个 listener 的重复窗口伪造连续。
- AI 活群任务详情必须同时展示当前任务日的 `quantity_status`、`content_mix_status`、`conversation_quality_status` 和组合 `acceptance_status`；只有三维均 `met` 才能宣称达标。质量 blocker/count 先追加唯一质量事实，再由 projector 以目标行 version CAS 汇总；禁止行锁、跨表合并或后提交覆盖其他 Action 的阻断事实。
- 画像同步、历史拉取、候选重算和重建必须记录 run / version / trace，支持失败可见、部分成功可见和按来源水位恢复。
- 新画像初始化只创建空租户级画像版本，不读取旧目标级画像表作为兼容兜底。
- Recovery 能恢复超时 claim 和 worker 失联。
- Metrics 能展示 pending、executing、失败、延迟和 worker 状态。
- 汇总读模型必须能支撑运营中心和运营数据常规查询；`actions`、`execution_attempts` 等热写事实表只承担短事务写入和下钻查询。
- 数据保留必须区分热写事实、热读汇总和冷归档；超出热数据保留期的明细先汇总或归档，再清理。

### 8.3 测试范围

| 范围 | 用例 |
| --- | --- |
| 账号登录 | 验证码、二维码、2FA、session 失效、重新登录、管理后台登录态过期强制重新登录且不误报 TG 登录失败、主授权登录后自动补齐 standby_1 / standby_2 session、三槽位状态按推导公式生成、任一健康槽位通过官方 code 刷新掉线槽位、救援锁防重复 flow、三槽位全部掉线标识、曾登录账号全部掉线、全部掉线只允许人工重新登录 / 扫码 / 手动验证码、定时检查触发 session 自愈 |
| 接码专用分组 | 系统固定接码专用 `AccountPool` + `account_identity=code_receiver` 双保险、分组和身份不一致时进入接码身份待修复、组内账号只用于提取 Telegram 官方 code、授权资产诊断和备用 session 补齐 / 自愈；禁止资料初始化、改昵称 / TG 姓名 / 简介 / username / 头像、初始化账号面具、设置或轮换 2FA 密码、一键清理其他登录设备、参与任务 / 消息发送 / 监听 / AI 活跃群 / 目标准入 / Planner / Dispatcher / Listener |
| 降权专用分组与搜索排名观察 | 同租户多个启用/禁用 `rank_deboost` 分组、账号新增/迁移原子同步 `pool_id + account_identity`、用途不一致 fail closed、普通任务 all/group/manual 全部排除、降权任务 all 只选择全部启用降权组；分组持久代理绑定复用、SOCKS/HTTP 运行端点、同端点实时出口探测、真实 Telethon 搜索和每 action 最多一次 `navigate_only` 点击、confirmed/明确无点击原因/unknown_after_click 分态、逐点击 reservation、防并发越限、create_and_start 失败不留半创建任务；禁止旧消息、资料初始化、账号面具、2FA、设备清理、Listener 和其他任务使用降权账号。 |
| 资料初始化 | AI 成功、AI 超时、无健康供应商、本地兜底、手工编辑、头像跳过、`custom_prompt` 命名风格、50 账号一次 AI 请求、100 账号快速选择、资料待初始化筛选、需重新资料初始化筛选、备用 session 缺口筛选、健康备用 session 不足 2 个筛选、可激活恢复筛选、整批差异控制、弹窗确认、任务中心状态投影、头像缓存等待、头像回显和失败提示 |
| 设置二步密码 | 未设置账号成功托管、已设置账号跳过、离线账号阻断、失败审计 |
| 清理登录设备 | 新账号首次登录后立即查看/刷新设备和四类计数；App A/B/C 三槽为独立 AuthKey/非零 hash，MY 休眠时远端仍 active；单账号详情展示 current SV 登录时间和不可执行原因并置灰按钮；创建阶段只读数据库，严格覆盖 0h/47:59:59/恰好 48h/超过 48h/登录时间缺失，结果依次为 skipped/skipped/skipped/eligible/skipped，前端时钟不参与；批量返回 requested/eligible/skipped 和逐原因汇总，且零 Telegram 调用；worker 逐账号读取并冻结执行开始时 exact set，单账号读取超时/失败不阻塞整批；一次确认逐 hash 撤销，FRESH 只使当前账号失败且不等待/自动重试；最终 exact-set 证明目标消失、我方保护集完整且无新增 external/unresolved；官方手机/桌面/Web 未匹配我方 hash 时进入清理，接码专用账号仍阻断 |
| TG 官方验证码 | 点击提取后读取并识别 Telegram 官方服务 code 消息，直接展示 code、来源消息时间、有效期、读取授权槽位、原始消息摘要和失败原因；未找到官方消息、无健康 session、读取失败或 code 过期都必须显式展示 |
| 频道任务 | 任务内粘贴新频道、已关注直接互动、未关注前置关注、全部失败阻断主互动、部分成功继续、运行时再次守卫关注状态、未关注点赞 / 评论不得调用 TG 主互动接口而必须先补准入 |
| 频道浏览产量 | 最近 N 条初始范围只纳入 N 条、持续监听只纳入任务启动后新帖、单帖每日浏览量、`0=无上限` 的单帖累计软目标、有限目标允许按当日批次粒度超额、帖子有效期、任务级每日安全上限、当天未完成不滚入次日、跨 Task 同日同账号同帖原子去重 |
| 频道评论异常 | 频道消息 ID 无法解析讨论区、频道未绑定讨论组、账号不可进入讨论组、目标实体无效、异常映射为 `COMMENT_UNAVAILABLE` 或 `PEER_INVALID` |
| 群聊任务 | 任务内粘贴新群聊、未加入先加入、AI 处理入群问题、全部失败阻断主互动、部分成功继续 |
| AI 活跃群质量 | 真人上下文接话、空闲低频暖场、无锚点沉默、语义重复拦截、幻觉事实拦截、多账号角色分工、在线保活准入、掉线原因可见、质量字段留痕 |
| AI 引用回复与素材占比 | AI 活跃群每轮最少引用回复数、频道评论每条消息最少引用回复数、普通消息和引用回复拆分规划；AI 活群只引用本 Task 权威成功我方消息，频道评论保持自身候选合同；引用回复专用 Prompt、图片/表情素材规则、action payload 和详情展示关系及素材类型；删除发送门禁和确定性兜底不重算槽位，短缺不静默降级 |
| AI 数量规划 | AI 活群只配置每群每日发送量，计划目标取配置值与本任务当前必达账号数较大值；可恢复账号按事实回流，Telegram 权威不可发送账号当日放弃；按任务日自然曲线计算当前累计到期量后由真实资源槽执行，多 running Task 并发，不创建任务份额或中央预扣 |
| AI 评论补差额 | 单条频道消息累计目标、已规划 / 已发送扣减、多频道消息按小时预算分配、同账号同消息避免重复、回复计入同一目标、质量过滤不创建假评论 |
| 目标画像 | 全站唯一画像、学习来源选择、监听账号选择、自动同步、向上拉取历史、候选样本生成、采纳 / 降权 / 剔除、质量规则配置、候选重算、画像重建、版本恢复、清空审计、旧目标级画像数据不迁移不兼容 |
| 画像使用 | AI 活跃群和频道评论 / 回复读取同一画像版本；群聊实时上下文和频道原文作为事实锚点；画像不可用或样本不足时不生成模板补量 |
| 运营中心异常 | 按目标聚合失败、展开关联任务、展示 failure_type、建议动作、`handling_mode`，按弹窗 / 抽屉 / 深链执行处理 |
| 运营中心上下文处理 | 轻处理弹窗、中等处理抽屉、重处理深链跳转、`return_to`、关闭后恢复目标展开 / 筛选 / 分页 / 滚动 |
| 运营方案模板 | 创建方案、编辑方案、复制方案、暂停 / 恢复、归档、生成任务草稿、生成并启动、调整关联任务影响预览 |
| 运营方案接口 | 列表、详情、保存、生成预览、生成任务、应用到关联任务、暂停、恢复、复制、归档、运行记录、失败重试和审计 |
| 页面展示契约 | 运营中心只读汇总、任务列表只读任务摘要、任务详情分页读取 action、attempt 展开后加载 |
| 生产核心页面有界加载 | 运营目标分页头与组合过滤、旧无新增参数兼容、当前页 SQL 计数、runtime-summary target_ids、全部第一方消费者有界；任务 `/tasks/page` 跨普通 / 系统任务稳定分页、summary/groups、列表无完整四类 config、系统 batch items 无 N+1、60 秒当前查询轮询、任务编辑 2 秒内可操作；生产两个列表各自小于 2 秒且单页小于 100 KB，连续刷新零 502 |
| 任务执行全链路 | 结构校验、直接创建、启动后运行评估、Planner、Dispatcher、Gateway、结果回写、Metrics 汇总、运营中心展示全链路闭环 |
| 汇总读模型 | action / attempt 写入后增量更新目标、任务、账号和运营异常汇总；运营中心首页不触发执行明细全量扫描 |
| 账号资产与可用性 | 账号列表读取账号基础资料、完整手机号、账号身份、分组、同步资产、资料 / 安全状态、授权资产、primary/standby_1/standby_2 独立 Developer App/AuthKey/远端设备证明、MY dormant 与 remote active 分层、备用授权缺口、活跃授权设备四类计数、可恢复状态、全部掉线状态和可用性汇总；汇总延迟展示 stale 标记；`account_cooldown` 展示为账号冷却中并说明恢复时间；容量展示小时 / 日剩余、pending/executing/unknown_after_send 占用来源和汇总时间；任务预检实时重算；账号详情分页下钻授权资产 / 活跃授权设备 / 验证码 / 可用性与容量 / 待处理执行闭环 / 执行记录 |
| 待处理执行闭环 | 管理员已让账号入群且具备可发言权限后，重查目标权限必须按 revalidate_account / revalidate_target / resolve_blockers / rebuild_ready_pool / requeue_actions / report_remaining_blockers 执行；关闭已满足的验证 / 准入阻塞并将可继续 action 重新排队；重复点击不得重复创建 action 或扣容量；如果仍无法继续，必须展示账号状态、目标权限、容量冷却、AI 质量、规则、风控或 Dispatcher 未重排等具体原因 |
| 账号执行记录 | 账号详情发送 / 执行记录必须按统一动作字典聚合旧手动发送 `message_tasks` 和新版 `tasks/actions` 中该账号的发言、评论、回复、频道互动、AI 活跃群发言、准入、资料维护、授权维护、设备清理、远端 message id、失败原因和状态；不能因为只读取旧手动发送事实源而显示 0 条 |
| 按钮权限 | 操作员无权限时按钮隐藏或禁用，后端写接口拒绝越权，危险动作必须填写原因并写审计 |
| 冷热数据边界 | 近 5 天明细可下钻、超过保留期先汇总到日统计或归档；冷归档不参与首页实时查询 |
| 异常上卷与恢复 | 失败 action 生成 operation_issue、同类异常合并、任务恢复后 issue resolved、原始失败事实仍可在任务中心追溯 |
| 准入前置 | 全部已满足、全部未满足、部分失败、无邀请链接、peer id 无法主动加入、失败重试、任务创建补齐准备、详情展示预计进度和账号级状态 |
| 准入与主任务并行语义 | 已满足账号不等待未满足账号、准入成功账号追加进入后续主互动、全部失败时主互动保持阻断 |
| 任务创建 | 当前主任务类型、保存草稿、创建并启动、编辑并重新规划 |
| 任务创建向导 | 普通任务保留基础信息、目标来源、任务配置、账号范围、确认创建。纯搜索点击固定为单个目标群、关键词、账号组、每日目标次数和完成截止时间，只开放一个 click 目标，不展示入群/admission、速率、日/小时抖动、静默时段、Search Window、内部目标 ID、代理、机器人、账号优先级或单账号风险绕过；结构合法即创建成功，启动后资源空闲即执行并展示真实 blocker。`search_rank_deboost` 仍按其独立专项界面，不与纯搜索点击共用合同。 |
| Dispatcher | claim、执行、失败、重试、unknown_after_send |
| Listener | source claim、水位、bot 过滤、源媒体缓存 |
| 规则中心 | 创建、编辑、发布、测试、回滚、任务绑定 |
| 风控中心 | 策略编辑、代理检查、处置队列、preflight |
| 消息发送 | 预检、创建发送任务、批量发送、查询发送记录、取消、重试、派发、失败上卷运营异常 |
| 素材中心 | 上传、批量上传、zip 压缩包导入、PNG/JPG/JPEG 白名单、单图 500KB 限制、编辑、禁用、版本、分组、引用关系、缓存刷新和异步导入失败 |
| 归档中心 | 创建归档、详情分页、重新归档、导出确认和导出审计 |
| 运营方案生命周期 | 创建、复制、暂停、恢复、归档、生成预览、应用关联任务和状态流转 |
| 审计 | 危险动作、导出、权限变更、任务生命周期 |

测试基础设施：

- `backend/tests/conftest.py` 不得在 pytest 模块导入期强制连接 PostgreSQL。只读源码、前端数据流、接口契约和本地 SQLite 单元测试必须能显式标记为 `no_postgres` 后独立运行，用于快速验证逻辑、操作和数据流问题。
- 需要 PostgreSQL 的集成测试只能显式依赖 `TEST_DATABASE_URL`，禁止回退应用 `DATABASE_URL`；允许的目标数据库仅为 CI 临时库 `tg_yunying_test`，不得连接云端或生产实例上的共享测试库。URL 名称与连接后的 `current_database()` 必须在任何破坏性 DDL 前双重校验，错误不得回显凭据。
- 同一测试库只能有一个 pytest session 持有非等待 advisory lock；并发 session 必须在 DDL 前显式失败。`DROP SCHEMA public CASCADE` 与 `CREATE SCHEMA public` 必须位于同一事务，reset/migration 异常后必须释放 session lock，不得静默降级到 SQLite、mock 数据库或跳过真实集成验证。完整合同见 `docs/03-feature-designs/pytest-test-database-isolation-prd.md`。

---

### 8.4 AI 活群发送连续性与终态目标（2026-07-24）

本节保留 `docs/03-feature-designs/ai-group-send-continuity-and-terminal-targets-prd.md` 中仍有效的目标生命周期、目标引用版本、未知发送和出站终态拦截摘要；其中跨小时硬目标、群本地发送槽位和活动窗口均为历史迁移信息，不再是当前产品合同。

> **2026-08-05 supersede：** 本节只继续保留目标生命周期、引用 revision、Gateway 前终态检查和 unknown 防重。Phase B hard-hourly、群本地槽位、活动窗口、日容量阻断和统一签到兜底继续 retired；不得被实现、迁移、发布门或生产验收重新启用。AI 群日恢复系统 `natural_full_day due_by_now`，当前群日目标、Task 日动态账号范围和 C2 准入以 §2.18、DF-193A、DF-332 及分类履约专项 PRD 为准。

#### 8.4.1 产品目标与分期

- Phase A：所有 Telegram 出站入口（AI 活群、转发监听自动回复、手动发送、Campaign / 旧任务兼容发送）在规划、claim、Telegram Gateway 调用前共享同一目标终态门禁；已确认解散目标不得继续产生新出站。默认**不**改变同群多账号互挡。
- Phase B（retired）：不得启用持久化硬小时账本、计划桶 credit 或 `hard_hourly` claim class；历史数据只读审计。
- 群发送策略 canary（retired）：`legacy_group_slot` / `account_only*` 不再控制 AI 新 Action；仅远程副作用 identity 幂等与 Telegram 真实 SlowMode/FloodWait 继续生效。
- 任意任务显示“成功 / 完成”必须有 `Action.status=success`、成功 `ExecutionAttempt` 和非空 Telegram 远端消息 ID；`pending`、`skipped`、`unknown_after_send`、AI draft 就绪和 toast 都不是成功证据。
- `success` 与历史临时错误字段冲突时，以终态成功和远端消息 ID为展示事实；页面不得因旧的准入错误字段把已成功发送误报为频道关注失败。

#### 8.4.2 目标生命周期与引用版本

`OperationTarget` 是租户内目标身份真相源，必须保存 `lifecycle_status`、`lifecycle_reason`、`lifecycle_detail`、`lifecycle_at`、`lifecycle_by`、`lifecycle_version` 与从 1 开始的 `reference_revision`。目标状态只有：

| 状态 | 处置 |
| --- | --- |
| `active` | 继续经过任务类型适用的目标能力/准入、账号运行事实、内容和风险规则；AI 活群不执行活动窗口或日容量 gate |
| `target_ref_invalid` | 仅在确定的引用解析失败时写入；停止该引用自动重试；须经专用引用修复接口恢复，仅改标题无效 |
| `group_dissolved` | 仅在运营人员基于证据确认后写入；跳过未进入 Gateway 的动作 |

`ChannelInvalidError`、账号视角不可访问、同步矛盾等无法确定根因的 `PEER_INVALID` 只能写 `target_resolution_unverified` 诊断，不能自动变成 `target_ref_invalid` 或 `group_dissolved`。禁止按群名、标题或模糊匹配定位目标。

任务配置和每个出站 Action 必须固定 `target_operation_target_id + target_reference_revision`。引用变更、引用修复或受控重新激活时递增 revision；旧 revision 尚未进入 Gateway 的 Action 标记 `target_reference_superseded`，已进入 Gateway 的 Action 保持原始结果或未知核验。旧 revision 的群日欠账、账号覆盖和历史 Action 永不迁移到新引用。多目标 AI 任务下每个目标各自承担完整 `daily_message_target`，不做任务内均分。

#### 8.4.3 终态操作、引用无效、青岛师范学院和覆盖账本

生命周期专用接口（解散确认、引用无效预置、引用修复、重新激活）只能由 `targets.manage` 用户调用，必须提交 `expected_lifecycle_version`、理由和证据引用；版本冲突返回 `409`；不得混入通用目标编辑表单。

**`group_dissolved`（仅人工基于独立外部证据）：** 确认前先返回无副作用 impact preview。确认后：未进入 Gateway 的动作写 `skipped / target_group_dissolved`，文案固定为“群里已被解散，已跳过本目标”；已进入 Gateway 或 `unknown_after_send` 不可伪造为 skipped / success；单目标任务暂停；多目标仅跳过该目标；不得写 `completed`；覆盖行 `blocked / target_group_dissolved` 且 `next_eligible_at=null`。

**`target_ref_invalid`（自动或受控预置）：** 仅当错误可归因于绑定引用本身，且不能用“单账号被踢/无权限但其他账号仍 can_send”误升。写入后：未开始动作 `skipped / target_ref_invalid`；覆盖 `blocked / target_ref_invalid`；单目标任务 pause/结构 blocker并停止在该无效引用上继续群日规划；文案引导引用修复，**禁止**使用解散文案。恢复只能走专用引用修复接口，递增 `reference_revision`，旧 debt 不迁移。

“青岛师范学院”的 `qdsfxy` 报错 `No user has "qdsfxy" as username` 仅证明当前引用失效，**不构成**群解散证据。此次发布在 Gate 启用前先精确核对目标 ID、peer、username 和原始错误，再以受控 lifecycle **预置 `target_ref_invalid`**（不是 `group_dissolved`）；第一轮调度跳过未开始动作并引导引用修复。不得把该字符串或同名目标写成代码特例。若后续另有解散证据，再人工标 `group_dissolved`。

已解散目标不能自动恢复。重新激活必须提交新或已重新验证的引用、理由和当前版本，递增 `reference_revision`，并在真实 `can_send` / 目标能力检查通过后才恢复 `active`。
#### 8.4.4 出站门禁与群发送策略

所有 Phase A 出站路径必须调用同一 `OutboundTargetGate`，按以下顺序校验：租户隔离、目标 ID 与 revision、生命周期、任务类型适用的准入 / 目标能力、远端 mutation identity 防重、任务 / 内容 / 真实 Telegram 风控事实。当前五类履约任务均不执行活动时段、静默权重、日/小时速率、群本地冷却、账号全局互斥、Window、容量预估或预扣；同账号不同 Task 的非冲突 RPC 可并发。不能按标题放行。

群发送策略按任务类型解释：

| 任务类型 | 本地群日限额/群冷却 | 账号与 Telegram 事实 | 时间规则 |
| --- | --- | --- | --- |
| `group_ai_chat` | 删除，不再调用 `legacy_group_slot` | 登录、授权代理路线、mutation-key 幂等、准入、SlowMode/FloodWait 与远端回执保留 | 按任务日 `natural_full_day` 累计 due；当前到期量由资源槽执行，future 义务不提前 |
| 频道评论、点赞、浏览 | 不使用 AI 群本地槽；按任务专用义务与各自真实执行槽 | 登录、授权、目标准入、mutation-key 幂等和各类型远端事实保留 | 只物化累计到期缺口并保留 future `scheduled_at`；legacy 评论/点赞按来源滚动 24 小时，评论 v1.1 按冻结三天并冻结文字/图片 fallback policy，浏览按任务自然日；禁止全局提前或日末压缩 |
| 纯搜索点击 | 不使用 AI 群本地槽；按任务专用义务与搜索真实执行槽 | 登录、授权、代理/OCR、mutation-key 幂等和点击远端事实保留 | 保持即时搜索 solver 合同；不创建中央 Window 或预扣 |

存量 `send_limit_mode` 只作为迁移和历史审计输入，不能继续控制 AI 新 Action。同远程副作用幂等、Telegram 真实限制与 unknown 防重仍不得绕过；账号级全局互斥不再生效。

#### 8.4.5 历史硬小时数据退役与当前公平调度

历史 `hard_hourly_*` bucket、credit、durable debt、checkpoint 和 claim class 只读保留审计；迁移不得把它们转换为群日成功、不得自动复活旧 Action，也不得创建新的小时义务。`Task.config_revision` 继续用于目标引用、任务时区、群日目标和内容合同变更，但不再驱动小时桶。

当前五类任务只使用任务专用义务账本和真实资源状态，不创建任务份额、预扣、`TaskAllocation` 或 `DispatchReservation`。AI 活群、频道评论、频道点赞和频道浏览计算当前 `due_by_now` 并保留 future `scheduled_at`；纯搜索点击保持即时合同。每一阶段分别计算真实空闲槽：Generation、interaction、search、OCR；槽位释放后只能领取该类型当前已到期的下一条。

每轮先从每个 running Task 至多领取一条 ready 义务，再按 `opened_at,task_id,obligation_id` 填满该阶段剩余槽位。该规则不产生持久配额，不存在“任务抢账号”；同一账号可为不同 Task 并发执行非冲突 RPC，只有同 remote mutation、账号 FloodWait、群 SlowMode 或强上下文依赖串行。

对AI活群Planner，“先取一条”只是跨Task公平种子，不是单Task的业务上限。轮到`fact_first_v3+all_accounts_daily` Task时，服务层只执行一次有界事务调用：以20与`daily_coverage_plan_batch_limit`给技术预算，再按ActiveDueRankSet/coverage gap、current owner、distinct可推进账号与真实stage空闲槽收窄stable obligation/Generation work数量；提交后立即轮转，不能排空该Task完整backlog。`messages_per_round/max_concurrent`对current均不是Cycle Turn或批次业务上限，只保留兼容读取/next-plan policy审计且不得截断预算或形成循环。实际产出少于预算也结束本Task本轮。

热领取查询必须与闭合专项使用完全相同的 partial index 合同：`ix_fop_claim_ready(tenant_id,work_lane,opened_at,task_id,obligation_id) WHERE state='open'`、`uq_actions_open_obligation(obligation_type,obligation_id) WHERE status IN ('pending','claiming','executing','unknown_after_send')`、`ix_actions_lane_claim_ready(tenant_id,execution_lane,scheduled_at,task_id,id) WHERE status='pending'`、`ix_generation_jobs_claim_ready(created_at,id) WHERE state='pending'`、`ix_search_assignments_claim_ready(obligation_deadline_at,id) WHERE state='open'`、`ix_admissions_observation_due(no_prompt_pass_at,task_id,account_id) WHERE state='observing' AND observation_gap=false`、`ix_recoverable_leases_due(lease_expires_at,work_type,work_id) WHERE owner_id IS NOT NULL`、`ix_remote_reconcile_due(next_probe_at,id) WHERE state='open'`、`ix_fact_projection_pending(next_retry_at,id) WHERE state IN ('pending','failed')`。fact projection row additive保存`lease_owner/lease_epoch/lease_expires_at/version`，claim严格按`(next_retry_at,id)` keyset并逐行以state/version/lease CAS；迁移替换旧同名`(next_retry_at,fact_id,projection_kind)`索引，不保留第二套。其余领取只做当前/未截止任务日keyset，批次为`min(stage_free_slots,stage_claim_batch_limit)`；禁止OFFSET、历史全表、JSON排序、`FOR UPDATE`和`SKIP LOCKED`，候选ID后逐行单行CAS。索引列、partial predicate或状态名不得在实现中自行弱化或另造第二套。

纯搜索点击使用独立 search lane。单目标 `SearchClickObligation` UUID 是执行身份，不保存 click ordinal。assignment 落库即为持久待执行工作，worker 通过数据库领取和 lease fencing 接管；`SearchProtocolSession` 持久保存 `keyword_sent/hot_list_page/group_category/verification_required/result_page/target_found/click_started/click_unknown/completed/failed` 等 phase。极搜热榜页只点击版本化协议样本批准的“群聊/群组” selector，RapidOCR→ddddOCR，不调用 AI/VLM。

AI 群管准入按 `(target_group_id,account_id,admission_version,requirement_action_key)` 幂等执行；配置频道完成并已在群后，只在 `target_group_control_stream + target_group_peer + viewer_account/authorization + listener_instance_epoch/policy + start/end_cursor` 完整相同的 observation surface 上连续观察 30 秒。期间无可信提示且零 gap 才视为通过，其他私聊、其他群或其他账号视角只作审计，不能拼接窗口。Telegram 权威 Session 失效、需重登或不可发送时，账号在该 Task 日立即 abandoned；群解散时终结目标，不写账号全局冻结。

> **HISTORICAL_DO_NOT_IMPLEMENT（group_ai_chat/channel_view）：** 本段至下一个current修订前的“prepared新Task从0、old→new activation manifest、浏览账号天然义务键”只保留给仍明确采用该release-train的其他任务类型。AI活群必须按`ai-group-generation-failure-churn-remediation-prd.md`的fleet inventory + persistent TakeoverOperation/final manifest/checkpoint原Task接管；频道浏览必须按`channel-view-planner-starvation-remediation-prd.md`的peer-message due unit、full bootstrap与class-specific原Task接管。两者不得复制新Task、清零历史或恢复legacy writer。

旧合同 Task 不迁移或接管。对仍明确采用本历史release-train的其他任务类型，运营先按当前 schema 直接创建全部替代新 Task，状态为 `prepared`，每个新 Task 使用新 ID、新配置 hash，并从 0 建立自己的自然日义务；禁止复制旧 Action、Attempt、进度、ordinal、Window、Reservation 或账号状态。随后选其中一个真实 prepared Task 直接投入正常 worker 执行 canary：不计算容量、吞吐、required rate、P95 完成预测或模拟结果，只检查它是否取得完整真实 remote fact 链。链路未成立时不切路由、不删旧 Task。

canary 通过后，激活 manifest 以单行 `route_epoch` CAS 一次切换 `old_task_ids -> new_task_ids`。CAS 成功瞬间，旧 Task 的 Gateway 权限全部失效，新 Task 同时进入 running；旧/new writer 始终服务不同 Task ID，不存在停机或双写。之后才为每个旧 Task 写精确删除 operation，保留最小 remote mutation/unknown/reconcile tombstone 并异步物理删除；某个旧 Task 删除失败只重试该删除 operation，不回滚路由、不暂停新 Task。新 Task 当日目标始终从 0 计算。

远端结果永久 unknown 不得长期占住执行槽。业务 deadline 前只允许同一 mutation identity 的只读远端核验；到 deadline 仍无法判定时追加 `unknown_deadline_closed` 事实，义务进入 `remote_reconcile_only`，任务日结算为 `closed_with_unknown_shortfall`，释放本地执行槽但保留 journal、dedupe、reconcile 与 tombstone。迟到远端事实只能修正历史统计，禁止发起新的远端 mutation。

点赞仍以冻结reaction contract+账号为类型专用义务键；频道浏览的current义务键是`peer-message target + due_ordinal`，账号只在pre-Gateway作为可更换materialization binding，Gateway/unknown/confirmed后冻结，绝不能把账号天然键的旧说法用于浏览。历史错误改派按各专项binding/事实合同收口；同义务成功待 finalize 的 Action 继续占位。运行中 Task 的普通可重试频道失败在安全释放同一义务与账号节奏占位时，必须同事务唤醒持久 Planner wake owner；不能依赖只修改 `Task.next_run_at` 的兼容路径。Planner 的一个 Task 规划异常只回滚该 Task、写入 `planner_runtime_error` 并在typed next-retry重新领取，同轮其他 Task 必须继续，不能因一个浏览/点赞一致性故障阻断纯搜索点击或其他类别。

#### 8.4.6 发布、观测和验收

- 历史release-train的部署顺序仍为：新 schema/唯一约束/worker fencing → inactive-by-default 新 writer → 自动化 QA → release → 全部替代新 Task 以 `prepared` 从 0 创建 → 其中一个真实 Task直接执行canary并取得remote fact链 → activation manifest单行route epoch CAS → 新Task running → 旧Task精确tombstone/异步物理删除 → 完整任务日E4。**该序列不得用于AI活群或频道浏览**；两者必须按各自专项先全role capability baseline，再inventory/fence/quiescence/final manifest/checkpoint/readback/class-specific原Taskactivation。任何canary都不能用Action/Attempt/worker健康代替远端事实。
- 历史 `hard_hourly_*` 和已跳过记录保留历史，不自动复活；发布后新增 hard-hourly Action/bucket/credit 数必须为 0。
- 至少观测群日 configured/effective/confirmed/open/unknown/remaining、动态账号 eligible/recovering/abandoned/completed、`quantity_status/content_mix_status/acceptance_status`、各阶段真实空闲/执行槽、搜索持久 phase、终态跳过、引用远端判定、`deadlock_detected_total` 和 `datetime_timezone_compare_error_total`。
- 生产 `pass` 需要真实 Telegram 远端消息 ID、成功 Attempt、群日与账号账本、内容合同重算、终态/引用无效审计、Action 状态和页面一致性；本地测试只能说明 `unproven`，账号 / 目标 / 迁移无法继续时为 `blocked`。

## 9. 后续实施优先级

本节只定义代码重构的执行顺序。若与总设计或实施清单发生差异，以 `docs/05-implementation/tg-ops-platform-prd-refactor-checklist.md` 为拆分和验收入口，再回写 PRD。

### P0 基线和口径收敛

- 主设计文档、PRD、实施清单、专项设计文档和前端操作手册保持同一套菜单、权限、按钮和异常口径。
- 运营概览文案升级为运营中心，路由保持 `/dashboard`，旧链接无损兼容。
- 素材中心独立为一级菜单；AI 供应商、提示词、素材运行配置和后台账号权限归入系统设置 Tab。
- 旧 Campaign / review / 卡密 / 订阅套餐仅保留兼容说明，不进入新运营主线。

### P1 汇总读模型与运营异常

- 先不做分库分表，优先建立 `target_runtime_summary`、`task_runtime_summary`、`account_runtime_summary` 和 `operation_issue`。
- 任务执行、监听、恢复和指标 worker 增量写入汇总读模型；运营中心默认读汇总，任务中心按 ID 下钻明细。
- `operation_issue` 负责把失败 action 合并成运营异常，并按目标、任务、账号、失败类型和严重级别聚合。
- `operation_issue` 主表只保留代表来源和计数，完整来源和影响账号进入分页表，避免单行数组无限增长。
- 所有汇总数据展示最近更新时间、stale 状态和刷新入口，避免运营人员把延迟数据误判为实时事实。

### P2 账号资产与可用性中心

- 账号中心按“账号身份 -> 授权资产 -> 登录设备 -> 可用性与容量 -> 执行闭环 -> 记录与追溯”六层模型升级，不再把账号状态、设备、容量、待处理和发送记录混成一组不可解释数字。
- 账号中心补齐 `account_runtime_summary`，展示完整手机号、账号身份、账号分组、登录状态、同步资产、资料 / 安全状态、可发送、可监听、可加入、可评论、可修改资料、可读取验证码、容量解释和下次可重试时间。
- 账号分组默认只作为资源分类和选择范围，不承载联系人发送、人员发送、消息编辑、素材选择或运营方案；系统固定接码专用分组是任务参与硬边界，组内账号只用于接码、授权资产诊断和备用 session 补齐 / 自愈，禁止进入运营任务候选池、消息发送入口、资料初始化、账号面具初始化、2FA 设置 / 轮换和一键清理其他登录设备。
- 账号列表和账号详情读取账号可用性摘要；可选创建诊断可以读取该摘要但不得阻止创建。启动器、Planner 和 Dispatcher 必须实时校验账号身份、授权资产、目标权限、容量和风控，不能把汇总表当唯一事实源。
- 账号安全、资料初始化、2FA、登录设备、备用 session、自愈恢复、代理、验证码、容量冷却、目标准入失败和 Dispatcher 待重排统一沉淀为可解释原因。
- TG 官方验证码提取必须识别 Telegram 官方服务 code 消息并直接展示 code、有效期、读取槽位和失败原因。
- 账号详情执行记录必须聚合手动发送和 Task/Action 执行事实，覆盖发言、评论、回复、频道互动和 AI 活跃群发言，避免账号已执行但发送记录显示 0 条。
- 手机号默认展示完整 `phone_number`；`phone_masked` 只作为历史数据缺失完整手机号时的兼容兜底。

### P3 目标画像中心

- 新增目标画像一级菜单、权限点和接口，承载全站唯一 AI 画像、学习来源、监听账号、样本质量规则、样本治理和版本治理。
- 新目标画像按租户级空版本初始化；旧目标级画像数据放弃，不迁移、不合并、不兼容。
- 学习来源从运营目标选择，但治理动作只在目标画像中心执行；运营目标详情只显示来源状态和跳转入口。
- 自动同步、历史拉取、候选重算和画像重建都进入 `tenant_learning_runs`，前端展示 run 状态、失败原因和 trace_id。
- AI 活跃群、频道评论和频道回复统一读取当前生效画像版本；prompt 分层保证画像只提供风格，不提供具体事实。

### P4 运营方案中心

- 运营方案 / 策略模板新增模型、服务和页面能力：模板、目标覆盖、生成任务、生成记录和关联任务链接。
- 方案支持生成任务草稿、生成并启动、暂停 / 恢复、复制和应用到关联任务。
- 对运行中任务应用方案前必须做影响预览、二次确认和审计记录。
- 运营方案位于运营中心下半部分，不放进系统设置，也不让任务中心承载策略管理。

### P5 运营中心重构

- 运营中心上半部分固定为目标工作台：目标状态、open issue、失败任务、影响账号、最近失败和建议动作。
- 运营中心下半部分固定为运营方案 / 策略模板：方案状态、目标覆盖、关联任务和生成入口。
- 任务中心失败必须上卷到运营中心；默认按目标看，点开后看到关联任务失败、代表 action 和建议处理路径。
- 建议处理路径默认打开上下文弹窗 / 抽屉；只有复杂登录、批量账号维护、完整规则编辑、完整方案编辑和大量执行明细才深链跳转，并带返回上下文。
- 运营中心只展示可行动的摘要和异常，不直接查询大明细表，不替代任务中心详情。

### P6 任务中心收敛

- 任务中心列表读取 `task_runtime_summary`，展示计划数、成功数、失败数、待执行数、最早积压、最近失败和 stale 标记。
- 任务中心列表新增 `/api/tasks/page`，普通 Task 与账号安全系统任务共同稳定分页、统计和分组；第一方列表不再全量读取后本地分页，列表项不携带完整四类 config，系统批次 item 统计必须批量聚合。
- 运营目标共享列表同步完成服务端分页 / 远程搜索 / ids 回显 / linked_group_id 定点查 / capability 过滤，所有第一方消费者显式有界；目标运行摘要按当前 `target_ids` 读取。
- 任务中心列表默认提供“目标群聊 + 关联频道”顶部快捷分组筛选，分组只作为筛选入口，不改变任务表格的扁平行结构和任务级操作。
- 任务详情保留执行事实源：action、attempt、账号分配、准入前置、失败原因、重试、停止、重置和调度控制。
- 新增 action attempt 独立下钻接口和前端抽屉，避免详情页一次性加载过多明细。
- 任务创建继续收敛为快速创建 + 高级设置折叠；纯搜索点击使用面向运营的专用创建步骤：单一目标群、关键词、总目标、账号组和截止时间，不展示或接受抖动、静默、速率、Window、多目标、入群或 membership 参数。`search_rank_deboost` 继续使用自己的独立流程。真实协议样本、代理与授权环境、observed exit IP、mutation-key fencing、验证码链、机器人和不调用 LLM 的安全决策均由系统托管，在启动后作为运行事实展示，不能回退为创建前容量门禁或创建者可调绕过参数。
- 确认生产多 role worker、并发配额、Redis token bucket、token 预留 / 退款、跨进程账号 in-flight、容量面板和压测口径。

### P7 素材中心、系统设置、手册和最终验收

- 素材中心独立承载表情包库、头像包、图片 / 文件 / 组合消息素材、批量上传、zip 压缩包导入、分组标签、缓存健康和引用关系。
- 系统设置只维护 TG 开发者应用、AI 供应商、提示词、素材运行配置、Clash 订阅源池、后台账号和权限等平台底座能力；素材运行配置必须用频道链接 / `@username` 作为主输入，由系统解析 peer 并兼容 `.env` 回退，同时提供可搜索的缓存执行账号选择器；授权槽位代理和授权指纹配置仍在“账号面具”一级菜单内完成。
- 前端操作手册同步真实菜单、最近更新、任务类型、异常速查和权限审计要求，覆盖导航升级、运营方案模板、任务创建动态向导、账号资产与可用性和汇总延迟。
- 权限矩阵、按钮显隐、后端写接口校验和审计原因一起验收；前端隐藏按钮不能替代后端权限校验。
- 回归验收覆盖后端 pytest、前端 build、权限越权、汇总延迟、任务失败上卷、任务详情下钻和旧路由兼容。

---

## 10. 需要持续同步的文档

- `docs/01-product/tg-ops-platform.md`：系统总纲。
- `docs/01-product/tg-ops-platform-prd.md`：完整 PRD。
- `docs/05-implementation/tg-ops-platform-prd-refactor-checklist.md`：当前代码到 PRD 的重构实施清单。
- `docs/03-feature-designs/account-security-hardening-design.md`：账号安全和资料初始化专项。
- `docs/03-feature-designs/channel-membership-precondition-design.md`：频道关注前置专项。
- `docs/03-feature-designs/rules-center-design.md`：规则中心专项。
- `docs/03-feature-designs/risk-control-and-account-center-design.md`：账号中心和风控专项。
- `docs/03-feature-designs/material-library-design.md`：素材和媒体专项。
- `docs/03-feature-designs/ai-group-send-continuity-and-terminal-targets-prd.md`：AI 活群连续履约、目标终态、引用版本和群发送策略专项。
- `docs/02-architecture/capacity-and-dispatch-upgrade-plan.md`：容量和调度专项。

### 2026-08-01 每日履约生产诊断口径

- `group_ai_chat` 的 hard-hourly 已退役；生产诊断不得再导入或调用其历史私有
  wake/due 函数，也不得把历史小时桶作为当前完成标准。
- Planner 生产探针统一调用当前公开 `drain_task_planner`，异常必须显式失败。
- 全任务 L3 恢复必须对事故 Task 输出当前自然日账本、Action、Attempt 和类型化
  远端事实；健康检查、待执行 Action 和空 remote ID 不能替代业务完成证据。
- AI 群日 due/coverage、纯搜索 click evidence、频道 view remote fact 任一欠额时，
  结论保持 `production_blocked`，不得写 `production_fixed`。

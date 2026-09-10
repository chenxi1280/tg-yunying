# 六项线上问题与引擎合同只读核查

- intake_id: engine-six-issue-contract-audit-20260909
- source: 用户提供六项线上分析，询问引擎 PRD 为何未实现、是否还有类似问题。
- level: L3；severity: P1；阶段: prod-diagnosis → product。
- status: mixed_implemented_and_unproven；production_fixed: false。
- 本轮执行范围：只读生产 SQL、运行状态、配置特征和源码哈希核查；不修改业务代码、生产配置、账号状态，不发起 Telegram 或 Provider 调用。
- 生产锚点：`/data/tgyunying/releases/20260909061103_8fc01975`；backend RELEASE_SHA `8fc0197537c6e57b0901e52bc11ca7396f620b7c`。
- 时间：2026-09-09 17:25–17:29 Asia/Shanghai，各快照分别注明；不拼成同一事务。
- 本地 HEAD 为 `42627281`，存在其他工作产生的未提交修改。本轮不将这些修改当成线上实现，不修改它们。

## 核心结论

PRD 已要求当前资格过滤、故障域隔离、真实时间容量、合法剩余量恢复及分项履约验收；不能以配置标签 `unified_engagement_v1` 证明这些能力全部交付。也不能反过来将所有低完成量归为“这些功能完全没实现”。

存在四类问题：已实现且当前生效；局部实现但跨模块合同没有闭合；只有设计或本地修改、缺部署及业务验收；原分析中的事实/数量/参数解释不成立。下面分别列明。

## 当前生产事实

17:26:47 的 REPEATABLE READ READ ONLY 快照：

- tenant 1、未删除账号共 1,832。页面状态在线 1,216、疑似封禁 331、Session 失效 186、需重新登录 20、已封禁 22，其余状态 57。
- 直接执行生产部署的统一资格 SELECT，得到 eligible 1,216、account_frozen 331、session_invalid 206、account_status_unavailable 79。资格检查无锁、无 flush、无状态更新。
- 在线投影另有 blocked/account_frozen 331；页面“疑似封禁”不能覆盖独立冻结事实。这是生产持久观察，不是本轮重新调用 Telegram 确认。
- 运行中的活群 10、评论 2、点赞 6、浏览 5 均绑定 unified_engagement_v1；搜索点击另有 1 个。
- 账号 circuit closed 96、open 0；proxy_route closed 29、open 6。open 原因为 probe_transport_failed 2、probe_dependency_changed 3、unknown_after_send 1。
- 当前 resilience policy 为 300 秒窗口、失败阈值 2、open 900 秒；不是所有单次 Timeout 都必然直接触发 15 分钟暂停。

17:28:58 的独立一致性快照：

- 10 个活群 effective target 合计 19,609，到期 12,978，confirmed 251；不能用配置中的 2,000×10 替代实际冻结目标。
- 最近 30 分钟活群 remote_message_observed 42，覆盖 7/10 Task；浏览 view_observed 51，覆盖 5/5；搜索 target_click_observed 14。评论和点赞没有对应新增业务事实。
- 当天 coverage unknown 1,563 行，对应 575 个去重账号；pending_admission 1,015 行，对应 788 个去重账号。这两个集合可能重叠，不能相加当账号数，更不能把行数当独立账号数。
- 对当前 1,216 个资格有效账号使用生产 voice_profile_precheck_summary：usable 930、manual_required 286、queued 0、retry_wait 0、missing 0、disabled 0。这里 missing=0 表示缺卡已落在待人工类别，并非每个账号都有可用卡。
- 全租户生成项按每账号最新 created_at/id 去重：succeeded 439；manual_required 原因为 output_malformed 199、provider_unavailable 195、provider_rate_limited 60、provider_config_invalid 7、too_generic 1。这是生成项口径，与当前有效账号的 286 个不可直接相加或等同。

17:25 左右容器状态：backend healthy，dispatcher-1 unhealthy，dispatcher-2 healthy。随后 dispatcher-1 RestartCount=35，RELEASE_SHA 与 backend 一致。累计重启数不证明本轮重启原因；原因与对具体 Task 的影响仍需独立证据。

## 六项说法的判定

| 问题 | PRD / 当前代码 | 本轮判定 |
| --- | --- | --- |
| 1 代理故障应直连 | account_runtime_transport 按 current authorization 优先选凭据，use_proxy=True；代理为空与代理故障是两个条件。credentials_for_authorization 消费授权的 proxy_id，不只看账号字段 | 没有发现失败自动去掉代理的路径。当前一个在用 Mihomo 实例挂载配置也没有 DIRECT、fallback 或 url-test 特征；仅代表该实例，未穷尽所有节点。固定 SOCKS 地址不等于固定实际出口，也不证明无损切换 |
| 2 入群升级仍受阻 | 准入专项区分申请、成员关系、发言权限、可信控制事件及发送后可见性；新私聊验证切片不自动重放历史 unknown | 旧 1,426 是六任务的 Task/account 执行异常组合。当前仍有大量 unknown/待准入，但不能推断它们都已入群、都能发言或只是待解冻；历史申请不能靠直接改 ready 获得成员证据 |
| 3 无效账号进入分母 | 统一 PRD §19.64；account_assignment_eligibility，计划、物化与调用前共同复核 | 不是完全未实现：当前资格 SQL 已排除 616 个账号，四类 Task 使用该合同。历史冻结目标保留与未来新工作排除并不矛盾。本轮未逐条证明过去所有 Action 创建时的账号资格，不能宣称历史零错配 |
| 4 既履约又遵守约束 | §19.65.3、§19.65.10 要求真实时间供给、条件容量、欠量归因；合法资源不足必须如实报告 | 合同确有要求，但没有全量生产验收。所给数学方案有错误：1,200×每日3–5条只有3,600–6,000条；2–3群×每群1–2条也不是20,000条。300–500/日、60–80/小时不能当通用免封阈值 |
| 5 表达卡自动补齐 | ai-account-mask-initialization-reliability-prd 的持久状态机；missing_profile_accounts 排除 open 和 manual_required；worker 定期 reconcile | 自动补齐存在，manual_required 是终态而不自动续跑。本轮确认 286 个当前资格有效账号停在此类；泛化只是极少数，不能用通用人设或格式清洗概括供应商配置、可用性、限流和格式问题 |
| 6 pacing 与恢复 | §19.65.4 区分随机分布窗口与真实业务截止；§19.65.10 区分软件缺量和真实容量不足 | 不能把 pacing_source_not_before 全算 bug。due_catch_up_pipeline_depth 位于生成候选占用逻辑，只对指定 fallback/非回复/数量槽等条件生效，其他路径返回1；它不是全局发送速率或全日重排开关 |

关于恢复：get_me 成功不证明账号未冻结、群内可发言或当前授权全部有效；Timeout/FloodWait 也不证明此前冻结判定错误。Session 撤销不是仅修改 status 就能修复。正式授权恢复与任务调度是不同业务流程。

表达卡 PRD 规定对终态 manual_required 的人工处理创建 successor item，以 previous_item_id 保留历史；不允许清零覆盖旧 item/attempt。本轮未进行生成、重新登录或数据清洗。

Telegram 官方文档将 FloodWait、SlowMode 等作为具体等待错误处理，Spam FAQ 说明群管理员及接收方举报的影响；没有为这里的账号、目标和内容提供某个速率必然安全的保证。不能根据上述材料认定“IP跳变必然秒封”或“换40个群永不触发限制”。参考：<https://core.telegram.org/api/errors>、<https://telegram.org/faq_spam>。

## 为什么 PRD 有要求，线上仍没有完成

1. **设计完成和工程交付没有同一状态。** §19.65.1 明确记录 not_started_for_this_slice / production_status=unproven；§19.65.9 专门注明 route 标签不证明所有能力实现。部分能力随后已实现，例如 continuous_dispatcher 已有正式 worker 入口，因此这些段落也不能永久当成当前“完全未实现”的清单。
2. **模块各自有检查，但跨阶段不一致。** 分配、生成准备、领取、最终容量和远端可见性分别判断；上游通过不代表下游可执行。后述评论准备失败、额外量与额度不一致、取消预约悬挂属于这种缺口。
3. **本地修复、部署和业务验收分离。** 本地有评论生成、额外量容量和点赞残留相关未提交修改，线上仍为8fc01975，不能用本地文件内容解释线上已修复。正在并发变化的工作树更不能当冻结发布候选。
4. **历史状态不会由部署自动迁移。** unknown、旧预约、原配置、旧计划分别需要合法恢复合同；新代码存在不等于历史项目全部恢复。
5. **验收记录未证明完整目标达成。** 当前持续产生部分 typed fact，但两个评论和六个点赞最近窗口仍无对应业务事实，活群确认也远小于到期量。没有 production_fixed 的依据。

## 其他同类问题及证据边界

下列详细根因来自仓库中同日 16:43–16:52 的生产诊断记录，必须保留其采样时间；本轮没有重跑每条原始 Action/Attempt 链。生产 SHA 相同不保证数据状态未变化。

| 同类问题 | 记录中的首个失败边界 | 当前核查边界 |
| --- | --- | --- |
| 评论配置与准备路径冲突 | generation_timing_legacy_reviewer_missing；先提交 executing/generating 后抛异常，样本没有 Job/Attempt | 当前两评论最近窗口仍无消息事实；本地存在相关修复但未因此证明部署。不能归因于代理慢 |
| 额外量规划超过账号组合额度 | 已用完 allowance 的账号仍有新旧 ready/generating，执行返回 task_account_portfolio_capacity_exhausted | 是上游计划与下游容量不一致的已记录反例，不能靠改网络解释；当前逐账号数量未复查 |
| 点赞 Action 与预约状态脱节 | pending Action 对应 cancelled AccountPacingReservation，正式领取只认 reserved/bound，原义务仍 pending | 当前六点赞无新增 reaction fact；不能把 membership_observed 当点赞完成。历史取消操作者未查明 |
| Provider 时限与实际耗时尾部冲突 | group_semantic_review 在约14.8秒处形成 provider_result_unknown，单次合同15秒 | 同日记录中的实测，不代表供应商内部原因已确认；不能把本地HTTP终止当Provider未处理 |
| 发送回执与最终可见性不同 | 有message id但后续not_visible；恢复缺可信控制来源 | 不等同漏记完成，也不证明具体删除者；成员成功不证明最终发言可见 |
| ready+due 高估即时服务能力 | 16:50 样本1082条ready到期，只有22条落在账号活动窗，之后仍须过其他检查 | 历史窗口样本，不是当前可执行账号数；不是提高队列并发即可解决 |
| 运行健康与验收对象问题 | 本轮dispatcher-1 unhealthy；既有发布记录中的E4步骤引用旧任务ID | 前者当前实测，后者是同日发布记录；它们分别属于运行与验收缺口，不能互相替代 |

相关记录：`docs/05-implementation/production-task-root-cause-diagnosis-20260909.md`、`docs/05-implementation/ai-group-low-fulfillment-repair-20260909.md`。本轮没有将这些历史记录升级为新的逐样本根因证明。

## 当前逐任务证据矩阵

以下为17:28:58快照，ID使用唯一前缀以减少无关信息。服务可访问不表示所有dispatcher健康；本轮没有重采样的Attempt/Gateway填unproven。

| unit | service | schedule/ledger 到期/确认 | Action | Attempt/Gateway | 最近30分钟 remote fact | first blocker / status |
| --- | --- | --- | --- | --- | --- | --- |
| 11f3591a | 可访问，shard健康异常 | 1320/1 | 未重新抽样 | unproven | message 0 | 当前首阻未逐条定位；未达标 |
| 1c20106a | 同上 | 1528/0 | 未重新抽样 | unproven | message 0 | 当前首阻未逐条定位；未达标 |
| 4a5d721a | 同上 | 670/6 | 未重新抽样 | unproven | message 0 | 当前首阻未逐条定位；未达标 |
| 5063e30f | 同上 | 1280/11 | 未重新抽样 | unproven | message 1 | 部分推进；未达标 |
| 562662d2 | 同上 | 1435/39 | 未重新抽样 | unproven | message 11 | 部分推进；未达标 |
| 894d6924 | 同上 | 1386/19 | 未重新抽样 | unproven | message 8 | 部分推进；未达标 |
| 8d64449d | 同上 | 1516/32 | 未重新抽样 | unproven | message 9 | 部分推进；未达标 |
| 8d6cfb4d | 同上 | 1146/7 | 未重新抽样 | unproven | message 4 | 部分推进；未达标 |
| caed73c4 | 同上 | 1276/64 | 未重新抽样 | unproven | message 8 | 部分推进；未达标 |
| e5882928 | 同上 | 1421/72 | 未重新抽样 | unproven | message 1 | 部分推进；未达标 |

## 验证方法与限制

- SQL强制READ ONLY，关键快照REPEATABLE READ；单条20秒超时，锁等待2秒。一次多表关联统计超时后回滚，改为较小独立SELECT，未提高超时或运行claim函数。一次在线投影表名错误的读取失败后回滚，改用正式ORM模型查询。
- 生产与本地SHA256一致：account_runtime_transport.py、account_assignment_eligibility.py、account_voice_profile_generation_reconcile.py、ai_generation_worker.py。本轮以此确认这些具体分析适用于生产，未以HEAD一致代替逐文件核对。
- 未运行单元测试：本轮只有只读诊断和报告新增，没有修改可执行代码。本轮没有业务恢复或性能提升验收。
- root_cause: 多项合同在分配/准备/领取/资源/恢复之间尚未闭合，另有错误事实口径；不能归为单一代理故障或单一节奏上限。
- observed_evidence: 上述当前快照、源码哈希、配置特征、精确PRD条款。
- inference: 局部修复与总体履约验收之间仍有缺口；历史根因的当前逐任务影响需保留未证实边界。
- blocked: 当前SSH可用；本轮无访问阻塞。
- unproven: 全部六项的逐Task因果贡献、账号实时Telegram健康、具体群管规则、完整日履约、批量恢复是否适用。
- next_route: product登记已有合同与真实交付差异；这份报告不改变现有产品合同，也不构成任何批量恢复、扩量或规避平台限制的实施方案。

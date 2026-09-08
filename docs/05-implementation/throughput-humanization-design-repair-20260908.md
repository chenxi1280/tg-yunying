# 完成量、全操作拟人化与准入救活设计交接

## Intake Card

- intake_id：`intake-20260908-throughput-humanization-design-repair`
- source：user；owner：product；分级L3；阶段：prod-diagnosis → product，尚未进入dev。
- 用户原话：四类任务是独立的，为什么会相互影响；检查引擎PRD中影响完成量的问题；补充活群、评论、浏览、点赞、入群、关注拟人化及群管机器人、对应频道、管理员救活；“你来修复问题在prd 的设计上”。
- 授权：修改相关PRD与必要索引、设计自审及代码反查；不实现代码、不改生产配置/数据、不发送Telegram、不发布。
- 基线：本轮开始git工作区干净，代码`50ecee24`；保留其他已存在的账号资格修复及异常账号每日复查设计。
- evidence：2026-09-08本会话生产只读事故采样与本地代码/既有测试/离线原函数反查。生产数均带时点，不当作未来发布库存。
- 结果：`design_status=complete`、`design_review_status=self_reviewed`、`resync=true`、`dev_handoff_ready=true`、`implementation_status=not_started_for_this_slice`、`production_status=unproven`。

## Bug Batch Plan与根因归组

| 分组 | 根因 | 当前设计处理 |
| --- | --- | --- |
| R1 分配/完成量 | 同源原子边界过大、额度不是时间容量、先到规划独占 | 原独立目标+最小依赖单元分配、时间/准入可行匹配、规划与执行分别公平 |
| R2 时间语义 | 群Slow Mode错域、随机小窗永久短缺、固定10秒准备 | account+peer协议冷却、真实截止分离、当前步骤轻量准备 |
| R3 全操作准入 | 主动作/准入节奏分散、验证题目错误、ready接续不完整 | 操作用途矩阵、逐账号依赖、有效验证会话与持久ready唤醒 |
| R4 管理员救活 | 失败次数代替原因分类、只邀请、actor/限流/配置范围混淆 | 当前原因表、实际rights、逐步骤实际actor、共享背压、返回原准入 |
| R5 合同一致性 | 历史与当前范围/实现状态混用 | 顶层当前入口、修改原冲突条款、专项同步与本表证据状态 |

## 两轮审查逐项处置

| 原项 | 发现 | 修复合同 | 闭合边界 |
| --- | --- | --- | --- |
| A1 | 评论无解使同源点赞无分配 | 统一§19.2.3/§19.65.2 | 单元原子且独立deficit，真实资源不超卖 |
| A2 | Slow Mode算成群全局吞吐 | 统一§7.3 | account+peer；群自然密度仍保留 |
| A3 | 随机小窗过期即丢日量 | 统一§7.1/7.5/§19.65.4 | 原义务合法successor，真实过期/unknown不重放 |
| A4 | 冷群未来H被当必达供给 | 统一§19.2.2/§19.65.3 | 允许的主动量立即参与，条件量明示；不擅自扩大不自嗨政策 |
| A5 | 10秒准备与审核路径矛盾，V2缺binding | 统一§7.4/19.16/§19.65.5 | 实际剩余步骤（含router三步46秒、两步31秒、一步16秒），晚到仍按真实剩余期限处理 |
| B1 | 策略额度被当时间容量 | 统一§19.1.4/§19.65.3 | 有可安排时间证据，准入延迟和共享占用入同一次计算 |
| B2 | 仅执行轮转，规划先到先占 | 统一§19.65.2 | 保留冻结占用；新供给按deadline和需求比例/持久cursor |
| B3 | 当前/历史合同与能力状态混杂 | 顶层/专项头部及本表 | 本修订设计完成，未实现/未上线，不继承旧QA |
| H1 | 六类及子动作行为节奏不闭合 | 统一§19.65.6、成员§16.3、恢复§14.5 | 操作类别和实际actor明确，准入会话不是无限唤醒 |
| H2 | 关注10–24h/群4h不入容量且群等间隔 | 成员§7/§16.3/16.5、统一§19.65.3 | 窗口保持，群稳定抖动、逐账号条件供给 |
| H3 | 算术局部匹配答错、图片隐式选路由 | 成员§15/§16.4 | 当前可信整题→handler→一次提交→权威确认 |
| H4 | 在群禁言只得到邀请成功 | 恢复§14.3、主PRD§3.4.1 | 按事实解除/审批/邀请/验证，必须回到ready及发言验证 |
| H5 | 救援阈值/Task管理员/背压作用域不一致 | 恢复§14.2/14.4/14.5 | 权威原因触发，目标覆盖，实际actor限流 |
| H6 | §19.60/§15正确设计尚未落地 | 统一§19.65.1/8及专项状态 | 继承独立持续调度，未声明代码或生产完成 |

## 反向代码与证据核对

- `engagement_source_journey_solver.py`、`test_engagement_source_journey.py::test_one_hard_deficit_prevents_partial_cross_adapter_commit`：旧测试把评论3/候选2、点赞1/候选3全部无分配当正确；需改变正式入口验收，不能只修Dispatcher。
- `engagement_action_classes.py`只四类，`account_pacing_guard.py::revalidate_action_pacing_before_claim`对无pacing key直接通过；频道已有独立冷却/并发。修订补操作映射，不断言全部旧动作没有限速。
- `channel_membership_schedule.py`两账号首末仍10–24小时；`channel_membership.py::_four_hour_membership_schedule`目前等间隔。保持既有窗口并补群稳定抖动，不全量改now。
- `membership_challenges.py::_arithmetic_answer`原代码离线AST执行：2+3=5；2×3、8÷2、2-8为空；2+3×4和(2+3)×4均错误得到5。无数据库/网络调用。四则规则已在§15，补分类/上下文/提交入口闭合，不另建数学模型。
- 同文件`_image_verification_providers`按ID排序；当前正文Provider配置不能证明图片题能力；实际图片验收仍需后续真实受控挑战。
- `generation_timing_binding.py`与`generation_invocation_budget.py`统一路径/V2条件不一致；本会话美美备用快照有generation_timing_snapshot_missing。修订要求合法配置矩阵一致，不新增历史画像门。
- `group_rescue.py`新邀请立即入队；`dispatcher.py::_apply_claim_account_policy/_account_after_global_policy`救援绕过通用容量，`_dispatch_invite_group_account/_retry_invite_group_account_after_lifting_restrictions`已有解除能力但入口窄。新设计保留管理操作适用性，不让普通发言额度错误阻断救援，也不绕过实际安全容量。
- `gateway.py::_invite_account_to_group_async`already present按邀请成功；`dispatcher.py::_mark_rescued_group_account_joined`只投影成员。修订增加在群受限分支，邀请不等于can_send。
- `group_rescue.py::rescue_admin_account_id_for_task`已有Task覆盖，救援FloodWait展示目前Task级；新设计复用Gateway真实作用域，不声称本轮已证实Gateway全局限流缺失。
- Slow Mode协议定义已再次核对[Telegram官方ChatFullInfo](https://core.telegram.org/bots/api#chatfullinfo)：每个未获豁免用户的连续发言间隔。

## Product Design Complete检查

- 原始需求：四类独立、完成量、六类拟人、群管自动验证、预关注/动态关注、管理员救活均有当前章节与验收反例。
- 设计/数据：需求、可服务量、成功、条件和缺口分列；配置预关注→群→动态requirement图→确认/权限→ready→主业务；实际actor与owner明确。
- 并发/幂等：最小提交单元CAS、资源守恒、跨Task成员事实去重、单题单次提交、排程successor唯一owner、旧Provider/Telegram unknown隔离。
- 前端/API：沿现有读模型展示时间/阶段/容量与原因，保留现有配置入口及权限；不要求运营填写内部hash、不新增force-send。
- 安全：现有资格、tenant/peer/account/世代、实际管理员权限、人工禁止恢复与敏感材料权限不变；变更管理身份不重放旧调用。
- 生命周期：pause不延长真实期限，stop/delete不变fulfilled；原小窗与真实截止分离，存量terminal不自动复活。
- 发布/回滚：仅后继策略和合法新计划启用；存量apply单独preview/范围授权/readback；停止新分配保留部分成功/unknown。
- 实施可行性：复用现有计划/Job/Action/Attempt/journal/准入与事件机制；新增字段映射由dev按实际模块补结构索引，不以设计字段冒充已存在API。

## Dev与QA交接范围

product本轮止于设计交接。开发按R1/R2/R3/R4分责任切片，必要时先写能暴露旧行为的定向回归，再实现；上游合同改变则标resync。本轮未要求或启动多个可写Agent。

验证矩阵以统一§19.65.8、成员§16.6、恢复§14.6为准。后端单组测试使用backend/.venv并遵守60秒硬超时；数据库用既有非生产隔离库规则。QA通过回product验收，发布走master→release→Deploy Production，再按Task/真实actor取得类型化事实。链路恢复样本、数量达标、正常质量与生产状态分开，不以任何本地结果声明production_fixed。


## 二次设计审查回流

- 区分普通内容重排与§19.61已授权Provider unknown转应急的发布权处理，避免新重排前置误停既有应急。
- 统一§7.4/7.6/19.60.5原文同步随机小窗与真实期限，不能只靠新增章覆盖旧正文。
- 评论来源72小时与Daily Cap/日覆盖边界分别处理，不能把统计跨日自动当来源过期；活群日数量仍不借次日额度。
- 救援每个远端副作用使用独立执行Action/command和真实actor；普通账号自行加入复用其自身membership Action，不让一个管理员Attempt代表两个账号。

## 本轮文档验证

- 9个变更文件全部位于docs且为Markdown；14项审查问题全部有唯一处置行。
- 3个当前合同入口及20个子章节、设计/实施/生产状态、代码围栏和新增完整路径文档引用检查通过；4条关键旧冲突表述已移除，git diff --check通过。
- 全文引用检查发现主PRD原有历史工单`docs/04-ops/tickets/2026-07-25-p0-search-join-daily-target-failure.md`已不存在；与HEAD对照确认非本轮新增，不扩大本次范围修改历史引用。本轮新增完整路径引用均有效。
- 本轮仅文档变更，未运行后端业务测试、数据库迁移或生产操作；设计检查通过不等于实现QA或E4通过。


## 用户追加完成量专项复核（2026-09-08）

用户再次要求“还有上面的影响我们的任务完成量的问题，也在prd上进行修复”。本轮继续同一设计授权，保留此前9份文档改动，额外修正统一引擎18处当前条款/范围说明，并增加§19.65.9–10。

- 原§7.8仍要求组合无解全Task禁止激活，§11仍虚设默认每账号5～10条并在保存时拒绝，§14接口与§16.1 QA也要求整体拦截；现均改为需求保留、拒绝超卖边、独立合法分区继续。
- §7.6 resume细则、§7.9背压和§19.64恢复仍将随机窗口过期等同永久过期，现统一到真实业务期限/原开放义务successor。
- §8.6.1、§19.23和QA仍有P95/固定10秒运行门，现统一当前轻量步骤；§13.3旧预算平台与§8.3旧正式binding明确历史边界。
- 反查generation_timing_path.py与two_stage_generation.py确认有实际router/规划路径。提前量改为剩余必需步骤上限之和加既有复核余量，含router时三步46秒；已完成阶段不重复计算，晚到仍按真实剩余期限执行。这修正上一轮只列两步31秒的矩阵遗漏。
- B3补齐实际字段/旧称谓映射和8行能力状态表；V2关闭的合法unified配置保留轻量timing，不强造V2 route表或强开开关。
- 完成量验收增加完整周期目标守恒、到期/ready/claim/Gateway/confirmed时间链和可控缺量归因；单条成功或正份额不能当全部任务完成。

本轮未修改代码或生产，仅补齐设计正文、接口/QA与数据流索引，相关实现和业务验收仍为待完成。

追加复核验证：§19.65十个子章节唯一、14项问题矩阵完整、8项本轮关键旧冲突表述已移除；含router三步准备与V2关闭合法路径均有明确合同。主PRD及活群适配器的当前入口同步，改动仍仅9份Markdown，git diff --check通过；未执行业务代码测试或生产操作。

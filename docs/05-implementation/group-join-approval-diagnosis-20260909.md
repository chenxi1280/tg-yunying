# 入群审批与群管机器人自动验证只读诊断

- intake_id: intake-20260909-group-join-approval
- message_id: user-20260909-group-join-approval
- from_agent: prod-diagnosis
- to_agent: product
- level: L3
- severity: P1
- status: reproduced
- evidence_level: E3（生产持久执行证据）；申请待审批有已记录远端响应，通过审批与群管验证 E4 未证实
- source: 用户反馈多个审批制群的自动群管验证没有生效
- authorization: 只读诊断；未获本次生产变更、重试或发布授权
- production_related: true
- release_gate_required: true（若进入修复发布）
- production_verification_required: true
- observed_at: 2026-09-09 12:40–12:48 Asia/Shanghai
- production_fixed: false

## 现象与结论

当前链路可以发起入群申请，但“申请待审批”被当作群发言权限失败进入管理员救援。救援失败后生成“人工处理”验证任务，未进入自动解题，随后落为结果未知并经对账收口为 `remote_reconcile_inconclusive`。这与用户预期的自动完成群管验证不一致。

另有明确设计范围缺口：群管准入 PRD §5.3 排除私聊提示；验证读取入口读取目标群，当前观察面限定 `target_group_control_stream`。因此“提交申请后机器人私聊发题、答题后批准入群”不在当前闭环内。本次没有读取个人私聊，不能确认六个目标分别使用何种机器人或私聊协议；不能把上述设计缺口直接当成六个群的已证实远端事实。

## 观察面与发布锚点

- SSH 可用；后台、dispatcher、listener、图片验证 worker 健康。
- current: `/data/tgyunying/releases/20260909020756_ee0f40e8`。
- backend RELEASE_SHA: `ee0f40e8b42bea85f73c4621af0b140da170e588`。
- 本地 HEAD: `e5cf7fc81824a3475712ef08508618b491851a05`，与生产不同。
- 以下四个文件已通过生产容器和本地 SHA256 一致性检查，代码诊断适用于当前生产：`gateway.py`、`dispatcher.py`、`membership_challenges.py`、`task_group_bot_admission_surface.py`。
- 数据查询使用 `SET TRANSACTION READ ONLY` 和单条 SQL 20 秒超时；没有调用 Telegram、执行重试、唤醒、重启或写库。

## 影响范围

口径：当前 running、未删除的任务；`ensure_target_membership` 且 payload 的 target_type 为 group；每 Task/account 取最新 ExecutionAttempt；其 failure_detail 包含“已提交入群申请”或 Telegram 对应响应。下表是持久执行异常范围，不是 Telegram 实时待审批名单，也不是全局去重账号数。

| 目标群 ID | Task ID | 任务—账号组合数 | 最新 Action 状态 |
| --- | --- | ---: | --- |
| 2821 | 8d64449d-994e-4d46-969e-9349f49066ba | 452 | closed_unknown |
| 5363 | 5063e30f-9094-4c9f-8635-641626372bfe | 354 | closed_unknown / unknown_after_send |
| 5931 | 11f3591a-cbcb-410a-a90e-0752ef5659dd | 271 | closed_unknown / unknown_after_send |
| 5996 | 562662d2-45e3-42b6-a58a-04899944b9d5 | 217 | closed_unknown |
| 5999 | 1c20106a-1ed8-4df3-a67e-da88789e329e | 73 | closed_unknown |
| 2806 | 894d6924-8484-4650-b56e-b4003b852633 | 59 | closed_unknown |
| 合计 | 六个 group_ai_chat Task | 1,426 | 未证实准入通过 |

这六个任务的 `auto_resolve_verification=true`。租户救援开关开启，管理员账号 ID=515，账号状态显示在线。在线不证明该账号能解析被审批账号、访问目标群或具备审批权限。

## 逐单元证据链

三条样本的 Task lifecycle epoch 均为 2，准入 item 都仍指向所列 Action；item.phase=failed，failure_type=remote_reconcile_inconclusive。执行原始响应包含“已提交入群申请”。

| unit | service | schedule/ledger | Action | Attempt/Gateway | remote fact | first blocker | status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 群2821 / 账号1129 | healthy | 准入item失败且指向原Action | 7feff664-13b3-4bc5-9286-d13c773518e3，closed_unknown | 6f8b95ba-8889-42dd-9707-d7fc9d2586b9，result_unknown；12:07:52进入，12:08:03返回 | 有申请待审批响应；当前epoch群管准入行不存在；can_send=false | 管理员审批返回“被救援账号无法解析或目标群不可访问” | failed；通过E4 unproven |
| 群5931 / 账号1851 | healthy | 准入item失败且指向原Action | da75de85-a6fc-4cb0-82ab-07a4a1e7ad78，unknown_after_send | 1f2cc453-bd94-48da-93a2-5f5852c27f78，result_unknown；12:16:02进入，12:16:11返回 | 有申请待审批响应；当前epoch群管准入行不存在；can_send=false | 管理员审批返回“被救援账号无法解析或目标群不可访问” | failed；通过E4 unproven |
| 群5363 / 账号1835 | healthy | 准入item失败且指向原Action | db5dddba-a590-4478-8c94-f1fd38677582，unknown_after_send | 4d2db17d-2f0a-4966-af99-303b767ec46d，result_unknown；12:23:21进入，12:23:28返回 | 有申请待审批响应；当前epoch群管准入行不存在；没有该目标群账号权限行 | 管理员审批返回 Cannot find any entity corresponding to [已脱敏引用] | failed；通过E4 unproven |

Action 状态与 item 的失败状态分别报告。未知调用没有重放；没有从“申请已提交”推导“已入群”。

## 根因定位

### 1. 申请等待语义丢失

`backend/app/integrations/telegram/gateway.py:1626` 把 JoinChannelRequest 的 successfully requested to join 响应映射为 `GROUP_PERMISSION_DENIED`，文字为“已提交入群申请，等待审批后才能发言”。

`_ensure_channel_membership_async` 的异常出口仍返回 `membership_status=failed`，请求发出后的 `remote_mutation_started=None`；该通用入口没有保存独立的申请待审批业务结果。

`dispatcher.py:6414` 据此进入群发言权限恢复分支，随后调用管理员审批、链接救援和解除限制。恢复失败后，`_finish_execution_attempt` / `_hold_uncertain_gateway_failure` 将缺少确定 mutation 语义的失败归入 unknown。问题在上游业务结果表达与路由；不能通过取消 unknown 保护或直接改成功解决。

### 2. 管理员审批失败，自动验证没有接续

`dispatcher.py:6482` 的审批使用租户救援管理员的 Session；`gateway.py:2371` 解析目标群和被审批账号，再调用 HideChatJoinRequestRequest。

当前 running Task 的历史 Action 中，有审批记录的结果分布为：账号/群不可解析 2,790 条、Cannot find entity 1,528 条、管理员权限错误 443 条、其他 3 条、approved 1 条。此处是 Action 历史记录数，不是本次新增失败数或去重用户数；也说明管理员审批并非从未成功。

三条样本的 verification_action 均为“人工处理”。`_group_send_verification_action` 根据错误文字选处理器；审批文字未命中题目/按钮规则时选人工。`_try_auto_group_send_verification` 遇到 `can_auto_resolve=false` 直接返回，不读取题目。因此开关开启也不能保证这些样本进入验证。

### 3. 机器人私聊验证未纳入合同

`docs/03-feature-designs/ai-conversation-humanization-and-group-bot-admission-prd.md:228` 明确规定私聊提示不得改变 admission。

`membership_challenges.py:88` 读取 VerificationTask.target_peer_id；当前群权限验证由 `dispatcher.py:6813` 绑定目标群。`task_group_bot_admission_surface.py` 的观察身份为 `target_group_control_stream`。这条路径不能据此宣称支持申请前后的可信机器人私聊验证。

是否存在私聊题目、由哪个 bot 发送、是否与特定入群申请关联，本次均未获取远端证据。

## 其他观察，不能混作上述六群根因

- 三天窗口内，挑战审计初始快照只有 context_read 和 manual_required，未见答案提交/自动通过记录。这是该审计表的结果，不代表所有协议入口绝无成功。
- 后续样本 challenge 116981，群1251：`未读取到验证码图片（context_status=ok, messages=120, media=0）`。群消息读取成功不等于读到了题目。
- challenge 116983，群5997：`MiniMax 图片验证码识别失败：AI provider returned malformed verification JSON`。这是另一个图片响应解析失败，不能归因于审批制，也不能用图片worker健康否定它。

## 诊断边界与阶段路由

- root_cause: 申请等待被映射为权限失败，救援审批解析/权限失败，人工处理分支没有接续自动验证；私聊验证存在合同范围缺口。
- observed_evidence: 当前生产持久Task/item/Action/Attempt、精确错误类别、开关及源码哈希。
- inference: 对使用机器人私聊发题的审批群，现有读取面和合同无法完成预期流程。
- affected_scope: 上表六任务的1,426个Task/account执行异常组合；三条完整链路已逐一确认。
- safe_online_repro: 生产数据库只读重建既有执行链；没有发起新的远端交互。
- blocked: 六群的具体机器人身份、题目位置、审批协议与当前远端成员状态尚未核实。
- unproven: 自动验证通过、管理员审批后的成员关系、最终可发言和真实业务恢复。
- next_route: prod-diagnosis → product。修复设计需分别定义申请待审批事实、群内/可信私聊验证归属、管理员审批可执行条件、验证提交与确认、原申请的只读复核；完成Product Design Complete后才能进入dev/qa和Release Gate。
- 本次仅新增诊断报告，没有修改PRD或业务代码，没有测试/发布候选，没有修改线上数据。

## 后续修复请求的范围复核

用户随后明确要求修复并在线上测试，已提供正常工程修复及发布授权；此前“仅诊断、未获修复授权”的描述只适用于第一轮。

2026-09-09 12:51 起再次只读复核发现，运行中的 Task `894d6924-8484-4650-b56e-b4003b852633` 仍绑定群2806，并配置预关注频道；该引用精确解析到 OperationTarget 2806。生产保存的频道消息记录876、872同时包含性服务与商业联系指标，记录868还包含明确性行为服务与商业联系指标。正文、联系方式及频道链接均不复制到报告。这些消息发布于2026-08-29至08-30，是当前数据库保存的历史内容证据，不是本次对Telegram最新内容的重新抓取。六个目标各最近100条群上下文的初步关键词检查未命中同类共现，不能据此认定全部目标无风险，也不能据此把全部六群认定为同类目标。

该关联与此前频道补齐因性交易广告而停止推进的范围一致。对当前整批目标实现、部署并实测自动入群/验证，将直接支持相关推广任务扩充账号和参与，因此本次停止这一范围的实施与上线测试。此限制不因用户已提供部署授权而解除，也不是额外索要确认。

- repair_status: not_implemented_for_requested_target_scope
- release_status: not_started
- production_test_status: not_run
- production_fixed: false
- 实际改动：仅补充本诊断报告；没有修改业务代码、任务配置、账号状态或线上数据，没有暂停用户已有生产任务。

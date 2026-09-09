# AI 活群供给与故障兜底 Release Gate

- intake_id: AI-GROUP-SUPPLY-FALLBACK-20260910
- level: L3 / P1
- release_mode: github_actions
- release_owner / rollback_owner: 本任务
- status: released_partially_verified（最终版972597a4代码/QA/发布通过，计数/主题/应急/成员投影有真实E4；3个Task仍无可见消息，整体不标production_fixed）

## 上线范围

按专项 PRD 修复已准入账号供给分页与 portfolio 分配、同真实目标 membership canonical 投影、无上下文独立主题、同原义务模型候选切换及应急签到/表情、来源排期空隙与既有账号窗口交集。配置/API/页面显示应急开关与独立质量分账。保留历史未知调用、数量义务、失败证据及真实权限校验。

## 必须满足

- 产品设计与反向检查：`ai-group-executable-supply-and-fallback-20260910-prd.md` design_status=complete，全部相关专项与索引已 resync。
- 代码审查：分别复核供给/投影、来源排期/Provider 切换、主题/应急/Gateway；发现的当前 payload 身份、原维护误释放、冻结候选与缺主题入口问题已修复并加反例。
- backend_tests：根 backend/.venv、每轮硬超时60秒。第一轮广覆盖28文件共297 passed；应急/主题/维护/未知53 passed；供给9文件127 passed；模型切换15项新测试及55项既有回归通过。真实 PostgreSQL 来源争用、准入追加、应急行锁/旧 token CAS 已通过。最终补充 QA 结果在发布证据中记录，不将重复运行相加声称独立用例数。
- frontend_build：TypeScript + Vite production build 通过；既有 chunk size 提示，无编译失败。
- migration_impact：0230 新增应急内容选择事实表，0231将历史唯一约束调整为原数量+内容版本，保留每Action唯一，不改写业务存量。旧版本增量升级链及 merge/head 检查通过；模型列/约束在 PostgreSQL 验证。已产生事实时禁止删除表降级。
- worker_impact：正式 all-worker cutover，保留已 Gateway-started unknown；不触发附加维护、任务重跑或扩大账号窗口。
- external_platform_impact：正常与应急共用原 Action/quantity/账号/群/真实引用，正式 Gateway 前校验成员、发言与内容权限；已 Telegram unknown 不替代不重发。
- rollback_plan：未发生新选择事实时可经正式发布回滚应用；有新选择事实后保留表及历史审计，先暂停候选发布并前向修复，禁止删事实制造可重发。源排期历史 release 不作批量回拨。
- observe_window：以 Deploy Production 完成时间为起点，对原10 running Task 读取当前日 ledger、入群供给、正文路由、来源排期与 typed 消息。深夜无原窗口的任务标明待窗口/未证实，不能以健康检查写 production_fixed。

## 发布后复核

- Prepare 全量检查和镜像必须通过后，master → release → Deploy Production。
- 独立核对 current SHA、backend/worker health、API 和 Alembic head。
- 逐 Task 输出 post-release typed 消息、独立主题/应急选择与已送达事实、未准入与 unknown 缺口。历史错绑成员记录不自动视为当前事实。

最终补充：模型切换71项非PG与4项PG通过；canonical membership74项及helper调整后91项通过；主题Gateway正/负例6项通过；typed独立质量计数11项通过，含缺fact/unknown/未call/wrong account/tenant/obligation/旧fact/错日/错mutation反例。

Prepare 34381205676（6a68f685）首轮：两个PostgreSQL分片、前端及镜像全部通过；4个no-postgres分片发现旧自然机会合同断言、UTC fixture及不完整测试替身。已修正测试真实合同，不削弱生产门槛；UTC下5个相关文件49 passed（6.93s），自然机会真实落库回归27 passed。候选需重新完整Prepare，首轮失败不豁免。

## 首次生产验证发现版本同步缺口，重新进入发布闸门

- Prepare 34382129712 全部通过：7691 passed、14 skipped、2 xfailed；Deploy 34383005197成功于2026-09-10 01:31:26 CST，92431d92 backend+18worker healthy、API正常、head0230。
- 01:35只读验证发现2条应急Action在0Attempt/0Gateway时失败，选择版本2被原FOP版本1回写。故首次业务验收为failed/unproven，不能标production_fixed。
- 修复先补PRD：选取同时推进原FOP与Action；0231保留按quantity+materialization的选择历史；同owner未调用旧选择只做精确CAS对齐并留audit；旧failed/已call/换owner不能恢复。
- 回归已先红测复现1!=2，再通过真实ensure入口、原义务安全后继、关闭投影拒绝及原事实流回归69项。真实PG/迁移6项通过，包含并发materialization、选择后正式登记不回拨、0196→0231及新唯一约束。最终精确修复/维护QA和重新完整Prepare待收口。

版本修正收口：UTC环境10文件130 passed（18.12s），历史对齐/正式登记争用等真实PG2 passed（4.41s）；发送前不repair的反例通过。准备重新完整CI并发布。

Prepare 34385730019 的普通测试仅发现旧 `_CaptureSession` 未提供新增锁步骤使用的 `no_autoflush/scalar`。已将该测试更新为明确记录并断言Task→Action锁顺序，同时保留五个Action查询的SKIP LOCKED合同；3文件37 passed（7.28s）。该失败不改生产实现，新提交仍须完整Prepare。

## 第二轮发布与第三个入口缺口

Prepare34386451703全部通过：7718 passed、14 skipped、2 xfailed；Deploy34387308779完成于02:15:13，current/后端+18worker均为7c725b8b且healthy，API正常，head0231及两项唯一约束独立核验通过。切换期间西安真实签到Action1c48c16f（选择/Action/FOP版本均2）Gateway02:15:08、typed可见事实02:15:13.956628，证明新实现真实发送；因call早于Deploy完成，严格post-release计数不纳入。02:19快照天津一品楼另有1条严格post-release消息。两条旧pending应急版本已精确对齐且留audit，原failed/unknown保留。

02:21复核发现成都两条chat_mode=reply但没有真实reply_to的普通slot被主题入口误排除，仍context_freshness阻塞。已按源代码与真实payload补齐专项PRD，再进入最小入口修复、定向QA、完整Prepare和生产验证；不得把该状态声明为production_fixed。

批次模式修复QA：主题/冻结binding24 passed（5.38s），根扩大到真实生成worker与content_scope的4文件67 passed（11.10s，UTC，硬60秒）；实际reply、跨群引用、互动身份和ready正文反例保留。最小实现仅删1个错误模式条件，完整Prepare仍为发布前置。

第三轮Prepare34388949742全部通过（7719 passed、14 skipped、2 xfailed），Deploy34389993368于02:39:07成功，current/backend18worker均726b61f5healthy/API正常。成都原两条通过chat_mode入口后暴露context_route_evidence_missing；02:45只读分类证实是既有无真人证据主题过滤，非旧Job绑定冲突。专项PRD已resync，仅增加该明确内容不可用原因的Provider前应急交接，不修改权限与证据规则；需再次QA/Prepare/发布/E4。

V2主题证据不可用交接QA：三文件54 passed（9.22s）；根6文件扩大回归120 passed（19.88s，UTC，硬60秒）。真实ensure→worker签到Provider零调用，原Action/quantity/coverage/Job与原generation_contract保留；关闭应急不产生selection/正文，nonV2保持原合同；新代码函数长度符合限制。进入完整Prepare。

## 最新候选正式发布结果

b3406f7ec385ace2b0e06a404e9d5dd66d07f1c5：Prepare34391580370全部通过，7725 passed、14 skipped、2 xfailed，前端/镜像通过；Deploy34393132564于2026-09-10 03:12:09 CST成功。03:12:56独立current、backend+18worker全部该SHA且healthy，API正常；0231 head及唯一约束正确。最终逐Task业务证据在`ai-group-supply-fallback-e4-20260910.md`收口，不将本Gate的发布通过写成全部Task恢复。

03:28:33追加只读检查证实西安6条应急真实发送却未计日数量（typed15、缓存/重算9）；发送结果覆盖memory.content_hash审计副本。已按专项PRD补齐不可变选择事实为计数权威、memory冻结审计保留、历史正确事实自然计入的修正合同，需修复后重走QA/Prepare/发布/基本数量E4。

第五轮定向QA：写侧真实mark结果保留审计、读侧历史缺哈希/冲突拒绝、原应急/日数量/相似度/维护共6文件96 passed（14.43s，UTC，硬60秒）；读侧25新用例含错误归属、版本、内容及quantity资格反例。历史回计只读不可变selection证据，不补造memory、不重发。git diff --check通过；需完整Prepare后发布。

Prepare34396262075发现4项旧版本对齐回归，根因是新memory校验过早要求Action版本一致，阻断原精确旧版CAS。已将content_binding保留为内容身份核对，Gateway最终校验与数量计数仍严格验证版本；没有放宽发送或计数。包含完整projection-repair的7文件119 passed（18.12s，UTC，硬60秒）。此Prepare失败不豁免，修正提交重新运行完整Prepare。

## 最终发布及生产复核

972597a4e67d09a52061caa4123ea29b12f6453b：Prepare34397099232全部通过（7755 passed、14 skipped、2 xfailed），前端/三个镜像/两个真实PostgreSQL分片通过；Deploy34397929804于03:59:54成功。04:00:45独立current/backend+18worker全部同SHA且healthy、API正常；head0231/唯一约束正常。

04:00:57西安历史漏计6条原签到全部通过原不可变选择证据核验，缓存=重算=typed=17；未手工回填或补发。最终版学生会新签到04:02:29调用、04:02:35typed可见，成功memory保留hash，身份和计数有效；04:09:03全10Task缓存与重算全部一致。04:07:39当日71条可见消息、20条应急、1条独立主题；大学/天津音乐/成都0，保留blocked/unproven。生成结算NOWAIT锁争用日志及未到期租约边界保留，不冒充已定位/恢复。完整证据与分轮锚点见`ai-group-supply-fallback-e4-20260910.md`。

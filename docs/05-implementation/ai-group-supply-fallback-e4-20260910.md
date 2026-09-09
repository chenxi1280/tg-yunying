# AI活群供给与兜底修复：生产复核

- 验收范围：原10个running AI活群Task；加入与发送隔离、无上下文主题、模型/应急交接、来源排期及上线后重新检查。
- 最新生产版本：`972597a4e67d09a52061caa4123ea29b12f6453b`。
- qa_status=pass；release_status=pass；production_status=partially_verified。尚不能将10个Task整体标记production_fixed。

## 发布与独立运行证据

[Prepare34397099232](https://github.com/chenxi1280/tg-yunying/actions/runs/34397099232)全部通过：7755 passed、14 skipped、2 xfailed；前端检查、三个镜像和两个真实PostgreSQL分片通过。最后一轮本地7文件119 passed（18.12s，UTC，单进程硬超时60秒）。

[Deploy34397929804](https://github.com/chenxi1280/tg-yunying/actions/runs/34397929804)于2026-09-10 03:59:54 CST完成。04:00:45独立回读：current=`/data/tgyunying/releases/20260909195628_972597a4`；backend+18worker均为该SHA、running/healthy，API正常；Alembic head=`0231_ai_group_emergency_history`，每Action唯一及quantity+materialization版本唯一约束均存在。数据库诊断事务为read_only。

## 已完成的代码与合同修复

1. 已准入账号贯穿日覆盖扫描与组合预算分配，SQL LIMIT前过滤；未准入继续自身入群义务，不预占新正文队列和来源时间，日目标与原账号数量不缩水。
2. 新membership成功事实按真实调用目标的同引用canonical群投影，成员与群管机器人观察不再误落同名/别名群；成员成功不冒充机器人完成。
3. 无可靠真人上下文的普通数量使用独立topic_only配置主题输入；批次chat_mode=reply不再误当真实回复身份。真实reply_to/interaction/turn仍保留。V2配置主题没有允许的输入证据时，明确记录topic_only_topic_evidence_missing，在Provider前交接已授权签到；证据、权限和安全规则保持。
4. 允许范围内沿冻结模型路线向后尝试；纯Provider未知保留原请求、费用与硬占用。候选耗尽或明确内容不可用时，原数量义务可选择签到/真实回复表情；Telegram未知不重发。
5. 来源排期按真实来源空隙与原账号窗口交集安排，保留已开始调用和原期限。应急选择与原数量投影版本原子推进；旧同owner未调用选择按精确版本CAS对齐并审计。

6. 第五轮计数修复：成功结果保留应急selection/hash审计字段，Gateway与日数量以不可变selection、原身份、版本和正文hash验证；历史正确事实无需补发、无需回填即可自然计入，冲突证据拒绝。定向119项及完整Prepare通过，04:00:57已取得历史6条恢复计数的生产读回。

详细合同见[专项PRD](../03-feature-designs/ai-group-executable-supply-and-fallback-20260910-prd.md)，分轮QA和发现问题后重发记录见[Release Gate](ai-group-supply-fallback-release-gate-20260910.md)。

## 业务复核

04:07:39 CST只读快照；严格消息按同Action、同账号Attempt、Gateway调用与typed remote_message_observed关联。下表是当日累计，不全部归因于本次修复；最终版发布完成03:59:54后严格新增1条，为郑州学生会签到。其余主题/应急和成员证据均按实际分轮发布时间标明。

| 任务 | 当日可见消息 | 最终版发布后 | 其中应急 | 其中独立主题 |
|---|---:|---:|---:|---:|
| 郑州大学 | 0 | 0 | 0 | 0 |
| 天津音乐 | 0 | 0 | 0 | 0 |
| 美美备用 | 3 | 0 | 0 | 0 |
| 西安天上人间 | 17 | 0 | 7 | 0 |
| 三亚 | 16 | 0 | 4 | 0 |
| 郑州楼凤 | 7 | 0 | 1 | 1 |
| 郑州学生会 | 9 | 1 | 3 | 0 |
| 天津一品楼 | 16 | 0 | 4 | 0 |
| 郑州师范 | 3 | 0 | 1 | 0 |
| 成都怡红院 | 0 | 0 | 0 | 0 |

合计71条，其中20条应急、1条独立主题。7个Task当日已有可见消息；大学、天津音乐、成都仍0，不能将任何Task当日全部目标完成或10Task整体production_fixed。[逐Task脱敏读回](evidence/ai-group-supply-fallback-20260910/task-final-readback.json)。

## 基本数量漏计修复验证

03:28:33西安typed15、缓存9、真实代码重算9；缺失的6条全部是已有成功Attempt与typed可见事实的签到，原memory.result缺少content_hash。最终版04:00:57快照，6个原Action全部通过不可变证据校验，西安缓存=重算=typed=17；target由正常worker在03:59:56刷新。没有手工回填、重跑或补发。[前后证据](evidence/ai-group-supply-fallback-20260910/quantity-credit-after.json)。

04:04:40天津一品楼由正常worker将部署前缓存12刷新为16；04:07:31学生会新增消息汇总刷新为9。04:09:03只读复核，**10任务缓存与真实重算全部一致**，分别为0/0/3/17/16/7/9/16/3/0。[全部数量最终读回](evidence/ai-group-supply-fallback-20260910/all-quantity-final.json)。

最终版新写入正例：学生会Action `88161586-e693-4546-8be9-cc34d4a1ffd6`，selection `8927cadd-58d6-4213-b858-347e5311dc1d`，账号11，provider_route_exhausted后签到。Gateway04:02:29.159586，typed可见04:02:35.252358；同账号成功Attempt、可计数量slot、FOP confirmed，memory成功结果保留content_hash/selection_id，正文/hash/身份及计数校验全部为true。04:06实时重算=typed=9，随后正常汇总刷新到9，无手工写入。[新发送计数证据](evidence/ai-group-supply-fallback-20260910/new-emergency-quantity.json)。

## 新入群事实与canonical投影

03:17:57一致只读快照（read_only=on，repeatable read）关联首修发布01:31:26后新Action、成功Attempt、Gateway实际引用、membership_observed、当前目标canonical成员link及GroupBotAdmission：410条全链一致，分布为楼凤9、学生会102、大学63、三亚77、天津音乐28、西安131。其中36条Gateway调用在第四轮版本03:12:09完成后开始；其它4Task本观察窗口无新事实，维持unproven。机器人记录仍处观察/规则未决等状态，不能把成员投影正确当机器人准入完成或发言已履约。

[入群完整验收表与证据](evidence/ai-group-supply-fallback-20260910/admission-after-release.md)。417条成功Action中另外7条因Gateway早于首修部署完成而排除，均已有成功Attempt和membership_observed，不属于缺事实或投影失败。

## 独立主题真实送达

郑州楼凤Action `8b1917e6-ac40-4648-83a7-5c923c306458`、账号72：topic_only原因listener_watermark_unproven；历史上下文、reply/interaction/turn均空，没有应急选择，真实候选校验通过且三份正文hash一致。Attempt `04c8980e-28f9-432d-a04f-14dcf11d999a`于03:25:22开始并success，typed fact `4a157564-556b-4e48-b166-ac4448d13f11`于03:25:29确认消息4174616，完整身份一致、FOP与coverage confirmed。证明无上下文独立主题路径真实可达；不代表人工语义质量验收。

[主题完整证据](evidence/ai-group-supply-fallback-20260910/zhengzhou-topic-e4.md)。

## 已确认的真实应急送达与失败边界

- 西安首条签到：Action `1c48c16f-781b-4dbd-9900-757168ea0119`，选择/Action/FOP版本均2且FOP confirmed；Attempt `e0419f16-eb86-4600-8c52-3c354d92c507` success，Gateway调用02:15:08.271185，可见事实 `16a1a7cc-ed5b-464d-b1d3-d9fb405c5872` 于02:15:13.956628记录，消息367890，tenant/account/action/attempt/obligation一致。原Job failed保留。该调用早于第二轮Deploy完成，单列为新运行版本真实送达，不纳入严格发布后计数。
- 三亚Provider未知交接：Action `bf62e492-0932-4456-92ae-ff1bbbf7eb98`，选择原因provider_result_unknown；Job仍为unknown，Action/选择/FOP版本均2，FOP confirmed。Attempt `e7d49d90-b98a-453e-b9ae-5cdccd7132c4` 于02:47:45.873778开始调用并success，可见事实 `b1fe8c02-c08d-4723-ba38-63e2fc9b1e7f` 于02:47:48.867494记录，消息185698，完整身份匹配。证明仅交接内容发布权，没有把原Provider未知改成完成。
- 后续西安签到、师范真实回复表情、三亚和天津一品楼应急均出现严格typed可见消息；最终数量随下表同一快照记录。应急只计基本数量，不计普通主题质量。
- 大学Action `d9cb81ff-9535-46fb-8dc6-cbc30402a021` 与 `3a748422-d069-4a70-b256-91e2aa3cd318` 虽有调用完成记录，后续均`post_send_intercepted`，没有可见消息事实，不算履约；另一条`account_shared_usage_unproven`在Gateway前跳过。

## 尚未闭合的条件

- 原10Task的自动加入/自动验证均启用，正式管理员解析到租户账号515，本地在线、未冻结且Session存在；远端目标群成员/邀请权限仍未证实。管理员“已配置”不能写成“救援成功”。历史救援存在目标无效、权限失败、closed_unknown及义务关闭跳过。
- 00:17的历史membership核查证明天津63项、西安4项同实际目标错投影，另7项缺匹配依据；本次未从旧成功回执直接回填当前成员/机器人状态。这些历史项仍需独立当前事实，不能宣称74项已恢复。
- 成都03:40:21只读追踪：03:33窗口动作`0d335a8c-e577-43ff-9d7a-cf929b652ef6`在内容生成前由C2准入跳过。账号213于03:32:54、03:33:41、03:34:29三条typed post_follow_visibility均ChannelPrivateError，最终observation_gap_limit_reached/abandoned_for_day；该Action没有Provider调用、Attempt、应急选择或可见消息，不能声明发送恢复。
- 成都显式监听账号63的Session失效仍存在；没有修改账号、创建登录或伪造监听成功。普通主题/应急不再将监听错误当整个群的数量阻塞，真实回复继续要求真实目标。
- 03:59:54后生成worker记录一条`psycopg.errors.LockNotAvailable`/`sqlalchemy.exc.OperationalError`，位置为结算时Task共享NOWAIT锁；并非generation_result_task_retired或emergency_message_memory_invalid。04:09:02只读检查原10Task仅1条generating Job，其租约至04:12:31尚未到期，关联Action pending/ready，没有过期生成租约。该日志与精确Job的关联及最终结算结果未证明，保留unproven，不能把没有过期租约写成异常已恢复。
- 冻结、离线、失效Session、未准入、C2拦截和远端未知仍按各自事实处理；原账号窗口和数量期限保持。未通过真实可见消息验证的项目维持blocked/unproven。

[成都当前准入边界完整证据](evidence/ai-group-supply-fallback-20260910/chengdu-final-boundary.md)。

## 证据与操作范围

只读脚本及分轮JSON保留在 `/tmp/ai-group-release-20260910/`，准入与管理员只读报告保留在 `/tmp/ai-group-check-20260909-2335/admission-rescue-report.md`；仅输出必要ID、时间、枚举、数量、布尔和哈希，不输出凭据、Session或正文。正式生产变更经master→release→GitHub Actions；未执行附加任务重跑、批量配置/数据回填或手动提前排期。原工作区两个未跟踪诊断文件保持，临时本地PostgreSQL已停止。

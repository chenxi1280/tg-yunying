# AI 活群正常发送恢复（2026-09-08 晚间）

- Intake：ai-normal-send-20260908，L3/P1。
- 用户要求：解决10个AI任务零发送，正常发送。
- 路由：prod-diagnosis → product → dev → qa → product → prod-diagnosis。
- 产品合同：统一引擎PRD§19.67；design_status=complete；完整§19.60的workload保护份额、持久公平cursor不据此声明完成。
- 状态：本地实现与定向验证，release_pending，production_fixed=false。
- 基线：代码9d32d5a9；19:11累计应发14,999、confirmed=0；19:16发送Attempt 110条均未调用Gateway。
- 工作树边界：开始时只有本任务上一轮未提交的零发送诊断Markdown；无其他dirty代码。

## 根因与实现

1. `dispatch_session_priority.py`在LIMIT之前按原同账号/tenant/业务日Session判定候选紧迫度；`direct_action_claims`优先当前合法时段、Task内优先临近关闭时段，Task间保留轮次再比关闭时刻，真实过期工作不占满首轮。排序不授予发送权限，原claim/Gateway复核保持。
2. `continuous_dispatcher.py`在生产dispatcher worker循环内持有executor/future登记，按真实空闲名额补领；完成事件唤醒，无整批等待。service正式drain入口传ID，事务不跨线程。单次显式drain与其他worker仍维持原调用生命周期。
3. `runtime_resources.py`把领取临时scope的原预约对象/token转交future，finally仅释放捕获的owner；新owner不能被旧finally释放。停机停止领取后等待已交付动作正常收口，再进入既有Gateway/未决停机验证。
4. `ai_generation_runtime_config.py`为unified非V2同样读取现有GenerationJob；`generation_timing_binding`冻结真实期限；`legacy_generation_timing.py`复用正式provider选择读取provider/model及凭据密文版本摘要/endpoint摘要，不保存明文、不构造V2 route_set，原审核与HTTP时间上限保持。
5. 群规划`_resolve_plan_group`在准入与日覆盖物化之前取得既有NO KEY UPDATE NOWAIT群互斥，避免此前直到内容冻结阶段才上锁。锁忙显式回滚本Task规划，其他数据库错误不吞掉。

## 反向线上只读检查

`/tmp/ai-send-candidate-proposed-v2.jsonl`在只读事务中执行新候选SQL，不claim、不持久化：两个分片各取13条，九个已有ready内容的AI任务全部出现在两个分片首轮集合内。查询及逐候选脱敏回读分别约1.47秒和0.51秒；这是有限样本，不表示所有负载下固定耗时。原分片0首轮大多为旧准入。

## 定向QA与代码审查

- 106项通过：持续派发、逐预约所有权、生成时限合法配置、worker角色/生命周期及共享执行合同（9.62秒）；正式drain入口与最终变更复验51项通过（6.40秒，含重叠用例）。
- 42项通过：候选排序、过期积压安全收口、fact_first履约与账号pacing接管（7.34秒）。
- 原有生成worker、活群成员准入、日覆盖与数据流回归在专用PG上68项通过（12.27秒）。
- 真实PG：群内容/监听Task反序竞争、日覆盖前互斥及外键引用共5项通过（7.64秒）；PG JSON Session窗口/UTC连接及群锁路径4项通过（7.02秒，部分重叠，不累加为唯一测试数量）。
- PG始终使用独立本机tg_yunying_test、专用临时schema和测试框架advisory lock；每次后端测试60秒硬超时。无线上测试写入。
- 审查：没有future-to-now、没有目标减量、没有unknown重放、没有新增并发上限或审批开关；SQL参数化，只有必要的持久Session读取参与排序。新文件按职责拆分，旧庞大入口仅局部接入。

## Release Gate

- release_authorized：用户要求修复正常发送，沿本项目既有master→release→Deploy Production闭环。
- 首轮候选9a803ef9 / Prepare run34222594372：仅旧测试`test_generation_timing_binding_bypassed_when_route_v2_disabled`失败，它要求保留本次被修复的unified跳过时限行为。已按§19.67更新为unified不得跳过配置校验，新增legacy合同保留验证；合法non-V2正式builder已有成功用例。47项相关回归通过（7.75秒），其余首轮CI分片及镜像全部成功，部署未触发。
- 修正测试后的candidate_sha / workflow / deployed_sha / live_anchor：pending。
- migration：not_applicable；生产配置变更/维护apply：not_applicable。
- 回退边界：已有正常调用/unknown/事实不能回放或清理；代码切回旧版本不能被当成业务补发方式。
- 部署核验：Actions终态、current目录SHA、各角色镜像/RELEASE_SHA、健康。
- 业务验收：从新部署实际完成时刻开始，逐Task核对ready/claim/Gateway/成功remote_message_observed/confirmed持续增量；本地QA与候选回读不代表发送成功。若出现下一个首断点，继续定位修复，不终止于部署成功。

## 第一轮发布后回读与继续修复

- candidate/deployed SHA：`2d53107da91285533442757a61f4934185160bbe`；Prepare `34223267310`全通过；Deploy `34223952247`成功，live anchor `2026-09-08T20:06:34+08:00`；current目录及19应用容器RELEASE_SHA一致，全部健康。
- 20:16回读：三亚有1条新调用及有效`remote_message_observed`；郑州大学3个Attempt success带远端message ID，但Action为failed、仅unknown事实，仍需查清收口；其余任务未通过。完整正常发送未恢复，production_fixed=false。
- 美美备用：时限缺失错误已解除，暴露非V2 slot没有generation_job_id导致HTTP scope KeyError。正式builder测试移除slot enrichment替身后复现同形失败；unified槽沿原payload传递Job身份，V2内容合同验证保持。相关52项测试通过（20.92秒）。该修正待下一轮完整CI和发布。

- 20:20–20:23根因补正：八个任务被context_stale终结的327个样本全为普通非回复且有原generation identity；其他仍pending的候选除1条外同样revision已前进。AI内容PRD§4.1、DF-347已规定自然漂移只观察，实际`ai_content_runtime`却无条件终结，故修正为普通冻结候选记录drift后沿原hash绑定；reply/turn/policy仍拒绝。经正式group binding入口的反例先复现失败，修复后与原runtime服务22项通过（4.95秒）；scope及生成阶段、capacity dispatch/记忆回归在专用PG上186项通过（38.32秒）；最终上下文矩阵23项通过。测试保护曾拒绝默认非测试库后，改用独立tg_yunying_test完成回归，未触碰线上库。郑州大学3条均为post-send probe not_visible，不能计正常消息；并非事实投影丢失。


## 第二轮发布与静默等待修复

- `deda8aa9222e4d2fb763cd7b4ffae7d694671c4b`：Prepare `34226201223`成功，Deploy `34226938035`成功，实际完成北京时间20:38:44；current及19应用容器一致，OCR和全部应用健康。
- 21:01只读回读：10个Task仍running，三亚当日confirmed=2，其他9个=0；本轮完成时刻后有效消息事实为0。郑州大学3次API调用成功但可见性复核不通过，天津一品楼1个unknown仍待核对。未执行历史任务重试或数据修复。
- 连续新真人消息使attention_quiet_after无限顺延，违反既有§19.4有界deadline。已补正§19.67实施合同：首次实际等待冻结当前配置max，持久保存于同一Action；后续只截断此项静默等待，全部其他校验保持，原候选/Job不变。
- 正式dispatcher入口持久提交并重载测试先复现缺少固定deadline；修复后attention与conversation 15项通过（4.17秒），包含连续新事件、重复领取、重新加载、提前静默结束与明确回复例外。design_status=complete，code_review=pass，qa_pass；production_fixed=false，待本轮完整CI/发布及真实逐Task验证。


## 第三轮发布与精确数据恢复预览

- 本轮代码SHA `e4fce79d204ed34ad41dfdfd7e3919983fcc0240`；Prepare `34229528061`全成功；Deploy `34230307017`成功，部署Job完成21:13:13，workflow终态更新时间21:13:14，后续业务回读使用21:13:14作为保守anchor。current目录为`20260908131036_e4fce79d`，19应用容器RELEASE_SHA一致且healthy，OCR healthy，API HTTP 200。
- 21:15–21:16回读：新anchor后已获得三亚1条有效`remote_message_observed`（调用21:14:32、可见事实21:14:35）；固定attention等待均180秒，尚未证明所有群持续发送。其他任务仍有source pacing、账号忙、topic容量合同及熔断阻塞。
- 美美备用仅3个已准入账号的日覆盖被`generation_contract_error / generation_contract_repair`永久阻塞，账号ID 29/39/346；同义务全部历史Attempt=0、remote facts=0、open Actions=0；旧Job/Action failed，quantity与projection仍open，原错误均是已修复的non-V2 generation_job_id KeyError。
- 只读预览的coverage IDs：`751d7670-5781-43b7-8b7d-9b7fea02aad3`、`898e5485-ea20-4d6b-bc57-223cc66ad9b5`、`e7c0cb5d-6410-45d3-9a93-4d85a2a91983`。预览摘要`10be7c31b404146236ef2076ba5f8c335bb3dd3257c8b7078650e141060b45aa`。
- 精确恢复工具已准备，默认preview；apply要求操作者、明确批准引用、预览摘要、线上SHA、同日/Task身份状态/epoch/coverage旧值和无reserved/inflight/fact复核；锁定Task/coverage/历史Action后只将3条coverage转ready并写AuditLog。原Job/Action保留failed，配置与其他记录不改。仅本地事务验证和生产只读预览已执行，apply尚未获明确批准，persistence_status=preview_only。§5.3要求运营确认合同修复并审计，因此已向用户提出精确确认，等待答复。
- 熔断回读：20:38:44至21:09期间40次独立健康探测全部超时，15个proxy route和5个account处于probe_transport_failed open；还有1个proxy route为unknown_after_send open。此为探测结果，尚未定位为某个网络设备或代理根因；没有绕过熔断或修改代理配置。

- 21:18新回读：新anchor后三亚与西安天上人间各1条有效远端消息事实。西安该Action有固定attention deadline并最终success，证明有界等待后完整发送链实际通过。21:18:59回读三亚当日confirmed=4、西安=2，其余8任务当日confirmed=0；其他群多条Action已越过attention进入来源节奏、账号使用证据或容量检查。该结果只能证明部分链路恢复，不能声明10任务production_fixed。

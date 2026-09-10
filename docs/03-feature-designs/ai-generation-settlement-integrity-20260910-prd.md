# AI生成结算一致性修复（2026-09-10）

## Intake / Bug Batch Plan

- intake_id: AI-GROUP-LOGIC-REPAIR-20260910；L3/P1。
- 用户要求：解决线上群活跃执行问题；明确本次为通用逻辑代码错误修复，不以历史任务名称或用途记录替代当前需求。
- 阶段：prod-diagnosis → product → dev → qa → product → prod-diagnosis。独立分支 `codex/ai-group-logic-repair-20260910`，基线 `af602f73`；主工作区三个未跟踪文档保持。
- 本批按根因分组处理；不把每一种错误码直接等同于独立代码缺陷，不把等待、发送回执或服务健康计作完成。

| 组 | 当前证据 | 当前设计/处理范围 |
| --- | --- | --- |
| G1 生成发布半提交 | 生产972597a4的Task NOWAIT锁冲突发生在Action释放提交之后；Job仍generating，租约恢复将ready Action对应Job取消并作废候选窗 | 完成同事务发布与已准备结果的精确恢复；不得重新调用Provider或发送Telegram |
| G2 阶段字段溢出 | 生产3个generation worker均出现varchar(32)溢出；mark_emergency_pending将33字符原因topic_only_topic_evidence_missing写入generation_stage | 阶段与原因分离，沿用既有emergency_pending阶段；完整原因存于Action结果和Job证据，不截断字符串、不扩展数据库字段 |
| G3 路由证据缺失 | generation_contract_error中的AiContentJobBindingError指纹对应context_route_evidence_missing；topic前置只用meaningful_context_text判断，后续绑定使用fact_id_map，清洗后无事实的文本被前置误认为可用上下文 | 对v2使用与绑定入口一致的可用事实检测；既有配置话题分支继续原授权，不补造上下文、不放宽路由与内容规则 |
| G4 可见性、准入、排期、Provider、计数投影 | 上一快照有post_send_intercepted、准入等待、生成/排期等待和目标投影差1 | 继续逐层只读定位；仅复现出的逻辑缺陷进入对应修复，不从汇总数推出单一原因 |

## G1 产品/数据合同

1. 普通并行生成准备成功后，原Action解除生成claim与原GenerationJob进入ready必须由一次数据库事务提交。任一身份验证、CAS、数据库锁或提交失败时全部回滚，错误明确暴露；不能先让Dispatcher看见pending/ready，再结算Job。
2. 使用原tenant/Task/lifecycle/obligation/Action/Job绑定，原owner、token、lease epoch和版本验证。当前同owner同epoch的context/window冻结导致job_version合法增长继续支持；新owner、epoch或候选身份变化拒绝旧结果。
3. 完成入口按Task→Action→Job读取锁定，并保留原Task退役检查。重复完成只允许同一结算身份、候选hash和Job epoch的已提交结果读回；不能让同owner的另一Action被批量释放。旧版本遗留的pending/ready与generating Job仅在现有完整身份和无claim争用时精确收口。
4. Job租约到期恢复时，区分“已准备完整结果”与“还在生成”。同Task、当前epoch、原Action/Job、内容hash一致，且原候选窗仍属于此Job的ready结果，只修复本地生成状态；保留正文、原due/release/deadline/数量槽、窗口与原Provider证据。已发送、unknown、gateway_bound仍走原远端对账，不进入本地准备恢复。
5. ready恢复不得为缺失/被作废窗口重建发送权，不恢复历史cancelled/failed Job，不改写已有终态。已误取消历史的纠正需单独当前证据与精确恢复合同，本补丁不批量复活存量。
6. 非ready的原生成中/Provider未知/暂停退役/失配/缺Action路径保留既有行为与显式失败。锁冲突不会变成业务成功，后续沿原租约恢复周期收口。

## G2 阶段与原因合同

- `generation_stage`保存稳定阶段`emergency_pending`；具体触发原因完整保存于Action的error_code/generation_stage及Job evaluator_evidence的emergency_generation字段。
- 不改变是否允许应急、内容选择、账号/数量/回复身份和Gateway条件；不添加新的降级，不缩短原因，不吞掉数据库错误。
- SQLite不强制varchar长度，必须补真实PostgreSQL回归验证实际写入及事务行为。

## Product Design Complete / 反向检查

- G1/G2 design_status=complete；G3按下节反例resync完成设计；G4仍为diagnosis，不能声明整批已闭环。
- 原始需求、失败路径、owner/CAS/窗口/未知边界已覆盖；无前端/API/schema/配置修改，现有错误展示继续使用完整原因。
- 真实入口：ai_generation_parallel_settlement、ai_generation_claim_lifecycle、ai_generation_job_finish、ai_generation_recovery、ai_group_emergency_pending。旧release_prepared_batch→finish_generation_job两个事务可复现半提交；旧恢复只识别generating正文状态，缺少ready分支。
- QA：完成后Action/Job同时可见；Job CAS或提交失败Action仍归原owner；Task锁冲突零部分提交；同claim幂等、错token/epoch/候选hash拒绝；过期ready保留原窗口、错误hash和失配窗口拒绝；Gateway unknown不恢复；阶段长原因PostgreSQL持久化保留全文。
- 发布：定向测试及真实PG、代码审查、完整Prepare、master→release→GitHub Actions Deploy Production。独立核对SHA/runtime，再核对新生成结果是否有半提交/误取消与新的typed消息事实。仅本地测试或新消息一条不代表全量目标达标。

## 发布与回滚

无迁移或存量apply；候选新JSON证据字段向后可读。回退应用版本会重新暴露半提交和字段溢出，不改历史已提交数据。生产不手工更改Job/Action、不重放unknown；正式worker按既有claim/reconcile流程消费补丁。

## G3 resync：可用上下文检测与真实绑定一致

- 08:38只读反查：部分错误Action旧payload的meaningful行数非零，但fact_id_map为零；另一些旧payload的fact_id_map非零，因失败后事务回滚保留旧payload，不能把它当失败瞬间输入或断言全部同一根因。
- 中性反例：仅有“今日发言量/群内排名”通知、纯链接或无法形成安全事实的群消息，前置将中文文字存在当作可用真人上下文；绑定清洗后实际无事实。存在已配置“周末运动”话题时，本应沿已存在topic_only分支，却走入context_route_evidence_missing。
- v2前置采用与绑定相同的fact_id_map判断；配置话题也按同一实际事实判断能否绑定。普通字数/中文判断仍用于legacy分支，不修改sanitize规则、授权路由或内容范围。
- 仍只有原合法direct、未准备正文、无回复/互动/会话引用、同内容scope身份的工作可使用已有配置话题；reply/foreign reference保持原拒绝/等待，不能借此清空真实引用。
- 无可用配置话题继续已有显式错误及已授权的应急合同，不生成默认话题、不新增降级开关。真实输入仍有可用事实时沿原正常上下文。
- QA验证：中性系统通知/链接清洗后为空时选既有话题；同样输入有真实reply时不切换；清洗后为空的话题在绑定之前明确报错；legacy及真实普通聊天不变。

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

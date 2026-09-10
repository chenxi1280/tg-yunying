# CPU/内存热点修复：阶段与Release Gate

- intake_id: cpu-memory-hotpath-repair-20260910
- classification: L3
- owner/merge_owner: 本任务
- base: 85469be6
- locked_paths: engagement_natural_opportunity.py、新presence查询投影模块；ai_message_memory.py、ai_message_window_dedupe.py、新查重查询模块；定向tests；专项PRD及结构/数据索引。
- PRD/设计反查：complete；无迁移/线上历史维护，不改变锁、worker轮询和容量。
- Dev：complete；代码自审通过。已有96项热点及范围回归通过；补充91项回归通过（与前批有重叠）；真PostgreSQL4项通过。每批后端硬超时60秒。
- 隔离6000条、每条约8KB无关JSON的SQLite对照：旧查询峰值103.87—104.01MiB，新查询2.79—2.83MiB，均命中600条；三次耗时旧0.366/0.415/0.382秒，新0.179/0.164/0.485秒。仅证明查询分配减少，不能换算生产RSS或CPU；完整原始证据在本地发布报告。
- Release Gate：源码定向QA通过，冻结候选后重新执行制品QA、前端构建和镜像平台/hash校验；生产验证pending。

- 第一轮1e9358f8发布通过：19应用容器、34配置指纹、API/static/OCR/合同验证通过；Planner栈不再解码完整Action JSON。第二轮resync：Listener全状态remote_id扫描、按群记忆排序扫描、retention COALESCE(EXISTS)关联子查询，PRD已补齐；追加locked_paths=engagement_unowned_activity.py、group_ai_chat.py近期记忆投影入口/新模块、runtime_retention_selection.py、模型索引/0233迁移与专项测试。不得弱化保护或跳过业务来消除等待。
- 第二轮自审/QA：45项相关单元通过，新增retention三值逻辑等价后19项通过（重叠）；真PG首批6项通过（含0233迁移），第二批6项通过（含12000行索引计划/低于1秒点查和5项真实并发FK保护）。查询保护谓词、消息状态/peer范围与去重不变。线上antijoin试算仍触发15秒只读超时，当前仅确认计划可优化，耗时需发布后复测；未以EXPLAIN成本替代实测。

- 第二轮由合并发布1129407a实际安装：独立读回19容器、34配置一致、0233索引valid/ready，真实查询4.1ms/55.4ms；不得重装此前被维护锁拦截的2df94a9d候选。
- 第三轮prod-diagnosis -> product complete/resync -> dev：线程栈确认预关注Gateway持有父Session长事务。locked_paths追加task_prejoin_channels.py、Dispatcher异常传播入口、prejoin定向测试及相关合同/索引。快照释放事务并在结果后核验原认领；成功事实保留，旧调用不投影新owner状态。17项单元/准入回归通过，真实PG及Release Gate待完成。

- 第三轮定向QA：17项首批准入/事务单元、45项运行资源/持续调度/关注/退役回归通过；独立PostgreSQL2项通过，实测RPC期间父连接xact_start为空且另一连接可NOWAIT取得账号行锁，认领替换后成功事实仍持久且不覆盖新token。新增无待处理频道/异常传播测试随冻结制品QA复验。没有生产数据改写；Release Gate允许进入本地制品准备，业务验收仍pending。

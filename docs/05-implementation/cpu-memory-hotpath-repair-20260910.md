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

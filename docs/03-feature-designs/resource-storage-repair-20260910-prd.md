# 数据库存储与服务器资源修复

## Intake 与范围

- intake_id: resource-storage-repair-20260910
- 用户要求：修复已诊断的数据库磁盘/内存、服务器 CPU/内存问题，并判断 Redis 的适用数据。
- level: L3；阶段：prod-diagnosis → product → dev → qa → product → prod-diagnosis。
- 本批根因分组：R1 Mihomo 匿名卷遗留；R2 无效 Action 索引；R3 准入路径重复存储与不必要的大字段装载。
- 不把短时 CPU 波峰当持续饱和，不声称数据库内存异常已经定位；不修改其他项目、账号资格、任务数量、Provider、权限语义或 unknown 状态。

## 当前证据

2026-09-10 15:07–15:12 +08，应用 current 为 09393075：4 核/7474 MiB，总盘 40G/已用26G；可用内存694–819 MiB，swap1290–1366 MiB。Planner约536 MiB，两Dispatcher合计687 MiB，三AI生成合计803 MiB。CPU短时有波峰、无持续换页证据。

PolarDB数据库20GB，actions4953MB（TOAST2518MB、索引1892MB）；planning_admission_snapshots2399MB（TOAST2350MB）。最近200个准入快照只有84份不同账号路径，按已存储字段大小计算重复11,214,441字节，去重后5,781,946字节。重复发生在不同来源/观察版本，不能删除原快照身份。

273个无任何现存（包括停止）容器引用的匿名卷合计8,161,808KiB，目录结构均为Mihomo数据目录；尚须文件指纹、来源和备份核对才可删除。无效索引ix_actions_hard_hourly_history_scheduled_ccnew约181MB，indisvalid=false/indisready=true，对应正式索引valid/ready。

## 产品合同

### R1 卷生命周期

恢复Mihomo时使用按容器名固定的项目数据卷，复用同一目录，避免每次重建产生匿名副本。既有在线容器不重建、不换代理。历史清理仅针对精确冻结的匿名卷，检查全部容器引用、内容清单/hash、Mihomo可重建文件特征和当前release。清理前保留可恢复的内容备份及卷元数据；未知文件、符号链接、引用或内容漂移均不删除。apply逐个重新核对，Docker拒绝在用卷，禁止force/prune。审计保存每项结果，中途失败不宣称整体完成；独立读回卷清单、在用容器身份及实际磁盘。

### R2 索引

只处理上述精确无效索引，冻结OID、定义、valid/ready、对应正式索引和release。使用并发DROP，有限lock/statement timeout，不重建/删除其他索引。执行前确认没有同索引维护；保存DDL及审计。读回目标不存在、正式索引仍valid/ready和实际关系大小。此动作不删除Action/Attempt/履约证据。

### R3 准入路径去重

增加租户隔离的不可变PlanningAdmissionEvidence，按规范化account_paths内容SHA256寻址。原PlanningAdmissionSnapshot仍保存原id、租户、Task/epoch、participation、horizon、dependency hash、decision hash、valid_until、账号集合和created_at，仅大字段引用共享证据。同租户相同路径复用；不同租户不复用；任一路径内容变化产生不同证据。唯一键与数据库冲突处理保证并发只产生一份；不引入TTL复用资格，不减少真实资格重查。

混合读：旧行保持原account_paths；新行保存evidence引用和空legacy字段，模型account_paths返回完整原列表。引用不存在显式报错，不返回空成功。列表顺序、值、hash及资格语义不变，所有业务外键继续引用原snapshot。运行路径只需要admissible_account_ids时直接选择该字段，不装载路径及整个ORM对象。

迁移仅新增表、可空引用和约束；不在DDL迁移中改写历史。上线前停止旧业务worker，再迁移/backend更新，最后启动新worker，沿用现有发布fence。首个新引用生成后不能回滚到不识别引用的旧代码；仅forward fix，或先经有审计的完整物化还原并核验。

2026-09-10开发反查resync：准入代码还会对浏览/点赞无条件查询人格，并读取每账号所有历史人格完整JSON后在Python丢弃旧版本。修复为仅内容任务查询人格、在数据库按租户/账号取最大version并只读取准入需要的id/account/version/status/quality/summary字段；最新版本不合格仍按原合同阻塞，不回退旧active版本、不缓存资格。测试要求未加载历史/无关JSON、跨租户过滤、最新失败优先及准入结论一致。设计复核complete。

历史去重另走精确snapshot ID的preview/apply/readback：逐批锁行重新校验指纹；完整路径写入证据后才清空旧字段，引用、原身份及全部其他字段保持不变。原始逻辑内容hash前后守恒；审计写同一事务，失败回滚。中断只重新preview剩余legacy行，不重放旧manifest。只做表示转换，不删除任何快照/Task/Action/远端事实。普通VACUUM只报告可复用空间，物理文件下降必须独立实测；不执行VACUUM FULL。

### Redis 决策

Task/Action/Attempt、RemoteInvocationFence、准入决策证据和防重身份保留PG。现有主机内存紧张，不把数GB冷历史直接迁入同机Redis。Redis可用于可重建的展示聚合或纯函数结果，但本批未定位到具备完整版本失效合同的此类瓶颈，不新增缓存、静默回源或双写。R3先消除重复数据和无用读取，Redis不是本批验收前提。

## QA、索引、发布和验收

- 单元/集成：新旧快照读取等价；路径顺序/值不变；不同租户和内容隔离；并发同内容唯一；回滚无孤儿；scalar查询不取路径；混合数据业务准入原测试通过。
- 历史转换：指纹漂移零写；转换前后完整路径hash与业务字段一致；审计同事务；重复apply拒绝；原快照引用不变。
- 卷：在用/有额外文件/内容漂移不删除；精确对象审计与读回；固定卷复用。
- 发布：定向pytest每批60秒；真实本地PG迁移/并发；代码自审；master→release→local_release prepare/deploy；独立SHA/runtime读回。成功后沿用上一轮镜像清理合同。
- 资源验收：分别报告磁盘物理回收、PG逻辑重复减少、查询投影、在线内存/CPU前后同口径采样。工作负载/重启改变时不能把RSS下降全归因代码；业务副作用零新增，不以容量改善宣称Telegram履约恢复。
- 更新project-dataflow-index/project-structure-index及PRODUCTION_RUNTIME；Release Gate及审计引用为resource-storage-repair-20260910。

## Product Design Complete

原需求、混合读/调用方兼容、后端事务、租户隔离、并发唯一、错误路径、历史转换保护、不可逆回滚边界及验收已闭合。无前端交互变更。design_status=complete；实现中发现差异需resync。

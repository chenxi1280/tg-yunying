# Findings

## Production facts at 2026-08-13 10:33 Asia/Shanghai

- 当前生产 release: `/data/tgyunying/releases/20260812025114_14bebb2b`，镜像 SHA `14bebb2b15d5d1391ac05d9ee3307a16e5e28a16`。
- 主机 4 vCPU / 7.4 GiB / 无 swap；重启后 26 分钟 load average 约 `9.47/9.35/8.39`。
- `vmstat` 运行队列 17–36，CPU idle 连续为 0%，主要是 user CPU；无 OOM、磁盘 66%、无等待锁。
- PostgreSQL 容器瞬时约 255% CPU，Planner 约 65% CPU；PostgreSQL 连接约 50。
- 活跃 SQL 来自 dispatcher 容器 IP，形态为读取完整 `Action` 并对每行相关查询最新成功 `ExecutionAttempt.remote_message_id`；相同 SQL 多并发。
- `actions` 约 143,503 live rows / 27,520 dead rows；数据库启动后约 24,465,469 blocks read，说明重启未消除查询压力。
- 无 PostgreSQL blocking pid、无 waiting lock、无 deadlock；事故不是行锁链。
- 重启前 Docker 健康检查反复 `cannot exec in a stopped state`，应用/API/SSH 变慢属于主机高压后的级联结果。
- SSH 公网存在持续密码扫描，但当前 key 登录正常；本地早期 banner timeout 的额外原因是 Clash TUN 路由，并非全部为服务器 sshd 故障。

## Code boundary

- `group_ai_scope.successful_own_history_reply_facts` 构造相关 scalar subquery，并在 SELECT / 非空过滤 / 精确 remote id 过滤中重复使用。
- 调用点覆盖 Planner reply pool、AI generation 本地 guard、Gateway scope validator；多 worker 会并发放大。
- 业务语义要求同 tenant/Task/group、成功 Action、冻结正文非空、最新成功 Attempt 远端 ID；跨 Task 在途占用在 limit 前排除。
- 修复必须维持该语义，不允许以 Action.result、listener 上下文、缩小目标或跳过去重替代。

## Working hypothesis to verify with EXPLAIN

当前相关子查询在生产规模与 JSON group 过滤、跨任务占用排除组合下产生高成本重复扫描；需要让查询形状显式从有界候选 Action 集合出发，再读取候选的最新成功 Attempt，并为精确 remote-id guard 使用 Attempt 主导的窄查询。

## Contract drift boundary

- 最新长期 PRD/数据流把最终权威提升为 canonical send remote fact + bound quantity fact binding。
- 当前生产 SHA 与 `origin/master` 的实际 reply guard 仍读取 Action + latest successful ExecutionAttempt；本事故不混入未完成的合同切流。
- 代码修复保持当前 guard 语义，生产关闭仍要求 canonical typed remote fact；后续合同切流必须单独经过 inventory/backfill/fence/E4。

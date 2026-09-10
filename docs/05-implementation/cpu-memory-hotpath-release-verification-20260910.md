# CPU/内存热点修复：发布与生产验收

验收时间：2026-09-10 19:24（Asia/Shanghai）。当前应用SHA：`d69a6a261abcd9b9168f3b2f6b0b7e1a08d4ccca`；生产目录：`/data/tgyunying/releases/20260910191310_d69a6a26`。本报告后的文档提交不改变实际运行SHA。

## 结果与边界

本轮已完成三轮热点代码修复、定向QA、本地镜像直传和生产读回。确定性查询分配、两处索引访问和预关注远端等待的事务边界已验证。整体CPU固定降幅仍为unproven，其他秒级数据库锁等待仍可出现；不能把本报告解释为全部任务日目标已经履约。未修改历史Task/Action状态、重放unknown或执行额外索引维护。

## 修复与证据

| 路径 | 修改 | 验证 |
| --- | --- | --- |
| Planner presence | 全量Action/JSON对象改为所需标量与时间列投影 | 6000行隔离对照，旧查询分配峰值103.87—104.01MiB，新查询2.79—2.83MiB；600条命中和统计一致 |
| AI去重 | 同一次判定复用账号窗口，exact/template只读身份列 | 保留租户/账号、窗口、状态与预占唯一约束；下一次判定和发送前仍新鲜读取 |
| Listener归属与群记忆 | 0233补全状态remote-message索引与tenant/group/time索引，近期记忆仅投影所需列 | 19:21实际归属点查3.03ms，实际运行群5828最近120条1.94ms；两索引valid/ready，计划命中相应索引 |
| Retention候选 | 非NULL EXISTS移除COALESCE，允许反连接 | 真实PG候选等价及引用/并发保护通过；保留全部删除前保护，未执行历史删除。本批没有给出稳定生产耗时承诺 |
| 预关注频道 | RPC前保存不可变快照并提交，RPC后刷新认领/生命周期/账号；认领变化保留成功事实并停止旧调用 | 真实PG证明RPC期间父连接xact_start为空、另一连接可NOWAIT获得账号行锁；生产线程采样仍有778个预关注等待样本，19:17/19:19/19:21三个DB快照对应admission_facts长事务均为0 |

预关注修复前，曾有4个对应idle-in-transaction持续279—296秒。19:08发布前补充快照仍有8个对应事务；发布后上述采样没有该事务形态。19:21仍有约6秒的其他锁等待，因此只确认已定位路径修复，不声明系统无锁等待。

## 资源观察

相同20秒进程采样口径：Planner修复前RSS598492KiB（584.5MiB），第三轮后289012KiB（282.2MiB）；RSS差约302MiB。前后存在worker重启、自然任务变化及另一已授权维护任务清理工作量，不能将全部差值归因于本批。隔离查询分配下降是更直接的代码因果证据。

Planner CPU单核口径此前34.51%，第一轮曾9.01%，第二轮43.42%，第三轮39.70%。这些短窗口并非等负载，不能宣称固定CPU降幅。第三轮Dispatcher仍在实际执行准入/发送，未通过降低并发、延长轮询或暂停Task制造低资源指标。

## QA、制品与运行

- 第三轮冻结SHA的64项回归通过（19项事务/准入、45项运行资源/持续调度/关注/退役）；另外2项独立PostgreSQL并发事务测试通过，0001→0233建库迁移通过。每批后端测试硬超时60秒。
- 前端构建通过；schema-v2 linux/amd64三镜像包612861453字节，SHA256 `d3a5669709e28a22c5125189cfcb04d6e6f34377ec3cad4ac0c78791466a2eec`。
- 安装后19个应用容器、实际Image ID、API/static/OCR和合同检查通过；34项原运行覆盖参数指纹完全相同。
- 第一轮1e9358f8及第二轮2df94a9d的改动均为当前SHA祖先。第二轮2df94a9d直传曾被另一维护任务的操作锁挡在安装前；随后由合并候选1129407a发布。未重放旧安装或回滚该合并版本。
- 上一轮成功版本1129407a的服务器镜像/包已由安装器核对并清理，本地相同版本的归档及未引用镜像也已精确清理；共享的当前前端Image ID保留。此前本任务成功版本1e9358f8的本地归档/未引用镜像由正常清理指针处理。失败候选和审计清单不作为清理目标。

## 当前版本的真实发送链

19:21:55只读确认一条发布后新Gateway调用：

`Task 8d64449d-994e-4d46-969e-9349f49066ba -> ledger d4161690-1fcd-42c3-901a-853988c863f3 -> coverage obligation 50272294-d5aa-4ea6-a42a-838268867dbf -> Action f1419cb8-6193-4d78-9aa7-d0e7d2760b0b -> success Attempt / Gateway 19:18:51 -> remote_message_observed typed fact -> visible_confirmed 19:19:00`。

Gateway调用发生在两个新Dispatcher启动（19:16:23）以后；存在远端消息ID，未把旧Attempt或历史投影当成新版本调用。此证据只证明该实际发送链和本批业务冒烟，不能代替10个运行活群的全部日目标验收。

日志另有频道浏览路径的`AttributeError: NoneType.connect`，不在本次修改路径；尚未证明其根因或修复，不能声明所有业务错误已消失。已检查的Planner/Listener/Dispatcher/metrics/backend日志未见本批SQL schema、索引定义或数据库完整性错误。

## 证据位置

本地证据目录：`/private/tmp/tgyunying-hotpath-release-evidence-20260910/`。

- `release-prejoin/`：冻结QA、镜像hash/ID、deployment/runtime、34参数指纹、清理报告。
- `query-prejoin-after.json`：索引状态、计划与实际查询耗时。
- `prejoin-after-1.json`、`prejoin-after-2.json`、`prejoin-after-3.json`与`prejoin-after.raw`：数据库事务和真实RPC等待栈。
- `process-before.json`、`process-prejoin-after.json`：进程RSS/Swap/CPU原始样本。
- `business-prejoin-after-2.json`：新版本完整真实发送链。
- `error-shapes-after.json`：未闭合的其他业务异常形态。

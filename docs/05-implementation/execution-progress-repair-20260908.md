# 执行前进性修复交接

## Intake与Bug Batch Plan

- 用户要求：检查并修复影响任务完成量、死锁和无法执行的问题；2026-09-08明确“你来修复这些问题”。
- 分级L3；阶段prod-diagnosis → product → dev → qa → product → prod-diagnosis。当前不操作生产数据，不重试Telegram历史unknown。
- 基线：本地/生产50ecee24；工作区已有9份本会话授权的完成量/拟人化PRD改动，保留，其未实现条款不宣称在本批完成。
- 责任：本会话顺序执行各阶段，不启动并行可写Agent。锁定本批代码/测试及统一PRD、结构/数据流索引；不触碰其他工作树。

| 分组 | 缺陷与最小修改范围 | 验收 |
|---|---|---|
| D1 锁序 | Attempt/资源实际账号先排他锁，避免共享锁阻塞升级 | 真实PG双执行者不发生40P01；冻结/容量准入保留 |
| D2 恢复 | 原预算未分配缺口读取恢复后的供给、创建唯一successor | 释放/恢复后补原缺口；重复和并发不重复分配；历史占用/期限保留 |
| D3 局部竞争 | 批量资格busy逐账号反馈，健康账号继续，分母保留 | 常数SQL批量、busy隔离、恢复与失效不混淆 |
| D4 唤醒 | 持久父子交付、每Task独立事务、错误显式状态 | 失败前缀不饿死健康工作；重启不重复交付 |
| D5 浏览领取 | 修正daily identity过早call_issued及原未调用投影安全结算 | 准入拒绝/历史未调用可结算；called/unknown/fact绝不误释放 |

## Product Design Complete

合同见统一引擎PRD§19.66。原需求、状态流、当前数据库可实施性、幂等/并发、资格和远端证据边界、期限、错误可见性、QA与发布均已补齐；不增加外部队列、API或业务成功通道。`design_status=complete`，`resync=true`。以上五组已完成最小实现、审查与定向QA；`implementation_status=implemented`，`production_status=unproven`。

只读D5证据：Action 5173a2be-1726-4b42-a7f6-68493733525f 的daily owner call_issued，4条Attempt均为未调用的skipped_before_gateway，其中一条task_lifecycle_admission_busy；资源fence为terminal、started_at=NULL、safely_not_called；Gateway journal和通用远端fact为0。正式修复仍需每次锁内复核，不以此快照作为未来任意释放依据。

## 实现与审查

- D1/D3：账号、current authorization和online identity按稳定顺序批量共享锁；busy只影响对应账号且保留参与分母。实际Attempt先取得账号排他NOWAIT锁，资源入口复用同一锁。冻结写入与最终调用资格仍互斥。
- D2：同需求锁原plan并重读最新active successor；只追加原未分配缺口，已released预约、called/unknown及原deadline不变。原预约/计划数量不一致显式报错；需求改写即使原分配为零也不能绕过旧身份。
- D4：原Outbox事件先expanded并持久化每Task子事件，再分别锁Task、核对binding与epoch并交付。锁忙保持pending并后移，非法/异常明确invalid/failed，父事件等全部子事件结算；现有close_turn消费者只读取自身stage，不受新增状态影响。无表结构、外部队列或迁移变化。
- D5：先运行Task/冻结/账号准入检查，再用savepoint原子写资源call-issued、浏览daily identity与Attempt call-start。历史安全结算要求锁Action并核对全部Attempt、journal、fence和完整浏览身份事实；只追加safely_not_executed，不写浏览成功。
- 审查修正：补原零分配需求改写检查；最新successor查询增加确定性排序；资格快照不得把busy计为admissible；Gateway写入savepoint不得回滚准入拒绝产生的未调用证据。
- §19.65及上一轮拟人化/完成量设计改动一并保留；它们的其余设计条款未在本批实现，不能以本批发布替代验收。

## 定向QA

所有后端测试使用backend/.venv，每次pytest进程硬超时60秒。PostgreSQL使用独立临时PG16集群、显式tg_yunying_test和原测试advisory lock，无生产/已有开发数据库写入。

| 检查批次 | 结果 | 本机日志 |
|---|---|---|
| 资格、SQL规模、原成员基础 | 70 passed | /tmp/execution-progress-first-tests.log |
| 原组合预算与新恢复/规模 | 27 passed | /tmp/execution-progress-portfolio-tests.log |
| 成员基础及持久子交付 | 21 passed | /tmp/execution-progress-wake-tests.log |
| 原浏览生命周期、退役、资源 | 47 passed | /tmp/execution-progress-view-existing-tests.log |
| 新浏览安全证据与预算恢复 | 20 passed | /tmp/execution-progress-latest-unit.log |
| 真实PG：锁序/局部busy/预算并发/单Task锁与原资格/组版本 | 27 passed | /tmp/execution-progress-pg-combined.log |
| 冻结、Gateway journal、接管、共享容量、跨日浏览、资格回归 | 104 passed | /tmp/execution-progress-final-regression.log |

批次有交叠，以上不相加冒充独立用例数。PG有一条原Alembic path_separator弃用告警，无测试失败。测试证明本地修复路径，不证明生产完成量。

## Release Gate

- message_id/intake_id：execution-progress-repair-20260908；level=L3；release_mode=github_actions；release_owner/rollback_owner=本会话；status=pending。
- 本地定向QA已通过；编译、diff检查及完整CI按候选提交记录。
- frontend/API：无代码改动，Actions仍执行既有前端build；Schema无变化，现有0228迁移不变。
- worker影响：Planner预算恢复与成员唤醒，Dispatcher账号锁序与浏览未调用结算；正常worker消费新代码，不执行额外生产维护apply或重试unknown。
- 外部平台：仅原合法Action经过全部原准入后调用Telegram；本批无新增消息/探针或测试账号操作。
- rollback_plan：不回退0226/0228。新增Outbox子事件须由理解该stage的代码交付，故优先向前修复；不能未经读回直接回滚到忽略expanded/子事件的旧应用。
- observe_window：以真实部署完成时间锚定，读取current SHA、runtime、锁/领取错误、原问题Action安全终态与后续实际工作；按四类Task分别读取Attempt/Gateway/typed fact。
- immutable candidate / Actions / deployed SHA：待发布记录。
- runtime / 四类业务事实：待发布后只读核对；当前production_unproven。

## 首轮完整CI修正

候选3490844f的Actions 34206131377在backend-postgres-checks (0)失败，未进入镜像/部署。旧共享预算并发测试仍断言阻塞锁，现改为锁占用时account_execution_busy、提交释放后重新准入仍拒绝超预算，保留原预算守恒验收。新增唤醒并发测试暴露UTC数据库连接下naive北京时间被解释为UTC的问题；父事件生产与drain当前时刻改用显式Asia/Shanghai aware时刻，新增UTC/Asia/Shanghai连接参数化回归。没有修改/放宽业务数量或资格。

修正后定向QA：24项真实PG（共享预算、执行前进性、组版本）通过，日志/tmp/execution-progress-ci-repair-pg.log；32项成员版本/基础/唤醒通过，日志/tmp/execution-progress-ci-repair-unit.log。

## 生产反查再次resync（17:11）

fae502c5已通过Actions 34207051818全部检查并于17:09:25完成部署；current/SHA、19应用容器healthy及内外health均通过。原浏览Action已skipped，daily owner available，新增一条safely_not_executed且原4条未调用Attempt保留。新版本没有重复浏览领取释放错误，但Planner仍有channel_membership._eligible_membership_candidates的整批NOWAIT失败，D3尚有运行成员前置入口未改到；日志链路覆盖AI活群/点赞/浏览。回到§19.66.2补齐：运行前置候选skip-busy，全busy保留等待；启动全量路径显式strict。补真实PG成员gate/恢复/严格启动回归后重新发布，当前不可写production_fixed。

成员gate补修QA：6项真实PG通过（新增健康继续/全busy等待/严格启动、原启动PG），日志/tmp/execution-progress-membership-gate-pg.log；92项启动/资格/原覆盖回归通过，日志/tmp/execution-progress-membership-gate-unit.log；编译与diff检查通过。

## 外键锁反查补充

17:20新快照的数据库deadlocks从发布后首次1846增至1852。全部worker只读日志定位Planner的ai_group_content_allocation._lock_group_surface与daily_coverage.ensure_task_daily_coverage flush，涉及tg_groups/tg_accounts。按§19.66.7补NO KEY UPDATE序列化及真实FK并发验证。66bcf9e9的Actions 34208990716尚未部署，已取消并由包含这次修复的完整候选取代；生产仍为fae502c5，不能把该取消写成部署失败或业务修复完成。

真实复现：/tmp/execution-reference-locks-before.log中旧代码2项失败，分别为40P01和FK lock timeout。修复后20项PG并发/资格/成员gate通过（/tmp/execution-reference-locks-after.log），69项内容分配/运行资源/共享准入/冻结回归通过（/tmp/execution-reference-locks-unit.log）。原两个事务的群member_count最终2、两条FK引用均提交，证明保留序列化而非并发丢写。

## Planner唤醒锁反查继续修复

a49b5087 Actions34210057986全部通过并于17:42:52部署；current=/data/tgyunying/releases/20260908093820_a49b5087，19容器healthy，内外健康200，schema0228。17:43:15至17:44:43数据库deadlocks从1865增至1867，原账号资格LockNotAvailable及浏览释放错误均为0，但新增两次Planner mark_task_planner_started等待wake行的死锁；这是独立的Task/wake锁反序。按§19.66.8将初始及commit后Planner认领改为savepoint联合非阻塞认领，不能写整体死锁已清零。当前13频道任务正式只读E4均not_met，主互动成功fact为0，部署成功不等于业务恢复。

联合认领QA：16项PG（新联合认领、原规划并发、执行竞争、退役）通过（/tmp/planner-joint-claim-pg.log），41项唤醒/退役/成员回归通过（/tmp/planner-joint-claim-unit.log）；补充非过期ORM缓存下新wake revision的读回/确认，最后4项联合认领PG全部通过（/tmp/planner-joint-claim-final.log）。锁后populate_existing防止陈旧revision覆盖新唤醒。


生产Session审查：SessionLocal配置autoflush=False，锁后populate_existing之前必须显式flush自己尚未持久化的wake变更，避免同事务连续唤醒/mark/complete丢revision；新增生产配置PG回归。错误/节奏冲突的三条记录事务也必须联合认领Task/wake，防止在错误处理时再次发生反序等待。65e0bf41的Actions34212562262已在部署前取消，由完整修正候选取代。

生产Session与错误记录补修最终QA：14项PG通过（/tmp/planner-joint-claim-final-combined.log），51项单元/边界回归通过（/tmp/planner-joint-claim-final-unit.log），编译与diff检查通过。

## 完整候选CI

候选067ae7bc08e2a0b3dd1b02c155fbb9bf1485ab78，Actions34212961666。五组后端检查全部通过：PG两组404/420 passed、各7 skipped和1 xfailed；非PG三组2043/2109/2106 passed，合计7082 passed、14 skipped、2 xfailed。前端build与三个镜像构建均通过。2026-09-08 18:09:04开始deploy；CI和镜像证据不作为生产恢复结论。

## 群面与监听Task反序继续resync

067ae7bc Actions34212961666于18:13:40成功部署，18:14:52独立读回current=/data/tgyunying/releases/20260908100909_067ae7bc，19个带RELEASE_SHA应用容器一致且healthy，验证码worker镜像也匹配且healthy；内外health均200、schema0228。新日志原资格/浏览释放/Task-wake错误未见，仍发现群内容分配40P01。18:14:54快照deadlocks1873、主互动typed fact仍0，不能写production_fixed。

/tmp/execution-lock-graph-067.jsonl的tick17/18捕获真实互等：监听73320持群更新等待Task，Planner76711持Task等待群。按§19.66.9将群面改为显式非阻塞资源认领，沿既有Planner事务回滚/错误重试和Gateway未调用路径。旧代码真实PG复现1 failed/1 passed（/tmp/content-surface-progress-before.log）；修复后12项PG通过（/tmp/content-surface-progress-after.log），证明监听统计与wake落库、原任务恢复及外键引用/数量守恒。

内容分配、共享资源与冻结准入69项回归通过（/tmp/content-surface-progress-unit.log）。审查确认群内容复核位于_reserve_group_send_attempt之前，RuntimeResourceBlocked由既有_dispatch_action捕获并延期，不新建called/unknown事实；群锁函数496行所在文件未超过500行，编译与diff检查通过。设计/结构/数据流已resync，重新进入完整CI与发布验证。

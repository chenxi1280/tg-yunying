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

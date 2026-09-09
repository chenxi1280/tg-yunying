# E4报告完整性修复与验证

- intake_id: production-e4-reporting-integrity-20260909；L3/P1。
- PRD: `docs/03-feature-designs/production-e4-reporting-integrity-prd.md`，先完成产品合同与反向检查，再实施。
- 基线2839c10f；独立分支codex/e4-reporting-integrity-20260909。
- locked_paths与隔离范围见PRD；“排查线上AI活群任务”“确认克隆任务引擎更新”均已确认无诊断文件重叠。共享目录、他们的分支与发布候选未修改；本分支索引只追加两条入口说明。

## 已实现

1. 发送回执与typed消息事实分开计数。消息E4要求同tenant/Task/ledger、Action/Attempt/账号、消息ID一致及发布后的Gateway和观察时间；重复观察按Action去重。
2. coverage_total_count保留全量历史；unified必达分母包含abandoned、unknown和待准入，legacy保留动态active必达口径，分别显示active/abandoned；非confirmed状态残留的成功数字或message id不再算完成。
3. 状态/原因分组新增distinct_account_count；ledger另算全量coverage_distinct_account_count，不能将交叉分组人数相加。
4. 原722行脚本按Attempt、活群统计、blocker责任拆分，CLI保留原函数导入入口，搜索/浏览判定原样迁移。

## 测试与审查

- 在修改实现之前，两个正式入口反例均失败：成功Attempt无typed fact未报消息事实缺失；4条coverage含1条abandoned却报告分母3。修复后通过。
- 最终55项定向测试通过，耗时6.93秒；每轮由Python subprocess timeout=60强制限制。使用原项目backend/.venv的Python，在独立工作树backend目录执行，数据为独立内存SQLite，未使用共享测试库或生产数据库。
- 文件：test_production_e4_reporting_integrity.py、test_task_fulfillment_e4_diagnostics.py、test_channel_view_targets.py。
- 反例覆盖缺事实、错误类型、跨租户/Task/ledger、错误Action/Attempt/账号、缺Gateway、发布前调用、观察早于调用、消息ID不一致、重复观察、状态残留、冻结不抹历史分母和多目标账号去重。SQL监听确认新统计只执行SELECT，不产生写操作。
- 定向Ruff与git diff --check通过。新增模块、修改后脚本及新增测试不超过500行；新增/提取函数按50个非空行检查。
- 自检不将Attempt回执、成员事实、Provider成功或unknown替代消息事实；未改变任何调度、发送、恢复、账号或Provider状态。
- 审查发现旧群日总量PRD允许legacy合法放弃后降低当前必达数，已先resync本PRD并保留该兼容合同；本次仅纠正unified误用该legacy规则的情况，新增全量历史字段不改变legacy验收分母。

## 交付边界

- design_status=complete；implementation_status=implemented；qa_status=passed_local；review_status=self_reviewed。
- CI、部署及生产E4：未执行；production_fixed=false。本分支不进入其他任务已经冻结的发布候选，不推进master/release。
- 此结果仅证明诊断与验收逻辑修复，不代表六项线上执行问题全部恢复，也不代表发送目标已完成。

## 二次修复：自动发现、软删除及一致只读事务

- 先resync专项PRD，再新增反例：修复前10项失败、1项通过，验证自动发现截断及漏删、显式快照缺少删除标记、三类任务历史证据可能误通过、CLI缺少事务初始化。该结果来自本地构造数据，不推断线上存在第11条漏检任务。
- 自动发现改为全部未删除running/completed的channel_view任务，updated_at降序后按id升序稳定排序；显式ID顺序和去重保持原合同。显式软删除任务仍有报告，task_deleted禁止历史证据被当作当前通过。
- 新模块production_e4_scope负责范围发现和PostgreSQL一致只读事务设置。CLI先设置REPEATABLE READ READ ONLY及20秒statement/2秒lock超时，再读取任务和证据；任一设置失败或读取失败均抛出错误，不输出通过摘要。
- 最终4个测试文件共69项通过，耗时8.70秒，subprocess硬超时60秒。新文件test_production_e4_scope包含14项用例；三条事务命令逐项失败及查询异常均覆盖。查询测试使用独立内存SQLite；CLI事务命令顺序/错误传播使用Session测试替身，未运行真实PostgreSQL隔离级别并发集成测试，不能据此声称生产快照已经验收。
- 定向Ruff、git diff --check和AST长度检查通过；CLI 468行、scope模块26行、blocker模块109行、新测试140行，函数均不超过50个非空行。
- 两个并行任务再次确认无路径冲突，当前Prepare/Deploy窗口由“确认克隆任务引擎更新”持有；本提交基于88046287独立交接，不推进共享master/release，不改变正在验证的候选。
- 本轮状态：design_status=complete、implementation_status=implemented、qa_status=passed_local、review_status=self_reviewed；CI/deployed-SHA/生产E4仍未执行，production_fixed=false。

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

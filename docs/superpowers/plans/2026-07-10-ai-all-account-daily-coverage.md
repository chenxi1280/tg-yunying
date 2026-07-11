# AI 活群“全部账号”每日履约实施计划

> 实施依据：`docs/03-feature-designs/ai-group-all-accounts-daily-coverage-prd.md`

## 目标与完成口径

将 `group_ai_chat` 的“全部账号”模式从 Planner 动态缩小分母，改为持久化目标关系和北京时间每日覆盖账本。新账号增量加入所有相关任务；账号进入当日账本后不因入群失败、离线、受限或 Session 失效而从分母消失；只有成功 Action 对应成功 ExecutionAttempt 且存在 Telegram `remote_message_id` 才完成履约。

完成代码实现需同时满足：

1. 数据库迁移可升级、可降级，旧运行任务可幂等回填。
2. Planner 不再为每个“全部账号”任务每轮扫描全部账号。
3. 账号变化事件可增量同步任务关系和当日账本，租户级核对只扫描一次账号快照。
4. 准入失败和发送阻塞保留在当日分母，并可分页查看。
5. 覆盖调度只分配账号 slot；AI 生成、上下文、话题、人设和质量门继续使用现有链路。
6. 生成失败、重复、上下文过期和发送失败释放预约；未知发送不立即重发。
7. 完成计数由 Telegram 远端回执确认，不能由 Action 创建或本地成功伪造。
8. 容量不足在预检和运行统计中显式阻塞。
9. 相关后端测试、迁移检查、前端测试与构建通过。

## Task 1：数据模型与迁移

**文件：**

- 新增：`backend/app/models/task_account_coverage.py`
- 修改：`backend/app/models/__init__.py`
- 新增：`backend/migrations/versions/0088_ai_group_daily_coverage.py`
- 新增：`backend/tests/test_task_account_daily_coverage_models.py`

**步骤：**

1. 先写失败测试，断言 `TaskAccountDailyCoverage` 的唯一键、状态字段、远端回执字段，以及 `AccountEligibilityEvent` 的未消费索引存在。
2. 运行：

   ```bash
   cd backend && timeout 60 .venv/bin/pytest -q tests/test_task_account_daily_coverage_models.py
   ```

   预期因模型不存在而失败。
3. 新建独立模型文件，避免继续扩大 `task_center.py`。覆盖账本唯一键为 `(tenant_id, task_id, group_id, account_id, coverage_date)`；事件表保存租户、账号、事件类型、发生时间、消费状态和错误。
4. 新增 `0088` 迁移，创建两张表、唯一约束和待消费/任务日期查询索引。降级只删除本次新增对象。
5. 重跑测试至通过，并执行 Alembic 单头检查。

## Task 2：目标账号判定、初始快照与增量事件

**文件：**

- 新增：`backend/app/services/task_center/account_scope.py`
- 修改：`backend/app/services/task_center/service.py`
- 修改：`backend/app/services/accounts.py`
- 修改：`backend/app/services/account_pools.py`
- 新增：`backend/tests/test_task_account_scope_sync.py`

**步骤：**

1. 先写失败测试，覆盖：
   - `status=在线`、普通用途、Session 可用账号进入范围；
   - 接码、搜索降权、用途冲突、删除、禁用或无 Session 账号不进入初始范围；
   - 创建“全部账号”任务时一次快照生成持久关系；
   - 新账号事件只同步该账号到同租户全部相关任务；
   - 重复事件和重复消费幂等；
   - 租户级核对对所有任务复用一份账号 ID 快照。
2. 运行定向测试，确认失败原因是目标服务缺失。
3. 在 `account_scope.py` 实现单一目标资格查询、`is_all_accounts_task`、任务初始快照、事件写入、事件消费和租户级核对。
4. 复用账号用途策略的普通池判定，不复制或弱化新上线的用途隔离规则。
5. 在任务创建事务中初始化目标关系；在账号录入、Session 登录成功、账号用途/状态变化的事务中写增量事件。事件写入与账号状态变更同事务提交。
6. Planner 常规循环只消费事件并读取关系，不调用目标账号全量资格查询。
7. 重跑定向测试至通过。

## Task 3：北京时间每日覆盖账本与准入阻塞

**文件：**

- 新增：`backend/app/services/task_center/daily_coverage.py`
- 修改：`backend/app/services/task_center/channel_membership.py`
- 修改：`backend/app/services/task_center/membership_admission.py`
- 新增：`backend/tests/test_task_account_daily_coverage.py`

**步骤：**

1. 先写失败测试，覆盖：
   - 每个任务关系每天只生成一条义务；
   - 活跃窗口结束后的新账号从次日开始；
   - 进入当日账本后，账号随后离线或 Session 失效仍保留；
   - 未入群、不可发言、入群失败分别显示 `pending_admission` 或 `blocked`；
   - 并发预约同一账本只有一个 Action 成功；
   - 释放预约后回到 `ready`，且不减少目标数。
2. 运行定向测试，确认红灯。
3. 实现北京时间日期和活跃窗口判断、批量建账、准入事实同步、原子预约、释放、未知和确认状态转换。
4. 准入链路更新关系状态时同步更新覆盖 blocker；不删除覆盖记录。
5. 使用唯一键与条件更新防止并发重复预约，不依赖进程内锁。
6. 重跑定向测试至通过。

## Task 4：覆盖 slot 调度与 AI 内容链路解耦

**文件：**

- 修改：`backend/app/services/task_center/account_pool.py`
- 修改：`backend/app/services/task_center/executors/group_ai_chat.py`
- 修改：`backend/app/services/task_center/ai_content.py`（仅在现有质量兜底入口位于该文件时）
- 新增：`backend/tests/test_ai_group_daily_coverage_planner.py`

**步骤：**

1. 先写失败测试，覆盖：
   - `all_accounts_daily` 从账本读取当前到期的 `ready` 账号，不重新查询全平台候选；
   - 已确认账号不再入选，阻塞账号仍在覆盖摘要；
   - 选中的账号继续经过现有容量、冷却、面具、上下文和 AI 质量门；
   - 创建 Action 后账本预约到该 Action，payload 含 `coverage_ledger_id`；
   - AI 候选不足不生成模板或通用表情，账本保持未完成；
   - `messages_per_round` 仍是单 Cycle 上限。
2. 运行测试确认红灯。
3. 为 `account_pool` 增加“按账本候选 ID 加载账号”的入口；原分组和手动账号模式保持不变。
4. `group_ai_chat` 在 all-account 模式中先计算当小时到期 slot，再调用原 AI 对话生成流程；只在 Action 持久化后预约账本。
5. 关闭 all-account 覆盖路径的 emoji/模板质量兜底；失败原因写入 blocker 但不直接发送补量消息。
6. 重跑相关 Planner、hard-hourly、账号池和数据流测试。

## Task 5：Dispatcher 远端确认、失败释放与未知态

**文件：**

- 修改：`backend/app/services/task_center/dispatcher.py`
- 新增：`backend/tests/test_task_daily_coverage_dispatch.py`

**步骤：**

1. 先写失败测试，覆盖：
   - Action 成功但没有成功 ExecutionAttempt 或没有 `remote_message_id` 时不确认；
   - 成功回执原子增加 `confirmed_count`，达到目标后进入 `confirmed`；
   - `duplicate_message`、质量失败、上下文过期、发送前失败释放预约；
   - 可重试发送失败释放并设置下一次允许时间；
   - `unknown_after_send` 进入 `unknown`，不释放为可立即补发；
   - 同一 Action 重复回执幂等，不重复计数。
2. 运行测试确认红灯。
3. 在 Dispatcher 终态写入边界调用账本服务；成功确认必须读取当前 ExecutionAttempt 的真实远端 ID。
4. 上下文过期批量跳过时逐条释放相关账本预约；其他任务类型或无账本 payload 的 Action 行为不变。
5. 重跑定向测试及 Dispatcher 现有回归。

## Task 6：容量证明、摘要 API 与分页明细

**文件：**

- 新增：`backend/app/services/task_center/coverage_capacity.py`
- 修改：`backend/app/services/task_center/precheck.py`
- 修改：`backend/app/services/task_center/account_coverage.py`
- 修改：`backend/app/schemas/task_center.py`
- 修改：`backend/app/api/routers/task_center.py`
- 新增：`backend/tests/test_task_daily_coverage_api.py`

**步骤：**

1. 先写失败测试，覆盖：
   - 容量证明比较目标总量、群日上限、群冷却可达量、任务小时上限和账号总容量；
   - 任一硬约束不足返回 `daily_coverage_capacity_insufficient`，不能自动提高配置；
   - 覆盖摘要分母来自账本，返回目标/确认/待准入/阻塞/未知/逾期数量和原因分布；
   - `/api/tasks/{task_id}/account-coverage` 支持状态、阻塞原因、日期和分页查询；
   - 阻塞账号出现在明细中并计入分母。
2. 运行测试确认红灯。
3. 实现容量计算和硬阻塞；旧运行任务在 stats 暴露 blocker，新建/更新任务由 precheck 阻止启动。
4. 用账本汇总替代 all-account 模式的动态成功 Action 反查；其他覆盖模式保留现有逻辑。
5. 新增分页 schema 和路由，接口不返回 Session 内容等敏感数据。
6. 重跑 API、precheck 和 task center 服务回归。

## Task 7：前端阻塞可观测性

**文件：**

- 修改：`frontend/src/app/types/taskCenter.ts`
- 修改：`frontend/src/app/components/TaskCenterDetailModal.tsx`
- 修改：`frontend/src/app/views/TaskCenterView.tsx`
- 修改：`backend/tests/test_frontend_permission_gating.py` 或新增对应前端测试

**步骤：**

1. 先更新/新增失败测试，要求任务详情展示真实分母、确认数、阻塞数、未知数、主要原因，以及可分页查看账号明细。
2. 接入摘要字段和明细 API。状态使用现有任务详情视觉规范，不新增营销式页面或嵌套卡片。
3. 明细中明确显示 `已完成 / 待入群 / 受限 / 离线 / Session 失效 / 容量不足 / 结果未知`，不能把阻塞项隐藏在“可用账号”过滤后。
4. 运行前端定向测试和 `npm run build`。

## Task 8：运行接入、回填、超时与文档索引

**文件：**

- 修改：`backend/app/services/task_center/service.py`
- 修改：`backend/app/services/task_center/listener_runtime.py`
- 修改：实际 Telegram gateway 的群消息拉取实现
- 新增：`backend/scripts/reconcile_ai_group_daily_coverage.py`
- 修改：`docs/00-index/project-structure-index.md`
- 修改：`docs/00-index/project-dataflow-index.md`
- 修改：`docs/05-implementation/multi-agent-practice/agent-status-board.md`
- 修改：`docs/05-implementation/multi-agent-practice/worklog/dev.md`
- 新增或修改：`backend/tests/test_task_daily_coverage_runtime.py`

**步骤：**

1. 先写失败测试，覆盖事件 drain 在 Planner 前运行、回填脚本幂等、单个 listener 拉取超时不阻塞后续 source，且主循环心跳能区分卡住与后台 DB 心跳。
2. 将事件消费、每日建账和低频租户核对接入独立 drain；常规 task planner 只读取持久关系和账本。
3. 回填脚本对已有运行/暂停“全部账号”任务建立关系和当日账本，输出扫描、创建、修复和失败数；失败返回非零状态。
4. 在 Telegram gateway 网络调用处设置明确超时并让错误暴露到 source 状态，禁止静默跳过。
5. 更新结构索引、数据流索引和开发状态记录。

## 最终验证

按顺序执行并保留完整结果：

```bash
cd backend
timeout 60 .venv/bin/pytest -q \
  tests/test_task_account_daily_coverage_models.py \
  tests/test_task_account_scope_sync.py \
  tests/test_task_account_daily_coverage.py \
  tests/test_ai_group_daily_coverage_planner.py \
  tests/test_task_daily_coverage_dispatch.py \
  tests/test_task_daily_coverage_api.py \
  tests/test_task_daily_coverage_runtime.py
timeout 60 .venv/bin/pytest -q \
  tests/test_task_account_pool.py \
  tests/test_task_center_config_normalization.py \
  tests/test_ai_group_hard_hourly_target.py \
  tests/test_task_center_capacity_dispatch.py \
  tests/test_task_center_membership_items.py \
  tests/test_task_center_ai_planner_membership.py \
  tests/test_group_ai_chat_dataflow.py \
  tests/test_task_center_role_drains.py \
  tests/test_operations_center_runtime.py
.venv/bin/alembic heads
.venv/bin/alembic upgrade head --sql >/tmp/tg-yunying-coverage-migration.sql
cd ../frontend
npm run test -- --run
npm run build
cd ..
git diff --check
git status --short
```

本地验证通过只代表 `qa_pass` 候选，不代表生产恢复。上线后仍需按 PRD Release Gate，在完整北京时间自然日核验“任务 × 群 × 账号”账本、Telegram 远端消息 ID、阻塞分母和 listener 主循环心跳，才能标记 `production_fixed`。

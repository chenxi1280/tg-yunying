# Progress

## Session: 2026-08-01

### Phase 1: 生产时间线与失败链
- **Status:** completed
- **Started:** 2026-08-01 10:40+08:00
- Actions taken:
  - 复用上一轮只读生产采样作为诊断起点。
  - 建立独立 scoped 计划，未修改 `.planning/.active_plan`。
  - 确认三项 AI 在新容器启动后无新业务事实，搜索点击仍缓慢增长。
  - 确认 10:04、10:13 三次数据库 deadlock，当前无残留锁等待。

### Phase 2: 代码差异与锁顺序
- **Status:** completed
- Actions taken:
  - 核对实时容器环境：实际 Dispatcher 为 2 分片，其他 writer 为默认 1 分片。
  - 核对实时窗口：相邻分钟存在 1/2 分片 allocation 混用和跨 epoch 保留。
  - 对照搜索 fulfillment、Dispatcher selection 与 allocation capacity 代码，确认 reservation 只能被精确分片拓扑消费。
  - 确认搜索严格需求与 admission 类别挤占容量，普通 AI ready Action 无 active claim。
  - 将 PostgreSQL deadlock 对应到 Scope→Task FK 与 Task FOR UPDATE→Scope 的反向锁序。
  - 独立审查确认 merge commit 本身 tree 未变化，并识别 AI scope 合同、watermark 和 reply-target 新闸门。

### Phase 3: 各任务根因归类
- **Status:** completed
- Actions taken:
  - 分离 AI 公共 claim 阻塞、历史 scope 兼容断层与逐任务业务闸门。
  - 核实搜索为持续增长但低转化的容量漏斗，不是完全停摆。
  - 更正评论判断：当前 Action 尚未到计划时间，并非已证明停滞。
  - 核实三项无 pending 的 dynamic-new 点赞任务没有当前执行输入；阿哥日记有少量 due 但无 claim。

### Phase 4: 复采样与结论
- **Status:** completed
- Actions taken:
  - 10:57 复采样：搜索 329，三个 AI 仍为 83/80/59。
  - 检查 PostgreSQL 日志：10:14 后无新增 deadlock。
  - 检查线上 Dispatcher 环境：每 worker 有效 DB 并发 13，总计约 26，低于 scope capacity 52。
  - 汇总生产、代码与独立 Agent 证据，按 confirmed / likely / unproven 分级输出。
- Files created/modified:
  - `.planning/production_root_cause_20260801/task_plan.md`
  - `.planning/production_root_cause_20260801/findings.md`
  - `.planning/production_root_cause_20260801/progress.md`

## Test Results
| Test | Input | Expected | Actual | Status |
|------|-------|----------|--------|--------|
| scope isolation | inspect planning paths | no overwrite of active plan | new scoped directory only | pass |

## Error Log
| Timestamp | Error | Attempt | Resolution |
|-----------|-------|---------|------------|
| 2026-08-01 10:40 | recovery skill placeholder | 1 | use repository and live evidence |
| 2026-08-01 10:43 | `dispatch_claim_reservations` has no shard columns | 1 | read shard values from joined `dispatch_claim_shard_allocations` |
| 2026-08-01 10:44 | JSON column does not support jsonb `?` operator | 2 | use direct JSON extraction null check |
| 2026-08-01 10:52 | `actions.updated_at` does not exist | 3 | use `COALESCE(executed_at, created_at)` |
| 2026-08-01 10:54 | SSH connection closed by remote host | 4 | bounded one-time retry succeeded |

## 5-Question Reboot Check
| Question | Answer |
|----------|--------|
| Where am I? | Phase 4 conclusion synthesis |
| Where am I going? | evidence-graded final diagnosis |
| What's the goal? | explain current production task root causes |
| What have I learned? | see findings.md |
| What have I done? | see above |

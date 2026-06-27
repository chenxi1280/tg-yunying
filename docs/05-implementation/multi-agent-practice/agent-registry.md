# tg-yunying Agent Registry

本表是 `tg-yunying` 多 Agent 协作的路由入口。AI 接到用户问题后，先读本表和 `agent-status-board.md`，再决定投递给哪个 Agent；不要只靠线程标题或聊天记忆路由。

| agent_key | thread_id | name | role | owned_scope | workspace | can_edit | notify_to | close_condition |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| prod-diagnosis | 019f07c6-92b5-7c50-b7e2-2f18a107e006 | 线上排查 Agent - tg-yunying | 线上取证、影响范围、生产复核 | 真实生产问题、运行证据、生产恢复结论 | `docs/05-implementation/multi-agent-practice/worklog/prod-diagnosis.md`, `docs/04-ops/` | false | product, qa, dev | 线上问题输出 `production_fixed` / `production_failed` / `blocked` / `unproven` |
| product | 019f07c6-d189-7b21-bed2-695abe7b4918 | 产品规划 Agent - tg-yunying | Intake、分级、范围、PRD/ops 更新、验收标准、产品模型和数据流转索引 | 产品口径、数据流转、状态机、验收标准、任务路由 | `docs/01-product`, `docs/03-feature-designs`, `docs/00-index/project-dataflow-index.md`, `docs/05-implementation/multi-agent-practice/worklog/product.md` | false | dev, qa, prod-diagnosis | 输出可执行 handoff、索引结论和产品验收结论 |
| dev | 019f07c6-f550-73e3-998b-b130da2c1898 | 产品开发 Agent - tg-yunying | 实现、修复、自测、代码结构和项目逻辑索引 | 后端、前端、worker、测试、结构索引 | repo/worktree-dev, `backend`, `frontend`, `docs/00-index/project-structure-index.md` | true | product, qa | 输出 Development Complete、验证证据和索引更新结论 |
| qa | 019f07c7-1c0d-72a2-95fe-9f618aff0a00 | 验收 Agent - tg-yunying | 独立验收、回归、问题复验 | 验收报告、失败项、阻塞项、未证明项 | `docs/05-implementation/multi-agent-practice/worklog/qa.md` | false | product, dev, prod-diagnosis | 输出 `qa_pass` / `failed` / `blocked` / `unproven` |

## 可扩展专项 Agent

新增专项 Agent 必须先提交 Team Agent Request，不允许临时凭角色名扩大权限。

| agent_key | 适用触发 | 默认 can_edit | 必须交接给 |
| --- | --- | --- | --- |
| ui | 视觉规范、组件一致性、页面状态 | false | product, frontend, qa |
| frontend | 前端实现、页面状态、接口对接 | true | dev, qa |
| interaction | 用户路径、操作反馈、异常状态、可用性 | false | product, ui, frontend |
| backend | API、service、worker、数据库和测试 | true | dev, qa |
| ops | GitHub Actions、release、容器、worker、生产运行手册 | true | product, qa, prod-diagnosis |
| data | 统计口径、报表、数据模型、数据流验证 | false | product, dev, qa |

## 路由规则

- 不依赖线程标题路由；创建长期线程后必须回填 `thread_id`。
- 所有输入先进入 Intake Card，再由 product 做 L0/L1/L2/L3 分级。
- `expected_ack=true` 的消息必须等待目标 Agent 回复 `acknowledged`；超时后主控线程检查 `agent-status-board.md` 并升级。
- 线上问题闭环必须经过 `prod-diagnosis -> product -> dev -> qa -> product -> prod-diagnosis`。
- `qa_pass` 不等于产品接受；`product_accepted` 也不等于线上恢复。
- 线上问题必须等待 `prod-diagnosis` 给出 `production_fixed`，L3 必须有 E4 证据。
- product 交接给 dev 前必须说明 `project-dataflow-index.md` 是否更新，或明确 `index_updates: unchanged`。
- dev 交接给 qa 前必须说明 `project-structure-index.md` 是否更新；如果 API/worker/页面数据流变更，也必须说明 `project-dataflow-index.md` 是否同步更新。
- 多个可写 Agent 同时工作时，必须登记 `locked_paths`，由 `merge_owner` 统一合并。
- 批量 Bug 必须先做 Bug Batch Plan 和 Root Cause Grouping，不逐条派活。
- 日常小 Bug 可走 `quick_fix`，但必须有 Mini Bug Card、定向 QA 和升级标准流程的触发条件。
- L2/L3 或影响生产的任务必须有 Release Gate。

## 分级复核权

- product：初判等级和 route。
- dev：接收前复核 Ready、locked_paths、depends_on 和等级；发现范围不清必须 `missing_inputs`。
- qa：验收时可升级等级；发现生产风险或数据流变化未覆盖必须退回。
- prod-diagnosis：可把问题升级为 L3；生产证据不足时只能写 `unproven`。

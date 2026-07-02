1. Think Before Coding
Don't assume. Don't hide confusion. Surface tradeoffs.

Before implementing:

State your assumptions explicitly. If uncertain, ask.
If multiple interpretations exist, present them - don't pick silently.
If a simpler approach exists, say so. Push back when warranted.
If something is unclear, stop. Name what's confusing. Ask.
2. Simplicity First
Minimum code that solves the problem. Nothing speculative.

No features beyond what was asked.
No abstractions for single-use code.
No "flexibility" or "configurability" that wasn't requested.
No error handling for impossible scenarios.
If you write 200 lines and it could be 50, rewrite it.
Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

3. Surgical Changes
Touch only what you must. Clean up only your own mess.

When editing existing code:

Don't "improve" adjacent code, comments, or formatting.
Don't refactor things that aren't broken.
Match existing style, even if you'd do it differently.
If you notice unrelated dead code, mention it - don't delete it.
When your changes create orphans:

Remove imports/variables/functions that YOUR changes made unused.
Don't remove pre-existing dead code unless asked.
The test: Every changed line should trace directly to the user's request.

4. Goal-Driven Execution
Define success criteria. Loop until verified.

Transform tasks into verifiable goals:

"Add validation" → "Write tests for invalid inputs, then make them pass"
"Fix the bug" → "Write a test that reproduces it, then make it pass"
"Refactor X" → "Ensure tests pass before and after"
For multi-step tasks, state a brief plan:

1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.


# tg-yunying Agent Rules

本项目使用 `docs/05-implementation/multi-agent-practice/` 作为多 Agent 协作协议。接到需求、Bug、线上问题或排障请求时，先阅读该目录，再执行。

## 项目真相源

- 产品口径：`docs/01-product/tg-ops-platform-prd.md` 和 `docs/03-feature-designs/`。
- 数据流转：`docs/00-index/project-dataflow-index.md`。
- 代码结构：`docs/00-index/project-structure-index.md`。
- 生产运行：`docs/04-ops/deployment/PRODUCTION_RUNTIME.md` 和真实线上证据。
- 多 Agent 状态：`docs/05-implementation/multi-agent-practice/agent-status-board.md`。

## 多 Agent 流程

- 所有输入先进入 Intake Card，再由 product 做 L0/L1/L2/L3 分级。
- 线上问题必须走 `prod-diagnosis -> product -> dev -> qa -> product -> prod-diagnosis`。
- product 整理完需求后必须投递 dev；dev 完成后必须投递 qa；qa 通过后必须回到 product 验收。
- 任何 Agent 输出阶段结论时，只要 `next_agent` 不为空，必须同时完成真实线程投递；只写 `notify_xxx: true`、`next_agent: xxx` 或口头说明“需要通知”不算完成。
- 当前 Agent 无法直接投递时，必须写 `handoff_delivery_status=blocked`、`requires_orchestrator_send=true`、目标线程、完整消息正文和阻塞原因，交给主控/监督 Agent 代发。
- `qa_pass` 不等于产品接受；`product_accepted` 不等于线上恢复。
- L3 只有真实生产 E4 证据才能写 `production_fixed`。

## Product Design Complete 闸门

- product 不能实现代码；即使用户在 product 线程输入“执行/实现/修复”，product 也必须转成 Product Handoff 投递给 dev。
- product 投递 dev 前必须完成 Product Design Complete 自检，覆盖原始需求、功能设计、前端状态、后端/API/worker 设计、数据流转、权限安全、边界场景和 QA 验收口径。
- `design_status=partial/blocked` 时不能投递 dev，也不能声明产品设计完成；必须列出缺口、追问或补齐设计。
- product 必须深度自检遗漏项：未覆盖用户原话、隐含场景、失败路径、并发/幂等、数据一致性、发布/迁移风险和回滚口径。

## 交接监督

- `flow-supervisor` 或主控线程负责检查 `agent-status-board.md` 中的 `next_agent`、`handoff_delivery_status`、`ack_deadline` 和 `retry_count`。
- 交接状态必须从 `pending -> sent -> acknowledged` 推进；超时或没有真实投递记录时，监督 Agent 必须重发 handoff 或标记 `handoff_delivery_status=blocked`。
- 开发完成后没有投递 QA、QA 通过后没有投递 product、product 接受后没有投递 release/production verify，均视为流程断链，不能把任务写成 closed。

## 快修与并行

- 日常小 Bug 可走 `quick_fix`，但必须有 Mini Bug Card、定向 QA 和升级标准流程的触发条件。
- 批量 Bug 不逐条派活，必须先做 Bug Batch Plan 和 Root Cause Grouping。
- 多个可写 Agent 并行时必须登记 `locked_paths`，由 `merge_owner` 统一合并。

## PRD 和索引

- 需求、流程、验收标准或数据流转变化时，先更新 PRD / 专项设计 / 数据流转索引，再通知 dev。
- 代码入口、模块边界、API、worker、页面数据流变化时，dev 必须更新项目结构索引，必要时同步数据流转索引。
- 上游文档或索引在开发开始后变化时，product 必须发送 `resync`，不能只改文档。

## 发布和验证

- 生产发布路径默认是 `master -> release -> GitHub Actions Deploy Production`。
- L2/L3 或影响生产的任务必须有 Release Gate。
- 后端测试默认使用 `backend/.venv`。
- 不允许 silent fallback、mock success 或未经验证的完成声明。

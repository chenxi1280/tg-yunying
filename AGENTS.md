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

本项目按本文件中的 Intake、分级、阶段流转、PRD/索引、QA 和发布验证规则执行。`docs/05-implementation/multi-agent-practice/` 仅作为历史运行记录、模板和状态看板参考；接到需求、Bug、线上问题或排障请求时，按需阅读相关文件。

## 标准工程闭环与项目记忆

本项目所有需求更新、问题修复与功能迭代严格遵循以下闭环流转，循环直到真实业务验证通过：

```text
→ 完成 PRD 更新
→ 检查并修复设计问题
→ 检查并修复业务、数据和安全问题
→ 按 PRD 用真实代码、测试和只读线上脚本反向检查
→ 修复 PRD 设计问题
→ 代码实现
→ 代码审查
→ 修复代码问题
→ 定向测试
→ 发布上线验证
→ 发现问题后回到对应阶段修复
→ 重新审查和定向测试
→ 再次发布上线验证
→ 循环直到真实业务验证通过
```

### 闭环流转要求

1. **完成 PRD 更新**：梳理业务与需求，更新 `docs/01-product/` / `docs/03-feature-designs/` PRD 与设计文档。
2. **检查并修复设计问题**：深度自检架构设计、状态机、前端/后端契约与边界完整性。
3. **检查并修复业务、数据和安全问题**：全面排查业务规则闭合度、数据流转一致性、幂等性、并发安全与权限控制。
4. **按 PRD 用真实代码、测试和只读线上脚本反向检查**：对照 PRD，结合现有真实代码、已有测试与只读生产脚本，反向校验设计可行性、现有假设与潜在冲突。
5. **修复 PRD 设计问题**：根据反向检查发现的差异与缺口，二次修正 PRD 与设计合同，确保满足 Product Design Complete 闸门。
6. **代码实现**：设计完全闭合后进入 dev 阶段，执行最小、精准（外科手术式）代码修改。
7. **代码审查**：对照 PRD、设计合同与规范进行严格代码审查（逻辑正确性、边界处理、无额外副作用）。
8. **修复代码问题**：彻底修复审查中发现的问题与潜在缺陷。
9. **定向测试**：执行针对性单元测试、集成测试与回归测试，验证成功标准（后端测试默认使用 `backend/.venv`）。
10. **发布上线验证**：按 Release Gate 流程发布部署，并在生产环境进行真实验证。
11. **发现问题后回到对应阶段修复**：若测试或线上验证发现问题，准确定位根因并回到对应阶段（PRD/设计/数据流/代码）修复，严禁跳步。
12. **重新审查和定向测试**：对修复内容重新执行代码审查与定向测试。
13. **再次发布上线验证**：再次部署上线并进行生产环境验证。
14. **循环直到真实业务验证通过**：持续迭代该闭环，直至真实业务与生产指标完全验证通过。

## 项目真相源

- 产品口径：`docs/01-product/tg-ops-platform-prd.md` 和 `docs/03-feature-designs/`。
- 数据流转：`docs/00-index/project-dataflow-index.md`。
- 代码结构：`docs/00-index/project-structure-index.md`。
- 生产运行：`docs/04-ops/deployment/PRODUCTION_RUNTIME.md` 和真实线上证据。
- 多 Agent 状态：`docs/05-implementation/multi-agent-practice/agent-status-board.md`。

## 多 Agent 流程

- 所有输入先进入 Intake Card，再由 product 做 L0/L1/L2/L3 分级。
- 线上问题必须走 `prod-diagnosis -> product -> dev -> qa -> product -> prod-diagnosis`。
- product 整理完需求后进入 dev 阶段；dev 完成后进入 qa；qa 通过后回到 product 验收。
- `qa_pass` 不等于产品接受；`product_accepted` 不等于线上恢复。
- L3 只有真实生产 E4 证据才能写 `production_fixed`。

## Product Design Complete 闸门

- product 不能实现代码；即使用户输入“执行/实现/修复”，product 也必须转成 Product Handoff / 开发交接内容，由 dev 阶段处理。
- 进入 dev 前必须完成 Product Design Complete 自检，覆盖原始需求、功能设计、前端状态、后端/API/worker 设计、数据流转、权限安全、边界场景和 QA 验收口径。
- `design_status=partial/blocked` 时不能进入 dev，也不能声明产品设计完成；必须列出缺口、追问或补齐设计。
- product 必须深度自检遗漏项：未覆盖用户原话、隐含场景、失败路径、并发/幂等、数据一致性、发布/迁移风险和回滚口径。

## 快修与并行

- 日常小 Bug 可走 `quick_fix`，但必须有 Mini Bug Card、定向 QA 和升级标准流程的触发条件。
- 批量 Bug 不逐条派活，必须先做 Bug Batch Plan 和 Root Cause Grouping。
- 多个可写 Agent 并行时必须登记 `locked_paths`，由 `merge_owner` 统一合并。

## PRD 和索引

- 需求、流程、验收标准或数据流转变化时，先更新 PRD / 专项设计 / 数据流转索引，再进入 dev。
- 代码入口、模块边界、API、worker、页面数据流变化时，dev 必须更新项目结构索引，必要时同步数据流转索引。
- 上游文档或索引在开发开始后变化时，product 必须标记 `resync`，不能只改文档。
- `docs/03-feature-designs/` 下与当前需求相关的全部专项 PRD、设计文档及其实施/运行/验收合同不设行数上限；代码文件的行数限制不得套用于这些 Markdown 文档。以需求覆盖完整、可实施、可验收和边界闭合为准，不得为缩短篇幅而删减、拆散或弱化产品合同。

## 发布和验证

- 生产发布路径默认是 `master -> release -> GitHub Actions Deploy Production`。
- L2/L3 或影响生产的任务必须有 Release Gate。
- 后端测试默认使用 `backend/.venv`。
- 不允许 silent fallback、mock success 或未经验证的完成声明。

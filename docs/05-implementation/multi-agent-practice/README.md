# tg-yunying 多 Agent 协作协议

本目录不是普通教程，而是 `tg-yunying` 项目的多 Agent 运行协议。用户把本目录或本文件交给 AI 后，AI 应直接阅读、检查项目、初始化缺失文件、分诊任务并推动闭环；除非用户明确要求“只解释”，否则不要停在说明文档。

## 1. AI 自运行入口

给 AI 的最小启动指令：

```text
请完整阅读 docs/05-implementation/multi-agent-practice/，把它当作 tg-yunying 当前项目的多 Agent 协作协议。
不要只解释文档，请直接检查当前项目并增量初始化缺失的协作文件、模板、索引和状态看板。
完成后输出 created / updated / skipped / blocked / unproven / next_route。
如果当前输入是线上问题，先走 Incident Report；如果是普通需求，先走 Intake + Triage；如果是小 Bug，先判断 quick_fix；如果是批量问题，先输出 Bug Batch Plan。
```

AI 执行时必须遵守：

- 不覆盖已有 PRD、索引、worklog 和模板，只做增量更新。
- 不把本地通过、CI 通过、QA pass 写成线上恢复。
- 不使用 silent fallback、mock success 或未经验证的完成声明。
- 涉及线上环境时，真实证据优先；生产恢复必须由 `prod-diagnosis` 输出 `production_fixed`。
- 涉及需求、流程、数据流转变化时，先更新 PRD / 专项设计 / 数据流转索引，再投递开发。
- 涉及代码入口、模块边界、API、worker、页面数据流变化时，开发 Agent 必须更新项目结构索引，必要时同步数据流转索引。

## 2. 最小团队

| agent_key | 职责 | 关闭条件 |
| --- | --- | --- |
| `prod-diagnosis` | 线上排查、真实证据、影响范围、生产复核 | 线上问题输出 `production_fixed` / `production_failed` / `blocked` / `unproven` |
| `product` | Intake、分级、范围、PRD/ops 更新、验收标准、产品模型和数据流转索引 | 开发边界清楚，验收口径可执行，索引结论明确 |
| `dev` | 实现、修复、自测、项目结构索引和代码入口索引 | 输出 Development Complete、验证证据、索引更新结论 |
| `qa` | 独立验收、回归、按 bug_id 复验 | 输出 `qa_pass` / `failed` / `blocked` / `unproven` |

后续可以通过 Team Agent Request 增加 `ui`、`frontend`、`interaction`、`backend`、`ops`、`data` 等专项 Agent。专项 Agent 只能关闭自己的专业范围，不能宣布整条链路完成。

## 3. 项目文件

- [agent-registry.md](agent-registry.md)：Agent 登记表、路由规则和升级规则。
- [agent-status-board.md](agent-status-board.md)：跨 Agent 状态看板，所有任务都必须登记。
- [index-maintenance.md](index-maintenance.md)：产品数据流转索引和项目结构索引维护协议。
- [templates/](templates/)：Intake、Triage、handoff、开发完成、验收、生产复核、发布关口、复盘反补等模板。
- [worklog/](worklog/)：四个长期 Agent 的工作日志。
- [runs/](runs/)：每次完整闭环的运行记录。

共享索引入口：

- `docs/00-index/project-dataflow-index.md`
- `docs/00-index/project-structure-index.md`
- `docs/01-product/tg-ops-platform-prd.md`
- `docs/03-feature-designs/`
- `docs/04-ops/deployment/PRODUCTION_RUNTIME.md`

## 4. 标准流程

### 4.1 线上问题

```text
prod-diagnosis -> product -> dev -> qa -> product -> prod-diagnosis
```

关闭条件：

- `prod-diagnosis` 输出 Incident Report，并标注 E0-E4 证据。
- `product` 把 Incident 转成范围、数据流转、验收标准和 Release Gate 要求。
- `dev` 修复并输出 Development Complete。
- `qa` 独立验收并输出 `qa_pass`。
- `product` 做产品验收，输出 `product_accepted`。
- `prod-diagnosis` 回到真实生产环境复核，L3 必须有 E4 证据才能写 `production_fixed`。

### 4.2 普通需求

```text
user -> product(Intake + Triage + PRD/索引) -> dev -> qa -> product
```

产品 Agent 整理完成后必须投递开发 Agent，不能停在需求整理；开发完成后必须投递 QA；QA 通过后必须回到产品验收。

### 4.3 小 Bug 快修

`quick_fix` 只适用于范围清楚、低风险、可定向验收的问题：

```text
Mini Bug Card -> dev -> QA 定向验收 -> product/main 接受
```

一旦出现生产影响、数据流变化、权限/任务/worker 变更、范围扩大或验收不清，必须升级为标准流程。

### 4.4 批量 Bug 并行

产品 Agent 不逐条派活，必须先输出 Bug Batch Plan：

```text
批量分诊 -> Root Cause Grouping -> 按 lane/locked_paths 并行 -> merge_owner 合并 -> QA 按 bug_id 验收 -> 失败项剥离返工
```

## 5. 分级规则

| level | 场景 | 默认流程 | 关闭条件 |
| --- | --- | --- | --- |
| L0 | 极小修、文案、纯文档、低风险配置说明 | single_agent 或 product/main -> dev | product/main 接受，证据至少 E1 |
| L1 | 小 Bug、局部 UI/接口契约问题 | quick_fix 或 light_agents | QA 定向验收 + product 接受 |
| L2 | 普通需求、跨模块 Bug、影响数据流 | product -> dev -> qa -> product | QA pass + product_accepted + 必要 Release Gate |
| L3 | 线上事故、高风险发布、生产数据/worker/TG 真实链路 | prod-diagnosis -> product -> dev -> qa -> product -> prod-diagnosis | QA pass + product_accepted + E4 production_fixed |

分级不是产品 Agent 一人说了算：

- `product` 初判等级。
- `dev` 接收前复核 Ready、locked_paths、depends_on 和风险等级。
- `qa` 验收时可升级等级。
- `prod-diagnosis` 可以把线上问题升级为 L3。

## 6. 状态输出

每轮 AI 执行结束只输出：

- `created`：新建了哪些文件。
- `updated`：增量更新了哪些文件。
- `skipped`：哪些文件存在且无需改。
- `blocked`：缺信息、缺权限或环境不可达的动作。
- `unproven`：尚无证据证明的结论。
- `next_route`：下一步进入 Intake、Triage、quick_fix、batch、dev、qa、product_acceptance、production_verify 或 rule_backfill。

## 7. 本目录历史

[runs/2026-06-27-docs-practice.md](runs/2026-06-27-docs-practice.md) 记录了第一次文档级四 Agent 演练。该记录证明线程和文件交接能跑通，但不代表任何线上业务恢复。

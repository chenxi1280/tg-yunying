# 2026-06-27 四 Agent 协作实践记录

本次以本地 `tg-yunying` 项目为例，完成一次低风险、文档级四 Agent 闭环。

## 目标

验证以下协作链路可以落地到本地项目：

```text
prod-diagnosis -> product -> dev -> qa -> prod-diagnosis
```

## 0. 线程初始化

已为 `tg-yunying` 创建四个长期线程：

| agent_key | thread_id | 初始化状态 |
| --- | --- | --- |
| prod-diagnosis | 019f07c6-92b5-7c50-b7e2-2f18a107e006 | acknowledged |
| product | 019f07c6-d189-7b21-bed2-695abe7b4918 | acknowledged |
| dev | 019f07c6-f550-73e3-998b-b130da2c1898 | acknowledged |
| qa | 019f07c7-1c0d-72a2-95fe-9f618aff0a00 | acknowledged |

说明：

- 四个线程的初始化提示词都要求只 ACK、等待消息，不主动改业务代码。
- 四个线程均已回复 ACK。
- 线程标题已设置为：`线上排查 Agent - tg-yunying`、`产品规划 Agent - tg-yunying`、`产品开发 Agent - tg-yunying`、`验收 Agent - tg-yunying`。
- `agent-registry.md` 已回填真实 `thread_id`。
- 如果 Codex app 侧边栏标题刷新有延迟，以 `thread_id` 为准。

## 0.1 真实跨线程投递状态

已从主控线程向产品规划线程投递 `2026-06-27-docs-practice-incident-001`。

| step | source | target | status | evidence |
| --- | --- | --- | --- | --- |
| initialize prod-diagnosis | main | prod-diagnosis | acknowledged | thread `019f07c6-92b5-7c50-b7e2-2f18a107e006` ACK |
| initialize product | main | product | acknowledged | thread `019f07c6-d189-7b21-bed2-695abe7b4918` ACK |
| initialize dev | main | dev | acknowledged | thread `019f07c6-f550-73e3-998b-b130da2c1898` ACK |
| initialize qa | main | qa | acknowledged | thread `019f07c7-1c0d-72a2-95fe-9f618aff0a00` ACK |
| deliver Incident Report | main/prod-diagnosis | product | completed | product thread returned Product Handoff `2026-06-27-docs-practice-product-001` |
| deliver Product Handoff | main/product | dev | completed | dev thread returned Development Complete after read-only verification |
| deliver Development Complete | main/dev | qa | failed | qa thread returned Validation Report `failed` because status fields were stale |
| fix QA status findings | main | filesystem | completed | dev status, worklog unresolved fields, and real-thread status table updated |
| request QA recheck | main/dev | qa | completed | QA second recheck returned `pass` |
| request document-level production verification | main/qa | prod-diagnosis | completed | prod-diagnosis returned `document_flow_verified` |

结论：

- 真实线程层已经完成“创建 + 初始化 ACK + Incident 投递 + Product Handoff 投递 + Development Complete 投递 + QA 首轮验收”。
- QA 首轮验收为 `failed`，主控线程已按问题修正文档状态。
- QA 第二次 recheck 已返回 `pass`。
- prod-diagnosis 已返回 `document_flow_verified`，只表示文档级协作闭环确认，不表示线上业务恢复。
- 下方完整闭环记录是文件系统层的演练链路，用于验证模板和责任边界。

## 1. Incident Report

- message_id: 2026-06-27-docs-practice-incident-001
- from_agent: prod-diagnosis
- to_agent: product
- severity: P2
- status: reproduced
- source: 本地协作流程演练
- affected_scope: 多 Agent 协作材料缺失，线上问题闭环缺少可复用交接协议
- first_seen_at: 2026-06-27
- evidence_links:
  - `docs/05-implementation/README.md`
  - `docs/README.md`
- related_thread: 当前 Codex 线程

### 现象

`tg-yunying` 有明确的产品、运维、实施文档结构，但缺少四 Agent 协作材料。线上排查、产品定范围、开发修复、验收、生产复核之间没有本地可复用的登记表、模板和工作日志。

### 复现路径

1. 查看 `docs/05-implementation/README.md`。
2. 未发现多 Agent 协作入口。
3. 查看项目根目录，未发现 `agent-registry.md` 或四 Agent worklog。

### 线上证据

本次是文档级演练，不访问真实生产系统。生产证据项标记为 `unproven`，不作为线上恢复证明。

### 影响范围

- 线上问题可能绕过产品 Agent 直接进入开发。
- QA pass 可能被误当作生产恢复。
- 修复后缺少生产复核回传。

### 初步判断

需要在 `docs/05-implementation` 下建立协作材料，并跑通一次消息链。

### 建议产品 Agent 决策的问题

是否将本次演练范围限定为文档级，不改业务代码、不触发发布流程。

### 需要开发或验收补充的证据

- 开发需补齐登记表、模板、worklog、演练记录。
- QA 需确认材料完整性和边界。

## 2. Product Handoff

- message_id: 2026-06-27-docs-practice-plan-001
- from_agent: product
- to_agent: dev
- related_incident: 2026-06-27-docs-practice-incident-001
- related_version: docs-practice-2026-06-27
- task_type: implement
- priority: P2
- created_at: 2026-06-27
- source_thread: 当前 Codex 线程
- target_thread: dev
- reply_to_message_id: 2026-06-27-docs-practice-incident-001
- expected_ack: true
- status: acknowledged

### 背景

`tg-yunying` 需要把四 Agent 协作方式落到本地项目文档里，先用文档级演练证明闭环。

### 本次要你做什么

在 `docs/05-implementation/multi-agent-practice/` 下建立四 Agent 协作材料，并记录一次完整消息链。

### 输入材料

- `docs/README.md`
- `docs/05-implementation/README.md`
- 外部教程：`/Users/xida/codexProject/codexwork/Codex多Agent协作使用教程.md`

### 必须遵守的边界

- 不修改业务代码。
- 不创建假线上成功。
- 不触发部署。
- `thread_id` 已回填到 `agent-registry.md`。

### 完成标准

- 有 `agent-registry.md`。
- 有四个 worklog。
- 有 Incident、Handoff、Development Complete、Validation、Production Verification 模板。
- 有一份完整 runs 记录。
- `docs/05-implementation/README.md` 增加入口。

### 需要回传的内容

- 修改文件列表。
- 验证方式。
- 未验证项。

## 3. Development Complete

- message_id: 2026-06-27-docs-practice-devcomplete-001
- from_agent: dev
- to_agent: product, qa
- related_incident: 2026-06-27-docs-practice-incident-001
- related_version: docs-practice-2026-06-27
- reply_to_message_id: 2026-06-27-docs-practice-plan-001
- status: ready_for_validation

### 实现摘要

建立 `docs/05-implementation/multi-agent-practice/`，补齐登记表、模板、工作日志和本次实践记录。

### 修改文件

- `docs/05-implementation/README.md`
- `docs/05-implementation/multi-agent-practice/README.md`
- `docs/05-implementation/multi-agent-practice/agent-registry.md`
- `docs/05-implementation/multi-agent-practice/templates/incident-report-template.md`
- `docs/05-implementation/multi-agent-practice/templates/agent-handoff-template.md`
- `docs/05-implementation/multi-agent-practice/templates/development-complete-template.md`
- `docs/05-implementation/multi-agent-practice/templates/validation-report-template.md`
- `docs/05-implementation/multi-agent-practice/templates/production-verification-template.md`
- `docs/05-implementation/multi-agent-practice/worklog/prod-diagnosis.md`
- `docs/05-implementation/multi-agent-practice/worklog/product.md`
- `docs/05-implementation/multi-agent-practice/worklog/dev.md`
- `docs/05-implementation/multi-agent-practice/worklog/qa.md`
- `docs/05-implementation/multi-agent-practice/runs/2026-06-27-docs-practice.md`

### 验证命令和结果

```bash
find docs/05-implementation/multi-agent-practice -type f | sort
git diff --check
```

结果：

- `find` 确认登记表、五个模板、四个 worklog 和本 runs 文件均已存在。
- `git diff --check` 无输出，未发现空白格式问题。

### 未验证 / 风险

- 已创建真实 Codex 长期线程。
- 自动跨线程发送尚未执行。
- 未访问真实生产环境。
- 本次不涉及业务代码和部署。

### 请求验收的项目

- 文档入口是否清晰。
- 四 Agent 角色是否完整。
- 消息链是否闭环。
- 是否明确 QA pass 与 production_fixed 的区别。

## 4. Initial Validation Report

- message_id: 2026-06-27-docs-practice-qa-current-001
- from_agent: qa
- to_agent: product, dev, prod-diagnosis
- related_incident: 2026-06-27-docs-practice-incident-001
- related_version: docs-practice-2026-06-27
- reply_to_message_id: 2026-06-27-docs-practice-devcomplete-001
- status: failed
- validator: qa

### 通过项

- 四 Agent 登记表存在。
- Incident、Handoff、Development Complete、Validation、Production Verification 模板存在。
- 四个 worklog 存在。
- 本 runs 文件记录了完整链路。
- 文档明确本次不访问真实生产、不触发部署。
- `git diff --check` 通过。

### 不通过项

- 首轮 QA 发现真实线程投递状态、dev 状态和 worklog unresolved 字段有过期描述。
- 主控线程已修复这些状态字段，并发送 `2026-06-27-docs-practice-recheck-001` 给 QA 复验。

### 阻塞项

无。

### 未证明项

- 真实生产复核不适用于本次文档级演练。
- prod-diagnosis 文档级复核尚未返回最终结论。

### 需要开发 Agent 修复的问题

已修复首轮 QA 指出的状态字段不一致。QA 第二次 recheck 已通过。

## 5. Document-Level Production Verification

- message_id: 2026-06-27-docs-practice-prodverify-real-001
- from_agent: prod-diagnosis
- to_agent: product, qa, dev
- related_incident: 2026-06-27-docs-practice-incident-001
- reply_to_message_id: 2026-06-27-docs-practice-second-recheck-qa-001
- status: document_flow_verified
- verified_at: 2026-06-27
- production_surface: 本地文档级演练，不涉及真实线上服务

### 复核路径

QA 第二次 recheck 已返回 pass，结果已投递给 `prod-diagnosis` 线程。`prod-diagnosis` 线程只读复核后返回 `document_flow_verified`。

### 生产证据

本次目标是“文档级协作机制落地”，不是线上业务修复。`prod-diagnosis` 只确认文档级闭环，不能写成线上业务 `production_fixed`。

```bash
find docs/05-implementation/multi-agent-practice -type f | sort
git diff --check
```

### 结论

`document_flow_verified`。四 Agent 真实线程链路已完成最后一跳确认。

真实线上问题仍必须使用真实生产证据，不能复用本结论。

### 后续边界

- 真实线上问题仍需单独执行生产证据闭环。
- 本次结论不代表任何线上业务问题已修复。

## 6. Index Responsibility Supplement

- message_id: 2026-06-27-index-maintenance-001
- from_agent: main
- to_agent: product, dev, qa
- status: documented

### 背景

四 Agent 闭环跑通后，补充两个长期沉淀责任：

- 管理 / 产品 Agent 需要沉淀产品模型、业务对象、状态机、数据流程设计和数据流转索引。
- 执行 / 开发 Agent 需要沉淀项目代码架构、项目逻辑结构、代码入口、模块边界和测试入口。

### 落地文件

- `docs/05-implementation/multi-agent-practice/index-maintenance.md`
- `docs/00-index/project-dataflow-index.md`
- `docs/00-index/project-structure-index.md`

### 协作要求

- product 交接给 dev 前，必须说明 PRD/专项设计/数据流转索引是否更新。
- dev 交接给 qa 前，必须说明项目结构索引是否更新；若改动 API、worker 或页面数据流，也要说明数据流转索引是否同步更新。
- qa 验收时要读取对应索引入口，不能只看代码 diff 或 Agent 口头说明。

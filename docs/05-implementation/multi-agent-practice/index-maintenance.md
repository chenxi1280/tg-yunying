# Agent 索引沉淀协议

多 Agent 协作不能只传递“这次做了什么”，还要沉淀“下一次从哪里理解产品和代码”。本协议规定产品 Agent 和开发 Agent 必须维护的共享索引。

## 1. 共享索引入口

| 索引 | 维护责任 | 用途 |
| --- | --- | --- |
| `docs/00-index/project-dataflow-index.md` | product 主责，dev/qa/prod-diagnosis 补证 | 记录业务数据从页面、API、service、模型、外部系统到测试的流转路径。 |
| `docs/00-index/project-structure-index.md` | dev 主责，product/qa 补语义 | 记录项目结构、代码入口、业务域边界、核心文件、方法和测试入口。 |
| `docs/01-product/`、`docs/03-feature-designs/` | product 主责 | 记录产品模型、业务口径、PRD、专项设计和验收标准。 |
| `docs/04-ops/` | prod-diagnosis/ops 主责，product 补口径 | 记录生产部署、线上排障、worker、GitHub Actions 和真实环境复核口径。 |

`tg-yunying` 的默认真相源顺序：

1. 产品口径：`docs/01-product/tg-ops-platform-prd.md` 和 `docs/03-feature-designs/`。
2. 数据流转：`docs/00-index/project-dataflow-index.md`。
3. 代码结构：`docs/00-index/project-structure-index.md`。
4. 生产运行：`docs/04-ops/deployment/PRODUCTION_RUNTIME.md` 和真实线上证据。
5. 发布路径：`master -> release -> GitHub Actions Deploy Production`。

## 2. 管理 / 产品 Agent 必须沉淀的内容

产品 Agent 不只是把问题转成开发任务，还要维护“产品理解层”。

每次需求、缺陷范围或线上问题闭环涉及业务规则变化时，产品 Agent 必须检查并更新：

- 产品对象：涉及哪些业务对象、角色、页面、状态、权限和操作。
- 数据流转：数据从哪里产生、经过哪些页面/API/service/worker、落到哪些表或外部系统。
- 状态机：关键状态如何变化，哪些状态是用户可见，哪些状态只用于后台执行。
- 责任边界：哪些属于产品口径，哪些属于技术实现，哪些属于线上运维证据。
- 验收口径：QA 应该验证页面、接口、数据落点、后台任务还是生产现象。
- 索引回写：如果新增/调整业务流，必须在 `project-dataflow-index.md` 或相关 PRD 中留下可检索入口。

产品 Agent 交给开发 Agent 时，必须附带：

```text
## 产品沉淀

- intake_id:
- level:
- route:
- product_docs:
- dataflow_index:
- affected_business_objects:
- affected_pages:
- affected_api_or_worker_flows:
- state_transitions:
- acceptance_contract:
- release_gate_required:
- production_verification_required:
- resync_policy:
- unresolved_product_questions:
```

如果产品 Agent 更新了 PRD、专项设计、数据流转索引、验收标准或线上复核口径，且开发或 QA 已经开始工作，必须发送 `resync` 消息给 dev 和 qa：

```text
## Resync Message

- message_type: resync
- supersedes_message_id:
- changed_docs:
- changed_requirements:
- deprecated_requirements:
- still_valid_requirements:
- should_dev_pause: true | false
- should_qa_rebuild_checklist: true | false
- reason:
```

## 3. 执行 / 开发 Agent 必须沉淀的内容

开发 Agent 不只是完成代码修改，还要维护“代码理解层”。

每次改动代码结构、业务域入口、API、service、worker、模型、schema、前端页面或测试入口时，开发 Agent 必须检查并更新：

- 代码入口：本次功能从哪个前端页面、action、API router、service、worker 进入。
- 依赖路径：调用链经过哪些 service/helper/model/external adapter。
- 数据落点：改动影响哪些 ORM model、migration、缓存、文件、外部服务或 runtime summary。
- 测试入口：对应哪些后端测试、前端数据流测试、静态检查或生产验证命令。
- 结构索引：如果新增/移动/拆分文件或业务边界，必须更新 `project-structure-index.md`。
- 数据流索引：如果新增/修改 API、worker 流转、状态落点或页面数据加载契约，必须同步补充 `project-dataflow-index.md`。

开发 Agent 交给 QA 时，必须附带：

```text
## 代码索引沉淀

- intake_id:
- level:
- locked_paths:
- merge_owner:
- structure_index:
- changed_entrypoints:
- changed_modules:
- changed_data_models:
- changed_api_or_worker_flows:
- tests_or_checks:
- index_updates:
- dataflow_index_updates:
- known_gaps:
```

开发 Agent 接收任务前必须先复核：

- `ready_status` 是否为 `ready`。
- `locked_paths` 是否与其他任务冲突。
- `depends_on` 是否已完成。
- level 是否低估了生产、worker、数据库、权限或 TG 真实链路风险。
- 产品是否已明确 `dataflow_index: updated | unchanged | unproven`。

复核不通过时，不要硬做，回传 `missing_inputs` 或 `rejected`。

## 4. 什么时候必须更新索引

以下情况不能只写 worklog，必须更新共享索引或明确写 `unproven/unchanged`：

- 新增、删除或改名 API 路由。
- 新增、删除或移动前端页面、弹窗、全局 action。
- 新增、删除或改变 worker、dispatcher、listener、recovery、metrics 流程。
- 业务状态字段、状态机、权限、账号选择、任务执行口径发生变化。
- PRD 或专项设计改变了数据来源、数据落点、验收口径。
- 线上排查发现实际生产流转与文档索引不一致。
- `agent-status-board.md` 中 level、route、Release Gate 或证据等级发生变化。
- quick_fix 过程中发现范围扩大，必须升级标准流程并补索引判断。
- Bug Batch Plan 发现共同根因，必须补充根因所在的业务流或代码入口。

## 5. 不需要更新索引的情况

以下情况可以只在 worklog 或 run 记录中说明：

- 纯文案调整，不改变业务规则和数据流转。
- 只改测试断言，且没有改变产品口径或代码入口。
- 只修复格式、拼写、链接。
- 本次只是只读验收或只读生产复核。

即使不更新索引，也要在交接消息中写明：

```text
index_updates: unchanged
reason:
```

## 6. 关闭条件

一个涉及产品或代码流转的任务，只有同时满足以下条件，才可以从多 Agent 链路关闭：

- product 已确认 PRD/设计/数据流转索引是否需要更新。
- dev 已确认代码结构索引和数据流转索引是否需要更新。
- qa 已按索引入口复验关键路径。
- prod-diagnosis 对线上问题额外确认生产证据；文档级任务只输出 `document_flow_verified`。
- `agent-status-board.md` 已更新最终 `done_status`、`evidence_level` 和 `release_gate`。
- 复盘发现的新规则已进入 Rule Backfill，落到 AGENTS.md、skill、模板、PRD、索引或运行手册之一。

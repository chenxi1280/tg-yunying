# 2026-06-27 Agent 协作协议升级记录

- intake_id: intake-agent-protocol-upgrade-001
- message_id: 2026-06-27-agent-protocol-upgrade-001
- level: L1
- lane: docs
- owner_agent: product
- current_agent: main
- evidence_level: E1
- release_gate: not_required
- status: done

## 背景

原目录已经能完成四 Agent 文档级演练，但仍偏“演练材料”：缺少 AI 自运行入口、状态看板、Intake/Triage、quick_fix、批量并行、Release Gate、产品验收和复盘反补模板。

## 本次更新

- 新增项目级 `AGENTS.md`，让后续 AI 进入 `tg-yunying` 时先读取多 Agent 协作协议。
- 将 `multi-agent-practice/README.md` 升级为 AI 自运行协议入口。
- 扩展 `agent-registry.md`，补齐可扩展专项 Agent、路由规则、分级复核权、并行和快修规则。
- 新增 `agent-status-board.md`，作为跨 Agent 共享状态。
- 更新 `index-maintenance.md`，补齐 `resync`、Ready 复核、索引强制更新条件和关闭条件。
- 更新现有 Incident、Handoff、Development Complete、Validation、Production Verification 模板。
- 新增 Intake、Triage、Mini Bug、Bug Batch Plan、Product Acceptance、Release Gate、Rule Backfill 模板。
- 更新 `docs/05-implementation/README.md` 和 `docs/00-index/README.md` 的入口说明。

## 线程同步

已把新协议同步到四个长期 Agent 线程，并收到 ACK：

| agent_key | thread_id | sync_message_id | ack_status |
| --- | --- | --- | --- |
| prod-diagnosis | 019f07c6-92b5-7c50-b7e2-2f18a107e006 | 2026-06-27-agent-protocol-sync-prod-diagnosis-001 | acknowledged |
| product | 019f07c6-d189-7b21-bed2-695abe7b4918 | 2026-06-27-agent-protocol-sync-product-001 | acknowledged |
| dev | 019f07c6-f550-73e3-998b-b130da2c1898 | 2026-06-27-agent-protocol-sync-dev-001 | acknowledged |
| qa | 019f07c7-1c0d-72a2-95fe-9f618aff0a00 | 2026-06-27-agent-protocol-sync-qa-001 | acknowledged |

同步口径：

- 本次只更新线程上下文，不要求各 Agent 修改文件、部署或访问生产。
- 各 Agent 后续以 `AGENTS.md` 和 `docs/05-implementation/multi-agent-practice/` 为协作基线。
- product 必须按 Intake/Triage/PRD/数据流转索引分诊并投递 dev。
- dev 必须接收前复核 Ready、locked_paths、depends_on、Release Gate 和索引要求。
- qa 必须输出 `qa_pass` / `failed` / `blocked` / `unproven`，并在通过后通知 product 做验收。
- prod-diagnosis 只在真实生产 E4 证据充足时输出 `production_fixed`。

## 验证

```bash
find docs/05-implementation/multi-agent-practice -maxdepth 3 -type f -print | sort
rg -n "AI 自运行|agent-status-board|quick_fix|Bug Batch Plan|Release Gate|product_accepted|production_fixed|resync|locked_paths|merge_owner|Intake Card|Triage" AGENTS.md docs/05-implementation docs/00-index/README.md
```

结果：

- 多 Agent 协作目录文件齐全。
- 未发现冲突标记或行尾空格。
- 关键协议字段可检索。

## 未做

- 未修改业务代码。
- 未运行后端或前端测试。
- 未触发发布。
- 未访问真实生产环境。

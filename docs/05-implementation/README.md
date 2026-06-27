# 05-implementation：实施清单

本目录保存“从当前代码到 PRD”的执行清单。

- [tg-ops-platform-prd-refactor-checklist.md](tg-ops-platform-prd-refactor-checklist.md)
- [multi-agent-practice/](multi-agent-practice/)：`tg-yunying` 多 Agent 协作协议。AI 读取后应直接初始化状态看板、分诊 Intake/Triage、推动开发/QA/产品验收/线上复核闭环，而不是只解释文档。

维护规则：

- 清单是执行入口，不是产品源头。
- 每次 PRD 或专项验收口径变化，都要同步检查清单是否需要更新。
- 多 Agent 协作必须同步维护 `multi-agent-practice/agent-status-board.md`、产品数据流转索引和项目结构索引。

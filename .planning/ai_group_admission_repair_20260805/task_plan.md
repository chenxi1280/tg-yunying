# AI 活群准入与频道关注修复

## 目标

审查当前工作区已提交/未提交的 AI 活群修复，补齐 PRD 与数据流转口径，修复学生会 C2 频道关注失败不重建、旧准入状态污染 fact-first 任务的问题，并按生产流程发布与验收。

## 成功标准

- 任务配置、日覆盖分母、账号真实状态、频道关注事实和 C2 readiness 分开定义。
- FloodWait、账号不可用、admission version stale 等失败按远端 mutation 边界分流；只有明确未发生远端变更的频道关注失败能够按原 admission/channel 身份安全重建，已有远端事实不得盲重试。
- fact-first_v3 任务不再把 legacy `GroupBotAdmission` 的 ready 状态当作当前准入真相；旧状态只作审计或显式迁移输入。
- 新增回归覆盖 Action 已存在、失败/跳过、远端未知和已成功事实四类分支。
- PRD、数据流转索引、项目结构索引与代码一致。
- `master -> release -> Deploy Production` 完成；线上 SHA、容器、任务 Action/Attempt/远端事实有独立证据。

## 阶段

1. [completed] 审查现有改动、分支、生产版本和线上状态。
2. [completed] Product Design Complete：补齐故障合同、边界、幂等和回滚口径。
3. [completed] 实现代码与定向回归。
4. [in_progress] 本地/Actions QA 与发布门。
5. [pending] 生产发布和 E4 验收。

## 安全边界

- 不删除历史 Action/Attempt/远端事实。
- 不对 `closed_unknown` 做通用重试。
- 只有有明确 `remote_mutation_started=false` 且 Action 为 pre-Gateway 可恢复终态时，才允许清空旧绑定并重建。
- 生产数据修复如需执行，必须使用精确谓词、预览、审计和可回滚路径；本轮先完成代码/发布合同。

## 错误记录

| 错误 | 次数 | 处理 |
|---|---:|---|
| 发布专项技能内容为 TODO 空模板 | 1 | 按仓库 AGENTS.md、生产运行文档和真实线上证据执行 |
| 工作树已有历史 planning/docs 改动 | 1 | 保留用户改动，新建独立本轮 planning 目录，不覆盖旧计划 |

# AI 活群低完成量及前序修复汇总发布

- intake_id: AI-GROUP-LOW-FULFILLMENT-20260907
- level: L3
- release_owner: Codex；用户已授权修复、汇总提交、发布及线上监督，并确认 Antigravity 暂停生产修改。
- candidate: 本文件所在 master 提交；推送前冻结 SHA，master/release 保持完全一致。
- base: d9c4cf7560cb4e6b9fde3fb05491cf1c315f611b；包含前序 2d6ce013 的全部频道关注/预约修复。
- production_baseline: 发布标记 3ed0e062，存在不同 worker 热修改不一致；以正式镜像统一所有角色。
- status: local_passed；CI、发布、E4 pending。

## 范围与验证

1. AI 词频历史查询：修复全表 JSON 扫描、LIMIT 前漏过滤、重复事实挤占窗口及事实租户关联；不改变阈值。8 个定向测试通过，其中 3 个新增反例先红后绿。
2. 0227 仅增加两个索引：Action tenant/type/surface 表达式和完整群上下文时间索引。SQLite 升降重复执行、真实 PG 升级与 EXPLAIN ANALYZE 使用索引通过；完整增量升级及迁移图组合 15 项通过。
3. 前序频道修复：强制先关注、排程窗口、Gateway 并发/冷却、保留正常来源间隔与未到期预约；评论可见性只查真实目标 peer；未知 source admission 不清除。相关回归已通过。
4. 账号来源跨日修复：使用绑定 ledger/冻结 due，不按多个候选日期搜可用账号计划。跨日/跨租户/相册回归 8 项通过。
5. 现有 MessageBrief/结构化 schema 的长度合同对齐及测试纳入汇总，具体内容政策和质量阈值不另行放宽。
6. 核心回归组合 170 项通过；首轮迁移图单项因根目录运行缺 alembic.ini，已在 backend 工作目录重新通过。追加质量/worker/PG 并发回归 84 项通过；来源收口后的 8 项回归再次通过。

## 迁移、回滚与发布

PostgreSQL 索引使用 CONCURRENTLY 创建，重复执行保留 valid 索引；本迁移同名 invalid 残留先并发移除再重建。不修改任务配置/分母、Action/Attempt 或 Telegram 事实。增加索引兼容旧代码，若仅回退应用可保留索引；不降级 0226，不恢复退役任务，不重放 unknown。

正常 `master -> release -> GitHub Actions Deploy Production`，额外诊断 drain/retry/apply 开关全部 false。冻结候选、5 个后端分片、前端与镜像全部通过后部署。独立确认 current symlink、全部运行角色 SHA、健康、0227 和索引 valid。

## 线上验收与监督

以正式发布完成时刻为锚，逐个运行 AI 活群读取任务日目标/due/confirmed、生成状态、到期 ready、Attempt/Gateway、同账号/同 ledger typed message fact。核对词频/上下文 EXPLAIN 与实际队列推进，记录每个任务的首个阻塞点。频道 like/view/comment 分别读取正确类型事实及关注关系，不以 healthy 或 Action success 自证完成。首次恢复少量发送只能称路径恢复，完整日目标与剩余欠量仍需监督。只读监控不自动清理、重试、改配置或伪造业务完成。

## 首轮 CI 回流

候选 2fc598cb、Actions 34126399484：PG 两分片和前端通过；非 PG 分片暴露 Provider schema 四个旧参数化用例仍期望已移除的 micro 档。修正断言与现有 MessageBrief/结构化 schema 一致，保留 general 的 micro/short/medium 三档及原质量硬门。首轮未构建镜像、未操作生产。重新定向验证后形成下一候选，仍需完整 CI。

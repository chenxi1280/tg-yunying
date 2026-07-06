# AI 活群 hard-hourly 补齐计划

## 目标

补齐线上 AI 活群 hard-hourly 账号分布问题的三个缺口：

1. 旧的偏斜 open action 可被重新规划前显式跳过。
2. 账号在线不可用时在任务 stats 中暴露可核查的账号样本。
3. 新规划写库前校验账号分布，偏斜批次阻断并写入 blocker。

## 验收

1. 先新增失败测试，覆盖旧计划清理、在线状态样本、分布门禁。
2. 实现后 targeted backend tests 通过，且不引入 silent fallback 或 mock success。
3. 同步更新 PRD、数据流转索引、结构索引和协作状态。
4. 发布后通过 SSH 直连生产验证新逻辑证据。

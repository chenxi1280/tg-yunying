# 生产任务根因诊断计划

## Goal
用当前生产、部署时间线与代码路径证据解释 AI 活群、搜索点击、评论等任务异常的直接原因，并标明已证实、推断和未证实项。

## Current Phase
Complete

## Phases

### Phase 1: 生产时间线与失败链
- [x] 核对部署前后 Action、Attempt、远端事实变化
- [x] 核对 Dispatcher/Planner/PostgreSQL 日志与当前锁状态
- **Status:** completed

### Phase 2: 代码差异与锁顺序
- [x] 审查 `87fe0bf0` 相对稳定基线的相关改动
- [x] 将 deadlock 查询与具体代码事务顺序对应
- **Status:** completed

### Phase 3: 各任务根因归类
- [x] AI 活群逐任务归因
- [x] 搜索、浏览、点赞、评论分别归因
- [x] 区分共因、独立阻塞和业务边界
- **Status:** completed

### Phase 4: 复采样与结论
- [x] 验证异常是否持续
- [x] 输出证据强度明确的结论
- **Status:** completed

## Key Questions
1. AI 活群为什么在新版本上线后没有新增业务确认？
2. Dispatcher deadlock 的锁环来自哪些事务顺序？
3. 搜索点击为什么能增长但仍存在日目标缺口？
4. 评论和部分点赞没有新事实，是阻塞还是没有新业务输入？

## Decisions Made
| Decision | Rationale |
|----------|-----------|
| 全程只读 | 用户要求原因诊断，未授权修复或生产变更 |
| 业务事实优先于容器健康 | running/healthy 不能证明任务完成 |
| 以实时容器配置和实时窗口为准 | Task stats 中的 `shard_total=4` 是陈旧诊断字段，当前实际拓扑为 1 与 2 混用 |

## Errors Encountered
| Error | Attempt | Resolution |
|-------|---------|------------|
| queried shard columns from reservation instead of shard allocation | 1 | corrected query to select shard columns from joined allocation |
| used jsonb `?` operator on JSON column | 2 | changed predicate to `stats->'dispatch_claim' IS NOT NULL` |
| production Action query referenced nonexistent `actions.updated_at` | 3 | used `COALESCE(executed_at, created_at)` |
| one SSH diagnostic connection closed by remote host | 4 | retried once with bounded connect timeout |

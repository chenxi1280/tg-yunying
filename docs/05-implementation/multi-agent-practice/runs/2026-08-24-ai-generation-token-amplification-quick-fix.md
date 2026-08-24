# 2026-08-24 AI 生成 Token 放大快速修复

## Mini Bug Card

- bug_id: `AI-GROUP-TOKEN-AMPLIFICATION-20260824`
- intake_id: `production-ai-content-quality-20260824`
- level: `L3`
- route: `incident -> product -> dev -> qa -> product -> prod-diagnosis`
- owner_agent: `product/main`
- evidence_level: `E4`
- locked_paths: AI generation guard、Dispatcher、消息记忆/窗口查重、定向测试、AI 内容 PRD 与索引
- release_gate: `required`

## 现象

生产自当前 release 启动后，普通 AI 活群正文已经调用 Provider 并落为 ready，Gateway 前因真人上下文增加而清空候选、立即把原 Action 重排到当前时间，再次调用 Provider。只读快照中 `773` 个 Action 累计发生 `1032` 次 `context_superseded_requeue`；同期 `2072` 个 obligation 产生 `3688` 个 GenerationJob、`2713` 个已计 Token Action 共消耗 `3324808` Token。生成后现有查重以同账号历史为主，不能阻止同一群窗口内不同账号发送完全相同的归一化正文。

## 期望结果

- 普通非回复正文一旦生成并预占消息记忆，新增真人上下文只记录 `context_drift_observed`，不得清空 candidate、立即重排或再次调用 Provider。
- 生成前仍刷新最新上下文；回复目标、scope、内容政策、账号状态、最终消息记忆和 Gateway 门禁保持不变。
- 生成结果落库时，同租户同群 5 分钟窗口内跨账号的完全相同归一化正文必须以 `duplicate_message` 显式拦截，不能进入 Telegram。
- 同账号跨群 10 天精确/近似/语义/模板壳句规则保持不变；不同群的不同账号不互相硬阻断。

## 限定范围

- 不开启 V2、不创建 policy/attestation/binding、不修改生产任务配置或目标量。
- 不清理历史 Action、GenerationJob、消息记忆或 Token 记录。
- 不修改回复目标失效、Gateway unknown、账号 pacing、数量 obligation 或 Telegram 重试语义。

## 快速验收方法

- 定向测试证明普通 ready candidate 在上下文前进后保持正文、memory、candidate hash 和 Action 状态，且仅增加 drift 证据。
- Dispatcher Gateway 前复核证明普通 ready candidate 不再进入 `context_superseded_requeue`，回复类行为维持原拦截。
- SQLite 与 PostgreSQL 定向测试证明同群跨账号 5 分钟精确重复被拦截，并发 reservation key 以群窗口为原子边界。
- 发布后以部署时间为锚，比较 `context_superseded_requeue` 新增量、GenerationJob/obligation、duplicate gate、Token/成功 E4、任务目标和发送时间分布。

## 回滚

回滚仅回到上一不可变应用 release；本修复无 schema migration、无生产数据 apply。历史 drift/duplicate 证据保留，不重放 Telegram，不删除业务事实。

## Release Gate

- message_id: `release-ai-group-token-amplification-20260824`
- intake_id: `production-ai-content-quality-20260824`
- from_agent: `dev`
- to_agent: `product, qa, prod-diagnosis`
- level: `L3`
- release_mode: `github_actions`
- release_owner: `main`
- rollback_owner: `main`
- status: `pending`

### 上线范围

只发布 frozen candidate 与同群 5 分钟生成后精确查重；不带生产配置、数据 apply、migration、任务开关或历史清理。

### 必须满足

- ci_or_build: `git diff --check`、Python compileall 与 Deploy Production 全 jobs。
- backend_tests: 两组 60 秒内定向 no_postgres 回归，合计 `229 passed`。
- frontend_build: 无前端改动；仍由 Deploy Production workflow 全量执行。
- migration_impact: 无 migration。
- worker_impact: generation worker 与 dispatcher 载入新代码后生效，不新增 worker/队列。
- external_platform_impact: 只减少重复 Provider 调用并在重复正文时阻止 Telegram 调用；不主动补发或重放。
- rollback_plan: 回到上一不可变 release；无 schema/data rollback。
- observe_window: 从新容器 StartedAt 起连续观察至少两个 generation/dispatcher 周期，并读取新 E4。

### 发布后复核

- production_probe: expected SHA、current symlink、容器 StartedAt/health、migration revision。
- logs_or_actions: 新 `context_superseded_requeue=0`、drift evidence、duplicate gate、GenerationJob/obligation、Token/E4、小时目标和发送时间分布。
- owner: `prod-diagnosis/main`

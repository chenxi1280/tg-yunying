# 2026-08-25 AI V2 成人路由生产激活

## Mini Bug Card

- bug_id: `AI-V2-VOICE-THINK-WRAPPER-20260825`
- intake_id: `production-ai-v2-adult-route-activation-20260825`
- level: `L3`
- route: `prod-diagnosis -> product -> dev -> qa -> product -> prod-diagnosis`
- owner_agent: `product/main`
- evidence_level: `E4`
- locked_paths: `account_voice_profile_generation.py`、对应定向测试、AI 质量 PRD、本运行记录
- release_gate: `required`

## 现象

生产八个运行中的 AI 活群任务均未启用 V2。受保护 bootstrap preview 进一步发现，候选任务账号声线覆盖缺 11 条；对历史 `manual_required` 条目做一次带 CAS、幂等键和 AuditLog 的人工重试后，MiniMax-M2.5 调用成功但解析统一进入 `voice_profile_output_malformed`。

结构化现场探测只输出分类统计，不读取正文：响应共 19 个非空行，包含一对 `<think>...</think>`、16 行 reasoning 和一个 JSON 对象。当前严格 JSONL 解析器只处理代码围栏，不处理 Provider 明示 reasoning block，因此在业务 JSON 到达前失败。

首轮修复以 `e05572763c14f5c07cffb8ba24011289ca61410a` 发布后，生产读回 1 个账号已成功形成 active 面具；其余样本暴露两个后续根因：MiniMax HTTP 429 被通用字符串分类误判为 `provider_config_invalid` 并首次即终止，以及 512 token 被 reasoning 占满后没有剩余 JSON。第二轮修复将 429 明确映射为可退避状态，并把声线单条上限调整为 1024，不改变群聊正文生成预算。

第二轮发布 `efe8f31bedc7ec85505ba7d6d1776b4dbac73494` 后，楼凤声线已通过严格字段门禁补齐到 1061/1061，Provider 4/5 价格与两类成人证明完成受审计读回。bootstrap 数据流复核又发现 paused epoch 上创建的 binding 会在 resume 增加 lifecycle epoch 后失效；正式 start 事务必须在 epoch 推进后重新执行既有 V2 activation，生成当前 epoch binding，再允许任务恢复运行。

binding 修复以 `8ed9a4ea7d75faf5f2b1232ab4022ccd122248f1` 成功发布后，生产只暂停 `郑州楼凤` 单任务。暂停前锁内读回 `gateway_started=0`、`unknown=0`，但暂停后 Action 保持 51、GenerationJob 从 238 仅降至 229 后不再变化。代码复核确认 generation/dispatcher claim 都要求 Task running 且 lifecycle epoch 相等，因此 paused 旧 epoch 永远不会再被 worker 领取；bootstrap 的 open-work=0 门禁在现合同下不可达。

暂停清理修复 `97e00491c63f72f50e5204c94a67439fc1d4aa0a` 发布后，同一 paused epoch 的 Action 51→0、GenerationJob 229→0，审计 703007 记录 `generation_cleanup=complete`。完整 preview 得到 blockers=[]，但首次正式 apply 被严格 schema 拒绝并回滚：生产 fact-first 运行时曾把 `ai_provider_id=4` 写回 Task，而 GroupAIChatConfig 禁止该 runtime-only extra。V2 必须在 bootstrap 事务内显式移除旧单 Provider 绑定并改由 purpose routes 接管。

旧 Provider 迁移修复发布后，第二次 apply 又暴露生产 `SessionLocal(autoflush=False)` 与测试默认 Session 的差异：bootstrap 已暂存新 route items，但 activation 在显式 flush 前查询，`group_semantic_review` 被误判为空候选，事务再次完整回滚。bootstrap 必须在 route set 与全部 route items 写入后显式 flush，再进入 task activation；回归测试必须使用与生产一致的 `autoflush=False`。

## 期望结果

- 仅移除完整闭合的 `<think>...</think>` Provider reasoning block，再执行原严格 JSONL、必填字段、男性身份、列表数量、摘要长度和敏感措辞校验。
- 未闭合 reasoning block、额外非 JSON 行、缺字段、稀疏列表或非法身份继续显式失败。
- 不生成静态面具、不吞异常、不降低质量门禁、不自动重试生产 `manual_required`。
- 修复发布并读回后，精确 11 个条目才允许以新的人工 idempotency key 再次重试。

## 限定范围

- 本代码修复不启用 V2、不创建 policy/attestation/binding、不暂停任务、不改任务目标。
- 不改变 Provider、模型、价格、route 优先级或 Telegram 发送链路。
- 生产成人路由仍须通过独立的受保护 bootstrap preview/apply/readback。

## 快速验收方法

- 新增 MiniMax think wrapper 正例，证明闭合 reasoning block 后单行 JSON 通过。
- 新增未闭合 think wrapper 反例，证明不会搜索并提取任意尾部 JSON。
- 完整声线解析与 worker 定向测试全部通过，60 秒硬超时。
- Provider HTTP 429 回归必须落到 `retry_wait/provider_rate_limited`，不得落到 `manual_required/provider_config_invalid`；声线请求上限固定为 1024。
- paused V2 任务 resume 后必须同时保留历史 binding，并新增当前 lifecycle epoch、同 config revision 的 binding。
- group AI pause 无远端不确定性时必须释放旧 Action 计划并取消开放 GenerationJob；存在 unknown/Gateway-started/gateway-bound 时必须保留事实和可见审计 blocker，不得为通过 bootstrap 强制清理。
- bootstrap 对生产旧 `ai_provider_id` 必须 preview 指纹化、apply 显式移除并审计；不得放宽 schema 或在线手改 JSON。失败 apply 必须保持 policy/routes/task/audit 零持久化。
- bootstrap route 写入与 activation 必须在 `autoflush=False` 下仍具备事务内可见性；测试不得依赖 SQLAlchemy 测试 Session 的默认 autoflush 掩盖生产失败。
- binding 修复定向测试 `test_ai_content_policy.py + test_ai_v2_canary_bootstrap.py` 为 `16 passed`；本地没有 PostgreSQL 测试库，start/resume 的 PostgreSQL 扩展选择集由 Deploy Production 双矩阵门禁验证。
- 发布后精确重试 11 条，独立读回 active voice profiles 11/11，随后重新运行 bootstrap preview。

## 升级与回滚

- 已是 L3 标准流程；任何字段校验放宽、Provider 响应正文提取、任务范围变化或 migration 都需重新 product resync。
- 回滚到上一不可变 release；本代码提交无 schema/data 自动 apply，既有重试/AuditLog 事实保留。

## Release Gate

- message_id: `release-ai-v2-voice-think-wrapper-20260825`
- intake_id: `production-ai-v2-adult-route-activation-20260825`
- from_agent: `dev`
- to_agent: `product, qa, prod-diagnosis`
- level: `L3`
- release_mode: `github_actions`
- release_owner: `main`
- rollback_owner: `main`
- status: `route_flush_fix_pending`

### 上线范围

第一轮已发布 MiniMax 闭合 reasoning 前缀规范化；第二轮仅增加 HTTP 429 类型化退避、声线单条 1024 token 上限及对应回归。均不随发布修改 Provider、任务配置、policy、attestation、binding、目标量或 Telegram 数据。

### 必须满足

- ci_or_build: `git diff --check`、目标模块 compileall、Deploy Production 全 jobs。
- backend_tests: 60 秒硬超时内声线解析与 worker 定向回归 `64 passed`。
- frontend_build: 无前端改动；仍由 Deploy Production workflow 执行正式构建。
- migration_impact: 无 migration。
- worker_impact: `worker-voice-profile` 载入新解析器；不新增 worker、并发或轮询。
- external_platform_impact: 只有后续显式重试才调用 Provider；发布本身不调用 Provider、不发送 Telegram。
- rollback_plan: 回到上一不可变 release；无 schema/data rollback。
- observe_window: 发布后对 11 个精确条目使用新 idempotency key 单次重试并读回，不盲目重复。

### 发布后复核

- production_probe: expected SHA、current symlink、backend/voice-profile worker SHA 与 health。
- logs_or_actions: 11 个条目状态、active voice profile 数、bootstrap preview blockers；成人 route 与 Telegram E4 后续独立验证。
- owner: `prod-diagnosis/main`

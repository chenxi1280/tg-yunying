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
- status: `pending`

### 上线范围

仅发布 MiniMax 闭合 reasoning 前缀规范化、两条解析回归、PRD 与运行记录；不随发布修改 Provider、任务配置、policy、attestation、binding、目标量或 Telegram 数据。

### 必须满足

- ci_or_build: `git diff --check`、目标模块 compileall、Deploy Production 全 jobs。
- backend_tests: 60 秒硬超时内声线解析与 worker 定向回归 `63 passed`。
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

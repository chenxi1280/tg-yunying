# Findings

## Historical Production Snapshot

- 2026-06-12 快照显示 3 个 AI 活跃群不是同一根因：
  - 天津音乐学院：membership/verification 与 dispatcher/backlog 混合。
  - 青岛师范学院：入群/验证/can_send 缺口为主，图形验证码和人工处理多。
  - 石家庄修车：membership_ready，但 dispatcher lag 和 AI draft 失败为主。
- 共同风险：`AI provider returned malformed JSON drafts`、`global_pending` 大、`hard_hourly_overdue_open_count` 高。

## Current Workspace

- 当前分支：`release`。
- 当前已有未提交改动：
  - `backend/app/services/task_center/executors/group_ai_chat.py`
  - `backend/tests/test_ai_task_limits.py`
- 已有改动在优化 reply target / cycle scan，不能覆盖。

## Initial Code Findings

- `backend/app/services/membership_challenges.py` 已有 MiMo 图片验证码路径：查找健康 provider，下载图片，调用 `ai_gateway.solve_image_verification`，记录 attempt，低置信或重复图片转人工。
- `backend/app/services/task_center/channel_membership.py` 已有 `require_send` 参数和 `can_send` 过滤，但需要继续确认 group target 是否会在未授权/不可发送时强制准入。

## Supervisor Findings

- 监督子代理确认必须补组合证据：membership action -> dispatcher -> `TgGroupAccount.can_send=True` -> 下一轮 planner 创建 send action -> send dispatcher 不被 `account_target_permission` 拦截。
- 当前验证码分类依赖 marker；文本验证码、加减验证码和多个频道关注未形成完整自动处理证据。
- AI 群聊生成当前会按任务 `ai_provider_id` / `ai_model`、租户 default、首个健康 provider 选择，缺少小米 MiMo/mino 强制约束。
- hard-hourly 已有 planner/claim 提权，但仍需用组合测试证明 membership 和 send 都能被 drain。

## Implemented Fix Findings

- `mino-v2.5`、`Xiaomi Mino V2.5` 已归一化为 `mimo-v2.5`。
- AI 活跃群文本 draft 默认要求 MiMo/mino family；当健康小米供应商不存在时，抛出明确的 `没有健康小米 MiMo/mino 供应商`，不再静默落到 DeepSeek。
- 群发言权限错误中的普通验证码、加减/算术题会归类为 `发送验证回复`。
- 自动文本验证会读取最新验证上下文，优先提取简单加减答案，其次提取 3-8 位验证码，提交后复检 `can_send`。
- 需要关注多个频道时，会解析 `@username` 和 `t.me/username`，逐个关注后复检群发言权限。
- 新增组合测试覆盖：第一轮只创建准入动作、不创建发送；dispatcher 执行准入写回 `can_send=true`；下一轮 planner 创建 hard-hourly send；send dispatcher 成功。

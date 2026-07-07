# 2026-07-07 channel_comment AI 过程性内容过滤修复

## Incident

- intake_id: intake-2026-07-07-channel-comment-ai-meta-output
- bug_id: bug-2026-07-07-channel-comment-ai-meta-output
- lane: ai-provider/channel-comment
- level: L3
- production_related: true
- release_gate_required: true
- production_verification_required: true

用户截图证明频道评论发出 `<think>`、`让我分析这个频道内容`、`让我仔细分析这个请求` 等 AI 过程性内容。产品目标是让频道评论和已存在 pending action 中的脏评论在进入 Telegram gateway 前被显式过滤，不能静默吞掉，也不能把本地修复写成线上恢复。

## Dev Complete

- message_id: 2026-07-07-channel-comment-ai-meta-filter-devcomplete-001
- status: local_verified_pending_release
- evidence_level: E2

实现范围：

- `content_filters.looks_like_ai_meta_content` 新增 AI 过程性内容识别。
- `ai_generator.clean_channel_comment_contents` 在生成阶段丢弃 `<think>` / “让我分析...” 等候选。
- `dispatcher._dispatch_comment` 在 Telegram gateway 前复用公共出站过滤器，命中后以 `content_policy` / `拦截 AI 过程性内容` 可见失败，不调用 Telegram。

验证：

- `backend/tests/test_ai_gateway.py::test_channel_comment_rejects_thinking_and_analysis_meta_text` passed。
- `backend/tests/test_operations_center_runtime.py::test_channel_comment_pre_send_validation_blocks_ai_meta_text` passed。
- `backend/.venv/bin/python -m compileall -q backend/app` passed。
- `git diff --check` passed。

## QA Validation

- message_id: 2026-07-07-channel-comment-ai-meta-filter-qa-to-product-001
- status: qa_pass
- evidence_level: E2
- release_gate: pending
- production_verification: unproven

QA 通过范围：

- 频道评论生成清洗丢弃 `<think>`、`让我分析这个频道内容`、`让我仔细分析这个请求` 等 AI 过程性候选。
- 频道评论发送前复用公共出站过滤器，命中后以 `content_policy` / `拦截 AI 过程性内容` 失败。
- 旧 pending action 中脏 `comment_text` 不进入 `reply_channel_message` Telegram gateway。
- group send 仍复用公共出站过滤器，AI 过程性内容同样在 gateway 前失败。

QA 证据：

- 定向回归 `2 passed in 0.97s`。
- compileall passed。
- `git diff --check` passed。
- 只读 Python 探针证明 group send action 在 gateway 前以 `content_policy` 失败，gateway monkeypatch 未被调用。

未证明：

- 5 条组合测试因本地 PostgreSQL reset gate 阻断，未作为通过证据。
- 未访问生产、未部署、未取得生产 DB / worker / Telegram 真实发送 E4 证据。

## Product Acceptance

- message_id: 2026-07-07-channel-comment-ai-meta-filter-product-acceptance-001
- status: product_accepted
- evidence_level: E2
- accepted_scope: local code fix + targeted QA regression
- release_gate: pending
- production_verification_required: true
- next_agent: dev
- handoff_delivery_status: sent
- target_thread: 019f07c6-f550-73e3-998b-b130da2c1898

产品接受项：

- `channel_comment` AI 生成阶段过滤 AI 过程性候选。
- 频道评论发送前过滤旧 pending 脏 `comment_text`，失败状态可见且不进入 Telegram gateway。
- group send 出站过滤不回归。
- 失败语义使用 `content_policy` / `拦截 AI 过程性内容`，不使用 silent fallback 或 mock success。

产品未接受 / 未证明项：

- release gate 未通过。
- 生产未部署。
- 生产 worker / DB / Telegram 真实链路未验证。
- 不能写 `production_fixed`。

已真实投递 dev 做 Release Gate。Release Gate 通过并部署后，必须交回 prod-diagnosis 做 E4 production verification。

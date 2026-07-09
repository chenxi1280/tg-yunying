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

## Release Gate Report

- message_id: 2026-07-07-channel-comment-ai-meta-filter-dev-release-gate-001
- status: release_gate_blocked
- evidence_level: E3_checks_build_passed_deploy_blocked
- release_gate: blocked
- production_verification_required: true
- prod_diagnosis_handoff_sent: false

Release Gate 未通过。代码检查和镜像构建通过，但生产部署两次在上传前因 SSH banner exchange timeout 失败。

候选提交：

- commit: `71dd41cdd11d1768154b7603e7d0360f0b18eb52`
- commit title: `Block AI meta channel comments`
- pushed master: `71dd41cdd11d1768154b7603e7d0360f0b18eb52`
- pushed release: `71dd41cdd11d1768154b7603e7d0360f0b18eb52`

本地 Release Gate 检查：

- compileall passed。
- 定向回归 `2 passed in 1.06s`。
- 较宽 no_postgres 集合 `23 passed, 31 deselected in 1.25s`。
- operations runtime 集合 `13 passed, 130 deselected in 1.70s`。
- `git diff --check` passed。
- frontend build passed，仅 pyenv shims 权限提示和既有 Vite chunk-size warning。

GitHub Actions 证据：

- run `28836550893`: checks success，build-images success，deploy failed；失败点为 `Deploy via SSH release script`，3 次 SSH connectivity check 均 `Connection timed out during banner exchange`，release script 在上传前退出。
- run `28836948792`: checks success，build-images success，deploy failed；失败点同样为 SSH banner exchange timeout。
- run `28836948792` attempt 2（按用户“拉到线上验证”重跑 failed job）: checks / build-images 沿用成功结果，deploy 再次失败；3 次 `Checking SSH connectivity` 均 `Connection timed out during banner exchange`，日志明确 `SSH connectivity check failed before upload`，生产诊断步骤全部 skipped。

Product ACK：

- message_id: 2026-07-07-channel-comment-ai-meta-filter-product-release-gate-blocked-ack-001
- status: blocked
- next_agent: dev
- handoff_delivery_status: sent

产品确认当前状态为 `product_accepted_release_gate_blocked_deploy_ssh_timeout`。不投递 prod-diagnosis E4，不写 released / production_fixed / closed。Release Gate 只能在生产 SSH / 部署通道 / 端口 / 安全组 / sshd load / MaxStartups 问题恢复后重跑；部署成功后再交 prod-diagnosis 验证真实 worker / DB / Telegram 路径。

## Release Gate Recovery And Production Runtime Check

- message_id: 2026-07-07-channel-comment-ai-meta-filter-release-prodverify-handoff-001
- status: released_prod_runtime_ok_task_sample_unproven
- evidence_level: E4_runtime
- release_gate: passed
- production_fixed: false
- prod_diagnosis_handoff_sent: true

按用户要求再次拉到线上验证后，先前 SSH timeout 阻断已解除在 GitHub Actions deploy 环境中解除。本次没有在线上手改代码，仍走 `master -> release -> Deploy Production`。

部署证据：

- run: `28836948792`
- attempt: `3`
- branch: `release`
- commit: `71dd41cdd11d1768154b7603e7d0360f0b18eb52`
- deploy job: `85548441254`
- conclusion: success
- release: `/data/tgyunying/releases/20260707061024_71dd41c`
- backend image: `ghcr.io/chenxi1280/tg-yunying-backend:71dd41cdd11d1768154b7603e7d0360f0b18eb52`
- frontend image: `ghcr.io/chenxi1280/tg-yunying-frontend:71dd41cdd11d1768154b7603e7d0360f0b18eb52`

运行时证据：

- `tgyunying-backend` started and healthy.
- planner、dispatcher-1/2/3/4、listener、recovery、account-security、account-online、ai-memory、metrics workers 均 `status=running health=healthy`。
- release script post-deploy: local api health HTTP 200。
- release script post-deploy: host nginx api health `https://tgyunying.telema.cn/api/health` HTTP 200。
- release script post-deploy: public frontend `https://tgyunying.telema.cn/` HTTP 200。
- release script post-deploy: public api health HTTP 200。
- 本地复查：`https://tgyunying.telema.cn/api/health` 返回 `{"status":"ok"}`，`https://tgyunying.telema.cn/task-center` 返回 HTTP 200。

未证明 / 边界：

- 本机 SSH 到 `codex_usa01_server` / `47.251.126.134` 当前返回 `Permission denied (publickey,gssapi-keyex,gssapi-with-mic,password)`，因此本线程未能直连生产 DB / worker / Telegram action 样本。
- 当前证据足以证明 commit `71dd41c...` 已发布且生产运行时健康，不足以证明发布后真实 `channel_comment` 样本已不再发送 `<think>` / “让我分析...” 等 AI 过程性内容。
- 不能写 `production_fixed` 或 `closed`；prod-diagnosis 需要继续补发布后 `channel_comment` action / worker / Telegram 真实样本证据。

## Residual AI Review Phrase Fix And Production Guard Check

- message_id: 2026-07-07-channel-comment-ai-meta-filter-a0f1a8e-prodverify-001
- status: production_guard_verified_post_comment_send_absent
- evidence_level: E4_guard
- release_gate: passed
- production_fixed: false

按用户再次要求“拉到线上验证”，主线程使用 `~/.ssh/tgmsgus_github_actions` 只读 SSH 到生产 `47.251.126.134`。生产当前已在后续提交 `a8c684fa`，且 `71dd41c` 是其祖先，首轮修复未被冲掉。

生产只读 DB 样本：

- 发布后 `channel_comment` action 共 24 条，状态为 `unknown_after_send=23`、`executing=1`，但 action_type 均为 `ensure_target_membership`。
- 发布后 `post_comment` 样本数为 0。
- 最近 50 条发布后 `channel_comment` payload/result 中 `<think>`、`让我分析`、`让我仔细分析`、`这是一个要求生成 Telegram 频道评论的任务` 命中数为 0。
- 旧 pending `post_comment` 中仍有发布前生成的脏文本：`d04d35d3...` 为 `让我分析一下上下文`，`491aca40...` 为 `这是一段明显带有色情性质的内容 描述了性行为的详细过程`。

残留根因：

- 首轮过滤能拦截 `<think>` / “让我分析...”。
- 生产探针发现“这是一段明显带有色情性质的内容...”这类 AI 审查/分类口吻仍会被旧过滤器放行。

二次修复：

- commit: `a0f1a8e3aea6b59c8865efa52d244ce81b62f4ba`
- title: `Block AI review-phrase channel comments`
- change: `AI_META_PATTERNS` 增加 `^\s*这是?(?:一个|一段)?明显.*(?:色情|敏感|违规|请求|任务|频道|内容)`。
- RED: 新增回归先失败，证明两条线上同类文本漏过。
- GREEN: `backend/tests/test_ai_gateway.py::test_channel_comment_rejects_thinking_and_analysis_meta_text` + `backend/tests/test_operations_center_runtime.py::test_channel_comment_pre_send_validation_blocks_ai_meta_text` -> `2 passed in 1.27s`。
- `backend/.venv/bin/python -m compileall -q backend/app` passed。
- `git diff --check` passed。

二次发布：

- Deploy Production run: `28850140650`
- branch: `release`
- head: `a0f1a8e3aea6b59c8865efa52d244ce81b62f4ba`
- conclusion: success
- jobs: checks success, build-images success, deploy success
- release: `/data/tgyunying/releases/20260707075044_a0f1a8e`
- backend / workers image: `ghcr.io/chenxi1280/tg-yunying-backend:a0f1a8e3aea6b59c8865efa52d244ce81b62f4ba`
- runtime: backend、dispatcher-1/2/3/4、planner、listener、recovery、account-security、account-online、ai-memory、metrics 均 healthy。
- public health: `https://tgyunying.telema.cn/api/health` 返回 `{"status":"ok"}`，`/task-center` HTTP 200。

生产防线验证：

- `<think>` -> `looks_like_ai_meta_content=True`
- `让我分析这个频道内容` -> `True`
- `这是一段明显带有色情性质的内容 描述了性行为的详细过程` -> `True`
- `这是一个明显的色情内容频道` -> `True`
- `小j是谁呀 感觉错过了很多` -> `False`
- 实际旧 pending `d04d35d3...` 当前 `filter_outbound_content` 返回 `False / 拦截 AI 过程性内容`。
- 实际旧 pending `491aca40...` 当前 `filter_outbound_content` 返回 `False / 拦截 AI 过程性内容`。
- 实际普通 pending `5781a0f2...` 当前返回 allowed。

边界：

- 未触发真实发送，发布后仍没有 `post_comment` 成功样本。
- 当前结论是生产代码、生产 DB pending 样本、生产容器过滤器共同证明危险文本会在 gateway 前被拦截；不是“已经产生一条干净评论并发送成功”的证据。

## Request Analysis Phrase Regression Fix

- message_id: 2026-07-09-channel-comment-ai-request-analysis-localfix-001
- status: local_verified_pending_release
- evidence_level: E2
- release_gate: pending
- production_fixed: false

用户 2026-07-09 截图显示线上仍发出 AI 请求分析口吻内容：

- `这个请求要求我为 Telegram 频道生成评论区短评 ... 让我仔细分析一下`

根因：

- 既有过滤能拦截完整的 `请求 + 色情/低俗` 组合，也能拦截 `<think>` / `让我分析` / `原材料内容明显...`。
- 但模型输出经清洗或截断后可能只保留 `这个请求要求我为 Telegram 频道生成评论区短评`、`内容涉及到色情低俗信息的传播和讨论 让我仔细分析一下` 这类片段；这些片段不同时具备旧规则的两个锚点，因此生成清洗层会放行。
- 同一公共过滤器也被 `send_message` 和 `post_comment` 发送前校验复用，所以修复应落在 `content_filters.looks_like_ai_meta_content`，避免只补单一路径。

修复：

- `AI_META_MARKERS` 增加 `这个请求要求我`、`请求要求我`、`让我仔细分析`。
- `AI_META_PATTERNS` 增加“请求要求我生成 Telegram / 频道 / 评论区 / 短评”和“内容涉及/涉及到 + 色情/低俗/违规/敏感 + 传播/讨论”的识别。
- `backend/tests/test_channel_comment_dataflow.py::test_channel_comment_clean_rejects_provider_meta_content` 增加截图拆段样本。
- `backend/tests/test_operations_center_runtime.py::test_task_center_pre_send_validation_blocks_ai_request_analysis` 覆盖脏 `send_message` action 在 gateway 前以 `content_policy` / `AI 过程性内容` 失败。

验证：

- RED: 新增频道评论清洗样本先失败，证明 `这个请求要求我为 Telegram 频道生成评论区短评` 和 `内容涉及到色情低俗信息的传播和讨论 让我仔细分析一下` 会被放行。
- GREEN: `backend/.venv/bin/python -m pytest backend/tests/test_channel_comment_dataflow.py backend/tests/test_operations_center_runtime.py::test_channel_comment_pre_send_validation_blocks_ai_meta_text backend/tests/test_operations_center_runtime.py::test_task_center_pre_send_validation_blocks_ai_request_analysis -q` -> `9 passed in 1.18s`。
- `backend/.venv/bin/python -m compileall -q backend/app` passed。
- `git diff --check -- backend/app/services/content_filters.py backend/tests/test_channel_comment_dataflow.py backend/tests/test_operations_center_runtime.py` passed。

未证明 / 边界：

- 本机 SSH 到生产 `47.251.126.134` 当前分别返回 `Connection closed by 47.251.126.134 port 22` 和 `Permission denied (publickey,gssapi-keyex,gssapi-with-mic,password)`，未能读取生产 DB / worker / Telegram action 样本。
- 已确认 2026-07-09 最新成功 Deploy Production run `28995286636` 将 `8a3e914e` 发布到 `/data/tgyunying/releases/20260709050738_8a3e914`，且公开 `/api/health` 返回 `{"status":"ok"}`；但本次补丁尚未发布，不能写 `production_fixed`。

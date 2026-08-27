# Config2 空 baseline 重新登录修复

## Intake Card

- intake_id: intake-2026-08-28-config2-empty-baseline-login
- source: user
- raw_input: 支持新的 config2 HTTPS 接码地址，查看线上报错日志，修复后对指定账号重新登录并通过登录测试
- created_at: 2026-08-28
- owner_agent: product
- suspected_type: online_issue
- affected_surface: account-login / batch-login / code-source-client
- production_related: true
- initial_evidence_level: E0
- next_route: incident_report

## Incident / Triage

- bug_id: bug-2026-08-28-config2-empty-baseline
- level: L2
- severity: P1
- evidence_level: E4
- release_gate_required: true
- production_verification_required: true

生产 `50fc2d97` 已接受精确 config2 HTTPS URL 并创建 batch `#8`，但 item `529` 在 `code_baseline` 以 `url_error` 终止；attempt 未建 flow、未调用 Telegram。随后同一目标的人工 flow 成功发送 challenge，同一接码页出现非空 code/2FA。合成 UUID 只读复现证明该平台在 challenge 前统一返回 HTTP 200「此号不存在」错误页。第一断点是 provider lifecycle 与通用错误页 parser 合同冲突，不是 HTTPS、DNS、worker 或 Telegram 拒绝。

## Product Design Complete

- message_id: 2026-08-28-config2-empty-baseline-product
- intake_id: intake-2026-08-28-config2-empty-baseline-login
- from_agent: product
- to_agent: dev
- related_incident: batch 8 / item 529
- related_version: 50fc2d97
- level: L2
- design_status: complete
- evidence_level: E4
- next_agent: dev
- handoff_delivery_status: acknowledged

### 原始需求覆盖矩阵

| user_requirement | product_decision | functional_design | frontend_design | backend_design | dataflow_design | qa_acceptance | status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 支持新 HTTPS 地址 | 保留既有精确 config2 URL 白名单 | host/path/UUID 形状不变 | parser 已支持，无新增表单 | 不放宽 SSRF/redirect/port | precheck -> baseline | URL 正反例 | covered |
| 修复当前报错 | 修复 challenge 前空材料误判 | 空 baseline 不是成功 | 展示既有 phase/status | 仅 config2「此号不存在」返回空材料 | baseline -> create/send/wait | 空状态与未知错误分型 | covered |
| 查看日志 | 以 DB attempt + runtime + 脱敏在线页面为真相 | 分层报告 | 无 UI 改动 | failure_type 保持可见 | item/attempt/flow | 生产读回 | covered |
| 重新登录并通过测试 | 只对指定目标做一次受控 E4 | 禁止批量旧行重试 | 复用批量登录官方入口 | 新 generation/flow fence | item -> attempt -> flow -> authorization | Telegram authorized + online readback | covered |

### 功能与状态机

- happy_path: config2 精确 URL -> HTTPS 读到「此号不存在」-> 空 baseline HMAC -> 建号/绑定 -> send challenge -> 轮询得到非空 code/2FA -> verify -> 授权与在线读回。
- error_states: 频控 `url_fetch_failed`；其他错误标题/正文 `url_error`；缺字段 `url_parse_failed`；一直为空 `code_timeout`。
- permission_rules: 沿用 `accounts.batch_login + accounts.login`，完整凭据 reveal 仍需独立权限与原因。
- state_machine: 不新增状态；`LoginMaterials.code == ""` 只作为 config2 parser 的已知空材料，现有 wait 分支不验证空 code。

### 后端 / API / Worker

- affected_api: 无 schema 变化。
- affected_services: `account_login/identity.py`、`code_source_client.py`。
- affected_workers: account-login。
- data_models/migrations: 无。
- idempotency/concurrency: generation、lease、host rate bucket 和 remote call state 不变。
- failure_handling: host-scoped，禁止通用 fallback；未知页面显式失败。

### QA 与发布

- parser：同一错误页在 config2 下为空材料，在默认 host 下仍为 `url_error`。
- worker：空 baseline 后真实进入 send/wait，非空材料才 verify；一直为空按 timeout。
- regression：tgbotchecker、susubot、频控、SSRF、前端格式、批次 core。
- release_gate_required: true
- production_verification_required: true

### 深度自检

- uncovered_user_words: none
- missed_scenarios: 手工 flow 已过期，生产验收不得复用旧 code；必须新 generation。
- edge_cases: URL 有效但永不来码、旧 code、同码不同时间、2FA 缺失、challenge unknown。
- security_or_permission_risks: 空 baseline 不能暴露/持久 code 或允许任意 host。
- data_consistency_risks: 不得与现有人工账号/flow 并发；apply 前重读账号/flow。
- release_or_migration_risks: 无迁移；worker 行为变化需 Deploy Production 与 exact canary。
- rollback_considerations: 回滚代码后 config2 新行回到 baseline failure；已授权 session 不回滚。
- open_questions: none
- dev_handoff_ready: true

## Locked Paths

- `backend/app/services/account_login/identity.py`
- `backend/app/services/code_source_client.py`
- `backend/app/services/account_login/remote_phases.py`
- `backend/app/services/account_login/host_rate_policy.py`
- `backend/tests/test_account_batch_login_contract.py`
- `backend/tests/test_account_batch_login_core.py`
- `backend/tests/test_account_batch_login_config2.py`
- `docs/03-feature-designs/account-batch-auto-login-prd.md`
- `docs/00-index/project-dataflow-index.md`
- `docs/00-index/project-structure-index.md`
- `docs/05-implementation/multi-agent-practice/runs/2026-08-28-config2-empty-baseline-login.md`
- `docs/05-implementation/multi-agent-practice/agent-status-board.md`

## Development Complete

- implementation_status: local_implemented
- changed_behavior: `CodeSourceClient` 把已校验的 source host 传给 HTML parser；只有 config2 正文命中「此号不存在」才返回空 baseline。
- unchanged_boundaries: HTTPS/host/path/UUID、DNS/peer pinning、响应上限、频控、未知错误页、缺字段、generation/lease/remote-call fence 均不放宽。
- code_structure: 新增独立 config2 回归文件；生产代码文件小于 500 行，最大函数 26 行。
- index_update: 专项 PRD、数据流索引、项目结构索引已同步。
- development_result: passed

## QA Verification

- targeted_red: 2 failed / 1 passed，失败点为 parser 尚无 source-host 语义。
- targeted_green: 5 passed。
- regression: 76 passed in 4.67s，覆盖 contract/core/config2/runtime/parallelism/task-center contract，受 60 秒硬超时保护。
- static: `compileall`、`git diff --check`、文件/函数指标通过。
- online_readonly: 生产 pinned HTTPS transport 读取 challenge 前合成页面为 200「此号不存在」且无材料字段；同一真实来源在新 challenge 后为 200 且 code/2FA 字段非空。未记录材料值。
- local_network_note: 本机 Clash fake-IP 被既有 SSRF 门禁拒绝；未为测试放宽安全规则。
- qa_status: qa_pass
- evidence_level: E2

## Product Acceptance

- requirement_coverage: 新 config2 地址、HTTPS 报错根因、重新登录和真实验收口径全部覆盖。
- acceptance_scope: 接受 host-scoped parser 修复；不接受通用错误降级、旧 flow/code 复用或批量重试其他失败行。
- product_status: product_accepted
- release_status: release_gate_in_progress
- production_status: failed_at_baseline

## First Release and Production Attempts

- candidate_sha: `bfc4113fa47ed158a5500088034299332a522c74`
- deploy_run: `33094305138`
- release_result: CI 五个后端分片、前端构建、三个镜像与生产 deploy 全部通过；生产 current/backend/account-login 均读回该 SHA 且 healthy。
- generation_2: 原 baseline `url_error` 已越过；Telegram `send_call_state=confirmed`，随后 `wait_code` 以 `url_fetch_failed` 终止；未提交 code/2FA、无 session、无授权。
- generation_3: baseline 直接以 `url_fetch_failed` 终止，未发送 Telegram challenge。
- provider_evidence: pinned HTTPS 连续返回 200；频控页被 parser 明确识别为「接码平台请求频繁」。生产 challenge 证明 70 秒仍不足；成功读取后约 90 秒再次访问仍频控，约 130 秒静默后同一 transport 单次解析恢复且 code/2FA 字段存在，未记录字段值。
- root_cause_extension: host rate bucket 被硬编码为 tgbotchecker scope，生产间隔仅 3 秒；config2 baseline 后立即轮询，客户端 1 秒/3 秒重试进一步延长供应方频控。
- production_status: login_failed_rate_limited

## Rate Policy Remediation Development / QA

- implementation: host bucket scope 改为当前 item 的真实 `code_source_host`；config2 最小请求间隔固定为 `max(configured, 70)`，其他平台不变。
- failure_boundary: 频控页仍是显式 `url_fetch_failed`，未改为空材料或成功；TLS/SSRF/错误页合同不变。
- targeted_red: 新 host-policy 与 config2 full-flow 测试 2 failed / 1 passed。
- regression_green: 77 passed in 4.69s。
- release_status: second_release_gate_pending
- production_status: login_failed_rate_limited

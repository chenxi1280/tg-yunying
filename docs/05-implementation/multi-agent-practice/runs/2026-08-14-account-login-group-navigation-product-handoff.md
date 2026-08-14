# 2026-08-14 账号登录、分组语义与分组导航 Product Handoff

## Intake / Triage

- `intake_id`: `intake-2026-08-14-account-login-group-navigation-001`
- `raw_input`: 某账号登录验证码提交报错；同时登录时选择目标老号分组但结果仍落在其他分组语义里；账号分组在页面没有展示完整且不能横向滑动；当前 release 代码已合并但 PRD 没有收到代码变化影响，需要调整 PRD。
- `level`: `L2`
- `production_related`: `true`
- `route`: `product -> dev -> qa -> product -> prod-diagnosis`
- `design_status`: `product_design_complete`
- `implementation_started`: `false`
- `qa_pass`: `false`
- `production_fixed`: `false`

## Product Design Complete

- `truth_source`: `docs/03-feature-designs/account-login-group-navigation-recovery-prd.md`
- `supersedes_partial`: `docs/03-feature-designs/existing-account-reauthorization-routing-prd.md`
- `current_release_partial_facts`:
  - 同手机号已有未删除账号返回 `409 existing_account_requires_relogin`，不改原分组。
  - 账号中心已具备 20 条服务端分页和当前页可用性读取。
  - 重复 verify 已在线账号时具备幂等返回。
- `remaining_required_design`:
  - 主登录 / 备用登录必须以 `flow_id + flow_scope + flow_version + request_seq` 推进，禁止 latest-flow fallback。
  - 登录 intent 必须先持久化，再请求 Telegram challenge；临时 Session 由 flow owner permit 管理。
  - 授权、post-login sync、登录后分组迁移和安全动作必须拆成正交结果与 durable outbox。
  - 已有账号默认保留原分组；登录成功后迁移必须使用 `expected_from_pool_id` CAS。
  - 新账号目标分组失效或分组列表失败时禁止提交，不得 fallback 默认组。
  - 账号分组导航必须独立横向可达，选中分组失效时不得打开默认组。

## Development Handoff

| Area | Required implementation |
| --- | --- |
| Backend login flow | 设计/迁移 durable login flow v2、flow owner permit、exact flow verify/qr-check/resend/cancel、UID readback、typed errors |
| Post-login operations | 把资料同步、分组迁移、安全动作从 verify 请求拆到 durable outbox；verify 响应返回正交状态 |
| Group semantics | 已有账号 409 后 UI 显示保留原分组 / 登录后迁移；迁移接口增加来源 CAS 或专用 operation |
| Frontend account create | 禁止分组失效 fallback；提交前显示并复核目标分组 |
| Account group navigation | 替换 `Space wrap + Segmented` 裁切结构，提供横向可达 / 下拉降级 / 键盘语义 |
| Security | QR/code/2FA/session/phone_code_hash/原始异常不进入列表、日志或普通 detail |

## QA Acceptance

1. 验证码提交流程在 pending client 缺失、进程切换、过期、重发、2FA 错误、UID mismatch 时都返回类型化错误，不能返回 500。
2. 授权成功后同步失败，接口仍返回授权成功和同步失败状态；账号 session 可用。
3. 已有账号从新增入口重登时默认分组不变；显式迁移成功、no-op、source changed 和 target invalid 分态正确。
4. 新账号创建在分组列表失败、目标组删除 / 禁用 / 跨租户时不能提交或不能落默认组。
5. 账号分组数量超过一屏时首尾分组都可鼠标和键盘访问。
6. 账号中心仍保持 20 条服务端分页；分组切换不循环拉全量账号。

## Release Gate

- `release_mode`: `master -> release -> GitHub Actions Deploy Production`
- `migration_required`: likely yes for durable login flow v2; exact migration由 dev 设计后补。
- `mixed_version_fence`: required；旧实例不得处理 v2 durable flow，新实例开放前需隔离 open v1 flow。
- `production_e4_required`: true；必须取得真实 Telegram 登录、UID readback、无重复 AuthKey、post-login failure injection、分组迁移 CAS 和浏览器分组可达证据。

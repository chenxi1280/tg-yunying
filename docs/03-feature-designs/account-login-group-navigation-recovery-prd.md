# 账号登录、分组语义与分组导航修复 PRD

## 0. 状态与范围

| 项 | 值 |
| --- | --- |
| 日期 | 2026-08-14 |
| 版本 | v1.3 code-challenge-resync |
| 需求级别 | L2 / 账号登录主路径与账号中心可达性缺陷 |
| 设计状态 | `product_design_complete` |
| 当前实现状态 | `code_challenge_fix_local_implemented / qa_pass / release_pending` |
| 生产状态 | `unproven` |
| 关联问题 | 验证码提交只显示 Internal Server Error；平台窗口配置为 3 分钟而非 5 分钟；重复发送后旧 code 与新 challenge 串线；新增/重登分组语义与分组导航缺陷 |
| 非目标 | 本文不实现代码、不发布生产、不绕过 Telegram 官方登录安全限制 |

本文补齐当前 `release` 代码已发生但 PRD 未同步的合同，也明确剩余未实现缺口。2026-08-15 生产证据确认主账号验证码登录存在 180 秒本地窗口和同账号重复 `send_code_request`；后续 code 提交发生在本地截止时间之前仍被 Telegram 拒绝。本地修复已完成 300 秒平台窗口、显式 resend、flow 级临时 Session/hash 持久绑定、旧 flow fence、invalid/expired 分型和 2FA flow 恢复；未发布前生产状态仍为 `unproven`。`existing-account-reauthorization-routing-prd.md` 只保留“已有账号不重复创建、不静默改组”的窄合同；登录 flow 持久化、post-login 结果拆分和分组导航以本文为准。

## 1. 当前代码事实与缺口

### 1.1 已进入当前 release 的局部修复

- `POST /api/tg-accounts` 命中同租户未删除同手机号账号时返回结构化 `409 existing_account_requires_relogin`，不创建重复账号、不修改原账号分组。
- 账号列表在账号中心固定 20 条服务端分页，当前页头像懒加载；账号中心不再为了当前页表格循环拉全量账号。
- 重复提交同一登录验证码、且账号已经因前一次提交变为在线时，会返回当前在线账号，避免第二次命中进程内 pending client 缺失后误报失败。
- 登录初始化失败已写失败 flow、trace 和审计，并返回结构化错误；这只覆盖 start 阶段，不覆盖 verify 阶段和 post-login 同步阶段。

### 1.2 2026-08-15 修复前缺口与本地闭合结果

- **已在本地闭合：** code flow 不再依赖账号级 pending client。start 保存该 flow 的加密临时 StringSession 和 `phone_code_hash` 后断开；verify 按同一 flow 解密重建 client 并显式传入同一 hash。QR 内存态也已从 `account_id` 改为 `flow_id` 隔离，但跨实例 owner permit 仍属于后续韧性阶段。
- **已在本地闭合：** `flow_version` 已成为数据库列；resend 在锁内 supersede 旧 flow、递增旧版本并建立新 flow。旧 flow/version 在进入 Gateway 前返回 `login_flow_superseded`。
- **已在本地闭合：** 默认、生产示例和 compose 合同均为 `LOGIN_CODE_TTL_SECONDS=300`。该时间只命名为“平台提交截止时间”，Telegram 提前拒绝仍以类型化远端错误为准。
- **已在本地闭合：** 前端重发只调用独立 `POST /login/resend` 并携带旧 `flow_id + flow_version + request_seq`；普通 start 恢复现有 waiting-code / waiting-2FA / expired flow，不会静默再次发送 code。
- **已在本地闭合：** `PhoneCodeInvalidError` 保留当前 flow 可重试，`PhoneCodeExpiredError` 或平台超时进入 expired 并清除 challenge；进入 2FA 时只更新 flow 的临时 Session，不把未完成授权写为正式账号 Session。
- **本次未扩展：** owner permit / UID readback / durable outbox / 登录后分组迁移 CAS 仍是后续完整 v2 韧性目标，不得用本次 challenge 修复声明已经完成。
- `verify_login` 在主授权成功提交后同步执行资料 / 群 / 联系人等后置同步；后置同步异常仍可能让 HTTP 响应变成 500，用户只看到登录失败，但远端授权和数据库 session 可能已经成功。
- QR payload、开发模式 code preview 和原始异常 detail 仍可能进入 API 响应或详情展示；目标合同必须禁止生产主登录列表 / 日志暴露二维码原文、验证码、phone_code_hash、Session、2FA 或代理凭据。
- 新账号弹窗在 `selectedPoolId` 或分组列表缺失时仍会回退默认 / 首个分组；选中分组被删除、禁用或加载失败时，提交可能与用户看到的目标不一致。
- 当前 `selectedPool` 计算会在选中分组不存在时静默回退默认 / 首个分组，可能让“进入账号分组”打开错误分组。
- 分组导航仍是 `Space wrap + Segmented`，`.pool-filter-strip` 没有独立 `overflow-x:auto` / 滚动按钮 / tablist 键盘合同；长分组列表仍可能被裁切或不可达。
- 分组列表和账号当前页在同一个 `Promise.all` 里加载；任一失败会使整页失败，且不能表达 `pool_list_failed` 与 `account_page_failed` 的独立恢复。

## 2. 产品目标

1. 验证码 / QR / 2FA 登录的任一阶段失败都必须返回可行动的类型化错误，不再出现裸 `Internal Server Error`。
2. Telegram 远端授权成功、资料同步失败、登录后分组迁移失败必须正交展示；授权成功不能被后置失败回滚或误报。
3. 已有账号命中新建入口时默认保留原分组；如果运营想把账号移动到本次选择的分组，必须在登录成功后走显式迁移确认和 CAS。
4. 新账号创建必须严格使用提交时可验证的目标分组；分组加载失败、选择失效或目标组不可用时禁止提交，不能 fallback 到默认组。
5. 账号分组导航必须让全部分组可见、可选、可键盘访问，并且不破坏账号页 20 条服务端分页。
6. 验证码平台操作窗口固定为 5 分钟；继续登录或重新打开弹窗必须恢复现有可用 flow，只有显式“重新发送”才能请求新 challenge。
7. 每个 code challenge 必须与唯一 `flow_id` 的临时 Session 和 `phone_code_hash` 精确绑定；新 challenge 成功或结果不确定后，旧 flow 不得再进入 Telegram。

## 3. 登录 flow v2 合同

### 3.1 数据模型

新增或迁移为 `AccountLoginFlow` / `TgLoginFlowV2` 语义，字段至少包含：

| 字段 | 规则 |
| --- | --- |
| `flow_id` | 前端所有 start / verify / resend / cancel / qr-check 必须携带，不允许 latest fallback |
| `flow_scope` | `primary`、`standby_1`、`standby_2` 等授权槽位；唯一键和查询必须包含 scope |
| `method` | `code` 或 `qr`；切换方式必须 supersede 旧 flow |
| `flow_version` | 每次 resend、cancel、method switch 或 remote challenge 更新时递增 |
| `superseded_by_flow_id` | 新 challenge 替代旧 challenge 时记录新 flow；旧 flow 所有 verify 请求返回 `login_flow_superseded` |
| `owner_epoch` / `owner_lease_until` | 临时 Telegram Session 的唯一执行 owner；连接、RPC、序列化、断开都必须校验 owner permit |
| `developer_app_id` / `proxy_id` / `device_fingerprint_version` | challenge 到授权确认期间的配置快照，不允许中途漂移；当前主登录默认直连，`proxy_id` 不是主登录实际 egress 证明，备用授权才显式绑定并使用代理 |
| `authorization_status` | `intent_persisted / challenge_sent / waiting_code / waiting_qr / waiting_2fa / authorized / remote_unknown / failed / superseded / cancelled / expired` |
| `post_login_sync_status` | `not_started / queued / running / succeeded / failed / retryable` |
| `pool_transition_status` | `not_requested / queued / moved / no_op / failed / source_changed` |
| `security_post_login_status` | `not_requested / queued / succeeded / failed`；托管 2FA 和设备清理不得混在 verify 请求里 |
| `temporary_session_ciphertext` | code challenge 使用的未授权 Telethon StringSession，加密保存，只能由同 flow verify 解密使用 |
| `phone_code_hash_ciphertext` | Telegram 返回的 `phone_code_hash`，加密保存，只能与同 flow 的临时 Session 配对 |
| `remote_error_type` | 只保存 allowlist 异常类名，例如 `PhoneCodeInvalidError` / `PhoneCodeExpiredError`，不保存验证码或远端敏感载荷 |

验证码、phone_code_hash、QR token、临时 Session、2FA 输入、AuthKey 和代理凭据必须加密或只在内存短时存在；列表、详情、审计和错误 detail 只能返回 allowlist 类型化字段和 trace。

### 3.2 状态顺序

```text
persist intent
  -> claim flow owner permit
  -> Telegram start_login / qr_login
  -> persist challenge outcome
  -> user submits code / qr-check / 2FA with flow_id + version + request_seq
  -> claim same flow owner permit
  -> finish_login
  -> get_me authoritative UID readback
  -> persist primary or standby authorization
  -> enqueue post-login sync / security / optional pool transition outbox
  -> return orthogonal result projection
```

- 远端 challenge 发送前必须先持久化 intent；不能先调用 Telegram 后建 flow，否则进程崩溃会留下不可追踪的远端 challenge。
- 远端授权已发生但数据库提交失败时，flow 进入 `remote_unknown`，保留临时 Session，后续只能做 UID/readback reconcile，禁止直接重发验证码。
- 登录成功必须以同一临时 Session 执行 `get_me` 并核对稳定 Telegram UID。UID mismatch 时隔离候选 Session，不得激活。
- 同账号同 scope 的旧 owner 未断开或 Attempt 仍为 unknown 时，新 owner 不得连接同一临时 Session，避免 `AuthKeyDuplicated`。

### 3.3 验证码窗口与重发合同

- `LOGIN_CODE_TTL_SECONDS` 默认和生产合同固定为 `300` 秒。`code_expires_at = challenge_sent_at + 300 秒`，表示平台停止接受该 flow 新提交的时间；Telegram 提前拒绝时以远端类型化结果为准。
- initial start 必须先查同账号、同 scope、同 method 的可恢复 open flow；仍在 5 分钟窗口内时直接返回该 flow，不调用 Telegram。迁移前 `waiting-code` 且 `challenge_sent_at` 为空的旧 flow 没有 durable challenge binding，不得作为可恢复 flow 返回；用户点击“发送验证码”即 supersede 它并创建一个新 challenge。
- resend 使用独立 `POST /login/resend`，请求必须携带当前 `flow_id + flow_version + request_seq`。普通 `/login/start` 不承担 resend 语义。
- resend 在数据库事务内锁定当前 flow，校验仍为 open 后将其置为 `superseded`，再创建新的 intent；并发或迟到 resend 只能有一个获胜。
- 每个远端 challenge 使用新的 `flow_id`。`flow_version` 是该行的并发版本，不用相同 flow 覆盖历史 challenge；新 flow 从版本 1 开始。
- code start 成功后必须把临时 StringSession 与 `phone_code_hash` 加密落到该 flow 并断开临时 client；verify 重新建立同一临时 Session，并显式传入同一 hash。进程重启、负载均衡或重复打开弹窗不得改变配对。
- 新 challenge 远端调用失败或结果不确定时，旧 flow 保持 superseded；因为无法证明 Telegram 没有接受新请求，禁止回退继续提交旧 code。
- resend 成功后前端必须清空验证码输入，展示新 flow 的发送时间和平台截止时间；旧请求迟到只能返回 `login_flow_superseded`。

### 3.4 错误字典

| code | 展示 / 处置 |
| --- | --- |
| `login_flow_not_resumable` | 当前验证码流程无法跨进程继续，请重新发送验证码 |
| `login_flow_superseded` | 已有新的登录流程，当前输入已失效 |
| `login_code_invalid` | 当前 flow 的验证码错误；flow 仍在窗口内时允许重新输入，不自动重发 |
| `login_code_expired` | 当前 flow 已超过平台窗口或 Telegram 返回 `PhoneCodeExpiredError`；只能显式重发 |
| `login_rate_limited` | Telegram 返回 FloodWait；展示可重试时间，不自动重发 |
| `login_2fa_required` | 需要输入二步密码 |
| `login_2fa_invalid` | 二步密码错误；不得保存或轮换该输入 |
| `login_remote_unknown` | 远端授权状态不确定，等待系统核验，不得重发 |
| `login_identity_mismatch` | Telegram UID 与账号身份不一致，禁止激活 |
| `login_environment_changed` | 开发者应用、代理或设备指纹版本变化，需重新开始 |
| `post_login_sync_failed` | 授权已成功，资料 / 群 / 联系人同步失败，可单独重试 |
| `pool_transition_failed` | 授权已成功，目标分组迁移失败，可单独重试 |

## 4. 分组语义

### 4.1 已有账号

新增入口命中已有账号时，创建接口只返回结构化 409 和原 `account_id`。该响应不得发送验证码、修改原账号、移动分组或写任务范围。

前端必须明确展示：

- “该手机号已存在，将为原账号重新登录。”
- 原账号当前分组。
- 本次表单选择的分组只作为“登录成功后是否迁移”的候选，不会自动覆盖。

默认动作是 `preserve_current_pool`。如果用户选择“登录成功后移动到本次分组”，必须创建独立 `pool_transition_intent`，包含 `target_pool_id`、`expected_from_pool_id`、目标组名称快照和确认人。

### 4.2 分组迁移 CAS

登录成功后的分组迁移必须在授权提交之后独立执行：

```text
authorization_status=authorized
  -> durable pool_transition_outbox
  -> lock account + target pool
  -> assert account.pool_id == expected_from_pool_id
  -> sync pool_id + account_identity
  -> audit previous / target snapshot
```

- 当前分组已等于目标分组时返回 `no_op`。
- 账号在登录期间被其他管理员移动时返回 `pool_source_changed`，不得覆盖他人变更。
- 目标组被删除、禁用、跨租户或用途不允许时，迁移失败但授权仍成功。
- 迁移只影响未来动态选组和新任务候选；已冻结 Task scope、Action、Attempt 和远端事实不回写。

### 4.3 新账号

新账号创建必须在提交前和服务端事务内共同校验目标分组：

- 前端展示目标分组 ID / 名称快照；分组列表加载失败、目标组选项失效或目标组不可用时禁止提交。
- 服务端锁定目标 `AccountPool` 后校验租户、启用状态和用途，再原子写入 `pool_id + account_identity`。
- 前端不得在 `selectedPoolId` 缺失、目标分组不存在或列表失败时 fallback 到默认 / 首个分组。默认组只能在用户显式选择“默认账号分组”或后端收到空 `pool_id` 且分组列表事实正常时使用。

## 5. 账号分组导航

账号中心分组导航必须从普通 `Space wrap + Segmented` 升级为独立导航控件：

- 控件容器提供独立横向滚动，或者在窄屏切换为可完整展示的下拉；不得被卡片宽度裁切。
- 滚动区域有可见 affordance：滚动条、左右按钮或渐隐提示；普通竖向滚轮不被强行劫持为横向滚动。
- 使用 `role=tablist` / `tab` 或等价可访问语义；左右方向键移动分组，Home/End 到首尾，Enter/Space 选择。
- 分组项显示名称和账号数，长名称允许 tooltip 或多行展示，不允许不可读截断。
- `selectedPoolId` 在最新分组列表中不存在时，页面显示“选中分组已删除或不可用”，禁用“进入账号分组”，用户确认后回到全部账号；不得静默回退默认 / 首个分组。
- 分组列表 loading / error / empty 必须独立于账号列表 loading / error。`pool_list_failed` 时账号当前页可保留，但分组导航显示错误和重试，不把空数组解释为“没有分组”。
- 账号表格继续使用服务端 20 条分页、搜索和 `X-Total-Count`；分组切换只请求第 1 页，不循环拉全量账号。

## 6. API / 前端改动边界

| 层 | 要求 |
| --- | --- |
| API | 主登录 start / verify / qr-check / resend / cancel 均以 `flow_id` 为中心；resend 是携带旧 flow 身份的独立端点；verify 返回账号授权、post-login sync、pool transition 的正交投影 |
| 后端服务 | 登录后资料同步、授权安全动作和分组迁移全部进入 durable outbox；verify 请求内只完成授权提交和必要 UID readback |
| Gateway | code 登录不依赖进程内 pending client：使用 flow 持久化的临时 Session + hash；QR 临时 client 不再按 `account_id` 缓存，必须按 `flow_id + owner_epoch` 持有 |
| 前端 | 登录弹窗只接受当前 `flow_id + flow_version + request_seq` 的响应；关闭弹窗清空验证码和 2FA 输入 |
| 账号创建 | 目标分组选择失效时禁止提交；已有账号 409 显示保留原组 / 登录后迁移两种明确选择 |
| 样式 | 分组导航独立 CSS，不依赖父级 `Space wrap`；必须覆盖桌面、窄屏和大量分组 |

## 7. QA 与验收

### 7.1 自动化

- start 阶段：intent 已持久化后远端调用失败，flow 记录失败 trace；不把账号整体误降为不可恢复异常。
- verify 阶段：进程内 pending client 缺失返回 `login_flow_not_resumable`，不出现 500。
- post-login 同步失败：接口返回 `authorization_status=authorized` 且 `post_login_sync_status=failed`，账号 session 已持久化，可单独重试同步。
- 重发验证码：新 flow version supersede 旧输入；旧响应、旧 code 和多标签页迟到响应被拒绝。
- 5 分钟窗口：默认配置、API 截止时间和前端展示均为 300 秒；超过平台窗口不进入 Telegram。
- 重开弹窗：同一 open flow 被恢复，Gateway `send_code_request` 调用数不增加。
- challenge 配对：两个账号可并行、同账号先后两个 flow 不串 client/hash；verify 只接收本 flow 加密保存的临时 Session 与 hash。
- 错误分类：`PhoneCodeInvalidError` 保持当前 flow 可重试，`PhoneCodeExpiredError` 进入 expired，二者不再合并成同一数据库失败类型。
- QR 登录：列表 / 详情不暴露 raw QR payload；只在当前启动响应短时展示二维码。
- 2FA：用户为本次登录输入的密码不被隐式保存、托管或轮换。
- UID mismatch：不激活授权，临时 Session 隔离并写类型化错误。
- 已有账号 409：请求携带目标分组时原账号分组不变；选择迁移时必须在授权成功后按 expected_from_pool_id CAS。
- 新账号创建：分组列表失败、目标组删除 / 禁用 / 跨租户时不能 fallback 默认组。
- 分组导航：超过可视宽度的分组全部可达；选中失效分组不打开默认组。
- 账号中心：分组切换 / 搜索 / 翻页均只拉当前页；不使用全量账号循环支撑表格。

### 7.2 产品验收

- 用户看不到裸 `Internal Server Error`；每个失败都有可行动文案和 trace。
- 已有账号重登时页面明确说明“保留原分组”，或让用户显式确认“登录成功后迁移”。
- 分组导航在用户当前窗口宽度下能访问所有分组，含键盘操作。
- 登录成功、同步失败、迁移失败三者可以同时解释，不互相覆盖。

### 7.3 生产 E4

生产恢复只能在以下事实都满足后标记：

1. 至少一个真实可授权账号完成 code 或 QR 登录，远端 UID readback 与账号绑定一致。
2. 同一流程经历重发、迟到响应或跨实例路由时没有重复连接同一 AuthKey。
3. post-login 同步失败注入不再让 verify 接口返回 500。
4. 已有账号从新增入口重新登录后，默认原分组不变；显式迁移成功 / CAS 失败均有审计。
5. 账号中心分组数量超过一屏时，真实浏览器可访问首尾分组且账号分页仍为 20 条服务端页。
6. 真实 code 登录在不重发情况下于 5 分钟平台窗口内完成；重发 canary 明确证明旧 flow 被本地拒绝、新 flow 使用新 code 成功。

## 8. 发布与回滚

- 本设计涉及登录主路径，发布前必须进入维护 fence：阻止旧实例继续处理 v1 进程内 flow，等待或隔离 open flow，再放开新 flow。
- mixed-version 期间旧实例不得处理 v2 durable flow；回滚时 v2 flow 标记为 `maintenance_blocked`，用户需重新发起登录，不能交给旧进程内 client。
- 数据迁移必须保留旧 `TgLoginFlow` 只读审计；不得把历史 code_preview / qr_payload 迁成可继续提交的有效 flow。
- 任何发布成功、容器 healthy、SSH 可连都不等于登录业务恢复；必须按 §7.3 取证。

## 9. Product Design Complete 自检（v1.3）

- 原始问题：覆盖 3 分钟配置、重复 send、旧 code/new hash 串线、错误文案合并。
- 前端状态：覆盖首次、恢复、显式重发、迟到响应、输入清空、5 分钟展示和多标签页。
- 后端/API：覆盖独立 resend、flow/version CAS、旧 flow supersede、平台截止检查和类型化错误。
- Gateway/持久化：覆盖临时 Session/hash 加密、进程重启、同账号多 flow 与 QR 隔离边界。
- 并发/幂等：同账号同 scope 仅一个 open flow；并发 resend 仅一个获胜；重复 verify 已授权时幂等返回。
- 安全：API、列表、审计、日志不输出 code、hash、临时 Session、手机号或凭据。
- 发布/迁移/回滚：新增字段和 open-flow 唯一约束先迁移；mixed-version 必须 fence；回滚不尝试恢复 v2 challenge。
- QA/E4：自动化与真实 Telegram canary 分层；本地测试、CI、部署和 health 均不能替代真实 code 登录事实。

`design_status=product_design_complete`。进入开发后若上述字段、API 或 challenge 生命周期变化，必须标记 resync 并重新完成本节自检。

# 账号批量自动登号 PRD

> 日期口径：2026-08-15（Asia/Shanghai）
> 适用范围：TG 账号管理、账号分组（AccountPool）、登录 flow、后台 worker、审计。
> 定位：**当前**专项合同。与 [account-login-group-navigation-recovery-prd.md](account-login-group-navigation-recovery-prd.md)（单账号登录 flow 合同）、[existing-account-reauthorization-routing-prd.md](existing-account-reauthorization-routing-prd.md)（已有账号重登语义）、[account-standby-auto-authorization-prd.md](account-standby-auto-authorization-prd.md)（备用授权自动补齐）互补，不改变既有单账号登录语义。
> 设计状态：`product_design_complete`；实现状态：`local_implemented`；定向 QA：`passed`；发布状态：`not_released`；生产状态：`unproven`。本文仍不得把 PRD、POC、本地测试或分支合并当作批量能力已上线。

## 1. 背景与原始需求

运营侧持续新增 TG 账号。当前只能逐个打开登录弹窗、手动等待验证码、手动输入 2FA 密码，账号一多无法运维。运营使用的第三方接码平台（tgbotchecker.com）为每个号码提供一个只读页面，页面内实时展示该号码的 Telegram 登录验证码和 2FA 密码。

用户原始需求（原话归纳）：

- 账号列表提供「批量登录」按钮，点击弹窗。
- 弹窗内先选择账号要加入的分组（AccountPool），下方空白区域粘贴多行数据。
- 每行格式：`电话号码|接码链接`，示例：

  ```
  +12025550123|https://tgbotchecker.com/GetHTML?uuid=<32位uuid>
  ```

- 系统按行顺序依次完成登录：全部使用验证码登录；验证码和 2FA 密码都从链接页面获取；需要刷新链接直到出现最新验证码，再填 2FA 密码完成登录；登录完成后账号落入所选分组。

### 1.1 接码链接实测结论（2026-08-15 已验证）

对真实链接的实测（curl + Python POC 脚本，见附录 A）：

| 验证项 | 结论 |
| --- | --- |
| 请求方式 | `GET https://tgbotchecker.com/GetHTML?uuid={uuid}`，返回 `200` + HTML（Cloudflare，无登录态要求） |
| 验证码位置 | `<input id="code" class="form-input" type="text" value="[redacted]" readonly>`（`id="code"` 的 value） |
| 2FA 密码位置 | `<input id="pass2fa" class="form-input" type="text" value="[redacted]" readonly>`（`id="pass2fa"` 的 value） |
| 时间字段 | 「登录时间」（验证码产生时间）、「上次获取时间」，均为展示字段 |
| 无效 uuid | **HTTP 仍是 200**，标题变为「错误 - Telegram 登录接码工具」，正文含「此号不存在，请联系您的客服获取支持。」→ 判错必须解析页面内容，不能只看状态码 |
| 刷新语义 | 无新验证码时重复抓取值不变（间隔数秒至数分钟均验证）；`send_code` 触发 Telegram 下发新码后页面 value 更新 → 「刷新到最近验证码」的正确判据是 **code 值相对 baseline 发生变化**，不能依赖页面时间戳（实测页面时间与本地时钟存在偏差） |
| 解析容错风险 | 实测属性顺序调换（`type` 在 `id` 前）会使锚定 `id` 顺序的正则失配 → 生产解析器必须先定位含 `id="code"` 的 input 标签再提取 value（或用 HTML parser），且解析失败要显式报错 |

## 2. 目标

1. 账号列表页新增「批量登录」入口：弹窗内选择目标分组 + 粘贴多行 `号码|接码链接`，一次提交。
2. 系统创建登号批次，由 worker **按行顺序串行**执行：建号（或复用已存在账号）→ 发送验证码 → 轮询接码链接刷新出最新验证码 → 提交验证码 → 需要时提交链接内的 2FA 密码 → 登录完成，账号进入所选分组。
3. **跳过语义（本节为核心合同）**：确定失败或等待超时立即落 `failed` 并跳下一行；单行 300s 总预算耗尽为 `item_deadline_exceeded`，验证码 120s 窗口耗尽为 `code_timeout`。远程结果不可确定时落 `unresolved` 也继续下一行，不伪造 failed/succeeded；后台持续对账并以更正提醒收口。
4. 批次进度、每行结果、失败原因在批次详情中按行可见；失败项可一键重试；未执行项可取消。
5. **完成提醒**：操作员必须收到明确提醒，分别看到 failed、unresolved 和已授权但后置 warning 的行号/掩码手机号/原因。迟到权威结果改变未解状态时必须再发更正提醒，不允许静默结束或误报。
6. 任何失败都暴露明确原因（行级失败不阻塞后续行），不允许静默跳过、伪造成功或泛化错误。
7. 全流程审计；手机号与接码链接加密存储，验证码与 2FA 密码只在 worker 内存中短暂使用、永不落库，日志、追踪与审计全部脱敏。
8. Telegram 调用开始后结果未知时进入 `reconciling/unresolved`，禁止自动重发或重验；只有权威 flow/readback 收口，或操作员通过独立风险确认后才能开始新 generation。
9. 每行 UUID 必须持久绑定到最终 `account_id`：账号列表/详情显示独立「接码备注」以识别映射，账号改名或资料初始化不得覆盖；完整 UUID 加密保存并可由有权人员显式查看。

## 3. 非目标

- 不支持扫码（QR）方式批量登录。
- 不绕过 Telegram 官方验证码、2FA、FloodWait 限制；限流只做等待重试，不做规避。
- 不在本期做接码链接的有效性预连测（是否真实可用在执行时验证，见 7.4）。
- 不在登录成功后自动改密为平台托管 2FA（可作 P2，见 14）。
- 不自动清理登录设备、不做备用授权补齐（既有账号安全批次职责）。
- 不改变现有单账号登录弹窗与登录 flow 语义；本需求只新增批量编排层。

## 4. 方案选择

### 方案 A：前端循环调用现有单账号登录 API

运营粘贴多行后，前端逐行调 `login/start` + 轮询详情页拿验证码再 `login/verify`。

优点：后端几乎零改动。
缺点：浏览器关掉即中断；验证码轮询、2FA 提交逻辑被迫放在前端；无批次审计；违背「依次完成」的可靠性要求。**否决。**

### 方案 B：后端批次 + worker 串行执行（推荐）

复用既有批次样式，但不复制其 item-id 顺序 drain：使用批次/行项/执行尝试/提醒业务表，fingerprint alias 与持久限速辅助表，再加 precheck/confirm、公平的 phase drain、flow 归属和 CAS/lease。

优点：进度落库、断点续跑、失败可见、可重试可取消；与项目既有批量基础设施一致；敏感凭据不出后端。
缺点：新增四张业务表与两张辅助表、service/drain/reconciler/全局提醒，改动面中等。

### 方案 C：独立脚本/CLI 跑批

不进平台，用运维脚本直接跑。

优点：最快。缺点：无权限、无审计、无 UI 进度，违背平台化运营方向。**否决。**

**结论：采用方案 B。**

## 5. 用户角色与权限

| 角色 | 能力 |
| --- | --- |
| 平台管理员 / 账号添加专员 | 创建批量登号批次、查看进度、刷新凭据、重试失败/未解项、取消批次 |
| 运营主管 | 查看批次进度与失败原因 |
| 只读观察员 | 不可见批量登录入口 |

权限点：

| 权限点 | 控制范围 |
| --- | --- |
| `accounts.batch_login`（新增） | 批量登录按钮、批次创建/凭据刷新/重试/取消 API |
| `accounts.view` | 查看账号列表与批次列表 |
| `accounts.login`（既有） | 与单账号登录一致的底层登录能力 |
| `accounts.code_source_credentials.read`（新增） | 在账号详情显式查看完整接码 UUID；每次查看必须填写原因并审计 |

写操作（precheck/create/retry/refresh-credential/cancel）同时要求 `accounts.batch_login + accounts.login`；列表/详情/提醒要求 `accounts.view`；完整 UUID 查看同时要求 `accounts.view + accounts.code_source_credentials.read`。所有 batch/item/pool/account 读写必须资源租户校验，worker 也要复核同租户引用。前端隐藏不代替后端授权；操作、完整值查看与系统代执行都写 requested_by/system actor 审计。

## 6. 核心概念

| 概念 | 定义 |
| --- | --- |
| 登号批次 `TgAccountLoginBatch` | 一次「批量登录」提交对应的批次，绑定目标分组与操作原因 |
| 批次行项 `TgAccountLoginBatchItem` | 批次中的一行 `号码|接码链接`，独立状态机 |
| 接码链接 code URL | 第三方接码平台只读页面 URL，含该号码最新验证码与 2FA 密码，密文存储 |
| 手机号 fingerprint | 规范化 E.164 手机号的租户级 keyed HMAC；仅用于精确匹配/去重，不能反解；`phone_masked` 只用于展示 |
| baseline fingerprint | `send_code` 前对旧验证码与页面登录时间原文分别计算的 keyed HMAC；不保存验证码或时间原文 |
| 接码备注 | 由账号当前 code-source binding 派生的 `平台 + UUID 脱敏提示`（如 `tgbotchecker · a1b2c3…9f0e`）；独立于会被资料初始化修改的 `display_name` |
| 刷新窗口 | `send_code` 后轮询接码链接等待 code 值变化的时限（默认 120s） |
| flow 归属 | 每行每个 `execution_generation` 只能拥有一个精确 `login_flow_id + flow_version`；不能接管人工或其他批次 flow |
| 新号 / 已有候选 / 重登 / 已授权 | precheck 只分 `create/existing_probe_required`；worker 用主 session 新鲜权威探测后才变为 `relogin/already_authorized`。不用 ACTIVE、session 存在或历史 flow 代替 `is_user_authorized` |
| 未解结果 | 单行预算内叫 `reconciling`；预算耗尽后以 `unresolved` 让批次继续，独立 reconciler 可更正其结果和提醒 |

## 7. 页面需求（前端）

### 7.1 账号列表页入口

- `AccountsView` 顶部操作区新增「批量登录」按钮（权限 `accounts.batch_login`），与现有「新增账号」「安全批次」并列。
- 按钮点击打开 `accountBatchLogin` Modal（新增，antd `Modal`，`className="tg-modal"`，宽 640，模式对齐 `AppModals.tsx` 既有弹窗）。

### 7.2 批量登录弹窗（对应用户描述的交互）

弹窗内容自上而下：

1. **选择加入的分组**：`Select`，数据源 `accountPools`（仅 `is_enabled` 且通过 admission 校验的池），默认选中当前列表 Tab 的分组；必填。
2. **多行数据输入**：`Input.TextArea rows=12`，placeholder：

   ```
   +12025550123|https://tgbotchecker.com/GetHTML?uuid=<32位uuid>
   +1415xxxxxxx|https://tgbotchecker.com/GetHTML?uuid=...
   ```

   - 前端本地显示「共 N 行 / 格式有效 M 行 / 手机号重复 K 行 / UUID 重复 U 行」；同一 UUID 不能对应批内多个号码。后端预检只给出 `create/existing_probe_required`，不在未访问 Telegram 时伪造 relogin/already-authorized。
   - 行格式：`phone|url`，竖线分隔，允许行内出现成对反引号包裹 URL（用户粘贴示例含 Markdown 反引号，解析时剥离）；空行忽略；单批次上限 **100 行**（对齐 `BATCH_SELECTION_LIMIT`）。
   - phone 规范：`+` 开头 E.164；url 必须为 `https://` 且主机在接码平台白名单（默认 `tgbotchecker.com`，后端配置项，见 8.4）。
3. **操作原因**：`Input.TextArea rows=2 maxLength=255 showCount`，必填。所选分组是每个成功/已授权行的目标终态，不再提供默认不迁移的歧义选项。
4. 操作按钮：「取消」「预检并确认」。预检展示 create/existing 候选数、将迁移的既有账号清单、UUID 接码备注预览、当前排队位置、估算/最坏完成时间与凭据失效时间；100 行全部耗尽 300s 时最坏约 8h20m（另加排队/行间隔），必须显示而非隐藏。既有账号已绑定不同 UUID 时逐行显示旧/新脱敏提示，必须显式勾选「替换接码绑定」并提交旧 binding version，不能静默覆盖。

确认请求必须重传原始输入和分组选项，并携带后端返回的 `preview_token + preview_fingerprint` 与前端生成的 `idempotency_key`；提交成功后关闭弹窗、打开批次详情 Drawer，并提示「批次已创建，正在按行执行」。

### 7.3 批次详情 Drawer

复用 `AccountSecurityBatchDrawer` 的 Drawer + 状态映射模式：

- 顶部：`queued/running/completed/completed_with_unresolved/cancelled`、目标分组、总数/成功/失败/未解/警告/跳过、排队位置和预计时间。
- 行项表格：行号、掩码手机号、路由候选/实际路由、generation、phase、status、失败/未解/警告原因、重试次数与时间。`reconciling/unresolved` 分别显示「确认中/需持续对账」，不伪装成失败。
- 详情打开期间前端每 5s 轮询刷新（对齐项目既有轮询模式）；单行失败/超时被跳过时表格即时可见该行 `failed` 且下一行进入执行（跳过语义见 8.2）。
- 操作：批次「取消」；failed/unresolved 行「重试」；凭据过期行先「更新接码链接」。unresolved 重试必须弹出独立重复调用风险确认。
- 「查看账号」链接：行项已有 account_id 时跳账号详情。
- 账号列表与详情增加只读「接码备注」：默认只显示 host 与 UUID 脱敏提示；有权用户可点「查看完整 UUID」，必须填写原因并产生审计。普通列表、批次提醒和导出不返回完整值。
- **批次完成提醒**：全局应用壳轮询持久提醒事件（不限账号页/Drawer 是否打开）；Drawer 只负责详情展示：
  - Alert 显示「成功 X / 失败 Y / 未解 U / 警告 W / 跳过 Z」，失败与未解分列；未解不进入“已确定失败”清单。
  - 平台按 `batch_id + execution_generation + resolution_version` 消费提醒；迟到对账改变结果后产生 correction 事件，正文显示哪些行从 unresolved 改为 succeeded/failed。
  - 用户离线后再次登录仍能读取未确认提醒；用户确认后写 `acknowledged_at`，不能依赖瞬时状态变化。

### 7.4 前端不做的事

- 不在前端请求接码链接（SSRF/凭据泄露风险，全部走后端）。
- 不在前端拼装或保存验证码/2FA 密码；完整 UUID reveal 只在当前详情临时展示，禁止写 localStorage、埋点、错误上报或前端日志。

## 8. 后端设计

### 8.1 数据模型（新增 migration）

`TgAccountLoginBatch`（表 `tg_account_login_batches`）：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id / tenant_id / created_by | — | 常规 |
| pool_id | FK account_pools | 目标分组 |
| idempotency_key / request_fingerprint | str | `UNIQUE(tenant_id, created_by, idempotency_key)`；同 key 异 payload 拒绝 |
| status / state_version | — | `queued / running / cancelling / completed / completed_with_unresolved / cancelled` + CAS |
| execution_generation / resolution_version | int | 重试代次；未解结果每次权威更正递增 resolution |
| total/success/failed/unresolved/warning/skipped_count | int | 汇总；warning 同时计入 success，unresolved 绝不计 failed |
| last_claimed_at | ts | 跨租户/批次公平轮转游标 |
| reason | str | 操作原因（审计冗余） |
| trace_id | str | 全链路追踪 |
| started_at / finished_at / created_at | ts | — |

`TgAccountLoginBatchItem`（表 `tg_account_login_batch_items`）：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id / batch_id | — | `UNIQUE(batch_id, line_no)` |
| line_no | int | 行号（执行顺序） |
| phone_masked | str | 仅展示，不参与身份判断 |
| phone_fingerprint / phone_fingerprint_version | str/int | 规范化 E.164 的租户级 keyed HMAC 与密钥版本，用于匹配/去重 |
| phone_ciphertext | str | 加密手机号（建号用） |
| code_url_ciphertext / expires_at | — | 加密接码链接及保留截止；到期或确认无需重试后清除 |
| code_source_uuid_fingerprint / uuid_hint | str | 规范化 `host:uuid` 的稳定 SHA-256 与脱敏提示；UUID 为 128-bit 随机值，fingerprint 仅作相等判断，不返回 API |
| route_hint / route | enum | precheck: `create/existing_probe_required`；执行: `create/relogin/already_authorized` |
| account_id | FK tg_accounts, nullable | 建号/匹配后回填 |
| status | enum | `pending / running / waiting / reconciling / unresolved / succeeded / succeeded_with_warning / failed / skipped` |
| phase | enum | 见 8.2 |
| failure_type / failure_detail | str | 稳定错误码 + 人读详情 |
| current_attempt_id / execution_generation | — | 指向当前执行尝试的投影；旧尝试不覆盖 |
| state_version | int | 行投影 CAS；旧 worker 不得迟到回写 |
| started_at / finished_at / created_at / updated_at | ts | — |

`TgAccountLoginBatchAttempt`（表 `tg_account_login_batch_attempts`）按 `UNIQUE(item_id, execution_generation)` 保留每代 `state_version`、phase/lease/deadline、精确 flow，baseline HMAC，以及 send/code-verify/2FA-verify 各自的 `flow_version + request_seq + request_key + call_state(none/started/confirmed/unknown)`。远程未知时保存 `reconcile_status/reconcile_until_at/last_reconciled_at/authoritative_evidence_ref`；本地 request key 唯一不代表远程未执行。attempt 收口后清 baseline HMAC。

`TgAccountLoginBatchNotification` 按 `UNIQUE(batch_id, execution_generation, resolution_version, channel, recipient_user_id)` 保存接收人、initial/correction 类型、脱敏摘要和 platform/tg_bot 发送/ack/重试状态。

`TgAccountPhoneFingerprintAlias` 以 `UNIQUE(tenant_id, key_version, fingerprint)` 绑定 account；转 key 期对所有 accepted-version fingerprint 按固定顺序加 advisory lock，先双版本查 alias，新建时同事务插入所有 alias。不再依赖「带 version 的单一 unique 键」阻止跨版本重复。

`TgAccountLoginRateBucket` 以 `scope_type(host/developer_app) + scope_id` 保存跨 worker 的下次可用时间、并发租约与 CAS 版本；发布必须显式配置 host 全局并发/最小间隔和 developer-app 登录并发槽，缺失则 readiness fail closed。

`TgAccount` 增加 `code_source_host / code_source_uuid_ciphertext / code_source_uuid_fingerprint / code_source_uuid_hint / code_source_binding_status / code_source_binding_version / code_source_bound_at / code_source_bound_by`。完整 UUID 独立加密持久化；列表/详情的 `code_source_note` 由 host+hint 派生，不把 UUID 写进 `display_name`。同租户 `host + uuid_fingerprint` 只能绑定一个未删除账号；账号改名、资料初始化和批次 URL 到期清除不删除该绑定。

其他约束：批内 line/phone fingerprint/UUID fingerprint 唯一；软删除 phone alias 或 UUID binding 命中均显式冲突。新号 `display_name=待初始化账号-{phone_masked}`；正常建号只在接码 baseline 验证后发生。

### 8.2 行项执行状态机（worker 串行）

单批内按 `line_no` 严格串行；上一行到达 succeeded/failed/unresolved/skipped 后才进下一行。worker 每次只执行一个可恢复 phase，轮询/FloodWait 写 `next_retry_at` 后释放槽位，使其他批次可推进。

**跳过/对账合同**：确定失败立即 failed 并推进；远端 started 后结果未知在 300s 单行预算内只读 reconcile，到期改为 `unresolved` 并推进，不改写为确定 failed。独立 reconciler 在默认 24h 自动窗口内继续读精确 flow/session；权威成功/未执行证据更正 succeeded/failed，到期仍不明保留 `unresolved(manual_review_required)`。

```
pending
  → prepare                 全版本 alias 匹配；不建号、不信任 ACTIVE
  → authorization_probe     已有账号用主 session/direct 新鲜 is_user_authorized；true→already_authorized；
                              false→relogin；探测异常→failed(authorization_probe_failed)
  → code_baseline           create/relogin 先 GET 接码页，只存 code/login-time keyed HMAC
  → bind_account            create 在 baseline 后锁定/重查全部 alias；无命中才同事务建号+绑 item+aliases；
                              若并发新命中则绑定既有 account 并回到 authorization_probe，不重复建号
  → bind_code_source        以 binding version CAS 将输入 UUID 绑定最终 account；create/relogin 的 baseline
                              通过后为 verified_readable；already_authorized 未抓页面时为 provided_unverified，
                              但相同 UUID 的既有 verified_readable 不降级；already_authorized 随后直达 pool_transition
  → acquire_flow            创建 batch-owned intent，绑精确 flow/version，拒绝其他 owner
  → send_code               取 developer-app 持久限速槽；started→Telegram→confirmed
  → wait_code               每 3s 一个 phase；120s 未变→failed(code_timeout)；单行到期→failed(item_deadline_exceeded)
  → submit_code / submit_2fa 按同 flow/version/request_seq verify；2FA `policy=do_not_store`
  → pool_transition         create/relogin/already_authorized 统一将目标池 CAS 为用户所选分组；失败为
                              failed(pool_transition_failed)，但 authorization 投影保持已授权
  → succeeded / succeeded_with_warning（已在目标分组；仅资料同步/online readback 失败记 warning）
  → reconciling → unresolved（远端未解，让出批内顺序，等独立 reconciler/correction）
```

phase 以 `(attempt_id, generation, state_version, lease_token)` CAS，网络调用期间不持 DB 连接/锁/事务。`deadline_at` 在该行首次外部 phase claim 时生成，排队时间不偷走行预算；重启不重置。结果、行投影与计数同事务从行状态重算。

失败枚举：`line_format_invalid / url_domain_not_allowed / url_ssrf_rejected / url_error / url_fetch_failed / url_parse_failed / url_missing_2fa / credential_expired / code_source_binding_conflict / authorization_probe_failed / code_timeout / item_deadline_exceeded / login_flow_conflict / login_code_invalid / login_code_expired / twofa_invalid / login_rate_limited / quota_exceeded / developer_app_unavailable / pool_admission_rejected / pool_transition_failed / account_create_failed / login_remote_not_completed / retry_limit_exceeded / manual_interrupted`。`manual_review_required` 是 unresolved 原因，不是 failed；Telegram 异常仍复用既有类型化映射。

### 8.3 重试与取消

- **行重试**：failed/unresolved 可新建 generation，保留旧 attempt，只可 supersede 本 item 拥有的 flow。unresolved 必须先完成当前对账尝试，并携带 `confirm_remote_unknown=true + expected_attempt_id + expected_attempt_version + expected_resolution_version`，否则 409；单行最多 3 个人工重试 generation。
- **凭据刷新**：URL 过期行用专用 API 提交新链接、reason、expected item version 和 expected binding version；只更新 item 待验证密文/TTL，不自动发码或重试。新 URL 在后续 baseline 成功后才替换账号当前 binding；already_authorized 路径需再次显式确认后写 `provided_unverified`。
- **FloodWait**：不消耗人工重试次数；只有 `now + seconds < deadline_at` 才 waiting，否则 `failed(login_rate_limited)`。
- **批次取消**：CAS 置 `cancelling`；未领取行 skipped。已 started 的行不强杀；权威已授权则 succeeded/warning，可证未执行则 skipped，仍不明则 unresolved，然后批次 cancelled 但 reconciler/correction 继续。
- **同账号互斥**：账号锁只用于短事务检查/绑定 flow，不跨网络调用；同一 accepted-version phone alias/account_id 的人工或其他批次 flow 不得被本批次复用或覆盖。

### 8.4 外部接码源客户端（新增 `services/code_source_client.py`）

- `fetch_login_materials(url) -> {code, password_2fa, login_time, last_fetch_time}`。
- 实现要求（源自附录 A 实测）：
  - 固定 UA（如 `tg-yunying-login-worker/1.0`）；单次超时 15s；网络失败重试 2 次（退避 1s/3s）；重试耗尽 → `url_fetch_failed`。
  - **判错看内容**：标题含「错误」或正文含「此号不存在」或缺失 `id="code"` 输入 → `url_error`（HTTP 200 也可能是错误页）。
  - **解析容错**：先按标签定位含 `id="code"` / `id="pass2fa"` 的 `<input>`，再取其 `value`（不得依赖属性顺序）；解析失败 → `url_parse_failed`（可见失败，禁止回退为空值继续）。
  - 绝对时间不与本机时钟比较；仅对页面 `login_time` 原文做 HMAC 并与 baseline 比较，用于覆盖“新验证码恰好与旧码相同”的极小概率。
  - **SSRF 防护**：仅接受无 userinfo/fragment 的 `https://tgbotchecker.com:443/GetHTML?uuid={32位hex}`，query 只能有一个 uuid；禁重定向；响应解压后上限 256 KiB、只接受 `text/html`。DNS 解析后拒绝私网、环回、链路本地、保留及 `198.18.0.0/15` fake-IP；连接必须 pin 已验证公网 IP，并在连接后校验 peer IP，TLS SNI/证书仍使用原 host，防 DNS rebinding。生产 readiness 若只能得到 fake-IP/非公网地址必须 fail closed，不能把 fake-IP 网段加入白名单。
- 轮询默认间隔 3s、窗口 120s；请求前必须取得 host 持久限速槽，同 uuid 另做去重。限速不得只存进程内，无槽时不得绕过。

### 8.5 API 设计（批次新增 router `account_login_batches.py`；账号 UUID reveal 仍挂 accounts router）

| 端点 | 方法 | 请求 | 响应 |
| --- | --- | --- | --- |
| `/api/tg-accounts/login-batches/capability` | GET | — | `mode/limits/readiness`；仅供 UI 展示，不代替写 API 门禁 |
| `/api/tg-accounts/login-batches/precheck` | POST | `{pool_id, lines_text}` | preview + create/existing 候选、UUID 备注/冲突、迁池清单、排队/ETA |
| `/api/tg-accounts/login-batches` | POST | `{pool_id, lines_text, binding_decisions[], preview_token, preview_fingerprint, idempotency_key, reason}` | `LoginBatchOut`；替换项含 expected binding version |
| `/api/tg-accounts/login-batches` | GET | 分页 | `list[LoginBatchOut]` |
| `/api/tg-accounts/login-batches/{id}` | GET | `?item_limit&item_offset` | `LoginBatchDetailOut`（含 items） |
| `/api/tg-accounts/login-batches/{id}/retry` | POST | `{item_ids?, expected_state_version, expected_attempt_id?, expected_attempt_version?, expected_resolution_version?, confirm_remote_unknown=false, reason}` | failed 可直接重试；unresolved 必须显式确认且三个 expected 值必填 |
| `/api/tg-accounts/login-batches/{id}/items/{item_id}/refresh-credential` | POST | `{code_url, expected_item_version, expected_binding_version?, replace_binding=false, reason}` | 只更新待验证密文/TTL，不调 Telegram |
| `/api/tg-accounts/login-batches/{id}/cancel` | POST | `{expected_state_version, reason}` | `LoginBatchOut`；同一旧 version 重放不产生第二次副作用 |
| `/api/tg-accounts/login-batch-notifications` | GET | `?unacknowledged=true` | 仅 `recipient_user_id=current_user` 且同租户的持久提醒 |
| `/api/tg-accounts/login-batch-notifications/{id}/ack` | POST | `{expected_version}` | 同租户+接收人校验后 CAS 写 `acknowledged_at` |
| `/api/tg-accounts/{account_id}/code-source-binding/reveal` | POST | `{reason, expected_binding_version}` | 有权且同租户时返回完整 UUID；`Cache-Control: no-store` 并审计 |

`TgAccountOut/TgAccountDetailOut` 只增加 `code_source_note/code_source_binding_status/code_source_binding_version`，不返回 ciphertext、fingerprint 或完整 UUID；reveal 是唯一明文出口。

预检不访问接码站或 Telegram；它校验输入/exact URL/分组/配额，用 phone alias 只分出 create/existing 候选，并检查 UUID binding 重复/替换，计算接码备注、迁池清单、排队与最坏耗时。`preview_token` 默认 5 分钟，只含 actor/tenant/过期、规范输入 HMAC 和 phone/binding 状态版本摘要；确认重传原输入并全量比对。同 actor 幂等重放只在 request fingerprint 一致时返原批次。

`ACCOUNT_BATCH_LOGIN_MODE` 只接受 `off/reconcile_only/enabled`：off/reconcile_only 下 precheck/create/retry/refresh-credential 返回 503 `account_batch_login_disabled`；cancel、notification ack 和只读语义的 UUID reveal 仍可写审计，列表/详情/提醒仍可读。off 不启动 account-login/reconciler 角色，且只能在 unresolved=0、远端 started=0、outbox 待投递=0 后使用；reconcile_only 只运行 remote reconciler 和 notification outbox，不 claim 新 phase；enabled 才允许创建与 claim。mode 在 health/capability 显式返回，无效/缺失配置启动失败。

审计：预检确认、创建、取消、重试、unknown/reconcile、每行终态与提醒 dead-letter 均写 actor/reason/trace；detail 只含批次/行 ID、generation、掩码手机号、failure_type，不落完整 URL、验证码、2FA 或请求体。

### 8.6 Worker 挂载

- `drain_account_login_batches` 以 tenant `last_served_at`、batch `last_claimed_at`、line/phase 排序，每轮每批最多领一个 phase；不按 item id 把大批次排空。每个短事务独立 session，网络期间无 DB 连接/锁/事务。
- 已有账号权威探测使用主 session 和主登录 direct 路径；探测网络异常不得降级成 relogin。create 只在 baseline 通过后执行，并将 account/item/aliases 同事务绑定。
- 独立 `drain_account_login_reconciliation` 只处理 started/unknown/unresolved，权威收口时 CAS 更新行/批次计数/resolution version 并写 correction outbox。
- `worker.py` 与 `worker_health.py` 同步新增 `account-login`；本地 `legacy/all` 可包含，生产必须以独立 `--role account-login` 容器运行。Compose、发布脚本和 post-deploy health 必须同时检查该角色心跳。
- readiness 同时检查 mode、DNS/HTTPS、密钥、alias 回填和 rate-bucket 配置；任一缺失 fail closed。

### 8.7 批次完成提醒

所有行进入 sequence-terminal（包含 unresolved）时，同事务写 `completed/completed_with_unresolved`、重算六类计数，并为当前 generation/resolution 插入 initial platform/tg_bot 事件。unresolved 后续被权威修正，或 24 小时后升级为 `manual_review_required` 时，同事务递增 `resolution_version`、重算并插入 correction 事件：

1. **平台提醒（必达事实）**：离线重登可见，ack 后才消失；initial/correction 分开 ack，正文区分失败、未解和警告清单。
2. **TG Bot（可重试通道）**：独立 drain 按 outbox 状态指数退避发送，成功写 sent；达到有界次数写 dead_letter + 审计并在平台提醒中暴露“TG 提醒失败”，不回滚批次事实。
3. 提醒内容与审计同口径脱敏；不包含完整 URL/uuid/code/2FA/未掩码手机号。

## 9. 数据流转

```
前端 Modal(目标分组+多行)
  → POST precheck → create/existing 候选 + 迁池清单 + 排队/ETA + preview fingerprint
  → 用户确认(reason + idempotency_key) → CAS 建批次/行项/当前状态快照
  → fair phase drain（批内仍串行）：
      prepare: alias 匹配；已有账号做新鲜权威探测决定 already_authorized/relogin
      baseline: code_source_client → 内存 raw + DB keyed HMAC；通过后 create 才建号+绑 alias/item
      acquire/send: 绑定 item-owned flow；远端 started→confirmed，unknown→reconcile
      wait/verify: HMAC 变化后以内存 code/2FA 推进同一 flow，policy=do_not_store
      post-login: 授权/同步/目标池/online readback 正交回写
  → failed 或 unresolved 都让出批内顺序
  → initial 完成事件；独立 reconciler 收口 unresolved 后产生 correction 事件
  → 平台必达 + TG Bot outbox；failed/unresolved 显式 retry，过期 URL 先 refresh
```

不新增账号全局状态枚举；优先在批次行保存 flow 归属字段，只有现有 `TgLoginFlow` 无法表达 owner/generation fence 时才以 migration 最小扩展。本文已同步产品/数据流合同；`project-structure-index.md` 只记录已实现代码，dev 完成后再更新，禁止提前伪造当前入口。

## 10. 权限与安全（自检清单）

1. **接码链接与 UUID 是高敏凭据**：item 的完整 URL 密文只保留至成功/取消或默认 24h 重试窗口；unresolved 也不超过该窗口。最终账号的 UUID 映射另以 `code_source_uuid_ciphertext` 加密保留，直至显式替换或账号硬删除；标准 API 只返 host+hint，只有 reveal API 可返回原值。
2. **验证码与 2FA 用后即弃**：只存在于单次函数局部内存，不落库/日志/trace/error；baseline 只存 keyed HMAC，终态清除。批量 verify 必须显式 `do_not_store`，禁止进入托管 2FA 快照或改密路径。
3. SSRF 白名单与私网拒绝（8.4）。
4. 手机号加密/掩码；版本化 fingerprint 必须通过 alias + accepted-version 固定锁序防跨版本重复建号，不得只依赖含 version 的 unique key。
5. 写接口必须同时具备 `accounts.batch_login` 与 `accounts.login`，读接口必须具备 `accounts.view`；所有 batch/item/notification/binding 资源再次校验 `tenant_id`。`accounts.batch_login` 与 `accounts.code_source_credentials.read` 默认仅平台管理员和账号添加专员角色绑定。
6. 创建确认要求本次 reason、actor 与幂等键；重试、接码地址刷新、取消和远端未知确认均要求本次 reason、actor 与 expected version，重放旧 version 不产生第二次副作用。禁止复用旧 reason 代替新操作审计。
7. 不允许 mock success：`tg_gateway_mode=mock` 下批次创建直接拒绝（提示仅生产真实模式可用），防止假成功。
8. API/middleware/HTTP client 必须对请求体、query、异常对象和 access log 做字段级脱敏；任何错误不得拼接完整 URL、UUID 或第三方 HTML。UUID reveal 响应 `no-store`，不进入通用响应日志、APM body capture 或导出。

## 11. 边界与失败路径（自检）

| 场景 | 合同行为 |
| --- | --- |
| 行格式、精确 URL 形状、域名或批内 `phone_fingerprint` 非法 | precheck 整批 400，返回行号与类型化错误；不抓取 URL |
| 同一 UUID 出现在批内多个号码，或已绑定同租户其他账号 | precheck 409 `code_source_binding_conflict`，只返回冲突行和脱敏提示，不暴露另一账号完整 UUID |
| 确认时分组/配额/账号状态相对 preview 漂移 | 409 `preview_stale`，重做 precheck；不创建部分批次 |
| 同一 `idempotency_key` 重复确认 | 返回原 batch；`request_fingerprint` 不同则 409 |
| 数据库显示 ACTIVE/有 session | 只记为 `existing_probe_required`；worker 必须用主 session 做新鲜、直连、权威 `is_user_authorized` 探测，不据此判在线 |
| 权威探测成功/未授权/报错 | 已授权写 `already_authorized`、不发 code 并迁入目标分组；未授权进入 relogin；探测报错写 `authorization_probe_failed` 并跳行，不猜测重登 |
| 已有可续传且未被其他 owner 占用的 flow | 精确绑定后续传；人工/其他批次 flow 冲突则 `waiting(login_flow_conflict)` |
| 新号码的接码错误页/解析失败/网络耗尽 | 先完成接码 baseline；失败写 `url_error/url_parse_failed/url_fetch_failed` 并跳行，不创建孤儿账号 |
| DNS 或 peer 落入私网、保留网段或 fake-IP | `url_ssrf_rejected`；不请求、不 fallback |
| code HMAC 不变但 login-time HMAC 变化 | 视为新 challenge，只在当前 flow 尝试一次 |
| 单行 deadline / code 等待 deadline | `item_deadline_exceeded` / `code_timeout`，跳下一行；不将任意卡死都写成 code timeout |
| FloodWait 未超行 deadline | `waiting + next_retry_at`；超出则 `login_rate_limited` 终态 |
| 远程调用已开始但响应丢失 | 行内最多对账 300 秒；仍不明写 `unresolved(login_remote_unknown)` 并跳下一行，绝不伪装失败或自动重发/重验 |
| 未解行晚到权威结果 | 独立 reconciler 在 24h 窗口内继续 readback；收口后递增 `resolution_version`、修正计数并发 correction 提醒，超期为 `manual_review_required` |
| Telegram 需要 2FA，页面缺失或值错 | `url_missing_2fa/twofa_invalid`；明文不持久化，`policy=do_not_store` |
| 授权已持久化，资料同步/online readback 失败 | 行 `succeeded_with_warning`，保留独立授权与后置投影 |
| 授权已持久化但目标分组 CAS 失败 | 行 `failed(pool_transition_failed)`，authorization 投影仍为已授权；不计 success，重试只补目标分组，不重复登录 |
| 手机号命中软删除账号 | precheck 409 `soft_deleted_account_conflict`，不自动新建/复活 |
| 历史数据中同手机号同时存在唯一有效账号与软删除旧账号 | alias 回填由有效账号持有唯一 alias，软删除旧账号计 `shadowed_deleted` 且不阻塞；若存在两个有效账号或无法唯一选出有效账号则按 conflict 整批阻断 |
| 内置系统管理员创建批次 | 内置管理员 principal id=0 不存在于 `app_users`；batch/notification 的 `recipient_user_id` 是认证主体 ID 而非 `app_users` 外键，仍以 `tenant_id + recipient_user_id` 隔离列表、提醒和幂等键 |
| worker 在远程调用前崩溃 | 租约到期后同 generation 可重领；调用已 started 则转 `reconciling` |
| cancel 与远程返回竞态 | 以 `state_version + execution_generation` CAS；远程开始后先对账，禁止写回旧 generation |
| 失败/未解行 URL 失效或超期 | 密文清除，重试返回 `credential_expired`；操作员通过 refresh-credential 提交新地址，新地址本身不触发 Telegram 调用 |
| 已有账号当前 UUID 与输入不同 | 默认阻断；只有 precheck 后显式替换且 expected binding version 命中才更新，旧 UUID 不出现在审计正文 |
| 账号改名、资料初始化或 item URL 到期 | 独立接码备注及 UUID binding 保持不变；只有显式替换或账号硬删除可以改变 |
| 明确重试未解行 | 必须提交 `confirm_remote_unknown=true + expected_attempt_id + expected_attempt_version + expected_resolution_version`；任一值过期则 409 |
| 系统处于 `off/reconcile_only` | 两者都拒绝创建/重试/换凭据；取消、提醒确认和读接口保持可用；`reconcile_only` 继续 reconciler/outbox，`off` 仅允许在无未解/started/待投递事件后进入 |
| 同时存在多租户/多批次 | 每轮每批最多推进一个 phase，按租户/批次公平轮转；持久 rate bucket 在所有 worker 间限制 host/开发者应用速率 |
| fingerprint 密钥轮换并发创建 | 对所有 accepted version 的 alias 按固定顺序加 advisory lock；账号、行项及 aliases 同事务创建，冲突返回类型化 409 |
| 批次进入 sequence-terminal | unresolved 为 `completed_with_unresolved`；终态、六类计数和 initial 提醒同事务写入，后续修正另发 correction |

## 12. 并发与幂等

- 批次内严格按 `line_no` 串行，但一次 claim 只推进一个 phase 后让出；调度按租户再按批次公平轮转，长批次不能饿死短批次。
- 跨批次、人工登录与单号重登按 `tenant_id + accepted-version phone alias/account_id` 互斥；所有 alias 采用固定版本顺序加锁，避免密钥轮换期间重复建号。
- 创建使用 `tenant_id + actor_id + idempotency_key`、`preview_token + preview_fingerprint`；行使用 `batch_id + line_no`，不以 `phone_masked` 判定身份。
- claim/完成/取消使用 `state_version + execution_generation + lease_owner`。网络调用期间不持有账号锁或 DB 事务。
- 重试仅创建新 generation，保留旧 attempt 审计；只有当前 batch-owned flow 可被 supersede。
- 远程每步显式写 `not_started/started/confirmed/unknown`；`unknown` 不得因 lease 过期自动重放。
- 全 worker 通过数据库持久 rate bucket 领取 host/开发者应用发送配额；进程内 sleep 或单 worker 限速不能作为全局限流。
- 行状态变更、六类计数、批次完成判定和对应 initial/correction outbox 在同一短事务内完成；CAS 失败必须显式重读，不覆盖新状态。
- `ACCOUNT_BATCH_LOGIN_MODE` 是后端硬门：只接受 `off/reconcile_only/enabled`，不能用隐藏前端入口代替。

## 13. QA 验收标准

自动化（`backend/.venv`，缩短时钟，fake HTTP/Gateway 只模拟故障不伪造业务成功）：

1. 解析/SSRF：E.164、精确 host/path/query、userinfo/fragment/重复 query、redirect、超 256 KiB、DNS rebind、peer IP、IPv4/IPv6 私网/保留/fake-IP 全部定向覆盖。
2. precheck/确认：只给 create/existing 候选、UUID 备注/冲突、迁池清单、队列位置、ETA 与约 8 小时 20 分的 100 行纯等待上界；覆盖手机号/UUID 批内重复、跨账号 binding 冲突、显式替换、preview 漂移、幂等冲突和软删除。
3. 授权探测：ACTIVE/session 不得直接判在线；fresh direct probe 的 true/false/error 分别进入 already-authorized/relogin/typed failure，probe error 不发 code。
4. 建号顺序：新号必须 baseline 成功后才建账号；非法/错误/不可达地址不得遗留账号、行 alias 或半成品事务。
5. 状态机：flow owner、code HMAC/login-time HMAC 变化、code+2FA、FloodWait、单行错误继续下一行；仅已进入目标分组的行计 success，分组 CAS 失败只补分组且不重复登录，资料同步/online readback 失败为 warning。
6. 崩溃/未知矩阵：远程前、started 后响应前、confirmed 后落库前、落库后 ack 前注入；300 秒后为 unresolved 并跳行，晚到结果由 reconciler 修正且不重复 send/verify。
7. retry/refresh：failed 与 unresolved 新 generation；未知行没有显式确认或版本过期均拒绝；刷新地址只换加密凭据，不触发 Telegram 副作用。
8. 调度/限速：多租户、多长短批次、多 worker 并发下仍一批一 phase 公平轮转；host/开发者应用持久 rate bucket 不超额、无饥饿。
9. alias/密钥轮换：新旧 fingerprint 版本并发命中同号只产生一个账号；固定锁序无死锁，alias/account/item 同事务。
10. 权限/mode：读写权限组合、跨租户 batch/item/notification/binding 均 403；普通详情/列表/提醒/导出只见 UUID hint，reveal 需双权限、reason、version、no-store 和审计；`off/reconcile_only/enabled` 逐项验证。
11. CAS/取消：lease 丢失、旧 generation/attempt/resolution 迟到回写、cancel 竞态和 item/code deadline 分型均不覆盖新事实，后续行继续。
12. 安全/2FA：item URL 按期清除但账号 UUID 加密 binding 保留；账号改名/资料初始化不丢映射，显式替换后旧值不可从普通投影读取；API/log/trace/audit/outbox 不出现完整 URL、UUID、code、2FA 或未掩码手机号；批量路径始终 `do_not_store`。
13. 提醒：initial/correction 与事实原子写且分别幂等 ack；正文分列 failed/unresolved/warning；TG Bot dead-letter 不影响平台事实并在平台暴露。
14. 启动/运行：worker、reconciler、outbox、heartbeat、mode、DNS/HTTPS、密钥、alias 回填、rate bucket 与部署 readiness 缺一即 fail closed。

真实 E4（预发/生产，少量专用号码，Release Gate 后）：

15. 单行完整证据：batch/item/flow、授权 session、fresh 权威授权、`desired_online`、online readback、目标分组、账号 UUID binding/脱敏接码备注与 initial 提醒一致；受控 reveal 回读原输入 UUID 且有审计。
16. 混合批次（新号 + already-authorized/relogin + 错误/超时地址）中错误行不阻塞，手机号与 UUID 映射无串号，计数及 failed/unresolved/warning 清单一致。
17. send/verify 返回前中断 worker，恢复后只 reconcile；制造晚到权威结果，验证 correction、计数修正、无重复 Telegram 副作用和旧 generation 覆盖。

## 14. 分期与外部约束

- **P0（本合同范围）**：本文全部合同；必须一次性覆盖幂等、flow 归属、未知结果对账、敏感数据和持久提醒。
- **P2 候选**：登录成功后显式、独立的托管 2FA 流程；接码平台配置页；结果导出。P2 不得以 fallback 进入 P0。
- **已知外部约束**：实测页面不提供号码，无法验证「URL 对应号码 = 输入号码」。precheck 必须把该风险明示给操作员，确认后记录 reason；若供应方新增可验证号码字段，再升级为强校验。

## 15. 发布与回滚

- 发布路径：`master -> release -> GitHub Actions Deploy Production`；本需求为 L2（新表、新 worker 角色、真实 Telegram 授权），必须有 Release Gate。
- 发布顺序：migration + accepted-version phone alias 回填/零冲突报告 + code-source binding 字段/唯一索引 + rate bucket → 后端以 `reconcile_only` 上线 → worker/reconciler/outbox readiness → 前端 → 仅给受控操作员权限并切 `enabled` 做单号 E4/故障注入 → 灰度授权。
- Release Gate 同时核对部署 SHA、migration、mode、新权限注册、UUID 加密/reveal 审计、worker/reconciler heartbeat、生产 DNS/peer IP、alias/binding 冲突、rate bucket、outbox/correction drain、精确 flow/session、权威授权和 online readback；任一缺失都不得声称上线完成。
- 回滚先切 `reconcile_only`，停止新阶段但继续未知结果对账、提醒投递和凭据到期清除；不得只隐藏 UI 或强杀远程已 started 行。全部未解/started 收口且 outbox 清空后才可切 `off`。保留已建账号、session、attempt、批次和提醒审计，不回滚真实授权。

## 16. Product Design Complete 自检

- 原始需求、交互、后端/API/worker、数据流、权限安全、边界与 QA 已覆盖。
- 单号失败/超时跳行、失败/未解/警告汇总、迟到纠正提醒、过期地址刷新、fresh 授权探测、先 baseline 后建号、目标分组归一均已落为唯一合同。
- 每行 UUID 到最终账号的接码备注、加密 binding、冲突替换、权限查看和改名后保留已有唯一合同。
- 公平调度、全局限速、fingerprint 轮换去重、并发/幂等、flow 归属、mode gate、事务边界、数据保留、发布/回滚和 E4 均有验证口径。
- 唯一外部约束是供应页无号码字段，已以明示风险确认处置，不阻塞开发。
- 结论：`design_status=product_design_complete`；`implementation_status=local_implemented`；`qa_status=targeted_passed`；`release_status=not_released`；`production_status=unproven`。本结论不代表生产 migration、灰度启用或真实 Telegram E4 完成。

## 17. 备注

- 接码平台：`tgbotchecker.com`。
- 地址模板：`https://tgbotchecker.com/GetHTML?uuid=<32位uuid>`。
- **账号映射要求**：每一行实际 UUID 都必须在运行时加密绑定到最终账号，并在账号页显示独立接码备注；不能只把 host/模板记在文档，也不能因登录完成、账号改名或 item URL 到期而丢失映射。
- 接码备注默认格式：`tgbotchecker · <UUID前6位>…<UUID后4位>`。具备 `accounts.code_source_credentials.read` 的同租户用户可经显式 reveal 查看完整 UUID；本文不硬编码任何账号的真实 UUID。
- 该地址来自需求方提供并于 2026-08-15 做过只读页面 POC；POC 只证明页面当时可读取，不代表 Telegram 登录成功。

## 18. 本地实现与验证记录（2026-08-15）

- 已在隔离分支实现并合并回本地 `release`：0148 migration、批次/行项/attempt/持久提醒/手机号 alias/rate bucket、严格接码 URL 与 HTML 解析、按 phase 串行 worker、远程未知对账、更正提醒、权限/mode/readiness、前端预检与进度 Drawer、账号独立接码备注及受控 UUID reveal。
- 行级异常合同已落为自动化用例：验证码/行总预算超时后进入失败并继续下一行；远程调用结果未知进入 `unresolved` 后让出顺序；未决重试必须完成探测并 supersede 旧 attempt；取消跳过未开始行；完成提醒分列失败、未解、警告；Bot outbox 在 worker 崩溃后可按持久租约重新认领。
- 本地证据：相关后端集合 `84 passed`；前端 `tsc + vite build` 通过；Alembic 批量登录表由 `0148_account_batch_login` 创建，`0149_batch_login_principal` 允许内置系统管理员作为批次/提醒主体；PostgreSQL 空库 migration、Compose YAML、部署脚本语法、Python 编译与差异检查均纳入发布闸门。
- 敏感值边界：真实手机号和 UUID 不写入代码、测试、日志或本文；账号保存加密 UUID 并仅显示 `平台 · 前6位…后4位`，完整值需要双权限、操作原因、binding version 与 no-store 响应。
- 未执行：生产 migration、手机号 alias 回填、`reconcile_only/enabled` 切换、生产 worker 启动、真实 Telegram 批量授权与 E4 故障注入。因此当前只能写本地实现/定向 QA 通过，不能写已上线或生产修复。

## 附录 A：接码链接实测记录（2026-08-15）

- 样例：`GET https://tgbotchecker.com/GetHTML?uuid=<redacted>`。返回 `200` + HTML，Cloudflare `DYNAMIC`；这只证明当时页面可读，不证明 Telegram 登录成功。
- 页面包含 `id="code"`、「登录时间」、「上次获取时间」和 `id="pass2fa"`；文档不保留 code/2FA 原值。
- 无效 uuid（32 个 0）返回 `200` 与错误页，因此必须解析页面语义。
- 轮询观察中 code/2FA/时间字段均未变；页面时钟与本地时钟有偏差，故生产判新使用 code/login-time keyed HMAC 变化，不比较明文或本地时间。
- 属性换序会使锚定属性顺序的正则失配；生产实现必须用 HTML parser 按 `id` 取 value。
- POC 脱敏结果：`{code_present: true, twofa_present: true, login_time_present: true}`；错误页可识别；两轮轮询 code 未变。POC 没有触发 Telegram `send_code`。

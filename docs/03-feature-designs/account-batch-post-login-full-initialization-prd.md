# 批量账号逐账号完整初始化 PRD

- `intake_id`: `intake-2026-08-26-account-batch-post-login-full-init`
- `level`: `L3`
- `design_status`: `complete`
- `implementation_status`: `in_progress_profile_abc_before_two_fa_wait`
- `qa_status`: `pending_order_regression`
- `release_status`: `not_started`
- `production_status`: `unproven`
- `owner_flow`: `product -> dev -> qa -> product -> release -> prod-diagnosis`
- `authoritative_since`: `2026-08-26`
- `latest_local_baseline`: `a0ce7993fc9480672ed43e77dc9a5f9d6c8541e9`

## 1. Intake Card

- `raw_input`: 批量导入的 2FA 账号没有记录已验证密码，也没有改为平台固定 2FA；每导入成功一个账号也没有立即完成名字、头像和 ABC 备份初始化；此前已登录账号重新进入批量登录时也没有补齐缺口。
- `business_goal`: 每条批量登录行在 A 可用后，无论是新建、已授权还是重登，都统一创建或续接完整初始化；先完成姓名/头像；未启用 2FA 或已记录 Telegram-accepted 当前密码的账号先完成真实 ABC，再处理 fixed 2FA；只有当前密码未知的账号才等待服务端 reset，fixed 后续接原 ABC request。全部真实读回后才算该行完整成功。
- `affected_surface`: 批量登录、租户固定 2FA、账号安全批次、资料初始化、ABC 灾备、任务中心、提醒、审计和生产 E4。
- `authorization_scope`: 部署后使用 `normal_full_init_v1` 的批量登录 normal 账号；覆盖本行 `new_account/already_authorized/relogin` 三种 route。
- `out_of_scope`: 未重新进入批量登录的历史账号后台扫描、独立 account-ID 追补入口、接码专用账号、降权专用账号、设备清理和业务任务配置。
- `security_note`: 用户所述固定 2FA 值不得写入代码、PRD、日志或审计；运行时只读取租户设置中的加密固定密码及其版本。

## 2. 最新本地复核与根因

2026-08-26 重新复核本地 `master`：工作树干净，`HEAD=fde29376`，相对 `origin/master` 与 `origin/release` 的 `2e739e0c` 均领先 1 个提交。该提交只补强 ABC 的 B pre-challenge Timeout unknown 收口，没有触碰批量登录、2FA、资料初始化、API/UI 或 migration，因此没有修复本需求。

当前代码的首个断点如下：

1. 批量登录从接码源读取 2FA 并完成 A 登录，但 `_persist_authorized_session` 没有持久化本次已被 Telegram 接受的密码。
2. `rotate_managed_two_fa_after_login` 当前只调用 `record_managed_two_fa_password` 保存传入的现密码，没有调用 Telegram 改密，也没有切换为租户固定密码。
3. A 成功时只调用 `queue_login_profile_initialization` 创建资料批次；父登录项不保存资料子项关系，也不等待姓名和头像远端读回。
4. `online_readback` 后立即调用 `succeed_claim`，所以 A 已登录会被展示为成功，即使 2FA、资料和 ABC 都没有完成。
5. 批量登录没有创建 ABC request；现有 ABC 只支持固定 10 账号 canary 和全量在线 frozen-N，不能把当前单账号直接塞入现有 open batch。

`fde29376` 提供的安全复用点是：B 已记录 `remote_effect_started_at`、尚无 challenge/flow/code/candidate 时发生 Timeout，必须停止、保持原 operation unknown，经纯数据库 preview、异人批准和 CAS 收口为 manual debt；禁止重登、猜测 no-effect 或改写 A。

## 3. 文档优先级与冲突裁决

本 PRD 是“批量登录账号在 A 可用后的完整初始化”唯一专项合同。在本范围内，它补正以下旧口径：

- 批量登录的 `two_fa_policy=do_not_store` 不再适用于启用本策略的 normal 账号，无论本行是新建、已授权还是重登。
- “登录不得自动改线上 2FA”不再适用于后端强制 `normal_full_init_v1` 的eligible normal行；新建、已授权和重登route都必须先做gap decision，再只推进缺失子项。
- 对目标普通分组的normal行，`normal_full_init_v1` 由后端强制且前端只展示不可关闭，不是运营可降级选项；旧策略只服务历史batch和本文排除的专用用途。
- “登录后资料只入队”和“备用缺口不阻塞登录成功”仍可描述独立子事实，但不能再代表完整初始化成功。
- 10 账号 canary 和全量 frozen-N 继续保持原合同；新增单账号 `post_login_exact` 不能修改或绕过它们。
- 初始导入确认不等于 ABC 异人批准；不能预先批准尚不存在的账号、A Session 或 generation。

## 4. 适用范围和准入

### 4.1 必须进入完整初始化判断的账号

同时满足以下条件时强制进入：

- 路由是 `new_account`、`already_authorized` 或 `relogin`；三者只影响 A 授权步骤，不影响后置初始化资格。
- 现有实现写入的 `route=create` 在新合同/API投影中规范化为 `new_account`；幂等键、审计和 QA 只使用规范值，旧行读取时兼容映射但不得形成第四条执行分支。
- `account_identity=normal`，目标 AccountPool 也允许普通运营用途。
- `new_account/relogin` 的 A 登录由当前 item/generation 精确 flow 完成；`already_authorized` 由当前 item的新鲜、直连、权威 probe确认 A 可用。
- 批次创建时冻结 `post_login_policy=normal_full_init_v1`。

后端必须二次拒绝 `code_receiver`、`rank_deboost` 和用途不一致账号，前端隐藏不能代替后端校验。

### 4.2 创建前本地预检

precheck 不连接 Telegram，只验证并冻结：

- 租户固定 2FA 已配置、可解密、具备单调版本。
- 固定2FA的非敏感hint策略和恢复邮箱处理策略已冻结；本链不自动新增、替换或移除恢复邮箱。
- 自动姓名策略和已审核、TG cache ready 的头像策略版本存在。
- App A/B/C 角色、SV/MY owner domain、代理和授权灾备合同配置完整。
- 请求者具备批量登录、资料更新、使用托管 2FA 和发起 ABC request 的权限。
- 目标分组和账号用途一致。
- batch-login、post-init coordinator、账号安全 worker、素材缓存和 ABC request capability均为可执行状态；任一 required capability为 off/unready时新策略 batch零写入拒绝。
- 同一 tenant 内手机号/alias只能解析到一个未删除账号，解析出的 account、pool、code-source binding和策略版本必须同 tenant；任一歧义或跨租户引用直接阻断。
- precheck token/request fingerprint必须包含规范化行、resolved account、目标pool、binding decision、full-init/fixed/profile policy版本和required capability版本；同idempotency key任一字段漂移返回冲突。
- 同batch多个输入若经任一有效phone alias解析为同一account，创建前整体阻断；数据库对非空`(batch_id, account_id)`增加唯一约束，不能靠共享owner掩盖重复行和重复计数。

任一必需配置缺失时，创建接口零写入并返回类型化错误。运行资源的瞬时健康、Telegram 可用性和容量不在创建链同步探测；它们在各执行阶段形成真实事实。

## 5. 唯一成功口径

“每条批量登录成功行立即初始化”定义为：`new_account/relogin` 在 A 登录事务成功时、`already_authorized` 在 fresh probe确认时，立即创建或 attach 唯一、持久、可恢复的 full initialization，并写入 `abc_required=true` 的持久义务；不是等整批结束后扫描，也不是每次重触发都新建一套 mutation。ABC 义务先按现有 owner 分类为 attach/readback/conflict；只有没有可复用 owner时才创建 `post_login_abc_request`。

使用 `normal_full_init_v1` 时，父登录 item 只有全部满足才可进入 `succeeded`：

1. A fresh authorization 已持久化并在线读回。
2. 账号进入目标普通分组。
3. 2FA 来源已分类为 `verified_current/code_source_candidate/missing/already_fixed/manual_required`；只有实际经 Telegram 接受的 current/candidate才允许加密记录，`missing/already_fixed` 不伪造源密码。
4. Telegram 2FA 已由真实 mutation confirmed或受保护的密码校验证明为冻结的租户固定版本，账号安全快照和 operation evidence一致。
5. 平台 `display_name` 与 Telegram `first_name/last_name` 都与同一批准候选一致并完成读回。
6. 平台头像素材/对象投影与 Telegram 远端裁切后的感知指纹都与冻结目标一致。
7. B/SV 真实授权完成且资格读回通过。
8. C/MY 真实授权、MY 本地与 SV SSH 双副本、恢复密钥、inventory、隔离 restore probe 和断连门禁均通过。
9. A Saved Messages E4 存在非空 Telegram `remote_message_id`。

`authorization_status=confirmed` 可在 A 完成后单独展示；它不能增加 `success_count`。required 子项缺失不得包装为 `succeeded_with_warning`。

A 已确认后父 item进入非终态 `post_initialization_waiting` 并释放 login flow lease/rate bucket，由 coordinator推进；不得调用旧 `succeed_claim`、清除仍需使用的 secret ref或提前 finalize batch。只有 full-init owner终态成功才写父 item `succeeded`；等待审批、人工或 unknown 可以长期持久化但不占 login worker。

父 batch只有全部item成功才为`completed`；存在manual/failed/unresolved分别投影`completed_with_manual/completed_with_failures/completed_with_unresolved`，取消单独守恒。任何异常终态都不能增加`success_count`。

## 6. 逐账号状态机

```text
A authorized or fresh probe confirmed
  -> authorization_confirmed + full_init_bound + abc_required
  -> abc_owner_resolution(initial: attach/readback/conflict/request_pending_prerequisites)
  -> pool_transition
  -> profile_batch_created
  -> profile_name_running
  -> profile_name_readback
  -> profile_avatar_waiting_cache
  -> profile_avatar_running
  -> profile_avatar_readback
  -> abc_owner_resolution(recheck)
  -> abc_owner_attached | abc_readback_satisfied | abc_request_prepared
  -> if abc_readback_satisfied: keep evidence and continue
  -> abc_login_credential_resolution(no 2FA | Telegram-accepted encrypted password | managed snapshot)
  -> if credential ready: abc_request_ready -> approval -> B/C/E4 -> abc_readback_satisfied
  -> if current 2FA unavailable: keep request waiting_prerequisite, no B challenge
  -> two_fa_source_resolved
  -> fixed_2fa_rotation_pending | reset_requested_waiting(server date)
  -> fixed_2fa_rotation_started
  -> fixed_2fa_rotation_confirmed
  -> fixed_2fa_snapshot_readback
  -> if request was waiting_prerequisite: abc_request_ready -> approval -> B/C/E4
  -> abc_readback
  -> succeeded
```

允许的非成功终态：

| 状态 | 含义 | 后续边界 |
| --- | --- | --- |
| `failed` | 明确失败且可证明远端未发生未知副作用 | 仅按 typed safe-retry 合同重试 |
| `partial_failed` | A 已成功，但至少一个 required 子项明确失败 | 不计完整成功，不回滚已成功远端事实 |
| `manual_required` | 邮箱确认、ABC 人工债或其他明确人工项 | 保留原 operation 和证据 |
| `unresolved` | 任一远端副作用结果未知 | 只对账原 operation，不自动重放 |
| `cancelled` | 尚未开始任何后置远端副作用 | started 后禁止取消覆盖事实 |
| `blocked` | 策略/owner/账号生命周期发生确定性冲突 | 不自动降级；修复冲突后对同 owner重新 gap decision |

## 7. 固定 2FA 合同

### 7.1 密码来源与记录时点

- 接码源返回的密码在 Telegram 接受前不是真实账号凭据，不得提前标记为托管成功。
- Telegram 接受该密码并返回 A 授权后，A Session、flow 终态和源密码加密快照必须在同一数据库事务提交。
- 账号长期 current credential 以 `TgAccountSecuritySnapshot` 为权威；租户固定密码继续以租户加密设置为唯一来源。
- 租户设置增加单调 `fixed_two_fa_password_version` 和 key version/digest 读回，不得只靠设置时间猜版本。
- 账号快照增加 `two_fa_policy=tenant_fixed`、`tenant_fixed_two_fa_version`、`two_fa_verified_at` 和最后 operation/evidence 引用。
- 密码明文只能存在于批准 worker 的内存窗口，不进入协调表、API、日志、提醒、审计或命令参数。
- current/candidate仅以独立加密 secret ref暂存，绑定 tenant/account/item generation/source binding version/key version和可配置 expiry；明确错误立即销毁，confirmed后销毁旧值，unknown在原 operation收口前不得过期清除或换值重放。

### 7.2 Telegram 改密

固定化必须调用真实 Telegram 2FA mutation，不能继续使用当前“只保存现密码”的同名函数冒充轮换：

```text
verified current password + A Session
  -> freeze tenant fixed password version
  -> persist operation key and remote_effect_started_at
  -> Telegram updatePasswordSettings
  -> confirmed response
  -> account snapshot switches to tenant fixed version
  -> independent security snapshot readback
```

如果账号登录时没有启用 2FA，使用“无旧密码设置固定密码”的正式分支；如 Telegram 要求邮箱确认，进入 `two_fa_email_confirmation_required/manual_required`。

失败边界：

- 源密码错误：不记录为可信密码，不进入 fixed rotation。
- mutation 前固定设置缺失、不可解密或版本漂移：Telegram 调用数必须为 0。
- 明确失败：保留仍有效的源密码快照和 typed failure；已经先完成且与同一 A generation 绑定的 ABC evidence 不回滚，未取得可信当前密码的分支仍只允许 request preparation，B/C/E4 Telegram mutation 调用数为 0。
- 结果 unknown：保留原 operation、request key 和源密码加密快照，进入 `two_fa_rotation_unresolved`；不猜测当前密码、不自动发起第二次改密、不进入 B/C。
- Telegram mutation 已开始后，即使客户端返回异常、邮箱待确认或成功响应后的数据库提交结果未知，也统一保留原 request key：邮箱待确认进入人工项，其余不可判定结果进入 `reconcile_unknown`，都不得重放改密。
- 批量登录时被 Telegram 明确接受的当前密码必须立即加密写入账号安全快照，provenance 标记为本次登录接受、绑定 A authorization generation，并供 fixed rotation 前的 ABC B 登录使用；不得只放在短期 owner secret 后遗漏账号级记录。
- 只有固定密码已由 Telegram 明确接受并完成版本读回，才能覆盖上述源密码快照；旧密码随后按审计策略销毁，不保留普通历史明文或额外可 reveal 副本。
- `GetPasswordRequest=enabled` 只证明已启用，不能证明固定明文相等；`unchanged`、本地 ciphertext相等或普通状态快照均不算 fixed evidence。固定密码尚未证明不阻塞已有 Telegram-accepted 当前密码的 ABC；只有当前 2FA 已启用且没有可信当前密码时才禁止物化 B challenge。
- 邮箱确认沿用同一 operation/A generation；验证码只在请求内存使用，成功后再读回，错误明确失败，RPC unknown保持原 operation。UI提供带 expected version和原因的确认入口，不把邮箱码写入数据库/日志。
- 2FA hint不得含密码片段；未经独立配置和批准，本链不修改恢复邮箱。Telegram要求新的邮箱绑定时进入manual，不为完成批次自动填充邮箱。
- `manual_current_2fa_required` 可由有 credential-manage权限的操作者向原 full-init提交一次新 candidate；candidate不回显、不替换 unknown secret，只有 Telegram接受后才转可信，错误候选销毁并等待新的显式提交。

## 8. 姓名与头像初始化

- 复用账号安全批次和素材缓存执行内核，但本策略的 required actions 固定为 `update_profile + update_avatar`；username、bio 和账号面具不属于本次成功条件。
- 自动资料策略必须是租户已批准、带版本的默认策略；每个账号以不可变 seed 生成候选并先占用名称 claim。
- 头像只使用已审核、许可清晰且 `cache_ready_status=ready` 的非真人素材；未 ready 时进入 `profile_avatar_waiting_cache`，不得上传临时来源或伪造成功。
- post-init 必须保存精确 `profile_batch_id + profile_item_id`，不能只记录“已入队”。
- profile 子批次必须持久化账号级 idempotency key；创建响应后父 owner 提交未知时，下一轮先按该 key 重新绑定原批次，禁止再建一批并重复改名或上传头像。
- 完成证据必须绑定 full-init冻结的 profile policy版本、不可变姓名候选、名称 claim、许可素材 ID/cache SHA/crop算法版本和远端 readback；仅姓名/头像非空不算初始化完成。
- 姓名 mutation同时维护平台 `display_name` 与 Telegram first/last name，头像 mutation同时维护平台素材/object投影与远端photo evidence；远端成功但DB提交unknown时只按原item readback/前滚，不重复上传。
- 现有 `queue_login_profile_initialization` 会附带 username/voice-profile且只返回批次集合，禁止作为本链入口；只复用底层 exact `update_profile + update_avatar` executor、claim、素材缓存和 readback。
- 姓名必须拉取 Telegram `first_name/last_name` 读回；头像必须以远端对象和感知指纹读回。
- 姓名成功而头像 waiting/failed 时保持 incomplete；父 item 不成功。
- profile RPC unknown 只按冻结姓名和头像指纹做 readback/reconcile，不重复提交 mutation。
- 已有兼容 exact profile item则 attach；已有不兼容或 unknown资料 mutation则 `profile_owner_conflict`。人工保护资料或 preview后人工改值不得静默覆盖，进入 `profile_manual_override_conflict` 并要求重新裁决。

## 9. 单账号 ABC 合同

### 9.1 立即建立义务、资料完成后按当前密码能力先做 ABC

`new_account/relogin` 的 A 登录提交，或 `already_authorized` 的 fresh probe确认提交，必须同时 create-or-attach账号级 full-init和持久 `abc_required` 义务。gap decision 立即查询本账号 ABC owner：恰好一个可复用 owner则绑定原 owner，完整终态证据则进入 readback，多个 owner则 conflict；没有可复用 owner时先记 `request_pending_prerequisites`。姓名/头像完成后 coordinator 再次检查 owner；仍无 owner即创建唯一 `post_login_abc_request`。如果账号未启用 2FA，或平台持有与当前 A generation 绑定的 Telegram-accepted/managed 当前密码，请求直接进入 `waiting_approval`，异人批准后先完成真实 B/C/E4，再执行平台固定 2FA rotation；这样 Telegram 的 session/password fresh 限制不会阻塞资料和可执行的 ABC。批量导入确认不能替代 ABC 批准。

如果当前 2FA 已启用但平台没有可信密码，request 保持 `waiting_prerequisite`，不得物化 ABC batch、不得发送 B 登录 challenge；同一 owner 先等待 Telegram eligibility/reset server date，reset 完成并设置 fixed 2FA 后再激活原 request。ABC 已有完整终态 evidence 时允许在 fixed 2FA 等待期先做只读验证并写入当前 owner 的 `abc_evidence_ref`；没有 B/C 的账号不能把“request 已准备”展示为 ABC 完成。若未来提供经专项批准且能证明独立 AuthKey/identity 的无密码授权协议，必须另立 PRD 和 E4，不能在本合同静默切换。

批准必须满足：

- `originating_user_id` 永久保留原批量登录创建者；`requested_by_user_id` 在request生成时取当前显式 `execution_owner_user_id`，不得因新binding自动变化。
- `approved_by_user_id != requested_by_user_id`；execution owner接管后旧preview/approval失效并递增request version。
- `approval_ref` 必填。
- 批准绑定 exact manifest fingerprint、post-init version、A/current/generation、当前 2FA credential provenance/密文摘要、目标固定 2FA 版本、profile readback、App A/B/C 版本和 execution release SHA；不得记录密码明文。
- 任一冻结事实漂移使批准失效，重新 preview 和异人批准。
- 禁止永久 standing approval 批准未知的未来账号。

### 9.2 与现有 ABC 的关系

新增 `selection_mode=post_login_exact`，目标恰好为一个账号。它不是 10 账号 canary，也不是全量 frozen-N：

- request 排队阶段不属于 open ABC batch，不连接 Telegram、不占 MY client。
- “本账号 ABC owner”与“同租户其他 open global batch”分开查询：前者按账号 attach/readback，后者不是本账号 owner，只让新 request显示 `waiting_global_abc`。
- 只有全局无 open ABC、runtime/unknown/MY client 等门禁通过，coordinator 才把最早已批准 request 物化为单账号 ABC batch/item。
- 现有全局 supervisor 在 full sweep 与 deferred recovery 均空闲后，领取最早的 `approved/running post_login_exact`，逐账号串行执行 A probe -> B/SV -> source-less C/MY provision -> E4；批准后不再依赖人工 CLI 二次启动。
- exact item 的 `status/outcome=succeeded` 不是完成证据；post-init 必须再次读回 current A、合格且 AuthKey 独立的 B/C、C 双副本/restore probe，以及带非空 Saved Messages remote message ID 的 E4 audit，才能写入 `abc_evidence_ref`。任一缺失进入 `reconcile_unknown`，不重放 B/C/E4。
- `post_login_exact` 禁止通用手工 runner 直接执行，保证 compose supervisor 是自动执行 owner；进程重启只按原 batch/item/operation继续，`stopped` 或 unknown债务不自动 resume、不新建第二次 B/C/E4。
- 启动前合同错误把 exact item/batch 显式停止并投影 typed blocker；原 item已有 unknown operation时投影 `reconcile_unknown`。未分类的 supervisor异常只暴露该 lane 错误，不得连带暂停无关 full sweep/deferred batch。
- 不得把该账号追加到已冻结的 canary/full sweep N，也不得由登录 worker内联执行 B/C/MY。
- A、B、C 必须使用不同 Developer App、不同 AuthKey 和非零 Telegram authorization hash；C 完成后 MY active client 必须回到 0。

### 9.3 unknown 复用

#### 2026-09-05 ABC E4 Saved Messages 修正

线上账号1370已完成B/SV与C/MY，但E4消息在发送前的`SetTypingRequest`返回`PeerIdInvalidError`。使用当前A只读解析确认目标为`User.is_self=true`且输入实体为`InputPeerSelf`；再次仅执行typing重现同一异常，消息发送尚未开始。Saved Messages没有向对方展示正在输入的语义，网关应在明确识别self目标时直接进入真实消息发送，不调用typing；普通用户/群/频道仍保持原typing与失败边界。不能捕获typing异常后继续发送，也不能通过假remote ID完成E4。

本次变更不改ABC审批、账号集合、A授权或B/C资产。验收须覆盖真实self类型识别、self发送成功与真实message ID、普通目标typing失败仍为`remote_mutation_started=false`、消息发送开始后未知结果仍不可重放。账号1370原E4是明确pre-send失败；修复发布后使用新审批引用绑定的新E4 operation验证，不重建已成功B/C。完整E4证据读回前仍未完成。

- B pre-challenge Timeout 严格复用 `fde29376` 的 `stopped_b_prechallenge_unknown`：原 operation、纯 DB preview、异人批准、CAS manual debt、A 零写入；禁止第二次登录。
- B/C/E4 的其他 unknown 继续复用现有 ABC reconcile、deferred/manual contracts。
- SSH 断连视为 unknown；先读回 operation、runtime、bundle、inventory 和 E4，再决定状态。
- 任一 A Session/current/generation 漂移立即停止该 request，不自动选择另一个账号或重建 A。
- terminal `manual_required/deferred/unknown` 永远绑定原债务；terminal failed只有在原 operation明确 confirmed-no-effect且既有 retry合同允许时才重试原 request，不能新建 owner；cancelled/superseded也必须证明无副作用并CAS释放后才可重新 gap decision。
- A generation变化会使原 ABC整体 evidence失效，但不自动重登仍健康的 B/C；新 request重新 exact计划，合格 B/C只读回，只补失效槽位和绑定新 A generation的 E4。

## 10. 数据模型

新增账号级 `tg_account_full_initializations`：

- 身份：`tenant_id/account_id/initialization_generation/policy_version`。
- 冻结版本：`policy_version/account_version/account_identity/target_pool_id/primary_authorization_id/primary_session_digest/authorization_generation/authorization_fact_generation/connection_generation/code_source_binding_version/tenant_fixed_two_fa_version/profile_policy_version`。
- 状态：`status/state_version/next_retry_at/lease_token/lease_expires_at/failure_type/failure_detail/reconcile_status`；通用 `next_retry_at` 只调度当前 stage。
- 2FA：`two_fa_capture_status/two_fa_rotation_status/two_fa_operation_key/two_fa_remote_call_state/two_fa_remote_effect_started_at/two_fa_secret_ref/two_fa_evidence_ref/two_fa_next_retry_at`；专用 due 字段只保存 Telegram `until_date/retry_date`，profile/ABC preparation 不得覆盖它。
- 资料：`profile_batch_id/profile_item_id/profile_name_status/profile_avatar_status/profile_evidence_ref`。
- ABC：`abc_required/abc_resolution_status/abc_owner_type/abc_request_id/abc_batch_id/abc_item_id/abc_status/abc_evidence_ref`；owner/request 外键按分类可空，但 `abc_required` 不可丢失。
- 审计：`originating_user_id/execution_owner_user_id/created_at/updated_at/completed_at`；共享binding不自动改执行owner。

新增触发绑定表 `tg_account_login_post_initialization_bindings`：

- `login_batch_id/login_item_id/login_execution_generation/account_id/full_initialization_id/trigger_route/triggered_by_user_id/binding_status/detached_at/detach_reason`。
- 唯一约束 `UNIQUE(login_item_id, login_execution_generation)`。
- binding和owner都保存 `tenant_id` 并做 tenant/account一致性校验；账号级 partial unique约束保证每个 `(tenant_id, account_id)` 最多一个 active full-init，竞态冲突方重新读取后 attach，不在应用层盲重试 insert。
- active binding数量在锁定owner后从binding表计算，不保存可漂移计数；ABC request对`(full_initialization_id, request_version)`唯一且每个full-init最多一个active request。

同账号只能有一个 active full-init owner。新 batch item 的 gap decision：已有 active owner则 attach/wake；已有 succeeded owner且固定 2FA/profile/ABC evidence 与当前 A/policy仍一致则只读回；证据漂移才创建更高 `initialization_generation`。任何新触发都不能覆盖旧 operation/evidence。

新增短期 secret record，只保存 `tenant/account/full_init/type/key_version/ciphertext/source_version/expires_at/status`；API和审计不返回 ciphertext。账号、batch或binding删除均不得 cascade删除 full-init、operation、evidence或未收口 secret；只允许软隐藏投影，secret按第 7 节状态销毁。

新增 `tg_post_login_abc_requests`，在姓名/头像完成且再次确认没有可复用 ABC owner时创建；当前 ABC 登录凭据不足时承载 prerequisite waiting，凭据可用时承载 preview、排队和批准：

- `full_initialization_id/account_id/request_version/status(waiting_prerequisite/waiting_approval/approved/running/succeeded/manual_required/reconcile_unknown)`。
- `manifest_json/manifest_fingerprint/execution_release_sha`。
- `originating_user_id/requested_by_user_id/approved_by_user_id/approval_ref/approved_at`。
- `abc_batch_id/blocker/failure/reconcile fields`。

上述表都不得保存 2FA、验证码、Session、AuthKey、完整手机号或接码 URL 明文。

`tg_account_login_batches/items` 增加 `post_login_policy/authorization_status/post_initialization_status/full_initialization_id` 及 `authorized_count/fully_initialized_count/post_init_waiting_count/manual_required_count` 投影；旧策略/旧 batch继续按旧 success语义，新策略 `success_count=fully_initialized_count`，两者不得混算。

## 11. API、权限和 UI

### 11.1 API

- 批量 precheck/create 冻结 `post_login_policy=normal_full_init_v1` 及 fixed-2FA/profile policy 版本；normal 账号三条执行 route 都不允许前端静默降为旧策略。
- `GET /api/tg-accounts/login-batches/{batch_id}` 的每行增加 `authorization_status` 和 `post_initialization` 脱敏摘要。
- `GET /api/tg-accounts/login-batches/{batch_id}/items/{item_id}/post-initialization` 返回 2FA、姓名、头像、ABC/B/C/E4 子状态和 blocker。
- `POST /api/tg-accounts/post-login-abc-requests/{id}/approve` 接收 `expected_version/expected_fingerprint/approval_ref`，后端强制异人批准。
- `POST /api/tg-accounts/post-login-initializations/{id}/reconcile` 只收口原 unknown；不接受目标终态，不重放副作用。
- `POST .../{id}/two-fa-current-candidate` 与 `POST .../{id}/two-fa-email-confirmation` 都要求 expected owner/operation version、原因和 credential-manage权限；敏感输入不进入响应、日志、审计或通用 request dump。
- A 尚未确认时，cancel按既有批量登录合同停止该行；A 已确认并已建立账号级强制初始化义务后，cancel只把当前父行投影为 skipped，不撤销 owner 或清除其临时凭据，固定2FA/资料/ABC仍继续安全收口。owner完成后仍回写该行子状态和批次完整初始化计数，但不把 skipped父行改成成功。账号删除/用途变更使用同一安全分类，不能级联删除证据。
- `POST .../{id}/assume-execution-owner` 要求expected version、原因及全部required权限；只在原owner失权/离职或全部原binding解除后允许，写审计并使未物化ABC preview/approval失效，不能改写originating user。
- retry 只允许 typed、已证明远端副作用未发生的阶段。

### 11.2 权限

- 创建批量登录：既有批量登录权限。
- 使用租户固定 2FA：`accounts.security.credential_manage` 或专用 use 权限，不返回密码。
- 姓名/头像：`accounts.profile.batch_update`。
- 发起 ABC：`accounts.security.complete_online_abc`。
- 批准 ABC：授权灾备批准权限且 actor 不同于 requester。
- reveal、改密、资料 mutation、ABC 批准和 reconcile 全部写 secret-free AuditLog。
- 每个高风险子动作开始前重验 tenant、账号未删除、normal用途、A/current/generation、冻结策略和执行 capability；权限被撤销时未开始动作停在 `request_authorization_revoked`，started/unknown仍只收口。重新接管必须由有权限用户显式操作并形成新审计，不能自动替换 requester。

### 11.3 UI 与提醒

每行固定展示 A 登录、固定 2FA、姓名、头像、ABC 审批、B/SV、C/MY、E4 和当前 blocker。至少支持：

- `等待固定 2FA`
- `2FA 改密结果待对账`
- `等待头像缓存`
- `姓名已完成 / 头像未完成`
- `等待 ABC 异人审批`
- `等待全局 ABC 执行窗口`
- `ABC 人工处理`
- `ABC 结果待对账`

批次同时显示 `authorized_count/fully_initialized_count/post_init_waiting_count/manual_required_count`；`success_count` 对新策略等于 fully initialized。A阶段全部结束时可发“授权阶段完成”进度提醒，但不是 terminal initial；只有所有 full-init进入成功或显式异常终态才 finalize，后续收口使用 resolution-version correction，不显示敏感明文。

## 12. Worker、幂等和并发

- `account-login worker`：负责 A 登录或 fresh authorized probe、可信 2FA candidate binding、目标分组，以及原子 create-or-attach full-init 与 `abc_required` 义务；gap decision 只绑定既有 ABC owner或记录 `request_pending_prerequisites`，不创建 B/C/MY mutation。
- `post-login-init coordinator`：只编排并读取子状态，不直接调用 Telegram。
- `account-security worker`：执行固定 2FA、姓名和头像。
- `material-cache worker`：推进头像缓存。
- `authorization-abc supervisor`：按 `full sweep -> deferred recovery -> post_login_exact` 单执行权顺序，自动执行已批准或进程重启后仍 running 的 exact batch；只恢复持久化阶段，已 stopped/unknown债务不重放。
- login提交事务：new/relogin必须原子写 A Session/flow终态/2FA source分类或ref/trigger binding/full-init/`abc_required`；already-authorized必须原子写 fresh probe evidence/trigger binding/full-init。事务 unknown只按当前 item/generation读回，不允许出现 A confirmed却永久无 binding。
- invariant reconciler只扫描部署后启用策略且 `authorization_confirmed + binding missing/inconsistent` 的 login items，补齐同一 item义务或报冲突；它不是历史账号扫描，也不连接 Telegram。

幂等键：

- trigger binding：`login-full-init-bind:{item_id}:g{generation}`。
- full-init：`account-full-init:{account_id}:p{policy_version}:g{initialization_generation}`。
- 2FA：`login-post-init:{id}:twofa:v{state_version}`。
- profile：`login-post-init:{id}:profile:v{policy_version}`。
- ABC request：`login-post-init:{id}:abc-request:v{request_version}`。
- B/C/E4 继续使用现有 ABC operation/item 派生键。

并发边界：

- 同账号同一时刻只有一个 full-init owner和一个 2FA/profile mutation owner；重复批量登录只 attach，不抢占。
- active owner只有在 A、fixed/profile policy及目标池冻结版本兼容时才 attach；不兼容的新 item进入 `waiting_active_owner_policy_conflict`，等旧 owner安全终结后重新 gap decision，禁止并行 higher generation。
- 已有账号安全 2FA/profile mutation owner时，目标和版本完全相同则 attach；不同目标、started unknown或人工保护冲突则阻断，不创建第二个安全批次。
- profile mutation 在 A 权威确认后优先执行，不依赖 fixed 2FA；姓名/头像未确认前不创建 ABC request。姓名/头像确认后，未启用 2FA 或持有可信当前密码的账号可先物化 B/C/E4；只有当前 2FA 已启用但密码未知的账号保持 `waiting_prerequisite`。
- 不同账号 A 登录继续使用现有有界并发；逐账号 post-init 独立推进，不等整批结束。
- ABC 全局任一时刻只有一个敏感 operation；排队不绕过 open batch 和 runtime gate。
- DB commit unknown 时按原 request/operation key 读回，禁止创建第二个 owner。
- coordinator按 `next_retry_at + tenant fairness` 有界领取，等待审批/global ABC/capability off的owner不持有lease且不阻塞其他账号；监控必须暴露 oldest waiting、orphan binding、duplicate owner、unknown和secret-cleanup overdue。

## 13. Migration、mode 与回滚

- 使用 additive migration；历史行不改状态、不自动改密、不自动建 ABC。
- 仅部署后新建且明确冻结新策略的 batch 启用；部署时仍运行的旧 batch 保持旧分母。
- 历史终态行保持 `legacy_post_init_not_requested`，不在 migration 中扫描或执行 Telegram mutation；只有该账号重新进入新的批量登录 item，才按下节统一 route合同 create-or-attach。
- 独立 `ACCOUNT_POST_LOGIN_INIT_MODE=off|reconcile_only|enabled`；不复用batch-login mode冒充子链能力。`off`停止创建新远端副作用但started/unknown仍须对账，`reconcile_only`只推进收口。
- 回滚不撤销已成功固定 2FA、不改回旧密码、不删除姓名/头像/B/C/Session/双副本/审计。
- started/unknown 由兼容 reconciler 继续收口；全部远端 owner 和 unknown 清零前不得移除兼容代码或强停 worker。
- 上线顺序固定为 expand schema/index -> 部署兼容 reader/writer且feature off -> capability/readiness检查 -> tenant启用新策略；回滚先关新建/新副作用、保留reconcile，再回退应用。存在新状态行时禁止回滚到会把 `post_initialization_waiting` 当成功或未知状态的旧版本。
- `route=create` 只做读取兼容映射，migration不改历史行；新 writer只写 canonical `new_account`。批次/账号软删除不物理删除full-init链。
- 已配置租户fixed ciphertext原样保留并回填version=1、空配置version=0；key version/digest由受保护backfill生成且不输出明文。legacy账号密码快照不补造provenance，enable前必须读回tenant版本/摘要和partial-unique/index状态。

## 14. 已登录账号重新触发批量登录

### 14.1 三条 route 的统一决策

批量登录仍按现有 fresh A 事实区分 route，但三条 route 都进入同一个 `full_init_gap_decision`：

| route | A 边界 | 后置动作 |
| --- | --- | --- |
| `new_account` | 当前 item/flow 新建 A 并权威读回 | create-or-attach full-init |
| `already_authorized` | 当前 item执行新鲜、直连、权威 A probe；不得用数据库 ACTIVE 代替 | 不重登 A，create-or-attach full-init |
| `relogin` | 当前 item/flow 完成新的 A 登录并读回 | create-or-attach full-init |

账号没有再次进入批量登录时，本合同不做后台全租户扫描。每个重新触发的 item 必须冻结 account/pool、A current/Session digest/三组 generation、2FA snapshot、当前 item接码 binding、profile旧值、ABC owners、tenant fixed/profile policy版本；gap decision 本身不调用 Telegram mutation。

### 14.2 create、attach 或 readback

- 无 full-init owner：创建新 initialization generation，并让当前 item绑定它。
- 已有 active且冻结合同兼容的owner：当前 item只能 attach/wake；不兼容则等待其终结后重新决策，不得创建第二套 2FA/profile/ABC operation。
- 已有 succeeded owner且 fixed-2FA、姓名头像、A/B/C/E4 evidence与当前 A/policy完全一致：当前 item只绑定 `already_complete` 读回，零 mutation。
- succeeded evidence因 A generation、fixed policy或远端资料漂移而失效：创建更高 initialization generation，只修复实际缺口。
- 存在多个 active owner或证据归属冲突：`full_init_owner_conflict`，当前 item停止，禁止猜测 keeper。

父 batch item 在 attach 后等待同一个账号级 owner；另一个 batch item不能把 owner失败改成成功，也不能用自己的超时取消已 started operation。

### 14.3 已授权 route 的 2FA 来源

`already_authorized` 没有本次 Telegram 2FA登录成功事实，因此接码页面密码只能是 candidate：

- snapshot已有带来源证据的 current password：以其执行真实 fixed rotation。
- legacy snapshot只有ciphertext但缺少Telegram accepted operation、来源和policy version时降为candidate，不得自动视为可信current。
- snapshot无 current password，但本次/既有同版本 code-source binding 可读：服务端重建白名单 URL并读取 candidate，只以临时加密 secret ref提交 Telegram。
- Telegram只读状态为 missing：无需旧密码设置 fixed 2FA。
- Telegram为 enabled且没有 snapshot/candidate：`manual_current_2fa_required`，不得以固定密码盲试或重置。

candidate 在 Telegram 接受前不得写入 current snapshot。RPC成功后直接记录 tenant fixed version并销毁 candidate；密码错误则销毁并转人工；unknown保留原 operation和临时加密 ref只做对账。`unchanged` 只有证明真实远端校验发生才算 fixed，本地字符串相等或客户端短路不算证据。

`new_account/relogin` 若本次 2FA 已被 Telegram 接受，则沿用第 7 节可信 current password；否则同样进入上述 missing/candidate/manual分类。

code-source凭据过期或binding漂移时停在 `two_fa_candidate_refresh_required`；只允许对原 login item以 expected item/binding version刷新，且仅在2FA operation未 started时替换。started/unknown后禁止刷新 candidate来重放。

### 14.4 资料与 ABC 去重

- 姓名/头像 evidence完整且仍匹配 Telegram：readback only。
- 任一资料缺口：复用 exact profile item、名称 claim、许可头像和独立 readback，只修改缺失动作。
- 缺少同 policy版本的平台证据时，任意非空外部姓名/头像仍算 gap；若标记为人工保护则阻断并展示，不静默覆盖。
- 恰好一个 open ABC owner：attach/wait。
- terminal succeeded ABC 且 A/current/generation/evidence仍匹配：already-qualified readback。
- terminal manual/unknown/deferred：attach原债务并按原 operation收口，禁止新建。
- 多个 ABC owner：`abc_owner_conflict` 并停止。
- 无 ABC owner且姓名、头像均完成：创建唯一 `post_login_exact` request；当前 ABC 登录凭据可用时立即进入异人批准和物化，只有 2FA 已启用但可信当前密码缺失时保持 `waiting_prerequisite`，待 reset/fixed 后继续原 request。

每次子动作前后都复核账号未删除/封禁、仍为normal且在冻结目标池、A current/Session/generation未漂移。漂移发生在调用前则零远端写入阻断；调用started后只收口原operation，父 item不得成功。账号重新登录产生新 A时，旧 owner终结后由当前 binding创建higher generation。

### 14.5 已登录 Session 作为 ABC 验证码设备

`already_authorized` 重新进入批量登录时，不重新登录 A；`relogin` 完成新 A 后也只使用当前权威 A。两条 route 的 `post_login_exact` ABC 都必须冻结 `current_authorization_id + A Session digest + authorization/fact/connection generation`，并把该 A authorization 作为 B/C 登录 challenge 的唯一验证码读取设备：

- B/C 发起新 App 登录 challenge 后，通过冻结 A Session 读取 Telegram 官方 `777000` 服务消息；只接受 challenge 发送时间窗口内、带唯一 Telegram message ID 和 received-at 的验证码。
- 批量导入行中的接码 URL 只服务 A 登录和 current-2FA candidate，不得作为 ABC B/C 的验证码来源；A Session 服务消息不可读时显式进入 `verification_code_unreadable` 或原 operation unknown，不回退外部接码页。
- A 只承担验证码只读和最终 Saved Messages E4，不改变 A Session、App、AuthKey、current slot 或 generation；B/C 必须继续使用不同 Developer App、不同 AuthKey 和非零 authorization hash。
- “已有 Session 可收 code”不等于“可绕过 2FA”。Telegram 未启用 2FA 时，B/C 可在 code 后直接完成；当前密码已由本次登录明确接受或已有同 A generation 的 platform-fixed 证明时，B/C 在 code 后使用该密码；2FA 已启用但旧密码未知时，request 继续 `waiting_prerequisite`，不得发送 B challenge，必须按 Telegram 绝对 reset due 完成 reset/fixed 后再由同一 A Session 取 code。
- 已有 Session 上执行 fixed-2FA mutation 仍走 current A 的 `getPassword -> updatePasswordSettings/resetPassword` 正式路径；missing 可直接设置，可信 current 可 rotation，未知 current 不能用 Session 身份跳过密码校验。

验收必须分别证明：`already_authorized` 未重登 A、`relogin` 只产生一次新 A；B/C 的 `code_source_authorization_id` 等于冻结 current A；gateway poll 的 Session 与冻结 A 相同；验证码绑定 message ID/时间窗；B/C AuthKey 与 A 不同；外部接码 URL 未被 ABC runner读取；unknown不重放。

批量详情逐行展示 `trigger_route/full_init_binding/2FA source/name/avatar/ABC owner/B/C/E4/blocker`。`already_authorized` 只表示 A 可用，不得直接进入完整 `success_count`；只有 owner完整成功才完成当前 item。

前端所有approve/reconcile/cancel/candidate/email/assume-owner动作绑定`batch_id + item_id + full_init_id + action + request_seq + expected_version`；切换批次/账号、关闭抽屉或再次提交后，旧响应不得覆盖当前状态。candidate/email输入在请求结束或离开页面时立即清空，不进入持久store、埋点、错误上报或重放缓存。

## 15. QA 验收

### 15.1 E1/E2

1. fixed 2FA 未配置、不可解密、版本漂移时 Telegram 改密调用数为 0。
2. 接码源密码只有被 Telegram 接受后才进入加密安全快照，且 A 与快照同事务。
3. 固定密码来自租户配置，不来自代码常量；API、日志、提醒和审计无明文。
4. 改密成功后账号快照绑定固定版本；明确失败保留仍有效源密码，已完成资料与 ABC request preparation 不回滚，但新的 B/C/E4 Telegram 调用数为 0。
5. 改密 unknown 后进程重启不产生第二次 mutation。
6. 姓名成功、头像 waiting/failed/unknown 时父 item 不成功。
7. Telegram 姓名和头像指纹不匹配时不完成。
8. 每个账号只有一个 active full-init owner；每个 batch item/generation 恰好一条 trigger binding。
9. `new_account/already_authorized/relogin` 都执行 gap decision；code_receiver/rank_deboost 继续硬拒绝。
10. requester 与 approver 相同、fingerprint/version 漂移均类型化拒绝。
11. full sweep/canary open 时 request 保持 waiting，不创建第二个 open ABC batch。
12. B pre-challenge Timeout 复用原 operation 收口，A 前后摘要完全一致。
13. B/C/E4 任一 manual/unknown 时父 item 不成功。
14. cancel 不覆盖 started 远端 operation；retry 不重放 unknown。
15. 200 行并发批次中 phone、UUID、account、post-init、profile item 和 ABC request 无串号。
16. 崩溃矩阵覆盖 A+secret 同事务、2FA started/commit、profile started/readback、ABC materialize、B/C/E4 和通知提交前后。
17. already-authorized 必须使用本行 fresh A probe后 create-or-attach；数据库 ACTIVE 不能直接完成。
18. new/already-authorized/relogin 在相同缺口下绑定同一 owner，重复触发不创建第二套 operation。
19. `unchanged` 无远端校验证据时不计 fixed；密码错误和 RPC unknown 都不产生第二次 mutation。
20. open/terminal-success/manual/unknown/multiple-owner ABC 分类分别 attach/readback/reconcile/block，不重复登录。
21. 已有姓名/头像远端证据只读回；单一缺口只修改对应动作，非目标账号零变化。
22. succeeded owner evidence漂移时只创建更高 generation并修实际缺口；未重新进入批量登录的账号零变化。
23. A/probe提交前后崩溃不会留下永久 `authorization_confirmed + binding missing`；invariant reconciler只修新策略 item且零 Telegram调用。
24. legacy `create` 读取映射为 `new_account`，新 writer/API/审计不产生第四条 route；跨tenant/多alias解析歧义零写入拒绝。
25. 2FA missing、真实rotation confirmed、受保护 fixed验证、already-fixed分别能完成；仅 enabled、本地密文相等和无证据 unchanged均不能完成。
26. current candidate错误立即销毁，email确认/候选RPC unknown保留原operation；刷新接码binding不能替换started/unknown secret。
27. compatible 2FA/profile owner可attach，不兼容或unknown owner阻断；旧profile wrapper不得附带username/voice-profile副作用。
28. active owner策略不兼容、共享binding取消、最后binding取消、账号删除/封禁/用途或A generation漂移均覆盖调用前、started和unknown三类边界。
29. 新策略逐行和批次计数满足 `authorized >= fully_initialized = success`；授权阶段提醒不终结batch，manual/unknown correction保持守恒。
30. required capability off时新batch零写入；运行中关闭、tenant fairness、orphan binding、duplicate owner和secret cleanup overdue均有确定性状态/指标。
31. 外部非空资料无同policy evidence仍进入gap；人工保护/preview后改值阻断且非目标资料零变化。
32. 本账号ABC owner与同tenant其他global open batch分别命中attach和waiting-global，不会错绑或创建第二个owner。
33. Telegram资料成功但本地提交unknown时按原profile item前滚；平台display/avatar投影与远端姓名/头像任一不一致均不计完成。
34. normal行不能关闭full-init；preview后policy/capability/pool/binding漂移和同batch重复account都零写入冲突。
35. legacy password ciphertext无provenance只作candidate；tenant fixed version安全回填、独立post-init三态mode和前端stale-response/secret清理通过。

### 15.2 UI/合同 QA

- precheck 明示新策略、固定 2FA 已配置与版本、资料策略版本、eligible/excluded 数量，不回显密码。
- 每行阶段、blocker、异人批准和 global queue 状态可读；“初始化详情”接口和弹窗只返回状态、ID与 evidence-present 布尔值，不返回密码、ciphertext、Session、AuthKey或接码地址。
- `authorized_count` 不冒充 `success_count`，required 缺口不进入绿色完成提醒。
- API schema、权限中间件、Task Center 投影、initial/correction 去重和 200 行详情通过。
- current-candidate/email-confirmation请求的access log、异常、AuditLog和响应均无敏感值；权限撤销/显式接管、expected-version冲突和跨tenant访问通过。

### 15.2 profile / ABC / 2FA 最终设置顺序回归

1. 新 owner 从 `profile` 开始；资料远端读回成功前 2FA mutation 与 B/C/E4 调用数均为 0。
2. `two_fa_status=reset_eligibility_waiting|reset_waiting` 且 server due 在未来时，owner 仍可领取 profile；`two_fa_next_retry_at` 与 Telegram 日期精确保持，通用 `next_retry_at` 只调度当前 stage。
3. 批量登录中 Telegram 已接受的 2FA 密码必须加密进入账号安全快照并绑定 A generation；ABC B 登录读取该快照，fixed rotation 成功后再由平台固定密码覆盖。
4. profile 完成后先验证既有 ABC evidence；账号未启用 2FA或可信当前密码可用时，同一 request 进入审批并先完成真实 B/C/E4，然后 owner 才进入 fixed 2FA。
5. 只有当前 2FA 已启用且密码未知时，缺少 B/C 的 request 才保持 `waiting_prerequisite`、零 B challenge；owner 回到 `two_fa` 并等待原 server due。
6. reset/fixed 完成后，同一 prerequisite request 转为 `waiting_approval`；重新 preview 和异人批准后创建 `post_login_exact` batch。
7. 只有 profile、fixed 2FA、B/C/E4 三组 evidence 全部成功才投影 owner/父 item成功；request preparation 不能增加 `fully_initialized_count`。
8. migration 对已有 `reset_waiting` owner只复制 server due到专用列并把未完成 profile 的当前 stage改为 profile；不取消、不重发 reset，不创建 ABC operation。

## 16. Release Gate 与生产 E4

发布仍走 `master -> release -> GitHub Actions Deploy Production`。Release Gate 必须验证候选 SHA、migration、mode、权限、worker capability、ABC 全局门禁、secret-free 日志和定向自动化；CI/deploy/health 只算运行证据。

生产先选择 1 个低风险、明确批准的新 normal 账号做首个 E4，独立读回：

1. 部署 SHA 与候选一致。
2. 租户固定 2FA `configured=true`、版本正确，服务端等值检查通过且不输出明文。
3. A 使用接码源密码真实登录，源密码加密快照存在。
4. Telegram 2FA 真实切换，后续 B 登录能使用固定密码完成。
5. Telegram 姓名和头像指纹匹配。
6. A/B/C 的 App、AuthKey、非零 authorization hash 两两不同；B 在 SV，C 在 MY。
7. C 的 MY 本地+SV SSH 双副本、恢复密钥、inventory、restore probe 完整，MY client=0。
8. A Saved Messages E4 有非空 remote message ID。
9. 父 item、子状态、批次计数、提醒和审计一致，A Session/current/三组 generation 无漂移。

只有以上事实全部通过才写 `production_fixed`；发布、容器健康或本地测试不能替代。

重触发另做两个精确 E4：一个此前已登录但缺少 2FA/profile/ABC 的账号再次进入批量登录并命中 `already_authorized`，证明不重登 A但完整补齐；一个需要 `relogin` 的账号证明新 A 完成后绑定同一 full-init合同。随后同一完整账号再次触发，证明 `already_complete` 全程零 mutation。任一 unknown 先收口原 operation，不扩大批次。

生产负向 canary还要证明：2FA enabled但无可信current时只进入人工、同账号双批并发只产生一个owner、授权完成提醒不冒充成功、关闭capability后不新增副作用、未重新进入批量登录的账号零变化；重新开启后从原owner续接而非重放。

## 17. Product Design Complete

- [x] 覆盖原始需求和最新本地提交影响。
- [x] 覆盖适用范围、前端状态、后端/API、worker 和数据流。
- [x] 覆盖 2FA 敏感数据、权限、异人审批和审计。
- [x] 覆盖并发、幂等、unknown、迁移、mode、回滚和 Release Gate。
- [x] 覆盖自动化 QA 与单账号生产 E4。
- [x] 覆盖 already-authorized/relogin 重触发、无 current 2FA、full-init/ABC owner 去重和零重复 mutation E4。
- [x] 覆盖 already-authorized/relogin 利旧 current A Session 接收 B/C 官方验证码、A 零重登/零替换、ABC 不回退导入接码 URL，以及 code 后仍服从 current-2FA/reset 的协议边界。
- [x] 覆盖原子binding/orphan恢复、canonical route、父批次长等待计数、共享owner取消、账号生命周期、策略/权限/capability漂移和敏感secret清理。
- [x] 无 silent fallback、mock success 或健康即完成声明。

正式状态：`design_status=complete`、`dev_handoff_ready=true`、`implementation_status=implemented_local`、`qa_status=targeted_local_passed`、`release_status=not_started`、`production_status=unproven`。

## 18. Product Handoff

- `message_id`: `product-account-batch-post-login-full-init-20260826`
- `dev_goal`: 写失败测试复现“2FA 只使用不记录/不固定、资料只入队、登录提前成功、无 ABC request，以及 already-authorized/relogin 重触发不补缺口”，再实现账号级 durable coordinator和 trigger binding。
- `required_modules`: 批量登录 canonical三route/finalize与计数、原子full-init binding/orphan reconcile、真实2FA rotation/候选/邮箱确认、exact profile child readback、post-login coordinator、ABC owner resolution/request/`post_login_exact`、生命周期/权限/capability fence、API/UI/提醒、additive migration。
- `reuse_required`: 现有 batch flow fence、账号安全批次、素材缓存、ABC global supervisor，以及 `fde29376` 的 B pre-challenge unknown 收口。
- `forbidden`: 不硬编码固定密码；不把 import confirm 当 ABC approve；不在 login worker 内联 B/C/MY；不把资料入队、CI、部署或 health 写成完成。
- `qa_gate`: 后端每组测试 60 秒硬超时；先 E1/E2，再产品验收；生产最后由 prod-diagnosis 读回 E4。
- `release_gate`: L3，必须有数据库 migration/rollback、runtime mode、异人审批、单账号 canary 和远端事实读回。

## 19. 2026-08-26 本地实现与 QA 回读

- migration `0168_post_login_full_init` 已增加账号级 owner、登录代次 binding、ABC request、固定 2FA 证据和父批次投影；`0169_post_login_stage_order` 再增加独立 2FA 服务端到期字段并迁移存量等待账号到资料优先流程；同批同账号增加非空 partial unique index。
- `new_account/relogin` 在新 A 持久化事务写入短期加密 2FA 来源与 `waiting_login_parent` 义务；`already_authorized` 必须经本行 fresh probe，三条 route 在在线读回后统一进入 `post_initialization_waiting`。
- coordinator 已接入租户固定 2FA 真实 mutation、可信证据落库、姓名头像平台+Telegram 双读回、现有 ABC owner 分类和无 owner 单账号异人审批；完整 A/B/C/E4 前父行不成功。
- `post_login_exact` 复用现有 online ABC runner：B/C operation 把 current A authorization 冻结为 `code_source_authorization_id`，通过 A Session 的 Telegram `777000` 服务消息按 challenge 时间和 message ID 取码；导入接码 URL 不进入该链，A 不重登也不改变。
- `authorization_online_abc_supervisor.py` 已接入第三条 `post_login_exact` lane：full sweep/deferred均空闲后自动领取批准或可安全续接的 running exact batch；合同错误显式停止，unknown不重放，通用手工 runner拒绝该 selection mode。
- 同 item 重放复用原 binding；同账号新 batch 复用兼容 active owner，历史 succeeded owner只作为 predecessor，必须新建本次 gap-decision generation并重新读取当前 2FA/profile/ABC，不能直接投影成功；只有最新 generation 的 terminal failed/manual/unknown继续绑定原债务和原 operation，较旧债务不得覆盖较新成功事实，unknown只读回或显式对账而不重放 mutation。
- 前端已区分已授权、完整初始化、等待与人工状态，并提供当前 batch 精确过滤的 ABC request、冻结预览和 `system.manage` 审批入口；API/日志不返回临时 2FA 或固定密码。
- profile以冻结的历史目标逐项判缺口，姓名匹配时只补头像、头像匹配时只补姓名、两项都匹配时零 mutation；normal full-init的最终 A 在线读回是硬门禁，不能再用 warning 把未证实 A 放入后置链。
- `reconcile_unknown`、current-2FA candidate、恢复邮箱验证码和 execution-owner 接管均在原 owner 上收口；pre-mutation 2FA读回失败、profile和ABC terminal阶段提供同owner安全重检，当前2FA未知仍必须走candidate而不能通用重试。恢复邮箱确认后必须再次读回 `enabled`，远端仍为 missing或结果不明时继续人工/unknown，不得伪造成功或重放整条登录。
- 父批次增加 `completed_with_manual/completed_with_failures`，人工恢复期间回到 `running`并持续轮询，最终以 resolution correction更新；后置失败从后端也禁止整条登录 retry。登录/目标分组本身的失败通过独立 `post_initialization_failure_type` 与后置失败区分，后置随后成功不得覆盖 `pool_transition_failed` 等原失败。
- batch precheck同时要求已审核头像素材、三个互不重复且凭据版本匹配的 active ABC Developer App角色，并把 fixed-2FA/profile/素材/ABC assignment版本纳入冻结指纹；任一依赖变化都要求重新预检。
- 本地后端280项定向回归（含 ABC批准后自动执行、running续接、release合同失败零远端重放及本节业务闭环）、SQLAlchemy metadata/单 head、模块编译和前端 production build 已通过；依赖外部 PostgreSQL fixture 的 blank-DB migration 测试再次在本机 60 秒硬超时内停留于`0001_initial`，因此保留在 Release Gate，不能据此声明发布或生产修复。

## 20. 2026-08-27 Code Review Bug Batch Plan

本轮代码审查确认前述实现仍有 11 个业务缺陷，原 `targeted_local_passed` 不能继续作为当前 QA 状态。缺陷按根因合并处理，不逐条引入互相冲突的补丁：

1. `schema/bootstrap`：`0001_initial` 使用当前 metadata 建库时，`0168` 仍无条件新增同名列、表和索引，空库升级可能重复创建。
2. `evidence correctness`：首次资料初始化的空 override跳过姓名生成；2FA `unchanged` 被当作 fixed evidence；明确的旧密码错误被归入 remote unknown。
3. `recovery durability`：TTL 会清除 started/unknown/email 未决 operation 所需源密码；`reconcile_only` 领取不到显式 reconcile owner；邮箱确认远端成功后的本地提交异常没有独立 unknown 收口。
4. `generation/projection`：重登新 A 会复用不兼容的旧 terminal debt；人工修复形成新的 failed/manual/unknown 后，父 item 和批次仍保留旧终态统计。
5. `idempotency/UI`：单账号 ABC batch 与 request approval 分事务且既有 key 不校验 frozen facts；终态批次人工重开后前端轮询不会重新启动。

本批修复必须先增加失败测试，再满足以下验收：空库和已升级库均可执行 migration；首次初始化生成并冻结新姓名；只有真实 rotation 或受保护校验能确认 fixed 2FA；pre-mutation 明确失败可提交新候选而 unknown 不重放；回滚 `reconcile_only` 能收口原 operation；新 A generation 不被旧债务阻断且旧 unknown 证据不丢失；父批次与 owner 最新终态守恒；ABC request/batch/fingerprint 原子一致；人工操作后页面持续轮询到新终态。

## 21. 2026-08-27 审查修复实施与 QA 回读

- `schema/bootstrap`：`0168` 改为按实际 schema 独立检查列、表、索引和结构等价外键；同时覆盖当前 metadata 已建全部对象和“表存在但索引缺失”的中断态。
- `evidence correctness`：首次 profile 不再传空 override，保留冻结目标的精确补偿；2FA `unchanged` 改为未证实人工状态，`PasswordHashInvalidError` 改为明确 pre-mutation 失败，不再污染 remote unknown。
- `recovery durability`：`started/unknown/email` 未决 operation 的 source secret 保留到收口；`reconcile_only` 可领取显式 reconcile owner；邮箱确认在远端成功后遇本地持久化失败会回滚并落入原 owner unknown，不重放远端 mutation。
- `generation/projection`：只有当前 A 与冻结策略兼容时才续接 terminal debt；新 A 创建新 generation 且保留旧债务证据；属于 post-init 的新终态会重新投影父 item 和批次计数，不覆盖独立登录或分组失败。
- `idempotency/UI`：post-login ABC batch 与 request approval 由顶层单事务提交，复用已有 key 前校验选择模式、账号、fingerprint、release SHA 和审批事实；前端人工恢复后通过 poll revision 重启详情轮询。
- 失败基线精确命中 `15 failed, 38 passed`；修复后新增与相邻合同 `53 passed in 6.31s`，post-login/batch-login 扩大回归 `98 passed in 10.55s`，online ABC 全组 `213 passed in 38.73s`，相邻 AI/deploy/dependency/shard/frontend permission `188 passed in 3.90s`。
- Python compileall、相关文件/函数规模检查、`git diff --check` 和前端 `tsc + vite production build` 均通过。本地 SQLite current-metadata 迁移回归已证明本轮重复对象修复；外部 PostgreSQL blank-DB 全链仍在 `0001_initial` 超过60秒，因此仍是 Release Gate 未证明项。
- 本轮仅完成本地实施与定向 QA；未提交、未发布、未变更生产，`release_status=not_started`、`production_status=unproven`。

## 22. 2026-08-27 生产 exact-two canary 补充缺陷

- 生产只读预检确认两个目标都是 `already_authorized`：A Session 可权威读取，远端 2FA 均为 enabled，但平台没有可信 current-2FA 证据，profile 与 ABC 义务也没有完成。
- 两个历史接码页均明确返回凭据已失效。当前实现把这个确定的来源终态抛成 `two_fa_source_resolution_failed`，只能通用重检，无法进入 PRD 已定义的 current-2FA candidate 人工闭环；反复重检不会改变结果。
- 修复口径：仅把接码平台明确的 `url_error` 分类为 `two_fa_current_password_unavailable/manual_required`，允许在原 owner 上提交短期加密候选；DNS、超时、解析合同变化等依赖故障仍保留 `two_fa_source_resolution_failed`，不得静默降级或误要求候选。
- 验收：新增测试同时证明明确失效进入 candidate 路径、瞬时 fetch 失败仍进入安全重检路径；随后重新发布并由这两个 `already_authorized` 账号完成 fixed-2FA、姓名/头像与 ABC 全部远端读回。

## 23. 2026-08-27 account-login worker fresh-import 缺陷

- exact-two batch 已持久化但仍在 `pending/prepare` 时，生产 account-login worker 的 lazy role loader fresh-import post-login drain，触发 `account_post_login_init.__init__ -> binding/policy -> account_login.__init__ -> batches -> partial policy` 循环依赖；每轮在领取 item 前失败。
- 远端边界：两个 item 的 send/code/twofa call state 均为 `none`，authorization 未确认、full-init owner/binding 未创建，因此本缺陷只形成持久队列阻塞，没有 Telegram unknown 或部分完成。
- 修复口径：post-login 与 account-login package 均不再 eager re-export 全部实现模块；唯一依赖聚合导出的 API 路由改为显式子模块入口，生产 worker 与测试也继续使用显式入口。`from package import submodule` 保持 Python 标准行为。不得通过吞掉 ImportError、跳过 post-init drain 或伪造 worker healthy 解决。
- 验收：worker-role 全部 lazy target 在独立进程可导入；post-login/batch-login 定向回归通过；新 release 后从原 batch/item 继续，不能新建第二批或重放登录。

## 24. 2026-08-27 历史 2FA 无恢复方式的 durable reset 合同

- exact-two 原 owner 已证明：租户固定密码作为 current candidate 被 Telegram 在 mutation 前明确拒绝；两个账号均无恢复邮箱、无 hint、无既有 reset 等待期，历史接码页也不可恢复。此时不能伪造 fixed evidence，也不能继续猜测密码。
- 唯一允许的自动恢复是 Telegram 已登录 Session 的官方 password reset：操作员必须在原 `manual_required` owner 上显式请求；平台持久化审计、原 owner、reset request call fence 与 Telegram 返回的 `until_date/retry_date`，不得自动扫描历史账号发起 reset。
- Telegram 可能先以 `PASSWORD_TOO_FRESH_%d` / `SESSION_TOO_FRESH_%d` 拒绝 reset RPC；平台必须把服务端秒数换算为绝对日期并记录 `reset_eligibility_waiting`，此时远端 reset 尚未创建，不能标记 `remote_mutation_started`。资格到期后再调用 reset；若返回 `resetPasswordRequestedWait(until_date)`，转为 `reset_waiting`。两个等待阶段都保持原 owner，父批次回到 running/waiting，`next_retry_at` 到期前 worker 零 Telegram 调用；已创建的 reset 可在系统消息侧撤销，撤销或服务端延长等待时按新的 retry date 续接。
- terminal manual/unknown owner 被同一专项操作重新打开后，父 item 必须从仅由后置初始化投影出的 `failed/unresolved` 恢复为 `post_initialization_waiting`，清除对应后置失败投影并重新计数；不得出现父批次同时 `running + waiting` 又保留同一条目的 `failed_count`。登录、目标分组等独立失败仍原样保留，不得借后置恢复擦除。
- eligibility/reset waiting 期间同一 owner先完成 profile；若 reset 已经发起就代表当前密码未知，只能对 ABC 做 evidence readback或创建 `waiting_prerequisite` request，不得物化新的 B challenge。各自到期后同一 owner继续对应 RPC；只有 Telegram 返回 `resetPasswordOk` 或权威读回 2FA missing 才设置租户固定密码，然后激活原 ABC request。reset RPC 或本地提交 unknown 必须先用 `account.getPassword.pending_reset_date` 对账，禁止直接重放。
- API/UI 仅在 `two_fa_current_password_unavailable/two_fa_manual_required` 暴露“发起 2FA 重置”，要求 `accounts.security.credential_manage`、expected version 与原因；等待时间显示 Telegram 返回的绝对日期，不写死 24 小时或 7 天，并明确系统通知及可撤销影响。不得回显密码、Session、邮箱或账号标识。
- 验收：首次请求只产生一个远端 reset 与确定等待时间；等待期 reset 零调用但 profile 可完成、ABC request 可准备；到期 `resetPasswordOk -> 2FA missing -> fixed password set -> fixed evidence -> 原 ABC request审批/执行`；进程崩溃/超时可由读回收口；父 item/batch 在等待、完成和失败状态下计数守恒；exact-two 必须继续原 owner/batch。
# 2026-09-05：单账号 ABC 未知结果隔离

批量登录的 `post_login_exact` 批次发生 B/C/E4 结果未知并停止后，可通过独立 preview/apply 入口隔离该账号，继续其他已批准范围内的账号。该入口只接受一个账号、一个 stopped item、原请求/批次审批一致、A 冻结事实未漂移、无 owner/有效 lease、runtime off/scope 空、MY client=0、没有其他活动敏感操作的状态。preview 固定批次/item/operation/slot/A 版本、原执行版本和当前运行版本，apply 逐项重验并审计。

隔离保留同一 operation 的 `remote_call_state=unknown`，只转为 `deferred_reconcile / quarantined`，item 为 deferred_reconcile，单账号批次为 completed_with_exceptions。ABC 请求仍为 reconcile_unknown，不能计为成功；不重发消息、不重新登录、不改 A/B/C Session 或原执行版本。与原批次不同的当前运行版本只用于无远端写入的隔离审计，不授予旧批次重跑权限。同账号后续恢复仍需对账，不能因全局 unknown 门槛释放而自动重试。

验收必须覆盖：原 A/B/C 不变、原未知 operation 保留、另一敏感操作/活动租约/A 漂移/错误审批/错误账号或非单账号批次/指纹变化均零写入、同 key 重复 apply 不产生第二次审计、不触发 Gateway。

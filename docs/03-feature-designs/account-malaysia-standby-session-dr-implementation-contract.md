# 马来西亚异地备用 TG Session 实施与验收合同

> 版本：v2.24
> 日期口径：2026-08-23（Asia/Shanghai）
> 规范关系：本文是 [马来西亚异地备用 TG Session 灾备 PRD](account-malaysia-standby-session-dr-prd.md) 的强制组成部分；冲突时两份文档必须同步修订，不允许实现自行择一。
> 当前状态：`design_status=complete`、`product_resync_status=complete`、`dev_handoff_ready=true`、`implemented_scope=ten_account_canary_ready_for_primary_send_acceptance_plus_p0_local_recovery_and_full_online_abc_rolling_runner_deployed`、`core_deployed=50f657c03c6ad0ef60b75eced1bf1390516ce290`、`ssh_mirror_deployed=true`、`slot_canary=2/2_historical_pass`、`ten_account_acceptance_gate=primary_stable_plus_saved_message_remote_id`、`fixed_observation_duration=removed`、`p0_local_recovery=2/2_verified`、`approved_batch_runner=production_verified_run_and_status`、`full_online_abc_design=complete`、`full_online_abc_implementation=account_10_stopped_post_b_pre_c_pending_v2_24_release`、`full_prd_implementation=partial`、`runtime_mode=off`、`production_fixed=false`

> 两账号执行合同：一个账号一个 preview/fingerprint/idempotency/approval。B 的 `provision_standby_1` 与 C 的 `migrate_standby_2` 均冻结 canonical A 为唯一 code source；B 完成后重新读回 A，才可创建 C operation。C runtime 必须绑定 exact operation + capability `2.21-abc-a-source` + exact release SHA，成功自动关回 `off`。备份失败只允许 `failed|manual_required|reconcile_unknown`，不得修改或撤销 A；第一个账号未完成 Telegram 登录与 Saved Messages 发送 E4 时禁止第二个，两个账号未全部通过时禁止 10 账号。

> 批次执行入口合同：GitHub Actions 仅发布代码，不执行账号登录。生产 current release 必须提供 `deploy/authorization-online-abc-runner.sh --mode status|run --batch-id ...`；`run` 只消费已审批的 `TgAuthorizationOnlineAbcBatch`，从 item 冻结事实和确定性 key 驱动既有 canonical qualification、`apply_abc_backup/prepare_scoped_c_migration`、`apply_abc_e4`、`sync_online_abc_batch`，不得复制登录业务逻辑。runner 单账号串行，C 未终态时只轮询原 operation；已成功阶段不重放。任何 A 漂移、runtime scope 冲突、`provision_reconcile_unknown|reconcile_unknown|manual_required|failed` 立即非零退出并保持原事实，禁止自动换账号。相同 batch ID 重启必须从数据库阶段恢复；不得依赖 GitHub Actions workflow_dispatch、操作者手抄 MY image SHA 或临时 Python/SQL。

> create-only bundle 冲突恢复合同：generation 分配同时扫描 authorization、历史 C operation 和中心 bundle，避免已收口旧工件占用同代际路径。若旧版本已在远端登录确认后因本地不可变路径冲突进入 `provision_reconcile_unknown`，使用专用 preview/apply 从冻结 A 读取 Telegram 授权集合，只允许唯一匹配 operation App C、login 时间窗且不属于已登记 authorization 的设备；apply 精确撤销该 hash 并二次读回缺失，持久化 compensated/audit 后，runner 才能以 retry key 和更高 generation 恢复原 item。历史 bundle 原地保留；候选不唯一或远端读回不确定时不改变 unknown。

> runner checkpoint 恢复合同：C succeeded 后、E4 operation 创建前若唯一失败为 `malaysia_wake_unavailable`，新 runner 必须等待 MY ready heartbeat 刷新，不得创建远端 unknown。历史 stopped 项仅可由 `--mode resume` 恢复，且必须同时满足原 requester/approver/ref、唯一 `runner_blocked` item、blocker 精确为 `malaysia_wake_unavailable`、B/C operations succeeded、E4 operation 不存在、A frozen/qualified facts 不漂移、runtime off/scope 空、global unknown=0、MY ready/client=0；CAS 恢复原 item 为 running 后调用普通 runner，不修改 B/C 事实或 idempotency key。批次 manifest 的 `deployed_release_sha` 永久保留原冻结 SHA；若修复后 current release 已变化，只允许在上述精确断点把独立的 `execution_release_sha` 从原执行 SHA 一次性审计切换到 current SHA，后续普通 runner 继续强制 current=`execution_release_sha`。其他 blocker、E4 已创建或任一事实不一致一律拒绝。
> pre-primary 恢复合同：canonical A 是 current 业务角色，不等于持久物理槽名；合法 `local_activate` 后 current authorization 可保持 `logical_slot=standby_1`。qualification 只接受 current/slot-current、SV、账号 Session/App、冻结 authorization/fact/connection generation 全一致的 `primary|standby_1`。旧 runner 因该硬编码在远端动作前落成 `runner_blocked + ValueError` 时，`resume` 仅可在 B/C/E4 operation 全不存在、A `_primary_state=frozen`、canonical preview 成功、runtime off、global unknown=0、原审批一致时恢复原 item；审计 checkpoint=`pre_primary_no_remote_effect`。这不是通用 ValueError 重试入口，任一已创建 operation 或 A 漂移均拒绝。

> current A `hash=0` 依赖合同：当前 Session 从 Telegram 读取自身设备时返回 `hash=0` 是合法协议事实，但不能满足平台设备归属。若 frozen A 已有 UID/AuthKey、B 计划为 provision 且尚无 B operation，runner 先复核 A current/Session/App/三组 generation/UID/AuthKey，再用该 A 作为唯一验证码源完成 B；B 成功后立即由 B 的独立 Session 读取设备集，以 A 设备指纹唯一解析非零 hash，然后才提交 A qualification。B 自身 hash、历史 invalid repair hash、App/IP/设备名均不能替代 A hash。B 未成功、peer 零匹配/多匹配或 A 漂移时停在当前 item，C/E4 不得创建，A Session/current pointer/App/proxy/authorization generation/connection generation 均保持不变。
> 缺失 A UID/AuthKey 的 bootstrap 合同：仅 runner 可在 B preview/apply 上显式开启 `bootstrap_missing_primary_identity`。入口先以冻结 A Session/App/proxy做只读 identity probe，把 UID/AuthKey 摘要写入 preview fingerprint 和 B operation expected facts，不写 A 行、不增 fact version；普通 preview 默认关闭并保持 DB-only。B code-source fence 接受“本地 UID/AuthKey 同时为空、operation expected 两者非空”的精确 legacy 状态，但仍强制 current/Session/App/authorization/fact/connection generation 全一致；任一单字段已有且与探测不一致立即失败。B candidate 必须同 expected UID、不同 expected AuthKey；成功后通过 B observer 完整 qualification A，A fact generation 只增加一次。

> A/B 互补物理槽与登录前滚合同：current A 物理 `primary` 对应 B 物理 `standby_1`；current A 物理 `standby_1` 对应 B 物理 `primary`。B 始终保持业务 role=`standby_1`、non-current、SV、独立 App/AuthKey；所有 B resolver 使用 current A 之外的互补物理槽，不能只查 `logical_slot=standby_1`。新登录提交前只清除互补目标槽的非 current 历史行 `is_slot_current`，current A 命中任何冲突条件都必须失败而不是降级。历史 `IntegrityError` unknown 只允许 `sv_login_recovery` 复用已授权的原临时 Session，冻结 operation/flow/A/互补冲突行/远端集合/image SHA，以异人审批和 CAS 创建 generation+1 B；事务后 A 全字段不变，禁止重新登录。
> B reconcile 后 runner 恢复合同：只接受原 item outcome=`reconcile_unknown`、同一 B operation 已由正式 case 前滚为 `succeeded/reconcile_status=applied`、candidate 是 current A 之外的互补 SV healthy/non-current 槽且 UID 相同/AuthKey 不同、C/E4 均不存在、A `_primary_state=frozen`、canonical preview 不漂移、runtime off/global unknown=0 和原审批一致。checkpoint 固定为 `post_b_reconciled_pre_primary`；恢复后复用同 B operation，禁止重放登录，并先完成 A qualification。
> B succeeded 后、C 创建前的 App 轮换恢复合同：历史 A 若使用默认 App C，runner 不得改写 A 或重复 B。source-less C 从 `primary_sv|standby_1_sv|standby_2_my` 三条 active assignment 的三套 App 中排除冻结 A 与已成功 B，必须恰好剩余一套 active、credentials/version 一致的 App，并把其 purpose/App/version/credentials version 写入 operation fingerprint；MY claim 按 operation 的 App 反查同一 active assignment version，不再错误固定比较 `standby_2_my`。旧 runner 已以 `sv_redundancy_incomplete` 停在 B succeeded、C/E4 absent 时，`resume` 仅在 B candidate 仍为互补 SV healthy/non-current、同 UID/不同 AuthKey/不同 App，A 仅发生一次 qualification fact 增量且其他 generation/Session/current 不变、runtime off/global unknown=0、原审批一致时恢复同一 item，checkpoint=`post_b_pre_c`。任何 C/E4 已存在、B/A 漂移或剩余 App 不唯一均拒绝。

> 历史“结构健康 B”身份补齐合同：全量 manifest 中 `standby_1_plan=already_qualified` 只表示冻结时 B 的 SV 槽位、Session、App 和健康结构存在，不能代替 UID/AuthKey/非零设备 hash 资格。runner 在创建 C 之前必须对缺少任一身份事实的既有 B 执行受控补齐：先冻结 A 的 current/Session/App/三组 generation 和 B 的槽位/Session/fact，再以 B 连接 Telegram 读 UID/AuthKey，并仅由冻结 A 作为 peer observer 解析 B 的非零 hash。只有 UID 与 A 相同、AuthKey 与 A 不同、前后冻结指纹完全不变时，才能将摘要和健康时间写回 B，仅增加 B fact version；禁止改 A、新建 B、重放 C 或撤销任何设备。旧 runner 已在 C succeeded、E4 absent 后以 `sv_redundancy_incomplete` 停止时，`resume` 必须额外验证 B plan=already_qualified、C 原 operation succeeded、A qualification-only、runtime off/global unknown=0 和原审批，再从 `post_c_pre_existing_b_qualification` 恢复；不得误入 post-B/pre-C 分支或重放 C。

> runner 生产读回：GitHub Actions run `32576826536` 已发布 release `a6481e0ae8bd851718e91eb1d6cafd1c6f74d154`；backend healthy、Alembic head=`0163_local_activate_verify`。对旧 stopped batch `03456532-1c1c-4446-bb80-dbc9e5bf9618` 仅执行 `status`，返回 `next_action=stopped`、B/C `10/10`、守恒有效；未执行 `run`。独立读回为 runtime=`off`、claim scope 为空、global unknown=0、open ABC batch=0、MY active client=0。

> 全量滚动实现读回：release `cd06f75ca200552d621507f492629acd721a808f`、Alembic head=`0165_online_abc_full` 已部署 frozen-N manifest preview/apply、健康 B/C readback、旧 SV C migrate、source-less C/MY provision 和 `runner --max-accounts 10`。当前 batch `718657f1-6582-45e7-b0aa-40a4ea1bda3c` 为 B/C/E4 `10/10 observing`；逐账号 A 仍为原 current 且 active/healthy，10/10 Saved Messages 均有远端 ID，C 均为 MY current、双副本、KMS verified、restore probe passed/authorized。用户已取消固定 24 小时等待；v2.23 accept gate 发布后只需即时复核上述 A 与发送事实及 runtime/unknown/MY client 门槛。

> B 验证码/2FA 顺序合同：有托管 2FA 密码不代表可跳过验证码。首次 finish 必须先用冻结临时 Session、phone-code-hash 和本次 A-bound code 调用 code sign-in；仅在 Telegram 返回 `SessionPasswordNeeded` 后，于同一 client 提交 2FA。已有 post-code 临时 Session 的独立 2FA 请求才允许只提交密码。历史 `password_2fa_preceded_code_v1` 缺陷导致的 `AuthKeyUnregisteredError` 必须通过 operation-scoped reconcile case、异人审批和 flow-state fingerprint 收口为 confirmed-no-effect；不得直接改状态后重试。

> 生产结构纠偏：A/B/C 是环境级三套 App 注册和新账号默认角色，不是历史切换后每个账号不可变化的角色标签。单账号验收以三 App ID 两两不同为准；已有 SV `standby_2` 迁移冻结原 App，source-less C 则使用冻结 A/B 后唯一剩余 App。历史 App A `standby_repair` 必须经双 Session Telegram UID/AuthKey 探测和 CAS 转正为 SV `standby_1` 后，账号才能进入迁移。

> 当前设备 hash 合同：Telegram 从当前 Session 读取设备时允许返回 `hash=0`，不得判定登录失败，也不得把 `0` 保存为受保护设备标识。MY 必须提交当前设备规范化指纹摘要；SV 只用保留的 peer Session 读取结果解析唯一非零 hash。唯一匹配前不得写 Bundle/slot commit；零匹配、多匹配或 peer 读取失败统一进入 `provision_reconcile_unknown`，且不得自动重登。

> 生产实施读回：账号 27、28 的 MY generation 2 槽位均为 current，双副本为 `2/2`，隔离 restore probe passed，旧 SV App C Session 为 retained/protected，因此 `slot_canary=2/2_pass`。batch `03456532-1c1c-4446-bb80-dbc9e5bf9618` 初始 B/C/E4 为 `10/10`，随后因账号 8、11 的 A AuthKey duplicate 判观察失败；正式 `sync` 已把两项投影为 `primary_drift_after_success` 并停止批次。修复 release SHA `0ec48547fc5748f095724ac4d2da363b1d6364e5`、Alembic head `0163_local_activate_verify` 已生产读回；账号 8/11 分别切到 B 授权 2814/2818，并取得 Saved Messages message ID 86/396 和新鲜 online probe，旧 A 13/19 保持 invalid/needs_repair/protected。runtime=`off`、global unknown=0、MY active client=0。271 项历史批次已守恒为 `241 succeeded / 23 failed / 7 manual_required / 0 reconcile_unknown = 271`，其中 22 个不同账号有 `phone_number_banned` typed fact。全量动态 `complete_online_abc` 的 CLI/SSH frozen-N 滚动执行切片现已部署，但自动故障触发、`restore_sv_pair`、`drill_wake`、紧急主授权重建、中心恢复对账、decommission/erase、跨全部消费者的运行代次 fence、API/UI 与全量账号实际执行仍未完成。

> unknown 历史回归事实：24/25 是无包 remote orphan；26 是原 operation/generation 的 MY local-only 包；67 远端没有新设备但当前主 Session 已失效；87 是本地+SSH+inventory 领先中心且当前主 Session 已失效；111 的三条 SV Session 均不可授权，远端集合未证明。上述 open unknown 已收口，但对账协调器合同不变：只能复用原字节、原 operation 和原 generation；不得调用登录 RPC、生成新 Session 或把未证明状态推导成成功。

> unknown 恢复合同：使用 secret-free stage manifest，阶段至少覆盖 `remote_login_started/remote_login_confirmed/local_copy_verified/snapshot_copy_verified/inventory_persisted/central_receipt_committed/restore_probe_passed/slot_committed`。MY 仅在中心签发 operation-scoped reconcile permit 后读取原路径；local-only 只允许 create-only 补第二副本与 inventory，inventory-ahead 只允许重放同 digest receipt。所有 apply 复核 runtime=`off`、MY active client=0、operation/item/source versions、node/image、owner epoch、bundle generation 和 digest。无包 orphan 和远端未证明项只 hold/manual，不存在恢复登录入口。

> 封号投影合同：`PhoneNumberBannedError` 是账号手机号级 typed remote fact。新事实和历史对账 apply 都在同一事务把 `TgAccount.status` 投影为 `已封禁`、online state 投影为 `login_required/phone_number_banned` 并保留授权资产；统计必须分开“本迁移批次 22 个已确认账号”和“全平台已确认封禁账号”，禁止把 22 当作全平台手机号总量。

> 10 账号观察事故合同：批次 `03456532-1c1c-4446-bb80-dbc9e5bf9618` 的 B/C/E4 初始结果均为 10/10，但账号 8、11 的旧 A 在观察期出现权威 `AuthKeyDuplicatedError`，故 canary 最终为 failed。控制账必须通过正式 sync 把已成功 item 的后续 A 漂移投影为 `primary_drift_after_success` 并停止批次；B/C 已成功槽位不得回写失败或删除。

> P0 本地恢复合同：仅对 typed `authorization_key_duplicated` 的旧 A 和从未承载业务的 fresh B，允许 operator `local_activate` 窄切片。preview/apply 维持双 probe、异人审批和 generation CAS；切换后账号保持不可领取、B current+degraded，另一个幂等 verification operation 冻结新 current/generation 并向 Saved Messages 发送。只有远端 message id 成功读回才恢复账号 `在线`；发送失败/unknown 不回切旧 A，不自动重发，并阻止 `restore_sv_pair` 之前宣称三槽健康。

## 1. API 合同

| 接口 | 合同 |
| --- | --- |
| `GET /api/tg-accounts/dr-execution-nodes` | 返回唯一 MY 节点的脱敏能力、版本、heartbeat、固定 `standby_egress_id` 和可领取状态 |
| `GET /api/tg-accounts/dr-egresses?purpose=...` | 返回硅谷 `primary_regular` 与马来西亚 `standby_my` 两个固定出口的脱敏 purpose/region/freshness/status/version；前者是唯一业务出口，后者只允许创建和显式唤起，不返回完整 IP、secret ref 内容或凭据 |
| `GET/POST /api/system/developer-app-failure-domains` | 分页查询或创建 App owner-domain assertion；POST 经 TLS 接收 owner claim、证据引用和 expected App version，由服务端规范化/HMAC 后立即丢弃明文；GET/审计只返回 opaque digest/version |
| `POST /api/system/developer-app-failure-domains/{id}/approve` | 异人批准/拒绝 assertion 并冻结 key/evidence/version；过期或漂移立即使相关 qualification 失效 |
| `GET /api/system/telegram-egress-changes` | 按 tenant/status/type/egress/cursor 分页返回脱敏出口变更及状态计数 |
| `POST /api/system/telegram-egress-changes` | 创建出口 create/update/disable/retire/rotate_hmac 申请；携带脱敏 desired diff、expected egress version 和幂等键，不直接改出口 |
| `POST /api/system/telegram-egress-changes/{id}/approve` | 仅不同于发起人的出口审批人可批准或拒绝，冻结 expected version、assignment/usage fingerprint 和 secret ref version |
| `POST /api/system/telegram-egress-changes/{id}/apply` | 只应用已批准且版本未漂移的变更；服务端写 registry/fingerprint 后执行运行面 readback，失败进入 hold |
| `POST /api/system/telegram-egress-changes/{id}/cancel` | 仅 apply/远端配置副作用开始前按 expected version 取消；否则保持 hold 并 readback |
| `GET /api/system/telegram-egress-changes/{id}` | 返回脱敏 diff、审批、HMAC 回填覆盖、assignment/usage blocker 和 apply/readback，不返回 IP、secret ref 内容或凭据 |
| `POST /api/system/authorization-dr-backfills/preview` | 冻结存量范围和 expected old values，返回分类计数、冲突、target fingerprint 与只读差异 |
| `POST /api/system/authorization-dr-backfills/{id}/approve` | 仅不同于 preview 发起人的迁移审批人可批准或拒绝，冻结 fingerprint、范围、expected old values 和批次版本 |
| `POST /api/system/authorization-dr-backfills/{id}/apply` | 仅按批准 fingerprint、expected versions 和幂等键回填；不伪造缺失远端、MY 或历史 usage 事实 |
| `POST /api/system/authorization-dr-backfills/{id}/cancel` | 仅 `apply_started_at` 为空时按 expected batch version 取消；已开始 apply 必须完成逐项 readback |
| `GET /api/system/authorization-dr-backfills/{id}` | 返回逐类 apply/readback 计数、resolver shadow diff、冲突与 writer-cutover blocker |
| `GET /api/system/authorization-dr-backfills` | 按 tenant/status/fingerprint/created_at/cursor 分页返回迁移批次及状态计数 |
| `POST /api/system/authorization-contract-cutovers/preview` | 冻结目标 mode/epoch、最低 capability、非 stale runtime、活跃 client、Telegram egress ACL，以及 恢复密钥策略 version/digest、期望解密身份集合 digest 和 decrypt-denied 探测计划 |
| `GET /api/system/authorization-contract-cutovers` | 按 status/target_mode/created_at/cursor 分页返回 cutover、实例/恢复密钥/ACL blocker 与状态计数 |
| `POST /api/system/authorization-contract-cutovers/{id}/approve` | 异人批准/拒绝冻结的 cutover fingerprint、expected contract version 和 runtime/恢复密钥/ACL 条件 |
| `POST /api/system/authorization-contract-cutovers/{id}/apply` | 复核实例/旧 client/恢复密钥授权身份 与真实 decrypt-denied/ACL 后 CAS 提升 contract epoch/mode；漂移进入 failed_hold，不部分提交 |
| `POST /api/system/authorization-contract-cutovers/{id}/cancel` | 仅 `apply_started_at` 为空时按 expected version 取消；epoch 已提交只能新建更高 epoch 的降级 operation |
| `GET /api/system/authorization-contract-cutovers/{id}` | 返回 contract、runtime capability、client drain、DB mutation gate、恢复密钥策略/grantee/decrypt-denied fact 与 egress ACL 脱敏 readback |
| `POST /api/tg-accounts/security-batches/precheck` | 供补齐/迁移及 `complete_online_abc` 使用。后者只接受 `selection_mode=all_online_accounts|manual`；全量模式在同一可重复读事务冻结全部未删除 `status=online` 账号为动态 `N`，保存账号 ID 全集及版本 fingerprint，再按本地投影返回 A/B/C 分类和 blocker，不连接 Telegram、不以 A 探测结果缩小 `N`。`action_types` 含 `cleanup_devices` 时返回 422 `cleanup_precheck_not_supported` |
| `POST /api/tg-accounts/security-batches` | 用户确认后调用。`complete_online_abc` 必须携带已批准 precheck ID、`target_set_fingerprint`、`selection_mode`、`action_types=[complete_online_abc]` 和 `Idempotency-Key`，同一事务写恰好 `N` 个账号项及 B/C 各 `N` 个槽位结果；范围或版本漂移零写入返回 409。设备清理继续使用无 precheck 的本地 48 小时分类合同 |
| `GET /api/tg-accounts/security-batches/{id}` | `complete_online_abc` 返回 `N/target_set_fingerprint/primary_probe_counts/account_outcome_counts/standby_1_outcome_counts/standby_2_outcome_counts/coverage_numerator/claim_mode/stop_reason/observation_window` 和分页账号项；三组 outcome 均各自守恒为 `N`。设备清理批次返回既有 requested/eligible/skipped 与执行结果；单项超时不阻断列表或其他项执行 |
| `GET /api/tg-accounts/{id}/authorizations` | 返回不可变 logical slot、代次、Developer App/api_id 快照、远端授权存在状态、MY bundle receipt/coverage、`recoverable_copy_count`、MY 本地/硅谷 SSH 镜像最后校验、恢复密钥 readback 状态、MY inventory sequence、最后 restore probe、迁移恢复闸门/rollback window、health/qualification blocker、保护、脱敏异常、`business_runtime_status/sv_redundancy_status/authorization_recovery_status/current authorization/fact/connection generation` 与可否辅助重建 primary 的原因 |
| `GET /api/tg-accounts/{id}/authorization-devices` | 按最新 observation 分页返回四类设备、匹配槽位/授权/代次、Developer App、Telegram `date_created` 与脱敏元数据，以及 `remote_active_total/platform_current/platform_retained/external/unresolved/as_of/stale/current_sv_login_at/login_age_hours/cleanup_button_enabled/cleanup_disabled_reason`；新账号登录后立即可读，不返回 hash、完整 IP、倒计时或资格 precheck 状态 |
| `POST /api/tg-accounts/{id}/authorization-devices/refresh` | 显式刷新该账号设备列表，保存 observation 并返回分类计数；只服务单账号详情，不参与批量清理资格判断，不校验 48 小时，MY standby_2 休眠不影响其远端 active 分类 |
| `POST /api/tg-accounts/{id}/authorization-devices/cleanup` | 接受 `reason + Idempotency-Key`，按与批量接口相同的本地 48 小时规则直接创建一个账号的 cleanup batch；不接收 hash、preview/precheck ID。响应返回 `requested_count=1/eligible_count/skipped_count/skipped_reason_counts/batch_id`；被跳过时仍持久化单个 skipped 结果并返回 batch_id，但不写 worker 队列项 |
| `GET /api/tg-accounts/{id}/authorization-device-cleanups/{operation_id}` | 返回 `requested|executing|reconcile_unknown|succeeded|partial_failed|failed`、冻结 executor authorization/fact/login_at、worker 设备读取结果、逐目标脱敏结果、保护集回读和最终 exact-set 证据摘要；没有 waiting、retry_not_before 或自动恢复 |
| `POST /api/tg-accounts/{id}/authorizations/{aid}/dr-probe` | 管理员显式创建 `drill_wake` operation；只在 MY 执行限定授权探测并断连，永不切 current；调度器不得自动调用 |
| `POST /api/tg-accounts/{id}/authorizations/{aid}/dr-activate` | 仅用于管理员重试被阻断的 SV 本地恢复，不提供强制切换；状态查看复用 operation GET。常规 `local_activate` 由权威 primary failure + standby_1 即时 SV probe 自动创建。所有 MY claim/grant/client 计数必须为 0 |
| `POST /api/tg-accounts/{id}/authorizations/{aid}/dr-emergency-reauthorize-primary` | 仅接受 `logical_slot=standby_2`，创建 `emergency_reauthorize_primary` intent；冻结双 SV 失败事实、standby_2/bundle generation、新 primary generation、SV 登录材料和唯一 `primary_regular` 出口。该接口不会切换 standby_2，也不返回其 Session |
| `GET /api/tg-accounts/authorization-dr/operations` | 按 tenant/account/type/status/blocker/expiry/cursor 分页返回 operation、`cancellable` 与状态计数 |
| `GET /api/tg-accounts/authorization-dr/operations/{id}` | 返回非敏感状态、事实摘要、审批模式、review 状态和 blocker |
| `POST /api/tg-accounts/authorization-dr/operations/{id}/qr/start` | 仅 `waiting_qr` 且有当前 MY owner/control lease、固定 MY egress version 未漂移时，通过 mTLS broker 创建或刷新 operation-scoped QR challenge；提升 challenge generation，返回短时 payload/expiry 或 202 pending，旧 payload 立即失效 |
| `POST /api/tg-accounts/authorization-dr/operations/{id}/qr/check` | 只返回当前 challenge/operation 持久状态并请求 MY owner 刷新 Telegram 结果；主运行面不得代执行 QR 登录，跨 owner/generation 回调拒绝 |
| `POST /api/tg-accounts/authorization-dr/operations/{id}/approve` | 普通双人审批；审批人必须不同于发起人，按 expected operation version 批准或拒绝 |
| `POST /api/tg-accounts/authorization-dr/operations/{id}/cancel` | 仅在 `fence_effect_started_at/remote_effect_started_at` 均为空且没有 active fence/control lease/grant consumption 时按 expected version 取消；否则返回 reconcile required |
| `POST /api/tg-accounts/authorization-dr/operations/{id}/break-glass-approve` | 仅发起人单人例外；冻结强认证、incident、当前 preflight/failure fact generation；可选 anomaly override 只能引用已确认 `other_remote_device` review/version/reason，同事务写 P0 与 review |
| `POST /api/tg-accounts/authorization-dr/reviews/{id}/complete` | 仅另一名具备 review 权限的管理员可提交结论；不得修改既有切换事实或由原发起人自结案 |
| `POST /api/tg-accounts/{id}/activity-observations/{oid}/reviews` | 为 unexpected/unresolved/needs_review observation 创建或返回唯一 open review，冻结 observation/protected-manifest versions |
| `POST /api/tg-accounts/authorization-dr/activity-reviews/{id}/decide` | 仅对 unresolved 或归属事实冲突提交“匹配我方授权 / 确认 external / 确认资产异常 / 证据不足”决定和证据；不支持官方锚点或批准外部设备绕过一键清理 |
| `POST /api/tg-accounts/authorization-dr/activity-reviews/{id}/approve` | 异人按 expected versions 批准“匹配我方授权”等会扩大 protected manifest 的归属修复；已精确分类 external 的常规一键清理不需要逐设备异人审批 |
| `GET /api/tg-accounts/authorization-dr/activity-reviews` | 按 tenant/account/status/anomaly_scope/decision/cursor 分页返回脱敏调查及状态计数 |
| `POST /api/tg-accounts/authorization-dr/operations/{id}/reconcile/preview` | 只为 `provision_reconcile_unknown` 建立或重读唯一 open reconcile case；服务端冻结 operation/item/source versions、owner epoch、运行镜像 SHA 和脱敏证据 manifest，返回 `evidence_fingerprint/classification/artifact_state/allowed_transition/blockers`，不接受目标终态 |
| `POST /api/tg-accounts/authorization-dr/operations/{id}/reconcile` | 必须携带 `expected_operation_version/evidence_fingerprint/approval_ref/Idempotency-Key`；服务端重读冻结证据后自行推导终态并 CAS apply。不得强写 succeeded、恢复旧 owner、调用登录 RPC、生成新 Session 或跳过所需远端 readback；任一版本、证据或镜像 SHA 漂移均零写入 conflict |
| `GET /api/tg-accounts/authorization-dr/operations/{id}/reconcile` | 返回脱敏 evidence manifest、classification、artifact coverage、recommended transition、apply/readback 和 blocker；不返回 Session、AuthKey、2FA、验证码、远端 hash 明文、设备明细或日志原文 |
| `POST /internal/v1/authorization-dr/operations/{id}/reconcile-claim`、`reconcile-probe-material`、`stage-facts`，以及既有 `wake-bundle`/`restore-probe`/`slot-commit` | 仅 MY 节点按中心已批准的 reconcile case 领取原 operation/generation；probe material 只返回冻结 App C 凭据，不返回手机号、密码、验证码或 2FA。节点复用原 bundle 字节，上报 secret-free stage 和完整 receipt；服务端只接受 `local_only_bundle` 或 `inventory_ahead_of_central`，校验 digest/identity/copy/inventory 后前滚，重放幂等。 |
| `POST /api/tg-accounts/{id}/authorizations/{aid}/local-activate/preview` | 在 SV 读取目标备用 Session 的即时 Telegram identity，冻结 account authorization/fact/connection generation、目标 fact version、UID/AuthKey 和 current 行；只返回 fingerprint/blocker，不改变 current。 |
| `POST /api/tg-accounts/{id}/authorizations/{aid}/local-activate` | 异人批准并携带 expected fingerprint/Idempotency-Key；行锁重读后 CAS 切换，写 current authorization、legacy Session/App/proxy 投影并提升三类 generation，旧 current 保留为 repair/protected；新 current 保持业务冻结和 `applied_pending_verification`，不能直接恢复 online。任何 probe/version/identity 漂移均零写入。 |
| `POST /api/tg-accounts/{id}/authorizations/{aid}/local-activate/verify` | 另一个幂等 operation 冻结切换后的 current authorization 与三组 generation，经异人审批向 Saved Messages 发送一次验证消息；只有 Telegram message-id 成功读回才恢复 online/degraded。失败或 unknown 保持新 current 与业务冻结，不回切旧 A、不自动重发。 |
| `POST /api/tg-accounts/{id}/authorizations/{aid}/decommission` | 双人审批；冻结 target hash、固定 current SV executor 及其 authorization fact/version、`telegram_login_at`、expected versions；迁移源还必须冻结并验证 `migration_recovery_gate_passed`。严格超过 48 小时才创建远端执行项，否则返回 skipped 及原因，不创建等待重试；恢复闸门未通过直接 blocked，不能撤销旧 SV |
| `DELETE /api/tg-accounts/{id}` | 同一事务写 `business_deleted_authorizations_retained` 并停止账号全部业务资格；不撤销或删除授权资产，不把删除结果声明为 Telegram 设备已退出 |
| `GET /api/tg-accounts?include_deleted=true` | 已删除账号继续返回授权保留/退役状态、三槽和 unresolved blocker；`authorization_retired` 前不得从管理面完全消失 |
| `POST /api/tg-accounts/{id}/protected-device-refs/{ref_id}/remote-revocations` | 为 unknown/orphan/unexpected remote 创建专用撤销 operation；冻结非零 target hash、snapshot/version、protected manifest 和固定 current SV executor，不要求 authorization id |
| `GET /api/developer-apps` | `assigned_accounts` 改为 `assigned_distinct_accounts`，并返回 `pending_distinct_accounts/used_distinct_accounts/capacity_unlimited/available_accounts/slot_purpose/assignment_status`；生产 active 默认映射必须恰好是 App A=`primary_sv`、App B=`standby_1_sv`、App C=`standby_2_my`，历史切换账号允许 A/B 实际槽位互换但必须保持三个 App 两两不同；`used` 是两类 account ID 的并集基数，同一账号多代只计一次 |
| `PUT /api/developer-apps/slot-assignments` | 以 `expected_assignment_version` 原子提交 A/B/C 三个不同 App 的固定角色映射；不接受角色缺失、重复 App 或第四个 active role。已有迁移/login operation 时版本漂移返回 409，不替换其冻结映射 |
| `POST /internal/v1/authorization-dr/operations/claim` | MY mTLS 身份按 node/region/purpose/version 领取一条 operation；provision 时取得一次性登录材料 handle，wake 时只取得绑定本地 bundle generation 的 permit |
| `POST /internal/v1/authorization-dr/operations/{id}/login-input/fetch` | 同 node/owner/inventory/generation 单次领取加密的手机号、Developer App、client metadata 和必要连接参数 bundle；响应只有 ciphertext，消费即审计；验证码与 QR 共用该 operation 输入版本 |
| `POST /internal/v1/authorization-dr/operations/{id}/qr/challenge` | 当前 MY owner 按 flow/owner/inventory/target/QR generation 发布短 TTL 加密 payload 或扫码结果；中心只在 ephemeral broker 中转发 payload，数据库仅存 digest/expiry/state |
| `POST /internal/v1/authorization-dr/operations/{id}/sv-path-failures` | 受信控制面只在已存在 `emergency_reauthorize_primary` intent 下为 `primary` 与 `standby_1` 写 typed failure fact；两条 fact 必须同 generation、当前且均失败 |
| `POST /internal/v1/authorization-runtime/accounts/{id}/local-activate` | 权威 primary failure 后自动创建/重放 `local_activate`；执行 standby_1 probe、领取冻结、Gateway drain、current/account projection CAS、旧代次 lease 失效和模块重建，不唤起 MY |
| `POST /internal/v1/authorization-runtime/accounts/{id}/restore-sv-pair` | 在 standby_1 承载业务期间为 logical primary 创建更高 generation，验证后受控切回并恢复 standby_1 ready；失败保持业务 degraded，不伪装三槽健康 |
| `POST/GET /internal/v1/authorization-dr/operations/{id}/wake-bundle` | MY 在 provision/migrate 时提交不可变 bundle generation、两份 copy manifest/digest/readback receipt、wrapped DEK/恢复密钥版本、隔离恢复 fact 和不可被 SV 解密的密文副本或引用；GET 只返回 manifest/readback，不返回可解密 Session |
| `POST /internal/v1/authorization-dr/operations/{id}/wake-bundle/restore-probe` | 仅在原登录 client 已断连后，由同一 MY owner 从 SSH 镜像解封到内存或 operation 临时态并隔离恢复；禁止 SDK 打开/回写最终 bundle 路径。校验 恢复密钥解封、Session 解析、`is_user_authorized/get_me/AuthKey fingerprint`，随后断连、擦除临时态并返回不可变 probe/zeroize receipt |
| `GET /internal/v1/authorization-dr/wake-inventory` | 仅供恢复协调器按 account/sequence 分页读取 MY 对象存储中的追加 inventory 和对象 manifest 摘要；中心库落后时进入 restore hold，只允许补写较高代次，并按 `slot_decision_id/expected old/new/version` 幂等前滚 prepared 决策；不提供 Session 或删除接口 |
| `POST /internal/v1/authorization-dr/operations/{id}/wake-permit/fetch` | 仅当前 MY owner 可单次领取 `drill_wake_probe|emergency_code_source` permit；绑定 bundle generation/owner/fence/TTL，只允许 MY 本地解封 |
| `POST /internal/v1/authorization-dr/operations/{id}/facts` | 按 owner epoch、step、fact version 写不可变远端事实，不接受状态跳级 |
| `POST/GET /internal/v1/authorization-dr/operations/{id}/candidate-secret-commit` | 仅供 SV `central_business` 新授权，提交 envelope ciphertext 或按 generation/digest 读取 receipt；standby_2 必须改用 wake-bundle 接口 |
| `POST /internal/v1/authorization-dr/operations/{id}/login-code-grants` | MY 仅在 app-session delivery 分支下提交绑定 challenge/服务消息的一次性加密登录码；持久层只写 digest/receipt |
| `POST /internal/v1/authorization-dr/operations/{id}/login-code-grants/fetch` | 只有绑定的 SV login runtime 可消费一次；响应丢失不重取，进入 reconcile |
| `POST /internal/v1/authorization-dr/operations/{id}/provision-verification-code/fetch` | 仅供 provision/migrate 的 MY 新授权登录单次领取当前 flow 验证码 |
| `POST /internal/v1/authorization-dr/operations/{id}/managed-2fa/fetch` | 仅供 provision/migrate 的 MY 新授权登录；紧急重建中 2FA 只交付给 SV login runtime |
| `POST /internal/v1/authorization-dr/operations/{id}/transition` | 按 expected operation version 提交单步状态；服务端只根据已存在事实派生结果 |

所有 list API 统一返回 `items/next_cursor/status_counts/as_of`，服务端从调用者 tenant 注入范围并校验逐项权限，禁止客户端扩大 tenant；默认按 `created_at,id` 稳定倒序。所有 mutation 请求必须使用 `Idempotency-Key`；授权关键 mutation 还必须由 Gateway/operation 服务在事务内注入当前 `contract_epoch + operation_identity`，专用 DB writer role/gate 拒绝旧 epoch 或缺失身份。调度器按 CAS 将 DR/egress/backfill/cutover 中 `fence_effect_started_at/remote_effect_started_at/apply_started_at` 均为空、没有 active control lease/fence/grant consumption 且超过 `expires_at` 的记录置为 expired；已有副作用只能 hold/reconcile。activity review 不自动过期或结案，持续保护目标。版本冲突返回 409 和当前非敏感版本，不自动覆盖。

安全批次的动态规模不另建阶梯状态：`target_set_fingerprint` 对 `tenant_id + selection_mode + 规范化选择条件 + 排序后 account_id 全集` 计算 SHA-256；`N` 与指纹一经创建便不变，新增账号只进入新批次。`selection_mode=all_online_accounts` 必须先冻结当时全部在线账号，再执行任何新鲜 A probe；A 探测失败、封禁、人工处理或冻结后删除只能改变对应 outcome，不能删除批次项。`complete_online_abc` 每个账号项有一条 primary probe 和 B/C 各一条槽位结果，账号/B/C 三组聚合均各自等于 `N`，账号覆盖分子只计 `already_qualified+succeeded`。最终 outcome 为成功的槽位永不再领取；恢复只推进未终态槽位。MY 节点继续在全局单登录 owner 下每次只领取一个可执行 C operation，领取不使用单向页 cursor；不同账号由持久 rate bucket 和最早 `coalesce(last_claimed_at, created_at)` 公平轮转。

`complete_online_abc` 单账号按 A -> B -> A fence readback -> C 串行。A fresh probe 成功后冻结 `code_source_authorization_id/fact_version/connection_generation/challenge_generation`；B/C 需要登录时只能由该 A 读取各自 challenge-bound app-session code。B 或 C 已健康时执行资格 readback并写 `already_qualified`，不创建登录 flow。A 代次、current、UID 或权威可用性漂移写 `code_source_changed|primary_probe_failed`，停止该项后续登录；不得动态改用 B/C。hash=0 的 peer resolution 可以使用另一个合格 SV observer，但 `device_observer_authorization_id` 与 `code_source_authorization_id` 必须分别持久化，observer 不因此成为码源。

Developer App 与设备归属的服务端规则固定为：线上现有三套 App 先原子登记为 A=`primary_sv`、B=`standby_1_sv`、C=`standby_2_my` 默认映射，新账号和迁移 operation 冻结三条 assignment/credentials version，不选择第四套 App。历史主备切换账号允许 A/B 实际槽位互换，迁移审批必须再次证明当前主、SV standby_1、App C 迁移源使用三个不同 App。三槽分别绑定三个 Developer App，但归属主键仍是未撤销我方授权资产保存的唯一非零 remote authorization hash。`api_id` 命中但 hash 不命中时必须分类 `external`；hash 命中但 `api_id` 与该授权冻结快照不一致时仍保护，但同时产生资产异常 blocker。官方 App/device type 不自动受保护。App 容量按 `distinct account_id` 统计该 App 下未撤销授权账号集合与非终态登录 operation 账号集合的并集，同账号 App C 的 SV 旧代与 MY 新代并存只计一次；分配事务锁定 App/assignment version，`used_distinct_accounts >= max_accounts` 返回 `developer_app_capacity_shortfall`。

`max_accounts <= 0` 沿用现有“不设平台分配上限”语义，API 返回 `capacity_unlimited=true + available_accounts=null`；已软删除但尚未完成授权退役的账号继续计入 used，远端授权撤销并完成退役 readback 后才释放占用。

任何 Gateway-bound ExecutionAttempt 都必须在 `before_call` 阶段冻结 `authorization_id/authorization_fact_version/connection_generation/environment_binding_id/proxy_binding_id/account_fence_epoch`。Gateway 只接受与当前 permit 一致的快照；`gateway_call_started_at` 后字段不可修改。本地切换和主授权重建只影响后续新 Attempt，不能把旧 Attempt 原地改绑。

decommission、remote-device revoke 和 authorization-device cleanup 都由账号安全执行面经统一 Gateway 执行。设备清理固定使用批次创建时的 current SV authorization，不动态选择另一槽位；创建事务冻结 authorization/fact version 和已落库 `telegram_login_at`，并仅在 `server_now > telegram_login_at + 48h` 时写执行项。资格阶段不连接 Telegram。worker 对每个执行项单独取得 `inventory_mutation` lease，读取 exact set、分类、逐 hash 撤销和最终 readback；读取超时或失败只结束当前项并继续下一项。MY standby_2 不作 cleanup executor，external target 不要求本地 Session/authorization 行，MY operation claim endpoint 不能作为撤销后门。

## 2. 权限、安全与审计

| 权限 | 能力 |
| --- | --- |
| `accounts.view` | 查看 DR 摘要和脱敏出口 |
| `accounts.security.read` | 在账号详情查看和刷新登录设备、分类、最后 observation、远端授权时间与脱敏元数据；新登录账号立即可用 |
| `accounts.security.cleanup_devices` | 查看本地 48 小时门槛和置灰原因，一次确认后直接创建单账号/批量清理；不允许上传 hash，不提供 precheck/preview/waiting 操作 |
| `accounts.security.session_manage` | 创建补齐/迁移和人工登录处理 |
| `accounts.security.complete_online_abc` | preview/批准并创建 10 账号或全量在线 A/B/C 补齐批次；不能修改冻结 N、码源 A、槽位顺序或 stop gate |
| `accounts.authorization_dr.activate` | 发起显式演练/紧急重建、查看或重试被阻断的 SV 本地自动切换；不能绕过 failure/probe 强制切换，只有双硅谷授权失败事实允许唤起 MY |
| `accounts.authorization_dr.break_glass` | 单人 break-glass 批准；不能完成自己的 review |
| `accounts.authorization_dr.break_glass_review` | 复盘他人的 break-glass operation |
| `accounts.authorization_dr.activity_review` | 调查活动 observation；解除 unexpected 保护或确认撤销必须异人审批 |
| `accounts.authorization_dr.decommission` | 发起或审批撤销旧授权 |
| `accounts.authorization_dr.remote_device_revoke` | 发起或审批 unknown/orphan/unexpected remote 撤销；只能使用合格 peer，不能解除未 readback 的保护 |
| `accounts.authorization_dr.egress_manage` | 与 `system.manage` 同时具备时发起出口变更，不能批准自己的申请 |
| `accounts.authorization_dr.egress_approve` | 审批并应用他人的出口变更；不能读取 secret 内容 |
| `system.authorization_dr_migrate` | 执行存量 preview/apply/readback；apply 必须引用批准 fingerprint |
| `system.authorization_dr_migrate_approve` | 审批他人发起的存量 backfill；不能修改 target fingerprint 或自行 apply 自己的 preview |
| `system.authorization_contract_cutover` / `system.authorization_contract_cutover_approve` | 分别 preview/apply 或异人审批 contract epoch 切换；apply 人不能是 preview 发起人 |
| `system.developer_app_failure_domain_manage` / `system.developer_app_failure_domain_approve` | 分别提交或异人审批 App owner-domain assertion；均不能读取 owner 明文或 App secret |
| `accounts.security.credential_manage` | 受控使用托管 2FA |
| `authorization_dr.internal_execute` | MY 节点 mTLS 服务身份，仅处理 provision/migrate/repair/drill_wake/emergency_code_source；只能本地解封指定 bundle，无 operation 时不能连接 |
| `authorization_dr.sv_failure_observe` | 控制面健康判定服务 mTLS 身份，仅为已存在的紧急重建 operation 产生 primary/standby_1 typed failure fact，不读取 MY Session |
| `authorization_dr.sv_login` | 批准的 SV login runtime；只能从 `primary_regular` 发起新 primary 登录并消费一次 code grant，永不取得 standby_2 Session |

紧急重建 primary、decommission、存量 backfill/cutover apply、App failure-domain assertion、出口变更和会扩大我方 protected manifest 的归属修复要求发起人与审批人分离。普通一键清理已精确分类的 external 设备只要求有权限管理员的一次确认和操作原因，不要求逐设备或双人审批。单人 break-glass 只豁免紧急重建的审批人分离，不豁免双 SV 失败、SV 登录运行时就绪、Telegram 渠道判定、RPC fence、MY 断连、CAS 和 readback。

审计至少记录 operation/idempotency/trace、账号、logical slot/目标代次、Developer App/api_id/slot assignment 快照、旧新授权版本、`requested/eligible/skipped` 计数与 reason、冻结 executor authorization/fact version/`telegram_login_at`、worker 设备 observation/snapshot digest/protected manifest/target hash digests、设备读取时长/失败、逐目标撤销结果、contract epoch/runtime capability/DB gate、egress ACL、wake bundle/permit/code grant 的 generation/digest/receipt、双副本 readback、恢复密钥 key/decrypt fact、MY inventory sequence、隔离 restore probe、UID/AuthKey/设备结果、双 SV failure fact、MY wake/断连、SV new-primary commit/CAS/probe、审批和最终 readback。审计不保存原始 IP、远端 hash 明文、Session、QR payload、登录码、2FA、API Hash、AuthKey、出口凭据或完整手机号。

SV primary/standby_1 Session 使用中心 Session keyring；MY standby_2 每个 bundle 使用独立 DEK 密封，DEK 只以 wrapped 形式保存，并由双机备份的专用恢复密钥包装，不依赖 SV `SESSION_SECRET_KEY`。MY 本地持久卷与硅谷 SSH 镜像各保存一份不可变密文；恢复密钥以 root-only 文件存在两机运维目录，硅谷 backend、worker 和业务身份不挂载。上线前必须通过替换 MY 主机/数据盘的演练证明恢复密钥和 SSH 镜像仍可恢复；仅把 key 放在单台主机时返回 `my_recovery_key_unproven`。wake permit 和 login-code grant 默认 60 秒、严格单次消费；SV 业务运行时只能消费登录码，不能读取 MY Session。明文 Session、登录码和 2FA 只存在各自批准运行时的内存窗口，不进入数据库、队列、日志、argv 或环境变量。MY 没有业务 permit，ACL 只允许 provision/drill/emergency-code-source 的限定 RPC。

QR payload 只由当前 MY owner 生成，经 mTLS broker 在 challenge TTL 内一次性或短时读取；持久层仅保存 operation/flow/owner/inventory/target generation、QR generation、payload digest、expiry 和状态。刷新时先 CAS 提升 generation，再销毁旧 broker payload；扫码回调、2FA 和 candidate commit 均必须匹配新 generation。

login-code grant 在返回前原子标记 consumed；响应丢失不得重取。只有明确证明 SV `finish_login` 未产生远端授权、旧 flow 已 superseded、MY client/lease 为零且旧 code grant 已清零，才能发起新 challenge；任一事实未知保持 hold。

## 3. 数据保留、备份与清理

- 通用任务清理、Action/Attempt 归档、临时文件清理、登录 flow TTL、账号软删除、数据库压缩、SV 备份轮换和 MY 快照轮换不得删除当前/protected 授权及其 MY wake bundle、两份可恢复密文副本、MY inventory、wrapped DEK、receipt、仍被引用的 恢复密钥版本、logical slot/generation、probe/qualification/failure fact、operation、activity review 和 protected-device ref；轮换后 `recoverable_copy_count` 不得低于 2。
- 软删除必须先写 `business_deleted_authorizations_retained`，并使所有业务候选查询、Action claim、listener claim、online probe 和同步 claim 排除该账号；未进 Gateway 的 claim 释放，已进 Gateway 的 Attempt 按冻结代次收口。删除后禁止 provision/migrate/drill/emergency reauthorize，只允许授权 readback/reconcile/decommission。
- 已删除账号在 `GET /api/tg-accounts?include_deleted=true` 中保留到 `authorization_retired`。每个我方授权的 decommission readback 和 MY erase receipt 全部完成前，不允许物理删除账号、授权、设备观察或退役 blocker。
- probe fact 每个授权最新一条，以及 unknown/reconcile、活动异常、切换审计相关的派生 fact 永久保留；其余超过 180 天可由显式归档批处理聚合后删除。归档器不得命中永久集合。
- 原始 IP 密文与派生审计分离，默认最多保留 180 天；到期删除密文，只永久保留 keyed HMAC、国家、时间、设备分类、允许出口区间和处置结论。页面始终只显示脱敏值。
- egress assignment 在明确释放后保留逻辑记录；egress usage fact、connectivity fact、break-glass approval/review 和异常结论按审计策略永久保留，但不保留出口凭据或原始 IP。
- 幂等键在 operation 终态超过 90 天后允许清理；非终态 operation 的幂等键永不清理。
- 登录 flow 临时 Session、QR broker payload、登录码 envelope 和 2FA 材料按 TTL 清除；challenge/message digest、grant 消费/清零收据、已提交授权密文和 operation 审计不是临时数据。
- login-input grant 的节点公钥、版本和消费审计按 operation 保留；未消费 grant 到期即不可领取，服务端不保存明文 bundle，节点必须在 Session commit、owner 丢失或 operation 终止时清零内存材料。
- 只有双人审批 decommission、无冲突 lease/operation、Telegram 撤销 readback 和保留期均完成，才允许分别擦除 MY 本地包、SSH 镜像、中心密文副本和 wrapped DEK；每一步写独立 erase receipt，全部完成前保持 `erase_pending`，任一步 unknown 保持 `retirement_reconcile_unknown`。共享 恢复密钥版本 只在没有任何 active/retained bundle 引用时退役。已删除账号退役时由 SV peer 逐 hash 撤销其他平台设备，最后一个 SV revoker 使用当前授权退出接口；最后退出或任一撤销 unknown 时保持 `retirement_reconcile_unknown`。MY 不为退役唤起，只在 peer 已证明其远端授权撤销后离线擦除包。
- 中心数据库备份必须包含 wake bundle/copy manifest、receipt、digest、wrapped DEK/恢复密钥版本、最后已知 MY inventory sequence 与 operation/grant/probe/protection 元数据，但不能声称它可代替 MY 本地包、SSH 镜像或独立 MY inventory。禁止备份明文敏感值。
- 中心库从旧备份恢复时，先隔离旧运行面、提升 `cluster_incarnation/contract_epoch` 并进入 `central_restore_reconcile_required`，禁止 provision/migrate/decommission/erase 和授权设备清理。恢复协调器按 UID、AuthKey blind index、slot/bundle generation、copy manifest、ciphertext digest、MY receipt 和 MY inventory 最大有效 sequence 对账；中心只能追加恢复较高代次或保留 unknown。仅有 `slot_commit_prepared` 时按同一 `slot_decision_id/expected old/new/version` 幂等重放：已是目标返回原结果，仍是 expected old 则前滚，其他值 conflict；不能用旧中心数据覆盖、降代、标孤儿或擦除 MY 更新包。全量 readback 通过后才解除 mutation hold。

## 4. 失败码与处置

| 错误码 | 处置 |
| --- | --- |
| `execution_node_mismatch` / `malaysia_wake_operation_required` / `malaysia_egress_unproven` / `fixed_egress_version_conflict` | 不连接 Telegram，修复当前显式 operation、唯一 MY 节点、固定出口或 assignment 证明；不得由定时器连接或自动新增/切换 IP |
| `security_batch_target_changed` / `empty_target_set` | 只适用于补齐/迁移等仍有 precheck 的动作：前者重新确认新 `eligible_count/target_set_fingerprint`，后者显示当前无目标账号；设备清理不产生此资格预检冲突 |
| `primary_probe_failed` / `code_source_changed` | 保留账号与 B/C 槽位结果在冻结 `N` 中；不发起新的 B/C 登录、不改用 B/C 读码，按远端事实进入 failed/manual/unknown 并释放账号 lease |
| `online_abc_outcome_conservation_failed` | 立即停止新 claim 并保持 runtime=`off`；只从账号项与 B/C 槽位结果重算三组计数，禁止手工改汇总或缩小 `N` |
| `online_abc_canary_observation_incomplete` | 不允许创建全量 preview；必须先由正式 accept 证明同一 10 账号 10/10 ABC 合格、零 unknown、零保护漂移、MY client=0，且十个成功 E4 operation 都有非空 Saved Messages 远端消息 ID；不再要求固定经过时长 |
| `online_abc_primary_send_unproven` | canary 某个 A 缺少 succeeded E4 operation 或对应成功审计中的非空 Saved Messages 远端消息 ID；保持 `observing`，不创建全量 preview |
| `logical_slot_conflict` / `assignment_version_conflict` | 不改写 role 或覆盖槽位；按冻结账号、logical slot、目标代次、operation、authorization 和固定 egress version 对账 |
| `login_input_grant_expired` / `login_input_grant_consumed` / `login_input_version_conflict` | 不返回登录材料；先核对 owner、远端设备和 receipt，满足无远端副作用条件后才可提升 grant generation |
| `dr_qr_challenge_expired` / `dr_qr_owner_mismatch` / `dr_qr_generation_conflict` | 销毁 payload；只有当前 MY owner、同 operation/flow/inventory/target generation 可提升 QR generation 后重建，不降级到旧账号级 QR |
| `qualification_input_stale` / `developer_app_owner_domain_unproven` | 资格与登录输入失效；重新批准 assertion 或生成冻结当前资源版本的新 qualification fact |
| `login_runtime_lost` | grant 已消费且远端结果未知；冻结同 generation，执行 device-set/receipt 对账，不自动重发登录 |
| `sv_primary_failure_unproven` / `sv_standby_1_failure_unproven` | 不审批、不领取 MY operation；补齐同一 `emergency_reauthorize_primary` generation 的两条 typed failure fact，任一路径仍可用时终止 |
| `primary_available` / `standby_1_available` | MY 保持休眠；按硅谷本地路径继续或恢复 |
| `local_activate_primary_failure_unproven` / `local_activate_standby_probe_failed` | 不切换；前者继续观察 primary，后者保留业务 unavailable 并判断是否已形成双 SV 路径失败，MY 仍休眠 |
| `current_projection_inconsistent` / `authorization_generation_mismatch` | 停止新业务领取；重读唯一 current、账号 Session/App/proxy 投影和 fact/connection generation，旧结果不得写入新状态 |
| `listener_stale_generation` / `online_probe_stale_generation` / `sync_stale_generation` | 丢弃旧代次结果，不推进 cursor、online 或同步成功状态；由新 current generation 重新领取或探测 |
| `sv_local_redundancy_degraded` / `restore_sv_pair_failed` | 业务可由 standby_1 继续，但页面与告警保持 degraded；创建或重试 logical primary 更高 generation，完成受控切回前不标三槽健康 |
| `developer_app_capacity_shortfall` | 不发起登录；显示 App A/B/C 各自 assigned/pending/available 与缺口，补足 App 名额后按原 operation 重验，不通过增加 IP 绕过 |
| `developer_app_slot_assignment_incomplete` / `developer_app_slot_assignment_conflict` | 不创建新账号三槽或 MY 迁移；修复现有三套 App 的 A/B/C 唯一角色映射并重新 preview，不选择第四套 App |
| `account_business_deleted` / `account_authorization_retirement_pending` | 拒绝新业务、同步、演练和紧急唤起；保留账号与授权资产，只允许 readback/reconcile/decommission |
| `sv_login_runtime_unavailable` / `sv_primary_egress_unavailable` / `sv_login_input_unready` | 保持 `waiting_sv_login_runtime`，MY active client 必须为 0；恢复 SV 登录运行时、唯一业务出口或登录材料后重验 |
| `sv_path_recovered_during_wait` | 不下发 standby_2；同 emergency operation 重验并写 `superseded_by_sv_recovery`，standby_2 保持 dormant，按恢复的 primary 或 standby_1 安全解冻 |
| `egress_change_version_conflict` / `egress_topology_change_forbidden` / `egress_retire_blocked` | 不应用出口变更；一期只允许硅谷/MY 双 IP，重新 preview assignment、usage、lease 和版本 |
| `egress_hmac_version_uncomparable` | 保持 review，不升级异常；完成公共 key version 回填或按历史不可比较结案 |
| `egress_reputation_signal` | 停止 MY 新登录和显式唤起；保留账号级独立处置与人工恢复事实，不自动增加或切换 IP |
| `authorization_key_duplicated` | 授权置 `dr_state=invalid`，派生 blocker `repair_required`，停止连接和激活；调查 AuthKey 复制、runtime owner 和 fence，不归因出口 |
| `account_risk_signal` | 仅冻结或修复受影响账号；单账号限制不能封禁整条出口 |
| `identity_mismatch` / `auth_key_collision_conflict` | 隔离候选，禁止入槽、激活和重登，人工调查 |
| `authorization_hash_missing_or_zero` / `authorization_hash_ambiguous` | 从可信 peer 重读；未解决前阻断清理和提交 |
| `external_authorization_detected` | 正常可处理分类；hash 非零且不匹配任何我方授权资产，包括同 `api_id` 额外登录和官方手机/桌面/Web，由 worker 执行开始时纳入本次清理目标 |
| `authorization_activity_unresolved` / `authorization_activity_needs_review` / `activity_review_pending` | 保留 observation、我方 protected refs 和 review，阻断当次清理与常规切换，不猜测归属 |
| `authorization_activity_anomaly` | `target_authorization/account_unknown` 绝对阻断；仅确认无关的 `other_remote_device` 可被 break-glass 按 review/version/incident/reason 显式覆盖，保护与异常事实不变并强制复盘 |
| `provision_reconcile_unknown` | 阻断新登录、迁移提交和设备清理，显式对账 |
| `candidate_secret_commit_unknown` / `wake_bundle_commit_unknown` / `orphan_remote_authorization` | 不重登、不入槽；SV 查询 candidate receipt，MY 查询 bundle generation/digest receipt，无收据的远端设备继续保护和对账 |
| `remote_device_revoke_unknown` | 保持 protected ref 与账号冻结；同 operation exact-set reconcile，不重复撤销或释放 ref |
| `remote_device_target_already_absent` | 非错误终态；仅当新鲜 exact-set、manifest/version 未漂移、其他 protected hash 完整且无新增未知设备时写 fact，跳过 reset RPC 并以 `succeeded_already_absent` 释放 ref |
| `device_cleanup_unresolved` / `protected_authorization_hash_unproven` | 当前 worker 项零撤销并 failed；修复三槽及我方历史授权 hash 映射后由运营重新提交 |
| `cleanup_precheck_not_supported` | 设备清理禁止调用通用 precheck；直接提交创建接口，由服务端返回 eligible/skipped 汇总 |
| `login_age_not_over_48h` / `login_time_missing` | 当前账号直接 skipped；返回原因，不连接 Telegram、不创建 waiting 或自动任务 |
| `current_sv_authorization_unavailable` / `current_authorization_changed` | 当前账号直接 skipped；修复或稳定 current 后由运营重新提交，不在同一批次改绑 executor |
| `device_list_read_timeout` / `device_list_read_failed` | 当前执行项 failed 并释放 lease，批次继续下一账号；不把单项超时升级为整批失败 |
| `device_cleanup_remote_unknown` | 进入 `reconcile_unknown`，只重读 exact set，不重复撤销同一 hash |
| `new_external_detected_after_apply` | 已冻结执行快照目标保留各自结果，但 operation 为 `partial_failed`；不自动撤销执行期间新出现的 external，返回最新脱敏明细并由运营重新提交清理 |
| `protected_device_missing_after_cleanup` | P0；立即停止剩余撤销，冻结账号 Telegram mutation，保留全部 fact 并进入授权恢复；不得把批次标记成功 |
| `telegram_code_delivery_manual_required` | `SentCode.type` 为 SMS/email/call/QR/未知；不唤起 MY，进入人工处理 |
| `login_code_not_found` / `login_code_challenge_mismatch` / `login_code_grant_expired` | 拒绝旧消息、旧 flow、challenge 之前或多重匹配的登录码；MY 断连，不用“最新验证码”降级 |
| `verification_code_unreadable` / `two_fa_not_managed` | 进入人工处理，2FA 只在 SV login runtime 使用，不交付 MY |
| `telegram_limit` | 按 Telegram 权威 retry 时间等待 |
| `runtime_fenced` | 当前实例不得连接或发 RPC |
| `contract_epoch_mismatch` / `runtime_capability_unproven` / `session_recovery_key_policy_unproven` / `recovery_key_policy_version_conflict` / `telegram_egress_acl_unproven` | 拒绝关键 mutation 或 cutover；修复实例注册、旧 client drain、DB gate、恢复密钥授权身份/decrypt-denied fact 或 ACL readback |
| `account_rpc_frozen` / `authorization_control_busy` / `cluster_incarnation_mismatch` | Gateway 前拒绝并按当前持久事实对账 |
| `telegram_fresh_reset_rejected` / `FRESH_RESET_AUTHORISATION_FORBIDDEN` | 即使本地登录时间已超过 48 小时仍被 Telegram 拒绝时，当前项直接 failed，不 waiting、不自动重试；已有部分撤销时先 readback 并记 partial_failed |
| `gateway_drain_unknown` | 保持冻结，不切换、不自动回滚 |
| `malaysia_wake_unavailable` | MY 节点无法执行紧急唤起；保持 blocked，不允许在硅谷替代生成 MY wake fact |
| `wake_bundle_missing` / `wake_bundle_generation_conflict` / `wake_bundle_decrypt_failed` | 不复制中心/SV Session 修复；保留旧包和授权保护，在 MY 重新登录生成新 generation |
| `my_recovery_key_unproven` / `wake_bundle_copy_count_insufficient` | 不提交槽位、不撤销旧 SV、不擦除任一副本；恢复双机恢复密钥解封 事实和两份可恢复副本，单主机本地 key 不算通过 |
| `wake_bundle_local_copy_unverified` / `wake_bundle_snapshot_copy_unverified` / `wake_bundle_immutable_conflict` | 保留现有对象和远端授权；从仍可解封副本写更高 bundle generation 并重新验证，禁止覆盖原路径或重新登录 Telegram |
| `wake_bundle_restore_probe_failed` | 保持旧 SV current/retained+protected，MY candidate 不计 dormant_ready；从 SSH 镜像隔离恢复并完成 Telegram probe/断连前不得退役旧源 |
| `wake_bundle_inventory_ahead_of_central` / `central_restore_reconcile_required` | 冻结授权 mutation；按 MY 最大有效 inventory sequence 只增补回中心，禁止旧库降代、删除或释放保护 |
| `slot_commit_decision_conflict` | 保持新旧授权和全部 bundle protected；核对同一 decision 的 expected old/new/version，不恢复旧库、不覆盖槽位、不创建新登录或退役 |
| `malaysia_owner_fencing_unproven` / `login_code_grant_conflict` | 不重发登录码；先证明 MY lease/client/permit 已失效、grant 已清零且 SV 未产生远端授权 |
| `authorization_version_conflict` | 返回当前版本，重新预检 |
| `preflight_fact_stale` / `approved_intent_changed` | 普通路径同意图可刷新 generation；目标、策略、出口或结果类型变化使批准失效；break-glass 必须重新强认证和批准 |
| `operation_cancel_reconcile_required` | 已取得 lease/fence 或开始远端副作用，不能取消/expire；保留非终态并显式 reconcile |
| `migration_fingerprint_conflict` / `migration_conflict_unknown` | 不 apply、不切 resolver；重新 preview 或人工修复唯一映射 |
| `migration_app_mapping_incomplete` / `migration_source_standby_not_unique` | 不启动 MY 登录；先将现有三套 App 唯一映射 A/B/C，并唯一识别 App C 的旧 SV standby_2 |
| `migration_cutover_complete_retirement_skipped` | MY standby_2 已提交、旧 SV 源 retained+protected；当前 SV executor 未严格超过 48 小时、时间缺失或不可用，本次退役 skipped。三槽可用且业务不回滚，设备页显示 3 当前 + 1 历史，由运营后续重新发起退役 |
| `migration_cutover_complete_recovery_blocked` | MY slot 已提交但双副本/恢复密钥/inventory/隔离恢复闸门未通过；旧 SV 必须保持远端 active、retained+protected，不创建 decommission |
| `migration_retirement_reconcile_unknown` | 不删除旧 SV Session/保护或重复 reset；保护新旧授权并以 Telegram exact set 对账，MY 切换不自动回滚 |
| `migration_rollback_window_closed` | 旧 SV 远端撤销已 readback，禁止把旧密文重新设为 current；修复 MY 可恢复副本或重新登录新授权 |
| `wake_bundle_erase_partial_unknown` | 保持 `erase_pending` 和全部审计/保护引用；分别对账本地、对象、中心副本和 wrapped DEK，不把账号标为 authorization_retired |
| `telegram_recovery_unproven` / `send_recovery_unproven` | 保持 waiting/warming/hold；MY 读码、SV 新 primary 授权和业务发送分别取证 |
| `break_glass_pending_review` | 复盘逾期，阻断新的切换类 operation |
| `central_dependency_unavailable` | MY 不自主启动；恢复控制面和 SV login runtime 后再使用 MY 密封包 |

## 5. 指标与告警

- `dormant_malaysia_authorization_coverage`：当前非业务 standby_2 为 `dormant_ready|wake_probe_required`、qualification 完整、`recoverable_copy_count=2`、恢复密钥/inventory/隔离恢复事实有效且 MY client/lease 为零的账号数 / frozen eligible denominator；展示过期只提示需演练，不触发自动连接。
- `three_slot_independent_coverage`：三槽均为远端 active，Developer App、AuthKey 和非零 authorization hash 两两不同，且 standby_2 的双副本/恢复密钥/inventory/restore probe/bundle receipt/qualification 完整的账号数 / 同一 frozen denominator；不设降级 tier 冒充完整三槽。
- `complete_online_abc` 另行固定展示 `frozen_online_n/primary_probe_passed/standby_1_already_qualified|succeeded|open/standby_2_already_qualified|succeeded|open/account_abc_coverage/reconcile_unknown/stop_reason`；三组 outcome 合计分别必须等于 `N`，任何守恒失败告警 P0 并停止 claim。
- 设备指标必须分开展示 `remote_active_total/platform_current/platform_retained/external/unresolved/cleanup_requested/cleanup_eligible/cleanup_skipped/cleanup_skip_reasons/cleanup_failed/cleanup_unknown/device_list_read_timeout`，不把 MY client=0 计为 standby_2 已撤销，也不把同 `api_id` 的 external 计为我方设备。
- 核心运行指标为 `business_runtime_status`、`authorization_recovery_status`、`sv_primary_available`、`sv_standby_1_ready`、`sv_local_redundancy_degraded_count`、`failure_detection_duration`、`local_activate_duration`、`restore_sv_pair_pending/failed`、`stale_generation_discarded`、`my_dormant_authorization_coverage`、`my_wake_bundle_receipt_coverage`、`my_recoverable_copy_count_0|1|2`、`my_local_copy_last_verified_at`、`my_snapshot_copy_last_verified_at`、`my_recovery_key_unproven`、`my_restore_probe_last_result`、`my_inventory_ahead_of_central`、`migration_recovery_blocked`、`my_last_explicit_wake_at`、`my_active_client_count`、`waiting_sv_login_runtime`、`emergency_code_source_success`、`sv_new_primary_commit_success` 和 `superseded_by_sv_recovery`。无 provision/migrate/repair/drill/emergency-code-source operation 时 `my_active_client_count` 必须为 0。
- SV runtime/出口健康、standby_1 fresh ready 且无既有 Gateway unknown 的 canary 中，`primary_failure_confirmed_at -> unfreeze_new_business_claims_at` 必须不超过 120 秒；故障检测耗时单独展示，不能把检测阶段从端到端业务中断中隐藏。
- Developer App 指标固定为 `slot_assignment_complete/assigned_distinct_accounts/pending_distinct_accounts/available_accounts/capacity_shortfall_accounts`，必须从 A/B/C 角色映射、授权资产与非终态登录 operation 计算，不从账号主投影计算；同账号 App C 新旧代并存只计一次。
- 账号生命周期指标固定为 `business_deleted_authorizations_retained/authorization_retirement_pending/authorization_retired/retirement_unknown`；账号软删除成功不能减少未退役授权分母。
- 同时监控固定硅谷/MY 出口、冻结 `N`、覆盖分子、MY bundle generation/receipt/快照、串行登录 owner、QR challenge、login-input/wake-permit/code grant、qualification/resource drift、contract epoch/DB gate/ACL、AuthKey/UID/orphan、保护/control lease/RPC fence、`SentCode.type`、MY 读码与 SV 新 primary commit/probe 耗时；不设置周期 MY probe 指标。
- AuthKeyDuplicated、UID mismatch、未收口 orphan、受保护设备误删、RPC fence bypass、已确认 egress reputation、已确认 activity anomaly、MY 节点产生运营 Action/Attempt 或单人 break-glass 均进入 P0；停止范围必须按错误类型区分账号、授权、出口或整体扩量。
- 在控制面、SV login runtime/egress 和 MY code source 均就绪的前提下，单账号批准后新 primary 授权目标为 10 分钟内完成，计时起点为 `sv_login_runtime_ready_at`；该目标不是 SV 基础设施恢复 RTO，也不是发送 RTO。

## 6. 发布、迁移与回滚

1. `P0A additive schema`：新增 immutable logical slot/真实 provision region、Developer App A/B/C slot assignment 与 api_id 冻结快照、remote authorization state/hash blind index、设备 observation 的远端 `date_created`、当前 SV `telegram_login_at` 投影、cleanup requested/eligible/skipped/operation、App qualification fact、wake bundle/copy/inventory/restore fact/permit/login-code grant、QR challenge metadata、receipt、RPC fence/control lease、contract control/runtime registry、硅谷/MY 固定出口、protected ref、账号 current/runtime/lifecycle 投影、ExecutionAttempt 授权代次字段、online/listener/sync generation 与统一 resolver/Gateway permit；contract mode=`legacy_read`。
2. `P0B guarded backfill`：preview -> 异人批准 -> apply -> readback。先把线上现有三套 App 唯一冻结为 A=`primary_sv`、B=`standby_1_sv`、C=`standby_2_my`，不创建第四套 App；再冻结 role 到 logical slot 的唯一映射和每槽 Developer App/api_id，由合格非目标 peer 读取远端设备集，只对唯一收敛的账号/authorization/before-after 差分回填非零 hash 与远端 `date_created/telegram_login_at`。只有 legacy primary 且自身 hash 为零时，固定执行“primary 观察创建 SV standby_1 -> standby_1 反查 primary -> SV peer 观察创建 MY standby_2”的交叉证明顺序；任一步不唯一即停止后续登录并保持 unknown。只有 App 命中或归属歧义时同样保持 unknown；无法证明登录区域或登录时间时保持 unknown。旧 standby_2 不伪造 qualification/usage/anomaly；mode=`shadow`，此阶段设备可立即查看，但不开启 cleanup apply。
3. `P0C hard cutover`：shadow diff 为零后注册 runtime capability，验证旧 client drain、DB mutation gate、双机恢复密钥文件/版本 readback 和分区 Telegram egress ACL；旧业务进程仍能读取 MY bundle、把 standby_2 交给 SV 或从 MY 运行业务时失败。
4. `P0D DR enable`：再次 readback 非 stale 实例、DB gate、ACL 和兼容投影，提升新 epoch 到 `dr_enabled`；才允许 provision。回滚只能用更高 epoch 降 mode，不能恢复旧绕过路径。
5. `P1 node canary`：建立两个互相独立的单账号批次，第一个账号完整通过后才批准第二个。每个账号分别满足 MY current、唯一非零远端 hash、本地+SSH 镜像双副本、恢复密钥解封、隔离 restore probe、中心 receipt/MY inventory、slot CAS、旧 SV retained/protected 和 Telegram exact-set readback；提交后取得两次相隔至少 60 秒的独立读回，且没有新增 unknown、worker restart 或 MY 业务 Action/Attempt。任一失败立即切 `off`，不得领取第二项。原 MY 主机/数据盘替代恢复和更高 bundle generation 仍作为后续独立故障演练，不得以两账号槽位 canary 替代。
6. `P2 ten-account ABC canary`：在 runtime=`off`、全局 unknown=0、生产 SHA/schema/readback 与批准 manifest 一致后，冻结同一 10 个账号。逐账号 A fresh probe，健康 B/C 只 readback，缺失 B 由 A 读本次码在 SV/App B 补齐，旧 SV/缺失 C 由 A 读本次码在 MY/App C 迁移或补齐；每项完成后 MY client=0。一个 unknown 即停止，不能换账号补足 10。10/10 ABC、B/C 各 10/10、保护和计数守恒通过后进入待接受状态；正式 accept 即时复核十个 A 无漂移并逐项读取成功 E4 的非空 Saved Messages 远端消息 ID，不等待固定时长。
7. `P3 full online ABC rollout`：只在新的 10 账号 canary 通过即时 A/发送验收后创建 `selection_mode=all_online_accounts` preview。创建事务先冻结全部在线账号为动态 `N`，再逐项执行 A -> B -> C；A 失败项仍留在 `N`，健康槽不重登。全量只有一个 frozen batch，生产 SSH runner 每次 `run --max-accounts 10` 最多推进 10 个非终态项；非尾 chunk 恰好 10 个，剩余少于 10 时才允许 1–9 个尾批。每次开始前仍须复核 release/schema/runtime off/global unknown/MY client/批次守恒。MY 节点仅对需要 C 登录的账号逐个串行，App/host rate bucket、账号 lease、unknown stop gate 和批次守恒同时生效。任何 A 漂移、意外设备、failed/manual/reconcile_unknown 或 SSH 未知结果立即停止整个 batch，禁止换号补足 10；只能在原 item/operation/generation 对账或修复后继续。批次只在 `reconcile_unknown=0`、全部账号/B/C outcome 均为 `already_qualified|succeeded` 且三组各自守恒为 N 时结案。
8. `P4 retained retirement`：ABC 槽位完成不自动撤销旧 SV App C。只有新 MY 双副本、双机恢复密钥、MY inventory 和隔离恢复闸门通过后才允许创建独立退役；current SV 登录未严格超过 48 小时则退役项 skipped，不增加第四套 App、不停止正常 SV 业务、不创建自动等待任务。

产品功能模式为 `off -> read_only -> provision -> emergency_reauthorize`，只能逐级开启。`drill_wake` 只能由管理员显式创建，不存在定时 MY probe。回滚只停止新 claim，不删除 Telegram 授权、MY 包、保护或 unknown。

## 7. QA 与生产验收

### 7.1 数据与并发

- 同账号同槽并发迁移只有一个 operation 成功；同账号 provision/migrate/drill_wake/emergency_reauthorize_primary/local_activate/restore_sv_pair/decommission/remote_device_revoke 执行阶段只有一个 control lease。
- `logical_slot=standby_2` 在数据库 gate、resolver、API 和 Gateway 四层都无法成为 current，SV 无法获取其 Session。新 primary 使用更高 primary generation，不改写 standby_2 slot/bundle generation。
- SV candidate secret commit 只写 `central_business` Session；MY wake bundle commit 只写不可被 SV 解密的不可变双副本/恢复密钥/restore manifest 与 receipt，并按冻结 account/logical slot/generation/operation/authorization/MY egress version 同事务写 active assignment。两类提交重放返回原 receipt，不同 digest 或 copy manifest 永久冲突。
- 配置 readback 必须恰好得到一个硅谷 `primary_regular` 和一个 MY `standby_my` active 出口；primary 与 standby_1 都绑定前者，后者没有业务 permit。第三个 active 业务出口、轮换池或未知出口使 provision/emergency_reauthorize 模式不能开启。
- 使用 `N > 50` 的代表性数据集（例如 200 个账号）验证：全部账号项冻结到同一 `target_set_fingerprint`，均可引用同一个 MY egress，不因 `N` 超过某个固定值拒绝创建。跨页查询和多次续跑不丢项、不重项，顶部 `dr_outcome_counts` 总和始终等于 `N`；节点同一时间只执行一个登录 operation，并发领取被 owner/CAS 拒绝，恢复时不重做已成功项。
- 对 `complete_online_abc` 使用在线数在 preview 前后变化的用例验证：创建事务冻结的 `N` 不漂移；A probe 失败、冻结后删除、B 已健康、C 已健康、仅缺 C、B/C 都缺各占一个明确账号 outcome，账号/B/C 三组总数分别恰好等于 `N`。篡改前端只提交当前页或过滤 A 失败账号必须返回 fingerprint conflict。
- 同一账号严格验证 A -> B -> C：B/C 两个 flow 的 code source 都是冻结 A，各自 challenge 不能复用；A 在 B 后漂移时 C 登录 RPC 数量为 0；hash=0 可由另一 SV peer 观察得到非零 hash，但审计仍显示 A 为码源。B/C 已健康时 login RPC 数量为 0，readback 通过后写 `already_qualified`。
- 10 账号 canary 必须使用同一冻结 manifest，不能用两个不同 10 账号批次拼接 B/C 成果；失败账号不能替换。验证 10/10、正式 A 漂移 0/10、十条 Saved Messages 远端 ID、runtime off、unknown=0、MY client=0；任一保护漂移或发送证据缺失阻断全量 preview，不测试固定等待时长。
- 全量 rollout 使用一个 frozen `N`，连续调用 SSH runner，每次 `--max-accounts 10`；验证非尾 chunk 精确 10、尾批 1–9、chunk account ID 不重不漏、already-qualified 不重登。第 1–10 个成功而第 11 个 unknown 时，批次停在同一第 11 项且第 12 个登录 RPC 为 0；对账后只能从第 11 项原 operation 继续。
- 对 B/C provision/migrate 的每个失败注入验证 A 零变化：A authorization/current pointer、Session ciphertext digest、App/proxy、authorization/fact/connection generation、远端 hash 集和业务发送资格前后完全一致。SSH 在远端调用后断开必须落 unknown，禁止把非零退出或无输出推导为 no-effect。
- 分页中途改变某项 status 不改变 item ID 顺序或 cursor 中的 target fingerprint；全量无筛选遍历覆盖恰好 `N` 条。低 item ID 的 waiting 项在 `next_retry_at` 到期后仍能被领取；两个同时运行的大 `N` 批次按 `last_claimed_at` 轮转，不存在单向 cursor 跳过或单批次长期饥饿。
- 预检后新增、删除或修改入选账号使 fingerprint 改变时，创建返回 `409 security_batch_target_changed` 且不写部分批次；目标为空时返回 `422 empty_target_set`。创建提交后删除账号只把对应项投影为 `skipped_after_freeze`，不改 `N` 或 fingerprint。
- 同一 AuthKey 的复制、重新序列化或不同 App/代理变体均被唯一性检查拒绝；`AuthKeyDuplicated` 使目标授权 invalid，不产生 egress reputation signal。
- 三槽必须对应三个不同 Developer App ID/api_id 快照、三个不同 AuthKey 指纹和三个不同非零 remote authorization hash；任意两槽复用 App/AuthKey/hash 时均不计 `three_slot_independent`。primary/standby_1 使用同一 SV IP 不影响两设备独立性。
- Developer App 配置 readback 必须恰好存在 A=`primary_sv`、B=`standby_1_sv`、C=`standby_2_my` 三个 active role，三者 App ID 不同；缺一、重复或第四个 active role 都阻断新三槽/迁移 operation。迁移 preview 冻结 assignment/credentials version，期间配置漂移返回 409。
- Developer App 列表从授权资产与非终态登录 operation 统计不同账号占用；同账号 App C 的旧 SV standby_2、新 MY candidate 和 retained 旧代并存只计一次，standby_1/standby_2 必须计入。并发分配到最后一个名额只有一个成功，另一个返回 `developer_app_capacity_shortfall`。
- current authorization、账号 Session/App/proxy 兼容投影、fact version 与 connection generation 必须同事务 CAS；制造任一漂移时 resolver 和 Gateway 都拒绝新调用。
- 每个 Gateway-bound Attempt 在 RPC 前冻结 authorization/fact/connection/environment/proxy/fence；切换后旧 Attempt 字段不变，新 Attempt 只能使用新 current。固定授权 assignment 必须释放并创建新 assignment，不能原地改绑。
- 本地 fsync、SSH 镜像上传、两份读回、恢复密钥解封、源 client 断连、隔离恢复 probe、中心 receipt 请求前/中/响应丢失和 receipt 后崩溃均不二次登录；无 receipt 形成 orphan，有 matching receipt 或 MY inventory entry 时续跑。测试 Session SDK 的可写路径必须是 operation 临时态，最终本地/SSH 镜像 bundle bytes 在 probe 前后 digest 完全一致。
- 同一 login-input grant 只能被绑定 node/owner/generation 消费一次；过期、重放、输入版本漂移和响应丢失均不返回第二份材料，只有无设备/receipt 的 readback 可提升 grant generation。
- QR start/check 只能命中当前 provision/migrate operation 和 MY owner；刷新后旧 payload、旧扫码回调和旧 2FA 回调全部拒绝。主运行面旧 `/authorizations/login/start|qr/check` 无法生成 MY candidate，QR payload 不出现在数据库、队列、日志或审计正文。
- 手机号/App 凭据与 owner-domain assertion/environment binding/client identity/node/egress/secret/policy 任一 version/digest 漂移都使 login-input 与 qualification 失败；`qualification_basis_fact_id` 只能指向包含完整比较版本的不可变 qualification fact，没有批准 App owner-domain assertion 不得判定 failure-domain 隔离。
- primary/standby_1/QR/批量登录/清理、MY provision/drill/emergency wake 并发时只有一个 control lease；外部设备变化进入 unknown。
- 存量 preview/apply 拒绝 fingerprint 或 expected old value 漂移；多主、角色冲突、零 hash、无历史 usage 分别进入明确 blocker，不伪造 generation、MY assignment、健康或 anomaly。
- App C 旧 SV standby_2 迁移时，新登录必须发生在 MY 并产生不同 AuthKey/非零 hash/更高 generation；复制 Session、在 SV 重新登录或出现多个 after 差分均不得提交。slot CAS 前故障保持旧备份 current；CAS/receipt/inventory unknown 同时保护新旧且不二次登录。模拟本地盘损坏时必须能从 SSH 镜像+恢复密钥 readback；模拟SSH 镜像损坏时必须从本地副本写更高 bundle generation，不能覆盖旧对象或重新登录。
- MY slot CAS 后旧 SV standby_2 投影为 `platform_retained`。双副本/恢复密钥/inventory/隔离恢复任一未通过时为 `migration_cutover_complete_recovery_blocked`，退役 RPC 数量必须为 0。恢复闸门通过但 current SV executor 登录时间未严格超过 48 小时、时间缺失或不可用时状态为 `migration_cutover_complete_retirement_skipped`，设备计数为 3 个 current + 1 个 retained，不自动重试；最终只有运营后续退役使旧 hash 消失、三槽 current hash 完整、MY 双副本/恢复密钥/inventory/restore probe 与 client=0 全部通过才写 `migration_succeeded + rollback_window_closed_at`。
- 旧 SV 撤销前注入 MY 恢复失败时，按更高 slot decision generation 前滚回仍 active 的旧 SV，不恢复旧数据库、不覆盖 MY 包；旧 SV 撤销 readback 后，同一回滚请求必须返回 `migration_rollback_window_closed`。
- resolver 永不返回 candidate/retained/repair/invalid/unknown；账号兼容投影漂移时 fail closed。
- resolver shadow diff 未归零、旧 role-first/账号 Session reader、直接 switch writer、SV 可读 standby_2 Session 或 MY 可读紧急 2FA 时，`provision/emergency_reauthorize` 模式不能开启。
- 混合版本演练中，未携带当前 contract epoch/operation identity 的旧 writer 被 DB gate 拒绝；恢复密钥策略 version/grantee digest 漂移、旧角色 decrypt-denied 未证明、低 capability/stale 未处置实例、旧 client 非零或分区 egress ACL 未收口时 cutover apply 失败，旧进程不能绕过 Gateway 直连 Telegram。
- provision/migrate 在首个 login/QR RPC、drill_wake 在首次 Session 连接、emergency_reauthorize_primary 在 fence CAS 与 SV `send_code_request`、撤销在 reset RPC 前分别持久化副作用字段；写入失败不得继续。已发码或 grant 已消费后只能 reconcile。

### 7.2 节点、出口与健康

- 伪造节点标签、过期 heartbeat、非 MY 出口、无 assignment 或 node/egress version 漂移均无法领取或连接。
- MY 状态只能由创建或显式 wake fact 投影；无 operation 时 active client/lease 必须为零。`dormant_ready` 超过展示年龄只变为 `wake_probe_required`，不得触发自动连接；standby_2 永不退出休眠槽位成为业务授权。
- 不再存在 App 复用的降级覆盖；只有三套 App、三个 AuthKey 和三个非零 hash 全部独立才计 `three_slot_independent`。
- 更换 MY 固定出口不属于一期常态操作；必须先停止新登录和显式唤起，创建显式迁移计划并完成全部受影响授权的新出口探测后，才允许按更高 assignment generation CAS，期间不得同时保留两个 active MY 出口。
- 出口变更发起人不能自批；版本或 assignment/usage fingerprint 漂移不 apply，disable 只停止新领取且不删除历史关联，retire 在 assignment/usage/lease 未清零时失败。
- HMAC 轮换期双写/双读且只比较公共 key version；无公共版本进入 uncomparable，不产生 anomaly；覆盖 readback 前旧 key 不能退役。
- 当前业务授权、standby_1 和非业务 standby_2 分别只能投影 primary_authorization_healthy、standby_ready、dormant_ready|wake_probe_required|wake_verified；资源版本或 basis fact 漂移时对应资格立即失效。
- 时间流逝、计划任务、页面刷新和普通健康检查都不能创建 MY wake permit 或连接；只有 provision/migrate/repair/drill_wake/emergency_code_source 可使 MY active client 从 0 变为 1，operation 结束必须回到 0。
- `drill_wake` 必须走 `requested -> claimed_by_my -> drill_waking -> wake_verified -> my_disconnected_and_fenced -> succeeded_dormant`；全过程 current pointer、账号 RPC fence、Action/Attempt 与 SV runtime 不变，断连/fencing 不完整时不得成功。
- `emergency_reauthorize_primary` 必须先创建 intent 并产生同 operation/generation 的 primary 与 standby_1 两条 failure fact；缺一、过期或任一路径仍可用时都拒绝。
- primary 权威失败且 standby_1 即时 probe 成功时自动执行 `freeze claims -> fence old generation -> drain Gateway -> current/account projection CAS -> online/listener/sync lease invalidate -> runtime rebuild -> immediate probe -> listener reclaim -> unfreeze`；MY claim/wake/code grant 数量为 0。普通 timeout 或 probe 失败不得误切。
- local activate 解冻后必须显示 `business_runtime_status=degraded + sv_redundancy_status=degraded`；随后 logical primary 更高 generation 修复并受控切回、standby_1 再次 ready，才变为 healthy。修复失败不停止当前业务；降级期 current standby_1 再失败则进入双 SV 失败流程。
- 两个 SV 授权均失败但 SV login runtime/出口未就绪时必须停在 `waiting_sv_login_runtime`，MY client 仍为 0。MY ready 不能把 `business_runtime_status` 标成 available。
- SV `send_code_request` 后只有 `SentCode.type=app-session` 才能唤起 MY。SMS/email/call/QR 进入 `manual_required`；旧消息、challenge 之前消息、多重匹配或错误 flow 不能生成 code grant。
- code grant 只能被绑定 SV runtime 消费一次；MY 随后断连并清零。2FA 只在 SV 使用。新 primary AuthKey 必须与三个旧授权不同，其 commit/CAS/probe 完成前不解冻。standby_2 的 slot/current/bundle generation 全程不变。
- 删除测试环境的 SV `SESSION_SECRET_KEY`、原 MY 主机和本地数据盘后，替代 MY 运行环境仍能通过双机恢复密钥 与 SSH 镜像恢复并唤起；恢复旧中心库后进入 mutation hold，能按 MY inventory 最大 sequence 补回 bundle/copy/slot 事实而不覆盖 MY 新代次。
- `AuthKeyDuplicated`、单账号限制和多个账号相关出口失败分别进入不同失败码和停止范围。

### 7.3 活动观察、隐私与清理

- 新账号完成首个授权登录后，账号详情“登录设备”Tab 即可调用 GET/refresh；0 小时、24 小时和 48 小时都能查看设备，不得因清理门槛隐藏设备页。列表必须同时返回三个当前槽位、我方历史授权、external 和 unresolved 分组；MY client=0 时 standby_2 仍是 `platform_current + remote active`，不得显示为已退出。
- 我方设备只按未撤销授权资产保存的唯一非零 hash 匹配；`api_id`、App/设备名、IP、国家和 `official_app` 都不能单独使设备受保护。同 `api_id` 但不同 hash 的额外登录必须分类 external。
- 资格判断只读当前 SV authorization 的持久化 `telegram_login_at`：47:59:59 和恰好 48:00:00 都 skipped，严格超过 48 小时才 eligible；前端时钟、账号创建时间和 last active 均不能改变结果。时间缺失直接 `login_time_missing`，MY standby_2 永远不能作为 executor。
- 设备清理创建期间 Gateway/Telegram 调用数必须为 0。100/1000 个账号创建请求只做数据库读取并返回准确 `requested_count/eligible_count/skipped_count/skipped_reason_counts`；eligible 项才进入 worker，skipped 项没有 waiting、倒计时、next_retry_at 或定时重领。
- 单账号详情使用同一派生结果：未超过 48 小时、登录时间缺失、current SV 不可用和账号策略禁止分别展示明确原因并置灰按钮；不展示“预检”“重新预检”或倒计时。篡改前端 enabled 状态仍被服务端 skipped。
- current authorization/fact/login_at 在创建事务中变化时该账号 `skipped/current_authorization_changed`，不得动态改用 standby_1 或 MY。创建后 worker 使用冻结 executor；执行前版本漂移则当前项失败，不改绑。
- worker 对每个账号单独读取 `account.getAuthorizations`，并把执行开始时 observation 中所有 external 非零 hash 作为目标、全部我方当前/历史 hash 作为保护；不接受用户上传 hash，也不需要 preview。一个账号读取超时/失败时只写该项 `device_list_read_timeout|failed`，后续账号继续执行。
- 官方手机、桌面端、Web 和历史手工登录只要未匹配我方授权资产，都在一键清理目标中；不要求保留官方锚点。
- 确认页展示最后 observation 的 `official_or_manual_external_count` 与固定退出提示，并说明实际目标取 worker 执行开始时设备集；一期没有人工保留白名单，需要继续使用人工官方客户端的账号不执行一键清理。
- worker observation 中存在 unresolved 或保护 hash 不完整时当前项零撤销并 failed。执行后只有全部执行快照目标缺失、全部 protected hashes 仍在且没有新增 external/unresolved 时才成功；执行中新增 external 不自动纳入已冻结执行快照，记 `partial_failed/new_external_detected_after_apply`；RPC unknown 只对账，不重复 reset。
- 严格超过 48 小时后仍收到 `FRESH_RESET_AUTHORISATION_FORBIDDEN` 时当前项直接 `failed/telegram_fresh_reset_rejected`，不 waiting、不自动重试；已有部分撤销时完成 readback 并记 partial_failed。
- 48 小时 skipped 不改变 `business_runtime_status`、standby_1 健康、MY provision/migration 或任务可用性；接码专用账号仍固定拒绝一键清理。
- API、页面、审计、队列和日志均不出现完整 IP、手机号、API Hash、验证码、2FA 或连接凭据；login-input fetch 除 ciphertext 外无敏感响应字段，原始 IP 密文到期删除后永久 anomaly 摘要仍可验证。
- primary、standby_1、MY standby_2、candidate、retained、repair、invalid、unknown 与可证为我方登录产生的 orphan 全部保留；任一我方 hash 缺失、保护漂移或 operation 未终态时清理失败。
- 通用清理不能命中已提交 Session、receipt、assignment/usage、activity/review、probe 或 protected ref。
- 备份恢复演练可读回 backfill/cutover、contract epoch、runtime capability、App assertion、qualification 与 grant 收据，并从 MY 追加 inventory 重建中心缺失的较高 bundle/copy/slot generation。旧实例先失去 egress permit，提升 incarnation/epoch 后所有旧 lease/permit 失效；全量对账前 provision/migrate/decommission/erase/设备清理均被拒绝，健康保持 stale。
- 故障注入必须覆盖本地文件写后崩溃、父目录未 fsync、对象上传 ack 丢失、对象读回摘要不一致、恢复密钥 暂时不可用、restore probe 失败、slot CAS ack 丢失、CAS 成功但 `slot_commit_observed` 未写、中心库回滚和擦除部分成功；prepared decision 恢复时必须幂等前滚，所有场景均证明旧可恢复代次未被覆盖、可恢复副本未被通用清理删除、unknown 不被写成 succeeded/retired。

### 7.4 break-glass、切换与业务模块

- 普通 approve 拒绝发起人自批；break-glass endpoint 只接受 intent 创建后由服务端绑定的同 operation typed primary 与 standby_1 双失败事实，并拒绝无专用权限、强认证、incident、新鲜事实、原因或审批人快照的请求。
- 单人 break-glass 只能豁免审批人分离；target/account_unknown anomaly、目标不健康、Gateway unknown、MY fencing、fence/CAS 失败均不能切换。只有 confirmed other_remote_device anomaly 可携带 review/version/incident/reason 显式覆盖，原保护不解除且产生 P0 复盘。
- break-glass 同事务产生 P0 和 48 小时 review；发起人不能自结案，逾期阻断新的切换类 operation。
- 紧急重建前新 Action claim、listener 和安全 mutation 均被账号 fence 阻断；新 primary commit/CAS/probe 完成前不产生业务 RPC。
- MY code source 只能由 MY owner 执行；MY 节点不可用时保持 `malaysia_wake_unavailable`，SV 不得代写 wake fact，不得获取 MY Session。
- login-code grant 响应丢失不能重取；SV `finish_login` 的远端效果未证明不存在前，不能请求新验证码。若已产生远端新设备但 commit 未知，必须保护 orphan/candidate 并对账。
- 未领取 Task 和未调用普通 Action 保留业务义务，由新 Attempt 冻结新 current；已领取未进 Gateway 的旧 Attempt 结束或释放后重领，不原地换授权。
- 搜索点击/入群等固定 authorization/environment/proxy assignment 在 Gateway 前释放并重排；无法重排进入 waiting/shortfall。已进 Gateway 的 Attempt 保留旧代次，unknown 不自动重发；已有权威 remote fact 不改写。
- listener 旧 generation 结果丢弃且不推进 cursor；online/keepalive 旧结果不覆盖 warming；联系人、群组和资料同步旧结果不得写成功；runtime summary 只聚合 current generation。每类模块都必须有 stale-generation 自动化测试。
- 账号软删除立即退出 Planner/Dispatcher/listener/online/sync 候选并禁止 MY 演练/紧急唤起；已开始 Gateway Attempt 保留对账。账号保持 `business_deleted_authorizations_retained` 可见，全部 decommission/erase readback 后才进入 `authorization_retired`。
- 马来西亚节点的运营 Action、ExecutionAttempt 和业务 remote fact 数量恒为 0。
- 存量迁移、活动调查、出口变更和 DR operation 四个 list API 均验证稳定 cursor、筛选、状态计数、空页、跨租户与逐项权限；页面展示脱敏事实、版本与 blocker，篡改可用性或 ID 不能绕过审批、CAS、保护和 readback。

### 7.5 分层完成证据

| 层级 | 证据 |
| --- | --- |
| 配置完成 | 唯一 MY 节点、硅谷/MY 两个固定出口、现有三套 App 的 A/B/C 唯一 slot assignment、App credentials/owner-domain assertion、environment/client metadata、权限、出口 registry/secret version、schema 和 HMAC key version可读；不存在第四个 active role |
| 存量迁移完成 | logical slot/region 分类、preview fingerprint、apply/readback、resolver shadow diff、contract epoch、DB gate、恢复密钥策略/grantee/decrypt-denied、旧 client drain 和分区 egress ACL 全部通过 |
| 授权完成 | MY 不可变本地副本 fsync、独立 SSH 镜像、两份写后读/摘要/恢复密钥解封、SSH 镜像隔离恢复 Telegram probe、MY inventory/中心 receipt、UID/AuthKey/hash、protection、assignment、源/恢复 client 断连收据全部通过 |
| MY 槽位切换完成 | App C 在 MY 产生新 AuthKey/hash/generation，双副本/恢复密钥/inventory/restore probe、bundle receipt/qualification/slot CAS 完成；旧 SV 授权 retained+protected。此时可处于等待退役，不等于最终迁移完成 |
| 迁移最终完成 | `migration_recovery_gate_passed` 后，旧 SV standby_2 远端 hash 已由固定 current SV executor 撤销并 exact-set 回读消失，三个当前槽位仍完整，MY `recoverable_copy_count=2`、恢复密钥/inventory/restore probe 有效且 client=0，assignment/usage 账一致，并写 `rollback_window_closed_at` |
| SV 本地切换完成 | standby_1 即时 probe、业务冻结/Gateway drain、current/account projection CAS、旧代次失效、online/listener/runtime summary 新代次重建和任务解冻通过；状态仍为 redundancy degraded |
| SV 1 主 1 备恢复 | logical primary 更高 generation 通过资格与 probe，受控切回完成，standby_1 再次 ready，所有业务模块绑定同一 current generation |
| 紧急读码完成 | 双 SV 失败事实、SV login runtime ready、app-session delivery、MY wake、challenge-bound code grant 消费、MY 断连/fencing 和清零收据通过 |
| 主授权重建完成 | SV 新 AuthKey/Session candidate、UID/AuthKey/设备证明、primary generation CAS、兼容投影、SV runtime lease 和即时 probe 成功；standby_2 不变 |
| 账号业务删除完成 | 全部业务候选与 claim 已停止，已开始 Attempt 保留对账，账号显示 `business_deleted_authorizations_retained`；不代表授权退役 |
| 账号授权退役完成 | 全部平台授权撤销 readback、MY 本地/硅谷 SSH 镜像/中心元数据与 wrapped DEK 分步 erase receipt、无 unknown，账号显示 `authorization_retired`；共享恢复密钥仍被其他 bundle 引用时保持 |
| 发送恢复 | 另有任务类型权威远端事实；不由本 PRD 自动宣称 |

CI、容器健康、Session 非空、数据库行、页面 `2/2`、授权 probe 或出口连通均不能替代发送恢复。

## 8. Product Design Complete 与开发交接

### 8.1 当前实现差距（2026-08-22 本地 release 对比）

| 当前实现 | v2.21 差距与交接结论 |
| --- | --- |
| `backend/app/integrations/telegram/gateway.py` 已把 Telegram authorization 的 `date_created/date_active` 写入 snapshot | 复用该字段并为 current SV authorization 持久化 `telegram_login_at`；批量创建只读数据库，不再为资格连接 Telegram |
| `backend/app/services/account_security/service.py::_fresh_session_wait_until` 已选择 `is_current_session=true`，但按 24 小时返回 wait，`date_created` 缺失时返回 `None` | 改为严格 `server_now > telegram_login_at + 48h`；不足或缺失直接 skipped，返回原因，不创建 wait；不动态挑 standby_1/MY |
| 当前 security batch 在收到 `FRESH_RESET_AUTHORISATION_FORBIDDEN` 后写 waiting，并由 worker 到期自动重试；`backend/tests/test_account_security.py::test_waiting_account_security_item_is_retried_when_due` 固化了该行为 | 删除 waiting/到期自动 retry 测试；超过 48 小时仍收到 FRESH 时当前项 failed，已有部分效果先 readback，批次继续下一账号 |
| `frontend/src/app/types/accounts.ts` 已有 `date_created`，`frontend/src/app/views/AccountModals.tsx` 在“账号安全”内容中已有登录设备表 | 改为账号详情独立“登录设备”Tab，展示 current SV 登录时间/时长和置灰原因；不增加预检按钮、倒计时或 waiting 状态 |
| `_auto_standby_developer_app` 当前从健康 App 中自动选择，未表达 A/B/C 固定 slot assignment | 先回填线上现有三套 App 的唯一角色映射；新账号与迁移冻结 assignment/version，App C 只在 MY 创建 standby_2，不申请第四套 App |
| 当前测试未证明 App C 旧 SV standby_2 的 MY 新登录、slot CAS、3+1 retained 和到龄退役全链路 | 按 7.1、7.3 新增状态机与回归；代码、测试、部署和生产 Telegram readback 在完成前均保持未证明 |
| 当前没有 MY wake bundle 双副本、双机恢复密钥 readback、追加 inventory、隔离 restore probe 或分步 erase 模型 | 新增 bundle/copy/inventory/restore fact 与不可变代次实现；完成单主机/数据盘故障、中心库回滚和部分擦除故障注入前，不得声明 Session 零丢失或迁移最终完成 |
| release 已实现迁移批次、unknown 原字节对账、typed phone-ban 投影和 guarded `local_activate`，但没有 `complete_online_abc` action、账号+B/C 三重守恒、A 码源 fence 或同一 10 账号观察窗 gate | 新增批次 schema/read model/orchestrator/API/UI/指标与测试；实现完成前不得创建 10 账号或全量在线 ABC 批次，不得用既有 271 迁移批次替代 |

### 8.2 冻结设计与开发拆分

v2.23 当前一期范围：硅谷是唯一业务面和唯一业务 IP，primary/standby_1 均在 SV；primary 权威失败且 standby_1 即时 probe 通过时自动本地切换，切换后恢复 logical primary 并重建 SV 1 主 1 备。MY 只有一个休眠计算节点和一个固定 IP，但 Session 持久化不得依赖该单机：standby_2 必须具备 MY 本地卷与独立 SSH 镜像两份不可变副本、双机恢复密钥 readback能力、MY 追加 inventory 和 SSH 镜像隔离 restore probe，永不晋升、永不交付 Session、永不运行业务。线上现有且只使用三套 Developer App，固定为 App A/SV primary、App B/SV standby_1、App C/MY standby_2。全量在线补齐固定先冻结动态 `N`，再由 A 作为 B/C 唯一码源逐账号按 A -> B -> C 推进；健康槽只 readback，A 失败仍留在分母。10 账号同一 manifest、零 unknown、A 无漂移和十条发送远端 ID 通过前不允许全量；不设置固定观察时长。设备清理、账号退役、中心恢复 hold 和 unknown 收口合同继续保留。

1. DEV-A：additive schema、logical slot/region、App A/B/C slot assignment/assertion/qualification/resource version、observation `date_created`、current SV `telegram_login_at`、cleanup requested/eligible/skipped/reason 与 executor 冻结字段、硅谷/MY 固定出口、wake bundle/copy/inventory/restore fact/permit/code grant、protected ref、授权代次/probe/failure/operation、receipt、RPC/control lease、current/runtime/lifecycle 原子投影、resolver 和兼容投影。
2. DEV-B：backfill、runtime registry/contract cutover、恢复密钥策略/grantee/decrypt readback、DB mutation gate、resolver shadow、旧 reader/writer/2FA 封禁和 Telegram egress ACL。
3. DEV-C：MY mTLS claim、单登录 owner、无 operation 零连接、不可变密封包 fsync/双副本写后读/恢复密钥解封、MY 追加 inventory、SSH 镜像隔离 restore probe、显式 drill/emergency-code-source、operation-scoped QR、固定 MY egress、orphan/revoke 和 exact-set reconciliation。
4. DEV-D：账号详情“登录设备”读模型、立即可见与 loading/refreshing/置灰原因/执行状态、refresh/direct-cleanup/detail API、本地严格 48 小时分流、批量 requested/eligible/skipped/reason、worker 单账号读取超时隔离、四分类与 hash 归属校验、人工客户端退出提示、Developer App 授权占用、两阶段账号删除、operation/candidate CAS、逐 hash reset 和 exact-set 最终回读。
5. DEV-E：自动 local activate、restore SV pair、ExecutionAttempt/Gateway 授权冻结、固定 assignment 重排、online/listener/sync/runtime summary generation fence、RPC fence/incarnation/runtime generation、Gateway drain、双 SV failure fact、`waiting_sv_login_runtime`、`SentCode.type` 分流、challenge-bound 读码/code grant、MY 断连、SV 新 primary commit/CAS/probe、unknown 对账和 break-glass。
6. DEV-F：`complete_online_abc` additive schema、动态在线 N/fingerprint、账号+B/C 三重守恒、A 码源与 observer 分离、A -> B -> C orchestrator、批次 API/UI/指标、10 账号 manifest 与即时 A/发送证据 gate；复用现有 B provision 和 C migrate 内核但不复用旧批次成功口径。
7. DEV-G：新账号三 App 登录与设备立即可见、47:59:59/48:00:00/严格大于 48 小时边界、缺失登录时间、无 Telegram 资格调用、单项读取超时继续批次、App C SV-to-MY 迁移及 recovery-blocked/3+1 retained/rollback-window 状态、本地切换任务族 canary、MY 主机/数据盘故障恢复、中心库旧备恢复、部分擦除故障注入、账号删除/退役、10 账号与全量生产 readback 和任务类型 E4。

实际节点 ID、域名、证书和 secret 引用属于部署参数，P0 上线前录入并验证。完成文档不代表代码、QA、部署或生产恢复完成。

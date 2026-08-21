# 马来西亚异地备用 TG Session 灾备 PRD

> 版本：v2.21
> 日期口径：2026-08-22（Asia/Shanghai）
> 当前状态：`design_status=complete`、`product_resync_status=complete`、`dev_handoff_ready=true`、`implemented_scope=abc_two_account_canary_core_local_qa`、`core_deployed=pending_release`、`ssh_mirror_deployed=true`、`slot_canary=2/2_historical_pass`、`full_online_abc_design=complete`、`full_online_abc_implementation=partial`、`full_prd_implementation=partial`、`runtime_mode=off`、`production_fixed=false`
> 适用范围：账号授权资产、三槽位远端设备归属、活跃授权设备查看/清理、备用登录、硅谷本地自动切换、跨模块运行代次、显式演练、紧急登录码辅助和硅谷主授权重建；不包含业务系统整体异地容灾。
> 关联文档：[实施与验收合同](account-malaysia-standby-session-dr-implementation-contract.md)、[account-standby-auto-authorization-prd.md](account-standby-auto-authorization-prd.md)、[account-security-hardening-design.md](account-security-hardening-design.md)、[account-login-group-navigation-recovery-prd.md](account-login-group-navigation-recovery-prd.md)。

## 1. 背景与问题

当前账号已定义 `primary`、`standby_1`、`standby_2` 三类授权资产。核心运行关系固定为：硅谷是唯一日常业务运行面，`primary` 和 `standby_1` 都使用硅谷固定出口；`standby_1` 解决主授权自身失效后的本地快速恢复，不解决硅谷服务器或区域故障。`standby_2` 在马来西亚真实登录生成，平时不保持 Telegram 连接，只是异地紧急授权资产。线上现有且只使用三套 TG Developer App：App A、App B、App C 是新账号默认的 `primary`、`standby_1`、`standby_2` 分配，历史主备切换后同一账号的 A/B 可以互换角色；每个账号的三个当前槽位仍必须使用三个不同 App，存量迁移源必须是冻结的 App C `standby_2`。三个槽位都必须真实登录，对 Telegram 而言是三个独立授权设备，不是一个 App 配置或一份 Session 的三个标签；本次迁移不申请第四套 App。

### 1.1 2026-08-20 生产只读基线

- 当前未删除账号 1335 个；线上恰有三套 active Developer App。
- 存在健康 App C/SV `standby_2` 的迁移候选 391 个。其中 276 个为 App A 当前主 Session + App B/SV `standby_1` + App C/SV `standby_2`，可直接进入 MY 迁移 preview；两账号 canary 从这一组冻结。
- 另 115 个为 App B 当前主 Session + App A `standby_repair` + App C/SV `standby_2`。App A repair 行仍有 Session，但在使用当前 SV Session 和 repair Session 分别读取同一 Telegram UID、确认不同 AuthKey 并完成 CAS 转正前，不计作 SV `standby_1`，不得迁走 App C。
- 全量分母因此必须分两段守恒：先迁移具备双 SV 授权的账号；对 115 个 repair 账号执行正式 preview/fingerprint/apply/readback，成功恢复 SV `standby_1` 后再纳入迁移。失败项保留原 repair 和 App C/SV Session，不缩小原始分母或伪装完成。

同时必须避免三个错误理解：

- 复制 StringSession 不是新授权，会复用同一 AuthKey，并可能触发 `AuthKeyDuplicated`。
- Session 非空、数据库 `active`、worker healthy 或页面 `2/2` 都不是当前 Telegram 授权可用证据。
- Telegram 远端“活跃授权”只表示该设备尚未被撤销；MY client 断连休眠后，`standby_2` 仍应出现在 `account.getAuthorizations` 中，不得因“不在线”标为授权缺失。

### 1.2 2026-08-21 生产实施事实

- 账号 27、28 已分别完成 App C/SV `standby_2` 到 MY generation 2 的槽位提交；两项均读回 MY current、双副本 `2/2`、恢复密钥解封与隔离 restore probe passed，旧 SV App C Session 保持 `retained + protected`。这只证明 `slot_canary=2/2_pass`，不等于旧 SV 远端授权已退役或完整 PRD 已完成。
- 271 项扩量批次曾停在 `241 succeeded + 22 failed + 5 manual_required + 3 reconcile_unknown = 271`；经 typed no-effect、原字节前滚和不可恢复旧工件人工收口后，2026-08-21 已验证终态为 `241 succeeded + 23 failed + 7 manual_required + 0 reconcile_unknown = 271`。其中 22 个不同账号具有 `phone_number_banned` typed fact；22 不是全平台账号总数，也不是全平台永久封号总数。
- 全局 unknown 曾涉及账号 24、25、26、67、87、111：24/25 为无包 remote orphan，26 为 MY local-only 包，67 无新远端设备，87 为 inventory ahead of central，111 三条 SV Session 均不可授权且远端未证明。最终收口只允许复用原字节/原 operation/generation 或转人工，不调用重登 RPC、不伪造新 Session；该历史分类继续作为回归用例，不再描述为当前 open unknown。
- 当前已验证发布基线为 release SHA `8406a8125577de16da4227269a7f2046ba07425f`、Alembic head `0159_dr_repair`、runtime=`off`。已部署范围包括 `standby_2` 迁移状态机、MY 专用节点、SSH 双副本、恢复探测、slot CAS、旧源保护、unknown 原字节对账、typed phone-ban 投影和 guarded operator `local_activate`。
- 自动故障触发的完整 `local_activate`、`restore_sv_pair`、`drill_wake`、双 SV 失效后的 `emergency_reauthorize_primary`、中心恢复对账、decommission/erase、跨 Action/Gateway/listener/online/sync generation fence 与 `complete_online_abc` 仍未完成；既有 unknown 收口或 batch success 不能替代这些验收。
- 授权备份不等于消息、任务、数据库、Redis、素材和 Dispatcher 的异地容灾。

### 1.2.1 unknown 修复的冻结事实与放行边界

- 六个历史 unknown 在对账前冻结分类为：24/25=`remote_orphan_without_bundle`；26=`local_only_bundle`；67=`remote_no_new_device`；87=`inventory_ahead_of_central`；111=`remote_unproven_all_sv_sessions_invalid`。24/25/26/87 禁止重新登录；24/25 orphan 在唯一非零远端 hash、精确设备集与严格超过 48 小时均证明前继续 protected；111 只进入人工重新授权，不自动推导 no-effect。
- 账号 26 只允许从 MY 原 operation/generation 的本地不可变字节补写 SSH 镜像、追加 inventory、提交中心 receipt、执行隔离 restore probe 与 slot CAS；账号 87 只允许从已一致的本地+SSH 镜像和 inventory 前滚中心 receipt/probe/slot。任何 digest、operation、generation、Telegram UID/AuthKey 或 App C 身份不一致均保持 unknown。
- 账号 67/87 当前业务 primary 已由 Telegram 判定不可授权，但各自 SV `standby_1` 已完成真实授权/身份探测。`local_activate` 必须在账号与目标授权行锁内复核 expected generation/fact version，写 `current_authorization_id`，同步 legacy Session/App/proxy 投影，提升 authorization/fact/connection generation，清空旧 online Session 身份并重建为 warming；旧主授权保留为 repair/protected。切换后必须分别完成新 current 登录与 Saved Messages 发送读回，不能用数据库状态代替。
- 本轮 22 个 `phone_number_banned` 是该 271 迁移批次内 22 个不同账号的 Telegram typed 事实，不代表全平台总封禁数。每条 typed fact 必须把对应账号投影为 `已封禁`，保留 Session 密文和审计，不删除账号；未具备同类权威事实的其他账号不得批量推导为封禁。
- 下一批 10 个账号仅在 `global_reconcile_unknown=0`、上述逐账号结果已读回、迁移 runtime 仍为 `off`、22 个封号投影守恒、67/87 新 current 可登录且可发送、111 明确 manual_required 后才允许新建独立 preview；这些前置事实已满足不等于可以直接复用旧迁移批次，仍必须先实现 v2.21 的 `complete_online_abc` 合同。10 项逐项 outcome 守恒，出现一个新 unknown 即停止。

### 1.2.2 2026-08-22 产品 resync 结论

- “所有在线账号都登录 A/B/C”固定解释为：批次创建时冻结的全部在线账号都必须具备可验证的 A/SV、B/SV、C/MY 三槽授权；已健康槽位只做新鲜 readback，不为满足动作标签强制重新登录。A 是正常补齐 B/C 时唯一允许选择的登录码来源；B 只承担 SV 本地故障切换，C 只在 A、B 两条权威失败事实同时成立时辅助重建 A。
- 线上在线数量会变化，因此 1064/1065 只作为 2026-08-22 的只读时间点参考，不进入规范性目标。正式全量分母必须在批次创建事务中先冻结为动态 `N`，之后才逐项探测 A；A 探测失败、账号被封、需要人工、被冻结后删除或出现 unknown 都必须留在 `N` 中，禁止先探测后缩小分母。
- 2026-08-22 最近一次只读本地投影为：在线 1064、A Session 投影 1064、B ready 245、C/MY ready 237、ABC ready 237、仅缺 C 8、同时缺 B/C 819。该投影只用于估算工作量，不是 Telegram E4，不得直接据此创建成功结果。
- 本次新增的是 `complete_online_abc` 产品批次合同、10 账号 canary 和全量 Release Gate；既有生产代码尚未实现该合同。本地文档合并、代码测试、生产发布、Telegram 授权可用与消息发送必须分别验收。

### 1.2.3 两账号修复 canary 的实施切片

- 本切片只允许两个精确账号逐个执行，且只选择 canonical A 已显式建模、B 缺失、已有健康 App C/SV 迁移源的账号。它补齐 A 保护的 B 登录和 C 迁移编排，不冒充全量 `complete_online_abc`。
- 两账号执行前先运行 DB-only canonical A backfill：preview 冻结全部未删除账号的 Session 密文摘要、App/proxy/current pointer 与三组 generation 并生成 fingerprint；apply 需要异人审批且只为已有可解析 Session 的账号创建与 legacy Session/App/proxy 完全一致的 `primary/SV/current` 授权行，不连接 Telegram、不改 Session 和 generation。缺失/不可解析 Session、已有 current 冲突必须逐类留在 readback，禁止伪造 A。两账号候选再逐个执行显式 A identity qualification，Telegram 探测前后 fingerprint 不变才只补 UID/AuthKey/device hash 事实；该探测不能切主或替换 A。
- preview 只读数据库并冻结 A authorization、Session/App/proxy、authorization/fact/connection generation、App B assignment、SV proxy 和幂等键。apply 前异人审批；B/C 的验证码都只从冻结 A 读取，并绑定 challenge 时间与 Telegram service message ID，验证码不写 operation、argv、stdout 或审计。
- B 登录即使已托管 2FA 密码，也必须先在同一临时 Session 提交本次绑定验证码；Telegram 返回 `SessionPasswordNeeded` 后才可提交 2FA。不得因密码已知而跳过验证码。若历史版本在验证码提交前调用 2FA 并产生 `AuthKeyUnregisteredError`，只允许以冻结 flow 状态、异常类和发布 SHA 形成 `pre_code_submission_failure` 对账；apply 仅把 operation 收口为 `confirmed_no_effect/failed` 并清除临时 flow 密文，不创建/切换/撤销任何授权。
- 历史 A/B 角色互换账号若当前 A 已使用环境 App B，禁止再按固定 App B 新建 `standby_1`。存在 App A `standby_repair` 时必须先走 SV repair：双 Session 同 UID/不同 AuthKey 探测后提升 App A repair；若错误 App B standby 已生成，则同事务将其降为 retained/protected repair，不撤销设备、不改 A，再恢复三 current 槽 App 两两不同。
- 备份 operation 不具备切主能力。健康检查、Dispatcher、账号安全自愈和旧 activate 入口只能创建 `fault_candidate` 或保持失败，不能直接替换 A；正式切主只允许经 `local_activate` 独立 preview、双探测、代际 CAS 和异人 apply。
- MY 节点只有 capability=`2.21-abc-a-source` 且 runtime image SHA 与合同完全一致时才可领取；runtime 每次只绑定一个 C operation，C slot 成功后自动回到 `off`。任一 A 漂移、验证码歧义、remote unknown、意外设备变化或 capability/SHA 不匹配立即停止。
- 第一个账号必须完成 A 登录及 Saved Messages 发送读回、B/C 同 UID 且 AuthKey/hash 各自唯一、C 双副本/restore probe/slot commit、MY client=0 后，才允许对第二个账号重新 preview。第二个通过前不创建 10 账号批次。

### 1.3 2026-08-21 早期失败 canary 事实

- 账号 24/25 的早期迁移批次已按源授权 46/48 和目标 generation 2 冻结并审批；MY 固定出口、三套 App 槽位、SSH 双副本链路和节点心跳均先通过。该批次是失败事实，不是当前 `2/2 pass` canary。
- 两次 Telegram 登录都取得验证码并完成新设备登录，但 Telegram 对当前设备返回合法 `authorization hash=0`；旧实现把 `0` 当成缺失，在 Bundle 写入前将两项标记为 `provision_reconcile_unknown`。第二项在节点自动重启后被领取，发现后已停止节点、清空 lease，并把 runtime 切回 `off`。
- 两账号原 App C/SV `standby_2` 仍为 current、Session 非空且受清理保护；没有 candidate、Bundle、slot commit 或旧 Session 清理。新产生但未封装的 MY 远端设备属于我方 orphan，保持 unknown，不自动重试或清理。
- 修复口径不是把 `0` 直接存为我方设备 hash。MY 发送不含明文设备信息的当前设备指纹摘要；SV 用仍保留的 primary/standby peer Session 读取远端设备集，只有唯一指纹匹配得到非零 hash 时才能提交 Bundle。零匹配或多匹配继续进入 reconcile unknown。

本方案把 `standby_2` 改造成在马来西亚真实登录生成、由 MY 本地独立密封保存的授权。它只承担授权保全、显式演练，以及在两个硅谷授权均不可用时读取 Telegram 官方登录码，辅助硅谷生成全新 `primary` Session。`standby_2` 永不交给硅谷、永不切为 `current`、永不发送业务消息。

## 2. 决策与范围

### 2.1 一期唯一定位

一期是“马来西亚异地备用授权”，不是“马来西亚业务执行中心”。

| 能力 | 一期结论 |
| --- | --- |
| 生成 `standby_2` | 只能由已登记的马来西亚计算节点执行 |
| 探测 `standby_2` | 不设定时探测；只在创建/修复、管理员显式演练或紧急唤起 operation 中由马来西亚节点执行 |
| 日常发送、监听、点击、加群、资料修改 | 马来西亚节点全部禁止 |
| `primary` 不可用、`standby_1` 可用 | 权威失败确认且 standby_1 即时探测通过后，在硅谷自动本地切换；不得连接或唤起马来西亚 |
| `primary` 与 `standby_1` 均不可用 | 创建 `emergency_reauthorize_primary` operation；MY 仅作为登录码来源，最终产物是硅谷新生成的 `primary` AuthKey/Session |
| 硅谷登录运行时或唯一业务出口不可用 | operation 保持 `waiting_sv_login_runtime`，MY 继续休眠且不连接 Telegram；先恢复 SV 登录条件 |
| 主数据库/协调器整体不可用 | MY 不自主启动、不执行业务；恢复控制面和 SV 登录运行时后，可使用 MY 密封唤起包恢复授权 |
| SV runtime 或区域整体不可用 | 发送、监听、任务执行和同步均不可用；MY 不能接管业务，只保留后续恢复登录授权的能力 |
| 本地切到 standby_1 后 | 业务可恢复但进入 `sv_local_redundancy_degraded`；必须修复 logical primary 并恢复 SV 1 主 1 备 |
| 历史业务数据 | 不迁移、不改写 |

### 2.2 合格灾备与出口代理的区别

- `qualified_malaysia`：创建和最近一次显式唤起探测都由马来西亚计算节点执行，节点身份、运行版本和实测出口为 MY，且与硅谷主运行面故障域不同。
- `egress_only`：主生产服务器仅通过马来西亚代理连接。可作为网络策略，但不计异地备用达标。
- `unqualified`：节点、出口、Developer App、AuthKey 或设备保护任一关键证据缺失。

“马来西亚”以执行节点身份和运行时实测出口事实共同证明，不能只信 `server_tag`、代理名称或人工标签。

### 2.3 MY 本地副本与 SSH 双机镜像

“分散到马来西亚服务器”必须是真实登录和可恢复文件，不能只在硅谷数据库中标记 `region=MY`。每个合格 `standby_2` 同时保有两份不可变密文：MY 持久卷本地副本和通过专用受限 SSH 身份写入硅谷持久目录的 `remote_ssh_snapshot`。两份副本使用同一 bundle generation 和密文摘要，分别完成写后读、摘要校验和解封校验；任一副本缺失、只写未读回或摘要不一致时，可恢复副本数不得记为 2。

每个 bundle 使用独立随机 DEK 密封。DEK 由专用 32 字节恢复密钥包装；恢复密钥不进入数据库、容器镜像、日志或普通业务环境变量，只以 root-only 文件保存在 MY 节点，并通过受限 SSH 备份到硅谷运维目录。硅谷 backend、worker 和日常业务账号不挂载该密钥；只有替换 MY 节点的人工恢复步骤可以通过 SSH 取回。wrapped DEK、恢复密钥版本摘要和真实解封 readback 进入 manifest。第二份密文及追加 inventory 必须以 create-only 方式写入硅谷 SSH 镜像目录，同机第二目录不计独立副本。

“Session 不丢失”的一期验收范围固定为：平台自身不得因原位覆盖、通用清理、发布回滚、中心数据库旧备恢复、单台 MY 计算主机或其数据盘故障而失去一个仍在 Telegram 远端 active 的可恢复 `standby_2`。该承诺不覆盖 Telegram 主动撤销/封禁，以及两台服务器、两份密文和两份恢复密钥同时不可恢复的复合灾难。没有新鲜 SSH 镜像 readback、恢复密钥双机 readback或第二密文副本时，部署状态为 `session_durability_blocked`，不得退役旧 SV Session。

唤起包只包含连接 `standby_2` 并读取 Telegram 官方登录码所需的 Session、Developer App 凭据、client metadata、授权/账号标识和 bundle generation；不包含 Task、Action、ExecutionAttempt、联系人、群组、素材、消息或 2FA。该设计使用现有硅谷和马来西亚两台服务器、两个固定 IP，不新增付费 KMS 或 OSS 服务，也不等于业务数据异地备份。

### 2.4 业务连续性边界

产品必须分开显示 `authorization_recovery_status` 与 `business_runtime_status`。MY `standby_2=dormant_ready` 只能把前者标为 `ready`，不能把后者标为可用。`business_runtime_status` 只允许 `available/degraded/unavailable/warming`：当前 SV 授权、Gateway、任务领取及该账号已启用的 listener/在线/同步模块都绑定同一 current generation，且 SV 1 主 1 备完整时才是 `available`；上述模块已经可执行、但本地切换后尚未重建 SV 1 主 1 备时为 `degraded`；SV runtime/出口不可用或两个 SV 授权均不可用时为 `unavailable`；切换后各模块的旧代次屏障尚未生效或即时 probe 尚未完成时为 `warming`。

`authorization_recovery_status` 只允许 `ready/probe_required/degraded/unavailable/unknown`：当前 MY bundle/receipt/qualification 完整、`recoverable_copy_count=2`、恢复密钥 readback事实有效、隔离恢复探测通过且 `dormant_ready` 时为 ready；仅展示年龄过期为 probe_required；远端仍 active 但只剩一份可恢复副本、恢复密钥 或恢复探测事实失效时为 degraded，并阻断旧 SV 退役和 MY bundle 擦除；包缺失、远端已撤销或两份副本都明确不可解封时为 unavailable；远端、receipt、代次或恢复对账未收口为 unknown。该状态不读取 Task、Action 或发送结果。

本 PRD 不承诺 SV 服务器或区域故障时的跨区业务 RTO。双 SV 授权失效场景只从 `sv_login_runtime_ready_at` 开始计算“新 primary 授权重建耗时”；业务恢复耗时还必须包含 SV runtime/出口恢复、任务模块重建与具体任务类型的 Telegram 远端事实。页面、告警和验收不得把 MY 可唤起、授权重建成功或健康探测成功写成“发送已恢复”。

在 SV runtime/出口健康、standby_1 已是 fresh `standby_ready` 且无既有 Gateway unknown 的前提下，`primary_failure_confirmed_at -> unfreeze_new_business_claims_at` 的本地切换目标为 120 秒内。页面和指标必须同时展示 `failure_detection_duration` 与 `local_activate_duration`，不得从主授权实际开始影响业务的时间中删掉检测阶段；未满足前提的账号显示 blocker，不纳入达标样本但仍计故障总量。

## 3. 目标与非目标

目标：

1. 所有符合条件的存量账号最终形成 `硅谷 primary + 硅谷 standby_1 + 休眠的马来西亚 standby_2`，三者都是 Telegram 侧未撤销的活跃授权设备。
2. 三个槽位分别绑定三套 Developer App，并具有三个独立 AuthKey、三个非零远端授权 hash 和三份授权凭据资产；禁止复制、共享或用 App 数量代替真实登录。
3. 新授权必须完成账号身份、Developer App、AuthKey 唯一、节点、出口、设备 hash 和即时健康证明后才计为达标。
4. 迁移期间不覆盖现有 `standby_2`；新候选失败时旧备用继续保留。
5. 唤起包在 MY 独立密封保存，通用清理、数据压缩、账号业务软删除和硅谷备份还原都不得直接删除它；账号删除后由独立授权退役流程收口。
6. 设备清理永久保护三个当前槽位及所有未证明撤销的我方候选/保留/修复授权；不强制保留 Telegram 官方手机、桌面或 Web 锚点。
7. MY 紧急流程只输出一次性登录码授权；硅谷登录运行时必须生成新 AuthKey/Session 并完成即时探测。
8. 配置、MY 唤起包、授权创建、SV 主授权重建、Telegram 授权恢复、业务发送恢复分别验收。
9. primary 单路径失败时自动完成 SV 本地切换，并保证旧代次业务结果不能覆盖新 current。
10. Action/Attempt、固定授权 assignment、listener、在线探测、联系人/群组同步和运行摘要都具有明确的切换行为。
11. 本地切换后恢复 SV 1 主 1 备；账号删除后立即退出全部业务模块并保留可追踪的授权退役状态。
12. 新账号登录后可立即在账号详情查看和刷新 Telegram 登录设备；设备清理使用已落库的当前 SV 授权登录时间做本地 48 小时门槛，不影响查看、三槽补齐或 MY 迁移。
13. 一键清理不建立资格 precheck/preview：当前 SV 执行授权登录时间严格超过 48 小时才创建执行项，否则直接跳过并返回跳过总数和原因；单账号详情显示原因并将按钮置灰。
14. 使用线上现有 App C 把一个 SV 备用授权迁为 MY `standby_2` 新代次；新代次提交前保留旧备份，提交后待旧远端授权完成退役回读才结束迁移。
15. MY 当前 Session 必须具备两个独立可恢复密文副本、独立密钥恢复能力和最近一次隔离恢复探测；上述恢复闸门通过前，旧 SV 迁移源不得远端撤销。

非目标：

- 不在马来西亚部署 Planner、Dispatcher、listener、account-online 或运营任务 worker。
- 不自动跨区迁移 Action、ExecutionAttempt、listener cursor、任务排期或 remote fact。
- 不提供第三个业务出口，不允许硅谷故障时改由 MY 节点发送、监听或执行运营任务。
- 不绕过验证码、2FA、FloodWait、新登录限制或设备清理限制。
- 不使用 Telegram “退出除当前外全部设备”这类不可精确保护三槽位的全量撤销；一键清理必须逐个撤销冻结的非我方 hash。
- 不因缺少异地备用阻塞现有健康 primary 的日常任务；只显示风险和补齐入口。

## 4. 账号范围与成功分母

目标分母为安全批次创建事务中冻结的、未删除且命中本次选择范围的账号集合，记为 `N = frozen_eligible_count = tg_account_security_batch_items 行数`。`N` 是每个批次的动态事实，可以大于 50，业务合同不设账号总数上限。创建后新增账号进入后续新批次；已冻结账号后续失败、等待、需要人工或被删除，其批次项仍保留在 `N` 中，不允许通过过滤或分页缩小分母。

全量在线三槽补齐使用 `selection_mode=all_online_accounts`。服务端必须在同一可重复读事务中按 `deleted_at IS NULL + status=online` 去重冻结账号 ID 全集、每个账号当时的 `account/current_authorization/fact/connection generation` 和 `target_set_fingerprint`，先写完全部账号项，再返回 preview。不得把前端当前页、数据库中存在 Session、A 新鲜探测成功、已有 B/C、账号用途或 worker 能否立即领取当作冻结分母的额外过滤器。批次创建后状态变化只改变该账号 outcome，不改变 `N`。

`complete_online_abc` 每个账号项包含两个槽位子结果 `standby_1_result` 与 `standby_2_result`，以及独立的 `primary_probe_result`。B、C 子结果都必须各自覆盖恰好 `N` 条，状态集合固定为 `already_qualified|pending|waiting|manual_required|succeeded|failed|reconcile_unknown|skipped_after_freeze`；账号级 outcome 固定为 `already_qualified|succeeded|waiting|manual_required|failed|reconcile_unknown|skipped_after_freeze`。任何时刻必须同时满足：

```text
account_outcome_counts.total = N
standby_1_outcome_counts.total = N
standby_2_outcome_counts.total = N
coverage_numerator = count(account outcome in already_qualified|succeeded)
```

账号级成功只能由同一新鲜证据快照中的 A 可用、B 合格、C 合格和零 open unknown 派生；不能由任一单槽成功手工写入。A 探测失败时 B/C 都不得发起登录，子结果进入同一明确 blocker，账号进入 `failed|manual_required|reconcile_unknown`；已经完成的健康槽位保持 `already_qualified`，不得回滚或重复登录。

| 分类 | 处理 |
| --- | --- |
| primary 健康、手机号/接码绑定可用 | 自动补齐或迁移 |
| primary 健康、但需人工验证码或 QR | `manual_required`，保留在分母 |
| 三槽全部不可用 | `fully_offline`，只允许人工重新登录 |
| Telegram 限制或新登录冷却 | `waiting`，按权威 retry 时间重试 |
| 已有合格 MY `standby_2` | 仅在 v2.21 全套事实、双副本/恢复密钥 readback事实、隔离恢复探测与 MY 密封包 readback 一致时标记 `already_qualified`；缺项进入修复或迁移 |
| 已删除账号 | 不进入新批次，不允许新业务、演练或紧急唤起；既有授权进入独立退役生命周期，不因软删除被静默删除 |

批次创建必须在同一事务中按服务端选择条件去重写入全部 `N` 条批次项，同时保存 `target_set_fingerprint`；该指纹对 `tenant_id + 规范化选择条件 + 排序后 account_id 全集` 计算 SHA-256。禁止把前端当前页、某个 worker 领取页或分批执行数当成总目标。每个账号项必须且只能进入一个互斥 DR outcome bucket，任何时候均满足 `dr_outcome_counts 之和 = N`；覆盖分子只计 `succeeded + already_qualified`，`waiting/manual_required/failed/unknown/skipped_after_freeze` 全部单独展示且仍占分母。`already_qualified` 可投影为通用批次项 `status=skipped`，但必须保留 `standby_session_status=already_qualified`；`skipped_after_freeze` 使用 `status=skipped + reason=account_deleted_after_freeze`。重试或恢复只领取未成功项，不重做已成功授权。

接码专用账号可以补齐和探测备用授权，但继续硬禁止消息发送、资料修改、2FA 轮换和一键设备清理。其他固定账号池的禁止策略继续优先于本 PRD。

## 5. 授权拓扑与故障域

```text
硅谷唯一业务运行面（固定 SV 出口）
  primary    Developer App A + 独立 AuthKey/hash，当前业务授权
  standby_1  Developer App B + 独立 AuthKey/hash，同一 SV 出口上的本地备用
  coordinator / DB / 恢复密钥 / Task Center

马来西亚休眠授权节点（固定 MY 出口）
  authorization-dr worker
  standby_2  Developer App C + 独立 AuthKey/hash，创建/修复、显式演练、紧急登录码读取
  MY 密钥域 + 持久卷 + 加密快照
  无运营任务队列、无 Dispatcher、无 listener
  operation 结束立即断连，平时无持久 Telegram client
```

合格三槽位必须满足：

- 生产配置恰好冻结三套 App 角色：App A=`primary_sv`、App B=`standby_1_sv`、App C=`standby_2_my`。同一套 App 可服务多个账号，但单账号的三个当前槽位不能复用；迁移同一账号的 App C 旧代与新代按 distinct account 只计一次，不需要临时第四套 App。
- Developer App A/B/C 互不相同，且每个槽位实际登录时的 `api_id` 与冻结快照一致；复用 App 不计三槽达标，不再设置降级 tier。
- AuthKey 与远端授权 hash 在三个槽位间均不同；一个远端授权不能同时满足两个槽位。
- 硅谷 primary/standby_1 可共用一个 SV 固定 IP，但必须保持两套 App、两个 AuthKey、两个授权 hash 和两份 Session；“共用 IP”不等于“共用设备”。
- 节点实测出口国家为 `MY`，证据未过期。
- 节点只取得单条 operation 和单条授权的最小权限，不能读取任务或批量导出 Session；非显式 operation 时不得连接 Telegram。

`failure_domain_key` 至少由 `cloud_account/provider + region + host_group + egress_provider + developer_app_owner` 规范化生成；任一关键维度未知时不得判定隔离。

## 6. 数据模型与唯一约束
### 6.1 授权资产
`tg_account_authorizations` 是授权资产权威表，一期必须新增正式列，不允许把关键状态放入无约束 metadata：

```text
dr_policy                  none / malaysia_authorization_only
dr_state                   candidate / active_standby / current_primary / retained / repair / invalid / unknown / revoked
logical_slot               primary / standby_1 / standby_2；授权创建时冻结，repair 不可改写，standby_2 永不晋升
slot_generation            单账号单逻辑槽单调递增
is_slot_current            当前逻辑槽有效代次
developer_app_id / developer_app_api_id_snapshot  该代次真实登录所用 App 与 api_id，提交后不可改写
provision_region_code      实际登录生成区域；权威新登录后不可变，存量不能证明时为 unknown
standby_egress_binding_id / standby_egress_generation  MY 创建与显式唤起基准
last_observed_egress_code  最近一次显式唤起实测出口
operating_egress_id / operating_egress_since  当前业务运行出口引用与起始时间
qualification_tier        three_slot_independent
qualification_basis_fact_id  引用冻结资源版本与比较对象的不可变 qualification fact
health_status             candidate / primary_authorization_healthy / standby_ready / dormant_ready / wake_probe_required / wake_verified / failed / unknown
health_basis_fact_id      当前健康投影使用的不可变 fact
execution_node_id          登录生成节点
failure_domain_key
expected_tg_user_id / observed_tg_user_id
auth_key_fingerprint_hmac / auth_key_fingerprint_key_version
session_key_version / candidate_secret_commit_id   仅 central_business 授权使用
credential_storage_scope    central_business / malaysia_sealed_wake
wake_bundle_id / wake_bundle_generation / wake_bundle_receipt_id
authoritative_probe_fact_id
remote_authorization_state active / revoked / unknown；只能由 Telegram 设备快照 readback 投影
protected_from_cleanup
supersedes_authorization_id / replaced_by_authorization_id
fact_version
```

`telegram_authorization_hash_ciphertext` 继续保存 Telegram 设备 hash 密文；它与 AuthKey 指纹是不同事实，不能互相替代。我方设备归属以“同账号、未撤销授权资产、当前/保留代次、唯一非零 hash 精确匹配”为准；`api_id`、App 名称、设备名、IP 或地区只能做一致性校验和展示，不能单独证明归属。

现有 `role` 仅保留为运行期兼容投影，不得再充当逻辑槽身份或代次唯一键。`is_current=true` 的授权可把兼容 `role` 投影为 `primary`，但 `logical_slot` 始终保持创建时的槽位。`logical_slot=standby_2` 的数据库约束必须禁止 `is_current=true`、禁止进入普通业务 resolver，也禁止把其 Session 复制、交付或重加密给 SV Gateway。只有 `logical_slot=standby_2`、`provision_region_code=MY`、MY 密封唤起包和 qualification 完整的授权才进入 MY 覆盖分子。

`tg_account_authorizations` 是授权身份、槽位和健康投影真相源。`credential_storage_scope=central_business` 的凭据存在中心 Session keyring；`malaysia_sealed_wake` 的凭据真相由授权行 + 当前 `tg_authorization_wake_bundles` receipt/manifest + MY 本地包共同构成，授权行不存可供 SV 解密的 Session。迁移期 `TgAccount.session_ciphertext/developer_app_id/proxy_id` 只作当前 SV 业务授权的兼容投影，并新增 `current_authorization_id/current_authorization_fact_version/current_connection_generation/business_runtime_status/sv_redundancy_status/account_lifecycle_status` 作为原子切换投影。普通 Telegram 入口必须通过 resolver 返回 `authorization_id + fact_version + connection_generation + environment_binding_id + proxy_binding_id + credentials`，candidate、retained、repair、invalid、unknown 和 standby_2 永不进入业务 resolver。

### 6.2 新增对象
| 对象 | 关键字段与用途 |
| --- | --- |
| `telegram_execution_nodes` | `node_id/region/provider/failure_domain/worker_identity/allowed_purposes/status/release_sha/heartbeat/standby_egress_id/stale_after/version`；一期只有一个 MY 物理节点并固定绑定唯一 MY 出口 |
| `tg_developer_app_failure_domain_assertions` | `developer_app_id/owner_domain_hmac/key_version/source_evidence/status=proposed|approved|rejected|stale/version/verified_at/stale_after/requester/approver`；owner domain 不明或未异人批准时不得判定 App 隔离，新 key 版本须先重算、异人复核并覆盖 readback 后才供新 qualification 使用 |
| `tg_developer_app_slot_assignments` | 生产三 App 角色真相：`tenant_id/slot_purpose=primary_sv|standby_1_sv|standby_2_my/developer_app_id/credentials_version/status/version/effective_at`；同租户每个 purpose 只能有一个 active assignment，同一 App 不能同时占两个 purpose。迁移 preview 冻结 A/B/C 三条 assignment 与凭据版本，不接受第四套临时 App |
| `telegram_egresses` | `egress_id/purpose=standby_my|primary_regular/region/provider/exit_ip_ciphertext/connectivity_profile_secret_ref/connectivity_profile_secret_version/exclusivity_proof/allowed_runtime_scope/verified_at/stale_after/status/version`；一期只登记硅谷业务与 MY 唤起各一个固定 IP，API 和日志永不返回完整 IP、路由凭据或 secret 内容 |
| `tg_egress_change_operations` | 出口 create/update/disable/retire/rotate_hmac 的 requested/approved/applying/applied/failed_hold/rejected/cancelled/expired 状态、`expires_at/remote_effect_started_at`、脱敏 desired diff、expected egress version、双人审批、apply/readback 和幂等事实；普通 API 不直接写出口行，一期不得新增第三个 active 业务出口 |
| `tg_egress_ip_fingerprints` / `tg_authorization_activity_ip_fingerprints` | 每个出口或活动 observation 按有效 key version 保存 IP HMAC；支持轮换期双写、双读、回填和旧 key 退役，历史事实保留原 key version |
| `tg_authorization_egress_assignments` | `tenant_id/account_id/logical_slot/slot_generation/egress_id/operation_id/authorization_id/assignment_generation/state=active|released/attached_at/released_at/version`；MY wake bundle receipt 提交时同事务记录实际使用的唯一 MY 出口，不存在业务运行中间态 |
| `tg_authorization_egress_usage_facts` | 追加保存授权实际连接出口、开始/结束、connection generation、operation 和结果；SV 业务授权迁移可 CAS current pointer，standby_2 只更新槽位代次/包和 assignment，不改业务 current |
| `tg_egress_connectivity_facts` | 不携带业务 Session 的网络层 Telegram 可达性事实，绑定已存在 operation/preflight generation、运行环境、出口、目标 DC、探测目的、时间、stale_after、结果和 fact version |
| `tg_authorization_sv_path_failure_facts` | 绑定已存在 `emergency_reauthorize_primary` operation/preflight generation，分别记录 `path=primary|standby_1` 的 typed failure、证据引用和时效；两条当前事实缺一不可，不能用 primary 单点故障唤起 MY |
| `tg_authorization_backfill_batches` | 存量 preview/apply/readback 的冻结范围、target fingerprint、expected old values、分类计数、冲突、申请人/审批人、幂等键、`expires_at/apply_started_at`、resolver shadow diff、writer-cutover blocker 和 `previewed|approved|applying|applied|failed_hold|rejected|cancelled|expired` 状态 |
| `tg_authorization_contract_cutover_operations` | 冻结 target mode/epoch、最低 runtime capability、实例/client/ACL fingerprint、`kms_policy_version/kms_policy_digest/kms_expected_grantees_digest/kms_readback_fact_id`、expected contract version、双人审批、apply/readback、`expires_at/apply_started_at` 和 `previewed|approved|applying|applied|failed_hold|rejected|cancelled|expired` 状态；不与普通 DR operation 混表 |
| `tg_egress_reputation_facts` | 保存批准外部来源或多账号相关失败的 evidence type、窗口、distinct account count、policy version、结论和审核状态；单账号事实不能升级出口级信号 |
| `tg_authorization_activity_observations` | 每次 `account.getAuthorizations` 的不可变设备集快照；每条保存非零 hash 密文/指纹、`api_id`、设备/App 元数据、Telegram `Authorization.date_created`、活跃时间、脱敏 IP/国家、`current`、分类=`platform_current|platform_retained|external|unresolved`、匹配授权/槽位/代次/事实版本和派生异常。当前 SV 执行授权的 `date_created` 同步为持久化 `telegram_login_at`，供本地 48 小时门槛读取 |
| `tg_authorization_device_cleanup_operations` | 保存 `requested_count/eligible_count/skipped_count/skipped_reason_counts`、每项冻结的 `executor_authorization_id/executor_fact_version/telegram_login_at`、worker 执行时 observation/snapshot digest、protected manifest/version、external target digests、逐目标结果和最终 exact-set readback；不保存 precheck/preview 或 waiting 状态 |
| `tg_authorization_activity_reviews` | observation/unexpected remote 的 open/decision_pending/revocation_pending/resolved/unresolved 状态、分类决定、证据、expected versions、提交人/审批人、处置 operation 和 Telegram readback |
| `tg_authorization_break_glass_reviews` | 单人 break-glass 的申请人、强认证事实、primary 与 standby_1 双路径 failure facts、可审批人快照、不可达说明、incident、到期时间、状态和复盘结论 |
| `tg_authorization_dr_operations` | provision/migrate/drill_wake/emergency_reauthorize_primary/local_activate/restore_sv_pair/decommission/remote_device_revoke 的幂等状态机；保存 `expires_at/preflight_generation/source_authorization_id/source_fact_version/source_connection_generation/target_authorization_id/target_primary_generation/approved_intent_digest/fence_effect_started_at/remote_effect_started_at`、expected versions、审批、retry、trace 和失败事实 |
| `tg_authorization_probe_facts` | 不可变保存 operation/preflight generation、UID、AuthKey 指纹、App、节点、出口、授权状态、设备 hash、时间和结果 |
| `tg_authorization_qualification_facts` | 不可变保存授权/operation/policy 版本、Developer App 凭据与 owner-domain assertion 版本、environment binding ID/version/digest、client metadata version/client identity key、节点/故障域/出口/secret 版本、被比较授权及事实版本、tier、observed/stale/result；`qualification_basis_fact_id` 只能引用本对象 |
| `tg_authorization_authkey_fingerprints` | 每个授权按有效 blind-index key version 保存 HMAC；`key_version + fingerprint_hmac` 唯一，用于跨轮换期碰撞检测 |
| `tg_authorization_candidate_secret_commits` | 仅供 `credential_storage_scope=central_business` 的 SV 授权；每个 operation/generation 唯一，同事务写 candidate Session 密文、key version、operation-scoped digest 和 AuthKey 指纹；standby_2 禁止写入本对象 |
| `tg_authorization_login_input_grants` | operation-scoped 一次性登录输入授权；冻结账号/手机号 secret version 或 digest、Developer App ID/凭据与 owner-domain assertion 版本、environment binding ID/version/digest、client metadata version/identity key、egress ID/version/secret version、broker policy version/input digest，以及 node/owner/inventory/generation、公钥、bundle digest、issued/consumed/expired/superseded 和 TTL；不保存明文手机号、API Hash 或连接凭据 |
| `tg_authorization_wake_bundles` | 中心 manifest：`authorization_id/bundle_generation/ciphertext_digest/wrapped_dek_digest/my_kms_key_version/copy_manifest_digest/recoverable_copy_count/local_copy_receipt_id/snapshot_copy_receipt_id/restore_probe_fact_id/write_receipt_id/status=sealed|active|retained|repair_required|erase_pending|erased/created_at/last_verified_at`；每代不可变，中心密文副本不可被 SV 密钥解密 |
| `tg_authorization_wake_bundle_copies` | 每份不可变副本：`wake_bundle_id/copy_type=my_local_volume|my_remote_ssh_snapshot/storage_ref/ciphertext_digest/write_receipt_id/readback_receipt_id/decrypt_verified_at/status=written|verified|missing|corrupt|erase_pending|erased/version`；同一 bundle 只有两类各一份 `verified` 才可投影 `recoverable_copy_count=2`，修复必须写更高 bundle generation，禁止覆盖原对象 |
| `tg_authorization_wake_inventory_entries` | MY 对象存储中的追加账本，中心数据库只保留 projection：`inventory_sequence/previous_entry_digest/entry_digest/account_uid/authorization_id/slot_generation/bundle_generation/copy_manifest_digest/my_kms_key_version/event=sealed|central_receipt_readback|slot_commit_prepared|slot_commit_observed|retained|erase_requested|erase_observed/slot_decision_id/expected_old_authorization_id/target_authorization_id/expected_slot_version/operation_id/created_at`；中心库回滚后以 MY 最大有效序列做只增不减对账，prepared 决策可按相同 expected values 幂等前滚 |
| `tg_authorization_wake_permits` | `purpose=drill_wake_probe|emergency_code_source`的 operation-scoped 唤起许可；绑定唯一 MY node、bundle generation、owner epoch、TTL 和 consumed/closed receipt，只允许 MY 本地解封 |
| `tg_authorization_login_code_grants` | 一次性登录码授权：绑定 operation、源授权/bundle generation、目标 flow/primary generation、`challenge_sent_at`、`phone_code_hash_digest`、Telegram 服务消息 ID/date、envelope digest、TTL、consumed/expired/zeroized receipt；不保存登录码明文或 preview |
| `tg_account_rpc_fences` | 每账号唯一 `fence_epoch/state/operation_id/allowed_purpose/lease_until/cluster_incarnation/version`，冻结全部 Telegram 入口 |
| `tg_account_authorization_inventory_leases` | 每账号唯一授权控制 lease，`lease_mode=inventory_mutation|runtime_migration|switch`；覆盖全部登录、QR、补齐、清理、撤销、切换和 egress migrate，持有单调 `inventory_epoch` |
| `tg_authorization_runtime_leases` | `authorization_id/owner_instance/owner_epoch/purpose/lease_until/connection_generation/cluster_incarnation/state/version` |
| `execution_attempts` 与 Gateway permit | 每个即将调用 Telegram 的 Attempt 在 `before_call` 提交 `authorization_id/authorization_fact_version/connection_generation/environment_binding_id/proxy_binding_id/account_fence_epoch`；Gateway 开始前逐项校验，不允许只携带 `account_id` 动态换 Session |
| `tg_account_online_states` / probe job-result | 保存并传递 `authorization_id/authorization_fact_version/connection_generation`；旧代次 probe 结果只记 stale evidence，不更新账号在线状态、恢复状态或下一次探测计划 |
| listener / sync / runtime summary | `listener_source_states`、listener claim/fetch result、联系人/群组/资料同步记录和账号运行摘要都保存 `authorization_id/authorization_fact_version/connection_generation`；远端读取后、推进 cursor 或写业务状态前重新校验 current，旧代次结果进入 `stale_generation_discarded` |
| `tg_account_protected_device_refs` | `tenant_id/account_id/subject_type=platform_authorization|orphan_remote|unresolved_remote/subject_id/authorization_hash_ciphertext/hash_fingerprint_hmac/hash_key_version/source_observation_id/source_snapshot_digest/state=protected|release_pending|released/protected_at/release_fact_id/released_at/version`；当前/候选/保留/修复/未知且未撤销的我方 authorization 全部映射为 `platform_authorization`，普通 API 不返回 hash 明文 |
| `tg_authorization_contract_control` / `telegram_runtime_instances` | 单例合同控制记录 `contract_epoch/mode=legacy_read|shadow|resolver_required|dr_enabled/min_reader|min_writer|required_gateway/cluster_incarnation/version`；运行实例上报 identity/role/release SHA、reader/writer/Gateway capability、heartbeat、active client count 和 egress permit，用于切换 readback |

扩展 `TgAccount` 的手机号密文版本和上述 current/runtime/lifecycle 投影；Developer App 沿用现有 `credentials_version/max_accounts`，新增 `assigned_distinct_accounts/pending_distinct_accounts/used_distinct_accounts/available_accounts`。`used` 是未撤销授权账号集合与非终态登录 operation 账号集合的并集基数，同一账号同 App 多代或同时出现在两集合只计一次，因此 App C 的 SV 旧代与 MY 新候选并存不会把同一账号重复计费。账号环境绑定新增 `binding_version/binding_digest/client_metadata_version/is_current`。扩展 `tg_login_flows`：`dr_operation_id/target_logical_slot/target_generation/execution_node_id/owner_epoch/inventory_epoch/cluster_incarnation/login_input_grant_id/login_input_grant_generation/candidate_secret_commit_id/wake_bundle_receipt_id/expected_version/challenge_method/challenge_message_id/challenge_sent_at/phone_code_hash_digest/sent_code_type/code_source_authorization_id/code_source_bundle_generation/qr_challenge_generation/qr_payload_digest/qr_expires_at`。`candidate_secret_commit_id` 仅对 SV 授权有值，`wake_bundle_receipt_id` 仅对 MY standby_2 有值。只有 Telegram `SentCode.type` 明确为 app-session delivery 时才可自动读取 MY 登录码；SMS/email/call/QR 一律进入 `manual_required`。

扩展 `tg_account_security_batches`：增加 `frozen_eligible_count/target_set_fingerprint/next_retry_at/last_claimed_at`；扩展 `tg_account_security_batch_items`：增加 `dr_operation_id/operation_mode/target_node_id/target_generation/standby_session_status`。批次与全部去重账号项必须事务性创建，`UNIQUE(batch_id, account_id)` 保证 `item_count=N`；`dr_outcome_counts` 从项状态在同一查询快照中聚合，不另写一份可漂移计数。读取分页只按不可变 `batch_item_id` 升序，cursor 绑定 batch/fingerprint/last item ID，`status` 只作筛选而不参与排序。worker 不使用单向分页 cursor 领取，每次都从持久化状态中查找 `pending` 或已到 `next_retry_at` 的项并 CAS 一条，因此早先 waiting 项到期后不会被跳过。多个运行批次按最早 `coalesce(last_claimed_at, created_at)` 每轮各领一项，不让大 `N` 批次长期占满唯一登录 owner。批次仍投影为 `account_standby_session_provision`，不新增普通运营 Task。

### 6.3 数据库约束
- 每账号最多一个 `is_current=true` 授权。
- `logical_slot` 创建后不可更新；每账号每逻辑槽最多一个 `is_slot_current=true` 且未停用的授权，唯一约束使用 `account_id + logical_slot`，不得使用兼容 `role`。
- `logical_slot=standby_2` 必须满足 `is_current=false + credential_storage_scope=malaysia_sealed_wake`；任何 current pointer writer、兼容投影 writer 或业务 resolver 对它的访问都由数据库 gate 拒绝。
- 每账号每逻辑槽最多一个非终态 DR operation；operation 必须冻结 `logical_slot + target_slot_generation`，不得从运行时 role 反推。
- `provision_region_code` 只允许在权威新登录提交或经批准 backfill 时由 `unknown` 写入真实 ISO 区域，写入后不可修改；`standby_2` 计入覆盖前数据库 gate 必须验证值为 MY。
- 非终态授权的 AuthKey 指纹在每个有效 blind-index key version 下全局唯一；新 key 先全量回填并 readback，再允许候选写入，旧 key 只能在全部非终态授权已有新版本指纹后退役。该双版本机制的取舍依据：在 HMAC key 泄露的最坏假设下，单版本直接轮换会留下碰撞检测盲区窗口；双版本只在轮换窗口增加写入闸门成本，不增加运行时查询成本，因此保留。
- 每个授权同时最多一个有效 runtime lease 和 connection generation。
- `TgAccount.current_authorization_id/current_authorization_fact_version/current_connection_generation`、兼容 Session/App/proxy 投影和唯一 `is_current` 必须在同一事务 CAS；任何一项不一致时业务 resolver 返回 `current_projection_inconsistent`，不得继续领取新业务。
- 每个 Gateway-bound Attempt 在 `gateway_call_started_at` 写入前必须已有完整授权/代次/环境/代理/fence 快照；调用开始后这些字段不可修改，切换不能把旧 Attempt 改绑到新授权。
- 每账号只有一条 account RPC fence；冻结 CAS 必须提升 `fence_epoch`，permit 必须绑定未随数据库备份回滚的当前 `cluster_incarnation`。
- 每账号同时最多一个有效 authorization control lease；`inventory_mutation/runtime_migration/switch` 三种 mode 互斥，所有平台登录、设备变更和授权连接迁移共享该约束，外部并发变化进入 exact-set unknown。
- 一期只有一个 `standby_my` egress，且只能绑定唯一 MY execution node；授权总数不设 IP 配额账。candidate 只有在 MY 本地副本与 SSH 镜像均完成写后读/摘要/离线解封、源 client 已断连、从 SSH 镜像隔离恢复的 Telegram probe 通过并再次断连、中心收据 readback 后才可提交槽位；standby_2 assignment 只允许 active/released。
- 同账号同时最多一个非终态 provision/migrate/drill_wake/emergency_reauthorize_primary/local_activate/restore_sv_pair/decommission/remote_device_revoke operation；执行阶段必须先取得同一账号 control lease。`fence_effect_started_at/remote_effect_started_at` 均为空且无已消费 grant 时才可取消；任一副作用边界非空后只能 reconcile。
- Developer App 分配在同一事务中锁定 App 版本并计算 `used_distinct_accounts = count(distinct assigned_account_ids UNION pending_account_ids)`；达到 `max_accounts` 时返回 `developer_app_capacity_shortfall`。三槽必须分别选三个仍有账号名额的 App，不能因共用 SV IP 或已有账号主投影而跳过 standby 槽位占用。
- 沿用现有 `max_accounts <= 0` 表示不设平台分配上限；此时 API 返回 `capacity_unlimited=true + available_accounts=null`，页面显示“不限”。已软删除但尚未退役且远端未撤销的授权继续计入 used，只有授权退役后才释放 App 账号占用。
- `account_lifecycle_status=business_deleted_authorizations_retained` 时禁止 Task/Action 新领取、listener、在线探测、联系人/群组/资料同步、provision/migrate/drill_wake/emergency_reauthorize；仅允许 readback、reconcile 和显式 decommission。
- 每个单人 break-glass operation 必须且只能有一条非终态 review；`review_due_at` 到期未完成时账号进入 `break_glass_pending_review`。
- 每个 SV 业务授权 operation/generation 最多一个 candidate secret commit；每个 MY standby_2 operation/bundle generation 最多一个 wake bundle receipt。两者同一 digest 和 copy manifest 重放返回原收据，不同值永久冲突；bundle 文件、对象和 manifest 均按 `tenant/account/authorization/bundle_generation/ciphertext_digest` 不可变寻址，禁止原位覆盖。
- 每个 operation/grant generation 最多一个 login-input grant，且同时最多一个 `issued`；消费状态只能单向进入 consumed/expired/superseded，不能重置。
- 每个 `standby_2` 当前代次最多一个 active wake bundle；更换或修复副本时从仍可解封的上一代生成更高 bundle generation，完成双副本、恢复密钥、隔离恢复和中心 readback 后再将旧包改为 retained，永不覆盖上一代。每个紧急 flow 最多一个 issued 登录码 grant，TTL 默认 60 秒，只允许同一 SV flow 消费一次。
- 任一 active/retained bundle 依赖的 双机恢复密钥 key version 必须保持可恢复且有真实 decrypt readback；key 停用/退役、快照轮换或本地卷清理不得让任何 active/retained 授权的 `recoverable_copy_count` 降至 2 以下。
- 每个 activity observation 最多一个非终态 review；同一 egress/key version 和 observation/key version 最多一个 IP fingerprint。
- 每个 protected ref 必须同时满足租户和账号作用域、非零 hash 密文及当前有效 blind index；`protected|release_pending` 状态参与 manifest version/CAS，只有引用 Telegram 撤销 readback 的不可变 release fact 才能进入 `released`，跨账号 subject/hash 冲突不得合并。官方 App 设备或同 `api_id` 额外设备不因类型/App 自动进入 protected manifest。
- 同一 backfill target fingerprint 最多一个可 apply 批次；未由异人批准、expected old values 漂移或 readback 未完成时不能进入 writer cutover。
- contract control 提升到 `resolver_required/dr_enabled` 后，授权关键 mutation 必须携带事务级 `contract_epoch + operation identity`，数据库 gate 与专用 writer role 拒绝缺失/旧 epoch/旧角色；Session 解密只授予达标 Gateway 或 scoped authorization-dr identity。所有 live runtime 版本合格、旧 client 清零，且主运行面 Telegram 出口只允许 Gateway、MY 出口只允许 authorization-dr 前不得提升模式。
- 同一 `tenant + account + operation_type + idempotency_key` 重放必须返回原 operation；payload 不同返回 `409 idempotency_key_reused`。
- 所有提交使用账号版本、槽位 generation、授权 fact version 和 operation version 的 CAS。
- 固定锁顺序为 `account/version -> authorization control lease -> egress version -> RPC fence -> runtime lease/generation -> Telegram -> fact/slot CAS`；任一实现不得反序持锁或在 Telegram 网络等待期间持数据库行锁。

## 7. 马来西亚节点与任务定向
新增专用 worker role：`authorization-dr`。账号安全 worker 只创建和展示 operation，不能替马来西亚节点执行远端登录或唤起探测。该 worker 不运行常驻 Telegram client，也不订阅周期性 probe 队列。

领取条件必须同时满足：

```text
operation.required_region = MY
operation.execution_node_id = node.node_id
node.status = ready
node.worker_identity = 当前 mTLS 服务身份
node.release_sha 在批准清单
node.heartbeat 与 node.standby_egress 的 verified/stale_after 均有效
purpose in provision / migrate / repair / drill_wake / emergency_wake
expected operation version CAS 成功
```

主生产 worker、未知节点、仅配置 MY 代理的 worker，以及没有当前显式 operation 的定时器领取时返回 `execution_node_mismatch` 或 `malaysia_wake_operation_required`。节点标签不是授权；每次 Telegram 连接前都重新校验 lease、node version 和实测出口事实。

节点通过受限内部 API 领取单条 operation 并回传事实，不直接读取业务 Task、Action、联系人、素材或消息表。provision/migrate 的登录输入仍通过短 TTL、单次消费 grant 交付；授权成功后节点用 bundle DEK 密封 Session 和必要 client 元数据，以不可变临时路径写入 MY 持久卷，完成文件 fsync 后原子 rename 到最终路径并 fsync 父目录，再以 create-only 条件写入硅谷 SSH 镜像。节点必须从两份存储分别读回完整密文，校验同一 digest，并通过双机恢复密钥解封/解析。随后先断开并清零原登录 client，再从 SSH 镜像解封到内存或 operation 临时运行态，在隔离 owner 下恢复一次 Session，完成 `is_user_authorized/get_me/AuthKey fingerprint` 探测并再次断连、擦除临时态。Telegram client/Session SDK 禁止直接以最终 bundle 路径作为可写 session 文件，运行时产生的缓存或状态更新不得回写不可变副本。最后才向中心提交包含双副本、恢复密钥、恢复探测的 manifest/receipt。任何一步缺失都不能提交 candidate 槽位。

MY 同时把 `sealed -> central_receipt_readback -> slot_commit_prepared -> slot_commit_observed` 写入独立追加 inventory。中心提交响应丢失时按同 operation/bundle generation/digest/copy manifest 幂等查询；MY inventory 已领先中心时只允许向中心补写，不允许中心把 MY 较高序列回退、覆盖或删除。修复损坏副本必须从仍通过校验的副本生成更高 bundle generation，不重新登录 Telegram，也不改写损坏对象。

节点在 grant 消费后、远端结果明确前崩溃时进入 `login_runtime_lost` 并执行 before/after/wake-bundle-receipt 对账，不能自动再次获取同 generation 材料或重新登录。只有服务端证明未产生新远端设备、无 MY 密封包/receipt、旧 flow 已 superseded，才可提升 `login_input_grant_generation` 后重新签发。

不存在将 `standby_2` Session 下载给 SV、按设备 hash 重建 Session，或用硅谷 keyring 重加密为业务 Session 的接口。MY 节点只能在当前 wake permit 下本地解封当前 bundle generation。

### 7.1 双 IP 拓扑与执行节奏

一期网络事实固定为：硅谷唯一业务运行环境使用一个固定 SV 出口 IP，`primary` 与 `standby_1` 都绑定该出口；马来西亚休眠节点使用一个固定 MY 出口 IP，总计两个。唯一 MY IP 服务每个补齐/迁移批次冻结的全部 `N` 个符合条件账号，不把 `N` 建模为 IP 容量槽位，也不设账号总数上限；只有真实 Telegram 风险事实才能触发暂停或后续网络方案变更。

- SV 固定出口是唯一业务出口，同时承载 `primary` 日常调用和 `standby_1` 本地恢复；两者必须使用独立 AuthKey/授权槽位，但不得为了槽位数量虚构第二个硅谷 IP。MY 出口必须是休眠节点独享的静态出口；禁止共享代理池、轮换出口或未登记出口。一期 readback 必须确认恰好一个 active `primary_regular` 与一个 active `standby_my`，且 MY 出口没有业务 egress permit。
- MY 节点逐个执行登录生成，不并行批量创建授权。完成创建或修复后立即断连；后续没有定时、错峰或后台健康探测，只有管理员显式演练或紧急唤起才再次连接。
- 执行不设固定账号阶梯。创建时一次性冻结完整 `N`，MY 节点每次只领取一个可执行账号项，持续串行处理直到没有当前可领取项。系统级分页、页大小和 worker 查询窗口只是技术分页，不是业务上限或新分母。只有管理员将功能模式降为 `read_only/off`、停用 MY 节点，或权威 Telegram `next_retry_at`/风险事实可停止新领取；功能模式/节点恢复且权威等待到期后，依据批次项状态续跑，不重做 `succeeded/already_qualified`。
- 出口创建、secret ref/范围修改、停用、退役和 HMAC 轮换只能通过 `tg_egress_change_operations`：发起人与审批人分离，apply 按 expected egress version CAS 并做运行面 readback。disable 停止新登录和显式唤起；retire 必须等 usage、lease 和在途 operation 全部清零并完成 readback，不能删除历史 fact。
- IP HMAC 轮换先登记新 恢复密钥版本，再对全部出口和仍有原始 IP 密文的保留期内 observation 回填新版本并进入双写/双读。匹配只比较双方共有的有效 key version；没有公共版本时形成 `egress_hmac_version_uncomparable`，不得升级活动异常。新版本覆盖所有 active egress 和策略窗口内 observation 并 readback 后才切 current；旧 key 仅在没有新写入依赖且审计快照已固化后退役，历史 HMAC 继续携带原版本但不用于新异常判定。
- `standby_egress_binding` 只约束 standby_2 的创建和显式唤起。若更换 MY IP，必须先停止新登录/唤起，对全部受影响授权完成新出口探测，再按更高 assignment generation CAS 切换。该流程不增加第三个业务 IP，也不改变 standby_2 永不运行业务的合同。
- `AuthKeyDuplicated` 是授权级严重错误：立即投影 `dr_state=invalid`、`last_authoritative_error_code=authorization_key_duplicated`；API blocker `repair_required` 由这两个持久事实派生，它不是第二个 `dr_state`。同时撤销健康资格，冻结该账号相关连接并调查 Session 复制、runtime lease 或 owner fencing；它不构成出口信誉证据，也不能只靠换出口恢复。
- spambot 限制、账号功能受限和单账号登录后退出归为 `account_risk_signal` 或 `authorization_invalidated`，只处置受影响账号。`egress_reputation_signal` 必须来自已批准外部信誉事实，或在版本化时间窗内达到策略要求的多个不同账号同类连接层失败；单账号事件不能自动隔离整条出口。命中后停止 MY 新登录和显式唤起，既有授权保持可见并按各自事实处置，不自动增加或切换 IP。

## 8. 新授权健康资格
健康由最新不可变显式唤起 fact 投影，禁止登录函数、定时器或切换函数直接写 `health_status=healthy`。

健康词典按槽位与运行目的区分：当前业务授权仅可投影 `primary_authorization_healthy`，当前 standby_1 仅可投影 `standby_ready`，非业务 standby_2 仅可投影 `dormant_ready|wake_probe_required|wake_verified`。`primary_authorization_healthy` 要求 current pointer/兼容投影、UID、AuthKey、设备保护和 SV fresh probe 一致；`standby_ready` 要求当前 standby_1 槽位的同等授权证明且无并发连接；`dormant_ready` 要求最近显式事实、MY manifest/receipt/inventory 一致、两个独立副本均可解封、恢复密钥 readback事实有效、隔离恢复探测通过且当前无 MY client。事实超过展示年龄后只降为 `wake_probe_required`，不自动连接。`wake_verified` 只存在于当前显式演练或紧急登录码 operation 内，必须全部满足：

1. wake bundle receipt/digest/generation 与授权行及 MY inventory 一致，本地卷和 SSH 镜像两份副本分别完成写后读、digest 与 双机恢复密钥 解封，`recoverable_copy_count=2`；从 SSH 镜像隔离恢复后 `is_user_authorized=true`。
2. `get_me.id == expected_tg_user_id == observed_tg_user_id`。
3. AuthKey HMAC 指纹非空，且未与任何主备、候选、保留授权重复。
4. qualification fact 冻结的 Developer App 凭据/`api_id`、environment binding/client identity、目标节点、出口/secret、被比较授权事实和 policy 版本均与当前值一致，且三槽 Developer App/AuthKey/hash 全部独立，投影 `three_slot_independent`。
5. 登录和探测的实测出口均为 MY。
6. 由其他健康授权读取到该设备唯一非零 hash；`hash=0` 无效。
7. before/after 必须固定同一 observer authorization/fact version，只对排序后的非零远端 Session hash 做规范化集合哈希，排除 IP、active time等可变元数据；差分只能唯一命中新设备，否则进入 `reconcile_unknown`。
8. 当前唤起探测不超过 2 分钟，并且 MY client 断连、lease 释放、connection generation 提升的收据完整。

`dormant_ready` 的展示年龄默认 30 天，仅用于提醒管理员安排演练，不是自动探测周期，也不自动判定 Session 已失效。演练只能由管理员显式创建 `drill_wake`，不得切 current、不得冻结或释放业务 Action；紧急唤起无论上次演练时间多近，都必须在当前 operation 内重新探测。Telegram 返回 FloodWait 时遵守权威 retry 时间，不换节点或应用绕过限制。

显式演练状态机固定为：

```text
requested -> claimed_by_my -> drill_waking -> wake_verified
  -> my_disconnected_and_fenced -> succeeded_dormant
```

连接失败、事实不完整或断连/fencing 未证明时进入 `blocked|reconcile_unknown`，不得写 `succeeded_dormant`。演练不取得账号 RPC fence，不改变 current pointer、Action/Attempt、在线状态或硅谷 runtime；同账号已有切换、登录、清理或撤销 operation 时拒绝演练。

每次显式唤起或账号管理“刷新登录设备”都通过一个非目标、事实版本当前且可授权读取的我方 peer 调用 `account.getAuthorizations`，并把整个设备集合保存为不可变 observation。读取设备集不受清理年龄门槛影响，新账号完成任一真实登录后即可查看。远端每条授权只按以下顺序分类：

1. 唯一非零 hash 精确匹配同账号未撤销的当前槽位授权，为 `platform_current`。
2. 唯一非零 hash 精确匹配我方 candidate/retained/repair/invalid/unknown 授权，为 `platform_retained`，继续保护至显式 decommission readback。
3. hash 非零但不匹配任何我方授权，为 `external`；即使 `api_id` 与三槽之一相同，也仍是非我方设备。
4. `hash=0`、一个 hash 匹配多个本地授权、我方槽位 hash 缺失，或读取结果不完整，为 `unresolved`，禁止当次清理。

Developer App `api_id`、App 名称、设备元数据和区域只用于发现“hash 匹配但 App 不一致”的资产异常，不改变归属结论。官方手机/桌面/Web 只要没有精确匹配我方授权资产，就是 `external`，不再自动保留为锚点。

平台授权的 last-active 出口必须按活跃时间落入对应 append-only egress usage fact 的合法区间。单次 IP 不匹配只形成 `authorization_activity_needs_review` 并冻结常规切换，不能直接宣称凭据泄露；未知非官方设备、重复异常或人工确认后才升级 `authorization_activity_anomaly`。原始 IP 使用独立字段级密文保存，匹配使用 keyed HMAC，普通页面只显示脱敏 IP/国家；审计永久保留 hash、国家、时间、分类和结论，不永久保留原始 IP。

activity review 只用于 `unresolved` 或“hash 匹配但 App/账号事实不一致”的异常，允许结论为匹配我方授权、确认 external、确认资产异常或证据不足。已精确分类的 `external` 不需要逐设备人工审批，可直接进入用户确认的一键清理 manifest；任何 `unresolved` 存在时整个账号不执行清理。

## 9. 页面与操作入口
账号列表分别展示硅谷 `primary`、硅谷 `standby_1` 和休眠 MY `standby_2`，每个槽位展示 Developer App、授权设备存在状态和最后远端观测时间。列表同时独立展示 `business_runtime_status`、`sv_redundancy_status=healthy|degraded|unavailable`、`authorization_recovery_status`、当前业务授权/connection generation、`我方活跃授权数 / 非我方设备数 / 待识别数`、MY 休眠状态、`可恢复副本 0/1/2`、恢复密钥 readback 状态、最后隔离恢复结果、MY inventory 是否领先中心、硅谷/MY 两个脱敏固定出口、当前批次 `N` 与不可辅助重建原因。支持筛选 missing、wake_probe_required、recovery_degraded、unqualified、migration pending、external device、unresolved device、reconcile unknown、SV 本地冗余降级、业务 runtime 不可用、账号待退役和可紧急辅助重建账号。

账号详情新增独立“登录设备”Tab，这是 Telegram 远端活跃授权清单，不是当前连接中的 client 清单。只要账号存在一个可读取授权集合的 SV 平台 Session，该 Tab 在新账号登录后立即可用，不等待清理门槛，也不要求三槽补齐完成。顶部固定展示 `remote_active_total/platform_current/platform_retained/external/unresolved/as_of`，下方分组展示：

| 分组 | 展示与操作 |
| --- | --- |
| 我方当前设备 | primary/standby_1/standby_2 槽位、SV/MY 区域、Developer App、设备/App 元数据、授权时间、最后活跃、脱敏 IP/国家、最后刷新；固定保护 |
| 我方历史设备 | candidate/retained/repair/invalid/unknown 资产及对应代次；不参与一键清理，通过独立 decommission 收口 |
| 非我方设备 | 所有非零 hash 且不匹配我方授权资产的设备；默认全选为一键清理目标 |
| 待识别 | hash 缺失/为零、映射歧义或快照不完整；展示 blocker，禁止当次清理 |

“登录设备”Tab 必须覆盖以下服务端驱动状态，不能由前端自行推断可执行性：

| 状态 | 页面行为 |
| --- | --- |
| `loading|refreshing` | 保留上一份带 `as_of` 的设备结果并显示刷新中；首次读取无结果时显示加载态 |
| `ready` | 当前 SV 执行授权 `telegram_login_at` 存在且 `server_now > telegram_login_at + 48h`；展示“清理非我方设备” |
| `no_external_devices` | 展示“暂无非我方设备”，不进入确认弹窗 |
| `login_age_not_over_48h` | 展示当前 SV 授权登录时间和“登录时间未超过 48 小时，暂不能清理”，按钮置灰；不展示倒计时，不发 Telegram 请求 |
| `login_time_missing|current_sv_authorization_unavailable` | 展示“缺少登录时间”或“当前 SV 授权不可用”，按钮置灰 |
| `unresolved_devices|protected_hash_unproven` | 标明待识别数或缺失的我方槽位，按钮置灰；若 observation 在提交后才变化，则由 worker 将当前项标记失败 |
| `executing|reconcile_unknown|partial_failed|failed|succeeded` | 展示本次 operation、逐目标结果、失败原因和最终 exact-set 回读 |

“刷新设备”只读取并保存新 observation，与清理按钮资格无关。页面直接读取当前 SV 执行授权的持久化 `telegram_login_at` 和服务端派生 `cleanup_button_enabled/cleanup_disabled_reason`，不调用资格接口。按钮置灰时只展示原因；按钮可用时点击直接打开一次确认，确认页展示最后 observation 的我方/非我方/待识别数量作为参考，并明确“worker 将以实际执行开始时读取到的设备集为准，未登记为平台授权资产的 Telegram 手机、桌面、Web 和人工登录都会退出”。确认后直接创建单账号 cleanup batch，不生成 preview。服务端再次以同一持久化登录时间规则决定创建或 skipped，不能信任前端按钮状态。

批量入口同样不调用 Telegram 预检。确认弹窗只展示已选择账号数、清理规则、最近 observation 仅供参考的提示和操作原因，不提前请求全量资格。用户确认后的同一个创建请求在单一数据库事务中去重、分类并落批次，随后返回 `requested_count/eligible_count/skipped_count/skipped_reason_counts`；原因固定包括 `login_age_not_over_48h/login_time_missing/current_sv_authorization_unavailable/account_cleanup_forbidden`。只有 eligible 账号写入 worker 队列，skipped 账号保留账号级结果但不被领取；没有 waiting、倒计时、`next_retry_at` 或到期自动任务。

补齐/迁移抽屉固定提供：

- 动作 `complete_online_abc`、范围 `all_online_accounts|manual`、冻结 `N`、A 新鲜探测计数、B/C 各自 outcome 守恒和 ABC 覆盖分子；全量模式必须明确提示“冻结后失败项仍计入 N”。
- 正常补齐时登录码来源固定为 A，并展示冻结的 `code_source_authorization_id/fact_version/connection_generation`；禁止运行中静默改用 B 或 C。B/C 已健康时页面显示“已验证，无需重新登录”。
- 策略 `malaysia_authorization_only`、目标槽位 `standby_2`。
- 冻结账号范围及分类计数。
- 唯一 MY 节点、固定 MY 出口/secret version、Developer App credentials/owner-domain assertion、environment/client metadata version、故障域、`N`、`target_set_fingerprint` 和各状态计数。
- 验证码来源、托管 2FA 可用性和人工处理项。
- 迁移前后保护设备 manifest。
- MY 本地/硅谷 SSH 镜像两份副本状态与最后校验时间、恢复密钥 readback 事实、MY inventory sequence、隔离 restore probe、恢复闸门和旧 SV 回滚窗口。
- 操作原因、幂等请求 ID 和二次确认。

Developer App 页面必须把线上现有三套 App 显示为唯一 active 角色映射：App A=`SV primary`、App B=`SV standby_1`、App C=`MY standby_2`，并分别展示 `credentials_version/max_accounts/assigned_distinct_accounts/pending_distinct_accounts/used_distinct_accounts/capacity_unlimited/available_accounts/assignment_status`。新账号、补齐与迁移 preview 都冻结这三条映射；页面不得要求“再新增一个 MY App”，迁移同一账号的 App C 新旧代并存只计一个 distinct account。任一角色缺失、重复映射、App 不健康或真实名额不足时，批次项停在精确 blocker，不得临时复用 A/B、选择第四套 App 或通过增加 IP 解决。

账号详情按授权代次展示不可变 `logical_slot`、generation、Developer App/api_id、节点/区域/出口、wake bundle receipt、UID/AuthKey 证明、远端授权存在状态、设备保护、最后显式唤起事实和 operation trace。MY 运行状态只能是 `dormant/provisioning/drill_waking/emergency_code_source/wake_verified/blocked`；无当前 operation 时只能显示 `dormant`。这不影响它在“活跃授权设备”中显示 `remote_authorization_state=active`，页面不得把“休眠”和“已退出”混为一个状态。

“演练唤起”只对 `standby_2 + dormant_ready|wake_probe_required` 开放，完成后必须断连。“紧急辅助重新登录”只在 `primary` 与 `standby_1` 两条失败事实齐全时开放；SV 登录运行时、`primary_regular` 出口、目标 Developer App/client metadata、手机号和托管 2FA/人工通道未就绪时显示 `waiting_sv_login_runtime`，MY 保持休眠。确认页必须写明“MY 只读取官方登录码，结果是在硅谷新建 primary，不会切换或传输 standby_2 Session”，并展示冻结动作、在途 RPC、新 primary generation、回滚边界和审批人。

页面还必须提供四个受权限控制的分页工作队列及状态计数：存量迁移展示 preview/apply/readback、App assertion、contract cutover/runtime/恢复密钥/ACL blocker；活动调查展示 observation、anomaly scope、保护、remote revoke 审批/readback；出口变更展示硅谷/MY 双 IP 的脱敏 diff、HMAC 覆盖和 readback；DR operation 展示 `N`、已处理数、覆盖分子、等待/人工/失败/unknown 计数、expiry/preflight generation、break-glass review、egress hold、事实和 reconcile。列表 cursor 翻页不改变顶部全批次计数。cancel 只在服务端返回 `cancellable=true` 时显示；expired/hold 不伪装完成。筛选、租户隔离、权限和按钮 blocker 均由服务端执行。

## 10. 补齐与存量迁移状态机
### 10.1 存量数据回填与旧写路径切换

存量迁移先完成只读 preview，再允许任何 MY provision。preview 冻结账号范围、旧账号/授权版本、角色、当前指针、Session/Developer App/proxy 密文是否存在、非零设备 hash、现有健康字段和远端读取能力，生成不可变 `target_fingerprint + input_count + conflict_count`；apply 必须携带同一 fingerprint、expected old values、审批和批次幂等键，范围漂移返回冲突，不自动扩大目标。

回填规则固定如下：

1. 账号兼容 Session 与唯一 `is_current`/active primary 一致时初始化 `logical_slot=primary/slot_generation=1/is_slot_current=true/dr_state=current_primary`。只有账号级 Session、尚无授权资产时，必须先证明可解密、`is_user_authorized/get_me` UID 一致及唯一非零当前设备 hash，才以 `source=legacy_account_projection` 创建 generation 1 primary；不一致、多主、无法唯一映射或远端证明不足时进入 `migration_conflict_unknown`，不切 resolver。生成区域无法由权威登录事实证明时写 `provision_region_code=unknown`。
2. 每个角色唯一可用旧授权先映射并冻结 `logical_slot`，再初始化该槽 `generation=1`；当前 standby 初始化 `active_standby`，已停用但未证明 Telegram 撤销的授权初始化 `retained` 或 `repair` 并继续保护。角色冲突或无法唯一映射逻辑槽时进入 `migration_conflict_unknown`；只有撤销 readback 才可初始化 `revoked`。
3. 旧 `standby_2` 不因角色名、Session 非空或旧 `healthy` 自动取得 MY 资格。缺少 MY 密封包时必须在 MY 重新登录生成更高 generation candidate；禁止把现有 SV/中心 Session 复制或重加密成 MY 包。`already_qualified` 只适用于已具备 v2.21 全套双副本、恢复密钥 readback、隔离恢复探测、MY inventory、wake bundle receipt、qualification fact、UID/AuthKey/hash、保护和断连收据的行。
4. 对所有未撤销 Session 计算有效 key version 的 AuthKey blind index，并由一个不是被观测授权的合格 peer 读取远端设备集。只有账号 UID、本地授权资产、Developer App `api_id` 校验和 before/after 唯一差分同时收敛，才回填该授权的非零 hash 并建立 `platform_authorization` protected ref；无法解密、指纹冲突、hash 为零/歧义或只能按 `api_id` 猜测时进入 repair/unknown，禁止伪造值。
5. 只有 legacy primary 且自身视角返回 `hash=0` 时，按固定顺序自举，不要求先伪造 primary hash：先由 primary 冻结 before set，在 SV 使用 Developer App B 创建 standby_1，由 primary 的唯一 after 差分确认 standby_1 非零 hash并提交；再由 standby_1 读取完整设备集，回填 primary 的非零 hash；两者受保护后，才允许在 MY 使用 Developer App C 创建 standby_2，并由 SV 合格 peer 的唯一差分确认其非零 hash。任一步出现多重差分、UID/App/AuthKey 冲突、读取不完整或远端结果未知都停止后续登录，保留已有授权并进入 reconcile，不覆盖旧 Session。已有两槽或三槽账号从最早可形成交叉观察的一对开始，复用同一规则补齐缺口。
6. 历史 egress usage 不得倒推。只能在迁移时从当前运行配置和实时连接 readback 建立 `source=backfill_observed` 的开放 usage 区间；此前 observation 标记 `legacy_usage_unverifiable`，不自动升级异常。只有已有可验证 MY 节点/固定出口事实的授权才能回填 assignment；其余 standby_2 在新 wake bundle receipt 提交时创建 assignment。
7. apply 后逐账号 readback 槽位唯一性、兼容投影、指纹覆盖、保护 manifest、assignment/usage 账和 blocker；计数及 fingerprint 不一致则批次 `failed_hold`，不进入 provision。
8. 既有 Developer App 保留当前 `credentials_version` 并计算 keyed digest；既有环境绑定初始化 `binding_version=1/is_current=true` 和规范化 digest。App owner-domain 证据、egress secret version 或历史 binding 输入无法证明时保持 unknown，不伪造 assertion/qualification。

发布按 `schema -> runtime registration -> preview/apply/readback -> resolver shadow -> contract epoch 提升到 resolver_required -> DB writer/decrypt gate -> 分区 Telegram egress ACL -> 旧实例/client drain readback -> dr_enabled` 执行。cutover preview 冻结 expected contract version、恢复密钥策略 version/digest、允许解密身份集合 digest 和实例/client/ACL fingerprint，经异人审批后 apply；apply 前逐项复核，提交后写不可变 恢复密钥/DB/runtime/ACL readback fact。只有全部非 stale 实例满足最低 capability、旧 client=0、旧应用角色不在 恢复密钥授权身份 集合且真实 decrypt-denied 探测通过、主运行面仅 Gateway、MY 仅 authorization-dr 的 ACL readback 一致才提交 epoch。任一 恢复密钥策略/version/grantee/readback 漂移进入 `failed_hold`，不得只凭配置声明成功。旧进程不能 mutation、解密或直连 Telegram。DR flow 走专用 finish-login 并禁止旧 2FA 轮换；回滚只能提升新 epoch 并降模式，不重新开放旧绕过路径。

### 10.2 正常流程
```text
requested
  -> prechecking
  -> acquire_authorization_control_lease(inventory_mutation)
  -> provision_intent_persisted
  -> remote_before_snapshot_frozen
  -> claimed_by_malaysia_node
  -> login_input_grant_issued -> login_input_grant_consumed
  -> login_started
  -> waiting_code | waiting_qr | waiting_2fa
  -> session_received_in_node_memory
  -> identity_verified -> auth_key_unique
  -> remote_after_snapshot_reconciled
  -> protection_registered
  -> wake_bundle_seal_started
  -> wake_bundle_local_fsync -> wake_bundle_snapshot_created
  -> wake_bundle_both_copies_readback_verified
  -> source_client_zeroized_and_disconnected
  -> snapshot_restore_probe_verified
  -> restore_client_zeroized_and_disconnected
  -> wake_bundle_manifest_committed -> wake_bundle_receipt_readback
  -> candidate_ready
  -> slot_commit_prepared_in_my_inventory
  -> slot_commit_cas
  -> slot_commit_observed_in_my_inventory
  -> release_authorization_control_lease
  -> succeeded
```

Telegram 登录前必须先取得 `inventory_mutation` control lease，再提交 provision intent 和 before device-set hash；lease 必须续到 MY wake bundle receipt、after snapshot 和 slot commit 完成，lease 丢失立即进入 unknown。验证码必须绑定当前 flow、challenge message ID、发送时间和 code source authorization；旧 flow、旧消息不得完成新 operation。验证码和托管 2FA 按 owner epoch 经受限内部 API 单次领取，短 TTL、全程审计、不落盘、不入队列正文。这一段只适用于创建/迁移 standby_2；紧急重建 primary 的 2FA 仅在 SV 使用。

人工 QR 只能在已存在且已冻结唯一 MY 出口版本的 provision/migrate operation 下启动。MY owner 使用同一 login-input grant 发起 Telegram QR challenge，中心只转发绑定 `operation/flow/owner/inventory/target generation/qr challenge generation` 的短时 QR payload；授权页面按权限读取并轮询状态，所有 Telegram 检查仍由 MY owner 执行。刷新 QR 必须提升 challenge generation 并使旧 payload 立即失效；扫码后若进入 2FA，继续使用同 operation 的一次性托管 2FA grant。旧账号级 `/authorizations/login/start|qr/check` 不得创建或完成 MY standby_2，owner 丢失、payload 过期、版本漂移或回调跨 generation 均进入显式 blocker，不能降级到主运行面扫码。

每类 operation 必须在首个不可忽略副作用前持久化边界：provision/migrate 在第一次 login/QR RPC 前写 `remote_effect_started_at + attempt`；drill_wake 在首次 Session 连接前写该字段；`emergency_reauthorize_primary` 在账号 RPC fence CAS 前写 `fence_effect_started_at`，SV `send_code_request` 前写 `remote_effect_started_at`。发码、读码或 SV finish-login 的响应丢失后必须对账，不得盲目重发。

`wake_bundle_receipt_readback` 只有在双副本 readback、恢复密钥解封和 SSH 镜像隔离恢复探测全部通过后，才是 MY 远端登录成功后的第一完整持久边界。receipt 响应丢失时只能按 operation/bundle generation/ciphertext digest/copy manifest 查询或重放同一 manifest 提交，禁止发起第二次登录；后续 slot CAS 失败从已密封候选续跑。SV 业务授权的等价边界仍为 `candidate_secret_committed`，两类 receipt 不得混用。

本流程只使用现有托管 2FA 完成 Telegram 校验，不修改、轮换或回写 Telegram 2FA。需要轮换时必须创建独立 2FA 安全批次。

### 10.2.1 全部在线账号 A/B/C 补齐编排

`complete_online_abc` 复用既有授权候选、登录、探测、commit 和 unknown 对账内核，但不是把 B/C 两个普通动作无约束地同时提交。单账号固定按 A -> B -> C 顺序推进；不同账号可以由持久限速器公平交错，MY 登录仍保持全局单 owner。

```text
create preview with selection_mode=all_online_accounts
  -> freeze all online account ids as N and target_set_fingerprint
  -> approve exact manifest and create N account items + 2N slot results
  -> for each account acquire account authorization control lease
  -> fresh probe frozen A on SV primary_regular
       -> A not authoritative: no B/C login, persist typed blocker inside N
       -> A authoritative: freeze A as code source for this item
  -> B decision
       -> healthy B: fresh identity/AuthKey/hash/App/SV readback -> already_qualified
       -> missing/broken B: A receives this challenge code -> SV/App B provision or repair -> commit/probe/disconnect
  -> re-probe A and verify frozen code-source generations have not changed
  -> C decision
       -> healthy MY C: bundle/copy/recovery-key/inventory/restore/remote-active readback -> already_qualified
       -> healthy legacy SV C: A receives this challenge code -> migrate on MY/App C
       -> missing/broken MY C: A receives this challenge code -> provision or repair on MY/App C
  -> verify C dormant, MY active client=0, B standby_ready, A current and no open unknown
  -> derive one account outcome and release lease
```

A 的“登录码来源”和设备 hash 的“peer observer”是两个独立字段。`code_source_authorization_id` 固定为该账号 A；若 Telegram 当前设备自身视角返回 `hash=0`，允许由另一个合格 SV peer 读取 exact set 并解析唯一非零 hash，但这不把该 peer 变成登录码来源。A 在 B 或 C challenge 前失效、代次漂移或不再是冻结 current 时，写 `code_source_changed|primary_probe_failed` 并停止该账号后续登录，不静默切换到 B/C，不从 `N` 删除该项。

B 的完整成功边界是 SV/App B 独立 AuthKey、唯一非零 hash、同 UID、固定 SV 出口、`standby_ready`、Session 可由 SV 专用 resolver 解密且 provision client 已断连。C 的完整成功边界是 MY/App C 独立 AuthKey、唯一非零 hash、同 UID、MY 本地和 SV SSH 镜像两份不可变副本、恢复密钥 readback、MY inventory、中心 receipt、隔离 restore probe、slot CAS、`dormant_ready` 且 MY active client=0；中心 `session_ciphertext` 为空不是 C 失败条件。

10 账号 canary 是全量合同的强制前置批次，不能拆成“10 个 B 成功”和“另 10 个 C 成功”来拼接。必须冻结同一 10 个账号，逐账号完成 A 前后探测、B/C 槽位结果和账号 outcome；任一 `reconcile_unknown` 立即停 claim，runtime 回到 `off`。只有 10/10 账号结果均为 `already_qualified|succeeded`、B/C 各自 10/10、MY client=0、无保护漂移，并经过至少 24 小时观察窗且期间没有 correction/unknown/新封禁事实，才允许创建全量 `all_online_accounts` preview。观察窗内失败不改写原 canary 分母，修复后必须重新创建新的 10 账号 canary。

新账号采用固定三 App 顺序完成真实登录：App A 在 SV 创建 primary，App B 在 SV 创建 standby_1，App C 在 MY 创建 standby_2；每一步仍按 before/after exact-set、AuthKey/hash 唯一和候选提交验收。设备清理的 48 小时业务门槛不构成后续槽位登录前置条件，因此无需在三次登录之间等待。三个授权创建后应立即出现在“登录设备”Tab；当前 SV 授权登录时间未严格超过 48 小时时只把清理按钮置灰，不阻塞账号日常业务、standby_1 可用性或 MY bundle 提交。接码专用账号仍按既有策略禁止一键清理。

### 10.3 已有本地 `standby_2`

存量迁移的业务含义是“把现有 App C 的 SV `standby_2` 槽位迁为 MY 新授权代次”，不是搬运 Developer App 配置，也不是复制旧 Session：

1. preview 冻结线上 App A/B/C 三条 assignment/credentials version，并唯一识别 App C 对应的旧 SV `standby_2`。缺失、重复或旧备份无法唯一映射时进入 `migration_app_mapping_incomplete|migration_source_standby_not_unique`，不发起登录。
2. 旧 SV 授权继续 `is_slot_current=true + protected_from_cleanup=true`。App C 在 MY 发起一次全新 Telegram 登录，生成新的 AuthKey、非零远端 hash、Session 与更高 `slot_generation + dr_state=candidate`；禁止复制或重加密旧 SV Session。
3. 合格 SV peer 对登录前后 exact set 做唯一差分，确认新设备属于同账号、App C、目标 generation；MY 完成本地不可变副本 fsync、独立 SSH 镜像、两份写后读/摘要/解封、源 client 断连、SSH 镜像隔离恢复 probe、恢复 client 断连、中心 receipt、MY inventory、qualification 和保护回读。设备清理的 48 小时门槛不阻塞这一步。
4. 上述证据全部通过后，先向 MY inventory 追加带 `slot_decision_id + expected old/new authorization + expected slot version` 的 `slot_commit_prepared`，再在中心同一事务幂等 CAS：MY candidate 成为当前 `standby_2`；旧 SV 授权改为 `retained_migration_source + protected_from_cleanup=true`，写 supersede 双向引用。中心 readback 后追加 `slot_commit_observed`。CAS 响应或 observed 写入丢失时按同一 decision 重放：中心已是目标则返回原结果，仍是 expected old 则前滚到目标，其他值进入 conflict 并保护新旧。`primary`、`standby_1`、业务 current、Action/Attempt、listener、online 和同步均不变。
5. CAS 前任一步失败时，旧 SV 备份保持 current，MY 候选按 orphan/unknown 规则对账；CAS 或回读结果未知时同时保护新旧授权，不重新登录。
6. slot CAS 只表示“MY 槽位切换完成”，不是“允许撤销旧 SV”。系统必须再次读取当前 active bundle 的两份副本、恢复密钥解封 fact、MY inventory、中心 manifest 和隔离恢复 probe；全部一致才写不可变 `migration_recovery_gate_passed`。未通过时状态为 `migration_cutover_complete_recovery_blocked`，旧 SV 保持远端 active、retained、protected，本地 Session 密文也保留；不得创建 decommission。
7. 恢复闸门通过后才可尝试创建旧 SV 备份的 decommission。固定使用当时 current SV 执行授权，并以已落库 `telegram_login_at` 判断 `server_now > telegram_login_at + 48h`；MY 新 Session 不承担撤销。未严格超过 48 小时、时间缺失或 current SV 授权不可用时，本次退役项直接 `skipped`，迁移状态为 `migration_cutover_complete_retirement_skipped` 并保存原因；设备页明确显示 `3 个我方当前 + 1 个我方历史`，旧备份继续受保护，不回滚 MY 切换，也不创建自动等待任务。
8. 后续由运营再次发起旧备份退役；满足恢复闸门和 48 小时门槛后，worker 单账号读取 exact set、逐 hash 撤销旧 SV 备份并回读其消失、三个当前 hash 仍存在、MY 双副本/恢复密钥/inventory/restore probe 仍有效且 MY client=0，才写 `migration_succeeded + rollback_window_closed_at`。远端读取/撤销结果未知时保持 `migration_retirement_reconcile_unknown`，禁止删除旧 SV Session 密文、保护事实或重复 reset。
9. 旧 SV 远端授权撤销前发现 MY 恢复失效时，允许通过更高 slot decision generation 把仍 active 的旧 SV 授权重新设为当前 `standby_2`，新 MY 授权继续 protected 并进入修复；禁止回滚数据库或覆盖两边 Session。旧 SV 远端授权撤销 readback 后回滚窗口永久关闭，旧密文即使仍在也不能伪装成可恢复 Session，只能修复 MY 副本或重新登录新授权。

批量迁移对冻结账号集合 `N` 逐账号串行执行上述流程，同一 App A/B/C 可跨账号复用并按 distinct account 统计。迁移不暂停正常 SV 业务，也不在 MY 创建任何业务 Action、ExecutionAttempt、listener 或同步工作。

### 10.4 崩溃与孤儿对账

对账使用独立 reconcile case，operation 在证据收集期间继续保持 `provision_reconcile_unknown`。case 固定流转为 `open -> collecting_persisted_evidence -> collecting_remote_readback（按需） -> decision_ready -> applied|inconclusive|conflict`。客户端不得提交目标终态；服务端只能由不可变 evidence manifest 推导 `confirmed_no_effect`、`sealed_artifact_recoverable`、`orphan_remote_authorization` 或 `inconclusive`。其中 `confirmed_no_effect` 只允许把有 operation/node/owner epoch/运行镜像 SHA 精确证据的 typed 登录失败归一为 `failed|manual_required + remote_call_state=confirmed_no_effect`；无 artifact 永远不能推导为无远端授权。apply 必须按 operation/item/source version 与 evidence fingerprint CAS，重复请求返回同一结果且不重复审计。

- intent 后未产生新设备：允许原 operation 重试。
- login input grant 已消费但 owner 丢失：进入 `login_runtime_lost`；只有无新设备、无 receipt 且旧 flow 已 superseded 的服务端 readback 才允许提升 grant generation 后重试。
- 已存在 matching wake bundle receipt：从已密封 candidate 续跑，禁止重新登录。
- Telegram 已产生唯一新设备但无 matching receipt：标记 `orphan_remote_authorization`，只登记并保护该 hash；设备列表不含 Session/AuthKey，不能绑定或重建 candidate。保护 readback 后释放本次 lease；后续创建 `remote_device_revoke`，由合格旧 peer 按冻结 ref/hash/snapshot 重新取 `inventory_mutation` lease 撤销；readback 前不允许新 generation 登录。
- wake bundle commit 结果未知：只查询同 bundle generation/ciphertext digest receipt；未证明提交或未证明未提交时保持 `wake_bundle_commit_unknown`。
- 本地副本或SSH 镜像损坏：从仍通过 恢复密钥解封的副本生成更高 bundle generation 并重新完成双副本/隔离恢复，禁止覆盖损坏对象或重新登录 Telegram；两份都不可解封时标记 `wake_bundle_unrecoverable`，保留远端设备和全部事实并转人工重新登录。
- 中心库代次落后 MY inventory：全账号授权 mutation 进入 `central_restore_reconcile_required`；把 MY 最大有效 inventory sequence 和 manifest 只增补回中心，禁止 erase、decommission、新 provision 或以旧库状态释放保护。
- before/after 无法读取、出现多个新设备或回调结果未知：进入 `provision_reconcile_unknown`，阻断该账号新登录和设备清理。
- 远端明确为错误账号：立即断开候选并进入 `identity_mismatch`；撤销动作仍需独立远端确认。
- 只有远端和本地都证明未产生授权时才允许取消；远端结果未知不能显示 cancelled。

## 11. 设备清理保护合同

保护集合只按“是否来自我方授权资产”建立，必须包含：当前 primary/standby_1/standby_2，所有非终态 candidate，以及所有 `retained/repair/invalid/unknown` 且未有 Telegram 撤销 readback 的授权。官方手机/桌面/Web、历史手工登录、同 `api_id` 额外登录和未知 API 客户端，只要没有精确匹配上述我方授权 hash，就不在保护集合中。

一键清理固定流程：

```text
批量/单账号提交时只读本地当前 SV authorization + telegram_login_at
  -> server_now <= telegram_login_at + 48h、时间缺失或授权不可用：直接 skipped
  -> 同一事务持久化 eligible 执行项与 skipped 结果
  -> 创建响应返回 requested / eligible / skipped / skipped_reason_counts
  -> eligible 项进入异步 worker；不做批量 Telegram precheck
  -> worker 逐账号调用 account.getAuthorizations
  -> 精确匹配我方 protected hashes，其余非零 hashes 分类为 external
  -> 任一 unresolved / 保护 hash 缺失则当前项 failed，继续下一账号
  -> 冻结执行开始时的 snapshot digest + protected manifest/version + external target hashes
  -> 按冻结非零 hash 逐个 account.resetAuthorization
  -> 再读 Telegram exact set
  -> 确认目标全部消失、我方 protected hashes 全部仍存在，且没有新增 external/unresolved
```

任一受保护授权 `hash=0`、缺失、歧义、快照过期、operation 非终态、slot version 漂移或 apply 后 readback 未知时，设备清理 fail closed。不能只按 API ID、设备名、地区或“非当前 Session”判断，也不得调用 reset-all API。

清理执行授权固定为批次创建时的 current SV authorization，不在 primary/standby_1 之间动态挑选；必须冻结 `executor_authorization_id/fact_version/telegram_login_at`。资格只读取该授权已经持久化的 Telegram 登录时间并使用服务端时钟判断 `server_now > telegram_login_at + 48h`；恰好 48 小时仍 skipped。不得使用账号创建时间、last active 或前端时钟替代，时间缺失时直接 `skipped/login_time_missing`。MY standby_2 不作为日常 cleanup executor。

资格判断只有用户确认后的创建事务内本地数据库读取，不建立 precheck API，不连接 Telegram，不产生 waiting/倒计时/next_retry_at。必须满足 `requested_count = eligible_count + skipped_count` 且 `skipped_count = sum(skipped_reason_counts)`，每个 requested 账号只保存一个 eligible 或唯一 skipped reason。单账号详情使用同一派生字段决定按钮置灰，但服务端创建时必须重新计算一次；current authorization/fact/login_at 在创建事务中漂移时，该账号直接 `skipped/current_authorization_changed`。

worker 逐账号持有 `inventory_mutation` control lease，并在执行开始时读取 Telegram exact set；该读取超时或失败时当前项写 `failed/device_list_read_timeout|device_list_read_failed`，释放 lease 后继续下一账号，不重试到阻塞整批。每个 target 在 reset RPC 前持久化 `remote_effect_started_at + attempt`。即使已超过 48 小时仍收到 `FRESH_RESET_AUTHORISATION_FORBIDDEN`，当前项直接 `failed/telegram_fresh_reset_rejected`，不等待、不自动重试；已有部分撤销时先完成 readback并写 `partial_failed`。RPC 结果 unknown 进入 `reconcile_unknown`，禁止自动再撤销同一 hash。目标已消失可记 `target_already_absent`，但仍必须完成我方保护集回读。

一键清理成功的唯一产品口径是：冻结 external targets 全部不在新鲜 Telegram exact set 中，所有冻结 protected hashes 仍在，且最终 observation 中没有新增 `external/unresolved`。执行中出现的新 external 不得未经新确认自动撤销；本次 operation 记 `partial_failed/new_external_detected_after_apply`，保留新设备明细并要求运营重新提交新清理批次。只有 RPC 返回成功、worker 结束或本地批次 `success` 都不算完成。本操作只撤销 Telegram 远端非我方授权，不删除本地授权资产、Session/wake bundle、observation、审计或历史快照。

retained 授权必须定期收口，不允许无限累积：当当前 primary 为 `primary_authorization_healthy`、standby_1 为 `standby_ready`、standby_2 为 `dormant_ready`，三者 health basis 均未失效，且无非终态 operation、open activity review 或保护漂移，retained 授权稳定超过可审计保留期（默认 30 天）时进入 decommission 候选清单。页面提醒双人审批撤销，系统不自动撤销；retained 数量或远端设备数超过阈值时告警。

## 12. 硅谷本地切换、紧急重建与业务模块边界

### 12.1 primary 到 standby_1 的自动本地切换

当当前 primary 有权威授权失败事实，且同一账号当前 `standby_1=standby_ready` 的即时 SV probe 成功时，系统自动创建并执行幂等 `local_activate`，不等待人工批准。普通网络抖动、单次 timeout、旧在线状态或页面手工标记不构成触发条件；若 standby_1 probe 不通过，则不切换并继续按双路径失败条件判断。整个流程 MY operation、wake permit、client、code grant 和 activity fact 必须全部为 0。

```text
primary_failure_confirmed
  -> standby_1_immediate_probe
  -> acquire_control_lease(switch)
  -> freeze_new_business_claims
  -> fence_old_connection_generation
  -> drain_gateway_and_classify_inflight_attempts
  -> current_authorization_and_account_projection_cas
  -> invalidate_old_online_listener_sync_leases
  -> rebuild_gateway_runtime
  -> online_warming_and_immediate_probe
  -> listener_reclaim_and_runtime_summary_refresh
  -> unfreeze_new_business_claims
  -> sv_local_redundancy_degraded
```

切换 CAS 必须在同一事务更新唯一 `is_current`、账号 current authorization/fact/connection generation、兼容 Session/App/proxy 投影和业务状态。CAS 前已开始 Gateway RPC 的 Attempt 留在旧 generation 收口；CAS 后尚未开始 RPC 的工作不得再取得旧 Session。新 current 的即时 probe、所有已启用模块的旧代次写入屏障、listener/sync 旧 lease 失效和 runtime summary 刷新完成前保持 `business_runtime_status=warming`；屏障生效后即可开放新 generation 领取，不要求等待一次完整 listener 或同步任务跑完，并显示 `business_runtime_status=degraded + sv_redundancy_status=degraded`。

切换后系统自动创建 `restore_sv_pair`：使用 Developer App A 在 logical primary 创建更高 generation 的 SV candidate，完成 UID/AuthKey/hash/环境绑定和即时 probe 后，以同一套冻结、Gateway drain、CAS、模块重建流程受控切回 logical primary；logical standby_1 回到 `standby_ready` 后才标记 `sv_redundancy_status=healthy`。修复或切回失败不停止已经由 standby_1 承载的业务，只保持 degraded 和可见 blocker。降级期间当前 standby_1 再失败时，按两个 SV 路径均失败创建 `emergency_reauthorize_primary`，不得把 MY 变成业务 current。

### 12.2 紧急重建启动条件

只有当前 `primary` 和 `standby_1` 的两条 typed failure fact 同时成立，才可创建 `emergency_reauthorize_primary`。

operation 必须冻结账号、两个 SV 失败事实、当前 standby_2/bundle generation、目标 primary generation、SV Developer App/client metadata/手机号版本、`primary_regular` 出口、审批和幂等键。若控制面、SV 登录运行时、出口或必需登录材料未就绪，状态为 `waiting_sv_login_runtime`，MY 保持休眠。基础设施故障本身不构成 MY 连接许可。等待期间 primary 或 standby_1 恢复时，在任何发码副作用前以 `superseded_by_sv_recovery` 结案。

### 12.3 紧急重建状态机

```text
requested -> sv_primary_and_standby_1_failures_ready
  -> waiting_sv_login_runtime -> sv_login_runtime_ready
  -> normal_approved | break_glass_approved
  -> acquire_control_lease -> freeze_account_rpc_claims -> drain_gateway
  -> sv_new_primary_login_started(send_code_request)
  -> telegram_code_delivery_classified
       -> manual_required_sms_email_call_qr
       -> app_session_delivery
          -> my_code_source_waking -> my_code_source_authorized
          -> my_poll_777000_bound_to_challenge
          -> one_time_code_grant_issued -> one_time_code_grant_consumed
          -> my_disconnected_and_fenced
  -> sv_finish_login_with_code_and_managed_2fa
  -> sv_new_primary_candidate_committed
  -> uid_authkey_device_proof -> primary_slot_and_account_projection_cas
  -> sv_new_primary_probe
  -> invalidate_old_online_listener_sync_leases
  -> online_warming_and_immediate_probe
  -> listener_reclaim_and_runtime_summary_refresh
  -> active -> unfreeze_account_rpc_claims
```

SV 先调用 Telegram `send_code_request`，以其返回的 `SentCode.type` 作为权威渠道判定。只有 app-session delivery 才创建 MY wake permit；SMS、email、call、QR、未知或过期渠道一律进入 `manual_required`，不唤起 MY，不伪造登录码成功。

MY 解封当前 bundle 后只允许 `is_user_authorized/get_me/AuthKey/当前设备 hash` 和读取 Telegram 官方服务对话 `777000`。登录码必须绑定同一 operation、SV login flow、target primary generation、`challenge_sent_at`、`phone_code_hash_digest`、服务消息 ID/date；只接受 challenge 之后到达且唯一匹配的消息，不得读取“最新一个验证码”完成新 flow。登录码只以短 TTL、单次消费的内存 envelope 交付给绑定 SV runtime，数据库只留 digest/收据，不复用可展示 code preview 的通用验证码表。

SV 消费 code grant 后，MY 立即断连、撤销 wake permit、提升 connection generation 并写 zeroize receipt。托管 2FA 只能在 SV `finish_login` 时使用，MY 不获取、不修改 2FA。最终必须在 SV 生成全新 AuthKey/Session，以更高 primary slot generation 提交 candidate；AuthKey 必须与 primary、standby_1、standby_2 均不同。完成 CAS、SV 即时 probe、已启用模块旧代次屏障、listener/sync 旧 lease 失效和 runtime summary 刷新后才可解冻业务。全流程不改 standby_2 的 slot generation、`is_slot_current`、bundle generation 或休眠资格。

### 12.4 未知结果与任务族切换矩阵

`send_code_request`、code grant 消费或 SV `finish_login` 响应丢失时进入 `reconcile_unknown`。若 Telegram 可能已创建新授权，必须保留 candidate/orphan 和设备保护，对账 receipt、UID、AuthKey 与远端设备集；不得盲目请求第二个登录码或自动回复旧 owner。

所有 `local_activate`、`restore_sv_pair` 和 `emergency_reauthorize_primary` 必须使用同一任务族矩阵：

| 业务对象/阶段 | 切换行为 |
| --- | --- |
| 未领取的 Task、未创建 Attempt 的普通 Action | 保留原业务义务；解冻后重新规划，新 Attempt 解析并冻结新的 current authorization/fact/connection generation |
| 已领取但尚未进入 Gateway 的普通 Action | 释放旧 claim 或结束未调用 Attempt，再按原 Action 幂等键重新领取；不得把同一 Attempt 原地改绑新授权 |
| 固定授权/环境的搜索点击、入群 assignment | Gateway 前释放旧 assignment 并以 `authorization_switched` 重排；创建绑定新 authorization/environment/proxy 的新 assignment，旧 assignment 不修改。无法重排时进入业务 waiting/shortfall，不偷换绑定 |
| `gateway_call_started_at` 已存在 | Attempt 永久保留旧 authorization/fact/connection/environment/proxy；按远端事实进入 success/failed/unknown_hold，切换不自动重发 |
| 已有权威 remote fact | 原事实、消息 ID、成员关系和计数不迁移、不改写、不重复履约 |
| listener | 切换使旧 lease 失效；旧 generation 拉取结果丢弃且不推进 cursor，新 generation 重新领取并从最后已提交 cursor 继续 |
| online/keepalive | 旧 job/result 丢弃，不覆盖新状态；新 generation 先写 warming，再即时 probe 后投影 online/offline |
| 联系人、群组、资料同步 | 旧 generation 结果不得推进成功状态；未产生远端副作用的同步按原业务规则重排，远端结果未知则 reconcile，不自动重复写 |
| runtime summary/账号列表 | 只聚合与账号 current authorization/fact/connection generation 一致的数据；不一致显示 warming/stale，不沿用旧 online 或 listener 健康 |

MY 的 Action、ExecutionAttempt、listener、在线探测、同步记录和业务 remote fact 数量始终为 0。“新 primary 授权恢复”不等于“发送恢复”，发送仍需按任务类型验收 Telegram 权威 remote fact。

## 13. 迁移、清理、恢复与回滚

- 存量 `standby_2` 如果没有 MY 密封唤起包，必须在 MY 重新登录生成独立 AuthKey；不得复制现有 Session。新候选的双副本/恢复密钥/inventory/restore probe、bundle receipt、qualification 和 slot CAS 全部通过前，旧授权和旧包保持 protected。
- 每个 bundle generation 和两份副本均不可变；新建、修复或重封装只能写更高 bundle generation。active 指针只有在新代双副本/恢复密钥/隔离恢复/中心 readback 完成后 CAS，旧代再转 retained；失败或 ack unknown 时保留全部代次，不原位覆盖、不删除。
- 中心库从旧备份恢复后，先进入 `central_restore_reconcile_required`，禁止 provision/migrate/decommission/erase 和授权设备清理；再按账号 UID、AuthKey blind index、slot/bundle generation、copy manifest、ciphertext digest 和 MY write receipt，读取独立 MY inventory 的最大有效 sequence。中心只能补写缺失的新代事实；对仅有 `slot_commit_prepared` 或中心已丢失提交记录的决策，按冻结 expected old/new 幂等前滚并追加 observed，不得把 MY 更新降代、标孤儿、覆盖或擦除。全部账号 readback 收口后才解除 mutation hold。
- 迁移 rollback 只能在旧 SV 远端授权仍 active 且 protected 时通过更高 slot decision generation 前滚完成；不得通过恢复旧数据库改 current。旧 SV 撤销 readback 后 `rollback_window_closed_at` 永久关闭，旧密文仅作审计保留，不再计可恢复副本。
- `local_activate` 成功不等于三槽恢复完成；只有 logical primary 更高 generation 可用、受控切回完成、standby_1 恢复 standby_ready、standby_2 仍 dormant_ready，且在线/listener/runtime summary 均绑定新 current generation，才可从 `sv_local_redundancy_degraded` 结案。
- 账号删除采用两阶段生命周期。第一阶段软删除同一事务写 `account_lifecycle_status=business_deleted_authorizations_retained`，立即移出 Planner/Dispatcher 候选，阻止新 Action claim、listener、online、联系人/群组/资料同步、登录补齐、演练和紧急唤起；未进入 Gateway 的工作取消或释放，已进入 Gateway 的 Attempt 保持原代次对账。账号继续出现在“已删除账号/待退役授权”视图并展示三槽、远端设备和退役 blocker。
- 第二阶段由显式 authorization decommission 逐项撤销我方远端授权并完成 readback；仅该流程可为撤销目的短时连接现有 SV 平台授权，不得执行业务或读取登录码。由合格 SV peer 逐个撤销其他平台 hash，最后一个 SV revoker 使用 Telegram 当前授权退出接口并保存响应；任一步结果未知都进入 `retirement_reconcile_unknown`。MY standby_2 由 peer 远端撤销确认后只做离线包擦除，不为退役唤起。远端撤销、本地副本擦除、SSH 镜像擦除、中心密文擦除和 bundle DEK 引用释放分别写独立 receipt；任一步 unknown 时保持 `erase_pending|retirement_reconcile_unknown`。全部平台授权已撤销、全部副本擦除且无 unknown 后才写 `account_lifecycle_status=authorization_retired`；external 设备不属于平台退役分母。软删除、数据库行删除或通用 cleanup 都不得伪装成远端退役完成。
- 通用任务清理、Action/Attempt 归档、登录 flow TTL、账号软删除、数据压缩、硅谷备份轮换和 MY 主机快照轮换都不得删除当前或 protected 唤起包、任一可恢复副本、receipt、MY inventory、wrapped DEK、仍被引用的 恢复密钥版本 和设备保护事实；任何轮换都不得把 active/retained 授权降到两份可恢复副本以下。
- 只有显式 decommission 完成异人审批、Telegram 远端撤销 readback、无其他冲突非终态 operation/lease、保留期到期且不存在 active/retained 引用，才允许逐份擦除 MY 本地包、SSH 镜像、中心密文和 wrapped DEK，并分别保留 erase receipt。共享 恢复密钥版本 仍被其他 bundle 引用时不得退役。
- 发布回滚只停止新 operation/claim，不删除已创建的 Telegram 设备、MY 包、已提交 generation 或 unknown。SV 新授权远端结果未知时必须先对账，不能自动回滚或重新发码。

## 14. 实施、验收与开发交接

API、权限、敏感数据、保留清理、失败码、指标、发布、QA 和开发交接的规范性合同见 [account-malaysia-standby-session-dr-implementation-contract.md](account-malaysia-standby-session-dr-implementation-contract.md) v2.21。两份文档共同构成本 PRD；实现、QA 和发布不得只选择其中一份。

当前 `design_status=complete`、`product_resync_status=complete`、`dev_handoff_ready=true`。已部署的 `standby_2` 迁移核心、unknown 原字节对账、guarded `local_activate` 与两账号槽位 canary，和本次新增的全量在线 `complete_online_abc` 合同是不同完成层级；后者代码、QA、发布、10 账号 E4 和全量 E4 均未开始。开发必须先实现批次冻结/双槽守恒/A 码源 fence，再补齐 B provision/repair 与 C migrate/repair 编排，之后才进入 10 账号 canary；不得直接用既有 271 批次或数据库 ready 投影宣称全量三槽完成。

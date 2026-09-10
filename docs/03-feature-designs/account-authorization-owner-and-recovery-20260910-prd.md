# 授权连接唯一归属、失效账号恢复与去代理功能补全方案

## 状态与范围

> **本轮执行授权补正：** 用户随后明确要求“先恢复账号，再处理前面的问题，账号是核心”。恢复阶段先保存当前版本和精确容器/授权快照，临时停止本项目 Telegram 消费进程并禁用其自动重启；保留其它项目和 MY 密封资产。允许通过单一维护环境逐账号执行已有正式 local_activate，每个探测/切换/验证阶段使用退出后可证明连接已关闭的独立进程，避免同一备用被多个客户端重用。已恢复授权先保全；旧多进程调用路径在连接归属修复并验证前不重新放行。该方式使账号授权恢复先于完整架构修复，不宣称此时业务吞吐已恢复。无健康备用的账号继续核对用户可用接码/已登录设备，不因其阻塞搁置其它有备用账号。

- 2026-09-10 用户要求核对失效账号、跨角色连接、排名观察和直连熔断的解决方式。
- 本文件为已取证的修复方案，`design_status=authorized_in_progress`，不是已实现或已发布声明。生产观察版本为 `aaf5751c5c40fbca6e7696704d9a24511fac3f29`。
- 恢复执行已获用户授权：在隔离维护环境中逐项正式切主和真实验证；尚不唤起 MY，不重发 unknown，未删除恢复范围严格为 318 个。
- 继续停用项目代理。原固定直连合同只解决出口选择，不能证明授权连接归属已经安全。

## 1. 已验证的问题

### 1.1 失效授权不等于账号永久丢失

2026-09-10 23:07:28 北京时间的一致只读快照：current 授权 `invalid` 共 323 个，错误均为 `authorization_key_duplicated`；其中 5 个账号已删除且禁用，未删除的实际恢复候选为 318 个，账号状态均为 Session 失效。此前恢复的 513、175、10 不在这 318 个 current-invalid 集合中。

| 互斥分类 | 数量 | 证据与处置 |
| --- | ---: | --- |
| 符合现有 local_activate 备用元数据条件 | 141 | SV、slot-current、物理 standby_1、healthy、active/standby、中心凭据和 App 存在；仍需新鲜身份探测、operation/unknown 冲突检查及代次 CAS，不是保证可恢复 141 个 |
| 合法互补 SV 槽被旧谓词拒绝 | 2 | 账号 8、11 的备用 role=standby_1，但 physical logical_slot=primary；授权 ID 分别 2849、2852，现有 `_require_switchable_target` 硬编码物理 standby_1 |
| 历史 SV standby_2 特例 | 1 | 账号 407、授权 734，SV/healthy/standby/slot-current；不能假装是普通 standby_1，也不能放宽为任何 healthy standby_2 均可切主 |
| 没有健康备用 | 174 | 173 个没有非 current 授权记录；账号 176 留有两个 failed/needs_repair 的 SV 备用。该集合没有中心 wake bundle 记录，不能宣称可凭现有 MY 备份恢复 |
| 已删除账号，不自动恢复 | 5 | 从 323 中单列排除，保留历史失效和需求记录 |

未删除集合中“有任意健康非 current 授权”的账号是 144 个，不是 149 个；149 包含上述 5 个已删除账号。健康授权行数不能相加为独立可恢复账号数，也不能把 role、logical_slot、region 与 current 混为一谈。

未删除失效集合有 142 个账号引用 MY 密封包；中心记录为 active、KMS verified、2/2 副本及历史 passed restore probe。它们与 SV 候选高度重叠，不是额外 142 个账号，也不属于上述无健康备用的 174 个。此处只证明中心保存了证据；新恢复必须再核对 MY 当前本地包、receipt/manifest 和恢复方式。

`standby_repair/needs_repair` 是待评估资产，不因字符串含 repair 就当作可自动修复；failed、unknown、retained 各自保留原事实，既不丢弃也不直接升为 healthy。

Telegram 官方说明：收到 AUTH_KEY_DUPLICATED 时原 session 已被服务端失效，需新授权；复制旧密文、改状态、重启或更换出口不能使原 AuthKey 恢复。账号本身能否重新授权取决于其它有效独立设备、受控手机号接码、2FA 和 Telegram 账号状态，不能从无备用推断永久封号。

### 1.2 直连不是连接唯一归属证明

官方错误合同指出：非媒体 DC 的同一授权在多条 TCP 主连接上并行请求，即使来源 IP 相同，也可能触发 AUTH_KEY_DUPLICATED；只有明确获得 parallel-main-session 许可时才可按授权数量并行。媒体 DC 文件连接是单独例外，不能泛化为普通主连接权限。

生产配置中两个 dispatcher 为 2 分片；其它 17 个应用进程（含 backend，角色并非互不重复）为 1 分片。分片只决定该入口选择哪些账号，不构成跨角色、跨进程的连接互斥，也不能说明不调用 TG 的 planner/生成角色实际持有连接。

本地对当前 `TelethonClientLifecycle` 使用无网络测试客户端得到两个反例：

1. 同一授权/同一 metadata 同时请求缓存，创建 2 个 connected client，缓存只保留 1 个；锁只覆盖读写字典，没有覆盖正在建立连接的阶段。
2. 同一授权使用不同 metadata，创建并缓存 2 个 connected client；metadata 被当成缓存身份的一部分。

此外缓存和锁均为进程内对象，health/identity 等入口还会创建独立 client。业务资源层已存在 `account_remote_inflight`（同账号在途 1）及跨账号池测试，但不能覆盖监听常驻连接、探测、登录维护，也不能关闭已经存在的空闲 TCP 连接。

### 1.3 搜索与排名观察未适配直连

搜索点击已有运行任务被 `telegram_account_proxy_forbidden` 拦截。排名观察 `_verify_rank_deboost_proxy_guard` 要求正数 runtime_proxy_id 且与凭据相同，直连凭据无法满足，产生 runtime_proxy_mismatch。当前未删除 rank_deboost 任务为零，因此没有当前运行实例损失，但功能不可用是事实，不应靠没有任务隐藏。

### 1.4 熔断没有塌缩成全局，直连域却缺少明确模型

`proxy_domain_keys` 返回两个空字符串时，代理容量检查不适用；`_domain_keys` 始终生成 account:<id>，仅追加非空 route/egress。生产快照没有空 domain_key 的熔断行，存在 4 个 open account 域；历史 proxy_route 域保留，不能解释成直连的全局域。此快照活动 pool lease 为零，不据此证明全部业务路径资源保护已启用。

## 2. 修复顺序与验收

### P0：维护期独占连接，业务期统一连接归属

1. 建立 `(tenant, authorization_id/AuthKey digest, connection_generation, main DC)` 的唯一主连接 owner；账号 ID、App 名称或 metadata 不作为另开相同授权主连接的理由。源 AuthKey/Session 不写日志。
2. 同进程对相同身份的建连使用共享进行中结果，原子地创建、复用或显式失败；固定授权 metadata，配置变更必须走有旧连接退出证据的更新。
3. 跨进程采用授权 owner 路由/服务；dispatcher、search、listener、online、security、sync、登录后维护及诊断经 owner 复用主连接。业务编排仍属于各 worker，不把规划搬入网关。每个入口必须列出接入状态。
4. owner 接管需要 fencing generation；旧 owner 的取消请求、租约过期或进程失联本身不足以证明远端调用已经结束。先得到连接/进程退出证据，再放行新代次；业务 unknown 的对账身份继续保留。
5. 保留现有单账号业务远端在途 1，并补 PostgreSQL 双事务争用与多进程测试。监听订阅和同 owner 的状态读取不应被误当作永久占用一条业务发送名额；主连接个数与业务调用在途分别验收。
6. 回归覆盖：同键并发建连、不同 metadata 同授权、跨角色同时请求、owner 崩溃/网络分区、旧代次迟到、切主后旧入口、媒体连接例外、不同账号并行。先使两个已复现反例通过，再证明跨进程链路，不以单进程通过冒充全链路完成。

### P0：恢复队列与不可用账号分母

1. 318 个账号形成有版本的恢复清单；每项冻结 current/候选/代次/证据时间/分类/阻塞原因。`metadata_candidate`、`probe_verified`、`activated_pending_verify`、`restored_degraded`、`manual_reauthorization_required` 分开统计。这些是拟议恢复报告状态，不冒充现有生产字段。
2. local_activate 资格要求 role=standby_1、is_current=false、disabled_at 为空，再按 canonical current 之外的互补 SV 业务备用识别，接受合法 physical primary/standby_1 互换；仍要求独立 AuthKey、同 Telegram 身份、不同合格 App、slot-current、未禁用和健康证据。修复账号 8/11 的谓词，不通过改库重命名槽位绕过。
3. 141 个候选和修复后的 2 个互补槽候选，逐项正式 preview/新鲜探测、冲突检查、fingerprint、审计 apply、warming、Saved Messages 真实发送读回、在线读回；原失效授权保留 protected，不回切失效 AuthKey。批量恢复须先证明本节顶部规定的维护期单进程独占；业务 owner 未验收前不恢复业务流量。
4. 407 的独立读回确认：734 是 SV 的历史 App C 授权，无 MY wake bundle；失效 current 733 使用 App B。不能把 734 改名后塞入普通备用槽，也不把 App C 变为 SV 业务 current。先独立探测 734 的身份；若可用，仅作为受控登录码来源，为该账号通过正式备用登录入口生成新的 SV App A 授权，再执行正式 local_activate。原 734 和 MY 边界保留；没有新登录码或 2FA 验证失败时保留明确阻塞。
5. 174 个逐项核对合法的外部已登录设备、手机号接码能力和 2FA 来源；2026-09-11 的补充元数据读回发现 1831、1914 已保存 verified_readable 接码绑定及 telegram_accepted_import 密码来源。两个接口当前为空验证码基线，只能记为具备尝试正式重新授权的入口，不能记为已恢复；其余 172 个没有接码绑定。173 个无备用记录不能从空数据中重建授权，176 的 failed 备用先保留原权威错误。存在合法凭据则正式重新授权生成新 SV AuthKey；缺接码/2FA或受 Telegram 限制时明确保留 blocker，不能写成永久损失或伪造可恢复。
6. MY 仅在 MY 本地按紧急登录码读取合同参与重新生成 SV 授权，不交 Session 给 SV、不当业务 current。现有代码尚无完整 emergency_reauthorize_primary/restore_sv_pair 交付入口，必须先实现并验收相应状态机，不能调用文档中尚不存在的流程。
7. 每个已恢复账号补齐 SV 备用冗余，再按既有 MY 证据恢复灾备完整性；切主成功仅记 restored_degraded，不声称三槽恢复。
8. 隔离只改变当前供给，不静默删除已冻结 selected 集合、日目标或账号覆盖义务。新任务展示当前可执行/待恢复供给；旧任务保留原分母，分别显示数量欠额、账号覆盖欠额和恢复分类。账号跨过原任务日恢复，不追溯伪造旧日完成、不重放 unknown。

### P1：保留搜索产品能力，版本化迁移到直连证据

1. 拟增加 direct 模式，证明 owner/授权代次/地域/实际出口；不伪造 proxy_id 或把空 proxy 视为代理证明通过。
2. 搜索点击仍须真实 target_click_observed；排名观察须保留关键词、目标、真实排名观察和上下文证据。直连只代表该固定出口视角，不能声称多代理/多地域代表性或独立 IP 数。
3. 新旧合同版本显式区分，页面/API 在未适配前准确显示不支持/阻塞。旧运行任务迁移须保留已确认计数、原目标和未决调用身份，以审计版本变更处理，不暗改旧义务证据语义。
4. 验收零代理依赖、真实成功/目标不存在/权限拒绝、出口不符、旧 proxy 合同拒绝、未知结果不重放；没有运行 rank 任务不能代替新功能验收。

### P1：直连容量与熔断域显式化

1. 区分 transport_mode=direct 与历史 proxy 绑定，界面/指标明确代理并发参数不适用于 direct，而不是展示参数仍生效。
2. route/egress 使用明确的地域 direct 身份进行归因，直连预算及策略单独版本化；不得把原代理上限 2 原样套给全部账号共用出口，否则会造成全局限流。
3. account 熔断继续按账号隔离；单账号授权失效不触发全出口停用。区域网络熔断需独立的跨账号网络证据与已批准策略，不能从单个 session/权限错误推断。
4. 测试空 proxy key 只产生 account 域、账号 A 熔断不影响 B、历史 proxy 熔断不影响 direct、direct 网络故障不会误记为全体账号授权失效。新增 direct 上限的具体值须用容量与吞吐验收确定，不在本评估中随意指定。

## 3. 本轮业务 owner 实施合同

- SV 的正式网关统一由独立 `telegram-owner` 进程持有；现有 backend/worker 保留业务职责，通过项目私有 Unix socket 调用同一正式网关。IPC 仅采用白名单类型的 JSON 编码，不接受 pickle、动态模块加载或任意方法执行。Socket 和单实例锁放在独立共享目录，不放入公开 media。
- 单实例进程持有同一宿主文件锁直至进程退出；不以租约超时启动第二实例。上线须先确认旧 20 个业务进程全部退出，再启动 owner 和客户端。MY 节点保持本地区域的独立授权职责，本次不触碰其密封会话。
- 网关按实际 AuthKey 摘要串行执行同一授权的调用，不以账号池、App、设备 metadata 或 worker 分片区分主连接。不同授权继续并行，不增加全局业务并发上限。
- 普通任务、监听、在线检查、身份探测和素材读取复用 owner 的同一主连接。缓存建连采用 single-flight；显式设备 metadata 变更须先严格关闭旧连接再更换，空 metadata 读取复用已选身份。正在调用的连接不参与空闲清理。
- 登录创建的新会话由 owner 自己持有；已有 temporary session 按同一授权摘要归属。切主后的请求携带授权身份与代次，owner 在执行前核对当前数据库事实；旧代次拒绝执行，失效授权不会因旧缓存继续调用。
- IPC 请求有发送前/已提交两个边界。连接失败且尚未提交为明确未调用；已提交后连接中断保留 unknown，不自动重发或宣布远端终止。worker 退出不等于 owner 上远端调用结束；原 unknown 的业务身份继续保留。
- 启停与回滚按正式本地直传发布执行。owner 不可用时显式报错，业务容器不得本地另建 Telegram 客户端作为降级；部署验证必须包含 owner 唯一进程、所有客户端配置、真实账号读取与消息验证。

## 4. 交付证据

源代码与测试、部署版本、owner 连接证据、逐账号恢复、各任务业务事实分别验收。仅凭停代理、没有新增 duplicate、健康检查或恢复 3 个样本均不能关闭全部问题。

只读盘点与本地反例文件位于本次评估目录，恢复清单包含账号/授权 ID，不含手机号、Session、AuthKey 或密钥。现有 dirty 主工作树保持未修改。

参考：[Telegram API 错误合同](https://core.telegram.org/api/errors)、[现行直连切换合同](account-direct-egress-cutover-20260910-prd.md)、[灾备合同](account-malaysia-standby-session-dr-prd.md)、[统一履约合同](unified-engagement-fulfillment-engine-prd.md)。

# 搜索点击与排名观察的当前授权直连合同

> 状态：本地实现与定向验证完成，冻结发布前复验中；属于 2026-09-10 授权归属与去代理补全的 P1。实现、发布与真实业务验收分别记录，不用本文状态替代生产事实。

## 1. 范围与产品语义

适用 `search_click` 与 `search_rank_deboost`。新传输合同为 `sv_current_direct_v1`；搜索点击的 `fact_first_v3` 履约合同独立保留。用户目标、关键词、目标群、日数量、独立账号覆盖、原始截止时间、排名观察、豁免对象与禁止误点/误入群语义继续生效。

新建任务显式保存该传输版本。没有传输版本的历史配置按旧代理合同解释，不以空代理字段自动宣布迁移。历史确认事实、旧 unknown、原始义务和账号范围不重算、不删除；旧代理合同不允许重新打开代理或默默直连。

只说明固定 SV 直连出口的搜索/排名视角，不宣称多代理、多地域、独立 IP 数或推广效果。真实点击与目标不存在、权限拒绝、验证码/协议阻塞仍按各自事实记录。

## 2. Planner、执行与证据

1. Planner 通过正式当前业务授权 resolver 选择同账号、同租户、SV 的 current 授权，冻结授权 ID、fact version、授权代次、连接代次及 App 身份；不遍历备用槽寻找可用搜索身份。MY 密封备用不参与搜索。
2. 搜索复用 owner 已选择的客户端身份与同一主连接，不另外建立搜索专用连接，不借历史代理环境绑定切换 device metadata。新合同记录 `client_metadata_policy=owner_managed`，不把空 metadata 写成“已通过旧指纹 guard”。
3. Action 中代理 ID/代理绑定 ID 保持真正缺省；排名 payload 仅在新直连合同下允许 node ID 为 null。旧合同仍要求原有合法字段。不能制造 ID 0、虚拟代理或 `proxy_egress_guard=verified`。
4. 执行前重新解析 current 并核对冻结代次；owner 在正式调用前再次进行现有授权 fence。旧身份/代次明确拒绝，不转用另一个授权。
5. 部署显式冻结 `TELEGRAM_DIRECT_EGRESS_REGION=sv` 与 `TELEGRAM_DIRECT_EGRESS_IP`。值来自当前部署出口读回。owner 通过不读取环境代理的 HTTPS 出口探测核对该 IP，并记录来源、时间、owner 实例、授权/代次和直连合同版本。Planner 不伪造验证完成事实。同一 owner 实例、地域和固定 IP 策略只保存首次成功的真实观察，后续请求引用原观察时间；不把缓存读回改写成新的网络观测。实例或固定出口策略改变后重新取证，探测失败明确暴露且不缓存为成功。
6. HTTP 出口观测用于核对该主机直连路径，和 Telegram 自身返回的授权 IP 分开。Telegram 授权 IP 为空时不能冒充已经得到 Telegram IP 证明。网关的零代理字段约束、owner 归属与实际请求结果另行留证。
7. 出口探测失败、配置不一致或 owner 上下文缺失，在 Telegram 业务 RPC 前明确返回未调用事实。已返回真实业务结果却缺少所需传输证据时，保留结果和请求身份，不计新合同履约；可能已发生远端操作的项目进入未决核对，禁止自动重放。
8. 新直连成功必须同时具备既有 typed click/rank fact 和完整传输证据。历史事实不补造新证据，也不因新校验被改写为失败。

## 3. API 与页面

- 新建搜索点击/排名观察只创建直连版本。显式要求旧代理或提供正数代理节点时明确报错。
- 排名任务启动、真实豁免候选查询、正式 planner、dispatcher 和 Gateway 全部适配；不能只修改最后的 credentials。原黑账号分组/账号身份、关键词和协议样本条件保留。
- 页面显示“SV 直连出口视角”，当前直连路径不展示历史代理绑定为执行证据。代理并发设置明确标为对直连不适用；不把原每代理并发 2 复制成全账号共享限额。
- 备用授权登录页允许正式直连登录，去掉强制选择代理的提交条件；历史授权代理记录保留为历史记录。
- 传输版本属于执行合同，不要求运营人员手输版本号、owner ID 或技术代次。

## 4. 容量、熔断与未知结果

账号熔断继续按账号隔离；历史 proxy circuit 不参与 direct。direct 的地域/出口身份只用于明确归因，不创建空 key 全局熔断、不新增区域共享限额。将来区域级网络熔断须有跨账号网络证据和独立批准的策略，本轮不实施。

AI 活群覆盖候选入口同样遵守当前直连传输政策：不得根据授权或账号的历史 `proxy_id` 联表排除账号。`RemoteInvocationFence.domain_keys` 记录实际 `transport_mode=direct`、`proxy_id=null` 和账号身份，历史代理编号单独标为历史来源，不能作为当前故障域。账号熔断仍影响该账号的当前可执行性，冻结目标和覆盖义务保持原值。

搜索候选的代理 route 为空时不得将所有候选连为同一个“空代理”资源。直连版本/授权代次作为账号级资源快照，保留实际账号与授权冲突关系。日目标与账号覆盖分母不因可执行供给变化而缩小。

连接中断、owner unknown、旧验证码回调 unknown 以及已有 `unknown_after_send` 保留原执行身份；迁移只关闭经完整证据确认未调用的旧规划项。不得将 worker 退出视为 owner 远端请求结束。

## 5. 历史任务迁移

提供本次范围的 preview/apply 操作：冻结精确任务 ID、旧 type_config/账号范围/目标/截止时间/累计确认数和相关未决动作摘要，以旧值哈希和事务锁执行 CAS，保存操作人、依据、版本前后值与审计。

迁移仅更新传输版本及重新规划所需的确定未调用项目。已完成事实、原冻结 ledger/coverage、已发出或不确定请求、旧 callback fingerprint 均不修改。不按新版本自动重放旧失败，更不追补已过期日期的点击。

本轮迁移服务只更新精确 Task 的 `type_config.transport_contract_version`（排名任务将失效的配置级代理节点置空），不直接清理或改写任何 Action、Attempt、ledger 或 assignment。旧规划载荷保持原版本，经过原正式派发入口时仍明确拒绝旧代理合同；仅既有证据收口链确认未调用后，当前有效周期才可按原规划规则继续。迁移前后冻结目标、状态、时间范围、stats 与全部既有 Action 摘要，记录一致性读回；任何执行中或 unknown 均不因迁移获得重放权限。

发布后先读回旧范围、目标、计数、unknown 身份，再验证新 Action 使用 current/SV/direct 及完整 typed fact。没有运行中的排名任务，只能说明没有旧实例需迁移，不能代替该功能的验收。

## 6. 验收与发布

- PostgreSQL：当前 SV 可用；备用/MY/错误租户拒绝；代次漂移拒绝；创建与修改保留直连版本；旧记录不隐式转换；迁移 CAS 和审计、确认计数及 unknown 保全。
- 网关：owner 上下文、固定出口匹配/不匹配、无环境代理；成功、目标不存在、权限拒绝和远端未知；缺传输证据不假成功；客户端 metadata 复用已有 owner 身份。
- 容量：空 proxy route 不形成共享资源，账号 A 熔断不影响 B，历史 proxy circuit 不影响 direct，网络错误不误标全部账号授权失效。
- 前端：新建与详情直连口径、代理容量不适用、备用登录不再强制代理；类型检查与构建。
- 发布：冻结本地 linux/amd64 制品、正式导入/ID/平台/版本读回，所有 caller 指向唯一 owner。已有 144+3 恢复授权作为保全基线，部署前后核对新增失效。真实搜索点击/排名证据与剩余未恢复账号分别报告。

关联：[授权归属与恢复](account-authorization-owner-and-recovery-20260910-prd.md)、[搜索点击](search-click-boost-prd.md)、[排名观察](search-rank-deboost-hardening-design.md)、[直连切换](account-direct-egress-cutover-20260910-prd.md)、[本地发布](local-direct-production-release-prd.md)。

## 7. Owner 图片识别回调补正（2026-09-11）

`9f709650` 已安装并通过运行、账号保全和唯一 owner 读回；配置迁移前后 14,834 条既有 Action 与受保护状态哈希一致。新动作暴露 `telegram_owner_unsupported_value_type:function`：正式搜索传入图片验证码 solver，原 IPC 不支持函数。当前搜索业务 E4 未通过，不能据发布成功关闭问题。

- 识别函数及其 Provider/runtime 留在 caller 进程；不序列化函数、闭包、代码或数据库 Session。只有 `execute_search_join.image_verification_solver` 这一现有参数支持显式回调引用。
- 在同一条已认证、单次 invocation 的 IPC 上交换带 request/callback/call identity 的识别请求和结果。只传既有 ImageVerificationRequest/Decision/Vote 的封闭数据结构；图片不写 owner journal。owner 仍独占 Telegram 连接与验证码按钮提交。
- caller 在原调用线程执行识别，保持原 deadline、共识、模型和 OCR 策略；既有 RuntimeContract/ConsensusUnavailable 识别异常连同 code、votes 和原 monotonic deadline 原样恢复给 Gateway，保留既有未知 OCR 刷新和业务阻塞语义；其他识别错误按 callback 异常显式失败，不跳过验证码或伪造识别成功。
- 无效引用、身份不匹配及未允许的函数参数明确拒绝；发送前序列化失败必须标记为确认未提交。提交后断线或回调期间异常仍保留原远端不确定性，不据回调错误推断 Telegram 未执行。
- 原 `fact_first_v3` 不使用旧静默窗口/速率字段。迁移后自然产生的新义务按当前合同执行，不为验收修改日目标或复用未知义务。
- 本次短暂停止精确搜索 Task 的新工作，沿正式 pause/resume 入口审计并保留 unknown；发布后恢复原运行状态。其他业务及账号授权不随此问题停用。
- 验证必须覆盖真实跨进程 caller→owner→caller 识别请求与 typed decision、错误/断线、未知引用拒绝、普通 owner RPC 回归；部署后检查新搜索 Action→Attempt→owner→typed click。此前未知动作不自动重放。

# 搜索目标群点击任务（目标机器人 / SOSO 等第三方索引机器人）专项 PRD

## 1. 背景

tg-yunying 当前任务中心已支持 5 类主任务：`group_ai_chat`、`group_relay`、`channel_view`、`channel_like`、`channel_comment`。这些任务主要围绕 Telegram 内的群聊、频道和内容互动展开。

新产品诉求是新增第 6 类主任务：**针对第三方 TG 索引机器人（@searchbot、@soso、@smss 神马、@CJSY 超级索引等）的搜索目标群点击任务**。

业务定位：

- 第三方索引机器人的公开口径显示，排序和收录更偏向名称/内容相关性、持续更新、开放活跃、用户互动、内容质量、是否加入目标机器人生态、付费广告/流量联盟等综合信号；这些公开口径只能作为产品推断，不能写成确定算法。
- 本任务通过“账号矩阵 × 关键词矩阵 × 搜索机器人 × 翻页匹配 × 安全非目标浏览 × 目标群点击 / 确认 × 后续任务联动”的流程，让运营配置的目标群被账号通过搜索行为命中并确认；账号无论此前是否已加入目标群，都必须执行搜索和目标点击 / 确认动作，已在群内时以成员关系观察作为成功事实。
- 本任务是独立任务类型，不借用 AI 活跃群、频道浏览或频道点赞任务语义。

关键限制：

- 目标机器人和 Telegram 平台都可能对脚本化、同步化、异常高频行为触发限制。
- 授权槽位级代理出口、授权槽位级客户端元数据、账号画像、节奏和 decoy 关键词是任务能否灰度运行的硬前置，不是可选增强项；同一 Telegram 账号在不同 TG 开发者应用、不同 session key 和主 / 备用授权槽位下可以绑定不同代理和不同客户端元数据，但每个授权槽位一旦绑定必须长期固定并可审计。
- 首版必须先完成真实样本采集门槛：目标机器人回复结构、button payload、分页行为、失败消息、Clash 节点真实出口 IP 都要有样本证据，不能仅按推测协议开发。
- 本 PRD 中的“iOS / Android 设备指纹”主要用于让搜索、翻页、button callback / URL resolve 等 Telegram 协议动作在 Telegram / 目标机器人链路上呈现为稳定、完整、移动端风格的客户端画像。它指 Telegram MTProto `initConnection` 客户端元数据，不等价于真实原生 iOS / Android 客户端；不得把元数据伪装描述成真机能力。
- 本 PRD 只定义产品、数据流转、执行和验收口径；不把本地设计完成声明为生产可用。

## 2. 目标

- 新增任务类型 `search_join_group`，用户可见名称为“搜索目标群点击任务”，支持通过第三方索引机器人执行关键词搜索、翻页、匹配目标群、button callback / Telegram 内部 URL resolve、加入或已加入确认、停留、后续策略和结果记录。
- 任务中心完整承载搜索目标群点击任务的创建、预检、启动、Action 流水、任务详情、失败事实和运行汇总。
- 风控中心承载代理、客户端元数据、账号环境栈、搜索机器人异常和 search_join 专属告警。
- 账号环境栈采用授权槽位级镜像绑定：代理节点、客户端元数据、API ID 和 session 都按 `account_id + developer_app_id/api_id + authorization_id/session_role` 绑定；Planner 和 Executor 双重硬校验，缺失、复用或观测冲突即跳过或阻断。
- 搜索入群支持类似 AI 活跃群的小时执行量模型：按租户时区自然小时统计 `search_join` 成功数、未来待执行数、过期待执行数和缺口，使用 24 小时曲线、每轮 action 上限和每小时硬上限调度，但统计指标是搜索入群 action，不是发言消息。
- 搜索入群成功后支持和 AI 活跃群、转发监听、后续频道互动等任务联动：先写入加入事实和留存观察，再按任务链接、账号冷却、可发言复检和新成员占比限制，把账号动态追加到后续任务 ready pool。
- 机场订阅全部节点不可用时必须通过已配置的租户 Telegram Bot 向全部管理员 Chat ID 推送告警；Bot 未配置时仍要写告警和审计，不得把通知失败当作可继续执行的理由。
- 首版只允许人工选择 5-10 个已养号账号灰度，不允许默认全量扩到 100+ 账号。
- 调度层和 Executor 层都必须执行真实化节奏，不能只在配置层声明。

## 3. 非目标

- 不做独立的纯排名监测任务；首版排名数据只服务搜索入群任务的效果复盘和运营观察。
- 不自建目标 Bot 或自有索引系统；本任务只对接外部已存在的索引机器人。
- 不替代付费排名，也不承诺目标群一定排到第一。
- 不把搜索入群任务伪装成其他合法任务；任务类型必须独立可见。
- 不通过备用授权槽位扩大同账号并发；主 / 备用授权只用于故障切换和连续性，不作为多个独立账号同时执行。
- 不把 Telethon / MTProto 后端执行描述成真实移动端 UI 渲染或真实手指点击；如果未来要做真机自动化，必须另起专项方案。
- 不把公开资料推断写成第三方真实算法，也不承诺通过单一搜索/加入动作带来排名提升；技术方案只保证灰度、熔断、审计和失败可见。

## 4. 核心概念

### 4.1 搜索目标群点击任务（Search Join Group）

任务定义：

```json
{
  "task_type": "search_join_group",
  "search_bots": ["jisou"],
  "keywords": [
    {"text": "迪拜房产", "region": "AE", "lang": "zh", "decoy": false},
    {"text": "天气预报", "region": "AE", "lang": "zh", "decoy": true}
  ],
  "target_groups": [
    {"operation_target_id": 123, "match_strategy": "username_or_peer_id"}
  ],
  "anti_detection": {
    "warmup_days": 3,
    "behavior_realism": {},
    "rhythm": {},
    "paging": {"max_pages": 70},
    "anti_clustering": {}
  },
  "proxy_policy": {
    "required": true,
    "allowed_proxy_types": ["residential_static", "mobile_4g"],
    "country_match_account_region": false
  }
}
```

每个 action = 一个账号授权槽位 × 一个关键词 × 一个搜索机器人 × 一个目标群匹配策略 = 一次完整搜索目标群点击 / 确认链路。账号已在目标群内时，不跳过该 action；仍需完成搜索、命中目标和目标点击 / 成员确认，成功结果继续记录为 `membership_observed`，并可附带 already joined 事实。

### 4.2 第三方索引机器人

首版目标机器人只接入 `@searchbot`，其余机器人进入第二版适配。

| 机器人 | 代号 | 阶段 | 备注 |
| --- | --- | --- | --- |
| `@searchbot` | `jisou` | 第一版 | 首版协议适配和灰度目标 |
| `@searchbot2bot` | `jisou2bot` | 第二版 | 备用入口 |
| `@soso` / `@SoSoSearchBot` | `soso` | 第二版 | 老牌搜索入口 |
| `@smss` | `smss` | 第二版 | 神马搜索 |
| `@CJSY` / `@So1234Bot` | `cjsy` | 第二版 | 可能包含纯文本 + URL 结果 |

调用方式：向机器人发送关键词文本，机器人返回 inline button 或链接形式的搜索结果。Executor 解析结果、按目标群匹配策略定位结果项，再执行点击、加入和停留。

### 4.3 账号环境栈（Account Environment Stack）

每个执行账号必须绑定：

1. 代理 IP，见 §6。
2. 设备指纹，见 §7。
3. 目标群，任务创建时引用 `OperationTarget`。
4. 搜索机器人，任务创建时选择并写入 `type_config.search_bots`。

环境栈为授权槽位级五元组：`(account_id, developer_app_id/api_id, authorization_id/session_role, proxy_binding_id, client_metadata fields on account_environment_bindings)`。`authorization_id` 引用 `tg_account_authorizations.id`，`session_role` 对应 `primary / standby_1 / standby_2`。主授权和备用授权不能复用同一客户端元数据、同一 `client_identity_key` 或同一代理节点；不同 TG 开发者应用和不同授权槽位可以拥有不同代理和客户端元数据。

任一绑定缺失、冲突或观测不一致，都不得进入主执行。

### 4.4 Action 状态机

**Action 状态保持现有 5 值不变**：`pending`、`executing`、`success`、`failed`、`skipped`。不新增 `needs_warmup`、`needs_proxy` 等状态值。

原因：现有前端筛选、列表渲染、风控判断、审计、Dispatcher 和 Stats 都依赖固定状态集合；新增状态会扩大回归面，并可能让未知状态在前端被误判。

环境前置校验失败由 `action.payload.lifecycle_phase` 和 `action.result.skip_reason` 表达：

|`lifecycle_phase`|含义|落库状态|skip_reason|
|---|---|---|---|
|`main`|主任务阶段，正常执行（默认值）|success / failed|—|
|`warmup`|账号处于 warmup 期，只允许低强度 decoy 搜索|success（仅 decoy）/ skipped|`in_warmup_period`|
|`needs_proxy`|账号未绑代理，action 跳过|skipped|`missing_proxy_binding`|
|`authorization_proxy_conflict`|同账号主/备用授权复用同一代理节点、同一授权槽位存在多个 active 代理，或 observed exit IP 与绑定不一致|skipped|`authorization_proxy_conflict`|
|`needs_client_metadata`|账号未绑客户端元数据，action 跳过|skipped|`missing_client_metadata`|
|`proxy_dead`|授权槽位绑定的代理 IP 健康分 < 60，action 跳过|skipped|`proxy_reputation_below_threshold`|
|`client_metadata_invalid`|客户端元数据异常或与授权资产不一致，action 跳过|skipped|`client_metadata_anomaly`|
|`api_id_client_metadata_mismatch`|授权槽位登录 API ID / session / 运行时 API ID / 客户端元数据组合不一致|skipped|`api_id_client_metadata_mismatch`|
|`proxy_egress_guard_failed`|MTProto 连接未证明走绑定代理或出现直连风险，action 跳过|skipped|`proxy_egress_guard_failed`|
|`proxy_node_unreachable`|当前绑定机场节点不可达，已尝试按策略切换下一个健康节点但本次 action 不继续|skipped|`proxy_node_unreachable`|
|`airport_all_nodes_unavailable`|订阅下所有候选节点均不可达或未通过出口探测，必须停止真实操作|skipped|`airport_all_nodes_unavailable`|

状态机：

```
pending -> claiming -> executing -> success / failed / skipped
                      \-> retry -> pending
```

运营侧的“账号环境未就绪”信息通过 `lifecycle_phase`、`skip_reason`、任务 stats 和风控告警暴露。

### 4.5 运营真实性多层策略

| 防御层 | 触发难度 | 影响权重 | 本 PRD 方案 |
| --- | --- | --- | --- |
| 客户端元数据层 | 低 | 高 | iOS 优先，授权槽位级运行时生成，主/备授权独立镜像绑定 |
| IP 层 | 中 | 极高 | 独享静态住宅或健康 `airport_clash` 节点首版必填，按授权槽位长期绑定唯一固定代理节点 |
| 账号画像层 | 中 | 中 | 注册满 30 天、资料完整、已有自然群组和联系人 |
| 行为层 | 中 | 高 | 真人化随机延迟、翻页回退、停留、入群后留存和转化率控制 |
| 数据层 | 中 | 中 | decoy 关键词、关键词权重、入群转化率和目标排名轨迹可观测 |

### 4.6 关键词

每个关键词结构：

```json
{"text": "迪拜房产", "business_region": "AE", "account_locale": "zh-CN", "proxy_country": "SG", "weight": 1.0, "decoy": false}
```

`decoy=true` 表示非目标关键词，用于行为掩护，不计入目标群加入统计；`decoy=false` 才是真正用于目标群加入的关键词。

`business_region` 表示关键词业务区域，`account_locale` 表示账号语言画像，`proxy_country` 表示代理出口国家。三者不强制相等，只要求组合落入允许矩阵。例如“迪拜房产”可以是中文账号、中文客户端语言、新加坡或阿联酋出口 IP、阿联酋业务区域；不应被旧的 `keyword.region == account.region == ip.country` 硬等式误拦。

### 4.7 执行模式

首版执行模式固定为 `mtproto_userbot`：后端通过 Telethon / MTProto 会话与目标机器人交互，记录发送、等待、解析、callback / URL 跳转、加入、停留和入群后策略的协议事实。文档中“点击”“打开”“浏览”都指可审计的协议动作和等待节奏，不代表真实移动端 UI 渲染。

按钮请求的可见面必须分层理解：

| 按钮类型 | 第三方可见面 | iOS 客户端元数据的作用 | 代理出口的作用 |
| --- | --- | --- | --- |
| `callback_data` inline button | 目标机器人收到 Telegram callback query / bot 响应链路，不直接看到浏览器 UA | 让 Telegram 协议层的用户客户端画像更像稳定移动端，而不是 Telethon 默认 Desktop | Telegram 侧连接出口稳定，降低账号 / session / IP 组合异常 |
| `t.me` / Telegram 内部 URL | Telegram 侧执行 username / invite / peer resolve 和加入动作 | 同上，影响 MTProto 会话画像 | 同上 |
| 外部 HTTP URL | 外部网站看到的是 HTTP 请求 IP、User-Agent、Header、Cookie 等；MTProto `initConnection` 元数据不会自动变成 HTTP UA | 不足以让外部 HTTP 请求看起来像 iOS Safari / Telegram iOS WebView | 必须使用发起该操作的同一授权槽位绑定代理出口；如需真实 HTTP 打开，要另加 mobile webview / browser profile 设计 |

因此，首版“点击按钮正常化”的目标是：Telegram 协议链路上使用稳定的账号、session、API ID、iOS 风格客户端元数据、绑定代理出口和节奏组合；目标机器人可观察到的是 callback / 消息 / 加入行为和 Telegram 传递给机器人的有限上下文，不应假设目标机器人能直接读取 `device_model`、`system_version` 或完整客户端指纹。若某个按钮跳到非 Telegram 外部网站，首版不得默认打开；必须标记为 `external_url_requires_web_profile`，等待单独的 WebView / 浏览器画像设计。

“正常地址”首先指网络出口正常：所有搜索、翻页、callback、Telegram 内部 URL resolve 和 join 请求必须走该授权槽位绑定代理，不能在代理失败时回退本机直连。Executor 每次真实 action 前必须完成 `proxy_egress_guard`：通过授权槽位绑定代理探测 `observed_exit_ip`，确认与 `account_proxy_bindings` / `proxy_exit_ip_observations` 一致；如果代理连接失败、DNS / TCP 直连、出口 IP 漂移或探测不可用，action 必须 `skipped` 并写 `proxy_egress_guard_failed` 或 `authorization_proxy_exit_ip_mismatch`，不得继续点击按钮。

未来如需真实手机 UI 自动化，必须另建 `mobile_device_automation` 专项设计，覆盖真机设备、系统权限、画面识别、触摸事件、设备农场和更高成本验收；不能在本任务中混用两种执行模式。

### 4.8 真实样本采集门槛

开发 executor 前必须先完成 `protocol_sample_collection`：

| 样本 | 最低数量 | 必须记录 |
| --- | --- | --- |
| 目标机器人 `/start` 响应 | 每个首版机器人 ≥ 2 个账号 | 原始消息类型、按钮结构、是否要求验证码 / 入群 / 授权 |
| 关键词搜索响应 | 每个机器人 ≥ 5 个关键词 | 原始 message id、button text、button type、callback_data hash、url、分页按钮、按钮目标类型（callback / Telegram 内部 URL / 外部 HTTP URL）、button effect（navigate_only / join_candidate / external / unknown） |
| 翻页响应 | 每个机器人 ≥ 3 次分页 | 下一页 / 上一页按钮定位、消息更新方式、新旧 message id 关系 |
| 目标群匹配 | 每种匹配策略 ≥ 3 个样本 | username、peer_id、title fuzzy 的成功 / 失败样例 |
| 出口防泄漏样本 | 每种代理实现 ≥ 3 次 | Telethon MTProto 连接前后的 `observed_exit_ip`、国家、ASN、ISP、代理失败时是否 fail closed |
| 异常响应 | 至少覆盖空结果、无目标、限流、验证码或外部 URL 之一 | 原始错误摘要、分类结果、建议动作 |

样本只保存必要结构和 hash，不保存第三方机器人返回的群成员信息、消息正文或其他 PII。未完成样本采集时，`SearchJoinGroupExecutor` 只能实现 parser fixture 和预检，不得进入真实执行灰度。


### 4.9 公开排名规则与产品推导

公开资料只能说明第三方搜索机器人和 Telegram 全局搜索的部分规则，不能当作完整算法。产品设计按“公开规则推断 -> 可观测指标 -> 后续任务联动”处理：

| 公开口径 / 可推断信号 | PRD 中的产品化指标 | 对搜索入群任务的影响 |
| --- | --- | --- |
| 名称、描述和内容主题一致，避免蹭热词和关键词堆叠 | `target_relevance_score`、关键词与目标标题/描述匹配度、目标画像一致性 | 任务创建前提示运营补齐目标资料；不因短期加入量掩盖目标本身不相关 |
| 持续更新、原创/独家内容、减少广告和垃圾信息 | `target_content_health`、最近内容更新、广告/垃圾比例观察 | 搜索入群只负责带入账号；目标内容健康由 AI 活跃群、素材和人工运营共同维护 |
| 用户互动、开放活跃、非禁言群 | `post_join_activation_status`、AI 活跃群发送质量、真实上下文接话质量 | 入群后按冷却规则联动 AI 活跃群，形成持续活跃，而不是只停留在加入成功 |
| 邀请目标机器人加入频道/群组、流量联盟、付费广告/关键词排名 | `jisou_ecosystem_status`、`paid_boost_status`、`ad_keyword_position` | 作为运营配置和效果解释字段；系统不得把自然加入动作冒充付费排名 |
| 刷搜索次数、刷拉新、脚本刷活跃会被处罚或降权 | `suspicious_search_pattern`、`bot_rejection_rate`、`search_join_risk_events` | 不做短时高频、同步化、全点、非目标加入；异常时 fail closed 并告警 |
| Telegram 全局搜索公开信息有限，且会按国家/地区、安全策略和 Premium 用户等因素变化 | `rank_observation_source`、`country_relevance`、`shadowban_or_visibility_status` | 目标排名仅作观察指标；不能作为灰度通过硬条件 |

公开资料来源：极搜官方公告 `https://t.me/s/jisou1?before=187`、极搜广告/关键词排名频道 `https://t.me/s/JSGGZS`、TGInfo 对 Telegram 搜索变化的整理 `https://cn.tginfo.me/search-rules-change-2024`、公开搜索机器人目录 `https://github.com/itgoyo/TelegramGroup`。任何实现前仍需用真实目标机器人样本重新验证。


### 4.10 调研驱动的设计决策

按本轮调研，搜索入群不应被设计成“点击量任务”，而应是“搜索可见性 + 有效加入 + 入群后持续活跃”的组合任务。产品设计固定以下决策：

| 决策 | 设计落点 | 不这样做的风险 |
| --- | --- | --- |
| 目标资料先行 | 创建页必须展示目标群名称、简介、公开 username、关键词匹配度、是否疑似关键词堆叠；`target_relevance_score < threshold` 时只允许保存草稿 | 只堆加入量但目标内容不相关，容易被不收录或降权 |
| 内容健康纳入效果解释 | 运营中心和任务详情展示 `target_content_health`，包括最近更新、广告密度、垃圾信息观察、是否禁言、是否持续活跃 | 排名不上升时无法区分是搜索入群无效、内容弱、群禁言还是广告过多 |
| 极搜生态状态单独记录 | `jisou_ecosystem_status` 记录目标机器人是否已在群/频道、是否加入流量联盟、是否存在付费关键词广告 | 避免把付费广告、流量联盟或自然搜索入群混成一个效果来源 |
| 排名只做观察 | `search_join_rank_observations` 定时记录关键词、机器人、地区、目标位置和样本来源；不作为任务成功硬条件 | 第三方算法变化会导致误判系统失败或误报完成 |
| 入群后必须接运营任务 | 搜索入群成功后进入 `post_join_task_links`，联动 AI 活跃群、监听或评论任务，但先冷却和复检 | 账号加入后沉默，公开口径中的开放活跃、用户互动无法被后续系统承接 |
| 点击/浏览不帮别人加权 | 非目标结果只做 `navigate_only` 安全浏览，且总量默认 ≤ 3，不加入、不关注、不外跳 | 非目标群被误加权，且系统行为像批量刷点击 |
| 反作弊结果可见 | 任何 bot 拒绝、FloodWait、结构变化、账号锁、代理问题都进入 stats 和风控，不隐藏为普通跳过 | 运营无法知道是策略问题、账号问题还是第三方规则变化 |

新增效果归因维度：

```jsonc
{
  "search_visibility_attribution": {
    "organic_search_join": true,
    "linked_ai_activity": true,
    "jisou_ecosystem": "bot_joined|flow_alliance|unknown",
    "paid_keyword_ad": "none|active|expired|unknown",
    "content_health": "healthy|weak|blocked|unknown"
  }
}
```

任务详情必须把“搜索入群动作产量”和“目标搜索可见性变化”分开展示。前者由平台 action 事实证明，后者只是第三方搜索结果观察，不允许把排名上升直接归因为某一次 action。

## 5. 用户故事

1. 运营人员在任务中心创建搜索目标群点击任务，选择 `@searchbot`，导入关键词，选择或粘贴目标群，并配置运营真实性参数。
2. 灰度阶段，运营人员选择 5-10 个已养号 30 天以上的账号，每个账号都绑定独享静态住宅 IP 和镜像设备指纹。
3. 任务启动后，Planner 按账号授权槽位、关键词、搜索机器人和目标群生成 action；Executor 按真人节奏执行完整链路。
4. 加入成功后，系统先写入搜索入群事实、目标成员关系和留存观察，再按配置把账号联动到同目标 AI 活跃群等后续任务的 ready pool。
5. 运营人员在任务详情查看每个账号、关键词、机器人维度的累计入群、目标群平均排名、入群转化率、停留时长、后续任务联动状态和最近失败原因。
6. 当目标机器人突然加验证码、回复结构变化或拒绝请求时，系统暂停相关任务或账号，并在风控中心生成告警。
7. 当某个账号 IP 健康分低于阈值或设备指纹异常时，系统下线该账号的搜索入群任务，并保留可审计的失败事实。
8. 风控中心统一管理账号限流、代理健康、搜索机器人限流和 search_join 专属策略，任务中心只展示执行事实和调度控制。

## 6. IP 池设计

### 6.1 IP 类型分层

按“像真人程度”从高到低：

| 类型 | 来源 | 像真人度 | 成本 | 适用 |
| --- | --- | --- | --- | --- |
| 数据中心代理 | 云服务器机房 | 极低 | 低 | 禁用于本任务 |
| 数据中心静态住宅（伪造） | 机房但 ISP 标为 residential | 中低 | 中 | 不作为首版核心账号 |
| 动态住宅 IP | 真实家庭宽带轮换 | 高 | 中 | 辅助任务 |
| 独享静态住宅 IP | 真实家庭宽带、长期绑定 | 很高 | 高 | 核心账号推荐采购路线；若通过 `airport_clash` 节点达到稳定出口、健康分和容量要求，可作为首版灰度节点来源 |
| 4G / 5G 移动代理 | 真实移动运营商 SIM 卡 | 最高 | 最高 | 第二阶段现金牛账号 |

首版可用代理路径以 `airport_clash` 全局订阅接入为落地实现：系统设置保存一个全局 Clash 订阅，解析节点、观测真实出口 IP 和健康状态，再在“账号面具 > 账号代理”按授权槽位固定绑定。独享静态住宅 IP 是首版灰度推荐的节点质量目标，不要求另做一个住宅代理供应商实现；如果后续直接采购 IPFLY / Bright Data 等供应商，按同一 `ProxyProvider` 抽象扩展。第二阶段再评估 4G / 5G 移动代理。

### 6.2 代理供应商抽象
```python
 class ProxyProvider(Protocol): name: str proxy_type: Literal["residential_static", "residential_rotating", "mobile_4g", "datacenter"] async def list_available(self, country: str, count: int) -> list[ProxyInfo]: ... async def acquire(self, country: str, sticky_minutes: int) -> ProxyInfo: ... async def release(self, proxy_id: str) -> None: ... async def check_reputation(self, ip: str) -> ReputationResult: ... async def health_check(self, proxy_id: str) -> HealthResult: ...
```
首版具体 provider 为 `airport_clash`。IPFLY / Bright Data / ProxyScrape 等直连供应商作为后续扩展，不阻断当前 `search_join_group` 灰度验收。

### 6.3 机场 Clash 自动代理池

“机场”能力作为 `ProxyProvider` 的一种实现：`proxy_provider="airport_clash"`。系统设置提供“Clash 配置”Tab，只保存一个全局 Clash 订阅地址 / 接口配置（加密字段），定时拉取并自动识别订阅格式，把可用节点标准化为代理节点。系统设置不负责授权槽位代理分配；账号的授权槽位代理绑定在“账号面具”一级菜单的“账号代理”Tab 完成，按 `account_id + developer_app_id/api_id + authorization_id/session_role` 选择并固定节点。订阅输入必须同时支持：

- Base64 URI 列表：例如订阅返回体是单行 Base64，解码后每行是 `anytls://`、`trojan://`、`ss://`、`vmess://` 等节点 URI；套餐到期、剩余流量等伪节点必须过滤。
- Clash YAML：包含 `proxies` / `proxy-groups` 的标准 Clash 配置。
- JSON：供应商返回结构化节点列表时按 `nodes/proxies` 等字段解析；字段不匹配时进入 `subscription_schema_invalid`，不得猜测执行。

系统内部统一转换成可供 Clash / Mihomo 使用的节点配置和 `proxy_airport_nodes` 标准节点池；完整订阅 URL、节点密码、token 和 URI 原文都不得进入普通日志、任务 stats 或前端非敏感字段。

核心约束：

1. `clash_subscription_url` 必须加密存储，日志、审计摘要和前端列表只展示脱敏名称，不输出完整 URL。
2. 同一订阅下解析出的节点必须落入 `proxy_airport_nodes`，记录 `node_id`、节点名称、协议、host、port、country、region、asn、isp、健康状态和最近测速结果。
3. 账号授权槽位首次启用搜索入群时，从可用节点中随机选取一个符合 region / ISP / 健康阈值的节点，按 `account_id + developer_app_id/api_id + authorization_id/session_role` 写入 active `account_proxy_bindings`；绑定后长期固定，不随每次 action 轮换。
4. 同一账号的 `primary / standby_1 / standby_2` 必须分配不同节点；同一节点绑定授权槽位数不得超过配置阈值。系统必须支持全局默认容量 `max_authorizations_per_node_default` 和单节点覆盖 `node_capacity_override`，首版建议默认 `1`。
5. Clash 节点的 `proxy_host` 只是入口，不等于真实出口 IP；每次健康检查必须通过外部探测记录 `observed_exit_ip`、出口国家、ASN、ISP 和稳定性。

配置入口与权限：

- 系统设置 / Clash 配置：读取全局订阅脱敏状态需要 `system.view`；保存全局订阅地址、测试连通性、同步节点、展示最近同步时间、节点总数、健康节点数和失败原因需要 `system.manage`。
- 系统设置 / Clash 配置必须拆分保存状态、订阅解析状态、节点同步状态和健康探测状态：保存成功只表示加密订阅地址已更新；`test/sync` 成功只表示已拉取订阅并把 Base64 URI 列表 / Clash YAML / JSON 中的真实节点写入 `proxy_airport_nodes`；节点同步成功且健康节点数大于 0 才能作为授权槽位代理候选来源。保存成功但同步失败、同步成功但健康节点为 0、订阅解析失败、节点全不可用都必须显示可读错误和重试入口，不能把订阅保存成功或节点解析成功当作代理池可用。
- 账号面具 / 账号代理：按 `account_id + developer_app_id/api_id + authorization_id/session_role` 绑定代理节点，展示每个授权槽位的绑定节点、真实出口 IP、健康状态、warmup 状态和最近故障切换；需要 `account_environment.manage`。
- 账号面具 / 账号代理必须把 `primary / standby_1 / standby_2` 和不同 TG 开发者应用分开展示；运营可以按账号、应用、授权槽位、节点、健康分、warmup、故障切换状态筛选。批量重排代理必须先展示影响授权槽位数、预计重新 warmup 数和不可用节点数，确认后写审计。
- 普通日志、任务 stats、前端非敏感字段不得输出订阅完整 URL、节点密码、token 或 URI 原文。
6. 节点健康分低于阈值、订阅失效、节点消失、真实出口 IP 漂移过大或 IP 类型不符时，必须显式暂停对应授权槽位的搜索入群动作并告警，不做静默 fallback。
7. 当前绑定节点连接失败、TCP / TLS 不通、代理认证失败或 `proxy_egress_guard` 无法证明出口时，允许自动执行 `switch_to_next_healthy_node`：只在同一订阅、节点授权槽位容量未超限、出口 IP 可观测的约束下为当前授权槽位选择下一个健康节点，写入 `proxy_node_failover_events`，并让新 `(account_id, developer_app_id/api_id, authorization_id/session_role, proxy_binding_id)` 重新进入 warmup。
8. 自动故障切换不是每次 action 轮换。正常情况下授权槽位长期固定节点；只有明确的 `proxy_node_unreachable`、`proxy_reputation_below_threshold`、`exit_ip_changed`、`node_removed_from_subscription` 事件才能触发换节点。
9. 如果订阅下没有任何候选节点通过连通性、容量和出口 IP 校验，Executor 必须返回 `skipped` + `airport_all_nodes_unavailable`，不发送搜索、不点击按钮、不 join，也不回退本机直连。
10. `airport_all_nodes_unavailable` 必须触发租户 Bot 管理员通知：复用 `Tenant.admin_chat_id` 的多管理员 Chat ID 和已配置的 Bot Token，通过 Telegram Bot `sendMessage` 广播到所有管理员；通知内容只包含任务名、订阅脱敏名、受影响账号/授权槽位数量、最近失败摘要和处理入口，不包含订阅 URL、token、节点密码或关键词明文。

机场订阅与节点模型：

```python
class ProxyAirportSubscription(Base):
    __tablename__ = "proxy_airport_subscriptions"
    id: int
    name: str
    clash_subscription_url_encrypted: str
    provider_label: str | None
    subscription_format: Literal["auto", "base64_uri_list", "clash_yaml", "json"] = "auto"
    max_authorizations_per_node_default: int = 1
    all_nodes_down_policy: Literal["pause_task", "skip_action"] = "pause_task"
    notify_admin_on_all_nodes_down: bool = True
    fetch_interval_minutes: int = 60
    last_fetched_at: datetime | None
    last_fetch_status: Literal["success", "failed", "disabled"]
    last_fetch_error: str | None
    is_active: bool = True

class ProxyAirportNode(Base):
    __tablename__ = "proxy_airport_nodes"
    id: int
    subscription_id: int
    node_id: str
    node_name: str
    protocol: str
    proxy_host: str
    proxy_port: int
    proxy_username: str | None
    proxy_password_encrypted: str | None
    uri_scheme: str | None
    source_format: str
    country: str | None
    region: str | None
    city: str | None
    isp: str | None
    asn: str | None
    latency_ms: int | None
    node_capacity: int
    assigned_authorization_count: int
    failover_rank: int
    consecutive_failures: int
    last_unreachable_at: datetime | None
    observed_exit_ip: str | None
    observed_exit_country: str | None
    observed_exit_asn: str | None
    observed_exit_isp: str | None
    exit_ip_stability_score: float
    health_score: float
    is_active: bool
    last_health_check_at: datetime | None
```

### 6.4 数据库模型
```python
 class AccountProxyBinding(Base): __tablename__ = "account_proxy_bindings" id: int account_id: int developer_app_id: int developer_app_api_id_snapshot: int authorization_id: int session_role: str # primary / standby_1 / standby_2 proxy_id: str # 供应商侧代理 ID 或 proxy_airport_nodes.node_id proxy_provider: str # "ipfly" / "bright_data" / "custom" / "airport_clash" proxy_type: str # residential_static / residential_rotating / mobile_4g proxy_host: str # IP 或域名 proxy_port: int proxy_username: str

None # SOCKS5 鉴权 proxy_password: str|None # 加密存储 proxy_country: str # "US" / "DE" / "SG" / "JP" ... proxy_region: str|None proxy_city: str|None proxy_isp: str|None proxy_asn: str|None bound_at: datetime # 绑定时间 last_used_at: datetime|None last_health_check_at: datetime|None ip_reputation_score: float # IP 健康分 0-100 reputation_check_json: dict # 最近一次信誉检查的详细数据 is_active: bool notes: str|
|
 None created_at: datetime updated_at: datetime
```
 约束：- active 代理绑定唯一键为 `(account_id, developer_app_id/api_id, authorization_id/session_role)` - 同一账号不同 TG 开发者应用、主授权和备用授权槽位可以拥有不同 active 绑定 - 同一授权槽位不允许同时存在多个 active `proxy_host:proxy_port` 或多个 observed exit IP - `ip_reputation_score < 60` 时自动 `is_active=False` 并暂停该授权槽位相关动作 - `last_health_check_at` 距今 > 24h 时定时任务重测 #

### 6.5 账号-IP 绑定策略 **绑定而非轮换**是本任务的核心原则。真人不会每天换 IP 登录账号。- 账号注册时的 IP、养号 IP、任务 IP 要尽量保持连续，实际以授权槽位 `observed_exit_ip` 为风控事实源 - 代理绑定粒度为 `account_id + developer_app_id/api_id + authorization_id/session_role`，同一账号在不同 TG 开发者应用、不同 session key 和主 / 备用授权槽位下可以绑定不同节点，但每个授权槽位必须固定 - `airport_clash` 必须以 `observed_exit_ip` 作为风控事实源，不以节点入口 host 作为出口事实 - 所有 MTProto 连接必须显式走授权槽位绑定代理，代理不可用时 fail closed，不允许回退本机直连 - 当前机场节点完全不通时只能按 `switch_to_next_healthy_node` 为该授权槽位切换到下一个健康节点，且切换后该 `(account_id, developer_app_id/api_id, authorization_id/session_role, proxy_binding_id)` 重新进入 warmup；全订阅节点都不通时必须 `airport_all_nodes_unavailable` 并停止真实操作 - 同一出口 IP 至少稳定 30 天后才允许跑入群任务 - 同一 `/24` 子网最多绑 3 个账号 - 同一 ASN 最多绑 5 个账号
```python
 class ProxyPolicy(BaseModel): required: bool = True allowed_proxy_types: list[Literal["residential_static", "mobile_4g", "airport_clash"]] = ["residential_static", "airport_clash"] proxy_egress_guard_required: bool = True allow_direct_egress_fallback: bool = False enforce_unique_proxy_per_authorization: bool = True country_match_account_region: bool = False # 由关键词允许矩阵控制，不再强制三者硬相等 min_ip_reputation_score: float = 70.0 min_exit_ip_stability_score: float = 80.0 min_binding_age_days: int = 30 max_authorizations_per_node_default: int = 1 node_capacity_overrides: dict[str, int] = {} node_failover_policy: Literal["switch_to_next_healthy_node", "pause_only"] = "switch_to_next_healthy_node" all_nodes_down_policy: Literal["pause_task", "skip_action"] = "pause_task" max_accounts_per_asn: int = 5 max_accounts_per_ip_cidr_24: int = 3 max_daily_requests_per_ip: int = 50 max_weekly_requests_per_ip: int = 200
```
 #

### 6.6 IP 健康度监控 每天 02:00 北京时间定时任务跑 IP 信誉检测：
```python
 async def daily_ip_health_check(): bindings = get_all_active_bindings() for binding in bindings: result = await check_ip_reputation(binding.proxy_host, binding.proxy_country) binding.ip_reputation_score = result.score binding.reputation_check_json = result.to_dict() binding.last_health_check_at = utcnow() if result.score < 60: binding.is_active = False await pause_account_tasks( binding.account_id, reason=f"proxy_dead: ip_reputation_score={result.score}" ) await emit_alert( level="warning", kind="proxy_dead", binding_id=binding.id, account_id=binding.account_id, reason=result.reason, )
```
 信誉检查维度：- 真实出口 IP 探测（HTTP / SOCKS 出口、国家、ASN、ISP） - IPQS（ipqualityscore.com）信誉分 - Spamhaus DNSBL 状态 - IP2Location 类型校验（必须 residential / mobile） - 出口 IP 稳定性（24h 内变更次数、国家 / ASN 漂移） - 自有观察数据（被目标机器人/SOSO 拒绝次数、被 TG 触发 FloodWait 次数）

## 7. 客户端元数据画像设计

### 7.1 设备指纹结构

Telethon 客户端在 `initConnection` 协议中暴露的字段：

| 字段 | Telethon 默认值 | 风险 |
| --- | --- | --- |
| `device_model` | `"Desktop"` | 全网 Telethon 默认值，风险最高 |
| `system_version` | `"Windows 10"` | 全网默认，不像移动端账号 |
| `app_version` | `"4.16.8"` | 全网默认，同质化明显 |
| `lang_code` | `"en"` | 默认英文，不符合中文运营账号画像 |
| `client_id` | Telethon 随机 | 不同 session 可能不稳定 |
| `session_id` | Telethon session 内部值 | 需要和账号授权资产一致追踪 |

还必须校验授权资产层字段：`authorization_id` 对应的 `developer_app_id`、`developer_app_api_id_snapshot`、session 文件和运行时 `api_id/api_hash` 必须一致。客户端元数据不能单独解决 API ID / session 不一致问题；如果授权槽位是用某个开发者应用登录的，Executor 运行时也必须使用同一个开发者应用配置。任一不一致时 action 必须跳过并写 `api_id_client_metadata_mismatch`，不能用“iOS 设备指纹”掩盖授权资产不一致。

所有 Telethon 默认实例共享同一套客户端元数据，容易形成“脚本客户端”标签。本任务必须按授权槽位镜像绑定移动端风格的 MTProto 客户端元数据。主授权和备用授权都必须有完整元数据字段；默认优先使用 iOS 风格元数据，Android 只作为少量多样性补充。

本章的产品目的不是“做真机自动化”，而是让同一授权槽位发出的搜索、翻页、button callback、Telegram 内部 URL resolve 和 join 请求，在 Telegram 协议层表现为长期稳定的移动端客户端画像。第三方索引机器人如果基于 Telegram callback / 会话行为判断来源，也会看到更接近正常移动端账号的动作组合，而不是大量默认 Desktop Telethon 客户端。

重要边界：这些字段只影响 `initConnection` 里上报的客户端元数据，不等价于真实 iPhone / Android 设备，不证明系统拥有原生移动端 UI、推送 token、触摸行为或 iOS WebView。风控判断必须同时结合 API ID、session 来源、代理出口、请求节奏和失败事实。外部 HTTP URL 的正常化不由 MTProto 指纹单独解决，必须另行设计 HTTP User-Agent、WebView / 浏览器 profile、Cookie 和出口 IP 绑定。

### 7.2 客户端元数据规则集（不预设固定池） 客户端元数据**没有独立表**——运行时按规则集随机生成，写入 `AccountEnvironmentBinding` 表（见 §7.4.1）。规则集字段如下，存放在配置或代码常量中（推荐代码常量，方便 review）：
```python
 # backend/app/services/device_fingerprint/rules.py # 平台分布权重（硬约束：iOS 80% / Android 20%） PLATFORM_WEIGHTS = { "ios": 0.80, "android": 0.20, } # iOS 型号池（按市场真实份额加权） IOS_MODEL_POOL = [ {"device_model": "iPhone 15 Pro Max", "weight": 0.18}, {"device_model": "iPhone 15 Pro", "weight": 0.22}, {"device_model": "iPhone 15 Plus", "weight": 0.08}, {"device_model": "iPhone 15", "weight": 0.12}, {"device_model": "iPhone 14 Pro Max", "weight": 0.10}, {"device_model": "iPhone 14 Pro", "weight": 0.10}, {"device_model": "iPhone 14", "weight": 0.06}, {"device_model": "iPhone 13 Pro", "weight": 0.06}, {"device_model": "iPhone 13", "weight": 0.05}, {"device_model": "iPhone SE 3rd gen", "weight": 0.03}, ] # Android 型号池 ANDROID_MODEL_POOL = [ {"device_model": "Samsung SM-S908B", "weight": 0.18}, {"device_model": "Samsung SM-S921B", "weight": 0.10}, {"device_model": "Samsung SM-A546B", "weight": 0.08}, {"device_model": "Pixel 8 Pro", "weight": 0.12}, {"device_model": "Pixel 8", "weight": 0.10}, {"device_model": "Pixel 7 Pro", "weight": 0.06}, {"device_model": "Xiaomi 14", "weight": 0.10}, {"device_model": "Xiaomi 13", "weight": 0.06}, {"device_model": "Redmi Note 12", "weight": 0.04}, {"device_model": "OnePlus 11", "weight": 0.06}, {"device_model": "OnePlus 10 Pro", "weight": 0.04}, {"device_model": "Huawei P50 Pro", "weight": 0.03}, {"device_model": "Huawei Mate 50 Pro", "weight": 0.03}, ] # 系统版本池（按型号分组，取自真实发布历史） IOS_VERSION_POOL = { "iPhone 15 Pro Max": ["iOS 17.5.1", "iOS 17.4", "iOS 17.3"], "iPhone 15 Pro": ["iOS 17.5.1", "iOS 17.4", "iOS 17.3"], "iPhone 14 Pro": ["iOS 17.5.1", "iOS 17.4", "iOS 16.6.1"], "iPhone 13": ["iOS 16.6.1", "iOS 16.5", "iOS 15.7"], # ... } ANDROID_VERSION_POOL = { "Samsung SM-S908B": ["Android 14", "Android 13"], "Pixel 8 Pro": ["Android 14", "Android 14 QPR3"], "Xiaomi 14": ["Android 14", "HyperOS 1.0"], # ... } # TG app_version（从 TG 官方 GitHub release tag 取真实版本，按平台分组） TG_APP_VERSION_POOL = { "ios": ["10.6.2", "10.6.1", "10.5.2"], "android": ["10.6.2", "10.6.1", "10.5.2"], } # 区域 → lang_code 映射 REGION_LANG_MAP = { "CN": ("zh-hans", "zh-hans", "android" if platform=="android" else "ios"), "HK": ("zh-hant", "zh-hant", "ios"), "TW": ("zh-hant", "zh-hant", "ios"), "US": ("en", "en", "ios"), "JP": ("ja", "ja", "ios"), "KR": ("ko", "ko", "ios"), "DE": ("de", "de", "ios"), # ... } # 完整指纹必填字段 REQUIRED_FINGERPRINT_FIELDS = ["platform", "device_model", "system_version", "app_version", "lang_code", "system_lang_code", "lang_pack", "region_code", "client_identity_key"] # 同组合上限 COMBO_LIMIT_PER_AUTHORIZATION = 1 # 同账号内主/备授权不得复用同一组合 COMBO_LIMIT_PER_ACCOUNT = 3 # 同 (model + version + app_version) 组合最多 3 个账号 DEVICE_MODEL_LIMIT = 10 # 同一 device_model 不限版本最多 10 个账号 APP_VERSION_LIMIT = 30 # 同一 app_version 最多 30 个账号
```
 运行时生成算法见 §7.3.2，绑定规则见 §7.4.2。#

### 7.3 客户端元数据池要求 **核心原则：客户端元数据是授权槽位身份的一部分，主授权和备用授权都需要独立镜像绑定。** 元数据池是"运行时随机生成"，**不预定义固定池**。授权槽位首次创建任务时，从规则集按权重随机抽取一个移动端型号风格 + 系统版本 + TG app_version 组合，**永久绑定到该授权槽位**。##

#### 7.3.1 平台分布（硬约束）

|平台|占比|备注|
|---|---|---|
|**iOS**|**80%**（主力）|多数目标用户使用 iPhone 客户端，iOS 客户端 bot 检测相对 Android 更严格但更难被脚本伪造|
|Android|20%|保留部分多样性|
|TDesktop|0%|**本任务不适用**|
|
 未来如需调整比例，必须通过 `device_fingerprint_rule_set` 表的 pool_priority 字段调权重，不直接改代码。##

#### 7.3.2 型号池（运行时随机，不预设固定组合） **型号池是元数据，运行时按权重随机抽取组合**：- **iOS 型号池**（80% 权重）：- iPhone 15 Pro / iPhone 15 Pro Max / iPhone 15 / iPhone 15 Plus - iPhone 14 Pro / iPhone 14 Pro Max / iPhone 14 / iPhone 14 Plus - iPhone 13 Pro / iPhone 13 Pro Max / iPhone 13 / iPhone 13 mini - iPhone SE 3rd gen - 每个型号的 pool_priority 反映市场真实份额（如 iPhone 13 占比 > iPhone 15 Pro Max） - **Android 型号池**（20% 权重）：- Samsung SM-S908B / SM-S921B / SM-A546B - Pixel 8 Pro / Pixel 8 / Pixel 7 Pro - Xiaomi 14 / Xiaomi 13 / Redmi Note 12 - OnePlus 11 / OnePlus 10 Pro - Huawei P50 Pro / Mate 50 Pro - **iOS 系统版本池**：从 Apple 官方 iOS 发布历史取真实版本号（iOS 17.5.1 / 17.4 / 16.6.1 / 16.5 / 15.7 等），不编 - **Android 系统版本池**：从 AOSP / OEM 真实发布版本取（Android 14 / 13 / 12 等） - **TG app_version 池**：从 TG 官方 GitHub release tag 取真实版本（tdesktop 4.x、telegram-ios 10.x、telegram-android 10.x 等），不编 **运行时生成算法**（简化）：
```python
 def generate_fingerprint(region_code: str) -> DeviceFingerprint: platform = weighted_choice({"ios": 0.8, "android": 0.2}) if platform == "ios": device_model = weighted_choice(IOS_MODEL_POOL) system_version = weighted_choice(IOS_VERSION_POOL[device_model]) else: device_model = weighted_choice(ANDROID_MODEL_POOL) system_version = weighted_choice(ANDROID_VERSION_POOL[device_model]) app_version = weighted_choice(TG_APP_VERSION_POOL[platform]) lang_code, system_lang_code = pick_lang_for_region(region_code) lang_pack = "ios" if platform == "ios" else "android" return DeviceFingerprint( device_model=device_model, system_version=system_version, app_version=app_version, platform=platform, lang_code=lang_code, system_lang_code=system_lang_code, lang_pack=lang_pack, region_code=region_code, )
```
 ##

#### 7.3.3 区域与语言一致性

|region_code|平台|lang_code|system_lang_code|lang_pack|
|---|---|---|---|---|
|CN|iOS / Android|`zh-hans`|`zh-hans`|ios / android|
|HK / TW|iOS / Android|`zh-hant`|`zh-hant`|ios / android|
|US|iOS / Android|`en`|`en`|ios / android|
|JP|iOS / Android|`ja`|`ja`|ios / android|
|KR|iOS / Android|`ko`|`ko`|ios / android|
|DE|iOS / Android|`de`|`de`|ios / android|
|
 **一致性校验**：账号 region_code、设备语言和代理出口国家必须进入同一套关键词允许矩阵和风险评分。默认不要求三者硬相等；当矩阵明确不允许、出口 IP 与任务区域冲突、或 `country_match_account_region=true` 的任务显式要求强一致时，Executor 才拒绝执行并写入 `region_proxy_language_mismatch`。##

#### 7.3.4 同组合上限（避免同质化）

|组合|同质化阈值|说明|
|---|---|---|
|同一 `device_model + system_version + app_version` 组合|≤ 3 个账号|iPhone 这种用户基数大，3-5 个仍 OK，但 8+ 明显不真实|
|同一 `device_model`（不限版本）|≤ 10 个账号|同一型号可多个版本|
|同一 `app_version`|≤ 30 个账号|同期 TG 版本可能 30% 用户|
|
 新账号抽签时如果目标组合超限，自动换下一组；连续 5 次都超限则报"fingerprint pool exhausted, add more variants"，由运营决定是否扩池。#

### 7.4 授权槽位-客户端元数据绑定（镜像绑定） **客户端元数据与授权槽位"镜像绑定"——主授权、备用授权都是独立客户端身份。** 一旦绑定，整个生命周期都不切换。##

#### 7.4.1 数据模型（合并指纹字段到 binding 表）
```python
 class AccountEnvironmentBinding(Base): """授权槽位环境绑定（代理 + 设备指纹 = 授权槽位客户端身份镜像）""" __tablename__ = "account_environment_bindings" id: int account_id: int developer_app_id: int developer_app_api_id_snapshot: int authorization_id: int session_role: str # primary / standby_1 / standby_2 proxy_binding_id: int # 引用该授权槽位 active 代理绑定 # ↓ 设备指纹字段直接持久化到 binding 表（不依赖外键） device_model: str # "iPhone 15 Pro" system_version: str # "iOS 17.5.1" app_version: str # "10.6.2" platform: str # "ios" / "android" lang_code: str # "zh-hans" system_lang_code: str # "zh-hans" lang_pack: str # "ios" region_code: str # "CN" / "US" / "JP" ... client_identity_key: str # account_id + developer_app_id + authorization_id + platform + model + version hash，用于去重和审计 # 镜像冻结标记：true 后任何代码都不能更换 device_* 字段 fingerprint_locked: bool = True # 区域一致性校验 region_consistency_checked: bool region_consistency_errors: list[str]

None bound_at: datetime # 首次绑定时间（永久不变） last_used_at: datetime|None health_score: float notes: str|
|
 None
```
 ##

#### 7.4.2 镜像绑定规则 1. **首次绑定时机**：账号授权槽位首次创建任务时（手动触发或导入触发），按 §7.3 运行时算法生成一个指纹组合，并按 §6 绑定代理节点，写入 binding 表。`fingerprint_locked=true`。2. **主/备客户端和代理都独立**：同一账号的 `primary / standby_1 / standby_2` 都必须绑定不同 `client_identity_key`、不同 `device_model + system_version + app_version` 组合和不同代理节点；不能为了省资源让备用 session 复用主账号指纹或代理出口。3. **完整指纹字段必填**：`platform/device_model/system_version/app_version/lang_code/system_lang_code/lang_pack/region_code/client_identity_key` 全部必填，缺任一字段即 `fingerprint_invalid`。4. **永久不变**：绑定的 device_* 字段在授权槽位生命周期内不切换：- 跨任务（该授权槽位同时跑 search_join_group 和其他任务）→ **同一指纹** - 授权槽位换代理（IP 健康分 < 60 换新 IP）→ **同一指纹**，仅代理绑定代际变化 - 跨租户（理论上不应该，但万一）→ **同一指纹** - **会话重连 / session 恢复** → **同一指纹**（Telethon session 文件不含 device_fingerprint，但 initConnection 时每次都重新发送） 5. **解绑需要运营手动操作**：必须通过风控中心的 `unbind_environment` 接口，且写入审计日志。**调度层和 Executor 都不允许自动解绑**。6. **同组合查重**：调度层在绑定前查 `account_environment_bindings` 表，按 (device_model + system_version + app_version) 组合查询现有账号数，超过 §7.3.4 阈值则换组合。##

#### 7.4.3 调度层和 Executor 双重硬校验
```python
 def assert_environment_ready(account_id: int, authorization_id: int) -> AccountEnvironmentBinding: binding = get_binding_by_authorization(account_id, authorization_id) if not binding: raise NoEnvironmentBindingError(account_id, authorization_id) if not binding.region_consistency_checked: raise EnvironmentInconsistencyError(account_id, authorization_id) if binding.health_score < 60: raise EnvironmentUnhealthyError(account_id, authorization_id, binding.health_score) if binding.fingerprint_locked is False: raise FingerprintNotLockedError(account_id, authorization_id) # 异常：未锁定的指纹应不存在 if missing_required_fingerprint_fields(binding): raise FingerprintIncompleteError(account_id, authorization_id) return binding
```
 ##

#### 7.4.4 镜像绑定的语义价值 - **画像连续性**：TG 服务端 / 目标机器人 / SOSO 看到的是"同一个账号 + 稳定授权槽位代理出口 + 稳定授权槽位客户端元数据"长期一致的画像，不会某个授权槽位在不同 action 间反复换 IP，也不会突然从 iPhone 13 Pro 跳到 Xiaomi Mi 11。- **主备切换可解释**：切换到备用授权时，系统看到的是同账号下另一个稳定代理出口和另一个稳定客户端元数据组合，而不是同一组合被多个 session 复用。- **行为可追溯**：审计日志能完整看到"该授权槽位代理出口、授权槽位客户端元数据和任务历史"。- **横向防御**：即使目标机器人拿到一份账号列表，客户端元数据维度也是稳定的画像信号，不会因任务变化而被标记。

#### 7.4.5 配置入口、应用粒度和生效边界

授权指纹配置入口位于“账号面具”一级菜单的“授权指纹”Tab，不放在系统设置，也不混入面具编辑表单。系统必须把“账号面具”拆成面具管理、账号代理、授权指纹、异常与审计四个可理解区域，避免运营把“人设表达”和“授权环境”混成一个字段。

授权指纹绑定粒度为：

```text
account_id + developer_app_id/api_id + authorization_id/session_role
```

- `developer_app_id/api_id` 指系统里的 TG 开发者应用 `api_id/api_hash`。
- 同一账号在不同 TG 开发者应用下可以绑定不同客户端元数据和不同代理节点。
- 同一账号的 `primary / standby_1 / standby_2` 必须拥有不同 `client_identity_key`、不同 `device_model + system_version + app_version` 组合和不同代理节点。
- Executor 必须使用授权槽位登录时绑定的同一 TG 开发者应用和客户端元数据，并显式走该账号的唯一 active 代理绑定；不能用另一个应用下的指纹配置替代，不能为某个 session 单独换代理，也不能回退本机直连。

修改授权指纹配置只影响下一次使用该授权槽位建立连接、重登或新 session 初始化时上报的 MTProto 客户端元数据。保存配置成功只能表示“配置指纹已更新”，不能声明 Telegram 远端授权设备型号已经立即变更。远端实际显示必须通过 `tg_account_authorization_snapshots` 读取后作为“远端观测指纹”展示。

界面必须同时展示：

- 配置指纹：运营在后台设置的目标 `platform/device_model/system_version/app_version/lang_* / region_code / client_identity_key`。
- 远端观测指纹：从 Telegram 授权设备列表读取到的 `device_model/platform/system_version/app_name/app_version/api_id`。
- 一致性状态：`not_connected`、`pending_effect`、`observed_matched`、`observed_mismatch`、`unobservable`。

`observed_mismatch` 只能提示重登 / 刷新授权 / 人工检查，不能自动改写现有 session，也不能把配置保存包装成远端已变更。`unobservable` 表示 Telegram 授权设备快照没有返回足够字段用于比对，必须展示缺失字段；它既不能算匹配，也不能算配置失败。

生命周期口径：

- 新建授权槽位：首次连接前写入配置指纹；连接成功后通过授权设备快照刷新远端观测指纹。
- 修改已有授权槽位配置：保存后状态为 `pending_effect`，现有 Telegram 远端授权设备不会被立即改名或改型号；只有下一次该授权槽位重登、新 session 初始化或重新建立会触发 `initConnection` 的连接时，才可能被 Telegram 记录为新的客户端元数据。
- 刷新远端观测：只读取 Telegram 授权设备列表并更新观测字段，不改配置指纹，不自动重登。
- 批量更新：只批量写配置和审计，结果必须按授权槽位返回 `configured / pending_effect / observed_matched / observed_mismatch / unobservable / failed`；不得返回“远端设备已批量更新成功”。
- 手动重登 / 新 session：属于账号授权资产操作，必须走账号安全 / 授权资产流程和对应审计，不能由“保存指纹”按钮隐式触发。

### 7.5 账号授权槽位执行互斥

主 / 备用授权槽位只用于可用性和故障切换，不用于扩大同一账号并发。Planner 和 Dispatcher 必须持有账号级执行互斥锁：

1. 同一 `account_id` 任意时刻最多只有 1 个 `search_join` action 处于 `claiming/executing`。
2. 备用授权只有在主授权不可用、健康分低于阈值、人工切换或故障切换事件存在时才可执行。
3. 同账号从 primary 切到 standby 后，必须切到该 standby 自己绑定的代理节点和客户端元数据，记录 `authorization_switch_reason`。
4. 同账号不同授权槽位不得并行跑同一关键词或不同关键词；并发扩量只能通过不同账号实现。

违反互斥锁时，Planner 不创建 action；Dispatcher 领取时再次校验，失败写 `skip_reason=account_authorization_lock_conflict`。

## 8. 运营真实性设计

### 8.1 配置 Schema
```jsonc
 { "anti_detection": { "warmup_days": 3, "warmup_daily_actions": 3, "behavior_realism": { "decision_delay_seconds": [3, 8], "browse_other_results_before_join": [0, 2], "browse_other_results_after_join": [0, 1], "max_non_target_safe_navigation_per_action": 3, "pre_join_decoy_click_probability": 0.35, "pre_join_decoy_click_count": [0, 2], "pre_join_decoy_dwell_seconds": [10, 30], "post_join_safe_browse_probability": 0.25, "post_join_safe_browse_count": [0, 1], "post_join_safe_browse_dwell_seconds": [8, 20], "decoy_join_enabled": false, "post_join_policy": "stay_joined", "post_join_retention_days": [3, 14], "in_group_dwell_seconds": [30, 180], "post_join_linked_task_policy": { "enabled": true, "activation_delay_minutes": [60, 360], "min_retention_before_ai_minutes": 360, "max_new_joined_accounts_per_hour_ratio": 0.2 }, "exit_dwell_seconds": [5, 15], "occasional_message_probability": 0.0, "decoy_keyword_ratio": 0.5 }, "rhythm": { "action_interval_seconds": [300, 1800], "interval_distribution": "normal", "interval_std_dev_ratio": 0.4, "active_hours": ["08:00-23:00"], "task_start_jitter_seconds": [0, 1800] }, "paging": { "max_pages": 70, "scroll_back_probability": 0.3, "scroll_back_max_times": 2, "non_target_browse_probability": 0.2 }, "anti_clustering": { "max_accounts_per_ip_cidr_24": 3, "max_accounts_per_asn": 5, "max_daily_actions_per_account": 5, "max_daily_searches_per_keyword_per_account": 2, "max_concurrent_accounts_per_keyword": 10 } }, "proxy_airport_policy": { "subscription_format": "auto", "supported_formats": ["base64_uri_list", "clash_yaml", "json"], "max_authorizations_per_node_default": 1, "node_capacity_overrides": {"香港 01": 1, "日本 01": 2}, "enforce_unique_proxy_per_authorization": true, "node_failover_policy": "switch_to_next_healthy_node", "all_nodes_down_policy": "pause_task", "filter_non_node_entries": true, "allow_direct_egress_fallback": false } }
```
 #

### 8.2 Warmup 阶段

**Warmup 维度：`(account_id, developer_app_id/api_id, authorization_id/session_role, proxy_binding_id)` 五元组。** 每个 (账号, TG 开发者应用, 授权槽位, 代理) 对独立计算 warmup 进度。授权槽位换 IP / 节点时，新 (账号, 应用, 授权槽位, 新 IP) 对从 warmup 第 1 天重新开始。

新建任务或新 (账号, IP) 对上线后，前 N 天只允许低强度行为：

| 阶段 | 时长（自该账号-IP 对首次 action 起算） | 每天 action 数上限 | 关键词类型 |
| --- | --- | --- | --- |
| `warmup` | 1-3 天 | 3 | 全 decoy |
| `low` | 4-14 天 | 5 | decoy 推荐占比 50%，硬阈值仍为 30% |
| `steady` | 15 天后 | 按任务和风控策略 | decoy 硬阈值 30%，推荐 50% |
 数据模型：
```python
 class AccountProxyWarmupState(Base): """(账号, 应用, 授权槽位, 代理) 五元组 warmup 进度""" __tablename__ = "account_proxy_warmup_states" id: int account_id: int developer_app_id: int developer_app_api_id_snapshot: int authorization_id: int session_role: str proxy_binding_id: int stage: Literal["warmup", "low", "steady"] stage_started_at: datetime first_action_at: datetime

None # 该 (账号, 应用, 授权槽位, IP) 对首次 action 时间 daily_actions_count: int # 当日已执行 action 数 daily_actions_reset_at: datetime # 每日 00:00 重置 total_actions: int # 累计 action 数 reset_at: datetime|None # 重新 warmup 时记录（换 IP 时写入） reset_reason: str|
|
 None UNIQUE (account_id, developer_app_id, authorization_id, session_role, proxy_binding_id)
```
 推进规则：- 每日 00:00 重置 `daily_actions_count` - 阶段切换：`total_actions` 达到该阶段上限天数后，scheduler 在下一次 planner tick 自动切换 stage - 换 IP / 节点：`reset_at` 写入换 IP / 节点时间，`stage` 重置为 `warmup`，`first_action_at` 重新计算 运营可见：任务详情 / 风控中心 / 账号列表均显示当前 (账号, 应用, 授权槽位, IP) 的 warmup 阶段和进度条。#

### 8.3 Action 执行链路（行为真实化） 每个 action 必须按以下链路执行，不允许跳过任何步骤：

1. 准备阶段：选择搜索机器人和关键词，通过 env.stack 读取 proxy + client_metadata，校验机场节点健康、账号级执行互斥锁、warmup 和 `proxy_egress_guard`。
2. 搜索阶段：向目标机器人发送关键词，等待包含 inline button 或 link 的搜索结果；FloodWait、超时和结构变化必须显式记录。
3. 入群前安全浏览阶段：决策延迟后，按概率打开 0-2 个非目标结果；只允许 `button_effect=navigate_only`，停留 10-30 秒后返回；不得加入、关注、外跳或点击 `join_candidate/external/unknown`。
4. 匹配阶段：解析搜索结果的 button / link，按 username、peer_id 或标题模糊匹配目标群；默认最多翻 70 页，找满 70 页仍未匹配写 `target_not_in_results`、`pages_exhausted=true`，并自动停止该搜索目标群点击任务。
5. 加入阶段：通过 MTProto callback 或 Telegram 内部 URL resolve 执行目标群 join；失败必须分类为验证码、审批、链接失效、权限不足或目标机器人拒绝。
6. 目标群停留阶段：在目标群停留 30-180 秒，只执行低风险 read / history / read_ack；首版默认不发言。
7. 入群后安全浏览阶段：加入成功后允许按概率返回搜索结果页或翻页再浏览 0-1 个 `navigate_only` 结果；全链路非目标安全浏览总数不得超过 `max_non_target_safe_navigation_per_action=3`，且仍不得加入非目标群 / 频道。
8. 入群后策略：默认 `post_join_policy=stay_joined`，不立即退出；任何 delayed leave / leave after dwell 必须有独立清理任务、审批原因和留存结果。
9. 后续任务联动：写入加入事实和留存观察后，按 §8.9 把账号投递给同目标 AI 活跃群等任务的 ready pool，但必须经过冷却、可发言复检和新成员占比限制。
10. 记录阶段：写 action result，至少记录目标位置、total_results、dwell、pre/post safe navigation、post_join_policy、proxy_failover_event_id、linked task dispatch 状态和健康累计。

### 8.3.1 非目标安全浏览边界

非目标浏览的目的只是避免所有账号在搜索结果中机械地直奔同一个目标，不是给其他结果制造加入量或互动量。硬规则：

- 入群前 `pre_join_decoy_click_count` 默认 0-2，入群后 `post_join_safe_browse_count` 默认 0-1。
- 单 action `pre_join + post_join` 非目标安全浏览总数默认不超过 3；不得配置为“全点前几个结果”。
- 只允许点击协议样本已确认的 `navigate_only`；`join_candidate`、`external_http_url`、`unknown`、会触发加入/关注/投票/发言/外链打开的按钮一律跳过。
- `decoy_join_enabled=false` 是首版硬默认值；如果未来允许加入非目标群，必须另起风控审批和验收，不得复用本 PRD 默认链路。
- 每次安全浏览都写入 `pre_join_decoy_clicks` 或 `post_join_safe_navigation`，包含 button hash、position、effect、dwell、joined=false。

### 8.4 Decoy 关键词机制

任务创建硬约束：`decoy=true` 关键词占比必须 ≥ 30%。运营推荐值：灰度期保持 50% 左右。

decoy 关键词是非目标关键词（如“天气预报”“NBA 比分”“美食推荐”），用于：

- 稀释目标关键词搜索频率，避免大量账号只搜索同一目标词。
- 让账号搜索历史看起来更接近真人。
- 当 decoy 关键词出现搜索结果时，也按真人行为浏览，但不计入目标群加入统计。

decoy 关键词必须人工或 AI 生成，不能简单从目标关键词同批衍生。

### 8.5 小时执行量模型

搜索目标群点击任务复用 AI 活跃群“自然小时桶 + 24 小时曲线 + 当前小时补量 + 过期 action 不计入覆盖”的调度思想，但不复用 AI 活跃群的发言语义、AI 生成链路或 `send_message` 统计。

业务小时目标字段放入 `type_config`，仅 search_join 专属节奏覆盖字段放入 `pacing_config`。运行时合并必须忽略 `pacing_config` 中的 `null`，不能把空覆盖值当作 0；只有运营显式提交 `max_actions_per_hour=0` 时才表示该小时容量关闭。

```jsonc
{
  "hourly_round_curve": [0, 0, 0, 0, 0, 0, 1, 1, 2, 2, 2, 2, 1, 1, 2, 2, 3, 3, 2, 2, 1, 1, 0, 0],
  "actions_per_round_mode": "auto",
  "actions_per_round": 5,
  "max_actions_per_hour": 20,
  "hourly_min_successful_joins": 0,
  "hard_hourly_strategy": "force_planning_when_enabled"
}
```

字段含义：

| 字段 | 含义 |
| --- | --- |
| `hourly_round_curve` | 24 个整数，按租户时区解释；表示该小时最多启动多少个 search_join 规划轮次，`0` 表示该小时不主动开新轮 |
| `actions_per_round_mode` | `auto / manual`；auto 时由后端按账号容量、warmup 阶段、关键词权重和小时剩余额度推荐本轮 action 数 |
| `actions_per_round` | manual 模式下每轮最多创建多少个 `search_join` action；实际创建仍受账号、代理、关键词、目标和风控限制 |
| `max_actions_per_hour` | 任务级每小时硬上限；任何补量、重试或 decoy 都不得突破 |
| `hourly_min_successful_joins` | 可选硬目标；`0` 表示只按曲线和上限自然执行；大于 0 时表示每个自然小时最低成功 `search_join` 数 |
| `hard_hourly_strategy` | 首版固定 `force_planning_when_enabled`；只在 `hourly_min_successful_joins > 0` 时追缺口 |

当前小时统计口径：

```text
success_current_hour =
  count(action_type = search_join, status = success, executed_at in [hour_start, hour_end))

future_open_current_hour =
  count(status in pending/claiming/executing, scheduled_at in [now, hour_end))

overdue_open_count =
  count(status in pending/claiming/executing, scheduled_at < now)

deficit =
  max(hourly_min_successful_joins - success_current_hour - future_open_current_hour, 0)
```

不计入成功：

- `skipped`、`failed`、`unknown_after_send` 或执行结果未知。
- `proxy_node_unreachable` 后已切换但本轮跳过的 action。
- `airport_all_nodes_unavailable`、`proxy_egress_guard_failed`、`external_url_requires_web_profile`。
- 只浏览 decoy 且未完成目标加入的动作。

Planner 规则：

1. `next_run_at` 按 `hourly_round_curve[current_hour]` 推导；例如当前小时 6 轮，则理论轮间隔约 10 分钟，并叠加任务级 jitter。
2. 每次 Planner 只规划一个 search_join 轮次；本轮计划数按 `min(actions_per_round, max_actions_per_hour_remaining, account_capacity_remaining, proxy_capacity_remaining, keyword_limit_remaining)` 计算。
3. `hourly_round_curve[current_hour]=0` 时不主动开新轮；下一次运行时间跳到下一个非 0 小时。
4. `hourly_min_successful_joins > 0` 且 `deficit > 0` 时可以压缩本小时剩余窗口内的规划间隔，但仍不得绕过 warmup、账号锁、代理 egress guard、节点容量、关键词日上限和 Bot 协议样本门槛。
5. 已过期的 open action 不计入未来覆盖，必须进入 `overdue_open_count` 和 `dispatcher_lag / worker_backlog` 诊断。
6. 全订阅节点不可用时，action 级结果为 `skipped` + `airport_all_nodes_unavailable`，小时 stats 级状态为 `blocked`，任务主状态按 `all_nodes_down_policy` 进入 `paused` 或保持 running 但不再补量；补量恢复必须等代理节点重新健康后重排未来 action。

任务详情展示：

- 当前小时窗口、计划轮数、已启动轮次、每轮计划 action 数、每小时硬上限。
- 当前小时成功、未来待执行、过期待执行、缺口、最近补量时间。
- 本小时阻塞原因分布：代理全不可用、节点切换、账号锁、warmup、目标未出现、目标机器人限流等。
- 与 AI 活跃群类似显示 `catching_up / met / blocked / missed / disabled`，但文案必须写“搜索入群小时执行”，不能写“发言硬目标”。

#### 8.5.1 搜索节奏与账号上限（仅 `search_join_group` 生效）

任务创建、编辑页必须提供“节奏与账号上限”高级设置。本组字段只对“搜索目标群点击任务”生效，不得影响 AI 活跃群、频道浏览、点赞、评论或其他任务类型。

产品展示按“账号级 / 任务级 / 跳过与抖动”分组；落库仍放入 `pacing_config`，便于复用任务中心现有配置存储。

| 字段 | 默认值 | 含义 |
| --- | --- | --- |
| `per_account_total_action_limit` | `0` | 单账号在本任务生命周期内最多执行多少次真实搜索目标群点击操作；`0` 表示不设总上限 |
| `per_account_daily_action_limit` | `1` | 单账号每天最多执行多少次真实搜索目标群点击操作；`0` 表示不设日上限，但前端必须显示风险 warning |
| `per_account_cooldown_days` | `0` | 单账号完成一次真实搜索目标群点击操作后，至少间隔多少天才能再次被本任务规划 |
| `per_keyword_account_daily_limit` | `2` | 同一账号、同一关键词每天最多搜索 / 点击目标群的次数；用于覆盖“单关键词单账号每天搜索次数 ≤ 2”的默认硬限 |
| `max_actions_per_day` | `100` | 任务级每天最多创建 / 执行多少次真实搜索目标群点击操作 |
| `hourly_skip_probability` | `0` | 每个自然小时进入规划前的跳过概率；命中后本小时不创建真实搜索 action，并写入 pacing stats |
| `daily_skip_probability` | `0` | 每天进入规划前的跳过概率；命中后当天不创建真实搜索 action，并写入 pacing stats |
| `skip_probability_per_action` | `0.1` | 单个候选 action 的显式放弃概率；命中后可创建 `skipped` action，`skip_reason=skipped_by_behavior_pacing` |
| `hourly_jitter_percent` | `30` | 小时内 action 计划时间抖动百分比；影响本小时 `scheduled_at` 分布，不突破小时硬上限 |
| `daily_jitter_percent` | `20` | 日内预算分布抖动百分比；影响全天 action 在可执行时段内的分配，不突破日硬上限 |

字段校验：

- `per_account_total_action_limit`、`per_account_daily_action_limit`、`per_account_cooldown_days`、`per_keyword_account_daily_limit`、`max_actions_per_day` 必须是 `>= 0` 的整数；空值按默认值写入，负数拒绝保存。
- `hourly_skip_probability`、`daily_skip_probability`、`skip_probability_per_action` 必须是 `0..1` 小数。前端可以用百分比输入展示，但提交给 API 前必须归一为 `0..1`；提交 `10` 这类未归一百分数必须被拒绝。
- `hourly_jitter_percent`、`daily_jitter_percent` 必须是 `0..100` 的整数或一位小数；超过 100、负数或非数字拒绝保存。
- `actions_per_round`、`max_actions_per_hour` 和 `hourly_min_successful_joins` 必须是 `>= 0` 的整数；`hourly_min_successful_joins > max_actions_per_hour` 时拒绝保存。

“操作次数”统计口径：

- 计入：已经创建且预计会向目标机器人发起搜索的 `pending / claiming / executing / success / failed` action；以及已经向目标机器人发送关键词后才失败的 action，包括 `target_not_in_results`、`pages_exhausted=true` 和带重试策略的失败。
- 不计入：`skipped_by_behavior_pacing`、代理 / 环境 / 协议样本 / 权限预检失败、全订阅不可用、缺少客户端元数据、关键词矩阵不允许等尚未向 Telegram / 目标机器人发起真实搜索的 action。
- 规划时必须同时统计未来 open action，防止同一账号、同一关键词或任务日预算被并发规划透支。

时间窗口、并发和随机采样口径：

- 所有“每天”字段都按租户时区解释，窗口为租户本地日期 `[00:00, 24:00)`；stats 必须同时写入 `tenant_timezone` 和 `local_date`，不能用服务器 UTC 日期替代。
- Planner 计算任务日、账号日、同账号同关键词日上限时，必须在同一数据库事务内统计 open action 并写入新 action；同一 `task_id + account_id + local_date`、`task_id + account_id + keyword_hash + local_date` 和 `task_id + local_date` 的计数不能被并发 planner 透支。
- `daily_skip_probability`、`hourly_skip_probability` 和 jitter 采样结果必须落库为 pacing decision，键分别为 `task_id + local_date`、`task_id + hour_start`、`action_candidate_key`；同一窗口内重复 planner tick、worker 重启或 retry 不得重新抽样导致一会儿跳过一会儿执行。
- 修改运行中任务的节奏与账号上限只影响未来规划；已经 `claiming / executing` 的 action 不自动取消。已经 `pending` 的 action 只有在运营显式选择“按新节奏重排”时才可取消 / 重建，并必须写审计。
- `skip_probability_per_action`、`hourly_jitter_percent`、`daily_jitter_percent` 的唯一权威来源是 `pacing_config`；旧 `anti_detection.rhythm.skip_probability_per_action` 和旧 `pacing_config.jitter_percent` 只允许作为兼容输入被 normalization 映射到新字段，二者同时出现且值冲突时必须阻断保存。
- `search_join_group` 的实时 pacing / random decision 不调用 LLM；所有在线随机判断必须由规则、配置、seeded random 和 `search_join_pacing_decisions` 持久化决策完成。LLM 仅用于配置建议、关键词生成、目标相关性解释和复盘分析，不得直接决定某个账号是否搜索、点击、加入、跳过或重排。

Planner 硬闸门顺序：

1. 协议样本、授权槽位、代理、客户端元数据、warmup、权限、关键词矩阵等环境闸门。
2. `daily_skip_probability` 和 `hourly_skip_probability`；命中后本日 / 本小时直接停止规划，不得继续用小时补量绕过。
3. `max_actions_per_day` 任务级日上限。
4. `per_account_total_action_limit`、`per_account_daily_action_limit`、`per_account_cooldown_days` 账号级上限。
5. `per_keyword_account_daily_limit` 同账号同关键词日上限。
6. 单 IP / 代理日上限、跨账号同关键词并发上限和账号执行互斥锁。
7. `hourly_round_curve`、`actions_per_round`、`max_actions_per_hour`、`hourly_min_successful_joins` 小时执行模型。
8. `skip_probability_per_action`；命中后只允许生成显式 `skipped` action，不能假成功。
9. `hourly_jitter_percent` 和 `daily_jitter_percent` 只调整 `scheduled_at`，不能突破以上任何硬上限。

任务 stats / action result 必须暴露以下事实，方便运营判断“没执行”是主动节奏还是资源阻塞：

- `search_join_stats.pacing_limits.task_daily_action_count`
- `search_join_stats.pacing_limits.per_account_daily_limit_reached`
- `search_join_stats.pacing_limits.per_account_total_limit_reached`
- `search_join_stats.pacing_limits.per_account_cooldown_days_active`
- `search_join_stats.pacing_limits.per_keyword_account_daily_limit_reached`
- `search_join_stats.pacing_limits.task_daily_limit_reached`
- `search_join_stats.pacing_limits.hourly_skipped_by_pacing`
- `search_join_stats.pacing_limits.daily_skipped_by_pacing`
- `action.result.skip_reason=skipped_by_behavior_pacing`

创建 / 编辑页要求：

- 仅当任务类型为 `search_join_group` 时展示本组字段。
- 账号级设置展示：每账号总上限、每账号每日上限、每账号间隔天数、单账号单关键词每日上限。
- 任务级设置展示：任务每日上限、每小时上限、每轮 action 数、每小时最低成功数。
- 跳过与抖动设置展示：小时跳过、天跳过、单 action 显式跳过、小时抖动、天抖动。
- 详情页必须展示本日剩余额度、账号命中限制数量、被 pacing 跳过数量和最近一次命中的限制原因。
- 编辑运行中任务时必须展示“仅影响未来规划”的提示；如果运营选择重排 pending action，页面必须展示将取消 / 重建的 action 数、影响账号数、影响关键词数和审计原因输入框。
- 所有 `0` 作为“不设该项上限”的字段必须在页面文案中显示为“不限制”，不得显示成“0 次”导致误解；但 `max_actions_per_hour=0` 或 `hourly_round_curve` 当前小时为 0 表示该小时不主动规划，页面必须区分“不限制”和“不规划”。
- 创建、编辑和通用任务设置 API 必须允许 `search_join_group.pacing_config.max_actions_per_hour=0`，普通任务仍保持通用 pacing 小时上限最小为 1；前端也必须只对搜索目标群点击任务开放 0。
- 仅 search_join 生效的字段不得出现在 AI 活跃群、频道浏览、频道点赞、频道评论 / 回复等任务创建页，也不得被通用 pacing normalization 写回其他任务类型。

### 8.6 入群转化率 控制

|指标|阈值|控制方式|
|---|---|---|
|单账号加入/曝光比|30-60%|`non_target_browse_probability = 0.2` + 入群前非目标浏览后不加入|
|单关键词单账号每天搜索次数|≤ 2 次|scheduler 硬限|
|单 IP 每天目标机器人请求次数|≤ 50|IP 健康度检查|
|跨账号同关键词同时段并发|≤ 10 个账号|task_start_jitter + scheduler|
|同账号授权槽位并发|= 1|账号级执行互斥锁；主/备槽位不得并行跑同一账号|
|入群后立即退出比例|= 0（默认）|默认 `post_join_policy=stay_joined`；延迟退出必须由独立清理任务执行|

入群转化率 > 80% 或 < 10% 时标记为运营异常，不作为单次 action 的失败条件；连续异常才触发任务级 warning。入群后 24h 内退出比例、7 天留存状态和目标机器人拒绝率必须同时观察，避免只用“加入成功”误判真实效果。#

### 8.7 显式放弃动作

Planner 允许按 `skip_probability_per_action` 生成显式放弃动作，但不能做静默假成功：

- 放弃动作落库为 `skipped`，`skip_reason=skipped_by_behavior_pacing`。
- 不发送搜索、不点击、不加入，不计入搜索成功、加入成功或小时覆盖。
- 任务详情必须展示放弃动作数量和比例；比例异常时进入风控 warning。
- 该机制只用于节奏分布，不得掩盖代理、协议样本、账号锁或目标机器人错误。

### 8.8 翻页真实化 不允许线性翻页 `1→2→3→4`。必须支持回退：
```python
 def generate_paging_sequence(max_pages: int, scroll_back_prob: float, max_back_times: int) -> list[int]: """生成翻页序列，带回退""" pages = [1] current = 1 back_count = 0 while current < max_pages and random.random() < 0.7: if back_count < max_back_times and random.random() < scroll_back_prob and current > 1: current -= 1 back_count += 1 else: current += 1 pages.append(current) return pages
```


### 8.9 入群后与后续任务联动

搜索入群成功不是终点。公开排名口径更强调开放活跃、持续内容和用户互动，因此搜索入群必须和后续任务形成可控链路，而不是让账号加入后沉默或立即批量发言。

联动对象：

| 后续任务 | 联动方式 | 默认限制 |
| --- | --- | --- |
| AI 活跃群 `group_ai_chat` | 成功加入并复检 `can_send=True` 后，把账号追加到同目标任务的 ready pool | `min_retention_before_ai_minutes=360`；新加入账号每小时占参与账号比例 ≤ 20% |
| 转发监听 / 源群读取 | 如果目标作为监听源，成功加入后可进入可读取复检 | 只加监听能力，不自动发言 |
| 频道评论 / 回复 | 如果目标群关联频道讨论组，先写入准入事实，后续任务自行检查评论权限 | 不因为搜索加入成功而跳过频道/讨论组权限校验 |
| 运营中心目标健康 | 更新目标可见性、入群成功、留存、后续任务活跃度 | 排名变化只做观察，不作为完成硬条件 |

联动状态机：

```text
search_join success
  -> membership_observed
  -> retention_observing
  -> can_send_revalidated
  -> linked_task_ready_pending
  -> linked_task_ready_active / linked_task_blocked
```

关键规则：

1. `SearchJoinMembershipVerifier` 成功后写入目标成员关系、`joined_via=search_join`、`joined_at` 和 action id。
2. 只有 `post_join_policy=stay_joined` 且留存观察未失败的账号，才能进入后续任务联动。
3. AI 活跃群联动必须等待冷却和可发言复检；不能在刚入群后立即集中发言，也不能强行围绕搜索关键词发言。
4. 后续 AI 活跃群仍使用既有账号面具、短期立场、真人上下文、去重、小时硬目标和质量漏斗；搜索入群只提供“该账号已加入目标群”的准入事实。
5. 如果后续任务暂停、目标不可发言、账号在线状态 stale、账号冷却不足或 AI 质量不足，联动状态保持 `linked_task_blocked` 并展示具体原因，不得把搜索入群成功误报为活群成功。
6. 任务删除、目标变更或账号退出群时，必须 reconcile linked ready pool，移除不再满足条件的账号。

配置字段：

```jsonc
{
  "post_join_task_links": [
    {
      "target_task_id": "group-ai-task-id",
      "link_type": "ai_group_ready_pool",
      "enabled": true,
      "activation_delay_minutes": [60, 360],
      "min_retention_before_ai_minutes": 360,
      "max_new_joined_accounts_per_hour_ratio": 0.2,
      "require_can_send_revalidation": true
    }
  ]
}
```

## 9. 任务配置 Schema

### 9.1 完整 Payload
```jsonc
 { "task_type": "search_join_group", "execution_mode": "mtproto_userbot", "name": "迪拜房产群搜索目标点击", "search_bots": ["jisou"], "keywords": [ {"text": "迪拜房产", "business_region": "AE", "account_locale": "zh-CN", "proxy_country": "SG", "lang": "zh", "weight": 1.0, "decoy": false}, {"text": "迪拜租房", "business_region": "AE", "account_locale": "zh-CN", "proxy_country": "AE", "lang": "zh", "weight": 0.8, "decoy": false}, {"text": "天气预报", "business_region": "CN", "account_locale": "zh-CN", "proxy_country": "SG", "lang": "zh", "weight": 1.0, "decoy": true} ], "target_groups": [ { "operation_target_id": 123, "target_input": "@yourgroup", "match_strategy": "username_or_peer_id" } ], "anti_detection": { /* 见 §8.1 */ }, "proxy_policy": { /* 见 §6.5 */ }, "account_config": { "selection_mode": "manual", "account_ids": [101, 102, 103, 104, 105], "authorization_roles": ["primary", "standby_1"], "same_account_concurrency": 1, "authorization_switch_policy": "primary_first_failover_only", "max_concurrent": 5, "cooldown_per_account_minutes": 30, "ban_policy": "pause_task" }, "pacing_config": { "mode": "curve", "curve_type": "steady", "hourly_round_curve": [0,0,0,0,0,0,1,1,2,2,2,2,1,1,2,2,3,3,2,2,1,1,0,0], "actions_per_round_mode": "auto", "actions_per_round": 5, "max_actions_per_hour": 20, "hourly_min_successful_joins": 0, "max_actions_per_day": 100, "per_account_total_action_limit": 0, "per_account_daily_action_limit": 1, "per_account_cooldown_days": 0, "per_keyword_account_daily_limit": 2, "hourly_skip_probability": 0, "daily_skip_probability": 0, "skip_probability_per_action": 0.1, "hourly_jitter_percent": 30, "daily_jitter_percent": 20, "active_hours": [{"start": "08:00", "end": "23:00"}] }, "failure_policy": { "max_retries": 2, "retry_delay_seconds": 300, "on_account_banned": "pause_task", "on_api_rate_limit": "wait_and_retry", "on_target_not_found": "skip", "on_airport_all_nodes_unavailable": "pause_task_and_notify_admins" } }
```
 ##

#### 9.1.1 关键词字段语义（executor 实际使用）

|字段|类型|executor 中的用途|
|---|---|---|
|`text`|string|发送给搜索机器人的关键词原文|
|`business_region`|ISO 3166-1 alpha-2|关键词业务区域，用于运营统计和允许矩阵，不要求等于账号，也不能让备用 session 使用另一个代理出口国家|
|`account_locale`|BCP 47|账号语言画像，例如 `zh-CN`、`en-US`；用于选择客户端元数据和账号池|
|`proxy_country`|ISO 3166-1 alpha-2|期望代理出口国家；执行前以 `observed_exit_country` 校验，不用 Clash 节点 host 推断|
|`lang`|ISO 639-1|发送关键词和客户端语言的推荐值；与 `client_metadata.lang_code` 不一致时进入 warning / manual review，不默认硬跳过|
|`weight`|float (0-10)|**用于 action 配额分配**：planner 按 `Σweight` 计算每个关键词的 action 占比。如目标关键词 weight 总和 10、decoy 关键词 weight 总和 5，则 decoy 占 1/3 触发概率|
|`decoy`|boolean|true = 非目标关键词，只用于行为掩护，不计入目标群加入统计；false = 目标关键词|
|
 ##

关键词合法性由“业务区域、账号语言、代理出口国家”允许矩阵决定。禁止继续使用 `keyword.region == account.region_code == ip.country` 这类硬等式；例如中文账号 + 新加坡出口 + 阿联酋业务关键词是允许组合，但必须在配置矩阵中显式声明。

#### 9.1.2 target_groups 复用 OperationTarget `target_groups[].operation_target_id` 是任务配置的唯一持久引用。任务创建向导可以接收 `target_input`（`@username`、公开链接、邀请链接或 peer id）用于解析或 upsert `OperationTarget`，但任务保存后只持久化 `operation_target_id`、`match_strategy` 和可选权重；不得在 `type_config` 中直接保存裸 `peer_id`。Action payload 可以带执行时的目标快照，但必须来自 `OperationTarget`，并带 `operation_target_version` 便于目标资料变更后审计。#

### 9.2 Pydantic Schema
```python
 class SearchBotTarget(BaseModel): username: Literal["jisou", "jisou2bot", "soso", "smss", "CJSY"] weight: float = 1.0 class SearchJoinKeyword(BaseModel): text: str = Field(min_length=1, max_length=64) business_region: str | None = None account_locale: str = "zh-CN" proxy_country: str | None = None lang: str = "zh" weight: float = Field(default=1.0, ge=0, le=10) decoy: bool = False class SearchJoinTargetGroup(BaseModel): operation_target_id: int | None = None target_input: str | None = None match_strategy: Literal["username_only", "peer_id_only", "username_or_peer_id", "title_fuzzy"] = "username_or_peer_id" weight: float = Field(default=1.0, ge=0, le=10) class AntiDetectionConfig(BaseModel): warmup_days: int = Field(default=3, ge=0, le=30) warmup_daily_actions: int = Field(default=3, ge=1, le=20) behavior_realism: BehaviorRealismConfig rhythm: RhythmConfig paging: PagingConfig anti_clustering: AntiClusteringConfig class ProxyPolicyConfig(BaseModel): required: bool = True allowed_proxy_types: list[Literal["residential_static", "mobile_4g", "airport_clash"]] = ["residential_static", "airport_clash"] min_ip_reputation_score: float = Field(default=70, ge=0, le=100) min_exit_ip_stability_score: float = Field(default=80, ge=0, le=100) min_binding_age_days: int = Field(default=30, ge=0, le=180) max_accounts_per_asn: int = Field(default=5, ge=1, le=50) max_accounts_per_ip_cidr_24: int = Field(default=3, ge=1, le=20) max_daily_requests_per_ip: int = Field(default=50, ge=1, le=500) max_weekly_requests_per_ip: int = Field(default=200, ge=1, le=2000) class SearchJoinGroupConfig(BaseModel): execution_mode: Literal["mtproto_userbot"] = "mtproto_userbot" search_bots: list[SearchBotTarget] = Field(min_length=1) keywords: list[SearchJoinKeyword] = Field(min_length=1, max_length=500) target_groups: list[SearchJoinTargetGroup] = Field(min_length=1, max_length=50) anti_detection: AntiDetectionConfig proxy_policy: ProxyPolicyConfig same_account_concurrency: Literal[1] = 1
```
 #

`SearchJoinGroupConfig.pacing_config` 必须额外包含 §8.5.1 的 search_join 专属字段：`per_account_total_action_limit`、`per_account_daily_action_limit`、`per_account_cooldown_days`、`per_keyword_account_daily_limit`、`max_actions_per_day`、`hourly_skip_probability`、`daily_skip_probability`、`skip_probability_per_action`、`hourly_jitter_percent`、`daily_jitter_percent`。这些字段不得提升到任务中心通用 `PacingConfig` 后影响其他任务。

### 9.3 配置校验规则 - `execution_mode` 首版只能是 `mtproto_userbot`，前端必须说明这不是手机 UI 自动化 - 真实机器人协议样本未采集完成时，只允许保存草稿和运行 parser fixture，不允许启动真实灰度 - `decoy=true` 的关键词占比 ≥ 30%（硬约束，否则任务不创建） - `target_groups[].operation_target_id` 或 `target_input` 至少填一个；保存任务前必须解析 / upsert 为 `OperationTarget` 并持久化 `operation_target_id` - 关键词的 `business_region / account_locale / proxy_country` 必须落入允许矩阵 - `proxy_policy.required=true` 时，账号池中所有被选授权槽位必须已绑代理节点并完成 `observed_exit_ip` 健康检查 - 主/备用授权槽位必须各自拥有完整客户端元数据，且同账号内不得复用元数据组合或代理节点 - 同一 `account_id` 在 `search_join` 执行中只允许 1 个 action 处于 claiming / executing - `anti_detection.behavior_realism.decision_delay_seconds[0] >= 2`（不允许秒点） - `paging.max_pages` 默认且上限为 70，找满 70 页未命中必须停止任务 - `per_account_total_action_limit >= 0`、`per_account_daily_action_limit >= 0`、`per_account_cooldown_days >= 0`、`per_keyword_account_daily_limit >= 0`、`max_actions_per_day >= 0`；`0` 表示该项不设硬上限 - `hourly_skip_probability / daily_skip_probability / skip_probability_per_action` 必须在 `0..1` - `hourly_jitter_percent / daily_jitter_percent` 必须在 `0..100` - `hourly_min_successful_joins` 不得大于 `max_actions_per_hour`；`max_actions_per_day` 小于 `max_actions_per_hour` 时允许保存但必须提示 planner 会以日上限为准 - `decoy_join_enabled=true` 不作为首版默认能力，若开启必须单独走风控审批和审计 - 默认 `post_join_policy=stay_joined`，任何立即退出策略都必须单独审批并写审计 #

## 10. 目标机器人 / SOSO 协议交互契约 本节定义 executor 与第三方索引机器人（@searchbot、@soso、@smss、@CJSY）交互的协议契约。**dev 必须先按 §4.8 采集真实样本，再按本节实现 parser 和 executor**；样本缺失时只能跑 fixture / precheck，不允许启动真实灰度。#

### 10.1 通用交互流程
```
 [账号] 发送搜索词 ──> [机器人] [机器人] 返回搜索结果消息（带 inline button 或 link） [账号] 加入目标群对应的 button / link [机器人] → TG 客户端导航到目标群 [账号] 在目标群停留 N 秒并按 post_join_policy 留存或延迟处理
```
 #

### 10.2 @searchbot 目标机器人（首版目标）

已知行为（基于业内调研 + 外网资料）：

| 阶段 | 行为 |
| --- | --- |
| 触发 | 账号首次需向 @searchbot 发送 `/start`，否则机器人不响应后续请求 |
| 搜索 | 发送纯文本关键词，不加 @，不加前缀 |
| 回复 | 机器人返回一条或多条带 inline keyboard buttons 的消息，每个 button 对应一个群 / 频道结果 |
| 翻页 | 单次返回 5-10 条结果；更多结果需点击底部“下一页” button |
| 打开 | 对群对应 button / Telegram 内部 URL 执行 MTProto callback 或 URL resolve，记录协议事实；外部 HTTP URL 不在首版默认打开；decoy 浏览只允许 `navigate_only` 安全按钮 |
| 入群后策略 | 默认留在目标群；如审批为延迟退出，只能由独立清理任务在留存期后执行 |
 **协议细节**：- **消息格式**：目标机器人结果消息通常为 `InlineKeyboardMarkup`，包含：- 主结果区：每行 1 个 button，button.text 为群名/标题，button.data 或 button.url 携带定位信息 - 底部导航：单独一行 button，如 `« 上一页` `第 1/3 页` `下一页 »` - **button 类型**：- `callback_data`：点击后触发 `GetBotCallbackAnswerRequest`，机器人返回 `BotCallbackAnswer`（含 message 或 url） - `telegram_url`：`t.me` / Telegram 内部 URL，允许在 MTProto 会话内 resolve / join - `external_http_url`：非 Telegram 外部 URL，首版不得默认打开，返回 `external_url_requires_web_profile` - **button effect**：parser 必须把按钮标成 `navigate_only / join_candidate / external / unknown`；入群前 decoy 浏览只能点击 `navigate_only`，不得点击 `join_candidate / external / unknown` - **目标群匹配**：- 优先按 `telegram_url`（如 `https://t.me/yourgroup`）匹配 `OperationTarget.normalized_username` - 次选按 callback_data 携带的 chat_id 匹配 `OperationTarget.peer_id` - 兜底按 button.text 模糊匹配 `OperationTarget.title` #

### 10.3 @soso、@smss、@CJSY（第二版扩展）

|机器人|已知差异|
|---|---|
|@soso|类似目标机器人，inline button 模式|
|@smss|同样 inline button，但可能带"广告位"前置内容|
|@CJSY / @So1234Bot|经典老牌，部分结果可能用纯文本 + URL 而非 button|
|
 **第二版适配**：每个搜索机器人需独立写 `parse_search_results(response, bot_username)` 解析器，前 3 个模式相似可复用，第 4 个需要单独的 plain text parser。#

### 10.4 executor 入口协议
```python
 async def search_via_bot( client: TelegramClient, bot_username: str, keyword_text: str, max_pages: int = 5, ) -> SearchResultPage: """统一的搜索入口，按 bot_username 分发到不同解析器""" async with client.conversation(bot_username, timeout=60) as conv: # 1. 触发搜索 await conv.send_message(keyword_text) response = await conv.get_response() # 2. 按机器人分发解析 parser = SEARCH_BOT_PARSERS.get(bot_username) return await parser.parse(response, max_pages=max_pages) @dataclass class SearchResultPage: message_id: int bot_peer: Peer buttons: list[SearchResultButton] # 当前页所有 button has_next: bool current_page: int total_pages_estimated: int

None raw_response: Message # 原始消息，供 fallback @dataclass class SearchResultButton: text: str # button 文字（群名/标题） button_type: Literal["callback_data", "telegram_url", "external_http_url"] callback_data: bytes|None # callback 类型时 url: str|None # telegram_url / external_http_url 类型时 target_chat_id: int|None # 解析出的 chat_id target_username: str|
|
 None # 解析出的 username position: int # 在当前页的位置（1-based） is_target: bool = False # 是否匹配目标群
```
 #

### 10.5 翻页协议
```python
 async def navigate_to_page( client: TelegramClient, current: SearchResultPage, target_page: int, ) -> SearchResultPage: """翻页：支持前进和回退""" delta = target_page - current.current_page if delta == 0: return current # delta > 0 点"下一页"；delta < 0 点"上一页" button_text = "下一页 »" if delta > 0 else "« 上一页" page_nav_button = find_page_nav_button(current, button_text) if page_nav_button is None: raise NoNavigationButtonError(current.current_page, target_page) await client(GetBotCallbackAnswerRequest( peer=current.bot_peer, msg_id=current.message_id, data=page_nav_button.callback_data, )) # 等下一条消息（机器人回复新页结果） new_msg = await client.get_messages(current.bot_peer, ids=[current.message_id + 1]) return parse_search_results(new_msg, max_pages=...)
```
 #

### 10.6 已知异常模式

|异常|触发场景|处置|
|---|---|---|
|`BotBlockedError`|账号被机器人拉黑（目标机器人主动 block）|账号下线，标记 `bot_blocked`，换账号继续|
|`TimeoutError` (conv.get_response 超时)|机器人维护 / 网络问题|retry 3 次，指数退避|
|空消息回复|关键词无结果|action 标 `skipped`，`skip_reason=keyword_no_results`|
|找满 70 页仍无目标群 button|关键词相关但目标群不在结果中|action 标 `failed`，`error_code=target_not_in_results`，任务自动 `stopped`|
|外部 HTTP URL button|button 指向非 `t.me` / Telegram 内部地址|action 标 `skipped`，`skip_reason=external_url_requires_web_profile`，不默认打开|
|button effect unknown|样本无法判断点击后是否入群、外跳或触发验证|action 标 `skipped`，`skip_reason=button_effect_unknown`，等待人工样本确认|
|代理出口防泄漏失败|MTProto 连接未证明走绑定代理或 observed exit IP 不一致|action 标 `skipped`，`skip_reason=proxy_egress_guard_failed`|
|API ID / session 不一致|授权槽位登录 API ID、运行时 API ID 或 session 文件不一致|action 标 `skipped`，`skip_reason=api_id_client_metadata_mismatch`|
|验证码 / 人机验证消息|目标机器人偶尔对异常账号弹验证|自动识别 → 中断该 action → 标记 `bot_response_changed` → 告警|
|FloodWaitError(seconds > 60)|目标机器人对短时间高频请求限流|累计到账号 cooldown，自动 sleep 后重试|
|
 #

### 10.7 行为契约 executor 必须严格遵守：- ✅ **必须**走 `client.conversation()` 完整会话，不要直接 `InvokeWithLayerRequest` 等底层 API - ✅ **必须**按 §10.4 协议解析结果，不要假设消息结构 - ✅ **必须**对每个 button 操作间隔至少 1 秒，避免被识别为脚本 - ✅ **必须**在真实点击前通过 `proxy_egress_guard` 和 API ID / session 一致性校验 - ✅ **允许**在样本确认 button 是 callback 类型时使用 `GetBotCallbackAnswerRequest`，但必须带 conversation 上下文、等待响应、记录 callback/url 事实并审计 - ✅ **必须**区分 `callback_data`、Telegram 内部 URL 和外部 HTTP URL；外部 URL 首版只记录并跳过 - ✅ **必须**按 `button_effect` 过滤 decoy 浏览，decoy 只允许 `navigate_only` - ❌ **禁止**脱离样本和会话上下文盲发 callback - ❌ **禁止**代理失败时回退本机直连 - ❌ **禁止**用 MTProto 客户端元数据冒充 HTTP iOS Safari / Telegram iOS WebView - ❌ **禁止**并发向同一机器人发多个请求 - ❌ **禁止**在搜索结果中"全点"所有 button（入群转化率控制见 §8.5）

## 11. 与现有系统的集成边界 本节明确 search_join_group 任务**复用 / 旁路 / 新增**现有代码模块的边界，避免 dev 在实现时遗漏集成点或重复造轮子。#

### 11.1 完全复用（不改代码）

|模块|复用方式|
|---|---|
|`backend/app/services/task_center/service.py`|任务的 CRUD、列表、详情、stats、reset、resume 等接口完全复用|
|`backend/app/services/task_center/dispatcher.py`|Action 的 claim / execute / 回写 result / 重试 完全复用 dispatcher 现有逻辑|
|`backend/app/services/task_center/precheck.py`|任务创建预检扩展 `search_join_precheck` 字段，其余预检复用|
|`backend/app/services/task_center/stats.py`|任务 stats 扩展 `search_join_stats` 字段（见 §13.3），其余统计复用|
|`backend/app/services/task_center/config_normalization.py`|`search_join_group` 加入 `task_types` 白名单，自动绑默认规则集|
|`backend/app/api/routers/task_center.py`|`POST /api/tasks` 等通用接口完全复用|
|`backend/app/services/operations_center_rule_sets.py`|规则集支持 `task_types=["search_join_group"]`，复用现有规则集机制|
|`backend/app/auth.py` + `permission_middleware.py`|权限控制完全复用；新增 `tasks.create.search_join_group` 权限|
|风控中心账号小时/日上限|复用 `risk_control.account_hourly_limit` / `account_daily_limit`|
|风控中心账号冷却|复用 `risk_control.account_cooldown_minutes`|
|监控中心 RuntimeSummary|复用 `RuntimeSummary` 读模型；新增 search_join 维度的统计|
|审计日志|复用 `audit.py` 的写审计机制|
|前端 AppShell / 路由|复用 `frontend/src/app/routes.ts` 的 `/task-center` 路由|
|前端 taskCenterViewModel|扩展 `TaskTypeValue` 支持 `search_join_group`，其余列表 / 详情展示复用|
|
 #

### 11.2 旁路与有限复用（不适用主流程，但不能忽略结果验证）

|模块|旁路原因|
|---|---|
|`backend/app/services/task_center/channel_membership.py`|本任务不涉及频道关注，旁路|
|`backend/app/services/task_center/membership_admission.py`|不复用主任务的会员准入子任务，但必须复用挑战/验证码/审批等失败 taxonomy；新增 `SearchJoinMembershipVerifier` 负责入群结果验证、join approval / captcha / invite expired 识别和留存状态回写|
|`backend/app/services/task_center/listener_runtime.py`|本任务不监听群聊消息，旁路|
|`backend/app/services/task_center/hard_hourly.py`|复用“自然小时桶、future open、overdue open、deficit、catching_up/met/blocked/missed”统计思想；不得复用 AI 发言、AI 生成、MiMo/Mino 或 `send_message` 语义，搜索入群指标必须独立命名为 `search_join_hourly_*`|
|`backend/app/services/task_center/ai_generator.py`|实时执行、pacing 和 random decision 不调用 AI / LLM；本任务在线文案是用户提供的关键词。LLM 只允许作为离线配置建议、decoy 关键词候选、目标相关性解释和复盘分析工具|
|前端 Wizard 现有 AI 活群 / 频道分支|本任务新建独立的 wizard 分支|
|前端 GroupAIChat / ChannelComment 等专有组件|本任务不复用这些专有组件|
|
 #

### 11.3 新增（独立模块）

|模块|新增位置|说明|
|---|---|---|
|`SearchJoinGroupExecutor`|`backend/app/services/task_center/executors/search_join_group.py`|完整 executor 实现（见 §12）|
|`ProxyProvider` 抽象 + 一家供应商实现|`backend/app/services/proxy_pool/`|代理供应商抽象层|
|`ClientMetadataGenerator`|`backend/app/services/client_metadata/`|运行时随机生成 MTProto 客户端元数据|
|`BotSearchDispatcher`|`backend/app/services/task_center/executors/search_join_group.py`|协议解析（见 §10）|
|`SearchJoinMembershipVerifier`|`backend/app/services/task_center/search_join_membership.py`|验证入群结果、挑战类型、留存策略和失败 taxonomy|
|数据库表|见 §13.1|包含协议样本、机场订阅、机场节点、出口 IP 观测、代理绑定、环境绑定、warmup state、客户端元数据组合审计、授权执行锁、IP 信誉历史、search_join_action_stats 和 search_join_pacing_decisions 等 search_join 专属表|
|前端 Wizard 第 5 步 search_join_group 分支|`frontend/src/app/views/TaskCenterWizardSections.tsx`|运营真实性配置面板|
|前端任务详情"搜索入群统计" Tab|`frontend/src/app/views/TaskCenterDetailModal.tsx`|排名轨迹 + 行为漏斗|
|风控中心 search_join 维度告警|`frontend/src/app/views/RiskControlView.tsx`|proxy_dead / bot_blocked / fingerprint_anomaly 告警类型|
|
 #

### 11.4 与 OperationTarget 的关系 `target_groups[].operation_target_id` 是任务配置唯一持久引用（现有 `backend/app/models/operation_target.py`）。任务创建向导从运营目标列表中选已有目标，或粘贴 username / 公开链接 / 邀请链接 / peer id 自动解析后 upsert 到 OperationTarget 再引用。task.type_config 只存 `operation_target_id`、匹配策略和权重，不直接存裸 `peer_id`；action payload 可携带来自 OperationTarget 的执行快照和版本号。#

### 11.5 与 TG Account 的关系 账号创建 / 资料初始化 / 备用 session / 设备清理等复用现有 `account_security/service.py` 全部机制。本任务新增的"账号环境栈"绑定在 `tg_account_authorizations` 授权资产槽位之上，不修改账号主表；`primary / standby_1 / standby_2` 都按独立客户端身份管理。#

### 11.6 核心模型引用（dev 必读）

|模型 / 服务|文件|复用 / 新增|
|---|---|---|
|`Task`|`backend/app/models/task.py`|复用|
|`Action`|`backend/app/models/task.py`|复用；新增 `action_type=search_join`|
|`ExecutionAttempt`|`backend/app/models/task.py`|复用|
|`TgAccount`|`backend/app/models/account.py`|复用|
|`OperationTarget`|`backend/app/models/operation_target.py`|复用（target_groups 引用）|
|`TaskTypeValue` Literal|`backend/app/schemas/task_center.py`|扩展|
|`AccountEnvironmentBinding`|新建 migration|新增|
|`AccountProxyBinding`|新建 migration|新增|
|`ProxyAirportSubscription`|新建 migration|新增|
|`ProxyAirportNode`|新建 migration|新增|
|`AccountProxyWarmupState`|新建 migration|新增|
|`BotProtocolSample`|新建 migration|新增|
|`ProxyExitIpObservation`|新建 migration|新增|
|`AccountAuthorizationExecutionLock`|新建 migration|新增|
|`IpReputationHistory`|新建 migration|新增|
|`SearchJoinActionStats`|新建 migration|新增|
|`RuleSet`|`backend/app/models/rule_set.py`|复用（`task_types` 扩展）|
|`RuntimeSummary`|`backend/app/models/runtime_summary.py`|复用（新增 search_join 维度）|
|`TaskRuntimeSummaryOut`|`backend/app/schemas/runtime_summary.py`|复用|
|`AiGenerator`|`backend/app/ai_gateway.py`|**不调用**（本任务无 AI 生成）|
|`RiskControl`|`backend/app/services/risk_control.py`|复用账号上限；新增 search_join 维度|
|
 #

### 11.7 外部依赖（本任务新增）

|依赖|用途|采购决策|
|---|---|---|
|代理供应商（IPFLY / Bright Data / ProxyScrape 任一）|提供独享静态住宅 IP|**用户拍板**：第一版接哪家？灰度期先验证一家，第二季度加第二家容灾|
|机场订阅|提供可解析、可测速、可绑定的代理节点池|首版可作为 `airport_clash` 供应商实现；支持 Base64 URI 列表 / Clash YAML / JSON，订阅 URL 加密存储，节点按授权槽位容量随机分配后固定到授权槽位，节点不通时按策略切换下一个健康节点并让该授权槽位重新 warmup|
|IPQS（ipqualityscore.com）|IP 信誉分查询|**必采购**：每日 IP 健康度检测|
|Spamhaus DNSBL|黑名单查询|公开 API 免费|
|IP2Location|IP 类型校验（residential / mobile）|付费，必要时采购|
|


## 12. 执行器设计

### 12.1 Executor 文件 `backend/app/services/task_center/executors/search_join_group.py`

### 12.2 核心入口
```python
class SearchJoinGroupExecutor:
    async def execute_action(self, action: Action) -> ActionResult:
        env = await assert_environment_ready(
            account_id=action.account_id,
            developer_app_id=action.payload["developer_app_id"],
            authorization_id=action.payload["authorization_id"],
            session_role=action.payload["session_role"],
        )
        await assert_authorization_api_id_matches_runtime(env.authorization, env.client_metadata)
        failover = await self.proxy_failover.ensure_healthy_node_or_switch_authorization(env)
        if failover.all_nodes_unavailable:
            return ActionResult(status="skipped", error_code="airport_all_nodes_unavailable")
        await assert_observed_exit_ip_ready(env.proxy_binding_id)
        egress_guard = await assert_proxy_egress_guard(env.proxy_binding)
        lock = await acquire_account_execution_lock(action.account_id, action.id, action.action_type)
        if not lock.acquired:
            return ActionResult(status="skipped", error_code="account_authorization_lock_conflict")
        client = TelegramClient(
            session=env.authorization.session_ciphertext,
            api_id=env.authorization.developer_app_api_id_snapshot,
            api_hash=env.authorization.developer_app_api_hash,
            proxy=self._build_telethon_proxy(env.proxy_binding),
            device_model=env.client_metadata.device_model,
            system_version=env.client_metadata.system_version,
            app_version=env.client_metadata.app_version,
            lang_code=env.client_metadata.lang_code,
            system_lang_code=env.client_metadata.system_lang_code,
        )
        try:
            if not self._passes_warmup(env, action):
                return ActionResult(status="skipped", error_code="account_in_warmup")
            # Search, decoy navigation, target click, membership observe, dwell and post policy.
        finally:
            await release_account_execution_lock(lock)
```
 #

### 12.3 关键函数
```python
 async def _search(self, client, bot_username, keyword): """向搜索机器人发关键词，等回复；日志只记录 keyword_hash""" async with client.conversation(bot_username, timeout=60) as conv: await conv.send_message(keyword.text) response = await conv.get_response() return parse_search_results(response) async def _decision_delay(self, action): """真人化决策延迟""" config = action.payload["anti_detection"]["behavior_realism"] delay_range = config["decision_delay_seconds"] delay = self._sample_interval(delay_range, distribution="normal", std_dev_ratio=0.3) await asyncio.sleep(delay) async def _browse_other_results(self, client, search_results, action, before_click): """浏览非目标结果；默认只打开、停留、返回，不加入非目标群/频道""" config = action.payload["anti_detection"]["behavior_realism"] clicks = [] if random.random() < config.get("pre_join_decoy_click_probability", 0.35): other = [b for b in search_results.buttons if not b.is_target and b.button_effect == "navigate_only"] for target_btn in random.sample(other, min(len(other), random.randint(*config.get("pre_join_decoy_click_count", [1, 2])))): await self._navigate_to(client, target_btn) await asyncio.sleep(random.uniform(*config.get("pre_join_decoy_dwell_seconds", [10, 30]))) await self._navigate_back(client) clicks.append({"button_hash": hash_button(target_btn), "button_effect": target_btn.button_effect, "position": target_btn.position, "joined": False}) return clicks async def _click_target(self, client, search_results, target_button, action): """按真实样本执行 MTProto callback / Telegram 内部 URL 打开，并等待结果""" async with client.conversation(action.payload["bot_username"], timeout=60) as conv: await assert_button_matches_protocol_sample(action.payload["bot_username"], target_button) if target_button.button_effect == "unknown": return ClickResult(success=False, error_code="button_effect_unknown") if target_button.button_type == "callback_data": answer = await client(GetBotCallbackAnswerRequest(peer=conv.peer, msg_id=search_results.message_id, data=target_button.callback_data)) return await parse_callback_answer(answer, target_button) if target_button.button_type == "telegram_url": return await resolve_telegram_url(client, target_button.url) return ClickResult(success=False, error_code="external_url_requires_web_profile") async def _dwell_in_target_group(self, client, target_group, action): """在目标群停留并执行低风险 read/history/read_ack""" config = action.payload["anti_detection"]["behavior_realism"] dwell_range = config["in_group_dwell_seconds"] dwell = self._sample_interval(dwell_range, distribution="normal") await client.get_messages(target_group.peer_id, limit=random.randint(1, 3)) await asyncio.sleep(dwell) # 首版默认不发言；如概率被审批调高，内容策略必须先通过素材审核 if config.get("occasional_message_probability", 0.0) > 0: await self.content_policy.assert_message_allowed(action) action.result["actual_dwell_seconds"] = dwell
```
 #

### 12.4 异常处理

|异常|处理|
|---|---|
|`FloodWaitError(seconds=N)`|自动 sleep(N+5)；累计 N > 3600 时把账号置 cooldown 4h|
|`ChatForbiddenError` / `UsernameNotOccupiedError`|目标群失效 → 任务暂停 + 告警|
|`BotBlockedError`|账号被搜索机器人拉黑 → 该账号标记 inactive，换账号继续|
|`SlowModeWaitError`|群内有慢速模式 → 标记该次 action 失败但不计入账号 ban|
|`telethon.errors.RPCError("FROZEN_METHOD_INVALID")`|账号被冻结 → 暂停账号所有任务 + 告警|
|网络超时 / 连接错误|retry 3 次，每次间隔指数退避；超过则 action 失败|
|
 每次异常必须写入 `action.result.error_code` 和原始 traceback（脱敏后）。

## 13. 数据流转与存储

### 13.1 新增数据表
```sql
-- 设备指纹池
-- 客户端元数据无独立主表：运行时按 §7.2 规则集随机生成，写入 account_environment_bindings。
-- 如果需要审计曾经生成过的客户端元数据组合，单独建 fingerprint_combo_history。
CREATE TABLE fingerprint_combo_history (
  id BIGSERIAL PRIMARY KEY,
  device_model VARCHAR(64) NOT NULL,
  system_version VARCHAR(32) NOT NULL,
  app_version VARCHAR(16) NOT NULL,
  platform VARCHAR(16) NOT NULL,
  combo_key VARCHAR(160) UNIQUE NOT NULL,
  assigned_account_count INT DEFAULT 0,
  first_assigned_at TIMESTAMP WITH TIME ZONE,
  last_assigned_at TIMESTAMP WITH TIME ZONE
);

-- 机场 Clash 订阅
CREATE TABLE proxy_airport_subscriptions (
  id BIGSERIAL PRIMARY KEY,
  name VARCHAR(64) NOT NULL,
  clash_subscription_url_encrypted TEXT NOT NULL,
  provider_label VARCHAR(64),
  subscription_format VARCHAR(32) DEFAULT 'auto',
  max_authorizations_per_node_default INT DEFAULT 1,
  all_nodes_down_policy VARCHAR(32) DEFAULT 'pause_task',
  notify_admin_on_all_nodes_down BOOLEAN DEFAULT TRUE,
  fetch_interval_minutes INT DEFAULT 60,
  last_fetched_at TIMESTAMP WITH TIME ZONE,
  last_fetch_status VARCHAR(32),
  last_fetch_error TEXT,
  is_active BOOLEAN DEFAULT TRUE,
  created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
  updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- 机场 Clash 节点
CREATE TABLE proxy_airport_nodes (
  id BIGSERIAL PRIMARY KEY,
  subscription_id BIGINT NOT NULL,
  node_id VARCHAR(128) NOT NULL,
  node_name VARCHAR(128) NOT NULL,
  protocol VARCHAR(32) NOT NULL,
  proxy_host VARCHAR(128) NOT NULL,
  proxy_port INT NOT NULL,
  proxy_username VARCHAR(64),
  proxy_password_encrypted TEXT,
  uri_scheme VARCHAR(32),
  source_format VARCHAR(32) NOT NULL,
  country VARCHAR(8),
  region VARCHAR(64),
  city VARCHAR(64),
  isp VARCHAR(64),
  asn VARCHAR(32),
  observed_exit_ip VARCHAR(64),
  observed_exit_country VARCHAR(8),
  observed_exit_asn VARCHAR(32),
  observed_exit_isp VARCHAR(64),
  exit_ip_stability_score FLOAT DEFAULT 0.0,
  latency_ms INT,
  node_capacity INT DEFAULT 1,
  assigned_account_count INT DEFAULT 0,
  failover_rank INT DEFAULT 0,
  consecutive_failures INT DEFAULT 0,
  last_unreachable_at TIMESTAMP WITH TIME ZONE,
  health_score FLOAT DEFAULT 100.0,
  is_active BOOLEAN DEFAULT TRUE,
  last_health_check_at TIMESTAMP WITH TIME ZONE,
  created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
  updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
  FOREIGN KEY (subscription_id) REFERENCES proxy_airport_subscriptions(id),
  UNIQUE (subscription_id, node_id)
);

-- 机场节点故障切换审计
CREATE TABLE proxy_node_failover_events (
  id BIGSERIAL PRIMARY KEY,
  subscription_id BIGINT NOT NULL,
  account_id BIGINT NOT NULL,
  from_node_id BIGINT,
  to_node_id BIGINT,
  reason VARCHAR(64) NOT NULL,
  outcome VARCHAR(32) NOT NULL,
  observed_error TEXT,
  admin_notification_status VARCHAR(32),
  admin_notification_detail TEXT,
  admin_notified_at TIMESTAMP WITH TIME ZONE,
  triggered_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
  created_by VARCHAR(32) DEFAULT 'system',
  FOREIGN KEY (subscription_id) REFERENCES proxy_airport_subscriptions(id),
  FOREIGN KEY (from_node_id) REFERENCES proxy_airport_nodes(id),
  FOREIGN KEY (to_node_id) REFERENCES proxy_airport_nodes(id)
);

-- 代理出口 IP 观测历史
CREATE TABLE proxy_exit_ip_observations (
  id BIGSERIAL PRIMARY KEY,
  proxy_node_id BIGINT,
  proxy_binding_id BIGINT,
  observed_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
  observed_exit_ip VARCHAR(64) NOT NULL,
  observed_exit_country VARCHAR(8),
  observed_exit_asn VARCHAR(32),
  observed_exit_isp VARCHAR(64),
  check_source VARCHAR(32) NOT NULL,
  raw_response JSONB,
  FOREIGN KEY (proxy_node_id) REFERENCES proxy_airport_nodes(id)
);

-- 授权槽位代理绑定
CREATE TABLE account_proxy_bindings (
  id BIGSERIAL PRIMARY KEY,
  account_id BIGINT NOT NULL,
  developer_app_id BIGINT NOT NULL,
  developer_app_api_id_snapshot INT NOT NULL,
  authorization_id BIGINT NOT NULL,
  session_role VARCHAR(32) NOT NULL,
  proxy_node_id BIGINT,
  proxy_id VARCHAR(128) NOT NULL,
  proxy_provider VARCHAR(32) NOT NULL,
  proxy_type VARCHAR(32) NOT NULL,
  proxy_host VARCHAR(128) NOT NULL,
  proxy_port INT NOT NULL,
  proxy_username VARCHAR(64),
  proxy_password_encrypted TEXT,
  proxy_country VARCHAR(8) NOT NULL,
  proxy_region VARCHAR(64),
  proxy_city VARCHAR(64),
  proxy_isp VARCHAR(64),
  proxy_asn VARCHAR(32),
  bound_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
  last_failover_at TIMESTAMP WITH TIME ZONE,
  binding_generation INT DEFAULT 1,
  last_used_at TIMESTAMP WITH TIME ZONE,
  last_health_check_at TIMESTAMP WITH TIME ZONE,
  ip_reputation_score FLOAT DEFAULT 100.0,
  reputation_check_json JSONB,
  is_active BOOLEAN DEFAULT TRUE,
  notes TEXT,
  created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
  updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
  -- partial unique index in migration: one active proxy binding per account + developer app + authorization slot
  FOREIGN KEY (proxy_node_id) REFERENCES proxy_airport_nodes(id)
);

-- 授权槽位环境绑定
CREATE TABLE account_environment_bindings (
  id BIGSERIAL PRIMARY KEY,
  account_id BIGINT NOT NULL,
  developer_app_id BIGINT NOT NULL,
  developer_app_api_id_snapshot INT NOT NULL,
  authorization_id BIGINT NOT NULL,
  session_role VARCHAR(32) NOT NULL,
  proxy_binding_id BIGINT NOT NULL,
  device_model VARCHAR(64) NOT NULL,
  system_version VARCHAR(32) NOT NULL,
  app_version VARCHAR(16) NOT NULL,
  platform VARCHAR(16) NOT NULL,
  lang_code VARCHAR(16) NOT NULL,
  system_lang_code VARCHAR(16) NOT NULL,
  lang_pack VARCHAR(16) NOT NULL,
  client_identity_key VARCHAR(160) NOT NULL,
  fingerprint_locked BOOLEAN DEFAULT TRUE,
  region_code VARCHAR(8) NOT NULL,
  region_consistency_checked BOOLEAN DEFAULT FALSE,
  region_consistency_errors JSONB,
  bound_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
  last_used_at TIMESTAMP WITH TIME ZONE,
  health_score FLOAT DEFAULT 100.0,
  notes TEXT,
  FOREIGN KEY (proxy_binding_id) REFERENCES account_proxy_bindings(id),
  UNIQUE (account_id, developer_app_id, authorization_id, session_role),
  UNIQUE (client_identity_key)
);

-- 授权槽位代理 warmup 状态
CREATE TABLE account_proxy_warmup_states (
  id BIGSERIAL PRIMARY KEY,
  account_id BIGINT NOT NULL,
  developer_app_id BIGINT NOT NULL,
  developer_app_api_id_snapshot INT NOT NULL,
  authorization_id BIGINT NOT NULL,
  session_role VARCHAR(32) NOT NULL,
  proxy_binding_id BIGINT NOT NULL,
  stage VARCHAR(32) NOT NULL,
  stage_started_at TIMESTAMP WITH TIME ZONE NOT NULL,
  first_action_at TIMESTAMP WITH TIME ZONE,
  daily_actions_count INT DEFAULT 0,
  daily_actions_reset_at TIMESTAMP WITH TIME ZONE,
  total_actions INT DEFAULT 0,
  reset_at TIMESTAMP WITH TIME ZONE,
  reset_reason TEXT,
  UNIQUE (account_id, developer_app_id, authorization_id, session_role, proxy_binding_id)
);

-- 目标机器人协议样本
CREATE TABLE bot_protocol_samples (
  id BIGSERIAL PRIMARY KEY,
  bot_username VARCHAR(64) NOT NULL,
  sample_type VARCHAR(32) NOT NULL,
  sample_hash VARCHAR(128) UNIQUE NOT NULL,
  schema_version VARCHAR(32) NOT NULL,
  structure_json JSONB NOT NULL,
  captured_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
  captured_by VARCHAR(64),
  pii_scrubbed BOOLEAN DEFAULT TRUE,
  is_active BOOLEAN DEFAULT TRUE
);

-- 账号级 search_join 执行锁
CREATE TABLE account_authorization_execution_locks (
  id BIGSERIAL PRIMARY KEY,
  account_id BIGINT NOT NULL,
  action_type VARCHAR(32) NOT NULL,
  action_id BIGINT NOT NULL,
  authorization_id BIGINT NOT NULL,
  session_role VARCHAR(32) NOT NULL,
  acquired_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
  expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
  released_at TIMESTAMP WITH TIME ZONE,
  UNIQUE (account_id, action_type)
);

-- IP 健康度历史
CREATE TABLE ip_reputation_history (
  id BIGSERIAL PRIMARY KEY,
  proxy_binding_id BIGINT NOT NULL,
  checked_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
  score FLOAT NOT NULL,
  source VARCHAR(32),
  raw_response JSONB,
  FOREIGN KEY (proxy_binding_id) REFERENCES account_proxy_bindings(id)
);

-- 任务搜索结果摘要（按动作维度）
CREATE TABLE search_join_action_stats (
  id BIGSERIAL PRIMARY KEY,
  action_id BIGINT UNIQUE NOT NULL,
  task_id BIGINT NOT NULL,
  account_id BIGINT NOT NULL,
  authorization_id BIGINT NOT NULL,
  session_role VARCHAR(32) NOT NULL,
  bot_username VARCHAR(64) NOT NULL,
  keyword_hash VARCHAR(128) NOT NULL,
  keyword_display_encrypted TEXT,
  business_region VARCHAR(8),
  account_locale VARCHAR(16),
  proxy_country VARCHAR(8),
  target_group_id BIGINT NOT NULL,
  target_position INT,
  total_results INT,
  pre_join_decoy_clicks JSONB,
  post_join_safe_navigation JSONB,
  post_join_policy VARCHAR(32) NOT NULL DEFAULT 'stay_joined',
  join_status VARCHAR(32),
  dwell_seconds INT,
  linked_task_status VARCHAR(32),
  linked_task_block_reason TEXT,
  error_code VARCHAR(64),
  created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- 搜索可见性观察快照（不作为 action 成功事实）
CREATE TABLE search_join_rank_observations (
  id BIGSERIAL PRIMARY KEY,
  task_id BIGINT NOT NULL,
  bot_username VARCHAR(64) NOT NULL,
  keyword_hash VARCHAR(128) NOT NULL,
  keyword_display_encrypted TEXT,
  target_group_id BIGINT NOT NULL,
  observed_position INT,
  total_results INT,
  observed_region VARCHAR(8),
  observation_source VARCHAR(32) NOT NULL,
  paid_keyword_ad_status VARCHAR(32),
  jisou_ecosystem_status VARCHAR(32),
  target_relevance_score FLOAT,
  target_content_health VARCHAR(32),
  observed_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- 搜索入群后的任务联动投递记录
CREATE TABLE search_join_linked_task_dispatches (
  id BIGSERIAL PRIMARY KEY,
  search_join_action_id BIGINT NOT NULL,
  source_task_id BIGINT NOT NULL,
  linked_task_id BIGINT NOT NULL,
  account_id BIGINT NOT NULL,
  target_group_id BIGINT NOT NULL,
  link_type VARCHAR(32) NOT NULL,
  status VARCHAR(32) NOT NULL,
  block_reason TEXT,
  can_send_checked_at TIMESTAMP WITH TIME ZONE,
  activation_not_before TIMESTAMP WITH TIME ZONE,
  ready_pool_item_id BIGINT,
  created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
  updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- 搜索节奏采样和跳过决策（防止 worker 重启或重复 planner tick 重新抽样）
CREATE TABLE search_join_pacing_decisions (
  id BIGSERIAL PRIMARY KEY,
  task_id BIGINT NOT NULL,
  decision_scope VARCHAR(32) NOT NULL,
  decision_key VARCHAR(160) NOT NULL,
  tenant_timezone VARCHAR(64) NOT NULL,
  local_date DATE,
  hour_start TIMESTAMP WITH TIME ZONE,
  account_id BIGINT,
  keyword_hash VARCHAR(128),
  decision VARCHAR(32) NOT NULL,
  sampled_value FLOAT,
  threshold FLOAT,
  scheduled_at TIMESTAMP WITH TIME ZONE,
  reason VARCHAR(64),
  created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
  UNIQUE (task_id, decision_scope, decision_key)
);

CREATE INDEX idx_search_join_stats_task ON search_join_action_stats(task_id);
CREATE INDEX idx_search_join_stats_keyword ON search_join_action_stats(keyword_hash);
CREATE INDEX idx_search_join_stats_authorization ON search_join_action_stats(authorization_id);
CREATE INDEX idx_search_join_pacing_task_date ON search_join_pacing_decisions(task_id, local_date);
```
 #

### 13.2 Action 扩展 `action.action_type` 新增 `search_join`。`action.payload` 结构：
```json
 { "account_id": 101, "authorization_id": 9001, "session_role": "primary", "execution_mode": "mtproto_userbot", "bot_username": "jisou", "keyword": {"text": "迪拜房产", "business_region": "AE", "account_locale": "zh-CN", "proxy_country": "SG", "lang": "zh", "decoy": false}, "target_group": {"operation_target_id": 123, "operation_target_version": 7, "peer_id": 1234567890, "username": "yourgroup"}, "phase": "main" }
```
 `action.result` 结构：
```json
 { "bot": "jisou", "keyword_hash": "sha256:...", "target_username": "yourgroup", "target_position": 3, "total_results": 25, "join_status": "success", "dwell_seconds": 95, "actual_dwell_seconds": 97, "pre_join_decoy_clicks": [{"button_hash": "sha256:...", "button_effect": "navigate_only", "position": 1, "joined": false}], "post_join_policy": "stay_joined", "paging_history": [1, 2, 3, 2, 4], "search_attempts": 1, "authorization_id": 9001, "session_role": "primary", "proxy_binding_id": 301, "proxy_node_id": 6001, "proxy_failover_event_id": null, "proxy_exit_ip_observation_id": 7001, "observed_exit_ip": "203.0.113.10", "observed_exit_country": "SG", "client_identity_key": "iphone_15_pro_ios_17_5_1_zh", "completed_at": "2026-07-02T20:35:12Z" }
```
 #

### 13.3 Task Stats 扩展 `task.stats` 增加：
```json
 { "search_join_stats": { "protocol_sample_status": {"jisou": "ready"}, "by_bot": { "jisou": {"actions_total": 1234, "success": 987, "target_not_in_results": 198, "failed": 49} }, "by_keyword_hash": { "sha256:...": {"display_name": "迪拜房产", "actions_total": 350, "success": 280, "avg_target_position": 3.2, "avg_dwell_seconds": 92, "pre_join_decoy_clicks": 126} }, "by_target": { "yourgroup": {"clicks_total": 987, "by_keyword_hash": {...}} }, "hourly_execution": { "bucket": "2026-07-03T20:00:00+08:00", "status": "catching_up", "goal": 0, "success_count": 7, "future_open_count": 3, "overdue_open_count": 0, "deficit": 0, "current_hour_rounds": 2, "started_rounds": 1, "max_actions_per_hour": 20, "last_planned_count": 5, "last_blockers": {} }, "authorization_environment_summary": { "ready_slots": 18, "missing_proxy_slots": 1, "missing_client_metadata_slots": 2, "api_id_mismatches": 0, "ios_slots": 16, "android_slots": 2, "lock_conflicts": 0 }, "exit_ip_health_summary": { "active_accounts": 50, "degraded_accounts": 2, "exit_ip_changed": 1, "egress_guard_failed": 0, "proxy_node_unreachable": 0, "airport_all_nodes_unavailable": 0 }, "proxy_failover_summary": {"attempted": 0, "succeeded": 0, "failed": 0, "paused_due_all_nodes_down": 0, "admin_notified": 0, "notification_failed": 0}, "button_effect_summary": {"navigate_only": 126, "join_candidate": 987, "external": 0, "unknown": 0}, "post_join_policy_summary": {"stay_joined": 987, "delayed_leave": 0, "leave_after_dwell": 0}, "recent_target_positions": [ {"checked_at": "2026-07-02T20:00:00Z", "keyword_hash": "sha256:...", "display_name": "迪拜房产", "position": 5}, {"checked_at": "2026-07-02T20:30:00Z", "keyword_hash": "sha256:...", "display_name": "迪拜房产", "position": 3} ] } }
```

`search_join_stats` 必须额外包含 `pacing_limits` 摘要，至少展示租户时区、本地日期、任务日计数、账号日 / 总上限命中、账号间隔天数命中、同账号同关键词日上限命中、日 / 小时 pacing 跳过次数和最近 pacing decision。详情页读取 `search_join_pacing_decisions` 时必须能解释“本小时没规划”到底是主动跳过、账号限额、关键词限额、任务日限额还是全局风控上限。


## 14. 前端集成

### 14.1 任务类型枚举

`TaskTypeValue` 增加 `search_join_group`。`TASK_TYPE_OPTIONS` 增加：

```typescript
{ value: "search_join_group", label: "搜索目标群点击任务" }
```

任务类型筛选、规则集适用任务类型、运营方案生成任务类型、任务详情标题和操作手册都必须展示同一名称。

### 14.2 创建向导

5 步向导新增“搜索目标群点击任务”分支：

| 步骤 | 控件 |
| --- | --- |
| 1. 基础信息 | 任务名称、目标群聊、结束时间 |
| 2. 搜索机器人 | 多选搜索机器人；首版仅开放 `@searchbot` |
| 3. 关键词列表 | 表格录入 `text / business_region / account_locale / proxy_country / lang / weight / decoy`，支持 CSV 导入；decoy 占比提示且低于 30% 阻断 |
| 4. 运营真实性配置 | warmup、行为真实化、搜索节奏与账号上限、小时 / 天跳过、小时 / 天抖动、翻页、反聚类配置，默认折叠高级项 |
| 5. 账号 + 预览 | 展示 eligible、protocol sample、observed exit IP、proxy egress guard、API ID / session 一致性、missing proxy、missing client metadata、warmup、proxy_dead、account lock conflict 和 keyword matrix mismatch |

预检返回：

```json
{
  "search_join_precheck": {
    "eligible_accounts": 5,
    "protocol_sample_status": {"jisou": "ready"},
    "missing_environment": [{"account_id": 105, "authorization_id": 9003, "session_role": "standby_1", "missing": "client_metadata"}],
    "exit_ip_health": {"ready_bindings": 10, "exit_ip_changed": 0},
    "decoy_ratio": 0.35,
    "estimated_daily_actions": 25,
    "warnings": ["账号 102 已养号 28 天，建议再养 2 天"]
  }
}
```

### 14.3 任务列表

任务列表额外展示：

```text
[搜索目标群点击任务] 迪拜房产群搜索目标点击
累计入群 987 / 1500
目标群平均排名 P3.2（上升 2 位）
今日入群转化率 47%
AI 活跃群联动 126 个账号待冷却 / 64 个已进入 ready pool
状态：运行中
```

筛选条件：任务类型支持按 `search_join_group` 筛选。

### 14.4 任务详情

新增 Tab：搜索入群统计。

| 子模块 | 内容 |
| --- | --- |
| 累计入群 | 总成功数 / 失败数 / 跳过数；按机器人、关键词、目标群分别展开 |
| 排名轨迹 | 按关键词展示目标群在目标机器人结果中的位置变化；标明观察来源、地区、是否存在付费关键词广告和极搜生态状态 |
| 行为漏斗 | 搜索次数 -> 目标群出现次数 -> 加入成功次数 -> 停留合格次数 -> 后续任务 ready 数 |
| 小时执行 | 当前小时轮数、已启动轮次、成功 / 未来待执行 / 过期待执行 / 缺口、`catching_up / met / blocked / missed` 状态和阻塞原因 |
| IP / 客户端元数据状态 | 每个账号授权槽位的 observed exit IP、IP 健康分、客户端元数据、warmup 阶段、账号锁冲突和最近失败原因 |
| Decoy 分布 | decoy vs target 关键词 action 数对比 |
| 入群前浏览 | pre-join decoy click 次数、停留时长、是否误加入非目标群（首版应始终为 false） |
| 入群后安全浏览 | post-join safe navigation 次数、停留时长、是否误触发非目标加入（首版必须为 false） |
| 入群后策略 | `stay_joined / delayed_leave / leave_after_dwell` 分布和 24h / 7d 留存状态 |
| 后续任务联动 | AI 活跃群 / 转发监听 / 频道评论的 linked 状态、冷却剩余、can_send 复检、ready pool 加入数和阻塞原因 |
| 调研规则解释 | 展示目标资料相关性、内容健康、开放活跃、极搜生态状态、付费关键词广告状态和反作弊风险，解释为什么排名变化可能与搜索入群动作不完全同步 |

### 14.5 风控告警

风控中心增加“搜索入群”维度：

| 告警类型 | 触发条件 | 处置 |
| --- | --- | --- |
| `proxy_dead` | IP 健康分 < 60 | 自动暂停该账号任务 |
| `exit_ip_changed` | 绑定节点最近出口 IP 与历史稳定出口不一致 | 暂停该授权槽位并重置授权槽位 warmup |
| `proxy_airport_subscription_failed` | 订阅拉取失败、格式识别失败、节点为空或只解析出套餐/流量伪节点 | 暂停新绑定，保留既有健康节点 |
| `proxy_node_unreachable` | 当前绑定机场节点 TCP / TLS / 代理认证 / 出口探测不通 | 自动尝试 `switch_to_next_healthy_node`，写 failover 审计并重置 warmup |
| `airport_all_nodes_unavailable` | 同一订阅下所有候选节点均不可达、超容量或未通过出口探测 | 暂停任务或跳过 action，不发送搜索、不点击、不 join，并通过租户 Bot 向全部管理员 Chat ID 推送告警 |
| `admin_notification_failed` | 全节点不可用通知发送失败、Bot Token 缺失或管理员 Chat ID 缺失 | 仍保持任务暂停 / 跳过，并在风控中心展示通知失败原因 |
| `authorization_proxy_conflict` | 同账号主/备用授权复用同一代理节点、同一授权槽位存在多个 active 代理，或 observed exit IP 与绑定不一致 | 阻断任务创建 / 执行并要求收敛为授权槽位唯一代理 |
| `fingerprint_anomaly` | 设备指纹异常关联 | 人工审核 |
| `fingerprint_reused_in_same_account` | 同账号主/备用授权复用同一指纹组合 | 阻断任务创建 |
| `protocol_sample_missing` | 目标机器人真实样本未采集或过期 | 阻断真实灰度，只允许 fixture |
| `proxy_egress_guard_failed` | MTProto 连接未证明走授权槽位绑定代理，或 observed exit IP 与授权槽位绑定不一致 | 阻断真实点击，暂停该授权槽位 |
| `api_id_client_metadata_mismatch` | 授权槽位登录 API ID / session / 运行时 API ID / 客户端元数据不一致 | 阻断该槽位，要求重新绑定授权资产 |
| `button_effect_unknown` | 样本无法判断按钮是否外跳、入群或触发验证 | 阻断该按钮点击，等待样本确认 |
| `account_authorization_lock_conflict` | 同账号已有 search_join action 执行中 | 跳过新 action 并上报统计 |
| `keyword_plaintext_log_detected` | 日志或 stats 出现关键词明文 | critical，阻断发布 |
| `post_join_fast_leave_rate` | 24h 内退出比例异常 | 暂停退出策略并人工复核 |
| `bot_blocked` | 账号被目标机器人 / SOSO 拉黑 | 换号 + 告警 |
| `target_position_degrading` | 目标群排名连续下滑 | 提醒运营调整关键词、强度或付费策略 |
| `bot_response_changed` | 机器人回复结构无法解析 | 自动暂停相关任务，等待适配 |

## 15. 风控中心集成

风控中心已有的小时 / 日上限、账号冷却规则统一适用：

| 风控项 | 复用现有规则 |
| --- | --- |
| 账号小时上限 | `risk_control.account_hourly_limit` |
| 账号日上限 | `risk_control.account_daily_limit` |
| 账号冷却 | `risk_control.account_cooldown_minutes` |
| 规则集 | `rule_sets.task_types` 新增 `search_join_group` |

新增风控点：

任务内 `pacing_config` 是运营对单个 search_join 任务的更细限制；风控中心 / 规则集是租户或账号级全局上限。两者同时存在时，Planner 使用“更严格的正数限制”：任一侧为 `0` 只表示该侧不设上限，不得覆盖另一侧的正数限制。命中全局上限写 `risk_control_limit_reached`，命中任务内上限写 `pacing_limit_reached`，详情页必须分开展示。

| 风控项 | 默认值 | 说明 |
| --- | --- | --- |
| `search_join.bot_daily_limit_per_account` | 5 | 单账号对单机器人每天上限 |
| `search_join.keyword_daily_limit_per_account` | 2 | 单账号对单关键词每天上限 |
| `search_join.ip_daily_limit` | 50 | 单 IP 每天对所有机器人请求上限 |
| `search_join.fingerprint_account_max` | 1 | 同指纹最多绑定 1 个账号 |
| `search_join.auth_slot_client_metadata_required` | true | 主/备用授权槽位都必须绑定完整 MTProto 客户端元数据 |
| `search_join.protocol_sample_required` | true | 真实机器人协议样本缺失时阻断灰度 |
| `search_join.exit_ip_observation_required` | true | `airport_clash` 节点必须有真实出口 IP 观测 |
| `search_join.proxy_egress_guard_required` | true | 每次真实点击前证明 MTProto 连接走授权槽位绑定代理 |
| `search_join.allow_direct_egress_fallback` | false | 代理不可用时 fail closed，不允许直连 |
| `search_join.unique_proxy_per_authorization_required` | true | 每个账号 + TG 开发者应用 + 授权槽位必须绑定唯一代理出口，主 / 备用授权不得复用同一代理节点 |
| `search_join.notify_admin_on_all_nodes_down` | true | 机场订阅全节点不可用时通过租户 Bot 推送给配置的管理员 Chat ID |
| `search_join.hourly_execution_model_enabled` | true | 搜索入群按自然小时桶统计成功、future open、overdue open 和缺口 |
| `search_join.hourly_min_successful_joins_default` | 0 | 默认不强制硬目标；配置大于 0 时按缺口追规划 |
| `search_join.api_id_session_metadata_match_required` | true | 授权资产 API ID / session / 运行时 API ID / 客户端元数据必须一致 |
| `search_join.decoy_requires_navigate_only` | true | 入群前 decoy 浏览只允许 `button_effect=navigate_only` |
| `search_join.same_account_concurrency` | 1 | 同账号 search_join action 互斥 |
| `search_join.keyword_plaintext_log_allowed` | false | 日志、stats 和 action result 不允许关键词明文 |
| `search_join.pre_join_decoy_click_probability` | 0.35 | 入群前打开非目标群 / 频道结果的默认概率 |
| `search_join.decoy_join_enabled` | false | 首版默认不加入非目标群 / 频道，只浏览 |
| `search_join.min_decoy_ratio` | 0.3 | 低于阈值阻断任务创建 |

## 16. 监控与告警

### 16.1 关键指标

|指标|阈值|告警级别|
|---|---|---|
|任务级 click/曝光比 (入群转化率)|< 20% 或 > 70%|warning|
|任务级单 IP 请求成功率|< 80%|warning|
|授权槽位级 IP 健康分|< 60|auto_pause|
|授权槽位级出口 IP 变化|任一|auto_pause|
|代理出口防泄漏失败|任一|auto_pause|
|API ID / session / 客户端元数据不一致|任一|block_start|
|button effect unknown|任一真实点击目标|block_action|
|授权槽位级设备指纹异常|任一|manual_review|
|同账号主 / 备用授权复用代理节点或设备指纹，或同一授权槽位出现多个 active 代理 / 多出口 IP|任一|block_create|
|协议样本缺失或解析失败|任一|block_start|
|账号执行锁冲突率|> 5%|warning|
|关键词明文日志|任一|critical|
|入群后 24h 内快速退出比例|> 5%|warning|
|目标机器人/SOSO 拒绝次数（24h）|> 10|critical|
|FloodWait 触发（账号级）|单次 > 3600s|warning|
|任务级目标群排名连续下滑|3 次抽样都比上次差|info|
|
 #

### 16.2 告警渠道 复用现有告警链路：风控中心告警 → webhook / 邮件 / 站内信。新增专属告警类型：- `search_join.proxy_degraded` - `search_join.proxy_airport_subscription_failed` - `search_join.proxy_node_unreachable` - `search_join.airport_all_nodes_unavailable` - `search_join.admin_notification_failed` - `search_join.hourly_execution_blocked` - `search_join.hourly_execution_missed` - `search_join.exit_ip_changed` - `search_join.proxy_egress_guard_failed` - `search_join.api_id_client_metadata_mismatch` - `search_join.button_effect_unknown` - `search_join.proxy_node_reused_in_same_account` - `search_join.fingerprint_reused_in_same_account` - `search_join.protocol_sample_missing` - `search_join.account_authorization_lock_conflict` - `search_join.keyword_plaintext_log_detected` - `search_join.post_join_fast_leave_rate` - `search_join.bot_response_changed`（机器人回复格式变化） - `search_join.target_group_missing`（目标群从目标机器人索引消失） - `search_join.suspicious_block`（账号疑似被风控）

## 17. 灰度计划

### 17.0 阶段零：真实样本与出口验证（3-5 天）

- 用 1-2 个人工确认可用账号采集首版目标机器人的 `/start`、搜索、翻页、目标匹配、callback / URL 和异常响应样本。
- 样本只保存结构、字段路径、hash 和必要按钮类型，不保存成员信息、消息正文或其他 PII。
- `airport_clash` 节点必须完成真实出口 IP 观测，记录 `observed_exit_ip / observed_exit_country / asn / isp / exit_ip_stability_score`。
- 阶段零未通过时，不允许进入真实灰度；开发只能实现 parser fixture、预检和管理界面。

### 17.1 阶段一：环境准备（1 周）

- 采购 50 个独享静态住宅 IP（多国分散：US 20 + DE 15 + SG 10 + JP 5），或接入一条合规可用的机场 Clash 订阅并解析为节点池。
- 建立 iOS 80% / Android 20% 的设备指纹规则集，运行时生成并写入 `account_environment_bindings`；不引入独立 `device_fingerprints` 主表。
- 主授权和备用授权都补齐完整 iOS 优先设备指纹，并确保同账号不同槽位不复用指纹组合。
- 实现代理供应商抽象层并接入一家供应商或 `airport_clash` provider。
- 实现授权槽位级账号环境栈绑定、组合上限校验和 `fingerprint_combo_history` 审计摘要。
- 实现账号级 `search_join` 执行互斥锁、关键词 hash 存储和无明文日志检查。
- 完成单元测试和集成测试。

### 17.2 阶段二：养号（30 天） - 50 个真实 TG 账号（人工注册） - 每个账号的主 / 备用授权槽位各绑定 1 个住宅 IP 或 Clash 节点（注册 IP、养号 IP、任务 IP 尽量连续，实际以授权槽位 observed exit IP 为准；每个授权槽位长期固定自身出口） - 主/备用授权槽位各绑 1 个不同完整客户端元数据，优先 iOS 风格 - 50 个账号**只做日常活跃**（每天发几条无关消息、看几个频道），不跑入群任务 - 让 TG 自身画像、目标机器人画像都稳定下来

### 17.3 阶段三：灰度（2-4 周） - 选取 5-10 个账号、5-10 个目标关键词、1 个目标群 - 创建第一个 `search_join_group` 任务 - 每个账号每天 1-2 个目标搜索 - 监控指标：- 任务成功率 ≥ 80% - IP 健康分和出口 IP 稳定性无明显下降 - 目标机器人拒绝次数 ≤ 5 - 账号无异常 - 账号执行锁冲突率 ≤ 5% - 关键词明文日志 0 次 - 入群后快速退出比例 ≤ 5% - 目标群排名变化只作为观察指标，不作为灰度通过硬条件 通过条件：- 7 天灰度稳定 → 进入阶段四 - 不通过 → 调整策略（关键词、账号数量、强度、代理出口或后加入群策略）后重试

### 17.4 阶段四：扩量（持续） - 50 账号全部启用（按 warmup → low → steady 阶段递进） - 增加目标群数量 - 持续监控排名变化

### 17.5 阶段五：4G 移动代理（可选，第二季度） - 评估 4G 移动代理供应商 - 10-20 个现金牛账号升级到 4G 移动 - 监控封号率

## 18. 风险与合规

### 18.1 业务定性 本任务的最终合规边界由用户拍板。技术方案只保证：- **运营真实性可控**：行为画像、IP 信誉、授权槽位代理出口和授权槽位设备指纹环境栈都在阈值内 - **业务可灰度**：环境准备 → 养号 → 小范围灰度 → 扩量 - **异常可熔断**：机器人加验证码、IP 健康分下降、账号被封等场景自动暂停任务 + 告警 法务 / 合规边界由用户自担。#

### 18.2 平台风险

|风险|等级|应对|
|---|---|---|
|目标机器人/SOSO 升级反作弊|高|监控告警 + 自动暂停 + 人工验证后恢复|
|TG 平台封禁 userbot|中|多账号矩阵 + 灰度观察|
|目标机器人向 TG 投诉大量 IP|中|IP 健康度监控 + 自动换 IP|
|目标群被目标机器人降权|低|分散关键词 + 稀释 入群转化率|
|MTProto 客户端元数据被误解为真实设备|高|前端、PRD 和执行日志必须标明 `execution_mode=mtproto_userbot`；真机方案另起专项|
|同一机场节点真实出口 IP 漂移|高|以 observed exit IP 为准，漂移即暂停并重置 warmup|
|同账号主/备授权并发执行|高|账号级执行锁硬阻断，备用只做 failover|
|目标机器人协议样本过期|高|样本解析失败即暂停，不允许 silent fallback|
|
 #

### 18.3 账号风险

|风险|等级|应对|
|---|---|---|
|账号被举报封禁|中|养号前置 + 账号画像完整|
|账号被冻结（FROZEN_METHOD_INVALID）|中|FloodWait 监控 + 实时下线|
|账号被目标机器人拉黑|高|IP 健康度 + 行为真实性|
|账号 session 泄露|高|加密存储 + 内存解密 + 审计日志|
|
 #

### 18.4 数据风险

|风险|等级|应对|
|---|---|---|
|worker 数据库被攻击|高|字段级加密 + 权限隔离 + 审计|
|IP 池被竞品窃取|中|字段级加密|
|关键词列表被泄露|低|不在日志中打印关键词明文|
|action/stats 泄露关键词明文|中|只存 `keyword_hash` 和必要的加密展示字段，日志扫描命中即 critical|
|
 #

### 18.5 合规边界（再次强调） - 不向中国大陆运营主 worker - 不在欧盟/美国部署主数据中心 - 不采集目标机器人/SOSO 返回的群成员信息、消息内容等 PII - 任务日志只存必要字段（action_id、keyword_hash、bot_username、status、timestamp） - 数据保留期 90 天，到期自动清理

## 19. 实施优先级

### 第一阶段（最小可用，2-3 周）

后端：

- `schemas/task_center.py` 扩展 `TaskTypeValue`、`SearchJoinGroupConfig`。
- 数据库 migration 新增 `proxy_airport_subscriptions`、`proxy_airport_nodes`、`proxy_node_failover_events`、`fingerprint_combo_history`、`account_proxy_bindings`、`account_environment_bindings`、`account_proxy_warmup_states`、`ip_reputation_history`、`search_join_action_stats` 和 `search_join_pacing_decisions`。
- 数据库 migration 同步新增 `bot_protocol_samples`、`proxy_exit_ip_observations`、`account_authorization_execution_locks`。
- 代理供应商抽象 + 一家供应商或 `airport_clash` 实现。
- 机场订阅加密存储，自动识别 Base64 URI 列表 / Clash YAML / JSON，过滤套餐和流量伪节点，解析 `anytls` / `trojan` 等节点，完成真实出口 IP 观测、节点健康检查、容量分配、随机固定绑定和故障切换审计。
- 全订阅节点不可用时复用租户 Bot 通知链路，向 `Tenant.admin_chat_id` 的全部管理员 Chat ID 推送脱敏告警；通知失败写 `admin_notification_failed`，但任务仍保持暂停 / 跳过。
- 搜索入群小时执行量模型：复用 AI 活跃群的自然小时桶、future open、overdue open 和缺口统计思想，新增 `search_join_hourly_*` stats、24 小时曲线、每轮 action 上限、每小时硬上限和可选小时成功硬目标。
- 入群后任务联动：新增 `post_join_task_links` 配置、linked ready pool 投递、AI 活跃群冷却/可发言复检、新成员占比限制和联动阻塞原因展示。
- 调研规则投影：新增 `search_join_rank_observations` 和 `search_join_linked_task_dispatches`，把排名观察、极搜生态、付费关键词广告、目标资料相关性、内容健康和后续任务联动与 action 事实分开存储。
- 设备指纹规则集、运行时生成、授权槽位级镜像绑定和组合上限校验。
- API ID / session / 客户端元数据一致性校验，防止授权资产和运行时客户端画像错配。
- `proxy_egress_guard`，确保 MTProto 搜索、翻页、callback、Telegram 内部 URL resolve 和 join 都走绑定代理且不直连回退。
- 授权槽位级账号环境栈绑定 + Planner / Executor 双重校验。
- 账号级 `search_join` 执行锁，确保主/备用授权槽位不会同账号并发。
- 目标机器人样本采集 CLI / 管理入口，生成 parser fixture 后才允许真实灰度。
- 关键词存储与日志只落 `keyword_hash`，明文仅允许加密展示字段。
- Executor `search_join_group.py` 完整实现首版 `@searchbot` 协议。
- IP 健康度监控（每天定时）。
- 风控中心新增 search_join 维度。

前端：

- `TASK_TYPE_OPTIONS` 增加 `search_join_group`。
- Wizard 新增搜索目标群点击任务分支。
- 任务列表 / 详情 stats 输出。
- 风控告警类型展示。

测试：

- mock 目标机器人 / SOSO 的 fixture 测试。
- 协议样本缺失、样本解析、button type / button effect 分类、授权槽位环境栈缺失、warmup、decoy 比例、proxy_dead、exit_ip_changed、proxy_egress_guard_failed、authorization_proxy_conflict、proxy_node_unreachable、airport_all_nodes_unavailable、admin_notification_failed、search_join_hourly_execution、api_id_client_metadata_mismatch、fingerprint_invalid、订阅格式识别失败、Clash / Base64 URI / JSON 订阅解析失败、授权槽位多 active 代理 / 多出口 IP 和主备复用元数据的单元测试。
- 同账号执行锁、备用 failover、observed exit IP 观测、防直连回退、decoy 只点 `navigate_only` 和关键词明文日志扫描测试。
- pre-join decoy click 默认只浏览不加入、结果写入 `pre_join_decoy_clicks` 的单元测试。
- post-join safe navigation 默认只浏览不加入、总非目标安全浏览次数不超过 3、结果写入 `post_join_safe_navigation` 的单元测试。
- search_join 专属节奏与账号上限测试：租户时区日界线、账号日 / 总上限、账号间隔天数、同账号同关键词日上限、任务日上限、日 / 小时跳过采样持久化、小时 / 天抖动不突破硬上限、运行中编辑只影响未来规划。
- `post_join_policy=stay_joined` 默认策略和 24h / 7d 留存状态回写测试。
- 搜索入群成功后联动 AI 活跃群 ready pool 的测试：冷却未到不入池、can_send 复检失败不入池、新成员占比超限不入池、任务暂停不入池、满足条件后进入 ready pool。
- 排名观察和效果归因测试：`search_join_rank_observations` 不计入 action success，付费广告/流量联盟/内容健康字段只影响解释和 warning，不改写搜索入群成功数。
- 真实账号 × 1-2 个真实关键词 × 1 个真实目标群 × 7 天灰度。

### 第二阶段（扩量 + 报表，4-6 周）

- 多搜索机器人支持（@soso、@smss、@CJSY）。
- 排名轨迹 ECharts 报表。
- 运营数据页 search_join 汇总。
- 第二个代理供应商接入（容灾）。
- 行为配置模板化，允许运营保存运营真实性预设。

### 第三阶段（升级，第二季度）

- 4G 移动代理接入。
- AI 生成 decoy 关键词。
- 行为模式 ML 调优，基于历史数据自动调整 anti_detection 参数。
- 跨任务学习：账号在一个任务里的失败经验反哺其他任务。

## 20. 验收口径

### 20.1 后端验收

- 旧任务（5 类主任务）行为不变。
- `execution_mode` 首版固定为 `mtproto_userbot`，前端和 API 返回不得暗示真实手机 UI 自动化。
- 未采集真实目标机器人协议样本时，创建启动必须阻断真实灰度，只允许 parser fixture / precheck。
- 真实协议样本必须区分 `callback_data / telegram_url / external_http_url` 和 `button_effect`；外部 HTTP URL、unknown effect、可能直接入群的非目标 decoy button 都不得被默认点击。
- 新建 `search_join_group` 任务时，账号池里无授权槽位环境绑定的账号必须在预检中可见；创建并启动时无环境槽位不得进入主执行。
- 主授权和备用授权都必须绑定不同完整客户端元数据和不同代理节点；同账号主 / 备用授权复用代理节点或元数据组合、同一授权槽位出现多个 active 代理 / 多出口 IP 时，任务创建必须被阻断。
- `airport_clash` provider 必须能加密保存订阅 URL，自动识别 Base64 URI 列表 / Clash YAML / JSON，过滤套餐和流量伪节点，解析节点，按默认容量和单节点覆盖控制“每个节点多少授权槽位”，观测真实出口 IP、健康检查并把随机节点固定到授权槽位；订阅失败、节点为空、出口 IP 漂移不得静默 fallback。
- 当前绑定节点不通时，必须按 `switch_to_next_healthy_node` 在同订阅内选择下一个健康且未超容量的节点，写 `proxy_node_failover_events`，并让新代理绑定重新 warmup；如果全订阅节点都不可用，必须 `skipped` / 暂停并写 `airport_all_nodes_unavailable`，不得搜索、点击或加入。
- 全订阅节点不可用时，必须通过租户 Bot 向全部配置的管理员 Chat ID 推送脱敏告警；Bot Token 或管理员 Chat ID 未配置、发送失败时必须写 `admin_notification_failed` 和审计，但不得恢复执行或改走直连。
- 搜索入群小时执行量必须按租户时区自然小时统计；`success_current_hour` 只统计真实成功 `search_join`，`future_open_current_hour` 只统计未来待执行，过期待执行进入 `overdue_open_count`，不得把 skipped / failed / 代理不可用 / decoy-only 浏览计入成功或覆盖缺口。
- 每次真实搜索、翻页、callback、Telegram 内部 URL resolve 和 join 前必须通过 `proxy_egress_guard`；代理失败、直连风险或出口 IP 与绑定不一致时必须 `skipped`，不得继续执行。
- 授权槽位登录 API ID、session 文件、运行时 API ID / API hash 和客户端元数据必须一致；不一致时必须 `skipped` 并写 `api_id_client_metadata_mismatch`。
- 同一 `account_id` 同时只允许 1 个 `search_join` action 执行；锁冲突必须以 `account_authorization_lock_conflict` 跳过并进入 stats。
- `search_join_group` 的 planner、dispatcher、executor 实时路径不得调用 AI Gateway、AI Provider 或 `task_center/ai_generator.py`；测试必须能在未配置任何 AI 供应商时创建 pacing decision、规划或跳过 search_join action，并断言 pacing/random decision 没有触发 AI 调用。LLM 只允许在离线配置建议、关键词生成、目标相关性解释和复盘分析接口中出现。
- `action.result`、stats、worker 日志和告警不得保存关键词明文，必须使用 `keyword_hash`；展示明文只能走加密展示字段。
- decoy 关键词占比 < 30% 时任务创建被拒绝。
- 每个主执行 action 必须经过 §8.3 的完整 8 步链路；warmup / decoy 路径可按 §8.2 放宽，但必须写清 lifecycle 和 skip reason。
- 授权槽位未绑唯一代理、同一授权槽位存在多 active 代理、授权槽位未绑客户端元数据时，Executor 返回 `skipped`，并写入 `lifecycle_phase=needs_proxy/needs_client_metadata/authorization_proxy_conflict` 与对应 `skip_reason`，不得返回新 action status。
- 入群前非目标点击默认只打开/停留/返回，不加入非目标群 / 频道；每次点击必须写入 `action.result.pre_join_decoy_clicks`。
- 入群后安全浏览默认只打开/停留/返回，不加入非目标群 / 频道；每次点击必须写入 `action.result.post_join_safe_navigation`。
- 入群前和入群后的非目标安全浏览总数不得超过 3；只允许 `button_effect=navigate_only`；`join_candidate / external / unknown` 都必须跳过。
- 默认 `post_join_policy=stay_joined`；任何退出策略都必须记录策略、执行时间、审批原因和留存结果。
- 搜索入群成功不得直接等同于 AI 活跃群 ready；必须在留存观察、冷却、`can_send` 复检、新成员占比和任务状态全部满足后，才能把账号追加到 linked task ready pool。
- 排名观察必须写入独立 `search_join_rank_observations`，不得计入 action success，也不得把付费广告、流量联盟或内容健康变化自动归因为 search_join action。
- IP 健康分 < 60 时，该账号所有搜索入群 action 自动标记 `proxy_dead` 并暂停相关账号任务。
- `proxy_airport_subscriptions`、`proxy_airport_nodes`、`account_proxy_bindings`、`account_environment_bindings`、`fingerprint_combo_history` 的新增、禁用、解绑和修订必须写审计。
- 系统设置 Clash 配置验收必须区分订阅保存、订阅解析、节点同步、节点健康检查和授权槽位绑定五个阶段；任一阶段失败都必须有独立状态和错误原因，不能用保存成功覆盖后续失败。
- 授权指纹验收必须证明配置指纹、远端观测指纹和一致性状态同时存在；保存配置后未重登时必须进入 `pending_effect`，远端快照缺字段时必须进入 `unobservable` 并展示缺失字段，不能显示 `observed_matched` 或“远端已更新”。
- session 文件加密存储，磁盘不存明文。
- FloodWait 累计 > 3600s 时账号自动 cooldown 4h。
- `bot_username=jisou` 的灰度任务能被目标机器人接收且不立即拒绝。

### 20.2 前端验收

- 创建搜索目标群点击任务时能选择机器人、导入关键词、配置运营真实性参数、仅 search_join 生效的节奏与账号上限、预览账号环境状态。
- 创建 / 编辑页必须只在 `search_join_group` 下展示每账号总上限、每日上限、间隔天数、同账号同关键词日上限、任务日上限、小时 / 天跳过、单 action 跳过、小时 / 天抖动；其他任务类型不得出现这些字段。
- 运行中编辑节奏与账号上限时，页面必须提示只影响未来规划；如选择重排 pending action，必须展示影响预览并要求填写审计原因。
- 任务列表展示任务类型、入群转化率、平均排名、后续任务联动摘要和状态。
- 任务详情展示累计入群、排名轨迹、行为漏斗、IP / 客户端元数据状态、协议样本状态、账号锁冲突、入群后安全浏览、入群后策略、后续任务联动、调研规则解释和 Decoy 分布。
- 任务详情必须能解释“本小时没执行”的原因：日 / 小时跳过、账号日上限、账号总上限、账号间隔天数、同账号同关键词日上限、任务日上限、全局风控上限、代理不可用、缺指纹、协议样本缺失和 worker backlog 需要分开展示。
- 风控中心能查看 search_join 告警。
- 预检返回的警告和缺失环境清晰可见。

### 20.3 灰度验收（5-10 账号 × 7 天）

- 任务成功率 ≥ 80%。
- 0 个账号被封。
- 0 个账号被目标机器人拉黑。
- IP 健康分和出口 IP 稳定性无明显下降。
- 账号执行锁冲突率 ≤ 5%。
- 关键词明文日志 0 次。
- 入群后 24h 内快速退出比例 ≤ 5%。
- 目标机器人拒绝请求次数 ≤ 5。
- 目标群排名变化只作为运营观察指标，不作为系统验收通过条件。
- 至少一个 linked AI 活跃群任务能看到搜索入群带来的账号处于 `linked_task_ready_pending / linked_task_ready_active / linked_task_blocked` 之一，且阻塞原因可解释。

## 21. Product Design Complete 自检

| 检查项 | 结论 |
| --- | --- |
| 原始需求覆盖 | 已覆盖“搜索目标群点击任务”和“合并到 PRD” |
| 用户补充细节覆盖 | 已覆盖主/备用独立客户端元数据、机场订阅自动代理、订阅格式识别、每节点容量配置、随机固定节点、节点不通切换下一个健康节点、全订阅不通停止操作、Bot 管理员通知、小时执行数量类似 AI 活群模型、搜索节奏与账号上限、小时 / 天跳过、小时 / 天抖动、实时 pacing / random 不调用 LLM、入群前非目标浏览、入群后安全浏览、授权指纹配置与远端观测分离和 AI 活跃群等后续任务联动 |
| 功能设计 | 已定义任务类型、机器人、关键词、目标群、公开排名规则推导、执行模式、环境栈、warmup、小时执行量、搜索节奏与账号上限、执行链路、入群后策略、后续任务联动和灰度 |
| 前端状态 | 已定义创建向导、预检、运行中编辑影响预览、任务列表、任务详情、小时执行状态、节奏与账号上限状态、协议样本状态、出口 IP 状态、Clash 保存 / 同步状态、授权指纹配置 / 远端观测状态和风控告警 |
| 后端 / API / worker | 已定义 schema、planner、executor、parser、stats、worker 边界、执行锁和异常处理 |
| 数据流转 | 已定义新增表、Action payload/result、Task stats、OperationTarget 引用、协议样本、出口 IP 观测、节点容量、故障切换审计、管理员通知状态、小时执行 stats、pacing decision、排名观察快照和 linked task ready pool 投递 |
| 权限安全 | 已要求任务创建权限、代理管理权限、审计、session 加密、环境栈硬校验和关键词明文禁止落日志 |
| 边界场景 | 已覆盖 warmup、proxy_dead、proxy_node_unreachable、airport_all_nodes_unavailable、admin_notification_failed、search_join_hourly_blocked、exit_ip_changed、client_metadata_pending_effect、client_metadata_observed_mismatch、client_metadata_unobservable、bot_blocked、FloodWait、目标缺失、join approval / captcha 和机器人结构变化 |
| QA 验收 | 已定义后端、前端、灰度三层验收口径 |
| 仍需用户拍板 | 首版真实目标机器人账号、灰度目标群、关键词样本、真实样本采集账号和灰度账号范围 |

### 21.1 完整梳理缺口处理矩阵

| 复核项 | 缺口 / 易混点 | PRD 收口 |
| --- | --- | --- |
| 任务命名 | “搜索自动入群”容易让运营误解为只要未入群才执行 | 用户可见统一为“搜索目标群点击任务”；账号已在群内也必须执行搜索、目标点击 / 确认，成功事实写 `membership_observed` |
| 找不到目标 | 只写“翻页匹配”不足以说明失败终止条件 | 默认最多翻 70 页；找满仍未命中写 `target_not_in_results` 和 `pages_exhausted=true`，该搜索目标群点击任务自动停止 |
| 非目标浏览 | “假装点击其他结果”容易被实现成全点或误加入 | 非目标浏览只允许 `button_effect=navigate_only`，不得加入、关注、外跳或点击 `join_candidate/external/unknown`；pre + post 总数默认不超过 3，并写入 action result |
| 节奏配置 | 每账号总上限、每日上限、间隔天数、小时 / 天跳过和抖动可能被通用任务误用 | `pacing_config` 是 search_join 专属；创建 / 编辑页只在 `search_join_group` 展示这些字段；planner 先执行账号 / 关键词 / 任务日限额，再做小时补量 |
| 随机决策 | 实时 pacing / random decision 如果调 LLM 会不可复现且增加失败面 | 实时路径禁止调用 AI Gateway、AI Provider 和 `task_center/ai_generator.py`；只允许规则、配置、seeded random 和持久化 `search_join_pacing_decisions` |
| Clash 配置 | 系统设置保存订阅地址容易被误报为代理池可用 | 系统设置只保存一个全局 Clash 订阅；保存、解析、同步、健康检查和授权槽位绑定分阶段展示；健康节点数为 0 或同步失败不能作为候选代理池 |
| 账号代理 | 单账号代理配置容易被放回系统设置或只按账号粒度保存 | 账号代理在“账号面具 > 账号代理”配置，粒度为 `account_id + developer_app_id/api_id + authorization_id/session_role`；同一槽位只允许一个 active 代理和一个 observed exit IP |
| 授权指纹 | “修改指纹配置”容易被误报成远端授权设备已立即改变 | 授权指纹在“账号面具 > 授权指纹”配置；保存只写配置和审计，只影响下一次连接 / 重登 / 新 session 初始化；远端显示必须来自授权设备快照 |
| 远端观测 | Telegram 快照缺字段时不能判断一致或失败 | 一致性状态增加 `unobservable`；页面必须展示缺失字段，不能显示 `observed_matched` 或“远端已更新” |
| 首版代理路径 | “独享静态住宅 IP 必选”和用户给的 Clash 订阅配置存在冲突 | 首版落地 provider 为 `airport_clash`；独享静态住宅 IP 是节点质量目标和采购路线，不阻塞当前 Clash 订阅灰度 |
| 全节点不可用 | 只写“节点不可用”不足以约束 action、小时 stats 和任务状态 | action 写 `skipped + airport_all_nodes_unavailable`；小时 stats 为 `blocked`；任务按 `all_nodes_down_policy` 暂停或保持 running 但不补量，并发送管理员脱敏通知 |
| 登录体验 | 登录不支持回车会导致运营误以为提交无响应 | 主 PRD 要求验证码、2FA、登录确认表单支持 Enter 回车提交，并复用点击主按钮逻辑；本专项依赖账号授权资产流程提供同等体验 |
| 线上验收 | 本地测试、CI 或发布健康不能证明真实业务动作成功 | 真实 Clash 同步、出口观测、远端授权快照刷新和 Zhengzhou 3 账号线上搜索加入测试必须分别产出生产证据；未取证前只能写 `unproven` |

## 22. 未来扩展

1. **多机器人策略**：根据机器人类型自动选择不同关键词组合。
2. **AI 生成关键词**：基于目标群描述自动生成同义词 / 长尾词。
3. **ML 行为优化**：基于历史行为数据训练 ML 模型，自动调整 anti_detection 参数。
4. **多目标群入群**：一次任务绑定多个目标群，按权重分配 action。
5. **自定义 decoy 策略**：运营可上传自家 decoy 词库，避免重复。
6. **目标机器人付费 API 对接**：作为自然量的补充。
7. **目标群质量预筛**：自动过滤质量过低的群。
8. **跨任务数据回流**：一个账号在不同任务中的成功经验反哺 anti_detection 参数。
9. **真机真卡集群**：自建猫池或采购真机设备，最高安全水位。
10. **搜索行为分析**：定期抓取目标机器人搜索结果快照，反向分析排名变化曲线。

## 23. 变更记录

|日期|版本|变更人|变更内容|
|---|---|---|---|
|2026-07-02|v0.1|Mavis（PRD 起草）|初版草案|
|2026-07-02|v0.2|Mavis（PRD 修订）|1. §4.4 Action 状态机：移除 `needs_warmup` 状态值，改为 `action.payload.lifecycle_phase` + `result.skip_reason`，避免全链路回归<br>2. §7.2 设备指纹模型改为规则集（无独立表）<br>3. §7.3 设备指纹池改为**运行时随机生成** + **iOS 80% / Android 20% 硬约束**<br>4. §7.4 改为**镜像绑定**（账号生命周期内设备指纹不变，含换 IP / 跨任务）<br>5. §8.2 Warmup 改为 `(account_id, proxy_binding_id)` 二元组维度<br>6. §9.1.1 关键词字段语义：明确 region/lang/weight 在 executor 的实际使用<br>7. §9.1.2 target_groups 复用 OperationTarget<br>8. **新增 §10 目标机器人/SOSO 协议交互契约**（解析协议、翻页、异常模式、行为契约）<br>9. **新增 §11 与现有系统的集成边界**（复用 / 旁路 / 新增列表 + 核心模型引用 + 外部依赖）<br>10. §12 / §13 / §14 / §15 / §16 / §17 / §18 / §19 / §20 等章节编号因新增 §10 / §11 顺延<br>11. §13.1 数据表移除 `device_fingerprints` 独立表，更新 `account_environment_bindings` 字段|
|2026-07-02|v0.3|Mavis（PRD 修订）|按用户要求措辞中性化：<br>- 全文业务术语替换为更中性表述（如目标机器人 / 运营真实性 / 运营加入行为）<br>- §1 背景与 §18.1 去掉业务定性措辞，改由用户自担|
|2026-07-02|v0.4|Mavis（PRD 修订）|按用户要求重新定位产品功能：文档标题改为“搜索自动入群”，任务类型英文名改为 `search_join_group`，执行链路改为搜索、翻页、匹配、点击、加入、停留、退出，schema 字段统一为 `search_join_*` 和 `join_status`|
|2026-07-02|v0.5|Codex（PRD 合并）|补齐主 PRD 合并口径，修正设备指纹无独立主表、iOS 80% / Android 20%、Action 状态不新增 `needs_warmup`、实施清单和验收口径冲突；新增 Product Design Complete 自检|
|2026-07-02|v0.6|Codex（PRD 补充）|按用户补充细节完善：主/备用授权槽位独立绑定完整 iOS 优先设备指纹；新增机场 Clash 订阅、节点解析、随机分配并固定到授权槽位；入群前 pre-join decoy click 默认只浏览非目标群 / 频道不加入，并写入 action 结果、统计、风控和验收口径|
|2026-07-03|v0.7|Codex（PRD 设计修复）|深度反思并修复设计缺口：明确首版 `mtproto_userbot` 执行边界和真实样本闸门；把 Clash 节点入口与真实出口 IP 拆开；新增账号级执行锁、协议样本、出口 IP 观测、关键词 hash、默认入群后留存、无明文日志和灰度验收修正；将目标排名 Top 5 从硬验收改为运营观察指标|
|2026-07-03|v0.8|Codex（PRD 订阅与节点容灾修复）|按真实订阅返回结构补齐机场订阅解析和节点容灾：支持 Base64 URI 列表 / Clash YAML / JSON，过滤套餐/流量伪节点；新增每节点容量默认值和单节点覆盖；节点不通时按 `switch_to_next_healthy_node` 切换下一个健康节点并写审计；全订阅不可用时 `airport_all_nodes_unavailable`，不得搜索、点击或加入|
|2026-07-03|v0.9|Codex（PRD 通知与小时执行修复）|按用户补充修复：机场订阅全节点不可用时复用租户 Telegram Bot 向配置的管理员 Chat ID 推送脱敏告警，通知失败写 `admin_notification_failed`；搜索入群小时执行数量复用 AI 活跃群自然小时桶、24 小时曲线、future open、overdue open、deficit 和状态模型，但指标改为 `search_join` 成功 action，不复用 AI 发言语义|
|2026-07-03|v0.10|Codex（PRD 排名规则与联动修复）|联网核查极搜公开公告、广告/关键词排名频道和 Telegram 搜索变化资料后，补充公开排名规则只能作为产品推断；新增名称/内容相关性、持续更新、开放活跃、用户互动、流量联盟/付费广告、反作弊风险等指标映射；补齐入群后安全浏览总量不超过 3 且只点 navigate_only；新增搜索入群成功后联动 AI 活跃群等任务的 ready pool 冷却、can_send 复检、新成员占比和阻塞原因展示|
|2026-07-03|v0.11|Codex（调研落地强化）|按“我们的调研”进一步把极搜公开收录/排名口径落成设计：新增调研驱动设计决策、效果归因维度、`search_join_rank_observations` 排名观察快照和 `search_join_linked_task_dispatches` 联动投递记录；明确排名观察不计入 action success，付费广告/流量联盟/内容健康只作为解释和 warning，不改写搜索入群事实|
|2026-07-03|v0.12|Codex（任务命名与已入群口径修复）|按用户确认，将用户可见任务名调整为“搜索目标群点击任务”；内部任务类型 `search_join_group` 保持兼容。成功口径从“必须新加入”修正为“完成搜索、目标点击 / 确认并观察到目标成员关系”，账号已在群内时仍执行搜索和目标确认，`membership_observed` 仍计入成功。|
|2026-07-04|v0.13|Codex（节奏与账号上限补齐）|按用户确认，新增仅 `search_join_group` 生效的搜索节奏与账号上限设计：每账号总上限 / 每日上限 / 间隔天数、单账号单关键词每日上限、任务每日上限、小时 / 天跳过、单 action 显式跳过、小时 / 天抖动；补齐 Planner 闸门顺序、统计口径、创建编辑页字段、配置校验和验收口径。|
|2026-07-04|v0.14|Codex（PRD 漏洞审查补齐）|审查并补齐 PRD 内部漏洞：统一用户可见名称为“搜索目标群点击任务”；收敛 `target_groups` 到 `OperationTarget` 持久引用；明确 `pacing_config` 是跳过 / 抖动唯一权威来源；补齐租户时区日界线、并发计数事务、随机采样持久化、运行中编辑生效规则、全局风控与任务内上限优先级，以及 `search_join_pacing_decisions` 数据表。|
|2026-07-04|v0.15|Codex（账号面具环境粒度复核）|完整复核账号面具、全局 Clash 和授权指纹口径：授权环境配置落到 `account_id + developer_app_id/api_id + authorization_id/session_role`；系统设置 Clash 配置拆分读取 `system.view` 与保存 / 测试 / 同步 `system.manage`；保存指纹仍只代表配置更新，不代表远端授权设备立即变更。|
|2026-07-04|v0.16|Codex（代理粒度复核）|曾按 `account_id + developer_app_id/api_id + authorization_id/session_role` 讨论代理和授权指纹同粒度绑定；该口径已被 v0.17 覆盖，不能作为当前实现依据。|
|2026-07-04|v0.17|Codex（授权槽位级代理定稿）|按用户最新口径完整收口：系统设置只保存一个全局 Clash 订阅地址；“账号面具”一级菜单承载授权槽位级代理和授权指纹配置；同一账号不同 TG 开发者应用、session key 和主 / 备用授权槽位可以使用不同代理和不同指纹；修改指纹配置只影响下一次连接 / 重登 / 新 session 初始化，不声明远端授权设备立即变更。|
|2026-07-04|v0.18|Codex（PRD 缺口复核）|补齐系统设置页 Clash 配置入口验收文字；修正示例配置和区域一致性口径，默认由关键词允许矩阵与风险评分决定，不再强制账号区域、设备语言和代理出口国家三者硬相等；强化“配置指纹已保存”不等于“远端已观测一致”。|
|2026-07-04|v0.19|Codex（PRD 完整梳理）|按主线程和子代理复核补齐缺口：主 PRD 任务类型表新增 `search_join_group`；系统设置 / 账号面具 / 风控中心代理权属拆成全局 Clash 订阅、授权槽位代理绑定、代理健康处置三层；首版代理路径定为 `airport_clash`；补齐远端观测 `unobservable`、观测刷新 API、运行中节奏编辑影响预览、概率 / 抖动字段范围、全节点不可用 action / 小时 / task 三层状态，以及实时 pacing / random 不调用 AI Gateway 的验收。|

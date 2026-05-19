# TG 运营管理平台系统设计文档

> 本文是 TG 运营管理平台的主设计文档，用于统一产品边界、系统模块、核心流程、执行架构、当前实现状态和后续实施优先级。  
> 规则中心细节见 `docs/rules-center-design.md`。  
> 账号中心与风控中心细节见 `docs/risk-control-and-account-center-design.md`。  
> TG 账号登录设备清理、平台可信设备、二步验证、头像、昵称和用户名批量初始化见 `docs/account-security-hardening-design.md`。
> 素材库、图片发送、监听媒体转发和 AI 表情包设计见 `docs/material-library-design.md`。  
> 1000 账号容量、Dispatcher 多 worker 和执行层升级方案见 `docs/capacity-and-dispatch-upgrade-plan.md`。  
> 当前代码静态复核和实施前补充设计见 `docs/architecture-scale-assessment-and-upgrade-plan.md`。  
> 频道任务启动前自动关注频道的前置阶段见 `docs/channel-membership-precondition-design.md`。
> 本文不作为数据库字段完整设计；数据库、迁移和接口以代码与专门设计文档为准。

---

## 1. 项目定位

TG 运营管理平台面向 Telegram 运营团队，用一个后台统一管理 TG 账号、运营目标、消息发送、频道互动、AI 活跃群、转发监听、规则、风控、归档、运营数据和审计。

平台核心目标：

```text
账号接入
  -> 资产同步
  -> 目标确认
  -> 规则配置
  -> 风控策略
  -> 任务创建
  -> 监听 / AI / Worker 执行
  -> 任务详情追踪
  -> 数据复盘
  -> 审计留痕
```

新版系统不再以旧 Campaign、旧 AI 草稿审核、多租户 SaaS、卡密或订阅套餐作为主线。历史模型可以保留为兼容层，但不能进入新版运营主流程。

系统边界：

- 只能使用已接入并授权的 TG 账号。
- 只能操作已确认的运营目标。
- 只能监听已授权源群、群聊、频道或讨论组。
- 所有发送、监听、过滤、转换、AI 生成、失败、重试都必须留痕。
- 后台支持多个内部账号共同使用，权限做到菜单、按钮和写接口级别。
- 高风险动作依靠二次确认、原因填写和审计追踪控制。

产品原则：

```text
可配置
可自动执行
可追踪
可暂停
可重试
可解释
可风控
可审计
```

AI 活跃群和转发监听群不走人工审核主线。系统通过规则过滤、自动校验、风控、限速、上下文检查、失败重试和审计来保证任务可控。

---

## 2. 当前实现状态

本节按当前代码口径总结，不等同于最终目标。

### 2.1 已落地能力

- 前端主导航已覆盖：运营概览、TG 账号管理、运营目标、消息发送、任务中心、监听中心、规则中心、风控中心、归档中心、运营数据、系统设置、审计记录。
- 后端主路由已覆盖账号、账号池、开发者应用、AI 配置、运营目标、消息发送、任务中心、监听与规则汇总、运营数据、归档和审计。
- 旧 Campaign、旧 operation-task、旧 review 路由已从默认主路径隔离，仅在显式兼容开关打开时进入旧口径。
- 任务中心主任务类型为 5 类：AI 活跃群、转发监听群、频道浏览、频道点赞、频道评论/回复。
- 任务中心支持创建、创建并启动、启动、暂停、继续、停止、重试、重置、编辑配置、删除、详情查看和执行记录追踪。
- 频道任务默认按 `dynamic_new` 持续采集新消息，支持容量检查、每条频道消息执行明细、账号执行项和容量不足提示。
- 频道任务启动前会按目标能力判断是否需要关注频道阶段：运营目标可用 `@username`、公开频道链接、`https://t.me/+...` 或 `joinchat` 邀请链接、peer id 手动添加；已授权且可发送的频道可直接进入主互动；未授权或不可发送的频道先让账号按抖动节奏加入频道，只补齐未关注账号，主互动只使用已关注或刚关注成功的账号。
- AI 活跃群已按 Cycle / Turn 记录发言计划，支持账号角色映射、近期账号记忆、长期账号画像、AI 生成记录、内容过滤、重复过滤、上下文采集和账号冷却。
- 转发监听群已接入源群上下文采集、规则版本、过滤、转换、路由、去重、目标群发送项和转发批次追踪。
- 监听中心已有聚合页面和 `/api/listeners/summary`，按频道 / 群展示关联任务、监听账号、事件积压、最后事件和错误。
- 规则中心已有规则集版本管理、规则测试、规则冲突提示、规则命中统计和转发归因。
- 运营数据已有 `/api/operation-metrics/summary` 真实汇总接口。
- 审计记录支持详情抽屉和 CSV 导出。
- 执行层已有 Task / Action 模型、Action claim、执行租约、worker heartbeat、监听运行层、账号容量检查和 Redis token bucket 基础。

### 2.2 仍需收敛的问题

- `groups` 与 `operation_targets` 仍处在双轨过渡期。用户侧入口应优先使用运营目标，执行层仍可保留群资产作为 TG 授权、账号权限和上下文采集承载。
- 任务创建页仍暴露部分执行参数，例如抖动、静默最多账号、静默每轮发言、爬坡比例、上下文过期。应改为“默认可运行、解释可理解、高级可覆盖”的创建体验。
- 规则中心仍需继续取消独立关键词库口径，将关键词、敏感词、白名单词、路由词统一收进规则集条件。
- 后台账号与权限仍需补完整闭环：后台账号登录、用户管理、菜单和按钮权限、后端写接口校验、权限变更审计。
- 单账号代理、代理健康检查、代理告警状态机和风控中心处置闭环仍是增强项。
- 面向 1000 账号和 20-30 个持续任务，当前执行层还不能只靠单 worker 综合 drain，需要升级为多 role worker、有界并发、跨进程账号锁、多维限流和运行快照。

### 2.3 2026-05-20 更新记录

本日更新重点围绕账号资料初始化和频道任务启动前置条件，已落入代码和测试：

- 账号资料初始化：批量资料预览改为一次 AI 请求生成整批账号资料，避免逐账号调用导致慢和超时；前端资料预览请求支持按账号数量放宽超时时间。
- 账号资料初始化：新增命名风格提示 `custom_prompt`，默认引导生成真实 TG 用户昵称，而不是正式姓名或营销号名称；本地兜底昵称池也改为更自然的网名风格。
- 账号资料初始化：AI 供应商选择改为优先使用健康默认供应商，默认不可用时回落到首个健康供应商；AI 超时、mock 或无健康供应商时仍生成本地兜底预览，并在预览里展示原因。
- 账号资料初始化：资料动作的预检不再强制刷新登录设备安全快照；缺失或不可用头像来源不阻塞昵称、简介和 username 执行，只跳过头像并展示警告。
- TG 账号管理：批量入口拆成“资料初始化”“设置二步密码”“清理登录设备”，降低把资料初始化和安全加固混在同一动作里的误操作风险。
- 频道互动任务：已授权且可发送的频道不再进入关注频道前置阶段，直接生成主互动 action；未授权或不可发送的频道仍按 `ensure_channel_membership` 前置阶段补齐关注。
- 频道互动任务：新增 PostgreSQL 级回归测试覆盖关注前置、全部关注失败阻断主互动、已授权频道跳过前置、运行时守卫和旧任务守卫。
- 账号新增：PostgreSQL 下创建账号前会同步 `tg_accounts.id` 序列，避免历史导入或手工插入后序列落后导致新增账号主键冲突。

---

## 3. 系统总架构

新版系统按功能中心组织：

1. 运营概览中心
2. 系统初始化中心
3. TG 账号中心
4. 运营目标中心
5. 消息发送中心
6. 任务中心
7. 监听中心
8. 规则中心
9. 风控中心
10. AI 内容中心
11. 素材库
12. 后台账号与权限中心
13. 数据、归档与审计中心
14. 执行中心

模块关系：

```text
系统初始化
  提供 TG 开发者应用、AI 供应商、素材、代理和基础配置。

TG 账号中心
  提供可登录、可同步、可绑定代理、可发送、可监听的账号资源。

运营目标中心
  把账号同步到的群、频道、联系人整理成可运营对象。

规则中心
  提供过滤、转换、路由、账号策略、限速、重试策略和输出校验。

风控中心
  负责账号评分、发送准入、代理健康、目标能力、限流、冷却、处置和告警。

任务中心
  把业务意图拆成 Task 和 Action，交给执行中心运行。

执行中心
  负责 Planner、Dispatcher、Listener、Recovery、Metrics 等后台执行角色。

运营数据 / 归档 / 审计
  提供统计、追溯、归档和审计闭环。
```

---

## 4. 主导航与页面结构

建议主导航保持清晰分层：

- 运营概览
- TG 账号管理
- 运营目标
- 消息发送
- 任务中心
- 监听中心
- 规则中心
- 风控中心
- 归档中心
- 运营数据
- 系统设置
- 后台账号与权限
- 审计记录

如果前端需要简化，可以合并为：

- 运营概览
- TG 账号管理
- 运营目标
- 消息发送
- 任务中心
- 规则、风控与监听
- 归档与数据
- 系统设置
- 后台账号与权限
- 审计记录

从产品清晰度看，任务中心、监听中心、规则中心、风控中心建议保持独立，因为它们分别代表执行、事件、规则和运行控制。

---

## 5. 后台账号与权限

后台不再假设所有用户都是同级 admin。权限目标是控制菜单、按钮和写接口，不做数据隔离。

角色建议：

| 角色 | 职责 |
| --- | --- |
| 平台管理员 | 全部配置、账号接入、任务管理、风控处置、用户权限 |
| 运营主管 | 任务、目标、规则、数据和审计查看，部分风控处置 |
| 运营人员 | 创建和维护任务、查看执行结果、处理日常异常 |
| 账号添加专员 | 只负责 TG 账号接入、登录、恢复和基础信息同步 |
| 只读观察员 | 查看概览、任务、数据和审计，不执行写操作 |

权限粒度：

- 菜单权限：控制导航入口。
- 按钮权限：控制创建、编辑、启动、暂停、删除、导出、切换账号等操作。
- 写接口权限：后端必须二次校验，不能只靠前端隐藏按钮。
- 危险动作：停止任务、删除任务、切换监听账号、恢复账号、修改风控策略、导出敏感数据必须写审计。

账号添加专员只能访问：

- TG 开发者应用查看或选择。
- 账号登录 / 二维码 / 验证码 / 2FA 流程。
- 账号同步状态。
- 账号接入失败原因。

账号添加专员不能访问：

- 任务中心。
- 消息发送。
- 规则中心。
- 风控策略。
- 运营数据。
- 审计导出。

---

## 6. 系统初始化中心

系统初始化中心只放运行基础配置，不承载任务节奏和风控策略。

包括：

- TG 开发者应用：`api_id`、`api_hash`、应用名称、使用状态、账号绑定情况。
- AI 供应商：模型供应商、API Key、模型名、启用状态、默认模型。
- 素材基础配置：素材上传限制、素材缓存目标、媒体缓存策略。
- 本地代理资源：代理地址、认证信息、健康检查、绑定账号数。
- 系统运行配置：数据库、Redis、worker、媒体目录、环境开关。

不包括：

- 全局发送节奏。
- 全局静默时段。
- 默认失败处理策略。
- 账号评分。
- 处置队列。
- 命中记录。

这些统一归入风控中心。

---

## 7. TG 账号中心

账号中心负责账号接入、同步、健康、分组、代理绑定和恢复闭环。

### 7.1 账号状态

建议状态模型：

| 状态 | 含义 | 任务是否可用 |
| --- | --- | --- |
| 在线 | session 可用，最近同步正常 | 可用 |
| 离线 | 最近连接失败或长期未同步 | 不建议使用 |
| 需重新登录 | session 失效或 2FA 失败 | 不可用 |
| 受限 | 账号被 TG 限制发送或互动 | 不可用或只读 |
| 异常 | 代理、权限、同步或登录异常 | 需风控判断 |
| 禁用 | 人工停用 | 不可用 |

### 7.2 账号详情

账号详情需要展示：

- 基础信息：昵称、用户名、手机号脱敏、账号状态、健康分。
- 登录信息：session 状态、最近登录时间、最近同步时间。
- 代理信息：绑定代理、健康状态、最近错误。
- 群/频道资产：可发送、可监听、可浏览、可评论的目标列表。
- 任务参与：参与中的任务、最近执行项、成功/失败次数。
- 风控摘要：小时 / 日限额、冷却、FloodWait、账号受限、处置记录。

### 7.3 账号分组与账号池

账号分组用于运营选择和任务配置，不是权限隔离。

任务创建时账号范围可选择：

- 全部可用账号。
- 指定账号分组。
- 手动选择账号。
- 按目标推荐账号。

执行时必须再次经过风控中心和账号容量检查。前端预检只用于减少无效提交，不能替代 worker 执行前校验。

### 7.4 代理绑定

代理不是日常任务选择主维度。没有代理告警时，任务仍按账号分组 / 账号池选择账号。代理作为账号运行环境进入风控：

- 代理不可达。
- 代理认证失败。
- 同代理账号异常聚集。
- 同代理账号 FloodWait 异常增加。
- 绑定账号数或并发连接超过建议上限。

代理告警达到阈值后进入风控中心处置队列。

---

## 8. 运营目标中心

运营目标是用户创建任务、发送消息和查看数据时面对的业务对象。

目标类型：

| 类型 | 能力 |
| --- | --- |
| 群 | 发送、监听、AI 活跃、转发源、转发目标、归档 |
| 频道 | 浏览、点赞、评论、回复、监听新消息、归档 |
| 讨论组 | 评论/回复承载、上下文采集 |
| 联系人 / 私聊 | 消息发送，默认不进入自动任务 |

目标详情需要展示：

- 目标基础信息。
- 授权状态。
- 可发送 / 可监听 / 可归档 / 可创建任务能力。
- 关联账号和账号能力。
- 目标级风控策略。
- 最近消息和上下文。
- 已绑定任务和运行状态。
- 数据统计和失败原因。

目标中心负责把 TG 资产整理成运营对象；执行层仍可使用群资产和频道消息表承载底层授权与上下文。

---

## 9. 规则中心

规则中心维护系统级规则集，任务绑定已发布规则版本。

规则集承载：

- 输入过滤：关键词、敏感词、白名单词、链接白名单、来源条件。
- 输出校验：生成内容是否命中禁词、是否过长、是否像模板噪声、是否包含运营人员内部话术。
- 内容转换：轻量改写、去来源、保留来源、格式化、素材处理。
- 路由规则：按源群、关键词、目标类型、权重分配目标。
- 账号策略：固定账号、轮询、权重、目标账号映射。
- 限速策略：任务级、目标级、账号级、任务类型级。
- 重试策略：最大重试、退避、失败后跳过或暂停。

规则中心原则：

- 任务只能绑定已发布版本。
- 草稿版本不能被运行任务绑定。
- 关键词不再作为独立产品中心出现，它只是规则条件的一种。
- 规则测试器必须能预览过滤、转换、路由、账号策略和限速结果。
- 规则命中需要能回溯到规则集版本、任务、目标、账号和执行项。

---

## 10. 风控中心

风控中心负责运行硬边界和处置闭环。

风控职责：

- 全局发送节奏。
- 默认失败处理策略。
- 账号小时 / 日上限。
- 账号全局冷却。
- 目标群 / 频道发送窗口。
- 群日上限和群冷却。
- 代理健康和代理告警。
- FloodWait、SlowMode、账号受限、权限不足、内容拦截处置。
- 账号评分和处置队列。
- 命中记录和策略审计。

执行准入链路：

```text
Action
  -> 账号可用性
  -> 代理健康
  -> 目标权限
  -> 目标级风控
  -> 规则中心过滤 / 转换 / 路由
  -> Redis 多维限流
  -> Telegram Gateway
```

失败处置口径：

| 失败类型 | 影响对象 | 默认处置 |
| --- | --- | --- |
| FloodWait | 账号 | 写入账号冷却，延后 action |
| SlowMode | 目标 | 写入目标冷却，延后 action |
| 账号受限 | 账号 | 暂停账号派单，进入处置队列 |
| 账号不可用 | 账号 | 转派或跳过 |
| 目标无权限 | 目标 | 拦截并提示重新授权 |
| 代理不可达 | 代理 / 账号 | 进入代理告警，必要时阻塞绑定账号 |
| 内容拦截 | 内容 / 规则 | 跳过、改写重试或暂停任务 |
| unknown_after_send | 执行项 | 进入人工确认或补偿查询，不自动重发 |

---

## 11. 素材库与 AI 内容

素材库从“URL / 文本配置”增强为可复用基础能力：

- 实时上传素材。
- 维护素材类型、大小、用途和标签。
- 缓存 Telegram 媒体引用。
- 支持消息发送、转发监听和 AI 活跃群共用。
- 支持源媒体下载再上传，不假设不同账号可直接复用 Telegram 文件引用。

AI 内容中心负责：

- 群上下文摘要。
- AI 活跃群多账号对话生成。
- 频道评论生成。
- 回复生成。
- 转发改写。
- 内容自动校验。
- 账号画像和账号语气记忆。

AI 输出必须经过规则中心和风控中心校验，不能把内部提示词、后台术语、审核词或操作员话术发送到公开群。

---

## 12. 任务中心

任务中心统一使用 Task / Action 模型。

```text
Task
  表示一个长期或短期运营任务。

Action
  表示某次具体执行动作，例如发送、浏览、点赞、评论、回复。
```

任务通用状态：

| 状态 | 含义 |
| --- | --- |
| draft | 草稿 |
| pending | 等待开始 |
| running | 运行中 |
| paused | 暂停 |
| completed | 完成 |
| failed | 失败 |
| stopped | 人工停止 |
| deleted | 删除归档 |

任务操作：

- 启动：进入运行状态，开始规划 Action。
- 暂停：暂停新规划和新执行，保留状态。
- 继续：从暂停恢复。
- 停止：结束任务，跳过未执行项。
- 重试：重试失败项。
- 重置：清空未完成计划，重新规划。
- 删除：隐藏或软删除任务，保留审计。

### 12.1 创建体验

任务创建页应采用“快速创建 + 高级设置”。

快速创建只要求：

- 任务类型。
- 运营目标。
- 账号范围。
- 任务结束时间。
- 业务意图，例如话题方向、评论方向、转发处理方式。
- 创建前预检。

高级设置承载：

- 账号参与策略。
- 24 小时活跃曲线。
- AI 生成与规则集选择。
- 内容过滤与改写。
- 失败处理和重试。
- 审计和告警。

默认配置必须能直接运行，不能要求运营人员先理解抖动、爬坡、静默比例、冷却和退避。

### 12.2 24 小时活跃曲线

任务级节奏从多个零散字段收敛为一条 24 小时活跃曲线。每小时一个强度点，取值 0-100。

强度含义：

| 强度 | 含义 |
| --- | --- |
| 0 | 休眠，只保留监听和状态检查 |
| 1-20 | 低频 / 静默 |
| 21-60 | 常规 |
| 61-100 | 高峰 |

曲线只表达计划强度，不直接承诺发送量。实际动作量还必须经过账号容量、目标能力、规则和风控检查。

小时计划量建议公式：

```text
小时计划量
  = 任务类型默认基准量
  * 曲线强度比例
  * 可用账号系数
  * 风控系数
  * 目标能力系数
```

### 12.3 预检确认

创建前必须展示：

- 可用账号数。
- 目标能力。
- 预计动作量。
- 容量缺口。
- 规则版本。
- 风控命中。
- 可能导致任务无法执行的阻塞项。

预检只是提示和减少无效提交，worker 执行前必须重新校验。

频道浏览、点赞、评论和回复任务的预检必须区分两类能力：

- 目标频道是否可以被系统解析和确认。
- 账号范围内有哪些账号已关注、哪些账号需要先关注、哪些账号无法关注或受限。

如果目标频道存在但当前账号均未关注，允许保存任务配置；未授权或不可发送的频道启动时必须先进入关注频道前置阶段。已授权且可发送的频道可以跳过前置阶段直接规划主互动。前置阶段没有任何账号成功关注时，主互动阶段不能开始。

---

## 13. 任务类型设计

### 13.1 频道互动任务

包括：

- 频道浏览。
- 频道点赞。
- 频道评论 / 回复。

核心设计：

- 运营人员可以通过 `@username`、公开频道链接、`https://t.me/+...` / `joinchat` 邀请链接或已知 peer id 手动添加频道目标；其中 peer id 更适合作为已同步目标识别，无法保证单独完成关注。
- 启动频道互动任务前，系统先判断目标频道是否已授权且可发送；已授权且可发送时直接规划主互动，其他频道自动生成 `ensure_channel_membership` 前置阶段，覆盖本任务账号范围内所有候选账号。
- 已关注账号直接标记为已满足前置条件；未关注账号按抖动、限速和风控节奏执行关注；关注失败账号记录失败原因并参与后续重试或跳过策略。
- 主互动阶段只能使用已确认关注成功或原本已关注的账号。
- 默认持续监听新消息。
- 每条频道消息生成独立执行明细。
- 去重维度至少包含任务、频道消息、动作类型、账号。
- 评论和回复当前可由同一个 `channel_comment` 类型承载，payload 区分 `comment_mode`、`reply_to_message_id` 和 `reply_target_label`。
- 目标量必须经过账号容量和目标能力检查。
- 关注频道前置阶段不能集中瞬时执行，必须使用随机账号顺序、批次间隔、单账号退避和 FloodWait 延后。
- 如果部分账号关注失败，默认使用成功关注账号继续执行主互动阶段，并在任务详情中展示容量缺口和失败账号明细；如果 0 个账号成功关注，则任务进入异常或等待处理状态。

### 13.2 AI 活跃群

AI 活跃群用于在授权群中进行多账号自然发言。

核心设计：

- 按 Cycle / Turn 规划。
- 支持账号角色、人设、近期账号记忆和长期账号画像。
- 生成前汇总真人上下文、上一轮 AI 内容和主线方向。
- 一次生成多账号对话，形成承接、补充、互动、收束的多轮结构。
- 发送前做内容过滤、重复过滤、上下文过期检查和账号冷却。
- 不走人工审核。

图片和表情包策略：

- 可从素材库选择。
- 不把图片作为所有发言的默认必选项。
- 媒体发送必须经过素材缓存和目标能力检查。

### 13.3 转发监听群

转发监听群用于监听源群事件，按规则过滤、转换、路由并发送到目标群。

核心设计：

- 源群可以有多个。
- 目标群可以有多个。
- 规则集版本决定过滤、转换、路由、账号策略和限速。
- 源事件必须有唯一键，避免重复转发。
- 多目标转发时，一个源事件发到 3 个目标群，计为 3 条目标群发送量。
- 目标群发送量由 24 小时曲线和风控共同决定。
- 不走人工审核。

来源过滤配置：

- 屏蔽机器人消息：默认开启。旧任务未显式配置时也按开启处理，避免机器人消息继续触发转发。
- 不转发群主和管理员消息：默认关闭。开启后，源群群主和管理员发送的消息只记录来源事件，不生成目标群转发发送项。
- 来源不转发名单：默认空。运营人员可以从最近来源发言人中选择，也可以手动粘贴 `@username` / `sender_peer_id` / 昵称。
- 名单保存优先使用稳定的 `sender_peer_id`，其次使用 `@username`，昵称只作为无法识别稳定 ID 时的兜底；昵称匹配存在同名误伤风险，界面和方案中必须提示。
- 来源过滤属于转发任务配置，不是监听中心的固定全局规则；不同转发任务可以对同一个源群采用不同过滤策略。

---

## 14. 监听中心

监听中心负责源事件和上下文采集，不负责业务发送。

监听对象：

- 群。
- 频道。
- 讨论区。
- 评论树。

监听中心展示：

- 监听对象。
- 监听账号。
- 关联任务。
- 订阅事件类型。
- 待分发统计。
- 去重键统计。
- 最近事件。
- 最近错误。
- 备用监听账号建议。

监听运行层原则：

- 同一个源在同一窗口内只采集一次。
- 使用 source claim 和 lease 防止多 worker 重复采集。
- 维护事件水位。
- 记录来源身份，包括发送者 `sender_peer_id`、昵称、`@username`、是否机器人、是否群主或管理员。
- 不把 bot 消息过滤写死为监听层不可配置规则；转发任务按自己的来源过滤配置决定是否过滤机器人、群主 / 管理员或来源不转发名单中的发送者。
- 处理 media group 聚合。
- 采集失败和发送失败分开统计。

---

## 15. 执行中心

执行中心是面向 1000 账号规模的核心。

### 15.1 目标执行架构

后台执行拆成五类 worker：

| Role | 职责 |
| --- | --- |
| Planner Worker | 扫描 running task，生成 pending action |
| Dispatcher Worker | 原子领取 pending action，执行 TG API，回写结果 |
| Listener Worker | 采集源事件和上下文，维护监听水位 |
| Recovery Worker | 恢复 claim 超时、lease 超时、worker 失联和结果未知 |
| Metrics Worker | 记录队列、延迟、限流、worker、账号和代理运行快照 |

`all` 只用于开发和兼容，不作为生产推荐。

### 15.2 Worker role 契约

| Role | 输入 | 允许写入 | 禁止事项 |
| --- | --- | --- | --- |
| planner | running tasks、规则、账号池、上下文 | actions、tasks.next_run_at、任务 stats | 禁止调用 TG Gateway |
| dispatcher | due pending actions | action result、execution attempts、账号运行状态、任务 stats | 禁止生成新业务 action |
| listener | listener source、源目标、监听账号 | 上下文消息、listener source state、唤醒任务 | 禁止发送业务消息 |
| recovery | claiming / executing / heartbeat 超时数据 | action 恢复状态、attempt 结果未知、任务错误摘要 | 禁止调用 TG |
| metrics | action、task、worker、账号、代理、Redis 指标 | runtime snapshots、聚合指标 | 禁止改变业务状态 |
| legacy | 旧 message task / campaign / operation task | 旧兼容表 | 禁止进入新版 Task Center 主流程 |

### 15.3 Action 状态机

主路径：

```text
pending
  -> claiming
  -> executing
  -> success / failed / skipped
```

恢复路径：

- `claiming -> pending`：claim 超时、运行资源不足、账号分片不匹配、Redis 限流、账号 in-flight 冲突。
- `executing -> unknown_after_send`：已进入 Telegram Gateway 调用边界，但本地结果未知。
- `executing -> failed`：没有进入 Gateway 调用边界，或明确失败。

`unknown_after_send` 不能自动重复发送，必须进入人工确认、补偿查询或明确失败处理。

### 15.4 Dispatcher claim

Dispatcher claim 必须两段式短事务：

```text
阶段 1：DB 短事务预领取
  SELECT due pending actions
  JOIN running tasks
  APPLY task priority / type weight / per-task quota / account shard
  FOR UPDATE SKIP LOCKED
  SET status = claiming, claim_owner, claim_token, claim_expires_at

阶段 2：事务外获取运行资源
  Redis token bucket
  account in-flight lock
  proxy / target / media quota

阶段 3：DB 短事务确认执行
  SET status = executing, lease_owner, lease_expires_at
```

Telegram API 调用期间不持有数据库事务。

### 15.5 限流与账号互斥

多 worker 限流使用 Redis token bucket，覆盖：

- 全局 TG API。
- 任务。
- 任务类型。
- 账号。
- 代理。
- 目标。
- 媒体发送。

Redis token bucket 可以先采用保守扣减语义。完整 reservation / confirm / refund 在 Dispatcher 并发稳定后再增强。

同一账号默认只能被一个 worker 同时使用。账号 in-flight 与 token bucket 分开处理：

- token bucket 管速率。
- in-flight lock 管同一账号是否正在执行。

DB 唯一索引可以作为最后兜底，但不应作为主要调度机制。

### 15.6 账号分片

账号分片必须在 claim 前生效。

原则：

- 账号转派只能在当前 shard 内完成。
- 当前 shard 无可用账号时 action 延后并记录原因。
- 不建议运行中频繁修改 `ACCOUNT_SHARD_TOTAL`。
- worker 故障接管需要明确是保持 shard 积压，还是由 standby worker 接管。

### 15.7 数据库连接池与 Telethon client

Dispatcher 并发必须受 PostgreSQL 连接池预算约束。

需要显式配置：

- `DB_POOL_SIZE`
- `DB_MAX_OVERFLOW`
- `DB_POOL_TIMEOUT`
- `DB_POOL_RECYCLE`

Gateway 契约数据结构已收敛到 `backend/app/integrations/telegram/contracts.py`，Telegram 网关适配器集中在 `backend/app/integrations/telegram/` 包内，旧 `backend/app/gateways.py` 已删除。Telethon client 生命周期已从网关适配器中抽到 `backend/app/telethon_lifecycle.py`，由独立模块管理后台 event loop、client cache、idle 释放、LRU 上限和应用停止时的 disconnect。生产参数由环境变量控制：

- `TELETHON_CLIENT_CACHE_SIZE`：单进程最多缓存的 Telethon client 数。
- `TELETHON_CLIENT_IDLE_SECONDS`：client 空闲多久后释放。
- `TELETHON_CLIENT_CONNECT_TIMEOUT_SECONDS`：连接超时。
- `TELETHON_OPERATION_TIMEOUT_SECONDS`：同步业务入口等待异步 TG 调用的上限。

生命周期规则：

- 每个 worker 只应加载自身 shard 内账号；Dispatcher / Listener 默认不共享进程，也就不共享 client cache。
- client cache key 首期按 `api_id + session` 维度管理；session 失效、账号受限、代理切换时应调用 lifecycle invalidate 或由后续风控处置清理。
- FastAPI shutdown 会调用 Telethon lifecycle shutdown，尽量断开已缓存 client。
- 单 worker 的 client 数必须小于 cache size，并结合文件描述符、内存、代理出口和 TG 延迟做真实压测。

### 15.8 Metrics 快照

运营数据实时查询不能替代 Metrics Worker。Metrics 需要周期性记录运行快照。

首期指标：

- `actions.pending.count`
- `actions.claiming.count`
- `actions.executing.count`
- `actions.oldest_pending_age_seconds`
- `actions.claimed_per_minute`
- `actions.success_per_minute`
- `actions.failed_per_minute`
- `gateway.flood_wait.count`
- `gateway.slowmode.count`
- `worker.active.count`
- `worker.stale.count`
- `redis.token_limited.count`
- `db.pool_wait_ms.p95`

页面必须能回答：

- 当前是否积压。
- 积压在哪个 role。
- 哪个 worker 掉线。
- 哪个账号、代理、目标造成限流最多。

---

## 16. 归档、运营数据与审计

### 16.1 归档中心

归档中心负责群、频道和消息上下文的长期保存和检索。

能力：

- 群 / 频道归档。
- 消息检索。
- 成员快照。
- 归档任务状态。
- 与运营目标关联。

### 16.2 运营数据

运营数据用于复盘，不承载策略编辑。

数据维度：

- 账号数据：发送数、成功数、失败数、FloodWait、受限次数。
- 目标数据：发送量、互动量、失败量、容量不足次数。
- 任务数据：计划数、执行数、成功率、失败原因、积压。
- 规则数据：命中次数、转化、目标归因、账号归因。
- 素材数据：素材指纹、源事件、目标、成功率、失败和跳过。
- worker 数据：活跃、过期、处理速率、最老 pending。

### 16.3 数据保留

运行明细默认保留最近 5 个自然日。第 6 天先把第 1 天明细汇总到日统计，再滚动删除第 1 天明细。

长期保留：

- 任务维度汇总。
- 账号维度汇总。
- 目标维度汇总。
- 任务类型维度汇总。
- 日期和状态维度汇总。
- 清理审计。

`unknown_after_send` 和人工未处理项不应被普通保留周期清理，除非已经人工确认或归档。

### 16.4 审计记录

审计记录必须覆盖：

- 账号登录、恢复、禁用、代理切换。
- 目标授权、能力变更、账号覆盖编辑。
- 任务创建、编辑、启动、暂停、停止、删除、重试、重置。
- 规则发布、回滚、复制、测试。
- 风控策略修改和处置。
- 数据导出。
- 权限变更。
- 手动 drain 或其他危险操作。

---

## 17. 前端页面规划

页面应以“可操作、可解释、可定位”为目标。

| 页面 | 关键内容 |
| --- | --- |
| 运营概览 | 今日任务、执行量、异常、worker 状态、积压、快捷入口 |
| TG 账号管理 | 账号列表、登录接入、状态、分组、代理、详情、恢复 |
| 运营目标 | 群/频道/联系人目标、能力、账号覆盖、风控、关联任务 |
| 消息发送 | 手动发送、目标选择、素材选择、发送记录 |
| 任务中心 | 任务列表、创建向导、详情、执行项、重试、重置 |
| 监听中心 | 监听对象、监听账号、事件积压、最近事件、切换监听号 |
| 规则中心 | 规则集、版本、测试器、发布、回滚、命中统计 |
| 风控中心 | 全局策略、账号评分、代理告警、处置队列、命中记录 |
| 归档中心 | 归档任务、消息检索、成员快照 |
| 运营数据 | 账号、目标、任务、规则、素材、worker 指标 |
| 系统设置 | TG 应用、AI 供应商、素材基础配置、运行配置 |
| 后台账号与权限 | 后台用户、角色、权限、审计 |
| 审计记录 | 操作记录、筛选、详情、导出 |

---

## 18. 压测与容量报告

1000 账号能力必须通过压测固化，静态评估不能等同线上承载证明。

当前已新增可重复的 mock gateway 容量模型：

- 脚本：`backend/scripts/run_capacity_benchmark.py`
- JSON 输出：`reports/capacity/latest.json`
- Markdown 报告：`docs/capacity-report-100-300-1000.md`

该报告用于固化容量参数和验收口径，仍不能替代真实 PostgreSQL / Redis / TG API 延迟压测。

压测场景：

- 100 账号 / 5 任务。
- 300 账号 / 10 任务。
- 1000 账号 / 20-30 任务。
- TG Gateway mock 快速返回。
- TG Gateway mock 慢返回。
- FloodWait / SlowMode / 账号受限注入。
- Redis 不可用。
- DB 连接池紧张。
- worker 异常退出和重启。

容量报告必须输出：

- 推荐 worker 数。
- 推荐 `DISPATCHER_CONCURRENCY`。
- 推荐 `ACTION_CLAIM_LIMIT`。
- 推荐 `DB_POOL_SIZE / DB_MAX_OVERFLOW`。
- 每分钟处理量。
- oldest pending P95。
- `unknown_after_send` 数。
- 重复发送数，必须为 0。
- 单机容量边界。

---

## 19. 实施优先级

### P0：口径收敛

- 保持新版主线，不让旧 Campaign、旧 review、旧多租户订阅口径回流。
- 任务创建入口以运营目标为主，不暴露旧群 ID 作为用户主选择。
- 系统设置不承载发送节奏和风控策略。

### P1：千账号执行层升级

- 接通 `WORKER_ROLE`，让 role 配置真正生效。
- 把 task center drain 拆成 planner / dispatcher / listener / recovery / metrics 可单独调用的函数。
- 更新生产 compose，启动多角色 worker。
- Dispatcher 接入有界并发和短 session 执行。
- 补 Redis 跨进程账号 in-flight 和账号分片。
- Listener 独立 worker 和 source shard 扩容。
- Metrics 快照和容量面板。
- 压测验证 100 / 300 / 1000 账号。

### P1：运营目标统一

- 继续减少旧群 ID、频道 ID 在用户侧入口暴露。
- 保持执行层兼容群资产承载 TG 授权和上下文采集。
- 消息发送、任务创建、归档和运营数据优先使用运营目标。

### P1：任务创建体验改造

- 快速创建只保留任务类型、目标、账号范围、结束时间和业务意图。
- 高级设置折叠展示。
- 24 小时活跃曲线替代静默、爬坡、抖动等零散字段。
- 预检确认页展示容量、规则、风控和风险。
- 频道任务启动前自动执行关注频道前置阶段，前置阶段使用抖动、批次限速、账号退避和失败追踪；本阶段主要是任务中心后端和执行层能力，不新增独立前端功能。

### P1：规则与风控闭环

- 关键词统一进入规则集条件。
- 规则测试器展示过滤、转换、路由、账号策略和限速。
- 风控中心补齐代理告警、账号处置和命中记录。

### P2：数据和审计补深

- Metrics Worker 运行快照。
- 任务、账号、目标、规则、素材多维归因。
- 审计导出和危险动作审计原因。
- `unknown_after_send` 人工确认闭环。

### P3：高级能力

- 更完整的素材媒体缓存和跨任务素材复用。
- 更强的账号画像和 AI 长期话题编排。
- 更完整的代理风险分析和容量预测。
- 更细的容量自动建议和 worker 自动扩容策略。

### P6：大文件拆分和边界收敛

本轮已完成第一批低风险拆分：

- Gateway 数据契约：`backend/app/integrations/telegram/contracts.py`。
- Mock Gateway：`backend/app/integrations/telegram/mock.py`。
- Telethon 生命周期：`backend/app/telethon_lifecycle.py`。
- Task Center 字段配置：`backend/app/services/task_center/config_fields.py`。
- Task Center 创建预检：`backend/app/services/task_center/precheck.py`。
- Task Center 详情聚合：`backend/app/services/task_center/details.py`。
- Task Center 工具函数：`backend/app/services/task_center/utils.py`。
- Dispatcher 运行资源：`backend/app/services/task_center/runtime_resources.py`。
- Task Center 审核域：`backend/app/services/task_center/reviews.py`。
- Task Center 统计与重试：`backend/app/services/task_center/stats.py`。
- Task Center 配置归一化：`backend/app/services/task_center/config_normalization.py`。
- 运营中心默认规则：`backend/app/services/operations_center_defaults.py`。
- 运营中心监听域：`backend/app/services/operations_center_listener.py`。
- 运营中心风控/运行风险指标：`backend/app/services/operations_center_risk.py`。
- 运营中心规则指标：`backend/app/services/operations_center_rule_metrics.py`。
- 运营中心规则集版本管理：`backend/app/services/operations_center_rule_sets.py`。
- 运营中心工具函数：`backend/app/services/operations_center_utils.py`。
- Telethon 内容采集：`backend/app/integrations/telegram/telethon_content.py`。
- Telethon 媒体发送：`backend/app/integrations/telegram/telethon_media.py`。
- Telethon 目标解析：`backend/app/integrations/telegram/telethon_utils.py`。
- 前端领域类型：`frontend/src/app/types/`。
- 任务中心前端 view-model：`frontend/src/app/views/taskCenterViewModel.ts`。
- 任务中心前端向导组件：`frontend/src/app/views/TaskCenterWizardSections.tsx`。
- 任务中心详情弹窗：`frontend/src/app/views/TaskCenterDetailModal.tsx`。
- App Provider 默认表单状态：`frontend/src/app/context/defaults.ts`。
- App Provider 全局刷新：`frontend/src/app/context/refresh.ts`。
- App Provider 操作 loading hook：`frontend/src/app/context/actionRunner.ts`。
- App Provider 认证动作：`frontend/src/app/context/authActions.ts`。
- App Provider 账号动作：`frontend/src/app/context/accountActions.ts`。
- App Provider 消息动作：`frontend/src/app/context/messageActions.ts`。
- App Provider 系统配置动作：`frontend/src/app/context/systemActions.ts`。
- App Provider modal 编排：`frontend/src/app/context/modalState.ts`。
- App Provider 素材/关键词动作：`frontend/src/app/context/contentActions.ts`。
- 规则中心配置表单和规则 JSON 互转：`frontend/src/app/views/RulesCenterConfig.tsx`。

当前关键文件行数：

| 文件 | 当前行数 | 状态 |
| --- | ---: | --- |
| `backend/app/services/operations_center.py` | 780 | 监听域、默认规则、风控指标、规则指标、规则集版本管理和工具已拆；规则测试仍可继续拆 |
| `frontend/src/app/context.tsx` | 797 | 默认值、刷新、pending action、认证、账号、消息、系统配置、素材/关键词和 modal 编排动作已拆 |
| `backend/app/services/task_center/service.py` | 960 | 预检、详情、字段映射、reviews、stats、配置归一化已拆；CRUD、planner、recovery、role drain 仍需继续拆 |
| `backend/app/integrations/telegram/gateway.py` | 943 | 契约、Mock、Telethon 生命周期、内容采集、媒体发送、目标解析已拆；Telethon login/profile/channel action 仍需继续拆 |
| `frontend/src/app/views/TaskCenterView.tsx` | 873 | 创建/编辑向导、view-model 和详情弹窗已拆 |
| `frontend/src/app/views/RulesCenterView.tsx` | 892 | 规则配置表单和配置互转已拆 |
| `backend/app/services/task_center/dispatcher.py` | 727 | 运行资源已拆；claim/result handler 可继续拆 |

后续继续拆：

- `operations_center.py` 的规则测试和运营聚合报表。
- `task_center/service.py` 的 CRUD、planner/recovery、role drain。
- `context.tsx` 后续只做 selection state / React Query hooks 级别的渐进收敛，不再作为本轮 P6 阻塞项。
- `RulesCenterView.tsx` 的测试器、发布面板。

---

## 20. 验收标准

系统设计验收：

- 用户侧主流程只围绕账号、运营目标、任务、规则、风控和数据展开。
- 旧 Campaign、旧 review、旧订阅套餐等不进入新版主流程。
- 每个任务都能追踪到目标、账号、规则版本、执行项、结果和审计。
- 公开群消息不泄露后台配置、内部提示词、审核词或运营人员话术。

执行层验收：

- 多 Dispatcher Worker 同时运行时，同一 action 不重复执行。
- 单 worker 内可并发执行，但同一账号不被并发滥用。
- Redis 不可用时 Dispatcher 不 fail-open。
- 已调用 TG 但本地结果未知的 action 进入 `unknown_after_send`，不自动重复发送。
- Listener 压力增大时不拖慢发送 action。
- 页面能看到队列积压、最老等待时间、worker 心跳和主要失败原因。
- 压测报告能给出推荐 worker 数、并发数、claim limit、PostgreSQL 连接池和单机容量边界。

---

## 21. 总结

TG 运营管理平台的新版主线是：

```text
账号资源
  -> 运营目标
  -> 规则与风控
  -> 任务中心
  -> 执行中心
  -> 数据、归档、审计
```

当前系统已经具备可用的任务中心、监听中心、规则中心、风控基础、运营数据和执行层雏形。下一阶段重点不是继续叠加功能，而是把执行层从单 worker 综合 drain 升级为可横向扩容、可观测、可恢复的多 role worker 架构，并把任务创建体验从工程参数表单收敛为运营人员可理解的业务流程。

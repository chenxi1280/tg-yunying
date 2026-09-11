# 频道 / 群聊任务准入前置阶段设计

> **2026-09-08 全操作与完成量设计修复：** §16为逐账号准入、群4小时自然排程、动态频道依赖与主业务恢复的当前补充，统一引擎§19.65管理时间容量/独立性，群成员恢复设计§14管理管理员救活。仅设计修订，`resync=true`、`implementation_status=not_started_for_this_slice`、`production_status=unproven`；旧准入动作的发布记录不证明本补充已上线。

> **2026-09-08 当前设计修订：** §15 定义群准入文本四则、显式图片验证路由与自动处理闭环；独立持续调度、资源生命周期和积压恢复以 `unified-engagement-fulfillment-engine-prd.md` §19.60 为准。原“等待全部账号关注完成再开始主业务”不是当前合同。设计完成，尚未实现或生产验收。

## 1. 背景

频道浏览、点赞、评论和回复任务的真实执行前提是账号已经能够访问目标频道。当前只按全局可用账号和频道目标做预检，会出现两类问题：

- 所有账号都没有关注频道时，目标频道无法进入可执行状态，任务创建和启动链路不清晰。
- 只有部分账号关注频道时，任务容易只使用已关注账号，无法自动补齐账号范围内其他账号的关注动作。

新的产品口径是：运营人员创建的是频道互动任务，系统在启动时建立账号关注频道的前置阶段；已就绪账号独立执行主互动，不等待其他账号全部关注完成。

同一套准入口径也适用于 AI 活跃群目标群。AI 活跃群真实发送前不仅要“已入群”，还要完成群管理验证、必需频道关注、加减乘除验证 / 图片验证码 / 人工审批，并复检账号在目标群 `can_send=true`。否则硬目标任务会把未可发账号当成可用容量，导致 3 个线上 AI 活跃群无法按量完成。

## 2. 目标

- 支持通过 `@username`、公开频道链接、邀请链接或已知 peer id 手动添加频道目标。
- 邀请链接包含用户从 Telegram 频道“加入/邀请”入口复制的 `https://t.me/+...`、`https://t.me/joinchat/...`、`telegram.me/+...` 等链接。
- 频道目标可以先进入运营目标库，不要求所有账号已经关注。
- 创建并启动频道浏览、点赞、评论或回复任务时，在启动事务内一次性为任务完整有效账号范围生成 `ensure_target_membership` 前置关注动作，不等待 Planner、来源帖子或 AI 内容；保存草稿不创建、不执行关注动作。
- 彻底废弃公开频道浏览免关注豁免（`not_required_public_view`）与已授权频道免关注豁免：所有频道互动任务必须严格基于已确认关注的账号执行。
- 关注频道动作必须按随机打散、时间步长分摊、均匀抖动和限速排程执行，避免账号集中操作触发 Telegram 风控。
- 明确频道关注排程时间窗口：**频道互动任务每批随机选取 10～24 小时的总排程窗口，创建并启动时全量落库后逐步执行**；验证、审批、FloodWait 和运行期并发等待单独展示，不承诺远端在排程窗口内完成。
- 主互动阶段（浏览、点赞、评论、回复）只使用已确认关注成功或原本已关注的账号。
- 频道浏览 / 点赞 / 评论 / 回复在运行时再次发现账号未关注、未加入或无法进入关联讨论区时，必须自动生成准入动作并延后当前互动，严禁未关注账号直连网关。
- 任务详情能追踪每个账号的前置关注状态、失败原因、重试状态和对主任务容量的影响。
- AI 活跃群逐账号取得入群、验证、关联频道关注和`can_send`复检证据后才能参与发言；任务启动同时推进未就绪账号准备与已就绪账号主业务，不等待全体准备结束。
- 准入阶段必须覆盖图片验证码、加减乘除验证、人工审批和需要关注多个频道的群管理规则；不能把这些场景统一写成普通“加入失败”。
- AI 活跃群和转发目标群的加入账号前置任务必须在任务中心可见，并默认在 4 小时内排完所有待准备账号；超时只允许来自 FloodWait、慢速模式、人工审批或验证上下文不可读等账号级真实阻塞。

## 3. 非目标

- 不把“关注频道”做成运营人员必须单独创建的普通任务。
- 不新增独立前端业务功能或新的任务类型入口；现有任务创建、启动和任务详情只消费后端返回的阶段状态。
- 不承诺 peer id 一定可以完成关注。peer id 适合识别已同步目标；需要主动加入时优先使用 `@username`、公开链接或邀请链接。
- 不把群准入前置动作计入 AI 活跃群发送成功数；硬目标成功只认 `send_message success`。

## 4. 业务流程

```text
创建频道互动任务（浏览 / 点赞 / 评论 / 回复）
  -> 选择或手动添加频道目标
  -> 选择账号范围
  -> 仅校验输入与目标引用结构
  -> 保存草稿：仅保存，不执行关注；以下流程在创建并启动或显式启动时执行
  -> 启动事务读取频道目标和完整候选账号成员关系，不调用远端预检
  -> 为全部未关注账号生成 ensure_target_membership；已关注账号记跳过审计
  -> 同事务冻结随机 10～24 小时排程，重复请求复用原动作
  -> Worker 按冻结排程、抖动和限速逐步执行关注频道
  -> 每个账号关注结果独立落库并更新摘要，不等待全量关注完成
  -> 仅已关注账号进入浏览 / 点赞 / 评论 / 回复主互动阶段
  -> 调度执行时运行时复验成员关系，未关注账号自动延后等待准入
  -> 任务详情持续展示前置阶段和主阶段结果
```

AI 活跃群目标群准入流程：

```text
创建 / 恢复 AI 活跃群任务
  -> 选择目标群和账号范围
  -> 直接创建任务
  -> 启动后检查目标是否可准备、账号是否可用、是否需要发送权限
  -> Planner / 硬目标控制器发现缺口
  -> 先执行 ensure_target_membership
  -> 未入群：尝试加入目标群
  -> 需要验证：读取验证上下文
  -> 需要关注频道：逐个生成 / 执行必需频道关注动作
  -> 图片验证码：按 §15 明确且已验证的群准入图片路由识别并提交
  -> 加减乘除验证：解析最近验证消息题目，提交答案
  -> 人工审批 / 无法读取 / 低置信：进入待处理
  -> 验证提交后复检目标群 can_send
  -> 当前验证/关注/观察证据全部满足且 can_send=true：账号进入 AI draft 和 send_message 规划
  -> can_send=false：保留准入 blocker，不创建主发送动作
```

## 5. 频道目标录入与解析规范

手动添加频道时允许以下输入：

| 输入格式 | 示例 | 解析逻辑 | 关注方式 | 潜在风险与应对 |
| --- | --- | --- | --- | --- |
| `@username` | `@tech_news` | 去除 `@` 提取 `tech_news` | 通过 `JoinChannelRequest` 加入 | 频道改名后需重新解析更新 |
| 公开频道链接 | `https://t.me/tech_news` | 规范化提取 `tech_news` | 通过 `JoinChannelRequest` 加入 | 同上 |
| 私有邀请链接 | `https://t.me/+AbCdEf123` | 提取 invite hash `AbCdEf123` | 通过 `ImportChatInviteRequest` 加入 | 链接可能过期或撤销，失效时报 `TARGET_INVALID` |
| 旧式邀请链接 | `https://t.me/joinchat/AbCdEf123` | 提取 invite hash `AbCdEf123` | 通过 `ImportChatInviteRequest` 加入 | 同上 |
| 数字 peer id | `-1001234567890` | 原样保存 peer id | 仅供已存在 session 账号寻址 | 无法作为主动加入凭证，需配合链接或 username |

## 6. 关注频道技术执行细节设计

### 6.1 Telegram 客户端 RPC 调用协议

在 Telegram Gateway 层面，根据目标频道的类型分别采取以下调用链路：

1. **公开频道关注（Public Broadcast Channel）**：
   - 步骤 1：调用 `client.get_entity(username)` 解析频道实体 `entity`；
   - 步骤 2：校验实体属性 `entity.broadcast == True` 确认属于广播频道；
   - 步骤 3：调用 `client(functions.channels.JoinChannelRequest(channel=entity))` 执行加入；
   - 步骤 4：幂等处理：若捕获 `telethon.errors.UserAlreadyParticipantError`，判定为已关注成功，返回 `already_joined`。

2. **私有频道 / 邀请链接关注（Private Channel via Invite Link）**：
   - 步骤 1：提取 invite hash，调用 `client(functions.messages.CheckChatInviteRequest(hash=invite_hash))` 检查邀请有效性并获取目标信息；
   - 步骤 2：调用 `client(functions.messages.ImportChatInviteRequest(hash=invite_hash))` 执行加入；
   - 步骤 3：幂等处理：若邀请检查或加入返回 `UserAlreadyParticipantError`，视为已关注成功；若返回 `InviteHashExpiredError` 或 `InviteHashInvalidError`，则抛出明确的目标失效错误。

### 6.2 数据层持久化与关系写入

当账号成功执行关注动作后，必须原子写入以下数据状态：
- **`TgGroupAccount` 表**：
  - `tenant_id`：租户 ID；
  - `group_id`：关联 `TgGroup` 的主键 ID；
  - `account_id`：执行关注的账号 ID；
  - `status`：`"已关注"`；
  - `permission_label`：`"已关注"`；
  - `can_send`：沿用频道的 `can_send` 属性（普通频道为 `False`，若为投稿频道则根据配置设定）。
- **`OperationTarget` 与 `TgGroup`**：
  - 更新 `auth_status = GroupAuthStatus.AUTHORIZED.value`；
  - 更新 `updated_at = _now()`。

### 6.3 异常分类与自愈策略

| 异常类型 | Telegram 错误标识 | 处理策略 | 对任务与账号的影响 |
| --- | --- | --- | --- |
| **限流等待** | `FloodWaitError(seconds)` | 捕获等待秒数，将当前 action 设为 `pending`，`scheduled_at = now + seconds + random(5, 15)` | 仅延后当前账号，不阻塞同任务其他账号 |
| **频道上限** | `ChannelsTooMuchError` | 账号加入群组/频道总数达到 500 上限，标记账号 `ACCOUNT_LIMIT_EXCEEDED` | 该账号跳过当前任务准入，提示清理或更换账号 |
| **邀请失效** | `InviteHashExpiredError` / `InviteHashInvalidError` | 目标邀请链接失效，标记目标 `TARGET_INVALID`，更新任务 `last_error="频道邀请链接已失效"` | 任务进入 `membership_blocked`，需运营更新目标链接 |
| **频道私密/封禁** | `ChannelPrivateError` / `UserBannedInChannelError` | 账号无权访问或被目标拉黑，标记 `ACCOUNT_RESTRICTED` | 该账号跳过，其余正常账号继续执行 |
| **会话失效** | `AuthKeyUnregisteredError` / `UserDeactivatedError` | 账号 session 失效，更新账号状态为 `离线/失效` | 剔除出候选账号池，不影响其他健康账号 |

## 7. 排程时间窗口与抖动算法设计

### 7.1 排程时间窗口规范（在多少个小时内完成）

为在**任务启动时效性**与**Telegram 防风控安全**之间取得最佳平衡，系统制定分级时间窗口策略：

1. **频道互动任务（浏览 / 点赞 / 评论 / 回复）**：
   - **准入排程总窗口：每批在 10～24 小时之间随机选取，并冻结到每个 Action 的 scheduled_at**。
   - **设计依据**：该窗口用于打散首次尝试；私有频道可能需要审批，FloodWait、验证和网关等待可能使实际完成时间超出窗口。排程和真实准入结果分别展示，不保证规避 Telegram 风控。
   - **统一策略**：取消原 1～6 小时配置入口。旧请求/存量配置的 `membership_schedule_window_hours` 仅作为明确废弃字段接收并从新序列化配置排除，不能覆盖新策略；既有 Action 的排程不自动重写。

2. **AI 活跃群与群聊转发任务**：
   - **默认准入排程总窗口：4 小时（14400 秒）**。
   - **设计依据**：群聊准入需经历加入、读取验证消息、多模态视觉验证码识别/四则答题、必需频道关注以及管理员人工审核等复杂链路，因此采用 4 小时标准排程窗口。

上述窗口用于首次准备尝试，不是验证/审批完成保证。群准入配置预关注和当前群管提示的动态关注按§16依赖用途推进，不能仅因目标是channel便另建10～24小时等待；已存在合法关注事实直接复用。独立频道互动任务的冻结关注排程保持，不能借群准入用途提前该任务的既有Action。

### 7.2 抖动与打散算法（Jitter & Distribution Algorithm）

创建并启动时为全部候选账号规划；Planner 后续仅补新出现的缺失账号。每批 $N$ 个未关注账号按以下算法生成执行时间序列：

```text
输入：N 个未关注账号，最小间隔 G=15 秒，开始时间 S
N=0：返回空序列；N=1：返回 [S]。
N>=2：最短窗口 L=max(10小时,(N-1)*G)，最长窗口 H=24小时。
若 L>H：显式报 membership_schedule_capacity_exceeded；不丢弃账号、不压缩间隔。
按整秒随机选择 W=randint(L,H)，step=floor(W/(N-1))，jitter=min(floor(step*0.3),floor((step-G)/2))。
先随机打散账号；首账号时间=S，末账号时间=S+W；
其余第 i 个时间=S+floor(W*i/(N-1))+randint(-jitter,+jitter)。
相邻间隔至少 G，首末实际跨度在 10～24 小时内，窗口内时间不均匀；
重复启动/Planner不得重新抽取或提前既有 Action。
```

任务统计记录本批实际排程跨度 `membership_schedule_window_hours`（可为小数）及 `membership_schedule_policy=humanized_10_24h`；它是只读结果，不是可写配置。0/1 个未关注账号不强行等待 10 小时。审批、验证、FloodWait、跨任务并发可能推迟真实完成时间。

### 7.3 并发控制与账号级冷却

除了时间上的打散与抖动外，Dispatcher 在执行层实施两道保护防线：

1. **频道级全局并发上限（Channel Concurrency Cap）**：
   - 同一目标频道全局最多允许 **2 个账号同时执行加入操作**（频道固定并发策略为 2；不复用群任务的 `membership_max_concurrent` 排程参数）。
   - 网关调用前按 tenant + 已核验频道 peer 的 PostgreSQL 事务锁串行检查；统计该真实频道所有任务已调用网关、尚未结束的成员准入 Attempt，名额确认与本次 gateway_call_started 写入在同一事务提交。无结果/unknown 的 Attempt 保持占位，即使已记录本地 after_call 时间也要等待 reconcile；租约到期不能伪装为已结束。超过并发时 Action 回到 pending 并保留明确等待原因，不创建网关 Attempt。

2. **账号级操作冷却（Per-Account Operation Cooldown）**：
   - 单账号完成一次频道加入后，自动进入 **60 秒账号级操作冷却**，避免单账号在多任务并行场景下短时间内频繁加入不同频道。

## 8. 状态流转

频道互动任务增加前置阶段状态：

| 状态 | 含义 |
| --- | --- |
| `membership_pending` | 等待生成或执行关注频道动作 |
| `membership_running` | 正在按抖动节奏执行关注频道 |
| `membership_partial` | 部分账号已满足关注条件，部分失败或等待 |
| `membership_ready` | 至少一个账号可进入主互动阶段 |
| `membership_blocked` | 没有任何账号可关注或已关注，主互动阶段不能启动 |

主任务状态仍沿用任务中心现有状态。阶段状态放入任务统计、阶段摘要或详情字段中。

AI 活跃群详情还必须拆出验证子阶段：

| 子阶段 | 含义 |
| --- | --- |
| `join_pending` | 尚未加入目标群或加入动作待执行 |
| `verification_pending` | 已触发群管理验证，等待读取或提交 |
| `required_channel_pending` | 需要关注一个或多个频道，尚未全部完成；如验证消息带“我已加入 / 我已关注”确认按钮，关注后还需点击确认 |
| `captcha_solving` | 图片验证码正在多模态视觉模型识别或提交 |
| `arithmetic_solving` | 加减乘除验证正在解析和提交 |
| `manual_required` | 人工审批、低置信、上下文不可读或自动处理不可继续 |
| `can_send_checking` | 验证后正在复检发言权限 |
| `can_send_ready` | 账号已确认可在目标群发言 |

`membership_ready` 对 AI 活跃群的含义是“至少一个账号 `can_send_ready`”，不是“至少一个账号已加入群”。

## 9. 主互动阶段准入

浏览、点赞、评论和回复动作规划时必须使用目标频道过滤账号：

- 已关注或刚刚关注成功。
- 账号状态为可用。
- 账号容量未超限。
- 目标频道能力满足当前动作类型。
- 评论 / 回复还必须满足账号可以访问评论讨论区；没有准入关系的账号不得直接规划评论，应先生成关注 / 加入前置动作。

AI 活跃群发送动作规划时必须使用目标群过滤账号：

- 账号已加入目标群。
- 已处理当前群管理验证，包括图片验证码、加减乘除验证、人工审批或必需频道关注。
- 必需频道验证若带确认按钮，必须在关注全部频道后点击确认，再进入 `can_send` 复检。
- 账号-目标关系明确 `can_send=true`。
- 账号状态可用、容量未超限、未处于全局冷却或 TG FloodWait。
- 未满足上述条件的账号只能生成 / 继续准入动作，不能创建 `send_message`。

如果部分账号关注失败，默认策略是：

- 使用已关注成功账号继续执行主互动阶段。
- 任务详情展示容量缺口。
- 失败账号进入可重试队列或等待人工处理。

如果 0 个账号满足关注条件：

- 主互动阶段不能开始。
- 任务进入异常或等待处理状态。
- 错误原因明确显示为“没有账号成功关注目标频道”。

## 10. 创建后运行就绪摘要

保存草稿不调用频道 membership 预检或生成动作；创建并启动成功返回时，全部有效候选账号的前置动作已经同事务保存。此阶段只读取本地成员关系，不调用 Telegram 或 AI，详情读模型需要持续提供：

- 目标频道解析状态。
- 账号范围内已关注账号数。
- 需要关注账号数。
- 不可关注账号数和原因。
- 预计关注动作量。
- 预计前置阶段耗时区间。
- 关注阶段是否会造成主互动容量缺口。

历史 `/tasks/precheck` 输出只允许用于显式只读诊断或编辑建议，不得进入创建提交链路。Worker 启动时和每次规划动作前必须重新读取账号、目标和风控状态。

## 11. 异常处理

| 场景 | 处理 |
| --- | --- |
| 邀请链接失效 | 前置阶段阻塞，提示更换链接 |
| 账号已经关注 | 标记 `already_joined` |
| 账号被限制加入频道 | 记录账号受限，跳过或等待处理 |
| FloodWait | 单账号延后到可执行时间 |
| 目标不可访问 | 阻塞主互动阶段 |
| 评论运行时发现账号未关注 / 未加入 | 补齐前置关注动作，当前评论延后重试 |
| 评论运行时发现账号已准入但不可评论 | 标记账号级评论权限异常，只跳过该账号后续评论 |
| 评论运行时发现消息本身不可评论 | 标记消息不可评论，同帖评论跳过并展示“该消息无法评论” |
| 评论运行时出现未知 TG/API 错误 | 保留原始失败码、详情和尝试记录 |
| AI 活跃群目标群未加入 | 生成 `ensure_target_membership`，主发送延后 |
| 群管理 bot 要求图片验证码 | 读取可信验证上下文和图片，按 §15 显式图片路由识别；提交后取得当前 challenge 确认并复检权限/观察条件 |
| 群管理 bot 要求加减乘除验证 | 读取最近验证消息，解析题目，提交答案，提交后复检 `can_send` |
| 入群要求关注多个频道 | 为每个必需频道生成关注动作，全部成功后再复检目标群 `can_send` |
| 验证消息不可读或已过期 | 标记 `manual_required` 或重新触发准入，不伪造验证成功 |
| 已入群但 `can_send=false` | 标记 `target_can_send_blocked`，不创建主发送动作 |
| 部分账号失败 | 成功账号继续主互动，失败账号可重试 |
| 全部账号失败 | 主互动阶段不启动 |

## 12. 后端实现范围

建议落点：

- 运营目标：手动添加频道目标时保存原始输入、解析状态和可加入凭据。
- 任务中心详情：补充频道 membership 运行就绪和容量摘要。
- 创建/启动：共享启动入口同事务生成全部 `ensure_target_membership`，随后由 Worker 执行；Planner 只补缺失账号。
- Planner：AI 活跃群在 `send_message` 规划前先生成 / 复用 `ensure_target_membership`，硬目标缺口不能绕过准入。
- Dispatcher：执行关注频道动作，写入账号与频道目标关联状态。
- Dispatcher：执行目标群准入动作，记录加入、验证读取、多模态视觉识别、加减乘除验证提交、必需频道关注和 `can_send` 复检结果。
- 账号选择：频道主互动动作只选择已关注目标频道的账号。
- 账号选择：AI 活跃群主发送动作只选择目标群 `can_send=true` 的账号。
- 任务详情：展示前置阶段进度、失败原因和容量缺口。

## 13. 验收标准

- 所有账号都未关注频道时，创建并启动成功返回前已保存全部关注动作；无需帖子、AI 内容或第一轮 Planner。保存草稿没有动作。
- 部分账号已关注频道时，已关注账号跳过前置关注，未关注账号按抖动执行关注。
- 频道互动任务（浏览 / 点赞 / 评论 / 回复）严格要求账号完成关注，严禁因频道目标已授权或可发送而绕过前置关注阶段。
- 频道互动任务未关注账号随机打散，首末排程跨度随机落在 10～24 小时，相邻账号至少 15 秒；创建重放和后续 Planner 保持原时间。
- 前置阶段 0 个账号成功时，浏览、点赞、评论和回复动作不会被规划。
- 前置阶段部分成功时，主互动阶段只使用成功账号，并展示容量缺口。
- 关注动作具备随机顺序、抖动间隔、批次限速和 FloodWait 单账号延后。
- 运行时调度复核：未关注账号的 `view_message` 和 `like_message` 严禁直连 Telegram 网关，必须自动递延并补齐准入动作。
- 任务详情能看到每个账号的关注状态、失败原因、重试结果和主互动执行结果。
- AI 活跃群目标群未加入时，先生成 / 执行 `ensure_target_membership`，默认在 4 小时窗口内排完准入；远端审批/验证等待单独展示，不承诺 4 小时内全部通过，不会提前创建 `send_message`。
- AI 活跃群遇到图片验证码时，使用 §15 显式图片验证路由，记录题目/答案摘要、受控证据引用、置信度、提交结果和复检结果；不将图片或答案复制进通用日志。
- AI 活跃群遇到加减乘除验证时，记录题目读取、答案解析、提交结果和复检结果。
- AI 活跃群遇到需要关注多个频道时，每个频道关注动作独立展示；全部必需频道完成前不进入主发送。
- 已入群但 `can_send=false` 时，任务详情展示 `target_can_send_blocked`，主发送动作不创建。
- 入群完成、验证完成、必需频道关注完成且 `can_send=true` 后，账号才可进入 AI draft 和 `send_message` 规划。
- 频道评论运行时遇到未关注 / 未加入账号时，会补齐准入并延后评论；准入成功后该账号可以继续评论。
- 已准入账号被 Telegram 拒绝评论时，只标记该账号对频道评论区不可发言，不影响同帖其他账号继续评论。
- 频道消息本身不可评论时，详情明确显示“该消息无法评论”，并且同帖后续评论不会继续重试。
- 其他未归类错误在详情中保留原始失败码和原始返回摘要，便于查看尝试记录和 Trace。


## 14. 2026-09-07 五项审查修复合同

- Intake：本地代码与 PRD 审查的五项问题；分级 L2，范围为来源预约回收、来源节奏、频道排程窗口、真实频道并发和配置链路。Root Cause Grouping：预约事实/恢复节奏一组；排程数学/配置一组；网关并发一组。
- 来源恢复沿用原 period/计划数量计算的间隔，禁止 reaction=30、comment=180 秒强制压缩。保留已冻结预约间隔，不重写已有 owner 的 release。预约重用不能将已有合法时间槽提前。
- 恢复脚本必须显式 tenant 和 source state 集合。只取消绑定已终止 Action（failed/skipped/cancelled）、所有 Attempt 均未进入 Gateway 且已结束、无远端 fact 的 reserved 预约；pending/claiming/executing/retryable_failed、缺失关联数据、任何 unknown/call-started 证据均不自动回收。处理依据是事实失效，不是等待时间长。
- preview 只读，输出候选 ID、Action/Admission/State 版本、来源现值与排除候选后的预计游标，并生成完整快照 hash。apply 必须提供该 preview、actor、audit reference；锁定 Action -> SourcePacingState -> Admission 后重新计算并逐项比较，任何漂移零写入退出。保留所有 Action、Attempt、fact、owner 和旧预约审计信息，仅修改确证失效的预约及关联 source cursor。没有“全部生产数据”的默认 apply。
- Product Design Complete 自检：原始五项、前后端配置、窗口容量边界、真实 peer 身份、跨任务并发、unknown 保护、锁顺序、只读 preview、CAS apply 和定向 QA 已纳入上述合同；进入 dev。上线前必须完成本地回归与 Release Gate；本地修复不等于生产恢复。
- QA：有效未来预约不取消；真实游标预测；Action/Attempt/State/Admission 漂移拒绝；原间隔不缩短；0/1/2/容量边界账号窗口与最小间隔；三种频道任务配置创建/修改/回显与非法值；同 peer 跨任务最多两个未结束调用、不同 peer 独立；账号加入完成后的 60 秒冷却。
- Release Gate：候选改动审查、定向测试、前端构建、PostgreSQL 并发验证、master -> release -> Actions、部署 SHA 和真实成员关系/互动 E4 分别验证。恢复脚本仅交付工具，不因代码发布自动 apply。


### 14.1 本地验收记录（2026-09-07）

- `design_status=complete`，`local_qa=passed`，`production_status=unproven`。
- 171 个独立定向用例通过：原回归与新增边界共 166 个无 PostgreSQL 依赖用例，另有 5 个真实 PostgreSQL 用例（3 个频道并发/回收事务用例、2 个既有来源预约集成用例）；补充修改后针对相关模块重跑通过。
- PostgreSQL 使用本次独立创建、仅 Unix socket 可访问的临时实例，数据库名 `tg_yunying_test`；遵守测试 advisory lock，未使用 `.env` 中指向非测试库的 URL。
- 前端 `npm run build`（TypeScript + Vite）通过；新增 Python 模块的文件/函数长度检查和本次修复差异空白检查通过。
- 已验证两账号窗口上界、容量边界、最小间隔、1~6 小时配置创建/修改链路、三事务最多两次 Gateway admission、unknown 持续占位、60 秒账号冷却、只读 preview、锁竞争零写入、事实/日志保护、版本漂移拒绝、预计游标与实际回读一致。
- 此次交付为本地修复；尚未提交或发布，也未对生产运行恢复脚本，生产业务结果保持未验证。

### 14.2 发布全量回归的测试前置条件

- 2026-09-07 首轮发布 CI 揭示旧场景把频道级授权当作账号级关注。验证评论、点赞、浏览下游行为的测试必须显式写入对应账号的频道成员关系；不得恢复生产授权旁路或以 mock 代替准入。
- 已关注账号产生的 `ensure_target_membership/skipped/already_joined` 是前置审计记录。业务主动作的数量、重置、义务和事实链测试按 `post_comment`、`like_message`、`view_message` 查询，不把成员前置动作误认为主互动。
- 未关注场景单独验收：只生成关注前置动作，主互动延后；主互动没有 ExecutionAttempt、Gateway 调用和远端事实。PostgreSQL 测试清理包含本场景建立的成员关系与频道镜像，按外键顺序回收。

### 14.3 Antigravity 增量审查回流（2026-09-07）

- Intake / L2：修复评论可见性目标错配、探测异常误判不存在、来源间隔强制压缩、未结束预约无证据自动释放四项问题。账号来源跨日匹配及文案长度改动不属于本轮修复范围。
- 评论探测沿用冻结的 `actual_target_peer`，其次为 `discussion_peer_id`，仅在二者缺失时沿用历史 `channel_id`。Gateway 只查询传入的精确 peer/message identity，禁止以同 ID 猜测当前关联讨论组；错误与未知必须返回失败/未知，不能生成不存在或可见的肯定事实。空消息列表及 Telegram MessageEmpty 为指定身份不存在。
- 来源间隔继续使用原周期/计划数量及已冻结预约值，禁止点赞 30 秒、评论 180 秒封顶，来源尾部也不得统一截断为 180 秒。
- 查询 `call_started/remote_unknown` 的前次预约只读业务状态，不自动改为 finished；缺少 Gateway 时间不构成未执行证明。合法 pre-Gateway 终止由原正常结算流程关闭；Action unknown 或 Attempt result_unknown 均保持 remote_unknown，不受缺失时间字段影响。保留 autoflush=False 下先 flush 后查询的修正，使正常结算能够读到刚建立的预约。
- 反向检查：可见性肯定结果会更新 Action、履约及远端事实；否定结果会结算为被拦截。来源预约在重试入口检查并可能复用，因此错误释放会影响后续执行。不得用修改测试期望值接受与上述合同冲突的行为。
- Product Design Complete：四项触发条件、正常与失败路径、存量 unknown、前端无契约变化、无迁移、QA 和发布影响均已核对，design_status=complete。验证覆盖双 peer 同 ID 冲突、探测异常、空消息、原间隔/预约尾部、缺时间戳的 unknown 与仍在执行的预约、正常 pre-Gateway 结算与重试。

- 本地验收：145 项定向测试通过（主回归 131 项、来源复用/延后/重排 14 项），Python 编译与 `git diff --check` 通过；新增回归文件 145 行、函数不超过 50 行。三组审查反例已转为自动断言，另覆盖 Telegram MessageEmpty 不得误判可见。账号来源/文案长度代码经原快照 hash 核对未改动。此记录仅证明本地修复，未提交或发布；此前生产并发操作协调状态尚未解除。

### 14.4 汇总发布时的账号来源日归属修正（2026-09-07）

复核待提交的来源修复时发现，按 `release_not_before_at/created_at/scheduled_at` 枚举多个日期再选择含账号的 participation plan 会把延迟后的发送混入其他任务日。发布合同改为：优先使用 Action 显式绑定并通过 tenant/Task 校验的 TaskDayLedger；点赞无显式 ledger 时使用义务冻结的 pacing_due_at，再使用 Action 冻结 pacing_due_at；仅无冻结日的原兼容记录保留 scheduled_at 解析。可变 release 时间不改变来源日，不跨日搜索“能匹配账号”的计划。义务 tenant/Task/epoch 不匹配显式失败，不能借其他 source/day 的成员计划放行。专项测试须证明跨午夜推迟仍选原日、另日计划不能填补原日缺失、伪造 ledger/义务归属被拒绝。此前同一来源相册子消息匹配合同保持。Product Design Complete 后进入 dev；此修正属于本次汇总发布的账号来源修复，不修改业务配置。

### 14.5 新版评论频道关注旁路修复（2026-09-07）

- Intake / L2：本地审查发现 grounding 评论账号选择及执行前校验跳过频道关注。修复范围为这两个入口，保留讨论组准入合同及用户已有多账号分组改动。
- 产品合同：频道关注和讨论组可发送分别验收。新版评论候选账号必须已关注频道，频道 `can_send=false` 不代表讨论组不可发；非 unified 账号选择同样按频道成员关系过滤，使用 `require_send=false`。
- 执行合同：`grounding_enrollment_id` 不能豁免频道成员检查。已排队账号的频道成员关系缺失时，复用进行中的关注动作或创建 `ensure_target_membership(require_send=false)`，当前评论明确 pending 等待，不能创建评论 Attempt 或进入评论 Gateway；恢复成员关系后再次进入独立的讨论组权限校验。
- 反向检查：Planner 的任务级 gate 只证明至少一个账号可用；不能替代逐账号过滤。现有 `discussion_send_blocker` 验证讨论组事实，不能替代频道关注。旧评论 `require_send=true` 行为保持，租户和频道身份校验保持，关注动作沿用已有去重和运行期并发控制。
- Product Design Complete：原始触发、部分已关注、排队后成员关系移除、重复调度、关注恢复、讨论组不可发、旧路径兼容均纳入 QA；无 API、前端和数据迁移变化。`design_status=complete`，进入 dev。本轮仅本地修复验证；生产状态须经独立 Release Gate 和真实成员/评论事实验证。
- 本地 QA：新增 7 项回归在修复前暴露候选过滤及执行前放行问题，修复后全部通过；新版评论计划/讨论组准入共 43 项通过。最终合并回归 183 passed / 1 failed；唯一失败为并发新增的 `test_account_online_state_task_accounts_supports_account_group_ids` 导入不存在的 `_task_accounts`，不属于本轮评论修改，未修改该测试或在线状态代码。本轮变更 Python 编译、修改函数不超过 50 行和定向 `git diff --check` 均通过。`local_comment_qa=passed`，整体工作区未全绿，未提交、未发布，`production_status=unproven`。

### 14.6 多分组账号配置 account_group_ids 全链路补齐与积压下线工具（2026-09-07）

- **背景与根因**：线上频道任务（点赞、浏览、评论）统一使用多分组配置 `{'selection_mode': 'group', 'account_group_ids': [1, 2, ...]}`。然而 `candidate_accounts_for_config`、`precheck._precheck_candidate_accounts`、`account_online_state._configured_online_accounts`、`operations_center_listener._listener_accounts_for_group`、`search_rank_deboost_planner._apply_rank_account_selection`、`service._rank_deboost_selected_pool_ids` 以及 `AccountConfig` / `RecommendTaskAccountsRequest` 只检查单数 `account_group_id`。导致：
  1. 所有多分组任务候选账号计算返回 0，触发 `membership_blocked`；
  2. 队列头部堆积了数千条旧的过期 Action，Dispatcher 优先按照 scheduled_at 扫描历史过期动作，导致大面积 `pacing_claim_deadline_exceeded` 进而饿死当前正常时间窗口的任务。
- **修复措施**：
  1. `schemas/task_center.py`：`AccountConfig` 与 `RecommendTaskAccountsRequest` 增加 `account_group_ids: list[int] = Field(default_factory=list)`，并在 validator 中放行 `account_group_ids` 非空的分组模式。
  2. `channel_membership.py`、`precheck.py`、`account_online_state.py`、`operations_center_listener.py`、`search_rank_deboost_planner.py`、`service.py`：统一支持 `account_group_ids` 列表，兼容回退 `account_group_id`，筛选满足任意指定分组的有效账号。
  3. `scripts/abandon_channel_historical_backlog.py`：提供安全下线频道历史积压的维护脚本，按截止时间通过 `settle_fact_first_action_before_gateway` 进行合规安全下线（标记为 skipped 并同步冲销/对账），释放被历史锁定的账号容量与调度窗口。
- **QA 验收**：
  - `backend/tests/test_task_account_pool.py` 新增 4 个针对性单测覆盖 schema 校验、precheck 候选账号匹配、在线状态多分组账号提取，全部通过。
  - 全量 120 项回归测试 100% 通过。

### 14.6 创建并启动即全量关注排程（2026-09-07）

- Intake / L2：用户确认草稿不执行；创建并启动时全量安排关注。最新指令使用拟人化 10～24 小时随机排程，覆盖此前“尽快连续执行”和 2 小时窗口。
- 全量指现有业务资格和账号选择合同内的全部账号，不按 max_concurrent、每日参与抽样或帖子数量截断；未关注逐账号 pending，已关注逐账号 skipped 审计。无匹配账号保留 membership_blocked，不伪造完成。失效会话/限流仍走原明确失败及恢复合同。
- 创建并启动 API、显式启动共用启动事务；在当前生命周期和目标 scope 确定后生成动作。幂等创建重放不复制、不重排；启动失败回滚动作，保留创建成功/启动失败的正式合同。旧评论按原 require_send 合同准备，新版评论只要求频道成员关系，讨论组权限独立验证。
- 定时任务创建并启动后也先排关注；仅带本次启动 epoch 标记的频道 membership Action 可在 Task pending 时执行，主互动仍等 scheduled_start。草稿、暂停、停止、删除、退役和旧 epoch 不放行。Claim、确认及最终 Gateway 共享此精确判断；不能把所有 pending 动作放开。
- 反向检查：当前启动仅建账本，关注依赖 Planner；当前运行状态门禁会拦住定时任务关注。需同时补启动物化和精确准入判断，保留 route、账号来源、目标身份、租约、unknown 和最终生命周期锁。
- Product Design Complete：创建/启动事务、完整账号范围、0/1/大批量、无帖子、已关注、草稿/定时/暂停/旧 epoch、去重重放、随机边界、废弃配置回显及回滚均覆盖；无新增表或生产迁移，前端删除旧窗口输入并展示新只读排程。design_status=complete，进入 dev。
- 审查回流 / resync：长窗口内暂停后恢复必须继续未调用关注动作。只重绑本任务先前启动 epoch、当前账号范围及同目标引用版本的 pending 行；要求零 Attempt、零 Gateway journal、零远端 fact、executed_at 为空，锁行后核对。保留 Action ID、原随机间隔及审计；若最早时间已过，整批等量顺延到当前时间，不压缩、不重新抽样。已有调用/unknown/失败/其他目标/移出范围的动作不重放，仍由原恢复合同处理。补充暂停恢复及证据排除回归后重新验收。
- 本地验收：263 项无 PostgreSQL 依赖定向用例全部通过（36.90 秒），覆盖三类任务正式创建/启动、草稿、无帖子、完整分组账号范围、幂等排程、定时任务成员阶段、生命周期拒绝、启动事务回滚、暂停恢复、调用证据排除、随机窗口/最小间隔/容量边界及评论原路径。另在独立本地 PostgreSQL `tg_yunying_test` 通过 6 项真实事务/并发用例（6.20 秒），包括定时成员 Claim/Gateway、并发暂停、恢复行锁阻止迟到 Attempt、退役最终锁；实例仅 Unix socket 访问并已停止。前端 TypeScript/Vite 构建与 `git diff --check` 通过。`local_qa=passed`，未提交或发布，`production_status=unproven`。原 §14.1 的 2 小时窗口验收为历史记录，当前策略以本节及 §7 为准。

### 14.7 启动与生命周期目标汇总事务身份

创建/启动/删除在同一事务中多次刷新同一 tenant/target 的运行汇总时，必须复用事务内尚未 flush 的 TargetRuntimeSummary，避免关闭 autoflush 时重复 INSERT 违反唯一键。不得吞掉唯一键异常或伪造汇总成功；事务仍由既有生命周期提交边界管理。回归以同一 Session 两次刷新对象身份一致、最终仅一行及真实 PostgreSQL 并发删除验证。

### 14.7 历史积压工具补齐发布（2026-09-08）

- Intake / L2：用户要求提交并按流程部署遗留工具；范围仅代码发布，不执行生产积压清理。
- 反向检查：既有脚本复用正式 safely_not_executed 结算，但候选查询后等待行锁期间 scheduled_at 可能变化；锁定查询必须重复任务类型、状态、截止时间、任务范围和 Gateway 未开始条件，并刷新 ORM 对象。远端 unknown 由正式结算函数显式拒绝。
- 产品合同：默认 preview 零写；apply 按批事务，已提交批次保留，当前批失败回滚并显式报错；输出候选数与实际结算数，锁冲突跳过不计完成。无自动调度、无 Telegram 调用、无迁移。
- QA：覆盖 preview 零写、真实结算 fact/状态、重复 apply、截止边界及 Gateway 已开始排除、候选后重新排程排除。design_status=complete，进入 dev；部署验收为脚本存在、CLI 可加载和生产 SHA/运行健康，不将发布等同积压清理完成。

### 14.8 关注补齐与历史准入物理占用修复（2026-09-08）

- Intake / L3：用户要求把线上六频道账号关注补齐完成。08:31只读生产d3559c27显示，当前关注Action大量等待account_membership_inflight_wait；历史群准入约2.4万条result_unknown/closed_unknown被一律计作在途。原Gateway结果journal仍在，但补偿复检用Action结果覆盖Attempt快照，丢失原transport ACK。基线范围为tenant1频道6、19、2765、2806、5911、5981及其当前运行浏览/点赞/评论任务；完整账号范围按正式资格与分组合同计算，不减少分母验收。
- 设计修正：物理调用是否结束与业务结果是否已知分别判断。原Attempt明确transport ACK，或同tenant/action/attempt/account/epoch及冻结请求身份/目标hash匹配、观察时间不早于call-issued、双结果hash有效的原recorded Gateway结果回执，可证明该原调用已返回；该回执来自同步Gateway返回后的结果保存路径，remote_mutation_state=unknown不等于transport仍运行。显式未确认取消且没有后续终止证明、缺失/冲突回执、归属或hash不一致时仍占物理容量，不能凭after_call_at、closed_unknown、租约过期或worker缺失释放。
- 业务防重：同账号同目标的原unknown即使transport结束仍等待原权威对账，禁止重放；其他目标只解除已证实结束的物理占用，旧Action/Attempt状态、结果、账本和业务去重不改写。目标身份缺失仍阻塞，不能按名称推断不同目标。成功后60秒账号冷却、同频道两个物理调用上限及冻结10～24小时排程保持。
- 复检写回必须合并保留原Attempt的请求/终止证据，不再用Action结果整体替换。当前只修复未知成员复检的成功释放、失败、超时和连接错误路径；不回填历史ACK或伪造远端事实。
- 反向验证：读取正式channel_membership_runtime、Gateway journal writer、_finish_execution_attempt及复检路径；原journal可作为调用返回证据但不能证明加入成功。同目标unknown继续阻塞；无回执旧调用单独通过原退出证据或对账路径处理，不删除未知记录。
- Product Design Complete：完整范围、物理/业务分离、同目标unknown、跨目标、租户/账号/epoch、请求三字段、双hash、时间、显式取消、0/1/并发上限、ACK丢失、原冷却和排程、无schema/API/前端变化均已纳入定向QA；design_status=complete，进入dev。发布遵循master→release→Deploy Production，发布与真实关注分别验收。
- 审查回流：复检Action可能携带旧请求/终止字段，合并时原Attempt请求三字段及transport_termination_*必须优先；stale成员执行恢复同样保留这些原证据，其他类型的stale逻辑不改动。生产67条缺证据均取得10个原容器正面退出日志，但旧退出CLI仅接收四类主动作。将同四类Task下的ensure_target_membership/ensure_channel_membership加入同一退出证明入口；所有原身份、原进程、精确集合、SHA/hash、无统一资源、审计和读回门禁保持。此扩展只追加结束证据，不改原unknown、成员关系或排程。同步统一引擎§19.59与运行手册后再次QA，design_status=complete。

### 14.9 调度批次被已完成评论的遗留动作阻塞（2026-09-08）
- 只读反查：评论旧待执行Action没有ExecutionAttempt，但相同comment obligation已由另一Action产生remote_message_observed并确认；旧Action过期结算错误要求重新打开履约，导致dispatcher整批回滚，阻塞关注。
- 合同：安全结算先核对当前Action未发生远端变更；正式ensure_action_obligation拒绝已关闭/重复履约后，保留其显式skipped和原因，只释放当前Action自己的前置资源，不产生新的safely_not_executed事实、不投影覆盖已确认事实、不重开或重绑赢家履约。资源所有权冲突仍显式报错。
- QA：已确认评论赢家保持confirmed、未知赢家保持unknown、重复待执行赢家不变；输家无Gateway/新增Attempt/RemoteFact，当前节奏预留释放；远端证据非false仍拒绝安全结算。产品设计、数据与并发反查完成，design_status=complete，转dev。

## 15. 群准入验证完整性与独立恢复合同（2026-09-08）

### 15.1 Intake、适用范围和状态

与统一引擎 §19.60 共享 `intake-2026-09-08-admission-dispatch-independence`，L3；本节 `design_status=complete`、`resync=true`、`implementation_status=not_started_for_this_slice`、`production_status=unproven`。用户要求机器人加群支持验证、加减乘除，同时不阻塞独立主业务；本节是设计修订，不是线上已支持声明。

生产 `89d837b8` 的 `_arithmetic_answer` 仅加减，`2×3`/`8÷2` 返回空；群准入 `_image_verification_provider` 按 ID 取首个可用供应商，现场选择 M2.5，独立于正文 M3 路由。最近一天验证记录主要为人工待处理，仅一条 challenge context_read，没有自动提交答案记录。没有证据说明这些待处理记录全是数学题，也没有仅凭上述选择证明 M2.5 的图片能力不可用；须分别验证分类入口和实际图片请求。

仅适用于平台已授权操作的目标群/频道准入。纯搜索点击的验证码继续走既有双 OCR/协议样本/换题合同，禁止接入本节 AI 路由。本文 §4、§8、§9、§11、§13 中原“加减”范围由本节扩为四则；来源可信性、人工审批、关注前置、群管确认和可见性合同保持。

### 15.2 题目身份、分类和提交幂等

1. 读取当前可信 bot 的原始消息/按钮与受控图片引用，按 tenant、submit account、canonical peer、当前 admission/requirement version、source message ID/fingerprint 绑定 challenge。沿用现有收件人精确归属和可信控制信号，不把广告、聊天正文或其他账号的题目作为验证指令。
2. 分类为按钮确认、文本四则、文本验证码、图片验证码、必需频道关注、人工审批或明确 unsupported；记录选中处理器及原因。出现模糊提示时读取原上下文后再分类，不因摘要未命中关键词直接永久转人工；若来源/收件人不确定，保留明确等待原因，不猜测。
3. 复用 `VerificationTask`、`TargetMembershipChallengeAttempt`、当前 `TaskGroupBotAdmission` 与 account-scoped admission facts；字段缺口用相应现有对象的版本化扩展，不复活历史 `GroupBotAdmission` 所有权模型。不以父 Task ID 重复创建同账号同目标同原始题目的远端提交。跨 Task 当前投影可以引用同一有权威身份的通过事实。
4. 每次提交前复读原消息/媒体 fingerprint、当前题目有效性和账号/target/epoch，CAS 取得同 challenge 的提交资格。答案提交/按钮 callback 各自具备 Attempt 与 Gateway journal。并发 worker、重复事件和 crash resume 不能重复发答案；已 call-issued/unknown 的原题只对账，不靠 retry_count 或 TTL 获得重发许可。
5. 机器人明确拒绝且明确允许重答时，才依原协议追加下一 attempt；换题只接受权威新 fingerprint 或原协议批准的 refresh，并保留旧题结果。unsupported、除零、过期和低置信显式暴露，不能猜答案、使用模型绕过确定性解析失败或私自触发机器人命令。

### 15.3 文本四则的确定性合同

- 从当前题目中唯一提取完整表达式，不能 regex 命中子串即提交；支持阿拉伯整数和已有中文整数表达、半/全角数字、空格、`+ - * / × ÷ ＋ －` 与“加/减/乘/乘以/除以”归一化。支持一元正负号、括号与通常的先乘除后加减；混合式必须完整消费，拒绝把 `2+3×4` 错算成 `2+3`。
- 计算采用受限语法解析和精确整数/有理数运算，不使用 eval、shell 或通用代码执行，不请求 LLM 猜算式。移除原纯因数值超过 9999 或结果为负而返回空的实现限制；输出遵守题目/机器人明确的答案格式。
- 整数结果按十进制规范输出；明确接受小数且可精确有限表示时输出精确小数；题目要求分数时输出约分结果。机器人要求整数但结果非整数、没有明确格式且结果非整数、无限小数未给分数/精度协议、除零或歧义输入均给可解释错误，不取整、不随意四舍五入。不得借本次补齐引入额外任意题目数值上限。
- 基础验收至少包含 `2+3=5`、`8-3=5`、`2×3=6`、`8÷2=4`、`2+3×4=14`、`(2+3)×4=20`、`2-8=-6`、中文“六乘以七=42”、全角“８÷２=4”；另覆盖除零、精确小数/分数格式、歧义题、多账号同 bot、嵌入链接/脚本、旧题和重复提交。

### 15.4 群准入图片验证的显式路由

- 为群准入图片识别建立明确 purpose `group_admission_image_verification`，沿用现有版本化 Provider route set/item、credential 及调用审计。这个 purpose 独立于正文生成和审核；不得按供应商 ID、列表顺序、名称关键词或正文默认路由隐式选模。
- 本次沿用用户确认的 MiniMax 供应商方向；M3 是待验证的图片路由候选，不以名称推断能力。发布前用实际 endpoint、model、凭据和图片请求完成能力/响应 schema/题目答案及延迟验证，再形成批准的 route revision；未通过时仅图片验证显示 `image_verification_route_unavailable`，不改选 M2.5、其他供应商或伪造可用状态。路由未验证不能宣称图片验证修复完成。
- 请求冻结 provider/model、route revision、challenge fingerprint、request hash、deadline 和既有 confidence policy。只消费最终答案/置信度，推理内容不成为答案；数学图片先得到完整表达式时走 §15.3 计算，直接给最终答案时仍核对当前题目要求和输出格式。低置信/schema 错误与网络结果不明分别持久化。
- 图片计算使用既有独立 Provider 资源；不持有等待审批/Telegram 回复的 dispatcher 槽，不把图片识别失败扩散到点赞/浏览。识别 API 的调用成本和 unknown 按原 Provider attempt 合同审计；不新增静默回退或模型投票。
- 证据只保存恢复需要的受保护原始引用、hash、格式/长度、所用处理器和结果状态；题目、图片、答案不复制到普通日志、Task stats、PRD 或批量导出。运营详情按既有权限查看受控证据，不持久化凭据。

### 15.5 可恢复阶段与就绪判定

阶段是现有准入/验证读模型的业务语义，不另建第二套总状态机：`join_pending → context_reading → challenge_ready → solving → submission_pending → awaiting_confirmation → can_send_checking → ready`；关注图、观察窗口和人工审批可形成各自依赖等待分支。每个等待记录原阶段、reason、版本、last_progress_at 和唤醒事件或 next_eligible_at；人工等待无确定时间时明确显示等待条件，不编造完成时间。

- 同账号的多个必需频道按既有 canonical identity 去重，独立依赖按原允许并发推进；确认必须等对应 requirement 集合完成。即使某条等待，其他没有依赖它的工作仍推进。
- 一个网络步骤结束即短事务记录结果；需要等待新提示/审批/观察窗口时结束当前本地工作，按统一引擎 §19.60 释放本地名额。恢复时从持久阶段和现存远端事实继续，不重新入群或重新交答案。
- 答案“已提交”不等于验证通过，`joined` 不等于 `can_send_ready`，Telegram `can_send=true` 也不能替代当前群管确认。需要 bot 确认的 challenge 必须有归属当前题目的通过证据，且所有必需频道/确认/原 C2 观察与权限复检满足，才能投影 ready；没有挑战的群按原 not-required/no-prompt 证据合同，不凭计时自动通过。
- 普通 `GROUP_PERMISSION_DENIED` 不能直接解释为验证码，更不能自动把人工禁言当题目求解。类型不明先补只读上下文；管理员审批或真实禁止发言保留相应人工/权限状态。
- 明确可自动解决的题目应进入对应 handler；转人工必须展示原始 typed 原因、已读上下文/当前题目有效性、是否提交和失败阶段。关闭自动验证或 AI 辅助开关时保留用户选择，不以修复为由强行开启。
- 任务聚合只对本 Task 的到期依赖汇总；部分账号 ready 时，主业务在自身来源/时间/容量允许下继续。主业务欠量、等待验证数量和当前验证处理量分开，绝不将准入 success 计入正文/评论/点赞/浏览。

### 15.6 QA、存量和验收

QA 使用真实入口证明“可信提示识别 → handler → 持久阶段 → 一次提交 → 权威确认 → 权限/观察复检 → 主动作”的连接，不能只测试算术 helper。覆盖文本和图片两条路径、同题重复事件/多 worker、submit 前换题、submit 后 DB 失败/unknown、模型不可用、关闭自动开关、审批/权限误分类、观察中断、删除/暂停/epoch 变化以及跨 tenant/账号/peer 错配。图片路由必须证明不再受 provider ID 排序影响，搜索回归必须证明 AI/VLM 调用数为 0。

历史待处理 VerificationTask 不能全量自动重试。先只读核对是否仍是当前题目、原提交/Attempt/journal/fact 是否存在、当前开关/授权、原目标/账号及世代；仅明确未提交且当前有效的项进入原阶段恢复。原题已过期、人工审批、unknown 和已通过项各自保留正式处理语义。任何存量 apply 使用统一引擎 §19.60.5 的精确 preview/hash/授权/readback 合同。

生产验收同时需要受控的真实四则和图片挑战通过证据，以及“准入仍在等待时，另一健康账号已完成主互动”的时间重叠证据。观察窗口内没有出现某种真实挑战时，该类型保持 unproven，不把本地用例、Provider 请求成功或人工确认作为自动通过 E4。日目标/覆盖/评论窗口/可见性仍按原任务合同验收。


### 15.7 前置任务同样不得分配给失效账号（2026-09-08）

统一引擎§19.64资格过滤在关注/入群/验证候选生成之前执行。Session失效、冻结、需重新登录或资格未确认账号不得因“必须先入群”而新建membership Action、验证消息、救援或内容生成任务；也不能用这些业务动作探测账号健康。已存在的前置工作先复核当前账号资格，明确未调用则按原owner收口；已call-issued/unknown保持原身份对账。只读健康/正式授权恢复走原账号流程；健康账号和其他目标继续正常准入。§19.64.6已完成上述账号资格入口的本地实现/定向QA，尚未发布；§15其余验证与独立调度仍按各自开发状态验收，旧前置任务排程/物理占用E4不证明新入口已经在生产生效。

## 16. 全操作拟人化与逐账号准入闭环（2026-09-08）

### 16.1 Intake和当前边界

共享统一引擎§19.65的`intake-20260908-throughput-humanization-design-repair`，L3；本节`design_status=complete`、`resync=true`、`implementation_status=not_started_for_this_slice`、`production_status=unproven`。保留§15四则、明确图片purpose、可信题目和unknown规则；本节补齐时间与前后连接。独立频道关注10～24小时、群首次准备4小时、原冷却和真实并发仍有效。

### 16.2 每个账号的依赖图与就绪事件

1. 配置的`group_ai_prejoin_channel_ids`先取得全部当前成员事实，再加入canonical目标群；配置为空则没有此依赖。此处“全部”仅指当前账号的配置频道，不指全Task全部账号。
2. 入群后读取当前可信提示，按§15精确归属账号/原消息/requirement version；同一提示可有多个动作，不同无依赖步骤可并行准备。需要动态关注多个频道时按canonical identity去重，依赖它们的确认按钮只等自己的requirement集合；同账号实际RPC仍受账号互斥。
3. 配置关注与动态关注共享有权威身份的account-scoped成员事实，跨Task可以引用；不同Task不能针对同账号/频道同时发起相同副作用。引用的有效性至少核对tenant、account/auth generation、peer和当前成员状态。Task投影被删除或epoch变化不抹除成员事实，也不授权重复关注。
4. 若同账号/频道已有其他合法未调用关注owner，本依赖引用它及其原排程，不复制动作或把它提前到now；若已有called/unknown则只对账。没有现存owner时，群准入用途的关注按本准入会话节奏创建唯一owner，不另开独立频道任务的长批次。用途必须从真实依赖关系推导，不能在重试时修改payload标签绕过排程。
5. 答案已提交、按钮已点、joined、can_send分别是不同事实。需要群管确认时，必须取得当前题目/requirement通过证据，再满足全部关注、原观察窗和权限复检；没有题目的群沿用正式not-required/no-prompt证据，不计时自动通过。
6. ready投影提交时在同一事务写现有持久唤醒事件/标记；事件消费幂等，仅唤醒仍运行且当前epoch引用该账号/目标的Task。正常Planner从已有剩余义务继续，不等下一整批准备结束，不新增第二份coverage或数量。事件失败可由现有到期复检恢复，不通过再次入群/再答题重建ready。

### 16.3 群首次准备排程与验证会话

- 群4小时批次使用冻结seed进行账号打散和有界时间抖动，取代等间隔时间序列。N=0不建动作；N=1按当前首个合法时间尝试；N>1冻结批次起点S和跨度W=14400秒，首末候选为S/S+W。中间账号以`W*i/(N-1)`为中心，抖动幅度最多相邻步长的30%，同时受现有群准入最小间隔（如有）和窗口边界约束；seed包含tenant/task/epoch/batch/账号集合版本，用不同purpose分别生成顺序与抖动，重跑复用。该公式只管批次分布，实际资格、账号/peer容量、前置频道未完成仍明确延后，不把排程当已就绪。
- 现有最小间隔/可用时间与4小时无法同时满足时展示可安排与条件/不可安排数量，不压缩间隔、不承诺全体4小时完成。相同账号跨Task的预约冲突由统一时间线仲裁；首尾原候选不构成强制现在调用。
- 验证后续是已经开始的准入会话：读当前题、求解、提交、等确认、权限复检按依赖和当前题目截止推进，不重走首次批次。可在普通发言Session外收口同一准入会话，不因此给普通发言/点赞/浏览或新目标创建无限唤醒。
- 频道60秒冷却用于后续频道加入等原适用操作；不要把该数误用于不冲突的读题或确认。确实受账号/Telegram冷却限制且题目已来不及时，记录当前题过期或不可达，按bot允许的新题/重入协议处理；禁止为了抢时限取消限流或私自重发机器人命令。
- 文本求解/图片计算不持Telegram执行名额；等bot回复、人工审批、观察窗、冷却时持久化原阶段/reason/next eligible或唤醒条件，释放已结束的本地步骤名额。真实尚未结束的调用仍保持原硬占用，transport结束不等于业务成功。

### 16.4 自动验证的执行闭合

- §15完整表达式消费要求适用于自动分类与提交两处。不得只在helper支持乘除，却让分类入口将它永久转人工；`2+3×4`、`(2+3)×4`必须分别得到14、20，负数/除零/格式歧义按§15显式处理。
- 题目上下文只消费当前可信原消息与受控引用，不能合并历史无关题目后取第一个regex命中。提交前复读当前fingerprint、有效期、收件人及exact account/peer，替换题目使旧未调用提交失效；已经提交/unknown只对账，不能对同题再次发送。
- 群图片purpose独立于正文生成/审核和纯搜索OCR；未验证endpoint/model/响应格式时显示图片路由不可用，只影响相应图片题。关闭自动处理或人工审批选择保持；界面展示真实阶段、选择的handler、是否提交和通过证据状态。
- 网络返回答案或callback成功不直接ready。准入通过后才唤醒主业务；主业务发前再核验当前ready、账号资格与来源时效，发后按既有visibility规则计成功。

### 16.5 首日容量、故障恢复和界面

- 每Task分列：未到准备时间、依赖关注、当前题待处理、已提交等确认、人工审批、权限失败、救援中、ready及当前可发账号；同时显示各自数量/原因，不把所有等待合成“加群中”。
- ready前的账号不提供当前保证发送容量；给出有依据的预计区间或外部条件，按统一§19.65.3计算剩余Session可安排量。部分ready账号可继续主业务，任务原目标/历史分母不因暂时等待重写。
- 权限/成员失败进入群成员恢复设计§14的原因分支。恢复后仍走本节当前题与权限复检，不因管理员返回成功跳过bot；同义务Telegram unknown禁止补发。当前业务期内错过随机小窗只适用统一§19.65.4，不延长题目或主回复真实期限。
- 存量待处理题先精确只读preview：当前题、原提交/Attempt/journal、开关、账号/target/epoch及原owner。有效且明确未提交才恢复；旧题、已通过、人工审批、unknown分别保持正式状态，不能批量“自动重试全部”。

### 16.6 QA与交接

覆盖0/1/2/大批次稳定排程、跨日准备、同账号多频道/跨Task共享owner、动态关注不另排长窗口、普通发言休息时准入后续收口、同题重复事件/多worker/提交后DB失败/换题、图片路由故障和关闭开关、ready事件丢失恢复、暂停/停止/epoch变化、管理员救援返回，以及等待账号与健康账号主动作时间重叠。时间容量验收不能只看scheduled_at数量，自动验证验收不能只看helper。

产品设计已覆盖依赖用途、时序、身份/权限、持久唤醒、并发幂等、失败路径和前端语义；dev同步真实数据字段/入口索引后定向QA，生产需真实题目通过和主业务可见事实，未遇对应挑战保持unproven。无新增生产apply授权。

## 17. 发言资格硬约束、加群主动验证、小学四则算术与救援批处理专项闭环（2026-09-08）

### 17.1 核心原则与边界分立
1. **只分给可以发送的在线账号**：发信动作（`send_message`）与每日账本规划（`TaskAccountDailyCoverage`）严格执行资格前置过滤，仅向满足以下条件的账号派发任务：
   - 账号处于 `ACTIVE` 且具备可用 `session_ciphertext`，未被标记为 `cannot_send`、`session_expired`、`need_relogin`、`account_banned` 或 `account_offline`；
   - 在目标群的成员关联（`TgGroupAccount`）中 `can_send is True`，且无禁言标记；
   - 不存在待处理阻断的验证辅助任务（`VerificationTask`）；
   - 最近发后无可见性阻断或 Telegram 物理拒止（`ChatWriteForbidden`）。
   - **历史覆盖账本不得继续向失效账号派发新工作**：当账号出现不可恢复的禁言或 Session 故障时，当日 coverage state 立即置为 `abandoned_for_day` 并记录确切阻断原因，释放容量，不持续死刷。
2. **拟人化与准入闸门的边界分立**：
   - **拟人化**专职负责执行层面的表现：随机发送时间抖动、操作时间间隔（Jitter Delay）、内容与表情风格；
   - **准入闸门**专职负责物理与安全条件的客观验证：访问资格、发言权限、机器人限制条件。绝对禁止以“拟人化需要等待”为借口掩盖观察证据缺失的死循环。

### 17.2 加群后主动发言测试（Active Post-Join Verification Probe）
1. 账号加入目标群后，不能仅依赖 Telegram 底层 `get_permissions` 静态位掩码（无法感知机器人发后秒删与入群即禁言）；
2. 入群动作完成后，系统对需要准入核验的群执行一次主动发言可用性验证（Active Probe）：
   - 向目标群发送一条轻量测试验证消息；
   - 若外呼直接抛出 `ChatWriteForbiddenError`，立刻捕获并转入机器人验证码/加减乘除识别链路；
   - 若发送成功，快速复检该消息在群会话中的留存状态（确认未被群管机器人秒删）；
   - 确认留存后，方可在 `TgGroupAccount` 标记 `can_send = True` 并将准入状态推进为 `ready`；若被秒删或被限制，记录阻断事实并转入对应处置。

### 17.3 小学算术直接计算能力（四则运算与复合算式）
1. 升级本地数学算术题自动解析器（`membership_challenges.py`）：
   - 支持完整的四则运算符：加（`+`、`＋`、`加`）、减（`-`、`－`、`减`）、乘（`*`、`×`、`x`、`X`、`乘`、`乘以`）、除（`/`、`÷`、`除`、`除以`）、等号与问号（`=`、`等于`、`?`、`是多少`、`等于几`）；
   - 支持中文复合汉字数字解析（覆盖零、一至九、十、百、千、万，如“二十五” -> 25，“一百零八” -> 108）；
   - 支持括号与四则运算优先级计算（先乘除后加减，括号优先计算）；
   - 适配群内机器人常见算术题干模板（如 `5 x 6 = ?`、`请计算：12 + 4 * 3 等于多少`）；
2. 只解析完整的连续算式，支持嵌套括号、十进制小数以及中间为分数而最终为整数的四则运算；用精确有理数计算，最终整数范围保持 `[-999999, 999999]`。中文数字按“万”分节，`十二万=120000`，不能把大单位与十百千平级累加。
3. 不完整括号、非法操作符、除零、最终非整数或超范围算式不自动提交；禁止退回局部二元算式，也不能把失败题干里的数字当普通验证码。现有“需人工处理/verification_answer_missing”状态暴露失败。正确答案沿现有 `answer_source=rule` 审计提交，不外呼模型；提交后仍须权限/留存事实，答案提交成功不等于准入完成。

### 17.4 准入闸门死循环治理（重试上限与快速失败）
1. `MAX_OBSERVATION_GAP_RETRIES=3` 约束同一观察阶段连续三次真实读取失败，使用独立持久字段 `consecutive_observation_gaps`。`observation_version` 只表达观察版本，正常关注后复检、Session/观察面更新不消耗失败次数。
2. 失败计数与观察版本通过 CAS 同时写入，旧版本结果不能覆盖新观察。成功读取（包括合法空消息列表）、正常关注后复检、观察面更新及次日重开均清零连续失败次数；无消息本身不是失败。
3. 达到三次后当日终止，稳定原因 `observation_gap_limit_reached`，精确异常类型保存在 terminal evidence；UI/coverage 使用 `c2_observation_evidence_missing`，不能误称不可发送或任务永久失效。只对当前任务日安全未调用工作投影放弃，原成功与 unknown 事实不重写、不重放。
4. 次日由正式 Planner 准入恢复入口及 `evaluate_task_admission` 重新观察同 Task/account/target；同日不重开。次日重开校验账号、群与任务仍可执行、当前 epoch，清零计数并追加事实。旧日 coverage 保持原状态，当日账本才可继续；不能只在执行门加跨日分支却让 Planner 永远过滤 abandoned 账号。
5. 迁移只增加默认0的计数字段，不从 observation_version 推断历史失败、不自动复活历史工作。rollback 必须保留已经记录的非零计数，必要时仅前向修复。

### 17.5 管理员救援受控批处理与吞吐优化
1. 本次反查修正“同一批先claim五条”的实现方案：同一管理员的 legacy 救援仍只认领一个执行owner，未取得执行资格的其他候选保持 pending，不预先持有五份资源或制造 account_inflight_conflict。
2. 复用持久 `ContinuousDispatcher` 的完成通知补领下一条；仍在执行的管理员在候选筛选时跳过，其他管理员与正常业务继续。局部批次不等待其他慢动作结束，无新增批大小开关或硬编码1～2秒sleep；真实账号节奏、FloodWait、共享资源与远端unknown仍由现有正式门处理。
3. 连续五条是吞吐验收样例，不是五条并发claim或任务数量上限。验收必须通过真实资源预约与持久执行器证明：同管理员最大并发1、完成后预约释放并及时补领、未选中的Action不被推迟30秒、无关慢动作尚未完成时五条救援已推进。跨进程竞争仍遵守既有数据库/Redis/Gateway门，不能放宽互斥。
4. 管理员无权限、限流、传输unknown沿现有精确失败分类保留；本次不批量重试旧救援，也不修改线上任务配置。

### 17.6 本地修复审查与验收（2026-09-08 resync）

六项修复分为候选筛选（缺失or_导入）、算术完整性/中文数字、观察失败计数/当天终止，以及救援完成补领。无前端/API变更；既有状态展示消费明确blocker。§17.2主动测试消息属于原设计范围，本次六项修复不据此宣称已实现或已通过真实主动探测。开发交接、测试和Release Gate见 `docs/05-implementation/local-review-fixes-20260908.md`；设计已反查真实候选、Gateway提交、观察、Planner恢复和持续执行器入口，`design_status=complete`，进入dev。


## 2026-09-09 审批群私聊验证补充

公开group的新入群申请接入管理员机器人私聊数学callback协议，精确合同见`ai-conversation-humanization-and-group-bot-admission-prd.md`末节。InviteRequestSentError是已提交申请、尚未成为成员，不能标joined或按普通权限失败重发申请。新题绑定原账号/目标/管理员bot/申请前游标；一次callback后独立GetParticipant确认，再走发言权限与C2。未确认保留不可重放状态和pending_approval业务事实；历史unknown不重试。无迁移，resync=true。

## 2026-09-11 已冻结频道义务恢复排程

线上反例：点赞任务的未绑定 open 义务原 due 已到期、原 deadline 尚未到期，但其它义务的 release 位于 deadline 或远期尾部；旧恢复器将 max(release) 当成所有义务的下限，返回零个排程点，重复规划也无法恢复。

- 浏览/点赞恢复只作用于仍为 open、无 current_action_id 的既有冻结义务。保留原 due、ordinal、plan_total、epoch、原来源窗口与 deadline；原 release 只能保持或向后推进，已绑定 Action、已调用/未知事实和数量目标不改。
- 在原来源 advisory 锁内读取其它 owner 的具体冻结点。有效点采用 max(due, release)，原窗口以外的不可执行预约不占用窗口内空隙；历史行保留。未来点占用它自己的时间位置，不再将其最大值当作所有到期旧义务的统一下限。
- 未来已冻结的合法点保持；到期旧义务从不早于 now/原 release/原 due 的位置按原来源名义间隔和稳定 jitter 搜索空隙，与两侧预约保持双方间隔的最大值。无空隙则保留显式短缺，不延长窗口、不压缩间隔。新义务仍沿原 ordinal/cursor 合同；账号 Session 交集、账号预算、成员资格与正式 Gateway 准入继续独立校验。
- 同批一个义务已被推进到 deadline，不能让循环提前退出并跳过其它仍有窗口的义务。恢复不回拨这条已到 deadline 的义务。
- 验收：真实 PostgreSQL 来源锁与跨批预约读回；远期尾部及 deadline 哨兵不会饿死早期空隙；不同计划间隔、连续规划幂等、未来冻结点不回拨、不可执行义务不阻断其它义务、无空隙显式不足、新 ordinal 规则保持。

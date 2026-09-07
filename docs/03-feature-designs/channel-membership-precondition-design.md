# 频道 / 群聊任务准入前置阶段设计

## 1. 背景

频道浏览、点赞、评论和回复任务的真实执行前提是账号已经能够访问目标频道。当前只按全局可用账号和频道目标做预检，会出现两类问题：

- 所有账号都没有关注频道时，目标频道无法进入可执行状态，任务创建和启动链路不清晰。
- 只有部分账号关注频道时，任务容易只使用已关注账号，无法自动补齐账号范围内其他账号的关注动作。

新的产品口径是：运营人员创建的是频道互动任务，系统自动在启动前补齐账号关注频道的前置阶段。

同一套准入口径也适用于 AI 活跃群目标群。AI 活跃群真实发送前不仅要“已入群”，还要完成群管理验证、必需频道关注、加减验证 / 图片验证码 / 人工审批，并复检账号在目标群 `can_send=true`。否则硬目标任务会把未可发账号当成可用容量，导致 3 个线上 AI 活跃群无法按量完成。

## 2. 目标

- 支持通过 `@username`、公开频道链接、邀请链接或已知 peer id 手动添加频道目标。
- 邀请链接包含用户从 Telegram 频道“加入/邀请”入口复制的 `https://t.me/+...`、`https://t.me/joinchat/...`、`telegram.me/+...` 等链接。
- 频道目标可以先进入运营目标库，不要求所有账号已经关注。
- 启动频道浏览、点赞、评论或回复任务时，全部目标频道均要求任务账号范围内的账号先完成关注；未关注账号自动生成 `ensure_target_membership` 前置关注动作。
- 彻底废弃公开频道浏览免关注豁免（`not_required_public_view`）与已授权频道免关注豁免：所有频道互动任务必须严格基于已确认关注的账号执行。
- 关注频道动作必须按随机打散、时间步长分摊、均匀抖动和限速排程执行，避免账号集中操作触发 Telegram 风控。
- 明确频道关注排程时间窗口：**频道互动任务默认在 2 小时（可配置 1~6 小时）内排完候选账号首次关注动作**；验证、审批、FloodWait 和运行期并发等待单独展示，不承诺远端在排程窗口内完成。
- 主互动阶段（浏览、点赞、评论、回复）只使用已确认关注成功或原本已关注的账号。
- 频道浏览 / 点赞 / 评论 / 回复在运行时再次发现账号未关注、未加入或无法进入关联讨论区时，必须自动生成准入动作并延后当前互动，严禁未关注账号直连网关。
- 任务详情能追踪每个账号的前置关注状态、失败原因、重试状态和对主任务容量的影响。
- AI 活跃群启动和硬目标补量前，目标群必须先补齐入群、验证、关联频道关注和 `can_send` 复检。
- 准入阶段必须覆盖图片验证码、加减验证、人工审批和需要关注多个频道的群管理规则；不能把这些场景统一写成普通“加入失败”。
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
  -> 直接保存草稿或创建并启动
  -> 解析频道目标并核验候选账号成员关系
  -> 发现未关注账号：生成 ensure_target_membership 前置动作
  -> 按抖动和限速执行关注频道
  -> 汇总关注结果
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
  -> 图片验证码：使用多模态视觉供应商（MiMo/Mino 或 MiniMax）识别并提交
  -> 加减验证：解析最近验证消息题目，提交答案
  -> 人工审批 / 无法读取 / 低置信：进入待处理
  -> 验证提交后复检目标群 can_send
  -> can_send=true：账号进入 AI draft 和 send_message 规划
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
   - **默认准入排程总窗口：2 小时（7200 秒）**。
   - **设计依据**：该窗口用于打散首次尝试；私有频道可能需要审批，FloodWait、验证和网关等待可能使实际完成时间超出窗口。排程和真实准入结果分别展示，不保证规避 Telegram 风控。
   - **可配置性**：支持在任务高级配置中通过 `membership_schedule_window_hours` 覆盖，取值范围为 `1 ~ 6` 小时，默认值为 `2` 小时。

2. **AI 活跃群与群聊转发任务**：
   - **默认准入排程总窗口：4 小时（14400 秒）**。
   - **设计依据**：群聊准入需经历加入、读取验证消息、多模态视觉验证码识别/加减答题、必需频道关注以及管理员人工审核等复杂链路，因此采用 4 小时标准排程窗口。

### 7.2 抖动与打散算法（Jitter & Distribution Algorithm）

当 Planner 检测到 $N$ 个候选账号尚未关注目标频道时，按以下算法生成执行时间序列：

```text
输入：N 个未关注账号，窗口 W 秒，最小间隔 G=15 秒，开始时间 S
N=0：返回空序列；N=1：返回 [S]
若 (N-1)*G > W：显式报 membership_schedule_capacity_exceeded，展示数量及窗口；
不丢弃账号、不缩短间隔、不自动延长窗口，由运营调整账号范围或 1~6 小时配置。
N>=2：step=W/(N-1)，jitter=min(step*0.3, (step-G)/2)
先随机打散账号；第 i 个时间=S+i*step+uniform(-jitter,+jitter)，
首账号 offset 范围 [0,jitter]，末账号 offset 范围 [-jitter,0]。
由构造保证全部时间在 [S,S+W] 内，相邻时间至少 G；无需后推修正。
```

`membership_schedule_window_hours` 是频道三类任务共同的严格整数配置，默认 2，范围 1~6；创建、修改、任务详情回显和高级设置统一使用此字段。未配置的历史任务使用默认值；非法已存配置明确报错。修改只影响以后创建的准入动作，不重写既有排程。

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
| `arithmetic_solving` | 加减验证正在解析和提交 |
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
- 已处理当前群管理验证，包括图片验证码、加减验证、人工审批或必需频道关注。
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

任务创建不调用频道 membership 预检。创建成功并进入启动/运行阶段后，详情读模型需要持续提供：

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
| 群管理 bot 要求图片验证码 | 读取验证上下文和图片，使用多模态视觉供应商（MiMo/Mino 或 MiniMax）识别，提交后复检 `can_send` |
| 群管理 bot 要求加减验证 | 读取最近验证消息，解析题目，提交答案，提交后复检 `can_send` |
| 入群要求关注多个频道 | 为每个必需频道生成关注动作，全部成功后再复检目标群 `can_send` |
| 验证消息不可读或已过期 | 标记 `manual_required` 或重新触发准入，不伪造验证成功 |
| 已入群但 `can_send=false` | 标记 `target_can_send_blocked`，不创建主发送动作 |
| 部分账号失败 | 成功账号继续主互动，失败账号可重试 |
| 全部账号失败 | 主互动阶段不启动 |

## 12. 后端实现范围

建议落点：

- 运营目标：手动添加频道目标时保存原始输入、解析状态和可加入凭据。
- 任务中心详情：补充频道 membership 运行就绪和容量摘要。
- Planner：频道互动任务启动后先生成 `ensure_channel_membership` 前置动作。
- Planner：AI 活跃群在 `send_message` 规划前先生成 / 复用 `ensure_target_membership`，硬目标缺口不能绕过准入。
- Dispatcher：执行关注频道动作，写入账号与频道目标关联状态。
- Dispatcher：执行目标群准入动作，记录加入、验证读取、多模态视觉识别、加减验证提交、必需频道关注和 `can_send` 复检结果。
- 账号选择：频道主互动动作只选择已关注目标频道的账号。
- 账号选择：AI 活跃群主发送动作只选择目标群 `can_send=true` 的账号。
- 任务详情：展示前置阶段进度、失败原因和容量缺口。

## 13. 验收标准

- 所有账号都未关注频道时，可以创建频道互动任务；启动后先执行关注频道前置阶段。
- 部分账号已关注频道时，已关注账号跳过前置关注，未关注账号按抖动执行关注。
- 频道互动任务（浏览 / 点赞 / 评论 / 回复）严格要求账号完成关注，严禁因频道目标已授权或可发送而绕过前置关注阶段。
- 频道互动任务的未关注账号排程默认在 2 小时窗口内完成抖动生成，相邻账号调度间隔满足最小安全间隔（$\ge 15\text{s}$）。
- 前置阶段 0 个账号成功时，浏览、点赞、评论和回复动作不会被规划。
- 前置阶段部分成功时，主互动阶段只使用成功账号，并展示容量缺口。
- 关注动作具备随机顺序、抖动间隔、批次限速和 FloodWait 单账号延后。
- 运行时调度复核：未关注账号的 `view_message` 和 `like_message` 严禁直连 Telegram 网关，必须自动递延并补齐准入动作。
- 任务详情能看到每个账号的关注状态、失败原因、重试结果和主互动执行结果。
- AI 活跃群目标群未加入时，先生成 / 执行 `ensure_target_membership`，默认在 4 小时窗口内完成准入，不会提前创建 `send_message`。
- AI 活跃群遇到图片验证码时，使用多模态视觉供应商（MiMo/Mino 或 MiniMax）识别并记录验证码图片、答案、置信度、提交结果和复检结果。
- AI 活跃群遇到加减验证时，记录题目读取、答案解析、提交结果和复检结果。
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

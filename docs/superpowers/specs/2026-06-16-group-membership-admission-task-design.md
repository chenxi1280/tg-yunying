# 群聊准入任务设计

## 背景

AI 活跃群任务当前已经内置目标准入能力，但它的主要目标是发言活跃。天津群的线上问题说明，入群准备和活跃发言需要拆开验收：群链接能打开，不代表所有账号都能自动入群并发言；账号已提交入群申请，也不代表任务已经具备可发能力。

新增独立任务类型“群聊准入任务”，目标是在指定时间窗口内，把选中账号分组里的账号推进到“已入群且真实发言成功”。它不负责正式活跃内容，只负责把账号准备到可发状态，并把审批、验证码、群权限和账号不可用等阻塞清楚暴露出来。

## 设计目标

- 创建任务时选择目标群聊、账号分组、执行时间和执行节奏。
- 不配置加入人数；任务开始时锁定所选分组的账号快照。
- 快照内全部账号都必须完成“入群 + 真实测试发言成功”，任务才算达标。
- 测试发言使用 AI 生成的低风险自然短句，发送成功才证明账号能发言。
- 管理员审批、群解除限制等外部阻塞进入“需人工处理”，不被系统伪装成成功。
- 复用现有 `ensure_target_membership`、前置关注、验证码和 membership 明细能力。

## 非目标

- 不替代 AI 活跃群任务，不生成正式活跃对话。
- 不自动换号补齐；失败账号必须显式展示原因。
- 不把任务执行期间新增到分组的账号动态加入当前任务。
- 不绕过 Telegram 或群管理员权限限制。

## 任务创建

新增任务类型：`group_membership_admission`，中文名“群聊准入任务”。

创建表单字段：

| 字段 | 说明 |
|---|---|
| 目标群聊 | 从运营目标中选择一个群聊 |
| 账号分组 | 选择一个或多个账号分组 |
| 执行时间 | 开始时间和结束时间 |
| 执行节奏 | 默认均匀铺开，可配置最大并发、每分钟处理数 |
| 测试发言 | AI 生成随机自然短句 |
| 删除测试消息 | 创建时可选，默认不删除 |

任务创建后不立即锁定账号。到达开始时间时，系统读取账号分组并生成快照。快照生成后，当前任务只处理快照内账号；分组后续新增或移除账号不影响本次任务。

## 配置结构

```json
{
  "target_operation_target_id": 485,
  "account_group_ids": [1, 2, 3],
  "schedule_start_at": "2026-06-16T20:00:00+08:00",
  "schedule_end_at": "2026-06-16T21:00:00+08:00",
  "pacing": {
    "mode": "spread",
    "max_concurrent": 5,
    "per_minute": 10
  },
  "test_message": {
    "mode": "ai_random",
    "min_chars": 3,
    "max_chars": 12,
    "delete_after_send": false
  }
}
```

AI 测试发言要求：

- 长度默认 3 到 12 个字。
- 风格为低风险日常语气，例如签到、刚看到、收到、来了。
- 生成失败时，该账号进入“AI 发言生成失败”，不得走假成功。

## 执行流程

每个账号按同一条链路推进：

1. 检查账号 session、账号状态和执行资格。
2. 检查目标群是否已有成员关系。
3. 如果群要求前置关注频道，先自动关注频道。
4. 发起入群或确认已入群。
5. 如果出现按钮、验证码、图片验证码，复用现有自动验证链路。
6. 如果需要管理员审批，账号进入“等待审批”，任务进入“需人工处理”。
7. 入群成功后生成 AI 测试发言。
8. 真实发送测试消息。
9. 如果配置了删除测试消息，发送成功后尝试删除。
10. 测试消息发送成功后，该账号标记为“已达标”。

审批通过或人工解除限制后，运营可触发继续检查。系统从当前账号状态继续执行，重新确认成员关系，再做测试发言。

## 状态机

任务级状态：

| 状态 | 含义 |
|---|---|
| 未开始 | 已创建，等待开始时间 |
| 执行中 | 正在按节奏推进账号 |
| 需人工处理 | 有账号卡在审批或群限制，其他账号继续推进 |
| 已达标 | 快照内全部账号都入群并发言成功 |
| 未达标 | 到结束时间仍有失败或未完成账号 |
| 已取消 | 人工取消任务 |

账号级状态：

| 状态 | 含义 |
|---|---|
| 待执行 | 已在快照内，等待调度 |
| 前置关注中 | 正在处理入群前关注频道 |
| 入群中 | 正在加入目标群 |
| 验证中 | 正在处理按钮或验证码 |
| 等待审批 | 已提交入群申请，等待管理员通过 |
| 测试发言中 | 已入群，正在生成并发送测试消息 |
| 已达标 | 测试消息发送成功 |
| 失败 | 账号、目标、验证、发言等出现明确失败 |

## 失败分类

失败分类需要稳定，方便详情页筛选、统计和后续运营处理。

| 分类 | 例子 | 是否人工处理 |
|---|---|---|
| 账号不可用 | session 失效、账号冻结、2FA 未补齐 | 是 |
| 目标不可访问 | 群不可解析、账号看不到群 | 是 |
| 等待管理员审批 | 已提交入群申请，需管理员通过 | 是 |
| 前置关注失败 | 关注要求频道失败 | 可自动重试 |
| 验证失败 | 按钮、验证码、图片验证码未通过 | 视情况 |
| 群权限限制 | 已入群但禁言或无发言权限 | 是 |
| 测试发言失败 | 入群后真实发言失败 | 可重试 |
| AI 发言生成失败 | 未生成合规测试话术 | 可重试 |
| 删除测试消息失败 | 删除失败但发言已成功 | 否 |

删除测试消息失败只记录，不影响账号达标。

## 详情页与运营操作

任务详情页按账号展示完整准入链路：

- 账号昵称、用户名、账号 ID。
- 当前阶段。
- 是否已入群。
- 是否已发言成功。
- 测试消息内容、发送时间、删除状态。
- Telegram 原始错误和系统失败分类。
- 处理建议。

运营操作入口：

| 操作 | 作用 |
|---|---|
| 重试该账号 | 重新推进单个账号 |
| 重试失败账号 | 批量重试失败项 |
| 重查入群状态 | 管理员审批后重新检测 |
| 继续测试发言 | 已入群但未完成发言时继续 |
| 标记人工已处理 | 审批或解除限制后让系统继续验证 |
| 导出失败清单 | 给人工审批、换群策略或账号处理使用 |

任务统计展示：

- 快照账号总数。
- 已达标数。
- 执行中数。
- 等待审批数。
- 失败数。
- 达标率。
- 预计剩余时间。
- 是否超出结束时间。
- 当前阻塞主因。

## 后台模型

复用现有 `Task` 和 `Action`，新增账号级快照记录表。

```text
task_membership_admission_items
- id
- task_id
- account_id
- target_id
- phase
- membership_action_id
- test_message_action_id
- delete_action_id
- test_message_text
- test_message_id
- delete_after_send
- delete_status
- failure_type
- failure_detail
- manual_required
- completed_at
- created_at
- updated_at
```

关键约束：

- 同一个任务内 `account_id` 唯一。
- 快照生成后不因分组变更而改变。
- `completed_at` 只在真实测试消息发送成功后写入。
- `manual_required=true` 不代表失败，代表需要外部处理后继续。

## 接口设计

任务创建接口：

| 方法 | 路径 | 作用 |
|---|---|---|
| POST | `/api/tasks/group-membership-admission` | 创建草稿任务 |
| POST | `/api/tasks/group-membership-admission/create-and-start` | 创建并启动任务 |

运营操作接口：

| 方法 | 路径 | 作用 |
|---|---|---|
| POST | `/api/tasks/{task_id}/membership-admission/items/{item_id}/retry` | 重试单个失败账号 |
| POST | `/api/tasks/{task_id}/membership-admission/retry-failed` | 批量重试失败账号 |
| POST | `/api/tasks/{task_id}/membership-admission/items/{item_id}/manual-handled` | 人工审批或解除限制后重新验证 |
| GET | `/api/tasks/{task_id}/membership-admission/failures.csv` | 导出失败或需人工处理账号清单 |

详情接口 `/api/tasks/{task_id}` 返回 `membership_admission_phase` 和 `membership_admission_items`，详情页直接用这两个字段展示快照进度、失败原因、测试消息和删除状态。

## 调度设计

调度分为三层：

1. 任务调度：到开始时间生成快照，按 pacing 推进账号。
2. 准入动作：复用 `ensure_target_membership` 处理入群、前置关注和验证码。
3. 测试发言动作：入群后生成 AI 短句并真实发送。

执行规则：

- 默认均匀铺开，避免同一时间集中加入触发风控。
- `max_concurrent` 控制同时执行账号数。
- `per_minute` 控制每分钟最多推进账号数。
- 可执行账号继续推进，等待审批账号不阻塞其他账号。
- 到结束时间后，仍未达标的账号保持具体阶段，任务标记为“未达标”或“需人工处理未完成”。

## 测试验收

### 后端单元测试

- 创建 `group_membership_admission` 任务时，必须保存目标群、账号分组、时间窗口、节奏和测试发言配置。
- 未到开始时间时，不生成账号快照。
- 到开始时间时，按账号分组生成一次性快照。
- 快照生成后，分组新增账号不进入当前任务。
- 账号完成 `ensure_target_membership` 后，必须创建测试发言动作。
- 测试发言成功后，账号才进入“已达标”。
- 仅入群成功但未发言成功，不得算达标。
- 管理员审批场景进入 `manual_required`，任务状态变为“需人工处理”。
- 删除测试消息失败不影响账号达标。
- AI 发言生成失败不得产生假成功。

### 前端与接口测试

- 创建任务表单展示“群聊准入任务”。
- 表单不展示“加入人数”。
- 账号分组、目标群聊、时间窗口为必填。
- 任务详情展示账号级阶段、测试消息、失败分类和处理建议。
- 支持重试单个账号、重试失败账号、重查入群状态、继续测试发言。
- 任务列表能展示已达标、未达标、需人工处理等状态。

### 线上验收

第一轮线上验收使用一个低风险测试群和一个小账号分组：

1. 创建群聊准入任务，选择目标群、账号分组和 15 分钟时间窗口。
2. 到开始时间后确认快照账号数等于分组当时账号数。
3. 确认系统按节奏推进，不一次性打满所有账号。
4. 对已入群账号确认存在真实测试发言。
5. 对等待审批账号确认任务进入“需人工处理”，且其他账号继续执行。
6. 人工审批后执行“重查入群状态”或“标记人工已处理”，确认系统继续测试发言。
7. 快照内所有账号发言成功后，任务状态变为“已达标”。
8. 导出失败清单，确认不暴露无关账号和敏感凭据。

## 落地顺序

### 第一阶段：数据模型和后端服务

- 新增任务类型枚举、schema 和配置校验。
- 新增账号快照表和迁移。
- 实现任务开始时锁定账号快照。
- 实现账号级状态更新和统计聚合。

### 第二阶段：准入编排

- 将快照 item 与现有 `ensure_target_membership` action 关联。
- 复用前置关注、验证码和 membership recovery 分类。
- 处理等待审批、群权限限制和账号不可用分类。
- 支持重试和重查入群状态。

### 第三阶段：测试发言

- 增加 AI 测试发言生成器，限制长度和低风险风格。
- 增加真实发送测试消息 action。
- 支持可选删除测试消息；删除通过 `delete_message` action 执行，失败只记录 `delete_failed`。
- 用测试发言成功驱动账号达标。

### 第四阶段：前端创建与详情页

- 任务中心新增“群聊准入任务”创建表单。
- 详情页新增账号级准入进度表。
- 增加运营操作入口和失败清单导出。
- 任务列表展示达标率和阻塞主因。

### 第五阶段：线上灰度和生产验证

- 先用测试群和小分组灰度。
- 再选择一个真实目标群做 10 到 20 个账号的小批量验证。
- 验证通过后再用于 AI 活跃群正式准备。
- 生产观察指标包括达标率、等待审批数、验证失败数、测试发言失败数、平均完成时长。

## 当前落地验收

本次代码落地后的本地验收命令：

```bash
PYTHONPATH=backend python - <<'PY'
from backend.tests import test_group_membership_admission as gt
for name in sorted(n for n in dir(gt) if n.startswith('test_')):
    getattr(gt, name)()
print('group admission direct tests ok')
PY

PYTHONPATH=backend python - <<'PY'
from backend.tests import test_frontend_permission_gating as ft
ft.test_frontend_exposes_group_membership_admission_task_type()
print('frontend static ok')
PY

PYTHONPATH=backend python -m py_compile \
  backend/app/services/task_center/membership_admission.py \
  backend/app/services/task_center/dispatcher.py \
  backend/app/services/task_center/payloads.py \
  backend/app/integrations/telegram/mock.py \
  backend/app/integrations/telegram/gateway.py \
  backend/app/api/routers/task_center.py \
  backend/app/models/task_center.py \
  backend/app/services/task_center/executors/group_membership_admission.py

npm --prefix frontend run build
git diff --check
```

`pytest backend/tests/test_group_membership_admission.py` 在当前仓库会先加载全局 `conftest.py`，它依赖外部 PostgreSQL 测试库；本地无该库时不作为准入阻塞。群聊准入相关用例使用上面的直接调用方式验证业务状态机。

## 验收口径

- 群聊准入任务不填加入人数，只按任务开始时的账号快照验收。
- 快照内所有账号都真实发送测试消息成功，任务才算“已达标”。
- 入群但未发言成功，不算达标。
- 等待管理员审批不算失败，但任务必须进入“需人工处理”。
- 到结束时间仍有未完成账号，任务不得显示为达标。
- 所有失败必须能追溯到账号、目标群、阶段、原始错误和系统分类。
- 测试消息删除失败不影响达标，但必须记录。

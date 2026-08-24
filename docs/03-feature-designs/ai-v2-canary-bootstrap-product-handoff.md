# AI V2 单任务受保护 bootstrap Product Handoff

## 1. 状态与授权

- 分级：L3 / production-sensitive configuration path。
- 当前授权：本地设计、实现、测试、独立 commit。
- 当前禁止：生产 apply、Task 开关、Provider/Prompt/面具/证明修改、补发、重生成、发布。
- 完成口径：本地 immutable commit；`production_fixed=unproven`。

## 2. 用户问题与第一个坏边界

生产 AI 活群已有 typed remote message，但运行任务未激活 route V2/two-stage，
policy/binding/reviewer evidence 缺失，legacy static 模板与结构塌缩可进入 ready Action。
模型或默认 Provider 切换不能替代 MessageBrief、voice、去重、独立 reviewer 和
quality_wait。现有 schema/runtime 已有分层组件，缺少的是不依赖 SQL 的受保护配置入口。

## 3. 开发交接

### 3.1 Owned paths

- `backend/app/services/task_center/ai_v2_canary_bootstrap*.py`
- `backend/scripts/manage_ai_v2_canary_bootstrap.py`
- `.github/workflows/production-ai-v2-canary-bootstrap.yml`
- 对应 tests、本文、质量 PRD 与项目索引

不得修改 authorization_dr/ABC、账号 Session、Telegram operation 或生产数据。

### 3.2 代码清单与显式选择

代码清单只包含已批准的版本/门禁常量。以下内容必须来自操作人选择并进入 fingerprint：

| 字段 | 原因 |
| --- | --- |
| exact tenant/task | 产品设计不指定生产 canary task |
| allowed routes | general/adult 的业务授权不同 |
| adult attestation IDs | 只能引用正式 scope evidence |
| purpose route items | Provider/model/timeout/rate/concurrency 是容量与成本决策 |
| max cost per slot / daily AI budget | 价格阈值不是设计常量 |
| sampling manifest hash | canary 样本集合必须预注册 |
| requester / approver / reference | 高风险 mutation 需异人审批与审计 |

preview 选择不完整时仍返回稳定 JSON 和 `missing_user_choices`；apply 对任何缺失字段失败。

### 3.3 原子 apply

apply 在一个数据库事务内：

1. 锁 Task、current policy、required purpose current route、候选 Provider、Task open work；
2. 重算 preview 并校验 expected fingerprint/deployed SHA；
3. 新建并激活不可变 policy version；
4. 为 required purposes 新建递增 active route revisions，旧 active 只 retired；
5. 仅把一个静默 `group_ai_chat` Task 更新到 next config revision 并创建 binding；
6. 写一个包含 old/new hashes、requester/approver/reference/fingerprint 的 AuditLog；
7. commit 后独立 readback。

任何失败整体回滚。重复相同 approval reference/fingerprint 只读回原结果；不同 fingerprint
复用 reference 必须冲突。

### 3.4 门禁

- Task tenant/type/revision/status 漂移：阻断。
- 其他 V2 task 已启用：阻断，保持 single-task canary。
- open GenerationJob、open Action、Gateway-started、unknown：阻断，不自动取消或重放。
- voice profile coverage 不完整：阻断，只报告账号 ID hash/count。
- adult attestation scope/version/expiry 不符：阻断。
- Provider credential/active/health/pricing 不完整：阻断。
- reviewer identity 与任一 router/realizer candidate 相交：阻断。
- purpose 缺项、priority 重复、timeout 或 policy 结构非法：阻断。

## 4. QA

- incomplete preview 明确列出所有 user choices；
- general-only 完整 preview/apply/readback；
- stale fingerprint / task revision / route revision 零写；
- reviewer identity 交叉、Provider unhealthy、voice 缺失、open Gateway/unknown 零写；
- audit failure 整体回滚；
- apply 只启用一个 Task，不改 tenant static fallback；
- policy/route/binding hashes 与独立 readback 一致；
- workflow 只暴露 preview/apply/readback，并校验 deployed SHA、fingerprint、审批字段。

## 5. 生产验收仍未包含

bootstrap 通过后仍必须完成 120+ 分层评测和单 Task 3 天 canary：至少 100 条 typed
remote facts、30 条盲审、3 个 context cluster、10 个账号，并逐条追踪 GenerationJob、
MessageBrief/voice/policy/routes/reviewer、Action、Attempt/Gateway、remote ID 与正文 hash。
这些证据不能由 persisted config、CI、deploy health 或发送数量替代。

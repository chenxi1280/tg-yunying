# 账号资料群风格多样化与精确登录批次初始化 PRD

- `intake_id`: `intake-2026-08-18-account-profile-group-style-300`
- `level`: `L2`
- `design_status`: `complete`
- `implementation_status`: `implemented_new_account_audit_fix_pending_release`
- `production_status`: `unproven`
- `owner_flow`: `product -> dev -> qa -> product -> release -> prod-diagnosis`
- `supersedes`: 仅补正 `account-profile-identity-uniqueness-prd.md` 的名字多样性和生产精确初始化入口；名称 claim、头像许可治理和账号用途边界继续沿用原合同。

## 1. Intake Card

- `raw_input`: “我们新登录300个账号，你来把账号初始化做了（名字+头像），名字取其他群里去抄一点，之前名字感觉一模一样的，我们需要升级优化。”
- `business_goal`: 为同一批新登录的 300 个普通运营账号完成名字与头像初始化；名字在唯一的基础上呈现更自然、更多样的群体风格。
- `affected_surface`: 批量登录、资料初始化、名称生成、账号安全批次、头像素材、生产账号身份 workflow、Telegram 远端回读。
- `authorization_scope`: 仅用户所指的精确登录批次成功账号；不扩展到全租户其他未就绪账号，不覆盖已完成且不属于该批次的账号。
- `out_of_scope`: 复制群成员完整姓名、username 或头像；抓取社交平台头像；修改接码/降权专用账号；修改 2FA、设备、代理、授权槽位、任务配置或消息内容。

## 2. 问题定义与根因

当前本地名字生成器已经有租户级唯一 claim，但 9 个模板和少量词表高度集中在“薄荷/晚风/云朵 + 日记/汽水/窗台 + 等风来/看晚霞”等同一文艺语域。唯一不等于多样：300 个账号虽然字符串不同，仍会呈现相同模板、相同词性和相近长度。

现有 `account_profile_initialization_reconcile.py` 面向全租户缺口扫描，不能把“新登录 300 个账号”冻结为精确目标；直接 apply 会扩大授权范围。现有 `Production Account Profile Identity Operations` 只有重复昵称治理和头像导入，也没有按登录批次初始化的 preview/apply/readback 操作。

## 3. 产品口径

### 3.1 “从其他群抄一点”的安全解释

系统可以从同租户已授权监听群的真人 sender name 中学习匿名化风格分布，但禁止复制完整身份：

- 只统计名字长度段、结构类别、是否含常见昵称前缀/后缀、是否含轻量标点或 emoji 等聚合特征。
- 不把完整 `sender_name`、`sender_username`、`sender_peer_id` 写入 manifest、日志、审计、新账号资料或新表。
- 生成候选必须来自平台扩展后的审核词片/模板；任何候选若等于来源样本的规范化完整名字，必须排除。
- 群成员头像不读取、不下载、不复制；头像继续来自许可明确、已审核且 TG cache ready 的非真人素材池。
- 样本不足或群风格不可用时必须显式输出 `style_sample_insufficient`；不得静默声称“已按群风格生成”。允许用户基于预览批准使用扩展本地多样性分布。

### 3.2 多样性目标

对 300 个目标账号：

- 规范化名字 300/300 唯一，且不命中当前账号名或历史 claim。
- 至少覆盖 8 个命名结构类别；任一类别占比不超过 25%。
- 2–3、4–6、7–12 字三个长度段均有样本，任一长度段占比不超过 60%。
- 同一首二字前缀占比不超过 5%，同一末二字后缀占比不超过 5%。
- 不使用递增序号、手机号片段、批次行号或 account ID 作为展示名区分手段。
- 不生成公司、客服、官方、营销、未成年人或具体真实人物身份口吻。

### 3.3 目标身份

目标必须由一组明确 `login_batch_ids` 冻结；当前批量登录单批最多 200 行，因此 300 个新登录账号允许来自多个终态批次：

1. 每个 `TgAccountLoginBatch.tenant_id` 等于 workflow 输入租户，batch ID 不重复。
2. 批次状态为 `completed`、`completed_with_unresolved` 或 `cancelled`；只纳入其中 `succeeded/succeeded_with_warning` 且存在同事务 `批量登录创建TG账号 + batch_item_id` 审计的本批新建账号，已有账号重登/已授权成功以及 failed/unresolved/skipped 行不进入目标。
3. 成功 item 状态只能是 `succeeded` 或 `succeeded_with_warning`。
4. 每个 item 必须有唯一 `account_id`，账号为 active、有 session、未删除、普通运营用途，pool 与 `account_identity=normal` 一致。
5. 所选全部批次的“本批新建且登录成功”账号并集不得跨批重复，`expected_target_count` 必须等于并集账号数；本次固定为 300。批次 `success_count` 可因已有账号重登成功而大于本次目标数。
6. manifest 冻结每个 batch 的 `state_version/execution_generation/resolution_version/finished_at` 和每个 item/account 旧状态。

preview 未提供 batch IDs 时，可在最近 7 天最多 20 个终态批次中发现“本批新建且登录成功”账号并集恰好 300 的组合；零个或多个组合时必须输出脱敏候选 batch ID、状态、全部成功数和新建成功数并失败，不得自行从成功账号中删减或扩大到全租户。

## 4. 功能设计

### 4.1 名称结构类别

扩展本地生成器至少覆盖：

1. 短昵称：`阿 + 单字/双字`、`小 + 单字/双字`、独立双字昵称。
2. 食物与口味：食物、饮品、口味偏好和轻量生活表达。
3. 动物轻拟人：非受保护品牌/角色的通用动物词片与日常状态。
4. 场景与地点：不指向具体住址的通用城市/天气/通勤/店铺场景。
5. 兴趣与物件：摄影、运动、阅读、音乐、游戏等通用兴趣表达。
6. 心情与动作：自然口语状态，不复用单一“等风来”模板。
7. 轻梗与反差：审核通过的无攻击性口语组合。
8. 姓名感昵称：使用常见姓氏与虚构给名组合，但必须排除来源完整姓名和租户历史 claim；不声明真实身份。
9. 标点/emoji 变体：仅少量使用，最多一个轻量符号，不形成批量相同尾巴。
10. 不规则生活短句：长度 7–12 字，避免同一主谓模板。

生成器接收不可变随机 seed 和可选 `NameStyleWeights`；无 style weights 时使用审定默认分布。模板选择不能继续按 `slot % template_count` 轮转，应按 seed 驱动的加权抽样并在 manifest 中输出实际分布。

### 4.2 群风格摘要

生产 preview 从输入 `style_group_ids` 读取最近 30 天、每群最多 2,000 条 `GroupContextMessage`：

- 限同租户、指定群、`is_bot=false`、非空且非默认 `真人用户` 名称。
- 先按规范化完整名字去重，再按 sender identity 去重，避免高频发言者放大权重。
- 只在内存中保留原始名字完成分类；输出仅包含群 ID、合格样本数、类别计数、长度段计数、拒绝原因计数和 `source_fingerprint`。
- `source_fingerprint` 由已排序的匿名分类记录计算，不允许反推出原始姓名。
- 少于 100 个合格去重样本时标记不足并阻断群风格 apply。

### 4.3 头像分配

- 只选择 `review_status=已审核`、图片 MIME 合法、`cache_ready_status=ready` 且 TG cache 三元组完整的 upload 素材。
- 只使用头像候选标签或现有 avatar 优先集合，不读取群成员头像。
- preview 冻结每个账号的 `material:<id>`；按 `usage_count` 最低层优先并在同层 seed 随机，逐次增加 manifest 内模拟使用计数，避免一次预览集中到少数图片。
- 本次允许多账号共享同一素材，但输出 `unique_avatar_material_count/max_material_assignment_count`；若 ready 素材少于 12 个或单素材分配超过目标数的 10%，阻断 apply。
- 本次是对精确新登录批次集合的显式资料升级，允许覆盖这些 target 的旧自动初始化名字/头像；manifest 冻结旧值，preview 后人工变更会触发 CAS 漂移并零写入。批次外账号绝不覆盖。

### 4.4 用户名边界

本次原始需求只要求名字和头像。受保护操作动作固定为 `update_profile + update_avatar`，不修改 username；自动登录链路既有 username 初始化保持原合同，不在本次扩权。

## 5. 受保护生产操作

在 `Production Account Profile Identity Operations` 新增：

- `operation=login_batch_initialize`
- `mode=preview|apply|readback`
- `login_batch_ids`（逗号分隔；preview 可留空做唯一组合发现）
- `expected_target_count`（本次 300）
- `style_group_ids`（逗号分隔的精确群 ID）
- `seed`
- `deployed_sha`
- `expected_manifest_sha256`
- `approval_ref`

workflow 校验完整 40 位 release SHA、当前生产 symlink、输入格式和 account-security/material-cache worker 状态；脚本在 backend 容器中执行，不暴露手机号、session、AuthKey、群原始姓名或头像。

### 5.1 Preview

canonical manifest 包含：

- `tenant_id/login_batch_ids/expected_target_count/deployed_sha/seed`
- 每个登录批次的版本、状态、成功/失败/未解计数和完成时间；目标 item 另由账号创建审计绑定
- 匿名群风格摘要与 source fingerprint
- ready 头像池摘要
- 每个 target 的 account/item ID、旧展示名/TG 名/头像有无/资料状态、账号用途、账号状态、生成的新名、冻结 avatar source、no-op 原因
- 名称类别/长度/前后缀分布、头像分配分布
- target snapshot hash、canonical manifest SHA-256

preview 只读，禁止写 claim、batch、item、audit、头像 usage_count 或远端 Telegram。

### 5.2 Apply

apply 必须提供同一 seed、login batch IDs、style groups、deployed SHA、manifest SHA 和 approval ref：

1. 重新构建 manifest，SHA 不一致即失败。
2. 锁定登录 batch/items 和精确 accounts，验证所有版本与旧值。
3. 检查同 manifest 已创建批次；一致则复用，漂移或重复账号即失败。
4. 对缺失 target 按最多 50 个一批调用公开 `create_account_security_batch`。
5. 使用 `preview_overrides` 冻结名字与 avatar source，动作仅 `update_profile/update_avatar`，`overwrite_existing=true` 只适用于 manifest 明确判定的占位/未就绪目标。
6. claim、账号安全 batch/items 与 AuditLog 在正式服务事务中创建；不得直接 SQL 改资料。
7. active/open 旧资料初始化项若属于同一 target，必须先分类：已成功为 no-op；同目标同值开放项复用；值不同或远端状态未知则 `existing_profile_operation_conflict`，禁止双批并发。

### 5.3 Readback

readback 使用 manifest SHA 找到精确批次与 items：

- 持久化：account display/TG name/avatar object key、name claim、batch/item 状态、audit cardinality 与 target count 匹配。
- Telegram 远端：逐账号 `pull_profile` 验证 first name 等于 manifest 新名、last name 为空，并验证头像远端状态由 item `avatar_status=succeeded` 与独立 profile/avatar readback 共同证明。
- 邻居不变：同登录 batch 的 no-op 行和非目标账号旧 snapshot hash 不变。
- 输出失败类型、等待缓存、FloodWait、pull_failed、mismatched 和 unknown；任何非 matched 均不能报告完成。

## 6. 并发、幂等与失败路径

- workflow concurrency group 固定串行账号身份操作。
- canonical manifest + login batch versions + account old-state hash 是 drift guard。
- 同 manifest 重复 apply 只补未创建 chunk；已成功项不重发。
- Telegram call started 后结果未知时保留 claim/material assignment，进入 readback/reconcile，禁止自动创建新 mutation。
- 素材 cache 不 ready、来源丢失或 TG cache 引用不完整时 item 保持 waiting/failed；名字成功不能伪装头像成功。
- 名称 claim 冲突、目标数不是 300、目标用途不合法、style sample 不足、头像池不足、现有冲突批次、部署 SHA 漂移均为零写入失败。
- 部分批次成功时禁止整体回滚远端资料；后续只按原 manifest 对未完成项 reconcile/readback。

## 7. 前端与权限

- 本期不新增前端页面；账号安全批次详情继续展示逐账号状态。
- 操作只允许 GitHub `production-silicon-valley` 受保护环境和现有生产运维权限。
- 普通账号页面不能获得“按群抄名字”的批量入口。
- 审计保存 actor、approval ref、manifest SHA、batch IDs、target IDs 和聚合 style fingerprint；不保存原始群姓名、用户名、sender peer、手机号或凭据。

## 8. 数据流转

```text
selected TgAccountLoginBatches + succeeded BatchItems
  -> exact account set + old-state/version snapshot
  -> selected GroupContextMessage sender names (memory-only)
  -> anonymous NameStyleSummary + source_fingerprint
  -> expanded curated generator + unavailable/current/historical claims
  -> ready licensed avatar pool + simulated least-used allocation
  -> canonical manifest + SHA-256
  -> protected apply/CAS
  -> TgAccountProfileNameClaim + TgAccountSecurityBatch/Items + AuditLog
  -> account-security worker -> Telegram profile/avatar mutation
  -> independent DB + Telegram readback
```

## 9. QA 验收

### 9.1 E2 自动化

1. 300/500/1000 个名字唯一、可复现、无 ID/序号尾巴，并满足类别/长度/前后缀集中度。
2. 来源完整名字即使恰好由模板生成也会排除；manifest 不包含原始 sender name/username/peer ID。
3. 群越租户、群不存在、样本不足、机器人/默认名只样本明确失败。
4. login batch 非终态、新建成功账号并集非 expected、跨批重复账号、创建审计缺失、item 无 account、用途不一致均零写入；已有账号重登/已授权成功及 failed/unresolved/skipped 行不进入目标。
5. preview 零写入；apply 缺 SHA/ref 或任何版本/旧值漂移零写入。
6. 同 manifest 重复 apply 幂等；冲突 manifest/open item 明确失败。
7. 头像分配只用 ready 素材，模拟 least-used 分布满足阈值；不读取群成员头像。
8. 50 个 chunk 边界下 300 个目标恰好创建 6 个或复用等价批次，不丢不重。
9. 名字成功、头像 waiting/failed 必须保持 partial/incomplete；不能报告整项成功。
10. readback 必须检查远端 first/last name 和头像状态，pull failure/unknown 不计 matched。

### 9.2 E4 生产验收

- preview 精确 target count=300，用户批准 manifest hash。
- apply 后 300 个 target 均有唯一 claim、唯一 item owner 和审计引用。
- Telegram 远端名字 matched=300/300，头像 matched=300/300；数据库与远端一致。
- target 内重复名=0、历史 claim 冲突=0，非目标账号变更=0。
- 名称类别、长度、前后缀和头像素材分布通过质量阈值。
- backend/account-security/material-cache healthy 只作运行证据，不代替上述远端事实。

## 10. 发布与回滚

- 默认 `master -> release -> Deploy Production`，L2 Release Gate 必须包含 no_postgres、PostgreSQL、workflow contract 与镜像构建。
- 先发布生成器和受保护 operation，再执行 preview；发布不自动初始化任何账号。
- apply 前确认没有其他账号身份 workflow 在运行，且 account-security/material-cache worker 正常。
- 代码回滚只停止新 apply；不删除 claim、batch、item、audit 或已成功远端资料。
- 名字质量不合格时创建新的精确纠正 manifest；不恢复旧占位名，不对 300 个账号盲目整体重发。

## 11. Product Design Complete 自检

- [x] 覆盖用户原话：300、新登录、名字、头像、参考其他群、解决同质化。
- [x] 定义前端、后端、workflow、worker、数据流、权限与隐私边界。
- [x] 定义批次身份、数量、版本、幂等、并发、漂移和 unknown。
- [x] 定义群样本匿名化和不复制完整身份/头像。
- [x] 定义名称与头像质量阈值。
- [x] 定义 preview/apply/readback、审计、发布、回滚和 E2/E4。
- [x] 无 silent fallback、mock success 或健康即完成声明。

`design_status=complete`。生产 apply 仍必须取得精确 `login_batch_ids`、`style_group_ids`、preview manifest SHA 和 approval reference。

## 12. Product Handoff

- `message_id`: `product-account-profile-group-style-300-20260818`
- `dev_scope`: 扩展名称生成器；新增匿名群风格摘要；新增精确 login batch 集合初始化脚本与 protected workflow operation；补定向测试和索引。
- `locked_paths`: `account_profile_identity.py`、新增 group-style/profile-init 模块、生产 identity workflow/script、对应 tests、本 PRD、项目结构/数据流索引。
- `qa_gate`: 先写失败测试覆盖 300 分布、隐私、target/CAS/preview/idempotency/readback，再实现；后端测试单次 60 秒硬超时。
- `release_gate`: 不在开发分支运行生产 apply；合并 release 并部署成功后，才用当前部署 SHA 运行 protected preview。

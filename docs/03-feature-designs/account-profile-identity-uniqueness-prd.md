# 账号昵称唯一性、存量去重与头像素材治理 PRD

> **2026-08-18 多样性补正：** 租户级 claim、历史名字保留与许可头像治理继续以本文为准；精确 completed 登录 batch、匿名群风格分布、10 类名字生成、ready 头像冻结分配和名字+头像独立 readback 以 `account-profile-group-style-initialization-prd.md` 为准。不得用本文旧的宽泛全租户 reconcile apply 处理“新登录 300 个账号”。

- `intake_id`: `intake-2026-08-09-account-profile-identity-uniqueness`
- `level`: `L3`
- `design_status`: `complete`
- `dev_handoff_ready`: `true`
- `resync`: `true`
- `production_related`: `true`
- `evidence_level`: `E1`

## 1. Intake Card

- `source`: user
- `suspected_type`: feature + production data repair
- `affected_surface`: 账号新增、登录后资料初始化、手工资料修改、资料同步、账号安全批次、素材中心、生产发布工作流
- `raw_input`: 梳理现有重复名字并重新生成；以后注册不得重复；名字范围更随机；补充照片并上传作为头像。
- `next_route`: product -> dev -> qa -> product -> release gate -> prod-diagnosis

### 已知生产证据

2026-08-09 对 tenant 1 的 active 普通运营账号做只读聚合：

| 指标 | 当前值 |
| --- | ---: |
| active 普通运营账号 | 892 |
| 重复昵称组 | 49 |
| 位于重复组中的账号 | 533 |
| 每组保留一个后的改名目标 | 484 |
| 已审核、上传且 TG cache ready 的图片 | 295 |
| 上述图片的不同素材指纹 | 295 |

现有本地生成器按 `account_id/index` 对有限昵称池做确定性取模，仅用单次预览内的 `used_names` 去重。生产中同一昵称已被 40 余个账号复用，且多数已同步到 Telegram。历史 `rename_half_account_profiles` 工作流按 ID 顺序改一半账号，不识别重复组，禁止用于本需求。

## 2. 原始需求覆盖矩阵

| user_requirement | product_decision | functional_design | backend/data design | QA acceptance |
| --- | --- | --- | --- | --- |
| 梳理重复名字 | 以租户内 active 普通运营账号的规范化昵称分组 | 提供只读 preview 和精确目标清单 | NFKC + 去零宽 + 合并空白 + trim + casefold | 分组、账号 ID、keeper、target 分母可复算 |
| 重新生成新名字 | 每组保留一个，其余账号生成全租户未使用名 | 生成 manifest，确认后创建账号安全批次 | 名称注册表先占位，worker 再改 Telegram | 484 基线只作当前证据，apply 前按新快照冻结分母 |
| 以后注册不重复 | 平台所有稳定资料写入口统一占名 | 新增、自动初始化、批量初始化、手工修改都走同一服务 | 数据库唯一键处理并发，不依赖随机概率 | 并发创建/批次/手工修改只能一个成功 |
| 名字更随机 | 扩大组合维度并使用批次随机种子 | AI 候选和本地生成都过滤全租户已用名 | 多类词片 + 多种模板 + 有界冲突重抽 | 跨批次无重复，长度/类型分布不过度集中 |
| 补照片并上传 | 只引入许可清晰的非真人图片 | manifest 导入、审核、缓存后进入头像池 | 保存来源、许可、SHA-256、感知哈希 | 无未授权来源、精确重复和近似重复素材 |

## 3. 业务边界与定义

### 3.1 唯一性范围

- 唯一性作用域是 `tenant_id`。
- 约束对象是未删除普通运营账号的稳定 `display_name`；`tg_first_name` 在平台资料更新成功时与该值保持一致，`tg_last_name` 默认空。
- 接码专用、降权观察和用途错配账号继续遵守现有禁止资料修改边界，不进入自动去重目标。
- “托管账号”“新托管账号”“未命名账号”和导入占位名属于注册过渡态；它们必须自身带随机唯一后缀，但不算资料初始化完成。
- 外部在 Telegram 客户端手工改名造成的漂移不属于平台可阻止范围；下次资料同步必须标记 `name_conflict` 并进入治理清单，不能把冲突静默覆盖成成功。

### 3.2 规范化名称

`name_key` 由以下固定顺序生成：Unicode NFKC -> 移除零宽字符 -> 连续空白折叠为一个空格 -> 去首尾空白 -> Unicode casefold。

- 阻断条件是 `name_key` 精确相同。
- 相似名只作为质量 warning，不作为硬阻断，避免误伤正常昵称。
- 空值、纯空白、只含零宽字符直接校验失败。

### 3.3 keeper 与目标选择

每个重复组只保留一个 keeper，优先级固定：

1. `profile_sync_status=已同步`；
2. 有头像；
3. `created_at` 最早；
4. `account_id` 最小。

其他账号成为 rename target。preview 必须输出 keeper、target、旧名、新名、旧资料状态、头像是否存在和快照哈希。非重复账号不得进入 apply。

## 4. 名称生成与唯一占用

### 4.1 名称注册表

新增 `tg_account_profile_name_claims`：

| 字段 | 说明 |
| --- | --- |
| `tenant_id` | 唯一性租户范围 |
| `account_id` | 获得该名字的账号 |
| `display_name` | 生成时的可见名 |
| `name_key` | 规范化唯一键 |
| `source` | registration / ai_random / local_random / manual / dedupe |
| `batch_id` / `batch_item_id` | 可选账号安全批次来源 |
| `trace_id` | 预览/生成追踪 ID |
| `created_by` / `created_at` | 审计 |

数据库唯一约束为 `(tenant_id, name_key)`。注册表采用只增不改策略：账号后续改名时旧 claim 仍保留，避免历史名字被快速复用形成新的批量规律。`(tenant_id, account_id, name_key)` 作为幂等键。

迁移时只为每个当前规范化名字的 keeper 回填 claim；重复组其他账号在获得新名字时写入新 claim。生成器同时读取当前 `tg_accounts` 和 claim 表，因而迁移未完成期间也不会生成任何当前已占用名字。

### 4.2 并发和事务

- precheck 只读，不占名。
- 创建账号或确认资料批次时，在同一数据库事务内写入 claim；唯一冲突返回明确 `display_name_conflict`，整个批次不做部分提交。
- worker 调用 Telegram 前再次确认 claim 仍归属于当前账号和批次；不匹配时失败，不发送远端修改。
- Telegram 修改成功但本地落库未知时保留 claim，进入现有 unknown/人工对账边界；不得把名字释放给其他账号。
- 手工资料修改、自动资料初始化和存量去重必须复用同一个 claim 服务。

### 4.3 更随机的本地生成器

- 生成器从时间/天气、食物、植物、物品、场景、动作、心情、轻拟人化词片中组合，使用至少 8 种模板和 2～4 个片段。
- 每批创建不可预测的 `uniqueness_seed` 并持久化；测试通过注入确定性随机源复现。
- 不再使用 `account_id % pool_size` 作为名字决定因素，不使用递增序号作为默认结果。
- 候选必须依次通过：中文展示规则、禁用词、长度、当前账号表、历史 claim、本批已用名、模板集中度检查。
- 冲突有界重抽；达到 `NAME_GENERATION_MAX_ATTEMPTS` 后明确失败 `name_pool_exhausted`，不回退到“用户 + account_id”。
- AI 生成也读取全租户 unavailable name keys；输出不足时允许现有显式 warning 的本地生成路径，但本地路径仍必须通过同一 claim 规则。

## 5. 存量去重流程

### 5.1 preview

新增 `account_profile_duplicate_reconcile.py`，默认且无参数时只能 preview：

1. 读取精确租户、active 普通运营账号和当前 deployed SHA；
2. 计算重复组和 keeper；
3. 为 target 生成未占用新名字，不写数据库；
4. 输出不含手机号/Session 的 manifest；
5. manifest 包含目标 ID、旧值、新值、资料状态、账号用途、生成 seed、deployed SHA 和 canonical SHA-256；
6. unexpected zero、空新名、重复新名、目标越界或非重复账号进入目标时直接失败。

### 5.2 apply

- apply 必须显式提供 preview manifest 与其 SHA-256、actor、approval/incident reference。
- apply 前重新读取每个目标；账号 ID、租户、旧名、状态、版本、deployed SHA 任一漂移即整体失败。
- 每个 target 先写 name claim，再通过公开的账号安全 batch service 创建 `update_profile` 项；禁止直接 SQL 改名和直接调用私有 `_execute_batch_item`。
- 单批最多 50 个账号，批次间由正式 account-security worker 执行；Telegram flood wait、账号离线和 unknown 分别留痕，不扩大重试范围。
- 已成功 target 不重复改名；重跑从 manifest 和批次审计识别 no-op、未执行和失败项。

### 5.3 readback 与结果分层

- 持久化验证：每个 target 的 `display_name/name_key/claim/batch item/profile_sync_status` 一致，邻近非目标账号未改变。
- Telegram 验证：逐账号远端 profile 读取的 `first_name/last_name` 与目标一致。
- `persisted_verified` 不能代替 Telegram 业务结果；只有全部目标具备远端回读才能报告本次改名完成。

### 5.4 生产队列隔离

- `account-security` worker 只执行账号安全和资料批次；素材 TG 暂存由常驻 `material-cache` worker 独立执行。
- 素材上传超时、FloodWait 或缓存账号异常不得阻塞纯昵称更新、2FA、设备清理等不依赖头像缓存的批次。
- 资料批次若需要头像，仍必须逐项检查 `cache_ready_status=ready`；未 ready 时保持 `waiting_cache`，不得绕过素材缓存或静默当作完整成功。
- 发布检查必须同时验证两个容器和两个独立 heartbeat；容器 healthy 只证明进程存活，生产验收仍以批次项和素材状态持续推进为准。

## 6. 头像素材补充与分配

### 6.1 来源规则

- 默认只采集自然、食物、物品、纹理、抽象插画等非真人图片；首批固定 17 个 Wikimedia Commons 页面 ID，不运行无边界爬虫。
- 允许许可：CC0、Public Domain Mark、CC BY 2.0/2.5/3.0/4.0、CC BY-SA 3.0/4.0；BY/SA 的署名和传播要求必须随素材保存。
- 禁止抓取社交平台头像、搜索结果缩略图、版权不明图片、真人面孔、未成年人、品牌 Logo、证件或水印图。
- 每个素材必须保存 `source_url`、`license_code`、`license_url`、`attribution_text`、抓取时间和导入 actor。

### 6.2 导入与去重

- 使用 manifest 驱动的素材导入器，不做无边界网络爬虫。
- 下载后验证 MIME、可解码性、尺寸、文件大小；保存 SHA-256 和感知哈希。
- SHA-256 相同直接拒绝；感知哈希距离低于阈值进入人工复核，不能静默当新图。
- manifest hash 的人工批准视为这批固定候选的素材审核；apply 后仍需走 TG cache worker，只有 `已审核 + cache_ready` 才能用于头像。
- material-cache 每次远端上传使用一次性 Telethon client，完成或异常后都断开；禁止复用已经执行多次媒体上传的进程级 client 让单个陈旧连接拖住整批素材。
- Commons 导入素材在 `cache_ready` 前不得依赖短 TTL 清理后不可恢复的临时文件。若 apply 发现同一 `source_page_id` 已存在但本地内容文件缺失，必须重新下载 manifest 中的受控 URL，复核许可、来源字段、SHA-256、感知哈希、MIME 和尺寸均与现有来源记录及本次 manifest 完全一致，再幂等恢复原 Material 的上传文件并写审计；任一字段漂移时显式失败，不新建重复素材、不覆盖已 ready 的 TG 缓存引用。
- 恢复仍遵守 `preview -> manifest SHA -> apply -> readback`：preview 不写入；apply 只恢复缺失文件或幂等跳过已有/已 ready 素材；readback 必须报告本地内容是否存在、缓存错误和 TG cache 三元组。队头素材缺文件时必须先修复该精确素材，禁止将其静默标成 ready 或跳过到后续素材。

### 6.3 分配策略

- 存量去重默认不覆盖已有头像，只给缺头像账号补头像。
- 新注册账号从最低使用次数的一组素材中再随机选择，减少热点重复；素材不足时明确等待/跳过头像，不影响唯一昵称落库。
- 头像不承诺严格一账号一图；使用次数和近似重复度必须可见。昵称唯一性是硬约束，头像多样性是质量约束。

## 7. 前端边界

- 本期不新增普通用户页面；现有账号安全预览继续展示生成昵称，冲突在确认时由数据库 claim 明确返回。
- 存量去重和许可素材导入只走受保护的 GitHub Actions preview/apply/readback，避免在普通页面提供全量生产写入口。
- 账号安全批次详情继续承载逐账号执行状态；manifest、许可和来源元数据在 Actions 输出与数据库审计中核验。

## 8. 权限、安全与审计

- 预览需要账号资料查看权限；单账号修改沿用账号资料编辑权限；全量去重 apply 仅受保护 GitHub Actions/生产运维权限可执行。
- 审计记录 target IDs、manifest hash、actor、approval ref、batch IDs、旧/新 name key 和结果；不记录手机号、Session 或凭据。
- 头像导入记录来源和许可；下载只允许 HTTPS、限制重定向和响应大小，阻止内网/本地地址，防止 SSRF。

## 9. QA 与验收

### 9.1 E2 自动化

1. NFKC、零宽、空白、大小写等价名称归一为同一 key。
2. 两个并发事务争用同一名字时只有一个 claim 成功，另一个明确冲突。
3. 两次独立批次、登录自动初始化、手工修改均不能生成/保存当前或历史已用名。
4. 60、500、1000 个账号生成结果全唯一，模板/长度分布不过度集中，固定测试随机源可复现。
5. 重复组只选择 keeper 之外的账号；非重复账号和 code receiver 不进入目标。
6. preview 零写入；apply 的 old-value/SHA/CAS 任一漂移时零写入。
7. worker 缺 claim 时不调用 Telegram；失败/unknown 不释放 claim。
8. 头像导入拒绝未知许可、重复 SHA、不可解码文件、内网 URL 和超限响应；近似重复进入复核。
9. 存量脚本默认 preview，未提供 manifest hash/actor/ref 时不能 apply。
10. `material-cache` 调用阻塞时，`account-security` 仍能独立推进不依赖头像缓存的昵称批次；生产 compose 和发布检查必须包含两个 worker。
11. 连续缓存多张素材时每张都新建并断开 Telethon client；前一张连接异常不得污染下一张素材上传。
12. 已导入但临时文件被清理的素材在新 manifest apply 中只恢复同一 Material；来源或哈希漂移时零写入，已有文件与已 ready 素材保持幂等不变，恢复后 material-cache 能继续推进后续素材。

### 9.2 E4 生产验收

- 发布前后重复昵称基线均可复算；发布本身不代表数据修复。
- apply 前冻结最新 manifest 分母；每个 batch 都有 Action/执行记录、claim、远端 profile 回读。
- 最终 active 普通运营账号 `duplicate_group_count=0`，`claim_conflict_count=0`，非目标账号变更数为 0。
- 新注册受控样本完成登录和自动资料初始化后，名字不与任何现有或历史 claim 重复，头像来自已审核且 cache ready 的许可素材。

## 10. 发布、迁移与回滚

- 默认路径：`master -> release -> GitHub Actions Deploy Production`。
- 先上线 claim 表、生成器和 preview；验证 backfill/preview 后再启用 apply。
- 迁移只回填 keeper claim，不在 schema migration 中调用 Telegram 或批量改账号。
- 回滚代码时停止创建新批次，保留 claim 和审计表；已经成功的唯一名字不自动恢复成旧重复名。
- 若新名字质量有问题，使用新的精确纠正 manifest 单账号/小批修正；禁止恢复 484 个旧重复名作为整体回滚。
- 头像导入和账号改名是两个独立操作，任一失败不得伪装另一个成功。

## 11. Product Design Complete 自检

| 检查项 | 结论 |
| --- | --- |
| 原始需求 | 四项全部覆盖 |
| 功能/前端/API/worker | 已定义入口、状态、执行与展示 |
| 数据模型与流转 | claim、preview、batch、worker、readback 完整 |
| 权限与隐私 | 全量 apply 受保护，输出不含凭据 |
| 失败/并发/幂等 | 数据库唯一键、CAS、unknown 和重跑边界明确 |
| 发布/迁移/回滚 | 分阶段上线，不在迁移中远端改名，不恢复重复名 |
| QA | E2 与真实 Telegram E4 分层 |
| 未覆盖用户原话 | 无 |

结论：`design_status=complete`、`dev_handoff_ready=true`、`resync=true`。这只表示产品设计完成，不表示代码、QA、发布或生产改名已完成。

## 12. Product Handoff

- `message_id`: `product-account-profile-identity-uniqueness-20260809`
- `from_agent`: product
- `to_agent`: dev
- `message_type`: implement
- `level`: L3
- `evidence_level`: E1
- `handoff_delivery_status`: acknowledged-in-current-task
- `locked_paths`: 账号资料唯一性新模块/模型/迁移、资料初始化生成与批次创建、手工资料更新、生产 reconcile 工作流、定向测试、PRD/索引

开发必须先写归一化、跨批次重复、并发 claim、preview 零写入和精确 duplicate target 红测；使用独立模块承载名称规则，不能继续把逻辑堆入已超长的 `account_security/service.py`。完成后进入独立 QA；QA pass 后仍需产品验收和 Release Gate，生产 E4 必须以 0 重复组及逐账号远端回读为准。

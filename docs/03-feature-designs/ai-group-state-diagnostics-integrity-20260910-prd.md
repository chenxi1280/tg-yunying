# AI 活群状态与诊断一致性修复

## Intake / Bug Batch Plan

- intake_id：AI-GROUP-STATE-DIAGNOSTICS-20260910；level：L3。
- 用户要求：修复已定位的救援终态回退、发后验证信息缺失、权限异常信息丢失、排期显示失真、生成失败混算。
- 范围：通用状态保护与只读诊断。目标群真实成员权限、消息可见性、合法排期空隙仍需远端证据；不以修改状态代替业务恢复。
- 阶段：prod-diagnosis → product → dev → qa → product → prod-diagnosis；同一执行者按阶段串行负责；独立工作树 `codex/ai-group-full-recovery-20260910`。
- 根因分组：R1 救援状态读写；R2 采集/权限观测；R3 原日队列与生成统计。无 schema migration，无新增恢复副作用。

## 1. 救援状态合同

Action 真实终态优先于 result 中的历史 rescue_status。`unknown_after_send`、`closed_unknown` 保持原意；`skipped + obligation_state=closed_with_unknown_shortfall` 展示 closed_unknown；普通 skipped 展示 skipped；success/failed 分别映射 invite_success/invite_failed。结果回写只更新触发 Action 的关联和展示，不修改被引用救援 Action。

刷新入口先锁原 Action，检查当前义务和全部 Attempt：已确认或未知终态、非 open 义务、任何未证明 remote_mutation=false 的 Gateway 调用均保持原身份和证据。只有原合同允许的未调用或确定未修改远端的失败工作，才可执行既有刷新。不得借配置变化重新排队历史未知。查询必须限定 tenant/Task/义务；无新 Action、无新调用、无删除事实。旧记录通过当前真实状态投影纠正展示，不批量重写历史。

## 2. 发后控制提示观测

保持原账号、原目标、原发送 ID 之后、最多 100 条和原按钮筛选规则。读取接口以显式 `include_diagnostics` 返回结构化观测；普通读取接口保持原列表返回。观测记录原读取数、无按钮排除数、候选数、游标、首末消息时间和读取上限是否触及。达到上限只表示窗口可能不完整，不声称读完。

候选保留 sender_role 与独立 sender_role_error（异常类名，不保存 RPC 参数/正文/凭据）。非 bot、角色未知、角色查询失败、确定非管理员分别记原因。协议、收件人归属和可信来源判定保持原合同。失败分类计数和采集元数据写入原 Action 诊断；阻塞准入证据保留相同诊断。读取异常继续现有 retry，异常类型可见；不能伪造零条成功读取。纯文本被筛除与空读取可由计数区分。

## 3. 发送错误与远端结果分开

SendResult 增加独立 diagnostics：RPC 异常类、request 类、失败阶段（解析目标/发送调用/回执后）、send_call_started、remote_mutation_state。只提取类名，不记录异常 request 的参数。传递到 Action.result 与 Attempt.result_snapshot 的 `send_diagnostics`。Gateway 返回后，在权限恢复等所有分支返回前保存原始调用诊断；常规最终结算与提前恢复必须保留同一份证据，不能因分支选择丢失。权限错误可同时拥有 unknown 远端结果；错误分类、unknown 判断及既有救援触发规则保持原合同。

## 4. 原期限队列

只读诊断对原 ledger 的 Action，结合原截止、release/effective/scheduled 放行时间、当前时间和已调用证据，分类：valid_wait、outside_original_deadline、expired_uncalled、called_history、unknown_preserved、terminal。called_history 只证明有过调用，不猜测该调用是否未知；closed_unknown 仍显示未知已收口。等于半开区间截止即 outside_original_deadline；未知及已调用不能投影成可抛弃的未调用。保留原义务/期限/预约，不提前或重建工作，不扩大窗口。现有 deadline 收口仍由正式 owner 服务执行。没有合法空隙反例前不改排期算法。

## 5. 生成阶段与最终结果

现有 generation_stage_counts 明确为历史 Job 阶段，不作为任务失败数。新增按当前 Action/原义务绑定的结果分类，普通生成失败后转应急可归入 emergency_pending、ready、发送待确认或 typed remote confirmed；准入终态 c2_account_abandoned 归入 admission_blocked；provider_result_unknown 与 Telegram unknown 分开。

同一原义务优先采用 FOP 的 active Action；没有匹配 active Action 时采用最高 materialization_version/最新创建的物化，未绑定义务的 Action 独立计数。多代 Job 按原 tenant/Task/epoch/义务的最新序号/时间选择，阶段分布与最终结果分布分别展示，不相加。报告支持显式 since：Action 创建/执行时间或最新 Job 创建时间任一达到锚点才纳入；远端完成还要求调用和事实均在锚点之后。默认结果为当前生命周期历史工作，旧 generation_stage_counts 明确为全历史 Job。远端完成必须匹配 tenant/Task/ledger/Action/Attempt/消息 ID 的 typed fact；Action success 不等价于可见完成。计数查询只取状态及正文存在布尔值，不加载正文和候选证据大字段。

## Product Design Complete / QA

- 原始五项需求均映射到以上合同；目标权限恢复、全量 E4 和排期是否浪费保留 unproven，不能写成实现已完成。
- API 使用原受权限保护的诊断响应增加字段；既有调用默认列表兼容。前端任务详情增加当前义务结果、原日排期及调用/读取证据；发送回执与可见消息分开命名；救援未知关闭不显示成未触发。
- QA：真实函数与中性数据库对象复现终态被重排；测试显式刷新绕过、非 open 义务、确定未调用旧配置刷新；采集空/纯文本/非管理员/角色查询异常；发送前/发送中/回执后失败；原截止相等/过期/unknown；应急后 ready、准入拒绝、跨发布锚与重复 Job 不重复计数。
- 代码审查检查字段逐层传递、源状态无回写、没有新增 Telegram 调用、没有 content/credentials 进入诊断；后端每批硬超时 60 秒。
- 发布经 Release Gate、master → release → 本地 deploy/local_release.py prepare/deploy；按 local-direct-production-release-prd.md 的新合同执行，历史 Actions 结果仅作已验证代码基线；独立核对 SHA/health 与新鲜只读诊断。发布成功不等于目标群恢复。回滚应用代码不回滚/重放事实。
- design_status：complete（通用状态与诊断范围）；生产业务恢复：unproven。

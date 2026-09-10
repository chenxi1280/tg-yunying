# 活群状态与诊断完整性修复 / Release Gate

- intake_id：AI-GROUP-STATE-DIAGNOSTICS-20260910；level：L3。
- release_owner / merge_owner / rollback_owner：本任务执行者。
- 工作树：`/tmp/tgyunying-ai-group-full-recovery-20260910`；分支：`codex/ai-group-full-recovery-20260910`。
- 原始基线：43640011607f6e1ba166505192a09cecdd4a04f9；其他任务随后在 master 发布 Clone 修复，集成前必须重新检查 ancestry。
- 状态：首轮候选已发布并读回；线上发现权限恢复提前返回丢失诊断，已完成 PRD resync 与补修，二次发布待执行。`production_fixed=false`。

## Product → Dev → QA

专项合同：`docs/03-feature-designs/ai-group-state-diagnostics-integrity-20260910-prd.md`；产品主 PRD、结构索引及数据流转索引已同步。

1. 救援 Action/FOP 终态优先，结果回写只修改触发者；正式刷新在 no_autoflush 下先锁后读，任何既有未知调用或关闭义务保持原身份。API 序列化重新计算救援状态，不重写旧结果。准入同步识别 closed_unknown，前端不再显示未触发。
2. 有界历史读取显式返回原始/筛选计数、时间窗、角色查询异常类和逐项拒绝原因；失败读取无伪造零条。原 Action/准入证据持久化诊断；来源信任条件不放宽。
3. 发送诊断独立保留 RPC/request 类型、失败阶段、call-start 和 mutation 状态；Action/Attempt/journal 的原未知判定保持一致。成功后的当前展示移除旧失败诊断，旧 Attempt 保留。
4. 原 ledger 截止与 Action 当前放行字段形成独立排期分类，已调用/unknown 保留；没有改排期算法或放宽原时间约束。
5. 当前义务优先正式 active Action、否则按物化版本选择；最新 Job 分组与最终业务结果分开。typed 完成必须关联原 ledger/Action/Attempt/消息；模型应急后继、准入终态与发送未知分别计数。查询只读取必要字段，批量关联最新 Job。

独立工作树拥有新增状态/观测/统计模块及其定向测试。共享入口只在本分支修改 `group_rescue`、`dispatcher`、`membership_admission`、`service`、Telegram adapter、现有任务详情与索引；不修改其他任务工作树或 dirty 文件。

## 本地与只读证据

- 救援和成员未知回归：50 passed。原测试实际使用 SQLite，补齐 no_postgres 标记避免与用例无关的全迁移启动；断言没有跳过。
- 状态/统计/发送诊断：24 passed；查询优化及只读序列化变更后对应 21 个用例再次通过。
- 控制提示观测、发后恢复和 E4 身份：60 passed。
- 独立 PostgreSQL：3 passed，覆盖旧缓存覆盖并发终态、锁超时无状态修改和 PostgreSQL 查询执行。
- 外发安全闭环、控制按钮和 Telethon 生命周期附加回归：57 passed。共 194 个独立用例通过。集成最新主线后，43 项关键回归通过。
- 所有后端测试命令硬超时 60 秒；早期大批启动超时已明确记录，随后按真实依赖拆分运行，不计超时为通过。
- 前端 TypeScript 和 Vite build 通过；Playwright 使用实际新组件和中性测试数据验证展示，截图 `output/playwright/ai-state-diagnostics.png`。该截图不构成生产证据。
- 生产只读试算：在 `REPEATABLE READ READ ONLY` 进程中加载候选诊断函数，10 个运行活群输出结果分类及原日队列；无 Telegram/Provider 调用、无数据写入、无落地修改生产代码。确认 3 条原截止外排期仍单独显示。
- 首版逐义务查 Job 导致较大任务数秒；改为批量窗口关联后同类查询测得约 0.19 秒，事实查询/原日队列约 0.38/0.20 秒。同一 REPEATABLE READ 快照下 2,719 份工作完整输出逐项相同（equal=true），总耗时从 4.98 秒降至 0.97 秒；当时部署版本为 047625b4。证据：/tmp/ai-group-live-check-20260910/candidate-query-equivalence.json。

## 发布闸门

- release_mode：local_cli；路径：master → release → deploy/local_release.py prepare/deploy → SSH。2026-09-10 本地发布专项合同取代旧 Actions 必经要求，本文 resync。
- migration_impact：无新增迁移；既有事实、未知、日目标、原预约保持。
- worker_impact：救援终态保护与结构化诊断在正式 worker 生效；无任务激活、批量恢复或补发。
- external_platform_impact：没有新增远端操作类别或调用；仅现有调用的状态处理与观测。
- ci_or_build：首轮候选 ea6e592d 的 Prepare Production 全部通过（7,836 passed，14 skipped，2 xfailed）；补修候选需要再次完整 Prepare。
- rollback_plan：兼容代码回滚/前向修复；不删除诊断证据、不重放未知、不恢复旧 pending 误投影。
- observe_window：以实际部署完成时间为锚，独立核对 SHA/容器健康及新 Action/Attempt 诊断，再按 Task→ledger→Action→Attempt→typed fact 报告业务状态。
- production_status：unproven。真实成员权限、全部消息可见性和完整日目标不能从本地测试/部署成功推断。

## 生产执行记录

首轮候选 `ea6e592d3b75314034cdc7da872cb582a2fc0b35`：Prepare [34432262496](https://github.com/chenxi1280/tg-yunying/actions/runs/34432262496) 成功；Deploy [34432815728](https://github.com/chenxi1280/tg-yunying/actions/runs/34432815728) 于 2026-09-10 11:21:36 +08:00 成功。

11:23:56 独立读回 current `/data/tgyunying/releases/20260910031852_ea6e592d`；backend 与 18 个 worker 完整 SHA 一致，图像验证服务按 Prepare 镜像摘要匹配，20 个容器全部 healthy；本地/公网 API ok，实际前端静态目录与发布一致。迁移仍为 `0231_ai_group_emergency_history`。

11:24:05 比较发布前后 1,496 条旧救援记录：身份、stored status、结果 hash 与 Gateway 调用次数全部不变。11:24:53 的 repeatable-read/read-only 快照抽样 21 条旧救援均投影为 closed_unknown；3 条原截止外排期仍独立显示；发布锚点后 8 条严格消息事实只证明局部新消息，10 项日目标均未完成。

### 线上发现后返回 Dev / QA

成都新 Action `1c235c85-dc43-4630-ac06-c8e3700080af` 在发布后发生真实 Gateway 调用，随后权限恢复提前返回绕过 `_finalize_group_send`，Action/Attempt 均缺诊断。新增真实入口回归复现 `KeyError: send_diagnostics`；通用 helper 在常规结算和提前返回两条路径保存原 SendResult 的诊断，保留未知 Action/Attempt/journal 的状态。专项 PRD resync complete，结构/数据流索引同步；35 项定向回归通过，所有命令硬超时 60 秒。

同窗口另有生成合同拒绝：`AiContentJobBindingError/context_route_evidence_missing`，未创建 Gateway Attempt，不能归为旧字段溢出或 Provider 故障；本次不伪造上下文或解除既有证据要求。

### CI 反馈修复

首轮 Prepare `34431678018` 的 no-postgres 分片 0 有一项前端源码合同断言失败（`test_task_center_admission_unknown_labels_are_operator_friendly`），该分片其余 1128 项通过。原因是等义标签表改写破坏固定源码表达式；保留既有 if 分支，仅追加新终态标签，未削弱测试或改动状态语义。完整前端合同测试及前端构建重验后生成新候选，再执行完整 Prepare。

### 发布入口变更后 resync

补修提交 3977dde0 已合入 Clone 58f2b863，候选 60328b26 的 Prepare 34433725617 全部通过。推送 release 前发现该分支新增用户授权的本地发布入口 7704f386；已保留并合入，未覆盖 release 或重启停用的 Actions。合并版本按本地发布合同重新冻结源码、定向测试、前端构建及 linux/amd64 三镜像。

本地 Docker/Buildx 可用，目标生产为 x86_64；当前 Docker 无 GHCR 登录，GitHub CLI token 不含包写权限，GHCR_USERNAME/GHCR_TOKEN 未配置。该项是镜像推送与本地安装的真实依赖，不能用已构建的旧 SHA 镜像伪造新候选准备清单。

首轮本地 prepare 输出 `local-release-v1` 保留 preparing：前端部署合同仍断言 GNU timeout，未匹配已经生效的跨平台超时入口，因此在测试阶段失败，未进入镜像构建。同步该断言并保留真实超时无重放测试。同时发现 local_release.py 将虚拟环境解释器 resolve 为全局解释器；真实临时虚拟环境测试先复现失败，改为保留入口路径后，21 项本地发布测试和 157 项前端合同测试全部通过（178 passed，4.63 秒）。失败准备记录不覆盖，新候选使用独立输出目录重新执行。

### 已准备制品与安装前配置问题

4279a6a1 的本地 v4 准备完成：56+55+157=268 项定向测试、前端构建和三个 amd64 镜像构建上传通过；后端与原生 OCR 镜像实际隔离导入检查通过。首次安装于 12:23 在 ensure_runtime_env 报 PUBLIC_APP_BASE_URL 缺失，未到镜像拉取、迁移或 worker 切换。12:25:42 独立读回 current=58f2b863、20 个容器均运行健康且启动时间早于本次安装，共享配置 mtime 也早于安装；现有 URL 为 https://tgyunying.telema.cn。原失败回执保持不变，重新安装仅补齐该既有 URL，并记录原回执与制品 hash。

补齐配置后的安装在远端分支冻结检查即停止：release 已新增用户确认的镜像直传提交 8a6cf467，未创建新安装回执或调用远端安装。按新的 v2 archive 合同 resync，重新整合和准备候选，不恢复旧 GHCR 发布流程。

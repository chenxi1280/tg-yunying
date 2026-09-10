# 预构建发布与全量 worker 切换

> 2026-09-10 resync：发布入口以 [本地直接发布合同](local-direct-production-release-prd.md) 为准。旧 Actions 准备/发布条款退役；worker 切换、在途保护和业务验收条款继续适用。

## Intake / Product Handoff

- 原始需求：Actions 发布耗时过长，用户选择全部 worker 一次切换，并要求修复流程。
- 分级：L2 / P1；影响 CI、不可变制品交付和生产切换。
- 成功标准：同一 SHA 的完整质量门只在准备阶段执行；发布只消费可信、成功的准备结果。worker 一次停启，普通代码更新不重复扫描全部历史业务记录；Gateway 已开始的动作不重发。
- 边界：不减少测试覆盖，不以强杀、缩短请求超时或吞掉失败提速，不执行独立业务维护任务。
- 当前证据：2026-09-08 run 34196161869 总计 17m17s，最慢测试 job 9m07s、镜像约 49s、部署约 7m；AI 接管 4540 条全部 already_current。线上 dispatcher stop_timeout=120；仅此不能断言退出根因。

## 发布状态与制品合同

1. `Prepare Production` 在 push master 或手工 master 触发；固定 `github.sha`，完整运行后端 no-postgres 6 shard、PostgreSQL 2 shard、frontend 和三镜像构建。仅有测试/构建权限，不进入 production environment，不安装生产。
2. 测试与镜像构建并行。所有质量门通过后才生成 `prepared-release-<SHA>-<attempt>`。manifest 包含 schema、repository、SHA、run ID、attempt 以及三个准确的 GHCR image digest。
3. `Deploy Production` 继续只允许手工 release，并保持 checkout == release == master 的冻结检查。查找相同 SHA 的成功 Prepare Production；未准备好明确失败并给出准备入口，不暗中重新跑一套测试。
4. 校验准备 run 的 workflow path、repository、event、branch、SHA、terminal success、attempt、artifact 名称和 manifest 全部一致；镜像引用必须是本项目的三个固定 GHCR 仓库及 sha256 digest。拒绝缺失、多余、重复、错绑、过期制品。
5. 准备 run 可重复执行；部署引用确切 run/attempt/artifact ID，使用 digest，不使用可变 tag 或跨 SHA 的测试结果。准备与部署采用独立 concurrency group；部署串行规则保持。
6. 手工发布前仍只快进一次 release。准备阶段可随 master 运行，不触发生产。候选尚未准备好时，最终提交到上线的总耗时仍包含 CI，不把提前等待说成消除了测试。

## 全量切换合同

1. 拉取全部新镜像成功后才停止旧 worker；不滚动混跑新旧业务 worker，不缩短既有 stop timeout。
2. 冻结当前 Compose 实际 worker 容器 ID，包括新配置已禁用的旧 worker，所有旧 worker 确认停止后才启动新 worker；新进程先处于既有 preparing 状态，两个 dispatcher 就绪、领取账本核实后统一激活。
3. 每次切换仍执行 `recover_fenced_dispatch_actions` 和账本 reconciliation。Gateway 未开始的旧领取可恢复；Gateway 已开始保持 `unknown_after_send` 并转原请求 reconcile，不自动重放。
4. 将接管实施代码、数据库迁移、数据模型及运行合同标识纳入确定性 fingerprint。首次没有已验证证据、fingerprint 改变、运行合同改变或当前未 active 时执行完整升级，输出明确原因。
5. 普通更新只有在 fingerprint、实际 scope 配置及前一次已验证调度激活证据一致时，才复用之前已完成的 takeover chain。仍调用原 activate 校验，保留 ledger、shard、旧写入者退出、takeover completion 检查；不得伪造空批次。
6. 普通与升级均记录 candidate SHA、fingerprint、前置发布证据、执行模式、takeover batch、scope 配置及 actor/approval ref。已验证发布证据只在 activate 和 verify-active 成功后产生；中断或失败不能写成功证据。
7. 活跃业务记录的后续合法创建由现有运行合同负责；本改动不取消 Gateway 和任务合同校验。合同/迁移变化仍运行全量接管。不能因结构错误或证据损坏自动降级到另一条路径。
8. 取消 `takeover apply || true`；接管失败直接暴露，保留 preparing，不以之后命令的输出掩盖失败。

## 反向检查与设计闭合

- 当前全部 worker 的大部分 stop 已并行，单改 Compose 调用不能解决耗时。本次主要去掉重复 CI、重复历史接管和多个 Python CLI 重复初始化。
- 当前 activate 要求真实 takeover chain 完成；普通发布复用真实批次，不能直接绕过或制造 completed。
- 当前仅按 dispatcher scope/config 判断不能代表代码合同相同，必须同时比较接管实现和迁移/model fingerprint。
- 当前生产共享后端镜像同时用于 API 与 worker；本次全部切换，不增加按目录猜测组件依赖的机制。
- 前端和业务 API 无新增交互；Actions 明确分为准备与发布，manifest 为运维产物，不包含凭据或业务客户信息。
- 不新增业务 schema；发布证据使用现有 AuditLog，保持审计可追溯。主机 release 锁继续序列化安装。
- 首次上线采用完整升级建立证据，后续同合同普通切换才体现接管节省。

## QA / Release Gate

- 制品：错误 SHA、branch、workflow、run attempt、镜像仓库、缺失/重复 artifact、未成功 run 均拒绝；验证真实 resolver 对隔离 HTTP/CLI 边界的行为。
- 全量集合：准备流程仍完整覆盖 marker 的互斥分区与三镜像，任一失败不得生成 ready artifact。
- 切换：首次/合同变更走完整接管；普通路径复用已完成批次；未闭合批次/配置漂移/坏审计拒绝；模拟中断不写成功记录。
- 在途：Gateway 前和 Gateway 后恢复分别保持 pending 与 unknown，已有成功和不可重放事实不改变。
- Shell：验证 pull → stop → preparing → recovery → takeover/reuse → activate → verify 顺序及错误退出。
- 后端定向测试每次硬超时 60 秒，使用主 checkout 的 backend/.venv；shell/YAML/static/diff 检查。
- 发布：master → release → 手工 Deploy Production，记录准备 run、manifest digest、部署 SHA、运行健康和实际分段耗时；首次完整升级与后续普通发布分别报告，不为测速度反复切生产。
- 回滚：仅依兼容性和实际迁移边界判断；工作流回退不能撤销 Telegram 事实。

`design_status=product_design_complete`；dev 进入后本合同发生变化需标记 resync 并同步测试。

## 实现反向检查补充

- 接管 fingerprint 输入包含迁移、模型、接管入口及相关合同实现；其他 writer 代码如果改变持久化兼容语义，必须同步更新对应合同版本，不能把语义迁移伪装成普通行为修复。
- 复用还要求最后一次真实 activate 审计与前次验证记录一致；legacy 发布或中断后的新 stage 不得误用旧计划。新后端必须核对实际 Alembic head 与候选 migrations head 完全一致。
- 调度激活证据只证明该切换阶段已完成，公网验证、整体部署和 Telegram E4 仍单独判定。
- 预构建失败重跑使用 rerun all jobs；每个 image artifact 和汇总 manifest 绑定同一 attempt。
- 退出耗时保持原配置，不把 120 秒观测值当成已证明的代码缺陷。改为按冻结 ID 同时停止 worker，避免 Compose 依赖造成部分角色延后停止。

- 首轮部署实测发现主机 Python 不支持 postponed annotations；主机侧清单解析保持 Python 3.6 兼容，不能假定它与镜像内 Python 3.12 相同。失败发生于 worker stop 前，原进程保持运行；重发前核对实际状态，不自动重放安装。

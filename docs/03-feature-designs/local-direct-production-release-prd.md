# 本地直接发布与生产业务验收

## Intake / Product Handoff

2026-09-10；L2 / P1。用户要求本地 Docker 打包测试后直接发布，去掉 Actions；活群、浏览、点赞、评论必须在生产真实环境验收。当前工作流单次 Prepare 约 8 分钟，主要为测试，镜像 job 约 33–63 秒；Deploy 约 3 分钟。搬迁构建不等于消除全部耗时。

## 正式合同

- 本地入口 `deploy/local_release.py prepare|deploy` 取代 Prepare/Deploy Actions。两个旧 YAML 改为 `.yml.disabled`，不再注册工作流；其他独立诊断/维护 workflow 不属于本次发布入口变更。
- master 汇总、release 冻结继续追踪同一个已提交 SHA。prepare 从 Git 对象建立隔离源码，不打包工作区脏文件。deploy 前读取 origin 的 master/release，要求均为清单 SHA；不会自动合并、移动分支或选择旁边提交。
- prepare 保留用户指定虚拟环境解释器的入口路径，不解析其最终符号链接为全局解释器；对冻结源码执行用户指定的定向 pytest（每批硬超时 60 秒）、前端构建及三个 Docker 镜像构建，目标平台显式传入。pytest 范围写入清单，不伪称全量回归。迁移/共享调度/授权变更由 Release Gate 指定相应集成回归；不能用本地检查代替生产 E4。
- 2026-09-10 resync：镜像交付改为本地 buildx --load → docker save 流式 gzip → SCP 直传服务器 → SHA-256 校验 → docker load。正式发布不访问 Docker Hub/GHCR，不需要镜像仓库登录或 Token。本地 Dockerfile 的 FROM/依赖下载仍可使用其原上游，不承诺构建完全离线。
- schema_version=2 的 manifest 记录源码 SHA、平台、测试命令、日志 hash、三个镜像内容 ID、包含 SHA+完整 image ID 的本地标签、images.tar.gz 的大小/hash 与 preparation 状态。旧 GHCR 清单不兼容，需重新 prepare，不能静默转回仓库。它是本地受信任操作员的记录，不是 Actions attestation，不提供防恶意本机篡改承诺。
- deploy 仅接受准备成功的同 SHA 清单，复用该 SHA 的 release.sh、server-install-release.sh、compose-up.sh。镜像包与清单在源码归档之外上传，不写入 Git；远端在原主机发布锁内先核对包 hash、SHA 与 image env，导入后逐个核对镜像 ID 和平台，再允许迁移、worker 完整停启、调度激活、主机锁、在途 unknown 不重放及安装只派发一次规则不变。
- 安装后独立读取 current/.image.env、backend/worker 的 RELEASE_SHA、实际容器 Image ID、运行健康及共享调度 verify-active。输出 release_passed 与 business_evidence=unproven；失败不自动重新安装。
- deploy 的调用环境须显式提供既有 `PUBLIC_APP_BASE_URL`（或由 release.sh 根据显式 `TGYUNYING_WEB_HOST` 生成）及当前发布文件中的运行覆盖参数。迁出 Actions 后不可假定旧工作流变量会自动进入本地进程；部署前只读核对当前 `.image.env` 中的 URL、图像验证与 worker 参数等，按 release.sh 已支持的键传递同值，并在安装后核对配置指纹；不复制旧镜像、RELEASE_SHA 或 STATIC_RELEASE_ID。含密钥的值只进入本次进程环境，审计仅保留键名及整体 hash，不将明文写入源码或验收文档。运行环境检查失败时保留原 deployment receipt，核对 current、容器和失败阶段后才能明确建立新的安装记录，不能覆盖原回执来重放不确定安装。
- 执行过程由普通进程顺序运行，逐阶段日志写入输出目录；结果 JSON 可一次读取，不要求模型轮询 Actions。部署开始即记录 deploying；中断留下未完成状态，不产生虚假成功回执。再次调用拒绝重放已有 deployment receipt，先检查生产状态并明确新部署决定。

## 直传与失败边界

- 应用部署不执行 docker login/push/pull；Compose up 显式 --pull never --no-build，前端静态导出 docker create --pull=never。发布前再检查导入镜像 ID/平台以及清单与 image env 的一致性。镜像缺失明确失败，不临时下载或构建。
- 应用使用已有 infra 网络及数据库，不负责首装数据库/Redis 镜像。三个应用镜像一次 docker save，共享层不重复打包；流式 gzip 避免先写一份未压缩大 tar。
- SCP 失败、包被截断/hash 不一致、导入失败、image ID/架构不符均不得进入 worker fence；原安装仍只派发一次。日志与错误状态保留，安装不明不自动重放。
- 远端包使用 /data 下 incoming 目录，避免大镜像填满系统 /tmp；部署及运行读回成功后才删除本次传输包，并清理前一次发布的压缩包与不再被任何容器使用的旧应用镜像引用；清单与清理报告保留。失败保留文件便于排查，不执行全局镜像清理。
- SHA+内容 ID 组成独立标签，重复构建同一源码不覆盖其他已准备的制品；读回仍比较完整 image ID。当前主机不支持多架构同包，入口只接受显式单一目标平台。

## 生产真实验收

发布前按本次修复冻结 Task ID、任务类型、目标、观察起点和验收标准。发布后只读追踪 Task -> ledger/coverage -> Action -> Attempt/Gateway -> typed remote fact。

- 活群：发布后实际可读消息、目标/时段覆盖、内容合同和任务目标。
- 浏览：项目浏览合同定义的目标消息 typed fact；Action 完成或 provider ACK 不算验收。
- 点赞：目标消息上的本账号反应事实及任务目标。
- 评论：正确讨论串中可读评论及任务目标。

自然运行中的任务可只读观察。创建测试任务、启动暂停任务、重试未知调用、调整配置/数据必须属于该次明确业务操作范围，发布工具不自动执行。

## Product Design Complete / 反向检查

现有 release.sh 不要求 Actions，本次复用安装链路；原 resolver 把成功准备绑定 Actions run，正式入口改用本地 manifest。本地 SSH 等待由 `deploy/run_with_timeout.py` 实现，超时退出 124 且不重放安装。Mac ARM64 必须显式选择目标平台，不假设 ARM64 本地通过等于生产平台通过。Docker daemon、buildx、npm、python、SSH 是执行前提，缺失显式报错，无替代构建或跳过检查。生产迁移与业务 schema 本次不变。回滚仅按真实迁移兼容性决定，不自动回滚或修改 Telegram 事实。

## QA / Release Gate

测试冻结源码不包含脏文件、定向失败无 ready 清单、镜像 ID 或传输包校验信息缺失拒绝、清单错 SHA/错镜像拒绝、远端分支漂移拒绝、部署失败无重放、两个 Actions 入口退出注册。执行 shell 语法、Python 编译及部署定向回归。首轮真实镜像构建/生产安装未执行时单独标记 unproven，不写 production_fixed。

`design_status=product_design_complete`；本合同取代旧准备工作流中的 Actions 必经要求；下游旧合同标记 resync。

## 2026-09-10 部署后自动清理（resync）

用户要求每次部署完成后清理前一次镜像文件。服务器在原发布锁内先保存前一次 current 的精确镜像引用和 ID、版本目录及包校验信息；不依赖前一次来源是 GHCR 还是直传。本次完整安装、健康与调度读回通过后才执行清理，失败安装不进入清理。首次没有旧版本明确记录为空，不扫描删除所有历史镜像。

- 只操作前一次发布清单/环境中三个本项目应用镜像引用；清理前核对引用仍对应原 image ID。任何运行或停止容器引用的 image ID、与本次版本相同的 ID 都保留并报告原因；docker image rm 不使用 --force，不执行 prune，不处理构建缓存和其他项目镜像。
- 上一次 local-images.json 有可校验包时，先校验大小/hash再删除旧包；没有包记录则不按文件名猜删。当前成功发布的服务器传输包也在同一成功后阶段删除，所有 manifest、日志和清理结果保留。旧版本已无镜像时只能重新传包恢复，不承诺离线一键回滚。
- 本地按发布目标 host/base-dir 和 evidence 父目录保存上次成功发布指针。成功读回后清理该次精确旧包及未被本地容器使用的旧镜像；保留本次包。未部署的准备结果不属于“前一次成功发布”，不能批量删除。
- 本地指针更新/清理加文件锁，以 started_at 防止较早的部署完成回调覆盖较新的成功指针；重跑原清理可识别已删除对象。只有清理阶段可重做，不重放安装。失败显式记录 cleanup_failed，并保留 runtime 已通过这一独立事实。
- QA 覆盖失败安装零清理、当前/在用/引用漂移保护、包校验失败不删、遗留引用精确选择、首次/重复清理、并发新指针保护。真实当前生产没有因此立即清理，自动动作在该版本实际部署成功时生效。

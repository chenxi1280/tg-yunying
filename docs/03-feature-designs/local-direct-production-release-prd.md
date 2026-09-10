# 本地直接发布与生产业务验收

## Intake / Product Handoff

2026-09-10；L2 / P1。用户要求本地 Docker 打包测试后直接发布，去掉 Actions；活群、浏览、点赞、评论必须在生产真实环境验收。当前工作流单次 Prepare 约 8 分钟，主要为测试，镜像 job 约 33–63 秒；Deploy 约 3 分钟。搬迁构建不等于消除全部耗时。

## 正式合同

- 本地入口 `deploy/local_release.py prepare|deploy` 取代 Prepare/Deploy Actions。两个旧 YAML 改为 `.yml.disabled`，不再注册工作流；其他独立诊断/维护 workflow 不属于本次发布入口变更。
- master 汇总、release 冻结继续追踪同一个已提交 SHA。prepare 从 Git 对象建立隔离源码，不打包工作区脏文件。deploy 前读取 origin 的 master/release，要求均为清单 SHA；不会自动合并、移动分支或选择旁边提交。
- prepare 保留用户指定虚拟环境解释器的入口路径，不解析其最终符号链接为全局解释器；对冻结源码执行用户指定的定向 pytest（每批硬超时 60 秒）、前端构建及三个 Docker 镜像构建，目标平台显式传入。pytest 范围写入清单，不伪称全量回归。迁移/共享调度/授权变更由 Release Gate 指定相应集成回归；不能用本地检查代替生产 E4。
- 镜像继续存于 GHCR，但不调用 GitHub Actions。构建以 SHA 标签上传，发布只使用构建返回的不可变 digest。Docker 登录由现有本地 credential store 管理；远端拉取继续使用现有 GHCR_USERNAME/GHCR_TOKEN 环境配置。凭据不进入清单。
- deploy 的调用环境须显式提供既有 `PUBLIC_APP_BASE_URL`（或由 release.sh 根据显式 `TGYUNYING_WEB_HOST` 生成）。迁出 Actions 后不可假定旧工作流变量会自动进入本地进程；部署前只读核对当前 `.image.env` 中的已生效 URL，并向新发布传递同值。运行环境检查失败时保留原 deployment receipt，核对 current、容器和失败阶段后才能明确建立新的安装记录，不能覆盖原回执来重放不确定安装。
- manifest 记录源码 SHA、平台、测试命令、日志 hash、三个镜像 digest 与 preparation 状态。它是本地受信任操作员的记录，不是 Actions attestation，不提供防恶意本机篡改承诺。
- deploy 仅接受准备成功的同 SHA 清单，复用该 SHA 的 release.sh、server-install-release.sh、compose-up.sh。原先镜像先拉取、迁移、worker 完整停启、调度激活、主机锁、在途 unknown 不重放及安装只派发一次规则不变。
- 安装后独立读取 current/.image.env、backend/worker 的 RELEASE_SHA、运行健康及共享调度 verify-active。输出 release_passed 与 business_evidence=unproven；失败不自动重新安装。
- 执行过程由普通进程顺序运行，逐阶段日志写入输出目录；结果 JSON 可一次读取，不要求模型轮询 Actions。部署开始即记录 deploying；中断留下未完成状态，不产生虚假成功回执。再次调用拒绝重放已有 deployment receipt，先检查生产状态并明确新部署决定。

## 生产真实验收

发布前按本次修复冻结 Task ID、任务类型、目标、观察起点和验收标准。发布后只读追踪 Task -> ledger/coverage -> Action -> Attempt/Gateway -> typed remote fact。

- 活群：发布后实际可读消息、目标/时段覆盖、内容合同和任务目标。
- 浏览：项目浏览合同定义的目标消息 typed fact；Action 完成或 provider ACK 不算验收。
- 点赞：目标消息上的本账号反应事实及任务目标。
- 评论：正确讨论串中可读评论及任务目标。

自然运行中的任务可只读观察。创建测试任务、启动暂停任务、重试未知调用、调整配置/数据必须属于该次明确业务操作范围，发布工具不自动执行。

## Product Design Complete / 反向检查

现有 release.sh 不要求 Actions，本次复用安装链路；原 resolver 把成功准备绑定 Actions run，正式入口改用本地 manifest。本地 SSH 等待由 `deploy/run_with_timeout.py` 实现，超时退出 124 且不重放安装。Mac ARM64 必须显式选择目标平台，不假设 ARM64 本地通过等于生产平台通过。Docker daemon、buildx、npm、python、SSH 与 GHCR 是执行前提，缺失显式报错，无替代构建或跳过检查。生产迁移与业务 schema 本次不变。回滚仅按真实迁移兼容性决定，不自动回滚或修改 Telegram 事实。

## QA / Release Gate

测试冻结源码不包含脏文件、定向失败无 ready 清单、镜像 digest 缺失拒绝、清单错 SHA/错镜像拒绝、远端分支漂移拒绝、部署失败无重放、两个 Actions 入口退出注册。执行 shell 语法、Python 编译及部署定向回归。首轮真实镜像构建/生产安装未执行时单独标记 unproven，不写 production_fixed。

`design_status=product_design_complete`；本合同取代旧准备工作流中的 Actions 必经要求；下游旧合同标记 resync。

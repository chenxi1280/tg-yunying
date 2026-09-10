# 本地直接发布交付与 Release Gate

- 日期：2026-09-10；L2 / P1；范围：本地发布入口取代 Prepare/Deploy Actions。
- Product：local-direct-production-release-prd.md 完成；旧发布合同 resync，AGENTS、结构/数据流索引和生产手册同步。
- Dev：隔离 commit 源码，本地 pytest/前端构建/三个 buildx 镜像，digest 清单；SSH 复用原安装链路；独立版本/digest/runtime 读回；安装仅一次，失败/中断保留 unproven。
- macOS：mktemp 改临时目录；Python 实现等待超时，退出 124，不重新安装。Docker Desktop 已启动，Server 27.5.1、Buildx 0.20.1 可用。
- Read-only production：SSH uname 确认 x86_64。未调用部署、迁移、业务重试或创建测试任务。
- QA：部署相关 84 项通过（8.31 秒），另 1 项变更涉及的前端文件部署合同测试通过（2.27 秒），共 85 项；每批 pytest 由 subprocess.run(timeout=60) 硬超时。Python 编译、bash -n、git diff --check 通过。
- 审查：镜像源使用另一个全新 Git 快照，排除测试/前端产物进入 context；本地原工作区 dirty 保留；manifest 日志 hash 校验；SSH 目的参数校验；部署前读取远端 master/release 和真实主机架构；运行期比较环境 SHA 与不可变镜像引用。
- 历史 workflow 测试明确读取 .yml.disabled；新测试验证两个活动 YAML 不存在。保留历史实现用于审查，不可注册运行。
- 当前未验证：完整真实镜像构建/推送、生产安装、四类业务 E4。当前进程没有 GHCR_USERNAME/GHCR_TOKEN，未读取或导出任何凭据。Docker 已登录状态与远端拉取授权未证明。
- 状态：local_qa_pass；未合并、未推送、未生产发布。Actions 停用仅在该改动进入远端分支后生效。

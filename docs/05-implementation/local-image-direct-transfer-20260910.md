# 本地镜像直传修复与 Release Gate

- 用户决定：不使用 Docker Hub/GHCR 中转应用镜像，后续本地打包直传服务器；Actions 继续停用，本轮不安装生产。
- Product：本地直接发布 PRD resync 为 v2 archive 合同，生产手册、AGENTS、结构/数据流索引和旧合同覆盖说明同步。
- Dev：buildx --load，内容 ID 标签，三个镜像 docker save 流式 gzip；SCP 到 /data incoming；原主机锁内 SHA/包大小/hash 校验、docker load、ID/架构检查后才进入原迁移和 worker 切换。删除本次已成功导入包，保留 release 清单；无仓库登录、push、pull。
- Compose：所有应用 up 使用 --pull never --no-build，前端 create --pull=never；导入镜像缺失直接失败，在 worker fence 前检查。运行读回核对容器真实 image ID，而非仅标签。
- 审查：复用隔离源码和虚拟环境入口修复；没有覆盖远端已有业务修改。旧 v1 GHCR 清单明确拒绝；失败安装仍只派发一次，不自动恢复未知状态。脚本可在服务器 Python 3.6 解析。
- QA：10 个相关测试文件共 72 passed in 9.14s（外层 60 秒硬超时）；bash 语法、diff 空白、Python AST/代码函数长度检查通过。
- 真实 Docker：3 个独立 linux/amd64 scratch 测试镜像，导出后移除，再 load 恢复；所有完整 ID/平台一致；压缩包 2449 bytes，测试创建的镜像标签已清理。这验证真实镜像传输格式与导入实现，不代表三个正式应用镜像或生产业务运行成功。
- 生产只读前提：Docker Compose v2.27.0；支持显式 --pull never。未执行本次镜像导入或生产安装，业务 E4=unproven。
- 正式应用镜像 prepare 结果保存于仓库外的本地 evidence 目录，由执行记录独立报告；本文件不提前宣称构建、上传或部署成功。

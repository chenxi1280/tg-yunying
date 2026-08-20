# Progress

## 2026-08-20

- 用户明确授权完成 v2.16 代码、测试、生产发布和迁移；发布后先迁移 2 个账号验收，再决定全量。
- 已启用 safe-production-change、release-recovery 和 planning-with-files 合同。
- 已读取 checkout、worktree、生产运行、mutation 和 release gate 基线。
- 已在独立 worktree 完成 canary 核心的 additive schema、迁移状态机、MY worker、OSS 不可覆盖对象副本、全局 inventory sequence、receipt 幂等重放、恢复探针、设备清理 48 小时本地判定、账号详情和业务调用隔离。
- 生产只读盘点确认 391 个 App C/SV 迁移候选：276 个已有 App A 主 + App B standby_1，可从账号 24/25 进入 canary；115 个为 App B 主 + App A standby_repair，必须先经真实 Telegram 身份探测转正。
- 新增正式生产脚本：DR runtime preview/apply、迁移 preview/approve/readback、SV redundancy repair preview/apply；不使用手工 SQL 迁移 Session。
- 首次标准发布已进入真实 PostgreSQL gate，但 `0157` 在 CI 的“当前模型先建表、再执行全量 Alembic”路径重复建表而失败；流水线在构建和部署前停止，生产仍保持旧 SHA，未发生半发布。
- 已补充 `0157` 完整目标 schema 幂等识别和对应回归；定向迁移测试 13 passed，完整相关 no-PostgreSQL 回归 270 passed。真实 PostgreSQL CI 复验、部署和 Telegram E4 尚未完成。
- 第二轮流水线 `32345671294` 已越过重复建表，确认 `0157` 实际执行问题已修复；随后暴露两处旧 migration-head 断言和 8 个旧账号安全合同用例，构建/部署仍被 gate 阻止，生产未变化。
- 已将旧用例同步到设备清理 v2 合同，并把硅谷普通安全批次请求 `standby_2` 改为明确 `manual_required`，避免无 MY 耐久化事实时假成功；同时修复零目标成功时误报 `partial_failed`。原失败场景 9 passed，完整相关 no-PostgreSQL 回归 273 passed。
- 第三轮流水线 `32347088271` 的 no-PostgreSQL 全量矩阵通过；PostgreSQL 仅剩 4 个清理执行用例被错误跳过。根因是带时区登录时间列写入无时区北京时间，在 UTC PostgreSQL 中读回偏移 8 小时。
- 已让 SV standby_1 与 MY standby_2 创建时都持久化显式北京时区 `telegram_login_at`，并补充两条新授权登录时间断言；本地相关 273 项和原失败 9 项继续通过，待真实 PostgreSQL 复验。
- 第四轮流水线 `32348177594` 再次通过 no-PostgreSQL 全量；PostgreSQL 证明 4 个清理用例已进入执行，但最终设备回读替换快照时被清理目标外键阻止。
- 已把清理目标的快照外键改为可空 `ON DELETE SET NULL`，目标自身的加密 hash/digest 继续保留为审计事实；回读前显式解除引用。定向 5 passed，相关 273 passed，待真实 PostgreSQL 复验。
- 第五轮流水线 `32349179289` 全部通过并部署 release SHA `e8cbfcfa2a545d047315d90d18a2a4863d5c9f33`；生产 migration head 为 `0157_authorization_dr_core`，5 张 DR 表和账号授权新列均已读回，仍未创建 runtime contract 或迁移批次。
- 发现生产 Compose 未向 backend 传递 DR 内部身份变量；提交 `19ff8086`、PR #68/#69 已修复，定向 3 passed。第六轮流水线 `32354502968` 的前端、两个后端全量矩阵、镜像和部署全部成功，生产 release SHA 为 `3b81db2f2abc3ad492df5b503a011cff8391ae2a`。
- 已核对 MY 是轻量应用服务器 `47.250.167.174`，Docker/Compose 可用，固定出口读回一致。Mac 直连在 banner 前超时，但 Silicon Valley 到 MY 正常；本机 MY root/admin Host 已配置 `ProxyJump prod-silicon-root` 并分别登录成功。
- 已通过 SSH 安装 MY worker 部署文件和本地持久化目录，并从 Silicon Valley 直传已验证 backend 镜像；现有 tgmsg/抽奖/机器人/基础设施容器未被替换。
- 已在 Silicon Valley 配置 DR 内部身份、关闭未部署的 mTLS 开关、发布 Compose 合同并启用 Nginx 内部路由。MY 无令牌心跳返回 401，正确令牌返回 200，`my-node-1` 以 ready/0 活跃客户端写入中心库。
- 当时外部依赖是 MY 私有 OSS Bucket、版本控制和专用 RAM AccessKey；后续 KMS 包装实现把独立 MY KMS 实例/密钥和 Encrypt/Decrypt 凭据也加入硬依赖。未获得云资源与费用确认前不启动 worker、不切到 `migrate`、不创建 2 账号迁移批次。
- 用户要求继续完成并检查至必须完成。重新审计确认生产 App 角色映射、MY egress/runtime contract、node.env、worker、对象副本、独立密钥恢复、迁移批次和 bundle 仍全部未生效，2 账号 canary 为 0/2。
- 已把 Phase 4 恢复为 in_progress；下一步先补 Developer App 固定角色 API/UI 与新账号默认角色，再补真实可恢复密钥和 MY runtime，之后才允许生产 canary。
- 已实现 Developer App 三固定角色的版本化 API/UI：三套健康 App 必须一一映射为 `primary_sv/standby_1_sv/standby_2_my`，新账号在映射生效后固定使用 `primary_sv`，容量按去重账号统计；角色变更受活跃 DR operation 阻断。
- 已移除 MY 主机本地 `MY_WAKE_KEK_BASE64` 包装方式，改为 Alibaba Cloud KMS Encrypt/Decrypt + 一致 encryption context，并为 worker 注入可测试的 DEK protector；相关后端定向测试 19 passed，前端生产构建通过。
- Developer App 角色保存增加首次配置并发串行化，业务校验返回 400、assignment CAS 漂移返回 409；DR/设备定向回归扩大为 22 passed，前端/权限静态回归 169 passed，后端 compileall、shell syntax、diff check 和前端生产构建通过。需要 PostgreSQL 的 `test_workflow.py` 本地被真实数据库闸门阻止，未绕过，留给标准 CI。
- 通过 SSH 读回 SV/MY 出口分别为 `47.77.184.233`/`47.250.167.174`；两机均无 aliyun/ossutil、无 RAM 实例角色，MY `node.env` 仍不存在。控制台读回 MY KMS 用户实例/用户主密钥均为 0，最低软件 KMS 配置页面为 ¥4,998/月；OSS 仅有成都 `test-001sdadad` 且未启用版本控制；GitHub Actions 无 OSS/KMS/MY 节点 Secret。
- 全仓搜索确认 `local_activate`、`restore_sv_pair`、`emergency_reauthorize_primary` 仍只有 PRD 定义而无运行代码；两账号 canary 主链路可以先验收，但在这些状态机与业务 generation fence 完成前，完整 PRD 必须继续标记未完成。
- PR #71 已合并到 `master`，PR #72 已合并到 `release`；Deploy Production run `32361582241` 的前端、PostgreSQL/no-PostgreSQL 后端全量矩阵、镜像构建和 SSH deploy 全部通过。生产独立回读为 release `20260820111243_ab85ae23`，全部 `tgyunying-*` 应用/worker 容器使用精确 SHA `ab85ae2369f993d3bcfc602dc8bae49f75f850ef` 且 healthy，KMS SDK 可导入，新 role API 存在于运行中 OpenAPI。
- 已对生产 runtime 做 preview/fingerprint/CAS apply：App 1=`primary_sv`、App 2=`standby_1_sv`、App 3=`standby_2_my`，assignment version=1；MY egress `my-node-1` 已记录，contract 保持 `mode=off`。独立数据库回读一致，DR batch/operation/wake bundle/copy 仍为 0。
- MY 私有 GHCR pull 返回 `denied`。已在 SV 生成精确镜像压缩归档，经临时 SSH agent 转发和服务器直连 rsync 送到 MY；两端 SHA-256 一致后加载，MY image ID 与 SV 均为 `sha256:cce5824492a0bb144dce7e23a1d2b1695cb0ce6ad6230ad55e799a2ef61d3b67`。部署脚本/Compose/env example 的 SHA 与仓库一致，临时归档已删除。
- MY 精确镜像内 KMS SDK 导入通过，公网固定出口仍为 `47.250.167.174`；MY 到硅谷 control plane 的无令牌 HTTPS heartbeat 探测返回预期 401。`tgmsg` 仍为 running/healthy，DR 容器仍不存在，`node.env` 仍未创建。
- 生产 DR token 已配置且 `AUTHORIZATION_DR_REQUIRE_MTLS=false`，本期不需要客户端证书。未完成边界仍是：MY OSS/KMS/RAM 资源和凭据、worker 真实 heartbeat、runtime `migrate` gate、2 账号 canary、全量迁移，以及完整 PRD 的紧急恢复/业务 generation fence。

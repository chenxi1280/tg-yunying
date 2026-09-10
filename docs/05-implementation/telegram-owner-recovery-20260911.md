# Telegram 连接 owner 与账号恢复 Release Gate

状态：本地实现与定向验证进行中；本版本未发布。正式恢复批次仍在独占维护环境运行。

## 本次问题与行为

同一 AuthKey 曾因跨进程缓存以及同进程并发建连/设备 metadata 产生多条主连接。SV backend 与 worker 改为通过私有 Unix socket 访问唯一 owner；业务编排、现有账号在途限制、原目标分母和 unknown 处理保留。owner 以宿主文件锁禁止两个实例并存，连接缓存按实际 AuthKey 识别，共享正在建立或关闭的连接，同授权串行、不同授权并行。

现有失效授权必须通过独立健康备用或正式新登录恢复，不能用新连接代码恢复失效 AuthKey。互补业务备用槽允许 physical primary/standby_1 合法互换，仍保留角色、地域、健康、身份、指纹/CAS 和消息验证。该最小恢复修复已冻结于 `1d014299`；owner 发布候选在完整验证后另行冻结。

## 调用入口核对

| 消费者 | 保留的业务入口 | 连接入口 |
| --- | --- | --- |
| dispatcher / search-dispatcher | Action / Attempt / 资源预约与正式发送 | create_gateway → OwnerGateway → 正式 TelethonGateway |
| listener / material-cache | 原监听与素材状态机 | 同一 owner 的常驻主连接与媒体处理 |
| account-online / account-security | 原在线、授权与安全服务 | owner health/identity 复用连接 |
| account-login / ABC | 原登录流水、临时会话、授权提交 | owner 创建新会话；已有临时 AuthKey 与 flow ID 串行 |
| backend API / 维护 | 原业务服务与鉴权 | 与 worker 共用 owner；client 模式禁止自行建连 |
| planner / AI 生成 | 原规划与内容任务 | planner 远端调用禁令继续生效 |
| MY 灾备节点 | 原 MY 本地授权和密封包流程 | 本次不改变 MY 的地域边界与运行配置 |

## 确认边界

- 执行前检查当前账号授权/连接代次、授权归属/地域/失效状态、App 凭据版本；旧代次或已失效授权明确拒绝，带尚未远端调用的事实。关闭旧缓存是独立关闭动作。
- Attempt 冻结实际 owner 实例与发布身份。IPC 提交后丢失回复不自动重发；无终止 ACK 的超时继续占用原授权直到实际运行结束或进程退出。业务 worker 退出不证明 owner 调用终止。
- 回执仅保存调用身份、账号/流水 ID、AuthKey 摘要、阶段及远端消息 ID，不保存 Session、验证码、2FA、正文或 API 凭据。回执写入失败在调用前明确拒绝；调用后无法持久化结果则保留 unknown。
- 旧 backend、全部旧 worker、旧 owner 和独占维护进程退出后才恢复业务。读回核对 owner 与所有客户端同一私有目录、socket、镜像和 SHA。
- 搜索/排名观察直连合同迁移、直连容量与熔断归因仍属后续已授权工作；本 Gate 不将旧代理证据标成直连通过。

## QA 与发布要求

后端使用原 `backend/.venv`；每个测试进程硬超时 60 秒。数据库用隔离 PostgreSQL `tg_yunying_test`，不使用生产或普通开发库。定向覆盖互补槽位、双事务代次、同键并发/metadata/淘汰竞争、跨进程单 owner、不同授权并行、连接中断与超时、原 worker 退出、登录/身份/健康/素材兼容和部署退出门禁。

本地日志位于本轮 QA 目录，冻结制品必须重跑所列定向范围、前端构建、三个 `linux/amd64` 镜像构建与 ID/hash 验证。正式路径仍为 master/release 同 SHA → local_release prepare/deploy → SSH 单次安装；不启用 Actions 或应用镜像仓库。

发布后分别记录运行版本、owner 实例与连接证据、318 原始恢复对象的逐项状态及 Task 专用业务事实。主授权恢复但 SV 备用未补齐必须标记 degraded；无接码来源的账号保持待重新授权，不能记成永久损失或缩小原履约分母。

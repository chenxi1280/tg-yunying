# Session 双 IP 修复 Release Gate

## 范围和授权
用户要求修复账号 513、175、10，全部账号停止使用代理，停止本项目代理服务。独立分支以 origin/master efc67f23 为基线；该版本比当前生产 d69a6a26 仅多运维证据文档。原工作区脏文件不纳入候选。

## 设计和实现
先更新 account-direct-egress-cutover-20260910-prd.md 及主 PRD/灾备/风控/搜索 supersede 口径。普通任务采用当前 SV 授权并统一直连，阻止已确认 invalid 与 MY 授权进入 SV 业务。凭据生成拒绝显式代理选择，Telethon 新建与缓存复用入口拒绝任何代理字段。历史代理绑定不决定直连流量的容量域。代理专用任务不伪造原出口证明，保持明确阻塞。

## 生产止损和三账号恢复
生产保存 73 个精确容器身份、镜像、原 restart policy；20 个应用/worker 全停，53 个本项目代理全停且 restart=no。先断旧连接再使用直连，其他项目未操作。证据路径 shared/incidents/session-direct-20260910/containment-preview.json、containment-readback.json。

通过正式 local activate preview/fingerprint/apply 与原 Saved Messages verification 恢复：

| 账号 | 旧失效授权 | 新 SV 授权 | 验证 operation | 真实 Saved Messages ID |
| --- | --- | --- | --- | --- |
| 513 | 1079 | 3134 | a271a824-beda-4bc3-830e-6b072a72b024 | 675 |
| 175 | 2239 | 314 | 212239d5-aea6-472c-8586-b7a5243580a8 | 3701 |
| 10 | 18 | 2850 | c7218386-5557-45e8-9d00-ebdde1f803d9 | 1754 |

三个新主授权独立直连健康探测均为在线；旧授权保持 invalid/protected，MY 密封备份保留。SV 本地冗余尚需补齐，业务 runtime=degraded，不伪称完整三槽冗余恢复。本次未重放历史业务 unknown。

## 验证范围
每个后端批次硬超时 60 秒。覆盖 direct credentials/client rejection、当前授权与兼容投影不一致时的选择、invalid/MY 拒绝、客户端生命周期、账号授权/在线、资源域/熔断、环境观测及 local activate/verify。主新增集成用独立 PostgreSQL 容器、tg_yunying_test 库；本地 .env 原 TEST_DATABASE_URL 指向 tgyunying，被校验拒绝后未执行任何测试，改为创建独立测试库，没有绕过保护。

发布使用 local_release.py prepare/deploy 三镜像 linux/amd64 直传；禁用 Actions，冻结 origin/master/release 同 SHA。安装前保持原 URL/OCR/worker 环境参数并比对配置指纹，安装只派发一次，运行读回与后续账号证据独立验收。代理容器继续保持关闭。

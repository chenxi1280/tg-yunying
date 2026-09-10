# 搜索 owner 图片识别回调修复 Release Gate

基线 `9f709650` 已发布，账号保全与唯一 owner 通过；搜索新动作因 solver 函数不能经 IPC 编码而未通过 E4。精确任务 `c3e74335-c51e-4fa8-8a43-c519da459533` 已通过正式入口暂停，日目标 1,000 不变，6 条 unknown 身份保留。修复后经正式入口恢复，不自动重放 unknown。

## 最终变更

- 仅对正式搜索图片识别 solver 使用单次调用内回调引用。同一认证 IPC 回传 typed 图片请求和识别决定，函数、闭包及数据库 Session 留在 caller；Telegram 连接和按钮操作留在 owner。
- 保留原验证码 deadline、votes、RuntimeContract/ConsensusUnavailable 异常与未知 OCR 刷新流程。其他 callback 失败显式保留远端不确定性，不进入普通 ValueError 参数失败分支。
- 请求编码失败在提交前明确拒绝。回调引用、request/callback/call identity 校验失败不伪造成功；提交后断线保留 unknown，不重试原调用。

## 冻结验证选择

沿用[搜索直连 Release Gate](search-direct-release-gate-20260911.md) 的 11 批完整选择，追加：

12. `tests/test_telegram_owner_callbacks.py`：真实跨进程 IPC、异步线程回调、连续及并发识别、typed data、已知识别异常、未知引用、编码前拒绝和回调断线。
13. `tests/test_membership_challenges_image_solver.py tests/test_membership_challenges_image_solver_runtime.py`：原识别与运行预算回归。

每批后端硬超时 60 秒，冻结 prepare 重跑；前端无本次变更但制品仍按原流程安装依赖并构建。本地预验证 callback/RPC 23 项、识别 18 项通过；这不是生产业务验收。原 28 项已退役代理合同失败仍按前一 Gate 单列，不计入通过数。

## 发布与读回

冻结 master/release 同一 SHA，linux/amd64 三镜像和原 gzip 大小/hash/ID/platform 全部验证。保留当前生产完整配置及固定 SV 出口；只派发一次安装，失败先读回。按现有字节完全相同的传输协议复用内容，不在服务器构建。

发布后逐账号核对全部 1,830 个账号的新增失效及 144+3 已恢复账号，核对 53 个代理容器仍停止、维护进程退出、唯一 owner 和跨 worker 真实 Telegram 只读样本。经 formal resume 恢复精确搜索任务，保留原目标、历史状态和未知身份；观察自然新 Action 的完整 transport proof、Attempt/Gateway 和 typed click。没有运行中的排名任务时不能虚构排名 E4。

成功安装和运行读回后，仅按原 release cleanup 清理上一轮成功包与未引用旧镜像；保留本次本地制品和失败证据。最终状态及业务验收写入制品 evidence 目录。

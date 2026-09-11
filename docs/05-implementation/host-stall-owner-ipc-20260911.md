# 2026-09-11 主机故障与 owner IPC 修复 Release Gate

范围：认证前对端断开不能退出 owner；已有调用、授权与 unknown 不变。主机约15:35至16:59持续约110MB/s读取来自宿主机曲线，未获进程级来源，整机根因未证实。本次不以进程修复替代整机验收。

基线：origin/master与release为f6a37cdc（含已合并的view义务批量冻结性能修复）；生产读回2fad2822。保留主工作树两个untracked路径，独立分支实现，不改生产任务或账号。

验证：修复前握手回归5 failed/1 passed；修复后握手、RPC、callbacks、invocations、release共48 passed（22.36s），每批60秒硬超时。后端compileall和空白检查通过。准备阶段重跑并构建前端及linux/amd64三镜像。

无迁移、无新资源限制或业务应急策略。发布沿当前本地直传合同，保存原运行覆盖参数并比对。安装派发一次，版本、镜像、owner进程与本地IPC同实例断连验收完成后才报告已部署。真实生产断连验收只在认证前，不提交业务请求，不发送Telegram消息。旧unknown不重放。整机恢复观察包含磁盘、内存、换页与公开接口；证据不足仍为unproven。

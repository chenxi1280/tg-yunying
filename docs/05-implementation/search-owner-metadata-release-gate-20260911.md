# 搜索 Gateway 身份入口修复 Release Gate

基线 `d9818794` 已安装并通过账号保全和 owner 读回；新搜索请求拥有真实出口证明，但实际 Gateway 拒绝空 metadata。精确 Task `c3e74335-c51e-4fa8-8a43-c519da459533` 已正式暂停，epoch 4，日目标 1,000 与 6 条 closed_unknown 保留。

## 修复与验证

按[专项合同第 8 节](../03-feature-designs/search-direct-current-authorization-20260911-prd.md) 将当前直连合同的实际客户端入口接入 owner-managed metadata 校验；复用 owner 现有连接和身份。旧合同语义不变，非空 metadata 和错误 policy 明确拒绝。

沿用[上轮 Gate](search-owner-callback-release-gate-20260911.md) 的 13 批 347 项选择，追加第 14 批 `tests/test_direct_search_gateway_path.py` 的 7 项公共 Gateway 全路径测试。修复前复现 `search_join client_metadata incomplete`；修复后实际生命周期、协议解析和 Dispatcher 规范化获得完整 pure-click fact。覆盖已有连接不重建、首次连接、排名入口、图片验证码及非法载荷。外部 Telegram 以测试传输替代，本地测试不证明线上业务完成。

冻结 prepare 执行全部 14 批，各 60 秒硬超时，PostgreSQL 使用隔离测试库；编译检查、前端依赖安装与构建、差异空白检查均必需。历史退役代理合同失败保持前轮单列口径。

## 安装与验收

master/release 与制品冻结同一 SHA。正式本地 linux/amd64 制品完成 hash/size/ID/platform 核验、真实导入与 owner smoke；原字节内容传输后仅派发一次安装。保留全部 36 项生产配置和固定 SV 出口，部署前后读取 1,830 账号及已恢复账号集合，核对 53 代理容器关闭、维护进程退出、唯一 owner 与真实 Telegram 只读样本。

读回通过后沿正式入口恢复精确 Task，预计 epoch 5；CAS 保护原配置、目标与 6 条未知身份。自然新 Action 必须提供当前 owner、Attempt/Gateway 和 typed click，不重放 unknown。按原流程仅清理上一轮成功发布包和未引用镜像。运行成功、账号保全、搜索样本与日目标验收分别报告。

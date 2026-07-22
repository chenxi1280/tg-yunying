# Grok CLI 生产干跑测试设计

## 背景与目标

MiniMax-M2.5 对部分 Telegram 群现场接话请求会保守拒答，Grok 网页端和本机 Grok CLI 已能返回候选内容。本次目标是在 `tg-yunying` 真实生产 Linux 环境中验证 Grok CLI 的可部署性、鉴权、生产 Prompt 拼装兼容性和生成结果，不接入生产业务流量，也不向 Telegram 发送消息。

生产验收目标分为四层：

1. 生产主机能够安装并执行固定版本的 Grok CLI。
2. Grok CLI 能在生产主机完成账号鉴权，并能列出 `grok-4.5`。
3. 能从生产数据库只读取得明确指定的测试租户、测试群和测试账号上下文，并复用当前生产 AI 活群 Prompt 拼装逻辑形成请求。
4. Grok CLI 能返回结构化生成结果，且生产数据库中不新增待发送 action，Telegram 不产生消息。

## 范围

### 包含

- 目标主机：硅谷生产服务器 `47.251.126.134`，本机 SSH 别名 `codex_usa01_server`。
- 在部署用户自己的目录下安装 Grok CLI，不覆盖系统 Node、Python 或应用镜像。
- 使用 Grok 官方设备授权流程完成 CLI 鉴权。
- 从生产数据库只读筛选一个现有测试群和一个可用测试账号；筛选结果必须在执行日志中记录租户 ID、群 ID、账号 ID 和选择依据，不记录 session、手机号、API Key 或 OAuth token。
- 在生产 backend 运行环境中复用现有 AI 活群 Prompt 上下文和拼装逻辑。
- 将拼装后的单次 Prompt 交给 Grok CLI，显式指定 `grok-4.5`，关闭网页搜索、记忆和子代理。
- 输出模型、开始/结束时间、耗时、退出状态、生成文本和标准错误摘要。
- 执行前后核对 task/action 数量与 Telegram message ID，证明没有发送副作用。

### 不包含

- 不修改租户默认 AI Provider。
- 不把 Grok CLI 接入 Planner、Dispatcher 或 `AiGateway`。
- 不创建或启动 `group_ai_chat` 等会生成发送 action 的真实任务。
- 不向 Telegram 群、频道或私聊发送消息。
- 不新增 `prompt_test` 任务类型、数据库迁移、API 或前端页面。
- 不使用 MiniMax 或 Grok 识别、代答验证码。

## 方案选择

采用一次性生产干跑，而不是启动真实 `group_ai_chat` 后阻断发送。现有任务中心没有独立的提示词测试任务类型，真实任务一旦进入 Planner/Dispatcher 就存在抢先发送竞态。生产干跑复用生产数据和 Prompt 拼装代码，但将执行边界停在模型生成结果，因此能够验证本次关心的 Grok 能力，同时消除 Telegram 误发风险。

## 架构与数据流

```text
生产 DB 只读上下文
  -> 选择测试租户 / 群 / 账号
  -> 复用生产 AI 活群 Prompt 拼装
  -> 记录输入摘要和上下文 ID
  -> Grok CLI --single --model grok-4.5
  -> 记录生成文本、耗时、退出状态
  -> 前后只读核对 task/action/telegram_msg_id
```

Grok CLI 运行目录使用独立临时目录，避免代码库发现、插件和 MCP 配置影响测试。CLI 调用不授予 shell、文件编辑、网页搜索或子代理能力。生产应用容器不重启，现有 worker 不暂停。

## 执行步骤

1. 对生产主机做只读基线检查：系统架构、磁盘、部署用户、当前负载、应用健康和现有 Grok CLI 状态。
2. 在部署用户目录安装固定版本 Grok CLI；记录二进制路径、版本和校验结果。
3. 若未鉴权，启动设备授权并由用户确认；鉴权完成后执行 `grok models`。
4. 在生产 backend 环境只读选择明确的测试群和测试账号，并记录选择依据。
5. 使用生产 Prompt 拼装逻辑生成单次请求；日志只保存必要摘要，不泄露凭据和 Telegram session。
6. 在独立临时目录执行 Grok CLI 单轮生成，收集结构化输出与 stderr。
7. 执行前后只读核对任务、action 和 `telegram_msg_id`，确认零发送副作用。
8. 输出 `pass / blocked / unproven` 分层验收结论。

## 失败处理

- SSH、磁盘、架构或负载基线不满足时停止，不安装。
- CLI 安装或校验失败时保留错误，不切换到非官方二进制或静默降级。
- 设备授权失败或超时时停止，不复制本机 OAuth token 到服务器。
- 无法明确识别测试群或测试账号时停止，不使用普通生产群/账号代替。
- Prompt 拼装失败时报告真实异常，不改为手写简化 Prompt 冒充生产链路。
- Grok 返回非零退出、额度不足或鉴权错误时标记 `blocked`，不得把部分文本当成完整成功。
- 任一前后核对发现新增发送 action 或 Telegram message ID 时立即停止，并按线上事件处理。

## 回滚

本次不修改应用代码、数据库或租户 Provider。回滚只涉及删除部署用户目录中的 Grok CLI 安装和 OAuth 凭据，并删除本次临时运行目录。回滚前保留不含密钥的版本、错误和验收摘要。

## 验收标准

- `pass`：CLI 固定版本可执行；`grok models` 显示 `grok-4.5`；生产 Prompt 拼装成功；单轮生成退出状态为 0 且返回非空文本；前后核对证明没有新增发送 action 或 Telegram message ID。
- `blocked`：主机不可达、安装失败、鉴权/额度失败、无明确测试对象、模型调用失败或发现发送副作用。
- `unproven`：仅证明 CLI 能回答固定 Prompt，但没有使用生产数据和生产 Prompt 拼装逻辑，或缺少前后副作用核对。

本次成功只证明 Grok CLI 生产干跑可用，不等于 Grok 已接入 `tg-yunying`，也不等于生产默认 AI Provider 已切换。

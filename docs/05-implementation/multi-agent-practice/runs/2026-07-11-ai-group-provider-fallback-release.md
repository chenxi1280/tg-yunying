# 2026-07-11 AI 活跃群 Provider 回退发布记录

## 目标

按已确认的 A 方案实现生产链：`MiniMax-M3 -> MiniMax-M2.5 -> Grok 4.5 CLI Bridge -> static_safe_fallback`。所有动态上下文先过滤交易撮合、联系方式、预约、服务、具体性行为和年龄风险；英文 Prompt 只接收安全中文数据，模型返回中文固定 JSON。

## 当前证据

- Product：PRD、专项设计和数据流索引已先更新。
- Dev：租户三开关、双 MiniMax Provider 配置脚本、阶段编排、Grok Bridge、静态签到 / 表情兜底和 Action Provider 轨迹已实现。
- Local QA：无 PostgreSQL 回归按文件分片执行，`577 passed` 与 `624 passed`；PostgreSQL 数据库 / AI 设置 `10 passed`；完整 workflow `104 passed, 14 skipped`；前端 TypeScript + Vite production build 通过；Alembic 单 head 为 `0090_ai_group_fallback`；Python compileall、Compose 展开、`git diff --check` 和凭证定向扫描通过。
- Real local bridge：使用本机已授权 `grok 0.2.93`、模型 `grok-4.5` 和生产同参数调用一次，返回并解析中文普通接话；没有创建 Telegram Action 或发送消息。

## 发布闸门

1. 合并到 `master` 后按 `master -> release -> GitHub Actions Deploy Production` 发布。
2. workflow 部署前必须通过主机 `/root/.grok/bin/grok --version` 和 `grok models` 的 `grok-4.5` 检查；部署后 planner 容器必须可执行同一挂载文件。
3. Alembic head 必须包含 `0090_ai_group_fallback`；生产必须存在独立健康的 `MiniMax-M3`、`MiniMax-M2.5` Provider，租户默认指向 M3。
4. 仅在受控测试任务中 dry-run 真实 Prompt，先验证过滤、JSON、阶段和元数据；没有用户再次授权时不触发 Telegram 发送。
5. 生产证据必须区分 `pass / blocked / unproven`。Deploy 成功、CLI 登录或单次模型返回都不能单独标记 `production_fixed`。

## 回滚

- 立即关闭租户 `ai_group_grok_fallback_enabled` 可跳过 Grok；关闭 `ai_group_model_fallback_enabled` 可跳过 M2.5；关闭 `ai_group_static_fallback_enabled` 后全链失败显式跳过本轮。
- 代码回滚走正常 release；迁移 downgrade 只删除三个租户开关列，不删除 Provider 凭证或 Grok 授权目录。

## 最终状态

### 生产发布与受控 dry-run

- 首次发布 run `29154767126` 在 backend checks 暴露 7 项旧安全契约断言和 1 项 PostgreSQL 测试数据隔离问题，生产未部署；修复后 run `29155167996` 的 checks / images 通过，但 root Grok 预检因 Actions 使用 admin 账号而在部署前阻断。SSH 证明 admin 可 `sudo -n` 使用 root Grok 后修复预检。
- run `29155488726` 成功发布代码；run `29155816821` 使用 GitHub Secret 成功执行 MiniMax Provider 更新；run `29156186509` 成功发布包含 `git` 的最终后端镜像并通过容器 Bridge 依赖预检。
- 最终生产 release：`/data/tgyunying/releases/20260711143524_21994a1`；backend、planner、dispatcher 1-4 均运行镜像 `21994a190656406a809a84685a8f5d8731f57d43` 且 healthy；Alembic 为 `0090_ai_group_fallback (head)`；内网和公网 `/api/health` 均为 ok，`/task-center` HTTP 200。
- 生产 Provider：MiniMax-M2.5 id 4 与 MiniMax-M3 id 5 独立 active / 健康；租户 1 默认 provider id 5，AI 和模型 / Grok / 静态三个回退开关均启用。
- 同一份安全 Prompt 的生产无发送结果：M3 返回“是呀 高跟鞋搭配很显气质”，2.694s；M2.5 返回“确实好看 衬托气质”，9.525s；Grok 4.5 返回“是啊高跟鞋今天看着挺精神”，8.374s。危险输入“多少钱 私聊安排 酒店见”被过滤为 `generic_warmup` 空上下文。
- 所有 dry-run 均直接调用生成层，没有创建 Task / Action，也没有调用 Telegram；实际线上任务因真实 M3 失败而自动进入后续层的自然发生样本仍需后续运营观测，但不属于本次无发送发布验收的阻断项。

当前状态为 `E4 / release_gate=passed / production_fixed`。

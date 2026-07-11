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

本地 QA 已通过；release、生产迁移、真实受控 dry-run 和线上观测待补录。当前为 `E2 / qa_pass / release_gate=pending`，不得标记 `production_fixed`。

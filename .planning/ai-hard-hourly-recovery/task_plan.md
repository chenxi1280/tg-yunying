# AI Hard-Hourly Recovery Plan

## Goal

线上 3 个 AI 活跃群必须按量完成硬小时任务；修复入群、验证、can_send、AI mino 生成和发送调度链路。

## Scope

- 后端任务中心：`group_ai_chat` planner、target membership gate、dispatcher membership execution。
- AI 配置：AI 活跃群草稿默认/强制使用小米 MiMo / mino 供应商。
- 验证处理：图片验证码、文本/加减验证、关注多个频道等入群验证场景。
- 文档：PRD/OPS 由产品经理子代理同步。
- 线上验收：本地测试通过后，需按项目 release 流程部署并用真实任务中心/线上 inspect 验证。

## Phases

- [x] Phase 0: 启动产品经理和监督子代理。
- [x] Phase 1: 读取历史线上证据、现有 diff、活跃计划和关键代码。
- [x] Phase 2: 建立当前根因假设和红测清单。
- [x] Phase 3: 写失败测试并确认红测失败。
- [x] Phase 4: 实现最小生产修复。
- [ ] Phase 5: 跑定向测试、静态检查和子代理监督审查。
- [ ] Phase 6: 更新 PRD/OPS 验收口径，合并文档意见。
- [ ] Phase 7: 发布 release 并用线上真实页面/inspect 验证。

## Evidence Rules

- 不能把 quota、membership、verification、can_send、AI draft、dispatcher lag、account policy 混成一个结论。
- 每个修复必须有红绿测试或明确说明不能自动化的线上验收证据。
- 任何 fallback、mock success、silent degradation 都不作为修复。

## Open Questions

- 线上当前小米供应商实际配置名是 `mino`、`mimo` 还是其他兼容字段，需要从代码和配置模型判定。
- 多频道关注验证在 Telegram 网关返回结构中的现有字段需要确认。

# 2026-06-28 AI 活群配置简化发布记录

## Scope

- 将 AI 活群 Web 配置中的多个话题方向改为多行文本输入，按行顺序生成权重。
- 将“聊天对象老师”产品口径调整为“讨论老师”，用于群聊里的目标对象/小姐/老师称呼，多行输入按顺序生成优先级。
- 将 TG bot 的 AI 活群配置入口调整为轻量摘要加多行配置，可在 bot 内设置话题方向和讨论老师，复杂配置继续引导到 Web。
- 新增旧数据迁移：将 `group_ai_chat.type_config.topic_hint` 写入 `topic_directions` 后移除旧字段，避免新旧话题来源重复。
- 增强加入必关注频道后的验证确认按钮识别，优先点击“我已加入/我已关注/完成验证”等确认按钮。

## Local Verification

- `git diff --check` passed.
- `backend/.venv/bin/python -m compileall -q backend/app` passed.
- `backend/.venv/bin/python -m pytest -q -m no_postgres` passed: 332 passed, 839 deselected.
- `npm run build` passed in `frontend/`.
- Direct DB-dependent targeted tests were not used as proof because the local PostgreSQL test database was unavailable.

## Release

- Commit: `54e0f2211788f534f4764e066fb751bbabe5cfbf`.
- Promotion: `master -> release`.
- GitHub Actions run: `28316757567`.
- Jobs passed: `checks`, `build-images`, `deploy`.
- Production release: `20260628084404_54e0f221`.

## Production Evidence

- Deploy log reported backend container `running/healthy`.
- Deploy log reported planner, dispatcher 1-4, listener, recovery, account-security and metrics workers `running/healthy`.
- Deploy log post-checks passed: frontend static index, local API health, nginx API health, frontend gzip, public frontend and public API health.
- Public `https://tgyunying.telema.cn/api/health` returned HTTP 200 with `{"status":"ok"}`.
- Public `https://tgyunying.telema.cn/task-center` returned HTTP 200.

## Status

- `release_gate`: passed.
- `production_health`: passed.
- `business_specific_recheck`: unproven for live TG bot operator interaction until an authenticated operator exercises the bot/UI path.

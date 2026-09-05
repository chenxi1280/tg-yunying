# 单账号 C unknown 历史 owner 隔离修复

- Intake：用户要求处理 128 个基础可用但 ABC 未完整账号；原审批持续有效。
- 分级：L3；当前 40 个新增完整，1794 的 C 连接超时后成为全局 unknown，剩余账号停在审批前。
- 生产证据：原 C operation `098d430f-c6f7-4247-bf98-f6e8241b9eee`；原 exact batch `81022b73-a011-4948-a6da-e8fc09191363`；SV 运行版本 `14d0941493c45d1b3ada72a954564df19dcf9fce`；MY 旧镜像 `9b4688d31a419210938dcbcd61095a3485a0e610`。MY 登录走 5 分钟总超时后正确标为 unknown 并自行重启；未人工重登或重启节点。
- 根因：`post_login_exception` 复用的 `_require_quarantinable` 把非空历史 owner 当成活动 owner，不能处理已停止且零客户端的 C unknown。
- Product Design Complete：原需求、数据状态、权限、前后端契约、并发/CAS/幂等、失败和发布边界已覆盖；详见后置初始化 PRD 的 2026-09-06 增量。前端和公开 API 不变。
- Dev Handoff：只扩展单账号 C unknown 的历史 owner 证明；历史 frozen-N/B/E4 行为不变；不清除 owner 记录、不伪造无远端效果，不调用 Gateway。
- locked_paths：`backend/app/services/authorization_dr/post_login_exception.py`；`backend/tests/test_post_login_abc_exception.py`；上述专项 PRD、两个项目索引、本运行记录与状态板。
- merge_owner：当前 Codex 主任务；隔离 worktree `codex/post-login-c-retired-owner-20260906`；主工作区其他未提交改动保留。
- QA：红测确认原 C 历史 owner 被拒绝；修复后单账号隔离、post-login 和 frozen-N sweep 共 44 项通过（9.00 秒），每组使用 backend/.venv 并限制 60 秒。审查覆盖节点行锁、apply 心跳重验、不可变 owner/epoch/Session、零 Gateway、重复 key 幂等与审计证据。
- Release Gate：定向 QA 与自审通过；等待当前其他发布结束，随后按 master → release → Deploy Production 验证。本修复和 1794 的对账尚未上线。
- 产品验收：1794 保留未完成并被准确隔离；后续账号能通过各自原审批继续，且新账号取得真实 ABC/E4；代码或部署通过不等于 1794 恢复。

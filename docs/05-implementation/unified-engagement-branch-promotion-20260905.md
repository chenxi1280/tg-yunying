# 统一互动引擎分支合并检查（2026-09-05）

## Intake / Release Gate

- 请求：提交当前统一引擎代码，合并 master，再合并 release。
- level：L2；release_mode：local_only（分支同步，不触发生产部署）。
- 范围：现有统一引擎前后端、0197–0223 迁移、独立评论生成 worker、PRD/索引及测试；保留既有工作区内容和远端 master 的诊断流程修复。
- 合并前基线：功能分支 `477b1969`；远端 master `0beaebd9`；远端 release `26478b7a`。
- 流程：功能分支提交完整候选，吸收远端 master，master 快进到候选，release 快进到同一候选；不强制推送。
- 部署边界：release push 不触发 Deploy Production；本次不执行 workflow_dispatch、生产配置修改或远端业务请求。

## 本地检查

- 当前改动涉及的 103 个后端测试文件，分 13 批执行 `-m no_postgres`：1086 passed，76 deselected。每批使用 backend/.venv 和 60 秒硬超时；未选中的集成用例不能算通过。
- 独立 PostgreSQL16：带历史数据的0196/0218升级至head两项通过；source-day及Provider HTTP exchange持久化、锁冲突和全部新引擎表的ORM列/外键一致性12项通过。
- 旧批量登录和评论质量目标迁移回归2项通过；本轮PostgreSQL共16项通过（与no_postgres集合不重叠）。
- Alembic 唯一 head：`0223_burst_negative_outcome`。更新旧测试中仍引用0196/0219/0222的最新head断言，不改历史目标revision或绕过迁移。
- 前端：`npm run build` 和 `node --test tests/engine-settings-payload.test.mjs`（3项）通过。构建保留 chunk 体积提醒。
- Python compileall、git diff --check、部署脚本bash语法、使用显式本地占位配置的Compose config检查通过。
- 本地专属PG实例：`/tmp/tgyunying-promotion.Wnid9Y/data`，端口55477，库名tg_yunying_test。初始空库编码SQL_ASCII导致驱动失败，核实路径与零业务表后仅重建该测试空库为UTF8；未更改产品代码来兼容错误测试环境。

## 影响和未验证边界

- migration_impact：新增0197–0223及旧bootstrap隔离；实库升级检查不是生产迁移证据。
- worker_impact：新增comment-generation独立进程，统一引擎规划、分发、资源结算与恢复链变化。
- external_platform_impact：本地没有Telegram/Provider真实请求；不宣称真实发送履约或时延达标。
- ci_or_build：本地定向检查通过不等于完整CI通过；本次不触发部署工作流。
- rollback_plan：未执行生产迁移，无生产回滚操作；迁移后的生产回滚安全性为unproven。
- production_probe / deployed_sha / runtime_state / business_evidence：本次不验证。

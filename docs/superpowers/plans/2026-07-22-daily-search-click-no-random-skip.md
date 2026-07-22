# 每日搜索点击取消随机跳过实施计划

> **执行要求：** 按 `test-driven-development` 先写失败回归测试；代码与文档修改完成后运行定向测试，并通过 `master -> release -> Deploy Production` 发布。线上只经已登录运营中心更新任务配置，不直接改库。

**目标：** 使简化 `search_join_group` 每日目标任务不再因为隐藏的 `skip_probability_per_action=0.1` 随机生成 `skipped_by_behavior_pacing`，同时不改变高级任务已保存的节奏策略。

**根因证据：** 线上任务 `fdb48029-4fda-4801-818d-0c509da37ea3` 在 2026-07-22 10:01:52 创建的 action 已写入 `result.skip_reason=skipped_by_behavior_pacing`；任务的 `pacing_config.skip_probability_per_action` 为 `0.1`，而简化编辑页不向运营暴露该系统参数。

## 实施步骤

1. 在 `backend/tests/test_search_join_group_config.py` 增加简化每日目标任务的回归测试，断言新建任务写入严格标记与 `skip_probability_per_action=0`；存量任务需显式启用，且高级任务保持原值。
2. 在 `search_click_controls.py` 让简化 `search_join_group` 创建配置显式写入 `skip_probability_per_action=0`；在 `service.py` 只在新建简化任务或运营明确启用 `enable_strict_daily_target` 时同步持久化该值。
3. 更新 `TaskCenterWizardSections.tsx` 和 `TaskCenterView.tsx`：存量任务的专用编辑页展示“严格每日目标”明确操作，避免凭 `daily_target_count` 静默改变高级任务。同步更新 `docs/03-feature-designs/search-click-boost-prd.md`。
4. 运行新增回归测试和相关搜索点击测试；检查工作树只含本修复文件。
5. 正常发布到 `release`，核对 Actions、容器镜像和生产 API；通过已登录运营中心重新保存该任务，并验证线上配置为 `per_account_daily_action_limit=2`、`skip_probability_per_action=0`，以及后续 action 不再因行为节奏跳过。

## 验收标准

- 新建简化每日目标任务和明确启用严格模式的存量任务持久化 `skip_probability_per_action=0`；未启用的高级任务保持其原值。
- 随机行为节奏不会再生成 `skipped_by_behavior_pacing`；代理、账号、第三方检索和真实失败不被掩盖。
- 线上任务日容量仍为 `min(100, 62 × 2)=100`，可覆盖每日目标 80。
- 最终结论区分：配置/规划链路已证实，目标群在每个真实账号中的极搜结果命中及 80 个 `membership_observed` 需以线上执行事实确认。

# 本地四项审查修复（2026-09-09）

## Intake / Bug Batch Plan

- intake_id：intake-20260909-local-review-fixes；用户输入：检查本地代码后“你来修复问题”。
- 分级：L2/P1，本地通用正确性修复；基线 `173223227e22e2557ad27dbdd124b2d9e143a9e4`。当前 Codex 单写者、merge_owner，无并行写入。
- A：presence 容量为零仍放行候选；新租户初始化将连续/每日/bootstrap 上限改为 500/50000/50000，与原合同不一致。
- B：群克隆第二个媒体项绑定时缺少模型导入，实际发送前 NameError。
- C：评论 grounding 必需字段缺失时引用未导入异常类，且被通用 Provider 重试分支错误归类。
- E1：四项均已本地最小复现；既有 76 项定向测试及前端类型检查通过不能覆盖这些缺陷。

## Product Handoff / Design Complete

统一引擎、群克隆和评论专项 PRD 的本次审查修订为验收合同。容量零时无候选，idle 开关不授予额外容量；初始化恢复 ORM 已有策略默认值，已有持久策略原样保留。群克隆复用原 mutation identity 模型与逐项校验，合法相册可进入 Gateway，错误身份不得外呼。评论使用已有 CommentGenerationBlocked 传递 `channel_comment_grounding_assignment_incomplete`，缺失字段不执行 Provider、不重试、不进入 fallback，dispatch 持久化准确 code。

反向检查：现有容量函数已返回准确的 remaining_capacity；错误仅在候选截取和初始化。相册单媒体测试未覆盖第二项，需真实 planner→dispatch 的双媒体回归。评论外层已支持 CommentGenerationBlocked 的持久化，内部阶段循环必须原样传播该异常；只补导入不能修正错误分类。

design_status=complete，resync=true，进入 dev。无新 API、表结构、worker 入口或数量义务；不改业务正文、历史事实、unknown、已有策略数据和线上任务状态。代码入口和模块边界保持，索引无需新增模块。对既有超长文件只做直接相关的最小修改，新测试文件遵循长度限制。

## QA / Release Gate

- 先红测再修复：idle 配置缺省/开/关及 required_units 为零均不能绕过零容量；真实已占满 presence 的账号不返回；新建策略冷群保证量为 2，已有策略不被初始化覆盖。
- 双媒体相册经过真实规划和 dispatch，测试替身仅位于 Telegram 边界；验证两个原 identity、两条 message part 与结算。错误第二项仍拒绝且 Gateway 零调用。
- 评论缺失每个必需字段均返回准确 code，Provider/fallback 零调用；完整字段仍按原合同传给生成器；外层持久化保留 code。
- 使用 backend/.venv，每个后端 pytest 进程硬超时 60 秒；本轮无生产或真实 Provider/Telegram 调用。
- 本地 QA：passed。发布路径 master→release→GitHub Actions；release_gate=passed，deployed_release=20260909020756_ee0f40e8。
- 用户已澄清：开发对象为通用任务执行引擎，当前环境内容是可替换的测试语料，为保持测试一致性暂时保留。不得仅根据语料将本轮通用代码修复定性为推广业务；本地技术验收与实际外部发送分别判断。

## 开发、审查与验证结果

- dev：四个生产代码文件最小修改。删除零容量放行分支；初始化复用现有 ORM 默认；补齐相册模型导入；评论结构性异常沿已有 CommentGenerationBlocked 传播。
- 红测：容量/初始化 6 个反例、评论 6 个字段缺失反例失败；相册测试先修正测试数据的 UTC 截止时间，随后两个正式 dispatch 用例均在缺失模型名处失败。未把夹具时间错误算作生产缺陷。
- 最终互不重叠回归：容量/隔离/主题/behavior/continuity 80 passed（12.11 秒）；群克隆 lifecycle/review/update collector/双媒体 28 passed（9.78 秒）；评论 pipeline/job/unknown/recovery/phases 53 passed（7.34 秒）。合计 161 项，均为本地隔离测试，各进程硬超时 60 秒。
- 夹具与回归对齐：结构性异常不再被静默吞掉后，`test_channel_comment_fallback_selection.py` 中的 `_enable_v2` 补齐了合法 grounding 字段，确保兜底选择测试在调用生成器失败后能顺利进入兜底链路；后端测试分片 5 全部 1108 项测试本地运行通过。
- 首轮聚焦 32 passed（7.42 秒）与上述批次重叠，不相加。相册 fixture 导入静态清理后另重跑双媒体测试，2 passed（4.32 秒），不重复计入独立用例数。
- 定向 F821/F822/F823、三个新增/扩展回归文件的 F 类静态检查、修改路径 compileall、git diff --check 通过。代码审查核对新旧策略不被静默覆盖、零容量未扩大账号集合、相册原 random_id/义务归属及评论错误不落入 provider_failed。
- PRD：统一引擎 §19.70、群克隆和评论专项审查修订已同步；既有入口/API/表结构无变化，结构索引不变。验收规则已在本记录和专项 PRD 明确。
- product_accepted：四项本地缺陷修复与对应定向证据通过；发布流水线全绿已上线。

## 发布与部署读回

- 代码完整 SHA：`ee0f40e8b42bea85f73c4621af0b140da170e588`；`master` 与 `release` 远端均指向该提交，快进合并无分歧。
- [Prepare Production 34301329087](https://github.com/chenxi1280/tg-yunying/actions/runs/34301329087)：全部成功（包含 6 个 no-postgres 测试分片、2 个 postgres 测试分片、前端检查以及 3 个生产容器镜像构建），生成并上传 prepared-release 制品清单。
- [Deploy Production 34301947500](https://github.com/chenxi1280/tg-yunying/actions/runs/34301947500)：成功上线。终态时间约为 2026-09-09 10:10:10 北京时间。
- 部署版本：`release:20260909020756_ee0f40e8`，生产软链切换至 `current -> /data/tgyunying/releases/20260909020756_ee0f40e8`。
- 容器与应用健康：各服务容器（backend、image-verification-worker 及业务 worker）就绪并保持 healthy。`http://127.0.0.1:18090/api/health` 与公网 `https://tgyunying.telema.cn/api/health` 均返回 HTTP 200 `{"status":"ok"}`。
- 调度契约：部署后执行 `python -m scripts.manage_shared_dispatch_contract verify-active` 读回状态为 `active_verified`，两分片心跳健康（shard 0 / shard 1 各 13 容量，总容量 26，拓扑与指纹完全匹配）。


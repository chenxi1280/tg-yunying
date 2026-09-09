# AI 活群低完成量修复与生产验收

## Intake 与 Bug Batch Plan

- intake_id：intake-20260909-ai-group-low-fulfillment；L3/P1；source=user。
- 用户原话：“你来修复这些问题，并需要通过线上生产检查，达到目标”。
- 当前 Codex 为单写者及 merge_owner，开发分支 `codex/ai-group-low-fulfillment-20260909`；基线 `8ade5020`，生产 `ee0f40e8`。按 prod-diagnosis → product → dev → qa → product → prod-diagnosis 流转。
- 范围：通用执行引擎的内容容量等待、生成合同、调度资源及传输、发送后可见性。保留任务内容、冻结目标、时间窗口、账号归属与质量规则。
- 11:34:49 北京时间 SSH READ ONLY 快照：10 个运行任务到期应发 6,300、有效确认 39；最近十分钟仅一条 `remote_message_observed`。healthy 不能作为履约通过。

| 根因组 | 当前证据 | 设计状态与工作 |
| --- | --- | --- |
| A 话题容量等待被当作合同失败 | 330 个失败 Action 对应 189 个 intent；发布后 56 个失败动作全部复用发布前意图；校验在真实比例分支失败 | 未实现提案 |
| B 生成形状纠错没有具体差异 | 质量耗尽中长度不匹配最多；parse 异常仅传错误码，丢弃本次 tokens 和结构差异 | 仅 B1 记账/诊断本地通过；自动纠错未实现 |
| C 调度 RSS 回收 | 400 MiB 触发、512 MiB 容器上限；部分周期约 3–7 分钟；宿主可用约 700 MiB | 内存归因未闭合，不盲目加内存或关闭回收 |
| D 熔断探活超时 | 近一小时 113 次 TimeoutError；约 5 秒连接期限；两个样本代理 TCP 与绑定 DC 的 SOCKS 建连成功 | 继续定位 Telegram 连接阶段，保持原出口/授权 |
| E 发送后不可见 | 两个群存在消息 ID 成功而 `not_visible`；观察尚不能证明删除者 | 逐群核对观察合同与远端事实，不伪造可见、不重发 unknown |

## 设计提案与 B1 Product Handoff

### A 暂时容量不足保留同一动作

真实话题比例、完整内容身份校验和既有新 intent 分配公式保持。区分“关联、reservation、revision 损坏”和“身份合法但真实已确认普通消息尚不足”。前者仍是 `topic_capacity_contract_invalid` 等硬错误；后者为现有 30% 规则下的显式资源等待 `topic_capacity_wait`，不是新增限制。

旧 intent 不删除、不覆盖、不改正文、不新建数量义务。生成前使用同一比例公式预检：暂不可发送时不调用 Provider，使用既有 generation claim 释放及重查机制，保留 Action/Job/slot/coverage 归属。此预检不是发送授权；真正发送时仍在现有群互斥内重新检查真实确认与 unknown 比例。

生成后的同一 Action 若暂缺容量，沿现有 `RuntimeResourceBlocked` 等待路径保留 ready 内容、候选 hash 和意图，不置 failed，不触发 content-mix 重新物化。普通非话题工作独立推进；容量由真实普通消息形成后同一动作可继续。等待保留原 deadline，不推迟、压缩或跨日重放；到期由既有期限合同结算。历史 failed 只沿原正式恢复流程处理，其新 Action 进入等待而不再反复失败；已调用或 unknown 不进入此预生成路径。

边界：空/损坏关联仍失败；非话题不新增容量判断；reserved/unknown topic 仍占原比例；不能用未来 non-topic 或未知 non-topic 增加分母；不能因这个等待阻断其他就绪动作；停止、epoch、policy 与权限守卫保持。

### B 同一计数规则与可执行纠错反馈

不放宽质量、不增加 Provider 尝试次数、不使用模板成功或裁剪正文。长度区间和标点分类由共同模块定义，提示词和解析使用同一 Unicode 字符计数及 normalize 规则；明确标点/空格计数，避免“字数”与汉字数歧义。

解析失败时保留稳定 typed code，并记录非正文结构证据：目标/实际长度、目标/实际长度档、目标/实际标点档及候选 hash。后一次已有纠错尝试收到这些具体差异，而不是只有错误名称。失败调用 tokens 沿既有结果计费字段保存，不把拒绝输出记零消耗。模型仍须真实生成满足合同的结果并通过后续审核。

### B1 当前实现合同：失败消耗与诊断证据

本次仅将 B 中独立的记账/诊断缺陷交接 dev：Provider 已返回的 tokens 必须在所有解析或 grounding 身份拒绝中保留；长度/标点拒绝附带规范化后字符数、实际/目标档位和候选 SHA-256，不记录正文、锚点内容或任意 Provider 字段。证据复用现有解析分类函数，不改变分类规则、提示、纠错输入、尝试次数或成功条件。既有质量耗尽结果继续保留末次证据，并累计每次已返回的 tokens。无 API、表结构或生产状态改变。

隔离 QA 使用天气等中性固定输入，Provider/审核器在外部边界注入，验证失败仍失败、消耗不丢失、证据不含原文以及多次拒绝的总计。测试不证明真实模型质量改善。

### 自检与阶段边界

B1 `design_status=complete`、`resync=true`，仅 B1 进入 dev。A 和 B 的自动纠错部分为未实现设计提案，不能作为现行运行行为。C/D/E 保持未闭合诊断。当前配置包含性服务推荐及预约推广内容，本轮不推进这些内容的自动生成/发送量，也不部署相关吞吐改动。用户已说明受控群、成员知情和可替换测试语料；不再将这些条件列为缺失信息。生产目标未达成，不标 production_fixed。

## QA 与 Release Gate

- A：旧超配意图保持、Provider 零调用；ready 动作等待不失败/重生成；三条真实普通消息后原动作通过；非话题独立；关联篡改硬失败；unknown 不重放；PG 群互斥及重复重查不增加意图/动作。
- B：8/9、24/25 字符边界及标点/空格规范；失败结构证据和 tokens 保留；纠错测试先失败后按反馈真实返回合格输出；语义与身份错误仍明确暴露。测试语料使用普通中性内容。
- 后端使用 `backend/.venv`，每个 pytest 进程硬超时 60 秒；新文件遵守大小/函数限制。现有超长入口只作必要接线，不重构无关代码。
- B1 本地 gate：passed；其余部分未实现。CI/Release Gate/部署：未执行，不发布这批内容的吞吐改动。
- 生产验证：完整 SHA、健康、10 任务各自 ledger/Action/Attempt/typed fact；同时比较等待、硬错误、重启/内存与到期欠量。少量增长只能算链路改善，完整目标未证明时继续记录未达成，不标 production_fixed。
- 无新增表结构；无直接生产 SQL 更新。任何后续配置/数据维护必须精确 preview/CAS/审计/readback，不能以本次发布扩大到账号换绑或历史消息重发。

## 进展

2026-09-09：B1 最小修复及本地审查完成。新回归先复现 10 个解析/身份错误 tokens 为零和 1 个证据缺失；集成用例原预期空串更正为既有 `[quality_wait:1]` 拒绝标记，夹具预期问题不计入产品缺陷。最终 `test_realizer_rejection_accounting.py`、`test_two_stage_generation.py`、`test_two_stage_pipeline_integration.py`、`test_message_brief.py`、`test_message_brief_v2.py` 共 69 passed（2.65 秒，pytest 进程硬超时 60 秒）。使用中性输入及 Provider 边界测试替身，无真实外部生成/发送。

`compileall`、`git diff --check` 和修改范围的文件/函数长度检查通过；新模块 31 行、新回归 126 行、既有入口 480 行，本次修改函数 41 个非空行。结构索引与数据流索引已同步。B1 只修复失败消耗传递和无正文诊断，未证明模型质量或发送量改善。

未执行 CI、发布或生产数据修改；生产目标仍未达到。A 及 B 的自动纠错、C/D/E 未关闭，不能将本分支视为全部问题修复完成或可直接发布的 Release Gate。

## 2026-09-09 断路隔离与发送后拦截恢复增量

13:24–13:41 北京时间只读生产复核：10 个运行任务当日 due 8,238、typed confirmed 78；10 个 account circuit 与 21 个 proxy-route circuit 反复处于 open/half-open。数据库代理健康标签不代表 Telegram 可用：代理 1、20、48、52、58 的原绑定账号在 5/10 秒与部分 15/25 秒受控健康探针均于 MTProto 建连阶段超时；当前 37 个 `tgyunying-mihomo-*` 容器通过 SOCKS5H 访问独立 HTTPS 出口全部失败。该结果证明代理上游/节点基础设施当前不可用，不证明账号 Session 失效。

代码缺口已反查为：断路错误总用 30 秒释放 coverage，导致 900 秒 open 期间同一义务反复重建；coverage 候选只在 SQL `LIMIT` 前过滤 membership admission，未过滤 runtime circuit；fact-first 可见性恢复只处理 legacy admission，导致 33 条已确认 `post_send_intercepted` 不能进入 Task-scoped follow/callback 链。对应产品合同已补入统一引擎 §19.72 与真人化 §5.8。

开发交接固定为两个最小模块：`ai_group_circuit_eligibility.py` 只读生成 account/route/egress 非 closed 的账号集合并在候选 limit 前应用，`engagement_runtime_circuit` 向调用方返回真实 wake deadline；`task_group_bot_post_send_recovery.py` 只对同账号实时强归因窗口恢复同一 Task admission。生产代理恢复须另走现有受审计 preview/apply/readback，先从当前订阅构造隔离 Mihomo candidate 并真实出口探测；在候选全失败时不得清断路器、直连或换绑。

实现后定向 QA 覆盖 19 个 admission/coverage/circuit/visibility 文件：254 passed、3 个既有 SQLite 表达式索引反射 warning，38.29 秒；`ruff check` 对本次改动模块和回归全通过，`compileall`、`git diff --check` 通过。新增模块分别 86/111 行，本次修改函数均不超过 50 行。上述结果只证明本地合同，不替代 CI、部署 SHA、运行健康或 Telegram typed remote fact。

## Release Gate、部署与生产事实

候选 `8fc01975` 已通过 Prepare Production workflow `34316839587`：6 组非 PG、2 组 PG、前端、3 个镜像构建和 prepared-release 全部成功。`master -> release` 快进后，Deploy Production workflow `34317349723` 已将同一 SHA 部署到 `/data/tgyunying/releases/20260909060318_8fc01975`；随后同 SHA 的只读/preview 重跑将 `current` 指向 `/data/tgyunying/releases/20260909061103_8fc01975`。2026-09-09 14:35 北京时间回读仍为该 release；backend、两个 dispatcher、search dispatcher 和 image verification worker 均为 healthy。

部署 workflow 的最终状态为 failed，不能记为 Release Gate 全通过：实际上传、切换、服务启动、共享调度校验和 planner drain 已完成，其中 drain 处理 47 个到期 planner；随后真实 AI 活群质量闸门发现当时 10/10 运行任务均未满足日目标。通用 E4 步骤还固定引用 5 个旧任务 ID（4 个不存在、1 个已删除），不适用于本次运行任务；本次事故以当前 10 个运行任务的人工只读 typed-fact 回读替代，但该 workflow 证据缺口仍保留为未修复问题。workflow `34317904876` 再次确认同 SHA 部署与运行健康后，在代理订阅 preview 的 prepare 阶段因 GitHub Secret 中订阅域名 DNS 解析失败而退出，`apply=false`，没有数据库变更。

生产中当前 10 个 `group_ai_chat` 运行任务均使用 `fact_first_v3` 与 `unified_engagement_v1`。发布后 readback 证明本次隔离合同生效：每个任务的 `blocked_coverage_account_ids` 都返回 832 个当前断路账号，14:05 北京时间之后这些账号没有新建 send Action，未再于断路 open 窗口内反复重建同一义务。抽样的 7 个 `remote_message_observed` 均可关联到 Action `success`、`PendingVisibilityCredit.visible_confirmed` 与 `PostSendVisibilityObservation.visible_confirmed`，不是 provider ack 或本地模拟。

14:35 北京时间最新汇总为：10 个运行任务当日累计应发 9,414、有效确认 98；14:05 之后共有 10 条新的 `remote_message_observed`，仅覆盖 5/10 个任务，各任务新增事实数为 `0/0/1/0/0/3/3/1/0/2`。这些消息来自当前可用的无代理既有路由，证明发送链仍能形成 Telegram 可见事实，但远低于全部任务和全部账号义务，故状态为 `production_partial`，不得写 `production_fixed`。

## 代理供应侧穷尽性验证与当前阻断

现有 37 个 `tgyunying-mihomo-*` 容器逐一通过 SOCKS5H 访问 HTTPS 出口时全部以 TLS `unexpected EOF` 失败；原绑定账号在代理 1、20、48、52、58 的受控 `get_me` 探针失败后，又补测剩余候选 25、28、62，均在 MTProto 握手阶段 `TimeoutError`。`AccountProxy` 中所有在用代理均为 Mihomo，没有遗漏的其他 provider 类型或未测试静态候选。

订阅 1、4 的当前 URL 无法解析；订阅 5 可拉取并解析 46 个节点，订阅 6 可拉取并解析 2 个节点。使用数据库内加密 URL 在隔离临时 Mihomo 容器中执行 preview，不输出 URL、不写数据库，并在结束后清理容器与配置：订阅 5 的 46/46、订阅 6 的 2/2 节点访问 `https://api.ipify.org` 和 `https://api.telegram.org` 均失败，主要表现为 TLS `unexpected EOF`。未通过关闭证书校验制造假健康。

因此，代码层能够修复的断路隔离、真实 wake deadline、发送后 bot admission 恢复和拒绝 token 记账均已实现、测试并部署；但当前全部现有/可刷新 provider 节点中可验证的 Telegram 出口为 0，无法让 832 个绑定代理的账号正常承担各自覆盖义务。未清空断路器、未将账号切换为直连、未跨账号代发、未换绑代理，也未重放任何已进入 Gateway 或结果 unknown 的 Action。完成“正常发送我们的所有消息”仍需要一个实际可用且受支持的代理订阅/出口；获得候选后必须沿现有审计流程执行隔离 probe -> 精确 preview -> old-value/hash CAS apply -> 账号原绑定 `get_me` -> typed remote fact readback，不能由本次代码发布越权替代。

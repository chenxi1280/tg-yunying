# GitHub Actions 生产发布性能优化专项设计

## 1. 文档状态

- intake_id: `INTAKE-20260826-ACTIONS-PERFORMANCE`
- level: `L2`
- design_status: `complete`
- implementation_status: `local_complete`
- qa_status: `targeted_local_passed_actions_required`
- release_status: `not_started`
- production_status: `unproven`
- 原始需求：发布次数因高频问题修复不能减少；优化其他可以修复的 Actions、镜像和部署耗时。

## 2. 目标与不变量

本专项在不改变 release 触发频率的前提下缩短单次 `Deploy Production` 关键路径。以下不变量不可通过性能优化放宽：

1. Backend `no_postgres` 与 PostgreSQL 测试的并集必须与优化前完整集合一致，不得新增 skip、`continue-on-error` 或 silent fallback。
2. frontend 正式构建、全部生产镜像、生产迁移、writer fence、shared dispatch contract、AI content-scope takeover 和 post-deploy checks 均保留。
3. 生产大镜像继续串行拉取；不得恢复已知会放大磁盘和网络竞争的并行 pull。
4. `preview -> fingerprint/counts -> apply -> activate` 顺序保持不变，任何 drift/conflict 继续阻断激活。
5. 本轮不变更 API、页面、权限、Telegram 行为、Task 目标、Provider 路由或生产数据。

## 3. 当前基线

最近 20 次成功 workflow 的平均总耗时约 1191 秒。最新已核对样本中：

- 3799 个 `no_postgres` 测试约 592 秒；
- 762 个 PostgreSQL 测试约 444 秒；
- backend/frontend/OCR 三镜像串行约 211 秒；
- frontend 历史 assets 字节复制约 30 秒；
- 4420 条 AI content-scope takeover preview/apply 约 183 秒。

上述数据是优化优先级依据，不是发布后验收结果。

## 4. 功能设计

### 4.1 Backend 完整测试分片

新增确定性 pytest shard plugin。每个收集到的 nodeid 使用固定 SHA-256 映射：

```text
shard = first_64_bits(sha256(nodeid)) mod shard_total
```

合同如下：

- `no_postgres` 使用 3 个独立 runner；
- PostgreSQL 分区使用 2 个独立 runner，每个 runner 拥有独立 PostgreSQL/Redis service；
- marker 过滤仍由既有 `-m no_postgres` / `-m "not no_postgres"` 执行；shard plugin 在 marker 过滤前做确定性互斥分配，因此每个 marker 分区的全部 nodeid 恰好属于一个 shard；
- shard index/total 缺失、非整数、越界或分片为空均显式失败；
- 每个 runner 输出 selected/deselected 计数和最慢测试列表；
- workflow 静态回归必须校验 3+2 shard、完整 needs 和无 `continue-on-error`。

此设计增加并行 runner 数，但不减少测试数量。

### 4.2 Python 镜像依赖层缓存

Backend 与 image-verification Dockerfile 将第三方依赖安装和应用源码复制拆层：

1. 只复制 `pyproject.toml` 与依赖安装 helper；
2. 从 `build-system.requires`、`project.dependencies` 及批准的 optional dependency group 安装构建和第三方依赖；
3. 再复制完整 backend 源码；
4. 使用 `pip install -e . --no-deps` 安装当前应用包。

源码变化不再使第三方依赖层失效。`pyproject.toml` 或基础镜像变化仍会真实重建依赖层，失败必须直接暴露。该改造不引入模拟镜像、不复用旧应用代码，也不改变依赖版本声明语义。

### 4.3 三镜像并行构建

`build-images` 改为包含 backend、frontend、image-verification 三项的 matrix job。每个 job 独立 checkout、Buildx、GHCR login、cache scope 和不可变 SHA tag。`deploy.needs` 必须等待整个 matrix 成功；任一镜像失败时不得进入生产。

生产服务器仍按 backend -> image-verification -> frontend 顺序串行 pull。

### 4.4 Frontend 历史 hash 资源复用

新 release 先从新 frontend 镜像复制本版静态文件，再从当前 release 的 `assets/` 中以 `cp -aln` 补充本版缺失的历史 hash 文件：

- `-l` 只创建同文件系统 hard link，不重复复制字节；
- `-n` 保证新镜像文件优先，禁止覆盖本版同名资源；
- 只读取当前 release；当前 release 已持有此前仍需兼容的 hash 资源集合；
- release 与临时目录位于同一个 `releases` 文件系统，hard link 失败时部署显式失败，不回退到字节复制；
- 旧 release prune 只减少一个目录项，仍被新 release 链接的 inode 保持可用。

### 4.5 AI content-scope takeover

不得因性能问题跳过、截断或后台化 Stage B 接管。本轮实施：

- preview 输出 `classification_reason_counts`，键为 `classification:reason_code`，用于定位持续重规划的第一原因；
- reason 聚合来自同一批已分类结果并随 preview identity 输出，不另做生产扫描；
- zero-write 初始 drift 检查批量读取 batch 对应 Action，减少逐行主表查询；
- apply chunk 一次锁定本 chunk 的 Action 集合，再逐 item 复用原状态/hash/业务写入逻辑；
- 初始整批 drift 检查与 apply 时二次锁内校验均保留，不能用减少校验换性能。
- 修正 `fact_first_v3` 的接管绑定合同：该合同以 `primary_quantity_slot_id` 为权威绑定，Action 与 payload 均不要求旧版 `content_mix_cycle_slot_id`；数量槽一致、归属正确且 coverage 合法时不得判为重规划。
- `fact_first_v3` 数量槽缺失或事实不存在时仍判 `replan_required`；Action/payload 数量槽冲突或租户、任务归属冲突时仍判 `quarantine`，不得补造旧版周期槽或静默放行。
- preview、初始整批 drift 检查和 apply chunk 锁内复核分别构造本阶段的新鲜批量事实快照；不得跨事务边界或锁边界复用旧 preview 快照。
- 批量事实快照一次预载最新 attempt、Task/Group/目标、账号绑定、上下文、记忆、数量槽/周期槽/coverage 及回复目标事实；同一批 Action 的查询次数应为常数级，不随 Action 数线性增长。
- 批量分类必须与逐 Action 权威分类在 classification、reason 和 input hash 上完全一致；无法证明等价时不得启用批量路径。

## 5. 页面、API、权限与安全

- 页面状态：无变化。
- HTTP API/schema：无变化。
- 用户权限：无变化。
- GitHub 权限仍为 `contents: read`、`packages: write`；不新增 secret 输出。
- 依赖 helper 只读取仓库内 `pyproject.toml`，不读取环境 secret。
- workflow 日志只输出测试计数、阶段耗时和非敏感 reason enum，不输出 payload、手机号、Session、AuthKey、token 或正文。

## 6. 数据流转

```text
release SHA
  -> 5 个互斥 Backend test shards + frontend build
  -> 3 个并行 immutable image builds
  -> 串行 production deploy
  -> frontend current-assets hard-link merge
  -> writer fence / migration / dispatch stage
  -> AI takeover preview(fresh bulk facts + reason aggregation)
  -> initial drift fresh bulk facts
  -> apply chunk lock + fresh bulk facts
  -> activate / runtime checks
```

新增数据只存在于 Actions 日志中的 shard summary 与 preview 返回的 reason 聚合；不新增数据库表、列或 migration。

## 7. 边界与失败路径

1. shard 参数非法：pytest collection 前失败。
2. 某 shard 无测试：失败，防止 matrix 配置或 marker 漂移被误判成功。
3. 分片测试失败：整个 backend matrix 失败，镜像不构建。
4. 依赖声明无法解析/安装：镜像构建失败，不使用旧依赖层模拟成功。
5. 任一镜像失败：deploy 不启动。
6. 历史 assets 不存在：新部署正常继续；存在但 hard link 失败：部署失败。
7. 新旧 assets 同名：保留新镜像版本；hash 名称应保证内容寻址，冲突由新版本获胜并由页面构建/回归暴露。
8. takeover preview 为空：reason 聚合为空，原空批次合同不变。
9. takeover drift/conflict：零业务写或阻断后续激活，原合同不变。
10. `fact_first_v3` 仅有合法数量槽绑定：按当前合同继续 scope 校验，不创建旧版周期槽，不进入无意义重规划。
11. 批量事实预载与逐 Action 分类不等价：测试失败并阻断发布，不以缓存结果覆盖权威事实。

## 8. QA 验收

### 8.1 静态合同

- YAML 可解析；matrix 精确为 no-postgres 3 shard、PostgreSQL 2 shard、image 3 项。
- backend 两分区和 frontend 均为 build 前置；deploy 等待全部 image matrix。
- workflow 不出现新增 skip、`continue-on-error` 或 push/deploy触发变化。
- Dockerfile 中依赖安装发生在完整 backend `COPY` 之前，应用安装使用 `--no-deps`。
- compose-up 仍保持串行 pull 和 Stage A/B/C 顺序。

### 8.2 行为回归

- shard hash 对任意 nodeid 只返回一个合法 shard，跨全部 shard 的并集完整且互斥。
- 非法 shard 参数失败；合法 shard 输出 summary。
- frontend 新文件优先、历史缺失 hash 文件可通过 hard link 继承。
- takeover preview reason 计数与 item outcome 一致；空批次为空；drift 与 resume 回归继续通过。
- `fact_first_v3` 数量槽合法时分类为 `already_current` 或后续 scope 结果，不得出现 `content_mix_binding_missing`；缺失、冲突和归属错误仍按失败关闭合同分类。
- 相同 Action 集合在逐 Action 与批量事实路径下的 classification、reason、input hash 完全一致。
- 1 条与多条 Action 的批量分类 SELECT 次数保持常数级，不因 Action 数量线性增加。

### 8.3 本地门禁

- Backend 定向测试受 60 秒硬超时约束。
- `bash -n deploy/compose-up.sh`。
- Dockerfile 结构回归。
- YAML parse。
- `git diff --check`。

完整 4561+ 测试和真实镜像缓存命中耗时只能由精确候选 SHA 的 GitHub Actions 证明；本地定向测试不能代替。

### 8.4 2026-08-26 本地读回

- 当前仓库 pytest collection 共 4618 项；`no_postgres` 三 shard 分别选中 1240、1279、1321 项，合计 3840，全部非空。
- shard、依赖解析、workflow、Dockerfile 静态合同、release fence、takeover、frontend 资产与 merge integrity 定向回归共 44 项通过。
- workflow YAML parse、`bash -n deploy/compose-up.sh`、相关 Python compileall 与 `git diff --check` 通过。
- P0 根因回归确认 `fact_first_v3` 的合法 quantity-only Action 分类为 `already_current:scope_contract_current`；数量槽缺失仍为 `replan_required`，Action/payload 冲突仍为 `quarantine`，不创建 legacy cycle。
- takeover、scope、memory、remote reconciliation 扩展定向回归 47 项通过；批量与逐 Action 的 classification/reason/input hash 及 own-history reply 事实等价，1 条与 12 条样本的批量 SELECT 次数一致且不超过 6，quantity 关联事实漂移会改变 input hash。
- 本机 PostgreSQL 16 隔离临时实例可启动、建库和连接；使用 current metadata 的数量槽锁与 5000 条 own-history 两项真实 PostgreSQL 用例通过（4.84 秒），本次 fact-first + own-history 的逐条/批量分类也直接在 PostgreSQL 上通过；实例和临时目录均已清理。
- `0168_account_post_login_full_init.py` 已补齐 current-metadata、历史 schema 和中断态的幂等检查，SQLite current-metadata 定向回归通过。标准 PostgreSQL blank-DB 全迁移链在本机 60 秒门限内仍停留于 `0001_initial`，未再复现重复建列错误，但本地结果仍为未证明；候选 SHA 的 Actions PostgreSQL shard 必须通过后才能发布。
- 本机 Docker daemon 未运行，BuildKit `docker buildx build --check` 无法连接 socket；因此真实 Docker build/cache hit 与 Actions 总耗时仍为 `unproven`，不得把静态回归写成镜像构建通过。

## 9. Release Gate 与回滚

- migration_impact: none
- worker_impact: 发布编排不变；只优化镜像构建和 takeover 查询方式
- external_platform_impact: none
- rollback: 回滚 workflow、两个 Dockerfile、dependency helper、compose-up 资产复用和 takeover 查询/输出变更；数据库无需回滚
- observe_window: 至少比较 3 次完整 Actions 的测试 shard、镜像构建、SSH deploy 分段耗时，并确认测试总量/结果无回退
- production evidence: current SHA、容器/runtime 与 post-deploy checks；不把 Actions 加速当作 Telegram 业务修复

## 10. Product Design Complete 自检

- [x] 覆盖用户原话和“不减少发布次数”约束。
- [x] 覆盖 workflow、Docker、部署脚本、worker fence 和数据流。
- [x] 覆盖测试完整性、失败路径、并发、幂等和一致性。
- [x] 覆盖权限、secret、日志和隐私。
- [x] 覆盖发布、回滚、观测和未证明项。
- [x] 无页面/API/migration 遗漏。

结论：`design_status=complete`、`implementation_status=local_complete`、`qa_status=targeted_local_passed_actions_required`；两个 P0 的定向回归和本轮 migration 幂等回归已通过，标准 PostgreSQL blank-DB 全链仍需由候选 SHA 的完整 Actions 独立闭合，发布与生产验证尚未开始。
